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
from app.models import ParLevel, ParLevelSource, Store, Vendor, VendorSkuConfig
from app.services.square_request_policy import enforce_square_request_policy


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _square_post(path: str, payload: dict, *, inventory_quantity_write: bool = False) -> dict:
    enforce_square_request_policy(
        'POST', path, payload, inventory_quantity_write=inventory_quantity_write
    )
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
    gtin: str | None
    item_name: str
    variation_name: str
    # Reserved for a locally approved cost when this object is part of an
    # ordering snapshot. A catalog-only read must leave it unknown.
    unit_cost: Decimal | None
    unit_price: Decimal | None


@dataclass(frozen=True)
class CatalogVariationMeta:
    variation_id: str
    sku: str
    gtin: str | None
    item_name: str
    variation_name: str
    unit_price: Decimal | None
    vendor_cost_by_square_vendor_id: dict[str, Decimal]
    first_vendor_unit_cost: Decimal | None


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


def _money_from_cents(raw_amount: object) -> Decimal | None:
    if raw_amount is None:
        return None
    try:
        return (Decimal(str(raw_amount)) / Decimal('100')).quantize(Decimal('0.01'))
    except Exception:
        return None


def _extract_vendor_costs(vdata: dict) -> tuple[dict[str, Decimal], Decimal | None]:
    infos = (
        vdata.get('item_variation_vendor_infos')
        or vdata.get('item_variation_vendor_info_data')
        or []
    )
    by_vendor: dict[str, Decimal] = {}
    ranked: list[tuple[int, str, Decimal]] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        info_data = info.get('item_variation_vendor_info_data') if 'item_variation_vendor_info_data' in info else info
        if not isinstance(info_data, dict):
            continue
        vendor_id = str(info_data.get('vendor_id') or '').strip()
        if not vendor_id:
            continue
        cost = _money_from_cents((info_data.get('price_money') or {}).get('amount'))
        if cost is None:
            continue
        by_vendor.setdefault(vendor_id, cost)
        ordinal_raw = info_data.get('ordinal')
        try:
            ordinal = int(ordinal_raw) if ordinal_raw is not None else 999999
        except Exception:
            ordinal = 999999
        ranked.append((ordinal, vendor_id, cost))

    first_cost: Decimal | None = None
    if ranked:
        ranked.sort(key=lambda item: item[0])
        first_cost = ranked[0][2]
    return by_vendor, first_cost


def fetch_catalog_variation_maps() -> tuple[dict[str, CatalogVariationMeta], dict[str, CatalogVariationMeta]]:
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

    by_variation_id: dict[str, CatalogVariationMeta] = {}
    by_sku: dict[str, CatalogVariationMeta] = {}
    for item in items:
        item_data = item.get('item_data') or {}
        item_name = str(item_data.get('name') or item.get('name') or '').strip()
        for variation in item_data.get('variations', []) or []:
            variation_id = str(variation.get('id') or '').strip()
            vdata = variation.get('item_variation_data') or {}
            sku = str(vdata.get('sku') or '').strip()
            gtin = str(vdata.get('upc') or '').strip() or None
            if not variation_id:
                continue
            unit_price = _money_from_cents((vdata.get('price_money') or {}).get('amount'))
            vendor_cost_by_square_vendor_id, first_vendor_unit_cost = _extract_vendor_costs(vdata)
            meta = CatalogVariationMeta(
                variation_id=variation_id,
                sku=sku,
                gtin=gtin,
                item_name=item_name or sku,
                variation_name=str(vdata.get('name') or 'Default'),
                unit_price=unit_price,
                vendor_cost_by_square_vendor_id=vendor_cost_by_square_vendor_id,
                first_vendor_unit_cost=first_vendor_unit_cost,
            )
            by_variation_id[variation_id] = meta
            if sku:
                by_sku.setdefault(sku, meta)
    return by_variation_id, by_sku


