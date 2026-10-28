"""Narrow worker: render immutable public documents; sandbox delivery only.

Every worker transaction assumes a least-privilege role without PO, policy,
worksheet, inventory, authorization or finance write rights. No real provider is
configured or called. Ambiguous outcomes retain the same immutable command.
"""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from uuid import UUID,uuid4,uuid5
import hashlib
from sqlalchemy import text,create_engine
from .isolation import require_isolated_url
from .identity import digest
from .isolation import require_isolated
from . import worksheets
from .vendor_documents import render,validate_projection


def worker_engine(database_url):
    url=require_isolated_url(database_url).set(username='erupted_v4_outbox_worker',password=None)
    return create_engine(url,pool_size=2,max_overflow=1)


@contextmanager
def transaction(engine):
    with engine.begin() as c:
        require_isolated(c)
        if c.execute(text("SELECT current_user='erupted_v4_outbox_worker' AND session_user='erupted_v4_outbox_worker'")).scalar_one() is not True:raise PermissionError('Outbox workers require their unprivileged login; owner/PO-writer connections are rejected')
        yield c


def claim(engine,command_id,kind,*,retry_ambiguous=False,lease_seconds=300,reconciliation_digest=None):
    with transaction(engine) as c:
        command=c.execute(text('SELECT * FROM purchasing_v4.integration_command WHERE id=:id AND kind=:kind'),dict(id=command_id,kind=kind)).mappings().one()
        if digest(command['payload'])!=command['payload_digest']:raise ValueError('Immutable command payload hash mismatch')
        state=c.execute(text('SELECT * FROM purchasing_v4.integration_state WHERE command_id=:id FOR UPDATE'),{'id':command_id}).mappings().one()
        now=datetime.now(timezone.utc)
        if state['state']=='ACKNOWLEDGED':return dict(command),None
        if kind=='DELIVER_DOCUMENTS' and c.execute(text("SELECT to_regclass('purchasing_v4.quotation_delivery_eligibility')")).scalar_one():
            eligibility=c.execute(text('SELECT state,number FROM purchasing_v4.quotation_delivery_eligibility WHERE request_id=:id'),{'id':command['vendor_authorization_id']}).mappings().one_or_none()
            if eligibility and (eligibility['state']!='OPEN' or eligibility['number']!=1) and reconciliation_digest!=command['payload_digest']:raise ValueError('Original request was reissued, canceled or committed; retain its prior delivery evidence and use the current reviewed revision')
        if state['state']=='LEASED' and state['lease_until']>now:raise ValueError('Command is already leased')
        if kind=='DELIVER_DOCUMENTS' and (state['state']=='AMBIGUOUS' or state['state']=='LEASED') and not retry_ambiguous:
            if state['state']=='LEASED':finish(c,command_id,str(state['lease_id']),'AMBIGUOUS',dict(meaning='Delivery lease expired without an acknowledgment; external outcome needs reconciliation',simulated=True))
            return dict(command),None
        lease=str(uuid4());c.execute(text("UPDATE purchasing_v4.integration_state SET state='LEASED',lease_id=:lease,lease_until=:until,version=version+1,updated_at=now() WHERE command_id=:id"),dict(id=command_id,lease=lease,until=now+timedelta(seconds=lease_seconds)))
        return dict(command),lease


def finish(c,command_id,lease,outcome,evidence):
    evidence=dict(evidence,lease_id=lease)
    row=c.execute(text('SELECT lease_id,state FROM purchasing_v4.integration_state WHERE command_id=:id FOR UPDATE'),{'id':command_id}).mappings().one()
    if str(row['lease_id'])!=lease or row['state']!='LEASED':raise ValueError('Worker lease changed; retain command for reconciliation')
    c.execute(text('INSERT INTO purchasing_v4.integration_attempt(id,command_id,outcome,evidence) VALUES (:id,:command,:outcome,CAST(:e AS jsonb))'),dict(id=str(uuid4()),command=command_id,outcome=outcome,e=worksheets.js(evidence)))
    c.execute(text('UPDATE purchasing_v4.integration_state SET state=:state,evidence=CAST(:e AS jsonb),version=version+1,lease_until=NULL,updated_at=now() WHERE command_id=:id'),dict(id=command_id,state=outcome,e=worksheets.js(evidence)))


