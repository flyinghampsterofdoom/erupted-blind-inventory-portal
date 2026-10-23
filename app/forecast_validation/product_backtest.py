"""Strict rolling-origin evaluation of retained as-of snapshots, never reconstructed live inputs."""
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction as F
from .contracts import MODELS, validate, stamp, TZ
from .demand import calculate
from .product import HORIZONS

def rolling(snapshots, outcomes):
    """Outcomes keyed (store, variation, date) with sales, exposure, complete.

    Every training snapshot must satisfy the existing evidence contract, including
    known_at <= cutoff and exactly 84 completed days. A later catalog/stock snapshot
    is never substituted. Only uncensored, fully observed outcome windows are scored.
    The origin day is excluded because this system starts evaluation next midnight.
    """
    scores=defaultdict(lambda: {'n':0,'absolute_error':F(0),'actual':F(0),'bias':F(0)})
    skipped=defaultdict(int)
    for snapshot in sorted(snapshots,key=lambda s:s['origin']):
        validate(snapshot, now=stamp(snapshot['origin']))
        start=date.fromisoformat(snapshot['evaluation_start'])
        for row in snapshot['rows']:
            estimates={m:calculate(m,row) for m in MODELS}
            band='high' if sum(F(str(x)) for x in row['sales'])/84>=1 else 'low'
            for h in HORIZONS:
                window=[outcomes.get((str(row['store_id']),row['variation_id'],(start+timedelta(days=i)).isoformat())) for i in range(h)]
                if any(x is None or not x.get('complete') for x in window):
                    skipped['missing_outcomes_'+str(h)]+=1;continue
                if any(x['exposure']!='IN_STOCK' for x in window):
                    skipped['censored_outcomes_'+str(h)]+=1;continue
                actual=sum(F(str(x['sales'])) for x in window)
                for m,estimate in estimates.items():
                    if estimate['rate'] is None:skipped['abstention_'+m]+=1;continue
                    error=F(estimate['rate'])*h-actual
                    for dimension,value in [('all','all'),('store',str(row['store_id'])),('category',row.get('category') or 'Unknown'),('volume',band)]:
                        score=scores[(m,h,dimension,value)];score['n']+=1;score['absolute_error']+=abs(error);score['bias']+=error;score['actual']+=actual
    return {'scores':[{'model':m,'horizon':h,'dimension':d,'value':v,'n':s['n'],
                       'mae':float(s['absolute_error']/s['n']), 'bias':float(s['bias']/s['n']),
                       'wape':float(s['absolute_error']/s['actual']) if s['actual'] else None}
                      for (m,h,d,v),s in scores.items()], 'skipped':dict(skipped),
            'winner':None,'stockout_utility':None,'purchasing_utility':None,
            'limitations':['Uncensored sales error only; no counterfactual lost sales or purchasing savings estimate.',
                           'Stockout and purchase utility require historical positions, dated arrivals and outcome evidence.']}
