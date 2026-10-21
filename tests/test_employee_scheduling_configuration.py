from datetime import date,time,timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from starlette.datastructures import FormData
from app import models as m
from app.services.employee_scheduling_configuration import weekday_mapping,configuration_view,save_configuration,STATES
from app.services.v2_scheduling_policy_service import evaluate_assignment,base_pattern_score
from app.services.v2_scheduling_service import SchedulingValidationError,SchedulingConflict
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import person,period,shift
from test_v2_primary import primary,login


def legacy(a=None,b=None):
    return SimpleNamespace(weekday_preferences=None,week_a_workdays_mask=a,week_b_workdays_mask=b)


def window(day,kind,start=time.min,end=time.max):
    return SimpleNamespace(day_of_week=day,kind=m.SchedulingWindowKind(kind),start_time=start,end_time=end,active=True)


def test_identical_legacy_pattern_maps_without_losing_off_day_preferences():
    states,issues=weekday_mapping(legacy(28,28),[])
    assert not issues
    assert states==['PREFER_OFF','PREFER_OFF','PREFER_WORK','PREFER_WORK','PREFER_WORK','PREFER_OFF','PREFER_OFF']
    assert weekday_mapping(legacy(0,0),[])[0]==['AVAILABLE']*7


@pytest.mark.parametrize('profile,windows',[(legacy(4,8),[]),(legacy(),[window(2,'HARD_UNAVAILABLE',time(9),time(12))]),(legacy(4,4),[window(1,'PREFERRED')])])
def test_ambiguous_legacy_inputs_are_reported_not_flattened(profile,windows):
    assert weekday_mapping(profile,windows)[1]


def form_for(db,e,today=date(2026,10,19)):
    v=configuration_view(db,e,today=today)
    p=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
    values=dict(configuration_token=v['token'],shifts_per_week=str(v['current_allocation']),max_consecutive_work_days=str(p.max_consecutive_work_days or ''),minimum_days_off_after_max_block=str(p.minimum_days_off_after_max_block),allocation_effective_date='2026-10-25')
    if not v['issues']:values.update({f'weekday_{i}':state for i,state in enumerate(v['states'])})
    if e.scheduling_lead_capable:values['lead_capable']='true'
    if e.scheduling_double_coverage:values['double_coverage']='true'
    values.update({f"preference_{s['id']}":s['value'] for s in v['normal_stores']})
    values.update({f"special_{s['id']}":s['value'] for s in v['special_stores']})
    return values


def save(db,e,actor,ids,values):
    return save_configuration(db,principal=actor,employee_id=e.id,form=FormData(values),allowed_store_ids=(ids['north'],ids['south']),today=date(2026,10,19))


@pytest.mark.parametrize('state',STATES)
def test_four_weekday_states_remain_distinct_in_eligibility_and_scoring(scheduling_db,state):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Four states',target=1)
        values=form_for(db,e);values['weekday_1']=state
        save(db,e,actor,ids,values)
        result=evaluate_assignment(db,employee_id=e.id,store_id=ids['north'],shift_date=date(2026,11,2),start_time=time(9),end_time=time(10))
        assert result.eligible==(state!='CANNOT_WORK')
        assert base_pattern_score(db,employee_id=e.id,shift_date=date(2026,11,2))=={'PREFER_WORK':1,'PREFER_OFF':-1,'AVAILABLE':0,'CANNOT_WORK':0}[state]


