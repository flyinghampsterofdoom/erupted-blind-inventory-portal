"""Isolated HWY99 physical receipt and deterministic remote failure qualification."""
from copy import deepcopy
from uuid import uuid4
import pytest
from sqlalchemy import text
from app.purchasing_v4 import receiving as r,receiving_worker as worker,controls
from app.purchasing_v4.receiving_square import InventoryAdapter,API_VERSION,WRITE_PATH,HISTORY_PATH
from app.services.square_request_policy import enforce_square_request_policy
from app.purchasing_v4.schema import migrate_receiving
from test_purchasing_v4_submission import purchasing,sheet,ready,engine,template,reading,review,authorize,pytestmark


class FakeSquare:
    """Square contract simulator only: counts never establish adjustment success."""
    headers={'Square-Version':API_VERSION}
    def __init__(self):self.applied={};self.sends=[];self.mode='OK';self.history_visible=True
    def post(self,path,payload,*,inventory_quantity_write=False):
        enforce_square_request_policy('POST',path,payload,inventory_quantity_write=inventory_quantity_write)
        if path==HISTORY_PATH:return dict(changes=list(self.applied.values()) if self.history_visible else [])
        assert path==WRITE_PATH;self.sends.append(deepcopy(payload))
        if self.mode=='BEFORE':raise ConnectionError('Injected transport failure before apply; outcome unknown to client')
        key=payload['idempotency_key']
        for i,component in enumerate(payload['changes']):self.applied.setdefault(component['adjustment']['reference_id'],dict(type='ADJUSTMENT',adjustment=dict(component['adjustment'],id='remote-'+key+'-'+str(i))))
        if self.mode=='LOST':raise ConnectionError('Injected response loss after Square accepted the adjustment')
        return dict(counts=[dict(quantity='987654321')])


@pytest.fixture
def receiving(purchasing):
    e,state=purchasing
    with e.begin() as c:migrate_receiving(c)
    result=authorize(purchasing,review(purchasing));po=result['children'][0]['purchase_order_id']
    with e.begin() as c:r.capture_order(c,po)
    return e,po,state


def state(fixture):
    with fixture[0].connect() as c:return r.projection(c,fixture[1],actor_id=1)


def cmd(fixture,kind='RECEIPT',lines=None,**kw):
    p=state(fixture);l=p['lines'][0];args=dict(actor_id=1,command_id=str(uuid4()),purchase_order_id=fixture[1],expected_version=p['version'],kind=kind,lines=lines or [dict(line_id=l['id'],good=l['supplier_outstanding'],damaged=0)],reason='Explicit verified isolated receiving or owner split decision',confirmed=True);args.update(kw)
    with fixture[0].begin() as c:return r.command(c,**args)


def drain(fixture,fake):
    a=InventoryAdapter(fake)
    for _ in range(30):
        rows=[p for l in state(fixture)['lines'] for p in l['posting'] if p['state']!='VERIFIED']
        if not rows:break
        for p in rows:worker.run(fixture[0],p['id'],a)
    return state(fixture)


def test_fixed_actual_hwy99_no_new_location_and_original_po_immutable(receiving):
    e,po,_=receiving;p=state(receiving);assert p['snapshot']['central']['name']=='HWY 99';assert p['snapshot']['central']['id']==2
    with e.connect() as c:before=dict(c.execute(text('SELECT * FROM public.purchase_orders WHERE id=:id'),{'id':po}).mappings().one())
    cmd(receiving);p=drain(receiving,FakeSquare());assert p['lines'][0]['supplier_outstanding']==0 and p['lines'][0]['posting_complete']
    with e.connect() as c:assert dict(c.execute(text('SELECT * FROM public.purchase_orders WHERE id=:id'),{'id':po}).mappings().one())==before


