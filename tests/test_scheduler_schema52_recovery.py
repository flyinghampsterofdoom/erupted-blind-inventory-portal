"""Recovery preserves schema-52 authority while serving the prior scheduler UI."""
from datetime import date,datetime,time,timedelta
import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal,Role
from app.services.v2_scheduling_service import update_shift,clone_published_revision
from app.services.scheduling_shift_lead_service import has_shift_lead_permission
from app.services.v2_scheduling_exception_service import record_commitment,effective_shift_segments,cancel_commitment
from app.services.v2_scheduling_policy_service import organization_policy
from test_v2_scheduling_foundation import scheduling_db
from test_scheduling_lifecycle_integrity import complete,args,values
from test_v2_lead_duty import period,shift
from zoneinfo import ZoneInfo


def owner(db):
    row=m.Principal(username='recovery-owner',role=m.PrincipalRole.ADMIN,active=True)
    db.add(row);db.flush();return Principal(row.id,row.username,Role.ADMIN,None,True)


@pytest.mark.parametrize('explicit', [False,True])
def test_legacy_move_revokes_grant_and_explicit_owner_assignment_is_preserved(scheduling_db,explicit):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        actor=owner(db);per,rows=complete(db,manager,ids);row=rows[0];row.is_shift_lead=True;db.commit()
        update_shift(db,**args(per,actor,ids),shift_id=row.id,values=values(row,store_id=ids['south']),
            manual_edit=True,**({'lead_designation':'ASSIGN'} if explicit else {}));db.commit()
        assert row.is_shift_lead==explicit and row.store_id==ids['south']
        event=db.scalar(select(m.AuditLog).where(m.AuditLog.action.in_(
            ('V2:SCHEDULING:SHIFT_LEAD_ASSIGNED','V2:SCHEDULING:SHIFT_LEAD_REMOVED'))).order_by(m.AuditLog.id.desc()))
        assert event.actor_principal_id==actor.id


def test_recovered_revision_preserves_grants_coverage_scopes_and_expiration(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        actor=owner(db);day=date(2030,5,14);per=period(db,actor,day)
        original=shift(db,actor,per,ids['alex'],ids['north'],day,lead=False)
        original.is_shift_lead=True
        account=m.Principal(username='recovered-relief',role=m.PrincipalRole.STORE,store_id=ids['south'],active=True)
        db.add(account);db.flush();db.get(m.Employee,ids['blair']).principal_id=account.id
        coverage=record_commitment(db,principal=actor,shift_id=original.id,employee_id=ids['blair'],
            start_time=time(12),end_time=time(15),lead_designation='ASSIGN',today=day)
        db.commit()
        draft=clone_published_revision(db,principal=actor,published_period_id=per.id,
            allowed_store_ids=(ids['north'],ids['south']))
        copied=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==draft.id,
            m.ScheduleShift.source_shift_id==original.id))
        assert copied.is_shift_lead and coverage.is_shift_lead
        assert any(s.is_shift_lead and s.employee_id==ids['blair'] for s in effective_shift_segments(db,copied))
        temp=Principal(account.id,account.username,Role.STORE,ids['south'],True)
        segment=next(s for s in effective_shift_segments(db,original) if s.employee_id==ids['blair'])
        zone=ZoneInfo(organization_policy(db).timezone_name)
        assert has_shift_lead_permission(db,principal=temp,permission_key='scheduling.attendance.record',
            shift=segment,now=datetime.combine(day,time(13),zone))
        assert not has_shift_lead_permission(db,principal=temp,permission_key='scheduling.publish',
            shift=segment,now=datetime.combine(day,time(13),zone))
        assert not has_shift_lead_permission(db,principal=temp,permission_key='scheduling.attendance.record',
            shift=segment,now=datetime.combine(day,time(15),zone))
        cancel_commitment(db,principal=actor,commitment_id=coverage.id)
        assert not has_shift_lead_permission(db,principal=temp,permission_key='scheduling.attendance.record',
            shift=segment,now=datetime.combine(day,time(13),zone))
        assert db.get(m.Principal,account.id).role==m.PrincipalRole.STORE
