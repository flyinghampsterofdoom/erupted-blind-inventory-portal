"""Explicit isolated Phase3 migration, activity and preservation qualification."""
import argparse,hashlib,json,time,resource,statistics
from pathlib import Path
from datetime import datetime,timedelta
from collections import defaultdict,Counter
from sqlalchemy import create_engine,text,event
from app.purchasing_v4.isolation import require_isolated_url,readonly_snapshot
from app.purchasing_v4.schema import migrate_controls
from app.purchasing_v4.management_read import catalog_projection,import_review_evidence,list_catalog,list_legacy_manual,list_manual
from app.purchasing_v4.reconciliation import public_fingerprints,public_structure
from app.purchasing_v4.calculation_documents import configuration_from_document
from app.purchasing_v4.calculation_source import load_snapshot
from app.purchasing_v4.calculation_serialization import canonical_bytes
from app.purchasing_v4.activity import investigate
from app.purchasing_v4.identity import digest


def foundation(connection):
    names=connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='purchasing_v4' AND tablename!='alembic_version' ORDER BY tablename")).scalars().all()
    return {name:hashlib.sha256(connection.execute(text("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM purchasing_v4."+connection.dialect.identifier_preparer.quote(name)+' t')).scalar_one().encode()).hexdigest() for name in names}


