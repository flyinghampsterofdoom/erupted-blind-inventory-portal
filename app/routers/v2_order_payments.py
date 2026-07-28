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
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStoreAllocation,
    Store,
    Vendor,
    VendorPaymentSetting,
    VendorVariationAssignment,
    VendorVariationCost,
)
from app.routers.v2 import V2Page, _visible_navigation
from app.security.csrf import verify_csrf
from app.services.v2_order_payments_service import (
    PAYMENT_CATEGORIES,
    backfill_placed_order_payments,
    consignment_balance,
    create_payment_method,
    ensure_order_payment,
    inventory_snapshot,
    masked_payment_method,
    portal_today,
    purchase_order_scope_labels,
    record_cash_settlement,
    save_vendor_settings,
    set_payment_method_active,
    sync_consignment_replenishment,
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
        badge='Owner Preview · Financial controls',
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
            ('Consignment Report', '/v2/consignment'),
        ),
        'masked_payment_method': masked_payment_method,
        'cogs_actions_enabled': settings.v2_consignment_cogs_actions_enabled,
        **values,
    }


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
    try:
        save_vendor_settings(
            db,
            vendor_id=vendor_id,
            default_payment_method_id=int(raw_method) if raw_method else None,
            report_email=str(form.get('report_email') or ''),
            payment_notes=str(form.get('payment_notes') or ''),
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
    created = backfill_placed_order_payments(db, actor_id=principal.id)
    replenishments = db.scalars(select(ConsignmentReplenishment).order_by(
        ConsignmentReplenishment.purchase_order_id)).all()
    for replenishment in replenishments:
        sync_consignment_replenishment(db, replenishment=replenishment, actor_id=principal.id)
    db.commit()
    rows = db.execute(
        select(OrderPayment, PurchaseOrder, Vendor, PaymentMethod, ConsignmentReplenishment)
        .join(PurchaseOrder, PurchaseOrder.id == OrderPayment.purchase_order_id)
        .join(Vendor, Vendor.id == OrderPayment.vendor_id)
        .outerjoin(PaymentMethod, PaymentMethod.id == OrderPayment.payment_method_id)
        .outerjoin(
            ConsignmentReplenishment,
            ConsignmentReplenishment.purchase_order_id == PurchaseOrder.id,
        )
        .order_by(
            (OrderPayment.status == 'UNPAID').desc(),
            OrderPayment.due_date.asc().nullslast(),
            PurchaseOrder.ordered_at.desc().nullslast(),
            PurchaseOrder.id.desc(),
        )
    ).all()
    order_scopes = purchase_order_scope_labels(
        db, order_ids=[int(row.PurchaseOrder.id) for row in rows]
    )
    methods = db.scalars(
        select(PaymentMethod)
        .where(PaymentMethod.is_active.is_(True), PaymentMethod.category != 'CONSIGNMENT')
        .order_by(PaymentMethod.category, PaymentMethod.display_name)
    ).all()
    unpaid_total = sum(
        (Decimal(str(row.OrderPayment.order_amount)) for row in rows if row.OrderPayment.status == 'UNPAID'),
        Decimal('0'),
    )
    overdue_total = sum(
        (
            Decimal(str(row.OrderPayment.order_amount))
            for row in rows
            if row.OrderPayment.status == 'UNPAID'
            and row.OrderPayment.due_date
            and row.OrderPayment.due_date < portal_today()
        ),
        Decimal('0'),
    )
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/index.html',
        _context(
            request,
            principal,
            page=_page(
                'Order Payments',
                'Captured invoice payment state and consignment replenishment visibility.',
                '/v2/order-payments',
            ),
            rows=rows,
            methods=methods,
            unpaid_total=unpaid_total,
            overdue_total=overdue_total,
            today=portal_today(),
            backfilled_count=created,
            order_scopes=order_scopes,
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
    ensure_order_payment(db, order=source_order, actor_id=principal.id)
    replenishment_to_sync = db.scalar(select(ConsignmentReplenishment).where(
        ConsignmentReplenishment.purchase_order_id == order_id
    ))
    if replenishment_to_sync is not None:
        sync_consignment_replenishment(
            db, replenishment=replenishment_to_sync, actor_id=principal.id
        )
    db.commit()
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
        for replenishment in replenishments:
            sync_consignment_replenishment(db, replenishment=replenishment, actor_id=principal.id)
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
    db.commit()
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
                'Consignment Report',
                'Rolling inventory settlement; no paid/unpaid A/P treatment.',
                '/v2/consignment',
            ),
            summaries=summaries,
            latest_inventory_refresh=latest_inventory_refresh,
            accounting_blocker=(
                'Finalization now uses immutable local facts. Synchronize and resolve every blocking attribution '
                'record before generating a finalizable preview.'
            ),
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
            replenishments=replenishments,
            methods=methods,
            reports=reports,
            automatic_start_date=automatic_report_start_date(db, vendor_id=vendor_id),
            today=portal_today(),
        ),
    )


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
    return request.app.state.templates.TemplateResponse('v2/order_payments/report_preview.html',
        _context(request, principal, page=_page(f'Report {report.report_number}',
            'Reproducible preview from immutable local sales, returns, costs, and inventory snapshots.',
            f'/v2/consignment/{vendor_id}/reports/{report_id}'), report=report, vendor=vendor,
            lines=lines, inventory=inventory, deliveries=deliveries, settings=settings_row,
            blocked_sales=blocked_sales, blocked_returns=blocked_returns,
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
