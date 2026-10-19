"""Weekly payment orchestration and an independent current-stock display.

Batch tables coordinate existing writers and audit; they never establish card
balances or allocate inventory with FIFO. Inventory valuation reads Square stock
through the existing gateway. Recording payments makes no external calls.
"""
import hashlib
import json
from datetime import date,datetime,time,timedelta,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from sqlalchemy import select,func,text,or_
from app.config import settings
from app.models import (FundingAccount,PaymentMethod,Vendor,Store,ConsignmentSaleFact,
    OrderPayment,PurchaseOrder,PurchaseOrderLine,PurchaseOrderReceiptLine,PurchaseOrderReceipt,
    FundingReport,FundingReportLine,FundingReportFactLink,FundingPayment,PurchasingProductAuthority,PurchasingPaymentBatch,PurchasingPaymentBatchEntry)
from app.services import v2_order_payments_service as financing
from app.services import v2_funding_reports_service as funding
from app.services import card_balance_service as cards
from app.services.purchasing_classification_service import classify_product
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE,utc,sales_coverage_for
from app.services.purchasing_session_service import authorize, _cost
from app.services.audit_service import log_audit

ROLES=('big','streamline','card1','card2')
LABELS={'big':'Big Wholesale','streamline':'Streamline','card1':'Credit Card 1','card2':'Credit Card 2'}
CENT=Decimal('.01')

class Conflict(ValueError):pass

def now():return financing.utc_now()
def today():return now().astimezone(BUSINESS_TIMEZONE).date()

def access(db,actor_id):
    authorize(db,actor_id)
    if not settings.purchasing_v3_phase1_enabled or not settings.purchasing_monday_payments_enabled:
        raise PermissionError('Monday payments are not enabled')
    if db.get_bind().dialect.name!='postgresql':raise RuntimeError('Monday payments require PostgreSQL')

def period(monday=None):
    current=today()
    if monday is None:
        monday=current-timedelta(days=current.weekday())
        if current.weekday()==6:monday+=timedelta(days=7)
    elif isinstance(monday,str):monday=date.fromisoformat(monday)
    if monday.weekday()!=0:raise ValueError('Choose a Monday payment week')
    return monday-timedelta(days=8),monday-timedelta(days=2),monday

def money(value):
    if isinstance(value,bool):raise ValueError('Enter nonnegative dollar amounts with at most two decimals')
    try:amount=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):raise ValueError('Enter a valid dollar amount')
    if not amount.is_finite() or amount<0 or amount>Decimal('9999999999.99') or amount!=amount.quantize(CENT):
        raise ValueError('Enter nonnegative dollar amounts with at most two decimals')
    return amount.quantize(CENT)

def digest(payload):return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def account_map(db):
    result={};errors={}
    for role,vid in [('big',settings.purchasing_big_square_vendor_id),('streamline',settings.purchasing_streamline_square_vendor_id)]:
        matches=db.scalars(select(FundingAccount).join(Vendor,FundingAccount.vendor_id==Vendor.id).where(
            FundingAccount.account_type=='CONSIGNMENT',FundingAccount.is_active.is_(True),Vendor.active.is_(True),Vendor.square_vendor_id==vid)).all() if vid else []
        result[role]=matches[0] if len(matches)==1 else None
        if not result[role]:errors[role]='Configure a unique active consignment account and canonical Square vendor'
    from app.services.purchasing_attention_service import card_id
    for role,default in [('card1',settings.purchasing_card1_account_id),('card2',settings.purchasing_card2_account_id)]:
        aid=card_id(db,role,default)
        a=db.get(FundingAccount,aid) if aid else None
        method=db.get(PaymentMethod,a.payment_method_id) if a else None
        result[role]=a if a and a.is_active and a.account_type=='CREDIT_CARD' and method and method.is_active and method.category=='CREDIT_CARD' else None
        if not result[role]:errors[role]='Configure the active credit-card funding account'
    ids=[a.id for a in result.values() if a]
    if len(set(ids))!=len(ids):errors.update({role:'The four accounts must be distinct' for role in ROLES})
    return result,errors

