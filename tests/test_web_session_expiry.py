"""Fixed employee login deadlines, without PostgreSQL or external services."""
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.models import Employee, Principal as PrincipalModel, PrincipalRole, WebSession
from app.security import sessions


LOGIN_AT = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
LIFETIME = timedelta(hours=13)


class SessionDB:
    """Supply authentication query results without opening a database."""

    def __init__(self, row=None, *, role='STORE', active=True, employee_active=True):
        self.row = row
        self.user = PrincipalModel(
            id=11, username='employee@example.test', password_hash='unused',
            role=PrincipalRole(role), active=active, store_id=5,
        )
        self.employee = SimpleNamespace(id=17, principal_id=11, active=employee_active)
        self.info = {}

    def execute(self, statement):
        entity = statement.column_descriptions[0]['entity']
        if entity is WebSession:
            result = (self.row, self.user) if self.row is not None else None
            return SimpleNamespace(one_or_none=lambda: result)
        if entity is Employee:
            return SimpleNamespace(scalar_one_or_none=lambda: self.employee)
        raise AssertionError(f'Unexpected authentication query: {statement}')

    def add(self, row):
        self.row = row

    def flush(self):
        if self.row is not None and self.row.id is None:
            self.row.id = 31

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def session_row(**changes):
    fields = dict(
        id=31, session_token='test-login-token', principal_id=11,
        created_at=LOGIN_AT, last_seen_at=LOGIN_AT,
        expires_at=LOGIN_AT + LIFETIME, revoked_at=None,
    )
    fields.update(changes)
    return WebSession(**fields)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=LOGIN_AT)
    monkeypatch.setattr(sessions, '_now', lambda: clock.now)
    monkeypatch.setattr(settings, 'session_ttl_minutes', 780)
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    return clock


def test_creation_starts_one_thirteen_hour_deadline(clock):
    assert Settings.model_fields['session_ttl_minutes'].default == 780
    db = SessionDB()
    token = sessions.create_web_session(
        db, principal_id=11, ip='127.0.0.1', user_agent='session regression',
    )
    assert token == db.row.session_token
    assert db.row.principal_id == 11
    assert db.row.created_at == db.row.last_seen_at == LOGIN_AT
    assert db.row.expires_at == LOGIN_AT + LIFETIME
    assert db.row.ip == '127.0.0.1'
    assert db.row.user_agent == 'session regression'


@pytest.mark.parametrize('role', ['ADMIN', 'MANAGER', 'LEAD', 'STORE'])
def test_activity_never_extends_login_deadline_for_any_role(clock, role):
    row = session_row()
    db = SessionDB(row, role=role)
    for elapsed in (
        timedelta(hours=1), timedelta(hours=12),
        LIFETIME - timedelta(seconds=1),
    ):
        clock.now = LOGIN_AT + elapsed
        loaded = sessions.load_session_from_token(db, row.session_token)
        assert loaded is not None
        assert loaded[1].role.value == role
        assert loaded[1].active is True
        assert row.last_seen_at == clock.now
        assert row.expires_at == LOGIN_AT + LIFETIME
    clock.now = LOGIN_AT + LIFETIME
    assert sessions.load_session_from_token(db, row.session_token) is None
    clock.now += timedelta(seconds=1)
    assert sessions.load_session_from_token(db, row.session_token) is None


@pytest.mark.parametrize('reason', ['expired', 'revoked'])
def test_preexisting_expiry_or_revocation_is_not_revived(clock, reason):
    clock.now = LOGIN_AT + timedelta(hours=1)
    changes = (
        {'expires_at': clock.now - timedelta(seconds=1)}
        if reason == 'expired'
        else {'revoked_at': LOGIN_AT + timedelta(minutes=15)}
    )
    row = session_row(**changes)
    original_expiry = row.expires_at
    assert sessions.load_session_from_token(SessionDB(row), row.session_token) is None
    assert row.expires_at == original_expiry
    assert row.last_seen_at == LOGIN_AT


def test_legacy_renewed_session_is_capped_from_original_login(clock):
    row = session_row(expires_at=LOGIN_AT + timedelta(days=3))
    clock.now = LOGIN_AT + timedelta(hours=12)
    assert sessions.load_session_from_token(SessionDB(row), row.session_token)
    assert row.expires_at == LOGIN_AT + LIFETIME
    clock.now = LOGIN_AT + LIFETIME
    assert sessions.load_session_from_token(SessionDB(row), row.session_token) is None


