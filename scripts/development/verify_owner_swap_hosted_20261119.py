"""Approved hosted acceptance: TEMP-only mutations and existing-session real GETs.

The coordinator supplies this source in memory using python -I -B and pins the
final release SHA. This script creates no host files, credentials or sessions.
Temporary DDL precedes application services; every service transaction enforces
read-only persistent tables and uses independent TEMP sequences/foreign keys.
"""
import argparse
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from time import perf_counter
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, '/opt/render/project/src')
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from app import models as m
from app.auth import Principal, Role
from app.config import settings
from app.services.scheduling_completion_service import approve_and_publish_schedule
from app.services.scheduling_owner_review import OwnerScheduleReview
from app.services.v2_scheduling_exception_service import (
    coverage_assignments, effective_shift_segments, record_exception,
)
from app.services.v2_scheduling_policy_service import scheduled_weekly_hours
from app.services.v2_scheduling_service import SchedulingConflict, SchedulingValidationError
from app.services.v2_scheduling_swap_service import change_assignments

PROTECTED = (m.Employee, m.EmployeeCompensationRate, m.SchedulePeriod,
    m.ScheduleShift, m.ScheduleAttendanceEvent, m.ScheduleCoverageCommitment,
    m.ScheduleApproval, m.ScheduleGeneratorSnapshot)


def fingerprint(engine):
    result = {}
    with engine.connect() as c:
        c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        assert c.scalar(text('SHOW transaction_read_only')) == 'on'
        quote = c.dialect.identifier_preparer.quote
        for model in PROTECTED:
            name = model.__tablename__
            digest, count = hashlib.sha256(), 0
            for raw in c.scalars(text('SELECT row_to_json(t)::text FROM public.' + quote(name) + ' t ORDER BY id')):
                digest.update(raw.encode() + b'\n'); count += 1
            result[name] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


def isolated_connection(engine):
    c = engine.connect()
    try:
        names = sorted(t.name for t in m.Base.metadata.tables.values() if t.schema is None)
        quote = c.dialect.identifier_preparer.quote
        foreign_keys = list(c.execute(text("""SELECT r.relname,pg_get_constraintdef(k.oid)
            FROM pg_constraint k JOIN pg_class r ON r.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=r.relnamespace
            WHERE n.nspname='public' AND k.contype='f'""")))
        defaults = list(c.execute(text("""SELECT r.relname,a.attname,pg_get_expr(d.adbin,d.adrelid)
            FROM pg_attrdef d JOIN pg_class r ON r.oid=d.adrelid
            JOIN pg_namespace n ON n.oid=r.relnamespace
            JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=d.adnum
            WHERE n.nspname='public'""")))
        # Structure only: no production rows are copied. All identifiers below
        # come from registered SQLAlchemy metadata or PostgreSQL catalogs.
        for name in names:
            c.execute(text('CREATE TEMP TABLE ' + quote(name) +
                ' (LIKE public.' + quote(name) + ' INCLUDING ALL) ON COMMIT PRESERVE ROWS'))
        c.execute(text('SET search_path TO pg_temp,public'))
        for index, (table, column, expression) in enumerate(defaults):
            if table in names and 'nextval(' in expression:
                sequence = 'owner_swap_probe_sequence_' + str(index)
                c.execute(text('CREATE TEMP SEQUENCE ' + sequence))
                c.execute(text('ALTER TABLE pg_temp.' + quote(table) + ' ALTER COLUMN ' + quote(column) +
                    " SET DEFAULT nextval('pg_temp." + sequence + "'::regclass)"))
        for table, definition in foreign_keys:
            if table in names:
                c.execute(text('ALTER TABLE pg_temp.' + quote(table) + ' ADD ' +
                    definition.replace('REFERENCES public.', 'REFERENCES pg_temp.')))
        # No copied serial default may retain a dependency on a public sequence.
        assert c.scalar(text("""SELECT count(*) FROM pg_attrdef a
            JOIN pg_class t ON t.oid=a.adrelid
            JOIN pg_depend d ON d.classid='pg_attrdef'::regclass AND d.objid=a.oid
            JOIN pg_class s ON s.oid=d.refobjid AND s.relkind='S'
            WHERE t.relnamespace=pg_my_temp_schema() AND s.relpersistence<>'t'""")) == 0
        c.commit()
        c.execute(text('SET SESSION default_transaction_read_only=on')); c.commit()
        assert c.scalar(text('SHOW default_transaction_read_only')) == 'on'
        assert c.scalar(text('SHOW transaction_read_only')) == 'on'
        for name in names:
            assert c.scalar(text("SELECT relpersistence FROM pg_class WHERE oid=to_regclass(:name)"),
                {'name': name}) == 't'
        c.commit()
        return c
    except BaseException:
        c.close()
        raise


