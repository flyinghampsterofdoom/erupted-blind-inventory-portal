"""Independent readiness/acceptance; unknown evidence is never application PASS."""
from dataclasses import dataclass, asdict
from urllib.parse import urlsplit
from scripts.release.phase3.manifest import SUITE


@dataclass
class Finding:
    status: str
    category: str
    code: str
    application_health: str = 'unknown'

    def dict(self): return asdict(self)


class Failure(Exception):
    def __init__(self, category, code, health='unknown'):
        self.finding = Finding('FAIL', category, code, health)
        super().__init__(code)


def application(code): raise Failure('application', code, 'unhealthy')
def malformed(code): raise Failure('verification', code)


def headers(values):
    """Normalize names without losing ambiguity to a lossy dict conversion."""
    result = {}
    pairs = values.items() if hasattr(values, 'items') else values
    for name,value in pairs:
        name=name.lower()
        if name in result and result[name] != value:
            malformed('ambiguous-header-'+name)
        result[name] = value
    return result


def login_redirect(status, values):
    if status not in (302,303): application('anonymous-boundary-status')
    location = headers(values).get('location')
    if not isinstance(location, str): malformed('missing-redirect-location')
    u=urlsplit(location)
    if u.scheme or u.netloc or u.path != '/login': application('anonymous-boundary-target')
    return True


def readiness(evidence, expected, schema):
    if not isinstance(evidence, dict) or evidence.get('contract') != 'erupted-readiness-v1':
        malformed('unsupported-readiness-contract')
    required={'process','database','schema','source'}
    if set(evidence.get('dependencies',{})) != required:
        malformed('missing-readiness-dependencies')
    if not all(v is True for v in evidence['dependencies'].values()):
        application('required-dependency-unready')
    if evidence.get('schema') != schema: application('schema-incompatible')
    for role in ('web','cron'):
        proof=evidence.get(role)
        if not isinstance(proof,dict): malformed('missing-'+role+'-identity')
        if (proof.get('revision') != expected['revision'] or
            proof.get('source_tree_sha256') != expected['source_tree_sha256'] or
            proof.get('status') != 'VERIFIED' or proof.get('role') != role):
            application('web-cron-identity-mismatch')
    return Finding('PASS','none','ready','ready').dict()


# Deliberately fixed GET scope. No login submission, create/refresh/generate or cron run.
READS = {
    'anonymous':('/v2/scheduling/week',),
    'owner':('/session-status','/v2/scheduling/api/board','/management/purchasing','/management/sessions'),
    'lead':('/session-status','/v2/scheduling/week','/management/purchasing'),
    'store':('/session-status','/management/purchasing'),
}


def acceptance(fetch, principals):
    """fetch(role,path) must use existing ordinary sessions and disable redirects.

    Bodies are reduced to boolean observations, never persisted as customer data.
    Local fixtures and TestClient exercise this same parser. No live client ships.
    """
    if set(principals) != {'owner','lead','store'} or len(set(principals.values())) != 3:
        malformed('invalid-session-expectations')
    receipts=[]
    for role, paths in READS.items():
        for path in paths:
            response=fetch(role,path)
            if not isinstance(response,dict) or not isinstance(response.get('status'),int):
                malformed('unsupported-http-response')
            status=response['status'];body=response.get('body')
            if role == 'anonymous': login_redirect(status,response.get('headers',{}))
            elif path == '/management/purchasing' and role in ('lead','store'):
                if status != 403: application('owner-authorization-boundary')
            else:
                if status in (401,302,303,307,308):
                    raise Failure('verification','normal-session-unavailable')
                if status != 200: application('functional-http-'+str(status))
                if path == '/session-status':
                    if not isinstance(body,dict) or body.get('authenticated') is not True or body.get('principal_id') != principals[role]:
                        application('authenticated-session-missing')
                elif path == '/v2/scheduling/api/board':
                    if not isinstance(body,dict) or not isinstance(body.get('employees'),list) or not isinstance(body.get('shifts'),list):
                        malformed('unsupported-scheduling-body')
                else:
                    markers={'/v2/scheduling/week':'data-schedule-board',
                        '/management/purchasing':'/management/purchasing/generate',
                        '/management/sessions':'Management Sessions'}
                    if not isinstance(body,str) or markers[path] not in body:
                        malformed('unsupported-page-body')
            receipts.append({'role':role,'path':path,'status':status,'passed':True})
    return {'status':'PASS','version':SUITE,'checks':receipts}


def verifier_health():
    # Independent finite contract self-check; defects cannot be called app failure.
    for name in ('Location','location','LOCATION','lOcAtIoN'):
        login_redirect(303,{name:'/login?return_to=%2Fv2%2Fscheduling%2Fweek'})
    return True


def execute(check):
    """Assertions/program defects default to verifier failure, never app failure."""
    try:
        result=check()
        if not isinstance(result,dict) or result.get('status') != 'PASS':
            malformed('invalid-verifier-result')
        return result
    except Failure: raise
    except (TimeoutError,ConnectionError,OSError):
        raise Failure('infrastructure','verification-transport-unavailable') from None
    except Exception:
        # No response bodies/URLs/exception messages (may contain credentials).
        raise Failure('verification','verifier-implementation-defect') from None
