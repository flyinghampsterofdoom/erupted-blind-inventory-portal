from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AuditLog,
    ConsignmentEmailDelivery,
    ConsignmentInventorySnapshot,
    ConsignmentLedgerEntry,
    ConsignmentReport,
    ConsignmentReportFactLink,
    ConsignmentReportLine,
    ConsignmentReturnFact,
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    OrderingCatalogIdentity,
    Store,
    Vendor,
    VendorPaymentSetting,
    VendorVariationAssignment,
    VendorVariationCost,
)
from app.services.v2_order_payments_service import (
    calculate_consignment_balance,
    inventory_snapshot,
    money,
    utc_now,
)


PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')
BLOCKING_STATUSES = {
    'MISSING_VENDOR', 'MISSING_COST', 'AMBIGUOUS_VENDOR', 'SOURCE_INCOMPLETE', 'UNMATCHED_RETURN'
}
SQUARE_OFFLINE_LAG = timedelta(hours=72)


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


def _money_object(value: object) -> tuple[Decimal, str]:
    raw = value if isinstance(value, dict) else {}
    return money(_decimal(raw.get('amount')) / Decimal('100')), str(raw.get('currency') or 'USD')


def _timestamp(value: object) -> datetime:
    clean = str(value or '').strip().replace('Z', '+00:00')
    parsed = datetime.fromisoformat(clean) if clean else utc_now()
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _business_date(at: datetime) -> date:
    return at.astimezone(PORTAL_TIMEZONE).date()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _audit(db: Session, *, actor_id: int, action: str, entity: str, entity_id: int, before=None, after=None, reason='', ip=None):
    db.add(AuditLog(
        actor_principal_id=actor_id, action=action, ip=ip,
        meta={'domain': 'CONSIGNMENT_FACTS_V2', 'entity_type': entity, 'entity_id': entity_id,
              'before': before or {}, 'after': after or {}, 'reason': reason},
    ))


def _period_overlap_query(model, *, variation_id: str, start_at: datetime, end_at: datetime | None, vendor_id: int | None = None):
    candidate_end = end_at or datetime.max.replace(tzinfo=timezone.utc)
    query = select(model).where(
        model.square_variation_id == variation_id,
        model.effective_start_at < candidate_end,
        or_(model.effective_end_at.is_(None), model.effective_end_at > start_at),
    )
    if vendor_id is not None:
        query = query.where(model.vendor_id == vendor_id)
    return query


def create_assignment(db: Session, *, vendor_id: int, variation_id: str, is_consignment: bool,
                      start_at: datetime, end_at: datetime | None, actor_id: int, notes: str = '', ip=None):
    if db.get(Vendor, vendor_id) is None or not variation_id.strip():
        raise ValueError('A valid vendor and variation are required.')
    if end_at and end_at <= start_at:
        raise ValueError('Assignment end must be after its start.')
    if db.scalar(_period_overlap_query(VendorVariationAssignment, variation_id=variation_id,
                                       start_at=start_at, end_at=end_at)):
        raise ValueError('Vendor assignment periods cannot overlap for a variation.')
    row = VendorVariationAssignment(
        vendor_id=vendor_id, square_variation_id=variation_id.strip(), is_consignment=is_consignment,
        effective_start_at=start_at, effective_end_at=end_at, source='OWNER', notes=notes.strip() or None,
        created_by_principal_id=actor_id,
    )
    db.add(row); db.flush()
    _audit(db, actor_id=actor_id, action='CONSIGNMENT_ASSIGNMENT_CREATED', entity='vendor_variation_assignment',
           entity_id=row.id, after={'vendor_id': vendor_id, 'variation_id': variation_id,
                                    'is_consignment': is_consignment, 'start_at': start_at.isoformat(),
                                    'end_at': end_at.isoformat() if end_at else None}, reason=notes, ip=ip)
    return row


def create_cost(db: Session, *, vendor_id: int, variation_id: str, unit_cost: Decimal,
                start_at: datetime, end_at: datetime | None, actor_id: int, notes: str = '', ip=None):
    cost = _decimal(unit_cost).quantize(Decimal('0.0001'))
    if cost < 0 or db.get(Vendor, vendor_id) is None or not variation_id.strip():
        raise ValueError('A valid vendor, variation, and non-negative cost are required.')
    if end_at and end_at <= start_at:
        raise ValueError('Cost end must be after its start.')
    if db.scalar(_period_overlap_query(VendorVariationCost, variation_id=variation_id,
                                       start_at=start_at, end_at=end_at, vendor_id=vendor_id)):
        raise ValueError('Cost periods cannot overlap for the same vendor and variation.')
    row = VendorVariationCost(
        vendor_id=vendor_id, square_variation_id=variation_id.strip(), unit_cost=cost, currency='USD',
        effective_start_at=start_at, effective_end_at=end_at, source='OWNER', notes=notes.strip() or None,
        created_by_principal_id=actor_id,
    )
    db.add(row); db.flush()
    _audit(db, actor_id=actor_id, action='CONSIGNMENT_COST_CREATED', entity='vendor_variation_cost',
           entity_id=row.id, after={'vendor_id': vendor_id, 'variation_id': variation_id,
                                    'unit_cost': str(cost), 'start_at': start_at.isoformat(),
                                    'end_at': end_at.isoformat() if end_at else None}, reason=notes, ip=ip)
    return row


@dataclass(frozen=True)
class Attribution:
    status: str
    source: str
    vendor_id: int | None = None
    vendor_name: str | None = None
    is_consignment: bool | None = None
    unit_cost: Decimal | None = None


