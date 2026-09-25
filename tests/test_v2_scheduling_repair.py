"""Permanent regressions from the October generator audit (anonymized fixture)."""
import json
from pathlib import Path
from datetime import date, datetime, time, timedelta
import pytest
from sqlalchemy import select, Date, DateTime, Time
from sqlalchemy.orm import sessionmaker
from app import models as m
from app.services import v2_scheduling_policy_service as p
from app.services import v2_scheduling_assignments_service as a
from app.services.v2_scheduling_repair_service import repair_required_assignments
from app.services.v2_scheduling_service import create_shift, ShiftInput, SchedulingValidationError
from test_v2_scheduling_foundation import scheduling_db


def person(db, manager, name, lead=False, target=1):
    e=m.Employee(full_name=name,normalized_name=name.lower(),active=True,scheduling_active=True,visible_to_leads=True,scheduling_lead_capable=lead)
    db.add(e);db.flush()
    db.add(m.EmployeeSchedulingProfile(employee_id=e.id,active=True,target_shifts_per_week=target,max_consecutive_work_days=3,minimum_days_off_after_max_block=0,created_by_principal_id=manager.id,updated_by_principal_id=manager.id));db.flush()
    return e


def period(db,manager,day,status='DRAFT',rev=1):
    r=m.SchedulePeriod(week_start_date=day,week_end_date=day+timedelta(days=6),status=m.SchedulePeriodStatus(status),revision_number=rev,created_by_principal_id=manager.id,updated_by_principal_id=manager.id)
    db.add(r);db.flush();return r


def shift(db,manager,per,e,day,store,hour=9):
    r=m.ScheduleShift(schedule_period_id=per.id,employee_id=e.id if e else None,store_id=store,shift_date=day,start_time=time(hour),end_time=time(hour+1),unpaid_break_minutes=0,generated_from_coverage_requirement=True,created_by_principal_id=manager.id,updated_by_principal_id=manager.id)
    db.add(r);db.flush();return r


def evaluate(db,e,day,store,exclude=None,context=None):
    return p.evaluate_assignment(db,employee_id=e.id,store_id=store,shift_date=day,start_time=time(9),end_time=time(10),exclude_shift_id=exclude,schedule_period_id=context)


def load_shape(db, manager):
    fixture=json.loads((Path(__file__).parent/'fixtures/scheduling_reassignment_shape.json').read_text())
    for e in db.scalars(select(m.Employee)):e.scheduling_active=False
    stores={};employees={};periods={}
    classes={'employee_scheduling_profiles':m.EmployeeSchedulingProfile,'employee_scheduling_store_preferences':m.EmployeeSchedulingStorePreference,'employee_scheduling_windows':m.EmployeeSchedulingWindow,'coverage_requirements':m.CoverageRequirement,'store_shifts':m.StoreShift,'schedule_periods':m.SchedulePeriod,'schedule_shifts':m.ScheduleShift}
    for r in fixture['stores']:
        row=m.Store(name=f"Store {r['id']}",square_location_id=f"FIXTURE-{r['id']}",active=True);db.add(row);db.flush();stores[r['id']]=row.id
    for raw in fixture['employees']:
        r=dict(raw);old=r.pop('id');r['last_effective_date']=date.fromisoformat(r['last_effective_date']) if r['last_effective_date'] else None
        row=m.Employee(full_name=f'Worker {old}',normalized_name=f'worker {old}',visible_to_leads=True,**r);db.add(row);db.flush();employees[old]=row.id
    for table,cls in classes.items():
        for raw in fixture[table]:
            r=dict(raw);old=r.pop('id',None)
            for key,mapping in [('employee_id',employees),('store_id',stores),('home_store_id',stores),('schedule_period_id',periods)]:
                if r.get(key) is not None:r[key]=mapping[r[key]]
            for key,value in list(r.items()):
                if value is None:continue
                typ=cls.__table__.c[key].type
                if isinstance(typ,DateTime):r[key]=datetime.fromisoformat(value)
                elif isinstance(typ,Date):r[key]=date.fromisoformat(value)
                elif isinstance(typ,Time):r[key]=time.fromisoformat(value)
            row=cls(**r,created_by_principal_id=manager.id,updated_by_principal_id=manager.id);db.add(row);db.flush()
            if table=='schedule_periods':periods[old]=row.id
    return stores,employees


