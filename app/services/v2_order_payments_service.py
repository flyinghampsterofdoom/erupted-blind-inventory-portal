from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    ConsignmentAllocation,
    ConsignmentLedgerEntry,
    ConsignmentReceiptAllocation,
    ConsignmentReplenishment,
    ConsignmentReplenishmentReceipt,
    ConsignmentReplenishmentReceiptLine,
    ConsignmentReport,
    OrderPayment,
    OrderPaymentEvent,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    PaymentMethod,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStoreAllocation,
    Store,
    Vendor,
    VendorPaymentSetting,
    VendorSkuConfig,
)


PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')
PAYMENT_CATEGORIES = ('WIRE', 'CREDIT_CARD', 'DEBIT_CARD', 'TERMS', 'CONSIGNMENT')
INVOICE_STATUSES = ('UNPAID', 'PAID')
CONSIGNMENT_STATUSES = (
    'CONSIGNMENT_ORDERED',
    'CONSIGNMENT_PARTIALLY_RECEIVED',
    'CONSIGNMENT_RECEIVED',
    'CONSIGNMENT_PARTIALLY_APPLIED',
    'CONSIGNMENT_APPLIED',
)
PLACED_ORDER_STATUSES = ('IN_TRANSIT', 'RECEIVED_SPLIT_PENDING', 'SENT_TO_STORES', 'COMPLETED')
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MONEY = Decimal('0.01')


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def portal_today(now: datetime | None = None) -> date:
    return (now or utc_now()).astimezone(PORTAL_TIMEZONE).date()


def money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def masked_payment_method(method: PaymentMethod | None) -> str:
    if method is None:
        return 'Not selected'
    suffix = f' •••• {method.last_four}' if method.last_four else ''
    return f'{method.display_name}{suffix}'


def validate_payment_method(
    *,
    display_name: str,
    category: str,
    last_four: str | None,
    term_days: int | None,
) -> None:
    if not display_name.strip():
        raise ValueError('Display name is required.')
    if category not in PAYMENT_CATEGORIES:
        raise ValueError('Unsupported payment category.')
    if last_four and (len(last_four) != 4 or not last_four.isdigit()):
        raise ValueError('Only the final four digits may be stored.')
    if category == 'TERMS' and (term_days is None or term_days <= 0):
        raise ValueError('Terms payment methods require a positive term duration.')
    if category != 'TERMS' and term_days is not None:
        raise ValueError('Term duration is only valid for Terms payment methods.')


def validate_report_email(value: str | None) -> str | None:
    clean = (value or '').strip().lower()
    if not clean:
        return None
    if not EMAIL_PATTERN.fullmatch(clean):
        raise ValueError('Enter a valid report email address.')
    return clean


def _audit(
    db: Session,
    *,
    actor_id: int,
    action: str,
    entity_type: str,
    entity_id: int,
    before: dict | None = None,
    after: dict | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_principal_id=actor_id,
            action=action,
            ip=ip,
            meta={
                'domain': 'ORDER_PAYMENTS_V2',
                'entity_type': entity_type,
                'entity_id': entity_id,
                'before': before or {},
                'after': after or {},
            },
        )
    )


def create_payment_method(
    db: Session,
    *,
    actor_id: int,
    display_name: str,
    category: str,
    institution: str | None,
    account_nickname: str | None,
    last_four: str | None,
    term_days: int | None,
    notes: str | None,
    ip: str | None = None,
) -> PaymentMethod:
    display_name = display_name.strip()
    category = category.strip().upper()
    last_four = (last_four or '').strip() or None
    validate_payment_method(
        display_name=display_name, category=category, last_four=last_four, term_days=term_days
    )
    row = PaymentMethod(
        display_name=display_name,
        category=category,
        institution_or_company_name=(institution or '').strip() or None,
        account_nickname=(account_nickname or '').strip() or None,
        last_four=last_four,
        term_days=term_days,
        consignment_cycle='SINCE_LAST_FINALIZED_REPORT' if category == 'CONSIGNMENT' else None,
        is_active=True,
        notes=(notes or '').strip() or None,
        created_by_principal_id=actor_id,
        updated_by_principal_id=actor_id,
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        actor_id=actor_id,
        action='PAYMENT_METHOD_CREATED',
        entity_type='payment_method',
        entity_id=row.id,
        after={'display_name': row.display_name, 'category': row.category, 'last_four': row.last_four},
        ip=ip,
    )
    return row


