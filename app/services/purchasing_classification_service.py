"""Sales financing policy: owner > Streamline > Big > combined card pool.

Square establishes sale quantities/dates. No PO, lot, receipt, stock quantity,
card limit, or supplier selection is an eligibility or classification input.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta, timezone
from sqlalchemy import select, text
from app.config import settings
from app.models import (PurchasingProductAuthority, Vendor, VendorSkuConfig,
    VendorVariationAssignment, VendorVariationCost, FundingAccount, FundingSkuMapping, PurchasingOwnerDecision,
    OrderingCatalogIdentity, ConsignmentSaleFact, AuditLog)
from app.services.purchasing_evidence_service import utc, BUSINESS_TIMEZONE

CATEGORIES=('STREAMLINE','BIG','CREDIT_CARD_POOL')
LABELS={'STREAMLINE':'Streamline','BIG':'Big Wholesale','CREDIT_CARD_POOL':'Credit Card'}
LOCK='purchasing-sales-classification'

@dataclass(frozen=True)
class CogsClassification:
    category: str
    reason: str
    square_vendor_ids: tuple[str,...]=()


def square_vendor_ids(variation_data):
    infos=variation_data.get('item_variation_vendor_infos') or variation_data.get('item_variation_vendor_info_data') or []
    return tuple(sorted({str((x.get('item_variation_vendor_info_data',x)).get('vendor_id')).strip()
        for x in infos if isinstance(x,dict) and isinstance(x.get('item_variation_vendor_info_data',x),dict)
        and x.get('item_variation_vendor_info_data',x).get('vendor_id')}))


def account_category(account,vendors):
    if account is None:return 'UNKNOWN'
    if account.account_type=='CREDIT_CARD':return 'CREDIT_CARD_POOL'
    vendor=vendors.get(account.vendor_id)
    if vendor and vendor.square_vendor_id==settings.purchasing_streamline_square_vendor_id:return 'STREAMLINE'
    if vendor and vendor.square_vendor_id==settings.purchasing_big_square_vendor_id:return 'BIG'
    return 'UNKNOWN'


class ClassificationContext:
    """One operation's persisted associations; no remote reads or global cache."""
    def __init__(self,db):
        self.vendors={v.id:v for v in db.scalars(select(Vendor))}
        self.accounts={a.id:a for a in db.scalars(select(FundingAccount))}
        self.authorities={a.square_variation_id:a for a in db.scalars(select(PurchasingProductAuthority))}
        self.decisions={a.subject_key:a for a in db.scalars(select(PurchasingOwnerDecision).where(PurchasingOwnerDecision.kind=='FINANCING'))}
        self.mappings=defaultdict(list)
        self.historical_identities={i.square_variation_id:i for i in db.scalars(select(OrderingCatalogIdentity))}
        self.identities={vid:i for vid,i in self.historical_identities.items() if not i.square_is_deleted}
        self.by_sku=defaultdict(set)
        for vid,i in self.identities.items():
            if i.sku:self.by_sku[self.sku_key(i.sku)].add(vid)
        for m in db.scalars(select(VendorSkuConfig).where(VendorSkuConfig.active.is_(True))):
            vendor=self.vendors.get(m.vendor_id)
            if not vendor or not vendor.active:continue
            vid=str(m.square_variation_id or '').strip()
            if not vid:
                aliases=self.by_sku[self.sku_key(m.sku)]
                if len(aliases)==1:vid=next(iter(aliases))
            if vid:self.mappings[vid].append(m)
        self.assignments=defaultdict(list)
        for a in db.scalars(select(VendorVariationAssignment)):self.assignments[a.square_variation_id].append(a)
        self.costs=defaultdict(list)
        for cost in db.scalars(select(VendorVariationCost)):self.costs[cost.square_variation_id].append(cost)
        self.funding_mappings=defaultdict(list)
        for m in db.scalars(select(FundingSkuMapping).where(FundingSkuMapping.status=='ACTIVE')):
            vid=m.square_variation_id
            if not vid:
                aliases=self.by_sku[self.sku_key(m.normalized_sku)]
                if len(aliases)==1:vid=next(iter(aliases))
            if vid:self.funding_mappings[vid].append(m)

    @staticmethod
    def sku_key(value):return ''.join(str(value or '').upper().split())

    def owner_event(self,vid,as_of):
        row=self.decisions.get(vid)
        if not row:return None
        for event in reversed(row.value.get('classification_history',[])):
            if utc(datetime.fromisoformat(event['start_at']))<=utc(as_of) and (
                event.get('end_at') is None or utc(as_of)<utc(datetime.fromisoformat(event['end_at']))):return event
        # Existing explicit account decisions remain prospective from their audit
        # timestamp. New historical corrections always specify an explicit range.
        legacy=row.value.get('legacy_assignment')
        if legacy and utc(datetime.fromisoformat(legacy['start_at']))<=utc(as_of):return legacy
        if not row.value.get('classification_history') and utc(row.updated_at)<=utc(as_of):
            return {'category':account_category(self.accounts.get(row.value.get('account_id')),self.vendors),'reason':'EXISTING_OWNER_ACCOUNT'}
        return None

    def classify(self,vid,as_of,*,fact=None,vendor_ids=None):
        event=self.owner_event(vid,as_of)
        if event:return CogsClassification(event['category'],'OWNER_CLASSIFICATION')
        if fact is not None and getattr(fact,'attribution_source',None)=='OWNER_TRANSACTION_OVERRIDE':
            if fact.attribution_status=='NON_CONSIGNMENT':return CogsClassification('CREDIT_CARD_POOL','OWNER_TRANSACTION_OVERRIDE')
            if fact.attribution_status=='ATTRIBUTED':
                vendor=self.vendors.get(fact.vendor_id_snapshot)
                ids={vendor.square_vendor_id} if vendor else set()
                return self.automatic(ids,'OWNER_TRANSACTION_OVERRIDE')
        effective=[]
        for assignment in self.assignments.get(vid,[]):
            if utc(assignment.effective_start_at)<=utc(as_of) and (assignment.effective_end_at is None or utc(as_of)<utc(assignment.effective_end_at)):
                vendor=self.vendors.get(assignment.vendor_id)
                if assignment.source=='OWNER':
                    effective.append('CREDIT_CARD_POOL' if not assignment.is_consignment else self.automatic({vendor.square_vendor_id} if vendor else set()).category)
        for mapping in self.funding_mappings.get(vid,[]):
            day=utc(as_of).astimezone(BUSINESS_TIMEZONE).date()
            if mapping.effective_start_date<=day and (mapping.effective_end_date is None or mapping.effective_end_date>=day):
                effective.append(account_category(self.accounts.get(mapping.account_id),self.vendors))
        if effective:
            category=next((c for c in CATEGORIES if c in effective),'UNKNOWN')
            return CogsClassification(category,'EXISTING_OWNER_SKU_ASSOCIATION')
        ids=set(vendor_ids or [])
        authority=self.authorities.get(vid)
        if authority:ids.update(authority.square_vendor_ids or [])
        for mapping in self.mappings.get(vid,[]):
            if mapping.is_default_vendor:ids.add(self.vendors[mapping.vendor_id].square_vendor_id)
        if fact is not None and getattr(fact,'is_consignment_snapshot',False):
            vendor=self.vendors.get(fact.vendor_id_snapshot)
            if vendor:ids.add(vendor.square_vendor_id)
        return self.automatic(ids)

    @staticmethod
    def automatic(ids,reason='AUTOMATIC_HIERARCHY'):
        category='STREAMLINE' if settings.purchasing_streamline_square_vendor_id and settings.purchasing_streamline_square_vendor_id in ids else 'BIG' if settings.purchasing_big_square_vendor_id and settings.purchasing_big_square_vendor_id in ids else 'CREDIT_CARD_POOL'
        return CogsClassification(category,reason,tuple(sorted(x for x in ids if x)))


