"""Product-definition regressions for newly calculated Consignment reports."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from test_v2_funding_reports import (
    _assign_order,
    _configure_vendor_product,
    _legacy_report,
    _return,
    _sale,
)
from test_v2_funding_reports import (
    db as db,  # noqa: PLC0414 -- explicitly re-export the pytest fixture
)

from app.models import (
    Base,
    FundingReportFactLink,
    FundingReportLine,
    FundingSkuMapping,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    VendorSkuConfig,
)
from app.services.consignment_product_report import SEMANTICS, number
from app.services.v2_funding_reports_service import (
    calculate_report,
    finalize_report,
    funding_report_fifo_exceptions,
    normalize_draft_funding_allocation,
    report_position,
    resolve_funding_report_fifo_exception,
)


def calculate(db, *, stores=None, product="", acknowledged=False):
    return calculate_report(
        db,
        account_id=1,
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 2),
        store_ids=stores or [],
        sku_filter=product,
        internal_note="",
        actor_id=6,
        overlap_acknowledged=acknowledged,
    )


def lines(db, report):
    return list(
        db.scalars(
            select(FundingReportLine).where(FundingReportLine.report_id == report.id)
        ).all()
    )


def test_owned_five_sales_need_no_po(db):
    _configure_vendor_product(db)
    _sale(db, quantity="5")
    report = calculate(db)
    assert report.units_sold == 5
    assert report.calculated_cogs == 20
    finalize_report(db, report_id=report.id, actor_id=6)
    assert report.status == "FINALIZED"


def test_reds_with_legacy_po_and_account_mapping_is_entirely_outside_vendor(db):
    _configure_vendor_product(db)
    _configure_vendor_product(db, account_id=3, sku="REDS", variation_id="VAR-REDS")
    _assign_order(db, account_id=1, sku="REDS", ordered_qty=99)
    db.add(
        FundingSkuMapping(
            account_id=1,
            normalized_sku="REDS",
            sku_snapshot="REDS",
            square_variation_id="VAR-REDS",
            product_name_snapshot="Reds",
            effective_start_date=date(2026, 1, 1),
            unit_cost=9,
            status="ACTIVE",
            reason="Historical assignment",
            created_by_principal_id=6,
        )
    )
    owned = _sale(db, quantity="5")
    reds = _sale(db, fact_id=2, quantity="75", sku="REDS", variation_id="VAR-REDS")
    report = calculate(db)
    assert {line.square_variation_id for line in lines(db, report)} == {"VAR-EXACT"}
    assert report.units_sold == 5 and report.calculated_cogs == 20
    assert funding_report_fifo_exceptions(db, report_id=report.id) == []
    links = db.scalars(
        select(FundingReportFactLink).where(
            FundingReportFactLink.report_id == report.id
        )
    ).all()
    assert [link.sale_fact_id for link in links] == [owned.id]
    assert reds.id not in [link.sale_fact_id for link in links]
    finalize_report(db, report_id=report.id, actor_id=6)


def test_75_sales_against_one_funded_unit_finalize(db):
    _configure_vendor_product(db)
    _assign_order(db, account_id=1, ordered_qty=1)
    _sale(db, quantity="75")
    report = calculate(db)
    assert (report.units_sold, report.calculated_cogs) == (75, 300)
    assert not funding_report_fifo_exceptions(db, report_id=report.id)
    finalize_report(db, report_id=report.id, actor_id=6)


def test_return_needs_no_prior_allocation_and_preserves_negative_net(db):
    _configure_vendor_product(db)
    old = _sale(db, day=date(2026, 6, 1), quantity="1")
    ret = _return(db, old, quantity="4")
    ret.original_sale_fact_id = None
    report = calculate(db)
    assert (
        report.units_sold,
        report.units_returned,
        report.net_units,
        report.calculated_cogs,
    ) == (0, 4, -4, -16)
    finalize_report(db, report_id=report.id, actor_id=6)
    assert report.finalized_snapshot["calculated_cogs"] == "-16.00"


@pytest.mark.parametrize("quantity", ["-3", "0", "5"])
def test_signed_sale_quantities_are_not_integrity_failures(db, quantity):
    _configure_vendor_product(db)
    _sale(db, quantity=quantity)
    report = calculate(db)
    assert report.net_units == Decimal(quantity)
    finalize_report(db, report_id=report.id, actor_id=6)


@pytest.mark.parametrize("state", ["negative", "missing", "stale", "critical"])
def test_inventory_is_informational_and_unknown_is_not_zero(db, state):
    _configure_vendor_product(db)
    _sale(db, quantity="5")
    observed = db.get(OrderingCurrentInventory, ("VAR-EXACT", 1))
    if state == "negative":
        observed.counted_quantity = -3
    elif state == "missing":
        db.delete(observed)
    else:
        observed.refreshed_at = datetime.now(timezone.utc) - timedelta(
            days=4 if state == "critical" else 2
        )
    db.flush()
    report = calculate(db, stores=[1])
    assert report.inventory_units_snapshot == (-3 if state == "negative" else None)
    assert lines(db, report)[0].inventory_units_snapshot == (
        -3 if state == "negative" else None
    )
    assert report.units_sold == 5
    finalize_report(db, report_id=report.id, actor_id=6)


def test_explicit_unrelated_variation_never_falls_back_to_owned_sku(db):
    _configure_vendor_product(db)
    _sale(db, variation_id="VAR-OTHER", sku="AB12", quantity="77")
    assert calculate(db).units_sold == 0


def test_missing_variation_uses_globally_unique_sku(db):
    _configure_vendor_product(db)
    _sale(db, variation_id=None, sku=" ab 12 ", quantity="5")
    assert calculate(db).units_sold == 5


def test_ambiguous_sku_on_identityless_vendor_candidate_blocks_without_guessing(db):
    _configure_vendor_product(db)
    db.add(
        OrderingCatalogIdentity(
            square_variation_id="OTHER",
            sku="AB12",
            item_name="Other",
            square_is_deleted=False,
            last_seen_at=datetime.now(timezone.utc),
        )
    )
    _sale(db, variation_id=None, sku="AB12")
    with pytest.raises(ValueError, match="ambiguous SKU AB12"):
        calculate(db)


def test_selected_vendor_default_ambiguity_blocks(db):
    _configure_vendor_product(db)
    db.add(
        VendorSkuConfig(
            vendor_id=11,
            sku="AB12",
            square_variation_id="VAR-EXACT",
            unit_cost=8,
            active=True,
            is_default_vendor=True,
        )
    )
    db.flush()
    with pytest.raises(ValueError, match="ownership is ambiguous.*VAR-EXACT"):
        calculate(db)


def test_unrelated_catalog_ambiguity_does_not_block(db):
    _configure_vendor_product(db)
    for variation in ["UNRELATED-1", "UNRELATED-2"]:
        db.add(
            OrderingCatalogIdentity(
                square_variation_id=variation,
                sku="DUPLICATE",
                item_name="Other",
                square_is_deleted=False,
                last_seen_at=datetime.now(timezone.utc),
            )
        )
    db.add(
        VendorSkuConfig(
            vendor_id=11,
            sku="DUPLICATE",
            unit_cost=None,
            active=True,
            is_default_vendor=True,
        )
    )
    _sale(db, quantity="5")
    assert calculate(db).units_sold == 5


def test_unresolved_selected_product_does_not_silently_disappear(db):
    _configure_vendor_product(db)
    db.add(
        VendorSkuConfig(
            vendor_id=10, sku="BROKEN", unit_cost=4, active=True, is_default_vendor=True
        )
    )
    db.flush()
    with pytest.raises(ValueError, match="Square Variation ID missing; SKU has no unique catalog match"):
        calculate(db)


@pytest.mark.parametrize("raw", [None, "NaN", "Infinity", "-Infinity", "not-a-number"])
def test_invalid_quantity_is_distinct_from_signed_quantity(raw):
    assert number(raw) is None
    assert number("-3") == -3


@pytest.mark.parametrize("raw", [None, Decimal("NaN"), Decimal("Infinity")])
def test_matched_return_missing_quantity_is_scoped_error(db, raw):
    _configure_vendor_product(db)
    sale = _sale(db)
    ret = _return(db, sale)
    ret.quantity_returned = raw
    db.flush()
    db.expire_all()
    with pytest.raises(ValueError, match=f"vendor return fact {ret.id}.*quantity"):
        calculate(db)


def test_broken_unrelated_return_is_ignored(db):
    _configure_vendor_product(db)
    sale = _sale(db, variation_id="OUTSIDE", sku="OUTSIDE")
    ret = _return(db, sale, sku="OUTSIDE")
    ret.quantity_returned = None
    db.flush()
    assert calculate(db).units_returned == 0


def test_unknown_cost_persists_null_and_financial_finalization_is_blocked(db):
    _configure_vendor_product(db, cost=None)
    _sale(db, quantity="5")
    report = calculate(db)
    db.flush()
    db.expire_all()
    assert report.units_sold == 5 and report.calculated_cogs is None
    assert all(line.unit_cost_snapshot is None for line in lines(db, report))
    active = [line for line in lines(db, report) if line.net_units]
    assert active[0].extended_cogs is None
    assert report_position(db, report_id=report.id)["adjusted_amount"] is None
    assert report_position(db, report_id=report.id)["remaining_amount"] is None
    link = db.scalar(
        select(FundingReportFactLink).where(
            FundingReportFactLink.report_id == report.id
        )
    )
    assert link.cogs_amount_snapshot is None
    with pytest.raises(ValueError, match="cost is UNKNOWN"):
        finalize_report(db, report_id=report.id, actor_id=6)


def test_filters_cover_activity_zero_rows_and_inventory(db):
    _configure_vendor_product(db)
    _configure_vendor_product(db, sku="AB123", variation_id="VAR-PARTIAL")
    _configure_vendor_product(db, sku="ZERO", variation_id="VAR-ZERO")
    _sale(db, fact_id=1, quantity="5", store_id=1)
    _sale(db, fact_id=2, quantity="99", store_id=2)
    excluded = _sale(
        db, fact_id=3, sku="AB123", variation_id="VAR-PARTIAL", quantity="88"
    )
    _return(db, excluded, sku="AB123")
    report = calculate(db, stores=[1], product="Exact Product")
    assert report.units_sold == 5 and report.units_returned == 0
    assert {
        (line.square_variation_id, line.store_id) for line in lines(db, report)
    } == {("VAR-EXACT", 1)}
    assert report.inventory_units_snapshot == 5
    zero = calculate(db, stores=[2], product="ZERO", acknowledged=True)
    assert zero.units_sold == 0
    assert {(line.square_variation_id, line.store_id) for line in lines(db, zero)} == {
        ("VAR-ZERO", 2)
    }
    assert zero.inventory_units_snapshot is None


def test_finalized_cost_and_source_records_remain_unchanged(db, monkeypatch):
    config = _configure_vendor_product(db)
    _order, po_line = _assign_order(db, account_id=1)
    _sale(db, quantity="5")

    def no_http(*args, **kwargs):
        pytest.fail("Report calculation must not call Square or any HTTP service")

    monkeypatch.setattr("urllib.request.urlopen", no_http)
    monkeypatch.setattr("app.services.square_ordering_data_service.urlopen", no_http)
    monkeypatch.setattr("app.services.square_vendor_service.urlopen", no_http)
    monkeypatch.setattr("app.services.square_snapshot_provider.urlopen", no_http)
    monkeypatch.setattr("httpx.Client.request", no_http)
    writes = []

    def capture(_conn, _cursor, statement, _params, _ctx, _many):
        if statement.lstrip().upper().startswith(("UPDATE ", "DELETE ", "INSERT ")):
            writes.append(statement.lower())

    event.listen(db.bind, "before_cursor_execute", capture)
    report = calculate(db)
    finalize_report(db, report_id=report.id, actor_id=6)
    db.flush()
    for table in [
        "vendor_sku_configs",
        "purchase_order_lines",
        "ordering_current_inventory",
        "consignment_sale_facts",
        "consignment_return_facts",
    ]:
        assert not any(table in statement for statement in writes)
    snapshot = dict(report.finalized_snapshot)
    original_cost = lines(db, report)[0].unit_cost_snapshot
    config.unit_cost = 90
    db.flush()
    assert normalize_draft_funding_allocation(db, report=report, actor_id=6) is False
    assert (
        report.finalized_snapshot == snapshot
        and lines(db, report)[0].unit_cost_snapshot == original_cost
    )
    assert po_line.unit_cost == 4


@pytest.mark.parametrize("decision", ["PENDING", "IGNORE", "INCLUDE"])
def test_old_draft_decisions_survive_fresh_calculation(db, decision):
    _assign_order(db, account_id=1, ordered_qty=1)
    _sale(db, quantity="5")
    old = _legacy_report(db)
    exception = funding_report_fifo_exceptions(db, report_id=old.id)[0]
    if decision != "PENDING":
        resolve_funding_report_fifo_exception(
            db,
            report_id=old.id,
            exception_id=exception.id,
            action=decision,
            reason="Historical decision",
            actor_id=6,
            unit_cost=4,
        )
    db.flush()
    old_lines = [(line.id, line.extended_cogs) for line in lines(db, old)]
    old_state = (
        exception.id,
        exception.status,
        exception.cost_basis,
        exception.resolution_reason,
    )
    _configure_vendor_product(db)
    fresh = calculate(db, acknowledged=True)
    assert fresh.id != old.id and fresh.units_sold == 5
    assert (
        fresh.warning_summary["purchase_order_scope"]["allocation_semantics"]
        == SEMANTICS
    )
    assert normalize_draft_funding_allocation(db, report=old, actor_id=6) is False
    assert old_lines == [(line.id, line.extended_cogs) for line in lines(db, old)]
    assert old_state == (
        exception.id,
        exception.status,
        exception.cost_basis,
        exception.resolution_reason,
    )
    finalize_report(db, report_id=fresh.id, actor_id=6)


def test_get_consignment_report_is_read_only_and_unknown_reaches_view(db, monkeypatch):
    from app.routers import v2_funding_reports as router

    columns = ", ".join(
        f'"{column.name}" TEXT' for column in Base.metadata.tables["principals"].columns
    )
    db.connection().exec_driver_sql("CREATE TABLE principals (" + columns + ")")
    _configure_vendor_product(db, cost=None)
    _sale(db, quantity="5")
    report = calculate(db)
    db.flush()
    monkeypatch.setattr(
        router, "_funding_context", lambda request, principal, **values: values
    )
    templates = SimpleNamespace(TemplateResponse=lambda name, context: context)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(templates=templates))
    )
    statements = []
    event.listen(
        db.bind,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, many: statements.append(
            statement
        ),
    )
    view = router.funding_report_detail_page(
        1, report.id, request, _feature=None, principal=SimpleNamespace(id=6), db=db
    )
    assert view["position"]["adjusted_amount"] is None
    assert view["report"].calculated_cogs is None
    from jinja2 import Environment, FileSystemLoader

    request.state = SimpleNamespace()
    view.update(
        request=request,
        principal=SimpleNamespace(
            username="Owner", role=SimpleNamespace(value="ADMIN")
        ),
        page=SimpleNamespace(label="Consignment report", description=""),
        status_label=lambda value: value,
        business_datetime=lambda value: str(value),
        csrf_token=lambda request: "test-only",
        payment_tabs=[],
    )
    html = (
        Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
        .get_template("v2/order_payments/funding_report_detail.html")
        .render(**view)
    )
    assert "Configured vendor products" in html
    assert "Configured cost is UNKNOWN" in html
    assert "Funded quantity needs an owner decision" not in html
    assert "Observed inventory" in html and "UNKNOWN" in html
    assert not any(
        s.lstrip().upper().startswith(("UPDATE ", "INSERT ", "DELETE "))
        for s in statements
    )


def test_unknown_currency_formatter_does_not_render_zero():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/templates"))
    macro = env.get_template("v2/order_payments/_shared.html").module.money
    assert macro(None) == "UNKNOWN"
    assert macro(Decimal(-3)) == "$-3.00"
    assert macro(Decimal(0)) == "$0.00"


def test_unknown_cost_remains_unknown_in_combined_view_and_deletion_audit(
    db, monkeypatch
):
    from app.services import v2_funding_reports_service as service

    _configure_vendor_product(db, cost=None)
    _sale(db, quantity="5")
    parent = service.calculate_combined_report(
        db,
        account_id=1,
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 2),
        store_ids=[],
        sku_filter="",
        internal_note="",
        actor_id=6,
    )
    assert parent.calculated_cogs is None
    assert (
        service.report_position_for_display(db, report_id=parent.id)["adjusted_amount"]
        is None
    )
    captures = []
    monkeypatch.setattr(
        service, "_audit", lambda *args, **kwargs: captures.append(kwargs)
    )
    # Preserve a realistic draft deletion audit without manufacturing a value.
    service.delete_report(
        db,
        account_id=1,
        report_id=parent.id,
        expected_token=service._report_version_token(parent),
        actor_id=6,
        reason="Discard unknown draft",
    )
    assert captures
    snapshots = [
        row["after"] for row in captures if "calculated_cogs" in row.get("after", {})
    ]
    assert snapshots and all(row["calculated_cogs"] is None for row in snapshots)


def test_legacy_consignment_get_preserves_lines_and_pending_decisions(db, monkeypatch):
    from app.routers import v2_funding_reports as router

    columns = ", ".join(
        f'"{column.name}" TEXT' for column in Base.metadata.tables["principals"].columns
    )
    db.connection().exec_driver_sql("CREATE TABLE principals (" + columns + ")")
    _assign_order(db, account_id=1, ordered_qty=1)
    _sale(db, quantity="5")
    old = _legacy_report(db)
    before_lines = [(line.id, line.extended_cogs) for line in lines(db, old)]
    before_exceptions = [
        (row.id, row.status, row.cost_basis)
        for row in funding_report_fifo_exceptions(db, report_id=old.id)
    ]
    monkeypatch.setattr(
        router, "_funding_context", lambda request, principal, **values: values
    )
    templates = SimpleNamespace(TemplateResponse=lambda name, context: context)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(templates=templates))
    )
    statements = []
    event.listen(
        db.bind,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, many: statements.append(
            statement
        ),
    )
    view = router.funding_report_detail_page(
        1, old.id, request, _feature=None, principal=SimpleNamespace(id=6), db=db
    )
    assert [
        (row.id, row.status, row.cost_basis) for row in view["fifo_exceptions"]
    ] == before_exceptions
    assert [(line.id, line.extended_cogs) for line in view["lines"]] == before_lines
    assert not any(
        s.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))
        for s in statements
    )


@pytest.mark.parametrize("change", ["cost", "owner"])
def test_relevant_configuration_changed_after_calculation_requires_fresh_report(
    db, change
):
    mapping = _configure_vendor_product(db)
    _sale(db, quantity="5")
    report = calculate(db)
    if change == "cost":
        mapping.unit_cost = 9
    else:
        mapping.vendor_id = 11
    db.flush()
    with pytest.raises(ValueError, match="changed after calculation"):
        finalize_report(db, report_id=report.id, actor_id=6)
    assert report.calculated_cogs == 20
    assert report.status == "DRAFT"


def test_unknown_inventory_refresh_does_not_invalidate_known_sales(db):
    _configure_vendor_product(db)
    _sale(db, quantity="5")
    report = calculate(db)
    observed = db.get(OrderingCurrentInventory, ("VAR-EXACT", 1))
    observed.counted_quantity = -99
    db.flush()
    finalize_report(db, report_id=report.id, actor_id=6)
    assert report.units_sold == 5
    assert report.finalized_snapshot["inventory_units"] is None


# Exact identities observed by the read-only BIG Wholesale diagnostic. Quantities
# and dates below are synthetic; no production financial data is reproduced.
BIG_POUCHES = [
    ("810096912435", "734PMHBXAGAQEPM2QZK73G4O", "Watermelon Strawberry Mint 6mg"),
    ("810096912442", "TFLNVBYOGRJ3WQNP6A6WSEJD", "Mango Strawberry Mint 6mg"),
    ("810096912428", "DYF5OWZXV62R5JSRWUDNHPKY", "Raspberry Lemonade Mint 6mg"),
    ("810096912411", "ETED34NA67VS2Q2BLMI6Z2KR", "Peach Pineapple Mint 6mg"),
    ("810096912404", "I6F37X2563UF27SB2M2XIH7S", "Blueberry Lemon Mint 6mg"),
]


def test_big_wholesale_five_missing_catalog_products_create_combined_report(db):
    from app.models import Vendor
    from app.services.v2_funding_reports_service import calculate_combined_report, combined_report_members

    db.get(Vendor, 10).name = "BIG Wholesale"
    expected_sales = set()
    for index, (sku, variation, label) in enumerate(BIG_POUCHES, 1):
        db.add(VendorSkuConfig(vendor_id=10, sku=sku, square_variation_id=variation,
                              unit_cost=4, active=True, is_default_vendor=True))
        _, po_line = _assign_order(db, sku=sku)
        po_line.variation_id = variation
        po_line.item_name = "Juice Head Pouches"
        po_line.variation_name = label
        if sku != "810096912428":
            sale = _sale(db, fact_id=index, sku=None, variation_id=variation,
                         product="Juice Head Pouches", quantity="2")
            sale.variation_name_snapshot = label
            expected_sales.add(sale.id)
    db.flush()
    assert all(db.get(OrderingCatalogIdentity, v) is None for _, v, _ in BIG_POUCHES)
    combined = calculate_combined_report(
        db, account_id=1, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter="", internal_note="", actor_id=6,
    )
    member, = combined_report_members(db, report=combined)
    result = lines(db, member)
    assert {r.square_variation_id for r in result} == {v for _, v, _ in BIG_POUCHES}
    assert {r.variation_name_snapshot for r in result} == {name for _, _, name in BIG_POUCHES}
    assert {r.product_name_snapshot for r in result} == {"Juice Head Pouches"}
    assert member.units_sold == combined.units_sold == 8
    assert member.calculated_cogs == 32
    assert {r.sale_fact_id for r in db.scalars(select(FundingReportFactLink))} == expected_sales
    assert all(r.units_sold == 0 for r in result if r.sku_snapshot == "810096912428")
    assert all(db.get(OrderingCatalogIdentity, v) is None for _, v, _ in BIG_POUCHES)


@pytest.mark.parametrize("catalog_state", ["absent", "stale", "deleted"])
def test_explicit_identity_needs_no_current_catalog_confirmation(db, catalog_state):
    _configure_vendor_product(db)
    identity = db.get(OrderingCatalogIdentity, "VAR-EXACT")
    if catalog_state == "absent":
        db.delete(identity)
    else:
        identity.last_seen_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        identity.square_is_deleted = catalog_state == "deleted"
    sale = _sale(db, sku=None, variation_id="VAR-EXACT", quantity="5")
    _return(db, sale, quantity="2")
    # Same SKU/name with an explicit different variation must remain excluded.
    _sale(db, fact_id=2, sku="AB12", variation_id="OTHER", quantity="99")
    db.flush()
    report = calculate(db)
    assert (report.units_sold, report.units_returned, report.net_units) == (5, 2, 3)
    assert report.calculated_cogs == 12
    finalize_report(db, report_id=report.id, actor_id=6)
    assert report.status == "FINALIZED"


def test_missing_catalog_and_all_labels_with_no_sales_is_valid(db):
    db.add(VendorSkuConfig(vendor_id=10, sku="NO-LABEL", square_variation_id="KNOWN-ID",
                          unit_cost=4, active=True, is_default_vendor=True))
    db.flush()
    report = calculate(db)
    assert report.units_sold == 0
    assert {r.product_name_snapshot for r in lines(db, report)} == {"Product name unavailable"}
    assert {r.sku_snapshot for r in lines(db, report)} == {"NO-LABEL"}


def test_snapshot_names_filter_products_but_never_establish_identity(db):
    mapping = _configure_vendor_product(db)
    db.delete(db.get(OrderingCatalogIdentity, "VAR-EXACT"))
    _sale(db, variation_id="VAR-EXACT", product="Stored pouch name", quantity="5")
    _sale(db, fact_id=2, variation_id="OTHER", product="Stored pouch name", quantity="99")
    db.flush()
    assert calculate(db, product="Stored pouch name").units_sold == 5
    mapping.square_variation_id = None
    db.flush()
    with pytest.raises(ValueError) as exc:
        calculate(db, acknowledged=True)
    message = str(exc.value)
    assert "Square Variation ID missing" in message
    assert "SKU AB12" in message
    assert "Inventory → Vendor SKU Mappings" in message


def test_missing_configured_identity_with_duplicate_sku_explains_candidates(db):
    mapping = _configure_vendor_product(db)
    mapping.square_variation_id = None
    db.add(OrderingCatalogIdentity(square_variation_id="SECOND", sku="AB12",
                                  item_name="Other pouch", variation_name="Mint",
                                  square_is_deleted=False, last_seen_at=datetime.now(timezone.utc)))
    db.flush()
    with pytest.raises(ValueError) as exc:
        calculate(db)
    message = str(exc.value)
    assert "Other pouch" in message
    assert "SKU AB12" in message
    assert "SKU matches multiple Square variations" in message
    assert "Vendor SKU Mappings" in message


def test_missing_catalog_does_not_weaken_conflicting_default_mapping_guard(db):
    _configure_vendor_product(db)
    db.delete(db.get(OrderingCatalogIdentity, "VAR-EXACT"))
    db.add(VendorSkuConfig(vendor_id=11, sku="OTHER-SKU", square_variation_id="VAR-EXACT",
                          unit_cost=4, active=True, is_default_vendor=True))
    _sale(db, variation_id="VAR-EXACT", product="Named pouch")
    db.flush()
    with pytest.raises(ValueError, match="ownership is ambiguous.*Named pouch"):
        calculate(db)


def test_missing_configured_id_still_resolves_only_unique_catalog_sku(db):
    mapping = _configure_vendor_product(db)
    mapping.square_variation_id = None
    _sale(db, variation_id="VAR-EXACT", quantity="5")
    db.flush()
    assert calculate(db).units_sold == 5
    assert mapping.square_variation_id is None  # Resolution never repairs configuration.


def test_cacheless_product_uses_return_label_when_no_sale_or_po_exists(db):
    mapping = _configure_vendor_product(db)
    db.delete(db.get(OrderingCatalogIdentity, "VAR-EXACT"))
    sale = _sale(db, variation_id="VAR-EXACT")
    ret = _return(db, sale)
    ret.original_sale_fact_id = None
    ret.product_name_snapshot = "Returned pouch"
    db.delete(sale)
    db.flush()
    report = calculate(db, product="Returned pouch")
    assert report.units_returned == 1
    assert {r.product_name_snapshot for r in lines(db, report)} == {"Returned pouch"}
    assert mapping.square_variation_id == "VAR-EXACT"
