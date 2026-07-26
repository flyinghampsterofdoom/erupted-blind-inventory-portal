from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import v2_ordering_lifecycle_repository as repository
from app.services.v2_ordering_lifecycle_repository import (
    CatalogCoverage,
    LifecycleProductRow,
    LifecycleStoreInventory,
    LifecycleWorkspaceFilters,
    query_lifecycle_workspace,
)
from app.services.v2_ordering_inventory_repository import effective_freshness


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


def _rows():
    return (
        LifecycleProductRow('VAR-A', 'CLICK-100', 'Clickmate — Pink', '7 Daze', 'ACTIVE', 0, None, mapped=True, product_name_available=True),
        LifecycleProductRow('VAR-B', 'CLICK-200', 'Clickmate — Pear', '7 Daze', 'NO_FUTURE_REORDER', 2, 'Obsolete', changed_at=NOW, changed_by='owner', mapped=True, product_name_available=True),
        LifecycleProductRow('VAR-C', 'ZERO-1', 'Zero Product', 'Juice Co', 'ACTIVE', 0, None, mapped=True, product_name_available=True),
        LifecycleProductRow('VAR-D', 'UNK-1', 'Product name unavailable', '7 Daze', 'ACTIVE', 0, None, mapped=False, product_name_available=False),
        LifecycleProductRow('VAR-E', 'ARC-1', 'Archived Product', 'Juice Co', 'ARCHIVED', 1, 'Cleanup', changed_at=NOW, changed_by='owner', pre_archive_status='NO_FUTURE_REORDER', mapped=True, product_name_available=True),
    )


COVERAGE = CatalogCoverage(4, 3, 1, 'PARTIAL', NOW, None, 'One mapped variation missing')


@pytest.fixture(autouse=True)
def local_catalog(monkeypatch):
    monkeypatch.setattr(repository, '_catalog_rows', lambda _db: (_rows(), COVERAGE, 9, None))


def _query(*, archived=False, filters=LifecycleWorkspaceFilters(), sort='product', direction='asc', page=1, size=50):
    return query_lifecycle_workspace(
        object(), archived=archived, filters=filters, sort=sort, direction=direction,
        page_number=page, page_size=size,
    )


def test_product_sku_vendor_lifecycle_and_combined_filters_are_normalized():
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(product_search=' CLICKMATE ')).rows] == ['VAR-B', 'VAR-A']
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(sku_search='unk-1')).rows] == ['VAR-D']
    assert _query(filters=LifecycleWorkspaceFilters(vendor='7 daze')).total_count == 3
    combined = _query(filters=LifecycleWorkspaceFilters(product_search='clickmate', vendor='7 Daze', lifecycle='ACTIVE'))
    assert [row.square_variation_id for row in combined.rows] == ['VAR-A']


def test_unknown_names_remain_in_population_and_have_an_explicit_filter():
    page = _query()
    assert page.unfiltered_count == 4
    assert 'VAR-D' in {row.square_variation_id for row in page.rows}
    unknown = _query(filters=LifecycleWorkspaceFilters(name_state='UNKNOWN'))
    assert [row.square_variation_id for row in unknown.rows] == ['VAR-D']
    assert unknown.rows[0].product_name == 'Product name unavailable'
    assert unknown.coverage.missing_mapped_count == 1


def test_product_name_search_excludes_unknown_names_without_changing_population_semantics():
    page = _query(filters=LifecycleWorkspaceFilters(product_search='clickmate'))
    assert page.total_count == 2
    assert page.unfiltered_count == 4
    assert all(row.product_name_available for row in page.rows)


def test_mapping_filter_sort_pagination_and_archived_restore_context():
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(mapping='UNMAPPED')).rows] == ['VAR-D']
    first = _query(page=1, size=2)
    final = _query(page=99, size=2)
    assert (first.page_number, first.total_pages, first.range_start, first.range_end) == (1, 2, 1, 2)
    assert (final.page_number, final.range_start, final.range_end) == (2, 3, 4)
    archived = _query(archived=True)
    assert archived.rows[0].pre_archive_status == 'NO_FUTURE_REORDER'
    assert first.query_count == 9