def set_payment_method_active(
    db: Session, *, method_id: int, active: bool, actor_id: int, ip: str | None = None
) -> PaymentMethod:
    row = db.get(PaymentMethod, method_id)
    if row is None:
        raise LookupError('Payment method not found.')
    before = {'is_active': row.is_active}
    row.is_active = active
    row.updated_by_principal_id = actor_id
    _audit(
        db,
        actor_id=actor_id,
        action='PAYMENT_METHOD_ACTIVATED' if active else 'PAYMENT_METHOD_DEACTIVATED',
        entity_type='payment_method',
        entity_id=row.id,
        before=before,
        after={'is_active': active},
        ip=ip,
    )
    return row


def save_vendor_settings(
    db: Session,
    *,
    vendor_id: int,
    default_payment_method_id: int | None,
    report_email: str | None,
    payment_notes: str | None,
    actor_id: int,
    ip: str | None = None,
) -> VendorPaymentSetting:
    if db.get(Vendor, vendor_id) is None:
        raise LookupError('Vendor not found.')
    method = db.get(PaymentMethod, default_payment_method_id) if default_payment_method_id else None
    if default_payment_method_id and (method is None or not method.is_active):
        raise ValueError('Vendor defaults must use an active payment method.')
    email = validate_report_email(report_email)
    row = db.get(VendorPaymentSetting, vendor_id)
    before = {}
    if row is None:
        row = VendorPaymentSetting(vendor_id=vendor_id, updated_by_principal_id=actor_id)
        db.add(row)
    else:
        before = {
            'default_payment_method_id': row.default_payment_method_id,
            'report_email': row.report_email,
        }
    row.default_payment_method_id = default_payment_method_id
    row.report_email = email
    row.payment_notes = (payment_notes or '').strip() or None
    row.updated_by_principal_id = actor_id
    db.flush()
    _audit(
        db,
        actor_id=actor_id,
        action='VENDOR_PAYMENT_SETTINGS_CHANGED',
        entity_type='vendor',
        entity_id=vendor_id,
        before=before,
        after={
            'default_payment_method_id': default_payment_method_id,
            'report_email': email,
        },
        ip=ip,
    )
    return row


def _order_cost_snapshot(db: Session, order_id: int) -> tuple[Decimal, bool]:
    value, missing_cost_count = db.execute(
        select(
            func.coalesce(
                func.sum(PurchaseOrderLine.ordered_qty * PurchaseOrderLine.unit_cost),
                Decimal('0'),
            ),
            func.count().filter(
                PurchaseOrderLine.ordered_qty > 0,
                PurchaseOrderLine.unit_cost.is_(None),
            ),
        ).where(
            PurchaseOrderLine.purchase_order_id == order_id,
            PurchaseOrderLine.removed.is_(False),
        )
    ).one()
    return money(value), int(missing_cost_count) == 0


def _order_date(order: PurchaseOrder) -> date:
    value = order.ordered_at or order.submitted_at or order.created_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PORTAL_TIMEZONE).date()


