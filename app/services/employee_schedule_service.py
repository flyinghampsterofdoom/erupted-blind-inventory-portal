"""Published schedule cards keep intent separate from effective coverage and work."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select, or_
from app.models import Principal as Account, SchedulePeriod, SchedulePeriodStatus, ScheduleShift, ScheduleAttendanceEvent
from app.services.read_scope import active_evidence, read_operation
from app.services.v2_scheduling_exception_service import effective_shift_segments, shift_lineage
from app.services.v2_scheduling_policy_service import organization_policy
from app.services.v2_scheduling_service import scheduled_paid_minutes


def own_published_assignments(db, *, employee_id, start_date, end_date):
    """Original absence cards and independent replacement cards for one employee."""
    return [row for row in _published_cards(db, start_date=start_date, end_date=end_date,
                                           include_previously_published=True)
            if row.employee_id == employee_id]


def company_published_assignments(db, *, start_date, end_date):
    """Latest published cards only, without private notes or management payloads.

    Original rows never change owner because of a call-out. Coverage is a linked
    projection of the existing commitment, not a second scheduling source of truth.
    """
    return _published_cards(db, start_date=start_date, end_date=end_date)


@read_operation
def _published_cards(db, *, start_date, end_date, include_previously_published=False):
    published = SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED
    if include_previously_published:
        published = or_(published, SchedulePeriod.published_at.is_not(None))
    periods = list(db.scalars(select(SchedulePeriod).where(
        published,
        SchedulePeriod.is_historical.is_(False),
        SchedulePeriod.week_start_date <= end_date,
        SchedulePeriod.week_end_date >= start_date,
    ).order_by(SchedulePeriod.revision_number, SchedulePeriod.id)))
    selected = {p.id: p for p in {p.week_start_date: p for p in periods}.values()}
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
        segments = effective_shift_segments(db, shift)
        original_minutes = sum(scheduled_paid_minutes(row) for row in segments
                               if row.employee_id == shift.employee_id and not row.is_coverage)
        events = active_evidence(db, ScheduleAttendanceEvent, [row.id for row in shift_lineage(db, shift)
            if row.employee_id == shift.employee_id and row.shift_date == shift.shift_date])
        statuses = [event.event_type.value.replace('_', ' ').title() for event in events]
        coverage = [row for row in segments if row.is_coverage]
        common = dict(store_id=shift.store_id, shift_date=shift.shift_date, schedule_period_id=shift.schedule_period_id,
                      period_version=period.version, assigned_employee_id=shift.employee_id,
                      is_opener=shift.is_opener, is_closer=shift.is_closer)
        result.append(SimpleNamespace(
            **common, id=shift.id, source_shift_id=shift.id, coverage_assignment_id=None,
            start_time=shift.start_time, end_time=shift.end_time,
            unpaid_break_minutes=shift.unpaid_break_minutes,
            owner_name=db.get(Account, shift.owner_principal_id).username if shift.owner_principal_id else None,
            employee_id=shift.employee_id, is_coverage=False, covering_employee_id=None,
            is_shift_lead=any(s.is_shift_lead for s in segments if s.employee_id == shift.employee_id),
            is_double_coverage=shift.is_double_coverage, display_lead=shift.is_lead_of_day,
            has_exception=bool(statuses or coverage), attendance_statuses=statuses,
            original_scheduled_hours=scheduled_paid_minutes(shift) / 60,
            effective_scheduled_hours=original_minutes / 60,
            transfer_eligible=shift.employee_id is not None and not statuses and not coverage,
        ))
        for row in coverage:
            result.append(SimpleNamespace(
                **common, id=-(row.coverage_assignment_id or (1_000_000_000 + shift.id)), source_shift_id=shift.id,
                coverage_assignment_id=row.coverage_assignment_id, is_coverage=True, owner_name=None,
                start_time=row.start_time, end_time=row.end_time,
                unpaid_break_minutes=row.unpaid_break_minutes,
                employee_id=row.employee_id, covering_employee_id=shift.employee_id,
                is_shift_lead=row.is_shift_lead, is_double_coverage=False, display_lead=False, has_exception=False,
                attendance_statuses=[], original_scheduled_hours=0,
                effective_scheduled_hours=scheduled_paid_minutes(row) / 60,
                transfer_eligible=False,
            ))
    return result
