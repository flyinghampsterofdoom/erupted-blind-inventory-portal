"""A normal Lead login can be excluded through existing scheduling eligibility."""
from datetime import date,time
from sqlalchemy import select
from app import models as m
from app.auth import Principal,Role
from app.security.passwords import hash_password
from app.services.access_control_service import effective_permission_flags
from app.services.employee_profile_service import save_employee_account
from app.services.v2_scheduling_roster_service import list_scheduling_candidates
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_policy_service import evaluate_assignment,weekend_fairness
from app.services.v2_scheduling_horizon_service import plan_schedule_horizon
from app.v2.store_scope import list_authorized_stores
from test_v2_primary import primary,post,PASSWORD
from test_v2_employee_profiles import _admin
from test_v2_scheduling_foundation import scheduling_db
from test_scheduling_lifecycle_integrity import complete


def test_real_lead_login_and_admin_denial_with_scheduling_disabled(primary):
    with primary.Session() as db:
        actor=_admin(db)
        employee=m.Employee(full_name='Test Lead',normalized_name='test lead',active=True,scheduling_active=False)
        db.add(employee);db.flush()
        account=save_employee_account(db,actor=actor,employee_id=employee.id,action='create',role='LEAD',login='test-lead@example.test',confirmed=True)
        account.password_hash=hash_password(PASSWORD)
        lead=Principal(account.id,account.username,Role.LEAD,None,True)
        ordinary=Principal(primary.manager.id,'ordinary',Role.LEAD,None,True)
        assert effective_permission_flags(db,principal=lead)==effective_permission_flags(db,principal=ordinary)
        assert {s.id for s in list_authorized_stores(db,lead)}=={primary.ids['north'],primary.ids['south']}
        db.commit()
    client=primary.client
    response=post(client,'/login',username='test-lead@example.test',password=PASSWORD)
    assert response.status_code==303
    assert client.get('/v2/scheduling/week').status_code==200
    assert client.get('/v2/scheduling/my-schedule').status_code==200
    assert client.get('/v2/scheduling/business-hours').status_code==403
    assert post(client,'/v2/scheduling/business-hours',scope='all',calendar_date='2030-05-09',event_name='Unauthorized',mode='closed').status_code==403
    assert client.get('/management/users').status_code==403


def test_disabled_employee_does_not_change_planning_allocations_or_fairness(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        per,rows=complete(db,actor,ids)
        before=plan_schedule_horizon(db,periods=[per],planning_date=date(2030,5,5),time_limit=5)
        candidates=[e.id for e in list_scheduling_candidates(db)]
        fairness=weekend_fairness(db,employee_id=ids['alex'],weekday=5,before_date=date(2030,5,5))
        existing=[(s.id,s.employee_id,s.start_time,s.end_time) for s in rows]
        employee=m.Employee(full_name='Arbitrary disabled person',normalized_name='arbitrary disabled person',active=True,scheduling_active=False)
        db.add(employee);db.flush()
        after=plan_schedule_horizon(db,periods=[per],planning_date=date(2030,5,5),time_limit=5)
        assert [e.id for e in list_scheduling_candidates(db)]==candidates
        assert after['assignments']==before['assignments']
        assert employee.id not in (after.get('assignments') or {}).values()
        assert weekend_fairness(db,employee_id=ids['alex'],weekday=5,before_date=date(2030,5,5))==fairness
        assert [(s.id,s.employee_id,s.start_time,s.end_time) for s in rows]==existing
        board=serialize_week_board(db,week_start=per.week_start_date,selected_store_ids=(ids['north'],),all_authorized_store_ids=(ids['north'],ids['south']),permission_flags={})
        assert employee.id not in {e['id'] for e in board['employees']}
        assert db.scalar(select(m.EmployeeSchedulingProfile.id).where(m.EmployeeSchedulingProfile.employee_id==employee.id)) is None
        check=evaluate_assignment(db,employee_id=employee.id,store_id=ids['north'],shift_date=per.week_start_date,start_time=time(9),end_time=time(17))
        assert not check.eligible
