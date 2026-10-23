"""Explain model abstentions and sort advisory attention without altering model math."""
from collections import Counter, defaultdict
from fractions import Fraction as F

PRIORITY_LABELS = {'immediate': 'Immediate stockout scenario (≤7 days)',
                   'near_term': 'Replenishment attention', 'evidence': 'Insufficient evidence',
                   'slow': 'Verified slow stock', 'excess': 'Above policy target', 'monitor': 'Monitor'}

def abstention_audit(payload):
    rows={(str(r['store_id']),r['variation_id']):r for r in payload['snapshot']['rows']}
    models={}
    for model in ('A_RAW84','B_CURRENT','C_CONSERVATIVE','D_VERIFIED'):
        reasons=Counter();combinations=Counter();stores=defaultdict(Counter);store_n=Counter();categories=Counter();vendors=Counter();details=Counter();unique=set()
        for forecast in payload['forecasts']:
            result=forecast['models'][model]
            if result['rate'] is not None:continue
            key=(str(forecast['store_id']),forecast['variation_id']);r=rows[key];unique.add(key[1])
            reasons.update(result['reasons']);combinations[' + '.join(result['reasons'])]+=1
            stores[key[0]].update(result['reasons']);store_n[key[0]]+=1;categories[r.get('category') or 'Unknown']+=1;vendors[str(r.get('vendor_id'))]+=1
            if r.get('on_hand') is None:details['on_hand_missing']+=1
            if r.get('incoming') is None:details['incoming_missing']+=1
            if not r['sales_complete']:details['sales_coverage_incomplete']+=1
            if r.get('lifecycle','ACTIVE')!='ACTIVE':details['lifecycle_'+r['lifecycle']]+=1
            if 'DEMAND_CENSORED_OR_NEW' in result['reasons']:
                # This is the exact baseline branch; don't equate unknown availability to no demand.
                availability=r['availability'];sales=[F(v) for v in r['sales']]
                age=r.get('product_age_days');values=sales[-max(0,age):] if age is not None and age>0 else [] if age is not None else sales
                selected=availability[-len(values):] if values else []
                if all(v=='STOCKOUT' for v in selected[-min(56,len(selected)):]):details['no_eligible_days_in_long_window']+=1
                elif not any(v>0 for v in values) and F(r['on_hand'])<=0 and 'IN_STOCK' not in selected:
                    details['zero_observed_sales_nonpositive_stock_no_confirmed_in_stock_day']+=1
                else:details['other_censored_demand_branch']+=1
        n=sum(combinations.values());models[model]={'denominator_store_sku':len(rows),'abstentions':n,'fraction':n/len(rows),
            'distinct_variations_affected':len(unique),'reason_memberships':dict(reasons),'exclusive_combinations':dict(combinations),
            'stores':{s:{'denominator':sum(k[0]==s for k in rows),'reasons':dict(stores[s]),'abstentions':store_n[s]} for s in payload['snapshot']['stores']},
            'categories':dict(categories),'vendors':dict(vendors),'underlying_facts':dict(details)}
    return {'unit':'One store × catalog variation × selected model; reasons may overlap; combinations are exclusive.',
            'models':models,'recommendations':['Retain verified in-stock exposure; do not fill unknown availability with in-stock or zero demand.',
            'Recover missing stock and incoming facts through separately authorized established workflows.',
            'Review lifecycle blocks without changing archived product status or purchasing policy.',
            'Unknown supplier lead times and costs can limit recommendations even where demand does not abstain.']}

def attention(row, result, policy, days, excess, slow):
    missing=[]
    for key in ('on_hand','incoming'):
        if row.get(key) is None:missing.append(key)
    if not row.get('pack_known',True):missing.append('pack')
    if not row['sales_complete']:missing.append('sales_coverage')
    if result['rate'] is None:missing.append('demand')
    if row.get('unit_cost') is None:missing.append('cost')
    if row.get('lead_time_days') is None:missing.append('lead_time')
    insufficient=bool(result['rate'] is None or policy.get('quantity') is None or missing)
    immediate=days is not None and days<=7
    # Existing reorder trigger OR stock coverage at/below the greater of 14 days and known lead time.
    near=bool((policy.get('quantity') or 0)>0 or (days is not None and days<=max(14,row.get('lead_time_days') or 0)))
    primary='immediate' if immediate else 'evidence' if result['rate'] is None or policy.get('quantity') is None else 'near_term' if near else 'slow' if slow else 'excess' if excess else 'evidence' if insufficient else 'monitor'
    return {'primary':primary,'rank':{'immediate':0,'near_term':1,'evidence':2,'slow':3,'excess':4,'monitor':5}[primary],
            'immediate':immediate,'near_term':near,'insufficient':insufficient,'missing':missing}
