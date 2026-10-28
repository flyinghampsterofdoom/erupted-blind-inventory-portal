"""Bounded SELECT-only on-hand view using the accepted inventory projection.

No freshness default, inventory refresh, forecasting or persistence. Inventory
is an explicitly dated read view, independent of pinned worksheet decisions.
"""
from collections import Counter,defaultdict
from datetime import timedelta
from sqlalchemy import text
from . import worksheets,controls
from .receiving_supply import posting_membership,qualify_inventory
from .contracts import EvidenceState,QuantityEvidence
from .evidence_rules import inventory_observation
from .isolation import require_read_only
from .calculation_serialization import primitive


def inventory_view(c,*,session_id,actor_id,source_namespace,as_of,max_age=None):
    require_read_only(c)
    if as_of.utcoffset() is None:raise controls.InvalidPolicy('Aware inventory cutoff required')
    if max_age is not None and (not isinstance(max_age,timedelta) or max_age<timedelta()):raise controls.InvalidPolicy('Explicit nonnegative inventory age required')
    state=worksheets.snapshot(c,session_id,actor_id);ids=[r['variation_id'] for r in state['rows']]
    stores=list(c.execute(text('SELECT id,square_location_id,active FROM public.stores')).mappings());by_store={s['id']:s for s in stores};claims=Counter(s['square_location_id'] for s in stores if s['active'] and s['square_location_id'])
    refs=defaultdict(list)
    for r in c.execute(text('SELECT variation_id::text,square_variation_id FROM purchasing_v4.variation_reference WHERE source_namespace=:ns AND variation_id=ANY(CAST(:ids AS uuid[]))'),dict(ns=source_namespace,ids=ids)).mappings():refs[r['variation_id']].append(r['square_variation_id'])
    squares=[r[0] for r in refs.values() if len(r)==1]
    observations={(r['square_variation_id'],r['store_id']):r for r in c.execute(text('''SELECT i.*,r.result,r.started_at,r.completed_at FROM public.ordering_current_inventory i
      JOIN public.ordering_inventory_refresh_runs r ON r.id=i.refresh_run_id WHERE i.square_variation_id=ANY(:squares)'''),dict(squares=squares)).mappings()}
    membership=posting_membership(c,squares)
    result={}
    for row in state['rows']:
        references=refs[row['variation_id']];square=references[0] if len(references)==1 else None;values=[]
        for bound in state['session']['stores']:
            store=by_store.get(bound['id']);location=store['square_location_id'] if store and store['active'] and claims[store['square_location_id']]==1 else None
            if square is None:evidence=QuantityEvidence(EvidenceState.UNKNOWN,None,'legacy.square_inventory_observation','UNRESOLVED_VARIATION_REFERENCE')
            elif max_age is None:evidence=QuantityEvidence(EvidenceState.UNAVAILABLE,None,'legacy.square_inventory_observation','FRESHNESS_NOT_CONFIGURED')
            else:evidence=inventory_observation(observations.get((square,bound['id'])),location,variation_id=square,store_id=bound['id'],as_of=as_of,max_age=max_age)
            evidence=qualify_inventory(evidence,membership[(square,bound['id'])],observations.get((square,bound['id'])),as_of=as_of)
            values.append(primitive(evidence))
        result[row['id']]=values
    return dict(as_of=as_of.isoformat(),max_age_seconds=max_age.total_seconds() if max_age is not None else None,unit='individual_inventory_units',stores=state['session']['stores'],rows=result)
