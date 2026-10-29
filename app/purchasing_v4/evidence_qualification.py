"""Source witnesses and independent diagnostics over the accepted evidence loader.

No source refresh, numerical default, lost-sale estimate or purchasing recommendation.
"""
from collections import Counter
from datetime import datetime,time,timedelta
from uuid import UUID,uuid5
import json
from sqlalchemy import text
from .identity import digest
from .isolation import require_isolated,require_read_only
from .evidence_rules import inventory_observation,coverage_status,BUSINESS_ZONE,INVENTORY_SQL,verified_partial_pair
from .calculation_source import load_snapshot

WITNESS_NAMESPACE=UUID('ed517329-a23f-4afd-97ae-e24e046704ce')


def operational_status(connection):
    """Dates and source outcomes only; no hidden freshness or forecast defaults."""
    require_read_only(connection)
    observations=[dict(r) for r in connection.execute(text('''SELECT r.result,i.freshness_state,count(*) AS observed_pairs,
      min(i.refreshed_at) AS earliest_observation,max(i.refreshed_at) AS latest_observation,
      min(i.source_calculated_at) AS earliest_source_calculation,max(i.source_calculated_at) AS latest_source_calculation,
      count(*) FILTER(WHERE i.source_calculated_at IS NULL) AS missing_calculation_timestamps
      FROM public.ordering_current_inventory i JOIN public.ordering_inventory_refresh_runs r ON r.id=i.refresh_run_id
      GROUP BY r.result,i.freshness_state ORDER BY 1,2''')).mappings()]
    runs=[dict(r) for r in connection.execute(text('''SELECT id,result,expected_pair_count,covered_pair_count,missing_pair_count,
      square_request_count,started_at,completed_at,error_code,error_summary FROM public.ordering_inventory_refresh_runs
      ORDER BY completed_at DESC,id DESC LIMIT 10''')).mappings()]
    coverage=[dict(r) for r in connection.execute(text('SELECT source,location_id,start_at,through_at,verified_at FROM public.purchasing_history_coverage ORDER BY source,location_id')).mappings()]
    sync=connection.execute(text("""SELECT last_result,last_attempted_at,last_successful_start_at,
      last_successful_through_at,last_successful_at FROM public.consignment_sales_sync_state WHERE id=1""")).mappings().one_or_none()
    return dict(observations=observations,recent_inventory_runs=runs,history_coverage=coverage,sales_source_status=dict(sync) if sync else None,
      numerical_defaults_installed=False,source_refresh_executed=False,
      interpretation='An observation timestamp is distinct from Square calculation time. Missing pairs remain unknown. Partial rows require exact retained pair proof. Coverage must span the requested window and qualified anchor.')


def capture_inventory(connection,*,source_namespace):
    require_isolated(connection)
    if not isinstance(source_namespace,str) or not source_namespace.strip():raise ValueError('Explicit observed source namespace required')
    rows=connection.execute(text('''SELECT i.*,r.result,r.started_at,r.completed_at
      FROM public.ordering_current_inventory i JOIN public.ordering_inventory_refresh_runs r ON r.id=i.refresh_run_id
      ORDER BY i.square_variation_id,i.store_id''')).mappings()
    count=0;inserted=0
    for row in rows:
        payload=json.loads(json.dumps(dict(row),default=str));sha=digest(payload);identity=str(uuid5(WITNESS_NAMESPACE,source_namespace+':'+sha));count+=1
        inserted+=connection.execute(text('''INSERT INTO purchasing_v4.inventory_witness(id,source_namespace,source_digest,square_variation_id,store_id,observed_at,source_calculated_at,payload)
          VALUES (:id,:ns,:sha,:vid,:store,:seen,:calculated,CAST(:payload AS jsonb)) ON CONFLICT(source_namespace,source_digest) DO NOTHING'''),dict(id=identity,ns=source_namespace,sha=sha,vid=row['square_variation_id'],store=row['store_id'],seen=row['refreshed_at'],calculated=row['source_calculated_at'],payload=json.dumps(payload))).rowcount
    return dict(observed_rows=count,inserted_witnesses=inserted,source_mutations=0,identity_or_policy_mutations=0)


