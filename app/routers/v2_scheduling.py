from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import Principal, Role, get_current_principal, require_capability
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import ScheduleShift
from app.routers.v2 import V2Page, _visible_navigation
from app.security.csrf import verify_csrf
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_board_service import normalize_week_start, serialize_week_board
from app.services.v2_scheduling_service import (
    FEATURE_KEY,
    ShiftInput,
    SchedulingConflict,
    SchedulingValidationError,
    clone_published_revision,
    create_draft_period,
    create_shift,
    delete_shift,
    publish_schedule,
    update_shift,
)
from app.v2.feature_exposure import require_v2_feature
from app.v2.results import ActionResult, ResultKind, SaveOutcome
from app.v2.store_scope import (
    ScopeMode,
    list_authorized_stores,
    resolve_request_store_scope,
)


PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')
router = APIRouter(prefix='/v2/scheduling', tags=['v2-scheduling'])
feature_access = require_v2_feature(FEATURE_KEY)
create_draft_access = require_capability('scheduling.create_draft', Role.ADMIN, Role.MANAGER)
edit_shift_access = require_capability('scheduling.edit_draft_shifts', Role.ADMIN, Role.MANAGER)
delete_shift_access = require_capability('scheduling.delete_draft_shifts', Role.ADMIN, Role.MANAGER)
modify_published_access = require_capability('scheduling.modify_published', Role.ADMIN, Role.MANAGER)
publish_access = require_capability('scheduling.publish', Role.ADMIN, Role.MANAGER)


def board_access(
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> Principal:
    flags = getattr(request.state, 'permission_flags', {}) or {}
    view_all = principal_has_permission(
        db,
        principal=principal,
        permission_key='scheduling.view_all',
        fallback_allowed=principal.role in {Role.ADMIN, Role.MANAGER},
    )
    view_store = principal_has_permission(
        db,
        principal=principal,
        permission_key='scheduling.view_store',
        fallback_allowed=principal.role in {Role.ADMIN, Role.MANAGER},
    )
    if not (view_all or view_store):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    flags['scheduling.view_all'] = view_all
    flags['scheduling.view_store'] = view_store
    request.state.permission_flags = flags
    return principal


class DraftCreatePayload(BaseModel):
    week_start_date: date
    notes: str = ''


class ShiftPayload(BaseModel):
    expected_version: int = Field(gt=0)
    employee_id: int | None = None
    store_id: int = Field(gt=0)
    shift_date: date
    start_time: time
    end_time: time
    unpaid_break_minutes: int = 0
    shift_type_id: int | None = None
    is_opener: bool = False
    is_closer: bool = False
    employee_note: str = ''
    override_hard_unavailability: bool = False
    override_reason: str = ''


class DeleteShiftPayload(BaseModel):
    expected_version: int = Field(gt=0)


class DuplicateShiftPayload(BaseModel):
    expected_version: int = Field(gt=0)


class PublishPayload(BaseModel):
    expected_version: int = Field(gt=0)
    confirm_serious_warnings: bool = False
    override_reason: str = ''


def _requested_week(request: Request) -> date:
    raw = request.query_params.get('start', '').strip()
    if raw:
        try:
            selected = date.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail='Enter start as YYYY-MM-DD.') from exc
    else:
        selected = datetime.now(tz=PORTAL_TIMEZONE).date()
    return normalize_week_start(selected)


def _scope_context(scope, authorized_stores) -> dict:
    if scope.mode == ScopeMode.ALL:
        label = 'All Stores'
    elif len(scope.store_names) == 1:
        label = scope.store_names[0]
    else:
        label = f'{len(scope.store_names)} stores'
    return {
        'stores': [{'id': row.id, 'name': row.name} for row in authorized_stores],
        'selected_store_ids': list(scope.store_ids),
        'all_stores_selected': scope.mode == ScopeMode.ALL,
        'store_scope_label': label,
        'scope_locked': scope.locked,
        'scope_caption': 'Stores',
    }


def _board(
    request: Request,
    db: Session,
    principal: Principal,
    *,
    week_start: date | None = None,
) -> dict:
    scope = resolve_request_store_scope(request, db, principal)
    authorized = list_authorized_stores(db, principal)
    return serialize_week_board(
        db,
        week_start=week_start or _requested_week(request),
        selected_store_ids=scope.store_ids,
        all_authorized_store_ids=tuple(row.id for row in authorized),
        permission_flags=getattr(request.state, 'permission_flags', {}) or {},
    )


def _shift_values(payload: ShiftPayload, *, source_shift_id: int | None = None) -> ShiftInput:
    return ShiftInput(
        employee_id=payload.employee_id,
        store_id=payload.store_id,
        shift_date=payload.shift_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        unpaid_break_minutes=payload.unpaid_break_minutes,
        shift_type_id=payload.shift_type_id,
        is_opener=payload.is_opener,
        is_closer=payload.is_closer,
        employee_note=payload.employee_note,
        source_shift_id=source_shift_id,
    )


