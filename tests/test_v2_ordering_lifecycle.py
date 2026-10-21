from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import Principal, Role
from app.models import OrderingProductLifecycle
from app.routers.v2_ordering import lifecycle_access
from app.services.access_control_service import FALLBACK_ROLE_SET_BY_PERMISSION, permission_defs
from app.services.v2_ordering_lifecycle_service import (
    LifecycleCommand,
    LifecycleSelection,
    LifecycleTransitionError,
    transition_lifecycle,
)


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


class _Db:
    def __init__(self):
        self.added = []
        self.flush_count = 0

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flush_count += 1


def _stored(status='ACTIVE', version=2, pre_archive_status=None):
    return OrderingProductLifecycle(
        square_variation_id='VAR-1',
        status=status,
        row_version=version,
        pre_archive_status=pre_archive_status,
    )


def _run(monkeypatch, *, command, row, expected_version, note=None):
    db = _Db()
    audits = []
    monkeypatch.setattr(
        'app.services.v2_ordering_lifecycle_service.lock_lifecycle_rows',
        lambda _db, _ids: {} if row is None else {row.square_variation_id: row},
    )
    monkeypatch.setattr(
        'app.services.v2_ordering_lifecycle_service.add_lifecycle_row',
        lambda _db, value: db.add(value),
    )
    monkeypatch.setattr(
        'app.services.v2_ordering_lifecycle_service.write_v2_audit_event',
        lambda _db, *, event, ip: audits.append((event, ip)),
    )
    result = transition_lifecycle(
        db,
        command=command,
        selections=(LifecycleSelection('VAR-1', expected_version, 'SKU-1', 'Product 1'),),
        actor_principal_id=4,
        note=note,
        ip='127.0.0.1',
        now=NOW,
    )
    changed = row or db.added[0]
    return result, changed, audits, db


@pytest.mark.parametrize(
    ('command', 'initial', 'pre_archive', 'target'),
    [
        (LifecycleCommand.SET_NO_FUTURE_REORDER, 'ACTIVE', None, 'NO_FUTURE_REORDER'),
        (LifecycleCommand.SET_ACTIVE, 'NO_FUTURE_REORDER', None, 'ACTIVE'),
        (LifecycleCommand.ARCHIVE, 'ACTIVE', None, 'ARCHIVED'),
        (LifecycleCommand.ARCHIVE, 'NO_FUTURE_REORDER', None, 'ARCHIVED'),
        (LifecycleCommand.RESTORE, 'ARCHIVED', 'ACTIVE', 'ACTIVE'),
        (LifecycleCommand.RESTORE, 'ARCHIVED', 'NO_FUTURE_REORDER', 'NO_FUTURE_REORDER'),
        (LifecycleCommand.RESTORE, 'ARCHIVED', None, 'NO_FUTURE_REORDER'),
    ],
)
def test_explicit_transition_matrix(monkeypatch, command, initial, pre_archive, target):
    result, changed, audits, db = _run(
        monkeypatch,
        command=command,
        row=_stored(initial, pre_archive_status=pre_archive),
        expected_version=2,
        note='Owner decision',
    )
    assert changed.status == target
    assert changed.row_version == 3
    assert result.changed_count == 1
    assert len(audits) == 1 and audits[0][0].correlation_id == result.correlation_id
    assert audits[0][0].before['status'] == initial
    assert audits[0][0].after == {'status': target, 'row_version': 3}
    assert db.flush_count == 1
    if command == LifecycleCommand.RESTORE and target == 'NO_FUTURE_REORDER':
        assert changed.no_future_reorder_at == NOW
        assert changed.no_future_reorder_by_principal_id == 4


def test_sparse_absence_means_active_and_first_override_is_version_one(monkeypatch):
    result, changed, _audits, _db = _run(
        monkeypatch,
        command=LifecycleCommand.SET_NO_FUTURE_REORDER,
        row=None,
        expected_version=0,
    )
    assert changed.status == 'NO_FUTURE_REORDER'
    assert changed.row_version == 1
    assert result.versions == (('VAR-1', 1),)


def test_stale_or_duplicate_batch_is_rejected_before_mutation(monkeypatch):
    row = _stored()
    monkeypatch.setattr(
        'app.services.v2_ordering_lifecycle_service.lock_lifecycle_rows',
        lambda _db, _ids: {'VAR-1': row},
    )
    db = _Db()
    with pytest.raises(LifecycleTransitionError, match='changed after') as stale:
        transition_lifecycle(
            db,
            command=LifecycleCommand.ARCHIVE,
            selections=(LifecycleSelection('VAR-1', 1),),
            actor_principal_id=4,
        )
    assert stale.value.code == 'STALE_VERSION'
    assert row.status == 'ACTIVE' and row.row_version == 2
    with pytest.raises(LifecycleTransitionError) as duplicate:
        transition_lifecycle(
            db,
            command=LifecycleCommand.ARCHIVE,
            selections=(LifecycleSelection('VAR-1', 2), LifecycleSelection('VAR-1', 2)),
            actor_principal_id=4,
        )
    assert duplicate.value.code == 'DUPLICATE_VARIATION'


def test_batch_limits_notes_and_invalid_transitions_fail_closed(monkeypatch):
    db = _Db()
    oversized = tuple(LifecycleSelection(f'VAR-{index}', 0) for index in range(251))
    with pytest.raises(LifecycleTransitionError) as too_many:
        transition_lifecycle(
            db,
            command=LifecycleCommand.ARCHIVE,
            selections=oversized,
            actor_principal_id=4,
        )
    assert too_many.value.code == 'OVERSIZED_SELECTION'
    with pytest.raises(LifecycleTransitionError) as note:
        transition_lifecycle(
            db,
            command=LifecycleCommand.ARCHIVE,
            selections=(LifecycleSelection('VAR-1', 0),),
            actor_principal_id=4,
            note='x' * 1001,
        )
    assert note.value.code == 'NOTE_TOO_LONG'
    row = _stored('ARCHIVED', pre_archive_status='ACTIVE')
    monkeypatch.setattr(
        'app.services.v2_ordering_lifecycle_service.lock_lifecycle_rows',
        lambda _db, _ids: {'VAR-1': row},
    )
    with pytest.raises(LifecycleTransitionError) as invalid:
        transition_lifecycle(
            db,
            command=LifecycleCommand.SET_ACTIVE,
            selections=(LifecycleSelection('VAR-1', 2),),
            actor_principal_id=4,
        )
    assert invalid.value.code == 'INVALID_TRANSITION'
    assert db.flush_count == 0


def test_capability_has_no_role_fallback():
    assert 'ordering.lifecycle.manage' in {definition.key for definition in permission_defs()}
    assert 'ordering.lifecycle.manage' not in FALLBACK_ROLE_SET_BY_PERMISSION

    class _Scalar:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _PermissionDb:
        def __init__(self, values):
            self.values = iter(values)

        def execute(self, _query):
            return _Scalar(next(self.values))

    owner = Principal(id=4, username='owner', role=Role.ADMIN, store_id=None, active=True)
    with pytest.raises(HTTPException) as denied:
        lifecycle_access(owner, _PermissionDb([None, None]))
    assert denied.value.status_code == 403
    assert lifecycle_access(owner, _PermissionDb([True])) == owner
