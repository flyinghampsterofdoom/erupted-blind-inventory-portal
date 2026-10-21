from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient
from app.security.sessions import install_auth_session_middleware
from app.security.csrf import install_csrf_cookie_middleware


def test_only_matching_versioned_assets_are_immutable_and_cookie_free(monkeypatch):
    monkeypatch.setenv('RENDER_GIT_COMMIT','a'*40)
    app=FastAPI();app.mount('/v2-assets',StaticFiles(directory=Path(__file__).resolve().parents[1]/'app/static/v2'))
    install_csrf_cookie_middleware(app);install_auth_session_middleware(app)
    with TestClient(app) as client:
        current=client.get('/v2-assets/v2.css?v='+'a'*12)
        assert current.status_code==200
        assert current.headers['cache-control']=='public, max-age=31536000, immutable'
        assert 'set-cookie' not in current.headers
        old=client.get('/v2-assets/v2.css?v='+'b'*12)
        assert 'immutable' not in old.headers.get('cache-control','')
        assert 'immutable' not in client.get('/v2-assets/v2.css').headers.get('cache-control','')
