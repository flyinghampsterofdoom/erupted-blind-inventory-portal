"""Real PostgreSQL/HTTP net-state, atomicity, authorization and concurrency checks."""
from datetime import date,time,timedelta,datetime,timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid
import pytest
from sqlalchemy import select,func
from app import models as m
from app.auth import Principal,Role
from app.services import scheduling_shift_request_service as service
from app.services.v2_scheduling_service import SchedulingConflict,SchedulingValidationError
from app.services.v2_scheduling_rules_service import upsert_employee_profile
from test_v2_primary import primary,login
from test_v2_scheduling_foundation import scheduling_db
from test_company_schedule_visibility import seed_company,WEEK


def setup(primary,role='STORE',receiver_role='STORE',configure_profiles=True):
    pid=primary.account(role);period,sids,_,_=seed_company(primary)
    with primary.Session() as db:
        receiver=m.Principal(username='receiver@example.test',role=m.PrincipalRole(receiver_role),active=True)
        db.add(receiver);db.flush();db.get(m.Employee,primary.ids['blair']).principal_id=receiver.id
        for eid in ((primary.ids['alex'],primary.ids['blair']) if configure_profiles else ()):
            upsert_employee_profile(db,principal=primary.manager,employee_id=eid,
                home_store_id=primary.ids['north'],target_weekly_hours=40,
                allowed_store_ids=(primary.ids['north'],primary.ids['south']))
        db.commit()
        return Principal(pid,'person@example.test',Role(role),primary.ids['north'],True),Principal(receiver.id,receiver.username,Role(receiver_role),primary.ids['south'],True),period,sids


def owner(primary):
    with primary.Session() as db:
        p=m.Principal(username='owner',role=m.PrincipalRole.ADMIN,active=True);db.add(p);db.commit()
        return Principal(p.id,'owner',Role.ADMIN,None,True)


def assignments(primary,sids):
    with primary.Session() as db:return [(s.id,s.employee_id,s.manually_locked) for s in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.id.in_(sids)).order_by(m.ScheduleShift.id))]


def request(primary,a,sids,**kw):
    with primary.Session() as db:
        row=service.create_request(db,principal=a,shift_id=sids[0],counterpart_shift_id=sids[1],**kw);db.commit();return row.id


@pytest.mark.parametrize('receiver_role',['STORE','LEAD'])
def test_cross_location_atomic_swap_idempotency_and_refresh(primary,receiver_role):
    a,b,pid,sids=setup(primary,receiver_role=receiver_role);key=str(uuid.uuid4());rid=request(primary,a,sids,operation_key=key)
    before=assignments(primary,sids)
    with primary.Session() as db:
        assert service.respond_request(db,principal=b,request_id=rid,accept=True).status==m.ShiftTransferStatus.COMPLETED;db.commit()
        assert service.respond_request(db,principal=b,request_id=rid,accept=True).status==m.ShiftTransferStatus.COMPLETED;db.commit()
        assert service.create_request(db,principal=a,shift_id=sids[0],counterpart_shift_id=sids[1],operation_key=key).id==rid;db.commit()
    after=assignments(primary,sids)
    assert after[0][1]==before[1][1] and after[1][1]==before[0][1] and after[2:]==before[2:]
    assert after[0][2] and after[1][2]
    with primary.Session() as db:
        assert db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SHIFT_SWAP_COMPLETED'))==1
        assert db.get(m.SchedulePeriod,pid).version==2
        from app.services.v2_scheduling_exception_service import published_provenance
        assert published_provenance(db,db.get(m.ScheduleShift,sids[0]))['employee_id']==before[0][1]
        assert published_provenance(db,db.get(m.ScheduleShift,sids[1]))['employee_id']==before[1][1]
        from app.services.v2_scheduling_service import clone_published_revision
        for draft in db.scalars(select(m.SchedulePeriod).where(m.SchedulePeriod.week_start_date==WEEK,m.SchedulePeriod.status==m.SchedulePeriodStatus.DRAFT)):draft.status=m.SchedulePeriodStatus.ARCHIVED
        db.flush()
        copy=clone_published_revision(db,principal=primary.manager,published_period_id=pid,allowed_store_ids=tuple(db.scalars(select(m.Store.id))))
        clones=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==copy.id,m.ScheduleShift.source_shift_id.in_(sids[:2]))))
        assert len(clones)==2 and all(s.manually_locked for s in clones)
        db.rollback()
    login(primary);page=primary.client.get('/v2/scheduling/my-schedule?start='+str(WEEK))
    assert page.status_code==200 and 'Completed' in page.text
    primary.client.cookies.clear();primary.client.get('/login');assert login(primary).status_code==303
    assert 'Completed' in primary.client.get('/v2/scheduling/my-schedule?start='+str(WEEK)).text


