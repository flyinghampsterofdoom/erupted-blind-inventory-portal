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
    AttendanceEventType,
    Employee,
    EmployeeSchedulingWindow,
    Principal as PrincipalModel,
    PrincipalRole,
    SchedulePeriod,
    SchedulePeriodStatus,
    ScheduleShift,
    ScheduleAttendanceEvent,
    ScheduleWarning,
    SchedulingWindowKind,
    Store,
    StoreShift,
    TimeOffRequestStatus,
    EmployeeSchedulingProfile, EmployeeSchedulingStorePreference, ScheduleLifecycleStage,
    SchedulingOrganizationPolicy, ShiftTransferStatus, SpecialStoreParticipation,
    ShiftTransferRequest, SpecialStoreRotationState, StorePreferenceLevel,
    SchedulingStoreDefaults,
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
    update_shift, scheduled_paid_minutes,
)
from app.services.v2_scheduling_policy_service import (
    assignment_score, automation_draft_dashboard, choose_employee_for_shift,
    compute_automation_window, configure_special_store,
    consecutive_policy_reasons,
    create_transfer_request, evaluate_assignment, regenerate_period, respond_to_transfer, review_transfer,
    ensure_rolling_schedule_horizon, manual_generate_draft_schedule,
    run_schedule_automation, set_publication_hold,
    longview_rotation_fairness, update_organization_policy, weekend_fairness,
)
from app.services.v2_scheduling_pattern_service import (
    ALTERNATING_WEEK_A_ANCHOR, alternating_week_for_date, weekdays_to_mask,
)
from app.services.v2_scheduling_roster_service import (
    list_scheduling_candidates,
    set_scheduling_capabilities,
    set_scheduling_participation,
    sync_square_scheduling_roster,
)
from app.services.v2_scheduling_assignments_service import (
    ensure_daily_lead_staffing, lead_fairness, override_double_coverage_employee,
    reconcile_lead_designations,
    set_double_coverage_store, set_lead_of_day, update_store_defaults,
)
from app.services.v2_scheduling_attendance_service import (
    attendance_facts_for_shift, record_attendance_event, void_attendance_event,
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
        alex = Employee(full_name='Alex One', normalized_name='alex one', active=True,
                        visible_to_leads=True, scheduling_lead_capable=True)
        blair = Employee(full_name='Blair Two', normalized_name='blair two', active=True,
                         visible_to_leads=True, scheduling_lead_capable=True)
        inactive = Employee(full_name='Former Person', normalized_name='former person', active=False, visible_to_leads=True)
        db.add_all([alex, blair, inactive])
        db.flush()
        manager = Principal(id=manager_model.id, username='manager', role=Role.MANAGER, store_id=None, active=True)
        general = create_shift_type(db, principal=manager, name='General')
        lead = create_shift_type(db, principal=manager, name='Lead')
        vacation = create_time_off_reason_category(db, principal=manager, name='Vacation')
        update_store_defaults(
            db, principal=manager, store_id=None,
            standard_shift_start=time(8, 45), standard_shift_end=time(22))
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


def _coverage(db, manager, ids, *, weekday=0, count=1, store_id=None):
    return create_coverage_requirement(
        db, principal=manager, store_id=store_id or ids['north'], day_of_week=weekday,
        start_time=time(9), end_time=time(21), minimum_employee_count=count,
        allowed_store_ids=(ids['north'], ids['south']))


class _TeamMembersClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def post(self, path, payload):
        self.calls.append((path, dict(payload)))
        return self.pages.pop(0)


def test_square_roster_sync_is_idempotent_and_preserves_local_scheduling_state(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        profile = upsert_employee_profile(
            db, principal=manager, employee_id=ids['alex'], home_store_id=ids['north'],
            target_weekly_hours=Decimal('32'), allowed_store_ids=(ids['north'], ids['south']))
        set_scheduling_participation(
            db, principal=manager, employee_id=ids['alex'], active=False)
        set_scheduling_capabilities(
            db, principal=manager, employee_id=ids['alex'],
            lead_capable=True, double_coverage=True)
        first = _TeamMembersClient([{'team_members': [
            {'id': 'TM-ALEX', 'given_name': 'Alex', 'family_name': 'One', 'status': 'ACTIVE',
             'assigned_locations': {'assignment_type': 'EXPLICIT_LOCATIONS', 'location_ids': ['N']}},
            {'id': 'TM-NEW', 'given_name': 'Casey', 'family_name': 'New', 'status': 'ACTIVE',
             'assigned_locations': {'assignment_type': 'ALL_CURRENT_AND_FUTURE_LOCATIONS'}},
        ]}])
        result = sync_square_scheduling_roster(db, principal=manager, client=first)
        db.flush()
        assert (result.added, result.updated, result.removed, result.unchanged) == (1, 1, 0, 0)
        alex = db.get(Employee, ids['alex'])
        casey = db.execute(select(Employee).where(
            Employee.square_team_member_id == 'TM-NEW')).scalar_one()
        assert alex.square_team_member_id == 'TM-ALEX'
        assert alex.scheduling_active is False
        assert alex.scheduling_lead_capable is True
        assert alex.scheduling_double_coverage is True
        assert db.get(EmployeeSchedulingProfile, profile.id).target_weekly_hours == Decimal('32')
        assert casey.scheduling_active is False and casey.active is True
        assert casey.principal_id is None
        assert first.calls == [('/v2/team-members/search', {'limit': 200})]
        set_scheduling_participation(
            db, principal=manager, employee_id=casey.id, active=True)
        assert casey.id in {row.id for row in list_scheduling_candidates(db)}

        second = _TeamMembersClient([{'team_members': [
            {'id': 'TM-ALEX', 'given_name': 'Alexandra', 'family_name': 'One', 'status': 'ACTIVE',
             'assigned_locations': {'assignment_type': 'EXPLICIT_LOCATIONS', 'location_ids': ['N', 'S']}},
            {'id': 'TM-NEW', 'given_name': 'Casey', 'family_name': 'New', 'status': 'ACTIVE',
             'assigned_locations': {'assignment_type': 'ALL_CURRENT_AND_FUTURE_LOCATIONS'}},
        ]}])
        changed = sync_square_scheduling_roster(db, principal=manager, client=second)
        assert (changed.added, changed.updated, changed.removed, changed.unchanged) == (0, 1, 0, 1)
        assert alex.full_name == 'Alexandra One'
        assert alex.square_location_ids == ['N', 'S']
        assert alex.scheduling_active is False
        assert alex.scheduling_lead_capable is True
        assert alex.scheduling_double_coverage is True
        assert casey.scheduling_active is True
        assert db.execute(select(func.count(Employee.id)).where(
            Employee.square_team_member_id == 'TM-ALEX')).scalar_one() == 1

        third = _TeamMembersClient([{'team_members': [
            {'id': 'TM-ALEX', 'given_name': 'Alexandra', 'family_name': 'One', 'status': 'ACTIVE',
             'assigned_locations': {'assignment_type': 'EXPLICIT_LOCATIONS', 'location_ids': ['S', 'N']}},
            {'id': 'TM-NEW', 'given_name': 'Casey', 'family_name': 'New', 'status': 'ACTIVE',
             'assigned_locations': {'assignment_type': 'ALL_CURRENT_AND_FUTURE_LOCATIONS'}},
        ]}])
        unchanged = sync_square_scheduling_roster(db, principal=manager, client=third)
        assert (unchanged.added, unchanged.updated, unchanged.removed, unchanged.unchanged) == (0, 0, 0, 2)


def test_generation_persists_exactly_one_lead_and_extra_double_coverage(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        alex = db.get(Employee, ids['alex'])
        blair = db.get(Employee, ids['blair'])
        alex.scheduling_lead_capable = True
        alex.scheduling_double_coverage = True
        blair.scheduling_lead_capable = True
        set_double_coverage_store(db, principal=manager, store_id=ids['north'])
        _coverage(db, manager, ids)
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=1,
                     values=_shift(ids['blair'], ids['north'], shift_type_id=ids['general']),
                     allowed_store_ids=(ids['north'], ids['south']))
        result = regenerate_period(db, principal=manager, schedule_period_id=period.id)
        rows = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id)).scalars())
        assert result['double_coverage']['assigned'] == 1
        assert sum(row.is_double_coverage for row in rows) == 1
        assert sum(not row.is_double_coverage for row in rows) == 1
        assert sum(row.is_lead_of_day for row in rows) == 1
        assert next(row for row in rows if row.is_double_coverage).store_id == ids['north']

        alternative = next(row for row in rows if row.employee_id == ids['blair'])
        set_lead_of_day(db, principal=manager, shift_id=alternative.id)
        assert sum(row.is_lead_of_day for row in rows) == 1
        assert alternative.is_lead_of_day is True
        assert alternative.lead_of_day_manually_assigned is True
        regenerate_period(db, principal=manager, schedule_period_id=period.id)
        preserved = db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.is_lead_of_day.is_(True))).scalar_one()
        assert preserved.employee_id == ids['blair']
        assert preserved.lead_of_day_manually_assigned is True


def test_lead_designation_rotates_deterministically_within_period(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        for offset in range(3):
            for employee_id, start_at, end_at in (
                (ids['alex'], time(8), time(16)),
                (ids['blair'], time(9), time(17)),
            ):
                db.add(ScheduleShift(
                    schedule_period_id=period.id, employee_id=employee_id,
                    store_id=ids['north'], shift_date=period.week_start_date + timedelta(days=offset),
                    start_time=start_at, end_time=end_at, unpaid_break_minutes=0,
                    created_by_principal_id=manager.id, updated_by_principal_id=manager.id,
                ))
        db.flush()
        assert reconcile_lead_designations(db, schedule_period_id=period.id) == []
        leads = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.is_lead_of_day.is_(True)).order_by(ScheduleShift.shift_date)).scalars())
        assert [row.employee_id for row in leads] == [ids['alex'], ids['blair'], ids['alex']]
        assert len({row.shift_date for row in leads}) == 3


def test_far_future_lead_designation_uses_already_planned_context(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        for employee_id in (ids['alex'], ids['blair']):
            db.get(Employee, employee_id).scheduling_active = True
            upsert_employee_profile(
                db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_shifts_per_week=3,
                target_weekly_hours=Decimal('39'),
                allowed_store_ids=(ids['north'], ids['south']))
        _coverage(db, manager, ids, weekday=1, count=2)
        first = create_draft_period(db, principal=manager, week_start=date(2026, 12, 6))
        regenerate_period(db, principal=manager, schedule_period_id=first.id)
        first_lead = db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == first.id,
            ScheduleShift.is_lead_of_day.is_(True))).scalar_one()

        second = create_draft_period(db, principal=manager, week_start=date(2026, 12, 13))
        regenerate_period(db, principal=manager, schedule_period_id=second.id)
        second_leads = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == second.id,
            ScheduleShift.is_lead_of_day.is_(True))).scalars())
        assert len(second_leads) == 1
        assert second_leads[0].employee_id != first_lead.employee_id
        assert db.execute(select(func.count()).select_from(ScheduleShift).where(
            ScheduleShift.schedule_period_id == second.id,
            ScheduleShift.employee_id.is_not(None))).scalar_one() == 2


