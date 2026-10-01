"""Deployment entrypoints must fail closed before scheduling can touch a database."""
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('gate', [None, 'false', 'TRUE'])
def test_cron_gate_requires_explicit_true_without_database_access(gate):
    env = {**os.environ, 'DATABASE_URL':'postgresql+psycopg://invalid:invalid@127.0.0.1:1/unreachable'}
    env.pop('SCHEDULE_AUTOMATION_EXECUTION_ENABLED', None)
    if gate is not None:
        env['SCHEDULE_AUTOMATION_EXECUTION_ENABLED'] = gate
    result = subprocess.run(['sh', 'scripts/release/cron.sh'], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert 'execution disabled' in result.stdout
    assert not result.stderr


def test_enabled_cron_requires_schema_validation_before_automation():
    import sys
    env = {**os.environ, 'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
           'SCHEDULE_AUTOMATION_EXECUTION_ENABLED':'true',
           'DATABASE_URL':'postgresql+psycopg://invalid:invalid@127.0.0.1:1/unreachable',
           'SCHEMA_REVISION_CHECK_ENABLED':'true'}
    result = subprocess.run(['sh', 'scripts/release/cron.sh'], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Unable to verify the database schema revision' in result.stderr
