"""Purchasing correspondence evidence. No SMTP, purchasing authority or Square calls."""
from uuid import UUID,uuid4,uuid5
from datetime import datetime,timezone
import hashlib,json,re,base64,hmac
from email.utils import parseaddr
from sqlalchemy import text
from . import controls,worksheets,submission
from .identity import digest
from .isolation import require_isolated

SENDER='purchasing@orders.eruptedvapor.com'
DOMAIN='orders.eruptedvapor.com'
class DocumentPending(Exception):pass

def exists(c):return bool(c.execute(text("SELECT to_regclass('purchasing_v4.mail_delivery')")).scalar_one())

def address(value):
 if not isinstance(value,str) or len(value)>254 or '\r' in value or '\n' in value:raise controls.InvalidPolicy('One explicit vendor email address required')
 parsed=parseaddr(value)[1]
 if parsed!=value or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+',parsed):raise controls.InvalidPolicy('One explicit vendor email address required')
 return parsed.lower()

def queue_child(c,child_id,actor_id,revision_id=None):
 require_isolated(c);submission.owner(c,actor_id)
 row=c.execute(text('SELECT kind,snapshot FROM purchasing_v4.authorized_vendor WHERE id=:id'),{'id':child_id}).mappings().one();snap=row['snapshot']
 # The original immutable public delivery envelope owns the approved opt-in and recipient.
 doc=c.execute(text("SELECT payload FROM purchasing_v4.integration_command WHERE vendor_authorization_id=:id AND kind='GENERATE_DOCUMENTS'"),{'id':child_id}).scalar_one();route=doc['delivery']
 if not route['auto_enabled']:return None
 if row['kind']=='REQUEST':
  from . import quotations as q
  current=c.execute(text('SELECT revision_id::text FROM purchasing_v4.quotation_head WHERE request_id=:id'),{'id':child_id}).scalar_one_or_none()
  if not current:
   q.prepare(c,actor_id=actor_id,command_id=str(uuid5(UUID(child_id),'automatic-workbook:v1')),request_id=child_id)
   current=c.execute(text('SELECT revision_id::text FROM purchasing_v4.quotation_head WHERE request_id=:id'),{'id':child_id}).scalar_one()
  revision_id=revision_id or current
 else:revision_id=None
 did=str(uuid5(UUID(child_id),'resend-email:'+str(revision_id or 'official-po')))
 prior=c.execute(text('SELECT id::text FROM purchasing_v4.mail_delivery WHERE id=:id'),{'id':did}).scalar_one_or_none()
 if prior:return prior
 recipient=route.get('recipient');issue=None
 try:recipient=address(recipient)
 except controls.InvalidPolicy:recipient=None;issue='Vendor email missing or invalid; use manual delivery until explicitly configured'
 env=dict(schema='purchasing-email/1',provider='resend',sender=SENDER,recipient=recipient,reply_to='purchasing+'+did+'@'+DOMAIN,subject=('Quotation request ' if revision_id else 'Purchase order ')+str(doc['document']['number']),text='Please complete the attached quotation workbook and reply with the completed file attached. Your reply will be reviewed before an official purchase order is authorized.' if revision_id else 'Please find the authorized purchase order attached.',format='XLSX' if revision_id else route['format'],kind=row['kind'])
 c.execute(text("INSERT INTO purchasing_v4.integration_command(id,vendor_authorization_id,kind,payload,payload_digest) VALUES (:id,:child,'PURCHASING_EMAIL',CAST(:env AS jsonb),:digest)"),dict(id=did,child=child_id,env=worksheets.js(env),digest=digest(env)))
 c.execute(text("INSERT INTO purchasing_v4.integration_state(command_id,state) VALUES (:id,'PENDING')"),{'id':did})
 c.execute(text('INSERT INTO purchasing_v4.mail_delivery(id,vendor_authorization_id,revision_id,owner_id,envelope,payload_digest) VALUES (:id,:child,:revision,:owner,CAST(:env AS jsonb),:digest)'),dict(id=did,child=child_id,revision=revision_id,owner=actor_id,env=worksheets.js(env),digest=digest(env)))
 c.execute(text('INSERT INTO purchasing_v4.mail_state(delivery_id,state,evidence) VALUES (:id,:state,CAST(:e AS jsonb))'),dict(id=did,state='HELD' if issue else 'NOT_SENT',e=worksheets.js(dict(detail=issue) if issue else {})))
 return did

def delivery(c,did):
 row=c.execute(text('SELECT d.*,s.state,s.version,s.lease_id,s.first_attempt_at,s.provider_id,s.evidence FROM purchasing_v4.mail_delivery d JOIN purchasing_v4.mail_state s ON s.delivery_id=d.id WHERE d.id=:id'),{'id':controls.uuid(did)}).mappings().one_or_none()
 if not row:raise controls.InvalidPolicy('Unknown purchasing delivery')
 if digest(row['envelope'])!=row['payload_digest']:raise controls.Conflict('Immutable email envelope integrity failure')
 return dict(row)

def eligible(c,row):
 if row['revision_id']:
  h=c.execute(text('SELECT revision_id,state FROM purchasing_v4.quotation_head WHERE request_id=:id'),{'id':row['vendor_authorization_id']}).mappings().one()
  return h['state']=='OPEN' and h['revision_id']==row['revision_id']
 return True

