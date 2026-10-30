"""Constant-query read-only snapshot extraction for the isolated candidate.

One source database, shared Phase1 projections, no per-product SQL or new ledger.
This loader does not refresh Square, persist policies or create sessions.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
import json
from sqlalchemy import text
from .contracts import QuantityEvidence, EvidenceState, ExposureDay
from .evidence_rules import (BUSINESS_ZONE,INCOMING_SQL,coverage_status,inventory_observation,
                             incoming_quantity,availability_projection,load_inventory)
from .isolation import require_read_only
from .receiving_supply import incoming_sql,posting_membership,qualify_inventory
from .calculation import StoreInput,Supply,SupplyLot,ManualRule
from .calculation_serialization import evidence_digest


@dataclass(frozen=True)
class SourceSnapshot:
    as_of: datetime
    rows: tuple[StoreInput, ...]
    digest: str
    exceptions: tuple[str, ...]
    mapping_exceptions: tuple[str, ...]
    data_queries: int
    evidence_summary: tuple[tuple[str,int], ...]


def load_snapshot(connection,configuration,*,as_of,max_age,source_namespace,selected_variations=None):
    require_read_only(connection)
    if as_of.utcoffset() is None or not isinstance(max_age,timedelta) or max_age<timedelta():
        raise ValueError('Aware cutoff and explicit nonnegative source age required')
    if selected_variations is not None and (not isinstance(selected_variations,tuple) or not 1<=len(selected_variations)<=100):
        raise ValueError('Select1..100 immutable variations for a bounded history check')
    select_all=selected_variations is None
    cutoff=as_of.astimezone(BUSINESS_ZONE).date()
    longest=max([configuration.global_parameters.model.windows.lookback_days]+
                [configuration.resolve(c.category_id)[0].model.windows.lookback_days for c in configuration.categories])
    first=cutoff-timedelta(days=longest)
    start=datetime.combine(first,time(),BUSINESS_ZONE)
    queries=0
    def fetch(sql,**values):
        nonlocal queries
        require_read_only(connection);queries+=1
        return connection.execute(text(sql),values).mappings().all()
    catalog=fetch('''SELECT c.*,r.variation_id::text AS internal_id
      FROM public.ordering_catalog_identity c LEFT JOIN purchasing_v4.variation_reference r
      ON r.source_namespace=:namespace AND r.square_variation_id=c.square_variation_id
      WHERE (:all OR r.variation_id=ANY(CAST(:selected AS uuid[])))
      ORDER BY c.square_variation_id''',namespace=source_namespace,all=select_all,selected=list(selected_variations or ()))
    selected_squares=[c['square_variation_id'] for c in catalog]
    stores=fetch('SELECT id,name,square_location_id FROM public.stores WHERE active ORDER BY id')
    locations=Counter(r['square_location_id'] for r in stores if r['square_location_id'])
    location_by_store={r['id']:r['square_location_id'] if r['square_location_id'] and locations[r['square_location_id']]==1 else None for r in stores}
    cover={(r['source'],r['location_id']):r for r in fetch('SELECT * FROM public.purchasing_history_coverage')}
    state=fetch('SELECT last_result,last_attempted_at FROM public.consignment_sales_sync_state WHERE id=1')
    from app.services.inventory_history_projection import source_failed
    failed=source_failed(connection,state[0] if state else None,as_of=as_of)
    inventory={(r['square_variation_id'],r['store_id']):r for r in load_inventory(fetch)}
    base=fetch('''SELECT square_order_id,square_line_item_uid,square_variation_id,square_location_id,
      business_date,quantity_sold,source_synchronized_at FROM public.consignment_sale_facts
      WHERE business_date>=:start AND business_date<:end AND (:all OR square_variation_id=ANY(:squares))''',start=first,end=cutoff,all=select_all,squares=selected_squares)
    corrections=fetch('''SELECT c.* FROM public.purchasing_sale_corrections c WHERE c.kind='LINE'
      AND ((c.business_date>=:start AND c.business_date<:end) OR EXISTS (
       SELECT 1 FROM public.consignment_sale_facts f WHERE f.square_order_id=c.square_order_id
       AND f.square_line_item_uid=c.square_line_item_uid AND f.business_date>=:start AND f.business_date<:end))
      AND (:all OR c.variation_id=ANY(:squares) OR EXISTS(SELECT 1 FROM public.consignment_sale_facts f WHERE f.square_order_id=c.square_order_id AND f.square_line_item_uid=c.square_line_item_uid AND f.square_variation_id=ANY(:squares)))''',
      start=first,end=cutoff,all=select_all,squares=selected_squares)
    correction_by_line={(r['square_order_id'],r['square_line_item_uid']):r for r in corrections}
    totals=defaultdict(Decimal);future=set();bad=set()
    for row in base:
        key=(row['square_location_id'],row['square_variation_id'],row['business_date'])
        correction=correction_by_line.get((row['square_order_id'],row['square_line_item_uid']))
        if row['source_synchronized_at']>as_of or correction and correction['synchronized_at']>as_of:
            future.add((key[0],key[1]))
        if correction is None:
            if row['quantity_sold']<0 or not row['quantity_sold'].is_finite():bad.add((key[0],key[1]))
            else:totals[key]+=row['quantity_sold']
    for row in corrections:
        key=(row['location_id'],row['variation_id'],row['business_date'])
        if row['synchronized_at']>as_of:future.add((key[0],key[1]))
        if first<=row['business_date']<cutoff and row['variation_id']:
            if row['quantity']<0 or not row['quantity'].is_finite():bad.add((key[0],key[1]))
            else:totals[key]+=row['quantity']
    from app.services.inventory_history_projection import movement_table
    movement_source=movement_table(connection)
    movements=defaultdict(list)
    for row in fetch('''SELECT * FROM public.purchasing_inventory_events WHERE (occurred_at>=:start
      OR calculated_before_at>=:start) AND (:all OR variation_id=ANY(:squares)) ORDER BY occurred_at,event_type,event_id'''.replace('public.purchasing_inventory_events',movement_source),start=start,all=select_all,squares=selected_squares):
        movements[(row['location_id'],row['variation_id'])].append(row)
    from .absolute_availability import read_absolute_counts,reconcile_projection
    absolute_counts=read_absolute_counts(fetch,movement_source,as_of=as_of)
    incoming_rows=fetch(incoming_sql(connection,INCOMING_SQL))
    v4_membership=posting_membership(connection)
    # Group once, retaining unidentifiable obligations in each relevant scope.
    from .identity import valid_reference
    incoming_by_variation=defaultdict(list);unidentified=[]
    for row in incoming_rows:
        if valid_reference(row['variation_id']):incoming_by_variation[row['variation_id']].append(row)
        else:unidentified.append(row)
    lifecycle={r['square_variation_id']:r['status'] for r in fetch('SELECT square_variation_id,status FROM public.ordering_product_lifecycle')}
    configs=fetch('SELECT id,vendor_id,sku,square_variation_id,pack_size,is_default_vendor,active FROM public.vendor_sku_configs')
    receipt_events=defaultdict(list)
    for r in fetch('''SELECT a.id,a.square_variation_id,a.store_id,a.created_at,e.status,e.updated_at AS posting_observed_at
      FROM public.purchase_order_receipt_allocations a LEFT JOIN public.square_sync_events e ON e.id=a.square_sync_event_id
      WHERE NOT a.posting_not_required AND a.event_kind!='CLOSURE' ORDER BY a.id'''):
        receipt_events[(r['square_variation_id'],r['store_id'])].append(r)
    manual_rows=fetch("SELECT * FROM public.par_levels WHERE par_source='MANUAL' ORDER BY id")
    mapping_rows=fetch('''SELECT e.code,e.source_table,e.source_key,e.details FROM purchasing_v4.identity_exception e
      JOIN purchasing_v4.import_run r ON r.id=e.run_id WHERE r.source_namespace=:namespace
      ORDER BY e.code,e.source_table,e.source_key''',namespace=source_namespace)
    mapping_exceptions=tuple(sorted(set(json.dumps(dict(r),sort_keys=True,default=str) for r in mapping_rows)))
    issues=set(mapping_exceptions);identity_issues=defaultdict(set)
    for row in mapping_rows:
        vid=row['details'].get('square_variation_id')
        if vid:identity_issues[vid].add(row['code']+':'+row['source_key'])
    cfg_by_variation=defaultdict(list);cfg_by_vendor_sku=defaultdict(set)
    for row in configs:
        if row['active']:
            cfg_by_variation[row['square_variation_id']].append(row)
            cfg_by_vendor_sku[(row['vendor_id'],row['sku'])].add(row['square_variation_id'])
    manual_by_pair=defaultdict(list);policy_issues=defaultdict(set)
    for row in manual_rows:
        ids=cfg_by_vendor_sku.get((row['vendor_id'],row['sku']),set())
        if len(ids)!=1:
            issues.add('UNRESOLVED_LEGACY_MANUAL_REFERENCE:'+str(row['id']))
            # Candidate identities are blocked, never bound or silently converted.
            candidates={c['square_variation_id'] for c in configs if c['sku']==row['sku']}
            for candidate in candidates:
                for store in stores:
                    if row['store_id'] is None or row['store_id']==store['id']:
                        policy_issues[(candidate,store['id'])].add('UNRESOLVED_LEGACY_MANUAL_REFERENCE')
            continue
        vid=next(iter(ids))
        if row['store_id'] not in location_by_store:
            issues.add('UNRESOLVED_LEGACY_MANUAL_STORE_SCOPE:'+str(row['id']))
            for store in stores:policy_issues[(vid,store['id'])].add('UNRESOLVED_LEGACY_MANUAL_STORE_SCOPE')
        else:manual_by_pair[(vid,row['store_id'])].append(row)
    rows=[];summary=Counter()
    for product in catalog:
        vid=product['square_variation_id']
        if not product['internal_id']:
            issues.add('MISSING_PHASE1_INTERNAL_VARIATION:'+vid);continue
        configs_for_product=cfg_by_variation[vid]
        explicit=[c for c in configs_for_product if c['is_default_vendor']]
        packs={c['pack_size'] for c in explicit}
        pack=next(iter(packs)) if len(packs)==1 else None
        if len({c['vendor_id'] for c in explicit})!=1:identity_issues[vid].add('UNRESOLVED_PRIMARY_SUPPLIER_EVIDENCE')
        attrs=product['catalog_attributes'] or {}
        category=attrs.get('reporting_category_id') or (attrs.get('item_data') or {}).get('reporting_category_id')
        # Unknown or multiple category references are not assigned an arbitrary category.
        category=str(category) if isinstance(category,(str,int)) else None
        category_ids=attrs.get('category_ids') or []
        candidates={c for c in category_ids if isinstance(c,str) and c}
        if category is None and len(candidates)==1:category=next(iter(candidates))
        ambiguous_category=(category is None and len(candidates)>1 and
                            bool(candidates.intersection(c.category_id for c in configuration.categories)))
        parameters,_,_,_=configuration.resolve(category)
        begin=cutoff-timedelta(days=parameters.model.windows.lookback_days)
        begin_at=datetime.combine(begin,time(),BUSINESS_ZONE)
        for store in stores:
            sid=store['id'];location=location_by_store[sid]
            hand=inventory_observation(inventory.get((vid,sid)),location,variation_id=vid,store_id=sid,as_of=as_of,max_age=max_age)
            hand=qualify_inventory(hand,v4_membership[(vid,sid)],inventory.get((vid,sid)),as_of=as_of)
            sales_state,sales_reason,verified=coverage_status(cover.get(('SALES',location)),failed,
                start=begin_at,end=datetime.combine(cutoff,time(),BUSINESS_ZONE),as_of=as_of,max_age=max_age)
            if not location:sales_state,sales_reason=EvidenceState.UNKNOWN,'UNRESOLVED_STORE_LOCATION'
            if (location,vid) in future:sales_state,sales_reason=EvidenceState.UNKNOWN,'SOURCE_CHANGED_AFTER_CUTOFF'
            if (location,vid) in bad:sales_state,sales_reason=EvidenceState.UNKNOWN,'INVALID_SALES_QUANTITY'
            sales={begin+timedelta(days=n):QuantityEvidence(sales_state,
                totals[(location,vid,begin+timedelta(days=n))] if sales_state==EvidenceState.KNOWN else None,
                'legacy.corrected_gross_sales',sales_reason,verified,
                ('purchasing_history_coverage:SALES:'+location,) if location else ())
                for n in range((cutoff-begin).days)}
            movement=movements[(location,vid)]
            events=[e for e in movement if e['occurred_at']>=begin_at]
            boundary=[e for e in movement if e['occurred_at']<begin_at and e['calculated_before_at']>=begin_at]
            movement_coverage=coverage_status(cover.get(('MOVEMENTS',location)),failed,start=begin_at,
                end=max(datetime.combine(cutoff,time(),BUSINESS_ZONE),hand.observed_at or begin_at),as_of=as_of,max_age=max_age)
            exposure=availability_projection(location=location,inventory=hand,anchor_row=inventory.get((vid,sid)),
                coverage=movement_coverage,events=events,boundary=boundary,sales=sales,start_day=begin,end_day=cutoff,as_of=as_of)
            exposure=reconcile_projection(exposure,absolute_counts=absolute_counts,events=events,
                inventory=hand,anchor_row=inventory.get((vid,sid)),coverage=movement_coverage,
                sales=sales,start_day=begin,end_day=cutoff,as_of=as_of,zone=BUSINESS_ZONE)
            history=tuple(ExposureDay(day,status,sales[day],reason,tuple(refs)) for day,status,_,reason,refs in exposure)
            values,reason,refs=incoming_quantity(incoming_by_variation[vid]+unidentified,vid)
            if not location:reason='UNRESOLVED_STORE_LOCATION'
            incoming=QuantityEvidence(EvidenceState.UNKNOWN if reason else EvidenceState.KNOWN,
                None if reason else Decimal(values.get(sid,0)),'legacy.po_allocations',reason,references=refs)
            unqualified=[]
            count_row=inventory.get((vid,sid))
            for operation in receipt_events[(vid,sid)]:
                completed=operation['posting_observed_at']
                if (operation['status']!='SUCCESS' or completed is None or completed>as_of or
                        not count_row or count_row['source_calculated_at'] is None or
                        max(completed,operation['created_at'])>min(count_row['source_calculated_at'],count_row['refreshed_at'])):
                    unqualified.append('UNRECONCILED_RECEIPT_POSTING:'+str(operation['id']))
            supply=Supply((SupplyLot('legacy-open:'+vid+':'+str(sid),'VENDOR_OPEN',incoming,False),),(),(),not unqualified,
                          'retained-snapshot:'+as_of.isoformat(),tuple(unqualified))
            manual=None;rules=manual_by_pair[(vid,sid)]
            if rules:
                for rule in rules:
                    if rule['manual_par_level'] is None or rule['manual_stock_up_level'] is None:
                        issues.add(json.dumps(dict(code='UNRESOLVED_LEGACY_MANUAL_QUANTITIES',source_table='par_levels',source_key=str(rule['id']),
                            details={key:rule[key] for key in ('sku','vendor_id','store_id','manual_par_level','manual_stock_up_level')}),sort_keys=True,default=str))
                pairs={(r['manual_par_level'],r['manual_stock_up_level']) for r in rules}
                if len(pairs)!=1 or any(v is None for pair in pairs for v in pair):
                    policy_issues[(vid,sid)].add('UNRESOLVED_LEGACY_MANUAL_QUANTITIES')
                else:
                    par,level=next(iter(pairs))
                    if par<0 or level<0:policy_issues[(vid,sid)].add('INVALID_LEGACY_MANUAL_QUANTITIES')
                    else:manual=ManualRule('legacy-par:'+','.join(str(r['id']) for r in rules),par,level)
            if ambiguous_category:policy_issues[(vid,sid)].add('UNRESOLVED_CATEGORY_OVERRIDE_SCOPE')
            rows.append(StoreInput(product['internal_id'],str(sid),category,history,hand,supply,pack,
                lifecycle.get(vid) in ('NO_FUTURE_REORDER','ARCHIVED'),manual,None,None,tuple(sorted(identity_issues[vid])),
                tuple(sorted(policy_issues[(vid,sid)]))))
            summary['inventory.'+hand.state.value]+=1;summary['incoming.'+incoming.state.value]+=1
            summary.update('availability.'+e[1].value for e in exposure)
            summary.update('availability_reason.'+e[3] for e in exposure if e[3])
    rows=tuple(rows);exceptions=tuple(sorted(issues))
    return SourceSnapshot(as_of,rows,evidence_digest(rows,as_of=as_of,source_exceptions=exceptions),
                          exceptions,mapping_exceptions,queries,tuple(sorted(summary.items())))