def _lot_categories(db,variations,end_at):
    """Use uniform saved financing overrides; mixed lots are unknown, never FIFO.

    A sales fact has no lot ID. Conflicting received-lot assignments therefore
    cannot be resolved by selecting a supplier or the newest PO.
    """
    rows=db.execute(select(PurchaseOrderLine,OrderPayment).join(PurchaseOrder,
        PurchaseOrder.id==PurchaseOrderLine.purchase_order_id).join(OrderPayment,
        OrderPayment.purchase_order_id==PurchaseOrder.id).where(PurchaseOrderLine.variation_id.in_(variations or ['']),
        PurchaseOrderLine.received_qty_total>0,PurchaseOrderLine.removed.is_(False),
        func.coalesce(PurchaseOrder.ordered_at,PurchaseOrder.submitted_at,PurchaseOrder.created_at)<end_at)).all()
    output={}
    for line,payment in rows:
        if payment.financial_treatment=='REPLENISHMENT':
            v=db.get(Vendor,payment.vendor_id)
            category='big' if v and v.square_vendor_id==settings.purchasing_big_square_vendor_id else 'streamline' if v and v.square_vendor_id==settings.purchasing_streamline_square_vendor_id else 'unknown'
        elif payment.payment_category_snapshot in {'WIRE','DEBIT_CARD'}:category='cash'
        elif payment.payment_category_snapshot=='CREDIT_CARD':category='pool'
        else:category='unknown'
        if utc(payment.created_at)>=end_at:
            category='unknown'
        dated=db.scalar(select(PurchaseOrderReceiptLine.id).join(PurchaseOrderReceipt,PurchaseOrderReceipt.id==PurchaseOrderReceiptLine.receipt_id).where(PurchaseOrderReceiptLine.purchase_order_line_id==line.id,PurchaseOrderReceiptLine.received_qty>0,PurchaseOrderReceipt.status=='SUBMITTED',func.coalesce(PurchaseOrderReceipt.received_at,PurchaseOrderReceipt.created_at)<end_at).limit(1))
        if not dated:
            category='unknown'
        output.setdefault(line.variation_id,set()).add(category)
    return output