def ensure_order_payment(db: Session, *, order: PurchaseOrder, actor_id: int) -> OrderPayment:
    existing = db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id == order.id))
    if existing is not None:
        return existing
    settings = db.get(VendorPaymentSetting, order.vendor_id)
    method = (
        db.get(PaymentMethod, settings.default_payment_method_id)
        if settings and settings.default_payment_method_id
        else None
    )
    is_consignment = bool(method and method.category == 'CONSIGNMENT')
    term_days = method.term_days if method and method.category == 'TERMS' else None
    order_amount, order_cost_complete = _order_cost_snapshot(db, order.id)
    row = OrderPayment(
        purchase_order_id=order.id,
        vendor_id=order.vendor_id,
        payment_method_id=method.id if method else None,
        payment_category_snapshot=method.category if method else None,
        payment_method_label_snapshot=masked_payment_method(method) if method else None,
        term_days_snapshot=term_days,
        status='CONSIGNMENT_ORDERED' if is_consignment else 'UNPAID',
        financial_treatment='REPLENISHMENT' if is_consignment else 'INVOICE',
        order_amount=order_amount,
        order_cost_complete=order_cost_complete,
        due_date=_order_date(order) + timedelta(days=term_days) if term_days else None,
    )
    db.add(row)
    db.flush()
    db.add(
        OrderPaymentEvent(
            order_payment_id=row.id,
            new_status=row.status,
            new_payment_method_id=row.payment_method_id,
            actor_principal_id=actor_id,
            note='Safe V2 initialization; no paid state inferred.',
        )
    )
    if is_consignment:
        db.add(
            ConsignmentReplenishment(
                vendor_id=order.vendor_id,
                purchase_order_id=order.id,
                ordered_cost_value=row.order_amount,
                received_cost_value=Decimal('0'),
                amount_applied=Decimal('0'),
                excess_credit_created=Decimal('0'),
                status='PENDING',
                created_by_principal_id=actor_id,
            )
        )
    return row


def backfill_placed_order_payments(db: Session, *, actor_id: int) -> int:
    orders = db.scalars(
        select(PurchaseOrder)
        .outerjoin(OrderPayment, OrderPayment.purchase_order_id == PurchaseOrder.id)
        .where(PurchaseOrder.status.in_(PLACED_ORDER_STATUSES), OrderPayment.id.is_(None))
        .order_by(PurchaseOrder.id)
    ).all()
    for order in orders:
        ensure_order_payment(db, order=order, actor_id=actor_id)
    return len(orders)


def purchase_order_scope_labels(db: Session, *, order_ids: list[int]) -> dict[int, str]:
    if not order_ids:
        return {}
    rows = db.execute(
        select(PurchaseOrderLine.purchase_order_id, Store.name)
        .join(
            PurchaseOrderStoreAllocation,
            PurchaseOrderStoreAllocation.purchase_order_line_id == PurchaseOrderLine.id,
        )
        .join(Store, Store.id == PurchaseOrderStoreAllocation.store_id)
        .where(
            PurchaseOrderLine.purchase_order_id.in_(order_ids),
            PurchaseOrderLine.removed.is_(False),
        )
        .distinct()
        .order_by(PurchaseOrderLine.purchase_order_id, Store.name)
    ).all()
    names: dict[int, list[str]] = {}
    for order_id, store_name in rows:
        names.setdefault(int(order_id), []).append(str(store_name))
    return {
        order_id: ', '.join(store_names) if store_names else 'Organization-wide'
        for order_id, store_names in names.items()
    }


