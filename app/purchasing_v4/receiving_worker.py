"""Durable per-operation inventory execution; network outside database locks."""
from contextlib import contextmanager
from datetime import datetime,timezone
from uuid import uuid4
from sqlalchemy import text
from .identity import digest
from .isolation import require_isolated
from . import controls,worksheets
from .receiving_square import API_VERSION,InventoryAdapter,match_proof


@contextmanager
def transaction(engine):
    with engine.begin() as c:
        require_isolated(c);c.execute(text('SET LOCAL ROLE erupted_v4_inventory_worker'))
        yield c


def operation(c,cid,lock=False):
    row=c.execute(text('''SELECT x.*,s.state,s.version,s.attempt_id FROM purchasing_v4.inventory_command x
      JOIN purchasing_v4.inventory_operation s ON s.command_id=x.id WHERE x.id=:id'''+(' FOR UPDATE OF s' if lock else '')),{'id':controls.uuid(cid)}).mappings().one_or_none()
    if row is None:raise controls.InvalidPolicy('Unknown inventory command')
    if row['api_version']!=API_VERSION or digest(row['payload'])!=row['payload_digest']:raise controls.Conflict('Immutable inventory payload/version integrity failure')
    return dict(row)


def _state(c,row,state,evidence,attempt):
    c.execute(text('UPDATE purchasing_v4.inventory_operation SET state=:state,version=version+1,attempt_id=:attempt,evidence=CAST(:evidence AS jsonb),updated_at=now() WHERE command_id=:id AND version=:version'),dict(state=state,evidence=worksheets.js(evidence),attempt=attempt,id=row['id'],version=row['version']))


def run(engine,command_id,adapter,*,reconcile_only=False,after_remote=None,_owner_retry=False):
    if not isinstance(adapter,InventoryAdapter):raise PermissionError('Explicit version-pinned policy-controlled inventory adapter required')
    attempt=str(uuid4())
    with transaction(engine) as c:
        row=operation(c,command_id,True)
        if row['state']=='VERIFIED':return dict(command_id=command_id,state='VERIFIED',replayed=True)
        if row['state']=='IN_FLIGHT':return dict(command_id=command_id,state='IN_FLIGHT',detail='Another worker may still be executing; reconcile its retained identity')
        if row['predecessor_id'] is not None:
            prior=c.execute(text('SELECT state FROM purchasing_v4.inventory_operation WHERE command_id=:id'),{'id':row['predecessor_id']}).scalar_one()
            if prior!='VERIFIED':return dict(command_id=command_id,state=row['state'],detail='Prior inventory movement remains unresolved')
        send=(row['state'] in ('PENDING','REJECTED') or (row['state']=='AMBIGUOUS' and _owner_retry)) and not reconcile_only
        _state(c,row,'IN_FLIGHT',dict(action='SEND' if send else 'RECONCILE',immutable_command=command_id),attempt)
    # Every command/attempt is durable before the first possible remote effect.
    evidence=dict(isolated_simulator=bool(getattr(adapter.client,'isolated_simulator',False)));matched=None;state='AMBIGUOUS'
    try:
        matched=adapter.reconcile(row['payload'])
        if not matched and send:
            evidence['send_response']=adapter.send(row['payload'])
            if after_remote:after_remote()  # Adversarial process interruption, outside DB transaction.
            matched=adapter.reconcile(row['payload'])
        if matched:
            if not match_proof(row['payload'],matched):raise controls.Conflict('Remote inventory evidence does not match the immutable command')
            evidence['verified_change']=matched;state='VERIFIED'
        else:evidence['detail']='No exact adjustment proof yet; counts and missing history are not proof of success or non-execution'
    except Exception as error:evidence['error']=str(error)[:2000];matched=None
    # A BaseException interruption intentionally leaves IN_FLIGHT for controlled reconciliation.
    with transaction(engine) as c:
        current=operation(c,command_id,True)
        if current['state']=='VERIFIED':return dict(command_id=command_id,state='VERIFIED',replayed=True)
        if str(current['attempt_id'])!=attempt:raise controls.Conflict('Worker ownership changed; outcome must be reconciled')
        c.execute(text('INSERT INTO purchasing_v4.inventory_attempt(id,command_id,kind,result,evidence) VALUES (:id,:command,:kind,:result,CAST(:evidence AS jsonb))'),dict(id=attempt,command=command_id,kind='SEND' if send else 'RECONCILE',result=state,evidence=worksheets.js(evidence)))
        if matched:
            c.execute(text('INSERT INTO purchasing_v4.inventory_verification(command_id,remote_adjustment_id,evidence) VALUES (:command,:remote,CAST(:evidence AS jsonb))'),dict(command=command_id,remote=(matched['changes'][0] if 'changes' in matched else matched)['adjustment']['id'],evidence=worksheets.js(matched)))
        _state(c,current,state,evidence,attempt)
    return dict(command_id=command_id,state=state,replayed=False)