def attribution_at(db: Session, *, variation_id: str | None, transacted_at: datetime) -> Attribution:
    if not variation_id:
        return Attribution('SOURCE_INCOMPLETE', 'SOURCE')
    assignments = db.scalars(select(VendorVariationAssignment).where(
        VendorVariationAssignment.square_variation_id == variation_id,
        VendorVariationAssignment.effective_start_at <= transacted_at,
        or_(VendorVariationAssignment.effective_end_at.is_(None),
            VendorVariationAssignment.effective_end_at > transacted_at),
    )).all()
    if not assignments:
        return Attribution('MISSING_VENDOR', 'EFFECTIVE_DATED_ASSIGNMENT')
    if len(assignments) != 1:
        return Attribution('AMBIGUOUS_VENDOR', 'EFFECTIVE_DATED_ASSIGNMENT')
    assignment = assignments[0]
    vendor = db.get(Vendor, assignment.vendor_id)
    if not assignment.is_consignment:
        return Attribution('NON_CONSIGNMENT', 'EFFECTIVE_DATED_ASSIGNMENT', assignment.vendor_id,
                           vendor.name if vendor else None, False)
    costs = db.scalars(select(VendorVariationCost).where(
        VendorVariationCost.vendor_id == assignment.vendor_id,
        VendorVariationCost.square_variation_id == variation_id,
        VendorVariationCost.effective_start_at <= transacted_at,
        or_(VendorVariationCost.effective_end_at.is_(None), VendorVariationCost.effective_end_at > transacted_at),
    )).all()
    if len(costs) != 1:
        return Attribution('MISSING_COST' if not costs else 'SOURCE_INCOMPLETE', 'EFFECTIVE_DATED_COST',
                           assignment.vendor_id, vendor.name if vendor else None, True)
    return Attribution('ATTRIBUTED', 'EFFECTIVE_DATED_ASSIGNMENT_AND_COST', assignment.vendor_id,
                       vendor.name if vendor else None, True, Decimal(str(costs[0].unit_cost)))


def _identity(db: Session, variation_id: str | None):
    return db.get(OrderingCatalogIdentity, variation_id) if variation_id else None


def _store_id(db: Session, location_id: str) -> int | None:
    return db.scalar(select(Store.id).where(Store.square_location_id == location_id))


def _return_exceeds_sale(
    db: Session,
    *,
    sale: ConsignmentSaleFact,
    quantity: Decimal,
    exclude_return_id: int | None = None,
) -> bool:
    query = select(func.coalesce(func.sum(ConsignmentReturnFact.quantity_returned), 0)).where(
        ConsignmentReturnFact.original_sale_fact_id == sale.id,
        ConsignmentReturnFact.attribution_status == 'ATTRIBUTED',
    )
    if exclude_return_id is not None:
        query = query.where(ConsignmentReturnFact.id != exclude_return_id)
    already_returned = _decimal(db.scalar(query))
    return quantity <= 0 or quantity + already_returned > _decimal(sale.quantity_sold)


@dataclass(frozen=True)
class ImportResult:
    orders: int
    sales_created: int
    returns_created: int
    existing: int
    unresolved: int