def recommendation(db,*,actor_id,monday=None):
    access(db,actor_id)
    start,end,monday=period(monday);accounts,errors=account_map(db)
    start_at=datetime.combine(start,time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc)
    end_at=datetime.combine(end+timedelta(days=1),time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc)
    locations=list(db.scalars(select(Store.square_location_id).where(Store.active.is_(True))))
    if len(set(locations))!=len(locations) or end_at>now() or not sales_coverage_for(db,start_at=start_at,end_at=end_at,location_ids=locations,as_of=now()):
        errors.update({r:'Incomplete completed-period sales coverage' for r in ROLES})
    facts=db.scalars(select(ConsignmentSaleFact).where(ConsignmentSaleFact.transacted_at>=start_at,
        ConsignmentSaleFact.transacted_at<end_at,ConsignmentSaleFact.square_location_id.in_(locations)).order_by(ConsignmentSaleFact.id)).all()
    linked=db.scalars(select(FundingReport).join(FundingReportFactLink,FundingReportFactLink.report_id==FundingReport.id).where(
        FundingReportFactLink.sale_fact_id.in_([f.id for f in facts]),FundingReport.status.not_in(['DRAFT','VOIDED']))).all()
    if any(not ((r.warning_summary or {}).get('monday_payments') and r.sales_start_date==start and r.sales_end_date==end) for r in linked):
        errors.update({r:'Sales already represented by finalized reports require review; no duplicate recommendation was created' for r in ROLES})
    categories=_lot_categories(db,{f.square_variation_id for f in facts},end_at)
    amounts={'big':Decimal(0),'streamline':Decimal(0),'pool':Decimal(0)};evidence=[];cost_scopes={}
    for f in facts:
        assigned=categories.get(f.square_variation_id,set())
        if f.attribution_source=='OWNER_TRANSACTION_OVERRIDE':
            if f.attribution_status=='EXCLUDED':continue
            if f.attribution_status=='NON_CONSIGNMENT':assigned={'pool'}
            elif f.vendor_id_snapshot:
                v=db.get(Vendor,f.vendor_id_snapshot)
                assigned={'big' if v and v.square_vendor_id==settings.purchasing_big_square_vendor_id else 'streamline' if v and v.square_vendor_id==settings.purchasing_streamline_square_vendor_id else 'unknown'}
        if len(assigned)>1 or 'unknown' in assigned:
            errors.update({r:f'Ambiguous financed lots for {f.sku_snapshot or f.square_variation_id}' for r in ROLES});continue
        if assigned:category=next(iter(assigned))
        else:
            c=classify_product(db,variation_id=f.square_variation_id,as_of=now())
            category={'BIG':'big','STREAMLINE':'streamline','CREDIT_CARD_POOL':'pool','CASH_9671':'cash'}.get(c.category,'unknown')
        if category=='cash':continue
        value=f.extended_cogs_snapshot;unit=f.unit_cost_snapshot;cost_source='CAPTURED_SALE_FACT'
        if value is None and unit is None:
            authority=db.get(PurchasingProductAuthority,f.square_variation_id)
            ids=authority.square_vendor_ids if authority and authority.source_complete else []
            v=db.scalar(select(Vendor).where(Vendor.square_vendor_id==ids[0],Vendor.active.is_(True))) if len(ids)==1 else None
            if v:
                if v.id not in cost_scopes:
                    try:cost_scopes[v.id]=funding.consignment_product_report.product_scope(db,vendor_id=v.id)[0]
                    except ValueError:cost_scopes[v.id]={}
                mapping=cost_scopes[v.id].get(f.square_variation_id)
                if mapping and mapping.unit_cost is not None:
                    unit=mapping.unit_cost;value=(f.quantity_sold*unit).quantize(CENT,rounding=ROUND_HALF_UP);cost_source=f'LOCAL_CONFIG:{mapping.id}'
        if (category=='unknown' or value is None or not value.is_finite() or value<0 or f.quantity_sold<0
                or f.currency!='USD' or f.attribution_status in {'SOURCE_INCOMPLETE','AMBIGUOUS_VENDOR'}):
            errors.update({r:f'Incomplete financing/cost evidence for {f.sku_snapshot or f.square_variation_id}' for r in ROLES});continue
        # Existing sale-fact valuation is gross quantity x captured cost. Return
        # facts and discounts never reduce this approved weekly basis.
        if unit is None or value!=(f.quantity_sold*unit).quantize(CENT,rounding=ROUND_HALF_UP):
            errors.update({r:'Gross COGS snapshot does not reconcile to captured cost' for r in ROLES});continue
        amounts[category]+=value
        evidence.append({'fact_id':f.id,'variation_id':f.square_variation_id,'category':category,'gross_cogs':str(value),
            'unit_cost':str(unit),'cost_source':cost_source,'source_version':f.source_order_version,'updated_at':f.updated_at.isoformat()})
    balances={};recommended={'big':amounts['big'],'streamline':amounts['streamline']}
    for role in ('card1','card2'):
        balances[role]=cards.actual_card_balance(db,account_id=accounts[role].id) if accounts[role] else None
    if any(v is None for v in balances.values()):
        for r in ('card1','card2'):errors.setdefault(r,'Needs Attention: set actual issuer balances before calculating the credit-card split')
    else:
        weights={r:max(Decimal(0),v) for r,v in balances.items()};total=sum(weights.values())
        if total==0:errors.update({r:'Needs Attention: enter actual positive card balances or provide actual payments; no allocation was assumed' for r in weights})
        else:
            recommended['card1']=(amounts['pool']*weights['card1']/total).quantize(CENT,rounding=ROUND_HALF_UP) if total else Decimal(0)
            recommended['card2']=amounts['pool']-recommended['card1']
    # Existing reports must not be counted again as a new weekly obligation.
    existing=db.scalars(select(FundingReport).where(FundingReport.account_id.in_([a.id for a in accounts.values() if a]),
        FundingReport.sales_start_date<=end,FundingReport.sales_end_date>=start,FundingReport.status!='VOIDED')).all()
    if any(not (r.warning_summary or {}).get('monday_payments') for r in existing):
        errors.update({r:'Existing overlapping COGS reports require review; no duplicate recommendation was created' for r in ROLES})
    rows=[]
    for role in ROLES:
        a=accounts[role];balance=balances.get(role)
        rows.append({'role':role,'label':LABELS[role],'account_id':a.id if a else None,'name':a.display_name if a else 'Needs Attention — awaiting configuration',
            'recommended':str(recommended.get(role,Decimal(0)).quantize(CENT)) if role not in errors else None,
            'unavailable':errors.get(role),'balance':str(balance) if balance is not None else None})
    snapshot={'sales_start':str(start),'sales_end':str(end),'monday':str(monday),'rows':rows,'gross_card_pool':str(amounts['pool']),
        'facts':evidence,'card_balances':{k:str(v) if v is not None else None for k,v in balances.items()}}
    snapshot['token']=digest(snapshot)
    return snapshot

