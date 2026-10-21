from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import v2_ordering_catalog_service as service
from app.services.v2_ordering_square_gateway import (
    SquareCatalogReadResult,
    SquareOrderingReadMetrics,
    SquareProductMetadata,
)


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Db:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.added = []
        self.flush_count = 0

    def execute(self, _statement):
        return _Result(self.existing)

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flush_count += 1


def _state():
    return SimpleNamespace(
        last_result='NEVER', expected_mapped_count=0, covered_mapped_count=0,
        missing_mapped_count=0, last_attempted_at=None, last_successful_at=None,
        last_error=None, last_refreshed_by_principal_id=None,
    )


def _patch_contract(monkeypatch, state, coverage_values):
    monkeypatch.setattr(service, '_mapped_variation_ids', lambda _db: ('VAR-1', 'VAR-2'))
    monkeypatch.setattr(service, '_requested_variation_ids', lambda _db, mapped: mapped)
    monkeypatch.setattr(service, '_refresh_state', lambda _db: state)
    values = iter(coverage_values)
    monkeypatch.setattr(service, '_coverage_count', lambda _db, _ids: next(values))
    monkeypatch.setattr(service, 'write_v2_audit_event', lambda *_args, **_kwargs: None)


def test_complete_refresh_uses_bulk_catalog_result_and_updates_ordering_owned_rows(monkeypatch):
    state = _state()
    _patch_contract(monkeypatch, state, [2])
    products = {
        variation_id: SquareProductMetadata(
            variation_id, f'SKU-{index}', 'Product', str(index), NOW, False,
            item_id=f'ITEM-{index}', updated_at=NOW,
        )
        for index, variation_id in enumerate(('VAR-1', 'VAR-2'), 1)
    }
    gateway = SimpleNamespace(fetch_catalog_identity=lambda _ids: SquareCatalogReadResult(
        products,
        SquareOrderingReadMetrics(
            request_count=3,
            endpoint_request_counts=(('/v2/catalog/search-catalog-items', 3),),
        ),
    ))
    db = _Db()

    result = service.refresh_ordering_catalog_identity(
        db, actor_principal_id=6, ip='127.0.0.1', gateway=gateway, attempted_at=NOW
    )

    assert result.outcome == 'COMPLETE'
    assert (result.square_request_count, result.square_page_count) == (3, 3)
    assert state.last_successful_at == NOW
    assert {row.square_variation_id for row in db.added} == {'VAR-1', 'VAR-2'}
    assert {row.product_name for row in db.added} == {'Product — 1', 'Product — 2'}


def test_partial_refresh_preserves_existing_rows_and_does_not_claim_complete(monkeypatch):
    state = _state()
    state.last_successful_at = datetime(2026, 7, 24, tzinfo=timezone.utc)
    _patch_contract(monkeypatch, state, [2])
    existing = SimpleNamespace(
        square_variation_id='VAR-1', square_item_id='OLD-ITEM', sku='OLD-SKU',
        item_name='Known', variation_name='Name', product_name='Known — Name',
        square_is_deleted=False, square_updated_at=NOW, last_seen_at=NOW,
    )
    product = SquareProductMetadata('VAR-1', '', '', '', NOW, False)
    gateway = SimpleNamespace(fetch_catalog_identity=lambda _ids: SquareCatalogReadResult(
        {'VAR-1': product},
        SquareOrderingReadMetrics(
            request_count=1,
            endpoint_request_counts=(('/v2/catalog/search-catalog-items', 1),),
        ),
    ))

    result = service.refresh_ordering_catalog_identity(
        _Db([existing]), actor_principal_id=6, ip=None, gateway=gateway, attempted_at=NOW
    )

    assert result.outcome == 'PARTIAL'
    assert state.last_successful_at == datetime(2026, 7, 24, tzinfo=timezone.utc)
    assert existing.product_name == 'Known — Name'
    assert existing.sku == 'OLD-SKU'


def test_failed_refresh_preserves_prior_snapshot_and_records_failed_state(monkeypatch):
    state = _state()
    state.last_successful_at = datetime(2026, 7, 24, tzinfo=timezone.utc)
    _patch_contract(monkeypatch, state, [1])

    class FailedGateway:
        def fetch_catalog_identity(self, _ids):
            raise RuntimeError('Square catalog unavailable')

        def current_metrics(self):
            return SquareOrderingReadMetrics(
                request_count=1,
                endpoint_request_counts=(('/v2/catalog/search-catalog-items', 1),),
            )

    db = _Db()
    result = service.refresh_ordering_catalog_identity(
        db, actor_principal_id=6, ip=None, gateway=FailedGateway(), attempted_at=NOW
    )

    assert result.outcome == 'FAILED'
    assert result.covered_mapped_count == 1
    assert (result.square_request_count, result.square_page_count) == (1, 1)
    assert state.last_result == 'FAILED'
    assert state.last_successful_at == datetime(2026, 7, 24, tzinfo=timezone.utc)
    assert db.added == []


def test_ordering_catalog_service_has_no_touchscreen_dependency():
    source = __import__('pathlib').Path(service.__file__).read_text(encoding='utf-8').lower()
    assert 'touchscreensquarevariationcache' not in source.replace('_', '')
    assert 'touchscreenstoreinventorycache' not in source.replace('_', '')
    assert 'touchscreen_' not in source
