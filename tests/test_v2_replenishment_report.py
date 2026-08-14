from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.auth import Principal as AuthPrincipal
from app.auth import Role
from app.main import app
from app.models import (
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    OrderingInventoryRefreshRun,
    OrderingProductLifecycle,
    Principal,
    PrincipalRole,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStoreAllocation,
    ReportingSavedView,
    Store,
    Vendor,
    VendorSkuConfig,
)
from app.routers.v2_reporting import _context, replenishment_finalize_route
from app.services.v2_replenishment_report_service import (
    build_replenishment_preview,
    create_replenishment_purchase_order,
    run_replenishment_report,
)


@compiles(CITEXT, "sqlite")
def _compile_citext_sqlite(_type, _compiler, **_kwargs):
    return "TEXT"


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)


@pytest.fixture()
def replenishment_db():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def sqlite_functions(connection, _record):
        connection.create_function(
            "char_length", 1, lambda value: len(value) if value is not None else None
        )

    tables = (
        Store.__table__,
        Principal.__table__,
        Vendor.__table__,
        VendorSkuConfig.__table__,
        OrderingCatalogIdentity.__table__,
        OrderingProductLifecycle.__table__,
        OrderingInventoryRefreshRun.__table__,
        OrderingCurrentInventory.__table__,
        ConsignmentSaleFact.__table__,
        ConsignmentSalesSyncState.__table__,
        ReportingSavedView.__table__,
        PurchaseOrder.__table__,
        PurchaseOrderLine.__table__,
        PurchaseOrderStoreAllocation.__table__,
    )
    for table in tables:
        table.create(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def assign_ids(session, *_):
        counters = {
            PurchaseOrder: 100,
            PurchaseOrderLine: 200,
            PurchaseOrderStoreAllocation: 300,
        }
        for row in session.new:
            for model, start in counters.items():
                if isinstance(row, model) and row.id is None:
                    used = [
                        item.id
                        for item in session.new
                        if isinstance(item, model) and item.id is not None
                    ]
                    row.id = max(used or [start - 1]) + 1

    event.listen(Session, "before_flush", assign_ids)
    with SessionLocal() as db:
        db.add_all(
            [
                Store(id=1, name="North", square_location_id="N", active=True),
                Principal(
                    id=1,
                    username="owner",
                    password_hash="x",
                    role=PrincipalRole.ADMIN,
                    active=True,
                ),
                Vendor(
                    id=1, square_vendor_id="V-1", name="Primary Vendor", active=True
                ),
                Vendor(
                    id=2, square_vendor_id="V-2", name="Alternate Vendor", active=True
                ),
                OrderingInventoryRefreshRun(
                    id=1,
                    correlation_id="report-test",
                    result="COMPLETE",
                    expected_variation_count=4,
                    active_store_count=1,
                    expected_pair_count=4,
                    covered_pair_count=4,
                    missing_pair_count=0,
                    square_request_count=1,
                    started_at=NOW,
                    completed_at=NOW,
                    refreshed_by_principal_id=1,
                ),
                ConsignmentSalesSyncState(
                    id=1,
                    last_result="COMPLETE",
                    last_successful_through_at=datetime(
                        2026, 8, 15, tzinfo=timezone.utc
                    ),
                ),
            ]
        )
        for variation_id, sku, primary, vendor_id, pack, moq in (
            ("A", "SKU-A", True, 1, 6, 12),
            ("A", "SKU-A", False, 2, 1, 0),
            ("B", "SKU-B", True, 1, 1, 0),
            ("C", "SKU-C", True, 1, 1, 0),
            ("D", "SKU-D", True, 1, 1, 0),
        ):
            db.add(
                VendorSkuConfig(
                    id=10 + len(db.new),
                    vendor_id=vendor_id,
                    sku=sku,
                    square_variation_id=variation_id,
                    unit_cost=Decimal("2.50"),
                    pack_size=pack,
                    min_order_qty=moq,
                    is_default_vendor=primary,
                    active=True,
                )
            )
        for variation_id in ("A", "B", "C", "D"):
            db.add(
                OrderingCatalogIdentity(
                    square_variation_id=variation_id,
                    sku=f"SKU-{variation_id}",
                    item_name=f"Product {variation_id}",
                    variation_name="Default",
                    product_name=f"Product {variation_id} — Default",
                    square_is_deleted=False,
                    last_seen_at=NOW,
                )
            )
            db.add(
                OrderingCurrentInventory(
                    square_variation_id=variation_id,
                    store_id=1,
                    square_location_id="N",
                    counted_quantity=Decimal(2),
                    refreshed_at=(
                        NOW
                        if variation_id != "C"
                        else datetime(2026, 8, 10, tzinfo=timezone.utc)
                    ),
                    freshness_state="FRESH",
                    refresh_run_id=1,
                )
            )
        db.add(
            OrderingProductLifecycle(
                square_variation_id="D",
                status="NO_FUTURE_REORDER",
                no_future_reorder_at=NOW,
                no_future_reorder_by_principal_id=1,
            )
        )
        for row_id, variation_id, qty in ((1, "A", "5"), (2, "C", "2"), (3, "D", "1")):
            db.add(
                ConsignmentSaleFact(
                    id=row_id,
                    square_order_id=f"O-{row_id}",
                    square_line_item_uid=f"L-{row_id}",
                    square_variation_id=variation_id,
                    square_product_id=f"I-{variation_id}",
                    square_location_id="N",
                    store_id=1,
                    business_date=date(2026, 8, 10),
                    transacted_at=NOW,
                    quantity_sold=Decimal(qty),
                    gross_sales_amount=Decimal(10),
                    discount_amount=Decimal(0),
                    tax_amount=Decimal(0),
                    net_sales_amount=Decimal(10),
                    currency="USD",
                    product_name_snapshot=f"Product {variation_id}",
                    variation_name_snapshot="Default",
                    sku_snapshot=f"SKU-{variation_id}",
                    vendor_id_snapshot=1,
                    vendor_name_snapshot="Primary Vendor",
                    extended_cogs_snapshot=Decimal(5),
                    attribution_status="ATTRIBUTED",
                    attribution_source="SYNC",
                    source_synchronized_at=NOW,
                )
            )
        db.commit()
        yield db
    event.remove(Session, "before_flush", assign_ids)
    engine.dispose()


def test_report_uses_28_day_velocity_trusted_inventory_and_first_class_exclusions(
    replenishment_db,
):
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
        store_ids=[1],
        as_of=date(2026, 8, 14),
        now=NOW,
    )
    primary_a = next(
        row for row in report.rows if row.vendor_id == 1 and row.variation_id == "A"
    )
    alternate_a = next(
        row for row in report.rows if row.vendor_id == 2 and row.variation_id == "A"
    )
    row_b = next(row for row in report.rows if row.variation_id == "B")
    row_c = next(row for row in report.rows if row.variation_id == "C")
    row_d = next(row for row in report.rows if row.variation_id == "D")
    assert primary_a.selected_units_sold == Decimal(5)
    assert primary_a.average_weekly_sales == Decimal("1.25")
    assert primary_a.weeks_of_supply == Decimal("1.6")
    assert not primary_a.excluded and not alternate_a.is_primary_vendor
    assert row_b.excluded and "No sales in trailing 28 days" in row_b.exclusion_reasons
    assert row_c.current_on_hand is None and row_c.weeks_of_supply is None
    assert row_d.excluded and "No Future Reorder" in row_d.exclusion_reasons


