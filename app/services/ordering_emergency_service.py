from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SquareSyncEvent, SquareSyncStatus, Store, Vendor, VendorSkuConfig
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
        variation_id = (
            str(row.square_variation_id or '').strip()
            or (str(meta.variation_id) if meta else '')
        )
        options.append(
            {
                'sku': sku,
                'item_name': item_name,
                'variation_name': variation_name,
                'variation_id': variation_id,
                'search_text': f"{sku} {item_name} {variation_name}".lower(),
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


def build_emergency_editor_detail(
    db: Session,
    *,
    vendor_id: int | None,
    selected_skus: list[str],
) -> dict:
    vendors = _active_vendors(db)
    vendor_ids = {int(vendor.id) for vendor in vendors}
    valid_vendor_id = int(vendor_id) if vendor_id is not None and int(vendor_id) in vendor_ids else None
    stores = _active_store_rows(db)

    if valid_vendor_id is None:
        return {
            'vendors': vendors,
            'selected_vendor_id': None,
            'stores': stores,
            'selected_skus': [],
            'lookup_options': [],
            'rows': [],
        }

    options = _vendor_sku_options(db, vendor_id=valid_vendor_id)
    option_by_sku = {str(option['sku']): option for option in options}
    clean_skus: list[str] = []
    seen: set[str] = set()
    for sku in selected_skus:
        clean = str(sku or '').strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        clean_skus.append(clean)

    variation_ids = sorted(
        {
            str(option_by_sku[sku]['variation_id'])
            for sku in clean_skus
            if sku in option_by_sku and str(option_by_sku[sku]['variation_id']).strip()
        }
    )
    on_hand_by_store_variation = fetch_on_hand_by_store_variation(
        db,
        variation_ids=variation_ids,
        store_ids=[store['store_id'] for store in stores],
    ) if variation_ids else {}

    rows: list[dict] = []
    for sku in clean_skus:
        option = option_by_sku.get(sku)
        if option is None:
            rows.append(
                {
                    'sku': sku,
                    'item_name': sku,
                    'variation_name': 'Unknown',
                    'variation_id': '',
                    'missing_mapping': True,
                    'store_values': [
                        {
                            'store_id': store['store_id'],
                            'store_name': store['store_name'],
                            'on_hand_qty': Decimal('0'),
                        }
                        for store in stores
                    ],
                }
            )
            continue
        variation_id = str(option['variation_id'] or '').strip()
        store_values = []
        for store in stores:
            qty = on_hand_by_store_variation.get((int(store['store_id']), variation_id), Decimal('0')) if variation_id else Decimal('0')
            store_values.append(
                {
                    'store_id': store['store_id'],
                    'store_name': store['store_name'],
                    'on_hand_qty': qty,
                }
            )
        rows.append(
            {
                'sku': sku,
                'item_name': str(option['item_name']),
                'variation_name': str(option['variation_name']),
                'variation_id': variation_id,
                'missing_mapping': not bool(variation_id),
                'store_values': store_values,
            }
        )

    return {
        'vendors': vendors,
        'selected_vendor_id': valid_vendor_id,
        'stores': stores,
        'selected_skus': clean_skus,
        'lookup_options': options,
        'rows': rows,
    }


def push_emergency_true_counts(
    db: Session,
    *,
    rows: list[dict],
    stores: list[dict],
    quantities_by_variation_store: dict[tuple[str, int], Decimal],
) -> dict:
    location_by_store_id = {int(store['store_id']): str(store.get('square_location_id') or '').strip() for store in stores}
    row_by_variation = {
        str(row.get('variation_id') or ''): row
        for row in rows
        if str(row.get('variation_id') or '').strip()
    }

    attempted = 0
    succeeded = 0
    failed = 0
    failed_rows: list[dict] = []
    now = _now()

    for (variation_id, store_id), qty in quantities_by_variation_store.items():
        clean_variation_id = str(variation_id or '').strip()
        location_id = location_by_store_id.get(int(store_id), '')
        row = row_by_variation.get(clean_variation_id)
        sku = str(row.get('sku') or '') if row else ''
        item_name = str(row.get('item_name') or '') if row else ''
        variation_name = str(row.get('variation_name') or '') if row else ''
        store_name = next((str(store['store_name']) for store in stores if int(store['store_id']) == int(store_id)), '')

        attempted += 1
        if not clean_variation_id or clean_variation_id.startswith('SKU::'):
            failed += 1
            failed_rows.append({'store_name': store_name, 'sku': sku, 'item_name': item_name, 'variation_name': variation_name, 'error': 'Missing Square variation mapping'})
            continue
        if not location_id:
            failed += 1
            failed_rows.append({'store_name': store_name, 'sku': sku, 'item_name': item_name, 'variation_name': variation_name, 'error': 'Store missing square_location_id'})
            continue
        if qty < 0:
            failed += 1
            failed_rows.append({'store_name': store_name, 'sku': sku, 'item_name': item_name, 'variation_name': variation_name, 'error': 'Quantity cannot be negative'})
            continue

        idempotency_key = f'ordering-emergency-{store_id}-{clean_variation_id}-{uuid4().hex}'
        event = SquareSyncEvent(
            purchase_order_id=None,
            purchase_order_line_id=None,
            store_id=int(store_id),
            sync_type=ORDERING_EMERGENCY_SQUARE_SYNC_TYPE,
            idempotency_key=idempotency_key,
            status=SquareSyncStatus.PENDING,
            request_payload={
                'store_id': int(store_id),
                'store_name': store_name,
                'location_id': location_id,
                'variation_id': clean_variation_id,
                'sku': sku,
                'item_name': item_name,
                'variation_name': variation_name,
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
                        'catalog_object_id': clean_variation_id,
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
                    'store_name': store_name,
                    'sku': sku,
                    'item_name': item_name,
                    'variation_name': variation_name,
                    'error': str(exc),
                }
            )
        event.attempt_count = 1
        event.last_attempt_at = _now()
        db.flush()

    return {
        'attempted': attempted,
        'succeeded': succeeded,
        'failed': failed,
        'failed_rows': failed_rows,
    }