def test_lead_fairness_separates_history_planned_future_and_current_week(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        historical = create_draft_period(db, principal=manager, week_start=date(2026, 7, 19))
        historical_shift = create_shift(
            db, principal=manager, schedule_period_id=historical.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 7, 20)),
            allowed_store_ids=(ids['north'], ids['south']))
        set_lead_of_day(db, principal=manager, shift_id=historical_shift.shift_id)
        historical.status = SchedulePeriodStatus.PUBLISHED
        historical.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED

        target = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        for employee_id, start_at in ((ids['alex'], time(8)), (ids['blair'], time(9))):
            db.add(ScheduleShift(
                schedule_period_id=target.id, employee_id=employee_id,
                store_id=ids['north'], shift_date=date(2026, 10, 5),
                start_time=start_at, end_time=time(17), unpaid_break_minutes=0,
                created_by_principal_id=manager.id, updated_by_principal_id=manager.id))
        db.flush()
        decisions = []
        assert reconcile_lead_designations(
            db, schedule_period_id=target.id, planning_date=date(2026, 10, 1),
            diagnostics=decisions) == []
        chosen = db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == target.id,
            ScheduleShift.is_lead_of_day.is_(True))).scalar_one()
        assert chosen.employee_id == ids['blair']
        assert decisions[-1]['historical_12_week_count'] == 0

        carla = Employee(
            full_name='Carla Lead', normalized_name='carla lead', active=True,
            scheduling_active=True, scheduling_lead_capable=True, visible_to_leads=True)
        dana = Employee(
            full_name='Dana Lead', normalized_name='dana lead', active=True,
            scheduling_active=True, scheduling_lead_capable=True, visible_to_leads=True)
        db.add_all([carla, dana]); db.flush()
        future = create_draft_period(db, principal=manager, week_start=date(2026, 10, 11))
        future_shift = create_shift(
            db, principal=manager, schedule_period_id=future.id, expected_version=1,
            values=_shift(carla.id, ids['north'], date(2026, 10, 12)),
            allowed_store_ids=(ids['north'], ids['south']))
        set_lead_of_day(db, principal=manager, shift_id=future_shift.shift_id)
        far = create_draft_period(db, principal=manager, week_start=date(2026, 10, 18))
        for employee_id, start_at in ((carla.id, time(8)), (dana.id, time(9))):
            db.add(ScheduleShift(
                schedule_period_id=far.id, employee_id=employee_id,
                store_id=ids['north'], shift_date=date(2026, 10, 19),
                start_time=start_at, end_time=time(17), unpaid_break_minutes=0,
                created_by_principal_id=manager.id, updated_by_principal_id=manager.id))
        db.flush()
        assert reconcile_lead_designations(
            db, schedule_period_id=far.id, planning_date=date(2026, 10, 1)) == []
        far_lead = db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == far.id,
            ScheduleShift.is_lead_of_day.is_(True))).scalar_one()
        assert far_lead.employee_id == dana.id
        carla_fairness = lead_fairness(
            db, employee_id=carla.id, before_date=date(2026, 10, 19),
            planning_date=date(2026, 10, 1), current_period_id=far.id)
        assert carla_fairness.planned_future_assignment_count == 1
        assert carla_fairness.historical_assignment_count == 0


def test_lead_designation_is_metadata_and_invalid_manual_override_is_repaired(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        for employee_id in (ids['alex'], ids['blair']):
            upsert_employee_profile(
                db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_shifts_per_week=3,
                target_weekly_hours=Decimal('40'),
                week_a_workdays_mask=weekdays_to_mask((1, 2, 3)),
                week_b_workdays_mask=weekdays_to_mask((1, 2, 3)),
                allowed_store_ids=(ids['north'], ids['south']))
        period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        outcomes = []
        version = 1
        for employee_id, start_at in ((ids['alex'], time(8)), (ids['blair'], time(9))):
            outcome = create_shift(
                db, principal=manager, schedule_period_id=period.id,
                expected_version=version,
                values=_shift(employee_id, ids['north'], date(2026, 10, 5),
                              start=start_at, end=time(17), break_minutes=0),
                allowed_store_ids=(ids['north'], ids['south']))
            outcomes.append(outcome); version = outcome.version
        before_rows = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id)).scalars())
        before = {(row.id, row.employee_id, row.start_time, row.end_time,
                   row.base_pattern_deviation_reason) for row in before_rows}
        before_masks = {row.employee_id: (row.week_a_workdays_mask, row.week_b_workdays_mask)
                        for row in db.execute(select(EmployeeSchedulingProfile)).scalars()}
        set_lead_of_day(db, principal=manager, shift_id=outcomes[0].shift_id)
        assert len(list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id)).scalars())) == 2
        after_rows = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id)).scalars())
        assert before == {(row.id, row.employee_id, row.start_time, row.end_time,
                           row.base_pattern_deviation_reason) for row in after_rows}
        assert before_masks == {row.employee_id: (row.week_a_workdays_mask, row.week_b_workdays_mask)
                                for row in db.execute(select(EmployeeSchedulingProfile)).scalars()}

        db.get(Employee, ids['alex']).scheduling_lead_capable = False
        diagnostics = []
        assert reconcile_lead_designations(
            db, schedule_period_id=period.id, planning_date=date(2026, 10, 1),
            diagnostics=diagnostics) == []
        repaired = db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.is_lead_of_day.is_(True))).scalar_one()
        assert repaired.employee_id == ids['blair']
        assert repaired.lead_of_day_manually_assigned is False
        assert any(row['action'] == 'INVALID_MANUAL_LEAD_OVERRIDE' for row in diagnostics)


def test_lead_coverage_failure_reports_pto_lockout_never_and_hour_constraints(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        carla = Employee(
            full_name='Carla Nonlead', normalized_name='carla nonlead', active=True,
            scheduling_active=True, scheduling_lead_capable=False, visible_to_leads=True)
        evan = Employee(
            full_name='Evan Lead', normalized_name='evan lead', active=True,
            scheduling_active=True, scheduling_lead_capable=True, visible_to_leads=True)
        dana = Employee(
            full_name='Dana Never', normalized_name='dana never', active=True,
            scheduling_active=True, scheduling_lead_capable=True, visible_to_leads=True)
        db.add_all([carla, evan, dana]); db.flush()
        for employee_id in (ids['alex'], ids['blair'], evan.id, dana.id):
            upsert_employee_profile(
                db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
                allowed_store_ids=(ids['north'], ids['south']))
        reason = db.get(
            __import__('app.models', fromlist=['TimeOffReasonCategory']).TimeOffReasonCategory,
            ids['vacation'])
        pto = create_time_off_request(
            db, principal=manager,
            values=TimeOffInput(
                employee_id=ids['alex'], start_date=date(2026, 10, 8),
                end_date=date(2026, 10, 8), full_day=True,
                reason_category_id=reason.id), management_entered=True)
        review_time_off_request(
            db, principal=manager, request_id=pto.id,
            status=TimeOffRequestStatus.APPROVED)
        create_scheduling_window(
            db, principal=manager, employee_id=ids['blair'], day_of_week=4,
            start_time=time.min, end_time=time.max,
            kind=SchedulingWindowKind.HARD_UNAVAILABLE)
        set_store_preference(
            db, principal=manager, employee_id=dana.id, store_id=ids['north'],
            preference_rank=None, preference_level=StorePreferenceLevel.NEVER,
            allowed_store_ids=(ids['north'], ids['south']))
        period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        for offset in (1, 2, 3):
            db.add(ScheduleShift(
                schedule_period_id=period.id, employee_id=evan.id,
                store_id=ids['north'], shift_date=period.week_start_date + timedelta(days=offset),
                start_time=time(8, 45), end_time=time(22), unpaid_break_minutes=0,
                created_by_principal_id=manager.id, updated_by_principal_id=manager.id))
        target = ScheduleShift(
            schedule_period_id=period.id, employee_id=carla.id,
            store_id=ids['north'], shift_date=date(2026, 10, 8),
            start_time=time(8, 45), end_time=time(22), unpaid_break_minutes=0,
            created_by_principal_id=manager.id, updated_by_principal_id=manager.id)
        db.add(target); db.flush()
        unresolved = ensure_daily_lead_staffing(
            db, principal=manager, schedule_period_id=period.id,
            planning_date=date(2026, 10, 1))
        failure = next(row for row in unresolved if row['date'] == '2026-10-08')
        assert {'APPROVED_TIME_OFF', 'HARD_WEEKDAY_LOCKOUT',
                'STORE_NEVER', 'WEEKLY_HOURS_APPROVAL_REQUIRED'} <= set(failure['constraints'])
        assert target.employee_id == carla.id


def test_generation_surfaces_serious_uncovered_lead_when_none_available(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        db.get(Employee, ids['alex']).scheduling_lead_capable = False
        db.get(Employee, ids['blair']).scheduling_lead_capable = False
        _coverage(db, manager, ids)
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=1,
                     values=_shift(None, ids['north'], shift_type_id=ids['general']),
                     allowed_store_ids=(ids['north'], ids['south']))
        result = regenerate_period(db, principal=manager, schedule_period_id=period.id)
        assert result['lead_uncovered'] == [{
            'date': '2026-08-02', 'reason': 'NO_ELIGIBLE_LEAD', 'constraints': []}]
        warnings = rebuild_schedule_warnings(db, schedule_period_id=period.id)
        assert any(row.warning_type == 'NO_LEAD_OF_DAY'
                   and row.severity.value == 'SERIOUS' for row in warnings)


def test_missing_double_coverage_store_is_serious_and_never_guessed(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        db.get(Employee, ids['alex']).scheduling_double_coverage = True
        _coverage(db, manager, ids)
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=1,
                     values=_shift(ids['blair'], ids['north'], shift_type_id=ids['general']),
                     allowed_store_ids=(ids['north'], ids['south']))
        result = regenerate_period(db, principal=manager, schedule_period_id=period.id)
        assert result['double_coverage']['assigned'] == 0
        assert {row['code'] for row in result['double_coverage']['uncovered']} == {
            'DOUBLE_COVERAGE_STORE_MISSING'}
        warnings = rebuild_schedule_warnings(db, schedule_period_id=period.id)
        assert any(row.warning_type == 'DOUBLE_COVERAGE_STORE_MISSING'
                   and row.severity.value == 'SERIOUS' for row in warnings)
        assert not db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.is_double_coverage.is_(True))).scalars().all()


def test_employee_roster_tabs_counts_and_search_use_scheduling_status(scheduling_db):
    from app.main import app
    from app.routers.v2_scheduling import scheduling_employees_page, scheduling_rules_page
    from starlette.requests import Request

    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        db.get(Employee, ids['inactive']).scheduling_active = False
        db.commit()

        def render(query: str = '') -> str:
            request = Request({
                'type': 'http', 'http_version': '1.1', 'method': 'GET',
                'scheme': 'http', 'path': '/v2/scheduling/employees',
                'raw_path': b'/v2/scheduling/employees', 'query_string': query.encode(),
                'headers': [], 'client': ('test', 1), 'server': ('test', 80), 'app': app,
            })
            response = scheduling_employees_page(
                request=request, _feature=manager, principal=manager, db=db)
            return response.body.decode()

        active = render()
        assert 'Alex One' in active and 'Blair Two' in active
        assert 'Former Person' not in active
        assert 'Active (2)' in active and 'Inactive (1)' in active

        inactive = render('status=inactive')
        assert 'Former Person' in inactive and 'Alex One' not in inactive

        searched = render('status=active&q=alex')
        assert 'Alex One' in searched and 'Blair Two' not in searched
        assert 'Active (2)' in searched and 'Inactive (1)' in searched

        rules_request = Request({
            'type': 'http', 'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
            'path': '/v2/scheduling/rules', 'raw_path': b'/v2/scheduling/rules',
            'query_string': b'', 'headers': [], 'client': ('test', 1),
            'server': ('test', 80), 'app': app,
        })
        rules = scheduling_rules_page(
            request=rules_request, _feature=manager, principal=manager, db=db).body.decode()
        assert 'Alex One' in rules and 'Blair Two' in rules
        assert 'Former Person' not in rules
        assert 'Scheduling: Active' in rules


