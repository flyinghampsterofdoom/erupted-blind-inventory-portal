from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from app.config import settings
from app.models import Base


BASELINE_REVISION = '20260715_0001'
HEAD_REVISION = '20260718_0003'
SUPPORTED_REVISIONS = frozenset({HEAD_REVISION})
RENDER_PRODUCTION_V1_PROFILE = 'render-production-v1-20260717'

_PRODUCTION_COLUMN_ORDERS: dict[str, tuple[str, ...]] = {
    'par_levels': (
        'id', 'sku', 'vendor_id', 'manual_par_level', 'suggested_par_level', 'par_source',
        'confidence_score', 'confidence_state', 'locked_manual', 'confidence_streak_up',
        'confidence_streak_down', 'updated_by_principal_id', 'created_at', 'updated_at',
        'store_id', 'manual_stock_up_level',
    ),
    'principals': (
        'id', 'username', 'password_hash', 'role', 'store_id', 'active', 'created_at',
        'updated_at', 'custom_role_label',
    ),
    'purchase_order_lines': (
        'id', 'purchase_order_id', 'variation_id', 'sku', 'item_name', 'variation_name',
        'unit_cost', 'unit_price', 'suggested_qty', 'ordered_qty', 'received_qty_total',
        'in_transit_qty', 'confidence_score', 'confidence_state', 'par_source',
        'manual_par_level', 'suggested_par_level', 'removed', 'created_at', 'updated_at', 'gtin',
    ),
    'purchase_order_store_allocations': (
        'id', 'purchase_order_line_id', 'store_id', 'expected_qty', 'allocated_qty',
        'store_received_qty', 'variance_qty', 'created_at', 'updated_at', 'manual_par_level',
    ),
    'purchase_orders': (
        'id', 'vendor_id', 'status', 'reorder_weeks', 'stock_up_weeks', 'history_lookback_days',
        'notes', 'pdf_path', 'created_by_principal_id', 'submitted_by_principal_id', 'ordered_at',
        'submitted_at', 'email_sent_at', 'email_sent_by_principal_id', 'created_at', 'updated_at',
        'invoice_payment_status', 'invoice_paid_date', 'invoice_paid_amount',
        'invoice_difference_note',
    ),
    'snapshot_lines': (
        'session_id', 'variation_id', 'sku', 'item_name', 'variation_name', 'expected_on_hand',
        'source_catalog_version', 'created_at', 'section_type', 'previous_recount_variance',
        'recount_closed_out',
    ),
    'store_recount_items': (
        'store_id', 'variation_id', 'sku', 'item_name', 'variation_name', 'last_variance',
        'updated_at', 'consecutive_match_count', 'total_count_attempts', 'last_counted_qty',
    ),
    'vendor_sku_configs': (
        'id', 'vendor_id', 'sku', 'pack_size', 'min_order_qty', 'is_default_vendor', 'active',
        'updated_by_principal_id', 'created_at', 'updated_at', 'square_variation_id', 'unit_cost',
        'gtin',
    ),
}
_CANONICAL_PRINCIPAL_ROLE_ORDER = ('ADMIN', 'MANAGER', 'LEAD', 'STORE')
_PRODUCTION_PRINCIPAL_ROLE_ORDER = ('MANAGER', 'STORE', 'ADMIN', 'LEAD')
_PRODUCTION_MISSING_CHECKS: dict[str, tuple[tuple[str, str], ...]] = {
    'change_box_par_levels': (
        ('change_box_par_levels_level_non_negative_ck', 'level_quantity >= 0'),
        ('change_box_par_levels_non_negative_ck', 'par_quantity >= 0'),
    ),
    'non_sellable_par_levels': (
        ('non_sellable_par_levels_level_non_negative_ck', 'level_quantity >= 0::numeric'),
        ('non_sellable_par_levels_non_negative_ck', 'par_quantity >= 0::numeric'),
    ),
}


class UnsupportedSchemaError(RuntimeError):
    pass


def _normalized_url(url: str) -> str:
    clean = str(url).strip()
    if clean.startswith('postgres://'):
        return 'postgresql+psycopg://' + clean[len('postgres://') :]
    if clean.startswith('postgresql://'):
        return 'postgresql+psycopg://' + clean[len('postgresql://') :]
    return clean


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        exists = connection.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one()
        if not exists:
            return None
        revisions = connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
    if len(revisions) != 1:
        raise UnsupportedSchemaError(
            f'Expected one Alembic revision row; found {len(revisions)}. Run the schema validation tooling.'
        )
    return str(revisions[0])