def generate_documents(engine,command_id):
    command,lease=claim(engine,command_id,'GENERATE_DOCUMENTS')
    if lease is None:
        with transaction(engine) as c:
            originals=[dict(r) for r in c.execute(text('SELECT id::text,format,sha256,content FROM purchasing_v4.vendor_artifact WHERE command_id=:id ORDER BY format'),{'id':command_id}).mappings()]
            if {r['format'] for r in originals}!={'PDF','XLSX'} or any(hashlib.sha256(bytes(r['content'])).hexdigest()!=r['sha256'] for r in originals):raise ValueError('Original vendor files are unavailable or corrupt; restore/reconcile originals, never replace them using current data')
            return [{k:v for k,v in r.items() if k!='content'} for r in originals]
    envelope=command['payload']
    if set(envelope)!={'document','delivery'} or set(envelope['delivery'])!={'auto_enabled','recipient','format'} or type(envelope['delivery']['auto_enabled']) is not bool or envelope['delivery']['format'] not in ('PDF','XLSX'):raise ValueError('Invalid immutable document/delivery envelope')
    projection=envelope['document'];route=envelope['delivery'];validate_projection(projection);files=render(projection);artifacts=[]
    with transaction(engine) as c:
        for format,content in files.items():
            aid=str(uuid5(UUID(command_id),format));sha=hashlib.sha256(content).hexdigest()
            old=c.execute(text('SELECT sha256,content FROM purchasing_v4.vendor_artifact WHERE id=:id'),{'id':aid}).mappings().one_or_none()
            if old:
                if old['sha256']!=sha or bytes(old['content'])!=content:raise ValueError('Existing original differs; restore original bytes, never replace it')
            else:c.execute(text('INSERT INTO purchasing_v4.vendor_artifact(id,vendor_authorization_id,command_id,format,sha256,content,public_projection) VALUES (:id,:child,:command,:format,:sha,:content,CAST(:p AS jsonb))'),dict(id=aid,child=command['vendor_authorization_id'],command=command_id,format=format,sha=sha,content=content,p=worksheets.js(projection)))
            artifacts.append(dict(id=aid,format=format,sha256=sha))
        child=str(command['vendor_authorization_id']);delivery=None
        if route['auto_enabled']:
            delivery=str(uuid5(UUID(child),'delivery:v1'));payload=dict(schema='v4-delivery/1',recipient=route['recipient'],document_number=projection['number'],artifacts=[f for f in artifacts if f['format']==route['format']],transport='SANDBOX_ONLY')
            prior=c.execute(text('SELECT payload_digest FROM purchasing_v4.integration_command WHERE id=:id'),{'id':delivery}).scalar_one_or_none()
            if prior is not None and prior!=digest(payload):raise ValueError('Delivery identity already has another immutable payload')
            if prior is None:
                c.execute(text("INSERT INTO purchasing_v4.integration_command(id,vendor_authorization_id,kind,payload,payload_digest) VALUES (:id,:child,'DELIVER_DOCUMENTS',CAST(:p AS jsonb),:sha)"),dict(id=delivery,child=child,p=worksheets.js(payload),sha=digest(payload)))
                c.execute(text("INSERT INTO purchasing_v4.integration_state(command_id,state) VALUES (:id,'PENDING')"),{'id':delivery})
        finish(c,command_id,lease,'ACKNOWLEDGED',dict(artifacts=artifacts,delivery_command_id=delivery,meaning='Vendor files ready; automatic delivery '+('queued separately, not performed' if delivery else 'disabled; manual download only')))

    return artifacts


class SandboxProvider:
    """Deterministic fake; no network implementation or credentials."""
    def __init__(self):self.accepted={};self.lose_ack=False;self.calls=0
    def send(self,command_id,payload):
        self.calls+=1
        prior=self.accepted.get(command_id)
        if prior and prior['payload_digest']!=digest(payload):raise ValueError('Provider rejected changed payload under immutable command identity')
        result=prior or dict(provider_reference='SANDBOX:'+command_id,payload_digest=digest(payload),simulated=True,accepted=True)
        self.accepted[command_id]=result
        if self.lose_ack:raise TimeoutError('Simulated accepted response was lost')
        return result


def deliver_sandbox(engine,command_id,provider,*,reconcile=False):
    if type(provider) is not SandboxProvider:raise PermissionError('Only the isolated fake provider is enabled; real delivery is unauthorized')
    proof=provider.accepted.get(command_id,{}).get('payload_digest') if reconcile else None
    command,lease=claim(engine,command_id,'DELIVER_DOCUMENTS',retry_ambiguous=reconcile,reconciliation_digest=proof)
    if lease is None:return dict(command_id=command_id,processed=False)
    try:result=provider.send(command_id,command['payload'])
    except Exception:
        with transaction(engine) as c:finish(c,command_id,lease,'AMBIGUOUS',dict(meaning='External acceptance could not be proved; reconcile/retry only this same command',simulated=True))
        return dict(command_id=command_id,state='AMBIGUOUS')
    with transaction(engine) as c:finish(c,command_id,lease,'ACKNOWLEDGED',result)
    return dict(command_id=command_id,state='ACKNOWLEDGED',simulated=True)