def test_square_roster_sync_keeps_distinct_same_name_team_members_idempotently(scheduling_db):
    Session, manager, _ids, _engine = scheduling_db
    members = [
        {'id': 'TM-SAME-1', 'given_name': 'Jordan', 'family_name': 'Same', 'status': 'ACTIVE'},
        {'id': 'TM-SAME-2', 'given_name': 'Jordan', 'family_name': 'Same', 'status': 'INACTIVE'},
    ]
    with Session() as db:
        first = sync_square_scheduling_roster(
            db, principal=manager, client=_TeamMembersClient([{'team_members': members}]))
        rows = list(db.execute(select(Employee).where(
            Employee.square_team_member_id.in_(('TM-SAME-1', 'TM-SAME-2')))).scalars())
        assert (first.added, first.updated, first.removed, first.unchanged) == (2, 0, 0, 0)
        assert len(rows) == 2
        assert {row.full_name for row in rows} == {'Jordan Same'}
        assert len({row.normalized_name for row in rows}) == 2
        assert all(row.scheduling_active is False for row in rows)
        second = sync_square_scheduling_roster(
            db, principal=manager, client=_TeamMembersClient([{'team_members': members}]))
        assert (second.added, second.updated, second.removed, second.unchanged) == (0, 0, 0, 2)


def test_scheduling_status_and_square_status_gate_candidates_without_principal_or_history_loss(
    scheduling_db, monkeypatch,
):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        alex = db.get(Employee, ids['alex'])
        blair = db.get(Employee, ids['blair'])
        alex.principal_id = None
        alex.square_status = 'ACTIVE'
        inactive_sync = _TeamMembersClient([{'team_members': [
            {'id': 'TM-BLAIR', 'given_name': 'Blair', 'family_name': 'Two', 'status': 'INACTIVE',
             'assigned_locations': {'assignment_type': 'ALL_CURRENT_AND_FUTURE_LOCATIONS'}},
        ]}])
        sync_result = sync_square_scheduling_roster(db, principal=manager, client=inactive_sync)
        assert (sync_result.added, sync_result.updated, sync_result.removed) == (0, 1, 0)
        assert blair.square_status == 'INACTIVE' and blair.scheduling_active is True
        period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        shift = create_shift(db, principal=manager, schedule_period_id=period.id,
            expected_version=period.version, values=_shift(ids['alex'], ids['north'], day=date(2026, 10, 5)),
            allowed_store_ids=(ids['north'], ids['south']))
        assert {row.id for row in list_scheduling_candidates(db)} == {ids['alex']}
        import app.services.sales_transactions_report_service as square_transport
        monkeypatch.setattr(square_transport, '_SquareClient', lambda: pytest.fail(
            'A local scheduling-status change must not construct a Square client.'))
        set_scheduling_participation(db, principal=manager, employee_id=ids['alex'], active=False)
        assert list_scheduling_candidates(db) == []
        assert db.get(Employee, ids['alex']).active is True
        assert db.get(ScheduleShift, shift.shift_id).employee_id == ids['alex']
        blocked = evaluate_assignment(db, employee_id=ids['alex'], store_id=ids['north'],
            shift_date=date(2026, 10, 6), start_time=time(9), end_time=time(17))
        assert {reason.code for reason in blocked.reasons} == {'SCHEDULING_INACTIVE'}
        set_scheduling_participation(db, principal=manager, employee_id=ids['alex'], active=True)
        assert {row.id for row in list_scheduling_candidates(db)} == {ids['alex']}
        square_blocked = evaluate_assignment(db, employee_id=ids['blair'], store_id=ids['north'],
            shift_date=date(2026, 10, 6), start_time=time(9), end_time=time(17))
        assert 'SQUARE_INACTIVE' in {reason.code for reason in square_blocked.reasons}


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
        _coverage(db, manager, ids, weekday=2)
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


def test_coverage_generation_uses_full_standard_shifts_and_shift_targets(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        alex = db.get(Employee, ids['alex'])
        alex.scheduling_active = True
        db.get(Employee, ids['blair']).scheduling_active = False
        upsert_employee_profile(
            db, principal=manager, employee_id=alex.id, home_store_id=ids['north'],
            target_shifts_per_week=3, target_weekly_hours=Decimal('39'),
            allowed_store_ids=(ids['north'], ids['south']))
        for weekday in (1, 2, 3):
            _coverage(db, manager, ids, weekday=weekday)
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))

        result = regenerate_period(db, principal=manager, schedule_period_id=period.id)
        rows = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.is_double_coverage.is_(False)).order_by(ScheduleShift.shift_date)).scalars())

        assert len(rows) == 3
        assert all((row.start_time, row.end_time) == (time(8, 45), time(22)) for row in rows)
        assert all(row.employee_id == alex.id for row in rows)
        assert sum(scheduled_paid_minutes(row) for row in rows) == 2385
        assert result['positions']['created'] == 3
        assert next(row for row in result['shift_targets'] if row['employee_id'] == alex.id) == {
            'employee_id': alex.id, 'target_shifts': 3, 'assigned_shifts': 3}


def test_generation_fails_without_defaults_and_preserves_locked_custom_time(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        db.get(Employee, ids['alex']).scheduling_active = True
        _coverage(db, manager, ids, weekday=1, count=2)
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 2))
        locked = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 8, 3),
                          start=time(10), end=time(18), break_minutes=30),
            allowed_store_ids=(ids['north'], ids['south']))
        defaults = db.execute(select(SchedulingStoreDefaults)).scalar_one()
        defaults.standard_shift_start = None
        defaults.standard_shift_end = None
        with pytest.raises(SchedulingValidationError, match='Standard Shift'):
            regenerate_period(db, principal=manager, schedule_period_id=period.id)
        assert db.get(ScheduleShift, locked.shift_id).start_time == time(10)
        assert db.execute(select(func.count()).select_from(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id)).scalar_one() == 1

        defaults.standard_shift_start = time(8, 45)
        defaults.standard_shift_end = time(22)
        outcome = regenerate_period(db, principal=manager, schedule_period_id=period.id)
        rows = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id)).scalars())
        assert outcome['positions']['preserved'] == 1
        assert len(rows) == 2
        assert db.get(ScheduleShift, locked.shift_id).start_time == time(10)
        generated = next(row for row in rows if row.id != locked.shift_id)
        assert generated.generated_from_coverage_requirement is True
        assert (generated.start_time, generated.end_time) == (time(8, 45), time(22))


def test_rolling_horizon_establishes_eight_weeks_then_appends_only_far_week(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        _coverage(db, manager, ids, weekday=0)
        update_organization_policy(
            db, principal=manager, weekly_approval_hours=Decimal('40'),
            schedule_length_weeks=8, generate_days_before_end=7,
            publish_days_before_end=3, publication_local_time=time(9),
            timezone_name='America/Los_Angeles')
        live = create_draft_period(db, principal=manager, week_start=date(2026, 8, 23))
        manual = create_shift(
            db, principal=manager, schedule_period_id=live.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 8, 23),
                          start=time(10), end=time(18), break_minutes=30),
            allowed_store_ids=(ids['north'], ids['south']))
        live.status = SchedulePeriodStatus.PUBLISHED
        live.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        live.published_at = datetime(2026, 8, 22, tzinfo=timezone.utc)
        original_live_version = live.version
        db.flush()

        first = ensure_rolling_schedule_horizon(
            db, principal=manager, now=datetime(2026, 8, 25, 18, tzinfo=timezone.utc))
        assert first['horizon_weeks'] == 8
        assert len(first['created_period_ids']) == 7
        planned_before = list(db.execute(select(SchedulePeriod).where(
            SchedulePeriod.week_start_date.between(date(2026, 8, 23), date(2026, 10, 11))
        ).order_by(SchedulePeriod.week_start_date)).scalars())
        snapshot = {row.week_start_date: (row.id, row.version, row.status) for row in planned_before}
        assert [row.alternating_week for row in planned_before] == ['B', 'A', 'B', 'A', 'B', 'A', 'B', 'A']

        repeated = ensure_rolling_schedule_horizon(
            db, principal=manager, now=datetime(2026, 8, 25, 19, tzinfo=timezone.utc))
        assert repeated['created_period_ids'] == []
        advanced = ensure_rolling_schedule_horizon(
            db, principal=manager, now=datetime(2026, 8, 30, 18, tzinfo=timezone.utc))
        assert len(advanced['created_period_ids']) == 1
        appended = db.get(SchedulePeriod, advanced['created_period_ids'][0])
        assert appended.week_start_date == date(2026, 10, 18)
        for week_start, state in snapshot.items():
            row = db.get(SchedulePeriod, state[0])
            assert (row.id, row.version, row.status) == state
        assert db.get(SchedulePeriod, live.id).version == original_live_version
        locked = db.get(ScheduleShift, manual.shift_id)
        assert locked.manually_locked and (locked.start_time, locked.end_time) == (time(10), time(18))


def test_alternating_base_patterns_recover_after_pto_exception(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        alex = db.get(Employee, ids['alex'])
        alex.scheduling_active = True
        db.get(Employee, ids['blair']).scheduling_active = False
        upsert_employee_profile(
            db, principal=manager, employee_id=alex.id, home_store_id=ids['north'],
            target_shifts_per_week=3, target_weekly_hours=Decimal('39'),
            week_a_workdays_mask=weekdays_to_mask((1, 3, 6)),
            week_b_workdays_mask=weekdays_to_mask((0, 2, 4)),
            allowed_store_ids=(ids['north'], ids['south']))
        for weekday in range(7):
            _coverage(db, manager, ids, weekday=weekday)

        first_a_start = ALTERNATING_WEEK_A_ANCHOR + timedelta(weeks=36)
        first_a = create_draft_period(db, principal=manager, week_start=first_a_start)
        regenerate_period(db, principal=manager, schedule_period_id=first_a.id)
        first_dates = {row.shift_date for row in db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == first_a.id,
            ScheduleShift.employee_id == alex.id)).scalars()}
        assert alternating_week_for_date(first_a_start) == 'A'
        assert first_dates == {first_a_start + timedelta(days=value) for value in (1, 3, 6)}

        b_start = first_a_start + timedelta(weeks=1)
        week_b = create_draft_period(db, principal=manager, week_start=b_start)
        regenerate_period(db, principal=manager, schedule_period_id=week_b.id)
        b_dates = {row.shift_date for row in db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == week_b.id,
            ScheduleShift.employee_id == alex.id)).scalars()}
        assert week_b.alternating_week == 'B'
        assert b_dates == {b_start + timedelta(days=value) for value in (0, 2, 4)}

        exception_start = first_a_start + timedelta(weeks=2)
        pto = create_time_off_request(
            db, principal=manager,
            values=TimeOffInput(
                employee_id=alex.id, start_date=exception_start + timedelta(days=1),
                end_date=exception_start + timedelta(days=1), full_day=True,
                reason_category_id=ids['vacation']), management_entered=True)
        review_time_off_request(
            db, principal=manager, request_id=pto.id,
            status=TimeOffRequestStatus.APPROVED)
        exception = create_draft_period(db, principal=manager, week_start=exception_start)
        diagnostics = regenerate_period(db, principal=manager, schedule_period_id=exception.id)
        exception_rows = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == exception.id,
            ScheduleShift.employee_id == alex.id)).scalars())
        assert len(exception_rows) == 3
        assert exception_start + timedelta(days=1) not in {row.shift_date for row in exception_rows}
        assert any(row.base_pattern_deviation_reason == 'APPROVED_PTO' for row in exception_rows)
        assert diagnostics['base_pattern_week'] == 'A'

        profile = db.execute(select(EmployeeSchedulingProfile).where(
            EmployeeSchedulingProfile.employee_id == alex.id)).scalar_one()
        assert profile.week_a_workdays_mask == weekdays_to_mask((1, 3, 6))
        assert profile.week_b_workdays_mask == weekdays_to_mask((0, 2, 4))

        recovery_start = exception_start + timedelta(weeks=2)
        recovery = create_draft_period(db, principal=manager, week_start=recovery_start)
        regenerate_period(db, principal=manager, schedule_period_id=recovery.id)
        recovery_dates = {row.shift_date for row in db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == recovery.id,
            ScheduleShift.employee_id == alex.id)).scalars()}
        assert recovery_dates == {recovery_start + timedelta(days=value) for value in (1, 3, 6)}
        assert db.execute(select(func.count()).select_from(ScheduleShift).where(
            ScheduleShift.schedule_period_id == recovery.id,
            ScheduleShift.is_lead_of_day.is_(True))).scalar_one() == 3


