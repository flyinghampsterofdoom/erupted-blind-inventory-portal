"""One-action operational exceptions. Unknown, eligibility and rest rules fail closed."""
from app.auth import Role
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_service import SchedulingValidationError

OPERATIONAL_CODES = frozenset({'MAX_WEEKLY_HOURS','MAX_CONSECUTIVE_DAYS',
    'HARD_WEEKDAY_LOCKOUT','STORE_NEVER','LONGVIEW_NOT_PARTICIPATING',
    'SPECIAL_STORE_PRIMARY_ONLY','HOURS_APPROVAL','WEEKLY_ALLOCATION',
    'LONGVIEW_WEEKLY_TRAVELER_UNSATISFIED'})
PTO_CODES = frozenset({'APPROVED_TIME_OFF'})


def can_override(db, principal):
    return bool(principal and principal.active and principal.role in (Role.ADMIN, Role.LEAD) and
        principal_has_permission(db, principal=principal,
            permission_key='scheduling.override_operational', fallback_allowed=True))


def authorize(db, principal, *, confirmed, reason, pto=False):
    if not confirmed or not can_override(db, principal):
        raise PermissionError('An explicit scheduler-authorized operational exception is required.')
    if not reason.strip():
        raise SchedulingValidationError('Enter a reason for this specific operational exception.')
    if len(reason)>2000:raise SchedulingValidationError('Keep the override reason to 2,000 characters.')


def validate_reasons(db, principal, reasons, *, confirmed=False, reason='', override_pto=False):
    reasons=list(reasons)
    hard=[r for r in reasons if r.code not in OPERATIONAL_CODES | PTO_CODES]
    if hard:
        raise SchedulingValidationError('Cannot override: ' + '; '.join(r.message for r in hard))
    if reasons:
        if not confirmed:
            raise SchedulingValidationError('Scheduling exception required: ' + '; '.join(r.message for r in reasons))
        authorize(db,principal,confirmed=confirmed,reason=reason)
        if any(r.code in PTO_CODES for r in reasons) and not override_pto:
            raise SchedulingValidationError('Approved PTO remains protected. Explicitly confirm the PTO exception.')
    elif confirmed:
        authorize(db,principal,confirmed=confirmed,reason=reason)
    return [{'code':r.code,'message':r.message} for r in reasons]


def authorized_assignment_codes(db,shift):
    """An exact prior action may retain its exception through publication."""
    from sqlalchemy import select
    from app.models import AuditLog
    from app.services.v2_scheduling_exception_service import shift_lineage
    from app.services.v2_scheduling_service import _shift_values
    evidence=db.scalar(select(AuditLog).where(AuditLog.action.in_(
        ('V2:SCHEDULING:SHIFT_CREATED','V2:SCHEDULING:SHIFT_CHANGED','V2:SCHEDULING:SHIFT_REQUEST_ASSIGNMENT_CHANGED')),
        AuditLog.meta['entity_id'].as_string().in_([str(r.id) for r in shift_lineage(db,shift)]),
        AuditLog.meta['entity_type'].as_string()=='schedule_shift').order_by(AuditLog.id.desc()).limit(1))
    data=evidence.meta if evidence else {};after=data.get('after') or {}
    exact=data.get('reason') and all(after.get(k)==_shift_values(shift)[k] for k in
        ('employee_id','store_id','shift_date','start_time','end_time','unpaid_break_minutes'))
    return {r['code'] for r in data.get('metadata',{}).get('operational_exceptions',[])} if exact else set()


def assignment_reasons_remaining(db,shift,reasons):
    codes=set(db.info.get('authorized_action_exceptions',{}).get(shift.id,()))
    if shift.manually_locked:codes |= authorized_assignment_codes(db,shift)
    return [r for r in reasons if r.code not in codes or r.code not in OPERATIONAL_CODES | PTO_CODES]


def reconcile_authorized_assignment(db,*,schedule_period_id,shift_id,exceptions):
    from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
    previous=db.info.get('authorized_action_exceptions')
    db.info['authorized_action_exceptions']={**(previous or {}),shift_id:{r['code'] for r in exceptions}}
    try:return reconcile_lead_designations(db,schedule_period_id=schedule_period_id)
    finally:
        if previous is None:db.info.pop('authorized_action_exceptions',None)
        else:db.info['authorized_action_exceptions']=previous
