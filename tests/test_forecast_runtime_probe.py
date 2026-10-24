from datetime import datetime,timezone,timedelta
import base64,json
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from starlette.requests import Request
from app.forecast_validation.contracts import canonical,EvidenceError
from app.forecast_validation.runtime_probe import authorize_probe
from app.routers.forecast_validation import runtime_observation
from app.config import settings

NOW=datetime(2026,10,23,23,tzinfo=timezone.utc)
def keys():
 private=Ed25519PrivateKey.generate();public=base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode();ts=int(NOW.timestamp());body=canonical({'purpose':'fv-runtime-read-v1','timestamp':ts}).encode();sig=base64.b64encode(private.sign(body)).decode();return public,str(ts),sig

def test_signed_runtime_probe_authorization():
 pub,ts,sig=keys();authorize_probe(pub,ts,sig,now=NOW)
 for args in [('',ts,sig),(pub,str(int(ts)-31),sig),(pub,str(int(ts)+31),sig),(pub,ts,'bad'),(pub,None,None)]:
  with pytest.raises(EvidenceError):authorize_probe(*args,now=NOW)

def req(headers=()):return Request({'type':'http','method':'GET','path':'/management/forecast-validation/runtime','headers':headers})
def test_probe_disabled_and_unsigned_denied(monkeypatch):
 monkeypatch.setattr(settings,'forecast_validation_monitor_public_key','')
 with pytest.raises(HTTPException) as e:runtime_observation(req())
 assert e.value.status_code==404
 monkeypatch.setattr(settings,'forecast_validation_monitor_public_key',keys()[0])
 with pytest.raises(HTTPException) as e:runtime_observation(req())
 assert e.value.status_code==403

def test_probe_is_bounded_and_has_no_source_or_database_access(monkeypatch):
 import app.forecast_validation.runtime_probe as probe
 from app.routers import forecast_validation as route
 monkeypatch.setattr(settings,'forecast_validation_monitor_public_key',keys()[0]);monkeypatch.setattr(probe,'authorize_probe',lambda *a:None)
 monkeypatch.setattr(probe,'measure',lambda:dict(at=NOW.isoformat(),memory_bytes=123,cpu_usage_seconds=1,instance_id='synthetic',revision='test'))
 monkeypatch.setattr(route,'evidence_store',lambda:pytest.fail('Telemetry must not open evidence'))
 response=runtime_observation(req());assert response.status_code==200 and json.loads(response.body)['memory_bytes']==123
 assert response.headers['cache-control']=='no-store'
 assert route.read_slots.acquire(blocking=False) and route.read_slots.acquire(blocking=False)
 try:
  with pytest.raises(HTTPException) as e:runtime_observation(req())
  assert e.value.status_code==503
 finally:route.read_slots.release();route.read_slots.release()


def test_probe_through_real_session_middleware_without_business_session(monkeypatch):
    import app.security.sessions as sessions
    import app.forecast_validation.runtime_probe as probe
    from app.main import app
    from fastapi.testclient import TestClient
    def forbidden():
        pytest.fail('Independent runtime GET must not resolve a business session')
    monkeypatch.setattr(sessions, 'SessionLocal', forbidden)
    identity = Ed25519PrivateKey.generate()
    public = base64.b64encode(identity.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    monkeypatch.setattr(settings, 'forecast_validation_monitor_public_key', public)
    monkeypatch.setattr(probe, 'measure', lambda: {'memory_bytes': 123, 'cpu_usage_seconds': 1})
    ts = int(datetime.now(timezone.utc).timestamp())
    sig = base64.b64encode(identity.sign(canonical({'purpose':'fv-runtime-read-v1','timestamp':ts}).encode())).decode()
    client = TestClient(app)
    path = '/management/forecast-validation/runtime'
    assert client.get(path).status_code == 403
    assert client.get(path, headers={'X-FV-Timestamp':str(ts),'X-FV-Signature':sig}).status_code == 200
    monkeypatch.setattr(settings, 'forecast_validation_monitor_public_key', '')
    assert client.get(path, headers={'X-FV-Timestamp':str(ts),'X-FV-Signature':sig}).status_code == 404
