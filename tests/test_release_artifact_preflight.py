"""Failure-before-deploy regression, including the failed ea2c7ab source."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import pytest
from scripts.ci.artifact_fixture import make, REVISION
from scripts.release import artifact_preflight as gate, advisory_installer as installer

BASELINE='6b81c95d7eb55510f7e8647b7b0314e3d2fef2a7'

@pytest.fixture
def artifact(tmp_path):
    spec,env=make(tmp_path/'fixture')
    return tmp_path/'fixture',spec,env


def configure_install(monkeypatch,tmp_path,artifact,content=None):
    folder,spec,env=artifact
    class Response(io.BytesIO): status=200
    class Opener:
        def open(self,request,**kwargs):
            assert request.full_url==env['FORECAST_PRODUCT_ARTIFACT_URL']
            return Response(content if content is not None else (folder/'artifact.tar.gz').read_bytes())
    monkeypatch.chdir(tmp_path)
    for k,v in env.items():monkeypatch.setenv(k,v)
    monkeypatch.setenv('FORECAST_PRODUCT_PROJECTION_PATH',str(tmp_path/'.forecast-stage4/product.sqlite3'))
    monkeypatch.setattr(installer.urllib.request,'build_opener',lambda *args:Opener())


def test_correct_artifact_and_matching_ea2_source(artifact,monkeypatch,tmp_path):
    folder,spec,env=artifact
    receipt=gate.configuration(env,REVISION,gate.BUILD_COMMAND)
    assert receipt['spec']==spec
    configure_install(monkeypatch,tmp_path,artifact);installer.main()
    target=tmp_path/'.forecast-stage4/product.sqlite3'
    assert hashlib.sha256(target.read_bytes()).hexdigest()==spec['projection_sha256']
    assert target.stat().st_mode & 0o777==0o400


def test_previously_failed_ea2_candidate_rejects_baseline_bound_spec(artifact):
    _,spec,env=artifact;spec['revision']=BASELINE;env['FORECAST_ADVISORY_ASSET_SPEC']=json.dumps(spec)
    with pytest.raises(gate.PreflightError,match='artifact-source-binding-mismatch'):
        gate.configuration(env,REVISION,gate.BUILD_COMMAND)


def test_changing_expected_sha_alone_does_not_rebind_embedded_manifest(monkeypatch,tmp_path):
    folder=tmp_path/'old';spec,env=make(folder,BASELINE)
    spec['revision']=REVISION;env['FORECAST_ADVISORY_ASSET_SPEC']=json.dumps(spec);env['RENDER_GIT_COMMIT']=REVISION
    configure_install(monkeypatch,tmp_path,(folder,spec,env))
    with pytest.raises(AssertionError):installer.main()
    assert not (tmp_path/'.forecast-stage4/product.sqlite3').exists()


@pytest.mark.parametrize('defect', ['missing-setting','expired','insufficient-window','wrong-installer','wrong-command','wrong-checksum-setting','worker-enabled','schedule-enabled','wrong-path','optimized','wrong-source'])
def test_configuration_failures_are_named_without_secrets(artifact,defect):
    _,spec,env=artifact;command=gate.BUILD_COMMAND;now=datetime.now(timezone.utc)
    if defect=='missing-setting':env.pop('FORECAST_PRODUCT_ARTIFACT_URL')
    if defect=='expired':now+=timedelta(days=1)
    if defect=='insufficient-window':now+=timedelta(minutes=50)
    if defect=='wrong-installer':env['FORECAST_ADVISORY_INSTALLER_B64']=base64.b64encode(b'print("unsafe")').decode()
    if defect=='wrong-command':command='true'
    if defect=='wrong-checksum-setting':env['FORECAST_PRODUCT_PROJECTION_SHA256']='0'*64
    if defect=='worker-enabled':env['FORECAST_VALIDATION_WORKER_ENABLED']='true'
    if defect=='schedule-enabled':env['FORECAST_VALIDATION_SCHEDULE_ENABLED']='true'
    if defect=='wrong-path':env['FORECAST_PRODUCT_PROJECTION_PATH']='/tmp/unsafe'
    if defect=='optimized':env['PYTHONOPTIMIZE']='1'
    if defect=='wrong-source':spec['revision']=BASELINE;env['FORECAST_ADVISORY_ASSET_SPEC']=json.dumps(spec)
    with pytest.raises(gate.PreflightError) as caught:gate.configuration(env,REVISION,command,now=now)
    error=caught.value
    assert error.component and error.code and error.action
    assert 'X-Amz-Signature' not in str(error) and 'SYNTHETIC' not in str(error)


@pytest.mark.parametrize('defect',['corrupted','wrong-archive-checksum','wrong-projection-checksum','wrong-size'])
def test_actual_installer_rejects_artifact_corruption(artifact,monkeypatch,tmp_path,defect):
    folder,spec,env=artifact;content=None
    if defect=='corrupted':content=b'x'+(folder/'artifact.tar.gz').read_bytes()[1:]
    if defect=='wrong-archive-checksum':spec['archive_sha256']='0'*64
    if defect=='wrong-projection-checksum':spec['projection_sha256']='0'*64;env['FORECAST_PRODUCT_PROJECTION_SHA256']='0'*64
    if defect=='wrong-size':spec['archive_bytes']-=1
    env['FORECAST_ADVISORY_ASSET_SPEC']=json.dumps(spec)
    configure_install(monkeypatch,tmp_path,artifact,content)
    with pytest.raises(AssertionError):installer.main()
    assert not (tmp_path/'.forecast-stage4/product.sqlite3').exists()


def test_missing_remote_artifact_does_not_install(artifact,monkeypatch,tmp_path):
    configure_install(monkeypatch,tmp_path,artifact)
    class Missing:
        def open(self,*args,**kwargs):raise FileNotFoundError('synthetic missing artifact')
    monkeypatch.setattr(installer.urllib.request,'build_opener',lambda *args:Missing())
    with pytest.raises(FileNotFoundError):installer.main()
    assert not (tmp_path/'.forecast-stage4/product.sqlite3').exists()


def test_repackage_is_deterministic_and_preserves_projection(artifact,tmp_path):
    folder,spec,_=artifact;original=(folder/'synthetic.sqlite').read_bytes()
    a=gate.repackage(folder/'synthetic.sqlite',{'projection_sha256':spec['projection_sha256']},spec['projection_sha256'],BASELINE,tmp_path/'one.tar.gz')
    b=gate.repackage(folder/'synthetic.sqlite',{'projection_sha256':spec['projection_sha256']},spec['projection_sha256'],BASELINE,tmp_path/'two.tar.gz')
    assert a==b and (tmp_path/'one.tar.gz').read_bytes()==(tmp_path/'two.tar.gz').read_bytes()
    assert (folder/'synthetic.sqlite').read_bytes()==original
    with tarfile.open(tmp_path/'one.tar.gz') as tf:
        assert tf.extractfile('product.sqlite').read()==original
        assert json.load(tf.extractfile('release-manifest.json'))['web_candidate']==BASELINE


def test_unverified_projection_repackage_fails_without_output(artifact,tmp_path):
    folder,spec,_=artifact
    with pytest.raises(gate.PreflightError,match='frozen-projection-mismatch'):
        gate.repackage(folder/'synthetic.sqlite',{'projection_sha256':'0'*64},'0'*64,REVISION,tmp_path/'forbidden.tar.gz')
    assert not (tmp_path/'forbidden.tar.gz').exists()


def test_installer_imports_no_business_execution_dependencies(tmp_path):
    result=subprocess.run([sys.executable,'-c','import sys; from scripts.release import advisory_installer; assert not any(m in sys.modules for m in ["app.db","app.config","app.models","boto3","psycopg"])'],cwd=gate.ROOT,capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_wrong_lock_is_rejected_by_existing_manifest_verifier(tmp_path):
    from scripts.release.phase3.manifest import prepare
    # Complete clean source clone, no network or business imports. Change the
    # lock after sealing: the existing clean-build identity must reject it.
    root=tmp_path/'clean'
    subprocess.run(['git','clone','--quiet','--no-hardlinks',str(gate.ROOT),str(root)],check=True)
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    manifest=prepare(root,revision,from_schema='20261024_0050',baseline_revision=BASELINE,baseline_supports=['20261024_0050'],compatibility_review='test-only')
    (root/'requirements.txt').write_text((root/'requirements.txt').read_text()+'\n# altered lock\n')
    with pytest.raises(gate.PreflightError,match='sealed-source-or-lock-mismatch'):gate.source(root,manifest)
