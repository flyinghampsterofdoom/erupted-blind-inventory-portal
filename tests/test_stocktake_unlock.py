"""Real authentication/HTTP, PostgreSQL evidence and employee continuation."""
from decimal import Decimal
from unittest.mock import Mock
import pytest
from sqlalchemy import select, func
from app import models as m
from app.auth import Principal, Role
from app.config import settings
from app.security.sessions import create_web_session
from app.services.blind_count_service import submit_round, save_draft
from app.services.session_service import unlock_session, count_round_identity
from test_v2_primary import primary, post, login
from test_v2_scheduling_foundation import scheduling_db


def employee_session(primary):
    with primary.Session() as db:
        user=m.Principal(username='shared-store',role=m.PrincipalRole.STORE,store_id=primary.ids['north'],active=True,password_hash='unused')
        db.add(user);db.flush()
        count=m.CountSession(store_id=user.store_id,campaign_id=db.scalar(select(m.Campaign.id)),employee_name='Employee',created_by_principal_id=user.id)
        db.add(count);db.flush()
        db.add(m.SnapshotLine(session_id=count.id,variation_id='V1',item_name='Product',variation_name='Test'))
        token=create_web_session(db,user.id,None,None);db.commit()
        return count.id,user.id,token


def use(primary,token):
    primary.client.cookies.clear();primary.client.cookies.set(settings.session_cookie_name,token);primary.client.get('/login')


@pytest.mark.parametrize('role,linked', [('LEAD',True),('LEAD',False),('ADMIN',False),('MANAGER',False)])
def test_complete_unlock_workflow(primary,monkeypatch,role,linked):
    from app.routers import store
    supervisor=primary.account(role,linked=linked)
    sid,employee,token=employee_session(primary)
    provider=Mock();provider.fetch_current_on_hand.return_value={'V1':Decimal('10')}
    monkeypatch.setattr(store,'snapshot_provider',provider)
    use(primary,token);path=f'/store/sessions/{sid}'
    headers={'X-CSRF-Token':primary.client.cookies.get('csrf_token'),'X-Working-Context':'legacy'}
    saved=primary.client.post(path+'/draft',headers=headers,json={'revision':0,'operation_id':'first','changes':{'V1':{'front_qty':'2','back_qty':'3'}}})
    assert saved.status_code==200,saved.text
    assert primary.client.post(path+'/submit',headers=headers,json={'revision':saved.json()['revision']}).status_code==200
    receipt=primary.client.get(path)
    assert receipt.status_code==200 and 'Work successfully submitted' in receipt.text
    assert 'data-field=' not in receipt.text  # Receipt grants no editing or historical quantity access.
    assert post(primary.client,f'/management/sessions/{sid}/unlock').status_code==403
    with primary.Session() as db:
        def values(model):return [tuple(getattr(e,c.name) for c in model.__table__.columns) for e in db.scalars(select(model).where(model.session_id==sid))]
        entries=values(m.Entry);observations=values(m.CountObservation);audits=set(db.scalars(select(m.AuditLog.id)))
    assert login(primary).status_code==303
    assert primary.client.get('/management/sessions').status_code==200
    page=primary.client.get(f'/management/sessions/{sid}')
    assert page.status_code==200 and 'Unlock Session' in page.text
    assert primary.client.post(f'/management/sessions/{sid}/unlock',data={'csrf_token':'wrong'}).status_code==403
    response=post(primary.client,f'/management/sessions/{sid}/unlock');assert response.status_code==303,response.text
    resumed=int(response.headers['location'].rsplit('/',1)[1]);assert resumed!=sid
    assert post(primary.client,f'/management/sessions/{sid}/unlock').headers['location']==response.headers['location']
    if role=='LEAD':
        assert primary.client.get('/management/users').status_code==403
        assert post(primary.client,f'/management/sessions/{sid}/force-recount').status_code==403
    with primary.Session() as db:
        assert db.get(m.CountSession,sid).observation_closed and db.get(m.CountSession,sid).status==m.SessionStatus.SUBMITTED
        assert db.get(m.CountSession,resumed).status==m.SessionStatus.DRAFT and count_round_identity(db,resumed)==sid
        assert values(m.Entry)==entries and values(m.CountObservation)==observations
        assert audits<=set(db.scalars(select(m.AuditLog.id)))
        e=db.get(m.Entry,(resumed,'V1'));assert (e.front_qty,e.back_qty,e.counted_qty)==(2,3,5)
        event=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='COUNT_SESSION_UNLOCKED').order_by(m.AuditLog.id))
        assert event.actor_principal_id==supervisor and event.meta['resumed_session_id']==resumed
        assert db.scalar(select(func.count()).select_from(m.CountCorrection))==0
    use(primary,token);path=f'/store/sessions/{resumed}'
    assert primary.client.get(path).status_code==200
    headers={'X-CSRF-Token':primary.client.cookies.get('csrf_token'),'X-Working-Context':'legacy'}
    saved=primary.client.post(path+'/draft',headers=headers,json={'revision':0,'operation_id':'resume','changes':{'V1':{'front_qty':'4'}}})
    assert saved.status_code==200,saved.text
    assert primary.client.post(path+'/submit',headers=headers,json={'revision':saved.json()['revision']}).status_code==200
    with primary.Session() as db:
        assert db.get(m.Entry,(sid,'V1')).counted_qty==5 and db.get(m.Entry,(resumed,'V1')).counted_qty==7
        assert db.scalar(select(func.count()).select_from(m.CountCorrection))==0


