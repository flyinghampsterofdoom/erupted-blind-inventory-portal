"""Atomic Lead-attested changes using existing draft/publication lineage."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4
from sqlalchemy import select
from app.models import Employee, SchedulePeriod, SchedulePeriodStatus, ScheduleShift
from app.auth import Role
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_service import (SchedulingValidationError, SchedulingConflict,
    clone_published_revision, validate_publication_assignments, approve_schedule, publish_schedule, _shift_values)
from app.services.v2_scheduling_rules_service import _audit
from app.services.v2_scheduling_policy_service import organization_policy, SCHEDULE_AUTOMATION_LOCK_KEY


def _change_assignments(db, *, principal, shift_id, other_shift_id=None,
                       replacement_employee_id=None, kind='SHIFT', reason='',
                       expected_versions=None, allowed_store_ids=(), now=None,
                       operational_override=False,override_pto=False):
    if not principal.active or not principal_has_permission(db, principal=principal,
        permission_key='scheduling.swap', fallback_allowed=principal.role in (Role.ADMIN, Role.MANAGER, Role.LEAD)):
        raise PermissionError('Assignment change permission is required.')
    if operational_override:
        from app.services.scheduling_operational_exception_service import authorize
        authorize(db,principal,confirmed=True,reason=reason)
    if kind not in ('SHIFT', 'STORE') or not reason.strip() or len(reason) > 2000:
        raise SchedulingValidationError('Choose Shift Swap or Store Swap and enter a reason.')
    if (other_shift_id is None) == (replacement_employee_id is None):
        raise SchedulingValidationError('Choose another assignment or a replacement employee.')
    if kind == 'STORE' and other_shift_id is None:
        raise SchedulingValidationError('Store Swap requires two assignments.')
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(organization_policy(db).timezone_name))
    with db.begin_nested():
        from sqlalchemy import func
        db.scalar(select(func.pg_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY)))
        ids = sorted({shift_id, other_shift_id} - {None})
        if len(ids) != (2 if other_shift_id else 1):
            raise SchedulingValidationError('Choose distinct assignments.')
        original = list(db.scalars(select(ScheduleShift).where(ScheduleShift.id.in_(ids)).order_by(ScheduleShift.id)))
        if len(original) != len(ids) or any(s.employee_id is None for s in original):
            raise SchedulingValidationError('Choose assigned shifts.')
        if any(s.store_id not in allowed_store_ids for s in original):
            raise PermissionError('Assignments are outside your store scope.')
        if any(s.shift_date < local.date() or (s.shift_date == local.date() and s.end_time <= local.time()) for s in original):
            raise SchedulingValidationError('Use attendance correction for completed shifts.')
        if kind == 'STORE' and (original[0].shift_date != original[1].shift_date or original[0].employee_id == original[1].employee_id):
            raise SchedulingValidationError('Store Swap requires different employees on the same day.')
        people = sorted({s.employee_id for s in original} | ({replacement_employee_id} if replacement_employee_id else set()))
        from app.services.inventory_working_store_service import lock_employee
        for eid in people:
            if lock_employee(db, eid) is None:
                raise SchedulingValidationError('Employee not found.')
        periods = list(db.scalars(select(SchedulePeriod).where(
            SchedulePeriod.id.in_({s.schedule_period_id for s in original})).order_by(SchedulePeriod.id).with_for_update().execution_options(populate_existing=True)))
        drafts, was_published = {}, set()
        expected_versions = expected_versions or {}
        for period in periods:
            if str(period.id) not in expected_versions or expected_versions[str(period.id)] != period.version:
                raise SchedulingConflict('Schedule changed. Reload before swapping.')
            if period.status == SchedulePeriodStatus.PUBLISHED:
                if any(not principal_has_permission(db, principal=principal, permission_key=key, fallback_allowed=principal.role in (Role.ADMIN, Role.MANAGER, Role.LEAD)) for key in ('scheduling.modify_published', 'scheduling.approve', 'scheduling.publish')):
                    raise PermissionError('Published swaps require revision, approval and publication permission.')
                drafts[period.id] = clone_published_revision(db, principal=principal,
                    published_period_id=period.id, allowed_store_ids=allowed_store_ids)
                was_published.add(period.id)
            elif period.status == SchedulePeriodStatus.DRAFT:
                drafts[period.id] = period
            else:
                raise SchedulingConflict('Choose the current schedule revision.')
        targets = {}
        for source in original:
            target = source if source.schedule_period_id not in was_published else db.scalar(select(ScheduleShift).where(
                ScheduleShift.schedule_period_id == drafts[source.schedule_period_id].id,
                ScheduleShift.source_shift_id == source.id))
            targets[source.id] = target
        first = targets[shift_id]
        second = targets.get(other_shift_id)
        before = {s.id: _shift_values(s) for s in targets.values()}
        if kind == 'STORE':
            first.store_id, second.store_id = second.store_id, first.store_id
        elif second:
            first.employee_id, second.employee_id = second.employee_id, first.employee_id
        else:
            if first.employee_id == replacement_employee_id:
                raise SchedulingValidationError('Choose a different replacement employee.')
            first.employee_id = replacement_employee_id
        for target in targets.values():
            target.manually_locked = True
            target.locked_by_principal_id = principal.id
            target.locked_at = now
            target.lock_reason = reason.strip()
            target.updated_by_principal_id = principal.id
        db.flush()
        correlation = str(uuid4())
        db.info["scheduling_swap_periods"] = {d.week_start_date: d for d in drafts.values()}
        for draft in drafts.values():
            draft.version += 1
            validate_publication_assignments(db, period=draft, allowed_store_ids=allowed_store_ids,
                allow_policy_override=operational_override,override_pto=override_pto)
        # Validate the joint final state before publishing either half.
        for draft in drafts.values():
            approve_schedule(db, principal=principal, schedule_period_id=draft.id,
                expected_version=draft.version, allowed_store_ids=allowed_store_ids,
                allow_serious_warnings=operational_override,confirmed=operational_override,
                override_reason=reason,override_pto=override_pto)
        for source in periods:
            draft = drafts[source.id]
            if source.id in was_published:
                publish_schedule(db, principal=principal, schedule_period_id=draft.id,
                    expected_version=draft.version, allowed_store_ids=allowed_store_ids,
                allow_serious_warnings=operational_override,confirmed=operational_override,
                override_reason=reason,override_pto=override_pto)
        for eid in people:
            db.get(Employee, eid).working_context_version += 1
        for target in targets.values():
            _audit(db, principal=principal, action='STORE_SWAPPED' if kind == 'STORE' else 'SHIFT_SWAPPED',
                entity_type='schedule_shift', entity_id=target.id, reason=reason.strip(),
                before=before[target.id], after=_shift_values(target), correlation_id=correlation,
                metadata={'effective_at': now.isoformat(), 'original_shift_ids': ids,
                          'operational_override':operational_override,'override_pto':override_pto})
        db.info.pop("scheduling_swap_periods", None)
        db.flush()
        return {'period_ids': [d.id for d in drafts.values()], 'correlation_id': correlation}


def change_assignments(db, **kwargs):
    try:
        return _change_assignments(db, **kwargs)
    finally:
        db.info.pop('scheduling_swap_periods', None)
