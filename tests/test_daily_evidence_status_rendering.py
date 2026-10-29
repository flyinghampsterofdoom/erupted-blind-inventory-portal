"""Populated collector status must satisfy both portal and JSON boundaries."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest
from starlette.requests import Request

from app.config import settings
from app.services.daily_evidence_runtime import status
from app.services import v2_square_data_service as square

NOW = datetime(2026, 10, 29, 16, tzinfo=timezone.utc)
DATES = ('last_successful_at', 'coverage_start_at', 'coverage_through_at',
         'required_start_at', 'required_through_at')


def assert_rendered_contract(monkeypatch, connection, now, expected_state):
    from app.main import app, _format_portal_datetime
    from app.auth import Principal, Role

    monkeypatch.setattr(settings, 'daily_evidence_collector_enabled', True)
    monkeypatch.setattr(square, '_now', lambda: now)
    db = Mock()
    db.connection.return_value = connection
    internal = square.square_data_status(db, now=now)
    assert internal['state'] == expected_state
    assert all(isinstance(internal[key], datetime) and internal[key].tzinfo
               for key in DATES if internal[key] is not None)
    # Exercise the actual shared base used by overview, admin, and reports.
    for path in ('/v2/overview', '/v2/admin', '/v2/reports'):
        request = Request({'type': 'http', 'path': path, 'root_path': '',
                           'scheme': 'https', 'server': ('testserver', 443),
                           'query_string': b'', 'headers': [], 'app': app})
        request.state.square_data_status = internal
        principal = Principal(id=6, username='owner', role=Role.ADMIN,
                              store_id=None, active=True)
        html = app.state.templates.env.get_template('v2/base.html').render(
            request=request, principal=principal, navigation=[],
            page=SimpleNamespace(label='Overview', description='', badge='Operations'),
            stores=[], current_store=None)
        assert 'Square Data' in html and internal['label'] in html
        if internal['coverage_through_at']:
            assert _format_portal_datetime(internal['coverage_through_at']) in html
        if expected_state == 'incomplete':
            assert 'Required through ' + _format_portal_datetime(internal['required_through_at']) in html
    wire = square.serializable_square_data_status(db)
    json.dumps(wire)
    for key in DATES:
        assert wire[key] == (internal[key].isoformat() if internal[key] else None)


@pytest.mark.parametrize('case,expected', [('COMPLETE', 'current'), ('PARTIAL', 'incomplete'),
    ('STALE', 'stale'), ('FAILED', 'failed'), ('NEVER', 'never')])
def test_populated_status_renders_and_serializes(monkeypatch, case, expected):
    completed = NOW - timedelta(days=2) if case == 'STALE' else NOW - timedelta(minutes=10)
    cutoff = datetime(2026, 10, 29, 7, tzinfo=timezone.utc)
    success = None if case == 'NEVER' else dict(id='retained-attempt', completed_at=completed,
        status='PARTIAL' if case == 'PARTIAL' else 'COMPLETE',
        manifest=dict(sales_end=cutoff.isoformat(), qualified_counts=5280 if case == 'PARTIAL' else 5288,
                      expected_counts=5288, scope={'stores': [(i, f'L{i}') for i in range(4)]}))
    latest = dict(id='failed-attempt', status='FAILED', error_code='SOURCE_READ_FAILED') if case == 'FAILED' else success
    coverage = [dict(source=source, location_id=f'L{i}', start_at=cutoff-timedelta(days=126),
                     through_at=cutoff, verified_at=completed)
                for i in range(4) for source in ('SALES', 'MOVEMENTS')]
    connection = Mock()
    def execute(statement, *args):
        query = str(statement)
        result = Mock()
        if 'purchasing_history_coverage' in query:
            result.mappings.return_value.all.return_value = coverage
        else:
            result.mappings.return_value.first.return_value = success if "WHERE status IN" in query else latest
        return result
    connection.execute.side_effect = execute
    assert_rendered_contract(monkeypatch, connection, NOW, expected)
