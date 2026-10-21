"""Real PostgreSQL lifecycle and authorization tests; synthetic scheduling data only."""
from copy import deepcopy
from datetime import date,datetime,time,timedelta,timezone
import pytest
from sqlalchemy import select,text,func
from sqlalchemy.exc import DBAPIError
from app import models as m
from app.auth import Principal,Role
from app.services.v2_scheduling_service import (approve_schedule,publish_schedule,create_draft_period,create_shift,update_shift,delete_shift,clone_published_revision,ShiftInput,SchedulingConflict,SchedulingValidationError)
from app.services.v2_scheduling_policy_service import run_schedule_automation,set_manual_lock
from app.services.v2_scheduling_assignments_service import override_double_coverage_employee,set_lead_of_day
from app.services.v2_scheduling_snapshot_service import canonical_checksum
from app.services.scheduling_review_tracking import monday_deadline,review_status
from app.services.v2_scheduling_rules_service import upsert_employee_profile
from test_v2_scheduling_foundation import scheduling_db,_coverage,date_hours
from test_v2_primary import primary,login
from test_v2_scheduling_prebeta import ready_period
DAY=date(2030,5,14)

def args(p,actor,ids):return dict(principal=actor,schedule_period_id=p.id,expected_version=p.version,allowed_store_ids=(ids['north'],ids['south']))
def profile(db,actor,ids,eid,n):return upsert_employee_profile(db,principal=actor,employee_id=eid,home_store_id=ids['north'],target_weekly_hours=13,target_shifts_per_week=n,allowed_store_ids=(ids['north'],ids['south']))
def complete(db,actor,ids):
    p,rows=ready_period(db,actor,ids,day=DAY)
    profile(db,actor,ids,ids['alex'],1);profile(db,actor,ids,ids['blair'],0)
    return p,rows

def values(s,**overrides):
    d={k:getattr(s,k) for k in ('employee_id','store_id','shift_date','start_time','end_time','unpaid_break_minutes','shift_type_id','is_opener','is_closer','employee_note')};d.update(overrides);return ShiftInput(**d)


