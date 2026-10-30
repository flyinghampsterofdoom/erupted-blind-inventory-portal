"""Only live current-count evidence changes; missing counts never become zero."""
from uuid import uuid4
from copy import deepcopy
from sqlalchemy import text
import pytest
from app.services.current_inventory_refresh import refresh
from app.services.daily_square_collector import LOCK,load_scope
from app.purchasing_v4.evidence_rules import load_inventory,inventory_observation
from app.purchasing_v4.contracts import EvidenceState
from datetime import timedelta
from test_daily_square_collector import collected_engine,engine,template,Provider,NOW
from app.purchasing_v4.reconciliation import public_fingerprints


def fingerprints(e):
 with e.connect() as c:
  c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'));return {k:v for k,v in public_fingerprints(c).items() if k not in ('ordering_current_inventory','ordering_inventory_refresh_runs','purchasing_evidence_runs')}


def test_current_refresh_is_read_only_in_square_idempotent_and_preserves_other_records(collected_engine):
 e=collected_engine;p=Provider();before=fingerprints(e);cid=str(uuid4())
 result=refresh(e,p,actor_id=1,command_id=cid,clock=lambda:NOW)
 assert result['status']=='COMPLETE' and result['known_pairs']==8
 assert all(path=='/v2/inventory/counts/batch-retrieve' for path,_ in p.requests) and len(p.requests)==2
 assert refresh(e,p,actor_id=1,command_id=cid,clock=lambda:NOW)['replayed'] and len(p.requests)==2
 assert fingerprints(e)==before
 with e.connect() as c:
  rows=load_inventory(lambda sql,**values:c.execute(text(sql),values).mappings().all())
  assert len(rows)==8 and all(r['counted_quantity']==9 for r in rows)
  assert c.scalar(text("SELECT count(*) FROM purchasing_v4.collection_attempt"))==0
 with pytest.raises(PermissionError):refresh(e,p,actor_id=2,command_id=cid,clock=lambda:NOW)


def test_partial_refresh_certifies_only_explicit_stable_counts_and_preserves_unknowns(collected_engine):
 e=collected_engine;p=Provider();p.missing=True
 result=refresh(e,p,actor_id=1,command_id=str(uuid4()),clock=lambda:NOW)
 assert result['status']=='PARTIAL' and result['known_pairs']==6 and result['unknown_pairs']==2
 with e.connect() as c:
  rows=load_inventory(lambda sql,**values:c.execute(text(sql),values).mappings().all())
  assert len(rows)==6
  assert all(inventory_observation(r,r['square_location_id'],variation_id=r['square_variation_id'],store_id=r['store_id'],as_of=NOW,max_age=timedelta(hours=1)).state==EvidenceState.KNOWN for r in rows)


def test_failed_and_conflicting_reads_preserve_every_previous_count(collected_engine):
 e=collected_engine;refresh(e,Provider(),actor_id=1,command_id=str(uuid4()),clock=lambda:NOW)
 with e.connect() as c:before=[dict(r) for r in c.execute(text('SELECT * FROM ordering_current_inventory ORDER BY square_variation_id,store_id')).mappings()]
 def fail(path,payload):raise ConnectionError('secret source body must not escape')
 result=refresh(e,fail,actor_id=1,command_id=str(uuid4()),clock=lambda:NOW)
 assert result['status']=='FAILED' and result['known_pairs']==0 and 'secret' not in str(result)
 with e.connect() as c:assert [dict(r) for r in c.execute(text('SELECT * FROM ordering_current_inventory ORDER BY square_variation_id,store_id')).mappings()]==before
 provider=Provider()
 def duplicate(path,payload):
  data=provider(path,payload);data['counts'].append(deepcopy(data['counts'][0]));return data
 assert refresh(e,duplicate,actor_id=1,command_id=str(uuid4()),clock=lambda:NOW)['status']=='FAILED'


def test_source_lock_excludes_daily_or_manual_overlap(collected_engine):
 e=collected_engine;p=Provider()
 with e.connect() as c:
  c.execute(text('SELECT pg_advisory_lock(:key)'),{'key':LOCK});c.commit()
  try:
   with pytest.raises(RuntimeError):refresh(e,p,actor_id=1,command_id=str(uuid4()),clock=lambda:NOW)
   assert not p.requests
  finally:c.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':LOCK});c.commit()


