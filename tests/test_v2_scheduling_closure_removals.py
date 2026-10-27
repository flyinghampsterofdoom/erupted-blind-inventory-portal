"""Synthetic disposable PostgreSQL regression cases; no production connections."""
from collections import Counter
from datetime import date, time, timedelta
import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal, Role
from app.services.v2_scheduling_service import (create_draft_period, update_shift, ShiftInput,
    approve_schedule, SchedulingValidationError)
from app.services.v2_scheduling_policy_service import regenerate_period
from app.services.v2_scheduling_business_hours_service import save_business_hours
from app.services.v2_scheduling_allocation_service import allocation_report
from app.services.v2_scheduling_horizon_service import plan_schedule_horizon, apply_schedule_horizon_plan
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from app.services.v2_scheduling_rules_service import create_coverage_requirement
from test_v2_scheduling_foundation import scheduling_db, opening_hours
from test_v2_scheduling_repair import person

START = date(2027, 1, 3)


def setup(db, actor, ids):
    for e in db.scalars(select(m.Employee)):
        e.scheduling_active = False
    stores = (ids['north'], ids['south'])
    for sid in stores:
        for weekday in range(7):
            opening_hours(db, actor, sid, weekday, closing=time(10))
            create_coverage_requirement(db, principal=actor, store_id=sid, day_of_week=weekday,
                start_time=time(8,45), end_time=time(10), minimum_employee_count=1, allowed_store_ids=stores)
    db.get(m.SchedulingStoreDefaults, 1).standard_shift_end = time(10)
    people = [person(db, actor, f'Synthetic worker {i}', lead=True, target=2) for i in range(7)]
    week = create_draft_period(db, principal=actor, week_start=START)
    return stores, people, week


def close(db, actor, stores, days):
    admin = Principal(actor.id, actor.username, Role.ADMIN, None, True)
    for day in days:
        save_business_hours(db, principal=admin, store_ids=stores, allowed_store_ids=stores,
            calendar_date=day, event_name='Synthetic closure', closed_all_day=True)


def remove(db, actor, week, row):
    return update_shift(db, principal=actor, schedule_period_id=week.id, shift_id=row.id,
        expected_version=week.version, allowed_store_ids=(row.store_id,),
        values=ShiftInput(employee_id=None, store_id=row.store_id, shift_date=row.shift_date,
            start_time=row.start_time, end_time=row.end_time, unpaid_break_minutes=row.unpaid_break_minutes))


@pytest.mark.parametrize('store_count,day_count', [(1,1), (2,1), (2,3), (2,7)])
def test_closure_generation_approval_and_next_week(scheduling_db, store_count, day_count):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        stores, people, week = setup(db, actor, ids)
        # Reproduce the production sequence: an existing complete generated draft,
        # followed by closures whose demand cannot meet unchanged allocations.
        assert regenerate_period(db, principal=actor, schedule_period_id=week.id)['validation_status'] == 'VALID'
        original = {r.id:r.employee_id for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))}
        close(db, actor, stores[:store_count], [START+timedelta(days=i) for i in range(day_count)])
        result = regenerate_period(db, principal=actor, schedule_period_id=week.id)
        assert result['allocation']['status'] == 'PASS'
        assert result['validation_status'] == 'VALID', result['final_validation_errors']
        rows = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id == week.id)))
        assert len(rows) == 14-store_count*day_count and all(r.employee_id for r in rows)
        report = allocation_report(db, week)
        assert sum(r['closure_shortfall'] for r in report) == store_count*day_count
        assert all(r['normal_shifts'] == r['effective_shifts'] == 2 for r in report)
        counts = Counter(r.employee_id for r in rows)
        baseline = Counter(original[r.id] for r in rows)
        loss = lambda totals: sum((totals.get(e.id,0)-2)**2 for e in people)
        changes = sum(r.employee_id != original[r.id] for r in rows)
        assert changes <= result['allocation']['search']['fairness']['additional_change_budget']
        assert loss(counts) <= loss(baseline)
        # A closure does not justify churn by itself. Additional moves require
        # a strict equity gain, and the next regeneration must be stable.
        if changes:
            assert loss(counts) < loss(baseline)
        retained = {r.id:r.employee_id for r in rows}
        repeated = regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert repeated['allocation']['changes']==[]
        assert all(r.employee_id==retained[r.id] for r in rows)
        assert all(counts.get(e.id, 0) <= 2 for e in people)
        assert all('closure' in w.message for w in rebuild_schedule_warnings(db,schedule_period_id=week.id)
                   if w.warning_type == 'BELOW_TARGET_SHIFTS')
        approve_schedule(db, principal=actor, schedule_period_id=week.id, expected_version=week.version, allowed_store_ids=stores)
        following = create_draft_period(db, principal=actor, week_start=START+timedelta(days=7))
        assert regenerate_period(db, principal=actor, schedule_period_id=following.id)['validation_status'] == 'VALID'
        assert all(r['actual_shifts'] == 2 and r['closure_shortfall'] == 0 for r in allocation_report(db, following))
        # A target shortfall also works without a closure; source targets remain intact.
        profile = db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==people[0].id))
        profile.target_shifts_per_week = 3
        db.flush()
        result=regenerate_period(db, principal=actor, schedule_period_id=following.id)
        assert result['validation_status']=='VALID'
        assert any(d['code']=='ALLOCATION_SHORTFALL' for d in result['allocation']['diagnostics'])