def investigate(connection,configuration,*,source_namespace,as_of,max_age):
    require_read_only(connection)
    snapshot=load_snapshot(connection,configuration,source_namespace=source_namespace,as_of=as_of,max_age=max_age)
    from .activity import investigate as activity_report
    activity=activity_report(connection,source_namespace=source_namespace,as_of=as_of,max_age=max_age,availability_snapshot=snapshot)
    observed_recent={p['id'] for p in activity['products'] if p['streams']['sales']['windows']['30']['positive_records']>0}
    observed_active={p['id'] for p in activity['products'] if p['cohort']=='VERIFIED_RECENT_ACTIVITY'}
    inventory={(r['square_variation_id'],str(r['store_id'])):dict(r) for r in connection.execute(text(INVENTORY_SQL)).mappings()}
    references=dict(connection.execute(text('SELECT variation_id::text,square_variation_id FROM purchasing_v4.variation_reference WHERE source_namespace=:ns'),{'ns':source_namespace}).all())
    stores={str(r['id']):r['square_location_id'] for r in connection.execute(text('SELECT id,square_location_id FROM public.stores WHERE active')).mappings()}
    counts=Counter(stores.values());locations={sid:loc if loc and counts[loc]==1 else None for sid,loc in stores.items()}
    coverage={(r['source'],r['location_id']):dict(r) for r in connection.execute(text('SELECT * FROM public.purchasing_history_coverage')).mappings()}
    failed=connection.execute(text("SELECT last_result='FAILED' FROM public.consignment_sales_sync_state WHERE id=1")).scalar_one_or_none() or False
    pairs=[];issues=Counter();recent=set();qualified=set()
    for row in snapshot.rows:
        vid=references[row.variation_id];source=inventory.get((vid,row.store_id));location=locations[row.store_id];flags=[]
        hand=inventory_observation(source,location,variation_id=vid,store_id=row.store_id,as_of=as_of,max_age=max_age)
        if source is None:flags.append('MISSING_INVENTORY_OBSERVATION')
        else:
            if as_of-source['refreshed_at']>max_age:flags.append('STALE_INVENTORY_OBSERVATION')
            if source['source_calculated_at'] is None or source['source_calculated_at']>source['refreshed_at']:flags.append('UNQUALIFIED_INVENTORY_ANCHOR')
            if (source['result']!='COMPLETE' and not verified_partial_pair(source,vid,row.store_id)) or source['freshness_state']!='FRESH':flags.append('INCOMPLETE_INVENTORY_REFRESH')
        parameters,_,_,_=configuration.resolve(row.category_id)
        begin=datetime.combine(as_of.astimezone(BUSINESS_ZONE).date()-timedelta(days=parameters.model.windows.lookback_days),time(),BUSINESS_ZONE)
        end=datetime.combine(as_of.astimezone(BUSINESS_ZONE).date(),time(),BUSINESS_ZONE)
        for stream in ['SALES','MOVEMENTS']:
            state,reason,_=coverage_status(coverage.get((stream,location)),failed,start=begin,end=max(end,hand.observed_at or begin) if stream=='MOVEMENTS' else end,as_of=as_of,max_age=max_age)
            if reason:flags.append(stream+':'+reason)
        available=sum(d.availability.value=='CONFIRMED_AVAILABLE' and d.sales.state.value=='KNOWN' for d in row.history);unknown=sum(d.availability.value=='UNKNOWN' for d in row.history)
        if unknown:flags.append('UNKNOWN_STOCKOUT_PERIODS')
        known_sales=[d for d in row.history if d.sales.value is not None]
        sales_unknown=any(d.sales.state.value!='KNOWN' for d in row.history)
        if sales_unknown:flags.append('SALES_HISTORY_QUALIFICATION_BLOCKED')
        elif len(known_sales)<parameters.model.windows.minimum_available_days:flags.append('GENUINE_INSUFFICIENT_CONFIRMED_SELLING_DAYS')
        active=any(d.day>=as_of.astimezone(BUSINESS_ZONE).date()-timedelta(days=30) and d.sales.value is not None and d.sales.value>0 for d in row.history)
        if active:recent.add(row.variation_id)
        if available>=parameters.model.windows.minimum_available_days:qualified.add(row.variation_id)
        for flag in flags:issues[flag]+=1
        pairs.append(dict(variation_id=row.variation_id,square_variation_id=vid,store_id=row.store_id,inventory_state=hand.state.value,inventory_reason=hand.reason,flags=flags,confirmed_available_days=available,unknown_availability_days=unknown,confirmed_sales_days=len(known_sales),selling_history_assessment='UNKNOWN_SOURCE_COVERAGE' if sales_unknown else 'MINIMUM_DAYS_NOT_MET' if len(known_sales)<parameters.model.windows.minimum_available_days else 'CONFIRMED_DAYS_AVAILABLE',recent_positive_sales=active))
    return dict(source_snapshot_digest=snapshot.digest,as_of=as_of.isoformat(),source_age_seconds=max_age.total_seconds(),candidate_configuration_digest=configuration.content_digest,configuration_persisted=False,policy_changes=0,catalog_variations=len(references),store_variation_pairs=len(pairs),recently_active_variations=len(observed_active),observed_recent_selling_variations=len(observed_recent),qualified_recent_selling_variations=len(recent),variations_with_minimum_confirmed_availability=len(qualified),recently_selling_without_qualified_availability=len(observed_recent-qualified),activity_cohorts=activity['cohort_counts'],reason_counts=dict(sorted(issues.items())),pairs=pairs)