def assert_supported_schema(engine: Engine | None = None) -> None:
    if not settings.schema_revision_check_enabled:
        return
    if engine is None:
        from app.db import engine as application_engine

        engine = application_engine
    try:
        revision = current_revision(engine)
    except UnsupportedSchemaError:
        raise
    except Exception as exc:
        raise UnsupportedSchemaError(
            'Unable to verify the database schema revision. The application did not modify the schema.'
        ) from exc
    if revision not in SUPPORTED_REVISIONS:
        found = revision or 'unversioned'
        raise UnsupportedSchemaError(
            f'Unsupported database schema revision: {found}. Expected one of: '
            f'{", ".join(sorted(SUPPORTED_REVISIONS))}. Validate and migrate or stamp the database before startup.'
        )


def _clean_default(value: Any) -> str | None:
    if value is None:
        return None
    return ' '.join(str(value).split()).lower()


def _clean_sql(value: Any) -> str:
    return ' '.join(str(value or '').split()).lower()


def _sorted_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def schema_snapshot(engine: Engine) -> dict[str, Any]:
    inspector = inspect(engine)
    tables: dict[str, Any] = {}
    for table_name in sorted(inspector.get_table_names(schema='public')):
        if table_name == 'alembic_version':
            continue
        columns = [
            {
                'name': column['name'],
                'type': str(column['type']).lower(),
                'nullable': bool(column['nullable']),
                'default': _clean_default(column.get('default')),
            }
            for column in inspector.get_columns(table_name, schema='public')
        ]
        primary_key = inspector.get_pk_constraint(table_name, schema='public')
        unique_constraints = [
            {'name': row.get('name'), 'columns': sorted(row.get('column_names') or [])}
            for row in inspector.get_unique_constraints(table_name, schema='public')
        ]
        foreign_keys = [
            {
                'name': row.get('name'),
                'columns': row.get('constrained_columns') or [],
                'target_table': row.get('referred_table'),
                'target_columns': row.get('referred_columns') or [],
                'ondelete': (row.get('options') or {}).get('ondelete'),
            }
            for row in inspector.get_foreign_keys(table_name, schema='public')
        ]
        checks = [
            {'name': row.get('name'), 'sql': _clean_sql(row.get('sqltext'))}
            for row in inspector.get_check_constraints(table_name, schema='public')
        ]
        indexes = [
            {
                'name': row.get('name'),
                'columns': row.get('column_names') or [],
                'unique': bool(row.get('unique')),
            }
            for row in inspector.get_indexes(table_name, schema='public')
        ]
        tables[table_name] = {
            'columns': columns,
            'primary_key': primary_key.get('constrained_columns') or [],
            'unique_constraints': _sorted_rows(unique_constraints),
            'foreign_keys': _sorted_rows(foreign_keys),
            'checks': _sorted_rows(checks),
            'indexes': _sorted_rows(indexes),
        }

    with engine.connect() as connection:
        extensions = sorted(
            connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname NOT IN ('plpgsql')")
            ).scalars().all()
        )
        enums = {
            row.type_name: list(row.labels)
            for row in connection.execute(
                text(
                    """
                    SELECT t.typname AS type_name,
                           array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
                    FROM pg_type t
                    JOIN pg_enum e ON e.enumtypid = t.oid
                    JOIN pg_namespace n ON n.oid = t.typnamespace
                    WHERE n.nspname = 'public'
                    GROUP BY t.typname
                    ORDER BY t.typname
                    """
                )
            )
        }
        triggers = [
            {'table': row.table_name, 'name': row.trigger_name, 'definition': _clean_sql(row.definition)}
            for row in connection.execute(
                text(
                    """
                    SELECT c.relname AS table_name, t.tgname AS trigger_name,
                           pg_get_triggerdef(t.oid, true) AS definition
                    FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND NOT t.tgisinternal
                    ORDER BY c.relname, t.tgname
                    """
                )
            )
        ]
    return {'tables': tables, 'extensions': extensions, 'enums': enums, 'triggers': triggers}


@dataclass(frozen=True)
class SchemaComparison:
    matches: bool
    differences: tuple[str, ...]
    orm_warnings: tuple[str, ...]
    accepted_differences: tuple[str, ...] = ()


