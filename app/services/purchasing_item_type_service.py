"""Presentation of existing Square inventory classifications; never writes catalog data."""
from collections import OrderedDict
from threading import RLock
from time import monotonic
from app.config import settings
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway

_cache = OrderedDict()
_lock = RLock()


def classify(product, names):
    reporting = getattr(product, 'reporting_category_id', '')
    ids = (reporting,) if reporting else tuple(sorted(set(getattr(product, 'category_ids', ()))))
    if not ids:
        return {'id': 'missing', 'name': 'No inventory category', 'issue': 'No category is assigned in inventory.'}
    labels = [names.get(key) or f'Unnamed category ({key})' for key in ids]
    import json
    return {'id': json.dumps(ids, separators=(',', ':')), 'name': ' / '.join(labels),
        'issue': 'Multiple categories; inventory has no reporting category.' if len(ids)>1 else
            ('Inventory category name is unavailable.' if not names.get(ids[0]) else None)}


def legacy_types():
    """Legacy drafts omitted classifications. One bulk read, cached five minutes.

    Failure is cached briefly to avoid retrying Square for each supplier/page.
    Values are presentation only, never backfilled into old order evidence.
    """
    if not settings.square_access_token:
        return {}, 'Inventory categories unavailable: Square connection is not configured.'
    key = (settings.square_api_base_url, settings.square_access_token)
    with _lock:
        saved = _cache.get(key)
        if saved and saved[0] > monotonic(): return saved[1], saved[2]
        try:
            gateway = SquareOrderingReadGateway()
            catalog = gateway.fetch_active_catalog()
            names = gateway.fetch_category_names()
            values = {vid: classify(p, names) for vid, p in catalog.items()}
            issue = None; ttl = 300
        except (ValueError, RuntimeError, OSError):
            values = {}; issue = 'Inventory categories could not be read. Products and saved quantities remain available.'; ttl = 30
        _cache[key] = (monotonic()+ttl, values, issue)
        while len(_cache)>4: _cache.popitem(last=False)
        return values, issue


def for_lines(lines):
    legacy = {}; issue = None
    if any(not item_type for _, _, _, item_type in lines): legacy, issue = legacy_types()
    fallback = {'id':'unavailable', 'name':'Inventory category unavailable', 'issue':'Classification was not captured in this draft and could not be read.'}
    return {row_id: item_type or legacy.get(vid, fallback) for row_id, vid, _, item_type in lines}, issue