def update_order_payment(
    db: Session,
    *,
    order_payment_id: int,
    payment_method_id: int | None,
    status: str,
    paid_date: date | None,
    actor_id: int,
    ip: str | None = None,
) -> OrderPayment:
    row = db.get(OrderPayment, order_payment_id)
    if row is None:
        raise LookupError('Order payment not found.')
    if row.financial_treatment == 'REPLENISHMENT':
        raise ValueError('Consignment orders are settled through receipt and ledger allocation.')
    if status not in INVOICE_STATUSES:
        raise ValueError('Unsupported invoice payment status.')
    method = db.get(PaymentMethod, payment_method_id) if payment_method_id else None
    if method is None and status == 'PAID':
        raise ValueError('A payment method is required before marking an order paid.')
    if status == 'PAID' and not row.order_cost_complete:
        raise ValueError('Cannot mark an order paid while its saved V1 line-cost snapshot is incomplete.')
    if method and not method.is_active and method.id != row.payment_method_id:
        raise ValueError('Inactive payment methods cannot be newly selected.')
    if method and method.category == 'CONSIGNMENT':
        raise ValueError('An existing invoice cannot be converted to consignment through inline editing.')
    prior_status = row.status
    prior_method_id = row.payment_method_id
    row.payment_method_id = method.id if method else None
    if method:
        row.payment_category_snapshot = method.category
        row.payment_method_label_snapshot = masked_payment_method(method)
        row.term_days_snapshot = method.term_days if method.category == 'TERMS' else None
        row.due_date = (
            _order_date(db.get(PurchaseOrder, row.purchase_order_id)) + timedelta(days=method.term_days)
            if method.category == 'TERMS'
            else None
        )
    row.status = status
    effective_date = None
    if status == 'PAID':
        effective_date = paid_date or portal_today()
        row.paid_date = effective_date
        row.paid_amount = row.order_amount
        row.marked_paid_at = utc_now()
        row.marked_paid_by_principal_id = actor_id
    else:
        row.paid_date = None
        row.paid_amount = None
        row.marked_paid_at = None
        row.marked_paid_by_principal_id = None
    db.add(
        OrderPaymentEvent(
            order_payment_id=row.id,
            prior_status=prior_status,
            new_status=status,
            prior_payment_method_id=prior_method_id,
            new_payment_method_id=row.payment_method_id,
            effective_date=effective_date,
            actor_principal_id=actor_id,
        )
    )
    _audit(
        db,
        actor_id=actor_id,
        action='ORDER_PAYMENT_CHANGED',
        entity_type='order_payment',
        entity_id=row.id,
        before={'status': prior_status, 'payment_method_id': prior_method_id},
        after={'status': status, 'payment_method_id': row.payment_method_id, 'paid_date': str(effective_date or '')},
        ip=ip,
    )
    return row


@dataclass(frozen=True)
class ConsignmentBalance:
    cogs_generated: Decimal
    replenishment_applied: Decimal
    cash_adjustments: Decimal
    approved_credits: Decimal
    unreplenished_cogs: Decimal
    available_replenishment_credit: Decimal


def calculate_consignment_balance(totals: dict[str, Decimal]) -> ConsignmentBalance:
    normalized = {key: money(value) for key, value in totals.items()}
    cogs = max(
        normalized.get('COGS_GENERATED', Decimal('0.00'))
        - normalized.get('VOID_REVERSAL', Decimal('0.00')),
        Decimal('0.00'),
    )
    applied = normalized.get('REPLENISHMENT_APPLIED', Decimal('0.00'))
    cash = normalized.get('CASH_SETTLEMENT', Decimal('0.00'))
    credits = normalized.get('APPROVED_CREDIT', Decimal('0.00'))
    unreplenished = max(cogs - applied - cash - credits, Decimal('0.00'))
    available = max(
        normalized.get('REPLENISHMENT_CREDIT_CREATED', Decimal('0.00'))
        - normalized.get('REPLENISHMENT_CREDIT_USED', Decimal('0.00')),
        Decimal('0.00'),
    )
    return ConsignmentBalance(cogs, applied, cash, credits, unreplenished, available)


def oldest_first_allocation(
    received_value: Decimal, report_open_balances: list[tuple[int, Decimal]]
) -> tuple[list[tuple[int, Decimal]], Decimal]:
    remaining = money(received_value)
    allocations: list[tuple[int, Decimal]] = []
    for report_id, raw_open_amount in report_open_balances:
        open_amount = max(money(raw_open_amount), Decimal('0.00'))
        applied = min(open_amount, remaining)
        if applied > 0:
            allocations.append((report_id, applied))
            remaining = money(remaining - applied)
        if remaining <= 0:
            break
    return allocations, remaining