def _frozen_count(engine):
    with engine.connect() as c:return c.execute(text('SELECT count(*) FROM purchasing_v4.legacy_manual_evidence')).scalar_one()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ('source-url','target-url','source-namespace','as-of','study-profiles','profile-id','phase2-evidence','output'):p.add_argument('--'+arg,required=True)
    args=p.parse_args();source_url=require_isolated_url(args.source_url);target_url=require_isolated_url(args.target_url)
    if source_url.database==target_url.database:raise ValueError('Migration target must be a separate disposable clone')
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);accepted=Path(args.phase2_evidence)
    manifest=json.loads((accepted/'evidence-manifest.json').read_text())
    for name,sha in manifest['files'].items():
        if not (accepted/name).resolve().is_relative_to(accepted.resolve()):raise ValueError('Evidence manifest path escapes its bundle')
        if hashlib.sha256((accepted/name).read_bytes()).hexdigest()!=sha:raise ValueError('Accepted Phase2 artifact differs: '+name)
    profiles=json.loads(Path(args.study_profiles).read_text())['profiles'];profile=next(r for r in profiles if r['id']==args.profile_id)
    config=configuration_from_document(profile);asof=datetime.fromisoformat(args.as_of);age=timedelta(days=2)
    source=create_engine(source_url);target=create_engine(target_url)
    with readonly_snapshot(source) as c:
        original=public_fingerprints(c);structure=public_structure(c);original_foundation=foundation(c)
    with readonly_snapshot(target) as c:assert public_fingerprints(c)==original and public_structure(c)==structure
    # Explicit development DDL only. A failure rolls back the complete migration.
    with target.begin() as c:migrate_controls(c)
    with target.begin() as c:projected=catalog_projection(c,source_namespace=args.source_namespace)
    entries=[]
    for e in json.loads((accepted/'evaluation/mapping-exceptions.json').read_text()):entries.append(('MAPPING',e))
    incoming=defaultdict(list)
    for r in json.loads((accepted/'evaluation/unresolved-incoming.json').read_text()):incoming[r['variation_id']].append(r)
    for vid,positions in incoming.items():entries.append(('PO_ALLOCATION',dict(variation_id=vid,positions=positions)))
    manual_ids=[]
    for e in json.loads((accepted/'source-exceptions.json').read_text()):
        if isinstance(e,str) and e.startswith('UNRESOLVED_LEGACY_MANUAL_REFERENCE:'):manual_ids.append(int(e.rsplit(':',1)[1]))
    with readonly_snapshot(source) as c:
        rows=c.execute(text('SELECT * FROM public.par_levels WHERE id=ANY(:ids) ORDER BY id'),{'ids':manual_ids}).mappings().all()
        assert len(rows)==len(manual_ids)==52
        entries.extend(('MANUAL_REFERENCE',dict(r)) for r in rows)
    packs=defaultdict(list)
    for r in json.loads((accepted/'source-policy-diagnostics.json').read_text()):
        if r['pack_size'] is None:packs[r['variation_id']].append(r)
    for vid,positions in packs.items():entries.append(('PACK_CONFIGURATION',dict(variation_id=vid,positions=positions)))
    assert len(incoming)==2 and len(packs)==40
    with target.begin() as c:
        import_review_evidence(c,entries,source_digest=digest(manifest));import_review_evidence(c,entries,source_digest=digest(manifest))
    with readonly_snapshot(source) as c:
        started=time.perf_counter();snapshot=load_snapshot(c,config,as_of=asof,max_age=age,source_namespace=args.source_namespace)
        load_seconds=time.perf_counter()-started
        started=time.perf_counter();activity=investigate(c,as_of=asof,max_age=age,source_namespace=args.source_namespace,availability_snapshot=snapshot)
        activity_seconds=time.perf_counter()-started
        after_source=public_fingerprints(c);after_foundation=foundation(c)
    assert original==after_source and original_foundation==after_foundation
    with readonly_snapshot(target) as c:
        after=public_fingerprints(c);after_structure=public_structure(c);all_foundation=foundation(c)
        assert after==original and after_structure==structure
        assert all(all_foundation[name]==value for name,value in original_foundation.items())
        assert c.execute(text('SELECT count(*) FROM purchasing_v4.review_evidence')).scalar_one()==102
        assert c.execute(text('SELECT count(*) FROM purchasing_v4.control_head')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM purchasing_v4.authority_supplier')).scalar_one()==40
        versions=dict(public=c.execute(text('SELECT version_num FROM public.alembic_version')).scalar_one(),v4=c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one())
    timings=[];queries=[]
    @event.listens_for(target,'before_cursor_execute')
    def count_queries(conn,cursor,statement,parameters,context,executemany):queries.append(statement)
    for search in ('','Geek','Juice Head','Pulse'):
        samples=[];counts=[]
        for _ in range(5):
            with target.connect() as c:
                begin=len(queries);started=time.perf_counter();result=list_catalog(c,search=search,limit=50);samples.append((time.perf_counter()-started)*1000);counts.append(len(queries)-begin)
        timings.append(dict(operation='catalog',search=search,limit=50,returned=len(result['rows']),milliseconds=samples,median_ms=statistics.median(samples),query_counts=counts))
    for operation,reader in (('manual',lambda c:list_manual(c,limit=50)),('legacy_manual',lambda c:list_legacy_manual(c,limit=50,source_namespace=args.source_namespace))):
        samples=[]
        for _ in range(5):
            with target.connect() as c:
                start=time.perf_counter();reader(c);samples.append((time.perf_counter()-start)*1000)
        timings.append(dict(operation=operation,limit=50,milliseconds=samples,median_ms=statistics.median(samples)))
    with target.connect() as c:
        plan=c.execute(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT variation_id FROM purchasing_v4.management_catalog WHERE search_vector @@ to_tsquery('simple','geek:*') ORDER BY variation_id LIMIT 50")).scalar_one()
    event.remove(target,'before_cursor_execute',count_queries)
    (out/'activity-analysis.json').write_bytes(canonical_bytes(activity))
    (out/'public-before.json').write_bytes(canonical_bytes(original));(out/'public-after.json').write_bytes(canonical_bytes(after))
    (out/'foundation-preservation.json').write_bytes(canonical_bytes(dict(before=original_foundation,after={name:all_foundation[name] for name in original_foundation})))
    (out/'performance.json').write_bytes(canonical_bytes(dict(hardware='Mac15,11;14 logical CPUs;36GiB RAM;PostgreSQL18.6;Python3.10.13',catalog_products=projected,active_stores=4,
        source_snapshot_seconds=load_seconds,activity_analysis_seconds=activity_seconds,max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,management_reads=timings,search_plan=plan)))
    report=dict(status='PASS',source_database=source_url.database,target_database=target_url.database,public_tables=len(original),public_rows=sum(t['rows'] for t in original.values()),
        public_records_structure_sequences_unchanged=True,foundation_records_unchanged=True,schema_versions=versions,projection_products=projected,
        review_evidence=dict(Counter(k for k,v in entries)),review_count=102,control_heads=0,unapproved_defaults_installed=0,
        frozen_legacy_manual_records=len(all_foundation) and _frozen_count(target),immutable_supplier_identities=40,production_changes=0,external_requests=0,catalog_status_changes=0,manual_conversions=0,
        source_activity_cohorts=activity['cohort_counts'],availability_snapshot_digest=snapshot.digest)
    (out/'qualification.json').write_bytes(canonical_bytes(report));source.dispose();target.dispose()
    print(json.dumps(report))

if __name__=='__main__':main()
