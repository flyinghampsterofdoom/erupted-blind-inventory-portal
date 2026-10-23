"""Read-only prepared-view identity checks, cached only while file identity is unchanged."""
import hashlib
from pathlib import Path
import threading

_lock=threading.Lock()
_verified=set()

def verify_file(path, expected, required=False):
    path=Path(path)
    if required and not expected:raise ValueError('Production requires a pinned projection SHA256')
    if not expected:return
    if len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected):raise ValueError('Invalid projection identity')
    stat=path.stat();identity=(str(path.resolve()),stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,expected)
    with _lock:
        if identity in _verified:return
        h=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
        after=path.stat()
        if stat!=after or h.hexdigest()!=expected:raise ValueError('Prepared projection identity mismatch')
        _verified.clear();_verified.add(identity)
