"""Isolated Phase1 rehearsal against an already restored disposable database.

Never fetches a production snapshot, runs business jobs, calls Square or creates
orders. The caller must provide a loopback erup... Phase1 URL explicitly. The
separate schema and source namespace are local concepts, not Square merchant IDs.
"""
from argparse import ArgumentParser
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
from sqlalchemy import create_engine, text
from app.purchasing_v4.evidence import ReadOnlyEvidence, BUSINESS_ZONE
from app.purchasing_v4.isolation import require_isolated, require_isolated_url, readonly_snapshot
from app.purchasing_v4.schema import migrate, downgrade
from app.purchasing_v4.storage import apply_plan
from app.purchasing_v4.reconciliation import public_fingerprints, public_structure


@contextmanager
def reading(engine):
    with readonly_snapshot(engine) as connection:
        yield connection


def qualify(engine, *, source_namespace, as_of, max_age, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with engine.connect() as connection:
        require_isolated(connection)
    with reading(engine) as connection:
        before = public_fingerprints(connection)
        structure_before = public_structure(connection)
        reader = ReadOnlyEvidence(connection)
        plan = reader.identity_plan(source_namespace)
        stores = [r[0] for r in connection.execute(text('SELECT id FROM public.stores WHERE active ORDER BY id'))]
        catalog = [r[0] for r in connection.execute(text(
            'SELECT square_variation_id FROM public.ordering_catalog_identity ORDER BY square_variation_id'))]
        counts = Counter()
        unresolved = Counter()
        incoming_exceptions = []
        cutoff_day = as_of.astimezone(BUSINESS_ZONE).date()
        # Exhaustive incoming/identity inspection, bounded deterministic history sample.
        for vid in catalog:
            for store in stores:
                evidence = reader.incoming(vid, store)
                counts['incoming.'+evidence.state.value] += 1
                if evidence.reason:
                    unresolved['incoming.'+evidence.reason] += 1
                    incoming_exceptions.append(dict(square_variation_id=vid,store_id=store,
                        reason=evidence.reason,references=evidence.references))
        for vid in catalog[:12]:
            for store in stores:
                inventory = reader.inventory(vid, store, as_of=as_of, max_age=max_age)
                counts['inventory.'+inventory.state.value] += 1
                if inventory.reason:unresolved['inventory.'+inventory.reason] += 1
                sales = reader.sales(vid, store, cutoff_day-timedelta(days=14), cutoff_day,
                                     as_of=as_of, max_age=max_age)
                counts.update('sales.'+r.state.value for r in sales.values())
                unresolved.update('sales.'+r.reason for r in sales.values() if r.reason)
                availability = reader.availability(vid,store,cutoff_day-timedelta(days=14),cutoff_day,
                                                    as_of=as_of,max_age=max_age)
                counts.update('availability.'+r.availability.value for r in availability)
                unresolved.update('availability.'+r.reason for r in availability if r.reason)
        phase0 = connection.execute(text("SELECT to_regnamespace('purchasing_v4') IS NOT NULL")).scalar_one()
    class Interruption(Exception):pass
    try:
        with engine.begin() as connection:
            migrate(connection)
            apply_plan(connection, plan)
            raise Interruption()
    except Interruption:
        pass
    with reading(engine) as connection:
        phase_after = connection.execute(text("SELECT to_regnamespace('purchasing_v4') IS NOT NULL")).scalar_one()
        assert phase0 == phase_after, 'Interrupted schema creation was not rolled back'
    with engine.begin() as connection:
        migrate(connection)
        first = apply_plan(connection, plan)
    with engine.begin() as connection:
        replay = apply_plan(connection, plan)
        assert replay['replayed']
        try:
            downgrade(connection)
        except RuntimeError:
            downgrade_rejected = True
        else:
            raise AssertionError('Destructive provenance downgrade must be rejected')
    with reading(engine) as connection:
        after = public_fingerprints(connection)
        structure_after = public_structure(connection)
        foundation_counts = {r[0]:r[1] for r in connection.execute(text('''
            SELECT 'product',count(*) FROM purchasing_v4.product UNION ALL
            SELECT 'variation',count(*) FROM purchasing_v4.variation UNION ALL
            SELECT 'supplier',count(*) FROM purchasing_v4.supplier UNION ALL
            SELECT 'procurement_reference',count(*) FROM purchasing_v4.legacy_procurement_reference'''))}
        revision = connection.execute(text('SELECT version_num FROM public.alembic_version')).scalar_one()
    mismatches = sorted(name for name in set(before)|set(after) if before.get(name)!=after.get(name))
    assert not mismatches and structure_before == structure_after, 'Legacy preservation qualification failed'
    assert foundation_counts['procurement_reference'] == before['purchase_orders']['rows']
    exceptions = plan.data['exceptions']
    report = dict(status='PASS', scope='isolated Phase1 foundation only',
        source_namespace=source_namespace, evidence_cutoff=as_of.isoformat(),
        explicit_max_age_seconds=max_age.total_seconds(), source_digest=plan.data['source_digest'],
        transform_version=plan.data['transform_version'], plan_digest=plan.data['plan_digest'],
        public_schema_revision=revision, public_tables=len(before),
        public_rows=sum(row['rows'] for row in before.values()), legacy_mismatches=mismatches,
        public_structure_and_sequences_unchanged=structure_before==structure_after,
        imported=foundation_counts, first_import=first, retry=replay,
        interrupted_transaction_recovered=True, destructive_downgrade_rejected=downgrade_rejected,
        exception_counts=dict(sorted(Counter(r['code'] for r in exceptions).items())),
        evidence_counts=dict(sorted(counts.items())), evidence_unresolved=dict(sorted(unresolved.items())),
        history_sample=dict(selection='first 12 catalog variation IDs in lexical order, all active stores',
                            variations=min(12,len(catalog)),stores=len(stores),completed_days=14),
        historical_pos_referenced_not_recreated=True, production_calls=0,
        square_calls=0, vendor_emails=0, purchasing_jobs=0, production_changes=0)
    for name,value in [('before-public.json',before),('after-public.json',after),
                       ('before-structure.json',structure_before),('after-structure.json',structure_after),
                       ('identity-exceptions.json',exceptions),('incoming-exceptions.json',incoming_exceptions),
                       ('qualification.json',report)]:
        (output/name).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
    return report


if __name__ == '__main__':
    parser=ArgumentParser(description=__doc__)
    parser.add_argument('--database-url',required=True)
    parser.add_argument('--source-namespace',required=True)
    parser.add_argument('--as-of',required=True,help='Aware timestamp of retained snapshot observation')
    parser.add_argument('--max-age-hours',type=float,required=True,help='Explicit qualification input, not an active default')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.max_age_hours < 0:
        parser.error('Age limit must be nonnegative')
    require_isolated_url(args.database_url)  # Reject unsafe endpoints before connecting.
    engine=create_engine(args.database_url)
    try:
        result=qualify(engine,source_namespace=args.source_namespace,
            as_of=datetime.fromisoformat(args.as_of),max_age=timedelta(hours=args.max_age_hours),output=args.output)
        print(json.dumps(result,indent=2))
    finally:
        engine.dispose()
