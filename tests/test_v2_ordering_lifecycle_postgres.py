import os
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.schema_contract import upgrade_database
from app.services.v2_ordering_lifecycle_repository import (
    LifecycleWorkspaceFilters,
    load_lifecycle_states,
    query_lifecycle_workspace,
)
from app.services.v2_ordering_lifecycle_service import (
    LifecycleCommand,
    LifecycleSelection,
    LifecycleTransitionError,
    transition_lifecycle,
)


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


@pytest.mark.skipif(not ADMIN_URL, reason='set TEST_POSTGRES_ADMIN_URL for Ordering lifecycle PostgreSQL integration')
def test_postgres_atomic_batch_audit_and_optimistic_conflict():
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_lifecycle_{uuid.uuid4().hex[:10]}'
    database_url = f"{ADMIN_URL.rsplit('/', 1)[0]}/{database_name}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(database_url)
    try:
        upgrade_database(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO principals (id, username, password_hash, role, active) "
                    "VALUES (900001, 'lifecycle-owner', 'not-a-login-hash', 'ADMIN', true)"
                )
            )
        with Session(engine) as db:
            first = transition_lifecycle(
                db,
                command=LifecycleCommand.SET_NO_FUTURE_REORDER,
                selections=(LifecycleSelection('VAR-1', 0), LifecycleSelection('VAR-2', 0)),
                actor_principal_id=900001,
            )
            db.commit()
            assert first.changed_count == 2
            assert {state.status for state in load_lifecycle_states(db, {'VAR-1', 'VAR-2'}).values()} == {
                'NO_FUTURE_REORDER'
            }
            assert db.execute(
                text("SELECT count(*) FROM audit_log WHERE action='V2:ordering_lifecycle:lifecycle_status_changed'")
            ).scalar_one() == 2

        with Session(engine) as first_session, Session(engine) as stale_session:
            first_version = load_lifecycle_states(first_session, {'VAR-1'})['VAR-1'].row_version
            stale_version = load_lifecycle_states(stale_session, {'VAR-1'})['VAR-1'].row_version
            transition_lifecycle(
                first_session,
                command=LifecycleCommand.SET_ACTIVE,
                selections=(LifecycleSelection('VAR-1', first_version),),
                actor_principal_id=900001,
            )
            first_session.commit()
            with pytest.raises(LifecycleTransitionError) as conflict:
                transition_lifecycle(
                    stale_session,
                    command=LifecycleCommand.SET_ACTIVE,
                    selections=(LifecycleSelection('VAR-1', stale_version),),
                    actor_principal_id=900001,
                )
            stale_session.rollback()
            assert conflict.value.code == 'STALE_VERSION'

        with Session(engine) as db:
            before = load_lifecycle_states(db, {'VAR-1', 'VAR-2'})
            with pytest.raises(LifecycleTransitionError):
                transition_lifecycle(
                    db,
                    command=LifecycleCommand.ARCHIVE,
                    selections=(
                        LifecycleSelection('VAR-1', before['VAR-1'].row_version),
                        LifecycleSelection('VAR-2', before['VAR-2'].row_version - 1),
                    ),
                    actor_principal_id=900001,
                )
            db.rollback()
            after = load_lifecycle_states(db, {'VAR-1', 'VAR-2'})
            assert after == before
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()


@pytest.mark.skipif(not ADMIN_URL, reason='set TEST_POSTGRES_ADMIN_URL for Ordering lifecycle PostgreSQL integration')
def test_production_sized_workspace_uses_ordering_identity_without_touchscreen_rows_or_n_plus_one():
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_lifecycle_workspace_{uuid.uuid4().hex[:10]}'
    database_url = f"{ADMIN_URL.rsplit('/', 1)[0]}/{database_name}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(database_url)
    try:
        upgrade_database(database_url)
        mappings = [
            {
                'id': index,
                'sku': f'SKU-{index:04d}',
                'variation_id': f'VAR-{index:04d}',
            }
            for index in range(1, 825)
        ]
        identities = [
            {
                'variation_id': row['variation_id'],
                'sku': row['sku'],
                'item_name': f'Product {row["id"]:04d}',
                'variation_name': 'Default',
                'product_name': f'Product {row["id"]:04d} — Default',
            }
            for row in mappings[:-4]
        ]
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO vendors (id, square_vendor_id, name, active) "
                "VALUES (900001, 'SQUARE-VENDOR-900001', 'Catalog Vendor', true)"
            ))
            connection.execute(
                text(
                    "INSERT INTO vendor_sku_configs "
                    "(id, vendor_id, sku, square_variation_id, unit_cost, pack_size, min_order_qty, "
                    "is_default_vendor, active) VALUES "
                    "(:id, 900001, :sku, :variation_id, 0, 1, 0, true, true)"
                ),
                mappings,
            )
            connection.execute(
                text(
                    "INSERT INTO ordering_catalog_identity "
                    "(square_variation_id, sku, item_name, variation_name, product_name, "
                    "square_is_deleted, last_seen_at) VALUES "
                    "(:variation_id, :sku, :item_name, :variation_name, :product_name, false, now())"
                ),
                identities,
            )
            assert connection.execute(text('SELECT count(*) FROM touchscreen_square_variation_cache')).scalar_one() == 0
            assert connection.execute(text('SELECT count(*) FROM touchscreen_store_inventory_cache')).scalar_one() == 0

        select_count = 0

        def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
            nonlocal select_count
            if statement.lstrip().upper().startswith('SELECT'):
                select_count += 1

        event.listen(engine, 'before_cursor_execute', count_selects)
        try:
            with Session(engine) as db:
                workspace = query_lifecycle_workspace(
                    db,
                    archived=False,
                    filters=LifecycleWorkspaceFilters(),
                    sort='product',
                    direction='asc',
                    page_number=1,
                    page_size=50,
                )
                unknown = query_lifecycle_workspace(
                    db,
                    archived=False,
                    filters=LifecycleWorkspaceFilters(name_state='UNKNOWN'),
                    sort='product',
                    direction='asc',
                    page_number=1,
                    page_size=50,
                )
        finally:
            event.remove(engine, 'before_cursor_execute', count_selects)

        assert workspace.unfiltered_count == 824
        assert workspace.coverage == workspace.coverage.__class__(824, 820, 4, 'NEVER', None, None, None)
        assert workspace.rows[0].product_name.startswith('Product ')
        assert unknown.total_count == 4
        assert all(row.product_name == 'Product name unavailable' for row in unknown.rows)
        assert select_count == 12
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
