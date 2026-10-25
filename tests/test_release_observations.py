"""Failure injection uses synthetic identities, disposable PostgreSQL, no secrets."""
import base64
from datetime import datetime, timedelta, timezone
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import event, text, create_engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.config import settings
from app.services import deployment_observation_service as service
from app.services.cron_observation_service import receipt
from scripts.release.phase3 import observations as verify
from scripts.release.phase3.verification import production_readiness
from test_v2_primary import primary
from test_v2_scheduling_foundation import scheduling_db

NOW = datetime(2026, 10, 25, 10, 30, tzinfo=timezone.utc)
IDENTITY = {'revision': 'a'*40, 'source_tree_sha256': 'b'*64, 'status': 'VERIFIED'}
EXPECTED = {'revision': 'a'*40, 'source_tree_sha256': 'b'*64, 'schema': '20261024_0050'}
NONCE = 'f'*32


def key():
    private = Ed25519PrivateKey.generate()  # Ephemeral fixture, never production.
    return private, base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def signature(private, timestamp=None, nonce=NONCE):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    return {'X-Observation-Timestamp': timestamp, 'X-Observation-Nonce': nonce,
        'X-Observation-Signature': base64.b64encode(private.sign(service.signing_body(timestamp, nonce))).decode()}


def response():
    return {'status': 200, 'body': {'contract': service.CONTRACT, 'nonce': NONCE,
        'observed_at': NOW.isoformat(), 'web': IDENTITY,
        'application': service.finding('PASS', 'source-intact'),
        'database': {**service.finding('PASS', 'connected-compatible-schema'), 'schema': EXPECTED['schema'], 'read_only': True},
        'authorization': {**service.finding('PASS', 'sampled-existing-policy-dependencies'),
            'roles': {r: service.finding('PASS', 'policy-boundaries') for r in ('owner', 'lead', 'employee')}}}}


def observed(value=None):
    return verify.probe(lambda: value or response(), expected=EXPECTED, nonce=NONCE, now=NOW)


def cron_config():
    return {'enabled': True, 'schedule': '0 10 * * 0', 'service_id': 'cron-fixture',
        'revision': EXPECTED['revision'], 'deployed_at': (NOW-timedelta(days=1)).isoformat(),
        'last_success': (NOW-timedelta(minutes=20)).isoformat()}


def terminal(phase='committed', work='no-work-due'):
    return {'event': 'ERUPTED_CRON_EXECUTION_V1', 'phase': phase, 'work': work, 'run_id': NONCE,
        'service_id': 'cron-fixture', 'revision': EXPECTED['revision'],
        'source_tree_sha256': EXPECTED['source_tree_sha256'], 'identity_status': 'VERIFIED',
        'schema': EXPECTED['schema'], 'observed_at': (NOW-timedelta(minutes=20)).isoformat(),
        'durable_work': {'status': 'NO_CHANGES', 'recorded_at': (NOW-timedelta(minutes=21)).isoformat(), 'generated_count': 0},
        'counts': {'generated_period_ids': 0, 'published_period_ids': 0, 'blocked_period_ids': 0}}


def cron(config=None, receipts=None):
    return verify.cron(config or cron_config(), expected=EXPECTED, schema=EXPECTED['schema'],
        receipts=receipts if receipts is not None else [terminal()], now=NOW,
        durable_work=terminal()['durable_work'])


@pytest.mark.parametrize('ledger', [None, {'status': 'SUCCESS', 'recorded_at': NOW.isoformat(), 'generated_count': 1}])
def test_missing_or_unrelated_durable_work_cannot_pass(ledger):
    result = verify.cron(cron_config(), expected=EXPECTED, schema=EXPECTED['schema'],
        receipts=[terminal()], now=NOW, durable_work=ledger)
    assert result['expected_work']['status'] == 'INCONCLUSIVE'


def test_healthy_independent_qualification():
    result = production_readiness(observed(), cron())
    assert result['overall']['status'] == 'PASS'


