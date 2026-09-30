import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import Principal, Role, get_current_principal
from app.config import settings
from app.db import get_db
from app.models import Employee, Principal as PrincipalModel, PrincipalRole, PrincipalPermissionOverride
from app.routers import v2_employees, v2_scheduling
from app.security.csrf import install_csrf_cookie_middleware
from app.security.passwords import verify_password
from app.services.access_control_service import fallback_allowed_for_role, permission_defs, principal_has_permission
from app.services.employee_profile_service import save_employee_account, save_personal_information
from test_v2_scheduling_foundation import scheduling_db


@pytest.fixture
def employee_client(scheduling_db, monkeypatch):
    from app.main import app
    Session, manager, ids, _ = scheduling_db
    current = {'principal': manager}
    test_app = FastAPI()
    test_app.state.templates = app.state.templates
    install_csrf_cookie_middleware(test_app)
    test_app.include_router(v2_scheduling.router)
    test_app.include_router(v2_employees.router)

    @test_app.middleware('http')
    async def identity(request, call_next):
        actor = current['principal']
        request.state.principal = actor
        with Session() as db:
            request.state.permission_flags = {p.key: principal_has_permission(db, principal=actor,
                permission_key=p.key, fallback_allowed=fallback_allowed_for_role(
                    role=actor.role.value, permission_key=p.key)) for p in permission_defs()}
        return await call_next(request)

    def database():
        with Session() as db:
            yield db

    test_app.dependency_overrides[get_db] = database
    test_app.dependency_overrides[get_current_principal] = lambda: current['principal']
    monkeypatch.setattr(settings, 'v2_enabled_features', 'staff_scheduling_v2')
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    with TestClient(test_app, follow_redirects=False) as client:
        client.get('/missing')
        yield client, current, Session, ids


def _post(client, path, **data):
    return client.post(path, data={'csrf_token': client.cookies.get('csrf_token'), **data})


def _admin(db):
    model = PrincipalModel(username='owner@example.test', password_hash='unused', role=PrincipalRole.ADMIN, active=True)
    db.add(model); db.commit()
    return Principal(id=model.id, username=model.username, role=Role.ADMIN, store_id=None, active=True)


def test_hr_and_scheduling_share_directory_profiles_and_contact_persistence(employee_client):
    client, current, Session, ids = employee_client
    for path in ('/v2/hr/employees', '/v2/scheduling/employees'):
        page = client.get(path)
        assert page.status_code == 200
        assert 'HR' in page.text and 'Scheduling' in page.text
        assert 'href="/v2/hr/employees"' in page.text
        assert 'href="/v2/scheduling/employees"' in page.text
        assert f'href="/v2/hr/employees/{ids["alex"]}"' in page.text
    employee_id = ids['alex']
    path = f'/v2/hr/employees/{employee_id}'
    fields = dict(preferred_name='Al', phone='+1 555 123 4567', email='al@example.test',
        street_address='123 Test St', city='Test City', state='WA', postal_code='98600', full_name='Alex One')
    assert _post(client, path + '/personal', **fields).status_code == 303
    for prefix in ('/v2/hr/employees', '/v2/scheduling/employees'):
        html = client.get(f'{prefix}/{employee_id}').text
        for value in fields.values():
            assert value in html
        for section in ('Personal Information', 'Employment', 'Scheduling', 'Account &amp; Access', 'Points / Discipline', 'HR / Records'):
            assert f'<summary>{section}</summary>' in html
        assert '<details class="v2-card employee-section" id="scheduling">' in html
    with Session() as db:
        row = db.get(Employee, employee_id)
        for key, value in fields.items():
            assert getattr(row, key) == value
        assert db.scalar(select(func.count(Employee.id))) == 3
        assert row.principal_id is None
        assert row.scheduling_lead_capable is True
    invalid = _post(client, path + '/personal', preferred_name='Should rollback', email='bad-email')
    assert 'error=' in invalid.headers['location']
    with Session() as db:
        assert db.get(Employee, employee_id).preferred_name == 'Al'


