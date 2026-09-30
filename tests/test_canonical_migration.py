"""Canonical merge paths use disposable PostgreSQL; production is never a target."""
import os
import uuid

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.schema_contract import (
    HEAD_REVISION, UnsupportedSchemaError, _alembic_config,
    assert_supported_schema, current_revision, upgrade_database,
)

ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')
PARENTS = ('20260928_0028', '20260929_0028')


def test_single_merge_head_preserves_both_parent_revisions():
    script = ScriptDirectory.from_config(_alembic_config('postgresql+psycopg://localhost/unused'))
    assert script.get_heads() == ['20260930_0031'] == [HEAD_REVISION]
    assert script.get_revision(HEAD_REVISION).down_revision == '20260930_0030'
    assert script.get_revision('20260930_0030').down_revision == '20260930_0029'
    assert script.get_revision('20260930_0029').down_revision == PARENTS
    for parent in PARENTS:
        assert script.get_revision(parent).down_revision == '20260927_0027'


@pytest.mark.skipif(not ADMIN_URL, reason='set TEST_POSTGRES_ADMIN_URL for migration integration')
@pytest.mark.parametrize('start', ['20260927_0027', *PARENTS])
def test_upgrade_from_production_or_either_feature_preserves_identity_and_strict_gate(start):
    admin = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    name = 'erupted_canonical_' + uuid.uuid4().hex[:10]
    url = ADMIN_URL.rsplit('/', 1)[0] + '/' + name
    engine = create_engine(url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        config = _alembic_config(url)
        command.upgrade(config, start)
        with engine.begin() as conn:
            principal_id = conn.execute(text("INSERT INTO principals (username,password_hash,role,active) VALUES ('canonical-legacy','historical-hash','LEAD',true) RETURNING id")).scalar_one()
            employee_id = conn.execute(text("INSERT INTO employees (full_name,normalized_name,principal_id,email) VALUES ('Canonical Person','canonical person',:id,'person@example.test') RETURNING id"), {'id': principal_id}).scalar_one()
        # Startup must reject each incomplete schema and leave its revision alone.
        with pytest.raises(UnsupportedSchemaError, match='Unsupported database schema revision'):
            assert_supported_schema(engine)
        assert current_revision(engine) == start
        upgrade_database(url)
        assert current_revision(engine) == HEAD_REVISION
        assert_supported_schema(engine)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT id,principal_id,email FROM employees')).one() == (employee_id, principal_id, 'person@example.test')
            assert conn.execute(text('SELECT password_hash,recovery_email_confirmed FROM principals')).one() == ('historical-hash', False)
            tables = set(conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")).scalars())
            assert {'schedule_generator_snapshots', 'application_settings', 'password_reset_tokens', 'auth_throttles'} <= tables
        # Alembic's merge downgrade changes graph state only, not either schema.
        command.downgrade(config, PARENTS[0])
        with engine.connect() as conn:
            assert set(conn.execute(text('SELECT version_num FROM alembic_version')).scalars()) == set(PARENTS)
            assert conn.execute(text("SELECT to_regclass('schedule_generator_snapshots'),to_regclass('password_reset_tokens')")).one() == ('schedule_generator_snapshots', 'password_reset_tokens')
        with pytest.raises(UnsupportedSchemaError, match='Expected one Alembic revision row'):
            assert_supported_schema(engine)
        command.upgrade(config, 'head')
        assert_supported_schema(engine)
        # Empty feature tables permit the existing parent downgrade policy.
        command.downgrade(config, '20260927_0027')
        assert current_revision(engine) == '20260927_0027'
        with engine.connect() as conn:
            assert conn.execute(text('SELECT id,principal_id,email FROM employees')).one() == (employee_id, principal_id, 'person@example.test')
        command.upgrade(config, 'head')
        assert_supported_schema(engine)
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
