"""Path/status-only evidence. Never include source diffs, secrets or file contents."""
import json
import os
from pathlib import Path
import subprocess


def git(root, *args):
    # Git process routing must refer to this checkout, never the operator's index.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.check_output(['git', '-C', str(root), *args], env=env)


def runtime_path(path):
    return path.startswith(('app/', 'scripts/', 'migrations/')) or path in {
        'requirements.txt', '.python-version', 'render.yaml', 'alembic.ini'}


def snapshot(root, phase):
    root = Path(root).resolve()
    def output(*args):
        return git(root, *args).decode().strip()
    branch = output('symbolic-ref', '--quiet', '--short', 'HEAD') if git(root, 'branch', '--show-current').strip() else '(detached)'
    return {'event': 'ERUPTED_BUILD_WORKSPACE', 'phase': phase,
            'root': str(root), 'process_cwd': str(Path.cwd()),
            'git_toplevel': output('rev-parse', '--show-toplevel'),
            'revision': output('rev-parse', 'HEAD'), 'branch': branch,
            'status': output('status', '--porcelain=v1', '--untracked-files=all'),
            'staged': output('diff', '--cached', '--name-status', 'HEAD'),
            'unstaged': output('diff', '--name-status'),
            'untracked': git(root, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')[:-1],
            'runtime_changes': git(root, 'diff', 'HEAD', '--name-only', '-z', '--', 'app', 'scripts', 'migrations', 'requirements.txt', '.python-version', 'render.yaml', 'alembic.ini').decode().split('\0')[:-1],
            'inherited_git_variable_names': sorted(k for k in os.environ if k.startswith('GIT_'))}


def assert_clean(root, phase):
    evidence = snapshot(root, phase)
    print(json.dumps(evidence, sort_keys=True), flush=True)
    if Path(evidence['git_toplevel']).resolve() != Path(root).resolve():
        raise RuntimeError('Build root must be the isolated checkout root')
    added = [p for p in evidence['untracked'] if runtime_path(p)]
    if evidence['runtime_changes'] or added:
        raise RuntimeError('Runtime source has uncommitted changes: ' + ', '.join(evidence['runtime_changes'] + added))
    return evidence


if __name__ == '__main__':
    import sys
    assert_clean(Path(__file__).resolve().parents[2], sys.argv[1])
