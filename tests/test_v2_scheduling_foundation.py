from __future__ import annotations

import os
import json
import uuid
from datetime import date, time
from decimal import Decimal

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
    TimeOffRequestStatus,
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
from app.services.v2_scheduling_template_service import (
    CopySelection,
    copy_schedule_periods,
    create_shift_type,
    create_time_off_reason_category,
    instantiate_schedule_template,
    save_schedule_template,
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


def test_scheduling_capability_defaults_are_management_only_and_self_service_off():
    management = (
        'scheduling.view_store', 'scheduling.view_all', 'scheduling.create_draft',
        'scheduling.edit_draft_shifts', 'scheduling.delete_draft_shifts', 'scheduling.copy',
        'scheduling.manage_shift_templates', 'scheduling.manage_schedule_templates',
        'scheduling.manage_preferences', 'scheduling.manage_availability',
        'scheduling.time_off.view', 'scheduling.time_off.review',
        'scheduling.manage_operating_hours', 'scheduling.manage_special_hours',
        'scheduling.manage_coverage', 'scheduling.view_labor_cost', 'scheduling.publish',
        'scheduling.modify_published', 'scheduling.override_hard_unavailability',
        'scheduling.publish_with_warnings',
    )
    assert all(fallback_allowed_for_role(role=Role.ADMIN, permission_key=key) for key in management)
    assert all(fallback_allowed_for_role(role=Role.MANAGER, permission_key=key) for key in management)
    assert not any(fallback_allowed_for_role(role=Role.LEAD, permission_key=key) for key in management)
    assert not any(fallback_allowed_for_role(role=Role.STORE, permission_key=key) for key in management)
    for role in Role:
        assert not fallback_allowed_for_role(role=role, permission_key='scheduling.view_own')
        assert not fallback_allowed_for_role(role=role, permission_key='scheduling.time_off.submit_own')


def test_scheduling_api_is_separate_feature_gated_and_csrf_protected():
    from app.main import app
    from app.routers.v2_scheduling import create_draft_access, edit_shift_access, feature_access
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
    assert 'onpointerdown' in script and "e.key==='Escape'" in script
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


def test_coverage_open_shift_roles_special_hours_and_time_off(scheduling_db):
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
        assert {'NO_ASSIGNED_EMPLOYEE', 'REQUIRED_ROLE_ABSENT', 'NO_OPENER', 'NO_CLOSER'} <= warning_types
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
