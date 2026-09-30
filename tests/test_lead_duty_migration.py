"""Disposable PostgreSQL upgrade, strict gate and evidence-preserving rollback."""
import os
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from app.schema_contract import _alembic_config, assert_supported_schema, UnsupportedSchemaError, HEAD_REVISION


@pytest.mark.parametrize('start', [None, '20260930_0029'])
def test_lead_duty_upgrade_and_guarded_downgrade(start):
    admin_url = os.getenv('TEST_POSTGRES_ADMIN_URL')
    if not admin_url:
        pytest.skip('set TEST_POSTGRES_ADMIN_URL for migration integration')
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    name = 'erupted_lead_migration_' + uuid.uuid4().hex[:10]
    url = admin_url.rsplit('/', 1)[0] + '/' + name
    engine = create_engine(url)
    with admin.connect() as conn: conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        config = _alembic_config(url)
        if start:
            command.upgrade(config, start)
            with pytest.raises(UnsupportedSchemaError): assert_supported_schema(engine)
        command.upgrade(config, 'head')
        assert_supported_schema(engine)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == HEAD_REVISION
        command.downgrade(config, '20260930_0029')
        with pytest.raises(UnsupportedSchemaError): assert_supported_schema(engine)
        command.upgrade(config, 'head')
        with engine.begin() as conn:
            principal = conn.execute(text("INSERT INTO principals(username,password_hash,role,active) VALUES ('migration','unused','MANAGER',true) RETURNING id")).scalar_one()
            store = conn.execute(text("INSERT INTO stores(name,active) VALUES ('Synthetic',true) RETURNING id")).scalar_one()
            per = conn.execute(text("INSERT INTO schedule_periods(week_start_date,week_end_date,status,revision_number,created_by_principal_id,updated_by_principal_id) VALUES ('2026-09-20','2026-09-26','PUBLISHED',1,:p,:p) RETURNING id"), {'p': principal}).scalar_one()
            shift = conn.execute(text("INSERT INTO schedule_shifts(schedule_period_id,store_id,shift_date,start_time,end_time,unpaid_break_minutes,created_by_principal_id,updated_by_principal_id) VALUES (:per,:s,'2026-09-22','09:00','17:00',0,:p,:p) RETURNING id"), {'per': per, 's': store, 'p': principal}).scalar_one()
            conn.execute(text("INSERT INTO lead_duty_outcomes(business_date,outcome,scheduled_shift_id,reason,recorded_by_principal_id) VALUES ('2026-09-22','UNCOVERED',:s,'Historical evidence',:p)"), {'s': shift, 'p': principal})
        with pytest.raises(RuntimeError, match='audit history'):
            command.downgrade(config, '20260930_0029')
        assert_supported_schema(engine)
        with engine.connect() as conn: assert conn.execute(text('SELECT count(*) FROM lead_duty_outcomes')).scalar_one() == 1
    finally:
        engine.dispose()
        with admin.connect() as conn: conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
