"""Read-only runtime proof, derived from built files rather than an expected SHA."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tree_digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def capture_identity(root=ROOT):
    result = {'status': 'UNVERIFIED', 'started_at': datetime.now(timezone.utc).isoformat(),
              'service_id': os.getenv('RENDER_SERVICE_ID'), 'instance_id': os.getenv('RENDER_INSTANCE_ID'),
              'platform_revision': os.getenv('RENDER_GIT_COMMIT')}
    try:
        manifest = json.loads((root / 'release-identity.json').read_text())
        files = manifest['files']
        if not files or any(Path(p).is_absolute() or '..' in Path(p).parts for p in files):
            raise ValueError('Invalid build paths')
        actual = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in files}
        valid = actual == files and tree_digest(files) == manifest['source_tree_sha256']
        result.update(status='VERIFIED' if valid else 'INVALID', revision=manifest['revision'],
                      source_tree_sha256=tree_digest(actual), file_count=len(files))
        if result['platform_revision'] and result['platform_revision'] != manifest['revision']:
            result['status'] = 'INVALID'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


def cron_runtime_proof():
    # No DB/config imports, including on the disabled scheduling path. Logging
    # identity does not execute an automation tick or create a second scheduler.
    return {'event': 'ERUPTED_RUNTIME_IDENTITY', 'role': 'cron', **capture_identity()}
