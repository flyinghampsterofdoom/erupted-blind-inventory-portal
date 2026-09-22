"""Human purchasing decisions; deliberately independent of vendor configuration writes."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation,
                        PurchaseOrderStatus, PurchaseOrderConfidenceState, ParLevelSource,
                        Store, VendorSkuConfig)
from app.services.audit_service import log_audit
from app.services.square_ordering_data_service import fetch_catalog_variation_maps

_cache_lock = Lock()
_catalog = {}
_catalog_at = 0.0


def catalog_variations():
    """Complete read-only catalog, keyed by real identity, never a SKU-first collapse."""
    global _catalog, _catalog_at
    with _cache_lock:
        if not _catalog or monotonic() - _catalog_at > 60:
            rows, _ = fetch_catalog_variation_maps()
            _catalog = rows
            _catalog_at = monotonic()
        return dict(_catalog)


def purchase_cost(raw) -> Decimal:
    if raw is None or not str(raw).strip():
        raise ValueError('Enter and confirm the purchase unit cost. Blank is unknown; zero is allowed.')
    try:
        cost = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError('Enter a valid purchase unit cost.') from exc
    if not cost.is_finite() or cost < 0 or cost >= Decimal('10000000000') or cost != cost.quantize(Decimal('.0001')):
        raise ValueError('Cost must be nonnegative, below 10 billion, with at most four decimal places.')
    return cost


def search_products(db: Session, *, vendor_id: int, query: str) -> list[dict]:
    tokens = query.casefold().split()
    if not tokens:
        return []
    if len(query) > 200:
        raise ValueError('Search must be at most 200 characters.')
    matches = []
    for identity, meta in catalog_variations().items():
        name = f'{meta.item_name} — {meta.variation_name}'
        searchable = f'{name} {meta.sku or ""} {meta.gtin or ""}'.casefold()
        if all(token in searchable for token in tokens):
            rank = (0 if query.casefold() in {str(meta.sku or '').casefold(), str(meta.gtin or '').casefold()} else 1,
                    0 if name.casefold().startswith(query.casefold()) else 1, name.casefold(), identity)
            matches.append((rank, identity, meta, name))
    selected = sorted(matches, key=lambda row: row[0])[:30]
    ids = [row[1] for row in selected]
    mappings = db.scalars(select(VendorSkuConfig).where(
        VendorSkuConfig.vendor_id == vendor_id, VendorSkuConfig.active.is_(True),
        VendorSkuConfig.square_variation_id.in_(ids),
    ).order_by(VendorSkuConfig.id)).all() if ids else []
    costs = {}
    for row in mappings:
        costs.setdefault(row.square_variation_id, row.unit_cost)
    return [dict(variation_id=identity, name=name, sku=meta.sku, gtin=meta.gtin,
                 unit_cost=str(costs[identity]) if costs.get(identity) is not None else None)
            for _, identity, meta, name in selected]


def add_catalog_product(db: Session, *, purchase_order_id: int, variation_id: str,
                        initial_qty: int, unit_cost, actor_id: int, ip=None) -> PurchaseOrderLine:
    cost = purchase_cost(unit_cost)
    if isinstance(initial_qty, bool) or not isinstance(initial_qty, int) or not 1 <= initial_qty <= 1_000_000:
        raise ValueError('Quantity must be a whole number between 1 and 1,000,000.')
    po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id).with_for_update())
    if po is None or po.status not in {PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.IN_TRANSIT}:
        raise ValueError('Only active orders can be edited.')
    meta = catalog_variations().get(variation_id)
    if meta is None or not variation_id or variation_id.startswith(('SKU::', 'VAR::')):
        raise ValueError('Select a current catalog variation from the search results.')
    existing = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == po.id, PurchaseOrderLine.variation_id == variation_id))
    if existing is not None:
        if not existing.removed:
            raise ValueError('This variation already exists on this order. Edit its existing quantity.')
        if existing.unit_cost is None or existing.unit_cost != cost:
            raise ValueError(f'Restoring this line requires its saved purchase cost ({existing.unit_cost if existing.unit_cost is not None else "UNKNOWN"}). Historical cost changes require an audited correction.')
        if existing.received_qty_total:
            raise ValueError('A removed line with receipt history requires an audited receipt correction before restoration.')
    stores = db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name, Store.id)).all()
    if not stores:
        raise ValueError('No active stores are available.')
    line = existing or PurchaseOrderLine(purchase_order_id=po.id, variation_id=variation_id,
        sku=meta.sku or None, item_name=meta.item_name, variation_name=meta.variation_name,
        gtin=meta.gtin, unit_price=meta.unit_price, unit_cost=cost, suggested_qty=initial_qty,
        ordered_qty=initial_qty, received_qty_total=0, in_transit_qty=initial_qty,
        confidence_score=Decimal('1'), confidence_state=PurchaseOrderConfidenceState.NORMAL,
        par_source=ParLevelSource.DYNAMIC, removed=False)
    line.removed = False
    line.ordered_qty = initial_qty
    line.in_transit_qty = initial_qty
    db.add(line)
    db.flush()
    prior_allocations = {a.store_id:a for a in db.scalars(select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id == line.id))}
    if any(a.store_received_qty for a in prior_allocations.values()):
        raise ValueError('Receipt history must be reconciled before restoring this line.')
    for allocation in prior_allocations.values():
        allocation.allocated_qty = 0
        allocation.variance_qty = -int(allocation.expected_qty or 0)
    for index, store in enumerate(stores):
        qty = initial_qty if index == 0 else 0
        allocation = prior_allocations.get(store.id)
        if allocation is None:
            db.add(PurchaseOrderStoreAllocation(purchase_order_line_id=line.id, store_id=store.id,
                expected_qty=0, allocated_qty=qty, variance_qty=qty, store_received_qty=0))
        else:
            allocation.allocated_qty = qty
            allocation.variance_qty = qty - int(allocation.expected_qty or 0)
    log_audit(db, actor_principal_id=actor_id, action='ORDERING_CATALOG_PRODUCT_RESTORED' if existing is not None else 'ORDERING_CATALOG_PRODUCT_ADDED',
        session_id=None, ip=ip, metadata={'purchase_order_id': po.id, 'vendor_id': po.vendor_id,
            'line_id': line.id, 'variation_id': variation_id, 'sku': line.sku,
            'item_name': line.item_name, 'variation_name': line.variation_name, 'gtin': line.gtin,
            'unit_cost': str(cost), 'cost_source': 'USER_CONFIRMED', 'quantity': initial_qty,
            'allocations': {str(store.id): initial_qty if index == 0 else 0 for index, store in enumerate(stores)}})
    db.flush()
    return line
