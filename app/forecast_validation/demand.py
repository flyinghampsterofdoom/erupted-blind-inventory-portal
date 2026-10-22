"""Exact audit definitions. Independent of the production calculator and policy."""
from fractions import Fraction as F
from statistics import median
from datetime import date
from .contracts import TZ,stamp,MODELS,VERSION,number

PARAMETERS={
 'A_RAW84':{'calendar_days':84},
 'B_CURRENT':{'long_days':56,'trend_prior_days':28,'recent_weeks':2,'spike_multiple':4,'window_uplift_cap':2,'growth_weights':['.65','.35'],'decline_weights':['.75','.25'],'growth_threshold':'1.15','decline_threshold':'.85','growth_cap':'1.6','new_history_days':28,'new_shrinkage_pseudodays':7,'no_established_demand_days':14,'recent_zero_days':28,'aged_stock_days':180,'aged_slow_factor':'.75','unknown_availability':'ELIGIBLE','reliable_stock_age_default':None},
 'C_CONSERVATIVE':{'minimum_true_days':28,'minimum_positive_true_days':7,'maximum_unknown_fraction':'.10','verified_cap':'1.5','uplift_weight':'.5','fallback':'RAW84'},
 'D_VERIFIED':{'minimum_true_days':14,'unknown':'EXCLUDE','partial':'EXCLUDE','insufficient':'ABSTAIN','exposure':'FULL_CALENDAR_DAY_PROXY'},
}

def _current(row,sales,availability):
    reasons=[]
    if row.get('on_hand') is None or row.get('incoming') is None:reasons.append('INVENTORY_OR_INCOMING_UNKNOWN')
    if row.get('lifecycle','ACTIVE')!='ACTIVE':reasons.append('LIFECYCLE_BLOCKED')
    if reasons:return None,{'reasons':reasons,'confidence':'LOW'}
    age=row.get('product_age_days')
    values=sales[-max(0,age):] if age is not None and age>0 else [] if age is not None else sales
    if not values:return None,{'reasons':['NO_COMPLETED_PRODUCT_HISTORY'],'confidence':'LOW'}
    a=availability[-len(values):]
    positives=[q for q in values if q>0]
    cap=max(4*median(positives),F(1)) if len(positives)>=7 and len(values)>=28 else None
    clipped=[min(q,cap) if cap is not None else q for q in values]
    stockouts=a.count(False)
    def rate(start,end=None):
        v=clipped[start:end];av=a[start:end];eligible=[q for q,s in zip(v,av) if s is not False]
        if not eligible:return None
        observed=sum(v)/len(v)
        return min(sum(eligible)/len(eligible),2*observed) if stockouts else observed
    if len(values)<14 and not positives:return None,{'reasons':['NO_ESTABLISHED_DEMAND'],'confidence':'LOW'}
    long=rate(-min(56,len(values)))
    if long is None or not positives and F(row['on_hand'])<=0 and True not in a:return None,{'reasons':['DEMAND_CENSORED_OR_NEW'],'confidence':'LOW'}
    v=long;trend='STABLE'
    if len(values)<28:v*=F(len(values),len(values)+7);trend='NEW';reasons.append('CONSERVATIVE_LIMITED_HISTORY')
    elif len(values)>=42:
        previous,w1,w2=rate(-42,-14),rate(-14,-7),rate(-7)
        if None not in (previous,w1,w2) and previous>0:
            recent=(w1+w2)/2
            if min(w1,w2)>previous*F('1.15'):v=min(recent*F('.65')+long*F('.35'),long*F('1.6'));trend='GROWING'
            elif max(w1,w2)<previous*F('.85'):v=recent*F('.75')+long*F('.25');trend='DECLINING'
    if len(values)>=28 and sum(values[-28:])==0 and False not in a[-28:]:v=F(0);trend='SLOW';reasons.append('NO_RECENT_OBSERVED_SALES')
    if row.get('reliable_stock_age_days') is not None and row['reliable_stock_age_days']>=180 and trend in ('SLOW','DECLINING'):v*=F('.75');reasons.append('VERIFIED_AGED_SLOW_INVENTORY')
    if any(x!=y for x,y in zip(values,clipped)):reasons.append('ISOLATED_SPIKES_CAPPED')
    if stockouts:reasons.append('CONFIRMED_STOCKOUT_ADJUSTMENT')
    confidence='HIGH' if len(values)>=42 and len(positives)>=14 and stockouts<len(values)//2 else 'MEDIUM' if len(values)>=14 else 'LOW'
    return v,{'reasons':reasons,'confidence':confidence,'trend':trend}

def calculate(model,row):
    if model not in MODELS:raise ValueError('Unknown model')
    if not row['sales_complete']:return {'model':model,'version':VERSION,'parameters':PARAMETERS[model],'rate':None,'demand28':None,'demand56':None,'uncertainty':{'interval':None,'reason':'Incomplete evidence'},'reasons':['SALES_COVERAGE_INCOMPLETE'],'confidence':'LOW'}
    u=[F(number(q,nonnegative=True)) for q in row['sales']]
    a=[True if v=='IN_STOCK' else False if v=='STOCKOUT' else None for v in row['availability']]
    strict=[True if v=='IN_STOCK' else False if v=='STOCKOUT' else None for v in row['verified_availability']]
    raw=sum(u)/84;idx=[i for i,v in enumerate(strict) if v is True]
    verified=sum(u[i] for i in idx)/len(idx) if idx else None
    meta={'reasons':[],'confidence':'UNVALIDATED','verified_days':len(idx),'unknown_days':strict.count(None)}
    if model=='A_RAW84':v=raw
    elif model=='B_CURRENT':v,meta=_current(row,u,a)
    elif model=='D_VERIFIED':
        v=verified if len(idx)>=14 else None
        meta['reasons']=['FULL_DAY_EXPOSURE_PROXY'] if v is not None else ['INSUFFICIENT_VERIFIED_EXPOSURE']
    else:
        qualified=len(idx)>=28 and sum(u[i]>0 for i in idx)>=7 and strict.count(None)<=84/10
        v=raw+F('.5')*max(F(0),min(verified,F('1.5')*raw)-raw) if qualified else raw
        meta['reasons']=['EXPOSURE_GATED_UPLIFT'] if qualified else ['EXPLICIT_RAW84_FALLBACK']
    return {'model':model,'version':VERSION,'parameters':PARAMETERS[model],'rate':str(v) if v is not None else None,
        'demand28':str(v*28) if v is not None else None,'demand56':str(v*56) if v is not None else None,
        **meta,'limitations':row.get('limitations',[]),'uncertainty':{'interval':None,'reason':'No prospectively calibrated prediction interval; point estimate only'},'availability_assumption':'Expected demand if available; substitutions not identified'}

def current_without_stockouts(row):
    return calculate('B_CURRENT',{**row,'availability':['UNKNOWN']*84})