def import_square_orders(db: Session, *, orders: list[dict], synchronized_at: datetime | None = None) -> ImportResult:
    sync_at = synchronized_at or utc_now()
    sales_created = returns_created = existing_count = unresolved = 0
    for order in orders:
        order_id = str(order.get('id') or '').strip()
        location_id = str(order.get('location_id') or '').strip()
        transacted = _timestamp(order.get('closed_at') or order.get('updated_at') or order.get('created_at'))
        payment_id = next((str(t.get('payment_id')) for t in order.get('tenders', []) if t.get('payment_id')), None)
        for index, line in enumerate(order.get('line_items') or []):
            uid = str(line.get('uid') or f'index-{index}')
            existing = db.scalar(select(ConsignmentSaleFact).where(
                ConsignmentSaleFact.square_order_id == order_id,
                ConsignmentSaleFact.square_line_item_uid == uid,
            ))
            if existing:
                existing.source_synchronized_at = sync_at
                existing.source_order_version = order.get('version')
                if existing.attribution_status in BLOCKING_STATUSES:
                    enriched = attribution_at(db, variation_id=existing.square_variation_id,
                                               transacted_at=_utc(existing.transacted_at))
                    existing.vendor_id_snapshot = enriched.vendor_id
                    existing.vendor_name_snapshot = enriched.vendor_name
                    existing.is_consignment_snapshot = enriched.is_consignment
                    existing.unit_cost_snapshot = enriched.unit_cost
                    existing.extended_cogs_snapshot = (
                        money(existing.quantity_sold * enriched.unit_cost)
                        if enriched.unit_cost is not None else None
                    )
                    existing.attribution_status = (
                        'SOURCE_INCOMPLETE'
                        if (existing.store_id is None or existing.quantity_sold <= 0)
                        and enriched.status == 'ATTRIBUTED'
                        else enriched.status
                    )
                    existing.attribution_source = (
                        'SOURCE_LOCATION_UNMAPPED' if existing.store_id is None and enriched.status == 'ATTRIBUTED'
                        else enriched.source
                    )
                existing_count += 1
                continue
            variation_id = str(line.get('catalog_object_id') or '').strip() or None
            identity = _identity(db, variation_id)
            attribution = attribution_at(db, variation_id=variation_id, transacted_at=transacted)
            store_id = _store_id(db, location_id)
            qty = _decimal(line.get('quantity'))
            attribution_status = (
                'SOURCE_INCOMPLETE' if (store_id is None or qty <= 0) and attribution.status == 'ATTRIBUTED'
                else attribution.status
            )
            gross, currency = _money_object(line.get('gross_sales_money'))
            discount, _ = _money_object(line.get('total_discount_money'))
            tax, _ = _money_object(line.get('total_tax_money'))
            net, _ = _money_object(line.get('total_money'))
            extended = money(qty * attribution.unit_cost) if attribution.unit_cost is not None else None
            row = ConsignmentSaleFact(
                square_payment_id=payment_id, square_order_id=order_id, square_line_item_uid=uid,
                square_variation_id=variation_id, square_product_id=identity.square_item_id if identity else None,
                square_location_id=location_id, store_id=store_id, business_date=_business_date(transacted),
                transacted_at=transacted, quantity_sold=qty, gross_sales_amount=gross,
                discount_amount=discount, tax_amount=tax, net_sales_amount=net, currency=currency,
                product_name_snapshot=str(line.get('name') or (identity.product_name if identity else '') or 'Unknown product'),
                variation_name_snapshot=str(line.get('variation_name') or (identity.variation_name if identity else '') or '') or None,
                sku_snapshot=identity.sku if identity else None, vendor_id_snapshot=attribution.vendor_id,
                vendor_name_snapshot=attribution.vendor_name, is_consignment_snapshot=attribution.is_consignment,
                unit_cost_snapshot=attribution.unit_cost, extended_cogs_snapshot=extended,
                attribution_status=attribution_status,
                attribution_source=(attribution.source if attribution_status == attribution.status else 'SOURCE_LOCATION_UNMAPPED'),
                source_synchronized_at=sync_at, source_order_version=order.get('version'),
            )
            db.add(row); db.flush(); sales_created += 1
            unresolved += int(attribution_status in BLOCKING_STATUSES)

        for return_index, order_return in enumerate(order.get('returns') or []):
            return_uid = str(order_return.get('uid') or f'return-{return_index}')
            source_order_id = str(order_return.get('source_order_id') or '').strip() or None
            for line_index, line in enumerate(order_return.get('return_line_items') or []):
                line_uid = str(line.get('uid') or f'line-{line_index}')
                existing_return = db.scalar(select(ConsignmentReturnFact).where(
                    ConsignmentReturnFact.square_return_order_id == order_id,
                    ConsignmentReturnFact.square_return_uid == return_uid,
                    ConsignmentReturnFact.square_return_line_uid == line_uid,
                ))
                if existing_return:
                    existing_return.source_synchronized_at = sync_at
                    if existing_return.attribution_status in BLOCKING_STATUSES:
                        candidate = db.scalar(select(ConsignmentSaleFact).where(
                            ConsignmentSaleFact.square_order_id == existing_return.original_square_order_id,
                            ConsignmentSaleFact.square_line_item_uid == existing_return.original_square_line_uid,
                            ConsignmentSaleFact.attribution_status == 'ATTRIBUTED',
                        ))
                        if (candidate and existing_return.quantity_returned is not None
                                and not _return_exceeds_sale(
                                    db,
                                    sale=candidate,
                                    quantity=_decimal(existing_return.quantity_returned),
                                    exclude_return_id=existing_return.id,
                                )):
                            existing_return.original_sale_fact_id = candidate.id
                            existing_return.vendor_id_snapshot = candidate.vendor_id_snapshot
                            existing_return.vendor_name_snapshot = candidate.vendor_name_snapshot
                            existing_return.unit_cost_snapshot = candidate.unit_cost_snapshot
                            existing_return.extended_cogs_reversal = money(
                                existing_return.quantity_returned * candidate.unit_cost_snapshot)
                            existing_return.attribution_status = 'ATTRIBUTED'
                            existing_return.match_method = 'SOURCE_ORDER_AND_LINE_UID_RETRY'
                    existing_count += 1; continue
                source_line_uid = str(line.get('source_line_item_uid') or '').strip() or None
                sale = db.scalar(select(ConsignmentSaleFact).where(
                    ConsignmentSaleFact.square_order_id == source_order_id,
                    ConsignmentSaleFact.square_line_item_uid == source_line_uid,
                )) if source_order_id and source_line_uid else None
                qty = _decimal(line.get('quantity'))
                refund, currency = _money_object(line.get('total_money'))
                if sale and not _return_exceeds_sale(db, sale=sale, quantity=qty):
                    status = sale.attribution_status if sale.attribution_status != 'NON_CONSIGNMENT' else 'NON_CONSIGNMENT'
                    cost = Decimal(str(sale.unit_cost_snapshot)) if sale.unit_cost_snapshot is not None else None
                    reversal = money(qty * cost) if cost is not None else None
                    reason = None
                elif sale:
                    status = 'SOURCE_INCOMPLETE'; cost = reversal = None
                    reason = 'RETURN_QUANTITY_EXCEEDS_OR_INVALID_FOR_ORIGINAL_SALE'
                else:
                    status = 'UNMATCHED_RETURN'; cost = reversal = reason = None
                identity = _identity(db, str(line.get('catalog_object_id') or '').strip() or None)
                db.add(ConsignmentReturnFact(
                    square_return_order_id=order_id, square_return_uid=return_uid,
                    square_return_line_uid=line_uid, original_square_order_id=source_order_id,
                    original_square_line_uid=source_line_uid,
                    square_variation_id=sale.square_variation_id if sale else str(line.get('catalog_object_id') or '').strip() or None,
                    square_location_id=location_id, store_id=_store_id(db, location_id), business_date=_business_date(transacted),
                    returned_at=transacted, quantity_returned=qty, refund_amount=refund, currency=currency,
                    product_name_snapshot=sale.product_name_snapshot if sale else str(line.get('name') or 'Unknown return'),
                    variation_name_snapshot=sale.variation_name_snapshot if sale else (identity.variation_name if identity else None),
                    sku_snapshot=sale.sku_snapshot if sale else (identity.sku if identity else None),
                    vendor_id_snapshot=sale.vendor_id_snapshot if sale else None,
                    vendor_name_snapshot=sale.vendor_name_snapshot if sale else None,
                    unit_cost_snapshot=cost, extended_cogs_reversal=reversal,
                    attribution_status=status, original_sale_fact_id=sale.id if sale else None,
                    match_method='SOURCE_ORDER_AND_LINE_UID' if sale else None,
                    attribution_reason=reason, source_synchronized_at=sync_at,
                )); returns_created += 1; unresolved += int(status in BLOCKING_STATUSES)
        if not (order.get('returns') or []):
            for refund_index, refund in enumerate(order.get('refunds') or []):
                refund_id = str(refund.get('id') or f'refund-{refund_index}')
                if db.scalar(select(ConsignmentReturnFact.id).where(
                    ConsignmentReturnFact.square_return_order_id == order_id,
                    ConsignmentReturnFact.square_return_uid == refund_id,
                    ConsignmentReturnFact.square_return_line_uid == 'UNITEMIZED_REFUND',
                )):
                    existing_count += 1; continue
                refunded, currency = _money_object(refund.get('amount_money'))
                db.add(ConsignmentReturnFact(
                    square_return_order_id=order_id, square_return_uid=refund_id,
                    square_return_line_uid='UNITEMIZED_REFUND', square_location_id=location_id,
                    store_id=_store_id(db, location_id), business_date=_business_date(transacted),
                    returned_at=transacted, quantity_returned=None, refund_amount=refunded, currency=currency,
                    product_name_snapshot='Unitemized Square refund', attribution_status='SOURCE_INCOMPLETE',
                    source_synchronized_at=sync_at,
                ))
                returns_created += 1; unresolved += 1
    return ImportResult(len(orders), sales_created, returns_created, existing_count, unresolved)


