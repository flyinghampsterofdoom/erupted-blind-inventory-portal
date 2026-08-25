from __future__ import annotations

import os
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.auth import Principal, Role
from app.models import (
    AuditLog,
    Employee,
    EmployeeSchedulingWindow,
    Principal as PrincipalModel,
    PrincipalRole,
    SchedulePeriod,
    SchedulePeriodStatus,
    ScheduleShift,
    ScheduleWarning,
    SchedulingWindowKind,
    Store,
    StoreShift,
    TimeOffRequestStatus,
    EmployeeSchedulingProfile, EmployeeSchedulingStorePreference, ScheduleLifecycleStage,
    SchedulingOrganizationPolicy, ShiftTransferStatus, SpecialStoreParticipation,
    SpecialStoreRotationState, StorePreferenceLevel,
)
from app.schema_contract import upgrade_database
from app.services.access_control_service import fallback_allowed_for_role
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from app.services.v2_scheduling_board_service import normalize_week_start, serialize_week_board
from app.services.v2_scheduling_rules_service import (
    TimeOffInput,
    create_compensation_rate,
    create_coverage_requirement,
    create_operating_hour,
    create_scheduling_window,
    create_time_off_request,
    estimate_labor_cost,
    review_time_off_request,
    set_store_preference,
    upsert_employee_profile,
    upsert_special_hour,
)
from app.services.v2_scheduling_service import (
    ShiftInput,
    SchedulingConflict,
    SchedulingValidationError,
    clone_published_revision,
    create_draft_period,
    create_shift,
    delete_shift,
    publish_schedule,
    update_shift,
)
from app.services.v2_scheduling_policy_service import (
    assignment_score, choose_employee_for_shift, compute_automation_window, configure_special_store,
    consecutive_policy_reasons,
    create_transfer_request, evaluate_assignment, regenerate_period, respond_to_transfer, review_transfer,
    run_schedule_automation, set_publication_hold, update_organization_policy,
)
from app.services.v2_scheduling_template_service import (
    CopySelection,
    copy_schedule_periods,
    create_shift_type,
    create_time_off_reason_category,
    instantiate_schedule_template,
    save_schedule_template,
)
from app.services.v2_store_shift_service import (
    StoreShiftInput,
    copy_store_shift,
    create_store_shift,
    list_store_shifts,
    place_store_shift,
    reorder_store_shifts,
    update_store_shift,
)


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


@pytest.fixture
def scheduling_db():
    if not ADMIN_URL:
        pytest.skip('set TEST_POSTGRES_ADMIN_URL for Staff Scheduling PostgreSQL integration')
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_scheduling_{uuid.uuid4().hex[:10]}'
    database_url = f'{ADMIN_URL.rsplit("/", 1)[0]}/{database_name}'
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    upgrade_database(database_url)
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        manager_model = PrincipalModel(
            username='manager', password_hash='unused', role=PrincipalRole.MANAGER, active=True
        )
        north = Store(name='North', square_location_id='N', active=True)
        south = Store(name='South', square_location_id='S', active=True)
        db.add_all([manager_model, north, south])
        db.flush()
        alex = Employee(full_name='Alex One', normalized_name='alex one', active=True, visible_to_leads=True)
        blair = Employee(full_name='Blair Two', normalized_name='blair two', active=True, visible_to_leads=True)
        inactive = Employee(full_name='Former Person', normalized_name='former person', active=False, visible_to_leads=True)
        db.add_all([alex, blair, inactive])
        db.flush()
        manager = Principal(id=manager_model.id, username='manager', role=Role.MANAGER, store_id=None, active=True)
        general = create_shift_type(db, principal=manager, name='General')
        lead = create_shift_type(db, principal=manager, name='Lead')
        vacation = create_time_off_reason_category(db, principal=manager, name='Vacation')
        db.commit()
        ids = {
            'manager': manager_model.id,
            'north': north.id, 'south': south.id,
            'alex': alex.id, 'blair': blair.id, 'inactive': inactive.id,
            'general': general.id, 'lead': lead.id, 'vacation': vacation.id,
        }
    try:
        yield Session, manager, ids, engine
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()


def _shift(employee_id, store_id, day=date(2026, 8, 2), start=time(9), end=time(17), break_minutes=30, shift_type_id=None):
    return ShiftInput(
        employee_id=employee_id, store_id=store_id, shift_date=day,
        start_time=start, end_time=end, unpaid_break_minutes=break_minutes,
        shift_type_id=shift_type_id,
    )


def test_policy_automation_window_uses_business_timezone_and_separate_events():
    policy = SchedulingOrganizationPolicy(
        weekly_approval_hours=Decimal('40'), schedule_length_weeks=3,
        generate_days_before_end=7, publish_days_before_end=3,
        publication_local_time=time(9), timezone_name='America/Los_Angeles',
        active=True, updated_by_principal_id=1,
    )
    result = compute_automation_window(date(2026, 9, 27), policy)
    assert (result.next_start, result.next_end) == (date(2026, 9, 28), date(2026, 10, 18))
    assert result.generate_at.isoformat() == '2026-09-20T16:00:00+00:00'
    assert result.publish_at.isoformat() == '2026-09-24T16:00:00+00:00'


def test_consecutive_policy_crosses_boundaries_in_both_directions_and_enforces_split():
    friday = date(2026, 10, 2)
    prior_three = {friday, friday + timedelta(days=1), friday + timedelta(days=2)}
    blocked = consecutive_policy_reasons(work_dates=prior_three,
        proposed_date=friday + timedelta(days=3), max_consecutive_work_days=3,
        minimum_days_off_after_max_block=1)
    assert {reason.code for reason in blocked} == {'MAX_CONSECUTIVE_DAYS'}
    assert consecutive_policy_reasons(work_dates=prior_three,
        proposed_date=friday + timedelta(days=3), max_consecutive_work_days=4,
        minimum_days_off_after_max_block=1) == ()

    monday = date(2026, 10, 5)
    forward = consecutive_policy_reasons(work_dates={monday, monday + timedelta(days=1)},
        proposed_date=monday - timedelta(days=1), max_consecutive_work_days=2,
        minimum_days_off_after_max_block=1)
    assert {reason.code for reason in forward} == {'MAX_CONSECUTIVE_DAYS'}

    prior_block = {monday - timedelta(days=3), monday - timedelta(days=2)}
    too_short_break = consecutive_policy_reasons(work_dates=prior_block,
        proposed_date=monday, max_consecutive_work_days=2, minimum_days_off_after_max_block=2)
    assert {reason.code for reason in too_short_break} == {'REQUIRED_DAYS_OFF'}
    assert consecutive_policy_reasons(work_dates=prior_block,
        proposed_date=monday + timedelta(days=1), max_consecutive_work_days=2,
        minimum_days_off_after_max_block=2) == ()


