"""Personal published schedule reads, independent of account store scope."""
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, or_
from app.models import SchedulePeriod, SchedulePeriodStatus, ScheduleShift
from app.services.v2_scheduling_exception_service import expected_employee_id
from app.services.v2_scheduling_policy_service import organization_policy


def own_published_assignments(db, *, employee_id, start_date, end_date):
    periods=list(db.scalars(select(SchedulePeriod).where(
        or_(SchedulePeriod.status==SchedulePeriodStatus.PUBLISHED, SchedulePeriod.published_at.is_not(None)),
        SchedulePeriod.is_historical.is_(False), SchedulePeriod.week_start_date<=end_date,
        SchedulePeriod.week_end_date>=start_date).order_by(SchedulePeriod.revision_number)))
    latest={p.week_start_date:p for p in periods}
    selected={p.id:p for p in latest.values()}
    if not selected:return []
    zone=ZoneInfo(organization_policy(db).timezone_name)
    result=[]
    for shift in db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id.in_(selected),
            ScheduleShift.shift_date.between(start_date,end_date)).order_by(ScheduleShift.shift_date,ScheduleShift.start_time,ScheduleShift.id)):
        period=selected[shift.schedule_period_id]
        if period.cleared_at and datetime.combine(shift.shift_date,shift.end_time,tzinfo=zone)>period.cleared_at:
            continue
        if expected_employee_id(db,shift)==employee_id:result.append(shift)
    return result


def company_published_assignments(db, *, start_date, end_date):
    """Latest published week only; never expose the replacement draft or notes.

    This projection deliberately avoids the management board's warning rebuild,
    generation, store scoping and administrative payload.
    """
    from types import SimpleNamespace
    periods = list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
        SchedulePeriod.is_historical.is_(False),
        SchedulePeriod.week_start_date <= end_date,
        SchedulePeriod.week_end_date >= start_date,
    ).order_by(SchedulePeriod.revision_number, SchedulePeriod.id)))
    latest = {p.week_start_date: p for p in periods}
    selected = {p.id: p for p in latest.values()}
    if not selected:
        return []
    zone = ZoneInfo(organization_policy(db).timezone_name)
    result = []
    for shift in db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id.in_(selected),
        ScheduleShift.shift_date.between(start_date, end_date),
    ).order_by(ScheduleShift.shift_date, ScheduleShift.start_time, ScheduleShift.id)):
        period = selected[shift.schedule_period_id]
        if period.cleared_at and datetime.combine(shift.shift_date, shift.end_time, tzinfo=zone) > period.cleared_at:
            continue
        effective_id = expected_employee_id(db, shift)
        result.append(SimpleNamespace(
            id=shift.id, store_id=shift.store_id, shift_date=shift.shift_date,
            start_time=shift.start_time, end_time=shift.end_time,
            employee_id=effective_id, assigned_employee_id=shift.employee_id,
            display_lead=shift.is_lead_of_day and effective_id == shift.employee_id,
            transfer_eligible=effective_id is not None and effective_id == shift.employee_id,
        ))
    return result
