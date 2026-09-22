from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ParLevel,
    ParLevelSource,
    PurchaseOrder,
    PurchaseOrderConfidenceState,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseOrderStoreAllocation,
    Store,
    Vendor,
    VendorSkuConfig,
)
from app.services.purchase_order_generation_service import (
    generate_vendor_scoped_recommendations,
    list_selected_vendor_skus,
)
from app.services.purchase_order_admin_service import add_purchase_order_line_by_sku
from app.services.purchase_order_math_service import MathOverrides
from app.services.purchase_order_math_service import OrderingMathParams
from app.services.square_ordering_data_service import sync_vendor_sku_configs_from_square


TABLES = (
    'stores',
    'vendors',
    'vendor_sku_configs',
    'ordering_product_lifecycle',
    'par_levels',
    'purchase_orders',
    'purchase_order_lines',
    'purchase_order_store_allocations',
)


@pytest.fixture()
def db():
    engine = create_engine('sqlite+pysqlite:///:memory:')
    @event.listens_for(engine, 'connect')
    def sqlite_functions(conn, _):
        conn.create_function('char_length', 1, lambda value: len(value) if value is not None else None)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in TABLES])
    session = Session(engine)
    counters: dict[str, int] = {}

    @event.listens_for(session, 'before_flush')
    def assign_bigint_ids(_session, _context, _instances):
        for row in session.new:
            if hasattr(row, 'id') and row.id is None:
                key = type(row).__name__
                counters[key] = counters.get(key, 100) + 1
                row.id = counters[key]

    session.add_all([
        Store(id=1, name='Downtown', square_location_id='LOC-1', active=True),
        Vendor(id=1, square_vendor_id='SQUARE-JUICE', name='Juice Head', active=True),
        Vendor(id=2, square_vendor_id='SQUARE-BIG', name='BIG Wholesale', active=True),
        Vendor(id=3, square_vendor_id='SQUARE-STALE', name='Stale Vendor', active=True),
    ])
    session.commit()
    yield session
    session.close()


def mapping(
    *,
    row_id: int,
    vendor_id: int,
    cost: Decimal | None,
    active: bool = True,
    default: bool = False,
    pack_size: int = 1,
    min_order_qty: int = 0,
) -> VendorSkuConfig:
    return VendorSkuConfig(
        id=row_id,
        vendor_id=vendor_id,
        sku='JH-ABC',
        square_variation_id='VAR-JH',
        gtin='GTIN-JH',
        unit_cost=cost,
        pack_size=pack_size,
        min_order_qty=min_order_qty,
        active=active,
        is_default_vendor=default,
    )


def square_response(*, square_cost_cents: int | None = 999999) -> dict:
    vendor_info = {'vendor_id': 'SQUARE-BIG', 'ordinal': 1}
    if square_cost_cents is not None:
        vendor_info['price_money'] = {'amount': square_cost_cents}
    return {
        'items': [{
            'item_data': {
                'variations': [{
                    'id': 'VAR-JH',
                    'item_variation_data': {
                        'sku': 'JH-ABC',
                        'upc': 'GTIN-JH',
                        'item_variation_vendor_infos': [
                            {'item_variation_vendor_info_data': vendor_info}
                        ],
                    },
                }]
            }
        }]
    }


def run_sync(db: Session, *, square_cost_cents: int | None = 999999) -> dict[str, object]:
    with patch(
        'app.services.square_ordering_data_service._square_post',
        return_value=square_response(square_cost_cents=square_cost_cents),
    ):
        result = sync_vendor_sku_configs_from_square(db)
    db.flush()
    return result


