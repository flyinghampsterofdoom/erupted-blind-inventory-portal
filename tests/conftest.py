"""PDF writers in tests use an explicit disposable directory, never configured R2."""
import pytest
from app.config import settings


@pytest.fixture(autouse=True)
def isolated_po_pdf_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'environment', 'test')
    monkeypatch.setattr(settings, 'purchase_order_pdf_storage', 'filesystem')
    monkeypatch.setattr(settings, 'purchase_order_pdf_local_directory', str(tmp_path / 'generated' / 'purchase_orders'))
    monkeypatch.setattr(settings, 'purchase_order_pdf_cache_directory', str(tmp_path / 'cache'))
