"""Reproducible isolated evaluation; all candidate values come from a named file.
No production discovery, writes, engine activation or policy persistence.
"""
import argparse
from collections import Counter,defaultdict
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
from fractions import Fraction
import hashlib,json,platform,resource,statistics,time
from pathlib import Path
from sqlalchemy import create_engine,event,text
from app.purchasing_v4.calculation import StoreInput,Supply,SupplyLot,ManualRule,recommend_batch
from app.purchasing_v4.calculation_config import StockoutControl
from app.purchasing_v4.calculation_documents import configuration_from_document
from app.purchasing_v4.calculation_serialization import canonical_bytes,evidence_digest,result_digest,primitive
from app.purchasing_v4.contracts import Availability,ExposureDay,QuantityEvidence,EvidenceState
from app.purchasing_v4.calculation_source import load_snapshot
from app.purchasing_v4.isolation import readonly_snapshot,require_isolated_url
from app.purchasing_v4.reconciliation import public_fingerprints,public_structure

ASOF=datetime(2026,10,27,12,tzinfo=timezone.utc)
COHORTS=('stable','rising_step','falling_step','rising_slope','falling_slope','intermittent','sparse_new',
         'stockouts','unknown_availability','complete_stockout','partial_availability','missing_recent',
         'confirmed_zero','negative_inventory','pending_supply','manual','no_reorder')

def quantity(value,reason=''):
    return QuantityEvidence(EvidenceState.KNOWN if value is not None else EvidenceState.UNKNOWN,
        Decimal(value) if value is not None else None,'deterministic-fixture',reason)


