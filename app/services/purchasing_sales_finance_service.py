"""Shared sales-based COGS. Receipt and purchase-order history are not inputs."""
from datetime import datetime,time,timedelta,timezone
from decimal import Decimal,ROUND_HALF_UP
from urllib.parse import urlencode
from sqlalchemy import select
from app.models import ConsignmentSaleFact,ConsignmentReturnFact,Store
from app.services.purchasing_classification_service import ClassificationContext
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE
from app.services.vendor_product_ownership import single_default_mapping
from app.services.purchasing_product_policy_service import reward_marker,no_replenishment

CENT=Decimal('.01')
ROLES={'BIG':['big'],'STREAMLINE':['streamline'],'CREDIT_CARD_POOL':['card1','card2'],'UNKNOWN':['big','streamline','card1','card2']}
SEMANTICS='SQUARE_SALES_CLASSIFICATION_V1'


def cost_for(context,fact):
    """Captured accounting cost first; otherwise the established local default.

    No receiving or saved PO cost is required. An ambiguous default is UNKNOWN.
    """
    vid=fact.square_variation_id
    if fact.unit_cost_snapshot is not None:
        unit=Decimal(str(fact.unit_cost_snapshot))
        if unit.is_finite() and unit>=0:return unit,'CAPTURED_SALE_FACT'
        return None,'INVALID_CAPTURED_COST'
    mapping=single_default_mapping(context.mappings.get(vid,[]))
    if mapping and mapping.unit_cost is not None:
        configured=Decimal(str(mapping.unit_cost))
        if configured.is_finite() and configured>=0:return configured,f'LOCAL_CONFIG:{mapping.id}'
    # Existing effective-dated accounting costs are also established evidence,
    # not an inferred inventory or receiving price. Conflicting prices stay unknown.
    day=fact.business_date;stamp=getattr(fact,'transacted_at',None) or fact.returned_at
    from app.services.purchasing_evidence_service import utc
    candidates=[]
    for saved in context.funding_mappings.get(vid,[]):
        if saved.effective_start_date<=day and (saved.effective_end_date is None or day<=saved.effective_end_date) and saved.unit_cost is not None:
            candidates.append((Decimal(str(saved.unit_cost)),f'FUNDING_SKU_MAPPING:{saved.id}'))
    for saved in context.costs.get(vid,[]):
        if utc(saved.effective_start_at)<=utc(stamp) and (saved.effective_end_at is None or utc(stamp)<utc(saved.effective_end_at)):
            candidates.append((Decimal(str(saved.unit_cost)),f'EFFECTIVE_PRODUCT_COST:{saved.id}'))
    values={cost for cost,source in candidates if cost.is_finite() and cost>=0}
    if len(values)==1 and len(values)==len({cost for cost,source in candidates}):
        return next(iter(values)),','.join(sorted(source for cost,source in candidates))
    return None,'MISSING_OR_AMBIGUOUS_LOCAL_COST'


