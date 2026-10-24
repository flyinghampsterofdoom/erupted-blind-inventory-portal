"""Fresh fixtures upgrade the entire graph; rollback cannot destroy intent evidence."""
from datetime import date, timedelta
import pytest
from alembic import command
from sqlalchemy import inspect, select
from app.schema_contract import _alembic_config, assert_supported_schema, UnsupportedSchemaError
from app.models import ScheduleCoverageCommitment
from app.services.v2_scheduling_exception_service import record_commitment
from test_v2_scheduling_foundation import scheduling_db
from test_v2_lead_duty import period, shift


def test_commitment_empty_roundtrip(scheduling_db):
    Session, actor, ids, engine = scheduling_db
    config = _alembic_config(engine.url.render_as_string(hide_password=False))
    command.downgrade(config, '20260930_0030')
    assert 'schedule_coverage_commitments' not in inspect(engine).get_table_names()
    with pytest.raises(UnsupportedSchemaError): assert_supported_schema(engine)
    command.upgrade(config, 'head')
    assert_supported_schema(engine)
    assert 'schedule_coverage_commitments' in inspect(engine).get_table_names()


def test_commitment_downgrade_refuses_audit_loss(scheduling_db):
    Session, actor, ids, engine = scheduling_db
    today = date(2026, 9, 30); day = today + timedelta(days=1)
    with Session() as db:
        row = shift(db, actor, period(db, actor, day), ids['alex'], ids['north'], day)
        record_commitment(db, principal=actor, shift_id=row.id, employee_id=ids['blair'], today=today)
        db.commit()
    config = _alembic_config(engine.url.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match='audit history'):
        command.downgrade(config, '20260930_0030')
    assert_supported_schema(engine)
    with Session() as db: assert db.scalar(select(ScheduleCoverageCommitment)) is not None


def test_partial_coverage_upgrade_preserves_existing_full_shift_records(scheduling_db):
    from sqlalchemy import text
    Session, actor, ids, engine = scheduling_db
    config = _alembic_config(engine.url.render_as_string(hide_password=False))
    command.downgrade(config, '20261021_0048')
    with engine.begin() as connection:
        # The legacy row is inserted through the old schema, not new ORM columns.
        per_id = connection.execute(text("""INSERT INTO schedule_periods
            (week_start_date,week_end_date,status,revision_number,created_by_principal_id,updated_by_principal_id)
            VALUES ('2026-10-25','2026-10-31','PUBLISHED',1,:actor,:actor) RETURNING id"""), {'actor': actor.id}).scalar_one()
        sid = connection.execute(text("""INSERT INTO schedule_shifts
            (schedule_period_id,employee_id,store_id,shift_date,start_time,end_time,created_by_principal_id,updated_by_principal_id)
            VALUES (:period,:employee,:store,'2026-10-25','09:00','17:00',:actor,:actor) RETURNING id"""),
            {'period': per_id, 'employee': ids['alex'], 'store': ids['north'], 'actor': actor.id}).scalar_one()
        cid = connection.execute(text("""INSERT INTO schedule_coverage_commitments
            (schedule_shift_id,employee_id,recorded_by_principal_id,note)
            VALUES (:shift,:employee,:actor,'Existing historical note') RETURNING id"""),
            {'shift': sid, 'employee': ids['blair'], 'actor': actor.id}).scalar_one()
        before = connection.execute(text('SELECT * FROM schedule_coverage_commitments WHERE id=:id'), {'id': cid}).mappings().one()
    command.upgrade(config, 'head')
    assert_supported_schema(engine)
    with engine.connect() as connection:
        after = connection.execute(text('SELECT * FROM schedule_coverage_commitments WHERE id=:id'), {'id': cid}).mappings().one()
        assert all(after[key] == value for key, value in before.items())
        assert after['start_time'] is after['end_time'] is after['unpaid_break_minutes'] is None
    with Session() as db:
        from app.services.v2_scheduling_exception_service import coverage_interval
        row = db.get(ScheduleCoverageCommitment, cid)
        from app.models import ScheduleShift
        from datetime import time
        assert coverage_interval(db.get(ScheduleShift, sid), row) == (time(9), time(17), 0)


def test_partial_coverage_downgrade_refuses_interval_history_loss(scheduling_db):
    from datetime import time
    Session, actor, ids, engine = scheduling_db
    today = date(2026, 10, 24); day = today + timedelta(days=1)
    with Session() as db:
        source = shift(db, actor, period(db, actor, day), ids['alex'], ids['north'], day)
        row = record_commitment(db, principal=actor, shift_id=source.id, employee_id=ids['blair'],
            start_time=time(13), end_time=time(17), today=today)
        db.commit(); cid = row.id
    with pytest.raises(RuntimeError, match='audit history'):
        command.downgrade(_alembic_config(engine.url.render_as_string(hide_password=False)), '20261021_0048')
    assert_supported_schema(engine)
    with Session() as db:
        assert db.get(ScheduleCoverageCommitment, cid).start_time == time(13)
