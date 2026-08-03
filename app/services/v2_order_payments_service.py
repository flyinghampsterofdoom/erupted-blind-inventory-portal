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
    ConsignmentManualAdjustment,
    ConsignmentReceiptAllocation,
    ConsignmentReplenishment,
    ConsignmentReplenishmentReceipt,
    ConsignmentReplenishmentReceiptLine,
    ConsignmentReport,
    OrderPayment,
    OrderBalanceAdjustment,
    OrderManualPaymentEntry,
    OrderPaymentBackfillOperation,
    OrderPaymentBackfillResult,
    OrderPaymentEvent,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    PaymentMethod,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStoreAllocation,
    Store,
    Vendor,
    VendorAssignmentChange,
    VendorAssignmentOperation,
    VendorPaymentClassification,
    VendorPaymentSetting,
    VendorSkuConfig,
)


PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')
PAYMENT_CATEGORIES = ('WIRE', 'CREDIT_CARD', 'DEBIT_CARD', 'TERMS', 'CONSIGNMENT')
MANUAL_CHARGE_TYPES = (
    'SHIPPING_CHARGE', 'TAX_CHARGE', 'VENDOR_FEE', 'MISCELLANEOUS_CHARGE'
)
MANUAL_CREDIT_TYPES = (
    'VENDOR_CREDIT', 'DAMAGE_CREDIT', 'PROMOTIONAL_CREDIT', 'MISCELLANEOUS_CREDIT'
)
MANUAL_ADJUSTMENT_TYPES = MANUAL_CHARGE_TYPES + MANUAL_CREDIT_TYPES
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
    if category in {'TERMS', 'CONSIGNMENT'}:
        institution = account_nickname = last_four = None
    if category != 'TERMS':
        term_days = None
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


