"""Explicit operator commands. No scheduler, activation, business DML or order action."""
import argparse,json,time,sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.config import settings
from .storage import Store
from .runner import forecast
from .evaluation import evaluate
from .contracts import EvidenceError

from .transport import BoundedRead

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','forecast','evaluate','capture','capture-outcome','status'])
    parser.add_argument('--horizon',type=int,choices=[28,56]);parser.add_argument('--revision');
    parser.add_argument('--file');parser.add_argument('--run-id');args=parser.parse_args()
    if not settings.forecast_validation_worker_enabled:raise EvidenceError('Shadow worker is disabled')
    store=Store(settings.forecast_validation_database_url,business_url=settings.database_url,production=settings.environment=='production')
    if args.command=='init':store.initialize();print('Dedicated evidence schema initialized');return
    if args.command=='status':print(json.dumps({'runs':store.list_runs(),'failures':store.failures()}));return
    if args.command=='forecast':print(forecast(store,json.loads(Path(args.file).read_text())));return
    if args.command=='evaluate':print(evaluate(store,args.run_id,json.loads(Path(args.file).read_text())));return
    import signal
    def expired(*_):raise TimeoutError('Shadow capture hard wall-clock budget exceeded')
    signal.signal(signal.SIGALRM,expired);signal.alarm(960)
    # Use a separately provisioned SELECT-only source role as well as transaction enforcement.
    if not settings.forecast_validation_source_url:raise EvidenceError('Explicit SELECT-only source connection required')
    from .capture import capture_outcome
    from .collection import collect
    from .exposure import EvidenceGateway
    from app.services.square_ordering_data_service import _square_post
    bounded=BoundedRead(_square_post)
    engine=create_engine(settings.forecast_validation_source_url,isolation_level='REPEATABLE READ',connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000'})
    try:
        gateway=EvidenceGateway(post=bounded)
        if args.command=='capture-outcome':
            run=store.get_run(args.run_id)
            if not run:raise EvidenceError('Forecast not found')
            # Compatibility adapter releases its session before the same staged collector.
            from .collection import source_session
            with source_session(engine) as db:outcome=capture_outcome(db,gateway,run,args.horizon,args.revision)
        else:
            snapshot=collect(engine,gateway);snapshot['source_budget']['square']=bounded.metrics()
        print(evaluate(store,args.run_id,outcome) if args.command=='capture-outcome' else forecast(store,snapshot))
    except Exception as exc:
        try:store.attempt('capture','FAILED',type(exc).__name__)
        except Exception as cleanup:exc.collection_attempt_error=type(cleanup).__name__
        raise
    finally:
        signal.alarm(0)
        primary=sys.exc_info()[1]
        try:engine.dispose()
        except Exception as cleanup:
            if primary is None:raise
            primary.collection_dispose_error=type(cleanup).__name__

if __name__=='__main__':main()