def test_audited_generation_discovers_three_position_chain_and_is_deterministic(scheduling_db):
    _,manager,ids,engine=scheduling_db
    with sessionmaker(engine,expire_on_commit=False,autoflush=False)() as db:
        stores,employees=load_shape(db,manager)
        per=period(db,manager,date(2026,10,11))
        results=[]
        for _ in range(2):
            result=p.regenerate_period(db,principal=manager,schedule_period_id=per.id)
            rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)))
            mapping={(r.shift_date,r.store_id):r.employee_id for r in rows}
            assert len(rows)==len(mapping)==28
            assert mapping[date(2026,10,16),stores[6]]==employees[6]
            assert mapping[date(2026,10,17),stores[6]]==employees[10]
            assert mapping[date(2026,10,17),stores[2]]==employees[9]
            # All other positions match the audited original, including the
            # existing Thursday global-Lead swap. No unrelated reshuffle.
            expected = [
                [2,8,10,9], [5,4,1,7], [1,4,6,5], [9,8,6,2],
                [8,7,10,2], [1,4,6,5], [9,7,10,116],
            ]
            assert mapping == {(date(2026,10,11)+timedelta(days=offset),stores[store]): employees[employee]
                               for offset, day in enumerate(expected)
                               for store, employee in zip((2,4,6,9),day)}
            assert result['uncovered']==result['lead_uncovered']==[]
            repairs=[d for d in result['reassignment_repairs'] if d['action']=='BOUNDED_REASSIGNMENT_SUCCEEDED']
            assert len(repairs)==1 and len(repairs[0]['changes'])==3
            for row in rows:
                check=p.evaluate_assignment(db,employee_id=row.employee_id,store_id=row.store_id,shift_date=row.shift_date,start_time=row.start_time,end_time=row.end_time,exclude_shift_id=row.id)
                assert check.eligible and not check.requires_hour_approval
            assert all(t['assigned_shifts']==t['target_shifts'] for t in result['shift_targets'] if t['employee_id']!=employees[3])
            results.append(mapping)
        assert results[0]==results[1]


@pytest.mark.parametrize('status',['DRAFT','PUBLISHED','COPY'])
@pytest.mark.parametrize('direction',['prior','following'])
def test_adjacent_boundaries_and_shared_assignment_paths(scheduling_db,monkeypatch,status,direction):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=person(db,manager,'Boundary',True,target=5);giver=person(db,manager,'Giver');giver.principal_id=manager.id;db.flush()
        prior=period(db,manager,date(2027,1,3),'DRAFT' if status=='COPY' else status)
        nxt=period(db,manager,date(2027,1,10),'DRAFT' if status=='COPY' else status)
        if status=='COPY':nxt.source_schedule_period_id=prior.id
        if direction=='prior':
            for day in [8,9]:shift(db,manager,prior,e,date(2027,1,day),ids['north'])
            shift(db,manager,nxt,e,date(2027,1,10),ids['north']);day=date(2027,1,11);cur=nxt
        else:
            for d in [10,11,12]:shift(db,manager,nxt,e,date(2027,1,d),ids['north'])
            day=date(2027,1,9);cur=prior
        slot=shift(db,manager,cur,giver,day,ids['north'])
        assert 'MAX_CONSECUTIVE_DAYS' in [r.code for r in evaluate(db,e,day,ids['north']).reasons]
        if status!='PUBLISHED':
            with pytest.raises(SchedulingValidationError,match='4 consecutive'):
                create_shift(db,principal=manager,schedule_period_id=cur.id,expected_version=cur.version,values=ShiftInput(employee_id=e.id,store_id=ids['north'],shift_date=day,start_time=time(9),end_time=time(10),unpaid_break_minutes=0),allowed_store_ids=(ids['north'],))
        with pytest.raises(SchedulingValidationError,match='4 consecutive'):
            p.create_transfer_request(db,principal=manager,shift_id=slot.id,to_employee_id=e.id,today=date(2027,1,1))
        monkeypatch.setattr(p,'list_scheduling_candidates',lambda db:[e]);monkeypatch.setattr(a,'list_scheduling_candidates',lambda db:[e])
        assert p.choose_employee_for_shift(db,shift=slot)[0] is None
        assert p.complete_weekly_targets(db,principal=manager,period=cur)==0
        outcomes=a.ensure_daily_lead_staffing(db,principal=manager,schedule_period_id=cur.id)
        assert any(r['date']==day.isoformat() for r in outcomes), (outcomes, [(r.shift_date,r.employee_id) for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==cur.id))])


