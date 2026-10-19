"""Actual subprocess startup: no runpy, inherited PYTHONPATH, database or Square."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from scripts.release.history_job_command import ENTRYPOINT, start_command

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def artifact(tmp_path_factory):
    root=tmp_path_factory.mktemp('history-artifact')
    for name in ('app','scripts'):
        shutil.copytree(ROOT/name,root/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
           for parent in ('app','scripts') for p in (root/parent).rglob('*') if p.is_file()}
    digest=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    (root/'release-identity.json').write_text(json.dumps({'revision':'a'*40,'files':files,'source_tree_sha256':digest}))
    return root,digest


def clean_environment():
    env=os.environ.copy()
    for key in list(env):
        if key.startswith(('ERUPTED_EXPECTED_','RENDER_')) or key=='PYTHONPATH':env.pop(key)
    env.update(PATH=str(Path(sys.executable).parent)+os.pathsep+env['PATH'],
               DATABASE_URL='postgresql+psycopg://invalid:invalid@127.0.0.1:1/no_database',
               SQUARE_ACCESS_TOKEN='nonfunctional-test-placeholder',SQUARE_API_BASE_URL='http://127.0.0.1:1')
    return env


def test_exact_hosted_preflight_command_resolves_real_app_without_io(artifact):
    root,digest=artifact
    result=subprocess.run(start_command('a'*40,digest,preflight=True),shell=True,cwd=root,
        env=clean_environment(),capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    proof=json.loads(result.stdout)
    assert proof['event']=='ERUPTED_HISTORY_JOB_PREFLIGHT_VERIFIED'
    assert proof['application_imports_verified'] and proof['configuration_loaded']
    assert proof['entrypoint']=='__main__'
    assert proof['database_connections']==proof['database_writes']==proof['Square_requests']==0


def test_launcher_finds_artifact_from_other_working_directory(artifact,tmp_path):
    root,_=artifact
    result=subprocess.run(['sh',str(root/ENTRYPOINT[1]),'--preflight'],cwd=tmp_path,
        env=clean_environment(),capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['application_imports_verified']


def test_rehearsal_build_and_production_share_entrypoint():
    assert start_command('a'*40,'b'*64).endswith('sh scripts/release/purchasing_history_job.sh')
    assert ' '.join(ENTRYPOINT)+' --preflight' in (ROOT/'scripts/release/build.sh').read_text()
    launcher=(ROOT/ENTRYPOINT[1]).read_text()
    assert 'exec python -m scripts.release.refresh_purchasing_history "$@"' in launcher
    assert 'PYTHONPATH=' not in launcher


@pytest.mark.parametrize('revision,digest',[('main','b'*64),('a'*40,'unknown'),('a'*40+';bad','b'*64)])
def test_invalid_identity_cannot_be_submitted(revision,digest):
    with pytest.raises(ValueError):start_command(revision,digest)


def test_preflight_rejects_wrong_artifact_identity(artifact):
    root,digest=artifact
    result=subprocess.run(start_command('c'*40,digest,preflight=True),shell=True,cwd=root,
        env=clean_environment(),capture_output=True,text=True,timeout=30)
    assert result.returncode!=0
    assert 'AssertionError' in result.stderr
