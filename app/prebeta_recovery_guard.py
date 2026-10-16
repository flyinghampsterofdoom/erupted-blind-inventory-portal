"""Installed only in the bounded 0036 recovery artifact by release preparation."""
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
CONTRACT_TABLES=('schedule_periods','schedule_shifts','store_operating_hours','store_special_hours','store_shifts','coverage_requirements','principals','employees','scheduling_organization_policies','schedule_generator_snapshots',
                 'application_settings','password_reset_tokens','auth_throttles','lead_duty_outcomes',
                 'schedule_coverage_commitments','schedule_approvals','schedule_generation_runs',
                 'scheduling_preference_requests','inventory_store_access_grants','scheduling_eligibility_periods')


def protected(name):return name in TABLES or name.startswith(PREFIXES)


def canonical_preference_status_check(value):
    # PostgreSQL 18.3 restores this varchar IN check with element text casts;
    # 18.6 deparses the same check with an outer text[] cast. Accept only these
    # two exact, equivalent representations of the approved five-value check.
    values = ('pending', 'approved', 'denied', 'withdrawn', 'revoked')
    outer = "status::text = any (array[" + ', '.join(
        "'" + v + "'::character varying" for v in values) + "]::text[])"
    elements = "status::text = any (array[" + ', '.join(
        "'" + v + "'::character varying::text" for v in values) + "])"
    return outer if value in (outer, elements) else value


def canonical_legacy_check(table, check):
    # Production's original checks and pg_dump-restored copies use two exact,
    # equivalent varchar/text-array cast spellings. Limit acceptance to these
    # three named checks and their approved value sets; all other SQL is retained
    # verbatim for the strict object comparison.
    cases = {
        ('lead_duty_outcomes', 'lead_duty_outcome_kind_ck'):
            ('outcome', ('performed', 'uncovered', 'unresolved'), '', ''),
        ('schedule_generator_snapshots', 'schedule_generator_snapshots_kind_ck'):
            ('generation_kind', ('initial', 'regeneration'), '', ''),
        ('schedule_periods', 'schedule_periods_alternating_week_ck'):
            ('alternating_week', ('a', 'b'), 'alternating_week is null or (', ')'),
    }
    case = cases.get((table, check['name']))
    if case is None:
        return check['sql']
    column, values, prefix, suffix = case
    outer = prefix + column + "::text = any (array[" + ', '.join(
        "'" + value + "'::character varying" for value in values) + "]::text[])" + suffix
    elements = prefix + column + "::text = any (array[" + ', '.join(
        "'" + value + "'::character varying::text" for value in values) + "])" + suffix
    return elements if check['sql'] in (outer, elements) else check['sql']


def signature(engine):
    from app.schema_contract import schema_snapshot
    snapshot=schema_snapshot(engine)
    tables={name:snapshot['tables'].get(name) for name in CONTRACT_TABLES}
    for table in tables.values():
        if table:table['columns']=sorted(table['columns'],key=lambda c:c['name'])
    preference = tables.get('scheduling_preference_requests')
    if preference:
        for check in preference.get('checks', []):
            if check['name'] == 'preference_request_status_ck':
                check['sql'] = canonical_preference_status_check(check['sql'])
    for name in ('lead_duty_outcomes', 'schedule_generator_snapshots', 'schedule_periods'):
        for check in (tables.get(name) or {}).get('checks', []):
            check['sql'] = canonical_legacy_check(name, check)
    with engine.connect() as c:
        indexes=list(c.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename IN ('lead_duty_outcomes','schedule_coverage_commitments','schedule_periods','schedule_shifts','scheduling_eligibility_periods') ORDER BY indexname")).tuples())
        triggers=list(c.execute(text("SELECT t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true),pg_get_functiondef(t.tgfoid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN ('schedule_generator_snapshots','schedule_periods','schedule_shifts') AND NOT t.tgisinternal ORDER BY t.tgname")).tuples())
    with engine.connect() as c:
        eligibility_constraints=list(c.execute(text("SELECT conname,contype,pg_get_constraintdef(oid),convalidated FROM pg_constraint WHERE conrelid='scheduling_eligibility_periods'::regclass ORDER BY conname")).tuples())
    return json.loads(json.dumps({'tables':tables,'indexes':[list(r) for r in indexes],'triggers':[list(r) for r in triggers],'eligibility_constraints':[list(r) for r in eligibility_constraints]}))


def assert_contract():
    from app.db import engine
    from app.schema_contract import assert_supported_schema,UnsupportedSchemaError
    from app.models import Base
    from app.config import settings
    if not settings.schema_revision_check_enabled:raise UnsupportedSchemaError('Recovery requires schema validation.')
    assert_supported_schema(engine)
    expected=json.loads(Path(__file__).with_name('recovery_schema_0036.json').read_text())
    if signature(engine)!=expected:raise UnsupportedSchemaError('Recovery requires the rehearsed 0036 object contract.')
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
        response=await call_next(request);response.headers['X-Erupted-Recovery']='schema-0036-bounded';return response
    # Both existing shells receive an unmistakable banner; it is not an approval workflow.
    app.state.templates.env.globals['recovery_message']=MESSAGE
