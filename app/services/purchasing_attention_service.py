"""Small owner-input boundary for current Purchasing; no historical or money writes."""
import hashlib
import json
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from sqlalchemy import select,text
from app.models import (PurchasingOwnerDecision as Decision,FundingAccount,PaymentMethod,Vendor,
    VendorSkuConfig,PurchaseOrder,PurchaseOrderLine,OrderPayment,AuditLog,PurchasingProductAuthority)
from app.services.purchase_order_admin_service import authorize_purchasing_owner
from app.services.audit_service import log_audit
from app.config import settings

class Conflict(ValueError):pass

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
def decision(db,kind,key):
    snapshot=db.info.get('purchasing_decision_snapshot')
    return snapshot.get((kind,str(key))) if snapshot is not None else db.get(Decision,(kind,str(key)))
def card_id(db,role,default):
    row=decision(db,'CARD',role)
    return row.value['account_id'] if row else default

def valid_accounts(db):
    result=[]
    for a in db.scalars(select(FundingAccount).where(FundingAccount.is_active.is_(True)).order_by(FundingAccount.id)):
        method=db.get(PaymentMethod,a.payment_method_id) if a.payment_method_id else None
        if a.account_type=='CONSIGNMENT':
            vendor=db.get(Vendor,a.vendor_id)
            if not vendor or not vendor.active or vendor.square_vendor_id not in {settings.purchasing_big_square_vendor_id,settings.purchasing_streamline_square_vendor_id}:continue
        if a.account_type=='CONSIGNMENT' or (a.account_type=='CREDIT_CARD' and method and method.is_active and method.category=='CREDIT_CARD'):
            result.append(a)
    return result

def financing_choice(db,vid,vendor_ids,categories,*,prospective=False):
    """Owner SKU choice is independent of received financing; PO records stay intact."""
    from app.services.purchasing_classification_service import ClassificationContext
    context=ClassificationContext(db);event=context.owner_event(vid,datetime.now(timezone.utc))
    if not event:return None,None
    category=event['category']
    if category=='CREDIT_CARD_POOL':
        row=decision(db,'FINANCING',vid)
        account=db.get(FundingAccount,row.value.get('account_id')) if row and row.value.get('account_id') else None
        return (account if account and account.account_type=='CREDIT_CARD' and account.is_active else None),None
    wanted=settings.purchasing_streamline_square_vendor_id if category=='STREAMLINE' else settings.purchasing_big_square_vendor_id if category=='BIG' else None
    matches=[a for a in valid_accounts(db) if a.account_type=='CONSIGNMENT' and context.vendors.get(a.vendor_id) and context.vendors[a.vendor_id].square_vendor_id==wanted] if wanted else []
    return (matches[0],None) if len(matches)==1 else (None,'Configure the owner-selected consignment account')


def cost_choice(db,vid,vendor_id,basis):
    row=decision(db,'COST',f'{vid}:{vendor_id}')
    if not row:return None,None
    if basis.get('source')=='configured' and row.value.get('basis_data',{}).get('source')=='received':return None,None
    if row.value['basis']!=digest(basis):return None,'Authoritative cost evidence changed; review the saved current-cost decision'
    return Decimal(row.value['unit_cost']),None

def facts(db,gateway=None):
    from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
    from app.services import v2_funding_reports_service as funding
    provided=gateway is not None;gateway=gateway or SquareOrderingReadGateway()
    products=gateway.fetch_active_catalog()
    category_names=getattr(gateway,'category_names',{})
    if not provided:
        from app.sync_square_campaigns import SquareClient,fetch_categories
        category_names=fetch_categories(SquareClient(settings.square_api_base_url.rstrip('/'),
            {'Authorization':'Bearer '+(settings.square_access_token or ''),'Content-Type':'application/json'},settings.square_timeout_seconds))
    vendors={v.id:v for v in db.scalars(select(Vendor).where(Vendor.active.is_(True)))}
    canonical={settings.purchasing_big_square_vendor_id:'big',settings.purchasing_streamline_square_vendor_id:'streamline'}
    lots={}
    for po,payment,line in db.execute(select(PurchaseOrder,OrderPayment,PurchaseOrderLine).join(
        OrderPayment,OrderPayment.purchase_order_id==PurchaseOrder.id).join(PurchaseOrderLine,
        PurchaseOrderLine.purchase_order_id==PurchaseOrder.id).where(PurchaseOrder.status.in_(funding.QUALIFYING_ORDER_STATUSES),
        PurchaseOrderLine.removed.is_(False),PurchaseOrderLine.received_qty_total>0)):
        v=vendors.get(payment.vendor_id)
        if payment.financial_treatment=='REPLENISHMENT' and payment.payment_category_snapshot=='CONSIGNMENT':
            role=canonical.get(v.square_vendor_id,'unknown') if v else 'unknown'
        elif payment.financial_treatment=='INVOICE' and payment.payment_category_snapshot in {'TERMS','CREDIT_CARD','WIRE','DEBIT_CARD'}:role='other'
        else:role='unknown'
        lots.setdefault(line.variation_id,[]).append({'role':role,'po_id':po.id,'line_id':line.id,'payment_id':payment.id,
            'vendor_id':payment.vendor_id,'treatment':payment.financial_treatment,'category':payment.payment_category_snapshot,
            'unit_cost':str(line.unit_cost) if line.unit_cost is not None else None,
            'ordered_at':str(po.ordered_at or po.submitted_at or po.created_at)})
    mappings={}
    for m in db.scalars(select(VendorSkuConfig).where(VendorSkuConfig.active.is_(True))):
        mappings.setdefault((m.square_variation_id,m.vendor_id),[]).append(m)
    return products,lots,mappings,vendors,category_names

