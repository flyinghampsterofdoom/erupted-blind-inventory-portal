from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from io import StringIO
from math import ceil
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select, func, text
from sqlalchemy.orm import Session

from app.models import (
    Principal, PurchaseOrderReceipt, PurchaseOrderReceiptLine, PurchaseOrderReceiptAllocation, PurchaseOrderReceiptStatus,
    ParLevel,
    ParLevelSource,
    PurchaseOrder,
    PurchaseOrderConfidenceState,
    PurchaseOrderPdfTemplate,
    SquareSyncEvent,
    SquareSyncStatus,
    PurchaseOrderLine,
    PurchaseOrderStoreAllocation,
    PurchaseOrderStatus,
    Store,
    Vendor,
    VendorSkuConfig,
)
from app.services.ordering_service import resolve_effective_math_params
from app.services.purchase_order_generation_service import generate_vendor_scoped_recommendations
from app.services.purchase_order_math_service import MathOverrides
from app.services.purchase_order_math_service import LineMathInput, compute_line_recommendation, resolve_math_params
from app.services.square_ordering_data_service import (
    _square_post,
    build_square_ordering_snapshot,
    fetch_catalog_variation_maps,
    fetch_on_hand_by_store_variation,
    fetch_sales_volume_by_variation,
    fetch_catalog_by_sku,
    sync_vendor_sku_configs_from_square,
)
from app.services.sort_utils import item_variation_sort_key
from app.services.inventory_velocity_report_service import StockCoveragePurchaseRow


INVOICE_PAYMENT_PAID = 'PAID'
INVOICE_PAYMENT_UNPAID = 'UNPAID'
STORE_RECEIVE_PRIORITY = ('HWY 99', 'Longview', 'Andresen', 'SR 503')


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _store_split_label(store_name: str) -> str:
    name = (store_name or '').strip()
    key = name.lower()
    if '99' in key:
        return '99'
    if 'andresen' in key:
        return 'A'
    if '503' in key:
        return '503'
    if 'longview' in key:
        return 'L'
    return name


def _store_receive_priority_key(store_name: str) -> tuple[int, str]:
    normalized = ''.join(ch for ch in (store_name or '').lower() if ch.isalnum())
    if '99' in normalized:
        return 0, ''
    if 'longview' in normalized:
        return 1, ''
    if 'andresen' in normalized:
        return 2, ''
    if '503' in normalized:
        return 3, ''
    return len(STORE_RECEIVE_PRIORITY), (store_name or '').lower()


def _line_extended_cost(line: PurchaseOrderLine) -> Decimal:
    if line.unit_cost is None:
        return Decimal('0.00')
    return (Decimal(str(line.unit_cost)) * Decimal(max(int(line.ordered_qty or 0), 0))).quantize(Decimal('0.01'))


def _purchase_order_invoice_amount(lines: list[PurchaseOrderLine]) -> Decimal:
    total = Decimal('0.00')
    for line in lines:
        if bool(line.removed):
            continue
        total += _line_extended_cost(line)
    return total.quantize(Decimal('0.01'))


def _decimal_to_money(value: Decimal | None) -> str:
    if value is None:
        return '-'
    try:
        amount = Decimal(str(value)).quantize(Decimal('0.01'))
    except Exception:
        return '-'
    return f'{amount:.2f}'


def _decimal_to_quantity_text(value: Decimal | None) -> str:
    if value is None:
        return '-'
    try:
        qty = Decimal(str(value))
    except Exception:
        return '-'
    if qty == qty.to_integral_value():
        return str(int(qty))
    return format(qty.normalize(), 'f')


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _format_square_quantity(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, 'f')
    return text if '.' in text else f'{text}.000'


def _parse_int(value: str | None, *, field: str, minimum: int | None = None) -> int:
    raw = (value or '').strip()
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f'Invalid {field}') from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f'{field} must be at least {minimum}')
    return parsed


def _parse_invoice_date(value: str | None) -> date | None:
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError as exc:
        raise ValueError('Paid date must use YYYY-MM-DD') from exc


def _parse_money(value: str | None, *, field: str) -> Decimal | None:
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw.replace('$', '').replace(',', '')).quantize(Decimal('0.01'))
    except Exception as exc:
        raise ValueError(f'Invalid {field}') from exc
    if parsed < 0:
        raise ValueError(f'{field} cannot be negative')
    return parsed


def list_active_vendors(db: Session) -> list[Vendor]:
    return db.execute(select(Vendor).where(Vendor.active.is_(True)).order_by(Vendor.name.asc())).scalars().all()


def list_purchase_order_pdf_template_assignments(db: Session) -> dict:
    vendors = list_active_vendors(db)
    rows = db.execute(
        select(PurchaseOrderPdfTemplate).where(PurchaseOrderPdfTemplate.active.is_(True))
    ).scalars().all()
    generic = next((row for row in rows if row.is_generic), None)
    vendor_templates: dict[int, PurchaseOrderPdfTemplate] = {
        int(row.vendor_id): row
        for row in rows
        if row.vendor_id is not None and not row.is_generic
    }
    vendors_by_id = {int(vendor.id): vendor.name for vendor in vendors}
    return {
        'generic': generic,
        'templates': [
            {
                'id': int(row.id),
                'name': row.name,
                'legal_disclaimer': row.legal_disclaimer or '',
                'is_generic': bool(row.is_generic),
                'vendor_id': int(row.vendor_id) if row.vendor_id is not None else None,
                'vendor_name': (
                    'Generic Fallback'
                    if row.is_generic
                    else vendors_by_id.get(int(row.vendor_id), f'Vendor #{row.vendor_id}')
                ),
                'updated_at': row.updated_at,
            }
            for row in sorted(rows, key=lambda r: (0 if r.is_generic else 1, str(r.name).lower(), int(r.id)))
        ],
        'vendors': [
            {
                'id': int(vendor.id),
                'name': vendor.name,
                'template_name': (
                    vendor_templates[int(vendor.id)].name
                    if int(vendor.id) in vendor_templates
                    else None
                ),
                'has_template': int(vendor.id) in vendor_templates,
            }
            for vendor in vendors
        ],
    }


def save_purchase_order_pdf_template_assignments(
    db: Session,
    *,
    name: str,
    legal_disclaimer: str | None,
    apply_generic: bool,
    vendor_ids: list[int],
    updated_by_principal_id: int,
) -> int:
    normalized_name = (name or '').strip()
    if not normalized_name:
        raise ValueError('Template name is required')

    normalized_disclaimer = (legal_disclaimer or '').strip() or None
    touched = 0

    if apply_generic:
        generic = db.execute(
            select(PurchaseOrderPdfTemplate).where(PurchaseOrderPdfTemplate.is_generic.is_(True))
        ).scalar_one_or_none()
        if generic is None:
            generic = PurchaseOrderPdfTemplate(
                name=normalized_name,
                legal_disclaimer=normalized_disclaimer,
                is_generic=True,
                vendor_id=None,
                active=True,
                updated_by_principal_id=updated_by_principal_id,
            )
            db.add(generic)
        else:
            generic.name = normalized_name
            generic.legal_disclaimer = normalized_disclaimer
            generic.active = True
            generic.updated_by_principal_id = updated_by_principal_id
            generic.updated_at = _now()
        touched += 1

    for vendor_id in sorted(set(vendor_ids)):
        existing = db.execute(
            select(PurchaseOrderPdfTemplate).where(
                PurchaseOrderPdfTemplate.vendor_id == vendor_id,
                PurchaseOrderPdfTemplate.is_generic.is_(False),
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = PurchaseOrderPdfTemplate(
                name=normalized_name,
                legal_disclaimer=normalized_disclaimer,
                is_generic=False,
                vendor_id=vendor_id,
                active=True,
                updated_by_principal_id=updated_by_principal_id,
            )
            db.add(existing)
        else:
            existing.name = normalized_name
            existing.legal_disclaimer = normalized_disclaimer
            existing.active = True
            existing.updated_by_principal_id = updated_by_principal_id
            existing.updated_at = _now()
        touched += 1

    if touched <= 0:
        raise ValueError('Select at least one vendor or apply as generic template')
    db.flush()
    return touched


def update_purchase_order_pdf_template(
    db: Session,
    *,
    template_id: int,
    name: str,
    legal_disclaimer: str | None,
    updated_by_principal_id: int,
) -> PurchaseOrderPdfTemplate:
    template = db.execute(
        select(PurchaseOrderPdfTemplate).where(PurchaseOrderPdfTemplate.id == template_id)
    ).scalar_one_or_none()
    if template is None or not template.active:
        raise ValueError('Template not found')
    normalized_name = (name or '').strip()
    if not normalized_name:
        raise ValueError('Template name is required')
    template.name = normalized_name
    template.legal_disclaimer = (legal_disclaimer or '').strip() or None
    template.updated_by_principal_id = updated_by_principal_id
    template.updated_at = _now()
    db.flush()
    return template


def list_vendor_par_level_rows(
    db: Session,
    *,
    vendor_id: int,
    history_lookback_days: int,
) -> dict:
    vendor = db.execute(select(Vendor).where(Vendor.id == vendor_id, Vendor.active.is_(True))).scalar_one_or_none()
    if vendor is None:
        raise ValueError('Vendor not found')

    sku_rows = db.execute(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id == vendor_id,
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
        )
        .order_by(VendorSkuConfig.sku.asc())
    ).scalars().all()
    if not sku_rows:
        return {'vendor': vendor, 'stores': [], 'items': []}

    store_rows = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    store_ids = [int(row.id) for row in store_rows]
    if not store_ids:
        return {'vendor': vendor, 'stores': [], 'items': []}

    stores = [
        {
            'id': int(store.id),
            'name': str(store.name),
            'label': _store_split_label(str(store.name)),
        }
        for store in store_rows
    ]

    snapshot = build_square_ordering_snapshot(db, vendor_ids=[vendor_id], lookback_days=history_lookback_days)
    vendor_defaults = resolve_effective_math_params(db, vendor_id=vendor_id)
    params = resolve_math_params(
        vendor_defaults,
        MathOverrides(history_lookback_days=history_lookback_days),
    )

    par_rows = db.execute(select(ParLevel).where(ParLevel.vendor_id == vendor_id)).scalars().all()
    par_by_store_sku: dict[tuple[int | None, str], ParLevel] = {}
    for par in par_rows:
        key = (int(par.store_id), par.sku) if par.store_id is not None else (None, par.sku)
        par_by_store_sku[key] = par

    items: list[dict] = []
    for sku_row in sku_rows:
        sku = sku_row.sku
        meta = snapshot.meta_for(vendor_id, sku)
        store_cells: list[dict] = []
        for store in stores:
            store_id = int(store['id'])
            par = par_by_store_sku.get((store_id, sku)) or par_by_store_sku.get((None, sku))
            history = snapshot.history_loader(vendor_id, store_id, sku, params.history_lookback_days)
            on_hand = snapshot.on_hand_loader(store_id, sku)
            line = LineMathInput(
                sku=sku,
                current_on_hand=on_hand,
                in_transit_qty=0,
                history_daily_units=history,
                unit_pack_size=sku_row.pack_size,
                min_order_qty=sku_row.min_order_qty,
                manual_level=par.manual_par_level if par else None,
                manual_par=par.manual_stock_up_level if par else None,
                par_source=par.par_source if par else ParLevelSource.DYNAMIC,
            )
            result = compute_line_recommendation(line, params)
            store_cells.append(
                {
                    'store_id': store_id,
                    'store_name': store['name'],
                    'row_key': f'{store_id}|{sku}',
                    'on_hand_qty': int(on_hand) if on_hand >= 0 else 0,
                    'avg_weekly_units': str(result.avg_weekly_units),
                    'suggested_level': result.suggested_reorder_level,
                    'suggested_par': result.suggested_stock_up_level,
                    'effective_level': result.effective_reorder_level,
                    'effective_par': result.effective_stock_up_level,
                    'manual_level': par.manual_par_level if par else None,
                    'manual_par': par.manual_stock_up_level if par else None,
                    'par_source': (par.par_source.value if par else ParLevelSource.DYNAMIC.value),
                    'confidence_state': result.confidence_state.value,
                    'confidence_score': str(result.confidence_score),
                    'needs_manual': (
                        result.confidence_state == PurchaseOrderConfidenceState.LOW
                        and ((par is None) or par.manual_par_level is None or par.manual_stock_up_level is None)
                    ),
                }
            )

        items.append(
            {
                'sku': sku,
                'item_name': meta.item_name if meta else sku,
                'variation_name': meta.variation_name if meta else '-',
                'store_cells': store_cells,
                'needs_manual': any(cell['needs_manual'] for cell in store_cells),
            }
        )

    items.sort(
        key=lambda item: item_variation_sort_key(
            item_name=item.get('item_name'),
            variation_name=item.get('variation_name'),
        )
    )
    return {'vendor': vendor, 'stores': stores, 'items': items}


def save_vendor_store_par_levels(
    db: Session,
    *,
    vendor_id: int,
    entries: list[tuple[int, str, int | None, int | None]],
) -> int:
    vendor = db.execute(
        select(Vendor).where(Vendor.id == vendor_id, Vendor.active.is_(True))
    ).scalar_one_or_none()
    if vendor is None:
        raise ValueError(f'Active vendor {vendor_id} was not found')

    normalized_entries: list[tuple[int, str, int | None, int | None]] = []
    seen_rows: set[tuple[int, str]] = set()
    for store_id, raw_sku, manual_level, manual_par in entries:
        sku = (raw_sku or '').strip()
        if not sku:
            raise ValueError(f'SKU is required for store {store_id}')
        row_identity = (store_id, sku)
        if row_identity in seen_rows:
            raise ValueError(f'Duplicate change for SKU {sku} at store {store_id}')
        seen_rows.add(row_identity)
        if manual_level is not None and manual_level < 0:
            raise ValueError(f'Manual level cannot be negative for SKU {sku} at store {store_id}')
        if manual_par is not None and manual_par < 0:
            raise ValueError(f'Manual par cannot be negative for SKU {sku} at store {store_id}')
        normalized_entries.append((store_id, sku, manual_level, manual_par))

    store_ids = {store_id for store_id, _, _, _ in normalized_entries}
    if store_ids:
        valid_store_ids = set(
            db.execute(
                select(Store.id).where(Store.id.in_(store_ids), Store.active.is_(True))
            ).scalars()
        )
        invalid_store_ids = sorted(store_ids - valid_store_ids)
        if invalid_store_ids:
            raise ValueError(f'Active store {invalid_store_ids[0]} was not found')

    skus = {sku for _, sku, _, _ in normalized_entries}
    if skus:
        mapped_skus = set(
            db.execute(
                select(VendorSkuConfig.sku).where(
                    VendorSkuConfig.vendor_id == vendor_id,
                    VendorSkuConfig.sku.in_(skus),
                    VendorSkuConfig.active.is_(True),
                    VendorSkuConfig.is_default_vendor.is_(True),
                )
            ).scalars()
        )
        invalid_skus = sorted(skus - mapped_skus)
        if invalid_skus:
            raise ValueError(
                f'SKU {invalid_skus[0]} is not an active default mapping for vendor {vendor_id}'
            )

    existing_by_identity: dict[tuple[int, str], ParLevel] = {}
    if normalized_entries:
        existing_rows = db.execute(
            select(ParLevel).where(
                ParLevel.vendor_id == vendor_id,
                ParLevel.store_id.in_(store_ids),
                ParLevel.sku.in_(skus),
            )
        ).scalars().all()
        existing_by_identity = {
            (int(row.store_id), row.sku): row
            for row in existing_rows
            if row.store_id is not None
        }

    saved = 0
    for store_id, sku, manual_level, manual_par in normalized_entries:
        existing = existing_by_identity.get((store_id, sku))
        if existing is None:
            existing = ParLevel(
                vendor_id=vendor_id,
                store_id=store_id,
                sku=sku,
                manual_par_level=manual_level,
                manual_stock_up_level=manual_par,
                par_source=(
                    ParLevelSource.MANUAL
                    if (manual_level is not None or manual_par is not None)
                    else ParLevelSource.DYNAMIC
                ),
            )
            db.add(existing)
        else:
            existing.manual_par_level = manual_level
            existing.manual_stock_up_level = manual_par
            existing.par_source = (
                ParLevelSource.MANUAL
                if (manual_level is not None or manual_par is not None)
                else ParLevelSource.DYNAMIC
            )
            existing.updated_at = _now()
        saved += 1
    db.flush()
    return saved


def prefill_vendor_store_par_levels_from_living(
    db: Session,
    *,
    vendor_id: int,
    history_lookback_days: int,
) -> int:
    vendor = db.execute(select(Vendor).where(Vendor.id == vendor_id, Vendor.active.is_(True))).scalar_one_or_none()
    if vendor is None:
        raise ValueError('Vendor not found')

    sku_rows = db.execute(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id == vendor_id,
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
        )
        .order_by(VendorSkuConfig.sku.asc())
    ).scalars().all()
    if not sku_rows:
        return 0

    store_rows = db.execute(select(Store.id).where(Store.active.is_(True)).order_by(Store.name.asc())).all()
    if not store_rows:
        return 0

    snapshot = build_square_ordering_snapshot(db, vendor_ids=[vendor_id], lookback_days=history_lookback_days)
    vendor_defaults = resolve_effective_math_params(db, vendor_id=vendor_id)
    params = resolve_math_params(
        vendor_defaults,
        MathOverrides(history_lookback_days=history_lookback_days),
    )

    par_rows = db.execute(select(ParLevel).where(ParLevel.vendor_id == vendor_id)).scalars().all()
    par_by_store_sku: dict[tuple[int, str], ParLevel] = {}
    for par in par_rows:
        if par.store_id is None:
            continue
        par_by_store_sku[(int(par.store_id), par.sku)] = par

    prefilled = 0
    for sku_row in sku_rows:
        sku = sku_row.sku
        for store_row in store_rows:
            store_id = int(store_row.id)
            existing = par_by_store_sku.get((store_id, sku))
            if existing is None:
                existing = ParLevel(
                    vendor_id=vendor_id,
                    store_id=store_id,
                    sku=sku,
                    manual_par_level=None,
                    manual_stock_up_level=None,
                    par_source=ParLevelSource.DYNAMIC,
                )
                db.add(existing)
                par_by_store_sku[(store_id, sku)] = existing

            if existing.manual_par_level is not None and existing.manual_stock_up_level is not None:
                continue

            history = snapshot.history_loader(vendor_id, store_id, sku, params.history_lookback_days)
            on_hand = snapshot.on_hand_loader(store_id, sku)
            line = LineMathInput(
                sku=sku,
                current_on_hand=on_hand,
                in_transit_qty=0,
                history_daily_units=history,
                unit_pack_size=sku_row.pack_size,
                min_order_qty=sku_row.min_order_qty,
                manual_level=None,
                manual_par=None,
                par_source=ParLevelSource.DYNAMIC,
            )
            result = compute_line_recommendation(line, params)

            changed = False
            if existing.manual_par_level is None:
                existing.manual_par_level = result.suggested_reorder_level
                changed = True
            if existing.manual_stock_up_level is None:
                existing.manual_stock_up_level = result.suggested_stock_up_level
                changed = True

            if changed:
                existing.suggested_par_level = result.suggested_stock_up_level
                existing.confidence_score = result.confidence_score
                existing.confidence_state = result.confidence_state
                existing.par_source = ParLevelSource.MANUAL
                existing.locked_manual = True
                existing.updated_at = _now()
                prefilled += 1

    db.flush()
    return prefilled