def _entries(db,bid):return db.scalars(select(PurchasingPaymentBatchEntry).where(PurchasingPaymentBatchEntry.batch_id==bid).order_by(PurchasingPaymentBatchEntry.id)).all()

def save(db,*,actor_id,monday,amounts,request_key,token,corrects=None,note=''):
    access(db,actor_id);start,end,monday=period(monday)
    if not isinstance(note,str) or len(note)>500:raise ValueError('Correction note must be at most 500 characters')
    if corrects is not None and (isinstance(corrects,bool) or not isinstance(corrects,int) or corrects<1):raise ValueError('Choose a valid batch to correct')
    if not isinstance(request_key,str) or not request_key.strip() or len(request_key)>128:raise ValueError('A stable request identity is required')
    if not isinstance(amounts,dict) or set(amounts)!=set(ROLES):raise ValueError('Save all four account amounts together')
    actual={r:str(money(amounts[r])) for r in ROLES}
    intent={'monday':str(monday),'amounts':actual,'corrects':corrects,'note':note.strip()}
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':f'monday-payments:{start}'})
    old_request=db.scalar(select(PurchasingPaymentBatch).where(PurchasingPaymentBatch.request_key==request_key))
    if old_request:
        if old_request.request_payload!=intent or old_request.actor_id!=actor_id:raise Conflict('Request identity was already used for different payments')
        return old_request
    # Serialize account-role decisions with first saves; historical retries above retain their original batch.
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':'purchasing-exception:CARD:roles'})
    latest=db.scalar(select(PurchasingPaymentBatch).where(PurchasingPaymentBatch.sales_start==start).order_by(PurchasingPaymentBatch.revision.desc()).limit(1))
    if (latest and corrects!=latest.id) or (not latest and corrects):raise Conflict('This week already has a batch or its correction changed; review history first')
    if latest:
        snapshot=latest.recommendations;accounts={e.role:db.get(FundingAccount,e.account_id) for e in _entries(db,latest.id)}
    else:
        accounts,_=account_map(db)
        if any(a is None for a in accounts.values()) or len({a.id for a in accounts.values()})!=4:raise ValueError('Configure all four distinct accounts before recording payments')
    db.execute(select(FundingAccount).where(FundingAccount.id.in_([a.id for a in accounts.values()])).order_by(FundingAccount.id).with_for_update().execution_options(populate_existing=True)).all()
    if any(not a.is_active for a in accounts.values()):raise ValueError('A selected account is inactive; owner review is required')
    if not latest:
        start_at=datetime.combine(start,time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc)
        end_at=datetime.combine(end+timedelta(days=1),time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc)
        db.execute(select(ConsignmentSaleFact.id).where(ConsignmentSaleFact.transacted_at>=start_at,ConsignmentSaleFact.transacted_at<end_at).order_by(ConsignmentSaleFact.id).with_for_update()).all()
        db.expire_all()  # Re-read source/configuration after waiting for transaction locks.
        snapshot=recommendation(db,actor_id=actor_id,monday=monday)
        if token!=snapshot['token']:raise Conflict('Recommendations or balances changed; refresh and review. Your actual amounts are retained.')
    batch=PurchasingPaymentBatch(sales_start=start,sales_end=end,monday=monday,payment_date=today(),revision=latest.revision+1 if latest else 1,
        request_key=request_key,supersedes_id=latest.id if latest else None,request_payload=intent,recommendations=snapshot,
        note=note.strip() or None,actor_id=actor_id)
    db.add(batch);db.flush()
    previous={e.role:e for e in _entries(db,latest.id)} if latest else {}
    for role in ROLES:
        entry=PurchasingPaymentBatchEntry(batch_id=batch.id,role=role,account_id=accounts[role].id,actual=Decimal(actual[role]))
        db.add(entry)
    db.flush()
    for entry in _entries(db,batch.id):
        role=entry.role;account=accounts[role];prior=previous.get(role)
        if prior and prior.payment_id:
            reversal=funding.reverse_payment(db,payment_id=prior.payment_id,reason=note.strip() or f'Owner correction of weekly batch #{latest.id}',
                actor_id=actor_id,weekly_batch_id=batch.id)
            entry.reversal_id=reversal.id
        report=db.get(FundingReport,prior.report_id) if prior and prior.report_id else None
        row=next(r for r in snapshot['rows'] if r['role']==role)
        if not latest and row['recommended'] is not None:
            report=FundingReport(account_id=account.id,vendor_id=account.vendor_id,report_number=f'MONDAY-{batch.id}-{role}',
                account_name_snapshot=account.display_name,account_type_snapshot=account.account_type,sales_start_date=start,sales_end_date=end,
                status='FINALIZED',calculated_cogs=Decimal(row['recommended']),finalized_at=now(),finalized_by_principal_id=actor_id,
                created_by_principal_id=actor_id,warning_summary={'monday_payments':batch.id,'basis':'Gross sale COGS; returns excluded; card pool proportional to actual issuer balances'},
                finalized_snapshot={'monday_payments':batch.id,'recommended':row['recommended'],'snapshot':snapshot})
            db.add(report);db.flush()
            summary_line=FundingReportLine(report_id=report.id,normalized_sku='WEEKLY-COGS',sku_snapshot='WEEKLY-COGS',product_name_snapshot='Weekly gross COGS dollar allocation',
                units_sold=0,units_returned=0,net_units=0,extended_cogs=Decimal(row['recommended']),mapping_effective_date_snapshot=start,source_transaction_count=sum(f['category']==(role if role in ('big','streamline') else 'pool') for f in snapshot['facts']))
            db.add(summary_line);db.flush()
            category=role if role in ('big','streamline') else 'pool'
            sources=[f for f in snapshot['facts'] if f['category']==category]
            remaining=Decimal(row['recommended']);basis=sum((Decimal(f['gross_cogs']) for f in sources),Decimal(0))
            for i,fact in enumerate(sources):
                share=remaining if i==len(sources)-1 else (Decimal(row['recommended'])*Decimal(fact['gross_cogs'])/basis).quantize(CENT,rounding=ROUND_HALF_UP) if basis else Decimal(0)
                remaining-=share
                db.add(FundingReportFactLink(report_id=report.id,report_line_id=summary_line.id,sale_fact_id=fact['fact_id'],cogs_amount_snapshot=share))
        entry.report_id=report.id if report else None
        db.flush()
        if entry.actual>0:
            payment=funding.record_payment(db,account_id=account.id,entry_type='PAYMENT',amount=entry.actual,payment_date=today(),
                payment_source='Owner-reported Monday payment',confirmation_number='',reason=f'Monday payments {monday}',internal_note=note.strip(),
                allocations={report.id:entry.actual} if report else {},actor_id=actor_id,idempotency_key=f'monday-{batch.id}-{role}',weekly_batch_id=batch.id)
            entry.payment_id=payment.id
        db.flush()
    log_audit(db,actor_principal_id=actor_id,action='MONDAY_PAYMENTS_CORRECTED' if latest else 'MONDAY_PAYMENTS_SAVED',session_id=None,ip=None,
        metadata={'batch_id':batch.id,'supersedes_id':batch.supersedes_id,'monday':str(monday),'amounts':actual,'recommendation_token':snapshot['token']})
    db.flush();return batch

