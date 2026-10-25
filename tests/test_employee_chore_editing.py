"""Real employee routes and PostgreSQL persistence; no authorization overrides."""
import re
from datetime import timedelta

import pytest
from sqlalchemy import select, func
from app import models as m
from app.config import settings
from app.services.daily_chore_service import get_or_create_today_sheet, save_sheet_progress
from test_v2_scheduling_foundation import scheduling_db
from test_v2_primary import primary, login, post
from test_employee_operational_context import seeded_current
from test_inventory_durable_drafts import SECRET


def draft_headers(primary, sid):
    data = primary.client.get(f'/store/drafts/daily-chore-sheet/{sid}/key').json()
    scope = data['scope']
    return {'X-Requested-With': 'autosave', 'X-Inventory-Draft': 'true',
        'X-Working-Context': data['working_context'],
        'X-Inventory-Identity': scope['record_uuid'], 'X-Inventory-Principal': scope['principal_uuid'],
        'X-Inventory-Store': scope['store_uuid'], 'X-Draft-Version': data['state']['revision']}


@pytest.mark.parametrize('role', ['STORE', 'LEAD'])
def test_chore_edit_persistence_scope_history_and_management(primary, monkeypatch, role):
    monkeypatch.setattr(settings, 'inventory_draft_encryption_key', SECRET)
    pid, *_ = seeded_current(primary, role=role)
    client = primary.client
    page = client.get('/store/daily-chore-sheet')
    assert page.status_code == 200
    sid = int(re.search(r'/daily-chore-sheet/(\d+)/save', page.text).group(1))
    with primary.Session() as db:
        tasks = list(db.scalars(select(m.DailyChoreEntry.task_id).where(m.DailyChoreEntry.sheet_id == sid).order_by(m.DailyChoreEntry.task_id)))
        sheet = db.get(m.DailyChoreSheet, sid)
        history = m.DailyChoreSheet(store_id=sheet.store_id, sheet_date=sheet.sheet_date-timedelta(days=1),
            employee_name='Historical employee', created_by_principal_id=pid, status=m.DailyChoreSheetStatus.SUBMITTED)
        db.add(history); db.flush()
        entry = m.DailyChoreEntry(sheet_id=history.id, task_id=tasks[0], completed=True, completed_at=sheet.created_at)
        db.add(entry)
        audit = m.AuditLog(actor_principal_id=pid, action='HISTORICAL_CHORE_SENTINEL', meta={'sheet_id': history.id})
        db.add(audit); db.commit()
        hid, aid, stamp = history.id, audit.id, entry.completed_at
        foreign, _ = get_or_create_today_sheet(db, store_id=primary.ids['south'], principal_id=pid)
        db.commit(); fid = foreign.id
    h = draft_headers(primary, sid)
    h['X-Draft-Operation'] = 'complete-first'
    first = client.post(
        f'/store/daily-chore-sheet/{sid}/save', headers=h,
        data={'csrf_token': client.cookies.get('csrf_token'), 'employee_name': 'Synthetic Lexi', 'completed_task_ids': str(tasks[0])})
    assert first.status_code == 200, first.text
    assert first.json()['completed_at'][str(tasks[0])]
    # A real competing edit must continue to fail, never bypass the revision guard.
    stale = client.post(f'/store/daily-chore-sheet/{sid}/save', headers={**h, 'X-Draft-Operation': 'stale'},
        data={'csrf_token': client.cookies.get('csrf_token'), 'employee_name': 'Stale'})
    assert stale.status_code == 409
    h.update({'X-Draft-Version': first.json()['revision'], 'X-Draft-Operation': 'complete-second'})
    second = client.post(f'/store/daily-chore-sheet/{sid}/save', headers=h,
        data={'csrf_token': client.cookies.get('csrf_token'), 'employee_name': 'Synthetic Lexi', 'completed_task_ids': [str(tasks[0]), str(tasks[1])]})
    assert second.status_code == 200
    assert second.json()['completed_at'][str(tasks[0])] == first.json()['completed_at'][str(tasks[0])]
    h.update({'X-Draft-Version': second.json()['revision'], 'X-Draft-Operation': 'uncheck-first'})
    third = client.post(f'/store/daily-chore-sheet/{sid}/save', headers=h,
        data={'csrf_token': client.cookies.get('csrf_token'), 'employee_name': 'Synthetic Lexi', 'completed_task_ids': str(tasks[1])})
    assert third.status_code == 200 and third.json()['completed_at'][str(tasks[0])] is None
    assert post(client, '/logout').status_code == 303
    assert login(primary).status_code == 303
    assert client.get(f'/store/drafts/daily-chore-sheet/{sid}/resume').status_code == 200
    h = draft_headers(primary, sid)
    for endpoint in ('key', 'state', 'resume'):
        assert client.get(f'/store/drafts/daily-chore-sheet/{fid}/{endpoint}').status_code == 404
    for action in ('save', 'submit'):
        response = client.post(f'/store/daily-chore-sheet/{fid}/{action}', headers=h,
            data={'csrf_token': client.cookies.get('csrf_token'), 'employee_name': 'Unauthorized'})
        assert response.status_code in (403, 404)
    assert client.post(f'/store/daily-chore-sheet/{sid}/save', headers=h, data={'employee_name':'No CSRF'}).status_code == 403
    assert client.get('/management/daily-chore-lists').status_code == (403 if role == 'STORE' else 200)
    assert client.get('/management/daily-chore-tasks').status_code == (200 if role == 'ADMIN' else 403)
    if role != 'ADMIN':
        assert post(client, '/management/daily-chore-tasks/add', section='Opening', prompt='Unauthorized').status_code == 403
    with primary.Session() as db:
        entries = list(db.scalars(select(m.DailyChoreEntry).where(m.DailyChoreEntry.sheet_id == sid).order_by(m.DailyChoreEntry.task_id)))
        assert not entries[0].completed and entries[0].completed_at is None
        assert entries[1].completed and entries[1].completed_at is not None
        assert db.get(m.DailyChoreSheet, sid).employee_name == 'Synthetic Lexi'
        old = db.get(m.DailyChoreSheet, hid)
        assert old.employee_name == 'Historical employee' and old.status == m.DailyChoreSheetStatus.SUBMITTED
        assert db.get(m.DailyChoreEntry, (hid, tasks[0])).completed_at == stamp
        assert db.get(m.AuditLog, aid).meta == {'sheet_id': hid}
        assert db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action == 'DAILY_CHORE_SHEET_SAVED')) == 3


def test_owner_retains_chore_management_and_store_role_boundary(primary):
    pid = primary.account('ADMIN')
    login(primary)
    client = primary.client
    assert client.get('/management/daily-chore-lists').status_code == 200
    assert client.get('/management/daily-chore-tasks').status_code == 200
    assert post(client, '/management/daily-chore-tasks/add', section='Opening', prompt='Owner added task').status_code == 303
    with primary.Session() as db:
        assert db.scalar(select(m.DailyChoreTask.id).where(m.DailyChoreTask.prompt == 'Owner added task')) is not None
        sheet, _ = get_or_create_today_sheet(db, store_id=primary.ids['north'], principal_id=pid)
        db.commit(); sid = sheet.id
    assert client.get(f'/management/daily-chore-lists/{sid}').status_code == 200
    assert client.get('/store/daily-chore-sheet').status_code == 403
