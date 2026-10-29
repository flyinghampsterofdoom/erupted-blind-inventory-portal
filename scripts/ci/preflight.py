"""Fail before tests if isolation, dependencies, runtime or checkout are wrong."""
import json
import os
from pathlib import Path
import platform
import subprocess
from urllib.parse import urlsplit
import psycopg

assert platform.python_version() == '3.10.13'
assert os.environ['ENVIRONMENT'] == 'test'
assert not Path('.env').exists(), 'CI cannot load a workstation environment'
assert not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all']).strip(), 'Checkout must start clean'
for key in ('RENDER_API_KEY', 'RENDER_API_TOKEN', 'SQUARE_ACCESS_TOKEN', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'INTEGRATION_ENCRYPTION_KEY', 'FORECAST_VALIDATION_BACKUP_SECRET_ACCESS_KEY'):
    assert not os.environ.get(key), 'Forbidden CI credential present: ' + key
for key in ('DATABASE_URL', 'TEST_POSTGRES_ADMIN_URL', 'P1_TEST_DATABASE_URL'):
    url = urlsplit(os.environ[key].replace('postgresql+psycopg:', 'postgresql:'))
    expected_port = 5433 if key == 'TEST_POSTGRES_ADMIN_URL' else 5432
    assert url.hostname == '127.0.0.1' and url.port == expected_port
    assert url.username == 'postgres' and url.password == 'ci_only'
assert os.environ['SCHEDULE_AUTOMATION_EXECUTION_ENABLED'] == 'false'
assert os.environ['FORECAST_VALIDATION_WORKER_ENABLED'] == 'false'
assert os.environ['FORECAST_VALIDATION_SCHEDULE_ENABLED'] == 'false'
with psycopg.connect('postgresql://postgres:ci_only@127.0.0.1:5432/postgres') as db:
    version = db.execute('SHOW server_version').fetchone()[0]
    assert version.startswith('18.6 '), version
for executable in ('pg_dump', 'pg_restore'):
    assert '18.6' in subprocess.check_output([executable, '--version'], text=True)
# checkout action must not leave a push credential in Git configuration.
auth = subprocess.run(['git', 'config', '--local', '--get-regexp', r'http\..*extraheader'], capture_output=True)
assert auth.returncode == 1, 'Checkout retained an authentication header'
print(json.dumps({'status': 'PASS', 'clean_checkout': True, 'postgres_version': version,
                  'database_scope': 'disposable loopback service only', 'production_credentials': False}))
