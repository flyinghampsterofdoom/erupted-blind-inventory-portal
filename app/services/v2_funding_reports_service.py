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
    FundingReportFifoException,
    FundingReportLine,
    FundingSkuMapping,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    OrderPayment,
    PaymentMethod,
    Principal,
    PurchaseOrder,
    PurchaseOrderLine,
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


def combined_report_metadata(report: FundingReport) -> dict | None:
    metadata = (report.warning_summary or {}).get('combined_report')
    return metadata if isinstance(metadata, dict) else None


def is_combined_report(report: FundingReport) -> bool:
    return combined_report_metadata(report) is not None


def combined_report_member_state(
    db: Session, *, report: FundingReport
) -> tuple[list[FundingReport], list[int]]:
    metadata = combined_report_metadata(report)
    if metadata is None:
        return [], []
    member_ids = [int(value) for value in metadata.get('member_report_ids', [])]
    members = {
        row.id: row for row in db.scalars(select(FundingReport).where(
            FundingReport.id.in_(member_ids or [-1])
        )).all()
    }
    return (
        [members[report_id] for report_id in member_ids if report_id in members],
        [report_id for report_id in member_ids if report_id not in members],
    )


def combined_report_members(db: Session, *, report: FundingReport) -> list[FundingReport]:
    members, missing = combined_report_member_state(db, report=report)
    if missing:
        raise ValueError(
            'This combined report references missing vendor report(s): '
            + ', '.join(str(value) for value in missing)
        )
    return members


def combined_reports_referencing(
    db: Session, *, report: FundingReport
) -> list[int]:
    if is_combined_report(report):
        return []
    candidates = db.scalars(select(FundingReport).where(
        FundingReport.account_id == report.account_id,
        FundingReport.vendor_id.is_(None),
    )).all()
    return [
        candidate.id for candidate in candidates
        if report.id in (
            (combined_report_metadata(candidate) or {}).get('member_report_ids', [])
        )
    ]


def _matching_vendor_report(
    db: Session, *, account_id: int, vendor_id: int, start_date: date,
    end_date: date, store_ids: list[int], sku_filter: str,
) -> FundingReport | None:
    candidates = db.scalars(select(FundingReport).where(
        FundingReport.account_id == account_id,
        FundingReport.vendor_id == vendor_id,
        FundingReport.sales_start_date == start_date,
        FundingReport.sales_end_date == end_date,
        FundingReport.status != 'VOIDED',
    ).order_by(FundingReport.created_at, FundingReport.id)).all()
    normalized_stores = sorted(set(store_ids))
    normalized_filter = sku_filter.strip() or None
    matches = [row for row in candidates
        if sorted(row.store_ids or []) == normalized_stores
        and (row.sku_filter or None) == normalized_filter]
    if not matches:
        return None
    finalized = [row for row in matches if row.status != 'DRAFT']
    return (finalized or matches)[0]


def duplicate_finalized_fact_links(db: Session, *, report: FundingReport) -> list[int]:
    if is_combined_report(report):
        return []
    links = db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id
    )).all()
    sale_ids = [row.sale_fact_id for row in links if row.sale_fact_id is not None]
    return_ids = [row.return_fact_id for row in links if row.return_fact_id is not None]
    if not sale_ids and not return_ids:
        return []
    conditions = []
    if sale_ids:
        conditions.append(FundingReportFactLink.sale_fact_id.in_(sale_ids))
    if return_ids:
        conditions.append(FundingReportFactLink.return_fact_id.in_(return_ids))
    return list(db.scalars(select(FundingReport.id).join(
        FundingReportFactLink,
        FundingReportFactLink.report_id == FundingReport.id,
    ).where(
        FundingReport.id != report.id,
        FundingReport.account_id == report.account_id,
        FundingReport.vendor_id == report.vendor_id,
        FundingReport.status.not_in(('DRAFT', 'VOIDED')),
        or_(*conditions),
    ).distinct().order_by(FundingReport.id)).all())


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


def _consignment_order_scope(
    db: Session, *, account: FundingAccount, start_date: date, end_date: date
) -> dict:
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
    blocking_issues = [
        row for row in setup_issues
        if row['issue'] in {'Missing SKU', 'Missing saved cost'}
    ]
    if blocking_issues:
        line_ids = ', '.join(str(row['purchase_order_line_id']) for row in blocking_issues)
        raise ValueError(
            'Assigned funded PO lines have missing catalog identity or cost and cannot '
            f'be allocated safely. Review PO line(s): {line_ids}.'
        )
    funded_skus = set(cost_sources)
    if not funded_skus:
        raise ValueError('No purchase-order SKUs are assigned to this Consignment account.')
    for sku in cost_sources:
        cost_sources[sku].sort(key=lambda row: (row['order_date'], row['purchase_order_line_id']))
    funded_variations = {
        str(row['line'].variation_id or '').strip()
        for sources in cost_sources.values()
        for row in sources
        if str(row['line'].variation_id or '').strip()
    }
    candidate_catalog = [
        row for row in db.scalars(select(OrderingCatalogIdentity).where(
            OrderingCatalogIdentity.square_is_deleted.is_(False),
        )).all()
        if normalize_sku(row.sku) in funded_skus
    ]
    # Consignment membership is account-scoped. Purchasing relationships in
    # VendorSkuConfig (including alternates and defaults) are deliberately not a
    # membership source for this report.
    account_mappings = _period_account_mappings(
        db, account_id=int(account.id), start_date=start_date, end_date=end_date
    )
    mapped_variations = {
        str(row.square_variation_id or '').strip()
        for row in account_mappings
        if str(row.square_variation_id or '').strip()
    }
    lots = []
    for sku in sorted(cost_sources):
        for source in cost_sources[sku]:
            line = source['line']
            order, payment = orders[int(line.purchase_order_id)]
            quantity = Decimal(str(line.ordered_qty))
            lots.append(_FundingAllocationLayer(
                order=order,
                line=line,
                payment=payment,
                account_id=int(account.id),
                receipt_line_id=None,
                funded_at=_order_timestamp(order),
                quantity=quantity,
                remaining=quantity,
            ))
    lots.sort(key=lambda row: (
        _order_timestamp(row.order), int(row.order.id), int(row.line.id)
    ))
    return {
        'orders': orders,
        'eligible_skus': funded_skus,
        'eligible_variations': {
            str(row.square_variation_id) for row in candidate_catalog
        } | funded_variations | mapped_variations,
        'account_mappings': account_mappings,
        'cost_sources': cost_sources,
        'source_lines': source_lines,
        'setup_issues': setup_issues,
        'lots': lots,
        'oldest_funded_order_date': min(
            row.funded_at.astimezone(PORTAL_TIMEZONE).date() for row in lots
        ),
    }


@dataclass
class _FundingAllocationLayer:
    order: PurchaseOrder
    line: PurchaseOrderLine
    payment: OrderPayment | None
    account_id: int | None
    receipt_line_id: int | None
    funded_at: datetime
    quantity: Decimal
    remaining: Decimal

    @property
    def key(self) -> tuple[int, int | None, str]:
        source = 'FUNDED_ORDER'
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
    evidence_timestamp = received_at if store_quantity > line_quantity else None
    return quantity, evidence_timestamp


def _credit_card_product_scope(
    db: Session, *, account: FundingAccount, vendor: Vendor
) -> dict:
    """Select mapped products and their latest saved PO cost, without inventory history."""
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

    source_lines = []
    setup_issues = []
    eligible_variations = set()
    for order, payment, line in assigned_rows:
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
            'funded_quantity': str(Decimal(str(line.ordered_qty))),
            'unit_cost': str(line.unit_cost) if line.unit_cost is not None else None,
            'cost_effective_date': str(_purchase_order_date(order)),
            'original_vendor_id': int(order.vendor_id),
            'financial_vendor_id': int(vendor.id),
            'financial_account': account.display_name,
        }
        source_lines.append(source)
        if not source['square_variation_id']:
            setup_issues.append({**source, 'issue': 'Missing Square variation ID'})
            continue
        eligible_variations.add(source['square_variation_id'])

    blocking_issues = [
        row for row in setup_issues
        if row['issue'] in {'Missing Square variation ID', 'Missing saved cost'}
    ]
    if blocking_issues:
        line_ids = ', '.join(str(row['purchase_order_line_id']) for row in blocking_issues)
        raise ValueError(
            'Assigned PO lines have missing Square identity or cost and cannot be '
            f'calculated. Review PO line(s): {line_ids}.'
        )
    if not eligible_variations:
        raise ValueError(
            'No identifiable purchased products are assigned to this Funding '
            'Account and vendor.'
        )

    # One cost per variation, from this card/vendor only. PO dates select cost;
    # they never determine whether a period sale is eligible.
    products = {}
    for order, payment, line in sorted(assigned_rows, key=lambda row: (
        _purchase_order_date(row[0]), int(row[2].id)
    )):
        products[str(line.variation_id).strip()] = (order, line)
    return {
        'assigned_orders': assigned_orders,
        'eligible_variations': eligible_variations,
        'products': products,
        'source_lines': source_lines,
        'setup_issues': setup_issues,
    }