def test_new_week_base_pattern_respects_prior_planned_consecutive_boundary(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        alex = db.get(Employee, ids['alex'])
        alex.scheduling_active = True
        db.get(Employee, ids['blair']).scheduling_active = False
        week_start = ALTERNATING_WEEK_A_ANCHOR + timedelta(weeks=42)
        upsert_employee_profile(
            db, principal=manager, employee_id=alex.id, home_store_id=ids['north'],
            target_shifts_per_week=3, target_weekly_hours=Decimal('39'),
            week_a_workdays_mask=weekdays_to_mask((0, 1, 2)),
            week_b_workdays_mask=weekdays_to_mask((0, 1, 2)),
            max_consecutive_work_days=3, minimum_days_off_after_max_block=1,
            allowed_store_ids=(ids['north'], ids['south']))
        for weekday in range(7):
            _coverage(db, manager, ids, weekday=weekday)
        previous = create_draft_period(
            db, principal=manager, week_start=week_start - timedelta(weeks=1))
        version = 1
        for offset in (4, 5, 6):
            outcome = create_shift(
                db, principal=manager, schedule_period_id=previous.id,
                expected_version=version,
                values=_shift(alex.id, ids['north'], previous.week_start_date + timedelta(days=offset),
                              start=time(8, 45), end=time(22), break_minutes=0),
                allowed_store_ids=(ids['north'], ids['south']))
            version = outcome.version

        current = create_draft_period(db, principal=manager, week_start=week_start)
        diagnostics = regenerate_period(db, principal=manager, schedule_period_id=current.id)
        assigned = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == current.id,
            ScheduleShift.employee_id == alex.id)).scalars())
        assert week_start not in {row.shift_date for row in assigned}
        assert any(item['reason'] == 'CONSECUTIVE_DAY_AVOIDANCE'
                   for item in diagnostics['base_pattern_deviations'])


def test_attendance_callout_and_coverage_preserve_published_schedule_truth(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 23))
        outcome = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['south'], date(2026, 8, 23),
                          start=time(8, 45), end=time(22), break_minutes=0),
            allowed_store_ids=(ids['north'], ids['south']))
        shift = db.get(ScheduleShift, outcome.shift_id)
        shift.is_lead_of_day = True
        period.status = SchedulePeriodStatus.PUBLISHED
        period.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        period.published_at = datetime(2026, 8, 20, 17, tzinfo=timezone.utc)
        period.published_by_principal_id = manager.id
        original = (shift.employee_id, shift.store_id, shift.shift_date, shift.start_time,
                    shift.end_time, shift.is_lead_of_day, shift.base_pattern_deviation_reason)

        callout = record_attendance_event(
            db, principal=manager, shift_id=shift.id,
            event_type=AttendanceEventType.CALLED_OUT,
            event_at=datetime(2026, 8, 23, 14, tzinfo=timezone.utc),
            note='Reported illness', today=date(2026, 8, 28))
        coverage = record_attendance_event(
            db, principal=manager, shift_id=shift.id,
            event_type=AttendanceEventType.COVERED_SHIFT,
            replacement_employee_id=ids['blair'],
            event_at=datetime(2026, 8, 23, 15, tzinfo=timezone.utc),
            note='Covered full shift', today=date(2026, 8, 28))

        assert callout.event.original_employee_id == ids['alex']
        assert coverage.event.original_employee_id == ids['alex']
        assert coverage.event.replacement_employee_id == ids['blair']
        assert callout.event.recorded_by_principal_id == manager.id
        assert callout.event.created_at is not None and callout.event.note == 'Reported illness'
        assert original == (
            shift.employee_id, shift.store_id, shift.shift_date, shift.start_time,
            shift.end_time, shift.is_lead_of_day, shift.base_pattern_deviation_reason)
        facts = attendance_facts_for_shift(db, shift_id=shift.id)
        assert facts['scheduled_employee_id'] == ids['alex']
        assert facts['scheduled_employee_absent'] is True
        assert facts['replacement_employee_ids'] == [ids['blair']]
        assert facts['actual_worker_ids'] == [ids['blair']]
        assert facts['is_weekend'] is True
        assert facts['scheduled_lead_of_day'] is True
        assert db.execute(select(func.count()).select_from(ShiftTransferRequest)).scalar_one() == 0
        board = serialize_week_board(
            db, week_start=period.week_start_date,
            selected_store_ids=(ids['north'], ids['south']),
            all_authorized_store_ids=(ids['north'], ids['south']),
            permission_flags={'scheduling.attendance.record': True},
            schedule_period_id=period.id)
        board_shift = next(row for row in board['shifts'] if row['id'] == shift.id)
        assert board_shift['attendance_statuses'] == ['Called Out', 'Covered Shift']
        assert board_shift['can_record_attendance'] is True
        assert board_shift['attendance_events'][1]['replacement_employee_name'] == 'Blair Two'

        period.status = SchedulePeriodStatus.ARCHIVED
        replacement_period = SchedulePeriod(
            week_start_date=period.week_start_date, week_end_date=period.week_end_date,
            status=SchedulePeriodStatus.PUBLISHED,
            lifecycle_stage=ScheduleLifecycleStage.PUBLISHED,
            revision_number=2, supersedes_schedule_period_id=period.id,
            created_by_principal_id=manager.id, updated_by_principal_id=manager.id,
            published_by_principal_id=manager.id,
            published_at=datetime(2026, 8, 24, 17, tzinfo=timezone.utc))
        db.add(replacement_period); db.flush()
        replacement_shift = ScheduleShift(
            schedule_period_id=replacement_period.id, employee_id=ids['blair'],
            store_id=ids['south'], shift_date=shift.shift_date,
            start_time=shift.start_time, end_time=shift.end_time,
            unpaid_break_minutes=shift.unpaid_break_minutes, source_shift_id=shift.id,
            created_by_principal_id=manager.id, updated_by_principal_id=manager.id)
        db.add(replacement_shift); db.flush()
        assert callout.event.schedule_shift_id == shift.id
        assert coverage.event.schedule_shift_id == shift.id
        assert attendance_facts_for_shift(db, shift_id=shift.id)['scheduled_employee_id'] == ids['alex']
        assert attendance_facts_for_shift(
            db, shift_id=replacement_shift.id)['events'] == []


def test_attendance_coverage_validation_overtime_and_audited_correction(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        upsert_employee_profile(
            db, principal=manager, employee_id=ids['blair'],
            home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
            approval_weekly_hours=Decimal('40'),
            week_a_workdays_mask=weekdays_to_mask((0, 1, 2)),
            week_b_workdays_mask=weekdays_to_mask((0, 1, 2)),
            allowed_store_ids=(ids['north'], ids['south']))
        set_store_preference(
            db, principal=manager, employee_id=ids['blair'], store_id=ids['south'],
            preference_rank=None, preference_level=StorePreferenceLevel.NEVER,
            allowed_store_ids=(ids['north'], ids['south']))
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 23))
        target = ScheduleShift(
            schedule_period_id=period.id, employee_id=ids['alex'], store_id=ids['south'],
            shift_date=date(2026, 8, 26), start_time=time(8, 45), end_time=time(22),
            unpaid_break_minutes=0, created_by_principal_id=manager.id,
            updated_by_principal_id=manager.id)
        db.add(target)
        for offset in (0, 1, 2):
            db.add(ScheduleShift(
                schedule_period_id=period.id, employee_id=ids['blair'], store_id=ids['north'],
                shift_date=date(2026, 8, 23) + timedelta(days=offset),
                start_time=time(8, 45), end_time=time(22), unpaid_break_minutes=0,
                created_by_principal_id=manager.id, updated_by_principal_id=manager.id))
        period.status = SchedulePeriodStatus.PUBLISHED
        period.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        period.published_at = datetime(2026, 8, 20, 17, tzinfo=timezone.utc)
        period.published_by_principal_id = manager.id
        db.flush()

        with pytest.raises(SchedulingValidationError, match='existing replacement'):
            record_attendance_event(
                db, principal=manager, shift_id=target.id,
                event_type=AttendanceEventType.COVERED_SHIFT,
                replacement_employee_id=999999,
                event_at=datetime(2026, 8, 26, 16, tzinfo=timezone.utc),
                today=date(2026, 8, 28))
        with pytest.raises(SchedulingValidationError, match='Never'):
            record_attendance_event(
                db, principal=manager, shift_id=target.id,
                event_type=AttendanceEventType.COVERED_SHIFT,
                replacement_employee_id=ids['blair'],
                event_at=datetime(2026, 8, 26, 16, tzinfo=timezone.utc),
                today=date(2026, 8, 28))

        result = record_attendance_event(
            db, principal=manager, shift_id=target.id,
            event_type=AttendanceEventType.COVERED_SHIFT,
            replacement_employee_id=ids['blair'],
            event_at=datetime(2026, 8, 26, 16, tzinfo=timezone.utc),
            override_store_restriction=True,
            override_reason='Employee confirms emergency coverage was worked',
            today=date(2026, 8, 28))
        assert set(result.warnings) == {
            'STORE_NEVER_OVERRIDDEN', 'ACTUAL_COVERAGE_OVER_APPROVAL_THRESHOLD'}
        assert result.resulting_hours == Decimal('53.00')
        assert result.approval_threshold_hours == Decimal('40.00')
        assert db.get(ScheduleShift, target.id).employee_id == ids['alex']

        voided = void_attendance_event(
            db, principal=manager, event_id=result.event.id,
            reason='Wrong replacement selected')
        assert voided.voided_at is not None
        assert voided.voided_by_principal_id == manager.id
        assert voided.void_reason == 'Wrong replacement selected'
        assert db.get(ScheduleAttendanceEvent, result.event.id) is voided
        audit_actions = set(db.execute(select(AuditLog.action).where(
            AuditLog.action.like('V2:SCHEDULING_ATTENDANCE:%'))).scalars())
        assert {'V2:SCHEDULING_ATTENDANCE:ATTENDANCE_EVENT_RECORDED',
                'V2:SCHEDULING_ATTENDANCE:ATTENDANCE_EVENT_VOIDED'} <= audit_actions


