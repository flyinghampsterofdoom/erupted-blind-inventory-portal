"""Real PostgreSQL transactions exercise evidence, retries and legacy migration."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

import pytest
from alembic import command
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

from app.auth import Principal, Role
from app.models import (Campaign, CountSession, Entry, SnapshotLine, Store, Principal as User,
    PrincipalRole, CountObservation, CountCorrection, CountReview, CountCorrectionAttempt,
    AuditLog, StoreRecountItem, WebSession)
from app.schema_contract import upgrade_database, _alembic_config, HEAD_REVISION, current_revision
from app.services.blind_count_service import (save_draft, submit_round, execute_correction,
    review_correction, employee_rows, DraftConflict, review_queue, correction_detail)
from app.services.session_service import unlock_session, purge_count_sessions
from app.security.sessions import load_session_from_token

ADMIN = os.getenv('TEST_POSTGRES_ADMIN_URL')
pytestmark = pytest.mark.skipif(not ADMIN, reason='requires isolated PostgreSQL admin URL')

@pytest.fixture(scope='module')
def engine():
    admin = create_engine(ADMIN, isolation_level='AUTOCOMMIT')
    name = 'blind_counts_' + uuid4().hex[:12]
    url = ADMIN.rsplit('/',1)[0] + '/' + name
    with admin.connect() as c: c.execute(text(f'CREATE DATABASE "{name}"'))
    eng = create_engine(url)
    upgrade_database(url)
    try: yield eng
    finally:
        eng.dispose()
        with admin.connect() as c: c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()

@pytest.fixture
def ctx(engine):
    db=Session(engine)
    store=Store(name='Test '+uuid4().hex,square_location_id=uuid4().hex); db.add(store);db.flush()
    user=User(username=uuid4().hex,password_hash='unused',role=PrincipalRole.STORE,store_id=store.id)
    lead=User(username=uuid4().hex,password_hash='unused',role=PrincipalRole.LEAD)
    campaign=Campaign(label='Test');db.add_all([user,lead,campaign]);db.flush()
    principal=Principal(user.id,user.username,Role.STORE,store.id,True)
    manager=Principal(lead.id,lead.username,Role.LEAD,None,True)
    db.commit()
    yield SimpleNamespace(db=db,store=store,principal=principal,lead=manager,campaign=campaign)
    db.close()

def round_(c, front='2',back='3',expected=10):
    count=CountSession(store_id=c.store.id,campaign_id=c.campaign.id,employee_name='Shared account entered name',created_by_principal_id=c.principal.id)
    c.db.add(count);c.db.flush()
    c.db.add(SnapshotLine(session_id=count.id,variation_id='V1',item_name='Product',variation_name='6mg',expected_on_hand=None));c.db.flush()
    result=save_draft(c.db,principal=c.principal,session_id=count.id,revision=0,
        changes={'V1':{'front_qty':front,'back_qty':back}},operation_id=uuid4().hex)
    c.db.commit()
    provider=Mock();provider.fetch_current_on_hand.return_value={} if expected is None else {'V1':Decimal(str(expected))}
    _,ids=submit_round(c.db,principal=c.principal,session_id=count.id,revision=result['revision'],provider=provider)
    c.db.commit()
    return count,ids,provider

def test_three_rounds_retry_and_independent_review(ctx):
    c=ctx
    first,ids,p=round_(c);assert ids==[]
    _,ids=submit_round(c.db,principal=c.principal,session_id=first.id,revision=0,provider=p)
    assert ids==[] and p.fetch_current_on_hand.call_count==1
    with pytest.raises(ValueError): unlock_session(c.db,principal=c.lead,session_id=first.id)
    second,ids,_=round_(c);assert ids==[]
    third,ids,_=round_(c);assert len(ids)==1
    correction=c.db.get(CountCorrection,ids[0]); payload=correction.request_payload.copy()
    # A separate connection sees preparation before any external request.
    with Session(c.db.bind) as other: assert other.get(CountCorrection,ids[0]).status=='PENDING'
    client=Mock();client.post.side_effect=RuntimeError('Response lost')
    execute_correction(c.db,correction_id=ids[0],actor_id=c.principal.id,client=client)
    assert correction.status=='FAILED' and c.db.scalar(select(CountReview).where(CountReview.correction_id==ids[0])) is None
    client.post.side_effect=None;client.post.return_value={'counts':[{'catalog_object_id':'V1','quantity':'5'}]}
    execute_correction(c.db,correction_id=ids[0],actor_id=c.lead.id,client=client)
    assert client.post.call_args.args[1]==payload
    assert correction.status=='SUCCESS' and correction.attempts==2
    review=c.db.scalar(select(CountReview).where(CountReview.correction_id==ids[0]));assert review.status=='OPEN'
    assert c.db.get(StoreRecountItem,(c.store.id,'V1')) is None
    execute_correction(c.db,correction_id=ids[0],actor_id=c.lead.id,client=client)
    assert client.post.call_count==2
    assert any(r['correction'].id==ids[0] and r['review'].status=='OPEN' for r in review_queue(c.db))
    evidence=correction_detail(c.db,ids[0]);assert len(evidence['observations'])==3
    assert {o.session_id for o in evidence['observations']}=={first.id,second.id,third.id}
    assert all(o.total_qty==5 and o.variance==-5 and o.counted_by_principal_id==c.principal.id for o in evidence['observations'])
    review_correction(c.db,correction_id=ids[0],principal=c.lead,explanation='Unable to determine',notes='Inspected the source evidence.',related_ids=[]);c.db.commit()
    assert review.status=='REVIEWED' and correction.status=='SUCCESS'
    assert review.reviewed_by_principal_id==c.lead.id
    with pytest.raises(ValueError): purge_count_sessions(c.db,session_ids=[first.id])
    with pytest.raises(DBAPIError):
        with c.db.begin_nested(): c.db.execute(text('UPDATE count_observations SET total_qty=total_qty WHERE session_id=:id'),{'id':first.id})
    with pytest.raises(DBAPIError):
        with c.db.begin_nested(): c.db.execute(text('DELETE FROM count_observations WHERE session_id=:id'),{'id':first.id})

@pytest.mark.parametrize('gap',[None,5,11])
def test_unknown_zero_or_changed_variance_breaks_streak(ctx,gap):
    c=ctx
    round_(c);round_(c)
    count,ids,p=round_(c,expected=gap);assert ids==[]
    o=c.db.scalar(select(CountObservation).where(CountObservation.session_id==count.id))
    if gap is None:
        assert o.expected_qty is None and o.variance is None
        assert o.expected_provenance['state']=='UNKNOWN'
        assert c.db.get(StoreRecountItem,(c.store.id,'V1')).last_variance is None
        p.fetch_current_on_hand.return_value={'V1':0}
        submit_round(c.db,principal=c.principal,session_id=count.id,revision=0,provider=p)
        assert p.fetch_current_on_hand.call_count==1 and o.expected_qty is None
    assert round_(c)[1]==[]
    assert round_(c)[1]==[]
    assert len(round_(c)[1])==1

def test_draft_clear_partial_errors_replay_revision_scope_and_projection(ctx):
    c=ctx
    count=CountSession(store_id=c.store.id,campaign_id=c.campaign.id,employee_name='Name',created_by_principal_id=c.principal.id)
    c.db.add(count);c.db.flush();c.db.add(SnapshotLine(session_id=count.id,variation_id='V1',item_name='P',variation_name='V',expected_on_hand=999,previous_recount_variance=-12));c.db.flush()
    args=dict(principal=c.principal,session_id=count.id,revision=0,changes={'V1':{'front_qty':'0','back_qty':''}},operation_id='op1')
    first=save_draft(c.db,**args);c.db.commit()
    assert save_draft(c.db,**args)==first
    assert c.db.get(Entry,(count.id,'V1')).counted_qty is None
    with pytest.raises(ValueError): submit_round(c.db,principal=c.principal,session_id=count.id,revision=1,provider=Mock())
    second=save_draft(c.db,principal=c.principal,session_id=count.id,revision=1,changes={'V1':{'front_qty':'','back_qty':'2'}},operation_id='op2');c.db.commit()
    assert second['revision']==2
    with pytest.raises(DraftConflict): save_draft(c.db,**args)
    invalid=save_draft(c.db,principal=c.principal,session_id=count.id,revision=2,changes={'V1':{'front_qty':'3','back_qty':'-1'}},operation_id='op3');c.db.commit()
    assert invalid['errors'] and c.db.get(Entry,(count.id,'V1')).counted_qty==5
    projection=employee_rows(c.db,count.id)[0]
    assert set(projection)=={'variation_id','item_name','variation_name','section_type','front_qty','back_qty'}
    assert '999' not in str(projection) and '-12' not in str(projection)
    other=Principal(c.principal.id,'Other',Role.STORE,c.store.id+1000,True)
    with pytest.raises(HTTPException): save_draft(c.db,principal=other,session_id=count.id,revision=3,changes={},operation_id='forbidden')
    changed=c.db.scalars(select(AuditLog).where(AuditLog.session_id==count.id,AuditLog.action=='BLIND_COUNT_DRAFT_CHANGED')).all()
    assert len(changed)==3 and all(a.actor_principal_id==c.principal.id for a in changed)

def test_nonrenewing_probe_and_expiration(ctx):
    c=ctx
    expires=datetime.now(timezone.utc)+timedelta(minutes=5)
    row=WebSession(session_token=uuid4().hex,principal_id=c.principal.id,expires_at=expires)
    c.db.add(row);c.db.commit();seen=row.last_seen_at
    assert load_session_from_token(c.db,row.session_token,renew=False)
    assert row.expires_at==expires and row.last_seen_at==seen
    assert load_session_from_token(c.db,row.session_token)
    assert row.expires_at>expires
    row.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);c.db.commit()
    assert load_session_from_token(c.db,row.session_token,renew=False) is None


def test_migration_preserves_legacy_totals_and_rejects_lossy_downgrade():
    admin=create_engine(ADMIN,isolation_level='AUTOCOMMIT');name='blind_legacy_'+uuid4().hex[:10];url=ADMIN.rsplit('/',1)[0]+'/'+name
    with admin.connect() as db: db.execute(text(f'CREATE DATABASE "{name}"'))
    eng=create_engine(url)
    try:
        upgrade_database(url,'20260915_0025')
        with eng.begin() as db:
            db.execute(text("INSERT INTO stores(id,name) VALUES(1,'Legacy')"))
            db.execute(text("INSERT INTO principals(id,username,password_hash,role,store_id) VALUES(1,'legacy','unused','STORE',1)"))
            db.execute(text("INSERT INTO campaigns(id,label) VALUES(1,'Legacy')"))
            db.execute(text("INSERT INTO count_sessions(id,store_id,campaign_id,employee_name,created_by_principal_id,status) VALUES(1,1,1,'shared',1,'SUBMITTED')"))
            db.execute(text("INSERT INTO snapshot_lines(session_id,variation_id,item_name,variation_name,expected_on_hand) VALUES(1,'V','P','V',8)"))
            db.execute(text("INSERT INTO entries(session_id,variation_id,counted_qty,updated_by_principal_id) VALUES(1,'V',7,1)"))
        upgrade_database(url)
        with eng.connect() as db:
            assert db.execute(text('SELECT counted_qty,front_qty,back_qty FROM entries')).one()==(7,None,None)
            assert db.execute(text('SELECT observation_closed FROM count_sessions')).scalar_one() is True
            assert db.execute(text('SELECT count(*) FROM count_observations')).scalar_one()==0
        # Legacy-only rollback is lossless; re-upgrade remains supported.
        command.downgrade(_alembic_config(url),'20260915_0025')
        with eng.begin() as db:
            db.execute(text("INSERT INTO store_recount_items(store_id,variation_id,item_name,variation_name,last_variance,consecutive_match_count,total_count_attempts) VALUES(1,'V','P','V',-1,2,2)"))
        upgrade_database(url)
        with eng.connect() as db:
            assert db.execute(text('SELECT consecutive_match_count,total_count_attempts FROM store_recount_items')).one()==(0,0)
        with eng.begin() as db: db.execute(text('UPDATE entries SET front_qty=3,back_qty=4'))
        with pytest.raises(DBAPIError): command.downgrade(_alembic_config(url),'20260915_0025')
        assert current_revision(eng)==HEAD_REVISION
    finally:
        eng.dispose()
        with admin.connect() as db: db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()

def test_employee_http_allowlist_scope_csrf_and_real_session_expiration(ctx,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.main import app as configured_app
    from app.routers import store as store_routes, management, auth
    from app.security.csrf import install_csrf_cookie_middleware
    from app.security.sessions import install_auth_session_middleware
    from app.db import get_db
    from app.config import settings
    from app.services import blind_count_service
    c=ctx
    count=CountSession(store_id=c.store.id,campaign_id=c.campaign.id,employee_name='Name',created_by_principal_id=c.principal.id)
    c.db.add(count);c.db.flush();c.db.add(SnapshotLine(session_id=count.id,variation_id='V1',item_name='Visible Product',variation_name='6mg',expected_on_hand=928347,previous_recount_variance=-123456));c.db.commit()
    expires=datetime.now(timezone.utc)+timedelta(minutes=5)
    token=uuid4().hex;c.db.add(WebSession(session_token=token,principal_id=c.principal.id,expires_at=expires));c.db.commit()
    monkeypatch.setattr('app.security.sessions.SessionLocal',lambda:Session(c.db.bind))
    app=FastAPI();app.state.templates=configured_app.state.templates
    app.include_router(store_routes.router);app.include_router(management.router);app.include_router(auth.router)
    def database():
        with Session(c.db.bind) as db: yield db
    app.dependency_overrides[get_db]=database
    install_auth_session_middleware(app);install_csrf_cookie_middleware(app)
    with TestClient(app) as client:
        client.cookies.set(settings.session_cookie_name,token)
        status=client.get('/session-status');assert status.status_code==200
        assert settings.session_cookie_name not in status.headers.get('set-cookie','')
        with Session(c.db.bind) as db: assert db.scalar(select(WebSession).where(WebSession.session_token==token)).expires_at==expires
        page=client.get(f'/store/sessions/{count.id}');assert page.status_code==200
        assert 'Visible Product' in page.text and 'Front Stock' in page.text
        assert '928347' not in page.text and '-123456' not in page.text
        assert page.headers['cache-control']=='no-store'
        assert client.get('/management/audit-queue').status_code==403
        assert client.get('/management/ordering-tool/orders/1/products?q=test').status_code==403
        payload={'revision':0,'operation_id':'http-save','changes':{'V1':{'front_qty':'3','back_qty':'2'}}}
        path=f'/store/sessions/{count.id}/draft'
        assert client.post(path,json=payload).status_code==403
        csrf={'X-CSRF-Token':client.cookies.get('csrf_token'),'X-Requested-With':'autosave'}
        saved=client.post(path,json=payload,headers=csrf);assert saved.status_code==200
        assert set(saved.json())=={'revision','errors','operation_id'}
        # Access is checked again after a store reassignment, not trusted from page load.
        with Session(c.db.bind) as db:
            other=Store(name='Other');db.add(other);db.flush();db.get(User,c.principal.id).store_id=other.id;db.commit()
        assert client.post(path,json=payload,headers=csrf).status_code==403
        with Session(c.db.bind) as db:
            db.get(User,c.principal.id).store_id=c.store.id
            web=db.scalar(select(WebSession).where(WebSession.session_token==token));web.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
        assert client.get('/session-status').status_code==401
        assert client.post(path,json=payload,headers=csrf).status_code==401
        # Same account can resume after authenticating again; old session stays expired.
        new_token=uuid4().hex
        with Session(c.db.bind) as db:
            db.add(WebSession(session_token=new_token,principal_id=c.principal.id,expires_at=expires));db.commit()
        client.cookies.clear()
        client.cookies.set('csrf_token',csrf['X-CSRF-Token'])
        client.cookies.set(settings.session_cookie_name,new_token)
        assert client.post(path,json=payload,headers=csrf).json()['revision']==1
        provider=Mock();provider.fetch_current_on_hand.return_value={'V1':10}
        monkeypatch.setattr(store_routes,'snapshot_provider',provider)
        monkeypatch.setattr(blind_count_service,'execute_correction',Mock())
        result=client.post(f'/store/sessions/{count.id}/submit',json={'revision':1},headers=csrf)
        assert result.json()=={'submitted':True,'redirect':'/store/daily-count'}
        assert client.post(f'/store/sessions/{count.id}/submit',json={'revision':1},headers=csrf).json()==result.json()
        assert provider.fetch_current_on_hand.call_count==1

def test_legacy_draft_total_survives_untouched_save_and_changes_are_audited(ctx):
    c=ctx
    count=CountSession(store_id=c.store.id,campaign_id=c.campaign.id,employee_name='Shared',created_by_principal_id=c.principal.id)
    c.db.add(count);c.db.flush()
    c.db.add(SnapshotLine(session_id=count.id,variation_id='V1',item_name='P',variation_name='V',expected_on_hand=None))
    c.db.add(Entry(session_id=count.id,variation_id='V1',counted_qty=7,updated_by_principal_id=c.principal.id));c.db.commit()
    save_draft(c.db,principal=c.principal,session_id=count.id,revision=0,changes={'V1':{'front_qty':'','back_qty':''}},operation_id='untouched');c.db.commit()
    assert c.db.get(Entry,(count.id,'V1')).counted_qty==7
    result=save_draft(c.db,principal=c.principal,session_id=count.id,revision=0,changes={'V1':{'front_qty':'2','back_qty':'3'}},operation_id='entered');c.db.commit()
    entry=c.db.get(Entry,(count.id,'V1'));assert entry.counted_qty==5
    actor_time=entry.updated_at
    same=save_draft(c.db,principal=c.principal,session_id=count.id,revision=result['revision'],changes={'V1':{'front_qty':'2','back_qty':'3'}},operation_id='same');c.db.commit()
    assert same['revision']==result['revision'] and entry.updated_at==actor_time
    audit=c.db.scalar(select(AuditLog).where(AuditLog.session_id==count.id,AuditLog.action=='BLIND_COUNT_DRAFT_CHANGED'))
    assert Decimal(audit.meta['changes']['V1']['before']['counted_qty'])==7


def test_square_omitted_quantity_remains_unknown_and_confirmed_zero_is_retained():
    from app.services.square_snapshot_provider import SquareSnapshotProvider
    provider=object.__new__(SquareSnapshotProvider)
    provider._get_store=Mock(return_value=SimpleNamespace(square_location_id='L'))
    provider._post=Mock(return_value={'counts':[{'catalog_object_id':'zero','quantity':'0'}, {'catalog_object_id':'missing'}, {'catalog_object_id':'bad','quantity':'bad'}]})
    assert provider.fetch_current_on_hand(store_id=1,variation_ids=['zero','missing','bad','omitted'])=={'zero':Decimal('0')}

def test_concurrent_submit_and_correction_replay_use_one_observation_and_write(ctx):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    c=ctx;round_(c);round_(c)
    count=CountSession(store_id=c.store.id,campaign_id=c.campaign.id,employee_name='Name',created_by_principal_id=c.principal.id)
    c.db.add(count);c.db.flush()
    c.db.add(SnapshotLine(session_id=count.id,variation_id='V1',item_name='P',variation_name='6mg',expected_on_hand=None));c.db.flush()
    result=save_draft(c.db,principal=c.principal,session_id=count.id,revision=0,changes={'V1':{'front_qty':'2','back_qty':'3'}},operation_id='parallel');c.db.commit()
    count_id=count.id;gate=Barrier(2);provider=Mock();provider.fetch_current_on_hand.return_value={'V1':10};client=Mock();client.post.return_value={}
    def submit():
        with Session(c.db.bind) as db:
            gate.wait(timeout=10)
            _,ids=submit_round(db,principal=c.principal,session_id=count_id,revision=result['revision'],provider=provider);db.commit()
            for cid in ids: execute_correction(db,correction_id=cid,actor_id=c.principal.id,client=client)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures=[workers.submit(submit) for _ in range(2)]
        for f in futures: f.result(timeout=15)
    assert provider.fetch_current_on_hand.call_count==1 and client.post.call_count==1
    assert len(c.db.scalars(select(CountObservation).where(CountObservation.store_id==c.store.id)).all())==3
    corrections=c.db.scalars(select(CountCorrection).where(CountCorrection.store_id==c.store.id)).all()
    assert len(corrections)==1 and corrections[0].status=='SUCCESS'