def payload(c,row):
 env=row['envelope']
 if set(env)!={'schema','provider','sender','recipient','reply_to','subject','text','format','kind'} or env['sender']!=SENDER:raise controls.InvalidPolicy('Explicit external email allowlist required')
 address(env['recipient'])
 if row['revision_id']:
  f=c.execute(text('SELECT template AS content,sha256 FROM purchasing_v4.quotation_revision WHERE id=:id'),{'id':row['revision_id']}).mappings().one()
 else:
  f=c.execute(text('SELECT content,sha256 FROM purchasing_v4.vendor_artifact WHERE vendor_authorization_id=:id AND format=:format'),dict(id=row['vendor_authorization_id'],format=env['format'])).mappings().one_or_none()
  if not f:raise DocumentPending('Original authorized document is not ready; manual download and preparation remain available')
 content=bytes(f['content'])
 if hashlib.sha256(content).hexdigest()!=f['sha256']:raise controls.Conflict('Original attachment integrity failure; restore original, never regenerate from current data')
 return dict(**{'from':env['sender']},to=[env['recipient']],reply_to=env['reply_to'],subject=env['subject'],text=env['text'],attachments=[dict(filename=('quotation-' if row['revision_id'] else 'purchase-order-')+str(row['revision_id'] or row['vendor_authorization_id'])+'.'+env['format'].lower(),content=base64.b64encode(content).decode())],tags=[dict(name='purchasing_delivery',value=str(row['id']))])

def record_attempt(c,row,kind,state,evidence,*,lease=None,provider_id=None):
 evidence=dict(evidence,state_version=row['version']+1)
 c.execute(text('INSERT INTO purchasing_v4.mail_attempt(id,delivery_id,kind,outcome,evidence) VALUES (:id,:delivery,:kind,:state,CAST(:e AS jsonb))'),dict(id=str(uuid4()),delivery=row['id'],kind=kind,state=state,e=worksheets.js(evidence)))
 c.execute(text('UPDATE purchasing_v4.mail_state SET state=:state,version=version+1,lease_id=:lease,provider_id=coalesce(:provider,provider_id),first_attempt_at=CASE WHEN :state=\'SENDING\' THEN coalesce(first_attempt_at,now()) ELSE first_attempt_at END,evidence=CAST(:e AS jsonb),updated_at=now() WHERE delivery_id=:id'),dict(id=row['id'],state=state,lease=lease,provider=provider_id,e=worksheets.js(evidence)))

def verify_webhook(raw,headers,secret,*,now=None):
 if not isinstance(raw,bytes) or len(raw)>1024*1024:raise PermissionError('Inbound event exceeds its bound')
 mid=headers.get('svix-id','');stamp=headers.get('svix-timestamp','');signatures=headers.get('svix-signature','')
 try:
  if not 1<=len(mid)<=256 or not stamp.isdecimal() or abs((now or datetime.now(timezone.utc)).timestamp()-int(stamp))>300:raise ValueError()
  key=base64.b64decode(secret.removeprefix('whsec_'),validate=True);assert len(key)>=16
  expected=base64.b64encode(hmac.new(key,mid.encode()+b'.'+stamp.encode()+b'.'+raw,hashlib.sha256).digest()).decode()
  if not any(hmac.compare_digest(s,'v1,'+expected) for s in signatures.split()):raise ValueError()
  value=json.loads(raw)
  if not isinstance(value,dict) or not isinstance(value.get('data'),dict) or not isinstance(value.get('type'),str):raise ValueError()
 except (ValueError,TypeError,AssertionError):raise PermissionError('Invalid authenticated Resend event') from None
 return mid,value

def accept_event(c,raw,headers,secret):
 require_isolated(c);mid,value=verify_webhook(raw,headers,secret);hashed=hashlib.sha256(raw).hexdigest()
 c.execute(text('INSERT INTO purchasing_v4.mail_event(id,payload_digest,body,payload) VALUES (:id,:hash,:raw,CAST(:p AS jsonb)) ON CONFLICT(id) DO NOTHING'),dict(id=mid,hash=hashed,raw=raw,p=worksheets.js(value)))
 old=c.execute(text('SELECT payload_digest FROM purchasing_v4.mail_event WHERE id=:id'),{'id':mid}).scalar_one()
 if old!=hashed:raise controls.Conflict('Provider event identity replayed with different bytes')
 c.execute(text('INSERT INTO purchasing_v4.mail_event_state(event_id) VALUES (:id) ON CONFLICT DO NOTHING'),{'id':mid})
 return dict(event_id=mid,accepted=True)

def attention(c,actor_id):
 submission.owner(c,actor_id)
 deliveries=[dict(r) for r in c.execute(text('SELECT d.id::text,d.vendor_authorization_id::text,d.revision_id::text,d.envelope,s.state,s.provider_id,s.evidence,s.updated_at FROM purchasing_v4.mail_delivery d JOIN purchasing_v4.mail_state s ON s.delivery_id=d.id WHERE d.owner_id=:owner ORDER BY d.created_at DESC LIMIT 200'),{'owner':actor_id}).mappings()]
 incoming=[dict(r) for r in c.execute(text('SELECT e.id,e.payload->>\'type\' AS type,s.state,s.evidence,s.updated_at FROM purchasing_v4.mail_event e JOIN purchasing_v4.mail_event_state s ON s.event_id=e.id ORDER BY e.received_at DESC LIMIT 200')).mappings()]
 imports=[dict(r) for r in c.execute(text('SELECT i.attachment_id::text,i.request_id::text,a.filename,i.result,i.created_at FROM purchasing_v4.mail_import i JOIN purchasing_v4.mail_attachment a ON a.id=i.attachment_id ORDER BY i.created_at DESC LIMIT 200')).mappings()]
 return dict(deliveries=deliveries,events=incoming,imports=imports,retention='Indefinite; no automatic deletion',responsibility='Owner')
