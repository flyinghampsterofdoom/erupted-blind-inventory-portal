from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Vendor, VendorSkuConfig
from app.services.inventory_velocity_report_service import fetch_current_inventory


ZERO = Decimal('0')


@dataclass(frozen=True)
class VendorInventoryStore:
    id: int
    name: str


@dataclass(frozen=True)
class VendorInventoryRow:
    variation_id: str
    sku: str
    product_name: str
    quantities: dict[int, Decimal]
    total_quantity: Decimal
    unit_cost: Decimal | None
    retail_price: Decimal | None
    total_cost_value: Decimal | None
    total_retail_value: Decimal | None


@dataclass(frozen=True)
class VendorInventoryReport:
    vendor_id: int
    vendor_name: str
    generated_at: datetime
    stores: tuple[VendorInventoryStore, ...]
    rows: tuple[VendorInventoryRow, ...]
    store_totals: dict[int, Decimal]
    company_total: Decimal
    known_total_cost_value: Decimal
    known_total_retail_value: Decimal
    cost_complete: bool
    retail_complete: bool
    warnings: tuple[str, ...] = ()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or '0'))


def _mapping_rows(db: Session, *, vendor_id: int) -> tuple[Vendor, list[VendorSkuConfig]]:
    vendor = db.scalar(
        select(Vendor).where(Vendor.id == vendor_id, Vendor.active.is_(True))
    )
    if vendor is None:
        raise ValueError('Choose an active vendor.')
    mappings = list(db.scalars(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id == vendor_id,
            VendorSkuConfig.active.is_(True),
        )
        .order_by(
            VendorSkuConfig.is_default_vendor.desc(),
            VendorSkuConfig.updated_at.desc(),
            VendorSkuConfig.id.desc(),
        )
    ).all())
    return vendor, mappings


def build_vendor_inventory_report(
    db: Session,
    *,
    vendor_id: int,
    generated_at: datetime | None = None,
) -> VendorInventoryReport:
    """Build one variation-level report from the shared current-inventory source.

    VendorSkuConfig is the only vendor/product relationship used. Legacy mappings
    without a variation id may resolve by their existing SKU, but every resolved
    Square variation is emitted at most once.
    """
    vendor, mappings = _mapping_rows(db, vendor_id=vendor_id)
    inventory, store_rows, _ = fetch_current_inventory(db)
    stores = tuple(VendorInventoryStore(int(store_id), str(name)) for store_id, name in store_rows)

    by_sku: dict[str, list] = {}
    for item in inventory.values():
        sku = str(item.sku or '').strip()
        if sku:
            by_sku.setdefault(sku.casefold(), []).append(item)

    resolved: dict[str, tuple[object, VendorSkuConfig]] = {}
    unresolved = 0
    ambiguous_legacy = 0
    duplicate_paths = 0
    for mapping in mappings:
        variation_id = str(mapping.square_variation_id or '').strip()
        item = inventory.get(variation_id) if variation_id else None
        if item is None and not variation_id:
            candidates = by_sku.get(str(mapping.sku or '').strip().casefold(), [])
            if len(candidates) == 1:
                item = candidates[0]
                variation_id = str(item.variation_id)
            elif len(candidates) > 1:
                ambiguous_legacy += 1
                continue
        if item is None or not variation_id:
            unresolved += 1
            continue
        if variation_id in resolved:
            duplicate_paths += 1
            continue
        resolved[variation_id] = (item, mapping)

    store_totals = {store.id: ZERO for store in stores}
    rows: list[VendorInventoryRow] = []
    known_total_cost = ZERO
    known_total_retail = ZERO
    cost_complete = True
    retail_complete = True
    for variation_id, (item, mapping) in resolved.items():
        quantities = {
            store.id: _decimal(item.by_store.get(store.id, ZERO))
            for store in stores
        }
        total_quantity = sum(quantities.values(), ZERO)
        for store_id, quantity in quantities.items():
            store_totals[store_id] += quantity
        unit_cost = _decimal(mapping.unit_cost) if mapping.unit_cost is not None else None
        retail_price = (
            _decimal(item.unit_price) if getattr(item, 'unit_price', None) is not None else None
        )
        total_cost_value = total_quantity * unit_cost if unit_cost is not None else None
        total_retail_value = total_quantity * retail_price if retail_price is not None else None
        if total_cost_value is None:
            if total_quantity != ZERO:
                cost_complete = False
        else:
            known_total_cost += total_cost_value
        if total_retail_value is None:
            if total_quantity != ZERO:
                retail_complete = False
        else:
            known_total_retail += total_retail_value
        rows.append(VendorInventoryRow(
            variation_id=variation_id,
            sku=str(item.sku or mapping.sku or variation_id),
            product_name=str(item.product_name or item.sku or variation_id),
            quantities=quantities,
            total_quantity=total_quantity,
            unit_cost=unit_cost,
            retail_price=retail_price,
            total_cost_value=total_cost_value,
            total_retail_value=total_retail_value,
        ))

    rows.sort(key=lambda row: (row.product_name.casefold(), row.sku.casefold(), row.variation_id))
    warnings: list[str] = []
    if unresolved:
        warnings.append(
            f'{unresolved} active vendor mapping(s) could not be resolved to a current catalog variation.'
        )
    if ambiguous_legacy:
        warnings.append(
            f'{ambiguous_legacy} legacy SKU mapping(s) matched multiple catalog variations and were excluded.'
        )
    if duplicate_paths:
        warnings.append(
            f'{duplicate_paths} duplicate or legacy mapping path(s) resolved to an already included variation and were deduplicated.'
        )
    if not cost_complete:
        warnings.append('Some in-stock variations have unknown unit cost; cost totals include known values only.')
    if not retail_complete:
        warnings.append('Some in-stock variations have unknown retail price; retail totals include known values only.')

    return VendorInventoryReport(
        vendor_id=int(vendor.id),
        vendor_name=str(vendor.name),
        generated_at=generated_at or datetime.now(tz=timezone.utc),
        stores=stores,
        rows=tuple(rows),
        store_totals=store_totals,
        company_total=sum(store_totals.values(), ZERO),
        known_total_cost_value=known_total_cost,
        known_total_retail_value=known_total_retail,
        cost_complete=cost_complete,
        retail_complete=retail_complete,
        warnings=tuple(warnings),
    )


