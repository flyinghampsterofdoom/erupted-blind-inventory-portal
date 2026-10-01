"""Bounded 0031 recovery; no switch permits legacy scheduling/account writes."""
import json
import re
from pathlib import Path
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session
from fastapi.responses import JSONResponse

RECOVERY_MESSAGE = ('Recovery mode: scheduling and employee/account changes are temporarily unavailable. '
                    'New coverage and Lead evidence is retained. Scheduled assignments are not actual-coverage confirmation.')
PROTECTED_PREFIXES = ('schedule_', 'scheduling_', 'employee_', 'special_store_', 'time_off_', 'attendance_')
PROTECTED_TABLES = {'employees', 'principals', 'principal_permission_overrides', 'role_permission_overrides', 'store_shifts', 'shift_transfer_requests',
                    'coverage_requirements', 'lead_duty_outcomes', 'application_settings',
                    'password_reset_tokens', 'auth_throttles'}
NEW_TABLES = ('schedule_generator_snapshots', 'application_settings', 'password_reset_tokens',
              'auth_throttles', 'lead_duty_outcomes', 'schedule_coverage_commitments')


def protected_table(name):
    return name in PROTECTED_TABLES or name.startswith(PROTECTED_PREFIXES)


def recovery_schema_signature(engine):
    from app.schema_contract import schema_snapshot
    snapshot = schema_snapshot(engine)
    names = (*NEW_TABLES, 'principals')
    tables = {name: snapshot['tables'].get(name) for name in names}
    # Physical principal column order differs on the established production profile.
    if tables.get('principals'):
        tables['principals']['columns'] = sorted(tables['principals']['columns'], key=lambda c: c['name'])
    with engine.connect() as conn:
        indexes = list(conn.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename IN ('lead_duty_outcomes','schedule_coverage_commitments') ORDER BY indexname")).tuples())
        triggers = list(conn.execute(text("SELECT t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true),pg_get_functiondef(t.tgfoid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='schedule_generator_snapshots' AND NOT t.tgisinternal ORDER BY t.tgname")).tuples())
    return json.loads(json.dumps({'tables':tables, 'indexes':[list(row) for row in indexes], 'triggers':[list(row) for row in triggers]}))


def assert_recovery_schema(engine):
    from app.schema_contract import UnsupportedSchemaError
    from app.models import Base
    expected = json.loads(Path(__file__).with_name('rollback_schema_0031.json').read_text())
    if recovery_schema_signature(engine) != expected:
        raise UnsupportedSchemaError('Recovery schema objects do not match the rehearsed 0031 contract.')
    inspector = inspect(engine)
    for table in Base.metadata.tables.values():
        columns = {c['name'] for c in inspector.get_columns(table.name, schema='public')}
        if not set(table.columns.keys()) <= columns:
            raise UnsupportedSchemaError('Legacy application columns are missing: ' + table.name)


def _guard_flush(session, _context, _instances):
    for row in session.new | session.dirty | session.deleted:
        if protected_table(row.__table__.name) and (row in session.new or row in session.deleted or session.is_modified(row)):
            raise RuntimeError(RECOVERY_MESSAGE)


def _guard_bulk(state):
    if state.is_update or state.is_delete or state.is_insert:
        table = getattr(state.statement, 'table', None)
        if table is not None and protected_table(table.name):
            raise RuntimeError(RECOVERY_MESSAGE)


def install_recovery_guards(app):
    if not event.contains(Session, 'before_flush', _guard_flush):
        event.listen(Session, 'before_flush', _guard_flush)
        event.listen(Session, 'do_orm_execute', _guard_bulk)

    @app.middleware('http')
    async def recovery_boundary(request, call_next):
        path = request.url.path.rstrip('/')
        domain = path.startswith(('/v2/scheduling', '/v2/hr', '/admin/settings'))
        account = (path.startswith(('/management/users', '/management/access-controls', '/management/password'))
                   or bool(re.fullmatch(r'/management/stores/[^/]+/credentials', path)))
        if request.method not in {'GET', 'HEAD', 'OPTIONS'} and (domain or account):
            return JSONResponse({'detail':RECOVERY_MESSAGE}, status_code=503, headers={'Retry-After':'3600'})
        # Old readiness/fairness/automation/transfer interpretations cannot account for new evidence.
        if path.startswith(('/v2/scheduling/automation', '/v2/scheduling/transfer-approvals')) or re.fullmatch(r'/v2/scheduling/periods/[^/]+/review', path):
            return JSONResponse({'detail':RECOVERY_MESSAGE}, status_code=503)
        response = await call_next(request)
        response.headers['X-Erupted-Recovery'] = 'schema-0031-bounded'
        return response
