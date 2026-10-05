"""Full application / real PostgreSQL acceptance for presentation-only cutover."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from app.config import settings
from app.db import get_db
from app.models import Campaign, Employee, Principal, PrincipalRole, PrincipalPermissionOverride, RolePermissionOverride, WebSession
from app.security.passwords import hash_password
from app.security.sessions import create_web_session
from app.v2.workspaces import DESTINATIONS, local_get_target, visible_destinations
from test_v2_scheduling_foundation import scheduling_db

PASSWORD = 'phase one test password'


@pytest.fixture
def primary(scheduling_db, monkeypatch):
    from app import main
    from app.security import sessions
    from app.routers import auth
    from app.schema_contract import assert_supported_schema
    Session, manager, ids, engine = scheduling_db
    monkeypatch.setattr(settings, 'v2_enabled_features', 'v2_primary,staff_scheduling_v2')
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    monkeypatch.setattr(sessions, 'SessionLocal', Session)
    monkeypatch.setattr(main, 'assert_supported_schema', lambda: assert_supported_schema(engine))
    monkeypatch.setattr(auth, 'square_data_needs_refresh', lambda db: False)
    def database():
        with Session() as db:
            yield db
    previous = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_db] = database
    with Session() as db:
        db.add(Campaign(label='Test campaign')); db.commit()
    def account(role='STORE', linked=True, active=True, employee_active=True):
        with Session() as db:
            person = Principal(username='person@example.test', role=PrincipalRole(role), active=active,
                store_id=ids['north'] if role == 'STORE' else None, password_hash=hash_password(PASSWORD))
            db.add(person); db.flush()
            if linked:
                employee = db.get(Employee, ids['alex']); employee.principal_id = person.id; employee.active = employee_active
            db.commit()
            return person.id
    with TestClient(main.app, follow_redirects=False, client=('127.0.0.1', 50000)) as client:
        client.get('/login')
        yield SimpleNamespace(client=client, Session=Session, ids=ids, account=account, app=main.app, manager=manager)
    main.app.dependency_overrides.clear()
    main.app.dependency_overrides.update(previous)


def post(client, path, **data):
    return client.post(path, data={'csrf_token': client.cookies.get('csrf_token'), **data})


def login(primary, **extra):
    return post(primary.client, '/login', username='person@example.test', password=PASSWORD, **extra)


@pytest.mark.parametrize('role,linked,features,target', [
    ('ADMIN', False, 'v2_primary,staff_scheduling_v2', '/v2/overview'),
    ('ADMIN', True, 'v2_primary,staff_scheduling_v2', '/v2/overview'),
    ('MANAGER', True, 'v2_primary,staff_scheduling_v2', '/v2/overview'),
    ('LEAD', True, 'v2_primary,staff_scheduling_v2', '/v2/scheduling/my-schedule'),
    ('STORE', True, 'v2_primary,staff_scheduling_v2', '/v2/scheduling/my-schedule'),
    ('LEAD', False, 'v2_primary,staff_scheduling_v2', '/v2/overview'),
    ('STORE', False, 'v2_primary,staff_scheduling_v2', '/v2/store-operations'),
    ('STORE', True, 'v2_primary', '/v2/store-operations'),
    ('LEAD', True, 'v2_primary', '/v2/overview'),
])
def test_consistent_entry_and_real_landing(primary, monkeypatch, role, linked, features, target):
    monkeypatch.setattr(settings, 'v2_enabled_features', features)
    primary.account(role, linked)
    assert login(primary).headers['location'] == target
    for path in ('/', '/v2', '/v2/', '/login'):
        assert primary.client.get(path).headers['location'] == target
    response = primary.client.get(target)
    assert response.status_code == 200
    assert 'data-v2-shell' in response.text and 'Return to V1' not in response.text
    assert 'V1 remains the production system' not in response.text


@pytest.mark.parametrize('active,employee_active', [(False, True), (True, False)])
def test_inactive_account_or_employee(primary, active, employee_active):
    pid = primary.account(active=active, employee_active=employee_active)
    assert login(primary).status_code == 401
    with primary.Session() as db:
        token = create_web_session(db, pid, None, None); db.commit()
    primary.client.cookies.set(settings.session_cookie_name, token)
    for path in ('/', '/v2', '/v2/', '/v2/scheduling/my-schedule'):
        assert primary.client.get(path).status_code == 403


def test_deep_link_get_capture_query_preservation_and_retry(primary):
    primary.account('ADMIN')
    target = '/management/sessions?store_id=1&status=SUBMITTED&search=a%20b&store_id=2'
    response = primary.client.get(target)
    assert parse_qs(urlsplit(response.headers['location']).query)['return_to'] == [target]
    page = primary.client.get(response.headers['location'])
    assert 'name="return_to"' in page.text
    failed = post(primary.client, '/login', username='person@example.test', password='wrong', return_to=target)
    assert failed.status_code == 401 and 'SUBMITTED' in failed.text
    assert login(primary, return_to=target).headers['location'] == target
    assert primary.client.get(target).status_code == 200
    assert primary.client.get('/login', params={'return_to': target}).headers['location'] == target


@pytest.mark.parametrize('target', [
    'https://evil.test/x', '//evil.test/x', '/\\evil.test/x', '/%2f%2fevil.test',
    '/v2/../management/users', '/v2/scheduling/my-schedule#secret', '/logout',
    '/v2/reports/replenishment/finalize', '/management/users', '/admin/settings/integrations',
    '/v2/scheduling/week', '/v2/scheduling/my-schedule?store_id=999',
    '/v2/scheduling/my-schedule?period_id=99',
])
def test_invalid_unauthorized_or_unsafe_return_falls_back(primary, target):
    primary.account()
    assert login(primary, return_to=target).headers['location'] == '/v2/scheduling/my-schedule'


def test_no_unsafe_method_replay_and_autosave_unchanged(primary):
    for method in ('post', 'put', 'patch', 'delete'):
        response = getattr(primary.client, method)('/management/users/create')
        assert response.headers['location'] == '/login'
    assert primary.client.post('/store/sessions/1/draft', headers={'X-Requested-With': 'autosave'}).status_code == 401


def test_session_expiry_poll_logout_csrf(primary):
    pid = primary.account()
    assert login(primary).status_code == 303
    token = primary.client.cookies.get(settings.session_cookie_name)
    with primary.Session() as db:
        session = db.scalar(select(WebSession).where(WebSession.session_token == token)); before = session.expires_at
    assert primary.client.get('/session-status').status_code == 200
    with primary.Session() as db:
        assert db.scalar(select(WebSession).where(WebSession.session_token == token)).expires_at == before
    assert primary.client.post('/logout').status_code == 403
    assert primary.client.post('/v2/scheduling/api/transfers', json={}).status_code in (403, 422)
    assert post(primary.client, '/logout').headers['location'] == '/login'
    with primary.Session() as db:
        assert db.scalar(select(WebSession).where(WebSession.session_token == token)).revoked_at
        expired = create_web_session(db, pid, None, None)
        db.scalar(select(WebSession).where(WebSession.session_token == expired)).expires_at = datetime.now(timezone.utc)-timedelta(minutes=1)
        db.commit()
    primary.client.cookies.set(settings.session_cookie_name, expired)
    assert primary.client.get('/session-status').status_code == 401
    assert primary.client.get('/v2/scheduling/my-schedule').headers['location'].startswith('/login?return_to=')


def test_navigation_guards_and_shared_account_isolation(primary):
    primary.account(linked=False)
    login(primary)
    page = primary.client.get('/v2/store-operations')
    for path in ('/store/daily-count', '/store/daily-chore-sheet', '/store/opening-checklist', '/store/change-form'):
        assert f'href="{path}"' in page.text
    for path in ('/v2/scheduling/my-schedule', '/management/users', '/v2/order-payments', '/v2/reports'):
        assert f'href="{path}"' not in page.text
    assert primary.client.get('/v2/scheduling/my-schedule').status_code == 409
    assert primary.client.get('/management/users').status_code == 403
    help_page = primary.client.get('/v2/access-help')
    assert 'not linked to an individual employee' in help_page.text


def test_permission_precedence_and_capability_based_priority(primary):
    pid = primary.account('LEAD')
    with primary.Session() as db:
        db.add(RolePermissionOverride(role=PrincipalRole.LEAD, permission_key='management.admin', allowed=False))
        db.add(PrincipalPermissionOverride(principal_id=pid, permission_key='management.admin', allowed=True))
        db.commit()
    assert login(primary).headers['location'] == '/v2/overview'
    with primary.Session() as db:
        db.scalar(select(PrincipalPermissionOverride).where(PrincipalPermissionOverride.principal_id == pid)).allowed = False
        db.commit()
    assert primary.client.get('/v2').headers['location'] == '/v2/scheduling/my-schedule'


def test_empty_access_help_and_per_principal_exposure_rollback(primary, monkeypatch):
    pid = primary.account(linked=False)
    with primary.Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=pid, permission_key='store.access', allowed=False)); db.commit()
    assert login(primary).headers['location'] == '/v2/access-help'
    assert primary.client.get('/v2/access-help').status_code == 200
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    assert primary.client.get('/').headers['location'] == '/store/home'
    assert primary.client.get('/v2').status_code == 403
    assert primary.client.get('/login').status_code == 200
    monkeypatch.setattr(settings, 'v2_principal_features', f'{pid}:v2_primary')
    assert primary.client.get('/').headers['location'] == '/v2/access-help'


def test_legacy_bridges_retain_shell_forms_and_rollback(primary, monkeypatch):
    primary.account('ADMIN')
    login(primary)
    overview = primary.client.get('/v2/overview')
    for path in ('/management/ordering-tool', '/management/groups', '/management/access-controls',
                 '/management/store-count', '/management/reports/sales-transactions', '/admin/settings/integrations'):
        assert f'href="{path}"' in overview.text
    for path in ('/management/groups', '/management/users', '/management/access-controls', '/management/daily-chore-tasks'):
        page = primary.client.get(path)
        assert page.status_code == 200, page.text
        assert 'data-v2-shell' in page.text
        assert 'csrf_token' in page.text
        assert 'AUTO_SAVE_INTERVAL_MS = 30000' in page.text
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    assert primary.client.get('/').headers['location'] == '/management/home'
    assert primary.client.get('/v2').headers['location'] == '/v2/overview'
    assert primary.client.get('/management/users').status_code == 200
    assert 'data-v2-shell' not in primary.client.get('/management/users').text
    post(primary.client, '/logout')
    assert login(primary).headers['location'] == '/'


def test_registry_only_contains_existing_get_workflows(primary):
    # FastAPI 0.129 uses included-router wrappers; OpenAPI resolves the full tree.
    paths = primary.app.openapi()['paths']
    assert len({d.path for d in DESTINATIONS}) == len(DESTINATIONS)
    for destination in DESTINATIONS:
        assert 'get' in paths[destination.path], destination.path
    assert {d.group for d in DESTINATIONS} >= {'Financials', 'Reports', 'Scheduling', 'Administration / Settings'}


def test_device_sessions_are_independent(primary):
    from app.security.touchscreen_devices import create_touchscreen_device, revoke_touchscreen_device
    from app.security.display_sessions import DISPLAY_SESSION_COOKIE
    from app.services.digital_signage_service import create_display
    from app.models import DigitalSignageDisplaySession
    with primary.Session() as db:
        device, raw = create_touchscreen_device(db, store_id=primary.ids['north'], name='Test kiosk', orientation='AUTO', principal=primary.manager, ip=None)
        device_id = device.id
        display, password = create_display(db, name='Test display', username='test-display', password=None, is_enabled=True, principal=primary.manager, ip=None)
        slug = display.slug
        db.commit()
    client = primary.client
    assert client.get('/touchscreen/api/session').status_code == 401
    assert client.get('/touchscreen/' + raw).status_code == 200
    assert client.get('/touchscreen/api/session').status_code == 200
    assert client.get('/touchscreen/service-worker.js').status_code == 200
    assert client.get('/touchscreen/media/not-authorized').status_code in (404, 503)
    assert client.get('/display/' + slug).status_code == 200
    response = post(client, '/display/' + slug + '/login', username='test-display', password=password)
    assert response.status_code == 303
    assert client.cookies.get(DISPLAY_SESSION_COOKIE)
    assert client.get('/display/api/playlist').status_code == 200
    assert not client.cookies.get(settings.session_cookie_name)
    assert post(client, '/display/logout').status_code == 303
    assert client.get('/display/api/playlist').status_code == 401
    with primary.Session() as db:
        assert db.scalar(select(DigitalSignageDisplaySession)).revoked_at
        revoke_touchscreen_device(db, device_id=device_id, principal=primary.manager, ip=None); db.commit()
    assert client.get('/touchscreen/api/session').status_code == 401


def test_record_deep_links_check_store_ownership(primary):
    from app.models import CountSession
    pid = primary.account()
    with primary.Session() as db:
        campaign = db.scalar(select(Campaign))
        rows = [CountSession(store_id=store, campaign_id=campaign.id, employee_name='Test', created_by_principal_id=pid)
                for store in (primary.ids['north'], primary.ids['south'])]
        db.add_all(rows); db.flush(); own, other = [row.id for row in rows]; db.commit()
    target = f'/store/sessions/{own}?resume=1'
    assert login(primary, return_to=target).headers['location'] == target
    assert primary.client.get('/v2', params={'return_to': f'/store/sessions/{other}'}).headers['location'] == '/v2/scheduling/my-schedule'
    assert primary.client.get(f'/store/sessions/{other}').status_code == 403


def test_primary_own_schedule_never_uses_requested_employee(primary):
    from datetime import time
    from zoneinfo import ZoneInfo
    from app.models import SchedulePeriod, ScheduleShift
    pid = primary.account()
    today = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    start = today - timedelta(days=today.weekday())
    with primary.Session() as db:
        period = SchedulePeriod(week_start_date=start, week_end_date=start+timedelta(days=6), status='PUBLISHED',
            revision_number=1, created_by_principal_id=primary.ids['manager'], updated_by_principal_id=primary.ids['manager'])
        db.add(period); db.flush()
        for employee, hour in ((primary.ids['alex'], 9), (primary.ids['blair'], 20)):
            db.add(ScheduleShift(schedule_period_id=period.id, employee_id=employee, store_id=primary.ids['north'],
                shift_date=today, start_time=time(hour), end_time=time(hour+1),
                created_by_principal_id=primary.ids['manager'], updated_by_principal_id=primary.ids['manager']))
        db.commit()
    login(primary)
    response = primary.client.get('/v2/scheduling/my-schedule', params={'employee_id': primary.ids['blair']})
    assert response.status_code == 200 and '9:00 AM' in response.text and '8:00 PM' not in response.text
    api = primary.client.get('/v2/scheduling/api/own-schedule', params={'employee_id': primary.ids['blair']})
    assert api.json()['employee_id'] == primary.ids['alex']
    assert len(api.json()['assignments']) == 1


def test_primary_csrf_and_exposure_do_not_grant_domain_permission(primary, monkeypatch):
    pid = primary.account(linked=False)
    login(primary)
    monkeypatch.setattr(settings, 'v2_enabled_features', 'v2_primary,daily_store_logs_v2,order_payments_v2')
    assert primary.client.post('/v2/current-store', data={'store_id': primary.ids['north']}).status_code == 403
    assert post(primary.client, '/v2/current-store', store_id=str(primary.ids['north'])).status_code == 303
    assert primary.client.get('/v2/order-payments').status_code == 403
    # Even broad capability overrides do not replace the financial literal-role guard.
    with primary.Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=pid, permission_key='management.admin', allowed=True)); db.commit()
    assert primary.client.get('/v2/order-payments').status_code == 404
    assert 'href="/v2/order-payments"' not in primary.client.get('/v2/overview').text


def test_primary_links_agree_with_real_route_guard_dependencies(primary, monkeypatch):
    import inspect
    from app.auth import Principal as Actor
    from app.dependencies import get_templates
    from app.services.access_control_service import effective_permission_flags, permission_defs
    features = {'v2_primary'} | {d.feature for d in DESTINATIONS if d.feature}
    monkeypatch.setattr(settings, 'v2_enabled_features', ','.join(features))
    routes = {route.path: route for route in primary.app.routes if 'GET' in getattr(route, 'methods', ())}
    with primary.Session() as db:
        for role in PrincipalRole:
            person = Principal(username='guard-'+role.value, password_hash='unused', role=role, active=True, store_id=primary.ids['north'] if role == PrincipalRole.STORE else None)
            db.add(person); db.flush()
            actor = Actor(person.id, person.username, role, person.store_id, True)
            request = Request({'type': 'http', 'path': '/v2/overview', 'query_string': b'', 'headers': [], 'app': primary.app})
            request.state.principal = actor
            request.state.employee_id = primary.ids['alex']
            for all_allowed in (False, True):
                if all_allowed:
                    db.add_all(PrincipalPermissionOverride(principal_id=person.id, permission_key=d.key, allowed=True) for d in permission_defs())
                    db.flush()
                request.state.permission_flags = effective_permission_flags(db, principal=actor)
                def resolve(dependency):
                    if dependency.call == get_db:
                        return db
                    if dependency.call == get_templates:
                        return primary.app.state.templates
                    values = {child.name: resolve(child) for child in dependency.dependencies}
                    if 'request' in inspect.signature(dependency.call).parameters:
                        values['request'] = request
                    return dependency.call(**values)
                for destination in visible_destinations(request):
                    for dependency in routes[destination.path].dependant.dependencies:
                        resolve(dependency)


def test_shell_deep_link_precedes_employee_default(primary):
    primary.account()
    assert login(primary, return_to='/v2/store-operations').headers['location'] == '/v2/store-operations'
    assert primary.client.get('/v2', params={'return_to': '/v2/access-help'}).headers['location'] == '/v2/access-help'
    assert primary.client.get('/v2', params={'return_to': '/v2/overview'}).headers['location'] == '/v2/scheduling/my-schedule'


def test_saved_view_restoration_preserves_private_ownership(primary):
    from app.models import ReportingSavedView
    pid = primary.account('ADMIN')
    with primary.Session() as db:
        own = ReportingSavedView(principal_id=pid, name='Mine', report_type='sales_analysis', configuration={})
        other = ReportingSavedView(principal_id=primary.ids['manager'], name='Private', report_type='sales_analysis', configuration={})
        db.add_all([own, other]); db.flush(); own_id, other_id = own.id, other.id; db.commit()
    target = f'/v2/reports?saved_view_id={own_id}&page_size=25'
    assert login(primary, return_to=target).headers['location'] == target
    assert primary.client.get('/v2', params={'return_to': f'/v2/reports?saved_view_id={other_id}'}).headers['location'] == '/v2/overview'
    assert primary.client.get('/v2/reports', params={'saved_view_id': other_id}).status_code == 404


def test_explicit_schedule_period_restore_checks_draft_access(primary):
    from app.models import SchedulePeriod
    pid = primary.account('LEAD')
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=today.weekday())
    with primary.Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=pid, permission_key='scheduling.view_all', allowed=True))
        period = SchedulePeriod(week_start_date=start, week_end_date=start+timedelta(days=6), status='DRAFT',
            revision_number=1, created_by_principal_id=primary.ids['manager'], updated_by_principal_id=primary.ids['manager'])
        db.add(period); db.flush(); period_id = period.id; db.commit()
    target = f'/v2/scheduling/week?period_id={period_id}'
    assert login(primary, return_to=target).headers['location'] == '/v2/scheduling/my-schedule'
    with primary.Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=pid, permission_key='scheduling.generate', allowed=True)); db.commit()
    assert primary.client.get('/v2', params={'return_to': target}).headers['location'] == target


def test_rollout_off_does_not_capture_anonymous_get(primary, monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    assert primary.client.get('/').headers['location'] == '/login'
    assert primary.client.get('/management/sessions').headers['location'] == '/login'


def test_existing_v2_financial_and_schedule_pages_keep_their_forms(primary, monkeypatch):
    from html.parser import HTMLParser
    class Forms(HTMLParser):
        def __init__(self):
            super().__init__(); self.forms = []
        def handle_starttag(self, tag, attrs):
            if tag == 'form':
                values = dict(attrs)
                self.forms.append((values.get('method', 'get'), values.get('action', '')))
    primary.account('ADMIN')
    login(primary)
    for path in ('/v2/order-payments', '/v2/payment-methods', '/v2/funding-accounts', '/v2/consignment', '/v2/scheduling/week'):
        monkeypatch.setattr(settings, 'v2_enabled_features', 'staff_scheduling_v2,order_payments_v2')
        old = primary.client.get(path)
        monkeypatch.setattr(settings, 'v2_enabled_features', 'v2_primary,staff_scheduling_v2,order_payments_v2')
        new = primary.client.get(path)
        assert old.status_code == new.status_code == 200, path
        before, after = Forms(), Forms(); before.feed(old.text); after.feed(new.text)
        assert before.forms == after.forms, path
        assert 'Return to V1' not in new.text