def _credit_card_fifo_scope(
    db: Session, *, account: FundingAccount, vendor: Vendor
) -> dict:
    """Build funded PO layers using current owner-entered payment assignments."""
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
            'funded_quantity': str(Decimal(str(line.ordered_qty))),
            'received_quantity': str(received_quantity),
            'unit_cost': str(line.unit_cost) if line.unit_cost is not None else None,
            'cost_effective_date': str(_purchase_order_date(order)),
            'original_vendor_id': int(order.vendor_id),
            'financial_vendor_id': int(vendor.id),
            'financial_account': account.display_name,
        }
        source_lines.append(source)
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
            'Assigned funded PO lines have missing Square identity or cost and cannot be '
            f'allocated safely. Review PO line(s): {line_ids}.'
        )
    if not eligible_variations:
        raise ValueError(
            'No usable funded purchase-order quantity is assigned to this Funding '
            'Account and vendor.'
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
        PurchaseOrderLine.ordered_qty > 0,
        PurchaseOrder.status.in_(QUALIFYING_ORDER_STATUSES),
    ).order_by(PurchaseOrder.ordered_at, PurchaseOrder.id, PurchaseOrderLine.id)).all()
    global_rows = list(global_candidates)
    account_by_payment_method = {
        int(row.payment_method_id): int(row.id)
        for row in db.scalars(select(FundingAccount).where(
            FundingAccount.account_type == 'CREDIT_CARD',
            FundingAccount.payment_method_id.is_not(None),
        )).all()
    }
    lots: list[_FundingAllocationLayer] = []
    for line, order, payment in global_rows:
        payment_method_id = int(payment.payment_method_id) if payment and payment.payment_method_id else None
        lot_account_id = account_by_payment_method.get(payment_method_id)
        # Funding allocation is limited to quantities attached to an actual Funding
        # Account.  An unrelated or unassigned PO is not an opening-inventory layer.
        if lot_account_id is None:
            continue
        funded_quantity = Decimal(str(line.ordered_qty))
        lots.append(_FundingAllocationLayer(
            order=order, line=line, payment=payment, account_id=lot_account_id,
            receipt_line_id=None, funded_at=_order_timestamp(order),
            quantity=funded_quantity, remaining=funded_quantity,
        ))
    lots.sort(key=lambda row: (
        _order_timestamp(row.order), int(row.order.id), int(row.line.id),
        row.receipt_line_id or 0,
    ))
    if not lots:
        raise ValueError('No funded purchase-order quantities are available for allocation.')
    return {
        'assigned_orders': assigned_orders,
        'eligible_variations': eligible_variations,
        'source_lines': source_lines,
        'setup_issues': setup_issues,
        'lots': lots,
        'oldest_funded_order_date': min(
            row.funded_at.astimezone(PORTAL_TIMEZONE).date() for row in lots
        ),
    }


def funding_report_required_coverage_start(
    db: Session, *, account: FundingAccount, vendor: Vendor, requested_start: date
) -> date:
    # Funding attribution is not receipt-based inventory accounting.  Square only
    # needs to cover the requested sales period; a PO receipt date is not an
    # eligibility boundary for a sale.
    return requested_start


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
    account, order, line, _payment = _funding_account_po_line(
        db, account_id=account_id, purchase_order_line_id=purchase_order_line_id)
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
    invalidated = _invalidate_drafts_for_po_line(
        db, account=account, order=order, line=line, actor_id=actor_id, ip=ip)
    prior = {
        'square_variation_id': str(line.variation_id or '') or None,
        'sku': str(line.sku or '') or None,
        'product': line.item_name,
        'variation': line.variation_name,
    }
    line.variation_id = identity.square_variation_id
    line.sku = identity.sku
    line.item_name = identity.product_name or identity.item_name or line.item_name
    line.variation_name = identity.variation_name or line.variation_name
    _audit(
        db,
        actor_id=actor_id,
        action='FUNDING_PO_LINE_IDENTITY_RESOLVED',
        entity_type='purchase_order_line',
        entity_id=int(line.id),
        after={
            'funding_account_id': int(account.id),
            'purchase_order_id': int(order.id),
            'prior_identity': prior,
            'square_variation_id': identity.square_variation_id,
            'sku': identity.sku,
            'product': line.item_name,
            'variation': line.variation_name,
            'resolution': 'OWNER_OVERRIDE',
            'reason': clean_reason,
            'invalidated_draft_report_ids': [row['report_id'] for row in invalidated],
            'finalized_history_preserved': True,
        },
        ip=ip,
    )
    db.flush()
    return line


def _funding_account_po_line(
    db: Session, *, account_id: int, purchase_order_line_id: int,
) -> tuple[FundingAccount, PurchaseOrder, PurchaseOrderLine, OrderPayment]:
    account = db.get(FundingAccount, account_id)
    line = db.get(PurchaseOrderLine, purchase_order_line_id)
    if account is None or line is None:
        raise LookupError('Assigned purchase-order line not found.')
    order = db.get(PurchaseOrder, line.purchase_order_id)
    payment = db.scalar(select(OrderPayment).where(
        OrderPayment.purchase_order_id == line.purchase_order_id,
    ))
    assigned = bool(order is not None and payment is not None)
    if assigned and account.account_type == 'CREDIT_CARD':
        assigned = payment.payment_method_id == account.payment_method_id
    elif assigned and account.account_type == 'CONSIGNMENT':
        assigned = (
            payment.vendor_id == account.vendor_id
            and payment.financial_treatment == 'REPLENISHMENT'
            and payment.payment_category_snapshot == 'CONSIGNMENT'
        )
    else:
        assigned = False
    if not assigned:
        raise ValueError('That PO line is not assigned to this Funding Account.')
    return account, order, line, payment


def funding_account_purchase_lines(
    db: Session, *, account: FundingAccount,
    include_resolution_candidates: bool = False,
) -> list[dict]:
    """Return authoritative PO lines currently assigned to a Funding account."""
    query = select(PurchaseOrder, PurchaseOrderLine, OrderPayment, Vendor).join(
        PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id
    ).join(
        OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id
    ).outerjoin(Vendor, Vendor.id == PurchaseOrder.vendor_id).where(
        PurchaseOrder.status.in_(QUALIFYING_ORDER_STATUSES),
        PurchaseOrderLine.removed.is_(False),
        PurchaseOrderLine.ordered_qty > 0,
    )
    if account.account_type == 'CREDIT_CARD':
        query = query.where(OrderPayment.payment_method_id == account.payment_method_id)
    else:
        query = query.where(
            OrderPayment.vendor_id == account.vendor_id,
            OrderPayment.financial_treatment == 'REPLENISHMENT',
            OrderPayment.payment_category_snapshot == 'CONSIGNMENT',
        )
    rows = db.execute(query.order_by(PurchaseOrder.id, PurchaseOrderLine.id)).all()
    candidates_by_sku: dict[str, list[OrderingCatalogIdentity]] = defaultdict(list)
    if include_resolution_candidates:
        identities = db.scalars(select(OrderingCatalogIdentity).where(
            OrderingCatalogIdentity.square_is_deleted.is_(False),
            OrderingCatalogIdentity.sku.is_not(None),
        ).order_by(OrderingCatalogIdentity.square_variation_id)).all()
        for identity in identities:
            candidates_by_sku[normalize_sku(identity.sku)].append(identity)
    receipt_evidence = _store_receipt_evidence(
        db, line_ids=[int(line.id) for _order, line, _payment, _vendor in rows])
    output = []
    for order, line, payment, vendor in rows:
        vendor = vendor or db.get(Vendor, payment.vendor_id)
        received, _received_at = _received_quantity(line, receipt_evidence)
        output.append({
            'purchase_order': order,
            'line': line,
            'payment': payment,
            'vendor': vendor,
            'received_units': received,
            'unit_cost': (
                Decimal(str(line.unit_cost)) if line.unit_cost is not None else None),
            'resolution_candidates': candidates_by_sku.get(
                normalize_sku(line.sku), []),
        })
    return output


def _report_line_uses_po_line(line: FundingReportLine, *, line_id: int) -> bool:
    return (
        line.purchase_order_line_id == line_id
        or str(line.warning_state or '') == f'PO_LINE:{line_id}'
    )


