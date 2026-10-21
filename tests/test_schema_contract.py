from pathlib import Path
import copy
import hashlib

import pytest

from app.schema_contract import (
    BASELINE_REVISION,
    HEAD_REVISION,
    RENDER_PRODUCTION_V1_PROFILE,
    UnsupportedSchemaError,
    _PRODUCTION_COLUMN_ORDERS,
    compare_schemas,
    assert_supported_schema,
    stamp_matching_database,
)


def test_baseline_uses_full_deployed_schema_and_contains_runtime_gtin_columns():
    schema = Path('sql/schema.sql').read_text(encoding='utf-8')
    migration = Path('migrations/versions/20260715_0001_v1_schema_baseline.py').read_text(encoding='utf-8')
    assert schema.count('CREATE TABLE IF NOT EXISTS') == 72
    assert 'CREATE EXTENSION IF NOT EXISTS citext' in schema
    assert 'vendor_sku_configs ADD COLUMN IF NOT EXISTS gtin' in schema
    assert 'purchase_order_lines ADD COLUMN IF NOT EXISTS gtin' in schema
    assert "revision = '20260715_0001'" in migration
    assert hashlib.sha256(schema.encode('utf-8')).hexdigest() in migration


class _RevisionEngine:
    pass


def _table(columns, checks=()):
    return {
        'columns': [
            {'name': name, 'type': 'text', 'nullable': True, 'default': None}
            for name in columns
        ],
        'primary_key': [],
        'unique_constraints': [],
        'foreign_keys': [],
        'checks': [dict(row) for row in checks],
        'indexes': [],
    }


def _compatibility_snapshots():
    canonical_tables = {
        name: _table(sorted(order))
        for name, order in _PRODUCTION_COLUMN_ORDERS.items()
    }
    canonical_tables.update(
        {
            'change_box_par_levels': _table(
                ('store_id', 'denomination_code', 'level_quantity', 'par_quantity'),
                (
                    {'name': 'change_box_par_levels_level_non_negative_ck', 'sql': 'level_quantity >= 0'},
                    {'name': 'change_box_par_levels_non_negative_ck', 'sql': 'par_quantity >= 0'},
                ),
            ),
            'non_sellable_par_levels': _table(
                ('store_id', 'item_id', 'level_quantity', 'par_quantity'),
                (
                    {
                        'name': 'non_sellable_par_levels_level_non_negative_ck',
                        'sql': 'level_quantity >= 0::numeric',
                    },
                    {
                        'name': 'non_sellable_par_levels_non_negative_ck',
                        'sql': 'par_quantity >= 0::numeric',
                    },
                ),
            ),
        }
    )
    canonical = {
        'tables': canonical_tables,
        'extensions': ['citext'],
        'enums': {'principal_role': ['ADMIN', 'MANAGER', 'LEAD', 'STORE']},
        'triggers': [],
    }
    production = copy.deepcopy(canonical)
    for name, order in _PRODUCTION_COLUMN_ORDERS.items():
        by_name = {row['name']: row for row in production['tables'][name]['columns']}
        production['tables'][name]['columns'] = [by_name[column_name] for column_name in order]
    production['enums']['principal_role'] = ['MANAGER', 'STORE', 'ADMIN', 'LEAD']
    production['tables']['change_box_par_levels']['checks'] = []
    production['tables']['non_sellable_par_levels']['checks'] = []
    return canonical, production


def _comparison(monkeypatch, target_snapshot, reference_snapshot=None, *, reference_revision=BASELINE_REVISION):
    canonical, _ = _compatibility_snapshots()
    reference_snapshot = reference_snapshot or canonical
    reference_engine = object()
    target_engine = object()
    monkeypatch.setattr(
        'app.schema_contract.schema_snapshot',
        lambda engine: copy.deepcopy(reference_snapshot if engine is reference_engine else target_snapshot),
    )
    monkeypatch.setattr(
        'app.schema_contract.current_revision',
        lambda engine: reference_revision if engine is reference_engine else None,
    )
    return compare_schemas(
        reference_engine=reference_engine,
        target_engine=target_engine,
        include_orm_coverage=False,
        compatibility_profile=RENDER_PRODUCTION_V1_PROFILE,
    )


