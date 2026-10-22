"""Mature outcomes only. Immutable revisions; no stockout sales as demand truth."""
from datetime import date,datetime,timedelta,timezone
from fractions import Fraction as F
from collections import defaultdict
from .contracts import MODELS,stamp,TZ,number,EvidenceError

def metrics(cases):
    n=len(cases);actual=sum((F(c['actual']) for c in cases),F(0));ae=sum((abs(F(c['predicted'])-F(c['actual'])) for c in cases),F(0));bias=sum((F(c['predicted'])-F(c['actual']) for c in cases),F(0))
    known=[c for c in cases if c['cost'] is not None];ac=sum((F(c['actual'])*F(c['cost']) for c in known),F(0));ce=sum((abs(F(c['predicted'])-F(c['actual']))*F(c['cost']) for c in known),F(0));cb=sum(((F(c['predicted'])-F(c['actual']))*F(c['cost']) for c in known),F(0))
    return {'n':n,'actual_units':float(actual),'mae':float(ae/n) if n else None,'wape':float(ae/actual*100) if actual else None,'bias_units':float(bias),'bias_percent':float(bias/actual*100) if actual else None,'over_frequency':sum(F(c['predicted'])>F(c['actual']) for c in cases)/n if n else None,'under_frequency':sum(F(c['predicted'])<F(c['actual']) for c in cases)/n if n else None,'cost_sample_n':len(known),'cost_mae':float(ce/len(known)) if known else None,'cost_wape':float(ce/ac*100) if ac else None,'cost_bias':float(cb),'cost_bias_percent':float(cb/ac*100) if ac else None}

def evaluate(store,run_id,outcome,*,now=None):
    try:
        run=store.get_run(run_id)
        if run is None:raise EvidenceError('Forecast not found')
        original=run['payload'];snapshot=original['snapshot'];horizon=outcome['horizon']
        if horizon not in (28,56):raise EvidenceError('Only 28/56-day horizons are supported')
        start=date.fromisoformat(snapshot['evaluation_start']);end=start+timedelta(days=horizon);observed=stamp(outcome['observed_at']);cutoff=stamp(outcome['cutoff'])
        if observed>stamp(now or datetime.now(timezone.utc)) or cutoff>observed:raise EvidenceError('Future outcome capture')
        if min(observed,cutoff)<datetime.combine(end,datetime.min.time(),TZ):raise EvidenceError('Evaluation period has not concluded')
        if not outcome.get('revision'):raise EvidenceError('Explicit source revision required')
        previous=store.list_evaluations(run_id)
        parent=outcome.get('supersedes_id')
        if parent and not any(e['id']==parent and e['horizon']==horizon for e in previous):raise EvidenceError('Revision parent must be an evaluation of this run and horizon')
        rows={(r['store_id'],r['variation_id']):r for r in outcome['rows']}
        if len(rows)!=len(outcome['rows']):raise EvidenceError('Duplicate outcome identity')
        expected={(r['store_id'],r['variation_id']) for r in snapshot['rows']}
        if set(rows)-expected:raise EvidenceError('Outcome contains a different scope')
        inputs={(r['store_id'],r['variation_id']):r for r in snapshot['rows']};cases=[];coverage=[];abstentions={m:0 for m in MODELS}
        for forecast in original['forecasts']:
            key=(forecast['store_id'],forecast['variation_id']);source=rows.get(key,{});days={}
            for r in source.get('days',[]):
                d=date.fromisoformat(r['date'])
                if not start<=d<end or d in days:raise EvidenceError('Outcome day outside period or duplicated')
                if r['availability'] not in ('IN_STOCK','STOCKOUT','PARTIAL','UNKNOWN'):raise EvidenceError('Invalid outcome availability')
                if r.get('units') is not None:number(r['units'],nonnegative=True)
                days[d]=r
            for r in source.get('source_records',[]):
                if not r.get('id') or not r.get('source') or 'data' not in r or 'version' not in r or stamp(r['known_at'])>cutoff or stamp(r['occurred_at'])>cutoff:raise EvidenceError('Outcome source revision exceeds capture cutoff')
            if source.get('complete') and any(number(r['units'])>0 for r in days.values() if r.get('units') is not None) and not source.get('source_records'):raise EvidenceError('Observed positive sales require retained source records')
            complete=source.get('complete') is True and len(days)==horizon and all(r.get('units') is not None for r in days.values())
            reliable=complete and all(r['availability']!='UNKNOWN' for r in days.values())
            eligible=[r for r in days.values() if r['availability']=='IN_STOCK' and r.get('units') is not None]
            classification='CONTINUOUS' if reliable and len(eligible)==horizon else 'PARTIAL' if reliable else 'UNKNOWN'
            # Unknown scope cannot enter any definitive metric, even if some days appear available.
            scored=reliable and len(eligible)>=7
            rates={m:forecast['models'][m]['rate'] for m in MODELS}
            for m,v in rates.items():abstentions[m]+=v is None
            common=scored and all(v is not None for v in rates.values())
            coverage.append({'store_id':key[0],'variation_id':key[1],'class':classification,'known_days':len(eligible),'expected_days':horizon,'common_eligible':common})
            if common:
                actual=sum((F(number(r['units'])) for r in eligible),F(0));meta=inputs[key]
                for m,v in rates.items():
                    cases.append({'model':m,'store':key[0],'sku':meta['sku'],'variation_id':key[1],'category':meta.get('category','UNKNOWN'),'vendor':str(meta.get('vendor_id') or 'UNKNOWN'),'class':classification,'actual':str(actual),'predicted':str(F(v)*len(eligible)),'cost':meta.get('unit_cost')})
        groups=[]
        for dimension in ('class','store','category','vendor','sku'):
            for value in sorted({str(c[dimension]) for c in cases}):
                for m in MODELS:
                    for cohort in ('CONTINUOUS','PARTIAL'):
                        selected=[c for c in cases if c['model']==m and str(c[dimension])==value and c['class']==cohort]
                        groups.append({'dimension':dimension,'value':value,'cohort':cohort,'model':m,**metrics(selected)})
        result={'schema':'forecast-evaluation-v1','outcome':outcome,'cases':cases,'metrics':groups,'coverage':coverage,'abstentions':abstentions,'population':len(expected),'model_version':original['model_version'],'classification':'COMPLETE' if all(c['class']!='UNKNOWN' for c in coverage) else 'INCOMPLETE','recommendation':'No model is automatically promoted; sample sizes, uncertainty and cohort selection must be reviewed','prospective':snapshot.get('kind','PROSPECTIVE')=='PROSPECTIVE'}
        ident=store.add_evaluation(run_id,horizon,str(outcome['revision']),result);store.attempt('evaluation',result['classification'],ident);return ident
    except Exception as exc:
        store.attempt('evaluation','FAILED',type(exc).__name__+': '+str(exc));raise
