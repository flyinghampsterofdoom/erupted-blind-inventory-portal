"""Only the new pre-deploy wrapper; existing0057 migration already qualified."""
from datetime import date, timedelta, time
from alembic import command
from sqlalchemy import text
import pytest
from app.schema_contract import _alembic_config
from scripts.development.migrate_owner_coverage_0057 import migrate
from test_v2_scheduling_foundation import scheduling_db


def test_release_migration_retains_records_and_reuses_applied_schema(scheduling_db):
    Session, actor, ids, engine = scheduling_db
    url = engine.url.render_as_string(hide_password=False)
    command.downgrade(_alembic_config(url), '20261107_0056')
    with engine.begin() as c:
        period = c.scalar(text("INSERT INTO schedule_periods "
            "(week_start_date,week_end_date,status,revision_number,created_by_principal_id,updated_by_principal_id) "
            "VALUES ('2030-05-05','2030-05-11','PUBLISHED',1,:actor,:actor) RETURNING id"), {'actor':actor.id})
        shift = c.scalar(text("INSERT INTO schedule_shifts "
            "(schedule_period_id,employee_id,store_id,shift_date,start_time,end_time,created_by_principal_id,updated_by_principal_id) "
            "VALUES (:period,:employee,:store,'2030-05-06','09:00','17:00',:actor,:actor) RETURNING id"),
            {'period':period,'employee':ids['alex'],'store':ids['north'],'actor':actor.id})
        c.execute(text("INSERT INTO schedule_coverage_commitments "
            "(schedule_shift_id,employee_id,recorded_by_principal_id,note) VALUES (:shift,:employee,:actor,'Preserved')"),
            {'shift':shift,'employee':ids['blair'],'actor':actor.id})
    first = migrate(url)
    assert first['schema']=='20261119_0057' and first['existing_scheduling_records_unchanged']
    second = migrate(url)
    assert second['from_schema']=='20261119_0057' and second['tables']==first['tables']


def test_release_migration_refuses_unknown_schema(scheduling_db):
    _,_,_,engine=scheduling_db
    with engine.begin() as c:c.execute(text("UPDATE alembic_version SET version_num='unknown'"))
    with pytest.raises(RuntimeError,match='requires schema'):
        migrate(engine.url.render_as_string(hide_password=False))
    with engine.connect() as c:assert c.scalar(text('SELECT version_num FROM alembic_version'))=='unknown'
