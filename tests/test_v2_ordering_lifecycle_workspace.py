from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import v2_ordering_lifecycle_repository as repository
from app.services.v2_ordering_lifecycle_repository import (
    LifecycleProductRow,
    LifecycleWorkspaceFilters,
    query_lifecycle_workspace,
)


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


def _rows():
    return (
        LifecycleProductRow('VAR-A', 'CLICK-100', 'Clickmate — Pink', '7 Daze', 'ACTIVE', 0, None, mapped=True, inventory_total=Decimal('5'), relevant_store_ids=(1,)),
        LifecycleProductRow('VAR-B', 'CLICK-200', 'Clickmate — Pear', '7 Daze', 'NO_FUTURE_REORDER', 2, 'Obsolete', changed_at=NOW, changed_by='owner', mapped=True, inventory_total=Decimal('1'), relevant_store_ids=(2,)),
        LifecycleProductRow('VAR-C', 'ZERO-1', 'Zero Product', 'Juice Co', 'ACTIVE', 0, None, mapped=True, inventory_total=Decimal('0'), relevant_store_ids=(1,)),
        LifecycleProductRow('VAR-D', 'UNK-1', 'Unknown Inventory', '7 Daze', 'ACTIVE', 0, None, mapped=False, inventory_total=None, relevant_store_ids=()),
        LifecycleProductRow('VAR-E', 'ARC-1', 'Archived Product', 'Juice Co', 'ARCHIVED', 1, 'Cleanup', changed_at=NOW, changed_by='owner', pre_archive_status='NO_FUTURE_REORDER', mapped=True, inventory_total=Decimal('0'), relevant_store_ids=(1,)),
    )


@pytest.fixture(autouse=True)
def local_catalog(monkeypatch):
    monkeypatch.setattr(repository, '_catalog_rows', lambda _db: (_rows(), ((1, 'Andresen'), (2, 'SR 503')), 6))


def _query(*, archived=False, filters=LifecycleWorkspaceFilters(), sort='product', direction='asc', page=1, size=50):
    return query_lifecycle_workspace(
        object(),
        archived=archived,
        filters=filters,
        sort=sort,
        direction=direction,
        page_number=page,
        page_size=size,
    )


def test_product_sku_vendor_lifecycle_and_combined_filters_are_normalized():
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(product_search='  CLICKMATE ')).rows] == ['VAR-B', 'VAR-A']
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(sku_search='click-2')).rows] == ['VAR-B']
    assert _query(filters=LifecycleWorkspaceFilters(vendor='7 daze')).total_count == 3
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(lifecycle='NO_FUTURE_REORDER')).rows] == ['VAR-B']
    combined = _query(filters=LifecycleWorkspaceFilters(product_search='clickmate', vendor='7 Daze', lifecycle='ACTIVE'))
    assert [row.square_variation_id for row in combined.rows] == ['VAR-A']


def test_store_inventory_mapping_and_explicit_unknown_filters():
    assert {row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(store='1')).rows} == {'VAR-A', 'VAR-C'}
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(store='unknown')).rows] == ['VAR-D']
    assert {row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(inventory='POSITIVE')).rows} == {'VAR-A', 'VAR-B'}
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(inventory='ZERO')).rows] == ['VAR-C']
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(inventory='UNKNOWN')).rows] == ['VAR-D']
    assert [row.square_variation_id for row in _query(filters=LifecycleWorkspaceFilters(mapping='UNMAPPED')).rows] == ['VAR-D']


def test_lifecycle_sort_uses_policy_order_and_variation_id_tie_breaker():
    page = _query(sort='lifecycle')
    assert [(row.status, row.square_variation_id) for row in page.rows] == [
        ('ACTIVE', 'VAR-A'), ('ACTIVE', 'VAR-C'), ('ACTIVE', 'VAR-D'), ('NO_FUTURE_REORDER', 'VAR-B')
    ]
    descending = _query(sort='lifecycle', direction='desc')
    assert descending.rows[0].status == 'NO_FUTURE_REORDER'
    assert [row.square_variation_id for row in descending.rows[1:]] == ['VAR-A', 'VAR-C', 'VAR-D']


def test_server_pagination_clamps_boundaries_and_reports_ranges():
    first = _query(page=1, size=2)
    middle = _query(page=2, size=2)
    final = _query(page=99, size=2)
    assert (first.page_number, first.total_pages, first.range_start, first.range_end) == (1, 2, 1, 2)
    assert (middle.page_number, middle.range_start, middle.range_end) == (2, 3, 4)
    assert (final.page_number, final.range_start, final.range_end) == (2, 3, 4)
    assert first.query_count == 6


def test_archived_workspace_is_separate_and_preserves_pre_archive_state():
    page = _query(archived=True, filters=LifecycleWorkspaceFilters(product_search='archived'))
    assert page.total_count == 1
    assert page.rows[0].status == 'ARCHIVED'
    assert page.rows[0].pre_archive_status == 'NO_FUTURE_REORDER'


@pytest.mark.parametrize(
    ('filters', 'sort', 'direction'),
    [
        (LifecycleWorkspaceFilters(inventory='MAYBE'), 'product', 'asc'),
        (LifecycleWorkspaceFilters(store='not-a-store'), 'product', 'asc'),
        (LifecycleWorkspaceFilters(), 'internal_id', 'asc'),
        (LifecycleWorkspaceFilters(), 'product', 'sideways'),
    ],
)
def test_filter_and_sort_allowlists_fail_closed(filters, sort, direction):
    with pytest.raises(ValueError):
        _query(filters=filters, sort=sort, direction=direction)


def test_template_and_script_limit_selection_to_rendered_rows_and_one_batch_confirmation():
    root = Path(__file__).resolve().parents[1]
    template = (root / 'app/templates/v2/ordering/lifecycle_products.html').read_text(encoding='utf-8')
    script = (root / 'app/static/v2/ordering-lifecycle.js').read_text(encoding='utf-8')
    assert 'data-select-all' in template
    assert 'from {{ rows|length }} visible' in template
    assert 'Select all filtered' not in template
    assert 'data-confirm-dialog' in template
    assert 'one atomic batch' in script
    assert "boxes.filter((box) => box.checked)" in script
    assert 'maxlength="1000"' in template