def test_full_receipt_uses_approved_split_and_conserves_all_units(receiving):
    cmd(receiving);before=state(receiving);l=before['lines'][0];assert l['allocated']==l['ordered'];assert any(p['state']=='PENDING' for p in l['posting'])
    assert sum(b['units'] for b in l['balances'])==0
    fake=FakeSquare();p=drain(receiving,fake);l=p['lines'][0];assert l['posting_complete'];assert sum(b['units'] for b in l['balances'])==0
    assert not any(b['units']<0 for b in l['balances'] if b['account']!='SUPPLIER_EXTERNAL')
    assert sum(b['units'] for b in l['balances'] if b['account'] in ('CENTRAL_AVAILABLE','DESTINATION_AVAILABLE'))==l['received_good']
    assert all(x['idempotency_key']==x['changes'][0]['adjustment']['reference_id'] for x in fake.sends)


def test_partial_receipt_waits_for_owner_split_without_proportional_allocation(receiving):
    l=state(receiving)['lines'][0];cmd(receiving,lines=[dict(line_id=l['id'],good=3,damaged=0)]);p=drain(receiving,FakeSquare());l=p['lines'][0]
    assert l['unassigned']==3 and sum(l['allocated'].values())==0
    split={k:0 for k in l['ordered']};split['1']=3
    cmd(receiving,'ALLOCATE',[dict(line_id=l['id'],split=split)]);assert state(receiving)['lines'][0]['allocated']['1']==3


def test_duplicate_receipt_same_uuid_returns_same_result(receiving):
    cid=str(uuid4());p=state(receiving);lines=[dict(line_id=p['lines'][0]['id'],good=p['lines'][0]['supplier_outstanding'],damaged=0)];first=cmd(receiving,command_id=cid,lines=lines);second=cmd(receiving,command_id=cid,expected_version=p['version'],lines=lines);assert second['replayed'];assert first['version']==second['version']
    with pytest.raises(controls.Conflict):cmd(receiving,command_id=cid)


def test_lost_square_response_reconciles_exact_adjustment_without_resending(receiving):
    cmd(receiving);fake=FakeSquare();fake.mode='LOST';a=InventoryAdapter(fake);op=next(p for p in state(receiving)['lines'][0]['posting'] if p['predecessor_id'] is None)
    assert worker.run(receiving[0],op['id'],a)['state']=='AMBIGUOUS';assert len(fake.applied)==1
    assert worker.run(receiving[0],op['id'],a)['state']=='VERIFIED';assert len(fake.sends)==1


def test_counts_and_missing_history_never_prove_success(receiving):
    cmd(receiving);fake=FakeSquare();fake.history_visible=False;op=next(p for p in state(receiving)['lines'][0]['posting'] if p['predecessor_id'] is None)
    assert worker.run(receiving[0],op['id'],InventoryAdapter(fake))['state']=='AMBIGUOUS'
    assert worker.run(receiving[0],op['id'],InventoryAdapter(fake))['state']=='AMBIGUOUS';assert len(fake.sends)==1
    assert not state(receiving)['lines'][0]['posting_complete']


def test_v3_receiving_ownership_including_po129_is_not_adopted(receiving):
    with receiving[0].begin() as c:
        with pytest.raises(controls.InvalidPolicy):r.capture_order(c,129)


def test_interrupted_worker_keeps_durable_identity_and_reconciles(receiving):
    cmd(receiving);fake=FakeSquare();a=InventoryAdapter(fake);op=next(p for p in state(receiving)['lines'][0]['posting'] if p['predecessor_id'] is None)
    def crash():raise KeyboardInterrupt('Injected process loss after remote apply')
    with pytest.raises(KeyboardInterrupt):worker.run(receiving[0],op['id'],a,after_remote=crash)
    assert worker.run(receiving[0],op['id'],a)['state']=='IN_FLIGHT'
    worker.recover_interrupted(receiving[0],op['id'],actor_id=1,reason='Owner confirms isolated worker process was stopped')
    assert worker.run(receiving[0],op['id'],a)['state']=='VERIFIED' and len(fake.sends)==1


