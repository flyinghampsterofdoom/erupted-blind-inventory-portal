"""Explicit independent email outbox; no scheduler or automatic live sender.

Uses append-only existing operational receipts. Provider acceptance and
receiving-server delivery are separate states. Credentials never enter workers.
"""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import base64,fcntl,hashlib,hmac,json,os,time
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool
from .contracts import EvidenceError,canonical,digest,stamp
from .qualification import records,receipt


@contextmanager
def exclusive(store, key):
    identity=hashlib.sha256(('fv-control:'+key).encode()).digest()
    if store.url.get_backend_name()=='postgresql':
        engine=create_engine(store.url,hide_parameters=True,poolclass=NullPool,isolation_level='AUTOCOMMIT',
                             connect_args={'options':'-c statement_timeout=5000 -c lock_timeout=1000'})
        try:
            with engine.connect() as c:
                lock=int.from_bytes(identity[:8],'big',signed=True)
                if not c.execute(text('SELECT pg_try_advisory_lock(:k)'),{'k':lock}).scalar_one():
                    raise EvidenceError('Independent control operation already active')
                try:yield
                finally:c.execute(text('SELECT pg_advisory_unlock(:k)'),{'k':lock})
        finally:engine.dispose()
    else:
        name=str(store.url.database)+'.'+identity.hex()+'.lock'
        fd=os.open(name,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
        finally:os.close(fd)


def queue_email(store, *, code, period_id, recipient_ref, now=None):
    if not code or len(code)>80 or len(period_id)!=64 or len(recipient_ref)!=64:
        raise EvidenceError('Bounded notification identity required')
    value={'code':code,'period_id':period_id,'recipient_ref':recipient_ref}
    ident=digest(value)
    with exclusive(store,'email:'+ident),store.engine.begin() as c:
        receipt(c,'email:'+ident,'QUEUED',value,key='queued',now=now)
    return ident


def deliver_email(store, ident, send, *, now=None):
    current=stamp(now or datetime.now(timezone.utc));op='email:'+ident
    with exclusive(store,op):
        with store.engine.connect() as c:history=records(c,op,maximum=32)
        queued=next((r for r in history if r['status']=='QUEUED'),None)
        if not queued:raise EvidenceError('Notification was not queued')
        if any(r['status'] in ('ACCEPTED','DELIVERED','BOUNCED','COMPLAINED') for r in history):
            return 'ALREADY_SUBMITTED'
        trials=[r for r in history if r['status']=='ATTEMPT']
        age=(current-stamp(queued['recorded_at'])).total_seconds()
        if not 0<=age<23*3600:raise EvidenceError('Email idempotency window requires independent reconciliation')
        if len(trials)>=3:raise EvidenceError('Email retry budget exhausted; independent alert required')
        if trials and (current-stamp(trials[-1]['recorded_at'])).total_seconds() < (60 if len(trials)==1 else 300):
            return 'RETRY_NOT_DUE'
        number=len(trials)+1
        with store.engine.begin() as c:receipt(c,op,'ATTEMPT',{'number':number},key='attempt:'+str(number),now=current)
        try:
            accepted=send(queued['value'],idempotency_key='erupted-fv-'+ident)
            if not isinstance(accepted,str) or not 1<=len(accepted)<=100:raise EvidenceError('Invalid email provider acknowledgement')
        except Exception as exc:
            with store.engine.begin() as c:
                receipt(c,op,'UNCERTAIN',{'number':number,'error_type':type(exc).__name__},key='uncertain:'+str(number),now=current)
            return 'UNCONFIRMED'
        with store.engine.begin() as c:
            receipt(c,op,'ACCEPTED',{'provider_id':accepted,'number':number},key='accepted',now=current)
        return 'ACCEPTED_NOT_DELIVERED'


class ResendTransport:
    """Prepared adapter for the existing provider; caller must authorize sending."""
    def __init__(self, *, api_key, sender, recipient, recipient_ref, approved=False):
        self.api_key=api_key;self.sender=sender;self.recipient=recipient
        self.recipient_ref=recipient_ref;self.approved=approved
    def __call__(self, message, *, idempotency_key):
        if not self.approved or not self.api_key or digest(self.recipient)!=self.recipient_ref:
            raise EvidenceError('Email delivery configuration is not commissioned')
        if message['recipient_ref']!=self.recipient_ref:raise EvidenceError('Owner destination changed')
        import httpx
        payload={'from':self.sender,'to':[self.recipient],
                 'subject':'Erupted forecasting: '+message['code'],
                 'text':'Forecasting requires Owner review. Code: '+message['code']+
                        '\nPeriod: '+message['period_id']+'\nReview Erupted Admin forecast operational status.'}
        with httpx.Client(timeout=httpx.Timeout(10,connect=3),follow_redirects=False) as client:
            response=client.post('https://api.resend.com/emails',json=payload,
                headers={'Authorization':'Bearer '+self.api_key,'Idempotency-Key':idempotency_key})
        if not 200<=response.status_code<300:raise EvidenceError('Email provider did not acknowledge submission')
        return response.json().get('id')


def record_webhook(store, ident, raw, headers, *, signing_secret, now=None):
    """Svix/Resend signature over original bytes; no public endpoint installed."""
    current=stamp(now or datetime.now(timezone.utc))
    if len(raw)>16384:raise EvidenceError('Webhook exceeds bounded receipt size')
    try:
        event_id=headers['svix-id'];ts=headers['svix-timestamp'];timestamp=int(ts)
        if abs(current.timestamp()-timestamp)>300 or not 1<=len(event_id)<=100:raise ValueError()
        if not signing_secret.startswith('whsec_'):raise ValueError()
        key=base64.b64decode(signing_secret.removeprefix('whsec_'),validate=True)
        if len(key)<16:raise ValueError()
        expected=base64.b64encode(hmac.new(key,event_id.encode()+b'.'+ts.encode()+b'.'+raw,hashlib.sha256).digest()).decode()
        if not any(hmac.compare_digest(v[3:],expected) for v in headers['svix-signature'].split() if v.startswith('v1,')):
            raise ValueError()
        event=json.loads(raw);provider_id=event['data']['email_id']
        state={'email.delivered':'DELIVERED','email.bounced':'BOUNCED','email.complained':'COMPLAINED'}[event['type']]
    except (ValueError,KeyError,TypeError):raise EvidenceError('Invalid, stale or unsupported delivery receipt') from None
    op='email:'+ident
    with exclusive(store,op),store.engine.begin() as c:
        history=records(c,op,maximum=32)
        if not any(r['status']=='ACCEPTED' and r['value']['provider_id']==provider_id for r in history):
            raise EvidenceError('Delivery event is not bound to an acknowledged forecast email')
        receipt(c,op,state,{'provider_id':provider_id,'event_id':event_id,
                          'body_sha256':hashlib.sha256(raw).hexdigest()},key='webhook:'+event_id,now=current)
    return state
