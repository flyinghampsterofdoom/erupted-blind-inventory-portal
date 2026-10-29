"""Resend protocol, correspondence retention and purchasing integrity qualification."""
from uuid import uuid4
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import json,hashlib,base64,hmac
import pytest
from sqlalchemy import text
from app.purchasing_v4 import vendor_email as mail,email_worker as worker,controls,quotations as q,outbox
from app.purchasing_v4.email_simulator import install,FixtureResend
from app.purchasing_v4.schema import migrate_email
from test_purchasing_v4_quotations import quote,book,upload,select,final,commit,purchasing,sheet,ready,engine,template,reading,review,authorize,fresh,edit,save,pytestmark

SECRET='whsec_'+base64.b64encode(b'isolated-test-signing-key-32bytes!').decode()
def signed(payload,mid=None,stamp=None):
 raw=json.dumps(payload,separators=(',',':')).encode();stamp=stamp or str(int(datetime.now(timezone.utc).timestamp()));mid=mid or 'msg_'+str(uuid4());sig=base64.b64encode(hmac.new(base64.b64decode(SECRET[6:]),mid.encode()+b'.'+stamp.encode()+b'.'+raw,hashlib.sha256).digest()).decode();return raw,{'svix-id':mid,'svix-timestamp':stamp,'svix-signature':'v1,'+sig}

@pytest.fixture
def email(quote):
 e,state,rid,h=quote
 with e.begin() as c:
  migrate_email(c)
  # Fixture profile opt-in was pinned at original authorization. A new synthetic
  # request uses the original fixture's explicit route, supplied at creation.
  sid=state['rows'][0]['supplier_id'];p=controls.heads(c,{('SUPPLIER',sid)})[('SUPPLIER',sid)]
  controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Explicit email test opt-in',changes=[dict(kind='SUPPLIER',key=sid,expected_version=p['version'],value=dict(p['payload'],automatic_spreadsheet_email=True,automatic_po_email=True,email='vendor@example.invalid'))])
 # Original opt-out request is left intact; create another reviewed fixture.
 from test_purchasing_v4_submission import fresh
 e,state=fresh((e,state));result=authorize((e,state),review((e,state)))
 rid=result['children'][0]['id']
 with e.connect() as c:h=q.head(c,rid);did=c.execute(text('SELECT id::text FROM purchasing_v4.mail_delivery WHERE vendor_authorization_id=:id'),{'id':rid}).scalar_one()
 install(e);return (e,state,rid,h),did,FixtureResend(e)

def reply(email,content=None,*,sender='vendor@example.invalid',auth='pass',recipient=None,filename='vendor.xlsx'):
 f,did,provider=email;e=f[0];pid,aid=str(uuid4()),str(uuid4());content=book(f) if content is None else content
 with e.begin() as c:
  env=mail.delivery(c,did)['envelope'];message=dict(id=pid,**{'from':sender},to=[recipient or env['reply_to']],authentication=dict(dmarc=auth),subject='Vendor can change subject',raw=dict(download_url='fixture:message:'+pid),attachments=[dict(id=aid,filename=filename,size=len(content))])
  c.execute(text('INSERT INTO purchasing_v4_email_simulator.message(id,payload,original) VALUES (:id,CAST(:p AS jsonb),:raw)'),dict(id=pid,p=json.dumps(message),raw=b'Original synthetic vendor MIME\r\n'+content))
  c.execute(text('INSERT INTO purchasing_v4_email_simulator.attachment(id,message_id,filename,content) VALUES (:id,:pid,:filename,:content)'),dict(id=aid,pid=pid,filename=filename,content=content))
 raw,headers=signed(dict(type='email.received',data=dict(email_id=pid),created_at=datetime.now(timezone.utc).isoformat()))
 with e.begin() as c:event=mail.accept_event(c,raw,headers,SECRET)
 return event['event_id'],pid,aid,raw,headers