def test_replacement_draft_context_rejects_four_days_and_ignores_published_overlap(scheduling_db,monkeypatch):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=person(db,manager,'Revision')
        prior=period(db,manager,date(2027,1,3));pub=period(db,manager,date(2027,1,10),'PUBLISHED');draft=period(db,manager,date(2027,1,10),rev=2)
        for d in [8,9]:shift(db,manager,prior,e,date(2027,1,d),ids['north'])
        old=shift(db,manager,pub,e,date(2027,1,14),ids['north']);current=shift(db,manager,draft,e,date(2027,1,10),ids['north'])
        assert 'MAX_CONSECUTIVE_DAYS' in [r.code for r in evaluate(db,e,date(2027,1,11),ids['north'],context=draft.id).reasons]
        assert evaluate(db,e,date(2027,1,14),ids['north'],context=draft.id).eligible
        assert evaluate(db,e,date(2027,1,11),ids['north'],context=pub.id).eligible
        giver=person(db,manager,'Revision giver');giver.principal_id=manager.id
        offered=shift(db,manager,draft,giver,date(2027,1,11),ids['north'])
        with pytest.raises(SchedulingValidationError,match='4 consecutive'):
            create_shift(db,principal=manager,schedule_period_id=draft.id,expected_version=draft.version,
                         values=ShiftInput(employee_id=e.id,store_id=ids['north'],shift_date=date(2027,1,11),start_time=time(9),end_time=time(10),unpaid_break_minutes=0),allowed_store_ids=(ids['north'],))
        with pytest.raises(SchedulingValidationError,match='4 consecutive'):
            p.create_transfer_request(db,principal=manager,shift_id=offered.id,to_employee_id=e.id,today=date(2027,1,1))
        monkeypatch.setattr(p,'list_scheduling_candidates',lambda db:[e])
        monkeypatch.setattr(a,'list_scheduling_candidates',lambda db:[e])
        e.scheduling_lead_capable=True
        db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id)).target_shifts_per_week=5
        db.flush()
        assert p.choose_employee_for_shift(db,shift=offered)[0] is None
        assert p.complete_weekly_targets(db,principal=manager,period=draft)==0
        assert any(r['date']=='2027-01-11' for r in a.ensure_daily_lead_staffing(db,principal=manager,schedule_period_id=draft.id))
        db.delete(current);db.flush()
        assert evaluate(db,e,date(2027,1,14),ids['north'],context=draft.id).eligible
        assert 'scheduling_assignment_period_id' not in db.info


@pytest.mark.parametrize('ordinary_alternative',[True,False])
def test_lead_swap_preserves_special_store_unless_required(scheduling_db,ordinary_alternative):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        low=person(db,manager,'Low');high=person(db,manager,'High',True);other=person(db,manager,'Other');donor=person(db,manager,'Donor',True)
        p.configure_special_store(db,principal=manager,store_id=ids['south'],primary_employee_ids=(),rotation_employee_ids=(low.id,high.id))
        hist=period(db,manager,date(2026,12,13),'PUBLISHED')
        for d in [14,17]:shift(db,manager,hist,high,date(2026,12,d),ids['south'])
        cur=period(db,manager,date(2027,1,3));lv=shift(db,manager,cur,None,date(2027,1,4),ids['south'],8)
        assert p.choose_employee_for_shift(db,shift=lv,planning_date=date(2027,1,1))[0].id==low.id
        lv.employee_id=low.id
        normal=shift(db,manager,cur,other,date(2027,1,4),ids['north']);normal.manually_locked=not ordinary_alternative
        source=shift(db,manager,cur,high,date(2027,1,5),ids['north']);shift(db,manager,cur,donor,date(2027,1,5),ids['north'],10);db.flush()
        diag=[];assert a.ensure_daily_lead_staffing(db,principal=manager,schedule_period_id=cur.id,diagnostics=diag)==[]
        assert (lv.employee_id==low.id)==ordinary_alternative
        assert diag[0]['longview_disrupted']==(not ordinary_alternative)
        if ordinary_alternative:assert diag[0]['special_store_preservation_influenced_selection']


