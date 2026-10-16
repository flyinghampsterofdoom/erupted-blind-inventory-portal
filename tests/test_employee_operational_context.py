"""Employee self-service and operational scope against disposable PostgreSQL."""
from datetime import date,datetime,time,timedelta,timezone
import re
import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal,Role
from app.services.inventory_working_store_service import working_context
from app.services.v2_scheduling_lead_duty_service import business_today
from app.services.v2_scheduling_service import approve_schedule,publish_schedule
from app.services.v2_scheduling_swap_service import change_assignments
from app.services.v2_scheduling_exception_service import record_exception
from test_v2_primary import primary,login,post
from test_v2_scheduling_foundation import scheduling_db,date_hours
from test_v2_scheduling_prebeta import ready_period


def seeded_current(primary, *, two=True):
    pid=primary.account()
    with primary.Session() as db:
        day=business_today(db)
        p,rows=ready_period(db,primary.manager,primary.ids,day=day,two=two)
        for s in rows:
            s.start_time=time(0);s.end_time=time(23,59)
            date_hours(db,primary.manager,s.store_id,day,opening=time(0,15),closing=time(23,59))
        db.flush()
        args=dict(principal=primary.manager,schedule_period_id=p.id,expected_version=p.version,allowed_store_ids=(primary.ids['north'],primary.ids['south']))
        approve_schedule(db,**args);publish_schedule(db,**args)
        # Remove fixed-store dependence entirely, leaving identity and permissions intact.
        db.get(m.Principal,pid).store_id=None
        result=(pid,p.id,p.version,[s.id for s in rows]);db.commit()
    login(primary)
    return result


def test_own_schedule_past_upcoming_all_stores_and_draft_isolation(primary):
    pid=primary.account()
    with primary.Session() as db:
        today=business_today(db);week=today-timedelta(days=(today.weekday()+1)%7)
        db.get(m.Principal,pid).store_id=None
        for offset in (-21,0,28):
            p=m.SchedulePeriod(week_start_date=week+timedelta(days=offset),week_end_date=week+timedelta(days=offset+6),status=m.SchedulePeriodStatus.PUBLISHED,published_at=datetime.now(timezone.utc),revision_number=1,created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id)
            db.add(p);db.flush()
            for day,person,store in ((0,'alex','north'),(1,'alex','south'),(2,'blair','north')):
                db.add(m.ScheduleShift(schedule_period_id=p.id,employee_id=primary.ids[person],store_id=primary.ids[store],shift_date=p.week_start_date+timedelta(days=day),start_time=time(8,45),end_time=time(22),employee_note='PRIVATE OTHER EMPLOYEE' if person=='blair' else '',created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id))
        draft=m.SchedulePeriod(week_start_date=week+timedelta(days=7),week_end_date=week+timedelta(days=13),status=m.SchedulePeriodStatus.DRAFT,revision_number=1,created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id)
        db.add(draft);db.flush();db.add(m.ScheduleShift(schedule_period_id=draft.id,employee_id=primary.ids['alex'],store_id=primary.ids['north'],shift_date=draft.week_start_date,start_time=time(7,11),end_time=time(16),created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id));db.commit()
    login(primary)
    page=primary.client.get('/v2/scheduling/my-schedule',params={'employee_id':primary.ids['blair'],'store_id':999})
    assert page.status_code==200 and 'North' in page.text and 'South' in page.text
    assert page.text.count('8:45 AM')==4 and '7:11 AM' not in page.text and 'PRIVATE OTHER' not in page.text
    history=primary.client.get('/v2/scheduling/my-schedule',params={'start':str(week-timedelta(days=21))})
    assert history.status_code==200 and history.text.count('8:45 AM')==2
    assert 'Previous week' in history.text and 'Next week' in history.text
    assert primary.client.get('/v2/scheduling/my-schedule?start=not-a-date').status_code==422
    assert primary.client.get('/v2/scheduling/week').status_code==403


