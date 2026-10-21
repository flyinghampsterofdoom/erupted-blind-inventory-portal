"""Final approval/publication gate over persisted assignments, never a repair."""
from sqlalchemy import select
from app.models import ScheduleWarning, ScheduleWarningSeverity


def validate_complete_schedule(db, *, period, allowed_store_ids, principal=None,
                               allow_serious_warnings=False, confirmed=False,
                               override_reason=''):
    from app.services.v2_scheduling_service import validate_publication_assignments, SchedulingValidationError
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    owner_override = False
    if confirmed:
        from app.auth import Role
        from app.services.access_control_service import principal_has_permission
        owner_override = bool(principal is not None and principal.active and principal.role == Role.ADMIN
            and allow_serious_warnings and principal_has_permission(db,
                principal=principal, permission_key='scheduling.publish_with_warnings', fallback_allowed=True))
        if owner_override and not override_reason.strip():
            raise SchedulingValidationError('Enter a reason for the Owner publication override.')
    assignment_exceptions = validate_publication_assignments(db, period=period,
        allowed_store_ids=allowed_store_ids, allow_policy_override=owner_override)
    rebuild_schedule_warnings(db, schedule_period_id=period.id)
    blocking = list(db.scalars(select(ScheduleWarning).where(
        ScheduleWarning.schedule_period_id==period.id,
        ScheduleWarning.severity==ScheduleWarningSeverity.SERIOUS,
        ScheduleWarning.warning_type!='HARD_UNAVAILABLE')))
    # HARD_UNAVAILABLE may have a previously authorized exact-assignment override;
    # validate_publication_assignments independently checks that evidence above.
    if owner_override:
        from types import SimpleNamespace
        return blocking + [SimpleNamespace(id=None, warning_type='ASSIGNMENT_POLICY_OVERRIDE', message=message)
                           for message in assignment_exceptions]
    if blocking:
        raise SchedulingValidationError('Resolve hard scheduling requirements and required coverage before approval/publication: '
            + '; '.join(row.message for row in blocking))

    return []
