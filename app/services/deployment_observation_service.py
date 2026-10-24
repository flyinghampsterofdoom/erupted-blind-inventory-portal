"""Fixed, session-free observations. Never invoke business route handlers.

The verification key confers no principal/role and cannot select records or SQL.
Only existing authorization dependencies are exercised, in a fresh PostgreSQL
connection with server-enforced read-only mode. All output is aggregate policy
evidence, not an authenticated business-page acceptance claim.
"""
import base64
import json
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as PoolTimeout
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

PATH = '/management/deployment/observation'
CONTRACT = 'erupted-observation-v1'
PURPOSE = 'erupted-readiness-observation-v1'


def signing_body(timestamp, nonce):
    return json.dumps({'purpose': PURPOSE, 'method': 'GET', 'path': PATH,
                       'timestamp': int(timestamp), 'nonce': nonce},
                      sort_keys=True, separators=(',', ':')).encode()


def authorized(public_key, timestamp, nonce, signature, now=None):
    try:
        if not re.fullmatch(r'[a-f0-9]{32}', nonce or ''):
            return False
        if abs((time.time() if now is None else now) - int(timestamp)) > 30:
            return False
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True), signing_body(timestamp, nonce))
        return True
    except (ValueError, TypeError, InvalidSignature):
        # InvalidSignature and malformed inputs fail closed; no errors reflected.
        return False


def finding(status, code):
    return {'status': status, 'code': code}


def database_error(exc):
    if isinstance(exc, (TimeoutError, PoolTimeout)) or getattr(getattr(exc, 'orig', None), 'sqlstate', None) == '57014':
        return finding('FAIL', 'database-timeout')
    return finding('FAIL', 'database-unavailable')


def fresh_engine():
    from app.db import engine
    # A pooled stale socket is insufficient. Bound the new TCP connection and
    # every statement before the first SQL. No new database or credential.
    return create_engine(engine.url, poolclass=NullPool, connect_args={
        'connect_timeout': 2,
        'options': '-c default_transaction_read_only=on -c statement_timeout=750 -c lock_timeout=250',
    })


def _allows(call):
    try:
        call()
        return True
    except (HTTPException, PermissionError) as exc:
        if isinstance(exc, HTTPException) and exc.status_code not in (401, 403):
            raise
        return False


def authorization(db):
    from app.auth import Principal, Role
    from app.config import settings
    from app.models import Principal as Account, PrincipalRole
    from app.routers.management import management_access, ordering_access
    from app.routers.purchasing import owner
    from app.routers.store import store_capability
    from app.routers.v2_scheduling import board_access
    from app.v2.store_scope import list_authorized_stores, resolve_store_scope

    results = {}
    for label, role in (('owner', None), ('lead', PrincipalRole.LEAD), ('employee', PrincipalRole.STORE)):
        query = select(Account).where(Account.active.is_(True))
        query = query.where(Account.id == settings.purchasing_owner_principal_id) if label == 'owner' else query.where(Account.role == role)
        if label == 'employee':
            # This bounded probe tests the fixed assigned-store contract. Live
            # schedule-derived inventory scope requires separate functional QA.
            query = query.where(Account.store_id.is_not(None))
        account = db.scalar(query.order_by(Account.id).limit(1))
        if account is None:
            results[label] = finding('INCONCLUSIVE', 'active-role-sample-unavailable')
            continue
        principal = Principal(account.id, account.username, Role(account.role.value), account.store_id, account.active)
        # No ASGI principal injection, HTTP request dispatch or business reads.
        request = SimpleNamespace(state=SimpleNamespace(permission_flags={}))
        scheduling = _allows(lambda: board_access(request, principal, db))
        inventory = _allows(lambda: management_access(principal, db))
        employee_inventory = _allows(lambda: store_capability(principal, db))
        purchasing = _allows(lambda: owner(ordering_access(principal, db), db))
        stores = list_authorized_stores(db, principal)
        scope_ok = bool(stores)
        if stores:
            selected = resolve_store_scope(principal=principal, authorized_stores=stores,
                                           requested_store_ids=(stores[0].id,))
            scope_ok = selected.store_ids == (stores[0].id,)
            # Reject an impossible store, without reading any business objects.
            scope_ok &= not _allows(lambda: resolve_store_scope(principal=principal,
                authorized_stores=stores, requested_store_ids=(-1,)))
            if label == 'employee':
                scope_ok &= selected.locked and selected.store_ids == (principal.store_id,)
        expected = (True, True, True) if label == 'owner' else (True, True, False) if label == 'lead' else (False, False, False)
        ok = (scheduling, inventory, purchasing) == expected and scope_ok and (label != 'employee' or employee_inventory)
        results[label] = {**finding('PASS' if ok else 'FAIL', 'policy-boundaries' if ok else 'policy-expectation-mismatch'),
            'scheduling_board': scheduling, 'inventory_management': inventory,
            'inventory_employee_capability': employee_inventory,
            'purchasing': purchasing, 'store_scope': bool(scope_ok)}
    status = ('FAIL' if any(x['status'] == 'FAIL' for x in results.values()) else
              'INCONCLUSIVE' if any(x['status'] != 'PASS' for x in results.values()) else 'PASS')
    return {**finding(status, 'sampled-existing-policy-dependencies'), 'roles': results,
            'coverage': 'one-active-account-per-role; employee has fixed store; not session, business-page, or live working-context acceptance'}


