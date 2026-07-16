from __future__ import annotations

import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.auth import Principal, Role
from app.config import settings
from app.models import (
    AuditLog,
    ExchangeReturnForm,
    Principal as PrincipalModel,
    PrincipalPermissionOverride,
    PrincipalRole,
    RolePermissionOverride,
    Store,
    WebSession,
)
from app.routers.v2_exchanges_returns import _form_values
from app.schema_contract import upgrade_database
from app.services.v2_exchange_return_service import (
    FEATURE_KEY,
    ExchangeReturnInput,
    issue_submission_token,
    submit_exchange_return,
)


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


@pytest.fixture
def module_site(monkeypatch):
    if not ADMIN_URL:
        pytest.skip('set TEST_POSTGRES_ADMIN_URL for V2 route integration')
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_exchanges_{uuid.uuid4().hex[:10]}'
    database_url = f'{ADMIN_URL.rsplit("/", 1)[0]}/{database_name}'
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    upgrade_database(database_url)
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        north = Store(name='North', square_location_id='NORTH', active=True)
        south = Store(name='South', square_location_id='SOUTH', active=True)
        db.add_all([north, south])
        db.flush()
        people = {
            'store': PrincipalModel(username='alice', password_hash='unused', role=PrincipalRole.STORE, store_id=north.id, active=True),
            'store2': PrincipalModel(username='bob', password_hash='unused', role=PrincipalRole.STORE, store_id=south.id, active=True),
            'legacy': PrincipalModel(
                username='legacy-north',
                password_hash='unused',
                role=PrincipalRole.STORE,
                store_id=north.id,
                custom_role_label='Legacy/shared account',
                active=True,
            ),
            'lead': PrincipalModel(username='lead', password_hash='unused', role=PrincipalRole.LEAD, store_id=None, active=True),
            'manager': PrincipalModel(username='manager', password_hash='unused', role=PrincipalRole.MANAGER, store_id=None, active=True),
            'admin': PrincipalModel(username='admin', password_hash='unused', role=PrincipalRole.ADMIN, store_id=None, active=True),
            'inactive': PrincipalModel(username='inactive', password_hash='unused', role=PrincipalRole.STORE, store_id=north.id, active=False),
        }
        db.add_all(people.values())
        db.flush()
        tokens = {}
        for key, person in people.items():
            token = f'test-session-{key}'
            tokens[key] = token
            db.add(
                WebSession(
                    session_token=token,
                    principal_id=person.id,
                    ip=None,
                    user_agent='pytest',
                    expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=2),
                )
            )
        db.commit()
        ids = {key: int(person.id) for key, person in people.items()}
        store_ids = {'north': int(north.id), 'south': int(south.id)}

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

        def add_record(
            *,
            store='north',
            actor='store',
            created_at=datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc),
            original='ORIG-1',
            exchange='EXCH-1',
            refund=False,
            items='Item details',
        ) -> int:
            with Session() as db:
                row = ExchangeReturnForm(
                    store_id=store_ids[store],
                    employee_name=people[actor].username,
                    original_purchase_date=date(2026, 7, 10),
                    generated_at=created_at,
                    original_ticket_number=original,
                    exchange_ticket_number=exchange,
                    items_text=items,
                    reason_text='Damaged product',
                    refund_given=refund,
                    refund_approved_by='Taylor Lead',
                    created_by_principal_id=ids[actor],
                    created_at=created_at,
                )
                db.add(row)
                db.commit()
                return int(row.id)

        yield SimpleNamespace(
            client=client,
            Session=Session,
            ids=ids,
            store_ids=store_ids,
            add_record=add_record,
        )

    engine.dispose()
    with admin_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
    admin_engine.dispose()


def _hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', html)
    assert match, name
    return match.group(1)


def _valid_form(page_html: str) -> dict[str, str]:
    return {
        'csrf_token': _hidden(page_html, 'csrf_token'),
        'submission_token': _hidden(page_html, 'submission_token'),
        'original_purchase_date': '2026-07-01',
        'original_ticket_number': 'ORIG-100',
        'exchange_ticket_number': 'EXCH-200',
        'items_text': 'Broken item',
        'reason_text': 'Damaged',
        'refund_given': 'N',
        'refund_approved_by': 'Taylor Lead',
    }


