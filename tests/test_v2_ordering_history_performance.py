"""History transport optimization must preserve evidence and bound external concurrency."""
from datetime import datetime,timezone
from threading import Lock,Barrier
from decimal import Decimal
import pytest
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway,SquareProductMetadata

NOW=datetime(2026,10,19,15,tzinfo=timezone.utc)
IDS=[f'V-{i:04}' for i in range(601)]
def adjustment(vid,store='A',quantity='2'):
 return {'type':'ADJUSTMENT','adjustment':{'catalog_object_id':vid,'location_id':store,'from_state':'IN_STOCK','to_state':'SOLD','quantity':quantity,'occurred_at':'2026-10-18T12:00:00.79Z'}}

def history_post(calls,fail=False):
 def post(path,payload):
  assert path=='/v2/inventory/changes/batch-retrieve';calls.append(payload)
  assert set(payload['location_ids'])=={'A','B'} and payload['updated_before']==NOW.isoformat().replace('+00:00','Z')
  if fail and 'V-0200' in payload['catalog_object_ids']:raise RuntimeError('History unavailable')
  ids=payload['catalog_object_ids'];offset=int(payload.get('cursor','0'));rows=[]
  for vid in ids:rows.extend([adjustment(vid),adjustment(vid,'B','3')])
  if IDS[0] in ids:rows.append({'type':'PHYSICAL_COUNT','physical_count':{'catalog_object_id':IDS[0],'location_id':'A'}})
  result={'changes':rows[offset:offset+70]}
  if offset+70<len(rows):result['cursor']=str(offset+70)
  return result
 return post


def test_parallel_history_preserves_all_deltas_physical_counts_and_exact_metrics():
 serial_calls=[];parallel_calls=[]
 a=SquareOrderingReadGateway(history_post(serial_calls));b=SquareOrderingReadGateway(history_post(parallel_calls))
 options=dict(as_of=NOW,include_today=True)
 expected=a._inventory_deltas(['A','B'],IDS,**options)
 actual=b._inventory_deltas(['A','B'],IDS,parallel_history=True,**options)
 assert actual==expected and len(actual)==1202
 assert actual[('A',IDS[0],NOW.date().replace(day=18))]==Decimal('-2')
 assert a._uncertain_history_pairs==b._uncertain_history_pairs=={('A',IDS[0])}
 assert a._inventory_changes_returned==b._inventory_changes_returned==1203
 assert dict(b.current_metrics().endpoint_request_counts)['/v2/inventory/changes/batch-retrieve']==len(parallel_calls)
 assert all(len(c['catalog_object_ids'])<=100 for c in parallel_calls)
 assert max(len(c['catalog_object_ids']) for c in serial_calls)==500
 assert all(len(c['catalog_object_ids'])<=500 for c in serial_calls)


def test_at_most_four_history_requests_and_pagination_stays_with_its_batch():
 lock=Lock();barrier=Barrier(4);state={'active':0,'maximum':0,'calls':0}
 def post(path,payload):
  with lock:state['active']+=1;state['maximum']=max(state['maximum'],state['active']);state['calls']+=1;number=state['calls']
  try:
   if number<=4:barrier.wait(timeout=3)
   assert path=='/v2/inventory/changes/batch-retrieve'
   return {'changes':[adjustment(payload['catalog_object_ids'][0])]}
  finally:
   with lock:state['active']-=1
 gateway=SquareOrderingReadGateway(post)
 assert len(gateway._inventory_deltas(['A'],IDS,as_of=NOW,parallel_history=True))==7
 assert state['maximum']==4 and state['calls']==7


@pytest.mark.parametrize('parallel',[False,True])
def test_failed_history_never_turns_into_available_zero_demand_evidence(parallel):
 calls=[];history=history_post(calls,fail=True)
 def post(path,payload):
  if path.endswith('/changes/batch-retrieve'):return history(path,payload)
  if path.endswith('/counts/batch-retrieve'):return {'counts':[]}
  if path=='/v2/orders/search':return {'orders':[]}
  raise AssertionError(path)
 gateway=SquareOrderingReadGateway(post)
 result=gateway.fetch(location_by_store={1:'A',2:'B'},variation_ids=IDS,as_of=NOW,include_today=True,catalog={},parallel_history=parallel)
 assert len(result.by_store_variation)==1202
 assert all(not next(s for s in row.required_sources if s.source=='stockout_history').available for row in result.by_store_variation.values())
 assert all(row.current_on_hand is None and not row.inventory_valid for row in result.by_store_variation.values())


def test_parallel_repeated_cursor_fails_instead_of_looping_or_returning_partial_history():
 calls=[]
 def post(path,payload):
  assert path=='/v2/inventory/changes/batch-retrieve';calls.append(payload)
  return {'changes':[adjustment(payload['catalog_object_ids'][0])],'cursor':'stuck'}
 with pytest.raises(ValueError,match='Repeated inventory history cursor'):
  SquareOrderingReadGateway(post)._inventory_deltas(['A'],IDS,as_of=NOW,parallel_history=True)
 assert len(calls)<=2*7
