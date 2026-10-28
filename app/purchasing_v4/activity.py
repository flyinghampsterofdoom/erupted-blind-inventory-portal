"""Read-only recorded activity investigation. Activity never changes eligibility."""
from collections import Counter,defaultdict
from dataclasses import dataclass
from datetime import date,datetime,time,timedelta
from decimal import Decimal
from sqlalchemy import text
from .contracts import Availability,EvidenceState
from .evidence_rules import BUSINESS_ZONE,coverage_status,inventory_observation
from .isolation import require_read_only
from .identity import digest

HORIZONS=(30,60,90,180)
SOURCES=('sales','receipts','inventory','purchasing')

@dataclass(frozen=True)
class ActivityEvent:
    source: str
    variation_id: str
    day: date
    units: Decimal | None
    reference: str


def classify(events,coverage,*,cutoff):
    """Positive recorded units are evidence of activity, never a reorder decision."""
    streams={}
    for source in SOURCES:
        values=[e for e in events if e.source==source and e.day<cutoff]
        positive=[e for e in values if e.units is not None and e.units>0]
        windows={}
        for days in HORIZONS:
            recent=[e for e in values if e.day>=cutoff-timedelta(days=days)]
            pos=[e for e in recent if e.units is not None and e.units>0]
            complete=coverage.get((source,days),False)
            windows[str(days)]=dict(observation_count=len(recent),positive_records=len(pos),positive_units=str(sum((e.units for e in pos),Decimal())),
                coverage_qualified=complete,state='VERIFIED_POSITIVE' if pos else 'CONFIRMED_NO_RECORDED_ACTIVITY' if complete else 'UNKNOWN')
        streams[source]=dict(windows=windows,last_positive_day=max((e.day.isoformat() for e in positive),default=None),
                             last_observation_day=max((e.day.isoformat() for e in values),default=None))
    positive30=any(s['windows']['30']['positive_records'] for s in streams.values())
    positive180=any(s['windows']['180']['positive_records'] for s in streams.values())
    if positive30:cohort='VERIFIED_RECENT_ACTIVITY'
    elif positive180:cohort='INTERMITTENT_OBSERVED_ACTIVITY'
    elif all(s['windows']['180']['coverage_qualified'] for s in streams.values()):cohort='NO_MEANINGFUL_RECENT_RECORDED_ACTIVITY'
    else:cohort='INSUFFICIENT_EVIDENCE'
    return dict(cohort=cohort,streams=streams,positive_transaction_30_days=any(streams[s]['windows']['30']['positive_records'] for s in ('sales','receipts','purchasing')),
                no_positive_observed_180_days=not positive180,
                longer_term='OBSERVED_ACTIVITY_ONLY_BEFORE_180_DAYS' if not positive180 and any(s['last_positive_day'] for s in streams.values()) else 'SEE_DATED_EVIDENCE',
                policy_changed=False)


