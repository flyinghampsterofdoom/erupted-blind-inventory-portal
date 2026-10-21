"""Transactional invalidation of operational drafts after hard input changes.

The ORM hooks cover all application writers, including imports and deletes.
They only update freshness metadata and the derived warning cache. Assignments,
Lead flags, locks, snapshots and published revisions are never rewritten here.
"""
from datetime import datetime, timezone
from sqlalchemy import event, inspect, select, update, insert, func
from sqlalchemy.orm import Session, attributes

REGENERATION_REQUIRED = 'SCHEDULE_REGENERATION_REQUIRED'
MESSAGE = 'Hard scheduling constraints changed. This schedule requires regeneration and review before publication. Saved assignments have not been changed.'

# Deliberately exclude notes, timestamps, soft preferences and rotation counters.
HARD_FIELDS = {
    'Employee': 'active scheduling_active last_effective_date scheduling_lead_capable scheduling_double_coverage square_status',
    'EmployeeSchedulingProfile': 'employee_id active maximum_weekly_hours target_shifts_per_week minimum_days_off_after_max_block special_store_participation weekday_preferences',
    'EmployeeSchedulingWindow': 'employee_id day_of_week start_time end_time kind active',
    'EmployeeSchedulingStorePreference': 'employee_id store_id preference_level active',
    'TimeOffRequest': 'employee_id start_date end_date full_day start_time end_time status',
    'SchedulingEligibilityPeriod': 'employee_id start_date end_date legacy_unknown_start',
    'SchedulingLeaveAllocationException': 'employee_id week_start_date work_shifts active',
    'SchedulingAllocationChange': 'employee_id effective_date shifts_per_week baseline active',
    'Store': 'active',
    'CoverageRequirement': 'store_id day_of_week start_time end_time minimum_employee_count required_shift_type_id requires_opener requires_closer active',
    'StoreOperatingHour': 'store_id day_of_week opening_time closing_time active',
    'StoreSpecialHour': 'store_id calendar_date closed_all_day opening_time closing_time active',
    'StoreShift': 'store_id start_time end_time active_weekdays active',
    'SchedulingStoreDefaults': 'double_coverage_store_id standard_shift_start standard_shift_end',
    'SpecialStorePolicy': 'store_id active',
    'SpecialStoreRotationState': 'store_id employee_id participation',
}


def requires_regeneration(period):
    return bool(period.constraints_changed_at and
                (period.generated_at is None or period.constraints_changed_at > period.generated_at))


def _values(obj, field):
    state = inspect(obj)
    return [getattr(obj, field, None), *state.attrs[field].history.deleted]


def _enum(value):
    return getattr(value, 'value', value)