def test_startup_accepts_only_supported_revision(monkeypatch):
    monkeypatch.setattr('app.schema_contract.current_revision', lambda _engine: HEAD_REVISION)
    assert_supported_schema(_RevisionEngine())
    monkeypatch.setattr('app.schema_contract.current_revision', lambda _engine: None)
    with pytest.raises(UnsupportedSchemaError, match='unversioned'):
        assert_supported_schema(_RevisionEngine())


def test_stamp_rejects_unknown_revision_before_connecting():
    with pytest.raises(UnsupportedSchemaError, match='unsupported revision'):
        stamp_matching_database(
            database_url='postgresql+psycopg:///unused',
            reference_url='postgresql+psycopg:///unused',
            revision='unknown',
        )


def test_render_production_profile_cannot_stamp_a_non_baseline_revision():
    with pytest.raises(UnsupportedSchemaError, match='may stamp only the V1 baseline'):
        stamp_matching_database(
            database_url='postgresql+psycopg:///unused',
            reference_url='postgresql+psycopg:///unused',
            revision=HEAD_REVISION,
            compatibility_profile=RENDER_PRODUCTION_V1_PROFILE,
        )


def test_render_production_profile_accepts_only_exact_known_differences(monkeypatch):
    canonical, production = _compatibility_snapshots()
    comparison = _comparison(monkeypatch, production, canonical)
    assert comparison.matches
    assert set(comparison.accepted_differences) == {
        *(f'{table_name} physical column order' for table_name in _PRODUCTION_COLUMN_ORDERS),
        'principal_role enum order',
        'change_box_par_levels.change_box_par_levels_level_non_negative_ck absent',
        'change_box_par_levels.change_box_par_levels_non_negative_ck absent',
        'non_sellable_par_levels.non_sellable_par_levels_level_non_negative_ck absent',
        'non_sellable_par_levels.non_sellable_par_levels_non_negative_ck absent',
    }


def test_render_production_profile_accepts_immutable_canonical_baseline(monkeypatch):
    canonical, _ = _compatibility_snapshots()
    comparison = _comparison(monkeypatch, canonical, canonical)
    assert comparison.matches
    assert comparison.accepted_differences == ()


@pytest.mark.parametrize(
    'mutation',
    (
        lambda snapshot: snapshot['tables'].pop('principals'),
        lambda snapshot: snapshot['tables']['principals']['columns'].pop(),
        lambda snapshot: snapshot['tables']['principals']['columns'][0].update(type='bigint'),
        lambda snapshot: snapshot['enums']['principal_role'].append('UNEXPECTED'),
        lambda snapshot: snapshot['tables']['change_box_par_levels']['checks'].append(
            {'name': 'unexpected_check', 'sql': 'store_id > 0'}
        ),
        lambda snapshot: snapshot['tables'].update({'unexpected_table': _table(('id',))}),
    ),
    ids=('missing-table', 'missing-column', 'changed-type', 'enum-value', 'constraint-drift', 'extra-table'),
)
def test_render_production_profile_rejects_every_additional_drift(monkeypatch, mutation):
    canonical, production = _compatibility_snapshots()
    mutation(production)
    comparison = _comparison(monkeypatch, production, canonical)
    assert not comparison.matches
    assert comparison.differences


def test_render_production_profile_requires_versioned_canonical_reference(monkeypatch):
    canonical, production = _compatibility_snapshots()
    with pytest.raises(UnsupportedSchemaError, match='versioned canonical migration reference'):
        _comparison(monkeypatch, production, canonical, reference_revision=None)


def test_unknown_compatibility_profile_is_rejected():
    with pytest.raises(UnsupportedSchemaError, match='Unknown schema compatibility profile'):
        compare_schemas(
            reference_engine=object(),
            target_engine=object(),
            compatibility_profile='unrelated-schema',
        )