def batch_view(db,batch):
    from app.models import Principal
    users=db.get(Principal,batch.actor_id)
    return {'id':batch.id,'revision':batch.revision,'monday':str(batch.monday),'payment_date':str(batch.payment_date),'sales_start':str(batch.sales_start),
        'sales_end':str(batch.sales_end),'created_at':batch.created_at.isoformat(),'recorded_at':utc(batch.created_at).astimezone(BUSINESS_TIMEZONE).strftime('%b %d, %Y %I:%M %p %Z'),'actor':users.username if users else str(batch.actor_id),
        'actual_total':str(sum(e.actual for e in _entries(db,batch.id))),'supersedes_id':batch.supersedes_id,'request_key':batch.request_key,'note':batch.note,'recommendations':batch.recommendations,
        'entries':[{'role':e.role,'label':LABELS[e.role],'account_id':e.account_id,'actual':str(e.actual),'payment_id':e.payment_id,'report_id':e.report_id} for e in _entries(db,batch.id)]}

def consignment_inventory_values(db, *, accounts=None, as_of=None, gateway=None):
    """Presentation adapter for the shared current-assignment inventory valuation."""
    from app.services.consignment_reporting_service import inventory_value, money
    accounts=accounts if accounts is not None else account_map(db)[0]
    result={}
    for role in ('big','streamline'):
        account=accounts.get(role)
        row={'value':None,'known_value':'0.00','complete':False,'items':[],'issues':[],
             'observed_at':(as_of or now()).isoformat(),'store_names':{}}
        result[role]=row
        if account is None:
            row['issues'].append({'sku':'Unconfigured account','variation_id':None,'message':'Configure the active consignment account.'});continue
        view=inventory_value(db,vendor_id=account.vendor_id,gateway=gateway)
        row.update(value=str(money(view['value'])) if view['complete'] else None,
            known_value=str(money(view['known_value'])),complete=view['complete'],
            observed_at=(as_of or view['read_at']).isoformat(),store_names={s['store_id']:s['name'] for s in view['stores']},
            negative_observations=[{**r,'quantity':str(r['quantity'])} for r in view['negative_observations']])
        row['issues']=[{'sku':r['sku'],'variation_id':None,'message':r['store']+': '+r['reason']} for r in view['issues']]
        by_mapping={}
        for observation in view['rows']:by_mapping.setdefault(observation['mapping_id'],[]).append(observation)
        for product in view['products']:
            observations=by_mapping.get(product['mapping_id'],[])
            if not observations:continue
            complete=all(r['value'] is not None for r in observations)
            value=sum((r['value'] for r in observations),Decimal(0)) if complete else None
            row['items'].append({'variation_id':product['variation_id'],'sku':product['sku'],'name':product['name'],'product':product['name'],
                'attribution_source':'CURRENT_VENDOR_ASSIGNMENT','cost_source':f"VendorSkuConfig:{product['mapping_id']}",
                'unit_cost':str(product['unit_cost']) if product['unit_cost'] is not None else None,
                'on_hand':str(sum((r['positive_units'] or Decimal(0) for r in observations),Decimal(0))) if all(r['quantity'] is not None for r in observations) else None,
                'stores':{r['store_id']:str(r['quantity']) if r['quantity'] is not None else None for r in observations},
                'value':str(money(value)) if value is not None else None,'unrounded_value':str(value) if value is not None else None})
    for row in result.values():row['items'].sort(key=lambda item:item['variation_id'] or '')
    return result


def workspace(db,*,actor_id,monday=None):
    rec=recommendation(db,actor_id=actor_id,monday=monday)
    latest=db.scalar(select(PurchasingPaymentBatch).where(PurchasingPaymentBatch.monday==date.fromisoformat(rec['monday'])).order_by(PurchasingPaymentBatch.revision.desc()).limit(1))
    history=db.scalars(select(PurchasingPaymentBatch).order_by(PurchasingPaymentBatch.id.desc()).limit(30)).all()
    return {'recommendation':rec,'latest':batch_view(db,latest) if latest else None,'history':[batch_view(db,b) for b in history],
        'inventory_values':consignment_inventory_values(db)}
