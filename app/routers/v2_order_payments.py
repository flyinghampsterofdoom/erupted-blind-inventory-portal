from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.config import settings
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import (
    ConsignmentLedgerEntry,
    ConsignmentManualAdjustment,
    ConsignmentEmailDelivery,
    ConsignmentInventorySnapshot,
    ConsignmentReplenishment,
    ConsignmentReplenishmentReceipt,
    ConsignmentReplenishmentReceiptLine,
    ConsignmentReport,
    ConsignmentReportLine,
    ConsignmentReturnFact,
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    OrderPayment,
    OrderingInventoryRefreshRun,
    PaymentMethod,
    Principal as PrincipalRecord,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStoreAllocation,
    Store,
    Vendor,
    VendorPaymentClassification,
    VendorPaymentSetting,
    VendorVariationAssignment,
    VendorVariationCost,
)
from app.routers.v2 import V2Page, _visible_navigation
from app.security.csrf import verify_csrf
from app.services.v2_order_payments_service import (
    MANUAL_ADJUSTMENT_TYPES,
    MANUAL_CHARGE_TYPES,
    MANUAL_CREDIT_TYPES,
    PAYMENT_CATEGORIES,
    classification_correction_preview,
    confirm_classification_correction,
    confirm_historical_backfill,
    consignment_balance,
    create_consignment_adjustment,
    create_payment_method,
    historical_backfill_preview,
    inventory_snapshot,
    masked_payment_method,
    order_payment_list_rows,
    portal_today,
    purchase_order_scope_labels,
    record_cash_settlement,
    reverse_consignment_adjustment,
    save_vendor_settings,
    set_payment_method_active,
    update_payment_method,
    update_order_payment,
)
from app.services.v2_consignment_facts_service import (
    BLOCKING_STATUSES,
    capture_test_email,
    automatic_report_start_date,
    create_assignment,
    create_cost,
    finalize_report,
    generate_report,
    resolve_return_fact,
    resolve_sale_fact,
    synchronize_square_facts,
    void_report,
)
from app.v2.feature_exposure import require_v2_feature


FEATURE_KEY = 'order_payments_v2'
router = APIRouter(tags=['v2-order-payments'])
feature_access = require_v2_feature(FEATURE_KEY)
capability_access = require_capability('management.admin', Role.ADMIN, Role.MANAGER)


def owner_access(principal: Principal = Depends(capability_access)) -> Principal:
    if principal.role not in {Role.ADMIN, Role.MANAGER}:
        raise HTTPException(status_code=404)
    return principal


def cogs_actions_access() -> None:
    if not settings.v2_consignment_cogs_actions_enabled:
        raise HTTPException(status_code=404)


def _page(label: str, description: str, path: str) -> V2Page:
    return V2Page(
        'order-payments',
        label,
        description,
        permission='management.admin',
        route_path=path,
        badge='Financial controls',
        active_prefix='/v2/order-payments',
    )


def _context(request: Request, principal: Principal, *, page: V2Page, **values) -> dict:
    return {
        'request': request,
        'principal': principal,
        'page': page,
        'navigation': _visible_navigation(request),
        'stores': [],
        'selected_store_ids': [],
        'all_stores_selected': True,
        'store_scope_label': 'Organization-wide',
        'scope_locked': True,
        'scope_caption': 'Financial scope',
        'message': request.query_params.get('message', ''),
        'error': request.query_params.get('error', ''),
        'payment_tabs': (
            ('Order Payments', '/v2/order-payments'),
            ('Payment Methods', '/v2/payment-methods'),
            ('Existing Orders', '/v2/order-payments/backfill'),
            ('Consignment', '/v2/consignment'),
        ),
        'masked_payment_method': masked_payment_method,
        'cogs_actions_enabled': settings.v2_consignment_cogs_actions_enabled,
        'hide_preview_banner': True,
        'business_date': _business_date,
        'business_datetime': _business_datetime,
        'status_label': _status_label,
        'payment_type_label': _payment_type_label,
        'ledger_activity_label': _ledger_activity_label,
        **values,
    }


MONTH_LABELS = ('Jan.', 'Feb.', 'Mar.', 'Apr.', 'May', 'Jun.', 'Jul.', 'Aug.', 'Sep.', 'Oct.', 'Nov.', 'Dec.')
STATUS_LABELS = {
    'DRAFT': 'Draft', 'PREVIEWED': 'Ready to review', 'FINALIZED': 'Finalized', 'EMAILED': 'Email captured',
    'VOIDED': 'Reversed', 'SENT_TO_STORES': 'Sent to stores', 'IN_TRANSIT': 'In transit',
    'RECEIVED_SPLIT_PENDING': 'Receipt review', 'COMPLETED': 'Completed', 'CANCELLED': 'Discarded',
    'UNINITIALIZED': 'Setup required', 'UNCONFIGURED': 'Vendor setup required', 'BLOCKED': 'Needs review',
    'UNPAID': 'Unpaid', 'PAID': 'Paid', 'CONSIGNMENT_ORDERED': 'Waiting for receipt',
    'CONSIGNMENT_PARTIALLY_RECEIVED': 'Partially received', 'CONSIGNMENT_RECEIVED': 'Received',
    'CONSIGNMENT_PARTIALLY_APPLIED': 'Partially applied', 'CONSIGNMENT_APPLIED': 'Applied',
    'PENDING': 'Waiting for receipt', 'PARTIALLY_RECEIVED': 'Partially received', 'RECEIVED': 'Received',
    'PARTIALLY_APPLIED': 'Partially applied', 'APPLIED': 'Applied',
}
PAYMENT_TYPE_LABELS = {
    'WIRE': 'Wire', 'CREDIT_CARD': 'Credit Card', 'DEBIT_CARD': 'Debit Card',
    'TERMS': 'Terms', 'CONSIGNMENT': 'Consignment', 'UNCONFIGURED': 'Vendor setup required',
}
LEDGER_ACTIVITY_LABELS = {
    'COGS_GENERATED': 'COGS report', 'REPLENISHMENT_RECEIVED': 'Inventory received',
    'REPLENISHMENT_APPLIED': 'Inventory replenishment',
    'REPLENISHMENT_CREDIT_CREATED': 'Replenishment credit',
    'REPLENISHMENT_CREDIT_USED': 'Credit applied', 'VENDOR_RETURN': 'Vendor return',
    'INVENTORY_ADJUSTMENT': 'Inventory adjustment', 'CASH_SETTLEMENT': 'Cash settlement',
    'APPROVED_CREDIT': 'Approved credit', 'MANUAL_CORRECTION': 'Manual correction',
    'VOID_REVERSAL': 'Report reversal', 'SHIPPING_CHARGE': 'Shipping', 'TAX_CHARGE': 'Tax',
    'VENDOR_FEE': 'Vendor fee', 'MISCELLANEOUS_CHARGE': 'Miscellaneous charge',
    'VENDOR_CREDIT': 'Vendor credit', 'DAMAGE_CREDIT': 'Damage credit',
    'PROMOTIONAL_CREDIT': 'Promotional credit', 'MISCELLANEOUS_CREDIT': 'Miscellaneous credit',
    'CORRECTION_REVERSAL': 'Adjustment reversal',
}


