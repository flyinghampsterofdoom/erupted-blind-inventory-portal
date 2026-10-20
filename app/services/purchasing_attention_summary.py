"""Informational, bounded summaries from explicit authoritative workspace reads.

Never contacts Square or calculates a workspace. Process-local snapshots may be
stale across workers; labels always expose their observation time and cache basis.
They never authorize a purchasing/financial action.
"""
from collections import OrderedDict
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from sqlalchemy import event
from sqlalchemy.orm import Session

TTL_SECONDS = 30
MAX_ENTRIES = 128
_cache = OrderedDict()
_lock = RLock()
_PREFIXES = ('purchas', 'vendor', 'order_', 'funding_', 'payment_', 'consignment_', 'ordering_')


def _key(db, actor_id):
    return (db.get_bind().url, actor_id)


def remember(db, actor_id, result):
    if db.new or db.dirty or db.deleted or db.info.get('_attention_changed'):
        return
    value = {'sku_count': result['sku_count'], 'configuration_count': result['configuration_count'],
             'observed_at': datetime.now(timezone.utc).isoformat()}
    with _lock:
        _cache[_key(db, actor_id)] = (monotonic(), value)
        _cache.move_to_end(_key(db, actor_id))
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)


def summary(db, actor_id):
    with _lock:
        row = _cache.get(_key(db, actor_id))
        if row is None:
            return {'state': 'unavailable', 'sku_count': None, 'configuration_count': None, 'observed_at': None}
        captured, value = row
        return {**value, 'state': 'current' if monotonic()-captured < TTL_SECONDS else 'stale',
                'basis': 'cached_workspace', 'max_cache_age_seconds': TTL_SECONDS}


def invalidate(db):
    with _lock:
        for key,(captured,value) in list(_cache.items()):
            if key[0] == db.get_bind().url:
                _cache[key] = (float('-inf'), value)


@event.listens_for(Session, 'before_flush')
def _modified(db, *args):
    if any(obj.__table__.name.startswith(_PREFIXES) for obj in (*db.new, *db.dirty, *db.deleted)):
        db.info['_attention_changed'] = True


@event.listens_for(Session, 'do_orm_execute')
def _bulk(execution):
    if not execution.is_select:
        table = getattr(execution.statement, 'table', None)
        if table is not None and table.name.startswith(_PREFIXES):
            execution.session.info['_attention_changed'] = True


@event.listens_for(Session, 'after_commit')
def _commit(db):
    if db.info.pop('_attention_changed', False):
        invalidate(db)


@event.listens_for(Session, 'after_rollback')
def _rollback(db):
    db.info.pop('_attention_changed', None)
