"""Employee-owned requests. Approved leave changes only through management review."""
from datetime import datetime, timezone
from sqlalchemy import select
from app.auth import Role
from app.models import Employee, TimeOffRequest, TimeOffRequestStatus as Status, TimeOffReasonCategory, AuditLog
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_service import lock_scheduling, SchedulingConflict, SchedulingValidationError
from app.services.v2_scheduling_rules_service import create_time_off_request, _audit


def own_employee(db, principal):
    if not principal.active or not principal_has_permission(db, principal=principal,
            permission_key='scheduling.time_off.submit_own',
            fallback_allowed=principal.role in {Role.ADMIN,Role.MANAGER,Role.LEAD,Role.STORE}):
        raise PermissionError('Personal time-off access is not permitted.')
    employee = db.scalar(select(Employee).where(Employee.principal_id == principal.id, Employee.active.is_(True)))
    if employee is None:
        raise PermissionError('Your account must be linked to an active employee.')
    return employee


def request_values(row):
    return {key: (getattr(row, key).isoformat() if hasattr(getattr(row, key), 'isoformat') else getattr(row, key))
            for key in ('employee_id','start_date','end_date','full_day','start_time','end_time',
                        'reason_category_id','employee_note','revision','replaces_request_id','requests_cancellation')} | {'status': row.status.value}


def _validate(db, values, employee, *, allow_past_start=False, allow_inactive_reason=False):
    from app.services.v2_scheduling_lead_duty_service import business_today
    if values.employee_id != employee.id:
        raise PermissionError('You can only request your own time off.')
    if not allow_past_start and values.start_date < business_today(db):
        raise SchedulingValidationError('Choose today or a future start date.')
    if values.end_date < values.start_date:
        raise SchedulingValidationError('End date must be on or after start date.')
    if len(values.employee_note) > 2000:
        raise SchedulingValidationError('Keep the employee note to 2,000 characters.')
    if values.full_day and (values.start_time is not None or values.end_time is not None):
        raise SchedulingValidationError('Full-day requests cannot include times.')
    if not values.full_day and (values.start_date != values.end_date or values.start_time is None
            or values.end_time is None or values.end_time <= values.start_time):
        raise SchedulingValidationError('Partial-day requests require one date and a valid time range.')
    category = db.get(TimeOffReasonCategory, values.reason_category_id)
    if category is None or (not category.active and not allow_inactive_reason):
        raise SchedulingValidationError('Choose an active time-off reason.')


def save_own_request(db, *, principal, values=None, request_id=None, expected_revision=None,
                     action='save', note='', ip=None):
    lock_scheduling(db)
    employee = own_employee(db, principal)
    if action not in {'save','cancel','amend','request_cancel'}:
        raise SchedulingValidationError('Choose a valid request action.')
    row = None
    if request_id is not None:
        row = db.scalar(select(TimeOffRequest).where(TimeOffRequest.id == request_id).with_for_update().execution_options(populate_existing=True))
        if row is None or row.employee_id != employee.id:
            raise PermissionError('You can only change your own time-off requests.')
        if row.revision != expected_revision:
            raise SchedulingConflict('This request changed. Reload before saving.')
    elif action != 'save':
        raise SchedulingValidationError('Choose an existing request.')
    if action in {'amend','request_cancel'}:
        if row.status != Status.APPROVED:
            raise SchedulingConflict('Only approved requests use management-reviewed changes.')
        from app.services.v2_scheduling_lead_duty_service import business_today
        if row.end_date < business_today(db):
            raise SchedulingConflict('Historical approved time off cannot be changed here.')
        if not note.strip() or len(note) > 2000:
            raise SchedulingValidationError('Explain the requested change for management in up to 2,000 characters.')
        if db.scalar(select(TimeOffRequest.id).where(TimeOffRequest.replaces_request_id == row.id,
                TimeOffRequest.status == Status.PENDING)):
            raise SchedulingConflict('A change to this request is already pending.')
        if action == 'request_cancel':
            from app.services.v2_scheduling_rules_service import TimeOffInput
            values = TimeOffInput(employee.id, row.start_date, row.end_date, row.full_day,
                row.reason_category_id, row.start_time, row.end_time, note)
        _validate(db, values, employee, allow_past_start=action == 'request_cancel', allow_inactive_reason=action == 'request_cancel')
        amendment = create_time_off_request(db, principal=principal, values=values, management_entered=False,
            allow_inactive_reason=action == 'request_cancel', ip=ip)
        amendment.replaces_request_id = row.id
        amendment.requests_cancellation = action == 'request_cancel'
        amendment.employee_note = note.strip()
        db.flush()
        _audit(db, principal=principal, action='TIME_OFF_CHANGE_REQUESTED', entity_type='time_off_request',
            entity_id=amendment.id, after=request_values(amendment), metadata={'original_request_id':row.id}, ip=ip)
        return amendment
    if row is not None and row.status != Status.PENDING:
        raise SchedulingConflict('Only pending requests may be edited or cancelled. Request management review for approved PTO.')
    if row is None:
        _validate(db, values, employee)
        return create_time_off_request(db, principal=principal, values=values, management_entered=False, ip=ip)
    before = request_values(row)
    if action == 'cancel':
        row.status = Status.CANCELLED
    else:
        _validate(db, values, employee)
        if row.requests_cancellation:
            raise SchedulingConflict('Cancel this pending cancellation request and submit a new change.')
        for key in ('start_date','end_date','full_day','start_time','end_time','reason_category_id','employee_note'):
            setattr(row, key, getattr(values, key))
    row.revision += 1
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by_principal_id = principal.id
    db.flush()
    _audit(db, principal=principal, action='TIME_OFF_CANCELLED' if action == 'cancel' else 'TIME_OFF_EDITED',
        entity_type='time_off_request', entity_id=row.id, before=before, after=request_values(row), ip=ip)
    return row


def own_history(db, request_id):
    return list(db.scalars(select(AuditLog).where(AuditLog.action.like('V2:SCHEDULING:TIME_OFF%'),
        AuditLog.meta['entity_type'].as_string() == 'time_off_request',
        AuditLog.meta['entity_id'].as_string() == str(request_id)).order_by(AuditLog.id)))
