"""One board and schedule-derived inventory access; synthetic local PostgreSQL only."""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app import models as m
from app.auth import Principal, Role
from app.services.inventory_working_store_service import working_context, assert_inventory_store, WorkingStoreConflict
from app.services.v2_scheduling_service import approve_schedule, publish_schedule
from app.services.v2_scheduling_exception_service import record_exception
from app.services.v2_scheduling_swap_service import change_assignments
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_prebeta import ready_period
from test_v2_scheduling_history import history, open_week, add, lock, WEEK
from test_v2_primary import primary, login
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_history_service import begin_historical_edit


def test_assignment_dialog_modes_preserve_backend_contract():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for assignment dialog interaction test')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, 'tests/js/scheduling_assignment.test.cjs'],
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    html = (root / 'app/templates/v2/scheduling/week.html').read_text()
    for value, label in [('REPLACE', 'Replace employee'), ('SHIFT', 'Swap shifts'), ('STORE', 'Swap stores')]:
        assert f'<option value="{value}">{label}</option>' in html
    assert 'Published changes retain a new approved revision' not in html


def snapshot(db, ids, week, **kwargs):
    return serialize_week_board(db, week_start=week,
        selected_store_ids=(ids['north'], ids['south']),
        all_authorized_store_ids=(ids['north'], ids['south']),
        permission_flags={'scheduling.edit_draft_shifts': True, 'scheduling.create_draft': True},
        **kwargs)


@pytest.mark.parametrize('store_names', [('Andresen', 'Highway 99'), ('SR 503', 'Longview')])
@pytest.mark.parametrize('operation', ['callout', 'replacement', 'shift_swap', 'store_swap'])
def test_coverage_changes_alone_update_inventory_authorization(scheduling_db, operation, store_names):
    Session, actor, ids, _ = scheduling_db
    now = datetime(2026, 10, 20, 17, tzinfo=timezone.utc)
    with Session() as db:
        db.get(m.Store, ids['north']).name, db.get(m.Store, ids['south']).name = store_names
        principals = {}
        for key in ('alex', 'blair'):
            user = m.Principal(username=f'inventory-{key}', password_hash='unused',
                role=m.PrincipalRole.STORE, store_id=ids['north'], active=True)
            db.add(user); db.flush()
            db.get(m.Employee, ids[key]).principal_id = user.id
            principals[key] = Principal(user.id, user.username, Role.STORE, ids['north'], True)
        period, rows = ready_period(db, actor, ids, two=operation.endswith('swap'))
        args = dict(principal=actor, schedule_period_id=period.id,
            expected_version=period.version, allowed_store_ids=(ids['north'], ids['south']))
        approve_schedule(db, **args); publish_schedule(db, **args)
        before = working_context(db, principal=principals['alex'], now=now)
        if operation == 'callout':
            record_exception(db, principal=actor, shift_id=rows[0].id,
                event_type=m.AttendanceEventType.CALLED_OUT, event_at=now,
                commitment_employee_id=ids['blair'], today=date(2026, 10, 20))
        else:
            change_assignments(db, principal=actor, shift_id=rows[0].id,
                other_shift_id=rows[1].id if operation.endswith('swap') else None,
                replacement_employee_id=ids['blair'] if operation == 'replacement' else None,
                kind='STORE' if operation == 'store_swap' else 'SHIFT', reason='Verified coverage change',
                expected_versions={str(period.id): period.version},
                allowed_store_ids=(ids['north'], ids['south']), now=now)
        alex_store = ids['south'] if operation.endswith('swap') else None
        assert working_context(db, principal=principals['alex'], now=now)['store_id'] == alex_store
        assert working_context(db, principal=principals['blair'], now=now)['store_id'] == ids['north']
        assert working_context(db, principal=principals['alex'], now=now)['version'] != before['version']
        with pytest.raises(WorkingStoreConflict):
            assert_inventory_store(db, principal=principals['alex'], store_id=ids['north'], now=now)
        assert_inventory_store(db, principal=principals['blair'], store_id=ids['north'], now=now)
        assert list(db.scalars(select(m.InventoryStoreAccessGrant))) == []


