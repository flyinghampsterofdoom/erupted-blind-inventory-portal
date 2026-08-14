from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    OrderingProductLifecycle,
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
from app.services.square_ordering_data_service import fetch_catalog_by_sku
from app.services.v2_ordering_inventory_repository import effective_freshness

ZERO = Decimal(0)
REPLENISHMENT_MODES = {"replace_sales", "target_weeks"}
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{24,64}$")


@dataclass(frozen=True)
class ReplenishmentRow:
    key: str
    vendor_id: int
    vendor: str
    is_primary_vendor: bool
    variation_id: str
    product_name: str
    variation_name: str
    sku: str
    category: str
    unit_cost: Decimal | None
    current_on_hand: Decimal | None
    inventory_state: str
    selected_units_sold: Decimal
    recent_units_sold: Decimal
    average_weekly_sales: Decimal
    weeks_of_supply: Decimal | None
    lifecycle: str
    pack_size: int
    min_order_qty: int
    store_sales: tuple[tuple[int, Decimal], ...]
    excluded: bool
    exclusion_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ReplenishmentReport:
    start_date: date
    end_date: date
    rows: tuple[ReplenishmentRow, ...]
    shown_count: int
    excluded_count: int
    warnings: tuple[str, ...]

    @property
    def report_type(self) -> str:
        return "replenishment"


@dataclass(frozen=True)
class ReplenishmentPreviewLine:
    row: ReplenishmentRow
    raw_suggested_qty: Decimal | None
    adjusted_suggested_qty: int | None
    final_qty: int
    adjustment_note: str
    estimated_line_cost: Decimal | None
    projected_weeks_of_supply: Decimal | None


@dataclass(frozen=True)
class ReplenishmentPreview:
    vendor_id: int
    vendor: str
    mode: str
    target_weeks: Decimal | None
    lines: tuple[ReplenishmentPreviewLine, ...]
    estimated_total: Decimal
    missing_cost_count: int


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _ceil(value: Decimal) -> int:
    return max(0, int(value.to_integral_value(rounding=ROUND_CEILING)))


def _positive_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    amount = _decimal(value)
    return amount if amount > 0 else None


def _adjust_order_quantity(
    raw: Decimal, *, pack_size: int, min_order_qty: int
) -> tuple[int, str]:
    quantity = _ceil(raw)
    notes: list[str] = []
    if quantity > 0 and min_order_qty > quantity:
        quantity = min_order_qty
        notes.append(f"MOQ {min_order_qty}")
    pack = max(1, pack_size)
    if quantity > 0 and quantity % pack:
        quantity = ((quantity + pack - 1) // pack) * pack
        notes.append(f"case pack {pack}")
    return quantity, ", ".join(notes)


def _sales_by_variation(
    db: Session, *, start_date: date, end_date: date, store_ids: tuple[int, ...]
) -> tuple[dict[str, Decimal], dict[str, dict[int, Decimal]]]:
    query = select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.business_date >= start_date,
        ConsignmentSaleFact.business_date <= end_date,
        ConsignmentSaleFact.square_variation_id.is_not(None),
    )
    if store_ids:
        query = query.where(ConsignmentSaleFact.store_id.in_(store_ids))
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    by_store: dict[str, dict[int, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: ZERO)
    )
    for sale in db.scalars(query).all():
        variation_id = str(sale.square_variation_id or "").strip()
        quantity = _decimal(sale.quantity_sold)
        totals[variation_id] += quantity
        if sale.store_id is not None:
            by_store[variation_id][int(sale.store_id)] += quantity
    return dict(totals), {key: dict(value) for key, value in by_store.items()}