class SquareOrdersReader:
    def __init__(self):
        if not settings.square_access_token:
            raise RuntimeError('SQUARE_ACCESS_TOKEN is required for consignment fact synchronization.')

    def search(self, *, location_ids: list[str], start_at: datetime, end_at: datetime):
        cursor = None
        while True:
            payload = {'location_ids': location_ids, 'limit': 500, 'return_entries': False,
                       'query': {'filter': {'date_time_filter': {'updated_at': {
                           'start_at': start_at.isoformat().replace('+00:00', 'Z'),
                           'end_at': end_at.isoformat().replace('+00:00', 'Z')}},
                           'state_filter': {'states': ['COMPLETED']}},
                           'sort': {'sort_field': 'UPDATED_AT', 'sort_order': 'ASC'}}}
            if cursor: payload['cursor'] = cursor
            request = Request(f"{settings.square_api_base_url.rstrip('/')}/v2/orders/search",
                              data=json.dumps(payload).encode(), method='POST', headers={
                                  'Authorization': f'Bearer {settings.square_access_token}',
                                  'Content-Type': 'application/json',
                                  **({'Square-Version': settings.square_api_version} if settings.square_api_version else {}),
                              })
            try:
                with urlopen(request, timeout=settings.square_timeout_seconds) as response:
                    result = json.loads(response.read().decode())
            except (HTTPError, URLError) as exc:
                raise RuntimeError(f'Square order synchronization failed: {exc}') from exc
            if result.get('errors'): raise RuntimeError(f"Square returned errors: {result['errors']}")
            yield result.get('orders') or []
            cursor = result.get('cursor')
            if not cursor: break


def synchronize_square_facts(db: Session, *, start_at: datetime, end_at: datetime, actor_id: int,
                             reader: SquareOrdersReader | None = None) -> ImportResult:
    state = db.get(ConsignmentSalesSyncState, 1)
    if state is None:
        state = ConsignmentSalesSyncState(id=1, updated_by_principal_id=actor_id); db.add(state)
    state.last_attempted_at = utc_now(); state.last_result = 'RUNNING'; state.last_error = None
    location_ids = list(db.scalars(select(Store.square_location_id).where(
        Store.active.is_(True), Store.square_location_id.is_not(None))).all())
    if not location_ids: raise ValueError('No active Square store locations are configured.')
    totals = ImportResult(0, 0, 0, 0, 0)
    try:
        with db.begin_nested():
            for orders in (reader or SquareOrdersReader()).search(location_ids=location_ids, start_at=start_at, end_at=end_at):
                page = import_square_orders(db, orders=orders)
                totals = ImportResult(*(getattr(totals, field) + getattr(page, field) for field in totals.__dataclass_fields__))
        state.last_successful_start_at = (
            min(_utc(state.last_successful_start_at), start_at) if state.last_successful_start_at else start_at
        )
        state.last_successful_through_at = (
            max(_utc(state.last_successful_through_at), end_at) if state.last_successful_through_at else end_at
        )
        state.last_successful_at = utc_now(); state.last_result = 'COMPLETE'; state.updated_by_principal_id = actor_id
        return totals
    except Exception as exc:
        state.last_result = 'FAILED'; state.last_error = str(exc)[:1000]
        raise


def _fact_finalized(db: Session, *, sale_id: int | None = None, return_id: int | None = None) -> bool:
    query = select(ConsignmentReportFactLink.id).join(
        ConsignmentReport, ConsignmentReport.id == ConsignmentReportFactLink.report_id
    ).where(ConsignmentReport.status.in_(('FINALIZED', 'EMAILED')))
    query = query.where(ConsignmentReportFactLink.sale_fact_id == sale_id) if sale_id else query.where(
        ConsignmentReportFactLink.return_fact_id == return_id)
    return db.scalar(query) is not None


def _mark_linked_drafts_stale(db: Session, *, sale_id: int | None = None, return_id: int | None = None) -> None:
    query = select(ConsignmentReport).join(
        ConsignmentReportFactLink, ConsignmentReportFactLink.report_id == ConsignmentReport.id
    ).where(ConsignmentReport.status.in_(('DRAFT', 'PREVIEWED')))
    query = query.where(ConsignmentReportFactLink.sale_fact_id == sale_id) if sale_id else query.where(
        ConsignmentReportFactLink.return_fact_id == return_id)
    for report in db.scalars(query).all():
        blockers = dict(report.data_integrity_blockers or {})
        codes = list(blockers.get('codes') or [])
        if 'FACT_CHANGED_REGENERATE_REQUIRED' not in codes:
            codes.append('FACT_CHANGED_REGENERATE_REQUIRED')
        blockers['codes'] = codes
        report.data_integrity_blockers = blockers


