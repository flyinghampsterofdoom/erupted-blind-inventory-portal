"""Narrow release acceptance for weekly maintenance, owner filtering and Lead review."""
from copy import deepcopy
from datetime import date,datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select,func
from app import models as m
from app.services.v2_scheduling_service import create_draft_period,approve_schedule,publish_schedule,SchedulingValidationError
from app.services.v2_scheduling_policy_service import run_schedule_automation
from app.services.v2_scheduling_snapshot_service import capture_snapshot,serialize_schedule
from test_scheduling_lifecycle_integrity import complete,args,profile
from test_v2_scheduling_foundation import scheduling_db,_coverage
from test_v2_primary import primary,login
NOW=datetime(2030,5,5,10,tzinfo=timezone.utc)
FIRST=date(2030,5,12)


def seed(db,actor,ids,*,missing=8):
    for eid in (ids['alex'],ids['blair']):profile(db,actor,ids,eid,1)
    _coverage(db,actor,ids,weekday=2);_coverage(db,actor,ids,weekday=4)
    db.add(m.SchedulingOrganizationPolicy(id=1,schedule_length_weeks=9,replenish_at_weeks=8,updated_by_principal_id=actor.id))
    for i in range(9):
        if i!=missing:create_draft_period(db,principal=actor,week_start=FIRST+timedelta(weeks=i),notes='Retain existing revision')
    db.commit()


def test_owner_defaults_to_changes_and_all_keeps_selectors_and_evidence(primary):
    primary.account('ADMIN')
    with primary.Session() as db:
        p,_=complete(db,primary.manager,primary.ids);pid=p.id
        capture_snapshot(db,period=p,principal=primary.manager,batch_id='filter-test',origin='test',before=serialize_schedule(db,p),diagnostics={},initial=True)
        approve_schedule(db,**args(p,primary.manager,primary.ids));publish_schedule(db,**args(p,primary.manager,primary.ids));db.commit()
        frozen=[deepcopy(s.payload) for s in db.scalars(select(m.ScheduleGeneratorSnapshot))]
        a=db.scalar(select(m.ScheduleGeneratorSnapshot.id).where(m.ScheduleGeneratorSnapshot.generation_kind=='APPROVAL'));pub=db.scalar(select(m.ScheduleGeneratorSnapshot.id).where(m.ScheduleGeneratorSnapshot.generation_kind=='PUBLICATION'))
    login(primary);url=f'/v2/scheduling/periods/{pid}/history?approved_snapshot={a}&published_snapshot={pub}'
    changed=primary.client.get(url);all_rows=primary.client.get(url+'&assignments=all')
    assert changed.status_code==all_rows.status_code==200
    assert 'No changed assignments in the selected captures' in changed.text
    table=lambda html:html.split('aria-label="Assignment comparison"')[1].split('</section>')[0]
    assert '<article class="audit-diff-row' not in table(changed.text)
    assert '<article class="audit-diff-row' in table(all_rows.text)
    for page in (changed,all_rows):
        for label in ('Approved state','Published state','checksum verified','Revision history','Approval decisions'):assert label in page.text
    assert 'value="all" selected' in all_rows.text
    with primary.Session() as db:assert frozen==[s.payload for s in db.scalars(select(m.ScheduleGeneratorSnapshot))]


def test_authorized_lead_sees_monday_pending_and_overdue_without_admin_controls(primary):
    primary.account('LEAD')
    with primary.Session() as db:
        for i,delta in enumerate((-1,1)):
            p=create_draft_period(db,principal=primary.manager,week_start=FIRST+timedelta(weeks=i))
            local=datetime.now(ZoneInfo('America/Los_Angeles'))
            monday=local.date()-timedelta(days=local.weekday())+timedelta(weeks=0 if delta<0 else 1)
            p.generated_at=NOW;p.review_due_at=datetime.combine(monday,datetime.max.time(),tzinfo=local.tzinfo).astimezone(timezone.utc)
        db.commit()
    login(primary);response=primary.client.get('/v2/scheduling/automation')
    assert response.status_code==200
    for text in ('Pending review','Review overdue','Due Monday','Review Schedule'):assert text in response.text
    assert 'Save automation settings' not in response.text
    assert 'Repair future draft horizon together' not in response.text
    assert primary.client.get('/v2/scheduling/periods/1/history').status_code==200