def workspace(db,*,actor_id,gateway=None):
    from app.services.purchasing_session_service import authorize
    authorize(db,actor_id)
    products,lots,mappings,vendors,names=facts(db,gateway)
    accounts=valid_accounts(db);rows=[];resolved=[]
    canonical={settings.purchasing_big_square_vendor_id:'big',settings.purchasing_streamline_square_vendor_id:'streamline'}
    def add(kind,key,product,reason,evidence_basis,*,target=None,**extra):
        saved=decision(db,kind,key)
        (rows if target is None else target).append({'kind':kind,'key':str(key),'sku':product.sku if product else str(key),
            'product':product.item_name if product else 'Funding configuration','variation_id':product.variation_id if product else '',
            'vendor_ids':list(product.vendor_ids) if product else [],'big':bool(product and settings.purchasing_big_square_vendor_id in product.vendor_ids),
            'streamline':bool(product and settings.purchasing_streamline_square_vendor_id in product.vendor_ids),
            'reason':reason,'version':saved.version if saved else 0,'token':digest(evidence_basis),
            'current_assignment':(next((a.display_name for a in accounts if a.id==saved.value.get('account_id')),
                'Saved current cost' if kind=='COST' else 'Saved account unavailable') if saved else 'Unresolved'),
            'saved_value':saved.value if saved else None,**extra})
    # Ordinary products are classified automatically. SKU changes use the dated,
    # audited classification editor; received lots are not a financial queue.
    # Cost exceptions use existing managed SKU mappings, plus missing received costs.
    cost_targets={key:('configured',rows_) for key,rows_ in mappings.items() if any(m.unit_cost is None or m.unit_cost<0 for m in rows_) or len({m.unit_cost for m in rows_})>1}
    for vid,assigned in lots.items():
        if len({r['role'] for r in assigned})==1 and assigned[0]['role'] in {'big','streamline'}:
            latest=max(assigned,key=lambda r:(r['ordered_at'],r['line_id']))
            if latest['unit_cost'] is None or Decimal(latest['unit_cost'])<0:
                cost_targets[(vid,latest['vendor_id'])]=('received',latest)
    for saved in db.scalars(select(Decision).where(Decision.kind=='COST')):
        vid=saved.subject_key.rsplit(':',1)[0];vendor_id=saved.value['vendor_id']
        if saved.value.get('basis_data',{}).get('source')=='configured':
            cost_targets.setdefault((vid,vendor_id),('configured',mappings.get((vid,vendor_id),[])))
    for (vid,vendor_id),(source,raw) in cost_targets.items():
        product=products.get(vid)
        if not product:continue
        from app.services.purchasing_product_policy_service import no_replenishment,reward_marker
        if reward_marker(vid) or no_replenishment(vid,product=product,names=names):continue
        basis=({'source':'received','line_id':raw['line_id'],'cost':raw['unit_cost']} if source=='received' else
            {'source':'configured','rows':sorted([(m.id,str(m.unit_cost) if m.unit_cost is not None else None) for m in raw])})
        cost,problem=cost_choice(db,vid,vendor_id,basis)
        if cost is not None:
            add('COST',f'{vid}:{vendor_id}',product,'Saved current cost',basis,target=resolved,
                vendor_id=vendor_id,basis=basis,evidence=[],suggested_account_id=None)
        else:
            vendor_name=vendors[vendor_id].name if vendor_id in vendors else f'Vendor #{vendor_id}'
            reason=(problem or 'Missing or conflicting verified unit COGS')+f' for {vendor_name}.'
            if source=='received':reason+=f" Establish current valuation cost only; historical PO line #{raw['line_id']} remains unchanged."
            add('COST',f'{vid}:{vendor_id}',product,reason,basis,
                vendor_id=vendor_id,cost_vendor_name=vendor_name,basis=basis,evidence=[],suggested_account_id=None)
    for role,default in [('card1',settings.purchasing_card1_account_id),('card2',settings.purchasing_card2_account_id)]:
        aid=card_id(db,role,default);other='card2' if role=='card1' else 'card1'
        otherid=card_id(db,other,getattr(settings,'purchasing_'+other+'_account_id'))
        if not any(a.id==aid and a.account_type=='CREDIT_CARD' for a in accounts) or aid==otherid:
            add('CARD',role,None,f'{role}: select a distinct existing credit-card account; missing accounts must be configured separately',
                {'accounts':[(a.id,a.account_type,a.payment_method_id) for a in accounts],'other':otherid},evidence=[],suggested_account_id=None)
    result = {'rows':rows,'resolved_rows':resolved,'sku_count':len({r['variation_id'] for r in rows if r['variation_id']}),
        'accounts':[{'id':a.id,'name':a.display_name,'type':a.account_type} for a in accounts],
        'configuration_count':sum(r['kind']=='CARD' for r in rows)}
    from app.services.purchasing_attention_summary import remember
    remember(db, actor_id, result)
    return result

