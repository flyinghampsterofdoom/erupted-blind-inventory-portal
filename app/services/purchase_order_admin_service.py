from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import StringIO

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import (
    ParLevel,
    ParLevelSource,
    PurchaseOrder,
    PurchaseOrderConfidenceState,
    PurchaseOrderLine,
    PurchaseOrderStoreAllocation,
    PurchaseOrderStatus,
    Store,
    Vendor,
    VendorSkuConfig,
)
from app.services.purchase_order_generation_service import generate_vendor_scoped_recommendations
from app.services.purchase_order_math_service import MathOverrides
from app.services.square_ordering_data_service import (
    build_square_ordering_snapshot,
    fetch_catalog_by_sku,
    sync_vendor_sku_configs_from_square,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _store_split_label(store_name: str) -> str:
    name = (store_name or '').strip()
    key = name.lower()
    if '99' in key:
        return '99'
    if 'andresen' in key:
        return 'A'
    if '503' in key:
        return '503'
    if 'longview' in key:
        return 'L'
    return name


def _parse_int(value: str | None, *, field: str, minimum: int | None = None) -> int:
    raw = (value or '').strip()
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f'Invalid {field}') from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f'{field} must be at least {minimum}')
    return parsed


def list_active_vendors(db: Session) -> list[Vendor]:
    return db.execute(select(Vendor).where(Vendor.active.is_(True)).order_by(Vendor.name.asc())).scalars().all()


def list_purchase_orders(db: Session, *, limit: int = 100) -> list[dict]:
    rows = db.execute(
        select(PurchaseOrder, Vendor.name)
        .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
        .order_by(PurchaseOrder.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            'id': po.id,
            'vendor_id': po.vendor_id,
            'vendor_name': vendor_name,
            'status': po.status.value,
            'created_at': po.created_at,
            'submitted_at': po.submitted_at,
            'ordered_at': po.ordered_at,
        }
        for po, vendor_name in rows
    ]


def generate_purchase_orders(
    db: Session,
    *,
    vendor_ids: list[int],
    created_by_principal_id: int,
    reorder_weeks: int,
    stock_up_weeks: int,
    history_lookback_days: int,
) -> list[PurchaseOrder]:
    if not vendor_ids:
        raise ValueError('Select at least one vendor')

    sync_vendor_sku_configs_from_square(db, vendor_ids=vendor_ids)

    mapped_count = db.execute(
        select(VendorSkuConfig.id)
        .where(
            VendorSkuConfig.vendor_id.in_(vendor_ids),
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
        )
        .limit(1)
    ).first()
    if not mapped_count:
        raise ValueError(
            'No vendor SKU mappings found for selected vendors. Square did not return vendor assignments for these SKUs; configure vendor_sku_configs manually.'
        )

    overrides = MathOverrides(
        reorder_weeks=reorder_weeks,
        stock_up_weeks=stock_up_weeks,
        history_lookback_days=history_lookback_days,
    )
    snapshot = build_square_ordering_snapshot(db, vendor_ids=vendor_ids, lookback_days=history_lookback_days)
    if not snapshot.meta_by_vendor_sku:
        raise ValueError(
            'No Square catalog mappings resolved for selected vendors. Confirm SKU values and/or square_variation_id mappings.'
        )
    lines = generate_vendor_scoped_recommendations(
        db,
        vendor_ids=vendor_ids,
        history_loader=snapshot.history_loader,
        on_hand_loader=snapshot.on_hand_loader,
        overrides=overrides,
    )
    if not lines:
        raise ValueError(
            'No order quantities generated. Current settings and Square history/on-hand produced zero demand.'
        )

    grouped: dict[tuple[int, str], list] = {}
    for line in lines:
        grouped.setdefault((line.vendor_id, line.sku), []).append(line)

    orders_by_vendor: dict[int, PurchaseOrder] = {}
    created_orders: list[PurchaseOrder] = []
    for (vendor_id, sku), store_lines in grouped.items():
        po = orders_by_vendor.get(vendor_id)
        if po is None:
            po = PurchaseOrder(
                vendor_id=vendor_id,
                status=PurchaseOrderStatus.DRAFT,
                reorder_weeks=reorder_weeks,
                stock_up_weeks=stock_up_weeks,
                history_lookback_days=history_lookback_days,
                created_by_principal_id=created_by_principal_id,
            )
            db.add(po)
            db.flush()
            orders_by_vendor[vendor_id] = po
            created_orders.append(po)

        total_qty = sum(row.result.rounded_recommended_qty for row in store_lines)
        if total_qty <= 0:
            continue
        confidence_score = min(row.result.confidence_score for row in store_lines)
        confidence_state = (
            PurchaseOrderConfidenceState.LOW
            if any(row.result.confidence_state == PurchaseOrderConfidenceState.LOW for row in store_lines)
            else PurchaseOrderConfidenceState.NORMAL
        )
        suggested_qty = sum(row.result.suggested_stock_up_level for row in store_lines)
        suggested_par = sum(row.result.suggested_reorder_level for row in store_lines)
        base_result = store_lines[0].result
        meta = snapshot.meta_for(vendor_id, sku)
        po_line = PurchaseOrderLine(
            purchase_order_id=po.id,
            variation_id=meta.variation_id if meta else f'SKU::{sku}',
            sku=sku,
            item_name=meta.item_name if meta else sku,
            variation_name=meta.variation_name if meta else 'Default',
            unit_cost=meta.unit_cost if meta else None,
            unit_price=meta.unit_price if meta else None,
            suggested_qty=suggested_qty,
            ordered_qty=total_qty,
            received_qty_total=0,
            in_transit_qty=total_qty,
            confidence_score=confidence_score,
            confidence_state=confidence_state,
            par_source=base_result.par_source,
            manual_par_level=(
                base_result.effective_reorder_level if base_result.par_source == ParLevelSource.MANUAL else None
            ),
            suggested_par_level=suggested_par,
            removed=False,
        )
        db.add(po_line)
        db.flush()
        for row in store_lines:
            db.add(
                PurchaseOrderStoreAllocation(
                    purchase_order_line_id=po_line.id,
                    store_id=row.store_id,
                    expected_qty=row.result.rounded_recommended_qty,
                    allocated_qty=row.result.rounded_recommended_qty,
                    variance_qty=0,
                )
            )
    db.flush()
    return created_orders


