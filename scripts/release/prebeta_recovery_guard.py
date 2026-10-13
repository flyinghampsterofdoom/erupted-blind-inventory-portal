"""Installed only in the bounded 0033 recovery artifact by release preparation."""
import json
import re
from pathlib import Path
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session
from fastapi.responses import JSONResponse

MESSAGE='Recovery mode: scheduling, account and emergency authorization changes are paused. Effective assignments and inventory history are retained.'
PREFIXES=('schedule_','scheduling_','employee_','special_store_','time_off_','attendance_')
TABLES={'stores','store_operating_hours','store_special_hours','employees','principals','principal_permission_overrides','role_permission_overrides','store_shifts',
        'shift_transfer_requests','coverage_requirements','lead_duty_outcomes','application_settings',
        'password_reset_tokens','auth_throttles','inventory_store_access_grants'}
CONTRACT_TABLES=('principals','employees','scheduling_organization_policies','schedule_generator_snapshots',
                 'application_settings','password_reset_tokens','auth_throttles','lead_duty_outcomes',
                 'schedule_coverage_commitments','schedule_approvals','schedule_generation_runs',
                 'scheduling_preference_requests','inventory_store_access_grants')


def protected(name):return name in TABLES or name.startswith(PREFIXES)


def canonical_varchar_array_check(value, column, values):
    # Persisted legacy constraints and restored constraints can place the same
    # varchar-to-text casts on the array or its elements. Match exact approved
    # expressions only; never normalize arbitrary SQL or change allowed values.
    outer = column + "::text = any (array[" + ', '.join(
        "'" + v + "'::character varying" for v in values) + "]::text[])"
    elements = column + "::text = any (array[" + ', '.join(
        "'" + v + "'::character varying::text" for v in values) + "])"
    return outer if value in (outer, elements) else value


def canonical_preference_status_check(value):
    return canonical_varchar_array_check(value, 'status',
        ('pending', 'approved', 'denied', 'withdrawn', 'revoked'))


def canonical_contract_checks(tables):
    approved = {
        ('scheduling_preference_requests', 'preference_request_status_ck'):
            ('status', ('pending', 'approved', 'denied', 'withdrawn', 'revoked')),
        ('lead_duty_outcomes', 'lead_duty_outcome_kind_ck'):
            ('outcome', ('performed', 'uncovered', 'unresolved')),
        ('schedule_generator_snapshots', 'schedule_generator_snapshots_kind_ck'):
            ('generation_kind', ('initial', 'regeneration')),
    }
    for (name, constraint), (column, values) in approved.items():
        table = tables.get(name)
        if table:
            for check in table.get('checks', []):
                if check['name'] == constraint:
                    check['sql'] = canonical_varchar_array_check(check['sql'], column, values)


def signature(engine):
    from app.schema_contract import schema_snapshot
    snapshot=schema_snapshot(engine)
    tables={name:snapshot['tables'].get(name) for name in CONTRACT_TABLES}
    for table in tables.values():
        if table:table['columns']=sorted(table['columns'],key=lambda c:c['name'])
    canonical_contract_checks(tables)
    with engine.connect() as c:
        indexes=list(c.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename IN ('lead_duty_outcomes','schedule_coverage_commitments') ORDER BY indexname")).tuples())
        triggers=list(c.execute(text("SELECT t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true),pg_get_functiondef(t.tgfoid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='schedule_generator_snapshots' AND NOT t.tgisinternal ORDER BY t.tgname")).tuples())
    return json.loads(json.dumps({'tables':tables,'indexes':[list(r) for r in indexes],'triggers':[list(r) for r in triggers]}))


def assert_contract():
    from app.db import engine
    from app.schema_contract import assert_supported_schema,UnsupportedSchemaError
    from app.models import Base
    from app.config import settings
    if not settings.schema_revision_check_enabled:raise UnsupportedSchemaError('Recovery requires schema validation.')
    assert_supported_schema(engine)
    expected=json.loads(Path(__file__).with_name('recovery_schema_0033.json').read_text())
    if signature(engine)!=expected:raise UnsupportedSchemaError('Recovery requires the rehearsed 0033 object contract.')
    inspector=inspect(engine)
    for table in Base.metadata.tables.values():
        if not set(table.columns.keys())<={c['name'] for c in inspector.get_columns(table.name,schema='public')}:
            raise UnsupportedSchemaError('Recovery application columns are missing.')


def flush_guard(session,*_):
    for row in session.new|session.dirty|session.deleted:
        if protected(row.__table__.name) and (row in session.new or row in session.deleted or session.is_modified(row)):
            raise RuntimeError(MESSAGE)


def bulk_guard(state):
    if state.is_update or state.is_delete or state.is_insert:
        table=getattr(state.statement,'table',None)
        if table is not None and protected(table.name):raise RuntimeError(MESSAGE)


def blocked_mutation_path(path):
    return (path.startswith(('/v2/scheduling','/v2/hr','/admin/settings','/management/users',
                '/management/access-controls','/management/password','/management/employee-logs/employees'))
            or bool(re.fullmatch(r'/management/stores/[^/]+/credentials',path))
            or path in {'/forgot-password','/password','/reset-password','/setup-password'})


def install_recovery_guards(app):
    event.listen(Session,'before_flush',flush_guard);event.listen(Session,'do_orm_execute',bulk_guard)
    app.add_event_handler('startup',assert_contract)
    @app.middleware('http')
    async def boundary(request,call_next):
        path=request.url.path.rstrip('/')
        if request.method not in {'GET','HEAD','OPTIONS'} and blocked_mutation_path(path):
            return JSONResponse({'detail':MESSAGE},status_code=503,headers={'Retry-After':'3600'})
        if path.startswith(('/v2/scheduling/automation','/v2/scheduling/transfer-approvals')):
            return JSONResponse({'detail':MESSAGE},status_code=503)
        response=await call_next(request);response.headers['X-Erupted-Recovery']='schema-0033-bounded';return response
    # Both existing shells receive an unmistakable banner; it is not an approval workflow.
    app.state.templates.env.globals['recovery_message']=MESSAGE