def resolve_sale_fact(db: Session, *, fact_id: int, vendor_id: int | None, unit_cost: Decimal | None,
                      disposition: str, reason: str, actor_id: int, ip=None):
    fact = db.get(ConsignmentSaleFact, fact_id)
    if fact is None: raise LookupError('Sale fact not found.')
    if _fact_finalized(db, sale_id=fact.id): raise ValueError('Facts included in a finalized report cannot be rewritten.')
    if not reason.strip(): raise ValueError('A correction reason is required.')
    before = {'status': fact.attribution_status, 'vendor_id': fact.vendor_id_snapshot,
              'unit_cost': str(fact.unit_cost_snapshot) if fact.unit_cost_snapshot is not None else None}
    if disposition in {'NON_CONSIGNMENT', 'EXCLUDED'}:
        fact.attribution_status = disposition; fact.is_consignment_snapshot = False
        fact.vendor_id_snapshot = vendor_id; fact.unit_cost_snapshot = None; fact.extended_cogs_snapshot = None
    else:
        vendor = db.get(Vendor, vendor_id) if vendor_id else None
        if vendor is None or unit_cost is None or _decimal(unit_cost) < 0:
            raise ValueError('Attributed facts require a valid vendor and non-negative historical cost.')
        fact.vendor_id_snapshot = vendor.id; fact.vendor_name_snapshot = vendor.name
        fact.is_consignment_snapshot = True; fact.unit_cost_snapshot = _decimal(unit_cost).quantize(Decimal('0.0001'))
        fact.extended_cogs_snapshot = money(fact.quantity_sold * fact.unit_cost_snapshot)
        fact.attribution_status = 'ATTRIBUTED'
    fact.attribution_source = 'OWNER_TRANSACTION_OVERRIDE'; fact.attribution_reason = reason.strip()
    fact.attributed_by_principal_id = actor_id; fact.attributed_at = utc_now()
    _mark_linked_drafts_stale(db, sale_id=fact.id)
    _audit(db, actor_id=actor_id, action='CONSIGNMENT_SALE_FACT_ATTRIBUTED', entity='consignment_sale_fact',
           entity_id=fact.id, before=before, after={'status': fact.attribution_status,
           'vendor_id': fact.vendor_id_snapshot, 'unit_cost': str(fact.unit_cost_snapshot)}, reason=reason, ip=ip)
    return fact


def resolve_return_fact(db: Session, *, fact_id: int, sale_fact_id: int | None, reason: str,
                        actor_id: int, disposition: str = 'ATTRIBUTED', ip=None):
    fact = db.get(ConsignmentReturnFact, fact_id)
    sale = db.get(ConsignmentSaleFact, sale_fact_id) if sale_fact_id else None
    if fact is None: raise LookupError('Return fact not found.')
    if _fact_finalized(db, return_id=fact.id): raise ValueError('Facts included in a finalized report cannot be rewritten.')
    if not reason.strip():
        raise ValueError('A correction reason is required.')
    before = {'status': fact.attribution_status, 'original_sale_fact_id': fact.original_sale_fact_id}
    if disposition in {'NON_CONSIGNMENT', 'EXCLUDED'}:
        fact.attribution_status = disposition
        fact.unit_cost_snapshot = None
        fact.extended_cogs_reversal = None
        fact.attribution_reason = reason.strip()
        fact.attributed_by_principal_id = actor_id
        fact.attributed_at = utc_now()
        _mark_linked_drafts_stale(db, return_id=fact.id)
        _audit(db, actor_id=actor_id, action='CONSIGNMENT_RETURN_FACT_ATTRIBUTED',
               entity='consignment_return_fact', entity_id=fact.id, before=before,
               after={'status': disposition}, reason=reason, ip=ip)
        return fact
    if sale is None:
        raise LookupError('Original sale fact not found.')
    if sale.attribution_status != 'ATTRIBUTED' or fact.quantity_returned is None:
        raise ValueError('A reason, attributed original sale, and itemized return quantity are required.')
    if _return_exceeds_sale(
        db,
        sale=sale,
        quantity=_decimal(fact.quantity_returned),
        exclude_return_id=fact.id,
    ):
        raise ValueError('Cumulative returns cannot exceed the original sale quantity.')
    fact.original_sale_fact_id = sale.id; fact.original_square_order_id = sale.square_order_id
    fact.original_square_line_uid = sale.square_line_item_uid; fact.square_variation_id = sale.square_variation_id
    fact.vendor_id_snapshot = sale.vendor_id_snapshot; fact.vendor_name_snapshot = sale.vendor_name_snapshot
    fact.unit_cost_snapshot = sale.unit_cost_snapshot
    fact.extended_cogs_reversal = money(fact.quantity_returned * fact.unit_cost_snapshot)
    fact.product_name_snapshot = sale.product_name_snapshot; fact.variation_name_snapshot = sale.variation_name_snapshot
    fact.sku_snapshot = sale.sku_snapshot; fact.attribution_status = 'ATTRIBUTED'; fact.match_method = 'OWNER_LINKED_SALE'
    fact.attribution_reason = reason.strip(); fact.attributed_by_principal_id = actor_id; fact.attributed_at = utc_now()
    _mark_linked_drafts_stale(db, return_id=fact.id)
    _audit(db, actor_id=actor_id, action='CONSIGNMENT_RETURN_FACT_ATTRIBUTED', entity='consignment_return_fact',
           entity_id=fact.id, before=before, after={'status': 'ATTRIBUTED', 'original_sale_fact_id': sale.id},
           reason=reason, ip=ip)
    return fact


