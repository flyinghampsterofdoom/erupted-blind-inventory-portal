"""Combined completion keeps the existing gates and commits no partial lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError
from app import models as m
from app.auth import Principal, Role
from app.services import scheduling_completion_service as completion
from app.services.v2_scheduling_service import approve_schedule, SchedulingConflict, SchedulingValidationError
from test_scheduling_lifecycle_integrity import scheduling_db, complete, args
from test_v2_primary import primary, login


def evidence(db):
    return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (m.ScheduleApproval, m.ScheduleGeneratorSnapshot, m.AuditLog)}


def test_success_and_repeat_are_single_completion(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        p, rows = complete(db, actor, ids); kw = args(p, actor, ids); db.commit()
        before = evidence(db)
        completion.approve_and_publish_schedule(db, **kw); db.commit()
        assert p.status == m.SchedulePeriodStatus.PUBLISHED
        after = evidence(db)
        assert after['schedule_approvals'] == before['schedule_approvals'] + 1
        captures = list(db.scalars(select(m.ScheduleGeneratorSnapshot).where(m.ScheduleGeneratorSnapshot.generation_kind.in_(['APPROVAL','PUBLICATION']))))
        assert len(captures) == 2
        assert captures[0].payload['approval_id'] == captures[1].payload['approval_id']
        events = list(db.scalars(select(m.AuditLog.action)))
        assert events.count('V2:SCHEDULING:SCHEDULE_APPROVED') == events.count('V2:SCHEDULING:SCHEDULE_PUBLISHED') == 1
        with pytest.raises(SchedulingConflict): completion.approve_and_publish_schedule(db, **kw)
        db.commit()
        assert evidence(db) == after


@pytest.mark.parametrize('approved', [False, True])
def test_late_publication_failure_rolls_back_even_if_caller_commits(scheduling_db, monkeypatch, approved):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        p, rows = complete(db, actor, ids)
        if approved: approve_schedule(db, **args(p, actor, ids))
        db.commit(); before = evidence(db); version = p.version
        original = completion.publish_schedule
        def fail_after_publication(*a, **kw):
            original(*a, **kw)
            raise SQLAlchemyError('Synthetic late publication failure')
        monkeypatch.setattr(completion, 'publish_schedule', fail_after_publication)
        with pytest.raises(SQLAlchemyError): completion.approve_and_publish_schedule(db, **args(p, actor, ids))
        db.commit(); db.refresh(p)
        assert p.status == m.SchedulePeriodStatus.DRAFT and p.version == version
        assert p.published_at is None and evidence(db) == before


def test_validation_failure_has_no_approval_side_effect(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        p, rows = complete(db, actor, ids)
        rows[0].is_lead_of_day = False
        db.commit(); before = evidence(db)
        with pytest.raises(SchedulingValidationError): completion.approve_and_publish_schedule(db, **args(p, actor, ids))
        db.commit(); db.refresh(p)
        assert p.status == m.SchedulePeriodStatus.DRAFT and evidence(db) == before


@pytest.mark.parametrize('approved', [False, True])
def test_publish_only_permission_reuses_approval_but_cannot_create_it(scheduling_db, approved):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        p, rows = complete(db, actor, ids)
        if approved: approve_schedule(db, **args(p, actor, ids))
        db.add(m.PrincipalPermissionOverride(principal_id=actor.id, permission_key='scheduling.approve', allowed=False))
        db.commit(); before = evidence(db)
        if approved:
            completion.approve_and_publish_schedule(db, **args(p, actor, ids)); db.commit()
            assert p.status == m.SchedulePeriodStatus.PUBLISHED
            assert evidence(db)['schedule_approvals'] == before['schedule_approvals']
        else:
            with pytest.raises(PermissionError): completion.approve_and_publish_schedule(db, **args(p, actor, ids))
            db.commit(); assert evidence(db) == before


def test_concurrent_requests_publish_once(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        p, rows = complete(db, actor, ids); kw = args(p, actor, ids); db.commit()
    barrier = Barrier(2)
    def submit():
        with Session() as db:
            barrier.wait(timeout=10)
            try:
                completion.approve_and_publish_schedule(db, **kw); db.commit(); return 'published'
            except SchedulingConflict:
                db.rollback(); return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: submit(), range(2))) == ['conflict', 'published']
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(m.ScheduleApproval)) == 1
        assert list(db.scalars(select(m.AuditLog.action))).count('V2:SCHEDULING:SCHEDULE_PUBLISHED') == 1


@pytest.mark.parametrize('role,status', [('STORE',403), ('LEAD',200), ('ADMIN',200)])
def test_route_role_boundary_and_csrf(primary, role, status):
    primary.account(role)
    with primary.Session() as db:
        p, _ = complete(db, primary.manager, primary.ids); pid, version = p.id, p.version; db.commit()
    login(primary); client=primary.client
    path=f'/v2/scheduling/api/periods/{pid}/approve-and-publish'
    assert client.post(path,json={'expected_version':version}).status_code == 403
    result=client.post(path,json={'expected_version':version},headers={'X-CSRF-Token':client.cookies.get('csrf_token')})
    assert result.status_code == status, result.text


def test_denied_publish_and_out_of_scope_do_not_approve(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        p, _ = complete(db, actor, ids); db.commit(); before = evidence(db)
        scoped = args(p, actor, ids); scoped['allowed_store_ids'] = (ids['south'],)
        with pytest.raises(PermissionError): completion.approve_and_publish_schedule(db, **scoped)
        db.commit(); assert evidence(db) == before
        db.add(m.PrincipalPermissionOverride(principal_id=actor.id, permission_key='scheduling.publish', allowed=False)); db.commit()
        with pytest.raises(PermissionError): completion.approve_and_publish_schedule(db, **args(p, actor, ids))
        db.commit(); assert evidence(db) == before


def test_failed_revision_publication_preserves_existing_published_revision(scheduling_db, monkeypatch):
    from app.services.v2_scheduling_service import clone_published_revision
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        published, rows = complete(db, actor, ids)
        completion.approve_and_publish_schedule(db, **args(published, actor, ids)); db.commit()
        original_shifts = [(s.id,s.employee_id,s.shift_date,s.start_time,s.end_time,s.manually_locked) for s in rows]
        draft=clone_published_revision(db,principal=actor,published_period_id=published.id,allowed_store_ids=(ids['north'],ids['south']))
        db.commit(); before=evidence(db); published_version=published.version
        original=completion.publish_schedule
        def fail(*a, **kw):
            original(*a,**kw); raise SQLAlchemyError('Synthetic failure after archiving old publication')
        monkeypatch.setattr(completion,'publish_schedule',fail)
        with pytest.raises(SQLAlchemyError): completion.approve_and_publish_schedule(db,**args(draft,actor,ids))
        db.commit(); db.refresh(published); db.refresh(draft)
        assert published.status==m.SchedulePeriodStatus.PUBLISHED and published.version==published_version
        assert draft.status==m.SchedulePeriodStatus.DRAFT and evidence(db)==before
        assert [(s.id,s.employee_id,s.shift_date,s.start_time,s.end_time,s.manually_locked) for s in rows]==original_shifts


def test_http_publication_failure_is_actionable_and_atomic(primary, monkeypatch):
    primary.account('ADMIN')
    with primary.Session() as db:
        p, _=complete(db,primary.manager,primary.ids); pid,version=p.id,p.version; db.commit(); before=evidence(db)
    original=completion.publish_schedule
    def fail(*a,**kw):
        original(*a,**kw); raise SQLAlchemyError('Synthetic late failure')
    monkeypatch.setattr(completion,'publish_schedule',fail)
    login(primary); c=primary.client
    result=c.post(f'/v2/scheduling/api/periods/{pid}/approve-and-publish',json={'expected_version':version},headers={'X-CSRF-Token':c.cookies.get('csrf_token')})
    assert result.status_code==500, result.text
    with primary.Session() as db:
        assert db.get(m.SchedulePeriod,pid).status==m.SchedulePeriodStatus.DRAFT
        # Login records are unrelated to the schedule operation.
        assert db.scalar(select(func.count()).select_from(m.ScheduleApproval))==before['schedule_approvals']
        assert db.scalar(select(func.count()).select_from(m.ScheduleGeneratorSnapshot))==before['schedule_generator_snapshots']


@pytest.mark.parametrize('fail', [False, True])
def test_stale_approval_uses_existing_reapproval_rules_atomically(scheduling_db, monkeypatch, fail):
    Session, actor, ids, _=scheduling_db
    with Session() as db:
        p,rows=complete(db,actor,ids); approve_schedule(db,**args(p,actor,ids))
        rows[0].employee_note='Synthetic content change after approval'
        db.commit(); before=evidence(db); version=p.version
        if fail:
            original=completion.publish_schedule
            def late_failure(*a,**kw):
                original(*a,**kw); raise SQLAlchemyError('Synthetic stale-approval publication failure')
            monkeypatch.setattr(completion,'publish_schedule',late_failure)
            with pytest.raises(SQLAlchemyError): completion.approve_and_publish_schedule(db,**args(p,actor,ids))
            db.commit(); db.refresh(p)
            assert evidence(db)==before and p.version==version and p.status==m.SchedulePeriodStatus.DRAFT
        else:
            completion.approve_and_publish_schedule(db,**args(p,actor,ids)); db.commit()
            assert p.status==m.SchedulePeriodStatus.PUBLISHED
            assert evidence(db)['schedule_approvals']==before['schedule_approvals']+1


def test_automation_shares_calendar_empty_and_mixed_states(primary, monkeypatch):
    from dataclasses import replace
    from datetime import timedelta
    from app.routers import v2_scheduling as router
    from app.services.v2_scheduling_service import create_draft_period, clone_published_revision
    from app.services.v2_scheduling_readiness_service import HorizonWeek
    primary.account('ADMIN')
    with primary.Session() as db:
        p,_=complete(db,primary.manager,primary.ids)
        completion.approve_and_publish_schedule(db,**args(p,primary.manager,primary.ids))
        clone_published_revision(db,principal=primary.manager,published_period_id=p.id,allowed_store_ids=(primary.ids['north'],primary.ids['south']))
        empty=create_draft_period(db,principal=primary.manager,week_start=p.week_start_date+timedelta(days=7))
        # Exercise multiple range batches without changing horizon business rules.
        weeks=tuple(HorizonWeek(p.week_start_date+timedelta(weeks=i),p.week_end_date+timedelta(weeks=i),'A',p.id if i==0 else empty.id if i==1 else None,'DRAFT' if i<2 else 'MISSING') for i in range(10))
        readiness=replace(router.scheduling_readiness(db,today=p.week_start_date-timedelta(weeks=1)),horizon_weeks=weeks)
        db.commit()
    monkeypatch.setattr(router,'scheduling_readiness',lambda *a,**kw:readiness)
    login(primary)
    page=primary.client.get('/v2/scheduling/automation')
    assert page.status_code==200,page.text
    assert 'calendar-mixed' in page.text and 'Published schedule with unpublished revision' in page.text
    assert 'calendar-not-generated' in page.text and 'Not generated (empty draft)' in page.text
