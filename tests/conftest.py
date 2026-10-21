"""PDF writers in tests use an explicit disposable directory, never configured R2."""
import pytest
from app.config import settings


@pytest.fixture(autouse=True)
def isolated_po_pdf_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'environment', 'test')
    monkeypatch.setattr(settings, 'purchase_order_pdf_storage', 'filesystem')
    monkeypatch.setattr(settings, 'purchase_order_pdf_local_directory', str(tmp_path / 'generated' / 'purchase_orders'))
    monkeypatch.setattr(settings, 'purchase_order_pdf_cache_directory', str(tmp_path / 'cache'))


@pytest.fixture
def hosted_r2_storage(monkeypatch, tmp_path, request):
    """Explicit hosted tests use a unique confined prefix, never live object keys."""
    from hosted_r2_support import configured_hosted_storage
    remote = configured_hosted_storage(monkeypatch, tmp_path)
    try:
        yield remote
    finally:
        remote.report('request-audit-' + request.node.name, {'scope': 'isolated hosted test prefix only'})


@pytest.fixture(autouse=True)
def prohibit_unmocked_square_network(monkeypatch):
    """An obsolete mock must never turn an isolated test into a live Square call."""
    from urllib.request import OpenerDirector
    from urllib.parse import urlsplit
    original=OpenerDirector.open
    def guarded(self, fullurl, *args, **kwargs):
        url=getattr(fullurl,'full_url',str(fullurl))
        if urlsplit(url).hostname in {'connect.squareup.com','connect.squareupsandbox.com'}:
            raise AssertionError('Unmocked Square network access prohibited in isolated tests')
        return original(self,fullurl,*args,**kwargs)
    monkeypatch.setattr(OpenerDirector,'open',guarded)