def test_generate_edit_approve_publish_correct_audit(primary,monkeypatch):
    uid=primary.account('LEAD');ids=primary.ids
    monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED','true')
    with primary.Session() as db:
        db.get(m.Employee,ids['alex']).full_name='Parker — synthetic';db.get(m.Employee,ids['blair']).full_name='Lexi — synthetic'
        for eid in (ids['alex'],ids['blair']):profile(db,primary.manager,ids,eid,1)
        _coverage(db,primary.manager,ids,weekday=2);_coverage(db,primary.manager,ids,weekday=4)
        db.add(m.SchedulingOrganizationPolicy(id=1,schedule_length_weeks=1,replenish_at_weeks=0,updated_by_principal_id=primary.manager.id));db.flush()
        result=run_schedule_automation(db,principal=primary.manager,now=datetime(2030,5,5,12,tzinfo=timezone.utc));pid,=result['generated_period_ids'];p=db.get(m.SchedulePeriod,pid)
        original=db.scalar(select(m.ScheduleGeneratorSnapshot));saved=deepcopy(original.payload);original_id=original.id
        assert p.review_due_at==datetime(2030,5,7,6,59,59,999999,tzinfo=timezone.utc)
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==pid)))
        assert {s.employee_id for s in rows}=={ids['alex'],ids['blair']};db.commit()
    login(primary);c=primary.client;headers={'X-CSRF-Token':c.cookies.get('csrf_token')}
    assert 'Pending review' in c.get('/v2/scheduling/automation').text
    with primary.Session() as db:
        p=db.get(m.SchedulePeriod,pid)
        for s in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==pid)).all():
            update_shift(db,**args(p,Principal(uid,'lead',Role.LEAD,None,True),ids),shift_id=s.id,values=values(s,employee_id=ids['blair'] if s.employee_id==ids['alex'] else ids['alex']))
        v=p.version;db.commit()
    assert c.post(f'/v2/scheduling/api/periods/{pid}/publish',json={'expected_version':v},headers=headers).status_code==422
    for action in ('approve','publish'):
        r=c.post(f'/v2/scheduling/api/periods/{pid}/{action}',json={'expected_version':v},headers=headers);assert r.status_code==200,r.text
    with primary.Session() as db:
        p=db.get(m.SchedulePeriod,pid);source=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==pid)))
        before=[(s.id,s.employee_id,s.is_lead_of_day,s.employee_note) for s in source]
        a=db.scalar(select(m.ScheduleGeneratorSnapshot).where(m.ScheduleGeneratorSnapshot.generation_kind=='APPROVAL'));pub=db.scalar(select(m.ScheduleGeneratorSnapshot).where(m.ScheduleGeneratorSnapshot.generation_kind=='PUBLICATION'))
        assert a.payload['approval_id']==pub.payload['approval_id'];frozen=deepcopy(a.payload)
        correction=clone_published_revision(db,principal=primary.manager,published_period_id=pid,allowed_store_ids=(ids['north'],ids['south']))
        cs=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==correction.id))
        update_shift(db,**args(correction,primary.manager,ids),shift_id=cs.id,values=values(cs,employee_note='Reviewed transport arrangement'))
        approve_schedule(db,**args(correction,primary.manager,ids));publish_schedule(db,**args(correction,primary.manager,ids));db.commit()
        assert [(s.id,s.employee_id,s.is_lead_of_day,s.employee_note) for s in source]==before
        assert a.payload==frozen and db.get(m.ScheduleGeneratorSnapshot,original_id).payload==saved
        assert canonical_checksum(saved)==db.get(m.ScheduleGeneratorSnapshot,original_id).checksum
    lead=c.get(f'/v2/scheduling/periods/{pid}/history');assert lead.status_code==200 and 'Owner comparison' not in lead.text
    with primary.Session() as db:db.get(m.Principal,uid).role=m.PrincipalRole.ADMIN;db.commit() # disposable synthetic owner
    page=c.get(f'/v2/scheduling/periods/{pid}/history');assert page.status_code==200,page.text
    for word in ('Owner comparison','Original generated draft','Approved revision','Published revision','Parker','Lexi','Shift Changed','Schedule Approved','Schedule Published','Reviewed transport arrangement'):assert word in page.text
    assert c.get(f'/v2/scheduling/periods/{pid}/history?approved_snapshot=999999').status_code==422


@pytest.mark.parametrize('target',[0,2])
def test_approval_and_publication_allow_target_deviations_with_legal_coverage(scheduling_db,target):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,rows=complete(db,actor,ids)
        db.scalar(select(m.EmployeeSchedulingProfile).where(
            m.EmployeeSchedulingProfile.employee_id==ids['alex'])).target_shifts_per_week=target
        db.flush()
        approve_schedule(db,**args(p,actor,ids));publish_schedule(db,**args(p,actor,ids))
        assert p.status==m.SchedulePeriodStatus.PUBLISHED
        assert rows[0].employee_id==ids['alex']


