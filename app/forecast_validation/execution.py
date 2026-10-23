"""Disabled-by-default manual one-off entrypoint. Never installs a schedule."""
import argparse,hashlib,json,os,sys,time
from datetime import datetime,timezone
from pathlib import Path
from .contracts import EvidenceError,digest,stamp
from .qualification import GRANT,require_execution


def authorize(config,permit,*,now=None):
    if config.get('execution_authorized') is not True:
        raise EvidenceError('Prepared capacity does not authorize production execution')
    if config.get('recurring_enabled') is not False:
        raise EvidenceError('Recurring execution must remain disabled')
    return require_execution(permit,approved_sha256=config.get('approved_execution_sha256',''),
                             deployment_sha=config.get('deployment_sha',''),now=now)


def backup_window_deadline(began, *, now=None):
    current=time.monotonic() if now is None else now
    deadline=min(current+120,began+900)
    if deadline<=current:raise TimeoutError("No backup time remains in the lifecycle")
    return deadline


def child_environment(environment):
    allowed=('PATH','HOME','PYTHONPATH','LANG','LC_ALL','ENVIRONMENT','SQUARE_ACCESS_TOKEN',
             'SQUARE_ENVIRONMENT','RENDER_GIT_COMMIT','FV_QUALIFICATION_CONFIG','FV_EXECUTION_DIRECTORY',
             'FORECAST_VALIDATION_BACKUP_ENDPOINT_URL','FORECAST_VALIDATION_BACKUP_BUCKET',
             'FORECAST_VALIDATION_BACKUP_ACCESS_KEY_ID','FORECAST_VALIDATION_BACKUP_SECRET_ACCESS_KEY')
    result={k:environment[k] for k in allowed if k in environment}
    source=environment.get('FORECAST_QUALIFICATION_SOURCE_DATABASE_URL')
    writer=environment.get('FORECAST_QUALIFICATION_EVIDENCE_DATABASE_URL')
    if not source or not writer:raise EvidenceError('Explicit least-privilege qualification databases required')
    result.update(DATABASE_URL=source,FORECAST_VALIDATION_DATABASE_URL=writer,
                  FORECAST_VALIDATION_WORKER_ENABLED='false',FORECAST_VALIDATION_SCHEDULE_ENABLED='false')
    return result


def _source_engine(url):
    from sqlalchemy import create_engine
    return create_engine(url,pool_size=1,max_overflow=0,pool_timeout=5,hide_parameters=True,
            isolation_level='REPEATABLE READ',connect_args={'options':
                '-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=1000'})


