"""Bounded receipt-triggered mail work. External calls never hold domain locks."""
from uuid import uuid4,uuid5,UUID
from datetime import datetime,timezone
from pathlib import PurePath
import hashlib,json,re
from email.utils import parseaddr
from sqlalchemy import text
from . import vendor_email as mail,worksheets,controls,quotations as q
from .email_provider import Rejected
from .isolation import require_isolated


def send(engine,did,provider,*,retry=False,actor_id=None,reason=None,interrupted=False):
 with engine.begin() as c:
  require_isolated(c)
  if retry or interrupted:
   if not c.execute(text('SELECT 1 FROM purchasing_v4.mail_owner WHERE id=:id'),{'id':actor_id}).scalar_one_or_none():raise PermissionError('Explicit configured owner recovery authority required')
   if not isinstance(reason,str) or not reason.strip():raise controls.InvalidPolicy('Explicit owner recovery reason required')
  row=mail.delivery(c,did);c.execute(text('SELECT 1 FROM purchasing_v4.mail_state WHERE delivery_id=:id FOR UPDATE'),{'id':did});row=mail.delivery(c,did)
  if row['state']=='HELD':return dict(state='HELD',replayed=True)
  if row['state'] in ('ACCEPTED','DELIVERED'):return dict(state=row['state'],replayed=True)
  if row['state']=='SENDING' and not interrupted:return dict(state='SENDING')
  if row['state'] in ('SENDING','UNCERTAIN'):
   # Never send again on uncertain outcome, even with an explicit retry click.
   mail.record_attempt(c,row,'RECONCILE','UNCERTAIN',dict(detail='Original outcome unknown; await authenticated provider evidence or explicit provider status',owner_reason=reason))
   return dict(state='UNCERTAIN')
  if row['state']=='FAILED' and not retry:return dict(state='FAILED')
  if not mail.eligible(c,row):
   mail.record_attempt(c,row,'ELIGIBILITY','HELD',dict(detail='Request canceled, superseded or committed; original evidence retained'))
   return dict(state='HELD')
  body=mail.payload(c,row);lease=str(uuid4());canonical_claim(c,did);mail.record_attempt(c,row,'CLAIM','SENDING',dict(payload_sha256=mail.digest(body)),lease=lease)
 state='UNCERTAIN';pid=None;evidence={}
 try:pid=provider.send(did,body);state='ACCEPTED';evidence=dict(provider_id=pid,meaning='Accepted by Resend; recipient delivery is not yet confirmed',simulated=bool(getattr(provider,'isolated_simulator',False)))
 except Rejected:evidence=dict(detail='Provider explicitly rejected this attempt; owner may retry the retained delivery');state='FAILED'
 except Exception as error:evidence=dict(detail='External submission outcome uncertain; no automatic resend',error_type=type(error).__name__)
 with engine.begin() as c:
  current=mail.delivery(c,did);c.execute(text('SELECT 1 FROM purchasing_v4.mail_state WHERE delivery_id=:id FOR UPDATE'),{'id':did});current=mail.delivery(c,did)
  if current['state'] in ('ACCEPTED','DELIVERED'):state=current['state'];pid=current['provider_id']
  elif str(current['lease_id'])==lease:
   canonical_finish(c,did,'ACKNOWLEDGED' if state=='ACCEPTED' else 'FAILED' if state=='FAILED' else 'AMBIGUOUS',evidence)
   mail.record_attempt(c,current,'SEND',state,evidence,provider_id=pid)
 return dict(state=state,provider_id=pid)