def assignment_shape(db):
    return {s.id: {col.name: getattr(s, col.name) for col in m.ScheduleShift.__table__.columns}
        for s in db.scalars(select(m.ScheduleShift))}


def fixture(db, start):
    account = m.Principal(username='synthetic-hosted-owner', password_hash='unused',
        role=m.PrincipalRole.ADMIN, active=True)
    db.add(account); db.flush()
    actor = Principal(account.id, account.username, Role.ADMIN, None, True)
    manager = Principal(account.id, account.username, Role.MANAGER, None, True)
    stores = [m.Store(name='Synthetic hosted store ' + str(i),
        square_location_id='SYNTH-HOSTED-' + str(i), active=True) for i in range(4)]
    people = [m.Employee(full_name='Synthetic hosted worker ' + str(i),
        normalized_name='synthetic hosted worker ' + str(i), active=True,
        scheduling_active=True, visible_to_leads=True, scheduling_lead_capable=True)
        for i in range(8)]
    db.add_all(stores + people); db.flush()
    for person in people:
        db.add(m.EmployeeSchedulingProfile(employee_id=person.id, active=True,
            target_shifts_per_week=3, minimum_days_off_after_max_block=0,
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
    for store in stores:
        for weekday in range(7):
            db.add(m.StoreOperatingHour(store_id=store.id, day_of_week=weekday,
                opening_time=time(9, 15), closing_time=time(10), active=True,
                created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
            db.add(m.CoverageRequirement(store_id=store.id, day_of_week=weekday,
                start_time=time(9, 15), end_time=time(10), minimum_employee_count=1,
                active=True, created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
    periods, rows = [], {}
    for week in range(3):
        per = m.SchedulePeriod(week_start_date=start+timedelta(weeks=week),
            week_end_date=start+timedelta(weeks=week, days=6),
            status=m.SchedulePeriodStatus.PUBLISHED, revision_number=1,
            published_at=datetime.now(timezone.utc), created_by_principal_id=actor.id,
            updated_by_principal_id=actor.id)
        db.add(per); db.flush(); periods.append(per)
        rows[per.id] = []
        for day in range(7):
            for index, store in enumerate(stores):
                shift = m.ScheduleShift(schedule_period_id=per.id,
                    employee_id=people[(day*4+index)%len(people)].id,
                    store_id=store.id, shift_date=per.week_start_date+timedelta(days=day),
                    start_time=time(9), end_time=time(10), unpaid_break_minutes=0,
                    is_lead_of_day=index==0, created_by_principal_id=actor.id,
                    updated_by_principal_id=actor.id)
                db.add(shift); rows[per.id].append(shift)
    db.commit()
    return actor, manager, tuple(s.id for s in stores), people, periods, rows


def measured(connection, operation):
    counts, sql_ms = [0], [0.0]
    def begin(conn, cursor, statement, parameters, context, many):
        counts[0] += 1; context._owner_probe_start = perf_counter()
    def end(conn, cursor, statement, parameters, context, many):
        sql_ms[0] += (perf_counter()-context._owner_probe_start)*1000
    event.listen(connection, 'before_cursor_execute', begin)
    event.listen(connection, 'after_cursor_execute', end)
    began = perf_counter()
    try:
        result = operation()
        return result, {'elapsed_ms': round((perf_counter()-began)*1000, 3),
            'sql_count': counts[0], 'sql_ms': round(sql_ms[0], 3),
            'measurement': 'synthetic-hosted TEMP service plus commit; excludes browser/WAN and durable business WAL'}
    finally:
        event.remove(connection, 'before_cursor_execute', begin)
        event.remove(connection, 'after_cursor_execute', end)


def synthetic_checks(engine, now):
    from app.services.employee_schedule_service import company_published_assignments
    start = now.date()+timedelta(days=14)
    start -= timedelta(days=(start.weekday()+1)%7)
    c = isolated_connection(engine)
    try:
        with Session(c, expire_on_commit=False) as db:
            actor, manager, stores, people, periods, rows = fixture(db, start)
            report = {'stores': 4, 'employees': 8, 'periods': 3, 'shifts': 84}
            original = assignment_shape(db)
            employee_count = db.scalar(select(func.count()).select_from(m.Employee))
            payroll_count = db.scalar(select(func.count()).select_from(m.EmployeeCompensationRate))
            source, other = rows[periods[2].id][:2]
            hours_before = scheduled_weekly_hours(db, employee_id=source.employee_id, shift_date=source.shift_date)
            def owner_save():
                record_exception(db, principal=actor, shift_id=source.id,
                    event_type=m.AttendanceEventType.CALLED_OUT,
                    commitment_owner_principal_id=actor.id, today=now.date())
                db.commit()
            _, timing = measured(c, owner_save)
            commitment, = coverage_assignments(db, source)
            assert commitment.employee_id is None and commitment.owner_principal_id == actor.id
            effective, = effective_shift_segments(db, source)
            assert effective.employee_id is None and effective.owner_principal_id == actor.id
            assert not effective.is_lead_of_day and not effective.is_shift_lead
            assert hours_before-scheduled_weekly_hours(db, employee_id=source.employee_id,
                shift_date=source.shift_date) == Decimal('1.00')
            public_projection = company_published_assignments(db, start_date=source.shift_date, end_date=source.shift_date)
            relief, = [r for r in public_projection if r.is_coverage]
            assert relief.employee_id is None and relief.owner_name == actor.username
            assert relief.effective_scheduled_hours == 0 and not relief.transfer_eligible
            counts = (db.scalar(select(func.count()).select_from(m.ScheduleCoverageCommitment)),
                db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent)))
            try:
                record_exception(db, principal=actor, shift_id=other.id,
                    commitment_owner_principal_id=actor.id, today=now.date())
                raise AssertionError('Overlapping Owner relief was accepted')
            except SchedulingValidationError:
                db.commit()
            assert counts == (db.scalar(select(func.count()).select_from(m.ScheduleCoverageCommitment)),
                db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent)))
            record_exception(db, principal=actor, shift_id=source.id, cancel_coverage=True, today=now.date())
            db.commit()
            assert coverage_assignments(db, source) == [] and commitment.voided_at is not None
            assert db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent)) == 1
            assert assignment_shape(db) == original
            assert db.scalar(select(func.count()).select_from(m.Employee)) == employee_count
            assert db.scalar(select(func.count()).select_from(m.EmployeeCompensationRate)) == payroll_count
            assert db.scalar(select(m.Employee.id).where(m.Employee.principal_id==actor.id)) is None
            report['owner_coverage'] = dict(status='PASS', **timing, save_cancel_overlap=True,
                published_intent_unchanged=True, absence_and_coverage_history_retained=True,
                no_employee_or_payroll_identity=True, employee_work_credit=0)
            metrics = []
            for kind, principal, per, pair, review in (
                ('SHIFT', actor, periods[0], (rows[periods[0].id][0],rows[periods[0].id][4]), True),
                ('STORE', manager, periods[1], tuple(rows[periods[1].id][:2]), False)):
                before = assignment_shape(db)
                left, right = pair
                version = per.version
                def swap():
                    result = change_assignments(db, principal=principal, shift_id=left.id,
                        other_shift_id=right.id, kind=kind, reason='Synthetic hosted release acceptance',
                        expected_versions={str(per.id):version}, allowed_store_ids=stores, now=now,
                        owner_review=OwnerScheduleReview(actor,'synthetic-hosted',{},'') if review else None)
                    db.commit()
                    return result
                result, timing = measured(c, swap)
                after = assignment_shape(db)
                assert all(after[sid]==old for sid,old in before.items())
                current = db.get(m.SchedulePeriod, result['period_id'])
                assert current.status == (m.SchedulePeriodStatus.DRAFT if review else m.SchedulePeriodStatus.PUBLISHED)
                assert per.status == (m.SchedulePeriodStatus.PUBLISHED if review else m.SchedulePeriodStatus.ARCHIVED)
                changed = {s.source_shift_id:s for s in db.scalars(select(m.ScheduleShift).where(
                    m.ScheduleShift.schedule_period_id==current.id))}
                if kind == 'SHIFT':
                    assert changed[left.id].employee_id==before[right.id]['employee_id']
                    assert changed[right.id].employee_id==before[left.id]['employee_id']
                else:
                    assert changed[left.id].store_id==before[right.id]['store_id']
                    assert changed[right.id].store_id==before[left.id]['store_id']
                    assert db.scalar(select(func.count()).select_from(m.ScheduleApproval).where(
                        m.ScheduleApproval.schedule_period_id==current.id)) == 1
                    assert db.scalar(select(func.count()).select_from(m.ScheduleGeneratorSnapshot).where(
                        m.ScheduleGeneratorSnapshot.schedule_period_id==current.id,
                        m.ScheduleGeneratorSnapshot.generation_kind.in_(('APPROVAL','PUBLICATION')))) == 2
                stable = assignment_shape(db)
                try:
                    change_assignments(db, principal=principal, shift_id=result['shift_id'],
                        other_shift_id=changed[right.id].id, kind=kind, reason='Reject stale synthetic swap',
                        expected_versions={str(current.id):current.version-1}, allowed_store_ids=stores,
                        now=now, owner_review=OwnerScheduleReview(actor,'synthetic-hosted',{},'') if review else None)
                    raise AssertionError('Stale swap was accepted')
                except SchedulingConflict:
                    db.commit()
                assert assignment_shape(db)==stable
                metrics.append(dict(kind=kind, actor='Owner' if review else 'Manager', status='PASS',
                    stale_rejected=True, original_rows_preserved=True, publication_behavior_preserved=True, **timing))
            report['swaps'] = metrics
            # Explicit invalid second side: the savepoint must undo both sides,
            # even if a caller commits after catching the validation error.
            per = db.get(m.SchedulePeriod, result['period_id'])
            target = db.get(m.ScheduleShift, result['shift_id'])
            stable, version = assignment_shape(db), per.version
            try:
                change_assignments(db, principal=manager, shift_id=target.id,
                    other_shift_id=target.id, kind='STORE', reason='Reject duplicate second side',
                    expected_versions={str(per.id):version}, allowed_store_ids=stores, now=now)
                raise AssertionError('Invalid second side was accepted')
            except SchedulingValidationError:
                db.commit()
            assert assignment_shape(db)==stable and per.version==version
            assert c.scalar(text('SHOW transaction_read_only')) == 'on'
            report['invalid_second_side_atomic'] = True
            report['persistent_tables_read_only_during_services'] = True
            return report
    finally:
        c.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authenticated_gets(engine, origin, now, revision):
    # Existing ordinary Owner session only. Never print/return its token or
    # mint a token, create an account, submit a form, or call a mutation route.
    with engine.connect() as c:
        c.execute(text('SET TRANSACTION READ ONLY'))
        session = c.execute(text("""SELECT w.session_token FROM public.web_sessions w
            JOIN public.principals p ON p.id=w.principal_id
            LEFT JOIN public.employees e ON e.principal_id=p.id
            WHERE p.id=:owner AND p.active AND p.role='ADMIN' AND (e.id IS NULL OR e.active)
              AND w.revoked_at IS NULL AND w.expires_at>:expiry
            ORDER BY w.last_seen_at DESC LIMIT 1"""), {'expiry': now+timedelta(minutes=2), 'owner':settings.purchasing_owner_principal_id}).scalar_one_or_none()
        shift = c.execute(text("""SELECT s.id,p.week_start_date FROM public.schedule_shifts s
            JOIN public.schedule_periods p ON p.id=s.schedule_period_id
            WHERE p.status='PUBLISHED' AND NOT p.is_historical AND p.cleared_at IS NULL
              AND s.employee_id IS NOT NULL AND s.shift_date>=:today
            ORDER BY s.shift_date,s.id LIMIT 1"""), {'today': now.date()}).first()
    assert session is not None, 'Existing unexpired Owner WebSession required; no session was created'
    assert shift is not None, 'Existing current/future published employee shift required for GET verification'
    opener = urllib.request.build_opener(NoRedirect)
    cookie = settings.session_cookie_name + '=' + session
    results = {}
    paths = {'week': '/v2/scheduling/week?start='+str(shift.week_start_date),
        'coverage_candidates': '/v2/scheduling/api/shifts/'+str(shift.id)+'/coverage-candidates',
        'runtime':'/management/deployment/runtime', 'readiness':'/management/deployment/readiness',
        'purchasing':'/purchasing-v4'}
    for label, path in paths.items():
        request = urllib.request.Request(origin+path, method='GET', headers={
            'Cookie':cookie, 'User-Agent':'Erupted approved scheduling GET verification'})
        started = perf_counter()
        with opener.open(request, timeout=40) as response:
            assert response.status==200
            body=response.read()
        if label=='week':
            assert re.search(rb'value="owner:[0-9]+"', body), 'Owner coverage option absent from real board'
        elif label=='coverage_candidates':
            data=json.loads(body)
            assert data.get('owners') and all(isinstance(r['id'],int) for r in data['owners'])
        elif label=='runtime':
            data=json.loads(body)
            assert data['revision']==revision and data['status']=='VERIFIED'
            assert data['files_still_match'] and data['schema']=='20261119_0057'
            assert data['principal_id']==settings.purchasing_owner_principal_id
        elif label=='readiness':
            data=json.loads(body)
            assert data['status']=='READY' and all(data['dependencies'].values())
            assert data['schema']=='20261119_0057' and data['web']['revision']==revision
        results[label]={'status':200,'owner_option_present':label in ('week','coverage_candidates'),
            'elapsed_ms':round((perf_counter()-started)*1000,3)}
    return results


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--origin', default='https://erupted-blind-inventory-portal.onrender.com')
    args=parser.parse_args()
    assert re.fullmatch('[0-9a-f]{40}',args.expected_revision)
    assert args.origin=='https://erupted-blind-inventory-portal.onrender.com'
    revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    assert revision==args.expected_revision
    identity=json.loads(Path('release-identity.json').read_text())
    assert identity['revision']==revision
    report={'revision':revision,'source_tree_sha256':identity['source_tree_sha256'],
        'measurement_environment':'synthetic-hosted TEMP functional acceptance',
        'persistent_business_mutations':False,'cron_invoked':False,'new_auth_tokens':False}
    engine=create_engine(settings.database_url_normalized,poolclass=NullPool,
        connect_args={'options':'-c statement_timeout=20000 -c lock_timeout=2000 -c timezone=UTC'})
    stage='schema-and-protected-before'
    try:
        with engine.connect() as c:
            c.execute(text('SET TRANSACTION READ ONLY'))
            assert c.scalar(text('SELECT version_num FROM public.alembic_version'))=='20261119_0057'
        before=fingerprint(engine);report['protected_before']=before
        stage='temporary-owner-and-swaps'
        report['synthetic']=synthetic_checks(engine,datetime.now(timezone.utc))
        stage='existing-owner-authenticated-gets'
        report['authenticated_gets']=authenticated_gets(engine,args.origin,datetime.now(timezone.utc),revision)
        report['status']='PASS'
    except Exception as exc:
        # Never include arbitrary exception text, HTTP bodies or credentials.
        report.update(status='FAIL',failure_stage=stage,error_type=type(exc).__name__)
    finally:
        try:
            after=fingerprint(engine);report['protected_after']=after
            report['persistent_fingerprints_unchanged']=after==report.get('protected_before')
            if not report['persistent_fingerprints_unchanged']:
                report.update(status='FAIL',failure_stage='persistent-fingerprint-comparison')
        except Exception as exc:
            report.update(status='FAIL',failure_stage='protected-after',error_type=type(exc).__name__)
        engine.dispose()
    print('ERUPTED_OWNER_SWAP_HOSTED '+json.dumps(report,default=str),flush=True)
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