def test_preview_modes_make_rounding_visible_and_withhold_inventory_dependent_advice(
    replenishment_db,
):
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
        store_ids=[1],
        as_of=date(2026, 8, 14),
        now=NOW,
    )
    replacement = build_replenishment_preview(report, vendor_id=1, mode="replace_sales")
    line_a = next(line for line in replacement.lines if line.row.variation_id == "A")
    assert line_a.raw_suggested_qty == Decimal(5)
    assert line_a.adjusted_suggested_qty == 12
    assert line_a.adjustment_note == "MOQ 12"

    target = build_replenishment_preview(
        report, vendor_id=1, mode="target_weeks", target_weeks=4
    )
    line_c = next(line for line in target.lines if line.row.variation_id == "C")
    assert line_c.raw_suggested_qty is None and line_c.adjusted_suggested_qty is None
    assert replenishment_db.scalar(select(func.count(PurchaseOrder.id))) == 0
    assert replenishment_db.scalar(select(func.count(PurchaseOrderLine.id))) == 0
    assert (
        replenishment_db.scalar(select(func.count(PurchaseOrderStoreAllocation.id)))
        == 0
    )


def test_target_weeks_rejects_manipulated_quantity_without_trusted_inventory(
    replenishment_db,
):
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
        store_ids=[1],
        as_of=date(2026, 8, 14),
        now=NOW,
    )
    row_c = next(line for line in report.rows if line.variation_id == "C")
    with pytest.raises(ValueError, match="trusted current inventory is unavailable"):
        build_replenishment_preview(
            report,
            vendor_id=1,
            mode="target_weeks",
            target_weeks=4,
            final_quantities={row_c.key: 10},
        )
    assert replenishment_db.scalar(select(func.count(PurchaseOrder.id))) == 0