def _po_cost_report_impacts(
    db: Session, *, account_id: int, line_id: int, old_cost: Decimal,
    new_cost: Decimal,
) -> list[dict]:
    report_lines = db.scalars(select(FundingReportLine).join(
        FundingReport, FundingReport.id == FundingReportLine.report_id
    ).where(
        FundingReport.account_id == account_id,
        or_(
            FundingReportLine.purchase_order_line_id == line_id,
            FundingReportLine.warning_state == f'PO_LINE:{line_id}',
        ),
    )).all()
    report_ids = {int(row.report_id) for row in report_lines}
    reports = {row.id: row for row in db.scalars(select(FundingReport).where(
        FundingReport.id.in_(report_ids or [-1]))).all()}
    delta = new_cost - old_cost
    finalized_impacts = []
    for report_id in sorted(report_ids):
        report = reports[report_id]
        if report.status in {'DRAFT', 'VOIDED'}:
            continue
        quantity = sum((
            Decimal(str(row.net_units)) for row in report_lines
            if row.report_id == report_id and _report_line_uses_po_line(row, line_id=line_id)
        ), Decimal('0'))
        position = report_position(db, report_id=report_id)
        finalized_impacts.append({
            'report_id': report_id,
            'report_number': report.report_number,
            'status': report.status,
            'affected_units': str(quantity),
            'posted_cost_difference': str(money(quantity * delta)),
            'settled_amount': str(position['settled_amount']),
            'payment_history_preserved': position['settled_amount'] > 0,
        })
    return finalized_impacts


def _invalidate_drafts_for_po_line(
    db: Session, *, account: FundingAccount, order: PurchaseOrder,
    line: PurchaseOrderLine, actor_id: int, ip=None,
) -> list[dict]:
    draft_ids = set(db.scalars(select(FundingReportLine.report_id).join(
        FundingReport, FundingReport.id == FundingReportLine.report_id
    ).where(
        FundingReport.account_id == account.id,
        FundingReport.status == 'DRAFT',
        or_(
            FundingReportLine.purchase_order_line_id == line.id,
            FundingReportLine.warning_state == f'PO_LINE:{line.id}',
        ),
    )).all())
    drafts = list(db.scalars(select(FundingReport).where(
        FundingReport.id.in_(draft_ids or [-1])).order_by(FundingReport.id)).all())
    combined_parents = [row for row in db.scalars(select(FundingReport).where(
        FundingReport.account_id == account.id,
        FundingReport.vendor_id.is_(None),
        FundingReport.status == 'DRAFT',
    )).all() if draft_ids.intersection(
        (combined_report_metadata(row) or {}).get('member_report_ids', []))]
    invalidated = []
    for report in combined_parents + drafts:
        invalidated.append(delete_draft_report(
            db, report_id=int(report.id), actor_id=actor_id,
            reason=(
                f'Invalidated because authoritative PO {order.id} line {line.id} was corrected.'
            ), ip=ip))
    return invalidated


def correct_funding_po_line_cost(
    db: Session, *, account_id: int, purchase_order_line_id: int,
    unit_cost: Decimal, reason: str, actor_id: int, ip=None,
) -> dict:
    """Atomically correct the authoritative PO lot cost and its side effects."""
    with db.begin_nested():
        return _correct_funding_po_line_cost(
            db, account_id=account_id,
            purchase_order_line_id=purchase_order_line_id,
            unit_cost=unit_cost, reason=reason, actor_id=actor_id, ip=ip)


def _correct_funding_po_line_cost(
    db: Session, *, account_id: int, purchase_order_line_id: int,
    unit_cost: Decimal, reason: str, actor_id: int, ip=None,
) -> dict:
    """Correct the authoritative PO lot cost without rewriting posted reports."""
    account, order, line, _payment = _funding_account_po_line(
        db, account_id=account_id, purchase_order_line_id=purchase_order_line_id)
    value = Decimal(str(unit_cost))
    if not value.is_finite() or value < 0:
        raise ValueError('Unit cost must be a non-negative amount.')
    value = value.quantize(Decimal('0.0001'))
    note = reason.strip()
    if not note:
        raise ValueError('A reason is required for a cost correction.')
    if line.unit_cost is None:
        old_cost = Decimal('0.0000')
        old_cost_value = None
    else:
        old_cost = Decimal(str(line.unit_cost)).quantize(Decimal('0.0001'))
        old_cost_value = str(old_cost)
    if old_cost_value is not None and value == old_cost:
        raise ValueError('Enter a different unit cost.')

    finalized_impacts = _po_cost_report_impacts(
        db, account_id=account.id, line_id=int(line.id),
        old_cost=old_cost, new_cost=value)
    invalidated = _invalidate_drafts_for_po_line(
        db, account=account, order=order, line=line, actor_id=actor_id, ip=ip)

    line.unit_cost = value
    line.updated_at = datetime.now(timezone.utc)
    audit_after = {
        'funding_account_id': int(account.id),
        'purchase_order_id': int(order.id),
        'purchase_order_line_id': int(line.id),
        'product': line.item_name,
        'variation': line.variation_name,
        'square_variation_id': str(line.variation_id or '') or None,
        'sku': str(line.sku or '') or None,
        'old_unit_cost': old_cost_value,
        'new_unit_cost': str(value),
        'reason': note,
        'invalidated_draft_report_ids': [row['report_id'] for row in invalidated],
        'finalized_report_impacts': finalized_impacts,
        'finalized_history_preserved': True,
        'payment_history_preserved': True,
    }
    _audit(db, actor_id=actor_id, action='FUNDING_PO_LINE_COST_CORRECTED',
           entity_type='purchase_order_line', entity_id=int(line.id),
           after=audit_after, ip=ip)
    db.flush()
    return audit_after


def funding_po_cost_correction_history(
    db: Session, *, account_id: int,
) -> list[dict]:
    rows = db.scalars(select(AuditLog).where(
        AuditLog.action == 'FUNDING_PO_LINE_COST_CORRECTED'
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())).all()
    history = []
    for row in rows:
        after = dict((row.meta or {}).get('after', {}))
        if int(after.get('funding_account_id') or 0) != account_id:
            continue
        for key in ('old_unit_cost', 'new_unit_cost'):
            if after.get(key) is not None:
                after[key] = Decimal(str(after[key]))
        impacts = []
        for raw_impact in after.get('finalized_report_impacts', []):
            impact = dict(raw_impact)
            for key in ('affected_units', 'posted_cost_difference', 'settled_amount'):
                if impact.get(key) is not None:
                    impact[key] = Decimal(str(impact[key]))
            impacts.append(impact)
        after['finalized_report_impacts'] = impacts
        history.append({'audit': row, **after})
    return history


