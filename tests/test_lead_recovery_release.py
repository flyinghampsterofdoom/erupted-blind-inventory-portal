"""Short-login recovery stays tied to an Admin-confirmed employee contact."""
import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal,Role
from app.security.passwords import hash_password,verify_password
from app.services import password_reset_service as resets
from app.security.sessions import create_web_session
from app.services.application_settings_service import confirm_employee_recovery,ConfigurationError
from test_employee_access import access,post,make_account,PASSWORD
from test_v2_scheduling_foundation import scheduling_db


def short_account(access):
    client,Session,admin,ids,sent=access
    pid=make_account(Session,admin,ids,role='LEAD')
    with Session() as db:
        account=db.get(m.Principal,pid);account.username='familiar-lead';account.password_hash=hash_password(PASSWORD)
        account.recovery_email_confirmed=False
        db.get(m.Employee,ids['alex']).email='alex@example.test';db.commit()
    return pid


def test_short_username_self_service_recovery_keeps_login_and_revokes_sessions(access):
    c,Session,admin,ids,sent=access;pid=short_account(access)
    with Session() as db:
        old_hash=db.get(m.Principal,pid).password_hash
        confirm_employee_recovery(db,actor=admin,employee_id=ids['alex'],confirmed=True);db.commit()
        assert db.get(m.Principal,pid).password_hash==old_hash
    c.cookies.clear();c.get('/forgot-password')
    response=post(c,'/forgot-password',email='FAMILIAR-LEAD')
    assert response.status_code==200 and len(sent)==1
    assert sent[0]['recipient']=='alex@example.test'
    token=sent[0]['token']
    assert post(c,'/password',token=token,password=PASSWORD+' new',confirmation=PASSWORD+' new').status_code==303
    assert post(c,'/login',username='familiar-lead',password=PASSWORD+' new').status_code==303
    assert c.get('/v2/scheduling/my-schedule').status_code==200
    assert c.get('/management/users').status_code==403
    with Session() as db:
        account=db.get(m.Principal,pid)
        assert account.username=='familiar-lead' and account.role==m.PrincipalRole.LEAD
        assert verify_password(PASSWORD+' new',account.password_hash)
        assert db.scalar(select(m.PasswordResetToken).where(m.PasswordResetToken.principal_id==pid)).consumed_at


@pytest.mark.parametrize('mutation',['contact','username','link','inactive','unconfirmed'])
def test_recovery_token_fails_closed_when_ownership_changes(access,mutation):
    _,Session,admin,ids,_=access;pid=short_account(access)
    with Session() as db:
        confirm_employee_recovery(db,actor=admin,employee_id=ids['alex'],confirmed=True)
        _,token,_=resets.issue(db,pid);db.commit();before=db.get(m.Principal,pid).password_hash
        account=db.get(m.Principal,pid);employee=db.get(m.Employee,ids['alex'])
        if mutation=='contact':employee.email='different@example.test'
        if mutation=='username':account.username='different-lead'
        if mutation=='link':employee.principal_id=None
        if mutation=='inactive':account.active=False
        if mutation=='unconfirmed':account.recovery_email_confirmed=False
        db.commit()
        with pytest.raises(ValueError,match='invalid'):resets.redeem(db,token,PASSWORD+' new',PASSWORD+' new')
        db.rollback();assert db.get(m.Principal,pid).password_hash==before


def test_recovery_address_requires_admin_confirmation_and_denies_ambiguity(access):
    _,Session,admin,ids,_=access;pid=short_account(access)
    with Session() as db:
        actor=Principal(pid,'familiar-lead',Role.LEAD,None,True)
        with pytest.raises(PermissionError):confirm_employee_recovery(db,actor=actor,employee_id=ids['alex'],confirmed=True)
        with pytest.raises(ConfigurationError):confirm_employee_recovery(db,actor=admin,employee_id=ids['alex'],confirmed=False)
        with pytest.raises(ValueError):resets.issue(db,pid)
        other=m.Principal(username='another@example.test',role=m.PrincipalRole.STORE,active=True,store_id=ids['north']);db.add(other);db.flush()
        employee=db.get(m.Employee,ids['blair']);employee.principal_id=other.id;employee.email='alex@example.test';db.flush()
        with pytest.raises(ConfigurationError,match='shared'):confirm_employee_recovery(db,actor=admin,employee_id=ids['alex'],confirmed=True)


def test_email_request_reaches_canonical_short_login_not_disabled_duplicate(access):
    _,Session,admin,ids,_=access;pid=short_account(access)
    with Session() as db:
        duplicate=m.Principal(username='alex@example.test',role=m.PrincipalRole.LEAD,active=False)
        db.add(duplicate)
        confirm_employee_recovery(db,actor=admin,employee_id=ids['alex'],confirmed=True);db.commit()
        assert resets.recovery_account(db,'alex@example.test').id==pid
        assert resets.recovery_account(db,'familiar-lead').id==pid
        assert resets.recovery_account(db,'not-a-user') is None


def test_contact_edit_revokes_confirmation_and_outstanding_credentials(access):
    from app.services.employee_profile_service import save_personal_information
    _,Session,admin,ids,_=access;pid=short_account(access)
    with Session() as db:
        confirm_employee_recovery(db,actor=admin,employee_id=ids['alex'],confirmed=True)
        row,token,_=resets.issue(db,pid)
        session_token=create_web_session(db,pid,ip=None,user_agent='isolated-recovery-test')
        db.commit()
        save_personal_information(db,actor=admin,employee_id=ids['alex'],values={'email':'changed@example.test'})
        db.commit()
        assert not db.get(m.Principal,pid).recovery_email_confirmed
        db.refresh(row)
        assert row.revoked_at
        assert db.scalar(select(m.WebSession).where(m.WebSession.session_token==session_token)).revoked_at
        with pytest.raises(ValueError,match='invalid'):
            resets.redeem(db,token,PASSWORD+' new',PASSWORD+' new')
        assert resets.recovery_account(db,'familiar-lead') is None
