"""Real scheduling projections preserve intent, effective intervals, and unknown work."""
from datetime import date, time
from decimal import Decimal

from app import models as m
from app.services.employee_schedule_service import company_published_assignments, own_published_assignments
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_exception_service import record_exception, cancel_commitment
from test_v2_scheduling_foundation import scheduling_db
from test_v2_lead_duty import period, shift, attendance

DAY = date(2030, 5, 5)


def board(db, ids, period_id):
    return serialize_week_board(db, week_start=DAY, schedule_period_id=period_id,
        selected_store_ids=(ids['north'], ids['south']),
        all_authorized_store_ids=(ids['north'], ids['south']),
        permission_flags={'scheduling.attendance.record': True})


def test_published_overlap_uses_partial_effective_intervals_and_keeps_real_conflicts(scheduling_db):
    from app.services.v2_scheduling_coverage_service import evaluate_schedule_warnings

    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        original = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        other = shift(db, actor, per, ids['alex'], ids['south'], DAY, lead=False)
        other.start_time, other.end_time = time(11), time(15)
        db.flush()
        record_exception(db, principal=actor, shift_id=original.id,
            commitment_employee_id=ids['blair'], coverage_start_time=time(11), coverage_end_time=time(15))

        def overlaps():
            return [warning for warning in evaluate_schedule_warnings(db, schedule_period_id=per.id)
                    if warning.warning_type == 'EMPLOYEE_OVERLAP']

        assert overlaps() == []  # Original employee works 9–11, other store 11–15, then 15–17.
        other.start_time = time(10)
        db.flush()
        warnings = overlaps()
        assert len(warnings) == 1
        assert warnings[0].employee_id == ids['alex']
        assert (warnings[0].start_time, warnings[0].end_time) == (time(10), time(11))
        assert original.start_time == time(9) and original.end_time == time(17)