@pytest.mark.parametrize('status,code', [(404, 'observation-not-activated'), (403, 'verification-identity-rejected'),
    (401, 'verification-identity-rejected'), (429, 'observation-busy')])
def test_observation_unavailable_never_passes(status, code):
    result = observed({'status': status})
    assert result['database']['code'] == code
    assert result['application']['status'] != 'FAIL'  # Not evidence of outage.


def test_network_interruption_is_unknown_not_application_failure():
    def broken():
        raise TimeoutError('secret network detail')
    result = verify.probe(broken, expected=EXPECTED, nonce=NONCE, now=NOW)
    assert result['application'] == service.finding('INCONCLUSIVE', 'verification-network-interruption')
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('part', ['nonce', 'observed_at', 'roles', 'schema', 'read_only', 'revision'])
def test_incomplete_stale_or_substituted_evidence_cannot_pass(part):
    value = response(); body = value['body']
    if part == 'nonce': body['nonce'] = 'x'
    if part == 'observed_at': body['observed_at'] = (NOW-timedelta(minutes=3)).isoformat()
    if part == 'roles': body['authorization']['roles'].pop('employee')
    if part == 'schema': body['database']['schema'] = 'wrong'
    if part == 'read_only': body['database']['read_only'] = False
    if part == 'revision': body['web'] = {**IDENTITY, 'revision': 'c'*40}
    assert verify.aggregate(observed(value).values())['status'] != 'PASS'


@pytest.mark.parametrize('mutation', ['missing', 'not-yet', 'failed', 'disabled', 'wrong-revision', 'wrong-schema',
    'stale', 'unrelated-service', 'missing-provider', 'blocked', 'policy-disabled', 'unsupported-schedule'])
def test_cron_independent_failures_and_absent_evidence(mutation):
    config = cron_config(); rows = [terminal()]
    expected = 'INCONCLUSIVE'
    if mutation == 'missing': rows = []
    if mutation == 'not-yet':
        config['deployed_at'] = (NOW-timedelta(minutes=5)).isoformat(); rows = []
        expected = 'NOT YET OBSERVABLE'
    if mutation in ('failed', 'disabled'):
        rows[0]['phase'] = mutation; expected = 'FAIL'
    if mutation == 'wrong-revision': rows[0]['revision'] = 'c'*40; expected = 'FAIL'
    if mutation == 'wrong-schema': rows[0]['schema'] = 'old'; expected = 'FAIL'
    if mutation == 'stale': rows[0]['observed_at'] = (NOW-timedelta(hours=2)).isoformat()
    if mutation == 'unrelated-service': rows[0]['service_id'] = 'other'
    if mutation == 'missing-provider': config['last_success'] = None
    if mutation in ('blocked', 'policy-disabled'):
        rows[0]['work'] = mutation; expected = 'FAIL'
    if mutation == 'unsupported-schedule': config['schedule'] = '*/5 * * * *'
    result = cron(config, rows)
    assert result['overall']['status'] == expected, result
    assert production_readiness(observed(), result)['application_readiness']['status'] == 'PASS'


def test_deployed_web_cron_mismatch():
    config = cron_config(); config['revision'] = 'd'*40
    result = cron(config, [terminal()])  # Even a valid receipt cannot mask provider mismatch.
    assert result['source_schema']['status'] == 'FAIL'
    assert production_readiness(observed(), result)['web_cron_compatibility']['status'] == 'FAIL'


def test_real_database_unavailable_is_bounded_and_redacted():
    def refused():
        return create_engine('postgresql+psycopg://unused@127.0.0.1:1/unused',
                             poolclass=NullPool, connect_args={'connect_timeout': 1})
    started = time.monotonic()
    result = service.observe(IDENTITY, IDENTITY, engine_factory=refused)
    assert result['database']['code'] == 'database-unavailable'
    assert result['authorization']['status'] == 'INCONCLUSIVE'
    assert time.monotonic()-started < 3
    assert '127.0.0.1' not in json.dumps(result) and 'unused' not in json.dumps(result)


