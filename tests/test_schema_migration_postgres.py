import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

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
            ).scalar_one() == 157
            scheduling_tables = set(connection.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                "AND table_name IN ('scheduling_organization_policies', 'special_store_policies', "
                "'special_store_rotation_states', 'scheduling_notifications', 'shift_transfer_requests')"
            )).scalars())
            assert scheduling_tables == {
                'scheduling_organization_policies', 'special_store_policies',
                'special_store_rotation_states', 'scheduling_notifications',
                'shift_transfer_requests',
            }
            scheduling_enums = set(connection.execute(text(
                "SELECT typname FROM pg_type WHERE typname IN "
                "('schedule_lifecycle_stage', 'store_preference_level', "
                "'special_store_participation', 'shift_transfer_status')"
            )).scalars())
            assert scheduling_enums == {
                'schedule_lifecycle_stage', 'store_preference_level',
                'special_store_participation', 'shift_transfer_status',
            }
            reporting_tables = set(connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                    "AND table_name = 'reporting_saved_views'"
                )
            ).scalars())
            assert reporting_tables == {'reporting_saved_views'}
            assert set(connection.execute(text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'public.reporting_saved_views'::regclass"
            )).scalars()) >= {
                'reporting_saved_views_principal_name_uniq',
                'reporting_saved_views_report_type_ck',
            }
            funding_tables = set(connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                    "AND table_name LIKE 'funding_%'"
                )
            ).scalars())
            assert funding_tables == {
                'funding_accounts', 'funding_sku_mappings', 'funding_reports',
                'funding_report_lines', 'funding_report_fact_links',
                'funding_report_exclusions', 'funding_report_adjustments',
                'funding_payments', 'funding_payment_allocations', 'funding_ledger_entries',
            }
            assert connection.execute(text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='funding_report_lines' "
                "AND column_name='mapping_id'"
            )).scalar_one() == 'YES'
            assert set(connection.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='funding_report_lines' "
                "AND column_name IN ('purchase_order_line_id', "
                "'purchase_order_receipt_line_id', 'lot_received_at_snapshot')"
            )).scalars()) == {
                'purchase_order_line_id',
                'purchase_order_receipt_line_id',
                'lot_received_at_snapshot',
            }
            assert connection.execute(text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='funding_report_fact_links' "
                "AND column_name='allocated_quantity'"
            )).scalar_one() == 'YES'
            assert set(connection.execute(text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema='public' AND column_name='vendor_id' "
                "AND table_name IN ('funding_reports', 'funding_payments')"
            )).scalars()) == {'funding_reports', 'funding_payments'}
            funding_constraints = set(connection.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid IN "
                    "('public.funding_accounts'::regclass, 'public.funding_sku_mappings'::regclass, "
                    "'public.funding_reports'::regclass, 'public.funding_report_fact_links'::regclass, "
                    "'public.funding_payments'::regclass, 'public.funding_ledger_entries'::regclass)"
                )
            ).scalars())
            assert {
                'funding_accounts_owner_ck', 'funding_sku_mappings_period_ck',
                'funding_reports_period_ck', 'funding_report_fact_links_one_source_ck',
                'funding_payments_amount_ck', 'funding_ledger_entries_direction_ck',
                'funding_report_fact_links_sale_line_uniq',
                'funding_report_fact_links_return_line_uniq',
            } <= funding_constraints
            ordering_catalog_tables = set(connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                    "AND table_name IN ('ordering_catalog_identity', 'ordering_catalog_refresh_state')"
                )
            ).scalars())
            assert ordering_catalog_tables == {'ordering_catalog_identity', 'ordering_catalog_refresh_state'}
            ordering_inventory_tables = set(connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                    "AND table_name IN ('ordering_inventory_refresh_runs', 'ordering_current_inventory')"
                )
            ).scalars())
            assert ordering_inventory_tables == {'ordering_inventory_refresh_runs', 'ordering_current_inventory'}
            ordering_inventory_constraints = set(connection.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid IN "
                    "('public.ordering_inventory_refresh_runs'::regclass, "
                    "'public.ordering_current_inventory'::regclass)"
                )
            ).scalars())
            assert {
                'ordering_inventory_refresh_runs_result_ck',
                'ordering_inventory_refresh_runs_counts_non_negative_ck',
                'ordering_inventory_refresh_runs_coverage_ck',
                'ordering_inventory_refresh_runs_outcome_ck',
                'ordering_inventory_refresh_runs_time_order_ck',
                'ordering_current_inventory_freshness_ck',
            } <= ordering_inventory_constraints
            catalog_constraints = set(connection.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid IN "
                    "('public.ordering_catalog_identity'::regclass, "
                    "'public.ordering_catalog_refresh_state'::regclass)"
                )
            ).scalars())
            assert {
                'ordering_catalog_identity_product_name_length_ck',
                'ordering_catalog_identity_sku_length_ck',
                'ordering_catalog_refresh_state_singleton_ck',
                'ordering_catalog_refresh_state_result_ck',
                'ordering_catalog_refresh_state_counts_ck',
            } <= catalog_constraints
            lifecycle_constraints = set(connection.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'public.ordering_product_lifecycle'::regclass"
                )
            ).scalars())
            assert {
                'ordering_product_lifecycle_status_ck',
                'ordering_product_lifecycle_pre_archive_status_ck',
                'ordering_product_lifecycle_row_version_ck',
                'ordering_product_lifecycle_note_length_ck',
                'ordering_product_lifecycle_sku_length_ck',
                'ordering_product_lifecycle_name_length_ck',
                'ordering_product_lifecycle_archive_evidence_ck',
                'ordering_product_lifecycle_nfr_evidence_ck',
            } <= lifecycle_constraints
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "INSERT INTO ordering_product_lifecycle "
                            "(square_variation_id, status, row_version) VALUES ('INVALID', 'INFERRED', 1)"
                        )
                    )
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
            store_shift_constraints = set(connection.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'public.store_shifts'::regclass"
                )
            ).scalars())
            assert {
                'store_shifts_store_label_uniq',
                'store_shifts_time_order_ck',
                'store_shifts_weekdays_ck',
            } <= store_shift_constraints
            store_shift_index = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
                    "AND indexname='idx_store_shifts_store_active_order'"
                )
            ).scalar_one()
            assert '(store_id, active, display_order)' in store_shift_index
            source_columns = connection.execute(
                text(
                    "SELECT table_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema='public' AND column_name='source_store_shift_id' "
                    "AND table_name IN ('schedule_shifts', 'schedule_template_shifts')"
                )
            ).all()
            assert set(source_columns) == {
                ('schedule_shifts', 'YES'),
                ('schedule_template_shifts', 'YES'),
            }
            source_foreign_keys = set(connection.execute(
                text(
                    "SELECT conrelid::regclass::text, confdeltype FROM pg_constraint "
                    "WHERE conname IN ('schedule_shifts_source_store_shift_id_fkey', "
                    "'schedule_template_shifts_source_store_shift_id_fkey')"
                )
            ).all())
            assert source_foreign_keys == {
                ('schedule_shifts', 'n'),
                ('schedule_template_shifts', 'n'),
            }

        command.downgrade(_alembic_config(fresh_url), '20260725_0008')
        assert current_revision(fresh_engine) == '20260725_0008'
        with fresh_engine.connect() as connection:
            assert connection.execute(
                text("SELECT to_regclass('public.ordering_current_inventory') IS NULL")
            ).scalar_one() is True
            assert connection.execute(
                text("SELECT to_regclass('public.ordering_catalog_identity') IS NOT NULL")
            ).scalar_one() is True
        upgrade_database(fresh_url)
        assert current_revision(fresh_engine) == HEAD_REVISION

        command.downgrade(_alembic_config(fresh_url), '20260725_0007')
        assert current_revision(fresh_engine) == '20260725_0007'
        with fresh_engine.connect() as connection:
            assert connection.execute(
                text("SELECT to_regclass('public.ordering_catalog_identity') IS NULL")
            ).scalar_one() is True
            assert connection.execute(
                text("SELECT to_regclass('public.ordering_current_inventory') IS NULL")
            ).scalar_one() is True
            assert connection.execute(
                text("SELECT to_regclass('public.ordering_product_lifecycle') IS NOT NULL")
            ).scalar_one() is True
        upgrade_database(fresh_url)
        assert current_revision(fresh_engine) == HEAD_REVISION

        command.downgrade(_alembic_config(fresh_url), '20260720_0006')
        assert current_revision(fresh_engine) == '20260720_0006'
        with fresh_engine.connect() as connection:
            assert connection.execute(
                text("SELECT to_regclass('public.ordering_product_lifecycle') IS NULL")
            ).scalar_one() is True
        upgrade_database(fresh_url)
        assert current_revision(fresh_engine) == HEAD_REVISION

        command.downgrade(_alembic_config(fresh_url), '20260718_0003')
        assert current_revision(fresh_engine) == '20260718_0003'
        with fresh_engine.connect() as connection:
            assert connection.execute(
                text("SELECT to_regclass('public.store_shifts') IS NULL")
            ).scalar_one() is True
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                    "AND column_name='source_store_shift_id' "
                    "AND table_name IN ('schedule_shifts', 'schedule_template_shifts')"
                )
            ).scalar_one() == 0
        upgrade_database(fresh_url)
        assert current_revision(fresh_engine) == HEAD_REVISION

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


