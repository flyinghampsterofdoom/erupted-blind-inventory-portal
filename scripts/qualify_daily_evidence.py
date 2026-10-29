"""Offline qualification over retained real Square observations; isolated DB only."""
import argparse,json,os,time
from pathlib import Path
from collections import Counter
from datetime import timedelta
from sqlalchemy import create_engine,text
from app.purchasing_v4.isolation import require_isolated_url,readonly_snapshot
from app.purchasing_v4.schema import migrate_daily_evidence
from app.services.daily_square_collector import Collector,Scope,instant
from app.purchasing_v4.calculation_source import load_snapshot
from app.purchasing_v4.calculation_documents import configuration_from_document
from app.purchasing_v4.calculation import estimate


def fingerprint(c, audit_limit):
    # Every public business table except explicit source projections, plus every
    # preexisting private table. Counts alone would miss edited quantities/IDs.
    c.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    excluded={'ordering_current_inventory','ordering_inventory_refresh_runs','purchasing_evidence_runs','purchasing_history_coverage','purchasing_sale_corrections','purchasing_order_source_state','audit_logs'}
    tables=c.execute(text("SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('public','purchasing_v4') ORDER BY 1,2")).all()
    out={}
    for schema,table in tables:
        if schema=='purchasing_v4' and table in {'alembic_version','collection_attempt','collection_page','inventory_checkpoint','movement_leg'}:continue
        if schema=='public' and table in excluded:continue
        where=f' WHERE id<={int(audit_limit)}' if schema=='public' and table=='audit_log' else ''
        out[schema+'.'+table]=dict(c.execute(text(f'''SELECT count(*) AS rows,md5(coalesce(string_agg(digest,',' ORDER BY digest),'')) AS digest FROM (SELECT md5(to_jsonb(t)::text) AS digest FROM "{schema}"."{table}" t{where}) q''')).mappings().one())
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--captures',type=Path,required=True);p.add_argument('--audit',type=Path,required=True);p.add_argument('--profile',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    require_isolated_url(a.url);engine=create_engine(a.url);a.output.mkdir(parents=True,exist_ok=True)
    load=lambda name:json.loads((a.captures/(name+'.json')).read_text())
    before=load('live-square-counts');after=load('live-square-counts-after');moves=load('square-movement-history');sales=load('square-126day-sales');db=load('database-evidence')
    scope=Scope(tuple(before['scope_ids']),tuple((s['id'],s['square_location_id']) for s in db['stores'] if s['active']))
    orders={}
    for l in sales['lines']:
        order=orders.setdefault(l['order_id'],dict(id=l['order_id'],location_id=l['location_id'],closed_at=l['closed_at'],version=l['order_version'],state='COMPLETED',line_items=[]))
        order['line_items'].append(dict(uid=l['uid'],catalog_object_id=l['variation_id'],quantity=l['quantity']))
    bundle=dict(before=before,after=after,begin=sales['start_at'],sales_end=sales['end_at'],movement_end=before['completed_at'],observed=moves['read_completed_at'],orders=list(orders.values()),updates=[],changes=moves['changes'])
    with engine.begin() as c:
        audit_limit=c.scalar(text('SELECT coalesce(max(id),0) FROM public.audit_log'))
        old=fingerprint(c,audit_limit);migrate_daily_evidence(c)
    tick=time.monotonic();collector=Collector(engine,None)
    result=collector.project(scope,bundle,reason='RECOVERY',command_key='recovery:verified-20261028-126day')
    result['projection_seconds']=round(time.monotonic()-tick,3)
    replay=collector.project(scope,bundle,reason='RECOVERY',command_key='recovery:verified-20261028-126day')
    profile=json.loads(a.profile.read_text())['profile'];configuration=configuration_from_document(profile)
    active={r['square_variation_id'] for r in json.loads((a.audit/'pair-results.json').read_text()) if r['erupted_active']}
    with readonly_snapshot(engine) as c:
        new=fingerprint(c,audit_limit)
        identities={r.internal:r.square for r in c.execute(text("SELECT variation_id::text internal,square_variation_id square FROM purchasing_v4.variation_reference WHERE source_namespace='erupted-retained-square-source'"))}
        tick=time.monotonic();snapshot=load_snapshot(c,configuration,as_of=instant(after['completed_at']),max_age=timedelta(days=2),source_namespace='erupted-retained-square-source')
        elapsed=time.monotonic()-tick
        rows=[]
        for row in snapshot.rows:
            square=identities[row.variation_id]
            if square not in active:continue
            f=estimate(row,configuration.resolve(row.category_id)[0],as_of=instant(after['completed_at']))
            rows.append(dict(variation_id=square,store_id=row.store_id,qualified=f.weekly_units is not None,state=f.state,branch=f.branch,available=f.pooled.available_days))
        checkpoints=dict(Counter(c.execute(text('SELECT state FROM purchasing_v4.inventory_checkpoint')).scalars()))
    changed=[t for t,v in old.items() if new.get(t)!=v]
    summary=dict(collection=result,replay=replay,protected_tables=len(old),protected_table_changes=changed,active_pairs=len(rows),qualified_pairs=sum(r['qualified'] for r in rows),
        branches=dict(Counter(r['branch'] for r in rows)),snapshot_seconds=round(elapsed,3),snapshot_data_queries=snapshot.data_queries,checkpoints=checkpoints,
        forecast_requirements_changed=False,production_writes=0,live_square_calls=0,schedule_activated=False,
        limitation='Real captured recovery has CLOSED_AT source evidence only; no UPDATED_AT response was captured. Ongoing collector additionally exhausts UPDATED_AT for late corrections. Explicit 48-hour study profile is diagnostic, not an installed policy.')
    (a.output/'qualification.json').write_text(json.dumps(summary,indent=2));(a.output/'pair-results.json').write_text(json.dumps(rows,indent=2))
    (a.output/'preservation.json').write_text(json.dumps(dict(before=old,after=new,changed=changed),indent=2))
    print(json.dumps(summary,indent=2));assert not changed
    assert len(rows)==5120 and sum(r['qualified'] for r in rows)==3093
    assert replay['status']=='ALREADY_COLLECTED'

if __name__=='__main__':main()
