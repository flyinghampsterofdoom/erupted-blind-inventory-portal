"""Rehearse 0031 -> 0033 on an already-restored disposable loopback DB.

Never connects to production. The operator owns secure snapshot transfer and cleanup.
Only sanitized counts, codes and pass/fail evidence are written to the report.
"""
import argparse,hashlib,importlib.util,json,os,subprocess,sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
from sqlalchemy import create_engine,inspect,select,text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def target_url(raw):
    url=make_url(raw)
    if url.host not in {'localhost','127.0.0.1','::1'} or not url.database or not url.database.startswith('erupted_prebeta_release_'):
        raise ValueError('Rehearsal writes require a uniquely named disposable loopback database.')
    return url


def projections(engine):
    result={};inspector=inspect(engine)
    with engine.connect() as c:
        for name in inspector.get_table_names(schema='public'):
            if name=='alembic_version':continue
            columns=[v['name'] for v in inspector.get_columns(name,schema='public')]
            pk=inspector.get_pk_constraint(name,schema='public')['constrained_columns']
            where='';params={}
            if pk:
                key=pk[0];maximum=c.execute(text(f'SELECT max("{key}") FROM "{name}"')).scalar_one()
                if type(maximum) is int:
                    where=f'WHERE "{key}" <= :cutoff';params={'cutoff':maximum}
                elif len(pk)==1:
                    keys=list(c.execute(text(f'SELECT "{key}" FROM "{name}"')).scalars())
                    where=f'WHERE "{key}" = ANY(:keys)';params={'keys':keys}
            result[name]={'columns':columns,'where':where,'params':params}
    return result


def fingerprints(engine,projection):
    result={}
    with engine.connect() as c:
        for name,p in projection.items():
            columns=','.join('"'+v+'"' for v in p['columns'])
            row=c.execute(text(f'''SELECT count(*),md5(coalesce(string_agg(h,'' ORDER BY h),''))
                FROM (SELECT md5(row_to_json(r)::text) AS h FROM
                (SELECT {columns} FROM "{name}" {p['where']}) r) digest'''),p['params']).one()
            result[name]={'count':row[0],'digest':row[1]}
    return result


def probe(directory,mode,env,private):
    result=subprocess.run([sys.executable,'scripts/release/probe_prebeta_artifact.py',mode],cwd=directory,env={**env,'PYTHONPATH':str(directory)},text=True,capture_output=True)
    (private/(mode+'-probe-private.log')).write_text(result.stdout+'\n'+result.stderr)
    if result.returncode:raise RuntimeError(mode+' artifact probe failed; inspect protected local diagnostics.')
    return json.loads(result.stdout.strip().splitlines()[-1])