def recover_interrupted(engine,command_id,*,actor_id,reason):
    """Explicit owner intervention preserves the original payload and command ID."""
    from .submission import owner
    with engine.begin() as c:
        require_isolated(c);owner(c,actor_id);row=operation(c,command_id,True)
        if not isinstance(reason,str) or not reason.strip():raise controls.InvalidPolicy('Explain the independently confirmed worker interruption')
        if row['state']!='IN_FLIGHT':return dict(command_id=command_id,state=row['state'])
        aid=str(uuid4());evidence=dict(owner_id=actor_id,reason=reason,prior_attempt=str(row['attempt_id']),outcome='Unknown; reconcile only, never create a replacement command')
        c.execute(text("INSERT INTO purchasing_v4.inventory_attempt(id,command_id,kind,result,evidence) VALUES (:id,:command,'INTERRUPTED','AMBIGUOUS',CAST(:evidence AS jsonb))"),dict(id=aid,command=command_id,evidence=worksheets.js(evidence)))
        _state(c,row,'AMBIGUOUS',evidence,aid)
    return dict(command_id=command_id,state='AMBIGUOUS')


def run_order(engine,po_id,adapter,*,limit=100):
    """Bounded explicit work following Save & Submit; no new independent scheduler."""
    with engine.connect() as c:
        require_isolated(c)
        ids=[r[0] for r in c.execute(text("""WITH RECURSIVE selected AS (
         SELECT x.id,x.predecessor_id,x.created_at FROM purchasing_v4.inventory_command x JOIN purchasing_v4.receiving_event e ON e.id=x.event_id WHERE e.purchase_order_id=:po
         ), chain AS (
         SELECT x.id,x.predecessor_id,0 AS depth FROM selected x WHERE x.predecessor_id IS NULL
         UNION ALL SELECT x.id,x.predecessor_id,chain.depth+1 FROM selected x JOIN chain ON x.predecessor_id=chain.id
         ) SELECT chain.id::text FROM chain JOIN purchasing_v4.inventory_operation s ON s.command_id=chain.id
         LEFT JOIN purchasing_v4.inventory_operation prior ON prior.command_id=chain.predecessor_id
         WHERE s.state NOT IN ('VERIFIED','IN_FLIGHT') AND (chain.predecessor_id IS NULL OR prior.state='VERIFIED')
         ORDER BY CASE WHEN s.state='AMBIGUOUS' THEN 1 ELSE 0 END,chain.depth,chain.id LIMIT :limit"""),dict(po=po_id,limit=limit))]
    return [run(engine,cid,adapter) for cid in ids]


def retry_same_identity(engine,command_id,adapter,*,actor_id,reason):
    from .submission import owner
    with engine.begin() as c:
        require_isolated(c);owner(c,actor_id);row=operation(c,command_id,True)
        if not isinstance(reason,str) or not reason.strip():raise controls.InvalidPolicy('Explicit owner review and same-payload retry reason required')
        if row['state']=='IN_FLIGHT':raise controls.Conflict('Confirm the interrupted worker before retrying its retained inventory identity')
        if row['state']=='VERIFIED':return dict(command_id=command_id,state='VERIFIED',replayed=True)
        evidence=dict(owner_id=actor_id,reason=reason,payload_digest=row['payload_digest'],idempotency_key=row['payload']['idempotency_key'],meaning='Retry only identical payload and original key; never infer absence from counts/history')
        c.execute(text("INSERT INTO purchasing_v4.inventory_attempt(id,command_id,kind,result,evidence) VALUES (:id,:command,'RECONCILE','OWNER_RETRY_SAME_IDENTITY',CAST(:evidence AS jsonb))"),dict(id=str(uuid4()),command=command_id,evidence=worksheets.js(evidence)))
    return run(engine,command_id,adapter,_owner_retry=True)


def process_order(engine,po_id,adapter,*,batch_size=100,max_operations=30000):
    """Drain the receipt-triggered bounded work; unresolved lines do not starve others."""
    completed=0;attempted=0
    while attempted<max_operations:
        results=run_order(engine,po_id,adapter,limit=min(batch_size,max_operations-attempted))
        if not results:break
        attempted+=len(results);progress=sum(x['state']=='VERIFIED' for x in results);completed+=progress
        if not progress:break
    return dict(attempted=attempted,verified=completed,remaining_require_reconciliation=attempted>completed)