def test_employee_permissions_feature_exposure_and_csrf(employee_client, monkeypatch):
    client, current, Session, ids = employee_client
    path = f'/v2/hr/employees/{ids["alex"]}'
    assert client.post(path + '/personal', data={'email': 'x@example.test'}).status_code == 403
    for role in (Role.STORE, Role.LEAD):
        current['principal'] = Principal(ids['manager'], 'restricted', role, ids['north'], True)
        for route in ('/v2/hr/employees', '/v2/scheduling/employees', path):
            assert client.get(route).status_code == 403
        assert _post(client, path + '/personal', preferred_name='Denied').status_code == 403
        assert _post(client, path + '/account', action='create').status_code == 403
    current['principal'] = Principal(ids['manager'], 'manager', Role.MANAGER, None, True)
    assert _post(client, path + '/account', action='create').status_code == 403
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    assert client.get('/v2/hr/employees').status_code == 404
    assert client.get('/v2/scheduling/employees').status_code == 404


def test_contact_and_account_edits_never_change_square_or_scheduling(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = _admin(db)
        employee = db.get(Employee, ids['alex'])
        employee.square_team_member_id = 'square-read-only'
        db.commit()
        with pytest.raises(ValueError, match='read only'):
            save_personal_information(db, actor=actor, employee_id=employee.id, values={'full_name': 'Different'})
        db.rollback()
        save_personal_information(db, actor=actor, employee_id=employee.id, values={'email': 'alex@example.test'})
        account = save_employee_account(db, actor=actor, employee_id=employee.id, action='create',
            login='alex@example.test', password='long local test password', store_id=ids['south'], confirmed=True)
        db.commit()
        assert account.role == PrincipalRole.STORE
        assert account.password_hash is None and account.recovery_email_confirmed
        assert employee.square_team_member_id == 'square-read-only'
        assert employee.full_name == 'Alex One'
        assert employee.scheduling_lead_capable and employee.scheduling_active
        staff = Principal(account.id, account.username, Role.STORE, account.store_id, True)
        for permission in ('scheduling.generate', 'scheduling.manage_preferences', 'management.users', 'management.admin'):
            assert not principal_has_permission(db, principal=staff, permission_key=permission,
                fallback_allowed=fallback_allowed_for_role(role='STORE', permission_key=permission))
        employee.scheduling_lead_capable = False
        db.commit()
        save_employee_account(db, actor=actor, employee_id=employee.id, action='update',
            role='LEAD', store_id=None, confirmed=True)
        db.commit()
        assert account.role == PrincipalRole.LEAD
        assert not employee.scheduling_lead_capable
        assert employee.principal_id == account.id


def test_association_is_explicit_unique_and_preserves_existing_access(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = _admin(db)
        existing = PrincipalModel(username='individual@example.test', password_hash='existing hash',
                                  role=PrincipalRole.MANAGER, active=True)
        db.add(existing); db.commit()
        for kwargs in ({'confirmed': False}, {'confirmed': True, 'role': 'STORE'}):
            with pytest.raises(ValueError):
                save_employee_account(db, actor=actor, employee_id=ids['alex'], action='associate',
                    login=existing.username, **kwargs)
            db.rollback()
        account = save_employee_account(db, actor=actor, employee_id=ids['alex'], action='associate',
            login=existing.username, role='MANAGER', confirmed=True)
        db.commit()
        assert account.id == existing.id and account.role == PrincipalRole.MANAGER
        assert account.password_hash == 'existing hash'
        with pytest.raises(ValueError, match='another employee'):
            save_employee_account(db, actor=actor, employee_id=ids['blair'], action='associate',
                login=existing.username, role='MANAGER', confirmed=True)
        db.rollback()
        assert db.get(Employee, ids['blair']).principal_id is None
        assert db.scalar(select(func.count(PrincipalModel.id))) == 3


def test_account_routes_require_admin_capability_and_atomic_validation(employee_client):
    client, current, Session, ids = employee_client
    with Session() as db:
        current['principal'] = _admin(db)
    path = f'/v2/hr/employees/{ids["alex"]}/account'
    import os
    if os.getenv('EMPLOYEE_UI_PREVIEW_DIR'):
        from pathlib import Path
        output = Path(os.environ['EMPLOYEE_UI_PREVIEW_DIR']); output.mkdir(parents=True, exist_ok=True)
        (output / 'admin.html').write_text(client.get(f'/v2/hr/employees/{ids["alex"]}').text)
    args = dict(action='create', login='new@example.test', password='long local test password',
                store_id=str(ids['north']), confirm_identity='true')
    assert client.post(path, data=args).status_code == 403
    bad = _post(client, path, **{**args, 'role': 'SUPERADMIN'})
    assert 'error=' in bad.headers['location']
    with Session() as db:
        assert db.get(Employee, ids['alex']).principal_id is None
        assert db.scalar(select(func.count(PrincipalModel.id))) == 2
    assert 'Employee%20account%20saved' in _post(client, path, **args).headers['location']
    with Session() as db:
        employee = db.get(Employee, ids['alex'])
        account = db.get(PrincipalModel, employee.principal_id)
        assert account.role == PrincipalRole.STORE and account.store_id == ids['north']
        assert employee.scheduling_lead_capable is True
        db.add(PrincipalPermissionOverride(principal_id=current['principal'].id,
            permission_key='management.users', allowed=False))
        db.commit()
    assert _post(client, path, action='update', role='ADMIN', confirm_identity='true').status_code == 403


def test_profile_time_off_link_uses_employee_id_and_preserves_requests(employee_client):
    from datetime import date
    from app.services.v2_scheduling_rules_service import TimeOffInput, create_time_off_request
    from app.models import TimeOffRequest
    client, current, Session, ids = employee_client
    with Session() as db:
        for key in ('alex', 'blair'):
            create_time_off_request(db, principal=current['principal'], values=TimeOffInput(
                employee_id=ids[key], start_date=date(2026, 10, 5), end_date=date(2026, 10, 5),
                full_day=True, reason_category_id=ids['vacation'], employee_note=f'{key} request'))
        db.commit()
    page = client.get(f'/v2/hr/employees/{ids["alex"]}')
    assert f'/v2/scheduling/time-off?employee_id={ids["alex"]}' in page.text
    assert '2026-10-05' in page.text
    queue = client.get(f'/v2/scheduling/time-off?employee_id={ids["alex"]}')
    assert queue.status_code == 200
    assert 'alex request' in queue.text and 'blair request' not in queue.text
    assert f'name="employee_id" value="{ids["alex"]}"' in queue.text
    with Session() as db:
        assert db.scalar(select(func.count(TimeOffRequest.id))) == 2


def test_hr_records_retain_existing_management_permission(employee_client):
    from app.models import EmployeeLogEntry
    client, current, Session, ids = employee_client
    with Session() as db:
        db.add(EmployeeLogEntry(employee_id=ids['alex'], category_label='Record',
            note='Confidential existing record', created_by_principal_id=ids['manager']))
        db.commit()
    path = f'/v2/hr/employees/{ids["alex"]}'
    assert 'Confidential existing record' in client.get(path).text
    with Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=ids['manager'],
            permission_key='management.access', allowed=False))
        db.commit()
    assert 'Confidential existing record' not in client.get(path).text


