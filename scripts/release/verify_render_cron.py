"""Inspect an existing scheduled cron runtime via authenticated Render GET APIs."""
import argparse,json,os,sys,urllib.request,urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.release.verify_runtime import validate_deploy,verify_cron_logs,GateFailure,ProviderEvidenceError,InfrastructureUnavailable,AuthenticationRequired,PendingEvidence
from scripts.release.render_contract import get_json,collect_logs,deploy_state


def main():
 p=argparse.ArgumentParser();p.add_argument('--service-id',required=True);p.add_argument('--deploy-id',required=True);p.add_argument('--manifest',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
 token=os.environ.get('RENDER_API_KEY')
 if not token:p.error('Use existing authenticated Render API access via RENDER_API_KEY; never pass a credential on the command line')
 def get(path):
  return get_json('https://api.render.com/v1/'+path,headers={'Authorization':'Bearer '+token})
 try:
  manifest=json.loads(a.manifest.read_text());service=get('services/'+a.service_id);deploy=get('services/'+a.service_id+'/deploys/'+a.deploy_id)
  if service.get('id')!=a.service_id or service.get('type')!='cron_job' or service.get('serviceDetails',{}).get('schedule')!='0 * * * *':raise GateFailure('Unexpected cron service or schedule')
  if deploy_state(deploy,manifest['revision'])=='WAIT':raise PendingEvidence('Cron deployment is still progressing')
  start=deploy['finishedAt'];query={'ownerId':service['ownerId'],'resource':a.service_id,'startTime':start,'direction':'forward','limit':100};logs=[]
  logs=collect_logs(get,query)
  result=verify_cron_logs(logs,revision=manifest['revision'],digest=manifest['source_tree_sha256'],service_id=a.service_id,deployed_at=start,last_success=service['serviceDetails'].get('lastSuccessfulRunAt'))
 except PendingEvidence as exc:result={'status':'WAIT','category':'PendingEvidence','reason':str(exc)}
 except AuthenticationRequired as exc:result={'status':'STOP','category':'AuthenticationRequired','reason':str(exc)}
 except (ProviderEvidenceError,InfrastructureUnavailable) as exc:result={'status':'STOP','category':type(exc).__name__,'reason':str(exc)}
 except GateFailure as exc:result={'status':'STOP','category':'CronUnverified','reason':str(exc)}
 except (KeyError,TypeError,ValueError,OSError):result={'status':'STOP','category':'ProviderEvidenceError','reason':'Malformed/missing provider or local evidence; no runtime pass'}
 a.output.write_text(json.dumps(result,indent=2));print(json.dumps({'status':result['status'],'category':result.get('category')}));return 0 if result['status']=='PASS' else (2 if result['status']=='WAIT' else 1)


if __name__=='__main__':raise SystemExit(main())
