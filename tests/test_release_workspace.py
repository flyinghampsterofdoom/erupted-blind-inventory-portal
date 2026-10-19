"""Release provenance boundaries, independent indexes and safe diagnostics."""
import subprocess
from pathlib import Path
import pytest
from scripts.release.prepare_checkout import prepare
from scripts.release.workspace_evidence import assert_clean, snapshot
from scripts.release.build_identity import build
from app.services.runtime_identity_service import capture_identity


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv('RENDER_GIT_COMMIT', raising=False)
    root = tmp_path/'source'; root.mkdir(); git(root, 'init', '-q')
    (root/'app').mkdir(); (root/'app/main.py').write_text('original source\n')
    (root/'.gitignore').write_text('__pycache__/\nrelease-identity.json\n')
    git(root, 'add', '.'); git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Original')
    return root


def test_staged_and_unstaged_files_named_without_contents(repo, capsys):
    p=repo/'app/main.py'; p.write_text('PRIVATE CONTENT staged'); git(repo, 'add', str(p)); p.write_text('PRIVATE CONTENT unstaged')
    state=snapshot(repo,'test'); assert state['staged']=='M\tapp/main.py' and state['unstaged']=='M\tapp/main.py'
    with pytest.raises(RuntimeError, match='app/main.py'): build(repo)
    output=capsys.readouterr().out
    assert 'PRIVATE CONTENT' not in output and 'app/main.py' in output


def test_generated_caches_do_not_dirty_tracked_runtime(repo):
    cache=repo/'app/__pycache__'; cache.mkdir(); (cache/'main.pyc').write_bytes(b'generated')
    (repo/'output').mkdir(); (repo/'output/report.json').write_text('{}')
    result=build(repo); assert capture_identity(repo)['status']=='VERIFIED'
    assert 'output/report.json' not in result['files']


def test_executable_mode_changes_remain_integrity_failures(repo):
    git(repo, 'config', 'core.fileMode', 'true'); (repo/'app/main.py').chmod(0o755)
    with pytest.raises(RuntimeError, match='app/main.py'): build(repo)


def test_untracked_runtime_code_is_not_accepted(repo):
    (repo/'app/unexpected.py').write_text('unreviewed')
    with pytest.raises(RuntimeError, match='app/unexpected.py'): build(repo)


def test_isolated_checkout_uses_commit_not_active_files(repo,tmp_path,monkeypatch):
    revision=git(repo,'rev-parse','HEAD'); (repo/'app/main.py').write_text('owner unfinished source')
    (repo/'untracked.txt').write_text('owner unfinished file')
    before=git(repo,'status','--porcelain=v1','--untracked-files=all')
    dest=tmp_path/'release'; result=prepare(repo,dest,revision)
    assert result['status']=='PASS' and (dest/'app/main.py').read_text()=='original source\n'
    assert not (dest/'untracked.txt').exists() and (repo/'app/main.py').read_text()=='owner unfinished source'
    assert git(repo,'status','--porcelain=v1','--untracked-files=all')==before
    # An inherited index/worktree must not redirect release provenance checks.
    monkeypatch.setenv('GIT_WORK_TREE',str(repo)); monkeypatch.setenv('GIT_DIR',str(repo/'.git'))
    evidence=assert_clean(dest,'test'); assert evidence['git_toplevel']==str(dest)
    assert build(dest)['revision']==revision
    monkeypatch.delenv('GIT_DIR'); monkeypatch.delenv('GIT_WORK_TREE')
    assert git(dest,'rev-parse','--git-common-dir')=='.git'
    with pytest.raises(ValueError,match='must not exist'):prepare(repo,dest,revision)


def test_full_sha_and_expected_platform_revision_required(repo,tmp_path,monkeypatch):
    with pytest.raises(ValueError,match='full commit'): prepare(repo,tmp_path/'bad','HEAD')
    monkeypatch.setenv('RENDER_GIT_COMMIT','f'*40)
    with pytest.raises(RuntimeError,match='Checkout differs'):build(repo)