def test_changed_counts_remain_unknown_without_rewriting_old_quantities(collected_engine):
 e=collected_engine;p=Provider();n=0
 def changed(path,payload):
  nonlocal n
  n+=1;data=p(path,payload)
  if n==2:data['counts'][0]['quantity']='12'
  return data
 result=refresh(e,changed,actor_id=1,command_id=str(uuid4()),clock=lambda:NOW)
 assert result['status']=='PARTIAL' and result['known_pairs']==7 and result['unknown_pairs']==1


from test_purchasing_v4_worksheet_http import client,sheet,ready
from app.purchasing_v4 import worksheets


def test_manual_http_refresh_requires_csrf_and_owned_worksheet_and_does_not_recalculate(client):
 c,state=client;calls=[]
 def check(**values):calls.append(values);return dict(status='COMPLETE',command_id=values['command_id'],known_pairs=8,unknown_pairs=0)
 c.app.state.inventory_source_refresh=check;sid=state['session']['id'];body=dict(command_id=str(uuid4()));headers={'X-CSRF-Token':c.cookies['csrf_token']};before=c.get('/api/worksheets/'+sid).json()
 assert c.post('/api/worksheets/'+sid+'/inventory-refresh',json=body).status_code==403 and not calls
 assert c.post('/api/worksheets/'+str(uuid4())+'/inventory-refresh',json=body,headers=headers).status_code==422 and not calls
 response=c.post('/api/worksheets/'+sid+'/inventory-refresh',json=body,headers=headers);assert response.status_code==200,response.text
 assert calls==[dict(actor_id=1,command_id=body['command_id'])] and c.get('/api/worksheets/'+sid).json()==before


def test_start_fresh_checks_inventory_before_generation_and_replay_does_not_refetch(client,monkeypatch):
 c,state=client;events=[]
 def check(**values):events.append('inventory');return dict(status='COMPLETE',command_id=values['command_id'],known_pairs=8,unknown_pairs=0)
 def generation(*args,**kw):
  assert events==['inventory'];events.append('calculation');rows=[dict(deepcopy(r['original']),id=str(uuid4())) for r in state['rows']];return rows,None
 c.app.state.inventory_source_refresh=check;monkeypatch.setattr(worksheets,'prepare_generation',generation)
 body=dict(command_id=str(uuid4()),calculate=True,title='Current inventory fixture');headers={'X-CSRF-Token':c.cookies['csrf_token']}
 response=c.post('/api/worksheets',json=body,headers=headers);assert response.status_code==200,response.text
 assert events==['inventory','calculation'] and response.json()['inventory_refresh']['status']=='COMPLETE'
 assert c.post('/api/worksheets',json=body,headers=headers).json()==response.json() and events==['inventory','calculation']


def test_failed_inventory_prevents_new_worksheet_but_retains_existing_draft(client):
 c,state=client;c.app.state.inventory_source_refresh=lambda **kw:dict(status='FAILED',command_id=kw['command_id']);headers={'X-CSRF-Token':c.cookies['csrf_token']};before=c.get('/api/worksheets').json()
 result=c.post('/api/worksheets',json=dict(command_id=str(uuid4()),calculate=False),headers=headers);assert result.status_code==424,result.text
 assert c.get('/api/worksheets').json()==before


def test_explicit_zero_negative_and_fractional_counts_keep_exact_values(collected_engine):
 e=collected_engine;provider=Provider()
 def source(path,payload):
  data=provider(path,payload)
  for i,row in enumerate(data['counts']):row['quantity']='0' if i==0 else '-2.125'
  return data
 result=refresh(e,source,actor_id=1,command_id=str(uuid4()),clock=lambda:NOW);assert result['status']=='COMPLETE'
 with e.connect() as c:
  quantities=list(c.scalars(text('SELECT counted_quantity FROM ordering_current_inventory')));assert len(quantities)==8 and sum(q==0 for q in quantities)==1 and sum(str(q)=='-2.125' for q in quantities)==7