def list_purchase_orders(db: Session, *, limit: int = 100) -> list[dict]:
    rows = db.execute(
        select(PurchaseOrder, Vendor.name)
        .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
        .order_by(PurchaseOrder.created_at.desc())
        .limit(limit)
    ).all()
    order_ids = [int(po.id) for po, _vendor_name in rows]
    lines_by_order_id: dict[int, list[PurchaseOrderLine]] = {order_id: [] for order_id in order_ids}
    if order_ids:
        line_rows = db.execute(
            select(PurchaseOrderLine)
            .where(PurchaseOrderLine.purchase_order_id.in_(order_ids))
        ).scalars().all()
        for line in line_rows:
            lines_by_order_id.setdefault(int(line.purchase_order_id), []).append(line)
    return [
        {
            'id': po.id,
            'vendor_id': po.vendor_id,
            'vendor_name': vendor_name,
            'status': po.status.value,
            'invoice_payment_status': po.invoice_payment_status or INVOICE_PAYMENT_UNPAID,
            'invoice_amount': _purchase_order_invoice_amount(lines_by_order_id.get(int(po.id), [])),
            'invoice_paid_amount': po.invoice_paid_amount,
            'received_qty_total': sum(
                max(int(line.received_qty_total or 0), 0)
                for line in lines_by_order_id.get(int(po.id), [])
                if not bool(line.removed)
            ),
            'ordered_qty_total': sum(
                max(int(line.ordered_qty or 0), 0)
                for line in lines_by_order_id.get(int(po.id), [])
                if not bool(line.removed)
            ),
            'created_at': po.created_at,
            'submitted_at': po.submitted_at,
            'ordered_at': po.ordered_at,
        }
        for po, vendor_name in rows
    ]


def generate_purchase_orders(
    db: Session,
    *,
    vendor_ids: list[int],
    created_by_principal_id: int,
    reorder_weeks: int,
    stock_up_weeks: int,
    history_lookback_days: int,
    include_full_stock_lines: bool = False,
) -> tuple[list[PurchaseOrder], list[str]]:
    if not vendor_ids:
        raise ValueError('Select at least one vendor')

    sync_vendor_sku_configs_from_square(db, vendor_ids=vendor_ids)

    mapped_count = db.execute(
        select(VendorSkuConfig.id)
        .where(
            VendorSkuConfig.vendor_id.in_(vendor_ids),
            VendorSkuConfig.active.is_(True),
        )
        .limit(1)
    ).first()
    if not mapped_count:
        raise ValueError(
            'No vendor SKU mappings found for selected vendors. Square did not return vendor assignments for these SKUs; configure vendor_sku_configs manually.'
        )

    overrides = MathOverrides(
        reorder_weeks=reorder_weeks,
        stock_up_weeks=stock_up_weeks,
        history_lookback_days=history_lookback_days,
    )
    snapshot = build_square_ordering_snapshot(
        db,
        vendor_ids=vendor_ids,
        lookback_days=history_lookback_days,
        include_non_default_vendor_skus=include_full_stock_lines,
    )
    if not snapshot.meta_by_vendor_sku:
        raise ValueError(
            'No Square catalog mappings resolved for selected vendors. Confirm SKU values and/or square_variation_id mappings.'
        )

    lines = generate_vendor_scoped_recommendations(
        db,
        vendor_ids=vendor_ids,
        history_loader=snapshot.history_loader,
        on_hand_loader=snapshot.on_hand_loader,
        overrides=overrides,
        include_zero_qty=include_full_stock_lines,
        include_non_default_vendor_skus=include_full_stock_lines,
    )
    if not lines:
        raise ValueError(
            'No order quantities generated. Check live inventory coverage, incoming commitments, and demand; unknown pairs cannot generate quantities.'
        )
    vendor_name_by_id = {
        int(row.id): str(row.name)
        for row in db.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(vendor_ids))).all()
    }
    warnings: list[str] = []
    coverage = snapshot.inventory_coverage
    if coverage.get('missing_pairs') or coverage.get('mapping_issues') or coverage.get('unresolved_skus'):
        warnings.append(f"Live inventory coverage is partial: {len(coverage.get('missing_pairs', []))} unknown pairs; "
            f"mapping issues: {', '.join(coverage.get('mapping_issues', [])) or 'none'}; "
            f"unresolved SKUs: {', '.join(coverage.get('unresolved_skus', [])) or 'none'}. Unknown pairs were excluded.")
    from app.config import settings
    if settings.purchasing_v3_phase1_enabled:
        from app.services.purchasing_evidence_service import record_run
        record_run(db, source='INVENTORY', status='PARTIAL' if warnings else 'COMPLETE',
            started_at=snapshot.inventory_observed_at, completed_at=_now(),
            scope={'variation_ids': sorted({m.variation_id for m in snapshot.meta_by_vendor_sku.values()}),
                   'store_ids': sorted({sid for sid, _ in coverage.get('requested_pairs', [])})},
            evidence=coverage)
    variation_aliases: dict[tuple[int, str], list[str]] = {}
    for (vendor_id, sku), meta in snapshot.meta_by_vendor_sku.items():
        key = (int(vendor_id), str(meta.variation_id))
        skus = variation_aliases.setdefault(key, [])
        clean_sku = str(sku).strip()
        if clean_sku and clean_sku not in skus:
            skus.append(clean_sku)
    for (vendor_id, variation_id), skus in sorted(variation_aliases.items(), key=lambda item: (item[0][0], item[0][1])):
        if len(skus) <= 1:
            continue
        vendor_name = vendor_name_by_id.get(vendor_id, f'Vendor #{vendor_id}')
        preview = ', '.join(sorted(skus)[:3])
        suffix = '...' if len(skus) > 3 else ''
        warnings.append(
            f"{vendor_name}: variation '{variation_id}' maps to multiple SKUs ({preview}{suffix}). "
            'Continuing with SKU-first order lines.'
        )

    grouped: dict[tuple[int, str], list] = {}
    par_rows = db.execute(select(ParLevel).where(ParLevel.vendor_id.in_(vendor_ids))).scalars().all()
    par_by_vendor_store_sku: dict[tuple[int, int, str], ParLevel] = {}
    for par in par_rows:
        if par.vendor_id is None or par.store_id is None:
            continue
        par_by_vendor_store_sku[(int(par.vendor_id), int(par.store_id), par.sku)] = par

    for line in lines:
        existing_par = par_by_vendor_store_sku.get((line.vendor_id, line.store_id, line.sku))
        if existing_par is None:
            existing_par = ParLevel(
                vendor_id=line.vendor_id,
                store_id=line.store_id,
                sku=line.sku,
                manual_par_level=None,
                manual_stock_up_level=None,
            )
            db.add(existing_par)
            db.flush()
            par_by_vendor_store_sku[(line.vendor_id, line.store_id, line.sku)] = existing_par
        existing_par.suggested_par_level = line.result.suggested_stock_up_level
        existing_par.confidence_score = line.result.confidence_score
        existing_par.confidence_state = line.result.confidence_state
        existing_par.par_source = (
            ParLevelSource.MANUAL
            if (existing_par.manual_par_level is not None or existing_par.manual_stock_up_level is not None)
            else ParLevelSource.DYNAMIC
        )
        existing_par.locked_manual = existing_par.par_source == ParLevelSource.MANUAL
        existing_par.updated_at = _now()
        grouped.setdefault((line.vendor_id, line.sku), []).append(line)

    orders_by_vendor: dict[int, PurchaseOrder] = {}
    created_orders: list[PurchaseOrder] = []
    variation_ids_by_order_id: dict[int, set[str]] = {}
    for (vendor_id, sku), store_lines in grouped.items():
        po = orders_by_vendor.get(vendor_id)
        if po is None:
            po = PurchaseOrder(
                vendor_id=vendor_id,
                status=PurchaseOrderStatus.DRAFT,
                reorder_weeks=reorder_weeks,
                stock_up_weeks=stock_up_weeks,
                history_lookback_days=history_lookback_days,
                created_by_principal_id=created_by_principal_id,
            )
            db.add(po)
            db.flush()
            orders_by_vendor[vendor_id] = po
            created_orders.append(po)
            variation_ids_by_order_id[po.id] = set()

        total_qty = sum(row.result.rounded_recommended_qty for row in store_lines)
        if total_qty <= 0 and not include_full_stock_lines:
            continue
        confidence_score = min(row.result.confidence_score for row in store_lines)
        confidence_state = (
            PurchaseOrderConfidenceState.LOW
            if any(row.result.confidence_state == PurchaseOrderConfidenceState.LOW for row in store_lines)
            else PurchaseOrderConfidenceState.NORMAL
        )
        suggested_qty = sum(row.result.suggested_stock_up_level for row in store_lines)
        suggested_par = sum(row.result.suggested_stock_up_level for row in store_lines)
        manual_par_values = [
            row.result.effective_reorder_level
            for row in store_lines
            if row.result.par_source == ParLevelSource.MANUAL
        ]
        line_manual_par_level = sum(manual_par_values) if manual_par_values else None
        base_result = store_lines[0].result
        meta = snapshot.meta_for(vendor_id, sku)
        # Full-stock mode should include all SKUs, but still keep standard reorder math
        # for quantities that actually trigger.
        ordered_qty = total_qty
        line_variation_id = meta.variation_id if meta else f'SKU::{sku}'
        existing_variations = variation_ids_by_order_id.setdefault(po.id, set())
        if line_variation_id in existing_variations:
            line_variation_id = f'SKU::{sku}'
            vendor_name = vendor_name_by_id.get(vendor_id, f'Vendor #{vendor_id}')
            warning = (
                f"{vendor_name}: duplicate variation line key for SKU '{sku}'. "
                'Using SKU-based key to keep both SKU lines.'
            )
            if warning not in warnings:
                warnings.append(warning)
        existing_variations.add(line_variation_id)
        po_line = PurchaseOrderLine(
            purchase_order_id=po.id,
            variation_id=line_variation_id,
            sku=sku,
            gtin=meta.gtin if meta else None,
            item_name=meta.item_name if meta else sku,
            variation_name=meta.variation_name if meta else 'Default',
            unit_cost=meta.unit_cost if meta else None,
            unit_price=meta.unit_price if meta else None,
            suggested_qty=suggested_qty,
            ordered_qty=ordered_qty,
            received_qty_total=0,
            in_transit_qty=ordered_qty,
            confidence_score=confidence_score,
            confidence_state=confidence_state,
            par_source=base_result.par_source,
            manual_par_level=line_manual_par_level,
            suggested_par_level=suggested_par,
            removed=False,
        )
        db.add(po_line)
        db.flush()
        for row in store_lines:
            allocated_qty = row.result.rounded_recommended_qty
            manual_par_level = (
                row.result.effective_reorder_level if row.result.par_source == ParLevelSource.MANUAL else None
            )
            db.add(
                PurchaseOrderStoreAllocation(
                    purchase_order_line_id=po_line.id,
                    store_id=row.store_id,
                    expected_qty=row.result.rounded_recommended_qty,
                    allocated_qty=allocated_qty,
                    manual_par_level=manual_par_level,
                    variance_qty=allocated_qty - row.result.rounded_recommended_qty,
                )
            )
    db.flush()
    return created_orders, warnings


