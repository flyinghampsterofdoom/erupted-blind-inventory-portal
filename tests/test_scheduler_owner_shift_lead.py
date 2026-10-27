"""Real PostgreSQL qualification for Owner completion and scoped shift Lead duty."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, func
from app import models as m
from app.auth import Principal, Role
from app.services import scheduling_shift_lead_service as leads
from app.services import v2_scheduling_service as schedule
from app.services.scheduling_completion_service import approve_and_publish_schedule
from app.services.v2_scheduling_exception_service import (
    record_exception, record_commitment, cancel_commitment, effective_shift_segments,
)
from app.services.v2_scheduling_swap_service import change_assignments
from app.services.v2_scheduling_policy_service import organization_policy
from test_scheduling_lifecycle_integrity import complete, args, values, DAY
from test_v2_scheduling_foundation import scheduling_db
from test_v2_lead_duty import period, shift
from test_v2_primary import primary, login


def owner(db):
    row = m.Principal(username='owner-scheduler', role=m.PrincipalRole.ADMIN, active=True)
    db.add(row); db.flush()
    return Principal(row.id, row.username, Role.ADMIN, None, True)


def local_now(db, day, hour):
    return datetime.combine(day, time(hour), ZoneInfo(organization_policy(db).timezone_name))


@pytest.mark.parametrize('lead_available', [False, True])
def test_owner_final_approval_executes_without_lead_attestation(scheduling_db, lead_available):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per, rows = complete(db, manager, ids)
        for row in rows: row.is_lead_of_day = lead_available
        if not lead_available:
            for employee in db.scalars(select(m.Employee)): employee.scheduling_lead_capable = False
        db.commit()
        approve_and_publish_schedule(db, **args(per, actor, ids)); db.commit()
        assert per.status == m.SchedulePeriodStatus.PUBLISHED
        approvals = list(db.scalars(select(m.ScheduleApproval)))
        assert len(approvals) == 1 and approvals[0].actor_principal_id == actor.id
        audit = db.scalar(select(m.AuditLog).where(m.AuditLog.action == 'V2:SCHEDULING:SCHEDULE_APPROVED'))
        assert any(w['type'] == 'NO_LEAD_OF_DAY' for w in audit.meta['metadata']['overridden_warnings']) == (not lead_available)


@pytest.mark.parametrize('safeguard', ['inactive', 'scope', 'denied', 'invalid_time'])
def test_owner_lead_precedence_preserves_unrelated_guards(scheduling_db, safeguard):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per, rows = complete(db, manager, ids)
        rows[0].is_lead_of_day = False
        kw = args(per, actor, ids)
        if safeguard == 'inactive': db.get(m.Employee, rows[0].employee_id).active = False
        elif safeguard == 'scope': kw['allowed_store_ids'] = (ids['south'],)
        elif safeguard == 'denied': db.add(m.PrincipalPermissionOverride(principal_id=actor.id, permission_key='scheduling.approve', allowed=False))
        else: rows[0].unpaid_break_minutes = 999
        db.commit()
        with pytest.raises((PermissionError, schedule.SchedulingValidationError)):
            approve_and_publish_schedule(db, **kw)
        db.commit()
        assert per.status == m.SchedulePeriodStatus.DRAFT
        assert db.scalar(select(func.count()).select_from(m.ScheduleApproval)) == 0


@pytest.mark.parametrize('permanent_lead', [False, True])
def test_assign_remove_nonlead_and_existing_lead_without_role_changes(scheduling_db, permanent_lead):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per, rows = complete(db, manager, ids); row = rows[0]
        account = m.Principal(username='shift-lead', role=m.PrincipalRole.LEAD if permanent_lead else m.PrincipalRole.STORE, active=True)
        db.add(account); db.flush()
        person = db.get(m.Employee, row.employee_id); person.principal_id = account.id
        person.scheduling_lead_capable = permanent_lead; db.commit()
        schedule.update_shift(db, **args(per, actor, ids), shift_id=row.id, values=values(row), manual_edit=True, lead_designation='ASSIGN')
        assert row.is_shift_lead
        schedule.update_shift(db, **args(per, actor, ids), shift_id=row.id, values=values(row), manual_edit=True, lead_designation='REMOVE')
        db.commit()
        assert not row.is_shift_lead and person.scheduling_lead_capable == permanent_lead
        assert account.role == (m.PrincipalRole.LEAD if permanent_lead else m.PrincipalRole.STORE)
        events = list(db.scalars(select(m.AuditLog.action)))
        assert 'V2:SCHEDULING:SHIFT_LEAD_ASSIGNED' in events and 'V2:SCHEDULING:SHIFT_LEAD_REMOVED' in events


def test_move_lead_and_reassign_original_location_atomically(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per, rows = complete(db, manager, ids); row = rows[0]
        backup = shift(db, manager, per, ids['blair'], ids['north'], row.shift_date, lead=False)
        backup.start_time, backup.end_time = row.start_time, row.end_time
        row.is_shift_lead = True; db.get(m.Employee, ids['blair']).scheduling_lead_capable = False; db.commit()
        schedule.update_shift(db, **args(per, actor, ids), shift_id=row.id,
            values=values(row, store_id=ids['south']), manual_edit=True,
            lead_designation='KEEP', reassign_lead_shift_id=backup.id)
        db.commit()
        assert row.store_id == ids['south'] and row.is_shift_lead and backup.is_shift_lead
        assert not db.get(m.Employee, ids['blair']).scheduling_lead_capable


def test_failed_backup_assignment_rolls_back_move_and_audit_even_if_caller_commits(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per, rows = complete(db, manager, ids); row = rows[0]
        row.is_shift_lead = True; db.commit(); version = per.version
        count = db.scalar(select(func.count()).select_from(m.AuditLog))
        with pytest.raises(schedule.SchedulingValidationError):
            schedule.update_shift(db, **args(per, actor, ids), shift_id=row.id,
                values=values(row, store_id=ids['south']), manual_edit=True,
                lead_designation='REMOVE', reassign_lead_shift_id=999999)
        db.commit(); db.refresh(row)
        assert row.store_id == ids['north'] and row.is_shift_lead and per.version == version
        assert db.scalar(select(func.count()).select_from(m.AuditLog)) == count


def test_late_audit_failure_rolls_back_assignment_and_designation(scheduling_db, monkeypatch):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per, rows = complete(db, manager, ids); row = rows[0]; db.commit(); version = per.version
        def fail(*a, **kw): raise RuntimeError('Injected audit failure')
        monkeypatch.setattr(schedule, 'write_v2_audit_event', fail)
        with pytest.raises(RuntimeError):
            schedule.update_shift(db, **args(per, actor, ids), shift_id=row.id,
                values=values(row, store_id=ids['south']), manual_edit=True, lead_designation='ASSIGN')
        db.commit(); db.refresh(row)
        assert not row.is_shift_lead and row.store_id == ids['north'] and per.version == version


@pytest.mark.parametrize('revoke', ['end', 'remove', 'absence', 'archive', 'cleared', 'inactive', 'denied'])
def test_temporary_authority_expires_and_revokes_without_role_mutation(scheduling_db, revoke):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); day = date(2030, 5, 14); per = period(db, actor, day)
        assignment = shift(db, actor, per, ids['alex'], ids['north'], day, lead=False)
        assignment.is_shift_lead = True
        account = m.Principal(username='temporary', role=m.PrincipalRole.STORE, store_id=ids['south'], active=True)
        db.add(account); db.flush(); db.get(m.Employee, ids['alex']).principal_id = account.id; db.flush()
        temporary = Principal(account.id, account.username, Role.STORE, ids['south'], True)
        kw = dict(principal=temporary, permission_key='scheduling.attendance.record', shift=assignment, now=local_now(db, day, 12))
        assert leads.has_shift_lead_permission(db, **kw)
        if revoke == 'end': kw['now'] = local_now(db, day, 17)
        elif revoke == 'remove': assignment.is_shift_lead = False
        elif revoke == 'absence':
            record_exception(db, principal=actor, shift_id=assignment.id, event_type=m.AttendanceEventType.CALLED_OUT, today=day)
        elif revoke == 'archive': per.status = m.SchedulePeriodStatus.ARCHIVED
        elif revoke == 'cleared': per.cleared_at = datetime.now(timezone.utc)
        elif revoke == 'inactive': db.get(m.Employee, ids['alex']).active = False
        else: db.add(m.PrincipalPermissionOverride(principal_id=account.id, permission_key='scheduling.attendance.record', allowed=False))
        db.flush()
        assert not leads.has_shift_lead_permission(db, **kw)
        assert account.role == m.PrincipalRole.STORE


def test_scoped_authority_does_not_reach_other_locations_days_or_admin_permissions(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); day = date(2030, 5, 14); per = period(db, actor, day)
        own = shift(db, actor, per, ids['alex'], ids['north'], day, lead=False); own.is_shift_lead = True
        other = shift(db, actor, per, ids['blair'], ids['south'], day, lead=False)
        tomorrow = shift(db, actor, per, ids['blair'], ids['north'], day + timedelta(days=1), lead=False)
        account = m.Principal(username='scoped', role=m.PrincipalRole.STORE, store_id=ids['north'], active=True)
        db.add(account); db.flush(); db.get(m.Employee, ids['alex']).principal_id = account.id; db.flush()
        temp = Principal(account.id, account.username, Role.STORE, ids['north'], True)
        kw = dict(principal=temp, permission_key='scheduling.attendance.record', now=local_now(db, day, 12))
        assert leads.has_shift_lead_permission(db, shift=own, **kw)
        assert not leads.has_shift_lead_permission(db, shift=other, **kw)
        assert not leads.has_shift_lead_permission(db, shift=tomorrow, **kw)
        for key in ('scheduling.publish', 'scheduling.edit_draft_shifts', 'scheduling.lead_duty.resolve', 'access_control.manage'):
            assert not leads.has_shift_lead_permission(db, principal=temp, permission_key=key, shift=own, now=kw['now'])
        other.store_id = ids['north']; other.end_time = time(18); db.flush()
        assert not leads.has_shift_lead_permission(db, shift=other, **kw)


def test_coverage_lead_replacement_partial_expiration_and_cancel(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); day = date(2030, 5, 14); per = period(db, actor, day)
        source = shift(db, actor, per, ids['alex'], ids['north'], day, lead=False)
        account = m.Principal(username='relief', role=m.PrincipalRole.STORE, active=True)
        db.add(account); db.flush(); db.get(m.Employee, ids['blair']).principal_id = account.id; db.flush()
        temp = Principal(account.id, account.username, Role.STORE, None, True)
        record_exception(db, principal=actor, shift_id=source.id, event_type=m.AttendanceEventType.CALLED_OUT,
            replacement_employee_id=ids['blair'], coverage_start_time=time(12), coverage_end_time=time(15),
            lead_designation='ASSIGN', today=day)
        segment, = effective_shift_segments(db, source)
        assert segment.is_shift_lead
        kw = dict(principal=temp, permission_key='scheduling.attendance.record', shift=segment)
        assert leads.has_shift_lead_permission(db, now=local_now(db, day, 13), **kw)
        assert not leads.has_shift_lead_permission(db, now=local_now(db, day, 15), **kw)
        revised = record_commitment(db, principal=actor, shift_id=source.id, employee_id=ids['blair'],
            start_time=time(12), end_time=time(15), lead_designation='REMOVE', today=day)
        assert not revised.is_shift_lead
        assert not leads.has_shift_lead_permission(db, now=local_now(db, day, 13), **kw)
        revised = record_commitment(db, principal=actor, shift_id=source.id, employee_id=ids['blair'],
            start_time=time(12), end_time=time(15), lead_designation='ASSIGN', today=day)
        cancel_commitment(db, principal=actor, commitment_id=revised.id)
        assert not leads.has_shift_lead_permission(db, now=local_now(db, day, 13), **kw)


def test_nonowner_cannot_grant_or_extend_temporary_lead(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        per, rows = complete(db, manager, ids); row = rows[0]; db.commit()
        with pytest.raises(PermissionError):
            schedule.update_shift(db, **args(per, manager, ids), shift_id=row.id, values=values(row),
                manual_edit=True, lead_designation='ASSIGN')
        db.commit(); assert not row.is_shift_lead
        row.is_shift_lead = True; db.commit()
        schedule.update_shift(db, **args(per, manager, ids), shift_id=row.id,
            values=values(row, store_id=ids['south']), manual_edit=True)
        assert not row.is_shift_lead


def test_owner_published_replacement_assigns_nonlead_in_one_atomic_completion(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per, rows = complete(db, manager, ids); row = rows[0]
        db.get(m.Employee, ids['blair']).scheduling_lead_capable = False
        approve_and_publish_schedule(db, **args(per, actor, ids)); db.commit()
        result = change_assignments(db, principal=actor, shift_id=row.id,
            replacement_employee_id=ids['blair'], reason='Owner designates relief Lead',
            expected_versions={str(per.id): per.version}, allowed_store_ids=(ids['north'], ids['south']),
            lead_designation='ASSIGN', operational_override=True, now=local_now(db, DAY - timedelta(days=1), 12))
        db.commit()
        revised = db.get(m.SchedulePeriod, result['period_ids'][0])
        replacement = db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==revised.id,
            m.ScheduleShift.source_shift_id==row.id))
        assert revised.status == m.SchedulePeriodStatus.PUBLISHED and per.status == m.SchedulePeriodStatus.ARCHIVED
        assert replacement.employee_id == ids['blair'] and replacement.is_shift_lead
        assert not db.get(m.Employee, ids['blair']).scheduling_lead_capable


def test_owner_shift_ui_and_http_designation(primary):
    primary.account('ADMIN')
    with primary.Session() as db:
        per, rows = complete(db, primary.manager, primary.ids); row=rows[0]; pid=per.id; sid=row.id; version=per.version
        payload = dict(expected_version=version, employee_id=row.employee_id, store_id=row.store_id,
            shift_date=str(row.shift_date), start_time=row.start_time.isoformat(), end_time=row.end_time.isoformat(),
            unpaid_break_minutes=row.unpaid_break_minutes, lead_designation='ASSIGN'); db.commit()
    login(primary); c=primary.client
    html=c.get('/v2/scheduling/week?start='+str(DAY)).text
    assert html.count('Lead for this shift') >= 4
    assert 'Keep existing designation' in html and 'Reassign Lead at original location' in html
    result=c.patch(f'/v2/scheduling/api/periods/{pid}/shifts/{sid}', json=payload,
        headers={'X-CSRF-Token':c.cookies.get('csrf_token')})
    assert result.status_code == 200, result.text
    assert result.json()['shift']['is_shift_lead']
    html=c.get('/v2/scheduling/week?start='+str(DAY)).text
    assert '>LEAD</strong>' in html


def test_temporary_lead_http_uses_local_scope_and_expires(primary, monkeypatch):
    from app.services.access_control_service import principal_has_permission
    uid = primary.account('STORE')
    day = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    class DutyClock(datetime):
        hour = 12
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(day, time(cls.hour), ZoneInfo('America/Los_Angeles')).astimezone(tz or timezone.utc)
    monkeypatch.setattr(leads, 'datetime', DutyClock)
    with primary.Session() as db:
        actor = owner(db); per = period(db, actor, day)
        duty = shift(db, actor, per, primary.ids['alex'], primary.ids['south'], day, lead=False)
        duty.is_shift_lead = True
        source = shift(db, actor, per, primary.ids['blair'], primary.ids['south'], day, lead=False)
        unrelated = shift(db, actor, per, primary.ids['blair'], primary.ids['north'], day, lead=False)
        relief = m.Employee(full_name='Relief', normalized_name='relief', active=True)
        db.add(relief); db.flush(); eid=relief.id; sid=source.id; wrong=unrelated.id; db.commit()
    login(primary); c=primary.client
    assert c.get(f'/v2/scheduling/api/shifts/{sid}/coverage-candidates').status_code == 200
    assert c.get(f'/v2/scheduling/api/shifts/{wrong}/coverage-candidates').status_code == 403
    page = c.get('/v2/scheduling/week?start='+str(day))
    assert page.status_code == 200 and 'data-callout-open' in page.text
    result = c.post(f'/v2/scheduling/api/shifts/{sid}/attendance',
        json={'commitment_employee_id': eid}, headers={'X-CSRF-Token':c.cookies.get('csrf_token')})
    assert result.status_code == 201, result.text
    with primary.Session() as db:
        account = db.get(m.Principal, uid)
        temp = Principal(account.id, account.username, Role.STORE, account.store_id, True)
        assert account.role == m.PrincipalRole.STORE
        assert not principal_has_permission(db, principal=temp, permission_key='scheduling.publish', fallback_allowed=False)
    DutyClock.hour = 17
    assert c.get(f'/v2/scheduling/api/shifts/{sid}/coverage-candidates').status_code == 403
    result = c.post(f'/v2/scheduling/api/shifts/{sid}/attendance',
        json={'cancel_coverage': True}, headers={'X-CSRF-Token':c.cookies.get('csrf_token')})
    assert result.status_code == 403


@pytest.mark.parametrize('reviewer_role', ['ADMIN', 'LEAD'])
def test_owner_or_lead_review_executes_consented_request(primary, reviewer_role):
    from test_atomic_employee_swaps import setup, request
    from app.services import scheduling_shift_request_service as requests
    a, b, pid, sids = setup(primary)
    with primary.Session() as db:
        profile = db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id == primary.ids['blair']))
        profile.approval_weekly_hours = 1
        reviewer_row = m.Principal(username='reviewer', role=m.PrincipalRole(reviewer_role), active=True)
        db.add(reviewer_row); db.flush()
        reviewer = Principal(reviewer_row.id, reviewer_row.username, Role(reviewer_role), None, True)
        db.commit()
    rid = request(primary, a, sids)
    with primary.Session() as db:
        pending = requests.respond_request(db, principal=b, request_id=rid, accept=True); db.commit()
        assert pending.status == m.ShiftTransferStatus.PENDING_MANAGER
        completed = requests.review_request(db, principal=reviewer, request_id=rid, approve=True)
        db.commit()
        assert completed.status == m.ShiftTransferStatus.COMPLETED
        assert completed.manager_principal_id == reviewer.id


def test_owner_legacy_request_finishes_when_lead_becomes_unavailable(scheduling_db):
    from app.services.scheduling_shift_request_service import review_request
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per = period(db, actor, DAY)
        source = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        db.get(m.Employee, ids['blair']).scheduling_lead_capable = False
        row = m.ShiftTransferRequest(shift_id=source.id, from_employee_id=ids['alex'],
            to_employee_id=ids['blair'], initiated_by_principal_id=actor.id,
            status=m.ShiftTransferStatus.PENDING_MANAGER)
        db.add(row); db.commit()
        review_request(db, principal=actor, request_id=row.id, approve=True,
            today=DAY-timedelta(days=1))
        db.commit()
        assert source.employee_id == ids['blair'] and not source.is_lead_of_day
        assert row.status == m.ShiftTransferStatus.COMPLETED


def test_published_store_swap_updates_both_lead_choices_atomically(scheduling_db):
    from test_v2_scheduling_prebeta import ready_period
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        actor = owner(db); per, rows = ready_period(db, manager, ids, day=DAY, two=True)
        rows[1].is_shift_lead = True
        approve_and_publish_schedule(db, **args(per, actor, ids)); db.commit()
        result = change_assignments(db, principal=actor, shift_id=rows[0].id, other_shift_id=rows[1].id,
            kind='STORE', reason='Owner assigns the destination Lead', expected_versions={str(per.id):per.version},
            allowed_store_ids=(ids['north'], ids['south']), lead_designation='ASSIGN',
            other_lead_designation='REMOVE', now=local_now(db, DAY-timedelta(days=1), 12))
        db.commit()
        revised = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==result['period_ids'][0]).order_by(m.ScheduleShift.id)))
        first = next(s for s in revised if s.source_shift_id==rows[0].id)
        second = next(s for s in revised if s.source_shift_id==rows[1].id)
        assert first.store_id==ids['south'] and first.is_shift_lead
        assert second.store_id==ids['north'] and not second.is_shift_lead
        assert db.get(m.SchedulePeriod, result['period_ids'][0]).status==m.SchedulePeriodStatus.PUBLISHED
