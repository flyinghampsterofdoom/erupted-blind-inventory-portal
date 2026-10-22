"""Atomic owner draft saves. No purchase orders, Square writes, or payments."""
import hashlib
import json
from types import SimpleNamespace
from sqlalchemy import select
from app.models import (PurchasingDraftBatch,PurchasingSessionLine,PurchasingSessionGroup,Vendor)
from app.services import purchasing_session_service as purchasing
from app.services.purchasing_attention_service import snapshot_decisions
from app.services.purchasing_metadata_cache import revision_metadata


class Conflict(ValueError):pass


def validate_operation(op):
    if not isinstance(op,dict) or set(op)-{'kind','line_id','supplier_id','changes'}:
        raise ValueError('Invalid draft operation')
    kind=op.get('kind');changes=op.get('changes',{})
    if not isinstance(changes,dict):raise ValueError('Invalid draft fields')
    if kind=='line':
        if set(changes)-{'quantity','allocations','supplier_id','cost','acknowledge','accept_proposal'}:
            raise ValueError('Unsupported draft field')
        for key in ('quantity','supplier_id'):
            if key in changes and type(changes[key]) is not int:raise ValueError('Enter a whole number')
        for key in ('acknowledge','accept_proposal'):
            if key in changes and type(changes[key]) is not bool:raise ValueError('Use a true or false review decision')
        if 'allocations' in changes and (not isinstance(changes['allocations'],dict) or any(type(v) is not int for v in changes['allocations'].values())):
            raise ValueError('Allocations must contain whole numbers')
        if 'cost' in changes and (changes['cost'] is None or str(changes['cost']).strip()==''):
            raise ValueError('Enter a nonnegative purchase cost')
    elif kind in ('remove','undo'):
        if changes:raise ValueError('Removal has no editable fields')
    elif kind=='account':
        if set(changes)!={'account_id'}:raise ValueError('Select an intended payment account')
    else:raise ValueError('Unsupported draft action')
    key='supplier_id' if kind=='account' else 'line_id'
    if type(op.get(key)) is not int or op[key]<=0:raise ValueError('A saved product or supplier identity is required')


@snapshot_decisions
def save(db,*,session_id,actor_id,version,request_key,operations):
    if type(version) is not int:raise ValueError('A saved draft version is required')
    request_key=purchasing._key(request_key)
    if not isinstance(operations,list) or not 1<=len(operations)<=1000:raise ValueError('Save between 1 and 1,000 draft changes together')
    for op in operations:validate_operation(op)
    digest=hashlib.sha256(json.dumps({'version':version,'operations':operations},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    session=purchasing._session(db,session_id,actor_id)
    previous=db.scalar(select(PurchasingDraftBatch).where(PurchasingDraftBatch.session_id==session_id,PurchasingDraftBatch.request_key==request_key))
    if previous:
        if previous.payload_sha256!=digest:raise Conflict('This save identity was already used for different decisions. Review the saved draft.')
        return {**previous.result,'replayed':True}
    if session.version!=version:raise Conflict('Another save changed this draft. Compare your pending decisions before reapplying them.')
    if session.status!='ACTIVE' and any(op['kind']!='account' for op in operations):raise ValueError('This draft is complete; open its purchase orders')
    ids=[op['line_id'] for op in operations if op['kind']!='account']
    lines={line.id:line for line in db.scalars(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session_id,PurchasingSessionLine.id.in_(ids)))}
    if set(ids)!=set(lines):raise LookupError('Saved product not found in this draft')
    groups={g.supplier_id:g for g in purchasing._groups(db,session)}
    supplier_ids={op['changes'].get('supplier_id',lines[op['line_id']].supplier_id) for op in operations if op['kind']=='line'}
    active={v.id for v in db.scalars(select(Vendor).where(Vendor.id.in_(supplier_ids),Vendor.active.is_(True)))}
    if supplier_ids-active:raise ValueError('Select an active purchasing supplier')
    for sid in supplier_ids:
        if sid in groups and groups[sid].status=='DISCARDED':groups[sid].status='OPEN'
    configured,historical=purchasing._cost_context(db,[line.variation_id for line in lines.values()])
    stores=revision_metadata(db,session)[0]
    context={'session':session,'lines':lines,'groups':groups,'configured':configured,'historical':historical,'revision':SimpleNamespace(evidence={'stores':stores})}
    # Account confirmation deliberately follows transfers so it covers the final group.
    for op in sorted(operations,key=lambda op:op['kind']=='account'):
        kind=op['kind'];changes=op.get('changes',{})
        if kind=='line':
            purchasing.update_line(db,session_id=session_id,actor_id=actor_id,line_id=op['line_id'],_context=context,**changes)
        elif kind in ('remove','undo'):
            line=lines[op['line_id']];group=groups.get(line.supplier_id)
            if group and group.status=='CONVERTED':raise ValueError('Edit converted products in the PO editor')
            line.removed=kind=='remove';line.updated_at=purchasing.now()
            if kind=='undo' and group and group.status=='DISCARDED':group.status='OPEN'
            purchasing._audit(db,actor_id,'LINE_REMOVED' if line.removed else 'LINE_RESTORED',purchasing_session_id=session_id,line_id=line.id)
        else:
            # Existing account safeguard also handles converted draft POs.
            purchasing.set_account(db,session_id=session_id,supplier_id=op['supplier_id'],actor_id=actor_id,account_id=int(changes['account_id'] or 0) or None)
    db.flush()
    occupied=set(db.scalars(select(PurchasingSessionLine.supplier_id).where(PurchasingSessionLine.session_id==session_id)))
    for sid,g in groups.items():
        if g.status=='OPEN' and sid not in occupied:g.status='DISCARDED'
    # One externally visible version for the whole transaction, even if account helpers ran.
    session.version=version
    purchasing._complete(db,session)
    result={'saved':True,'version':session.version,'status':session.status,'changes_saved':len(operations),'request_key':request_key}
    db.add(PurchasingDraftBatch(session_id=session_id,request_key=request_key,payload_sha256=digest,result=result,actor_id=actor_id))
    purchasing._audit(db,actor_id,'DRAFT_SAVED',purchasing_session_id=session_id,request_key=request_key,changes=len(operations),version=session.version)
    db.flush();return result
