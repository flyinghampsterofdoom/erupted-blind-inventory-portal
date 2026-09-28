"""A1 evidence contract, against disposable PostgreSQL, never operational data."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from test_v2_scheduling_foundation import scheduling_db, _coverage, _shift
from app.auth import Principal, Role
from app.models import (AuditLog, Employee, Store, ScheduleGeneratorSnapshot,
                        SchedulePeriod, ScheduleShift, SchedulingOrganizationPolicy)
from app.schema_contract import _alembic_config
from app.services.v2_scheduling_service import (
    create_draft_period, create_shift, update_shift, delete_shift,
    publish_schedule, clone_published_revision,
)
from app.services.v2_scheduling_policy_service import (
    regenerate_period, ensure_rolling_schedule_horizon, set_manual_lock,
)
from app.services.v2_scheduling_snapshot_service import (
    serialize_schedule, canonical_checksum, read_generator_proposals,
)


def prepare(db, manager, ids):
    _coverage(db, manager, ids)
    period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 11))
    return period


def proposals(db, period):
    return list(db.scalars(select(ScheduleGeneratorSnapshot).where(
        ScheduleGeneratorSnapshot.schedule_period_id == period.id).order_by(ScheduleGeneratorSnapshot.sequence)))


def test_initial_edits_regeneration_labels_publication(scheduling_db):
    Session, manager, ids, engine = scheduling_db
    with Session() as db:
        period = prepare(db, manager, ids)
        diagnostics = regenerate_period(db, principal=manager, schedule_period_id=period.id)
        first, = proposals(db, period)
        original = deepcopy(first.payload)
        assert first.sequence == 1 and first.generation_kind == 'INITIAL'
        assert original['result'] == serialize_schedule(db, period)
        assert original['diagnostics'] == diagnostics
        assert canonical_checksum(original) == first.checksum
        assert first.version_after == first.version_before + 1
        audit = db.scalars(select(AuditLog).where(AuditLog.action == 'V2:SCHEDULING:SCHEDULE_REGENERATED')).one()
        assert audit.meta['correlation_id'] == first.batch_id
        assert audit.meta['metadata']['snapshot_id'] == first.id
        shift = db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id == period.id)).first()
        update_shift(db, principal=manager, schedule_period_id=period.id, shift_id=shift.id,
                     expected_version=period.version,
                     values=_shift(ids['blair'], ids['south'], date(2026, 10, 12)),
                     allowed_store_ids=(ids['north'], ids['south']))
        set_manual_lock(db, principal=manager, shift_id=shift.id, locked=False)
        added = create_shift(db, principal=manager, schedule_period_id=period.id,
                             expected_version=period.version, values=_shift(None, ids['north'], date(2026, 10, 13)),
                             allowed_store_ids=(ids['north'], ids['south']))
        delete_shift(db, principal=manager, schedule_period_id=period.id, shift_id=added.shift_id,
                     expected_version=period.version, allowed_store_ids=(ids['north'], ids['south']))
        db.get(Employee, ids['alex']).full_name = 'Renamed Employee'
        db.get(Store, ids['north']).name = 'Renamed Store'
        before = serialize_schedule(db, period)
        regenerate_period(db, principal=manager, schedule_period_id=period.id)
        second = proposals(db, period)[1]
        assert second.generation_kind == 'REGENERATION'
        assert second.payload['before'] == before
        assert second.payload['result'] == serialize_schedule(db, period)
        assert first.payload == original
        regenerate_period(db, principal=manager, schedule_period_id=period.id)
        assert [r.sequence for r in proposals(db, period)] == [1, 2, 3]
        assert first.payload == original
        publish_schedule(db, principal=manager, schedule_period_id=period.id,
                         expected_version=period.version, allowed_store_ids=(ids['north'], ids['south']),
                         allow_serious_warnings=True, confirmed=True, override_reason='Snapshot test')
        clone_published_revision(db, principal=manager, published_period_id=period.id,
                                 allowed_store_ids=(ids['north'], ids['south']))
        db.commit()
        db.expire_all()
        assert db.get(ScheduleGeneratorSnapshot, first.id).payload == original
        admin = Principal(manager.id, manager.username, Role.ADMIN, None, True)
        read = read_generator_proposals(db, principal=admin, week_start=period.week_start_date)
        assert len(read) == 3
        read[0]['payload']['result']['shifts'].clear()
        assert first.payload == original
        with pytest.raises(PermissionError):
            read_generator_proposals(db, principal=manager, snapshot_id=first.id)


def test_multiweek_noop_and_legacy(scheduling_db):
    Session, manager, ids, engine = scheduling_db
    with Session() as db:
        _coverage(db, manager, ids)
        db.add(SchedulingOrganizationPolicy(id=1, schedule_length_weeks=3,
                                           updated_by_principal_id=manager.id))
        now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        result = ensure_rolling_schedule_horizon(db, principal=manager, now=now,
                                               generation_origin='automation')
        rows = list(db.scalars(select(ScheduleGeneratorSnapshot)))
        assert len(rows) == 3 and len({r.batch_id for r in rows}) == 1
        assert {r.origin for r in rows} == {'automation'}
        assert {r.schedule_period_id for r in rows} == set(result['created_period_ids'])
        again = ensure_rolling_schedule_horizon(db, principal=manager, now=now)
        assert again['created_period_ids'] == []
        assert len(list(db.scalars(select(ScheduleGeneratorSnapshot)))) == 3
        legacy = create_draft_period(db, principal=manager, week_start=date(2026, 9, 6))
        legacy.generated_at = now
        regenerate_period(db, principal=manager, schedule_period_id=legacy.id)
        row, = proposals(db, legacy)
        assert row.sequence == 1 and row.generation_kind == 'REGENERATION'
        assert row.payload['before']['period']['generated_at'] == now.isoformat()


@pytest.mark.parametrize('after_insert', [False, True])
def test_snapshot_failure_cannot_commit_generation(scheduling_db, monkeypatch, after_insert):
    Session, manager, ids, engine = scheduling_db
    from app.services import v2_scheduling_snapshot_service as snapshots
    with Session() as db:
        period = prepare(db, manager, ids)
        db.commit()
        before = serialize_schedule(db, period)
        real = snapshots.capture_snapshot
        def fail(*args, **kwargs):
            if after_insert:
                real(*args, **kwargs)
            raise RuntimeError('failure after snapshot insert')
        monkeypatch.setattr(snapshots, 'capture_snapshot', fail)
        with pytest.raises(RuntimeError):
            regenerate_period(db, principal=manager, schedule_period_id=period.id)
        db.commit()  # Even a caller that catches the error cannot commit partial work.
        assert serialize_schedule(db, period) == before
        assert proposals(db, period) == []
        monkeypatch.setattr(snapshots, 'capture_snapshot', real)
        regenerate_period(db, principal=manager, schedule_period_id=period.id)
        db.rollback()
        assert proposals(db, period) == []


@pytest.mark.parametrize('statement', [
    "UPDATE schedule_generator_snapshots SET origin='tampered'",
    'DELETE FROM schedule_generator_snapshots',
    'TRUNCATE schedule_generator_snapshots',
])
def test_database_immutability(scheduling_db, statement):
    Session, manager, ids, engine = scheduling_db
    with Session() as db:
        period = prepare(db, manager, ids)
        regenerate_period(db, principal=manager, schedule_period_id=period.id)
        db.commit()
        with pytest.raises(DBAPIError, match='immutable audit evidence'):
            db.execute(text(statement))
        db.rollback()
        assert len(proposals(db, period)) == 1
    config = _alembic_config(engine.url.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match='snapshot evidence exists'):
        command.downgrade(config, '20260927_0027')


def test_orm_immutability_and_empty_migration_roundtrip(scheduling_db):
    Session, manager, ids, engine = scheduling_db
    with Session() as db:
        legacy = create_draft_period(db, principal=manager, week_start=date(2026, 9, 6))
        db.commit()
        legacy_id = legacy.id
    config = _alembic_config(engine.url.render_as_string(hide_password=False))
    command.downgrade(config, '20260927_0027')
    command.upgrade(config, 'head')
    with Session() as db:
        assert list(db.scalars(select(ScheduleGeneratorSnapshot))) == []
        assert db.get(SchedulePeriod, legacy_id).week_start_date == date(2026, 9, 6)
        period = prepare(db, manager, ids)
        regenerate_period(db, principal=manager, schedule_period_id=period.id)
        row, = proposals(db, period)
        db.commit()
        row.origin = 'tampered'
        with pytest.raises(ValueError, match='immutable'):
            db.flush()
        db.rollback()
        db.delete(row)
        with pytest.raises(ValueError, match='immutable'):
            db.flush()
        db.rollback()


def test_concurrent_regeneration_sequences_and_build(scheduling_db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    Session, manager, ids, engine = scheduling_db
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'test-deployed-build')
    with Session() as db:
        period = prepare(db, manager, ids)
        period_id = period.id
        db.commit()
    barrier = Barrier(2)
    def generate():
        with Session() as db:
            db.get(SchedulePeriod, period_id)  # Simulate a caller with cached period state.
            barrier.wait(timeout=15)
            regenerate_period(db, principal=manager, schedule_period_id=period_id)
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(generate) for _ in range(2)]
        for future in futures:
            future.result(timeout=60)
    with Session() as db:
        rows = proposals(db, db.get(SchedulePeriod, period_id))
        assert [r.sequence for r in rows] == [1, 2]
        assert rows[1].version_before == rows[0].version_after
        assert {r.build_identity for r in rows} == {'test-deployed-build'}


def test_designations_template_replacement_preserve_evidence(scheduling_db):
    from app.services.v2_scheduling_assignments_service import set_lead_of_day, override_double_coverage_employee
    from app.services.v2_scheduling_template_service import save_schedule_template, instantiate_schedule_template
    Session, manager, ids, engine = scheduling_db
    with Session() as db:
        _coverage(db, manager, ids, count=2)
        period = create_draft_period(db, principal=manager, week_start=date(2026, 10, 11))
        regenerate_period(db, principal=manager, schedule_period_id=period.id)
        row, = proposals(db, period)
        evidence = deepcopy(row.payload)
        shifts = list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id == period.id)))
        assigned = next(s for s in shifts if s.employee_id is not None)
        set_lead_of_day(db, principal=manager, shift_id=assigned.id)
        override_double_coverage_employee(db, principal=manager, shift_id=assigned.id, employee_id=assigned.employee_id)
        source = create_draft_period(db, principal=manager, week_start=date(2026, 10, 4))
        create_shift(db, principal=manager, schedule_period_id=source.id, expected_version=source.version,
            values=_shift(None, ids['north'], date(2026, 10, 4)),
            allowed_store_ids=(ids['north'], ids['south']))
        template = save_schedule_template(db, principal=manager, name='Snapshot test',
            source_period_ids=(source.id,), allowed_store_ids=(ids['north'], ids['south']))
        instantiate_schedule_template(db, principal=manager, schedule_template_id=template.id,
            target_week_start=period.week_start_date, mode='REPLACE',
            allowed_store_ids=(ids['north'], ids['south']))
        db.commit()
        db.expire_all()
        assert row.payload == evidence
        assert canonical_checksum(row.payload) == row.checksum


def test_failed_automatic_creation_leaves_no_generated_period(scheduling_db, monkeypatch):
    from app.services import v2_scheduling_snapshot_service as snapshots
    Session, manager, ids, engine = scheduling_db
    with Session() as db:
        _coverage(db, manager, ids)
        db.add(SchedulingOrganizationPolicy(id=1, schedule_length_weeks=3,
                                           updated_by_principal_id=manager.id))
        db.commit()
        def fail(*args, **kwargs):
            raise RuntimeError('snapshot storage unavailable')
        monkeypatch.setattr(snapshots, 'capture_snapshot', fail)
        with pytest.raises(RuntimeError):
            ensure_rolling_schedule_horizon(db, principal=manager,
                now=datetime(2026, 10, 11, 12, tzinfo=timezone.utc), generation_origin='automation')
        db.commit()
        assert list(db.scalars(select(SchedulePeriod))) == []
        assert list(db.scalars(select(ScheduleGeneratorSnapshot))) == []