def test_absence_and_coverage_cards_keep_distinct_hours_and_identity(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        original = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        record_exception(db, principal=actor, shift_id=original.id,
            event_type=m.AttendanceEventType.CALLED_OUT, commitment_employee_id=ids['blair'])
        view = board(db, ids, per.id)
        source, cover = view['shifts']
        assert source['id'] == original.id and source['employee_id'] == ids['alex']
        assert source['exception']['status'] and source['attendance_statuses']
        assert (source['original_scheduled_hours'], source['effective_hours'], source['actual_hours']) == (8, 0, 0)
        assert cover['is_coverage'] and cover['id'] < 0 and cover['source_shift_id'] == original.id
        assert cover['covering_employee_name'] == 'Alex One' and cover['employee_id'] == ids['blair']
        assert cover['effective_hours'] == 8 and cover['actual_hours'] is None
        assert not cover['is_lead_of_day']
        people = {row['id']: row for row in view['employees']}
        assert people[ids['alex']]['scheduled_hours'] == 0
        assert people[ids['alex']]['original_scheduled_hours'] == 8
        assert people[ids['blair']]['scheduled_hours'] == 8
        assert people[ids['blair']]['actual_confirmed_hours'] is None
        cards = company_published_assignments(db, start_date=DAY, end_date=DAY)
        assert [(row.employee_id, row.is_coverage) for row in cards] == [(ids['alex'], False), (ids['blair'], True)]
        assert cards[0].has_exception and not cards[0].transfer_eligible
        assert not cards[1].display_lead and not cards[1].transfer_eligible
        assert own_published_assignments(db, employee_id=ids['alex'], start_date=DAY, end_date=DAY)[0].id == original.id
        cancel_commitment(db, principal=actor, commitment_id=cover['coverage_assignment_id'])
        assert not any(row['is_coverage'] for row in board(db, ids, per.id)['shifts'])
        assert original.employee_id == ids['alex']


def test_partial_late_relief_keeps_original_remaining_hours_and_existing_shift(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        original = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        existing = shift(db, actor, per, ids['blair'], ids['south'], DAY, lead=False)
        # Attendance evidence is distinct from the prospective relief assignment.
        attendance(db, actor, original, 'LATE')
        record_exception(db, principal=actor, shift_id=original.id,
            commitment_employee_id=ids['blair'], coverage_start_time=time(9), coverage_end_time=time(11))
        view = board(db, ids, per.id)
        source = next(row for row in view['shifts'] if row['id'] == original.id)
        cover = next(row for row in view['shifts'] if row['is_coverage'])
        assert (source['original_scheduled_hours'], source['effective_hours']) == (8, 6)
        assert source['actual_hours'] is None and cover['actual_hours'] is None
        assert (cover['start_time'], cover['end_time'], cover['effective_hours']) == ('09:00', '11:00', 2)
        people = {row['id']: row for row in view['employees']}
        assert people[ids['blair']]['scheduled_hours'] == 10
        assert next(row for row in view['shifts'] if row['id'] == existing.id)['store_id'] == ids['south']
        cards = company_published_assignments(db, start_date=DAY, end_date=DAY)
        assert sum(row.effective_scheduled_hours for row in cards if row.employee_id == ids['alex']) == 6
        assert sum(row.effective_scheduled_hours for row in cards if row.employee_id == ids['blair']) == 10
        assert original.start_time == time(9) and original.end_time == time(17)


def test_legacy_actual_only_coverage_projects_without_commitment(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        original = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        attendance(db, actor, original, 'CALLED_OUT')
        attendance(db, actor, original, 'COVERED_SHIFT', ids['blair'])
        cover = next(row for row in board(db, ids, per.id)['shifts'] if row['is_coverage'])
        assert cover['employee_id'] == ids['blair'] and cover['coverage_assignment_id'] is None
        assert cover['id'] < 0 and cover['source_shift_id'] == original.id
        assert cover['actual_hours'] == 8
        company = company_published_assignments(db, start_date=DAY, end_date=DAY)
        assert len(company) == 2 and company[1].employee_id == ids['blair']


def test_labor_estimate_uses_replacement_rate_and_partial_original_intervals(scheduling_db):
    from app.services.v2_scheduling_rules_service import create_compensation_rate, estimate_labor_cost
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        original = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        for eid, rate in ((ids['alex'], '10'), (ids['blair'], '30')):
            create_compensation_rate(db, principal=actor, employee_id=eid,
                effective_start_date=DAY, hourly_rate=Decimal(rate))
        cost = lambda: estimate_labor_cost(db, schedule_period_id=per.id,
            permitted=True, allowed_store_ids=(ids['north'],))
        assert cost().estimated_cost == Decimal('80.00')
        record_exception(db, principal=actor, shift_id=original.id,
            event_type=m.AttendanceEventType.CALLED_OUT, commitment_employee_id=ids['blair'])
        assert cost().estimated_cost == Decimal('240.00')
        assert cost().costed_paid_hours == Decimal('8.00')
        # A factual status change preserves the six-hour original interval.
        attendance(db, actor, original, 'LATE')
        for event in db.query(m.ScheduleAttendanceEvent).filter_by(schedule_shift_id=original.id):
            if event.event_type == m.AttendanceEventType.CALLED_OUT:
                from app.services.v2_scheduling_attendance_service import void_attendance_event
                void_attendance_event(db, principal=actor, event_id=event.id, reason='Corrected to late')
        record_exception(db, principal=actor, shift_id=original.id,
            commitment_employee_id=ids['blair'], coverage_start_time=time(9), coverage_end_time=time(11))
        assert cost().estimated_cost == Decimal('120.00')
        assert cost().costed_paid_hours == Decimal('8.00')
        assert original.employee_id == ids['alex']


def test_reassigned_child_does_not_inherit_prior_employees_absence(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        first = period(db, actor, DAY)
        original = shift(db, actor, first, ids['alex'], ids['north'], DAY)
        attendance(db, actor, original, 'CALLED_OUT')
        first.status = m.SchedulePeriodStatus.ARCHIVED
        replacement = period(db, actor, DAY, revision=2)
        reassigned = shift(db, actor, replacement, ids['blair'], ids['north'], DAY)
        reassigned.source_shift_id = original.id
        db.flush()
        company = company_published_assignments(db, start_date=DAY, end_date=DAY)
        assert len(company) == 1
        card = company[0]
        assert card.id == reassigned.id and card.employee_id == ids['blair']
        assert not card.has_exception and card.attendance_statuses == []
        assert card.transfer_eligible and card.effective_scheduled_hours == 8
        personal = own_published_assignments(db, employee_id=ids['blair'], start_date=DAY, end_date=DAY)
        assert len(personal) == 1 and not personal[0].has_exception
        assert db.query(m.ScheduleAttendanceEvent).filter_by(schedule_shift_id=original.id).one().original_employee_id == ids['alex']
