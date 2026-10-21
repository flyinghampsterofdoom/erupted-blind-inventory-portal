from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.security.csrf import CSRF_COOKIE_NAME, install_csrf_cookie_middleware, verify_csrf


def _app() -> FastAPI:
    app = FastAPI()
    install_csrf_cookie_middleware(app)

    @app.get('/token')
    def token():
        return {'ok': True}

    @app.post('/mutation')
    def mutation(_csrf: None = Depends(verify_csrf)):
        return {'ok': True}

    return app


def test_csrf_json_header_is_accepted_and_invalid_or_missing_header_is_rejected():
    with TestClient(_app()) as client:
        client.get('/token')
        token = client.cookies.get(CSRF_COOKIE_NAME)
        assert token
        assert client.post('/mutation', json={}, headers={'X-CSRF-Token': token}).status_code == 200
        assert client.post('/mutation', json={}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
        assert client.post('/mutation', json={}).status_code == 403


def test_existing_csrf_form_behavior_is_unchanged():
    with TestClient(_app()) as client:
        client.get('/token')
        token = client.cookies.get(CSRF_COOKIE_NAME)
        assert client.post('/mutation', data={'csrf_token': token}).status_code == 200
        assert client.post('/mutation', data={'csrf_token': 'wrong'}).status_code == 403
