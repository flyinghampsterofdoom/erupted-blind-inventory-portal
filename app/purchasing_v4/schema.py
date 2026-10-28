"""Explicit isolated-only migration entry point; public Alembic head unchanged."""
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from .isolation import require_isolated


def migrate(connection, revision='p1_0001'):
    require_isolated(connection)
    if revision=='p1_0001' and connection.execute(text("SELECT to_regclass('purchasing_v4.alembic_version')")).scalar_one():
        current=connection.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one_or_none()
        if current in ('p3_0001','p3_0002','p3_0003'):return  # Additive controls already include the accepted foundation.
    connection.execute(text('CREATE SCHEMA IF NOT EXISTS purchasing_v4'))
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).parent / 'schema_migrations'))
    config.attributes['connection'] = connection
    command.upgrade(config, revision)


def downgrade(connection):
    require_isolated(connection)
    raise RuntimeError('Foundation evidence is retained; rollback code, recover forward, never erase provenance')


def migrate_controls(connection):
    """Explicit isolated Phase3 migration; old foundation entry remains pinned."""
    migrate(connection, revision="p3_0003")