def test_selected_period_and_trailing_velocity_are_independent_and_pack_rounds_up(
    replenishment_db,
):
    replenishment_db.add(
        ConsignmentSaleFact(
            id=20,
            square_order_id="O-20",
            square_line_item_uid="L-20",
            square_variation_id="A",
            square_product_id="I-A",
            square_location_id="N",
            store_id=1,
            business_date=date(2026, 6, 15),
            transacted_at=datetime(2026, 6, 15, 12, tzinfo=timezone.utc),
            quantity_sold=Decimal(13),
            gross_sales_amount=Decimal(130),
            discount_amount=Decimal(0),
            tax_amount=Decimal(0),
            net_sales_amount=Decimal(130),
            currency="USD",
            product_name_snapshot="Product A",
            variation_name_snapshot="Default",
            sku_snapshot="SKU-A",
            vendor_id_snapshot=1,
            vendor_name_snapshot="Primary Vendor",
            extended_cogs_snapshot=Decimal("32.50"),
            attribution_status="ATTRIBUTED",
            attribution_source="SYNC",
            source_synchronized_at=NOW,
        )
    )
    replenishment_db.commit()
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 30),
        store_ids=[1],
        exclude_no_recent_sales=False,
        as_of=date(2026, 8, 14),
        now=NOW,
    )
    row_a = next(
        row for row in report.rows if row.vendor_id == 1 and row.variation_id == "A"
    )
    assert row_a.selected_units_sold == Decimal(13)
    assert row_a.recent_units_sold == Decimal(5)
    assert row_a.average_weekly_sales == Decimal("1.25")

    replacement = build_replenishment_preview(report, vendor_id=1, mode="replace_sales")
    replacement_a = next(
        line for line in replacement.lines if line.row.variation_id == "A"
    )
    assert replacement_a.raw_suggested_qty == Decimal(13)
    assert replacement_a.adjusted_suggested_qty == 18
    assert replacement_a.adjusted_suggested_qty >= replacement_a.raw_suggested_qty
    assert replacement_a.adjustment_note == "case pack 6"

    target = build_replenishment_preview(
        report, vendor_id=1, mode="target_weeks", target_weeks=4
    )
    target_a = next(line for line in target.lines if line.row.variation_id == "A")
    assert target_a.raw_suggested_qty == Decimal(3)


def test_non_primary_vendor_is_warned_but_allowed_and_vendor_isolated(replenishment_db):
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
        store_ids=[1],
        as_of=date(2026, 8, 14),
        now=NOW,
    )
    alternate_row = next(
        row for row in report.rows if row.vendor_id == 2 and row.variation_id == "A"
    )
    primary_row = next(
        row for row in report.rows if row.vendor_id == 1 and row.variation_id == "A"
    )
    assert not alternate_row.is_primary_vendor
    preview = build_replenishment_preview(
        report,
        vendor_id=2,
        mode="replace_sales",
        final_quantities={alternate_row.key: 7, primary_row.key: 999},
    )
    assert all(line.row.vendor_id == 2 for line in preview.lines)
    assert all(line.final_qty != 999 for line in preview.lines)
    order, created = create_replenishment_purchase_order(
        replenishment_db,
        preview=preview,
        created_by_principal_id=1,
        idempotency_key="replenishment_alternate_vendor_01",
        selected_store_ids=[1],
    )
    assert created and order.vendor_id == 2
    line = replenishment_db.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
    )
    assert line.sku == "SKU-A" and line.ordered_qty == 7


