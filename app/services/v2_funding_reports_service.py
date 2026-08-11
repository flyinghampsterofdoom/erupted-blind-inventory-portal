from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    ConsignmentReturnFact,
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    FundingAccount,
    FundingLedgerEntry,
    FundingPayment,
    FundingPaymentAllocation,
    FundingReport,
    FundingReportAdjustment,
    FundingReportExclusion,
    FundingReportFactLink,
    FundingReportLine,
    FundingSkuMapping,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    OrderPayment,
    PaymentMethod,
    Principal,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptLine,
    PurchaseOrderStoreAllocation,
    PurchaseOrderStatus,
    Store,
    Vendor,
)

CENT = Decimal('0.01')
PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')
ADJUSTMENT_TYPES = {
    'SHIPPING', 'TAX', 'VENDOR_FEE', 'CARD_FEE', 'VENDOR_CREDIT', 'DAMAGE_CREDIT',
    'PROMOTIONAL_CREDIT', 'MISCELLANEOUS_CHARGE', 'MISCELLANEOUS_CREDIT', 'OTHER',
}
LEDGER_TYPES = {
    'OPENING_BALANCE', 'INVENTORY_PURCHASE', 'INTEREST', 'FEE', 'MANUAL_CHARGE',
    'PAYMENT', 'REPLENISHMENT', 'CREDIT', 'CORRECTION', 'REVERSAL',
}


def money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def normalize_sku(value: object) -> str:
    return re.sub(r'\s+', '', str(value or '').strip()).upper()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def funding_report_source_readiness(
    db: Session, *, start_date: date, end_date: date
) -> dict:
    """Return the persisted Square coverage required for a financial report."""
    start_at = datetime.combine(start_date, time.min, PORTAL_TIMEZONE).astimezone(timezone.utc)
    end_at = datetime.combine(
        end_date + timedelta(days=1), time.min, PORTAL_TIMEZONE
    ).astimezone(timezone.utc)
    state = db.get(ConsignmentSalesSyncState, 1)
    blockers = []
    if state is None or state.last_result != 'COMPLETE':
        blockers.append('SQUARE_SYNC_NOT_COMPLETE')
    if state is None or state.last_successful_start_at is None or _utc(state.last_successful_start_at) > start_at:
        blockers.append('SQUARE_SYNC_START_GAP')
    if state is None or state.last_successful_through_at is None or _utc(state.last_successful_through_at) < end_at:
        blockers.append('SQUARE_SYNC_END_GAP')
    return {
        'blockers': blockers,
        'period_start_at': start_at,
        'period_end_at': end_at,
        'last_successful_start_at': state.last_successful_start_at if state else None,
        'last_successful_through_at': state.last_successful_through_at if state else None,
        'last_successful_at': state.last_successful_at if state else None,
    }


def assert_funding_report_source_ready(
    db: Session, *, start_date: date, end_date: date
) -> dict:
    readiness = funding_report_source_readiness(
        db, start_date=start_date, end_date=end_date)
    if readiness['blockers']:
        raise ValueError(
            'Square sales data is not complete for this reporting period. '
            'Update Square Data for the full period, then calculate the report again. '
            f"Blocked by: {', '.join(readiness['blockers'])}."
        )
    return readiness


def _source_readiness_snapshot(readiness: dict) -> dict:
    return {
        'blockers': list(readiness['blockers']),
        'period_start_at': readiness['period_start_at'].isoformat(),
        'period_end_at': readiness['period_end_at'].isoformat(),
        'last_successful_start_at': _utc(readiness['last_successful_start_at']).isoformat(),
        'last_successful_through_at': _utc(readiness['last_successful_through_at']).isoformat(),
        'last_successful_at': _utc(readiness['last_successful_at']).isoformat(),
    }


def _audit(db: Session, *, actor_id: int, action: str, entity_type: str, entity_id: int, after: dict, ip=None) -> None:
    db.add(AuditLog(
        actor_principal_id=actor_id,
        action=action,
        ip=ip,
        meta={'entity_type': entity_type, 'entity_id': entity_id, 'after': after},
    ))


def create_funding_account(
    db: Session,
    *,
    account_type: str,
    vendor_id: int | None,
    payment_method_id: int | None,
    display_name: str,
    actor_id: int,
    issuer: str = '',
    account_nickname: str = '',
    last_four: str = '',
    internal_notes: str = '',
    ip=None,
) -> FundingAccount:
    account_type = account_type.strip().upper()
    if account_type not in {'CONSIGNMENT', 'CREDIT_CARD'}:
        raise ValueError('Choose Consignment or Credit Card.')
    name = display_name.strip()
    if not name:
        raise ValueError('Account display name is required.')
    if account_type == 'CONSIGNMENT':
        vendor = db.get(Vendor, vendor_id)
        if vendor is None or not vendor.active:
            raise ValueError('Choose an active Consignment vendor.')
        if db.scalar(select(FundingAccount).where(FundingAccount.vendor_id == vendor.id)):
            raise ValueError('That Consignment account already exists.')
        payment_method_id = None
        last_four = ''
    else:
        method = db.get(PaymentMethod, payment_method_id)
        if method is None or method.category != 'CREDIT_CARD':
            raise ValueError('Choose a configured Credit Card payment method.')
        if db.scalar(select(FundingAccount).where(FundingAccount.payment_method_id == method.id)):
            raise ValueError('That Credit Card account already exists.')
        vendor_id = None
        issuer = issuer.strip() or str(method.institution_or_company_name or '')
        account_nickname = account_nickname.strip() or str(method.account_nickname or '')
        last_four = last_four.strip() or str(method.last_four or '')
        if last_four and (len(last_four) != 4 or not last_four.isdigit()):
            raise ValueError('Store only the final four card digits.')
    row = FundingAccount(
        account_type=account_type,
        vendor_id=vendor_id,
        payment_method_id=payment_method_id,
        display_name=name,
        issuer=issuer.strip() or None,
        account_nickname=account_nickname.strip() or None,
        last_four=last_four or None,
        internal_notes=internal_notes.strip() or None,
        created_by_principal_id=actor_id,
        updated_by_principal_id=actor_id,
    )
    db.add(row)
    db.flush()
    _audit(db, actor_id=actor_id, action='FUNDING_ACCOUNT_CREATED', entity_type='funding_account',
           entity_id=row.id, after={'account_type': row.account_type, 'display_name': row.display_name}, ip=ip)
    return row


@dataclass(frozen=True)
class FundingAccountVendorMembership:
    vendor: Vendor
    assigned_po_count: int


def funding_account_vendor_memberships(
    db: Session, *, account: FundingAccount
) -> list[FundingAccountVendorMembership]:
    """Derive account vendor membership from authoritative PO assignments."""
    if not account.is_active:
        return []
    if account.account_type == 'CONSIGNMENT':
        vendor = db.get(Vendor, account.vendor_id)
        if vendor is None:
            return []
        assigned_po_count = db.scalar(
            select(func.count(func.distinct(PurchaseOrder.id)))
            .join(OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id)
            .where(
                OrderPayment.vendor_id == account.vendor_id,
                OrderPayment.financial_treatment == 'REPLENISHMENT',
            )
        ) or 0
        return [FundingAccountVendorMembership(
            vendor=vendor,
            assigned_po_count=int(assigned_po_count),
        )]
    if account.account_type != 'CREDIT_CARD' or account.payment_method_id is None:
        return []

    rows = db.execute(
        select(Vendor, func.count(func.distinct(PurchaseOrder.id)))
        .join(PurchaseOrder, PurchaseOrder.vendor_id == Vendor.id)
        .join(OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id)
        .where(
            OrderPayment.payment_method_id == account.payment_method_id,
        )
        .group_by(Vendor.id)
        .order_by(func.lower(Vendor.name), Vendor.id)
    ).all()
    return [FundingAccountVendorMembership(
        vendor=vendor,
        assigned_po_count=int(assigned_po_count),
    ) for vendor, assigned_po_count in rows]


def eligible_vendors_for_account(db: Session, *, account: FundingAccount) -> list[Vendor]:
    """Return PO-assigned vendors eligible for report/payment scope."""
    return [membership.vendor for membership in funding_account_vendor_memberships(
        db, account=account
    )]


def resolve_account_vendor(
    db: Session, *, account: FundingAccount, vendor_id: int | None, purpose: str = 'report'
) -> Vendor:
    eligible = eligible_vendors_for_account(db, account=account)
    if account.account_type == 'CONSIGNMENT':
        if not eligible:
            raise ValueError('Choose a valid Consignment funding account.')
        if vendor_id is not None and vendor_id != eligible[0].id:
            raise ValueError('The selected vendor is not valid for this funding account.')
        return eligible[0]
    if vendor_id is None:
        if purpose == 'payment':
            raise ValueError('Select a vendor before recording this payment.')
        raise ValueError('Select a vendor before generating this report.')
    for vendor in eligible:
        if vendor.id == vendor_id:
            return vendor
    raise ValueError('The selected vendor has no purchase order assigned to this credit card account.')


def update_credit_terms(
    db: Session,
    *,
    account_id: int,
    credit_limit: Decimal | None,
    promotional_apr: Decimal | None,
    promotional_start_date: date | None,
    promotional_expiration_date: date | None,
    standard_apr: Decimal | None,
    internal_notes: str,
    actor_id: int,
    ip=None,
) -> FundingAccount:
    account = db.get(FundingAccount, account_id)
    if account is None or account.account_type != 'CREDIT_CARD':
        raise LookupError('Credit Card account not found.')
    for label, value in (('Credit limit', credit_limit), ('Promotional APR', promotional_apr), ('Standard APR', standard_apr)):
        if value is not None and Decimal(str(value)) < 0:
            raise ValueError(f'{label} cannot be negative.')
    if promotional_expiration_date and promotional_start_date and promotional_expiration_date < promotional_start_date:
        raise ValueError('Promotion expiration must be on or after its start date.')
    account.credit_limit = money(credit_limit) if credit_limit is not None else None
    account.promotional_apr = Decimal(str(promotional_apr)) if promotional_apr is not None else None
    account.promotional_start_date = promotional_start_date
    account.promotional_expiration_date = promotional_expiration_date
    account.standard_apr = Decimal(str(standard_apr)) if standard_apr is not None else None
    account.internal_notes = internal_notes.strip() or None
    account.updated_by_principal_id = actor_id
    _audit(db, actor_id=actor_id, action='FUNDING_ACCOUNT_TERMS_UPDATED', entity_type='funding_account',
           entity_id=account.id, after={'promotional_apr': str(account.promotional_apr or ''),
           'promotional_expiration_date': str(account.promotional_expiration_date or ''),
           'standard_apr': str(account.standard_apr or '')}, ip=ip)
    return account


def set_funding_account_status(
    db: Session, *, account_id: int, is_active: bool, reason: str, actor_id: int, ip=None
) -> FundingAccount:
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise LookupError('Funding account not found.')
    if not reason.strip():
        raise ValueError('A reason is required.')
    account.is_active = is_active
    account.updated_by_principal_id = actor_id
    _audit(db, actor_id=actor_id, action='FUNDING_ACCOUNT_STATUS_CHANGED', entity_type='funding_account',
           entity_id=account.id, after={'is_active': is_active, 'reason': reason.strip()}, ip=ip)
    return account


def catalog_rows(db: Session) -> list[dict]:
    mappings = db.scalars(select(FundingSkuMapping).order_by(
        FundingSkuMapping.normalized_sku, FundingSkuMapping.effective_start_date.desc())).all()
    current_by_sku: dict[str, list[FundingSkuMapping]] = defaultdict(list)
    today = date.today()
    for row in mappings:
        if row.status == 'ACTIVE' and row.effective_start_date <= today and (
            row.effective_end_date is None or row.effective_end_date >= today
        ):
            current_by_sku[row.normalized_sku].append(row)
    accounts = {row.id: row for row in db.scalars(select(FundingAccount)).all()}
    return [{
        'identity': identity,
        'normalized_sku': normalize_sku(identity.sku),
        'mappings': current_by_sku.get(normalize_sku(identity.sku), []),
        'account_names': [accounts[row.account_id].display_name for row in current_by_sku.get(normalize_sku(identity.sku), []) if row.account_id in accounts],
    } for identity in db.scalars(select(OrderingCatalogIdentity).where(
        OrderingCatalogIdentity.square_is_deleted.is_(False),
        OrderingCatalogIdentity.sku.is_not(None),
    ).order_by(OrderingCatalogIdentity.product_name, OrderingCatalogIdentity.variation_name)).all()]