def update_payment_method(
    db: Session,
    *,
    method_id: int,
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
    row = db.get(PaymentMethod, method_id)
    if row is None:
        raise LookupError('Payment method not found.')
    display_name = display_name.strip()
    category = category.strip().upper()
    last_four = (last_four or '').strip() or None
    if category in {'TERMS', 'CONSIGNMENT'}:
        institution = account_nickname = last_four = None
    if category != 'TERMS':
        term_days = None
    validate_payment_method(
        display_name=display_name,
        category=category,
        last_four=last_four,
        term_days=term_days,
    )
    in_use = bool(db.scalar(
        select(func.count()).select_from(OrderPayment).where(OrderPayment.payment_method_id == row.id)
    )) or bool(db.scalar(
        select(func.count()).select_from(VendorPaymentClassification).where(
            VendorPaymentClassification.payment_method_id == row.id
        )
    ))
    if in_use and category != row.category:
        raise ValueError('The type cannot change after a payment method has been used.')
    before = {
        'display_name': row.display_name,
        'category': row.category,
        'institution': row.institution_or_company_name,
        'account_nickname': row.account_nickname,
        'last_four': row.last_four,
        'term_days': row.term_days,
        'notes': row.notes,
    }
    row.display_name = display_name
    row.category = category
    row.institution_or_company_name = (institution or '').strip() or None
    row.account_nickname = (account_nickname or '').strip() or None
    row.last_four = last_four
    row.term_days = term_days
    row.consignment_cycle = 'SINCE_LAST_FINALIZED_REPORT' if category == 'CONSIGNMENT' else None
    row.notes = (notes or '').strip() or None
    row.updated_by_principal_id = actor_id
    db.flush()
    current_classifications = db.scalars(
        select(VendorPaymentClassification).where(
            VendorPaymentClassification.payment_method_id == row.id,
            VendorPaymentClassification.is_current.is_(True),
        )
    ).all()
    now = utc_now()
    for current in current_classifications:
        current.is_current = False
        current.superseded_at = now
        db.add(VendorPaymentClassification(
            vendor_id=current.vendor_id,
            payment_method_id=row.id,
            payment_category=row.category,
            payment_method_label_snapshot=masked_payment_method(row),
            term_days_snapshot=row.term_days if row.category == 'TERMS' else None,
            is_consignment=row.category == 'CONSIGNMENT',
            effective_date=portal_today(),
            internal_note='Payment method details updated by owner.',
            is_current=True,
            created_by_principal_id=actor_id,
        ))
    after = {
        'display_name': row.display_name,
        'category': row.category,
        'institution': row.institution_or_company_name,
        'account_nickname': row.account_nickname,
        'last_four': row.last_four,
        'term_days': row.term_days,
        'notes': row.notes,
    }
    _audit(
        db,
        actor_id=actor_id,
        action='PAYMENT_METHOD_UPDATED',
        entity_type='payment_method',
        entity_id=row.id,
        before=before,
        after=after,
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
    effective_date: date | None,
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
    now = utc_now()
    current = db.scalar(
        select(VendorPaymentClassification).where(
            VendorPaymentClassification.vendor_id == vendor_id,
            VendorPaymentClassification.is_current.is_(True),
        )
    )
    if current is not None:
        current.is_current = False
        current.superseded_at = now
        db.flush()
    classification = VendorPaymentClassification(
        vendor_id=vendor_id,
        payment_method_id=method.id if method else None,
        payment_category=method.category if method else 'UNCONFIGURED',
        payment_method_label_snapshot=masked_payment_method(method) if method else None,
        term_days_snapshot=method.term_days if method and method.category == 'TERMS' else None,
        is_consignment=bool(method and method.category == 'CONSIGNMENT'),
        effective_date=effective_date or portal_today(now),
        internal_note=(payment_notes or '').strip() or None,
        is_current=True,
        created_by_principal_id=actor_id,
    )
    db.add(classification)
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
            'classification_id': classification.id,
            'payment_category': classification.payment_category,
            'effective_date': str(classification.effective_date),
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


def current_vendor_classification(
    db: Session, *, vendor_id: int
) -> VendorPaymentClassification | None:
    return db.scalar(
        select(VendorPaymentClassification).where(
            VendorPaymentClassification.vendor_id == vendor_id,
            VendorPaymentClassification.is_current.is_(True),
        )
    )


def _canonical_received_quantity(db: Session, *, order_id: int) -> int:
    return int(db.scalar(
        select(func.coalesce(func.sum(PurchaseOrderLine.received_qty_total), 0)).where(
            PurchaseOrderLine.purchase_order_id == order_id,
            PurchaseOrderLine.removed.is_(False),
        )
    ) or 0)


def initialize_order_payment(
    db: Session,
    *,
    order: PurchaseOrder,
    classification: VendorPaymentClassification,
    actor_id: int,
    event_note: str,
    financial_vendor_id: int | None = None,
) -> OrderPayment:
    existing = db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id == order.id))
    if existing is not None:
        return existing
    if int(classification.vendor_id) != int(order.vendor_id):
        raise ValueError('Vendor classification does not match the V1 purchase order vendor.')
    financial_vendor_id = int(financial_vendor_id or order.vendor_id)
    financial_vendor = db.get(Vendor, financial_vendor_id)
    if (
        financial_vendor is None
        or not financial_vendor.active
        or not str(financial_vendor.square_vendor_id or '').strip()
    ):
        raise ValueError('Vendor not available. Add this vendor in Square before assigning it here.')
    if classification.payment_category == 'UNCONFIGURED' or classification.payment_method_id is None:
        raise ValueError('Vendor financial classification is required before order initialization.')
    method = db.get(PaymentMethod, classification.payment_method_id)
    if method is None or method.category != classification.payment_category:
        raise ValueError('Vendor classification payment method is missing or no longer matches its category.')
    is_consignment = bool(classification.is_consignment)
    term_days = classification.term_days_snapshot if classification.payment_category == 'TERMS' else None
    order_amount, order_cost_complete = _order_cost_snapshot(db, order.id)
    if not order_cost_complete:
        raise ValueError('Saved V1 line-cost snapshots are incomplete; initialization is blocked.')
    row = OrderPayment(
        purchase_order_id=order.id,
        vendor_id=financial_vendor_id,
        payment_method_id=method.id if method else None,
        payment_category_snapshot=classification.payment_category,
        payment_method_label_snapshot=classification.payment_method_label_snapshot,
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
            note=event_note,
        )
    )
    if is_consignment:
        db.add(
            ConsignmentReplenishment(
                vendor_id=financial_vendor_id,
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


def initialize_new_order_if_configured(
    db: Session, *, order: PurchaseOrder, actor_id: int
) -> OrderPayment | None:
    classification = current_vendor_classification(db, vendor_id=int(order.vendor_id))
    if (
        classification is None
        or classification.payment_category == 'UNCONFIGURED'
        or classification.effective_date > _order_date(order)
    ):
        return None
    method = db.get(PaymentMethod, classification.payment_method_id)
    if (
        method is None
        or not method.is_active
        or method.category != classification.payment_category
    ):
        return None
    _amount, complete = _order_cost_snapshot(db, int(order.id))
    if not complete:
        return None
    if method.category == 'CONSIGNMENT':
        if _canonical_received_quantity(db, order_id=int(order.id)) <= 0:
            return None
        if _consignment_receipt_blocker(db, order_id=int(order.id)):
            return None
    payment = initialize_order_payment(
        db,
        order=order,
        classification=classification,
        actor_id=actor_id,
        event_note=(
            'Initialized at the deliberate V1 receipt lifecycle event.'
            if method.category == 'CONSIGNMENT'
            else 'Initialized at the deliberate V1 placed-order lifecycle event.'
        ),
    )
    if method.category == 'CONSIGNMENT':
        replenishment = db.scalar(
            select(ConsignmentReplenishment).where(
                ConsignmentReplenishment.purchase_order_id == order.id
            )
        )
        sync_consignment_replenishment(db, replenishment=replenishment, actor_id=actor_id)
    return payment


def _consignment_receipt_blocker(db: Session, *, order_id: int) -> str | None:
    lines = db.scalars(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id == order_id,
            PurchaseOrderLine.removed.is_(False),
        )
    ).all()
    for line in lines:
        received = max(int(line.received_qty_total or 0), 0)
        if received <= 0:
            continue
        allocations = db.scalars(
            select(PurchaseOrderStoreAllocation).where(
                PurchaseOrderStoreAllocation.purchase_order_line_id == line.id
            )
        ).all()
        if not allocations:
            return f'Line {line.id} has received quantity but no canonical store-allocation receipt rows.'
        allocation_total = sum(max(int(row.store_received_qty or 0), 0) for row in allocations)
        if allocation_total != received:
            return f'Line {line.id} canonical store receipts do not reconcile to the V1 line total.'
    return None


def historical_backfill_preview(
    db: Session,
    *,
    vendor_id: int,
    payment_method_id: int | None,
    scope_type: str,
    financial_vendor_id: int | None = None,
    effective_from: date | None = None,
    selected_order_ids: list[int] | None = None,
) -> dict:
    if scope_type not in {'ALL_ELIGIBLE', 'FROM_DATE', 'SELECTED'}:
        raise ValueError('Unsupported historical backfill scope.')
    if scope_type == 'FROM_DATE' and effective_from is None:
        raise ValueError('An effective date is required for date-scoped backfill.')
    selected = {int(value) for value in (selected_order_ids or [])}
    financial_vendor_id = int(financial_vendor_id or vendor_id)
    financial_vendor = db.get(Vendor, financial_vendor_id)
    if (
        financial_vendor is None
        or not financial_vendor.active
        or not str(financial_vendor.square_vendor_id or '').strip()
    ):
        raise ValueError('Vendor not available. Add this vendor in Square before assigning it here.')
    method = db.get(PaymentMethod, payment_method_id) if payment_method_id else None
    current = current_vendor_classification(db, vendor_id=vendor_id)
    orders = db.scalars(
        select(PurchaseOrder)
        .where(PurchaseOrder.vendor_id == vendor_id)
        .order_by(PurchaseOrder.ordered_at, PurchaseOrder.id)
    ).all()
    order_ids = [int(order.id) for order in orders]
    existing_by_order = {
        int(row.purchase_order_id): row
        for row in db.scalars(
            select(OrderPayment).where(OrderPayment.purchase_order_id.in_(order_ids or [-1]))
        ).all()
    }
    store_names = purchase_order_scope_names(db, order_ids=order_ids)
    rows = []
    for order in orders:
        order_id = int(order.id)
        order_date = _order_date(order)
        in_scope = (
            scope_type == 'ALL_ELIGIBLE'
            or (scope_type == 'FROM_DATE' and effective_from is not None and order_date >= effective_from)
            or (scope_type == 'SELECTED' and order_id in selected)
        )
        amount, cost_complete = _order_cost_snapshot(db, order_id)
        existing = existing_by_order.get(order_id)
        reason = None
        action = 'CREATE'
        if not in_scope:
            action, reason = 'LEAVE_UNINITIALIZED', 'Outside the selected backfill scope.'
        elif existing is not None:
            action, reason = 'SKIP', f'Existing V2 state: {existing.status}.'
        elif str(order.status.value if hasattr(order.status, 'value') else order.status) not in PLACED_ORDER_STATUSES:
            action, reason = 'BLOCKED', 'V1 order is not in an eligible placed state.'
        elif db.get(Vendor, vendor_id) is None:
            action, reason = 'BLOCKED', 'V1 vendor mapping is missing.'
        elif method is None or not method.is_active:
            action, reason = 'BLOCKED', 'Proposed payment method is missing or inactive.'
        elif not cost_complete:
            action, reason = 'BLOCKED', 'Saved V1 line-cost snapshots are incomplete.'
        elif method.category == 'CONSIGNMENT':
            received_qty = _canonical_received_quantity(db, order_id=order_id)
            receipt_blocker = None
            if received_qty <= 0:
                action = 'BLOCKED'
                reason = 'No canonical V1 receipt exists; consignment begins only when inventory is received.'
            else:
                receipt_blocker = _consignment_receipt_blocker(db, order_id=order_id)
            if received_qty > 0 and receipt_blocker:
                action, reason = 'BLOCKED', receipt_blocker
        term_days = method.term_days if method and method.category == 'TERMS' else None
        rows.append({
            'order': order,
            'order_id': order_id,
            'order_date': order_date,
            'store_scope': ', '.join(store_names.get(order_id, ())) or 'Organization-wide',
            'order_total': amount,
            'cost_complete': cost_complete,
            'proposed_category': method.category if method else 'UNCONFIGURED',
            'proposed_method': masked_payment_method(method) if method else 'Not configured',
            'proposed_term_days': term_days,
            'proposed_due_date': order_date + timedelta(days=term_days) if term_days else None,
            'proposed_consignment': bool(method and method.category == 'CONSIGNMENT'),
            'source_vendor': db.get(Vendor, vendor_id),
            'financial_vendor': financial_vendor,
            'existing_state': existing.status if existing else 'UNINITIALIZED',
            'action': action,
            'reason': reason,
        })
    actionable = [row for row in rows if row['action'] == 'CREATE']
    return {
        'vendor': db.get(Vendor, vendor_id),
        'financial_vendor': financial_vendor,
        'current_classification': current,
        'method': method,
        'scope_type': scope_type,
        'effective_from': effective_from,
        'selected_order_ids': sorted(selected),
        'rows': rows,
        'actionable_count': len(actionable),
        'actionable_total': money(sum((row['order_total'] for row in actionable), Decimal('0'))),
    }


def confirm_historical_backfill(
    db: Session,
    *,
    vendor_id: int,
    payment_method_id: int,
    scope_type: str,
    effective_from: date | None,
    selected_order_ids: list[int],
    confirmation_note: str,
    actor_id: int,
    financial_vendor_id: int | None = None,
    ip: str | None = None,
) -> OrderPaymentBackfillOperation:
    preview = historical_backfill_preview(
        db,
        vendor_id=vendor_id,
        financial_vendor_id=financial_vendor_id,
        payment_method_id=payment_method_id,
        scope_type=scope_type,
        effective_from=effective_from,
        selected_order_ids=selected_order_ids,
    )
    method = preview['method']
    current = preview['current_classification']
    if method is None or not method.is_active:
        raise ValueError('An active payment method is required.')
    if (
        current is not None
        and current.payment_category != 'UNCONFIGURED'
        and int(current.payment_method_id or 0) == int(method.id)
    ):
        classification = current
    else:
        classification = VendorPaymentClassification(
            vendor_id=vendor_id,
            payment_method_id=method.id,
            payment_category=method.category,
            payment_method_label_snapshot=masked_payment_method(method),
            term_days_snapshot=method.term_days if method.category == 'TERMS' else None,
            is_consignment=method.category == 'CONSIGNMENT',
            effective_date=effective_from or portal_today(),
            internal_note=confirmation_note.strip() or 'Order-specific financial assignment.',
            is_current=False,
            created_by_principal_id=actor_id,
        )
        db.add(classification)
        db.flush()
    operation = OrderPaymentBackfillOperation(
        vendor_id=vendor_id,
        vendor_classification_id=classification.id,
        scope_type=scope_type,
        effective_from=effective_from,
        selected_order_ids=selected_order_ids,
        status='CONFIRMED',
        confirmation_note=confirmation_note.strip() or None,
        created_by_principal_id=actor_id,
    )
    db.add(operation)
    db.flush()
    created_count = skipped_count = blocked_count = 0
    for candidate in preview['rows']:
        if candidate['action'] == 'LEAVE_UNINITIALIZED':
            continue
        outcome = 'BLOCKED' if candidate['action'] == 'BLOCKED' else 'SKIPPED'
        reason = candidate['reason']
        payment = None
        if candidate['action'] == 'CREATE':
            payment = initialize_order_payment(
                db,
                order=candidate['order'],
                classification=classification,
                actor_id=actor_id,
                event_note=f'Owner-saved financial assignment operation #{operation.id}.',
                financial_vendor_id=preview['financial_vendor'].id,
            )
            if classification.is_consignment:
                replenishment = db.scalar(
                    select(ConsignmentReplenishment).where(
                        ConsignmentReplenishment.purchase_order_id == candidate['order_id']
                    )
                )
                sync_consignment_replenishment(db, replenishment=replenishment, actor_id=actor_id)
            outcome, reason = 'CREATED', None
            created_count += 1
        elif outcome == 'BLOCKED':
            blocked_count += 1
        else:
            skipped_count += 1
        db.add(OrderPaymentBackfillResult(
            operation_id=operation.id,
            purchase_order_id=candidate['order_id'],
            order_payment_id=payment.id if payment else None,
            outcome=outcome,
            reason=reason,
            proposed_state={
                'category': candidate['proposed_category'],
                'method': candidate['proposed_method'],
                'term_days': candidate['proposed_term_days'],
                'due_date': str(candidate['proposed_due_date'] or ''),
                'consignment': candidate['proposed_consignment'],
                'order_total': str(candidate['order_total']),
                'source_vendor_id': vendor_id,
                'source_square_vendor_id': preview['vendor'].square_vendor_id,
                'financial_vendor_id': preview['financial_vendor'].id,
                'financial_square_vendor_id': preview['financial_vendor'].square_vendor_id,
            },
        ))
    operation.created_count = created_count
    operation.skipped_count = skipped_count
    operation.blocked_count = blocked_count
    operation.status = 'COMPLETED_WITH_BLOCKS' if blocked_count else 'COMPLETED'
    operation.completed_at = utc_now()
    _audit(
        db,
        actor_id=actor_id,
        action='ORDER_PAYMENT_BACKFILL_CONFIRMED',
        entity_type='order_payment_backfill_operation',
        entity_id=operation.id,
        after={
            'vendor_id': vendor_id,
            'source_square_vendor_id': preview['vendor'].square_vendor_id,
            'financial_vendor_id': preview['financial_vendor'].id,
            'financial_square_vendor_id': preview['financial_vendor'].square_vendor_id,
            'created_count': created_count,
            'skipped_count': skipped_count,
            'blocked_count': blocked_count,
        },
        ip=ip,
    )
    return operation


def confirm_financial_assignment_queue(
    db: Session,
    *,
    selected_order_ids: list[int],
    financial_vendor_id: int,
    payment_method_id: int,
    optional_notes: str,
    actor_id: int,
    ip: str | None = None,
) -> OrderPaymentBackfillOperation:
    selected = sorted({int(value) for value in selected_order_ids})
    if not selected:
        raise ValueError('Select at least one order.')
    orders = db.scalars(
        select(PurchaseOrder).where(PurchaseOrder.id.in_(selected)).order_by(PurchaseOrder.id)
    ).all()
    if [int(order.id) for order in orders] != selected:
        raise ValueError('One or more selected orders are unavailable.')

    orders_by_vendor: dict[int, list[int]] = {}
    for order in orders:
        orders_by_vendor.setdefault(int(order.vendor_id), []).append(int(order.id))
    previews = {
        vendor_id: historical_backfill_preview(
            db,
            vendor_id=vendor_id,
            financial_vendor_id=financial_vendor_id,
            payment_method_id=payment_method_id,
            scope_type='SELECTED',
            effective_from=None,
            selected_order_ids=order_ids,
        )
        for vendor_id, order_ids in orders_by_vendor.items()
    }
    method = db.get(PaymentMethod, payment_method_id)
    if method is None or not method.is_active:
        raise ValueError('An active payment method is required.')

    classifications: dict[int, VendorPaymentClassification] = {}
    for vendor_id, preview in previews.items():
        current = preview['current_classification']
        if (
            current is not None
            and current.payment_category != 'UNCONFIGURED'
            and int(current.payment_method_id or 0) == int(method.id)
        ):
            classification = current
        else:
            classification = VendorPaymentClassification(
                vendor_id=vendor_id,
                payment_method_id=method.id,
                payment_category=method.category,
                payment_method_label_snapshot=masked_payment_method(method),
                term_days_snapshot=method.term_days if method.category == 'TERMS' else None,
                is_consignment=method.category == 'CONSIGNMENT',
                effective_date=portal_today(),
                internal_note=optional_notes.strip() or 'Order-specific financial assignment.',
                is_current=False,
                created_by_principal_id=actor_id,
            )
            db.add(classification)
            db.flush()
        classifications[vendor_id] = classification

    single_vendor_id = next(iter(orders_by_vendor)) if len(orders_by_vendor) == 1 else None
    operation = OrderPaymentBackfillOperation(
        vendor_id=single_vendor_id,
        vendor_classification_id=(classifications[single_vendor_id].id if single_vendor_id else None),
        scope_type='SELECTED',
        effective_from=None,
        selected_order_ids=selected,
        status='CONFIRMED',
        confirmation_note=optional_notes.strip() or None,
        created_by_principal_id=actor_id,
    )
    db.add(operation)
    db.flush()

    created_count = skipped_count = blocked_count = 0
    for source_vendor_id, preview in previews.items():
        classification = classifications[source_vendor_id]
        for candidate in preview['rows']:
            if candidate['action'] == 'LEAVE_UNINITIALIZED':
                continue
            outcome = 'BLOCKED' if candidate['action'] == 'BLOCKED' else 'SKIPPED'
            reason = candidate['reason']
            payment = None
            if candidate['action'] == 'CREATE':
                payment = initialize_order_payment(
                    db,
                    order=candidate['order'],
                    classification=classification,
                    actor_id=actor_id,
                    event_note=f'Owner-saved financial assignment operation #{operation.id}.',
                    financial_vendor_id=preview['financial_vendor'].id,
                )
                if classification.is_consignment:
                    replenishment = db.scalar(
                        select(ConsignmentReplenishment).where(
                            ConsignmentReplenishment.purchase_order_id == candidate['order_id']
                        )
                    )
                    sync_consignment_replenishment(db, replenishment=replenishment, actor_id=actor_id)
                outcome, reason = 'CREATED', None
                created_count += 1
            elif outcome == 'BLOCKED':
                blocked_count += 1
            else:
                skipped_count += 1
            db.add(OrderPaymentBackfillResult(
                operation_id=operation.id,
                purchase_order_id=candidate['order_id'],
                order_payment_id=payment.id if payment else None,
                outcome=outcome,
                reason=reason,
                proposed_state={
                    'category': candidate['proposed_category'],
                    'method': candidate['proposed_method'],
                    'term_days': candidate['proposed_term_days'],
                    'due_date': str(candidate['proposed_due_date'] or ''),
                    'consignment': candidate['proposed_consignment'],
                    'order_total': str(candidate['order_total']),
                    'source_vendor_id': source_vendor_id,
                    'source_square_vendor_id': preview['vendor'].square_vendor_id,
                    'financial_vendor_id': preview['financial_vendor'].id,
                    'financial_square_vendor_id': preview['financial_vendor'].square_vendor_id,
                },
            ))

    operation.created_count = created_count
    operation.skipped_count = skipped_count
    operation.blocked_count = blocked_count
    operation.status = 'COMPLETED_WITH_BLOCKS' if blocked_count else 'COMPLETED'
    operation.completed_at = utc_now()
    _audit(
        db,
        actor_id=actor_id,
        action='ORDER_FINANCIAL_ASSIGNMENT_SAVED',
        entity_type='order_payment_backfill_operation',
        entity_id=operation.id,
        after={
            'source_vendor_ids': sorted(orders_by_vendor),
            'financial_vendor_id': financial_vendor_id,
            'financial_square_vendor_id': previews[next(iter(previews))]['financial_vendor'].square_vendor_id,
            'selected_order_ids': selected,
            'created_count': created_count,
            'skipped_count': skipped_count,
            'blocked_count': blocked_count,
        },
        ip=ip,
    )
    return operation


def classification_correction_preview(
    db: Session, *, order_payment_id: int, payment_method_id: int
) -> dict:
    payment = db.get(OrderPayment, order_payment_id)
    method = db.get(PaymentMethod, payment_method_id)
    if payment is None:
        raise LookupError('Order payment not found.')
    if method is None or not method.is_active:
        raise ValueError('Proposed payment method is missing or inactive.')
    order = db.get(PurchaseOrder, payment.purchase_order_id)
    if order is None:
        raise ValueError('Canonical V1 purchase order is missing.')
    replenishment = db.scalar(select(ConsignmentReplenishment).where(
        ConsignmentReplenishment.purchase_order_id == payment.purchase_order_id
    ))
    event_count = int(db.scalar(select(func.count(OrderPaymentEvent.id)).where(
        OrderPaymentEvent.order_payment_id == payment.id
    )) or 0)
    receipt_count = int(db.scalar(select(func.count(ConsignmentReplenishmentReceipt.id)).where(
        ConsignmentReplenishmentReceipt.purchase_order_id == payment.purchase_order_id
    )) or 0)
    ledger_count = int(db.scalar(select(func.count(ConsignmentLedgerEntry.id)).where(
        ConsignmentLedgerEntry.purchase_order_id == payment.purchase_order_id
    )) or 0)
    allocation_count = 0
    if replenishment is not None:
        allocation_count = int(db.scalar(select(func.count(ConsignmentAllocation.id)).where(
            ConsignmentAllocation.replenishment_id == replenishment.id
        )) or 0)
    blockers = []
    if int(payment.payment_method_id or 0) == int(method.id):
        blockers.append('The proposed classification is identical to the current snapshot.')
    if payment.status not in {'UNPAID', 'CONSIGNMENT_ORDERED'} or payment.paid_date or payment.paid_amount:
        blockers.append('The order is paid, settled, or no longer in an untouched initialization state.')
    if event_count != 1:
        blockers.append('The order has financial transition history beyond its initialization event.')
    if receipt_count or ledger_count or allocation_count:
        blockers.append('The order has receipt, ledger, or allocation references.')
    if not payment.order_cost_complete:
        blockers.append('The saved V1 cost snapshot is incomplete.')
    if method.category == 'CONSIGNMENT':
        receipt_blocker = _consignment_receipt_blocker(db, order_id=int(order.id))
        if receipt_blocker:
            blockers.append(receipt_blocker)
    return {
        'payment': payment,
        'order': order,
        'method': method,
        'replenishment': replenishment,
        'prior': {
            'category': payment.payment_category_snapshot,
            'method': payment.payment_method_label_snapshot,
            'treatment': payment.financial_treatment,
            'status': payment.status,
        },
        'proposed': {
            'category': method.category,
            'method': masked_payment_method(method),
            'treatment': 'REPLENISHMENT' if method.category == 'CONSIGNMENT' else 'INVOICE',
            'status': 'CONSIGNMENT_ORDERED' if method.category == 'CONSIGNMENT' else 'UNPAID',
            'term_days': method.term_days if method.category == 'TERMS' else None,
        },
        'impact': {
            'event_count': event_count,
            'receipt_count': receipt_count,
            'ledger_count': ledger_count,
            'allocation_count': allocation_count,
        },
        'blockers': blockers,
        'allowed': not blockers,
    }


def confirm_classification_correction(
    db: Session,
    *,
    order_payment_id: int,
    payment_method_id: int,
    reason: str,
    actor_id: int,
    ip: str | None = None,
) -> OrderPayment:
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError('A correction reason is required.')
    preview = classification_correction_preview(
        db, order_payment_id=order_payment_id, payment_method_id=payment_method_id
    )
    if not preview['allowed']:
        raise ValueError('Classification correction is blocked: ' + ' '.join(preview['blockers']))
    payment = preview['payment']
    order = preview['order']
    method = preview['method']
    replenishment = preview['replenishment']
    prior_status = payment.status
    prior_method_id = payment.payment_method_id
    prior = dict(preview['prior'])
    proposed_consignment = method.category == 'CONSIGNMENT'
    payment.payment_method_id = method.id
    payment.payment_category_snapshot = method.category
    payment.payment_method_label_snapshot = masked_payment_method(method)
    payment.term_days_snapshot = method.term_days if method.category == 'TERMS' else None
    payment.due_date = (
        _order_date(order) + timedelta(days=method.term_days)
        if method.category == 'TERMS' else None
    )
    payment.financial_treatment = 'REPLENISHMENT' if proposed_consignment else 'INVOICE'
    payment.status = 'CONSIGNMENT_ORDERED' if proposed_consignment else 'UNPAID'
    if proposed_consignment and replenishment is None:
        replenishment = ConsignmentReplenishment(
            vendor_id=payment.vendor_id,
            purchase_order_id=payment.purchase_order_id,
            ordered_cost_value=payment.order_amount,
            received_cost_value=Decimal('0'),
            amount_applied=Decimal('0'),
            excess_credit_created=Decimal('0'),
            status='PENDING',
            created_by_principal_id=actor_id,
        )
        db.add(replenishment)
        db.flush()
        sync_consignment_replenishment(db, replenishment=replenishment, actor_id=actor_id)
    elif not proposed_consignment and replenishment is not None:
        db.delete(replenishment)
    db.add(OrderPaymentEvent(
        order_payment_id=payment.id,
        prior_status=prior_status,
        new_status=payment.status,
        prior_payment_method_id=prior_method_id,
        new_payment_method_id=method.id,
        actor_principal_id=actor_id,
        note=f'Owner-confirmed classification correction: {clean_reason}',
    ))
    _audit(
        db,
        actor_id=actor_id,
        action='ORDER_PAYMENT_CLASSIFICATION_CORRECTED',
        entity_type='order_payment',
        entity_id=payment.id,
        before=prior,
        after={**preview['proposed'], 'reason': clean_reason},
        ip=ip,
    )
    return payment


def order_payment_list_rows(db: Session) -> list[dict]:
    orders = db.scalars(
        select(PurchaseOrder)
        .where(PurchaseOrder.status.in_(PLACED_ORDER_STATUSES))
        .order_by(PurchaseOrder.ordered_at.desc().nullslast(), PurchaseOrder.id.desc())
    ).all()
    order_ids = [int(order.id) for order in orders]
    payments = {
        int(row.purchase_order_id): row
        for row in db.scalars(
            select(OrderPayment).where(OrderPayment.purchase_order_id.in_(order_ids or [-1]))
        ).all()
    }
    vendors = {int(row.id): row for row in db.scalars(select(Vendor)).all()}
    classifications = {
        int(row.vendor_id): row
        for row in db.scalars(
            select(VendorPaymentClassification).where(VendorPaymentClassification.is_current.is_(True))
        ).all()
    }
    methods = {
        int(row.id): row for row in db.scalars(select(PaymentMethod)).all()
    }
    store_names = purchase_order_scope_names(db, order_ids=order_ids)
    rows = []
    for order in orders:
        payment = payments.get(int(order.id))
        classification = classifications.get(int(order.vendor_id))
        amount, complete = _order_cost_snapshot(db, int(order.id))
        if payment is not None:
            position = (
                order_financial_position(db, order_payment_id=payment.id)
                if payment.financial_treatment == 'INVOICE'
                else None
            )
            display_state = position['status'] if position else payment.status
            reason = None
        elif not complete:
            display_state = 'BLOCKED'
            reason = 'Saved V1 line-cost snapshots are incomplete.'
        elif classification is None or classification.payment_category == 'UNCONFIGURED':
            display_state = 'UNINITIALIZED'
            reason = 'Ready for an owner-saved financial assignment.'
        elif (
            classification.is_consignment
            and _canonical_received_quantity(db, order_id=int(order.id)) <= 0
        ):
            display_state = 'UNINITIALIZED'
            reason = 'Waiting for a canonical V1 receipt before entering consignment.'
        else:
            display_state = 'UNINITIALIZED'
            reason = 'Ready for an owner-saved financial assignment.'
        rows.append({
            'order': order,
            'vendor': vendors.get(int(order.vendor_id)),
            'financial_vendor': vendors.get(int(payment.vendor_id)) if payment else None,
            'payment': payment,
            'payment_method': methods.get(int(payment.payment_method_id)) if payment and payment.payment_method_id else None,
            'classification': classification,
            'classification_method': methods.get(int(classification.payment_method_id)) if classification and classification.payment_method_id else None,
            'order_amount': payment.order_amount if payment else amount,
            'remaining_amount': position['remaining_amount'] if payment is not None and position else (
                payment.order_amount if payment is not None else amount
            ),
            'cost_complete': payment.order_cost_complete if payment else complete,
            'display_state': display_state,
            'reason': reason,
            'store_names': store_names.get(int(order.id), ()),
            'store_scope': ', '.join(store_names.get(int(order.id), ())) or 'Organization-wide',
            'store_display': (
                store_names[int(order.id)][0]
                if len(store_names.get(int(order.id), ())) == 1
                else f'{store_names[int(order.id)][0]} +{len(store_names[int(order.id)]) - 1}'
                if store_names.get(int(order.id))
                else 'Organization-wide'
            ),
        })
    return rows


def purchase_order_scope_labels(db: Session, *, order_ids: list[int]) -> dict[int, str]:
    return {
        order_id: ', '.join(store_names)
        for order_id, store_names in purchase_order_scope_names(db, order_ids=order_ids).items()
    }


def purchase_order_scope_names(db: Session, *, order_ids: list[int]) -> dict[int, tuple[str, ...]]:
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
    return {order_id: tuple(store_names) for order_id, store_names in names.items()}


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


def _active_payment_total(db: Session, *, order_payment_id: int) -> Decimal:
    rows = db.execute(
        select(OrderManualPaymentEntry.entry_type, func.coalesce(func.sum(OrderManualPaymentEntry.amount), 0))
        .where(OrderManualPaymentEntry.order_payment_id == order_payment_id)
        .group_by(OrderManualPaymentEntry.entry_type)
    ).all()
    totals = {str(kind): money(amount) for kind, amount in rows}
    return money(totals.get('PAYMENT', Decimal('0')) + totals.get('REPLACEMENT', Decimal('0')) - totals.get('REVERSAL', Decimal('0')))


def _active_adjustment_total(db: Session, *, order_payment_id: int) -> Decimal:
    rows = db.execute(
        select(OrderBalanceAdjustment.direction, func.coalesce(func.sum(OrderBalanceAdjustment.amount), 0))
        .where(OrderBalanceAdjustment.order_payment_id == order_payment_id)
        .group_by(OrderBalanceAdjustment.direction)
    ).all()
    totals = {str(kind): money(amount) for kind, amount in rows}
    # Reversal rows carry the opposite sign of the linked original in their type snapshot.
    reversals = db.scalars(select(OrderBalanceAdjustment).where(
        OrderBalanceAdjustment.order_payment_id == order_payment_id,
        OrderBalanceAdjustment.direction == 'REVERSAL',
    )).all()
    reversal_effect = Decimal('0')
    for row in reversals:
        original = db.get(OrderBalanceAdjustment, row.reversed_adjustment_id) if row.reversed_adjustment_id else None
        if original:
            reversal_effect += row.amount if original.direction == 'DECREASE' else -row.amount
    return money(totals.get('INCREASE', Decimal('0')) - totals.get('DECREASE', Decimal('0')) + reversal_effect)


def order_financial_position(db: Session, *, order_payment_id: int) -> dict:
    payment = db.get(OrderPayment, order_payment_id)
    if payment is None:
        raise LookupError('Order payment not found.')
    adjustments = _active_adjustment_total(db, order_payment_id=order_payment_id)
    adjusted_amount = money(payment.order_amount + adjustments)
    paid = _active_payment_total(db, order_payment_id=order_payment_id)
    event_count = int(db.scalar(select(func.count()).select_from(OrderManualPaymentEntry).where(
        OrderManualPaymentEntry.order_payment_id == order_payment_id
    )) or 0)
    if event_count == 0 and payment.status == 'PAID' and payment.paid_amount is not None:
        paid = money(payment.paid_amount)
    remaining = money(adjusted_amount - paid)
    status = 'UNPAID' if paid == 0 else 'PARTIALLY_PAID'
    if paid == adjusted_amount:
        status = 'PAID'
    elif paid > adjusted_amount:
        status = 'OVERPAID'
    return {
        'original_amount': money(payment.order_amount), 'adjustments': adjustments,
        'adjusted_amount': adjusted_amount, 'payments_recorded': paid,
        'remaining_amount': remaining, 'status': status,
    }


def record_manual_order_payment(
    db: Session, *, order_payment_id: int, payment_method_id: int, amount: Decimal,
    effective_date: date, reason: str, actor_id: int, confirmation_number: str | None = None,
    internal_note: str | None = None, entry_type: str = 'PAYMENT',
    replacement_for_entry_id: int | None = None, ip: str | None = None,
) -> OrderManualPaymentEntry:
    payment = db.get(OrderPayment, order_payment_id)
    if payment is None:
        raise LookupError('Order payment not found.')
    if payment.financial_treatment != 'INVOICE':
        raise ValueError('Consignment orders require an exceptional cash settlement, not an ordinary payment.')
    method = db.get(PaymentMethod, payment_method_id)
    if method is None or method.category == 'CONSIGNMENT' or not method.is_active:
        raise ValueError('Choose an active non-consignment payment method.')
    amount = money(amount)
    if amount <= 0:
        raise ValueError('Amount must be greater than zero.')
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError('A reason or reference is required.')
    if entry_type not in {'PAYMENT', 'REPLACEMENT'}:
        raise ValueError('Unsupported payment entry type.')
    if entry_type == 'REPLACEMENT':
        replaced = db.get(OrderManualPaymentEntry, replacement_for_entry_id) if replacement_for_entry_id else None
        reversal = db.scalar(select(OrderManualPaymentEntry).where(
            OrderManualPaymentEntry.original_entry_id == replacement_for_entry_id,
            OrderManualPaymentEntry.entry_type == 'REVERSAL',
        )) if replacement_for_entry_id else None
        if replaced is None or reversal is None or int(replaced.order_payment_id) != int(payment.id):
            raise ValueError('A replacement payment can only follow a reversed payment for this order.')
    existing_event_count = int(db.scalar(select(func.count()).select_from(OrderManualPaymentEntry).where(
        OrderManualPaymentEntry.order_payment_id == payment.id
    )) or 0)
    if existing_event_count == 0 and payment.status == 'PAID' and money(payment.paid_amount or 0) > 0:
        db.add(OrderManualPaymentEntry(
            order_payment_id=payment.id, vendor_id=payment.vendor_id, entry_type='PAYMENT',
            amount=money(payment.paid_amount), payment_method_id=payment.payment_method_id,
            effective_date=payment.paid_date or effective_date,
            reason='Preserved prior V2 paid snapshot before append-only payment entry.',
            created_by_principal_id=actor_id,
        ))
        db.flush()
    row = OrderManualPaymentEntry(
        order_payment_id=payment.id, vendor_id=payment.vendor_id, entry_type=entry_type,
        amount=amount, payment_method_id=method.id, effective_date=effective_date,
        reason=clean_reason, confirmation_number=(confirmation_number or '').strip() or None,
        internal_note=(internal_note or '').strip() or None,
        replacement_for_entry_id=replacement_for_entry_id, created_by_principal_id=actor_id,
    )
    db.add(row); db.flush()
    position = order_financial_position(db, order_payment_id=payment.id)
    payment.paid_amount = position['payments_recorded']
    payment.status = 'PAID' if position['status'] == 'PAID' else 'UNPAID'
    payment.paid_date = effective_date if position['status'] == 'PAID' else None
    _audit(
        db, actor_id=actor_id, action='MANUAL_ORDER_PAYMENT_RECORDED',
        entity_type='order_manual_payment_entry', entity_id=row.id,
        after={
            'vendor_id': payment.vendor_id, 'entry_type': entry_type,
            'payment_method_id': method.id, 'amount': str(amount),
            'effective_date': str(effective_date), 'reason': clean_reason,
            'confirmation_number': row.confirmation_number,
            'internal_note': row.internal_note,
            'replacement_for_entry_id': replacement_for_entry_id,
            'position': position['status'],
        },
        ip=ip,
    )
    return row


def reverse_manual_order_payment(
    db: Session, *, entry_id: int, amount: Decimal, effective_date: date,
    reason: str, actor_id: int, ip: str | None = None,
) -> OrderManualPaymentEntry:
    original = db.get(OrderManualPaymentEntry, entry_id)
    if original is None or original.entry_type == 'REVERSAL':
        raise LookupError('Original payment entry not found.')
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError('A reversal reason is required.')
    amount = money(amount)
    already = money(db.scalar(select(func.coalesce(func.sum(OrderManualPaymentEntry.amount), 0)).where(
        OrderManualPaymentEntry.original_entry_id == original.id,
        OrderManualPaymentEntry.entry_type == 'REVERSAL',
    )))
    if amount <= 0 or money(already + amount) > money(original.amount):
        raise ValueError('Reversal amount must be positive and cannot exceed the active payment amount.')
    row = OrderManualPaymentEntry(
        order_payment_id=original.order_payment_id, vendor_id=original.vendor_id,
        entry_type='REVERSAL', amount=amount, payment_method_id=original.payment_method_id,
        effective_date=effective_date, reason=clean_reason, original_entry_id=original.id,
        created_by_principal_id=actor_id,
    )
    db.add(row); db.flush()
    payment = db.get(OrderPayment, original.order_payment_id)
    position = order_financial_position(db, order_payment_id=original.order_payment_id)
    payment.paid_amount = position['payments_recorded']
    payment.status = 'PAID' if position['status'] == 'PAID' else 'UNPAID'
    payment.paid_date = effective_date if position['status'] == 'PAID' else None
    _audit(
        db, actor_id=actor_id, action='MANUAL_ORDER_PAYMENT_REVERSED',
        entity_type='order_manual_payment_entry', entity_id=row.id,
        before={'original_entry_id': original.id, 'original_amount': str(original.amount)},
        after={
            'payment_method_id': original.payment_method_id, 'amount': str(amount),
            'effective_date': str(effective_date), 'reason': clean_reason,
            'position': position['status'],
        },
        ip=ip,
    )
    return row


def create_order_balance_adjustment(
    db: Session, *, order_payment_id: int, direction: str, adjustment_type: str,
    amount: Decimal, effective_date: date, reason: str, actor_id: int,
    internal_note: str | None = None, replacement_for_adjustment_id: int | None = None,
    ip: str | None = None,
) -> OrderBalanceAdjustment:
    payment = db.get(OrderPayment, order_payment_id)
    if payment is None:
        raise LookupError('Order payment not found.')
    if payment.financial_treatment != 'INVOICE':
        raise ValueError('Use the consignment charge or credit workflow for consignment records.')
    direction = direction.strip().upper()
    if direction not in {'INCREASE', 'DECREASE'}:
        raise ValueError('Choose increase or decrease.')
    amount = money(amount)
    if amount <= 0 or not reason.strip() or not adjustment_type.strip():
        raise ValueError('Adjustment type, positive amount, and reason are required.')
    if replacement_for_adjustment_id is not None:
        replaced = db.get(OrderBalanceAdjustment, replacement_for_adjustment_id)
        reversal = db.scalar(select(OrderBalanceAdjustment).where(
            OrderBalanceAdjustment.reversed_adjustment_id == replacement_for_adjustment_id
        ))
        if replaced is None or reversal is None or int(replaced.order_payment_id) != int(payment.id):
            raise ValueError('A replacement can only follow a reversed adjustment for this order.')
    prior = order_financial_position(db, order_payment_id=payment.id)['adjusted_amount']
    resulting = money(prior + amount if direction == 'INCREASE' else prior - amount)
    row = OrderBalanceAdjustment(
        order_payment_id=payment.id, vendor_id=payment.vendor_id, direction=direction,
        adjustment_type=adjustment_type.strip().upper(), amount=amount, effective_date=effective_date,
        reason=reason.strip(), internal_note=(internal_note or '').strip() or None,
        original_calculated_amount=payment.order_amount, prior_adjusted_amount=prior,
        resulting_adjusted_amount=resulting, replacement_for_adjustment_id=replacement_for_adjustment_id,
        created_by_principal_id=actor_id,
    )
    db.add(row); db.flush()
    _audit(
        db, actor_id=actor_id, action='ORDER_BALANCE_ADJUSTED',
        entity_type='order_balance_adjustment', entity_id=row.id,
        before={'adjusted_amount': str(prior)},
        after={
            'vendor_id': payment.vendor_id, 'direction': direction,
            'adjustment_type': row.adjustment_type, 'amount': str(amount),
            'effective_date': str(effective_date), 'reason': row.reason,
            'internal_note': row.internal_note,
            'original_calculated_amount': str(payment.order_amount),
            'resulting_adjusted_amount': str(resulting),
            'replacement_for_adjustment_id': replacement_for_adjustment_id,
        }, ip=ip,
    )
    return row


def reverse_order_balance_adjustment(
    db: Session, *, adjustment_id: int, effective_date: date, reason: str,
    actor_id: int, ip: str | None = None,
) -> OrderBalanceAdjustment:
    original = db.get(OrderBalanceAdjustment, adjustment_id)
    if original is None or original.direction == 'REVERSAL':
        raise LookupError('Original adjustment not found.')
    if db.scalar(select(OrderBalanceAdjustment.id).where(OrderBalanceAdjustment.reversed_adjustment_id == original.id)):
        raise ValueError('This adjustment has already been reversed.')
    if not reason.strip():
        raise ValueError('A reversal reason is required.')
    prior = order_financial_position(db, order_payment_id=original.order_payment_id)['adjusted_amount']
    resulting = money(prior - original.amount if original.direction == 'INCREASE' else prior + original.amount)
    row = OrderBalanceAdjustment(
        order_payment_id=original.order_payment_id, vendor_id=original.vendor_id,
        direction='REVERSAL', adjustment_type='ADJUSTMENT_REVERSAL', amount=original.amount,
        effective_date=effective_date, reason=reason.strip(), original_calculated_amount=original.original_calculated_amount,
        prior_adjusted_amount=prior, resulting_adjusted_amount=resulting,
        reversed_adjustment_id=original.id, created_by_principal_id=actor_id,
    )
    db.add(row); db.flush()
    _audit(
        db, actor_id=actor_id, action='ORDER_BALANCE_ADJUSTMENT_REVERSED',
        entity_type='order_balance_adjustment', entity_id=row.id,
        before={'original_adjustment_id': original.id, 'prior_adjusted_amount': str(prior)},
        after={
            'amount': str(original.amount), 'effective_date': str(effective_date),
            'reason': reason.strip(), 'resulting_adjusted_amount': str(resulting),
        }, ip=ip,
    )
    return row


def vendor_assignment_preview(
    db: Session, *, order_ids: list[int], new_vendor_id: int,
) -> dict:
    if not order_ids:
        raise ValueError('Select at least one purchase order.')
    new_vendor = db.get(Vendor, new_vendor_id)
    if new_vendor is None or not new_vendor.active or not str(new_vendor.square_vendor_id or '').strip():
        raise ValueError('Vendor not available. Add this vendor in Square before assigning it here.')
    rows = []
    for order_id in dict.fromkeys(order_ids):
        order = db.get(PurchaseOrder, order_id)
        payment = db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id == order_id))
        if order is None or payment is None:
            rows.append({'order_id': order_id, 'allowed': False, 'blocked_reason': 'Order Payments setup is required.'})
            continue
        source = db.get(Vendor, order.vendor_id)
        current = db.get(Vendor, payment.vendor_id)
        manual_payment_count = int(db.scalar(select(func.count()).select_from(OrderManualPaymentEntry).where(OrderManualPaymentEntry.order_payment_id == payment.id)) or 0)
        adjustment_count = int(db.scalar(select(func.count()).select_from(OrderBalanceAdjustment).where(OrderBalanceAdjustment.order_payment_id == payment.id)) or 0)
        ledger_count = int(db.scalar(select(func.count()).select_from(ConsignmentLedgerEntry).where(ConsignmentLedgerEntry.purchase_order_id == order.id)) or 0)
        replenishment = db.scalar(select(ConsignmentReplenishment).where(ConsignmentReplenishment.purchase_order_id == order.id))
        impacts = {
            'manual_payment_entries': manual_payment_count,
            'balance_adjustments': adjustment_count,
            'consignment_ledger_entries': ledger_count,
            'requires_typed_transfer': bool(manual_payment_count or adjustment_count or ledger_count or payment.status == 'PAID'),
        }
        rows.append({
            'order_id': int(order.id), 'order': order, 'payment': payment,
            'source_vendor': source, 'current_vendor': current, 'new_vendor': new_vendor,
            'payment_method': db.get(PaymentMethod, payment.payment_method_id) if payment.payment_method_id else None,
            'order_total': money(payment.order_amount),
            'payment_state': order_financial_position(db, order_payment_id=payment.id)['status'] if payment.financial_treatment == 'INVOICE' else payment.status,
            'consignment_state': replenishment.status if replenishment else 'Not consignment',
            'downstream_impact': impacts, 'allowed': int(payment.vendor_id) != int(new_vendor.id),
            'blocked_reason': 'Already assigned to this vendor.' if int(payment.vendor_id) == int(new_vendor.id) else None,
        })
    return {'new_vendor': new_vendor, 'rows': rows, 'total': money(sum((row.get('order_total', Decimal('0')) for row in rows if row.get('allowed')), Decimal('0')))}


