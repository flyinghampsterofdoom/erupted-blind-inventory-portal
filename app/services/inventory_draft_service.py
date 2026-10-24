"""Keys and receipts for authorized, encrypted browser recovery. No browser grant is trusted."""
from datetime import timezone
import base64
import hashlib
import hmac
import json
import re
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.models import CountSession, CountObservation, Store, NonSellableStockTake, ChangeBoxCount, DailyChoreSheet
from app.services.inventory_working_store_service import assert_inventory_store

MODELS = {'blind-count': CountSession, 'non-sellable-stock-take': NonSellableStockTake,
          'change-box-count': ChangeBoxCount, 'daily-chore-sheet': DailyChoreSheet}


def authorized_record(db, *, principal, kind, record_id, lock=False):
    model = MODELS.get(kind)
    if model is None:
        raise HTTPException(404, 'Draft not found')
    row = db.get(model, record_id)
    if row is None or row.store_id != principal.store_id:
        raise HTTPException(404, 'Draft not found')
    assert_inventory_store(db, principal=principal, store_id=row.store_id)
    if lock:
        row = db.scalar(select(model).where(model.id == record_id).with_for_update().execution_options(populate_existing=True))
    return row


def revision_token(row):
    return row.updated_at.astimezone(timezone.utc).isoformat()


def draft_state(db, *, kind, row):
    submitted = row.status.value == 'SUBMITTED'
    result = {'write_enabled': settings.inventory_draft_writes_enabled, 'record_uuid': str(row.inventory_identity), 'submitted': submitted, 'revision': row.draft_revision if kind == 'blind-count' else revision_token(row)}
    if submitted:
        result['receipt'] = {'id': row.id, 'kind': kind, 'store_id': row.store_id,
            'record_uuid': str(row.inventory_identity),
            'store_name': db.scalar(select(Store.name).where(Store.id == row.store_id)),
            'submitted_at': row.submitted_at.astimezone(timezone.utc).isoformat() if row.submitted_at else None}
        if kind != 'blind-count':
            from app.models import AuditLog
            actions = {'non-sellable-stock-take': 'NON_SELLABLE_STOCK_TAKE_SUBMITTED',
                       'change-box-count': 'CHANGE_BOX_COUNT_SUBMITTED', 'daily-chore-sheet': 'DAILY_CHORE_SHEET_SUBMITTED'}
            audit = db.scalar(select(AuditLog).where(AuditLog.action == actions[kind],
                AuditLog.meta['draft_recovery']['kind'].as_string() == kind,
                AuditLog.meta['draft_recovery']['record_id'].as_integer() == row.id)
                .order_by(AuditLog.id.desc()).limit(1))
            if audit is not None:
                result['receipt'].update(operation_id=audit.meta['draft_recovery']['operation'],
                    principal_id=audit.actor_principal_id)
        if kind == 'blind-count':
            result['receipt']['product_count'] = db.scalar(select(func.count()).select_from(CountObservation).where(CountObservation.session_id == row.id))
    return result


def recovery_scope(db, *, principal_id, kind, row):
    from app.models import Principal
    return {'principal_id': principal_id, 'store_id': row.store_id, 'kind': kind, 'record_id': row.id,
            'principal_uuid': str(db.get(Principal, principal_id).inventory_identity),
            'store_uuid': str(db.get(Store, row.store_id).inventory_identity),
            'record_uuid': str(row.inventory_identity)}


def recovery_key(*, scope):
    """Return only scoped derived keys. Master keys never leave the server.

    Keeping historical versions permits offline device drafts to survive rotation.
    A missing/malformed ring fails closed; there is no application-secret fallback.
    """
    try:
        for field in ('principal_uuid', 'store_uuid', 'record_uuid'):
            if str(UUID(scope[field])) != scope[field]:
                raise ValueError()
        if scope['kind'] not in MODELS:
            raise ValueError()
        configured = json.loads(settings.inventory_draft_encryption_keys or '{}')
        active = settings.inventory_draft_active_key_id
        # Dedicated single-key compatibility is explicit, never a public/app secret.
        if not configured and settings.inventory_draft_encryption_key:
            if len(settings.inventory_draft_encryption_key) < 32:
                raise ValueError()
            configured = {'dedicated-v1': base64.b64encode(hashlib.sha256(
                settings.inventory_draft_encryption_key.encode()).digest()).decode()}
            active = 'dedicated-v1'
        if not isinstance(configured, dict) or not configured or active not in configured:
            raise ValueError()
        masters = {}
        for kid, encoded in configured.items():
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', kid):
                raise ValueError()
            masters[kid] = base64.b64decode(encoded, validate=True)
            if len(masters[kid]) != 32:
                raise ValueError()
        domain = json.dumps(['erupted:inventory-draft:v2', scope['principal_uuid'],
            scope['store_uuid'], scope['kind'], scope['record_uuid']], separators=(',', ':')).encode()
        derived = {kid: base64.b64encode(hmac.new(master, domain, hashlib.sha256).digest()).decode()
                   for kid, master in masters.items()}
        return {'scope': scope, 'active_key_id': active, 'keys': derived}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(503, 'Protected draft keys are unavailable. Drafts are retained; ask an administrator for recovery assistance.') from None


