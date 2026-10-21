"""Coordinate existing approval/publication services without changing either gate."""
from sqlalchemy import select
from app.models import ScheduleApproval
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_service import (
    atomic_schedule_change, _lock_period, _require_draft, approve_schedule,
    publish_schedule, SchedulingValidationError,
)
from app.services.v2_scheduling_snapshot_service import verify_approved_content


@atomic_schedule_change
def approve_and_publish_schedule(db, *, principal, schedule_period_id,
                                 expected_version, allowed_store_ids,
                                 allow_serious_warnings=False, confirmed=False,
                                 override_reason='', ip=None):
    # The outer savepoint includes both services' approvals, snapshots and audit
    # rows. Neither service commits; the HTTP boundary commits once on success.
    if not principal.active or not principal_has_permission(
        db, principal=principal, permission_key='scheduling.publish',
        fallback_allowed=principal.role.value in ('ADMIN', 'MANAGER', 'LEAD'),
    ):
        raise PermissionError('Schedule publication permission is required.')
    period = _lock_period(db, schedule_period_id)
    _require_draft(period, expected_version=expected_version)
    approval = db.scalar(select(ScheduleApproval).where(
        ScheduleApproval.schedule_period_id == period.id,
        ScheduleApproval.version == period.version,
    ))
    valid_approval = False
    if approval is not None:
        try:
            verify_approved_content(db, period=period, approval=approval)
            valid_approval = True
        except SchedulingValidationError:
            pass  # Existing service preserves stale evidence when reapproving.
    if not valid_approval:
        approve_schedule(db, principal=principal, schedule_period_id=period.id,
                         expected_version=expected_version,
                         allowed_store_ids=allowed_store_ids,
                         allow_serious_warnings=allow_serious_warnings, confirmed=confirmed,
                         override_reason=override_reason)
    return publish_schedule(
        db, principal=principal, schedule_period_id=period.id,
        expected_version=period.version, allowed_store_ids=allowed_store_ids,
        allow_serious_warnings=allow_serious_warnings, confirmed=confirmed,
        override_reason=override_reason, ip=ip,
    )
