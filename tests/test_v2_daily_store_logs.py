from __future__ import annotations

import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.auth import Principal, Role
from app.config import settings
from app.models import (
    AuditLog,
    DailyStoreLog,
    DailyStoreLogAction,
    Principal as PrincipalModel,
    PrincipalPermissionOverride,
    PrincipalRole,
    Store,
    WebSession,
)
from app.schema_contract import upgrade_database
from app.services.v2_daily_store_log_service import (
    FEATURE_KEY,
    DailyLogConflict,
    DailyLogInput,
    issue_action_token,
    issue_submission_token,
    perform_management_action,
    portal_today,
    submit_daily_log,
)
from app.services.v2_store_operations_completion_service import (
    CompletionResolution,
    CompletionState,
    CompletionStatus,
    completion_statuses,
)


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


@pytest.fixture
def daily_site(monkeypatch):
    if not ADMIN_URL:
        pytest.skip('set TEST_POSTGRES_ADMIN_URL for V2 Daily Store Log integration')
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_daily_logs_{uuid.uuid4().hex[:10]}'
    database_url = f'{ADMIN_URL.rsplit("/", 1)[0]}/{database_name}'
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    upgrade_database(database_url)
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        north = Store(name='North', square_location_id='NORTH', active=True)
        south = Store(name='South', square_location_id='SOUTH', active=True)
        closed = Store(name='Closed', square_location_id='CLOSED', active=False)
        db.add_all([north, south, closed])
        db.flush()
        people = {
            'store': PrincipalModel(username='alice', password_hash='unused', role=PrincipalRole.STORE, store_id=north.id, active=True),
            'store2': PrincipalModel(username='bob', password_hash='unused', role=PrincipalRole.STORE, store_id=south.id, active=True),
            'lead': PrincipalModel(username='lead', password_hash='unused', role=PrincipalRole.LEAD, store_id=None, active=True),
            'manager': PrincipalModel(username='manager', password_hash='unused', role=PrincipalRole.MANAGER, store_id=None, active=True),
            'admin': PrincipalModel(username='admin', password_hash='unused', role=PrincipalRole.ADMIN, store_id=None, active=True),
            'inactive': PrincipalModel(username='inactive', password_hash='unused', role=PrincipalRole.STORE, store_id=north.id, active=False),
        }
        db.add_all(people.values())
        db.flush()
        tokens = {}
        for key, person in people.items():
            tokens[key] = f'daily-log-session-{key}'
            db.add(
                WebSession(
                    session_token=tokens[key],
                    principal_id=person.id,
                    ip=None,
                    user_agent='pytest',
                    expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=2),
                )
            )
        db.commit()
        ids = {key: int(person.id) for key, person in people.items()}
        store_ids = {'north': int(north.id), 'south': int(south.id), 'closed': int(closed.id)}

    import app.db as db_module
    import app.main as main_module
    import app.security.sessions as session_module

    monkeypatch.setattr(db_module, 'SessionLocal', Session)
    monkeypatch.setattr(session_module, 'SessionLocal', Session)
    monkeypatch.setattr(main_module, 'assert_supported_schema', lambda: None)
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    monkeypatch.setattr(settings, 'v2_enabled_features', FEATURE_KEY)
    monkeypatch.setattr(settings, 'v2_principal_features', '')

    with TestClient(
        main_module.app,
        base_url='https://testserver',
        follow_redirects=False,
        client=('127.0.0.1', 50000),
    ) as base_client:
        def client(key: str | None = None) -> TestClient:
            base_client.cookies.clear()
            if key:
                base_client.cookies.set(settings.session_cookie_name, tokens[key])
            return base_client

        yield SimpleNamespace(client=client, Session=Session, ids=ids, store_ids=store_ids, engine=engine)

    engine.dispose()
    with admin_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    admin_engine.dispose()


def _hidden(html: str, name: str, occurrence: int = 0) -> str:
    matches = re.findall(rf'name="{re.escape(name)}" value="([^"]+)"', html)
    assert len(matches) > occurrence, name
    return matches[occurrence]


def _action_token(html: str, slug: str) -> str:
    match = re.search(
        rf'action="[^"]+/{re.escape(slug)}".*?name="action_token" value="([^"]+)"',
        html,
        flags=re.DOTALL,
    )
    assert match, slug
    return match.group(1)


