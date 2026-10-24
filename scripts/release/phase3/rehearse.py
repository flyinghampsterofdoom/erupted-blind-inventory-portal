"""Deterministic fault exercises; evidence is explicitly simulated, never production."""
import argparse
import json
import time
from pathlib import Path
from scripts.release.phase3.manifest import seal, validate, VERSION, SUITE, write_once
from scripts.release.phase3.simulation import Simulation, Interrupted
from scripts.release.phase3.coordinator import Coordinator
from scripts.release.phase3.journal import Journal, Busy
from scripts.release.phase3.verification import acceptance, login_redirect


def fixture_manifest():
    # Synthetic source identities; actual repository candidate is prepared separately.
    revision='a'*40;source='c'*64
    return validate(seal({'format':VERSION,'revision':revision,'source_tree_sha256':source,
        'backup_requirement':'isolated-restore','artifact':{'format':'git-archive-tar','revision':revision,'sha256':'d'*64},
        'lockfiles':{'requirements.txt':'e'*64,'requirements-test.txt':'f'*64},
        'schema':{'from':'fixture_1','expected':'fixture_2','candidate_supports':['fixture_2'],
                  'required_migrations':['fixture_2']},
        'roles':{r:{'revision':revision,'source_tree_sha256':source} for r in ('web','cron')},
        'baseline':{'revision':'b'*40,'source_tree_sha256':'1'*64,'artifact_sha256':'3'*64,'supports':['fixture_1','fixture_2']},
        'maintenance_required':True,'compatibility_review':'synthetic-schema-contract-v1',
        'verification':{'version':SUITE,'sha256':'2'*64},'built_at':'2026-10-24T00:00:00+00:00'}))


SCENARIOS=('successful-release','failed-deployment','failed-migration','incorrect-verifier-assertion',
    'header-casing','provider-timeout','network-interruption','web-cron-mismatch',
    'interrupted-resumed','compatible-rollback','incompatible-forward',
    'maintenance-restore-failed','cron-restore-failed','concurrent-release','failed-rollback',
    'application-unready','unknown-schema-held','failed-ci','missing-backup','lost-deploy-response','deployment-timeout','natural-cron-timeout')