@pytest.mark.parametrize('action',['cancel','decline','reject','stale','eligibility'])
def test_terminal_states_preserve_both_assignments(primary,action):
    a,b,pid,sids=setup(primary);before=assignments(primary,sids);rid=request(primary,a,sids)
    with primary.Session() as db:
        if action=='cancel':
            row=service.cancel_request(db,principal=a,request_id=rid);assert row.status==m.ShiftTransferStatus.CANCELLED
        elif action=='decline':
            row=service.respond_request(db,principal=b,request_id=rid,accept=False);assert row.status==m.ShiftTransferStatus.DECLINED
        elif action in ('stale','eligibility'):
            if action=='stale':db.get(m.SchedulePeriod,pid).version+=1
            else:db.get(m.Employee,primary.ids['alex']).active=False
            db.commit();row=service.respond_request(db,principal=b,request_id=rid,accept=True)
            if row:assert row.invalidated_at and row.status==m.ShiftTransferStatus.CANCELLED and row.resolution_reason
        else:
            profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==primary.ids['blair']));profile.approval_weekly_hours=1;db.commit()
            row=service.respond_request(db,principal=b,request_id=rid,accept=True);assert row.status==m.ShiftTransferStatus.PENDING_MANAGER;db.commit()
            row=service.review_request(db,principal=primary.manager,request_id=rid,approve=False,note='Not approved');assert row.status==m.ShiftTransferStatus.REJECTED
        db.commit()
    assert assignments(primary,sids)==before


def test_failure_after_joint_change_rolls_back_even_if_caller_commits(primary,monkeypatch):
    a,b,_,sids=setup(primary);rid=request(primary,a,sids);before=assignments(primary,sids)
    original=service.audit
    def fail(db,principal,row,event,**kw):
        if event=='SHIFT_SWAP_COMPLETED':raise RuntimeError('Synthetic failure after both changes')
        return original(db,principal,row,event,**kw)
    monkeypatch.setattr(service,'audit',fail)
    with primary.Session() as db:
        with pytest.raises(RuntimeError):service.respond_request(db,principal=b,request_id=rid,accept=True)
        db.commit()
        assert db.get(m.ShiftTransferRequest,rid).status==m.ShiftTransferStatus.PENDING_RECIPIENT
    assert assignments(primary,sids)==before