def observe(current, started, *, engine_factory=None):
    from app.schema_contract import SUPPORTED_REVISIONS
    began = time.monotonic()
    count = 0
    source_ok = (current.get('status') == started.get('status') == 'VERIFIED'
        and bool(current.get('revision')) and current.get('revision') == started.get('revision')
        and bool(current.get('source_tree_sha256'))
        and current.get('source_tree_sha256') == started.get('source_tree_sha256'))
    result = {'contract': CONTRACT, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'application': finding('PASS' if source_ok else 'FAIL', 'source-intact' if source_ok else 'source-unverified'),
        'web': {k: current.get(k) for k in ('revision', 'source_tree_sha256', 'status')},
        'database': finding('INCONCLUSIVE', 'not-observed'),
        'authorization': finding('INCONCLUSIVE', 'database-not-ready')}
    db_engine = None
    def bounded(conn, cursor, statement, parameters, context, executemany):
        nonlocal count
        if time.monotonic() - began > 4:
            raise TimeoutError()
        count += 1
    try:
        db_engine = (engine_factory or fresh_engine)()
        event.listen(db_engine, 'before_cursor_execute', bounded)
        with db_engine.connect().execution_options(postgresql_readonly=True) as conn:
            with conn.begin():
                # Also enforce at transaction level for injected test engines.
                conn.execute(text('SET TRANSACTION READ ONLY'))
                conn.execute(text("SET LOCAL statement_timeout = '750ms'"))
                conn.execute(text("SET LOCAL lock_timeout = '250ms'"))
                if conn.scalar(text('SELECT 1')) != 1:
                    raise RuntimeError('Unexpected connectivity response')
                versions = list(conn.scalars(text('SELECT version_num FROM alembic_version LIMIT 2')))
                compatible = len(versions) == 1 and versions[0] in SUPPORTED_REVISIONS
                result['database'] = {**finding('PASS' if compatible else 'FAIL',
                    'connected-compatible-schema' if compatible else 'schema-incompatible'),
                    'schema': versions[0] if len(versions) == 1 else None, 'read_only': True}
                if compatible:
                    with Session(bind=conn, autoflush=False) as db:
                        result['authorization'] = authorization(db)
                # Explicit rollback even on healthy path. No application commit.
                conn.rollback()
    except (SQLAlchemyError, TimeoutError) as exc:
        result['database'] = database_error(exc)
        result['authorization'] = finding('INCONCLUSIVE', 'database-not-ready')
    except Exception:
        result['authorization'] = finding('INCONCLUSIVE', 'observer-error')
    finally:
        if db_engine is not None:
            event.remove(db_engine, 'before_cursor_execute', bounded)
            db_engine.dispose()
    result['query_count'] = count
    result['duration_ms'] = round((time.monotonic() - began) * 1000, 2)
    return result