def bulk_assign_skus(
    db: Session,
    *,
    account_id: int,
    skus: list[str],
    effective_date: date,
    unit_cost: Decimal,
    reason: str,
    actor_id: int,
    ip=None,
) -> list[FundingSkuMapping]:
    account = db.get(FundingAccount, account_id)
    if account is None or not account.is_active:
        raise ValueError('Choose an active funding account.')
    cost = Decimal(str(unit_cost))
    if cost < 0:
        raise ValueError('Effective cost cannot be negative.')
    note = reason.strip()
    if not note:
        raise ValueError('A reason is required.')
    normalized = sorted({normalize_sku(value) for value in skus if normalize_sku(value)})
    if not normalized:
        raise ValueError('Select at least one SKU.')
    identities = db.scalars(select(OrderingCatalogIdentity).where(
        OrderingCatalogIdentity.sku.is_not(None))).all()
    identity_by_sku: dict[str, list[OrderingCatalogIdentity]] = defaultdict(list)
    for identity in identities:
        identity_by_sku[normalize_sku(identity.sku)].append(identity)
    created = []
    for sku in normalized:
        matches = identity_by_sku.get(sku, [])
        if not matches:
            raise ValueError(f'SKU {sku} is not present in the Square catalog.')
        existing = db.scalars(select(FundingSkuMapping).where(
            FundingSkuMapping.normalized_sku == sku,
            FundingSkuMapping.status == 'ACTIVE',
            FundingSkuMapping.effective_start_date < effective_date,
            or_(FundingSkuMapping.effective_end_date.is_(None), FundingSkuMapping.effective_end_date >= effective_date),
        )).all()
        for prior in existing:
            prior.effective_end_date = effective_date - timedelta(days=1)
        identity = sorted(matches, key=lambda row: row.square_variation_id)[0]
        row = FundingSkuMapping(
            account_id=account.id,
            normalized_sku=sku,
            sku_snapshot=str(identity.sku),
            square_variation_id=identity.square_variation_id,
            product_name_snapshot=identity.product_name or identity.item_name,
            variation_name_snapshot=identity.variation_name,
            effective_start_date=effective_date,
            unit_cost=cost,
            status='ACTIVE',
            reason=note,
            created_by_principal_id=actor_id,
        )
        db.add(row)
        db.flush()
        created.append(row)
    _audit(db, actor_id=actor_id, action='FUNDING_SKUS_ASSIGNED', entity_type='funding_account',
           entity_id=account.id, after={'normalized_skus': normalized, 'effective_date': str(effective_date),
           'unit_cost': str(cost)}, ip=ip)
    return created


def overlapping_reports(
    db: Session, *, account_id: int, start_date: date, end_date: date,
    vendor_id: int | None = None,
) -> list[FundingReport]:
    query = select(FundingReport).where(
        FundingReport.account_id == account_id,
        FundingReport.status != 'VOIDED',
        FundingReport.sales_start_date <= end_date,
        FundingReport.sales_end_date >= start_date,
    )
    if vendor_id is not None:
        query = query.where(FundingReport.vendor_id == vendor_id)
    return db.scalars(query.order_by(FundingReport.sales_start_date, FundingReport.id)).all()


def _active_mappings(
    db: Session, *, account_id: int, normalized_sku: str, business_date: date
) -> list[FundingSkuMapping]:
    return db.scalars(select(FundingSkuMapping).where(
        FundingSkuMapping.account_id == account_id,
        FundingSkuMapping.normalized_sku == normalized_sku,
        FundingSkuMapping.status == 'ACTIVE',
        FundingSkuMapping.effective_start_date <= business_date,
        or_(FundingSkuMapping.effective_end_date.is_(None), FundingSkuMapping.effective_end_date >= business_date),
    )).all()


def _period_account_mappings(
    db: Session, *, account_id: int, start_date: date, end_date: date
) -> list[FundingSkuMapping]:
    """Return the selected account's mappings that overlap the requested period.

    This is the hard report boundary. Sale and return facts are not considered until
    this account-scoped set has been loaded and validated.
    """
    return db.scalars(select(FundingSkuMapping).where(
        FundingSkuMapping.account_id == account_id,
        FundingSkuMapping.status == 'ACTIVE',
        FundingSkuMapping.effective_start_date <= end_date,
        or_(FundingSkuMapping.effective_end_date.is_(None), FundingSkuMapping.effective_end_date >= start_date),
    ).order_by(FundingSkuMapping.normalized_sku, FundingSkuMapping.effective_start_date)).all()


def _mapping_periods_overlap(left: FundingSkuMapping, right: FundingSkuMapping) -> bool:
    left_end = left.effective_end_date or date.max
    right_end = right.effective_end_date or date.max
    return left.effective_start_date <= right_end and right.effective_start_date <= left_end


def _validate_account_mapping_boundary(
    db: Session, *, account_id: int, start_date: date, end_date: date
) -> tuple[list[FundingSkuMapping], set[str]]:
    mappings = _period_account_mappings(
        db, account_id=account_id, start_date=start_date, end_date=end_date)
    if not mappings:
        raise ValueError('No SKUs are mapped to this funding account for the selected period.')
    if any(row.unit_cost is None for row in mappings):
        raise ValueError('Some mapped SKUs need an effective cost before this report can be completed.')
    skus = {row.normalized_sku for row in mappings}
    if any(
        left.id != right.id
        and left.normalized_sku == right.normalized_sku
        and _mapping_periods_overlap(left, right)
        for index, left in enumerate(mappings) for right in mappings[index + 1:]
    ):
        raise ValueError('Some mapped SKUs have conflicting effective dates for this funding account.')
    other_mappings = db.scalars(select(FundingSkuMapping).where(
        FundingSkuMapping.account_id != account_id,
        FundingSkuMapping.normalized_sku.in_(skus),
        FundingSkuMapping.status == 'ACTIVE',
        FundingSkuMapping.effective_start_date <= end_date,
        or_(FundingSkuMapping.effective_end_date.is_(None), FundingSkuMapping.effective_end_date >= start_date),
    )).all()
    if any(
        selected.normalized_sku == other.normalized_sku
        and _mapping_periods_overlap(selected, other)
        for selected in mappings for other in other_mappings
    ):
        raise ValueError('Some mapped SKUs are assigned to multiple funding accounts for the selected period.')
    return mappings, skus


def _normalized_sku_expression(column, *, dialect_name: str):
    value = func.coalesce(column, '')
    if dialect_name == 'postgresql':
        return func.upper(func.regexp_replace(value, r'\s+', '', 'g'))
    # SQLite test parity for the whitespace emitted by Square/catalog snapshots.
    for whitespace in (' ', '\t', '\n', '\r'):
        value = func.replace(value, whitespace, '')
    return func.upper(value)


QUALIFYING_ORDER_STATUSES = (
    PurchaseOrderStatus.IN_TRANSIT,
    PurchaseOrderStatus.RECEIVED_SPLIT_PENDING,
    PurchaseOrderStatus.SENT_TO_STORES,
    PurchaseOrderStatus.COMPLETED,
)


def _purchase_order_date(order: PurchaseOrder) -> date:
    timestamp = order.ordered_at or order.submitted_at or order.created_at
    return timestamp.date()


def _consignment_order_scope(db: Session, *, account: FundingAccount) -> dict:
    if account.account_type != 'CONSIGNMENT' or account.vendor_id is None:
        raise ValueError('Choose a valid Consignment funding account.')
    order_rows = db.execute(select(PurchaseOrder, OrderPayment).join(
        OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id
    ).where(
        OrderPayment.vendor_id == account.vendor_id,
        OrderPayment.financial_treatment == 'REPLENISHMENT',
        OrderPayment.payment_category_snapshot == 'CONSIGNMENT',
        PurchaseOrder.status.in_(QUALIFYING_ORDER_STATUSES),
    ).order_by(PurchaseOrder.ordered_at, PurchaseOrder.id)).all()
    if not order_rows:
        raise ValueError('No purchase-order SKUs are assigned to this Consignment account.')
    orders = {int(order.id): (order, payment) for order, payment in order_rows}
    lines = db.scalars(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id.in_(orders),
        PurchaseOrderLine.removed.is_(False),
        PurchaseOrderLine.ordered_qty > 0,
    ).order_by(PurchaseOrderLine.purchase_order_id, PurchaseOrderLine.id)).all()
    source_lines = []
    setup_issues = []
    cost_sources: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        order, payment = orders[int(line.purchase_order_id)]
        sku = normalize_sku(line.sku)
        source = {
            'purchase_order_id': int(order.id),
            'purchase_order_number': f'PO #{order.id}',
            'purchase_order_line_id': int(line.id),
            'sku': str(line.sku or ''),
            'normalized_sku': sku,
            'product': line.item_name,
            'variation': line.variation_name,
            'ordered_quantity': int(line.ordered_qty),
            'unit_cost': str(line.unit_cost) if line.unit_cost is not None else None,
            'cost_effective_date': str(_purchase_order_date(order)),
            'original_vendor_id': int(order.vendor_id),
            'financial_vendor_id': int(payment.vendor_id),
            'financial_account': account.display_name,
        }
        source_lines.append(source)
        if not sku:
            setup_issues.append({**source, 'issue': 'Missing SKU'})
            continue
        if line.unit_cost is None:
            setup_issues.append({**source, 'issue': 'Missing saved cost'})
            continue
        cost_sources[sku].append({**source, 'line': line, 'order_date': _purchase_order_date(order)})
    eligible_skus = set(cost_sources)
    if not eligible_skus:
        raise ValueError('No purchase-order SKUs are assigned to this Consignment account.')
    for sku in cost_sources:
        cost_sources[sku].sort(key=lambda row: (row['order_date'], row['purchase_order_line_id']))
    return {
        'orders': orders,
        'eligible_skus': eligible_skus,
        'cost_sources': cost_sources,
        'source_lines': source_lines,
        'setup_issues': setup_issues,
    }


@dataclass
class _FundingInventoryLot:
    order: PurchaseOrder
    line: PurchaseOrderLine
    payment: OrderPayment | None
    account_id: int | None
    receipt_line_id: int | None
    received_at: datetime
    quantity: Decimal
    remaining: Decimal

    @property
    def key(self) -> tuple[int, int | None, str]:
        source = 'RECEIPT' if self.receipt_line_id is not None else 'LEGACY'
        return int(self.line.id), self.receipt_line_id, source


def _order_timestamp(order: PurchaseOrder) -> datetime:
    return _utc(order.ordered_at or order.submitted_at or order.created_at)


def _store_receipt_evidence(
    db: Session, *, line_ids: list[int]
) -> dict[int, tuple[Decimal, datetime | None]]:
    if not line_ids:
        return {}
    rows = db.execute(
        select(
            PurchaseOrderStoreAllocation.purchase_order_line_id,
            func.coalesce(func.sum(PurchaseOrderStoreAllocation.store_received_qty), 0),
            func.max(PurchaseOrderStoreAllocation.updated_at),
        )
        .where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids))
        .group_by(PurchaseOrderStoreAllocation.purchase_order_line_id)
    ).all()
    return {
        int(line_id): (Decimal(str(received or 0)), received_at)
        for line_id, received, received_at in rows
    }


def _received_quantity(
    line: PurchaseOrderLine,
    receipt_evidence: dict[int, tuple[Decimal, datetime | None]],
) -> tuple[Decimal, datetime | None]:
    line_quantity = Decimal(str(line.received_qty_total or 0))
    store_quantity, received_at = receipt_evidence.get(
        int(line.id), (Decimal('0'), None)
    )
    quantity = max(line_quantity, store_quantity)
    evidence_timestamp = received_at if store_quantity >= line_quantity and store_quantity > 0 else None
    return quantity, evidence_timestamp