def receive_update(c,event):
 data=event['data'];pid=data.get('email_id');tags=data.get('tags') or {};did=tags.get('purchasing_delivery') if isinstance(tags,dict) else next((x.get('value') for x in tags if x.get('name')=='purchasing_delivery'),None)
 row=c.execute(text('SELECT delivery_id::text FROM purchasing_v4.mail_state WHERE provider_id=:id'),{'id':pid}).scalar_one_or_none()
 if not row and did:
  try:row=mail.delivery(c,did)['id']
  except controls.InvalidPolicy:return dict(outcome='UNMATCHED_DELIVERY_UPDATE')
 if not row:return dict(outcome='UNMATCHED_DELIVERY_UPDATE')
 c.execute(text('SELECT 1 FROM purchasing_v4.mail_state WHERE delivery_id=:id FOR UPDATE'),{'id':row});current=mail.delivery(c,str(row));env=current['envelope']
 if not pid or data.get('from')!=env['sender'] or data.get('to')!=[env['recipient']]:return dict(outcome='SUSPICIOUS_DELIVERY_UPDATE')
 if current['provider_id'] and current['provider_id']!=pid:return dict(outcome='CONFLICTING_PROVIDER_ID')
 from .receiving_square import instant
 try:
  incoming_time=instant(event['created_at']);old_time=current['evidence'].get('provider_created_at')
  if old_time and incoming_time<instant(old_time):return dict(outcome='OLDER_STATUS_RETAINED')
 except (ValueError,KeyError,TypeError):return dict(outcome='INVALID_DELIVERY_TIMESTAMP')
 typ=event['type'];status='DELIVERED' if typ=='email.delivered' else 'FAILED' if typ in ('email.bounced','email.failed','email.complained','email.suppressed') else 'ACCEPTED' if typ=='email.sent' else None
 if not status:return dict(outcome='IGNORED_EVENT_TYPE')
 if current['state']=='DELIVERED' and status=='ACCEPTED':return dict(outcome='OLDER_STATUS_RETAINED')
 # A bounce is not proof that Resend did not accept/send: never make it resendable.
 if status=='FAILED':status='HELD'
 canonical_finish(c,str(row),'ACKNOWLEDGED',dict(provider_event=event['type'],provider_id=pid))
 mail.record_attempt(c,current,'PROVIDER_EVENT',status,dict(provider_type=typ,provider_created_at=event.get('created_at'),detail='Provider delivery evidence retained'),provider_id=pid)
 return dict(outcome=status,delivery_id=str(row))


def process_event(engine,event_id,provider):
 with engine.begin() as c:
  require_isolated(c);state=c.execute(text('SELECT * FROM purchasing_v4.mail_event_state WHERE event_id=:id FOR UPDATE'),{'id':event_id}).mappings().one()
  if state['state']=='DONE':return dict(state='DONE',replayed=True)
  if state['state']=='PROCESSING':return dict(state='PROCESSING')
  event=c.execute(text('SELECT payload FROM purchasing_v4.mail_event WHERE id=:id'),{'id':event_id}).scalar_one();c.execute(text("UPDATE purchasing_v4.mail_event_state SET state='PROCESSING',version=version+1,updated_at=now() WHERE event_id=:id"),{'id':event_id})
 try:
  if event['type']=='email.received':result=receive_message(engine,event_id,event,provider)
  else:
   with engine.begin() as c:result=receive_update(c,event)
  state='DONE'
 except Exception as error:result=dict(outcome='NEEDS_ATTENTION',error_type=type(error).__name__,detail='Processing failed; original event retained; retry original event');state='FAILED'
 with engine.begin() as c:c.execute(text('UPDATE purchasing_v4.mail_event_state SET state=:state,evidence=CAST(:e AS jsonb),version=version+1,updated_at=now() WHERE event_id=:id'),dict(id=event_id,state=state,e=worksheets.js(result)))
 return dict(state=state,result=result)