def test_reassigns_existing_destination_and_carries_local_cost_and_par(db):
    old = mapping(
        row_id=10,
        vendor_id=1,
        cost=Decimal('11.90'),
        default=True,
        pack_size=6,
        min_order_qty=12,
    )
    destination = mapping(row_id=11, vendor_id=2, cost=None)
    old_par = ParLevel(
        id=20,
        vendor_id=1,
        store_id=1,
        sku='JH-ABC',
        manual_par_level=8,
        manual_stock_up_level=20,
        suggested_par_level=18,
        par_source=ParLevelSource.MANUAL,
        confidence_score=Decimal('0.8750'),
        confidence_state=PurchaseOrderConfidenceState.NORMAL,
        locked_manual=True,
        confidence_streak_up=3,
        confidence_streak_down=1,
        updated_by_principal_id=77,
    )
    db.add_all([old, destination, old_par])
    db.flush()

    result = run_sync(db)

    assert old.active is True
    assert old.is_default_vendor is False
    assert destination.active is True
    assert destination.is_default_vendor is True
    assert destination.unit_cost == Decimal('11.90')
    assert destination.pack_size == 1
    assert destination.min_order_qty == 0
    assert result['vendor_reassigned'] == 1
    assert result['reassignments'] == [{
        'sku': 'JH-ABC',
        'old_vendor_id': 1,
        'new_vendor_id': 2,
    }]
    carried = db.scalar(select(ParLevel).where(
        ParLevel.vendor_id == 2,
        ParLevel.store_id == 1,
        ParLevel.sku == 'JH-ABC',
    ))
    assert carried is not None
    for field in (
        'manual_par_level', 'manual_stock_up_level', 'suggested_par_level',
        'par_source', 'confidence_score', 'confidence_state', 'locked_manual',
        'confidence_streak_up', 'confidence_streak_down', 'updated_by_principal_id',
    ):
        assert getattr(carried, field) == getattr(old_par, field)


def test_new_destination_inherits_only_local_cost_and_ordering_configuration(db):
    old = mapping(
        row_id=10,
        vendor_id=1,
        cost=Decimal('11.90'),
        default=True,
        pack_size=6,
        min_order_qty=12,
    )
    db.add(old)
    db.flush()

    result = run_sync(db, square_cost_cents=2500)

    destination = db.scalar(select(VendorSkuConfig).where(
        VendorSkuConfig.vendor_id == 2,
        VendorSkuConfig.sku == 'JH-ABC',
    ))
    assert destination is not None
    assert destination.unit_cost == Decimal('11.90')
    assert destination.unit_cost != Decimal('25.00')
    assert destination.pack_size == 6
    assert destination.min_order_qty == 12
    assert destination.active is True
    assert destination.is_default_vendor is True
    assert result['created'] == 1
    assert result['vendor_reassigned'] == 1


def test_existing_destination_cost_and_explicit_par_win(db):
    old = mapping(row_id=10, vendor_id=1, cost=Decimal('11.90'), default=True)
    destination = mapping(row_id=11, vendor_id=2, cost=Decimal('12.25'))
    source_par = ParLevel(
        id=20, vendor_id=1, store_id=1, sku='JH-ABC', manual_par_level=8,
        manual_stock_up_level=20, par_source=ParLevelSource.MANUAL, locked_manual=True,
    )
    destination_par = ParLevel(
        id=21, vendor_id=2, store_id=1, sku='JH-ABC', manual_par_level=12,
        manual_stock_up_level=30, par_source=ParLevelSource.MANUAL, locked_manual=True,
    )
    db.add_all([old, destination, source_par, destination_par])
    db.flush()

    run_sync(db, square_cost_cents=5000)

    assert destination.unit_cost == Decimal('12.25')
    assert destination_par.manual_par_level == 12
    assert destination_par.manual_stock_up_level == 30


def test_inactive_destination_is_reactivated_and_unknown_cost_stays_unknown(db):
    old = mapping(row_id=10, vendor_id=1, cost=None, default=True)
    destination = mapping(row_id=11, vendor_id=2, cost=None, active=False)
    stale = mapping(row_id=12, vendor_id=3, cost=Decimal('99.00'), active=False, default=True)
    db.add_all([old, destination, stale])
    db.flush()

    result = run_sync(db, square_cost_cents=0)

    assert destination.active is True
    assert destination.is_default_vendor is True
    assert destination.unit_cost is None
    assert result['reactivated'] == 1
    defaults = db.scalars(select(VendorSkuConfig).where(
        VendorSkuConfig.sku == 'JH-ABC',
        VendorSkuConfig.active.is_(True),
        VendorSkuConfig.is_default_vendor.is_(True),
    )).all()
    assert defaults == [destination]
    assert old.is_default_vendor is False
    assert stale.is_default_vendor is False