def consignment_balance(db: Session, *, vendor_id: int) -> ConsignmentBalance:
    rows = db.execute(
        select(ConsignmentLedgerEntry.entry_type, func.coalesce(func.sum(ConsignmentLedgerEntry.amount), 0))
        .where(ConsignmentLedgerEntry.vendor_id == vendor_id)
        .group_by(ConsignmentLedgerEntry.entry_type)
    ).all()
    return calculate_consignment_balance({str(row.entry_type): money(row[1]) for row in rows})


def _set_replenishment_status(
    db: Session,
    *,
    replenishment: ConsignmentReplenishment,
    any_received: bool,
    all_received: bool,
) -> None:
    accounted = money(replenishment.amount_applied) + money(replenishment.excess_credit_created)
    if not any_received:
        status = 'PENDING'
    elif all_received and accounted >= money(replenishment.received_cost_value):
        status = 'APPLIED'
    elif all_received:
        status = 'RECEIVED'
    elif accounted > 0:
        status = 'PARTIALLY_APPLIED'
    else:
        status = 'PARTIALLY_RECEIVED'
    replenishment.status = status
    payment = db.scalar(
        select(OrderPayment).where(OrderPayment.purchase_order_id == replenishment.purchase_order_id)
    )
    if payment:
        payment.status = {
            'PENDING': 'CONSIGNMENT_ORDERED',
            'PARTIALLY_RECEIVED': 'CONSIGNMENT_PARTIALLY_RECEIVED',
            'RECEIVED': 'CONSIGNMENT_RECEIVED',
            'PARTIALLY_APPLIED': 'CONSIGNMENT_PARTIALLY_APPLIED',
            'APPLIED': 'CONSIGNMENT_APPLIED',
        }[status]