def classify_product(db,*,variation_id,as_of):
    return ClassificationContext(db).classify(variation_id,as_of)


def save_classification(db,*,actor_id,variation_id,category,scope,start_date,end_date=None,reason,version,request_key):
    from app.services.purchasing_session_service import authorize
    from app.services.audit_service import log_audit
    from app.services.purchasing_payments_service import digest
    authorize(db,actor_id)
    if isinstance(version,bool) or not isinstance(version,int) or version<0:raise ValueError('Supply the current classification version')
    if not isinstance(variation_id,str) or not variation_id.strip() or len(variation_id)>255:raise ValueError('Choose a Square variation')
    if category not in CATEGORIES or scope not in ('FORWARD','HISTORICAL'):raise ValueError('Choose a classification and effective scope')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>500:raise ValueError('Enter a correction reason (up to 500 characters)')
    if not isinstance(request_key,str) or not request_key or len(request_key)>128:raise ValueError('A stable request identity is required')
    start=date.fromisoformat(start_date);end=date.fromisoformat(end_date) if end_date else None
    today=datetime.now(BUSINESS_TIMEZONE).date()
    if scope=='FORWARD' and (start<today or end is not None):raise ValueError('Forward changes start today or later and have no end date')
    if scope=='HISTORICAL' and (end is None or start>end or end>today):raise ValueError('Historical corrections require an explicit completed date range')
    intent={'variation_id':variation_id,'category':category,'scope':scope,'start':str(start),'end':str(end) if end else None,'reason':reason.strip(),'version':version}
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':LOCK})
    prior=db.scalar(select(AuditLog).where(AuditLog.action=='SKU_FINANCING_CLASSIFIED',AuditLog.actor_principal_id==actor_id,AuditLog.meta['request_key'].as_string()==request_key))
    if prior:
        if prior.meta['intent']!=intent:raise ValueError('Request key was already used for another change')
        return prior.meta['result']
    row=db.scalar(select(PurchasingOwnerDecision).where(PurchasingOwnerDecision.kind=='FINANCING',PurchasingOwnerDecision.subject_key==variation_id).execution_options(populate_existing=True))
    if (row.version if row else 0)!=version:raise ValueError('Classification changed; refresh and review before saving')
    context=ClassificationContext(db)
    # A sold product is a valid target even when it has no catalog, PO or receipt.
    known=variation_id in context.identities or variation_id in context.authorities or variation_id in context.mappings or db.scalar(select(ConsignmentSaleFact.id).where(ConsignmentSaleFact.square_variation_id==variation_id).limit(1))
    if not known:raise ValueError('Square variation was not found in products or sales')
    before=dict(row.value) if row else None;value=dict(before or {})
    if before and not value.get('classification_history') and before.get('account_id'):
        value['legacy_assignment']={'category':account_category(context.accounts.get(before['account_id']),context.vendors),'start_at':utc(row.updated_at).isoformat()}
    event={'category':category,'scope':scope,'start_at':datetime.combine(start,time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc).isoformat(),
        'end_at':datetime.combine(end+timedelta(days=1),time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc).isoformat() if end else None,'reason':reason.strip(),'actor_id':actor_id,'recorded_at':datetime.now(timezone.utc).isoformat()}
    value['classification_history']=[*value.get('classification_history',[]),event]
    if row:row.value=value;row.version+=1;row.actor_id=actor_id;row.updated_at=datetime.now(timezone.utc)
    else:row=PurchasingOwnerDecision(kind='FINANCING',subject_key=variation_id,value=value,version=1,actor_id=actor_id);db.add(row)
    db.flush();result={'saved':True,'variation_id':variation_id,'version':row.version,'category':category,'scope':scope}
    log_audit(db,actor_principal_id=actor_id,action='SKU_FINANCING_CLASSIFIED',session_id=None,ip=None,
        metadata={'request_key':request_key,'intent':intent,'before':before,'after':value,'result':result,'finalized_reports_rewritten':False})
    return result


