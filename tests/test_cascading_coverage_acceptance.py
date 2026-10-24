"""Test-only investigation: real sessions, coverage routes, work writes and reversals."""
from contextlib import ExitStack
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app import models as m
from app.config import settings
from app.security.passwords import hash_password
from app.services.v2_scheduling_lead_duty_service import business_today
from test_v2_primary import primary, PASSWORD, login, post
from test_v2_scheduling_foundation import scheduling_db


def _seed(primary):
    lead_id = primary.account('LEAD', linked=False)
    with primary.Session() as db:
        day = business_today(db)
        week = day - timedelta(days=(day.weekday() + 1) % 7)
        db.get(m.Store, primary.ids['north']).name = 'Longview'
        db.get(m.Store, primary.ids['south']).name = 'Andresen'
        people = {'Micah': db.get(m.Employee, primary.ids['alex']),
                  'Cameron': db.get(m.Employee, primary.ids['blair'])}
        people['Mikey'] = m.Employee(full_name='Mikey', normalized_name='mikey', active=True)
        db.add(people['Mikey']); db.flush()
        account_ids = {}
        for name, employee in people.items():
            employee.full_name = name
            account = m.Principal(username=name.lower()+'@cascade.test', role=m.PrincipalRole.STORE,
                store_id=primary.ids['north' if name == 'Micah' else 'south'],
                active=True, password_hash=hash_password(PASSWORD))
            db.add(account); db.flush()
            employee.principal_id = account.id
            account_ids[name] = account.id
        period = m.SchedulePeriod(week_start_date=week, week_end_date=week+timedelta(days=6),
            status=m.SchedulePeriodStatus.PUBLISHED, revision_number=1,
            published_at=datetime.now(timezone.utc), created_by_principal_id=primary.manager.id,
            updated_by_principal_id=primary.manager.id)
        db.add(period); db.flush()
        shifts = {}
        for name, store in [('Micah', 'north'), ('Cameron', 'south')]:
            row = m.ScheduleShift(schedule_period_id=period.id, employee_id=people[name].id,
                store_id=primary.ids[store], shift_date=day, start_time=time(8,45), end_time=time(22,15),
                unpaid_break_minutes=0, created_by_principal_id=primary.manager.id,
                updated_by_principal_id=primary.manager.id)
            db.add(row); db.flush(); shifts[name] = row.id
        for store in ('north', 'south'):
            for weekday in range(7):
                db.add(m.StoreOperatingHour(store_id=primary.ids[store], day_of_week=weekday,
                    opening_time=time(9), closing_time=time(22,15),
                    created_by_principal_id=primary.manager.id, updated_by_principal_id=primary.manager.id))
        db.commit()
        original = {sid: {column.key: str(getattr(db.get(m.ScheduleShift, sid), column.key))
                          for column in m.ScheduleShift.__table__.columns} for sid in shifts.values()}
        return dict(lead_id=lead_id, day=day, week=week, shifts=shifts, original=original,
                    employees={name: employee.id for name, employee in people.items()}, accounts=account_ids)


def _ordinary_coverage_payload(employee_id):
    """Execute the form's real serializer, then exercise the resulting HTTP path."""
    values = dict(event_type='', commitment_employee_id=str(employee_id),
        coverage_start_time='08:45', coverage_end_time='22:15', coverage_break_minutes='0', note='')
    program = """
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync('app/static/v2/scheduling.js', 'utf8');
const helper = source.slice(source.indexOf('  function attendancePayload('), source.indexOf('  function updateAttendanceFields('));
const context = {}; vm.createContext(context); vm.runInContext(helper, context);
const values = JSON.parse(process.argv[1]);
console.log(JSON.stringify(context.attendancePayload({get: key => values[key]})));
"""
    return json.loads(subprocess.check_output(['node','-e',program,json.dumps(values)],text=True))


