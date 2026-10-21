"""Final approval/publication gate over persisted assignments, never a repair."""
from sqlalchemy import select
from app.models import ScheduleShift, ScheduleWarning, ScheduleWarningSeverity


def validate_complete_schedule(db, *, period, allowed_store_ids):
    from app.services.v2_scheduling_service import validate_publication_assignments, SchedulingValidationError
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    from app.services.v2_scheduling_roster_service import list_scheduling_candidates
    from app.services.v2_scheduling_lifecycle_service import eligible_during
    from app.services.v2_scheduling_allocation_service import weekly_allocation
    validate_publication_assignments(db, period=period, allowed_store_ids=allowed_store_ids)
    rebuild_schedule_warnings(db, schedule_period_id=period.id)
    blocking = list(db.scalars(select(ScheduleWarning).where(
        ScheduleWarning.schedule_period_id==period.id,
        ScheduleWarning.severity==ScheduleWarningSeverity.SERIOUS,
        ScheduleWarning.warning_type!='HARD_UNAVAILABLE')))
    # HARD_UNAVAILABLE may have a previously authorized exact-assignment override;
    # validate_publication_assignments independently checks that evidence above.
    if blocking:
        raise SchedulingValidationError('Resolve hard scheduling requirements and required coverage before approval/publication: '
            + '; '.join(row.message for row in blocking))
