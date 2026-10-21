"""Owner warning override and closure-aware Longview policy, isolated data."""
from datetime import date, timedelta
import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal,Role
from app.services.scheduling_completion_service import approve_and_publish_schedule
from app.services.v2_scheduling_service import SchedulingValidationError,create_draft_period
from app.services import v2_scheduling_coverage_service as coverage
from app.services.v2_scheduling_policy_service import weekly_longview_travelers
from test_scheduling_lifecycle_integrity import scheduling_db,complete,args
from test_v2_longview_weekly import setup_pool,shift


def owner(db,actor):
    db.get(m.Principal,actor.id).role=m.PrincipalRole.ADMIN;db.flush()
    return Principal(actor.id,actor.username,Role.ADMIN,None,True)


def warning_gate(monkeypatch):
    original=coverage.rebuild_schedule_warnings
    def warning(db,*,schedule_period_id,**kw):
        result=original(db,schedule_period_id=schedule_period_id,**kw)
        db.add(m.ScheduleWarning(schedule_period_id=schedule_period_id,store_id=db.scalar(select(m.Store.id).limit(1)),
            severity=m.ScheduleWarningSeverity.SERIOUS,warning_type='OWNER_POLICY_TEST',
            message='Explicit Owner policy exception',warning_date=date(2030,5,14)))
        db.flush();return result
    monkeypatch.setattr(coverage,'rebuild_schedule_warnings',warning)


@pytest.mark.parametrize('kind',['unconfirmed','blank','manager','denied','owner'])
def test_owner_override_is_explicit_authorized_and_audited(scheduling_db,monkeypatch,kind):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        period,rows=complete(db,actor,ids)
        if kind!='manager':actor=owner(db,actor)
        if kind=='denied':db.add(m.PrincipalPermissionOverride(principal_id=actor.id,
            permission_key='scheduling.publish_with_warnings',allowed=False));db.flush()
        db.commit();before=[(s.id,s.employee_id,s.store_id,s.shift_date,s.start_time,s.end_time) for s in rows]
        warning_gate(monkeypatch)
        kw=args(period,actor,ids)
        kw.update(allow_serious_warnings=True,confirmed=kind!='unconfirmed',
            override_reason='' if kind=='blank' else 'Owner accepts this staffing policy exception.')
        if kind!='owner':
            with pytest.raises((SchedulingValidationError,PermissionError)):
                approve_and_publish_schedule(db,**kw)
            db.commit();assert period.status==m.SchedulePeriodStatus.DRAFT
            assert db.scalar(select(m.ScheduleApproval)) is None
        else:
            approve_and_publish_schedule(db,**kw);db.commit()
            assert period.status==m.SchedulePeriodStatus.PUBLISHED
            events=list(db.scalars(select(m.AuditLog).where(m.AuditLog.action.in_([
                'V2:SCHEDULING:SCHEDULE_APPROVED','V2:SCHEDULING:SCHEDULE_PUBLISHED_WITH_WARNINGS']))))
            assert len(events)==2
            for event in events:
                assert event.meta['reason']==kw['override_reason']
                assert event.meta['metadata']['owner_override']
                assert event.meta['metadata']['overridden_warnings'][0]['type']=='OWNER_POLICY_TEST'
            assert before==[(s.id,s.employee_id,s.store_id,s.shift_date,s.start_time,s.end_time) for s in rows]


@pytest.mark.parametrize('closed,active,expected',[(True,True,True),(True,False,False),(False,True,False)])
def test_only_active_approved_closure_satisfies_seventh_day_travel(scheduling_db,closed,active,expected):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        setup_pool(db,actor,ids)
        period=create_draft_period(db,principal=actor,week_start=date(2026,12,20))
        # Fewer than seven assignments alone must not waive the traveler.
        row=shift(db,actor,period,ids,employee=None)
        from datetime import time
        db.add(m.StoreSpecialHour(store_id=ids['south'],calendar_date=date(2026,12,25),
            event_name='Christmas',active=active,closed_all_day=closed,
            opening_time=None if closed else time(11),closing_time=None if closed else time(16),
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result=weekly_longview_travelers(db,period=period,planning_date=date(2026,10,21))[0]
        assert result['satisfied']==expected
        if expected:
            assert result['operating_days']==6 and result['evidence_class']=='NOT_REQUIRED'
            assert 'CLOSURE_SHORTENED_WEEK' in result['reason_codes']
        assert row.employee_id is None


def test_owner_policy_override_also_honors_actual_assignment_exception(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        period,rows=complete(db,actor,ids);actor=owner(db,actor)
        db.add(m.TimeOffRequest(employee_id=ids['alex'],start_date=rows[0].shift_date,
            end_date=rows[0].shift_date,full_day=True,status=m.TimeOffRequestStatus.APPROVED,
            reason_category_id=ids['vacation'],submitted_by_principal_id=actor.id,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.commit()
        with pytest.raises(SchedulingValidationError):approve_and_publish_schedule(db,**args(period,actor,ids))
        approve_and_publish_schedule(db,**args(period,actor,ids),allow_serious_warnings=True,
            confirmed=True,override_reason='Owner confirms the exceptional assignment.');db.commit()
        assert period.status==m.SchedulePeriodStatus.PUBLISHED
        audit=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SCHEDULE_PUBLISHED_WITH_WARNINGS'))
        assert audit is not None and any(w['type']=='ASSIGNMENT_POLICY_OVERRIDE' for w in audit.meta['metadata']['overridden_warnings'])
        assert db.scalar(select(m.TimeOffRequest)).status==m.TimeOffRequestStatus.APPROVED


def test_owner_override_preserves_version_scope_and_approved_content(scheduling_db):
    from app.services.v2_scheduling_service import approve_schedule,publish_schedule,SchedulingConflict
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        period,rows=complete(db,actor,ids);actor=owner(db,actor);db.commit()
        kw=args(period,actor,ids)
        kw.update(allow_serious_warnings=True,confirmed=True,override_reason='Owner schedule exception.')
        with pytest.raises(SchedulingConflict):approve_and_publish_schedule(db,**{**kw,'expected_version':period.version+1})
        with pytest.raises(PermissionError):approve_and_publish_schedule(db,**{**kw,'allowed_store_ids':(ids['south'],)})
        approve_schedule(db,**kw);db.commit()
        rows[0].employee_note='Changed after captured approval';db.commit()
        with pytest.raises(SchedulingValidationError):publish_schedule(db,**kw)
        db.commit();assert period.status==m.SchedulePeriodStatus.DRAFT