def test_authentication_exposure_and_authorization_are_independent(module_site, monkeypatch):
    assert module_site.client().get('/v2/customer-forms/exchanges-returns').status_code == 303
    store_client = module_site.client('store')
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    assert store_client.get('/v2/customer-forms/exchanges-returns').status_code == 404
    monkeypatch.setattr(settings, 'v2_enabled_features', FEATURE_KEY)
    assert store_client.get('/v2/customer-forms/exchanges-returns').status_code == 200
    assert store_client.get('/v2/customer-forms/exchanges-returns/history').status_code == 403
    assert module_site.client('inactive').get('/v2/customer-forms/exchanges-returns').status_code == 403


@pytest.mark.parametrize(
    'field',
    [
        'original_purchase_date',
        'original_ticket_number',
        'exchange_ticket_number',
        'items_text',
        'reason_text',
        'refund_given',
        'refund_approved_by',
    ],
)
def test_v2_required_field_characterization(field):
    values = {
        'original_purchase_date': '2026-07-01',
        'original_ticket_number': 'A',
        'exchange_ticket_number': 'B',
        'items_text': 'item',
        'reason_text': 'reason',
        'refund_given': 'N',
        'refund_approved_by': 'lead',
    }
    values[field] = ''
    _, errors, parsed = _form_values(values)
    assert field in errors
    assert parsed is None


def test_valid_submission_uses_authenticated_actor_locked_store_server_time_and_safe_audit(module_site):
    client = module_site.client('store')
    page = client.get('/v2/customer-forms/exchanges-returns')
    assert 'alice' in page.text and 'North' in page.text
    data = _valid_form(page.text)
    data.update({'employee_name': 'Imposter', 'store_id': str(module_site.store_ids['south']), 'generated_at': '2000-01-01T00:00:00Z'})
    before = datetime.now(tz=timezone.utc)
    response = client.post('/v2/customer-forms/exchanges-returns', data=data)
    assert response.status_code == 303
    with module_site.Session() as db:
        row = db.execute(select(ExchangeReturnForm)).scalar_one()
        assert row.store_id == module_site.store_ids['north']
        assert row.created_by_principal_id == module_site.ids['store']
        assert row.employee_name == 'alice'
        assert row.generated_at >= before
        audit = db.execute(select(AuditLog).where(AuditLog.action == 'V2:CUSTOMER_FORMS:SUBMITTED')).scalar_one()
        assert audit.actor_principal_id == module_site.ids['store']
        assert audit.meta['entity_id'] == str(row.id)
        assert audit.meta['correlation_id']
        assert audit.meta['metadata']['refund_given'] is False
        assert 'csrf' not in str(audit.meta).lower()


def test_manipulated_query_store_is_forbidden(module_site):
    client = module_site.client('store')
    page = client.get('/v2/customer-forms/exchanges-returns')
    response = client.post(
        f'/v2/customer-forms/exchanges-returns?store_id={module_site.store_ids["south"]}',
        data=_valid_form(page.text),
    )
    assert response.status_code == 403
    with module_site.Session() as db:
        assert db.scalar(select(func.count(ExchangeReturnForm.id))) == 0


def test_refund_approver_remains_required_for_no_refund_and_values_are_retained(module_site):
    client = module_site.client('store')
    page = client.get('/v2/customer-forms/exchanges-returns')
    data = _valid_form(page.text)
    data['refund_approved_by'] = ''
    response = client.post('/v2/customer-forms/exchanges-returns', data=data)
    assert response.status_code == 422
    assert 'Current V1 behavior requires this field for both Yes and No' in response.text
    assert 'ORIG-100' in response.text
    assert 'aria-invalid="true"' in response.text
    assert 'href="#refund_approved_by"' in response.text


def test_csrf_is_required(module_site):
    client = module_site.client('store')
    page = client.get('/v2/customer-forms/exchanges-returns')
    data = _valid_form(page.text)
    data.pop('csrf_token')
    assert client.post('/v2/customer-forms/exchanges-returns', data=data).status_code == 403


def test_repeated_identical_submission_token_returns_original_record(module_site):
    client = module_site.client('store')
    page = client.get('/v2/customer-forms/exchanges-returns')
    data = _valid_form(page.text)
    first = client.post('/v2/customer-forms/exchanges-returns', data=data)
    second = client.post('/v2/customer-forms/exchanges-returns', data=data)
    assert first.status_code == second.status_code == 303
    assert 'duplicate=0' in first.headers['location']
    assert 'duplicate=1' in second.headers['location']
    with module_site.Session() as db:
        assert db.scalar(select(func.count(ExchangeReturnForm.id))) == 1
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == 'V2:CUSTOMER_FORMS:SUBMITTED')) == 1