def test_removal_save_reopen_regenerate_and_lead_authority(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        stores, people, week = setup(db, actor, ids)
        regenerate_period(db, principal=actor, schedule_period_id=week.id)
        row = db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id, m.ScheduleShift.is_lead_of_day.is_(True)))
        row.lead_of_day_manually_assigned = True
        row.is_lead_of_day = True
        db.flush()
        lead = Principal(actor.id, actor.username, Role.LEAD, None, True)
        staff = Principal(actor.id, actor.username, Role.STORE, None, True)
        with pytest.raises(PermissionError, match='scheduling authority'):
            remove(db, staff, week, row)
        old_employee = row.employee_id
        remove(db, lead, week, row)
        row_id, week_id = row.id, week.id
        assert row.employee_id is None and row.manually_locked
        assert not row.is_lead_of_day and not row.lead_of_day_manually_assigned
        audit = db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SHIFT_CHANGED').order_by(m.AuditLog.id.desc()))
        assert audit.meta['before']['employee_id']==old_employee
        assert audit.meta['metadata']['assignment_removed'] is True
        db.commit()
    with Session() as db:
        row, week = db.get(m.ScheduleShift,row_id), db.get(m.SchedulePeriod,week_id)
        assert row.employee_id is None and row.manually_locked
        board = serialize_week_board(db, week_start=START, selected_store_ids=stores,
            all_authorized_store_ids=stores, permission_flags={'scheduling.view_all':True,'scheduling.edit_draft_shifts':True}, historical_admin=True)
        assert next(r for r in board['shifts'] if r['id']==row_id)['intentionally_unfilled']
        result = regenerate_period(db, principal=actor, schedule_period_id=week.id)
        assert row.employee_id is None and row.manually_locked
        assert result['allocation']['status']=='PASS'
        with pytest.raises(SchedulingValidationError, match='Unfilled assignments'):
            approve_schedule(db, principal=actor, schedule_period_id=week.id, expected_version=week.version, allowed_store_ids=stores)
        plan = plan_schedule_horizon(db, periods=[week], planning_date=START, time_limit=5)
        assert plan['assignments'] is not None, plan
        assert plan['assignments'][row_id] is None
        apply_schedule_horizon_plan(db, periods=[week], plan=plan, principal=actor)
        assert row.employee_id is None


def test_closed_removal_and_approved_pto_preserved(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        stores, people, week = setup(db, actor, ids)
        pto = m.TimeOffRequest(employee_id=people[0].id, start_date=START, end_date=START,
            full_day=True, status=m.TimeOffRequestStatus.APPROVED, reason_category_id=ids['vacation'],
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        db.add(pto);db.flush()
        regenerate_period(db, principal=actor, schedule_period_id=week.id)
        assert not db.scalar(select(m.ScheduleShift.id).where(m.ScheduleShift.employee_id==people[0].id,m.ScheduleShift.shift_date==START))
        row = db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id,m.ScheduleShift.store_id==stores[0],m.ScheduleShift.shift_date==START))
        close(db, actor, stores, [START])
        remove(db, actor, week, row)
        result = regenerate_period(db, principal=actor, schedule_period_id=week.id)
        assert result['validation_status']=='VALID', result
        assert row.employee_id is None and db.get(m.ScheduleShift,row.id) is row
        assert pto.status == m.TimeOffRequestStatus.APPROVED and pto.start_date==pto.end_date==START
        assert not any(w.warning_type in {'SHIFT_ON_CLOSED_DATE','COVERAGE_GAP','NO_LEAD_OF_DAY'} and w.warning_date==START
                       for w in rebuild_schedule_warnings(db,schedule_period_id=week.id))
        approve_schedule(db, principal=actor, schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=stores)
        from app.services.v2_scheduling_service import publish_schedule, clone_published_revision
        publish_schedule(db, principal=actor, schedule_period_id=week.id, expected_version=week.version, allowed_store_ids=stores)
        revision = clone_published_revision(db, principal=actor, published_period_id=week.id, allowed_store_ids=stores)
        marker = db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==revision.id, m.ScheduleShift.source_shift_id==row.id))
        assert marker.employee_id is None and marker.manually_locked
        assert regenerate_period(db, principal=actor, schedule_period_id=revision.id)['validation_status']=='VALID'
        assert marker.employee_id is None and marker.manually_locked


