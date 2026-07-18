from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    CoverageRequirement,
    Employee,
    EmployeeCompensationRate,
    EmployeeSchedulingProfile,
    EmployeeSchedulingStorePreference,
    EmployeeSchedulingWindow,
    SchedulePeriod,
    ScheduleShift,
    ScheduleShiftType,
    SchedulingWindowKind,
    Store,
    StoreOperatingHour,
    StoreSpecialHour,
    TimeOffReasonCategory,
    TimeOffRequest,
    TimeOffRequestStatus,
)
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from app.services.v2_scheduling_service import SchedulingConflict, SchedulingValidationError, scheduled_paid_minutes
from app.v2.audit import V2AuditEvent, write_v2_audit_event


@dataclass(frozen=True)
class TimeOffInput:
    employee_id: int
    start_date: date
    end_date: date
    full_day: bool
    reason_category_id: int
    start_time: time | None = None
    end_time: time | None = None
    employee_note: str = ''


@dataclass(frozen=True)
class LaborCostEstimate:
    estimated_cost: Decimal
    costed_paid_hours: Decimal
    missing_rate_paid_hours: Decimal
    missing_rate_shift_count: int


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _audit(
    db: Session,
    *,
    principal: Principal,
    action: str,
    entity_type: str,
    entity_id: int,
    store_ids: tuple[int, ...] = (),
    before: dict | None = None,
    after: dict | None = None,
    metadata: dict | None = None,
    reason: str | None = None,
    ip: str | None = None,
    correlation_id: str | None = None,
) -> None:
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action=action,
            domain='SCHEDULING',
            entity_type=entity_type,
            entity_id=entity_id,
            store_ids=store_ids,
            timestamp=_now(),
            before=before,
            after=after,
            reason=reason,
            correlation_id=correlation_id or str(uuid.uuid4()),
            metadata=metadata or {},
        ),
        ip=ip,
    )


