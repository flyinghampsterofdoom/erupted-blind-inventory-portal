"""Prepare existing revision workflows for a departing employee, atomically."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from sqlalchemy import select
from app.models import (Employee, SchedulePeriod, SchedulePeriodStatus, ScheduleShift,
                        SchedulingEligibilityPeriod, Store)
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_service import (atomic_schedule_change, clone_published_revision,
    update_shift, ShiftInput, SchedulingValidationError, SchedulingConflict)
from app.services.v2_scheduling_lifecycle_service import within_employment_dates
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def _authorize(db, principal):
    for key in ('scheduling.edit_draft_shifts', 'scheduling.modify_published'):
        if not principal.active or not principal_has_permission(db, principal=principal,
                permission_key=key, fallback_allowed=principal.role.value in ('ADMIN', 'MANAGER', 'LEAD')):
            raise PermissionError('Scheduling edit and revision permissions are required.')


def preview_departure(db, *, principal, employee_id, final_day, allowed_store_ids):
    _authorize(db, principal)
    person = db.get(Employee, employee_id)
    if person is None:
        raise SchedulingValidationError('Choose an existing employee.')
    if not within_employment_dates(person, final_day):
        raise SchedulingValidationError('Choose a final workday within current scheduling eligibility.')
    if any(p.start_date and p.start_date > final_day for p in person.eligibility_periods):
        raise SchedulingValidationError('A future return is already recorded. Review eligibility dates before preparing departure coverage.')
    from app.services.v2_scheduling_lead_duty_service import business_today
    first_day = max(final_day + timedelta(days=1), business_today(db))
    periods = list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
        SchedulePeriod.is_historical.is_(False), SchedulePeriod.week_end_date >= first_day
    ).order_by(SchedulePeriod.revision_number, SchedulePeriod.id)))
    # One operational plan per week; an existing draft already supersedes its publication.
    by_week = {}
    for p in periods:
        old = by_week.get(p.week_start_date)
        if old is None or p.status == SchedulePeriodStatus.DRAFT or old.status != SchedulePeriodStatus.DRAFT:
            by_week[p.week_start_date] = p
    selected = list(by_week.values())
    shifts = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id.in_([p.id for p in selected]),
        ScheduleShift.employee_id == employee_id, ScheduleShift.shift_date >= first_day
    ).order_by(ScheduleShift.shift_date, ScheduleShift.store_id, ScheduleShift.id)))
    # Never silently miss out-of-scope work when ending company-wide eligibility.
    if any(s.store_id not in allowed_store_ids for s in shifts):
        raise PermissionError('Affected shifts include stores outside your authorized scope.')
    state = {'employee': person.id, 'final_day': str(final_day),
             'eligibility': [(p.id, str(p.start_date), str(p.end_date)) for p in person.eligibility_periods],
             'legacy_end': str(person.last_effective_date),
             'shifts': [(s.id, s.schedule_period_id, s.store_id, str(s.shift_date), str(s.start_time), str(s.end_time),
                         db.get(SchedulePeriod, s.schedule_period_id).version) for s in shifts]}
    fingerprint = sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return {'employee': person, 'shifts': shifts, 'fingerprint': fingerprint,
            'stores': {s.id: s.name for s in db.scalars(select(Store))}, 'final_day': final_day}


@atomic_schedule_change
def prepare_departure(db, *, principal, employee_id, final_day, allowed_store_ids, fingerprint, reason):
    if not reason.strip() or len(reason) > 2000:
        raise SchedulingValidationError('Enter a departure reason of 1–2,000 characters.')
    db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    preview = preview_departure(db, principal=principal, employee_id=employee_id,
        final_day=final_day, allowed_store_ids=allowed_store_ids)
    if fingerprint != preview['fingerprint']:
        raise SchedulingConflict('The schedule or eligibility changed. Preview affected shifts again.')
    person = preview['employee']
    before = {'last_effective_date': str(person.last_effective_date),
              'periods': [(p.id, str(p.start_date), str(p.end_date)) for p in person.eligibility_periods]}
    revisions, affected = {}, []
    for source in preview['shifts']:
        period = db.get(SchedulePeriod, source.schedule_period_id)
        if period.id not in revisions:
            target = clone_published_revision(db, principal=principal, published_period_id=period.id,
                allowed_store_ids=allowed_store_ids) if period.status == SchedulePeriodStatus.PUBLISHED else period
            revisions[period.id] = target
        target = revisions[period.id]
        row = source if target.id == period.id else db.scalar(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == target.id, ScheduleShift.source_shift_id == source.id))
        if row.manually_locked:
            from app.services.v2_scheduling_policy_service import set_manual_lock
            set_manual_lock(db, principal=principal, shift_id=row.id, locked=False, reason=reason)
        values = ShiftInput(None, row.store_id, row.shift_date, row.start_time, row.end_time,
            unpaid_break_minutes=row.unpaid_break_minutes, shift_type_id=row.shift_type_id,
            is_opener=row.is_opener, is_closer=row.is_closer, employee_note=row.employee_note or '')
        update_shift(db, principal=principal, schedule_period_id=target.id, shift_id=row.id,
            expected_version=target.version, values=values, allowed_store_ids=allowed_store_ids,
            manual_edit=True, override_reason=reason)
        affected.append(row.id)
    current = next((p for p in person.eligibility_periods if
        (p.start_date is None or p.start_date <= final_day) and (p.end_date is None or final_day <= p.end_date)), None)
    if current:
        current.end_date = final_day
        current.updated_by_principal_id = principal.id
        current.updated_at = datetime.now(timezone.utc)
    elif not person.eligibility_periods:
        person.eligibility_periods.append(SchedulingEligibilityPeriod(employee_id=person.id,
            start_date=None, end_date=final_day, legacy_unknown_start=True, created_by_principal_id=principal.id))
    db.flush()
    write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=principal.id,
        action='EMERGENCY_COVERAGE_PREPARED', domain='SCHEDULING', entity_type='employee',
        entity_id=person.id, timestamp=datetime.now(timezone.utc), reason=reason,
        before=before, after={'final_available_day': str(final_day)}, store_ids=tuple(allowed_store_ids),
        metadata={'open_shift_ids': affected, 'revision_ids': [p.id for p in revisions.values()],
                  'preview_fingerprint': fingerprint}), ip=None)
    return sorted(revisions.values(), key=lambda p:p.week_start_date)
