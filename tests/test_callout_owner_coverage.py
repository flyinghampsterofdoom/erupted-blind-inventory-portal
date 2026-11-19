"""Owner relief is audited coverage, never an employee or published-intent rewrite."""
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app import models as m
from app.schema_contract import _alembic_config, assert_supported_schema
from app.services.employee_schedule_service import company_published_assignments
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_exception_service import (
    coverage_assignments, effective_shift_segments, exception_facts, record_exception,
)
from app.services.v2_scheduling_policy_service import scheduled_weekly_hours
from app.services.v2_scheduling_service import (
    SchedulingValidationError, ShiftInput, validate_owner_coverage,
)
from test_v2_scheduling_foundation import scheduling_db
from test_v2_lead_duty import period, shift

DAY = date(2030, 10, 27)
TODAY = DAY - timedelta(days=1)


def owner(db, *, active=True, role=m.PrincipalRole.ADMIN):
    row = m.Principal(username='Owner relief', role=role, active=active)
    db.add(row); db.flush()
    return row


def test_owner_reassign_cancel_preserves_intent_history_and_employee_hours(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        account = owner(db)
        employee_count = db.scalar(select(func.count()).select_from(m.Employee))
        record_exception(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.CALLED_OUT,
            commitment_employee_id=ids['blair'], replace_existing=True, today=TODAY)
        first, = coverage_assignments(db, source)
        original_event = db.scalar(select(m.ScheduleAttendanceEvent))
        record_exception(db, principal=actor, shift_id=source.id,
            commitment_owner_principal_id=account.id, today=TODAY)
        relief, = coverage_assignments(db, source)
        assert first.voided_at and relief.employee_id is None
        assert relief.owner_principal_id == account.id and not relief.is_shift_lead
        assert source.employee_id == ids['alex'] and source.owner_principal_id is None
        assert source.is_lead_of_day
        assert db.scalar(select(func.count()).select_from(m.Employee)) == employee_count
        assert scheduled_weekly_hours(db, employee_id=ids['alex'], shift_date=DAY) == Decimal('0.00')
        assert scheduled_weekly_hours(db, employee_id=ids['blair'], shift_date=DAY) == Decimal('0.00')
        segment, = effective_shift_segments(db, source)
        assert segment.employee_id is None and segment.owner_principal_id == account.id
        assert segment.is_coverage and not segment.is_lead_of_day and not segment.is_shift_lead
        facts = exception_facts(db, source, TODAY)
        assert facts['commitment']['owner_name'] == account.username
        assert facts['commitment']['owner_principal_id'] == account.id
        assert facts['commitment']['employee_name'] is None
        assert facts['commitment_history'][-1]['owner_name'] == account.username
        audit = db.scalar(select(m.AuditLog).where(
            m.AuditLog.action == 'V2:SCHEDULING:COVERAGE_COMMITTED').order_by(m.AuditLog.id.desc()))
        assert audit.meta['metadata']['owner_principal_id'] == account.id
        assert audit.meta['metadata']['employee_id'] is None
        record_exception(db, principal=actor, shift_id=source.id,
            commitment_employee_id=ids['blair'], today=TODAY)
        assert relief.voided_at
        record_exception(db, principal=actor, shift_id=source.id, cancel_coverage=True, today=TODAY)
        assert coverage_assignments(db, source) == []
        assert db.get(m.ScheduleAttendanceEvent, original_event.id).voided_at is None
        assert db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent)) == 1
        assert db.scalar(select(func.count()).select_from(m.ScheduleCoverageCommitment)) == 3