def test_lifecycle_is_revalidated_before_any_order_is_created(replenishment_db):
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
        store_ids=[1],
        as_of=date(2026, 8, 14),
        now=NOW,
    )
    preview = build_replenishment_preview(report, vendor_id=1, mode="replace_sales")
    replenishment_db.add(
        OrderingProductLifecycle(
            square_variation_id="A",
            status="NO_FUTURE_REORDER",
            no_future_reorder_at=NOW,
            no_future_reorder_by_principal_id=1,
        )
    )
    replenishment_db.commit()
    with pytest.raises(ValueError, match="no longer eligible"):
        create_replenishment_purchase_order(
            replenishment_db,
            preview=preview,
            created_by_principal_id=1,
            idempotency_key="replenishment_lifecycle_guard_01",
            selected_store_ids=[1],
        )
    assert replenishment_db.scalar(select(func.count(PurchaseOrder.id))) == 0


def test_finalization_creates_standard_draft_order_lines_and_allocations(
    replenishment_db,
):
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
        store_ids=[1],
        as_of=date(2026, 8, 14),
        now=NOW,
    )
    row_a = next(
        row for row in report.rows if row.vendor_id == 1 and row.variation_id == "A"
    )
    row_c = next(
        row for row in report.rows if row.vendor_id == 1 and row.variation_id == "C"
    )
    preview = build_replenishment_preview(
        report,
        vendor_id=1,
        mode="replace_sales",
        final_quantities={row_a.key: 18, row_c.key: 0},
    )
    order, created = create_replenishment_purchase_order(
        replenishment_db,
        preview=preview,
        created_by_principal_id=1,
        idempotency_key="replenishment_test_finalize_0001",
        selected_store_ids=[1],
    )
    assert created
    assert order.status.value == "DRAFT"
    lines = replenishment_db.scalars(
        select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
    ).all()
    assert (
        len(lines) == 1 and lines[0].ordered_qty == 18 and lines[0].suggested_qty == 12
    )
    assert lines[0].sku == "SKU-A" and lines[0].unit_cost == Decimal("2.5000")
    allocation = replenishment_db.scalar(
        select(PurchaseOrderStoreAllocation).where(
            PurchaseOrderStoreAllocation.purchase_order_line_id == lines[0].id
        )
    )
    assert allocation.store_id == 1 and allocation.allocated_qty == 18

    duplicate, duplicate_created = create_replenishment_purchase_order(
        replenishment_db,
        preview=preview,
        created_by_principal_id=1,
        idempotency_key="replenishment_test_finalize_0001",
        selected_store_ids=[1],
    )
    assert duplicate.id == order.id and not duplicate_created
    assert replenishment_db.scalar(select(func.count(PurchaseOrder.id))) == 1


def test_repeated_finalize_returns_existing_order_before_recomputing_preview(
    replenishment_db,
):
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 14),
        store_ids=[1], as_of=date(2026, 8, 14), now=NOW,
    )
    row_a = next(
        row for row in report.rows if row.vendor_id == 1 and row.variation_id == "A"
    )
    preview = build_replenishment_preview(
        report, vendor_id=1, mode="replace_sales",
        final_quantities={
            row.key: (5 if row.key == row_a.key else 0)
            for row in report.rows if row.vendor_id == 1
        },
    )
    order, _created = create_replenishment_purchase_order(
        replenishment_db, preview=preview, created_by_principal_id=1,
        idempotency_key="replenishment_repeat_route_0001", selected_store_ids=[1],
    )
    replenishment_db.commit()
    replenishment_db.add(OrderingProductLifecycle(
        square_variation_id="A", status="NO_FUTURE_REORDER",
        no_future_reorder_at=NOW, no_future_reorder_by_principal_id=1,
    ))
    replenishment_db.commit()

    body = urlencode({
        "po_finalize_key": "replenishment_repeat_route_0001",
        "po_vendor_id": "1",
    }).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/v2/reports/replenishment/finalize",
        "raw_path": b"/v2/reports/replenishment/finalize", "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "scheme": "https", "server": ("test", 443), "client": ("127.0.0.1", 1),
        "app": app,
    }, receive)
    principal = AuthPrincipal(
        id=1, username="owner", role=Role.ADMIN, store_id=None, active=True
    )
    response = asyncio.run(replenishment_finalize_route(
        request, principal=principal, _csrf=None, db=replenishment_db,
    ))
    assert response.status_code == 303
    assert response.headers["location"].endswith(
        f"/orders/{order.id}?created_from=replenishment&duplicate=1"
    )
    assert replenishment_db.scalar(select(func.count(PurchaseOrder.id))) == 1


