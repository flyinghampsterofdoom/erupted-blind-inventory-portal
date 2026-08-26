from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import Employee, ScheduleShift
from app.services.employee_log_service import normalize_name
from app.v2.audit import V2AuditEvent, write_v2_audit_event


SQUARE_INACTIVE = 'INACTIVE'


class SquareTeamMembersClient(Protocol):
    def post(self, path: str, payload: dict) -> dict: ...


@dataclass(frozen=True)
class SquareTeamMemberSnapshot:
    team_member_id: str
    full_name: str
    status: str
    location_assignment: str | None
    location_ids: tuple[str, ...]


@dataclass(frozen=True)
class RosterSyncResult:
    added: int
    updated: int
    removed: int
    unchanged: int

    @property
    def message(self) -> str:
        return (
            f'{self.added} employees added, {self.updated} updated, '
            f'{self.removed} removed, {self.unchanged} unchanged.'
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _member_name(member: dict) -> str:
    full_name = ' '.join(part for part in (
        str(member.get('given_name') or '').strip(),
        str(member.get('family_name') or '').strip(),
    ) if part).strip()
    return full_name or str(member.get('email_address') or member.get('id') or '').strip()


def _snapshot(member: dict) -> SquareTeamMemberSnapshot | None:
    team_member_id = str(member.get('id') or '').strip()
    full_name = _member_name(member)
    if not team_member_id or not full_name:
        return None
    assigned = member.get('assigned_locations') if isinstance(member.get('assigned_locations'), dict) else {}
    assignment = str(assigned.get('assignment_type') or '').strip().upper() or None
    location_ids = tuple(sorted({
        str(value).strip() for value in (assigned.get('location_ids') or []) if str(value).strip()
    }))
    return SquareTeamMemberSnapshot(
        team_member_id=team_member_id,
        full_name=full_name,
        status=str(member.get('status') or 'UNKNOWN').strip().upper(),
        location_assignment=assignment,
        location_ids=location_ids,
    )


def fetch_square_team_members(client: SquareTeamMembersClient) -> tuple[SquareTeamMemberSnapshot, ...]:
    by_id: dict[str, SquareTeamMemberSnapshot] = {}
    cursor: str | None = None
    while True:
        payload: dict[str, object] = {'limit': 200}
        if cursor:
            payload['cursor'] = cursor
        response = client.post('/v2/team-members/search', payload)
        for member in response.get('team_members', []) or []:
            if not isinstance(member, dict):
                continue
            snapshot = _snapshot(member)
            if snapshot is not None:
                by_id[snapshot.team_member_id] = snapshot
        cursor = str(response.get('cursor') or '').strip() or None
        if cursor is None:
            break
    return tuple(by_id[key] for key in sorted(by_id))


def sync_square_scheduling_roster(
    db: Session, *, principal: Principal, client: SquareTeamMembersClient | None = None,
) -> RosterSyncResult:
    if client is None:
        # Reuse the repository's current read-only Team Members API transport.
        from app.services.sales_transactions_report_service import _SquareClient
        client = _SquareClient()
    snapshots = fetch_square_team_members(client)
    existing = list(db.execute(select(Employee).with_for_update()).scalars())
    by_square_id = {row.square_team_member_id: row for row in existing if row.square_team_member_id}
    unlinked_by_name = {
        row.normalized_name: row for row in existing if row.square_team_member_id is None
    }
    incoming_name_counts = Counter(normalize_name(row.full_name) for row in snapshots)
    reserved_names = {row.normalized_name: row for row in existing}
    added = updated = unchanged = 0
    captured_at = _now()
    for snapshot in snapshots:
        normalized = normalize_name(snapshot.full_name)
        row = by_square_id.get(snapshot.team_member_id)
        created = False
        # A name bridge is safe only when Square supplies exactly one Team Member with that
        # normalized name. Same-name Team Members remain distinct through their stable Square IDs.
        if row is None and incoming_name_counts[normalized] == 1:
            row = unlinked_by_name.pop(normalized, None)
        if row is None:
            row = Employee(
                full_name=snapshot.full_name,
                normalized_name=normalized,
                visible_to_leads=True,
                active=True,
                scheduling_active=False,
                created_by_principal_id=principal.id,
            )
            db.add(row)
            created = True
        if reserved_names.get(row.normalized_name) is row:
            reserved_names.pop(row.normalized_name)
        unique_normalized = normalized
        if unique_normalized in reserved_names:
            unique_normalized = f'{normalized} [square:{snapshot.team_member_id.lower()}]'
        suffix = 2
        while unique_normalized in reserved_names:
            unique_normalized = f'{normalized} [square:{snapshot.team_member_id.lower()}:{suffix}]'
            suffix += 1
        reserved_names[unique_normalized] = row
        before = (
            row.square_team_member_id, row.full_name, row.normalized_name, row.square_status,
            row.square_location_assignment, tuple(row.square_location_ids or ()),
        )
        row.square_team_member_id = snapshot.team_member_id
        row.full_name = snapshot.full_name
        row.normalized_name = unique_normalized
        row.square_status = snapshot.status
        row.square_location_assignment = snapshot.location_assignment
        row.square_location_ids = list(snapshot.location_ids)
        row.square_synced_at = captured_at
        after = (
            row.square_team_member_id, row.full_name, row.normalized_name, row.square_status,
            row.square_location_assignment, tuple(row.square_location_ids or ()),
        )
        by_square_id[snapshot.team_member_id] = row
        if created:
            added += 1
        elif before != after:
            updated += 1
        else:
            unchanged += 1
    result = RosterSyncResult(added=added, updated=updated, removed=0, unchanged=unchanged)
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id,
        action='SQUARE_EMPLOYEE_ROSTER_SYNCED',
        domain='SCHEDULING',
        entity_type='employee_roster',
        entity_id='square',
        timestamp=captured_at,
        metadata={
            'added': result.added, 'updated': result.updated,
            'removed': result.removed, 'unchanged': result.unchanged,
        },
    ), ip=None)
    db.flush()
    return result


