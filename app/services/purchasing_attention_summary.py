"""Informational, bounded summaries from explicit authoritative workspace reads.

Never contacts Square or calculates a workspace. Process-local snapshots may be
stale across workers; labels always expose their observation time and cache basis.
They never authorize a purchasing/financial action.
"""
from collections import OrderedDict
from datetime import datetime, timezone
from threading import RLock
from time import monotonic

TTL_SECONDS = 30
MAX_ENTRIES = 128
_cache = OrderedDict()
_lock = RLock()


def _key(db, actor_id):
    return (getattr(db.get_bind(), 'engine', db.get_bind()), actor_id)


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
            if key[0] == getattr(db.get_bind(), 'engine', db.get_bind()):
                _cache[key] = (float('-inf'), value)
