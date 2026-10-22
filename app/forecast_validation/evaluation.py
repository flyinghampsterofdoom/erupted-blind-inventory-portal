"""Mature outcomes only. Immutable revisions; no stockout sales as demand truth."""
from datetime import date,datetime,timedelta,timezone
from fractions import Fraction as F
from collections import defaultdict
from .contracts import MODELS,stamp,TZ,number,EvidenceError

def metrics(cases):
    n=len(cases);actual=sum((F(c['actual']) for c in cases),F(0));ae=sum((abs(F(c['predicted'])-F(c['actual'])) for c in cases),F(0));bias=sum((F(c['predicted'])-F(c['actual']) for c in cases),F(0))
    known=[c for c in cases if c['cost'] is not None];ac=sum((F(c['actual'])*F(c['cost']) for c in known),F(0));ce=sum((abs(F(c['predicted'])-F(c['actual']))*F(c['cost']) for c in known),F(0));cb=sum(((F(c['predicted'])-F(c['actual']))*F(c['cost']) for c in known),F(0))
    return {'n':n,'actual_units':float(actual),'mae':float(ae/n) if n else None,'wape':float(ae/actual*100) if actual else None,'bias_units':float(bias),'bias_percent':float(bias/actual*100) if actual else None,'over_frequency':sum(F(c['predicted'])>F(c['actual']) for c in cases)/n if n else None,'under_frequency':sum(F(c['predicted'])<F(c['actual']) for c in cases)/n if n else None,'cost_sample_n':len(known),'cost_mae':float(ce/len(known)) if known else None,'cost_wape':float(ce/ac*100) if ac else None,'cost_bias':float(cb),'cost_bias_percent':float(cb/ac*100) if ac else None}

def evaluate(store,run_id,outcome,*,now=None,budget_seconds=120):
    """Score indexed immutable predictions; never load full forecast/history blobs."""
    import time
    from .shared import verify_outcome,outcome_revision
    began=time.monotonic()
    def bounded():
        if time.monotonic()-began>budget_seconds:
            raise TimeoutError('Outcome evaluation deadline exceeded; no result published')
    try:
        meta=store.get_run_meta(run_id)
        if meta is None:raise EvidenceError('Forecast not found')
        original=store.get_run_summary(run_id);snapshot=original['snapshot'];horizon=outcome['horizon']
        if horizon not in (28,56):raise EvidenceError('Only 28/56-day horizons are supported')
        start=date.fromisoformat(snapshot['evaluation_start']);end=start+timedelta(days=horizon)
        observed=stamp(outcome['observed_at']);cutoff=stamp(outcome['cutoff'])
        if observed>stamp(now or datetime.now(timezone.utc)) or cutoff>observed:raise EvidenceError('Future outcome capture')
        if min(observed,cutoff)<datetime.combine(end,datetime.min.time(),TZ):raise EvidenceError('Evaluation period has not concluded')
        if not outcome.get('revision'):raise EvidenceError('Explicit source revision required')
        rows={(str(r['store_id']),r['variation_id']):r for r in outcome['rows']}
        if len(rows)!=len(outcome['rows']):raise EvidenceError('Duplicate outcome identity')
        verify_outcome(store,snapshot,outcome)
        bounded()
        source_revision=outcome_revision(outcome)
        existing=store.find_evaluation_source(run_id,horizon,source_revision)
        if existing:return existing
        previous=store.list_evaluation_meta(run_id,horizon=horizon,limit=1)
        parent=outcome.get('supersedes_id')
        if previous and parent!=previous[0]['id']:raise EvidenceError('Changed outcome requires the latest explicit revision parent')
        if not previous and parent:raise EvidenceError('First outcome cannot name a revision parent')
        cases=[];coverage=[];abstentions={m:0 for m in MODELS};expected=set()
        for pair in store.iter_run_pairs(run_id):
            bounded()
            forecast=pair['forecast'];meta=pair['input'];key=(str(meta['store_id']),meta['variation_id'])
            expected.add(key);source=rows.get(key,{});days={}
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
            scored=reliable and len(eligible)>=7
            rates={m:forecast['models'][m]['rate'] for m in MODELS}
            for m,v in rates.items():abstentions[m]+=v is None
            common=scored and all(v is not None for v in rates.values())
            coverage.append({'store_id':key[0],'variation_id':key[1],'class':classification,'known_days':len(eligible),'expected_days':horizon,'common_eligible':common})
            if common:
                actual=sum((F(number(r['units'])) for r in eligible),F(0))
                for m,v in rates.items():
                    cases.append({'model':m,'store':key[0],'sku':meta['sku'],'variation_id':key[1],'category':meta.get('category','UNKNOWN'),'vendor':str(meta.get('vendor_id') or 'UNKNOWN'),'class':classification,'actual':str(actual),'predicted':str(F(v)*len(eligible)),'cost':meta.get('unit_cost')})
        if set(rows)-expected:raise EvidenceError('Outcome contains a different scope')
        # Partition once instead of scanning every case for every SKU/group.
        selected=defaultdict(list);values=defaultdict(set)
        for case in cases:
            for dimension in ('class','store','category','vendor','sku'):
                value=str(case[dimension]);values[dimension].add(value)
                selected[(dimension,value,case['model'],case['class'])].append(case)
        groups=[]
        for dimension in ('class','store','category','vendor','sku'):
            for value in sorted(values[dimension]):
                for model in MODELS:
                    for cohort in ('CONTINUOUS','PARTIAL'):
                        bounded()
                        groups.append({'dimension':dimension,'value':value,'cohort':cohort,'model':model,**metrics(selected[(dimension,value,model,cohort)])})
        result={'schema':'forecast-evaluation-v1','outcome':outcome,'cases':cases,'metrics':groups,'coverage':coverage,'abstentions':abstentions,'population':len(expected),'model_version':original['model_version'],'classification':'COMPLETE' if all(c['class']!='UNKNOWN' for c in coverage) else 'INCOMPLETE','recommendation':'No model is automatically promoted; sample sizes, uncertainty and cohort selection must be reviewed','prospective':snapshot.get('kind','PROSPECTIVE')=='PROSPECTIVE','source_revision':source_revision}
        bounded()
        ident=store.add_evaluation(run_id,horizon,str(outcome['revision']),result)
        store.attempt('evaluation',result['classification'],ident)
        return ident
    except Exception as exc:
        try:store.attempt('evaluation','FAILED',type(exc).__name__+': '+str(exc)[:500])
        except Exception as cleanup:exc.evaluation_attempt_error=type(cleanup).__name__
        raise