def _select_store(client: TestClient, store_id, return_to: str = '/v2/store-operations/daily-logs'):
    page = client.get(f'/v2/current-store?return_to={return_to}')
    assert page.status_code == 200
    return client.post(
        '/v2/current-store',
        data={
            'csrf_token': _hidden(page.text, 'csrf_token'),
            'store_id': str(store_id),
            'return_to': return_to,
        },
    )


def _valid_form(html: str, **overrides) -> dict[str, str]:
    data = {
        'csrf_token': _hidden(html, 'csrf_token'),
        'submission_token': _hidden(html, 'submission_token'),
        'general_summary': 'Steady customer traffic and normal store operations.',
        'customer_incidents': '',
        'inventory_concerns': '',
        'facility_equipment_issues': '',
        'staffing_coverage_notes': '',
        'follow_up_items': '',
    }
    data.update(overrides)
    return data


def _submit(daily_site, *, actor='store', store='north', **overrides) -> int:
    client = daily_site.client(actor)
    selection = _select_store(client, daily_site.store_ids[store])
    assert selection.status_code == 303
    page = client.get('/v2/store-operations/daily-logs')
    response = client.post(
        '/v2/store-operations/daily-logs',
        data=_valid_form(page.text, **overrides),
    )
    assert response.status_code == 303, response.text
    return int(re.search(r'submitted=(\d+)', response.headers['location']).group(1))


def test_exposure_authentication_navigation_and_no_shift_contract(daily_site, monkeypatch):
    assert daily_site.client().get('/v2/store-operations/daily-logs').status_code == 303
    store = daily_site.client('store')
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    assert store.get('/v2/store-operations/daily-logs').status_code == 404
    monkeypatch.setattr(settings, 'v2_enabled_features', FEATURE_KEY)
    redirect = store.get('/v2/store-operations/daily-logs')
    assert redirect.status_code == 303
    assert redirect.headers['location'].startswith('/v2/current-store?return_to=')
    current_store = store.get(redirect.headers['location'])
    assert current_store.status_code == 200
    assert 'Which store are you working at today?' in current_store.text
    assert 'Working context only' in current_store.text
    assert 'does not change your permissions, store assignments, or authorization' in current_store.text
    assert 'V2 Owner Preview' in current_store.text
    assert 'North' in current_store.text and 'South' in current_store.text and 'Closed' not in current_store.text
    assert _select_store(store, daily_site.store_ids['south']).status_code == 303
    page = store.get('/v2/store-operations/daily-logs')
    assert page.status_code == 200
    assert '<select' not in page.text and 'type="date"' not in page.text
    assert 'Pacific business date' not in page.text
    assert 'South' in page.text and 'Logged by alice' in page.text
    assert 'Submission locked' in page.text and 'Once submitted, this report cannot be edited.' in page.text
    for label in (
        'Daily Chore List',
        'Inventory Counts',
        'Non-Sellable Counts',
        'Change Box Count',
        'Customer Requests',
        'Item Errors',
        'Customer Rewards Errors',
        'Repair Requests',
    ):
        assert label in page.text
    assert '>Daily Store Log</span>' in page.text
    assert 'href="/v2/store-operations/daily-logs"' in page.text
    assert 'Coming Soon' in page.text
    assert 'aria-expanded="true"' in page.text
    assert 'shift_type' not in page.text and 'scheduled_shift_id' not in page.text
    assert store.get('/v2/store-operations/daily-logs/history').status_code == 403
    assert daily_site.client('inactive').get('/v2/store-operations/daily-logs').status_code == 403