@pytest.mark.parametrize('change',['store_swap','shift_swap','callout','replacement'])
def test_live_chore_and_inventory_context_after_authorized_change(primary,change):
    pid,period_id,version,shift_ids=seeded_current(primary,two=change.endswith('swap'))
    client=primary.client
    old=client.get('/session-status').json()
    page=client.get('/store/daily-chore-sheet')
    assert page.status_code==200 and 'Location: North' in page.text
    sheet_id=int(re.search(r'/daily-chore-sheet/(\d+)/save',page.text).group(1))
    with primary.Session() as db:
        task=db.scalar(select(m.DailyChoreEntry.task_id).where(m.DailyChoreEntry.sheet_id==sheet_id))
    assert post(client,f'/store/daily-chore-sheet/{sheet_id}/save',working_context=old['working_context'],employee_name='Alex',completed_task_ids=str(task)).status_code==303
    with primary.Session() as db:
        if change=='callout':record_exception(db,principal=primary.manager,shift_id=shift_ids[0],event_type=m.AttendanceEventType.CALLED_OUT,event_at=datetime.now(timezone.utc),commitment_employee_id=primary.ids['blair'],today=business_today(db))
        else:change_assignments(db,principal=primary.manager,shift_id=shift_ids[0],other_shift_id=shift_ids[1] if change.endswith('swap') else None,replacement_employee_id=primary.ids['blair'] if change=='replacement' else None,kind='STORE' if change=='store_swap' else 'SHIFT',reason='Authorized test transfer',expected_versions={str(period_id):version},allowed_store_ids=(primary.ids['north'],primary.ids['south']))
        db.commit()
    current=client.get('/session-status').json()
    assert current['working_context']!=old['working_context']
    for action in ('save','submit','restart','delete'):
        assert post(client,f'/store/daily-chore-sheet/{sheet_id}/{action}',working_context=old['working_context'],employee_name='Should not save').status_code==409
    if change.endswith('swap'):
        assert current['store_id']==primary.ids['south']
        page=client.get('/store/daily-chore-sheet?store_id='+str(primary.ids['north']))
        assert page.status_code==200 and 'Location: South' in page.text
        assert client.get('/store/daily-count').status_code==200
        assert post(client,f'/store/daily-chore-sheet/{sheet_id}/submit',working_context=current['working_context'],employee_name='Wrong store').status_code==403
    else:
        assert current['store_id'] is None
        assert client.get('/store/daily-chore-sheet').status_code==409
        assert client.get('/store/daily-count').status_code==409
    with primary.Session() as db:
        sheet=db.get(m.DailyChoreSheet,sheet_id)
        assert sheet.store_id==primary.ids['north'] and sheet.employee_name=='Alex' and sheet.status==m.DailyChoreSheetStatus.DRAFT
        assert db.scalar(select(m.DailyChoreEntry.completed).where(m.DailyChoreEntry.sheet_id==sheet_id,m.DailyChoreEntry.task_id==task)) is True
        # Replacement employee gets the old location without another grant.
        if not change.endswith('swap'):
            user=m.Principal(username='replacement',role=m.PrincipalRole.STORE,active=True,store_id=None);db.add(user);db.flush();db.get(m.Employee,primary.ids['blair']).principal_id=user.id;db.flush()
            assert working_context(db,principal=Principal(user.id,user.username,Role.STORE,None,True))['store_id']==primary.ids['north']