def test_persistence_failure_retains_idempotency_token_for_safe_retry(module_site, monkeypatch):
    client = module_site.client('store')
    page = client.get('/v2/customer-forms/exchanges-returns')
    data = _valid_form(page.text)

    def fail_submission(*_args, **_kwargs):
        raise OperationalError('INSERT', {}, RuntimeError('connection interrupted'))

    monkeypatch.setattr('app.routers.v2_exchanges_returns.submit_exchange_return', fail_submission)
    response = client.post('/v2/customer-forms/exchanges-returns', data=data)
    assert response.status_code == 500
    assert _hidden(response.text, 'submission_token') == data['submission_token']
    assert 'connection interrupted' not in response.text
    assert 'href="#submission"' not in response.text


def test_concurrent_double_click_is_serialized_by_submission_fingerprint(module_site):
    principal = Principal(
        id=module_site.ids['store'],
        username='alice',
        role=Role.STORE,
        store_id=module_site.store_ids['north'],
        active=True,
    )
    token = issue_submission_token(principal_id=principal.id)
    values = ExchangeReturnInput(
        original_purchase_date=date(2026, 7, 1),
        original_ticket_number='CONCURRENT-1',
        exchange_ticket_number='CONCURRENT-2',
        items_text='Item',
        reason_text='Reason',
        refund_given=False,
        refund_approved_by='Taylor Lead',
    )

    def submit_once():
        with module_site.Session() as db:
            outcome = submit_exchange_return(
                db,
                principal=principal,
                submission_token=token,
                values=values,
                ip=None,
            )
            db.commit()
            return outcome

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _value: submit_once(), range(2)))
    assert sorted(outcome.duplicate for outcome in outcomes) == [False, True]
    assert outcomes[0].record_id == outcomes[1].record_id
    with module_site.Session() as db:
        assert db.scalar(select(func.count(ExchangeReturnForm.id))) == 1


def test_missing_store_is_rejected_before_token_use():
    principal = Principal(id=4, username='person', role=Role.STORE, store_id=None, active=True)
    with pytest.raises(PermissionError, match='assigned store'):
        submit_exchange_return(None, principal=principal, submission_token='unused', values=None, ip=None)


def test_management_history_scope_filters_search_sort_and_roles(module_site):
    older = module_site.add_record(store='north', actor='store', created_at=datetime(2026, 7, 14, 17, tzinfo=timezone.utc), original='NORTH-OLD')
    newer = module_site.add_record(store='south', actor='store2', created_at=datetime(2026, 7, 15, 18, tzinfo=timezone.utc), original='SOUTH-NEW', refund=True)
    for role in ('lead', 'manager', 'admin'):
        assert module_site.client(role).get('/v2/customer-forms/exchanges-returns/history?scope=all').status_code == 200
    client = module_site.client('admin')
    all_page = client.get('/v2/customer-forms/exchanges-returns/history?scope=all')
    assert all_page.text.index(f'/exchanges-returns/{newer}') < all_page.text.index(f'/exchanges-returns/{older}')
    north = client.get(f'/v2/customer-forms/exchanges-returns/history?store_id={module_site.store_ids["north"]}')
    assert 'NORTH-OLD' in north.text and 'SOUTH-NEW' not in north.text
    search = client.get('/v2/customer-forms/exchanges-returns/history?scope=all&q=SOUTH-NEW&refund=yes')
    assert 'SOUTH-NEW' in search.text and 'NORTH-OLD' not in search.text
    actor = client.get('/v2/customer-forms/exchanges-returns/history?scope=all&actor=bob')
    assert 'SOUTH-NEW' in actor.text and 'NORTH-OLD' not in actor.text
    date_page = client.get('/v2/customer-forms/exchanges-returns/history?scope=all&from=2026-07-15&to=2026-07-15')
    assert 'SOUTH-NEW' in date_page.text and 'NORTH-OLD' not in date_page.text
    assert client.get('/v2/customer-forms/exchanges-returns/history?store_id=999999').status_code == 403


def test_management_history_honors_role_and_principal_overrides(module_site):
    with module_site.Session() as db:
        db.add(
            RolePermissionOverride(
                role=PrincipalRole.LEAD,
                permission_key='management.access',
                allowed=False,
                updated_by_principal_id=module_site.ids['admin'],
            )
        )
        db.commit()
    assert module_site.client('lead').get('/v2/customer-forms/exchanges-returns/history').status_code == 403
    with module_site.Session() as db:
        db.add(
            PrincipalPermissionOverride(
                principal_id=module_site.ids['lead'],
                permission_key='management.access',
                allowed=True,
                updated_by_principal_id=module_site.ids['admin'],
            )
        )
        db.add(
            PrincipalPermissionOverride(
                principal_id=module_site.ids['admin'],
                permission_key='management.access',
                allowed=False,
                updated_by_principal_id=module_site.ids['admin'],
            )
        )
        db.commit()
    assert module_site.client('lead').get('/v2/customer-forms/exchanges-returns/history').status_code == 200
    assert module_site.client('admin').get('/v2/customer-forms/exchanges-returns/history').status_code == 403


