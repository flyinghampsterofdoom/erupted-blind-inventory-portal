"""GET-only deployment verifier. Never deploys, rolls back, or invokes a cron run."""
from __future__ import annotations
import argparse
import http.cookiejar
import json
import socket
import re
import http.client
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.release.rfc3339 import timestamp
import urllib.error
import urllib.request


class GateFailure(RuntimeError):
    pass


class InfrastructureUnavailable(GateFailure):
    pass


class ProviderEvidenceError(InfrastructureUnavailable):
    """Malformed provider evidence: verification cannot pass, no app failure claim."""


class AuthenticationRequired(GateFailure):
    pass


class PendingEvidence(GateFailure):
    """Expected natural-run/readiness wait; never a PASS or automatic rollback."""


def provider_time(value):
    try:
        return timestamp(value)
    except (TypeError, ValueError, OverflowError):
        raise ProviderEvidenceError('Malformed or non-comparable provider timestamp') from None



def validate_identity(proof, *, revision, digest, service_id, role):
    if not isinstance(proof, dict):
        raise ProviderEvidenceError('Runtime response is not an object')
    if not isinstance(revision,str) or not re.fullmatch('[0-9a-f]{40}',revision) or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
        raise ProviderEvidenceError('Expected full revision and SHA256 digest')
    if proof.get('revision') != revision:
        raise GateFailure('Unexpected application revision')
    if (proof.get('status') != 'VERIFIED' or proof.get('source_tree_sha256') != digest
            or proof.get('role') != role or proof.get('service_id') != service_id):
        raise GateFailure('Runtime source identity does not match reviewed build')
    if proof.get('platform_revision') != revision:
        raise GateFailure('Platform and built runtime identities disagree')


def validate_deploy(deploy, revision):
    if not isinstance(deploy, dict) or not isinstance(deploy.get('commit'), dict):
        raise ProviderEvidenceError('Malformed deploy metadata')
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
            if exc.code in (401, 403, 301, 302, 303, 307, 308):
                raise AuthenticationRequired('Owner authentication/authorization failed; renew through normal login') from None
            if exc.code not in (404, 429, 502, 503, 504):
                raise GateFailure('Application HTTP failure: '+str(exc.code)) from None
            observations.append({'kind': 'infrastructure', 'http': exc.code})
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, http.client.IncompleteRead):
            observations.append({'kind': 'infrastructure', 'error': 'connection unavailable'})
        except (ValueError, TypeError):
            raise ProviderEvidenceError('Malformed runtime JSON response') from None
        else:
            if not isinstance(proof, dict):
                raise ProviderEvidenceError('Runtime response is not an object')
            if proof.get('revision') == previous_revision:
                if proof.get('status') not in (None, 'VERIFIED'):
                    raise GateFailure('Previous runtime reports invalid source identity')
                observations.append({'kind': 'stabilizing', 'revision': previous_revision})
            else:
                validate_identity(proof, revision=revision, digest=digest, service_id=service_id, role='web')
                if (proof.get('files_still_match') is not True or proof.get('schema') != schema
                        or proof.get('principal_id') != owner_id or proof.get('owner_authorized') is not True
                        or proof.get('engine_version') != 'purchasing-v3-p2-exact-20261018'):
                    raise GateFailure('Application/schema/owner safety check failed')
                return {'status': 'PASS', 'observations': observations, 'runtime': proof}
        sleep(min(5, max(0, deadline-clock())))
    if observations and observations[-1]['kind'] == 'infrastructure':
        raise InfrastructureUnavailable('Infrastructure unavailable within stabilization deadline; no application pass')
    raise GateFailure('Application revision did not stabilize within deadline')