def _credit_card_fifo_scope(
    db: Session, *, account: FundingAccount, vendor: Vendor
) -> dict:
    """Build received PO lots using current owner-entered payment assignments."""
    assigned_order_rows = db.execute(select(
        PurchaseOrder, OrderPayment
    ).join(
        OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id
    ).where(
        OrderPayment.payment_method_id == account.payment_method_id,
        PurchaseOrder.vendor_id == vendor.id,
        PurchaseOrder.status.in_(QUALIFYING_ORDER_STATUSES),
    ).order_by(PurchaseOrder.ordered_at, PurchaseOrder.id)).all()
    if not assigned_order_rows:
        raise ValueError(
            'No purchase orders are assigned to this Funding Account and vendor.'
        )
    assigned_orders = {
        int(order.id): (order, payment) for order, payment in assigned_order_rows
    }
    assigned_rows = db.execute(select(
        PurchaseOrder, OrderPayment, PurchaseOrderLine
    ).join(
        OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id
    ).join(
        PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id
    ).where(
        PurchaseOrder.id.in_(assigned_orders),
        PurchaseOrderLine.removed.is_(False),
        PurchaseOrderLine.ordered_qty > 0,
    ).order_by(PurchaseOrder.ordered_at, PurchaseOrder.id, PurchaseOrderLine.id)).all()
    if not assigned_rows:
        raise ValueError(
            'No purchased PO lines are assigned to this Funding Account and vendor.'
        )

    assigned_receipt_evidence = _store_receipt_evidence(
        db, line_ids=[int(line.id) for _order, _payment, line in assigned_rows]
    )
    source_lines = []
    setup_issues = []
    eligible_variations = set()
    for order, payment, line in assigned_rows:
        received_quantity, _received_at = _received_quantity(
            line, assigned_receipt_evidence
        )
        source = {
            'purchase_order_id': int(order.id),
            'purchase_order_number': f'PO #{order.id}',
            'purchase_order_line_id': int(line.id),
            'square_variation_id': str(line.variation_id or '').strip(),
            'sku': str(line.sku or ''),
            'normalized_sku': normalize_sku(line.sku),
            'product': line.item_name,
            'variation': line.variation_name,
            'ordered_quantity': int(line.ordered_qty),
            'received_quantity': str(received_quantity),
            'unit_cost': str(line.unit_cost) if line.unit_cost is not None else None,
            'cost_effective_date': str(_purchase_order_date(order)),
            'original_vendor_id': int(order.vendor_id),
            'financial_vendor_id': int(vendor.id),
            'financial_account': account.display_name,
        }
        source_lines.append(source)
        if received_quantity <= 0:
            setup_issues.append({**source, 'issue': 'Not received'})
            continue
        if not source['square_variation_id']:
            setup_issues.append({**source, 'issue': 'Missing Square variation ID'})
            continue
        if line.unit_cost is None:
            setup_issues.append({**source, 'issue': 'Missing saved cost'})
            continue
        eligible_variations.add(source['square_variation_id'])

    blocking_issues = [
        row for row in setup_issues
        if row['issue'] in {'Missing Square variation ID', 'Missing saved cost'}
    ]
    if blocking_issues:
        line_ids = ', '.join(str(row['purchase_order_line_id']) for row in blocking_issues)
        raise ValueError(
            'Assigned received PO lines have missing Square identity or cost and cannot be '
            f'allocated safely. Review PO line(s): {line_ids}.'
        )
    if not eligible_variations:
        raise ValueError(
            'No received purchase-order inventory is assigned to this Funding Account and vendor.'
        )

    global_candidates = db.execute(select(
        PurchaseOrderLine, PurchaseOrder, OrderPayment
    ).join(
        PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id
    ).outerjoin(
        OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id
    ).where(
        PurchaseOrderLine.variation_id.in_(eligible_variations),
        PurchaseOrderLine.removed.is_(False),
        PurchaseOrder.status.in_(QUALIFYING_ORDER_STATUSES),
    ).order_by(PurchaseOrder.ordered_at, PurchaseOrder.id, PurchaseOrderLine.id)).all()
    global_receipt_evidence = _store_receipt_evidence(
        db, line_ids=[int(line.id) for line, _order, _payment in global_candidates]
    )
    global_rows = [
        row for row in global_candidates
        if _received_quantity(row[0], global_receipt_evidence)[0] > 0
    ]
    line_rows = {int(line.id): (line, order, payment) for line, order, payment in global_rows}
    account_by_payment_method = {
        int(row.payment_method_id): int(row.id)
        for row in db.scalars(select(FundingAccount).where(
            FundingAccount.account_type == 'CREDIT_CARD',
            FundingAccount.payment_method_id.is_not(None),
        )).all()
    }
    receipt_rows: dict[int, list[tuple[PurchaseOrderReceipt, PurchaseOrderReceiptLine]]] = defaultdict(list)
    if line_rows:
        for receipt, receipt_line in db.execute(select(
            PurchaseOrderReceipt, PurchaseOrderReceiptLine
        ).join(
            PurchaseOrderReceiptLine,
            PurchaseOrderReceiptLine.receipt_id == PurchaseOrderReceipt.id,
        ).where(
            PurchaseOrderReceipt.status == 'SUBMITTED',
            PurchaseOrderReceiptLine.purchase_order_line_id.in_(line_rows),
            PurchaseOrderReceiptLine.received_qty > 0,
        ).order_by(
            PurchaseOrderReceipt.received_at,
            PurchaseOrderReceipt.id,
            PurchaseOrderReceiptLine.id,
        )):
            receipt_rows[int(receipt_line.purchase_order_line_id)].append((receipt, receipt_line))

    lots: list[_FundingInventoryLot] = []
    for line, order, payment in global_rows:
        available, store_received_at = _received_quantity(
            line, global_receipt_evidence
        )
        payment_method_id = int(payment.payment_method_id) if payment and payment.payment_method_id else None
        lot_account_id = account_by_payment_method.get(payment_method_id)
        for receipt, receipt_line in receipt_rows.get(int(line.id), []):
            if available <= 0:
                break
            quantity = min(available, Decimal(str(receipt_line.received_qty)))
            if quantity <= 0:
                continue
            received_at = _utc(receipt.received_at) if receipt.received_at else _order_timestamp(order)
            lots.append(_FundingInventoryLot(
                order=order, line=line, payment=payment, account_id=lot_account_id,
                receipt_line_id=int(receipt_line.id), received_at=received_at,
                quantity=quantity, remaining=quantity,
            ))
            available -= quantity
        if available > 0:
            lots.append(_FundingInventoryLot(
                order=order, line=line, payment=payment, account_id=lot_account_id,
                receipt_line_id=None,
                received_at=(
                    _utc(store_received_at)
                    if store_received_at is not None
                    else _order_timestamp(order)
                ),
                quantity=available, remaining=available,
            ))
    lots.sort(key=lambda row: (
        row.received_at, int(row.order.id), int(row.line.id), row.receipt_line_id or 0
    ))
    if not lots:
        raise ValueError('No received purchase-order lots are available for FIFO allocation.')
    return {
        'assigned_orders': assigned_orders,
        'eligible_variations': eligible_variations,
        'source_lines': source_lines,
        'setup_issues': setup_issues,
        'lots': lots,
        'fifo_start_date': min(
            row.received_at.astimezone(PORTAL_TIMEZONE).date() for row in lots
        ),
    }


def funding_report_required_coverage_start(
    db: Session, *, account: FundingAccount, vendor: Vendor, requested_start: date
) -> date:
    if account.account_type != 'CREDIT_CARD':
        return requested_start
    scope = _credit_card_fifo_scope(db, account=account, vendor=vendor)
    return min(requested_start, scope['fifo_start_date'])


def _catalog_matches_for_sku(
    db: Session, *, sku: object
) -> list[OrderingCatalogIdentity]:
    normalized = normalize_sku(sku)
    if not normalized:
        return []
    rows = db.scalars(select(OrderingCatalogIdentity).where(
        OrderingCatalogIdentity.square_is_deleted.is_(False),
        OrderingCatalogIdentity.sku.is_not(None),
    ).order_by(OrderingCatalogIdentity.square_variation_id)).all()
    return [row for row in rows if normalize_sku(row.sku) == normalized]


def resolve_assigned_po_line_identities(
    db: Session, *, account: FundingAccount, vendor: Vendor | None = None,
    actor_id: int, ip=None,
) -> list[PurchaseOrderLine]:
    """Persist only unambiguous catalog identities on currently assigned PO lines."""
    if account.account_type != 'CREDIT_CARD' or account.payment_method_id is None:
        return []
    db.flush()
    query = select(PurchaseOrderLine, PurchaseOrder).join(
        PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id
    ).join(
        OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id
    ).where(
        OrderPayment.payment_method_id == account.payment_method_id,
        PurchaseOrderLine.removed.is_(False),
        PurchaseOrderLine.ordered_qty > 0,
        or_(PurchaseOrderLine.variation_id.is_(None), PurchaseOrderLine.variation_id == ''),
    )
    if vendor is not None:
        query = query.where(PurchaseOrder.vendor_id == vendor.id)
    resolved = []
    for line, order in db.execute(query.order_by(PurchaseOrder.id, PurchaseOrderLine.id)):
        matches = _catalog_matches_for_sku(db, sku=line.sku)
        if len(matches) != 1:
            continue
        identity = matches[0]
        line.variation_id = identity.square_variation_id
        resolved.append(line)
        _audit(
            db,
            actor_id=actor_id,
            action='FUNDING_PO_LINE_IDENTITY_RESOLVED',
            entity_type='purchase_order_line',
            entity_id=int(line.id),
            after={
                'funding_account_id': int(account.id),
                'purchase_order_id': int(order.id),
                'square_variation_id': identity.square_variation_id,
                'resolution': 'UNIQUE_SKU_MATCH',
            },
            ip=ip,
        )
    if resolved:
        db.flush()
    return resolved


def resolve_funding_po_line_identity(
    db: Session, *, account_id: int, purchase_order_line_id: int,
    square_variation_id: str, reason: str, actor_id: int, ip=None,
) -> PurchaseOrderLine:
    """Apply an explicit owner identity repair to a line assigned to this account."""
    account = db.get(FundingAccount, account_id)
    line = db.get(PurchaseOrderLine, purchase_order_line_id)
    if account is None or account.account_type != 'CREDIT_CARD' or line is None:
        raise LookupError('Assigned purchase-order line not found.')
    order = db.get(PurchaseOrder, line.purchase_order_id)
    payment = db.scalar(select(OrderPayment).where(
        OrderPayment.purchase_order_id == line.purchase_order_id,
    ))
    if (
        order is None
        or payment is None
        or payment.payment_method_id != account.payment_method_id
    ):
        raise ValueError('That PO line is not assigned to this Funding Account.')
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError('A reason is required for an explicit identity repair.')
    variation_id = square_variation_id.strip()
    identity = db.scalar(select(OrderingCatalogIdentity).where(
        OrderingCatalogIdentity.square_variation_id == variation_id,
        OrderingCatalogIdentity.square_is_deleted.is_(False),
    ))
    if identity is None:
        raise ValueError('Choose a current Square catalog variation.')
    prior = str(line.variation_id or '')
    line.variation_id = identity.square_variation_id
    _audit(
        db,
        actor_id=actor_id,
        action='FUNDING_PO_LINE_IDENTITY_RESOLVED',
        entity_type='purchase_order_line',
        entity_id=int(line.id),
        after={
            'funding_account_id': int(account.id),
            'purchase_order_id': int(order.id),
            'prior_square_variation_id': prior or None,
            'square_variation_id': identity.square_variation_id,
            'resolution': 'OWNER_OVERRIDE',
            'reason': clean_reason,
        },
        ip=ip,
    )
    db.flush()
    return line