def _business_date(value: date | datetime | None) -> str:
    if value is None:
        return '—'
    day = value.date() if isinstance(value, datetime) else value
    return f'{MONTH_LABELS[day.month - 1]} {day.day}, {day.year}'


def _business_datetime(value: datetime | None) -> str:
    if value is None:
        return '—'
    local = value.astimezone(ZoneInfo('America/Los_Angeles'))
    return f'{_business_date(local)} · {local.strftime("%-I:%M %p")}'


def _status_label(value: object) -> str:
    raw = str(value.value if hasattr(value, 'value') else value)
    return STATUS_LABELS.get(raw, raw.replace('_', ' ').title())


def _payment_type_label(value: object) -> str:
    raw = str(value.value if hasattr(value, 'value') else value)
    return PAYMENT_TYPE_LABELS.get(raw, raw.replace('_', ' ').title())


def _ledger_activity_label(value: object) -> str:
    return LEDGER_ACTIVITY_LABELS.get(str(value), str(value).replace('_', ' ').title())


def _ledger_activity_rows(db: Session, *, vendor_id: int) -> list[dict]:
    entries = db.scalars(
        select(ConsignmentLedgerEntry)
        .where(ConsignmentLedgerEntry.vendor_id == vendor_id)
        .order_by(ConsignmentLedgerEntry.effective_at, ConsignmentLedgerEntry.id)
    ).all()
    adjustments = db.scalars(
        select(ConsignmentManualAdjustment).where(
            ConsignmentManualAdjustment.vendor_id == vendor_id
        )
    ).all()
    by_ledger = {int(row.ledger_entry_id): row for row in adjustments}
    reversed_ids = {
        int(row.reversed_adjustment_id)
        for row in adjustments
        if row.reversed_adjustment_id is not None
    }
    actor_ids = {int(row.created_by_principal_id) for row in entries}
    actors = {
        int(row.id): row.username
        for row in db.scalars(select(PrincipalRecord).where(PrincipalRecord.id.in_(actor_ids or {-1}))).all()
    }
    increasing_types = {'COGS_GENERATED'} | set(MANUAL_CHARGE_TYPES)
    decreasing_types = {
        'REPLENISHMENT_APPLIED', 'CASH_SETTLEMENT', 'APPROVED_CREDIT', 'VOID_REVERSAL'
    } | set(MANUAL_CREDIT_TYPES)
    running = Decimal('0')
    result = []
    for entry in entries:
        adjustment = by_ledger.get(int(entry.id))
        if adjustment is not None:
            increase = entry.amount if adjustment.direction == 'INCREASE' else Decimal('0')
            decrease = entry.amount if adjustment.direction == 'DECREASE' else Decimal('0')
        else:
            increase = entry.amount if entry.entry_type in increasing_types else Decimal('0')
            decrease = entry.amount if entry.entry_type in decreasing_types else Decimal('0')
        running = max(Decimal('0'), running + Decimal(str(increase)) - Decimal(str(decrease)))
        result.append({
            'entry': entry,
            'adjustment': adjustment,
            'increase': increase,
            'decrease': decrease,
            'running_balance': running,
            'actor': actors.get(int(entry.created_by_principal_id), f'User #{entry.created_by_principal_id}'),
            'is_reversed': bool(adjustment and int(adjustment.id) in reversed_ids),
        })
    return list(reversed(result))


def _back(path: str, *, message: str = '', error: str = '') -> RedirectResponse:
    query = f'?message={quote(message)}' if message else (f'?error={quote(error)}' if error else '')
    return RedirectResponse(f'{path}{query}', status_code=303)


@router.get('/v2/payment-methods')
def payment_methods_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    methods = db.scalars(
        select(PaymentMethod).order_by(PaymentMethod.is_active.desc(), PaymentMethod.display_name, PaymentMethod.id)
    ).all()
    vendors = db.execute(
        select(Vendor, VendorPaymentSetting, PaymentMethod)
        .outerjoin(VendorPaymentSetting, VendorPaymentSetting.vendor_id == Vendor.id)
        .outerjoin(PaymentMethod, PaymentMethod.id == VendorPaymentSetting.default_payment_method_id)
        .where(Vendor.active.is_(True))
        .order_by(Vendor.name)
    ).all()
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/payment_methods.html',
        _context(
            request,
            principal,
            page=_page('Payment Methods', 'Reusable masked methods and vendor financial defaults.', '/v2/payment-methods'),
            methods=methods,
            vendors=vendors,
            active_methods=[row for row in methods if row.is_active],
            categories=PAYMENT_CATEGORIES,
        ),
    )


@router.get('/v2/payment-methods/{method_id}/edit')
def payment_method_edit_page(
    method_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    method = db.get(PaymentMethod, method_id)
    if method is None:
        raise HTTPException(status_code=404)
    in_use = bool(db.scalar(
        select(func.count()).select_from(OrderPayment).where(OrderPayment.payment_method_id == method.id)
    )) or bool(db.scalar(
        select(func.count()).select_from(VendorPaymentClassification).where(
            VendorPaymentClassification.payment_method_id == method.id
        )
    ))
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/payment_method_edit.html',
        _context(
            request,
            principal,
            page=_page('Edit Payment Method', 'Update reusable payment details.', request.url.path),
            method=method,
            categories=PAYMENT_CATEGORIES,
            method_in_use=in_use,
        ),
    )


