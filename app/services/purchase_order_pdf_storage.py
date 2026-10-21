"""Immutable PO documents using the existing private R2 infrastructure.

Database paths remain unchanged. Local files are a cache in production, never
an authoritative replacement for a missing content-addressed R2 original.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.services.digital_signage_storage import R2ObjectStorage

PREFIX = 'generated/purchase_orders/'
NEW_NAME = re.compile(r'purchase_order_(\d+)_sha256_([0-9a-f]{64})\.pdf\Z')


class OriginalPdfUnavailable(ValueError):
    """No substitution or database repair is permitted from a download."""

    def __init__(self, reason: str = 'missing'):
        super().__init__('Original PDF Unavailable')
        self.reason = reason


class PdfStorageUnavailable(ValueError):
    def __init__(self):
        super().__init__('Purchase-order PDF storage unavailable; retry later or contact an administrator')


def _name(reference: str) -> str:
    path = PurePosixPath(reference)
    if (not reference.startswith(PREFIX) or len(path.parts) != 3
            or str(path) != reference or path.suffix != '.pdf'
            or '\\' in reference or '..' in path.parts):
        raise OriginalPdfUnavailable('invalid reference')
    return path.name


def _directory(root: Path) -> Path:
    if settings.purchase_order_pdf_storage == 'filesystem':
        # Explicit local fixtures only. A checkout directory cannot become a
        # production durability guarantee by setting a flag.
        if settings.environment_normalized not in {'development', 'test', 'local'}:
            raise PdfStorageUnavailable()
        value = settings.purchase_order_pdf_local_directory
        if not value or not Path(value).is_absolute():
            raise PdfStorageUnavailable()
        return Path(value)
    if settings.purchase_order_pdf_storage != 'r2':
        raise PdfStorageUnavailable()
    return Path(settings.purchase_order_pdf_cache_directory) if settings.purchase_order_pdf_cache_directory else root / PREFIX


def _r2():
    values = (settings.r2_endpoint_url, settings.r2_bucket_name,
              settings.r2_access_key_id, settings.r2_secret_access_key)
    if not all(str(value or '').strip() for value in values):
        raise PdfStorageUnavailable()
    # Same endpoint/client/credentials as the existing private media adapter;
    # PO keys use a separate prefix and are never public or presigned.
    storage = R2ObjectStorage(*values, region=settings.r2_region)
    return storage._client(), storage.bucket_name


def validate_pdf(reference: str, content: bytes, *, purchase_order_id: int | None = None) -> None:
    name = _name(reference)
    match = NEW_NAME.fullmatch(name)
    if '_sha256_' in name and match is None:
        raise OriginalPdfUnavailable('invalid integrity reference')
    if match:
        if purchase_order_id is not None and int(match[1]) != purchase_order_id:
            raise OriginalPdfUnavailable('PO association mismatch')
        if hashlib.sha256(content).hexdigest() != match[2]:
            raise OriginalPdfUnavailable('checksum mismatch')
    # Legacy originals have no historical checksum to compare. Never invent one
    # as evidence of authenticity. Reject obvious truncation/invalid structure.
    if not content.startswith(b'%PDF-') or not content.rstrip().endswith(b'%%EOF'):
        raise OriginalPdfUnavailable('invalid or truncated PDF')
    matches = list(re.finditer(rb'startxref\s+(\d+)\s+%%EOF', content))
    if (not matches or content[int(matches[-1][1]):][:4] != b'xref'
            or b'/Type /Catalog' not in content or b'/Type /Page' not in content):
        raise OriginalPdfUnavailable('invalid PDF structure')


def _cache(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.po-pdf-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def read_pdf(reference: str, *, root: Path, purchase_order_id: int | None = None) -> Path:
    name = _name(reference)
    directory = _directory(root)
    path = directory / name
    modern = NEW_NAME.fullmatch(name) is not None
    # Historical files recovered at their original location remain readable.
    # Modern documents always check R2, even if an old cache survived a deploy.
    if settings.purchase_order_pdf_storage == 'filesystem' or (not modern and path.is_file()):
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise OriginalPdfUnavailable() from exc
        validate_pdf(reference, content, purchase_order_id=purchase_order_id)
        return path
    try:
        client, bucket = _r2()
        response = client.get_object(Bucket=bucket, Key=reference)
        with response['Body'] as stream:
            content = stream.read()
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') in {'NoSuchKey', '404', 'NotFound'}:
            raise OriginalPdfUnavailable() from exc
        raise PdfStorageUnavailable() from exc
    except BotoCoreError as exc:
        raise PdfStorageUnavailable() from exc
    validate_pdf(reference, content, purchase_order_id=purchase_order_id)
    return _cache(path, content)


def store_pdf(content: bytes, *, purchase_order_id: int, root: Path) -> str:
    digest = hashlib.sha256(content).hexdigest()
    reference = f'{PREFIX}purchase_order_{purchase_order_id}_sha256_{digest}.pdf'
    validate_pdf(reference, content, purchase_order_id=purchase_order_id)
    directory = _directory(root)
    if settings.purchase_order_pdf_storage == 'filesystem':
        directory.mkdir(parents=True, exist_ok=True)
        # Atomic publication without replacement, including concurrent callers.
        fd, temporary = tempfile.mkstemp(prefix='.po-pdf-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(temporary, directory / _name(reference))
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    else:
        try:
            client, bucket = _r2()
            try:
                client.put_object(Bucket=bucket, Key=reference, Body=content,
                                  ContentType='application/pdf', IfNoneMatch='*')
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') not in {'PreconditionFailed', '412'}:
                    raise
                # A retry is successful only if the existing original verifies.
        except (ClientError, BotoCoreError) as exc:
            raise PdfStorageUnavailable() from exc
    stored = read_pdf(reference, root=root, purchase_order_id=purchase_order_id)
    if stored.read_bytes() != content:
        raise OriginalPdfUnavailable('stored bytes differ')
    return reference