def observation(cohort,n,store):
    units=3*store
    if cohort=='rising_step':units=(2 if n<180 else 7)*store
    if cohort=='falling_step':units=(7 if n<180 else 2)*store
    if cohort=='rising_slope':units=(1+n//35)*store
    if cohort=='falling_slope':units=max(1,10-n//35)*store
    if cohort=='intermittent':units=12*store if n%9==0 else 0
    if cohort in ('confirmed_zero','complete_stockout'):units=0
    availability=Availability.AVAILABLE
    if cohort=='stockouts' and n%7 in (0,1,2):availability=Availability.STOCKOUT;units=0
    if cohort=='complete_stockout':availability=Availability.STOCKOUT
    if cohort=='unknown_availability' and n%3!=0:availability=Availability.UNKNOWN
    if cohort=='partial_availability' and n%4!=0:availability=Availability.STOCKOUT;units=0
    if cohort=='sparse_new' and n<250:availability=Availability.UNKNOWN
    return availability,units


def fixture(cohort,store,origin,product=None):
    first=date(2026,1,1)
    history=[]
    for n in range(max(0,origin-182),origin):
        availability,units=observation(cohort,n,store)
        sales=quantity(None,'SIMULATED_SOURCE_UNAVAILABLE') if cohort=='missing_recent' and n>=origin-28 else quantity(units)
        history.append(ExposureDay(first+timedelta(days=n),availability,sales))
    pending=cohort=='pending_supply'
    return StoreInput(product or cohort,str(store),None,tuple(history),quantity(-3 if cohort=='negative_inventory' else 8),
        Supply((SupplyLot('supply:'+str(store),'ARRIVAL_PENDING' if pending else 'VENDOR_OPEN',quantity(6),False),),(),(),True,'fixture-watermark'),
        5,cohort=='no_reorder',ManualRule('fixture-owner-manual',15,45) if cohort=='manual' else None,
        None,None,(),())


def synthetic(profiles):
    results=[];examples=[]
    for config in profiles:
        buckets=defaultdict(list);last={};stability=[];directions=defaultdict(list);states=Counter()
        for origin in range(182,267,14):
            asof=datetime.combine(date(2026,1,1)+timedelta(days=origin),datetime.min.time(),timezone.utc)+timedelta(hours=12)
            rows=tuple(fixture(c,s,origin) for c in COHORTS for s in range(1,5))
            batch=recommend_batch(rows,config,as_of=asof,input_digest=evidence_digest(rows,as_of=asof,source_exceptions=()))
            for row,rec in zip(rows,batch.recommendations):
                cohort=row.variation_id;states[rec.state]+=1
                b=buckets[cohort];forecast=rec.forecast.weekly_units
                future=[observation(cohort,n,int(row.store_id)) for n in range(origin,origin+28)]
                qualified=[u for a,u in future if a==Availability.AVAILABLE]
                outcome=Fraction(sum(qualified)*7,len(qualified)) if len(qualified)>=14 else None
                b.append((forecast,outcome,rec.forecast.state.startswith('SKIPPED')))
                key=(cohort,row.store_id)
                if rec.raw_need_units is not None and key in last:stability.append(abs(rec.suggested_packs-last[key]))
                if rec.raw_need_units is not None:last[key]=rec.suggested_packs
                f=rec.forecast
                if f.branch=='DISJOINT_ADAPTIVE' and f.historical.velocity!=f.recent.velocity:
                    directions[f.direction].append(float((forecast-f.historical.velocity)/(f.recent.velocity-f.historical.velocity)))
                if origin==210 and cohort in ('rising_step','falling_step','manual','unknown_availability','pending_supply'):
                    examples.append(dict(profile=config.version,cohort=cohort,result=primitive(rec)))
        reports={}
        for cohort,values in buckets.items():
            scored=[(f,o) for f,o,skip in values if f is not None and o is not None and not skip]
            eligible=sum(not skip for _,_,skip in values)
            forecasts=sum(f is not None for f,_,skip in values if not skip)
            reports[cohort]=dict(cases=len(values),forecast_eligible_cases=eligible,forecasts=forecasts,
                abstentions=eligible-forecasts,scored=len(scored),
                mean_error=float(sum((f-o for f,o in scored),Fraction())/len(scored)) if scored else None,
                mean_absolute_error=float(sum((abs(f-o) for f,o in scored),Fraction())/len(scored)) if scored else None,
                wape=float(sum(abs(f-o) for f,o in scored)/sum(o for _,o in scored)) if scored and sum(o for _,o in scored) else None)
        results.append(dict(profile=config.version,configuration_digest=config.content_digest,cohorts=reports,
            recommendation_states=dict(states),mean_absolute_pack_change=statistics.mean(stability) if stability else None,
            mean_fraction_of_observed_gap={k:statistics.mean(v) for k,v in directions.items()}))
    return dict(method='Deterministic synthetic longitudinal holdouts; seven origins 182..266 every14 days; four stores; 28 future days; minimum14 confirmed available future days. Unknown outcome is unscored, never zero. Profiles prespecified, no fitted parameters.',results=results,examples=examples)


def benchmark(config,count):
    start=time.perf_counter()
    rows=tuple(fixture('stable',store,266,product='BENCH_'+str(product)) for product in range(count) for store in range(1,5))
    build=time.perf_counter()-start
    asof=datetime(2026,9,24,12,tzinfo=timezone.utc)
    start=time.perf_counter();fingerprint=evidence_digest(rows,as_of=asof,source_exceptions=());digest_time=time.perf_counter()-start
    timings=[];digests=[];serialization=[]
    for _ in range(3):
        start=time.perf_counter();batch=recommend_batch(rows,config,as_of=asof,input_digest=fingerprint);timings.append(time.perf_counter()-start)
        start=time.perf_counter();digests.append(result_digest(batch));serialization.append(time.perf_counter()-start)
    assert len(set(digests))==1
    return dict(products=count,stores=4,rows=len(rows),history_days_per_row=182,exposure_rows=len(rows)*182,
        fixture_build_seconds=build,input_digest_seconds=digest_time,calculation_seconds=timings,
        median_seconds=statistics.median(timings),result_serialization_and_digest_seconds=serialization,max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        deterministic_result_digest=digests[0],engine_database_queries=0,engine_network_calls=0)


def retained(url,profiles,output,*,as_of):
    require_isolated_url(url);engine=create_engine(url);statements=[]
    @event.listens_for(engine,'before_cursor_execute')
    def record(conn,cursor,statement,parameters,context,executemany):statements.append(statement.split()[0])
    def foundation(conn):
        names=conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='purchasing_v4' ORDER BY tablename")).scalars().all()
        return {name:hashlib.sha256(conn.execute(text("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM purchasing_v4."+conn.dialect.identifier_preparer.quote(name)+' t')).scalar_one().encode()).hexdigest() for name in names}
    with readonly_snapshot(engine) as conn:
        foundation_before_all=foundation(conn)
        before=public_fingerprints(conn);structure=public_structure(conn)
        namespace=conn.execute(text('SELECT source_namespace FROM purchasing_v4.import_run ORDER BY created_at LIMIT 1')).scalar_one()
        foundation_before=conn.execute(text("SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text FROM purchasing_v4.identity_exception t")).scalar_one()
        start=time.perf_counter();count=len(statements)
        snapshot=load_snapshot(conn,profiles[0],as_of=as_of,max_age=timedelta(days=2),source_namespace=namespace)
        load_time=time.perf_counter()-start;actual=len(statements)-count
        reports=[]
        for profile in profiles:
            start=time.perf_counter();batch=recommend_batch(snapshot.rows,profile,as_of=as_of,input_digest=snapshot.digest,source_exceptions=snapshot.exceptions)
            reports.append(dict(profile=profile.version,seconds=time.perf_counter()-start,
                states=dict(Counter(r.state for r in batch.recommendations)),forecast_states=dict(Counter(r.forecast.state for r in batch.recommendations)),
                branches=dict(Counter(r.forecast.branch for r in batch.recommendations)),
                results_digest=result_digest(batch)))
        # Earlier cutoffs cannot use current mutable cache/certificates as historical evidence.
        historical=[]
        current={(r.variation_id,r.store_id):r for r in snapshot.rows}
        for offset in (28,56):
            cutoff=as_of-timedelta(days=offset)
            snap=load_snapshot(conn,profiles[0],as_of=cutoff,max_age=timedelta(days=2),source_namespace=namespace)
            batch=recommend_batch(snap.rows,profiles[0],as_of=cutoff,input_digest=snap.digest,source_exceptions=snap.exceptions)
            scored=[];qualified_outcomes=0
            from app.purchasing_v4.calculation_source import BUSINESS_ZONE
            start_day=cutoff.astimezone(BUSINESS_ZONE).date()
            for rec in batch.recommendations:
                outcome_rows=[d for d in current[(rec.variation_id,rec.store_id)].history
                              if start_day<=d.day<start_day+timedelta(days=28) and d.availability==Availability.AVAILABLE
                              and d.sales.state==EvidenceState.KNOWN and d.sales.value>=0]
                if len(outcome_rows)>=14:
                    qualified_outcomes+=1
                    if rec.forecast.weekly_units is not None:
                        actual=7*sum((Fraction(d.sales.value) for d in outcome_rows),Fraction())/len(outcome_rows)
                        scored.append((rec.forecast.weekly_units,actual))
            historical.append(dict(as_of=cutoff.isoformat(),forecast_states=dict(Counter(r.forecast.state for r in batch.recommendations)),
                qualified_future_outcomes=qualified_outcomes,scored_cases=len(scored),
                mean_absolute_error=float(sum(abs(f-o) for f,o in scored)/len(scored)) if scored else None,
                bias=float(sum(f-o for f,o in scored)/len(scored)) if scored else None,
                limitation='Only independently qualified future exposure is scored; observations after generation cutoff rejected from training.'))

        after=public_fingerprints(conn);structure_after=public_structure(conn)
        foundation_after=conn.execute(text("SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text FROM purchasing_v4.identity_exception t")).scalar_one()
        foundation_after_all=foundation(conn)
        assert before==after and structure==structure_after and foundation_before==foundation_after and foundation_before_all==foundation_after_all
    engine.dispose()
    (output/'public-before.json').write_bytes(canonical_bytes(before));(output/'public-after.json').write_bytes(canonical_bytes(after))
    (output/'mapping-exceptions.json').write_bytes(canonical_bytes([json.loads(e) for e in snapshot.mapping_exceptions]))
    unresolved=[primitive(r) for r in snapshot.rows if any(l.quantity.state!=EvidenceState.KNOWN for l in r.supply.incoming)]
    # Omit long history from exception report while preserving exact incoming references.
    for r in unresolved:r.pop('history')
    (output/'unresolved-incoming.json').write_bytes(canonical_bytes(unresolved))
    return dict(rows=len(snapshot.rows),source_namespace=namespace,data_queries=snapshot.data_queries,all_loader_statements=actual,
        snapshot_load_seconds=load_time,input_digest=snapshot.digest,evidence_summary=dict(snapshot.evidence_summary),
        mapping_exceptions=len(snapshot.mapping_exceptions),profiles=reports,historical_cutoffs=historical,
        as_of=as_of.isoformat(),preservation=dict(foundation_before=foundation_before_all,foundation_after=foundation_after_all,public_tables=len(before),public_rows=sum(t['rows'] for t in before.values()),unchanged=True,
                          structure_before=structure,structure_after=structure_after,identity_exceptions_unchanged=True),
        limitation='Retained isolated Phase1 snapshot; no production access. Sales facts alone cannot validate latent demand without confirmed availability. Historical quality metrics remain unavailable.')


def main():
    p=argparse.ArgumentParser();p.add_argument('--profiles',required=True);p.add_argument('--output',required=True)
    p.add_argument('--isolated-url');p.add_argument('--retained-as-of');p.add_argument('--benchmark-products',type=int,nargs='+',required=True);args=p.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    documents=json.loads(Path(args.profiles).read_text());profiles=[configuration_from_document(r) for r in documents['profiles']]
    variants=[]
    for config in profiles:
        for kind,threshold in (('minimum_available_share',Fraction(1,2)),('maximum_unknown_share',Fraction(1,4))):
            variants.append(replace(config,version=config.version+'-'+kind,global_parameters=replace(config.global_parameters,stockout=StockoutControl(kind,threshold))))
    (output/'synthetic-evaluation.json').write_bytes(canonical_bytes(synthetic(profiles+variants)))
    if args.isolated_url:(output/'retained-evaluation.json').write_bytes(canonical_bytes(retained(args.isolated_url,profiles,output,as_of=datetime.fromisoformat(args.retained_as_of))))
    benches=[benchmark(profiles[1],count) for count in args.benchmark_products]
    (output/'benchmark.json').write_bytes(canonical_bytes(dict(platform=platform.platform(),machine=platform.machine(),
        hardware='Mac15,11;14 logical CPUs;38654705664 bytes RAM',python=platform.python_version(),rss_units='bytes on Darwin; process lifetime high-water mark',results=benches)))

if __name__=='__main__':main()