def test_weekly_clock_not_hourly_assumption():
    assert verify.latest_due('0 10 * * 0', NOW) == NOW.replace(minute=0)
    assert verify.latest_due('0 10 * * 0', NOW-timedelta(hours=1)) == NOW.replace(minute=0)-timedelta(days=7)


@pytest.mark.parametrize('fraction', ['1', '12', '123', '1232', '32328', '123456', '1234567', '12345678', '123456789'])
def test_render_fractional_widths_qualify_without_python310_parser_failure(fraction):
    config = cron_config(); row = terminal()
    config['deployed_at'] = '2026-10-24T21:59:12.'+fraction+'Z'
    config['last_success'] = '2026-10-25T10:10:00.'+fraction+'Z'
    row['observed_at'] = config['last_success']
    result = cron(config, [row])
    assert result['overall']['status'] == 'PASS', result
    assert verify.timestamp(config['deployed_at']).tzinfo == timezone.utc


def test_nanosecond_before_deployment_receipt_is_not_accepted():
    config = cron_config(); config['deployed_at'] = '2026-10-25T10:10:00.123456789Z'
    row = terminal(); row['observed_at'] = '2026-10-25T10:10:00.123456788Z'
    result = cron(config, [row])
    assert result['recent_execution']['status'] != 'PASS'


def test_provider_success_outside_window_by_one_nanosecond_is_not_correlated():
    config = cron_config(); config['last_success'] = '2026-10-25T10:15:00.000000001Z'
    result = cron(config, [terminal()])
    assert result['recent_execution']['code'] == 'provider-success-not-correlated'


@pytest.mark.parametrize('stamp', ['2026-10-25T10:30:00.32328Z', '2026-10-25T10:30:00.123456789Z'])
def test_observation_rfc3339_fractional_widths(stamp):
    value = response(); value['body']['observed_at'] = stamp
    assert verify.aggregate(observed(value).values())['status'] == 'PASS'


def test_observation_one_nanosecond_stale_does_not_pass():
    value = response(); value['body']['observed_at'] = '2026-10-25T10:27:59.999999999Z'
    assert verify.aggregate(observed(value).values())['status'] != 'PASS'


@pytest.mark.parametrize('change', ['expired', 'future', 'wrong-key', 'wrong-purpose', 'wrong-nonce', 'malformed'])
def test_verification_identity_security(change):
    private, public = key(); headers = signature(private, int(time.time()))
    if change == 'expired': headers = signature(private, int(time.time())-31)
    if change == 'future': headers = signature(private, int(time.time())+31)
    if change == 'wrong-key': _, public = key()
    if change == 'wrong-purpose': headers['X-Observation-Signature'] = base64.b64encode(private.sign(b'fv-runtime-read-v1')).decode()
    if change == 'wrong-nonce': headers['X-Observation-Nonce'] = 'e'*32
    if change == 'malformed': headers['X-Observation-Signature'] = '!'
    assert not service.authorized(public, headers['X-Observation-Timestamp'],
        headers['X-Observation-Nonce'], headers['X-Observation-Signature'])


@pytest.mark.parametrize('code,expected', [('57014', 'database-timeout'), ('08006', 'database-unavailable')])
def test_database_failure_classification_and_redaction(code, expected):
    original = Exception('private credentials'); original.sqlstate = code
    error = OperationalError('private URL', {}, original)
    def broken(): raise error
    result = service.observe(IDENTITY, IDENTITY, engine_factory=broken)
    assert result['database'] == service.finding('FAIL', expected)
    assert result['authorization']['status'] == 'INCONCLUSIVE'
    assert 'private' not in json.dumps(result)


def test_real_postgres_timeout_is_bounded(scheduling_db):
    _, _, _, engine = scheduling_db
    with engine.connect() as conn:
        conn.execute(text("SET LOCAL statement_timeout='25ms'"))
        started = time.monotonic()
        with pytest.raises(OperationalError) as failed:
            conn.execute(text('SELECT pg_sleep(2)'))
        assert service.database_error(failed.value)['code'] == 'database-timeout'
        assert time.monotonic()-started < 1


