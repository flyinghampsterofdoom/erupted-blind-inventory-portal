"""Additional clone-only approval and retained-new-evidence recovery acceptance."""
import argparse,json,os,sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session
from scripts.release.rehearse_prebeta import target_url,projections,fingerprints,probe


def main():
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--artifacts',required=True,type=Path);parser.add_argument('--private-dir',required=True,type=Path);parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();url=target_url(os.environ['REHEARSAL_DATABASE_URL']);engine=create_engine(url)
    from app import models as m
    from app.auth import Principal,Role
    from app.services.v2_scheduling_service import validate_publication_assignments,approve_schedule,publish_schedule
    from app.services.inventory_working_store_service import authorize_store
    from app.services.v2_scheduling_preference_request_service import submit_request,decide_request
    from app.services.v2_scheduling_prebeta_service import publication_status
    protected=('principals','employees','stores','coverage_requirements','schedule_periods','schedule_shifts',
        'schedule_generator_snapshots','schedule_coverage_commitments','lead_duty_outcomes','schedule_approvals',
        'schedule_generation_runs','scheduling_preference_requests','inventory_store_access_grants','scheduling_organization_policies')
    report={}
    try:
        with Session(engine) as db:
            stores=tuple(db.scalars(select(m.Store.id)))
            generated=list(db.scalars(select(m.SchedulePeriod).where(m.SchedulePeriod.generated_at.is_not(None),m.SchedulePeriod.week_start_date>=datetime.now(timezone.utc).date()).order_by(m.SchedulePeriod.week_start_date)))
            assert len(generated)==9
            for period in generated:validate_publication_assignments(db,period=period,allowed_store_ids=stores)
            report['all_nine_generated_weeks_pass_authoritative_validation']=True
            person=db.scalar(select(m.Principal).where(m.Principal.active.is_(True),m.Principal.role==m.PrincipalRole.LEAD).order_by(m.Principal.id))
            assert person is not None
            lead=Principal(person.id,person.username,Role.LEAD,None,True)
            first=generated[0]
            approve_schedule(db,principal=lead,schedule_period_id=first.id,expected_version=first.version,allowed_store_ids=stores)
            publish_schedule(db,principal=lead,schedule_period_id=first.id,expected_version=first.version,allowed_store_ids=stores)
            db.commit();report['lead_approval_and_publication_on_clone']='passed'
            report['status_after_next_week_publication']=publication_status(db,today=datetime.now(timezone.utc).date())['color']
            sid=db.scalar(select(m.Store.id).where(m.Store.active.is_(True)).order_by(m.Store.id))
            account=m.Principal(username='release-rehearsal-grant@example.test',password_hash=None,role=m.PrincipalRole.STORE,store_id=sid,active=True)
            db.add(account);db.flush()
            employee=m.Employee(full_name='Synthetic Recovery Evidence',normalized_name='synthetic recovery evidence',principal_id=account.id,active=True,scheduling_active=False)
            db.add(employee);db.flush()
            worker=Principal(account.id,account.username,Role.STORE,sid,True)
            admin_row=db.scalar(select(m.Principal).where(m.Principal.username=='release-rehearsal-admin@example.test'))
            admin=Principal(admin_row.id,admin_row.username,Role.ADMIN,None,True)
            today=datetime.now(timezone.utc).date()
            preference=submit_request(db,principal=worker,start_date=today,end_date=today+timedelta(days=7),preferences={'preferred_stores':[sid]})
            decide_request(db,principal=admin,request_id=preference.id,decision='APPROVED',reason='Synthetic retained recovery acceptance')
            authorize_store(db,principal=lead,employee_id=employee.id,store_id=sid,reason='Synthetic temporary recovery evidence',expires_at=datetime.now(timezone.utc)+timedelta(hours=1),expected_version=employee.working_context_version)
            db.commit()
        # Capture every row, including new 0033 evidence, before bounded recovery.
        projection={k:v for k,v in projections(engine).items() if k in protected}
        before=fingerprints(engine,projection)
        assert all(before[t]['count']>0 for t in ('schedule_approvals','schedule_generation_runs','scheduling_preference_requests','inventory_store_access_grants','schedule_generator_snapshots'))
        env={k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','LANG','LC_ALL'}}
        env.update(DATABASE_URL=url.render_as_string(hide_password=False),ENVIRONMENT='production',DEMO_SEED_ENABLED='false',SCHEMA_REVISION_CHECK_ENABLED='true',SCHEDULE_AUTOMATION_EXECUTION_ENABLED='false',SNAPSHOT_PROVIDER='mock',SQUARE_ACCESS_TOKEN='',INTEGRATION_ENCRYPTION_KEY='',APP_SECRET='synthetic-local-release-only',SESSION_COOKIE_SECURE='false',V2_ENABLED_FEATURES='v2_primary,staff_scheduling_v2',V2_PRINCIPAL_FEATURES='',PYTHONDONTWRITEBYTECODE='1',REHEARSAL_PRIVATE_STATE=str(args.private_dir/'synthetic-probe-state.json'))
        env['PATH']=str(Path(sys.executable).parent)+os.pathsep+env.get('PATH','')
        report['recovery']=probe(args.artifacts/'recovery','recovery',env,args.private_dir)
        assert fingerprints(engine,projection)==before
        report['forward_restart']=probe(args.artifacts/'web-cron','restart',env,args.private_dir)
        assert fingerprints(engine,projection)==before
        report['protected_tables_unchanged_after_recovery_and_forward_restart']=list(projection)
        report['new_evidence_counts']={t:before[t]['count'] for t in ('schedule_approvals','schedule_generation_runs','scheduling_preference_requests','inventory_store_access_grants','schedule_generator_snapshots')}
        report['status']='passed'
        args.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
    finally:engine.dispose()


if __name__=='__main__':main()