@pytest.mark.parametrize('event_type', [
    AttendanceEventType.WORKED_AS_SCHEDULED,
    AttendanceEventType.CALLED_OUT,
    AttendanceEventType.LATE,
    AttendanceEventType.OPENED_STORE_LATE,
    AttendanceEventType.NO_CALL_NO_SHOW,
])
def test_attendance_event_types_are_additive_and_do_not_regenerate(event_type, scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        profile = upsert_employee_profile(
            db, principal=manager, employee_id=ids['alex'],
            home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
            week_a_workdays_mask=weekdays_to_mask((0, 2, 4)),
            week_b_workdays_mask=weekdays_to_mask((1, 3, 5)),
            allowed_store_ids=(ids['north'], ids['south']))
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 23))
        outcome = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 8, 25)),
            allowed_store_ids=(ids['north'], ids['south']))
        period.status = SchedulePeriodStatus.PUBLISHED
        period.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        period.published_at = datetime(2026, 8, 20, 17, tzinfo=timezone.utc)
        period.published_by_principal_id = manager.id
        db.flush()
        before_masks = (profile.week_a_workdays_mask, profile.week_b_workdays_mask)
        before_periods = db.execute(select(func.count()).select_from(SchedulePeriod)).scalar_one()
        before_shifts = db.execute(select(func.count()).select_from(ScheduleShift)).scalar_one()
        shift = db.get(ScheduleShift, outcome.shift_id)
        before_shift = (shift.employee_id, shift.store_id, shift.shift_date,
                        shift.start_time, shift.end_time, shift.source_shift_id)

        record_attendance_event(
            db, principal=manager, shift_id=shift.id, event_type=event_type,
            event_at=datetime(2026, 8, 25, 17, tzinfo=timezone.utc),
            note='Explicit fact', today=date(2026, 8, 28))
        assert before_masks == (profile.week_a_workdays_mask, profile.week_b_workdays_mask)
        assert before_periods == db.execute(select(func.count()).select_from(SchedulePeriod)).scalar_one()
        assert before_shifts == db.execute(select(func.count()).select_from(ScheduleShift)).scalar_one()
        assert before_shift == (shift.employee_id, shift.store_id, shift.shift_date,
                                shift.start_time, shift.end_time, shift.source_shift_id)


def test_attendance_rejects_draft_future_and_conflicting_outcomes(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=manager, week_start=date(2026, 8, 30))
        outcome = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 8, 30)),
            allowed_store_ids=(ids['north'], ids['south']))
        with pytest.raises(SchedulingValidationError, match='published'):
            record_attendance_event(
                db, principal=manager, shift_id=outcome.shift_id,
                event_type=AttendanceEventType.CALLED_OUT,
                event_at=datetime(2026, 8, 28, 17, tzinfo=timezone.utc),
                today=date(2026, 8, 28))
        period.status = SchedulePeriodStatus.PUBLISHED
        period.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        period.published_at = datetime(2026, 8, 28, 17, tzinfo=timezone.utc)
        with pytest.raises(SchedulingValidationError, match='before the scheduled date'):
            record_attendance_event(
                db, principal=manager, shift_id=outcome.shift_id,
                event_type=AttendanceEventType.CALLED_OUT,
                event_at=datetime(2026, 8, 28, 17, tzinfo=timezone.utc),
                today=date(2026, 8, 28))

        shift = db.get(ScheduleShift, outcome.shift_id)
        shift.shift_date = date(2026, 8, 28)
        record_attendance_event(
            db, principal=manager, shift_id=shift.id,
            event_type=AttendanceEventType.CALLED_OUT,
            event_at=datetime(2026, 8, 28, 17, tzinfo=timezone.utc),
            today=date(2026, 8, 28))
        with pytest.raises(SchedulingValidationError, match='conflicts'):
            record_attendance_event(
                db, principal=manager, shift_id=shift.id,
                event_type=AttendanceEventType.WORKED_AS_SCHEDULED,
                event_at=datetime(2026, 8, 28, 18, tzinfo=timezone.utc),
                today=date(2026, 8, 28))


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

        def make_week(week_start, receiver_shift_count):
            period = create_draft_period(db, principal=manager, week_start=week_start)
            version = 1
            for day in range(receiver_shift_count):
                outcome = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=version,
                    values=_shift(ids['blair'], ids['north'], week_start + timedelta(days=day),
                                  start=time(8, 45), end=time(22), break_minutes=0),
                    allowed_store_ids=(ids['north'], ids['south']))
                version = outcome.version
            offered = create_shift(db, principal=manager, schedule_period_id=period.id, expected_version=version,
                values=_shift(ids['alex'], ids['north'], week_start + timedelta(days=5),
                              start=time(8, 45), end=time(22), break_minutes=0),
                allowed_store_ids=(ids['north'], ids['south']))
            return offered.shift_id

        normal_shift_id = make_week(date(2026, 9, 6), 2)
        normal_request = create_transfer_request(db, principal=giver, shift_id=normal_shift_id,
                                                  to_employee_id=ids['blair'], today=date(2026, 8, 1))
        normal_request = respond_to_transfer(db, principal=receiver, request_id=normal_request.id, accept=True)
        assert normal_request.status == ShiftTransferStatus.COMPLETED
        assert normal_request.existing_scheduled_hours == Decimal('26.50')
        assert normal_request.shift_hours == Decimal('13.25')
        assert normal_request.resulting_scheduled_hours == Decimal('39.75')
        assert db.get(ScheduleShift, normal_shift_id).employee_id == ids['blair']

        overtime_shift_id = make_week(date(2026, 9, 13), 3)
        overtime_request = create_transfer_request(db, principal=giver, shift_id=overtime_shift_id,
                                                    to_employee_id=ids['blair'], today=date(2026, 8, 1))
        overtime_request = respond_to_transfer(db, principal=receiver, request_id=overtime_request.id, accept=True)
        assert overtime_request.status == ShiftTransferStatus.PENDING_MANAGER
        assert overtime_request.existing_scheduled_hours == Decimal('39.75')
        assert overtime_request.shift_hours == Decimal('13.25')
        assert overtime_request.resulting_scheduled_hours == Decimal('53.00')
        assert overtime_request.amount_over_threshold == Decimal('13.00')
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


def test_far_future_longview_generation_uses_already_planned_rotation_context(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        db.get(Employee, ids['alex']).scheduling_active = False
        carla = Employee(
            full_name='Carla Longview', normalized_name='carla longview', active=True,
            scheduling_active=True, scheduling_lead_capable=True, visible_to_leads=True)
        db.add(carla); db.flush()
        configure_special_store(
            db, principal=manager, store_id=ids['south'], primary_employee_ids=(),
            rotation_employee_ids=(ids['blair'], carla.id))
        _coverage(db, manager, ids, weekday=1, store_id=ids['south'])

        first = create_draft_period(db, principal=manager, week_start=date(2026, 11, 1))
        regenerate_period(db, principal=manager, schedule_period_id=first.id)
        first_assignee = db.execute(select(ScheduleShift.employee_id).where(
            ScheduleShift.schedule_period_id == first.id,
            ScheduleShift.store_id == ids['south'],
            ScheduleShift.employee_id.is_not(None))).scalar_one()
        assert first_assignee == ids['blair']

        far_future = create_draft_period(db, principal=manager, week_start=date(2026, 11, 8))
        regenerate_period(db, principal=manager, schedule_period_id=far_future.id)
        next_assignee = db.execute(select(ScheduleShift.employee_id).where(
            ScheduleShift.schedule_period_id == far_future.id,
            ScheduleShift.store_id == ids['south'],
            ScheduleShift.employee_id.is_not(None))).scalar_one()
        assert next_assignee == carla.id


def test_longview_rotation_separates_history_future_and_base_pattern(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        configure_special_store(
            db, principal=manager, store_id=ids['south'], primary_employee_ids=(),
            rotation_employee_ids=(ids['alex'], ids['blair']))
        alex_profile = upsert_employee_profile(
            db, principal=manager, employee_id=ids['alex'], home_store_id=ids['north'],
            target_shifts_per_week=3, target_weekly_hours=Decimal('40'),
            week_a_workdays_mask=weekdays_to_mask((1,)),
            week_b_workdays_mask=weekdays_to_mask((1,)),
            special_store_participation=SpecialStoreParticipation.ROTATION,
            allowed_store_ids=(ids['north'], ids['south']))
        blair_profile = upsert_employee_profile(
            db, principal=manager, employee_id=ids['blair'], home_store_id=ids['north'],
            target_shifts_per_week=3, target_weekly_hours=Decimal('40'),
            week_a_workdays_mask=weekdays_to_mask((2, 3, 4)),
            week_b_workdays_mask=weekdays_to_mask((2, 3, 4)),
            special_store_participation=SpecialStoreParticipation.ROTATION,
            allowed_store_ids=(ids['north'], ids['south']))

        tied = create_draft_period(db, principal=manager, week_start=date(2026, 9, 6))
        tied_open = create_shift(
            db, principal=manager, schedule_period_id=tied.id, expected_version=1,
            values=_shift(None, ids['south'], date(2026, 9, 7)),
            allowed_store_ids=(ids['north'], ids['south']))
        tied_choice, _ = choose_employee_for_shift(
            db, shift=db.get(ScheduleShift, tied_open.shift_id),
            planning_date=date(2026, 9, 6))
        assert tied_choice.id == ids['alex']  # Equal burden preserves Alex's Monday base day.

        history = create_draft_period(db, principal=manager, week_start=date(2026, 8, 23))
        create_shift(
            db, principal=manager, schedule_period_id=history.id, expected_version=1,
            values=_shift(ids['alex'], ids['south'], date(2026, 8, 24)),
            allowed_store_ids=(ids['north'], ids['south']))
        history.status = SchedulePeriodStatus.PUBLISHED
        history.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        target = create_draft_period(db, principal=manager, week_start=date(2026, 9, 13))
        target_open = create_shift(
            db, principal=manager, schedule_period_id=target.id, expected_version=1,
            values=_shift(None, ids['south'], date(2026, 9, 14)),
            allowed_store_ids=(ids['north'], ids['south']))
        diagnostics = []
        target_shift = db.get(ScheduleShift, target_open.shift_id)
        choice, _ = choose_employee_for_shift(
            db, shift=target_shift, planning_date=date(2026, 9, 13),
            longview_diagnostics=diagnostics)
        assert choice.id == ids['blair']
        assert target_shift.base_pattern_deviation_reason == 'LONGVIEW_ROTATION'
        assert diagnostics[0]['fairness_overrode_base_pattern'] is True
        alex_burden = longview_rotation_fairness(
            db, employee_id=ids['alex'], store_id=ids['south'],
            before_date=date(2026, 9, 14), as_of_date=date(2026, 9, 13))
        assert alex_burden.historical_assignment_count == 1
        assert alex_burden.last_historical_assignment_date == date(2026, 8, 24)
        assert alex_profile.week_a_workdays_mask == weekdays_to_mask((1,))
        assert blair_profile.week_a_workdays_mask == weekdays_to_mask((2, 3, 4))

        balancing_history = create_draft_period(
            db, principal=manager, week_start=date(2026, 8, 30))
        create_shift(
            db, principal=manager, schedule_period_id=balancing_history.id,
            expected_version=1,
            values=_shift(ids['blair'], ids['south'], date(2026, 8, 31)),
            allowed_store_ids=(ids['north'], ids['south']))
        balancing_history.status = SchedulePeriodStatus.PUBLISHED
        balancing_history.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        planned = create_draft_period(db, principal=manager, week_start=date(2026, 9, 20))
        planned_shift = create_shift(
            db, principal=manager, schedule_period_id=planned.id, expected_version=1,
            values=_shift(ids['blair'], ids['south'], date(2026, 9, 21)),
            allowed_store_ids=(ids['north'], ids['south']))
        assert db.get(ScheduleShift, planned_shift.shift_id).manually_locked is True
        future_target = create_draft_period(db, principal=manager, week_start=date(2026, 9, 27))
        future_open = create_shift(
            db, principal=manager, schedule_period_id=future_target.id, expected_version=1,
            values=_shift(None, ids['south'], date(2026, 9, 28)),
            allowed_store_ids=(ids['north'], ids['south']))
        # Historical burdens are tied, so Blair's locked future Longview shift
        # makes Alex the next rotation participant.
        future_choice, _ = choose_employee_for_shift(
            db, shift=db.get(ScheduleShift, future_open.shift_id),
            planning_date=date(2026, 9, 13))
        assert future_choice.id == ids['alex']


def test_longview_rotation_hard_restrictions_and_cross_week_context(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        carla = Employee(
            full_name='Carla Restricted', normalized_name='carla restricted', active=True,
            scheduling_active=True, visible_to_leads=True)
        db.add(carla); db.flush()
        configure_special_store(
            db, principal=manager, store_id=ids['south'], primary_employee_ids=(),
            rotation_employee_ids=(ids['alex'], ids['blair'], carla.id))
        for employee_id in (ids['alex'], ids['blair'], carla.id):
            upsert_employee_profile(
                db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
                max_consecutive_work_days=3,
                special_store_participation=SpecialStoreParticipation.ROTATION,
                allowed_store_ids=(ids['north'], ids['south']))
        reason = db.get(
            __import__('app.models', fromlist=['TimeOffReasonCategory']).TimeOffReasonCategory,
            ids['vacation'])
        pto = create_time_off_request(
            db, principal=manager,
            values=TimeOffInput(
                employee_id=ids['alex'], start_date=date(2026, 10, 4),
                end_date=date(2026, 10, 4), full_day=True,
                reason_category_id=reason.id), management_entered=True)
        review_time_off_request(
            db, principal=manager, request_id=pto.id,
            status=TimeOffRequestStatus.APPROVED)
        create_scheduling_window(
            db, principal=manager, employee_id=ids['blair'], day_of_week=0,
            start_time=time.min, end_time=time.max,
            kind=SchedulingWindowKind.HARD_UNAVAILABLE)
        set_store_preference(
            db, principal=manager, employee_id=carla.id, store_id=ids['south'],
            preference_rank=None, preference_level=StorePreferenceLevel.NEVER,
            allowed_store_ids=(ids['north'], ids['south']))
        target = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        open_shift = create_shift(
            db, principal=manager, schedule_period_id=target.id, expected_version=1,
            values=_shift(None, ids['south'], date(2026, 10, 4)),
            allowed_store_ids=(ids['north'], ids['south']))
        choice, reasons = choose_employee_for_shift(
            db, shift=db.get(ScheduleShift, open_shift.shift_id),
            planning_date=date(2026, 10, 1))
        assert choice is None
        assert {'APPROVED_TIME_OFF', 'HARD_WEEKDAY_LOCKOUT', 'STORE_NEVER'} <= {
            reason.code for reason in reasons}

        # Remove the date-specific restrictions and prove the prior-week block
        # still prevents Longview from bypassing consecutive-day policy.
        db.delete(pto)
        for window in db.execute(select(EmployeeSchedulingWindow)).scalars():
            db.delete(window)
        set_store_preference(
            db, principal=manager, employee_id=carla.id, store_id=ids['south'],
            preference_rank=None, preference_level=StorePreferenceLevel.ACCEPTABLE,
            allowed_store_ids=(ids['north'], ids['south']))
        previous = create_draft_period(db, principal=manager, week_start=date(2026, 9, 27))
        version = 1
        for day in (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)):
            outcome = create_shift(
                db, principal=manager, schedule_period_id=previous.id,
                expected_version=version,
                values=_shift(ids['alex'], ids['north'], day),
                allowed_store_ids=(ids['north'], ids['south']))
            version = outcome.version
        choice, _ = choose_employee_for_shift(
            db, shift=db.get(ScheduleShift, open_shift.shift_id),
            planning_date=date(2026, 10, 1))
        assert choice.id in {ids['blair'], carla.id}


def test_locked_longview_shift_counts_once_toward_target_and_preserves_lead(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        configure_special_store(
            db, principal=manager, store_id=ids['south'], primary_employee_ids=(),
            rotation_employee_ids=(ids['alex'],))
        upsert_employee_profile(
            db, principal=manager, employee_id=ids['alex'], home_store_id=ids['north'],
            target_shifts_per_week=3, target_weekly_hours=Decimal('40'),
            week_a_workdays_mask=weekdays_to_mask((1, 2, 3)),
            week_b_workdays_mask=weekdays_to_mask((1, 2, 3)),
            special_store_participation=SpecialStoreParticipation.ROTATION,
            allowed_store_ids=(ids['north'], ids['south']))
        upsert_employee_profile(
            db, principal=manager, employee_id=ids['blair'], home_store_id=ids['north'],
            target_shifts_per_week=3, target_weekly_hours=Decimal('40'),
            week_a_workdays_mask=weekdays_to_mask((4,)),
            week_b_workdays_mask=weekdays_to_mask((4,)),
            allowed_store_ids=(ids['north'], ids['south']))
        for weekday, store_id in (
            (1, ids['south']), (2, ids['north']),
            (3, ids['north']), (4, ids['north'])):
            _coverage(db, manager, ids, weekday=weekday, store_id=store_id)
        period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        locked = create_shift(
            db, principal=manager, schedule_period_id=period.id, expected_version=1,
            values=_shift(ids['alex'], ids['south'], date(2026, 10, 5),
                          start=time(8, 45), end=time(22), break_minutes=0),
            allowed_store_ids=(ids['north'], ids['south']))
        result = regenerate_period(db, principal=manager, schedule_period_id=period.id)
        locked_row = db.get(ScheduleShift, locked.shift_id)
        assert locked_row.manually_locked and locked_row.store_id == ids['south']
        alex_shifts = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.employee_id == ids['alex'])).scalars())
        assert len(alex_shifts) == 3
        assert sum(row.store_id == ids['south'] for row in alex_shifts) == 1
        assert next(row for row in result['shift_targets'] if row['employee_id'] == ids['alex']) == {
            'employee_id': ids['alex'], 'target_shifts': 3, 'assigned_shifts': 3}
        assert db.execute(select(func.count()).select_from(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.is_lead_of_day.is_(True))).scalar_one() == 4


