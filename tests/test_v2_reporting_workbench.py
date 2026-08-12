from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.auth import Principal, Role
from app.main import app
from app.models import (
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    PrincipalRole,
    ReportingSavedView,
    Store,
    Vendor,
)
from app.models import (
    Principal as PrincipalModel,
)
from app.routers.v2_reporting import _context, reporting_page, router, run_report_route
from app.security.csrf import verify_csrf
from app.services.access_control_service import fallback_allowed_for_role
from app.services.v2_reporting_workbench_service import (
    REPORT_DEFINITIONS,
    ReportResult,
    SearchableProduct,
    delete_saved_view,
    get_saved_view,
    parse_search_terms,
    product_matches,
    resolve_relative_dates,
    run_sales_analysis,
    run_stock_value,
    save_view,
)


@compiles(CITEXT, 'sqlite')
def _compile_citext_sqlite(_type, _compiler, **_kwargs):
    return 'TEXT'


@pytest.fixture()
def reporting_db():
    engine = create_engine('sqlite:///:memory:')
    for table in (
        Store.__table__, PrincipalModel.__table__, Vendor.__table__,
        ConsignmentSaleFact.__table__, ConsignmentSalesSyncState.__table__,
        ReportingSavedView.__table__,
    ):
        table.create(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def assign_saved_view_id(session, *_):
        next_id = 1
        for row in session.new:
            if isinstance(row, ReportingSavedView) and row.id is None:
                row.id = next_id
                next_id += 1

    event.listen(Session, 'before_flush', assign_saved_view_id)
    with SessionLocal() as db:
        db.add_all([
            Store(id=1, name='North', square_location_id='N', active=True),
            Store(id=2, name='South', square_location_id='S', active=True),
            PrincipalModel(id=1, username='owner', password_hash='x', role=PrincipalRole.ADMIN, active=True),
            PrincipalModel(id=2, username='other', password_hash='x', role=PrincipalRole.ADMIN, active=True),
        ])
        db.add(ConsignmentSalesSyncState(
            id=1, last_result='COMPLETE',
            last_successful_through_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        ))
        db.commit()
        yield db
    event.remove(Session, 'before_flush', assign_saved_view_id)
    engine.dispose()


def _sale(
    row_id: int, *, name: str, variation: str, sku: str, store_id: int,
    day: date = date(2026, 8, 10), units: str = '1', gross: str = '10',
    discount: str = '0', net: str = '10', cogs: str | None = '4', vendor: str = 'Vendor A',
) -> ConsignmentSaleFact:
    return ConsignmentSaleFact(
        id=row_id, square_order_id=f'ORDER-{row_id}', square_line_item_uid=f'LINE-{row_id}',
        square_variation_id=f'VAR-{row_id}', square_product_id=f'ITEM-{row_id}',
        square_location_id='N' if store_id == 1 else 'S', store_id=store_id,
        business_date=day, transacted_at=datetime(2026, 8, 10, 18, tzinfo=timezone.utc),
        quantity_sold=Decimal(units), gross_sales_amount=Decimal(gross),
        discount_amount=Decimal(discount), tax_amount=Decimal('0'), net_sales_amount=Decimal(net),
        currency='USD', product_name_snapshot=name, variation_name_snapshot=variation,
        sku_snapshot=sku, vendor_name_snapshot=vendor, extended_cogs_snapshot=(Decimal(cogs) if cogs else None),
        attribution_status='ATTRIBUTED' if cogs else 'MISSING_COST', attribution_source='SYNC',
        source_synchronized_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )


def _form_request(data: list[tuple[str, str]], principal: Principal) -> Request:
    body = urlencode(data).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {'type': 'http.disconnect'}
        sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    request = Request({
        'type': 'http', 'method': 'POST', 'path': '/v2/reports/run',
        'raw_path': b'/v2/reports/run', 'query_string': b'',
        'headers': [(b'content-type', b'application/x-www-form-urlencoded')],
        'scheme': 'https', 'server': ('test', 443), 'client': ('127.0.0.1', 1),
        'app': app,
    }, receive)
    request.state.principal = principal
    request.state.permission_flags = {
        'management.access': True, 'nav.reports.all': True,
        'reports.workbench.view': True,
    }
    request.state.csrf_token = 'test-token'
    return request


def test_search_term_parsing_is_familiar_deterministic_and_deduplicated():
    assert set(REPORT_DEFINITIONS) == {'sales_analysis', 'stock_value'}
    assert REPORT_DEFINITIONS['stock_value'].date_mode == 'current_only'
    assert parse_search_terms(' Juice Head, Lemon; juice head\nZero ') == [
        'Juice Head', 'Lemon', 'Zero',
    ]
    assert parse_search_terms('Juice Head,Lemon,Zero') == ['Juice Head', 'Lemon', 'Zero']
    assert parse_search_terms('Juice Head;Lemon;Zero') == ['Juice Head', 'Lemon', 'Zero']


def test_product_search_is_case_insensitive_contains_with_any_and_all():
    product = SearchableProduct('Juice Head Freeze', 'Lemon', 'JH-42', 'VAR-1')
    assert product_matches(product, include_terms=['juice head'])
    assert product_matches(product, include_terms=['head', 'LEMON'], match_mode='all')
    assert product_matches(product, include_terms=['missing', 'jh-42'], match_mode='any')
    assert not product_matches(product, include_terms=['head', 'missing'], match_mode='all')
    assert not product_matches(product, include_terms=['juice'], exclude_terms=['freeze'])
    assert not product_matches(product, include_terms=['juice', 'missing'], exclude_terms=['other', 'lemon'])


def test_sales_analysis_date_search_exclusions_store_grouping_and_metrics(reporting_db):
    reporting_db.add_all([
        _sale(1, name='Juice Head', variation='Lemon Bottle', sku='JH-B', store_id=1, units='2', gross='20', discount='2', net='18', cogs='8'),
        _sale(2, name='Juice Head', variation='Lemon Pouch', sku='JH-P', store_id=1, net='9', cogs='3'),
        _sale(3, name='Juice Head', variation='Peach Bottle', sku='JH-X', store_id=2, net='12', cogs='5'),
        _sale(4, name='Other', variation='Lemon', sku='OTHER', store_id=1, net='7', cogs='2'),
        _sale(5, name='Juice Head Old', variation='Bottle', sku='OLD', store_id=1, day=date(2026, 7, 1)),
    ])
    reporting_db.commit()

    result = run_sales_analysis(
        reporting_db, start_date=date(2026, 8, 1), end_date=date(2026, 8, 12),
        store_ids=[1], include_terms=parse_search_terms('juice head; lemon'),
        exclude_terms=parse_search_terms('pouch, disposable'), match_mode='all', grouping='product',
    )
    assert result.matched_product_count == 1
    assert result.sale_count == 1
    assert result.excluded_products == ('Juice Head — Lemon Pouch',)
    assert result.rows[0]['units_sold'] == Decimal('2')
    assert result.rows[0]['gross_sales'] == Decimal('20')
    assert result.rows[0]['discounts'] == Decimal('2')
    assert result.rows[0]['net_sales'] == Decimal('18')
    assert result.rows[0]['cogs'] == Decimal('8')
    assert result.rows[0]['gross_profit'] == Decimal('10')

    by_store = run_sales_analysis(
        reporting_db, start_date=date(2026, 8, 1), end_date=date(2026, 8, 12), grouping='store',
    )
    assert {row['group'] for row in by_store.rows} == {'North', 'South'}


def test_sales_analysis_match_any_and_missing_cost_are_explicit(reporting_db):
    reporting_db.add_all([
        _sale(1, name='Juice Head', variation='Peach', sku='ONE', store_id=1, cogs=None),
        _sale(2, name='Other', variation='Lemon', sku='TWO', store_id=1),
    ])
    reporting_db.commit()
    result = run_sales_analysis(
        reporting_db, start_date=date(2026, 8, 1), end_date=date(2026, 8, 12),
        include_terms=['juice', 'lemon'], match_mode='any', grouping='product',
    )
    assert result.sale_count == 2
    juice = next(row for row in result.rows if row['group'] == 'Juice Head')
    assert juice['cogs'] is None and juice['gross_profit'] is None and juice['gross_margin'] is None
    assert 'no authoritative cost snapshot' in result.warnings[0]


def test_stock_value_uses_existing_inventory_costs_and_marks_unknown(monkeypatch, reporting_db):
    inventory = {
        'A': SimpleNamespace(
            variation_id='A', sku='JH-A', product_name='Juice Head Lemon Bottle', vendor='Vendor A',
            unit_cost=Decimal('4'), by_store={1: Decimal('3'), 2: Decimal('2')},
        ),
        'B': SimpleNamespace(
            variation_id='B', sku='JH-P', product_name='Juice Head Lemon Pouch', vendor='Vendor A',
            unit_cost=None, by_store={1: Decimal('5'), 2: Decimal('0')},
        ),
    }
    monkeypatch.setattr(
        'app.services.v2_reporting_workbench_service.fetch_current_inventory',
        lambda _db: (inventory, [(1, 'North'), (2, 'South')], {'N': 1, 'S': 2}),
    )
    monkeypatch.setattr(
        'app.services.v2_reporting_workbench_service._lifecycle_by_variation',
        lambda _db: {'A': 'ACTIVE', 'B': 'NO_FUTURE_REORDER'},
    )
    result = run_stock_value(
        reporting_db, store_ids=[1], include_terms=['juice head'], exclude_terms=['pouch'],
        grouping='variation',
    )
    assert result.matched_product_count == 1
    assert result.rows[0]['quantity_on_hand'] == Decimal('3')
    assert result.rows[0]['unit_cost'] == Decimal('4')
    assert result.rows[0]['inventory_value'] == Decimal('12')
    assert result.excluded_products == ('Juice Head Lemon Pouch',)

    unknown = run_stock_value(reporting_db, store_ids=[1], include_terms=['pouch'])
    assert unknown.rows[0]['unit_cost'] is None and unknown.rows[0]['inventory_value'] is None
    assert 'no authoritative cost basis' in unknown.warnings[0]

    by_store = run_stock_value(reporting_db, grouping='store')
    assert {row['group'] for row in by_store.rows} == {'North', 'South'}


def test_realistic_sales_and_stock_requests_render_results(monkeypatch, reporting_db):
    reporting_db.add_all([
        _sale(1, name='Juice Head', variation='Lemon Bottle', sku='JH-B', store_id=1,
              units='2', gross='20', discount='2', net='18', cogs='8'),
        _sale(2, name='Juice Head', variation='Lemon Pouch', sku='JH-P', store_id=1,
              net='9', cogs='3'),
    ])
    reporting_db.commit()
    principal = Principal(id=1, username='owner', role=Role.ADMIN, store_id=None, active=True)
    sales_request = _form_request([
        ('report_type', 'sales_analysis'), ('date_mode', 'custom'),
        ('start_date', '2026-08-01'), ('end_date', '2026-08-12'),
        ('store_id', '1'), ('include_search', 'Juice Head; Lemon'),
        ('exclude_search', 'Pouch'), ('match_mode', 'all'),
        ('grouping', 'product'), ('sort', 'net_sales_desc'),
        ('metric', 'net_sales'), ('metric', 'cogs'), ('metric', 'gross_profit'),
    ], principal)
    sales_response = asyncio.run(run_report_route(sales_request, principal, None, reporting_db))
    sales_html = bytes(sales_response.body).decode()
    assert sales_response.status_code == 200
    assert 'Matched 1 product' in sales_html
    assert '$18.00' in sales_html and '$8.00' in sales_html and '$10.00' in sales_html

    inventory = {
        'A': SimpleNamespace(
            variation_id='A', sku='JH-A', product_name='Juice Head Lemon Bottle',
            vendor='Vendor A', unit_cost=Decimal('4'), by_store={1: Decimal('3')},
        ),
        'B': SimpleNamespace(
            variation_id='B', sku='JH-P', product_name='Juice Head Lemon Pouch',
            vendor='Vendor A', unit_cost=None, by_store={1: Decimal('5')},
        ),
    }
    monkeypatch.setattr(
        'app.services.v2_reporting_workbench_service.fetch_current_inventory',
        lambda _db: (inventory, [(1, 'North')], {'N': 1}),
    )
    monkeypatch.setattr(
        'app.services.v2_reporting_workbench_service._lifecycle_by_variation',
        lambda _db: {'A': 'ACTIVE', 'B': 'ACTIVE'},
    )
    stock_request = _form_request([
        ('report_type', 'stock_value'), ('date_mode', 'custom'), ('store_id', '1'),
        ('include_search', 'Juice Head'), ('match_mode', 'any'),
        ('grouping', 'variation'), ('sort', 'inventory_value_desc'),
    ], principal)
    stock_response = asyncio.run(run_report_route(stock_request, principal, None, reporting_db))
    stock_html = bytes(stock_response.body).decode()
    assert stock_response.status_code == 200
    assert 'Juice Head Lemon Bottle' in stock_html and '$12.00' in stock_html
    assert 'Juice Head Lemon Pouch' in stock_html and '>Unknown<' in stock_html


def test_saved_view_create_load_update_delete_and_owner_boundary(reporting_db):
    row = save_view(
        reporting_db, principal_id=1, name='Juice Head Bottle Sales', report_type='sales_analysis',
        configuration={'include_terms': ['Juice Head'], 'exclude_terms': ['Pouch']},
    )
    reporting_db.commit()
    assert get_saved_view(reporting_db, principal_id=1, view_id=row.id).configuration['exclude_terms'] == ['Pouch']
    with pytest.raises(LookupError):
        get_saved_view(reporting_db, principal_id=2, view_id=row.id)
    save_view(
        reporting_db, principal_id=1, view_id=row.id, name='Updated', report_type='sales_analysis',
        configuration={'grouping': 'store'},
    )
    reporting_db.commit()
    assert get_saved_view(reporting_db, principal_id=1, view_id=row.id).name == 'Updated'
    with pytest.raises(LookupError):
        delete_saved_view(reporting_db, principal_id=2, view_id=row.id)
    delete_saved_view(reporting_db, principal_id=1, view_id=row.id)
    reporting_db.commit()
    with pytest.raises(LookupError):
        get_saved_view(reporting_db, principal_id=1, view_id=row.id)


def test_relative_date_definitions():
    today = date(2026, 8, 12)
    assert resolve_relative_dates('last_7_days', today=today) == (date(2026, 8, 6), today)
    assert resolve_relative_dates('last_month', today=today) == (date(2026, 7, 1), date(2026, 7, 31))
    assert resolve_relative_dates('choose_when_run', today=today) is None


def test_relative_date_saved_view_keeps_definition_and_resolves_when_run(reporting_db):
    row = save_view(
        reporting_db, principal_id=1, name='Rolling week', report_type='sales_analysis',
        configuration={'date_mode': 'last_7_days', 'start_date': '', 'end_date': ''},
    )
    reporting_db.commit()
    loaded = get_saved_view(reporting_db, principal_id=1, view_id=row.id)
    assert loaded.configuration['date_mode'] == 'last_7_days'
    assert resolve_relative_dates(loaded.configuration['date_mode'], today=date(2026, 8, 12)) == (
        date(2026, 8, 6), date(2026, 8, 12),
    )


def test_reporting_page_loads_and_saved_view_mutations_require_csrf(reporting_db):
    assert fallback_allowed_for_role(
        role=PrincipalRole.ADMIN, permission_key='reports.workbench.view'
    )
    assert not fallback_allowed_for_role(
        role=PrincipalRole.STORE, permission_key='reports.workbench.view'
    )
    principal = Principal(id=1, username='owner', role=Role.ADMIN, store_id=None, active=True)
    scope = {
        'type': 'http', 'method': 'GET', 'path': '/v2/reports', 'raw_path': b'/v2/reports',
        'query_string': b'', 'headers': [], 'scheme': 'https', 'server': ('test', 443),
        'client': ('127.0.0.1', 1), 'app': app,
    }
    request = Request(scope)
    request.state.principal = principal
    request.state.permission_flags = {
        'management.access': True, 'nav.reports.all': True, 'reports.workbench.view': True,
    }
    request.state.csrf_token = 'test-token'
    response = reporting_page(request, principal, reporting_db)
    html = bytes(response.body).decode()
    assert response.status_code == 200
    assert 'Reporting Workbench' in html and 'Sales Analysis' in html and 'Stock Value' in html
    assert 'Exclusions' in html and 'data-token-editor' in html
    assert 'does not net returns' in html
    assert 'Stock Value is current-only' in html
    assert 'never silently treated as $0' in html

    stock_result = ReportResult(
        report_type='stock_value',
        columns=(('group', 'Variation'), ('unit_cost', 'Unit cost / cost basis'), ('inventory_value', 'Inventory value')),
        rows=({'group': 'Juice Head Pouch', 'unit_cost': None, 'inventory_value': None},),
        matched_product_count=1,
        sale_count=0,
        warnings=('1 inventory position has no authoritative cost basis.',),
    )
    rendered = request.app.state.templates.TemplateResponse(
        'v2/reporting/workbench.html',
        _context(
            request, principal, reporting_db,
            config={'report_type': 'stock_value', 'grouping': 'variation'},
            result=stock_result,
        ),
    )
    stock_html = bytes(rendered.body).decode()
    assert 'Juice Head Pouch' in stock_html
    assert stock_html.count('>Unknown<') == 2

    mutation_paths = {
        '/v2/reports/saved-views',
        '/v2/reports/saved-views/{view_id}',
        '/v2/reports/saved-views/{view_id}/delete',
    }
    for route in router.routes:
        if route.path in mutation_paths:
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            assert verify_csrf in dependencies
