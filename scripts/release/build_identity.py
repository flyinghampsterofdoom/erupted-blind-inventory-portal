"""Build-time attestation. No DB or network; never use an expected SHA as identity."""
import hashlib
import json
import os
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.release.workspace_evidence import assert_clean, git


def build(root):
    revision = git(root, 'rev-parse', 'HEAD').decode().strip()
    expected = os.getenv('RENDER_GIT_COMMIT')
    if expected and expected != revision:
        raise RuntimeError('Checkout differs from platform revision')
    assert_clean(root, 'identity')
    names = git(root, 'ls-files', '-z').decode().split('\0')
    # Runtime source, assets, schema and entrypoints. Tests and reports are not
    # executable production dependencies. No environment/credential files.
    names = [p for p in names if p.startswith(('app/', 'scripts/', 'migrations/'))
             or p in {'requirements.txt', '.python-version', 'render.yaml', 'alembic.ini'}]
    files = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in names}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    result = {'revision': revision, 'source_tree_sha256': digest, 'files': files}
    (root / 'release-identity.json').write_text(json.dumps(result, sort_keys=True) + '\n')
    return result


if __name__ == '__main__':
    result = build(Path(__file__).resolve().parents[2])
    print(json.dumps({k: result[k] for k in ('revision', 'source_tree_sha256')}))
