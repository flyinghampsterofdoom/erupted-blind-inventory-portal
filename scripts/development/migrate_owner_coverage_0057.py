"""Exact0056→0057 pre-deploy migration with the existing scheduling lock."""
import hashlib
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from app.schema_contract import upgrade_database, assert_supported_schema

PREVIOUS = '20261107_0056'
EXPECTED = '20261119_0057'
LOCK = 73120260824
TABLES = ('schedule_periods', 'schedule_shifts', 'schedule_attendance_events',
          'schedule_coverage_commitments')


def fingerprint(connection):
    result = {}
    for table in TABLES:
        digest, count = hashlib.sha256(), 0
        projection = "(to_jsonb(t) - 'owner_principal_id')" if table == 'schedule_coverage_commitments' else 'to_jsonb(t)'
        for row in connection.scalars(text(
                "SELECT " + projection + "::text FROM public."
                + table + ' t ORDER BY id')):
            digest.update(row.encode() + b'\n'); count += 1
        result[table] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


def migrate(database_url):
    url = make_url(database_url)
    options = '-c lock_timeout=5000 -c statement_timeout=60000'
    url = url.update_query_dict({'options': options})
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as guard:
            if not guard.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': LOCK}):
                raise RuntimeError('Scheduling mutation is active; migration has not started.')
            guard.commit()
            try:
                current = guard.scalar(text('SELECT version_num FROM public.alembic_version'))
                if current not in (PREVIOUS, EXPECTED):
                    raise RuntimeError('Migration requires schema0056 or already-applied0057.')
                before = fingerprint(guard); guard.commit()
                if current == PREVIOUS:
                    upgrade_database(url.render_as_string(hide_password=False), EXPECTED)
                assert_supported_schema(engine)
                assert guard.scalar(text('SELECT version_num FROM public.alembic_version')) == EXPECTED
                assert fingerprint(guard) == before, 'Existing scheduling records changed during migration.'
                if current == PREVIOUS:
                    assert guard.scalar(text('SELECT count(*) FROM public.schedule_coverage_commitments '
                        'WHERE owner_principal_id IS NOT NULL')) == 0
                constraints = set(guard.scalars(text("SELECT conname FROM pg_constraint WHERE "
                    "conrelid='public.schedule_coverage_commitments'::regclass")))
                assert {'coverage_commitment_owner_fk', 'coverage_commitment_assignee_ck',
                        'coverage_commitment_owner_lead_ck'} <= constraints
                return {'status': 'PASS', 'from_schema': current, 'schema': EXPECTED,
                        'existing_scheduling_records_unchanged': True,
                        'scheduling_lock_used': True, 'tables': before}
            finally:
                guard.rollback()
                guard.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK}); guard.commit()
    finally:
        engine.dispose()


if __name__ == '__main__':
    from app.config import settings
    from app.services.runtime_identity_service import capture_identity
    identity = capture_identity()
    assert identity['status'] == 'VERIFIED'
    assert os.environ.get('RENDER_SERVICE_ID') == 'srv-d6ccg9ktgctc738u1c1g'
    assert make_url(settings.database_url_normalized).host.startswith('dpg-d6ccc3kr85hc7384neo0-a')
    print('ERUPTED_MIGRATION_0057 ' + json.dumps(migrate(settings.database_url_normalized)), flush=True)