def _apply_fifo_inventory_history(
    db: Session, *, scope: dict, account: FundingAccount, vendor: Vendor,
    through_date: date,
) -> tuple[list[dict], list[dict]]:
    """Consume cached Square events and return per-line funded inventory positions."""
    variations = scope['eligible_variations']
    sales = db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.business_date >= scope['fifo_start_date'],
        ConsignmentSaleFact.business_date <= through_date,
        ConsignmentSaleFact.square_variation_id.in_(variations),
    ).order_by(ConsignmentSaleFact.transacted_at, ConsignmentSaleFact.id)).all()
    returns = db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.business_date >= scope['fifo_start_date'],
        ConsignmentReturnFact.business_date <= through_date,
        ConsignmentReturnFact.square_variation_id.in_(variations),
    ).order_by(ConsignmentReturnFact.returned_at, ConsignmentReturnFact.id)).all()
    events = [(row.transacted_at, 0, int(row.id), row, False) for row in sales]
    events += [(row.returned_at, 1, int(row.id), row, True) for row in returns]
    events.sort(key=lambda row: (_utc(row[0]), row[1], row[2]))
    lots_by_variation: dict[str, list[_FundingInventoryLot]] = defaultdict(list)
    for lot in scope['lots']:
        lots_by_variation[str(lot.line.variation_id)].append(lot)
    sale_allocations: dict[int, list[dict]] = defaultdict(list)
    allocation_history: dict[str, list[dict]] = defaultdict(list)
    unallocated = []
    for event_at, _event_type, _event_id, fact, is_return in events:
        variation_id = str(fact.square_variation_id or '').strip()
        remaining = Decimal(str(
            fact.quantity_returned if is_return else fact.quantity_sold
        ))
        if remaining <= 0:
            continue
        candidates = []
        if is_return:
            candidates = list(sale_allocations.get(int(fact.original_sale_fact_id or 0), []))
            if not candidates:
                candidates = list(allocation_history.get(variation_id, []))
            for original in reversed(candidates):
                if remaining <= 0:
                    break
                if original['returnable'] <= 0:
                    continue
                quantity = min(remaining, original['returnable'])
                original['returnable'] -= quantity
                original['lot'].remaining += quantity
                remaining -= quantity
        else:
            for lot in lots_by_variation.get(variation_id, []):
                if remaining <= 0:
                    break
                if lot.received_at > _utc(event_at) or lot.remaining <= 0:
                    continue
                quantity = min(remaining, lot.remaining)
                lot.remaining -= quantity
                allocation = {'lot': lot, 'quantity': quantity, 'returnable': quantity}
                sale_allocations[int(fact.id)].append(allocation)
                allocation_history[variation_id].append(allocation)
                remaining -= quantity
        if remaining > 0:
            unallocated.append({
                'source_type': 'RETURN' if is_return else 'SALE',
                'source_id': int(fact.id),
                'square_variation_id': variation_id,
                'quantity': str(remaining),
                'business_date': str(fact.business_date),
            })
    positions = []
    for lot in scope['lots']:
        if lot.account_id != account.id or int(lot.order.vendor_id) != vendor.id:
            continue
        unit_cost = Decimal(str(lot.line.unit_cost))
        sold = lot.quantity - lot.remaining
        positions.append({
            'lot': lot,
            'original_units': lot.quantity,
            'original_value': money(lot.quantity * unit_cost),
            'remaining_units': lot.remaining,
            'remaining_value': money(lot.remaining * unit_cost),
            'sold_units': sold,
            'sold_cogs': money(sold * unit_cost),
        })
    return positions, unallocated


def credit_card_inventory_summary(db: Session, *, account: FundingAccount) -> dict:
    """Derive funded inventory directly from current PO financial assignments."""
    if account.account_type != 'CREDIT_CARD' or account.payment_method_id is None:
        return {
            'vendors': [], 'lines': [], 'issues': [], 'original_units': Decimal('0'),
            'original_value': Decimal('0'), 'remaining_units': None,
            'remaining_value': None, 'sold_units': None, 'sold_cogs': None,
            'assigned_po_count': 0, 'as_of': None, 'history_blockers': [],
        }
    memberships = funding_account_vendor_memberships(db, account=account)
    raw_rows = db.execute(select(
        PurchaseOrder, PurchaseOrderLine, Vendor
    ).join(
        OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id
    ).join(
        PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id
    ).join(
        Vendor, Vendor.id == PurchaseOrder.vendor_id
    ).where(
        OrderPayment.payment_method_id == account.payment_method_id,
        PurchaseOrder.status.in_(QUALIFYING_ORDER_STATUSES),
        PurchaseOrderLine.removed.is_(False),
        PurchaseOrderLine.ordered_qty > 0,
    ).order_by(func.lower(Vendor.name), PurchaseOrder.id, PurchaseOrderLine.id)).all()
    raw_receipt_evidence = _store_receipt_evidence(
        db, line_ids=[int(line.id) for _order, line, _vendor in raw_rows]
    )
    lines = []
    issues = []
    original_units = Decimal('0')
    original_value = Decimal('0')
    for order, line, vendor in raw_rows:
        received, _received_at = _received_quantity(line, raw_receipt_evidence)
        candidates = [] if str(line.variation_id or '').strip() else _catalog_matches_for_sku(
            db, sku=line.sku
        )
        issue = None
        if received > 0 and not str(line.variation_id or '').strip():
            issue = 'Product identity unresolved' if len(candidates) != 1 else 'Unique SKU identity awaiting resolution'
        if received > 0 and line.unit_cost is None:
            issue = 'Missing saved cost' if issue is None else f'{issue}; missing saved cost'
        row = {
            'purchase_order_id': int(order.id),
            'purchase_order_line_id': int(line.id),
            'vendor': vendor,
            'sku': str(line.sku or ''),
            'product': line.item_name,
            'variation': line.variation_name,
            'square_variation_id': str(line.variation_id or '').strip() or None,
            'received_units': received,
            'unit_cost': Decimal(str(line.unit_cost)) if line.unit_cost is not None else None,
            'original_value': money(received * Decimal(str(line.unit_cost))) if line.unit_cost is not None else None,
            'remaining_units': None,
            'remaining_value': None,
            'sold_units': None,
            'sold_cogs': None,
            'issue': issue,
            'resolution_candidates': candidates,
        }
        lines.append(row)
        if received > 0:
            original_units += received
            if row['original_value'] is not None:
                original_value += row['original_value']
        if issue:
            issues.append(row)

    state = db.get(ConsignmentSalesSyncState, 1)
    through_at = _utc(state.last_successful_through_at) if (
        state is not None and state.last_result == 'COMPLETE'
        and state.last_successful_through_at is not None
    ) else None
    as_of = (through_at - timedelta(microseconds=1)).astimezone(PORTAL_TIMEZONE).date() if through_at else None
    positions_by_line: dict[int, list[dict]] = defaultdict(list)
    history_complete = as_of is not None
    history_blockers = [] if as_of is not None else [
        'Square coverage is unavailable; remaining funded inventory cannot be calculated.'
    ]
    for membership in memberships:
        vendor = membership.vendor
        try:
            scope = _credit_card_fifo_scope(db, account=account, vendor=vendor)
        except ValueError as exc:
            history_complete = False
            history_blockers.append(f'{vendor.name}: {exc}')
            continue
        if (
            as_of is None
            or state.last_successful_start_at is None
            or _utc(state.last_successful_start_at) > datetime.combine(
                scope['fifo_start_date'], time.min, PORTAL_TIMEZONE
            ).astimezone(timezone.utc)
        ):
            history_complete = False
            history_blockers.append(
                f'{vendor.name}: Square coverage does not reach the earliest FIFO lot; '
                'remaining funded inventory is unavailable.'
            )
            continue
        positions, unallocated = _apply_fifo_inventory_history(
            db, scope=scope, account=account, vendor=vendor, through_date=as_of
        )
        if unallocated:
            history_complete = False
            history_blockers.append(
                f'{vendor.name}: FIFO transaction history is incomplete; remaining '
                'funded inventory is unavailable.'
            )
            issues.append({
                'purchase_order_id': None,
                'purchase_order_line_id': None,
                'vendor': vendor,
                'issue': 'FIFO history incomplete; remaining inventory is unavailable',
                'resolution_candidates': [],
            })
            continue
        for position in positions:
            positions_by_line[int(position['lot'].line.id)].append(position)
    for row in lines:
        positions = positions_by_line.get(row['purchase_order_line_id'], [])
        if not positions:
            continue
        row['remaining_units'] = sum((item['remaining_units'] for item in positions), Decimal('0'))
        row['remaining_value'] = money(sum((item['remaining_value'] for item in positions), Decimal('0')))
        row['sold_units'] = sum((item['sold_units'] for item in positions), Decimal('0'))
        row['sold_cogs'] = money(sum((item['sold_cogs'] for item in positions), Decimal('0')))
    return {
        'vendors': memberships,
        'lines': lines,
        'issues': issues,
        'original_units': original_units,
        'original_value': money(original_value),
        'remaining_units': (
            sum((row['remaining_units'] for row in lines if row['remaining_units'] is not None), Decimal('0'))
            if history_complete else None
        ),
        'remaining_value': (
            money(sum((row['remaining_value'] for row in lines if row['remaining_value'] is not None), Decimal('0')))
            if history_complete else None
        ),
        'sold_units': (
            sum((row['sold_units'] for row in lines if row['sold_units'] is not None), Decimal('0'))
            if history_complete else None
        ),
        'sold_cogs': (
            money(sum((row['sold_cogs'] for row in lines if row['sold_cogs'] is not None), Decimal('0')))
            if history_complete else None
        ),
        'assigned_po_count': len({int(order.id) for order, _line, _vendor in raw_rows}),
        'as_of': as_of,
        'history_blockers': list(dict.fromkeys(history_blockers)),
    }


def _fact_matches_report_filter(
    fact, *, start_date: date, end_date: date, store_ids: list[int],
    filter_text: str, normalized_filter: str, product_filter: str,
) -> bool:
    if not (start_date <= fact.business_date <= end_date):
        return False
    if store_ids and fact.store_id not in store_ids:
        return False
    if not filter_text:
        return True
    sku = normalize_sku(fact.sku_snapshot)
    product_text = (
        f'{fact.product_name_snapshot or ""} {fact.variation_name_snapshot or ""}'.casefold()
    )
    return sku == normalized_filter or product_filter in product_text


