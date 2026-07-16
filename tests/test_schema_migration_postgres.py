import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.schema_contract import (
    BASELINE_REVISION,
    UnsupportedSchemaError,
    assert_supported_schema,
    compare_schemas,
    current_revision,
    stamp_matching_database,
    upgrade_database,
)


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


@pytest.mark.skipif(not ADMIN_URL, reason='set TEST_POSTGRES_ADMIN_URL for PostgreSQL migration integration')
def test_fresh_upgrade_existing_stamp_and_no_runtime_schema_mutation(monkeypatch):
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    suffix = uuid.uuid4().hex[:10]
    fresh_name = f'erupted_migration_{suffix}'
    existing_name = f'erupted_existing_{suffix}'
    base_url = ADMIN_URL.rsplit('/', 1)[0]
    fresh_url = f'{base_url}/{fresh_name}'
    existing_url = f'{base_url}/{existing_name}'
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{fresh_name}"'))
        connection.execute(text(f'CREATE DATABASE "{existing_name}"'))
    fresh_engine = create_engine(fresh_url)
    existing_engine = create_engine(existing_url)
    try:
        upgrade_database(fresh_url)
        assert current_revision(fresh_engine) == BASELINE_REVISION
        with fresh_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name <> 'alembic_version'"
                )
            ).scalar_one() == 72
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                    "AND ((table_name='vendor_sku_configs' AND column_name='gtin') "
                    "OR (table_name='purchase_order_lines' AND column_name='gtin'))"
                )
            ).scalar_one() == 2

        schema_sql = Path('sql/schema.sql').read_text(encoding='utf-8')
        with existing_engine.begin() as connection:
            connection.exec_driver_sql(schema_sql)
        with pytest.raises(UnsupportedSchemaError, match='non-empty unversioned'):
            upgrade_database(existing_url)
        comparison = compare_schemas(reference_engine=fresh_engine, target_engine=existing_engine)
        assert comparison.matches, (*comparison.differences, *comparison.orm_warnings)
        stamp_matching_database(database_url=existing_url, reference_url=fresh_url)
        assert current_revision(existing_engine) == BASELINE_REVISION

        before = compare_schemas(reference_engine=fresh_engine, target_engine=existing_engine)
        assert_supported_schema(existing_engine)
        from app.main import app

        monkeypatch.setattr('app.main.assert_supported_schema', lambda: assert_supported_schema(existing_engine))
        with TestClient(app):
            pass
        after = compare_schemas(reference_engine=fresh_engine, target_engine=existing_engine)
        assert before == after
    finally:
        fresh_engine.dispose()
        existing_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{fresh_name}" WITH (FORCE)'))
            connection.execute(text(f'DROP DATABASE IF EXISTS "{existing_name}" WITH (FORCE)'))
        admin_engine.dispose()