def assert_draft_identity(request, row, db, principal):
    if request.headers.get('X-Inventory-Draft') == 'true':
        scope = recovery_scope(db, principal_id=principal.id, kind='blind-count', row=row)
        if any(request.headers.get(header) != scope[field] for header, field in (
                ('X-Inventory-Identity', 'record_uuid'), ('X-Inventory-Principal', 'principal_uuid'),
                ('X-Inventory-Store', 'store_uuid'))):
            raise HTTPException(409, 'Stock-take identity differs. Protected work is quarantined; it cannot be imported into this count.')


def guard_write(db, *, request, principal, kind, row, form):
    """Serialize legacy form retries using its existing audit trail; no new business tables."""
    from app.models import AuditLog
    import json
    if request.headers.get('X-Inventory-Draft') != 'true':
        return None
    assert_draft_identity(request, row, db, principal)
    operation = request.headers.get('X-Draft-Operation', '')
    if not 1 <= len(operation) <= 64:
        raise HTTPException(400, 'A draft operation ID is required')
    fields = sorted((k, str(v)) for k, v in form.multi_items() if k not in {'csrf_token', 'working_context'})
    digest = hashlib.sha256(json.dumps([request.url.path, fields]).encode()).hexdigest()
    prior = db.scalar(select(AuditLog).where(AuditLog.actor_principal_id == principal.id,
        AuditLog.meta['draft_recovery']['operation'].as_string() == operation,
        AuditLog.meta['draft_recovery']['kind'].as_string() == kind,
        AuditLog.meta['draft_recovery']['record_id'].as_integer() == row.id))
    version = row.updated_at.astimezone(timezone.utc).isoformat()
    if prior is not None:
        evidence = prior.meta['draft_recovery']
        if evidence['digest'] != digest or evidence['revision'] != version:
            raise HTTPException(409, 'This draft changed elsewhere. Review the saved and protected values.')
        return draft_state(db, kind=kind, row=row)
    if request.headers.get('X-Draft-Version') != version:
        raise HTTPException(409, 'This draft changed elsewhere. Review the saved and protected values.')
    request.state.draft_recovery_operation = {'operation': operation, 'digest': digest, 'kind': kind, 'record_id': row.id}
    return None


def audit_metadata(request, row, metadata):
    operation = getattr(request.state, 'draft_recovery_operation', None)
    if operation:
        from sqlalchemy.orm import object_session
        db = object_session(row)
        # PostgreSQL's updated_at trigger owns the final version, not the pre-flush Python clock.
        db.flush()
        db.refresh(row, ['updated_at'])
        metadata = {**metadata, 'draft_recovery': {**operation, 'revision': row.updated_at.astimezone(timezone.utc).isoformat()}}
    return metadata


def recovery_labels(db, *, kind, row):
    """Labels only; never return historical quantities, expected inventory or variances."""
    from app.models import SnapshotLine, NonSellableStockTakeLine
    if kind == 'blind-count':
        return {r.variation_id: f'{r.item_name} — {r.variation_name}' for r in db.execute(
            select(SnapshotLine.variation_id, SnapshotLine.item_name, SnapshotLine.variation_name).where(SnapshotLine.session_id == row.id))}
    if kind == 'non-sellable-stock-take':
        labels = {f'qty__{r.item_id}': r.item_name for r in db.execute(
            select(NonSellableStockTakeLine.item_id, NonSellableStockTakeLine.item_name).where(NonSellableStockTakeLine.stock_take_id == row.id))}
    elif kind == 'change-box-count':
        from app.services.change_box_count_service import DENOM_BY_CODE
        labels = {f'qty_{area}__{code}': f'{meta["label"]} · {area.title()}'
                  for code, meta in DENOM_BY_CODE.items() for area in ('rolls', 'loose')}
    else:
        from app.services.daily_chore_service import get_store_sheet_rows
        labels = {f'task:{r["task_id"]}': r['prompt'] for r in get_store_sheet_rows(db, sheet_id=row.id)}
    return {'employee_name': 'Counted by', **labels}