def test_v2_access_denied_page_preserves_status_and_safe_destinations(daily_site):
    store_denied = daily_site.client('store').get('/v2/store-operations/daily-logs/history')
    assert store_denied.status_code == 403
    assert 'This workspace is not available to your account' in store_denied.text
    assert 'Open Store Operations' in store_denied.text
    assert 'href="/store/home"' in store_denied.text
    assert '/management/home' not in store_denied.text

    management_denied = daily_site.client('admin').get('/v2/store-operations/daily-logs')
    assert management_denied.status_code == 403
    assert 'This workspace is not available to your account' in management_denied.text
    assert 'Open V2 Overview' in management_denied.text
    assert 'href="/management/home"' in management_denied.text
    assert 'Open Store Operations' not in management_denied.text

    v1_denied = daily_site.client('store').get('/management/ordering-tool')
    assert v1_denied.status_code == 403
    assert 'This workspace is not available to your account' not in v1_denied.text

    invalid_csrf = daily_site.client('store').post(
        '/v2/current-store',
        data={'csrf_token': 'invalid', 'store_id': daily_site.store_ids['north']},
    )
    assert invalid_csrf.status_code == 403
    assert 'This workspace is not available to your account' not in invalid_csrf.text


def test_ordering_bridge_disclosure_and_preview_branding_render(daily_site, monkeypatch):
    monkeypatch.setattr(
        settings,
        'v2_enabled_features',
        'daily_store_logs_v2,ordering_v1_links_v2',
    )
    page = daily_site.client('admin').get('/v2/overview')
    assert page.status_code == 200
    assert page.text.count('Existing V1') == 4
    assert page.text.count('Opens current production tool') == 4
    assert 'V2 Owner Preview' in page.text
    assert 'Owner Preview' in page.text
    assert 'Component preview' not in page.text
    assert 'Interaction primitives' not in page.text
    assert 'V1 remains canonical' in page.text


def test_store_operations_dashboard_current_store_date_and_required_statuses(daily_site):
    client = daily_site.client('store')
    redirect = client.get('/v2/store-operations')
    assert redirect.status_code == 303
    assert redirect.headers['location'].startswith('/v2/current-store')
    assert _select_store(client, daily_site.store_ids['north'], '/v2/store-operations').status_code == 303
    dashboard = client.get('/v2/store-operations')
    assert dashboard.status_code == 200
    expected_date = portal_today().strftime('%A, %B %d, %Y').replace(' 0', ' ')
    assert 'Location:</strong> North' in dashboard.text
    assert expected_date in dashboard.text
    assert 'type="date"' not in dashboard.text
    assert 'Pacific business date' not in dashboard.text
    for label in (
        'Daily Chore List',
        'Inventory Count',
        'Non-Sellable Stock Take',
        'Change Box Count — AM',
        'Change Box Count — PM',
    ):
        assert label in dashboard.text
    assert dashboard.text.count('Coming Later') >= 5
    assert 'aria-label="Daily Chore List: Coming Later"' in dashboard.text
    assert 'Open Daily Store Log' in dashboard.text
    with daily_site.Session() as db:
        assert db.scalar(select(func.count(DailyStoreLog.id))) == 0
    assert daily_site.client('admin').get('/v2/store-operations').status_code == 403
    assert daily_site.client('admin').get('/v2/store-operations/daily-logs/history').status_code == 200


def test_store_operations_dashboard_inactive_context_subset_and_location_change(daily_site):
    client = daily_site.client('store')
    assert _select_store(client, daily_site.store_ids['north'], '/v2/store-operations').status_code == 303
    with daily_site.Session() as db:
        principal_id = daily_site.ids['store']
        permissions = (
            'nav.store_operations.all',
            'nav.store_operations.inventory_counts',
            'nav.store_operations.non_sellable_counts',
            'nav.store_operations.change_box_count',
        )
        for permission_key in permissions:
            db.add(
                PrincipalPermissionOverride(
                    principal_id=principal_id,
                    permission_key=permission_key,
                    allowed=False,
                    updated_by_principal_id=daily_site.ids['admin'],
                )
            )
        db.commit()
    subset = client.get('/v2/store-operations')
    assert 'Daily Chore List' in subset.text
    assert 'Inventory Count' not in subset.text
    assert 'Non-Sellable Stock Take' not in subset.text
    assert 'Change Box Count — AM' not in subset.text
    assert _select_store(client, daily_site.store_ids['south'], '/v2/store-operations').status_code == 303
    changed = client.get('/v2/store-operations')
    assert 'Location:</strong> South' in changed.text
    with daily_site.Session() as db:
        db.get(Store, daily_site.store_ids['south']).active = False
        db.commit()
    invalid = client.get('/v2/store-operations')
    assert invalid.status_code == 303
    assert invalid.headers['location'].startswith('/v2/current-store')


