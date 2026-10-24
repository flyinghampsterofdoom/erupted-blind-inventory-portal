"""Release/recovery state machine restricted to an exact local Simulation class.

No HTTP client, provider token, subprocess, production adapter or dynamic import.
Failures remain failures even if independent recovery restores availability.
"""
import time
from scripts.release.phase3.manifest import validate
from scripts.release.phase3.journal import Journal
from scripts.release.phase3.simulation import Simulation
from scripts.release.phase3.verification import Failure, readiness, acceptance, execute, verifier_health


class Coordinator:
    def __init__(self, manifest, provider, directory, *, functional=None):
        validate(manifest)
        if type(provider) is not Simulation or provider.mode != 'simulation-only':
            raise ValueError('Only the bundled local Simulation is accessible in Phase 3')
        self.m=manifest;self.p=provider;self.directory=directory
        self.lock_path=provider.path.resolve().with_suffix('.release.lock')
        self.functional=functional or (lambda:acceptance(self.p.fetch,self.p.principals))

    def action(self, j, namespace, action, payload):
        key=self.m['manifest_id']+':'+namespace+':'+action
        previous=j.data['actions'].get(key)
        if previous:
            if previous['payload'] != payload: raise Failure('verification','action-payload-drift')
            if previous['status']=='done': return
            # A crash/lost POST response is resolved by GET receipt, never blind retry.
            receipt=self.p.receipt(key)
            if not receipt or receipt.get('payload') != payload or receipt.get('outcome')!='success':
                raise Failure('infrastructure','ambiguous-operation-inspect-required')
            j.data['actions'][key]['status']='done';j.event('reconciled',operation=key);return
        j.data['actions'][key]={'status':'intent','action':action,'payload':payload}
        j.event('intent',operation=key)
        result=self.p.mutate(key,action,payload)
        if result.get('outcome')!='success' or result.get('payload')!=payload:
            raise Failure('infrastructure','unsupported-provider-receipt')
        j.data['actions'][key]['status']='done';j.event('completed',operation=key)

    def check(self, j, name, callback):
        started=time.monotonic();result=execute(callback)
        j.event('verification',check=name,result=result,duration_seconds=time.monotonic()-started)
        return result

    def certify(self, j, expected, *, alternate=False):
        self.check(j,'readiness',lambda:readiness(self.p.readiness(expected),expected,self.p.state['schema']))
        self.check(j,'functional-independent' if alternate else 'functional',
            (lambda:acceptance(self.p.fetch,self.p.principals)) if alternate else self.functional)

    def restore(self, j, namespace, expected):
        # Fresh certification before each availability transition, never timeout-based.
        self.certify(j,expected,alternate=True)
        original=j.data['original']
        self.action(j,namespace,'restore-cron',{'suspended':original['cron_suspended']})
        self.certify(j,expected,alternate=True)  # Resume can rebuild a cron artifact.
        if not original['cron_suspended']:
            self.check(j,'natural-cron-execution',self.p.natural_cron)
        self.action(j,namespace,'restore-maintenance',{'enabled':original['maintenance']})
        s=self.p.observe()
        if s['maintenance']!=original['maintenance'] or s['cron_suspended']!=original['cron_suspended']:
            raise Failure('infrastructure','availability-restoration-unconfirmed')
        self.certify(j,expected,alternate=True)

    def decision(self, state):
        start=time.monotonic()
        if state['schema'] not in self.m['baseline']['supports']:
            choice='forward-only' if state['schema'] in self.m['schema']['candidate_supports'] else 'hold-unknown-schema'
        else: choice='rollback-compatible'
        return {'choice':choice,'observed_schema':state['schema'],
                'duration_seconds':time.monotonic()-start,'database_downgrade':False}

    def failure(self, j, exc):
        if isinstance(exc,Failure): finding=exc.finding.dict()
        elif isinstance(exc,(TimeoutError,ConnectionError,OSError)):
            finding=Failure('infrastructure','provider-or-network-unavailable').finding.dict()
        else: finding=Failure('verification','coordinator-implementation-defect').finding.dict()
        j.data['status']='FAILED';j.event('failure',finding=finding)
        try:
            s=self.p.observe();decision=self.decision(s)
            j.data['decision']=decision;j.event('recovery-decision',**decision)
            # Verifier defect cannot mean unhealthy; use separate built-in evidence
            # to release an unnecessary hold, but preserve failed verification status.
            expected=self.expected()
            if finding['category']=='verification' and s['schema']==self.m['schema']['expected']:
                self.restore(j,'independent-restoration',expected)
                j.data['status']='FAILED_VERIFICATION_RESTORED';j.event('independent-safety-restored')
        except Exception:
            j.event('recovery-held',reason='independent-safety-or-restoration-not-proven')
        return self.report(j)

    def candidate(self):
        actual=self.p.candidate();expected={k:self.expected()[k] for k in ('revision','source_tree_sha256','artifact_sha256')}
        if actual!=expected:raise Failure('verification','qualified-candidate-substitution')

    def expected(self):
        return {'revision':self.m['revision'],'source_tree_sha256':self.m['source_tree_sha256'],
                'supports':self.m['schema']['candidate_supports'],'artifact_sha256':self.m['artifact']['sha256']}

    def run(self):
        with Journal(self.directory,self.m['manifest_id'],self.lock_path) as j:
            if j.data['status'] in ('SUCCESS','RECOVERED','FAILED_VERIFICATION_RESTORED') or j.data['status']=='FAILED':
                return self.report(j)  # Explicit recovery only; no silent failure retries.
            started=time.monotonic()
            try:
                verifier_health()
                initial=self.p.observe()
                if 'original' not in j.data:
                    j.data['original']={k:initial[k] for k in ('maintenance','cron_suspended','web','cron','schema')}
                    if initial['web']!=self.m['baseline']['revision'] or initial['cron']!=initial['web'] or initial['schema']!=self.m['schema']['from']:
                        raise Failure('application','baseline-state-mismatch','unhealthy')
                    j.event('baseline',state=j.data['original'])
                ci=self.p.ci()
                if ci['revision']!=self.m['revision'] or ci['check']!='CI required' or ci['conclusion']!='success' or ci['app_id']!=15368:
                    raise Failure('verification','exact-candidate-ci-not-successful')
                self.candidate()
                if not initial['backup_verified'] or initial['backup_kind']!=self.m['backup_requirement'] or not initial['migration_prerequisites']:
                    raise Failure('verification','backup-or-migration-prerequisite-missing')
                j.event('preflight',ci=ci,backup_verified=True,migration_prerequisites=True)
                j.data['status']='RUNNING';j.event('running')
                if self.m['maintenance_required']: self.action(j,'release','fence',{})
                if self.m['maintenance_required']:
                    fenced=self.p.observe()
                    if not fenced['maintenance'] or not fenced['cron_suspended'] or fenced['cron_inflight']:
                        raise Failure('verification','maintenance-cron-fence-not-proven')
                if self.m['schema']['required_migrations']:
                    self.action(j,'release','migrate',{'schema':self.m['schema']['expected']})
                state=self.p.observe()
                if state['schema']!=self.m['schema']['expected']:raise Failure('application','migration-schema-mismatch','unhealthy')
                expected=self.expected()
                for role in ('web','cron'):
                    self.candidate()
                    self.action(j,'release','deploy-'+role,{k:expected[k] for k in ('revision','source_tree_sha256','artifact_sha256')})
                    self.p.wait(role,expected)
                self.certify(j,expected)
                self.restore(j,'release',expected)
                j.data['status']='SUCCESS';j.event('success',duration_seconds=time.monotonic()-started)
                return self.report(j)
            except Exception as exc: return self.failure(j,exc)
            # BaseException (termination) deliberately escapes: durable intents/receipts
            # survive, OS releases the lock, next run reconciles before proceeding.

    def recover(self, direction):
        if direction not in ('rollback','forward'):raise ValueError('Explicit rollback or forward recovery required')
        with Journal(self.directory,self.m['manifest_id'],self.lock_path) as j:
            if j.data['status']=='RECOVERED':return self.report(j)
            if j.data['status']!='FAILED':raise ValueError('Recovery requires a recorded failed release')
            try:
                state=self.p.observe();decision=self.decision(state);j.event('explicit-recovery',direction=direction,**decision)
                if direction=='rollback':
                    if decision['choice']!='rollback-compatible':raise Failure('verification','rollback-schema-incompatible')
                    expected=self.m['baseline']
                else:
                    if state['schema'] not in self.m['schema']['candidate_supports']:
                        raise Failure('verification','forward-schema-incompatible')
                    self.candidate()
                    ci=self.p.ci()
                    if ci['revision']!=self.m['revision'] or ci['conclusion']!='success':raise Failure('verification','forward-ci-missing')
                    expected=self.expected()
                namespace='recovery-'+direction
                # No migration/downgrade/restore occurs in application recovery.
                for role in ('web','cron'):
                    self.action(j,namespace,'deploy-'+role,{k:expected[k] for k in ('revision','source_tree_sha256','artifact_sha256')})
                    self.p.wait(role,expected)
                self.certify(j,expected,alternate=True);self.restore(j,namespace,expected)
                j.data['status']='RECOVERED';j.event('recovered',direction=direction,original_failure_retained=True)
                return self.report(j)
            except Exception as exc:return self.failure(j,exc)

    def report(self,j):
        return {'status':j.data['status'],'mode':'simulation-only','manifest_id':self.m['manifest_id'],
            'provider_api_calls_this_process':self.p.calls,'journal_events':len(j.data['events']),
            'decision':j.data.get('decision'),'failures':[e['finding'] for e in j.data['events'] if e['kind']=='failure'],
            'production_mutations':0}