def create_purchase_order_from_stock_coverage_rows(
    db: Session,
    *,
    vendor_id: int,
    rows: list[StockCoveragePurchaseRow],
    created_by_principal_id: int,
    history_lookback_days: int,
    target_months: Decimal,
) -> PurchaseOrder:
    vendor = db.execute(
        select(Vendor).where(Vendor.id == vendor_id, Vendor.active.is_(True))
    ).scalar_one_or_none()
    if vendor is None:
        raise ValueError('Vendor not found')

    selected_rows = [
        row
        for row in rows
        if row.vendor_id == vendor_id and row.recommended_purchase_quantity > 0
    ]
    if not selected_rows:
        raise ValueError('No suggested purchase items found for this vendor')

    skus = sorted({row.sku for row in selected_rows if row.sku})
    mapping_rows = db.execute(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id == vendor_id,
            VendorSkuConfig.sku.in_(skus),
            VendorSkuConfig.active.is_(True),
        )
        .order_by(VendorSkuConfig.is_default_vendor.desc(), VendorSkuConfig.id.asc())
    ).scalars().all()
    mapping_by_sku: dict[str, VendorSkuConfig] = {}
    for mapping in mapping_rows:
        clean_sku = str(mapping.sku or '').strip()
        if clean_sku and clean_sku not in mapping_by_sku:
            mapping_by_sku[clean_sku] = mapping
    missing_mappings = [sku for sku in skus if sku not in mapping_by_sku]
    if missing_mappings:
        preview = ', '.join(missing_mappings[:5])
        suffix = '...' if len(missing_mappings) > 5 else ''
        raise ValueError(f'Some suggested SKUs are not mapped to this vendor: {preview}{suffix}')

    try:
        catalog_by_sku = fetch_catalog_by_sku()
    except Exception:
        catalog_by_sku = {}

    active_stores = db.execute(
        select(Store.id)
        .where(Store.active.is_(True))
        .order_by(Store.name.asc(), Store.id.asc())
    ).all()
    stock_up_weeks = max(1, ceil((target_months * Decimal('30')) / Decimal('7')))
    reorder_weeks = max(1, ceil(Decimal(history_lookback_days) / Decimal('7')))
    po = PurchaseOrder(
        vendor_id=vendor_id,
        status=PurchaseOrderStatus.DRAFT,
        reorder_weeks=reorder_weeks,
        stock_up_weeks=stock_up_weeks,
        history_lookback_days=history_lookback_days,
        notes=f'Stock coverage purchase report: target {target_months:g} month(s), top velocity SKUs.',
        created_by_principal_id=created_by_principal_id,
    )
    db.add(po)
    db.flush()

    used_variation_ids: set[str] = set()
    for row in selected_rows:
        store_qty_by_id = {
            int(split.store_id): int(split.recommended_purchase_quantity)
            for split in (row.store_splits or [])
            if split.recommended_purchase_quantity > 0
        }
        qty = sum(store_qty_by_id.values()) if store_qty_by_id else int(row.recommended_purchase_quantity)
        if qty <= 0:
            continue
        mapping = mapping_by_sku[row.sku]
        catalog_meta = catalog_by_sku.get(row.sku)
        variation_id = (
            str(mapping.square_variation_id or '').strip()
            or (str(catalog_meta.variation_id).strip() if catalog_meta else '')
            or f'SKU::{row.sku}'
        )
        if variation_id in used_variation_ids:
            variation_id = f'SKU::{row.sku}'
        if variation_id in used_variation_ids:
            variation_id = f'SKU::{row.sku}::{uuid4().hex[:8]}'
        used_variation_ids.add(variation_id)

        po_line = PurchaseOrderLine(
            purchase_order_id=po.id,
            variation_id=variation_id,
            sku=row.sku,
            gtin=(catalog_meta.gtin if catalog_meta else None) or mapping.gtin,
            item_name=(catalog_meta.item_name if catalog_meta else None) or row.product_name or row.sku,
            variation_name=(catalog_meta.variation_name if catalog_meta else None) or 'Default',
            unit_cost=mapping.unit_cost,
            unit_price=catalog_meta.unit_price if catalog_meta else None,
            suggested_qty=qty,
            ordered_qty=qty,
            received_qty_total=0,
            in_transit_qty=qty,
            confidence_score=Decimal('1.0000'),
            confidence_state=PurchaseOrderConfidenceState.NORMAL,
            par_source=ParLevelSource.DYNAMIC,
            manual_par_level=None,
            suggested_par_level=int(row.target_inventory_quantity),
            removed=False,
        )
        db.add(po_line)
        db.flush()
        for store_row in active_stores:
            store_id = int(store_row.id)
            allocated_qty = store_qty_by_id.get(store_id, 0)
            if not store_qty_by_id and active_stores and store_id == int(active_stores[0].id):
                allocated_qty = qty
            db.add(
                PurchaseOrderStoreAllocation(
                    purchase_order_line_id=po_line.id,
                    store_id=store_id,
                    expected_qty=allocated_qty,
                    allocated_qty=allocated_qty,
                    variance_qty=0,
                )
            )
    db.flush()
    return po


def get_purchase_order_detail(db: Session, *, purchase_order_id: int) -> dict:
    po_row = db.execute(
        select(PurchaseOrder, Vendor.name)
        .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
        .where(PurchaseOrder.id == purchase_order_id)
    ).one_or_none()
    if not po_row:
        raise ValueError('Order not found')
    po, vendor_name = po_row

    all_order_lines = db.execute(
        select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)
    ).scalars().all()
    active_skus_in_order = {
        str(line.sku).strip()
        for line in all_order_lines
        if not bool(line.removed) and str(line.sku or '').strip()
    }
    removed_skus_in_order = {
        str(line.sku).strip()
        for line in all_order_lines
        if bool(line.removed) and str(line.sku or '').strip()
    }

    rows = db.execute(
        select(PurchaseOrderLine)
        .where(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.removed.is_(False),
        )
        .order_by(PurchaseOrderLine.confidence_state.asc(), PurchaseOrderLine.item_name.asc())
    ).scalars().all()

    mapping_rows = db.execute(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id == po.vendor_id,
            VendorSkuConfig.active.is_(True),
        )
        .order_by(VendorSkuConfig.sku.asc())
    ).scalars().all()
    variation_id_by_sku = {
        str(mapping.sku).strip(): str(mapping.square_variation_id).strip()
        for mapping in mapping_rows
        if str(mapping.sku or '').strip() and str(mapping.square_variation_id or '').strip()
    }
    line_variation_id_by_line_id: dict[int, str] = {}
    for row in rows:
        variation_id = str(row.variation_id or '').strip()
        if not variation_id or variation_id.startswith('SKU::'):
            variation_id = variation_id_by_sku.get(str(row.sku or '').strip(), '')
        if variation_id and not variation_id.startswith('SKU::'):
            line_variation_id_by_line_id[int(row.id)] = variation_id
    line_variation_ids = list(line_variation_id_by_line_id.values())

    normal_lines: list[dict] = []
    low_confidence_lines: list[dict] = []
    store_rows = db.execute(
        select(Store.id, Store.name)
        .where(Store.active.is_(True))
        .order_by(Store.name.asc())
    ).all()
    store_columns = [
        {
            'store_id': int(row.id),
            'store_name': row.name,
            'store_label': _store_split_label(row.name),
        }
        for row in store_rows
    ]
    store_ids = [store['store_id'] for store in store_columns]
    on_hand_by_store_variation: dict[tuple[int, str], Decimal] = {}
    if line_variation_ids:
        try:
            on_hand_by_store_variation = fetch_on_hand_by_store_variation(
                db,
                variation_ids=sorted(set(line_variation_ids)),
                store_ids=store_ids,
            )
        except Exception:
            on_hand_by_store_variation = {}

    sales_volume_by_variation: dict[str, Decimal] = {}
    if line_variation_ids:
        try:
            sales_volume_by_variation = fetch_sales_volume_by_variation(
                db,
                variation_ids=sorted(set(line_variation_ids)),
                lookback_days=30,
                store_ids=store_ids,
            )
        except Exception:
            sales_volume_by_variation = {}

    allocations_by_line_id: dict[int, dict[int, dict]] = {}
    line_ids = [row.id for row in rows]
    allocation_rows = []
    if line_ids:
        allocation_rows = db.execute(
            select(
                PurchaseOrderStoreAllocation.purchase_order_line_id,
                PurchaseOrderStoreAllocation.store_id,
                PurchaseOrderStoreAllocation.expected_qty,
                PurchaseOrderStoreAllocation.allocated_qty,
                PurchaseOrderStoreAllocation.manual_par_level,
                PurchaseOrderStoreAllocation.store_received_qty,
                Store.name,
            )
            .join(Store, Store.id == PurchaseOrderStoreAllocation.store_id)
            .where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids))
            .order_by(Store.name.asc())
        ).all()
    for allocation in allocation_rows:
        by_store = allocations_by_line_id.setdefault(allocation.purchase_order_line_id, {})
        by_store[int(allocation.store_id)] = {
            'store_id': int(allocation.store_id),
            'store_name': allocation.name,
            'store_label': _store_split_label(allocation.name),
            'expected_qty': int(allocation.expected_qty),
            'allocated_qty': int(allocation.allocated_qty),
            'manual_par_level': int(allocation.manual_par_level) if allocation.manual_par_level is not None else None,
            'store_received_qty': int(allocation.store_received_qty or 0),
        }

    for row in rows:
        allocation_map = allocations_by_line_id.get(row.id, {})
        store_allocations: list[dict] = []
        resolved_variation_id = line_variation_id_by_line_id.get(int(row.id), str(row.variation_id or ''))
        on_hand_qty_total = Decimal(0)
        on_hand_complete = bool(store_columns)
        for store in store_columns:
            split = allocation_map.get(store['store_id'])
            if split is None:
                split = {
                    'store_id': store['store_id'],
                    'store_name': store['store_name'],
                    'store_label': store['store_label'],
                    'expected_qty': 0,
                    'allocated_qty': 0,
                    'store_received_qty': 0,
                }
            on_hand = on_hand_by_store_variation.get((store['store_id'], resolved_variation_id))
            split['on_hand_qty'] = on_hand
            if on_hand is None:
                on_hand_complete = False
            else:
                on_hand_qty_total += on_hand
            store_allocations.append(split)

        extended_cost = None
        if row.unit_cost is not None:
            try:
                extended_cost = (Decimal(str(row.unit_cost)) * Decimal(int(row.ordered_qty or 0))).quantize(Decimal('0.01'))
            except Exception:
                extended_cost = None

        sales_volume = sales_volume_by_variation.get(resolved_variation_id)
        line = {
            'id': row.id,
            'sku': row.sku or '',
            'item_name': row.item_name,
            'variation_name': row.variation_name,
            'sales_volume': _decimal_to_quantity_text(sales_volume),
            'sales_volume_sort_value': sales_volume if sales_volume is not None else Decimal('-1'),
            'on_hand_qty_total': on_hand_qty_total if on_hand_complete else None,
            'unit_cost': row.unit_cost,
            'unit_price': row.unit_price,
            'suggested_qty': row.suggested_qty,
            'ordered_qty': row.ordered_qty,
            'manual_par_level': row.manual_par_level,
            'suggested_par_level': row.suggested_par_level,
            'par_source': row.par_source.value,
            'confidence_state': row.confidence_state.value,
            'confidence_score': row.confidence_score,
            'removed': row.removed,
            'cost_per_item_text': _decimal_to_money(row.unit_cost),
            'extended_cost_text': _decimal_to_money(extended_cost),
            'received_qty_total': int(row.received_qty_total or 0),
            'received_delta_qty': int(row.received_qty_total or 0) - int(row.ordered_qty or 0),
            'store_allocations': store_allocations,
        }
        if row.confidence_state == PurchaseOrderConfidenceState.LOW:
            low_confidence_lines.append(line)
        else:
            normal_lines.append(line)

    normal_lines.sort(
        key=lambda line: item_variation_sort_key(
            item_name=line.get('item_name'),
            variation_name=line.get('variation_name'),
        )
    )
    low_confidence_lines.sort(
        key=lambda line: item_variation_sort_key(
            item_name=line.get('item_name'),
            variation_name=line.get('variation_name'),
        )
    )

    receive_events = db.execute(
        select(SquareSyncEvent).where(
            SquareSyncEvent.purchase_order_id == po.id,
            SquareSyncEvent.sync_type == 'PURCHASE_ORDER_RECEIVE_ADD_STOCK',
        )
    ).scalars().all()
    latest_receive_event_by_target: dict[tuple[int, int], SquareSyncEvent] = {}
    for event in receive_events:
        line_id = int(event.purchase_order_line_id or 0)
        store_id = int(event.store_id or 0)
        if line_id <= 0 or store_id <= 0:
            continue
        key = (line_id, store_id)
        current = latest_receive_event_by_target.get(key)
        if current is None:
            latest_receive_event_by_target[key] = event
            continue
        current_ts = current.last_attempt_at or current.updated_at or current.created_at
        event_ts = event.last_attempt_at or event.updated_at or event.created_at
        if event_ts >= current_ts:
            latest_receive_event_by_target[key] = event

    failed_receive_rows: list[dict] = []
    receive_success_count = 0
    for (_, store_id), event in latest_receive_event_by_target.items():
        if event.status == SquareSyncStatus.SUCCESS:
            receive_success_count += 1
            continue
        if event.status != SquareSyncStatus.FAILED:
            continue
        payload = event.request_payload or {}
        failed_receive_rows.append(
            {
                'store_id': store_id,
                'store_name': str(payload.get('store_name') or f'Store #{store_id}'),
                'sku': str(payload.get('sku') or ''),
                'item_name': str(payload.get('item_name') or ''),
                'variation_name': str(payload.get('variation_name') or ''),
                'error_text': str(event.error_text or 'Unknown sync error'),
                'last_attempt_at': event.last_attempt_at,
            }
        )
    failed_receive_rows.sort(
        key=lambda row: (
            str(row.get('store_name') or '').lower(),
            *item_variation_sort_key(
                item_name=row.get('item_name'),
                variation_name=row.get('variation_name'),
            ),
        )
    )
    receive_sync_summary = {
        'attempted_targets': len(latest_receive_event_by_target),
        'successful_targets': receive_success_count,
        'failed_targets': len(failed_receive_rows),
        'failed_rows': failed_receive_rows,
    }

    add_item_options: list[dict] = []
    seen_skus: set[str] = set()
    for mapping in mapping_rows:
        sku = str(mapping.sku or '').strip()
        if not sku or sku in seen_skus:
            continue
        seen_skus.add(sku)
        if sku in active_skus_in_order:
            continue
        add_item_options.append(
            {
                'sku': sku,
                'removed_in_order': sku in removed_skus_in_order,
                'unit_cost_text': _decimal_to_money(mapping.unit_cost),
            }
        )

    active_lines = [row for row in rows if not bool(row.removed)]
    ordered_qty_total = sum(max(int(row.ordered_qty or 0), 0) for row in active_lines)
    received_qty_total = sum(max(int(row.received_qty_total or 0), 0) for row in active_lines)
    invoice_amount = _purchase_order_invoice_amount(active_lines)
    invoice_paid_amount = po.invoice_paid_amount
    invoice_difference_amount = None
    if invoice_paid_amount is not None:
        invoice_difference_amount = (Decimal(str(invoice_paid_amount)) - invoice_amount).quantize(Decimal('0.01'))

    return {
        'order': po,
        'vendor_name': vendor_name,
        'store_columns': store_columns,
        'normal_lines': normal_lines,
        'low_confidence_lines': low_confidence_lines,
        'all_lines': normal_lines + low_confidence_lines,
        'add_item_options': add_item_options,
        'receive_sync_summary': receive_sync_summary,
        'invoice': {
            'payment_status': po.invoice_payment_status or INVOICE_PAYMENT_UNPAID,
            'amount': invoice_amount,
            'amount_text': _decimal_to_money(invoice_amount),
            'paid_date': po.invoice_paid_date,
            'paid_amount': invoice_paid_amount,
            'paid_amount_text': _decimal_to_money(invoice_paid_amount),
            'difference_amount': invoice_difference_amount,
            'difference_amount_text': _decimal_to_money(invoice_difference_amount),
            'difference_note': po.invoice_difference_note or '',
        },
        'receiving': {
            'ordered_qty_total': ordered_qty_total,
            'received_qty_total': received_qty_total,
            'difference_qty': received_qty_total - ordered_qty_total,
        },
    }