def get_purchase_order_detail(db: Session, *, purchase_order_id: int) -> dict:
    po_row = db.execute(
        select(PurchaseOrder, Vendor.name)
        .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
        .where(PurchaseOrder.id == purchase_order_id)
    ).one_or_none()
    if not po_row:
        raise ValueError('Order not found')
    po, vendor_name = po_row

    rows = db.execute(
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.purchase_order_id == po.id)
        .order_by(PurchaseOrderLine.confidence_state.asc(), PurchaseOrderLine.item_name.asc())
    ).scalars().all()

    normal_lines: list[dict] = []
    low_confidence_lines: list[dict] = []
    allocations_by_line_id: dict[int, list[dict]] = {}
    line_ids = [row.id for row in rows]
    allocation_rows = []
    if line_ids:
        allocation_rows = db.execute(
            select(
                PurchaseOrderStoreAllocation.purchase_order_line_id,
                PurchaseOrderStoreAllocation.store_id,
                PurchaseOrderStoreAllocation.expected_qty,
                PurchaseOrderStoreAllocation.allocated_qty,
                Store.name,
            )
            .join(Store, Store.id == PurchaseOrderStoreAllocation.store_id)
            .where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids))
            .order_by(Store.name.asc())
        ).all()
    for allocation in allocation_rows:
        allocations_by_line_id.setdefault(allocation.purchase_order_line_id, []).append(
            {
                'store_id': int(allocation.store_id),
                'store_name': allocation.name,
                'store_label': _store_split_label(allocation.name),
                'expected_qty': int(allocation.expected_qty),
                'allocated_qty': int(allocation.allocated_qty),
            }
        )

    for row in rows:
        line = {
            'id': row.id,
            'sku': row.sku or '',
            'item_name': row.item_name,
            'variation_name': row.variation_name,
            'unit_cost': row.unit_cost,
            'unit_price': row.unit_price,
            'suggested_qty': row.suggested_qty,
            'ordered_qty': row.ordered_qty,
            'manual_par_level': row.manual_par_level,
            'suggested_par_level': row.suggested_par_level,
            'par_source': row.par_source.value,
            'confidence_state': row.confidence_state.value,
            'confidence_score': row.confidence_score,
            'removed': row.removed,
            'store_allocations': allocations_by_line_id.get(row.id, []),
        }
        if row.confidence_state == PurchaseOrderConfidenceState.LOW:
            low_confidence_lines.append(line)
        else:
            normal_lines.append(line)

    return {
        'order': po,
        'vendor_name': vendor_name,
        'normal_lines': normal_lines,
        'low_confidence_lines': low_confidence_lines,
    }