def investigate(connection,*,as_of,max_age,source_namespace,availability_snapshot):
    require_read_only(connection)
    if as_of.utcoffset() is None or availability_snapshot.as_of!=as_of:raise ValueError('Coherent aware cutoff required')
    cutoff=as_of.astimezone(BUSINESS_ZONE).date()
    products=connection.execute(text('''SELECT r.variation_id::text AS id,r.square_variation_id,c.item_name,c.variation_name,c.sku,c.square_is_deleted
      FROM purchasing_v4.variation_reference r JOIN public.ordering_catalog_identity c USING(square_variation_id)
      WHERE r.source_namespace=:ns ORDER BY r.variation_id'''),{'ns':source_namespace}).mappings().all()
    by_square={p['square_variation_id']:p['id'] for p in products};events=defaultdict(list);uncertainty=defaultdict(set)
    stores=connection.execute(text('SELECT id,square_location_id FROM public.stores WHERE active ORDER BY id')).mappings().all()
    locations=[s['square_location_id'] for s in stores];location_counts=Counter(locations)
    coverage={(r['source'],r['location_id']):r for r in connection.execute(text('SELECT * FROM public.purchasing_history_coverage')).mappings()}
    failed=connection.execute(text("SELECT last_result='FAILED' FROM public.consignment_sales_sync_state WHERE id=1")).scalar_one_or_none() or False
    # Corrections replace facts; future corrections cannot leak revised quantities into this cutoff.
    sales=connection.execute(text('''WITH effective AS (
      SELECT f.square_variation_id AS vid,f.business_date AS day,f.quantity_sold AS units,'consignment_sale_facts:'||f.id AS reference
      FROM public.consignment_sale_facts f WHERE f.source_synchronized_at<=:asof
      AND NOT EXISTS(SELECT 1 FROM public.purchasing_sale_corrections c WHERE c.kind='LINE' AND c.square_order_id=f.square_order_id AND c.square_line_item_uid=f.square_line_item_uid)
      UNION ALL SELECT c.variation_id,c.business_date,c.quantity,'purchasing_sale_corrections:'||c.square_order_id||'/'||c.square_line_item_uid
      FROM public.purchasing_sale_corrections c WHERE c.kind='LINE' AND c.synchronized_at<=:asof)
      SELECT vid,day,sum(units) AS units,count(*) AS lines,min(reference) AS sample_reference FROM effective
      WHERE day<:cutoff AND units>=0 GROUP BY vid,day ORDER BY vid,day'''),dict(asof=as_of,cutoff=cutoff)).mappings()
    for r in sales:
        if r['vid'] in by_square:
            if r['units'].is_finite() and r['units']>=0:events[by_square[r['vid']]].append(ActivityEvent('sales',by_square[r['vid']],r['day'],r['units'],r['sample_reference']))
            else:uncertainty[by_square[r['vid']]].add('INVALID_SALES_QUANTITY')
    future=connection.execute(text('''SELECT DISTINCT c.variation_id AS vid FROM purchasing_sale_corrections c WHERE c.synchronized_at>:asof
      UNION SELECT DISTINCT f.square_variation_id FROM consignment_sale_facts f WHERE source_synchronized_at>:asof
      UNION SELECT DISTINCT f.square_variation_id FROM consignment_sale_facts f JOIN purchasing_sale_corrections c
       ON c.kind='LINE' AND c.square_order_id=f.square_order_id AND c.square_line_item_uid=f.square_line_item_uid WHERE c.synchronized_at>:asof'''),dict(asof=as_of)).scalars()
    for vid in future:
        if vid in by_square:uncertainty[by_square[vid]].add('SALES_SOURCE_CHANGED_AFTER_CUTOFF')
    receipts=connection.execute(text('''SELECT l.id,p.variation_id,l.received_qty,r.received_at,greatest(l.created_at,r.updated_at) AS observed_at FROM public.purchase_order_receipt_lines l
      JOIN public.purchase_order_receipts r ON r.id=l.receipt_id JOIN public.purchase_order_lines p ON p.id=l.purchase_order_line_id
      WHERE r.status='SUBMITTED' AND l.received_qty>0 ORDER BY l.id''')).mappings()
    dated_receipt_products=set()
    for r in receipts:
        vid=by_square.get(r['variation_id'])
        if not vid:continue
        if r['received_at'] is not None and r['received_at']<=as_of and r['observed_at']<=as_of:
            events[vid].append(ActivityEvent('receipts',vid,r['received_at'].astimezone(BUSINESS_ZONE).date(),Decimal(r['received_qty']),'purchase_order_receipt_lines:'+str(r['id'])));dated_receipt_products.add(vid)
        else:uncertainty[vid].add('UNDATED_OR_FUTURE_RECEIPT')
    undated=connection.execute(text('''SELECT DISTINCT variation_id FROM public.purchase_order_lines WHERE received_qty_total>0''')).scalars()
    for square in undated:
        vid=by_square.get(square)
        if vid and vid not in dated_receipt_products:uncertainty[vid].add('UNDATED_LEGACY_AGGREGATE_RECEIPTS')
    purchases=connection.execute(text('''SELECT l.id,l.variation_id,l.ordered_qty,p.submitted_at,p.created_at,p.status,greatest(p.updated_at,l.updated_at) AS observed_at FROM public.purchase_order_lines l
      JOIN public.purchase_orders p ON p.id=l.purchase_order_id WHERE l.ordered_qty>0 AND NOT l.removed ORDER BY l.id''')).mappings()
    for r in purchases:
        vid=by_square.get(r['variation_id'])
        if not vid:continue
        if r['submitted_at'] is not None and r['submitted_at']<=as_of and r['observed_at']<=as_of and r['status']!='DRAFT':
            events[vid].append(ActivityEvent('purchasing',vid,r['submitted_at'].astimezone(BUSINESS_ZONE).date(),Decimal(r['ordered_qty']),'purchase_order_lines:'+str(r['id'])))
        else:uncertainty[vid].add('UNSUBMITTED_OR_UNDATED_PURCHASING_RECORD')
    inv=connection.execute(text('''SELECT i.*,r.result,r.started_at,r.completed_at FROM public.ordering_current_inventory i
      JOIN public.ordering_inventory_refresh_runs r ON r.id=i.refresh_run_id''')).mappings()
    inventory_states=Counter()
    for r in inv:
        vid=by_square.get(r['square_variation_id'])
        if not vid:continue
        observation=inventory_observation(r,r['square_location_id'] if location_counts[r['square_location_id']]==1 else None,
            variation_id=r['square_variation_id'],store_id=r['store_id'],as_of=as_of,max_age=max_age)
        inventory_states[observation.state.value]+=1
        if r['refreshed_at']<=as_of:
            events[vid].append(ActivityEvent('inventory',vid,r['refreshed_at'].astimezone(BUSINESS_ZONE).date(),observation.value if observation.state==EvidenceState.KNOWN else None,'ordering_current_inventory:'+r['square_variation_id']+'/'+str(r['store_id'])))
    available_by_product=defaultdict(list)
    for row in availability_snapshot.rows:available_by_product[row.variation_id].append(row)
    results=[];cohort_availability=defaultdict(Counter)
    for p in products:
        vid=p['id'];certified={}
        for horizon in HORIZONS:
            start=datetime.combine(cutoff-timedelta(days=horizon),time(),BUSINESS_ZONE);end=datetime.combine(cutoff,time(),BUSINESS_ZONE)
            certified[('sales',horizon)]=(bool(stores) and all(loc and location_counts[loc]==1 and coverage_status(coverage.get(('SALES',loc)),failed,
                start=start,end=end,as_of=as_of,max_age=max_age)[0]==EvidenceState.KNOWN for loc in locations)
                and not uncertainty[vid].intersection({'SALES_SOURCE_CHANGED_AFTER_CUTOFF','INVALID_SALES_QUANTITY'}))
            # No receipt/inventory completeness certificates exist in this retained source.
            certified[('receipts',horizon)]=False;certified[('inventory',horizon)]=False
            certified[('purchasing',horizon)]=not any(x.startswith('UNSUBMITTED_OR_UNDATED') for x in uncertainty[vid])
        activity=classify(events[vid],certified,cutoff=cutoff)
        availability={}
        for horizon in HORIZONS:
            counts=Counter(d.availability.value for row in available_by_product[vid] for d in row.history if cutoff-timedelta(days=horizon)<=d.day<cutoff)
            pairs=sum(sum(d.availability==Availability.AVAILABLE and d.sales.state==EvidenceState.KNOWN for d in row.history if cutoff-timedelta(days=horizon)<=d.day<cutoff)>=14 for row in available_by_product[vid])
            availability[str(horizon)]=dict(counts=counts,product_store_pairs_with_14_available_days=pairs)
            cohort_availability[(activity['cohort'],horizon)].update(counts)
        results.append(dict(p,**activity,uncertainty=sorted(uncertainty[vid]),availability=availability))
    return dict(as_of=as_of.isoformat(),max_age_seconds=max_age.total_seconds(),source_namespace=source_namespace,
        methodology='Completed LA dates; strictly positive verified recorded units, no volume threshold. Intermittent means observed activity31..180 days ago, not inferred seasonality. No physical inactivity is inferred from missing records. Inventory presence, sales, receipts and purchasing are reported independently. No policy/status changes.',
        coverage_limitations=['No exhaustive receipt/inventory history certificates; undated legacy aggregate receipts cannot be placed into a recent window.',
         'Current inventory is an observation, not proof of demand. Purchasing and receipts do not authorize replenishment.',
         'Verified dormant cohort may be empty because complete negative evidence is required. Absence-of-positive proxy is labeled separately.'],
        cohort_counts=dict(Counter(r['cohort'] for r in results)),inventory_observation_states=dict(inventory_states),
        cohort_availability=[dict(cohort=c,horizon_days=h,counts=dict(v)) for (c,h),v in sorted(cohort_availability.items())],products=results,
        status_mutations=0,policy_mutations=0,input_availability_digest=availability_snapshot.digest)
