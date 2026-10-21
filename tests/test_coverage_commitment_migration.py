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
