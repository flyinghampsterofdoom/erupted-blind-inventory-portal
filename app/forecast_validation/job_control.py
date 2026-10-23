"""Independent bounded Render controller. Explicitly authorized manual jobs only.

No HTTP client, credentials, timer installation or recurring defaults. Injected
API calls are exercised with fakes locally; a prepared permit cannot dispatch.
"""
from datetime import datetime,timezone
import re
from sqlalchemy import select
from .contracts import EvidenceError,digest,stamp
from .qualification import require_execution,records,receipt
from .notifications import exclusive

BASE_SERVICE='crn-da6pd2qd0e5s73dbudr0'
PLAN='plan-crn-005'
TERMINAL=frozenset({'succeeded','failed','canceled'})


def job_request(permit_id):
    if not re.fullmatch('[0-9a-f]{64}',permit_id):raise EvidenceError('Invalid execution permit identity')
    # The bootstrap clears the inherited base environment before importing app.
    return {'planId':PLAN,'startCommand':'python scripts/forecast_qualification_bootstrap.py --permit-id '+permit_id}


class Controller:
    def __init__(self, ledger, api, *, approved_sha256='', deployment_sha='', now=None):
        self.ledger=ledger;self.api=api;self.approved_sha256=approved_sha256
        self.deployment_sha=deployment_sha;self.clock=now or (lambda:datetime.now(timezone.utc))

    def dispatch(self, permit, *, readiness):
        require_execution(permit,approved_sha256=self.approved_sha256,
                          deployment_sha=self.deployment_sha,now=self.clock())
        required=('build_compatible','billing_approved','database_authorized','backup_verified',
                  'observer_ready','source_coverage_complete','independent_monitor_ready','cron_idle')
        if any(readiness.get(k) is not True for k in required):
            raise EvidenceError('Independent commissioning prerequisites are not verified')
        deploys=self.api('services/'+BASE_SERVICE+'/deploys?limit=20',method='GET')
        if any(x.get('deploy',x).get('status') in ('created','queued','build_in_progress','pre_deploy_in_progress','update_in_progress') for x in deploys):
            raise EvidenceError('Base service has an intervening deployment')
        built=next((x.get('deploy',x) for x in deploys if x.get('deploy',x).get('status') in ('live','deactivated')),None)
        if not built or built.get('commit',{}).get('id')!=self.deployment_sha:
            raise EvidenceError('Latest successful base build differs from approved candidate')
        ident=digest(permit);op='render-job:'+ident;body=job_request(ident)
        with exclusive(self.ledger,op):
            with self.ledger.engine.connect() as c:old=records(c,op,maximum=500)
            if old:raise EvidenceError('Dispatch already attempted; reconcile instead of creating another job')
            with self.ledger.engine.begin() as c:
                receipt(c,op,'CREATE_INTENT',{'service_id':BASE_SERVICE,'request_sha256':digest(body)},key='create',now=self.clock())
            try:job=self.api('services/'+BASE_SERVICE+'/jobs',method='POST',body=body)
            except Exception:
                with self.ledger.engine.begin() as c:
                    receipt(c,op,'CREATE_UNCONFIRMED',{'reconciliation_required':True},key='unknown',now=self.clock())
                return {'status':'CREATE_UNCONFIRMED','permit_id':ident}
            return self._bind(ident,job)

    def _bind(self,ident,job):
        job=job.get('job',job)
        if (not re.fullmatch(r'job-[0-9a-z]{20}',job.get('id',''))
                or job.get('serviceId')!=BASE_SERVICE or job.get('startCommand')!=job_request(ident)['startCommand']
                or job.get('planId')!=PLAN or job.get('status') not in TERMINAL|{'pending','running'}):
            raise EvidenceError('Render acknowledgement does not match the authorized job')
        with self.ledger.engine.begin() as c:
            receipt(c,'render-job:'+ident,'JOB_BOUND',{'job_id':job['id'],'service_id':BASE_SERVICE},key='binding',now=self.clock())
        return {'status':job['status'],'job_id':job['id'],'permit_id':ident}

    def reconcile(self,ident):
        # GET-only recovery from a lost creation response, never a second POST.
        matches=[];cursor=None
        for _ in range(5):
            route='services/'+BASE_SERVICE+'/jobs?limit=100'+('&cursor='+cursor if cursor else '')
            rows=self.api(route,method='GET')
            for item in rows:
                value=item.get('job',item)
                if value.get('startCommand')==job_request(ident)['startCommand']:matches.append(value)
            if len(rows)<100:break
            cursor=rows[-1].get('cursor')
            if not cursor:raise EvidenceError('Job reconciliation pagination is incomplete')
        else:raise EvidenceError('Job history exceeds reconciliation bound')
        if len(matches)!=1:raise EvidenceError('Ambiguous or absent dispatch requires Owner review; no automatic re-create')
        return self._bind(ident,matches[0])

    def supervise_once(self,ident, *, observe, limits, backup_receipt=None):
        from .safeguards import require_capacity,Yield
        op='render-job:'+ident;current=self.clock()
        with self.ledger.engine.connect() as c:history=records(c,op,maximum=500)
        bound=next((r for r in history if r['status']=='JOB_BOUND'),None)
        intent=next((r for r in history if r['status']=='CREATE_INTENT'),None)
        if not bound or not intent:raise EvidenceError('Job identity has not been reconciled')
        job_id=bound['value']['job_id'];route='services/'+BASE_SERVICE+'/jobs/'+job_id
        job=self.api(route,method='GET');job=job.get('job',job)
        if job.get('id')!=job_id or job.get('status') not in TERMINAL|{'pending','running'}:
            raise EvidenceError('Invalid job status observation')
        if job['status'] in TERMINAL:return job['status']
        reason=None
        age=(stamp(current)-stamp(intent['recorded_at'])).total_seconds()
        if age<0:raise EvidenceError('Controller clock is before dispatch')
        if age>=900 or job['status']=='pending' and age>=120:reason='JOB_DEADLINE'
        try:require_capacity(observe(),limits,now=current)
        except Exception:reason=reason or 'RESOURCE_OR_OBSERVER_FAILURE'
        if reason:
            try:self.api(route+'/cancel',method='POST',body={})
            except Exception:
                with self.ledger.engine.begin() as c:
                    receipt(c,op,'CANCEL_UNCONFIRMED',{'reason':reason},key='cancel:'+str(len(history)),now=current)
                return 'CANCEL_UNCONFIRMED'
            with self.ledger.engine.begin() as c:
                receipt(c,op,'CANCEL_REQUESTED',{'reason':reason},key='cancel:'+str(len(history)),now=current)
            # An acknowledgement is not evidence of termination. Next GET must
            # report canceled/failed/succeeded; alerts continue until confirmed.
            return 'CANCEL_REQUESTED'
        return job['status']


def main():
    import json
    from .qualification import GRANT
    print(json.dumps({'status':'PREPARED_NO_DISPATCH','service_id':BASE_SERVICE,'plan_id':PLAN,
                      'capacity':GRANT,'billing_approved':False,'execution_authorized':False}))
if __name__=='__main__':main()