def set_scheduling_participation(
    db: Session, *, principal: Principal, employee_id: int, active: bool,
) -> Employee:
    row = db.execute(select(Employee).where(Employee.id == employee_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ValueError('Employee not found.')
    before = bool(row.scheduling_active)
    row.scheduling_active = active
    row.updated_at = _now()
    if before != active:
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='EMPLOYEE_SCHEDULING_STATUS_CHANGED',
            domain='SCHEDULING',
            entity_type='employee',
            entity_id=row.id,
            timestamp=_now(),
            before={'scheduling_active': before},
            after={'scheduling_active': active},
            metadata={'square_team_member_id': row.square_team_member_id},
        ), ip=None)
    db.flush()
    if before != active:
        period_ids = tuple(db.execute(select(ScheduleShift.schedule_period_id).where(
            ScheduleShift.employee_id == row.id).distinct()).scalars())
        if period_ids:
            from app.models import SchedulePeriod, SchedulePeriodStatus
            from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
            from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
            periods = list(db.execute(select(SchedulePeriod).where(
                SchedulePeriod.id.in_(period_ids))).scalars())
            for period in periods:
                # Published history is never silently rewritten. It receives a
                # serious warning; draft designations are repaired when possible.
                if period.status == SchedulePeriodStatus.DRAFT:
                    reconcile_lead_designations(db, schedule_period_id=period.id)
                rebuild_schedule_warnings(db, schedule_period_id=period.id)
    return row


def set_scheduling_capabilities(
    db: Session, *, principal: Principal, employee_id: int,
    lead_capable: bool | None = None, double_coverage: bool | None = None,
) -> Employee:
    row = db.execute(select(Employee).where(Employee.id == employee_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ValueError('Employee not found.')
    before = {
        'scheduling_lead_capable': bool(row.scheduling_lead_capable),
        'scheduling_double_coverage': bool(row.scheduling_double_coverage),
    }
    if lead_capable is not None:
        row.scheduling_lead_capable = lead_capable
    if double_coverage is not None:
        row.scheduling_double_coverage = double_coverage
    after = {
        'scheduling_lead_capable': bool(row.scheduling_lead_capable),
        'scheduling_double_coverage': bool(row.scheduling_double_coverage),
    }
    row.updated_at = _now()
    if before != after:
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='EMPLOYEE_SCHEDULING_CAPABILITIES_CHANGED',
            domain='SCHEDULING', entity_type='employee', entity_id=row.id,
            timestamp=_now(), before=before, after=after,
            metadata={'square_team_member_id': row.square_team_member_id},
        ), ip=None)
    db.flush()
    if before != after:
        period_ids = tuple(db.execute(select(ScheduleShift.schedule_period_id).where(
            ScheduleShift.employee_id == row.id).distinct()).scalars())
        if period_ids:
            from app.models import SchedulePeriod, SchedulePeriodStatus
            from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
            from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
            for period in db.execute(select(SchedulePeriod).where(
                    SchedulePeriod.id.in_(period_ids))).scalars():
                if period.status == SchedulePeriodStatus.DRAFT:
                    reconcile_lead_designations(db, schedule_period_id=period.id)
                rebuild_schedule_warnings(db, schedule_period_id=period.id)
    return row


def square_allows_scheduling(employee: Employee) -> bool:
    return str(employee.square_status or '').upper() != SQUARE_INACTIVE


def is_scheduling_candidate(employee: Employee) -> bool:
    return bool(employee.active and employee.scheduling_active and square_allows_scheduling(employee))


def list_scheduling_candidates(db: Session) -> list[Employee]:
    """The autoscheduler's explicit root candidate set; principals are not consulted."""
    return list(db.execute(select(Employee).where(
        Employee.active.is_(True),
        Employee.scheduling_active.is_(True),
        or_(Employee.square_status.is_(None), Employee.square_status != SQUARE_INACTIVE),
    ).order_by(Employee.id)).scalars())