def test_historical_po_line_and_financial_tables_are_not_updated(db):
    old = mapping(row_id=10, vendor_id=1, cost=Decimal('11.90'), default=True)
    po = PurchaseOrder(
        id=30,
        vendor_id=1,
        status=PurchaseOrderStatus.IN_TRANSIT,
        created_by_principal_id=7,
    )
    line = PurchaseOrderLine(
        id=31,
        purchase_order_id=30,
        variation_id='VAR-JH',
        sku='JH-ABC',
        item_name='Juice Head',
        variation_name='ABC',
        unit_cost=Decimal('11.90'),
        ordered_qty=6,
        in_transit_qty=6,
    )
    allocation = PurchaseOrderStoreAllocation(
        id=32,
        purchase_order_line_id=31,
        store_id=1,
        expected_qty=6,
        allocated_qty=6,
    )
    db.add_all([old, po, line, allocation])
    db.flush()
    statements: list[str] = []

    @event.listens_for(db.bind, 'before_cursor_execute')
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    run_sync(db)

    assert po.vendor_id == 1
    assert line.unit_cost == Decimal('11.90')
    assert allocation.allocated_qty == 6
    forbidden_history_tables = (
        'purchase_orders', 'purchase_order_lines', 'purchase_order_store_allocations',
        'order_payments', 'funding_reports', 'consignment_replenishments',
    )
    assert not any(
        statement.lstrip().startswith(('update ', 'delete ', 'insert '))
        and any(table in statement for table in forbidden_history_tables)
        for statement in statements
    )


def test_future_default_generation_uses_big_and_old_vendor_inbound_is_subtracted(db):
    old = mapping(row_id=10, vendor_id=1, cost=Decimal('11.90'), default=True)
    po = PurchaseOrder(
        id=30,
        vendor_id=1,
        status=PurchaseOrderStatus.IN_TRANSIT,
        created_by_principal_id=7,
    )
    line = PurchaseOrderLine(
        id=31,
        purchase_order_id=30,
        variation_id='VAR-JH',
        sku='JH-ABC',
        item_name='Juice Head',
        variation_name='ABC',
        unit_cost=Decimal('11.90'),
        ordered_qty=4,
        in_transit_qty=4,
    )
    allocation = PurchaseOrderStoreAllocation(
        id=32,
        purchase_order_line_id=31,
        store_id=1,
        expected_qty=4,
        allocated_qty=4,
    )
    db.add_all([old, po, line, allocation])
    db.flush()
    run_sync(db)
    db.add(ParLevel(
        id=40,
        vendor_id=2,
        store_id=1,
        sku='JH-ABC',
        manual_par_level=5,
        manual_stock_up_level=10,
        par_source=ParLevelSource.MANUAL,
        locked_manual=True,
    ))
    db.flush()

    selected = list_selected_vendor_skus(db, vendor_ids=[1, 2])
    assert [row.vendor_id for rows in selected.values() for row in rows] == [2]

    with patch(
        'app.services.purchase_order_generation_service.resolve_effective_math_params',
        return_value=OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=30),
    ):
        lines = generate_vendor_scoped_recommendations(
            db,
            vendor_ids=[2],
            history_loader=lambda *_args: [Decimal('0')] * 30,
            on_hand_loader=lambda *_args: Decimal('0'),
            overrides=MathOverrides(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=30),
        )
    assert len(lines) == 1
    assert lines[0].vendor_id == 2
    assert lines[0].result.rounded_recommended_qty == 6


def test_manual_po_can_still_add_sku_from_old_active_non_default_vendor(db):
    old = mapping(row_id=10, vendor_id=1, cost=Decimal('11.90'), default=True)
    db.add(old)
    db.flush()
    run_sync(db)
    assert old.active is True
    assert old.is_default_vendor is False

    manual_po = PurchaseOrder(
        id=50,
        vendor_id=1,
        status=PurchaseOrderStatus.DRAFT,
        created_by_principal_id=7,
    )
    db.add(manual_po)
    db.flush()

    with patch(
        'app.services.purchase_order_admin_service.fetch_catalog_by_sku',
        return_value={},
    ):
        line, action = add_purchase_order_line_by_sku(
            db,
            purchase_order_id=manual_po.id,
            sku='JH-ABC',
            initial_qty=3,
        )

    assert action == 'added'
    assert line.purchase_order_id == manual_po.id
    assert line.sku == 'JH-ABC'
    assert line.unit_cost == Decimal('11.90')
    assert line.ordered_qty == 3