def test_inventory_sort_supports_totals_and_keeps_unavailable_rows_last(monkeypatch):
    inventory_rows = (
        replace(
            _rows()[0],
            inventory_source_available=True,
            current_inventory_total=12,
            inventory_state='FRESH',
            inventory_by_store=(LifecycleStoreInventory(1, 'Andresen', 12, 'FRESH'),),
        ),
        replace(
            _rows()[1],
            inventory_source_available=True,
            current_inventory_total=0,
            inventory_state='FRESH',
            inventory_by_store=(LifecycleStoreInventory(1, 'Andresen', 0, 'FRESH'),),
        ),
        _rows()[2],
    )
    monkeypatch.setattr(repository, '_catalog_rows', lambda _db: (inventory_rows, COVERAGE, 9, None))

    ascending = _query(sort='inventory', direction='asc')
    descending = _query(sort='inventory', direction='desc')

    assert [row.current_inventory_total for row in ascending.rows] == [0, 12, None]
    assert [row.current_inventory_total for row in descending.rows] == [12, 0, None]
    assert ascending.rows[0].inventory_by_store[0].store_name == 'Andresen'
    assert not ascending.rows[-1].inventory_source_available


def test_inventory_filters_keep_zero_distinct_from_unknown_and_stale(monkeypatch):
    inventory_rows = (
        replace(_rows()[0], inventory_source_available=True, current_inventory_total=0, inventory_state='FRESH'),
        replace(_rows()[1], inventory_source_available=True, current_inventory_total=5, inventory_state='FRESH'),
        replace(_rows()[2], inventory_source_available=True, current_inventory_total=None, inventory_state='STALE'),
        replace(_rows()[3], inventory_source_available=True, current_inventory_total=None, inventory_state='UNKNOWN'),
    )
    monkeypatch.setattr(repository, '_catalog_rows', lambda _db: (inventory_rows, COVERAGE, 9, None))

    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(inventory='ZERO')).rows] == ['VAR-A']
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(inventory='POSITIVE')).rows] == ['VAR-B']
    assert {row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(inventory='UNKNOWN')).rows} == {'VAR-C', 'VAR-D'}
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(inventory='STALE')).rows] == ['VAR-C']


@pytest.mark.parametrize(
    ('age_hours', 'expected'),
    [(24, 'FRESH'), (24.0001, 'STALE'), (72, 'STALE'), (72.0001, 'CRITICAL')],
)
def test_current_inventory_freshness_boundaries(age_hours, expected):
    refreshed_at = NOW - __import__('datetime').timedelta(hours=age_hours)
    assert effective_freshness(refreshed_at, now=NOW) == expected


@pytest.mark.parametrize(
    ('filters', 'sort', 'direction'),
    [
        (LifecycleWorkspaceFilters(name_state='MAYBE'), 'product', 'asc'),
        (LifecycleWorkspaceFilters(mapping='MAYBE'), 'product', 'asc'),
        (LifecycleWorkspaceFilters(), 'internal_id', 'asc'),
        (LifecycleWorkspaceFilters(), 'product', 'sideways'),
    ],
)
def test_filter_and_sort_allowlists_fail_closed(filters, sort, direction):
    with pytest.raises(ValueError):
        _query(filters=filters, sort=sort, direction=direction)


def test_template_contract_omits_unsupported_filters_and_touchscreen_dependencies():
    root = Path(__file__).resolve().parents[1]
    template = (root / 'app/templates/v2/ordering/lifecycle_products.html').read_text(encoding='utf-8')
    script = (root / 'app/static/v2/ordering-lifecycle.js').read_text(encoding='utf-8')
    repository_source = Path(repository.__file__).read_text(encoding='utf-8').lower()
    assert 'store relevance' not in template.lower().split('filters are deferred')[0]
    assert 'name="inventory"' in template
    assert "'POSITIVE':'Positive','ZERO':'Zero','UNKNOWN':'Unknown','STALE':'Stale'" in template
    assert 'name="store"' not in template
    assert 'name="category"' not in template
    assert 'touchscreen' not in repository_source
    assert 'Current Inventory' in template
    assert 'sort_urls.inventory' in template
    assert 'Inventory unavailable' in template
    assert '>Unknown<' in template
    assert 'Per-store inventory' in template
    assert 'data-select-all' in template
    assert 'from {{ rows|length }} visible' in template
    assert 'one atomic batch' in script