def sync_consignment_replenishment(
    db: Session, *, replenishment: ConsignmentReplenishment, actor_id: int
) -> ConsignmentReplenishment:
    lines = db.scalars(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id == replenishment.purchase_order_id,
            PurchaseOrderLine.removed.is_(False),
        ).order_by(PurchaseOrderLine.id)
    ).all()
    line_by_id = {int(line.id): line for line in lines}
    allocations = db.scalars(
        select(PurchaseOrderStoreAllocation)
        .where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(tuple(line_by_id) or (-1,)))
        .order_by(PurchaseOrderStoreAllocation.purchase_order_line_id,
                  PurchaseOrderStoreAllocation.store_id)
    ).all()
    allocations_by_line: dict[int, list[PurchaseOrderStoreAllocation]] = {}
    for allocation in allocations:
        allocations_by_line.setdefault(int(allocation.purchase_order_line_id), []).append(allocation)

    prior_receipt_lines = db.execute(
        select(ConsignmentReplenishmentReceiptLine)
        .join(
            ConsignmentReplenishmentReceipt,
            ConsignmentReplenishmentReceipt.id == ConsignmentReplenishmentReceiptLine.receipt_id,
        )
        .where(ConsignmentReplenishmentReceipt.replenishment_id == replenishment.id)
    ).scalars().all()
    processed_by_allocation: dict[int, int] = {}
    processed_by_line: dict[int, int] = {}
    for prior in prior_receipt_lines:
        if prior.purchase_order_store_allocation_id is not None:
            key = int(prior.purchase_order_store_allocation_id)
            processed_by_allocation[key] = max(
                processed_by_allocation.get(key, 0), int(prior.received_qty_snapshot)
            )
        else:
            key = int(prior.purchase_order_line_id)
            processed_by_line[key] = max(processed_by_line.get(key, 0), int(prior.received_qty_snapshot))

    source_rows: list[dict] = []
    any_received = False
    all_received = bool(lines)
    for line in lines:
        line_allocations = allocations_by_line.get(int(line.id), [])
        allocation_total = sum(max(int(row.store_received_qty or 0), 0) for row in line_allocations)
        current_total = max(int(line.received_qty_total or 0), 0)
        if current_total > 0 and not line_allocations:
            replenishment.integrity_warning = (
                f'Purchase-order line {line.id} has an aggregate received quantity but no canonical '
                'V1 store-allocation receipt rows; replenishment allocation is blocked.'
            )
            return replenishment
        if line_allocations and allocation_total != current_total:
            replenishment.integrity_warning = (
                f'V1 receiving totals do not reconcile for purchase-order line {line.id}; '
                'replenishment allocation is blocked.'
            )
            return replenishment
        any_received = any_received or current_total > 0
        if int(line.ordered_qty or 0) > 0 and current_total < int(line.ordered_qty or 0):
            all_received = False
        for allocation in line_allocations:
            current_qty = max(int(allocation.store_received_qty or 0), 0)
            prior_qty = processed_by_allocation.get(int(allocation.id), 0)
            if current_qty < prior_qty:
                replenishment.integrity_warning = (
                    f'V1 received quantity decreased for purchase-order line {line.id}; '
                    'an owner-reviewed typed correction is required before further allocation.'
                )
                return replenishment
            if current_qty == prior_qty:
                continue
            if line.unit_cost is None:
                replenishment.integrity_warning = (
                    f'Purchase-order line {line.id} has received quantity but no captured V1 unit cost; '
                    'receipt valuation and allocation are blocked.'
                )
                return replenishment
            delta_qty = current_qty - prior_qty
            source_rows.append({
                'line': line,
                'allocation': allocation,
                'prior_qty': prior_qty,
                'current_qty': current_qty,
                'delta_qty': delta_qty,
                'unit_cost': Decimal(str(line.unit_cost)),
                'value': money(Decimal(delta_qty) * Decimal(str(line.unit_cost))),
            })

    if not source_rows:
        _set_replenishment_status(
            db, replenishment=replenishment, any_received=any_received, all_received=all_received
        )
        return replenishment

    now = utc_now()
    received_delta = money(sum((row['value'] for row in source_rows), Decimal('0')))
    received_ledger = ConsignmentLedgerEntry(
        vendor_id=replenishment.vendor_id,
        entry_type='REPLENISHMENT_RECEIVED',
        effective_at=now,
        amount=received_delta,
        quantity=sum((Decimal(row['delta_qty']) for row in source_rows), Decimal('0')),
        purchase_order_id=replenishment.purchase_order_id,
        note='Derived only from positive V1 line/store received-quantity deltas at captured PO line cost.',
        created_by_principal_id=actor_id,
    )
    db.add(received_ledger)
    db.flush()
    receipt = ConsignmentReplenishmentReceipt(
        replenishment_id=replenishment.id,
        purchase_order_id=replenishment.purchase_order_id,
        received_ledger_entry_id=received_ledger.id,
        received_value_delta=received_delta,
        source_observed_at=now,
        created_by_principal_id=actor_id,
    )
    db.add(receipt)
    db.flush()

    balance_before_allocation = consignment_balance(db, vendor_id=replenishment.vendor_id)
    unapplied_offsets = money(
        balance_before_allocation.cash_adjustments + balance_before_allocation.approved_credits
    )
    reports = db.scalars(
        select(ConsignmentReport)
        .where(
            ConsignmentReport.vendor_id == replenishment.vendor_id,
            ConsignmentReport.status.in_(('FINALIZED', 'EMAILED')),
        )
        .order_by(ConsignmentReport.end_at, ConsignmentReport.id)
    ).all()
    report_open: list[list] = []
    for report in reports:
        already = money(db.scalar(select(func.coalesce(func.sum(ConsignmentAllocation.amount_applied), 0)).where(
            ConsignmentAllocation.cogs_report_id == report.id)))
        open_amount = max(money(report.total_cogs) - already, Decimal('0.00'))
        offset = min(open_amount, unapplied_offsets)
        report_open.append([report, money(open_amount - offset)])
        unapplied_offsets = money(unapplied_offsets - offset)

    for source in source_rows:
        line = source['line']
        allocation_source = source['allocation']
        receipt_line = ConsignmentReplenishmentReceiptLine(
            receipt_id=receipt.id,
            purchase_order_line_id=line.id,
            purchase_order_store_allocation_id=allocation_source.id if allocation_source else None,
            store_id=allocation_source.store_id if allocation_source else None,
            prior_received_qty=source['prior_qty'],
            received_qty_snapshot=source['current_qty'],
            received_qty_delta=source['delta_qty'],
            unit_cost_snapshot=source['unit_cost'],
            received_value_delta=source['value'],
        )
        db.add(receipt_line)
        db.flush()
        remaining = money(source['value'])
        for report_state in report_open:
            report, open_amount = report_state
            amount = min(open_amount, remaining)
            if amount <= 0:
                continue
            aggregate = db.scalar(select(ConsignmentAllocation).where(
                ConsignmentAllocation.replenishment_id == replenishment.id,
                ConsignmentAllocation.cogs_report_id == report.id,
            ))
            if aggregate:
                aggregate.amount_applied = money(aggregate.amount_applied + amount)
            else:
                db.add(ConsignmentAllocation(
                    vendor_id=replenishment.vendor_id,
                    replenishment_id=replenishment.id,
                    cogs_report_id=report.id,
                    amount_applied=amount,
                    created_by_principal_id=actor_id,
                ))
            applied_ledger = ConsignmentLedgerEntry(
                vendor_id=replenishment.vendor_id,
                entry_type='REPLENISHMENT_APPLIED',
                effective_at=now,
                amount=amount,
                report_id=report.id,
                purchase_order_id=replenishment.purchase_order_id,
                note=f'V1 receipt {receipt.id}, purchase-order line {line.id}.',
                created_by_principal_id=actor_id,
            )
            db.add(applied_ledger)
            db.flush()
            db.add(ConsignmentReceiptAllocation(
                receipt_line_id=receipt_line.id,
                cogs_report_id=report.id,
                applied_ledger_entry_id=applied_ledger.id,
                amount_applied=amount,
                created_by_principal_id=actor_id,
            ))
            replenishment.amount_applied = money(replenishment.amount_applied + amount)
            remaining = money(remaining - amount)
            report_state[1] = money(open_amount - amount)
            if remaining <= 0:
                break
        if remaining > 0:
            credit_ledger = ConsignmentLedgerEntry(
                vendor_id=replenishment.vendor_id,
                entry_type='REPLENISHMENT_CREDIT_CREATED',
                effective_at=now,
                amount=remaining,
                purchase_order_id=replenishment.purchase_order_id,
                note=f'Excess from V1 receipt {receipt.id}, purchase-order line {line.id}.',
                created_by_principal_id=actor_id,
            )
            db.add(credit_ledger)
            db.flush()
            receipt_line.credit_ledger_entry_id = credit_ledger.id
            replenishment.excess_credit_created = money(
                replenishment.excess_credit_created + remaining
            )

    replenishment.received_cost_value = money(replenishment.received_cost_value + received_delta)
    replenishment.last_receipt_at = now
    replenishment.integrity_warning = None
    _set_replenishment_status(
        db, replenishment=replenishment, any_received=any_received, all_received=all_received
    )
    return replenishment


