from __future__ import annotations

import inspect
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.auth as auth_module
import app.routers.management as management_router
from app.auth import Principal, Role, get_current_principal
from app.db import get_db
from app.models import (
    Base,
    ParLevel,
    ParLevelSource,
    PurchaseOrderConfidenceState,
    Store,
    Vendor,
    VendorSkuConfig,
)
from app.security.csrf import CSRF_COOKIE_NAME, install_csrf_cookie_middleware

TABLES = ('stores', 'vendors', 'vendor_sku_configs', 'par_levels')
ROOT = Path(__file__).resolve().parents[1]
EDITOR_SCRIPT = ROOT / 'app/static/v2/ordering-par-editor.js'
EDITOR_TEMPLATE = ROOT / 'app/templates/management_ordering_par_levels_vendor.html'


@pytest.fixture()
def par_editor_site(monkeypatch):
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in TABLES])
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """CREATE TABLE audit_log (
                id BIGINT PRIMARY KEY,
                actor_principal_id BIGINT,
                action TEXT NOT NULL,
                session_id BIGINT,
                ip TEXT,
                metadata JSON NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )"""
        )
    session = Session(engine, expire_on_commit=False)
    counters: dict[str, int] = {}

    @event.listens_for(session, 'before_flush')
    def assign_bigint_ids(_session, _context, _instances):
        for row in session.new:
            if hasattr(row, 'id') and row.id is None:
                key = type(row).__name__
                counters[key] = counters.get(key, 10_000) + 1
                row.id = counters[key]

    session.add(Vendor(id=10, square_vendor_id='SQ-VENDOR-10', name='Scale Vendor', active=True))
    session.add(Vendor(id=11, square_vendor_id='SQ-VENDOR-11', name='Other Vendor', active=True))
    session.add_all(
        Store(id=store_id, name=f'Store {store_id}', square_location_id=f'LOC-{store_id}', active=True)
        for store_id in range(1, 5)
    )
    session.add(Store(id=9, name='Inactive Store', square_location_id='LOC-9', active=False))
    session.add_all(
        VendorSkuConfig(
            id=1000 + index,
            vendor_id=10,
            sku=f'SKU-{index:03d}',
            square_variation_id=f'VAR-{index:03d}',
            unit_cost=None,
            active=True,
            is_default_vendor=True,
        )
        for index in range(200)
    )
    session.add(
        VendorSkuConfig(
            id=9999,
            vendor_id=11,
            sku='OTHER-VENDOR-SKU',
            active=True,
            is_default_vendor=True,
        )
    )
    session.commit()

    principal = SimpleNamespace(
        value=Principal(id=6, username='owner', role=Role.ADMIN, store_id=None, active=True)
    )
    app = FastAPI()
    install_csrf_cookie_middleware(app)
    app.include_router(management_router.router)

    def current_principal():
        return principal.value

    def db_override():
        yield session

    app.dependency_overrides[get_current_principal] = current_principal
    app.dependency_overrides[get_db] = db_override
    monkeypatch.setattr(
        auth_module,
        'principal_has_permission',
        lambda _db, *, principal, permission_key, fallback_allowed: fallback_allowed,
    )

    with TestClient(app, follow_redirects=False) as client:
        client.get('/csrf-cookie')
        yield SimpleNamespace(client=client, session=session, principal=principal)

    session.close()
    engine.dispose()


def post_changes(site, changes, *, vendor_id=10, payload_vendor_id=None, csrf_token=None):
    token = csrf_token if csrf_token is not None else site.client.cookies.get(CSRF_COOKIE_NAME)
    return site.client.post(
        f'/management/ordering-tool/par-levels/{vendor_id}/save',
        json={
            'vendor_id': vendor_id if payload_vendor_id is None else payload_vendor_id,
            'changes': changes,
        },
        headers={'X-CSRF-Token': token},
    )


def change(store_id, sku, manual_level, manual_par):
    return {
        'store_id': store_id,
        'sku': sku,
        'manual_level': manual_level,
        'manual_par': manual_par,
    }