def test_detail_authorization_actor_legacy_note_safe_text_and_not_found(module_site):
    individual = module_site.add_record(items='<script>alert(1)</script>\nSecond line')
    legacy = module_site.add_record(actor='legacy', original='LEGACY-1')
    admin = module_site.client('admin')
    detail = admin.get(f'/v2/customer-forms/exchanges-returns/{individual}')
    assert detail.status_code == 200
    assert 'alice' in detail.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in detail.text
    assert '<script>alert(1)</script>' not in detail.text
    legacy_detail = admin.get(f'/v2/customer-forms/exchanges-returns/{legacy}')
    assert 'Legacy/shared account attribution' in legacy_detail.text
    assert admin.get('/v2/customer-forms/exchanges-returns/999999').status_code == 404
    assert module_site.client('store').get(f'/v2/customer-forms/exchanges-returns/{individual}').status_code == 403
    with module_site.Session() as db:
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == 'V2:CUSTOMER_FORMS:VIEWED')) == 2


def test_management_override_does_not_expand_store_record_scope(module_site):
    own_record = module_site.add_record(store='north', actor='store', original='OWN-STORE')
    other_record = module_site.add_record(store='south', actor='store2', original='OTHER-STORE')
    with module_site.Session() as db:
        db.add(
            PrincipalPermissionOverride(
                principal_id=module_site.ids['store'],
                permission_key='management.access',
                allowed=True,
                updated_by_principal_id=module_site.ids['admin'],
            )
        )
        db.commit()

    client = module_site.client('store')
    history = client.get('/v2/customer-forms/exchanges-returns/history')
    assert history.status_code == 200
    assert 'OWN-STORE' in history.text
    assert 'OTHER-STORE' not in history.text
    assert client.get(f'/v2/customer-forms/exchanges-returns/{own_record}').status_code == 200
    assert client.get(f'/v2/customer-forms/exchanges-returns/{other_record}').status_code == 404


def test_v1_routes_and_v2_shell_remain_registered_and_operational(module_site):
    assert module_site.client('store').get('/store/exchange-return-form').status_code == 200
    assert module_site.client('admin').get('/management/exchange-return-forms').status_code == 200
    assert module_site.client('admin').get('/v2/overview').status_code == 200


def test_v1_submission_history_and_detail_behavior_remain_operational(module_site):
    store_client = module_site.client('store')
    page = store_client.get('/store/exchange-return-form')
    response = store_client.post(
        '/store/exchange-return-form/submit',
        data={
            'csrf_token': _hidden(page.text, 'csrf_token'),
            'generated_at': _hidden(page.text, 'generated_at'),
            'employee_name': 'V1 Employee',
            'original_purchase_date': '2026-07-02',
            'original_ticket_number': 'V1-ORIGINAL',
            'exchange_ticket_number': 'V1-EXCHANGE',
            'items_text': 'V1 item details',
            'reason_text': 'V1 reason',
            'refund_given': 'N',
            'refund_approved_by': 'V1 Lead',
        },
    )
    assert response.status_code == 303
    assert response.headers['location'] == '/store/exchange-return-form'

    with module_site.Session() as db:
        row = db.execute(
            select(ExchangeReturnForm).where(ExchangeReturnForm.original_ticket_number == 'V1-ORIGINAL')
        ).scalar_one()
        assert row.store_id == module_site.store_ids['north']
        assert row.created_by_principal_id == module_site.ids['store']
        assert db.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.action == 'EXCHANGE_RETURN_FORM_SUBMITTED')
        ) == 1
        record_id = int(row.id)

    admin = module_site.client('admin')
    history = admin.get('/management/exchange-return-forms')
    assert history.status_code == 200
    assert 'V1 Employee' in history.text
    detail = admin.get(f'/management/exchange-return-forms/{record_id}')
    assert detail.status_code == 200
    assert 'V1-ORIGINAL' in detail.text
    with module_site.Session() as db:
        assert db.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.action == 'EXCHANGE_RETURN_FORM_VIEWED_AUDIT')
        ) == 1