def test_automatic_request_uses_protected_original_template_and_safe_envelope(email):
 f,did,p=email
 with f[0].connect() as c:
  row=mail.delivery(c,did);body=mail.payload(c,row);assert set(body)=={'from','to','reply_to','subject','text','attachments','tags'};assert body['from']==mail.SENDER;assert body['to']==['vendor@example.invalid'];assert row['state']=='NOT_SENT';assert base64.b64decode(body['attachments'][0]['content'])==bytes(q.revision(c,f[3]['revision_id'])['template'])
 assert worker.send(f[0],did,p)['state']=='ACCEPTED';assert worker.send(f[0],did,p)['replayed'];assert p.calls==1

@pytest.mark.parametrize('quantity,price',[(0,None),(1,'12.34'),(6,'50.1234'),(20,'0')])
def test_valid_reply_imports_same_phase6_evidence_never_authorizes(email,quantity,price):
 f,did,p=email;e=f[0]
 with e.connect() as c:before=c.execute(text('SELECT count(*) FROM public.purchase_orders')).scalar_one()
 event,pid,aid,_,_=reply(email,book(f,quantity=quantity,price=price));r=worker.process_event(e,event,p);assert r['state']=='DONE';assert r['result']['results'][0]['outcome']=='OWNER_REVIEW_REQUIRED'
 with e.connect() as c:
  assert c.execute(text('SELECT count(*) FROM public.purchase_orders')).scalar_one()==before;assert q.head(c,f[2])['selected_response_id'] is None;assert bytes(c.execute(text('SELECT original FROM purchasing_v4.mail_message WHERE provider_id=:id'),{'id':pid}).scalar_one()).startswith(b'Original synthetic')

@pytest.mark.parametrize('mode',['LOST','BEFORE'])
def test_uncertain_delivery_never_automatically_resends(email,mode):
 f,did,p=email
 with f[0].begin() as c:c.execute(text('INSERT INTO purchasing_v4_email_simulator.fault(delivery_id,mode) VALUES (:id,:mode)'),dict(id=did,mode=mode))
 assert worker.send(f[0],did,p)['state']=='UNCERTAIN';assert worker.send(f[0],did,p,retry=True,actor_id=1,reason='Owner reviews uncertain outcome')['state']=='UNCERTAIN';assert p.calls==1

def test_definitive_rejection_allows_owner_retry_same_identity(email):
 f,did,p=email
 with f[0].begin() as c:c.execute(text("INSERT INTO purchasing_v4_email_simulator.fault(delivery_id,mode) VALUES (:id,'REJECT')"),{'id':did})
 assert worker.send(f[0],did,p)['state']=='FAILED';worker.send(f[0],did,p);assert p.calls==1
 with f[0].begin() as c:c.execute(text("UPDATE purchasing_v4_email_simulator.fault SET mode='OK' WHERE delivery_id=:id"),{'id':did})
 assert worker.send(f[0],did,p,retry=True,actor_id=1,reason='Owner retries explicit rejection')['state']=='ACCEPTED'

@pytest.mark.parametrize('sender,auth,recipient',[('attacker@example.invalid','pass',None),('vendor@example.invalid','fail',None),('vendor@example.invalid','pass','wrong@orders.eruptedvapor.com')])
def test_suspicious_reply_is_retained_without_import(email,sender,auth,recipient):
 event,*_=reply(email,sender=sender,auth=auth,recipient=recipient);r=worker.process_event(email[0][0],event,email[2]);assert r['result']['results'][0]['outcome']=='SUSPICIOUS_RESPONSE'

def test_duplicate_events_and_manual_attachment_share_one_response(email):
 f,did,p=email;content=book(f);manual=upload(f,content);event,pid,aid,raw,headers=reply(email,content)
 with f[0].begin() as c:assert mail.accept_event(c,raw,headers,SECRET)['event_id']==event
 r=worker.process_event(f[0],event,p);assert r['result']['results'][0]['outcome']=='DUPLICATE_RESPONSE';assert worker.process_event(f[0],event,p)['replayed']
 with f[0].connect() as c:assert c.execute(text('SELECT count(*) FROM purchasing_v4.quotation_response WHERE request_id=:id'),{'id':f[2]}).scalar_one()==1