def test_policy_constraints_preferences_and_manual_locks(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        upsert_employee_profile(
            db, principal=manager, employee_id=ids['alex'], home_store_id=ids['north'],
            target_weekly_hours=Decimal('32'), maximum_weekly_hours=Decimal('40'),
            max_consecutive_work_days=2, minimum_days_off_after_max_block=1,
            allowed_store_ids=(ids['north'], ids['south']),
        )
        create_scheduling_window(db, principal=manager, employee_id=ids['alex'], day_of_week=0,
                                 start_time=time(0), end_time=time(23, 59), kind=SchedulingWindowKind.HARD_UNAVAILABLE)
        result = evaluate_assignment(db, employee_id=ids['alex'], store_id=ids['north'],
                                     shift_date=date(2026, 8, 2), start_time=time(9), end_time=time(17))
        assert not result.eligible
        assert {reason.code for reason in result.reasons} == {'HARD_WEEKDAY_LOCKOUT'}

        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        first = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 8, 3)), allowed_store_ids=(ids['north'], ids['south']))
        second = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=first.version,
            values=_shift(ids['alex'], ids['north'], date(2026, 8, 4)), allowed_store_ids=(ids['north'], ids['south']))
        third = evaluate_assignment(db, employee_id=ids['alex'], store_id=ids['north'],
                                    shift_date=date(2026, 8, 5), start_time=time(9), end_time=time(17))
        assert not third.eligible and 'MAX_CONSECUTIVE_DAYS' in {r.code for r in third.reasons}
        assert db.get(ScheduleShift, first.shift_id).manually_locked is True

        set_store_preference(db, principal=manager, employee_id=ids['alex'], store_id=ids['south'],
                             preference_rank=1, preference_level=StorePreferenceLevel.PREFERRED,
                             allowed_store_ids=(ids['north'], ids['south']))
        set_store_preference(db, principal=manager, employee_id=ids['blair'], store_id=ids['south'],
                             preference_rank=3, preference_level=StorePreferenceLevel.AVOID,
                             allowed_store_ids=(ids['north'], ids['south']))
        assert assignment_score(db, employee_id=ids['alex'], store_id=ids['south'], shift_date=date(2026, 8, 6))[0] > assignment_score(
            db, employee_id=ids['blair'], store_id=ids['south'], shift_date=date(2026, 8, 6))[0]

        request = create_time_off_request(db, principal=manager,
            values=TimeOffInput(employee_id=ids['blair'], start_date=date(2026, 8, 6), end_date=date(2026, 8, 6),
                                full_day=True, reason_category_id=ids['vacation']), management_entered=True)
        review_time_off_request(db, principal=manager, request_id=request.id, status=TimeOffRequestStatus.APPROVED)
        pto = evaluate_assignment(db, employee_id=ids['blair'], store_id=ids['south'],
                                  shift_date=date(2026, 8, 6), start_time=time(9), end_time=time(17))
        assert not pto.eligible and 'APPROVED_TIME_OFF' in {r.code for r in pto.reasons}


def test_regeneration_preserves_locked_assignment_and_explains_uncovered(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        for employee_id in (ids['alex'], ids['blair']):
            upsert_employee_profile(db, principal=manager, employee_id=employee_id, home_store_id=ids['north'],
                target_weekly_hours=Decimal('32'), allowed_store_ids=(ids['north'], ids['south']))
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        locked = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 8, 3)), allowed_store_ids=(ids['north'], ids['south']))
        open_shift = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=locked.version,
            values=_shift(None, ids['north'], date(2026, 8, 4)), allowed_store_ids=(ids['north'], ids['south']))
        outcome = regenerate_period(db, principal=manager, schedule_period_id=period.id)
        assert outcome['locked_preserved'] == 1
        assert db.get(ScheduleShift, locked.shift_id).employee_id == ids['alex']
        assert db.get(ScheduleShift, open_shift.shift_id).employee_id is not None
        assert db.get(SchedulePeriod, period.id).lifecycle_stage == ScheduleLifecycleStage.REVIEW


def test_transfer_completes_normally_and_routes_overtime_to_explicit_approval(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        giver_principal = PrincipalModel(username='giver', password_hash='x', role=PrincipalRole.LEAD, active=True)
        receiver_principal = PrincipalModel(username='receiver', password_hash='x', role=PrincipalRole.LEAD, active=True)
        db.add_all([giver_principal, receiver_principal]); db.flush()
        db.get(Employee, ids['alex']).principal_id = giver_principal.id
        db.get(Employee, ids['blair']).principal_id = receiver_principal.id
        giver = Principal(id=giver_principal.id, username='giver', role=Role.LEAD, store_id=None, active=True)
        receiver = Principal(id=receiver_principal.id, username='receiver', role=Role.LEAD, store_id=None, active=True)
        upsert_employee_profile(db, principal=manager, employee_id=ids['blair'], home_store_id=ids['north'],
            target_weekly_hours=Decimal('40'), allowed_store_ids=(ids['north'], ids['south']))

        def make_week(week_start, receiver_hours):
            period = create_draft_period(db, principal=manager, week_start=week_start)
            version = 1
            remaining = receiver_hours
            day = 0
            while remaining:
                length = min(10, remaining)
                outcome = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=version,
                    values=_shift(ids['blair'], ids['north'], week_start + timedelta(days=day),
                                  start=time(8), end=time(8 + length), break_minutes=0),
                    allowed_store_ids=(ids['north'], ids['south']))
                version = outcome.version; remaining -= length; day += 1
            offered = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=version,
                values=_shift(ids['alex'], ids['north'], week_start + timedelta(days=5),
                              start=time(9), end=time(17), break_minutes=0),
                allowed_store_ids=(ids['north'], ids['south']))
            return offered.shift_id

        normal_shift_id = make_week(date(2026, 9, 6), 26)
        normal_request = create_transfer_request(db, principal=giver, shift_id=normal_shift_id,
                                                  to_employee_id=ids['blair'], today=date(2026, 8, 1))
        normal_request = respond_to_transfer(db, principal=receiver, request_id=normal_request.id, accept=True)
        assert normal_request.status == ShiftTransferStatus.COMPLETED
        assert normal_request.resulting_scheduled_hours == Decimal('34.00')
        assert db.get(ScheduleShift, normal_shift_id).employee_id == ids['blair']

        overtime_shift_id = make_week(date(2026, 9, 13), 36)
        overtime_request = create_transfer_request(db, principal=giver, shift_id=overtime_shift_id,
                                                    to_employee_id=ids['blair'], today=date(2026, 8, 1))
        overtime_request = respond_to_transfer(db, principal=receiver, request_id=overtime_request.id, accept=True)
        assert overtime_request.status == ShiftTransferStatus.PENDING_MANAGER
        assert overtime_request.resulting_scheduled_hours == Decimal('44.00')
        assert overtime_request.amount_over_threshold == Decimal('4.00')
        assert db.get(ScheduleShift, overtime_shift_id).employee_id == ids['alex']
        reviewed = review_transfer(db, principal=manager, request_id=overtime_request.id, approve=True)
        assert reviewed.status == ShiftTransferStatus.COMPLETED
        assert db.get(ScheduleShift, overtime_shift_id).employee_id == ids['blair']