def test_partial_posting_preserves_success_and_controlled_retry_keeps_key(receiving):
    cmd(receiving);fake=FakeSquare();a=InventoryAdapter(fake);p=state(receiving)['lines'][0]['posting'];first=next(x for x in p if x['predecessor_id'] is None)
    assert worker.run(receiving[0],first['id'],a)['state']=='VERIFIED'
    second=next(x for x in p if x['predecessor_id']==first['id']);fake.mode='BEFORE'
    assert worker.run(receiving[0],second['id'],a)['state']=='AMBIGUOUS'
    assert not state(receiving)['lines'][0]['posting_complete'];fake.mode='OK'
    assert worker.retry_same_identity(receiving[0],second['id'],a,actor_id=1,reason='Owner reviewed remote ambiguity; retry exact original inventory payload and identity')['state']=='VERIFIED'
    assert fake.sends[-1]==fake.sends[-2];assert worker.run(receiving[0],first['id'],a)['replayed']


def test_concurrent_receipts_one_stale_version_rejected(receiving):
    from concurrent.futures import ThreadPoolExecutor
    p=state(receiving);line=p['lines'][0];values=[dict(line_id=line['id'],good=1,damaged=0)]
    def one(_):
        try:return cmd(receiving,expected_version=p['version'],lines=values)
        except controls.Conflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(one,range(2)))
    assert sum(isinstance(x,dict) for x in results)==1 and results.count('conflict')==1
    assert state(receiving)['lines'][0]['received_good']==1


def test_broken_pack_damage_and_explicit_owner_closure(receiving):
    l=state(receiving)['lines'][0];cmd(receiving,lines=[dict(line_id=l['id'],good=3,damaged=2)]);drain(receiving,FakeSquare());l=state(receiving)['lines'][0]
    assert l['received_good']==3 and l['received_damaged']==2 and l['damage_unresolved']==2
    with pytest.raises(controls.InvalidPolicy):cmd(receiving,'CLOSE',[dict(line_id=l['id'],stores=l['remaining'])])
    cmd(receiving,'RESOLVE',[dict(line_id=l['id'],acknowledged_damage=2,physical_evidence='Owner retains damaged goods in quarantine and independently reviews supplier replacement obligation')])
    split={k:0 for k in l['ordered']};split['1']=3;cmd(receiving,'ALLOCATE',[dict(line_id=l['id'],split=split)]);drain(receiving,FakeSquare());l=state(receiving)['lines'][0]
    cmd(receiving,'CLOSE',[dict(line_id=l['id'],stores=l['remaining'])]);assert state(receiving)['lines'][0]['supplier_outstanding']==0


def test_overage_cannot_increase_original_commitment_and_needs_explicit_valuation(receiving):
    l=state(receiving)['lines'][0];excess=l['supplier_outstanding']+1
    with pytest.raises(controls.InvalidPolicy):cmd(receiving,lines=[dict(line_id=l['id'],good=excess,damaged=0)])
    allocation={k:0 for k in l['ordered']};allocation['1']=1
    cmd(receiving,'OVERAGE',[dict(line_id=l['id'],stores=allocation,currency='USD',approved_total='2.51',valuation_reason='Owner explicitly approves this separate overage value; historical PO and expected cost remain unchanged')])
    cmd(receiving,lines=[dict(line_id=l['id'],good=excess,damaged=0)]);assert state(receiving)['lines'][0]['ordered_qty']==l['ordered_qty']
    with receiving[0].connect() as c:assert c.execute(text('SELECT ordered_qty FROM public.purchase_order_lines WHERE id=:id'),{'id':l['id']}).scalar_one()==l['ordered_qty']


def test_draft_save_has_no_receipt_or_inventory_effect_and_conflicts_stale(receiving):
    with receiving[0].begin() as c:r.save_draft(c,actor_id=1,purchase_order_id=receiving[1],expected_draft_version=0,base_version=0,payload=dict(fields=[['good:1','7']],split_touched=[]))
    assert not state(receiving)['history'] and not state(receiving)['lines'][0]['posting']
    with receiving[0].begin() as c:
        with pytest.raises(controls.Conflict):r.save_draft(c,actor_id=1,purchase_order_id=receiving[1],expected_draft_version=0,base_version=0,payload=dict(fields=[],split_touched=[]))