def _apply_funding_allocation_history(
    db: Session, *, scope: dict, account: FundingAccount, vendor: Vendor,
    through_date: date,
) -> tuple[list[dict], list[dict]]:
    """Allocate cached Square events against funded quantities and return positions."""
    variations = scope['eligible_variations']
    sales = db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.business_date <= through_date,
        ConsignmentSaleFact.square_variation_id.in_(variations),
    ).order_by(ConsignmentSaleFact.transacted_at, ConsignmentSaleFact.id)).all()
    returns = db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.business_date <= through_date,
        ConsignmentReturnFact.square_variation_id.in_(variations),
    ).order_by(ConsignmentReturnFact.returned_at, ConsignmentReturnFact.id)).all()
    events = [(row.transacted_at, 0, int(row.id), row, False) for row in sales]
    events += [(row.returned_at, 1, int(row.id), row, True) for row in returns]
    events.sort(key=lambda row: (_utc(row[0]), row[1], row[2]))
    lots_by_variation: dict[str, list[_FundingAllocationLayer]] = defaultdict(list)
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
                if lot.remaining <= 0:
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
        funded = Decimal(str(line.ordered_qty))
        candidates = [] if str(line.variation_id or '').strip() else _catalog_matches_for_sku(
            db, sku=line.sku
        )
        issue = None
        if funded > 0 and not str(line.variation_id or '').strip():
            issue = 'Product identity unresolved' if len(candidates) != 1 else 'Unique SKU identity awaiting resolution'
        if funded > 0 and line.unit_cost is None:
            issue = 'Missing saved cost' if issue is None else f'{issue}; missing saved cost'
        row = {
            'purchase_order_id': int(order.id),
            'purchase_order_line_id': int(line.id),
            'vendor': vendor,
            'sku': str(line.sku or ''),
            'product': line.item_name,
            'variation': line.variation_name,
            'square_variation_id': str(line.variation_id or '').strip() or None,
            'funded_units': funded,
            'received_units': received,
            'unit_cost': Decimal(str(line.unit_cost)) if line.unit_cost is not None else None,
            'original_value': money(funded * Decimal(str(line.unit_cost))) if line.unit_cost is not None else None,
            'remaining_units': None,
            'remaining_value': None,
            'sold_units': None,
            'sold_cogs': None,
            'issue': issue,
            'resolution_candidates': candidates,
        }
        lines.append(row)
        if funded > 0:
            original_units += funded
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
        if as_of is None:
            history_complete = False
            history_blockers.append(
                f'{vendor.name}: Square coverage is unavailable; remaining funded '
                'quantity cannot be calculated.'
            )
            continue
        positions, unallocated = _apply_funding_allocation_history(
            db, scope=scope, account=account, vendor=vendor, through_date=as_of
        )
        if unallocated:
            history_complete = False
            history_blockers.append(
                f'{vendor.name}: sales or returns exceed configured funded quantity; '
                'remaining funded quantity is unavailable.'
            )
            issues.append({
                'purchase_order_id': None,
                'purchase_order_line_id': None,
                'vendor': vendor,
                'issue': 'Sales or returns exceed configured funded quantity',
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


def _populate_credit_card_funding_report(
    db: Session, *, report: FundingReport, account: FundingAccount, vendor: Vendor,
    scope: dict, start_date: date, end_date: date, store_ids: list[int],
    filter_text: str, normalized_filter: str, product_filter: str,
) -> dict:
    report.inventory_snapshot_at = None
    variations = scope['eligible_variations']
    sales = db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.business_date.between(start_date, end_date),
        ConsignmentSaleFact.square_variation_id.in_(variations),
    ).order_by(ConsignmentSaleFact.transacted_at, ConsignmentSaleFact.id)).all()
    returns = db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.business_date.between(start_date, end_date),
        ConsignmentReturnFact.square_variation_id.in_(variations),
    ).order_by(ConsignmentReturnFact.returned_at, ConsignmentReturnFact.id)).all()
    groups = {}
    for fact, is_return in [(row, False) for row in sales] + [(row, True) for row in returns]:
        if not _fact_matches_report_filter(
            fact, start_date=start_date, end_date=end_date, store_ids=store_ids,
            filter_text=filter_text, normalized_filter=normalized_filter,
            product_filter=product_filter,
        ):
            continue
        quantity = Decimal(str(fact.quantity_returned if is_return else fact.quantity_sold))
        if quantity <= 0:
            if is_return:
                raise ValueError(f'Return fact {fact.id} has no usable quantity.')
            continue
        variation_id = str(fact.square_variation_id).strip()
        order, po_line = scope['products'][variation_id]
        if po_line.unit_cost is None:
            raise ValueError(
                f'Missing saved cost for PO line {po_line.id} ({variation_id}); '
                'period payable cannot be calculated.'
            )
        group = groups.setdefault((variation_id, fact.store_id), {
            'order': order, 'po_line': po_line, 'fact': fact,
            'sold': Decimal('0'), 'returned': Decimal('0'), 'links': [],
        })
        group['returned' if is_return else 'sold'] += quantity
        group['links'].append((fact, is_return, quantity))

    for (variation_id, store_id), group in groups.items():
        order, po_line, fact = group['order'], group['po_line'], group['fact']
        unit_cost = Decimal(str(po_line.unit_cost))
        net = group['sold'] - group['returned']
        line = FundingReportLine(
            report_id=report.id,
            purchase_order_line_id=int(po_line.id),
            normalized_sku=normalize_sku(po_line.sku) or variation_id,
            sku_snapshot=fact.sku_snapshot or po_line.sku or variation_id,
            square_variation_id=variation_id,
            product_name_snapshot=fact.product_name_snapshot or po_line.item_name,
            variation_name_snapshot=fact.variation_name_snapshot or po_line.variation_name,
            store_id=store_id,
            units_sold=group['sold'], units_returned=group['returned'], net_units=net,
            unit_cost_snapshot=unit_cost, extended_cogs=money(net * unit_cost),
            mapping_effective_date_snapshot=_purchase_order_date(order),
            source_transaction_count=len(group['links']),
            warning_state=f'PO_LINE:{po_line.id}',
        )
        db.add(line)
        db.flush()
        for fact, is_return, quantity in group['links']:
            db.add(FundingReportFactLink(
                report_id=report.id, report_line_id=line.id,
                sale_fact_id=None if is_return else fact.id,
                return_fact_id=fact.id if is_return else None,
                allocated_quantity=quantity,
                cogs_amount_snapshot=money(quantity * unit_cost * (-1 if is_return else 1)),
            ))
        report.units_sold += group['sold']
        report.units_returned += group['returned']
        report.net_units += net
        report.calculated_cogs += line.extended_cogs
    return {
        'message': (
            'Period sales and returns for products on POs paid by this credit card, '
            'using the latest mapped PO cost per product. PO quantities and receipt '
            'chronology do not limit report activity.'
        ),
        'purchase_order_ids': sorted(scope['assigned_orders']),
        'assigned_purchase_order_count': len(scope['assigned_orders']),
        'eligible_skus': sorted(variations),
        'eligible_sku_count': len(variations),
        'source_lines': scope['source_lines'],
        'setup_issues': scope['setup_issues'],
        'allocation_method': 'MAPPED_PO_PRODUCT_SALES',
        'allocation_semantics': 'MAPPED_PO_PRODUCT_SALES',
        'cost_selection': 'Latest mapped PO date, then PO line ID; independent of sale date',
        'fifo_exception_count': 0,
    }