def save_purchase_order_lines(
    db: Session,
    *,
    purchase_order_id: int,
    ordered_qty_by_line_id: dict[int, int],
    removed_line_ids: set[int],
    manual_par_by_line_id: dict[int, int | None],
    allocation_qty_by_line_store: dict[tuple[int, int], int],
) -> PurchaseOrder:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status != PurchaseOrderStatus.DRAFT:
        raise ValueError('Only draft orders can be edited')

    lines = db.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)).scalars().all()
    line_ids = [line.id for line in lines]
    allocations_by_line_store: dict[tuple[int, int], PurchaseOrderStoreAllocation] = {}
    if line_ids:
        allocation_rows = db.execute(
            select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids))
        ).scalars().all()
        for allocation in allocation_rows:
            allocations_by_line_store[(int(allocation.purchase_order_line_id), int(allocation.store_id))] = allocation

    for line in lines:
        allocation_override_present = False
        allocation_total = 0
        for (line_id, store_id), qty in allocation_qty_by_line_store.items():
            if line_id != line.id:
                continue
            allocation = allocations_by_line_store.get((line_id, store_id))
            if allocation is None:
                continue
            if qty < 0:
                raise ValueError('Store split quantity cannot be negative')
            allocation.allocated_qty = qty
            allocation.variance_qty = qty - allocation.expected_qty
            allocation.updated_at = _now()
            allocation_override_present = True
            allocation_total += qty

        if line.id in ordered_qty_by_line_id:
            qty = ordered_qty_by_line_id[line.id]
            if qty < 0:
                raise ValueError('Ordered quantity cannot be negative')
            line.ordered_qty = qty
            line.in_transit_qty = max(qty - line.received_qty_total, 0)
        if allocation_override_present:
            line.ordered_qty = allocation_total
            line.in_transit_qty = max(allocation_total - line.received_qty_total, 0)
        line.removed = line.id in removed_line_ids
        if line.id in manual_par_by_line_id:
            line.manual_par_level = manual_par_by_line_id[line.id]
        line.updated_at = _now()
    po.updated_at = _now()
    db.flush()
    return po


def delete_draft_purchase_order(db: Session, *, purchase_order_id: int) -> None:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status != PurchaseOrderStatus.DRAFT:
        raise ValueError('Only draft orders can be discarded')
    db.delete(po)
    db.flush()


def submit_purchase_order(db: Session, *, purchase_order_id: int, actor_principal_id: int) -> PurchaseOrder:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status != PurchaseOrderStatus.DRAFT:
        raise ValueError('Only draft orders can be submitted')

    lines = db.execute(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id == purchase_order_id,
            PurchaseOrderLine.removed.is_(False),
        )
    ).scalars().all()
    if not lines:
        raise ValueError('Cannot submit an empty order')

    missing_low_confidence = [
        line.sku or line.item_name
        for line in lines
        if line.confidence_state == PurchaseOrderConfidenceState.LOW and line.manual_par_level is None
    ]
    if missing_low_confidence:
        raise ValueError('Low confidence lines require manual par level before submit')

    for line in lines:
        line.in_transit_qty = max(line.ordered_qty - line.received_qty_total, 0)
        if line.sku:
            par = db.execute(
                select(ParLevel).where(
                    and_(
                        ParLevel.vendor_id == po.vendor_id,
                        ParLevel.sku == line.sku,
                    )
                )
            ).scalar_one_or_none()
            if par is None:
                par = ParLevel(
                    sku=line.sku,
                    vendor_id=po.vendor_id,
                    manual_par_level=line.manual_par_level,
                    suggested_par_level=line.suggested_par_level,
                    par_source=line.par_source,
                    confidence_score=line.confidence_score,
                    confidence_state=line.confidence_state,
                    locked_manual=(line.par_source == ParLevelSource.MANUAL),
                    updated_by_principal_id=actor_principal_id,
                )
                db.add(par)
            else:
                par.manual_par_level = line.manual_par_level
                par.suggested_par_level = line.suggested_par_level
                par.par_source = line.par_source
                par.confidence_score = line.confidence_score
                par.confidence_state = line.confidence_state
                par.locked_manual = line.par_source == ParLevelSource.MANUAL
                par.updated_by_principal_id = actor_principal_id

    po.status = PurchaseOrderStatus.IN_TRANSIT
    po.ordered_at = _now()
    po.submitted_at = _now()
    po.submitted_by_principal_id = actor_principal_id
    po.updated_at = _now()
    db.flush()
    return po


def parse_generation_form(form) -> tuple[list[int], int, int, int]:
    vendor_ids = [int(value) for value in form.getlist('vendor_ids') if str(value).strip().isdigit()]
    reorder_weeks = _parse_int(str(form.get('reorder_weeks', '5')), field='Reorder weeks', minimum=1)
    stock_up_weeks = _parse_int(str(form.get('stock_up_weeks', '10')), field='Stock-up weeks', minimum=1)
    history_lookback_days = _parse_int(
        str(form.get('history_lookback_days', '120')),
        field='History lookback days',
        minimum=7,
    )
    if stock_up_weeks <= reorder_weeks:
        raise ValueError('Stock-up weeks must be greater than reorder weeks')
    return vendor_ids, reorder_weeks, stock_up_weeks, history_lookback_days


