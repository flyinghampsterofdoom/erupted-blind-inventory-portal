from __future__ import annotations

import csv
from io import StringIO
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_role
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import Campaign, CountGroup, CountSession, Store
from app.security.csrf import verify_csrf
from app.sync_square_campaigns import sync_campaigns
from app.services.audit_service import log_audit
from app.services.session_service import (
    create_count_group,
    create_forced_count,
    deactivate_count_group,
    get_management_variance_lines,
    group_management_data,
    list_store_login_rows,
    list_stores_with_rotation,
    purge_count_sessions,
    renumber_count_group_positions,
    reset_manager_password,
    set_store_next_group,
    unlock_session,
    update_count_group,
    upsert_store_login_credentials,
)

router = APIRouter(prefix='/management', tags=['management'])


@router.get('/sessions')
def list_sessions(
    request: Request,
    _: Principal = Depends(require_role(Role.MANAGER)),
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
            'rows': rows,
        },
    )


@router.post('/sessions/delete')
async def delete_sessions(
    request: Request,
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    _: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
            'session_row': session_row,
            'variance_rows': variance_rows,
            'no_variance': no_variance,
            'is_submitted': is_submitted,
        },
    )


@router.post('/sessions/{session_id}/force-recount')
def force_recount(
    session_id: int,
    request: Request,
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
    principal: Principal = Depends(require_role(Role.MANAGER)),
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