def _populate_consignment_funding_report(
    db: Session, *, report: FundingReport, account: FundingAccount, vendor: Vendor,
    scope: dict, start_date: date, end_date: date, store_ids: list[int],
    filter_text: str, normalized_filter: str, product_filter: str,
) -> dict:
    """Allocate consignment sales against funded PO quantity without date gating."""
    eligible_skus = scope['eligible_skus']
    eligible_variations = scope['eligible_variations']

    def account_mapping(fact) -> FundingSkuMapping | None:
        fact_variation = str(fact.square_variation_id or '').strip()
        fact_sku = normalize_sku(fact.sku_snapshot)
        for mapping in scope['account_mappings']:
            if not (
                mapping.effective_start_date <= fact.business_date
                and (
                    mapping.effective_end_date is None
                    or mapping.effective_end_date >= fact.business_date
                )
            ):
                continue
            if (
                fact_variation
                and fact_variation == str(mapping.square_variation_id or '').strip()
            ) or (fact_sku and fact_sku == normalize_sku(mapping.normalized_sku)):
                return mapping
        return None

    def candidate(fact) -> bool:
        return (
            normalize_sku(fact.sku_snapshot) in eligible_skus
            or str(fact.square_variation_id or '').strip() in eligible_variations
            or account_mapping(fact) is not None
        )

    sales = [row for row in db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.business_date <= end_date,
    ).order_by(ConsignmentSaleFact.transacted_at, ConsignmentSaleFact.id)).all()
        if candidate(row)]
    returns = [row for row in db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.business_date <= end_date,
    ).order_by(ConsignmentReturnFact.returned_at, ConsignmentReturnFact.id)).all()
        if candidate(row)]
    events = [(row.transacted_at, 0, int(row.id), row, False) for row in sales]
    events += [(row.returned_at, 1, int(row.id), row, True) for row in returns]
    events.sort(key=lambda row: (_utc(row[0]), row[1], row[2]))

    lots_by_sku: dict[str, list[_FundingAllocationLayer]] = defaultdict(list)
    for lot in scope['lots']:
        lots_by_sku[normalize_sku(lot.line.sku)].append(lot)
    catalog_by_variation = {
        str(row.square_variation_id): row
        for row in db.scalars(select(OrderingCatalogIdentity).where(
            OrderingCatalogIdentity.square_variation_id.in_(
                eligible_variations or {'__none__'}
            ),
            OrderingCatalogIdentity.square_is_deleted.is_(False),
        )).all()
    }

    sale_allocations: dict[int, list[dict]] = defaultdict(list)
    allocation_history: dict[str, list[dict]] = defaultdict(list)
    sold_through: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    included_allocations = []
    unallocated_history = []
    detected_sales = detected_returns = Decimal('0')
    exception_units = Decimal('0')
    exception_count = 0

    for _event_at, _event_type, _event_id, fact, is_return in events:
        quantity = Decimal(str(
            fact.quantity_returned if is_return else fact.quantity_sold
        ))
        matches_filter = _fact_matches_report_filter(
            fact, start_date=start_date, end_date=end_date, store_ids=store_ids,
            filter_text=filter_text, normalized_filter=normalized_filter,
            product_filter=product_filter,
        )
        if matches_filter:
            if is_return:
                detected_returns += max(quantity, Decimal('0'))
            else:
                detected_sales += max(quantity, Decimal('0'))
        if quantity <= 0:
            if is_return and matches_filter:
                raise ValueError(
                    f'Return fact {fact.id} has no usable quantity and cannot be allocated safely.'
                )
            continue

        variation_id = str(fact.square_variation_id or '').strip()
        membership_mapping = account_mapping(fact)
        sku = normalize_sku(fact.sku_snapshot) or normalize_sku(
            membership_mapping.sku_snapshot if membership_mapping else None
        )
        allocation_key = sku or f'VARIATION:{variation_id}'
        allocations = []
        remaining = quantity
        if not is_return:
            sold_through[allocation_key] += quantity
            for lot in lots_by_sku.get(sku, []):
                if remaining <= 0:
                    break
                if lot.remaining <= 0:
                    continue
                allocated = min(remaining, lot.remaining)
                lot.remaining -= allocated
                row = {'lot': lot, 'quantity': allocated, 'returnable': allocated}
                allocations.append(row)
                sale_allocations[int(fact.id)].append(row)
                allocation_history[allocation_key].append(row)
                remaining -= allocated
        else:
            candidates = list(sale_allocations.get(
                int(fact.original_sale_fact_id or 0), []
            ))
            if not candidates:
                candidates = list(allocation_history.get(allocation_key, []))
            for original in reversed(candidates):
                if remaining <= 0:
                    break
                if original['returnable'] <= 0:
                    continue
                reversed_quantity = min(remaining, original['returnable'])
                original['returnable'] -= reversed_quantity
                original['lot'].remaining += reversed_quantity
                allocations.append({
                    'lot': original['lot'], 'quantity': reversed_quantity,
                })
                remaining -= reversed_quantity

        if remaining > 0:
            issue = (
                'UNRESOLVED_CATALOG_IDENTITY'
                if not sku
                else 'FUNDED_CAPACITY_EXCEEDED'
            )
            unallocated_history.append({
                'source_type': 'RETURN' if is_return else 'SALE',
                'source_id': int(fact.id),
                'variation_id': variation_id,
                'quantity': str(remaining),
                'business_date': str(fact.business_date),
                'issue': issue,
            })
            if matches_filter:
                if is_return:
                    raise ValueError(
                        'Funding-allocation return data is incomplete for '
                        f'{fact.product_name_snapshot or "Unknown item"}'
                        + (f' · {fact.variation_name_snapshot}'
                           if fact.variation_name_snapshot else '')
                        + f' on {fact.business_date}. {remaining} returned unit(s) '
                        'could not be matched to a prior allocated sale.'
                    )
                catalog = catalog_by_variation.get(variation_id)
                db.add(FundingReportFifoException(
                    report_id=report.id,
                    sale_fact_id=int(fact.id),
                    square_variation_id=variation_id or 'MISSING',
                    product_name_snapshot=(
                        str(catalog.item_name or catalog.product_name or '').strip()
                        if catalog else ''
                    ) or fact.product_name_snapshot or 'Unknown item',
                    variation_name_snapshot=(
                        str(catalog.variation_name or '').strip() if catalog else ''
                    ) or fact.variation_name_snapshot,
                    sku_snapshot=(
                        str(catalog.sku or '').strip() if catalog else ''
                    ) or (
                        str(membership_mapping.sku_snapshot or '').strip()
                        if membership_mapping else ''
                    ) or fact.sku_snapshot,
                    store_id=fact.store_id,
                    sale_business_date=fact.business_date,
                    sale_transacted_at=fact.transacted_at,
                    quantity_affected=remaining,
                    sold_through_quantity=sold_through[allocation_key],
                    received_through_quantity=sum((
                        lot.quantity for lot in lots_by_sku.get(sku, [])
                    ), Decimal('0')),
                    status='PENDING',
                    cost_basis=issue,
                ))
                exception_units += remaining
                exception_count += 1

        if not matches_filter:
            continue
        for allocation in allocations:
            included_allocations.append({
                'lot': allocation['lot'],
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
            'lot': lot,
            'store_id': fact.store_id,
            'product': fact.product_name_snapshot or lot.line.item_name,
            'variation': fact.variation_name_snapshot or lot.line.variation_name,
            'sku': fact.sku_snapshot or lot.line.sku or '',
            'square_variation_id': str(fact.square_variation_id or lot.line.variation_id or ''),
            'sold': Decimal('0'),
            'returned': Decimal('0'),
            'links': {},
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
            'square_variation_id': str(fact.square_variation_id or ''),
            'source_type': 'RETURN' if is_return else 'SALE',
            'source_id': int(fact.id),
            'business_date': str(fact.business_date),
            'quantity': str(quantity),
            'unit_cost': str(lot.line.unit_cost),
            'cogs': str(-signed_cogs if is_return else signed_cogs),
        })

    grouped_lot_keys = {key[0] for key in groups}
    for lot in scope['lots']:
        if lot.key in grouped_lot_keys:
            continue
        groups[(lot.key, None)] = {
            'lot': lot,
            'store_id': None,
            'product': lot.line.item_name,
            'variation': lot.line.variation_name,
            'sku': lot.line.sku or '',
            'square_variation_id': str(lot.line.variation_id or ''),
            'sold': Decimal('0'),
            'returned': Decimal('0'),
            'links': {},
        }

    inventory_recorded_for_lot = set()
    for (_lot_key, _store_id), group in groups.items():
        lot = group['lot']
        unit_cost = Decimal(str(lot.line.unit_cost))
        net = group['sold'] - group['returned']
        inventory_quantity = inventory_value = Decimal('0')
        if lot.key not in inventory_recorded_for_lot:
            inventory_recorded_for_lot.add(lot.key)
            inventory_quantity = lot.remaining
            inventory_value = money(lot.remaining * unit_cost)
        line = FundingReportLine(
            report_id=report.id,
            mapping_id=None,
            purchase_order_line_id=int(lot.line.id),
            purchase_order_receipt_line_id=None,
            lot_received_at_snapshot=None,
            normalized_sku=normalize_sku(lot.line.sku),
            sku_snapshot=group['sku'] or str(lot.line.sku or ''),
            square_variation_id=group['square_variation_id'] or None,
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
            mapping_effective_date_snapshot=lot.funded_at.astimezone(
                PORTAL_TIMEZONE
            ).date(),
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
            'This consignment report allocates sales to the oldest outstanding '
            'funded purchase-order quantity for each SKU.'
        ),
        'purchase_order_ids': sorted(scope['orders']),
        'assigned_purchase_order_count': len(scope['orders']),
        'eligible_skus': sorted(scope['eligible_skus']),
        'eligible_sku_count': len(scope['eligible_skus']),
        'source_lines': [
            {key: value for key, value in row.items() if key not in {'line', 'order_date'}}
            for row in scope['source_lines']
        ],
        'setup_issues': scope['setup_issues'],
        'allocation_method': 'FIFO',
        'allocation_semantics': 'OLDEST_OUTSTANDING_FUNDED_QUANTITY',
        'lot_ordering': (
            'Oldest funded purchase order first; order and receipt dates do not '
            'determine sale eligibility.'
        ),
        'oldest_funded_order_date': str(scope['oldest_funded_order_date']),
        'fifo_allocations': reconciliation,
        'unallocated_history': unallocated_history,
        'fifo_exception_count': exception_count,
        'sales_reconciliation': {
            'square_units_detected': str(detected_sales),
            'allocated_units': str(report.units_sold),
            'exception_units': str(exception_units),
            'returns_detected': str(detected_returns),
        },
    }


def normalize_draft_funding_allocation(
    db: Session, *, report: FundingReport, actor_id: int, ip=None,
) -> bool:
    """Refresh legacy card drafts to product sales; preserve finalized snapshots.

    Consignment retains its existing pending-exception normalization rules.
    """
    if report.status != 'DRAFT' or report.account_type_snapshot not in {
        'CREDIT_CARD', 'CONSIGNMENT'
    }:
        return False
    exceptions = funding_report_fifo_exceptions(db, report_id=report.id)
    if report.account_type_snapshot == 'CONSIGNMENT':
        if not exceptions or any(row.status != 'PENDING' for row in exceptions):
            return False
    elif ((report.warning_summary or {}).get('purchase_order_scope') or {}).get(
        'allocation_semantics'
    ) == 'MAPPED_PO_PRODUCT_SALES' and not exceptions:
        return False
    account = db.get(FundingAccount, report.account_id)
    vendor = db.get(Vendor, report.vendor_id) if report.vendor_id is not None else None
    if account is None or vendor is None:
        return False

    db.execute(delete(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id
    ))
    db.execute(delete(FundingReportExclusion).where(
        FundingReportExclusion.report_id == report.id
    ))
    db.execute(delete(FundingReportFifoException).where(
        FundingReportFifoException.report_id == report.id
    ))
    db.execute(delete(FundingReportLine).where(
        FundingReportLine.report_id == report.id
    ))
    report.units_sold = Decimal('0')
    report.units_returned = Decimal('0')
    report.net_units = Decimal('0')
    report.calculated_cogs = Decimal('0')
    report.inventory_units_snapshot = Decimal('0')
    report.inventory_value_snapshot = Decimal('0')

    filter_text = str(report.sku_filter or '').strip()
    if account.account_type == 'CONSIGNMENT':
        scope = _consignment_order_scope(
            db,
            account=account,
            start_date=report.sales_start_date,
            end_date=report.sales_end_date,
        )
        source_summary = _populate_consignment_funding_report(
            db, report=report, account=account, vendor=vendor, scope=scope,
            start_date=report.sales_start_date, end_date=report.sales_end_date,
            store_ids=list(report.store_ids or []), filter_text=filter_text,
            normalized_filter=normalize_sku(filter_text),
            product_filter=filter_text.casefold(),
        )
        purchase_order_ids = sorted(scope['orders'])
    else:
        scope = _credit_card_product_scope(db, account=account, vendor=vendor)
        source_summary = _populate_credit_card_funding_report(
            db,
            report=report,
            account=account,
            vendor=vendor,
            scope=scope,
            start_date=report.sales_start_date,
            end_date=report.sales_end_date,
            store_ids=list(report.store_ids or []),
            filter_text=filter_text,
            normalized_filter=normalize_sku(filter_text),
            product_filter=filter_text.casefold(),
        )
        purchase_order_ids = sorted(scope['assigned_orders'])
    warning_summary = dict(report.warning_summary or {})
    warning_summary['purchase_order_scope'] = source_summary
    warning_summary['fifo_exceptions'] = {
        'pending': source_summary.get('fifo_exception_count', 0),
        'ignored': 0,
        'included': 0,
    }
    warning_summary['vendor_purchase_order_ids'] = purchase_order_ids
    report.warning_summary = warning_summary
    _audit(
        db,
        actor_id=actor_id,
        action='FUNDING_DRAFT_ATTRIBUTION_NORMALIZED',
        entity_type='funding_report',
        entity_id=report.id,
        after={
            'semantics': 'OLDEST_OUTSTANDING_FUNDED_QUANTITY',
            'removed_pending_exception_count': len(exceptions),
            'remaining_pending_exception_count': source_summary.get(
                'fifo_exception_count', 0
            ),
            'calculated_cogs': str(report.calculated_cogs),
        },
        ip=ip,
    )
    db.flush()
    return True


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
        order_scope = _consignment_order_scope(
            db, account=account, start_date=start_date, end_date=end_date
        )
        coverage_start_date = start_date
    else:
        credit_card_scope = _credit_card_product_scope(
            db, account=account, vendor=vendor)
        credit_card_order_ids = sorted(credit_card_scope['assigned_orders'])
        coverage_start_date = start_date
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
        source_summary = _populate_credit_card_funding_report(
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
            'fifo_exceptions': {
                'pending': source_summary.get('fifo_exception_count', 0),
                'ignored': 0,
                'included': 0,
            },
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
    if order_scope is not None:
        source_summary = _populate_consignment_funding_report(
            db,
            report=report,
            account=account,
            vendor=vendor,
            scope=order_scope,
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
            'fifo_exceptions': {
                'pending': source_summary.get('fifo_exception_count', 0),
                'ignored': 0,
                'included': 0,
            },
            'vendor_purchase_order_ids': sorted(order_scope['orders']),
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
                'eligible_sku_count': source_summary['eligible_sku_count'],
                'allocation_method': 'FIFO',
                'sales_reconciliation': source_summary['sales_reconciliation'],
            },
            ip=ip,
        )
        db.flush()
        return report
    raise RuntimeError(f'Unsupported funding account type: {account.account_type}')