@pytest.mark.parametrize('conflict',['pto','availability','store','consecutive','max_hours'])
def test_operational_conflicts_wait_for_explicit_owner_exception(primary,conflict):
    a,b,_,sids=setup(primary);o=owner(primary)
    with primary.Session() as db:
        if conflict=='pto':db.add(m.TimeOffRequest(employee_id=primary.ids['blair'],start_date=WEEK,end_date=WEEK,full_day=True,status=m.TimeOffRequestStatus.APPROVED,reason_category_id=primary.ids['vacation'],submitted_by_principal_id=a.id,created_by_principal_id=a.id,updated_by_principal_id=a.id))
        elif conflict=='availability':db.add(m.EmployeeSchedulingWindow(employee_id=primary.ids['blair'],day_of_week=0,start_time=time(0),end_time=time(23,59),kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,created_by_principal_id=o.id,updated_by_principal_id=o.id))
        elif conflict=='store':db.add(m.EmployeeSchedulingStorePreference(employee_id=primary.ids['blair'],store_id=primary.ids['north'],preference_level=m.StorePreferenceLevel.NEVER,created_by_principal_id=o.id,updated_by_principal_id=o.id))
        elif conflict=='max_hours':db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==primary.ids['blair'])).maximum_weekly_hours=4
        else:
            # Proposed Sunday plus Friday/Saturday reaches the fourth day via Thursday.
            for offset in (-3,-2,-1):
                prior=m.SchedulePeriod(week_start_date=WEEK-timedelta(days=7),week_end_date=WEEK-timedelta(days=1),status=m.SchedulePeriodStatus.PUBLISHED,revision_number=1,created_by_principal_id=o.id,updated_by_principal_id=o.id) if offset==-3 else prior
                if offset==-3:db.add(prior);db.flush()
                db.add(m.ScheduleShift(schedule_period_id=prior.id,employee_id=primary.ids['blair'],store_id=primary.ids['north'],shift_date=WEEK+timedelta(days=offset),start_time=time(9),end_time=time(17),created_by_principal_id=o.id,updated_by_principal_id=o.id))
        db.commit()
    rid=request(primary,a,sids);before=assignments(primary,sids)
    with primary.Session() as db:
        row=service.respond_request(db,principal=b,request_id=rid,accept=True);db.commit()
        assert row.status==m.ShiftTransferStatus.PENDING_MANAGER
        with pytest.raises((PermissionError,SchedulingValidationError)):service.review_request(db,principal=primary.manager,request_id=rid,approve=True,note='Not an Owner override')
        db.commit();assert assignments(primary,sids)==before
        if conflict=='pto':
            with pytest.raises(SchedulingValidationError):service.review_request(db,principal=o,request_id=rid,approve=True,note='Coverage',operational_override=True)
            db.commit()
        row=service.review_request(db,principal=o,request_id=rid,approve=True,note='Sick-call coverage',operational_override=True,override_pto=conflict=='pto');db.commit()
        assert row.status==m.ShiftTransferStatus.COMPLETED
        assert row.manager_note=='Sick-call coverage'
        audit=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SHIFT_SWAP_COMPLETED'))
        assert audit.meta['metadata']['override_authorizer']==o.id and audit.meta['metadata']['restrictions']
        if conflict=='pto':assert db.scalar(select(m.TimeOffRequest)).status==m.TimeOffRequestStatus.APPROVED


@pytest.mark.parametrize('role',['STORE','LEAD'])
def test_staff_denial_and_authorized_lead_operational_exception(primary,role):
    a,b,_,sids=setup(primary,role=role)
    with primary.Session() as db:
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==primary.ids['blair']));profile.maximum_weekly_hours=4;db.commit()
    rid=request(primary,a,sids)
    with primary.Session() as db:
        service.respond_request(db,principal=b,request_id=rid,accept=True);db.commit()
        db.add(m.PrincipalPermissionOverride(principal_id=a.id,permission_key='scheduling.approve_transfer_hours',allowed=True));db.commit()
        if role=='STORE':
            with pytest.raises(PermissionError):service.review_request(db,principal=a,request_id=rid,approve=True,note='Unauthorized',operational_override=True)
            db.commit();assert db.get(m.ShiftTransferRequest,rid).status==m.ShiftTransferStatus.PENDING_MANAGER
        else:
            service.review_request(db,principal=a,request_id=rid,approve=True,note='Lead emergency coverage',operational_override=True)
            db.commit();assert db.get(m.ShiftTransferRequest,rid).status==m.ShiftTransferStatus.COMPLETED