def add_purchase_order_line_by_sku(
    db: Session,
    *,
    purchase_order_id: int,
    sku: str,
    initial_qty: int,
) -> tuple[PurchaseOrderLine, str]:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status not in {PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.IN_TRANSIT}:
        raise ValueError('Only active orders can be edited')

    clean_sku = str(sku or '').strip()
    if not clean_sku:
        raise ValueError('SKU is required')
    if initial_qty < 0:
        raise ValueError('Initial qty cannot be negative')

    mapping = db.execute(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id == po.vendor_id,
            VendorSkuConfig.sku == clean_sku,
            VendorSkuConfig.active.is_(True),
        )
        .order_by(VendorSkuConfig.is_default_vendor.desc(), VendorSkuConfig.id.asc())
    ).scalars().first()
    if mapping is None:
        raise ValueError('SKU is not mapped to this vendor')

    existing_sku_line = db.execute(
        select(PurchaseOrderLine)
        .where(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.sku == clean_sku,
        )
        .order_by(PurchaseOrderLine.id.asc())
    ).scalars().first()

    active_stores = db.execute(
        select(Store.id)
        .where(Store.active.is_(True))
        .order_by(Store.name.asc(), Store.id.asc())
    ).all()
    first_store_id = int(active_stores[0].id) if active_stores else None

    def _apply_allocations_for_line(line: PurchaseOrderLine, qty: int) -> None:
        allocation_rows = db.execute(
            select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id == line.id)
        ).scalars().all()
        allocation_by_store: dict[int, PurchaseOrderStoreAllocation] = {int(row.store_id): row for row in allocation_rows}
        for store_row in active_stores:
            store_id = int(store_row.id)
            allocation = allocation_by_store.get(store_id)
            if allocation is None:
                allocation = PurchaseOrderStoreAllocation(
                    purchase_order_line_id=line.id,
                    store_id=store_id,
                    expected_qty=0,
                    allocated_qty=0,
                    variance_qty=0,
                )
                db.add(allocation)
                allocation_by_store[store_id] = allocation
            allocation.allocated_qty = qty if (first_store_id is not None and store_id == first_store_id and qty > 0) else 0
            allocation.variance_qty = allocation.allocated_qty - int(allocation.expected_qty or 0)
            allocation.updated_at = _now()

    if existing_sku_line is not None:
        if not existing_sku_line.removed:
            raise ValueError('SKU is already in this order')
        existing_sku_line.removed = False
        existing_sku_line.ordered_qty = initial_qty
        existing_sku_line.in_transit_qty = max(initial_qty - int(existing_sku_line.received_qty_total or 0), 0)
        existing_sku_line.updated_at = _now()
        _apply_allocations_for_line(existing_sku_line, initial_qty)
        po.updated_at = _now()
        db.flush()
        return existing_sku_line, 'restored'

    try:
        catalog_meta = fetch_catalog_by_sku().get(clean_sku)
    except Exception:
        catalog_meta = None
    variation_id = (
        str(mapping.square_variation_id or '').strip()
        or (str(catalog_meta.variation_id).strip() if catalog_meta else '')
        or f'SKU::{clean_sku}'
    )
    variation_conflict = db.execute(
        select(PurchaseOrderLine.id)
        .where(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.variation_id == variation_id,
        )
    ).scalar_one_or_none()
    if variation_conflict is not None:
        variation_id = f'SKU::{clean_sku}'
        fallback_conflict = db.execute(
            select(PurchaseOrderLine.id)
            .where(
                PurchaseOrderLine.purchase_order_id == po.id,
                PurchaseOrderLine.variation_id == variation_id,
            )
        ).scalar_one_or_none()
        if fallback_conflict is not None:
            variation_id = f'SKU::{clean_sku}::{uuid4().hex[:8]}'

    item_name = (catalog_meta.item_name if catalog_meta else None) or clean_sku
    variation_name = (catalog_meta.variation_name if catalog_meta else None) or 'Default'
    unit_price = catalog_meta.unit_price if catalog_meta else None
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        variation_id=variation_id,
        sku=clean_sku,
        gtin=(catalog_meta.gtin if catalog_meta else None) or mapping.gtin,
        item_name=item_name,
        variation_name=variation_name,
        unit_cost=mapping.unit_cost,
        unit_price=unit_price,
        suggested_qty=max(initial_qty, 0),
        ordered_qty=initial_qty,
        received_qty_total=0,
        in_transit_qty=initial_qty,
        confidence_score=Decimal('1.0000'),
        confidence_state=PurchaseOrderConfidenceState.NORMAL,
        par_source=ParLevelSource.DYNAMIC,
        manual_par_level=None,
        suggested_par_level=None,
        removed=False,
    )
    db.add(line)
    db.flush()
    _apply_allocations_for_line(line, initial_qty)
    po.updated_at = _now()
    db.flush()
    return line, 'added'


def refresh_purchase_order_lines_from_catalog(
    db: Session,
    *,
    purchase_order_id: int,
) -> dict[str, int]:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status != PurchaseOrderStatus.DRAFT:
        raise ValueError('Only draft orders can refresh SKU/name data')

    lines = db.execute(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.removed.is_(False),
        )
    ).scalars().all()
    if not lines:
        return {'scanned': 0, 'updated': 0, 'missing': 0}

    by_variation_id, by_sku = fetch_catalog_variation_maps()
    updated = 0
    missing = 0
    for line in lines:
        catalog_meta = None
        line_variation_id = str(line.variation_id or '').strip()
        line_sku = str(line.sku or '').strip()
        if line_variation_id and not line_variation_id.startswith('SKU::'):
            catalog_meta = by_variation_id.get(line_variation_id)
        if catalog_meta is None and line_sku:
            catalog_meta = by_sku.get(line_sku)
        if catalog_meta is None:
            missing += 1
            continue

        changed = False
        next_sku = str(catalog_meta.sku or '').strip() or line_sku
        if next_sku and next_sku != line_sku:
            line.sku = next_sku
            changed = True

        next_item_name = str(catalog_meta.item_name or '').strip() or str(line.item_name or '').strip()
        if next_item_name and next_item_name != str(line.item_name or '').strip():
            line.item_name = next_item_name
            changed = True

        next_variation_name = str(catalog_meta.variation_name or '').strip() or str(line.variation_name or '').strip()
        if next_variation_name and next_variation_name != str(line.variation_name or '').strip():
            line.variation_name = next_variation_name
            changed = True

        next_gtin = str(catalog_meta.gtin or '').strip() or None
        if (line.gtin or None) != next_gtin:
            line.gtin = next_gtin
            changed = True

        next_unit_price = catalog_meta.unit_price
        if next_unit_price is not None and Decimal(str(line.unit_price or 0)) != Decimal(str(next_unit_price)):
            line.unit_price = next_unit_price
            changed = True

        if changed:
            line.updated_at = _now()
            updated += 1

    if updated > 0:
        po.updated_at = _now()
    db.flush()
    return {'scanned': len(lines), 'updated': updated, 'missing': missing}


def save_purchase_order_lines(
    db: Session,
    *,
    purchase_order_id: int,
    ordered_qty_by_line_id: dict[int, int],
    removed_line_ids: set[int],
    manual_par_by_line_id: dict[int, int | None],
    manual_par_by_line_store: dict[tuple[int, int], int | None],
    allocation_qty_by_line_store: dict[tuple[int, int], int],
) -> PurchaseOrder:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    editable_statuses = {
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.IN_TRANSIT,
    }
    if po.status not in editable_statuses:
        raise ValueError('Only active orders can be edited')

    lines = db.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id).execution_options(populate_existing=True)).scalars().all()
    line_ids = [line.id for line in lines]
    allocations_by_line_store: dict[tuple[int, int], PurchaseOrderStoreAllocation] = {}
    if line_ids:
        allocation_rows = db.execute(
            select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids))
        ).scalars().all()
        for allocation in allocation_rows:
            allocations_by_line_store[(int(allocation.purchase_order_line_id), int(allocation.store_id))] = allocation

    has_receipts = bool(db.scalar(select(PurchaseOrderReceipt.id).where(PurchaseOrderReceipt.purchase_order_id==po.id).limit(1)))
    if has_receipts:
        for line in lines:
            supplied={sid:qty for (lid,sid),qty in allocation_qty_by_line_store.items() if lid==line.id}
            original={sid:a.allocated_qty for (lid,sid),a in allocations_by_line_store.items() if lid==line.id}
            if (line.id in removed_line_ids or ordered_qty_by_line_id.get(line.id,line.ordered_qty)!=line.ordered_qty
                    or supplied and supplied!=original):
                raise ValueError('Receiving has started; preserve the original PO quantities and intended allocations')
    for line in lines:
        allocation_override_present = False
        submitted_store_ids_for_line: set[int] = set()
        for (line_id, store_id), qty in allocation_qty_by_line_store.items():
            if line_id != line.id:
                continue
            allocation = allocations_by_line_store.get((line_id, store_id))
            if qty < 0:
                raise ValueError('Store split quantity cannot be negative')
            if allocation is None:
                allocation = PurchaseOrderStoreAllocation(
                    purchase_order_line_id=line_id,
                    store_id=store_id,
                    expected_qty=0,
                    allocated_qty=0,
                    variance_qty=0,
                )
                db.add(allocation)
                allocations_by_line_store[(line_id, store_id)] = allocation
            allocation.allocated_qty = qty
            allocation.variance_qty = qty - allocation.expected_qty
            allocation.updated_at = _now()
            allocation_override_present = True
            submitted_store_ids_for_line.add(int(store_id))

        for (line_id, store_id), manual_par in manual_par_by_line_store.items():
            if line_id != line.id:
                continue
            allocation = allocations_by_line_store.get((line_id, store_id))
            if manual_par is not None and manual_par < 0:
                raise ValueError('Store manual par level cannot be negative')
            if allocation is None:
                allocation = PurchaseOrderStoreAllocation(
                    purchase_order_line_id=line_id,
                    store_id=store_id,
                    expected_qty=0,
                    allocated_qty=0,
                    manual_par_level=None,
                    variance_qty=0,
                )
                db.add(allocation)
                allocations_by_line_store[(line_id, store_id)] = allocation
            allocation.manual_par_level = manual_par
            allocation.updated_at = _now()

        if allocation_override_present:
            for (line_id, store_id), allocation in allocations_by_line_store.items():
                if line_id != line.id:
                    continue
                if int(store_id) in submitted_store_ids_for_line:
                    continue
                allocation.allocated_qty = 0
                allocation.variance_qty = 0 - int(allocation.expected_qty or 0)
                allocation.updated_at = _now()
            allocation_total = sum(
                max(int(a.allocated_qty), 0)
                for (line_id, _), a in allocations_by_line_store.items()
                if line_id == line.id
            )
            line.ordered_qty = allocation_total
            line.in_transit_qty = max(allocation_total - line.received_qty_total, 0)
        store_manual_pars = [
            int(a.manual_par_level)
            for (line_id, _), a in allocations_by_line_store.items()
            if line_id == line.id and a.manual_par_level is not None
        ]
        if store_manual_pars:
            line.manual_par_level = sum(store_manual_pars)
        elif line.id in manual_par_by_line_id:
            line.manual_par_level = manual_par_by_line_id[line.id]
        else:
            line.manual_par_level = None
        # Treat untouched zero-quantity lines as removed, but keep received overage/manual
        # lines visible so scanned or entered quantities persist through ordinary saves.
        line.removed = _should_remove_saved_order_line(
            line_id=int(line.id),
            ordered_qty=int(line.ordered_qty or 0),
            received_qty_total=int(line.received_qty_total or 0),
            removed_line_ids=removed_line_ids,
            currently_removed=bool(line.removed),
        )
        if line.removed:
            # Removal closes ordering commitments; historical receiving remains intact.
            line.ordered_qty = 0
            line.in_transit_qty = 0
            for (line_id, _), allocation in allocations_by_line_store.items():
                if line_id != line.id:
                    continue
                allocation.allocated_qty = 0
                allocation.variance_qty = 0 - int(allocation.expected_qty or 0)
                allocation.updated_at = _now()
        line.updated_at = _now()
    po.updated_at = _now()
    db.flush()
    return po


def save_purchase_order_invoice(
    db: Session,
    *,
    purchase_order_id: int,
    payment_status: str,
    paid_date: str | None,
    paid_amount: str | None,
    difference_note: str | None,
) -> PurchaseOrder:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')

    normalized_status = (payment_status or '').strip().upper()
    if normalized_status not in {INVOICE_PAYMENT_PAID, INVOICE_PAYMENT_UNPAID}:
        raise ValueError('Invoice status must be Paid or Unpaid')

    active_lines = db.execute(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.removed.is_(False),
        )
    ).scalars().all()
    invoice_amount = _purchase_order_invoice_amount(active_lines)
    note = (difference_note or '').strip()

    if normalized_status == INVOICE_PAYMENT_PAID:
        parsed_date = _parse_invoice_date(paid_date)
        parsed_amount = _parse_money(paid_amount, field='paid amount')
        if parsed_date is None:
            raise ValueError('Paid date is required when invoice is Paid')
        if parsed_amount is None:
            raise ValueError('Paid amount is required when invoice is Paid')
        if parsed_amount != invoice_amount and not note:
            raise ValueError('A note is required when the paid amount differs from the order total')
        po.invoice_payment_status = INVOICE_PAYMENT_PAID
        po.invoice_paid_date = parsed_date
        po.invoice_paid_amount = parsed_amount
        po.invoice_difference_note = note or None
    else:
        po.invoice_payment_status = INVOICE_PAYMENT_UNPAID
        po.invoice_paid_date = None
        po.invoice_paid_amount = None
        po.invoice_difference_note = None

    po.updated_at = _now()
    db.flush()
    return po


def _sync_line_received_totals(
    db: Session,
    *,
    line_ids: list[int],
    line_by_id: dict[int, PurchaseOrderLine] | None = None,
) -> None:
    if not line_ids:
        return
    lines = line_by_id or {
        int(line.id): line
        for line in db.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.id.in_(line_ids))
        ).scalars().all()
    }
    allocations = db.execute(
        select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids))
    ).scalars().all()
    totals: dict[int, int] = {line_id: 0 for line_id in line_ids}
    for allocation in allocations:
        line_id = int(allocation.purchase_order_line_id)
        totals[line_id] = totals.get(line_id, 0) + max(int(allocation.store_received_qty or 0), 0)
    for line_id in line_ids:
        line = lines.get(int(line_id))
        if line is None:
            continue
        total = totals.get(int(line_id), 0)
        line.received_qty_total = total
        line.in_transit_qty = max(int(line.ordered_qty or 0) - total, 0)
        line.updated_at = _now()



def _receiving_store_rows(db: Session) -> list[Store]:
    rows = db.execute(
        select(Store).where(Store.active.is_(True))
    ).scalars().all()
    return sorted(rows, key=lambda store: _store_receive_priority_key(store.name))


def _select_next_receiving_store(
    stores: list[Store],
    allocation_by_store_id: dict[int, PurchaseOrderStoreAllocation],
) -> tuple[Store, bool]:
    if not stores:
        raise ValueError('No active stores are available for receiving')
    for store in stores:
        allocation = allocation_by_store_id.get(int(store.id))
        allocated_qty = int(allocation.allocated_qty or 0) if allocation is not None else 0
        received_qty = int(allocation.store_received_qty or 0) if allocation is not None else 0
        if received_qty < allocated_qty:
            return store, False
    for store in stores:
        if _store_receive_priority_key(store.name)[0] == 0:
            return store, True
    return stores[0], True


def _square_receive_quantity_from_singles(received_qty: int, pack_size: int) -> int:
    """Return the individual-unit quantity tracked by the mapped Square variation.

    Vendor pack size controls order rounding and barcode scan increments. It
    must not divide the received quantity: purchase-order allocations and
    Square inventory both track the individual units represented by the
    mapped catalog variation. Short shipments also do not need to be a full
    vendor pack.
    """
    clean_received = max(int(received_qty or 0), 0)
    _ = max(int(pack_size or 1), 1)
    return clean_received


def _should_remove_saved_order_line(
    *,
    line_id: int,
    ordered_qty: int,
    received_qty_total: int,
    removed_line_ids: set[int],
    currently_removed: bool = False,
) -> bool:
    if currently_removed:
        return True
    if line_id in removed_line_ids:
        return True
    return int(ordered_qty or 0) <= 0 and int(received_qty_total or 0) <= 0


def _ensure_allocation(
    db: Session,
    *,
    line_id: int,
    store_id: int,
    allocation_by_store_id: dict[int, PurchaseOrderStoreAllocation],
) -> PurchaseOrderStoreAllocation:
    allocation = allocation_by_store_id.get(store_id)
    if allocation is not None:
        return allocation
    allocation = PurchaseOrderStoreAllocation(
        purchase_order_line_id=line_id,
        store_id=store_id,
        expected_qty=0,
        allocated_qty=0,
        store_received_qty=0,
        variance_qty=0,
    )
    db.add(allocation)
    db.flush()
    allocation_by_store_id[store_id] = allocation
    return allocation