@pytest.mark.parametrize('blocker',[None,'never','cutoff','pto','availability','hours','archived','square','budget'])
def test_bounded_repair_constraints_and_unaffected_assignments(scheduling_db,monkeypatch,blocker):
    from app.services import v2_scheduling_repair_service as repair
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        home=person(db,manager,'Home');flex=person(db,manager,'Flexible',True);needed=person(db,manager,'Needed',True);fixed=person(db,manager,'Fixed',True)
        cur=period(db,manager,date(2027,1,3))
        friday=shift(db,manager,cur,home,date(2027,1,8),ids['south'])
        saturday=shift(db,manager,cur,flex,date(2027,1,9),ids['south'])
        empty=shift(db,manager,cur,None,date(2027,1,9),ids['north'])
        locked=shift(db,manager,cur,fixed,date(2027,1,6),ids['north']);locked.manually_locked=True
        for e in [home,needed]:db.add(m.EmployeeSchedulingStorePreference(employee_id=e.id,store_id=ids['north'],preference_level=m.StorePreferenceLevel.NEVER,active=True,created_by_principal_id=manager.id,updated_by_principal_id=manager.id))
        if blocker=='never':db.add(m.EmployeeSchedulingStorePreference(employee_id=needed.id,store_id=ids['south'],preference_level=m.StorePreferenceLevel.NEVER,active=True,created_by_principal_id=manager.id,updated_by_principal_id=manager.id))
        if blocker=='cutoff':needed.last_effective_date=date(2027,1,2)
        if blocker=='archived':needed.scheduling_active=False
        if blocker=='square':needed.square_status='INACTIVE'
        if blocker=='hours':db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==needed.id)).approval_weekly_hours=0.5
        if blocker=='availability':
            for weekday in range(7):db.add(m.EmployeeSchedulingWindow(employee_id=needed.id,day_of_week=weekday,start_time=time.min,end_time=time.max,kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,active=True,created_by_principal_id=manager.id,updated_by_principal_id=manager.id))
        if blocker=='pto':db.add(m.TimeOffRequest(employee_id=needed.id,start_date=date(2027,1,3),end_date=date(2027,1,9),full_day=True,status=m.TimeOffRequestStatus.APPROVED,reason_category_id=ids['vacation'],created_by_principal_id=manager.id,updated_by_principal_id=manager.id))
        if blocker=='budget':monkeypatch.setattr(repair,'MAX_SEARCH_EXTENSIONS',1)
        db.flush();original={r.id:r.employee_id for r in [friday,saturday,empty,locked]};diag=[]
        repair_required_assignments(db,principal=manager,period=cur,diagnostics=diag)
        assert locked.employee_id==fixed.id and locked.manually_locked
        if blocker is None:
            assert (friday.employee_id,saturday.employee_id,empty.employee_id)==(needed.id,home.id,flex.id)
            assert len(diag[0]['changes'])==3
        elif blocker in ('archived','square'):
            # An ineligible employee never participates. Required coverage may
            # still use the existing legal above-target fallback among the
            # remaining active employees, whose own targets are already met.
            assert all(r.employee_id != needed.id for r in [friday,saturday,empty,locked])
            assert empty.employee_id is not None
            for row in [friday,saturday,empty,locked]:
                result=evaluate(db,db.get(m.Employee,row.employee_id),row.shift_date,row.store_id,exclude=row.id)
                assert result.eligible and not result.requires_hour_approval
        else:
            assert {r.id:r.employee_id for r in [friday,saturday,empty,locked]}==original
        if blocker=='budget':assert diag[-1]['bound_reached']