@pytest.mark.parametrize('fault',['unsigned','modified','old','wrong_secret'])
def test_webhook_authentication_rejects_spoof_and_replay_window(fault):
 raw,headers=signed(dict(type='email.received',data=dict(email_id=str(uuid4()))),stamp='1' if fault=='old' else None)
 if fault=='unsigned':headers['svix-signature']=''
 if fault=='modified':raw+=b' '
 with pytest.raises(PermissionError):mail.verify_webhook(raw,headers,SECRET+'wrong' if fault=='wrong_secret' else SECRET)

def test_same_event_identity_changed_body_rejected(email):
 event,_,_,raw,headers=reply(email);changed,h=signed(dict(type='email.sent',data={}),mid=event)
 with email[0][0].begin() as c:
  with pytest.raises(controls.Conflict):mail.accept_event(c,changed,h,SECRET)

@pytest.mark.parametrize('change',[lambda w:setattr(w['Quotation']['F12'],'value','Changed protected name'),lambda w:setattr(w['Quotation']['H12'],'value','=1+1')])
def test_invalid_protected_or_formula_response_retained(email,change):
 content=book(email[0],mutate=change);event,*_=reply(email,content);r=worker.process_event(email[0][0],event,email[2]);assert r['result']['results'][0]['outcome']=='INVALID_WORKBOOK'

def test_malformed_unmatched_attachment_retained(email):
 event,*_=reply(email,b'not an xlsx');r=worker.process_event(email[0][0],event,email[2]);assert r['result']['results'][0]['outcome']=='INVALID_WORKBOOK'

def test_concurrent_sends_claim_original_once(email):
 f,did,p=email
 with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:worker.send(f[0],did,p),range(2)))
 assert p.calls==1

def test_lost_acceptance_authenticated_event_reconciles_without_resend(email):
 f,did,p=email;e=f[0]
 with e.begin() as c:c.execute(text("INSERT INTO purchasing_v4_email_simulator.fault(delivery_id,mode) VALUES (:id,'LOST')"),{'id':did})
 assert worker.send(e,did,p)['state']=='UNCERTAIN'
 with e.connect() as c:pid=c.execute(text('SELECT id FROM purchasing_v4_email_simulator.sent WHERE delivery_id=:id'),{'id':did}).scalar_one();env=mail.delivery(c,did)['envelope']
 def event(kind):
  raw,h=signed(dict(type=kind,created_at=datetime.now(timezone.utc).isoformat(),data=dict(email_id=pid,**{'from':env['sender']},to=[env['recipient']],tags=dict(purchasing_delivery=did))))
  with e.begin() as c:return mail.accept_event(c,raw,h,SECRET)['event_id']
 assert worker.process_event(e,event('email.delivered'),p)['state']=='DONE';worker.process_event(e,event('email.sent'),p)
 with e.connect() as c:assert mail.delivery(c,did)['state']=='DELIVERED'
 assert p.calls==1


def test_reissue_and_cancel_hold_original_queued_delivery(email):
 f,did,p=email;e=f[0]
 with e.begin() as c:q.transition(c,actor_id=1,command_id=str(uuid4()),request_id=f[2],expected_version=f[3]['version'],kind='CANCEL',reason='Owner explicitly cancels isolated request before delivery')
 assert worker.send(e,did,p)['state']=='HELD';assert p.calls==0
 event,*_=reply(email);r=worker.process_event(e,event,p);assert r['result']['results'][0]['outcome']=='OUTDATED_RESPONSE'


def test_restricted_worker_can_intake_but_cannot_authorize_or_read_finance(email):
 f,did,p=email;event,*_=reply(email);restricted=worker.worker_engine(f[0].url)
 try:
  assert worker.send(restricted,did,p)['state']=='ACCEPTED'
  with f[0].connect() as c:ev=c.execute(text('SELECT payload FROM purchasing_v4.mail_event WHERE id=:id'),{'id':event}).scalar_one()
  worker.receive_message(restricted,event,ev,p)
  r=worker.process_event(restricted,event,p);assert r['state']=='DONE',r;assert r['result']['results'][0]['outcome']=='OWNER_REVIEW_REQUIRED',r
  for sql in ['SELECT snapshot FROM purchasing_v4.authorized_vendor','SELECT snapshot FROM purchasing_v4.quotation_revision','INSERT INTO purchasing_v4.purchase_authorization DEFAULT VALUES','UPDATE public.purchase_orders SET status=status']:
   with restricted.begin() as c:
    with pytest.raises(Exception):c.execute(text(sql))
 finally:restricted.dispose()


