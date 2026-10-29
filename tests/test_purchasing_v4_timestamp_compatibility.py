"""The production Python 3.10 reader accepts PostgreSQL's exact ISO output."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from app.purchasing_v4.evidence_rules import _certificate_timestamp, verified_partial_pair, inventory_observation
from app.purchasing_v4.contracts import EvidenceState


@pytest.mark.parametrize('fraction', ['', '.1', '.03', '.035', '.0359', '.03596', '.035961'])
@pytest.mark.parametrize('offset', ['+00:00', '-07:00', '+05:30'])
def test_postgres_fractional_width_preserves_instant_offset_and_pair_proof(fraction, offset):
    raw = '2026-10-29T18:32:18' + fraction + offset
    fmt = '%Y-%m-%dT%H:%M:%S' + ('.%f' if fraction else '') + '%z'
    expected = datetime.strptime(raw, fmt)
    parsed = _certificate_timestamp(raw)
    assert parsed == expected and parsed.microsecond == expected.microsecond
    assert parsed.utcoffset() == expected.utcoffset()
    start = expected - timedelta(seconds=2)
    proof = dict(id=91, source='INVENTORY', status='PARTIAL',
                 started_at=start.isoformat(), completed_at=raw,
                 scope=dict(variation_ids=['REAL_PAIR'], store_ids=[2,4,6,9]),
                 evidence=dict(error_code='PARTIAL_COVERAGE', observed_pairs=[['REAL_PAIR',2]]))
    row = dict(result='PARTIAL', error_code='PARTIAL_COVERAGE', certificates=[proof],
               started_at=start, completed_at=expected, refreshed_at=expected,
               expected_pair_count=5288, covered_pair_count=5280, missing_pair_count=8,
               square_location_id='L2', source_calculated_at=start,
               freshness_state='FRESH', counted_quantity=10, refresh_run_id=8)
    before = deepcopy(row)
    assert verified_partial_pair(row, 'REAL_PAIR', 2)
    assert not verified_partial_pair(row, 'REAL_PAIR', 4)
    assert inventory_observation(row, 'L2', variation_id='REAL_PAIR', store_id=2,
                                as_of=expected+timedelta(minutes=1), max_age=timedelta(days=2)).state == EvidenceState.KNOWN
    assert row == before  # Never rewrite retained proof/evidence.
    row['certificates'][0]['completed_at'] = (expected+timedelta(microseconds=1)).isoformat()
    assert not verified_partial_pair(row, 'REAL_PAIR', 2)


@pytest.mark.parametrize('raw', ['not-a-date', '2026-10-29T18:32:18.03596+99:00',
    '2026-10-29T18:32:18.+00:00', '2026-10-29T18:32:18.1234567+00:00'])
def test_invalid_or_excess_precision_is_not_silently_changed(raw):
    with pytest.raises(ValueError):
        _certificate_timestamp(raw)