def test_legacy_session_cannot_outlive_cap_before_first_request(clock):
    row = session_row(expires_at=LOGIN_AT + timedelta(days=3))
    clock.now = LOGIN_AT + LIFETIME
    assert sessions.load_session_from_token(SessionDB(row), row.session_token) is None


def test_shorter_existing_expiry_is_preserved(clock):
    expiry = LOGIN_AT + timedelta(hours=2)
    row = session_row(expires_at=expiry)
    db = SessionDB(row)
    clock.now = LOGIN_AT + timedelta(hours=1)
    assert sessions.load_session_from_token(db, row.session_token)
    assert row.expires_at == expiry
    clock.now = expiry
    assert sessions.load_session_from_token(db, row.session_token) is None


def test_status_probe_preserves_activity_and_fixed_deadline(clock):
    row = session_row()
    clock.now = LOGIN_AT + timedelta(hours=8)
    assert sessions.load_session_from_token(SessionDB(row), row.session_token, renew=False)
    assert row.last_seen_at == LOGIN_AT
    assert row.expires_at == LOGIN_AT + LIFETIME


@pytest.mark.parametrize('active,employee_active', [(False, True), (True, False)])
def test_inactive_account_or_employee_stays_inactive(clock, active, employee_active):
    row = session_row()
    db = SessionDB(row, active=active, employee_active=employee_active)
    clock.now = LOGIN_AT + timedelta(hours=1)
    loaded = sessions.load_session_from_token(db, row.session_token)
    assert loaded is not None and loaded[1].active is False
    assert row.last_seen_at == LOGIN_AT
    assert row.expires_at == LOGIN_AT + LIFETIME


def middleware_client(monkeypatch, db):
    from app.v2 import workspaces

    monkeypatch.setattr(sessions, 'SessionLocal', lambda: db)
    monkeypatch.setattr(sessions, 'effective_permission_flags', lambda *a, **kw: {})
    monkeypatch.setattr(workspaces, 'primary_enabled', lambda principal: False)
    monkeypatch.setattr(workspaces, 'primary_rollout_configured', lambda: False)
    app = FastAPI()

    @app.get('/protected')
    def protected(request: Request):
        return {'expires_at': request.state.session_expires_at.isoformat()}

    @app.get('/session-status')
    def status(request: Request):
        # Keep the endpoint minimal: this checks the real middleware's polling path.
        if request.state.principal is None:
            return JSONResponse({'authenticated': False}, status_code=401)
        return {
            'authenticated': True,
            'expires_at': request.state.session_expires_at.isoformat(),
        }

    sessions.install_auth_session_middleware(app)
    return TestClient(app, follow_redirects=False)


def auth_cookie(response):
    cookies = SimpleCookie()
    for header in response.headers.get_list('set-cookie'):
        cookies.load(header)
    return cookies.get(settings.session_cookie_name)


def test_cookie_shrinks_and_status_poll_does_not_renew(clock, monkeypatch):
    row = session_row()
    db = SessionDB(row)
    with middleware_client(monkeypatch, db) as client:
        client.cookies.set(settings.session_cookie_name, row.session_token)
        first = client.get('/protected')
        assert first.status_code == 200
        assert int(auth_cookie(first)['max-age']) == 13 * 3600

        clock.now = LOGIN_AT + timedelta(hours=12)
        active = client.get('/protected')
        assert active.status_code == 200
        assert int(auth_cookie(active)['max-age']) == 3600
        assert row.expires_at == LOGIN_AT + LIFETIME
        seen = row.last_seen_at

        clock.now += timedelta(minutes=30)
        probe = client.get('/session-status')
        assert probe.status_code == 200
        assert probe.json()['expires_at'] == (LOGIN_AT + LIFETIME).isoformat()
        assert auth_cookie(probe) is None
        assert row.last_seen_at == seen
        assert row.expires_at == LOGIN_AT + LIFETIME


def test_backend_rejects_expired_cookie_for_pages_status_and_autosave(clock, monkeypatch):
    row = session_row()
    db = SessionDB(row)
    with middleware_client(monkeypatch, db) as client:
        client.cookies.set(settings.session_cookie_name, row.session_token)
        clock.now = LOGIN_AT + LIFETIME
        page = client.get('/protected')
        assert page.status_code == 303
        assert page.headers['location'] == '/login'
        assert client.get('/session-status').status_code == 401
        autosave = client.post('/protected', headers={'X-Requested-With': 'autosave'})
        assert autosave.status_code == 401
        assert auth_cookie(page) is None
        assert auth_cookie(autosave) is None