def _create_unexpected_scan_line(
    db: Session,
    *,
    po: PurchaseOrder,
    barcode: str,
) -> PurchaseOrderLine:
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        variation_id=f'SKU::{barcode}::{uuid4().hex[:8]}',
        sku=barcode,
        gtin=barcode if barcode.isdigit() else None,
        item_name='Unexpected Barcode',
        variation_name=barcode,
        unit_cost=None,
        unit_price=None,
        suggested_qty=0,
        ordered_qty=0,
        received_qty_total=0,
        in_transit_qty=0,
        confidence_score=Decimal('1.0000'),
        confidence_state=PurchaseOrderConfidenceState.NORMAL,
        par_source=ParLevelSource.DYNAMIC,
        manual_par_level=None,
        suggested_par_level=None,
        removed=False,
    )
    db.add(line)
    db.flush()
    return line


def _normalize_scan_key(value: object) -> str:
    raw = str(value or '').strip().lower()
    if raw.isdigit():
        return raw.lstrip('0') or '0'
    return raw


def _line_matches_barcode(line: PurchaseOrderLine, barcode_key: str) -> bool:
    candidates = [
        line.sku,
        line.variation_id,
        getattr(line, 'gtin', None),
    ]
    return any(_normalize_scan_key(candidate) == barcode_key for candidate in candidates if str(candidate or '').strip())


def _line_receive_scan_increment(db: Session, *, po: PurchaseOrder, line: PurchaseOrderLine, barcode_key: str = '') -> int:
    sku = str(line.sku or '').strip()
    variation_id = str(line.variation_id or '').strip()
    conditions = []
    if sku:
        conditions.append(VendorSkuConfig.sku == sku)
    if variation_id and not variation_id.startswith('SKU::'):
        conditions.append(VendorSkuConfig.square_variation_id == variation_id)
    if barcode_key:
        conditions.append(VendorSkuConfig.gtin.is_not(None))
    if not conditions:
        return 1
    rows = db.execute(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id == po.vendor_id,
            VendorSkuConfig.active.is_(True),
            or_(*conditions),
        )
        .order_by(VendorSkuConfig.is_default_vendor.desc(), VendorSkuConfig.id.asc())
    ).scalars().all()
    for row in rows:
        if barcode_key and _normalize_scan_key(row.gtin) == barcode_key:
            return max(int(row.pack_size or 1), 1)
    for row in rows:
        if sku and str(row.sku or '').strip() == sku:
            return max(int(row.pack_size or 1), 1)
        if variation_id and str(row.square_variation_id or '').strip() == variation_id:
            return max(int(row.pack_size or 1), 1)
    return 1


def _scan_purchase_order_barcode_target(
    db: Session,
    *,
    purchase_order_id: int,
    barcode: str,
) -> dict:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status not in (PurchaseOrderStatus.IN_TRANSIT, PurchaseOrderStatus.RECEIVED_SPLIT_PENDING):
        raise ValueError('Barcode scanning requires an active in-transit order')

    clean_barcode = (barcode or '').strip()
    if not clean_barcode:
        raise ValueError('Barcode is required')
    barcode_key = _normalize_scan_key(clean_barcode)

    lines = db.execute(
        select(PurchaseOrderLine)
        .where(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.removed.is_(False),
        )
        .order_by(PurchaseOrderLine.id.asc())
    ).scalars().all()
    line_skus = sorted({str(line.sku or '').strip() for line in lines if str(line.sku or '').strip()})
    gtin_by_sku: dict[str, str] = {}
    if line_skus:
        gtin_rows = db.execute(
            select(VendorSkuConfig.sku, VendorSkuConfig.gtin).where(
                VendorSkuConfig.vendor_id == po.vendor_id,
                VendorSkuConfig.sku.in_(line_skus),
                VendorSkuConfig.active.is_(True),
                VendorSkuConfig.gtin.is_not(None),
            )
        ).all()
        gtin_by_sku = {
            str(row.sku).strip(): str(row.gtin).strip()
            for row in gtin_rows
            if str(row.sku or '').strip() and str(row.gtin or '').strip()
        }
        for line in lines:
            if line.gtin:
                continue
            synced_gtin = gtin_by_sku.get(str(line.sku or '').strip())
            if synced_gtin:
                line.gtin = synced_gtin
                line.updated_at = _now()
    matching_lines = [
        line
        for line in lines
        if _line_matches_barcode(line, barcode_key)
    ]
    if not matching_lines:
        barcode_mapping_rows = db.execute(
            select(VendorSkuConfig).where(
                VendorSkuConfig.vendor_id == po.vendor_id,
                VendorSkuConfig.active.is_(True),
                VendorSkuConfig.gtin.is_not(None),
            )
        ).scalars().all()
        barcode_mapping = next(
            (row for row in barcode_mapping_rows if _normalize_scan_key(row.gtin) == barcode_key),
            None,
        )
        if barcode_mapping is not None:
            mapped_sku = str(barcode_mapping.sku or '').strip()
            mapped_variation_id = str(barcode_mapping.square_variation_id or '').strip()
            for line in lines:
                if (
                    (mapped_sku and str(line.sku or '').strip() == mapped_sku)
                    or (mapped_variation_id and str(line.variation_id or '').strip() == mapped_variation_id)
                ):
                    if not line.gtin:
                        line.gtin = str(barcode_mapping.gtin or '').strip() or None
                        line.updated_at = _now()
                    matching_lines = [line]
                    break
    matched_existing_line = bool(matching_lines)

    stores = _receiving_store_rows(db)
    target_line: PurchaseOrderLine | None = None
    target_allocations_by_store: dict[int, PurchaseOrderStoreAllocation] = {}
    for line in matching_lines:
        allocation_rows = db.execute(
            select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id == line.id)
        ).scalars().all()
        by_store = {int(row.store_id): row for row in allocation_rows}
        if int(line.received_qty_total or 0) < int(line.ordered_qty or 0):
            target_line = line
            target_allocations_by_store = by_store
            break
        if target_line is None:
            target_line = line
            target_allocations_by_store = by_store

    if target_line is None:
        target_line = _create_unexpected_scan_line(db, po=po, barcode=clean_barcode)
        target_allocations_by_store = {}
        matched_existing_line = False

    target_store, overage = _select_next_receiving_store(stores, target_allocations_by_store)
    allocation = _ensure_allocation(
        db,
        line_id=int(target_line.id),
        store_id=int(target_store.id),
        allocation_by_store_id=target_allocations_by_store,
    )
    scan_increment_qty = _line_receive_scan_increment(db, po=po, line=target_line, barcode_key=barcode_key)
    allocation.store_received_qty = max(int(allocation.store_received_qty or 0), 0) + scan_increment_qty
    allocation.updated_at = _now()
    _sync_line_received_totals(db, line_ids=[int(target_line.id)], line_by_id={int(target_line.id): target_line})
    po.updated_at = _now()
    db.flush()

    line_received_total = int(target_line.received_qty_total or 0)
    line_ordered_qty = int(target_line.ordered_qty or 0)
    return {
        'purchase_order_id': int(po.id),
        'line_id': int(target_line.id),
        'store_id': int(target_store.id),
        'store_name': target_store.name,
        'store_label': _store_split_label(target_store.name),
        'barcode': clean_barcode,
        'matched_existing_line': matched_existing_line,
        'overage': overage,
        'scan_increment_qty': scan_increment_qty,
        'sku': target_line.sku or clean_barcode,
        'item_name': target_line.item_name,
        'variation_name': target_line.variation_name,
        'store_received_qty': int(allocation.store_received_qty or 0),
        'line_received_qty': line_received_total,
        'line_ordered_qty': line_ordered_qty,
        'line_difference_qty': line_received_total - line_ordered_qty,
    }



def delete_draft_purchase_order(db: Session, *, purchase_order_id: int) -> None:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status not in {PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.IN_TRANSIT}:
        raise ValueError('Only draft or in-transit orders can be discarded')

    line_ids = select(PurchaseOrderLine.id).where(PurchaseOrderLine.purchase_order_id == po.id)
    has_receiving = db.scalar(select(PurchaseOrderReceipt.id).where(PurchaseOrderReceipt.purchase_order_id == po.id).limit(1))
    has_sync = db.scalar(select(SquareSyncEvent.id).where(SquareSyncEvent.purchase_order_id == po.id).limit(1))
    received = db.scalar(select(PurchaseOrderLine.id).where(PurchaseOrderLine.id.in_(line_ids), PurchaseOrderLine.received_qty_total > 0).limit(1))
    allocation_receipts = db.scalar(select(PurchaseOrderStoreAllocation.id).where(
        PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids), PurchaseOrderStoreAllocation.store_received_qty > 0).limit(1))
    if has_receiving or has_sync or received or allocation_receipts:
        raise ValueError('Orders with receiving or Square evidence cannot be discarded; close the remainder instead')

    pdf_path = (po.pdf_path or '').strip()
    if pdf_path:
        candidate = (Path(__file__).resolve().parents[2] / pdf_path).resolve()
        generated_root = (Path(__file__).resolve().parents[2] / 'generated').resolve()
        try:
            if candidate.is_file() and str(candidate).startswith(str(generated_root)):
                candidate.unlink(missing_ok=True)
        except Exception:
            pass
    db.delete(po)
    db.flush()


def _resolve_pdf_template_for_vendor(db: Session, *, vendor_id: int) -> PurchaseOrderPdfTemplate | None:
    vendor_template = db.execute(
        select(PurchaseOrderPdfTemplate).where(
            PurchaseOrderPdfTemplate.vendor_id == vendor_id,
            PurchaseOrderPdfTemplate.active.is_(True),
        )
    ).scalar_one_or_none()
    if vendor_template is not None:
        return vendor_template
    return db.execute(
        select(PurchaseOrderPdfTemplate).where(
            PurchaseOrderPdfTemplate.is_generic.is_(True),
            PurchaseOrderPdfTemplate.active.is_(True),
        )
    ).scalar_one_or_none()


def _wrap_text_to_width(pdf, text: str, *, font_name: str, font_size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or '').splitlines():
        content = paragraph.strip()
        if not content:
            lines.append('')
            continue
        words = content.split()
        current = words[0]
        for word in words[1:]:
            candidate = f'{current} {word}'
            if pdf.stringWidth(candidate, font_name, font_size) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines or ['']


def _generate_purchase_order_pdf(db: Session, *, purchase_order_id: int) -> str:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import LETTER
        from reportlab.pdfgen import canvas
    except Exception as exc:
        raise RuntimeError('PDF generation dependency missing: install reportlab') from exc

    po_row = db.execute(
        select(PurchaseOrder, Vendor.name)
        .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
        .where(PurchaseOrder.id == purchase_order_id)
    ).one_or_none()
    if po_row is None:
        raise ValueError('Order not found')
    po, vendor_name = po_row
    template = _resolve_pdf_template_for_vendor(db, vendor_id=int(po.vendor_id))

    lines = db.execute(
        select(PurchaseOrderLine)
        .where(
            PurchaseOrderLine.purchase_order_id == purchase_order_id,
            PurchaseOrderLine.removed.is_(False),
        )
        .order_by(PurchaseOrderLine.item_name.asc(), PurchaseOrderLine.variation_name.asc())
    ).scalars().all()
    if not lines:
        raise ValueError('Cannot generate PDF for empty order')

    # PDF-only vendor display helper:
    # show each quantity and, when configured, equivalent pack count.
    vendor_pack_rows = db.execute(
        select(
            VendorSkuConfig.sku,
            VendorSkuConfig.square_variation_id,
            VendorSkuConfig.pack_size,
        ).where(VendorSkuConfig.vendor_id == po.vendor_id)
    ).all()
    pack_size_by_sku: dict[str, int] = {}
    pack_size_by_variation_id: dict[str, int] = {}
    for sku_raw, variation_id_raw, pack_size_raw in vendor_pack_rows:
        pack_size = max(int(pack_size_raw or 1), 1)
        sku = str(sku_raw or '').strip()
        variation_id = str(variation_id_raw or '').strip()
        if sku:
            pack_size_by_sku[sku] = pack_size
        if variation_id:
            pack_size_by_variation_id[variation_id] = pack_size

    root = Path(__file__).resolve().parents[2]
    out_dir = root / 'generated' / 'purchase_orders'
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime('%Y%m%d%H%M%S')
    file_name = f'purchase_order_{po.id}_{stamp}.pdf'
    file_path = out_dir / file_name

    page_w, page_h = LETTER
    pdf = canvas.Canvas(str(file_path), pagesize=LETTER)

    company_lines = [
        'Erupted Vapor LTD',
        '6303 NE HWY 99',
        'Vancouver, WA 98665',
        '',
        'Phone: (503) 730 - 4236',
        'Email: Admin@EruptedVapor.com',
    ]
    disclaimer = (template.legal_disclaimer if template else None) or ''
    def cell_lines(value, width, size=8):
        # Preserve complete identifiers and names, including long unbroken SKUs.
        result=[]
        for paragraph in str(value or '').splitlines() or ['']:
            current=''
            for char in paragraph:
                if current and pdf.stringWidth(current+char,'Helvetica',size)>width:
                    result.append(current);current=''
                current+=char
            result.append(current)
        return result

    def cost_text(value):
        raw=f'{value:,.4f}'
        whole,fraction=raw.split('.')
        return '$'+whole+'.'+fraction.rstrip('0').ljust(2,'0')

    rows=[]
    for line in lines:
        qty_each=max(int(line.ordered_qty or 0),0)
        sku=str(line.sku or '').strip()
        pack_size=pack_size_by_variation_id.get(str(line.variation_id or '')) or pack_size_by_sku.get(sku) or 1
        qty_label=str(qty_each)
        if pack_size>1 and qty_each>0:
            qty_label=f'{qty_each} ({int(ceil(qty_each/pack_size))} pk @ {pack_size})'
        unit=Decimal(str(line.unit_cost)) if line.unit_cost is not None else None
        if unit is not None and (not unit.is_finite() or unit<0):unit=None
        amount=(unit*qty_each).quantize(Decimal('0.01')) if unit is not None else None
        columns=[cell_lines(sku or 'Not recorded',66),
            cell_lines(line.item_name,164)+cell_lines(line.variation_name,164),
            cell_lines(qty_label,106),cell_lines(cost_text(unit) if unit is not None else 'Unknown',68),
            cell_lines(f'${amount:,.2f}' if amount is not None else 'Unknown',88)]
        rows.append({'columns':columns,'height':max(26,max(map(len,columns))*10+8),'amount':amount})
    total=sum((row['amount'] for row in rows),Decimal(0)) if all(row['amount'] is not None for row in rows) else None
    disclaimer_lines=_wrap_text_to_width(pdf,disclaimer,font_name='Helvetica',font_size=7,max_width=page_w-84) if disclaimer else []
    footer_bottom=20.0
    disclaimer_height=len(disclaimer_lines)*8.5+6 if disclaimer_lines else 0
    table_bottom=max(68,footer_bottom+disclaimer_height+32)
    vendor_lines=cell_lines(f'Vendor: {vendor_name}',page_w-84,11)
    table_header_y=page_h-188-(len(vendor_lines)-1)*12
    first_row_top=table_header_y-18
    pages=[];page_rows=[];used=0
    for row in rows:
        if page_rows and used+row['height']>first_row_top-table_bottom:
            pages.append(page_rows);page_rows=[];used=0
        page_rows.append(row);used+=row['height']
    pages.append(page_rows)
    for page_idx,page_rows in enumerate(pages):
        pdf.setFont('Helvetica-Bold',10)
        for idx,company_line in enumerate(company_lines):pdf.drawString(42,page_h-48-idx*14,company_line)
        pdf.setFont('Helvetica-Bold',11)
        for idx,vendor_line in enumerate(vendor_lines):pdf.drawString(42,page_h-148-idx*12,vendor_line)
        pdf.drawString(42,table_header_y+24,f'Purchase Order #: {po.id}')
        pdf.setFont('Helvetica-Bold',8)
        pdf.drawString(46,table_header_y,'SKU');pdf.drawString(118,table_header_y,'Item / Variation')
        for right,label in ((398,'QTY'),(474,'Unit Cost'),(570,'Line Total')):pdf.drawRightString(right,table_header_y,label)
        pdf.line(42,table_header_y-6,page_w-42,table_header_y-6)
        top=first_row_top
        for index,row in enumerate(page_rows):
            bottom=top-row['height']
            if index%2:
                pdf.setFillColor(colors.Color(.92,.96,1));pdf.rect(42,bottom,page_w-84,row['height'],fill=1,stroke=0)
            pdf.setStrokeColor(colors.Color(.84,.89,.96));pdf.setLineWidth(.4);pdf.line(42,bottom,page_w-42,bottom)
            pdf.setFillColor(colors.black);pdf.setFont('Helvetica',8)
            for column,values in enumerate(row['columns']):
                for offset,value in enumerate(values):
                    baseline=top-11-offset*10
                    if column<2:pdf.drawString((46,118)[column],baseline,value)
                    else:pdf.drawRightString((398,474,570)[column-2],baseline,value)
            top=bottom
        if page_idx==len(pages)-1:
            pdf.setFont('Helvetica-Bold',10)
            pdf.drawRightString(page_w-42,footer_bottom+disclaimer_height+14,
                f'Estimated total: ${total:,.2f}' if total is not None else 'Estimated total: Unknown (cost not recorded)')
            pdf.setFont('Helvetica',7)
            for index,line in enumerate(disclaimer_lines):pdf.drawString(42,footer_bottom+disclaimer_height-4-index*8.5,line)
        pdf.setFont('Helvetica',7);pdf.drawString(42,12,f'Page {page_idx+1} of {len(pages)}')
        pdf.showPage()

    pdf.save()
    rel_path = str(Path('generated') / 'purchase_orders' / file_name)
    return rel_path


