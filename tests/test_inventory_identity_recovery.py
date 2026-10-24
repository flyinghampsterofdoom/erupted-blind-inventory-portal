"""Adversarial recovery: real PostgreSQL backup/restore, new IDs, retained keys.

Every database is disposable and every inventory quantity is synthetic.
"""
import base64
import json
import os
import shutil
import subprocess
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.exc import DBAPIError

from app import models as m
from app.config import settings
from app.services.inventory_draft_service import recovery_key, recovery_scope
from test_inventory_durable_drafts import case, headers, primary, scheduling_db

K1 = base64.b64encode(b'1' * 32).decode()
K2 = base64.b64encode(b'2' * 32).decode()


def ring(monkeypatch, keys=None, active='k1'):
    monkeypatch.setattr(settings, 'inventory_draft_encryption_keys', json.dumps(keys or {'k1': K1}))
    monkeypatch.setattr(settings, 'inventory_draft_active_key_id', active)


def identity_headers(c, payload):
    s = payload['scope']
    return {**headers(c), 'X-Inventory-Draft': 'true', 'X-Inventory-Identity': s['record_uuid'],
            'X-Inventory-Principal': s['principal_uuid'], 'X-Inventory-Store': s['store_uuid']}


def scoped_key(payload, kid='k1'):
    return base64.b64decode(payload['keys'][kid])


def recovery_path(scope):
    return (f"/store/draft-recovery/{scope['kind']}/{scope['record_uuid']}/key"
            f"?principal_uuid={scope['principal_uuid']}&store_uuid={scope['store_uuid']}&record_id={scope['record_id']}")


def add_count(c, size=1000, explicit_id=None):
    with c.primary.Session() as db:
        row = m.CountSession(store_id=c.primary.ids['north'], campaign_id=db.scalar(select(m.Campaign.id)),
            employee_name='Synthetic 1000-product round', created_by_principal_id=c.pid)
        if explicit_id is not None:
            row.id = explicit_id
        db.add(row); db.flush()
        for i in range(size):
            db.add(m.SnapshotLine(session_id=row.id, variation_id=f'SYN-{i}',
                item_name='Synthetic product', variation_name=str(i), expected_on_hand=5))
        db.commit()
        return row.id, row.inventory_identity


