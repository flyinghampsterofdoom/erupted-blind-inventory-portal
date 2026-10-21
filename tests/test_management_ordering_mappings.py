import asyncio
import json
from types import SimpleNamespace

from app.routers import management


class _Request:
    def __init__(self, form):
        self._form = form
        self.headers = {}
        self.client = None

    async def form(self):
        return self._form


class _Db:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


def test_vendor_mapping_bulk_save_accepts_many_rows_in_one_compact_field(monkeypatch):
    rows = [
        {
            'id': row_id,
            'vendor_id': 7,
            'sku': f'SKU-{row_id}',
            'square_variation_id': f'variation-{row_id}',
            'unit_cost': '2.50',
            'pack_size': '1',
            'min_order_qty': '0',
            'is_default_vendor': 'true',
            'active': 'false',
        }
        for row_id in range(1, 201)
    ]
    saved_rows = []
    monkeypatch.setattr(management, 'upsert_vendor_sku_config', lambda _db, **values: saved_rows.append(values))
    monkeypatch.setattr(management, 'log_audit', lambda *_args, **_kwargs: None)
    db = _Db()

    response = asyncio.run(
        management.ordering_tool_mappings_bulk_save(
            _Request({'csrf_token': 'token', 'rows_json': json.dumps(rows)}),
            principal=SimpleNamespace(id=3),
            db=db,
        )
    )

    assert response.status_code == 303
    assert response.headers['location'].endswith('bulk_saved=200&bulk_errors=0')
    assert len(saved_rows) == 200
    assert all(row['active'] is False for row in saved_rows)
    assert db.committed is True