def test_incoming_switches_from_original_obligation_to_verified_split_once(receiving):
    from app.purchasing_v4.receiving_supply import incoming_sql
    from app.purchasing_v4.evidence_rules import INCOMING_SQL,incoming_quantity
    def source():
        with receiving[0].connect() as c:
            rows=[dict(x) for x in c.execute(text(incoming_sql(c,INCOMING_SQL))).mappings()]
            return incoming_quantity(rows,'FIXTURE_V1')
    before,why,_=source();assert not why
    cmd(receiving);assert source()[0] is None
    drain(receiving,FakeSquare());after,why,_=source();assert not why
    l=state(receiving)['lines'][0];assert sum(before.values())-sum(after.values())==l['ordered_qty']


def test_receiving_capabilities_are_explicit_and_admin_is_not_owner(receiving):
    e,po,_=receiving
    with e.begin() as c:
        c.execute(text("INSERT INTO public.principals(id,username,password_hash,role,active) VALUES (2,'receiver','not-login-capable','MANAGER',true),(3,'other-admin','not-login-capable','ADMIN',true)"))
    with e.connect() as c:
        with pytest.raises(PermissionError):r.capability(c,2,'VIEW',2)
        with pytest.raises(PermissionError):r.capability(c,3,'VIEW',2)
    for cap in ['VIEW','ENTER','CONFIRM']:
        with e.begin() as c:r.grant(c,actor_id=1,command_id=str(uuid4()),principal_id=2,kind=cap,enabled=True,reason='Owner explicitly delegates this isolated receiving capability',store_id=2)
    p=state(receiving);l=p['lines'][0];cmd(receiving,actor_id=2,lines=[dict(line_id=l['id'],good=3,damaged=0)])
    assert state(receiving)['lines'][0]['unassigned']==3
    with pytest.raises(PermissionError):cmd(receiving,'ALLOCATE',[dict(line_id=l['id'],split={k:0 for k in l['ordered']})],actor_id=2)
    with e.begin() as c:r.grant(c,actor_id=1,command_id=str(uuid4()),principal_id=2,kind='CONFIRM',enabled=False,reason='Owner explicitly revokes confirmation',store_id=2)
    with pytest.raises(PermissionError):cmd(receiving,actor_id=2,lines=[dict(line_id=l['id'],good=1,damaged=0)])


def test_append_only_corrections_preserve_original_receipt_and_physical_balances(receiving):
    l=state(receiving)['lines'][0];cmd(receiving,lines=[dict(line_id=l['id'],good=3,damaged=0)]);fake=FakeSquare();drain(receiving,fake)
    first=state(receiving)['history'][0]
    cmd(receiving,'CORRECTION',[dict(line_id=l['id'],from_account='CENTRAL_UNASSIGNED',store_id=None,units=1,disposition='RETURN_FOR_REPLACEMENT',physical_evidence='Owner verified physical supplier return for replacement; reopen this explicit unit obligation')])
    drain(receiving,fake);p=state(receiving);assert p['history'][0]==first and p['lines'][0]['received_good']==2 and p['lines'][0]['unassigned']==2
    assert sum(b['units'] for b in p['lines'][0]['balances'])==0


def test_worker_cannot_authorize_or_edit_financial_and_receiving_facts(receiving):
    with worker.transaction(receiving[0]) as c:
        with pytest.raises(Exception):c.execute(text('SELECT * FROM purchasing_v4.quotation_key'))
    with worker.transaction(receiving[0]) as c:
        with pytest.raises(Exception):c.execute(text("INSERT INTO purchasing_v4.receiving_event(id,purchase_order_id,version,actor_id,kind,payload,payload_digest,result) VALUES (:id,:po,1,1,'CLOSE','{}',repeat('0',64),'{}')"),dict(id=str(uuid4()),po=receiving[1]))
    with receiving[0].begin() as c:
        with pytest.raises(Exception):c.execute(text('UPDATE purchasing_v4.receiving_order SET central_store_id=1 WHERE purchase_order_id=:po'),{'po':receiving[1]})


