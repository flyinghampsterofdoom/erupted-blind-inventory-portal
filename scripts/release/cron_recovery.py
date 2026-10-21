"""Explicitly invoked recovery primitive with injectable provider; no CLI side effects.

No native cron rollback or manual-run endpoint exists in this module. A caller
must supply an owner-approved baseline, uniquely pinned temporary branch, config
fingerprint function, and actual natural-run verification callback. Submission is
never retried. A journal must be retained outside the checkout.
"""
import time
import json
import os
import re
import hashlib
from pathlib import Path


class RecoveryJournal:
    def __init__(self, path):
        self.path=Path(path)
        self.data=json.loads(self.path.read_text()) if self.path.exists() else {}
    def get(self,key,default=None):return self.data.get(key,default)
    def __getitem__(self,key):return self.data[key]
    def __setitem__(self,key,value):
        self.data[key]=value
        temp=self.path.with_suffix('.tmp')
        with open(temp,'w',opener=lambda p,f:os.open(p,f,0o600)) as stream:
            json.dump(self.data,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,self.path)

from scripts.release.render_contract import deploy_state
from scripts.release.verify_runtime import GateFailure,InfrastructureUnavailable,PendingEvidence


def recover(api, *, service_id, revision, recovery_branch, branch_tip,
            config_fingerprint, verify_restored_run, journal, clock=time.monotonic,
            sleep=time.sleep, timeout=900):
    if not isinstance(revision,str) or not re.fullmatch('[0-9a-f]{40}',revision):
        raise GateFailure('Recovery requires the full approved commit SHA')
    if journal.get('submitted'):
        raise GateFailure('Recovery already submitted; inspect journal, never submit twice')
    before=api('services/'+service_id)
    if before.get('type')!='cron_job' or before.get('autoDeploy')!='no':
        raise GateFailure('Recovery requires existing cron and autodeploy off')
    original=before['branch'];fingerprint=config_fingerprint(before)
    if recovery_branch==original:raise GateFailure('Use a distinct temporary baseline branch')
    if branch_tip(recovery_branch)!=revision:raise GateFailure('Recovery branch is not pinned to approved baseline')
    try:
        api('services/'+service_id,'PATCH',{'branch':recovery_branch})
        if branch_tip(recovery_branch)!=revision:raise GateFailure('Recovery branch changed before submission')
        journal['submitted']=True  # Persist before POST; ambiguous network errors cannot trigger retries.
        deploy=api('services/'+service_id+'/deploys','POST',{'clearCache':'do_not_clear'})
        journal['deploy_id']=deploy['id']
        if deploy.get('commit',{}).get('id')!=revision:raise GateFailure('Recovery returned unexpected commit')
        deadline=clock()+timeout
        while clock()<deadline:
            deploy=api('services/'+service_id+'/deploys/'+journal['deploy_id'])
            if deploy_state(deploy,revision)=='PASS':break
            sleep(min(5,max(0,deadline-clock())))
        else:raise InfrastructureUnavailable('Recovery readiness deadline exceeded')
        journal['live']=True
    finally:
        # Restore source configuration even after a failed/ambiguous request. Do
        # not force/reset any Git ref or cancel a naturally running cron job.
        api('services/'+service_id,'PATCH',{'branch':original})
    after=api('services/'+service_id)
    if after['branch']!=original or config_fingerprint(after)!=fingerprint:
        raise GateFailure('Cron configuration differs after recovery')
    proof=verify_restored_run(deploy)
    if not isinstance(proof,dict) or proof.get('status')!='PASS' or proof.get('revision')!=revision:
        raise GateFailure('Recovery lacks exact revision and successful natural-run evidence')
    journal['verified']=True
    return {'status':'PASS','deploy_id':deploy['id'],'revision':revision,'natural_run':proof}


def verify_legacy_restoration(*, service, deploy, deployments, events, revision):
    """Read-only legacy recovery: provider artifact identity + actual natural run.

Legacy 8d227f8 has no app runtime-proof logger. This does NOT assert a runtime
source digest or replace verify_cron_logs for releases that include that logger.
"""
    from scripts.release.verify_runtime import provider_time, ProviderEvidenceError
    if service.get('type')!='cron_job' or service.get('serviceDetails',{}).get('schedule')!='0 * * * *':
        raise GateFailure('Unexpected restored cron configuration')
    deploy_state(deploy,revision);cutoff=provider_time(deploy['finishedAt'])
    live=[x['deploy'] for x in deployments if x.get('deploy',{}).get('status')=='live']
    if len(live)!=1 or live[0]['id']!=deploy['id'] or live[0]['commit']['id']!=revision:
        raise GateFailure('Restored artifact is not the sole current live deployment')
    for item in deployments:
        d=item['deploy']
        if d['id']!=deploy['id'] and provider_time(d['createdAt'])>cutoff:
            raise GateFailure('Concurrent/later deployment prevents recovery attribution')
    starts={};complete=[]
    for item in events:
        e=item.get('event',{})
        if e.get('serviceId')!=service['id']:continue
        stamp=provider_time(e.get('timestamp'))
        if stamp<=cutoff:continue
        detail=e.get('details',{});run=detail.get('cronJobRunId')
        if e.get('type')=='cron_job_run_started':
            if not run:raise ProviderEvidenceError('Cron start lacks run identifier')
            starts[run]=stamp
        elif e.get('type')=='cron_job_run_ended':
            complete.append((stamp,run,detail.get('status')))
    for end,run,status in sorted(complete,reverse=True):
        if run in starts and starts[run]<end:
            if status!='successful':raise GateFailure('Restored natural run did not succeed')
            return {'status':'PASS','revision':revision,'deploy_id':deploy['id'],
                    'run_id':run,'evidence_level':'provider_active_artifact_and_matched_natural_execution',
                    'runtime_source_digest':'UNAVAILABLE in legacy release'}
    raise PendingEvidence('Restoration is live but has no matched successful natural run yet')


def configuration_fingerprint(service, env_variables):
    """Hash effective configuration without exposing credential values."""
    details=service.get('serviceDetails',{})
    payload={k:service.get(k) for k in ('type','autoDeploy','suspended','environmentId')}
    payload['details']={k:details.get(k) for k in ('schedule','envSpecificDetails','runtime','plan','region','numInstances')}
    payload['environment']=env_variables
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