def receive_message(engine,event_id,event,provider):
 pid=event['data']['email_id'];UUID(pid);message=provider.received(pid)
 if message.get('id')!=pid:raise ValueError('Provider message identity mismatch')
 # Original MIME, including non-workbook attachments, is retained privately.
 original=provider.download(message['raw']['download_url'],64*1024*1024)
 with engine.begin() as c:
  c.execute(text('INSERT INTO purchasing_v4.mail_message(provider_id,event_id,metadata,original,sha256) VALUES (:id,:event,CAST(:meta AS jsonb),:raw,:hash) ON CONFLICT(provider_id) DO NOTHING'),dict(id=pid,event=event_id,meta=worksheets.js(message),raw=original,hash=hashlib.sha256(original).hexdigest()))
  stored=c.execute(text('SELECT sha256 FROM purchasing_v4.mail_message WHERE provider_id=:id'),{'id':pid}).scalar_one()
  if stored!=hashlib.sha256(original).hexdigest():raise ValueError('Original message identity changed')
 results=[]
 attachments=message.get('attachments',[])
 if not isinstance(attachments,list) or len(attachments)>100:raise ValueError('Attachment count exceeds bound')
 for attachment in attachments:
  aid=attachment['id'];UUID(aid);identity=str(uuid5(UUID(pid),aid))
  with engine.connect() as c:
   prior=c.execute(text('SELECT result FROM purchasing_v4.mail_import WHERE attachment_id=:id'),{'id':identity}).scalar_one_or_none()
   if prior:results.append(prior);continue
  size=attachment.get('size');content=None
  if type(size) is not int or size<0 or size>q.protocol.MAX_FILE:result=dict(outcome='OVERSIZED_ATTACHMENT',detail='Original message retained; workbook exceeds existing parser limit')
  else:
   remote=provider.attachment(pid,aid)
   if remote.get('id')!=aid or remote.get('size')!=size:raise ValueError('Attachment provenance mismatch')
   content=provider.download(remote['download_url'],q.protocol.MAX_FILE)
   if len(content)!=size:raise ValueError('Attachment size mismatch')
   result=None
  filename=str(attachment.get('filename') or 'unnamed')[:240]
  with engine.begin() as c:
   c.execute(text('INSERT INTO purchasing_v4.mail_attachment(id,provider_message_id,provider_attachment_id,filename,metadata,content,sha256) VALUES (:id,:pid,:aid,:filename,CAST(:meta AS jsonb),:content,:hash) ON CONFLICT(provider_message_id,provider_attachment_id) DO NOTHING'),dict(id=identity,pid=pid,aid=aid,filename=filename,meta=worksheets.js(attachment),content=content,hash=hashlib.sha256(content).hexdigest() if content is not None else None))
   retained=c.execute(text('SELECT sha256,filename FROM purchasing_v4.mail_attachment WHERE id=:id'),{'id':identity}).mappings().one()
   expected_hash=hashlib.sha256(content).hexdigest() if content is not None else None
   if retained['sha256']!=expected_hash or retained['filename']!=filename:raise controls.Conflict('Provider attachment identity changed; preserve the original and reconcile provenance')
  request_id=None
  if result is None:
   try:
    # Use the existing bounded container inspection before reading its manifest.
    q.protocol.bounded_package(content);book=q.protocol.load_workbook(q.protocol.BytesIO(content),data_only=False,read_only=True,keep_links=False)
    try:
     if 'Protocol' not in book or book['Protocol'].max_row>8 or book['Protocol'].max_column>2:raise ValueError('Missing quotation protocol')
     fields={row[0]:row[1] for row in book['Protocol'].iter_rows(max_row=8,max_col=2,values_only=True)};rid=fields.get('revision_id');UUID(rid)
    finally:book.close()
    with engine.connect() as c:
     route=c.execute(text('SELECT d.id::text,d.owner_id,d.vendor_authorization_id::text,d.envelope FROM purchasing_v4.mail_delivery d WHERE d.revision_id=:id'),{'id':rid}).mappings().one_or_none()
    if not route:result=dict(outcome='UNMATCHED_RESPONSE')
    else:
     request_id=route['vendor_authorization_id'];sender=parseaddr(message.get('from',''))[1].lower();recipients=[parseaddr(x)[1].lower() for x in message.get('to',[])]
     auth=message.get('authentication') or {}
     if sender!=route['envelope']['recipient'] or route['envelope']['reply_to'] not in recipients or auth.get('dmarc')!='pass':result=dict(outcome='SUSPICIOUS_RESPONSE',detail='Mailbox, sender or provider authentication does not match; owner manual upload remains available')
     else:
      imported=q.intake(engine,actor_id=route['owner_id'],request_id=request_id,filename=filename,content=content,mail_attachment_id=identity)
      validation=imported['validation'];result=dict(outcome='DUPLICATE_RESPONSE' if imported.get('duplicate_upload') else 'OUTDATED_RESPONSE' if validation.get('stale') else 'OWNER_REVIEW_REQUIRED' if validation['valid'] else 'INVALID_WORKBOOK',imported=imported,source='Authenticated Resend reply; not owner approval')
   except q.protocol.InvalidWorkbook:result=dict(outcome='INVALID_WORKBOOK',detail='Workbook rejected by the existing Phase6 container validator; original retained')
   except (ValueError,TypeError,KeyError,controls.InvalidPolicy):result=dict(outcome='INVALID_WORKBOOK' if request_id else 'INVALID_OR_UNMATCHED_WORKBOOK',detail='Protected identity or workbook container invalid; original retained')
  with engine.begin() as c:c.execute(text('INSERT INTO purchasing_v4.mail_import(attachment_id,request_id,result) VALUES (:id,:request,CAST(:result AS jsonb)) ON CONFLICT(attachment_id) DO NOTHING'),dict(id=identity,request=request_id,result=worksheets.js(result)))
  results.append(result)
 return dict(outcome='ATTACHMENTS_PROCESSED' if results else 'NO_ATTACHMENTS',message_id=pid,results=results)