def ensure_current_purchase_order_pdf(db: Session, *, purchase_order_id: int) -> Path:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')

    pdf_path = (po.pdf_path or '').strip()
    abs_path = (Path(__file__).resolve().parents[2] / pdf_path).resolve() if pdf_path else None
    pdf_missing = abs_path is None or not abs_path.exists() or not abs_path.is_file()
    pdf_stale = (
        po.submitted_at is not None
        and po.updated_at is not None
        and po.updated_at > po.submitted_at
        and not db.scalar(select(PurchaseOrderReceipt.id).where(PurchaseOrderReceipt.purchase_order_id == po.id).limit(1))
    )

    if pdf_missing or pdf_stale:
        po.pdf_path = _generate_purchase_order_pdf(db, purchase_order_id=purchase_order_id)
        db.flush()
        abs_path = (Path(__file__).resolve().parents[2] / str(po.pdf_path)).resolve()

    if abs_path is None or not abs_path.exists() or not abs_path.is_file():
        raise ValueError('PDF file not found on server')

    return abs_path


def submit_purchase_order(db: Session, *, purchase_order_id: int, actor_principal_id: int) -> PurchaseOrder:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    if po.status != PurchaseOrderStatus.DRAFT:
        raise ValueError('Only draft orders can be submitted')

    lines = db.execute(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id == purchase_order_id,
            PurchaseOrderLine.removed.is_(False),
        )
    ).scalars().all()
    active_lines = [line for line in lines if int(line.ordered_qty or 0) > 0]
    for line in lines:
        if int(line.ordered_qty or 0) <= 0:
            line.removed = True
            line.in_transit_qty = 0
            line.updated_at = _now()
    if not active_lines:
        raise ValueError('Cannot submit an empty order')

    for line in active_lines:
        line.in_transit_qty = max(line.ordered_qty - line.received_qty_total, 0)

    po.status = PurchaseOrderStatus.IN_TRANSIT
    po.ordered_at = _now()
    po.submitted_at = _now()
    po.submitted_by_principal_id = actor_principal_id
    po.pdf_path = _generate_purchase_order_pdf(db, purchase_order_id=purchase_order_id)
    po.updated_at = _now()
    db.flush()
    # V2 observes the deliberate V1 placed-order lifecycle event. It never
    # writes V1 fields and leaves the order uninitialized when the vendor has
    # no effective financial classification. Consignment orders deliberately
    # remain uninitialized here until their first canonical receipt.
    from app.services.v2_order_payments_service import initialize_new_order_if_configured

    initialize_new_order_if_configured(
        db,
        order=po,
        actor_id=actor_principal_id,
    )
    return po



def parse_generation_form(form) -> tuple[list[int], int, int, int]:
    vendor_ids = [int(value) for value in form.getlist('vendor_ids') if str(value).strip().isdigit()]
    reorder_weeks = _parse_int(str(form.get('reorder_weeks', '5')), field='Reorder weeks', minimum=1)
    stock_up_weeks = _parse_int(str(form.get('stock_up_weeks', '10')), field='Stock-up weeks', minimum=1)
    history_lookback_days = _parse_int(
        str(form.get('history_lookback_days', '120')),
        field='History lookback days',
        minimum=7,
    )
    if stock_up_weeks <= reorder_weeks:
        raise ValueError('Stock-up weeks must be greater than reorder weeks')
    return vendor_ids, reorder_weeks, stock_up_weeks, history_lookback_days


def list_vendor_sku_configs(db: Session, *, vendor_id: int | None = None) -> list[dict]:
    catalog_by_sku = fetch_catalog_by_sku()
    query = (
        select(VendorSkuConfig, Vendor.name)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .order_by(Vendor.name.asc(), VendorSkuConfig.sku.asc())
    )
    if vendor_id is not None:
        query = query.where(VendorSkuConfig.vendor_id == vendor_id)
    rows = db.execute(query).all()
    return [
        {
            'id': cfg.id,
            'vendor_id': cfg.vendor_id,
            'vendor_name': vendor_name,
            'sku': cfg.sku,
            'name': (
                f"{catalog_by_sku[cfg.sku].item_name} - {catalog_by_sku[cfg.sku].variation_name}"
                if cfg.sku in catalog_by_sku
                else '-'
            ),
            'square_variation_id': cfg.square_variation_id,
            'unit_cost': cfg.unit_cost,
            'pack_size': cfg.pack_size,
            'min_order_qty': cfg.min_order_qty,
            'is_default_vendor': cfg.is_default_vendor,
            'active': cfg.active,
        }
        for cfg, vendor_name in rows
    ]


def upsert_vendor_sku_config(
    db: Session,
    *,
    vendor_id: int,
    sku: str,
    square_variation_id: str | None,
    unit_cost: Decimal,
    pack_size: int,
    min_order_qty: int,
    is_default_vendor: bool = True,
    active: bool = True,
) -> VendorSkuConfig:
    clean_sku = sku.strip()
    clean_variation_id = (square_variation_id or '').strip() or None
    if not clean_sku:
        raise ValueError('SKU is required')
    if pack_size < 1:
        raise ValueError('Pack size must be at least 1')
    if min_order_qty < 0:
        raise ValueError('Min order qty cannot be negative')
    if unit_cost < Decimal('0'):
        raise ValueError('Unit cost cannot be negative')

    vendor_exists = db.execute(select(Vendor.id).where(Vendor.id == vendor_id, Vendor.active.is_(True))).scalar_one_or_none()
    if not vendor_exists:
        raise ValueError('Vendor not found')

    existing = db.execute(
        select(VendorSkuConfig).where(
            VendorSkuConfig.vendor_id == vendor_id,
            VendorSkuConfig.sku == clean_sku,
        )
    ).scalar_one_or_none()

    if is_default_vendor and active:
        identity_filters = [VendorSkuConfig.sku == clean_sku]
        if clean_variation_id:
            identity_filters.append(VendorSkuConfig.square_variation_id == clean_variation_id)
        competing_defaults = list(db.scalars(
            select(VendorSkuConfig).where(
                VendorSkuConfig.active.is_(True),
                VendorSkuConfig.is_default_vendor.is_(True),
                or_(*identity_filters),
                *([VendorSkuConfig.id != existing.id] if existing is not None else []),
            )
        ).all())
        for competing in competing_defaults:
            competing.is_default_vendor = False
            competing.updated_at = _now()
        if competing_defaults:
            # The partial unique index is immediate, so persist demotions before
            # promoting the requested relationship for the same SKU.
            db.flush()

    if existing is None:
        existing = VendorSkuConfig(
            vendor_id=vendor_id,
            sku=clean_sku,
            square_variation_id=clean_variation_id,
            unit_cost=unit_cost,
            pack_size=pack_size,
            min_order_qty=min_order_qty,
            is_default_vendor=is_default_vendor,
            active=active,
        )
        db.add(existing)
        db.flush()
        return existing

    existing.square_variation_id = clean_variation_id
    existing.unit_cost = unit_cost
    existing.pack_size = pack_size
    existing.min_order_qty = min_order_qty
    existing.is_default_vendor = is_default_vendor
    existing.active = active
    existing.updated_at = _now()
    db.flush()
    return existing


def import_vendor_sku_configs_csv(db: Session, *, csv_text: str) -> dict:
    reader = csv.DictReader(StringIO(csv_text))
    required = {'vendor_id', 'sku'}
    if not reader.fieldnames or not required.issubset({name.strip() for name in reader.fieldnames}):
        raise ValueError('CSV headers must include vendor_id and sku')

    created_or_updated = 0
    errors: list[str] = []
    for idx, row in enumerate(reader, start=2):
        try:
            vendor_id = int(str(row.get('vendor_id', '')).strip())
            sku = str(row.get('sku', '')).strip()
            square_variation_id = str(row.get('square_variation_id', '')).strip() or None
            unit_cost_raw = str(row.get('unit_cost', '0')).strip() or '0'
            pack_size_raw = str(row.get('pack_size', '1')).strip() or '1'
            min_qty_raw = str(row.get('min_order_qty', '0')).strip() or '0'
            is_default_raw = str(row.get('is_default_vendor', 'true')).strip().lower()
            active_raw = str(row.get('active', 'true')).strip().lower()

            upsert_vendor_sku_config(
                db,
                vendor_id=vendor_id,
                sku=sku,
                square_variation_id=square_variation_id,
                unit_cost=Decimal(unit_cost_raw),
                pack_size=int(pack_size_raw),
                min_order_qty=int(min_qty_raw),
                is_default_vendor=is_default_raw not in {'0', 'false', 'no'},
                active=active_raw not in {'0', 'false', 'no'},
            )
            created_or_updated += 1
        except Exception as exc:
            errors.append(f'Line {idx}: {exc}')
    return {'processed': created_or_updated, 'errors': errors}


def autofill_square_variation_ids(db: Session, *, vendor_id: int | None = None) -> dict:
    catalog = fetch_catalog_by_sku()
    query = select(VendorSkuConfig).where(VendorSkuConfig.active.is_(True))
    if vendor_id is not None:
        query = query.where(VendorSkuConfig.vendor_id == vendor_id)
    rows = db.execute(query).scalars().all()

    updated = 0
    skipped = 0
    for row in rows:
        if row.square_variation_id:
            skipped += 1
            continue
        meta = catalog.get(row.sku)
        if not meta:
            skipped += 1
            continue
        row.square_variation_id = meta.variation_id
        row.updated_at = _now()
        updated += 1
    db.flush()
    return {'updated': updated, 'skipped': skipped}

# Purchasing receiving uses the existing receipt and Square-sync evidence. Neither
# these events nor aggregate receipt totals are a source of current inventory.
_RECEIVING_SYNC_TYPE = 'PURCHASING_RECEIPT_V3'


def authorize_purchasing_owner(db: Session, actor_principal_id: int | None) -> Principal:
    from app.config import settings
    owner_id = settings.purchasing_owner_principal_id
    principal = db.execute(select(Principal).where(Principal.id == actor_principal_id).execution_options(populate_existing=True)).scalar_one_or_none() if actor_principal_id else None
    if not owner_id or not principal or not principal.active or principal.id != owner_id:
        raise PermissionError('Purchasing receiving requires the verified owner')
    return principal


def _receiving_order(db, purchase_order_id, actor_principal_id):
    authorize_purchasing_owner(db, actor_principal_id)
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if po is None:
        raise ValueError('Order not found')
    # Row locking serializes writers, but a reused Session may already hold older
    # allocation objects. Refresh them after obtaining the lock to avoid a lost
    # increment when another transaction committed between read and mutation.
    line_ids = select(PurchaseOrderLine.id).where(PurchaseOrderLine.purchase_order_id == po.id)
    db.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)
        .execution_options(populate_existing=True)).scalars().all()
    db.execute(select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id.in_(line_ids))
        .execution_options(populate_existing=True)).scalars().all()
    return po


def _receipt_request(db, po, *, kind, request_key, payload, actor_principal_id, notes=None):
    if not request_key or len(request_key) > 128:
        raise ValueError('A receiving request identity is required')
    payload = {'purchase_order_id': po.id, 'kind': kind, **payload}
    existing = db.execute(select(PurchaseOrderReceipt).where(PurchaseOrderReceipt.request_key == request_key)).scalar_one_or_none()
    if existing:
        if 'po_outstanding_after' in (existing.request_payload or {}):
            payload['po_outstanding_after']=existing.request_payload['po_outstanding_after']
        if existing.request_payload != payload or existing.received_by_principal_id != actor_principal_id:
            raise ValueError('Receiving request identity was already used for different input')
        return existing, False
    if kind in ('CLOSURE','CORRECTION'):
        # Capture the PO-wide balance at the event, not a later reconstruction.
        line_id=payload.get('line');delta=0;closed_delta=payload.get('quantity',0) if kind=='CLOSURE' else 0
        if kind=='CORRECTION':
            original=db.get(PurchaseOrderReceiptAllocation,payload['original'])
            line_id=original.purchase_order_line_id
            earlier=int(db.scalar(select(func.coalesce(func.sum(PurchaseOrderReceiptAllocation.quantity_delta),0)).where(
                PurchaseOrderReceiptAllocation.original_allocation_id==original.id)) or 0)
            delta=payload['corrected']-original.quantity_delta-earlier
        lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id,PurchaseOrderLine.removed.is_(False))).all()
        payload['po_outstanding_after']=sum(max(0,int(line.ordered_qty or 0)-int(line.received_qty_total or 0)
            -_receiving_closed(db,line.id)-(delta+closed_delta if line.id==line_id else 0)) for line in lines)
    receipt = PurchaseOrderReceipt(purchase_order_id=po.id, status=PurchaseOrderReceiptStatus.SUBMITTED,
        received_by_principal_id=actor_principal_id, received_at=_now(), event_kind=kind,
        request_key=request_key, request_payload=payload, notes=notes, is_partial=True)
    db.add(receipt)
    db.flush()
    return receipt, True


def _receiving_closed(db, line_id):
    return int(db.scalar(select(func.coalesce(func.sum(PurchaseOrderReceiptAllocation.closed_quantity), 0)).where(
        PurchaseOrderReceiptAllocation.purchase_order_line_id == line_id)) or 0)


def _validate_receipt_delta(db, line, delta):
    if delta > 0 and int(line.received_qty_total or 0) + delta + _receiving_closed(db, line.id) > int(line.ordered_qty or 0):
        raise ValueError('Received quantity exceeds the remaining ordered balance; no over-receiving exception is authorized')


