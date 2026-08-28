from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import Principal, Role, get_current_principal, require_capability
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import (
    AttendanceEventType, Employee, ScheduleAttendanceEvent, SchedulePeriod,
    SchedulePeriodStatus, ScheduleShift,
    ShiftTransferRequest,
    EmployeeSchedulingProfile, EmployeeSchedulingStorePreference, EmployeeSchedulingWindow,
    SchedulingNotification, SchedulingOrganizationPolicy, SchedulingWindowKind,
    ShiftTransferStatus, SpecialStoreParticipation, SpecialStorePolicy, SpecialStoreRotationState,
    Store, StorePreferenceLevel,
)
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
from app.services.v2_scheduling_attendance_service import (
    record_attendance_event, void_attendance_event,
)
from app.services.v2_scheduling_policy_service import (
    automation_draft_dashboard, configure_special_store, create_transfer_request,
    manual_generate_draft_schedule, regenerate_period, respond_to_transfer,
    review_transfer, run_schedule_automation, set_manual_lock, set_publication_hold,
    set_special_store_employee_participation, update_organization_policy, weekend_fairness,
    longview_rotation_fairness, organization_policy,
)
from app.services.v2_scheduling_rules_service import (
    set_full_day_weekday_lockouts, set_store_preference, upsert_employee_profile,
)
from app.services.v2_scheduling_roster_service import (
    is_scheduling_candidate,
    set_scheduling_capabilities,
    set_scheduling_participation,
    sync_square_scheduling_roster,
)
from app.services.v2_scheduling_pattern_service import (
    ALTERNATING_WEEK_A_ANCHOR, alternating_week_for_date, mask_label,
    mask_to_weekdays, weekdays_to_mask,
)
from app.services.v2_scheduling_assignments_service import (
    get_store_defaults, lead_fairness, override_double_coverage_employee, set_lead_of_day,
    update_store_defaults,
)
from app.services.v2_store_shift_service import (
    StoreShiftInput,
    copy_store_shift,
    create_store_shift,
    list_store_shifts,
    place_store_shift,
    reorder_store_shifts,
    update_store_shift,
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
view_store_shift_access = require_capability('scheduling.store_shifts.view', Role.ADMIN, Role.MANAGER)
manage_store_shift_access = require_capability('scheduling.store_shifts.manage', Role.ADMIN, Role.MANAGER)
place_store_shift_access = require_capability('scheduling.store_shifts.place', Role.ADMIN, Role.MANAGER)
generate_access = require_capability('scheduling.generate', Role.ADMIN, Role.MANAGER)
automation_access = require_capability('scheduling.manage_automation', Role.ADMIN, Role.MANAGER)
transfer_access = require_capability('scheduling.transfer_own')
transfer_approval_access = require_capability('scheduling.approve_transfer_hours', Role.ADMIN, Role.MANAGER)
own_schedule_access = require_capability('scheduling.view_own')
preferences_access = require_capability('scheduling.manage_preferences', Role.ADMIN, Role.MANAGER)
special_rotation_access = require_capability('scheduling.manage_special_rotation', Role.ADMIN, Role.MANAGER)
attendance_access = require_capability(
    'scheduling.attendance.record', Role.ADMIN, Role.MANAGER, Role.LEAD)


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


class LockPayload(BaseModel):
    locked: bool
    reason: str = ''


class HoldPayload(BaseModel):
    held: bool
    reason: str = ''


class TransferCreatePayload(BaseModel):
    shift_id: int = Field(gt=0)
    to_employee_id: int = Field(gt=0)


class TransferResponsePayload(BaseModel):
    accept: bool


class TransferReviewPayload(BaseModel):
    approve: bool
    note: str = ''


class AttendanceEventPayload(BaseModel):
    event_type: AttendanceEventType
    event_at: datetime
    replacement_employee_id: int | None = Field(default=None, gt=0)
    note: str = Field(default='', max_length=2000)
    override_store_restriction: bool = False
    override_reason: str = Field(default='', max_length=500)


class AttendanceCorrectionPayload(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class EmployeePolicyPayload(BaseModel):
    home_store_id: int | None = None
    target_shifts_per_week: int | None = Field(default=3, ge=0, le=7)
    week_a_workdays: list[int] = Field(default_factory=list)
    week_b_workdays: list[int] = Field(default_factory=list)
    target_weekly_hours: Decimal = Decimal('0')
    minimum_weekly_hours: Decimal | None = None
    maximum_weekly_hours: Decimal | None = None
    approval_weekly_hours: Decimal | None = None
    max_consecutive_work_days: int | None = None
    minimum_days_off_after_max_block: int = 1
    special_store_participation: SpecialStoreParticipation = SpecialStoreParticipation.NONE
    scheduler_note: str = ''
    active: bool = True


class StorePreferencePayload(BaseModel):
    store_id: int = Field(gt=0)
    preference_rank: int | None = None
    preference_level: StorePreferenceLevel = StorePreferenceLevel.ACCEPTABLE
    active: bool = True


class WeekdayLockoutPayload(BaseModel):
    weekdays: list[int] = Field(default_factory=list)


class AutomationPolicyPayload(BaseModel):
    weekly_approval_hours: Decimal = Decimal('40')
    schedule_length_weeks: int = Field(gt=0, le=8)
    generate_days_before_end: int = Field(ge=0)
    publish_days_before_end: int = Field(ge=0)
    publication_local_time: time
    timezone_name: str = 'America/Los_Angeles'
    active: bool = True


class SpecialStorePayload(BaseModel):
    store_id: int = Field(gt=0)
    primary_employee_ids: list[int] = Field(default_factory=list)
    rotation_employee_ids: list[int] = Field(default_factory=list)
    active: bool = True


class StoreShiftPayload(BaseModel):
    label: str
    store_id: int = Field(gt=0)
    start_time: time
    end_time: time
    active_weekdays: list[int]
    active: bool = True
    display_order: int = Field(default=0, ge=0)
    manager_note: str = ''


class StoreShiftCopyPayload(BaseModel):
    destination_store_id: int = Field(gt=0)
    label: str | None = None


class StoreShiftReorderPayload(BaseModel):
    ordered_ids: list[int]


class StoreShiftPlacementPayload(BaseModel):
    expected_version: int = Field(gt=0)
    shift_date: date
    employee_id: int | None = None
    destination_store_id: int = Field(gt=0)


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


def _requested_period_id(request: Request) -> int | None:
    raw = request.query_params.get('period_id', '').strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail='Enter period_id as an integer.') from exc
    if value <= 0:
        raise HTTPException(status_code=422, detail='Enter a positive period_id.')
    return value


def _authorize_explicit_period(
    db: Session, *, principal: Principal, schedule_period_id: int | None,
) -> SchedulePeriod | None:
    if schedule_period_id is None:
        return None
    period = db.get(SchedulePeriod, schedule_period_id)
    if period is None:
        raise HTTPException(status_code=404, detail='Schedule period not found.')
    if period.status == SchedulePeriodStatus.DRAFT:
        allowed = principal_has_permission(
            db, principal=principal, permission_key='scheduling.generate',
            fallback_allowed=principal.role in {Role.ADMIN, Role.MANAGER},
        ) or principal_has_permission(
            db, principal=principal, permission_key='scheduling.manage_automation',
            fallback_allowed=principal.role in {Role.ADMIN, Role.MANAGER},
        )
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return period


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
    schedule_period_id = _requested_period_id(request)
    explicit_period = _authorize_explicit_period(
        db, principal=principal, schedule_period_id=schedule_period_id)
    scope = resolve_request_store_scope(request, db, principal)
    authorized = list_authorized_stores(db, principal)
    return serialize_week_board(
        db,
        week_start=(explicit_period.week_start_date if explicit_period else week_start or _requested_week(request)),
        selected_store_ids=scope.store_ids,
        all_authorized_store_ids=tuple(row.id for row in authorized),
        permission_flags=getattr(request.state, 'permission_flags', {}) or {},
        schedule_period_id=schedule_period_id,
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


def _store_shift_values(payload: StoreShiftPayload) -> StoreShiftInput:
    return StoreShiftInput(
        label=payload.label,
        store_id=payload.store_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        active_weekdays=tuple(payload.active_weekdays),
        active=payload.active,
        display_order=payload.display_order,
        manager_note=payload.manager_note,
    )


def _store_shift_list(db: Session, request: Request, principal: Principal, *, include_inactive: bool) -> list[dict]:
    scope = resolve_request_store_scope(request, db, principal)
    flags = getattr(request.state, 'permission_flags', {}) or {}
    return list_store_shifts(
        db,
        allowed_store_ids=scope.store_ids,
        include_inactive=include_inactive and bool(flags.get('scheduling.store_shifts.manage')),
        include_manager_note=bool(flags.get('scheduling.store_shifts.manage')),
    )


def _store_shift_success(
    *,
    message: str,
    store_shifts: list[dict],
    store_shift_id: int | None = None,
    save_outcome: SaveOutcome = SaveOutcome.LOCAL_SAVED,
) -> dict:
    result = ActionResult(
        kind=ResultKind.SUCCESS,
        message=message,
        save_outcome=save_outcome,
        data={'store_shift_id': store_shift_id, 'store_shifts': store_shifts},
    ).as_json()
    result.update(result.pop('data'))
    return result


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


def _simple_page_context(request: Request, principal: Principal, *, page: V2Page, **values) -> dict:
    return {
        'request': request, 'principal': principal, 'page': page, 'navigation': _visible_navigation(request),
        'stores': [], 'selected_store_ids': [], 'all_stores_selected': True,
        'store_scope_label': 'All Stores', 'scope_locked': True,
        'message': request.query_params.get('message', ''), 'error': request.query_params.get('error', ''),
        **values,
    }


def _form_back(path: str, *, message: str = '', error: str = '') -> RedirectResponse:
    query = ('?message=' + quote(message)) if message else (('?error=' + quote(error)) if error else '')
    return RedirectResponse(path + query, status_code=303)


@router.get('/rules')
def scheduling_rules_page(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(preferences_access), db: Session = Depends(get_db),
):
    employees = list(db.execute(select(Employee).where(
        Employee.scheduling_active.is_(True)).order_by(Employee.full_name, Employee.id)).scalars())
    profiles = {row.employee_id: row for row in db.execute(select(EmployeeSchedulingProfile)).scalars()}
    policy = organization_policy(db)
    return request.app.state.templates.TemplateResponse('v2/scheduling/rules.html', _simple_page_context(
        request, principal, page=V2Page('scheduling/rules', 'Scheduling Rules',
        'Configure employee eligibility, fairness inputs, and automation.', route_path='/v2/scheduling/rules',
        badge='V2 Scheduling', active_prefix='/v2/scheduling/rules'), employees=employees,
        profiles=profiles, organization_policy=policy,
        pattern_labels={employee.id: {
            'A': mask_label(profiles[employee.id].week_a_workdays_mask),
            'B': mask_label(profiles[employee.id].week_b_workdays_mask),
        } for employee in employees if employee.id in profiles},
    ))


@router.get('/employees')
def scheduling_employees_page(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(preferences_access), db: Session = Depends(get_db),
):
    selected_status = str(request.query_params.get('status') or 'active').strip().lower()
    if selected_status not in {'active', 'inactive'}:
        selected_status = 'active'
    search = str(request.query_params.get('q') or '').strip().lower()
    active_count = db.execute(select(func.count(Employee.id)).where(
        Employee.scheduling_active.is_(True))).scalar_one()
    inactive_count = db.execute(select(func.count(Employee.id)).where(
        Employee.scheduling_active.is_(False))).scalar_one()
    profiles = {row.employee_id: row for row in db.execute(select(EmployeeSchedulingProfile)).scalars()}
    stores_by_square_id = {
        row.square_location_id: row.name for row in db.execute(select(Store).where(
            Store.square_location_id.is_not(None))).scalars()
    }
    rows = []
    employee_query = select(Employee).where(
        Employee.scheduling_active.is_(selected_status == 'active')).order_by(Employee.full_name, Employee.id)
    for employee in db.execute(employee_query).scalars():
        profile = profiles.get(employee.id)
        if search and search not in employee.full_name.lower():
            continue
        if employee.square_location_assignment == 'ALL_CURRENT_AND_FUTURE_LOCATIONS':
            location_summary = 'All current and future Square locations'
        else:
            location_summary = ', '.join(
                stores_by_square_id.get(location_id, location_id)
                for location_id in (employee.square_location_ids or [])
            ) or 'No Square locations supplied'
        rows.append({
            'employee': employee,
            'profile': profile,
            'location_summary': location_summary,
            'eligible': is_scheduling_candidate(employee),
            'needs_review': profile is None or not employee.scheduling_active,
        })
    return request.app.state.templates.TemplateResponse('v2/scheduling/employees.html', _simple_page_context(
        request, principal, page=V2Page('scheduling/employees', 'Employees',
        'Square-sourced roster and local autoscheduler participation.', route_path='/v2/scheduling/employees',
        badge='V2 Scheduling', active_prefix='/v2/scheduling/employees'), rows=rows,
        selected_status=selected_status, search=search,
        active_count=active_count, inactive_count=inactive_count,
    ))


@router.post('/employees/sync')
def sync_scheduling_employees(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(preferences_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        result = sync_square_scheduling_roster(db, principal=principal)
        db.commit()
        return _form_back('/v2/scheduling/employees', message=result.message)
    except (RuntimeError, SQLAlchemyError, ValueError):
        db.rollback()
        return _form_back('/v2/scheduling/employees', error=(
            'Square roster sync could not be completed. No roster changes were saved.'))


@router.post('/employees/{employee_id}/scheduling-status')
def employee_scheduling_status(
    employee_id: int, request: Request, active: bool = Form(...),
    return_status: str = Form('active'), return_q: str = Form(''),
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(preferences_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        employee = set_scheduling_participation(
            db, principal=principal, employee_id=employee_id, active=active)
        db.commit()
        path = f'/v2/scheduling/employees?status={quote(return_status)}&q={quote(return_q)}&message=' + quote(
            f'{employee.full_name} is now '
            f'{"Active" if employee.scheduling_active else "Inactive"} for Scheduling.')
        return RedirectResponse(path, status_code=303)
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        return _form_back('/v2/scheduling/employees', error=str(exc))


@router.post('/employees/{employee_id}/capabilities')
def employee_scheduling_capabilities(
    employee_id: int, request: Request,
    lead_capable: bool = Form(False), double_coverage: bool = Form(False),
    return_status: str = Form('active'), return_q: str = Form(''),
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(preferences_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        employee = set_scheduling_capabilities(
            db, principal=principal, employee_id=employee_id,
            lead_capable=lead_capable, double_coverage=double_coverage)
        db.commit()
        path = f'/v2/scheduling/employees?status={quote(return_status)}&q={quote(return_q)}&message=' + quote(
            f'{employee.full_name} scheduling capabilities saved.')
        return RedirectResponse(path, status_code=303)
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        return _form_back('/v2/scheduling/employees', error=str(exc))


@router.get('/store-defaults')
def scheduling_store_defaults_page(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(preferences_access), db: Session = Depends(get_db),
):
    stores = list(db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name)).scalars())
    return request.app.state.templates.TemplateResponse('v2/scheduling/store_defaults.html', _simple_page_context(
        request, principal, page=V2Page('scheduling/store-defaults', 'Store Defaults',
        'Configure store-backed defaults used by schedule generation.', route_path='/v2/scheduling/store-defaults',
        badge='V2 Scheduling', active_prefix='/v2/scheduling/store-defaults'), stores=stores,
        defaults=get_store_defaults(db),
    ))


@router.post('/store-defaults')
def update_scheduling_store_defaults(
    request: Request, double_coverage_store_id: int | None = Form(None),
    standard_shift_start: time = Form(...), standard_shift_end: time = Form(...),
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(preferences_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        update_store_defaults(
            db, principal=principal, store_id=double_coverage_store_id,
            standard_shift_start=standard_shift_start, standard_shift_end=standard_shift_end)
        db.commit()
        return _form_back('/v2/scheduling/store-defaults', message='Scheduling Store Defaults saved.')
    except (SchedulingValidationError, SQLAlchemyError) as exc:
        db.rollback()
        return _form_back('/v2/scheduling/store-defaults', error=str(exc))


@router.get('/employees/{employee_id}')
def employee_policy_page(
    employee_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(preferences_access), db: Session = Depends(get_db),
):
    employee = db.get(Employee, employee_id)
    if employee is None: raise HTTPException(status_code=404)
    profile = db.execute(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id == employee.id)).scalar_one_or_none()
    stores = list(db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name)).scalars())
    special_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(SpecialStorePolicy.active.is_(True))).scalars())
    preferences = {row.store_id: row for row in db.execute(select(EmployeeSchedulingStorePreference).where(
        EmployeeSchedulingStorePreference.employee_id == employee.id)).scalars()}
    lockouts = set(db.execute(select(EmployeeSchedulingWindow.day_of_week).where(
        EmployeeSchedulingWindow.employee_id == employee.id,
        EmployeeSchedulingWindow.kind == SchedulingWindowKind.HARD_UNAVAILABLE,
        EmployeeSchedulingWindow.active.is_(True), EmployeeSchedulingWindow.start_time == time.min,
        EmployeeSchedulingWindow.end_time == time.max)).scalars())
    special_states = list(db.execute(select(SpecialStoreRotationState).where(
        SpecialStoreRotationState.employee_id == employee.id)).scalars())
    today = datetime.now(PORTAL_TIMEZONE).date()
    next_saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    next_sunday = today + timedelta(days=(6 - today.weekday()) % 7)
    fairness = {
        'saturday': weekend_fairness(
            db, employee_id=employee.id, weekday=5,
            before_date=next_saturday, as_of_date=today),
        'sunday': weekend_fairness(
            db, employee_id=employee.id, weekday=6,
            before_date=next_sunday, as_of_date=today),
    }
    fairness_dates = {'saturday': next_saturday, 'sunday': next_sunday}
    planned_through = db.execute(select(func.max(SchedulePeriod.week_end_date)).where(
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
        SchedulePeriod.week_end_date >= today)).scalar_one()
    longview_cutoff = (planned_through + timedelta(days=1)
                       if planned_through else today + timedelta(weeks=8))
    longview_diagnostics = {
        state.store_id: {
            'fairness': longview_rotation_fairness(
                db, employee_id=employee.id, store_id=state.store_id,
                before_date=longview_cutoff, as_of_date=today),
            'never': bool(
                preferences.get(state.store_id)
                and preferences[state.store_id].active
                and preferences[state.store_id].preference_level == StorePreferenceLevel.NEVER),
        }
        for state in special_states
    }
    lead_diagnostic = lead_fairness(
        db, employee_id=employee.id, before_date=longview_cutoff,
        planning_date=today)
    defaults = get_store_defaults(db)
    standard_minutes = (
        (defaults.standard_shift_end.hour * 60 + defaults.standard_shift_end.minute)
        - (defaults.standard_shift_start.hour * 60 + defaults.standard_shift_start.minute)
        if defaults and defaults.standard_shift_start and defaults.standard_shift_end else None)
    target_shifts = profile.target_shifts_per_week if profile and profile.target_shifts_per_week is not None else 3
    expected_minutes = standard_minutes * target_shifts if standard_minutes is not None else None
    expected_hours_label = (
        f'{expected_minutes // 60}h {expected_minutes % 60:02d}m' if expected_minutes is not None else None)
    return request.app.state.templates.TemplateResponse('v2/scheduling/employee_policy.html', _simple_page_context(
        request, principal, page=V2Page('scheduling/employees', f'{employee.full_name} Scheduling',
        'Admin-managed scheduling eligibility and preferences.', route_path='/v2/scheduling/employees',
        badge='Admin only', active_prefix='/v2/scheduling/employees'), employee=employee, profile=profile,
        organization_policy=organization_policy(db), normal_stores=[s for s in stores if s.id not in special_ids],
        special_stores=[s for s in stores if s.id in special_ids], preferences=preferences,
        lockouts=lockouts, special_states={s.store_id: s for s in special_states},
        fairness=fairness, fairness_dates=fairness_dates,
        longview_diagnostics=longview_diagnostics,
        longview_through=longview_cutoff - timedelta(days=1),
        lead_diagnostic=lead_diagnostic,
        standard_shift_defaults=defaults, expected_hours_label=expected_hours_label,
        week_a_days=set(mask_to_weekdays(profile.week_a_workdays_mask if profile else None)),
        week_b_days=set(mask_to_weekdays(profile.week_b_workdays_mask if profile else None)),
        current_alternating_week=alternating_week_for_date(datetime.now(PORTAL_TIMEZONE).date()),
        alternating_anchor=ALTERNATING_WEEK_A_ANCHOR,
    ))


@router.post('/employees/{employee_id}')
async def save_employee_policy_page(
    employee_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(preferences_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form(); path = f'/v2/scheduling/employees/{employee_id}'
    try:
        scope = resolve_request_store_scope(request, db, principal)
        def decimal_or_none(name):
            raw = str(form.get(name, '')).strip(); return Decimal(raw) if raw else None
        profile = upsert_employee_profile(db, principal=principal, employee_id=employee_id,
            home_store_id=int(form['home_store_id']) if form.get('home_store_id') else None,
            target_shifts_per_week=int(form.get('target_shifts_per_week', 3)),
            week_a_workdays_mask=weekdays_to_mask(
                tuple(int(v) for v in form.getlist('week_a_workday'))),
            week_b_workdays_mask=weekdays_to_mask(
                tuple(int(v) for v in form.getlist('week_b_workday'))),
            target_weekly_hours=Decimal(str(form.get('target_weekly_hours', '0'))),
            approval_weekly_hours=decimal_or_none('approval_weekly_hours'),
            max_consecutive_work_days=int(form['max_consecutive_work_days']) if form.get('max_consecutive_work_days') else None,
            minimum_days_off_after_max_block=int(form.get('minimum_days_off_after_max_block', 1)),
            allowed_store_ids=scope.store_ids, scheduler_note=str(form.get('scheduler_note', '')))
        set_full_day_weekday_lockouts(db, principal=principal, employee_id=employee_id,
                                      weekdays=tuple(int(v) for v in form.getlist('weekday_lockout')))
        special_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(SpecialStorePolicy.active.is_(True))).scalars())
        for store_id in scope.store_ids:
            if store_id in special_ids:
                raw = str(form.get(f'special_{store_id}', 'NONE'))
                set_special_store_employee_participation(db, principal=principal, store_id=store_id,
                    employee_id=employee_id, participation=SpecialStoreParticipation(raw))
            else:
                raw = str(form.get(f'preference_{store_id}', 'ACCEPTABLE'))
                set_store_preference(db, principal=principal, employee_id=employee_id, store_id=store_id,
                    preference_rank=None, preference_level=StorePreferenceLevel(raw), allowed_store_ids=scope.store_ids)
        db.commit(); return _form_back(path, message='Employee scheduling policy saved.')
    except (ValueError, KeyError, SchedulingValidationError, PermissionError) as exc:
        db.rollback(); return _form_back(path, error=str(exc))


@router.get('/automation')
def automation_page(request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(automation_access), db: Session = Depends(get_db)):
    policy = organization_policy(db)
    today = datetime.now(ZoneInfo(policy.timezone_name)).date()
    dashboard = automation_draft_dashboard(db, today=today)
    can_generate = principal_has_permission(
        db, principal=principal, permission_key='scheduling.generate',
        fallback_allowed=principal.role in {Role.ADMIN, Role.MANAGER},
    )
    return request.app.state.templates.TemplateResponse('v2/scheduling/automation.html', _simple_page_context(
        request, principal, page=V2Page('scheduling/automation', 'Schedule Automation',
        'Configure generation and publication in business-local time.', route_path='/v2/scheduling/automation',
        badge='Admin only', active_prefix='/v2/scheduling/automation'), policy=policy,
        dashboard=dashboard, can_generate=can_generate))


def _generation_message(result: dict, *, regenerated: bool = False) -> str:
    uncovered = sum(len(row.get('uncovered', ())) for row in result.get('results', ()))
    lead = sum(len(row.get('lead_uncovered', ())) for row in result.get('results', ()))
    double = sum(len(row.get('double_coverage', {}).get('uncovered', ()))
                 for row in result.get('results', ()))
    action = 'regenerated' if regenerated else ('generated' if result.get('created') else 'already exists')
    return (f'Draft schedule {action}. Diagnostics: {uncovered} uncovered, '
            f'{lead} Lead of Day, {double} double-coverage warning(s).')


@router.post('/automation/generate')
def generate_automation_page(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(generate_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        result = manual_generate_draft_schedule(db, principal=principal)
        db.commit()
        destination = f'/v2/scheduling/week?period_id={result["primary_period_id"]}'
        return _form_back(destination, message=_generation_message(result))
    except (SchedulingValidationError, SchedulingConflict, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _form_back('/v2/scheduling/automation', error=str(exc))


@router.get('/periods/{period_id}/review')
def review_period_page(
    period_id: int, _feature: Principal = Depends(feature_access),
    _principal: Principal = Depends(automation_access), db: Session = Depends(get_db),
):
    period = db.get(SchedulePeriod, period_id)
    if period is None or period.status != SchedulePeriodStatus.DRAFT:
        raise HTTPException(status_code=404, detail='Draft schedule period not found.')
    return RedirectResponse(f'/v2/scheduling/week?period_id={period.id}', status_code=303)


@router.post('/periods/{period_id}/regenerate')
def regenerate_period_page(
    period_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(generate_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        diagnostics = regenerate_period(db, principal=principal, schedule_period_id=period_id)
        db.commit()
        result = {'created': True, 'results': [diagnostics]}
        return _form_back(
            f'/v2/scheduling/week?period_id={period_id}',
            message=_generation_message(result, regenerated=True),
        )
    except (SchedulingValidationError, SchedulingConflict, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _form_back(f'/v2/scheduling/week?period_id={period_id}', error=str(exc))


@router.post('/automation')
def save_automation_page(request: Request, weekly_approval_hours: Decimal = Form(...),
    schedule_length_weeks: int = Form(...), generate_days_before_end: int = Form(...),
    publish_days_before_end: int = Form(...), publication_local_time: time = Form(...),
    timezone_name: str = Form(...), active: bool = Form(False), _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(automation_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf)):
    try:
        update_organization_policy(db, principal=principal, weekly_approval_hours=weekly_approval_hours,
            schedule_length_weeks=schedule_length_weeks, generate_days_before_end=generate_days_before_end,
            publish_days_before_end=publish_days_before_end, publication_local_time=publication_local_time,
            timezone_name=timezone_name, active=active)
        db.commit(); return _form_back('/v2/scheduling/automation', message='Automation settings saved.')
    except (SchedulingValidationError, ValueError) as exc:
        db.rollback(); return _form_back('/v2/scheduling/automation', error=str(exc))


@router.post('/periods/{period_id}/hold')
def hold_period_page(period_id: int, request: Request, held: bool = Form(...), reason: str = Form(''),
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(automation_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    try:
        set_publication_hold(db, principal=principal, schedule_period_id=period_id, held=held, reason=reason)
        db.commit(); return _form_back('/v2/scheduling/automation', message='Publication hold updated.')
    except (SchedulingValidationError, SchedulingConflict) as exc:
        db.rollback(); return _form_back('/v2/scheduling/automation', error=str(exc))


@router.post('/shifts/{shift_id}/lock-form')
def lock_shift_form(shift_id: int, request: Request, locked: bool = Form(...), reason: str = Form(''),
    return_start: str = Form(''), _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(edit_shift_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf)):
    try:
        set_manual_lock(db, principal=principal, shift_id=shift_id, locked=locked, reason=reason)
        db.commit(); path = '/v2/scheduling/week' + (f'?start={quote(return_start)}' if return_start else '')
        return RedirectResponse(path, status_code=303)
    except (SchedulingValidationError, SchedulingConflict) as exc:
        db.rollback(); return _form_back('/v2/scheduling/week', error=str(exc))


@router.post('/shifts/{shift_id}/lead-of-day')
def set_lead_of_day_form(
    shift_id: int, request: Request, return_start: str = Form(''),
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(preferences_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        set_lead_of_day(db, principal=principal, shift_id=shift_id)
        db.commit()
        path = '/v2/scheduling/week' + (f'?start={quote(return_start)}' if return_start else '')
        return RedirectResponse(path, status_code=303)
    except (SchedulingValidationError, SchedulingConflict) as exc:
        db.rollback()
        return _form_back('/v2/scheduling/week', error=str(exc))


@router.post('/shifts/{shift_id}/double-coverage')
def set_double_coverage_form(
    shift_id: int, request: Request, employee_id: int = Form(...), return_start: str = Form(''),
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(preferences_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        override_double_coverage_employee(
            db, principal=principal, shift_id=shift_id, employee_id=employee_id)
        db.commit()
        path = '/v2/scheduling/week' + (f'?start={quote(return_start)}' if return_start else '')
        return RedirectResponse(path, status_code=303)
    except (SchedulingValidationError, SchedulingConflict) as exc:
        db.rollback()
        return _form_back('/v2/scheduling/week', error=str(exc))


@router.get('/my-schedule')
def my_schedule_page(request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(own_schedule_access), db: Session = Depends(get_db)):
    employee = db.execute(select(Employee).where(Employee.principal_id == principal.id)).scalar_one_or_none()
    if employee is None: raise HTTPException(status_code=409, detail='Account is not linked to an employee.')
    shifts = list(db.execute(select(ScheduleShift).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee.id, SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED)
        .order_by(ScheduleShift.shift_date, ScheduleShift.start_time)).scalars())
    requests = list(db.execute(select(ShiftTransferRequest).where(or_(
        ShiftTransferRequest.from_employee_id == employee.id,
        ShiftTransferRequest.to_employee_id == employee.id)).order_by(ShiftTransferRequest.created_at.desc())).scalars())
    employees = [row for row in db.execute(select(Employee).where(
        Employee.active.is_(True), Employee.id != employee.id).order_by(Employee.full_name)).scalars()
        if is_scheduling_candidate(row)]
    notifications = list(db.execute(select(SchedulingNotification).where(
        SchedulingNotification.principal_id == principal.id).order_by(SchedulingNotification.created_at.desc()).limit(20)).scalars())
    employee_by_id = {row.id: row for row in db.execute(select(Employee)).scalars()}
    shift_by_id = {row.id: row for row in db.execute(select(ScheduleShift).where(
        ScheduleShift.id.in_([r.shift_id for r in requests] or (-1,)))).scalars()}
    store_by_id = {row.id: row for row in db.execute(select(Store)).scalars()}
    return request.app.state.templates.TemplateResponse('v2/scheduling/my_schedule.html', _simple_page_context(
        request, principal, page=V2Page('scheduling/my-schedule', 'My Schedule',
        'View assignments and manage shift offers.', route_path='/v2/scheduling/my-schedule',
        badge='Employee', active_prefix='/v2/scheduling/my-schedule'), employee=employee, shifts=shifts,
        requests=requests, candidates=employees, notifications=notifications,
        employee_by_id=employee_by_id, shift_by_id=shift_by_id, store_by_id=store_by_id,
        today=datetime.now(PORTAL_TIMEZONE).date()))


@router.post('/my-schedule/transfers')
def create_transfer_form(request: Request, shift_id: int = Form(...), to_employee_id: int = Form(...),
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(transfer_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    try:
        create_transfer_request(db, principal=principal, shift_id=shift_id, to_employee_id=to_employee_id)
        db.commit(); return _form_back('/v2/scheduling/my-schedule', message='Shift offer sent.')
    except (SchedulingValidationError, SchedulingConflict, PermissionError) as exc:
        db.rollback(); return _form_back('/v2/scheduling/my-schedule', error=str(exc))


@router.post('/my-schedule/transfers/{request_id}/respond')
def respond_transfer_form(request_id: int, request: Request, accept: bool = Form(...),
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(transfer_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    try:
        row = respond_to_transfer(db, principal=principal, request_id=request_id, accept=accept)
        db.commit(); message = ('Transfer completed.' if row.status == ShiftTransferStatus.COMPLETED
            else 'Manager approval required; the shift has not changed.' if row.status == ShiftTransferStatus.PENDING_MANAGER
            else 'Shift offer declined.')
        return _form_back('/v2/scheduling/my-schedule', message=message)
    except (SchedulingValidationError, SchedulingConflict, PermissionError) as exc:
        db.rollback(); return _form_back('/v2/scheduling/my-schedule', error=str(exc))


@router.get('/transfer-approvals')
def transfer_approvals_page(request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(transfer_approval_access), db: Session = Depends(get_db)):
    rows = list(db.execute(select(ShiftTransferRequest).where(
        ShiftTransferRequest.status == ShiftTransferStatus.PENDING_MANAGER)
        .order_by(ShiftTransferRequest.created_at)).scalars())
    employees = {row.id: row for row in db.execute(select(Employee)).scalars()}
    shifts = {row.id: row for row in db.execute(select(ScheduleShift).where(
        ScheduleShift.id.in_([r.shift_id for r in rows] or (-1,)))).scalars()}
    stores = {row.id: row for row in db.execute(select(Store)).scalars()}
    return request.app.state.templates.TemplateResponse('v2/scheduling/transfer_approvals.html', _simple_page_context(
        request, principal, page=V2Page('scheduling/transfer-approvals', 'Transfer Approvals',
        'Review shift transfers exceeding scheduled-hour thresholds.', route_path='/v2/scheduling/transfer-approvals',
        badge='Admin only', active_prefix='/v2/scheduling/transfer-approvals'), rows=rows,
        employees=employees, shifts=shifts, stores=stores))


@router.post('/transfer-approvals/{request_id}')
def review_transfer_form(request_id: int, request: Request, approve: bool = Form(...), note: str = Form(''),
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(transfer_approval_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    try:
        review_transfer(db, principal=principal, request_id=request_id, approve=approve, note=note)
        db.commit(); return _form_back('/v2/scheduling/transfer-approvals',
                                       message='Transfer approved.' if approve else 'Transfer rejected.')
    except (SchedulingValidationError, SchedulingConflict, PermissionError) as exc:
        db.rollback(); return _form_back('/v2/scheduling/transfer-approvals', error=str(exc))


@router.get('/week')
def week_board_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(board_access),
    db: Session = Depends(get_db),
):
    schedule_period_id = _requested_period_id(request)
    explicit_period = _authorize_explicit_period(
        db, principal=principal, schedule_period_id=schedule_period_id)
    week_start = explicit_period.week_start_date if explicit_period else _requested_week(request)
    scope = resolve_request_store_scope(request, db, principal)
    authorized = list_authorized_stores(db, principal)
    board = serialize_week_board(
        db,
        week_start=week_start,
        selected_store_ids=scope.store_ids,
        all_authorized_store_ids=tuple(row.id for row in authorized),
        permission_flags=getattr(request.state, 'permission_flags', {}) or {},
        schedule_period_id=schedule_period_id,
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
        'message': request.query_params.get('message', ''),
        'error': request.query_params.get('error', ''),
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


@router.post('/api/shifts/{shift_id}/attendance', status_code=201)
def record_attendance_api(
    shift_id: int,
    payload: AttendanceEventPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(attendance_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        shift = db.get(ScheduleShift, shift_id)
        if shift is None:
            raise SchedulingValidationError('Scheduled shift not found.')
        scope = resolve_request_store_scope(request, db, principal)
        if shift.store_id not in scope.store_ids:
            raise PermissionError('This shift is outside your authorized store scope.')
        outcome = record_attendance_event(
            db, principal=principal, shift_id=shift_id,
            event_type=payload.event_type, event_at=payload.event_at,
            replacement_employee_id=payload.replacement_employee_id,
            note=payload.note,
            override_store_restriction=payload.override_store_restriction,
            override_reason=payload.override_reason,
            today=datetime.now(tz=PORTAL_TIMEZONE).date(),
            ip=get_client_ip(request),
        )
        result = _success_response(
            db, request, principal,
            message='Attendance outcome recorded.',
            week_start=shift.shift_date,
            shift_id=shift.id,
        )
        result['attendance_event_id'] = outcome.event.id
        result['attendance_warnings'] = list(outcome.warnings)
        result['resulting_hours'] = (
            str(outcome.resulting_hours) if outcome.resulting_hours is not None else None)
        result['approval_threshold_hours'] = (
            str(outcome.approval_threshold_hours)
            if outcome.approval_threshold_hours is not None else None)
        db.commit()
        return result
    except (SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.post('/api/attendance/{event_id}/void')
def void_attendance_api(
    event_id: int,
    payload: AttendanceCorrectionPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(attendance_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        existing = db.get(ScheduleAttendanceEvent, event_id)
        if existing is None:
            raise SchedulingValidationError('Attendance event not found.')
        shift = db.get(ScheduleShift, existing.schedule_shift_id)
        scope = resolve_request_store_scope(request, db, principal)
        if shift.store_id not in scope.store_ids:
            raise PermissionError('This shift is outside your authorized store scope.')
        event = void_attendance_event(
            db, principal=principal, event_id=event_id,
            reason=payload.reason, ip=get_client_ip(request))
        result = _success_response(
            db, request, principal,
            message='Attendance outcome voided; audit history was preserved.',
            week_start=shift.shift_date,
            shift_id=shift.id,
        )
        db.commit()
        return result
    except (SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.get('/api/store-shifts')
def store_shifts_api(
    request: Request,
    include_inactive: bool = False,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(view_store_shift_access),
    db: Session = Depends(get_db),
):
    rows = _store_shift_list(db, request, principal, include_inactive=include_inactive)
    db.commit()
    return _store_shift_success(
        message='Store Shifts loaded.', store_shifts=rows,
        save_outcome=SaveOutcome.NOTHING_SAVED,
    )


@router.post('/api/store-shifts', status_code=201)
def create_store_shift_api(
    payload: StoreShiftPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(manage_store_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        row = create_store_shift(
            db, principal=principal, values=_store_shift_values(payload),
            allowed_store_ids=scope.store_ids, ip=get_client_ip(request),
        )
        result = _store_shift_success(
            message='Store Shift created.', store_shift_id=row.id,
            store_shifts=_store_shift_list(db, request, principal, include_inactive=True),
        )
        db.commit()
        return result
    except (SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.patch('/api/store-shifts/{store_shift_id}')
def update_store_shift_api(
    store_shift_id: int,
    payload: StoreShiftPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(manage_store_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        row = update_store_shift(
            db, principal=principal, store_shift_id=store_shift_id,
            values=_store_shift_values(payload), allowed_store_ids=scope.store_ids,
            ip=get_client_ip(request),
        )
        result = _store_shift_success(
            message='Store Shift updated.', store_shift_id=row.id,
            store_shifts=_store_shift_list(db, request, principal, include_inactive=True),
        )
        db.commit()
        return result
    except (SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.post('/api/store-shifts/{store_shift_id}/copy', status_code=201)
def copy_store_shift_api(
    store_shift_id: int,
    payload: StoreShiftCopyPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(manage_store_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        row = copy_store_shift(
            db, principal=principal, store_shift_id=store_shift_id,
            destination_store_id=payload.destination_store_id, label=payload.label,
            allowed_store_ids=scope.store_ids, ip=get_client_ip(request),
        )
        result = _store_shift_success(
            message='Store Shift copied.', store_shift_id=row.id,
            store_shifts=_store_shift_list(db, request, principal, include_inactive=True),
        )
        db.commit()
        return result
    except (SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


@router.post('/api/store-shifts/reorder')
def reorder_store_shifts_api(
    payload: StoreShiftReorderPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(manage_store_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        reorder_store_shifts(
            db, principal=principal, ordered_ids=tuple(payload.ordered_ids),
            allowed_store_ids=scope.store_ids, ip=get_client_ip(request),
        )
        result = _store_shift_success(
            message='Store Shifts reordered.',
            store_shifts=_store_shift_list(db, request, principal, include_inactive=True),
        )
        db.commit()
        return result
    except (SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback()
        return _error_response(exc)


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


@router.post('/api/periods/{schedule_period_id}/store-shifts/{store_shift_id}/place', status_code=201)
def place_store_shift_api(
    schedule_period_id: int,
    store_shift_id: int,
    payload: StoreShiftPlacementPayload,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(place_store_shift_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        current_board = _board(
            request, db, principal, week_start=normalize_week_start(payload.shift_date),
        )
        outcome = place_store_shift(
            db, principal=principal, schedule_period_id=schedule_period_id,
            store_shift_id=store_shift_id, expected_version=payload.expected_version,
            shift_date=payload.shift_date, employee_id=payload.employee_id,
            destination_store_id=payload.destination_store_id,
            allowed_store_ids=scope.store_ids,
            eligible_employee_ids=tuple(row['id'] for row in current_board['employees']),
            ip=get_client_ip(request),
        )
        response = _success_response(
            db, request, principal, message='Store Shift placed.',
            week_start=normalize_week_start(payload.shift_date), shift_id=outcome.shift_id,
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
                source_store_shift_id=source.source_store_shift_id,
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


@router.post('/api/periods/{schedule_period_id}/generate')
def generate_period_api(
    schedule_period_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(generate_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        result = regenerate_period(db, principal=principal, schedule_period_id=schedule_period_id)
        db.commit()
        return {'ok': True, **result}
    except (ValueError, SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.post('/api/shifts/{shift_id}/lock')
def lock_shift_api(
    shift_id: int, payload: LockPayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(edit_shift_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        row = set_manual_lock(db, principal=principal, shift_id=shift_id, locked=payload.locked, reason=payload.reason)
        db.commit()
        return {'ok': True, 'shift_id': row.id, 'manually_locked': row.manually_locked, 'lock_reason': row.lock_reason}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.post('/api/periods/{schedule_period_id}/publication-hold')
def publication_hold_api(
    schedule_period_id: int, payload: HoldPayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(automation_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        row = set_publication_hold(db, principal=principal, schedule_period_id=schedule_period_id,
                                   held=payload.held, reason=payload.reason)
        db.commit()
        return {'ok': True, 'schedule_period_id': row.id, 'publication_hold': row.publication_hold,
                'publication_hold_reason': row.publication_hold_reason}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.get('/api/own-schedule')
def own_schedule_api(
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(own_schedule_access),
    db: Session = Depends(get_db),
):
    employee = db.execute(select(Employee).where(Employee.principal_id == principal.id)).scalar_one_or_none()
    if employee is None:
        raise HTTPException(status_code=409, detail='Your account is not linked to an employee.')
    shifts = db.execute(select(ScheduleShift, SchedulePeriod).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee.id,
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
    ).order_by(ScheduleShift.shift_date, ScheduleShift.start_time)).all()
    incoming = db.execute(select(ShiftTransferRequest).where(
        ShiftTransferRequest.to_employee_id == employee.id).order_by(ShiftTransferRequest.created_at.desc())).scalars()
    return {
        'employee_id': employee.id,
        'assignments': [{'id': shift.id, 'store_id': shift.store_id, 'date': shift.shift_date.isoformat(),
                         'start_time': shift.start_time.isoformat(), 'end_time': shift.end_time.isoformat(),
                         'transfer_eligible': shift.shift_date > datetime.now(PORTAL_TIMEZONE).date()}
                        for shift, _period in shifts],
        'incoming_transfers': [{'id': row.id, 'shift_id': row.shift_id, 'from_employee_id': row.from_employee_id,
                                'status': row.status.value} for row in incoming],
    }


@router.post('/api/transfers', status_code=201)
def create_transfer_api(
    payload: TransferCreatePayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(transfer_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        row = create_transfer_request(db, principal=principal, shift_id=payload.shift_id,
                                      to_employee_id=payload.to_employee_id)
        db.commit(); return {'ok': True, 'request_id': row.id, 'status': row.status.value}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.post('/api/transfers/{request_id}/respond')
def respond_transfer_api(
    request_id: int, payload: TransferResponsePayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(transfer_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        row = respond_to_transfer(db, principal=principal, request_id=request_id, accept=payload.accept)
        db.commit(); return {'ok': True, 'request_id': row.id, 'status': row.status.value,
                             'existing_hours': row.existing_scheduled_hours,
                             'shift_hours': row.shift_hours, 'resulting_hours': row.resulting_scheduled_hours,
                             'threshold': row.approval_threshold_hours, 'amount_over': row.amount_over_threshold}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.post('/api/transfers/{request_id}/review')
def review_transfer_api(
    request_id: int, payload: TransferReviewPayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(transfer_approval_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        row = review_transfer(db, principal=principal, request_id=request_id,
                              approve=payload.approve, note=payload.note)
        db.commit(); return {'ok': True, 'request_id': row.id, 'status': row.status.value}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.put('/api/employees/{employee_id}/policy')
def employee_policy_api(
    employee_id: int, payload: EmployeePolicyPayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(preferences_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        row = upsert_employee_profile(db, principal=principal, employee_id=employee_id,
            home_store_id=payload.home_store_id,
            target_shifts_per_week=payload.target_shifts_per_week,
            week_a_workdays_mask=weekdays_to_mask(payload.week_a_workdays),
            week_b_workdays_mask=weekdays_to_mask(payload.week_b_workdays),
            target_weekly_hours=payload.target_weekly_hours,
            minimum_weekly_hours=payload.minimum_weekly_hours, maximum_weekly_hours=payload.maximum_weekly_hours,
            approval_weekly_hours=payload.approval_weekly_hours,
            max_consecutive_work_days=payload.max_consecutive_work_days,
            minimum_days_off_after_max_block=payload.minimum_days_off_after_max_block,
            special_store_participation=payload.special_store_participation,
            scheduler_note=payload.scheduler_note, active=payload.active, allowed_store_ids=scope.store_ids)
        db.commit(); return {'ok': True, 'employee_id': employee_id, 'policy_id': row.id}
    except (ValueError, SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.put('/api/employees/{employee_id}/store-preference')
def employee_store_preference_api(
    employee_id: int, payload: StorePreferencePayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(preferences_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        row = set_store_preference(db, principal=principal, employee_id=employee_id,
            store_id=payload.store_id, preference_rank=payload.preference_rank,
            preference_level=payload.preference_level, active=payload.active, allowed_store_ids=scope.store_ids)
        db.commit(); return {'ok': True, 'preference_id': row.id}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.put('/api/employees/{employee_id}/weekday-lockouts')
def employee_weekday_lockouts_api(
    employee_id: int, payload: WeekdayLockoutPayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(preferences_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        rows = set_full_day_weekday_lockouts(db, principal=principal, employee_id=employee_id,
                                             weekdays=tuple(payload.weekdays))
        db.commit(); return {'ok': True, 'employee_id': employee_id,
                             'weekdays': sorted(row.day_of_week for row in rows)}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.put('/api/automation-policy')
def automation_policy_api(
    payload: AutomationPolicyPayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(automation_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        row = update_organization_policy(db, principal=principal,
            weekly_approval_hours=payload.weekly_approval_hours,
            schedule_length_weeks=payload.schedule_length_weeks,
            generate_days_before_end=payload.generate_days_before_end,
            publish_days_before_end=payload.publish_days_before_end,
            publication_local_time=payload.publication_local_time, timezone_name=payload.timezone_name,
            active=payload.active)
        db.commit(); return {'ok': True, 'policy_id': row.id}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.post('/api/automation/run')
def run_automation_api(
    request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(automation_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    try:
        result = run_schedule_automation(db, principal=principal)
        db.commit(); return {'ok': True, **result}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)


@router.put('/api/special-store-policy')
def special_store_policy_api(
    payload: SpecialStorePayload, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(special_rotation_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf),
):
    try:
        scope = resolve_request_store_scope(request, db, principal)
        if payload.store_id not in scope.store_ids:
            raise PermissionError('The selected store is outside the authorized store scope.')
        row = configure_special_store(db, principal=principal, store_id=payload.store_id,
            primary_employee_ids=tuple(payload.primary_employee_ids),
            rotation_employee_ids=tuple(payload.rotation_employee_ids), active=payload.active)
        db.commit(); return {'ok': True, 'policy_id': row.id}
    except (SchedulingConflict, SchedulingValidationError, PermissionError, SQLAlchemyError) as exc:
        db.rollback(); return _error_response(exc)
