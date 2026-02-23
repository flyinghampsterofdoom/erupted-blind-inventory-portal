from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from decimal import InvalidOperation
from io import StringIO
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, Role, is_admin_role, require_role
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import Campaign, CountGroup, CountSession, Store
from app.security.csrf import verify_csrf
from app.sync_square_campaigns import sync_campaigns
from app.services.audit_service import log_audit
from app.services.change_box_count_service import ROLL_SIZES_BY_CODE
from app.services.change_box_count_service import get_count_detail, list_counts_for_audit
from app.services.change_form_service import (
    DENOMS,
    get_change_form_detail,
    get_inventory_state,
    list_change_forms,
    submit_inventory_audit,
)
from app.services.customer_request_service import (
    add_item as add_customer_request_item,
    list_items_for_management as list_customer_request_items_for_management,
    list_submissions as list_customer_request_submissions,
    set_item_count as set_customer_request_item_count,
)
from app.services.daily_chore_service import get_sheet_detail_for_audit, list_sheets_for_audit
from app.services.exchange_return_form_service import get_form_detail as get_exchange_return_form_detail
from app.services.exchange_return_form_service import list_forms as list_exchange_return_forms
from app.services.non_sellable_stock_take_service import (
    add_item as add_non_sellable_item,
    deactivate_item as deactivate_non_sellable_item,
    get_stock_take_detail,
    list_items as list_non_sellable_items,
    list_stock_takes_for_audit,
    unlock_stock_take,
)
from app.services.opening_checklist_service import get_submission_detail, list_submissions
from app.services.session_service import (
    create_management_user,
    create_count_group,
    create_forced_count,
    deactivate_count_group,
    get_management_variance_lines,
    group_management_data,
    list_management_users,
    list_store_login_rows,
    list_stores_with_rotation,
    purge_count_sessions,
    renumber_count_group_positions,
    reset_management_user_password,
    reset_manager_password,
    set_management_user_active,
    set_store_next_group,
    unlock_session,
    update_count_group,
    upsert_store_login_credentials,
)

router = APIRouter(prefix='/management', tags=['management'])
management_access = require_role(Role.ADMIN, Role.MANAGER, Role.LEAD)
admin_access = require_role(Role.ADMIN, Role.MANAGER)


@router.get('/home')
def home(
    request: Request,
    principal: Principal = Depends(management_access),
):
    cards = [
        {'href': '/management/groups', 'label': 'Manage Count Groups', 'requires_admin': True},
        {'href': '/management/sessions', 'label': 'Current / Previous Counts', 'requires_admin': False},
        {'href': '/management/users', 'label': 'Users', 'requires_admin': True},
        {'href': '/management/ordering-tool', 'label': 'Ordering Tool', 'requires_admin': True},
        {'href': '/management/daily-chore-lists', 'label': 'Daily Chore Sheet Audit', 'requires_admin': False},
        {'href': '/management/opening-checklists', 'label': 'Store Opening Checklist Audit', 'requires_admin': False},
        {'href': '/management/change-box-count', 'label': 'Change Box Count', 'requires_admin': False},
        {'href': '/management/change-forms', 'label': 'Change Forms', 'requires_admin': False},
        {'href': '/management/exchange-return-forms', 'label': 'Exchange/Return Forms', 'requires_admin': False},
        {'href': '/management/change-box-audit', 'label': 'Change Box Audit', 'requires_admin': True},
        {'href': '/management/non-sellable-stock-take', 'label': 'Non-sellable Stock Take', 'requires_admin': False},
        {'href': '/management/customer-requests', 'label': 'Customer Requests', 'requires_admin': False},
        {'href': '/management/audit-queue', 'label': 'Audit Queue', 'requires_admin': False},
        {'href': '/management/reports', 'label': 'Reports & Exports', 'requires_admin': False},
    ]
    visible_cards = [card for card in cards if is_admin_role(principal.role) or not card['requires_admin']]
    return request.app.state.templates.TemplateResponse(
        'management_home.html',
        {
            'request': request,
            'principal': principal,
            'cards': visible_cards,
        },
    )


def _render_placeholder(request: Request, title: str) -> object:
    return request.app.state.templates.TemplateResponse(
        'management_placeholder.html',
        {
            'request': request,
            'title': title,
        },
    )


