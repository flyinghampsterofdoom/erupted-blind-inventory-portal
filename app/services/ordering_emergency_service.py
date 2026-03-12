from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    EmergencyOnHandDraft,
    EmergencyOnHandDraftLine,
    EmergencyOnHandDraftStatus,
    SquareSyncEvent,
    SquareSyncStatus,
    Store,
    Vendor,
    VendorSkuConfig,
)
from app.services.square_ordering_data_service import _square_post, fetch_catalog_by_sku, fetch_on_hand_by_store_variation

ORDERING_EMERGENCY_SQUARE_SYNC_TYPE = 'ORDERING_EMERGENCY_SET_ON_HAND'


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _format_square_quantity(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, 'f')
    return text if '.' in text else f'{text}.000'


def _active_vendors(db: Session) -> list[Vendor]:
    return db.execute(select(Vendor).where(Vendor.active.is_(True)).order_by(Vendor.name.asc())).scalars().all()


def _active_store_rows(db: Session) -> list[dict]:
    rows = db.execute(
        select(Store.id, Store.name, Store.square_location_id)
        .where(Store.active.is_(True))
        .order_by(Store.name.asc())
    ).all()
    return [
        {
            'store_id': int(row.id),
            'store_name': str(row.name),
            'square_location_id': str(row.square_location_id or '').strip(),
        }
        for row in rows
    ]


def _vendor_sku_options(db: Session, *, vendor_id: int) -> list[dict]:
    catalog = fetch_catalog_by_sku()
    rows = db.execute(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id == vendor_id,
            VendorSkuConfig.active.is_(True),
        )
        .order_by(VendorSkuConfig.sku.asc())
    ).scalars().all()
    options: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        sku = str(row.sku or '').strip()
        if not sku or sku in seen:
            continue
        seen.add(sku)
        meta = catalog.get(sku)
        item_name = meta.item_name if meta else sku
        variation_name = meta.variation_name if meta else 'Default'
        variation_id = str(row.square_variation_id or '').strip() or (str(meta.variation_id) if meta else '')
        options.append(
            {
                'sku': sku,
                'item_name': item_name,
                'variation_name': variation_name,
                'variation_id': variation_id,
                'search_text': f'{sku} {item_name} {variation_name}'.lower(),
            }
        )
    return options


def resolve_lookup_sku(
    db: Session,
    *,
    vendor_id: int,
    lookup: str,
) -> str:
    term = str(lookup or '').strip()
    if not term:
        raise ValueError('Lookup is required')
    options = _vendor_sku_options(db, vendor_id=vendor_id)
    if not options:
        raise ValueError('No active vendor SKUs available for this vendor')

    by_sku_lower = {str(option['sku']).lower(): str(option['sku']) for option in options}
    exact = by_sku_lower.get(term.lower())
    if exact:
        return exact

    matches = [option for option in options if term.lower() in str(option['search_text'])]
    if not matches:
        raise ValueError('No vendor SKU matched that lookup')
    matches.sort(key=lambda option: str(option['sku']).lower())
    return str(matches[0]['sku'])


def create_emergency_draft(db: Session, *, vendor_id: int, principal_id: int) -> EmergencyOnHandDraft:
    vendor = db.execute(select(Vendor).where(Vendor.id == vendor_id, Vendor.active.is_(True))).scalar_one_or_none()
    if vendor is None:
        raise ValueError('Vendor not found')
    draft = EmergencyOnHandDraft(
        vendor_id=vendor_id,
        status=EmergencyOnHandDraftStatus.DRAFT,
        created_by_principal_id=principal_id,
    )
    db.add(draft)
    db.flush()
    return draft


def get_emergency_draft(db: Session, *, draft_id: int) -> EmergencyOnHandDraft:
    draft = db.execute(select(EmergencyOnHandDraft).where(EmergencyOnHandDraft.id == draft_id)).scalar_one_or_none()
    if draft is None:
        raise ValueError('Emergency on-hand draft not found')
    return draft


def _load_line_rows(
    db: Session,
    *,
    draft: EmergencyOnHandDraft,
) -> list[EmergencyOnHandDraftLine]:
    return db.execute(
        select(EmergencyOnHandDraftLine)
        .where(EmergencyOnHandDraftLine.draft_id == draft.id)
        .order_by(EmergencyOnHandDraftLine.item_name.asc(), EmergencyOnHandDraftLine.variation_name.asc())
    ).scalars().all()


def _line_store_values(
    *,
    line: EmergencyOnHandDraftLine,
    stores: list[dict],
) -> list[dict]:
    raw_map = line.store_quantities if isinstance(line.store_quantities, dict) else {}
    values: list[dict] = []
    for store in stores:
        store_id = int(store['store_id'])
        raw = raw_map.get(str(store_id))
        try:
            qty = Decimal(str(raw)) if raw is not None else Decimal('0')
        except Exception:
            qty = Decimal('0')
        values.append(
            {
                'store_id': store_id,
                'store_name': store['store_name'],
                'on_hand_qty': qty,
            }
        )
    return values