def main():
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--artifacts',required=True,type=Path);parser.add_argument('--report',required=True,type=Path);parser.add_argument('--private-dir',required=True,type=Path)
    args=parser.parse_args();url=target_url(os.environ['REHEARSAL_DATABASE_URL']);engine=create_engine(url)
    report={'status':'running','production_writes':False,'provider_mutations':False,'schema_steps':[]}
    private=args.private_dir;private.mkdir(mode=0o700,parents=True,exist_ok=True)
    try:
        from app.schema_contract import _alembic_config,current_revision,assert_supported_schema
        from alembic import command
        if current_revision(engine)!='20260930_0031':raise RuntimeError('Expected the restored 0031 baseline.')
        projection=projections(engine);before=fingerprints(engine,projection)
        with engine.connect() as c:
            fk_before=list(c.execute(text("SELECT conrelid::regclass::text,conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='f' AND connamespace='public'::regnamespace ORDER BY 1,2")).tuples())
            seq_before=dict(list(c.execute(text("SELECT sequencename,last_value FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename")).tuples()))
        command.upgrade(_alembic_config(url.render_as_string(hide_password=False)),'20261013_0033')
        assert_supported_schema(engine);report['schema_steps']=['20260930_0031','20261013_0032','20261013_0033']
        after=fingerprints(engine,projection)
        changed=[name for name in before if before[name]!=after[name]]
        report['migration_preservation']={'tables_checked':len(before),'changed_original_tables':changed,'counts':{name:v['count'] for name,v in before.items()}}
        if changed:raise RuntimeError('Migration changed original rows in: '+','.join(changed))
        with engine.connect() as c:
            fks=set(c.execute(text("SELECT conrelid::regclass::text,conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='f' AND connamespace='public'::regnamespace")).tuples())
            sequences=dict(list(c.execute(text("SELECT sequencename,last_value FROM pg_sequences WHERE schemaname='public'")).tuples()))
            assert set(fk_before)<=fks
            assert all(sequences[name]==value for name,value in seq_before.items())
            report['migration_preservation']['foreign_keys_preserved']=True;report['migration_preservation']['existing_sequences_preserved']=True
            assert c.execute(text('SELECT count(*) FROM employees WHERE working_context_version <> 0')).scalar_one()==0
            report['migrated_policy']=[list(r) for r in c.execute(text('SELECT schedule_length_weeks,replenish_at_weeks,timezone_name FROM scheduling_organization_policies'))]
            report['employee_account_links']=c.execute(text('SELECT count(*) FROM employees WHERE principal_id IS NOT NULL')).scalar_one()
            report['periods_by_status']=[list(r) for r in c.execute(text('SELECT status,count(*),min(week_start_date),max(week_start_date) FROM schedule_periods GROUP BY status ORDER BY status'))]
        # Install a reviewed, data-free schema signature in the recovery artifact.
        guard=args.artifacts/'recovery/app/prebeta_recovery_guard.py'
        spec=importlib.util.spec_from_file_location('recovery_contract',guard);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        (guard.parent/'recovery_schema_0033.json').write_text(json.dumps(module.signature(engine),indent=2)+'\n')
        from app import models as m
        from app.auth import Principal,Role
        from app.services.v2_scheduling_policy_service import update_organization_policy,manual_generate_draft_schedule
        from app.services.v2_scheduling_readiness_service import scheduling_readiness
        from app.services.v2_scheduling_pattern_service import sunday
        with Session(engine) as db:
            person=db.scalar(select(m.Principal).where(m.Principal.active.is_(True),m.Principal.role==m.PrincipalRole.ADMIN).order_by(m.Principal.id))
            if person is None:raise RuntimeError('No active Admin exists for clone-only generation attribution.')
            actor=Principal(person.id,person.username,Role.ADMIN,None,True)
            policy=db.scalar(select(m.SchedulingOrganizationPolicy))
            update_organization_policy(db,principal=actor,weekly_approval_hours=policy.weekly_approval_hours,schedule_length_weeks=9,replenish_at_weeks=8,generate_days_before_end=policy.generate_days_before_end,publish_days_before_end=policy.publish_days_before_end,publication_local_time=policy.publication_local_time,timezone_name=policy.timezone_name,active=policy.active)
            db.commit()
            from zoneinfo import ZoneInfo
            now=datetime.now(timezone.utc);today=now.astimezone(ZoneInfo(policy.timezone_name)).date()
            readiness=scheduling_readiness(db,today=today)
            report['generation']={'clone_only_policy':{'X':9,'Y':8},'blocking_codes':[item.code.split(':')[0] for item in readiness.blocking],
                'warning_codes':sorted({item.code.split(':')[0] for item in readiness.warnings})}
            if not readiness.blocking:
                result=manual_generate_draft_schedule(db,principal=actor,now=now);db.commit()
                first=sunday(today)+timedelta(weeks=1)
                periods=list(db.scalars(select(m.SchedulePeriod).where(m.SchedulePeriod.week_start_date.between(first,first+timedelta(weeks=8)),m.SchedulePeriod.status.in_((m.SchedulePeriodStatus.DRAFT,m.SchedulePeriodStatus.PUBLISHED)))))
                starts={p.week_start_date for p in periods};assert starts=={first+timedelta(weeks=i) for i in range(9)}
                created=result['created_period_ids'];assert all(db.get(m.SchedulePeriod,pid).status==m.SchedulePeriodStatus.DRAFT for pid in created)
                again=manual_generate_draft_schedule(db,principal=actor,now=now);db.commit();assert again['created_period_ids']==[]
                report['generation'].update(status='passed',future_weeks=len(starts),new_weeks=len(created),automatically_published=0,repeated_generation_new_weeks=0,
                    future_week_starts=sorted(starts),serious_warning_count=db.scalar(select(__import__('sqlalchemy').func.count()).select_from(m.ScheduleWarning).where(m.ScheduleWarning.schedule_period_id.in_(created),m.ScheduleWarning.severity==m.ScheduleWarningSeverity.SERIOUS)))
            else:report['generation']['status']='blocked_by_existing_configuration'
        check=fingerprints(engine,projection)
        exceptions={'scheduling_organization_policies':'intentional clone-only X/Y change','schedule_warnings':'derived warning cache'}
        unexpected=[name for name in before if before[name]!=check[name] and name not in exceptions]
        if unexpected:raise RuntimeError('Generation modified retained original rows in: '+','.join(unexpected))
        report['generation']['existing_records_preserved']=True
        # Only local credentials are propagated. Never inherit production provider keys.
        env={k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','LANG','LC_ALL'}}
        env.update(DATABASE_URL=url.render_as_string(hide_password=False),ENVIRONMENT='production',DEMO_SEED_ENABLED='false',SCHEMA_REVISION_CHECK_ENABLED='true',SCHEDULE_AUTOMATION_EXECUTION_ENABLED='false',
            SNAPSHOT_PROVIDER='mock',SQUARE_ACCESS_TOKEN='',INTEGRATION_ENCRYPTION_KEY='',APP_SECRET='synthetic-local-release-only',SESSION_COOKIE_SECURE='false',V2_ENABLED_FEATURES='v2_primary,staff_scheduling_v2',V2_PRINCIPAL_FEATURES='',PYTHONDONTWRITEBYTECODE='1',REHEARSAL_PRIVATE_STATE=str(private/'synthetic-probe-state.json'))
        env['PATH']=str(Path(sys.executable).parent)+os.pathsep+env.get('PATH','')
        report['candidate']=probe(args.artifacts/'web-cron','candidate',env,private)
        report['recovery']=probe(args.artifacts/'recovery','recovery',env,private)
        report['candidate_restart']=probe(args.artifacts/'web-cron','restart',env,private)
        final=fingerprints(engine,projection)
        unexpected=[name for name in before if before[name]!=final[name] and name not in exceptions]
        if unexpected:raise RuntimeError('Artifact probes changed retained original records in: '+','.join(unexpected))
        report['retained_records_after_recovery']='all original projections preserved except documented clone-only policy and derived cache'
        # Evidence prevents a destructive return to 0031.
        try:command.downgrade(_alembic_config(url.render_as_string(hide_password=False)),'20260930_0031')
        except RuntimeError as exc:
            if 'evidence must be preserved' not in str(exc):raise
            report['destructive_downgrade']='correctly refused'
        else:raise RuntimeError('Destructive downgrade unexpectedly succeeded.')
        assert current_revision(engine)=='20261013_0033'
        report['status']='passed' if report['generation']['status']=='passed' else 'conditional_configuration_blocker'
    except Exception as exc:
        report['status']='failed';report['error_type']=type(exc).__name__
        raise
    finally:
        engine.dispose();args.report.write_text(json.dumps(report,indent=2,default=str)+'\n')
    print(json.dumps(report,indent=2,default=str))


if __name__=='__main__':main()