def test_completion_status_contract_supports_complete_incomplete_and_permissions():
    calls = []

    def completed(_db, store_id, business_date):
        calls.append(('complete', store_id, business_date))
        return CompletionResolution(
            CompletionState.COMPLETE,
            href='/implemented/complete',
            action_label='View Completed',
        )

    def incomplete(_db, store_id, business_date):
        calls.append(('incomplete', store_id, business_date))
        return CompletionResolution(
            CompletionState.INCOMPLETE,
            href='/implemented/start',
            action_label='Start',
        )

    today = portal_today()
    statuses = completion_statuses(
        object(),
        store_id=42,
        business_date=today,
        permission_flags={
            'nav.store_operations.all': False,
            'nav.store_operations.daily_chores': True,
            'nav.store_operations.inventory_counts': True,
        },
        sources={
            'daily_chore_list': completed,
            'inventory_count': incomplete,
        },
    )
    assert [(row.key, row.state_label, row.state_icon) for row in statuses] == [
        ('daily_chore_list', 'Complete', '✓'),
        ('inventory_count', 'Not Complete', '✕'),
    ]
    assert calls == [('complete', 42, today), ('incomplete', 42, today)]


def test_dashboard_renders_complete_and_incomplete_with_text_icons_and_actions(daily_site, monkeypatch):
    client = daily_site.client('store')
    assert _select_store(client, daily_site.store_ids['north'], '/v2/store-operations').status_code == 303
    monkeypatch.setattr(
        'app.routers.v2.completion_statuses',
        lambda *_args, **_kwargs: [
            CompletionStatus(
                key='daily_chore_list',
                label='Daily Chore List',
                state=CompletionState.COMPLETE,
                href='/completed',
                action_label='View Completed',
            ),
            CompletionStatus(
                key='inventory_count',
                label='Inventory Count',
                state=CompletionState.INCOMPLETE,
                href='/start',
                action_label='Start',
            ),
        ],
    )
    dashboard = client.get('/v2/store-operations')
    assert 'v2-completion-card--complete' in dashboard.text
    assert 'v2-completion-card--incomplete' in dashboard.text
    assert 'Daily Chore List: Complete' in dashboard.text
    assert 'Inventory Count: Not Complete' in dashboard.text
    assert '✓' in dashboard.text and '✕' in dashboard.text
    assert 'View Completed' in dashboard.text and '>Start</a>' in dashboard.text


def test_cross_store_submission_validates_server_store_and_writes_safe_atomic_audit(daily_site):
    record_id = _submit(daily_site, store='south')
    with daily_site.Session() as db:
        row = db.get(DailyStoreLog, record_id)
        assert row.store_id == daily_site.store_ids['south']
        assert row.submitted_by_principal_id == daily_site.ids['store']
        assert row.store_selection_source == 'CURRENT_STORE'
        assert row.store_confirmed_at is not None
        audit = db.execute(select(AuditLog).where(AuditLog.action == 'V2:STORE_OPERATIONS:DAILY_LOG_SUBMITTED')).scalar_one()
        assert audit.actor_principal_id == daily_site.ids['store']
        assert audit.meta['store_ids'] == [daily_site.store_ids['south']]
        assert 'daily-log-session' not in str(audit.meta)
        assert 'submission_token' not in str(audit.meta)
    detail = daily_site.client('store').get(f'/v2/store-operations/daily-logs/{record_id}')
    assert detail.status_code == 200 and 'South' in detail.text and 'alice' in detail.text
    assert 'Change store' in detail.text
    assert daily_site.client('store2').get(f'/v2/store-operations/daily-logs/{record_id}').status_code == 404


def test_store_and_date_are_server_derived_and_store_change_does_not_alter_prior_record(daily_site):
    client = daily_site.client('store')
    assert _select_store(client, daily_site.store_ids['north']).status_code == 303
    page = client.get('/v2/store-operations/daily-logs')
    response = client.post(
        '/v2/store-operations/daily-logs',
        data=_valid_form(
            page.text,
            store_id=str(daily_site.store_ids['south']),
            log_date='1999-01-01',
        ),
    )
    record_id = int(re.search(r'submitted=(\d+)', response.headers['location']).group(1))
    assert _select_store(client, daily_site.store_ids['south']).status_code == 303
    with daily_site.Session() as db:
        row = db.get(DailyStoreLog, record_id)
        assert row.store_id == daily_site.store_ids['north']
        assert row.log_date == portal_today()