def worker_engine(database_url):
 from sqlalchemy import create_engine
 from .isolation import require_isolated_url
 from .outbox import worker_engine as existing_worker
 return existing_worker(require_isolated_url(database_url))


def process_pending(engine,provider,*,limit=20):
 with engine.connect() as c:
  events=list(c.execute(text("SELECT event_id FROM purchasing_v4.mail_event_state WHERE state='PENDING' ORDER BY updated_at LIMIT :limit"),{'limit':limit}).scalars())
  deliveries=list(c.execute(text("SELECT delivery_id::text FROM purchasing_v4.mail_state WHERE state='NOT_SENT' ORDER BY updated_at LIMIT :limit"),{'limit':limit}).scalars())
 sent=[]
 for did in deliveries:
  try:sent.append(send(engine,did,provider))
  except mail.DocumentPending:sent.append(dict(state='NOT_SENT',delivery_id=did,detail='Original document generation is pending'))
  except Exception as error:
   with engine.begin() as c:
    c.execute(text('SELECT 1 FROM purchasing_v4.mail_state WHERE delivery_id=:id FOR UPDATE'),{'id':did});row=mail.delivery(c,did)
    if row['state']=='NOT_SENT':mail.record_attempt(c,row,'PREPARATION','HELD',dict(detail='Original attachment preparation needs attention; manual document recovery remains available',error_type=type(error).__name__))
   sent.append(dict(state='HELD',delivery_id=did))
 return dict(events=[process_event(engine,x,provider) for x in events],deliveries=sent)


def reconcile(engine,did,provider,*,provider_id,actor_id,reason):
 with engine.connect() as c:
  require_isolated(c)
  if not c.execute(text('SELECT 1 FROM purchasing_v4.mail_owner WHERE id=:id'),{'id':actor_id}).scalar_one_or_none() or not isinstance(reason,str) or not reason.strip():raise PermissionError('Explicit owner reconciliation required')
  row=mail.delivery(c,did)
 proof=provider.sent(provider_id);env=row['envelope'];tags=proof.get('tags') or {}
 if isinstance(tags,list):tags={x.get('name'):x.get('value') for x in tags}
 if proof.get('id')!=provider_id or proof.get('from')!=env['sender'] or proof.get('to')!=[env['recipient']] or proof.get('subject')!=env['subject'] or tags.get('purchasing_delivery')!=did:raise controls.Conflict('Provider evidence does not match this immutable purchasing delivery')
 state='DELIVERED' if proof.get('last_event')=='delivered' else 'HELD' if proof.get('last_event') in ('bounced','failed','complained','suppressed') else 'ACCEPTED'
 with engine.begin() as c:
  c.execute(text('SELECT 1 FROM purchasing_v4.mail_state WHERE delivery_id=:id FOR UPDATE'),{'id':did});current=mail.delivery(c,did)
  if current['state']=='DELIVERED':return dict(state='DELIVERED',replayed=True)
  canonical_finish(c,did,'ACKNOWLEDGED',dict(owner_id=actor_id,provider_id=provider_id,reason=reason))
  mail.record_attempt(c,current,'OWNER_PROVIDER_RECONCILIATION',state,dict(owner_id=actor_id,reason=reason,provider_id=provider_id,provider_last_event=proof.get('last_event')),provider_id=provider_id)
 return dict(state=state)


def canonical_claim(c,did):
 row=c.execute(text('SELECT * FROM purchasing_v4.integration_state WHERE command_id=:id FOR UPDATE'),{'id':did}).mappings().one()
 if row['state']=='ACKNOWLEDGED':return None
 if row['state']=='LEASED':return str(row['lease_id'])
 lease=str(uuid4())
 c.execute(text("UPDATE purchasing_v4.integration_state SET state='LEASED',version=version+1,lease_id=:lease,lease_until=now()+interval '300 seconds',updated_at=now() WHERE command_id=:id"),dict(id=did,lease=lease))
 return lease


def canonical_finish(c,did,state,evidence):
 from .outbox import finish
 lease=canonical_claim(c,did)
 if lease is not None:finish(c,did,lease,state,evidence)
