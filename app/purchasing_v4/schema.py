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
        if current in ('p3_0001','p3_0002','p3_0003','p4_0001','p4_0002','p4_0003','p5_0001','p6_0001','p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007'):return  # Additive controls already include the accepted foundation.
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
    require_isolated(connection)
    if connection.execute(text("SELECT to_regclass('purchasing_v4.alembic_version')")).scalar_one():
        if connection.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one_or_none() in ('p4_0001','p4_0002','p4_0003','p5_0001','p6_0001','p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007'):return
    migrate(connection, revision="p3_0003")


def migrate_worksheets(connection):
    if connection.execute(text("SELECT to_regclass('purchasing_v4.alembic_version')")).scalar_one():
        if connection.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one_or_none()in ('p5_0001','p6_0001','p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007'):return
    migrate(connection,revision="p4_0003")


def migrate_commitments(connection):
    if connection.execute(text("SELECT to_regclass('purchasing_v4.alembic_version')")).scalar_one():
        if connection.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one_or_none()in ('p6_0001','p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007'):return
    migrate(connection,revision="p5_0001")


def migrate_quotations(connection):
    if connection.execute(text("SELECT to_regclass('purchasing_v4.alembic_version')")).scalar_one():
        if connection.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one_or_none()in ('p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007'):return
    migrate(connection,revision="p6_0001")


def migrate_receiving(connection):
    if connection.execute(text("SELECT to_regclass('purchasing_v4.alembic_version')")).scalar_one():
        if connection.execute(text("SELECT version_num FROM purchasing_v4.alembic_version")).scalar_one_or_none()in ('p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007'):return
    migrate(connection,revision="p7_0008")


def migrate_email(connection):
    migrate(connection,revision="p8_0007")
