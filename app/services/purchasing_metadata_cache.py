"""Small metadata only, keyed by immutable committed session revision identity."""
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from app.models import PurchasingSessionRevision

_cache=OrderedDict()
_lock=RLock()
MAX_ENTRIES=128

@event.listens_for(Session,'before_flush')
def _write(db,*args):
    if db.new or db.dirty or db.deleted:db.info['_metadata_uncommitted']=True

@event.listens_for(Session,'do_orm_execute')
def _bulk(execution):
    if not execution.is_select:execution.session.info['_metadata_uncommitted']=True

@event.listens_for(Session,'after_commit')
@event.listens_for(Session,'after_rollback')
def _end(db):db.info.pop('_metadata_uncommitted',None)


def revision_metadata(db,session):
    key=(getattr(db.get_bind(), 'engine', db.get_bind()),session.id,session.revision)
    reusable=not (db.info.get('_metadata_uncommitted') or db.new or db.dirty or db.deleted)
    if reusable:
        with _lock:
            if key in _cache:
                _cache.move_to_end(key)
                return deepcopy(_cache[key])
    result=db.execute(select(PurchasingSessionRevision.evidence['stores'],
        PurchasingSessionRevision.evidence['observed_at']).where(
        PurchasingSessionRevision.session_id==session.id,
        PurchasingSessionRevision.number==session.revision)).one()
    value=(result[0],result[1])
    if reusable:
        with _lock:
            _cache[key]=deepcopy(value)
            while len(_cache)>MAX_ENTRIES:_cache.popitem(last=False)
    return value