@pytest.mark.parametrize('violation',['pto','never','missing_coverage','missing_lead','consecutive','rest','longview'])
def test_approval_rejects_incomplete_hard_requirements(scheduling_db,violation):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,rows=complete(db,actor,ids);s=rows[0]
        if violation=='pto':db.add(m.TimeOffRequest(employee_id=ids['alex'],start_date=DAY,end_date=DAY,full_day=True,status=m.TimeOffRequestStatus.APPROVED,reason_category_id=ids['vacation'],submitted_by_principal_id=actor.id,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        elif violation=='never':db.add(m.EmployeeSchedulingStorePreference(employee_id=ids['alex'],store_id=ids['north'],preference_level=m.StorePreferenceLevel.NEVER,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        elif violation=='missing_coverage':db.scalar(select(m.CoverageRequirement).where(m.CoverageRequirement.store_id==ids['north'])).minimum_employee_count=2
        elif violation=='missing_lead':s.is_lead_of_day=False
        elif violation in ('consecutive','rest'):
            prof=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==ids['alex']));prof.max_consecutive_work_days=1;prof.minimum_days_off_after_max_block=3 if violation=='rest' else 1
            offsets=(3,2,1) if violation=='consecutive' else (4,3,2)
            previous=create_draft_period(db,principal=actor,week_start=p.week_start_date-timedelta(days=7))
            for offset in offsets:
                day=DAY-timedelta(days=offset)
                owner=p if day>=p.week_start_date else previous
                date_hours(db,actor,ids['north'],day)
                if owner is p:_coverage(db,actor,ids,weekday=(day.weekday()+1)%7)
                db.add(m.ScheduleShift(schedule_period_id=owner.id,employee_id=ids['alex'],store_id=ids['north'],shift_date=day,start_time=time(8,45),end_time=time(22),is_lead_of_day=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        else:db.add(m.SpecialStorePolicy(store_id=ids['north'],active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush();v=p.version
        if violation in ('consecutive','rest'):
            from app.services.v2_scheduling_policy_service import evaluate_assignment
            verdict=evaluate_assignment(db,employee_id=s.employee_id,store_id=s.store_id,
                shift_date=s.shift_date,start_time=s.start_time,end_time=s.end_time,exclude_shift_id=s.id)
            assert ('MAX_CONSECUTIVE_DAYS' if violation=='consecutive' else 'REQUIRED_DAYS_OFF') in {r.code for r in verdict.reasons}
        with pytest.raises(SchedulingValidationError):approve_schedule(db,**args(p,actor,ids))
        assert db.scalar(select(func.count()).select_from(m.ScheduleApproval))==0 and p.version==v


def test_double_invalidates_and_published_alternate_routes_reject(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        date_hours(db,actor,ids['north'],DAY);db.scalar(select(m.CoverageRequirement).where(m.CoverageRequirement.store_id==ids['north'])).minimum_employee_count=2
        spare=m.Employee(full_name='Spare',normalized_name='spare',active=True,visible_to_leads=True,scheduling_lead_capable=True);db.add(spare);db.flush()
        p=create_draft_period(db,principal=actor,week_start=date(2030,5,12));shifts=[]
        for eid in (ids['alex'],ids['blair']):
            profile(db,actor,ids,eid,1);r=create_shift(db,**args(p,actor,ids),values=ShiftInput(eid,ids['north'],DAY,time(9),time(17)));shifts.append(db.get(m.ScheduleShift,r.shift_id))
        approve_schedule(db,**args(p,actor,ids));old=p.version;frozen=deepcopy(db.scalar(select(m.ScheduleGeneratorSnapshot)).payload)
        override_double_coverage_employee(db,principal=actor,shift_id=shifts[0].id,employee_id=spare.id);assert p.version==old+1
        with pytest.raises(SchedulingValidationError):publish_schedule(db,**args(p,actor,ids))
        override_double_coverage_employee(db,principal=actor,shift_id=shifts[0].id,employee_id=ids['alex'])
        with pytest.raises(SchedulingValidationError,match='Approve'):publish_schedule(db,**args(p,actor,ids))
        assert db.scalar(select(m.ScheduleGeneratorSnapshot)).payload==frozen
        approve_schedule(db,**args(p,actor,ids));publish_schedule(db,**args(p,actor,ids));before=(p.version,[(s.employee_id,s.is_lead_of_day,s.manually_locked) for s in shifts])
        for fn,kw in [(override_double_coverage_employee,{'employee_id':spare.id}),(set_lead_of_day,{}),(set_manual_lock,{'locked':False})]:
            with pytest.raises(SchedulingConflict):fn(db,principal=actor,shift_id=shifts[0].id,**kw)
        assert before==(p.version,[(s.employee_id,s.is_lead_of_day,s.manually_locked) for s in shifts])


@pytest.mark.parametrize('table,change',[('schedule_generator_snapshots',"checksum='tampered'"),('schedule_approvals','version=version+1'),('audit_log',"action='TAMPERED'")])
def test_evidence_database_immutable(scheduling_db,table,change):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,_=complete(db,actor,ids);approve_schedule(db,**args(p,actor,ids));db.commit();predicate=" WHERE action LIKE 'V2:SCHEDULING:%'" if table=='audit_log' else ''
        for sql in (f'UPDATE {table} SET {change}{predicate}',f'DELETE FROM {table}{predicate}',f'TRUNCATE {table}'):
            with pytest.raises(DBAPIError,match='immutable'):db.execute(text(sql))
            db.rollback()


def test_failed_capture_is_atomic(scheduling_db,monkeypatch):
    from app.services import v2_scheduling_snapshot_service as snapshots
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,_=complete(db,actor,ids);db.commit()
        monkeypatch.setattr(snapshots,'lifecycle_snapshot',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('evidence unavailable')))
        with pytest.raises(RuntimeError):approve_schedule(db,**args(p,actor,ids))
        db.commit();assert db.scalar(select(func.count()).select_from(m.ScheduleApproval))==0


def test_same_version_content_change_cannot_reuse_approval(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,rows=complete(db,actor,ids);approve_schedule(db,**args(p,actor,ids));old=p.version
        rows[0].employee_note='An editing route forgot to bump version';db.flush()
        with pytest.raises(SchedulingValidationError,match='changed after approval'):publish_schedule(db,**args(p,actor,ids))
        approve_schedule(db,**args(p,actor,ids));assert p.version==old+1
        assert db.scalar(select(func.count()).select_from(m.ScheduleApproval))==2
        publish_schedule(db,**args(p,actor,ids))


def test_review_tracking_and_invalidation(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,rows=complete(db,actor,ids);p.review_due_at=monday_deadline(datetime(2030,5,5,10,tzinfo=timezone.utc),'America/Los_Angeles')
        assert review_status(db,period=p,now=datetime(2030,5,6,20,tzinfo=timezone.utc))['status']=='PENDING'
        assert review_status(db,period=p,now=datetime(2030,5,7,12,tzinfo=timezone.utc))['status']=='OVERDUE'
        approve_schedule(db,**args(p,actor,ids));assert review_status(db,period=p)['status']=='APPROVED'
        set_manual_lock(db,principal=actor,shift_id=rows[0].id,locked=False)
        assert review_status(db,period=p,now=datetime(2030,5,7,12,tzinfo=timezone.utc))['status']=='OVERDUE'


def test_review_dst_dates():
    from zoneinfo import ZoneInfo
    for day in ('2026-10-25','2026-11-01','2027-03-14'):
        due=monday_deadline(datetime.fromisoformat(day+'T10:00:00+00:00'),'America/Los_Angeles');local=due.astimezone(ZoneInfo('America/Los_Angeles'))
        assert local.weekday()==0 and local.hour==23 and local.minute==59
    assert monday_deadline(datetime(2026,10,20,12,tzinfo=timezone.utc),'America/Los_Angeles') is None


@pytest.mark.parametrize('role',['ADMIN','MANAGER','LEAD','STORE'])
def test_alternate_http_routes_cannot_change_published_records(primary,role):
    primary.account(role)
    with primary.Session() as db:
        p,rows=complete(db,primary.manager,primary.ids);approve_schedule(db,**args(p,primary.manager,primary.ids));publish_schedule(db,**args(p,primary.manager,primary.ids));pid,sid,v=p.id,rows[0].id,p.version;before=(rows[0].employee_id,rows[0].is_lead_of_day,rows[0].manually_locked);db.commit()
    login(primary);c=primary.client
    for route,extra in [('double-coverage',{'employee_id':primary.ids['blair']}),('lead-of-day',{}),('lock-form',{'locked':'false'})]:
        r=c.post(f'/v2/scheduling/shifts/{sid}/{route}',data={'csrf_token':c.cookies.get('csrf_token'),**extra})
        if role=='STORE' or role=='LEAD' and route!='lock-form':assert r.status_code==403
        else:assert r.status_code==303 and 'error=' in r.headers['location']
    with primary.Session() as db:
        s=db.get(m.ScheduleShift,sid);assert db.get(m.SchedulePeriod,pid).version==v
        assert before==(s.employee_id,s.is_lead_of_day,s.manually_locked)


def test_removed_assignment_and_correction_events_remain_visible(primary):
    from app.services.scheduling_audit_history import week_history
    primary.account('ADMIN');ids=primary.ids
    with primary.Session() as db:
        date_hours(db,primary.manager,ids['north'],DAY);db.scalar(select(m.CoverageRequirement).where(m.CoverageRequirement.store_id==ids['north'])).minimum_employee_count=2
        spare=m.Employee(full_name='Replacement',normalized_name='replacement',active=True,visible_to_leads=True,scheduling_lead_capable=True);db.add(spare);db.flush()
        p=create_draft_period(db,principal=primary.manager,week_start=date(2030,5,12));rows=[]
        for eid in (ids['alex'],ids['blair']):
            r=create_shift(db,**args(p,primary.manager,ids),values=ShiftInput(eid,ids['north'],DAY,time(9),time(17)));rows.append(db.get(m.ScheduleShift,r.shift_id))
        override_double_coverage_employee(db,principal=primary.manager,shift_id=rows[0].id,employee_id=spare.id)
        delete_shift(db,**args(p,primary.manager,ids),shift_id=rows[0].id);db.commit();pid=p.id
        events=week_history(db,p)['audits'];assert any(a.action.endswith('DOUBLE_COVERAGE_ASSIGNMENT_CHANGED') for a in events)
    login(primary);page=primary.client.get(f'/v2/scheduling/periods/{pid}/history')
    assert page.status_code==200 and 'DOUBLE_COVERAGE_ASSIGNMENT_CHANGED' in page.text and 'SHIFT_DELETED' in page.text


def test_longview_missing_weekly_rotation_blocks_approval(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,rows=complete(db,actor,ids)
        db.add(m.SpecialStorePolicy(store_id=ids['north'],active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        for eid,participation in [(ids['alex'],m.SpecialStoreParticipation.PRIMARY),(ids['blair'],m.SpecialStoreParticipation.ROTATION)]:
            db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==eid)).special_store_participation=participation
            db.add(m.SpecialStoreRotationState(store_id=ids['north'],employee_id=eid,participation=participation,queue_position=0))
        db.flush()
        with pytest.raises(SchedulingValidationError,match='traveler|Traveler|Longview'):approve_schedule(db,**args(p,actor,ids))


def test_legacy_approval_is_retained_and_requires_new_captured_attestation(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,_=complete(db,actor,ids);old=p.version
        legacy=m.ScheduleApproval(schedule_period_id=p.id,version=old,actor_principal_id=actor.id);db.add(legacy);db.flush();oldid=legacy.id
        with pytest.raises(SchedulingValidationError,match='legacy'):publish_schedule(db,**args(p,actor,ids))
        approve_schedule(db,**args(p,actor,ids));assert p.version==old+1
        assert db.get(m.ScheduleApproval,oldid).version==old
        publish_schedule(db,**args(p,actor,ids))


@pytest.mark.parametrize('gap',[1,2])
def test_approval_rechecks_constraints_across_week_boundary(scheduling_db,gap):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        p,rows=complete(db,actor,ids)
        prof=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==ids['alex']))
        prof.max_consecutive_work_days=1;prof.minimum_days_off_after_max_block=3
        rows[0].shift_date=p.week_start_date
        date_hours(db,actor,ids['north'],p.week_start_date)
        previous=create_draft_period(db,principal=actor,week_start=p.week_start_date-timedelta(days=7))
        for offset in (gap,gap+1,gap+2):
            db.add(m.ScheduleShift(schedule_period_id=previous.id,employee_id=ids['alex'],store_id=ids['south'],shift_date=p.week_start_date-timedelta(days=offset),start_time=time(9),end_time=time(17),created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush()
        with pytest.raises(SchedulingValidationError,match='consecutive|rest|day.*off'):
            approve_schedule(db,**args(p,actor,ids))
        assert db.scalar(select(func.count()).select_from(m.ScheduleApproval))==0