def workspace(db,*,actor_id,query='',page=1,as_of=None):
    from app.services.purchasing_session_service import authorize
    authorize(db,actor_id);context=ClassificationContext(db);products={}
    for vid,i in context.identities.items():products[vid]={'variation_id':vid,'product':i.product_name or i.item_name or vid,'sku':i.sku or ''}
    for vid,name,sku in db.execute(select(ConsignmentSaleFact.square_variation_id,ConsignmentSaleFact.product_name_snapshot,ConsignmentSaleFact.sku_snapshot).distinct()):
        if vid:products.setdefault(vid,{'variation_id':vid,'product':name,'sku':sku or ''})
    for vid,mappings in context.mappings.items():products.setdefault(vid,{'variation_id':vid,'product':vid,'sku':mappings[0].sku or ''})
    selected=date.fromisoformat(as_of) if as_of else datetime.now(BUSINESS_TIMEZONE).date()
    selected_at=datetime.combine(selected,time.max,BUSINESS_TIMEZONE)
    term=query.strip().casefold();rows=[]
    for vid,p in products.items():
        if term and term not in (p['product']+' '+p['sku']+' '+vid).casefold():continue
        classification=context.classify(vid,selected_at);row=context.decisions.get(vid)
        history=[{**e,'label':LABELS.get(e['category'],e['category']),'scope_label':'Historical correction' if e['scope']=='HISTORICAL' else 'Going forward',
            'effective_start':str(utc(datetime.fromisoformat(e['start_at'])).astimezone(BUSINESS_TIMEZONE).date()),
            'effective_end':str((utc(datetime.fromisoformat(e['end_at']))-timedelta(microseconds=1)).astimezone(BUSINESS_TIMEZONE).date()) if e.get('end_at') else None} for e in row.value.get('classification_history',[])] if row else []
        rows.append({**p,'category':classification.category,'label':LABELS.get(classification.category,'Needs review'),'source':classification.reason,'version':row.version if row else 0,'history':history})
    rows.sort(key=lambda p:(p['product'].casefold(),p['variation_id']));page=max(1,int(page))
    return {'rows':rows[(page-1)*25:page*25],'total':len(rows),'page':page,'pages':max(1,(len(rows)+24)//25),'query':query,'as_of':str(selected),'monday':str(selected-timedelta(days=(selected.weekday()+1)%7)+timedelta(days=8)),'today':str(datetime.now(BUSINESS_TIMEZONE).date())}
