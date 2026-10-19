"""Provider contracts and bounded GET transport. No deployment or run mutations."""
import json
import socket
import http.client
import time
import urllib.error
import urllib.request
from scripts.release.verification_errors import (GateFailure, InfrastructureUnavailable,
    ProviderEvidenceError, AuthenticationRequired, PendingEvidence)
from scripts.release.rfc3339 import provider_time

PROGRESS = {'created','queued','build_in_progress','pre_deploy_in_progress','update_in_progress'}
FAILED = {'build_failed','pre_deploy_failed','update_failed','canceled','deactivated'}


def deploy_state(deploy, revision):
    if not isinstance(deploy,dict) or not isinstance(deploy.get('commit'),dict):
        raise ProviderEvidenceError('Malformed deploy response')
    if deploy['commit'].get('id') != revision:
        raise GateFailure('Unexpected deployed revision')
    status=deploy.get('status')
    if status in PROGRESS:return 'WAIT'
    if status in FAILED:raise GateFailure('Deployment terminated: '+status)
    if status!='live':raise ProviderEvidenceError('Unknown Render deployment status')
    if not deploy.get('id') or not deploy.get('finishedAt'):
        raise ProviderEvidenceError('Live deployment lacks identity/completion time')
    provider_time(deploy['finishedAt'])
    return 'PASS'


def get_json(url, *, headers=None, timeout=60, clock=time.monotonic, sleep=time.sleep, open_url=None):
    """Retry only idempotent GET infrastructure failures, within one deadline."""
    if open_url is None:
        from scripts.release.verify_runtime import NoRedirect
        open_url=urllib.request.build_opener(NoRedirect()).open
    deadline=clock()+timeout
    while clock()<deadline:
        try:
            with open_url(urllib.request.Request(url,headers=headers or {},method='GET'),timeout=min(15,deadline-clock())) as r:
                try:return json.load(r)
                except (ValueError,UnicodeError):raise ProviderEvidenceError('Malformed provider JSON') from None
        except urllib.error.HTTPError as exc:
            if exc.code in (301,302,303,307,308,401,403):raise AuthenticationRequired('Provider authentication requires normal renewal') from None
            if exc.code not in (429,500,502,503,504):raise ProviderEvidenceError('Provider HTTP status '+str(exc.code)) from None
            retry=exc.headers.get('Retry-After','') if exc.headers else ''
            delay=min(10,int(retry)) if retry.isdigit() else 3
        except (urllib.error.URLError,TimeoutError,socket.timeout,ConnectionError,http.client.IncompleteRead):delay=3
        sleep(min(delay,max(0,deadline-clock())))
    raise InfrastructureUnavailable('Provider GET unavailable within bounded deadline')


def collect_logs(get, query, max_pages=20):
    import urllib.parse
    query=dict(query);logs=[];seen=set()
    for _ in range(max_pages):
        page=get('logs?'+urllib.parse.urlencode(query))
        if not isinstance(page,dict) or not isinstance(page.get('logs'),list) or type(page.get('hasMore')) is not bool:
            raise ProviderEvidenceError('Malformed log page')
        logs.extend(page['logs'])
        if not page['hasMore']:return logs
        start,end=page.get('nextStartTime'),page.get('nextEndTime')
        provider_time(start);provider_time(end)
        cursor=(start,end)
        if cursor in seen:raise ProviderEvidenceError('Repeated log pagination cursor')
        seen.add(cursor);query.update(startTime=start,endTime=end)
    raise ProviderEvidenceError('Bounded log pagination exceeded')


def check_session(status, *, owner_id, now, minimum_remaining=120):
    if not isinstance(status,dict):raise ProviderEvidenceError('Malformed session status')
    if status.get('authenticated') is not True or status.get('principal_id')!=owner_id:
        raise AuthenticationRequired('Fresh authorized owner login required')
    remaining=provider_time(status.get('expires_at'))-provider_time(now)
    if remaining<=minimum_remaining:raise AuthenticationRequired('Owner session expired or too close to expiry')
    return {'status':'PASS','remaining_seconds':float(remaining)}