def _populate_credit_card_fifo_report(
    db: Session, *, report: FundingReport, account: FundingAccount, vendor: Vendor,
    scope: dict, start_date: date, end_date: date, store_ids: list[int],
    filter_text: str, normalized_filter: str, product_filter: str,
) -> dict:
    variations = scope['eligible_variations']
    sales = db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.business_date >= scope['fifo_start_date'],
        ConsignmentSaleFact.business_date <= end_date,
        ConsignmentSaleFact.square_variation_id.in_(variations),
    ).order_by(ConsignmentSaleFact.transacted_at, ConsignmentSaleFact.id)).all()
    returns = db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.business_date >= scope['fifo_start_date'],
        ConsignmentReturnFact.business_date <= end_date,
        ConsignmentReturnFact.square_variation_id.in_(variations),
    ).order_by(ConsignmentReturnFact.returned_at, ConsignmentReturnFact.id)).all()
    events = [(row.transacted_at, 0, int(row.id), row, False) for row in sales]
    events += [(row.returned_at, 1, int(row.id), row, True) for row in returns]
    events.sort(key=lambda row: (_utc(row[0]), row[1], row[2]))
    lots_by_variation: dict[str, list[_FundingInventoryLot]] = defaultdict(list)
    for lot in scope['lots']:
        lots_by_variation[str(lot.line.variation_id)].append(lot)

    sale_allocations: dict[int, list[dict]] = defaultdict(list)
    allocation_history: dict[str, list[dict]] = defaultdict(list)
    included_allocations = []
    unallocated_history = []
    for event_at, _event_type, _event_id, fact, is_return in events:
        variation_id = str(fact.square_variation_id or '').strip()
        quantity = Decimal(str(
            fact.quantity_returned if is_return else fact.quantity_sold
        ))
        if quantity <= 0:
            if is_return and start_date <= fact.business_date <= end_date:
                raise ValueError(
                    f'Return fact {fact.id} has no usable quantity and cannot be allocated safely.'
                )
            continue
        event_time = _utc(event_at)
        allocations = []
        remaining = quantity
        if not is_return:
            for lot in lots_by_variation.get(variation_id, []):
                if remaining <= 0:
                    break
                if lot.received_at > event_time or lot.remaining <= 0:
                    continue
                allocated = min(remaining, lot.remaining)
                lot.remaining -= allocated
                row = {'lot': lot, 'quantity': allocated, 'returnable': allocated}
                allocations.append(row)
                sale_allocations[int(fact.id)].append(row)
                allocation_history[variation_id].append(row)
                remaining -= allocated
        else:
            candidates = list(sale_allocations.get(int(fact.original_sale_fact_id or 0), []))
            if not candidates:
                candidates = list(allocation_history.get(variation_id, []))
            for original in reversed(candidates):
                if remaining <= 0:
                    break
                if original['returnable'] <= 0:
                    continue
                reversed_quantity = min(remaining, original['returnable'])
                original['returnable'] -= reversed_quantity
                original['lot'].remaining += reversed_quantity
                allocations.append({'lot': original['lot'], 'quantity': reversed_quantity})
                remaining -= reversed_quantity
        if remaining > 0:
            unallocated_history.append({
                'source_type': 'RETURN' if is_return else 'SALE',
                'source_id': int(fact.id),
                'variation_id': variation_id,
                'quantity': str(remaining),
                'business_date': str(fact.business_date),
            })
            if start_date <= fact.business_date <= end_date:
                raise ValueError(
                    'FIFO inventory history is incomplete: '
                    f'{remaining} unit(s) for Square variation {variation_id} on '
                    f'{fact.business_date} could not be allocated to a received PO lot.'
                )
        if not _fact_matches_report_filter(
            fact, start_date=start_date, end_date=end_date, store_ids=store_ids,
            filter_text=filter_text, normalized_filter=normalized_filter,
            product_filter=product_filter,
        ):
            continue
        for allocation in allocations:
            lot = allocation['lot']
            if lot.account_id != account.id or int(lot.order.vendor_id) != vendor.id:
                continue
            included_allocations.append({
                'lot': lot,
                'fact': fact,
                'is_return': is_return,
                'quantity': allocation['quantity'],
            })

    groups: dict[tuple[tuple[int, int | None, str], int | None], dict] = {}
    reconciliation = []
    for allocation in included_allocations:
        lot = allocation['lot']
        fact = allocation['fact']
        is_return = allocation['is_return']
        quantity = allocation['quantity']
        key = (lot.key, fact.store_id)
        group = groups.setdefault(key, {
            'lot': lot, 'store_id': fact.store_id,
            'product': fact.product_name_snapshot or lot.line.item_name,
            'variation': fact.variation_name_snapshot or lot.line.variation_name,
            'sku': fact.sku_snapshot or lot.line.sku or '',
            'sold': Decimal('0'), 'returned': Decimal('0'), 'links': {},
        })
        group['returned' if is_return else 'sold'] += quantity
        link_key = ('RETURN' if is_return else 'SALE', int(fact.id))
        link = group['links'].setdefault(link_key, {
            'fact': fact, 'is_return': is_return,
            'quantity': Decimal('0'), 'cogs': Decimal('0'),
        })
        link['quantity'] += quantity
        signed_cogs = money(quantity * Decimal(str(lot.line.unit_cost)))
        link['cogs'] += -signed_cogs if is_return else signed_cogs
        reconciliation.append({
            'purchase_order_id': int(lot.order.id),
            'purchase_order_line_id': int(lot.line.id),
            'purchase_order_receipt_line_id': lot.receipt_line_id,
            'square_variation_id': str(lot.line.variation_id),
            'source_type': 'RETURN' if is_return else 'SALE',
            'source_id': int(fact.id),
            'business_date': str(fact.business_date),
            'quantity': str(quantity),
            'unit_cost': str(lot.line.unit_cost),
            'cogs': str(-signed_cogs if is_return else signed_cogs),
        })

    inventory_recorded_for_lot = set()
    for (_lot_key, _store_id), group in groups.items():
        lot = group['lot']
        unit_cost = Decimal(str(lot.line.unit_cost))
        net = group['sold'] - group['returned']
        inventory_quantity = Decimal('0')
        inventory_value = Decimal('0')
        if lot.key not in inventory_recorded_for_lot:
            inventory_recorded_for_lot.add(lot.key)
            inventory_quantity = lot.remaining
            inventory_value = money(lot.remaining * unit_cost)
        line = FundingReportLine(
            report_id=report.id,
            mapping_id=None,
            purchase_order_line_id=int(lot.line.id),
            purchase_order_receipt_line_id=lot.receipt_line_id,
            lot_received_at_snapshot=lot.received_at,
            normalized_sku=normalize_sku(lot.line.sku) or str(lot.line.variation_id),
            sku_snapshot=group['sku'] or str(lot.line.variation_id),
            square_variation_id=str(lot.line.variation_id),
            product_name_snapshot=group['product'],
            variation_name_snapshot=group['variation'],
            store_id=group['store_id'],
            units_sold=group['sold'],
            units_returned=group['returned'],
            net_units=net,
            unit_cost_snapshot=unit_cost,
            extended_cogs=money(net * unit_cost),
            inventory_units_snapshot=inventory_quantity,
            inventory_value_snapshot=inventory_value,
            mapping_effective_date_snapshot=lot.received_at.astimezone(PORTAL_TIMEZONE).date(),
            source_transaction_count=len(group['links']),
            warning_state=f'PO_LINE:{lot.line.id}',
        )
        db.add(line)
        db.flush()
        for link in group['links'].values():
            fact = link['fact']
            db.add(FundingReportFactLink(
                report_id=report.id,
                report_line_id=line.id,
                sale_fact_id=None if link['is_return'] else fact.id,
                return_fact_id=fact.id if link['is_return'] else None,
                allocated_quantity=link['quantity'],
                cogs_amount_snapshot=money(link['cogs']),
            ))
        report.units_sold += group['sold']
        report.units_returned += group['returned']
        report.net_units += net
        report.calculated_cogs += line.extended_cogs
        report.inventory_units_snapshot += inventory_quantity
        report.inventory_value_snapshot += inventory_value
    report.inventory_snapshot_at = datetime.now(timezone.utc)
    return {
        'message': (
            'This credit-card report uses received FIFO lots from purchase orders '
            'assigned to this Funding Account and vendor.'
        ),
        'purchase_order_ids': sorted(scope['assigned_orders']),
        'assigned_purchase_order_count': len(scope['assigned_orders']),
        'eligible_skus': sorted(scope['eligible_variations']),
        'eligible_sku_count': len(scope['eligible_variations']),
        'source_lines': scope['source_lines'],
        'setup_issues': scope['setup_issues'],
        'allocation_method': 'FIFO',
        'lot_ordering': (
            'Submitted receipt received_at; legacy received quantities fall back to '
            'purchase-order ordered/submitted/created timestamp.'
        ),
        'fifo_history_start_date': str(scope['fifo_start_date']),
        'fifo_allocations': reconciliation,
        'unallocated_history': unallocated_history,
    }


def _purchase_order_cost_source(scope: dict, *, normalized_sku: str, business_date: date) -> dict | None:
    candidates = [row for row in scope['cost_sources'].get(normalized_sku, [])
                  if row['order_date'] <= business_date]
    return candidates[-1] if candidates else None


def _inventory_for_sku(
    db: Session, *, normalized_sku: str, unit_cost: Decimal, store_id: int | None
) -> tuple[Decimal, Decimal, datetime | None]:
    identities = db.scalars(select(OrderingCatalogIdentity).where(OrderingCatalogIdentity.sku.is_not(None))).all()
    variation_ids = [row.square_variation_id for row in identities if normalize_sku(row.sku) == normalized_sku]
    query = select(OrderingCurrentInventory).where(
        OrderingCurrentInventory.square_variation_id.in_(variation_ids or ['__none__']))
    if store_id is not None:
        query = query.where(OrderingCurrentInventory.store_id == store_id)
    rows = db.scalars(query).all()
    quantity = sum((Decimal(str(row.counted_quantity)) for row in rows), Decimal('0'))
    value = money(quantity * Decimal(str(unit_cost)))
    return quantity, value, max((row.refreshed_at for row in rows), default=None)


