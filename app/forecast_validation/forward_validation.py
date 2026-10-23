"""Explicit offline prospective registration/evaluation. No collectors, timers or business writes."""
from datetime import datetime, date, timedelta, timezone
from fractions import Fraction as F
from pathlib import Path
import json
import os
import tempfile
from collections import Counter, defaultdict
from .contracts import MODELS, TZ, validate, stamp, digest, canonical, number, EvidenceError
from .product import HORIZONS
from .evaluation import metrics

def _append(directory,payload):
    """Content-addressed create-only records; retries reuse identical bytes, mutations reject."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    ident=digest(payload);path=directory/(ident+'.json');raw=canonical(payload).encode()
    fd,name=tempfile.mkstemp(prefix='.pending-',dir=directory)
    pending=Path(name)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        pending.chmod(0o400)
        try:os.link(pending,path)
        except FileExistsError:
            if path.read_bytes()!=raw:raise EvidenceError('Immutable record identity mismatch')
        directory_fd=os.open(directory,os.O_RDONLY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
    finally:pending.unlink(missing_ok=True)
    return ident

def read(directory,ident):
    if len(ident)!=64 or any(c not in '0123456789abcdef' for c in ident):raise EvidenceError('Invalid immutable identity')
    value=json.loads((Path(directory)/(ident+'.json')).read_bytes())
    if digest(value)!=ident:raise EvidenceError('Immutable record was changed')
    return value

def register(directory,forecast,*,now=None):
    """Freeze an existing genuine prospective forecast before any outcome period starts.

    CLI uses actual UTC time. `now` is solely for deterministic local tests. Registry
    receipts must subsequently be archived independently before production validation.
    """
    now=stamp(now or datetime.now(timezone.utc));snapshot=forecast['snapshot']
    if forecast.get('schema')!='forecast-result-v1' or snapshot.get('kind')!='PROSPECTIVE':raise EvidenceError('Genuine prospective forecast required')
    validate(snapshot,now=now)
    start=datetime.combine(date.fromisoformat(snapshot['evaluation_start']),datetime.min.time(),TZ)
    if now>=start:raise EvidenceError('Registration must precede the outcome window')
    expected={(str(r['store_id']),r['variation_id']) for r in snapshot['rows']}
    actual=[(str(r['store_id']),r['variation_id']) for r in forecast['forecasts']]
    if set(actual)!=expected or len(actual)!=len(expected):raise EvidenceError('Forecast scope mismatch')
    if any(set(f['models'])!=set(MODELS) for f in forecast['forecasts']):raise EvidenceError('Four unchanged model predictions required')
    artifact=_append(directory,forecast)
    receipt={'schema':'forward-registration-v1','forecast_id':artifact,'registered_at':now.isoformat(),
             'evaluation_start':snapshot['evaluation_start'],'horizons':list(HORIZONS),
             'receipt_custody':'LOCAL_ONLY; independent archive verification required before production acceptance'}
    return _append(directory,receipt)

def evaluate(directory,registration,outcome,*,now=None):
    """Score only mature windows and frozen predictions; retain censored effects separately.

    Daily records require explicit coverage and exposure proofs. Positive sales also
    require transaction source records. This local input contract does not itself
    certify an external source adapter or replace existing production evidence rules.
    """
    now=stamp(now or datetime.now(timezone.utc));receipt=read(directory,registration)
    if receipt.get('schema')!='forward-registration-v1':raise EvidenceError('Registration receipt required')
    forecast=read(directory,receipt['forecast_id']);snapshot=forecast['snapshot']
    validate(snapshot,now=stamp(receipt['registered_at']))
    if snapshot['kind']!='PROSPECTIVE':raise EvidenceError('Retrospective evidence is not prospective')
    start=date.fromisoformat(snapshot['evaluation_start']);h=outcome.get('horizon');end=start+timedelta(days=h) if type(h) is int else None
    if h not in HORIZONS or isinstance(h,bool):raise EvidenceError('Unsupported horizon')
    if stamp(receipt['registered_at'])>=datetime.combine(start,datetime.min.time(),TZ):raise EvidenceError('Late registration')
    if outcome.get('registration')!=registration or not outcome.get('revision'):raise EvidenceError('Explicit registration and source revision required')
    cutoff,observed=stamp(outcome['cutoff']),stamp(outcome['observed_at'])
    if not datetime.combine(end,datetime.min.time(),TZ)<=cutoff<=observed<=now:raise EvidenceError('Immature or future outcomes')
    inputs={(str(r['store_id']),r['variation_id']):r for r in snapshot['rows']}
    outcomes={}
    for row in outcome['rows']:
        key=(str(row['store_id']),row['variation_id'])
        if key not in inputs or key in outcomes:raise EvidenceError('Unknown or duplicate outcome scope')
        days={}
        for day in row['days']:
            d=date.fromisoformat(day['date'])
            if not start<=d<end or d in days:raise EvidenceError('Duplicated or out-of-window outcome')
            if day['availability'] not in ('IN_STOCK','STOCKOUT','PARTIAL','UNKNOWN'):raise EvidenceError('Explicit exposure required')
            if day.get('units') is not None:number(day['units'],nonnegative=True)
            records=day.get('source_records',[]);coverage=False;exposure=False;transactions=False
            for r in records:
                if not r.get('id') or not r.get('source') or 'version' not in r or 'data' not in r:raise EvidenceError('Retained source identity required')
                if stamp(r['known_at'])>cutoff or stamp(r['occurred_at'])>cutoff:raise EvidenceError('Source exceeds outcome cutoff')
                data=r['data']
                if r['source']=='daily_sales_coverage' and data.get('date')==day['date'] and data.get('complete') is True:
                    coverage=day.get('units') is not None and F(str(data['units']))==F(str(day['units']))
                if r['source']=='daily_exposure' and data.get('date')==day['date'] and data.get('availability')==day['availability']:exposure=True
                if r['source'] in ('square_order_line','square_line_correction'):transactions=True
            if day.get('complete') and (not coverage or not exposure):raise EvidenceError('Complete day lacks coverage/exposure proof')
            if day.get('complete') and day.get('units') is not None and F(str(day['units']))>0 and not transactions:raise EvidenceError('Positive sales lack transaction evidence')
            days[d]=day
        outcomes[key]=days
    groups=defaultdict(list);common=defaultdict(list);effects=[];coverage=Counter();abstentions=Counter()
    for fc in forecast['forecasts']:
        key=(str(fc['store_id']),fc['variation_id']);row=inputs[key];days=outcomes.get(key,{})
        full=len(days)==h and all(d.get('complete') is True and d.get('units') is not None for d in days.values())
        continuous=full and all(d['availability']=='IN_STOCK' for d in days.values())
        category='CONTINUOUS' if continuous else 'CENSORED' if full else 'INCOMPLETE';coverage[category]+=1
        effects.append({'store_id':key[0],'variation_id':key[1],'classification':category,
                        'stockout_days':sum(d['availability']=='STOCKOUT' for d in days.values()),
                        'unknown_or_partial_days':sum(d['availability'] in ('UNKNOWN','PARTIAL') for d in days.values()),
                        'observed_sales':str(sum((F(str(d['units'])) for d in days.values() if d.get('units') is not None),F(0))),
                        'lost_demand':None})
        all_models=all(fc['models'][m]['rate'] is not None for m in MODELS)
        for m in MODELS:
            rate=fc['models'][m]['rate'];abstentions[m]+=rate is None
            if not continuous or rate is None:continue
            case={'predicted':str(F(rate)*h),'actual':str(sum(F(str(d['units'])) for d in days.values())),'cost':row.get('unit_cost')}
            band='high' if sum(F(v) for v in row['sales'])/84>=1 else 'low'
            for dimension,value in [('all','all'),('store',key[0]),('category',row.get('category') or 'Unknown'),('volume',band)]:
                groups[(m,dimension,value)].append(case)
                if all_models:common[(m,dimension,value)].append(case)
    result={'schema':'forward-evaluation-v1','registration':registration,'outcome_id':_append(directory,outcome),
            'horizon':h,'coverage':dict(coverage),'abstentions':dict(abstentions),'stockout_effects':effects,
            'individual_cohorts':[{'model':m,'dimension':d,'value':v,**metrics(c)} for (m,d,v),c in groups.items()],
            'common_four_model_cohort':[{'model':m,'dimension':d,'value':v,**metrics(c)} for (m,d,v),c in common.items()],
            'winner':None,'automatic_promotion':False,'limitations':['WAPE and bias_percent are percentages; n and cohort membership must accompany comparisons.',
             'Incomplete/censored outcomes are excluded from demand error. Observed sales are a lower bound, not lost-demand truth.',
             'No stockout prevention or purchasing savings claim; requires dated arrivals, counterfactuals and independent custody acceptance.']}
    return _append(directory,result)