@pytest.mark.parametrize('store_value', ['', 'bad', '-1', '999999'])
def test_malformed_unknown_or_forged_current_store_is_rejected(daily_site, store_value):
    client = daily_site.client('store')
    response = _select_store(client, store_value)
    assert response.status_code == 422
    with daily_site.Session() as db:
        assert db.scalar(select(func.count(DailyStoreLog.id))) == 0
        session = db.execute(
            select(WebSession).where(WebSession.principal_id == daily_site.ids['store'])
        ).scalar_one()
        assert session.current_store_id is None


def test_inactive_selection_safe_return_and_change_store_persistence(daily_site):
    client = daily_site.client('store')
    inactive = _select_store(client, daily_site.store_ids['closed'])
    assert inactive.status_code == 422
    external = _select_store(client, daily_site.store_ids['north'], 'https://evil.example/phish')
    assert external.status_code == 303
    assert external.headers['location'] == '/v2/store-operations/daily-logs'
    with daily_site.Session() as db:
        session = db.execute(
            select(WebSession).where(WebSession.principal_id == daily_site.ids['store'])
        ).scalar_one()
        assert session.current_store_id == daily_site.store_ids['north']
        assert session.current_store_checked_at is not None
    page = client.get('/v2/store-operations/daily-logs')
    assert 'Current Store' in page.text and 'North' in page.text and 'Change store' in page.text
    assert _select_store(client, daily_site.store_ids['south']).status_code == 303
    changed = client.get('/v2/store-operations/daily-logs')
    assert 'South' in changed.text


def test_inactive_current_store_context_is_invalidated(daily_site):
    client = daily_site.client('store')
    assert _select_store(client, daily_site.store_ids['north']).status_code == 303
    with daily_site.Session() as db:
        db.get(Store, daily_site.store_ids['north']).active = False
        db.commit()
    response = client.get('/v2/store-operations/daily-logs')
    assert response.status_code == 303
    assert response.headers['location'].startswith('/v2/current-store')


def test_unchecked_current_store_id_is_not_trusted(daily_site):
    with daily_site.Session() as db:
        session = db.execute(
            select(WebSession).where(WebSession.principal_id == daily_site.ids['store'])
        ).scalar_one()
        session.current_store_id = daily_site.store_ids['south']
        session.current_store_checked_at = None
        db.commit()
    response = daily_site.client('store').get('/v2/store-operations/daily-logs')
    assert response.status_code == 303
    assert response.headers['location'].startswith('/v2/current-store')


def test_no_issues_follow_up_confirmation_and_substantive_validation(daily_site):
    client = daily_site.client('store')
    assert _select_store(client, daily_site.store_ids['north']).status_code == 303
    page = client.get('/v2/store-operations/daily-logs')
    base = _valid_form(page.text, general_summary='')
    assert client.post('/v2/store-operations/daily-logs', data=base).status_code == 422
    base['no_issues_reported'] = '1'
    base['follow_up_required'] = '1'
    base['follow_up_items'] = ''
    response = client.post('/v2/store-operations/daily-logs', data=base)
    assert response.status_code == 422
    assert 'no-issues log cannot require follow-up' in response.text


def test_same_token_duplicate_and_store_date_conflict_privacy(daily_site):
    client = daily_site.client('store')
    assert _select_store(client, daily_site.store_ids['north']).status_code == 303
    page = client.get('/v2/store-operations/daily-logs')
    data = _valid_form(page.text)
    first = client.post('/v2/store-operations/daily-logs', data=data)
    second = client.post('/v2/store-operations/daily-logs', data=data)
    assert first.status_code == second.status_code == 303
    assert 'duplicate=1' in second.headers['location']
    new_page = client.get('/v2/store-operations/daily-logs')
    conflict = client.post(
        '/v2/store-operations/daily-logs',
        data=_valid_form(new_page.text),
    )
    assert conflict.status_code == 409
    assert 'View your existing record' in conflict.text

    _submit(daily_site, actor='store2', store='south')
    client = daily_site.client('store')
    assert _select_store(client, daily_site.store_ids['south']).status_code == 303
    other_page = client.get('/v2/store-operations/daily-logs')
    hidden = client.post(
        '/v2/store-operations/daily-logs',
        data=_valid_form(other_page.text),
    )
    assert hidden.status_code == 409
    assert 'already exists for the selected store and date' in hidden.text
    assert 'bob' not in hidden.text and 'View your existing record' not in hidden.text