def calculate_combined_report(
    db: Session,
    *,
    account_id: int,
    start_date: date,
    end_date: date,
    store_ids: list[int],
    sku_filter: str,
    internal_note: str,
    actor_id: int,
    overlap_acknowledged: bool = False,
    ip=None,
) -> FundingReport:
    account = db.get(FundingAccount, account_id)
    if account is None or not account.is_active:
        raise ValueError('Choose an active funding account.')
    vendors = eligible_vendors_for_account(db, account=account)
    if not vendors:
        raise ValueError('This Funding Account has no eligible vendors.')
    if end_date < start_date or end_date > date.today():
        raise ValueError('Choose a valid, non-future sales period.')

    member_reports = []
    created_member_ids = []
    all_overlaps = []
    for vendor in vendors:
        overlaps = overlapping_reports(
            db,
            account_id=account.id,
            vendor_id=vendor.id,
            start_date=start_date,
            end_date=end_date,
        )
        all_overlaps.extend(overlaps)
        if overlaps and not overlap_acknowledged:
            raise ValueError('OVERLAP_ACKNOWLEDGEMENT_REQUIRED')
        existing = _matching_vendor_report(
            db,
            account_id=account.id,
            vendor_id=vendor.id,
            start_date=start_date,
            end_date=end_date,
            store_ids=store_ids,
            sku_filter=sku_filter,
        )
        if existing is not None:
            if account.account_type == 'CREDIT_CARD':
                normalize_draft_funding_allocation(db, report=existing, actor_id=actor_id, ip=ip)
            member_reports.append(existing)
            continue
        report = calculate_report(
            db,
            account_id=account.id,
            vendor_id=vendor.id,
            start_date=start_date,
            end_date=end_date,
            store_ids=store_ids,
            sku_filter=sku_filter,
            internal_note=internal_note,
            overlap_acknowledged=overlap_acknowledged,
            actor_id=actor_id,
            ip=ip,
        )
        duplicates = duplicate_finalized_fact_links(db, report=report)
        if duplicates:
            raise ValueError(
                f'{vendor.name} overlaps finalized report(s) '
                + ', '.join(str(value) for value in duplicates)
                + '. The same Square activity cannot become payable twice.'
            )
        member_reports.append(report)
        created_member_ids.append(int(report.id))

    parent = FundingReport(
        account_id=account.id,
        vendor_id=None,
        report_number=(
            f'COGS-ALL-{account.id}-{start_date:%Y%m%d}-{end_date:%Y%m%d}-'
            f'{uuid4().hex[:8].upper()}'
        ),
        account_name_snapshot=account.display_name,
        account_type_snapshot=account.account_type,
        sales_start_date=start_date,
        sales_end_date=end_date,
        store_ids=sorted(set(store_ids)),
        sku_filter=sku_filter.strip() or None,
        internal_note=internal_note.strip() or None,
        overlap_acknowledged=bool(all_overlaps and overlap_acknowledged),
        overlapping_report_ids=sorted({int(row.id) for row in all_overlaps}),
        status='DRAFT',
        units_sold=sum((row.units_sold for row in member_reports), Decimal('0')),
        units_returned=sum((row.units_returned for row in member_reports), Decimal('0')),
        net_units=sum((row.net_units for row in member_reports), Decimal('0')),
        calculated_cogs=money(sum(
            (row.calculated_cogs for row in member_reports), Decimal('0')
        )),
        created_by_principal_id=actor_id,
    )
    parent.warning_summary = {
        'combined_report': {
            'version': 1,
            'member_report_ids': [int(row.id) for row in member_reports],
            'created_member_report_ids': created_member_ids,
            'vendor_count': len(member_reports),
        }
    }
    db.add(parent)
    db.flush()
    _audit(
        db,
        actor_id=actor_id,
        action='FUNDING_COMBINED_REPORT_CALCULATED',
        entity_type='funding_report',
        entity_id=parent.id,
        after={
            'account_id': account.id,
            'sales_start_date': str(start_date),
            'sales_end_date': str(end_date),
            'member_report_ids': [int(row.id) for row in member_reports],
            'calculated_cogs': str(parent.calculated_cogs),
            'overlap_acknowledged': parent.overlap_acknowledged,
        },
        ip=ip,
    )
    return parent


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
    if is_combined_report(report):
        members = combined_report_members(db, report=report)
        vendor_positions = [report_position(db, report_id=row.id) for row in members]
        adjusted = money(sum(
            (row['adjusted_amount'] for row in vendor_positions), Decimal('0')
        ))
        settled = money(sum(
            (row['settled_amount'] for row in vendor_positions), Decimal('0')
        ))
        remaining = money(sum(
            (row['remaining_amount'] for row in vendor_positions), Decimal('0')
        ))
        return {
            'report': report,
            'charges': money(sum(
                (row['charges'] for row in vendor_positions), Decimal('0')
            )),
            'credits': money(sum(
                (row['credits'] for row in vendor_positions), Decimal('0')
            )),
            'adjusted_amount': adjusted,
            'settled_amount': settled,
            'remaining_amount': remaining,
            'replenishment_applied': money(sum(
                (row['replenishment_applied'] for row in vendor_positions), Decimal('0')
            )),
            'cash_settlement': money(sum(
                (row['cash_settlement'] for row in vendor_positions), Decimal('0')
            )),
            'adjustments': [],
            'allocations': [],
            'vendor_positions': vendor_positions,
        }
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


def report_position_for_display(db: Session, *, report_id: int) -> dict:
    """Return a non-actionable snapshot when legacy combined lineage is broken."""
    try:
        position = report_position(db, report_id=report_id)
        return {**position, 'position_available': True, 'warning': None}
    except ValueError as exc:
        report = db.get(FundingReport, report_id)
        if report is None or not is_combined_report(report):
            raise
        _, missing_member_ids = combined_report_member_state(db, report=report)
        if not missing_member_ids:
            raise
        snapshot = report.finalized_snapshot or {}
        adjusted = money(snapshot.get('adjusted_amount', report.calculated_cogs))
        return {
            'report': report,
            'charges': Decimal('0.00'),
            'credits': Decimal('0.00'),
            'adjusted_amount': adjusted,
            'settled_amount': Decimal('0.00'),
            'remaining_amount': Decimal('0.00'),
            'replenishment_applied': Decimal('0.00'),
            'cash_settlement': Decimal('0.00'),
            'adjustments': [],
            'allocations': [],
            'vendor_positions': [],
            'position_available': False,
            'warning': str(exc),
        }


def funding_report_fifo_exceptions(
    db: Session, *, report_id: int
) -> list[FundingReportFifoException]:
    return list(db.scalars(select(FundingReportFifoException).where(
        FundingReportFifoException.report_id == report_id
    ).order_by(
        FundingReportFifoException.sale_transacted_at,
        FundingReportFifoException.id,
    )).all())