def confirm_vendor_reassignment(
    db: Session, *, order_ids: list[int], new_vendor_id: int, effective_date: date,
    reason: str, internal_note: str | None, actor_id: int, ip: str | None = None,
) -> VendorAssignmentOperation:
    if not reason.strip():
        raise ValueError('A reason is required.')
    preview = vendor_assignment_preview(db, order_ids=order_ids, new_vendor_id=new_vendor_id)
    allowed = [row for row in preview['rows'] if row.get('allowed')]
    if not allowed or len(allowed) != len(preview['rows']):
        blocked = next((row.get('blocked_reason') for row in preview['rows'] if not row.get('allowed')), None)
        raise ValueError(blocked or 'One or more selected orders cannot be reassigned.')
    operation = VendorAssignmentOperation(
        scope_type='SINGLE' if len(allowed) == 1 else 'BULK', effective_date=effective_date,
        reason=reason.strip(), internal_note=(internal_note or '').strip() or None,
        created_by_principal_id=actor_id,
    )
    db.add(operation); db.flush()
    for item in allowed:
        order, payment = item['order'], item['payment']
        source, prior, new = item['source_vendor'], item['current_vendor'], item['new_vendor']
        transfer_ids: list[int] = []

        # Preserve posted ordinary payment events: reverse the old-vendor effect and
        # recreate the same active amount under the new financial vendor.
        if payment.financial_treatment == 'INVOICE':
            source_entries = db.scalars(select(OrderManualPaymentEntry).where(
                OrderManualPaymentEntry.order_payment_id == payment.id,
                OrderManualPaymentEntry.entry_type.in_(('PAYMENT', 'REPLACEMENT')),
            ).order_by(OrderManualPaymentEntry.id)).all()
            if not source_entries and payment.status == 'PAID' and money(payment.paid_amount or payment.order_amount) > 0:
                baseline = OrderManualPaymentEntry(
                    order_payment_id=payment.id, vendor_id=prior.id, entry_type='PAYMENT',
                    amount=money(payment.paid_amount or payment.order_amount),
                    payment_method_id=payment.payment_method_id,
                    effective_date=payment.paid_date or effective_date,
                    reason='Preserved paid snapshot before vendor assignment transfer.',
                    created_by_principal_id=actor_id,
                )
                db.add(baseline); db.flush(); source_entries = [baseline]
            for source_entry in source_entries:
                reversed_amount = money(db.scalar(select(func.coalesce(func.sum(OrderManualPaymentEntry.amount), 0)).where(
                    OrderManualPaymentEntry.original_entry_id == source_entry.id,
                    OrderManualPaymentEntry.entry_type == 'REVERSAL',
                )))
                active_amount = money(source_entry.amount - reversed_amount)
                if active_amount > 0 and int(source_entry.vendor_id) == int(prior.id):
                    reversal = OrderManualPaymentEntry(
                        order_payment_id=payment.id, vendor_id=prior.id, entry_type='REVERSAL',
                        amount=active_amount, payment_method_id=source_entry.payment_method_id,
                        effective_date=effective_date, reason=f'Vendor assignment transfer: {reason.strip()}',
                        original_entry_id=source_entry.id, created_by_principal_id=actor_id,
                    )
                    db.add(reversal); db.flush(); transfer_ids.append(int(reversal.id))
                    replacement = OrderManualPaymentEntry(
                        order_payment_id=payment.id, vendor_id=new.id, entry_type='REPLACEMENT',
                        amount=active_amount, payment_method_id=source_entry.payment_method_id,
                        effective_date=effective_date, reason=f'Vendor assignment transfer: {reason.strip()}',
                        replacement_for_entry_id=source_entry.id, created_by_principal_id=actor_id,
                    )
                    db.add(replacement); db.flush(); transfer_ids.append(int(replacement.id))

            # Balance corrections retain their original rows and move through an
            # equal reversal/replacement pair under the new financial vendor.
            active_adjustments = db.scalars(select(OrderBalanceAdjustment).where(
                OrderBalanceAdjustment.order_payment_id == payment.id,
                OrderBalanceAdjustment.direction.in_(('INCREASE', 'DECREASE')),
                OrderBalanceAdjustment.vendor_id == prior.id,
            ).order_by(OrderBalanceAdjustment.id)).all()
            for adjustment in active_adjustments:
                if db.scalar(select(OrderBalanceAdjustment.id).where(
                    OrderBalanceAdjustment.reversed_adjustment_id == adjustment.id
                )):
                    continue
                prior_adjusted = order_financial_position(db, order_payment_id=payment.id)['adjusted_amount']
                reversed_result = money(prior_adjusted - adjustment.amount if adjustment.direction == 'INCREASE' else prior_adjusted + adjustment.amount)
                reversal = OrderBalanceAdjustment(
                    order_payment_id=payment.id, vendor_id=prior.id, direction='REVERSAL',
                    adjustment_type='ADJUSTMENT_REVERSAL', amount=adjustment.amount,
                    effective_date=effective_date, reason=f'Vendor assignment transfer: {reason.strip()}',
                    original_calculated_amount=payment.order_amount, prior_adjusted_amount=prior_adjusted,
                    resulting_adjusted_amount=reversed_result, reversed_adjustment_id=adjustment.id,
                    created_by_principal_id=actor_id,
                )
                db.add(reversal); db.flush(); transfer_ids.append(int(reversal.id))
                replacement = OrderBalanceAdjustment(
                    order_payment_id=payment.id, vendor_id=new.id, direction=adjustment.direction,
                    adjustment_type=adjustment.adjustment_type, amount=adjustment.amount,
                    effective_date=effective_date, reason=f'Vendor assignment transfer: {reason.strip()}',
                    original_calculated_amount=payment.order_amount, prior_adjusted_amount=reversed_result,
                    resulting_adjusted_amount=prior_adjusted, replacement_for_adjustment_id=adjustment.id,
                    created_by_principal_id=actor_id,
                )
                db.add(replacement); db.flush(); transfer_ids.append(int(replacement.id))

        # Posted consignment facts stay untouched. Equal transfer pairs move their
        # vendor effect while keeping the combined ledger net exactly zero.
        transferred_applied = money(db.scalar(select(func.coalesce(func.sum(ConsignmentLedgerEntry.amount), 0)).where(
            ConsignmentLedgerEntry.purchase_order_id == order.id,
            ConsignmentLedgerEntry.entry_type == 'REPLENISHMENT_APPLIED',
        )))
        credit_parts = db.execute(select(
            ConsignmentLedgerEntry.entry_type, func.coalesce(func.sum(ConsignmentLedgerEntry.amount), 0)
        ).where(
            ConsignmentLedgerEntry.purchase_order_id == order.id,
            ConsignmentLedgerEntry.entry_type.in_(('REPLENISHMENT_CREDIT_CREATED', 'REPLENISHMENT_CREDIT_USED')),
        ).group_by(ConsignmentLedgerEntry.entry_type)).all()
        credit_totals = {str(kind): money(amount) for kind, amount in credit_parts}
        transferred_credit = max(money(
            credit_totals.get('REPLENISHMENT_CREDIT_CREATED', 0)
            - credit_totals.get('REPLENISHMENT_CREDIT_USED', 0)
        ), Decimal('0'))
        ledger_effect = money(transferred_applied + transferred_credit)
        item['downstream_impact']['transferred_replenishment_applied'] = str(transferred_applied)
        item['downstream_impact']['transferred_available_credit'] = str(transferred_credit)
        if ledger_effect > 0:
            for vendor_id, kind in ((prior.id, 'VENDOR_ASSIGNMENT_TRANSFER_OUT'), (new.id, 'VENDOR_ASSIGNMENT_TRANSFER_IN')):
                entry = ConsignmentLedgerEntry(
                    vendor_id=vendor_id, entry_type=kind, effective_at=_effective_at(effective_date),
                    amount=ledger_effect, purchase_order_id=order.id,
                    note=f'Vendor assignment operation #{operation.id}: {reason.strip()}',
                    created_by_principal_id=actor_id,
                )
                db.add(entry); db.flush(); transfer_ids.append(int(entry.id))

        prior_state = payment.status
        replenishment = db.scalar(select(ConsignmentReplenishment).where(ConsignmentReplenishment.purchase_order_id == order.id))
        prior_consignment_state = replenishment.status if replenishment else None
        payment.vendor_id = new.id
        # With no posted activity this remains an unposted assignment read model.
        if replenishment and not item['downstream_impact']['consignment_ledger_entries']:
            replenishment.vendor_id = new.id
        change = VendorAssignmentChange(
            operation_id=operation.id, purchase_order_id=order.id, order_payment_id=payment.id,
            source_vendor_id=source.id, prior_financial_vendor_id=prior.id, new_financial_vendor_id=new.id,
            source_vendor_name_snapshot=source.name, prior_vendor_name_snapshot=prior.name,
            new_vendor_name_snapshot=new.name, source_square_vendor_id=source.square_vendor_id,
            prior_square_vendor_id=prior.square_vendor_id, new_square_vendor_id=new.square_vendor_id,
            prior_payment_state=prior_state, prior_consignment_state=prior_consignment_state,
            downstream_impact=item['downstream_impact'], transfer_entry_ids=transfer_ids,
            created_by_principal_id=actor_id,
        )
        db.add(change); db.flush()
        _audit(
            db, actor_id=actor_id, action='ORDER_FINANCIAL_VENDOR_CHANGED',
            entity_type='vendor_assignment_change', entity_id=change.id,
            before={
                'source_vendor_id': source.id,
                'source_square_vendor_id': source.square_vendor_id,
                'financial_vendor_id': prior.id,
                'financial_square_vendor_id': prior.square_vendor_id,
            },
            after={
                'financial_vendor_id': new.id,
                'financial_square_vendor_id': new.square_vendor_id,
                'effective_date': str(effective_date),
                'reason': reason.strip(),
                'internal_note': operation.internal_note,
                'operation_id': operation.id,
                'downstream_impact': item['downstream_impact'],
                'transfer_entry_ids': transfer_ids,
            },
            ip=ip,
        )
    return operation


