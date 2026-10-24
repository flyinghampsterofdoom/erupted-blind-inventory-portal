"""Authorized recovery keys, receipts and idempotent form retries against disposable PostgreSQL."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
import base64
import pytest
from sqlalchemy import select, func
from app import models as m
from app.config import settings
from app.security.sessions import create_web_session
from app.services.inventory_draft_service import recovery_key
from test_v2_scheduling_foundation import scheduling_db
from test_v2_primary import primary
from test_employee_operational_context import seeded_current

SECRET='test-only-strong-inventory-draft-secret-20261024'

@pytest.fixture
def case(primary,monkeypatch):
    monkeypatch.setattr(settings,'integration_encryption_key',SECRET)
    pid,*_=seeded_current(primary,two=True)
    with primary.Session() as db:
        campaign=db.scalar(select(m.Campaign))
        count=m.CountSession(store_id=primary.ids['north'],campaign_id=campaign.id,
            employee_name='Synthetic Cameron',created_by_principal_id=pid)
        db.add(count);db.flush()
        db.add(m.SnapshotLine(session_id=count.id,variation_id='V1',item_name='Synthetic Freebase',variation_name='6mg',expected_on_hand=928347))
        db.commit();cid=count.id
    provider=Mock();provider.fetch_current_on_hand.return_value={'V1':3}
    monkeypatch.setattr('app.routers.store.snapshot_provider',provider)
    return SimpleNamespace(primary=primary,pid=pid,cid=cid,base=f'/store/drafts/blind-count/{cid}',provider=provider)

def headers(c,context=None):
    client=c.primary.client
    return {'X-Requested-With':'autosave','X-CSRF-Token':client.cookies.get('csrf_token'),
        'X-Working-Context':context or client.get('/session-status').json()['working_context']}


def test_key_is_authorized_private_stable_and_identity_scoped(case):
    c=case;client=c.primary.client
    first=client.get(c.base+'/key');assert first.status_code==200
    assert first.headers['cache-control']=='no-store'
    payload=first.json();assert len(base64.b64decode(payload['key']))==32
    assert payload['scope']=={'principal_id':c.pid,'store_id':c.primary.ids['north'],'kind':'blind-count','record_id':c.cid}
    second=client.get(c.base+'/key').json();assert second['key']==payload['key']
    assert '928347' not in first.text
    assert payload['labels']=={'V1':'Synthetic Freebase — 6mg'}
    other=recovery_key(principal_id=c.pid+1,store_id=c.primary.ids['north'],kind='blind-count',record_id=c.cid)
    assert other['key']!=payload['key']
    for kw in ({'store_id':c.primary.ids['south']},{'record_id':c.cid+1},{'kind':'change-box-count'}):
        args=dict(principal_id=c.pid,store_id=c.primary.ids['north'],kind='blind-count',record_id=c.cid);args.update(kw)
        assert recovery_key(**args)['key']!=payload['key']
    with c.primary.Session() as db:
        token=create_web_session(db,c.pid,None,None);db.commit()
    client.cookies.set(settings.session_cookie_name,token)
    assert client.get(c.base+'/key').json()['key']==payload['key']


def test_weak_default_secret_fails_closed(case,monkeypatch):
    monkeypatch.setattr(settings,'integration_encryption_key',None);monkeypatch.setattr(settings,'app_secret_key','change-me')
    result=case.primary.client.get(case.base+'/key');assert result.status_code==503
    assert 'key' not in result.json()


def test_expired_session_and_wrong_store_cannot_get_key_state_or_receipt(case):
    c=case;client=c.primary.client
    with c.primary.Session() as db:
        foreign=m.CountSession(store_id=c.primary.ids['south'],campaign_id=db.scalar(select(m.Campaign.id)),employee_name='Other',created_by_principal_id=c.pid)
        db.add(foreign);db.commit();foreign_id=foreign.id
    for endpoint in ('key','state','resume'):
        response=client.get(f'/store/drafts/blind-count/{foreign_id}/{endpoint}')
        assert response.status_code==404 and 'key' not in response.json()
    with c.primary.Session() as db:
        token=client.cookies.get(settings.session_cookie_name)
        row=db.scalar(select(m.WebSession).where(m.WebSession.session_token==token));row.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    for endpoint in ('key','state','resume'):
        assert client.get(c.base+'/'+endpoint,headers={'X-Requested-With':'autosave'}).status_code==401


def test_closed_count_has_authorized_receipt_and_final_replay_does_not_duplicate(case):
    c=case;client=c.primary.client;h=headers(c)
    path=f'/store/sessions/{c.cid}'
    saved=client.post(path+'/draft',headers=h,json={'revision':0,'operation_id':'first','changes':{'V1':{'front_qty':'1','back_qty':'2'}}});assert saved.status_code==200
    assert client.post(path+'/submit',headers=h,json={'revision':1}).status_code==200
    replay=client.post(path+'/submit',headers=h,json={'revision':1});assert replay.status_code==200
    assert replay.json()['receipt']['product_count']==1 and replay.json()['receipt']['id']==c.cid
    state=client.get(c.base+'/state');assert state.json()['submitted'] is True
    receipt=client.get(path);assert receipt.status_code==200
    assert 'Work successfully submitted' in receipt.text and '928347' not in receipt.text and 'Front Stock' not in receipt.text
    assert receipt.headers['cache-control']=='no-store'
    with c.primary.Session() as db:
        assert db.scalar(select(func.count()).select_from(m.CountObservation).where(m.CountObservation.session_id==c.cid))==1
        assert db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.session_id==c.cid,m.AuditLog.action=='BLIND_COUNT_OBSERVATIONS_SUBMITTED'))==1
    assert c.provider.fetch_current_on_hand.call_count==1


@pytest.mark.parametrize('kind,path_pattern,model,id_pattern,action',[
    ('non-sellable-stock-take',r'/store/non-sellable-stock-take/(\d+)/save',m.NonSellableStockTake,'non_sellable_stock_take_id','NON_SELLABLE_STOCK_TAKE_DRAFT_SAVED'),
    ('change-box-count',r'/store/change-box-count/(\d+)/save',m.ChangeBoxCount,'change_box_count_id','CHANGE_BOX_COUNT_DRAFT_SAVED'),
    ('daily-chore-sheet',r'/store/daily-chore-sheet/(\d+)/save',m.DailyChoreSheet,'daily_chore_sheet_id','DAILY_CHORE_SHEET_SAVED')])
def test_form_save_replays_are_idempotent_and_receipt_survives_refresh(case,kind,path_pattern,model,id_pattern,action):
    import re
    c=case;client=c.primary.client;page=client.get('/store/'+kind);assert page.status_code==200
    assert 'data-durable-inventory' in page.text and 'inventory-form-drafts.js' in page.text
    record_id=int(re.search(path_pattern,page.text).group(1));base=f'/store/drafts/{kind}/{record_id}'
    initial=client.get(base+'/key').json()
    h={**headers(c),'X-Inventory-Draft':'true','X-Draft-Operation':'stable-operation','X-Draft-Version':str(initial['state']['revision'])}
    data={'employee_name':'Synthetic Cameron','csrf_token':client.cookies.get('csrf_token')}
    # Keep exact raw quantity fields so retries verify the same canonical form payload.
    if kind=='non-sellable-stock-take':
        with c.primary.Session() as db:line=db.scalar(select(m.NonSellableStockTakeLine).where(m.NonSellableStockTakeLine.stock_take_id==record_id));data['qty__'+str(line.item_id)]='12'
    if kind=='change-box-count':data['qty_loose__BILL_1']='12'
    first=client.post(f'/store/{kind}/{record_id}/save',data=data,headers=h)
    assert first.status_code==200,first.text
    assert first.json()['submitted'] is False
    replay=client.post(f'/store/{kind}/{record_id}/save',data=data,headers=h)
    assert replay.status_code==200 and replay.json()==first.json(), replay.text
    changed={**data,'employee_name':'Different values'}
    assert client.post(f'/store/{kind}/{record_id}/save',data=changed,headers=h).status_code==409
    with c.primary.Session() as db:
        assert db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action==action,m.AuditLog.meta[id_pattern].as_integer()==record_id))==1
    h.update({'X-Draft-Operation':'submit-operation','X-Draft-Version':first.json()['revision']})
    submitted=client.post(f'/store/{kind}/{record_id}/submit',data=data,headers=h)
    assert submitted.status_code==200,submitted.text
    assert submitted.json()['submitted'] is True
    replay_submit=client.post(f'/store/{kind}/{record_id}/submit',data=data,headers=h)
    assert replay_submit.status_code==200 and replay_submit.json()==submitted.json()
    receipt=client.get(base+'/resume');assert receipt.status_code==200
    assert 'Work successfully submitted' in receipt.text and not re.search(r'<form[^>]*data-durable-inventory',receipt.text)
    with c.primary.Session() as db:
        assert db.scalar(select(func.count()).select_from(model))==1
    assert client.get(base+'/state').json()['receipt']['id']==record_id


def test_dedicated_recovery_key_survives_unrelated_application_secret_rotation(case,monkeypatch):
    c=case
    monkeypatch.setattr(settings,'inventory_draft_encryption_key',SECRET+'-dedicated')
    first=c.primary.client.get(c.base+'/key').json()['key']
    monkeypatch.setattr(settings,'app_secret_key','rotated-application-secret-with-more-than-32-characters')
    monkeypatch.setattr(settings,'integration_encryption_key','rotated-integration-secret-with-more-than-32-characters')
    assert c.primary.client.get(c.base+'/key').json()['key']==first
