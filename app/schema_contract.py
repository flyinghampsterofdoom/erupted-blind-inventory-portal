from __future__ import annotations

import argparse
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
SUPPORTED_REVISIONS = frozenset({BASELINE_REVISION})


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


def compare_schemas(*, reference_engine: Engine, target_engine: Engine) -> SchemaComparison:
    reference = schema_snapshot(reference_engine)
    target = schema_snapshot(target_engine)
    differences = tuple(
        f'{section} differ from the migration-created reference schema'
        for section in ('tables', 'extensions', 'enums', 'triggers')
        if reference[section] != target[section]
    )
    target_tables = target['tables']
    orm_warnings: list[str] = []
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        actual = target_tables.get(table.name)
        if actual is None:
            orm_warnings.append(f'ORM table missing from database: {table.name}')
            continue
        actual_columns = {column['name'] for column in actual['columns']}
        for column in table.columns:
            if column.name not in actual_columns:
                orm_warnings.append(f'ORM column missing from database: {table.name}.{column.name}')
    return SchemaComparison(not differences and not orm_warnings, differences, tuple(orm_warnings))


def _alembic_config(database_url: str) -> Config:
    config = Config('alembic.ini')
    normalized = _normalized_url(database_url)
    config.attributes['database_url_override'] = normalized
    config.set_main_option('sqlalchemy.url', normalized.replace('%', '%%'))
    return config


def upgrade_database(database_url: str) -> None:
    normalized = _normalized_url(database_url)
    engine = create_engine(normalized, pool_pre_ping=True)
    try:
        revision = current_revision(engine)
        if revision is None:
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
    command.upgrade(_alembic_config(normalized), 'head')


def stamp_matching_database(*, database_url: str, reference_url: str) -> None:
    target_engine = create_engine(_normalized_url(database_url), pool_pre_ping=True)
    reference_engine = create_engine(_normalized_url(reference_url), pool_pre_ping=True)
    try:
        comparison = compare_schemas(reference_engine=reference_engine, target_engine=target_engine)
        if not comparison.matches:
            details = '; '.join((*comparison.differences, *comparison.orm_warnings))
            raise UnsupportedSchemaError(f'Refusing to stamp a non-matching database: {details}')
        if current_revision(target_engine) is not None:
            raise UnsupportedSchemaError('Refusing to stamp a database that already has a revision.')
    finally:
        target_engine.dispose()
        reference_engine.dispose()
    command.stamp(_alembic_config(database_url), BASELINE_REVISION)


def _comparison_command(database_url: str, reference_url: str) -> int:
    target_engine = create_engine(_normalized_url(database_url), pool_pre_ping=True)
    reference_engine = create_engine(_normalized_url(reference_url), pool_pre_ping=True)
    try:
        comparison = compare_schemas(reference_engine=reference_engine, target_engine=target_engine)
    finally:
        target_engine.dispose()
        reference_engine.dispose()
    for item in comparison.differences:
        print(f'DRIFT: {item}')
    for item in comparison.orm_warnings:
        print(f'ORM: {item}')
    if comparison.matches:
        print('Schema matches migration reference and ORM table/column coverage.')
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='V2 schema baseline and drift tooling')
    subparsers = parser.add_subparsers(dest='command', required=True)
    upgrade_parser = subparsers.add_parser('upgrade', help='Create/upgrade an empty or versioned database')
    upgrade_parser.add_argument('--database-url', default=settings.database_url_normalized)
    validate_parser = subparsers.add_parser('validate', help='Compare a target with a migrated reference database')
    validate_parser.add_argument('--database-url', default=settings.database_url_normalized)
    validate_parser.add_argument('--reference-url', required=True)
    stamp_parser = subparsers.add_parser('stamp-existing', help='Validate and stamp an existing matching database')
    stamp_parser.add_argument('--database-url', default=settings.database_url_normalized)
    stamp_parser.add_argument('--reference-url', required=True)
    args = parser.parse_args()
    if args.command == 'upgrade':
        upgrade_database(args.database_url)
        return 0
    if args.command == 'validate':
        return _comparison_command(args.database_url, args.reference_url)
    stamp_matching_database(database_url=args.database_url, reference_url=args.reference_url)
    print(f'Database stamped at {BASELINE_REVISION} after exact schema validation.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