def test_special_store_uses_primary_then_persistent_rotation_and_near_front_skip(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        third = Employee(full_name='Carla Three', normalized_name='carla three', active=True, visible_to_leads=True)
        db.add(third); db.flush()
        configure_special_store(db, principal=manager, store_id=ids['south'],
            primary_employee_ids=(ids['alex'],), rotation_employee_ids=(ids['blair'], third.id))
        period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        open_outcome = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(None, ids['south'], date(2026, 10, 5)), allowed_store_ids=(ids['north'], ids['south']))
        shift = db.get(ScheduleShift, open_outcome.shift_id)
        chosen, reasons = choose_employee_for_shift(db, shift=shift)
        assert chosen.id == ids['alex'] and not reasons

        create_scheduling_window(db, principal=manager, employee_id=ids['alex'], day_of_week=1,
                                 start_time=time.min, end_time=time.max, kind=SchedulingWindowKind.HARD_UNAVAILABLE)
        create_scheduling_window(db, principal=manager, employee_id=ids['blair'], day_of_week=1,
                                 start_time=time.min, end_time=time.max, kind=SchedulingWindowKind.HARD_UNAVAILABLE)
        before = db.execute(select(SpecialStoreRotationState).where(
            SpecialStoreRotationState.store_id == ids['south'],
            SpecialStoreRotationState.employee_id == ids['blair'])).scalar_one().queue_position
        chosen, _reasons = choose_employee_for_shift(db, shift=shift)
        assert chosen.id == third.id
        skipped = db.execute(select(SpecialStoreRotationState).where(
            SpecialStoreRotationState.store_id == ids['south'],
            SpecialStoreRotationState.employee_id == ids['blair'])).scalar_one()
        assert skipped.temporarily_skipped_at is not None
        assert skipped.queue_position == before + 1  # swapped one place, not sent to queue tail


def test_weekend_fairness_is_persistent_day_specific_and_respects_day_lockouts(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        for employee_id in (ids['alex'], ids['blair']):
            upsert_employee_profile(db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
                allowed_store_ids=(ids['north'], ids['south']))
        previous = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        sat = create_shift(db, principal=manager, schedule_period_id=previous.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 10, 10)),
            allowed_store_ids=(ids['north'], ids['south']))
        create_shift(db, principal=manager, schedule_period_id=previous.id, expected_version=sat.version,
            values=_shift(ids['blair'], ids['north'], date(2026, 10, 4)),
            allowed_store_ids=(ids['north'], ids['south']))
        upcoming = create_draft_period(db, principal=manager, week_start=date(2026, 10, 11))
        saturday_open = create_shift(db, principal=manager, schedule_period_id=upcoming.id, expected_version=1,
            values=_shift(None, ids['north'], date(2026, 10, 17)),
            allowed_store_ids=(ids['north'], ids['south']))
        sunday_open = create_shift(db, principal=manager, schedule_period_id=upcoming.id,
            expected_version=saturday_open.version, values=_shift(None, ids['north'], date(2026, 10, 11)),
            allowed_store_ids=(ids['north'], ids['south']))
        saturday_choice, _ = choose_employee_for_shift(db, shift=db.get(ScheduleShift, saturday_open.shift_id))
        sunday_choice, _ = choose_employee_for_shift(db, shift=db.get(ScheduleShift, sunday_open.shift_id))
        assert saturday_choice.id == ids['blair']  # Blair has fewer Saturdays.
        assert sunday_choice.id == ids['alex']  # Alex has fewer Sundays.

        create_scheduling_window(db, principal=manager, employee_id=ids['alex'], day_of_week=0,
                                 start_time=time.min, end_time=time.max,
                                 kind=SchedulingWindowKind.HARD_UNAVAILABLE)
        sunday_choice, reasons = choose_employee_for_shift(db, shift=db.get(ScheduleShift, sunday_open.shift_id))
        assert sunday_choice.id == ids['blair'] and not reasons
        saturday_choice, _ = choose_employee_for_shift(db, shift=db.get(ScheduleShift, saturday_open.shift_id))
        assert saturday_choice.id == ids['blair']  # Sunday-only lockout does not affect Saturday.
        create_scheduling_window(db, principal=manager, employee_id=ids['blair'], day_of_week=6,
                                 start_time=time.min, end_time=time.max,
                                 kind=SchedulingWindowKind.HARD_UNAVAILABLE)
        saturday_choice, _ = choose_employee_for_shift(db, shift=db.get(ScheduleShift, saturday_open.shift_id))
        sunday_choice, _ = choose_employee_for_shift(db, shift=db.get(ScheduleShift, sunday_open.shift_id))
        assert saturday_choice.id == ids['alex']
        assert sunday_choice.id == ids['blair']  # Saturday-only lockout does not affect Sunday.


def test_weekend_pto_and_consecutive_skip_do_not_complete_due_turn(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        upsert_employee_profile(db, principal=manager, employee_id=ids['alex'], home_store_id=ids['north'],
            target_weekly_hours=Decimal('40'), max_consecutive_work_days=3,
            allowed_store_ids=(ids['north'], ids['south']))
        upsert_employee_profile(db, principal=manager, employee_id=ids['blair'], home_store_id=ids['north'],
            target_weekly_hours=Decimal('40'), allowed_store_ids=(ids['north'], ids['south']))
        reason = db.get(__import__('app.models', fromlist=['TimeOffReasonCategory']).TimeOffReasonCategory, ids['vacation'])
        pto = create_time_off_request(db, principal=manager,
            values=TimeOffInput(employee_id=ids['alex'], start_date=date(2026, 10, 10),
                end_date=date(2026, 10, 10), full_day=True, reason_category_id=reason.id), management_entered=True)
        review_time_off_request(db, principal=manager, request_id=pto.id, status=TimeOffRequestStatus.APPROVED)
        period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        open_outcome = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(None, ids['north'], date(2026, 10, 10)), allowed_store_ids=(ids['north'], ids['south']))
        choice, _ = choose_employee_for_shift(db, shift=db.get(ScheduleShift, open_outcome.shift_id))
        assert choice.id == ids['blair']
        db.get(ScheduleShift, open_outcome.shift_id).employee_id = ids['blair']

        next_period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 11))
        next_open = create_shift(db, principal=manager, schedule_period_id=next_period.id, expected_version=1,
            values=_shift(None, ids['north'], date(2026, 10, 17)), allowed_store_ids=(ids['north'], ids['south']))
        choice, _ = choose_employee_for_shift(db, shift=db.get(ScheduleShift, next_open.shift_id))
        assert choice.id == ids['alex']  # PTO skip did not count as Alex's Saturday turn.

        # Locked work in the adjacent period still makes the otherwise-due employee ineligible.
        for day in (date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)):
            version = db.get(SchedulePeriod, next_period.id).version
            create_shift(db, principal=manager, schedule_period_id=next_period.id, expected_version=version,
                values=_shift(ids['alex'], ids['north'], day), allowed_store_ids=(ids['north'], ids['south']))
        choice, _ = choose_employee_for_shift(db, shift=db.get(ScheduleShift, next_open.shift_id))
        assert choice.id == ids['blair']


def test_repeated_weekend_generation_distributes_saturdays_and_sundays(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        carla = Employee(full_name='Carla Weekend', normalized_name='carla weekend', active=True, visible_to_leads=True)
        db.add(carla); db.flush()
        for employee_id in (ids['alex'], ids['blair'], carla.id):
            upsert_employee_profile(db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
                allowed_store_ids=(ids['north'], ids['south']))
        saturday_assignees, sunday_assignees = [], []
        for offset in range(3):
            week_start = date(2026, 11, 1) + timedelta(weeks=offset)
            period = create_draft_period(db, principal=manager, week_start=week_start)
            saturday = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=1,
                values=_shift(None, ids['north'], week_start + timedelta(days=6)),
                allowed_store_ids=(ids['north'], ids['south']))
            sunday = create_shift(db, principal=manager, schedule_period_id=period.id,
                expected_version=saturday.version, values=_shift(None, ids['north'], week_start),
                allowed_store_ids=(ids['north'], ids['south']))
            sat_shift, sun_shift = db.get(ScheduleShift, saturday.shift_id), db.get(ScheduleShift, sunday.shift_id)
            sat_employee, _ = choose_employee_for_shift(db, shift=sat_shift)
            sun_employee, _ = choose_employee_for_shift(db, shift=sun_shift)
            sat_shift.employee_id = sat_employee.id; sun_shift.employee_id = sun_employee.id
            db.flush()
            saturday_assignees.append(sat_employee.id); sunday_assignees.append(sun_employee.id)
        assert len(set(saturday_assignees)) == 3
        assert len(set(sunday_assignees)) == 3


