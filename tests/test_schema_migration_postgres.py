import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.schema_contract import (
    BASELINE_REVISION,
    HEAD_REVISION,
    RENDER_PRODUCTION_V1_PROFILE,
    UnsupportedSchemaError,
    assert_supported_schema,
    compare_schemas,
    current_revision,
    stamp_matching_database,
    upgrade_database,
    _alembic_config,
)


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


@pytest.mark.skipif(not ADMIN_URL, reason='set TEST_POSTGRES_ADMIN_URL for PostgreSQL migration integration')
def test_fresh_upgrade_existing_stamp_and_no_runtime_schema_mutation(monkeypatch):
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    suffix = uuid.uuid4().hex[:10]
    fresh_name = f'erupted_migration_{suffix}'
    existing_name = f'erupted_existing_{suffix}'
    baseline_name = f'erupted_baseline_{suffix}'
    compatible_name = f'erupted_compatible_{suffix}'
    base_url = ADMIN_URL.rsplit('/', 1)[0]
    fresh_url = f'{base_url}/{fresh_name}'
    existing_url = f'{base_url}/{existing_name}'
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{fresh_name}"'))
        connection.execute(text(f'CREATE DATABASE "{existing_name}"'))
        connection.execute(text(f'CREATE DATABASE "{compatible_name}"'))
    fresh_engine = create_engine(fresh_url)
    existing_engine = create_engine(existing_url)
    compatible_url = f'{base_url}/{compatible_name}'
    compatible_engine = create_engine(compatible_url)
    try:
        upgrade_database(fresh_url)
        assert current_revision(fresh_engine) == HEAD_REVISION
        with fresh_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name <> 'alembic_version'"
                )
            ).scalar_one() == 90
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                    "AND ((table_name='vendor_sku_configs' AND column_name='gtin') "
                    "OR (table_name='purchase_order_lines' AND column_name='gtin'))"
                )
            ).scalar_one() == 2
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name LIKE 'schedule%'"
                )
            ).scalar_one() == 6
            assert connection.execute(
                text("SELECT principal_id IS NULL FROM employees LIMIT 1")
            ).scalar_one_or_none() in {None, True}

        command.downgrade(_alembic_config(fresh_url), '20260716_0002')
        assert current_revision(fresh_engine) == '20260716_0002'
        with fresh_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name <> 'alembic_version'"
                )
            ).scalar_one() == 74
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='employees' AND column_name='principal_id'"
                )
            ).scalar_one() == 0
        upgrade_database(fresh_url)
        assert current_revision(fresh_engine) == HEAD_REVISION

        schema_sql = Path('sql/schema.sql').read_text(encoding='utf-8')
        with existing_engine.begin() as connection:
            connection.exec_driver_sql(schema_sql)
        with pytest.raises(UnsupportedSchemaError, match='non-empty unversioned'):
            upgrade_database(existing_url)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{baseline_name}"'))
        baseline_url = f'{base_url}/{baseline_name}'
        baseline_engine = create_engine(baseline_url)
        upgrade_database(baseline_url, BASELINE_REVISION)
        comparison = compare_schemas(
            reference_engine=baseline_engine,
            target_engine=existing_engine,
            include_orm_coverage=False,
        )
        assert comparison.matches, (*comparison.differences, *comparison.orm_warnings)
        stamp_matching_database(
            database_url=existing_url,
            reference_url=baseline_url,
            revision=BASELINE_REVISION,
        )
        assert current_revision(existing_engine) == BASELINE_REVISION
        upgrade_database(existing_url)
        assert current_revision(existing_engine) == HEAD_REVISION

        upgrade_database(compatible_url, BASELINE_REVISION)
        with compatible_engine.begin() as connection:
            connection.execute(
                text(
                    'ALTER TABLE change_box_par_levels '
                    'DROP CONSTRAINT change_box_par_levels_level_non_negative_ck, '
                    'DROP CONSTRAINT change_box_par_levels_non_negative_ck'
                )
            )
            connection.execute(
                text(
                    'ALTER TABLE non_sellable_par_levels '
                    'DROP CONSTRAINT non_sellable_par_levels_level_non_negative_ck, '
                    'DROP CONSTRAINT non_sellable_par_levels_non_negative_ck'
                )
            )
            connection.execute(text('DROP TABLE alembic_version'))
        compatible_comparison = compare_schemas(
            reference_engine=baseline_engine,
            target_engine=compatible_engine,
            include_orm_coverage=False,
            compatibility_profile=RENDER_PRODUCTION_V1_PROFILE,
        )
        assert compatible_comparison.matches, compatible_comparison.differences
        assert len(compatible_comparison.accepted_differences) == 4
        stamp_matching_database(
            database_url=compatible_url,
            reference_url=baseline_url,
            revision=BASELINE_REVISION,
            compatibility_profile=RENDER_PRODUCTION_V1_PROFILE,
        )
        assert current_revision(compatible_engine) == BASELINE_REVISION
        upgrade_database(compatible_url)
        assert current_revision(compatible_engine) == HEAD_REVISION
        migrated_comparison = compare_schemas(
            reference_engine=fresh_engine,
            target_engine=compatible_engine,
            compatibility_profile=RENDER_PRODUCTION_V1_PROFILE,
        )
        assert migrated_comparison.matches, migrated_comparison.differences
        assert len(migrated_comparison.accepted_differences) == 4

        before = compare_schemas(reference_engine=fresh_engine, target_engine=existing_engine)
        assert_supported_schema(existing_engine)
        from app.main import app

        monkeypatch.setattr('app.main.assert_supported_schema', lambda: assert_supported_schema(existing_engine))
        with TestClient(app):
            pass
        after = compare_schemas(reference_engine=fresh_engine, target_engine=existing_engine)
        assert before == after
        baseline_engine.dispose()
    finally:
        fresh_engine.dispose()
        existing_engine.dispose()
        compatible_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{fresh_name}"'))
            connection.execute(text(f'DROP DATABASE IF EXISTS "{existing_name}"'))
            connection.execute(text(f'DROP DATABASE IF EXISTS "{baseline_name}"'))
            connection.execute(text(f'DROP DATABASE IF EXISTS "{compatible_name}"'))
        admin_engine.dispose()
