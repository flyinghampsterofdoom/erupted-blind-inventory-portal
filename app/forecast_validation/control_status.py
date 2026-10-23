"""Bounded Owner-facing operational receipts, never a collection path."""
import json
from sqlalchemy import select
from .storage import attempts
from .qualification import GRANT


def status(store):
    with store.engine.connect() as c:
        rows=c.execute(select(attempts.c.operation,attempts.c.status,attempts.c.recorded_at,attempts.c.detail)
            .where(attempts.c.operation.like('qualification-%')|attempts.c.operation.like('render-job:%')|
                   attempts.c.operation.like('email:%'))
            .order_by(attempts.c.recorded_at.desc(),attempts.c.id.desc()).limit(50)).mappings().all()
    from app.config import settings
    return {'capacity_contract':GRANT,'execution_enabled':settings.forecast_validation_worker_enabled,
            'recurring_enabled':settings.forecast_validation_schedule_enabled,
            'manual_execution':'Requires separate pinned per-capture authorization',
            'receipt_scope':'Latest50 mirrored receipts only; external independent outbox is authoritative.',
            'receipts':[{'operation':r['operation'],'state':r['status'],'at':r['recorded_at'],
                         'detail':json.loads(r['detail'])} for r in rows]}