def test_cross_period_locked_days_block_manager_assignment_and_transfer(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        receiver_model = PrincipalModel(username='boundary_receiver', password_hash='x',
                                        role=PrincipalRole.LEAD, active=True)
        giver_model = PrincipalModel(username='boundary_giver', password_hash='x',
                                     role=PrincipalRole.LEAD, active=True)
        db.add_all([receiver_model, giver_model]); db.flush()
        db.get(Employee, ids['alex']).principal_id = receiver_model.id
        db.get(Employee, ids['blair']).principal_id = giver_model.id
        giver = Principal(id=giver_model.id, username='boundary_giver', role=Role.LEAD,
                          store_id=None, active=True)
        upsert_employee_profile(db, principal=manager, employee_id=ids['alex'], home_store_id=ids['north'],
            target_weekly_hours=Decimal('40'), max_consecutive_work_days=3,
            allowed_store_ids=(ids['north'], ids['south']))
        previous = create_draft_period(db, principal=manager, week_start=date(2026, 11, 1))
        version = 1
        for day in (date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 7)):
            outcome = create_shift(db, principal=manager, schedule_period_id=previous.id,
                expected_version=version, values=_shift(ids['alex'], ids['north'], day),
                allowed_store_ids=(ids['north'], ids['south']))
            version = outcome.version
            assert db.get(ScheduleShift, outcome.shift_id).manually_locked
        following = create_draft_period(db, principal=manager, week_start=date(2026, 11, 8))
        with pytest.raises(SchedulingValidationError, match='4 consecutive workdays'):
            create_shift(db, principal=manager, schedule_period_id=following.id, expected_version=1,
                values=_shift(ids['alex'], ids['north'], date(2026, 11, 8)),
                allowed_store_ids=(ids['north'], ids['south']))
        offered = create_shift(db, principal=manager, schedule_period_id=following.id, expected_version=1,
            values=_shift(ids['blair'], ids['north'], date(2026, 11, 8)),
            allowed_store_ids=(ids['north'], ids['south']))
        with pytest.raises(SchedulingValidationError, match='4 consecutive workdays'):
            create_transfer_request(db, principal=giver, shift_id=offered.shift_id,
                                    to_employee_id=ids['alex'], today=date(2026, 10, 1))


def test_schedule_automation_generation_publication_and_hold_are_retry_safe(scheduling_db):
    Session, manager, _ids, _engine = scheduling_db
    with Session() as db:
        anchor = create_draft_period(db, principal=manager, week_start=date(2026, 9, 20))
        anchor.status = SchedulePeriodStatus.PUBLISHED
        anchor.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        anchor.published_at = datetime(2026, 9, 19, tzinfo=timezone.utc)
        update_organization_policy(db, principal=manager, weekly_approval_hours=Decimal('40'),
            schedule_length_weeks=1, generate_days_before_end=7, publish_days_before_end=0,
            publication_local_time=time(9), timezone_name='America/Los_Angeles')
        db.commit()
        first = run_schedule_automation(db, principal=manager,
            now=datetime(2026, 9, 20, 17, tzinfo=timezone.utc)); db.commit()
        second = run_schedule_automation(db, principal=manager,
            now=datetime(2026, 9, 20, 18, tzinfo=timezone.utc)); db.commit()
        assert len(first['generated_period_ids']) == 1
        assert second['generated_period_ids'] == []
        generated_id = first['generated_period_ids'][0]
        assert db.execute(select(func.count()).select_from(SchedulePeriod).where(
            SchedulePeriod.week_start_date == date(2026, 9, 27))).scalar_one() == 1
        set_publication_hold(db, principal=manager, schedule_period_id=generated_id,
                             held=True, reason='Manager review'); db.commit()
        held = run_schedule_automation(db, principal=manager,
            now=datetime(2026, 9, 26, 17, tzinfo=timezone.utc)); db.commit()
        assert held['blocked_period_ids'] == [generated_id]
        set_publication_hold(db, principal=manager, schedule_period_id=generated_id, held=False); db.commit()
        published = run_schedule_automation(db, principal=manager,
            now=datetime(2026, 9, 26, 18, tzinfo=timezone.utc)); db.commit()
        repeated = run_schedule_automation(db, principal=manager,
            now=datetime(2026, 9, 26, 19, tzinfo=timezone.utc)); db.commit()
        assert published['published_period_ids'] == [generated_id]
        assert repeated['published_period_ids'] == []
        assert db.get(SchedulePeriod, generated_id).status == SchedulePeriodStatus.PUBLISHED


def test_scheduling_capability_defaults_are_management_only_and_self_service_off():
    management = (
        'scheduling.view_store', 'scheduling.view_all', 'scheduling.create_draft',
        'scheduling.edit_draft_shifts', 'scheduling.delete_draft_shifts', 'scheduling.copy',
        'scheduling.manage_shift_templates', 'scheduling.store_shifts.view',
        'scheduling.store_shifts.manage', 'scheduling.store_shifts.place',
        'scheduling.manage_schedule_templates',
        'scheduling.manage_preferences', 'scheduling.manage_availability',
        'scheduling.time_off.view', 'scheduling.time_off.review',
        'scheduling.manage_operating_hours', 'scheduling.manage_special_hours',
        'scheduling.manage_coverage', 'scheduling.view_labor_cost', 'scheduling.publish',
        'scheduling.modify_published', 'scheduling.override_hard_unavailability',
        'scheduling.publish_with_warnings',
        'scheduling.generate', 'scheduling.manage_automation',
        'scheduling.manage_special_rotation', 'scheduling.approve_transfer_hours',
    )
    assert all(fallback_allowed_for_role(role=Role.ADMIN, permission_key=key) for key in management)
    assert all(fallback_allowed_for_role(role=Role.MANAGER, permission_key=key) for key in management)
    assert not any(fallback_allowed_for_role(role=Role.LEAD, permission_key=key) for key in management)
    assert not any(fallback_allowed_for_role(role=Role.STORE, permission_key=key) for key in management)
    for role in Role:
        assert not fallback_allowed_for_role(role=role, permission_key='scheduling.view_own')
        assert not fallback_allowed_for_role(role=role, permission_key='scheduling.time_off.submit_own')
        assert not fallback_allowed_for_role(role=role, permission_key='scheduling.transfer_own')


