"""First-class Owner coverage through authenticated routes."""
from sqlalchemy import select
import pytest
from app import models as m
from test_callout_coverage_http import coverage_case,save
from test_v2_primary import primary,login
from test_v2_scheduling_foundation import scheduling_db

@pytest.mark.parametrize('role',['ADMIN','MANAGER','LEAD'])
def test_owner_option_save_projection_and_cancellation(primary,role):
    actor=primary.account(role)
    with primary.Session() as db:
        owner=m.Principal(username='coverage-owner@example.test',password_hash='unused',
            role=m.PrincipalRole.ADMIN,active=True)
        db.add(owner);db.commit();oid=owner.id
    sid,day,week=coverage_case(primary)
    login(primary)
    page=primary.client.get('/v2/scheduling/week',params={'start':str(week)})
    assert page.status_code==200,page.text
    assert f'value="owner:{oid}"' in page.text
    candidates=primary.client.get(f'/v2/scheduling/api/shifts/{sid}/coverage-candidates')
    assert candidates.status_code==200,candidates.text
    assert any(row['id']==oid for row in candidates.json()['owners'])
    result=save(primary,sid,commitment_owner_principal_id=oid)
    assert result.status_code==201,result.text
    data=result.json();card=next(row for row in data['board']['shifts'] if row.get('is_coverage'))
    assert card['employee_id'] is None and card['owner_principal_id']==oid
    assert 'coverage-owner@example.test' in card['owner_name']
    assert not card['is_shift_lead'] and not card['is_lead_of_day']
    with primary.Session() as db:
        shift=db.get(m.ScheduleShift,sid)
        commitment=db.scalar(select(m.ScheduleCoverageCommitment))
        event=db.scalar(select(m.ScheduleAttendanceEvent))
        assert shift.employee_id==primary.ids['alex'] and shift.owner_principal_id is None
        assert commitment.employee_id is None and commitment.owner_principal_id==oid
        assert commitment.recorded_by_principal_id==actor and event.original_employee_id==primary.ids['alex']
    company=primary.client.get('/v2/scheduling/my-schedule',params={'start':str(week)})
    assert company.status_code==200 and 'coverage-owner@example.test' in company.text
    canceled=save(primary,sid,cancel_coverage=True)
    assert canceled.status_code==201,canceled.text
    assert not any(row.get('is_coverage') for row in canceled.json()['board']['shifts'])
    with primary.Session() as db:
        assert db.scalar(select(m.ScheduleCoverageCommitment)).voided_at is not None
        assert db.scalar(select(m.ScheduleAttendanceEvent).where(m.ScheduleAttendanceEvent.voided_at.is_(None))) is not None


def test_ambiguous_owner_and_employee_rejected_atomically(primary):
    primary.account('ADMIN');sid,_,_=coverage_case(primary);login(primary)
    with primary.Session() as db:
        oid=db.scalar(select(m.Principal.id).where(m.Principal.role==m.PrincipalRole.ADMIN))
    response=save(primary,sid,commitment_owner_principal_id=oid,
        commitment_employee_id=primary.ids['blair'])
    assert response.status_code==422,response.text
    with primary.Session() as db:
        assert db.scalar(select(m.ScheduleCoverageCommitment)) is None
        assert db.scalar(select(m.ScheduleAttendanceEvent)) is None