def inventory_snapshot(db: Session, *, vendor_id: int) -> tuple[Decimal, Decimal, list[dict], list[str]]:
    vendor_variations = tuple(
        str(value)
        for value in db.scalars(
            select(VendorSkuConfig.square_variation_id).where(
                VendorSkuConfig.vendor_id == vendor_id,
                VendorSkuConfig.active.is_(True),
                VendorSkuConfig.square_variation_id.is_not(None),
            )
        ).all()
    )
    if not vendor_variations:
        return Decimal('0.000'), Decimal('0.00'), [], []
    mappings = db.execute(
        select(
            VendorSkuConfig.square_variation_id,
            func.count(VendorSkuConfig.id),
            func.count(func.distinct(VendorSkuConfig.vendor_id)),
            func.max(VendorSkuConfig.vendor_id),
            func.max(VendorSkuConfig.unit_cost),
        )
        .where(
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.square_variation_id.in_(vendor_variations),
        )
        .group_by(VendorSkuConfig.square_variation_id)
    ).all()
    unique_cost = {
        str(row.square_variation_id): Decimal(str(row[4]))
        for row in mappings
        if int(row[1]) == 1 and int(row[2]) == 1 and int(row[3]) == vendor_id
    }
    warnings = [
        f'Variation {row.square_variation_id} has ambiguous active vendor cost mappings and was excluded.'
        for row in mappings
        if not (int(row[1]) == 1 and int(row[2]) == 1 and int(row[3]) == vendor_id)
    ]
    if not unique_cost:
        return Decimal('0.000'), Decimal('0.00'), [], warnings
    rows = db.execute(
        select(
            OrderingCurrentInventory.square_variation_id,
            OrderingCurrentInventory.store_id,
            OrderingCurrentInventory.counted_quantity,
            OrderingCurrentInventory.refreshed_at,
            OrderingCatalogIdentity.product_name,
            OrderingCatalogIdentity.variation_name,
            OrderingCatalogIdentity.sku,
        )
        .outerjoin(
            OrderingCatalogIdentity,
            OrderingCatalogIdentity.square_variation_id == OrderingCurrentInventory.square_variation_id,
        )
        .where(OrderingCurrentInventory.square_variation_id.in_(tuple(unique_cost)))
        .order_by(OrderingCatalogIdentity.product_name, OrderingCurrentInventory.store_id)
    ).all()
    detail = []
    total_qty = Decimal('0')
    total_value = Decimal('0')
    for row in rows:
        qty = Decimal(str(row.counted_quantity))
        cost = unique_cost[str(row.square_variation_id)]
        value = money(qty * cost)
        total_qty += qty
        total_value += value
        detail.append(
            {
                'variation_id': row.square_variation_id,
                'product_name': row.product_name or 'Unknown product',
                'variation_name': row.variation_name or '',
                'sku': row.sku or '',
                'store_id': row.store_id,
                'quantity': qty,
                'unit_cost': cost,
                'value': value,
                'refreshed_at': row.refreshed_at,
                'negative': qty < 0,
            }
        )
        if qty < 0:
            warnings.append(f'Negative inventory retained for {row.sku or row.square_variation_id}.')
    return total_qty, money(total_value), detail, warnings