def test_concurrent_duplicate_acceptance_completes_once(primary):
    a,b,_,sids=setup(primary);rid=request(primary,a,sids);barrier=Barrier(2)
    def accept(_):
        with primary.Session() as db:
            barrier.wait(timeout=10);row=service.respond_request(db,principal=b,request_id=rid,accept=True);db.commit();return row.status
    with ThreadPoolExecutor(max_workers=2) as pool:assert list(pool.map(accept,range(2)))==[m.ShiftTransferStatus.COMPLETED]*2
    with primary.Session() as db:assert db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SHIFT_SWAP_COMPLETED'))==1


def test_overlap_and_qualification_are_not_owner_overrideable(primary):
    a,b,pid,sids=setup(primary);o=owner(primary)
    with primary.Session() as db:
        db.add(m.ScheduleShift(schedule_period_id=pid,employee_id=primary.ids['blair'],store_id=primary.ids['north'],shift_date=WEEK,start_time=time(10),end_time=time(16),created_by_principal_id=o.id,updated_by_principal_id=o.id));db.commit()
        with pytest.raises(SchedulingValidationError,match='overlapping'):service.create_request(db,principal=a,shift_id=sids[0],counterpart_shift_id=sids[1])
        db.rollback()
        extra=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==pid,m.ScheduleShift.start_time==time(10)));db.delete(extra)
        db.get(m.ScheduleShift,sids[0]).is_lead_of_day=True;db.get(m.Employee,primary.ids['blair']).scheduling_lead_capable=False;db.commit()
        with pytest.raises(SchedulingValidationError,match='qualification'):service.create_request(db,principal=a,shift_id=sids[0],counterpart_shift_id=sids[1])


def test_required_rest_is_not_hidden_by_overrideable_consecutive_limit(primary):
    from app.services.v2_scheduling_policy_service import consecutive_policy_reasons
    reasons=consecutive_policy_reasons(work_dates={WEEK+timedelta(days=i) for i in (-3,-2,-1,2)},
        proposed_date=WEEK,max_consecutive_work_days=3,minimum_days_off_after_max_block=2)
    assert {r.code for r in reasons}=={'MAX_CONSECUTIVE_DAYS','REQUIRED_DAYS_OFF'}
    from app.services.scheduling_operational_exception_service import validate_reasons
    with primary.Session() as db:
        with pytest.raises(SchedulingValidationError,match='Cannot override'):
            validate_reasons(db,owner(primary),reasons,confirmed=True,reason='Operational need')


def test_two_concurrent_offers_cannot_double_assign_recipient(primary):
    a,b,pid,sids=setup(primary)
    with primary.Session() as db:
        for sid in sids[2:]:db.get(m.ScheduleShift,sid).employee_id=primary.ids['alex']
        p=m.Principal(username='third@example.test',role=m.PrincipalRole.STORE,active=True);db.add(p);db.flush()
        e=m.Employee(full_name='Third synthetic',normalized_name='third synthetic',principal_id=p.id,active=True,visible_to_leads=True,scheduling_lead_capable=True);db.add(e);db.flush()
        shift=m.ScheduleShift(schedule_period_id=pid,employee_id=e.id,store_id=primary.ids['north'],shift_date=WEEK,start_time=time(11),end_time=time(19),created_by_principal_id=a.id,updated_by_principal_id=a.id);db.add(shift);db.commit()
        c=Principal(p.id,p.username,Role.STORE,None,True);third=shift.id
        first=service.create_request(db,principal=a,shift_id=sids[0],to_employee_id=primary.ids['blair']);db.commit()
        second=service.create_request(db,principal=c,shift_id=third,to_employee_id=primary.ids['blair']);db.commit();rids=[first.id,second.id]
    barrier=Barrier(2)
    def accept(rid):
        with primary.Session() as db:
            barrier.wait(timeout=10);r=service.respond_request(db,principal=b,request_id=rid,accept=True);db.commit();return r.status
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(accept,rids))
    assert sorted(r.value for r in results)==['CANCELLED','COMPLETED']
    with primary.Session() as db:
        assert len(list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.id.in_([sids[0],third]),m.ScheduleShift.employee_id==primary.ids['blair']))))==1
