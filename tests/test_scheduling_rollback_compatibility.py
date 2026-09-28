"""Bounded compatibility plus actual A1 -> rollback -> A1 disposable-DB handoff.

Set A1_SOURCE_DIR to the separately reviewed A1 worktree. No A1 runtime code or
migration is copied into the compatibility build.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select, text

from test_v2_scheduling_foundation import scheduling_db, _coverage
from app import schema_contract
from app.models import SchedulePeriod
from app.services.v2_scheduling_service import create_draft_period
from datetime import date


@pytest.mark.parametrize('revision', ['20260927_0027', '20260928_0028'])
def test_explicit_schema_acceptance(monkeypatch, revision):
    monkeypatch.setattr(schema_contract.settings, 'schema_revision_check_enabled', True)
    monkeypatch.setattr(schema_contract, 'current_revision', lambda engine: revision)
    schema_contract.assert_supported_schema(object())
    assert schema_contract.HEAD_REVISION == '20260927_0027'
    assert schema_contract.SUPPORTED_REVISIONS == {'20260927_0027', '20260928_0028'}


@pytest.mark.parametrize('revision', [None, '20260923_0026', '20260929_0029', 'unknown'])
def test_unapproved_schema_rejected(monkeypatch, revision):
    monkeypatch.setattr(schema_contract.settings, 'schema_revision_check_enabled', True)
    monkeypatch.setattr(schema_contract, 'current_revision', lambda engine: revision)
    with pytest.raises(schema_contract.UnsupportedSchemaError):
        schema_contract.assert_supported_schema(object())


PROBE = r'''
import json, os, sys
from datetime import date, datetime, time, timedelta, timezone
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from app.auth import Principal, Role
from app.models import SchedulePeriod, ScheduleShift, Principal as Account, Base, Employee
from app import schema_contract

url = os.environ['DATABASE_URL']
engine = create_engine(url)
assert engine.url.host in ('localhost', '127.0.0.1')
assert engine.url.database.startswith('erupted_scheduling_')
mode, period_id = sys.argv[1], int(sys.argv[2])
if mode == 'a1_create':
    schema_contract.upgrade_database(url)
# Invoke the actual application's startup schema hook, not a replacement guard.
from app.main import _verify_schema_revision
_verify_schema_revision()
with Session(engine) as db:
    account = db.scalars(select(Account)).first()
    principal = Principal(account.id, account.username, Role(account.role.value), None, True)
    period = db.get(SchedulePeriod, period_id)
    from app.services.v2_scheduling_policy_service import (
        regenerate_period, ensure_rolling_schedule_horizon, manual_generate_draft_schedule,
        run_schedule_automation, _create_generated_period,
    )
    if mode in ('a1_create', 'a1_restore'):
        from app.services.v2_scheduling_snapshot_service import canonical_checksum
        from app.models import ScheduleGeneratorSnapshot
        old = list(db.scalars(select(ScheduleGeneratorSnapshot).order_by(ScheduleGeneratorSnapshot.sequence)))
        for row in old:
            assert canonical_checksum(row.payload) == row.checksum
        regenerate_period(db, principal=principal, schedule_period_id=period_id)
        db.commit()
        rows = list(db.scalars(select(ScheduleGeneratorSnapshot).order_by(ScheduleGeneratorSnapshot.sequence)))
        assert [r.sequence for r in rows] == list(range(1,len(rows)+1))
        for row in rows:
            assert canonical_checksum(row.payload) == row.checksum
    else:
        assert 'schedule_generator_snapshots' not in Base.metadata.tables
        from app.services.v2_scheduling_service import SchedulingConflict, create_shift, ShiftInput
        from app.services.v2_scheduling_board_service import serialize_week_board
        from app.models import Store
        store_ids = tuple(db.scalars(select(Store.id)))
        board = serialize_week_board(db, week_start=period.week_start_date,
            selected_store_ids=store_ids, all_authorized_store_ids=store_ids,
            permission_flags={}, schedule_period_id=period_id)
        assert board
        # Ordinary emergency draft editing remains available without any A1 models.
        create_shift(db, principal=principal, schedule_period_id=period_id,
            expected_version=period.version,
            values=ShiftInput(employee_id=db.scalars(select(Employee.id).where(Employee.active.is_(True))).first(), store_id=store_ids[0],
                shift_date=period.week_start_date + timedelta(days=1 if mode == 'rollback_native' else 2),
                start_time=time(9), end_time=time(17)), allowed_store_ids=store_ids)
        db.commit()
        if mode == 'rollback_native':
            regenerate_period(db, principal=principal, schedule_period_id=period_id)
            db.commit()
            assert schema_contract.current_revision(engine) == '20260927_0027'
        else:
            # Startup bypass configuration cannot bypass the generation safety guard.
            schema_contract.settings.schema_revision_check_enabled = False
            calls = [
                lambda: regenerate_period(db, principal=principal, schedule_period_id=period_id),
                lambda: ensure_rolling_schedule_horizon(db, principal=principal),
                lambda: manual_generate_draft_schedule(db, principal=principal),
                lambda: run_schedule_automation(db, principal=principal),
                lambda: _create_generated_period(db, principal=principal,
                    week_start=date(2026,11,1), source=None, source_week_offset=0,
                    publication_at=None, generated_at=datetime.now(timezone.utc), note='blocked probe'),
            ]
            before = (period.version, db.scalar(select(func.count()).select_from(SchedulePeriod)))
            for call in calls:
                try:
                    call()
                except SchedulingConflict as exc:
                    assert 'generation is disabled' in str(exc)
                else:
                    raise AssertionError('generation was not blocked')
            db.commit()
            assert before == (period.version, db.scalar(select(func.count()).select_from(SchedulePeriod)))
            for sql in ('UPDATE schedule_generator_snapshots SET origin=origin',
                        'DELETE FROM schedule_generator_snapshots',
                        'TRUNCATE schedule_generator_snapshots'):
                try:
                    with db.begin_nested():
                        db.execute(text(sql))
                except DBAPIError as exc:
                    assert 'immutable audit evidence' in str(exc)
                else:
                    raise AssertionError('evidence mutation was allowed')
            assert schema_contract.current_revision(engine) == '20260928_0028'
print('probe passed:', mode)
engine.dispose()
'''


def run_probe(cwd, mode, url, period_id):
    env = {**os.environ, 'DATABASE_URL': url, 'PYTHONPATH': str(cwd),
           'SQUARE_ACCESS_TOKEN': '', 'SNAPSHOT_PROVIDER': 'mock',
           'SCHEMA_REVISION_CHECK_ENABLED': 'true'}
    result = subprocess.run([sys.executable, '-c', PROBE, mode, str(period_id)],
                            cwd=cwd, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def evidence(engine):
    with engine.connect() as connection:
        return connection.execute(text(
            'SELECT id, payload::text, checksum FROM schedule_generator_snapshots ORDER BY sequence')).all()


def test_actual_application_handoff_preserves_evidence(scheduling_db):
    a1 = os.getenv('A1_SOURCE_DIR')
    if not a1:
        pytest.skip('Set A1_SOURCE_DIR for cross-build PostgreSQL handoff')
    a1 = Path(a1).resolve()
    assert (a1 / 'migrations/versions/20260928_0028_scheduling_generator_snapshots.py').is_file()
    Session, manager, ids, engine = scheduling_db
    with Session() as db:
        _coverage(db, manager, ids)
        period = create_draft_period(db, principal=manager, week_start=date(2026,10,11))
        period_id = period.id
        db.commit()
    url = engine.url.render_as_string(hide_password=False)
    here = Path(__file__).resolve().parents[1]
    run_probe(here, 'rollback_native', url, period_id)
    native = schema_contract.schema_snapshot(engine)
    run_probe(a1, 'a1_create', url, period_id)
    upgraded = schema_contract.schema_snapshot(engine)
    assert set(upgraded['tables']) - set(native['tables']) == {'schedule_generator_snapshots'}
    for name, definition in native['tables'].items():
        assert upgraded['tables'][name] == definition
    assert upgraded['enums'] == native['enums']
    assert upgraded['extensions'] == native['extensions']
    assert [t for t in upgraded['triggers'] if t['table'] != 'schedule_generator_snapshots'] == native['triggers']
    assert len([t for t in upgraded['triggers'] if t['table'] == 'schedule_generator_snapshots']) == 1
    first = evidence(engine)
    assert len(first) == 1
    run_probe(here, 'rollback_a1_schema', url, period_id)
    assert evidence(engine) == first
    run_probe(a1, 'a1_restore', url, period_id)
    restored = evidence(engine)
    assert len(restored) == 2 and restored[:1] == first