def resolve_funding_report_fifo_exception(
    db: Session,
    *,
    report_id: int,
    exception_id: int,
    action: str,
    reason: str,
    actor_id: int,
    unit_cost: Decimal | None = None,
    ip=None,
) -> FundingReportFifoException:
    report = db.get(FundingReport, report_id)
    exception = db.get(FundingReportFifoException, exception_id)
    if report is None or exception is None or exception.report_id != report.id:
        raise LookupError('Funding-capacity exception not found.')
    if report.status != 'DRAFT' or report.finalized_at is not None:
        raise ValueError(
            'Funding-capacity exceptions can only be resolved on an unfinalized draft.'
        )
    if exception.status != 'PENDING':
        raise ValueError('This funding-capacity exception has already been resolved.')
    normalized_action = str(action or '').strip().upper()
    if normalized_action not in {'IGNORE', 'INCLUDE'}:
        raise ValueError('Choose Ignore or Include Anyway.')
    resolution_reason = str(reason or '').strip()
    if not resolution_reason:
        raise ValueError('A reason is required for this accounting decision.')

    sale = db.get(ConsignmentSaleFact, exception.sale_fact_id)
    if sale is None:
        raise ValueError('The source Square sale is no longer available.')
    resolved_at = datetime.now(timezone.utc)
    if normalized_action == 'IGNORE':
        exception.status = 'IGNORED'
        exception.cost_basis = 'EXCLUDED_FROM_REPORT'
        db.add(FundingReportExclusion(
            report_id=report.id,
            source_type='SALE',
            source_id=sale.id,
            reason_code='FIFO_EXCEPTION_OWNER_IGNORED',
            sku_snapshot=exception.sku_snapshot,
            product_name_snapshot=exception.product_name_snapshot,
            variation_name_snapshot=exception.variation_name_snapshot,
            store_id=exception.store_id,
            quantity_snapshot=exception.quantity_affected,
            amount_snapshot=None,
        ))
        audit_action = 'FUNDING_FIFO_EXCEPTION_IGNORED'
    else:
        chosen_cost = Decimal(str(unit_cost)) if unit_cost is not None else Decimal('-1')
        if chosen_cost <= 0:
            raise ValueError('Enter a positive unit cost to include this sale.')
        exception.status = 'INCLUDED'
        exception.unit_cost_snapshot = chosen_cost
        exception.cost_basis = 'OWNER_ENTERED_UNIT_COST'
        quantity = Decimal(str(exception.quantity_affected))
        extended_cogs = money(quantity * chosen_cost)
        line = FundingReportLine(
            report_id=report.id,
            mapping_id=None,
            purchase_order_line_id=None,
            purchase_order_receipt_line_id=None,
            lot_received_at_snapshot=None,
            normalized_sku=normalize_sku(exception.sku_snapshot) or exception.square_variation_id,
            sku_snapshot=exception.sku_snapshot or 'No SKU',
            square_variation_id=exception.square_variation_id,
            product_name_snapshot=exception.product_name_snapshot,
            variation_name_snapshot=exception.variation_name_snapshot,
            store_id=exception.store_id,
            units_sold=quantity,
            units_returned=Decimal('0'),
            net_units=quantity,
            unit_cost_snapshot=chosen_cost,
            extended_cogs=extended_cogs,
            inventory_units_snapshot=Decimal('0'),
            inventory_value_snapshot=Decimal('0'),
            mapping_effective_date_snapshot=exception.sale_business_date,
            source_transaction_count=1,
            warning_state=f'FIFO_OVERRIDE:{exception.id}',
        )
        db.add(line)
        db.flush()
        db.add(FundingReportFactLink(
            report_id=report.id,
            report_line_id=line.id,
            sale_fact_id=sale.id,
            return_fact_id=None,
            allocated_quantity=quantity,
            cogs_amount_snapshot=extended_cogs,
        ))
        report.units_sold += quantity
        report.net_units += quantity
        report.calculated_cogs += extended_cogs
        audit_action = 'FUNDING_FIFO_EXCEPTION_INCLUDED'

    exception.resolution_reason = resolution_reason
    exception.resolved_by_principal_id = actor_id
    exception.resolved_at = resolved_at
    summary = dict(report.warning_summary or {})
    exceptions = funding_report_fifo_exceptions(db, report_id=report.id)
    summary['fifo_exceptions'] = {
        'pending': sum(row.status == 'PENDING' for row in exceptions),
        'ignored': sum(row.status == 'IGNORED' for row in exceptions),
        'included': sum(row.status == 'INCLUDED' for row in exceptions),
    }
    report.warning_summary = summary
    _audit(
        db,
        actor_id=actor_id,
        action=audit_action,
        entity_type='funding_report_fifo_exception',
        entity_id=exception.id,
        after={
            'report_id': report.id,
            'sale_fact_id': sale.id,
            'square_variation_id': exception.square_variation_id,
            'quantity': str(exception.quantity_affected),
            'status': exception.status,
            'cost_basis': exception.cost_basis,
            'unit_cost': str(exception.unit_cost_snapshot) if exception.unit_cost_snapshot is not None else None,
            'reason': resolution_reason,
        },
        ip=ip,
    )
    db.flush()
    return exception


def finalize_report(db: Session, *, report_id: int, actor_id: int, ip=None) -> FundingReport:
    report = db.get(FundingReport, report_id)
    if report is None:
        raise LookupError('Report not found.')
    if report.status != 'DRAFT':
        raise ValueError('Only a draft report can be finalized.')
    if is_combined_report(report):
        members = combined_report_members(db, report=report)
        if any(row.status == 'VOIDED' for row in members):
            raise ValueError('A vendor report in this combined report has been voided.')
        for member in members:
            if member.status == 'DRAFT':
                finalize_report(db, report_id=member.id, actor_id=actor_id, ip=ip)
        position = report_position(db, report_id=report.id)
        report.finalized_snapshot = {
            'combined_report': True,
            'account': report.account_name_snapshot,
            'sales_start_date': str(report.sales_start_date),
            'sales_end_date': str(report.sales_end_date),
            'member_reports': [
                {
                    'report_id': member.id,
                    'vendor_id': member.vendor_id,
                    'calculated_cogs': str(member.calculated_cogs),
                }
                for member in members
            ],
            'adjusted_amount': str(position['adjusted_amount']),
        }
        report.status = 'FINALIZED'
        report.finalized_at = datetime.now(timezone.utc)
        report.finalized_by_principal_id = actor_id
        _audit(
            db,
            actor_id=actor_id,
            action='FUNDING_COMBINED_REPORT_FINALIZED',
            entity_type='funding_report',
            entity_id=report.id,
            after=report.finalized_snapshot,
            ip=ip,
        )
        return report
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
    source_scope = (report.warning_summary or {}).get('purchase_order_scope') or {}
    if (
        report.account_type_snapshot == 'CONSIGNMENT'
        and source_scope.get('allocation_semantics')
        != 'OLDEST_OUTSTANDING_FUNDED_QUANTITY'
    ):
        raise ValueError(
            'This consignment draft predates funded-quantity FIFO controls. Delete it '
            'and calculate a new report; finalized history will remain unchanged.'
        )
    normalize_draft_funding_allocation(
        db, report=report, actor_id=actor_id, ip=ip
    )
    pending_fifo_exceptions = db.scalar(select(func.count()).select_from(
        FundingReportFifoException
    ).where(
        FundingReportFifoException.report_id == report.id,
        FundingReportFifoException.status == 'PENDING',
    )) or 0
    if pending_fifo_exceptions:
        raise ValueError(
            f'Resolve {pending_fifo_exceptions} pending funding-capacity exception(s) '
            'before finalizing.'
        )
    duplicate_reports = duplicate_finalized_fact_links(db, report=report)
    if duplicate_reports:
        raise ValueError(
            'This report includes Square activity already represented by finalized '
            'report(s) ' + ', '.join(str(value) for value in duplicate_reports)
            + '. It cannot be finalized twice.'
        )
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
        'fifo_exceptions': [
            {
                'id': row.id,
                'sale_fact_id': row.sale_fact_id,
                'status': row.status,
                'quantity': str(row.quantity_affected),
                'cost_basis': row.cost_basis,
                'unit_cost': str(row.unit_cost_snapshot) if row.unit_cost_snapshot is not None else None,
                'reason': row.resolution_reason,
                'resolved_by_principal_id': row.resolved_by_principal_id,
                'resolved_at': row.resolved_at.isoformat() if row.resolved_at else None,
            }
            for row in funding_report_fifo_exceptions(db, report_id=report.id)
        ],
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
    referenced_by = combined_reports_referencing(db, report=report)
    if referenced_by:
        raise ValueError(
            'This vendor report belongs to combined report(s) '
            + ', '.join(str(value) for value in referenced_by)
            + '. Discard the combined report first.'
        )
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
    db.execute(delete(FundingReportFifoException).where(FundingReportFifoException.report_id == report.id))
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
    if is_combined_report(report):
        raise ValueError('Adjust the applicable vendor report, not the combined view.')
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
                   actor_id: int, vendor_id: int | None = None, ip=None,
                   allow_cross_vendor: bool = False) -> FundingPayment:
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise ValueError('Account not found.')
    if allow_cross_vendor:
        if account.account_type != 'CREDIT_CARD' or vendor_id is not None or not allocations:
            raise ValueError('Combined payments require a Credit Card account and report allocations.')
        vendor = None
    elif account.account_type == 'CREDIT_CARD' and vendor_id is None and allocations:
        allocation_vendor_ids = {report.vendor_id for report_id in allocations
            if (report := db.get(FundingReport, int(report_id))) is not None}
        if len(allocation_vendor_ids) == 1 and None not in allocation_vendor_ids:
            vendor_id = allocation_vendor_ids.pop()
        vendor = resolve_account_vendor(db, account=account, vendor_id=vendor_id, purpose='payment')
    else:
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
        if vendor is not None and report.vendor_id != vendor.id:
            raise ValueError('Payments cannot be allocated across vendors.')
        allocation_reports[int(report_id)] = report
    row = FundingPayment(account_id=account.id, vendor_id=vendor.id if vendor else None,
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
           'vendor_id': vendor.id if vendor else None,
           'allocated': str(value - remaining_payment), 'unallocated': str(remaining_payment)}, ip=ip)
    return row


