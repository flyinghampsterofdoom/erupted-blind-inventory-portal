"""Owner-requested manual comparisons. One source snapshot; no worksheet writes.

Compact immutable reports use the existing history_check evidence store. Pages
are filtered/sorted in PostgreSQL, never by loading the entire source again.
Only a fresh, explicit preview can be confirmed as an audited policy change.
"""
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
import json
from uuid import UUID,uuid4,uuid5
from sqlalchemy import text
from . import controls
from .calculation import _recommend, estimate
from .calculation_source import load_snapshot
from .contracts import EvidenceState
from .identity import digest
from .policy_projection import project_rows, verify_dependencies

FORMAT = 'manual-comparison-1'
SORTS = {'store_name','product','sku','on_hand','manual_par','manual_level',
         'engine_par','engine_level','weekly_units','manual_order','engine_order',
         'difference','cost_impact','available_days','eligible_for_conversion','pack_size'}
NUMERIC = SORTS - {'store_name','product','sku','eligible_for_conversion'}
FILTERS = {'all','reductions','increases','no_sales','high_target','high_stock',
           'stockout_risk','pack_mismatch','unresolved'}


def number(value):
    return None if value is None else float(value)


def money(value):
    # Costs contain whole units and source decimal costs, never rounded forecast units.
    return None if value is None else format(value, 'f')


def source_guard(c):
    """Short-lived preview guard; every relevant source table participates.

    Row-version checks catch updates, deletes and same-timestamp replacements;
    the retained long history is not serialized into Python a second time.
    """
    tables = ('ordering_current_inventory','ordering_inventory_refresh_runs',
              'purchasing_history_coverage','consignment_sale_facts',
              'purchasing_sale_corrections','purchasing_inventory_events',
              'purchasing_inventory_absolute_counts','consignment_sales_sync_state',
              'ordering_catalog_identity','ordering_product_lifecycle',
              'vendor_sku_configs','par_levels','stores','purchase_order_store_allocations',
              'purchase_order_lines','purchase_orders','purchase_order_receipt_allocations',
              'square_sync_events')
    result = {}
    for table in tables:
        if c.scalar(text('SELECT to_regclass(:name)'), {'name':'public.'+table}):
            result[table] = list(c.execute(text('SELECT count(*),coalesce(sum(xmin::text::bigint),0)::text FROM public.'+table)).one())
    # Guard backing tables, since shared movement relations are UNION views.
    for row in c.execute(text("SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('purchasing_evidence','purchasing_v4') AND tablename IN ('movement_leg','collection_page','collection_attempt','inventory_checkpoint') ORDER BY schemaname,tablename")).mappings():
        table=c.dialect.identifier_preparer.quote_schema(row['schemaname'])+'.'+c.dialect.identifier_preparer.quote(row['tablename'])
        result[table] = list(c.execute(text('SELECT count(*),coalesce(sum(xmin::text::bigint),0)::text FROM '+table)).one())
    return digest(result)


def frozen_rules(c, namespace):
    return [dict(r) for r in c.execute(text('SELECT * FROM purchasing_v4.legacy_manual_evidence WHERE source_namespace=:ns ORDER BY legacy_id'), {'ns':namespace}).mappings()]


