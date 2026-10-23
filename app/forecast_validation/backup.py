"""Streaming authenticated evidence backups and explicit local recovery rehearsal.

No service provisioning, key creation, network upload or business restoration.
Keys are supplied by the Owner; plaintext exists only in a protected temporary
file while pg_restore consumes it. Production scheduling remains disabled.
"""
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from hashlib import sha256
from contextlib import contextmanager
import os
import stat
import subprocess
import secrets
import json

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy.engine import make_url
from sqlalchemy import select, text
import psycopg
from psycopg import sql

from .contracts import EvidenceError, canonical
from .storage import metadata, Store

MAGIC = b'FV-EVIDENCE-BACKUP-1\n'
BLOCK = 64 * 1024
MAX_BACKUP = 8 * 1024 ** 3


def read_key(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise EvidenceError('Backup key must be an Owner-owned private regular file (0600)')
        key = file.read(33)
    if len(key) != 32:
        raise EvidenceError('Backup key must contain exactly 256 bits')
    return key


def _environment(url):
    parsed = make_url(url)
    return {**os.environ, 'PGHOST': parsed.host or 'localhost', 'PGPORT': str(parsed.port or 5432),
            'PGUSER': parsed.username or os.environ.get('USER', ''), 'PGPASSWORD': parsed.password or '',
            'PGDATABASE': parsed.database, 'PGSSLMODE': 'disable' if parsed.host in ('localhost', '127.0.0.1', '::1') else 'require',
            'PGOPTIONS': '-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000'}


def export_encrypted(store, destination, key_file, *, pg_dump='pg_dump'):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = read_key(key_file)
    with store.engine.connect() as c:
        names = c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all()
    if not names or any(not name.startswith('fv_') for name in names):
        raise EvidenceError('Backup requires an isolated evidence-only public schema')
    # A weekly advisory lock already owns one writer-role connection. Release
    # this Store's checked-in connection before pg_dump opens the second, so a
    # role capped at two connections can complete its required backup.
    store.engine.dispose()
    nonce = secrets.token_bytes(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    plain = sha256()
    count = 0
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out, TemporaryFile() as errors:
            out.write(MAGIC + nonce)
            process = subprocess.Popen([pg_dump, '-Fc', '--no-owner', '--no-acl', '--schema=public'],
                                       env=_environment(store.url), stdout=subprocess.PIPE, stderr=errors)
            try:
                while True:
                    block = process.stdout.read(BLOCK)
                    if not block:
                        break
                    count += len(block)
                    if count > MAX_BACKUP:
                        raise EvidenceError('Evidence backup exceeds reviewed size bound')
                    plain.update(block)
                    out.write(encryptor.update(block))
                if process.wait() != 0:
                    raise EvidenceError('Evidence pg_dump failed; no backup accepted')
                out.write(encryptor.finalize())
                out.write(encryptor.tag)
                out.flush()
                os.fsync(out.fileno())
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
        encrypted = sha256()
        with path.open('rb') as source:
            for block in iter(lambda: source.read(BLOCK), b''):
                encrypted.update(block)
        # Authenticate the whole output before marking it as a usable backup.
        with decrypted(path, key_file) as restored:
            recovered = sha256()
            with restored.open('rb') as file:
                for block in iter(lambda: file.read(BLOCK), b''):
                    recovered.update(block)
            if recovered.hexdigest() != plain.hexdigest():
                raise EvidenceError('Encrypted backup round trip differs')
        return {'verified': True, 'encryption': 'AES-256-GCM', 'plaintext_sha256': plain.hexdigest(),
                'encrypted_sha256': encrypted.hexdigest(), 'encrypted_bytes': path.stat().st_size,
                'plaintext_retained': False, 'off_device_copy_verified': False,
                'note': 'Authenticated local backup; remote custody and periodic full restore require separate commissioning'}
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@contextmanager
def decrypted(archive, key_file):
    key = read_key(key_file)
    with Path(archive).open('rb') as source, TemporaryDirectory(prefix='fv-restore-') as temporary:
        if source.read(len(MAGIC)) != MAGIC:
            raise EvidenceError('Unsupported evidence backup envelope')
        nonce = source.read(12)
        size = source.seek(0, 2)
        content_start = len(MAGIC) + 12
        if size < content_start + 16 or size > MAX_BACKUP + content_start + 16:
            raise EvidenceError('Invalid evidence backup size')
        source.seek(-16, 2)
        tag = source.read(16)
        decoder = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decoder.authenticate_additional_data(MAGIC)
        path = Path(temporary) / 'evidence.dump'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        source.seek(content_start)
        remaining = size - content_start - 16
        with os.fdopen(fd, 'wb') as output:
            while remaining:
                data = source.read(min(BLOCK, remaining))
                if not data:
                    raise EvidenceError('Truncated encrypted backup')
                output.write(decoder.update(data))
                remaining -= len(data)
            output.write(decoder.finalize())
        # Authentication succeeds before any destination database is touched.
        yield path


def restore_local(archive, key_file, admin_url, database_name, *, pg_restore='pg_restore'):
    url = make_url(admin_url)
    if (url.host not in ('localhost', '127.0.0.1', '::1')
            or not database_name.startswith('erupted_fv_restore_')):
        raise EvidenceError('Recovery rehearsal requires a fresh explicitly named local database')
    with decrypted(archive, key_file) as dump:
        return _restore_dump_local(dump, url, database_name, pg_restore=pg_restore)


def _restore_dump_local(dump, url, database_name, *, pg_restore):
    # Callers authenticate the complete envelope before reaching this helper.
    if (url.host not in ('localhost', '127.0.0.1', '::1')
            or not database_name.startswith('erupted_fv_restore_')):
        raise EvidenceError('Recovery requires a fresh explicitly named local database')
    with psycopg.connect(host=url.host, port=url.port or 5432, user=url.username or None,
                         password=url.password or None, dbname='postgres', autocommit=True) as c:
        if c.execute('SELECT 1 FROM pg_database WHERE datname=%s', (database_name,)).fetchone():
            raise EvidenceError('Recovery destination already exists; existing activity is preserved')
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name)))
    target = url.set(database=database_name)
    # pg_dump's schema-scoped archive includes CREATE SCHEMA public. Remove
    # only the empty default schema in this newly created rehearsal database.
    # No CASCADE: unexpected contents fail closed instead of being removed.
    with psycopg.connect(host=url.host, port=url.port or 5432, user=url.username or None,
                         password=url.password or None, dbname=database_name, autocommit=True) as c:
        c.execute('DROP SCHEMA public')
    # restore is the only write path in this module and is confined above.
    environment = _environment(target)
    environment['PGOPTIONS'] = '-c statement_timeout=10000 -c lock_timeout=2000'
    subprocess.run([pg_restore, '--no-owner', '--no-acl', '--exit-on-error', '-d', database_name, str(dump)],
                   env=environment, check=True, capture_output=True)
    return Store(target.render_as_string(hide_password=False))