def test_small_par_edit_saves_with_manual_semantics_and_preserves_other_fields(par_editor_site):
    existing = ParLevel(
        id=50,
        vendor_id=10,
        store_id=1,
        sku='SKU-000',
        manual_par_level=2,
        manual_stock_up_level=5,
        suggested_par_level=19,
        par_source=ParLevelSource.MANUAL,
        confidence_score='0.7500',
        confidence_state=PurchaseOrderConfidenceState.NORMAL,
        locked_manual=False,
        confidence_streak_up=3,
        confidence_streak_down=2,
    )
    par_editor_site.session.add(existing)
    par_editor_site.session.commit()

    response = post_changes(par_editor_site, [change(1, 'SKU-000', 8, 14)])

    assert response.status_code == 200
    assert response.json() == {'ok': True, 'vendor_id': 10, 'saved': 1}
    par_editor_site.session.refresh(existing)
    assert (existing.manual_par_level, existing.manual_stock_up_level) == (8, 14)
    assert existing.par_source == ParLevelSource.MANUAL
    assert existing.suggested_par_level == 19
    assert existing.locked_manual is False
    assert existing.confidence_score == pytest.approx(0.75)
    assert (existing.confidence_streak_up, existing.confidence_streak_down) == (3, 2)


def test_large_json_save_handles_grid_equivalent_to_1600_form_fields(par_editor_site):
    changes = [
        change(store_id, f'SKU-{sku_index:03d}', sku_index % 9, (sku_index % 9) + 10)
        for sku_index in range(200)
        for store_id in range(1, 5)
    ]

    response = post_changes(par_editor_site, changes)

    assert response.status_code == 200
    assert response.json()['saved'] == 800
    saved_count = par_editor_site.session.scalar(
        select(func.count()).select_from(ParLevel).where(ParLevel.vendor_id == 10)
    )
    assert saved_count == 800


def test_only_submitted_dirty_row_changes_and_unchanged_row_is_untouched(par_editor_site):
    original_time = datetime(2025, 1, 2, tzinfo=timezone.utc)
    changed = ParLevel(
        id=51,
        vendor_id=10,
        store_id=1,
        sku='SKU-001',
        manual_par_level=1,
        manual_stock_up_level=2,
        updated_at=original_time,
    )
    untouched = ParLevel(
        id=52,
        vendor_id=10,
        store_id=2,
        sku='SKU-001',
        manual_par_level=3,
        manual_stock_up_level=4,
        updated_at=original_time,
    )
    par_editor_site.session.add_all([changed, untouched])
    par_editor_site.session.commit()

    response = post_changes(par_editor_site, [change(1, 'SKU-001', 7, 11)])

    assert response.status_code == 200
    par_editor_site.session.refresh(changed)
    par_editor_site.session.refresh(untouched)
    assert (changed.manual_par_level, changed.manual_stock_up_level) == (7, 11)
    assert (untouched.manual_par_level, untouched.manual_stock_up_level) == (3, 4)
    assert untouched.updated_at.replace(tzinfo=timezone.utc) == original_time


def test_explicit_null_clears_fields_and_restores_dynamic_source(par_editor_site):
    existing = ParLevel(
        id=53,
        vendor_id=10,
        store_id=1,
        sku='SKU-002',
        manual_par_level=4,
        manual_stock_up_level=9,
        par_source=ParLevelSource.MANUAL,
    )
    par_editor_site.session.add(existing)
    par_editor_site.session.commit()

    response = post_changes(par_editor_site, [change(1, 'SKU-002', None, None)])

    assert response.status_code == 200
    par_editor_site.session.refresh(existing)
    assert existing.manual_par_level is None
    assert existing.manual_stock_up_level is None
    assert existing.par_source == ParLevelSource.DYNAMIC


@pytest.mark.parametrize('invalid_value', [-1, 1.5, '7', True])
def test_negative_and_non_integer_values_are_rejected_atomically(par_editor_site, invalid_value):
    response = post_changes(
        par_editor_site,
        [
            change(1, 'SKU-003', 5, 9),
            change(2, 'SKU-004', invalid_value, 10),
        ],
    )

    assert response.status_code == 400
    assert response.json()['ok'] is False
    assert response.json()['error']['sku'] == 'SKU-004'
    assert par_editor_site.session.scalar(select(ParLevel).where(ParLevel.sku == 'SKU-003')) is None