def calculate(c, *, actor_id, source_namespace, as_of, selected_keys=None):
    controls.owner(c, actor_id)
    config = controls.approved_configuration(c)
    if config is None:raise controls.InvalidPolicy('Approved numerical settings required')
    global_value = controls.live(controls.heads(c,{('GLOBAL','global')}).get(('GLOBAL','global')))
    age = global_value.get('max_source_age_seconds')
    if type(age) is not int or age<0:raise controls.InvalidPolicy('Approved source freshness seconds required')
    if selected_keys is not None:
        if not isinstance(selected_keys,list) or not 1<=len(selected_keys)<=100 or len(set(selected_keys))!=len(selected_keys):
            raise controls.InvalidPolicy('Explicitly select 1..100 distinct rules')
        for key in selected_keys:
            if not isinstance(key,str) or ':' not in key:raise controls.InvalidPolicy('Verified product-store key required')
            vid,store=key.split(':');controls.uuid(vid)
            if not store.isdecimal():raise controls.InvalidPolicy('Verified store required')
    source = load_snapshot(c,config,as_of=as_of,max_age=timedelta(seconds=age),source_namespace=source_namespace,
                           selected_variations=tuple(sorted({k.split(':')[0] for k in selected_keys})) if selected_keys else None)
    selected = set(selected_keys) if selected_keys else None
    inputs = tuple(r for r in source.rows if selected is None or r.variation_id+':'+r.store_id in selected)
    projected,versions = project_rows(c,inputs,with_versions=True)
    state=controls.heads(c,{('MANUAL',r.variation_id+':'+r.store_id) for r in projected})
    frozen=frozen_rules(c,source_namespace);by_key=defaultdict(list);exceptions=[]
    for f in frozen:
        mapped=f['mapped_variations'];store=f['store_id']
        if len(mapped)==1 and store is not None:
            by_key[mapped[0]+':'+str(store)].append(f)
        elif selected is None:
            exceptions.append(dict(key='legacy:'+str(f['legacy_id']),legacy_ids=[f['legacy_id']],
                product='Unresolved legacy SKU '+str(f['observation'].get('sku') or ''),sku=f['observation'].get('sku'),
                store_id=str(store) if store is not None else None,store_name='Unresolved store' if store is None else str(store),
                manual_par=f['observation'].get('manual_par_level'),manual_level=f['observation'].get('manual_stock_up_level'),
                eligible_for_conversion=False,history_qualified=False,selected_for_conversion=False,
                reasons=['UNRESOLVED_LEGACY_IDENTITY_OR_STORE'],flags=['unresolved'],policy_state='UNRESOLVED'))
    catalog={str(r['variation_id']):dict(r) for r in c.execute(text('SELECT variation_id,label,sku FROM purchasing_v4.management_catalog')).mappings()}
    stores={str(r['id']):r['name'] for r in c.execute(text('SELECT id,name FROM public.stores')).mappings()}
    # Same authoritative supplier/pack/cost selection as worksheet generation.
    offers={r['record_key']:r['payload'] for r in c.execute(text("SELECT record_key,payload FROM purchasing_v4.control_head WHERE kind='OFFER'")).mappings()}
    assignments={r['record_key']:r['payload'] for r in c.execute(text("SELECT record_key,payload FROM purchasing_v4.control_head WHERE kind='ASSIGNMENT'")).mappings()}
    resolved={r.category_id:config.resolve(r.category_id) for r in projected}
    rows=[];seen=set()
    for row in projected:
        key=row.variation_id+':'+row.store_id;record=state.get(('MANUAL',key));policy=controls.live(record);legacy=by_key.get(key,[])
        if not legacy and not policy:continue
        if policy and policy['state']=='AUTOMATIC':continue
        seen.add(key)
        m=_recommend(row,config,as_of,resolved[row.category_id])
        automatic=replace(row,manual=None,policy_issues=tuple(code for code in row.policy_issues if code!='EXPLICIT_INACTIVE_MANUAL_POLICY'))
        a=_recommend(automatic,config,as_of,resolved[row.category_id])
        sid=assignments.get(row.variation_id,{}).get('supplier_id')
        offer=offers.get(row.variation_id+':'+str(sid),{});cost=offer.get('expected_unit_cost')
        cost=Decimal(cost) if cost is not None else None
        valid_manual=row.manual is not None and row.manual.level_units>row.manual.par_units
        eligible=(a.state=='RECOMMENDED' and a.forecast.weekly_units is not None and not row.no_reorder
                  and cost is not None and (valid_manual or bool(policy and policy['state']=='INACTIVE')))
        mu=m.suggested_actual_units if m.raw_need_units is not None else None
        au=a.suggested_actual_units if eligible and a.raw_need_units is not None else None
        difference=au-mu if au is not None and mu is not None else None
        forecast=a.forecast
        if forecast.pooled is None:
            # Diagnostic qualification remains visible even when a manual
            # boundary or No Reorder policy blocks an actionable recommendation.
            forecast=estimate(automatic,resolved[row.category_id][0],as_of=as_of)
        weekly=forecast.weekly_units;pooled=forecast.pooled
        manual_policy=policy or (dict(variation_id=row.variation_id,store_id=int(row.store_id),state='ACTIVE',
            par_units=row.manual.par_units,level_units=row.manual.level_units) if valid_manual else None)
        par=row.manual.par_units if row.manual else (policy or {}).get('par_units')
        level=row.manual.level_units if row.manual else (policy or {}).get('level_units')
        legacy_values={(f['observation']['manual_par_level'],f['observation']['manual_stock_up_level']) for f in legacy}
        if row.manual is None and policy is None and len(legacy_values)==1:par,level=next(iter(legacy_values))
        flags=[]
        if not eligible:flags.append('unresolved')
        if difference is not None and difference<0:flags.append('reductions')
        if difference is not None and difference>0:flags.extend(['increases','stockout_risk'])
        if weekly is not None and weekly>0 and par is not None and a.reorder_units is not None and par<a.reorder_units and 'stockout_risk' not in flags:flags.append('stockout_risk')
        if weekly==0:flags.append('no_sales')
        if weekly is not None and level is not None and level>max(weekly*12,1):flags.append('high_target')
        if weekly is not None and row.on_hand.state==EvidenceState.KNOWN and row.on_hand.value>max(weekly*12,1):flags.append('high_stock')
        if row.pack_size and any(v is not None and v%row.pack_size for v in (par,level)):flags.append('pack_mismatch')
        reasons=set(m.reasons)|set(a.reasons)
        if not valid_manual and not (policy and policy['state']=='INACTIVE'):reasons.add('MANUAL_BOUNDARY_REVIEW_REQUIRED')
        meta=catalog.get(row.variation_id,{})
        rows.append(dict(key=key,variation_id=row.variation_id,store_id=row.store_id,store_name=stores.get(row.store_id,row.store_id),
            product=meta.get('label',row.variation_id),sku=meta.get('sku'),legacy_ids=[f['legacy_id'] for f in legacy],
            expected_version=record['version'] if record else 0,manual_policy=manual_policy,
            policy_state=(policy or {}).get('state','FROZEN_LEGACY'),
            on_hand=number(row.on_hand.value) if row.on_hand.state==EvidenceState.KNOWN else None,
            inventory_position=number(m.inventory_position),manual_par=par,manual_level=level,
            engine_par=number(a.reorder_units),engine_level=number(a.target_units),weekly_units=number(weekly),
            manual_order=mu,engine_order=au,difference=difference,pack_size=row.pack_size,supplier_id=sid,
            unit_cost=money(cost),manual_cost=money(cost*mu) if cost is not None and mu is not None else None,
            engine_cost=money(cost*au) if cost is not None and au is not None else None,
            cost_impact=money(cost*difference) if cost is not None and difference is not None else None,
            available_days=pooled.available_days if pooled else None,
            sales_dates=sum(d.sales.state==EvidenceState.KNOWN and d.sales.value>=0 and (d.sales.observed_at is None or d.sales.observed_at<=as_of) for d in row.history),
            manual_sources=[dict(id=f['legacy_id'],par=f['observation']['manual_par_level'],level=f['observation']['manual_stock_up_level']) for f in legacy],
            history_qualified=weekly is not None,eligible_for_conversion=eligible,selected_for_conversion=False,
            reasons=sorted(reasons),flags=flags,coverage_weeks=number(Fraction(row.on_hand.value)/weekly) if weekly and row.on_hand.state==EvidenceState.KNOWN else None))
    # Records outside catalog/store scope must still be accounted for and retained.
    if selected is None:
        for key,legacy in by_key.items():
            if key not in seen and not controls.live(state.get(('MANUAL',key))):
                exceptions.append(dict(key=key,legacy_ids=[f['legacy_id'] for f in legacy],product='Unresolved legacy scope',
                    sku=legacy[0]['observation'].get('sku'),store_name=stores.get(str(legacy[0]['store_id']),'Unresolved store'),
                    store_id=str(legacy[0]['store_id']),eligible_for_conversion=False,history_qualified=False,
                    selected_for_conversion=False,reasons=['UNAVAILABLE_CATALOG_OR_STORE_SCOPE'],flags=['unresolved'],policy_state='UNRESOLVED'))
    rows.extend(exceptions)
    if selected is not None and {r['key'] for r in rows}!=selected:raise controls.InvalidPolicy('Selected rule is no longer manual or lacks verified scope')
    return dict(id=str(uuid4()),actor_id=actor_id,configuration_digest=config.content_digest,input_digest=source.digest,
        as_of=as_of.isoformat(),max_age_seconds=age,rows=rows,dependency_versions=versions,format=FORMAT,
        preview=selected is not None,source_namespace=source_namespace,source_guard=source_guard(c),
        legacy_guard=digest(frozen),manual_records_evaluated=len(frozen),summary=totals(rows),
        source_queries=source.data_queries,unknown_inventory_positions=sum(r.on_hand.state!=EvidenceState.KNOWN for r in source.rows))


