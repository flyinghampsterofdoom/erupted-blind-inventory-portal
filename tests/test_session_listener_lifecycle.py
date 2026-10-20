"""Cold Session initialization must be independent of pytest collection order."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_purchasing_receiving_extension import database_template

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / 'tests/probes/session_listener_lifecycle.py'


def run_probe(database_template, mode, entry):
    _, url, template = database_template
    env = {**os.environ, 'DATABASE_URL': url.set(database=template).render_as_string(hide_password=False),
           'ENVIRONMENT': 'test', 'SQUARE_ACCESS_TOKEN': '', 'INTEGRATION_ENCRYPTION_KEY': '',
           'SNAPSHOT_PROVIDER': 'mock', 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, str(PROBE), mode, entry], cwd=ROOT,
                            env=env, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt['late_listener_registrations'] == []
    assert receipt['all_writes_rolled_back'] is True
    return receipt


@pytest.mark.parametrize('entry', ['database', 'models'])
@pytest.mark.parametrize('mode', ['scheduling', 'permission', 'requests'])
def test_first_writes_and_requests_in_fresh_process(database_template, mode, entry):
    run_probe(database_template, mode, entry)


def test_independent_workers_initialize_listeners_once(database_template):
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(lambda _: run_probe(database_template, 'scheduling', 'database'), range(3)))
    assert len(receipts) == 3