def run_replenishment_report(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    store_ids: Iterable[int] = (),
    exclude_over_four_weeks: bool = True,
    exclude_no_recent_sales: bool = True,
    manual_exclusions: Iterable[str] = (),
    as_of: date | None = None,
    now: datetime | None = None,
) -> ReplenishmentReport:
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")
    selected_stores = tuple(
        sorted({int(value) for value in store_ids if int(value) > 0})
    )
    today = as_of or datetime.now(tz=timezone.utc).date()
    current_time = now or datetime.now(tz=timezone.utc)
    recent_start = today - timedelta(days=27)
    selected_sales, selected_store_sales = _sales_by_variation(
        db, start_date=start_date, end_date=end_date, store_ids=selected_stores
    )
    recent_sales, _ = _sales_by_variation(
        db, start_date=recent_start, end_date=today, store_ids=selected_stores
    )
    active_store_ids = tuple(
        int(value)
        for value in db.scalars(
            select(Store.id).where(Store.active.is_(True)).order_by(Store.id)
        ).all()
    )
    expected_store_ids = set(selected_stores or active_store_ids)

    mappings = db.execute(
        select(VendorSkuConfig, Vendor)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .where(VendorSkuConfig.active.is_(True), Vendor.active.is_(True))
        .order_by(Vendor.name, VendorSkuConfig.sku, VendorSkuConfig.id)
    ).all()
    catalog_by_sku = {}
    if any(_positive_decimal(mapping.unit_cost) is None for mapping, _vendor in mappings):
        try:
            catalog_by_sku = fetch_catalog_by_sku()
        except Exception:  # noqa: BLE001 - match manual Ordering's resilient catalog fallback
            catalog_by_sku = {}
    variation_ids = tuple(
        sorted(
            {
                str(mapping.square_variation_id or "").strip()
                for mapping, _vendor in mappings
                if str(mapping.square_variation_id or "").strip()
            }
        )
    )
    identities = {
        row.square_variation_id: row
        for row in db.scalars(
            select(OrderingCatalogIdentity).where(
                OrderingCatalogIdentity.square_variation_id.in_(variation_ids or ("",))
            )
        ).all()
    }
    lifecycle = {
        row.square_variation_id: str(row.status)
        for row in db.scalars(
            select(OrderingProductLifecycle).where(
                OrderingProductLifecycle.square_variation_id.in_(variation_ids or ("",))
            )
        ).all()
    }
    inventory_query = select(OrderingCurrentInventory).where(
        OrderingCurrentInventory.square_variation_id.in_(variation_ids or ("",))
    )
    if selected_stores:
        inventory_query = inventory_query.where(
            OrderingCurrentInventory.store_id.in_(selected_stores)
        )
    inventory_rows: dict[str, list[OrderingCurrentInventory]] = defaultdict(list)
    for row in db.scalars(inventory_query).all():
        inventory_rows[row.square_variation_id].append(row)

    excluded_keys = {str(value) for value in manual_exclusions}
    output: list[ReplenishmentRow] = []
    unknown_inventory = 0
    for mapping, vendor in mappings:
        variation_id = str(mapping.square_variation_id or "").strip()
        if not variation_id:
            continue
        identity = identities.get(variation_id)
        status = lifecycle.get(variation_id, "ACTIVE")
        observations = inventory_rows.get(variation_id, [])
        states = {
            effective_freshness(row.refreshed_at, now=current_time)
            for row in observations
        }
        observed_store_ids = {int(row.store_id) for row in observations}
        inventory_known = (
            bool(expected_store_ids)
            and observed_store_ids == expected_store_ids
            and states == {"FRESH"}
        )
        on_hand = (
            sum((_decimal(row.counted_quantity) for row in observations), ZERO)
            if inventory_known
            else None
        )
        if not inventory_known:
            unknown_inventory += 1
        recent = recent_sales.get(variation_id, ZERO)
        weekly = recent / Decimal(4)
        wos = on_hand / weekly if on_hand is not None and weekly > 0 else None
        if bool(getattr(identity, "square_is_deleted", False)) and status == "ACTIVE":
            status = "ARCHIVED"
        key = f"{int(vendor.id)}:{variation_id}:{mapping.sku or ''!s}"
        reasons: list[str] = []
        if status in {"ARCHIVED", "NO_FUTURE_REORDER"}:
            reasons.append(status.replace("_", " ").title())
        if exclude_no_recent_sales and recent <= 0:
            reasons.append("No sales in trailing 28 days")
        if exclude_over_four_weeks and wos is not None and wos > Decimal(4):
            reasons.append("More than 4 weeks of supply")
        if key in excluded_keys:
            reasons.append("Manually excluded")
        product_name = str(
            getattr(identity, "item_name", None)
            or getattr(identity, "product_name", None)
            or mapping.sku
        )
        variation_name = str(getattr(identity, "variation_name", None) or "Default")
        catalog_meta = catalog_by_sku.get(str(mapping.sku or '').strip())
        cost = _positive_decimal(mapping.unit_cost) or _positive_decimal(
            getattr(catalog_meta, 'unit_cost', None)
        )
        output.append(
            ReplenishmentRow(
                key=key,
                vendor_id=int(vendor.id),
                vendor=str(vendor.name),
                is_primary_vendor=bool(mapping.is_default_vendor),
                variation_id=variation_id,
                product_name=product_name,
                variation_name=variation_name,
                sku=str(mapping.sku or getattr(identity, "sku", None) or ""),
                category="Uncategorized",
                unit_cost=cost,
                current_on_hand=on_hand,
                inventory_state="FRESH"
                if inventory_known
                else ("MISSING" if not observations else "/".join(sorted(states))),
                selected_units_sold=selected_sales.get(variation_id, ZERO),
                recent_units_sold=recent,
                average_weekly_sales=weekly,
                weeks_of_supply=wos,
                lifecycle=status,
                pack_size=max(1, int(mapping.pack_size or 1)),
                min_order_qty=max(0, int(mapping.min_order_qty or 0)),
                store_sales=tuple(
                    sorted(selected_store_sales.get(variation_id, {}).items())
                ),
                excluded=bool(reasons),
                exclusion_reasons=tuple(reasons),
            )
        )
    output.sort(
        key=lambda row: (
            row.vendor.casefold(),
            row.product_name.casefold(),
            row.variation_name.casefold(),
            row.sku.casefold(),
        )
    )
    warnings: tuple[str, ...] = ()
    if unknown_inventory:
        warnings = (
            f"{unknown_inventory} vendor/product row(s) have missing or stale inventory; WOS and inventory-dependent recommendations are withheld.",
        )
    sync_state = db.get(ConsignmentSalesSyncState, 1)
    synchronized_through = sync_state.last_successful_through_at if sync_state else None
    if synchronized_through is not None and synchronized_through.tzinfo is None:
        synchronized_through = synchronized_through.replace(tzinfo=timezone.utc)
    required_through = datetime.combine(
        max(end_date, today) + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    if (
        sync_state is None
        or sync_state.last_result != "COMPLETE"
        or synchronized_through is None
        or synchronized_through < required_through
    ):
        warnings += (
            "The local sales source is not confirmed complete through both the report and trailing 28-day windows; results may be partial.",
        )
    return ReplenishmentReport(
        start_date=start_date,
        end_date=end_date,
        rows=tuple(output),
        shown_count=sum(not row.excluded for row in output),
        excluded_count=sum(row.excluded for row in output),
        warnings=warnings,
    )


def build_replenishment_preview(
    report: ReplenishmentReport,
    *,
    vendor_id: int,
    mode: str,
    target_weeks: Decimal | int = Decimal(4),
    final_quantities: dict[str, int] | None = None,
    preview_exclusions: Iterable[str] = (),
) -> ReplenishmentPreview:
    if mode not in REPLENISHMENT_MODES:
        raise ValueError("Choose a valid PO calculation mode.")
    target = Decimal(str(target_weeks))
    if mode == "target_weeks" and (target <= 0 or target > 52):
        raise ValueError("Target weeks must be greater than zero and no more than 52.")
    vendor_rows = [row for row in report.rows if row.vendor_id == vendor_id]
    if not vendor_rows:
        raise ValueError("Choose a vendor represented in this report.")
    excluded = {str(value) for value in preview_exclusions}
    quantities = final_quantities or {}
    lines: list[ReplenishmentPreviewLine] = []
    for row in vendor_rows:
        if row.excluded or row.key in excluded:
            continue
        raw: Decimal | None
        if mode == "replace_sales":
            raw = max(ZERO, row.selected_units_sold)
        elif row.current_on_hand is None:
            raw = None
        else:
            raw = max(ZERO, row.average_weekly_sales * target - row.current_on_hand)
        adjusted, note = (
            _adjust_order_quantity(
                raw or ZERO, pack_size=row.pack_size, min_order_qty=row.min_order_qty
            )
            if raw is not None
            else (None, "")
        )
        final_qty = quantities.get(row.key, adjusted or 0)
        if final_qty < 0 or final_qty > 1_000_000:
            raise ValueError("Final order quantities must be between 0 and 1,000,000.")
        if mode == "target_weeks" and raw is None and final_qty > 0:
            raise ValueError(
                f"{row.product_name} cannot be ordered in Target Weeks mode because "
                "trusted current inventory is unavailable."
            )
        line_cost = row.unit_cost * final_qty if row.unit_cost is not None else None
        projected = (
            (row.current_on_hand + Decimal(final_qty)) / row.average_weekly_sales
            if row.current_on_hand is not None and row.average_weekly_sales > 0
            else None
        )
        lines.append(
            ReplenishmentPreviewLine(
                row=row,
                raw_suggested_qty=raw,
                adjusted_suggested_qty=adjusted,
                final_qty=final_qty,
                adjustment_note=note,
                estimated_line_cost=line_cost,
                projected_weeks_of_supply=projected,
            )
        )
    vendor_name = vendor_rows[0].vendor
    return ReplenishmentPreview(
        vendor_id=vendor_id,
        vendor=vendor_name,
        mode=mode,
        target_weeks=target if mode == "target_weeks" else None,
        lines=tuple(lines),
        estimated_total=sum((line.estimated_line_cost or ZERO for line in lines), ZERO),
        missing_cost_count=sum(
            line.final_qty > 0 and line.estimated_line_cost is None for line in lines
        ),
    )


def create_replenishment_purchase_order(
    db: Session,
    *,
    preview: ReplenishmentPreview,
    created_by_principal_id: int,
    idempotency_key: str,
    selected_store_ids: Iterable[int] = (),
) -> tuple[PurchaseOrder, bool]:
    clean_key = str(idempotency_key or "").strip()
    if not IDEMPOTENCY_KEY.fullmatch(clean_key):
        raise ValueError(
            "This PO preview has an invalid or missing finalization token. Generate it again."
        )
    existing_order = db.scalar(
        select(PurchaseOrder).where(PurchaseOrder.creation_idempotency_key == clean_key)
    )
    if existing_order is not None:
        if int(existing_order.vendor_id) != int(preview.vendor_id):
            raise ValueError(
                "This finalization token is already associated with another vendor."
            )
        if int(existing_order.created_by_principal_id) != int(created_by_principal_id):
            raise ValueError(
                "This finalization token is already associated with another owner."
            )
        return existing_order, False
    selected_lines = [line for line in preview.lines if line.final_qty > 0]
    if not selected_lines:
        raise ValueError("Enter a positive final quantity for at least one line.")
    vendor = db.scalar(
        select(Vendor).where(Vendor.id == preview.vendor_id, Vendor.active.is_(True))
    )
    if vendor is None:
        raise ValueError("Vendor is no longer active.")
    active_store_ids = tuple(
        db.scalars(
            select(Store.id)
            .where(Store.active.is_(True))
            .order_by(Store.name, Store.id)
        ).all()
    )
    requested = {int(value) for value in selected_store_ids}
    allocation_stores = tuple(
        store_id
        for store_id in active_store_ids
        if not requested or store_id in requested
    )
    if not allocation_stores:
        raise ValueError("No active stores are available for this purchase order.")
    prepared_lines: list[
        tuple[
            ReplenishmentPreviewLine,
            VendorSkuConfig,
            OrderingCatalogIdentity | None,
            str,
        ]
    ] = []
    used_variation_ids: set[str] = set()
    for preview_line in selected_lines:
        row = preview_line.row
        status = (
            db.scalar(
                select(OrderingProductLifecycle.status).where(
                    OrderingProductLifecycle.square_variation_id == row.variation_id
                )
            )
            or "ACTIVE"
        )
        if str(status) in {"ARCHIVED", "NO_FUTURE_REORDER"}:
            raise ValueError(f"{row.product_name} is no longer eligible for reorder.")
        mapping = db.scalar(
            select(VendorSkuConfig)
            .where(
                VendorSkuConfig.vendor_id == preview.vendor_id,
                VendorSkuConfig.sku == row.sku,
                VendorSkuConfig.square_variation_id == row.variation_id,
                VendorSkuConfig.active.is_(True),
            )
            .order_by(VendorSkuConfig.id)
        )
        if mapping is None:
            raise ValueError(f"{row.sku} is no longer mapped to the selected vendor.")
        identity = db.get(OrderingCatalogIdentity, row.variation_id)
        if bool(getattr(identity, "square_is_deleted", False)):
            raise ValueError(
                f"{row.product_name} is confirmed archived in the catalog."
            )
        line_variation_id = row.variation_id
        if line_variation_id in used_variation_ids:
            line_variation_id = f"SKU::{row.sku}"
        if line_variation_id in used_variation_ids:
            raise ValueError(
                f"{row.sku} duplicates another product identity in this order."
            )
        used_variation_ids.add(line_variation_id)
        prepared_lines.append((preview_line, mapping, identity, line_variation_id))

    try:
        catalog_by_sku = fetch_catalog_by_sku()
    except Exception:  # noqa: BLE001 - match manual Ordering's resilient catalog fallback
        catalog_by_sku = {}

    target_weeks = _ceil(preview.target_weeks or Decimal(1))
    order = PurchaseOrder(
        vendor_id=preview.vendor_id,
        status=PurchaseOrderStatus.DRAFT,
        reorder_weeks=4,
        stock_up_weeks=max(1, target_weeks),
        history_lookback_days=28,
        notes=f"Reports V2 replenishment: {preview.mode.replace('_', ' ')}.",
        creation_idempotency_key=clean_key,
        created_by_principal_id=created_by_principal_id,
    )
    db.add(order)
    db.flush()
    for preview_line, mapping, identity, line_variation_id in prepared_lines:
        row = preview_line.row
        catalog_meta = catalog_by_sku.get(row.sku)
        line = PurchaseOrderLine(
            purchase_order_id=order.id,
            variation_id=line_variation_id,
            sku=row.sku,
            gtin=getattr(catalog_meta, 'gtin', None) or mapping.gtin,
            item_name=str(
                getattr(catalog_meta, 'item_name', None)
                or getattr(identity, "item_name", None)
                or row.product_name
            ),
            variation_name=str(
                getattr(catalog_meta, 'variation_name', None)
                or getattr(identity, "variation_name", None)
                or row.variation_name
            ),
            unit_cost=_positive_decimal(mapping.unit_cost) or _positive_decimal(
                getattr(catalog_meta, 'unit_cost', None)
            ),
            unit_price=getattr(catalog_meta, 'unit_price', None),
            suggested_qty=preview_line.adjusted_suggested_qty or 0,
            ordered_qty=preview_line.final_qty,
            received_qty_total=0,
            in_transit_qty=preview_line.final_qty,
            confidence_score=Decimal("1.0000"),
            confidence_state=PurchaseOrderConfidenceState.NORMAL,
            par_source=ParLevelSource.DYNAMIC,
            manual_par_level=None,
            suggested_par_level=(
                _ceil(row.average_weekly_sales * (preview.target_weeks or ZERO))
                if preview.mode == "target_weeks"
                else None
            ),
            removed=False,
        )
        db.add(line)
        db.flush()
        sales_by_store = dict(row.store_sales)
        total_sales = sum(
            (sales_by_store.get(int(store_id), ZERO) for store_id in allocation_stores),
            ZERO,
        )
        remaining = preview_line.final_qty
        allocations: list[tuple[int, int]] = []
        for index, store_id in enumerate(allocation_stores):
            if index == len(allocation_stores) - 1:
                quantity = remaining
            elif total_sales > 0:
                quantity = int(
                    (
                        Decimal(preview_line.final_qty)
                        * sales_by_store.get(int(store_id), ZERO)
                        / total_sales
                    ).to_integral_value()
                )
                quantity = max(0, min(quantity, remaining))
            else:
                quantity = preview_line.final_qty if index == 0 else 0
            remaining -= quantity
            allocations.append((int(store_id), quantity))
        for store_id, quantity in allocations:
            db.add(
                PurchaseOrderStoreAllocation(
                    purchase_order_line_id=line.id,
                    store_id=store_id,
                    expected_qty=quantity,
                    allocated_qty=quantity,
                    variance_qty=0,
                )
            )
    db.flush()
    return order, True
