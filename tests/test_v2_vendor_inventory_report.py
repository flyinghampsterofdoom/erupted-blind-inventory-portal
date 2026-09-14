from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Store, Vendor, VendorSkuConfig
from app.routers.v2_reporting import reporting_access, router
from app.services.access_control_service import fallback_allowed_for_role
from app.services.v2_vendor_inventory_report_service import (
    build_vendor_inventory_report,
    vendor_inventory_pdf,
)
from app.v2.navigation import build_navigation


@pytest.fixture()
def vendor_inventory_db():
    engine = create_engine('sqlite:///:memory:')
    for table in (Store.__table__, Vendor.__table__, VendorSkuConfig.__table__):
        table.create(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        db.add_all([
            Store(id=1, name='North', square_location_id='N', active=True),
            Store(id=2, name='South', square_location_id='S', active=True),
            Vendor(id=10, square_vendor_id='SQ-A', name='Vendor A', active=True),
            Vendor(id=20, square_vendor_id='SQ-B', name='Vendor B', active=True),
        ])
        db.add_all([
            VendorSkuConfig(
                id=1, vendor_id=10, sku='A', square_variation_id='VAR-A',
                unit_cost=Decimal('2.50'), is_default_vendor=True, active=True,
            ),
            VendorSkuConfig(
                id=2, vendor_id=10, sku='A-ALIAS', square_variation_id='VAR-A',
                unit_cost=Decimal('99.00'), is_default_vendor=False, active=True,
            ),
            VendorSkuConfig(
                id=3, vendor_id=10, sku='B', square_variation_id='VAR-B',
                unit_cost=None, is_default_vendor=True, active=True,
            ),
            VendorSkuConfig(
                id=4, vendor_id=10, sku='C', square_variation_id='VAR-C',
                unit_cost=None, is_default_vendor=True, active=True,
            ),
            VendorSkuConfig(
                id=5, vendor_id=20, sku='OTHER', square_variation_id='VAR-OTHER',
                unit_cost=Decimal('1.00'), is_default_vendor=True, active=True,
            ),
        ])
        db.commit()
        yield db
    engine.dispose()


def _inventory():
    return {
        'VAR-A': SimpleNamespace(
            variation_id='VAR-A', sku='A', product_name='Same Product — Red',
            unit_price=Decimal('8.00'), by_store={1: Decimal('3'), 2: Decimal('2')},
        ),
        'VAR-B': SimpleNamespace(
            variation_id='VAR-B', sku='B', product_name='Same Product — Blue',
            unit_price=None, by_store={1: Decimal('1'), 2: Decimal('4')},
        ),
        'VAR-C': SimpleNamespace(
            variation_id='VAR-C', sku='C', product_name='Zero Product — Default',
            unit_price=None, by_store={1: Decimal('0'), 2: Decimal('0')},
        ),
        'VAR-OTHER': SimpleNamespace(
            variation_id='VAR-OTHER', sku='OTHER', product_name='Other Vendor Product',
            unit_price=Decimal('10.00'), by_store={1: Decimal('100'), 2: Decimal('100')},
        ),
    }


def test_vendor_inventory_uses_vendor_mapping_store_splits_totals_and_distinct_variations(
    monkeypatch, vendor_inventory_db,
):
    monkeypatch.setattr(
        'app.services.v2_vendor_inventory_report_service.fetch_current_inventory',
        lambda _db: (_inventory(), [(1, 'North'), (2, 'South')], {'N': 1, 'S': 2}),
    )
    report = build_vendor_inventory_report(
        vendor_inventory_db,
        vendor_id=10,
        generated_at=datetime(2026, 9, 14, 12, tzinfo=timezone.utc),
    )

    assert [row.variation_id for row in report.rows] == ['VAR-B', 'VAR-A', 'VAR-C']
    assert {row.sku for row in report.rows} == {'A', 'B', 'C'}
    assert all(row.variation_id != 'VAR-OTHER' for row in report.rows)
    assert len(report.rows) == 3  # Duplicate/alias mapping for VAR-A is not a second row.
    by_variation = {row.variation_id: row for row in report.rows}
    assert by_variation['VAR-A'].quantities == {1: Decimal('3'), 2: Decimal('2')}
    assert by_variation['VAR-A'].total_quantity == Decimal('5')
    assert by_variation['VAR-B'].quantities == {1: Decimal('1'), 2: Decimal('4')}
    assert by_variation['VAR-B'].total_quantity == Decimal('5')
    assert by_variation['VAR-C'].quantities == {1: Decimal('0'), 2: Decimal('0')}
    assert by_variation['VAR-C'].total_quantity == Decimal('0')
    assert report.store_totals == {1: Decimal('4'), 2: Decimal('6')}
    assert report.company_total == Decimal('10')
    assert by_variation['VAR-A'].unit_cost == Decimal('2.50')
    assert by_variation['VAR-A'].retail_price == Decimal('8.00')
    assert by_variation['VAR-A'].total_cost_value == Decimal('12.50')
    assert by_variation['VAR-A'].total_retail_value == Decimal('40.00')
    assert by_variation['VAR-B'].total_cost_value is None
    assert by_variation['VAR-B'].total_retail_value is None
    assert by_variation['VAR-C'].total_cost_value is None
    assert by_variation['VAR-C'].total_retail_value is None
    assert report.known_total_cost_value == Decimal('12.50')
    assert report.known_total_retail_value == Decimal('40.00')
    assert report.cost_complete is False and report.retail_complete is False
    assert any('deduplicated' in warning for warning in report.warnings)
    assert any('known values only' in warning for warning in report.warnings)


def test_inventory_and_financial_pdfs_have_strictly_separate_content(
    monkeypatch, vendor_inventory_db,
):
    monkeypatch.setattr(
        'app.services.v2_vendor_inventory_report_service.fetch_current_inventory',
        lambda _db: (_inventory(), [(1, 'North'), (2, 'South')], {'N': 1, 'S': 2}),
    )
    report = build_vendor_inventory_report(vendor_inventory_db, vendor_id=10)

    inventory_pdf = vendor_inventory_pdf(report, include_financials=False)
    financial_pdf = vendor_inventory_pdf(report, include_financials=True)

    assert inventory_pdf.startswith(b'%PDF-') and financial_pdf.startswith(b'%PDF-')
    for safe_text in (b'Vendor Inventory', b'Vendor: Vendor A', b'North', b'South', b'TOTAL INVENTORY'):
        assert safe_text in inventory_pdf
    for forbidden in (b'Unit Cost', b'Retail Price', b'Total Cost Value', b'Total Retail Value', b'$12.50'):
        assert forbidden not in inventory_pdf
    for financial_text in (
        b'Unit Cost', b'Retail Price', b'Total Cost Value', b'Total Retail Value', b'$12.50', b'$40.00', b'Unknown',
    ):
        assert financial_text in financial_pdf


def test_vendor_inventory_routes_reuse_existing_reporting_financial_permission():
    relevant = {
        route.path: [dependency.call for dependency in route.dependant.dependencies]
        for route in router.routes
        if route.path.startswith('/v2/reports/vendor-inventory')
    }
    assert set(relevant) == {
        '/v2/reports/vendor-inventory',
        '/v2/reports/vendor-inventory/inventory.pdf',
        '/v2/reports/vendor-inventory/inventory-cost-price.pdf',
    }
    assert all(reporting_access in dependencies for dependencies in relevant.values())
    assert fallback_allowed_for_role(role='ADMIN', permission_key='reports.workbench.view')
    assert fallback_allowed_for_role(role='MANAGER', permission_key='reports.workbench.view')
    assert not fallback_allowed_for_role(role='LEAD', permission_key='reports.workbench.view')


def test_vendor_inventory_navigation_is_available_and_uniquely_active():
    request = SimpleNamespace(
        url=SimpleNamespace(path='/v2/reports/vendor-inventory'),
        query_params={},
        state=SimpleNamespace(
            principal=SimpleNamespace(id=1, store_id=None),
            permission_flags={
                'nav.reports.all': True,
                'reports.workbench.view': True,
            },
        ),
    )
    reports = next(section for section in build_navigation(request) if section.key == 'reports')
    vendor_child = next(child for child in reports.children if child.key == 'reports.vendor_inventory')
    workbench_child = next(child for child in reports.children if child.key == 'reports.workbench')
    assert vendor_child.available and vendor_child.active
    assert not workbench_child.active