def scenario(name, directory):
    start=time.monotonic();directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    m=fixture_manifest()
    observations={}
    faults={'failed-deployment':{'deploy-web':'failure'},'failed-migration':{'migrate':'migration'},
        'provider-timeout':{'deploy-web':'timeout'},'network-interruption':{'observe':'network'},
        'interrupted-resumed':{'after-deploy-web':'interrupt'},
        'compatible-rollback':{'deploy-web':'failure'},'incompatible-forward':{'deploy-web':'failure'},
        'maintenance-restore-failed':{'restore-maintenance':'failure'},
        'cron-restore-failed':{'restore-cron':'failure'},'failed-rollback':{'deploy-web':'failure'},
        'deployment-timeout':{'wait-web':'timeout'},'natural-cron-timeout':{'natural-cron':'timeout'},'unknown-schema-held':{'migrate':'migration'},'lost-deploy-response':{'after-deploy-web':'network'}}.get(name,{})
    if name=='incompatible-forward':
        m['baseline']['supports']=['fixture_1'];m=seal({k:v for k,v in m.items() if k!='manifest_id'})
    p=Simulation(directory/'provider.json',m,faults=faults)
    if name=='failed-ci':p.state['ci_success']=False;p.save()
    if name=='missing-backup':p.state['backup_verified']=False;p.save()
    if name=='application-unready':p.state['ready']=False;p.save()
    def functional():
        if name=='incorrect-verifier-assertion':assert False,'Incorrect test expectation'
        if name=='web-cron-mismatch':
            p.state['cron']='9'*40;p.save()
        return acceptance(p.fetch,p.principals)
    c=Coordinator(m,p,directory/'journal',functional=functional)
    if name=='header-casing':
        # Exact defect: ordinary dict(Location) misses valid lowercase header.
        historical={'location':'/login?return_to=%2Fv2%2Fscheduling%2Fweek'}
        assert historical.get('Location','') == ''
        observations['historical_case_sensitive_lookup_failed']=True
        for spelling in ('location','Location','LOCATION','lOcAtIoN'):
            assert login_redirect(303,{spelling:historical['location']})
    if name=='concurrent-release':
        with Journal(directory/'journal',m['manifest_id'],c.lock_path):
            try:c.run()
            except Busy:observations['concurrent_attempt_rejected']=True
            else:raise AssertionError('Concurrent release was allowed')
    try:result=c.run()
    except Interrupted:
        first=p.observe();assert first['web']==m['revision'] and first['cron']==m['baseline']['revision']
        # Fresh provider and coordinator process reconstruct state solely from disk.
        p=Simulation(directory/'provider.json',m)
        c=Coordinator(m,p,directory/'journal');result=c.run()
        assert len([x for x in p.state['mutations'] if x['action']=='deploy-web'])==1
        observations['resumed_from_disk_without_duplicate_submission']=True
    if name in ('compatible-rollback','failed-rollback','incompatible-forward'):
        assert result['status']=='FAILED'
        if name=='incompatible-forward':
            assert result['decision']['choice']=='forward-only'
            refused=c.recover('rollback');assert refused['status']=='FAILED'
            assert not any(x['key'].split(':')[1]=='recovery-rollback' for x in p.state['mutations'])
            result=c.recover('forward')
        else:
            if name=='failed-rollback':p.faults['deploy-web']='failure'
            result=c.recover('rollback')
    if name=='unknown-schema-held':
        assert result['decision']['choice']=='hold-unknown-schema'
        assert c.recover('rollback')['status']=='FAILED'
        result=c.recover('forward');assert result['status']=='FAILED'
    s=p.observe()
    expected={
        'successful-release':'SUCCESS','header-casing':'SUCCESS','concurrent-release':'SUCCESS',
        'interrupted-resumed':'SUCCESS','incorrect-verifier-assertion':'FAILED_VERIFICATION_RESTORED',
        'compatible-rollback':'RECOVERED','incompatible-forward':'RECOVERED'}
    assert result['status']==expected.get(name,'FAILED'),(name,result)
    if result['status'] in ('SUCCESS','RECOVERED','FAILED_VERIFICATION_RESTORED'):
        assert not s['maintenance'] and not s['cron_suspended']
        assert s['web']==s['cron']
    if name=='compatible-rollback':assert s['web']==m['baseline']['revision'] and s['schema']=='fixture_2'
    if name=='incompatible-forward':assert s['web']==m['revision'] and s['schema']=='fixture_2'
    if name in ('failed-migration','unknown-schema-held'):assert s['maintenance'] and s['cron_suspended'] and s['schema']=='partial-unknown'
    if name=='cron-restore-failed':assert s['maintenance'] and s['cron_suspended']
    if name=='natural-cron-timeout':assert s['maintenance'] and not s['cron_suspended']
    if name=='maintenance-restore-failed':assert s['maintenance'] and not s['cron_suspended']
    if name=='incorrect-verifier-assertion':assert result['failures'][0]['category']=='verification' and result['failures'][0]['application_health']=='unknown'
    if name in ('provider-timeout','network-interruption','lost-deploy-response','deployment-timeout','natural-cron-timeout'):
        assert result['failures'][0]['category']=='infrastructure'
    if name=='lost-deploy-response':
        assert len([x for x in s['mutations'] if x['action']=='deploy-web'])==1
        assert c.run()['status']=='FAILED'  # no automatic re-submit after ambiguous response
    record={**result,'scenario':name,'assertions':'PASS','duration_seconds':time.monotonic()-start,
        'observations':observations,'state':{k:s[k] for k in ('schema','web','cron','maintenance','cron_suspended')},
        'provider_api_calls':p.calls,'mutation_count':len(s['mutations']),
        'simulated_manual_actions':1+(name in ('compatible-rollback','incompatible-forward','failed-rollback','interrupted-resumed')),
        'additional_infrastructure_cost':0}
    write_once(directory/'result.json',record);return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    a=parser.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    results=[scenario(name,a.output/name) for name in SCENARIOS]
    write_once(a.output/'summary.json',{'mode':'simulation-only','production_mutations':0,'scenarios':results})
    print(json.dumps({'scenarios':len(results),'passed':len(results),'production_mutations':0}))


if __name__=='__main__':main()