@dataclass(frozen=True)
class ConsignmentBalance:
    cogs_generated: Decimal
    replenishment_applied: Decimal
    cash_adjustments: Decimal
    approved_credits: Decimal
    unreplenished_cogs: Decimal
    available_replenishment_credit: Decimal
    manual_charges: Decimal = Decimal('0.00')
    manual_credits: Decimal = Decimal('0.00')


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
    manual_charges = normalized.get('MANUAL_CHARGES', Decimal('0.00')) + sum(
        (normalized.get(key, Decimal('0.00')) for key in MANUAL_CHARGE_TYPES), Decimal('0.00')
    )
    manual_credits = normalized.get('MANUAL_CREDITS', Decimal('0.00')) + sum(
        (normalized.get(key, Decimal('0.00')) for key in MANUAL_CREDIT_TYPES), Decimal('0.00')
    )
    before_manual_credit = money(cogs + manual_charges - applied - cash - credits)
    unreplenished = max(before_manual_credit - manual_credits, Decimal('0.00'))
    available = max(
        normalized.get('REPLENISHMENT_CREDIT_CREATED', Decimal('0.00'))
        - normalized.get('REPLENISHMENT_CREDIT_USED', Decimal('0.00')),
        Decimal('0.00'),
    ) + max(manual_credits - max(before_manual_credit, Decimal('0.00')), Decimal('0.00'))
    return ConsignmentBalance(
        cogs, applied, cash, credits, unreplenished, money(available),
        money(manual_charges), money(manual_credits),
    )


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
    totals = {str(row.entry_type): money(row[1]) for row in rows}
    adjustment_rows = db.execute(
        select(
            ConsignmentManualAdjustment.direction,
            func.coalesce(func.sum(ConsignmentManualAdjustment.amount), 0),
        )
        .where(ConsignmentManualAdjustment.vendor_id == vendor_id)
        .group_by(ConsignmentManualAdjustment.direction)
    ).all()
    totals['MANUAL_CHARGES'] = sum(
        (money(row[1]) for row in adjustment_rows if row.direction == 'INCREASE'), Decimal('0.00')
    )
    totals['MANUAL_CREDITS'] = sum(
        (money(row[1]) for row in adjustment_rows if row.direction == 'DECREASE'), Decimal('0.00')
    )
    assignment_changes = db.scalars(select(VendorAssignmentChange).where(
        (VendorAssignmentChange.prior_financial_vendor_id == vendor_id)
        | (VendorAssignmentChange.new_financial_vendor_id == vendor_id)
    )).all()
    for change in assignment_changes:
        applied = money((change.downstream_impact or {}).get('transferred_replenishment_applied', 0))
        available = money((change.downstream_impact or {}).get('transferred_available_credit', 0))
        if int(change.prior_financial_vendor_id) == vendor_id:
            totals['MANUAL_CHARGES'] = money(totals.get('MANUAL_CHARGES', 0) + applied)
            totals['REPLENISHMENT_CREDIT_USED'] = money(totals.get('REPLENISHMENT_CREDIT_USED', 0) + available)
        if int(change.new_financial_vendor_id) == vendor_id:
            totals['MANUAL_CREDITS'] = money(totals.get('MANUAL_CREDITS', 0) + applied)
            totals['REPLENISHMENT_CREDIT_CREATED'] = money(totals.get('REPLENISHMENT_CREDIT_CREATED', 0) + available)
    for key in MANUAL_ADJUSTMENT_TYPES + ('CORRECTION_REVERSAL',):
        totals.pop(key, None)
    return calculate_consignment_balance(totals)


