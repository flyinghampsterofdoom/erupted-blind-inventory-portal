"""Exercise the nullable report migration against isolated PostgreSQL."""

import os
import uuid

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


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_ADMIN_URL"), reason="Disposable PostgreSQL required"
)
def test_unknown_report_migration_preserves_history_and_refuses_lossy_downgrade():
    admin_url = os.environ["TEST_POSTGRES_ADMIN_URL"]
    name = "consignment_unknown_" + uuid.uuid4().hex[:12]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    url = admin_url.rsplit("/", 1)[0] + "/" + name
    engine = create_engine(url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        upgrade_database(url, "20260911_0024")
        with engine.begin() as conn:
            conn.execute(
                text("""
                INSERT INTO principals(id, username, password_hash, role)
                VALUES (9901, 'consignment-test', 'unused', 'ADMIN');
                INSERT INTO vendors(id, square_vendor_id, name, active) VALUES (9901, 'LOCAL-TEST', 'Consignment test', true);
                INSERT INTO funding_accounts(id, account_type, vendor_id, display_name,
                    created_by_principal_id, updated_by_principal_id)
                VALUES (9901, 'CONSIGNMENT', 9901, 'Test', 9901, 9901);
                INSERT INTO funding_reports(id, account_id, vendor_id, report_number,
                    account_name_snapshot, account_type_snapshot, sales_start_date,
                    sales_end_date, status, calculated_cogs, created_by_principal_id)
                VALUES (9901, 9901, 9901, 'Historical', 'Test', 'CONSIGNMENT',
                    '2026-07-01', '2026-07-02', 'FINALIZED', 20, 9901);
            """)
            )
            before = conn.execute(
                text("SELECT row_to_json(r)::text FROM funding_reports r WHERE id=9901")
            ).scalar_one()
        upgrade_database(url)
        assert current_revision(engine) == HEAD_REVISION
        with engine.begin() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT row_to_json(r)::text FROM funding_reports r WHERE id=9901"
                    )
                ).scalar_one()
                == before
            )
            nullable = conn.execute(
                text("""SELECT is_nullable FROM information_schema.columns
                WHERE table_name='funding_report_lines' AND column_name='unit_cost_snapshot'""")
            ).scalar_one()
            assert nullable == "YES"
            conn.execute(
                text("""INSERT INTO funding_reports(id, account_id, vendor_id, report_number,
                account_name_snapshot, account_type_snapshot, sales_start_date, sales_end_date,
                status, calculated_cogs, inventory_units_snapshot, inventory_value_snapshot,
                created_by_principal_id) VALUES (9902,9901,9901,'Unknown','Test','CONSIGNMENT',
                '2026-07-03','2026-07-04','DRAFT',NULL,NULL,NULL,9901)""")
            )
            assert (
                conn.execute(
                    text("SELECT calculated_cogs FROM funding_reports WHERE id=9902")
                ).scalar_one()
                is None
            )
        with pytest.raises(IntegrityError):
            command.downgrade(_alembic_config(url), "20260911_0024")
        assert current_revision(engine) == HEAD_REVISION
        with engine.begin() as conn:
            assert (
                conn.execute(
                    text("SELECT calculated_cogs FROM funding_reports WHERE id=9902")
                ).scalar_one()
                is None
            )
            # Delete only this test's draft to exercise a lossless downgrade.
            conn.execute(text("DELETE FROM funding_reports WHERE id=9902"))
        command.downgrade(_alembic_config(url), "20260911_0024")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT row_to_json(r)::text FROM funding_reports r WHERE id=9901"
                    )
                ).scalar_one()
                == before
            )
        upgrade_database(url)
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
