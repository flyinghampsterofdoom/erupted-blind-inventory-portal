"""Independent offline rational verifier for a saved Purchasing V3 evidence snapshot.

No application imports. This is an audit oracle, never an operational engine.
"""
from fractions import Fraction as F
from statistics import median
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo

def ceil(x):return -((-x.numerator)//x.denominator)
def allocate(total,weights):
 weights={str(k):max(F(v),0) for k,v in weights.items()};den=sum(weights.values())
 if not den:return {k:total if k==min(weights,key=int) else 0 for k in weights}
 exact={k:total*v/den for k,v in weights.items()};result={k:int(v) for k,v in exact.items()}
 for k in sorted(weights,key=lambda k:(-(exact[k]-result[k]),int(k)))[:total-sum(result.values())]:result[k]+=1
 return result

def reference(product,obs,evidence,settings):
 hand=F(obs['current_inventory']) if obs['current_inventory'] is not None else None
 inbound=F(obs['incoming']) if obs['incoming'] is not None else None
 position=hand+inbound if hand is not None and inbound is not None and inbound>=0 else None
 start=date.fromisoformat(evidence['sales_start']);end=date.fromisoformat(evidence['sales_end'])
 days=[start+timedelta(days=i) for i in range((end-start).days+1)]
 points=[F(obs['sales'].get(d.isoformat(),'0')) for d in days]
 reasons=[]
 if position is None:reasons.append('INVENTORY_OR_INCOMING_UNKNOWN')
 if not obs['sales_complete']:reasons.append('SALES_COVERAGE_INCOMPLETE')
 if product['lifecycle']!='ACTIVE':reasons.append('LIFECYCLE_BLOCKED')
 if any(x<0 for x in points):reasons.append('INVALID_SALES')
 if not points:reasons.append('NO_HISTORY')
 result=dict(daily_velocity=None,quantity=None,raw_need=None,inventory_position=position,reorder_level=None,target_level=None,trend='UNKNOWN',reasons=reasons,evidence_days=len(points),spike_days=0,stockout_days=0)
 if reasons:return result
 if product.get('product_created_at'):
  born=datetime.fromisoformat(product['product_created_at']).astimezone(ZoneInfo('America/Los_Angeles')).date()
  age=max(0,(end-born).days+1);points=points[-age:] if age else []
 if not points:return {**result,'trend':'NEW','reasons':['NO_COMPLETED_PRODUCT_HISTORY'],'evidence_days':0}
 n=len(points);availability=obs['availability'][-n:]
 if len(availability)!=n:availability=[None]*n
 positives=[x for x in points if x>0]
 cap=max(4*median(positives),F(1)) if len(positives)>=7 and n>=28 else None
 clipped=[min(x,cap) if cap is not None else x for x in points]
 spikes=sum(a!=b for a,b in zip(points,clipped));stockouts=availability.count(False)
 def rate(start,end=None):
  values=clipped[start:end];flags=availability[start:end];eligible=[v for v,a in zip(values,flags) if a is not False]
  if not eligible:return None
  observed=sum(values)/len(values)
  return min(sum(eligible)/len(eligible),2*observed) if stockouts else observed
 if n<14 and not positives:return {**result,'trend':'NEW','reasons':['NO_ESTABLISHED_DEMAND'],'evidence_days':n}
 long=rate(-min(56,n))
 result.update(evidence_days=n,spike_days=spikes,stockout_days=stockouts)
 if long is None or (not positives and hand<=0 and True not in availability):return {**result,'reasons':['DEMAND_CENSORED_OR_NEW']}
 daily=long;trend='STABLE';reasons=[]
 if n<28:daily=long*F(n,n+7);trend='NEW';reasons.append('CONSERVATIVE_LIMITED_HISTORY')
 elif n>=42:
  prev=rate(-42,-14);w1=rate(-14,-7);w2=rate(-7)
  if all(x is not None for x in (prev,w1,w2)):
   recent=(w1+w2)/2
   if prev>0 and min(w1,w2)>prev*F(115,100):trend='GROWING';daily=min(recent*F(65,100)+long*F(35,100),long*F(16,10))
   elif prev>0 and max(w1,w2)<prev*F(85,100):trend='DECLINING';daily=recent*F(75,100)+long*F(25,100)
 if n>=28 and sum(points[-28:])==0 and False not in availability[-28:]:daily=F(0);trend='SLOW';reasons.append('NO_RECENT_OBSERVED_SALES')
 if spikes:reasons.append('ISOLATED_SPIKES_CAPPED')
 if stockouts:reasons.append('CONFIRMED_STOCKOUT_ADJUSTMENT')
 reorder_days=max(F(str(settings['reorder_weeks']))*7,F(product.get('supplier_lead_time_days') or 0))
 target_days=max(F(str(settings['target_weeks']))*7,reorder_days)
 level=ceil(daily*reorder_days);target=ceil(daily*target_days)
 need=ceil(max(F(target)-position,0)) if position<=level else 0
 result.update(daily_velocity=daily,quantity=need,raw_need=need,reorder_level=level,target_level=target,trend=trend,reasons=reasons)
 return result
