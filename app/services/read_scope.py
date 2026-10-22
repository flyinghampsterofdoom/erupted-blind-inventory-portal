"""Bounded SELECT reuse during one synchronous read operation only.

No cross-request data, permission cache, transaction changes, or scheduling rules.
Writes, flushes and transaction boundaries discard results. Locking/refreshing
reads always reach the database. Callers must not span external side effects.
"""
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from app.session_events import _clear_reads as _clear
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.functions import FunctionElement

KEY = '_operation_read_scope'
MAX_ENTRIES = 1024
MAX_ROWS = 100000
PURE_FUNCTIONS = {'count', 'sum', 'min', 'max', 'coalesce', 'lower', 'upper',
                  'json_array_length', 'date', 'abs', 'nullif'}


def _reuse(execution):
    db = execution.session
    state = db.info.get(KEY)
    if state is None:
        return None
    statement = execution.statement
    if db.new or db.dirty or db.deleted or not execution.is_select:
        _clear(db)
        return None
    if (getattr(statement, '_for_update_arg', None) is not None
            or execution.execution_options.get('populate_existing')
            or execution.execution_options.get('yield_per')
            or execution.execution_options.get('stream_results')
):
        return None
    key = statement._generate_cache_key()
    if key is None:
        return None
    # Volatile functions and locks must never be replayed.
    if any(isinstance(node, FunctionElement) and node.name.lower() not in PURE_FUNCTIONS
           for node in visitors.iterate(statement)):
        return None
    try:
        identity = (key.key, tuple(repr(bind.value) for bind in key.bindparams), repr(execution.parameters))
        result = state['results'].get(identity)
    except (TypeError, AttributeError):
        return None
    if result is not None:
        return result()
    result = execution.invoke_statement().freeze()
    rows = len(result.data)
    if len(state['results']) < MAX_ENTRIES and state['rows'] + rows <= MAX_ROWS:
        state['results'][identity] = result
        state['rows'] += rows
    return result()


@contextmanager
def reuse_reads(db):
    # Planning has its own carefully scoped cache. Do not wrap/alter it.
    if not isinstance(db, Session) or KEY in db.info:
        yield
        return
    db.info[KEY] = {'results': {}, 'rows': 0, 'values': {}}
    try:
        yield
    finally:
        db.info.pop(KEY, None)


def read_operation(function):
    resolved = signature(function, eval_str=True)
    @wraps(function)
    def cached(*args, **kwargs):
        db = resolved.bind(*args, **kwargs).arguments['db']
        with reuse_reads(db):
            return function(*args, **kwargs)
    cached.__signature__ = resolved
    return cached


def read_value(db, key, load):
    state = db.info.get(KEY) if isinstance(db, Session) else None
    if state is None or db.new or db.dirty or db.deleted:
        return load()
    if key not in state['values']:
        value = load()
        rows = len(value) if isinstance(value, (list, tuple, dict, set)) else 1
        if len(state['values']) >= MAX_ENTRIES or state['rows'] + rows > MAX_ROWS:
            return value
        state['values'][key] = value
        state['rows'] += rows
    return state['values'][key]


def has_read_scope(db):
    return isinstance(db, Session) and KEY in db.info and not (db.new or db.dirty or db.deleted)


def read_model(db, model, identity):
    """Retain a bounded identity map for a frozen operation, including misses.

    SQLAlchemy's weak identity map otherwise reloads lineage ancestors on every
    traversal. Writes/flush/rollback discard these references with other reads.
    Locking and explicit refreshing callers continue to use db.get directly.
    """
    return read_value(db, ('model', model, identity), lambda: db.get(model, identity))


def active_evidence(db, model, shift_ids):
    from sqlalchemy import select
    if not has_read_scope(db):
        return list(db.scalars(select(model).where(model.schedule_shift_id.in_(shift_ids), model.voided_at.is_(None))))
    rows = read_value(db, ('active_evidence', model), lambda: list(db.scalars(
        select(model).where(model.voided_at.is_(None)))))
    ids = set(shift_ids)
    return [row for row in rows if row.schedule_shift_id in ids]
