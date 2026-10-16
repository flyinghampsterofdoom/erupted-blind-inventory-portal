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