def totals(rows):
    comparable=[r for r in rows if r.get('cost_impact') is not None]
    sum_cost=lambda key,rs:money(sum((Decimal(r[key]) for r in rs if r.get(key) is not None),Decimal()))
    return dict(rules=len(rows),eligible=sum(r['eligible_for_conversion'] for r in rows),
        requiring_history=sum(not r.get('history_qualified',False) for r in rows),
        other_exclusions=sum(r.get('history_qualified',False) and not r['eligible_for_conversion'] for r in rows),
        current_manual_cost=sum_cost('manual_cost',rows),comparable_manual_cost=sum_cost('manual_cost',comparable),
        equivalent_engine_cost=sum_cost('engine_cost',comparable),
        gross_reductions=money(-sum((Decimal(r['cost_impact']) for r in comparable if Decimal(r['cost_impact'])<0),Decimal())),
        gross_increases=money(sum((Decimal(r['cost_impact']) for r in comparable if Decimal(r['cost_impact'])>0),Decimal())),
        net_difference=sum_cost('cost_impact',comparable),comparable_rules=len(comparable),
        unresolved_manual_orders=sum(r.get('manual_order') is None for r in rows),
        unknown_cost_orders=sum(r.get('manual_order',0) not in (None,0) and r.get('unit_cost') is None for r in rows))


