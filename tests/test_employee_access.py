"""Real PostgreSQL and HTTP coverage; no live provider or production database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, time
import hashlib
import json
import threading
import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.auth import Role, Principal as Actor
from app.config import settings
from app.db import get_db
from app.models import (Principal, PrincipalRole, Employee, PasswordResetToken, WebSession,
    ApplicationSetting, AuditLog, SchedulePeriod, SchedulePeriodStatus, ScheduleShift, PrincipalPermissionOverride)
from app.security.passwords import hash_password, verify_password
from app.security.sessions import create_web_session, install_auth_session_middleware
from app.security.csrf import install_csrf_cookie_middleware
from app.services import password_reset_service as resets, transactional_email_service as emails
from app.services.application_settings_service import save_resend_configuration, resend_configuration, ConfigurationError
from app.services.employee_profile_service import save_employee_account
from test_v2_employee_profiles import _admin
from test_v2_scheduling_foundation import scheduling_db

PASSWORD = 'correct horse employee password'


@pytest.fixture
def access(scheduling_db, monkeypatch):
    from app.main import app as main_app
    from app.routers import auth, password_access, integrations, v2_employees, v2_scheduling, management
    from app.security import sessions
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        admin = _admin(db)
        admin_token = create_web_session(db, admin.id, None, None)
        db.commit()
    test_app = FastAPI()
    test_app.state.templates = main_app.state.templates
    for module in (auth, password_access, integrations, v2_employees, v2_scheduling, management):
        test_app.include_router(module.router)
    install_csrf_cookie_middleware(test_app)
    install_auth_session_middleware(test_app)
    monkeypatch.setattr(sessions, 'SessionLocal', Session)
    monkeypatch.setattr(settings, 'v2_enabled_features', 'staff_scheduling_v2')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    monkeypatch.setattr(settings, 'integration_encryption_key', Fernet.generate_key().decode())
    def database():
        with Session() as db:
            yield db
    test_app.dependency_overrides[get_db] = database
    sent = []
    def send(db, **kwargs):
        sent.append(kwargs)
        return emails.EmailResult(True, 'Email accepted by provider.')
    monkeypatch.setattr(resets, 'send_password_email', send)
    with TestClient(test_app, follow_redirects=False, client=('127.0.0.1', 50000)) as client:
        client.cookies.set(settings.session_cookie_name, admin_token)
        client.get('/login')
        yield client, Session, admin, ids, sent


def post(client, path, **data):
    return client.post(path, data={'csrf_token': client.cookies.get('csrf_token'), **data})


def make_account(Session, admin, ids, *, role='STORE'):
    with Session() as db:
        account = save_employee_account(db, actor=admin, employee_id=ids['alex'], action='create',
            login='alex@example.test', role=role, store_id=ids['north'] if role == 'STORE' else None, confirmed=True)
        db.commit()
        return account.id


def test_complete_path(access):
    client, Session, admin, ids, sent = access
    response = post(client, f'/v2/hr/employees/{ids["alex"]}/account', action='create',
        login='alex@example.test', role='STORE', store_id=str(ids['north']), confirm_identity='true')
    assert response.status_code == 303 and 'accepted' in response.headers['location']
    assert sent[0]['setup'] is True
    token = sent[0]['token']
    with Session() as db:
        account = db.scalar(select(Principal).where(Principal.username == 'alex@example.test'))
        assert account.password_hash is None and account.recovery_email_confirmed
        account_id = account.id
        old_session = create_web_session(db, account.id, None, None)
        db.commit()
    client.cookies.clear(); client.get('/login')
    assert post(client, '/login', username='alex@example.test', password=PASSWORD).status_code == 401
    for _ in range(2):
        page = client.get('/password')
        assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
    with Session() as db:
        assert db.scalar(select(PasswordResetToken)).consumed_at is None
    assert post(client, '/password', token=token, password=PASSWORD, confirmation=PASSWORD).headers['location'] == '/login?password_saved=1'
    assert post(client, '/password', token=token, password=PASSWORD, confirmation=PASSWORD).status_code == 400
    login = post(client, '/login', username='ALEX@example.test', password=PASSWORD)
    assert login.headers['location'] == '/v2/scheduling/my-schedule'
    assert client.get('/v2/scheduling/my-schedule?employee_id=' + str(ids['blair'])).status_code == 200
    with Session() as db:
        assert verify_password(PASSWORD, db.get(Principal, account_id).password_hash)
        assert db.scalar(select(WebSession).where(WebSession.session_token == old_session)).revoked_at
        assert token not in str([row.meta for row in db.scalars(select(AuditLog))])
        assert db.scalar(select(PasswordResetToken)).digest == hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.parametrize('mutation', ['expired', 'email', 'link', 'inactive_account', 'inactive_employee', 'unconfirmed'])
def test_token_rejects_invalid_state(access, mutation):
    client, Session, admin, ids, sent = access
    pid = make_account(Session, admin, ids)
    with Session() as db:
        row, token, _ = resets.issue(db, pid)
        if mutation == 'expired': row.expires_at = resets.now() - timedelta(seconds=1)
        if mutation == 'email': db.get(Principal, pid).username = 'different@example.test'
        if mutation == 'link': db.get(Employee, ids['alex']).principal_id = None
        if mutation == 'inactive_account': db.get(Principal, pid).active = False
        if mutation == 'inactive_employee': db.get(Employee, ids['alex']).active = False
        if mutation == 'unconfirmed': db.get(Principal, pid).recovery_email_confirmed = False
        db.commit()
        with pytest.raises(ValueError, match='invalid'):
            resets.redeem(db, token, PASSWORD, PASSWORD)
        db.rollback()
        assert db.get(Principal, pid).password_hash is None


def test_replacement_and_concurrent_redemption(access):
    client, Session, admin, ids, sent = access
    pid = make_account(Session, admin, ids)
    with Session() as db:
        _, old, _ = resets.issue(db, pid); db.commit()
        _, token, _ = resets.issue(db, pid); db.commit()
        with pytest.raises(ValueError): resets.redeem(db, old, PASSWORD, PASSWORD)
        db.rollback()
    barrier = threading.Barrier(2)
    def redeem():
        with Session() as db:
            barrier.wait()
            try:
                resets.redeem(db, token, PASSWORD, PASSWORD); db.commit(); return True
            except ValueError:
                db.rollback(); return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: redeem(), range(2))) == [False, True]


def test_forgot_generic_and_throttle(access):
    client, Session, admin, ids, sent = access
    pid = make_account(Session, admin, ids)
    client.cookies.clear(); client.get('/forgot-password')
    responses = [post(client, '/forgot-password', email=email) for email in ['alex@example.test', 'unknown@example.test']]
    assert responses[0].text == responses[1].text and len(sent) == 1
    with Session() as db:
        db.get(Principal, pid).active = False; db.commit()
    assert post(client, '/forgot-password', email='alex@example.test').text == responses[0].text
    assert len(sent) == 1
    for _ in range(5): post(client, '/forgot-password', email='alex@example.test')
    assert len(sent) == 1
    assert client.post('/forgot-password', data={'email':'alex@example.test'}).status_code == 403


@pytest.mark.parametrize('role', ['STORE','LEAD','MANAGER','ADMIN'])
def test_own_schedule_scope_roles_and_denies(access, role):
    from zoneinfo import ZoneInfo
    client, Session, admin, ids, sent = access
    pid = make_account(Session, admin, ids, role=role)
    today = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    start = today - timedelta(days=today.weekday())
    with Session() as db:
        account = db.get(Principal, pid); account.password_hash = hash_password(PASSWORD)
        token = create_web_session(db, pid, None, None)
        for week, status, rev, employee, label in [
            (0,'PUBLISHED',1,ids['alex'],'OWN'), (1,'PUBLISHED',1,ids['alex'],'NEXT'),
            (0,'DRAFT',2,ids['alex'],'DRAFT'), (0,'ARCHIVED',3,ids['alex'],'OLD'),
            (2,'PUBLISHED',1,ids['alex'],'FUTURE'),(-1,'PUBLISHED',1,ids['alex'],'PAST')]:
            day = start + timedelta(days=week*7)
            period=SchedulePeriod(week_start_date=day,week_end_date=day+timedelta(days=6),status=SchedulePeriodStatus(status),revision_number=rev,created_by_principal_id=admin.id,updated_by_principal_id=admin.id)
            db.add(period); db.flush()
            db.add(ScheduleShift(schedule_period_id=period.id,employee_id=employee,store_id=ids['south'],shift_date=day,start_time=time(9),end_time=time(17),created_by_principal_id=admin.id,updated_by_principal_id=admin.id,is_opener=True))
            if label == 'OWN':
                db.add(ScheduleShift(schedule_period_id=period.id,employee_id=ids['blair'],store_id=ids['north'],shift_date=day,start_time=time(20),end_time=time(21),created_by_principal_id=admin.id,updated_by_principal_id=admin.id))
        db.commit()
    client.cookies.clear(); client.get('/login')
    client.cookies.set(settings.session_cookie_name, token)
    page = client.get('/v2/scheduling/my-schedule?employee_id='+str(ids['blair']))
    assert page.status_code == 200
    assert page.text.count('9:00 AM') == 2 and '8:00 PM' not in page.text
    assert 'South' in page.text and 'Offer / transfer' not in page.text and 'Recipient' not in page.text
    assert post(client, '/v2/scheduling/my-schedule/transfers',shift_id='1',to_employee_id=str(ids['blair'])).status_code == 403
    with Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=pid,permission_key='scheduling.view_own',allowed=False)); db.commit()
    assert client.get('/v2/scheduling/my-schedule').status_code == 403


def test_settings_encryption_preservation_redaction_and_live_update(access):
    client, Session, admin, ids, sent = access
    path='/admin/settings/integrations'
    values=dict(enabled='true',from_email='accounts@example.test',from_name='Erupted Vapor',reply_to='',portal_url='https://portal.example.test')
    assert client.post(path,data=values).status_code == 403
    assert post(client,path,**values,replace_key='true',api_key='secret-key-one').status_code == 303
    page=client.get(path)
    assert 'Configured' in page.text and 'secret-key-one' not in page.text
    with Session() as db:
        row=db.get(ApplicationSetting,'integration.resend')
        assert 'secret-key-one' not in row.encrypted_secrets
        assert resend_configuration(db,decrypt=True).api_key == 'secret-key-one'
    post(client,path,**{**values,'from_name':'New name'},replace_key='true',api_key='')
    with Session() as db:
        assert resend_configuration(db,decrypt=True).api_key == 'secret-key-one'
        assert resend_configuration(db).from_name == 'New name'
    post(client,path,**values,replace_key='true',api_key='secret-key-two')
    with Session() as db:
        assert resend_configuration(db,decrypt=True).api_key == 'secret-key-two'
        audit=str([row.meta for row in db.scalars(select(AuditLog))])
        assert 'secret-key' not in audit and 'api_key_replaced' in audit
    pid=make_account(Session,admin,ids)
    with Session() as db:
        token=create_web_session(db,pid,None,None);db.commit()
    client.cookies.clear(); client.get('/login')
    client.cookies.set(settings.session_cookie_name,token)
    assert client.get(path).status_code == 403 and post(client,path,**values).status_code == 403


@pytest.mark.parametrize('outcome',[200,422,'timeout'])
def test_provider_errors_and_no_secret_leaks(access, monkeypatch, caplog, outcome):
    client, Session, admin, ids, sent=access
    with Session() as db:
        save_resend_configuration(db,actor=admin,values=dict(enabled='true',from_email='accounts@example.test',portal_url='https://portal.example.test'),replacement_key='secret-provider-key');db.commit()
        def fake(self,*args,**kwargs):
            assert kwargs['headers']['Authorization']=='Bearer secret-provider-key'
            assert kwargs['json']['text'] and kwargs['json']['html']
            if outcome=='timeout': raise httpx.ReadTimeout('secret-provider-key SHOULD NOT LEAK')
            return httpx.Response(outcome,text='secret-provider-key SHOULD NOT LEAK')
        monkeypatch.setattr(httpx.Client,'post',fake)
        result=emails.send_password_email(db,recipient='employee@example.test',token='usable-token-secret',setup=True,token_id=42)
        assert result.accepted == (outcome==200)
        assert 'secret' not in result.message and 'secret-provider-key' not in caplog.text


def test_missing_configuration_keeps_account_and_scheduling_archive_is_separate(access, monkeypatch):
    client,Session,admin,ids,sent=access
    monkeypatch.setattr(resets,'send_password_email',emails.send_password_email)
    monkeypatch.setattr(settings, 'integration_encryption_key', None)
    def forbidden_delivery(*args, **kwargs):
        raise AssertionError('Unconfigured email must not contact a provider')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden_delivery)
    response=post(client,f'/v2/hr/employees/{ids["alex"]}/account',action='create',login='alex@example.test',role='STORE',store_id=str(ids['north']),confirm_identity='true')
    assert 'Employee+account+saved' in response.headers['location'] or 'Employee%20account%20saved' in response.headers['location']
    from urllib.parse import unquote_plus
    assert 'disabled or incompletely configured' in unquote_plus(response.headers['location'])
    assert client.get('/v2/hr/employees').status_code == 200
    assert client.get('/admin/settings/integrations').status_code == 200
    with Session() as db:
        employee=db.get(Employee,ids['alex']);assert employee.principal_id
        employee.scheduling_active=False;db.commit()
        assert resets.eligible(db,db.get(Principal,employee.principal_id)) is not None


def test_legacy_bypass_and_hash_safety(access):
    from app.services.session_service import reset_management_user_password
    client,Session,admin,ids,sent=access
    pid=make_account(Session,admin,ids,role='LEAD')
    with Session() as db:
        with pytest.raises(ValueError,match='employee profile'):
            reset_management_user_password(db,actor=admin,target_principal_id=pid,new_password=PASSWORD)
        assert db.get(Principal,pid).password_hash is None
    assert post(client, f'/management/users/{pid}/password', new_password=PASSWORD).status_code == 400
    users_page = client.get('/management/users')
    assert 'Send Password Reset in employee profile' in users_page.text
    assert not verify_password(PASSWORD,None)
    assert not verify_password(PASSWORD,'malformed')


def test_feature_gate_and_inactive_login(access, monkeypatch):
    client, Session, admin, ids, sent = access
    pid = make_account(Session, admin, ids)
    with Session() as db:
        account=db.get(Principal,pid);account.password_hash=hash_password(PASSWORD);db.commit()
    client.cookies.clear();client.get('/login')
    assert post(client,'/login',username='alex@example.test',password=PASSWORD).status_code==303
    monkeypatch.setattr(settings,'v2_enabled_features','')
    assert client.get('/v2/scheduling/my-schedule').status_code==404
    monkeypatch.setattr(settings,'v2_enabled_features','staff_scheduling_v2')
    with Session() as db:
        db.get(Employee,ids['alex']).active=False;db.commit()
    assert client.get('/v2/scheduling/my-schedule').status_code==403
    assert post(client,'/login',username='alex@example.test',password=PASSWORD).status_code==401


def test_unlinked_and_inactive_forgot_and_legacy_login(access):
    client, Session, admin, ids, sent = access
    with Session() as db:
        db.add(Principal(username='legacy', password_hash=hash_password(PASSWORD), role=PrincipalRole.STORE,store_id=ids['north'],active=True))
        db.add(Principal(username='unlinked@example.test',password_hash=None,role=PrincipalRole.LEAD,active=True,recovery_email_confirmed=True));db.commit()
    pid=make_account(Session,admin,ids)
    with Session() as db:
        db.get(Employee,ids['alex']).active=False;db.commit()
    client.cookies.clear();client.get('/login')
    responses=[post(client,'/forgot-password',email=email) for email in ('unlinked@example.test','alex@example.test','missing@example.test')]
    assert len({r.text for r in responses})==1 and not sent
    assert post(client,'/login',username='legacy',password=PASSWORD).headers['location']=='/'
    assert client.get('/v2/scheduling/my-schedule').status_code==409


def test_existing_association_keeps_hash_and_sends_reset(access):
    client,Session,admin,ids,sent=access
    original=hash_password(PASSWORD)
    with Session() as db:
        account=Principal(username='existing@example.test',password_hash=original,role=PrincipalRole.LEAD,active=True)
        db.add(account);db.commit();pid=account.id
    response=post(client,f'/v2/hr/employees/{ids["alex"]}/account',action='associate',login='existing@example.test',role='LEAD',confirm_identity='true')
    assert response.status_code==303 and sent[-1]['setup'] is False
    with Session() as db:
        assert db.get(Principal,pid).password_hash==original
        assert db.get(Employee,ids['alex']).principal_id==pid


def test_admin_send_confirmation_and_permission_denial(access):
    client,Session,admin,ids,sent=access
    pid=make_account(Session,admin,ids)
    with Session() as db:
        db.get(Principal,pid).recovery_email_confirmed=False;db.commit()
    path=f'/v2/hr/employees/{ids["alex"]}/password-email'
    assert 'error=' in post(client,path).headers['location'] and not sent
    assert 'message=' in post(client,path,confirm_recovery='true').headers['location'] and len(sent)==1
    assert client.post(path).status_code==403
    with Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=admin.id,permission_key='management.users',allowed=False));db.commit()
    assert post(client,path).status_code==403
    assert client.get('/admin/settings/integrations').status_code==403


def test_key_missing_wrong_key_and_validation(access, monkeypatch):
    client,Session,admin,ids,sent=access
    values=dict(enabled='true',from_email='accounts@example.test',portal_url='https://portal.example.test')
    with Session() as db:
        save_resend_configuration(db,actor=admin,values=values,replacement_key='preserved-key');db.commit()
        monkeypatch.setattr(settings,'integration_encryption_key',Fernet.generate_key().decode())
        config=resend_configuration(db,decrypt=True)
        assert config.api_key=='' and 'decrypt' in config.status
        assert not emails.send_email(db,recipient='x@example.test',subject='test',text='test',html='test').accepted
        monkeypatch.setattr(settings,'integration_encryption_key',None)
        with pytest.raises(ConfigurationError):
            save_resend_configuration(db,actor=admin,values=values,replacement_key='new-key')
        db.rollback()
        with pytest.raises(ConfigurationError):
            save_resend_configuration(db,actor=admin,values={**values,'portal_url':'https://example.test/?secret=bad'})
        db.rollback()
        for bad_url in ('https://[invalid', 'https://example.test:wrong', 'https://exam\nple.test'):
            with pytest.raises(ConfigurationError):
                save_resend_configuration(db,actor=admin,values={**values,'portal_url':bad_url})
            db.rollback()


def test_concurrent_replacements_leave_only_one_usable_token(access):
    client,Session,admin,ids,sent=access
    pid=make_account(Session,admin,ids)
    barrier=threading.Barrier(2)
    def issue():
        with Session() as db:
            barrier.wait();row,token,_=resets.issue(db,pid);db.commit();return token
    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens=list(pool.map(lambda _:issue(),range(2)))
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(PasswordResetToken).where(PasswordResetToken.revoked_at.is_(None)))==1
        assert len(set(tokens))==2


def test_test_email_route_uses_saved_configuration(access,monkeypatch):
    from app.routers import integrations
    client,Session,admin,ids,sent=access
    response=post(client,'/admin/settings/integrations/test',recipient='owner@example.test')
    assert 'error=' in response.headers['location']
    monkeypatch.setattr(integrations,'send_email',lambda *args,**kwargs:emails.EmailResult(True,'Email accepted by provider. Inbox delivery is not confirmed.'))
    response=post(client,'/admin/settings/integrations/test',recipient='owner@example.test')
    assert 'message=' in response.headers['location'] and 'accepted' in response.headers['location']


@pytest.mark.parametrize('role',['STORE','LEAD','MANAGER'])
def test_non_admin_integration_access_denied_even_with_capability(access,role):
    client,Session,admin,ids,sent=access
    pid=make_account(Session,admin,ids,role=role)
    with Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=pid,permission_key='management.users',allowed=True))
        token=create_web_session(db,pid,None,None);db.commit()
    client.cookies.clear();client.get('/login');client.cookies.set(settings.session_cookie_name,token)
    assert client.get('/admin/settings/integrations').status_code==403
    assert post(client,'/admin/settings/integrations',enabled='true').status_code==403
    assert post(client,'/admin/settings/integrations/test',recipient='x@example.test').status_code==403


def test_linked_manager_self_change_invalidates_tokens_and_sessions(access):
    from app.services.session_service import reset_manager_password
    client,Session,admin,ids,sent=access
    pid=make_account(Session,admin,ids,role='MANAGER')
    with Session() as db:
        db.get(Principal,pid).password_hash=hash_password(PASSWORD);db.commit()
        token=create_web_session(db,pid,None,None)
        _,reset_token,_=resets.issue(db,pid);db.commit()
        with pytest.raises(ValueError,match='12 to 1024'):
            reset_manager_password(db,manager_principal_id=pid,current_password=PASSWORD,new_password='short',confirm_password='short')
        db.rollback()
        reset_manager_password(db,manager_principal_id=pid,current_password=PASSWORD,new_password=PASSWORD+' new',confirm_password=PASSWORD+' new');db.commit()
        assert db.scalar(select(WebSession).where(WebSession.session_token==token)).revoked_at
        with pytest.raises(ValueError):resets.redeem(db,reset_token,PASSWORD,PASSWORD)