def _portal_period(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if end_date < start_date or end_date > datetime.now(PORTAL_TIMEZONE).date():
        raise ValueError('Use a valid, non-future reporting period.')
    start = datetime.combine(start_date, time.min, PORTAL_TIMEZONE).astimezone(timezone.utc)
    end = datetime.combine(end_date + timedelta(days=1), time.min, PORTAL_TIMEZONE).astimezone(timezone.utc)
    return start, end


def automatic_report_start_date(db: Session, *, vendor_id: int) -> date | None:
    last_end = db.scalar(select(func.max(ConsignmentReport.end_at)).where(
        ConsignmentReport.vendor_id == vendor_id,
        ConsignmentReport.status.in_(('FINALIZED', 'EMAILED')),
    ))
    return _utc(last_end).astimezone(PORTAL_TIMEZONE).date() if last_end else None


def _ledger_balance_before(db: Session, *, vendor_id: int, before_at: datetime):
    rows = db.execute(select(ConsignmentLedgerEntry.entry_type,
        func.coalesce(func.sum(ConsignmentLedgerEntry.amount), 0)).where(
        ConsignmentLedgerEntry.vendor_id == vendor_id,
        ConsignmentLedgerEntry.effective_at < before_at,
    ).group_by(ConsignmentLedgerEntry.entry_type)).all()
    return calculate_consignment_balance({str(row.entry_type): money(row[1]) for row in rows})


def _ledger_period_totals(
    db: Session, *, vendor_id: int, start_at: datetime, end_at: datetime
) -> dict[str, Decimal]:
    rows = db.execute(select(
        ConsignmentLedgerEntry.entry_type,
        func.coalesce(func.sum(ConsignmentLedgerEntry.amount), 0),
    ).where(
        ConsignmentLedgerEntry.vendor_id == vendor_id,
        ConsignmentLedgerEntry.effective_at >= start_at,
        ConsignmentLedgerEntry.effective_at < end_at,
    ).group_by(ConsignmentLedgerEntry.entry_type)).all()
    return {str(entry_type): money(amount) for entry_type, amount in rows}


def generate_report(db: Session, *, vendor_id: int, start_date: date, end_date: date, actor_id: int, ip=None):
    vendor = db.get(Vendor, vendor_id)
    if vendor is None: raise LookupError('Vendor not found.')
    start_at, end_exclusive = _portal_period(start_date, end_date)
    prior_previews = db.scalars(select(ConsignmentReport).where(
        ConsignmentReport.vendor_id == vendor_id,
        ConsignmentReport.status.in_(('DRAFT', 'PREVIEWED')),
        ConsignmentReport.start_at == start_at,
        ConsignmentReport.end_at == end_exclusive,
    )).all()
    if prior_previews:
        prior_ids = [row.id for row in prior_previews]
        db.execute(delete(ConsignmentReportFactLink).where(
            ConsignmentReportFactLink.report_id.in_(prior_ids)))
        db.execute(delete(ConsignmentReportLine).where(
            ConsignmentReportLine.report_id.in_(prior_ids)))
        db.execute(delete(ConsignmentInventorySnapshot).where(
            ConsignmentInventorySnapshot.report_id.in_(prior_ids)))
        for prior_preview in prior_previews:
            db.delete(prior_preview)
        db.flush()
    overlap = db.scalar(select(ConsignmentReport.id).where(
        ConsignmentReport.vendor_id == vendor_id,
        ConsignmentReport.status.in_(('FINALIZED', 'EMAILED')),
        ConsignmentReport.start_at < end_exclusive,
        ConsignmentReport.end_at > start_at,
    ))
    state = db.get(ConsignmentSalesSyncState, 1)
    blockers = []
    if overlap: blockers.append('OVERLAPPING_FINALIZED_REPORT')
    if (state is None or state.last_result != 'COMPLETE' or not state.last_successful_through_at
            or _utc(state.last_successful_through_at) < end_exclusive):
        blockers.append('INCOMPLETE_SQUARE_SYNCHRONIZATION')
    if (state is None or not state.last_successful_at
            or _utc(state.last_successful_at) < end_exclusive + SQUARE_OFFLINE_LAG):
        blockers.append('SQUARE_OFFLINE_ORDER_LAG_WINDOW')
    unresolved_sales = db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.transacted_at >= start_at, ConsignmentSaleFact.transacted_at < end_exclusive,
        ConsignmentSaleFact.attribution_status.in_(BLOCKING_STATUSES),
    )).all()
    unresolved_returns = db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.returned_at >= start_at, ConsignmentReturnFact.returned_at < end_exclusive,
        ConsignmentReturnFact.attribution_status.in_(BLOCKING_STATUSES),
    )).all()
    blockers.extend(sorted({f'SALE_{row.attribution_status}' for row in unresolved_sales}))
    blockers.extend(sorted({f'RETURN_{row.attribution_status}' for row in unresolved_returns}))
    opening_balance = _ledger_balance_before(db, vendor_id=vendor_id, before_at=start_at)
    ending_balance_before_report = _ledger_balance_before(db, vendor_id=vendor_id, before_at=end_exclusive)
    period_ledger = _ledger_period_totals(
        db, vendor_id=vendor_id, start_at=start_at, end_at=end_exclusive
    )
    replenishment_period = period_ledger.get('REPLENISHMENT_APPLIED', Decimal('0.00'))
    cash_period = period_ledger.get('CASH_SETTLEMENT', Decimal('0.00'))
    approved_credit_period = period_ledger.get('APPROVED_CREDIT', Decimal('0.00'))
    void_reversal_period = period_ledger.get('VOID_REVERSAL', Decimal('0.00'))
    report = ConsignmentReport(
        vendor_id=vendor_id, report_number=f'COGS-{vendor_id}-{start_date:%Y%m%d}-{end_date:%Y%m%d}-{int(utc_now().timestamp())}',
        start_at=start_at, end_at=end_exclusive, status='PREVIEWED', total_units=0, total_cogs=0,
        inventory_quantity_snapshot=0, inventory_value_snapshot=0,
        source_sync_through_at=state.last_successful_through_at if state else None,
        prior_unreplenished_cogs_snapshot=opening_balance.unreplenished_cogs,
        replenishment_applied_period_snapshot=replenishment_period,
        cash_settlements_period_snapshot=cash_period,
        approved_credits_period_snapshot=approved_credit_period,
        void_reversals_period_snapshot=void_reversal_period,
        available_credit_snapshot=ending_balance_before_report.available_replenishment_credit,
        data_integrity_blockers={'codes': blockers, 'unresolved_sale_ids': [r.id for r in unresolved_sales],
                                 'unresolved_return_ids': [r.id for r in unresolved_returns]},
        created_by_principal_id=actor_id,
    )
    db.add(report); db.flush()
    sales = db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.vendor_id_snapshot == vendor_id, ConsignmentSaleFact.attribution_status == 'ATTRIBUTED',
        ConsignmentSaleFact.transacted_at >= start_at, ConsignmentSaleFact.transacted_at < end_exclusive,
    )).all()
    returns = db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.vendor_id_snapshot == vendor_id, ConsignmentReturnFact.attribution_status == 'ATTRIBUTED',
        ConsignmentReturnFact.returned_at >= start_at, ConsignmentReturnFact.returned_at < end_exclusive,
    )).all()
    groups: dict[tuple, dict] = {}
    for fact, is_return in [(row, False) for row in sales] + [(row, True) for row in returns]:
        cost = Decimal(str(fact.unit_cost_snapshot)); key = (fact.square_variation_id, fact.store_id, cost)
        group = groups.setdefault(key, {'product': fact.product_name_snapshot, 'variation': fact.variation_name_snapshot,
            'sku': fact.sku_snapshot, 'sold': Decimal('0'), 'returned': Decimal('0'), 'cogs': Decimal('0'), 'facts': []})
        if is_return:
            group['returned'] += fact.quantity_returned; group['cogs'] -= fact.extended_cogs_reversal
        else:
            group['sold'] += fact.quantity_sold; group['cogs'] += fact.extended_cogs_snapshot
        group['facts'].append((fact, is_return))
    for (variation_id, store_id, cost), group in groups.items():
        line = ConsignmentReportLine(report_id=report.id, square_variation_id=variation_id, store_id=store_id,
            product_name_snapshot=group['product'], variation_name_snapshot=group['variation'], sku_snapshot=group['sku'],
            units_sold=group['sold'], units_returned=group['returned'], net_units=group['sold'] - group['returned'],
            unit_cost_snapshot=cost, extended_cogs=money(group['cogs']), source_transaction_count=len(group['facts']))
        db.add(line); db.flush()
        for fact, is_return in group['facts']:
            db.add(ConsignmentReportFactLink(report_id=report.id, report_line_id=line.id,
                sale_fact_id=None if is_return else fact.id, return_fact_id=fact.id if is_return else None,
                cogs_amount_snapshot=money(-fact.extended_cogs_reversal if is_return else fact.extended_cogs_snapshot)))
        report.total_units += line.net_units; report.total_cogs += line.extended_cogs
    qty, value, inventory, warnings = inventory_snapshot(db, vendor_id=vendor_id)
    if warnings: report.data_integrity_blockers = {**report.data_integrity_blockers, 'inventory_warnings': warnings}
    snapshot_at = max((row['refreshed_at'] for row in inventory), default=utc_now())
    for row in inventory:
        db.add(ConsignmentInventorySnapshot(report_id=report.id, vendor_id=vendor_id,
            square_variation_id=row['variation_id'], store_id=row['store_id'], quantity_on_hand=row['quantity'],
            unit_cost_snapshot=row['unit_cost'], inventory_value_snapshot=row['value'],
            product_name_snapshot=row['product_name'], variation_name_snapshot=row['variation_name'],
            sku_snapshot=row['sku'], inventory_retrieved_at=row['refreshed_at'], attribution_status='ATTRIBUTED'))
    report.inventory_quantity_snapshot = qty; report.inventory_value_snapshot = value; report.inventory_snapshot_at = snapshot_at
    if report.total_cogs < 0:
        integrity = dict(report.data_integrity_blockers or {})
        integrity['codes'] = list(integrity.get('codes') or []) + ['NEGATIVE_PERIOD_COGS']
        report.data_integrity_blockers = integrity
    report.ending_unreplenished_cogs_snapshot = max(money(
        opening_balance.unreplenished_cogs
        + report.total_cogs
        - replenishment_period
        - cash_period
        - approved_credit_period
        - void_reversal_period
    ), Decimal('0.00'))
    _audit(db, actor_id=actor_id, action='CONSIGNMENT_REPORT_DRAFT_GENERATED', entity='consignment_report',
           entity_id=report.id, after={'vendor_id': vendor_id, 'total_cogs': str(report.total_cogs), 'blockers': blockers}, ip=ip)
    return report


