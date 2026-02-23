from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_role
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import Campaign, CountGroup, CountSession, SessionStatus, Store
from app.security.csrf import verify_csrf
from app.services.audit_service import log_audit
from app.services.daily_chore_service import (
    get_or_create_today_sheet,
    get_store_sheet_rows,
    get_store_sheet_strict_today,
    save_sheet_progress,
)
from app.services.opening_checklist_service import create_submission, list_items_for_store
from app.services.notification_service import send_variance_report_stub
from app.services.provider_factory import get_snapshot_provider
from app.services.session_service import (
    create_count_session,
    get_session_for_principal,
    get_store_session_lines,
    save_draft_entries,
    submit_session,
)

router = APIRouter(prefix='/store', tags=['store'])
snapshot_provider = get_snapshot_provider()


def _parse_quantities(form) -> dict[str, Decimal]:
    quantities: dict[str, Decimal] = {}
    for key, value in form.items():
        if not key.startswith('counted_qty__'):
            continue
        variation_id = key.split('__', 1)[1]
        raw = str(value).strip()
        if raw == '':
            continue
        try:
            qty = Decimal(raw)
        except InvalidOperation as exc:
            raise HTTPException(status_code=400, detail=f'Invalid quantity for {variation_id}') from exc
        if qty < 0:
            raise HTTPException(status_code=400, detail=f'Quantity cannot be negative for {variation_id}')
        quantities[variation_id] = qty
    return quantities


@router.get('/home')
def home(
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
):
    return request.app.state.templates.TemplateResponse(
        'store_home.html',
        {
            'request': request,
            'principal': principal,
        },
    )


@router.get('/daily-count')
def daily_count_page(
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
):
    draft_sessions = db.execute(
        select(
            CountSession.id,
            CountSession.employee_name,
            CountSession.status,
            CountSession.includes_recount,
            CountSession.created_at,
        )
        .where(
            CountSession.store_id == principal.store_id,
            CountSession.status == SessionStatus.DRAFT,
        )
        .order_by(CountSession.created_at.desc())
        .limit(25)
    ).all()
    return request.app.state.templates.TemplateResponse(
        'store_daily_count.html',
        {
            'request': request,
            'principal': principal,
            'draft_sessions': draft_sessions,
        },
    )


@router.get('/daily-chore-sheet')
def daily_chore_sheet_page(
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
):
    if principal.store_id is None:
        raise HTTPException(status_code=400, detail='Store login is missing scope')
    sheet, created = get_or_create_today_sheet(
        db,
        store_id=principal.store_id,
        principal_id=principal.id,
    )
    store_name = db.execute(select(Store.name).where(Store.id == principal.store_id)).scalar_one_or_none()
    rows = get_store_sheet_rows(db, sheet_id=sheet.id)
    db.commit()
    return request.app.state.templates.TemplateResponse(
        'store_daily_chore_sheet.html',
        {
            'request': request,
            'principal': principal,
            'sheet': sheet,
            'rows': rows,
            'is_new_sheet': created,
            'is_submitted': sheet.status.value == 'SUBMITTED',
            'store_name': store_name or str(principal.store_id),
        },
    )