def _receipt_allocation(db, po, receipt, *, line_id, store_id, delta=0, closed=0, corrected=None, original=None):
    line = db.get(PurchaseOrderLine, line_id)
    store = db.get(Store, store_id)
    if not line or line.purchase_order_id != po.id or not store or not store.active:
        raise ValueError('Receiving target does not belong to this order and active store')
    if not line.variation_id or str(line.variation_id).startswith('SKU::'):
        raise ValueError('Resolve the Square variation before saving receiving')
    allocation = db.execute(select(PurchaseOrderStoreAllocation).where(
        PurchaseOrderStoreAllocation.purchase_order_line_id == line_id,
        PurchaseOrderStoreAllocation.store_id == store_id)).scalar_one_or_none()
    if allocation is None:
        raise ValueError('Receiving requires an existing destination allocation')
    _validate_receipt_delta(db, line, delta)
    previous = int(allocation.store_received_qty or 0)
    if previous + delta < 0:
        raise ValueError('Correction cannot make aggregate receiving negative')
    receipt_line = db.execute(select(PurchaseOrderReceiptLine).where(
        PurchaseOrderReceiptLine.receipt_id == receipt.id,
        PurchaseOrderReceiptLine.purchase_order_line_id == line_id)).scalar_one_or_none()
    if receipt_line is None:
        receipt_line = PurchaseOrderReceiptLine(receipt_id=receipt.id, purchase_order_line_id=line_id,
            expected_qty=int(line.ordered_qty or 0), received_qty=corrected if corrected is not None else max(delta, 0),
            difference_qty=delta if corrected is not None else delta-int(line.ordered_qty or 0))
        db.add(receipt_line)
        db.flush()
    else:
        # Populate all destinations before the immutable receipt line is inserted.
        raise ValueError('Duplicate receipt line construction')
    event = PurchaseOrderReceiptAllocation(receipt_line_id=receipt_line.id, purchase_order_line_id=line_id,
        store_id=store_id, square_variation_id=line.variation_id, event_kind=receipt.event_kind,
        quantity_delta=delta, closed_quantity=closed, corrected_quantity=corrected,
        original_allocation_id=original, created_by_principal_id=receipt.received_by_principal_id,
        posting_not_required=receipt.event_kind == 'CLOSURE')
    db.add(event)
    allocation.store_received_qty = previous + delta
    allocation.updated_at = _now()
    db.flush()
    _sync_line_received_totals(db, line_ids=[line_id], line_by_id={line_id: line})
    db.flush()
    return event


def save_purchase_order_received_quantities(db: Session, *, purchase_order_id: int,
        received_qty_by_line_store: dict[tuple[int, int], int], actor_principal_id: int | None = None,
        request_key: str | None = None, submission_intent: bool = False) -> PurchaseOrder:
    po = _receiving_order(db, purchase_order_id, actor_principal_id)
    cells = []
    for (line_id, store_id), qty in sorted(received_qty_by_line_store.items()):
        if type(qty) is not int or qty < 0:
            raise ValueError('Received Today must be a nonnegative integer')
        if qty:
            cells.append([line_id, store_id, qty])
    zeros = [[line_id, store_id, 0] for (line_id, store_id), qty in sorted(received_qty_by_line_store.items()) if qty == 0]
    if not cells and not zeros:
        raise ValueError('Enter a shipment count')
    summary = {}
    if submission_intent:
        previous = db.scalar(select(PurchaseOrderReceipt).where(PurchaseOrderReceipt.request_key==request_key))
        if previous:
            # Reuse the original snapshot on retries; never recompute it from today's balance.
            if 'po_outstanding_after' in (previous.request_payload or {}):
                summary['po_outstanding_after'] = previous.request_payload['po_outstanding_after']
        else:
            new_by_line = {}
            for line_id, _, qty in cells:new_by_line[line_id]=new_by_line.get(line_id,0)+qty
            order_lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id,
                PurchaseOrderLine.removed.is_(False))).all()
            summary['po_outstanding_after']=sum(max(0,int(line.ordered_qty or 0)-int(line.received_qty_total or 0)
                -_receiving_closed(db,line.id)-new_by_line.get(line.id,0)) for line in order_lines)
    receipt, fresh = _receipt_request(db, po, kind='DELIVERY', request_key=request_key,
        payload={'cells': cells, **({'zero_cells': zeros} if zeros else {}), **({'store_submission':True} if submission_intent else {}), **summary}, actor_principal_id=actor_principal_id)
    if not fresh:
        return po
    if po.status not in (PurchaseOrderStatus.IN_TRANSIT, PurchaseOrderStatus.RECEIVED_SPLIT_PENDING, PurchaseOrderStatus.COMPLETED, PurchaseOrderStatus.SENT_TO_STORES):
        raise ValueError('Deliveries require a placed order')
    for line_id, store_id, _ in zeros:
        line = db.get(PurchaseOrderLine, line_id)
        allocation = db.scalar(select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id == line_id, PurchaseOrderStoreAllocation.store_id == store_id))
        if not line or line.purchase_order_id != po.id or line.removed or not allocation:
            raise ValueError('Invalid receiving line or destination')
    # Each line is inserted once; all destination children share that immutable line.
    by_line = {}
    for line_id, store_id, qty in cells:
        by_line.setdefault(line_id, []).append((store_id, qty))
    for line_id, stores in by_line.items():
        line = db.get(PurchaseOrderLine, line_id)
        if not line or line.purchase_order_id != po.id or line.removed:
            raise ValueError('Invalid receiving line')
        _validate_receipt_delta(db, line, sum(q for _,q in stores))
        rl = PurchaseOrderReceiptLine(receipt_id=receipt.id, purchase_order_line_id=line_id,
            expected_qty=int(line.ordered_qty or 0), received_qty=sum(q for _,q in stores),
            difference_qty=sum(q for _,q in stores)-int(line.ordered_qty or 0))
        db.add(rl)
        db.flush()
        for store_id, qty in stores:
            _append_delivery_destination(db, po, receipt, rl, line, store_id, qty)
        db.flush()
        _sync_line_received_totals(db, line_ids=[line_id], line_by_id={line_id: line})
    _update_receiving_status(db, po)
    po.updated_at = _now()
    db.flush()
    return po


def _append_delivery_destination(db, po, receipt, receipt_line, line, store_id, qty):
    store = db.get(Store, store_id)
    if not store or not store.active or not line.variation_id or str(line.variation_id).startswith('SKU::'):
        raise ValueError('Receiving requires an active store and resolved Square variation')
    allocation = db.execute(select(PurchaseOrderStoreAllocation).where(
        PurchaseOrderStoreAllocation.purchase_order_line_id == line.id,
        PurchaseOrderStoreAllocation.store_id == store_id)).scalar_one_or_none()
    if allocation is None:
        raise ValueError('Receiving requires an existing destination allocation')
    allocation.store_received_qty = int(allocation.store_received_qty or 0) + qty
    allocation.updated_at = _now()
    db.add(PurchaseOrderReceiptAllocation(receipt_line_id=receipt_line.id, purchase_order_line_id=line.id,
        store_id=store_id, square_variation_id=line.variation_id, event_kind='DELIVERY', quantity_delta=qty,
        closed_quantity=0, created_by_principal_id=receipt.received_by_principal_id, posting_not_required=False))


def correct_purchase_order_receiving(db, *, purchase_order_id, original_allocation_id,
        corrected_quantity, actor_principal_id=None, request_key=None):
    po = _receiving_order(db, purchase_order_id, actor_principal_id)
    if type(corrected_quantity) is not int or corrected_quantity < 0:
        raise ValueError('Corrected quantity must be a nonnegative integer')
    root = db.get(PurchaseOrderReceiptAllocation, original_allocation_id)
    if not root or root.event_kind != 'DELIVERY':
        raise ValueError('Original delivery not found')
    line = db.get(PurchaseOrderLine, root.purchase_order_line_id)
    if line.purchase_order_id != po.id:
        raise ValueError('Delivery belongs to another order')
    receipt, fresh = _receipt_request(db, po, kind='CORRECTION', request_key=request_key,
        payload={'original': root.id, 'corrected': corrected_quantity}, actor_principal_id=actor_principal_id)
    if not fresh:
        return receipt
    differences = db.execute(select(func.coalesce(func.sum(PurchaseOrderReceiptAllocation.quantity_delta), 0)).where(
        PurchaseOrderReceiptAllocation.original_allocation_id == root.id)).scalar_one()
    delta = corrected_quantity - root.quantity_delta - differences
    _receipt_allocation(db, po, receipt, line_id=line.id, store_id=root.store_id,
        delta=delta, corrected=corrected_quantity, original=root.id)
    _update_receiving_status(db, po)
    return receipt


def close_purchase_order_remainder(db, *, purchase_order_id, line_id, store_id, quantity,
        actor_principal_id=None, request_key=None, reason=None):
    po = _receiving_order(db, purchase_order_id, actor_principal_id)
    if type(quantity) is not int or quantity <= 0:
        raise ValueError('Closed quantity must be a positive integer')
    receipt, fresh = _receipt_request(db, po, kind='CLOSURE', request_key=request_key,
        payload={'line': line_id, 'store': store_id, 'quantity': quantity, 'reason': reason or ''},
        actor_principal_id=actor_principal_id, notes=reason or None)
    if not fresh:
        return receipt
    if po.status not in (PurchaseOrderStatus.IN_TRANSIT, PurchaseOrderStatus.RECEIVED_SPLIT_PENDING):
        raise ValueError('Only active commitments can be closed')
    allocation = db.execute(select(PurchaseOrderStoreAllocation).where(
        PurchaseOrderStoreAllocation.purchase_order_line_id == line_id,
        PurchaseOrderStoreAllocation.store_id == store_id)).scalar_one_or_none()
    closed = db.execute(select(func.coalesce(func.sum(PurchaseOrderReceiptAllocation.closed_quantity),0)).where(
        PurchaseOrderReceiptAllocation.purchase_order_line_id == line_id,
        PurchaseOrderReceiptAllocation.store_id == store_id)).scalar_one()
    if not allocation or quantity > max(0, int(allocation.allocated_qty or 0)-int(allocation.store_received_qty or 0)-closed):
        raise ValueError('Closed quantity exceeds outstanding commitment')
    if quantity > max(0, int(db.get(PurchaseOrderLine,line_id).ordered_qty or 0)-int(db.get(PurchaseOrderLine,line_id).received_qty_total or 0)-_receiving_closed(db,line_id)):
        raise ValueError('Closed quantity exceeds the remaining ordered balance')
    _receipt_allocation(db, po, receipt, line_id=line_id, store_id=store_id, closed=quantity)
    _update_receiving_status(db, po)
    return receipt


def scan_purchase_order_barcode(db, *, purchase_order_id, barcode, actor_principal_id=None, request_key=None):
    po = _receiving_order(db, purchase_order_id, actor_principal_id)
    existing = db.execute(select(PurchaseOrderReceipt).where(PurchaseOrderReceipt.request_key == request_key)).scalar_one_or_none() if request_key else None
    if existing:
        if existing.purchase_order_id != po.id or existing.received_by_principal_id != actor_principal_id or (existing.request_payload or {}).get('barcode') != barcode:
            raise ValueError('Barcode request identity conflicts')
        child = db.execute(select(PurchaseOrderReceiptAllocation).join(PurchaseOrderReceiptLine,
            PurchaseOrderReceiptLine.id == PurchaseOrderReceiptAllocation.receipt_line_id).where(
            PurchaseOrderReceiptLine.receipt_id == existing.id)).scalar_one()
        return {'line_id': child.purchase_order_line_id, 'store_id': child.store_id,
            'receipt_allocation_id': child.id, 'scan_increment_qty': child.quantity_delta, 'already_saved': True}
    receipt, _ = _receipt_request(db, po, kind='DELIVERY', request_key=request_key,
        payload={'barcode': barcode}, actor_principal_id=actor_principal_id)
    result = _scan_purchase_order_barcode_target(db, purchase_order_id=po.id, barcode=barcode)
    # The existing resolver increments aggregates; retain that increment and add evidence.
    line = db.get(PurchaseOrderLine, result['line_id'])
    if int(line.received_qty_total or 0) + _receiving_closed(db,line.id) > int(line.ordered_qty or 0):
        raise ValueError('Barcode quantity exceeds the remaining ordered balance')
    if not line.variation_id or str(line.variation_id).startswith('SKU::'):
        catalog, by_sku = fetch_catalog_variation_maps()
        candidates = {meta.variation_id: meta for meta in catalog.values()
            if _normalize_scan_key(meta.sku) == _normalize_scan_key(barcode)
            or (meta.gtin and _normalize_scan_key(meta.gtin) == _normalize_scan_key(barcode))}
        if len(candidates) != 1:
            raise ValueError('Barcode identity is unresolved or ambiguous; no delivery was saved. Resolve the Square variation first.')
        meta = next(iter(candidates.values()))
        line.variation_id = meta.variation_id
        line.sku = meta.sku or None
        line.item_name, line.variation_name = meta.item_name, meta.variation_name
    rl = PurchaseOrderReceiptLine(receipt_id=receipt.id, purchase_order_line_id=line.id,
        expected_qty=line.ordered_qty, received_qty=result['scan_increment_qty'],
        difference_qty=result['scan_increment_qty']-line.ordered_qty)
    db.add(rl)
    db.flush()
    child = PurchaseOrderReceiptAllocation(receipt_line_id=rl.id, purchase_order_line_id=line.id,
        store_id=result['store_id'], square_variation_id=line.variation_id, event_kind='DELIVERY',
        quantity_delta=result['scan_increment_qty'], closed_quantity=0, created_by_principal_id=actor_principal_id)
    db.add(child)
    db.flush()
    return {**result, 'receipt_allocation_id': child.id}


def cancel_purchase_order_barcode_scan(db, *, purchase_order_id, line_id, store_id,
        original_allocation_id=None, actor_principal_id=None, request_key=None):
    _receiving_order(db, purchase_order_id, actor_principal_id)
    root = db.get(PurchaseOrderReceiptAllocation, original_allocation_id) if original_allocation_id else None
    if not root or root.purchase_order_line_id != line_id or root.store_id != store_id:
        raise ValueError('Undo requires the saved delivery reference')
    correct_purchase_order_receiving(db, purchase_order_id=purchase_order_id,
        original_allocation_id=root.id, corrected_quantity=0,
        actor_principal_id=actor_principal_id, request_key=request_key)
    allocation = db.execute(select(PurchaseOrderStoreAllocation).where(
        PurchaseOrderStoreAllocation.purchase_order_line_id == line_id,
        PurchaseOrderStoreAllocation.store_id == store_id)).scalar_one()
    line = db.get(PurchaseOrderLine, line_id)
    return {'line_id': line_id, 'store_id': store_id, 'store_received_qty': allocation.store_received_qty,
        'line_received_qty': line.received_qty_total, 'removed_line': False}


def receiving_history(db, purchase_order_id):
    rows = db.execute(select(PurchaseOrderReceiptAllocation, PurchaseOrderReceipt, SquareSyncEvent).join(
        PurchaseOrderReceiptLine, PurchaseOrderReceiptLine.id == PurchaseOrderReceiptAllocation.receipt_line_id).join(
        PurchaseOrderReceipt, PurchaseOrderReceipt.id == PurchaseOrderReceiptLine.receipt_id).outerjoin(
        SquareSyncEvent, SquareSyncEvent.id == PurchaseOrderReceiptAllocation.square_sync_event_id).where(
        PurchaseOrderReceipt.purchase_order_id == purchase_order_id).order_by(PurchaseOrderReceiptAllocation.id)).all()
    result = []
    for child, receipt, operation in rows:
        state = 'Saved, not pushed'
        if child.posting_not_required:
            state = 'No adjustment required'
        elif operation:
            state = ('Successfully pushed' if operation.status == SquareSyncStatus.SUCCESS else
                'Failed' if operation.status == SquareSyncStatus.FAILED else
                'Outcome uncertain' if operation.attempt_count else 'Saved, not pushed')
        result.append({'event': child, 'receipt': receipt, 'operation': operation, 'posting_state': state})
    return result


