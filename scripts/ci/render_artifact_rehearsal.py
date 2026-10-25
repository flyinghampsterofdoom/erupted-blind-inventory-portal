"""Actual configured Render shell command with an explicitly SYNTHETIC transport.

A success proves clean Linux command/installer composition, never live R2 access
or production qualification. CI receives no production artifact or credentials.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from scripts.ci.artifact_fixture import make
from scripts.release.artifact_preflight import BUILD_COMMAND, configuration
from scripts.release.phase3.manifest import prepare


def main():
    root=Path(__file__).resolve().parents[2]
    out=root/'ci-results';out.mkdir(exist_ok=True)
    target=Path('/opt/render/project/src')
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    if target.exists():raise RuntimeError('Rehearsal root must be new and disposable')
    subprocess.run(['sudo','mkdir','-p',str(target.parent)],check=True)
    subprocess.run(['sudo','chown',str(os.getuid())+':'+str(os.getgid()),str(target.parent)],check=True)
    subprocess.run(['git','worktree','add','--detach',str(target),revision],cwd=root,check=True,capture_output=True)
    started=time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='synthetic-advisory-') as tmp:
            fixture=Path(tmp);spec,env=make(fixture/'artifact',revision)
            # Only the fixed synthetic HTTPS fixture request is intercepted.
            # All real socket connections from the build's Python children fail.
            guard=fixture/'guard';guard.mkdir()
            (guard/'sitecustomize.py').write_text('''import io, os, socket, urllib.request
class Response(io.BytesIO): status=200
class Opener:
 def open(self, request, *args, **kwargs):
  if request.full_url != os.environ['FORECAST_PRODUCT_ARTIFACT_URL']:raise RuntimeError('Non-fixture network request prohibited')
  with open(os.environ['SYNTHETIC_ARTIFACT_FILE'],'rb') as stream:return Response(stream.read())
urllib.request.build_opener=lambda *args,**kwargs:Opener()
def denied(*args,**kwargs):raise RuntimeError('Real network prohibited in synthetic build rehearsal')
socket.socket.connect=denied
''')
            contract=configuration(env,revision,BUILD_COMMAND)
            child={k:os.environ[k] for k in ('PATH','HOME','LD_LIBRARY_PATH') if k in os.environ}
            child.update(env,PYTHONPATH=str(guard),PYTHONDONTWRITEBYTECODE='1',PIP_NO_INDEX='1',PIP_DISABLE_PIP_VERSION_CHECK='1',SYNTHETIC_ARTIFACT_FILE=str(fixture/'artifact/artifact.tar.gz'))
            with (out/'render-artifact-build.log').open('w') as log:
                result=subprocess.run(BUILD_COMMAND,shell=True,cwd=target,env=child,stdout=log,stderr=subprocess.STDOUT,timeout=300)
            if result.returncode:raise RuntimeError('Full synthetic configured build failed; inspect synthetic build log')
            installed=target/'.forecast-stage4/product.sqlite3'
            if hashlib.sha256(installed.read_bytes()).hexdigest()!=spec['projection_sha256']:raise RuntimeError('Installed fixture identity mismatch')
            identity=json.loads((target/'release-identity.json').read_text())
            manifest=prepare(target,revision,from_schema='20261024_0050',baseline_revision='6b81c95d7eb55510f7e8647b7b0314e3d2fef2a7',baseline_supports=['20261024_0050'],compatibility_review='synthetic-CI-configured-build')
            report={'status':'PASS','revision':revision,'source_tree_sha256':identity['source_tree_sha256'],'build_command_sha256':contract['build_command_sha256'],'clean_checkout':True,'runtime':'Python 3.10.13 on GitHub ubuntu-24.04','working_directory':str(target),'actual_configured_command_executed':True,'real_network_allowed':False,'transport':'SYNTHETIC fixed HTTPS fixture; not live R2 qualification','production_artifact_or_credentials_present':False,'projection_is_synthetic':True,'web_cron_identity_equal':manifest['roles']['web']==manifest['roles']['cron'],'schema':manifest['schema'],'duration_seconds':round(time.monotonic()-started,3)}
            (out/'render-artifact-rehearsal.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
    finally:
        subprocess.run(['git','worktree','remove','--force',str(target)],cwd=root,check=True,capture_output=True)


if __name__=='__main__':main()
