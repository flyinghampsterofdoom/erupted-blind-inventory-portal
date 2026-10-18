"""Bounded preservation checks. All data and S3 responses are synthetic/local."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Lock, Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote
import zipfile

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from reportlab.pdfgen.canvas import Canvas

from app.config import settings
from app.models import PurchaseOrder, PurchaseOrderLine
from app.services import purchase_order_pdf_storage as storage
from app.services import purchase_order_admin_service as orders
from scripts.purchase_order_pdf_backup import backup, restore, verify
from test_purchasing_receiving_extension import database_template, receiving_db
from test_purchasing_ui import app_client

ROOT = Path(__file__).resolve().parents[1]


def document(label='Synthetic original'):
    stream = BytesIO()
    canvas = Canvas(stream, invariant=1)
    canvas.drawString(42, 740, label)
    canvas.save()
    return stream.getvalue()


def test_new_document_retries_do_not_replace_and_every_byte_is_verified(tmp_path):
    content = document()
    reference = storage.store_pdf(content, purchase_order_id=1, root=ROOT)
    path = storage.read_pdf(reference, root=ROOT, purchase_order_id=1)
    inode = path.stat().st_ino
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(lambda _: storage.store_pdf(content, purchase_order_id=1, root=ROOT), range(8))) == {reference}
    assert path.stat().st_ino == inode
    # Even valid-looking PDFs with one changed byte must be rejected.
    path.write_bytes(content.replace(b'Synthetic', b'Different', 1) if b'Synthetic' in content else content.replace(b'ReportLab', b'ReportLax', 1))
    with pytest.raises(storage.OriginalPdfUnavailable, match='Original PDF Unavailable'):
        storage.read_pdf(reference, root=ROOT)
    with pytest.raises(storage.OriginalPdfUnavailable):
        storage.store_pdf(content, purchase_order_id=1, root=ROOT)
    with pytest.raises(storage.OriginalPdfUnavailable):
        storage.read_pdf(reference, root=ROOT, purchase_order_id=2)


@pytest.mark.parametrize('content', [b'', b'%PDF-1.4 broken', b'%PDF-1.4\n/Type /Catalog /Type /Page\nstartxref\n0\n%%EOF'])
def test_corrupted_legacy_document_is_unavailable(tmp_path, content):
    path = Path(settings.purchase_order_pdf_local_directory) / 'purchase_order_1_old.pdf'
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with pytest.raises(storage.OriginalPdfUnavailable):
        storage.read_pdf(storage.PREFIX + path.name, root=ROOT)


@pytest.mark.parametrize('reference', ['../secrets.pdf', '/tmp/secret.pdf', 'generated/purchase_orders/../secret.pdf', 'generated/purchase_orders/x/y.pdf'])
def test_invalid_paths_fail_closed(reference):
    with pytest.raises(storage.OriginalPdfUnavailable):
        storage.read_pdf(reference, root=ROOT)


def test_backup_independently_restores_exact_originals_and_lists_missing(tmp_path, monkeypatch):
    content = document()
    reference = storage.store_pdf(content, purchase_order_id=1, root=ROOT)
    missing = storage.PREFIX + 'purchase_order_99_historical.pdf'
    archive = tmp_path / 'backup.zip'
    manifest = backup([reference, missing], archive)
    assert [r['original_file_backed_up'] for r in manifest['documents']] == [True, False]
    assert verify(archive) == manifest
    with pytest.raises(FileExistsError):
        backup([reference], archive)
    target = tmp_path / 'independent-restore'
    restore(archive, target)
    assert (target / reference).read_bytes() == content
    monkeypatch.setattr(settings, 'purchase_order_pdf_local_directory', str(target / storage.PREFIX))
    assert storage.read_pdf(reference, root=tmp_path / 'replacement-application').read_bytes() == content
    assert not (target / missing).exists()
    with pytest.raises(FileExistsError):
        restore(archive, target)
    bad = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(bad, 'w') as output:
        for name in source.namelist():
            output.writestr(name, source.read(name) + b'corrupt' if name == reference else source.read(name))
    with pytest.raises((ValueError, storage.OriginalPdfUnavailable)):
        restore(bad, tmp_path / 'must-not-exist')
    assert not (tmp_path / 'must-not-exist').exists()


def test_historical_download_never_regenerates_or_updates_even_if_stale(receiving_db, monkeypatch):
    from app.routers import management
    with Session(receiving_db) as db:
        po = db.get(PurchaseOrder, 1)
        po.pdf_path = storage.PREFIX + 'purchase_order_1_historical.pdf'
        db.commit()
        before = dict(db.execute(text('SELECT * FROM purchase_orders WHERE id=1')).mappings().one())
        def forbidden(*a, **kw):
            raise AssertionError('A read attempted generation or commit')
        monkeypatch.setattr(orders, '_generate_purchase_order_pdf', forbidden)
        client, app = app_client(db); app.include_router(management.router)
        monkeypatch.setattr(db, 'commit', forbidden)
        response = client.get('/management/ordering-tool/orders/1/pdf')
        assert response.status_code == 404 and response.json()['detail'] == 'Original PDF Unavailable'
        assert dict(db.execute(text('SELECT * FROM purchase_orders WHERE id=1')).mappings().one()) == before
        assert not db.dirty and not db.new and not db.deleted
        # An original recovered later is retrievable at the same exact path.
        target = Path(settings.purchase_order_pdf_local_directory) / Path(po.pdf_path).name
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(document('Recovered synthetic original'))
        db.execute(text("UPDATE purchase_orders SET submitted_at='2026-10-01', updated_at='2026-10-18' WHERE id=1"))
        db.flush(); db.expire_all()
        response = client.get('/management/ordering-tool/orders/1/pdf')
        assert response.status_code == 200 and response.content == target.read_bytes()
        assert db.get(PurchaseOrder, 1).pdf_path == before['pdf_path']


def test_generation_preserves_existing_reference_and_missing_original(receiving_db, monkeypatch):
    with Session(receiving_db) as db:
        po = db.get(PurchaseOrder, 1)
        po.pdf_path = orders._generate_purchase_order_pdf(db, purchase_order_id=1)
        db.commit()
        reference = po.pdf_path
        path = orders.ensure_current_purchase_order_pdf(db, purchase_order_id=1)
        original = path.read_bytes()
        db.get(PurchaseOrderLine, 1).ordered_qty = 999
        assert orders._generate_purchase_order_pdf(db, purchase_order_id=1) == reference
        assert path.read_bytes() == original
        path.unlink()
        with pytest.raises(storage.OriginalPdfUnavailable):
            orders._generate_purchase_order_pdf(db, purchase_order_id=1)
        assert po.pdf_path == reference


def test_storage_failure_does_not_publish_reference_or_submit_order(receiving_db, monkeypatch):
    monkeypatch.setattr(settings, 'purchase_order_pdf_storage', 'r2')
    monkeypatch.setattr(settings, 'r2_access_key_id', None)
    with Session(receiving_db) as db:
        po = db.get(PurchaseOrder, 1); po.status = 'DRAFT'; db.commit()
        with pytest.raises(storage.PdfStorageUnavailable):
            orders.submit_purchase_order(db, purchase_order_id=1, actor_principal_id=1)
        db.rollback(); db.expire_all()
        assert db.get(PurchaseOrder, 1).status == 'DRAFT'
        assert db.get(PurchaseOrder, 1).pdf_path is None


def test_production_cannot_fall_back_to_ephemeral_directory(monkeypatch):
    monkeypatch.setattr(settings, 'environment', 'production')
    with pytest.raises(storage.PdfStorageUnavailable):
        storage.store_pdf(document(), purchase_order_id=1, root=ROOT)


@pytest.fixture
def local_s3(tmp_path, monkeypatch):
    """File-backed local HTTP S3 fixture; real boto3 requests, no cloud access.

    Deliberately scoped to GetObject/conditional PutObject. This does not claim
    to verify actual R2 IAM, Cloudflare durability or a hosted Render restart.
    """
    objects = tmp_path / 'mock-s3-volume'; objects.mkdir()
    lock = Lock(); requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_PUT(self):
            key = unquote(self.path.split('?', 1)[0]).lstrip('/')
            assert key.startswith('isolated-pdf-test/generated/purchase_orders/')
            content = self.rfile.read(int(self.headers['Content-Length']))
            path = objects / key
            with lock:
                requests.append(('PUT', self.headers.get('If-None-Match')))
                if path.exists():
                    self.send_response(412); self.send_header('Content-Length', '0'); self.end_headers(); return
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
            self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
        def do_GET(self):
            path = objects / unquote(self.path.split('?', 1)[0]).lstrip('/')
            requests.append(('GET', None))
            if not path.is_file():
                content = b'<Error><Code>NoSuchKey</Code></Error>'; self.send_response(404)
            else:
                content = path.read_bytes(); self.send_response(200)
            self.send_header('Content-Length', str(len(content))); self.end_headers(); self.wfile.write(content)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    for key, value in {'purchase_order_pdf_storage':'r2', 'r2_endpoint_url':f'http://127.0.0.1:{server.server_port}',
                       'r2_bucket_name':'isolated-pdf-test', 'r2_access_key_id':'local-fixture', 'r2_secret_access_key':'local-fixture'}.items():
        monkeypatch.setattr(settings, key, value)
    try:
        yield objects, requests
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_web_cron_replacement_processes_share_authority_and_restore(local_s3, tmp_path, monkeypatch):
    objects, requests = local_s3
    content = document()
    reference = storage.store_pdf(content, purchase_order_id=7, root=ROOT)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(lambda _: storage.store_pdf(content, purchase_order_id=7, root=ROOT), range(8))) == {reference}
    assert all(condition == '*' for method, condition in requests if method == 'PUT')
    env = {**os.environ, 'ENVIRONMENT':'test', 'PURCHASE_ORDER_PDF_STORAGE':'r2',
           'R2_ENDPOINT_URL':settings.r2_endpoint_url, 'R2_BUCKET_NAME':settings.r2_bucket_name,
           'R2_ACCESS_KEY_ID':'local-fixture', 'R2_SECRET_ACCESS_KEY':'local-fixture'}
    for runtime in ('web-before-restart', 'web-after-restart', 'replacement-deployment', 'cron'):
        env['PURCHASE_ORDER_PDF_CACHE_DIRECTORY'] = str(tmp_path / runtime)
        program = 'from pathlib import Path; from app.services.purchase_order_pdf_storage import read_pdf; import hashlib; print(hashlib.sha256(read_pdf('+repr(reference)+', root=Path(".")).read_bytes()).hexdigest())'
        result = subprocess.run([sys.executable, '-c', program], cwd=ROOT, env=env, text=True, capture_output=True, check=True)
        assert result.stdout.strip() == hashlib.sha256(content).hexdigest()
    archive = tmp_path / 'r2-backup.zip'; backup([reference], archive)
    restore(archive, tmp_path / 'r2-independent-restore')
    assert (tmp_path / 'r2-independent-restore' / reference).read_bytes() == content
    # Remote corruption/missing original cannot be hidden by a valid old cache.
    remote = objects / 'isolated-pdf-test' / reference
    remote.write_bytes(content[:-20])
    with pytest.raises(storage.OriginalPdfUnavailable):
        storage.read_pdf(reference, root=ROOT)
    remote.unlink()
    with pytest.raises(storage.OriginalPdfUnavailable):
        storage.read_pdf(reference, root=ROOT)


def test_database_dump_restore_preserves_pdf_references_without_backing_up_files(receiving_db, database_template, tmp_path):
    """Real PostgreSQL custom dump/restore, with synthetic records only."""
    import shutil
    from uuid import uuid4
    from sqlalchemy import create_engine
    dump = shutil.which('pg_dump'); restore_bin = shutil.which('pg_restore')
    if not dump or not restore_bin:
        pytest.skip('PostgreSQL backup binaries required')
    with Session(receiving_db) as db:
        po = db.get(PurchaseOrder, 1)
        po.pdf_path = orders._generate_purchase_order_pdf(db, purchase_order_id=1)
        db.commit()
        expected = [(1, po.pdf_path)]
    archive = tmp_path / 'synthetic-db.pgcustom'
    url = receiving_db.url.set(drivername='postgresql').render_as_string(hide_password=False)
    subprocess.run([dump, '--format=custom', '--file', str(archive), url], check=True, capture_output=True)
    admin, template_url, _ = database_template
    name = 'erupted_purchasing_p1_pdf_restore_' + uuid4().hex
    with admin.connect() as con:
        con.execute(text(f'CREATE DATABASE "{name}"'))
    restored = create_engine(template_url.set(database=name))
    try:
        target = template_url.set(database=name, drivername='postgresql').render_as_string(hide_password=False)
        result = subprocess.run([restore_bin, '--exit-on-error', '--no-owner', '--dbname', target, str(archive)], text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        with restored.connect() as con:
            assert list(con.execute(text('SELECT id,pdf_path FROM purchase_orders ORDER BY id'))) == expected
        # SQL backups contain references only. File restoration is separate.
        listing = subprocess.run([restore_bin, '--list', str(archive)], text=True, capture_output=True, check=True).stdout
        assert ' BLOB ' not in listing and 'purchase_orders' in listing
    finally:
        restored.dispose()
        with admin.connect() as con:
            con.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def test_concurrent_submissions_with_stale_identity_maps_publish_one_original(receiving_db, monkeypatch):
    monkeypatch.setattr('app.services.v2_order_payments_service.initialize_new_order_if_configured', lambda *a, **kw: None)
    with Session(receiving_db) as db:
        db.get(PurchaseOrder, 1).status = 'DRAFT'
        db.commit()
    barrier = Barrier(2)
    def submit():
        with Session(receiving_db) as db:
            # Both callers deliberately have an old DRAFT in their identity map.
            po = db.get(PurchaseOrder, 1)
            assert po.status == 'DRAFT'
            barrier.wait(timeout=10)
            try:
                result = orders.submit_purchase_order(db, purchase_order_id=1, actor_principal_id=1)
                reference = result.pdf_path
                db.commit()
                return reference
            except ValueError as exc:
                db.rollback()
                return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: submit(), range(2)))
    assert outcomes.count('Only draft orders can be submitted') == 1
    with Session(receiving_db) as db:
        po = db.get(PurchaseOrder, 1)
        assert po.pdf_path in outcomes and po.status == 'IN_TRANSIT'
        assert len(list(Path(settings.purchase_order_pdf_local_directory).glob('*.pdf'))) == 1
        assert orders.ensure_current_purchase_order_pdf(db, purchase_order_id=1).read_bytes().startswith(b'%PDF-')


def test_remote_access_failure_is_503_and_never_serves_stale_cache(receiving_db, monkeypatch):
    from botocore.exceptions import ClientError
    from app.routers import management
    with Session(receiving_db) as db:
        po = db.get(PurchaseOrder, 1)
        po.pdf_path = orders._generate_purchase_order_pdf(db, purchase_order_id=1)
        db.commit()
        reference = po.pdf_path
        class Denied:
            def get_object(self, **kwargs):
                raise ClientError({'Error': {'Code': 'AccessDenied'}}, 'GetObject')
        monkeypatch.setattr(settings, 'purchase_order_pdf_storage', 'r2')
        monkeypatch.setattr(storage, '_r2', lambda: (Denied(), 'private-test'))
        client, app = app_client(db); app.include_router(management.router)
        response = client.get('/management/ordering-tool/orders/1/pdf')
        assert response.status_code == 503
        assert 'storage unavailable' in response.json()['detail']
        assert po.pdf_path == reference and not db.dirty


@pytest.mark.skipif(os.getenv('RUN_REAL_PO_PDF_R2_TESTS') != '1', reason='Requires explicit isolated PO_PDF_TEST_R2_* credentials; production bucket is forbidden')
def test_real_isolated_private_r2_pdf_preservation(tmp_path, monkeypatch):
    """Opt-in hosted storage gate; never falls back to production credentials."""
    from uuid import uuid4
    fields = ('endpoint_url', 'bucket_name', 'access_key_id', 'secret_access_key')
    values = {field: os.environ.get('PO_PDF_TEST_R2_' + field.upper(), '') for field in fields}
    assert all(values.values()), 'All dedicated PO_PDF_TEST_R2_* values are required'
    assert values['bucket_name'] != settings.r2_bucket_name, 'Do not use the application production bucket'
    for field, value in values.items():
        monkeypatch.setattr(settings, 'r2_' + field, value)
    monkeypatch.setattr(settings, 'r2_region', 'auto')
    monkeypatch.setattr(settings, 'purchase_order_pdf_storage', 'r2')
    content = document('Isolated private R2 verification ' + uuid4().hex)
    reference = storage.store_pdf(content, purchase_order_id=9001, root=ROOT)
    assert storage.store_pdf(content, purchase_order_id=9001, root=ROOT) == reference
    for process in ('web', 'restart', 'replacement', 'cron'):
        monkeypatch.setattr(settings, 'purchase_order_pdf_cache_directory', str(tmp_path / process))
        assert storage.read_pdf(reference, root=ROOT, purchase_order_id=9001).read_bytes() == content
    archive = tmp_path / 'private-r2-backup.zip'
    backup([reference], archive)
    restore(archive, tmp_path / 'independent-r2-restore')
    assert (tmp_path / 'independent-r2-restore' / reference).read_bytes() == content
    # Keep the synthetic test artifact in the dedicated test bucket for evidence;
    # application/storage code has no delete or historical-overwrite capability.