def run_child(config,permit):
    authorize(config,permit)
    import resource
    if (sys.platform!='linux' or os.getpgrp()!=os.getpid()
            or resource.getrlimit(resource.RLIMIT_AS)[0]>1024**3
            or resource.getrlimit(resource.RLIMIT_AS)[0]<0):
        raise EvidenceError('Kernel-limited private process group required')
    from sqlalchemy import text
    from app.config import settings
    from .storage import Store
    from .qualification import Capacity,QualifiedPeriod
    from .weekly import source_preflight,run_period
    from .collection import collect,readonly,source_session
    from .transport import BoundedRead
    from .exposure import EvidenceGateway
    from app.services.square_ordering_data_service import _square_post
    from .safeguards import private_json,require_capacity,Limits
    from .backup import inventory,archive_to_s3
    from .public_backup import export_public,verify_public_custody
    from .backup_verification import verified_recovery
    from botocore.config import Config
    import boto3
    store=Store(settings.forecast_validation_database_url,business_url=settings.database_url,production=True)
    source=_source_engine(settings.database_url);capacity=Capacity(store);began=time.monotonic();active_period=[None]
    try:
        # Reject business-write credentials even if transaction_read_only was set.
        with source_session(source) as db:
            readonly(db)
            writable=db.execute(text("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND has_table_privilege(current_user,quote_ident(schemaname)||'.'||quote_ident(tablename),'INSERT,UPDATE,DELETE,TRUNCATE'))")).scalar_one()
            if writable:raise EvidenceError('Source principal has business write privileges')
        with store.engine.connect() as db:
            if db.execute(text("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND (tablename NOT LIKE 'fv_%' OR has_table_privilege(current_user,quote_ident(schemaname)||'.'||quote_ident(tablename),'UPDATE,DELETE,TRUNCATE')))")).scalar_one():
                raise EvidenceError('Writer is not isolated and append-only')
        source_preflight(source)
        custody=verify_public_custody(config['custody_receipt'],config['public_recipient'])
        if hashlib.sha256(Path(config['age_binary']).read_bytes()).hexdigest()!=config['age_binary_sha256']:
            raise EvidenceError('Pinned official age binary differs')
        client=boto3.client('s3',endpoint_url=settings.forecast_validation_backup_endpoint_url,
             aws_access_key_id=settings.forecast_validation_backup_access_key_id,
             aws_secret_access_key=settings.forecast_validation_backup_secret_access_key,
             region_name='auto',config=Config(connect_timeout=3,read_timeout=10,retries={'max_attempts':0}))
        def priority():
            if time.monotonic()-began>=900:raise TimeoutError('Absolute manual lifecycle deadline')
            frozen=active_period[0].source() if active_period[0] is not None else None
            if frozen and not 0<=(datetime.now(timezone.utc)-stamp(frozen['origin'])).total_seconds()<=900:
                raise EvidenceError('Frozen qualification origin expired; no completion or recollection')
            require_capacity(private_json(config['observer_signal']),Limits());return True
        def collector(period,remaining):
            source_preflight(source);left=capacity.remaining(permit['slot'])
            seconds=min(420,period.remaining_source_seconds(),900-(time.monotonic()-began))
            transport=BoundedRead(_square_post,max_calls=min(remaining,left['initial']+left['retry']),
                budget=seconds,max_retry_attempts=left['retry'],attempt_hook=period.reserve_request)
            started=time.monotonic()
            snap=collect(source,EvidenceGateway(post=transport),budget_seconds=seconds)
            if time.monotonic()-started>seconds:raise TimeoutError('Source plus freeze preparation budget exceeded')
            snap['source_budget']['square']=transport.metrics();return snap
        def backup(period,publication):
            priority();backup_deadline=backup_window_deadline(began);target=Path(config['backup_directory'])/(period.id+'-'+period.attempt_id+'.age')
            with store.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
                with connection.begin():
                    connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                    snapshot_id=connection.execute(text('SELECT pg_export_snapshot()')).scalar_one()
                    summary=inventory(store,connection=connection)
                    result=export_public(store,target,config['public_recipient'],age=config['age_binary'],
                         timeout_seconds=backup_deadline-time.monotonic(),connection=connection,snapshot_id=snapshot_id)
            result['inventory_sha256']=digest(summary)
            result=archive_to_s3(client,settings.forecast_validation_backup_bucket,target,result,period_id=period.id,custody=custody)
            request_id=digest({'period':period.id,'attempt':period.attempt_id,'archive':result['encrypted_sha256']})
            request={'request_id':request_id,'encrypted_sha256':result['encrypted_sha256'],
                     'object_key':result['object_key'],'recipient':config['public_recipient'],
                     'inventory_sha256':digest(summary),'tables':14}
            client.put_object(Bucket=settings.forecast_validation_backup_bucket,Key='verification/'+request_id+'/request.json',
                              Body=json.dumps(request).encode(),IfNoneMatch='*')
            deadline=backup_deadline
            from botocore.exceptions import ClientError
            while time.monotonic()<deadline:
                priority()
                try:
                    response=client.get_object(Bucket=settings.forecast_validation_backup_bucket,Key='verification/'+request_id+'/receipt.json')
                except ClientError as exc:
                    if exc.response.get('Error',{}).get('Code') not in ('NoSuchKey','404'):raise
                    time.sleep(1);continue
                raw=response['Body'].read(16385)
                if len(raw)>16384:raise EvidenceError('Restoration receipt exceeds bound')
                ack=verified_recovery(result,json.loads(raw),verifier_public_key=config['restore_verifier_public_key'],request_id=request_id)
                priority();return ack
            raise EvidenceError('Independent same-archive recovery not acknowledged; period stays incomplete')
        from .qualification import receipt
        with store.engine.begin() as c:
            receipt(c,'qualification-execution','EXECUTION_AUTHORIZED',
                    {'permit_sha256':digest(permit),'approval_id':permit['approval_id'],'slot':permit['slot'],
                     'deployment_sha':permit['deployment_sha']},key=digest(permit))
        def factory(*a,**kw):
            p=QualifiedPeriod(*a,capacity=capacity,slot=permit['slot'],**kw);active_period[0]=p;return p
        return run_period(store,permit['period_start'],collect_source=collector,pressure=priority,backup=backup,
             evaluate_outcomes=False,budget_seconds=900,forecast_budget_seconds=300,
             scope_policy=permit['scope_policy'],period_factory=factory)
    finally:store.engine.dispose();source.dispose()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--permit-id');p.add_argument('--child',action='store_true');p.add_argument('--plan',action='store_true');a=p.parse_args()
    if a.plan or not a.permit_id:
        print(json.dumps({'status':'PREPARED_DISABLED','capacity':GRANT,'execution_authorized':False}));return
    from .safeguards import private_json,supervise,Limits
    config=private_json(os.environ['FV_QUALIFICATION_CONFIG'])
    permit=private_json(Path(os.environ['FV_EXECUTION_DIRECTORY'])/(a.permit_id+'.json'))
    if digest(permit)!=a.permit_id:raise EvidenceError('Permit identity differs')
    if os.environ.get('RENDER_GIT_COMMIT')!=config.get('deployment_sha'):
        raise EvidenceError('Runtime build does not match the approved deployment')
    authorize(config,permit) # Before any Store, source engine, child or remote write.
    if a.child:result=run_child(config,permit)
    else:
        import signal
        def expired(*args):raise TimeoutError('Absolute job lifetime exceeded')
        signal.signal(signal.SIGALRM,expired);signal.alarm(900)
        result=supervise([sys.executable,'-m','app.forecast_validation.execution','--child','--permit-id',a.permit_id],
             environment=child_environment(os.environ),backup_receipt=private_json(config['backup_gate']),
             observe=lambda:private_json(config['observer_signal']),journal=config['journal'],mutex=config['mutex'],
             limits=Limits(deadline_seconds=900),web_cpu_includes_worker=False)
    print(json.dumps(result))

if __name__=='__main__':main()