def record_cash_settlement(
    db: Session,
    *,
    vendor_id: int,
    amount: Decimal,
    effective_date: date,
    payment_method_id: int,
    note: str,
    actor_id: int,
    ip: str | None = None,
) -> ConsignmentLedgerEntry:
    amount = money(amount)
    method = db.get(PaymentMethod, payment_method_id)
    if amount <= 0 or method is None or method.category == 'CONSIGNMENT':
        raise ValueError('Cash settlement requires a positive amount and a non-consignment payment method.')
    if not note.strip():
        raise ValueError('A reason is required for exceptional cash settlement.')
    balance = consignment_balance(db, vendor_id=vendor_id)
    if amount > balance.unreplenished_cogs:
        raise ValueError('Cash settlement cannot exceed current unreplenished COGS.')
    effective_at = datetime.combine(effective_date, datetime.min.time(), tzinfo=PORTAL_TIMEZONE).astimezone(
        timezone.utc
    )
    row = ConsignmentLedgerEntry(
        vendor_id=vendor_id,
        entry_type='CASH_SETTLEMENT',
        effective_at=effective_at,
        amount=amount,
        payment_method_id=method.id,
        note=note.strip(),
        created_by_principal_id=actor_id,
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        actor_id=actor_id,
        action='CONSIGNMENT_CASH_SETTLEMENT_RECORDED',
        entity_type='consignment_ledger_entry',
        entity_id=row.id,
        after={'vendor_id': vendor_id, 'amount': str(amount), 'effective_date': str(effective_date)},
        ip=ip,
    )
    return row