def test_worker_interruption_never_implicitly_releases_original_send(email):
 f,did,p=email;original=p.send
 class ProcessLoss(BaseException):pass
 def fail(*args):original(*args);raise ProcessLoss()
 p.send=fail
 with pytest.raises(ProcessLoss):worker.send(f[0],did,p)
 p.send=original;assert worker.send(f[0],did,p)['state']=='SENDING';assert worker.send(f[0],did,p,retry=True,actor_id=1,reason='Prior worker deliberately stopped',interrupted=True)['state']=='UNCERTAIN';assert p.calls==1


def test_mail_evidence_cannot_be_overwritten_or_state_reset_without_proof(email):
 e=email[0][0]
 for sql in ['UPDATE purchasing_v4.mail_delivery SET envelope=\'{}\'','DELETE FROM purchasing_v4.mail_delivery','TRUNCATE purchasing_v4.mail_delivery CASCADE',"UPDATE purchasing_v4.mail_state SET state='DELIVERED',version=version+1"]:
  with e.begin() as c:
   with pytest.raises(Exception):c.execute(text(sql))


def test_standard_po_document_delivery_preserves_accepted_commitment(email):
 f,_,p=email;e,state,_,_=f;sid=state['rows'][0]['supplier_id']
 with e.begin() as c:
  h=controls.heads(c,{('SUPPLIER',sid)})[('SUPPLIER',sid)];controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Explicit standard vendor test',changes=[dict(kind='SUPPLIER',key=sid,expected_version=h['version'],value=dict(h['payload'],confirmation_enabled=False))])
 e,state=fresh((e,state));result=authorize((e,state),review((e,state)));child=result['children'][0];we=outbox.worker_engine(e.url)
 try:outbox.generate_documents(we,child['document_command_id'])
 finally:we.dispose()
 with e.connect() as c:
  did=c.execute(text('SELECT id::text FROM purchasing_v4.mail_delivery WHERE vendor_authorization_id=:id'),{'id':child['id']}).scalar_one();before=c.execute(text('SELECT to_jsonb(p) FROM public.purchase_orders p WHERE id=:id'),{'id':child['purchase_order_id']}).scalar_one();body=mail.payload(c,mail.delivery(c,did));assert body['attachments'][0]['filename'].endswith('.pdf');assert base64.b64decode(body['attachments'][0]['content']).startswith(b'%PDF')
 restricted=worker.worker_engine(e.url)
 try:worker.send(restricted,did,p)
 finally:restricted.dispose()
 with e.connect() as c:assert c.execute(text('SELECT to_jsonb(p) FROM public.purchase_orders p WHERE id=:id'),{'id':child['purchase_order_id']}).scalar_one()==before

def test_resend_wire_contract_is_bounded_and_reuses_existing_transport():
 import httpx
 from app.purchasing_v4.email_provider import ResendProvider
 calls=[];pid=str(uuid4())
 def handler(request):
  calls.append(request)
  if request.method=='POST':return httpx.Response(200,json=dict(id=pid))
  return httpx.Response(200,json=dict(id=pid,last_event='delivered'))
 with httpx.Client(base_url='https://api.resend.com',transport=httpx.MockTransport(handler)) as client:
  p=ResendProvider(client,client,controlled_recipients={'controlled@example.invalid'},download_hosts={'inbound-cdn.resend.com'})
  assert p.send(str(uuid4()),dict(to=['controlled@example.invalid']))==pid;assert calls[0].headers['Idempotency-Key'].startswith('purchasing/');assert p.sent(pid)['last_event']=='delivered'
  with pytest.raises(PermissionError):p.send(str(uuid4()),dict(to=['actual-vendor@example.invalid']))
  with pytest.raises(PermissionError):p.download('http://127.0.0.1/private',100)