def finalize_report(db: Session, *, report_id: int, actor_id: int, ip=None):
    report = db.get(ConsignmentReport, report_id)
    if report is None: raise LookupError('Report not found.')
    if report.status in {'FINALIZED', 'EMAILED'}: return report
    codes = (report.data_integrity_blockers or {}).get('codes') or []
    if codes: raise ValueError('Resolve all blocking facts and regenerate the preview before finalization.')
    state = db.get(ConsignmentSalesSyncState, 1)
    if (state is None or state.last_result != 'COMPLETE'
            or _utc(state.last_successful_through_at) < _utc(report.end_at)):
        raise ValueError('Square synchronization does not cover the report end date.')
    if not state.last_successful_at or _utc(state.last_successful_at) < _utc(report.end_at) + SQUARE_OFFLINE_LAG:
        raise ValueError('The 72-hour Square offline-order lag window has not been covered by a later sync.')
    linked = db.scalar(select(func.coalesce(func.sum(ConsignmentReportFactLink.cogs_amount_snapshot), 0)).where(
        ConsignmentReportFactLink.report_id == report.id))
    if money(linked) != money(report.total_cogs): raise ValueError('Report total does not reconcile to immutable fact links.')
    if db.scalar(select(ConsignmentReport.id).where(
        ConsignmentReport.id != report.id, ConsignmentReport.vendor_id == report.vendor_id,
        ConsignmentReport.status.in_(('FINALIZED', 'EMAILED')),
        ConsignmentReport.start_at < report.end_at, ConsignmentReport.end_at > report.start_at)):
        raise ValueError('The report period overlaps another finalized report.')
    ledger = db.scalar(select(ConsignmentLedgerEntry).where(
        ConsignmentLedgerEntry.report_id == report.id, ConsignmentLedgerEntry.entry_type == 'COGS_GENERATED'))
    if ledger is None:
        db.add(ConsignmentLedgerEntry(vendor_id=report.vendor_id, entry_type='COGS_GENERATED',
            effective_at=report.end_at, amount=money(report.total_cogs), quantity=report.total_units,
            report_id=report.id, note=f'Immutable report {report.report_number}', created_by_principal_id=actor_id))
    report.status = 'FINALIZED'; report.finalized_at = utc_now(); report.finalized_by_principal_id = actor_id
    _audit(db, actor_id=actor_id, action='CONSIGNMENT_REPORT_FINALIZED', entity='consignment_report',
           entity_id=report.id, after={'total_cogs': str(report.total_cogs), 'sync_through': str(report.source_sync_through_at)}, ip=ip)
    return report