def test_native_transfer_is_one_exact_cross_location_adjustment_per_destination(receiving):
    cmd(receiving);p=state(receiving);l=p['lines'][0];central=p['snapshot']['central']['square_location_id'];payloads=[x['payload']['changes'][0]['adjustment'] for x in l['posting']]
    transfers=[x for x in payloads if x['from_location_id']!=x['to_location_id']]
    assert len(transfers)==sum(v>0 and k!=str(p['snapshot']['central']['id']) for k,v in l['ordered'].items())
    assert all(x['from_location_id']==central and x['from_state']==x['to_state']=='IN_STOCK' and 'location_id' not in x for x in transfers)
    from app.purchasing_v4.receiving_square import match_adjustment
    whole=next(x['payload'] for x in l['posting'] if x['payload']['changes'][0]['adjustment']['from_location_id']!=x['payload']['changes'][0]['adjustment']['to_location_id']);proof=deepcopy(whole['changes'][0]);proof['adjustment']['id']='remote-exact'
    assert match_adjustment(whole,proof);proof['adjustment']['to_location_id']='DIFFERENT_VERIFIED_LOCATION';assert not match_adjustment(whole,proof)


def test_central_request_policy_supports_new_locations_but_not_product_mutations():
    from app.services.square_request_policy import SquareWriteBoundaryViolation
    from app.purchasing_v4.receiving_square import payload
    p=payload(str(uuid4()),variation_id='FIXTURE_V1',location_id='FIXTURE_LOC2',to_location_id='FIXTURE_LOC1',units=3,from_state='IN_STOCK',to_state='IN_STOCK',occurred_at='2026-10-28T12:00:00Z')
    enforce_square_request_policy('POST',WRITE_PATH,p,inventory_quantity_write=True)
    with pytest.raises(SquareWriteBoundaryViolation):enforce_square_request_policy('POST','/v2/catalog/batch-upsert',{},inventory_quantity_write=True)
    with pytest.raises(SquareWriteBoundaryViolation):enforce_square_request_policy('POST',WRITE_PATH,p)
    del p['changes'][0]['adjustment']['to_location_id']
    with pytest.raises(SquareWriteBoundaryViolation):enforce_square_request_policy('POST',WRITE_PATH,p,inventory_quantity_write=True)


def test_owner_corrects_misallocated_stock_with_native_transfer_and_append_only_allocations(receiving):
    from app.purchasing_v4.receiving_supply import incoming_sql
    from app.purchasing_v4.evidence_rules import INCOMING_SQL,incoming_quantity
    cmd(receiving);fake=FakeSquare();drain(receiving,fake);l=state(receiving)['lines'][0]
    with receiving[0].connect() as c:original=[dict(x) for x in c.execute(text('SELECT * FROM public.purchase_order_store_allocations WHERE purchase_order_line_id=:id ORDER BY store_id'),{'id':l['id']}).mappings()]
    cmd(receiving,'CORRECTION',[dict(line_id=l['id'],from_account='DESTINATION_AVAILABLE',store_id=1,to_store_id=4,units=3,disposition='REALLOCATE_POSTED',physical_evidence='Owner verifies the physical store split was misallocated; transfer three units from Andresen to Longview without changing purchasing value')])
    p=drain(receiving,fake);after=p['lines'][0];assert after['ordered']==l['ordered'] and after['allocated']['1']==l['allocated']['1']-3 and after['allocated']['4']==3
    assert sum(b['units'] for b in after['balances'])==0 and after['supplier_outstanding']==0
    with receiving[0].connect() as c:
        assert [dict(x) for x in c.execute(text('SELECT * FROM public.purchase_order_store_allocations WHERE purchase_order_line_id=:id ORDER BY store_id'),{'id':l['id']}).mappings()]==original
        rows=[dict(x) for x in c.execute(text(incoming_sql(c,INCOMING_SQL))).mappings()];values,why,_=incoming_quantity(rows,'FIXTURE_V1');assert not why


