"""Acknowledgement is per login, informational, permission/CSRF protected."""
from datetime import datetime, timezone
from types import SimpleNamespace
from starlette.responses import Response
from app.security.manual_scheduling_session import acknowledged, signature, set_acknowledgement, COOKIE
from test_v2_primary import primary, login
from test_v2_scheduling_foundation import scheduling_db


def request(user=1, session=1, active=True, cookie=None):
    return SimpleNamespace(state=SimpleNamespace(principal=SimpleNamespace(id=user,active=active),
        web_session_id=session,login_at=datetime(2026,10,21,tzinfo=timezone.utc)),
        cookies={COOKIE:cookie} if cookie is not None else {})


def test_ack_cookie_is_scoped_and_not_a_permanent_preference():
    r=request();token=signature(r)
    assert not acknowledged(r)
    assert acknowledged(request(cookie=token))
    assert not acknowledged(request(user=2,cookie=token))
    assert not acknowledged(request(session=2,cookie=token))
    assert not acknowledged(request(active=False,cookie=token))
    assert not acknowledged(request(cookie='malformed\N{SNOWMAN}'))
    response=Response();set_acknowledgement(response,r)
    cookie=response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Max-Age=' in cookie and 'Path=/' in cookie
    assert 'localStorage' not in cookie and str(r.state.web_session_id) != token


def test_acknowledgement_requires_auth_csrf_and_does_not_survive_new_login(primary):
    primary.account('ADMIN', linked=False)
    assert login(primary).status_code == 303
    path='/v2/scheduling/api/manual-edit-session'
    assert primary.client.get(path).json()=={'acknowledged':False}
    assert primary.client.post(path).status_code==403
    headers={'X-CSRF-Token':primary.client.cookies.get('csrf_token')}
    assert primary.client.post(path,headers=headers).status_code==200
    assert primary.client.get(path).json()=={'acknowledged':True}
    # A distinct authenticated login cannot inherit this acknowledgement cookie.
    old_cookie=primary.client.cookies.get(COOKIE)
    login(primary)
    primary.client.cookies.set(COOKIE,old_cookie)
    assert primary.client.get(path).json()=={'acknowledged':False}


def test_informational_acknowledgement_does_not_grant_employee_edit_permission(primary):
    primary.account('STORE')
    login(primary)
    path='/v2/scheduling/api/manual-edit-session'
    assert primary.client.get(path).status_code==403
    assert primary.client.post(path,headers={'X-CSRF-Token':primary.client.cookies.get('csrf_token')}).status_code==403
