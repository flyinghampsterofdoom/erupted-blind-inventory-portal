"""Disposable-local 0027 -> 0031 -> recovery -> candidate rehearsal; never a deployment tool.

Run from the canonical repository root with PYTHONPATH=.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from app.schema_contract import _alembic_config, assert_supported_schema
from app.security.passwords import hash_password

STEPS = ['20260927_0027', '20260929_0028', '20260928_0028',
         '20260930_0029', '20260930_0030', '20260930_0031']
EVIDENCE = ['principals', 'employees', 'schedule_periods', 'schedule_shifts', 'schedule_attendance_events',
            'schedule_warnings', 'employee_scheduling_profiles', 'scheduling_organization_policies',
            'schedule_generator_snapshots', 'lead_duty_outcomes', 'schedule_coverage_commitments',
            'application_settings', 'password_reset_tokens', 'auth_throttles']


def fingerprint(engine):
    with engine.connect() as c:
        return {t: c.execute(text(f"SELECT md5(coalesce(string_agg(row_to_json(x)::text, E'\\n' ORDER BY row_to_json(x)::text),'')) FROM {t} x")).scalar_one() for t in EVIDENCE}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--rollback-path', required=True, type=Path)
    parser.add_argument('--write-contract', action='store_true', help='Development only: generate the reviewed schema contract')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    admin_url = make_url(os.environ['TEST_POSTGRES_ADMIN_URL'])
    if admin_url.host not in {'localhost', '127.0.0.1', '::1'}:
        raise SystemExit('Rehearsal requires a loopback-only PostgreSQL administrator.')
    name = 'erupted_rollback_rehearsal_' + uuid.uuid4().hex[:12]
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    engine = create_engine(url)
    report = {'migration_steps': STEPS, 'database': name, 'checks': []}
    with admin.connect() as c: c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        config = _alembic_config(url)
        command.upgrade(config, STEPS[0])
        with engine.begin() as c:
            aid = c.execute(text("INSERT INTO principals(username,password_hash,role,active) VALUES ('recovery-admin',:hash,'ADMIN',true) RETURNING id"), {'hash':hash_password('synthetic recovery password')}).scalar_one()
            sid = c.execute(text("INSERT INTO stores(name,square_location_id,active) VALUES ('HWY 99','REHEARSAL',true) RETURNING id")).scalar_one()
            c.execute(text("INSERT INTO principal_permission_overrides(principal_id,permission_key,allowed) VALUES (:a,'ordering.lifecycle.manage',true)"),dict(a=aid))
            c.execute(text("INSERT INTO campaigns(label) VALUES ('Recovery synthetic campaign')"))
            eid = c.execute(text("INSERT INTO employees(full_name,normalized_name,active,scheduling_lead_capable) VALUES ('Synthetic Employee','synthetic employee',true,true) RETURNING id")).scalar_one()
            per = c.execute(text("INSERT INTO schedule_periods(week_start_date,week_end_date,status,revision_number,created_by_principal_id,updated_by_principal_id) VALUES ('2026-09-20','2026-09-26','PUBLISHED',1,:a,:a) RETURNING id"),{'a':aid}).scalar_one()
            shift = c.execute(text("INSERT INTO schedule_shifts(schedule_period_id,employee_id,store_id,shift_date,start_time,end_time,is_lead_of_day,created_by_principal_id,updated_by_principal_id) VALUES (:p,:e,:s,'2026-09-21','09:00','17:00',true,:a,:a) RETURNING id"),dict(p=per,e=eid,s=sid,a=aid)).scalar_one()
        for revision in STEPS[1:]: command.upgrade(config, revision)
        assert_supported_schema(engine)
        # Create defaults through canonical services before installing recovery guards.
        from sqlalchemy.orm import Session
        from app.auth import Principal, Role
        from app.services.v2_scheduling_policy_service import organization_policy
        from app.services.v2_scheduling_assignments_service import get_store_defaults
        from app.services.v2_scheduling_rules_service import upsert_employee_profile
        with Session(engine) as db:
            actor=Principal(id=aid, username='recovery-admin', role=Role.ADMIN, store_id=None, active=True)
            organization_policy(db, principal_id=aid)
            get_store_defaults(db)
            upsert_employee_profile(db, principal=actor, employee_id=eid, home_store_id=sid, target_weekly_hours=0, allowed_store_ids=(sid,))
            db.commit()
        with engine.begin() as c:
            pending=c.execute(text("INSERT INTO principals(username,password_hash,role,store_id,active,recovery_email_confirmed) VALUES ('pending@example.test',NULL,'STORE',:s,true,true) RETURNING id"),dict(s=sid)).scalar_one()

            peid=c.execute(text("INSERT INTO employees(full_name,normalized_name,principal_id,active) VALUES ('Pending Employee','pending employee',:p,true) RETURNING id"),dict(p=pending)).scalar_one()
            c.execute(text("INSERT INTO password_reset_tokens(principal_id,employee_id,login_email,digest,created_at,expires_at) VALUES (:p,:e,'pending@example.test',:d,now(),now()+interval '1 hour')"),dict(p=pending,e=peid,d='a'*64))
            c.execute(text("INSERT INTO auth_throttles(key,window_start,attempts) VALUES (:k,now(),1)"),dict(k='b'*64))
            c.execute(text("INSERT INTO lead_duty_outcomes(business_date,outcome,employee_id,scheduled_shift_id,reason,recorded_by_principal_id) VALUES ('2026-09-21','PERFORMED',:e,:s,'Synthetic retained outcome',:a)"),dict(e=eid,s=shift,a=aid))
            c.execute(text("INSERT INTO schedule_coverage_commitments(schedule_shift_id,employee_id,recorded_by_principal_id,note) VALUES (:s,:e,:a,'Synthetic retained commitment')"),dict(e=eid,s=shift,a=aid))
            payload=json.dumps({'synthetic':'retained generator evidence'})
            c.execute(text("INSERT INTO schedule_generator_snapshots(schedule_period_id,batch_id,sequence,generation_kind,actor_principal_id,origin,version_before,version_after,schema_version,payload,checksum) VALUES (:p,:b,1,'INITIAL',:a,'rehearsal',1,2,1,CAST(:j AS json),:h)"),dict(p=per,b=str(uuid.uuid4()),a=aid,j=payload,h=hashlib.sha256(payload.encode()).hexdigest()))
            c.execute(text("INSERT INTO application_settings(key,values,encrypted_secrets) VALUES ('integration.resend',CAST(:j AS json),'synthetic-retained-ciphertext')"),dict(j=json.dumps({'enabled':False})))
        before = fingerprint(engine)
        if args.write_contract:
            spec=importlib.util.spec_from_file_location('recovery_signature',args.rollback_path/'app/rollback_safety.py')
            mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
            (args.rollback_path/'app/rollback_schema_0031.json').write_text(json.dumps(mod.recovery_schema_signature(engine),indent=2)+'\n')
        env={**os.environ,'DATABASE_URL':url,'PYTHONPATH':str(args.rollback_path),'PYTHONDONTWRITEBYTECODE':'1',
             'ENVIRONMENT':'production','DEMO_SEED_ENABLED':'false','SCHEMA_REVISION_CHECK_ENABLED':'true',
             'SNAPSHOT_PROVIDER':'mock','SQUARE_ACCESS_TOKEN':'','SESSION_COOKIE_SECURE':'false',
             'V2_ENABLED_FEATURES':'staff_scheduling_v2,ordering_intelligence_v2,ordering_v1_links_v2',
             'INTEGRATION_ENCRYPTION_KEY':''}
        probe=Path(__file__).with_name('rollback_probe.py').resolve()
        completed=subprocess.run([sys.executable,str(probe),str(eid),str(shift),str(per)],cwd=args.rollback_path,env=env,text=True,capture_output=True)
        report['probe_stdout']=completed.stdout
        if completed.returncode:
            print(completed.stderr,file=sys.stderr)
            raise RuntimeError('Rollback application probe failed: '+completed.stdout)
        report['checks'].append('recovery startup, authentication, reads, denied writes, ORM/Core guard and cron passed')
        assert fingerprint(engine)==before, 'Recovery altered retained evidence or identities'
        report['checks'].append('all retained evidence/identity fingerprints unchanged')
        assert_supported_schema(engine)
        report['checks'].append('canonical schema gate still accepts 0031 after recovery')
        # A new canonical process must also start against the recovered database.
        env['PYTHONPATH']=str(Path.cwd())
        result=subprocess.run([sys.executable,'-c',"from fastapi.testclient import TestClient; from app.main import app\nwith TestClient(app) as c: assert c.get('/login').status_code == 200\nprint('candidate restart passed')"],cwd=Path.cwd(),env=env,text=True,capture_output=True)
        if result.returncode: raise RuntimeError(result.stderr)
        assert fingerprint(engine)==before
        report['checks'].append('candidate restart after rollback passed; schema and evidence unchanged')
        report['status']='passed'
    finally:
        engine.dispose()
        with admin.connect() as c: c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
        report['disposable_database_removed']=True
        args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__': main()