def seed_roles(primary, monkeypatch):
    from app import models as m
    owner = primary.account('ADMIN', linked=False)
    monkeypatch.setattr(settings, 'purchasing_owner_principal_id', owner)
    monkeypatch.setattr(settings, 'purchasing_v3_phase2_enabled', True)
    with primary.Session() as db:
        for role in ('LEAD', 'STORE'):
            db.add(m.Principal(username=role.lower()+'@observation.test', role=m.PrincipalRole(role),
                active=True, store_id=primary.ids['north'] if role == 'STORE' else None, password_hash='disabled-fixture'))
        db.commit()


def test_real_readonly_owner_lead_employee_and_no_session_mutation(primary, monkeypatch, record_property):
    from app.routers import deployment_verification as route
    seed_roles(primary, monkeypatch)
    engine = primary.Session.kw['bind']; writes = []; statements = []
    def audit(conn, cursor, statement, *_):
        statements.append(statement)
        if statement.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')): writes.append(statement)
    def factory():
        fresh = create_engine(engine.url, poolclass=NullPool)
        event.listen(fresh, 'before_cursor_execute', audit)
        return fresh
    monkeypatch.setattr(service, 'fresh_engine', factory)
    monkeypatch.setattr(route, 'capture_identity', lambda: IDENTITY)
    monkeypatch.setattr(primary.app.state, 'runtime_identity', IDENTITY)
    private, public = key()
    monkeypatch.setattr(settings, 'deployment_observation_public_key', public)
    # Even a supplied browser cookie must not invoke the session resolver.
    primary.client.cookies.set(settings.session_cookie_name, 'ignored-browser-token')
    from app.security import sessions
    monkeypatch.setattr(sessions, 'load_session_from_token', lambda *a, **k: pytest.fail('session resolver called'))
    response = primary.client.get(service.PATH, headers=signature(private))
    assert response.status_code == 200, response.text
    result = response.json()
    record_property('observation_duration_ms', result['duration_ms'])
    record_property('observation_query_count', result['query_count'])
    assert result['database']['status'] == result['authorization']['status'] == 'PASS', result
    assert result['authorization']['roles']['lead']['purchasing'] is False
    assert result['authorization']['roles']['employee']['scheduling_board'] is False
    assert result['authorization']['roles']['employee']['store_scope'] is True
    assert result['query_count'] == len(statements) and len(statements) < 60
    assert not writes and not response.headers.get('set-cookie')
    assert 'username' not in response.text and 'principal_id' not in response.text
    before = len(statements)
    assert primary.client.get(service.PATH).status_code == 403
    assert primary.client.post(service.PATH, headers=signature(private)).status_code == 405
    assert primary.client.get(service.PATH+'?principal_id=1', headers=signature(private)).status_code == 403
    monkeypatch.setattr(settings, 'deployment_observation_public_key', '')
    assert primary.client.get(service.PATH, headers=signature(private)).status_code == 404
    assert len(statements) == before


def test_real_permission_override_cannot_hide_employee_privilege_regression(primary, monkeypatch):
    from app import models as m
    seed_roles(primary, monkeypatch)
    with primary.Session() as db:
        db.add(m.RolePermissionOverride(role=m.PrincipalRole.STORE, permission_key='management.access', allowed=True))
        db.commit()
    engine = primary.Session.kw['bind']
    result = service.observe(IDENTITY, IDENTITY, engine_factory=lambda: create_engine(engine.url, poolclass=NullPool))
    assert result['database']['status'] == 'PASS'
    assert result['authorization']['roles']['employee']['status'] == 'FAIL'


def test_missing_role_is_inconclusive_not_synthetic_production_success(scheduling_db):
    _, _, _, engine = scheduling_db
    result = service.observe(IDENTITY, IDENTITY, engine_factory=lambda: create_engine(engine.url, poolclass=NullPool))
    assert result['database']['status'] == 'PASS'
    assert result['authorization']['status'] == 'INCONCLUSIVE'


