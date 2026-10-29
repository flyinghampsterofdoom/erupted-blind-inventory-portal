"""Disposable PostgreSQL owner authorization, canonical finance and duplicate safety."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text
import pytest
from app.purchasing_v4 import submission,worksheets,controls,outbox
from app.purchasing_v4.schema import migrate_commitments
from test_purchasing_v4_worksheets import sheet,ready,engine,template,reading,edit,save,AS_OF
from test_purchasing_v4_controls import profile,offer,change,execute
pytestmark=pytest.mark.skipif(not __import__('os').getenv('TEST_POSTGRES_ADMIN_URL'),reason='Explicit disposable PostgreSQL required')

@pytest.fixture
def purchasing(sheet):
 e,state=sheet
 with e.begin() as c:
  migrate_commitments(c);submission.configure_owner(c,1)
  if e.url.password:
   from psycopg import sql
   # Explicit disposable fixture credential; no trust-auth or production change.
   c.connection.driver_connection.execute(sql.SQL('ALTER ROLE erupted_v4_outbox_worker PASSWORD {}').format(sql.Literal(e.url.password)))
 return e,state


def review(purchasing,**options):
 e,state=purchasing;row=state['rows'][0]
 args=dict(actor_id=1,review_id=str(uuid4()),session_id=state['session']['id'],expected_version=state['session']['version'],supplier_ids=[row['supplier_id']],source_namespace='synthetic-fixture-source',currency='USD',valuations={row['id']:dict(source='EXPECTED' if row['unit_cost'] is not None else 'OWNER_EXPLICIT',unit_value=row['unit_cost'] if row['unit_cost'] is not None else '2.50',reason='Explicit owner fixture valuation; expected cost remains unchanged')},incremental_reason='Explicit additional units after reviewing existing legacy incoming supply',public_header=dict(purchaser='ERUPTED — isolated sample',ship_to='100 Fixture Way, Test City; isolated sample'))
 args.update(options);return submission.prepare_review(e,**args)


def authorize(purchasing,r,command=None):
 with purchasing[0].begin() as c:return submission.authorize(c,actor_id=1,command_id=command or str(uuid4()),review_id=r['id'],acknowledged=True,source_namespace='synthetic-fixture-source')


def test_exact_authorized_financial_identity_allocations_and_seal(purchasing):
 e,state=purchasing;r=review(purchasing);assert not r['blocking'];result=authorize(purchasing,r);assert result['commitments_created']==1
 child=result['children'][0]
 with reading(e) as c:
  po=c.execute(text('SELECT * FROM public.purchase_orders WHERE id=:id'),{'id':child['purchase_order_id']}).mappings().one();assert po['reorder_weeks'] is None
  assert c.execute(text('SELECT writer FROM purchasing_v4.po_ownership WHERE purchase_order_id=:id'),{'id':po['id']}).scalar_one()=='V4'
  assert c.execute(text('SELECT sum(allocated_qty) FROM public.purchase_order_store_allocations WHERE purchase_order_line_id IN(SELECT id FROM public.purchase_order_lines WHERE purchase_order_id=:id)'),{'id':po['id']}).scalar_one()==sum(state['rows'][0]['packs'])*state['rows'][0]['pack_size']
  assert worksheets.snapshot(c,state['session']['id'],1)['rows'][0]['sealed']['purchase_order_id']==po['id']
 with pytest.raises(Exception):save(e,dict(state,session=dict(state['session'],version=1)),edit(state,9),version=1)


def test_same_and_different_command_double_submission_return_existing(purchasing):
 r=review(purchasing);cid=str(uuid4());first=authorize(purchasing,r,cid)
 assert authorize(purchasing,r,cid)['authorization_id']==first['authorization_id']
 assert authorize(purchasing,r)['authorization_id']==first['authorization_id']
 with purchasing[0].connect() as c:assert c.execute(text("SELECT count(*) FROM purchasing_v4.po_ownership WHERE origin='V4'")).scalar_one()==1


def test_stale_worksheet_and_incoming_reject_without_commitment(purchasing):
 e,state=purchasing;r=review(purchasing);save(e,state,edit(state))
 with pytest.raises(controls.Conflict):authorize(purchasing,r)
 state['session']['version']=1;r=review(purchasing)
 with e.begin() as c:c.execute(text('UPDATE public.purchase_order_store_allocations SET store_received_qty=store_received_qty+1 WHERE id=1'));c.execute(text('UPDATE public.purchase_order_lines SET received_qty_total=received_qty_total+1 WHERE id=1'))
 with pytest.raises(controls.Conflict):authorize(purchasing,r)
 with e.connect() as c:assert c.execute(text('SELECT count(*) FROM purchasing_v4.purchase_authorization')).scalar_one()==0


def test_concurrent_reviews_consume_one_intent(purchasing):
 reviews=[review(purchasing),review(purchasing)]
 def run(r):
  try:return authorize(purchasing,r)
  except controls.Conflict:return 'conflict'
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,reviews))
 assert sum(isinstance(x,dict) for x in results)==1


def test_docs_real_immutable_and_worker_cannot_authorize(purchasing):
 from app.purchasing_v4.vendor_documents import render
 e,state=purchasing;result=authorize(purchasing,review(purchasing));child=result['children'][0];worker=outbox.worker_engine(e.url);files=outbox.generate_documents(worker,child['document_command_id']);assert {a['format'] for a in files}=={'PDF','XLSX'}
 assert outbox.generate_documents(worker,child['document_command_id'])==files
 with outbox.transaction(worker) as c:
  with pytest.raises(Exception):c.execute(text("INSERT INTO public.purchase_orders(vendor_id,created_by_principal_id) VALUES (1,1)"))
 with reading(e) as c:
  snapshot=c.execute(text('SELECT payload FROM purchasing_v4.integration_command WHERE id=:id'),{'id':child['document_command_id']}).scalar_one()
 assert render(snapshot['document'])==render(snapshot['document'])
 worker.dispose()


def test_unknown_cost_requires_explicit_owner_valuation(purchasing):
 e,state=purchasing;r=review(purchasing,valuations={});assert any('valuation required' in x for x in r['blocking'])
 with pytest.raises(controls.InvalidPolicy):authorize(purchasing,r)
 r=review(purchasing,valuations={state['rows'][0]['id']:dict(source='OWNER_EXPLICIT',unit_value='0',reason='Explicitly approved free sample valuation; invoice remains unrecorded')})
 assert authorize(purchasing,r)['commitments_created']==1


def fresh(purchasing,title='Incremental fixture'):
 e,state=purchasing
 with e.begin() as c:
  rows=worksheets.catalog_rows(c,[state['rows'][0]['variation_id']],'synthetic-fixture-source');rows[0].update(packs=[1,0,0,0],touched=[True]*4)
  return e,worksheets.create(c,actor_id=1,command_id=str(uuid4()),title=title,rows=rows)


def test_pending_requests_are_not_committed_incoming_and_incremental_is_explicit(purchasing):
 e,state=purchasing;sid=state['rows'][0]['supplier_id']
 with e.begin() as c:
  head=controls.heads(c,{('SUPPLIER',sid)})[('SUPPLIER',sid)];value=dict(head['payload'],confirmation_enabled=True)
  controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Explicit synthetic confirmation workflow',changes=[dict(kind='SUPPLIER',key=sid,expected_version=head['version'],value=value)])
 r=review(purchasing,valuations={});result=authorize(purchasing,r);assert result['commitments_created']==0 and result['requests_created']==1
 new=fresh(purchasing);blocked=review(new,valuations={},incremental_reason='');assert blocked['blocking'] and any(x['kind']=='PENDING_REQUEST' for x in blocked['lines'][0]['overlap'])
 assert blocked['lines'][0]['incoming']==r['lines'][0]['incoming']
 with pytest.raises(controls.InvalidPolicy):authorize(new,blocked)
 approved=review(new,valuations={},incremental_reason='Owner explicitly requests additional units in addition to pending request')
 assert authorize(new,approved)['requests_created']==1
 with e.connect() as c:assert c.execute(text("SELECT count(*) FROM purchasing_v4.po_ownership WHERE origin='V4'")).scalar_one()==0


def test_legitimate_incremental_po_and_legacy_overlap_guard(purchasing):
 e,state=purchasing;first=authorize(purchasing,review(purchasing));new=fresh(purchasing)
 held=review(new,incremental_reason='');assert any('additional-purchase' in m for m in held['blocking'])
 second=authorize(new,review(new));assert second['children'][0]['purchase_order_id']!=first['children'][0]['purchase_order_id']
 with pytest.raises(Exception,match='incremental'):
  with e.begin() as c:
   c.execute(text("INSERT INTO purchase_orders(id,vendor_id,created_by_principal_id) VALUES (10001,1,1)"))
   c.execute(text("INSERT INTO purchase_order_lines(id,purchase_order_id,variation_id,item_name,variation_name,ordered_qty) VALUES (10001,10001,'FIXTURE_V1','Legacy duplicate','First',5)"))
 with e.begin() as c:c.execute(text('UPDATE purchase_order_lines SET received_qty_total=received_qty_total+1 WHERE id=1'));c.execute(text('UPDATE purchase_order_store_allocations SET store_received_qty=store_received_qty+1 WHERE id=1'))
 with e.connect() as c:
  assert c.execute(text('SELECT count(*) FROM purchase_orders WHERE id=10001')).scalar_one()==0
  assert c.execute(text("SELECT writer FROM purchasing_v4.po_ownership WHERE purchase_order_id=129")).scalar_one()=='LEGACY'


def test_lost_provider_ack_stays_ambiguous_and_reuses_command(purchasing):
 e,state=purchasing
 with e.begin() as c:
  sid=state['rows'][0]['supplier_id'];h=controls.heads(c,{('SUPPLIER',sid)})[('SUPPLIER',sid)]
  controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Explicit simulated automatic delivery',changes=[dict(kind='SUPPLIER',key=sid,expected_version=h['version'],value=dict(h['payload'],automatic_po_email=True,email='fixture@example.invalid'))])
 result=authorize(purchasing,review(purchasing));worker=outbox.worker_engine(e.url);child=result['children'][0]
 outbox.generate_documents(worker,child['document_command_id'])
 with worker.connect() as c:cid=c.execute(text("SELECT id::text FROM purchasing_v4.integration_command WHERE kind='DELIVER_DOCUMENTS'")).scalar_one()
 fake=outbox.SandboxProvider();fake.lose_ack=True;assert outbox.deliver_sandbox(worker,cid,fake)['state']=='AMBIGUOUS'
 assert outbox.deliver_sandbox(worker,cid,fake)['processed'] is False
 fake.lose_ack=False;assert outbox.deliver_sandbox(worker,cid,fake,reconcile=True)['state']=='ACKNOWLEDGED'
 assert len(fake.accepted)==1
 with e.connect() as c:assert c.execute(text('SELECT count(*) FROM purchasing_v4.purchase_authorization')).scalar_one()==1
 worker.dispose()


def test_worker_login_cannot_assume_purchase_writer_or_read_private_valuations(purchasing):
 e,_=purchasing;worker=outbox.worker_engine(e.url)
 for sql in ['SET ROLE erupted_v4_po_writer','SELECT * FROM purchasing_v4.submission_review',"INSERT INTO purchasing_v4.purchase_authorization(id) VALUES ('00000000-0000-0000-0000-000000000000')"]:
  with pytest.raises(Exception):
   with outbox.transaction(worker) as c:c.execute(text(sql))
 with pytest.raises(PermissionError):
  with outbox.transaction(e):pass
 worker.dispose()


def test_supplier_change_after_review_rejects_and_public_records_immutable(purchasing):
 e,state=purchasing;r=review(purchasing);sid=state['rows'][0]['supplier_id']
 with e.begin() as c:
  h=controls.heads(c,{('SUPPLIER',sid)})[('SUPPLIER',sid)]
  controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Supplier changed after immutable review',changes=[dict(kind='SUPPLIER',key=sid,expected_version=h['version'],value=dict(h['payload'],name='Changed future supplier'))])
 with pytest.raises(controls.Conflict):authorize(purchasing,r)
 result=authorize(purchasing,review(purchasing));po=result['children'][0]['purchase_order_id']
 for sql in [f"UPDATE purchase_orders SET notes='legacy alteration' WHERE id={po}",f"UPDATE purchase_order_lines SET unit_cost=0 WHERE purchase_order_id={po}",f"DELETE FROM purchase_order_store_allocations WHERE purchase_order_line_id IN(SELECT id FROM purchase_order_lines WHERE purchase_order_id={po})",f"UPDATE purchase_order_store_allocations SET purchase_order_line_id=1 WHERE purchase_order_line_id IN(SELECT id FROM purchase_order_lines WHERE purchase_order_id={po})"]:
  with pytest.raises(Exception):
   with e.begin() as c:c.execute(text(sql))


def test_nonowner_admin_cannot_authorize_or_change_owner(purchasing):
 e,state=purchasing
 with e.begin() as c:c.execute(text("INSERT INTO principals(id,username,password_hash,role,active) VALUES(99,'other-admin','not-authenticatable','ADMIN',true)"))
 with pytest.raises(PermissionError):review(purchasing,actor_id=99)
 with pytest.raises(controls.Conflict):
  with e.begin() as c:submission.configure_owner(c,99)


def test_local_financial_identity_uses_existing_accounting_and_preserves_physical_supplier(purchasing):
 from sqlalchemy.orm import Session
 from app.models import PurchaseOrder,PaymentMethod,VendorPaymentClassification,Vendor,OrderPayment
 from app.services.v2_order_payments_service import initialize_order_payment,_order_cost_snapshot
 from decimal import Decimal
 from datetime import date
 e,state=purchasing;local=str(uuid4());vid=state['rows'][0]['variation_id']
 with e.begin() as c:
  controls.create_supplier(c,actor_id=1,command_id=str(uuid4()),reason='Explicit local supplier fixture',supplier_id=local,profile=profile('Local financial supplier'))
  controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Approved local offer',changes=[change('OFFER',vid+':'+local,offer(vid,local,pack=5,cost=None))])
  rows=worksheets.catalog_rows(c,[vid],'synthetic-fixture-source');rows[0].update(supplier_id=local,pack_size=5,packs=[1,2,3,0],touched=[True]*4)
  state=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Explicit local supplier order',rows=rows)
 purchasing=(e,state);result=authorize(purchasing,review(purchasing));poid=result['children'][0]['purchase_order_id']
 with e.begin() as c:
  db=Session(bind=c);po=db.get(PurchaseOrder,poid);vendor=db.get(Vendor,po.vendor_id);assert vendor.square_vendor_id is None
  assert _order_cost_snapshot(db,poid)==(Decimal('75.00'),True)
  method=PaymentMethod(display_name='Fixture Wire',category='WIRE',is_active=True,created_by_principal_id=1,updated_by_principal_id=1);db.add(method);db.flush()
  classification=VendorPaymentClassification(vendor_id=vendor.id,payment_method_id=method.id,payment_category='WIRE',payment_method_label_snapshot='Fixture Wire',is_consignment=False,effective_date=date(2026,10,27),is_current=False,created_by_principal_id=1)
  payment=initialize_order_payment(db,order=po,classification=classification,actor_id=1,event_note='Explicit isolated owner financial assignment after purchase authorization')
  assert payment.purchase_order_id==po.id and payment.vendor_id==vendor.id and payment.order_amount==Decimal('75.00')
  assert payment.status=='UNPAID' and po.invoice_paid_amount is None
  db.flush();db.close()


def test_transaction_interruption_rolls_back_all_children_and_forward_retry(purchasing,monkeypatch):
 e,state=purchasing;r=review(purchasing);original=submission.bridge_supplier
 def interrupted(*args,**kwargs):original(*args,**kwargs);raise RuntimeError('Injected interruption before canonical PO creation')
 with monkeypatch.context() as m:
  m.setattr(submission,'bridge_supplier',interrupted)
  with pytest.raises(RuntimeError):authorize(purchasing,r)
 with e.connect() as c:
  assert c.execute(text('SELECT count(*) FROM purchasing_v4.purchase_authorization')).scalar_one()==0
  assert c.execute(text('SELECT count(*) FROM purchasing_v4.supplier_financial_bridge')).scalar_one()==0
  assert c.execute(text('SELECT count(*) FROM public.purchase_orders')).scalar_one()==1
 assert authorize(purchasing,r)['commitments_created']==1


def test_vendor_safe_projection_excludes_all_private_fields_and_formula_injection():
 from app.purchasing_v4.vendor_documents import vendor_projection,render,validate_projection
 from io import BytesIO
 from zipfile import ZipFile
 from openpyxl import load_workbook
 import base64,zlib,re
 snapshot=dict(authorized_at='2026-10-28T12:00:00+00:00',header=dict(purchaser='ERUPTED isolated sample',ship_to='100 Fixture Way'),supplier=dict(name='Fixture Supplier',email='fixture@example.invalid',internal_notes='SECRET_FINANCE_241926'),stores=[dict(name='PRIVATE_ALLOCATION_9913')],lines=[dict(sku='0000123',vendor_sku='=HYPERLINK("https://example.invalid")',name='Fixture Product',variation='Mint',pack_size=5,total_packs=6,total_units=30,expected_unit_cost='97123.4217',valuation=dict(unit_value='87123.4217'),funding_account='SECRET_ACCOUNT_9127',units=[9913,8217,113,47])])
 public=vendor_projection(str(uuid4()),441,'PO',snapshot);files=render(public);sensitive=['SECRET_FINANCE_241926','PRIVATE_ALLOCATION_9913','97123.4217','87123.4217','SECRET_ACCOUNT_9127']
 with ZipFile(BytesIO(files['XLSX'])) as z:
  all_xml=b'\n'.join(z.read(n) for n in z.namelist());assert all(s.encode() not in all_xml for s in sensitive)
  assert not any('externalLinks' in n or 'vbaProject' in n or 'comments' in n for n in z.namelist())
 book=load_workbook(BytesIO(files['XLSX']),data_only=False);assert len(book.worksheets)==1 and book.active['A9'].value=='0000123';assert book.active['B9'].data_type=='s' and book.active['B9'].value.startswith('=HYPERLINK')
 assert not any(c.data_type=='f' for row in book.active for c in row)
 contents=[files['PDF']]
 for match in re.finditer(rb'stream\r?\n(.*?)endstream',files['PDF'],re.S):
  raw=match.group(1).strip()
  for decode in [lambda r:zlib.decompress(r),lambda r:zlib.decompress(base64.a85decode(r,adobe=True))]:
   try:contents.append(decode(raw))
   except Exception:pass
 assert all(s.encode() not in b'\n'.join(contents) for s in sensitive)
 with pytest.raises(ValueError):validate_projection(dict(public,expected_cost='PRIVATE'))


def test_outbox_crash_lease_forward_recovery_and_no_unproved_ack(purchasing):
 import time
 e,state=purchasing;result=authorize(purchasing,review(purchasing));worker=outbox.worker_engine(e.url);cid=result['children'][0]['document_command_id']
 # Allow real Docker/CI round trips before exercising actual lease expiry.
 command,lease=outbox.claim(worker,cid,'GENERATE_DOCUMENTS',lease_seconds=2)
 with pytest.raises(Exception):
  with worker.begin() as c:c.execute(text("UPDATE purchasing_v4.integration_state SET state='ACKNOWLEDGED',version=version+1 WHERE command_id=:id"),{'id':cid})
 time.sleep(2.1);files=outbox.generate_documents(worker,cid);assert len(files)==2
 with worker.connect() as c:assert c.execute(text('SELECT state FROM purchasing_v4.integration_state WHERE command_id=:id'),{'id':cid}).scalar_one()=='ACKNOWLEDGED'
 worker.dispose()


@pytest.mark.parametrize('confirmation',[False,True])
def test_two_distinct_worksheets_cannot_authorize_the_same_unreviewed_supply_state(purchasing,confirmation):
 e,state=purchasing
 if confirmation:
  sid=state['rows'][0]['supplier_id']
  with e.begin() as c:
   h=controls.heads(c,{('SUPPLIER',sid)})[('SUPPLIER',sid)]
   controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Request concurrency fixture',changes=[dict(kind='SUPPLIER',key=sid,expected_version=h['version'],value=dict(h['payload'],confirmation_enabled=True))])
 one=fresh(purchasing);two=fresh(purchasing);reviews=[(one,review(one)),(two,review(two))]
 def execute(pair):
  try:return authorize(*pair)
  except controls.Conflict:return None
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(execute,reviews))
 assert sum(r is not None for r in results)==1


def test_completed_receipt_since_draft_is_reviewed_but_fresh_future_orders_remain_possible(purchasing):
 e,state=purchasing
 with e.begin() as c:
  c.execute(text("UPDATE purchase_orders SET status='COMPLETED' WHERE id=129"));c.execute(text('UPDATE purchase_order_lines SET received_qty_total=ordered_qty WHERE id=1'))
  c.execute(text('UPDATE purchase_order_store_allocations SET store_received_qty=allocated_qty WHERE purchase_order_line_id=1'))
  receipt=c.execute(text("INSERT INTO purchase_order_receipts(purchase_order_id,status,received_by_principal_id,received_at) VALUES(129,'SUBMITTED',1,now()) RETURNING id")).scalar_one()
  c.execute(text('INSERT INTO purchase_order_receipt_lines(receipt_id,purchase_order_line_id,expected_qty,received_qty,difference_qty) VALUES(:id,1,100,100,0)'),{'id':receipt})
 old=review(purchasing,incremental_reason='');assert old['lines'][0]['incoming']==[0,0,0,0]
 assert any(x['kind']=='PURCHASING_CHANGE_SINCE_GENERATION' for x in old['lines'][0]['overlap'])
 assert old['blocking']
 new=fresh(purchasing);r=review(new,incremental_reason='');assert not r['blocking'];assert authorize(new,r)['commitments_created']==1


def test_same_revision_raw_quantity_change_cannot_escape_exact_review_binding(purchasing):
 e,state=purchasing;r=review(purchasing)
 with e.begin() as c:c.execute(text("UPDATE purchasing_v4.worksheet_row SET working=jsonb_set(jsonb_set(working,'{packs}','[9,9,9,9]'::jsonb),'{touched}','[true,true,true,true]'::jsonb) WHERE session_id=:id"),{'id':state['session']['id']})
 with pytest.raises(controls.Conflict):authorize(purchasing,r)


def test_unresolved_quantity_totals_are_typed_unknown_not_zero(purchasing):
 e,state=purchasing;changes=edit(state);changes[0]['value']['packs']=[None,2,0,0];save(e,state,changes);state['session']['version']=1
 r=review(purchasing);line=r['lines'][0]
 assert line['total_packs'] is None and line['total_units'] is None
 assert not line['quantities_complete'] and r['unresolved_quantity_lines']==1
 assert r['committed_total'] is None and not r['committed_total_complete']
 assert r['expected_total'] is None and not r['expected_total_complete']
 with pytest.raises(controls.InvalidPolicy):authorize(purchasing,r)


def test_confirmed_zero_without_pack_size_remains_zero_and_uncommitted(purchasing):
 e,state=purchasing;changes=edit(state,0);changes[0]['value']['pack_size']=None;save(e,state,changes);state['session']['version']=1
 r=review(purchasing);line=r['lines'][0]
 assert line['total_packs']==0 and line['total_units']==0 and line['units']==[0]*4
 assert line['quantities_complete'] and r['unresolved_quantity_lines']==0
 assert any('no positive' in message for message in r['blocking'])
 assert not any('pack quantities' in message for message in r['blocking'])
 with e.connect() as c:assert c.execute(text('SELECT count(*) FROM purchasing_v4.purchase_authorization')).scalar_one()==0


def test_pack_revision_requires_new_review_and_uses_exact_owner_packs(purchasing):
 e,state=purchasing;r=review(purchasing);changes=edit(state,2);changes[0]['value']['pack_size']=10;save(e,state,changes);state['session']['version']=1
 with pytest.raises(controls.Conflict):authorize(purchasing,r)
 updated=review(purchasing);assert updated['lines'][0]['packs']==[2]*4 and updated['lines'][0]['total_units']==80
 result=authorize(purchasing,updated)
 with e.connect() as c:assert c.execute(text('SELECT ordered_qty FROM public.purchase_order_lines WHERE purchase_order_id=:id'),{'id':result['children'][0]['purchase_order_id']}).scalar_one()==80


def test_vendor_originals_ignore_later_supplier_offer_and_pack_changes(purchasing):
 import hashlib
 from app.purchasing_v4.vendor_documents import render
 e,state=purchasing;result=authorize(purchasing,review(purchasing));child=result['children'][0]
 with e.begin() as c:
  envelope=c.execute(text('SELECT payload FROM purchasing_v4.integration_command WHERE id=:id'),{'id':child['document_command_id']}).scalar_one()
  sid=state['rows'][0]['supplier_id'];key=state['rows'][0]['variation_id']+':'+sid;heads=controls.heads(c,{('SUPPLIER',sid),('OFFER',key)})
  changes=[]
  for kind,k in [('SUPPLIER',sid),('OFFER',key)]:
   h=heads[(kind,k)];value=dict(h['payload'])
   if kind=='SUPPLIER':value.update(name='Future supplier name',email='future@example.invalid')
   else:value.update(pack_size=19,expected_unit_cost='9.99',vendor_sku='FUTURE-SKU')
   changes.append(dict(kind=kind,key=k,expected_version=h['version'],value=value))
  controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Explicit fixture future terms; issued original remains pinned',changes=changes)
 expected={format:hashlib.sha256(content).hexdigest() for format,content in render(envelope['document']).items()};worker=outbox.worker_engine(e.url)
 assert {r['format']:r['sha256'] for r in outbox.generate_documents(worker,child['document_command_id'])}==expected
 assert {r['format']:r['sha256'] for r in outbox.generate_documents(worker,child['document_command_id'])}==expected
 worker.dispose()
