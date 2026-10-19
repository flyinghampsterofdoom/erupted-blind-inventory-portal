from types import SimpleNamespace
import pytest
from scripts.release.history_refresh_result import require_current, failure_message, safe_diagnostic


def test_original_source_error_survives_operational_wrapper():
    original='Square order O1: invalid quantity; history not certified'
    result=SimpleNamespace(started=True,state='failed',message='Square data update failed: '+original)
    with pytest.raises(RuntimeError,match='invalid quantity') as caught:require_current(result)
    state=SimpleNamespace(last_result='FAILED',last_error=original)
    assert failure_message(state,caught.value)==original
    assert original in safe_diagnostic(caught.value)


def test_post_refresh_parity_failure_is_retained_and_credentials_redacted():
    require_current(SimpleNamespace(started=True,state='current'))
    assert failure_message(SimpleNamespace(last_result='COMPLETE',last_error=None),ValueError('Source parity mismatch'))=='Source parity mismatch'
    assert 'secret123' not in safe_diagnostic('postgresql://user:secret123@host/db token=secret123')


def test_finite_runner_logs_original_failure_and_retains_failed_state(monkeypatch,capsys):
    import runpy,signal,urllib.request
    from collections import namedtuple
    from pathlib import Path
    from app.services import square_ordering_data_service as square,v2_ordering_square_gateway as gateway
    original='Square order O-source: version must be an integer when supplied'
    state=SimpleNamespace(last_result='FAILED',last_error=original)
    class FakeDB:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,statement):
            if 'count(*) as lines' in str(statement):
                return SimpleNamespace(one=lambda:namedtuple('Draft','lines units')(896,3842))
            return SimpleNamespace(scalars=lambda:[])
        def get(self,*args):return state
        def commit(self):pass
    monkeypatch.setenv('ERUPTED_EXPECTED_REVISION','a'*40)
    monkeypatch.setenv('ERUPTED_EXPECTED_SOURCE_SHA256','b'*64)
    monkeypatch.setattr('app.services.runtime_identity_service.capture_identity',lambda:{'status':'VERIFIED','revision':'a'*40,'source_tree_sha256':'b'*64})
    monkeypatch.setattr('app.db.SessionLocal',FakeDB)
    monkeypatch.setattr('app.services.v2_square_data_service.refresh_square_sales_data',lambda **kw:SimpleNamespace(started=True,state='failed',message='Square data update failed: '+original))
    old_open=urllib.request.OpenerDirector.open;old_post=square._square_post;old_gateway=gateway._square_post;old_alarm=signal.getsignal(signal.SIGALRM)
    try:
        with pytest.raises(SystemExit) as stopped:
            runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/release/refresh_purchasing_history.py'),run_name='__main__')
        assert stopped.value.code==1
    finally:
        urllib.request.OpenerDirector.open=old_open;square._square_post=old_post;gateway._square_post=old_gateway
        signal.signal(signal.SIGALRM,old_alarm)
    assert state.last_result=='FAILED' and state.last_error==original
    assert original in capsys.readouterr().out