@pytest.mark.parametrize('days', [3, 7])
def test_fresh_closure_week_distributes_available_shifts_fairly(scheduling_db, days):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        stores, people, week = setup(db, actor, ids)
        close(db, actor, stores, [START+timedelta(days=i) for i in range(days)])
        result = regenerate_period(db, principal=actor, schedule_period_id=week.id)
        assert result['validation_status']=='VALID', result
        report = allocation_report(db, week)
        assert sum(r['actual_shifts'] for r in report)==14-2*days
        assert max(r['actual_shifts'] for r in report)-min(r['actual_shifts'] for r in report)<=1
        # Also exercise the joint solver's zero-position/closure behavior.
        plan = plan_schedule_horizon(db, periods=[week], planning_date=START, time_limit=3)
        assert plan['assignments'] is not None, plan


def test_removal_http_permissions_csrf_and_rendering(scheduling_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth import get_current_principal
    from app.config import settings
    from app.db import get_db
    from app.routers.v2_scheduling import router, FEATURE_KEY
    from app.security.csrf import install_csrf_cookie_middleware
    from app.main import app as main_app
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        stores, _, week = setup(db, actor, ids)
        regenerate_period(db, principal=actor, schedule_period_id=week.id)
        row = db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))
        path = f'/v2/scheduling/api/periods/{week.id}/shifts/{row.id}'
        payload = dict(expected_version=week.version, employee_id=None, store_id=row.store_id,
            shift_date=str(row.shift_date), start_time=row.start_time.isoformat(),end_time=row.end_time.isoformat(),
            unpaid_break_minutes=row.unpaid_break_minutes)
        row_id = row.id
        db.commit()
    current = {'principal':Principal(actor.id, actor.username, Role.ADMIN, None, True)}
    app = FastAPI();install_csrf_cookie_middleware(app)
    app.state.templates = main_app.state.templates
    @app.middleware('http')
    async def context(request, call_next):
        request.state.principal = current['principal']
        request.state.permission_flags = {'scheduling.view_all':True,'scheduling.edit_draft_shifts':True}
        return await call_next(request)
    def database():
        with Session() as db:
            yield db
    app.dependency_overrides[get_current_principal] = lambda:current['principal']
    app.dependency_overrides[get_db] = database
    app.include_router(router)
    monkeypatch.setattr(settings,'v2_enabled_features',FEATURE_KEY)
    monkeypatch.setattr(settings,'v2_principal_features','')
    monkeypatch.setattr(settings,'session_cookie_secure',False)
    with TestClient(app, follow_redirects=False, client=('127.0.0.1',50000)) as client:
        page = client.get(f'/v2/scheduling/week?start={START}')
        assert page.status_code==200 and 'Remove assignment' in page.text
        csrf = client.cookies.get('csrf_token')
        assert client.patch(path, json=payload).status_code==403
        current['principal'] = Principal(actor.id, actor.username, Role.STORE, None, True)
        assert client.patch(path, json=payload, headers={'X-CSRF-Token':csrf}).status_code==403
        current['principal'] = Principal(actor.id, actor.username, Role.LEAD, None, True)
        assert 'Remove assignment' in client.get(f'/v2/scheduling/week?start={START}').text
        response = client.patch(path, json=payload, headers={'X-CSRF-Token':csrf})
        assert response.status_code==200, response.text
        row = next(r for r in response.json()['board']['shifts'] if r['id']==row_id)
        assert row['intentionally_unfilled'] and row['requires_coverage'] and not row['business_closed']
        assert 'Intentionally unfilled' in client.get(f'/v2/scheduling/week?start={START}').text


def test_removed_legacy_double_coverage_retains_source_and_vacancy(scheduling_db):
    from test_v2_scheduling_exact_coverage import setup_day
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        week, store = setup_day(db, actor, ids, headcount=2)
        person(db, actor, 'Synthetic double one', lead=True)
        person(db, actor, 'Synthetic double two', lead=True)
        regenerate_period(db, principal=actor, schedule_period_id=week.id)
        first, second = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id).order_by(m.ScheduleShift.id)))
        second.is_double_coverage = True
        second.source_shift_id = first.id
        db.flush()
        remove(db, actor, week, second)
        regenerate_period(db, principal=actor, schedule_period_id=week.id)
        rows = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id)))
        assert len(rows)==2 and db.get(m.ScheduleShift,first.id) is first
        assert second in rows and second.employee_id is None and second.manually_locked
        close(db, actor, (store,), [START])
        assert regenerate_period(db, principal=actor, schedule_period_id=week.id)['validation_status']=='VALID'
        assert second.employee_id is None and second.manually_locked
