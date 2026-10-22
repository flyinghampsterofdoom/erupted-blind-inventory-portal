from dataclasses import asdict
from datetime import date
import pytest
from sqlalchemy import create_engine, select, event, update
from sqlalchemy.orm import Session
from app.models import Store
from app.services.read_scope import reuse_reads, KEY
from test_v2_scheduling_foundation import scheduling_db


def test_reuse_parameters_writes_and_rollback():
    engine = create_engine('sqlite://')
    Store.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([Store(id=1,name='One', active=True), Store(id=2,name='Two', active=True)]);db.commit()
        calls=[]
        event.listen(engine, 'before_cursor_execute', lambda *a: calls.append(1))
        with reuse_reads(db):
            assert db.scalar(select(Store.name).where(Store.id==1)) == 'One'
            count=len(calls)
            assert db.scalar(select(Store.name).where(Store.id==1)) == 'One'
            assert len(calls)==count
            assert db.scalar(select(Store.name).where(Store.id==2)) == 'Two'
            db.execute(update(Store).where(Store.id==1).values(name='Changed'))
            assert db.scalar(select(Store.name).where(Store.id==1)) == 'Changed'
            db.rollback()
            assert db.scalar(select(Store.name).where(Store.id==1)) == 'One'
        assert KEY not in db.info
        with reuse_reads(db):
            row=db.get(Store,1);row.name='Flushed';db.flush()
            assert db.scalar(select(Store.name).where(Store.id==1))=='Flushed'
        db.rollback()
        with pytest.raises(RuntimeError):
            with reuse_reads(db):raise RuntimeError()
        assert KEY not in db.info
    engine.dispose()


def test_readiness_equivalence_with_closure(scheduling_db):
    from app.services.v2_scheduling_readiness_service import scheduling_readiness
    from app.services.v2_scheduling_business_hours_service import save_business_hours
    from app.auth import Principal,Role
    Session_,actor,ids,_=scheduling_db
    with Session_() as db:
        day=date(2026,11,26)
        save_business_hours(db,principal=Principal(actor.id,actor.username,Role.ADMIN,None,True),
            store_ids=(ids['north'],),allowed_store_ids=(ids['north'],ids['south']),
            calendar_date=day,event_name='Closure',closed_all_day=True)
        db.commit()
        baseline=asdict(scheduling_readiness.__wrapped__(db,today=day));db.rollback()
        optimized=asdict(scheduling_readiness(db,today=day))
        assert optimized==baseline


def test_assignment_and_allocation_results_identical(scheduling_db):
    from app.services.v2_scheduling_policy_service import evaluate_assignment,weekend_fairness
    from app.services.v2_scheduling_allocation_service import weekly_allocation
    from datetime import time,timedelta
    Session_,actor,ids,_=scheduling_db
    def outputs(db):
        rows=[]
        for offset in range(14):
            day=date(2026,11,22)+timedelta(days=offset)
            rows.append((asdict(evaluate_assignment(db,employee_id=ids['alex'],store_id=ids['north'],
                shift_date=day,start_time=time(9),end_time=time(17))),
                weekly_allocation(db,ids['alex'],day),
                asdict(weekend_fairness(db,employee_id=ids['alex'],weekday=5,before_date=day))))
        return rows
    with Session_() as db:
        original=outputs(db);db.rollback()
        with reuse_reads(db):optimized=outputs(db)
        assert original==optimized


def test_value_fact_cache_respects_operation_row_bound(monkeypatch):
    from app.services import read_scope
    engine=create_engine('sqlite://')
    calls=[]
    monkeypatch.setattr(read_scope,'MAX_ROWS',2)
    with Session(engine) as db, reuse_reads(db):
        def load(): calls.append(1);return [1,2,3]
        assert read_scope.read_value(db,'large',load)==[1,2,3]
        assert read_scope.read_value(db,'large',load)==[1,2,3]
        assert len(calls)==2 and not db.info[KEY]['values']
        assert read_scope.read_value(db,'small',lambda:[1])==[1]
        assert read_scope.read_value(db,'small',lambda:None)==[1]
        assert db.info[KEY]['rows']==1
    engine.dispose()