def save(c, report):
    controls.owner(c,report['actor_id'])
    c.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
    config=controls.approved_configuration(c)
    if config is None or config.content_digest!=report['configuration_digest']:raise controls.Conflict('Settings changed; check history again')
    verify_dependencies(c,report['dependency_versions'])
    if report['legacy_guard']!=digest(frozen_rules(c,report['source_namespace'])):raise controls.Conflict('Manual source changed; check history again')
    c.execute(text('INSERT INTO purchasing_v4.history_check(id,actor_id,configuration_digest,input_digest,rows) VALUES (:id,:actor,:config,:input,CAST(:rows AS jsonb))'),
        dict(id=report['id'],actor=report['actor_id'],config=report['configuration_digest'],input=report['input_digest'],rows=json.dumps(report)))


def metadata(c, actor_id, report_id=None):
    controls.owner(c,actor_id)
    row=c.execute(text("SELECT id::text,rows - 'rows' - 'dependency_versions' - 'source_guard' - 'legacy_guard' AS metadata FROM purchasing_v4.history_check WHERE actor_id=:actor AND rows->>'format'=:format AND coalesce((rows->>'preview')::boolean,false)=false AND (:id='' OR id=CAST(NULLIF(:id,'') AS uuid)) ORDER BY created_at DESC,id DESC LIMIT 1"),dict(actor=actor_id,format=FORMAT,id=controls.uuid(report_id) if report_id else '')).mappings().one_or_none()
    if not row:return None
    return dict(row['metadata'],id=row['id'])


