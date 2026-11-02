"""Coordinate existing approval/publication services without changing either gate."""
from sqlalchemy import select
from app.models import ScheduleApproval, SchedulePeriodStatus
from app.services.v2_scheduling_service import (
    atomic_schedule_change, _require_draft, approve_schedule,
    publish_schedule, publication_state, SchedulingValidationError,
)
from app.services.v2_scheduling_snapshot_service import verify_approved_content


@atomic_schedule_change
def approve_and_publish_schedule(db, *, principal, schedule_period_id,
                                 expected_version, allowed_store_ids,
                                 allow_serious_warnings=False, confirmed=False,
                                 override_reason='', override_pto=False, ip=None):
    # The outer savepoint includes both services' approvals, snapshots and audit
    # rows. Neither service commits; the HTTP boundary commits once on success.
    period = publication_state(db, principal=principal, schedule_period_id=schedule_period_id,
                               allowed_store_ids=allowed_store_ids)
    # The immutable period ID is the retry identity, even after supersession.
    # Return before validation/reapproval so overrides and history are not repeated.
    if period.published_at is not None and period.status in (SchedulePeriodStatus.PUBLISHED, SchedulePeriodStatus.ARCHIVED):
        return period
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
                         override_reason=override_reason,override_pto=override_pto)
    return publish_schedule(
        db, principal=principal, schedule_period_id=period.id,
        expected_version=period.version, allowed_store_ids=allowed_store_ids,
        allow_serious_warnings=allow_serious_warnings, confirmed=confirmed,
        override_reason=override_reason,override_pto=override_pto, ip=ip,
    )