def test_concurrent_submission_protection_for_same_and_different_tokens(daily_site):
    principal = Principal(
        id=daily_site.ids['store'],
        username='alice',
        role=Role.STORE,
        store_id=daily_site.store_ids['north'],
        active=True,
    )
    values = DailyLogInput(
        general_summary='Concurrent operational submission content.',
    )
    token = issue_submission_token(principal_id=principal.id)

    def submit_once(candidate_token):
        with daily_site.Session() as db:
            try:
                outcome = submit_daily_log(
                    db,
                    principal=principal,
                    submission_token=candidate_token,
                    current_store_id=daily_site.store_ids['north'],
                    values=values,
                    ip=None,
                )
                db.commit()
                return outcome.duplicate
            except DailyLogConflict:
                db.rollback()
                return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as executor:
        same = list(executor.map(submit_once, [token, token]))
    assert sorted(same) == [False, True]

    values2 = DailyLogInput(
        general_summary='Different-token concurrency test content.',
    )
    def submit_south(candidate_token):
        with daily_site.Session() as db:
            try:
                outcome = submit_daily_log(
                    db,
                    principal=principal,
                    submission_token=candidate_token,
                    current_store_id=daily_site.store_ids['south'],
                    values=values2,
                    ip=None,
                )
                db.commit()
                return outcome.duplicate
            except DailyLogConflict:
                db.rollback()
                return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        different = list(executor.map(submit_south, [issue_submission_token(principal_id=principal.id), issue_submission_token(principal_id=principal.id)]))
    assert set(different) == {False, 'conflict'}


def test_submission_time_determines_business_date_across_pacific_midnight(daily_site):
    principal = Principal(
        id=daily_site.ids['store'],
        username='alice',
        role=Role.STORE,
        store_id=daily_site.store_ids['north'],
        active=True,
    )
    before_midnight = datetime(2026, 7, 17, 6, 59, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 7, 17, 7, 1, tzinfo=timezone.utc)
    with daily_site.Session() as db:
        first = submit_daily_log(
            db,
            principal=principal,
            submission_token=issue_submission_token(principal_id=principal.id),
            current_store_id=daily_site.store_ids['north'],
            values=DailyLogInput(general_summary='Before midnight operational report.'),
            ip=None,
            now=before_midnight,
        )
        second = submit_daily_log(
            db,
            principal=principal,
            submission_token=issue_submission_token(principal_id=principal.id),
            current_store_id=daily_site.store_ids['south'],
            values=DailyLogInput(general_summary='After midnight operational report.'),
            ip=None,
            now=after_midnight,
        )
        db.commit()
        assert db.get(DailyStoreLog, first.record_id).log_date.isoformat() == '2026-07-16'
        assert db.get(DailyStoreLog, second.record_id).log_date.isoformat() == '2026-07-17'


def test_persistence_failure_preserves_submission_identity(daily_site, monkeypatch):
    client = daily_site.client('store')
    assert _select_store(client, daily_site.store_ids['north']).status_code == 303
    page = client.get('/v2/store-operations/daily-logs')
    data = _valid_form(page.text)

    def fail(*_args, **_kwargs):
        raise OperationalError('INSERT', {}, RuntimeError('connection interrupted'))

    monkeypatch.setattr('app.routers.v2_daily_store_logs.submit_daily_log', fail)
    response = client.post('/v2/store-operations/daily-logs', data=data)
    assert response.status_code == 500
    assert _hidden(response.text, 'submission_token') == data['submission_token']
    assert 'connection interrupted' not in response.text


