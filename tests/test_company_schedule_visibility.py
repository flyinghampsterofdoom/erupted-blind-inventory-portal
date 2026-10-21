"""Real HTTP permission and published projection checks on disposable PostgreSQL."""
from datetime import date, datetime, time, timezone, timedelta
import re
import pytest
from sqlalchemy import select, event
from app import models as m
from test_v2_primary import primary, login, post
from test_v2_scheduling_foundation import scheduling_db

WEEK = date(2030, 5, 5)


def seed_company(primary, *, own=True):
    with primary.Session() as db:
        north=db.get(m.Store,primary.ids['north']); north.name='Andresen'
        south=db.get(m.Store,primary.ids['south']); south.name='Highway 99'
        stores=[north,south]
        for name in ('SR 503','Longview'):
            row=m.Store(name=name,square_location_id=name,active=True);db.add(row);stores.append(row)
        db.flush()
        period=m.SchedulePeriod(week_start_date=WEEK,week_end_date=WEEK+timedelta(days=6),
            revision_number=1,status=m.SchedulePeriodStatus.PUBLISHED,
            published_at=datetime(2020,5,1,tzinfo=timezone.utc),
            created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id)
        db.add(period);db.flush()
        shifts=[]
        for n,store in enumerate(stores):
            shift=m.ScheduleShift(schedule_period_id=period.id,employee_id=primary.ids['alex'] if n==0 and own else primary.ids['blair'],
                store_id=store.id,shift_date=WEEK+timedelta(days=n),start_time=time(9),end_time=time(17),
                employee_note='PRIVATE EMPLOYEE NOTE',
                created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id)
            db.add(shift);shifts.append(shift)
        draft=m.SchedulePeriod(week_start_date=WEEK,week_end_date=WEEK+timedelta(days=6),
            revision_number=2,status=m.SchedulePeriodStatus.DRAFT,
            created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id)
        db.add(draft);db.flush()
        secret=m.ScheduleShift(schedule_period_id=draft.id,employee_id=primary.ids['alex'],store_id=north.id,
            shift_date=WEEK,start_time=time(3,14),end_time=time(5,14),
            created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id)
        db.add(secret)
        for n,closed in ((0,True),(1,False)):
            db.add(m.StoreSpecialHour(store_id=stores[n].id,calendar_date=WEEK+timedelta(days=6),
                event_name='Approved closure' if closed else 'Special opening',closed_all_day=closed,
                opening_time=None if closed else time(11),closing_time=None if closed else time(16),
                created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id))
        db.commit()
        return period.id,[s.id for s in shifts],draft.id,secret.id


@pytest.mark.parametrize('role,own',[('STORE',True),('STORE',False),('LEAD',True),('LEAD',False)])
def test_company_visible_without_store_or_assignment_scope(primary,role,own):
    primary.account(role);pid,sids,draft,secret=seed_company(primary,own=own);login(primary)
    with primary.Session() as db:
        protected={table:db.execute(select(table)).all() for table in (m.SchedulePeriod.__table__,m.ScheduleShift.__table__,m.StoreSpecialHour.__table__,m.ShiftTransferRequest.__table__)}
    for query in ('', '&store_id=999&period_id='+str(draft)):
        page=primary.client.get('/v2/scheduling/my-schedule?start='+str(WEEK)+query)
        assert page.status_code==200,page.text
        for name in ('Andresen','Highway 99','SR 503','Longview','Blair Two','Approved closure','Closed','Modified hours: 11:00 AM–4:00 PM'):
            assert name in page.text
        assert all(f'data-shift-id="{sid}"' in page.text for sid in sids)
        assert f'data-shift-id="{secret}"' not in page.text
        assert 'PRIVATE EMPLOYEE NOTE' not in page.text and 'PRIVATE MANAGER NOTE' not in page.text
        assert ('company-schedule__shift--own' in page.text)==own
        assert 'start=2030-04-28' in page.text and 'start=2030-05-12' in page.text
        assert 'Cache-Control' in page.headers and page.headers['Cache-Control']=='no-store'
    following=primary.client.get('/v2/scheduling/my-schedule?start=2030-05-12')
    assert following.status_code==200 and 'No published shifts this week' in following.text
    assert not any(f'data-shift-id="{sid}"' in following.text for sid in sids)
    with primary.Session() as db:
        assert protected=={table:db.execute(select(table)).all() for table in protected}
    if role=='LEAD':
        assert primary.client.get('/v2/scheduling/week?start='+str(WEEK)).status_code==200


def test_staff_management_and_draft_transfer_are_denied(primary):
    primary.account();pid,sids,draft,secret=seed_company(primary);login(primary);c=primary.client
    for path in ('/v2/scheduling/week','/v2/scheduling/api/board',
                 f'/v2/scheduling/periods/{draft}/review','/v2/scheduling/automation',
                 '/management/users','/management/sessions','/v2/scheduling/transfer-approvals'):
        assert c.get(path).status_code==403,path
    headers={'X-CSRF-Token':c.cookies.get('csrf_token')}
    operations=[('post','/v2/scheduling/api/periods',{'week_start_date':'2030-05-12'}),
        ('patch',f'/v2/scheduling/api/periods/{draft}/shifts/{secret}',{'expected_version':1,'employee_id':primary.ids['blair'],'store_id':primary.ids['north'],'shift_date':str(WEEK),'start_time':'09:00','end_time':'17:00'}),
        ('delete',f'/v2/scheduling/api/periods/{draft}/shifts/{secret}',{'expected_version':1}),
        ('post',f'/v2/scheduling/api/periods/{draft}/publish',{'expected_version':1})]
    for method,path,payload in operations:
        response=c.request(method,path,json=payload,headers=headers)
        assert response.status_code==403,(path,response.text)
    response=c.post('/v2/scheduling/api/transfers',json={'shift_id':secret,'to_employee_id':primary.ids['blair']},headers=headers)
    assert response.status_code==403,response.text
    response=c.post('/v2/scheduling/api/transfers',json={'shift_id':sids[1],'to_employee_id':primary.ids['alex']},headers=headers)
    assert response.status_code==403,response.text
    assert c.post('/v2/scheduling/api/transfers',json={'shift_id':sids[0],'to_employee_id':primary.ids['blair']}).status_code==403
    with primary.Session() as db:
        assert db.scalar(select(m.ShiftTransferRequest)) is None
        assert db.get(m.ScheduleShift,secret).employee_id==primary.ids['alex']