@pytest.mark.parametrize('step2_mode', ['coverage_only_api', 'ordinary_form_payload'])
@pytest.mark.parametrize('cancel_order', ['Mikey_then_Cameron', 'Cameron_then_Mikey'])
def test_cascading_coverage_exact_scenario(primary, monkeypatch, step2_mode, cancel_order):
    scenario = _seed(primary)
    # Only the live access clock is fixed at noon, inside the exact requested
    # 08:45–22:15 shift. Authentication, identity, authorization and SQL stay real.
    from app.services import inventory_working_store_service as access
    noon = datetime.combine(scenario['day'], time(12), ZoneInfo('America/Los_Angeles'))
    class AccessClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return noon.astimezone(tz) if tz is not None else noon.replace(tzinfo=None)
    monkeypatch.setattr(access, 'datetime', AccessClock)
    checks, states = [], []
    def check(label, actual, expected):
        checks.append(dict(check=label, actual=actual, expected=expected, passed=actual == expected))
    def require(response, expected):
        assert response.status_code == expected, response.text
        return response
    def change(sid, payload):
        return require(primary.client.post(f'/v2/scheduling/api/shifts/{sid}/attendance', json=payload,
            headers={'X-CSRF-Token': primary.client.cookies.get('csrf_token')}), 201).json()
    def cancel(cid):
        return require(primary.client.post(f'/v2/scheduling/api/coverage-commitments/{cid}/void', json={},
            headers={'X-CSRF-Token': primary.client.cookies.get('csrf_token')}), 200).json()
    lv, an = primary.ids['north'], primary.ids['south']
    eids, sids = scenario['employees'], scenario['shifts']
    require(login(primary), 303)
    with ExitStack() as stack:
        clients, cookies = {}, {}
        for n, name in enumerate(eids):
            client = stack.enter_context(TestClient(primary.app, follow_redirects=False,
                client=('127.0.0.1', 51000+n)))
            client.get('/login')
            require(post(client, '/login', username=name.lower()+'@cascade.test', password=PASSWORD), 303)
            clients[name] = client
            cookies[name] = client.cookies.get(settings.session_cookie_name)

        def capture(stage, board, hours, stores, coverage_count, overlap=None):
            actual_hours = {name: next(row for row in board['employees'] if row['id'] == eid)
                            ['effective_scheduled_hours'] for name, eid in eids.items()}
            contexts = {name: require(client.get('/session-status'), 200).json()
                        for name, client in clients.items()}
            overlap_warnings = [row for row in board['warnings']
                if row['employee_id'] == eids['Cameron'] and row['type'] == 'EMPLOYEE_OVERLAP']
            with primary.Session() as db:
                events = list(db.scalars(select(m.ScheduleAttendanceEvent).where(
                    m.ScheduleAttendanceEvent.voided_at.is_(None))))
                attendance = {name: [event.event_type.value for event in events
                    if event.original_employee_id == eid] for name, eid in eids.items()}
            snapshot = dict(stage=stage, hours=actual_hours,
                operational_stores={name: row['store_id'] for name, row in contexts.items()},
                overlap_warnings=overlap_warnings, attendance=attendance,
                cards=[{key: row.get(key) for key in ('id','employee_id','store_id','is_coverage',
                    'source_shift_id','effective_hours','original_scheduled_hours','actual_hours')}
                    for row in board['shifts']])
            states.append(snapshot)
            check(stage+': effective employee hours', actual_hours, hours)
            check(stage+': effective total', board['summary']['effective_scheduled_hours'], sum(hours.values()))
            check(stage+': live store access', snapshot['operational_stores'], stores)
            check(stage+': coverage cards', sum(row['is_coverage'] for row in board['shifts']), coverage_count)
            check(stage+': original cards retained', sorted(row['id'] for row in board['shifts']
                if not row['is_coverage']), sorted(sids.values()))
            if overlap is not None:
                check(stage+': overlap warning agrees with effective work', bool(overlap_warnings), overlap)
            for name, client in clients.items():
                api = require(client.get('/v2/scheduling/api/own-schedule'), 200).json()
                check(stage+': '+name+' own API hours', sum(row['effective_scheduled_hours']
                    for row in api['assignments']), hours[name])
                check(stage+': '+name+' session unchanged', client.cookies.get(settings.session_cookie_name), cookies[name])
                # Never emit the synthetic session token into evidence.
                checks[-1]['actual'] = checks[-1]['expected'] = '[unchanged]' if checks[-1]['passed'] else '[changed]'
                status = client.get('/store/daily-count').status_code
                check(stage+': '+name+' inventory authorization', status, 200 if stores[name] is not None else 409)
            return contexts

        board = require(primary.client.get('/v2/scheduling/api/board', params={'start': str(scenario['week'])}), 200).json()
        initial = capture('initial', board, {'Micah':13.5,'Cameron':13.5,'Mikey':0},
            {'Micah':lv,'Cameron':an,'Mikey':None}, 0, False)
        page = require(clients['Cameron'].get('/store/daily-chore-sheet'), 200)
        andresen_sheet = int(re.search(r'/daily-chore-sheet/(\d+)/save', page.text).group(1))

        first = change(sids['Micah'], {'event_type':'CALLED_OUT','commitment_employee_id':eids['Cameron']})
        check('step1: overlap warns without blocking', 'OVERLAPPING_SHIFT' in {
            row['code'] for row in first['coverage_warnings']}, True)
        after_one = capture('Micah_to_Cameron', first['board'], {'Micah':0,'Cameron':27,'Mikey':0},
            {'Micah':None,'Cameron':lv,'Mikey':None}, 1)
        longview_card = next(row for row in first['board']['shifts'] if row['is_coverage'])
        longview_commitment = longview_card['coverage_assignment_id']
        page = require(clients['Cameron'].get('/store/daily-chore-sheet'), 200)
        longview_sheet = int(re.search(r'/daily-chore-sheet/(\d+)/save', page.text).group(1))
        require(post(clients['Cameron'], f'/store/daily-chore-sheet/{longview_sheet}/save',
            working_context=after_one['Cameron']['working_context'], employee_name='Cameron'),303)
        check('step1: superseded Andresen stale write rejected', post(clients['Cameron'],
            f'/store/daily-chore-sheet/{andresen_sheet}/save',
            working_context=initial['Cameron']['working_context'],employee_name='Stale').status_code,409)

        second_payload = {'commitment_employee_id':eids['Mikey']}
        if step2_mode == 'ordinary_form_payload':
            second_payload = _ordinary_coverage_payload(eids['Mikey'])
        second = change(sids['Cameron'], second_payload)
        after_two = capture('complete_chain', second['board'], {'Micah':0,'Cameron':13.5,'Mikey':13.5},
            {'Micah':None,'Cameron':lv,'Mikey':an}, 2, False)
        andresen_card = next(row for row in second['board']['shifts'] if row['is_coverage']
                             and row['employee_id'] == eids['Mikey'])
        andresen_commitment = andresen_card['coverage_assignment_id']
        check('step2: each store effectively staffed once',
            {row['store_id']:row['assigned_shift_count'] for row in second['board']['summary']['stores']},
            {lv:1,an:1})
        check('step2: Mikey card linked to Cameron source', andresen_card['source_shift_id'],sids['Cameron'])
        check('step2: Cameron covered without false absence', states[-1]['attendance']['Cameron'], [])
        for name, sid in [('Cameron',longview_sheet),('Mikey',andresen_sheet)]:
            require(post(clients[name], f'/store/daily-chore-sheet/{sid}/save',
                working_context=after_two[name]['working_context'],employee_name=name),303)
            for path in ('/management/sessions','/management/users'):
                check('step2: '+name+' no admin '+path, clients[name].get(path).status_code,403)
        for name, other_sheet in [('Cameron',andresen_sheet),('Mikey',longview_sheet)]:
            check('step2: '+name+' unrelated store write denied', post(clients[name],
                f'/store/daily-chore-sheet/{other_sheet}/save',
                working_context=after_two[name]['working_context'],employee_name='Denied').status_code,403)
        with primary.Session() as db:
            check('step2: written Longview work association',db.get(m.DailyChoreSheet,longview_sheet).store_id,lv)
            check('step2: written Andresen work association',db.get(m.DailyChoreSheet,andresen_sheet).store_id,an)
        company = require(clients['Mikey'].get('/v2/scheduling/my-schedule',params={'start':str(scenario['week'])}),200).text
        for name, cid in [('Cameron',longview_card['id']),('Mikey',andresen_card['id'])]:
            check('step2: '+name+' coverage card rendered',f'id="company-shift-{cid}"' in company,True)
        check('step2: purple coverage styling',company.count('company-schedule__shift--coverage'),2)
        check('step2: both original exception markers',company.count('shift-card-exception'),2)

        if cancel_order == 'Mikey_then_Cameron':
            reverse_one = cancel(andresen_commitment)
            capture('cancel_Mikey_first',reverse_one['board'],{'Micah':0,'Cameron':27,'Mikey':0},
                {'Micah':None,'Cameron':lv,'Mikey':None},1,True)
            reverse_two = cancel(longview_commitment)
        else:
            reverse_one = cancel(longview_commitment)
            capture('cancel_Cameron_first',reverse_one['board'],{'Micah':0,'Cameron':0,'Mikey':13.5},
                {'Micah':None,'Cameron':None,'Mikey':an},1,False)
            reverse_two = cancel(andresen_commitment)
        final = capture('both_cancelled',reverse_two['board'],{'Micah':0,'Cameron':13.5,'Mikey':0},
            {'Micah':None,'Cameron':an,'Mikey':None},0,False)
        for name, sid in [('Cameron',longview_sheet),('Mikey',andresen_sheet)]:
            check('final: '+name+' former coverage store write denied',post(clients[name],
                f'/store/daily-chore-sheet/{sid}/save',working_context=final[name]['working_context'],
                employee_name='No longer assigned').status_code,403 if name=='Cameron' and final[name]['store_id'] is not None else 409)
        with primary.Session() as db:
            actual = {sid:{column.key:str(getattr(db.get(m.ScheduleShift,sid),column.key))
                for column in m.ScheduleShift.__table__.columns} for sid in sids.values()}
            check('final: original published rows unchanged',actual,scenario['original'])
            commitments = list(db.scalars(select(m.ScheduleCoverageCommitment)))
            check('final: two coverage records retained',len(commitments),2)
            check('final: both coverage records cancelled',all(row.voided_at is not None for row in commitments),True)
            check('final: no mandatory note',all(row.note=='' for row in commitments),True)
            check('final: coverage actor recorded',all(row.recorded_by_principal_id==scenario['lead_id'] for row in commitments),True)
            for action in ('V2:SCHEDULING:COVERAGE_COMMITTED','V2:SCHEDULING:COVERAGE_COMMITMENT_CANCELED'):
                audits=list(db.scalars(select(m.AuditLog).where(m.AuditLog.action==action)))
                check('final: '+action+' audit count',len(audits),2)
                check('final: '+action+' actor and shift audit',all(a.actor_principal_id==scenario['lead_id']
                    and a.meta['metadata']['shift_id'] in sids.values() for a in audits),True)
                check('final: '+action+' original replacement and store audit',sorted([
                    a.meta['metadata'][key] for key in ('shift_id','original_employee_id','replacement_employee_id','store_id')]
                    for a in audits),sorted([[sids['Micah'],eids['Micah'],eids['Cameron'],lv],
                        [sids['Cameron'],eids['Cameron'],eids['Mikey'],an]]))
                check('final: '+action+' timestamps and exact interval',all(a.meta.get('occurred_at')
                    and a.meta['metadata']['start_time']=='08:45:00'
                    and a.meta['metadata']['end_time']=='22:15:00' for a in audits),True)
            active=list(db.scalars(select(m.ScheduleAttendanceEvent).where(m.ScheduleAttendanceEvent.voided_at.is_(None))))
            check('final: Micah call-out retained',any(row.original_employee_id==eids['Micah']
                and row.event_type==m.AttendanceEventType.CALLED_OUT for row in active),True)
            check('final: no invented actual worker assertions',any(row.event_type==m.AttendanceEventType.COVERED_SHIFT for row in active),False)
            check('final: employee roles unchanged',{name:db.get(m.Principal,pid).role.value
                for name,pid in scenario['accounts'].items()},dict.fromkeys(eids,'STORE'))
            check('final: home store associations unchanged',{name:db.get(m.Principal,pid).store_id
                for name,pid in scenario['accounts'].items()},{'Micah':lv,'Cameron':an,'Mikey':an})
        evidence=dict(mode=step2_mode,cancel_order=cancel_order,shift_hours=13.5,
            interval='08:45–22:15',checks=checks,states=states)
        target=Path(os.environ.get('CASCADE_EVIDENCE_DIR','/tmp/erupted-cascade-evidence'))
        target.mkdir(parents=True,exist_ok=True)
        (target/f'{step2_mode}-{cancel_order}.json').write_text(json.dumps(evidence,indent=2,default=str))
        failures=[row for row in checks if not row['passed']]
        assert not failures,json.dumps(failures,indent=2,default=str)
