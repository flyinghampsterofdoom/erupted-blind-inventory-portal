"""Administrative retirement of schedules; referenced evidence is never deleted."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.auth import Role
from app.models import (Employee, ScheduleLifecycleStage,
                        SchedulePeriod, SchedulePeriodStatus, ScheduleShift, Store)
from app.services.v2_scheduling_service import (SchedulingConflict, SchedulingValidationError,
                                               lock_scheduling)
from app.services.v2_scheduling_policy_service import organization_policy
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def require_admin(principal):
    # Capability overrides must never extend this destructive operation to other roles.
    if not principal.active or principal.role != Role.ADMIN:
        raise PermissionError('Only an active Admin may clear schedules.')


def _state(db, principal, kind, now):
    require_admin(principal)
    if kind not in ('pending', 'confirmed'):
        raise SchedulingValidationError('Choose pending or confirmed schedules.')
    lock_scheduling(db)
    zone = ZoneInfo(organization_policy(db).timezone_name)
    today = now.astimezone(zone).date()
    status = SchedulePeriodStatus.DRAFT if kind == 'pending' else SchedulePeriodStatus.PUBLISHED
    periods = list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.week_end_date >= today, SchedulePeriod.status == status)
        .order_by(SchedulePeriod.id).with_for_update().execution_options(populate_existing=True)))
    shifts = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id.in_([p.id for p in periods]))
        .order_by(ScheduleShift.id).with_for_update().execution_options(populate_existing=True)))
    from app.services.v2_scheduling_exception_service import expected_employee_id
    from app.services.inventory_working_store_service import scheduled_store
    today_stores = {}
    people = {s.employee_id for s in shifts if s.employee_id is not None}
    if kind == 'confirmed':
        for shift in shifts:
            if shift.shift_date != today:
                continue
            employee_id = expected_employee_id(db, shift)
            if employee_id is not None:
                people.add(employee_id)
                today_stores.setdefault(employee_id, set()).add(shift.store_id)
    employees = list(db.scalars(select(Employee).where(Employee.id.in_(people))
        .order_by(Employee.id).with_for_update().execution_options(populate_existing=True)))
    grants = []  # Retained historical grants never provide operational authority.
    versions = {e.id: e.working_context_version for e in employees}
    access = []
    for employee in employees:
        stores = today_stores.get(employee.id, set())
        if employee.active and employee.scheduling_active and employee.principal_id and stores:
            normal = scheduled_store(db, employee, now=now)
            if normal is None or normal not in stores:
                continue
            store = db.get(Store, normal)
            if store.active:
                access.append({'employee_id': employee.id, 'store_id': store.id, 'store_name': store.name})
    state = {'kind': kind, 'business_date': today.isoformat(),
        'periods': [{'id': p.id, 'version': p.version, 'week_start': p.week_start_date.isoformat(),
                     'week_end': p.week_end_date.isoformat()} for p in periods],
        'shifts': [{c.key: str(getattr(s, c.key)) for c in ScheduleShift.__table__.columns} for s in shifts],
        'versions': versions if kind == 'confirmed' else {},
        'grants': [{'id': g.id, 'store_id': g.store_id, 'expires_at': g.expires_at.isoformat()} for g in grants],
        'access': access}
    fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    preview = {'kind': kind, 'business_date': today.isoformat(), 'period_count': len(periods), 'assignment_count': len(shifts),
        'periods': state['periods'], 'access': access, 'retained_grant_count': len(grants),
        'fingerprint': fingerprint, 'blocked_by_working_access': bool(access)}
    return preview, periods, employees, grants, zone


def preview_clear(db, *, principal, kind, now=None):
    return _state(db, principal, kind, now or datetime.now(timezone.utc))[0]


def clear_schedules(db, *, principal, kind, fingerprint, confirmation,
                    now=None, ip=None):
    require_admin(principal)
    if confirmation != 'CLEAR':
        raise SchedulingValidationError('Type CLEAR exactly to confirm.')
    supplied_now = now
    lock_scheduling(db)
    now = now or datetime.now(timezone.utc)
    # A savepoint protects callers which catch errors and then commit their session.
    with db.begin_nested():
        preview, periods, employees, grants, zone = _state(db, principal, kind, now)
        if fingerprint != preview['fingerprint']:
            raise SchedulingConflict('Schedules or store access changed. Review a fresh preview before clearing.')
        if preview['blocked_by_working_access']:
            raise SchedulingValidationError(
                "Cannot clear published schedules providing today's employee inventory access. "
                "Review again on the next operating day; temporary access overrides are no longer supported.")
        if not periods:
            raise SchedulingConflict('No schedules remain to clear. Review a fresh preview.')
        if supplied_now is None:
            now = datetime.now(timezone.utc)
            if now.astimezone(zone).date().isoformat() != preview['business_date'] or any(
                    grant.expires_at <= now for grant in grants):
                raise SchedulingConflict('The business day or temporary access changed. Review a fresh preview.')
        grant_ids = []
        if kind == 'confirmed':
            for employee in employees:
                employee.working_context_version += 1
            db.flush()
        selected_ids = {p.id for p in periods}
        retained = []
        invalidated_draft_ids = []
        if kind == 'confirmed':
            weeks = {p.week_start_date for p in periods}
            # Cancelled future intent on older published revisions cannot later accrue
            # presumptive fairness credit. Keep all exact attendance/swap references.
            retained = list(db.scalars(select(SchedulePeriod).where(
                SchedulePeriod.week_start_date.in_(weeks),
                SchedulePeriod.status == SchedulePeriodStatus.ARCHIVED,
                SchedulePeriod.published_at.is_not(None), SchedulePeriod.cleared_at.is_(None))
                .order_by(SchedulePeriod.id).with_for_update().execution_options(populate_existing=True)))
            # Pending revisions stay pending, but previously approved copies must be reviewed again.
            for draft in db.scalars(select(SchedulePeriod).where(
                    SchedulePeriod.week_start_date.in_(weeks), SchedulePeriod.status == SchedulePeriodStatus.DRAFT)
                    .with_for_update().execution_options(populate_existing=True)):
                invalidated_draft_ids.append(draft.id)
                draft.version += 1
                draft.updated_by_principal_id = principal.id
                draft.updated_at = now
        for period in periods + retained:
            period.status = SchedulePeriodStatus.ARCHIVED
            period.lifecycle_stage = ScheduleLifecycleStage.CLOSED
            period.cleared_at = now
            period.automatic_publication_at = None
            period.version += 1
            period.updated_at = now
            period.updated_by_principal_id = principal.id
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id, action='SCHEDULES_CLEARED', domain='SCHEDULING',
            entity_type='schedule_reset', entity_id=preview['fingerprint'], timestamp=now,
            before={k: preview[k] for k in ('kind', 'period_count', 'assignment_count', 'periods', 'access')},
            after={'retired_period_ids': sorted(selected_ids),
                   'retained_prior_revision_ids': [p.id for p in retained],
                   'invalidated_draft_ids': invalidated_draft_ids,
                   'temporary_inventory_grant_ids': grant_ids, 'automatically_generated': False},
            reason='Admin typed CLEAR',
        ), ip=ip)
        db.flush()
        return preview
