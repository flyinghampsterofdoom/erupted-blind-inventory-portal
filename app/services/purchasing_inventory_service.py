"""One incoming-supply definition; contains no demand/reorder mathematics."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from sqlalchemy import select, func, text
from app.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation, PurchaseOrderStatus, PurchaseOrderReceiptAllocation, Store
from app.services.purchasing_evidence_service import quantity, utc

OPEN_STATUSES = (PurchaseOrderStatus.IN_TRANSIT, PurchaseOrderStatus.RECEIVED_SPLIT_PENDING)


@dataclass(frozen=True)
class IncomingEvidence:
    purchase_order_id: int
    vendor_id: int
    store_id: int
    variation_id: str
    sku: str
    quantity: int | None
    ordered_at: datetime | None
    reasons: tuple[str, ...] = ()


def outstanding_quantity(ordered, received, *, closed=0):
    values = tuple(quantity(value) for value in (ordered, received, closed))
    if any(value is None or value < 0 or value != value.to_integral_value() for value in values):
        return None
    remaining = values[0] - values[1] - values[2]
    # Preserve historical overreceipts as audit evidence; they have no remaining supply.
    return int(max(remaining, Decimal(0)))


def load_incoming_supply(db, *, store_ids=None):
    query = select(PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation).join(
        PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id).join(
        PurchaseOrderStoreAllocation, PurchaseOrderStoreAllocation.purchase_order_line_id == PurchaseOrderLine.id
    ).where(PurchaseOrder.status.in_(OPEN_STATUSES), PurchaseOrderLine.removed.is_(False))
    rows = db.execute(query).all()
    if not rows:
        return ()
    line_ids = {line.id for _, line, allocation in rows}
    closed_by_pair = dict(((line_id, store_id), int(closed)) for line_id, store_id, closed in db.execute(
        select(PurchaseOrderReceiptAllocation.purchase_order_line_id, PurchaseOrderReceiptAllocation.store_id,
               func.sum(PurchaseOrderReceiptAllocation.closed_quantity)).where(
            PurchaseOrderReceiptAllocation.purchase_order_line_id.in_(line_ids)).group_by(
            PurchaseOrderReceiptAllocation.purchase_order_line_id, PurchaseOrderReceiptAllocation.store_id)).all())
    # A receipt may go to a different store than planned. Cap the remaining
    # destination weights to the actual unfilled supplier commitment, using the
    # existing integer allocation utility; never invent incoming units.
    from app.services.purchase_order_math_service import allocate_purchase
    remaining_by_line = {}
    for _, line, allocation in rows:
        remaining_by_line.setdefault(line.id,{})[allocation.store_id] = outstanding_quantity(
            allocation.allocated_qty,allocation.store_received_qty or 0,
            closed=closed_by_pair.get((line.id,allocation.store_id),0))
    closed_by_line={}
    for (line_id,_),closed in closed_by_pair.items():closed_by_line[line_id]=closed_by_line.get(line_id,0)+closed
    unique_lines={line.id:line for _,line,_ in rows}
    for line in unique_lines.values():
        weights=remaining_by_line[line.id]
        cap=outstanding_quantity(line.ordered_qty,line.received_qty_total or 0,
            closed=closed_by_line.get(line.id,0))
        if cap is not None and all(value is not None for value in weights.values()) and sum(weights.values())>cap:
            remaining_by_line[line.id]=allocate_purchase(cap,weights)
    output = []
    for po, line, allocation in rows:
        if store_ids is not None and allocation.store_id not in store_ids:
            continue
        received = allocation.store_received_qty
        reasons = []
        # NULL means not entered on an untouched legacy allocation. It is not a Square count.
        received = 0 if received is None else received
        remaining = remaining_by_line[line.id][allocation.store_id]
        if remaining is None:
            reasons.append('INVALID_INCOMING_QUANTITY')
        if not line.variation_id or str(line.variation_id).startswith('SKU::'):
            reasons.append('UNRESOLVED_INCOMING_IDENTITY')
        if reasons:
            remaining = None
        output.append(IncomingEvidence(po.id, po.vendor_id, allocation.store_id, str(line.variation_id or ''),
            str(line.sku or ''), remaining, po.ordered_at or po.submitted_at or po.created_at, tuple(reasons)))
    # Phase7 V4 quantities come from its verified receiving projection. Original
    # public order/financial facts remain immutable; V3 rows keep their old owner.
    if db.get_bind().dialect.name == 'postgresql' and db.scalar(text("SELECT to_regclass('purchasing_v4.receiving_supply')")):
        current = db.execute(text("""SELECT s.*,p.vendor_id,coalesce(p.ordered_at,p.submitted_at,p.created_at) AS ordered_at
          FROM purchasing_v4.receiving_supply s JOIN public.purchase_orders p ON p.id=s.purchase_order_id
          ORDER BY s.id,s.store_id""")).mappings().all()
        v4_orders = {row['purchase_order_id'] for row in current}
        output = [row for row in output if row.purchase_order_id not in v4_orders]
        for row in current:
            if store_ids is not None and row['store_id'] not in store_ids:
                continue
            known = row['store_received_qty'] is not None and row['received_qty_total'] is not None
            remaining = max(row['allocated_qty']-row['store_received_qty']-row['closed'],0) if known else None
            output.append(IncomingEvidence(row['purchase_order_id'],row['vendor_id'],row['store_id'],
                row['variation_id'],str(row['sku'] or ''),remaining,row['ordered_at'],
                () if known else ('UNRECONCILED_V4_RECEIVING',)))
    return tuple(output)


def qualified_incoming(db, *, variation_id, store_ids):
    rows = [row for row in load_incoming_supply(db, store_ids=store_ids) if row.variation_id == variation_id]
    if any(row.quantity is None for row in rows):
        return None
    return sum(row.quantity for row in rows)


def inventory_position(on_hand, incoming, *, non_sellable=0, committed_in=0, committed_out=0):
    """Purchasing position is the exact live count plus qualified incoming.

    Legacy diagnostic arguments remain accepted but cannot alter stock authority.
    """
    hand, inbound = quantity(on_hand), quantity(incoming)
    if hand is None or inbound is None or inbound < 0:
        return None
    return hand + inbound


def purchasing_store_locations(db):
    """Resolve the four approved stores. Ambiguous/missing mappings stay unknown."""
    named = {'andresen': [], 'hwy99': [], 'sr503': [], 'longview': []}
    for store in db.scalars(select(Store).where(Store.active.is_(True))).all():
        key = ''.join(ch for ch in store.name.lower() if ch.isalnum())
        aliases = {'highway99': 'hwy99', 'hwy99': 'hwy99', 'sr503': 'sr503',
                   'andresen': 'andresen', 'longview': 'longview'}
        if key in aliases:
            named[aliases[key]].append(store)
    locations, issues = {}, []
    for key, stores in named.items():
        if len(stores) != 1 or not stores[0].square_location_id:
            issues.append(f'UNKNOWN_STORE_MAPPING:{key}')
        else:
            locations[stores[0].id] = stores[0].square_location_id
    duplicates = {loc for loc in locations.values() if list(locations.values()).count(loc) > 1}
    for sid in list(locations):
        if locations[sid] in duplicates:
            issues.append(f'AMBIGUOUS_LOCATION:{sid}')
            del locations[sid]
    return locations, tuple(issues)


def receipt_observation_requirements(db, *, variation_ids, store_ids):
    """Compatibility signature only. Historical receipts impose no stock barrier.

    Delivery evidence remains available through receipts and Square-sync history;
    it cannot be converted into, or veto, current live Square inventory.
    """
    return {}


def receipt_observation_verified(requirements, *, store_id, variation_id, observed_at, calculated_at):
    """Legacy compatibility hook: receiving history never vetoes a live count.

    Count validity and requested-pair coverage belong to the Square read gateway.
    """
    return not (observed_at is not None and calculated_at is not None and utc(calculated_at) > utc(observed_at))