def calculate_report(
    db: Session,
    *,
    account_id: int,
    start_date: date,
    end_date: date,
    store_ids: list[int],
    sku_filter: str,
    internal_note: str,
    overlap_acknowledged: bool,
    actor_id: int,
    vendor_id: int | None = None,
    ip=None,
) -> FundingReport:
    if end_date < start_date or end_date > date.today():
        raise ValueError('Choose a valid, non-future sales period.')
    if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id <= 0:
        raise ValueError('A valid funding account ID is required.')
    account = db.get(FundingAccount, account_id)
    if account is None or not account.is_active:
        raise ValueError('Choose an active funding account.')
    vendor = resolve_account_vendor(db, account=account, vendor_id=vendor_id)
    if account.account_type == 'CREDIT_CARD':
        resolve_assigned_po_line_identities(
            db, account=account, vendor=vendor, actor_id=actor_id, ip=ip
        )
    order_scope = None
    credit_card_scope = None
    credit_card_order_ids: list[int] = []
    if account.account_type == 'CONSIGNMENT':
        order_scope = _consignment_order_scope(db, account=account)
        account_skus = order_scope['eligible_skus']
        coverage_start_date = start_date
    else:
        credit_card_scope = _credit_card_fifo_scope(
            db, account=account, vendor=vendor)
        account_skus = credit_card_scope['eligible_variations']
        credit_card_order_ids = sorted(credit_card_scope['assigned_orders'])
        coverage_start_date = min(start_date, credit_card_scope['fifo_start_date'])
    source_readiness = assert_funding_report_source_ready(
        db, start_date=coverage_start_date, end_date=end_date)
    overlaps = overlapping_reports(db, account_id=account_id, vendor_id=vendor.id,
        start_date=start_date, end_date=end_date)
    if overlaps and not overlap_acknowledged:
        raise ValueError('OVERLAP_ACKNOWLEDGEMENT_REQUIRED')
    filter_text = sku_filter.strip()
    normalized_filter = normalize_sku(filter_text)
    product_filter = filter_text.casefold()
    report = FundingReport(
        account_id=account.id,
        vendor_id=vendor.id,
        report_number=f'COGS-{account.id}-{start_date:%Y%m%d}-{end_date:%Y%m%d}-{uuid4().hex[:8].upper()}',
        account_name_snapshot=account.display_name,
        account_type_snapshot=account.account_type,
        sales_start_date=start_date,
        sales_end_date=end_date,
        store_ids=sorted(set(store_ids)),
        sku_filter=filter_text or None,
        internal_note=internal_note.strip() or None,
        overlap_acknowledged=bool(overlaps),
        overlapping_report_ids=[row.id for row in overlaps],
        status='DRAFT',
        created_by_principal_id=actor_id,
    )
    db.add(report)
    db.flush()
    if credit_card_scope is not None:
        source_summary = _populate_credit_card_fifo_report(
            db,
            report=report,
            account=account,
            vendor=vendor,
            scope=credit_card_scope,
            start_date=start_date,
            end_date=end_date,
            store_ids=store_ids,
            filter_text=filter_text,
            normalized_filter=normalized_filter,
            product_filter=product_filter,
        )
        report.warning_summary = {
            'exclusions': {},
            'overlap_count': len(overlaps),
            'purchase_order_scope': source_summary,
            'vendor_purchase_order_ids': credit_card_order_ids,
            'square_source_readiness': _source_readiness_snapshot(source_readiness),
        }
        _audit(
            db,
            actor_id=actor_id,
            action='FUNDING_REPORT_CALCULATED',
            entity_type='funding_report',
            entity_id=report.id,
            after={
                'account_id': account.id,
                'vendor_id': vendor.id,
                'sales_start_date': str(start_date),
                'sales_end_date': str(end_date),
                'calculated_cogs': str(report.calculated_cogs),
                'overlap_acknowledged': report.overlap_acknowledged,
                'purchase_order_ids': source_summary['purchase_order_ids'],
                'eligible_variation_count': source_summary['eligible_sku_count'],
                'allocation_method': 'FIFO',
            },
            ip=ip,
        )
        db.flush()
        return report
    dialect_name = db.get_bind().dialect.name
    sale_query = select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.business_date >= start_date,
        ConsignmentSaleFact.business_date <= end_date,
        _normalized_sku_expression(
            ConsignmentSaleFact.sku_snapshot, dialect_name=dialect_name).in_(account_skus),
    )
    return_query = select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.business_date >= start_date,
        ConsignmentReturnFact.business_date <= end_date,
        _normalized_sku_expression(
            ConsignmentReturnFact.sku_snapshot, dialect_name=dialect_name).in_(account_skus),
    )
    if store_ids:
        sale_query = sale_query.where(ConsignmentSaleFact.store_id.in_(store_ids))
        return_query = return_query.where(ConsignmentReturnFact.store_id.in_(store_ids))
    facts = [(row, False) for row in db.scalars(sale_query).all()] + [
        (row, True) for row in db.scalars(return_query).all()
    ]
    groups: dict[tuple[str, int | None], dict] = {}
    exclusion_counts: dict[str, int] = defaultdict(int)
    snapshot_times: list[datetime] = []
    for fact, is_return in facts:
        sku = normalize_sku(fact.sku_snapshot)
        if sku not in account_skus:
            continue
        product_text = f'{fact.product_name_snapshot or ""} {fact.variation_name_snapshot or ""}'.casefold()
        if filter_text and sku != normalized_filter and product_filter not in product_text:
            continue
        reason_code = None
        mapping = None
        purchase_order_source = None
        purchase_order_source = _purchase_order_cost_source(
            order_scope, normalized_sku=sku, business_date=fact.business_date)
        if purchase_order_source is None:
            reason_code = 'MISSING_EFFECTIVE_PO_COST'
        if not reason_code and is_return and fact.quantity_returned is None:
            reason_code = 'RETURN_QUANTITY_MISSING'
        if reason_code:
            quantity = fact.quantity_returned if is_return else fact.quantity_sold
            db.add(FundingReportExclusion(
                report_id=report.id,
                source_type='RETURN' if is_return else 'SALE',
                source_id=fact.id,
                reason_code=reason_code,
                sku_snapshot=fact.sku_snapshot,
                product_name_snapshot=fact.product_name_snapshot,
                variation_name_snapshot=fact.variation_name_snapshot,
                store_id=fact.store_id,
                quantity_snapshot=quantity,
                amount_snapshot=fact.refund_amount if is_return else fact.net_sales_amount,
            ))
            exclusion_counts[reason_code] += 1
            continue
        unit_cost = Decimal(str(purchase_order_source['line'].unit_cost))
        source_id = f"PO:{purchase_order_source['purchase_order_line_id']}"
        key = (source_id, fact.store_id)
        group = groups.setdefault(key, {
            'mapping': mapping, 'purchase_order_source': purchase_order_source,
            'unit_cost': unit_cost, 'product': fact.product_name_snapshot,
            'variation': fact.variation_name_snapshot, 'sku': fact.sku_snapshot,
            'sold': Decimal('0'), 'returned': Decimal('0'), 'facts': [],
        })
        quantity = Decimal(str(fact.quantity_returned if is_return else fact.quantity_sold))
        group['returned' if is_return else 'sold'] += quantity
        group['facts'].append((fact, is_return, quantity))
    for (_source_id, store_id), group in groups.items():
        mapping = group['mapping']
        purchase_order_source = group['purchase_order_source']
        unit_cost = group['unit_cost']
        net = group['sold'] - group['returned']
        normalized_sku = (
            purchase_order_source['normalized_sku']
        )
        extended = money(net * unit_cost)
        inventory_qty, inventory_value, refreshed_at = _inventory_for_sku(
            db, normalized_sku=normalized_sku, unit_cost=unit_cost, store_id=store_id)
        if refreshed_at:
            snapshot_times.append(refreshed_at)
        line = FundingReportLine(
            report_id=report.id,
            mapping_id=None,
            normalized_sku=normalized_sku,
            sku_snapshot=group['sku'] or purchase_order_source['sku'],
            square_variation_id=purchase_order_source['line'].variation_id,
            product_name_snapshot=group['product'] or purchase_order_source['product'],
            variation_name_snapshot=group['variation'] or purchase_order_source['variation'],
            store_id=store_id,
            units_sold=group['sold'],
            units_returned=group['returned'],
            net_units=net,
            unit_cost_snapshot=unit_cost,
            extended_cogs=extended,
            inventory_units_snapshot=inventory_qty,
            inventory_value_snapshot=inventory_value,
            mapping_effective_date_snapshot=purchase_order_source['order_date'],
            source_transaction_count=len(group['facts']),
            warning_state=f"PO_LINE:{purchase_order_source['purchase_order_line_id']}",
        )
        db.add(line)
        db.flush()
        for fact, is_return, quantity in group['facts']:
            amount = money(quantity * unit_cost)
            db.add(FundingReportFactLink(
                report_id=report.id,
                report_line_id=line.id,
                sale_fact_id=None if is_return else fact.id,
                return_fact_id=fact.id if is_return else None,
                cogs_amount_snapshot=-amount if is_return else amount,
            ))
        report.units_sold += group['sold']
        report.units_returned += group['returned']
        report.net_units += net
        report.calculated_cogs += extended
        report.inventory_units_snapshot += inventory_qty
        report.inventory_value_snapshot += inventory_value
    report.inventory_snapshot_at = max(snapshot_times, default=datetime.now(timezone.utc))
    source_summary = None
    if order_scope is not None:
        source_summary = {
            'message': 'This report includes sales for SKUs found on purchase orders assigned to this Consignment account.',
            'purchase_order_ids': sorted(order_scope['orders']),
            'assigned_purchase_order_count': len(order_scope['orders']),
            'eligible_skus': sorted(order_scope['eligible_skus']),
            'eligible_sku_count': len(order_scope['eligible_skus']),
            'source_lines': [{key: value for key, value in row.items() if key not in {'line', 'order_date'}}
                             for row in order_scope['source_lines']],
            'setup_issues': order_scope['setup_issues'],
        }
    report.warning_summary = {
        'exclusions': dict(exclusion_counts), 'overlap_count': len(overlaps),
        'purchase_order_scope': source_summary,
        'vendor_purchase_order_ids': credit_card_order_ids,
        'square_source_readiness': _source_readiness_snapshot(source_readiness),
    }
    _audit(db, actor_id=actor_id, action='FUNDING_REPORT_CALCULATED', entity_type='funding_report',
           entity_id=report.id, after={'account_id': account.id, 'sales_start_date': str(start_date),
           'vendor_id': vendor.id,
           'sales_end_date': str(end_date), 'calculated_cogs': str(report.calculated_cogs),
           'overlap_acknowledged': report.overlap_acknowledged, 'exclusions': dict(exclusion_counts),
           'purchase_order_ids': source_summary['purchase_order_ids'] if source_summary else [],
           'eligible_sku_count': source_summary['eligible_sku_count'] if source_summary else None}, ip=ip)
    db.flush()
    return report


def active_adjustments(db: Session, *, report_id: int) -> list[FundingReportAdjustment]:
    return db.scalars(select(FundingReportAdjustment).where(
        FundingReportAdjustment.report_id == report_id).order_by(FundingReportAdjustment.id)).all()


def active_payment_allocations(db: Session, *, report_id: int) -> list[FundingPaymentAllocation]:
    reversals = db.scalars(select(FundingPayment.reversed_payment_id).where(
        FundingPayment.reversed_payment_id.is_not(None))).all()
    return db.scalars(select(FundingPaymentAllocation).join(
        FundingPayment, FundingPayment.id == FundingPaymentAllocation.payment_id
    ).where(
        FundingPaymentAllocation.report_id == report_id,
        FundingPayment.reversed_payment_id.is_(None),
        FundingPayment.id.not_in(list(reversals) or [-1]),
    )).all()


def report_position(db: Session, *, report_id: int) -> dict:
    report = db.get(FundingReport, report_id)
    if report is None:
        raise LookupError('Report not found.')
    adjustments = active_adjustments(db, report_id=report.id)
    charges = sum((money(row.amount) for row in adjustments if row.direction == 'INCREASE'), Decimal('0'))
    credits = sum((money(row.amount) for row in adjustments if row.direction == 'DECREASE'), Decimal('0'))
    adjusted = max(money(report.calculated_cogs) + charges - credits, Decimal('0'))
    allocations = active_payment_allocations(db, report_id=report.id)
    settled = sum((money(row.amount) for row in allocations), Decimal('0'))
    payment_types = {row.id: row.entry_type for row in db.scalars(select(FundingPayment).where(
        FundingPayment.id.in_([allocation.payment_id for allocation in allocations] or [-1]))).all()}
    replenishment = sum((money(row.amount) for row in allocations
        if payment_types.get(row.payment_id) == 'REPLENISHMENT'), Decimal('0'))
    cash = settled - replenishment
    remaining = max(adjusted - settled, Decimal('0'))
    return {'report': report, 'charges': money(charges), 'credits': money(credits),
            'adjusted_amount': money(adjusted), 'settled_amount': money(settled),
            'remaining_amount': money(remaining), 'replenishment_applied': money(replenishment),
            'cash_settlement': money(cash), 'adjustments': adjustments, 'allocations': allocations}


def finalize_report(db: Session, *, report_id: int, actor_id: int, ip=None) -> FundingReport:
    report = db.get(FundingReport, report_id)
    if report is None:
        raise LookupError('Report not found.')
    if report.status != 'DRAFT':
        raise ValueError('Only a draft report can be finalized.')
    source_snapshot = (report.warning_summary or {}).get('square_source_readiness')
    if not source_snapshot:
        raise ValueError(
            'This draft predates Square source-readiness controls. Delete it and calculate a new report.')
    source_readiness = assert_funding_report_source_ready(
        db, start_date=report.sales_start_date, end_date=report.sales_end_date)
    current_sync_at = _utc(source_readiness['last_successful_at']).isoformat()
    if source_snapshot.get('last_successful_at') != current_sync_at:
        raise ValueError(
            'Square sales were synchronized after this draft was calculated. '
            'Delete it and calculate a new report before finalizing.')
    position = report_position(db, report_id=report.id)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id).order_by(FundingReportLine.id)).all()
    report.finalized_snapshot = {
        'account': report.account_name_snapshot,
        'account_type': report.account_type_snapshot,
        'vendor_id': report.vendor_id,
        'sales_start_date': str(report.sales_start_date),
        'sales_end_date': str(report.sales_end_date),
        'store_ids': report.store_ids,
        'overlap_acknowledged': report.overlap_acknowledged,
        'overlapping_report_ids': report.overlapping_report_ids,
        'calculated_cogs': str(report.calculated_cogs),
        'adjusted_amount': str(position['adjusted_amount']),
        'inventory_units': str(report.inventory_units_snapshot),
        'inventory_value': str(report.inventory_value_snapshot),
        'line_ids': [row.id for row in lines],
        'mapping_ids': sorted({row.mapping_id for row in lines if row.mapping_id is not None}),
        'purchase_order_scope': report.warning_summary.get('purchase_order_scope'),
        'adjustment_ids': [row.id for row in position['adjustments']],
    }
    report.status = 'FINALIZED'
    report.finalized_at = datetime.now(timezone.utc)
    report.finalized_by_principal_id = actor_id
    _audit(db, actor_id=actor_id, action='FUNDING_REPORT_FINALIZED', entity_type='funding_report',
           entity_id=report.id, after=report.finalized_snapshot, ip=ip)
    return report


def void_report(db: Session, *, report_id: int, reason: str, actor_id: int, ip=None) -> FundingReport:
    report = db.get(FundingReport, report_id)
    if report is None or report.status in {'DRAFT', 'VOIDED'}:
        raise ValueError('Report not available to void.')
    if not reason.strip():
        raise ValueError('A void reason is required.')
    report.status = 'VOIDED'
    report.voided_at = datetime.now(timezone.utc)
    report.voided_by_principal_id = actor_id
    report.void_reason = reason.strip()
    _audit(db, actor_id=actor_id, action='FUNDING_REPORT_VOIDED', entity_type='funding_report',
           entity_id=report.id, after={'reason': report.void_reason}, ip=ip)
    return report


