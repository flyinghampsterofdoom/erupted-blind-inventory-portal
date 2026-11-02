"""Local-only acceptance and integrity checks for supplemental training/replacement."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal,Role
from app.services import scheduling_training_service as t
from app.services.v2_scheduling_service import SchedulingConflict,SchedulingValidationError,scheduled_paid_minutes
from app.services.v2_scheduling_policy_service import evaluate_assignment,scheduled_weekly_hours
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings,coverage_excesses
from test_v2_scheduling_foundation import scheduling_db,opening_hours
from test_v2_scheduling_repair import person,period,shift

NOW=datetime(2026,11,2,20,tzinfo=timezone.utc)
START=date(2026,11,8)


def setup(db,actor,ids):
    for e in db.scalars(select(m.Employee)):e.scheduling_active=False
    outgoing=person(db,actor,'Outgoing employee',lead=True,target=3)
    b=person(db,actor,'Scheduled Lead B',lead=True,target=2)
    c=person(db,actor,'Scheduled Lead C',lead=True,target=2)
    trainee=person(db,actor,'New employee',lead=True,target=3)
    nonlead=person(db,actor,'Manual trainer',lead=False,target=0)
    for weekday in range(7):
        opening_hours(db,actor,ids['north'],weekday,opening=time(9,15),closing=time(10))
        db.add(m.CoverageRequirement(store_id=ids['north'],day_of_week=weekday,start_time=time(9,15),end_time=time(10),
            minimum_employee_count=1,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
    periods=[]
    for week in range(2):
        per=period(db,actor,START+timedelta(weeks=week));periods.append(per)
        for day in range(7):
            e=outgoing if day<3 else b if day<5 else c
            row=shift(db,actor,per,e,per.week_start_date+timedelta(days=day),ids['north'])
            row.is_lead_of_day=True
    enrollment=t.start_training(db,principal=actor,employee_id=trainee.id,start_date=START,now=NOW)
    db.flush()
    return outgoing,trainee,nonlead,enrollment,periods


def scheduled(db,per):
    return list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id).order_by(m.ScheduleShift.id)))


def values(rows):
    return {r.id:(r.employee_id,r.store_id,r.shift_date,r.start_time,r.end_time,r.is_lead_of_day,r.manually_locked) for r in rows}


def train(db,actor,ids,periods):
    for per in periods:t.schedule_training(db,principal=actor,period=per,allowed_store_ids=(ids['north'],),now=NOW)


def preview(db,actor,ids,enrollment,outgoing,**kwargs):
    return t.preview_replacement(db,principal=actor,training_id=enrollment.id,outgoing_id=outgoing.id,
        effective_date=kwargs.pop('effective_date',START+timedelta(days=3)),allowed_store_ids=(ids['north'],),now=NOW,**kwargs)


def test_two_weeks_paid_supplemental_lead_pairing_and_idempotency(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids)
        before=values([r for per in periods for r in scheduled(db,per)])
        train(db,actor,ids,periods)
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.is_training.is_(True))))
        assert enrollment.duration_days==14 and t.training_end(enrollment)==START+timedelta(days=14)
        assert len(rows)==6 and sum(scheduled_paid_minutes(r) for r in rows)==360
        assert all(db.get(m.ScheduleShift,r.trainer_shift_id).is_lead_of_day for r in rows)
        assert all(not r.is_lead_of_day and not r.is_shift_lead and not r.is_double_coverage for r in rows)
        assert before=={r.id:v for r in [r for per in periods for r in scheduled(db,per)] if not r.is_training for v in [values([r])[r.id]]}
        assert scheduled_weekly_hours(db,employee_id=trainee.id,shift_date=START)==Decimal('3.00')
        for per in periods:
            assert coverage_excesses(db,period=per)==[]
            warnings=rebuild_schedule_warnings(db,schedule_period_id=per.id)
            assert not [w for w in warnings if w.warning_type in {'EXCESS_COVERAGE','NO_LEAD_OF_DAY','INVALID_TRAINING'}]
        train(db,actor,ids,periods)
        assert len(list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.is_training.is_(True)))))==6
        # Training cannot masquerade as one of the required ordinary positions.
        verdict=evaluate_assignment(db,employee_id=trainee.id,store_id=ids['north'],shift_date=START+timedelta(days=3),
            start_time=time(9),end_time=time(10),schedule_period_id=periods[0].id)
        assert 'TRAINING_EMPLOYEE' in {r.code for r in verdict.reasons}


def test_manual_trainer_override_and_manual_move_are_audited(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,nonlead,enrollment,periods=setup(db,actor,ids)
        other=shift(db,actor,periods[0],nonlead,START+timedelta(days=4),ids['north'])
        with pytest.raises(SchedulingValidationError,match='non-lead'):
            t.assign_trainer(db,principal=actor,training_id=enrollment.id,trainer_shift_id=other.id,allowed_store_ids=(ids['north'],),now=NOW)
        row=t.assign_trainer(db,principal=actor,training_id=enrollment.id,trainer_shift_id=other.id,allowed_store_ids=(ids['north'],),
            confirmed_override=True,override_reason='Authorized product training with an experienced employee.',now=NOW)
        assert row.trainer_employee_id==nonlead.id and row.trainer_override_reason
        lead=next(r for r in scheduled(db,periods[0]) if r.employee_id==outgoing.id)
        moved=t.assign_trainer(db,principal=actor,training_id=enrollment.id,trainer_shift_id=lead.id,shift_id=row.id,
            allowed_store_ids=(ids['north'],),now=NOW)
        assert moved.id==row.id and moved.shift_date==lead.shift_date and moved.trainer_override_reason is None
        assert db.scalar(select(m.AuditLog.id).where(m.AuditLog.action=='V2:SCHEDULING:TRAINER_ASSIGNED'))


def test_early_completion_preview_confirm_transfer_and_unaffected_preservation(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        before=values([r for per in periods for r in scheduled(db,per)])
        p=preview(db,actor,ids,enrollment,outgoing)
        assert p.payload['preview']['conflicts']==[]
        assert len(p.payload['preview']['changes'])==3
        assert len(p.payload['preview']['training_removals'])==3
        assert before==values([r for per in periods for r in scheduled(db,per)])
        assert db.get(m.EmployeeTraining,enrollment.id).completed_effective_date is None
        with pytest.raises(SchedulingValidationError,match='confirmation'):
            t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),now=NOW)
        result=t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)
        assert db.get(m.EmployeeTraining,enrollment.id).completed_effective_date==START+timedelta(days=3)
        rows=[r for per in periods for r in scheduled(db,per)]
        changed_ids={r['shift_id'] for r in result['changes']};removed_ids={r['shift_id'] for r in result['training_removals']}
        for r in rows:
            if r.id in changed_ids:
                assert r.employee_id==trainee.id and before[r.id][1:5]==values([r])[r.id][1:5]
            else:assert values([r])[r.id]==before[r.id]
        assert set(before)-{r.id for r in rows}==removed_ids
        assert outgoing.active and outgoing.scheduling_active and not outgoing.last_effective_date
        assert t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)==result


@pytest.mark.parametrize('blocker',['availability','overlap','qualification','protected','hours'])
def test_conflicts_are_reviewed_and_no_partial_application(scheduling_db,blocker):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        day=START+timedelta(days=7)
        if blocker=='availability':
            db.add(m.EmployeeSchedulingWindow(employee_id=trainee.id,day_of_week=0,start_time=time.min,end_time=time.max,
                kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        elif blocker=='overlap':shift(db,actor,periods[1],trainee,day,ids['north'])
        elif blocker=='qualification':trainee.scheduling_lead_capable=False
        elif blocker=='protected':next(r for r in scheduled(db,periods[1]) if r.employee_id==outgoing.id).manually_locked=True
        else:db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==trainee.id)).maximum_weekly_hours=Decimal('2')
        db.flush();before=values([r for per in periods for r in scheduled(db,per)])
        p=preview(db,actor,ids,enrollment,outgoing)
        assert p.payload['preview']['conflicts']
        with pytest.raises(SchedulingValidationError,match='conflicts'):
            t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)
        assert before==values([r for per in periods for r in scheduled(db,per)])
        assert db.get(m.EmployeeTraining,enrollment.id).completed_effective_date is None


def test_protected_assignment_requires_specific_reviewed_consent(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids)
        protected=next(r for r in scheduled(db,periods[1]) if r.employee_id==outgoing.id)
        protected.manually_locked=True;db.flush()
        p=preview(db,actor,ids,enrollment,outgoing,protected_ids=[protected.id])
        assert p.payload['preview']['conflicts']==[]
        assert next(r for r in p.payload['preview']['changes'] if r['shift_id']==protected.id)['protected']
        t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)
        assert protected.employee_id==trainee.id and protected.manually_locked


def test_stale_preview_rejected_and_failure_rolls_back_every_change(scheduling_db,monkeypatch):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        p=preview(db,actor,ids,enrollment,outgoing)
        trainee.phone='changed after review';db.flush()
        with pytest.raises(SchedulingConflict,match='changed'):
            t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)
        p=preview(db,actor,ids,enrollment,outgoing)
        before=values([r for per in periods for r in scheduled(db,per)])
        audit=t._audit
        def fail(db,**kwargs):
            if kwargs['action']=='TRAINING_COMPLETED_REPLACEMENT':raise RuntimeError('Injected final persistence failure')
            return audit(db,**kwargs)
        monkeypatch.setattr(t,'_audit',fail)
        with pytest.raises(RuntimeError,match='Injected'):
            t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)
        db.commit()  # Catch-and-commit must still leave no partial mutations.
        assert before==values([r for per in periods for r in scheduled(db,per)])
        assert db.get(m.EmployeeTraining,enrollment.id).completed_effective_date is None
        assert db.get(m.SchedulingReplacementProposal,p.id).applied_at is None


def test_published_replacement_creates_drafts_and_preserves_history(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        source=periods[1];source.status=m.SchedulePeriodStatus.PUBLISHED;source.published_at=NOW
        historical=period(db,actor,date(2026,10,25),'ARCHIVED');historical.is_historical=True
        old=shift(db,actor,historical,outgoing,date(2026,10,26),ids['north']);db.flush()
        source_before=values(scheduled(db,source));history_before=values([old])
        p=preview(db,actor,ids,enrollment,outgoing)
        assert p.payload['preview']['conflicts']==[]
        result=t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)
        assert source_before==values(scheduled(db,source)) and history_before==values([old])
        assert source.status==m.SchedulePeriodStatus.PUBLISHED
        draft=db.get(m.SchedulePeriod,result['period_ids'][0])
        assert draft.status==m.SchedulePeriodStatus.DRAFT and draft.supersedes_schedule_period_id==source.id
        assert len(scheduled(db,draft))==7
        assert db.scalar(select(m.AuditLog.id).where(m.AuditLog.action=='V2:SCHEDULING:TRAINING_COMPLETED_REPLACEMENT'))


def test_scope_and_role_are_enforced_in_services(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids)
        with pytest.raises(PermissionError):
            t.preview_replacement(db,principal=actor,training_id=enrollment.id,outgoing_id=outgoing.id,
                effective_date=START,allowed_store_ids=(ids['south'],),now=NOW)
        with pytest.raises(PermissionError):
            t.start_training(db,principal=Principal(actor.id,actor.username,Role.STORE,None,True),employee_id=trainee.id,
                start_date=START+timedelta(days=20),now=NOW)


def test_training_hours_and_trainer_reservations_survive_optimizer(scheduling_db):
    from app.services.v2_scheduling_horizon_service import plan_schedule_horizon,apply_schedule_horizon_plan
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        before=values(scheduled(db,periods[0]));training={r.id:r.trainer_employee_id for r in scheduled(db,periods[0]) if r.is_training}
        plan=plan_schedule_horizon(db,periods=[periods[0]],planning_date=NOW.date(),repair=True)
        assert plan['assignments'] is not None
        assert not set(training)&set(plan['assignments'])
        apply_schedule_horizon_plan(db,periods=[periods[0]],plan=plan,principal=actor)
        for row in scheduled(db,periods[0]):
            if row.is_training:
                assert row.trainer_employee_id==training[row.id] and t.trainer_reasons(db,row)==[]
        assert not [r for r in scheduled(db,periods[0]) if not r.is_training and r.employee_id==trainee.id]


def test_past_work_and_past_training_stay_unchanged(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        late=datetime(2026,11,16,16,tzinfo=timezone.utc)  # 08:00 local, before the day's shifts.
        past=[r for per in periods for r in scheduled(db,per) if r.shift_date<late.date()]
        before=values(past)
        p=t.preview_replacement(db,principal=actor,training_id=enrollment.id,outgoing_id=outgoing.id,
            effective_date=date(2026,11,16),allowed_store_ids=(ids['north'],),now=late)
        assert p.payload['preview']['conflicts']==[]
        assert len(p.payload['preview']['changes'])==2
        t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=late)
        assert before==values([db.get(m.ScheduleShift,k) for k in before])
        assert sum(r.is_training for r in past)==4


def test_partial_training_time_is_paid_and_requires_full_trainer_presence(scheduling_db):
    from app.services.v2_scheduling_rules_service import create_compensation_rate,estimate_labor_cost
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids)
        trainer=next(r for r in scheduled(db,periods[0]) if r.employee_id==outgoing.id)
        row=t.assign_trainer(db,principal=actor,training_id=enrollment.id,trainer_shift_id=trainer.id,
            allowed_store_ids=(ids['north'],),start_time=time(9,30),end_time=time(10),unpaid_break_minutes=0,now=NOW)
        assert scheduled_paid_minutes(row)==30 and row.manually_locked
        create_compensation_rate(db,principal=actor,employee_id=trainee.id,effective_start_date=START,hourly_rate=Decimal('20'))
        costs=estimate_labor_cost(db,schedule_period_id=periods[0].id,permitted=True,allowed_store_ids=(ids['north'],))
        assert costs.estimated_cost==Decimal('10.00') and costs.costed_paid_hours==Decimal('0.50')
        with pytest.raises(SchedulingValidationError,match='entire training shift'):
            t.assign_trainer(db,principal=actor,training_id=enrollment.id,trainer_shift_id=trainer.id,shift_id=row.id,
                allowed_store_ids=(ids['north'],),start_time=time(8),end_time=time(10),now=NOW)
        assert row.start_time==time(9,30)


def test_duration_changes_and_expired_review_fail_safely(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids)
        t.adjust_duration(db,principal=actor,training_id=enrollment.id,duration_days=21,now=NOW)
        assert enrollment.duration_days==21
        train(db,actor,ids,periods)
        with pytest.raises(SchedulingValidationError,match='beyond the new end date'):
            t.adjust_duration(db,principal=actor,training_id=enrollment.id,duration_days=7,now=NOW)
        p=preview(db,actor,ids,enrollment,outgoing)
        with pytest.raises(SchedulingConflict,match='expired'):
            t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW+timedelta(minutes=31))
        assert db.get(m.EmployeeTraining,enrollment.id).completed_effective_date is None


def test_failed_multiweek_replacement_rolls_back_versions_and_audit(scheduling_db,monkeypatch):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        p=preview(db,actor,ids,enrollment,outgoing,effective_date=START)
        assert len(p.payload['preview']['changes'])==6
        before=values([r for per in periods for r in scheduled(db,per)])
        versions={p.id:p.version for p in periods}
        from sqlalchemy import func
        count=db.scalar(select(func.count(m.AuditLog.id)))
        audit=t._audit
        def fail(db,**kwargs):
            if kwargs['action']=='TRAINING_COMPLETED_REPLACEMENT':raise RuntimeError('Final write failed')
            return audit(db,**kwargs)
        monkeypatch.setattr(t,'_audit',fail)
        with pytest.raises(RuntimeError):
            t.confirm_replacement(db,principal=actor,proposal_id=p.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)
        db.commit()
        assert before==values([r for per in periods for r in scheduled(db,per)])
        assert versions=={p.id:p.version for p in periods}
        assert count==db.scalar(select(func.count(m.AuditLog.id)))


def test_trainees_cannot_fill_minimum_staffing_or_lead_even_if_capable(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        first=next(r for r in scheduled(db,periods[0]) if r.employee_id==outgoing.id)
        # Simulate a vacancy independently of supplemental paid training.
        first.employee_id=None;first.is_lead_of_day=False;db.flush()
        warnings=rebuild_schedule_warnings(db,schedule_period_id=periods[0].id)
        assert any(w.warning_type in {'NO_ASSIGNED_EMPLOYEE','INSUFFICIENT_COVERAGE'} and w.warning_date==first.shift_date for w in warnings)
        assert any(w.warning_type=='NO_LEAD_OF_DAY' and w.warning_date==first.shift_date for w in warnings)
        from app.services.v2_scheduling_assignments_service import set_lead_of_day
        training=next(r for r in scheduled(db,periods[0]) if r.is_training)
        with pytest.raises(SchedulingValidationError,match='Training'):
            set_lead_of_day(db,principal=actor,shift_id=training.id)


from test_v2_employee_profiles import employee_client


def test_profile_browser_flow_preview_confirmation_and_csrf(employee_client):
    import re
    client,current,Session,ids=employee_client
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,current['principal'],ids)
        eid,training_id,outgoing_id=trainee.id,enrollment.id,outgoing.id
        original=values([r for per in periods for r in scheduled(db,per)])
        db.commit()
    path=f'/v2/scheduling/employees/{eid}/training'
    assert client.get(path).status_code==200
    assert client.post(path,data={'action':'schedule','training_id':training_id,'csrf_token':'invalid'}).status_code==403
    csrf=client.cookies.get('csrf_token')
    response=client.post(path,data={'action':'schedule','training_id':training_id,'csrf_token':csrf})
    assert response.status_code==303
    page=client.get(path)
    assert 'TRAINING' in page.text and 'End Training' in page.text and 'Calendar days' in page.text
    response=client.post(path+'/replacement/preview',data={'training_id':training_id,'outgoing_id':outgoing_id,
        'effective_date':str(START+timedelta(days=3)),'csrf_token':csrf})
    assert response.status_code==200
    assert 'Confirm Replacement &amp; End Training' in response.text
    assert 'Proposed one-for-one transfers (3)' in response.text
    proposal_id=re.search(r'name="proposal_id" value="([^"]+)"',response.text).group(1)
    with Session() as db:
        assert all(db.get(m.ScheduleShift,k).employee_id==v[0] for k,v in original.items())
    response=client.post(path+'/replacement/confirm',data={'proposal_id':proposal_id,'confirmed':'true','csrf_token':csrf})
    assert response.status_code==303 and 'Training%20completed' in response.headers['location']
    assert client.get(path).status_code==200


def test_training_ui_checks_edit_permission_independently(employee_client):
    client,current,Session,ids=employee_client
    with Session() as db:
        db.add(m.PrincipalPermissionOverride(principal_id=current['principal'].id,
            permission_key='scheduling.edit_draft_shifts',allowed=False));db.commit()
    assert client.get(f'/v2/scheduling/employees/{ids["alex"]}/training').status_code==403


def test_training_migration_is_additive_and_evidence_blocks_downgrade(scheduling_db):
    from alembic import command
    from sqlalchemy import text
    from app.schema_contract import _alembic_config,current_revision,HEAD_REVISION
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        original=person(db,actor,'Migration employee',lead=True)
        trainee=person(db,actor,'Migration trainee')
        per=period(db,actor,START)
        row=shift(db,actor,per,original,START,ids['north'])
        db.commit()
        config=_alembic_config(engine.url.render_as_string(hide_password=False))
        def facts():
            with engine.connect() as c:
                return {name:list(c.execute(text(f'SELECT to_jsonb(t)::text FROM {name} t ORDER BY id')).scalars())
                        for name in ['employees','schedule_periods','schedule_shifts','audit_log']}
        before=facts()
        command.downgrade(config,'20261027_0053');command.upgrade(config,HEAD_REVISION)
        assert facts()==before and current_revision(engine)==HEAD_REVISION
        enrollment=t.start_training(db,principal=actor,employee_id=trainee.id,start_date=START,now=NOW)
        db.commit()
        with pytest.raises(RuntimeError,match='history must be retained'):
            command.downgrade(config,'20261027_0053')
        assert current_revision(engine)==HEAD_REVISION
        assert db.get(m.EmployeeTraining,enrollment.id).duration_days==14
        assert db.get(m.ScheduleShift,row.id).employee_id==original.id


def test_owner_designated_shift_lead_can_train_without_global_role_change(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,manual,enrollment,periods=setup(db,actor,ids)
        trainer=shift(db,actor,periods[0],manual,START+timedelta(days=4),ids['north'])
        trainer.is_shift_lead=True;db.flush()
        row=t.assign_trainer(db,principal=actor,training_id=enrollment.id,trainer_shift_id=trainer.id,
            allowed_store_ids=(ids['north'],),now=NOW)
        assert row.trainer_employee_id==manual.id and not row.trainer_override_reason
        assert not manual.scheduling_lead_capable


def test_replacement_does_not_claim_unavailable_coworker_as_valid_lead(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids)
        day=periods[1].week_start_date
        original=next(r for r in scheduled(db,periods[1]) if r.shift_date==day)
        original.is_lead_of_day=False;db.flush()
        b=next(e for e in db.scalars(select(m.Employee)) if e.full_name=='Scheduled Lead B')
        lead=shift(db,actor,periods[1],b,day,ids['north']);lead.is_lead_of_day=True
        db.add(m.EmployeeSchedulingWindow(employee_id=b.id,day_of_week=0,start_time=time.min,end_time=time.max,
            kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush()
        p=preview(db,actor,ids,enrollment,outgoing)
        assert any(c['code']=='LEAD_COVERAGE' for c in p.payload['preview']['conflicts'])


def test_replacement_never_duplicates_double_coverage_or_partially_removes_training(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        first=next(r for r in scheduled(db,periods[1]) if r.employee_id==outgoing.id)
        first.is_double_coverage=True
        other=shift(db,actor,periods[1],trainee,periods[1].week_start_date+timedelta(days=4),ids['north'])
        other.is_double_coverage=True;db.flush()
        before=values([r for per in periods for r in scheduled(db,per)])
        with pytest.raises(SchedulingValidationError,match='duplicate'):
            preview(db,actor,ids,enrollment,outgoing)
        assert before==values([r for per in periods for r in scheduled(db,per)])


def test_automatic_training_considers_alternate_leads_within_hours(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids)
        short=person(db,actor,'Short scheduled Lead',lead=True,target=3)
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==trainee.id))
        profile.maximum_weekly_hours=Decimal('1.5')
        for weekday in [0,1,2]:
            opening_hours(db,actor,ids['south'],weekday,opening=time(9,15),closing=time(9,30))
            db.add(m.CoverageRequirement(store_id=ids['south'],day_of_week=weekday,start_time=time(9,15),end_time=time(9,30),
                minimum_employee_count=1,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
            row=shift(db,actor,periods[0],short,START+timedelta(days=weekday),ids['south'])
            row.end_time=time(9,30)
        db.flush()
        result=t.schedule_training(db,principal=actor,period=periods[0],allowed_store_ids=(ids['north'],ids['south']),now=NOW)
        rows=[db.get(m.ScheduleShift,k) for k in result['training_shift_ids']]
        assert len(rows)==3 and all(r.trainer_employee_id==short.id for r in rows)
        assert sum(scheduled_paid_minutes(r) for r in rows)==90
        assert result['unplaced']==[]


def test_board_never_labels_supplemental_training_as_double_or_lead_coverage(scheduling_db):
    from app.services.v2_scheduling_board_service import serialize_week_board
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        requirement=db.scalar(select(m.CoverageRequirement).where(m.CoverageRequirement.store_id==ids['north'],m.CoverageRequirement.day_of_week==0))
        requirement.minimum_employee_count=2;db.flush()
        rebuild_schedule_warnings(db,schedule_period_id=periods[0].id)
        board=serialize_week_board(db,week_start=START,selected_store_ids=(ids['north'],),all_authorized_store_ids=(ids['north'],),
            permission_flags={'scheduling.view_all':True,'scheduling.manage_preferences':True,'scheduling.edit_draft_shifts':True},principal=actor)
        cards=[r for r in board['shifts'] if r['is_training']]
        assert len(cards)==3 and all(r['trainer_name'] for r in cards)
        assert all(not r['is_double_coverage'] and not r['is_lead_of_day'] and not r['can_change_lead_designation'] for r in cards)
        assert board['summary']['assigned_hours']==10
        assert any(w['type']=='DOUBLE_COVERAGE_UNFILLED' for w in board['warnings'])
        assert board['actions']['manage_training']


def test_cleared_training_evidence_cannot_be_removed(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        row=next(r for r in scheduled(db,periods[0]) if r.is_training)
        periods[0].cleared_at=NOW;db.flush()
        with pytest.raises(SchedulingValidationError):
            t.remove_training_shift(db,principal=actor,shift_id=row.id,allowed_store_ids=(ids['north'],),confirmed=True,now=NOW)
        assert db.get(m.ScheduleShift,row.id) is not None


def test_training_dates_are_captured_without_rewriting_approval_evidence(scheduling_db):
    from copy import deepcopy
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_snapshot_service import canonical_checksum
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,trainee,_,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,periods)
        approve_schedule(db,principal=actor,schedule_period_id=periods[0].id,expected_version=periods[0].version,
            allowed_store_ids=(ids['north'],))
        snap=db.scalar(select(m.ScheduleGeneratorSnapshot).where(m.ScheduleGeneratorSnapshot.schedule_period_id==periods[0].id,
            m.ScheduleGeneratorSnapshot.generation_kind=='APPROVAL'))
        before=deepcopy(snap.payload)
        assert before['metadata']['employee_training'][0]['duration_days']==14
        assert len([r for r in before['result']['shifts'] if r.get('is_training')])==3
        t.adjust_duration(db,principal=actor,training_id=enrollment.id,duration_days=21,now=NOW)
        assert snap.payload==before and canonical_checksum(snap.payload)==snap.checksum