def _allow_hard_override(request: Request, payload: ShiftPayload) -> bool:
    if not payload.override_hard_unavailability:
        return False
    flags = getattr(request.state, 'permission_flags', {}) or {}
    if not flags.get('scheduling.override_hard_unavailability', False):
        raise PermissionError('Overriding hard unavailability requires explicit permission.')
    return True


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, SchedulingConflict):
        result = ActionResult(
            kind=ResultKind.CONFLICT,
            message='This schedule changed elsewhere. Refresh the board before trying again.',
            save_outcome=SaveOutcome.NOTHING_SAVED,
            safe_retry=True,
            data={'refresh_required': True},
        )
        return JSONResponse(result.as_json(), status_code=409)
    if isinstance(exc, SchedulingValidationError):
        result = ActionResult(
            kind=ResultKind.VALIDATION_ERROR,
            message=str(exc),
            save_outcome=SaveOutcome.NOTHING_SAVED,
            field_errors=exc.field_errors,
        )
        return JSONResponse(result.as_json(), status_code=422)
    if isinstance(exc, PermissionError):
        result = ActionResult(
            kind=ResultKind.AUTHORIZATION_FAILURE,
            message=str(exc),
            save_outcome=SaveOutcome.NOTHING_SAVED,
        )
        return JSONResponse(result.as_json(), status_code=403)
    result = ActionResult(
        kind=ResultKind.SERVER_FAILURE,
        message='The schedule could not be updated. Refresh and try again.',
        save_outcome=SaveOutcome.NOTHING_SAVED,
        safe_retry=True,
    )
    return JSONResponse(result.as_json(), status_code=500)


def _success_response(
    db: Session,
    request: Request,
    principal: Principal,
    *,
    message: str,
    week_start: date,
    shift_id: int | None = None,
    deleted_shift_id: int | None = None,
) -> dict:
    board = _board(request, db, principal, week_start=week_start)
    canonical_shift = next((row for row in board['shifts'] if row['id'] == shift_id), None)
    return {
        'kind': 'success',
        'message': message,
        'save_outcome': 'local_saved',
        'period_version': board['period']['version'] if board['period'] else None,
        'shift': canonical_shift,
        'deleted_shift_id': deleted_shift_id,
        'summary': board['summary'],
        'labor': board['labor'],
        'warnings': board['warnings'],
        'board': board,
    }


@router.get('/week')
def week_board_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(board_access),
    db: Session = Depends(get_db),
):
    week_start = _requested_week(request)
    scope = resolve_request_store_scope(request, db, principal)
    authorized = list_authorized_stores(db, principal)
    board = serialize_week_board(
        db,
        week_start=week_start,
        selected_store_ids=scope.store_ids,
        all_authorized_store_ids=tuple(row.id for row in authorized),
        permission_flags=getattr(request.state, 'permission_flags', {}) or {},
    )
    db.commit()
    context = {
        'request': request,
        'principal': principal,
        'page': V2Page(
            slug='scheduling/week',
            label='Schedule Board',
            description='Build and review the weekly staff schedule.',
            route_path='/v2/scheduling/week',
            badge='V2 Scheduling',
            active_prefix='/v2/scheduling/week',
        ),
        'navigation': _visible_navigation(request),
        **_scope_context(scope, authorized),
        'board': board,
        'today_week_start': normalize_week_start(datetime.now(tz=PORTAL_TIMEZONE).date()).isoformat(),
    }
    return request.app.state.templates.TemplateResponse('v2/scheduling/week.html', context)


@router.get('/api/board')
def board_api(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(board_access),
    db: Session = Depends(get_db),
):
    board = _board(request, db, principal)
    db.commit()
    return board


