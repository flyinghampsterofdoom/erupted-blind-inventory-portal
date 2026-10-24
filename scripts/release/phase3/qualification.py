"""Read-only GitHub qualification, binding actual build evidence to sealed source.

PR merge-ref checks do not qualify a different branch/main SHA. Use the automatic
main-push run (or an exact-revision manual run) after the final commit exists.
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from scripts.release.phase3.manifest import validate, write_once


def verify_ci(manifest, run, jobs, identity):
    validate(manifest)
    if run.get('app_id') != 15368:
        raise ValueError('Expected GitHub Actions qualification source')
    if run.get('event') not in ('push','workflow_dispatch') or run.get('head_sha') != manifest['revision']:
        raise ValueError('CI must validate the exact final candidate, not a PR merge substitute')
    if run.get('conclusion')!='success' or run.get('status')!='completed':
        raise ValueError('Candidate CI is not complete/successful')
    names={'Plan validation','CI required', *['Validate ('+s+')' for s in (
        'fast','schedule-foundation','schedule-a','schedule-b','purchasing','inventory-access','platform-forecast','other')]}
    if {j['name'] for j in jobs} != names or not all(j['conclusion']=='success' for j in jobs):
        raise ValueError('All expanded validation jobs must succeed')
    if identity.get('revision')!=manifest['revision'] or identity.get('source_tree_sha256')!=manifest['source_tree_sha256']:
        raise ValueError('Actual CI checkout/source differs from candidate')
    return {'status':'PASS','manifest_id':manifest['manifest_id'],'revision':manifest['revision'],
        'source_tree_sha256':manifest['source_tree_sha256'],'artifact_sha256':manifest['artifact']['sha256'],
        'ci_run_id':run['id'],'ci_run_url':run['html_url'],'ci_event':run['event'],'conclusion':'success',
        'production_activation_authorized':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo',required=True);p.add_argument('--run',type=int,required=True)
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.repo!='flyinghampsterofdoom/erupted-blind-inventory-portal':raise ValueError('Unexpected GitHub repository')
    def get(path):return json.loads(subprocess.check_output(['gh','api','repos/'+a.repo+'/'+path],text=True))
    run=get('actions/runs/'+str(a.run));suite=get('check-suites/'+str(run['check_suite_id']))
    run['app_id']=suite.get('app',{}).get('id')
    jobs=get('actions/runs/'+str(a.run)+'/jobs?per_page=100')['jobs']
    artifacts=get('actions/runs/'+str(a.run)+'/artifacts?per_page=100')['artifacts']
    name='validation-fast-'+str(a.run)+'-'+str(run['run_attempt'])
    matching=[x for x in artifacts if x['name']==name and not x['expired']]
    if len(matching)!=1:raise ValueError('Exact current-attempt build evidence unavailable')
    with tempfile.TemporaryDirectory(prefix='erupted-ci-proof-') as folder:
        subprocess.run(['gh','run','download',str(a.run),'--repo',a.repo,'--name',name,'--dir',folder],check=True)
        identity=json.loads((Path(folder)/'release-identity.json').read_text())
    result=verify_ci(json.loads(a.manifest.read_text()),run,jobs,identity)
    result['github_artifact_id']=matching[0]['id']
    write_once(a.output,result);print(json.dumps(result))


if __name__=='__main__':main()