def delete_draft_report(
    db: Session, *, report_id: int, actor_id: int, reason: str = '', ip=None
) -> dict:
    report = db.get(FundingReport, report_id)
    if report is None:
        raise LookupError('Report not found.')
    if report.status != 'DRAFT' or report.finalized_at is not None:
        raise ValueError('Only an unfinalized draft report can be deleted.')
    allocations = db.scalar(select(func.count()).select_from(FundingPaymentAllocation).where(
        FundingPaymentAllocation.report_id == report.id)) or 0
    ledger_entries = db.scalar(select(func.count()).select_from(FundingLedgerEntry).where(
        FundingLedgerEntry.report_id == report.id)) or 0
    if allocations or ledger_entries:
        raise ValueError('This draft has downstream financial activity and cannot be deleted.')
    snapshot = {
        'report_id': int(report.id),
        'account_id': int(report.account_id),
        'account_name': report.account_name_snapshot,
        'sales_start_date': str(report.sales_start_date),
        'sales_end_date': str(report.sales_end_date),
        'calculated_cogs': str(report.calculated_cogs),
        'reason': reason.strip() or None,
        'deleted_at': datetime.now(timezone.utc).isoformat(),
    }
    _audit(db, actor_id=actor_id, action='FUNDING_DRAFT_REPORT_DELETED',
           entity_type='funding_report', entity_id=report.id, after=snapshot, ip=ip)
    db.execute(delete(FundingReportFactLink).where(FundingReportFactLink.report_id == report.id))
    db.execute(delete(FundingReportExclusion).where(FundingReportExclusion.report_id == report.id))
    db.execute(delete(FundingReportLine).where(FundingReportLine.report_id == report.id))
    db.execute(delete(FundingReportAdjustment).where(FundingReportAdjustment.report_id == report.id))
    db.delete(report)
    db.flush()
    return snapshot


def add_adjustment(db: Session, *, report_id: int, adjustment_type: str, direction: str,
                   amount: Decimal, effective_date: date, reason: str, internal_note: str,
                   owner_confirmed: bool, actor_id: int, ip=None) -> FundingReportAdjustment:
    report = db.get(FundingReport, report_id)
    if report is None or report.status in {'DRAFT', 'VOIDED'}:
        raise ValueError('Choose a finalized active report.')
    adjustment_type = adjustment_type.strip().upper()
    direction = direction.strip().upper()
    if adjustment_type not in ADJUSTMENT_TYPES or direction not in {'INCREASE', 'DECREASE'}:
        raise ValueError('Choose a valid adjustment type and direction.')
    value = money(amount)
    if value <= 0 or not reason.strip() or not owner_confirmed:
        raise ValueError('Amount, reason, and owner confirmation are required.')
    row = FundingReportAdjustment(report_id=report.id, adjustment_type=adjustment_type,
        direction=direction, amount=value, effective_date=effective_date, reason=reason.strip(),
        internal_note=internal_note.strip() or None, owner_confirmed=True,
        created_by_principal_id=actor_id)
    db.add(row); db.flush()
    if report.status != 'DRAFT':
        report.status = 'ADJUSTED'
    _audit(db, actor_id=actor_id, action='FUNDING_REPORT_ADJUSTMENT_RECORDED', entity_type='funding_report_adjustment',
           entity_id=row.id, after={'report_id': report.id, 'type': adjustment_type,
           'direction': direction, 'amount': str(value)}, ip=ip)
    return row


def reverse_adjustment(db: Session, *, adjustment_id: int, reason: str, actor_id: int, ip=None) -> FundingReportAdjustment:
    original = db.get(FundingReportAdjustment, adjustment_id)
    if original is None or original.reversed_adjustment_id is not None:
        raise ValueError('Adjustment not available for reversal.')
    already = db.scalar(select(FundingReportAdjustment).where(
        FundingReportAdjustment.reversed_adjustment_id == original.id))
    if already:
        raise ValueError('Adjustment was already reversed.')
    if not reason.strip():
        raise ValueError('A reversal reason is required.')
    row = FundingReportAdjustment(report_id=original.report_id, adjustment_type='OTHER',
        direction='DECREASE' if original.direction == 'INCREASE' else 'INCREASE', amount=original.amount,
        effective_date=date.today(), reason=reason.strip(), internal_note='Reversal', owner_confirmed=True,
        reversed_adjustment_id=original.id, created_by_principal_id=actor_id)
    db.add(row); db.flush()
    _audit(db, actor_id=actor_id, action='FUNDING_REPORT_ADJUSTMENT_REVERSED', entity_type='funding_report_adjustment',
           entity_id=row.id, after={'reversed_adjustment_id': original.id}, ip=ip)
    return row


def _update_report_status(db: Session, report: FundingReport) -> None:
    if report.status in {'DRAFT', 'VOIDED'}:
        return
    position = report_position(db, report_id=report.id)
    if position['remaining_amount'] == 0 and position['adjusted_amount'] > 0:
        report.status = 'SETTLED'
    elif position['settled_amount'] > 0:
        report.status = 'PARTIALLY_SETTLED'
    elif position['adjustments']:
        report.status = 'ADJUSTED'
    else:
        report.status = 'FINALIZED'


def record_payment(db: Session, *, account_id: int, entry_type: str, amount: Decimal,
                   payment_date: date, payment_source: str, confirmation_number: str,
                   reason: str, internal_note: str, allocations: dict[int, Decimal],
                   actor_id: int, vendor_id: int | None = None, ip=None) -> FundingPayment:
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise ValueError('Account not found.')
    if account.account_type == 'CREDIT_CARD' and vendor_id is None and allocations:
        allocation_vendor_ids = {report.vendor_id for report_id in allocations
            if (report := db.get(FundingReport, int(report_id))) is not None}
        if len(allocation_vendor_ids) == 1 and None not in allocation_vendor_ids:
            vendor_id = allocation_vendor_ids.pop()
    vendor = resolve_account_vendor(db, account=account, vendor_id=vendor_id, purpose='payment')
    entry_type = entry_type.strip().upper()
    if entry_type not in {'PAYMENT', 'REPLENISHMENT'}:
        raise ValueError('Choose Payment or Replenishment.')
    if account.account_type == 'CREDIT_CARD' and entry_type != 'PAYMENT':
        raise ValueError('Credit Card accounts use payments, not replenishment.')
    value = money(amount)
    if value <= 0 or not reason.strip():
        raise ValueError('Payment amount and description are required.')
    allocation_reports: dict[int, FundingReport] = {}
    for report_id in allocations:
        report = db.get(FundingReport, int(report_id))
        if report is None or report.account_id != account.id or report.status in {'DRAFT', 'VOIDED'}:
            raise ValueError('Payments can only be allocated to finalized reports for this account.')
        if report.vendor_id is None:
            raise ValueError('Legacy reports without a known vendor cannot receive new payment allocations.')
        if report.vendor_id != vendor.id:
            raise ValueError('Payments cannot be allocated across vendors.')
        allocation_reports[int(report_id)] = report
    row = FundingPayment(account_id=account.id, vendor_id=vendor.id,
        entry_type=entry_type, amount=value,
        payment_date=payment_date, payment_source=payment_source.strip() or None,
        confirmation_number=confirmation_number.strip() or None, reason=reason.strip(),
        internal_note=internal_note.strip() or None, status='ACTIVE', created_by_principal_id=actor_id)
    db.add(row); db.flush()
    remaining_payment = value
    touched = []
    for report_id, requested in allocations.items():
        if remaining_payment <= 0:
            break
        report = allocation_reports[int(report_id)]
        available = report_position(db, report_id=report.id)['remaining_amount']
        allocation_amount = min(money(requested), available, remaining_payment)
        if allocation_amount <= 0:
            continue
        db.add(FundingPaymentAllocation(payment_id=row.id, report_id=report.id,
            amount=allocation_amount, created_by_principal_id=actor_id))
        db.flush()
        remaining_payment -= allocation_amount
        touched.append(report)
    direction = 'DECREASE'
    db.add(FundingLedgerEntry(account_id=account.id, entry_type=entry_type,
        direction=direction, amount=value, effective_date=payment_date, payment_id=row.id,
        reason=row.reason, internal_note=row.internal_note, created_by_principal_id=actor_id))
    db.flush()
    for report in touched:
        _update_report_status(db, report)
    _audit(db, actor_id=actor_id, action='FUNDING_PAYMENT_RECORDED', entity_type='funding_payment',
           entity_id=row.id, after={'account_id': account.id, 'amount': str(value),
           'vendor_id': vendor.id,
           'allocated': str(value - remaining_payment), 'unallocated': str(remaining_payment)}, ip=ip)
    return row


def reverse_payment(db: Session, *, payment_id: int, reason: str, actor_id: int, ip=None) -> FundingPayment:
    original = db.get(FundingPayment, payment_id)
    if original is None or original.reversed_payment_id is not None:
        raise ValueError('Payment not available for reversal.')
    if db.scalar(select(FundingPayment).where(FundingPayment.reversed_payment_id == original.id)):
        raise ValueError('Payment was already reversed.')
    if not reason.strip():
        raise ValueError('A reversal reason is required.')
    row = FundingPayment(account_id=original.account_id, vendor_id=original.vendor_id,
        entry_type=original.entry_type,
        amount=original.amount, payment_date=date.today(), reason=reason.strip(),
        internal_note='Reversal', status='ACTIVE', reversed_payment_id=original.id,
        created_by_principal_id=actor_id)
    db.add(row); db.flush()
    db.add(FundingLedgerEntry(account_id=original.account_id, entry_type='REVERSAL',
        direction='INCREASE', amount=original.amount, effective_date=row.payment_date,
        payment_id=row.id, reason=row.reason, created_by_principal_id=actor_id))
    db.flush()
    reports = db.scalars(select(FundingReport).join(FundingPaymentAllocation,
        FundingPaymentAllocation.report_id == FundingReport.id).where(
        FundingPaymentAllocation.payment_id == original.id)).all()
    for report in reports:
        _update_report_status(db, report)
    _audit(db, actor_id=actor_id, action='FUNDING_PAYMENT_REVERSED', entity_type='funding_payment',
           entity_id=row.id, after={'reversed_payment_id': original.id}, ip=ip)
    return row


def record_ledger_entry(db: Session, *, account_id: int, entry_type: str, direction: str,
                        amount: Decimal, effective_date: date, reason: str, internal_note: str,
                        actor_id: int, ip=None, order_payment_id: int | None = None,
                        inventory_backed_estimate: Decimal | None = None) -> FundingLedgerEntry:
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise ValueError('Account not found.')
    entry_type = entry_type.strip().upper(); direction = direction.strip().upper(); value = money(amount)
    if entry_type not in LEDGER_TYPES or direction not in {'INCREASE', 'DECREASE'}:
        raise ValueError('Choose a valid ledger activity and direction.')
    if value < 0 or not reason.strip():
        raise ValueError('Amount and reason are required.')
    estimate = money(inventory_backed_estimate) if inventory_backed_estimate is not None else None
    if estimate is not None and (entry_type != 'OPENING_BALANCE' or estimate < 0):
        raise ValueError('An inventory-backed estimate may be recorded only with an opening balance.')
    row = FundingLedgerEntry(account_id=account.id, entry_type=entry_type,
        direction=direction, amount=value, effective_date=effective_date,
        order_payment_id=order_payment_id, reason=reason.strip(),
        internal_note=internal_note.strip() or None, inventory_backed_estimate=estimate,
        created_by_principal_id=actor_id)
    db.add(row); db.flush()
    _audit(db, actor_id=actor_id, action='FUNDING_LEDGER_ENTRY_RECORDED', entity_type='funding_ledger_entry',
           entity_id=row.id, after={'account_id': account.id, 'type': entry_type,
           'direction': direction, 'amount': str(value)}, ip=ip)
    return row


def reverse_ledger_entry(db: Session, *, entry_id: int, reason: str, actor_id: int, ip=None) -> FundingLedgerEntry:
    original = db.get(FundingLedgerEntry, entry_id)
    if original is None or original.entry_type in {'PAYMENT', 'REPLENISHMENT', 'REVERSAL'}:
        raise ValueError('Account activity not available for this reversal.')
    if db.scalar(select(FundingLedgerEntry).where(FundingLedgerEntry.original_entry_id == original.id)):
        raise ValueError('Account activity was already reversed.')
    if not reason.strip():
        raise ValueError('A reversal reason is required.')
    row = FundingLedgerEntry(
        account_id=original.account_id,
        entry_type='REVERSAL',
        direction='DECREASE' if original.direction == 'INCREASE' else 'INCREASE',
        amount=original.amount,
        effective_date=date.today(),
        report_id=original.report_id,
        reason=reason.strip(),
        internal_note='Reversal',
        original_entry_id=original.id,
        created_by_principal_id=actor_id,
    )
    db.add(row); db.flush()
    _audit(db, actor_id=actor_id, action='FUNDING_LEDGER_ENTRY_REVERSED', entity_type='funding_ledger_entry',
           entity_id=row.id, after={'original_entry_id': original.id}, ip=ip)
    return row