@router.post('/v2/payment-methods/{method_id}')
async def payment_method_edit_action(
    method_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        raw_days = str(form.get('term_days') or '').strip()
        update_payment_method(
            db,
            method_id=method_id,
            actor_id=principal.id,
            display_name=str(form.get('display_name') or ''),
            category=str(form.get('category') or ''),
            institution=str(form.get('institution') or ''),
            account_nickname=str(form.get('account_nickname') or ''),
            last_four=str(form.get('last_four') or ''),
            term_days=int(raw_days) if raw_days else None,
            notes=str(form.get('notes') or ''),
            ip=get_client_ip(request),
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        return _back(f'/v2/payment-methods/{method_id}/edit', error=str(exc))
    return _back('/v2/payment-methods', message='Payment method updated.')


@router.post('/v2/payment-methods')
async def create_payment_method_action(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        raw_days = str(form.get('term_days') or '').strip()
        create_payment_method(
            db,
            actor_id=principal.id,
            display_name=str(form.get('display_name') or ''),
            category=str(form.get('category') or ''),
            institution=str(form.get('institution') or ''),
            account_nickname=str(form.get('account_nickname') or ''),
            last_four=str(form.get('last_four') or ''),
            term_days=int(raw_days) if raw_days else None,
            notes=str(form.get('notes') or ''),
            ip=get_client_ip(request),
        )
        db.commit()
    except (ValueError, TypeError) as exc:
        db.rollback()
        return _back('/v2/payment-methods', error=str(exc))
    return _back('/v2/payment-methods', message='Payment method created.')


@router.post('/v2/payment-methods/{method_id}/active')
async def payment_method_active_action(
    method_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        set_payment_method_active(
            db,
            method_id=method_id,
            active=str(form.get('active') or '') == '1',
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _back('/v2/payment-methods', message='Payment method status updated.')


@router.get('/v2/vendors/{vendor_id}/payment-settings')
def vendor_settings_page(
    vendor_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404)
    settings = db.get(VendorPaymentSetting, vendor_id)
    classifications = db.scalars(
        select(VendorPaymentClassification)
        .where(VendorPaymentClassification.vendor_id == vendor_id)
        .order_by(VendorPaymentClassification.created_at.desc(), VendorPaymentClassification.id.desc())
    ).all()
    methods = db.scalars(
        select(PaymentMethod)
        .where(PaymentMethod.is_active.is_(True))
        .order_by(PaymentMethod.category, PaymentMethod.display_name)
    ).all()
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/vendor_settings.html',
        _context(
            request,
            principal,
            page=_page(
                f'{vendor.name} Payment Settings',
                'Defaults affect future orders only.',
                f'/v2/vendors/{vendor_id}/payment-settings',
            ),
            vendor=vendor,
            settings=settings,
            classifications=classifications,
            methods=methods,
        ),
    )


@router.post('/v2/vendors/{vendor_id}/payment-settings')
async def vendor_settings_action(
    vendor_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    raw_method = str(form.get('default_payment_method_id') or '').strip()
    raw_effective_date = str(form.get('effective_date') or '').strip()
    try:
        save_vendor_settings(
            db,
            vendor_id=vendor_id,
            default_payment_method_id=int(raw_method) if raw_method else None,
            report_email=str(form.get('report_email') or ''),
            payment_notes=str(form.get('payment_notes') or ''),
            effective_date=date.fromisoformat(raw_effective_date) if raw_effective_date else portal_today(),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return _back(f'/v2/vendors/{vendor_id}/payment-settings', error=str(exc))
    return _back('/v2/payment-methods', message=f'Vendor payment settings saved.')


@router.get('/v2/order-payments')
def order_payments_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    rows = order_payment_list_rows(db)
    methods = db.scalars(
        select(PaymentMethod)
        .where(PaymentMethod.is_active.is_(True), PaymentMethod.category != 'CONSIGNMENT')
        .order_by(PaymentMethod.category, PaymentMethod.display_name)
    ).all()
    unpaid_total = sum(
        (Decimal(str(row['payment'].order_amount)) for row in rows
         if row['payment'] is not None and row['payment'].status == 'UNPAID'),
        Decimal('0'),
    )
    overdue_total = sum(
        (
            Decimal(str(row['payment'].order_amount))
            for row in rows
            if row['payment'] is not None
            and row['payment'].status == 'UNPAID'
            and row['payment'].due_date
            and row['payment'].due_date < portal_today()
        ),
        Decimal('0'),
    )
    unpaid_count = sum(
        1 for row in rows
        if row['payment'] is not None and row['payment'].status == 'UNPAID'
    )
    overdue_count = sum(
        1 for row in rows
        if row['payment'] is not None
        and row['payment'].status == 'UNPAID'
        and row['payment'].due_date
        and row['payment'].due_date < portal_today()
    )
    setup_count = sum(1 for row in rows if row['payment'] is None)
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/index.html',
        _context(
            request,
            principal,
            page=_page(
                'Order Payments',
                'Track vendor payment status, due dates, and consignment orders.',
                '/v2/order-payments',
            ),
            rows=rows,
            methods=methods,
            unpaid_total=unpaid_total,
            unpaid_count=unpaid_count,
            overdue_total=overdue_total,
            overdue_count=overdue_count,
            setup_count=setup_count,
            today=portal_today(),
        ),
    )


def _backfill_page_context(
    request: Request,
    principal: Principal,
    db: Session,
    *,
    preview: dict | None = None,
) -> dict:
    rows = order_payment_list_rows(db)
    grouped: dict[int, dict] = {}
    for row in rows:
        vendor = row['vendor']
        if vendor is None:
            continue
        summary = grouped.setdefault(int(vendor.id), {
            'vendor': vendor,
            'classification': row['classification'],
            'method': row['classification_method'],
            'order_count': 0,
            'uninitialized_count': 0,
            'existing_count': 0,
            'total': Decimal('0'),
            'first_date': None,
            'last_date': None,
        })
        summary['order_count'] += 1
        summary['uninitialized_count'] += int(row['payment'] is None)
        summary['existing_count'] += int(row['payment'] is not None)
        summary['total'] += Decimal(str(row['order_amount']))
        order_date = (row['order'].ordered_at or row['order'].submitted_at or row['order'].created_at).date()
        summary['first_date'] = min(summary['first_date'], order_date) if summary['first_date'] else order_date
        summary['last_date'] = max(summary['last_date'], order_date) if summary['last_date'] else order_date
    methods = db.scalars(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).order_by(
            PaymentMethod.category, PaymentMethod.display_name
        )
    ).all()
    return _context(
        request,
        principal,
        page=_page(
            'Set Up Existing Orders',
            'Apply vendor payment settings to orders that were created before Order Payments was enabled.',
            '/v2/order-payments/backfill',
        ),
        vendor_summaries=sorted(grouped.values(), key=lambda value: value['vendor'].name),
        methods=methods,
        preview=preview,
        today=portal_today(),
    )


@router.get('/v2/order-payments/backfill')
def order_payments_backfill_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/backfill.html',
        _backfill_page_context(request, principal, db),
    )


@router.post('/v2/order-payments/backfill/preview')
async def order_payments_backfill_preview_action(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    selected_ids = [int(value) for value in form.getlist('order_ids') if str(value).isdigit()]
    raw_date = str(form.get('effective_from') or '').strip()
    try:
        preview = historical_backfill_preview(
            db,
            vendor_id=int(str(form.get('vendor_id') or '0')),
            payment_method_id=int(str(form.get('payment_method_id') or '0')),
            scope_type=str(form.get('scope_type') or ''),
            effective_from=date.fromisoformat(raw_date) if raw_date else None,
            selected_order_ids=selected_ids,
        )
    except (ValueError, TypeError) as exc:
        return _back('/v2/order-payments/backfill', error=str(exc))
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/backfill.html',
        _backfill_page_context(request, principal, db, preview=preview),
    )


@router.post('/v2/order-payments/backfill/confirm')
async def order_payments_backfill_confirm_action(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    if str(form.get('confirmed') or '') != '1':
        return _back('/v2/order-payments/backfill', error='Explicit confirmation is required.')
    selected_ids = [int(value) for value in form.getlist('order_ids') if str(value).isdigit()]
    raw_date = str(form.get('effective_from') or '').strip()
    try:
        operation = confirm_historical_backfill(
            db,
            vendor_id=int(str(form.get('vendor_id') or '0')),
            payment_method_id=int(str(form.get('payment_method_id') or '0')),
            scope_type=str(form.get('scope_type') or ''),
            effective_from=date.fromisoformat(raw_date) if raw_date else None,
            selected_order_ids=selected_ids,
            confirmation_note=str(form.get('confirmation_note') or ''),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
    except (ValueError, TypeError) as exc:
        db.rollback()
        return _back('/v2/order-payments/backfill', error=str(exc))
    return _back(
        '/v2/order-payments/backfill',
        message=(
            f'Existing-order setup #{operation.id}: {operation.created_count} set up, '
            f'{operation.skipped_count} already set up, {operation.blocked_count} need review.'
        ),
    )


@router.post('/v2/order-payments/{payment_id}')
async def update_order_payment_action(
    payment_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    raw_method = str(form.get('payment_method_id') or '').strip()
    raw_paid_date = str(form.get('paid_date') or '').strip()
    try:
        paid_date = date.fromisoformat(raw_paid_date) if raw_paid_date else None
        update_order_payment(
            db,
            order_payment_id=payment_id,
            payment_method_id=int(raw_method) if raw_method else None,
            status=str(form.get('status') or ''),
            paid_date=paid_date,
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return _back('/v2/order-payments', error=str(exc))
    return _back('/v2/order-payments', message='Order payment saved.')


@router.get('/v2/order-payments/{payment_id}/classification-correction')
def order_payment_classification_correction_page(
    payment_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    payment = db.get(OrderPayment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404)
    methods = db.scalars(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).order_by(
            PaymentMethod.category, PaymentMethod.display_name
        )
    ).all()
    raw_method = str(request.query_params.get('payment_method_id') or '').strip()
    preview = None
    if raw_method.isdigit():
        try:
            preview = classification_correction_preview(
                db,
                order_payment_id=payment_id,
                payment_method_id=int(raw_method),
            )
        except (LookupError, ValueError) as exc:
            return _back('/v2/order-payments', error=str(exc))
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/classification_correction.html',
        _context(
            request,
            principal,
            page=_page(
                f'Order #{payment.purchase_order_id} Classification Correction',
                'Owner-confirmed correction with downstream impact checks.',
                f'/v2/order-payments/{payment_id}/classification-correction',
            ),
            payment=payment,
            methods=methods,
            preview=preview,
        ),
    )


@router.post('/v2/order-payments/{payment_id}/classification-correction')
async def order_payment_classification_correction_action(
    payment_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    if str(form.get('confirmed') or '') != '1':
        return _back(
            f'/v2/order-payments/{payment_id}/classification-correction',
            error='Explicit confirmation is required.',
        )
    try:
        payment = confirm_classification_correction(
            db,
            order_payment_id=payment_id,
            payment_method_id=int(str(form.get('payment_method_id') or '0')),
            reason=str(form.get('reason') or ''),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        return _back(
            f'/v2/order-payments/{payment_id}/classification-correction',
            error=str(exc),
        )
    return _back(
        f'/v2/order-payments/{payment.purchase_order_id}',
        message='Classification correction recorded.',
    )


@router.get('/v2/order-payments/{order_id}')
def order_payment_detail_page(
    order_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    source_order = db.get(PurchaseOrder, order_id)
    if source_order is None or source_order.status.value not in (
        'IN_TRANSIT', 'RECEIVED_SPLIT_PENDING', 'SENT_TO_STORES', 'COMPLETED'
    ):
        raise HTTPException(status_code=404)
    row = db.execute(
        select(OrderPayment, PurchaseOrder, Vendor, PaymentMethod)
        .join(PurchaseOrder, PurchaseOrder.id == OrderPayment.purchase_order_id)
        .join(Vendor, Vendor.id == OrderPayment.vendor_id)
        .outerjoin(PaymentMethod, PaymentMethod.id == OrderPayment.payment_method_id)
        .where(PurchaseOrder.id == order_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404)
    lines = db.scalars(
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.purchase_order_id == order_id, PurchaseOrderLine.removed.is_(False))
        .order_by(PurchaseOrderLine.item_name, PurchaseOrderLine.variation_name)
    ).all()
    line_ids = [int(line.id) for line in lines]
    allocation_rows = db.execute(
        select(PurchaseOrderStoreAllocation, Store)
        .join(Store, Store.id == PurchaseOrderStoreAllocation.store_id)
        .where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids or [-1]))
        .order_by(PurchaseOrderStoreAllocation.purchase_order_line_id, Store.name)
    ).all()
    allocations_by_line: dict[int, list] = {}
    for allocation_row in allocation_rows:
        allocations_by_line.setdefault(
            int(allocation_row.PurchaseOrderStoreAllocation.purchase_order_line_id), []
        ).append(allocation_row)
    receipt_lines = db.execute(
        select(ConsignmentReplenishmentReceiptLine, ConsignmentReplenishmentReceipt)
        .join(
            ConsignmentReplenishmentReceipt,
            ConsignmentReplenishmentReceipt.id == ConsignmentReplenishmentReceiptLine.receipt_id,
        )
        .where(ConsignmentReplenishmentReceipt.purchase_order_id == order_id)
        .order_by(ConsignmentReplenishmentReceipt.created_at, ConsignmentReplenishmentReceiptLine.id)
    ).all()
    replenishment = db.scalar(select(ConsignmentReplenishment).where(
        ConsignmentReplenishment.purchase_order_id == order_id))
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/detail.html',
        _context(
            request,
            principal,
            page=_page(
                f'Order #{order_id}',
                'Read-only commercial snapshot from saved purchase-order lines.',
                f'/v2/order-payments/{order_id}',
            ),
            row=row,
            lines=lines,
            allocations_by_line=allocations_by_line,
            receipt_lines=receipt_lines,
            replenishment=replenishment,
            order_scope=purchase_order_scope_labels(db, order_ids=[order_id]).get(
                order_id, 'Organization-wide'
            ),
        ),
    )


@router.get('/v2/consignment')
def consignment_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    vendor_rows = db.execute(
        select(Vendor, VendorPaymentSetting)
        .join(VendorPaymentSetting, VendorPaymentSetting.vendor_id == Vendor.id)
        .join(PaymentMethod, PaymentMethod.id == VendorPaymentSetting.default_payment_method_id)
        .where(Vendor.active.is_(True), PaymentMethod.category == 'CONSIGNMENT')
        .order_by(Vendor.name)
    ).all()
    summaries = []
    for vendor, settings in vendor_rows:
        replenishments = db.scalars(
            select(ConsignmentReplenishment).where(ConsignmentReplenishment.vendor_id == vendor.id)
        ).all()
        balance = consignment_balance(db, vendor_id=vendor.id)
        qty, value, _detail, warnings = inventory_snapshot(db, vendor_id=vendor.id)
        last_report = db.scalar(
            select(ConsignmentReport)
            .where(
                ConsignmentReport.vendor_id == vendor.id,
                ConsignmentReport.status.in_(('FINALIZED', 'EMAILED')),
            )
            .order_by(ConsignmentReport.end_at.desc(), ConsignmentReport.id.desc())
        )
        pending = sum(
            (
                Decimal(str(row.ordered_cost_value)) - Decimal(str(row.received_cost_value))
                for row in replenishments
                if row.status in {'PENDING', 'PARTIALLY_RECEIVED'}
            ),
            Decimal('0'),
        )
        summaries.append(
            {
                'vendor': vendor,
                'settings': settings,
                'balance': balance,
                'inventory_quantity': qty,
                'inventory_value': value,
                'last_report': last_report,
                'pending_replenishment': max(pending, Decimal('0')),
                'warnings': warnings,
            }
        )
    latest_inventory_refresh = db.scalar(
        select(func.max(OrderingInventoryRefreshRun.completed_at)).where(
            OrderingInventoryRefreshRun.result.in_(('COMPLETE', 'PARTIAL'))
        )
    )
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/consignment.html',
        _context(
            request,
            principal,
            page=_page(
                'Consignment',
                'Track vendor-owned inventory, replenishment credit, and rolling settlement balances.',
                '/v2/consignment',
            ),
            summaries=summaries,
            latest_inventory_refresh=latest_inventory_refresh,
            accounting_blocker='Report creation is temporarily unavailable while sales data is being verified.'
        ),
    )


def _portal_at(raw: object, *, end_of_day: bool = False) -> datetime:
    try:
        day = date.fromisoformat(str(raw or '').strip())
    except ValueError as exc:
        raise ValueError('Enter a valid date.') from exc
    local = datetime.combine(day + (timedelta(days=1) if end_of_day else timedelta()), time.min,
                             tzinfo=ZoneInfo('America/Los_Angeles'))
    return local.astimezone(timezone.utc)


@router.get('/v2/consignment/attribution')
def consignment_attribution_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    status = str(request.query_params.get('status') or '').strip().upper()
    sales_query = select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.attribution_status.in_(BLOCKING_STATUSES)
    )
    returns_query = select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.attribution_status.in_(BLOCKING_STATUSES)
    )
    if status:
        sales_query = sales_query.where(ConsignmentSaleFact.attribution_status == status)
        returns_query = returns_query.where(ConsignmentReturnFact.attribution_status == status)
    sales = db.scalars(sales_query.order_by(ConsignmentSaleFact.transacted_at.desc()).limit(250)).all()
    returns = db.scalars(returns_query.order_by(ConsignmentReturnFact.returned_at.desc()).limit(250)).all()
    vendors = db.scalars(select(Vendor).where(Vendor.active.is_(True)).order_by(Vendor.name)).all()
    candidate_sales = db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.attribution_status == 'ATTRIBUTED'
    ).order_by(ConsignmentSaleFact.transacted_at.desc()).limit(500)).all()
    assignments = db.scalars(select(VendorVariationAssignment).order_by(
        VendorVariationAssignment.square_variation_id, VendorVariationAssignment.effective_start_at.desc())).all()
    costs = db.scalars(select(VendorVariationCost).order_by(
        VendorVariationCost.square_variation_id, VendorVariationCost.effective_start_at.desc())).all()
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/attribution.html',
        _context(request, principal, page=_page('Consignment Attribution',
            'Resolve immutable sale and return facts before report finalization.', '/v2/consignment/attribution'),
            sales=sales, returns=returns, vendors=vendors, candidate_sales=candidate_sales,
            assignments=assignments, costs=costs, sync_state=db.get(ConsignmentSalesSyncState, 1),
            selected_status=status),
    )


