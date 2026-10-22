"""One frozen source observation, reused across forecasts and mature outcomes.

Freshness and raw-record reconciliation are separate from caching: an artifact
existing in storage is not a completeness certificate. No Square calls here.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from hashlib import sha256

from .contracts import EvidenceError, TZ, stamp, number, validate, SCHEMA
from .codec import canonical_parts
from .exposure import normalize, states, closing_balance

VERIFICATION = 'raw-sales-and-forward-inventory-v1'


def stream_digest(value):
    result = sha256()
    for part in canonical_parts(value):
        result.update(part)
    return result.hexdigest()


def record_digest(records):
    return stream_digest(records)


def reconcile_row(row, *, cutoff, location, start, length=84, synthetic=False):
    """Independently reconstruct daily sales and verified inventory availability."""
    cutoff = stamp(cutoff)
    records = row.get('source_records', [])
    seen = set()
    sales = defaultdict(Fraction)
    absolute = []
    counts = []
    coverage = []
    for record in records:
        if (not record.get('id') or not record.get('source') or 'version' not in record
                or 'data' not in record or stamp(record['known_at']) > cutoff
                or stamp(record['occurred_at']) > cutoff):
            raise EvidenceError('Outcome/source record exceeds cutoff or lacks identity')
        identity = (record['source'], record['id'])
        if identity in seen:
            raise EvidenceError('Duplicate retained source identity')
        seen.add(identity)
        kind, data = record['source'], record['data']
        if kind in ('square_order_line', 'square_line_correction'):
            day = date.fromisoformat(data['business_date']) if data.get('business_date') else stamp(record['occurred_at']).astimezone(TZ).date()
            if data.get('location_id') and data['location_id'] != location:
                raise EvidenceError('Sales source has the wrong store')
            sales[day] += Fraction(number(data['quantity']))
        elif kind == 'square_absolute_inventory_event':
            raw = data.get('physical_count') or data.get('adjustment') or {}
            if raw.get('location_id') != location or raw.get('catalog_object_id') != row['variation_id']:
                raise EvidenceError('Absolute inventory source has the wrong identity')
            absolute.append(data)
        elif kind == 'square_inventory_count':
            if record['id'] != location + ':' + row['variation_id']:
                raise EvidenceError('Current inventory source has the wrong identity')
            counts.append(data)
        elif kind == 'coverage' and record['id'] == 'sales:' + location:
            coverage.append(record)
    days = [start + timedelta(days=i) for i in range(length)]
    if synthetic:
        # Explicit local-only fixtures cannot enter a production Store. They are
        # not presented as independently certified Square source observations.
        return None, None
    complete = bool(coverage and any(stamp(c['occurred_at']) <= datetime.combine(start, datetime.min.time(), TZ)
                    and stamp(c['data']['through_at']) >= datetime.combine(start + timedelta(days=length), datetime.min.time(), TZ)
                    and timedelta(0) <= cutoff - stamp(c['known_at']) <= timedelta(hours=24)
                    for c in coverage))
    expected_sales = [str(sales[d]) for d in days]
    on_hand = row.get('on_hand')
    if on_hand is not None:
        if len(counts) != 1 or Fraction(counts[0]['quantity']) != Fraction(on_hand):
            raise EvidenceError('Inventory observation differs from retained source')
    events = normalize(absolute, cutoff).get((location, row['variation_id']), [])
    availability = (states(events, days) if on_hand is not None
                    and closing_balance(events, cutoff) == Fraction(on_hand) else ['UNKNOWN'] * length)
    return (expected_sales if complete else None), availability


def verify_snapshot(snapshot):
    validate(snapshot, now=stamp(snapshot['origin']))
    synthetic = snapshot.get('kind') == 'SYNTHETIC'
    if not synthetic:
        from .collection import SOURCE_MODELS
        expected_sources = {model.__tablename__ for model in SOURCE_MODELS}
        collection = snapshot.get('collection', {})
        before, after = collection.get('source_before'), collection.get('source_after')
        batches = collection.get('history_batches', [])
        variations = {row['variation_id'] for row in snapshot['rows']}
        if (not before or set(before) != expected_sources or before != after or collection.get('source_changes_detected')
                or collection.get('source_transaction_during_network') is not False
                or len(batches) != (len(variations) + 99) // 100
                or any(not b.get('pagination_exhausted') or b['offset'] != i * 100
                       or b['variations'] != min(100, len(variations) - i * 100)
                       for i, b in enumerate(batches))):
            raise EvidenceError('Shared source lacks complete, consistent pagination/snapshot proof')
    for row in snapshot['rows']:
        location = snapshot['stores'][str(row['store_id'])]['location_id']
        sales, availability = reconcile_row(row, cutoff=snapshot['cutoff'], location=location,
                                           start=date.fromisoformat(row['history_start']), synthetic=synthetic)
        if not synthetic:
            if bool(sales is not None) != bool(row['sales_complete']):
                raise EvidenceError('Sales completeness flag differs from source coverage')
            if sales is not None and [Fraction(v) for v in sales] != [Fraction(v) for v in row['sales']]:
                raise EvidenceError('Daily sales differ from retained source records')
            if availability != row['verified_availability']:
                raise EvidenceError('Availability differs from independent forward inventory ledger')
    return {'verification': VERIFICATION, 'population': len(snapshot['rows']),
            'origin': snapshot['origin'], 'cutoff': snapshot['cutoff'],
            'history_start': snapshot['rows'][0]['history_start'], 'stores': snapshot['stores'],
            'kind': snapshot.get('kind', 'PROSPECTIVE')}


class FrozenSource:
    def __init__(self, snapshot, *, artifact=None, verified=False):
        self.snapshot = snapshot
        self.certificate = verify_snapshot(snapshot) if not verified else None
        self.artifact = artifact or stream_digest(snapshot)
        self.rows = {(str(row['store_id']), row['variation_id']): row for row in snapshot['rows']}

    def outcome(self, store, run_id, horizon, *, revision=None, supersedes_id=None):
        summary = store.get_run_summary(run_id)
        if not summary:
            raise EvidenceError('Original forecast is not indexed')
        original = summary['snapshot']
        start = date.fromisoformat(original['evaluation_start'])
        end = start + timedelta(days=horizon)
        if horizon not in (28, 56) or stamp(self.snapshot['cutoff']) < datetime.combine(end, datetime.min.time(), TZ):
            raise EvidenceError('Outcome not mature at the frozen source cutoff')
        history_start = date.fromisoformat(self.snapshot['rows'][0]['history_start'])
        if start < history_start or end > history_start + timedelta(days=84):
            raise EvidenceError('Outcome outside the bounded source window')
        if original['stores'] != self.snapshot['stores']:
            raise EvidenceError('Store identity changed; explicit reconciliation required')
        offset = (start - history_start).days
        rows = []
        for pair in store.iter_run_pairs(run_id):
            identity = (str(pair['input']['store_id']), pair['input']['variation_id'])
            row = self.rows.get(identity)
            item = {'store_id': identity[0], 'variation_id': identity[1], 'complete': False,
                    'days': [], 'source_records': []}
            if row:
                item.update(complete=row['sales_complete'], history_start=row['history_start'], on_hand=row['on_hand'],
                            source_records=row['source_records'], source_records_sha256=record_digest(row['source_records']),
                            days=[{'date': str(start + timedelta(days=i)), 'units': row['sales'][offset+i],
                                   'availability': row['verified_availability'][offset+i]} for i in range(horizon)])
            rows.append(item)
        outcome = {'horizon': horizon, 'observed_at': self.snapshot['origin'], 'cutoff': self.snapshot['cutoff'],
                   'rows': rows, 'source_artifact': self.artifact, 'verification': VERIFICATION,
                   'observational_events': self.snapshot.get('observational_events', []),
                   'revision': revision or self.artifact}
        if supersedes_id:
            outcome['supersedes_id'] = supersedes_id
        return outcome


def verify_outcome(store, original, outcome):
    """Bind daily outcomes to an immutable, raw-reconciled source projection."""
    if original['kind'] == 'SYNTHETIC':
        return  # Existing explicit local mathematical fixtures only.
    artifact = outcome.get('source_artifact')
    certificate = store.artifact_view(artifact, 'source-summary') if artifact else None
    if not certificate or certificate.get('verification') != VERIFICATION:
        raise EvidenceError('Outcome requires an independently reconciled frozen source artifact')
    if (stamp(outcome['cutoff']) != stamp(certificate['cutoff'])
            or stamp(outcome['observed_at']) != stamp(certificate['origin'])
            or original['stores'] != certificate['stores']):
        raise EvidenceError('Outcome/source observation boundary mismatch')
    rows = {(str(row['store_id']), row['variation_id']): row for row in outcome['rows']}
    seen = set()
    start = date.fromisoformat(original['evaluation_start'])
    for source in store.iter_artifact_views(artifact, 'source-row'):
        identity = (str(source['store_id']), source['variation_id'])
        row = rows.get(identity)
        if row is None:
            continue
        seen.add(identity)
        if record_digest(row.get('source_records', [])) != source['source_records_sha256']:
            raise EvidenceError('Outcome provenance differs from the certified source')
        first = date.fromisoformat(source['history_start'])
        offset = (start - first).days
        if offset < 0 or offset + outcome['horizon'] > 84:
            raise EvidenceError('Outcome outside certified history')
        expected = [{'date': str(start + timedelta(days=i)), 'units': source['sales'][offset+i],
                     'availability': source['verified_availability'][offset+i]} for i in range(outcome['horizon'])]
        if (row.get('days') != expected or row.get('complete') != source['sales_complete']
                or row.get('history_start') != source['history_start'] or row.get('on_hand') != source['on_hand']):
            raise EvidenceError('Outcome daily values differ from the immutable source')
        sales, availability = reconcile_row(row, cutoff=outcome['cutoff'], location=original['stores'][identity[0]]['location_id'],
                                           start=first)
        if sales is not None and [Fraction(v['units']) for v in expected] != [Fraction(v) for v in sales[offset:offset+outcome['horizon']]]:
            raise EvidenceError('Outcome sales fail independent raw-source reconciliation')
        if [v['availability'] for v in expected] != availability[offset:offset+outcome['horizon']]:
            raise EvidenceError('Outcome availability fails independent ledger reconciliation')
    for identity, row in rows.items():
        if identity not in seen and (row.get('complete') or row.get('days') or row.get('source_records')):
            raise EvidenceError('Missing/deleted source cannot certify an outcome')


def outcome_revision(outcome):
    """Ignore labels/re-observation times, not changed historical source facts."""
    rows = []
    start = min((d['date'] for r in outcome['rows'] for d in r.get('days', [])), default='9999-12-31')
    end = (date.fromisoformat(start) + timedelta(days=outcome['horizon'])).isoformat() if start != '9999-12-31' else start
    for row in sorted(outcome['rows'], key=lambda r: (str(r['store_id']), r['variation_id'])):
        evidence = []
        for record in row.get('source_records', []):
            kind = record['source']
            # Match the collector's business-date attribution, including late
            # corrections whose transaction timestamp is outside that date.
            day = (record['data']['business_date'] if kind in ('square_order_line', 'square_line_correction')
                   and record['data'].get('business_date')
                   else stamp(record['occurred_at']).astimezone(TZ).date().isoformat())
            if kind in ('square_order_line', 'square_line_correction') and start <= day < end:
                evidence.append({k: record[k] for k in ('source', 'id', 'version', 'occurred_at', 'data')})
            elif kind == 'square_absolute_inventory_event' and day < end:
                evidence.append({k: record[k] for k in ('source', 'id', 'version', 'occurred_at', 'data')})
        rows.append({'store_id': str(row['store_id']), 'variation_id': row['variation_id'],
                     'complete': row.get('complete'), 'days': row.get('days', []),
                     'historical_records': sorted(evidence, key=lambda r: (r['source'], r['id']))})
    return stream_digest({'horizon': outcome['horizon'], 'rows': rows})