def test_finalized_cost_uses_manual_ordering_vendor_before_catalog_precedence(
    monkeypatch, replenishment_db,
):
    monkeypatch.setattr(
        "app.services.v2_replenishment_report_service.fetch_catalog_by_sku",
        lambda: {
            "SKU-A": SimpleNamespace(
                unit_cost=Decimal("3.75"), unit_price=Decimal("9.50"),
                gtin="CATALOG-GTIN", item_name="Catalog Product A",
                variation_name="Catalog Default",
            )
        },
    )
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 14),
        store_ids=[1], as_of=date(2026, 8, 14), now=NOW,
    )
    row_a = next(
        row for row in report.rows if row.vendor_id == 1 and row.variation_id == "A"
    )
    assert row_a.unit_cost == Decimal("2.50")
    preview = build_replenishment_preview(
        report,
        vendor_id=1,
        mode="replace_sales",
        final_quantities={
            row.key: (5 if row.variation_id == "A" else 0)
            for row in report.rows if row.vendor_id == 1
        },
    )
    order, created = create_replenishment_purchase_order(
        replenishment_db, preview=preview, created_by_principal_id=1,
        idempotency_key="replenishment_catalog_cost_0001", selected_store_ids=[1],
    )
    assert created
    line = replenishment_db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == order.id,
        PurchaseOrderLine.sku == "SKU-A",
    ))
    assert line.unit_cost == Decimal("2.5000")
    assert line.unit_price == Decimal("9.50")
    assert line.gtin == "CATALOG-GTIN"


def test_replenishment_result_and_preview_render_in_reports_v2(replenishment_db):
    report = run_replenishment_report(
        replenishment_db,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
        store_ids=[1],
        as_of=date(2026, 8, 14),
        now=NOW,
    )
    preview = build_replenishment_preview(report, vendor_id=1, mode="replace_sales")
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v2/reports",
        "raw_path": b"/v2/reports",
        "query_string": b"",
        "headers": [],
        "scheme": "https",
        "server": ("test", 443),
        "client": ("127.0.0.1", 1),
        "app": app,
    }
    request = Request(scope)
    principal = AuthPrincipal(
        id=1, username="owner", role=Role.ADMIN, store_id=None, active=True
    )
    request.state.principal = principal
    request.state.permission_flags = {
        "management.access": True,
        "nav.reports.all": True,
        "reports.workbench.view": True,
    }
    request.state.csrf_token = "test-token"
    config = {
        "report_type": "replenishment",
        "date_mode": "custom",
        "start_date": "2026-08-01",
        "end_date": "2026-08-14",
        "store_ids": [1],
        "exclude_over_four_weeks": True,
        "exclude_no_recent_sales": True,
        "po_vendor_id": "1",
        "po_mode": "replace_sales",
        "target_weeks": "4",
    }
    context = _context(
        request, principal, replenishment_db, config=config, result=report
    )
    context["po_preview"] = preview
    context["po_finalize_key"] = "replenishment_test_finalize_0002"
    response = app.state.templates.TemplateResponse(
        "v2/reporting/workbench.html", context
    )
    html = bytes(response.body).decode()
    assert "Generate Potential PO" in html
    assert "Temporary PO preview" in html
    assert "Finalize PO" in html
    assert "data-sortable-table" in html
    assert 'name="po_finalize_key" value="replenishment_test_finalize_0002"' in html