def fetch_catalog_by_sku() -> dict[str, SquareSkuMeta]:
    _, by_sku_meta = fetch_catalog_variation_maps()
    by_sku: dict[str, SquareSkuMeta] = {}
    for sku, meta in by_sku_meta.items():
        by_sku[sku] = SquareSkuMeta(
            variation_id=meta.variation_id,
            sku=sku,
            gtin=meta.gtin,
            item_name=meta.item_name,
            variation_name=meta.variation_name,
            unit_cost=None,
            unit_price=meta.unit_price,
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
    infos = (
        vdata.get('item_variation_vendor_infos')
        or vdata.get('item_variation_vendor_info_data')
        or []
    )
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


def _mapping_precedence(row: VendorSkuConfig) -> tuple[int, str, int]:
    return (
        1 if bool(row.active) else 0,
        row.updated_at.isoformat() if row.updated_at is not None else '',
        int(row.id or 0),
    )


def _par_is_explicit(row: ParLevel) -> bool:
    return bool(
        row.manual_par_level is not None
        or row.manual_stock_up_level is not None
        or row.locked_manual
        or row.par_source == ParLevelSource.MANUAL
    )


def _par_precedence(row: ParLevel) -> tuple[int, str, int]:
    return (
        1 if _par_is_explicit(row) else 0,
        row.updated_at.isoformat() if row.updated_at is not None else '',
        int(row.id or 0),
    )


_PAR_CONFIGURATION_FIELDS = (
    'manual_par_level',
    'manual_stock_up_level',
    'suggested_par_level',
    'par_source',
    'confidence_score',
    'confidence_state',
    'locked_manual',
    'confidence_streak_up',
    'confidence_streak_down',
    'updated_by_principal_id',
)


def _carry_forward_par_levels(
    db: Session,
    *,
    sku: str,
    source_vendor_id: int,
    destination_vendor_id: int,
    par_rows_by_vendor_sku: dict[tuple[int, str], list[ParLevel]],
) -> tuple[int, int]:
    source_rows = par_rows_by_vendor_sku.get((source_vendor_id, sku), [])
    if not source_rows:
        return 0, 0

    source_by_store: dict[int | None, ParLevel] = {}
    for row in source_rows:
        store_id = int(row.store_id) if row.store_id is not None else None
        current = source_by_store.get(store_id)
        if current is None or _par_precedence(row) > _par_precedence(current):
            source_by_store[store_id] = row

    destination_rows = par_rows_by_vendor_sku.setdefault((destination_vendor_id, sku), [])
    destination_by_store: dict[int | None, ParLevel] = {}
    for row in destination_rows:
        store_id = int(row.store_id) if row.store_id is not None else None
        current = destination_by_store.get(store_id)
        if current is None or _par_precedence(row) > _par_precedence(current):
            destination_by_store[store_id] = row

    created = 0
    updated = 0
    for store_id, source in source_by_store.items():
        destination = destination_by_store.get(store_id)
        if destination is not None and _par_is_explicit(destination):
            continue
        if destination is None:
            destination = ParLevel(
                vendor_id=destination_vendor_id,
                store_id=store_id,
                sku=sku,
            )
            for field in _PAR_CONFIGURATION_FIELDS:
                setattr(destination, field, getattr(source, field))
            db.add(destination)
            destination_rows.append(destination)
            destination_by_store[store_id] = destination
            created += 1
            continue

        changed = False
        for field in _PAR_CONFIGURATION_FIELDS:
            value = getattr(source, field)
            if getattr(destination, field) != value:
                setattr(destination, field, value)
                changed = True
        if changed:
            destination.updated_at = _now()
            updated += 1
    return created, updated


def sync_vendor_sku_configs_from_square(db: Session, *, vendor_ids: list[int] | None = None) -> dict[str, object]:
    """
    Build vendor SKU mappings from Square catalog vendor assignments.

    This mirrors reporter behavior: use Square vendor->variation->SKU mappings first,
    and only require manual mappings where Square has no assignment.
    """
    # Load all active vendors so a selected old vendor can be safely reassigned
    # to Square's current vendor even when the destination was not selected.
    vendor_square_map = _active_vendor_square_map(db)
    if not vendor_square_map:
        return {
            'created': 0,
            'updated': 0,
            'reactivated': 0,
            'vendor_reassigned': 0,
            'par_created': 0,
            'par_updated': 0,
            'skipped_missing_vendor_assignment': 0,
            'skipped_missing_sku': 0,
            'reassignments': [],
        }

    rows = db.execute(select(VendorSkuConfig)).scalars().all()
    existing_by_vendor_sku = {(int(row.vendor_id), row.sku): row for row in rows}
    mappings_by_sku: dict[str, list[VendorSkuConfig]] = {}
    for row in rows:
        mappings_by_sku.setdefault(str(row.sku), []).append(row)

    par_rows = db.execute(select(ParLevel)).scalars().all()
    par_rows_by_vendor_sku: dict[tuple[int, str], list[ParLevel]] = {}
    for row in par_rows:
        if row.vendor_id is None:
            continue
        par_rows_by_vendor_sku.setdefault((int(row.vendor_id), str(row.sku)), []).append(row)

    scoped_vendor_ids = {int(vendor_id) for vendor_id in (vendor_ids or [])}

    created = 0
    updated = 0
    reactivated = 0
    vendor_reassigned = 0
    par_created = 0
    par_updated = 0
    skipped_missing_vendor_assignment = 0
    skipped_missing_sku = 0
    reassignments: list[dict[str, object]] = []
    processed: set[tuple[int, str]] = set()
    dirty = False

    cursor: str | None = None
    while True:
        payload: dict = {'limit': 100}
        if cursor:
            payload['cursor'] = cursor
        response = _square_post('/v2/catalog/search-catalog-items', payload)
        for item in response.get('items', []):
            item_data = item.get('item_data') or {}
            item_variations = item_data.get('variations', []) or []
            item_square_vendor_ids: set[str] = set()
            for variation in item_variations:
                vdata = variation.get('item_variation_data') or {}
                assigned = _first_vendor_assignment(vdata)
                if assigned:
                    item_square_vendor_ids.add(assigned)
            inherited_square_vendor_id = next(iter(item_square_vendor_ids)) if len(item_square_vendor_ids) == 1 else ''

            for variation in item_variations:
                variation_id = str(variation.get('id') or '').strip()
                vdata = variation.get('item_variation_data') or {}
                sku = str(vdata.get('sku') or '').strip()
                if not sku and variation_id:
                    # Keep SKU-less variations visible in ordering by assigning a stable synthetic key.
                    sku = f'VAR::{variation_id}'
                if not sku:
                    skipped_missing_sku += 1
                    continue

                square_vendor_id = _first_vendor_assignment(vdata)
                if not square_vendor_id and inherited_square_vendor_id:
                    # Square can omit variation-level vendor assignment on sibling variants.
                    # If an item has one unambiguous vendor assignment, inherit it.
                    square_vendor_id = inherited_square_vendor_id
                if not square_vendor_id:
                    skipped_missing_vendor_assignment += 1
                    continue

                vendor_id = vendor_square_map.get(square_vendor_id)
                if vendor_id is None:
                    continue
                sku_mappings = mappings_by_sku.setdefault(sku, [])
                if scoped_vendor_ids and vendor_id not in scoped_vendor_ids and not any(
                    int(row.vendor_id) in scoped_vendor_ids and bool(row.is_default_vendor)
                    for row in sku_mappings
                ):
                    continue
                key = (vendor_id, sku)
                if key in processed:
                    continue
                processed.add(key)
                existing = existing_by_vendor_sku.get(key)
                destination_was_current = bool(
                    existing is not None and existing.active and existing.is_default_vendor
                )
                prior_defaults = [
                    row
                    for row in sku_mappings
                    if int(row.vendor_id) != vendor_id and bool(row.is_default_vendor)
                ]
                prior_default = max(prior_defaults, key=_mapping_precedence) if prior_defaults else None
                is_reassignment = prior_default is not None and not destination_was_current

                # PostgreSQL enforces one active/default mapping per SKU with a
                # non-deferrable partial unique index.  Demote and flush every
                # other default before the destination can become active/default;
                # otherwise a new/reactivated destination creates a transient
                # duplicate that the database correctly rejects.
                if prior_defaults:
                    for stale in prior_defaults:
                        stale.is_default_vendor = False
                        stale.updated_at = _now()
                    db.flush()
                    dirty = True

                if existing is None:
                    existing = VendorSkuConfig(
                        vendor_id=vendor_id,
                        sku=sku,
                        square_variation_id=variation_id or None,
                        gtin=str(vdata.get('upc') or '').strip() or None,
                        # This is a local-to-local carry-forward. Square cost is
                        # never consulted, and unknown remains unknown.
                        unit_cost=(prior_default.unit_cost if prior_default is not None else None),
                        pack_size=(int(prior_default.pack_size) if prior_default is not None else 1),
                        min_order_qty=(int(prior_default.min_order_qty) if prior_default is not None else 0),
                        is_default_vendor=True,
                        active=True,
                    )
                    db.add(existing)
                    db.flush()
                    existing_by_vendor_sku[key] = existing
                    sku_mappings.append(existing)
                    created += 1
                    dirty = True
                else:
                    changed = False
                    if not existing.active:
                        existing.active = True
                        reactivated += 1
                        changed = True
                    if not existing.is_default_vendor:
                        existing.is_default_vendor = True
                        changed = True
                    if variation_id and (existing.square_variation_id or '') != variation_id:
                        existing.square_variation_id = variation_id
                        changed = True
                    gtin = str(vdata.get('upc') or '').strip() or None
                    if (existing.gtin or None) != gtin:
                        existing.gtin = gtin
                        changed = True
                    if existing.unit_cost is None and prior_default is not None and prior_default.unit_cost is not None:
                        existing.unit_cost = prior_default.unit_cost
                        changed = True
                    if changed:
                        existing.updated_at = _now()
                        updated += 1
                        dirty = True
                        db.flush()

                if is_reassignment and prior_default is not None:
                    carried_created, carried_updated = _carry_forward_par_levels(
                        db,
                        sku=sku,
                        source_vendor_id=int(prior_default.vendor_id),
                        destination_vendor_id=vendor_id,
                        par_rows_by_vendor_sku=par_rows_by_vendor_sku,
                    )
                    par_created += carried_created
                    par_updated += carried_updated
                    dirty = dirty or bool(carried_created or carried_updated)
                    vendor_reassigned += 1
                    reassignments.append({
                        'sku': sku,
                        'old_vendor_id': int(prior_default.vendor_id),
                        'new_vendor_id': vendor_id,
                    })

        cursor = response.get('cursor')
        if not cursor:
            break

    if dirty:
        db.flush()
    return {
        'created': created,
        'updated': updated,
        'reactivated': reactivated,
        'vendor_reassigned': vendor_reassigned,
        'par_created': par_created,
        'par_updated': par_updated,
        'skipped_missing_vendor_assignment': skipped_missing_vendor_assignment,
        'skipped_missing_sku': skipped_missing_sku,
        'reassignments': reassignments,
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


def fetch_on_hand_by_store_variation(
    db: Session,
    *,
    variation_ids: list[str],
    store_ids: list[int] | None = None,
) -> dict[tuple[int, str], Decimal]:
    if not variation_ids:
        return {}
    store_location_map = _active_store_location_map(db)
    if store_ids is not None:
        allowed = set(store_ids)
        store_location_map = {sid: loc for sid, loc in store_location_map.items() if sid in allowed}
    if not store_location_map:
        return {}
    location_ids = sorted(set(store_location_map.values()))
    on_hand_by_loc_var = _fetch_on_hand(location_ids, variation_ids)
    by_store_var: dict[tuple[int, str], Decimal] = {}
    for store_id, location_id in store_location_map.items():
        for variation_id in variation_ids:
            by_store_var[(store_id, variation_id)] = on_hand_by_loc_var.get((location_id, variation_id), Decimal('0'))
    return by_store_var


def fetch_sales_volume_by_variation(
    db: Session,
    *,
    variation_ids: list[str],
    lookback_days: int = 30,
    store_ids: list[int] | None = None,
) -> dict[str, Decimal]:
    clean_variation_ids = sorted({str(value).strip() for value in variation_ids if str(value or '').strip()})
    if not clean_variation_ids:
        return {}
    if lookback_days < 1:
        raise ValueError('lookback_days must be at least 1')

    store_location_map = _active_store_location_map(db)
    if store_ids is not None:
        allowed = set(store_ids)
        store_location_map = {sid: loc for sid, loc in store_location_map.items() if sid in allowed}
    if not store_location_map:
        return {variation_id: Decimal('0') for variation_id in clean_variation_ids}

    end_at = _now()
    start_at = end_at - timedelta(days=lookback_days)
    allowed_location_ids = set(store_location_map.values())
    daily_sales = _fetch_daily_sales(sorted(allowed_location_ids), start_at, end_at)

    allowed_variation_ids = set(clean_variation_ids)
    totals = {variation_id: Decimal('0') for variation_id in clean_variation_ids}
    for (location_id, variation_id, _day), qty in daily_sales.items():
        if location_id not in allowed_location_ids:
            continue
        if variation_id not in allowed_variation_ids:
            continue
        totals[variation_id] = totals[variation_id] + qty
    return totals


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


def build_square_ordering_snapshot(
    db: Session,
    *,
    vendor_ids: list[int],
    lookback_days: int,
    include_non_default_vendor_skus: bool = False,
) -> SquareOrderingSnapshot:
    if not vendor_ids:
        return SquareOrderingSnapshot({}, {}, {})
    if not settings.square_access_token:
        raise RuntimeError('SQUARE_ACCESS_TOKEN is required for Square-backed order generation')

    store_location_map = _active_store_location_map(db)
    if not store_location_map:
        raise RuntimeError('No active stores have square_location_id configured')

    query = select(VendorSkuConfig).where(
        VendorSkuConfig.vendor_id.in_(vendor_ids),
        VendorSkuConfig.active.is_(True),
    )
    if not include_non_default_vendor_skus:
        query = query.where(VendorSkuConfig.is_default_vendor.is_(True))
    rows = db.execute(query).scalars().all()
    if not rows:
        return SquareOrderingSnapshot({}, {}, {})

    catalog_by_variation_id, catalog_by_sku = fetch_catalog_variation_maps()
    meta_by_vendor_sku: dict[tuple[int, str], SquareSkuMeta] = {}
    for row in rows:
        sku = row.sku.strip()
        if not sku:
            continue
        variation_meta: CatalogVariationMeta | None = None
        if row.square_variation_id:
            variation_meta = catalog_by_variation_id.get(row.square_variation_id)
        if variation_meta is None:
            variation_meta = catalog_by_sku.get(sku)
            if variation_meta:
                row.square_variation_id = variation_meta.variation_id
        if variation_meta is None:
            continue

        unit_cost = Decimal(str(row.unit_cost)) if row.unit_cost is not None else None

        meta_by_vendor_sku[(row.vendor_id, sku)] = SquareSkuMeta(
            variation_id=variation_meta.variation_id,
            sku=sku,
            gtin=variation_meta.gtin or row.gtin,
            item_name=variation_meta.item_name,
            variation_name=variation_meta.variation_name,
            unit_cost=unit_cost,
            unit_price=variation_meta.unit_price,
        )

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