def _prepare_receiving_operations(db, po, receipt_id=None):
    children = db.execute(select(PurchaseOrderReceiptAllocation).join(PurchaseOrderLine,
        PurchaseOrderLine.id == PurchaseOrderReceiptAllocation.purchase_order_line_id).where(
        PurchaseOrderLine.purchase_order_id == po.id).order_by(PurchaseOrderReceiptAllocation.id)).scalars().all()
    target_roots = None
    if receipt_id is not None:
        receipt_lines=set(db.scalars(select(PurchaseOrderReceiptLine.id).where(PurchaseOrderReceiptLine.receipt_id==receipt_id)))
        target_roots={row.id if row.event_kind=='DELIVERY' else row.original_allocation_id for row in children if row.receipt_line_id in receipt_lines}
    roots = {row.id:row for row in children if row.event_kind=='DELIVERY' and (target_roots is None or row.id in target_roots)}
    for root in roots.values():
        related = [row for row in children if row.id == root.id or row.original_allocation_id == root.id]
        pending = [row for row in related if row.square_sync_event_id is None and not row.posting_not_required]
        if not pending:
            continue
        delta = sum(row.quantity_delta for row in pending)
        if delta == 0:
            for row in pending:
                row.posting_not_required = True
            continue
        store = db.get(Store, root.store_id)
        if not store or not store.square_location_id:
            raise ValueError('Receiving destination has no Square location')
        key = str(uuid4())
        payload = {'idempotency_key': key, 'changes': [{'type': 'ADJUSTMENT', 'adjustment': {
            'catalog_object_id': root.square_variation_id, 'location_id': store.square_location_id,
            'from_state': 'NONE' if delta > 0 else 'IN_STOCK', 'to_state': 'IN_STOCK' if delta > 0 else 'NONE',
            'quantity': str(abs(delta)), 'occurred_at': _to_iso(_now())}}]}
        operation = SquareSyncEvent(purchase_order_id=po.id, purchase_order_line_id=root.purchase_order_line_id,
            store_id=root.store_id, sync_type=_RECEIVING_SYNC_TYPE, idempotency_key=key,
            request_payload=payload, status=SquareSyncStatus.PENDING, attempt_count=0)
        db.add(operation)
        db.flush()
        for row in pending:
            row.square_sync_event_id = operation.id
    db.flush()


def receive_purchase_order(db, *, purchase_order_id, actor_principal_id, retry_failed_only=False, receipt_id=None):
    """Commit exact commands before HTTP, serialized by a PO-scoped session lock.

    A lost reply remains pending/uncertain. Recovery sends the same request key,
    quantity and timestamp. No legacy aggregate is converted into a command.
    """
    authorize_purchasing_owner(db, actor_principal_id)
    if db.get_bind().dialect.name != 'postgresql':
        raise RuntimeError('Durable purchasing posting requires PostgreSQL')
    engine = db.get_bind()
    with engine.connect() as lock_connection:
        lock_key = f'purchasing-receiving:{purchase_order_id}'
        locked = lock_connection.execute(text('SELECT pg_try_advisory_lock(hashtextextended(:key, 0))'), {'key': lock_key}).scalar_one()
        lock_connection.commit()
        if not locked:
            raise ValueError('A Square push is already running for this order')
        try:
            po = _receiving_order(db, purchase_order_id, actor_principal_id)
            if receipt_id is not None:
                receipt = db.get(PurchaseOrderReceipt,receipt_id)
                if not receipt or receipt.purchase_order_id != po.id:
                    raise ValueError('Shipment does not belong to this PO')
                if not _has_store_submission_intent(db,receipt):
                    raise ValueError('Historical saved counts have no store-submission intent and cannot be replayed')
            if not retry_failed_only:
                _prepare_receiving_operations(db, po, receipt_id=receipt_id)
            db.commit()  # Exact outbound payload and event association are durable before send.
            operations = db.execute(select(SquareSyncEvent).where(SquareSyncEvent.purchase_order_id == purchase_order_id,
                SquareSyncEvent.sync_type == _RECEIVING_SYNC_TYPE).order_by(SquareSyncEvent.id)).scalars().all()
            if receipt_id is not None:
                ids=set(db.scalars(select(PurchaseOrderReceiptAllocation.square_sync_event_id).join(PurchaseOrderReceiptLine, PurchaseOrderReceiptLine.id == PurchaseOrderReceiptAllocation.receipt_line_id).where(PurchaseOrderReceiptLine.receipt_id == receipt_id)))
                operations=[op for op in operations if op.id in ids]
            result = dict(attempted=0, succeeded=0, failed=0, uncertain=0, skipped_already_synced=0,
                skipped_not_failed=0, store_count=len({op.store_id for op in operations}),
                line_count=len({op.purchase_order_line_id for op in operations}), failed_rows=[])
            blocked_pairs = set()
            for op in operations:
                if op.status == SquareSyncStatus.SUCCESS:
                    result['skipped_already_synced'] += 1
                    continue
                pair = (op.purchase_order_line_id, op.store_id)
                if pair in blocked_pairs:
                    continue
                authorize_purchasing_owner(db, actor_principal_id)
                op.attempt_count += 1
                op.last_attempt_at = _now()
                op.status = SquareSyncStatus.PENDING
                op.error_text = 'OUTCOME_UNCERTAIN: transmission started; acknowledgement pending'
                db.commit()
                result['attempted'] += 1
                try:
                    response = _square_post('/v2/inventory/changes/batch-create', op.request_payload,
                        inventory_quantity_write=True)
                    if response.get('errors'):
                        raise ValueError(str(response['errors']))
                except Exception as exc:
                    # HTTP 4xx / explicit API errors establish rejection; server/network
                    # errors may have occurred after acceptance and remain uncertain.
                    definitive = isinstance(exc, ValueError) or str(exc).startswith('Square API returned errors:') or any(
                        str(exc).startswith(f'Square API error {code}:') for code in (400,401,403,404,409,422))
                    op.status = SquareSyncStatus.FAILED if definitive else SquareSyncStatus.PENDING
                    op.error_text = str(exc) if definitive else f'OUTCOME_UNCERTAIN: {exc}'
                    result['failed' if definitive else 'uncertain'] += 1
                    blocked_pairs.add(pair)
                else:
                    op.status = SquareSyncStatus.SUCCESS
                    op.response_payload = response
                    op.error_text = None
                    result['succeeded'] += 1
                db.commit()
            if operations and not result['failed'] and not result['uncertain']:
                # Preserve the existing receipt lifecycle integration. Its
                # classification/actual-balance boundaries remain authoritative.
                from app.services.v2_order_payments_service import initialize_new_order_if_configured
                initialize_new_order_if_configured(db, order=db.get(PurchaseOrder, purchase_order_id), actor_id=actor_principal_id)
                db.commit()
            _update_receiving_status(db, db.get(PurchaseOrder,purchase_order_id))
            db.commit()
            return result
        finally:
            lock_connection.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key, 0))'), {'key': lock_key})
            lock_connection.commit()


def receiving_workspace(db, *, purchase_order_id, actor_principal_id):
    """Presentation of the existing PO, intended splits and immutable receipt events."""
    authorize_purchasing_owner(db, actor_principal_id)
    po = db.get(PurchaseOrder, purchase_order_id)
    if not po:
        raise ValueError('Order not found')
    lines = db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id,
        PurchaseOrderLine.removed.is_(False)).order_by(PurchaseOrderLine.item_name, PurchaseOrderLine.id)).all()
    allocations = db.scalars(select(PurchaseOrderStoreAllocation).where(
        PurchaseOrderStoreAllocation.purchase_order_line_id.in_([line.id for line in lines]))).all()
    by_line = {}
    store_ids = set()
    for allocation in allocations:
        by_line.setdefault(allocation.purchase_order_line_id, {})[str(allocation.store_id)] = allocation
        store_ids.add(allocation.store_id)
    stores = db.scalars(select(Store).where(Store.id.in_(store_ids)).order_by(Store.id)).all()
    rows = []
    for line in lines:
        closed = _receiving_closed(db, line.id)
        ordered, received = int(line.ordered_qty or 0), int(line.received_qty_total or 0)
        rows.append({'id':line.id,'name':line.item_name,'sku':line.sku,'variant':line.variation_name,
            'ordered':ordered,'previous':received,'missing':max(0,ordered-received),
            'closed':closed,'outstanding':max(0,ordered-received-closed),
            'stores':{sid:{'intended':int(a.allocated_qty or 0),'previous':int(a.store_received_qty or 0)}
                for sid,a in by_line.get(line.id,{}).items()}})
    ordered=sum(row['ordered'] for row in rows);received=sum(row['previous'] for row in rows)
    closed=sum(row['closed'] for row in rows);outstanding=sum(row['outstanding'] for row in rows)
    state=('Closed with Shortage' if not outstanding and closed else 'Fully Received' if ordered and not outstanding
        else 'Partially Received' if received else 'Awaiting Receipt')
    history=[]
    receipts=db.scalars(select(PurchaseOrderReceipt).where(PurchaseOrderReceipt.purchase_order_id == po.id)
        .order_by(PurchaseOrderReceipt.id.desc())).all()
    for receipt in receipts:
        events=db.execute(select(PurchaseOrderReceiptAllocation,PurchaseOrderReceiptLine).join(PurchaseOrderReceiptLine,
            PurchaseOrderReceiptLine.id == PurchaseOrderReceiptAllocation.receipt_line_id).where(
            PurchaseOrderReceiptLine.receipt_id == receipt.id)).all()
        children=[]
        for event,rl in events:
            op=db.get(SquareSyncEvent,event.square_sync_event_id) if event.square_sync_event_id else None
            posting=('No stock change' if event.posting_not_required else 'Submitted to stores' if op and op.status==SquareSyncStatus.SUCCESS
                else 'Failed — retry available' if op and op.status==SquareSyncStatus.FAILED else 'Uncertain — recover same request' if op and op.attempt_count else 'Not posted')
            children.append({'id':event.id,'line_id':event.purchase_order_line_id,'store_id':event.store_id,
                'quantity':event.quantity_delta,'closed':event.closed_quantity,'kind':event.event_kind,
                'posting':posting,'error':op.error_text if op else None})
        modern=receipt.event_kind is not None
        recoverable=receipt.event_kind in ('DELIVERY','CORRECTION') and _has_store_submission_intent(db,receipt) and any(child['posting'] not in ('Submitted to stores','No stock change') for child in children)
        snapshot=(receipt.request_payload or {}).get('po_outstanding_after')
        status=('Update failed' if any('Failed' in child['posting'] for child in children)
            else 'Recovery needed' if any('Uncertain' in child['posting'] for child in children)
            else 'Not posted' if any(child['posting']=='Not posted' for child in children)
            else 'Closed with shortage' if receipt.event_kind=='CLOSURE'
            else 'Complete' if snapshot==0 else 'Submitted' if modern else 'Historical')
        from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE
        event_at=receipt.received_at or receipt.created_at
        event_received=sum(child['quantity'] for child in children) if modern else int(db.scalar(
            select(func.coalesce(func.sum(PurchaseOrderReceiptLine.received_qty),0)).where(PurchaseOrderReceiptLine.receipt_id==receipt.id)) or 0)
        history.append({'id':receipt.id,'kind':receipt.event_kind or 'Legacy history — never replayed',
            'at':event_at.isoformat(),'date':event_at.astimezone(BUSINESS_TIMEZONE).strftime('%b %d, %Y %I:%M %p'),
            'received':event_received,'outstanding_after':snapshot,'status':status,'actor_id':receipt.received_by_principal_id,
            'children':children,'retry':recoverable,'request_key':receipt.request_key if modern else None,
            'zero_cells':(receipt.request_payload or {}).get('zero_cells',[])})
    return {'po_id':po.id,'vendor':db.get(Vendor,po.vendor_id).name,'po_status':po.status.value,
        'status':state,'ordered':ordered,'previous':received,'closed':closed,'outstanding':outstanding,
        'can_receive':bool(outstanding) and po.status in (PurchaseOrderStatus.IN_TRANSIT,PurchaseOrderStatus.RECEIVED_SPLIT_PENDING,PurchaseOrderStatus.COMPLETED,PurchaseOrderStatus.SENT_TO_STORES),
        'stores':[{'id':str(store.id),'name':store.name,'active':store.active} for store in stores],
        'rows':rows,'history':history,'owner_id':actor_principal_id}


def _update_receiving_status(db, po):
    """Reuse existing lifecycle values; user-facing receipt state is derived from evidence."""
    db.flush()
    lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id,
        PurchaseOrderLine.removed.is_(False))).all()
    if not lines:
        return
    outstanding=sum(max(0,int(line.ordered_qty or 0)-int(line.received_qty_total or 0)-_receiving_closed(db,line.id)) for line in lines)
    pending=db.scalar(select(PurchaseOrderReceiptAllocation.id).outerjoin(SquareSyncEvent,
        SquareSyncEvent.id==PurchaseOrderReceiptAllocation.square_sync_event_id).where(
        PurchaseOrderReceiptAllocation.purchase_order_line_id.in_([line.id for line in lines]),
        PurchaseOrderReceiptAllocation.posting_not_required.is_(False),
        or_(SquareSyncEvent.id.is_(None),SquareSyncEvent.status!=SquareSyncStatus.SUCCESS)).limit(1))
    po.status=PurchaseOrderStatus.IN_TRANSIT if outstanding else PurchaseOrderStatus.RECEIVED_SPLIT_PENDING if pending else PurchaseOrderStatus.COMPLETED
    po.updated_at=_now()


def submit_receiving_shipment(db, *, purchase_order_id, received_qty_by_line_store, actor_principal_id, request_key):
    """One owner action: durable delivery, followed by only that delivery's existing Square commands."""
    po=save_purchase_order_received_quantities(db,purchase_order_id=purchase_order_id,
        received_qty_by_line_store=received_qty_by_line_store,actor_principal_id=actor_principal_id,request_key=request_key,submission_intent=True)
    receipt=db.scalar(select(PurchaseOrderReceipt).where(PurchaseOrderReceipt.request_key==request_key))
    receipt_id=receipt.id
    db.commit()  # A crash after this point cannot lose or recount the shipment.
    try:
        result=receive_purchase_order(db,purchase_order_id=po.id,actor_principal_id=actor_principal_id,receipt_id=receipt_id)
    except Exception:
        db.rollback()  # The durable receipt remains visible and recoverable by its original identity.
        raise
    return {'receipt_id':receipt_id,**result}


def close_receiving_shortages(db, *, purchase_order_id, actor_principal_id, request_key):
    po=_receiving_order(db,purchase_order_id,actor_principal_id)
    # Repeating the same closure action never closes a second set of quantities.
    if db.scalar(select(PurchaseOrderReceipt.id).where(PurchaseOrderReceipt.purchase_order_id==po.id,
        PurchaseOrderReceipt.event_kind=='CLOSURE',PurchaseOrderReceipt.request_key.startswith(request_key+':',autoescape=True))):
        return
    workspace=receiving_workspace(db,purchase_order_id=po.id,actor_principal_id=actor_principal_id)
    if not workspace['can_receive']:
        raise ValueError('This order has no open receiving balance')
    from app.services.purchase_order_math_service import allocate_purchase
    for line in workspace['rows']:
        if not line['outstanding']:
            continue
        weights={int(sid):max(0,values['intended']-values['previous']-int(db.scalar(select(func.coalesce(func.sum(PurchaseOrderReceiptAllocation.closed_quantity),0)).where(
            PurchaseOrderReceiptAllocation.purchase_order_line_id==line['id'],PurchaseOrderReceiptAllocation.store_id==int(sid))) or 0)) for sid,values in line['stores'].items()}
        for sid,quantity in allocate_purchase(line['outstanding'],weights).items():
            if quantity:
                close_purchase_order_remainder(db,purchase_order_id=po.id,line_id=line['id'],store_id=sid,quantity=quantity,
                    actor_principal_id=actor_principal_id,request_key=f'{request_key}:{line["id"]}:{sid}')
    _update_receiving_status(db,po)



def _has_store_submission_intent(db, receipt):
    if (receipt.request_payload or {}).get('store_submission') is True:
        return True
    children=db.scalars(select(PurchaseOrderReceiptAllocation).join(PurchaseOrderReceiptLine,
        PurchaseOrderReceiptLine.id==PurchaseOrderReceiptAllocation.receipt_line_id).where(PurchaseOrderReceiptLine.receipt_id==receipt.id)).all()
    for child in children:
        if child.square_sync_event_id:
            return True  # An existing exact durable command may be recovered, never recreated from totals.
        if child.event_kind=='CORRECTION' and child.original_allocation_id:
            original=db.get(PurchaseOrderReceiptAllocation,child.original_allocation_id)
            if original and original.square_sync_event_id:
                return True
    return False
