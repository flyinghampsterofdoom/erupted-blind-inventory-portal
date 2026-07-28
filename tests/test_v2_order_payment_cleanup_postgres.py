import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from app.schema_contract import current_revision, upgrade_database


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')
CREATED_AT = datetime(2026, 7, 28, 18, 13, 46, 242973, tzinfo=timezone.utc)
PAYMENT_SOURCE = {
    1: (29, 15), 2: (30, 9), 3: (31, 2), 4: (41, 14), 5: (43, 13),
    6: (45, 13), 7: (47, 13), 8: (49, 2), 9: (50, 9), 10: (51, 13),
    11: (52, 33), 12: (53, 31), 13: (54, 13), 14: (65, 13), 15: (66, 15),
    16: (67, 9), 17: (69, 2), 18: (72, 9), 19: (73, 33), 20: (75, 13),
    21: (77, 14), 22: (78, 2), 23: (80, 13), 24: (85, 2), 25: (86, 9),
    26: (87, 2), 27: (90, 13), 28: (92, 31), 29: (93, 14), 30: (95, 15),
    31: (96, 33), 32: (97, 2), 33: (98, 32), 34: (99, 13), 35: (100, 36),
    36: (101, 9), 37: (102, 38), 38: (107, 32), 39: (109, 33),
}


def _seed_defect_state(engine, *, downstream=False):
    vendor_ids = sorted({vendor_id for _order_id, vendor_id in PAYMENT_SOURCE.values()})
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO principals (id, username, password_hash, role, active) "
            "VALUES (6, 'cleanup-owner', 'not-a-credential', 'ADMIN', true)"
        ))
        for vendor_id in vendor_ids:
            connection.execute(
                text(
                    "INSERT INTO vendors (id, square_vendor_id, name, active) "
                    "VALUES (:id, :square_id, :name, true)"
                ),
                {'id': vendor_id, 'square_id': f'CLEANUP-V-{vendor_id}', 'name': f'Vendor {vendor_id}'},
            )
        connection.execute(text(
            "INSERT INTO payment_methods "
            "(id, display_name, category, last_four, is_active, created_by_principal_id, updated_by_principal_id) "
            "VALUES (1, 'Owner Preview Visa', 'CREDIT_CARD', '4242', true, 6, 6)"
        ))
        for payment_id, (order_id, vendor_id) in PAYMENT_SOURCE.items():
            connection.execute(
                text(
                    "INSERT INTO purchase_orders "
                    "(id, vendor_id, status, created_by_principal_id, ordered_at, submitted_at) "
                    "VALUES (:order_id, :vendor_id, 'IN_TRANSIT', 6, :created_at, :created_at)"
                ),
                {'order_id': order_id, 'vendor_id': vendor_id, 'created_at': CREATED_AT},
            )
            connection.execute(
                text(
                    "INSERT INTO order_payments "
                    "(id, purchase_order_id, vendor_id, status, financial_treatment, order_amount, "
                    "order_cost_complete, created_at, updated_at) "
                    "VALUES (:payment_id, :order_id, :vendor_id, 'UNPAID', 'INVOICE', 1, true, "
                    ":created_at, :created_at)"
                ),
                {
                    'payment_id': payment_id,
                    'order_id': order_id,
                    'vendor_id': vendor_id,
                    'created_at': CREATED_AT,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO order_payment_events "
                    "(id, order_payment_id, new_status, note, actor_principal_id, created_at) "
                    "VALUES (:payment_id, :payment_id, 'UNPAID', "
                    "'Safe V2 initialization; no paid state inferred.', 6, :created_at)"
                ),
                {'payment_id': payment_id, 'created_at': CREATED_AT},
            )
        if downstream:
            connection.execute(text(
                "INSERT INTO consignment_replenishments "
                "(id, vendor_id, purchase_order_id, ordered_cost_value, received_cost_value, "
                "amount_applied, excess_credit_created, status, created_by_principal_id) "
                "VALUES (1, 15, 29, 1, 0, 0, 0, 'PENDING', 6)"
            ))


@pytest.mark.skipif(not ADMIN_URL, reason='set TEST_POSTGRES_ADMIN_URL for PostgreSQL cleanup integration')
def test_targeted_cleanup_removes_only_exact_defect_records_and_refuses_downstream_references():
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    suffix = uuid.uuid4().hex[:10]
    safe_name = f'erupted_cleanup_safe_{suffix}'
    blocked_name = f'erupted_cleanup_blocked_{suffix}'
    base_url = ADMIN_URL.rsplit('/', 1)[0]
    safe_url = f'{base_url}/{safe_name}'
    blocked_url = f'{base_url}/{blocked_name}'
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{safe_name}"'))
        connection.execute(text(f'CREATE DATABASE "{blocked_name}"'))
    safe_engine = create_engine(safe_url)
    blocked_engine = create_engine(blocked_url)
    try:
        upgrade_database(safe_url, '20260728_0011')
        _seed_defect_state(safe_engine)
        upgrade_database(safe_url)
        assert current_revision(safe_engine) == '20260728_0012'
        with safe_engine.connect() as connection:
            assert connection.execute(text('SELECT count(*) FROM order_payments')).scalar_one() == 0
            assert connection.execute(text('SELECT count(*) FROM order_payment_events')).scalar_one() == 0
            assert connection.execute(text('SELECT count(*) FROM payment_methods')).scalar_one() == 1
            assert connection.execute(text('SELECT count(*) FROM purchase_orders')).scalar_one() == 39

        upgrade_database(blocked_url, '20260728_0011')
        _seed_defect_state(blocked_engine, downstream=True)
        with pytest.raises(RuntimeError, match='downstream financial references'):
            upgrade_database(blocked_url)
        assert current_revision(blocked_engine) == '20260728_0011'
        with blocked_engine.connect() as connection:
            assert connection.execute(text('SELECT count(*) FROM order_payments')).scalar_one() == 39
    finally:
        safe_engine.dispose()
        blocked_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{safe_name}" WITH (FORCE)'))
            connection.execute(text(f'DROP DATABASE IF EXISTS "{blocked_name}" WITH (FORCE)'))
        admin_engine.dispose()