def record_compact_payment(
    db: Session, *, account_id: int, payment_date: date,
    amount: Decimal | None, paid_in_full: bool, actor_id: int,
    report_id: int | None = None, vendor_id: int | None = None,
    combined: bool = False, ip=None,
) -> FundingPayment:
    """Record an owner payment using authoritative current report balances."""
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise ValueError('Payment context was not found.')

    context_report = db.get(FundingReport, report_id) if report_id is not None else None
    if report_id is not None and (
        context_report is None or context_report.account_id != account.id
    ):
        raise ValueError('Payment context was not found.')

    if combined:
        if (
            account.account_type != 'CREDIT_CARD'
            or context_report is None
            or not is_combined_report(context_report)
            or vendor_id is not None
        ):
            raise ValueError('Choose a combined Credit Card report.')
        targets = combined_report_members(db, report=context_report)
        payment_vendor_id = None
    elif context_report is not None:
        if is_combined_report(context_report) or context_report.vendor_id is None:
            raise ValueError('Choose a vendor report.')
        if vendor_id is not None and vendor_id != context_report.vendor_id:
            raise ValueError('Vendor payment context does not match the report.')
        resolve_account_vendor(
            db, account=account, vendor_id=context_report.vendor_id, purpose='payment'
        )
        targets = [context_report]
        payment_vendor_id = context_report.vendor_id
    else:
        vendor = resolve_account_vendor(
            db, account=account, vendor_id=vendor_id, purpose='payment'
        )
        targets = db.scalars(select(FundingReport).where(
            FundingReport.account_id == account.id,
            FundingReport.vendor_id == vendor.id,
            FundingReport.status.not_in({'DRAFT', 'VOIDED'}),
        )).all()
        payment_vendor_id = vendor.id

    targets = sorted(
        (row for row in targets if row.status not in {'DRAFT', 'VOIDED'}),
        key=lambda row: (row.sales_end_date, row.sales_start_date, row.id),
    )
    positions = {row.id: report_position(db, report_id=row.id) for row in targets}
    total_remaining = money(sum(
        (positions[row.id]['remaining_amount'] for row in targets), Decimal('0')
    ))
    if total_remaining <= 0:
        raise ValueError('This obligation is already paid in full.')

    value = total_remaining if paid_in_full else money(amount)
    if value <= 0:
        raise ValueError('Enter a payment amount greater than zero.')
    if value > total_remaining:
        raise ValueError(
            f'Payment cannot exceed the remaining obligation of ${total_remaining:,.2f}.'
        )

    remaining = value
    allocations: dict[int, Decimal] = {}
    for report in targets:
        if remaining <= 0:
            break
        allocation = min(positions[report.id]['remaining_amount'], remaining)
        if allocation > 0:
            allocations[report.id] = allocation
            remaining -= allocation

    payment = record_payment(
        db,
        account_id=account.id,
        vendor_id=payment_vendor_id,
        entry_type='PAYMENT',
        amount=value,
        payment_date=payment_date,
        payment_source='',
        confirmation_number='',
        reason=('Combined credit-card payment' if combined else 'Vendor payment'),
        internal_note='',
        allocations=allocations,
        actor_id=actor_id,
        ip=ip,
        allow_cross_vendor=combined,
    )
    if combined and context_report is not None:
        _update_report_status(db, context_report)
    return payment


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
        referenced_by = combined_reports_referencing(db, report=report)
        if referenced_by:
            raise ValueError(
                'This vendor report belongs to combined report(s) '
                + ', '.join(str(value) for value in referenced_by)
                + '. Delete the combined view first.'
            )

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
            'fifo_exceptions': db.scalar(select(func.count()).select_from(
                FundingReportFifoException).where(
                    FundingReportFifoException.report_id == report.id)) or 0,
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
        db.execute(delete(FundingReportFifoException).where(
            FundingReportFifoException.report_id == report.id))
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


def account_summary(
    db: Session, *, account_id: int, include_purchase_order_lines: bool = False,
) -> dict:
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise LookupError('Account not found.')
    reports = db.scalars(select(FundingReport).where(FundingReport.account_id == account.id)
        .order_by(FundingReport.created_at.desc(), FundingReport.id.desc())).all()
    positions = {
        row.id: report_position_for_display(db, report_id=row.id)
        for row in reports
    }
    balance = tracked_balance(db, account_id=account.id)
    derived_inventory = None
    purchase_order_lines = funding_account_purchase_lines(
        db, account=account,
        include_resolution_candidates=include_purchase_order_lines)
    inventory_units = inventory_value = Decimal('0')
    refreshed = []
    if account.account_type == 'CREDIT_CARD':
        derived_inventory = credit_card_inventory_summary(db, account=account)
        inventory_units = derived_inventory['remaining_units']
        inventory_value = derived_inventory['remaining_value']
    else:
        # The latest assigned PO line is the authoritative current cost source.
        # Legacy FundingSkuMapping rows must not compete with a corrected lot cost.
        current_costs: dict[str, tuple[date, int, Decimal]] = {}
        for row in purchase_order_lines:
            line = row['line']
            if line.unit_cost is None or not normalize_sku(line.sku):
                continue
            key = normalize_sku(line.sku)
            candidate = (
                _purchase_order_date(row['purchase_order']), int(line.id),
                Decimal(str(line.unit_cost)),
            )
            if key not in current_costs or candidate[:2] > current_costs[key][:2]:
                current_costs[key] = candidate
        identities = db.scalars(select(OrderingCatalogIdentity).where(
            OrderingCatalogIdentity.sku.is_not(None))).all()
        variation_skus = {
            row.square_variation_id: normalize_sku(row.sku)
            for row in identities
            if normalize_sku(row.sku) in current_costs
        }
        inventory_by_sku: dict[str, Decimal] = defaultdict(Decimal)
        inventory_rows = db.scalars(select(OrderingCurrentInventory).where(
            OrderingCurrentInventory.square_variation_id.in_(
                list(variation_skus) or ['__none__']))).all()
        for inventory_row in inventory_rows:
            normalized_sku = variation_skus.get(inventory_row.square_variation_id)
            if normalized_sku:
                inventory_by_sku[normalized_sku] += Decimal(str(
                    inventory_row.counted_quantity))
            refreshed.append(inventory_row.refreshed_at)
        for normalized_sku, (_cost_date, _line_id, unit_cost) in current_costs.items():
            quantity = inventory_by_sku[normalized_sku]
            inventory_units += quantity
            inventory_value += money(quantity * unit_cost)
    payments = db.scalars(select(FundingPayment).where(FundingPayment.account_id == account.id)
        .order_by(FundingPayment.payment_date.desc(), FundingPayment.id.desc())).all()
    reversed_payment_ids = {row.reversed_payment_id for row in payments if row.reversed_payment_id is not None}
    ledger = db.scalars(select(FundingLedgerEntry).where(FundingLedgerEntry.account_id == account.id)
        .order_by(FundingLedgerEntry.effective_date.desc(), FundingLedgerEntry.id.desc())).all()
    reversed_ledger_ids = {row.original_entry_id for row in ledger if row.original_entry_id is not None}
    open_reports = sorted(
        (row for row in reports if not is_combined_report(row)
         and row.status not in {'DRAFT', 'VOIDED'}
         and positions[row.id]['remaining_amount'] > 0),
        key=lambda row: (row.sales_end_date, row.sales_start_date, row.id),
    )
    open_report_amount = sum((positions[row.id]['remaining_amount'] for row in reports
        if not is_combined_report(row)
        and row.status not in {'DRAFT', 'VOIDED'}), Decimal('0'))
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
        'purchase_order_lines': (
            purchase_order_lines if include_purchase_order_lines else []),
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