@pytest.mark.skipif(not ADMIN_URL, reason='set TEST_POSTGRES_ADMIN_URL for PostgreSQL migration integration')
def test_vendor_scope_backfill_preserves_ambiguous_reports_and_round_trips():
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_vendor_scope_{uuid.uuid4().hex[:10]}'
    database_url = f"{ADMIN_URL.rsplit('/', 1)[0]}/{database_name}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(database_url)
    try:
        upgrade_database(database_url, '20260803_0016')
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO principals (id, username, password_hash, role)
                VALUES (6, 'migration-owner', 'not-used', 'ADMIN');
                INSERT INTO vendors (id, square_vendor_id, name, active) VALUES
                  (10, 'MIG-10', 'Alpha Vendor', TRUE),
                  (12, 'MIG-12', 'Zulu Vendor', TRUE),
                  (13, 'MIG-13', 'Solo Vendor', TRUE),
                  (14, 'MIG-14', 'Inactive Vendor', FALSE);
                INSERT INTO payment_methods
                  (id, display_name, category, is_active, created_by_principal_id, updated_by_principal_id)
                VALUES
                  (20, 'Shared card', 'CREDIT_CARD', TRUE, 6, 6),
                  (21, 'Solo card', 'CREDIT_CARD', TRUE, 6, 6);
                INSERT INTO vendor_payment_settings
                  (vendor_id, default_payment_method_id, updated_by_principal_id)
                VALUES (10, 20, 6), (12, 20, 6), (13, 21, 6), (14, 21, 6);
                INSERT INTO funding_accounts
                  (id, account_type, vendor_id, payment_method_id, display_name,
                   created_by_principal_id, updated_by_principal_id)
                VALUES
                  (1, 'CONSIGNMENT', 10, NULL, 'Consignment', 6, 6),
                  (2, 'CREDIT_CARD', NULL, 20, 'Ambiguous card', 6, 6),
                  (3, 'CREDIT_CARD', NULL, 21, 'Solo card', 6, 6);
                INSERT INTO funding_reports
                  (id, account_id, report_number, account_name_snapshot, account_type_snapshot,
                   sales_start_date, sales_end_date, created_by_principal_id)
                VALUES
                  (1, 1, 'CONS-1', 'Consignment', 'CONSIGNMENT', '2026-07-01', '2026-07-02', 6),
                  (3, 2, 'AMB-3', 'Ambiguous card', 'CREDIT_CARD', '2026-07-01', '2026-07-02', 6),
                  (6, 2, 'AMB-6', 'Ambiguous card', 'CREDIT_CARD', '2026-07-03', '2026-07-04', 6),
                  (7, 3, 'SOLO-7', 'Solo card', 'CREDIT_CARD', '2026-07-01', '2026-07-02', 6);
                INSERT INTO funding_payments
                  (id, account_id, entry_type, amount, payment_date, reason, created_by_principal_id)
                VALUES
                  (1, 1, 'PAYMENT', 5, '2026-07-05', 'Consignment payment', 6),
                  (2, 2, 'PAYMENT', 5, '2026-07-05', 'Ambiguous card payment', 6),
                  (3, 3, 'PAYMENT', 5, '2026-07-05', 'Solo card payment', 6)
            """))
        upgrade_database(database_url)
        with engine.connect() as connection:
            assert dict(connection.execute(text(
                'SELECT id, vendor_id FROM funding_reports ORDER BY id')).all()) == {
                    1: 10, 3: None, 6: None, 7: 13}
            assert dict(connection.execute(text(
                'SELECT id, vendor_id FROM funding_payments ORDER BY id')).all()) == {
                    1: 10, 2: None, 3: 13}
        command.downgrade(_alembic_config(database_url), '20260803_0016')
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                "AND column_name='vendor_id' AND table_name IN ('funding_reports','funding_payments')"
            )).scalar_one() == 0
        upgrade_database(database_url)
        assert current_revision(engine) == HEAD_REVISION
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
