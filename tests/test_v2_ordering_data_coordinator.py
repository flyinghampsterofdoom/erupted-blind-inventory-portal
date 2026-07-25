from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.models import OrderingProductLifecycle, ParLevelSource
from app.services.v2_ordering_data_coordinator import build_ordering_dashboard
from app.services.v2_ordering_normalization_service import DailyQuantity
from app.services.v2_ordering_policy_service import DataSourceEvidence
from app.services.v2_ordering_square_gateway import (
    SquareOrderingReadResult,
    SquareProductMetadata,
    SquareStoreSkuData,
)


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


class _Result:
    def __init__(self, *, rows=(), scalars=(), scalar=None):
        self.rows = list(rows)
        self.scalar_rows = list(scalars)
        self.scalar = scalar

    def all(self):
        return self.rows

    def scalars(self):
        return _Result(rows=self.scalar_rows)

    def scalar_one_or_none(self):
        return self.scalar


class _ReadOnlyDb:
    def __init__(self, results):
        self.results = iter(results)

    def execute(self, _query):
        return next(self.results)


class _Gateway:
    def fetch(self, *, location_by_store, variation_ids, as_of):
        assert location_by_store == {1: 'LOC-1'}
        assert variation_ids == ['VAR-1']
        end = as_of.date() - timedelta(days=1)
        days = tuple(DailyQuantity(end - timedelta(days=offset), Decimal('1')) for offset in range(56))
        data = SquareStoreSkuData(
            store_id=1,
            variation_id='VAR-1',
            current_on_hand=Decimal('10'),
            inventory_valid=True,
            daily_sales=days,
            daily_inventory_deltas=(),
            required_sources=(
                DataSourceEvidence('inventory', as_of),
                DataSourceEvidence('sales', as_of),
                DataSourceEvidence('stockout_history', as_of),
            ),
            warnings=(),
        )
        return SquareOrderingReadResult(
            products={
                'VAR-1': SquareProductMetadata('VAR-1', 'SKU-1', 'Item', 'Variation', NOW - timedelta(days=100), False)
            },
            by_store_variation={(1, 'VAR-1'): data},
        )


def test_coordinator_reads_v1_facts_without_any_write_method():
    mapping = SimpleNamespace(
        id=4,
        vendor_id=2,
        sku='SKU-1',
        square_variation_id='VAR-1',
        created_at=NOW - timedelta(days=100),
    )
    vendor = SimpleNamespace(id=2, name='Vendor')
    par = SimpleNamespace(
        vendor_id=2,
        store_id=1,
        sku='SKU-1',
        manual_par_level=35,
        manual_stock_up_level=70,
        locked_manual=True,
        par_source=ParLevelSource.MANUAL,
    )
    incoming = SimpleNamespace(
        id=9,
        vendor_id=2,
        ordered_at=NOW - timedelta(days=3),
        submitted_at=None,
        created_at=NOW - timedelta(days=4),
        sku='SKU-1',
        store_id=1,
        allocated_qty=5,
    )
    db = _ReadOnlyDb(
        [
            _Result(rows=[SimpleNamespace(id=1, name='HWY99', square_location_id='LOC-1')]),
            _Result(rows=[(mapping, vendor)]),
            _Result(scalars=[]),
            _Result(scalars=[par]),
            _Result(scalar=SimpleNamespace(default_reorder_weeks=5, default_stock_up_weeks=10)),
            _Result(scalars=[]),
            _Result(rows=[incoming]),
        ]
    )
    dashboard = build_ordering_dashboard(db, store_ids=(1,), gateway=_Gateway(), as_of=NOW)
    assert len(dashboard.recommendations) == 1
    row = dashboard.recommendations[0]
    assert row.incoming_supply == 5
    assert row.calculated_quantity == 55
    assert row.store_id == 1
    assert row.applied_policies[-1] == 'P1-POL-016'


def test_archived_variations_are_filtered_before_square_and_nfr_remains_visible():
    active_mapping = SimpleNamespace(id=4, vendor_id=2, sku='SKU-1', square_variation_id='VAR-1')
    archived_mapping = SimpleNamespace(id=5, vendor_id=2, sku='SKU-2', square_variation_id='VAR-2')
    vendor = SimpleNamespace(id=2, name='Vendor')
    nfr = OrderingProductLifecycle(
        square_variation_id='VAR-1', status='NO_FUTURE_REORDER', row_version=1,
        no_future_reorder_at=NOW, no_future_reorder_by_principal_id=4,
    )
    archived = OrderingProductLifecycle(
        square_variation_id='VAR-2', status='ARCHIVED', row_version=1,
        archived_at=NOW, archived_by_principal_id=4, pre_archive_status='ACTIVE',
    )
    db = _ReadOnlyDb([
        _Result(rows=[SimpleNamespace(id=1, name='HWY99', square_location_id='LOC-1')]),
        _Result(rows=[(active_mapping, vendor), (archived_mapping, vendor)]),
        _Result(scalars=[nfr, archived]),
        _Result(scalars=[]),
        _Result(scalar=SimpleNamespace(default_reorder_weeks=5, default_stock_up_weeks=10)),
        _Result(scalars=[]),
        _Result(rows=[]),
    ])
    dashboard = build_ordering_dashboard(db, store_ids=(1,), gateway=_Gateway(), as_of=NOW)
    assert len(dashboard.recommendations) == 1
    result = dashboard.recommendations[0]
    assert result.variation_id == 'VAR-1'
    assert result.lifecycle_status == 'NO_FUTURE_REORDER'
    assert result.calculated_quantity is None
    assert dashboard.metrics.archived_variation_count == 1
    assert dashboard.metrics.no_future_reorder_count == 1


def test_empty_lifecycle_has_identical_square_population_and_active_results():
    mapping = SimpleNamespace(id=4, vendor_id=2, sku='SKU-1', square_variation_id='VAR-1')
    vendor = SimpleNamespace(id=2, name='Vendor')

    def database(lifecycle_rows):
        return _ReadOnlyDb([
            _Result(rows=[SimpleNamespace(id=1, name='HWY99', square_location_id='LOC-1')]),
            _Result(rows=[(mapping, vendor)]),
            _Result(scalars=lifecycle_rows),
            _Result(scalars=[]),
            _Result(scalar=SimpleNamespace(default_reorder_weeks=5, default_stock_up_weeks=10)),
            _Result(scalars=[]),
            _Result(rows=[]),
        ])

    implicit = build_ordering_dashboard(database([]), store_ids=(1,), gateway=_Gateway(), as_of=NOW)
    explicit = build_ordering_dashboard(
        database([OrderingProductLifecycle(square_variation_id='VAR-1', status='ACTIVE', row_version=1)]),
        store_ids=(1,),
        gateway=_Gateway(),
        as_of=NOW,
    )
    assert implicit.recommendations == explicit.recommendations
    assert len(implicit.recommendations) == len(explicit.recommendations) == 1
    assert implicit.metrics.active_variation_count == explicit.metrics.active_variation_count == 1
    assert implicit.metrics.archived_variation_count == explicit.metrics.archived_variation_count == 0