def test_owner_partial_projection_retains_original_hours_and_no_payroll_credit(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        source = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        account = owner(db)
        record_exception(db, principal=actor, shift_id=source.id,
            commitment_owner_principal_id=account.id,
            coverage_start_time=time(13), coverage_end_time=time(17), today=TODAY)
        assert scheduled_weekly_hours(db, employee_id=ids['alex'], shift_date=DAY) == Decimal('4.00')
        board = serialize_week_board(db, week_start=per.week_start_date,
            selected_store_ids=(ids['north'], ids['south']),
            all_authorized_store_ids=(ids['north'], ids['south']), permission_flags={},
            schedule_period_id=per.id)
        card, = [row for row in board['shifts'] if row['is_coverage']]
        assert card['employee_id'] is None and card['owner_principal_id'] == account.id
        assert card['owner_name'] == account.username and not card['is_open']
        assert card['effective_hours'] == 0 and not card['requires_coverage']
        assert card['start_time'] == '13:00' and card['end_time'] == '17:00'
        assert any(card['id'] == item['id'] for group in board['groups']
            for day in group['open_days'] for item in day['shifts'])
        company = company_published_assignments(db, start_date=DAY, end_date=DAY)
        published, = [row for row in company if row.is_coverage]
        assert published.owner_name == account.username and published.employee_id is None
        assert published.effective_scheduled_hours == 0 and not published.transfer_eligible
        original, = [row for row in company if not row.is_coverage]
        assert original.employee_id == ids['alex'] and original.effective_scheduled_hours == 4


@pytest.mark.parametrize('invalid', ['inactive', 'non_owner', 'missing', 'both', 'lead', 'interval'])
def test_invalid_owner_save_rolls_back_absence_and_existing_assignment(scheduling_db, invalid):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        account = owner(db, active=invalid != 'inactive',
            role=m.PrincipalRole.MANAGER if invalid == 'non_owner' else m.PrincipalRole.ADMIN)
        kwargs = {'commitment_owner_principal_id': account.id}
        if invalid == 'missing': kwargs['commitment_owner_principal_id'] = account.id + 10000
        if invalid == 'both': kwargs['commitment_employee_id'] = ids['blair']
        if invalid == 'lead': kwargs['lead_designation'] = 'ASSIGN'
        if invalid == 'interval': kwargs['coverage_start_time'] = time(8)
        with pytest.raises((SchedulingValidationError, PermissionError)):
            record_exception(db, principal=actor, shift_id=source.id,
                event_type=m.AttendanceEventType.CALLED_OUT, today=TODAY, **kwargs)
        assert db.scalar(select(m.ScheduleAttendanceEvent)) is None
        assert db.scalar(select(m.ScheduleCoverageCommitment)) is None
        assert source.employee_id == ids['alex'] and source.owner_principal_id is None


def test_owner_conflicts_checked_between_commitments_and_regular_assignments(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        one = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        two = shift(db, actor, per, ids['blair'], ids['south'], DAY, lead=False)
        account = owner(db)
        record_exception(db, principal=actor, shift_id=one.id,
            commitment_owner_principal_id=account.id,
            coverage_start_time=time(9), coverage_end_time=time(13), today=TODAY)
        with pytest.raises(SchedulingValidationError, match='overlaps'):
            record_exception(db, principal=actor, shift_id=two.id,
                commitment_owner_principal_id=account.id,
                coverage_start_time=time(12), coverage_end_time=time(17), today=TODAY)
        values = ShiftInput(None, ids['south'], DAY, time(12), time(17), owner_principal_id=account.id)
        with pytest.raises(SchedulingValidationError, match='overlaps'):
            validate_owner_coverage(db, values, schedule_period_id=per.id, exclude_shift_id=two.id)
        record_exception(db, principal=actor, shift_id=two.id,
            commitment_owner_principal_id=account.id,
            coverage_start_time=time(13), coverage_end_time=time(17), today=TODAY)
        # Editing the same commitment must not collide with its own previous interval.
        record_exception(db, principal=actor, shift_id=one.id,
            commitment_owner_principal_id=account.id,
            coverage_start_time=time(10), coverage_end_time=time(13), today=TODAY)
        third = shift(db, actor, per, None, ids['north'], DAY, lead=False)
        third.owner_principal_id = account.id
        third.start_time = time(17); third.end_time = time(19); db.flush()
        one.end_time = time(19); db.flush()
        with pytest.raises(SchedulingValidationError, match='overlaps'):
            record_exception(db, principal=actor, shift_id=one.id,
                commitment_owner_principal_id=account.id,
                coverage_start_time=time(17), coverage_end_time=time(18), today=TODAY)


def test_owner_migration_retains_existing_employee_records_and_blocks_lossy_downgrade(scheduling_db):
    Session, actor, ids, engine = scheduling_db
    config = _alembic_config(engine.url.render_as_string(hide_password=False))
    command.downgrade(config, '20261107_0056')
    with engine.begin() as connection:
        per = connection.execute(text("""INSERT INTO schedule_periods
            (week_start_date,week_end_date,status,revision_number,created_by_principal_id,updated_by_principal_id)
            VALUES ('2030-10-27','2030-11-02','PUBLISHED',1,:actor,:actor) RETURNING id"""), {'actor': actor.id}).scalar_one()
        sid = connection.execute(text("""INSERT INTO schedule_shifts
            (schedule_period_id,employee_id,store_id,shift_date,start_time,end_time,created_by_principal_id,updated_by_principal_id)
            VALUES (:period,:employee,:store,'2030-10-27','09:00','17:00',:actor,:actor) RETURNING id"""),
            {'period': per, 'employee': ids['alex'], 'store': ids['north'], 'actor': actor.id}).scalar_one()
        cid = connection.execute(text("""INSERT INTO schedule_coverage_commitments
            (schedule_shift_id,employee_id,recorded_by_principal_id,note)
            VALUES (:shift,:employee,:actor,'Retained employee coverage') RETURNING id"""),
            {'shift': sid, 'employee': ids['blair'], 'actor': actor.id}).scalar_one()
        before = dict(connection.execute(text('SELECT * FROM schedule_coverage_commitments WHERE id=:id'), {'id': cid}).mappings().one())
    command.upgrade(config, 'head')
    assert_supported_schema(engine)
    with engine.connect() as connection:
        after = connection.execute(text('SELECT * FROM schedule_coverage_commitments WHERE id=:id'), {'id': cid}).mappings().one()
        assert all(after[key] == value for key, value in before.items())
        assert after['owner_principal_id'] is None
    with Session() as db:
        account = owner(db)
        record_exception(db, principal=actor, shift_id=sid,
            commitment_owner_principal_id=account.id, today=TODAY)
        db.commit()
    with pytest.raises(RuntimeError, match='Owner coverage'):
        command.downgrade(config, '20261107_0056')
    assert_supported_schema(engine)
    with Session() as db:
        assert db.get(m.ScheduleCoverageCommitment, cid).note == before['note']
        for employee_id, owner_id in ((None, None), (ids['blair'], account.id)):
            with pytest.raises(IntegrityError), db.begin_nested():
                db.add(m.ScheduleCoverageCommitment(schedule_shift_id=sid,
                    employee_id=employee_id, owner_principal_id=owner_id,
                    recorded_by_principal_id=actor.id, voided_at=before['created_at'],
                    voided_by_principal_id=actor.id, void_reason='Invalid synthetic identity'))
                db.flush()


def test_owner_coverage_follows_compatible_publication_lineage_and_keeps_history(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        parent_period = period(db, actor, DAY)
        source = shift(db, actor, parent_period, ids['alex'], ids['north'], DAY)
        account = owner(db)
        record_exception(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.CALLED_OUT,
            commitment_owner_principal_id=account.id, today=TODAY)
        commitment, = coverage_assignments(db, source)
        parent_period.status = m.SchedulePeriodStatus.ARCHIVED
        current_period = period(db, actor, DAY, revision=2)
        current = shift(db, actor, current_period, ids['alex'], ids['north'], DAY)
        current.source_shift_id = source.id; db.flush()
        assert coverage_assignments(db, current) == [commitment]
        effective, = effective_shift_segments(db, current)
        assert effective.owner_principal_id == account.id and effective.employee_id is None
        company = company_published_assignments(db, start_date=DAY, end_date=DAY)
        coverage, = [row for row in company if row.is_coverage]
        assert coverage.source_shift_id == current.id and coverage.owner_name == account.username
        other = shift(db, actor, current_period, ids['blair'], ids['south'], DAY, lead=False)
        with pytest.raises(SchedulingValidationError, match='overlaps'):
            record_exception(db, principal=actor, shift_id=other.id,
                commitment_owner_principal_id=account.id, today=TODAY)
        # Moving a revision invalidates prospective relief, preserving its evidence.
        current.store_id = ids['south']; db.flush()
        assert coverage_assignments(db, current) == []
        facts = exception_facts(db, current, TODAY)
        assert facts['commitment'] is None
        assert facts['commitment_history'][0]['owner_principal_id'] == account.id
        assert db.get(m.ScheduleCoverageCommitment, commitment.id).voided_at is None


def test_unresolved_attendance_does_not_make_committed_owner_available(scheduling_db):
    from datetime import datetime, timezone
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        original = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        other = shift(db, actor, per, ids['blair'], ids['south'], DAY, lead=False)
        account = owner(db)
        record_exception(db, principal=actor, shift_id=original.id,
            commitment_owner_principal_id=account.id, today=TODAY)
        for event_type in (m.AttendanceEventType.CALLED_OUT, m.AttendanceEventType.WORKED_AS_SCHEDULED):
            db.add(m.ScheduleAttendanceEvent(schedule_shift_id=original.id,
                original_employee_id=ids['alex'], event_type=event_type,
                event_at=datetime.combine(DAY, time(9), timezone.utc),
                recorded_by_principal_id=actor.id, note='Conflicting legacy evidence'))
        db.flush()
        assert effective_shift_segments(db, original) == []
        with pytest.raises(SchedulingValidationError, match='overlaps unresolved'):
            record_exception(db, principal=actor, shift_id=other.id,
                commitment_owner_principal_id=account.id, today=TODAY)
        assert len(coverage_assignments(db, original)) == 1
        assert coverage_assignments(db, other) == []


@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('actual', ['WORKED_AS_SCHEDULED', 'COVERED_SHIFT'])
def test_past_neutral_owner_relief_never_credits_original_employee_without_actual_evidence(scheduling_db, partial, actual):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_attendance_service import (
        attendance_facts_for_shift, resolve_attendance_outcome)
    from app.services.v2_scheduling_policy_service import _longview_historical_credit_rows
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        account = owner(db)
        record_exception(db, principal=actor, shift_id=source.id,
            commitment_owner_principal_id=account.id, today=TODAY,
            **({'coverage_start_time': time(13), 'coverage_end_time': time(17)} if partial else {}))
        assert db.scalar(select(m.ScheduleAttendanceEvent)) is None
        assert scheduled_weekly_hours(db, employee_id=ids['alex'], shift_date=DAY) == Decimal('4.00' if partial else '0.00')
        outcome = resolve_attendance_outcome(source, [], presumptive=True, db=db)
        assert outcome['credited_employee_ids'] == ()
        assert outcome['reason'] == 'OWNER_COVERAGE_NO_EMPLOYEE_WORKED_CREDIT'
        facts = attendance_facts_for_shift(db, shift_id=source.id, as_of_date=DAY + timedelta(days=1))
        assert facts['actual_worker_ids'] == [] and not facts['worked_as_scheduled']
        assert facts['coverage_outcome']['credited_employee_ids'] == ()
        history, = _longview_historical_credit_rows(db, store_id=ids['north'], before_date=DAY + timedelta(days=1))
        assert history['credited_employee_ids'] == ()
        event = m.ScheduleAttendanceEvent(schedule_shift_id=source.id,
            original_employee_id=ids['alex'], event_type=m.AttendanceEventType(actual),
            replacement_employee_id=ids['blair'] if actual == 'COVERED_SHIFT' else None,
            event_at=datetime.combine(DAY, time(17), timezone.utc),
            recorded_by_principal_id=actor.id, note='Explicit actual work evidence')
        db.add(event); db.flush()
        outcome = resolve_attendance_outcome(source, [event], presumptive=True, db=db)
        assert outcome['credited_employee_ids'] == (ids['blair'] if actual == 'COVERED_SHIFT' else ids['alex'],)
        assert outcome['evidence_class'] == 'CONFIRMED_EVENT'