def _report_version_token(report: FundingReport) -> str:
    changed_at = report.updated_at or report.created_at
    return f'{report.status}|{changed_at.isoformat() if changed_at else "pending"}'


def delete_report(
    db: Session,
    *,
    account_id: int,
    report_id: int,
    expected_token: str,
    actor_id: int,
    reason: str = '',
    ip=None,
) -> dict:
    """Permanently delete a report and records exclusively owned by it."""
    with db.begin_nested():
        report = db.get(FundingReport, report_id)
        if report is None:
            raise LookupError('Report not found.')
        if report.account_id != account_id:
            raise ValueError('Report does not belong to this Funding Account.')
        if expected_token != _report_version_token(report):
            raise ValueError('This report changed. Refresh the page before deleting it.')

        allocation_ids = list(db.scalars(select(FundingPaymentAllocation.id).where(
            FundingPaymentAllocation.report_id == report.id)).all())
        ledger_rows = list(db.scalars(select(FundingLedgerEntry).where(
            FundingLedgerEntry.report_id == report.id).order_by(FundingLedgerEntry.id)).all())
        ledger_ids = {row.id for row in ledger_rows}

        has_shared_link = any(
            row.payment_id is not None
            or row.order_payment_id is not None
            or (row.original_entry_id is not None and row.original_entry_id not in ledger_ids)
            or (row.replacement_for_entry_id is not None
                and row.replacement_for_entry_id not in ledger_ids)
            for row in ledger_rows
        )
        if ledger_ids and not has_shared_link:
            has_shared_link = db.scalar(select(FundingLedgerEntry.id).where(
                FundingLedgerEntry.id.not_in(ledger_ids),
                or_(
                    FundingLedgerEntry.original_entry_id.in_(ledger_ids),
                    FundingLedgerEntry.replacement_for_entry_id.in_(ledger_ids),
                ),
            )) is not None
        if has_shared_link:
            raise ValueError(
                'A shared accounting entry references this report. Remove that link before deleting.'
            )

        dependent_counts = {
            'payment_allocations': len(allocation_ids),
            'ledger_entries': len(ledger_ids),
            'adjustments': db.scalar(select(func.count()).select_from(
                FundingReportAdjustment).where(
                    FundingReportAdjustment.report_id == report.id)) or 0,
            'fact_links': db.scalar(select(func.count()).select_from(
                FundingReportFactLink).where(
                    FundingReportFactLink.report_id == report.id)) or 0,
            'exclusions': db.scalar(select(func.count()).select_from(
                FundingReportExclusion).where(
                    FundingReportExclusion.report_id == report.id)) or 0,
            'lines': db.scalar(select(func.count()).select_from(
                FundingReportLine).where(FundingReportLine.report_id == report.id)) or 0,
        }
        snapshot = {
            'report_id': int(report.id),
            'account_id': int(report.account_id),
            'vendor_id': report.vendor_id,
            'account_name': report.account_name_snapshot,
            'account_type': report.account_type_snapshot,
            'report_number': report.report_number,
            'sales_start_date': str(report.sales_start_date),
            'sales_end_date': str(report.sales_end_date),
            'calculated_cogs': str(money(report.calculated_cogs)),
            'prior_status': report.status,
            'dependent_records_deleted': dependent_counts,
            'reason': reason.strip() or None,
            'deleted_at': datetime.now(timezone.utc).isoformat(),
        }
        _audit(
            db,
            actor_id=actor_id,
            action=('FUNDING_DRAFT_REPORT_DELETED'
                    if report.status == 'DRAFT' else 'FUNDING_REPORT_DELETED'),
            entity_type='funding_report',
            entity_id=report.id,
            after=snapshot,
            ip=ip,
        )

        db.execute(delete(FundingPaymentAllocation).where(
            FundingPaymentAllocation.report_id == report.id))
        db.execute(delete(FundingReportAdjustment).where(
            FundingReportAdjustment.report_id == report.id))
        db.execute(delete(FundingLedgerEntry).where(
            FundingLedgerEntry.report_id == report.id))
        db.execute(delete(FundingReportFactLink).where(
            FundingReportFactLink.report_id == report.id))
        db.execute(delete(FundingReportExclusion).where(
            FundingReportExclusion.report_id == report.id))
        db.execute(delete(FundingReportLine).where(
            FundingReportLine.report_id == report.id))
        db.delete(report)
        db.flush()
        return snapshot


def record_inventory_purchase_for_order(
    db: Session, *, payment_method_id: int, order_payment_id: int,
    amount: Decimal, effective_date: date, actor_id: int
) -> FundingLedgerEntry | None:
    account = db.scalar(select(FundingAccount).where(
        FundingAccount.account_type == 'CREDIT_CARD',
        FundingAccount.payment_method_id == payment_method_id,
        FundingAccount.is_active.is_(True),
    ))
    if account is None:
        return None
    existing = db.scalar(select(FundingLedgerEntry).where(
        FundingLedgerEntry.order_payment_id == order_payment_id,
        FundingLedgerEntry.entry_type == 'INVENTORY_PURCHASE',
    ))
    if existing:
        return existing
    row = FundingLedgerEntry(account_id=account.id, entry_type='INVENTORY_PURCHASE',
        direction='INCREASE', amount=money(amount), effective_date=effective_date,
        order_payment_id=order_payment_id,
        reason='Owner-confirmed inventory purchase assigned to this Credit Card account.',
        created_by_principal_id=actor_id)
    db.add(row); db.flush()
    return row


def tracked_balance(db: Session, *, account_id: int) -> Decimal:
    rows = db.execute(select(FundingLedgerEntry.direction,
        func.coalesce(func.sum(FundingLedgerEntry.amount), 0)).where(
        FundingLedgerEntry.account_id == account_id).group_by(FundingLedgerEntry.direction)).all()
    totals = {direction: money(amount) for direction, amount in rows}
    return money(totals.get('INCREASE', Decimal('0')) - totals.get('DECREASE', Decimal('0')))


@dataclass(frozen=True)
class AprEstimate:
    active_apr: Decimal
    promotional_active: bool
    days_until_expiration: int | None
    annual_cost: Decimal
    monthly_cost: Decimal
    post_promotion_annual_cost: Decimal | None
    post_promotion_monthly_cost: Decimal | None


def apr_estimate(account: FundingAccount, balance: Decimal, *, today: date | None = None) -> AprEstimate:
    today = today or date.today()
    promo_active = bool(account.promotional_apr is not None
        and (account.promotional_start_date is None or account.promotional_start_date <= today)
        and (account.promotional_expiration_date is None or account.promotional_expiration_date >= today))
    active_apr = Decimal(str(account.promotional_apr if promo_active else account.standard_apr or 0))
    annual = money(balance * active_apr / Decimal('100'))
    future_apr = Decimal(str(account.standard_apr)) if promo_active and account.standard_apr is not None else None
    future_annual = money(balance * future_apr / Decimal('100')) if future_apr is not None else None
    return AprEstimate(active_apr=active_apr, promotional_active=promo_active,
        days_until_expiration=(account.promotional_expiration_date - today).days
            if promo_active and account.promotional_expiration_date else None,
        annual_cost=annual, monthly_cost=money(annual / Decimal('12')),
        post_promotion_annual_cost=future_annual,
        post_promotion_monthly_cost=money(future_annual / Decimal('12')) if future_annual is not None else None)


def account_summary(db: Session, *, account_id: int) -> dict:
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise LookupError('Account not found.')
    reports = db.scalars(select(FundingReport).where(FundingReport.account_id == account.id)
        .order_by(FundingReport.created_at.desc(), FundingReport.id.desc())).all()
    positions = {row.id: report_position(db, report_id=row.id) for row in reports}
    balance = tracked_balance(db, account_id=account.id)
    derived_inventory = None
    inventory_units = inventory_value = Decimal('0')
    refreshed = []
    if account.account_type == 'CREDIT_CARD':
        derived_inventory = credit_card_inventory_summary(db, account=account)
        inventory_units = derived_inventory['remaining_units']
        inventory_value = derived_inventory['remaining_value']
    else:
        mapped = db.scalars(select(FundingSkuMapping).where(
            FundingSkuMapping.account_id == account.id
        )).all()
        current = [row for row in mapped if row.status == 'ACTIVE' and row.effective_start_date <= date.today()
            and (row.effective_end_date is None or row.effective_end_date >= date.today())]
        for mapping in current:
            if mapping.unit_cost is None:
                continue
            qty, value, at = _inventory_for_sku(db, normalized_sku=mapping.normalized_sku,
                unit_cost=Decimal(str(mapping.unit_cost)), store_id=None)
            inventory_units += qty; inventory_value += value
            if at: refreshed.append(at)
    payments = db.scalars(select(FundingPayment).where(FundingPayment.account_id == account.id)
        .order_by(FundingPayment.payment_date.desc(), FundingPayment.id.desc())).all()
    reversed_payment_ids = {row.reversed_payment_id for row in payments if row.reversed_payment_id is not None}
    ledger = db.scalars(select(FundingLedgerEntry).where(FundingLedgerEntry.account_id == account.id)
        .order_by(FundingLedgerEntry.effective_date.desc(), FundingLedgerEntry.id.desc())).all()
    reversed_ledger_ids = {row.original_entry_id for row in ledger if row.original_entry_id is not None}
    open_reports = sorted(
        (row for row in reports if row.status not in {'DRAFT', 'VOIDED'} and positions[row.id]['remaining_amount'] > 0),
        key=lambda row: (row.sales_end_date, row.sales_start_date, row.id),
    )
    open_report_amount = sum((positions[row.id]['remaining_amount'] for row in reports
        if row.status != 'VOIDED'), Decimal('0'))
    active_payment_ids = {row.id for row in payments if row.reversed_payment_id is None
        and not any(candidate.reversed_payment_id == row.id for candidate in payments)}
    all_allocations = db.scalars(select(FundingPaymentAllocation).where(
        FundingPaymentAllocation.payment_id.in_(active_payment_ids or [-1]))).all()
    allocated_by_payment = defaultdict(Decimal)
    for allocation in all_allocations:
        allocated_by_payment[allocation.payment_id] += money(allocation.amount)
    unallocated_by_payment = {row.id: max(money(row.amount) - allocated_by_payment[row.id], Decimal('0'))
        for row in payments if row.id in active_payment_ids}
    available_replenishment_credit = sum((unallocated_by_payment[row.id] for row in payments
        if row.id in active_payment_ids and row.entry_type == 'REPLENISHMENT'), Decimal('0'))
    unallocated_payment = sum((unallocated_by_payment[row.id] for row in payments
        if row.id in active_payment_ids and row.entry_type == 'PAYMENT'), Decimal('0'))
    inventory_value_money = money(inventory_value) if inventory_value is not None else None
    return {'account': account, 'reports': reports, 'positions': positions, 'tracked_balance': balance,
        'inventory_units': inventory_units, 'inventory_value': inventory_value_money,
        'derived_inventory': derived_inventory,
        'inventory_snapshot_at': (derived_inventory['as_of'] if derived_inventory else max(refreshed, default=None)),
        'payments': payments, 'ledger': ledger,
        'reversed_payment_ids': reversed_payment_ids,
        'open_reports': open_reports, 'reversed_ledger_ids': reversed_ledger_ids,
        'open_report_amount': money(open_report_amount),
        'available_replenishment_credit': money(available_replenishment_credit),
        'unallocated_payment': money(unallocated_payment),
        'apr': apr_estimate(account, balance) if account.account_type == 'CREDIT_CARD' else None,
        'inventory_backed_estimate': (
            min(inventory_value_money, max(balance, Decimal('0')))
            if inventory_value_money is not None else None
        ),
        'potential_non_inventory_balance': (
            max(balance - inventory_value_money, Decimal('0'))
            if inventory_value_money is not None else None
        )}