def test_network_wait_does_not_block_worksheet_editing(email):
 from threading import Event
 f,did,p=email;e=f[0];started=Event();finish=Event();original=p.send
 def slow(*args):started.set();assert finish.wait(10);return original(*args)
 p.send=slow
 with ThreadPoolExecutor(max_workers=2) as pool:
  job=pool.submit(worker.send,e,did,p);assert started.wait(5)
  try:
   e,state=fresh((e,f[1]));r=state['rows'][0]
   from app.purchasing_v4 import worksheets
   with e.begin() as c:result=worksheets.patch(c,actor_id=1,command_id=str(uuid4()),session_id=state['session']['id'],expected_version=0,changes=[dict(id=r['id'],value=dict(supplier_id=r['supplier_id'],pack_size=r['pack_size'],packs=[2,0,0,0],touched=[True]*4,removed=False))])
   assert result['version']==1
  finally:finish.set()
  assert job.result()['state']=='ACCEPTED'

def test_owner_provider_reconciliation_retains_original_identity(email):
 f,did,p=email;e=f[0]
 with e.begin() as c:c.execute(text("INSERT INTO purchasing_v4_email_simulator.fault(delivery_id,mode) VALUES (:id,'LOST')"),{'id':did})
 worker.send(e,did,p)
 with e.connect() as c:pid=c.execute(text('SELECT id FROM purchasing_v4_email_simulator.sent WHERE delivery_id=:id'),{'id':did}).scalar_one()
 restricted=worker.worker_engine(e.url)
 try:assert worker.reconcile(restricted,did,p,provider_id=pid,actor_id=1,reason='Owner identifies original provider message')['state']=='ACCEPTED'
 finally:restricted.dispose()
 assert p.calls==1


def test_named_sender_reply_keeps_provider_provenance_and_imports(email):
 event,*_=reply(email,sender='Vendor Contact <vendor@example.invalid>');restricted=worker.worker_engine(email[0][0].url)
 try:r=worker.process_event(restricted,event,email[2]);assert r['state']=='DONE',r;assert r['result']['results'][0]['outcome']=='OWNER_REVIEW_REQUIRED',r
 finally:restricted.dispose()

def test_oversize_attachment_keeps_original_message_and_exception(email):
 content=b'x'*(q.protocol.MAX_FILE+1);event,pid,*_=reply(email,content);r=worker.process_event(email[0][0],event,email[2]);assert r['result']['results'][0]['outcome']=='OVERSIZED_ATTACHMENT'
 with email[0][0].connect() as c:assert c.execute(text('SELECT octet_length(original) FROM purchasing_v4.mail_message WHERE provider_id=:id'),{'id':pid}).scalar_one()>len(content)


def test_valid_other_request_without_delivery_route_is_unmatched(email):
 f,_,p=email
 with f[0].connect() as c:content=bytes(c.execute(text('SELECT template FROM purchasing_v4.quotation_revision WHERE request_id<>:id ORDER BY created_at LIMIT 1'),{'id':f[2]}).scalar_one())
 event,*_=reply(email,content);r=worker.process_event(f[0],event,p);assert r['result']['results'][0]['outcome']=='UNMATCHED_RESPONSE'


def test_reissued_current_workbook_is_queued_once_and_prior_reply_stays_outdated(email):
 f,did,p=email;content=book(f)
 with f[0].begin() as c:
  snap=q.revision(c,f[3]['revision_id'])['snapshot'];choices={l['row_id']:dict(pack_size=l['pack_size'],packs=l['packs']) for l in snap['lines']};new=q.transition(c,actor_id=1,command_id=str(uuid4()),request_id=f[2],expected_version=f[3]['version'],kind='REISSUE',reason='Owner explicitly reissues original request',changes=choices)
  assert mail.queue_child(c,f[2],1,new['revision_id'])==c.execute(text('SELECT id::text FROM purchasing_v4.mail_delivery WHERE revision_id=:id'),{'id':new['revision_id']}).scalar_one()
 assert worker.send(f[0],did,p)['state']=='HELD'
 event,*_=reply(email,content);r=worker.process_event(f[0],event,p);assert r['result']['results'][0]['outcome']=='OUTDATED_RESPONSE'


