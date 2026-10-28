"""Fail closed before schema/import mutations; no ambient application settings."""
from contextlib import contextmanager
from sqlalchemy import text
from sqlalchemy.engine import make_url


def require_isolated_url(value):
    url = make_url(value) if isinstance(value, str) else value
    overrides = {'host', 'hostaddr', 'port', 'dbname', 'service', 'servicefile'}
    if (url.get_backend_name() != 'postgresql' or url.host != '127.0.0.1'
            or not (url.database or '').startswith('erupted_v4_phase1_')
            or overrides.intersection(url.query)):
        raise PermissionError('V4 Phase 1 writes require an explicit loopback disposable database')
    return url


def require_isolated(connection):
    url = require_isolated_url(connection.engine.url)
    server = connection.execute(text(
        "SELECT current_database(), inet_server_addr()::text, pg_is_in_recovery()"
    )).one()
    if server[0] != url.database or server[1] not in ('127.0.0.1/32', '127.0.0.1'):
        raise PermissionError('Database endpoint identity did not match isolated target')
    if server[2]:
        raise PermissionError('Foundation writes cannot target a recovery replica')


def require_read_only(connection):
    if connection.dialect.name != 'postgresql':
        raise PermissionError('Evidence requires PostgreSQL read-only enforcement')
    if connection.execute(text('SHOW transaction_read_only')).scalar_one() != 'on':
        raise PermissionError('Evidence requires a database-enforced read-only transaction')
    level = connection.execute(text('SHOW transaction_isolation')).scalar_one()
    if level not in ('repeatable read', 'serializable'):
        raise PermissionError('Evidence requires one coherent repeatable-read snapshot')
    if connection.execute(text('SHOW TimeZone')).scalar_one() != 'UTC':
        raise PermissionError('Evidence requires UTC serialization for deterministic provenance')


@contextmanager
def readonly_snapshot(engine):
    """Explicit transaction control only; no business initialization or writes."""
    require_isolated_url(engine.url)
    with engine.connect().execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True) as connection:
        with connection.begin():
            connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            yield connection