@router.get('/ordering-tool')
def ordering_tool_page(request: Request, _: Principal = Depends(admin_access)):
    return _render_placeholder(request, 'Ordering Tool')


@router.get('/daily-chore-lists')
def daily_chore_lists_page(
    request: Request,
    _: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    selected_store_id_raw = request.query_params.get('store_id', '').strip()
    from_raw = request.query_params.get('from', '').strip()
    to_raw = request.query_params.get('to', '').strip()
    selected_store_id = int(selected_store_id_raw) if selected_store_id_raw.isdigit() else None
    try:
        from_date = date.fromisoformat(from_raw) if from_raw else None
        to_date = date.fromisoformat(to_raw) if to_raw else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Invalid date filter') from exc

    stores = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    rows = list_sheets_for_audit(
        db,
        store_id=selected_store_id,
        from_date=from_date,
        to_date=to_date,
    )
    return request.app.state.templates.TemplateResponse(
        'management_daily_chore_audit.html',
        {
            'request': request,
            'stores': stores,
            'rows': rows,
            'selected_store_id': selected_store_id,
            'from_date': from_raw,
            'to_date': to_raw,
        },
    )


@router.get('/daily-chore-lists/{sheet_id}')
def daily_chore_sheet_detail(
    sheet_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    try:
        detail = get_sheet_detail_for_audit(db, sheet_id=sheet_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='DAILY_CHORE_SHEET_VIEWED_AUDIT',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'daily_chore_sheet_id': sheet_id},
    )
    db.commit()
    return request.app.state.templates.TemplateResponse(
        'management_daily_chore_detail.html',
        {
            'request': request,
            'detail': detail,
        },
    )


@router.get('/opening-checklists')
def opening_checklists_page(
    request: Request,
    _: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    selected_store_id_raw = request.query_params.get('store_id', '').strip()
    from_raw = request.query_params.get('from', '').strip()
    to_raw = request.query_params.get('to', '').strip()

    selected_store_id = int(selected_store_id_raw) if selected_store_id_raw.isdigit() else None
    try:
        from_date = date.fromisoformat(from_raw) if from_raw else None
        to_date = date.fromisoformat(to_raw) if to_raw else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Invalid date filter') from exc

    stores = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    rows = list_submissions(
        db,
        store_id=selected_store_id,
        from_date=from_date,
        to_date=to_date,
    )
    return request.app.state.templates.TemplateResponse(
        'management_opening_checklist_audit.html',
        {
            'request': request,
            'stores': stores,
            'rows': rows,
            'selected_store_id': selected_store_id,
            'from_date': from_raw,
            'to_date': to_raw,
        },
    )


@router.get('/opening-checklists/{submission_id}')
def opening_checklists_detail(
    submission_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    try:
        detail = get_submission_detail(db, submission_id=submission_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='OPENING_CHECKLIST_VIEWED_AUDIT',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'opening_checklist_submission_id': submission_id},
    )
    db.commit()
    return request.app.state.templates.TemplateResponse(
        'management_opening_checklist_detail.html',
        {
            'request': request,
            'detail': detail,
        },
    )


@router.get('/change-box-count')
def change_box_count_page(
    request: Request,
    _: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    selected_store_id_raw = request.query_params.get('store_id', '').strip()
    selected_store_id = int(selected_store_id_raw) if selected_store_id_raw.isdigit() else None
    stores = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    rows = list_counts_for_audit(db, store_id=selected_store_id)
    return request.app.state.templates.TemplateResponse(
        'management_change_box_count_audit.html',
        {
            'request': request,
            'stores': stores,
            'rows': rows,
            'selected_store_id': selected_store_id,
        },
    )


@router.get('/change-box-count/{count_id}')
def change_box_count_detail(
    count_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    try:
        detail = get_count_detail(db, count_id=count_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='CHANGE_BOX_COUNT_VIEWED_AUDIT',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'change_box_count_id': count_id},
    )
    db.commit()
    return request.app.state.templates.TemplateResponse(
        'management_change_box_count_detail.html',
        {
            'request': request,
            'detail': detail,
        },
    )


@router.get('/change-forms')
def change_forms_page(
    request: Request,
    _: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    selected_store_id_raw = request.query_params.get('store_id', '').strip()
    selected_store_id = int(selected_store_id_raw) if selected_store_id_raw.isdigit() else None
    stores = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    rows = list_change_forms(db, store_id=selected_store_id)
    return request.app.state.templates.TemplateResponse(
        'management_change_forms.html',
        {
            'request': request,
            'stores': stores,
            'selected_store_id': selected_store_id,
            'rows': rows,
        },
    )


@router.get('/change-forms/{submission_id}')
def change_form_detail(
    submission_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    try:
        detail = get_change_form_detail(db, submission_id=submission_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    log_audit(
        db,
        actor_principal_id=principal.id,
        action='CHANGE_FORM_VIEWED_AUDIT',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'change_form_submission_id': submission_id},
    )
    db.commit()
    return request.app.state.templates.TemplateResponse(
        'management_change_form_detail.html',
        {
            'request': request,
            'detail': detail,
        },
    )


@router.get('/change-box-audit')
def change_box_audit_page(
    request: Request,
    _: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
):
    selected_store_id_raw = request.query_params.get('store_id', '').strip()
    stores = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    selected_store_id = int(selected_store_id_raw) if selected_store_id_raw.isdigit() else (stores[0].id if stores else None)
    inventory = get_inventory_state(db, store_id=selected_store_id) if selected_store_id else {'target_amount': 0, 'total_amount': 0, 'lines': []}
    return request.app.state.templates.TemplateResponse(
        'management_change_box_audit.html',
        {
            'request': request,
            'stores': stores,
            'selected_store_id': selected_store_id,
            'inventory': inventory,
            'denoms': DENOMS,
            'roll_sizes': ROLL_SIZES_BY_CODE,
        },
    )


@router.get('/exchange-return-forms')
def exchange_return_forms_page(
    request: Request,
    _: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    selected_store_id_raw = request.query_params.get('store_id', '').strip()
    from_raw = request.query_params.get('from', '').strip()
    to_raw = request.query_params.get('to', '').strip()
    selected_store_id = int(selected_store_id_raw) if selected_store_id_raw.isdigit() else None
    try:
        from_date = date.fromisoformat(from_raw) if from_raw else None
        to_date = date.fromisoformat(to_raw) if to_raw else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Invalid date filter') from exc

    stores = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    rows = list_exchange_return_forms(
        db,
        store_id=selected_store_id,
        from_date=from_date,
        to_date=to_date,
    )
    return request.app.state.templates.TemplateResponse(
        'management_exchange_return_forms.html',
        {
            'request': request,
            'stores': stores,
            'selected_store_id': selected_store_id,
            'from_date': from_raw,
            'to_date': to_raw,
            'rows': rows,
        },
    )


@router.get('/exchange-return-forms/{form_id}')
def exchange_return_form_detail(
    form_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    try:
        detail = get_exchange_return_form_detail(db, form_id=form_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='EXCHANGE_RETURN_FORM_VIEWED_AUDIT',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'exchange_return_form_id': form_id},
    )
    db.commit()
    return request.app.state.templates.TemplateResponse(
        'management_exchange_return_form_detail.html',
        {
            'request': request,
            'detail': detail,
        },
    )


@router.post('/change-box-audit/{store_id}/submit')
async def change_box_audit_submit(
    store_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    auditor_name = str(form.get('auditor_name', '')).strip()
    target_amount_raw = str(form.get('target_amount', '0')).strip()
    quantities_by_code: dict[str, int] = {}
    for denom in DENOMS:
        code = denom['code']
        rolls_raw = str(form.get(f'qty_rolls__{code}', '0')).strip()
        loose_raw = str(form.get(f'qty_loose__{code}', '0')).strip()
        rolls = int(rolls_raw) if rolls_raw else 0
        loose = int(loose_raw) if loose_raw else 0
        if code in ROLL_SIZES_BY_CODE:
            quantities_by_code[code] = (rolls * ROLL_SIZES_BY_CODE[code]) + loose
        else:
            quantities_by_code[code] = loose

    try:
        target_amount = Decimal(target_amount_raw or '0')
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail='Invalid target amount') from exc

    try:
        audit = submit_inventory_audit(
            db,
            store_id=store_id,
            principal_id=principal.id,
            auditor_name=auditor_name,
            target_amount=target_amount,
            quantities_by_code=quantities_by_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='CHANGE_BOX_AUDIT_SUBMITTED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'change_box_audit_submission_id': audit.id, 'store_id': store_id},
    )
    db.commit()
    return RedirectResponse(f'/management/change-box-audit?store_id={store_id}', status_code=303)


@router.get('/non-sellable-stock-take')
def non_sellable_stock_take_page(
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    selected_store_id_raw = request.query_params.get('store_id', '').strip()
    selected_store_id = int(selected_store_id_raw) if selected_store_id_raw.isdigit() else None
    stores = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    stock_takes = list_stock_takes_for_audit(db, store_id=selected_store_id, include_draft=True)
    items = list_non_sellable_items(db, include_inactive=True)
    return request.app.state.templates.TemplateResponse(
        'management_non_sellable_stock_take.html',
        {
            'request': request,
            'principal': principal,
            'stores': stores,
            'selected_store_id': selected_store_id,
            'stock_takes': stock_takes,
            'items': items,
            'can_manage_items': is_admin_role(principal.role),
        },
    )


@router.get('/non-sellable-stock-take/{stock_take_id}')
def non_sellable_stock_take_detail(
    stock_take_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    try:
        detail = get_stock_take_detail(db, stock_take_id=stock_take_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='NON_SELLABLE_STOCK_TAKE_VIEWED_AUDIT',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'non_sellable_stock_take_id': stock_take_id},
    )
    db.commit()
    return request.app.state.templates.TemplateResponse(
        'management_non_sellable_stock_take_detail.html',
        {
            'request': request,
            'detail': detail,
            'principal': principal,
        },
    )


@router.post('/non-sellable-stock-take/{stock_take_id}/unlock')
async def non_sellable_stock_take_unlock(
    stock_take_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    try:
        take = unlock_stock_take(db, stock_take_id=stock_take_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='NON_SELLABLE_STOCK_TAKE_UNLOCKED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'non_sellable_stock_take_id': take.id},
    )
    db.commit()
    return RedirectResponse(f'/management/non-sellable-stock-take/{take.id}', status_code=303)


@router.post('/non-sellable-stock-take/items/create')
async def non_sellable_item_create(
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    name = str(form.get('name', '')).strip()
    try:
        item = add_non_sellable_item(db, name=name, created_by_principal_id=principal.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='NON_SELLABLE_ITEM_CREATED_OR_REACTIVATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'item_id': item.id, 'name': item.name},
    )
    db.commit()
    return RedirectResponse('/management/non-sellable-stock-take', status_code=303)


@router.post('/non-sellable-stock-take/items/{item_id}/deactivate')
async def non_sellable_item_deactivate(
    item_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    try:
        item = deactivate_non_sellable_item(db, item_id=item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='NON_SELLABLE_ITEM_DEACTIVATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'item_id': item.id, 'name': item.name},
    )
    db.commit()
    return RedirectResponse('/management/non-sellable-stock-take', status_code=303)


@router.get('/customer-requests')
def customer_requests_page(
    request: Request,
    _: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    selected_store_id_raw = request.query_params.get('store_id', '').strip()
    from_raw = request.query_params.get('from', '').strip()
    to_raw = request.query_params.get('to', '').strip()
    selected_store_id = int(selected_store_id_raw) if selected_store_id_raw.isdigit() else None
    try:
        from_date = date.fromisoformat(from_raw) if from_raw else None
        to_date = date.fromisoformat(to_raw) if to_raw else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Invalid date filter') from exc

    stores = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    submissions = list_customer_request_submissions(
        db,
        store_id=selected_store_id,
        from_date=from_date,
        to_date=to_date,
    )
    items = list_customer_request_items_for_management(db)
    return request.app.state.templates.TemplateResponse(
        'management_customer_requests.html',
        {
            'request': request,
            'stores': stores,
            'selected_store_id': selected_store_id,
            'from_date': from_raw,
            'to_date': to_raw,
            'submissions': submissions,
            'items': items,
        },
    )


@router.post('/customer-requests/items/create')
async def customer_requests_item_create(
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    name = str(form.get('name', '')).strip()
    try:
        item = add_customer_request_item(db, name=name, principal_id=principal.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='CUSTOMER_REQUEST_ITEM_CREATED_OR_REACTIVATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'item_id': item.id, 'name': item.name},
    )
    db.commit()
    return RedirectResponse('/management/customer-requests', status_code=303)


@router.post('/customer-requests/items/{item_id}/count')
async def customer_requests_item_set_count(
    item_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    count_raw = str(form.get('request_count', '0')).strip()
    try:
        request_count = int(count_raw)
        item = set_customer_request_item_count(db, item_id=item_id, request_count=request_count)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='CUSTOMER_REQUEST_ITEM_COUNT_UPDATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'item_id': item.id, 'request_count': item.request_count},
    )
    db.commit()
    return RedirectResponse('/management/customer-requests', status_code=303)


@router.get('/audit-queue')
def audit_queue_page(request: Request, _: Principal = Depends(management_access)):
    return _render_placeholder(request, 'Audit Queue')


@router.get('/reports')
def reports_page(request: Request, _: Principal = Depends(management_access)):
    return _render_placeholder(request, 'Reports & Exports')


@router.get('/users')
def users_page(
    request: Request,
    _: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
):
    users = list_management_users(db)
    return request.app.state.templates.TemplateResponse(
        'management_users.html',
        {
            'request': request,
            'users': users,
        },
    )


@router.post('/users/create')
async def create_user(
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    username = str(form.get('username', '')).strip()
    password = str(form.get('password', ''))
    role = str(form.get('role', 'LEAD')).strip().upper()
    try:
        created = create_management_user(
            db,
            actor=principal,
            username=username,
            password=password,
            role=role,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        actor_principal_id=principal.id,
        action='MANAGEMENT_USER_CREATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'principal_id': created.id, 'username': created.username, 'role': created.role.value},
    )
    db.commit()
    return RedirectResponse('/management/users', status_code=303)


@router.post('/users/{target_principal_id}/status')
async def set_user_status(
    target_principal_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    active = str(form.get('active', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
    try:
        updated = set_management_user_active(
            db,
            actor=principal,
            target_principal_id=target_principal_id,
            active=active,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        actor_principal_id=principal.id,
        action='MANAGEMENT_USER_STATUS_UPDATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'principal_id': updated.id, 'active': updated.active},
    )
    db.commit()
    return RedirectResponse('/management/users', status_code=303)


@router.post('/users/{target_principal_id}/password')
async def set_user_password(
    target_principal_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    new_password = str(form.get('new_password', ''))
    try:
        updated = reset_management_user_password(
            db,
            actor=principal,
            target_principal_id=target_principal_id,
            new_password=new_password,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(
        db,
        actor_principal_id=principal.id,
        action='MANAGEMENT_USER_PASSWORD_RESET',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'principal_id': updated.id},
    )
    db.commit()
    return RedirectResponse('/management/users', status_code=303)


@router.get('/sessions')
def list_sessions(
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(
            CountSession.id,
            CountSession.employee_name,
            CountSession.status,
            CountSession.includes_recount,
            CountSession.created_at,
            CountSession.submitted_at,
            Store.name.label('store_name'),
            CountGroup.name.label('group_name'),
            Campaign.category_filter.label('campaign_category_filter'),
            Campaign.label.label('campaign_label'),
        )
        .join(Store, Store.id == CountSession.store_id)
        .join(Campaign, Campaign.id == CountSession.campaign_id)
        .outerjoin(CountGroup, CountGroup.id == CountSession.count_group_id)
        .order_by(CountSession.created_at.desc())
    ).all()

    return request.app.state.templates.TemplateResponse(
        'management_sessions.html',
        {
            'request': request,
            'principal': principal,
            'rows': rows,
        },
    )


@router.post('/sessions/delete')
async def delete_sessions(
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    session_ids = [int(v) for v in form.getlist('session_ids')]
    deleted_count = purge_count_sessions(db, session_ids=session_ids)
    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_SESSIONS_PURGED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'requested_ids': session_ids, 'deleted_count': deleted_count},
    )
    db.commit()
    return RedirectResponse('/management/sessions', status_code=303)


@router.get('/groups')
def groups_page(
    request: Request,
    _: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
):
    params = request.query_params
    data = group_management_data(db)
    store_rotation_rows = list_stores_with_rotation(db)
    store_login_rows = list_store_login_rows(db)
    return request.app.state.templates.TemplateResponse(
        'management_groups.html',
        {
            'request': request,
            'groups': data['groups'],
            'ungrouped_campaigns': data['ungrouped_campaigns'],
            'campaign_rows': data['campaign_rows'],
            'store_rotation_rows': store_rotation_rows,
            'store_login_rows': store_login_rows,
            'sync_summary': {
                'created': params.get('created'),
                'updated': params.get('updated'),
                'deactivated': params.get('deactivated'),
            }
            if params.get('created') is not None
            else None,
        },
    )


@router.post('/groups/create')
async def create_group(
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    name = str(form.get('name', '')).strip()
    campaign_ids = [int(v) for v in form.getlist('campaign_ids')]

    try:
        group = create_count_group(db, name=name, campaign_ids=campaign_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_GROUP_CREATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'group_id': group.id, 'name': name, 'campaign_ids': campaign_ids},
    )
    db.commit()
    return RedirectResponse('/management/groups', status_code=303)


@router.post('/stores/{store_id}/credentials')
async def update_store_credentials(
    store_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    username = str(form.get('username', '')).strip()
    password = str(form.get('password', '')).strip()

    try:
        updated_principal, created = upsert_store_login_credentials(
            db,
            store_id=store_id,
            username=username,
            new_password=password if password else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='STORE_LOGIN_CREDENTIALS_UPDATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={
            'store_id': store_id,
            'principal_id': updated_principal.id,
            'username': updated_principal.username,
            'password_changed': bool(password),
            'created': created,
        },
    )
    db.commit()
    return RedirectResponse('/management/groups', status_code=303)


@router.post('/password/reset')
async def reset_password(
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    current_password = str(form.get('current_password', ''))
    new_password = str(form.get('new_password', ''))
    confirm_password = str(form.get('confirm_password', ''))

    try:
        reset_manager_password(
            db,
            manager_principal_id=principal.id,
            current_password=current_password,
            new_password=new_password,
            confirm_password=confirm_password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='MANAGER_PASSWORD_CHANGED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={},
    )
    db.commit()
    return RedirectResponse('/management/groups', status_code=303)


@router.post('/groups/{group_id}/update')
async def update_group(
    group_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    name = str(form.get('name', '')).strip()
    campaign_ids = [int(v) for v in form.getlist('campaign_ids')]

    try:
        group = update_count_group(db, group_id=group_id, name=name, campaign_ids=campaign_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_GROUP_UPDATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'group_id': group.id, 'name': group.name, 'campaign_ids': campaign_ids},
    )
    db.commit()
    return RedirectResponse('/management/groups', status_code=303)


@router.post('/groups/{group_id}/delete')
async def delete_group(
    group_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    try:
        group = deactivate_count_group(db, group_id=group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_GROUP_DEACTIVATED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'group_id': group.id, 'name': group.name},
    )
    db.commit()
    return RedirectResponse('/management/groups', status_code=303)


@router.post('/groups/renumber')
async def renumber_groups(
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    changed = renumber_count_group_positions(db)
    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_GROUP_POSITIONS_RENUMBERED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'changed_rows': changed},
    )
    db.commit()
    return RedirectResponse('/management/groups', status_code=303)


@router.post('/groups/sync-campaigns')
async def sync_campaigns_from_square(
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    min_items = int(str(form.get('min_items', '1')).strip() or '1')
    if min_items < 1:
        min_items = 1
    deactivate_missing = str(form.get('deactivate_missing', '')).lower() in {'1', 'true', 'on', 'yes'}

    try:
        created, updated, deactivated = sync_campaigns(
            min_items=min_items,
            deactivate_missing=deactivate_missing,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='SQUARE_CAMPAIGNS_SYNCED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={
            'min_items': min_items,
            'deactivate_missing': deactivate_missing,
            'created': created,
            'updated': updated,
            'deactivated': deactivated,
        },
    )
    db.commit()

    query = urlencode({'created': created, 'updated': updated, 'deactivated': deactivated})
    return RedirectResponse(f'/management/groups?{query}', status_code=303)


@router.post('/stores/{store_id}/set-next-group')
async def set_next_group(
    store_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    group_id = int(form.get('group_id'))

    try:
        rotation = set_store_next_group(db, store_id=store_id, group_id=group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='STORE_NEXT_GROUP_SET',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'store_id': store_id, 'group_id': group_id, 'next_group_id': rotation.next_group_id},
    )
    db.commit()
    return RedirectResponse('/management/groups', status_code=303)


@router.get('/sessions/{session_id}')
def view_session(
    session_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    session_row = db.execute(
        select(
            CountSession.id,
            CountSession.store_id,
            CountSession.campaign_id,
            CountSession.count_group_id,
            CountSession.employee_name,
            CountSession.status,
            CountSession.stable_variance,
            CountSession.includes_recount,
            CountSession.created_at,
            CountSession.submitted_at,
            Store.name.label('store_name'),
            CountGroup.name.label('group_name'),
            Campaign.category_filter.label('campaign_category_filter'),
            Campaign.label.label('campaign_label'),
        )
        .join(Store, Store.id == CountSession.store_id)
        .join(Campaign, Campaign.id == CountSession.campaign_id)
        .outerjoin(CountGroup, CountGroup.id == CountSession.count_group_id)
        .where(CountSession.id == session_id)
    ).one_or_none()
    if not session_row:
        return RedirectResponse('/management/sessions', status_code=303)

    variance_rows = get_management_variance_lines(db, session_id=session_id)
    no_variance = all(row['variance'] == 0 for row in variance_rows)
    is_submitted = session_row.status.value == 'SUBMITTED'
    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_SESSION_VIEWED_MANAGER',
        session_id=session_id,
        ip=get_client_ip(request),
        metadata={},
    )
    db.commit()
    return request.app.state.templates.TemplateResponse(
        'management_session_detail.html',
        {
            'request': request,
            'principal': principal,
            'session_row': session_row,
            'variance_rows': variance_rows,
            'no_variance': no_variance,
            'is_submitted': is_submitted,
            'can_force_recount': is_admin_role(principal.role),
        },
    )


@router.post('/sessions/{session_id}/force-recount')
def force_recount(
    session_id: int,
    request: Request,
    principal: Principal = Depends(admin_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    session_row = db.execute(select(CountSession).where(CountSession.id == session_id)).scalar_one_or_none()
    if not session_row:
        raise HTTPException(status_code=404, detail='Session not found')

    forced = create_forced_count(
        db,
        manager_principal_id=principal.id,
        store_id=session_row.store_id,
        group_id=session_row.count_group_id,
        campaign_id=session_row.campaign_id,
        reason='Manager forced recount from submitted session',
        source_session_id=session_id,
    )

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='FORCE_RECOUNT_CREATED',
        session_id=session_id,
        ip=get_client_ip(request),
        metadata={
            'forced_count_id': forced.id,
            'campaign_id': session_row.campaign_id,
            'group_id': session_row.count_group_id,
            'store_id': session_row.store_id,
        },
    )
    db.commit()
    return RedirectResponse('/management/sessions', status_code=303)


@router.post('/sessions/{session_id}/unlock')
def unlock(
    session_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    try:
        count_session = unlock_session(db, principal=principal, session_id=session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_SESSION_UNLOCKED',
        session_id=session_id,
        ip=get_client_ip(request),
        metadata={'previous_status': 'SUBMITTED', 'new_status': 'DRAFT'},
    )
    db.commit()
    return RedirectResponse(f'/management/sessions/{count_session.id}', status_code=303)


@router.get('/sessions/{session_id}/export.csv')
def export_csv(
    session_id: int,
    request: Request,
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    variance_rows = get_management_variance_lines(db, session_id=session_id)

    sio = StringIO()
    writer = csv.writer(sio)
    writer.writerow(['Section', 'SKU', 'Item Name', 'Variation', 'Expected On Hand', 'Counted Qty', 'Variance'])
    for row in variance_rows:
        writer.writerow(
            [
                row['section_type'],
                row['sku'] or '',
                row['item_name'],
                row['variation_name'],
                row['expected_on_hand'],
                row['counted_qty'],
                row['variance'],
            ]
        )

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_SESSION_EXPORTED_CSV',
        session_id=session_id,
        ip=get_client_ip(request),
        metadata={'rows': len(variance_rows)},
    )
    db.commit()

    sio.seek(0)
    return StreamingResponse(
        iter([sio.getvalue()]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename=session-{session_id}-variance.csv'},
    )
