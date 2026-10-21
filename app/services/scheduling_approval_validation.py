"""Final approval/publication gate over persisted assignments, never a repair."""
from sqlalchemy import select
from app.models import ScheduleWarning, ScheduleWarningSeverity


def validate_complete_schedule(db, *, period, allowed_store_ids, principal=None,
                               allow_serious_warnings=False, confirmed=False,
                               override_reason='', override_pto=False):
    from app.services.v2_scheduling_service import validate_publication_assignments, SchedulingValidationError
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    owner_override = False
    if confirmed:
        from app.auth import Role
        from app.services.scheduling_operational_exception_service import can_override
        from app.services.access_control_service import principal_has_permission
        owner_override = bool(principal is not None and principal.active and principal.role == Role.ADMIN and can_override(db,principal)
            and allow_serious_warnings and principal_has_permission(db,
                principal=principal, permission_key='scheduling.publish_with_warnings', fallback_allowed=True))
        if owner_override and not override_reason.strip():
            raise SchedulingValidationError('Enter a reason for the Owner publication override.')
    assignment_exceptions = validate_publication_assignments(db, period=period,
        allowed_store_ids=allowed_store_ids, allow_policy_override=owner_override,override_pto=override_pto)
    rebuild_schedule_warnings(db, schedule_period_id=period.id)
    blocking = list(db.scalars(select(ScheduleWarning).where(
        ScheduleWarning.schedule_period_id==period.id,
        ScheduleWarning.severity==ScheduleWarningSeverity.SERIOUS,
        ScheduleWarning.warning_type!='HARD_UNAVAILABLE')))
    from app.models import ScheduleShift,Employee
    from app.services.scheduling_operational_exception_service import authorized_assignment_codes
    rows=list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id==period.id)))
    codes={s.id:authorized_assignment_codes(db,s) for s in rows}
    blocking=[w for w in blocking if not (w.shift_id and w.warning_type in codes.get(w.shift_id,set()))]
    # A separately authorized PTO exception does not remove a real qualification.
    # Require exactly one designated, qualified person; never waive missing qualification.
    def covered_lead(w):
        if w.warning_type!='NO_LEAD_OF_DAY':return False
        designated=[s for s in rows if s.shift_date==w.warning_date and s.is_lead_of_day
                    and s.employee_id and db.get(Employee,s.employee_id).scheduling_lead_capable]
        return len(designated)==1 and (owner_override and override_pto or
            'APPROVED_TIME_OFF' in codes.get(designated[0].id,set()))
    blocking=[w for w in blocking if not covered_lead(w)]
    # HARD_UNAVAILABLE may have a previously authorized exact-assignment override;
    # validate_publication_assignments independently checks that evidence above.
    if owner_override:
        from app.services.scheduling_operational_exception_service import OPERATIONAL_CODES
        hard=[w for w in blocking if w.warning_type not in OPERATIONAL_CODES and
              not (w.warning_type=='APPROVED_TIME_OFF' and override_pto)]
        if hard:raise SchedulingValidationError('Cannot override: '+'; '.join(w.message for w in hard))
        from types import SimpleNamespace
        return blocking + [SimpleNamespace(id=None, warning_type='ASSIGNMENT_POLICY_OVERRIDE', message=message)
                           for message in assignment_exceptions]
    if blocking:
        raise SchedulingValidationError('Resolve hard scheduling requirements and required coverage before approval/publication: '
            + '; '.join(row.message for row in blocking))

    return []
