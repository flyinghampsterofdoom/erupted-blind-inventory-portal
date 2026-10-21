from fastapi.testclient import TestClient
from app.config import settings
from app.main import app
from app.services.scheduling_execution_control import scheduling_execution_enabled


def test_release_pause_blocks_every_http_mutation_before_database_access(monkeypatch):
    monkeypatch.setattr(settings,'scheduling_read_only',True)
    monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED','true')
    assert not scheduling_execution_enabled()
    client=TestClient(app)
    for method,path in [('POST','/v2/scheduling/my-schedule/transfers'),
        ('PATCH','/v2/scheduling/api/shifts/1'),('DELETE','/v2/scheduling/api/shifts/1'),
        ('POST','/v2/scheduling/automation/run')]:
        response=client.request(method,path)
        assert response.status_code==503 and response.headers['cache-control']=='no-store'
    assert client.get('/v2/scheduling/my-schedule',follow_redirects=False).status_code!=503
    assert client.get('/healthz').status_code!=503