@router.post('/v2/consignment/attribution/sync')
async def consignment_sync_action(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        result = synchronize_square_facts(db, start_at=_portal_at(form.get('start_date')),
            end_at=_portal_at(form.get('end_date'), end_of_day=True), actor_id=principal.id)
        db.commit()
        return _back('/v2/consignment/attribution', message=(
            f'Synchronized {result.orders} orders; created {result.sales_created} sales and '
            f'{result.returns_created} returns; {result.unresolved} unresolved.'))
    except RuntimeError as exc:
        db.commit(); return _back('/v2/consignment/attribution', error=str(exc))
    except ValueError as exc:
        db.rollback(); return _back('/v2/consignment/attribution', error=str(exc))


@router.post('/v2/consignment/attribution/assignments')
async def consignment_assignment_action(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        create_assignment(db, vendor_id=int(str(form.get('vendor_id') or '')),
            variation_id=str(form.get('variation_id') or ''),
            is_consignment=str(form.get('is_consignment') or '') == '1',
            start_at=_portal_at(form.get('start_date')),
            end_at=_portal_at(form.get('end_date'), end_of_day=True) if form.get('end_date') else None,
            actor_id=principal.id, notes=str(form.get('reason') or ''), ip=get_client_ip(request))
        db.commit(); return _back('/v2/consignment/attribution', message='Effective-dated assignment created.')
    except (ValueError, TypeError) as exc:
        db.rollback(); return _back('/v2/consignment/attribution', error=str(exc))


@router.post('/v2/consignment/attribution/costs')
async def consignment_cost_action(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        create_cost(db, vendor_id=int(str(form.get('vendor_id') or '')),
            variation_id=str(form.get('variation_id') or ''), unit_cost=Decimal(str(form.get('unit_cost') or '')),
            start_at=_portal_at(form.get('start_date')),
            end_at=_portal_at(form.get('end_date'), end_of_day=True) if form.get('end_date') else None,
            actor_id=principal.id, notes=str(form.get('reason') or ''), ip=get_client_ip(request))
        db.commit(); return _back('/v2/consignment/attribution', message='Effective-dated cost created.')
    except (ValueError, TypeError, InvalidOperation) as exc:
        db.rollback(); return _back('/v2/consignment/attribution', error=str(exc))


@router.post('/v2/consignment/attribution/sales/{fact_id}')
async def consignment_sale_resolution_action(
    fact_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form(); raw_vendor = str(form.get('vendor_id') or '').strip()
    try:
        resolve_sale_fact(db, fact_id=fact_id, vendor_id=int(raw_vendor) if raw_vendor else None,
            unit_cost=Decimal(str(form.get('unit_cost'))) if form.get('unit_cost') else None,
            disposition=str(form.get('disposition') or 'ATTRIBUTED'), reason=str(form.get('reason') or ''),
            actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return _back('/v2/consignment/attribution', message='Sale attribution saved.')
    except LookupError as exc:
        db.rollback(); raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, InvalidOperation) as exc:
        db.rollback(); return _back('/v2/consignment/attribution', error=str(exc))


@router.post('/v2/consignment/attribution/returns/{fact_id}')
async def consignment_return_resolution_action(
    fact_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    raw_sale_id = str(form.get('sale_fact_id') or '').strip()
    try:
        resolve_return_fact(db, fact_id=fact_id, sale_fact_id=int(raw_sale_id) if raw_sale_id else None,
            disposition=str(form.get('disposition') or 'ATTRIBUTED'),
            reason=str(form.get('reason') or ''), actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return _back('/v2/consignment/attribution', message='Return linked to immutable sale.')
    except LookupError as exc:
        db.rollback(); raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        db.rollback(); return _back('/v2/consignment/attribution', error=str(exc))


@router.get('/v2/consignment/{vendor_id}')
def consignment_vendor_page(
    vendor_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    vendor = db.get(Vendor, vendor_id)
    settings = db.get(VendorPaymentSetting, vendor_id)
    method = db.get(PaymentMethod, settings.default_payment_method_id) if settings else None
    if vendor is None or method is None or method.category != 'CONSIGNMENT':
        raise HTTPException(status_code=404)
    qty, value, inventory, warnings = inventory_snapshot(db, vendor_id=vendor_id)
    ledger = db.scalars(
        select(ConsignmentLedgerEntry)
        .where(ConsignmentLedgerEntry.vendor_id == vendor_id)
        .order_by(ConsignmentLedgerEntry.effective_at.desc(), ConsignmentLedgerEntry.id.desc())
    ).all()
    reports = db.scalars(
        select(ConsignmentReport)
        .where(ConsignmentReport.vendor_id == vendor_id)
        .order_by(ConsignmentReport.start_at.desc(), ConsignmentReport.id.desc())
    ).all()
    adjustments = db.scalars(
        select(ConsignmentManualAdjustment)
        .where(ConsignmentManualAdjustment.vendor_id == vendor_id)
        .order_by(ConsignmentManualAdjustment.created_at.desc(), ConsignmentManualAdjustment.id.desc())
    ).all()
    adjustment_actor_ids = {int(row.created_by_principal_id) for row in adjustments}
    adjustment_actors = {
        int(row.id): row.username
        for row in db.scalars(
            select(PrincipalRecord).where(PrincipalRecord.id.in_(adjustment_actor_ids or {-1}))
        ).all()
    }
    replenishments = db.scalars(
        select(ConsignmentReplenishment)
        .where(ConsignmentReplenishment.vendor_id == vendor_id)
        .order_by(ConsignmentReplenishment.created_at.desc())
    ).all()
    methods = db.scalars(
        select(PaymentMethod).where(
            PaymentMethod.is_active.is_(True), PaymentMethod.category != 'CONSIGNMENT'
        )
    ).all()
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/consignment_vendor.html',
        _context(
            request,
            principal,
            page=_page(
                vendor.name,
                'Inventory, replenishment, allocations, adjustments, and reconstructable ledger.',
                f'/v2/consignment/{vendor_id}',
            ),
            vendor=vendor,
            settings=settings,
            balance=consignment_balance(db, vendor_id=vendor_id),
            inventory_quantity=qty,
            inventory_value=value,
            inventory=inventory,
            warnings=warnings,
            ledger=ledger,
            ledger_rows=_ledger_activity_rows(db, vendor_id=vendor_id),
            adjustments=adjustments,
            adjustment_actors=adjustment_actors,
            replenishments=replenishments,
            methods=methods,
            reports=reports,
            automatic_start_date=automatic_report_start_date(db, vendor_id=vendor_id),
            today=portal_today(),
        ),
    )


@router.get('/v2/consignment/{vendor_id}/adjustments/new')
def consignment_adjustment_page(
    vendor_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404)
    reports = db.scalars(
        select(ConsignmentReport)
        .where(ConsignmentReport.vendor_id == vendor_id)
        .order_by(ConsignmentReport.start_at.desc(), ConsignmentReport.id.desc())
    ).all()
    ledger = db.scalars(
        select(ConsignmentLedgerEntry)
        .where(ConsignmentLedgerEntry.vendor_id == vendor_id)
        .order_by(ConsignmentLedgerEntry.effective_at.desc(), ConsignmentLedgerEntry.id.desc())
    ).all()
    replacements = db.scalars(
        select(ConsignmentManualAdjustment)
        .where(
            ConsignmentManualAdjustment.vendor_id == vendor_id,
            ConsignmentManualAdjustment.adjustment_type != 'CORRECTION_REVERSAL',
            ConsignmentManualAdjustment.id.in_(
                select(ConsignmentManualAdjustment.reversed_adjustment_id).where(
                    ConsignmentManualAdjustment.reversed_adjustment_id.is_not(None)
                )
            ),
        )
        .order_by(ConsignmentManualAdjustment.created_at.desc())
    ).all()
    selected_target = str(request.query_params.get('target') or '')
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/adjustment_form.html',
        _context(
            request,
            principal,
            page=_page('Add Adjustment', f'Record a charge or credit for {vendor.name}.', request.url.path),
            vendor=vendor,
            reports=reports,
            ledger=ledger,
            replacements=replacements,
            selected_target=selected_target,
            charge_types=MANUAL_CHARGE_TYPES,
            credit_types=MANUAL_CREDIT_TYPES,
            today=portal_today(),
        ),
    )


@router.post('/v2/consignment/{vendor_id}/adjustments')
async def create_consignment_adjustment_action(
    vendor_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    if str(form.get('confirmed') or '') != '1':
        return _back(f'/v2/consignment/{vendor_id}/adjustments/new', error='Confirmation is required.')
    target = str(form.get('target') or '')
    kind, _, raw_id = target.partition(':')
    try:
        target_id = int(raw_id)
        row = create_consignment_adjustment(
            db,
            vendor_id=vendor_id,
            report_id=target_id if kind == 'report' else None,
            target_ledger_entry_id=target_id if kind == 'ledger' else None,
            adjustment_type=str(form.get('adjustment_type') or ''),
            direction=str(form.get('direction') or ''),
            amount=Decimal(str(form.get('amount') or '')),
            effective_date=date.fromisoformat(str(form.get('effective_date') or '')),
            reason=str(form.get('reason') or ''),
            internal_note=str(form.get('internal_note') or ''),
            replacement_for_adjustment_id=(
                int(str(form.get('replacement_for_adjustment_id')))
                if str(form.get('replacement_for_adjustment_id') or '').isdigit()
                else None
            ),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, InvalidOperation, TypeError) as exc:
        db.rollback()
        return _back(f'/v2/consignment/{vendor_id}/adjustments/new', error=str(exc))
    destination = (
        f'/v2/consignment/{vendor_id}/reports/{row.report_id}'
        if row.report_id is not None
        else f'/v2/consignment/{vendor_id}'
    )
    return _back(destination, message='Adjustment recorded in the ledger.')


@router.post('/v2/consignment/{vendor_id}/adjustments/{adjustment_id}/reverse')
async def reverse_consignment_adjustment_action(
    vendor_id: int,
    adjustment_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    original = db.get(ConsignmentManualAdjustment, adjustment_id)
    if original is None or int(original.vendor_id) != vendor_id:
        raise HTTPException(status_code=404)
    form = await request.form()
    if str(form.get('confirmed') or '') != '1':
        return _back(f'/v2/consignment/{vendor_id}', error='Reversal confirmation is required.')
    try:
        reverse_consignment_adjustment(
            db,
            adjustment_id=adjustment_id,
            reason=str(form.get('reason') or ''),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
    except (LookupError, ValueError) as exc:
        db.rollback()
        return _back(f'/v2/consignment/{vendor_id}', error=str(exc))
    return _back(f'/v2/consignment/{vendor_id}', message='Adjustment reversed with a new ledger entry.')


@router.post('/v2/consignment/{vendor_id}/cash-settlements')
async def consignment_cash_settlement_action(
    vendor_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    if str(form.get('owner_confirmation') or '') != 'confirmed':
        return _back(f'/v2/consignment/{vendor_id}', error='Owner confirmation is required.')
    try:
        record_cash_settlement(
            db,
            vendor_id=vendor_id,
            amount=Decimal(str(form.get('amount') or '')),
            effective_date=date.fromisoformat(str(form.get('effective_date') or '')),
            payment_method_id=int(str(form.get('payment_method_id') or '')),
            note=str(form.get('note') or ''),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
    except (ValueError, InvalidOperation) as exc:
        db.rollback()
        return _back(f'/v2/consignment/{vendor_id}', error=str(exc))
    return _back(f'/v2/consignment/{vendor_id}', message='Exceptional cash settlement recorded.')


@router.post('/v2/consignment/{vendor_id}/reports')
async def generate_consignment_report_action(
    vendor_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        raw_start = str(form.get('start_date') or '').strip()
        start_date = date.fromisoformat(raw_start) if raw_start else automatic_report_start_date(db, vendor_id=vendor_id)
        if start_date is None:
            raise ValueError('Choose the initial start date for this vendor’s first report.')
        report = generate_report(db, vendor_id=vendor_id,
            start_date=start_date,
            end_date=date.fromisoformat(str(form.get('end_date') or '')),
            actor_id=principal.id, ip=get_client_ip(request))
        db.commit()
        return RedirectResponse(f'/v2/consignment/{vendor_id}/reports/{report.id}', status_code=303)
    except LookupError as exc:
        db.rollback(); raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/consignment/{vendor_id}', error=str(exc))


@router.get('/v2/consignment/{vendor_id}/reports/{report_id}')
def consignment_report_preview_page(
    vendor_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    report = db.get(ConsignmentReport, report_id); vendor = db.get(Vendor, vendor_id)
    if report is None or vendor is None or report.vendor_id != vendor_id:
        raise HTTPException(status_code=404)
    lines = db.scalars(select(ConsignmentReportLine).where(
        ConsignmentReportLine.report_id == report.id).order_by(ConsignmentReportLine.product_name_snapshot)).all()
    inventory = db.scalars(select(ConsignmentInventorySnapshot).where(
        ConsignmentInventorySnapshot.report_id == report.id).order_by(
        ConsignmentInventorySnapshot.product_name_snapshot, ConsignmentInventorySnapshot.store_id)).all()
    deliveries = db.scalars(select(ConsignmentEmailDelivery).where(
        ConsignmentEmailDelivery.report_id == report.id).order_by(ConsignmentEmailDelivery.created_at.desc())).all()
    settings_row = db.get(VendorPaymentSetting, vendor_id)
    integrity = report.data_integrity_blockers or {}
    blocked_sales = db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.id.in_(integrity.get('unresolved_sale_ids') or [-1]))).all()
    blocked_returns = db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.id.in_(integrity.get('unresolved_return_ids') or [-1]))).all()
    adjustments = db.scalars(
        select(ConsignmentManualAdjustment)
        .where(ConsignmentManualAdjustment.report_id == report.id)
        .order_by(ConsignmentManualAdjustment.created_at.desc(), ConsignmentManualAdjustment.id.desc())
    ).all()
    actor_ids = {int(row.created_by_principal_id) for row in adjustments}
    adjustment_actors = {
        int(row.id): row.username
        for row in db.scalars(select(PrincipalRecord).where(PrincipalRecord.id.in_(actor_ids or {-1}))).all()
    }
    charge_total = sum(
        (Decimal(str(row.amount)) for row in adjustments if row.direction == 'INCREASE'), Decimal('0')
    )
    credit_total = sum(
        (Decimal(str(row.amount)) for row in adjustments if row.direction == 'DECREASE'), Decimal('0')
    )
    adjusted_total = max(Decimal('0'), Decimal(str(report.total_cogs)) + charge_total - credit_total)
    return request.app.state.templates.TemplateResponse('v2/order_payments/report_preview.html',
        _context(request, principal, page=_page(f'Report {report.report_number}',
            'Review sales cost, adjustments, and settlement activity for this period.',
            f'/v2/consignment/{vendor_id}/reports/{report_id}'), report=report, vendor=vendor,
            lines=lines, inventory=inventory, deliveries=deliveries, settings=settings_row,
            blocked_sales=blocked_sales, blocked_returns=blocked_returns,
            adjustments=adjustments, adjustment_actors=adjustment_actors,
            adjustment_charges=charge_total, adjustment_credits=credit_total,
            adjusted_total=adjusted_total,
            period_end_date=(report.end_at.astimezone(ZoneInfo('America/Los_Angeles')) - timedelta(microseconds=1)).date(),
            inventory_warnings=integrity.get('inventory_warnings') or [],
            blockers=integrity.get('codes') or []))


@router.post('/v2/consignment/{vendor_id}/reports/{report_id}/finalize')
def finalize_consignment_report_action(
    vendor_id: int, report_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    report = db.get(ConsignmentReport, report_id)
    if report is None or report.vendor_id != vendor_id: raise HTTPException(status_code=404)
    try:
        finalize_report(db, report_id=report_id, actor_id=principal.id, ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/consignment/{vendor_id}/reports/{report_id}', message='Report finalized and ledgered.')
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/consignment/{vendor_id}/reports/{report_id}', error=str(exc))


@router.post('/v2/consignment/{vendor_id}/reports/{report_id}/void')
async def void_consignment_report_action(
    vendor_id: int, report_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    report = db.get(ConsignmentReport, report_id)
    if report is None or report.vendor_id != vendor_id: raise HTTPException(status_code=404)
    form = await request.form()
    try:
        void_report(db, report_id=report_id, reason=str(form.get('reason') or ''),
                    actor_id=principal.id, ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/consignment/{vendor_id}/reports/{report_id}', message='Report voided with reversal ledger entry.')
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/consignment/{vendor_id}/reports/{report_id}', error=str(exc))


@router.post('/v2/consignment/{vendor_id}/reports/{report_id}/test-email')
async def capture_consignment_test_email_action(
    vendor_id: int, report_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _cogs: None = Depends(cogs_actions_access),
    _csrf: None = Depends(verify_csrf),
):
    report = db.get(ConsignmentReport, report_id)
    if report is None or report.vendor_id != vendor_id: raise HTTPException(status_code=404)
    try:
        capture_test_email(db, report_id=report_id, actor_id=principal.id,
                           ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/consignment/{vendor_id}/reports/{report_id}', message='Test email captured locally; nothing was sent.')
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/consignment/{vendor_id}/reports/{report_id}', error=str(exc))
    order_payment_list_rows,