def build_emergency_editor_detail(
    db: Session,
    *,
    draft_id: int | None,
) -> dict:
    vendors = _active_vendors(db)
    stores = _active_store_rows(db)
    if draft_id is None:
        return {
            'vendors': vendors,
            'draft': None,
            'stores': stores,
            'lookup_options': [],
            'rows': [],
        }

    draft = get_emergency_draft(db, draft_id=draft_id)
    lines = _load_line_rows(db, draft=draft)
    rows = [
        {
            'line_id': int(line.id),
            'sku': str(line.sku),
            'item_name': str(line.item_name),
            'variation_name': str(line.variation_name),
            'variation_id': str(line.variation_id or ''),
            'missing_mapping': not bool(str(line.variation_id or '').strip()),
            'store_values': _line_store_values(line=line, stores=stores),
        }
        for line in lines
    ]
    lookup_options = _vendor_sku_options(db, vendor_id=int(draft.vendor_id))
    return {
        'vendors': vendors,
        'draft': draft,
        'stores': stores,
        'lookup_options': lookup_options,
        'rows': rows,
    }


def add_sku_to_draft(
    db: Session,
    *,
    draft_id: int,
    sku: str,
) -> EmergencyOnHandDraftLine:
    draft = get_emergency_draft(db, draft_id=draft_id)
    if draft.status != EmergencyOnHandDraftStatus.DRAFT:
        raise ValueError('Only emergency drafts can be edited')

    clean_sku = str(sku or '').strip()
    if not clean_sku:
        raise ValueError('SKU is required')

    existing = db.execute(
        select(EmergencyOnHandDraftLine).where(
            EmergencyOnHandDraftLine.draft_id == draft.id,
            EmergencyOnHandDraftLine.sku == clean_sku,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    options = _vendor_sku_options(db, vendor_id=int(draft.vendor_id))
    option = next((item for item in options if str(item['sku']) == clean_sku), None)
    if option is None:
        raise ValueError('SKU is not mapped to this vendor')

    stores = _active_store_rows(db)
    variation_id = str(option['variation_id'] or '').strip()
    on_hand_map = fetch_on_hand_by_store_variation(
        db,
        variation_ids=[variation_id],
        store_ids=[store['store_id'] for store in stores],
    ) if variation_id else {}
    store_quantities: dict[str, str] = {}
    for store in stores:
        qty = on_hand_map.get((int(store['store_id']), variation_id), Decimal('0')) if variation_id else Decimal('0')
        store_quantities[str(int(store['store_id']))] = format(qty, 'f')

    line = EmergencyOnHandDraftLine(
        draft_id=draft.id,
        sku=clean_sku,
        item_name=str(option['item_name']),
        variation_name=str(option['variation_name']),
        variation_id=variation_id or None,
        store_quantities=store_quantities,
    )
    db.add(line)
    draft.updated_at = _now()
    db.flush()
    return line


def save_draft_quantities(
    db: Session,
    *,
    draft_id: int,
    quantities_by_line_store: dict[tuple[int, int], Decimal],
) -> EmergencyOnHandDraft:
    draft = get_emergency_draft(db, draft_id=draft_id)
    if draft.status != EmergencyOnHandDraftStatus.DRAFT:
        raise ValueError('Only emergency drafts can be edited')

    lines = _load_line_rows(db, draft=draft)
    lines_by_id = {int(line.id): line for line in lines}
    for (line_id, store_id), qty in quantities_by_line_store.items():
        if qty < 0:
            raise ValueError('Quantity cannot be negative')
        line = lines_by_id.get(int(line_id))
        if line is None:
            continue
        raw = line.store_quantities if isinstance(line.store_quantities, dict) else {}
        raw[str(int(store_id))] = format(qty, 'f')
        line.store_quantities = raw
        line.updated_at = _now()

    draft.updated_at = _now()
    db.flush()
    return draft


def push_emergency_draft(
    db: Session,
    *,
    draft_id: int,
    principal_id: int,
) -> dict:
    draft = get_emergency_draft(db, draft_id=draft_id)
    if draft.status != EmergencyOnHandDraftStatus.DRAFT:
        raise ValueError('Emergency draft already pushed')

    stores = _active_store_rows(db)
    lines = _load_line_rows(db, draft=draft)
    if not lines:
        raise ValueError('Emergency draft has no lines')

    location_by_store_id = {int(store['store_id']): str(store['square_location_id'] or '').strip() for store in stores}
    store_name_by_id = {int(store['store_id']): str(store['store_name']) for store in stores}
    now = _now()

    attempted = 0
    succeeded = 0
    failed = 0
    failed_rows: list[dict] = []

    for line in lines:
        variation_id = str(line.variation_id or '').strip()
        raw_map = line.store_quantities if isinstance(line.store_quantities, dict) else {}
        for store in stores:
            store_id = int(store['store_id'])
            raw_qty = raw_map.get(str(store_id))
            if raw_qty is None or str(raw_qty).strip() == '':
                continue
            try:
                qty = Decimal(str(raw_qty))
            except Exception:
                continue
            attempted += 1
            if qty < 0:
                failed += 1
                failed_rows.append(
                    {
                        'store_name': store_name_by_id.get(store_id, f'Store #{store_id}'),
                        'sku': line.sku,
                        'item_name': line.item_name,
                        'variation_name': line.variation_name,
                        'error': 'Quantity cannot be negative',
                    }
                )
                continue

            location_id = location_by_store_id.get(store_id, '')
            if not variation_id or variation_id.startswith('SKU::'):
                failed += 1
                failed_rows.append(
                    {
                        'store_name': store_name_by_id.get(store_id, f'Store #{store_id}'),
                        'sku': line.sku,
                        'item_name': line.item_name,
                        'variation_name': line.variation_name,
                        'error': 'Missing Square variation mapping',
                    }
                )
                continue
            if not location_id:
                failed += 1
                failed_rows.append(
                    {
                        'store_name': store_name_by_id.get(store_id, f'Store #{store_id}'),
                        'sku': line.sku,
                        'item_name': line.item_name,
                        'variation_name': line.variation_name,
                        'error': 'Store missing square_location_id',
                    }
                )
                continue

            idempotency_key = f'ordering-emergency-draft-{draft.id}-{line.id}-{store_id}-{uuid4().hex}'
            event = SquareSyncEvent(
                purchase_order_id=None,
                purchase_order_line_id=None,
                store_id=store_id,
                sync_type=ORDERING_EMERGENCY_SQUARE_SYNC_TYPE,
                idempotency_key=idempotency_key,
                status=SquareSyncStatus.PENDING,
                request_payload={
                    'draft_id': draft.id,
                    'store_id': store_id,
                    'store_name': store_name_by_id.get(store_id, ''),
                    'location_id': location_id,
                    'variation_id': variation_id,
                    'sku': line.sku,
                    'item_name': line.item_name,
                    'variation_name': line.variation_name,
                    'counted_qty': str(qty),
                    'source': 'ordering_emergency_editor',
                },
                response_payload=None,
                error_text=None,
                attempt_count=0,
                last_attempt_at=None,
            )
            db.add(event)
            db.flush()

            payload = {
                'idempotency_key': idempotency_key,
                'changes': [
                    {
                        'type': 'PHYSICAL_COUNT',
                        'physical_count': {
                            'catalog_object_id': variation_id,
                            'location_id': location_id,
                            'state': 'IN_STOCK',
                            'quantity': _format_square_quantity(qty),
                            'occurred_at': _to_iso(now),
                        },
                    }
                ],
                'ignore_unchanged_counts': False,
            }
            try:
                response = _square_post('/v2/inventory/changes/batch-create', payload)
                event.status = SquareSyncStatus.SUCCESS
                event.response_payload = response
                event.error_text = None
                succeeded += 1
            except RuntimeError as exc:
                event.status = SquareSyncStatus.FAILED
                event.response_payload = None
                event.error_text = str(exc)
                failed += 1
                failed_rows.append(
                    {
                        'store_name': store_name_by_id.get(store_id, f'Store #{store_id}'),
                        'sku': line.sku,
                        'item_name': line.item_name,
                        'variation_name': line.variation_name,
                        'error': str(exc),
                    }
                )
            event.attempt_count = 1
            event.last_attempt_at = _now()
            db.flush()

    if attempted <= 0:
        raise ValueError('No quantities available to push')

    if failed <= 0:
        draft.status = EmergencyOnHandDraftStatus.PUSHED
        draft.submitted_at = _now()
        draft.submitted_by_principal_id = principal_id
    draft.updated_at = _now()
    db.flush()
    return {
        'attempted': attempted,
        'succeeded': succeeded,
        'failed': failed,
        'failed_rows': failed_rows,
        'pushed': failed <= 0,
    }


def list_emergency_draft_history(db: Session, *, limit: int = 100) -> list[dict]:
    rows = db.execute(
        select(EmergencyOnHandDraft, Vendor.name)
        .join(Vendor, Vendor.id == EmergencyOnHandDraft.vendor_id)
        .order_by(EmergencyOnHandDraft.created_at.desc())
        .limit(limit)
    ).all()
    history: list[dict] = []
    for draft, vendor_name in rows:
        status = 'Emergency On hand Pushed' if draft.status == EmergencyOnHandDraftStatus.PUSHED else 'Emergency On hand Draft'
        history.append(
            {
                'id': int(draft.id),
                'display_id': f'E-{int(draft.id)}',
                'vendor_id': int(draft.vendor_id),
                'vendor_name': str(vendor_name),
                'status': status,
                'created_at': draft.created_at,
                'submitted_at': draft.submitted_at,
                'open_href': f'/management/ordering-tool/emergency-editor?draft_id={int(draft.id)}',
                'can_receive': False,
                'can_discard': False,
                'is_emergency': True,
            }
        )
    return history
