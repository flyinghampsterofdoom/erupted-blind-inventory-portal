from datetime import date,datetime,time,timedelta,timezone
import pytest
from sqlalchemy import select,func
from app import models as m
from app.auth import Principal,Role
from app.services.v2_scheduling_business_hours_service import save_business_hours,restore_normal_hours,assignment_impacts
from app.services.v2_scheduling_service import SchedulingValidationError,SchedulingConflict,ShiftInput,create_shift,validate_publication_assignments
from app.services.v2_scheduling_policy_service import materialize_coverage_positions,evaluate_assignment,double_coverage_status,regenerate_period
from app.services.v2_scheduling_snapshot_service import serialize_schedule
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from test_v2_scheduling_foundation import scheduling_db,_coverage
from test_v2_scheduling_repair import period,shift,person

DAY=date(2026,11,26)

def admin(actor):return Principal(actor.id,actor.username,Role.ADMIN,None,True)

def save(db,a,ids,**kw):
    values=dict(principal=admin(a),store_ids=(ids['north'],),allowed_store_ids=(ids['north'],ids['south']),calendar_date=DAY,event_name='Thanksgiving',closed_all_day=True)
    values.update(kw);return save_business_hours(db,**values)


@pytest.mark.parametrize('role',[Role.MANAGER,Role.LEAD,Role.STORE])
def test_only_admin_can_change_business_hours(scheduling_db,role):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        with pytest.raises(PermissionError):save(db,a,ids,principal=Principal(a.id,a.username,role,None,True))
        assert db.scalar(select(func.count()).select_from(m.StoreSpecialHour))==0