@pytest.mark.parametrize('role,active,denied,wrong_store', [('STORE',True,False,False),('LEAD',False,False,False),('LEAD',True,True,False),('LEAD',True,False,True)])
def test_service_denial_independent_of_http(primary,role,active,denied,wrong_store):
    pid=primary.account(role);sid,_,_=employee_session(primary)
    with primary.Session() as db:
        if denied:db.add(m.PrincipalPermissionOverride(principal_id=pid,permission_key='management.access',allowed=False))
        db.flush();before=db.scalar(select(func.count()).select_from(m.CountSession))
        p=Principal(pid,'person',Role(role),primary.ids['south'] if wrong_store else None,active)
        with pytest.raises(PermissionError):unlock_session(db,principal=p,session_id=sid)
        assert db.scalar(select(func.count()).select_from(m.CountSession))==before


def test_legacy_unlock_preserves_same_session_and_revision(primary):
    pid=primary.account('LEAD',linked=False);sid,_,_=employee_session(primary)
    with primary.Session() as db:
        count=db.get(m.CountSession,sid);count.status=m.SessionStatus.SUBMITTED
        db.add(m.Entry(session_id=sid,variation_id='V1',front_qty=2,back_qty=3,counted_qty=5,updated_by_principal_id=pid));db.commit()
        p=Principal(pid,'person',Role.LEAD,primary.ids['north'],True)
        assert unlock_session(db,principal=p,session_id=sid).id==sid
        assert count.status==m.SessionStatus.DRAFT and count.draft_revision==1
        assert db.get(m.Entry,(sid,'V1')).counted_qty==5
        assert unlock_session(db,principal=p,session_id=sid).draft_revision==1


def test_continuations_do_not_qualify_as_independent_counts(primary):
    pid=primary.account('LEAD',linked=False);sid,employee,_=employee_session(primary)
    lead=Principal(pid,'lead',Role.LEAD,None,True);staff=Principal(employee,'staff',Role.STORE,primary.ids['north'],True)
    provider=Mock();provider.fetch_current_on_hand.return_value={'V1':Decimal('10')}
    with primary.Session() as db:
        save_draft(db,principal=staff,session_id=sid,revision=0,operation_id='first',changes={'V1':{'front_qty':'2','back_qty':'3'}})
        for i in range(4):
            count=db.get(m.CountSession,sid)
            _,ids=submit_round(db,principal=staff,session_id=sid,revision=count.draft_revision,provider=provider)
            assert ids==[];db.commit()
            resumed=unlock_session(db,principal=lead,session_id=sid);db.commit();sid=resumed.id
        assert db.scalar(select(func.count()).select_from(m.CountCorrection))==0
        assert db.scalar(select(func.count()).select_from(m.CountObservation))==4


def test_parallel_unlock_and_open_draft_conflict(primary):
    from concurrent.futures import ThreadPoolExecutor
    pid=primary.account('LEAD',linked=False);sid,employee,_=employee_session(primary)
    lead=Principal(pid,'lead',Role.LEAD,None,True)
    with primary.Session() as db:
        count=db.get(m.CountSession,sid);count.status=m.SessionStatus.SUBMITTED;count.observation_closed=True
        other=m.CountSession(store_id=count.store_id,campaign_id=count.campaign_id,employee_name='Other',created_by_principal_id=employee)
        db.add(other);db.commit();other_id=other.id
        with pytest.raises(ValueError,match='already has an open'):unlock_session(db,principal=lead,session_id=sid)
        db.rollback();db.get(m.CountSession,other_id).status=m.SessionStatus.SUBMITTED;db.commit()
    def unlock(_):
        with primary.Session() as db:
            result=unlock_session(db,principal=lead,session_id=sid);new_id=result.id;db.commit();return new_id
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(unlock,range(2)))
    assert results[0]==results[1] and results[0]!=sid
    with primary.Session() as db:
        assert db.scalar(select(func.count()).select_from(m.CountSession).where(m.CountSession.status==m.SessionStatus.DRAFT))==1