def test_lead_repair_prefers_ordinary_shift_and_longview_credit_follows_final_assignee(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        carla = Employee(
            full_name='Carla Longview Nonlead', normalized_name='carla longview nonlead',
            active=True, scheduling_active=True, scheduling_lead_capable=False,
            visible_to_leads=True)
        worker = Employee(
            full_name='Ordinary Worker', normalized_name='ordinary worker',
            active=True, scheduling_active=True, scheduling_lead_capable=False,
            visible_to_leads=True)
        db.add_all([carla, worker]); db.flush()
        db.get(Employee, ids['blair']).scheduling_active = False
        configure_special_store(
            db, principal=manager, store_id=ids['south'], primary_employee_ids=(),
            rotation_employee_ids=(ids['alex'], carla.id))
        for employee_id in (ids['alex'], carla.id, worker.id):
            upsert_employee_profile(
                db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
                special_store_participation=(
                    SpecialStoreParticipation.ROTATION
                    if employee_id in (ids['alex'], carla.id)
                    else SpecialStoreParticipation.NONE),
                allowed_store_ids=(ids['north'], ids['south']))

        # With an ordinary alternative, Lead repair leaves Longview untouched.
        direct = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        longview_shift = ScheduleShift(
            schedule_period_id=direct.id, employee_id=carla.id, store_id=ids['south'],
            shift_date=date(2026, 10, 5), start_time=time(8, 45), end_time=time(22),
            unpaid_break_minutes=0, created_by_principal_id=manager.id,
            updated_by_principal_id=manager.id)
        ordinary_shift = ScheduleShift(
            schedule_period_id=direct.id, employee_id=worker.id, store_id=ids['north'],
            shift_date=date(2026, 10, 5), start_time=time(8, 45), end_time=time(22),
            unpaid_break_minutes=0, created_by_principal_id=manager.id,
            updated_by_principal_id=manager.id)
        db.add_all([longview_shift, ordinary_shift]); db.flush()
        assert ensure_daily_lead_staffing(
            db, principal=manager, schedule_period_id=direct.id,
            planning_date=date(2026, 10, 1)) == []
        assert longview_shift.employee_id == carla.id
        assert ordinary_shift.employee_id == ids['alex']
        assert ordinary_shift.base_pattern_deviation_reason == 'LEAD_COVERAGE'

        # When Longview is the only repairable position, the final Lead receives
        # queue credit and the provisional non-Lead receives none.
        history = create_draft_period(db, principal=manager, week_start=date(2026, 8, 16))
        historical_shift = create_shift(
            db, principal=manager, schedule_period_id=history.id, expected_version=1,
            values=_shift(ids['alex'], ids['south'], date(2026, 8, 17)),
            allowed_store_ids=(ids['north'], ids['south']))
        history.status = SchedulePeriodStatus.PUBLISHED
        history.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        _coverage(db, manager, ids, weekday=1, store_id=ids['south'])
        target = create_draft_period(db, principal=manager, week_start=date(2026, 10, 18))
        result = regenerate_period(db, principal=manager, schedule_period_id=target.id)
        final_shift = db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == target.id,
            ScheduleShift.store_id == ids['south'])).scalar_one()
        assert final_shift.employee_id == ids['alex']
        decision = result['longview_rotation'][0]
        assert decision['rotation_selected_employee_id'] == carla.id
        assert decision['employee_id'] == ids['alex']
        assert decision['lead_repair_changed_assignment'] is True
        states = {row.employee_id: row for row in db.execute(select(
            SpecialStoreRotationState).where(
                SpecialStoreRotationState.store_id == ids['south'])).scalars()}
        assert states[ids['alex']].last_assigned_shift_id == final_shift.id
        assert states[carla.id].last_assigned_shift_id is None
        assert db.get(ScheduleShift, historical_shift.shift_id).employee_id == ids['alex']


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


def test_weekend_fairness_separates_history_future_and_pto(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        for employee_id in (ids['alex'], ids['blair']):
            upsert_employee_profile(
                db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
                allowed_store_ids=(ids['north'], ids['south']))
        historical = create_draft_period(
            db, principal=manager, week_start=date(2026, 6, 14))
        historical_shift = create_shift(
            db, principal=manager, schedule_period_id=historical.id,
            expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 6, 20)),
            allowed_store_ids=(ids['north'], ids['south']))
        historical.status = SchedulePeriodStatus.PUBLISHED
        historical.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED

        planned = create_draft_period(
            db, principal=manager, week_start=date(2026, 9, 13))
        planned_shift = create_shift(
            db, principal=manager, schedule_period_id=planned.id,
            expected_version=1,
            values=_shift(ids['blair'], ids['north'], date(2026, 9, 19)),
            allowed_store_ids=(ids['north'], ids['south']))
        create_shift(
            db, principal=manager, schedule_period_id=planned.id,
            expected_version=planned_shift.version,
            values=_shift(ids['blair'], ids['north'], date(2026, 9, 13)),
            allowed_store_ids=(ids['north'], ids['south']))
        assert db.get(ScheduleShift, planned_shift.shift_id).manually_locked is True

        reason = db.get(
            __import__('app.models', fromlist=['TimeOffReasonCategory']).TimeOffReasonCategory,
            ids['vacation'])
        pto = create_time_off_request(
            db, principal=manager,
            values=TimeOffInput(
                employee_id=ids['alex'], start_date=date(2026, 10, 24),
                end_date=date(2026, 10, 24), full_day=True,
                reason_category_id=reason.id), management_entered=True)
        review_time_off_request(
            db, principal=manager, request_id=pto.id,
            status=TimeOffRequestStatus.APPROVED)

        alex_saturday = weekend_fairness(
            db, employee_id=ids['alex'], weekday=5,
            before_date=date(2026, 10, 24), as_of_date=date(2026, 8, 30))
        alex_sunday = weekend_fairness(
            db, employee_id=ids['alex'], weekday=6,
            before_date=date(2026, 10, 25), as_of_date=date(2026, 8, 30))
        blair_saturday = weekend_fairness(
            db, employee_id=ids['blair'], weekday=5,
            before_date=date(2026, 10, 24), as_of_date=date(2026, 8, 30))
        assert alex_saturday.historical_assignment_count == 1
        assert alex_saturday.last_historical_assignment_date == date(2026, 6, 20)
        assert alex_saturday.planned_future_assignment_count == 0
        assert alex_sunday.assignment_count == 0
        assert blair_saturday.historical_assignment_count == 0
        assert blair_saturday.planned_future_assignment_count == 1

        sunday_target = create_draft_period(
            db, principal=manager, week_start=date(2026, 10, 25))
        sunday_open = create_shift(
            db, principal=manager, schedule_period_id=sunday_target.id,
            expected_version=1,
            values=_shift(None, ids['north'], date(2026, 10, 25)),
            allowed_store_ids=(ids['north'], ids['south']))
        sunday_choice, _ = choose_employee_for_shift(
            db, shift=db.get(ScheduleShift, sunday_open.shift_id),
            planning_date=date(2026, 8, 30))
        assert sunday_choice.id == ids['alex']  # Blair already carries the future Sunday burden.

        target = create_draft_period(
            db, principal=manager, week_start=date(2026, 10, 18))
        open_shift = create_shift(
            db, principal=manager, schedule_period_id=target.id,
            expected_version=1,
            values=_shift(None, ids['north'], date(2026, 10, 24)),
            allowed_store_ids=(ids['north'], ids['south']))
        choice, _ = choose_employee_for_shift(
            db, shift=db.get(ScheduleShift, open_shift.shift_id),
            planning_date=date(2026, 8, 30))
        assert choice.id == ids['blair']  # Alex's PTO is a hard exclusion, not burden.
        assert db.get(ScheduleShift, historical_shift.shift_id).employee_id == ids['alex']