def void_report(db: Session, *, report_id: int, reason: str, actor_id: int, ip=None):
    report = db.get(ConsignmentReport, report_id)
    if report is None: raise LookupError('Report not found.')
    if report.status not in {'FINALIZED', 'EMAILED'} or not reason.strip():
        raise ValueError('Only a finalized report can be voided, with a reason.')
    original = db.scalar(select(ConsignmentLedgerEntry).where(
        ConsignmentLedgerEntry.report_id == report.id,
        ConsignmentLedgerEntry.entry_type == 'COGS_GENERATED',
    ))
    if original is None:
        raise ValueError('The finalized report has no original COGS ledger entry to reverse.')
    reversal = db.scalar(select(ConsignmentLedgerEntry).where(
        ConsignmentLedgerEntry.report_id == report.id,
        ConsignmentLedgerEntry.entry_type == 'VOID_REVERSAL',
    ))
    if reversal is None:
        reversal = ConsignmentLedgerEntry(vendor_id=report.vendor_id, entry_type='VOID_REVERSAL',
            effective_at=utc_now(), amount=money(report.total_cogs), quantity=report.total_units,
            report_id=report.id, note=reason.strip(), created_by_principal_id=actor_id)
        db.add(reversal)
        db.flush()
    report.status = 'VOIDED'; report.voided_at = utc_now(); report.voided_by_principal_id = actor_id; report.void_reason = reason.strip()
    _audit(db, actor_id=actor_id, action='CONSIGNMENT_REPORT_VOIDED', entity='consignment_report',
           entity_id=report.id,
           before={'status': 'FINALIZED', 'original_ledger_entry_id': original.id},
           after={'status': 'VOIDED', 'reversal_ledger_entry_id': reversal.id},
           reason=reason, ip=ip)
    return report


def capture_test_email(db: Session, *, report_id: int, actor_id: int, ip=None):
    report = db.get(ConsignmentReport, report_id)
    vendor = db.get(Vendor, report.vendor_id) if report else None
    vendor_settings = db.get(VendorPaymentSetting, report.vendor_id) if report else None
    if report is None or vendor is None or report.status not in {'FINALIZED', 'EMAILED'}:
        raise ValueError('Only finalized reports can generate captured test email.')
    recipient = (vendor_settings.report_email or '').strip() if vendor_settings else ''
    if not recipient or '@' not in recipient:
        raise ValueError('The vendor profile requires a valid report email before capture.')
    subject = f'[TEST CAPTURE] Consignment COGS Report – {vendor.name} – {report.start_at.date()} through {(report.end_at - timedelta(microseconds=1)).date()}'
    body = (f'TEST DELIVERY — NO EXTERNAL EMAIL SENT\nVendor: {vendor.name}\nReporting period: '
            f'{report.start_at.date()} through {(report.end_at - timedelta(microseconds=1)).date()}\n'
            f'Current-period COGS: ${money(report.total_cogs):,.2f}\n'
            f'Opening unreplenished COGS: ${money(report.prior_unreplenished_cogs_snapshot):,.2f}\n'
            f'Replenishment applied: ${money(report.replenishment_applied_period_snapshot):,.2f}\n'
            f'Cash settlements: ${money(report.cash_settlements_period_snapshot):,.2f}\n'
            f'Approved credits: ${money(report.approved_credits_period_snapshot):,.2f}\n'
            f'Void reversals: ${money(report.void_reversals_period_snapshot):,.2f}\n'
            f'Closing unreplenished COGS: ${money(report.ending_unreplenished_cogs_snapshot):,.2f}\n'
            f'Available replenishment credit: ${money(report.available_credit_snapshot):,.2f}\n'
            f'Inventory snapshot at: {report.inventory_snapshot_at}\n'
            f'Current inventory quantity: {report.inventory_quantity_snapshot}\n'
            f'Current inventory value: ${money(report.inventory_value_snapshot):,.2f}')
    row = ConsignmentEmailDelivery(report_id=report.id, recipient=recipient, subject=subject,
        provider_message_id=None, status='CAPTURED_TEST', body_snapshot=body,
        error_summary=None, sent_at=utc_now(),
        sent_by_principal_id=actor_id)
    db.add(row); db.flush(); report.status = 'EMAILED'
    _audit(db, actor_id=actor_id, action='CONSIGNMENT_TEST_EMAIL_CAPTURED', entity='consignment_email_delivery',
           entity_id=row.id, after={'report_id': report.id, 'recipient': recipient, 'subject': subject}, ip=ip)
    return row