def test_scheduling_api_is_separate_feature_gated_and_csrf_protected():
    from app.main import app
    from app.routers.v2_scheduling import (
        create_draft_access,
        edit_shift_access,
        feature_access,
        manage_store_shift_access,
        place_store_shift_access,
    )
    from app.security.csrf import verify_csrf

    routes = {
        route.path: route
        for route in app.routes
        if getattr(route, 'path', '').startswith('/v2/scheduling/api')
    }
    assert '/v2/scheduling/api/periods' in routes
    assert '/v2/scheduling/api/periods/{schedule_period_id}/shifts' in routes
    period_dependencies = [row.call for row in routes['/v2/scheduling/api/periods'].dependant.dependencies]
    shift_dependencies = [row.call for row in routes['/v2/scheduling/api/periods/{schedule_period_id}/shifts'].dependant.dependencies]
    assert feature_access in period_dependencies and create_draft_access in period_dependencies
    assert feature_access in shift_dependencies and edit_shift_access in shift_dependencies
    assert verify_csrf in period_dependencies and verify_csrf in shift_dependencies
    manage_route = next(route for route in app.routes if getattr(route, 'path', '') == '/v2/scheduling/api/store-shifts' and 'POST' in route.methods)
    placement_route = routes['/v2/scheduling/api/periods/{schedule_period_id}/store-shifts/{store_shift_id}/place']
    manage_dependencies = [row.call for row in manage_route.dependant.dependencies]
    placement_dependencies = [row.call for row in placement_route.dependant.dependencies]
    assert feature_access in manage_dependencies and manage_store_shift_access in manage_dependencies
    assert feature_access in placement_dependencies and place_store_shift_access in placement_dependencies
    assert verify_csrf in manage_dependencies and verify_csrf in placement_dependencies


def test_server_rendered_scheduling_routes_separate_employee_and_admin_permissions_and_csrf():
    from app.main import app
    from app.routers.v2_scheduling import (
        automation_access, edit_shift_access, own_schedule_access, preferences_access,
        transfer_access, transfer_approval_access,
    )
    from app.security.csrf import verify_csrf
    routes = {(route.path, tuple(sorted(route.methods or ()))): route for route in app.routes
              if getattr(route, 'path', '').startswith('/v2/scheduling')}
    def dependencies(path, method):
        route = next(row for (candidate, methods), row in routes.items()
                     if candidate == path and method in methods)
        return {item.call for item in route.dependant.dependencies}
    assert preferences_access in dependencies('/v2/scheduling/rules', 'GET')
    assert own_schedule_access in dependencies('/v2/scheduling/my-schedule', 'GET')
    assert transfer_approval_access in dependencies('/v2/scheduling/transfer-approvals', 'GET')
    mutations = (
        ('/v2/scheduling/employees/{employee_id}', 'POST', preferences_access),
        ('/v2/scheduling/automation', 'POST', automation_access),
        ('/v2/scheduling/periods/{period_id}/hold', 'POST', automation_access),
        ('/v2/scheduling/shifts/{shift_id}/lock-form', 'POST', edit_shift_access),
        ('/v2/scheduling/my-schedule/transfers', 'POST', transfer_access),
        ('/v2/scheduling/my-schedule/transfers/{request_id}/respond', 'POST', transfer_access),
        ('/v2/scheduling/transfer-approvals/{request_id}', 'POST', transfer_approval_access),
    )
    for path, method, access in mutations:
        calls = dependencies(path, method)
        assert access in calls and verify_csrf in calls


def test_week_board_frontend_contracts_are_page_scoped_and_accessible():
    template = open('app/templates/v2/scheduling/week.html', encoding='utf-8').read()
    dialog = open('app/templates/v2/scheduling/_shift_dialog.html', encoding='utf-8').read()
    script = open('app/static/v2/scheduling.js', encoding='utf-8').read()
    styles = open('app/static/v2/scheduling.css', encoding='utf-8').read()
    assert 'scheduling.css' in template and 'scheduling.js' in template
    assert 'aria-live="polite"' in template
    assert 'data-shift-move' in template or 'data-shift-move' in open(
        'app/templates/v2/scheduling/_shift_card.html', encoding='utf-8'
    ).read()
    assert '<dialog' in dialog and 'Move shift' in dialog
    assert 'X-CSRF-Token' in script and 'expected_version' in script
    assert 'onpointerdown' in script and "event.key !== 'Escape'" in script
    assert 'data-tool-toggle="warnings"' in template and 'data-tool-toggle="shifts"' in template
    assert 'data-store-shift-place-form' in dialog and 'data-store-shift-form' in dialog
    assert 'shift-type' not in dialog and 'Coverage designation' not in dialog
    assert "cell?.dataset.storeId ?? board.stores[0]?.id" in script
    assert 'missing_rate_shift_count' in script and '[data-missing-rates]' in script
    assert 'prefers-reduced-motion' in styles
    assert 'grid-template-columns:220px repeat(7' in styles


def test_week_start_normalizes_to_sunday():
    assert normalize_week_start(date(2026, 8, 5)) == date(2026, 8, 2)


def test_board_serializer_redacts_private_and_labor_values(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        upsert_employee_profile(
            db, principal=manager, employee_id=ids['alex'], home_store_id=ids['north'],
            target_weekly_hours=Decimal('32'), scheduler_note='private scheduler note',
            allowed_store_ids=(ids['north'], ids['south']),
        )
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north']), allowed_store_ids=(ids['north'],),
        )
        board = serialize_week_board(
            db, week_start=date(2026, 8, 5), selected_store_ids=(ids['north'],),
            all_authorized_store_ids=(ids['north'], ids['south']),
            permission_flags={'scheduling.edit_draft_shifts': True},
        )
        serialized = repr(board)
        json.dumps(board)
        assert board['week']['start'] == '2026-08-02'
        assert board['labor'] is None
        assert 'scheduler_note' not in board['employees'][0]
        assert 'hourly_rate' not in serialized and 'private scheduler note' not in serialized
        assert all('reason' not in interval and 'note' not in interval for employee in board['employees'] for day in employee['days'] for interval in day['indicators'])


def test_draft_week_invariants_and_one_active_draft(scheduling_db):
    Session, manager, _ids, _engine = scheduling_db
    with Session() as db:
        with pytest.raises(SchedulingValidationError, match='Sunday'):
            create_draft_period(db, principal=manager, week_start=date(2026, 8, 3))
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        assert period.week_end_date == date(2026, 8, 8)
        assert period.revision_number == 1 and period.version == 1
        with pytest.raises(SchedulingConflict, match='draft already exists'):
            create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        db.commit()


def test_shift_validation_optimistic_version_and_immutable_publish(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        with pytest.raises(SchedulingValidationError):
            create_shift(
                db, principal=manager, schedule_period_id=period.id, expected_version=1,
                values=_shift(ids['alex'], ids['north'], end=time(9)), allowed_store_ids=(ids['north'],),
            )
        outcome = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], shift_type_id=ids['general']),
            allowed_store_ids=(ids['north'],),
        )
        assert outcome.version == 2
        with pytest.raises(SchedulingConflict, match='changed after'):
            update_shift(
                db, principal=manager, schedule_period_id=period.id, shift_id=outcome.shift_id,
                expected_version=1, values=_shift(ids['alex'], ids['north'], end=time(18)),
                allowed_store_ids=(ids['north'],),
            )
        published = publish_schedule(
            db, principal=manager, schedule_period_id=period.id, expected_version=2,
            allowed_store_ids=(ids['north'],),
        )
        assert published.status == SchedulePeriodStatus.PUBLISHED
        with pytest.raises(SchedulingConflict, match='immutable'):
            delete_shift(
                db, principal=manager, schedule_period_id=period.id, shift_id=outcome.shift_id,
                expected_version=published.version, allowed_store_ids=(ids['north'],),
            )
        db.commit()