def test_legacy_store_login_editor_never_claims_an_individual_account(scheduling_db):
    from app.services.session_service import list_store_login_rows, upsert_store_login_credentials
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = _admin(db)
        individual = save_employee_account(db, actor=actor, employee_id=ids['alex'], action='create',
            login='alex@example.test', password='individual test password', store_id=ids['north'], confirmed=True)
        db.commit()
        old_hash = individual.password_hash
        with pytest.raises(ValueError, match='Individual employee logins'):
            upsert_store_login_credentials(db, store_id=ids['north'],
                username=individual.username, new_password='must never be applied')
        db.rollback()
        rows = {row['store_id']: row for row in list_store_login_rows(db)}
        assert rows[ids['north']]['principal_id'] is None
        shared, created = upsert_store_login_credentials(db, store_id=ids['north'],
            username='north-shared', new_password='shared test password')
        db.commit()
        assert created and shared.id != individual.id
        shared_id = shared.id
        shared, created = upsert_store_login_credentials(db, store_id=ids['north'],
            username='north-shared-renamed', new_password='new shared test password')
        db.commit()
        assert not created and shared.id == shared_id
        db.refresh(individual)
        assert individual.username == 'alex@example.test' and individual.password_hash == old_hash
        assert db.get(Employee, ids['alex']).principal_id == individual.id
        rows = {row['store_id']: row for row in list_store_login_rows(db)}
        assert rows[ids['north']]['principal_id'] == shared_id