def _collect(db, flush_context, instances):
    # Production scheduling requires PostgreSQL (enforced by startup). Other
    # domains use partial SQLite schemas in unit tests, without advisory locks
    # or scheduling tables. Do not install scheduling work into those sessions.
    if db.get_bind().dialect.name != 'postgresql':
        return
    from app import models as m
    changes = []
    for obj in set(db.new) | set(db.dirty) | set(db.deleted):
        name = type(obj).__name__
        fields = HARD_FIELDS.get(name)
        if not fields:
            continue
        changed = obj in db.new or obj in db.deleted or any(
            inspect(obj).attrs[f].history.has_changes() for f in fields.split() if hasattr(obj, f))
        if not changed:
            continue
        if (name == 'Employee' and obj in db.new and obj.scheduling_active is False
                and not obj.eligibility_periods):
            # A newly created nonparticipant cannot change staffing feasibility.
            # Its id is not assigned yet: treating {None} as an employee scope
            # would incorrectly invalidate every draft with an unfilled row.
            continue
        if name == 'EmployeeSchedulingProfile' and obj not in db.new and obj not in db.deleted:
            fields_changed = {f for f in fields.split() if inspect(obj).attrs[f].history.has_changes()}
            if fields_changed == {'weekday_preferences'}:
                # Work/off preferences are soft; only changing absolute bans
                # invalidates hard-constraint validation.
                values = _values(obj, 'weekday_preferences')
                from datetime import time
                legacy_bans = set(db.scalars(select(m.EmployeeSchedulingWindow.day_of_week).where(
                    m.EmployeeSchedulingWindow.employee_id == obj.employee_id,
                    m.EmployeeSchedulingWindow.active.is_(True),
                    m.EmployeeSchedulingWindow.kind == m.SchedulingWindowKind.HARD_UNAVAILABLE,
                    m.EmployeeSchedulingWindow.start_time == time.min,
                    m.EmployeeSchedulingWindow.end_time == time.max)))
                bans = [({i for i, state in enumerate(v) if state == 'CANNOT_WORK'} if v is not None else legacy_bans) for v in values]
                if len(bans) < 2 or all(v == bans[0] for v in bans[1:]):
                    continue
        if name == 'EmployeeSchedulingWindow' and 'HARD_UNAVAILABLE' not in map(_enum, _values(obj, 'kind')):
            continue
        if name == 'EmployeeSchedulingStorePreference' and 'NEVER' not in map(_enum, _values(obj, 'preference_level')):
            continue
        if name == 'TimeOffRequest' and 'APPROVED' not in map(_enum, _values(obj, 'status')):
            continue
        changes.append(obj)
    if changes:
        # Serialize against generation/publication: an in-flight old-input plan
        # cannot erase a newer invalidation by setting generated_at afterwards.
        from app.services.v2_scheduling_policy_service import SCHEDULE_AUTOMATION_LOCK_KEY
        db.scalar(select(func.pg_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY)))
        scopes = []
        for obj in changes:
            name = type(obj).__name__
            employee_ids = set(_values(obj, 'employee_id')) if hasattr(obj, 'employee_id') else ({obj.id} if name == 'Employee' else set())
            dates = []
            if name == 'TimeOffRequest':
                dates = [v for f in ('start_date', 'end_date') for v in _values(obj, f) if v]
            elif name == 'SchedulingLeaveAllocationException':
                from datetime import timedelta
                dates = [v + offset for v in _values(obj, 'week_start_date') if v
                         for offset in (timedelta(0), timedelta(days=6))]
            elif name == 'StoreSpecialHour':
                dates = [v for v in _values(obj, 'calendar_date') if v]
            # Allocations can require work even when an employee has no shift.
            allocation = name in {'SchedulingAllocationChange', 'SchedulingLeaveAllocationException'} or (name == 'EmployeeSchedulingProfile'
                and inspect(obj).attrs.target_shifts_per_week.history.has_changes())
            scopes.append((employee_ids - {None}, min(dates) if dates else None,
                           max(dates) if dates else None, allocation))
        db.info['_schedule_constraint_scopes'] = scopes
        db.info['_schedule_new_periods'] = {p for p in db.new if isinstance(p, m.SchedulePeriod)}


def _revalidate(db, flush_context):
    scopes = db.info.pop('_schedule_constraint_scopes', None)
    new_periods = db.info.pop('_schedule_new_periods', set())
    if not scopes:
        return
    from app import models as m
    from app.services.v2_scheduling_lead_duty_service import business_today
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    today = business_today(db)
    periods = list(db.scalars(select(m.SchedulePeriod).where(
        m.SchedulePeriod.status == m.SchedulePeriodStatus.DRAFT,
        m.SchedulePeriod.is_historical.is_(False), m.SchedulePeriod.cleared_at.is_(None),
        m.SchedulePeriod.week_end_date >= today).order_by(m.SchedulePeriod.id).with_for_update()))
    now = datetime.now(timezone.utc)
    for period in periods:
        if period in new_periods:
            continue
        rows = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id == period.id)))
        if not rows:
            continue
        assigned = {r.employee_id for r in rows}
        if not any((not ids or allocation or assigned & ids) and
                   (start is None or (period.week_start_date <= end and period.week_end_date >= start))
                   for ids, start, end, allocation in scopes):
            continue
        db.execute(update(m.SchedulePeriod).where(m.SchedulePeriod.id == period.id).values(
            constraints_changed_at=now).execution_options(synchronize_session=False))
        attributes.set_committed_value(period, 'constraints_changed_at', now)
        # Core insert is safe inside this flush callback and makes the warning
        # cache immediately queryable even by autoflush-disabled callers.
        warnings = rebuild_schedule_warnings(db, schedule_period_id=period.id, flush=False)
        values = []
        for warning in warnings:
            values.append({c.name: getattr(warning, c.name) for c in m.ScheduleWarning.__table__.columns
                           if c.name != 'id'})
            db.expunge(warning)
        if values:
            db.connection().execute(insert(m.ScheduleWarning), values)


def _rollback(db, previous_transaction):
    db.info.pop('_schedule_constraint_scopes', None)
    db.info.pop('_schedule_new_periods', None)
    from app.models import SchedulePeriod
    for obj in list(db.identity_map.values()):
        if isinstance(obj, SchedulePeriod) and inspect(obj).persistent:
            db.expire(obj, ['constraints_changed_at'])


def install_freshness_hooks():
    if not event.contains(Session, 'before_flush', _collect):
        event.listen(Session, 'before_flush', _collect)
        event.listen(Session, 'after_flush_postexec', _revalidate)
        event.listen(Session, 'after_soft_rollback', _rollback)