def test_hard_unavailability_requires_override_and_reason(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        create_scheduling_window(
            db, principal=manager, employee_id=ids['alex'], day_of_week=0,
            start_time=time(8), end_time=time(18), kind=SchedulingWindowKind.HARD_UNAVAILABLE,
        )
        with pytest.raises(PermissionError, match='hard unavailability'):
            create_shift(
                db, principal=manager, schedule_period_id=period.id, expected_version=1,
                values=_shift(ids['alex'], ids['north']), allowed_store_ids=(ids['north'],),
            )
        with pytest.raises(SchedulingValidationError, match='override reason'):
            create_shift(
                db, principal=manager, schedule_period_id=period.id, expected_version=1,
                values=_shift(ids['alex'], ids['north']), allowed_store_ids=(ids['north'],),
                allow_hard_unavailability_override=True,
            )
        outcome = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north']), allowed_store_ids=(ids['north'],),
            allow_hard_unavailability_override=True, override_reason='Manager confirmed exception.',
        )
        warning_types = set(db.execute(select(ScheduleWarning.warning_type).where(
            ScheduleWarning.schedule_period_id == period.id)).scalars())
        assert 'HARD_UNAVAILABLE' in warning_types
        audit = db.execute(select(AuditLog).where(AuditLog.action == 'V2:SCHEDULING:SHIFT_CREATED')).scalar_one()
        assert audit.meta['reason'] == 'Manager confirmed exception.'
        assert outcome.version == 2


def test_coverage_open_shift_ignores_legacy_role_flags_and_honors_time_off(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        create_operating_hour(
            db, principal=manager, store_id=ids['north'], day_of_week=0,
            opening_time=time(9), closing_time=time(17), allowed_store_ids=(ids['north'],),
        )
        create_coverage_requirement(
            db, principal=manager, store_id=ids['north'], day_of_week=0,
            start_time=time(9), end_time=time(17), minimum_employee_count=2,
            required_shift_type_id=ids['lead'], requires_opener=True, requires_closer=True,
            allowed_store_ids=(ids['north'],),
        )
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        open_outcome = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(None, ids['north'], break_minutes=0), allowed_store_ids=(ids['north'],),
        )
        warning_types = set(db.execute(select(ScheduleWarning.warning_type).where(
            ScheduleWarning.schedule_period_id == period.id)).scalars())
        assert 'NO_ASSIGNED_EMPLOYEE' in warning_types
        assert not {'REQUIRED_ROLE_ABSENT', 'NO_OPENER', 'NO_CLOSER'} & warning_types
        assigned = ShiftInput(
            employee_id=ids['alex'], store_id=ids['north'], shift_date=date(2026, 8, 2),
            start_time=time(9), end_time=time(17), shift_type_id=ids['lead'],
            is_opener=True, is_closer=True,
        )
        assigned_outcome = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=open_outcome.version,
            values=assigned, allowed_store_ids=(ids['north'],),
        )
        warnings = db.execute(select(ScheduleWarning).where(ScheduleWarning.schedule_period_id == period.id)).scalars().all()
        assert any(row.warning_type == 'INSUFFICIENT_COVERAGE' and row.actual_count == 1 for row in warnings)
        reason = db.get(__import__('app.models', fromlist=['TimeOffReasonCategory']).TimeOffReasonCategory, ids['vacation'])
        assert reason is not None
        request = create_time_off_request(
            db, principal=manager,
            values=TimeOffInput(employee_id=ids['alex'], start_date=date(2026, 8, 2), end_date=date(2026, 8, 2),
                                full_day=True, reason_category_id=ids['vacation']),
        )
        review_time_off_request(db, principal=manager, request_id=request.id, status=TimeOffRequestStatus.APPROVED)
        assert db.get(ScheduleShift, assigned_outcome.shift_id) is not None
        assert db.execute(select(func.count()).select_from(ScheduleWarning).where(
            ScheduleWarning.schedule_period_id == period.id,
            ScheduleWarning.warning_type == 'APPROVED_TIME_OFF')).scalar_one() == 1
        upsert_special_hour(
            db, principal=manager, store_id=ids['north'], calendar_date=date(2026, 8, 2),
            event_name='Closure', closed_all_day=True, allowed_store_ids=(ids['north'],),
        )
        rebuild_schedule_warnings(db, schedule_period_id=period.id)
        assert db.execute(select(func.count()).select_from(ScheduleWarning).where(
            ScheduleWarning.schedule_period_id == period.id,
            ScheduleWarning.warning_type == 'SHIFT_ON_CLOSED_DATE')).scalar_one() == 2


def test_copy_is_independent_and_inactive_employee_is_retained_with_warning(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        source = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        source_shift = ScheduleShift(
            schedule_period_id=source.id, employee_id=ids['inactive'], store_id=ids['north'],
            shift_date=date(2026, 8, 3), start_time=time(10), end_time=time(14), unpaid_break_minutes=0,
            created_by_principal_id=manager.id, updated_by_principal_id=manager.id,
        )
        db.add(source_shift)
        db.flush()
        outcome = copy_schedule_periods(
            db, principal=manager, source_period_ids=(source.id,), target_week_start=date(2026, 8, 9),
            allowed_store_ids=(ids['north'],), mode='MERGE', selection=CopySelection(),
        )
        copied = db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == outcome.schedule_period_ids[0])).scalar_one()
        assert copied.id != source_shift.id and copied.source_shift_id == source_shift.id
        assert copied.employee_id == ids['inactive']
        source_shift.start_time = time(8)
        assert copied.start_time == time(10)
        assert db.execute(select(func.count()).select_from(ScheduleWarning).where(
            ScheduleWarning.schedule_period_id == outcome.schedule_period_ids[0],
            ScheduleWarning.warning_type == 'INACTIVE_EMPLOYEE')).scalar_one() == 1


def test_multiweek_template_instantiation_creates_independent_weekly_drafts(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        first = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        second = create_draft_period(db, principal=manager, week_start=date(2026, 8, 9))
        for period, shift_date in ((first, date(2026, 8, 2)), (second, date(2026, 8, 15))):
            db.add(ScheduleShift(
                schedule_period_id=period.id, employee_id=ids['alex'], store_id=ids['north'],
                shift_date=shift_date, start_time=time(9), end_time=time(13), unpaid_break_minutes=0,
                created_by_principal_id=manager.id, updated_by_principal_id=manager.id,
            ))
        db.flush()
        template = save_schedule_template(
            db, principal=manager, name='Two Week', source_period_ids=(first.id, second.id),
            allowed_store_ids=(ids['north'],),
        )
        assert template.week_count == 2
        outcome = instantiate_schedule_template(
            db, principal=manager, schedule_template_id=template.id,
            target_week_start=date(2026, 8, 16), allowed_store_ids=(ids['north'],), mode='MERGE',
        )
        assert len(outcome.schedule_period_ids) == 2 and outcome.shift_count == 2
        dates = db.execute(select(ScheduleShift.shift_date).where(
            ScheduleShift.schedule_period_id.in_(outcome.schedule_period_ids)).order_by(ScheduleShift.shift_date)).scalars().all()
        assert dates == [date(2026, 8, 16), date(2026, 8, 29)]


def test_effective_dated_labor_cost_subtracts_breaks_and_reports_missing_rates(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north']), allowed_store_ids=(ids['north'],),
        )
        create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=2,
            values=_shift(ids['blair'], ids['north'], day=date(2026, 8, 3), start=time(9), end=time(13), break_minutes=0),
            allowed_store_ids=(ids['north'],),
        )
        create_compensation_rate(
            db, principal=manager, employee_id=ids['alex'], effective_start_date=date(2026, 8, 1),
            hourly_rate=Decimal('20.00'),
        )
        estimate = estimate_labor_cost(
            db, schedule_period_id=period.id, permitted=True, allowed_store_ids=(ids['north'],)
        )
        assert estimate.estimated_cost == Decimal('150.00')
        assert estimate.costed_paid_hours == Decimal('7.50')
        assert estimate.missing_rate_paid_hours == Decimal('4.00')
        assert estimate.missing_rate_shift_count == 1
        with pytest.raises(PermissionError):
            estimate_labor_cost(db, schedule_period_id=period.id, permitted=False, allowed_store_ids=(ids['north'],))