def test_ledger_read_failure_does_not_become_application_or_database_failure(primary, monkeypatch):
    from app.services import cron_observation_service as cron_service
    seed_roles(primary, monkeypatch)
    def failed(db): raise TimeoutError()
    monkeypatch.setattr(cron_service, 'latest_work', failed)
    engine = primary.Session.kw['bind']
    result = service.observe(IDENTITY, IDENTITY, engine_factory=lambda: create_engine(engine.url, poolclass=NullPool))
    assert result['application']['status'] == result['database']['status'] == result['authorization']['status'] == 'PASS'
    assert result['cron_work'] is None
    assert result['cron_work_observation']['status'] == 'INCONCLUSIVE'


def test_wrong_schema_and_database_write_is_rejected(scheduling_db):
    _, _, _, engine = scheduling_db
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num='wrong'"))
    result = service.observe(IDENTITY, IDENTITY, engine_factory=lambda: create_engine(engine.url, poolclass=NullPool))
    assert result['database']['code'] == 'schema-incompatible'
    assert result['authorization']['status'] == 'INCONCLUSIVE'
    with engine.connect().execution_options(postgresql_readonly=True) as conn:
        with pytest.raises(SQLAlchemyError):
            conn.execute(text("UPDATE alembic_version SET version_num='20261024_0050'"))


def test_cron_terminal_receipt_contains_no_operational_ids():
    row = receipt(IDENTITY, NONCE, 'committed', schema=EXPECTED['schema'], result={
        'generated_period_ids': [123456], 'published_period_ids': [], 'blocked_period_ids': [], 'results': []})
    assert row['work'] == 'generated' and row['counts']['generated_period_ids'] == 1
    assert '123456' not in json.dumps(row)
    assert receipt(IDENTITY, NONCE, 'committed', result={})['work'] == 'policy-disabled'


def test_existing_automation_ledger_receipt_matches_committed_read(scheduling_db):
    from app.services.v2_scheduling_prebeta_service import generation_outcome
    from app.services.cron_observation_service import latest_work
    Session, principal, _, _ = scheduling_db
    @generation_outcome('automation')
    def work(db, **kwargs):
        return {'generated_period_ids': [], 'results': []}
    with Session() as db:
        work(db, principal=principal)
        metadata = db.info['schedule_automation_outcome']
        assert metadata['status'] == 'NO_CHANGES' and metadata['generated_count'] == 0
        db.commit()
    with Session() as db:
        assert latest_work(db) == metadata


def test_cron_runner_terminal_after_commit_and_failure_before_commit(scheduling_db, monkeypatch, capsys):
    from scripts import run_schedule_automation as runner
    from app.services import runtime_identity_service
    from app import db as database
    from app.services import v2_scheduling_policy_service as automation
    from sqlalchemy.orm import Session as SASession, sessionmaker
    _, _, _, engine = scheduling_db
    monkeypatch.setattr(runtime_identity_service, 'cron_runtime_proof', lambda: IDENTITY)
    monkeypatch.setattr(runner, 'scheduling_execution_enabled', lambda: True)
    import app.schema_contract as schema
    monkeypatch.setattr(schema, 'assert_supported_schema', lambda: None)
    events = []
    class Tracked(SASession):
        def commit(self):
            super().commit(); events.append('committed')
    monkeypatch.setattr(database, 'SessionLocal', sessionmaker(bind=engine, class_=Tracked))
    def work(db, **kwargs):
        assert not events
        return {'generated_period_ids': [], 'published_period_ids': [], 'blocked_period_ids': [], 'results': []}
    monkeypatch.setattr(automation, 'run_schedule_automation', work)
    runner.main()
    assert events == ['committed']
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]['phase'] == 'committed' and rows[-1]['work'] == 'no-work-due'
    assert rows[-1]['schema'] == EXPECTED['schema']
    def failed(db, **kwargs): raise RuntimeError('fixture')
    monkeypatch.setattr(automation, 'run_schedule_automation', failed)
    with pytest.raises(RuntimeError): runner.main()
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]['phase'] == 'failed' and not any(row.get('phase') == 'committed' for row in rows)
