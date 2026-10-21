"""Install operation-cache invalidation before any application Session is used.

This module deliberately imports no models or services. Both database and model
entry points initialize it, including scripts that use a plain SQLAlchemy Session.
Lazy service imports during flushes must never mutate the event listener deque.
Only registration is process-wide; all operation/permission state stays on its
own Session. The existing Purchasing summary cache retains its own lock.
"""
from threading import RLock

from sqlalchemy import event
from sqlalchemy.orm import Session

_install_lock = RLock()
_ATTENTION_PREFIXES = ('purchas', 'vendor', 'order_', 'funding_', 'payment_',
                       'consignment_', 'ordering_')


def _clear_reads(db):
    state = db.info.get('_operation_read_scope')
    if state:
        state['results'].clear()
        state['rows'] = 0
        state['values'].clear()


def _before_flush(db, *args):
    _clear_reads(db)
    db.info.pop('permission_override_maps', None)
    if db.new or db.dirty or db.deleted:
        db.info['_metadata_uncommitted'] = True
    if any(obj.__table__.name.startswith(_ATTENTION_PREFIXES)
           for obj in (*db.new, *db.dirty, *db.deleted)):
        db.info['_attention_changed'] = True


def _execute(execution):
    db = execution.session
    if not execution.is_select:
        db.info.pop('permission_override_maps', None)
        db.info['_metadata_uncommitted'] = True
        table = getattr(execution.statement, 'table', None)
        if table is None or table.name.startswith(_ATTENTION_PREFIXES):
            db.info['_attention_changed'] = True
    if '_operation_read_scope' in db.info:
        # A scope can only exist after this service has been imported. It no
        # longer installs listeners, so dispatch cannot change registration.
        from app.services.read_scope import _reuse
        return _reuse(execution)
    return None


def _commit(db):
    _clear_reads(db)
    # A released savepoint is not a committed outer transaction. Keep the
    # publication guards until the actual commit/rollback boundary.
    if db.in_nested_transaction():
        return
    db.info.pop('_metadata_uncommitted', None)
    if db.info.pop('_attention_changed', False):
        from app.services.purchasing_attention_summary import invalidate
        invalidate(db)


def _rollback(db):
    _clear_reads(db)
    db.info.pop('permission_override_maps', None)
    if db.in_nested_transaction():
        return
    db.info.pop('_metadata_uncommitted', None)
    db.info.pop('_attention_changed', None)


_LISTENERS = (
    ('before_flush', _before_flush, {}),
    ('do_orm_execute', _execute, {'retval': True}),
    ('after_commit', _commit, {}),
    ('after_rollback', _rollback, {}),
)


def install_session_listeners():
    """Idempotent across both entry points and concurrent initialization calls."""
    with _install_lock:
        for name, callback, options in _LISTENERS:
            if not event.contains(Session, name, callback):
                event.listen(Session, name, callback, **options)
