from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import OrderingProductLifecycle
from app.services.v2_ordering_lifecycle_repository import (
    ACTIVE,
    ARCHIVED,
    NO_FUTURE_REORDER,
    add_lifecycle_row,
    lock_lifecycle_rows,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event


MAX_BATCH_SIZE = 250
MAX_NOTE_LENGTH = 1000
MAX_SKU_SNAPSHOT_LENGTH = 255
MAX_PRODUCT_NAME_SNAPSHOT_LENGTH = 500


class LifecycleCommand(str, Enum):
    SET_NO_FUTURE_REORDER = 'SET_NO_FUTURE_REORDER'
    SET_ACTIVE = 'SET_ACTIVE'
    ARCHIVE = 'ARCHIVE'
    RESTORE = 'RESTORE'


class LifecycleTransitionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LifecycleSelection:
    square_variation_id: str
    expected_version: int
    sku_snapshot: str | None = None
    product_name_snapshot: str | None = None


@dataclass(frozen=True)
class LifecycleTransitionResult:
    command: LifecycleCommand
    correlation_id: str
    changed_count: int
    versions: tuple[tuple[str, int], ...]


def _target_status(command: LifecycleCommand, current: str, pre_archive: str | None) -> str:
    if command == LifecycleCommand.SET_NO_FUTURE_REORDER and current == ACTIVE:
        return NO_FUTURE_REORDER
    if command == LifecycleCommand.SET_ACTIVE and current == NO_FUTURE_REORDER:
        return ACTIVE
    if command == LifecycleCommand.ARCHIVE and current in {ACTIVE, NO_FUTURE_REORDER}:
        return ARCHIVED
    if command == LifecycleCommand.RESTORE and current == ARCHIVED:
        return pre_archive if pre_archive in {ACTIVE, NO_FUTURE_REORDER} else NO_FUTURE_REORDER
    raise LifecycleTransitionError(
        'INVALID_TRANSITION',
        f'{command.value} is not valid from {current}.',
    )


def transition_lifecycle(
    db: Session,
    *,
    command: LifecycleCommand,
    selections: tuple[LifecycleSelection, ...],
    actor_principal_id: int,
    note: str | None = None,
    ip: str | None = None,
    now: datetime | None = None,
) -> LifecycleTransitionResult:
    """Validate and stage one atomic lifecycle batch; the route owns commit/rollback."""
    if not selections:
        raise LifecycleTransitionError('EMPTY_SELECTION', 'Select at least one product.')
    if len(selections) > MAX_BATCH_SIZE:
        raise LifecycleTransitionError('OVERSIZED_SELECTION', f'At most {MAX_BATCH_SIZE} products may be changed at once.')
    cleaned_note = note.strip() if note and note.strip() else None
    if cleaned_note and len(cleaned_note) > MAX_NOTE_LENGTH:
        raise LifecycleTransitionError('NOTE_TOO_LONG', f'Notes may contain at most {MAX_NOTE_LENGTH} characters.')

    clean_ids = tuple(selection.square_variation_id.strip() for selection in selections)
    if any(not variation_id for variation_id in clean_ids):
        raise LifecycleTransitionError('MISSING_IDENTITY', 'Every selected product requires a Square variation ID.')
    if len(set(clean_ids)) != len(clean_ids):
        raise LifecycleTransitionError('DUPLICATE_VARIATION', 'A product may appear only once in a batch.')
    if actor_principal_id <= 0:
        raise LifecycleTransitionError('UNAUTHORIZED_SCOPE', 'An authenticated principal is required.')
    if any(len((selection.sku_snapshot or '').strip()) > MAX_SKU_SNAPSHOT_LENGTH for selection in selections):
        raise LifecycleTransitionError('SNAPSHOT_TOO_LONG', 'A SKU snapshot exceeds the supported length.')
    if any(
        len((selection.product_name_snapshot or '').strip()) > MAX_PRODUCT_NAME_SNAPSHOT_LENGTH
        for selection in selections
    ):
        raise LifecycleTransitionError('SNAPSHOT_TOO_LONG', 'A product-name snapshot exceeds the supported length.')

    locked = lock_lifecycle_rows(db, clean_ids)
    planned: list[tuple[LifecycleSelection, OrderingProductLifecycle | None, str, str]] = []
    for selection, variation_id in zip(selections, clean_ids, strict=True):
        row = locked.get(variation_id)
        current = row.status if row else ACTIVE
        current_version = row.row_version if row else 0
        if selection.expected_version != current_version:
            raise LifecycleTransitionError('STALE_VERSION', f'{variation_id} changed after this page was loaded.')
        target = _target_status(command, current, row.pre_archive_status if row else None)
        planned.append((selection, row, current, target))

    changed_at = now or datetime.now(tz=timezone.utc)
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    correlation_id = uuid4().hex
    versions: list[tuple[str, int]] = []
    for selection, row, current, target in planned:
        variation_id = selection.square_variation_id.strip()
        if row is None:
            row = OrderingProductLifecycle(square_variation_id=variation_id, status=ACTIVE, row_version=1)
            add_lifecycle_row(db, row)
            new_version = 1
        else:
            row.row_version += 1
            new_version = row.row_version
        if selection.sku_snapshot and selection.sku_snapshot.strip():
            row.sku_snapshot = selection.sku_snapshot.strip()
        if selection.product_name_snapshot and selection.product_name_snapshot.strip():
            row.product_name_snapshot = selection.product_name_snapshot.strip()
        row.status_note = cleaned_note
        if command == LifecycleCommand.SET_NO_FUTURE_REORDER:
            row.no_future_reorder_at = changed_at
            row.no_future_reorder_by_principal_id = actor_principal_id
        elif command == LifecycleCommand.ARCHIVE:
            row.pre_archive_status = current
            row.archived_at = changed_at
            row.archived_by_principal_id = actor_principal_id
        elif command == LifecycleCommand.RESTORE:
            row.restored_at = changed_at
            row.restored_by_principal_id = actor_principal_id
            if target == NO_FUTURE_REORDER and (
                row.no_future_reorder_at is None or row.no_future_reorder_by_principal_id is None
            ):
                row.no_future_reorder_at = changed_at
                row.no_future_reorder_by_principal_id = actor_principal_id
        row.status = target
        write_v2_audit_event(
            db,
            event=V2AuditEvent(
                actor_principal_id=actor_principal_id,
                action='lifecycle_status_changed',
                domain='ordering_lifecycle',
                entity_type='square_variation',
                entity_id=variation_id,
                timestamp=changed_at,
                before={'status': current, 'row_version': selection.expected_version},
                after={'status': target, 'row_version': new_version},
                reason=cleaned_note,
                correlation_id=correlation_id,
                metadata={'command': command.value},
            ),
            ip=ip,
        )
        versions.append((variation_id, new_version))

    db.flush()
    return LifecycleTransitionResult(command, correlation_id, len(planned), tuple(versions))