def test_target_repair_uses_surplus_position_before_creating_overlap(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        surplus=person(db,manager,'Surplus',True);needed=person(db,manager,'Required',True)
        cur=period(db,manager,date(2027,1,3))
        shift(db,manager,cur,surplus,date(2027,1,8),ids['north'])
        shift(db,manager,cur,surplus,date(2027,1,9),ids['north'])
        diag=[];assert p.complete_weekly_targets(db,principal=manager,period=cur,diagnostics=diag)==0
        for e in [surplus,needed]:assert p.weekly_work_pattern(db,employee_id=e.id,shift_date=date(2027,1,3)).worked_shifts==1
        assert diag[0]['reason']=='TARGET_WITHOUT_OVERLAP'


def test_proposed_sunday_before_existing_monday_tuesday_wednesday(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=person(db,manager,'Following Sunday');cur=period(db,manager,date(2027,1,10))
        for d in (11,12,13):shift(db,manager,cur,e,date(2027,1,d),ids['north'])
        assert 'MAX_CONSECUTIVE_DAYS' in [r.code for r in evaluate(db,e,date(2027,1,10),ids['north']).reasons]


def test_unavoidable_target_overlap_is_retained_and_diagnosed(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        fixed=person(db,manager,'Locked coverage',True);required=person(db,manager,'Required overlap',True)
        cur=period(db,manager,date(2027,1,3));locked=shift(db,manager,cur,fixed,date(2027,1,8),ids['north']);locked.manually_locked=True;db.flush()
        diag=[];assert p.complete_weekly_targets(db,principal=manager,period=cur,diagnostics=diag)==1
        addition=next(d for d in diag if d['action']=='TARGET_ADDITION_AFTER_BOUNDED_SEARCH')
        assert addition['overlap_dates']==['2027-01-08']
        assert locked.employee_id==fixed.id and locked.manually_locked
        assert p.weekly_work_pattern(db,employee_id=required.id,shift_date=cur.week_start_date).worked_shifts==1
        assert p.complete_weekly_targets(db,principal=manager,period=cur)==0


def test_replacement_hours_use_draft_without_weakening_approval(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=person(db,manager,'Replacement hours')
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        profile.approval_weekly_hours=14
        pub=period(db,manager,date(2027,1,10),'PUBLISHED');draft=period(db,manager,date(2027,1,10),rev=2)
        shift(db,manager,pub,e,date(2027,1,11),ids['north'])
        current=shift(db,manager,draft,e,date(2027,1,11),ids['north'],8);current.end_time=time(22);db.flush()
        proposed=evaluate(db,e,date(2027,1,12),ids['north'],context=draft.id)
        assert proposed.resulting_hours==15 and proposed.requires_hour_approval
        published=evaluate(db,e,date(2027,1,12),ids['north'],context=pub.id)
        assert published.resulting_hours==2 and not published.requires_hour_approval


def test_uncovered_repair_retains_legal_above_target_fallback(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        flexible=person(db,manager,'Flexible coverage',True);restricted=person(db,manager,'Restricted coverage',True)
        db.add(m.EmployeeSchedulingStorePreference(employee_id=restricted.id,store_id=ids['north'],preference_level=m.StorePreferenceLevel.NEVER,active=True,created_by_principal_id=manager.id,updated_by_principal_id=manager.id))
        cur=period(db,manager,date(2027,1,3))
        empty=shift(db,manager,cur,None,date(2027,1,8),ids['north'])
        donor=shift(db,manager,cur,flexible,date(2027,1,8),ids['south'])
        untouched=shift(db,manager,cur,restricted,date(2027,1,7),ids['south']);untouched.manually_locked=True;db.flush()
        assert p.choose_employee_for_shift(db,shift=empty)[0] is None
        assert repair_required_assignments(db,principal=manager,period=cur)==1
        assert empty.employee_id==flexible.id and donor.employee_id==restricted.id
        assert untouched.employee_id==restricted.id and untouched.manually_locked
        assert p.weekly_work_pattern(db,employee_id=restricted.id,shift_date=cur.week_start_date).worked_shifts==2
        assert evaluate(db,restricted,donor.shift_date,ids['south'],exclude=donor.id).eligible
