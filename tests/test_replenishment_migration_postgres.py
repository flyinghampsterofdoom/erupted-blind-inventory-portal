import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.schema_contract import (
    HEAD_REVISION,
    _alembic_config,
    current_revision,
    upgrade_database,
)

ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


@pytest.mark.skipif(
    not ADMIN_URL,
    reason='set TEST_POSTGRES_ADMIN_URL for PostgreSQL migration integration',
)
def test_replenishment_migration_upgrades_deployed_chain_and_round_trips():
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_replenishment_{uuid.uuid4().hex[:10]}'
    database_url = f"{ADMIN_URL.rsplit('/', 1)[0]}/{database_name}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(database_url)
    try:
        upgrade_database(database_url, '20260805_0017')
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO principals (id, username, password_hash, role)
                VALUES (610, 'replenishment-migration-owner', 'not-used', 'ADMIN');
                INSERT INTO vendors (id, square_vendor_id, name, active)
                VALUES (620, 'REPLENISHMENT-MIGRATION-VENDOR', 'Migration Vendor', TRUE);
                INSERT INTO purchase_orders (id, vendor_id, created_by_principal_id)
                VALUES (630, 620, 610), (631, 620, 610);
            """))

        upgrade_database(database_url, '20260812_0019')
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO reporting_saved_views
                    (id, principal_id, name, report_type, configuration)
                VALUES
                    (640, 610, 'Existing sales view', 'sales_analysis', '{}'),
                    (641, 610, 'Existing stock view', 'stock_value', '{}');
            """))

        upgrade_database(database_url)
        assert current_revision(engine) == HEAD_REVISION
        with engine.begin() as connection:
            assert connection.execute(text(
                'SELECT count(*) FROM purchase_orders '
                'WHERE id IN (630, 631) AND creation_idempotency_key IS NULL'
            )).scalar_one() == 2
            connection.execute(text(
                'INSERT INTO purchase_orders '
                '(id, vendor_id, created_by_principal_id, creation_idempotency_key) '
                "VALUES (632, 620, 610, NULL), (633, 620, 610, NULL), "
                "(634, 620, 610, 'replenishment-key-000000000001')"
            ))
            assert connection.execute(text(
                'SELECT count(*) FROM purchase_orders '
                'WHERE id IN (630, 631, 632, 633) AND creation_idempotency_key IS NULL'
            )).scalar_one() == 4
            assert connection.execute(text(
                "SELECT array_agg(report_type ORDER BY id) FROM reporting_saved_views "
                'WHERE id IN (640, 641)'
            )).scalar_one() == ['sales_analysis', 'stock_value']
            connection.execute(text("""
                INSERT INTO reporting_saved_views
                    (id, principal_id, name, report_type, configuration)
                VALUES (642, 610, 'Replenishment view', 'replenishment', '{}')
            """))

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(
                'INSERT INTO purchase_orders '
                '(id, vendor_id, created_by_principal_id, creation_idempotency_key) '
                "VALUES (635, 620, 610, 'replenishment-key-000000000001')"
            ))

        barrier = Barrier(2)

        def concurrent_insert(order_id: int) -> str:
            try:
                with engine.begin() as connection:
                    barrier.wait()
                    connection.execute(text(
                        'INSERT INTO purchase_orders '
                        '(id, vendor_id, created_by_principal_id, creation_idempotency_key) '
                        f"VALUES ({order_id}, 620, 610, 'replenishment-concurrent-key-01')"
                    ))
                return 'created'
            except IntegrityError:
                return 'duplicate'

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(concurrent_insert, (636, 637)))
        assert sorted(outcomes) == ['created', 'duplicate']
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT count(*) FROM purchase_orders WHERE "
                "creation_idempotency_key='replenishment-concurrent-key-01'"
            )).scalar_one() == 1

        with engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM reporting_saved_views WHERE report_type = 'replenishment'"
            ))
        command.downgrade(_alembic_config(database_url), '20260812_0019')
        assert current_revision(engine) == '20260812_0019'
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='purchase_orders' "
                "AND column_name='creation_idempotency_key'"
            )).scalar_one() == 0
            definition = connection.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='reporting_saved_views_report_type_ck'"
            )).scalar_one()
            assert 'replenishment' not in definition

        upgrade_database(database_url)
        assert current_revision(engine) == HEAD_REVISION
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