def _effective_at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=PORTAL_TIMEZONE).astimezone(timezone.utc)


def _report_adjustment_position(db: Session, *, report: ConsignmentReport) -> tuple[Decimal, Decimal]:
    rows = db.execute(
        select(
            ConsignmentManualAdjustment.direction,
            func.coalesce(func.sum(ConsignmentManualAdjustment.amount), 0),
        )
        .where(ConsignmentManualAdjustment.report_id == report.id)
        .group_by(ConsignmentManualAdjustment.direction)
    ).all()
    increases = sum((money(row[1]) for row in rows if row.direction == 'INCREASE'), Decimal('0'))
    decreases = sum((money(row[1]) for row in rows if row.direction == 'DECREASE'), Decimal('0'))
    original = money(report.total_cogs)
    return original, max(money(original + increases - decreases), Decimal('0.00'))


def create_consignment_adjustment(
    db: Session,
    *,
    vendor_id: int,
    report_id: int | None,
    target_ledger_entry_id: int | None,
    adjustment_type: str,
    direction: str,
    amount: Decimal,
    effective_date: date,
    reason: str,
    internal_note: str | None,
    actor_id: int,
    replacement_for_adjustment_id: int | None = None,
    ip: str | None = None,
) -> ConsignmentManualAdjustment:
    if db.get(Vendor, vendor_id) is None:
        raise LookupError('Vendor not found.')
    if (report_id is None) == (target_ledger_entry_id is None):
        raise ValueError('Choose one report or ledger activity to adjust.')
    adjustment_type = adjustment_type.strip().upper()
    direction = direction.strip().upper()
    if adjustment_type not in MANUAL_ADJUSTMENT_TYPES:
        raise ValueError('Choose a supported adjustment type.')
    expected_direction = 'INCREASE' if adjustment_type in MANUAL_CHARGE_TYPES else 'DECREASE'
    if direction != expected_direction:
        raise ValueError('The selected increase or decrease does not match the adjustment type.')
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError('A reason is required.')
    amount = money(amount)
    if amount <= 0:
        raise ValueError('Amount must be greater than zero.')
    report = db.get(ConsignmentReport, report_id) if report_id is not None else None
    target_ledger = db.get(ConsignmentLedgerEntry, target_ledger_entry_id) if target_ledger_entry_id else None
    if report_id is not None and (report is None or int(report.vendor_id) != vendor_id):
        raise ValueError('The selected report does not belong to this vendor.')
    if target_ledger_entry_id is not None and (
        target_ledger is None or int(target_ledger.vendor_id) != vendor_id
    ):
        raise ValueError('The selected ledger activity does not belong to this vendor.')
    if replacement_for_adjustment_id is not None:
        replaced = db.get(ConsignmentManualAdjustment, replacement_for_adjustment_id)
        reversal = db.scalar(select(ConsignmentManualAdjustment).where(
            ConsignmentManualAdjustment.reversed_adjustment_id == replacement_for_adjustment_id
        ))
        if replaced is None or reversal is None or int(replaced.vendor_id) != vendor_id:
            raise ValueError('A replacement can only follow a reversed adjustment for this vendor.')
        if replaced.report_id != report_id or replaced.target_ledger_entry_id != target_ledger_entry_id:
            raise ValueError('The replacement must use the same report or ledger target.')
    if report is not None:
        original, prior = _report_adjustment_position(db, report=report)
    else:
        current = consignment_balance(db, vendor_id=vendor_id)
        original = money(current.unreplenished_cogs - current.manual_charges + current.manual_credits)
        prior = money(current.unreplenished_cogs)
    signed = amount if direction == 'INCREASE' else -amount
    resulting = max(money(prior + signed), Decimal('0.00'))
    excess = max(money(-money(prior + signed)), Decimal('0.00'))
    ledger = ConsignmentLedgerEntry(
        vendor_id=vendor_id,
        entry_type=adjustment_type,
        effective_at=_effective_at(effective_date),
        amount=amount,
        report_id=report_id,
        note=clean_reason,
        created_by_principal_id=actor_id,
    )
    db.add(ledger)
    db.flush()
    row = ConsignmentManualAdjustment(
        vendor_id=vendor_id,
        report_id=report_id,
        target_ledger_entry_id=target_ledger_entry_id,
        ledger_entry_id=ledger.id,
        adjustment_type=adjustment_type,
        direction=direction,
        amount=amount,
        effective_date=effective_date,
        reason=clean_reason,
        internal_note=(internal_note or '').strip() or None,
        original_calculated_amount=original,
        prior_adjusted_amount=prior,
        resulting_adjusted_amount=resulting,
        excess_credit_created=excess,
        created_after_finalization=bool(
            report and (report.finalized_at is not None or report.status in {'FINALIZED', 'EMAILED', 'VOIDED'})
        ),
        replacement_for_adjustment_id=replacement_for_adjustment_id,
        created_by_principal_id=actor_id,
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        actor_id=actor_id,
        action='CONSIGNMENT_MANUAL_ADJUSTMENT_RECORDED',
        entity_type='consignment_manual_adjustment',
        entity_id=row.id,
        after={
            'adjustment_id': row.id,
            'vendor_id': vendor_id,
            'report_id': report_id,
            'target_ledger_entry_id': target_ledger_entry_id,
            'ledger_entry_id': ledger.id,
            'type': adjustment_type,
            'direction': direction,
            'amount': str(amount),
            'effective_date': str(effective_date),
            'reason': clean_reason,
            'internal_note': row.internal_note,
            'original_calculated_amount': str(original),
            'prior_adjusted_amount': str(prior),
            'resulting_adjusted_amount': str(resulting),
            'excess_credit_created': str(excess),
            'replacement_for_adjustment_id': replacement_for_adjustment_id,
        },
        ip=ip,
    )
    return row