def inventory(store, *, connection=None):
    """Bounded row-content inventory, including orphan staging and all histories."""
    from . import operations  # Register all isolated evidence tables.
    result = {}
    for table in metadata.tables.values():
        hasher = sha256()
        count = 0
        from contextlib import nullcontext
        with (nullcontext(connection) if connection is not None else store.engine.connect()) as c:
            query = select(table).order_by(*table.primary_key)
            rows = c.execution_options(stream_results=True, yield_per=16).execute(query).mappings()
            for row in rows:
                value = {key: {'binary_sha256': sha256(bytes(v)).hexdigest(), 'bytes': len(v)}
                         if isinstance(v, (bytes, memoryview)) else v for key, v in row.items()}
                hasher.update(canonical(value).encode() + b'\n')
                count += 1
        result[table.name] = {'rows': count, 'sha256': hasher.hexdigest()}
    return result


def verify_key_custody(receipt_path, key_file):
    """Validate an explicit Owner custody attestation; do not invent custody.

    Actual custody/restore acceptance remains an Owner commissioning task. The
    attestation contains only a key fingerprint, never the recovery key itself.
    """
    key = read_key(key_file)
    fd = os.open(receipt_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid() or info.st_size > 4096):
            raise EvidenceError('Key custody attestation must be a small private Owner file')
        receipt = json.load(file)
    if (receipt.get('key_sha256') != sha256(key).hexdigest()
            or receipt.get('independent_recovery_verified') is not True
            or not receipt.get('custodian') or not receipt.get('verified_at')):
        raise EvidenceError('Independent backup-key custody is not verified')
    return {'key_sha256': receipt['key_sha256'], 'custodian': receipt['custodian'],
            'verified_at': receipt['verified_at']}


def archive_to_s3(client, bucket, archive, receipt, *, period_id, custody):
    """Use an explicitly commissioned prefix-scoped client; never provision it.

    Unit tests inject a fake client. No configured purchasing bucket, credentials
    or object keys are reused implicitly. Round-trip ciphertext is streamed.
    """
    if not bucket or len(period_id) != 64 or any(c not in '0123456789abcdef' for c in period_id):
        raise EvidenceError('Explicit isolated archive bucket and period required')
    if not receipt.get('verified') or not custody:
        raise EvidenceError('Encrypted artifact and independent key custody required')
    path = Path(archive)
    archive_format = receipt.get('archive_format', 'aesgcm-v1')
    if archive_format not in ('aesgcm-v1', 'age-v1'):
        raise EvidenceError('Unsupported encrypted archive format')
    suffix = '.age' if archive_format == 'age-v1' else '.aesgcm'
    key = 'forecast-evidence/v1/' + period_id + '/' + receipt['encrypted_sha256'] + suffix
    # Unique ciphertext hash makes retries immutable. Conditional insertion
    # prevents overwriting an existing object under the same evidence identity.
    with path.open('rb') as body:
        try:
            client.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch='*',
                              ContentType='application/octet-stream',
                              Metadata={'sha256': receipt['encrypted_sha256']})
        except Exception as exc:
            code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
            # R2 can reject a retry at the retention-lock boundary before
            # evaluating If-None-Match. Never infer success from that rejection:
            # the GET below must still prove the entire ciphertext and size.
            if code not in ('PreconditionFailed', '412', 'ObjectLockedByBucketPolicy'):
                raise
    response = client.get_object(Bucket=bucket, Key=key)
    hasher, count = sha256(), 0
    stream = response['Body']
    try:
        while True:
            data = stream.read(BLOCK)
            if not data:
                break
            count += len(data)
            if count > MAX_BACKUP + (64 * 1024 if archive_format == 'age-v1' else len(MAGIC) + 28):
                raise EvidenceError('Archive verification exceeded bound')
            hasher.update(data)
    finally:
        stream.close()
    if hasher.hexdigest() != receipt['encrypted_sha256'] or count != receipt['encrypted_bytes']:
        raise EvidenceError('Off-device ciphertext round trip differs')
    return {**receipt, 'off_device_copy_verified': True, 'independent_key_custody_verified': True,
            'object_key': key, 'key_custody': custody}
