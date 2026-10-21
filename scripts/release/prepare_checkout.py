"""Create a new isolated release checkout. No deploy, reset, clean or source copy."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def prepare(source, destination, revision):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('An exact full commit SHA is required')
    source = Path(source).resolve(); destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError('Destination must not exist; existing files are never discarded')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    def git(root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], env=env)
    assert git(source, 'rev-parse', revision+'^{commit}').decode().strip() == revision
    source_status = git(source, 'status', '--porcelain=v1', '--untracked-files=all').decode()
    # --no-local transfers committed Git objects, with no shared objects/index or
    # active workspace files; --no-checkout avoids checking out the source HEAD.
    subprocess.run(['git', 'clone', '--no-local', '--no-checkout', '--', str(source), str(destination)], env=env, check=True, capture_output=True)
    git(destination, 'checkout', '--detach', revision)
    assert git(destination, 'rev-parse', 'HEAD').decode().strip() == revision
    assert not git(destination, 'status', '--porcelain=v1', '--untracked-files=all').strip()
    assert git(source, 'status', '--porcelain=v1', '--untracked-files=all').decode() == source_status
    return {'status': 'PASS', 'revision': revision, 'checkout': str(destination),
            'detached': True, 'independent_git_directory': True,
            'source_workspace_changed': False, 'production_calls': 0}


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--source', type=Path, required=True)
    p.add_argument('--destination', type=Path, required=True); p.add_argument('--revision', required=True)
    a = p.parse_args(); print(json.dumps(prepare(a.source, a.destination, a.revision)))