def test_admin_permission_override_is_respected(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        db.add(m.PrincipalPermissionOverride(principal_id=a.id,permission_key='scheduling.manage_special_hours',allowed=False,updated_by_principal_id=a.id));db.flush()
        with pytest.raises(PermissionError):save(db,a,ids)


def test_all_stores_share_audited_batch_and_existing_duplicate_does_not_partially_save(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        rows=save(db,a,ids,store_ids=(ids['north'],ids['south']))
        assert len(rows)==2 and all(r.closed_all_day and r.opening_time is None and r.closing_time is None for r in rows)
        assert rows[0].batch_correlation_id==rows[1].batch_correlation_id
        db.commit()
        with pytest.raises(SchedulingConflict,match='already exists'):save(db,a,ids,store_ids=(ids['north'],ids['south']))
        db.commit();assert db.scalar(select(func.count()).select_from(m.StoreSpecialHour))==2
        audit=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:BUSINESS_HOURS_SAVED'))
        assert audit.meta['metadata']['assignments_unchanged'] is True


def test_unauthorized_store_batch_never_partially_saves(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        with pytest.raises(PermissionError):save(db,a,ids,store_ids=(ids['north'],ids['south']),allowed_store_ids=(ids['north'],))
        db.commit();assert db.scalar(select(func.count()).select_from(m.StoreSpecialHour))==0


@pytest.mark.parametrize('opening,closing',[(None,time(14)),(time(18),time(14)),(time(0),time(14))])
def test_invalid_different_hours_fail_without_writes(scheduling_db,opening,closing):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        with pytest.raises(SchedulingValidationError):save(db,a,ids,closed_all_day=False,opening_time=opening,closing_time=closing)
        assert db.scalar(select(func.count()).select_from(m.StoreSpecialHour))==0


def test_edit_and_restore_preserve_history_and_reject_stale_update(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        row=save(db,a,ids)[0];stamp=row.updated_at;db.commit()
        save(db,a,ids,edit_id=row.id,expected_updated_at=stamp,closed_all_day=False,opening_time=time(10),closing_time=time(14))
        db.commit()
        with pytest.raises(SchedulingConflict):save(db,a,ids,edit_id=row.id,expected_updated_at=stamp)
        with pytest.raises(SchedulingConflict):restore_normal_hours(db,principal=admin(a),exception_id=row.id,expected_updated_at=stamp,allowed_store_ids=(ids['north'],))
        restore_normal_hours(db,principal=admin(a),exception_id=row.id,expected_updated_at=row.updated_at,allowed_store_ids=(ids['north'],))
        db.commit();assert not row.active and db.get(m.StoreSpecialHour,row.id) is not None
        events=list(db.scalars(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:BUSINESS_HOURS_SAVED')))
        assert len(events)==2 and events[1].meta['before']['exceptions'][0]['closed_all_day'] is True


def test_closure_preserves_assignments_and_old_approval_but_invalidates_draft_version(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        per=period(db,a,date(2026,11,22));row=shift(db,a,per,db.get(m.Employee,ids['alex']),DAY,ids['north'])
        db.add(m.ScheduleApproval(schedule_period_id=per.id,version=per.version,actor_principal_id=a.id));db.commit()
        old_version=per.version;old_shift={c.name:getattr(row,c.name) for c in m.ScheduleShift.__table__.columns}
        event=save(db,a,ids)[0];db.commit()
        assert old_shift=={c.name:getattr(row,c.name) for c in m.ScheduleShift.__table__.columns}
        assert per.version==old_version+1
        assert db.scalar(select(m.ScheduleApproval.version).where(m.ScheduleApproval.schedule_period_id==per.id))==old_version
        assert assignment_impacts(db,event)==[dict(period_id=per.id,status='DRAFT',shift_id=row.id)]
        assert db.scalar(select(m.ScheduleWarning.id).where(m.ScheduleWarning.schedule_period_id==per.id,m.ScheduleWarning.warning_type=='SHIFT_ON_CLOSED_DATE'))
        with pytest.raises(SchedulingValidationError):validate_publication_assignments(db,period=per,allowed_store_ids=(ids['north'],))


def test_published_schedule_and_evidence_remain_unchanged_when_hours_change(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        per=period(db,a,date(2026,11,22),status='PUBLISHED');row=shift(db,a,per,db.get(m.Employee,ids['alex']),DAY,ids['north'])
        db.commit();before=(per.version,per.status,per.updated_at,row.employee_id,row.start_time,row.end_time)
        event=save(db,a,ids)[0];db.commit()
        assert before==(per.version,per.status,per.updated_at,row.employee_id,row.start_time,row.end_time)
        assert assignment_impacts(db,event)[0]['status']=='PUBLISHED'


@pytest.mark.parametrize('employee',[True,False])
def test_closure_blocks_manual_assigned_and_unassigned_shifts(scheduling_db,employee):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        per=period(db,a,date(2026,11,22));save(db,a,ids)
        with pytest.raises(SchedulingValidationError,match='business closed'):
            create_shift(db,principal=admin(a),schedule_period_id=per.id,expected_version=per.version,
                values=ShiftInput(employee_id=ids['alex'] if employee else None,store_id=ids['north'],shift_date=DAY,start_time=time(9),end_time=time(14),unpaid_break_minutes=0),allowed_store_ids=(ids['north'],))
        result=evaluate_assignment(db,employee_id=ids['alex'],store_id=ids['north'],shift_date=DAY,start_time=time(9),end_time=time(14))
        assert 'BUSINESS_CLOSED' in [r.code for r in result.reasons]


def test_generation_respects_special_closing_and_skips_closed_date(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        _coverage(db,a,ids,weekday=3);_coverage(db,a,ids,weekday=4,count=2)
        per=period(db,a,date(2026,11,22))
        db.add(m.StoreShift(store_id=ids['north'],label='Normal day',start_time=time(8,45),end_time=time(22),active_weekdays=127,active=True,created_by_principal_id=a.id,updated_by_principal_id=a.id));db.flush()
        save(db,a,ids)
        save(db,a,ids,calendar_date=DAY-timedelta(days=1),event_name='Different hours',closed_all_day=False,opening_time=time(10),closing_time=time(14))
        materialize_coverage_positions(db,principal=admin(a),period=per)
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)))
        assert len(rows)==1 and rows[0].shift_date==DAY-timedelta(days=1)
        assert rows[0].start_time==time(9,45) and rows[0].end_time==time(14)
        assert not double_coverage_status(db,period=per)['uncovered']
        warnings=rebuild_schedule_warnings(db,schedule_period_id=per.id)
        assert not [w for w in warnings if w.warning_date==DAY and w.warning_type in ('NO_ASSIGNED_EMPLOYEE','DOUBLE_COVERAGE_UNFILLED')]


def test_different_hours_block_late_manual_assignment(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        save(db,a,ids,closed_all_day=False,opening_time=time(10),closing_time=time(14))
        result=evaluate_assignment(db,employee_id=ids['alex'],store_id=ids['north'],shift_date=DAY,start_time=time(9,45),end_time=time(22))
        assert 'OUTSIDE_SPECIAL_HOURS' in [r.code for r in result.reasons]


def test_admin_routes_enforce_csrf_and_render_saved_exception(scheduling_db,monkeypatch):
    from fastapi import FastAPI,Request
    from fastapi.testclient import TestClient
    from app.auth import get_current_principal
    from app.db import get_db
    from app.config import settings
    from app.routers.v2_scheduling_business_hours import router
    from app.security.csrf import install_csrf_cookie_middleware
    Session,a,ids,_=scheduling_db
    app=FastAPI();install_csrf_cookie_middleware(app)
    # Use the production template compatibility wrapper and globals, without its
    # startup/background integrations or any production database connection.
    from app.main import app as main_app
    app.state.templates=main_app.state.templates
    current={'principal':admin(a)}
    @app.middleware('http')
    async def context(request,call_next):
        request.state.principal=current['principal']
        request.state.permission_flags={}
        return await call_next(request)
    def db_override():
        with Session() as db:yield db
    app.dependency_overrides[get_current_principal]=lambda:current['principal']
    app.dependency_overrides[get_db]=db_override
    app.include_router(router)
    from app.routers.v2_scheduling import router as scheduling_router
    app.include_router(scheduling_router)
    monkeypatch.setattr(settings,'v2_enabled_features','staff_scheduling_v2')
    monkeypatch.setattr(settings,'v2_principal_features','')
    monkeypatch.setattr(settings,'session_cookie_secure',False)
    with TestClient(app,follow_redirects=False,client=('127.0.0.1',50000)) as client:
        client.get('/missing');csrf=client.cookies.get('csrf_token')
        data=dict(scope='all',calendar_date=str(DAY),event_name='Thanksgiving',mode='closed')
        assert client.post('/v2/scheduling/business-hours',data=data).status_code==403
        current['principal']=a
        assert client.get('/v2/scheduling/business-hours').status_code==403
        assert client.post('/v2/scheduling/business-hours',data={**data,'csrf_token':csrf}).status_code==403
        current['principal']=admin(a)
        saved=client.post('/v2/scheduling/business-hours',data={**data,'csrf_token':csrf})
        assert saved.status_code==303 and 'error=' not in saved.headers['location']
        page=client.get('/v2/scheduling/business-hours')
        assert page.status_code==200 and 'Thanksgiving' in page.text and 'Business Closed' in page.text
        board_page=client.get('/v2/scheduling/week?start=2026-11-22')
        assert board_page.status_code==200 and 'Business Closed' in board_page.text
        assert 'No schedule exists for this week' in board_page.text
        with Session() as db:assert db.scalar(select(func.count()).select_from(m.StoreSpecialHour))==2


def test_board_shows_closed_date_without_any_shift(scheduling_db):
    from app.services.v2_scheduling_board_service import serialize_week_board
    Session,a,ids,_=scheduling_db
    with Session() as db:
        save(db,a,ids)
        board=serialize_week_board(db,week_start=date(2026,11,22),selected_store_ids=(ids['north'],),all_authorized_store_ids=(ids['north'],ids['south']),permission_flags={})
        assert board['business_hours']==[dict(date=str(DAY),store_name='North',event_name='Thanksgiving',closed=True,hours=None)]


def test_closure_does_not_prorate_allocations_or_wipe_existing_draft(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        first=person(db,a,'First',lead=True,target=1);second=person(db,a,'Second',lead=True,target=1)
        _coverage(db,a,ids,weekday=3);_coverage(db,a,ids,weekday=4)
        per=period(db,a,date(2026,11,22));per.generated_at=datetime(2026,10,20,tzinfo=timezone.utc)
        shift(db,a,per,first,DAY-timedelta(days=1),ids['north']);shift(db,a,per,second,DAY,ids['north'])
        save(db,a,ids);db.commit();before=serialize_schedule(db,per)
        with pytest.raises(SchedulingValidationError,match='left unchanged'):
            regenerate_period(db,principal=admin(a),schedule_period_id=per.id)
        db.commit();db.expire_all()
        assert serialize_schedule(db,db.get(m.SchedulePeriod,per.id))==before
        assert db.get(m.EmployeeSchedulingProfile,db.scalar(select(m.EmployeeSchedulingProfile.id).where(m.EmployeeSchedulingProfile.employee_id==first.id))).target_shifts_per_week==1


def test_manual_partial_shift_inside_special_hours_retains_existing_support(scheduling_db):
    Session,a,ids,_=scheduling_db
    with Session() as db:
        save(db,a,ids,closed_all_day=False,opening_time=time(10),closing_time=time(17))
        result=evaluate_assignment(db,employee_id=ids['alex'],store_id=ids['north'],shift_date=DAY,start_time=time(13),end_time=time(17))
        assert 'OUTSIDE_SPECIAL_HOURS' not in [r.code for r in result.reasons]