def test_missing_and_ambiguous_assignment_fail_closed(primary):
    pid=primary.account();login(primary)
    assert primary.client.get('/store/daily-chore-sheet').status_code==409
    assert primary.client.get('/store/daily-count?store_id='+str(primary.ids['north'])).status_code==409
    with primary.Session() as db:
        day=business_today(db);week=day-timedelta(days=(day.weekday()+1)%7)
        p=m.SchedulePeriod(week_start_date=week,week_end_date=week+timedelta(days=6),status=m.SchedulePeriodStatus.PUBLISHED,revision_number=1,created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id);db.add(p);db.flush()
        for sid in (primary.ids['north'],primary.ids['south']):db.add(m.ScheduleShift(schedule_period_id=p.id,employee_id=primary.ids['alex'],store_id=sid,shift_date=day,start_time=time(0),end_time=time(23,59),created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id))
        db.commit()
    assert primary.client.get('/store/daily-chore-sheet').status_code==409
    assert primary.client.get('/store/daily-count').status_code==409
    with primary.Session() as db:assert not list(db.scalars(select(m.DailyChoreSheet)))


def test_concurrent_sessions_and_multipart_context_validation(primary):
    pid,_,_,_=seeded_current(primary,two=False)
    page=primary.client.get('/store/daily-chore-sheet');sid=int(re.search(r'/daily-chore-sheet/(\d+)/save',page.text).group(1))
    context=primary.client.get('/session-status').json()['working_context']
    fields={'csrf_token':primary.client.cookies.get('csrf_token'),'working_context':context,'employee_name':'Multipart'}
    assert primary.client.post(f'/store/daily-chore-sheet/{sid}/save',files={k:(None,v) for k,v in fields.items()},headers={'X-Requested-With':'autosave'}).status_code==204
    with primary.Session() as db:db.get(m.Employee,primary.ids['alex']).working_context_version+=1;db.commit()
    assert primary.client.post(f'/store/daily-chore-sheet/{sid}/save',files={k:(None,v) for k,v in fields.items()},headers={'X-Requested-With':'autosave'}).status_code==409


def test_sequential_store_assignments_resolve_by_time_and_gaps_fail_closed(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        user=m.Principal(username='rotating',role=m.PrincipalRole.STORE,active=True,store_id=None);db.add(user);db.flush();person=db.get(m.Employee,ids['alex']);person.principal_id=user.id
        p=m.SchedulePeriod(week_start_date=date(2026,10,18),week_end_date=date(2026,10,24),status=m.SchedulePeriodStatus.PUBLISHED,revision_number=1,created_by_principal_id=actor.id,updated_by_principal_id=actor.id);db.add(p);db.flush()
        for sid,start,end in ((ids['north'],time(8,45),time(12)),(ids['south'],time(13),time(22))):db.add(m.ScheduleShift(schedule_period_id=p.id,employee_id=person.id,store_id=sid,shift_date=date(2026,10,20),start_time=start,end_time=end,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush();principal=Principal(user.id,user.username,Role.STORE,None,True)
        # UTC 17 = 10am Pacific; 19:30 = 12:30pm gap; 21 = 2pm.
        before=working_context(db,principal=principal,now=datetime(2026,10,20,17,tzinfo=timezone.utc))
        gap=working_context(db,principal=principal,now=datetime(2026,10,20,19,30,tzinfo=timezone.utc))
        after=working_context(db,principal=principal,now=datetime(2026,10,20,21,tzinfo=timezone.utc))
        assert before['store_id']==ids['north'] and gap['store_id'] is None and after['store_id']==ids['south']
        assert before['version']!=after['version']


def test_two_logged_in_sessions_refresh_the_same_authoritative_context(primary):
    from app.security.sessions import create_web_session
    from app.config import settings
    pid,_,_,_=seeded_current(primary,two=False)
    first=primary.client.get('/session-status').json()
    with primary.Session() as db:
        second_token=create_web_session(db,pid,None,None);db.commit()
    headers={'cookie':settings.session_cookie_name+'='+second_token}
    assert primary.client.get('/session-status',headers=headers).json()['working_context']==first['working_context']
    with primary.Session() as db:db.get(m.Employee,primary.ids['alex']).working_context_version+=1;db.commit()
    normal=primary.client.get('/session-status').json()
    other=primary.client.get('/session-status',headers=headers).json()
    assert normal['working_context']==other['working_context']!=first['working_context']
