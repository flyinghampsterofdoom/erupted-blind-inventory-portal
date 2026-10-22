"""Explicit shadow worker; atomic four-model publication, bounded execution."""
from collections import defaultdict
from datetime import datetime,timezone
import time
from .contracts import MODELS,VERSION,validate,Conflict,digest,EvidenceError
from .demand import calculate,current_without_stockouts
from .policy import store_need,aggregate

def forecast(store,snapshot,*,now=None,budget_seconds=30,publication=None):
    started=time.monotonic()
    try:
        validate(snapshot,now=now)
        outputs=[];by_sku=defaultdict(list)
        for row in snapshot['rows']:
            if time.monotonic()-started>budget_seconds:raise TimeoutError('Forecast budget exceeded; no run published')
            results={model:calculate(model,row) for model in MODELS}
            policy={model:store_need(result['rate'],row) for model,result in results.items()}
            control=current_without_stockouts(row)
            item={'store_id':row['store_id'],'variation_id':row['variation_id'],'models':results,'policy':policy,'current_without_stockouts':control,'control_policy':store_need(control['rate'],row)}
            outputs.append(item);by_sku[row['variation_id']].append((row,item))
        purchases=[]
        for vid,rows in by_sku.items():
            packs={r['pack'] for r,_ in rows};costs={r.get('unit_cost') for r,_ in rows}
            if len(packs)!=1 or len(costs)!=1:raise EvidenceError('SKU cost/pack differs across stores')
            pack=next(iter(packs));cost=next(iter(costs))
            purchases.append({'variation_id':vid,'models':{m:aggregate([i['policy'][m] for _,i in rows],pack,cost) for m in MODELS},'current_without_stockouts':aggregate([i['control_policy'] for _,i in rows],pack,cost)})
        payload={'schema':'forecast-result-v1','snapshot':snapshot,'model_version':VERSION,'policy_version':'minmax-4-8-exact-v1','forecasts':outputs,'purchases':purchases,'limitations':['Shadow estimates only; no orders or inventory changed','No substitution or realized-savings estimate','Full-calendar-day exposure is not verified selling hours']}
        ident=store.add_run(payload,now=now,**({'publication':publication} if publication else {}))
        store.attempt('forecast','COMPLETE_WITH_ABSTENTIONS' if any(v['rate'] is None for r in outputs for v in r['models'].values()) else 'COMPLETE',ident)
        return ident
    except Exception as exc:
        try:store.attempt('forecast','FAILED',type(exc).__name__+': '+str(exc)[:500])
        except Exception as cleanup:exc.forecast_attempt_error=type(cleanup).__name__
        raise