def test_request_status_and_recipient_controls_are_private(primary):
    account=primary.account();pid,sids,_,_=seed_company(primary)
    with primary.Session() as db:
        statuses=list(m.ShiftTransferStatus)
        for status in statuses:
            db.add(m.ShiftTransferRequest(shift_id=sids[0],from_employee_id=primary.ids['blair'],
                to_employee_id=primary.ids['alex'],initiated_by_principal_id=account,status=status))
            # Active-per-shift index permits only one active request.
            if status==m.ShiftTransferStatus.PENDING_RECIPIENT:db.flush();db.execute(m.ShiftTransferRequest.__table__.update().where(m.ShiftTransferRequest.status==status).values(shift_id=sids[1]))
        db.commit()
    login(primary);page=primary.client.get('/v2/scheduling/my-schedule?start='+str(WEEK))
    assert page.status_code==200,page.text
    for label in ('Pending coworker response','Accepted · awaiting manager approval','Declined by coworker',
                  'Rejected by manager','Approved','Completed','Cancelled','Accept shift','Decline'):
        assert label in page.text


def test_default_transfer_permission_and_explicit_denial(primary):
    from app.services.access_control_service import effective_permission_flags
    from app.auth import Principal,Role
    pid=primary.account()
    with primary.Session() as db:
        p=Principal(pid,'person@example.test',Role.STORE,primary.ids['north'],True)
        assert effective_permission_flags(db,principal=p)['scheduling.transfer_own']
        db.add(m.PrincipalPermissionOverride(principal_id=pid,permission_key='scheduling.transfer_own',allowed=False));db.commit()
    _,sids,_,_=seed_company(primary);login(primary)
    page=primary.client.get('/v2/scheduling/my-schedule?start='+str(WEEK))
    assert 'Send request' not in page.text
    assert primary.client.post('/v2/scheduling/api/transfers',json={'shift_id':sids[0],'to_employee_id':primary.ids['blair']},headers={'X-CSRF-Token':primary.client.cookies.get('csrf_token')}).status_code==403


def test_staff_transfer_consent_revalidates_and_updates_company_schedule(primary):
    from app.services.v2_scheduling_rules_service import upsert_employee_profile
    from app.security.sessions import create_web_session
    from app.config import settings
    giver=primary.account();_,sids,_,_=seed_company(primary)
    with primary.Session() as db:
        receiver=m.Principal(username='receiver@example.test',role=m.PrincipalRole.STORE,active=True)
        db.add(receiver);db.flush();db.get(m.Employee,primary.ids['blair']).principal_id=receiver.id
        for sid in sids[1:]:db.get(m.ScheduleShift,sid).employee_id=primary.ids['alex']
        upsert_employee_profile(db,principal=primary.manager,employee_id=primary.ids['blair'],
            home_store_id=primary.ids['north'],target_weekly_hours=40,
            allowed_store_ids=(primary.ids['north'],primary.ids['south']))
        token=create_web_session(db,receiver.id,None,None);db.commit()
    login(primary);c=primary.client
    headers={'X-CSRF-Token':c.cookies.get('csrf_token')}
    request=c.post('/v2/scheduling/api/transfers',json={'shift_id':sids[0],'to_employee_id':primary.ids['blair']},headers=headers)
    assert request.status_code==201,request.text
    rid=request.json()['request_id'];assert request.json()['status']=='PENDING_RECIPIENT'
    assert c.post(f'/v2/scheduling/api/transfers/{rid}/respond',json={'accept':True},headers=headers).status_code==403
    with primary.Session() as db:assert db.get(m.ScheduleShift,sids[0]).employee_id==primary.ids['alex']
    c.cookies.clear();c.cookies.set(settings.session_cookie_name,token)
    c.get('/v2/scheduling/my-schedule?start='+str(WEEK))
    headers={'X-CSRF-Token':c.cookies.get('csrf_token')}
    response=c.post(f'/v2/scheduling/api/transfers/{rid}/respond',json={'accept':True},headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['status']=='COMPLETED'
    page=c.get('/v2/scheduling/my-schedule?start='+str(WEEK))
    card=re.search(r'<article[^>]*data-shift-id="'+str(sids[0])+r'"[^>]*>(.*?)</article>',page.text,re.S).group(1)
    assert 'Blair Two' in card and 'Your shift' in card
    with primary.Session() as db:
        assert db.get(m.ScheduleShift,sids[0]).employee_id==primary.ids['blair']
        assert db.get(m.ShiftTransferRequest,rid).status==m.ShiftTransferStatus.COMPLETED
