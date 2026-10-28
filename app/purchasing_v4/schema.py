"""Explicit isolated-only migration entry point; public Alembic head unchanged."""
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from .isolation import require_isolated


def migrate(connection, revision='head'):
    require_isolated(connection)
    connection.execute(text('CREATE SCHEMA IF NOT EXISTS purchasing_v4'))
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).parent / 'schema_migrations'))
    config.attributes['connection'] = connection
    command.upgrade(config, revision)


def downgrade(connection):
    require_isolated(connection)
    raise RuntimeError('Foundation evidence is retained; rollback code, recover forward, never erase provenance')