def test_publish_serious_warning_policy_and_revision_supersession(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        create_operating_hour(
            db, principal=manager, store_id=ids['north'], day_of_week=0,
            opening_time=time(9), closing_time=time(17), allowed_store_ids=(ids['north'],),
        )
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        rebuild_schedule_warnings(db, schedule_period_id=period.id)
        with pytest.raises(PermissionError, match='publish with serious'):
            publish_schedule(
                db, principal=manager, schedule_period_id=period.id, expected_version=1,
                allowed_store_ids=(ids['north'],),
            )
        with pytest.raises(SchedulingValidationError, match='override reason'):
            publish_schedule(
                db, principal=manager, schedule_period_id=period.id, expected_version=1,
                allowed_store_ids=(ids['north'],),
                allow_serious_warnings=True, confirmed=True,
            )
        publish_schedule(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            allowed_store_ids=(ids['north'],),
            allow_serious_warnings=True, confirmed=True, override_reason='Coverage accepted for closure prep.',
        )
        replacement = clone_published_revision(
            db, principal=manager, published_period_id=period.id, allowed_store_ids=(ids['north'],)
        )
        publish_schedule(
            db, principal=manager, schedule_period_id=replacement.id, expected_version=replacement.version,
            allowed_store_ids=(ids['north'],),
            allow_serious_warnings=True, confirmed=True, override_reason='Replacement confirmed.',
        )
        assert db.get(SchedulePeriod, period.id).status == SchedulePeriodStatus.ARCHIVED
        assert db.get(SchedulePeriod, replacement.id).status == SchedulePeriodStatus.PUBLISHED
        actions = set(db.execute(select(AuditLog.action).where(AuditLog.action.like('V2:SCHEDULING:%'))).scalars())
        assert 'V2:SCHEDULING:SCHEDULE_PUBLISHED_WITH_WARNINGS' in actions
        assert 'V2:SCHEDULING:PUBLISHED_SCHEDULE_SUPERSEDED' in actions


def test_profile_validation_and_store_scope(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        with pytest.raises(PermissionError):
            upsert_employee_profile(
                db, principal=manager, employee_id=ids['alex'], home_store_id=ids['south'],
                target_weekly_hours=Decimal('30'), allowed_store_ids=(ids['north'],),
            )
        profile = upsert_employee_profile(
            db, principal=manager, employee_id=ids['alex'], home_store_id=ids['north'],
            target_weekly_hours=Decimal('30'), minimum_weekly_hours=Decimal('20'),
            maximum_weekly_hours=Decimal('40'), preferred_workdays=4,
            allowed_store_ids=(ids['north'],),
        )
        assert profile.home_store_id == ids['north'] and profile.preferred_workdays == 4


def test_employee_may_have_multiple_segments_at_one_store_but_only_one_store_per_day(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        first = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], start=time(8), end=time(12), break_minutes=0),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        second = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=first.version,
            values=_shift(ids['alex'], ids['north'], start=time(13), end=time(17), break_minutes=0),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        with pytest.raises(SchedulingValidationError, match='only one store per day') as rejected:
            create_shift(
                db, principal=manager, schedule_period_id=period.id, expected_version=second.version,
                values=_shift(ids['alex'], ids['south'], start=time(18), end=time(20), break_minutes=0),
                allowed_store_ids=(ids['north'], ids['south']),
            )
        assert set(rejected.value.field_errors) == {'employee_id', 'store_id'}
        with pytest.raises(SchedulingValidationError, match='already scheduled at North'):
            update_shift(
                db, principal=manager, schedule_period_id=period.id, shift_id=second.shift_id,
                expected_version=second.version,
                values=_shift(ids['alex'], ids['south'], start=time(13), end=time(17), break_minutes=0),
                allowed_store_ids=(ids['north'], ids['south']),
            )


def test_store_shift_lifecycle_placement_fill_state_copy_and_private_note(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        with pytest.raises(PermissionError, match='authorized store scope'):
            create_store_shift(
                db, principal=manager,
                values=StoreShiftInput(
                    label='Out of scope', store_id=ids['south'], start_time=time(8), end_time=time(12),
                    active_weekdays=(0,),
                ),
                allowed_store_ids=(ids['north'],),
            )
        definition = create_store_shift(
            db, principal=manager,
            values=StoreShiftInput(
                label='Morning coverage', store_id=ids['north'], start_time=time(8), end_time=time(12),
                active_weekdays=(0, 1, 2, 3, 4, 5, 6), display_order=20,
                manager_note='Manager-only preparation detail.',
            ),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        later = create_store_shift(
            db, principal=manager,
            values=StoreShiftInput(
                label='Late coverage', store_id=ids['north'], start_time=time(13), end_time=time(17),
                active_weekdays=(0, 1, 2, 3, 4, 5, 6), display_order=30,
            ),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        reordered = reorder_store_shifts(
            db, principal=manager, ordered_ids=(later.id, definition.id),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        assert [row.id for row in reordered] == [later.id, definition.id]
        copied = copy_store_shift(
            db, principal=manager, store_shift_id=definition.id,
            destination_store_id=ids['south'], label='South morning',
            allowed_store_ids=(ids['north'], ids['south']),
        )
        assert copied.id != definition.id and copied.store_id == ids['south']
        copied.start_time = time(7)
        assert definition.start_time == time(8)

        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        with pytest.raises(SchedulingValidationError, match='configured store'):
            place_store_shift(
                db, principal=manager, schedule_period_id=period.id, store_shift_id=definition.id,
                expected_version=1, shift_date=date(2026, 8, 2), employee_id=None,
                destination_store_id=ids['south'], allowed_store_ids=(ids['north'], ids['south']),
                eligible_employee_ids=(ids['alex'], ids['blair']),
            )
        sunday_only = create_store_shift(
            db, principal=manager,
            values=StoreShiftInput(
                label='Sunday only', store_id=ids['north'], start_time=time(18), end_time=time(20),
                active_weekdays=(0,),
            ),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        with pytest.raises(SchedulingValidationError, match='not active on Monday'):
            place_store_shift(
                db, principal=manager, schedule_period_id=period.id, store_shift_id=sunday_only.id,
                expected_version=1, shift_date=date(2026, 8, 3), employee_id=None,
                destination_store_id=ids['north'], allowed_store_ids=(ids['north'], ids['south']),
                eligible_employee_ids=(ids['alex'], ids['blair']),
            )
        outcome = place_store_shift(
            db, principal=manager, schedule_period_id=period.id, store_shift_id=definition.id,
            expected_version=1, shift_date=date(2026, 8, 2), employee_id=ids['alex'],
            destination_store_id=ids['north'], allowed_store_ids=(ids['north'], ids['south']),
            eligible_employee_ids=(ids['alex'], ids['blair']),
        )
        placed = db.get(ScheduleShift, outcome.shift_id)
        assert placed.source_store_shift_id == definition.id and placed.unpaid_break_minutes == 0
        with pytest.raises(SchedulingConflict):
            place_store_shift(
                db, principal=manager, schedule_period_id=period.id, store_shift_id=later.id,
                expected_version=1, shift_date=date(2026, 8, 2), employee_id=None,
                destination_store_id=ids['north'], allowed_store_ids=(ids['north'], ids['south']),
                eligible_employee_ids=(ids['alex'], ids['blair']),
            )
        open_outcome = place_store_shift(
            db, principal=manager, schedule_period_id=period.id, store_shift_id=later.id,
            expected_version=outcome.version, shift_date=date(2026, 8, 2), employee_id=None,
            destination_store_id=ids['north'], allowed_store_ids=(ids['north'], ids['south']),
            eligible_employee_ids=(ids['alex'], ids['blair']),
        )
        public_rows = list_store_shifts(
            db, allowed_store_ids=(ids['north'], ids['south']), include_inactive=False,
            include_manager_note=False, period=period,
        )
        public_definition = next(row for row in public_rows if row['id'] == definition.id)
        assert 'manager_note' not in public_definition
        assert public_definition['fill_states']['2026-08-02'] == 'assigned'
        public_later = next(row for row in public_rows if row['id'] == later.id)
        assert public_later['fill_states']['2026-08-02'] == 'open'
        managed_rows = list_store_shifts(
            db, allowed_store_ids=(ids['north'],), include_inactive=True,
            include_manager_note=True, period=period,
        )
        assert next(row for row in managed_rows if row['id'] == definition.id)['manager_note'].startswith('Manager-only')

        publish_schedule(
            db, principal=manager, schedule_period_id=period.id, expected_version=open_outcome.version,
            allowed_store_ids=(ids['north'], ids['south']),
        )
        with pytest.raises(SchedulingConflict, match='immutable'):
            place_store_shift(
                db, principal=manager, schedule_period_id=period.id, store_shift_id=sunday_only.id,
                expected_version=db.get(SchedulePeriod, period.id).version,
                shift_date=date(2026, 8, 2), employee_id=None,
                destination_store_id=ids['north'], allowed_store_ids=(ids['north'], ids['south']),
                eligible_employee_ids=(ids['alex'], ids['blair']),
            )

        update_store_shift(
            db, principal=manager, store_shift_id=definition.id,
            values=StoreShiftInput(
                label='Morning coverage', store_id=ids['north'], start_time=time(8), end_time=time(12),
                active_weekdays=(1,), active=False, display_order=10,
            ),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        assert db.get(StoreShift, definition.id).active is False
        actions = set(db.execute(select(AuditLog.action).where(AuditLog.action.like('V2:SCHEDULING:STORE_SHIFT%'))).scalars())
        assert {'V2:SCHEDULING:STORE_SHIFT_CREATED', 'V2:SCHEDULING:STORE_SHIFT_CHANGED',
                'V2:SCHEDULING:STORE_SHIFT_COPIED', 'V2:SCHEDULING:STORE_SHIFT_PLACED'} <= actions


def test_schedule_copy_rejects_cross_store_employee_day_conflict(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        source = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        create_shift(
            db, principal=manager, schedule_period_id=source.id, expected_version=1,
            values=_shift(ids['alex'], ids['north']), allowed_store_ids=(ids['north'], ids['south']),
        )
        target = create_draft_period(db, principal=manager, week_start=date(2026, 8, 9))
        create_shift(
            db, principal=manager, schedule_period_id=target.id, expected_version=1,
            values=_shift(ids['alex'], ids['south'], day=date(2026, 8, 9)),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        with pytest.raises(SchedulingValidationError, match='only one store per day'):
            copy_schedule_periods(
                db, principal=manager, source_period_ids=(source.id,),
                target_week_start=date(2026, 8, 9), allowed_store_ids=(ids['north'], ids['south']),
                mode='MERGE',
            )


def test_cross_store_invariant_covers_duplicate_template_clone_and_store_shift_placement(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        legacy = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        db.add_all([
            ScheduleShift(
                schedule_period_id=legacy.id, employee_id=ids['alex'], store_id=ids['north'],
                shift_date=date(2026, 8, 2), start_time=time(8), end_time=time(12),
                unpaid_break_minutes=0, created_by_principal_id=manager.id,
                updated_by_principal_id=manager.id,
            ),
            ScheduleShift(
                schedule_period_id=legacy.id, employee_id=ids['alex'], store_id=ids['south'],
                shift_date=date(2026, 8, 2), start_time=time(13), end_time=time(17),
                unpaid_break_minutes=0, created_by_principal_id=manager.id,
                updated_by_principal_id=manager.id,
            ),
        ])
        db.flush()
        with pytest.raises(SchedulingValidationError, match='only one store per day'):
            with db.begin_nested():
                create_shift(
                    db, principal=manager, schedule_period_id=legacy.id, expected_version=legacy.version,
                    values=_shift(ids['alex'], ids['north'], start=time(18), end=time(20), break_minutes=0),
                    allowed_store_ids=(ids['north'], ids['south']),
                )

        template = save_schedule_template(
            db, principal=manager, name='Legacy conflicting template', source_period_ids=(legacy.id,),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        with pytest.raises(SchedulingValidationError, match='only one store per day'):
            with db.begin_nested():
                instantiate_schedule_template(
                    db, principal=manager, schedule_template_id=template.id,
                    target_week_start=date(2026, 8, 9), allowed_store_ids=(ids['north'], ids['south']),
                    mode='MERGE',
                )

        publish_schedule(
            db, principal=manager, schedule_period_id=legacy.id, expected_version=legacy.version,
            allowed_store_ids=(ids['north'], ids['south']),
        )
        with pytest.raises(SchedulingValidationError, match='only one store per day'):
            with db.begin_nested():
                clone_published_revision(
                    db, principal=manager, published_period_id=legacy.id,
                    allowed_store_ids=(ids['north'], ids['south']),
                )

        placement_period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 16))
        existing = create_shift(
            db, principal=manager, schedule_period_id=placement_period.id, expected_version=1,
            values=_shift(ids['alex'], ids['south'], day=date(2026, 8, 16)),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        definition = create_store_shift(
            db, principal=manager,
            values=StoreShiftInput(
                label='North slot', store_id=ids['north'], start_time=time(8), end_time=time(12),
                active_weekdays=(0,),
            ),
            allowed_store_ids=(ids['north'], ids['south']),
        )
        with pytest.raises(SchedulingValidationError, match='only one store per day'):
            place_store_shift(
                db, principal=manager, schedule_period_id=placement_period.id,
                store_shift_id=definition.id, expected_version=existing.version,
                shift_date=date(2026, 8, 16), employee_id=ids['alex'],
                destination_store_id=ids['north'], allowed_store_ids=(ids['north'], ids['south']),
                eligible_employee_ids=(ids['alex'],),
            )


def test_period_lock_prevents_concurrent_cross_store_writes(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        db.commit()
        period_id = period.id

    barrier = Barrier(2)

    def attempt(store_id: int) -> str:
        with Session() as worker:
            barrier.wait()
            try:
                create_shift(
                    worker, principal=manager, schedule_period_id=period_id, expected_version=1,
                    values=_shift(ids['alex'], store_id),
                    allowed_store_ids=(ids['north'], ids['south']),
                )
                worker.commit()
                return 'saved'
            except SchedulingConflict:
                worker.rollback()
                return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (ids['north'], ids['south'])))
    assert sorted(results) == ['conflict', 'saved']
    with Session() as db:
        shifts = db.execute(select(ScheduleShift).where(ScheduleShift.schedule_period_id == period_id)).scalars().all()
        assert len(shifts) == 1
