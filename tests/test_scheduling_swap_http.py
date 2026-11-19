"""Swap receipts return canonical boards and never retry committed mutations."""
from datetime import date, timedelta, time
from sqlalchemy import select
import pytest
from app import models as m
from app.routers import v2_scheduling as routes
from test_v2_employee_profiles import employee_client, _admin
from test_v2_scheduling_foundation import scheduling_db

@pytest.fixture
def swap_case(employee_client):
    client, current, Session, ids = employee_client
    day = date.today() + timedelta(days=14)
    week = day - timedelta(days=(day.weekday()+1)%7)
    with Session() as db:
        actor = _admin(db); current['principal'] = actor
        per = m.SchedulePeriod(week_start_date=week, week_end_date=week+timedelta(days=6),
            status=m.SchedulePeriodStatus.DRAFT, revision_number=1, created_by_principal_id=actor.id,
            updated_by_principal_id=actor.id)
        db.add(per); db.flush()
        rows = []
        for offset, employee in ((1,ids['alex']),(3,ids['blair'])):
            row = m.ScheduleShift(schedule_period_id=per.id, employee_id=employee,
                store_id=ids['north'],shift_date=week+timedelta(days=offset),
                start_time=time(9),end_time=time(17),created_by_principal_id=actor.id,
                updated_by_principal_id=actor.id)
            db.add(row); rows.append(row)
        db.commit()
        ids.update(first=rows[0].id,second=rows[1].id,period=per.id,week=week)
    def post(**extra):
        with Session() as db: version=db.get(m.SchedulePeriod,ids['period']).version
        payload={'shift_id':ids['first'],'other_shift_id':ids['second'],'kind':'SHIFT',
            'reason':'Exchange assigned work','expected_versions':{str(ids['period']):version},**extra}
        return client.post('/v2/scheduling/api/assignment-change',json=payload,
            headers={'X-CSRF-Token':client.cookies.get('csrf_token')})
    return client,Session,ids,post

@pytest.mark.parametrize('published',[False,True])
def test_swap_receipt_contains_exact_resulting_board(swap_case,published):
    client,Session,ids,post=swap_case
    if published:
        with Session() as db:
            db.get(m.SchedulePeriod,ids['period']).status=m.SchedulePeriodStatus.PUBLISHED
            db.commit()
    response=post()
    assert response.status_code==200,response.text
    result=response.json()
    assert result['ok'] and result['board']['period']['id']==result['period_id']
    assert result['board']['period']['version']==result['period_versions'][str(result['period_id'])]
    first=next(row for row in result['board']['shifts'] if row['id']==result['shift_id'])
    assert first['employee_id']==ids['blair']
    with Session() as db:
        if published:
            assert result['period_id']!=ids['period']
            assert db.get(m.ScheduleShift,ids['first']).employee_id==ids['alex']
            assert db.get(m.SchedulePeriod,ids['period']).status==m.SchedulePeriodStatus.PUBLISHED
            assert db.get(m.SchedulePeriod,result['period_id']).status==m.SchedulePeriodStatus.DRAFT
        else: assert result['period_id']==ids['period']


def test_board_failure_returns_success_for_committed_swap(swap_case,monkeypatch):
    _,Session,ids,post=swap_case
    def broken(*args,**kwargs):raise RuntimeError('Serializer unavailable')
    monkeypatch.setattr(routes,'_board',broken)
    response=post()
    assert response.status_code==200,response.text
    result=response.json()
    assert result['ok'] and result['board_refresh_required'] and 'board' not in result
    with Session() as db:
        assert db.get(m.ScheduleShift,ids['first']).employee_id==ids['blair']
        assert db.get(m.ScheduleShift,ids['second']).employee_id==ids['alex']


def test_stale_swap_does_not_mutate_or_build_board(swap_case,monkeypatch):
    _,Session,ids,post=swap_case
    monkeypatch.setattr(routes,'_board',lambda *a,**k:pytest.fail('Failed swap built a board'))
    response=post(expected_versions={str(ids['period']):0})
    assert response.status_code==409,response.text
    with Session() as db:
        assert db.get(m.ScheduleShift,ids['first']).employee_id==ids['alex']
        assert db.get(m.ScheduleShift,ids['second']).employee_id==ids['blair']


@pytest.mark.parametrize('owner',[True,False],ids=['owner','manager'])
@pytest.mark.parametrize('published',[False,True],ids=['draft','published'])
def test_four_store_http_processing(employee_client,owner,published):
    from time import perf_counter
    import json
    from test_scheduling_swap_performance import representative
    client,current,Session,ids=employee_client
    with Session() as db:
        if owner: current['principal']=_admin(db)
        per,rows,stores=representative(db,current['principal'],ids,published=published)
        payload={'shift_id':rows[0].id,'other_shift_id':rows[1].id,'kind':'STORE',
            'reason':'Four store HTTP latency measurement','expected_versions':{str(per.id):per.version}}
    began=perf_counter()
    result=client.post('/v2/scheduling/api/assignment-change',json=payload,
        headers={'X-CSRF-Token':client.cookies.get('csrf_token')})
    elapsed=(perf_counter()-began)*1000
    assert result.status_code==200,result.text
    body=result.json()
    assert body['ok'] and body['board']['period']['id']==body['period_id']
    assert not body.get('board_refresh_required')
    assert elapsed<1500
    record={'operation':'four_store_swap_http','owner':owner,'published':published,
        'http_ms':round(elapsed,3),'employees':117,'periods':50,'shifts':1312,
        'database':'isolated local PostgreSQL 16','board_included':True,'network':'in-process ASGI TestClient'}
    print(json.dumps(record),flush=True)
