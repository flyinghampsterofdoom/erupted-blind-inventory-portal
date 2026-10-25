"""Read-only release preparation. No provider mutations or production DB imports.

The tracked installer is a byte-for-byte copy of the existing Render installer.
Preflight invokes that implementation rather than maintaining a second archive,
SQLite, or checksum validation algorithm. Reports never contain environment values.
"""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / 'scripts/release/advisory_installer.py'
INSTALL_COMMAND = "python -c 'import os,base64;exec(compile(base64.b64decode(os.environ[\"FORECAST_ADVISORY_INSTALLER_B64\"]),\"<advisory-asset-installer>\",\"exec\"))'"
BUILD_COMMAND = 'sh scripts/release/build.sh && ' + INSTALL_COMMAND
REQUIRED = ('FORECAST_ADVISORY_ASSET_SPEC', 'FORECAST_ADVISORY_INSTALLER_B64',
    'FORECAST_PRODUCT_ARTIFACT_URL', 'FORECAST_PRODUCT_PROJECTION_PATH',
    'FORECAST_PRODUCT_PROJECTION_SHA256', 'FORECAST_VALIDATION_WORKER_ENABLED',
    'FORECAST_VALIDATION_SCHEDULE_ENABLED')


class PreflightError(ValueError):
    def __init__(self, component, code, action):
        self.component, self.code, self.action = component, code, action
        super().__init__(code)


def require(ok, component, code, action):
    if not ok: raise PreflightError(component, code, action)


def sha256(value): return hashlib.sha256(value).hexdigest()