def _normalized_for_compatibility(
    reference: dict[str, Any],
    target: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    normalized = copy.deepcopy(target)
    accepted: list[str] = []

    for table_name, production_order in _PRODUCTION_COLUMN_ORDERS.items():
        reference_table = reference['tables'].get(table_name)
        target_table = normalized['tables'].get(table_name)
        if reference_table is None or target_table is None:
            continue
        reference_columns = reference_table['columns']
        target_columns = target_table['columns']
        if target_columns == reference_columns:
            continue
        target_order = tuple(column['name'] for column in target_columns)
        if (
            target_order == production_order
            and _sorted_rows(target_columns) == _sorted_rows(reference_columns)
        ):
            target_table['columns'] = copy.deepcopy(reference_columns)
            accepted.append(f'{table_name} physical column order')

    reference_roles = tuple(reference['enums'].get('principal_role', ()))
    target_roles = tuple(normalized['enums'].get('principal_role', ()))
    if (
        reference_roles == _CANONICAL_PRINCIPAL_ROLE_ORDER
        and target_roles == _PRODUCTION_PRINCIPAL_ROLE_ORDER
    ):
        normalized['enums']['principal_role'] = list(reference_roles)
        accepted.append('principal_role enum order')

    for table_name, missing_checks in _PRODUCTION_MISSING_CHECKS.items():
        reference_table = reference['tables'].get(table_name)
        target_table = normalized['tables'].get(table_name)
        if reference_table is None or target_table is None:
            continue
        expected_rows = [
            {'name': name, 'sql': sql}
            for name, sql in missing_checks
        ]
        reference_checks = reference_table['checks']
        target_checks = target_table['checks']
        if target_checks == reference_checks:
            continue
        if all(row in reference_checks for row in expected_rows):
            without_known = [row for row in reference_checks if row not in expected_rows]
            if target_checks == without_known:
                target_table['checks'] = copy.deepcopy(reference_checks)
                accepted.extend(f'{table_name}.{row["name"]} absent' for row in expected_rows)

    return normalized, tuple(accepted)


def compare_schemas(
    *,
    reference_engine: Engine,
    target_engine: Engine,
    include_orm_coverage: bool = True,
    compatibility_profile: str | None = None,
) -> SchemaComparison:
    if compatibility_profile not in {None, RENDER_PRODUCTION_V1_PROFILE}:
        raise UnsupportedSchemaError(f'Unknown schema compatibility profile: {compatibility_profile}')
    reference = schema_snapshot(reference_engine)
    target = schema_snapshot(target_engine)
    accepted_differences: tuple[str, ...] = ()
    if compatibility_profile == RENDER_PRODUCTION_V1_PROFILE:
        reference_revision = current_revision(reference_engine)
        if reference_revision not in {BASELINE_REVISION, HEAD_REVISION}:
            raise UnsupportedSchemaError(
                f'{RENDER_PRODUCTION_V1_PROFILE} requires a versioned canonical migration reference.'
            )
        target, accepted_differences = _normalized_for_compatibility(reference, target)
    differences = tuple(
        f'{section} differ from the migration-created reference schema'
        for section in ('tables', 'extensions', 'enums', 'triggers')
        if reference[section] != target[section]
    )
    orm_warnings: list[str] = []
    if include_orm_coverage:
        target_tables = target['tables']
        for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
            actual = target_tables.get(table.name)
            if actual is None:
                orm_warnings.append(f'ORM table missing from database: {table.name}')
                continue
            actual_columns = {column['name'] for column in actual['columns']}
            for column in table.columns:
                if column.name not in actual_columns:
                    orm_warnings.append(f'ORM column missing from database: {table.name}.{column.name}')
    return SchemaComparison(
        not differences and not orm_warnings,
        differences,
        tuple(orm_warnings),
        accepted_differences,
    )


def _alembic_config(database_url: str) -> Config:
    config = Config('alembic.ini')
    normalized = _normalized_url(database_url)
    config.attributes['database_url_override'] = normalized
    config.set_main_option('sqlalchemy.url', normalized.replace('%', '%%'))
    return config


def upgrade_database(database_url: str, revision: str = 'head') -> None:
    normalized = _normalized_url(database_url)
    engine = create_engine(normalized, pool_pre_ping=True)
    try:
        existing_revision = current_revision(engine)
        if existing_revision is None:
            business_tables = [
                table_name
                for table_name in inspect(engine).get_table_names(schema='public')
                if table_name != 'alembic_version'
            ]
            if business_tables:
                raise UnsupportedSchemaError(
                    'Refusing to run the baseline upgrade against a non-empty unversioned database. '
                    'Create a migrated reference, validate the existing database, then use stamp-existing.'
                )
    finally:
        engine.dispose()
    command.upgrade(_alembic_config(normalized), revision)


def stamp_matching_database(
    *,
    database_url: str,
    reference_url: str,
    revision: str = HEAD_REVISION,
    compatibility_profile: str | None = None,
) -> None:
    if revision not in {BASELINE_REVISION, HEAD_REVISION}:
        raise UnsupportedSchemaError(f'Refusing to stamp unsupported revision: {revision}')
    if compatibility_profile and revision != BASELINE_REVISION:
        raise UnsupportedSchemaError(
            f'{compatibility_profile} may stamp only the V1 baseline revision.'
        )
    target_engine = create_engine(_normalized_url(database_url), pool_pre_ping=True)
    reference_engine = create_engine(_normalized_url(reference_url), pool_pre_ping=True)
    try:
        reference_revision = current_revision(reference_engine)
        if reference_revision != revision:
            raise UnsupportedSchemaError(
                f'Reference database is at {reference_revision or "unversioned"}; expected {revision}.'
            )
        comparison = compare_schemas(
            reference_engine=reference_engine,
            target_engine=target_engine,
            include_orm_coverage=revision == HEAD_REVISION,
            compatibility_profile=compatibility_profile,
        )
        if not comparison.matches:
            details = '; '.join((*comparison.differences, *comparison.orm_warnings))
            raise UnsupportedSchemaError(f'Refusing to stamp a non-matching database: {details}')
        if current_revision(target_engine) is not None:
            raise UnsupportedSchemaError('Refusing to stamp a database that already has a revision.')
    finally:
        target_engine.dispose()
        reference_engine.dispose()
    command.stamp(_alembic_config(database_url), revision)


def _comparison_command(
    database_url: str,
    reference_url: str,
    compatibility_profile: str | None = None,
) -> int:
    target_engine = create_engine(_normalized_url(database_url), pool_pre_ping=True)
    reference_engine = create_engine(_normalized_url(reference_url), pool_pre_ping=True)
    try:
        reference_revision = current_revision(reference_engine)
        comparison = compare_schemas(
            reference_engine=reference_engine,
            target_engine=target_engine,
            include_orm_coverage=reference_revision == HEAD_REVISION,
            compatibility_profile=compatibility_profile,
        )
    finally:
        target_engine.dispose()
        reference_engine.dispose()
    for item in comparison.differences:
        print(f'DRIFT: {item}')
    for item in comparison.orm_warnings:
        print(f'ORM: {item}')
    for item in comparison.accepted_differences:
        print(f'ACCEPTED {compatibility_profile}: {item}')
    if comparison.matches:
        print('Schema matches the versioned migration reference and required ORM coverage.')
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='V2 schema baseline and drift tooling')
    subparsers = parser.add_subparsers(dest='command', required=True)
    upgrade_parser = subparsers.add_parser('upgrade', help='Create/upgrade an empty or versioned database')
    upgrade_parser.add_argument('--database-url', default=settings.database_url_normalized)
    upgrade_parser.add_argument('--revision', default='head')
    validate_parser = subparsers.add_parser('validate', help='Compare a target with a migrated reference database')
    validate_parser.add_argument('--database-url', default=settings.database_url_normalized)
    validate_parser.add_argument('--reference-url', required=True)
    validate_parser.add_argument('--compatibility-profile', choices=[RENDER_PRODUCTION_V1_PROFILE])
    stamp_parser = subparsers.add_parser('stamp-existing', help='Validate and stamp an existing matching database')
    stamp_parser.add_argument('--database-url', default=settings.database_url_normalized)
    stamp_parser.add_argument('--reference-url', required=True)
    stamp_parser.add_argument('--revision', default=HEAD_REVISION)
    stamp_parser.add_argument('--compatibility-profile', choices=[RENDER_PRODUCTION_V1_PROFILE])
    args = parser.parse_args()
    if args.command == 'upgrade':
        upgrade_database(args.database_url, args.revision)
        return 0
    if args.command == 'validate':
        return _comparison_command(args.database_url, args.reference_url, args.compatibility_profile)
    stamp_matching_database(
        database_url=args.database_url,
        reference_url=args.reference_url,
        revision=args.revision,
        compatibility_profile=args.compatibility_profile,
    )
    profile_note = f' using {args.compatibility_profile}' if args.compatibility_profile else ''
    print(f'Database stamped at {args.revision} after validated schema comparison{profile_note}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
