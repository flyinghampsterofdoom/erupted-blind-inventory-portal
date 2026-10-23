"""Standard age encryption: only an X25519 public recipient is needed by Render.

No provisioning, scheduling, key generation or production restoration. The
Owner supplies a pinned official age executable. Private identities are used
only for an explicit local recovery rehearsal, never by the exporting worker.
"""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
import os
import re
import selectors
import stat
import subprocess
import time

from sqlalchemy import text
from sqlalchemy.engine import make_url
from .backup import BLOCK, MAX_BACKUP, _environment, _restore_dump_local
from .contracts import EvidenceError


def _private_file(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid() or info.st_size > 4096):
            raise EvidenceError('Recovery identity requires a small private Owner file')


def export_public(store, destination, recipient, *, age, pg_dump='pg_dump', timeout_seconds=120,
                  connection=None, snapshot_id=None):
    if not re.fullmatch(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}', recipient):
        raise EvidenceError('Explicit standard X25519 age recipient required')
    if not 0 < timeout_seconds <= 900:
        raise EvidenceError('Public backup requires a bounded execution timeout')
    from contextlib import nullcontext
    if snapshot_id is not None and (connection is None or not re.fullmatch(r'[0-9A-Fa-f-]+', snapshot_id)):
        raise EvidenceError('Explicit evidence-only backup snapshot is invalid')
    with (nullcontext(connection) if connection is not None else store.engine.connect()) as c:
        names = c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all()
    if not names or any(not name.startswith('fv_') for name in names):
        raise EvidenceError('Public backup requires an isolated evidence-only schema')
    if connection is None:
        store.engine.dispose()
    path = Path(destination)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    dump = encrypted = None
    try:
        with os.fdopen(fd, 'wb') as output, TemporaryFile() as errors, selectors.DefaultSelector() as selector:
            dump = subprocess.Popen([pg_dump, '-Fc', '--no-owner', '--no-acl', '--schema=public']
                                    + (['--snapshot='+snapshot_id] if snapshot_id else []),
                                    env=_environment(store.url), stdout=subprocess.PIPE, stderr=errors)
            encrypted = subprocess.Popen([str(age), '--encrypt', '--recipient', recipient],
                                         stdin=dump.stdout, stdout=subprocess.PIPE, stderr=errors)
            dump.stdout.close()
            os.set_blocking(encrypted.stdout.fileno(), False)
            selector.register(encrypted.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout_seconds
            count = 0
            digest = sha256()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EvidenceError('Public backup execution timeout')
                if not selector.select(min(remaining, .25)):
                    continue
                data = os.read(encrypted.stdout.fileno(), BLOCK)
                if not data:
                    break
                count += len(data)
                if count > MAX_BACKUP + BLOCK:
                    raise EvidenceError('Public backup exceeds reviewed size bound')
                digest.update(data)
                output.write(data)
            for process in (dump, encrypted):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or process.wait(timeout=remaining) != 0:
                    raise EvidenceError('Public backup subprocess failed')
            if count < 100:
                raise EvidenceError('Public backup envelope is incomplete')
            output.flush()
            os.fsync(output.fileno())
        return {'verified': True, 'archive_format': 'age-v1', 'encryption': 'age-X25519',
                'recipient': recipient, 'encrypted_sha256': digest.hexdigest(),
                'encrypted_bytes': count, 'plaintext_retained': False,
                'private_identity_required': False, 'off_device_copy_verified': False,
                'note': 'Successful standard encryption; independent full recovery verified separately'}
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        for process in (encrypted, dump):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout:
                    process.stdout.close()


@contextmanager
def decrypted_public(archive, identity, *, age, timeout_seconds=120):
    _private_file(identity)
    if not 100 <= Path(archive).stat().st_size <= MAX_BACKUP + BLOCK:
        raise EvidenceError('Invalid public archive size')
    with TemporaryDirectory(prefix='fv-age-restore-') as temporary:
        dump = Path(temporary) / 'evidence.dump'
        fd = os.open(dump, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as output:
            result = subprocess.run([str(age), '--decrypt', '--identity', str(identity), str(archive)],
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=timeout_seconds)
        if result.returncode != 0:
            raise EvidenceError('Public archive authentication failed; restoration prohibited')
        # Authentication succeeds before the caller can touch a database.
        yield dump


def restore_public_local(archive, identity, admin_url, database_name, *, age, pg_restore='pg_restore'):
    url = make_url(admin_url)
    if (url.host not in ('localhost', '127.0.0.1', '::1')
            or not database_name.startswith('erupted_fv_restore_')):
        raise EvidenceError('Recovery requires a fresh explicitly named local database')
    with decrypted_public(archive, identity, age=age) as dump:
        return _restore_dump_local(dump, url, database_name, pg_restore=pg_restore)


def verify_public_custody(receipt_path, recipient):
    import json
    _private_file(receipt_path)
    receipt = json.loads(Path(receipt_path).read_text())
    if not re.fullmatch(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}', recipient):
        raise EvidenceError('Independent public-key backup custody recipient is invalid')
    if (receipt.get('recipient') != recipient or receipt.get('custody_verified') is not True
            or receipt.get('local_matching_identity_roundtrip_verified') is not True
            or not receipt.get('custodian') or not receipt.get('verified_at')):
        raise EvidenceError('Independent public-key backup custody is not verified')
    return {key: receipt[key] for key in ('recipient', 'custodian', 'verified_at')}