def test_management_persistence_failure_preserves_action_identity(daily_site, monkeypatch):
    record_id = _submit(
        daily_site,
        follow_up_required='1',
        follow_up_items='Manager should review the action retry identity.',
    )
    client = daily_site.client('admin')
    detail = client.get(f'/v2/store-operations/daily-logs/{record_id}')
    token = _action_token(detail.text, 'resolve')

    def fail(*_args, **_kwargs):
        raise OperationalError('INSERT', {}, RuntimeError('connection interrupted'))

    monkeypatch.setattr('app.routers.v2_daily_store_logs.perform_management_action', fail)
    response = client.post(
        f'/v2/store-operations/daily-logs/{record_id}/resolve',
        data={
            'csrf_token': _hidden(detail.text, 'csrf_token'),
            'action_token': token,
            'response_note': 'Retry-safe resolution note.',
        },
    )
    assert response.status_code == 500
    assert _action_token(response.text, 'resolve') == token
    assert 'connection interrupted' not in response.text


def test_management_scope_filters_actions_idempotency_and_audit(daily_site):
    record_id = _submit(
        daily_site,
        store='south',
        follow_up_required='1',
        follow_up_items='Manager should verify the damaged display tomorrow.',
    )
    admin = daily_site.client('admin')
    history = admin.get('/v2/store-operations/daily-logs/history?scope=all&actor=alice&follow_up=yes')
    assert history.status_code == 200 and f'/daily-logs/{record_id}' in history.text
    detail = admin.get(f'/v2/store-operations/daily-logs/{record_id}')
    resolve_token = _action_token(detail.text, 'resolve')
    csrf = _hidden(detail.text, 'csrf_token')
    data = {'csrf_token': csrf, 'action_token': resolve_token, 'response_note': 'Display was repaired and verified.'}
    first = admin.post(f'/v2/store-operations/daily-logs/{record_id}/resolve', data=data)
    second = admin.post(f'/v2/store-operations/daily-logs/{record_id}/resolve', data=data)
    assert first.status_code == second.status_code == 303
    with daily_site.Session() as db:
        row = db.get(DailyStoreLog, record_id)
        assert row.lifecycle_status == 'RESOLVED' and row.follow_up_required is False
        assert db.scalar(select(func.count(DailyStoreLogAction.id))) == 1
        audit = db.execute(select(AuditLog).where(AuditLog.action == 'V2:STORE_OPERATIONS:DAILY_LOG_RESOLVED')).scalar_one()
        assert audit.actor_principal_id == daily_site.ids['admin']
        assert audit.meta['before']['follow_up_required'] is True
        assert audit.meta['after']['follow_up_required'] is False


def test_management_override_does_not_expand_store_scope(daily_site):
    north_id = _submit(daily_site, store='north')
    south_id = _submit(daily_site, actor='store2', store='south')
    with daily_site.Session() as db:
        db.add(
            PrincipalPermissionOverride(
                principal_id=daily_site.ids['store'],
                permission_key='management.access',
                allowed=True,
                updated_by_principal_id=daily_site.ids['admin'],
            )
        )
        db.commit()
    client = daily_site.client('store')
    history = client.get('/v2/store-operations/daily-logs/history')
    assert history.status_code == 200
    assert f'/daily-logs/{north_id}' in history.text and f'/daily-logs/{south_id}' not in history.text
    assert client.get(f'/v2/store-operations/daily-logs/{south_id}').status_code == 404


def test_schema_has_only_approved_daily_log_fields_and_v1_m4_routes_remain(daily_site):
    columns = {column['name'] for column in inspect(daily_site.engine).get_columns('daily_store_logs')}
    forbidden = {
        'shift_type',
        'scheduled_shift_id',
        'scheduled_store_id',
        'schedule_match',
        'schedule_warning',
    }
    assert not columns.intersection(forbidden)
    unique_sets = {
        tuple(row['column_names'])
        for row in inspect(daily_site.engine).get_unique_constraints('daily_store_logs')
    }
    assert ('store_id', 'log_date') in unique_sets
    session_columns = {
        column['name'] for column in inspect(daily_site.engine).get_columns('web_sessions')
    }
    assert {'current_store_id', 'current_store_checked_at'} <= session_columns
    assert daily_site.client('store').get('/store/home').status_code == 200
    assert daily_site.client('admin').get('/management/home').status_code == 200
    assert daily_site.client('admin').get('/v2/customer-forms/exchanges-returns/history').status_code == 404