def calculate(db,*,start_date,end_date,store_ids=None,include_returns=False,query=''):
    context=ClassificationContext(db)
    stores=list(db.scalars(select(Store).where(Store.active.is_(True))))
    selected=[s for s in stores if not store_ids or s.id in store_ids]
    locations=[s.square_location_id for s in selected]
    start_at=datetime.combine(start_date,time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc)
    end_at=datetime.combine(end_date+timedelta(days=1),time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc)
    sales=list(db.scalars(select(ConsignmentSaleFact).where(ConsignmentSaleFact.transacted_at>=start_at,ConsignmentSaleFact.transacted_at<end_at,ConsignmentSaleFact.square_location_id.in_(locations)).order_by(ConsignmentSaleFact.id)))
    returns=list(db.scalars(select(ConsignmentReturnFact).where(ConsignmentReturnFact.returned_at>=start_at,ConsignmentReturnFact.returned_at<end_at,ConsignmentReturnFact.square_location_id.in_(locations)).order_by(ConsignmentReturnFact.id))) if include_returns else []
    rows=[];issues=[];limitations=[];detected=Decimal(0);excluded=[];merchandise=Decimal(0)
    for fact,is_return in [(f,False) for f in sales]+[(f,True) for f in returns]:
        if query and query.casefold() not in (str(fact.product_name_snapshot)+' '+str(fact.sku_snapshot or '')).casefold():continue
        raw_quantity=fact.quantity_returned if is_return else fact.quantity_sold
        quantity=Decimal(str(raw_quantity)) if raw_quantity is not None else None
        if not is_return and quantity is not None:detected+=quantity
        if fact.attribution_status=='EXCLUDED' and getattr(fact,'attribution_source',None)=='OWNER_TRANSACTION_OVERRIDE':
            excluded.append({'fact_id':fact.id,'is_return':is_return,'reason':fact.attribution_reason});continue
        if reward_marker(fact.square_variation_id):
            excluded.append({'fact_id':fact.id,'is_return':is_return,'variation_id':fact.square_variation_id,
                'quantity':str(quantity),'reason':'LOYALTY_DISCOUNT_MARKER','product':fact.product_name_snapshot,
                'gross_sales':str(fact.gross_sales_amount) if not is_return else None,
                'discount':str(fact.discount_amount) if not is_return else None})
            continue
        if not is_return and quantity is not None:merchandise+=quantity
        original=db.get(ConsignmentSaleFact,fact.original_sale_fact_id) if is_return and fact.original_sale_fact_id else None
        basis=original or fact
        stamp=original.transacted_at if original else fact.returned_at if is_return else fact.transacted_at
        classification=context.classify(basis.square_variation_id,stamp,fact=basis)
        identity=context.historical_identities.get(basis.square_variation_id)
        discontinued=no_replenishment(basis.square_variation_id,attributes=identity.catalog_attributes if identity else None)
        unit,source=cost_for(context,basis)
        value=(quantity*unit).quantize(CENT,rounding=ROUND_HALF_UP) if quantity is not None and quantity.is_finite() and quantity>=0 and unit is not None else None
        issue=None
        if not fact.square_variation_id:issue='Missing Square variation identity; identify this sold product'
        elif classification.category=='UNKNOWN':issue='The explicit owner classification points to an unavailable financing association'
        elif quantity is None or not quantity.is_finite() or quantity<0 or fact.currency!='USD':issue='Invalid Square quantity or currency; review the source transaction'
        elif unit is None:issue='Missing or ambiguous established unit cost; correct the product cost'
        elif not is_return and fact.extended_cogs_snapshot is not None and fact.unit_cost_snapshot is not None and value!=fact.extended_cogs_snapshot:
            issue='Captured COGS does not reconcile to Square quantity × captured unit cost'
        variation=getattr(fact,'variation_name_snapshot',None) or (identity.variation_name if identity else None)
        name=fact.product_name_snapshot
        if variation and variation not in (name,'Default','Regular'):name=f'{name} — {variation}'
        row={'fact_id':fact.id,'is_return':is_return,'variation_id':fact.square_variation_id,'product':name,
            'sku':fact.sku_snapshot or (identity.sku if identity else None),'quantity':str(quantity) if quantity is not None else None,'category':classification.category,
            'classification_source':classification.reason,'unit_cost':str(unit) if unit is not None else None,'cost_source':source,
            'gross_cogs':str(value) if value is not None else None,'cogs':str(-value if is_return else value) if value is not None else None,
            'store_id':fact.store_id,'business_date':str(fact.business_date),'source_version':getattr(fact,'source_order_version',None),
            'updated_at':fact.updated_at.isoformat()}
        if issue and unit is None and discontinued and issue.startswith('Missing or ambiguous'):
            limitations.append({**row,'policy':'KRATOM_NO_FUTURE_REPLENISHMENT','message':'Historical Kratom accounting cost unavailable. Sale preserved; no replenishment, replacement cost or financing association required. Known-cost payment basis excludes this unpriced historical amount.'})
        elif issue:
            cost_problem=unit is None or 'COGS does not reconcile' in issue
            issues.append({**row,'affected_roles':ROLES.get(classification.category,ROLES['UNKNOWN']),
                'message':issue,'action_url':('/management/ordering-tool/mappings?' if cost_problem else '/management/purchasing/classifications?')+urlencode({'q':row['sku'] or row['product'] or fact.square_variation_id or ''}),
                'action':'Correct established product cost' if cost_problem else 'Review SKU classification','lots':[]})
        else:rows.append(row)
    totals={category:sum((Decimal(r['cogs']) for r in rows if r['category']==category),Decimal(0)) for category in ('BIG','STREAMLINE','CREDIT_CARD_POOL')}
    return {'semantics':SEMANTICS,'rows':rows,'issues':issues,'totals':{k:str(v.quantize(CENT)) for k,v in totals.items()},
        'detected_sale_units':str(detected),'merchandise_sale_units':str(merchandise),
        'historical_limitations':limitations,'historical_cogs_complete':not issues and not limitations,'excluded':excluded,'start_date':str(start_date),'end_date':str(end_date),
        'include_returns':include_returns,'store_ids':sorted(s.id for s in selected)}


def split_pool(db,rows,accounts):
    """Optional issuer-balance allocation advice; never a COGS or payment gate."""
    from app.services.card_balance_service import actual_card_balance
    balances={r:actual_card_balance(db,account_id=accounts[r].id) if accounts.get(r) else None for r in ('card1','card2')}
    if any(v is None for v in balances.values()):return balances,None,'Optional allocation recommendation unavailable: issuer balances are unknown'
    weights={r:max(v,Decimal(0)) for r,v in balances.items()};total=sum(weights.values())
    if total==0:return balances,None,'Optional allocation recommendation unavailable: no positive issuer balance'
    running=Decimal(0);prior=Decimal(0);shares=[]
    for row in rows:
        running+=Decimal(row['cogs']);current=(running*weights['card1']/total).quantize(CENT,rounding=ROUND_HALF_UP)
        share=current-prior;prior=current
        shares.append({'card1':str(share),'card2':str(Decimal(row['cogs'])-share)})
    return balances,shares,None