def test_incorrect_product_is_quarantined_as_evidence_without_inventing_square_identity(receiving):
    l=state(receiving)['lines'][0];result=cmd(receiving,'DISCREPANCY',[dict(line_id=l['id'],description='Supplier delivered a different product; original expected units remain outstanding',units=3,observed_product='Unidentified product on packing slip; no verified Square reference')]);p=state(receiving)
    assert p['lines'][0]['supplier_outstanding']==l['supplier_outstanding'] and not p['lines'][0]['posting']
    assert p['lines'][0]['discrepancies'][0]['units']==3
    cmd(receiving,'RESOLVE',[dict(line_id=l['id'],discrepancy_event_id=result['command_id'],returned_units=3,physical_evidence='Owner verifies all three incorrect units were physically returned to the supplier',complete=True)])
    assert state(receiving)['lines'][0]['discrepancies'][0]['complete'];assert not state(receiving)['lines'][0]['posting']


def test_network_wait_does_not_hold_worksheet_editing_locks(receiving):
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    from test_purchasing_v4_submission import fresh,edit,save
    draft=fresh((receiving[0],receiving[2]));cmd(receiving);fake=FakeSquare();entered=Event();release=Event()
    original=fake.post
    def blocked(path,payload,**kw):
        if path==WRITE_PATH:entered.set();assert release.wait(10)
        return original(path,payload,**kw)
    fake.post=blocked;op=next(p for p in state(receiving)['lines'][0]['posting'] if p['predecessor_id'] is None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(worker.run,receiving[0],op['id'],InventoryAdapter(fake));assert entered.wait(5)
        try:
            changed=pool.submit(save,draft[0],draft[1],edit(draft[1]));assert changed.result(timeout=3)['version']==1
        finally:release.set()
        assert pending.result()['state']=='VERIFIED'


def test_verified_receiving_supply_digest_is_stable_across_review_and_writer_timezones(receiving):
    from test_purchasing_v4_submission import fresh
    from app.purchasing_v4 import submission
    cmd(receiving);drain(receiving,FakeSquare());draft=fresh((receiving[0],receiving[2]));r0=review(draft);assert not r0['blocking']
    with receiving[0].begin() as c:
        c.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
        result=submission.authorize(c,actor_id=1,command_id=str(uuid4()),review_id=r0['id'],acknowledged=True,source_namespace='synthetic-fixture-source')
    assert result['commitments_created']==1


def test_damaged_receipt_uses_one_atomic_quarantine_operation_with_both_remote_proofs(receiving):
    l=state(receiving)['lines'][0];cmd(receiving,lines=[dict(line_id=l['id'],good=0,damaged=2)]);p=state(receiving);assert len(p['lines'][0]['posting'])==1
    operation=p['lines'][0]['posting'][0];changes=operation['payload']['changes'];assert len(changes)==2
    assert [(x['adjustment']['from_state'],x['adjustment']['to_state']) for x in changes]==[('NONE','IN_STOCK'),('IN_STOCK','WASTE')]
    fake=FakeSquare();result=worker.run(receiving[0],operation['id'],InventoryAdapter(fake));assert result['state']=='VERIFIED'
    p=state(receiving);assert sum(b['units'] for b in p['lines'][0]['balances'] if b['account']=='CENTRAL_QUARANTINE')==2
    assert not any(b['units'] for b in p['lines'][0]['balances'] if b['account'] in ('CENTRAL_AVAILABLE','DESTINATION_AVAILABLE'))
    with receiving[0].connect() as c:
        proof=c.execute(text('SELECT evidence FROM purchasing_v4.inventory_verification WHERE command_id=:id'),{'id':operation['id']}).scalar_one();assert len(proof['changes'])==2


def test_partial_quarantine_history_never_proves_full_atomic_outcome(receiving):
    l=state(receiving)['lines'][0];cmd(receiving,lines=[dict(line_id=l['id'],good=0,damaged=2)]);op=state(receiving)['lines'][0]['posting'][0];fake=FakeSquare();original=fake.post;hide=[True]
    def transport(path,payload,**kw):
        response=original(path,payload,**kw)
        if path==HISTORY_PATH and hide[0]:response['changes']=[x for x in response['changes'] if x['adjustment']['to_state']!='WASTE']
        return response
    fake.post=transport;a=InventoryAdapter(fake)
    assert worker.run(receiving[0],op['id'],a)['state']=='AMBIGUOUS';assert len(fake.sends)==1
    hide[0]=False;assert worker.run(receiving[0],op['id'],a)['state']=='VERIFIED';assert len(fake.sends)==1


def test_legacy_incoming_reader_uses_the_same_v4_projection_without_changing_v3(receiving):
    from sqlalchemy.orm import Session
    from app.services.purchasing_inventory_service import load_incoming_supply
    with Session(receiving[0]) as db:before=load_incoming_supply(db)
    legacy=[x for x in before if x.purchase_order_id==129];v4=[x for x in before if x.purchase_order_id==receiving[1]];assert sum(x.quantity for x in v4)==state(receiving)['lines'][0]['ordered_qty']
    cmd(receiving)
    with Session(receiving[0]) as db:pending=load_incoming_supply(db)
    assert all(x.quantity is None for x in pending if x.purchase_order_id==receiving[1])
    drain(receiving,FakeSquare())
    with Session(receiving[0]) as db:after=load_incoming_supply(db)
    assert [x for x in after if x.purchase_order_id==129]==legacy
    assert sum(x.quantity for x in after if x.purchase_order_id==receiving[1])==0


def test_inventory_observation_is_independent_from_posting_proof_and_can_confirm_zero():
    from datetime import datetime,timedelta,timezone
    from decimal import Decimal
    from app.purchasing_v4.receiving_supply import qualify_inventory
    from app.purchasing_v4.contracts import QuantityEvidence,EvidenceState
    now=datetime(2026,10,28,12,tzinfo=timezone.utc);remote=now-timedelta(seconds=5);verified=now-timedelta(seconds=3)
    observed=QuantityEvidence(EvidenceState.KNOWN,Decimal(0),'independent_square_count',observed_at=now)
    membership=[dict(command_id=str(uuid4()),state='VERIFIED',updated_at=verified,remote_received_at=remote,square_location_id='FIXTURE_LOC2')]
    count=dict(source_calculated_at=remote,refreshed_at=now,started_at=now-timedelta(seconds=1),square_location_id='FIXTURE_LOC2')
    assert qualify_inventory(observed,membership,count,as_of=now).state==EvidenceState.KNOWN
    assert qualify_inventory(observed,membership,dict(count,started_at=remote),as_of=now).state==EvidenceState.UNKNOWN
    assert qualify_inventory(observed,membership,dict(count,square_location_id='DIFFERENT_LOCATION'),as_of=now).value is None
    membership[0]['state']='AMBIGUOUS';assert qualify_inventory(observed,membership,count,as_of=now).state==EvidenceState.UNKNOWN


def test_square_nanosecond_timestamp_format_does_not_change_exact_intent():
    from app.purchasing_v4.receiving_square import payload,match_adjustment
    cid=str(uuid4());p=payload(cid,variation_id='FIXTURE_V1',location_id='FIXTURE_LOC2',units=1,from_state='NONE',to_state='IN_STOCK',occurred_at='2026-10-28T12:00:00.123456+00:00')
    proof=deepcopy(p['changes'][0]);proof['adjustment']['id']='independent-remote-id';proof['adjustment']['occurred_at']='2026-10-28T12:00:00.123456000Z';assert match_adjustment(p,proof)
    proof['adjustment']['occurred_at']='2026-10-28T12:00:00.123456001Z';assert not match_adjustment(p,proof)