def configuration(environment, revision, build_command, *, now=None, minimum_validity=900):
    """No network, filesystem writes, execution, or reflection of secret values."""
    now = now or datetime.now(timezone.utc)
    require(bool(re.fullmatch('[a-f0-9]{40}', revision or '')), 'source', 'full-sha-required', 'Select the sealed full Git SHA.')
    require(all(environment.get(k) for k in REQUIRED), 'environment', 'required-setting-missing', 'Supply the documented service-local artifact settings.')
    require(not environment.get('PYTHONOPTIMIZE') and not sys.flags.optimize,
        'installer', 'assertions-disabled', 'Use the pinned Python runtime without optimization.')
    require(build_command == BUILD_COMMAND, 'installer', 'unreviewed-build-command', 'Review and reproduce the complete configured build command.')
    try:
        installer = base64.b64decode(environment['FORECAST_ADVISORY_INSTALLER_B64'], validate=True)
        spec = json.loads(environment['FORECAST_ADVISORY_ASSET_SPEC'])
    except (ValueError, TypeError):
        raise PreflightError('environment', 'malformed-artifact-settings', 'Restore the reviewed installer and artifact specification.') from None
    require(installer == INSTALLER.read_bytes(), 'installer', 'installer-identity-mismatch', 'Review the changed installer before release.')
    require(spec.get('revision') == revision, 'advisory-artifact', 'artifact-source-binding-mismatch', 'Prepare a verified archive and embedded manifest for the exact candidate; changing only the expected SHA is insufficient.')
    require(all(isinstance(spec.get(k), str) and re.fullmatch('[a-f0-9]{64}', spec[k]) for k in ('projection_sha256','archive_sha256')),
        'advisory-artifact', 'invalid-checksum-contract', 'Provide the verified archive and projection SHA-256 identities.')
    require(all(type(spec.get(k)) is int and 0 < spec[k] <= 256*1024*1024 for k in ('projection_bytes','archive_bytes')),
        'advisory-artifact', 'invalid-size-contract', 'Provide bounded verified artifact sizes.')
    require(environment['FORECAST_PRODUCT_PROJECTION_SHA256'] == spec['projection_sha256'],
        'advisory-artifact', 'projection-identity-mismatch', 'Align the application projection identity with the verified archive.')
    require(all(environment[k].strip().lower() in ('false','0','no','off') for k in ('FORECAST_VALIDATION_WORKER_ENABLED','FORECAST_VALIDATION_SCHEDULE_ENABLED')),
        'forecast-safety', 'dormant-execution-not-disabled', 'Keep forecasting worker and recurring execution disabled.')
    require(environment['FORECAST_PRODUCT_PROJECTION_PATH'] == '/opt/render/project/src/.forecast-stage4/product.sqlite3',
        'environment', 'projection-path-mismatch', 'Use the reviewed Render projection path; isolate the rehearsal with the same working directory.')
    url = urlsplit(environment['FORECAST_PRODUCT_ARTIFACT_URL']); query = parse_qs(url.query)
    require(url.scheme == 'https' and bool(url.hostname) and url.hostname.endswith('.r2.cloudflarestorage.com') and not url.username and not url.password and not url.fragment,
        'artifact-access', 'unapproved-artifact-origin', 'Use the existing private HTTPS artifact origin without redirects.')
    try:
        issued = datetime.strptime(query['X-Amz-Date'][0], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        expiry = issued + timedelta(seconds=int(query['X-Amz-Expires'][0]))
        valid = int(query['X-Amz-Expires'][0]) > 0 and issued <= now and expiry > now + timedelta(seconds=minimum_validity)
    except (KeyError, ValueError, OverflowError):
        valid = False; expiry = None
    require(valid, 'artifact-access', 'artifact-access-expired-or-insufficient', 'Obtain separately authorized artifact access valid through the build window; do not alter production credentials here.')
    return {'revision':revision, 'spec':spec, 'installer_sha256':sha256(installer), 'build_command_sha256':sha256(build_command.encode()), 'artifact_access_expires_at':expiry.isoformat()}


def source(root, manifest):
    from scripts.release.phase3.manifest import verify_source
    try: verify_source(root, manifest)
    except Exception:
        raise PreflightError('source', 'sealed-source-or-lock-mismatch', 'Restore the exact clean checkout, dependency locks, schema and web/cron identities from the sealed manifest.') from None


def install(environment, root):
    """Actual Render artifact command, no alternate installer or validation bypass.

    Caller must use a disposable /opt/render/project/src checkout. Environment is
    an allowlist: no inherited production DB/Square/credential values enter child.
    """
    require(Path(root).resolve() == Path('/opt/render/project/src'), 'rehearsal', 'isolated-render-root-required', 'Run inside the documented disposable container at /opt/render/project/src.')
    env = {k:environment[k] for k in REQUIRED}
    env.update(PATH=os.environ['PATH'], HOME=os.environ.get('HOME','/tmp'), PYTHONDONTWRITEBYTECODE='1', RENDER_GIT_COMMIT=environment['RENDER_GIT_COMMIT'])
    result = subprocess.run(INSTALL_COMMAND, shell=True, cwd=root, env=env, capture_output=True, timeout=180)
    require(result.returncode == 0, 'advisory-artifact', 'artifact-installation-failed', 'Check artifact availability, checksum, embedded source binding and SQLite contract; no deploy is permitted.')
    return {'status':'PASS','installed_projection_sha256':sha256((Path(root)/'.forecast-stage4/product.sqlite3').read_bytes())}


def repackage(projection, original_manifest, expected_projection_sha256, revision, destination):
    """Offline metadata-only repackage. Original projection and archive stay intact."""
    require(bool(re.fullmatch('[a-f0-9]{40}', revision or '')), 'source','full-sha-required','Select the exact candidate.')
    data = Path(projection).read_bytes()
    require(sha256(data) == expected_projection_sha256 == original_manifest.get('projection_sha256'),
        'projection', 'frozen-projection-mismatch', 'Use the existing independently verified frozen projection; never regenerate protected data.')
    manifest = {**original_manifest, 'web_candidate':revision}
    # Canonical member metadata and gzip header make identical inputs reproducible.
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, content in [('product.sqlite',data), ('release-manifest.json',json.dumps(manifest,sort_keys=True,separators=(',',':'),allow_nan=False).encode())]:
                member=tarfile.TarInfo(name);member.size=len(content);member.mode=0o400;member.mtime=0
                archive.addfile(member,io.BytesIO(content))
    content=stream.getvalue()
    with open(destination,'xb',opener=lambda p,f:os.open(p,f,0o600)) as output:output.write(content)
    return {'revision':revision,'projection_sha256':expected_projection_sha256,'projection_bytes':len(data),'archive_sha256':sha256(content),'archive_bytes':len(content)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--environment',type=Path,required=True,help='Private allowlisted artifact environment JSON; never committed.')
    parser.add_argument('--build-command',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--install',action='store_true',help='Also execute the exact artifact command inside the isolated Render-root container.')
    args=parser.parse_args()
    try:
        manifest=json.loads(args.manifest.read_text());source(args.root,manifest)
        environment=json.loads(args.environment.read_text())
        report=configuration(environment,manifest['revision'],args.build_command.read_text().strip())
        require(environment.get('RENDER_GIT_COMMIT')==manifest['revision'], 'source','platform-revision-mismatch','Use the sealed exact SHA for the platform revision.')
        report={'status':'INCONCLUSIVE','deployment_ready':False,'scope':'source-and-configuration-only; remote artifact NOT qualified',**report}
        if args.install:
            report['installation']=install(environment,args.root);report['status']='PASS';report['scope']='source-configuration-and-real-artifact-installation';report['deployment_ready']=False
    except PreflightError as exc:
        report={'status':'FAIL','component':exc.component,'code':exc.code,'required_action':exc.action}
    except Exception:
        report={'status':'FAIL','component':'preflight-input','code':'invalid-or-unavailable-input','required_action':'Provide complete reviewed inputs; inspect private diagnostics without publishing secrets.'}
    with open(args.output,'x',opener=lambda p,f:os.open(p,f,0o600)) as stream:json.dump(report,stream,indent=2)
    print(json.dumps(report))
    return 0 if report['status']=='PASS' else 2


if __name__=='__main__':raise SystemExit(main())
