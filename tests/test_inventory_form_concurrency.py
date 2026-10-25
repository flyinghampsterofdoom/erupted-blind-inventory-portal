"""Competing complete form snapshots must serialize without stale overwrites."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import re
import pytest
from sqlalchemy import select, func
from app import models as m
from test_inventory_durable_drafts import case, headers
from test_v2_primary import primary
from test_v2_scheduling_foundation import scheduling_db


@pytest.mark.parametrize('kind,model,metadata,action', [
    ('change-box-count', m.ChangeBoxCount, 'change_box_count_id', 'CHANGE_BOX_COUNT_DRAFT_SAVED'),
    ('daily-chore-sheet', m.DailyChoreSheet, 'daily_chore_sheet_id', 'DAILY_CHORE_SHEET_SAVED'),
    ('non-sellable-stock-take', m.NonSellableStockTake, 'non_sellable_stock_take_id', 'NON_SELLABLE_STOCK_TAKE_DRAFT_SAVED'),
])
def test_simultaneous_form_saves_reject_stale_revision_then_submit_once(case, kind, model, metadata, action):
    c = case
    client = c.primary.client
    page = client.get('/store/' + kind)
    record_id = int(re.search(r'/store/' + kind + r'/(\d+)/save', page.text).group(1))
    base = f'/store/drafts/{kind}/{record_id}'
    initial = client.get(base + '/key').json()
    h = {**headers(c), 'X-Inventory-Draft': 'true',
         'X-Inventory-Identity': initial['scope']['record_uuid'],
         'X-Inventory-Principal': initial['scope']['principal_uuid'],
         'X-Inventory-Store': initial['scope']['store_uuid'],
         'X-Draft-Version': initial['state']['revision']}
    gate = Barrier(2)

    def save(label):
        gate.wait(timeout=10)
        return label, client.post(f'/store/{kind}/{record_id}/save',
            headers={**h, 'X-Draft-Operation': str(uuid4())},
            data={'employee_name': label, 'csrf_token': client.cookies.get('csrf_token')})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, label) for label in ('First counter', 'Second counter')]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(response.status_code for _, response in results) == [200, 409]
    winner, saved = next(pair for pair in results if pair[1].status_code == 200)
    with c.primary.Session() as db:
        assert db.get(model, record_id).employee_name == winner
        assert db.scalar(select(func.count()).select_from(m.AuditLog).where(
            m.AuditLog.action == action, m.AuditLog.meta[metadata].as_integer() == record_id)) == 1

    # Distinct final commands racing at the same accepted revision return one receipt.
    gate = Barrier(2)

    def submit():
        gate.wait(timeout=10)
        return client.post(f'/store/{kind}/{record_id}/submit',
            headers={**h, 'X-Draft-Version': saved.json()['revision'], 'X-Draft-Operation': str(uuid4())},
            data={'employee_name': winner, 'csrf_token': client.cookies.get('csrf_token')})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        receipts = [future.result(timeout=30) for future in futures]
    assert all(response.status_code == 200 for response in receipts)
    assert receipts[0].json() == receipts[1].json()
    with c.primary.Session() as db:
        assert db.get(model, record_id).employee_name == winner
        assert db.get(model, record_id).status.value == 'SUBMITTED'
        assert db.scalar(select(func.count()).select_from(m.AuditLog).where(
            m.AuditLog.action.like('%_SUBMITTED'), m.AuditLog.meta[metadata].as_integer() == record_id)) == 1
