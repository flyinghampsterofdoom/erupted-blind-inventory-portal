"""Durable local provider double. No network imports, credentials or live IDs."""
import copy
import json
from pathlib import Path
from scripts.release.phase3.journal import atomic
from scripts.release.phase3.verification import Failure


class Interrupted(BaseException): pass


class Simulation:
    mode='simulation-only'
    principals={'owner':1,'lead':2,'store':3}

    def __init__(self, path, manifest, *, faults=None):
        self.path=Path(path);self.manifest=manifest;self.faults=faults or {};self.calls=0
        if not self.path.exists():
            b=manifest['baseline']
            self.state={'environment':'simulation-only','api_calls':0,'schema':manifest['schema']['from'],
                'web':b['revision'],'cron':b['revision'],'digest':b['source_tree_sha256'],
                'maintenance':False,'cron_suspended':False,'cron_inflight':False,'receipts':{},'mutations':[],
                'ready':True,'backup_verified':True,'migration_prerequisites':True,
                'candidate_revision':manifest['revision'],'candidate_source':manifest['source_tree_sha256'],
                'candidate_artifact':manifest['artifact']['sha256'],'backup_kind':manifest['backup_requirement'],
                'backup_proof':{'status':'PASS','kind':manifest['backup_requirement'],
                    'schema':manifest['schema']['from'],'baseline_revision':b['revision'],
                    'isolated_restore_passed':manifest['backup_requirement']=='isolated-restore',
                    'evidence_id':'SIMULATED-backup-v1'},
                'migration_proof':{'status':'PASS','revision':manifest['revision'],
                    'from':manifest['schema']['from'],'to':manifest['schema']['expected'],
                    'path':manifest['schema']['required_migrations'],'evidence_id':'SIMULATED-migrations-v1'},
                'ci_revision':manifest['revision'],'ci_success':True,'source_intact':True,'dependencies_ok':True}
            atomic(self.path,self.state)
        self._load();self.calls=self.state.get('api_calls',0)

    def record_call(self):
        self._load();self.calls=self.state.get('api_calls',0)+1
        self.state['api_calls']=self.calls;self.save()

    def _load(self): self.state=json.loads(self.path.read_text())
    def save(self): atomic(self.path,self.state)

    def fault(self, name):
        if name not in self.faults: return
        kind=self.faults.pop(name)
        if kind == 'interrupt': raise Interrupted('Simulated process termination')
        if kind == 'network': raise ConnectionError('Simulated network reset')
        if kind == 'timeout': raise TimeoutError('Simulated provider timeout')
        if kind == 'assertion': raise AssertionError('Deliberately incorrect verifier assertion')
        if kind == 'migration':
            self.state['schema']='partial-unknown';self.save()
        if name.startswith('restore-'):raise Failure('infrastructure',name+'-failed')
        raise Failure('application',name+'-failed','unhealthy')

    def observe(self):
        self.record_call();self.fault('observe');self._load();return copy.deepcopy(self.state)

    def candidate(self):
        self.record_call();self._load()
        return {'revision':self.state['candidate_revision'],'source_tree_sha256':self.state['candidate_source'],
                'artifact_sha256':self.state['candidate_artifact']}

    def ci(self):
        self.record_call();self.fault('ci');return {'revision':self.state['ci_revision'],'check':'CI required',
            'conclusion':'success' if self.state['ci_success'] else 'failure','app_id':15368,'provenance':'SIMULATED'}

    def receipt(self, key):
        self.record_call();self._load();return copy.deepcopy(self.state['receipts'].get(key))

    def mutate(self, key, action, payload):
        self.record_call();self._load()
        if key in self.state['receipts']: raise RuntimeError('Duplicate submission prohibited')
        self.fault(action)
        if action == 'fence': self.state.update(maintenance=True,cron_suspended=True)
        elif action == 'migrate': self.state['schema']=payload['schema']
        elif action == 'deploy-web':
            self.state.update(web=payload['revision'],digest=payload['source_tree_sha256'])
        elif action == 'deploy-cron': self.state['cron']=payload['revision']
        elif action == 'restore-cron': self.state['cron_suspended']=payload['suspended']
        elif action == 'restore-maintenance': self.state['maintenance']=payload['enabled']
        else: raise ValueError('Unsupported simulated operation')
        self.state['mutations'].append({'key':key,'action':action,'payload':payload})
        result={'id':key,'action':action,'outcome':'success','payload':payload}
        self.state['receipts'][key]=result;self.save()
        # Simulates lost response / crash AFTER provider committed the operation.
        self.fault('after-'+action)
        return result

    def wait(self, role, expected):
        self.record_call();self.fault('wait-'+role);self._load()
        if self.state[role]!=expected['revision']:
            raise Failure('application','deployment-revision-mismatch','unhealthy')
        return {'status':'live','revision':self.state[role]}

    def natural_cron(self):
        self.record_call();self.fault('natural-cron');self._load()
        from scripts.release.verify_runtime import verify_cron_logs
        s=self.state
        if s['cron_suspended']:raise Failure('infrastructure','natural-cron-not-available')
        # Synthetic provider log shapes; no job is actually invoked.
        proof={'event':'ERUPTED_RUNTIME_IDENTITY','status':'VERIFIED','role':'cron',
            'service_id':'simulated-cron','revision':s['cron'],'platform_revision':s['cron'],
            'source_tree_sha256':s['digest']}
        logs=[{'timestamp':'2026-10-24T00:01:00.000000001Z','labels':[{'name':'resource','value':'simulated-cron'}],
            'message':json.dumps(proof)},
            {'timestamp':'2026-10-24T00:01:01Z','labels':[{'name':'resource','value':'simulated-cron'}],
            'message':'Cron job run finished successfully'}]
        result=verify_cron_logs(logs,revision=s['cron'],digest=s['digest'],service_id='simulated-cron',
            deployed_at='2026-10-24T00:00:00Z',last_success='2026-10-24T00:01:01Z')
        return {'status':result['status'],'proof':'SIMULATED matched natural runtime and completion'}

    def readiness(self, expected):
        self.record_call();self.fault('readiness');s=self.observe()
        return {'contract':'erupted-readiness-v1','schema':s['schema'],
            'dependencies':{'process':s['ready'],'database':s['schema']!='partial-unknown',
                'schema':s['schema'] in expected['supports'],'source':s['source_intact'] and s['dependencies_ok']},
            **{role:{'role':role,'revision':s[role],'source_tree_sha256':s['digest'],
                     'status':'VERIFIED' if s['source_intact'] else 'INVALID'} for role in ('web','cron')}}

    def fetch(self, role, path):
        self.record_call();self.fault('functional')
        if role == 'anonymous':return {'status':303,'headers':{'location':'/login?return_to=%2Fv2%2Fscheduling%2Fweek'}}
        if path == '/management/purchasing' and role in ('lead','store'):return {'status':403}
        if path == '/session-status':body={'authenticated':True,'principal_id':self.principals[role]}
        elif path == '/v2/scheduling/api/board':body={'employees':[],'shifts':[]}
        else:body='<html><form action="/management/purchasing/generate"></form><div data-schedule-board>Management Sessions</div></html>'
        return {'status':200,'body':body}