@router.post('/daily-chore-sheet/{sheet_id}/save')
async def daily_chore_sheet_save(
    sheet_id: int,
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    if principal.store_id is None:
        raise HTTPException(status_code=400, detail='Store login is missing scope')
    form = await request.form()
    employee_name = str(form.get('employee_name', '')).strip()
    completed_task_ids = {int(value) for value in form.getlist('completed_task_ids') if str(value).isdigit()}

    try:
        sheet = get_store_sheet_strict_today(db, store_id=principal.store_id, sheet_id=sheet_id)
        sheet = save_sheet_progress(
            db,
            sheet=sheet,
            employee_name=employee_name,
            completed_task_ids=completed_task_ids,
            submit=False,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='DAILY_CHORE_SHEET_SAVED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'daily_chore_sheet_id': sheet.id, 'completed_tasks': len(completed_task_ids)},
    )
    db.commit()
    return RedirectResponse('/store/daily-chore-sheet', status_code=303)


@router.post('/daily-chore-sheet/{sheet_id}/submit')
async def daily_chore_sheet_submit(
    sheet_id: int,
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    if principal.store_id is None:
        raise HTTPException(status_code=400, detail='Store login is missing scope')
    form = await request.form()
    employee_name = str(form.get('employee_name', '')).strip()
    completed_task_ids = {int(value) for value in form.getlist('completed_task_ids') if str(value).isdigit()}

    try:
        sheet = get_store_sheet_strict_today(db, store_id=principal.store_id, sheet_id=sheet_id)
        sheet = save_sheet_progress(
            db,
            sheet=sheet,
            employee_name=employee_name,
            completed_task_ids=completed_task_ids,
            submit=True,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='DAILY_CHORE_SHEET_SUBMITTED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={'daily_chore_sheet_id': sheet.id, 'completed_tasks': len(completed_task_ids)},
    )
    db.commit()
    return RedirectResponse('/store/daily-chore-sheet', status_code=303)


@router.get('/opening-checklist')
def opening_checklist_page(
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
):
    if principal.store_id is None:
        raise HTTPException(status_code=400, detail='Store login is missing scope')
    items = list_items_for_store(db, store_id=principal.store_id)
    return request.app.state.templates.TemplateResponse(
        'store_opening_checklist.html',
        {
            'request': request,
            'principal': principal,
            'items': items,
            'notes_types': ['NONE', 'ISSUE', 'MAINTENANCE', 'SUPPLY', 'FOLLOW_UP', 'OTHER'],
        },
    )


@router.post('/opening-checklist/submit')
async def opening_checklist_submit(
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    if principal.store_id is None:
        raise HTTPException(status_code=400, detail='Store login is missing scope')

    form = await request.form()
    submitted_by_name = str(form.get('submitted_by_name', '')).strip()
    lead_name = str(form.get('lead_name', '')).strip()
    previous_employee = str(form.get('previous_employee', '')).strip()
    summary_notes_type = str(form.get('summary_notes_type', '')).strip()
    summary_notes = str(form.get('summary_notes', '')).strip()

    answers_by_item_id: dict[int, str] = {}
    for key, value in form.items():
        if not key.startswith('answer__'):
            continue
        item_id = int(key.split('__', 1)[1])
        answers_by_item_id[item_id] = str(value)

    try:
        submission = create_submission(
            db,
            store_id=principal.store_id,
            created_by_principal_id=principal.id,
            submitted_by_name=submitted_by_name,
            lead_name=lead_name,
            previous_employee=previous_employee,
            summary_notes_type=summary_notes_type,
            summary_notes=summary_notes,
            answers_by_item_id=answers_by_item_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='OPENING_CHECKLIST_SUBMITTED',
        session_id=None,
        ip=get_client_ip(request),
        metadata={
            'opening_checklist_submission_id': submission.id,
            'store_id': principal.store_id,
            'submitted_by_name': submitted_by_name,
            'notes_type': summary_notes_type,
        },
    )
    db.commit()
    return RedirectResponse('/store/opening-checklist', status_code=303)


@router.post('/sessions/generate')
async def generate_session(
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    employee_name = str(form.get('employee_name', '')).strip()
    if not employee_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Employee name is required')

    try:
        count_session = create_count_session(
            db,
            principal=principal,
            employee_name=employee_name,
            snapshot_provider=snapshot_provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_SESSION_GENERATED',
        session_id=count_session.id,
        ip=get_client_ip(request),
        metadata={'employee_name': employee_name, 'includes_recount': count_session.includes_recount},
    )
    db.commit()
    return RedirectResponse(f'/store/sessions/{count_session.id}', status_code=303)


@router.get('/sessions/{session_id}')
def view_session(
    session_id: int,
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
):
    try:
        count_session = get_session_for_principal(db, session_id=session_id, principal=principal)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if count_session.status != SessionStatus.DRAFT:
        raise HTTPException(status_code=403, detail='Submitted sessions are only viewable by lead/admin')

    campaign = db.execute(select(Campaign).where(Campaign.id == count_session.campaign_id)).scalar_one_or_none()
    count_group = db.execute(select(CountGroup).where(CountGroup.id == count_session.count_group_id)).scalar_one_or_none()
    rows = get_store_session_lines(db, session_id=session_id)
    return request.app.state.templates.TemplateResponse(
        'count_entry.html',
        {
            'request': request,
            'principal': principal,
            'count_session': count_session,
            'campaign_label': (campaign.category_filter or campaign.label) if campaign else f'Campaign {count_session.campaign_id}',
            'group_name': count_group.name if count_group else None,
            'rows': rows,
            'locked': count_session.status != SessionStatus.DRAFT,
        },
    )


@router.post('/sessions/{session_id}/draft')
async def save_draft(
    session_id: int,
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    quantities = _parse_quantities(form)

    try:
        count_session = save_draft_entries(
            db,
            principal=principal,
            session_id=session_id,
            quantities_by_variation=quantities,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_SESSION_DRAFT_SAVED',
        session_id=count_session.id,
        ip=get_client_ip(request),
        metadata={'updated_lines': len(quantities)},
    )
    db.commit()
    return RedirectResponse(f'/store/sessions/{session_id}', status_code=303)


@router.post('/sessions/{session_id}/submit')
async def submit(
    session_id: int,
    request: Request,
    principal: Principal = Depends(require_role(Role.STORE)),
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    quantities = _parse_quantities(form)

    try:
        count_session, variance_rows, recount_result = submit_session(
            db,
            principal=principal,
            session_id=session_id,
            quantities_by_variation=quantities,
            snapshot_provider=snapshot_provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = db.execute(select(Store).where(Store.id == count_session.store_id)).scalar_one()

    log_audit(
        db,
        actor_principal_id=principal.id,
        action='COUNT_SESSION_SUBMITTED',
        session_id=count_session.id,
        ip=get_client_ip(request),
        metadata={
            'updated_lines': len(quantities),
            'stable_variance': recount_result['stable'],
            'recount_rounds': recount_result['rounds'],
        },
    )

    if recount_result['square_stub']:
        log_audit(
            db,
            actor_principal_id=principal.id,
            action='SQUARE_UPDATE_STUB_READY',
            session_id=count_session.id,
            ip=get_client_ip(request),
            metadata={
                'message': 'Two consecutive variance signatures are identical. Stub branch reached.',
                'signature': recount_result['signature'],
            },
        )

    send_variance_report_stub(
        db,
        actor_principal_id=principal.id,
        session_id=count_session.id,
        store_name=store.name,
        ip=get_client_ip(request),
        variance_rows=variance_rows,
    )

    db.commit()
    return RedirectResponse('/store/daily-count', status_code=303)
