"""Browser login recovery must reject stale submissions before authentication."""
import re
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from app.main import app as portal, login_response_cache_control, v2_http_exception_handler
from app.routers.auth import login_page
from app.db import get_db
from app.security.csrf import install_csrf_cookie_middleware, verify_csrf

HTML = {"Accept": "text/html,application/xhtml+xml"}
SECRET = "must-not-appear-in-recovery-html"

def client_app():
    app = FastAPI()
    app.state.templates = portal.state.templates
    app.add_exception_handler(HTTPException, v2_http_exception_handler)
    install_csrf_cookie_middleware(app)
    app.middleware("http")(login_response_cache_control)
    app.dependency_overrides[get_db] = lambda: None
    app.add_api_route("/login", login_page, methods=["GET"])
    auth_calls = []
    @app.post("/login")
    async def login(request: Request, csrf=Depends(verify_csrf)):
        auth_calls.append(True)
        return RedirectResponse("/signed-in", status_code=303)
    @app.post("/mutation")
    async def mutation(csrf=Depends(verify_csrf)):
        return {"ok": True}
    @app.get("/unrelated")
    async def unrelated():
        return {"ok": True}
    return app, auth_calls

def token(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)

def test_restored_form_with_replaced_cookie_returns_fresh_form_without_authentication():
    app, calls = client_app()
    with TestClient(app) as client:
        old = token(client.get("/login?return_to=/explorer/", headers=HTML))
        client.cookies.clear()
        client.cookies.set("csrf_token", "new-tab-token", domain="testserver.local", path="/")
        failed = client.post("/login", data={"csrf_token": old, "username": "someone", "password": SECRET,
            "return_to": "/explorer/?from=tablet"}, headers=HTML)
        assert failed.status_code == 403 and "text/html" in failed.headers["content-type"]
        assert "Please sign in again." in failed.text and token(failed) == "new-tab-token"
        assert 'value="/explorer/?from=tablet"' in failed.text
        assert SECRET not in failed.text and not calls
        retried = client.post("/login", data={"csrf_token": token(failed), "password": SECRET}, headers=HTML, follow_redirects=False)
        assert retried.status_code == 303 and calls == [True]

def test_lost_cookie_gets_a_matching_cookie_and_form_without_replaying_password():
    app, calls = client_app()
    with TestClient(app) as client:
        stale = token(client.get("/login", headers=HTML))
        client.cookies.clear()
        failed = client.post("/login", data={"csrf_token": stale, "password": SECRET}, headers=HTML)
        assert failed.status_code == 403 and not calls and SECRET not in failed.text
        assert token(failed) == client.cookies.get("csrf_token")
        assert token(failed) != stale
        assert client.post("/login", data={"csrf_token": token(failed)}, headers=HTML, follow_redirects=False).status_code == 303
        assert calls == [True]

def test_login_api_and_non_login_csrf_failures_remain_json_403():
    app, calls = client_app()
    with TestClient(app) as client:
        client.get("/login", headers=HTML)
        for path, headers in [("/login", {"Accept": "application/json"}), ("/mutation", HTML),
            ("/login", {**HTML, "X-CSRF-Token": "wrong"})]:
            failed = client.post(path, data={"csrf_token": "wrong", "password": SECRET}, headers=headers)
            assert failed.status_code == 403
            assert failed.json() == {"detail": "Invalid CSRF token"}
        assert not calls

def test_login_html_and_failure_responses_are_not_cacheable():
    app, calls = client_app()
    with TestClient(app) as client:
        initial = client.get("/login", headers=HTML)
        failed = client.post("/login", data={"csrf_token": "stale"}, headers=HTML)
        for response in (initial, failed):
            assert response.headers["cache-control"] == "no-store, private"
            assert response.headers["pragma"] == "no-cache"
            assert response.headers["expires"] == "0"
        assert "cache-control" not in client.get("/unrelated").headers

# Exercise the real login route, session middleware and PostgreSQL in addition to
# the focused handler tests above. These are isolated synthetic test identities.
import pytest
from sqlalchemy import select, func
from app.models import WebSession
from test_v2_primary import primary, PASSWORD
from test_v2_scheduling_foundation import scheduling_db

@pytest.mark.parametrize("cookie_state", ["lost", "replaced"])
def test_real_login_recovery_never_authenticates_the_rejected_post(primary, cookie_state):
    pid = primary.account("ADMIN", linked=False)
    client = primary.client
    stale = token(client.get("/login", headers=HTML))
    client.cookies.clear()
    if cookie_state == "replaced":
        client.cookies.set("csrf_token", "second-tab-token", domain="testserver.local", path="/")
    response = client.post("/login", headers=HTML,
        data={"csrf_token": stale, "username": "person@example.test", "password": PASSWORD,
              "return_to": "/v2/overview"})
    assert response.status_code == 403 and "text/html" in response.headers["content-type"]
    assert PASSWORD not in response.text and token(response) == client.cookies.get("csrf_token")
    with primary.Session() as db:
        assert db.scalar(select(func.count()).select_from(WebSession).where(WebSession.principal_id == pid)) == 0
    denied = client.post("/login", headers=HTML,
        data={"csrf_token": token(response), "username": "person@example.test", "password": "wrong"})
    assert denied.status_code == 401 and "Invalid username or password" in denied.text
    recovered = client.post("/login", headers=HTML,
        data={"csrf_token": token(denied), "username": "person@example.test", "password": PASSWORD,
              "return_to": "/v2/overview"})
    assert recovered.status_code == 303 and recovered.headers["location"] == "/v2/overview"
    with primary.Session() as db:
        assert db.scalar(select(func.count()).select_from(WebSession).where(WebSession.principal_id == pid)) == 1
