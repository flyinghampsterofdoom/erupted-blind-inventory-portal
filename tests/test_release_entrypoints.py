"""Exercise Render's exact Python entrypoint and all shared automation callers."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
DISABLED = [None, '', 'false', '0', 'no', 'off', 'invalid', 'truthy', '2', 'FALSE']
ENABLED = ['true', '1', 'yes', 'on', 'TRUE', ' On ']
COMMANDS = [[sys.executable, 'scripts/run_schedule_automation.py'],
            [sys.executable, '-m', 'scripts.run_schedule_automation'],
            ['sh', 'scripts/release/cron.sh']]


def execution_env(gate):
    env = {**os.environ, 'PYTHONPATH':str(ROOT),
           'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
           'DATABASE_URL':'postgresql+psycopg://invalid:invalid@127.0.0.1:1/unreachable',
           'SCHEMA_REVISION_CHECK_ENABLED':'true'}
    env.pop('SCHEDULE_AUTOMATION_EXECUTION_ENABLED', None)
    if gate is not None:
        env['SCHEDULE_AUTOMATION_EXECUTION_ENABLED'] = gate
    return env


@pytest.mark.parametrize('gate', DISABLED)
@pytest.mark.parametrize('command', COMMANDS)
def test_disabled_entrypoint_never_imports_database_or_automation(gate, command, tmp_path):
    # Abort even an attempted import of any application/database execution module.
    # This is stronger than checking unchanged rows: no DB operation can start.
    (tmp_path/'sitecustomize.py').write_text('''import sys
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'app.db', 'app.config', 'app.schema_contract', 'app.services.v2_scheduling_policy_service', 'sqlalchemy', 'psycopg'}:
            raise AssertionError('Disabled cron imported execution dependency: '+fullname)
sys.meta_path.insert(0, Guard())
''')
    env = execution_env(gate)
    # Wrapper resets PYTHONPATH: use an invalid configuration there to detect
    # eager settings imports. The exact Render command gets the import sentinel.
    if command[0] == 'sh':
        env['DATABASE_URL'] = 'invalid database URL that must never be parsed'
    else:
        env['PYTHONPATH'] = str(tmp_path)+os.pathsep+str(ROOT)
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'SCHEDULE_AUTOMATION_DISABLED' in result.stdout
    assert "'ok': False" in result.stdout
    assert "'status': 'disabled'" in result.stdout
    assert "'generated_period_ids': []" in result.stdout
    assert "'published_period_ids': []" in result.stdout


@pytest.mark.parametrize('gate', ENABLED)
@pytest.mark.parametrize('command', COMMANDS)
def test_enabled_entrypoint_reaches_schema_gate_before_automation(gate, command):
    result = subprocess.run(command, cwd=ROOT, env=execution_env(gate), capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Unable to verify the database schema revision' in result.stderr
    assert 'SCHEDULE_AUTOMATION_DISABLED' not in result.stdout


@pytest.mark.parametrize('gate', DISABLED)
def test_direct_service_and_manual_api_cannot_bypass_gate(gate, monkeypatch, caplog):
    caplog.set_level("INFO", logger="app.services.scheduling_execution_control")
    from app.services.v2_scheduling_policy_service import run_schedule_automation
    from app.routers.v2_scheduling import run_automation_api
    if gate is None:
        monkeypatch.delenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED', raising=False)
    else:
        monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED', gate)

    class NoDatabaseWork:
        def __getattr__(self, name):
            raise AssertionError('Disabled automation attempted DB work: '+name)
        def commit(self):
            # The HTTP route commits its caller-owned transaction, but the service
            # must not issue SQL, stage changes or flush scheduling state.
            pass

    db = NoDatabaseWork()
    result = run_schedule_automation(db, principal=None)
    assert result['status'] == 'disabled' and result['ok'] is False
    assert result['generated_period_ids'] == result['published_period_ids'] == []
    response = run_automation_api(request=None, _feature=None, principal=None, db=db, _csrf=None)
    assert response == result  # 'ok': True from the route cannot conceal the gate.
    assert 'SCHEDULE_AUTOMATION_DISABLED' in caplog.text
