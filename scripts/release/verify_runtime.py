"""GET-only deployment verifier. Never deploys, rolls back, or invokes a cron run."""
from __future__ import annotations
import argparse
import http.cookiejar
import json
import socket
import time
from datetime import datetime
from pathlib import Path
import urllib.error
import urllib.request


class GateFailure(RuntimeError):
    pass


class InfrastructureUnavailable(GateFailure):
    pass


def validate_identity(proof, *, revision, digest, service_id, role):
    if proof.get('revision') != revision:
        raise GateFailure('Unexpected application revision')
    if (proof.get('status') != 'VERIFIED' or proof.get('source_tree_sha256') != digest
            or proof.get('role') != role or proof.get('service_id') != service_id):
        raise GateFailure('Runtime source identity does not match reviewed build')
    if proof.get('platform_revision') != revision:
        raise GateFailure('Platform and built runtime identities disagree')


def validate_deploy(deploy, revision):
    if deploy.get('status') != 'live' or deploy.get('commit', {}).get('id') != revision:
        raise GateFailure('Render deployment is not live at the approved revision')


def verify_web(fetch, *, revision, previous_revision, digest, service_id, schema,
               owner_id, timeout=120, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    observations = []
    while clock() < deadline:
        try:
            proof = fetch(min(15, max(.1, deadline-clock())))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 302, 303, 307):
                raise GateFailure('Owner authentication/authorization failed') from None
            if exc.code not in (404, 429, 502, 503, 504):
                raise GateFailure('Application HTTP failure: '+str(exc.code)) from None
            observations.append({'kind': 'infrastructure', 'http': exc.code})
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
            observations.append({'kind': 'infrastructure', 'error': 'connection unavailable'})
        else:
            if proof.get('revision') == previous_revision:
                observations.append({'kind': 'stabilizing', 'revision': previous_revision})
            else:
                validate_identity(proof, revision=revision, digest=digest, service_id=service_id, role='web')
                if (not proof.get('files_still_match') or proof.get('schema') != schema
                        or proof.get('principal_id') != owner_id or not proof.get('owner_authorized')
                        or proof.get('engine_version') != 'purchasing-v3-p2-exact-20261018'):
                    raise GateFailure('Application/schema/owner safety check failed')
                return {'status': 'PASS', 'observations': observations, 'runtime': proof}
        sleep(min(5, max(0, deadline-clock())))
    if observations and observations[-1]['kind'] == 'infrastructure':
        raise InfrastructureUnavailable('Infrastructure unavailable within stabilization deadline; no application pass')
    raise GateFailure('Application revision did not stabilize within deadline')


def verify_cron_logs(logs, *, revision, digest, service_id, deployed_at, last_success):
    """Authenticated Render log records, scoped to this service and new deploy."""
    cutoff = datetime.fromisoformat(deployed_at.replace('Z', '+00:00'))
    success = datetime.fromisoformat(last_success.replace('Z', '+00:00')) if last_success else None
    for log in reversed(logs):
        try:
            proof = json.loads(log['message'])
            stamp = datetime.fromisoformat(log['timestamp'].replace('Z', '+00:00'))
        except (ValueError, KeyError, TypeError):
            continue
        labels = {l['name']: l['value'] for l in log.get('labels', [])}
        if stamp < cutoff or labels.get('resource') != service_id:
            continue
        if proof.get('event') != 'ERUPTED_RUNTIME_IDENTITY' or proof.get('revision') != revision:
            continue
        validate_identity(proof, revision=revision, digest=digest, service_id=service_id, role='cron')
        if success is None or success < stamp:
            raise GateFailure('Cron runtime seen; successful scheduled completion is not yet verified')
        return {'status': 'PASS', 'runtime': proof, 'log_timestamp': log['timestamp'], 'scheduled_run_success': last_success}
    raise GateFailure('No actual new cron runtime proof; a deployment API SHA alone is insufficient')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', required=True); p.add_argument('--cookie-file', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True); p.add_argument('--previous-revision', required=True)
    p.add_argument('--service-id', required=True); p.add_argument('--deploy-evidence', type=Path, required=True)
    p.add_argument('--owner-id', type=int, required=True); p.add_argument('--schema', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if not args.url.startswith('https://') or args.cookie_file.stat().st_mode & 0o077:
        p.error('HTTPS and a private owner cookie file (0600) are required')
    manifest = json.loads(args.manifest.read_text()); validate_deploy(json.loads(args.deploy_evidence.read_text()), manifest['revision'])
    jar = http.cookiejar.MozillaCookieJar(str(args.cookie_file)); jar.load(ignore_discard=True)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
    def fetch(timeout):
        req = urllib.request.Request(args.url.rstrip('/')+'/management/deployment/runtime', headers={'Cache-Control':'no-cache'})
        with opener.open(req, timeout=timeout) as response:
            return json.loads(response.read(65536))
    try:
        report = verify_web(fetch, revision=manifest['revision'], previous_revision=args.previous_revision,
                            digest=manifest['source_tree_sha256'], service_id=args.service_id,
                            schema=args.schema, owner_id=args.owner_id)
    except (GateFailure, ValueError) as exc:
        report = {'status': 'STOP', 'category': type(exc).__name__, 'reason': str(exc)}
    args.output.write_text(json.dumps(report, indent=2)); print(json.dumps({'status': report['status'], 'category': report.get('category')}))
    return 0 if report['status']=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