def verify_cron_logs(logs, *, revision, digest, service_id, deployed_at, last_success):
    """Authenticated Render log records, scoped to this service and new deploy."""
    cutoff = provider_time(deployed_at)
    success = provider_time(last_success) if last_success else None
    if not isinstance(logs, list):
        raise ProviderEvidenceError('Provider logs must be an array')
    found = []
    timeline = []
    for log in logs:
        if not isinstance(log, dict) or not isinstance(log.get('labels'), list):
            raise ProviderEvidenceError('Malformed provider log structure')
        try:
            labels = {l['name']: l['value'] for l in log['labels']}
        except (TypeError, KeyError):
            raise ProviderEvidenceError('Malformed log labels') from None
        if labels.get('resource') != service_id:
            continue
        stamp = provider_time(log.get('timestamp'))
        if stamp < cutoff:
            continue
        message = log.get('message')
        if not isinstance(message, str):
            raise ProviderEvidenceError('Malformed log message')
        timeline.append((stamp, message))
        if 'ERUPTED_RUNTIME_IDENTITY' not in message:
            continue  # Ordinary application/platform text is not proof.
        try:
            proof = json.loads(message)
        except ValueError:
            raise ProviderEvidenceError('Malformed runtime identity JSON') from None
        if not isinstance(proof, dict) or proof.get('event') != 'ERUPTED_RUNTIME_IDENTITY':
            raise ProviderEvidenceError('Malformed runtime identity event')
        validate_identity(proof, revision=revision, digest=digest, service_id=service_id, role='cron')
        found.append((stamp, log, proof))
    if not found:
        raise PendingEvidence('Awaiting actual scheduled cron runtime proof; deployment metadata alone is insufficient')
    stamp, log, proof = max(found, key=lambda item: item[0])
    matched_completion = None
    for when, message in sorted(timeline, key=lambda item: item[0]):
        if when <= stamp:
            continue
        if 'Cron job run started' in message:
            break  # A later run cannot supply completion for this proof.
        if 'Cron job run failed' in message or 'Cron job run finished unsuccessfully' in message:
            raise GateFailure('Verified cron runtime subsequently failed its run')
        if 'Cron job run finished successfully' in message:
            matched_completion = when
            break
    # The API completion field has whole-second precision; a fast run can finish
    # in the same second as its nanosecond proof. Require an ordered provider
    # success marker for that case rather than rounding away the discrepancy.
    coarse_same_second = (success is not None and success//1 == stamp//1
                          and matched_completion is not None)
    if success is None or (success < stamp and not coarse_same_second):
        raise PendingEvidence('Cron runtime seen; successful scheduled completion is not yet verified')
    return {'status': 'PASS', 'runtime': proof, 'log_timestamp': log['timestamp'], 'scheduled_run_success': last_success}


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
    try:
        if not args.url.startswith('https://') or args.cookie_file.stat().st_mode & 0o077:
            raise AuthenticationRequired('HTTPS and a private owner cookie file (0600) are required')
        manifest = json.loads(args.manifest.read_text())
        validate_deploy(json.loads(args.deploy_evidence.read_text()), manifest['revision'])
        jar = http.cookiejar.MozillaCookieJar(str(args.cookie_file)); jar.load(ignore_discard=True)
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
        def read(path, timeout):
            req = urllib.request.Request(args.url.rstrip('/')+path, headers={'Cache-Control':'no-cache'})
            with opener.open(req, timeout=timeout) as response:
                return json.loads(response.read(65536))
        from datetime import datetime, timezone
        from scripts.release.render_contract import check_session
        try:
            session = read('/session-status', 15)
        except urllib.error.HTTPError as exc:
            if exc.code in (401,403,301,302,303,307,308):
                raise AuthenticationRequired('Owner login expired; use normal sign-in') from None
            raise InfrastructureUnavailable('Session readiness endpoint unavailable') from None
        check_session(session, owner_id=args.owner_id, now=datetime.now(timezone.utc).isoformat())
        def fetch(timeout):
            return read('/management/deployment/runtime', timeout)
        report = verify_web(fetch, revision=manifest['revision'], previous_revision=args.previous_revision,
                            digest=manifest['source_tree_sha256'], service_id=args.service_id,
                            schema=args.schema, owner_id=args.owner_id)
    except (GateFailure, ValueError, KeyError, TypeError, OSError) as exc:
        category = type(exc).__name__ if isinstance(exc, GateFailure) else 'ProviderEvidenceError'
        report = {'status': 'STOP', 'category': category,
                  'reason': str(exc) if isinstance(exc, GateFailure) else 'Invalid local/provider evidence or unavailable transport'}
    args.output.write_text(json.dumps(report, indent=2)); print(json.dumps({'status': report['status'], 'category': report.get('category')}))
    return 0 if report['status']=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