def reverse_consignment_adjustment(
    db: Session,
    *,
    adjustment_id: int,
    reason: str,
    actor_id: int,
    ip: str | None = None,
) -> ConsignmentManualAdjustment:
    original = db.get(ConsignmentManualAdjustment, adjustment_id)
    if original is None:
        raise LookupError('Adjustment not found.')
    if original.adjustment_type == 'CORRECTION_REVERSAL':
        raise ValueError('A reversal cannot itself be reversed.')
    if db.scalar(select(ConsignmentManualAdjustment).where(
        ConsignmentManualAdjustment.reversed_adjustment_id == original.id
    )) is not None:
        raise ValueError('This adjustment has already been reversed.')
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError('A reversal reason is required.')
    direction = 'DECREASE' if original.direction == 'INCREASE' else 'INCREASE'
    if original.report_id is not None:
        report = db.get(ConsignmentReport, original.report_id)
        original_calculated, prior = _report_adjustment_position(db, report=report)
    else:
        current = consignment_balance(db, vendor_id=int(original.vendor_id))
        original_calculated = money(
            current.unreplenished_cogs - current.manual_charges + current.manual_credits
        )
        prior = money(current.unreplenished_cogs)
    signed = original.amount if direction == 'INCREASE' else -original.amount
    resulting = max(money(prior + signed), Decimal('0.00'))
    excess = max(money(-money(prior + signed)), Decimal('0.00'))
    ledger = ConsignmentLedgerEntry(
        vendor_id=original.vendor_id,
        entry_type='CORRECTION_REVERSAL',
        effective_at=utc_now(),
        amount=original.amount,
        report_id=original.report_id,
        note=clean_reason,
        created_by_principal_id=actor_id,
    )
    db.add(ledger)
    db.flush()
    row = ConsignmentManualAdjustment(
        vendor_id=original.vendor_id,
        report_id=original.report_id,
        target_ledger_entry_id=original.target_ledger_entry_id,
        ledger_entry_id=ledger.id,
        adjustment_type='CORRECTION_REVERSAL',
        direction=direction,
        amount=original.amount,
        effective_date=portal_today(),
        reason=clean_reason,
        internal_note=f'Reverses adjustment #{original.id}.',
        original_calculated_amount=original_calculated,
        prior_adjusted_amount=prior,
        resulting_adjusted_amount=resulting,
        excess_credit_created=excess,
        created_after_finalization=original.created_after_finalization,
        reversed_adjustment_id=original.id,
        created_by_principal_id=actor_id,
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        actor_id=actor_id,
        action='CONSIGNMENT_MANUAL_ADJUSTMENT_REVERSED',
        entity_type='consignment_manual_adjustment',
        entity_id=row.id,
        before={'reversed_adjustment_id': original.id},
        after={
            'adjustment_id': row.id,
            'vendor_id': row.vendor_id,
            'report_id': row.report_id,
            'target_ledger_entry_id': row.target_ledger_entry_id,
            'ledger_entry_id': ledger.id,
            'type': row.adjustment_type,
            'direction': row.direction,
            'amount': str(row.amount),
            'effective_date': str(row.effective_date),
            'reason': row.reason,
            'original_calculated_amount': str(row.original_calculated_amount),
            'prior_adjusted_amount': str(row.prior_adjusted_amount),
            'resulting_adjusted_amount': str(row.resulting_adjusted_amount),
        },
        ip=ip,
    )
    return row


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
    financial_vendor_id = int(db.scalar(select(OrderPayment.vendor_id).where(
        OrderPayment.purchase_order_id == replenishment.purchase_order_id
    )) or replenishment.vendor_id)
    received_delta = money(sum((row['value'] for row in source_rows), Decimal('0')))
    received_ledger = ConsignmentLedgerEntry(
        vendor_id=financial_vendor_id,
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

    balance_before_allocation = consignment_balance(db, vendor_id=financial_vendor_id)
    unapplied_offsets = money(
        balance_before_allocation.cash_adjustments + balance_before_allocation.approved_credits
    )
    reports = db.scalars(
        select(ConsignmentReport)
        .where(
            ConsignmentReport.vendor_id == financial_vendor_id,
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
                    vendor_id=financial_vendor_id,
                    replenishment_id=replenishment.id,
                    cogs_report_id=report.id,
                    amount_applied=amount,
                    created_by_principal_id=actor_id,
                ))
            applied_ledger = ConsignmentLedgerEntry(
                vendor_id=financial_vendor_id,
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
                vendor_id=financial_vendor_id,
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