def page(c, *, actor_id, report_id, search='', store_id='', qualification='all', flag='all', sort='cost_impact', direction='asc', offset=0, limit=50):
    controls.owner(c,actor_id)
    if sort not in SORTS or direction not in ('asc','desc') or qualification not in ('all','qualified','unqualified') or flag not in FILTERS or not 1<=limit<=100 or not 0<=offset<=100000 or len(search)>200 or len(store_id)>20:
        raise controls.InvalidPolicy('Bounded page and supported filter/sort required')
    report_id=controls.uuid(report_id)
    # Identifiers/direction are strict allowlists; all user values are bound parameters.
    expression="(r->>'"+sort+"')"+('::numeric' if sort in NUMERIC else '::boolean' if sort=='eligible_for_conversion' else '')
    sql="""WITH items AS (SELECT r FROM purchasing_v4.history_check h CROSS JOIN LATERAL jsonb_array_elements(h.rows->'rows') r
      WHERE h.id=:id AND h.actor_id=:actor AND h.rows->>'format'=:format), filtered AS (SELECT r FROM items
      WHERE (:search='' OR concat_ws(' ',r->>'product',r->>'sku') ILIKE :like ESCAPE '\\')
      AND (:store='' OR r->>'store_id'=:store)
      AND (:qualification='all' OR coalesce((r->>'eligible_for_conversion')::boolean,false)=(:qualification='qualified'))
      AND (:flag='all' OR r->'flags' ? :flag))
      SELECT (SELECT count(*) FROM filtered) AS total,
      coalesce((SELECT jsonb_agg(r ORDER BY position) FROM (SELECT r,row_number() OVER (ORDER BY """+expression+' '+direction+" NULLS LAST,r->>'key') position FROM filtered ORDER BY "+expression+' '+direction+" NULLS LAST,r->>'key' LIMIT :limit OFFSET :offset) p),'[]'::jsonb) AS rows"
    escaped=search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
    result=dict(c.execute(text(sql),dict(id=report_id,actor=actor_id,format=FORMAT,search=search,like='%'+escaped+'%',store=store_id,qualification=qualification,flag=flag,limit=limit,offset=offset)).mappings().one())
    return dict(result,offset=offset,limit=limit)


def confirm(c, *, actor_id, command_id, reason, preview_id, selected_keys):
    controls.owner(c,actor_id)
    if not isinstance(selected_keys,list) or not 1<=len(selected_keys)<=100 or len(set(selected_keys))!=len(selected_keys):raise controls.InvalidPolicy('Explicit nonempty selection required')
    with c.begin_nested():
        c.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
        report=c.scalar(text('SELECT rows FROM purchasing_v4.history_check WHERE id=:id AND actor_id=:actor'),dict(id=controls.uuid(preview_id),actor=actor_id))
        if not report or report.get('format')!=FORMAT or not report.get('preview'):raise controls.InvalidPolicy('Fresh impact preview required')
        rows={r['key']:r for r in report['rows']}
        if set(selected_keys)!=set(rows) or not all(r['eligible_for_conversion'] for r in rows.values()):raise controls.InvalidPolicy('Only explicitly previewed qualified rules may convert')
        replay=c.scalar(text('SELECT 1 FROM purchasing_v4.control_command WHERE id=:id'),dict(id=controls.uuid(command_id)))
        if not replay:
            age=datetime.now(timezone.utc)-datetime.fromisoformat(report['as_of'])
            if not timedelta()<=age<=timedelta(minutes=15):raise controls.Conflict('Impact preview expired; preview again')
            config=controls.approved_configuration(c)
            if config is None or config.content_digest!=report['configuration_digest']:raise controls.Conflict('Settings changed; preview again')
            verify_dependencies(c,report['dependency_versions'])
            if report['source_guard']!=source_guard(c) or report['legacy_guard']!=digest(frozen_rules(c,report['source_namespace'])):raise controls.Conflict('Evidence changed; preview again')
        provenance=dict(history_check=preview_id,legacy_rules={k:r['legacy_ids'] for k,r in rows.items()},impact=report['summary'])
        baseline=[dict(kind='MANUAL',key=key,expected_version=0,value=r['manual_policy']) for key,r in sorted(rows.items()) if r['expected_version']==0]
        if baseline and not replay:
            # A frozen rule has no owner revision yet. Preserve its exact ACTIVE
            # quantities as an audited baseline in this same atomic transaction,
            # so ordinary owner Restore can reverse the subsequent conversion.
            controls._apply(c,actor_id=actor_id,command_id=str(uuid5(UUID(controls.uuid(command_id)),'preserve-manual-before-conversion')),
                reason=reason,changes=baseline,action='PRESERVE_MANUAL_BEFORE_CONVERSION:'+preview_id,provenance=provenance)
        changes=[dict(kind='MANUAL',key=key,expected_version=r['expected_version'] or 1,value=dict(r['manual_policy'],state='AUTOMATIC')) for key,r in sorted(rows.items())]
        return controls._apply(c,actor_id=actor_id,command_id=command_id,reason=reason,changes=changes,conversion=True,
            action='USE_AUTOMATIC_ENGINE:'+preview_id,provenance=provenance)