def test_noop_save_preserves_limits_masks_ranks_and_existing_assignments(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Preserved employee',lead=True,target=3)
        p=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        p.max_consecutive_work_days=1;p.minimum_days_off_after_max_block=2
        p.week_a_workdays_mask=p.week_b_workdays_mask=28
        db.add(m.EmployeeSchedulingStorePreference(employee_id=e.id,store_id=ids['north'],preference_level=m.StorePreferenceLevel.NEVER,preference_rank=3,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        per=period(db,actor,date(2026,11,1));row=shift(db,actor,per,e,per.week_start_date,ids['south']);row.manually_locked=True;db.flush()
        before=(row.employee_id,row.store_id,row.manually_locked,per.version)
        save(db,e,actor,ids,form_for(db,e))
        assert (p.max_consecutive_work_days,p.minimum_days_off_after_max_block,p.target_shifts_per_week)==(1,2,3)
        assert (p.week_a_workdays_mask,p.week_b_workdays_mask)==(28,28)
        assert p.weekday_preferences==['PREFER_OFF','PREFER_OFF','PREFER_WORK','PREFER_WORK','PREFER_WORK','PREFER_OFF','PREFER_OFF']
        pref=db.scalar(select(m.EmployeeSchedulingStorePreference).where(m.EmployeeSchedulingStorePreference.employee_id==e.id))
        assert pref.preference_level==m.StorePreferenceLevel.NEVER and pref.preference_rank==3
        assert (row.employee_id,row.store_id,row.manually_locked,per.version)==before
        assert per.constraints_changed_at is None


def test_hard_edit_triggers_staleness_soft_edit_does_not(scheduling_db,monkeypatch):
    monkeypatch.setattr('app.services.v2_scheduling_lead_duty_service.business_today',lambda db:date(2026,10,19))
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Soft versus hard',lead=True,target=1)
        per=period(db,actor,date(2026,11,1));row=shift(db,actor,per,e,per.week_start_date,ids['north'])
        values=form_for(db,e);values['weekday_0']='PREFER_OFF';save(db,e,actor,ids,values)
        assert per.constraints_changed_at is None
        values=form_for(db,e);values['weekday_0']='CANNOT_WORK';save(db,e,actor,ids,values)
        assert per.constraints_changed_at is not None
        assert row.employee_id==e.id
        assert db.scalar(select(m.ScheduleWarning.id).where(m.ScheduleWarning.schedule_period_id==per.id,m.ScheduleWarning.warning_type=='HARD_UNAVAILABLE'))


def test_stale_form_and_invalid_state_fail_before_mutation(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Conflict employee',target=1)
        values=form_for(db,e)
        values['weekday_1']='OFF'
        with pytest.raises(SchedulingValidationError):save(db,e,actor,ids,values)
        values=form_for(db,e)
        p=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id));p.minimum_days_off_after_max_block=2;db.flush()
        with pytest.raises(SchedulingConflict):save(db,e,actor,ids,values)
        assert p.minimum_days_off_after_max_block==2


def test_ambiguous_pattern_survives_save_of_other_fields(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Legacy alternating',target=1)
        p=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        p.week_a_workdays_mask=4;p.week_b_workdays_mask=8;db.flush()
        values=form_for(db,e);values['minimum_days_off_after_max_block']='2';save(db,e,actor,ids,values)
        assert p.weekday_preferences is None and (p.week_a_workdays_mask,p.week_b_workdays_mask)==(4,8)
        values=form_for(db,e);values.update({f'weekday_{i}':'AVAILABLE' for i in range(7)})
        with pytest.raises(SchedulingValidationError):save(db,e,actor,ids,values)


def test_allocation_change_is_dated_exact_and_preserves_prior_baseline(scheduling_db):
    from app.services.v2_scheduling_allocation_service import weekly_allocation
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Dated allocation',target=3)
        values=form_for(db,e);values['shifts_per_week']='2';save(db,e,actor,ids,values)
        assert weekly_allocation(db,e.id,date(2026,10,18))==3
        assert weekly_allocation(db,e.id,date(2026,10,25))==2


def test_longview_never_override_survives_noop_save_with_rotation_history(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Longview history',target=1)
        db.add(m.SpecialStorePolicy(store_id=ids['south'],active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        rotation=m.SpecialStoreRotationState(employee_id=e.id,store_id=ids['south'],participation=m.SpecialStoreParticipation.ROTATION,queue_position=4,assignment_count=9)
        db.add(rotation)
        pref=m.EmployeeSchedulingStorePreference(employee_id=e.id,store_id=ids['south'],preference_level=m.StorePreferenceLevel.NEVER,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
        db.add(pref);db.flush()
        values=form_for(db,e);assert values[f"special_{ids['south']}"]=='NONE'
        save(db,e,actor,ids,values)
        assert rotation.participation==m.SpecialStoreParticipation.ROTATION and rotation.assignment_count==9 and rotation.queue_position==4
        assert pref.preference_level==m.StorePreferenceLevel.NEVER
        assert not evaluate_assignment(db,employee_id=e.id,store_id=ids['south'],shift_date=date(2026,11,2),start_time=time(9),end_time=time(10)).eligible


def test_compact_editor_renders_all_controls_and_hides_diagnostics(scheduling_db):
    from app.main import app
    from starlette.requests import Request
    from app.routers.v2_scheduling import employee_policy_page
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Compact employee',lead=True,target=3)
        request=Request({'type':'http','method':'GET','scheme':'http','path':f'/v2/hr/employees/{e.id}','query_string':b'','headers':[],'client':('test',1),'server':('test',80),'app':app})
        body=employee_policy_page(e.id,request,_feature=actor,principal=actor,db=db).body.decode()
        main=body.split('id="scheduling"')[1].split('<details')[0]
        assert main.count('type="radio"')==28
        assert 'name="shifts_per_week"' in main and 'name="max_consecutive_work_days"' in main
        assert 'Manage Days Off / PTO' in main and 'Saved scheduling summary' in main
        assert 'week_a_workday' not in body and 'week_b_workday' not in body
        assert 'Company-wide Lead fairness' not in main and 'Peer-normalized' not in main
        assert 'id="scheduling-diagnostics" open' not in body
        assert 'Advanced / Scheduling Diagnostics' in body


def test_http_compact_save_csrf_atomic_errors_and_stale_form(primary):
    primary.account('ADMIN',linked=False)
    assert login(primary).status_code==303
    with primary.Session() as db:
        actor=db.get(m.Principal,primary.manager.id)
        e=person(db,primary.manager,'HTTP compact employee',lead=True,target=3)
        eid=e.id;db.commit()
        values=form_for(db,e,today=date.today())
    path=f'/v2/hr/employees/{eid}'
    assert primary.client.get(path).status_code==200
    values['editor_version']='compact-v1'
    assert primary.client.post(path,data=values).status_code==403
    values['csrf_token']=primary.client.cookies.get('csrf_token')
    response=primary.client.post(path,data=values)
    assert response.status_code==303 and 'message=' in response.headers['location']
    assert primary.client.post(path,data=values).status_code==303
    assert 'error=' in primary.client.post(path,data=values).headers['location']
    with primary.Session() as db:
        e=db.get(m.Employee,eid);values=form_for(db,e,today=date.today())
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==eid))
        before=list(profile.weekday_preferences)
    values.update(editor_version='compact-v1',csrf_token=primary.client.cookies.get('csrf_token'),weekday_0='CANNOT_WORK')
    values[f"preference_{primary.ids['north']}"]='INVALID'
    response=primary.client.post(path,data=values)
    assert response.status_code==303 and 'error=' in response.headers['location']
    with primary.Session() as db:
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==eid))
        assert profile.weekday_preferences==before
        assert not db.scalar(select(m.EmployeeSchedulingWindow.id).where(m.EmployeeSchedulingWindow.employee_id==eid,m.EmployeeSchedulingWindow.active.is_(True)))


def test_displayed_organization_default_does_not_replace_inherited_limit(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Inherited maximum',target=3)
        p=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        p.max_consecutive_work_days=None;db.flush()
        values=form_for(db,e);values['max_consecutive_work_days']='3'
        save(db,e,actor,ids,values)
        assert p.max_consecutive_work_days is None


def test_retired_pattern_cannot_be_changed_through_legacy_profile_writer(scheduling_db):
    from app.services.v2_scheduling_rules_service import upsert_employee_profile
    from decimal import Decimal
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Retained pattern source',target=3)
        p=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        p.week_a_workdays_mask=p.week_b_workdays_mask=28;db.flush()
        save(db,e,actor,ids,form_for(db,e))
        with pytest.raises(SchedulingValidationError,match='retired'):
            upsert_employee_profile(db,principal=actor,employee_id=e.id,home_store_id=None,target_weekly_hours=Decimal(0),
                target_shifts_per_week=3,week_a_workdays_mask=4,week_b_workdays_mask=8,allowed_store_ids=(ids['north'],ids['south']))
        assert (p.week_a_workdays_mask,p.week_b_workdays_mask)==(28,28)


def test_full_day_soft_windows_are_retained_as_history(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Old preferred window',target=3)
        w=m.EmployeeSchedulingWindow(employee_id=e.id,day_of_week=1,start_time=time.min,end_time=time.max,
            kind=m.SchedulingWindowKind.PREFERRED,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
        db.add(w);db.flush();wid=w.id
        save(db,e,actor,ids,form_for(db,e))
        assert db.get(m.EmployeeSchedulingWindow,wid) is w and not w.active
        p=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        assert p.weekday_preferences[1]=='PREFER_WORK'
        assert evaluate_assignment(db,employee_id=e.id,store_id=ids['north'],shift_date=date(2026,11,2),start_time=time(9),end_time=time(10)).eligible