def test_past_idempotency_window_does_not_release_uncertain_send(email):
 f,did,p=email
 with f[0].begin() as c:c.execute(text("INSERT INTO purchasing_v4_email_simulator.fault(delivery_id,mode) VALUES (:id,'BEFORE')"),{'id':did})
 worker.send(f[0],did,p)
 with f[0].begin() as c:
  row=mail.delivery(c,did)
  # A separate immutable attempt records owner inspection; no window expiry can
  # transition UNCERTAIN back to SENDING, even under direct SQL.
  with pytest.raises(Exception):c.execute(text("UPDATE purchasing_v4.mail_state SET state='SENDING',version=version+1,lease_id=:lease WHERE delivery_id=:id"),dict(id=did,lease=str(uuid4())))
 assert worker.send(f[0],did,p)['state']=='UNCERTAIN';assert p.calls==1

def test_mail_is_bound_to_existing_outbox_and_legacy_sender_cannot_compete(email):
 f,did,p=email;e=f[0]
 with e.connect() as c:
  canonical=c.execute(text('SELECT kind,payload_digest FROM purchasing_v4.integration_command WHERE id=:id'),{'id':did}).one();assert canonical[0]=='PURCHASING_EMAIL';assert canonical[1]==mail.delivery(c,did)['payload_digest']
 legacy=outbox.worker_engine(e.url)
 try:
  with pytest.raises(PermissionError):outbox.deliver_sandbox(legacy,did,outbox.SandboxProvider())
 finally:legacy.dispose()
 with e.connect() as c:assert c.execute(text('SELECT state FROM purchasing_v4.integration_state WHERE command_id=:id'),{'id':did}).scalar_one()=='PENDING'
 assert worker.send(e,did,p)['state']=='ACCEPTED'
 with e.connect() as c:assert c.execute(text('SELECT state FROM purchasing_v4.integration_state WHERE command_id=:id'),{'id':did}).scalar_one()=='ACKNOWLEDGED'
 assert p.calls==1

def test_attachment_identity_cannot_import_changed_bytes_after_interrupted_intake(email,monkeypatch):
 f,did,p=email;event,pid,aid,_,_=reply(email);original=q.intake
 def interrupted(*args,**kwargs):raise RuntimeError('Injected persistence interruption after original attachment retention')
 monkeypatch.setattr(q,'intake',interrupted);assert worker.process_event(f[0],event,p)['state']=='FAILED';monkeypatch.setattr(q,'intake',original)
 with f[0].begin() as c:
  content=bytes(c.execute(text('SELECT content FROM purchasing_v4_email_simulator.attachment WHERE id=:id'),{'id':aid}).scalar_one());changed=bytes([content[0]^1])+content[1:];c.execute(text('UPDATE purchasing_v4_email_simulator.attachment SET content=:b WHERE id=:id'),dict(id=aid,b=changed))
 assert worker.process_event(f[0],event,p)['state']=='FAILED'
 with f[0].connect() as c:
  assert c.execute(text('SELECT count(*) FROM purchasing_v4.mail_import')).scalar_one()==0
  assert c.execute(text('SELECT sha256 FROM purchasing_v4.mail_attachment WHERE provider_attachment_id=:id'),{'id':aid}).scalar_one()==hashlib.sha256(content).hexdigest()

def test_existing_po_writer_commands_do_not_require_mail_read_permission(email):
 e=email[0][0]
 with e.begin() as c:
  c.execute(text('CREATE TEMP TABLE p8_nonmail_guard(kind text) ON COMMIT DROP'))
  c.execute(text('CREATE CONSTRAINT TRIGGER p8_guard AFTER INSERT ON p8_nonmail_guard DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_v4.mail_outbox_binding_guard()'))
  c.execute(text('GRANT INSERT ON p8_nonmail_guard TO erupted_v4_po_writer'))
  c.execute(text('SET LOCAL ROLE erupted_v4_po_writer'))
  c.execute(text("INSERT INTO p8_nonmail_guard(kind) VALUES ('GENERATE_DOCUMENTS')"))