def _quantity(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f'{int(value):,}'
    return f'{value:,.3f}'.rstrip('0').rstrip('.')


def _money(value: Decimal | None) -> str:
    return 'Unknown' if value is None else f'${value:,.2f}'


def vendor_inventory_pdf(report: VendorInventoryReport, *, include_financials: bool) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import LETTER, LEGAL, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle
    except Exception as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError('PDF generation dependency missing: install reportlab') from exc

    output = BytesIO()
    base_page_size = landscape(LEGAL if len(report.stores) > 5 else LETTER)
    financial_width = 3.25 * inch if include_financials else 0
    minimum_table_width = 2.35 * inch + financial_width + ((len(report.stores) + 1) * 0.55 * inch)
    page_size = (max(base_page_size[0], minimum_table_width + 0.6 * inch), base_page_size[1])
    doc = SimpleDocTemplate(
        output,
        pagesize=page_size,
        leftMargin=0.3 * inch,
        rightMargin=0.3 * inch,
        topMargin=0.35 * inch,
        bottomMargin=0.35 * inch,
        pageCompression=0,
        title=('Vendor Inventory + Cost/Price' if include_financials else 'Vendor Inventory'),
        author='Erupted Operations',
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'VendorInventoryTitle', parent=styles['Title'], fontSize=16, leading=19,
        alignment=TA_LEFT, textColor=colors.HexColor('#18382b'), spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        'VendorInventoryMeta', parent=styles['Normal'], fontSize=8, leading=11,
        textColor=colors.HexColor('#52625b'),
    )
    body_style = ParagraphStyle(
        'VendorInventoryBody', parent=styles['BodyText'], fontSize=6.5, leading=8,
    )
    story = [
        Paragraph('Vendor Inventory + Cost/Price' if include_financials else 'Vendor Inventory', title_style),
        Paragraph(f'Vendor: {escape(report.vendor_name)}', meta_style),
        Paragraph(f'Generated: {report.generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}', meta_style),
        Spacer(1, 0.14 * inch),
    ]

    headers = ['Product', 'SKU', *[store.name for store in report.stores], 'Total']
    if include_financials:
        headers.extend(['Unit Cost', 'Retail Price', 'Total Cost Value', 'Total Retail Value'])
    data: list[list[object]] = [[Paragraph(escape(value), body_style) for value in headers]]
    for row in report.rows:
        values: list[str] = [row.product_name, row.sku]
        values.extend(_quantity(row.quantities[store.id]) for store in report.stores)
        values.append(_quantity(row.total_quantity))
        if include_financials:
            values.extend([
                _money(row.unit_cost), _money(row.retail_price),
                _money(row.total_cost_value), _money(row.total_retail_value),
            ])
        data.append([Paragraph(escape(value), body_style) for value in values])
    totals: list[str] = ['TOTAL INVENTORY', '']
    totals.extend(_quantity(report.store_totals[store.id]) for store in report.stores)
    totals.append(_quantity(report.company_total))
    if include_financials:
        totals.extend([
            '', '',
            ('Known ' if not report.cost_complete else '') + _money(report.known_total_cost_value),
            ('Known ' if not report.retail_complete else '') + _money(report.known_total_retail_value),
        ])
    data.append([Paragraph(escape(value), body_style) for value in totals])

    available_width = page_size[0] - doc.leftMargin - doc.rightMargin
    fixed_width = 2.35 * inch + financial_width
    quantity_count = len(report.stores) + 1
    quantity_width = (available_width - fixed_width) / max(quantity_count, 1)
    column_widths = [1.6 * inch, 0.75 * inch]
    column_widths.extend([quantity_width] * quantity_count)
    if include_financials:
        column_widths.extend([0.7 * inch, 0.75 * inch, 0.9 * inch, 0.9 * inch])
    table = LongTable(data, colWidths=column_widths, repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#18382b')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#e8eee9')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('ALIGN', (2, 1), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#b7c3bc')),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    if include_financials and (not report.cost_complete or not report.retail_complete):
        story.extend([
            Spacer(1, 0.1 * inch),
            Paragraph(
                'Unknown financial values are not treated as zero. Grand totals labeled “Known” include only rows with an authoritative value.',
                meta_style,
            ),
        ])
    doc.build(story)
    return output.getvalue()
