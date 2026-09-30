"""Version 1 generator evidence. Reads never consult live labels or rebuild warnings."""
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.auth import Principal, Role
from app.models import (
    Employee, ScheduleGeneratorSnapshot, SchedulePeriod, ScheduleShift,
    ScheduleShiftType, ScheduleWarning, SchedulingOrganizationPolicy,
    SchedulingStoreDefaults, Store, StoreShift,
)

SCHEMA_VERSION = 1

# Explicit V1 field lists: new model columns do not silently change this contract.
V1_FIELDS = {
    SchedulePeriod: (
        'id', 'week_start_date', 'week_end_date', 'status', 'revision_number',
        'supersedes_schedule_period_id', 'source_schedule_period_id',
        'source_schedule_template_id', 'notes', 'version', 'created_by_principal_id', 'created_at',
        'updated_by_principal_id', 'updated_at', 'published_by_principal_id', 'published_at',
        'lifecycle_stage', 'generated_at', 'automatic_publication_at', 'publication_hold',
        'publication_hold_reason', 'alternating_week',
    ),
    ScheduleShift: (
        'id', 'schedule_period_id', 'employee_id', 'store_id', 'shift_date', 'start_time',
        'end_time', 'unpaid_break_minutes', 'shift_type_id', 'is_opener', 'is_closer',
        'is_lead_of_day', 'lead_of_day_manually_assigned', 'is_double_coverage',
        'double_coverage_manually_assigned', 'employee_note', 'source_shift_id',
        'source_store_shift_id', 'generated_from_coverage_requirement',
        'base_pattern_expected_day', 'base_pattern_deviation_reason', 'created_by_principal_id',
        'updated_by_principal_id', 'created_at', 'updated_at', 'manually_locked',
        'locked_by_principal_id', 'locked_at', 'lock_reason',
    ),
    ScheduleWarning: (
        'id', 'schedule_period_id', 'warning_type', 'severity', 'store_id', 'warning_date',
        'start_time', 'end_time', 'employee_id', 'shift_id', 'required_count', 'actual_count',
        'message', 'evaluated_at',
    ),
    SchedulingOrganizationPolicy: (
        'id', 'weekly_approval_hours', 'schedule_length_weeks', 'generate_days_before_end',
        'publish_days_before_end', 'publication_local_time', 'timezone_name', 'active',
        'updated_by_principal_id', 'updated_at',
    ),
    SchedulingStoreDefaults: (
        'id', 'double_coverage_store_id', 'standard_shift_start', 'standard_shift_end',
        'updated_by_principal_id', 'updated_at',
    ),
}


def json_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def _row(row):
    fields = V1_FIELDS.get(type(row))
    if fields is None:
        fields = tuple(column.key for column in row.__table__.columns)
    return {key: json_value(getattr(row, key)) for key in fields}


def canonical_checksum(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()


def serialize_schedule(db: Session, period: SchedulePeriod) -> dict:
    db.flush()
    shifts = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id).order_by(ScheduleShift.id)))
    employees = {r.id: r.full_name for r in db.scalars(select(Employee).where(
        Employee.id.in_({s.employee_id for s in shifts if s.employee_id is not None})))}
    stores = {r.id: r.name for r in db.scalars(select(Store).where(
        Store.id.in_({s.store_id for s in shifts})))}
    types = {r.id: r.name for r in db.scalars(select(ScheduleShiftType).where(
        ScheduleShiftType.id.in_({s.shift_type_id for s in shifts if s.shift_type_id is not None})))}
    sources = {r.id: r.label for r in db.scalars(select(StoreShift).where(
        StoreShift.id.in_({s.source_store_shift_id for s in shifts if s.source_store_shift_id is not None})))}
    return {
        'period': _row(period),
        'shifts': [{**_row(s), 'employee_name': employees.get(s.employee_id),
                    'store_name': stores.get(s.store_id), 'shift_type_name': types.get(s.shift_type_id),
                    'source_store_shift_label': sources.get(s.source_store_shift_id)} for s in shifts],
        'warnings': [_row(r) for r in db.scalars(select(ScheduleWarning).where(
            ScheduleWarning.schedule_period_id == period.id).order_by(ScheduleWarning.id))],
    }


def capture_snapshot(db: Session, *, period: SchedulePeriod, principal: Principal,
                     batch_id: str, origin: str, before: dict, diagnostics: dict,
                     initial: bool = False) -> ScheduleGeneratorSnapshot:
    # Caller holds the period lock throughout generation and insertion.
    sequence = (db.scalar(select(func.max(ScheduleGeneratorSnapshot.sequence)).where(
        ScheduleGeneratorSnapshot.schedule_period_id == period.id)) or 0) + 1
    build = os.getenv('RENDER_GIT_COMMIT') or os.getenv('GIT_COMMIT') or None
    payload = {
        'schema_version': SCHEMA_VERSION,
        'generator_contract': 'v2-scheduling-snapshot-v1',
        'build_identity': build,
        'generation': {
            'batch_id': batch_id, 'sequence': sequence,
            'kind': 'INITIAL' if initial else 'REGENERATION', 'origin': origin,
            'actor': {'id': principal.id, 'username': principal.username},
            'version_before': before['period']['version'], 'version_after': period.version,
        },
        'before': before,
        'result': serialize_schedule(db, period),
        'diagnostics': json_value(diagnostics),
        'policy': [_row(r) for r in db.scalars(select(SchedulingOrganizationPolicy).order_by(SchedulingOrganizationPolicy.id))],
        'store_defaults': [_row(r) for r in db.scalars(select(SchedulingStoreDefaults).order_by(SchedulingStoreDefaults.id))],
    }
    snapshot = ScheduleGeneratorSnapshot(
        schedule_period_id=period.id, batch_id=batch_id, sequence=sequence,
        generation_kind='INITIAL' if initial else 'REGENERATION',
        actor_principal_id=principal.id, origin=origin,
        version_before=before['period']['version'], version_after=period.version,
        schema_version=SCHEMA_VERSION, build_identity=build,
        payload=payload, checksum=canonical_checksum(payload))
    db.add(snapshot)
    db.flush()
    return snapshot


@event.listens_for(ScheduleGeneratorSnapshot, 'before_update')
@event.listens_for(ScheduleGeneratorSnapshot, 'before_delete')
def _reject_mutation(*_args):
    raise ValueError('Generator snapshots are immutable audit evidence.')


def read_generator_proposals(db: Session, *, principal: Principal,
                             schedule_period_id: int | None = None,
                             week_start: date | None = None,
                             snapshot_id: int | None = None) -> list[dict]:
    """Admin-only detached data, including pre-regeneration payload. No new HTTP API."""
    if not principal.active or principal.role != Role.ADMIN:
        raise PermissionError('Generator proposal evidence requires an active Admin.')
    if schedule_period_id is None and week_start is None and snapshot_id is None:
        raise ValueError('Select a schedule period, week, or proposal.')
    query = select(ScheduleGeneratorSnapshot).join(SchedulePeriod)
    if schedule_period_id is not None:
        query = query.where(ScheduleGeneratorSnapshot.schedule_period_id == schedule_period_id)
    if week_start is not None:
        query = query.where(SchedulePeriod.week_start_date == week_start)
    if snapshot_id is not None:
        query = query.where(ScheduleGeneratorSnapshot.id == snapshot_id)
    return [deepcopy(_row(r)) for r in db.scalars(query.order_by(
        ScheduleGeneratorSnapshot.schedule_period_id, ScheduleGeneratorSnapshot.sequence))]
