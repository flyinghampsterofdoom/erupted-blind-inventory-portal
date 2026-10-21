from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import v2_ordering_data_coordinator as coordinator
from app.services.v2_ordering_policy_service import DataSourceEvidence
from app.services.v2_ordering_square_gateway import SquareOrderingReadResult, SquareProductMetadata, SquareStoreSkuData

NOW = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)


def test_paging_search_supplier_and_store_scope_preserve_full_results(monkeypatch):
    monkeypatch.setattr(coordinator.settings, 'purchasing_v3_phase1_enabled', False)
    monkeypatch.setattr(coordinator.settings, 'purchasing_v3_phase2_enabled', False)
    monkeypatch.setattr(coordinator, 'load_lifecycle_states', lambda *a: {})
    monkeypatch.setattr(coordinator, 'load_incoming_supply', lambda *a, **kw: [])
    monkeypatch.setattr(coordinator, '_math_settings', lambda *a: (5, 10, {}))
    mappings = [(SimpleNamespace(id=i, vendor_id=i % 2 + 1, sku=f'SKU{i:03}', square_variation_id=f'V{i}'),
                 SimpleNamespace(id=i % 2 + 1, name=f'Vendor {i % 2}')) for i in range(120)]

    class Rows:
        def __init__(self, rows): self.rows = rows
        def all(self): return self.rows
        def scalars(self): return self

    class Db:
        def __init__(self): self.results = iter([Rows([SimpleNamespace(id=i, name=f'Store {i}', square_location_id=f'L{i}') for i in (1, 2)]), Rows(mappings), Rows([])])
        def execute(self, query): return next(self.results)

    class Gateway:
        submitted = []
        def fetch_product_metadata(self, ids):
            return {vid: SquareProductMetadata(vid, 'SKU'+vid[1:].zfill(3), 'Product '+vid[1:].zfill(3), '', None, False) for vid in ids}
        def fetch(self, *, location_by_store, variation_ids, **kwargs):
            self.submitted.append(len(variation_ids))
            sources = tuple(DataSourceEvidence(name, NOW) for name in ('inventory', 'sales', 'stockout_history'))
            return SquareOrderingReadResult(self.fetch_product_metadata(variation_ids), {
                (sid, vid): SquareStoreSkuData(sid, vid, Decimal(10), True, (), (), sources, ())
                for sid in location_by_store for vid in variation_ids})

    gateway = Gateway()
    def build(query, stores=(1, 2)):
        return coordinator.build_ordering_dashboard(Db(), store_ids=stores, as_of=NOW, gateway=gateway, query=query)
    full = build(None)
    pages = [build({'page': str(page), 'sort': 'product'}) for page in range(1, 6)]
    assert all(page.paged_total == 240 for page in pages)
    assert [len(page.recommendations) for page in pages] == [50, 50, 50, 50, 40]
    key = lambda row: (row.store_id, row.variation_id)
    assert {key(row): row for page in pages for row in page.recommendations} == {key(row): row for row in full.recommendations}
    assert max(gateway.submitted[1:]) <= 25
    selected = build({'q': 'SKU001', 'supplier': 'Vendor 1', 'sort': 'product'}, stores=(2,))
    assert len(selected.recommendations) == 1 and selected.recommendations[0].store_id == 2
    assert build({'q': 'nonexistent', 'sort': 'product'}).recommendations == ()
    assert build({'page': '999', 'sort': 'product'}).page_number == 5


def test_lazy_details_require_same_feature_capability_and_authorized_store(monkeypatch):
    from app.main import app
    from app.routers import v2_ordering as route
    detail = next(r for r in app.routes if getattr(r, 'path', '') == '/v2/ordering/details/{store_id}/{variation_id}')
    calls = [d.call for d in detail.dependant.dependencies]
    assert route.feature_access in calls and route.ordering_access in calls
    monkeypatch.setattr(route, 'list_authorized_stores', lambda *a: [SimpleNamespace(id=1)])
    monkeypatch.setattr(route, 'build_ordering_dashboard', lambda *a, **kw: pytest.fail('Unauthorized history read'))
    with pytest.raises(HTTPException) as denied:
        route.ordering_recommendation_details(2, 'V1', None, None, None, None)
    assert denied.value.status_code == 403
