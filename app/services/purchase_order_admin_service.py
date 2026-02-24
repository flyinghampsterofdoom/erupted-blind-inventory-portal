from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import (
    ParLevel,
    ParLevelSource,
    PurchaseOrder,
    PurchaseOrderConfidenceState,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Vendor,
)
from app.services.purchase_order_generation_service import generate_vendor_scoped_recommendations
from app.services.purchase_order_math_service import MathOverrides


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


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


def _history_loader_from_order_lines(db: Session):
    def loader(vendor_id: int, sku: str, lookback_days: int) -> list[Decimal]:
        from_dt = _now() - timedelta(days=lookback_days)
        rows = db.execute(
            select(
                func.date_trunc('day', func.coalesce(PurchaseOrder.submitted_at, PurchaseOrder.created_at)).label('day'),
                func.coalesce(func.sum(PurchaseOrderLine.ordered_qty), 0).label('qty'),
            )
            .select_from(PurchaseOrderLine)
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .where(
                PurchaseOrder.vendor_id == vendor_id,
                PurchaseOrderLine.sku == sku,
                PurchaseOrderLine.removed.is_(False),
                PurchaseOrder.status.in_(
                    [
                        PurchaseOrderStatus.IN_TRANSIT,
                        PurchaseOrderStatus.RECEIVED_SPLIT_PENDING,
                        PurchaseOrderStatus.SENT_TO_STORES,
                        PurchaseOrderStatus.COMPLETED,
                    ]
                ),
                func.coalesce(PurchaseOrder.submitted_at, PurchaseOrder.created_at) >= from_dt,
            )
            .group_by('day')
            .order_by('day')
        ).all()

        if not rows:
            return []

        start_day = from_dt.date()
        by_day: dict[datetime.date, Decimal] = {}
        for row in rows:
            day = row.day.date()
            by_day[day] = Decimal(row.qty or 0)

        out: list[Decimal] = []
        for i in range(lookback_days):
            day = start_day + timedelta(days=i)
            out.append(by_day.get(day, Decimal('0')))
        return out

    return loader


def _on_hand_loader_stub(_store_id: int, _sku: str) -> Decimal:
    # Phase 3: inventory by SKU is not wired yet in this codebase.
    # Phase 5 will replace this with Square-backed per-store on-hand lookup.
    return Decimal('0')


def generate_purchase_orders(
    db: Session,
    *,
    vendor_ids: list[int],
    store_id: int,
    created_by_principal_id: int,
    reorder_weeks: int,
    stock_up_weeks: int,
    history_lookback_days: int,
) -> list[PurchaseOrder]:
    if not vendor_ids:
        raise ValueError('Select at least one vendor')

    overrides = MathOverrides(
        reorder_weeks=reorder_weeks,
        stock_up_weeks=stock_up_weeks,
        history_lookback_days=history_lookback_days,
    )
    lines = generate_vendor_scoped_recommendations(
        db,
        vendor_ids=vendor_ids,
        store_id=store_id,
        history_loader=_history_loader_from_order_lines(db),
        on_hand_loader=_on_hand_loader_stub,
        overrides=overrides,
    )
    if not lines:
        return []

    orders_by_vendor: dict[int, PurchaseOrder] = {}
    created_orders: list[PurchaseOrder] = []
    for line in lines:
        po = orders_by_vendor.get(line.vendor_id)
        if po is None:
            po = PurchaseOrder(
                vendor_id=line.vendor_id,
                status=PurchaseOrderStatus.DRAFT,
                reorder_weeks=reorder_weeks,
                stock_up_weeks=stock_up_weeks,
                history_lookback_days=history_lookback_days,
                created_by_principal_id=created_by_principal_id,
            )
            db.add(po)
            db.flush()
            orders_by_vendor[line.vendor_id] = po
            created_orders.append(po)

        result = line.result
        db.add(
            PurchaseOrderLine(
                purchase_order_id=po.id,
                variation_id=f'SKU::{line.sku}',
                sku=line.sku,
                item_name=line.sku,
                variation_name='Default',
                suggested_qty=result.rounded_recommended_qty,
                ordered_qty=result.rounded_recommended_qty,
                received_qty_total=0,
                in_transit_qty=result.rounded_recommended_qty,
                confidence_score=result.confidence_score,
                confidence_state=result.confidence_state,
                par_source=result.par_source,
                manual_par_level=result.effective_reorder_level if result.par_source == ParLevelSource.MANUAL else None,
                suggested_par_level=result.suggested_reorder_level,
                removed=False,
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
) -> PurchaseOrder:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status != PurchaseOrderStatus.DRAFT:
        raise ValueError('Only draft orders can be edited')

    lines = db.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)).scalars().all()
    for line in lines:
        if line.id in ordered_qty_by_line_id:
            qty = ordered_qty_by_line_id[line.id]
            if qty < 0:
                raise ValueError('Ordered quantity cannot be negative')
            line.ordered_qty = qty
            line.in_transit_qty = max(qty - line.received_qty_total, 0)
        line.removed = line.id in removed_line_ids
        if line.id in manual_par_by_line_id:
            line.manual_par_level = manual_par_by_line_id[line.id]
        line.updated_at = _now()
    po.updated_at = _now()
    db.flush()
    return po


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


def parse_generation_form(form) -> tuple[list[int], int, int, int, int]:
    vendor_ids = [int(value) for value in form.getlist('vendor_ids') if str(value).strip().isdigit()]
    store_id = _parse_int(str(form.get('store_id', '0')), field='Store', minimum=1)
    reorder_weeks = _parse_int(str(form.get('reorder_weeks', '5')), field='Reorder weeks', minimum=1)
    stock_up_weeks = _parse_int(str(form.get('stock_up_weeks', '10')), field='Stock-up weeks', minimum=1)
    history_lookback_days = _parse_int(
        str(form.get('history_lookback_days', '120')),
        field='History lookback days',
        minimum=7,
    )
    if stock_up_weeks <= reorder_weeks:
        raise ValueError('Stock-up weeks must be greater than reorder weeks')
    return vendor_ids, store_id, reorder_weeks, stock_up_weeks, history_lookback_days