def save(db,*,actor_id,request_key,changes,gateway=None):
    from app.services.purchasing_session_service import authorize
    authorize(db,actor_id)
    if not isinstance(request_key,str) or not request_key or len(request_key)>128 or not isinstance(changes,list) or not changes:raise ValueError('Select decisions and a stable request identity')
    payload_hash=digest(changes)
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':f'purchasing-exceptions-request:{actor_id}:{request_key}'})
    prior=db.scalar(select(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS',AuditLog.actor_principal_id==actor_id,
        AuditLog.meta['request_key'].as_string()==request_key))
    if prior:
        if prior.meta['payload_hash']!=payload_hash:raise Conflict('Retry key already used for different decisions')
        return prior.meta['result']
    keys=[(c['kind'],str(c['key'])) for c in changes]
    if len(set(keys))!=len(keys):raise ValueError('Select each decision once')
    # All card-role edits serialize together, including previously absent rows.
    locks=sorted({('CARD','roles') if k=='CARD' else (k,v) for k,v in keys})
    for kind,key in locks:db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':f'purchasing-exception:{kind}:{key}'})
    # Re-read after the locks: a previously loaded identity must not hide another
    # request's committed decision/version.
    db.expire_all()
    view=workspace(db,actor_id=actor_id,gateway=gateway);available={(r['kind'],r['key']):r for r in view['rows']+view['resolved_rows']}
    account_by_id={a.id:a for a in valid_accounts(db)};audit=[]
    for change,(kind,key) in zip(changes,keys):
        issue=available.get((kind,key));row=decision(db,kind,key)
        if not issue or issue['version']!=change.get('version') or issue['token']!=change.get('token'):raise Conflict('Evidence or owner decision changed; reload Needs Attention before saving')
        if kind in {'FINANCING','CARD'}:
            aid=change.get('account_id');account=account_by_id.get(aid)
            if not account or (kind=='CARD' and account.account_type!='CREDIT_CARD'):raise ValueError('Select a valid existing funding account')
            if kind=='CARD':
                other='card2' if key=='card1' else 'card1';otherid=card_id(db,other,getattr(settings,'purchasing_'+other+'_account_id'))
                proposed=next((c.get('account_id') for c in changes if c['kind']=='CARD' and c['key']==other),otherid)
                if aid==proposed:raise ValueError('Credit Card 1 and Credit Card 2 must be distinct accounts')
            value={'account_id':aid,'vendor_ids':issue['vendor_ids'],'scope':'CURRENT_IF_UNAMBIGUOUS_AND_FUTURE'}
        elif kind=='COST':
            try:cost=Decimal(str(change['unit_cost']))
            except (InvalidOperation,KeyError,ValueError):raise ValueError('Enter a verified unit cost')
            if not cost.is_finite() or cost<0 or cost>Decimal('99999999.9999') or cost!=cost.quantize(Decimal('.0001')):raise ValueError('Enter a nonnegative verified unit cost with at most four decimals')
            value={'unit_cost':str(cost),'vendor_id':issue['vendor_id'],'basis':digest(issue['basis']),'basis_data':issue['basis'],'scope':'CURRENT_AND_FUTURE_COST_ONLY'}
        else:raise ValueError('Unsupported decision')
        before={'value':row.value,'version':row.version} if row else None
        if row:row.value=value;row.version+=1;row.actor_id=actor_id;row.updated_at=datetime.now(timezone.utc)
        else:row=Decision(kind=kind,subject_key=key,value=value,version=1,actor_id=actor_id,updated_at=datetime.now(timezone.utc));db.add(row)
        audit.append({'kind':kind,'key':key,'before':before,'after':value,'version':row.version,'evidence_token':issue['token']})
    db.flush();result={'saved':True,'count':len(changes),'request_key':request_key}
    log_audit(db,actor_principal_id=actor_id,action='PURCHASING_EXCEPTION_DECISIONS',session_id=None,ip=None,
        metadata={'request_key':request_key,'payload_hash':payload_hash,'decisions':audit,'result':result})
    db.flush();return result


def snapshot_decisions(function):
    """Operation-local bulk lookup, including absent decisions; never a global cache."""
    from functools import wraps
    @wraps(function)
    def call(db,*args,**kwargs):
        if 'purchasing_decision_snapshot' in db.info:return function(db,*args,**kwargs)
        db.info['purchasing_decision_snapshot']={(r.kind,r.subject_key):r for r in db.scalars(select(Decision))}
        try:return function(db,*args,**kwargs)
        finally:db.info.pop('purchasing_decision_snapshot',None)
    return call
