import asyncio
import json
import time
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient
from starlette.concurrency import run_in_threadpool
from app.config import settings
from app.performance import PerformanceMiddleware
from app.security.sessions import install_auth_session_middleware
from pathlib import Path


def test_only_known_public_assets_bypass_authentication(monkeypatch):
    app=FastAPI();app.mount('/v2-assets',StaticFiles(directory=Path(__file__).resolve().parents[1]/'app/static/v2'))
    def forbidden():raise RuntimeError('auth reached')
    monkeypatch.setattr('app.security.sessions.SessionLocal',forbidden)
    install_auth_session_middleware(app)
    with TestClient(app,raise_server_exceptions=False) as client:
        assert client.get('/v2-assets/v2.css').status_code==200
        assert client.get('/v2-assets/not-public.json').status_code==500
        assert client.get('/v2/overview').status_code==500
        assert client.post('/v2-assets/v2.css').status_code==500


def test_telemetry_uses_route_templates_and_numeric_fields_only(monkeypatch,caplog):
    monkeypatch.setattr(settings,'performance_metrics_enabled',True)
    monkeypatch.setattr(settings,'performance_sample_rate',1.)
    app=FastAPI();app.add_middleware(PerformanceMiddleware)
    @app.get('/records/{record_id}')
    async def read(record_id:str):
        # Independent coroutine remains runnable during the synchronous operation.
        tick=asyncio.create_task(asyncio.sleep(.01,result=True))
        await run_in_threadpool(time.sleep,.03)
        assert tick.done()
        return {'ok':True}
    with caplog.at_level('INFO',logger='erupted.performance'):
        with TestClient(app) as client:
            r=client.get('/records/private-value?token=not-for-logs',headers={'Authorization':'Bearer private-secret'})
            assert r.status_code==200 and 'x-correlation-id' in r.headers
    row=json.loads(next(record.message for record in caplog.records if 'request_performance' in record.message))
    assert row['route']=='/records/{record_id}' and row['request_ms']>=30
    assert 'private-value' not in caplog.text and 'private-secret' not in caplog.text and 'not-for-logs' not in caplog.text
