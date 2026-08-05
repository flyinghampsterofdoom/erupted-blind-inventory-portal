from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import re
from uuid import uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    ConsignmentReturnFact,
    ConsignmentSaleFact,
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
    PurchaseOrderStatus,
    Store,
    Vendor,
    VendorPaymentSetting,
)


CENT = Decimal('0.01')
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


def eligible_vendors_for_account(db: Session, *, account: FundingAccount) -> list[Vendor]:
    """Return canonical vendors eligible for report/payment scope."""
    if not account.is_active:
        return []
    if account.account_type == 'CONSIGNMENT':
        vendor = db.get(Vendor, account.vendor_id)
        return [vendor] if vendor is not None else []
    if account.account_type != 'CREDIT_CARD' or account.payment_method_id is None:
        return []
    return list(db.scalars(
        select(Vendor)
        .join(VendorPaymentSetting, VendorPaymentSetting.vendor_id == Vendor.id)
        .join(PaymentMethod, PaymentMethod.id == VendorPaymentSetting.default_payment_method_id)
        .where(
            VendorPaymentSetting.default_payment_method_id == account.payment_method_id,
            Vendor.active.is_(True),
            PaymentMethod.is_active.is_(True),
            PaymentMethod.category == 'CREDIT_CARD',
        )
        .order_by(func.lower(Vendor.name), Vendor.id)
    ).all())


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
    raise ValueError('The selected vendor is not configured for this credit card account.')


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


def _credit_card_order_ids(
    db: Session, *, account: FundingAccount, vendor_id: int
) -> list[int]:
    return list(db.scalars(select(PurchaseOrder.id).join(
        OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id
    ).where(
        OrderPayment.payment_method_id == account.payment_method_id,
        OrderPayment.vendor_id == vendor_id,
        PurchaseOrder.status.in_(QUALIFYING_ORDER_STATUSES),
    ).order_by(PurchaseOrder.id)).all())


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
    order_scope = None
    credit_card_order_ids: list[int] = []
    if account.account_type == 'CONSIGNMENT':
        order_scope = _consignment_order_scope(db, account=account)
        account_skus = order_scope['eligible_skus']
    else:
        _account_mappings, account_skus = _validate_account_mapping_boundary(
            db, account_id=account.id, start_date=start_date, end_date=end_date)
        credit_card_order_ids = _credit_card_order_ids(
            db, account=account, vendor_id=vendor.id)
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
    if account.account_type == 'CREDIT_CARD':
        sale_query = sale_query.where(ConsignmentSaleFact.vendor_id_snapshot == vendor.id)
        return_query = return_query.where(ConsignmentReturnFact.vendor_id_snapshot == vendor.id)
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
        if order_scope is not None:
            purchase_order_source = _purchase_order_cost_source(
                order_scope, normalized_sku=sku, business_date=fact.business_date)
            if purchase_order_source is None:
                reason_code = 'MISSING_EFFECTIVE_PO_COST'
        else:
            mappings = _active_mappings(
                db, account_id=account.id, normalized_sku=sku, business_date=fact.business_date)
            if not mappings:
                continue
            if len(mappings) > 1:
                reason_code = 'CONFLICTING_MAPPING'
            elif mappings[0].unit_cost is None:
                reason_code = 'MISSING_COST'
            else:
                mapping = mappings[0]
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
        unit_cost = Decimal(str(
            purchase_order_source['line'].unit_cost if purchase_order_source is not None else mapping.unit_cost))
        source_id = (
            f"PO:{purchase_order_source['purchase_order_line_id']}"
            if purchase_order_source is not None else f'MAPPING:{mapping.id}'
        )
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
            purchase_order_source['normalized_sku'] if purchase_order_source is not None
            else mapping.normalized_sku
        )
        extended = money(net * unit_cost)
        inventory_qty, inventory_value, refreshed_at = _inventory_for_sku(
            db, normalized_sku=normalized_sku, unit_cost=unit_cost, store_id=store_id)
        if refreshed_at:
            snapshot_times.append(refreshed_at)
        line = FundingReportLine(
            report_id=report.id,
            mapping_id=mapping.id if mapping is not None else None,
            normalized_sku=normalized_sku,
            sku_snapshot=group['sku'] or (
                purchase_order_source['sku'] if purchase_order_source is not None else mapping.sku_snapshot),
            square_variation_id=(
                purchase_order_source['line'].variation_id if purchase_order_source is not None
                else mapping.square_variation_id),
            product_name_snapshot=group['product'] or (
                purchase_order_source['product'] if purchase_order_source is not None
                else mapping.product_name_snapshot or mapping.normalized_sku),
            variation_name_snapshot=group['variation'] or (
                purchase_order_source['variation'] if purchase_order_source is not None
                else mapping.variation_name_snapshot),
            store_id=store_id,
            units_sold=group['sold'],
            units_returned=group['returned'],
            net_units=net,
            unit_cost_snapshot=unit_cost,
            extended_cogs=extended,
            inventory_units_snapshot=inventory_qty,
            inventory_value_snapshot=inventory_value,
            mapping_effective_date_snapshot=(
                purchase_order_source['order_date'] if purchase_order_source is not None
                else mapping.effective_start_date),
            source_transaction_count=len(group['facts']),
            warning_state=(f"PO_LINE:{purchase_order_source['purchase_order_line_id']}"
                           if purchase_order_source is not None else None),
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
    mapped = db.scalars(select(FundingSkuMapping).where(FundingSkuMapping.account_id == account.id)).all()
    inventory_units = inventory_value = Decimal('0')
    refreshed = []
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
    return {'account': account, 'reports': reports, 'positions': positions, 'tracked_balance': balance,
        'inventory_units': inventory_units, 'inventory_value': money(inventory_value),
        'inventory_snapshot_at': max(refreshed, default=None), 'payments': payments, 'ledger': ledger,
        'reversed_payment_ids': reversed_payment_ids,
        'open_reports': open_reports, 'reversed_ledger_ids': reversed_ledger_ids,
        'open_report_amount': money(open_report_amount),
        'available_replenishment_credit': money(available_replenishment_credit),
        'unallocated_payment': money(unallocated_payment),
        'apr': apr_estimate(account, balance) if account.account_type == 'CREDIT_CARD' else None,
        'inventory_backed_estimate': min(money(inventory_value), max(balance, Decimal('0'))),
        'potential_non_inventory_balance': max(balance - money(inventory_value), Decimal('0'))}
