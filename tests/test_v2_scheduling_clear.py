"""Real PostgreSQL acceptance for evidence-preserving administrative resets."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import re

import pytest
from sqlalchemy import select, func
from app import models as m
from app.auth import Principal, Role
from app.services.v2_scheduling_clear_service import preview_clear, clear_schedules
from app.services.v2_scheduling_service import (SchedulingConflict, SchedulingValidationError,
    approve_schedule, publish_schedule, create_draft_period, create_shift, update_shift, ShiftInput)
from test_v2_scheduling_foundation import scheduling_db, _coverage, date_hours
from test_v2_scheduling_prebeta import ready_period
from test_v2_primary import primary, login, post

NOW = datetime(2026, 10, 20, 18, tzinfo=timezone.utc)


def admin(actor):
    return Principal(actor.id, actor.username, Role.ADMIN, None, True)


def publish(db, actor, ids, period):
    args = dict(principal=actor, schedule_period_id=period.id, expected_version=period.version,
                allowed_store_ids=(ids['north'], ids['south']))
    approve_schedule(db, **args)
    publish_schedule(db, **args)


def clear(db, actor, kind='pending', now=NOW, **extra):
    preview = preview_clear(db, principal=admin(actor), kind=kind, now=now)
    return clear_schedules(db, principal=admin(actor), kind=kind, now=now,
        fingerprint=preview['fingerprint'], confirmation='CLEAR', **extra)


@pytest.mark.parametrize('role,active', [(Role.MANAGER,True),(Role.LEAD,True),(Role.STORE,True),(Role.ADMIN,False)])
def test_clear_literal_active_admin_required(scheduling_db, role, active):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        user = Principal(actor.id, actor.username, role, None, active)
        with pytest.raises(PermissionError): preview_clear(db, principal=user, kind='pending', now=NOW)
        with pytest.raises(PermissionError): clear_schedules(db, principal=user, kind='pending', fingerprint='', confirmation='CLEAR')


@pytest.mark.parametrize('kind', ['pending','confirmed'])
def test_counts_select_only_current_future_matching_status_and_preserve_references(scheduling_db, kind):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        past, old = ready_period(db, actor, ids, day=date(2026,10,13))
        publish(db, actor, ids, past)
        current, shifts = ready_period(db, actor, ids, day=date(2026,10,19))
        future, future_shifts = ready_period(db, actor, ids, day=date(2026,10,27))
        if kind == 'confirmed':
            publish(db, actor, ids, current); publish(db, actor, ids, future)
        other, _ = ready_period(db, actor, ids, day=date(2026,11,3))
        if kind == 'pending': publish(db, actor, ids, other)
        versions = {p.id: p.version for p in (past,current,future,other)}
        shift_ids = list(db.scalars(select(m.ScheduleShift.id)))
        approval_ids = list(db.scalars(select(m.ScheduleApproval.id)))
        result = clear(db, actor, kind)
        assert result['period_count'] == 2 and result['assignment_count'] == 2
        assert past.status == m.SchedulePeriodStatus.PUBLISHED and past.version == versions[past.id]
        assert other.version == versions[other.id] and other.cleared_at is None
        assert current.status == future.status == m.SchedulePeriodStatus.ARCHIVED
        assert current.cleared_at == future.cleared_at == NOW
        assert list(db.scalars(select(m.ScheduleShift.id))) == shift_ids
        assert list(db.scalars(select(m.ScheduleApproval.id))) == approval_ids
        assert db.scalar(select(func.count()).select_from(m.ScheduleGenerationRun)) == 0
        audit = db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SCHEDULES_CLEARED'))
        assert audit.actor_principal_id == actor.id
        # Same-week revisions remain unique and manual recreation gets a fresh revision.
        new = create_draft_period(db, principal=actor, week_start=current.week_start_date)
        assert new.id != current.id and new.revision_number > current.revision_number
        with pytest.raises(SchedulingConflict):
            create_shift(db, principal=actor, schedule_period_id=current.id, expected_version=versions[current.id],
                allowed_store_ids=(ids['north'],ids['south']), values=ShiftInput(ids['alex'],ids['north'],date(2026,10,21),time(9),time(17)))


@pytest.mark.parametrize('confirmation', ['', 'clear', ' CLEAR', 'CLEAR '])
def test_clear_requires_exact_confirmation_and_rolls_back(scheduling_db, confirmation):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, _ = ready_period(db, actor, ids)
        preview = preview_clear(db,principal=admin(actor),kind='pending',now=NOW)
        with pytest.raises(SchedulingValidationError):
            clear_schedules(db,principal=admin(actor),kind='pending',now=NOW,
                fingerprint=preview['fingerprint'],confirmation=confirmation)
        db.commit()
        assert period.status == m.SchedulePeriodStatus.DRAFT


def test_changed_preview_cannot_clear_new_or_edited_schedules(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, rows = ready_period(db, actor, ids)
        preview = preview_clear(db,principal=admin(actor),kind='pending',now=NOW)
        update_shift(db,principal=actor,schedule_period_id=period.id,shift_id=rows[0].id,expected_version=period.version,
            allowed_store_ids=(ids['north'],ids['south']),values=ShiftInput(ids['alex'],ids['north'],date(2026,10,20),time(10),time(17)))
        with pytest.raises(SchedulingConflict,match='fresh preview'):
            clear_schedules(db,principal=admin(actor),kind='pending',now=NOW,
                fingerprint=preview['fingerprint'],confirmation='CLEAR')
        db.commit()
        assert period.status == m.SchedulePeriodStatus.DRAFT


def test_manual_horizon_regeneration_and_snapshots_survive_clear(scheduling_db):
    from app.services.v2_scheduling_policy_service import manual_generate_draft_schedule
    Session, actor, ids, _ = scheduling_db
    now = datetime(2026,10,13,18,tzinfo=timezone.utc)
    with Session() as db:
        _coverage(db,actor,ids)
        first = manual_generate_draft_schedule(db,principal=actor,now=now)
        snapshots = list(db.scalars(select(m.ScheduleGeneratorSnapshot.id)))
        result = clear(db,actor,now=now)
        assert result['period_count'] == 9
        assert list(db.scalars(select(m.ScheduleGeneratorSnapshot.id))) == snapshots
        assert db.scalar(select(func.count()).select_from(m.ScheduleGenerationRun)) == 1
        second = manual_generate_draft_schedule(db,principal=actor,now=now)
        assert len(second['created_period_ids']) == 9
        assert not set(first['created_period_ids']) & set(second['created_period_ids'])
        assert all(db.get(m.SchedulePeriod,pid).status == m.SchedulePeriodStatus.DRAFT for pid in second['created_period_ids'])


def test_cancelled_future_work_never_matures_into_fairness_credit(scheduling_db):
    from app.services.v2_scheduling_policy_service import _longview_historical_credit_rows
    from app.services.v2_scheduling_lead_duty_service import lead_duty_facts
    Session,actor,ids,_ = scheduling_db
    with Session() as db:
        date_hours(db, actor, ids['north'], date(2026,10,21))
        period,rows = ready_period(db,actor,ids,day=date(2026,10,19))
        create_shift(db,principal=actor,schedule_period_id=period.id,expected_version=period.version,
            allowed_store_ids=(ids['north'],ids['south']),values=ShiftInput(ids['alex'],ids['north'],date(2026,10,21),time(9),time(17)))
        publish(db,actor,ids,period)
        before = _longview_historical_credit_rows(db,store_id=ids['north'],before_date=date(2026,10,20))
        clear(db,actor,'confirmed')
        after = _longview_historical_credit_rows(db,store_id=ids['north'],before_date=date(2026,11,1))
        assert before == after and [f['shift_date'] for f in after] == [date(2026,10,19)]
        facts=lead_duty_facts(db,start_date=date(2026,10,18),end_date=date(2026,10,24),as_of_date=date(2026,11,1))
        assert [f['business_date'] for f in facts if f['employee_id']] == [date(2026,10,19)]


def test_confirmed_clear_preserves_swapped_inventory_access_and_draft_counts(scheduling_db):
    from app.services.inventory_working_store_service import working_context, WorkingStoreConflict, assert_inventory_store
    from app.services.v2_scheduling_swap_service import change_assignments
    from app.services.blind_count_service import save_draft
    Session,actor,ids,_ = scheduling_db
    day=datetime.now(ZoneInfo('America/Los_Angeles')).date()
    now=datetime.now(timezone.utc)
    with Session() as db:
        user=m.Principal(username='clear-inventory',password_hash='unused',role=m.PrincipalRole.STORE,store_id=ids['north'],active=True)
        campaign=m.Campaign(label='Preserved reset counts');db.add_all([user,campaign]);db.flush()
        db.get(m.Employee,ids['alex']).principal_id=user.id
        employee=Principal(user.id,user.username,Role.STORE,ids['north'],True)
        period,rows=ready_period(db,actor,ids,day=day,two=True)
        publish(db,actor,ids,period)
        count=m.CountSession(store_id=ids['north'],campaign_id=campaign.id,employee_name='Reset Worker',created_by_principal_id=user.id)
        db.add(count);db.flush();db.add(m.SnapshotLine(session_id=count.id,variation_id='CLEAR',item_name='Synthetic',variation_name='Test'));db.flush()
        save_draft(db,principal=employee,session_id=count.id,revision=0,changes={'CLEAR':{'front_qty':'2','back_qty':'3'}},operation_id='seed')
        # Same-day swap must not be silently undone by the reset's temporary access.
        result=change_assignments(db,principal=actor,shift_id=rows[0].id,other_shift_id=rows[1].id,kind='STORE',reason='Actual working location',
            expected_versions={str(period.id):period.version},allowed_store_ids=(ids['north'],ids['south']),
            now=datetime.combine(day,time(8),ZoneInfo('America/Los_Angeles')))
        before=working_context(db,principal=employee,now=now)
        assert before['store_id']==ids['south']
        preview=preview_clear(db,principal=admin(actor),kind='confirmed',now=now)
        assert preview['access']==[{'employee_id':ids['alex'],'store_id':ids['south'],'store_name':'South'}]
        with pytest.raises(SchedulingValidationError,match='Cannot clear'):
            clear_schedules(db,principal=admin(actor),kind='confirmed',fingerprint=preview['fingerprint'],confirmation='CLEAR',now=now)
        refreshed=working_context(db,principal=employee,selected_store_id=ids['north'],now=now)
        assert refreshed['store_id']==ids['south'] and refreshed['version']==before['version']
        assert list(db.scalars(select(m.InventoryStoreAccessGrant)))==[]
        with pytest.raises(WorkingStoreConflict): assert_inventory_store(db,principal=employee,store_id=ids['north'],now=now)
        assert assert_inventory_store(db,principal=employee,store_id=ids['south'],now=now)
        assert count.store_id==ids['north'] and not count.observation_closed
        assert db.get(m.Entry,(count.id,'CLEAR')).counted_qty==Decimal('5')
        assert user.store_id==ids['north']
        assert period.cleared_at is None # blocked reset preserves the original swap evidence
        assert len(list(db.scalars(select(m.ScheduleShift))))==4


@pytest.mark.parametrize('role', ['ADMIN','MANAGER','LEAD','STORE'])
def test_http_admin_settings_visibility_and_authorization(primary, role):
    primary.account(role,linked=False)
    with primary.Session() as db:
        period,_=ready_period(db,primary.manager,primary.ids)
        db.commit()
    login(primary)
    settings=primary.client.get('/v2/scheduling/automation')
    path='/v2/scheduling/settings/clear/pending'
    if role!='ADMIN':
        assert 'Clear Pending Schedules' not in settings.text
        assert primary.client.get(path).status_code==403
        assert post(primary.client,path,fingerprint='x',confirmation='CLEAR').status_code==403
        return
    assert settings.status_code==200 and 'Clear Confirmed Schedules' in settings.text
    page=primary.client.get(path)
    assert page.status_code==200 and '1 period(s) and 1 assignment(s)' in page.text
    fingerprint=re.search(r'name="fingerprint" value="([a-f0-9]+)"',page.text)[1]
    assert primary.client.post(path,data={'fingerprint':fingerprint,'confirmation':'CLEAR'}).status_code==403
    assert post(primary.client,path,fingerprint=fingerprint,confirmation='clear').status_code==303
    with primary.Session() as db: assert db.get(m.SchedulePeriod,period.id).status==m.SchedulePeriodStatus.DRAFT
    assert post(primary.client,path,fingerprint=fingerprint,confirmation='CLEAR').status_code==303
    with primary.Session() as db: assert db.get(m.SchedulePeriod,period.id).cleared_at is not None


@pytest.mark.parametrize('operation', ['edit','publish','generate'])
def test_concurrent_clear_serializes_with_schedule_changes(scheduling_db, operation):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        if operation == 'regenerate':
            _coverage(db, actor, ids, weekday=2)
        period,rows=ready_period(db,actor,ids)
        pid,sid,version=period.id,rows[0].id,period.version
        approve_schedule(db,principal=actor,schedule_period_id=pid,expected_version=version,allowed_store_ids=(ids['north'],ids['south']))
        preview=preview_clear(db,principal=admin(actor),kind='pending',now=NOW)
        db.commit()
    barrier=Barrier(2)
    def reset():
        with Session() as db:
            barrier.wait()
            try:
                clear_schedules(db,principal=admin(actor),kind='pending',now=NOW,
                    fingerprint=preview['fingerprint'],confirmation='CLEAR')
                db.commit();return 'cleared'
            except SchedulingConflict: db.rollback();return 'stale'
    def change():
        with Session() as db:
            # Preload to exercise stale ORM identity maps too.
            db.get(m.SchedulePeriod,pid)
            barrier.wait()
            try:
                if operation=='edit':
                    update_shift(db,principal=actor,schedule_period_id=pid,shift_id=sid,expected_version=version,
                        allowed_store_ids=(ids['north'],ids['south']),values=ShiftInput(ids['alex'],ids['north'],date(2026,10,20),time(10),time(17)))
                elif operation=='regenerate':
                    from app.services.v2_scheduling_policy_service import regenerate_period
                    regenerate_period(db,principal=actor,schedule_period_id=pid)
                elif operation=='publish':
                    publish_schedule(db,principal=actor,schedule_period_id=pid,expected_version=version,allowed_store_ids=(ids['north'],ids['south']))
                else:
                    create_draft_period(db,principal=actor,week_start=date(2026,10,25))
                db.commit();return 'changed'
            except SchedulingConflict: db.rollback();return 'stale'
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(reset);b=pool.submit(change)
        outcomes=a.result(timeout=30),b.result(timeout=30)
    assert outcomes != ('stale','stale')
    assert operation=='generate' or outcomes in (('cleared','stale'),('stale','changed'))
    with Session() as db:
        periods=list(db.scalars(select(m.SchedulePeriod)))
        assert len({p.week_start_date for p in periods if p.status==m.SchedulePeriodStatus.PUBLISHED})==sum(p.status==m.SchedulePeriodStatus.PUBLISHED for p in periods)


def test_completed_counts_attendance_callouts_commitments_and_audit_unchanged(scheduling_db, monkeypatch):
    from app.services import v2_scheduling_attendance_service as attendance
    monkeypatch.setattr(attendance, '_now', lambda: NOW)
    from app.services.v2_scheduling_exception_service import record_exception
    from app.services.v2_scheduling_attendance_service import record_attendance_event
    from app.services.blind_count_service import save_draft, submit_round
    from app.services.v2_scheduling_lead_duty_service import record_lead_duty
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        date_hours(db, actor, ids['north'], date(2026,10,21))
        period,rows=ready_period(db,actor,ids,day=date(2026,10,19))
        later=create_shift(db,principal=actor,schedule_period_id=period.id,expected_version=period.version,
            allowed_store_ids=(ids['north'],ids['south']),values=ShiftInput(ids['alex'],ids['north'],date(2026,10,21),time(9),time(17)))
        publish(db,actor,ids,period)
        record_attendance_event(db,principal=actor,shift_id=rows[0].id,event_type=m.AttendanceEventType.WORKED_AS_SCHEDULED,
            event_at=NOW-timedelta(days=1),today=NOW.date())
        record_exception(db,principal=actor,shift_id=later.shift_id,event_type=m.AttendanceEventType.CALLED_OUT,
            event_at=NOW,commitment_employee_id=ids['blair'],today=NOW.date())
        record_lead_duty(db,principal=actor,shift_id=rows[0].id,outcome='PERFORMED',employee_id=ids['alex'],reason='Actual Lead',today=NOW.date())
        user=m.Principal(username='legacy-count-evidence',password_hash='unused',role=m.PrincipalRole.STORE,store_id=ids['north'],active=True)
        campaign=m.Campaign(label='Retained completed count');db.add_all([user,campaign]);db.flush()
        worker=Principal(user.id,user.username,Role.STORE,ids['north'],True)
        count=m.CountSession(store_id=ids['north'],campaign_id=campaign.id,employee_name='Completed',created_by_principal_id=user.id)
        db.add(count);db.flush();db.add(m.SnapshotLine(session_id=count.id,variation_id='DONE',item_name='Synthetic',variation_name='Test'));db.flush()
        revision=save_draft(db,principal=worker,session_id=count.id,revision=0,changes={'DONE':{'front_qty':'2','back_qty':'3'}},operation_id='completed')['revision']
        provider=Mock();provider.fetch_current_on_hand.return_value={'DONE':Decimal('5')}
        submit_round(db,principal=worker,session_id=count.id,revision=revision,provider=provider)
        db.flush()
        tables=(m.ScheduleShift,m.ScheduleAttendanceEvent,m.ScheduleCoverageCommitment,m.LeadDutyOutcome,
                m.ScheduleApproval,m.CountSession,m.Entry,m.SnapshotLine,m.CountObservation)
        def snapshot(model):
            return [dict(row) for row in db.execute(select(model.__table__)).mappings()]
        before={model.__tablename__:snapshot(model) for model in tables}
        audit_before={row['id']:row for row in snapshot(m.AuditLog)}
        assert count.observation_closed and before['count_observations']
        clear(db,actor,'confirmed')
        assert before=={model.__tablename__:snapshot(model) for model in tables}
        assert all(row in snapshot(m.AuditLog) for row in audit_before.values())


def test_pending_approval_invalidated_when_published_week_cleared(scheduling_db):
    from app.services.v2_scheduling_service import clone_published_revision
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        period,_=ready_period(db,actor,ids)
        publish(db,actor,ids,period)
        draft=clone_published_revision(db,principal=actor,published_period_id=period.id,allowed_store_ids=(ids['north'],ids['south']))
        args=dict(principal=actor,schedule_period_id=draft.id,expected_version=draft.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db,**args)
        clear(db,actor,'confirmed')
        assert draft.status==m.SchedulePeriodStatus.DRAFT
        with pytest.raises(SchedulingValidationError,match='Approve'):
            publish_schedule(db,**{**args,'expected_version':draft.version})
        approve_schedule(db,**{**args,'expected_version':draft.version})
        publish_schedule(db,**{**args,'expected_version':draft.version})
        assert draft.status==m.SchedulePeriodStatus.PUBLISHED


def test_historical_emergency_grants_remain_evidence_only(scheduling_db):
    from app.services.inventory_working_store_service import working_context
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=db.get(m.Employee,ids['alex']);employee.principal_id=actor.id
        period,_=ready_period(db,actor,ids,day=date(2026,10,21));publish(db,actor,ids,period)
        original=m.InventoryStoreAccessGrant(employee_id=employee.id,store_id=ids['south'],
            authorizing_principal_id=actor.id,reason='Retained legacy evidence',created_at=NOW,
            expires_at=NOW+timedelta(minutes=30),context_version=employee.working_context_version)
        db.add(original);db.flush();gid=original.id
        assert working_context(db,principal=actor,selected_store_id=ids['south'],now=NOW)['store_id'] is None
        clear(db,actor,'confirmed')
        assert db.get(m.InventoryStoreAccessGrant,gid).expires_at==NOW+timedelta(minutes=30)
        assert working_context(db,principal=actor,selected_store_id=ids['south'],now=NOW)['store_id'] is None


def test_clear_marker_cannot_be_downgraded_away(scheduling_db):
    from alembic import command
    from app.schema_contract import _alembic_config,current_revision
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        ready_period(db,actor,ids);clear(db,actor);db.commit()
    with pytest.raises(RuntimeError,match='Cleared schedule evidence'):
        command.downgrade(_alembic_config(engine.url.render_as_string(hide_password=False)),'20261013_0032')
    from app.schema_contract import HEAD_REVISION
    assert current_revision(engine)==HEAD_REVISION


def test_store_context_refresh_protects_two_logged_in_sessions(primary):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from app.services.v2_scheduling_lead_duty_service import business_today
    primary.account()
    with primary.Session() as db:
        period,_=ready_period(db,primary.manager,primary.ids,day=business_today(db))
        publish(db,primary.manager,primary.ids,period)
        count=m.CountSession(store_id=primary.ids['north'],campaign_id=db.scalar(select(m.Campaign.id)),
            employee_name='Stale count',created_by_principal_id=primary.ids['manager'])
        db.add(count);db.flush();cid=count.id;db.commit()
    login(primary)
    with TestClient(primary.app,follow_redirects=False,client=('127.0.0.1',50001)) as second:
        second.get('/login');login(SimpleNamespace(client=second))
        clients=(primary.client,second)
        old=[client.get('/session-status').json()['working_context'] for client in clients]
        with primary.Session() as db:
            with pytest.raises(SchedulingValidationError,match='Cannot clear'):
                clear(db,primary.manager,'confirmed',now=datetime.now(timezone.utc))
            db.commit()
        for client,context in zip(clients,old):
            assert client.get('/session-status').json()['working_context']==context
            assert client.get('/store/emergency-store').status_code==404
            assert client.get(f'/store/sessions/{cid}').status_code==200



def test_inventory_submission_during_clear_is_serialized_and_never_reassigned(scheduling_db):
    from app.services.blind_count_service import save_draft,submit_round
    from app.services.inventory_working_store_service import WorkingStoreConflict
    Session,actor,ids,_=scheduling_db
    day=datetime.now(ZoneInfo('America/Los_Angeles')).date()
    with Session() as db:
        user=m.Principal(username='transition-count',password_hash='unused',role=m.PrincipalRole.STORE,store_id=ids['north'],active=True)
        campaign=m.Campaign(label='Transition inventory');db.add_all([user,campaign]);db.flush()
        db.get(m.Employee,ids['alex']).principal_id=user.id
        worker=Principal(user.id,user.username,Role.STORE,ids['north'],True)
        period,_=ready_period(db,actor,ids,day=day);publish(db,actor,ids,period)
        count=m.CountSession(store_id=ids['north'],campaign_id=campaign.id,employee_name='Concurrent Worker',created_by_principal_id=user.id)
        db.add(count);db.flush();cid=count.id
        db.add(m.SnapshotLine(session_id=cid,variation_id='RACE',item_name='Synthetic',variation_name='Test'));db.flush()
        revision=save_draft(db,principal=worker,session_id=cid,revision=0,changes={'RACE':{'front_qty':'2','back_qty':'3'}},operation_id='seed')['revision']
        preview=preview_clear(db,principal=admin(actor),kind='confirmed')
        db.commit()
    provider=Mock();provider.fetch_current_on_hand.return_value={'RACE':Decimal('5')}
    barrier=Barrier(2)
    def submit():
        with Session() as db:
            barrier.wait()
            try:
                submit_round(db,principal=worker,session_id=cid,revision=revision,provider=provider)
                db.commit();return True
            except WorkingStoreConflict:db.rollback();return False
    def reset():
        with Session() as db:
            barrier.wait()
            with pytest.raises(SchedulingValidationError,match='Cannot clear'):
                clear_schedules(db,principal=admin(actor),kind='confirmed',fingerprint=preview['fingerprint'],
                    confirmation='CLEAR')
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted=pool.submit(submit);cleared=pool.submit(reset)
        accepted=submitted.result(timeout=30);cleared.result(timeout=30)
    with Session() as db:
        assert db.get(m.CountSession,cid).store_id==ids['north']
        assert db.get(m.CountSession,cid).observation_closed==accepted
        assert db.get(m.Entry,(cid,'RACE')).counted_qty==Decimal('5')
        assert provider.fetch_current_on_hand.call_count==int(accepted)


def test_manager_permission_overrides_cannot_grant_clear_access(primary):
    pid=primary.account('MANAGER',linked=False)
    with primary.Session() as db:
        db.add(m.PrincipalPermissionOverride(principal_id=pid,permission_key='scheduling.manage_automation',allowed=True))
        db.commit()
    login(primary)
    settings=primary.client.get('/v2/scheduling/automation')
    assert settings.status_code==200 and 'Clear Pending Schedules' not in settings.text
    for kind in ('pending','confirmed'):
        path=f'/v2/scheduling/settings/clear/{kind}'
        assert primary.client.get(path).status_code==403
        assert post(primary.client,path,fingerprint='anything',confirmation='CLEAR').status_code==403


def test_concurrent_clear_serializes_with_actual_regeneration(scheduling_db):
    test_concurrent_clear_serializes_with_schedule_changes(scheduling_db, 'regenerate')


@pytest.mark.parametrize('operation',['lock','lead','double','transfer'])
def test_preloaded_revisions_cannot_mutate_after_another_session_clears_them(scheduling_db,operation):
    from app.services.v2_scheduling_assignments_service import set_lead_of_day,override_double_coverage_employee
    from app.services.v2_scheduling_policy_service import set_manual_lock,create_transfer_request
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        db.get(m.Employee,ids['alex']).principal_id=actor.id
        period,rows=ready_period(db,actor,ids,day=date(2026,10,21))
        pid,sid=period.id,rows[0].id
        original_shift={c.key:getattr(rows[0],c.key) for c in m.ScheduleShift.__table__.columns}
        db.commit()
    with Session() as stale:
        old=stale.get(m.SchedulePeriod,pid)
        stale.get(m.ScheduleShift,sid)
        with Session() as reset:
            clear(reset,actor);reset.commit()
        assert old.status==m.SchedulePeriodStatus.DRAFT # deliberately stale identity
        with pytest.raises((SchedulingConflict,SchedulingValidationError)):
            if operation=='lock':set_manual_lock(stale,principal=actor,shift_id=sid,locked=True)
            elif operation=='lead':set_lead_of_day(stale,principal=actor,shift_id=sid)
            elif operation=='double':override_double_coverage_employee(stale,principal=actor,shift_id=sid,employee_id=ids['blair'])
            else:create_transfer_request(stale,principal=actor,shift_id=sid,to_employee_id=ids['blair'],today=NOW.date())
        stale.rollback()
    with Session() as db:
        kept=db.get(m.ScheduleShift,sid)
        assert {c.key:getattr(kept,c.key) for c in m.ScheduleShift.__table__.columns}==original_shift
        assert db.scalar(select(func.count()).select_from(m.ShiftTransferRequest))==0
