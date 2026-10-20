"""Unresolved regeneration must preserve the draft even if its caller commits."""
from datetime import date,datetime,timezone
import pytest
from sqlalchemy import select,func
from app import models as m
from app.services import v2_scheduling_policy_service as policy
from app.services.v2_scheduling_service import SchedulingValidationError
from app.services.v2_scheduling_snapshot_service import serialize_schedule
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import period,shift


def prepared(db,actor,ids):
    per=period(db,actor,date(2026,11,29))
    per.generated_at=datetime(2026,10,16,tzinfo=timezone.utc)
    row=shift(db,actor,per,db.get(m.Employee,ids['alex']),per.week_start_date,ids['north'])
    row.is_lead_of_day=True
    state=m.SpecialStoreRotationState(store_id=ids['north'],employee_id=ids['alex'],
        queue_position=1,assignment_count=2,last_assigned_shift_id=row.id)
    db.add(state)
    db.add(m.ScheduleWarning(schedule_period_id=per.id,warning_type='APPROVED_TIME_OFF',
        severity=m.ScheduleWarningSeverity.SERIOUS,shift_id=row.id,employee_id=row.employee_id,
        store_id=row.store_id,warning_date=row.shift_date,message='Retain this conflict for review.'))
    db.commit()
    return per,row,state


@pytest.mark.parametrize('code',['WEEKLY_ALLOCATION_SEARCH_LIMIT','WEEKLY_ALLOCATION_CONFLICT'])
@pytest.mark.parametrize('generated',[True,False])
def test_unresolved_regeneration_rolls_back_assignments_and_evidence_even_if_caller_commits(scheduling_db,monkeypatch,code,generated):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        per,row,state=prepared(db,actor,ids)
        if not generated:
            per.generated_at=None;db.commit()
        before=serialize_schedule(db,per)
        previous=(state.last_assigned_shift_id,state.assignment_count)
        audits=list(db.scalars(select(m.AuditLog.id).order_by(m.AuditLog.id)))
        def destructive(db,*,principal,schedule_period_id):
            state.last_assigned_shift_id=None;state.assignment_count+=1
            db.flush()
            for warning in db.scalars(select(m.ScheduleWarning).where(m.ScheduleWarning.schedule_period_id==per.id)):db.delete(warning)
            db.flush();db.delete(row);db.flush()
            shift(db,actor,per,None,per.week_start_date,ids['north'])
            per.version+=1;per.generated_at=datetime.now(timezone.utc)
            db.add(m.AuditLog(actor_principal_id=actor.id,action='TRIAL_REGENERATION',meta={}))
            db.flush()
            return {'allocation':{'status':'UNRESOLVED','diagnostics':[{'code':code,'hard_capacity_losses':{str(ids['alex']):{'configured':3,'capacity':2}}}]},'assigned':0,'uncovered':[]}
        monkeypatch.setattr(policy,'_regenerate_period',destructive)
        with pytest.raises(SchedulingValidationError,match=code) as error:
            policy.regenerate_period(db,principal=actor,schedule_period_id=per.id)
        assert 'allocation 3, eligible capacity 2' in str(error.value)
        db.commit()
        db.expire_all()
        assert serialize_schedule(db,db.get(m.SchedulePeriod,per.id))==before
        restored=db.get(m.SpecialStoreRotationState,state.id)
        assert (restored.last_assigned_shift_id,restored.assignment_count)==previous
        assert list(db.scalars(select(m.AuditLog.id).order_by(m.AuditLog.id)))==audits
        assert db.scalar(select(func.count()).select_from(m.ScheduleGeneratorSnapshot))==0


def test_successful_regeneration_still_commits_capture_and_audit(scheduling_db,monkeypatch):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        per,row,_=prepared(db,actor,ids)
        def successful(db,*,principal,schedule_period_id):
            row.employee_id=ids['blair'];per.version+=1;db.flush()
            return {'allocation':{'status':'PASS','diagnostics':[]},'assigned':1,'uncovered':[]}
        monkeypatch.setattr(policy,'_regenerate_period',successful)
        result=policy.regenerate_period(db,principal=actor,schedule_period_id=per.id)
        db.commit()
        assert result['allocation']['status']=='PASS'
        assert db.get(m.ScheduleShift,row.id).employee_id==ids['blair']
        assert db.scalar(select(func.count()).select_from(m.ScheduleGeneratorSnapshot))==1
        assert db.scalar(select(m.AuditLog.id).where(m.AuditLog.action=='V2:SCHEDULING:SCHEDULE_REGENERATED'))


def test_initial_empty_generation_can_still_report_incomplete(scheduling_db,monkeypatch):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        per=period(db,actor,date(2026,11,29));db.commit()
        def incomplete(db,*,principal,schedule_period_id):
            per.generated_at=datetime.now(timezone.utc);per.version+=1
            return {'allocation':{'status':'UNRESOLVED','diagnostics':[]},'assigned':0,'uncovered':[]}
        monkeypatch.setattr(policy,'_regenerate_period',incomplete)
        result=policy.regenerate_period(db,principal=actor,schedule_period_id=per.id)
        db.commit()
        assert result['allocation']['status']=='UNRESOLVED'
        assert db.scalar(select(func.count()).select_from(m.ScheduleGeneratorSnapshot))==1
