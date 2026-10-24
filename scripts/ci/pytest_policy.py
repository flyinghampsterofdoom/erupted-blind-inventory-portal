"""Unexpected skips/xfails are failures; hosted integration opt-ins are explicit."""
import json
from pathlib import Path

ALLOWED_SKIPS = {
    'tests/test_purchase_order_pdf_preservation.py::test_real_isolated_private_r2_pdf_preservation',
    'tests/test_v2_digital_signage.py::test_real_r2_private_delivery_and_authentication_separation',
}
rows = []

def pytest_runtest_logreport(report):
    if report.skipped:
        rows.append({'nodeid': report.nodeid, 'reason': str(report.longrepr),
                     'allowed': report.nodeid in ALLOWED_SKIPS and not getattr(report, 'wasxfail', None)})

def pytest_collectreport(report):
    if report.skipped:
        rows.append({'nodeid': report.nodeid, 'reason': str(report.longrepr), 'allowed': False})

def pytest_sessionfinish(session, exitstatus):
    Path('ci-results').mkdir(exist_ok=True)
    Path('ci-results/skips.json').write_text(json.dumps(rows, indent=2) + '\n')
    unexpected = [row for row in rows if not row['allowed']]
    if unexpected:
        session.exitstatus = 1
        print('\nCI POLICY FAILURE: unexpected skips/xfails: ' + ', '.join(row['nodeid'] for row in unexpected))