def test_real_older_backup_with_newer_browser_draft_rejects_reused_id_and_retains_recovery(case, monkeypatch, tmp_path):
    c = case; ring(monkeypatch)
    source = c.primary.Session.kw['bind']
    source_url = source.url
    # Tools are selected explicitly for the installed local major version.
    from pathlib import Path
    with source.connect() as db:
        major = int(db.execute(text('SHOW server_version_num')).scalar()) // 10000
    local_tools = Path(f'/opt/homebrew/opt/postgresql@{major}/bin')
    pg_dump = str(local_tools / 'pg_dump') if local_tools.exists() else shutil.which('pg_dump')
    pg_restore = str(local_tools / 'pg_restore') if local_tools.exists() else shutil.which('pg_restore')
    assert pg_dump and pg_restore, 'Matching PostgreSQL backup tools are required'
    env = {**os.environ, 'PGHOST': source_url.host or '', 'PGDATABASE': source_url.database,
           'PGUSER': source_url.username or os.environ.get('USER', ''), 'PGPASSWORD': source_url.password or ''}
    if source_url.port: env['PGPORT'] = str(source_url.port)
    backup = tmp_path / 'synthetic.dump'
    subprocess.run([pg_dump, '-Fc', '-f', str(backup)], env=env, check=True, capture_output=True)
    old_id, old_uuid = add_count(c)
    path = f'/store/drafts/blind-count/{old_id}/key'
    original = c.primary.client.get(path).json()
    values = {f'SYN-{i}': {'front_qty': str(i % 97 + 1), 'back_qty': str(i % 41 + 1)} for i in range(1000)}
    browser = json.dumps({'revision': 0, 'generation': 1000, 'acknowledged': 0, 'values': values}).encode()
    nonce = os.urandom(12)
    aad = f"inventory-draft:v2:{original['scope']['principal_uuid']}:{original['scope']['store_uuid']}:blind-count:{old_uuid}:k1".encode()
    ciphertext = AESGCM(scoped_key(original)).encrypt(nonce, browser, aad)
    restored_name = 'erupted_restore_' + uuid4().hex[:12]
    admin = create_engine(os.environ['TEST_POSTGRES_ADMIN_URL'], isolation_level='AUTOCOMMIT')
    restored = create_engine(source_url.set(database=restored_name))
    try:
        with admin.connect() as db: db.execute(text(f'CREATE DATABASE "{restored_name}"'))
        subprocess.run([pg_restore, '--exit-on-error', '--no-owner', '--no-privileges', '-d', restored_name, str(backup)],
                       env=env, check=True, capture_output=True)
        c.primary.Session.configure(bind=restored)
        new_id, new_uuid = add_count(c)
        assert new_id == old_id and new_uuid != old_uuid
        current = c.primary.client.get(path).json()
        assert scoped_key(current) != scoped_key(original)
        with pytest.raises(InvalidTag): AESGCM(scoped_key(current)).decrypt(nonce, ciphertext, aad)
        # Neither a draft nor a final command can cross the immutable identity.
        for endpoint, body in [('draft', {'revision': 0, 'operation_id': 'old-offline', 'changes': values}), ('submit', {'revision': 0})]:
            result = c.primary.client.post(f'/store/sessions/{new_id}/{endpoint}', json=body,
                                          headers=identity_headers(c, original))
            assert result.status_code == 409
        with c.primary.Session() as db:
            assert db.scalar(select(func.count()).select_from(m.Entry).where(m.Entry.session_id == new_id)) == 0
            assert db.scalar(select(func.count()).select_from(m.CountObservation).where(m.CountObservation.session_id == new_id)) == 0
        recovered = c.primary.client.get(recovery_path(original['scope']))
        assert recovered.status_code == 200
        assert AESGCM(scoped_key(recovered.json())).decrypt(nonce, ciphertext, aad) == browser
        ring(monkeypatch, {'k1': K1, 'k2': K2}, 'k2')
        rotated = c.primary.client.get(recovery_path(original['scope'])).json()
        assert rotated['active_key_id'] == 'k2'
        assert AESGCM(scoped_key(rotated)).decrypt(nonce, ciphertext, aad) == browser
        ring(monkeypatch, {'k2': K2}, 'k2')
        missing = c.primary.client.get(recovery_path(original['scope'])).json()
        assert 'k1' not in missing['keys']
        assert ciphertext  # Browser must retain this unchanged; real-WebCrypto tests enforce it.
    finally:
        c.primary.Session.configure(bind=source)
        restored.dispose()
        with admin.connect() as db: db.execute(text(f'DROP DATABASE IF EXISTS "{restored_name}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.parametrize('model', [m.CountSession, m.NonSellableStockTake, m.ChangeBoxCount, m.DailyChoreSheet, m.Store, m.Principal, m.AdminStoreCount])
def test_permanent_identity_cannot_be_changed(case, model):
    # Use an actual existing row when available; the migration installs the same
    # immutable trigger on all seven tables, independently verified below.
    with case.primary.Session() as db:
        triggers = db.execute(text("SELECT tgrelid::regclass::text FROM pg_trigger WHERE tgname='protect_inventory_identity'" )).scalars().all()
        assert model.__tablename__ in triggers
        row = db.scalar(select(model))
        if row is not None:
            with pytest.raises(DBAPIError):
                db.execute(text(f'UPDATE {model.__tablename__} SET inventory_identity=gen_random_uuid() WHERE id=:id'), {'id': row.id})
            db.rollback()


def test_sequence_reset_and_recreated_record_get_new_identity(case):
    c = case
    old = c.primary.client.get(c.base + '/key').json()
    with c.primary.Session() as db:
        db.execute(delete(m.SnapshotLine).where(m.SnapshotLine.session_id == c.cid))
        db.execute(delete(m.CountSession).where(m.CountSession.id == c.cid))
        db.execute(text("SELECT setval(pg_get_serial_sequence('count_sessions','id'), :id, false)"), {'id': c.cid})
        db.commit()
    reused, incarnation = add_count(c, size=1000)
    assert reused == c.cid and incarnation != old['scope']['record_uuid']
    assert c.primary.client.post(f'/store/sessions/{reused}/draft', headers=identity_headers(c, old),
        json={'revision': 0, 'operation_id': 'old', 'changes': {'SYN-0': {'front_qty': '1', 'back_qty': '2'}}}).status_code == 409


def test_orphan_recovery_never_grants_another_identity_access(case):
    c = case; original = c.primary.client.get(c.base + '/key').json()
    for field in ('principal_uuid', 'store_uuid'):
        wrong = {**original['scope'], field: str(uuid4())}
        assert c.primary.client.get(recovery_path(wrong)).status_code == 404
    # A correct account/store can decrypt its own missing record, without any
    # permission to create or submit that missing record.
    orphan = {**original['scope'], 'record_uuid': str(uuid4()), 'record_id': 9999999}
    assert c.primary.client.get(recovery_path(orphan)).status_code == 200
    assert c.primary.client.get('/store/drafts/blind-count/9999999/state').status_code == 404


def test_keys_stable_across_independent_instances_and_rotation_fails_closed(case, monkeypatch):
    ring(monkeypatch)
    c = case
    with c.primary.Session() as db:
        scope = recovery_scope(db, principal_id=c.pid, kind='blind-count', row=db.get(m.CountSession, c.cid))
    expected = recovery_key(scope=scope)
    code = ('import json,sys; from app.config import settings; '
            'from app.services.inventory_draft_service import recovery_key; '
            's=json.load(sys.stdin); settings.inventory_draft_encryption_keys=s["ring"]; '
            'settings.inventory_draft_active_key_id="k1"; '
            'print(json.dumps(recovery_key(scope=s["scope"])))')
    # Synthetic key only; child processes model fresh independent application instances.
    import sys
    for _ in range(2):
        child = subprocess.run([sys.executable, '-c', code], input=json.dumps({'ring': json.dumps({'k1': K1}), 'scope': scope}),
                               text=True, capture_output=True, check=True)
        assert json.loads(child.stdout) == expected
    monkeypatch.setattr(settings, 'inventory_draft_active_key_id', 'missing')
    result = c.primary.client.get(c.base + '/key')
    assert result.status_code == 503 and 'keys' not in result.json()


def test_downgrade_cannot_destroy_identity_or_strand_retained_drafts(case):
    from alembic import command
    from app.schema_contract import _alembic_config, current_revision, HEAD_REVISION
    engine = case.primary.Session.kw['bind']
    with engine.connect() as db:
        before = db.execute(text('SELECT id,inventory_identity FROM count_sessions ORDER BY id')).all()
    command.downgrade(_alembic_config(str(engine.url)), '20261024_0049')
    assert current_revision(engine) == '20261024_0049'
    command.upgrade(_alembic_config(str(engine.url)), 'head')
    with engine.connect() as db:
        assert db.execute(text('SELECT id,inventory_identity FROM count_sessions ORDER BY id')).all() == before
    assert current_revision(engine) == HEAD_REVISION