@router.post('/api/periods', status_code=201)
def create_period_api(
    payload: DraftCreatePayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(create_draft_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        row = create_draft_period(
            db, principal=principal, week_start=normalize_week_start(payload.week_start_date),
            notes=payload.notes, ip=get_client_ip(request),
        )
        response = _success_response(
            db, request, principal, message='Draft schedule created.', week_start=row.week_start_date,
        )
        db.commit()
        return response
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.post('/api/periods/{schedule_period_id}/shifts', status_code=201)
def create_shift_api(
    schedule_period_id: int,
    payload: ShiftPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(edit_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        outcome = create_shift(
            db, principal=principal, schedule_period_id=schedule_period_id,
            expected_version=payload.expected_version, values=_shift_values(payload),
            allowed_store_ids=scope.store_ids,
            allow_hard_unavailability_override=_allow_hard_override(request, payload),
            override_reason=payload.override_reason, ip=get_client_ip(request),
        )
        period_week = db.get(ScheduleShift, outcome.shift_id).shift_date
        response = _success_response(
            db, request, principal, message='Shift created.',
            week_start=normalize_week_start(period_week), shift_id=outcome.shift_id,
        )
        db.commit()
        return response
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.patch('/api/periods/{schedule_period_id}/shifts/{shift_id}')
def update_shift_api(
    schedule_period_id: int,
    shift_id: int,
    payload: ShiftPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(edit_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        outcome = update_shift(
            db, principal=principal, schedule_period_id=schedule_period_id, shift_id=shift_id,
            expected_version=payload.expected_version, values=_shift_values(payload),
            allowed_store_ids=scope.store_ids,
            allow_hard_unavailability_override=_allow_hard_override(request, payload),
            override_reason=payload.override_reason, ip=get_client_ip(request),
        )
        period_week = db.get(ScheduleShift, outcome.shift_id).shift_date
        response = _success_response(
            db, request, principal, message='Shift updated.',
            week_start=normalize_week_start(period_week), shift_id=outcome.shift_id,
        )
        db.commit()
        return response
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.delete('/api/periods/{schedule_period_id}/shifts/{shift_id}')
def delete_shift_api(
    schedule_period_id: int,
    shift_id: int,
    payload: DeleteShiftPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(delete_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        shift = db.execute(select(ScheduleShift).where(
            ScheduleShift.id == shift_id, ScheduleShift.schedule_period_id == schedule_period_id
        )).scalar_one_or_none()
        if shift is None:
            raise SchedulingValidationError('Shift not found in this schedule.')
        week_start = normalize_week_start(shift.shift_date)
        scope = resolve_request_store_scope(request, db, principal)
        delete_shift(
            db, principal=principal, schedule_period_id=schedule_period_id, shift_id=shift_id,
            expected_version=payload.expected_version, allowed_store_ids=scope.store_ids,
            ip=get_client_ip(request),
        )
        response = _success_response(
            db, request, principal, message='Shift deleted.', week_start=week_start,
            deleted_shift_id=shift_id,
        )
        db.commit()
        return response
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.post('/api/periods/{schedule_period_id}/shifts/{shift_id}/duplicate', status_code=201)
def duplicate_shift_api(
    schedule_period_id: int,
    shift_id: int,
    payload: DuplicateShiftPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(edit_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        source = db.execute(select(ScheduleShift).where(
            ScheduleShift.id == shift_id, ScheduleShift.schedule_period_id == schedule_period_id
        )).scalar_one_or_none()
        if source is None:
            raise SchedulingValidationError('Shift not found in this schedule.')
        scope = resolve_request_store_scope(request, db, principal)
        outcome = create_shift(
            db, principal=principal, schedule_period_id=schedule_period_id,
            expected_version=payload.expected_version,
            values=ShiftInput(
                employee_id=source.employee_id, store_id=source.store_id, shift_date=source.shift_date,
                start_time=source.start_time, end_time=source.end_time,
                unpaid_break_minutes=source.unpaid_break_minutes, shift_type_id=source.shift_type_id,
                is_opener=source.is_opener, is_closer=source.is_closer,
                employee_note=source.employee_note or '', source_shift_id=source.id,
            ),
            allowed_store_ids=scope.store_ids,
            allow_hard_unavailability_override=bool(
                (getattr(request.state, 'permission_flags', {}) or {}).get('scheduling.override_hard_unavailability')
            ),
            override_reason='Duplicated by authorized scheduler.' if source.employee_id else '',
            ip=get_client_ip(request),
        )
        response = _success_response(
            db, request, principal, message='Shift duplicated.',
            week_start=normalize_week_start(source.shift_date), shift_id=outcome.shift_id,
        )
        db.commit()
        return response
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.post('/api/periods/{schedule_period_id}/clone-published', status_code=201)
def clone_published_api(
    schedule_period_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(modify_published_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        row = clone_published_revision(
            db, principal=principal, published_period_id=schedule_period_id,
            allowed_store_ids=scope.store_ids, ip=get_client_ip(request),
        )
        response = _success_response(
            db, request, principal, message='Editable replacement draft created.', week_start=row.week_start_date,
        )
        db.commit()
        return response
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.post('/api/periods/{schedule_period_id}/publish')
def publish_api(
    schedule_period_id: int,
    payload: PublishPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(publish_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    flags = getattr(request.state, 'permission_flags', {}) or {}
    try:
        scope = resolve_request_store_scope(request, db, principal)
        row = publish_schedule(
            db, principal=principal, schedule_period_id=schedule_period_id,
            expected_version=payload.expected_version, allowed_store_ids=scope.store_ids,
            allow_serious_warnings=bool(flags.get('scheduling.publish_with_warnings')),
            confirmed=payload.confirm_serious_warnings, override_reason=payload.override_reason,
            ip=get_client_ip(request),
        )
        response = _success_response(
            db, request, principal, message='Schedule published.', week_start=row.week_start_date,
        )
        db.commit()
        return response
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)
