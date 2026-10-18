"""Approved date-bounded preferences overlay, never destructive profile updates."""
from datetime import date, datetime, timezone
from sqlalchemy import select, or_
from app.models import Employee, Store, SchedulingPreferenceRequest
from app.auth import Role
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_rules_service import _audit
from app.services.v2_scheduling_service import SchedulingValidationError, SchedulingConflict


KEYS = {'work_days', 'off_days', 'preferred_stores', 'avoided_stores'}


def submit_request(db, *, principal, start_date, end_date, preferences):
    if not principal.active or not principal_has_permission(db, principal=principal,
        permission_key='scheduling.preferences.request', fallback_allowed=True):
        raise PermissionError('Preference request permission is required.')
    employee = db.scalar(select(Employee).where(Employee.principal_id == principal.id,
                                               Employee.active.is_(True)).with_for_update())
    if employee is None:
        raise PermissionError('A linked active employee is required.')
    if end_date is not None and end_date < start_date:
        raise SchedulingValidationError('Expiration must not precede the effective start.')
    if not isinstance(preferences, dict) or not preferences or set(preferences) - KEYS:
        raise SchedulingValidationError('Choose preferred work/off days or preferred/avoided stores.')
    for key, values in preferences.items():
        if not isinstance(values, list) or any(type(v) is not int for v in values) or len(values) != len(set(values)):
            raise SchedulingValidationError('Preference selections must be distinct integer choices.')
        if key.endswith('days') and any(not 0 <= day <= 6 for day in values):
            raise SchedulingValidationError('Choose valid Sunday–Saturday weekdays.')
        if key.endswith('stores'):
            active = set(db.scalars(select(Store.id).where(Store.active.is_(True), Store.id.in_(values))))
            if active != set(values):
                raise SchedulingValidationError('Choose active store locations.')
    if set(preferences.get('work_days', [])) & set(preferences.get('off_days', [])):
        raise SchedulingValidationError('A day cannot be both preferred work and preferred off.')
    if set(preferences.get('preferred_stores', [])) & set(preferences.get('avoided_stores', [])):
        raise SchedulingValidationError('A store cannot be both preferred and avoided.')
    row = SchedulingPreferenceRequest(employee_id=employee.id, requested_by_principal_id=principal.id,
        start_date=start_date, end_date=end_date, preferences=preferences, status='PENDING', reason='')
    db.add(row); db.flush()
    _audit(db, principal=principal, action='PREFERENCE_REQUESTED', entity_type='scheduling_preference_request',
        entity_id=row.id, after={'preferences': preferences, 'start_date': start_date.isoformat(),
                                 'end_date': end_date.isoformat() if end_date else None})
    return row


def decide_request(db, *, principal, request_id, decision, reason, today=None):
    if not principal.active or not principal_has_permission(db, principal=principal,
        permission_key='scheduling.preferences.decide', fallback_allowed=principal.role == Role.ADMIN):
        raise PermissionError('Administrator preference decision permission is required.')
    if decision not in ('APPROVED','DENIED','REVOKED') or not reason.strip() or len(reason) > 2000:
        raise SchedulingValidationError('Choose a decision and enter a reason of at most 2,000 characters.')
    # Lock the employee before the request; approvals for different requests serialize.
    initial = db.get(SchedulingPreferenceRequest, request_id)
    if initial is None:
        raise SchedulingValidationError('Preference request not found.')
    db.scalar(select(Employee).where(Employee.id == initial.employee_id).with_for_update())
    row = db.scalar(select(SchedulingPreferenceRequest).where(SchedulingPreferenceRequest.id == request_id)
        .with_for_update().execution_options(populate_existing=True))
    if (decision == 'REVOKED' and row.status != 'APPROVED') or (decision != 'REVOKED' and row.status != 'PENDING'):
        raise SchedulingConflict('This request already changed. Reload its history.')
    if decision == 'APPROVED':
        overlaps = list(db.scalars(select(SchedulingPreferenceRequest).where(
            SchedulingPreferenceRequest.employee_id == row.employee_id,
            SchedulingPreferenceRequest.status.in_(('APPROVED','REVOKED')),
            SchedulingPreferenceRequest.id != row.id,
            SchedulingPreferenceRequest.start_date <= (row.end_date or date.max),
            or_(SchedulingPreferenceRequest.end_date.is_(None), SchedulingPreferenceRequest.end_date >= row.start_date))))
        if any(set(r.preferences) & set(row.preferences) and (r.revoked_on is None or r.revoked_on > row.start_date)
               and ((r.end_date is None) == (row.end_date is None)) for r in overlaps):
            raise SchedulingValidationError('An approved preference overlaps these dates and categories. Revoke or change dates first.')
    before = {'status': row.status, 'reason': row.reason}
    row.status = decision; row.reason = reason.strip()
    row.decided_by_principal_id = principal.id; row.decided_at = datetime.now(timezone.utc)
    if decision == 'REVOKED':
        from app.services.v2_scheduling_lead_duty_service import business_today
        row.revoked_on = today or business_today(db)
    db.flush()
    _audit(db, principal=principal, action='PREFERENCE_DECIDED', entity_type='scheduling_preference_request',
        entity_id=row.id, before=before, after={'status': decision, 'revoked_on': str(row.revoked_on)}, reason=reason.strip())
    return row


def approved_preferences(db, *, employee_id, on_date):
    rows = db.scalars(select(SchedulingPreferenceRequest).where(
        SchedulingPreferenceRequest.employee_id == employee_id,
        SchedulingPreferenceRequest.status.in_(('APPROVED','REVOKED')),
        SchedulingPreferenceRequest.start_date <= on_date,
        or_(SchedulingPreferenceRequest.end_date.is_(None), SchedulingPreferenceRequest.end_date >= on_date),
        or_(SchedulingPreferenceRequest.revoked_on.is_(None), SchedulingPreferenceRequest.revoked_on > on_date)))
    result = {}
    for row in sorted(rows, key=lambda r: (r.end_date is not None, r.start_date, r.id)):
        result.update(row.preferences)
    return result