def test_weekend_fairness_tie_prefers_base_and_imbalance_overrides_without_mutation(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        alex_profile = upsert_employee_profile(
            db, principal=manager, employee_id=ids['alex'],
            home_store_id=ids['north'], target_shifts_per_week=3,
            target_weekly_hours=Decimal('40'),
            week_a_workdays_mask=weekdays_to_mask((6,)),
            week_b_workdays_mask=weekdays_to_mask((6,)),
            allowed_store_ids=(ids['north'], ids['south']))
        blair_profile = upsert_employee_profile(
            db, principal=manager, employee_id=ids['blair'],
            home_store_id=ids['north'], target_shifts_per_week=3,
            target_weekly_hours=Decimal('40'),
            week_a_workdays_mask=weekdays_to_mask((1, 2, 3)),
            week_b_workdays_mask=weekdays_to_mask((1, 2, 3)),
            allowed_store_ids=(ids['north'], ids['south']))

        tied_period = create_draft_period(
            db, principal=manager, week_start=date(2026, 9, 6))
        tied_open = create_shift(
            db, principal=manager, schedule_period_id=tied_period.id,
            expected_version=1,
            values=_shift(None, ids['north'], date(2026, 9, 12)),
            allowed_store_ids=(ids['north'], ids['south']))
        tied_choice, _ = choose_employee_for_shift(
            db, shift=db.get(ScheduleShift, tied_open.shift_id),
            planning_date=date(2026, 9, 6))
        assert tied_choice.id == ids['alex']

        history = create_draft_period(
            db, principal=manager, week_start=date(2026, 9, 13))
        create_shift(
            db, principal=manager, schedule_period_id=history.id,
            expected_version=1,
            values=_shift(ids['alex'], ids['north'], date(2026, 9, 19)),
            allowed_store_ids=(ids['north'], ids['south']))
        history.status = SchedulePeriodStatus.PUBLISHED
        history.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        target = create_draft_period(
            db, principal=manager, week_start=date(2026, 9, 20))
        target_open = create_shift(
            db, principal=manager, schedule_period_id=target.id,
            expected_version=1,
            values=_shift(None, ids['north'], date(2026, 9, 26)),
            allowed_store_ids=(ids['north'], ids['south']))
        diagnostics = []
        target_shift = db.get(ScheduleShift, target_open.shift_id)
        choice, _ = choose_employee_for_shift(
            db, shift=target_shift, planning_date=date(2026, 9, 20),
            weekend_diagnostics=diagnostics)
        assert choice.id == ids['blair']
        assert target_shift.base_pattern_deviation_reason == 'WEEKEND_FAIRNESS'
        assert diagnostics[0]['fairness_overrode_base_pattern'] is True
        assert diagnostics[0]['candidate_burdens'][0]['employee_id'] == ids['blair']
        assert alex_profile.week_a_workdays_mask == weekdays_to_mask((6,))
        assert blair_profile.week_a_workdays_mask == weekdays_to_mask((1, 2, 3))


def test_longview_weekend_assignments_do_not_create_vancouver_burden(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        for employee_id in (ids['alex'], ids['blair']):
            upsert_employee_profile(
                db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_weekly_hours=Decimal('40'),
                allowed_store_ids=(ids['north'], ids['south']))
        configure_special_store(
            db, principal=manager, store_id=ids['south'],
            primary_employee_ids=(), rotation_employee_ids=(ids['alex'],))
        history = create_draft_period(
            db, principal=manager, week_start=date(2026, 9, 6))
        first = create_shift(
            db, principal=manager, schedule_period_id=history.id,
            expected_version=1,
            values=_shift(ids['alex'], ids['south'], date(2026, 9, 12)),
            allowed_store_ids=(ids['north'], ids['south']))
        create_shift(
            db, principal=manager, schedule_period_id=history.id,
            expected_version=first.version,
            values=_shift(ids['blair'], ids['north'], date(2026, 9, 12)),
            allowed_store_ids=(ids['north'], ids['south']))
        history.status = SchedulePeriodStatus.PUBLISHED
        history.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        target = create_draft_period(
            db, principal=manager, week_start=date(2026, 9, 13))
        open_shift = create_shift(
            db, principal=manager, schedule_period_id=target.id,
            expected_version=1,
            values=_shift(None, ids['north'], date(2026, 9, 19)),
            allowed_store_ids=(ids['north'], ids['south']))
        choice, _ = choose_employee_for_shift(
            db, shift=db.get(ScheduleShift, open_shift.shift_id),
            planning_date=date(2026, 9, 13))
        assert choice.id == ids['alex']
        assert weekend_fairness(
            db, employee_id=ids['alex'], weekday=5,
            before_date=date(2026, 9, 19),
            as_of_date=date(2026, 9, 13)).historical_assignment_count == 0


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
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        _coverage(db, manager, ids)
        anchor = create_draft_period(db, principal=manager, week_start=date(2026, 9, 20))
        anchor.status = SchedulePeriodStatus.PUBLISHED
        anchor.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        anchor.published_at = datetime(2026, 9, 19, tzinfo=timezone.utc)
        update_organization_policy(db, principal=manager, weekly_approval_hours=Decimal('40'),
            schedule_length_weeks=2, generate_days_before_end=7, publish_days_before_end=0,
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


def test_manual_generate_is_concurrent_idempotent_and_enters_review(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        _coverage(db, manager, ids)
        anchor = create_draft_period(db, principal=manager, week_start=date(2026, 8, 23))
        anchor.status = SchedulePeriodStatus.PUBLISHED
        anchor.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        anchor.published_at = datetime(2026, 8, 22, tzinfo=timezone.utc)
        update_organization_policy(
            db, principal=manager, weekly_approval_hours=Decimal('40'),
            schedule_length_weeks=4, generate_days_before_end=7, publish_days_before_end=3,
            publication_local_time=time(9), timezone_name='America/Los_Angeles')
        db.commit()

    barrier = Barrier(2)

    def generate():
        with Session() as worker:
            barrier.wait()
            result = manual_generate_draft_schedule(
                worker, principal=manager,
                now=datetime(2026, 8, 25, 18, tzinfo=timezone.utc))
            worker.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _value: generate(), range(2)))

    assert sorted(row['created'] for row in results) == [False, True]
    assert results[0]['period_ids'] == results[1]['period_ids']
    with Session() as db:
        periods = list(db.execute(select(SchedulePeriod).where(
            SchedulePeriod.status == SchedulePeriodStatus.DRAFT).order_by(
            SchedulePeriod.week_start_date)).scalars())
        assert [row.week_start_date for row in periods] == [
            date(2026, 8, 30), date(2026, 9, 6), date(2026, 9, 13)]
        assert all(row.lifecycle_stage == ScheduleLifecycleStage.REVIEW for row in periods)
        publication_dates = [row.automatic_publication_at.date() for row in periods]
        assert publication_dates == [date(2026, 8, 26), date(2026, 9, 2), date(2026, 9, 9)]


def test_generate_draft_form_redirects_to_exact_existing_review_without_duplicates(scheduling_db):
    from app.routers.v2_scheduling import generate_automation_page

    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        _coverage(db, manager, ids)
        anchor = create_draft_period(db, principal=manager, week_start=date(2026, 8, 23))
        anchor.status = SchedulePeriodStatus.PUBLISHED
        anchor.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        anchor.published_at = datetime(2026, 8, 22, tzinfo=timezone.utc)
        update_organization_policy(
            db, principal=manager, weekly_approval_hours=Decimal('40'),
            schedule_length_weeks=2, generate_days_before_end=7, publish_days_before_end=3,
            publication_local_time=time(9), timezone_name='America/Los_Angeles')
        db.commit()

        first = generate_automation_page(
            request=None, _feature=manager, principal=manager, db=db, _csrf=None)
        generated = db.execute(select(SchedulePeriod).where(
            SchedulePeriod.status == SchedulePeriodStatus.DRAFT)).scalar_one()
        assert first.status_code == 303
        assert f'/v2/scheduling/week?period_id={generated.id}' in first.headers['location']

        second = generate_automation_page(
            request=None, _feature=manager, principal=manager, db=db, _csrf=None)
        assert second.status_code == 303
        assert f'/v2/scheduling/week?period_id={generated.id}' in second.headers['location']
        assert db.execute(select(func.count()).select_from(SchedulePeriod).where(
            SchedulePeriod.week_start_date == generated.week_start_date)).scalar_one() == 1


def test_manual_generation_uses_canonical_lead_and_double_coverage_path(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        db.get(Employee, ids['alex']).scheduling_double_coverage = True
        for employee_id in (ids['alex'], ids['blair']):
            upsert_employee_profile(
                db, principal=manager, employee_id=employee_id,
                home_store_id=ids['north'], target_weekly_hours=Decimal('32'),
                allowed_store_ids=(ids['north'], ids['south']))
        set_store_preference(
            db, principal=manager, employee_id=ids['blair'], store_id=ids['north'],
            preference_rank=1, preference_level=StorePreferenceLevel.PREFERRED,
            allowed_store_ids=(ids['north'], ids['south']))
        set_double_coverage_store(db, principal=manager, store_id=ids['north'])
        _coverage(db, manager, ids, weekday=1)
        source = create_draft_period(db, principal=manager, week_start=date(2026, 8, 23))
        create_shift(
            db, principal=manager, schedule_period_id=source.id, expected_version=1,
            values=_shift(ids['blair'], ids['north'], day=date(2026, 8, 24),
                          shift_type_id=ids['general']),
            allowed_store_ids=(ids['north'], ids['south']))
        source.status = SchedulePeriodStatus.PUBLISHED
        source.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
        source.published_at = datetime(2026, 8, 22, tzinfo=timezone.utc)
        update_organization_policy(
            db, principal=manager, weekly_approval_hours=Decimal('40'),
            schedule_length_weeks=2, generate_days_before_end=1, publish_days_before_end=0,
            publication_local_time=time(9), timezone_name='America/Los_Angeles')
        db.commit()

        now = datetime(2026, 8, 20, 18, tzinfo=timezone.utc)
        result = manual_generate_draft_schedule(db, principal=manager, now=now)
        generated = db.get(SchedulePeriod, result['primary_period_id'])
        rows = list(db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == generated.id)).scalars())
        assert generated.lifecycle_stage == ScheduleLifecycleStage.REVIEW
        assert generated.status == SchedulePeriodStatus.DRAFT
        assert generated.automatic_publication_at > now
        assert len([row for row in rows if not row.is_double_coverage]) == 1
        assert len([row for row in rows if row.is_double_coverage]) == 1
        assert sum(row.is_lead_of_day for row in rows) == 1
        assert result['results'][0]['double_coverage']['assigned'] == 1

        double_shift = next(row for row in rows if row.is_double_coverage)
        override_double_coverage_employee(
            db, principal=manager, shift_id=double_shift.id,
            employee_id=double_shift.employee_id)
        regenerate_period(db, principal=manager, schedule_period_id=generated.id)
        preserved = db.get(ScheduleShift, double_shift.id)
        assert preserved.double_coverage_manually_assigned is True
        assert preserved.manually_locked is True
        assert db.execute(select(func.count()).select_from(SchedulePeriod).where(
            SchedulePeriod.week_start_date == generated.week_start_date)).scalar_one() == 1


def test_automation_dashboard_separates_historical_and_exact_period_review(scheduling_db):
    Session, manager, ids, _engine = scheduling_db
    with Session() as db:
        historical = create_draft_period(db, principal=manager, week_start=date(2026, 7, 5))
        historical.lifecycle_stage = ScheduleLifecycleStage.REVIEW
        create_shift(
            db, principal=manager, schedule_period_id=historical.id, expected_version=1,
            values=_shift(ids['alex'], ids['north'], day=date(2026, 7, 5)),
            allowed_store_ids=(ids['north'], ids['south']))
        upcoming = create_draft_period(db, principal=manager, week_start=date(2026, 8, 30))
        upcoming.lifecycle_stage = ScheduleLifecycleStage.REVIEW
        db.flush()
        dashboard = automation_draft_dashboard(db, today=date(2026, 8, 25))
        assert [row['period'].id for row in dashboard['historical']] == [historical.id]
        assert dashboard['historical'][0]['shift_count'] == 1
        assert [row['period'].id for row in dashboard['upcoming']] == [upcoming.id]

        board = serialize_week_board(
            db, week_start=upcoming.week_start_date, schedule_period_id=historical.id,
            selected_store_ids=(ids['north'],),
            all_authorized_store_ids=(ids['north'], ids['south']),
            permission_flags={'scheduling.edit_draft_shifts': True})
        assert board['period']['id'] == historical.id
        assert board['week']['start'] == '2026-07-05'


def test_automation_page_renders_owner_workflow_and_draft_query_requires_management(scheduling_db):
    from app.main import app
    from app.routers.v2_scheduling import _authorize_explicit_period, automation_page
    from fastapi import HTTPException
    from starlette.requests import Request

    Session, manager, _ids, _engine = scheduling_db
    with Session() as db:
        historical = create_draft_period(db, principal=manager, week_start=date(2026, 7, 5))
        historical.lifecycle_stage = ScheduleLifecycleStage.REVIEW
        upcoming = create_draft_period(db, principal=manager, week_start=date(2026, 8, 30))
        upcoming.lifecycle_stage = ScheduleLifecycleStage.REVIEW
        db.commit()
        request = Request({
            'type': 'http', 'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
            'path': '/v2/scheduling/automation', 'raw_path': b'/v2/scheduling/automation',
            'query_string': b'', 'headers': [], 'client': ('test', 1),
            'server': ('test', 80), 'app': app,
        })
        rendered = automation_page(
            request=request, _feature=manager, principal=manager, db=db).body.decode()
        assert 'Upcoming Draft' in rendered and 'Review Schedule' in rendered
        assert 'Historical Schedules' in rendered and '1 shift(s)' not in rendered
        assert f'/periods/{upcoming.id}/review' in rendered
        assert f'/periods/{historical.id}/review' in rendered

        employee = Principal(
            id=manager.id, username='employee', role=Role.STORE, store_id=None, active=True)
        with pytest.raises(HTTPException) as forbidden:
            _authorize_explicit_period(
                db, principal=employee, schedule_period_id=upcoming.id)
        assert forbidden.value.status_code == 403


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
    assert fallback_allowed_for_role(
        role=Role.LEAD, permission_key='scheduling.attendance.record')
    assert fallback_allowed_for_role(
        role=Role.MANAGER, permission_key='scheduling.attendance.record')
    assert not fallback_allowed_for_role(
        role=Role.STORE, permission_key='scheduling.attendance.record')
    for role in Role:
        assert not fallback_allowed_for_role(role=role, permission_key='scheduling.view_own')
        assert not fallback_allowed_for_role(role=role, permission_key='scheduling.time_off.submit_own')
        assert not fallback_allowed_for_role(role=role, permission_key='scheduling.transfer_own')


def test_scheduling_api_is_separate_feature_gated_and_csrf_protected():
    from app.routers.v2_scheduling import (
        create_draft_access,
        edit_shift_access,
        feature_access,
        attendance_access,
        manage_store_shift_access,
        place_store_shift_access,
        router,
    )
    from app.security.csrf import verify_csrf

    routes = {
        route.path: route
        for route in router.routes
        if getattr(route, 'path', '').startswith('/v2/scheduling/api')
    }
    assert '/v2/scheduling/api/periods' in routes
    assert '/v2/scheduling/api/periods/{schedule_period_id}/shifts' in routes
    period_dependencies = [row.call for row in routes['/v2/scheduling/api/periods'].dependant.dependencies]
    shift_dependencies = [row.call for row in routes['/v2/scheduling/api/periods/{schedule_period_id}/shifts'].dependant.dependencies]
    assert feature_access in period_dependencies and create_draft_access in period_dependencies
    assert feature_access in shift_dependencies and edit_shift_access in shift_dependencies
    assert verify_csrf in period_dependencies and verify_csrf in shift_dependencies
    manage_route = next(route for route in router.routes if getattr(route, 'path', '') == '/v2/scheduling/api/store-shifts' and 'POST' in route.methods)
    placement_route = routes['/v2/scheduling/api/periods/{schedule_period_id}/store-shifts/{store_shift_id}/place']
    manage_dependencies = [row.call for row in manage_route.dependant.dependencies]
    placement_dependencies = [row.call for row in placement_route.dependant.dependencies]
    assert feature_access in manage_dependencies and manage_store_shift_access in manage_dependencies
    assert feature_access in placement_dependencies and place_store_shift_access in placement_dependencies
    assert verify_csrf in manage_dependencies and verify_csrf in placement_dependencies
    attendance_route = routes['/v2/scheduling/api/shifts/{shift_id}/attendance']
    attendance_dependencies = [row.call for row in attendance_route.dependant.dependencies]
    assert feature_access in attendance_dependencies and attendance_access in attendance_dependencies
    assert verify_csrf in attendance_dependencies


def test_server_rendered_scheduling_routes_separate_employee_and_admin_permissions_and_csrf():
    from app.routers.v2_scheduling import (
        automation_access, edit_shift_access, generate_access, own_schedule_access, preferences_access,
        transfer_access, transfer_approval_access, router,
    )
    from app.security.csrf import verify_csrf
    routes = {(route.path, tuple(sorted(route.methods or ()))): route for route in router.routes
              if getattr(route, 'path', '').startswith('/v2/scheduling')}
    def dependencies(path, method):
        route = next(row for (candidate, methods), row in routes.items()
                     if candidate == path and method in methods)
        return {item.call for item in route.dependant.dependencies}
    assert preferences_access in dependencies('/v2/scheduling/rules', 'GET')
    assert preferences_access in dependencies('/v2/scheduling/employees', 'GET')
    assert preferences_access in dependencies('/v2/scheduling/store-defaults', 'GET')
    assert own_schedule_access in dependencies('/v2/scheduling/my-schedule', 'GET')
    assert transfer_approval_access in dependencies('/v2/scheduling/transfer-approvals', 'GET')
    mutations = (
        ('/v2/scheduling/employees/sync', 'POST', preferences_access),
        ('/v2/scheduling/employees/{employee_id}/scheduling-status', 'POST', preferences_access),
        ('/v2/scheduling/employees/{employee_id}/capabilities', 'POST', preferences_access),
        ('/v2/scheduling/store-defaults', 'POST', preferences_access),
        ('/v2/scheduling/employees/{employee_id}', 'POST', preferences_access),
        ('/v2/scheduling/automation', 'POST', automation_access),
        ('/v2/scheduling/automation/generate', 'POST', generate_access),
        ('/v2/scheduling/periods/{period_id}/regenerate', 'POST', generate_access),
        ('/v2/scheduling/periods/{period_id}/hold', 'POST', automation_access),
        ('/v2/scheduling/shifts/{shift_id}/lock-form', 'POST', edit_shift_access),
        ('/v2/scheduling/shifts/{shift_id}/lead-of-day', 'POST', preferences_access),
        ('/v2/scheduling/shifts/{shift_id}/double-coverage', 'POST', preferences_access),
        ('/v2/scheduling/my-schedule/transfers', 'POST', transfer_access),
        ('/v2/scheduling/my-schedule/transfers/{request_id}/respond', 'POST', transfer_access),
        ('/v2/scheduling/transfer-approvals/{request_id}', 'POST', transfer_approval_access),
    )
    for path, method, access in mutations:
        calls = dependencies(path, method)
        assert access in calls and verify_csrf in calls

    assert automation_access in dependencies('/v2/scheduling/periods/{period_id}/review', 'GET')


def test_automation_template_prioritizes_owner_workflow_and_separates_history():
    template = open('app/templates/v2/scheduling/automation.html', encoding='utf-8').read()
    assert 'Upcoming Draft' in template and 'Generate Draft' in template
    assert 'Review Schedule' in template and '>Regenerate<' in template
    assert 'Historical Schedules' in template and 'shift_count' in template
    assert '/automation/generate' in template and 'csrf_token' in template


def test_scheduling_employee_roster_template_separates_square_and_local_status():
    template = open('app/templates/v2/scheduling/employees.html', encoding='utf-8').read()
    assert 'Update Employees from Square' in template
    assert 'Square status' in template and 'Scheduling status' in template
    assert 'scheduling-status' in template
    assert 'csrf_token' in template
    assert 'Active ({{ active_count }})' in template
    assert 'Inactive ({{ inactive_count }})' in template
    assert 'name="filter"' not in template
    assert 'Lead capable' in template and 'Double Coverage' in template
    assert 'Login linked' in template and 'Login unlinked' in template


def test_lead_double_coverage_schema_and_display_contracts():
    from app.models import SchedulingStoreDefaults

    assert {'scheduling_lead_capable', 'scheduling_double_coverage'} <= set(Employee.__table__.columns.keys())
    assert {
        'is_lead_of_day', 'lead_of_day_manually_assigned',
        'is_double_coverage', 'double_coverage_manually_assigned',
    } <= set(ScheduleShift.__table__.columns.keys())
    index_names = {index.name for index in ScheduleShift.__table__.indexes}
    assert 'schedule_shifts_one_lead_per_day_uniq' in index_names
    assert 'schedule_shifts_one_double_coverage_per_employee_week_uniq' in index_names
    assert 'double_coverage_store_id' in SchedulingStoreDefaults.__table__.columns
    board_card = open('app/templates/v2/scheduling/_shift_card.html', encoding='utf-8').read()
    my_schedule = open('app/templates/v2/scheduling/my_schedule.html', encoding='utf-8').read()
    assert 'Lead' in board_card and 'Double Coverage' in board_card
    assert 'Lead of the Day' in my_schedule and 'Double Coverage' in my_schedule


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
