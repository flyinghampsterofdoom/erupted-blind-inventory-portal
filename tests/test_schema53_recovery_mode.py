from types import SimpleNamespace
import pytest
from starlette.requests import Request
from fastapi import HTTPException
from app.config import settings
from app.routers.v2_scheduling import scheduling_write_lock

def test_production_recovery_rejects_interactive_scheduling_writes_before_sql(monkeypatch):
    monkeypatch.setattr(settings,'environment','production')
    request=Request({'type':'http','method':'POST','path':'/v2/scheduling/api/periods/1/approve-and-publish','headers':[]})
    with pytest.raises(HTTPException) as exc:
        scheduling_write_lock(request,SimpleNamespace())
    assert exc.value.status_code==503
    assert 'recovery mode' in exc.value.detail
