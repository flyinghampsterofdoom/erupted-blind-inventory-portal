"""Small immutable read models, derived from the same frozen evidence object.

No forecasting or purchasing calculation is performed on a dashboard request.
Raw provenance stays in the complete, independently exportable artifact.
"""
from collections import Counter, defaultdict
from fractions import Fraction

from .contracts import MODELS, EvidenceError, canonical

MAX_VIEW_BYTES = 64 * 1024
MAX_VIEWS = 12_100


def _without_raw(row):
    return {**{k: v for k, v in row.items() if k != 'source_records'},
            'source_record_count': len(row.get('source_records', []))}


def run_summary(payload):
    snapshot = payload['snapshot']
    forecasts = payload['forecasts']
    if len(forecasts) != len(snapshot['rows']):
        raise EvidenceError('Forecast projection population mismatch')
    exposure = []
    for model in MODELS:
        values = [item['models'][model] for item in payload['purchases']]
        known = [item for item in values if item['cost'] is not None]
        exposure.append({'model': model,
                         'cost': str(sum((Fraction(item['cost']) for item in known), Fraction(0))),
                         'missing': len(values) - len(known),
                         'abstentions': sum(item['models'][model]['rate'] is None for item in forecasts)})
    costs = [item['current_without_stockouts']['cost'] for item in payload['purchases']]
    control = str(sum((Fraction(v) for v in costs), Fraction(0))) if all(v is not None for v in costs) else None
    impact = (str(Fraction(exposure[1]['cost']) - Fraction(control))
              if control is not None and exposure[1]['missing'] == 0 else None)
    availability = Counter(v for row in snapshot['rows'] for v in row['verified_availability'])
    return {'schema': 'forecast-summary-v1',
            'snapshot': {k: snapshot[k] for k in ('scope', 'origin', 'cutoff', 'evaluation_start', 'kind', 'stores')},
            'model_version': payload['model_version'], 'policy_version': payload['policy_version'],
            'population': len(forecasts), 'variations': len(payload['purchases']),
            'exposure': exposure, 'control': control, 'impact': impact,
            'reliability': {k: availability[k] for k in ('IN_STOCK', 'STOCKOUT', 'PARTIAL', 'UNKNOWN')},
            'unknown_sales_pairs': sum(not row['sales_complete'] for row in snapshot['rows'])}


def evaluation_summary(payload):
    return {'schema': 'evaluation-summary-v1',
            'metrics': [m for m in payload['metrics'] if m['dimension'] == 'class' and m['value'] == m['cohort']],
            'classification': payload['classification'], 'prospective': payload['prospective'],
            'population': payload['population'], 'abstentions': payload['abstentions'],
            'coverage_counts': dict(Counter(v['class'] for v in payload['coverage'])),
            'source_revision': payload.get('source_revision'),
            'supersedes_id': payload['outcome'].get('supersedes_id')}


def views(payload):
    """Yield bounded rows, without retaining a second whole evidence graph."""
    if payload.get('schema') == 'forecast-evidence-v1':
        from .shared import verify_snapshot, record_digest
        certificate = verify_snapshot(payload)
        yield ('source-summary', '', None, None, None, certificate)
        for row in payload['rows']:
            key = str(row['store_id'])
            yield ('source-row', row['variation_id'] + ':' + key, row['variation_id'], row['sku'], key,
                   {**_without_raw(row), 'source_records_sha256': record_digest(row['source_records'])})
    elif payload.get('schema') == 'forecast-result-v1':
        yield ('run-summary', '', None, None, None, run_summary(payload))
        for row, forecast in zip(payload['snapshot']['rows'], payload['forecasts']):
            key = (str(row['store_id']), row['variation_id'])
            if key != (str(forecast['store_id']), forecast['variation_id']):
                raise EvidenceError('Forecast projection identity mismatch')
            yield ('run-row', key[1] + ':' + key[0], key[1], row['sku'], key[0],
                   {'input': _without_raw(row), 'forecast': forecast})
    elif payload.get('schema') == 'forecast-evaluation-v1':
        yield ('evaluation-summary', '', None, None, None, evaluation_summary(payload))
        coverage = {(str(v['store_id']), v['variation_id']): v for v in payload['coverage']}
        cases = defaultdict(list)
        for case in payload['cases']:
            cases[(str(case['store']), case['variation_id'])].append(case)
        outcomes = {(str(v['store_id']), v['variation_id']): v for v in payload['outcome']['rows']}
        for key, item in coverage.items():
            row = outcomes.get(key)
            yield ('evaluation-row', key[1] + ':' + key[0], key[1], None, key[0],
                   {'coverage': item, 'outcome': _without_raw(row) if row else None, 'cases': cases[key]})
    else:
        raise EvidenceError('Unsupported projected evidence schema')


def encoded_views(payload):
    count = 0
    for kind, key, variation, sku, store, value in views(payload):
        data = canonical(value)
        count += 1
        if len(data.encode()) > MAX_VIEW_BYTES or count > MAX_VIEWS:
            raise EvidenceError('Projection bound exceeded; complete raw evidence is not truncated')
        yield {'kind': kind, 'item_key': key, 'variation_id': variation, 'sku': sku,
               'store_id': store, 'payload': data}
