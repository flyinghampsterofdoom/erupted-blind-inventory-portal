"""Inspect an existing scheduled cron runtime via authenticated Render GET APIs."""
import argparse,json,os,sys,urllib.request,urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.release.verify_runtime import validate_deploy,verify_cron_logs,GateFailure


def main():
 p=argparse.ArgumentParser();p.add_argument('--service-id',required=True);p.add_argument('--deploy-id',required=True);p.add_argument('--manifest',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
 token=os.environ.get('RENDER_API_KEY')
 if not token:p.error('Use existing authenticated Render API access via RENDER_API_KEY; never pass a credential on the command line')
 def get(path):
  req=urllib.request.Request('https://api.render.com/v1/'+path,headers={'Authorization':'Bearer '+token})
  with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)
 try:
  manifest=json.loads(a.manifest.read_text());service=get('services/'+a.service_id);deploy=get('services/'+a.service_id+'/deploys/'+a.deploy_id);validate_deploy(deploy,manifest['revision'])
  start=deploy.get('finishedAt') or deploy['createdAt'];query={'ownerId':service['ownerId'],'resource':a.service_id,'startTime':start,'direction':'forward','limit':100};logs=[]
  for _ in range(20):
   page=get('logs?'+urllib.parse.urlencode(query));logs.extend(page['logs'])
   if not page.get('hasMore'):break
   query.update(startTime=page['nextStartTime'],endTime=page['nextEndTime'])
  else:raise GateFailure('Bounded log pagination exceeded; no cron pass')
  result=verify_cron_logs(logs,revision=manifest['revision'],digest=manifest['source_tree_sha256'],service_id=a.service_id,deployed_at=start,last_success=service['serviceDetails'].get('lastSuccessfulRunAt'))
 except GateFailure as exc:result={'status':'STOP','category':'CronUnverified','reason':str(exc)}
 except Exception:result={'status':'STOP','category':'InfrastructureUnavailable','reason':'Render API evidence unavailable; no runtime pass'}
 a.output.write_text(json.dumps(result,indent=2));print(json.dumps({'status':result['status'],'category':result.get('category')}));return 0 if result['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