def test_historical_selection_requires_deliberate_edit_and_retains_original(history):
    Session, actor, ids, _ = history
    with Session() as db:
        assert not snapshot(db, ids, WEEK, historical_admin=True, historical_edit=True)['historical_edit']
        period = open_week(db, actor)
        shift = add(db, actor, ids, period)
        read = snapshot(db, ids, WEEK, historical_admin=True, schedule_period_id=period.id)
        assert not read['editable'] and not read['historical_edit']
        assert read['actions']['edit_history'] and not read['actions']['save_history']
        lock(db, actor, period)
        version = period.version
        assert not snapshot(db, ids, WEEK, historical_admin=True, historical_edit=True)['historical_edit']
        correction = begin_historical_edit(db, principal=actor, week_start=WEEK,
            reason='Correct actual coverage', expected_version=version)
        assert correction.id != period.id
        assert db.get(m.ScheduleShift, shift.id).schedule_period_id == period.id
        assert period.version == version and period.historical_locked_at
        edit = snapshot(db, ids, WEEK, historical_admin=True, historical_edit=True)
        assert edit['editable'] and edit['actions']['save_history']
        assert not edit['actions']['generate'] and not edit['actions']['publish']
        assert not snapshot(db, ids, WEEK, historical_admin=True)['editable']


def test_current_week_cannot_inherit_history_from_record_or_query(history):
    Session, actor, ids, _ = history
    with Session() as db:
        current = m.SchedulePeriod(week_start_date=date(2026, 10, 11),
            week_end_date=date(2026, 10, 17), revision_number=1,
            status=m.SchedulePeriodStatus.DRAFT, is_historical=True,
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        db.add(current); db.flush()
        for explicit in (None, current.id):
            board = snapshot(db, ids, current.week_start_date, schedule_period_id=explicit,
                historical_admin=True, historical_edit=True, include_former=True)
            assert not board['historical_edit'] and board['mode'] == 'EMPTY'
            assert not board['actions']['edit_history'] and not board['actions']['save_history']
            assert not board['include_former']
        assert current.is_historical and current.status == m.SchedulePeriodStatus.DRAFT


def test_former_employee_choices_are_explicit_and_assigned_evidence_retained(history):
    Session, actor, ids, _ = history
    with Session() as db:
        period = open_week(db, actor)
        assert ids['inactive'] not in {e['id'] for e in snapshot(db, ids, WEEK, historical_admin=True)['employees']}
        expanded = snapshot(db, ids, WEEK, historical_admin=True, include_former=True)
        assert ids['inactive'] in {e['id'] for e in expanded['employees']}
        add(db, actor, ids, period, employee='inactive')
        normal = snapshot(db, ids, WEEK, historical_admin=True)
        assert ids['inactive'] in {e['id'] for e in normal['employees']}
        assert ids['inactive'] in {e['id'] for e in normal['lead_duty_employees']}


def test_exact_route_and_history_return_links_are_normal_navigation(primary, monkeypatch):
    import re
    from html import unescape
    monkeypatch.setattr('app.services.v2_scheduling_lead_duty_service.business_today', lambda db: date(2026,10,15))
    primary.account('ADMIN', linked=False)
    assert login(primary).status_code == 303
    client = primary.client
    page = client.get('/v2/scheduling/week?start=2026-10-11')
    assert page.status_code == 200
    assert 'Edit previous week' not in page.text and 'data-history-save-form' not in page.text
    assert 'Authorize emergency' not in page.text
    previous = client.get('/v2/scheduling/week?start=2026-09-27')
    assert 'Edit previous week' in previous.text
    assert 'data-history-save-form' not in previous.text
    headers = {'X-CSRF-Token': client.cookies.get('csrf_token')}
    result = client.post('/v2/scheduling/api/history/edit', headers=headers,
        json={'week_start_date':'2026-09-27', 'reason':'Manager records'})
    assert result.status_code == 200, result.text
    pid = result.json()['period_id']
    editing = client.get(f'/v2/scheduling/week?start=2026-09-27&period_id={pid}&history=edit')
    assert 'Save historical corrections' in editing.text and 'Exit historical editing' in editing.text
    exit_href = unescape(re.search(r'href="([^"]+)"[^>]*>Exit historical editing</a>', editing.text).group(1))
    returned = client.get(exit_href)
    assert returned.status_code == 200 and 'data-history-save-form' not in returned.text
    current_href = unescape(re.search(r'href="([^"]+)"[^>]*>Current week</a>', editing.text).group(1))
    assert 'period_id=' not in current_href and 'history=' not in current_href
    assert 'data-history-save-form' not in client.get(current_href).text
    assert client.get('/v2/scheduling/emergency-store').status_code == 404
    assert client.post('/v2/scheduling/emergency-store', headers=headers, data={}).status_code == 404
    assert client.get('/store/emergency-store').status_code == 404
