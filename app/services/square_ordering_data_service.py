from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Store, Vendor, VendorSkuConfig


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _square_post(path: str, payload: dict) -> dict:
    if not settings.square_access_token:
        raise RuntimeError('SQUARE_ACCESS_TOKEN is required')

    headers = {
        'Authorization': f'Bearer {settings.square_access_token}',
        'Content-Type': 'application/json',
    }
    if settings.square_api_version:
        headers['Square-Version'] = settings.square_api_version

    req = Request(
        url=f'{settings.square_api_base_url.rstrip("/")}{path}',
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urlopen(req, timeout=settings.square_timeout_seconds) as response:
            parsed = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        body = exc.read().decode('utf-8', errors='ignore') if exc.fp else ''
        raise RuntimeError(f'Square API error {exc.code}: {body}') from exc
    except URLError as exc:
        raise RuntimeError(f'Square API network error: {exc.reason}') from exc

    if parsed.get('errors'):
        raise RuntimeError(f"Square API returned errors: {parsed['errors']}")
    return parsed


@dataclass(frozen=True)
class SquareSkuMeta:
    variation_id: str
    sku: str
    item_name: str
    variation_name: str
    unit_cost: Decimal | None
    unit_price: Decimal | None


@dataclass
class SquareOrderingSnapshot:
    meta_by_vendor_sku: dict[tuple[int, str], SquareSkuMeta]
    on_hand_by_store_sku: dict[tuple[int, str], Decimal]
    history_by_vendor_store_sku: dict[tuple[int, int, str], list[Decimal]]

    def history_loader(self, vendor_id: int, store_id: int, sku: str, lookback_days: int) -> list[Decimal]:
        series = self.history_by_vendor_store_sku.get((vendor_id, store_id, sku), [])
        if not series:
            return []
        return series[-lookback_days:]

    def on_hand_loader(self, store_id: int, sku: str) -> Decimal:
        return self.on_hand_by_store_sku.get((store_id, sku), Decimal('0'))

    def meta_for(self, vendor_id: int, sku: str) -> SquareSkuMeta | None:
        return self.meta_by_vendor_sku.get((vendor_id, sku))


def _active_store_location_map(db: Session) -> dict[int, str]:
    rows = db.execute(
        select(Store.id, Store.square_location_id).where(
            Store.active.is_(True),
            Store.square_location_id.is_not(None),
        )
    ).all()
    return {int(row.id): str(row.square_location_id) for row in rows if row.square_location_id}


def fetch_catalog_by_sku() -> dict[str, SquareSkuMeta]:
    items: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {'limit': 100}
        if cursor:
            payload['cursor'] = cursor
        response = _square_post('/v2/catalog/search-catalog-items', payload)
        items.extend(response.get('items', []))
        cursor = response.get('cursor')
        if not cursor:
            break

    by_sku: dict[str, SquareSkuMeta] = {}
    for item in items:
        item_data = item.get('item_data') or {}
        item_name = str(item_data.get('name') or item.get('name') or '').strip()
        for variation in item_data.get('variations', []) or []:
            variation_id = variation.get('id')
            vdata = variation.get('item_variation_data') or {}
            sku = str(vdata.get('sku') or '').strip()
            if not variation_id or not sku:
                continue
            if sku in by_sku:
                continue
            price_money = (vdata.get('price_money') or {})
            price = None
            amount = price_money.get('amount')
            if amount is not None:
                try:
                    price = (Decimal(str(amount)) / Decimal('100')).quantize(Decimal('0.01'))
                except Exception:
                    price = None
            by_sku[sku] = SquareSkuMeta(
                variation_id=str(variation_id),
                sku=sku,
                item_name=item_name or sku,
                variation_name=str(vdata.get('name') or 'Default'),
                unit_cost=None,
                unit_price=price,
            )
    return by_sku


def _active_vendor_square_map(db: Session, *, vendor_ids: list[int] | None = None) -> dict[str, int]:
    query = select(Vendor.id, Vendor.square_vendor_id).where(
        Vendor.active.is_(True),
    )
    if vendor_ids:
        query = query.where(Vendor.id.in_(vendor_ids))
    rows = db.execute(query).all()
    out: dict[str, int] = {}
    for row in rows:
        square_vendor_id = str(row.square_vendor_id or '').strip()
        if not square_vendor_id:
            continue
        out[square_vendor_id] = int(row.id)
    return out


def _first_vendor_assignment(vdata: dict) -> str:
    infos = vdata.get('item_variation_vendor_info_data') or []
    ranked: list[tuple[int, str]] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        info_data = info.get('item_variation_vendor_info_data') if 'item_variation_vendor_info_data' in info else info
        if not isinstance(info_data, dict):
            continue
        vendor_id = str(info_data.get('vendor_id') or '').strip()
        if not vendor_id:
            continue
        ordinal_raw = info_data.get('ordinal')
        try:
            ordinal = int(ordinal_raw) if ordinal_raw is not None else 999999
        except Exception:
            ordinal = 999999
        ranked.append((ordinal, vendor_id))
    if ranked:
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    info_ids = vdata.get('item_variation_vendor_info_ids') or []
    if isinstance(info_ids, list):
        info_by_id: dict[str, dict] = {}
        for info in infos:
            if not isinstance(info, dict):
                continue
            info_id = str(info.get('id') or '').strip()
            if not info_id:
                continue
            info_by_id[info_id] = info
        for info_id in info_ids:
            key = str(info_id or '').strip()
            if not key:
                continue
            info = info_by_id.get(key)
            if not info:
                continue
            info_data = info.get('item_variation_vendor_info_data') if 'item_variation_vendor_info_data' in info else info
            if not isinstance(info_data, dict):
                continue
            vendor_id = str(info_data.get('vendor_id') or '').strip()
            if vendor_id:
                return vendor_id

    return ''


def sync_vendor_sku_configs_from_square(db: Session, *, vendor_ids: list[int] | None = None) -> dict[str, int]:
    """
    Build vendor SKU mappings from Square catalog vendor assignments.

    This mirrors reporter behavior: use Square vendor->variation->SKU mappings first,
    and only require manual mappings where Square has no assignment.
    """
    vendor_square_map = _active_vendor_square_map(db, vendor_ids=vendor_ids)
    if not vendor_square_map:
        return {
            'created': 0,
            'updated': 0,
            'skipped_missing_vendor_assignment': 0,
            'skipped_missing_sku': 0,
            'skipped_conflict_default_vendor': 0,
        }

    rows = db.execute(
        select(VendorSkuConfig).where(
            VendorSkuConfig.active.is_(True),
        )
    ).scalars().all()
    existing_by_vendor_sku = {(int(row.vendor_id), row.sku): row for row in rows}
    default_vendor_by_sku: dict[str, int] = {}
    for row in rows:
        if not row.is_default_vendor:
            continue
        default_vendor_by_sku.setdefault(row.sku, int(row.vendor_id))

    created = 0
    updated = 0
    skipped_missing_vendor_assignment = 0
    skipped_missing_sku = 0
    skipped_conflict_default_vendor = 0

    cursor: str | None = None
    while True:
        payload: dict = {'limit': 100}
        if cursor:
            payload['cursor'] = cursor
        response = _square_post('/v2/catalog/search-catalog-items', payload)
        for item in response.get('items', []):
            item_data = item.get('item_data') or {}
            for variation in item_data.get('variations', []) or []:
                variation_id = str(variation.get('id') or '').strip()
                vdata = variation.get('item_variation_data') or {}
                sku = str(vdata.get('sku') or '').strip()
                if not sku:
                    skipped_missing_sku += 1
                    continue

                square_vendor_id = _first_vendor_assignment(vdata)
                if not square_vendor_id:
                    skipped_missing_vendor_assignment += 1
                    continue

                vendor_id = vendor_square_map.get(square_vendor_id)
                if vendor_id is None:
                    continue

                key = (vendor_id, sku)
                existing = existing_by_vendor_sku.get(key)
                if existing is not None:
                    if variation_id and (existing.square_variation_id or '') != variation_id:
                        existing.square_variation_id = variation_id
                        existing.updated_at = _now()
                        updated += 1
                    continue

                default_vendor_id = default_vendor_by_sku.get(sku)
                if default_vendor_id is not None and default_vendor_id != vendor_id:
                    skipped_conflict_default_vendor += 1
                    continue

                row = VendorSkuConfig(
                    vendor_id=vendor_id,
                    sku=sku,
                    square_variation_id=variation_id or None,
                    pack_size=1,
                    min_order_qty=0,
                    is_default_vendor=True,
                    active=True,
                )
                db.add(row)
                db.flush()
                existing_by_vendor_sku[key] = row
                default_vendor_by_sku.setdefault(sku, vendor_id)
                created += 1

        cursor = response.get('cursor')
        if not cursor:
            break

    return {
        'created': created,
        'updated': updated,
        'skipped_missing_vendor_assignment': skipped_missing_vendor_assignment,
        'skipped_missing_sku': skipped_missing_sku,
        'skipped_conflict_default_vendor': skipped_conflict_default_vendor,
    }


def _fetch_on_hand(location_ids: list[str], variation_ids: list[str]) -> dict[tuple[str, str], Decimal]:
    out: dict[tuple[str, str], Decimal] = {}
    if not location_ids or not variation_ids:
        return out
    batch_size = 100
    for i in range(0, len(variation_ids), batch_size):
        chunk = variation_ids[i : i + batch_size]
        cursor: str | None = None
        while True:
            payload: dict = {
                'catalog_object_ids': chunk,
                'location_ids': location_ids,
                'states': ['IN_STOCK'],
                'limit': 100,
            }
            if cursor:
                payload['cursor'] = cursor
            response = _square_post('/v2/inventory/batch-retrieve-counts', payload)
            for row in response.get('counts', []):
                loc = row.get('location_id')
                obj = row.get('catalog_object_id')
                if not loc or not obj:
                    continue
                out[(str(loc), str(obj))] = Decimal(str(row.get('quantity', '0')))
            cursor = response.get('cursor')
            if not cursor:
                break
    return out


def _fetch_daily_sales(location_ids: list[str], start_at: datetime, end_at: datetime) -> dict[tuple[str, str, datetime.date], Decimal]:
    out: dict[tuple[str, str, datetime.date], Decimal] = {}
    if not location_ids:
        return out
    cursor: str | None = None
    while True:
        payload: dict = {
            'location_ids': location_ids,
            'query': {
                'filter': {
                    'date_time_filter': {
                        'closed_at': {
                            'start_at': _to_iso(start_at),
                            'end_at': _to_iso(end_at),
                        }
                    },
                    'state_filter': {'states': ['COMPLETED']},
                }
            },
            'limit': 500,
        }
        if cursor:
            payload['cursor'] = cursor
        response = _square_post('/v2/orders/search', payload)
        for order in response.get('orders', []):
            location_id = str(order.get('location_id') or '')
            closed_at_raw = str(order.get('closed_at') or '')
            if not location_id or not closed_at_raw:
                continue
            try:
                closed_at = datetime.fromisoformat(closed_at_raw.replace('Z', '+00:00')).date()
            except ValueError:
                continue
            for line in order.get('line_items', []) or []:
                variation_id = line.get('catalog_object_id')
                qty_raw = line.get('quantity')
                if not variation_id or qty_raw is None:
                    continue
                qty = Decimal(str(qty_raw))
                key = (location_id, str(variation_id), closed_at)
                out[key] = out.get(key, Decimal('0')) + qty
        cursor = response.get('cursor')
        if not cursor:
            break
    return out


def build_square_ordering_snapshot(db: Session, *, vendor_ids: list[int], lookback_days: int) -> SquareOrderingSnapshot:
    if not vendor_ids:
        return SquareOrderingSnapshot({}, {}, {})
    if not settings.square_access_token:
        raise RuntimeError('SQUARE_ACCESS_TOKEN is required for Square-backed order generation')

    store_location_map = _active_store_location_map(db)
    if not store_location_map:
        raise RuntimeError('No active stores have square_location_id configured')

    rows = db.execute(
        select(VendorSkuConfig).where(
            VendorSkuConfig.vendor_id.in_(vendor_ids),
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
        )
    ).scalars().all()
    if not rows:
        return SquareOrderingSnapshot({}, {}, {})

    catalog_by_sku = fetch_catalog_by_sku()
    meta_by_vendor_sku: dict[tuple[int, str], SquareSkuMeta] = {}
    for row in rows:
        sku = row.sku.strip()
        if not sku:
            continue
        meta = None
        if row.square_variation_id:
            # Build minimal meta from mapped variation id; enrich from SKU lookup if available.
            fallback = catalog_by_sku.get(sku)
            meta = SquareSkuMeta(
                variation_id=row.square_variation_id,
                sku=sku,
                item_name=fallback.item_name if fallback else sku,
                variation_name=fallback.variation_name if fallback else 'Default',
                unit_cost=fallback.unit_cost if fallback else None,
                unit_price=fallback.unit_price if fallback else None,
            )
        else:
            fallback = catalog_by_sku.get(sku)
            if fallback:
                meta = fallback
                row.square_variation_id = fallback.variation_id
        if meta:
            meta_by_vendor_sku[(row.vendor_id, sku)] = meta

    db.flush()
    if not meta_by_vendor_sku:
        return SquareOrderingSnapshot({}, {}, {})

    variation_ids = sorted({meta.variation_id for meta in meta_by_vendor_sku.values()})
    location_ids = sorted(set(store_location_map.values()))
    on_hand_by_loc_var = _fetch_on_hand(location_ids, variation_ids)

    on_hand_by_store_sku: dict[tuple[int, str], Decimal] = {}
    for (vendor_id, sku), meta in meta_by_vendor_sku.items():
        _ = vendor_id
        for store_id, loc_id in store_location_map.items():
            on_hand_by_store_sku[(store_id, sku)] = on_hand_by_loc_var.get((loc_id, meta.variation_id), Decimal('0'))

    end_at = _now()
    start_at = end_at - timedelta(days=lookback_days)
    daily_sales = _fetch_daily_sales(location_ids, start_at, end_at)
    start_day = start_at.date()

    history_by_vendor_store_sku: dict[tuple[int, int, str], list[Decimal]] = {}
    for (vendor_id, sku), meta in meta_by_vendor_sku.items():
        for store_id, loc_id in store_location_map.items():
            series: list[Decimal] = []
            for i in range(lookback_days):
                day = start_day + timedelta(days=i)
                series.append(daily_sales.get((loc_id, meta.variation_id, day), Decimal('0')))
            history_by_vendor_store_sku[(vendor_id, store_id, sku)] = series

    return SquareOrderingSnapshot(
        meta_by_vendor_sku=meta_by_vendor_sku,
        on_hand_by_store_sku=on_hand_by_store_sku,
        history_by_vendor_store_sku=history_by_vendor_store_sku,
    )