def test_weekly_appends_one_validated_draft_and_repeat_is_noop(scheduling_db,monkeypatch):
    monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED','true');Session,actor,ids,_=scheduling_db
    with Session() as db:
        seed(db,actor,ids)
        before=[(p.id,p.notes,p.version) for p in db.scalars(select(m.SchedulePeriod).order_by(m.SchedulePeriod.id))]
        result=run_schedule_automation(db,principal=actor,now=NOW);db.commit()
        pid,=result['generated_period_ids'];p=db.get(m.SchedulePeriod,pid)
        assert p.week_start_date==FIRST+timedelta(weeks=8) and p.status==m.SchedulePeriodStatus.DRAFT
        assert p.review_due_at.astimezone(timezone.utc).weekday()==1 # UTC Tuesday is Monday end-of-day Pacific.
        assert db.scalar(select(func.count()).select_from(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==pid))==2
        assert [(db.get(m.SchedulePeriod,i).id,db.get(m.SchedulePeriod,i).notes,db.get(m.SchedulePeriod,i).version) for i,_,_ in before]==before
        assert not run_schedule_automation(db,principal=actor,now=NOW)['generated_period_ids'];db.commit()
        assert list(db.scalars(select(m.ScheduleGenerationRun.status).order_by(m.ScheduleGenerationRun.id)))==['SUCCESS','NO_CHANGES']


@pytest.mark.parametrize('missing',[0,3,None])
def test_unexpected_gap_does_not_fill_or_rewrite(scheduling_db,monkeypatch,missing):
    monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED','true');Session,actor,ids,_=scheduling_db
    with Session() as db:
        seed(db,actor,ids,missing=missing)
        if missing is None:
            # Two missed runs: two absent tail weeks rather than a bulk catch-up.
            for p in db.scalars(select(m.SchedulePeriod).where(m.SchedulePeriod.week_start_date>=FIRST+timedelta(weeks=7))):db.delete(p)
            db.commit()
        count=db.scalar(select(func.count()).select_from(m.SchedulePeriod))
        with pytest.raises(SchedulingValidationError,match='unexpected horizon gap'):run_schedule_automation(db,principal=actor,now=NOW)
        db.rollback();assert db.scalar(select(func.count()).select_from(m.SchedulePeriod))==count
        assert db.scalar(select(m.ScheduleGenerationRun.status))=='FAILED'


def test_failed_weekly_validation_rolls_back_new_week_but_retains_failure(scheduling_db,monkeypatch):
    from app.services import scheduling_approval_validation as validation
    monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED','true');Session,actor,ids,_=scheduling_db
    with Session() as db:
        seed(db,actor,ids)
        def fail(*a,**kw):raise SchedulingValidationError('Configured allocation or hard requirement failed')
        monkeypatch.setattr(validation,'validate_complete_schedule',fail)
        with pytest.raises(SchedulingValidationError):run_schedule_automation(db,principal=actor,now=NOW)
        db.commit()
        assert db.scalar(select(func.count()).select_from(m.SchedulePeriod))==8
        assert db.scalar(select(func.count()).select_from(m.ScheduleGeneratorSnapshot))==0
        assert db.scalar(select(m.ScheduleGenerationRun.status))=='FAILED'


def test_concurrent_weekly_invocations_create_one_tail(scheduling_db,monkeypatch):
    monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED','true');Session,actor,ids,_=scheduling_db
    with Session() as db:seed(db,actor,ids)
    barrier=Barrier(2)
    def run():
        with Session() as db:
            barrier.wait();r=run_schedule_automation(db,principal=actor,now=NOW);db.commit();return r
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:run(),range(2)))
    assert sorted(len(r['generated_period_ids']) for r in results)==[0,1]