def test_wrong_vendor_sku_and_inactive_store_are_rejected_without_partial_writes(par_editor_site):
    wrong_sku = post_changes(
        par_editor_site,
        [change(1, 'SKU-005', 5, 9), change(2, 'OTHER-VENDOR-SKU', 4, 8)],
    )
    assert wrong_sku.status_code == 400
    assert wrong_sku.json()['error']['code'] == 'VALIDATION_FAILED'
    assert 'OTHER-VENDOR-SKU' in wrong_sku.json()['error']['message']
    assert par_editor_site.session.scalar(select(ParLevel).where(ParLevel.sku == 'SKU-005')) is None

    inactive_store = post_changes(par_editor_site, [change(9, 'SKU-005', 5, 9)])
    assert inactive_store.status_code == 400
    assert 'Active store 9' in inactive_store.json()['error']['message']


def test_payload_vendor_must_match_url_vendor(par_editor_site):
    response = post_changes(
        par_editor_site,
        [change(1, 'SKU-000', 1, 2)],
        payload_vendor_id=11,
    )
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'VENDOR_MISMATCH'


def test_unauthorized_principal_cannot_save(par_editor_site):
    par_editor_site.principal.value = Principal(
        id=7,
        username='store',
        role=Role.STORE,
        store_id=1,
        active=True,
    )
    response = post_changes(par_editor_site, [change(1, 'SKU-000', 1, 2)])
    assert response.status_code == 403
    assert par_editor_site.session.scalar(select(ParLevel).where(ParLevel.sku == 'SKU-000')) is None


def test_json_save_remains_csrf_protected(par_editor_site):
    response = post_changes(
        par_editor_site,
        [change(1, 'SKU-000', 1, 2)],
        csrf_token='wrong',
    )
    assert response.status_code == 403
    assert response.json()['detail'] == 'Invalid CSRF token'
    assert par_editor_site.session.scalar(select(ParLevel).where(ParLevel.sku == 'SKU-000')) is None


def test_editor_transport_sends_only_json_dirty_rows_and_has_no_form_parser_override():
    route_source = inspect.getsource(management_router.ordering_tool_par_levels_vendor_save)
    template = EDITOR_TEMPLATE.read_text()
    script = EDITOR_SCRIPT.read_text()

    assert 'await request.json()' in route_source
    assert 'request.form()' not in route_source
    assert 'max_fields' not in route_source
    assert 'name="row_key"' not in template
    assert 'manual_level__' not in template
    assert "body: JSON.stringify({vendor_id: vendorId, changes})" in script
    assert "'X-CSRF-Token'" in script
    assert 'Array.from(dirtyRows.values())' in script


def test_browser_draft_contract_retains_on_failure_clears_on_success_restores_and_isolates_vendor():
    harness = f"""
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync({json.dumps(str(EDITOR_SCRIPT))}, 'utf8'));
const api = globalThis.OrderingParEditorDraft;
const values = new Map();
const storage = {{
  setItem: (key, value) => values.set(key, value),
  getItem: (key) => values.has(key) ? values.get(key) : null,
  removeItem: (key) => values.delete(key),
}};
const key10 = api.draftKey(10, 1);
const draft = {{version: 1, vendor_id: 10, changes: [{{
  store_id: 2, sku: 'SKU-009', manual_level: null, manual_par: 17,
}}]}};
if (!api.persistDraft(storage, key10, draft)) throw new Error('persist failed');
api.settleDraft(storage, key10, false);
if (!storage.getItem(key10)) throw new Error('failed save cleared draft');
const restored = api.decodeDraft(storage.getItem(key10), 10, 1);
if (!restored || restored.changes[0].manual_level !== null || restored.changes[0].manual_par !== 17) {{
  throw new Error('draft did not restore');
}}
if (api.decodeDraft(storage.getItem(key10), 11, 1) !== null) throw new Error('draft leaked across vendors');
if (api.draftKey(10, 1) === api.draftKey(11, 1)) throw new Error('vendor keys collide');
api.settleDraft(storage, key10, true);
if (storage.getItem(key10) !== null) throw new Error('successful save did not clear draft');
"""

    result = subprocess.run(
        ['node', '-e', harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_editor_has_recovery_failure_and_navigation_protection_ui_contract():
    script = EDITOR_SCRIPT.read_text()
    template = EDITOR_TEMPLATE.read_text()

    assert 'Unsaved changes recovered from this browser' in script
    assert 'Save failed — your changes are still preserved locally.' in script
    assert "addEventListener('beforeunload'" in script
    assert "setState('saving'" in script
    assert "setState('saved'" in script
    assert 'role="status"' in template
    assert '/v2-assets/ordering-par-editor.js' in template