def list_vendor_sku_configs(db: Session, *, vendor_id: int | None = None) -> list[dict]:
    query = (
        select(VendorSkuConfig, Vendor.name)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .order_by(Vendor.name.asc(), VendorSkuConfig.sku.asc())
    )
    if vendor_id is not None:
        query = query.where(VendorSkuConfig.vendor_id == vendor_id)
    rows = db.execute(query).all()
    return [
        {
            'id': cfg.id,
            'vendor_id': cfg.vendor_id,
            'vendor_name': vendor_name,
            'sku': cfg.sku,
            'square_variation_id': cfg.square_variation_id,
            'pack_size': cfg.pack_size,
            'min_order_qty': cfg.min_order_qty,
            'is_default_vendor': cfg.is_default_vendor,
            'active': cfg.active,
        }
        for cfg, vendor_name in rows
    ]


def upsert_vendor_sku_config(
    db: Session,
    *,
    vendor_id: int,
    sku: str,
    square_variation_id: str | None,
    pack_size: int,
    min_order_qty: int,
    is_default_vendor: bool = True,
    active: bool = True,
) -> VendorSkuConfig:
    clean_sku = sku.strip()
    if not clean_sku:
        raise ValueError('SKU is required')
    if pack_size < 1:
        raise ValueError('Pack size must be at least 1')
    if min_order_qty < 0:
        raise ValueError('Min order qty cannot be negative')

    vendor_exists = db.execute(select(Vendor.id).where(Vendor.id == vendor_id, Vendor.active.is_(True))).scalar_one_or_none()
    if not vendor_exists:
        raise ValueError('Vendor not found')

    existing = db.execute(
        select(VendorSkuConfig).where(
            VendorSkuConfig.vendor_id == vendor_id,
            VendorSkuConfig.sku == clean_sku,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = VendorSkuConfig(
            vendor_id=vendor_id,
            sku=clean_sku,
            square_variation_id=(square_variation_id or '').strip() or None,
            pack_size=pack_size,
            min_order_qty=min_order_qty,
            is_default_vendor=is_default_vendor,
            active=active,
        )
        db.add(existing)
        db.flush()
        return existing

    existing.square_variation_id = (square_variation_id or '').strip() or None
    existing.pack_size = pack_size
    existing.min_order_qty = min_order_qty
    existing.is_default_vendor = is_default_vendor
    existing.active = active
    existing.updated_at = _now()
    db.flush()
    return existing


def import_vendor_sku_configs_csv(db: Session, *, csv_text: str) -> dict:
    reader = csv.DictReader(StringIO(csv_text))
    required = {'vendor_id', 'sku'}
    if not reader.fieldnames or not required.issubset({name.strip() for name in reader.fieldnames}):
        raise ValueError('CSV headers must include vendor_id and sku')

    created_or_updated = 0
    errors: list[str] = []
    for idx, row in enumerate(reader, start=2):
        try:
            vendor_id = int(str(row.get('vendor_id', '')).strip())
            sku = str(row.get('sku', '')).strip()
            square_variation_id = str(row.get('square_variation_id', '')).strip() or None
            pack_size_raw = str(row.get('pack_size', '1')).strip() or '1'
            min_qty_raw = str(row.get('min_order_qty', '0')).strip() or '0'
            is_default_raw = str(row.get('is_default_vendor', 'true')).strip().lower()
            active_raw = str(row.get('active', 'true')).strip().lower()

            upsert_vendor_sku_config(
                db,
                vendor_id=vendor_id,
                sku=sku,
                square_variation_id=square_variation_id,
                pack_size=int(pack_size_raw),
                min_order_qty=int(min_qty_raw),
                is_default_vendor=is_default_raw not in {'0', 'false', 'no'},
                active=active_raw not in {'0', 'false', 'no'},
            )
            created_or_updated += 1
        except Exception as exc:
            errors.append(f'Line {idx}: {exc}')
    return {'processed': created_or_updated, 'errors': errors}


def autofill_square_variation_ids(db: Session, *, vendor_id: int | None = None) -> dict:
    catalog = fetch_catalog_by_sku()
    query = select(VendorSkuConfig).where(VendorSkuConfig.active.is_(True))
    if vendor_id is not None:
        query = query.where(VendorSkuConfig.vendor_id == vendor_id)
    rows = db.execute(query).scalars().all()

    updated = 0
    skipped = 0
    for row in rows:
        if row.square_variation_id:
            skipped += 1
            continue
        meta = catalog.get(row.sku)
        if not meta:
            skipped += 1
            continue
        row.square_variation_id = meta.variation_id
        row.updated_at = _now()
        updated += 1
    db.flush()
    return {'updated': updated, 'skipped': skipped}