def _active_employee(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if employee is None or not employee.active:
        raise SchedulingValidationError('Choose an active employee.')
    return employee


def _authorized_store(db: Session, store_id: int, allowed_store_ids: tuple[int, ...]) -> Store:
    if store_id not in set(allowed_store_ids):
        raise PermissionError('The selected store is outside the authorized store scope.')
    store = db.get(Store, store_id)
    if store is None or not store.active:
        raise SchedulingValidationError('Choose an active store.')
    return store


def upsert_employee_profile(
    db: Session,
    *,
    principal: Principal,
    employee_id: int,
    home_store_id: int | None,
    target_weekly_hours: Decimal,
    minimum_weekly_hours: Decimal | None = None,
    maximum_weekly_hours: Decimal | None = None,
    preferred_workdays: int | None = None,
    scheduler_note: str = '',
    active: bool = True,
    allowed_store_ids: tuple[int, ...],
    ip: str | None = None,
) -> EmployeeSchedulingProfile:
    _active_employee(db, employee_id)
    if home_store_id is not None:
        _authorized_store(db, home_store_id, allowed_store_ids)
    if target_weekly_hours < 0 or minimum_weekly_hours is not None and minimum_weekly_hours < 0:
        raise SchedulingValidationError('Weekly hour values cannot be negative.')
    if maximum_weekly_hours is not None and maximum_weekly_hours < 0:
        raise SchedulingValidationError('Weekly hour values cannot be negative.')
    if minimum_weekly_hours is not None and maximum_weekly_hours is not None and minimum_weekly_hours > maximum_weekly_hours:
        raise SchedulingValidationError('Minimum weekly hours cannot exceed maximum weekly hours.')
    if preferred_workdays is not None and not 0 <= preferred_workdays <= 7:
        raise SchedulingValidationError('Preferred workdays must be between zero and seven.')
    row = db.execute(
        select(EmployeeSchedulingProfile).where(EmployeeSchedulingProfile.employee_id == employee_id).with_for_update()
    ).scalar_one_or_none()
    now = _now()
    before = None
    if row is None:
        row = EmployeeSchedulingProfile(
            employee_id=employee_id,
            created_by_principal_id=principal.id,
            created_at=now,
            updated_by_principal_id=principal.id,
            updated_at=now,
        )
        db.add(row)
    else:
        before = {
            'home_store_id': row.home_store_id,
            'target_weekly_hours': str(row.target_weekly_hours),
            'minimum_weekly_hours': str(row.minimum_weekly_hours) if row.minimum_weekly_hours is not None else None,
            'maximum_weekly_hours': str(row.maximum_weekly_hours) if row.maximum_weekly_hours is not None else None,
            'preferred_workdays': row.preferred_workdays,
            'active': row.active,
        }
    row.home_store_id = home_store_id
    row.target_weekly_hours = target_weekly_hours
    row.minimum_weekly_hours = minimum_weekly_hours
    row.maximum_weekly_hours = maximum_weekly_hours
    row.preferred_workdays = preferred_workdays
    row.scheduler_note = scheduler_note.strip() or None
    row.active = active
    row.updated_by_principal_id = principal.id
    row.updated_at = now
    db.flush()
    _audit(
        db, principal=principal, action='PREFERENCE_CHANGED', entity_type='employee_scheduling_profile',
        entity_id=row.id, store_ids=(home_store_id,) if home_store_id else (), before=before,
        after={'employee_id': employee_id, 'home_store_id': home_store_id, 'target_weekly_hours': str(target_weekly_hours),
               'minimum_weekly_hours': str(minimum_weekly_hours) if minimum_weekly_hours is not None else None,
               'maximum_weekly_hours': str(maximum_weekly_hours) if maximum_weekly_hours is not None else None,
               'preferred_workdays': preferred_workdays, 'active': active},
        metadata={'employee_id': employee_id}, ip=ip,
    )
    return row


def create_scheduling_window(
    db: Session,
    *,
    principal: Principal,
    employee_id: int,
    day_of_week: int,
    start_time: time,
    end_time: time,
    kind: SchedulingWindowKind,
    ip: str | None = None,
) -> EmployeeSchedulingWindow:
    _active_employee(db, employee_id)
    if not 0 <= day_of_week <= 6 or end_time <= start_time:
        raise SchedulingValidationError('Enter a valid weekday and time window.')
    now = _now()
    row = EmployeeSchedulingWindow(
        employee_id=employee_id, day_of_week=day_of_week, start_time=start_time, end_time=end_time,
        kind=kind, active=True, created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(
        db, principal=principal, action='AVAILABILITY_CHANGED', entity_type='employee_scheduling_window',
        entity_id=row.id, after={'employee_id': employee_id, 'day_of_week': day_of_week,
        'start_time': start_time.isoformat(), 'end_time': end_time.isoformat(), 'kind': kind.value},
        metadata={'employee_id': employee_id}, ip=ip,
    )
    return row


def set_store_preference(
    db: Session,
    *,
    principal: Principal,
    employee_id: int,
    store_id: int,
    preference_rank: int | None,
    allowed_store_ids: tuple[int, ...],
    active: bool = True,
    ip: str | None = None,
) -> EmployeeSchedulingStorePreference:
    _active_employee(db, employee_id)
    _authorized_store(db, store_id, allowed_store_ids)
    if preference_rank is not None and preference_rank <= 0:
        raise SchedulingValidationError('Preference rank must be positive.')
    row = db.execute(
        select(EmployeeSchedulingStorePreference).where(
            EmployeeSchedulingStorePreference.employee_id == employee_id,
            EmployeeSchedulingStorePreference.store_id == store_id,
        ).with_for_update()
    ).scalar_one_or_none()
    now = _now()
    if row is None:
        row = EmployeeSchedulingStorePreference(
            employee_id=employee_id, store_id=store_id, created_by_principal_id=principal.id,
            updated_by_principal_id=principal.id, created_at=now, updated_at=now,
        )
        db.add(row)
    row.preference_rank = preference_rank
    row.active = active
    row.updated_by_principal_id = principal.id
    row.updated_at = now
    db.flush()
    _audit(
        db, principal=principal, action='PREFERENCE_CHANGED', entity_type='employee_scheduling_store_preference',
        entity_id=row.id, store_ids=(store_id,), after={'employee_id': employee_id, 'store_id': store_id,
        'preference_rank': preference_rank, 'active': active}, metadata={'employee_id': employee_id}, ip=ip,
    )
    return row


def create_time_off_request(
    db: Session,
    *,
    principal: Principal,
    values: TimeOffInput,
    management_entered: bool = True,
    ip: str | None = None,
) -> TimeOffRequest:
    _active_employee(db, values.employee_id)
    if values.end_date < values.start_date:
        raise SchedulingValidationError('End date must be on or after start date.')
    if values.full_day:
        if values.start_time is not None or values.end_time is not None:
            raise SchedulingValidationError('Full-day time off cannot include times.')
    elif (
        values.start_date != values.end_date
        or values.start_time is None
        or values.end_time is None
        or values.end_time <= values.start_time
    ):
        raise SchedulingValidationError('Partial-day time off requires one date and a valid time range.')
    category = db.get(TimeOffReasonCategory, values.reason_category_id)
    if category is None or not category.active:
        raise SchedulingValidationError('Choose an active time-off reason category.')
    now = _now()
    row = TimeOffRequest(
        employee_id=values.employee_id, start_date=values.start_date, end_date=values.end_date,
        full_day=values.full_day, start_time=values.start_time, end_time=values.end_time,
        reason_category_id=values.reason_category_id, employee_note=values.employee_note.strip() or None,
        status=TimeOffRequestStatus.PENDING,
        submitted_by_principal_id=None if management_entered else principal.id,
        submitted_at=now, created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(
        db, principal=principal, action='TIME_OFF_SUBMITTED', entity_type='time_off_request', entity_id=row.id,
        after={'employee_id': row.employee_id, 'start_date': row.start_date.isoformat(),
               'end_date': row.end_date.isoformat(), 'full_day': row.full_day, 'status': row.status.value,
               'management_entered': management_entered}, metadata={'employee_id': row.employee_id}, ip=ip,
    )
    return row


def review_time_off_request(
    db: Session,
    *,
    principal: Principal,
    request_id: int,
    status: TimeOffRequestStatus,
    management_review_note: str = '',
    ip: str | None = None,
) -> TimeOffRequest:
    if status not in {TimeOffRequestStatus.APPROVED, TimeOffRequestStatus.DENIED, TimeOffRequestStatus.CANCELLED}:
        raise SchedulingValidationError('Choose an approved review status.')
    row = db.execute(select(TimeOffRequest).where(TimeOffRequest.id == request_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise SchedulingValidationError('Time-off request not found.')
    if row.status != TimeOffRequestStatus.PENDING:
        raise SchedulingConflict('Only pending time-off requests can be reviewed.')
    before = row.status.value
    now = _now()
    row.status = status
    row.management_review_note = management_review_note.strip() or None
    row.reviewed_by_principal_id = principal.id
    row.reviewed_at = now
    row.updated_by_principal_id = principal.id
    row.updated_at = now
    affected = db.execute(
        select(SchedulePeriod).where(
            SchedulePeriod.week_start_date <= row.end_date,
            SchedulePeriod.week_end_date >= row.start_date,
        ).with_for_update()
    ).scalars().all()
    db.flush()
    for period in affected:
        rebuild_schedule_warnings(db, schedule_period_id=period.id)
    _audit(
        db, principal=principal, action='TIME_OFF_REVIEWED', entity_type='time_off_request', entity_id=row.id,
        before={'status': before}, after={'status': status.value}, reason=management_review_note.strip() or None,
        metadata={'employee_id': row.employee_id, 'affected_schedule_period_ids': [p.id for p in affected]}, ip=ip,
    )
    return row


def create_operating_hour(
    db: Session,
    *,
    principal: Principal,
    store_id: int,
    day_of_week: int,
    opening_time: time,
    closing_time: time,
    allowed_store_ids: tuple[int, ...],
    ip: str | None = None,
) -> StoreOperatingHour:
    _authorized_store(db, store_id, allowed_store_ids)
    if not 0 <= day_of_week <= 6 or closing_time <= opening_time:
        raise SchedulingValidationError('Enter a valid weekday and operating interval.')
    now = _now()
    row = StoreOperatingHour(
        store_id=store_id, day_of_week=day_of_week, opening_time=opening_time, closing_time=closing_time,
        active=True, created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(db, principal=principal, action='OPERATING_HOURS_CHANGED', entity_type='store_operating_hour',
           entity_id=row.id, store_ids=(store_id,), after={'day_of_week': day_of_week,
           'opening_time': opening_time.isoformat(), 'closing_time': closing_time.isoformat()}, ip=ip)
    return row


def upsert_special_hour(
    db: Session,
    *,
    principal: Principal,
    store_id: int,
    calendar_date: date,
    event_name: str,
    closed_all_day: bool,
    allowed_store_ids: tuple[int, ...],
    opening_time: time | None = None,
    closing_time: time | None = None,
    staffing_note: str = '',
    batch_correlation_id: str | None = None,
    ip: str | None = None,
) -> StoreSpecialHour:
    _authorized_store(db, store_id, allowed_store_ids)
    if not event_name.strip():
        raise SchedulingValidationError('Event or holiday name is required.')
    if closed_all_day:
        opening_time = closing_time = None
    elif opening_time is None or closing_time is None or closing_time <= opening_time:
        raise SchedulingValidationError('Open special hours require a valid opening and closing time.')
    row = db.execute(
        select(StoreSpecialHour).where(
            StoreSpecialHour.store_id == store_id,
            StoreSpecialHour.calendar_date == calendar_date,
            StoreSpecialHour.active.is_(True),
        ).with_for_update()
    ).scalar_one_or_none()
    now = _now()
    if row is None:
        row = StoreSpecialHour(
            store_id=store_id, calendar_date=calendar_date,
            created_by_principal_id=principal.id, created_at=now,
            updated_by_principal_id=principal.id, updated_at=now,
        )
        db.add(row)
    row.event_name = event_name.strip()
    row.closed_all_day = closed_all_day
    row.opening_time = opening_time
    row.closing_time = closing_time
    row.staffing_note = staffing_note.strip() or None
    row.batch_correlation_id = batch_correlation_id
    row.active = True
    row.updated_by_principal_id = principal.id
    row.updated_at = now
    db.flush()
    _audit(db, principal=principal, action='SPECIAL_HOURS_CHANGED', entity_type='store_special_hour',
           entity_id=row.id, store_ids=(store_id,), after={'calendar_date': calendar_date.isoformat(),
           'event_name': row.event_name, 'closed_all_day': closed_all_day,
           'opening_time': opening_time.isoformat() if opening_time else None,
           'closing_time': closing_time.isoformat() if closing_time else None}, ip=ip,
           correlation_id=batch_correlation_id)
    return row


def create_coverage_requirement(
    db: Session,
    *,
    principal: Principal,
    store_id: int,
    day_of_week: int,
    start_time: time,
    end_time: time,
    minimum_employee_count: int,
    allowed_store_ids: tuple[int, ...],
    required_shift_type_id: int | None = None,
    requires_opener: bool = False,
    requires_closer: bool = False,
    ip: str | None = None,
) -> CoverageRequirement:
    _authorized_store(db, store_id, allowed_store_ids)
    if not 0 <= day_of_week <= 6 or end_time <= start_time or minimum_employee_count < 0:
        raise SchedulingValidationError('Enter a valid coverage interval and employee count.')
    if required_shift_type_id is not None:
        role = db.get(ScheduleShiftType, required_shift_type_id)
        if role is None or not role.active:
            raise SchedulingValidationError('Choose an active required shift type.')
    now = _now()
    row = CoverageRequirement(
        store_id=store_id, day_of_week=day_of_week, start_time=start_time, end_time=end_time,
        minimum_employee_count=minimum_employee_count, required_shift_type_id=required_shift_type_id,
        requires_opener=requires_opener, requires_closer=requires_closer, active=True,
        created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(db, principal=principal, action='COVERAGE_RULE_CHANGED', entity_type='coverage_requirement',
           entity_id=row.id, store_ids=(store_id,), after={'day_of_week': day_of_week,
           'start_time': start_time.isoformat(), 'end_time': end_time.isoformat(),
           'minimum_employee_count': minimum_employee_count, 'required_shift_type_id': required_shift_type_id,
           'requires_opener': requires_opener, 'requires_closer': requires_closer}, ip=ip)
    return row


def create_compensation_rate(
    db: Session,
    *,
    principal: Principal,
    employee_id: int,
    effective_start_date: date,
    hourly_rate: Decimal,
    effective_end_date: date | None = None,
    ip: str | None = None,
) -> EmployeeCompensationRate:
    _active_employee(db, employee_id)
    if hourly_rate < 0 or effective_end_date is not None and effective_end_date < effective_start_date:
        raise SchedulingValidationError('Enter a valid effective date range and nonnegative hourly rate.')
    overlap = db.execute(
        select(EmployeeCompensationRate.id).where(
            EmployeeCompensationRate.employee_id == employee_id,
            EmployeeCompensationRate.active.is_(True),
            EmployeeCompensationRate.effective_start_date <= (effective_end_date or date.max),
            or_(
                EmployeeCompensationRate.effective_end_date.is_(None),
                EmployeeCompensationRate.effective_end_date >= effective_start_date,
            ),
        ).with_for_update()
    ).scalar_one_or_none()
    if overlap is not None:
        raise SchedulingConflict('Compensation rate effective dates may not overlap for one employee.')
    now = _now()
    row = EmployeeCompensationRate(
        employee_id=employee_id, effective_start_date=effective_start_date,
        effective_end_date=effective_end_date, hourly_rate=hourly_rate, active=True,
        created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(db, principal=principal, action='COMPENSATION_RATE_CHANGED', entity_type='employee_compensation_rate',
           entity_id=row.id, after={'employee_id': employee_id, 'effective_start_date': effective_start_date.isoformat(),
           'effective_end_date': effective_end_date.isoformat() if effective_end_date else None,
           'rate_recorded': True}, metadata={'employee_id': employee_id}, ip=ip)
    return row


def estimate_labor_cost(
    db: Session,
    *,
    schedule_period_id: int,
    permitted: bool,
    allowed_store_ids: tuple[int, ...],
) -> LaborCostEstimate:
    if not permitted:
        raise PermissionError('Viewing aggregate labor cost requires scheduling.view_labor_cost.')
    period = db.get(SchedulePeriod, schedule_period_id)
    if period is None:
        raise SchedulingValidationError('Schedule period not found.')
    shifts = db.execute(
        select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.store_id.in_(allowed_store_ids),
            ScheduleShift.employee_id.is_not(None),
        )
    ).scalars().all()
    estimated = Decimal('0.00')
    costed_minutes = 0
    missing_minutes = 0
    missing_count = 0
    for shift in shifts:
        paid_minutes = scheduled_paid_minutes(shift)
        rate = db.execute(
            select(EmployeeCompensationRate.hourly_rate).where(
                EmployeeCompensationRate.employee_id == shift.employee_id,
                EmployeeCompensationRate.active.is_(True),
                EmployeeCompensationRate.effective_start_date <= shift.shift_date,
                or_(
                    EmployeeCompensationRate.effective_end_date.is_(None),
                    EmployeeCompensationRate.effective_end_date >= shift.shift_date,
                ),
            ).order_by(EmployeeCompensationRate.effective_start_date.desc())
        ).scalar_one_or_none()
        if rate is None:
            missing_minutes += paid_minutes
            missing_count += 1
            continue
        costed_minutes += paid_minutes
        estimated += (Decimal(paid_minutes) / Decimal(60)) * rate
    return LaborCostEstimate(
        estimated_cost=estimated.quantize(Decimal('0.01')),
        costed_paid_hours=(Decimal(costed_minutes) / Decimal(60)).quantize(Decimal('0.01')),
        missing_rate_paid_hours=(Decimal(missing_minutes) / Decimal(60)).quantize(Decimal('0.01')),
        missing_rate_shift_count=missing_count,
    )
