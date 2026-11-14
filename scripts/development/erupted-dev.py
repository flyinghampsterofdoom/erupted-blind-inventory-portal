#!/opt/homebrew/bin/python3.12
"""Owner-authorized Erupted-only bounded profile, November 14, 2026.
All allocations use existing retained quota. Ava controller/dispatch unchanged.
"""
import argparse, hashlib, importlib.util, json, os, pathlib, re, signal, subprocess, sys, time
if not __debug__:raise SystemExit('Optimized enforcement forbidden')
B=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('qualified_ava_controller',B/'ava-dev.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
SOURCE=pathlib.Path('/Users/justinrawlinson/Developer/Erupted/canonical')
REV='aca373d'
PROFILE=B/'erupted-qualification.json'
PYTHON=pathlib.Path('/Users/justinrawlinson/.pyenv/versions/3.10.13/bin/python')
ALLOW=('app/services','app/routers','app/static/v2','tests','docs/development','scripts/development')
EXTRA=('AGENTS.md',)
ACTIVE=None

def safe(p):
 x=pathlib.PurePosixPath(p)
 return bool(p) and not x.is_absolute() and '..' not in x.parts and '.git' not in x.parts and (p in EXTRA or any(p==a or p.startswith(a+'/') for a in ALLOW))

def qualified():
 q=base.read(PROFILE)
 assert q['status']=='PASS' and all(q['checks'].values())
 for f,h in q['hashes'].items():assert base.sha(f)==h,'Qualification changed: '+f

def session(id,cfg=None):
 global ACTIVE
 assert re.fullmatch('AVA-ERUPTED-[A-Za-z0-9_-]{1,60}',id),'Erupted session ID required'
 v,_=base.volumes(cfg or base.read(base.CONFIG));root=pathlib.Path(v['permanent']['mount'])/'sessions'/id
 r=base.read(root/'session.json')
 assert r['id']==id and r['project']=='erupted' and r['sourceAuthority']==str(SOURCE) and r['budget']=='permanent'
 assert pathlib.Path(r['worktree'])==root/'source' and pathlib.Path(r['home'])==root/'lifecycle'
 assert all(safe(x) for x in r['scope'])
 ACTIVE=r
 return root,r,root/'disposable'

def configure():
 base.session=session
 original_env=base.cleanenv
 def env(temp):
  e=original_env(temp);root=temp.parent;home=root/'persistent/home';home.mkdir(parents=True,exist_ok=True,mode=0o700)
  e.update(HOME=str(home),CFFIXED_USER_HOME=str(home),XDG_CONFIG_HOME=str(home/'.config'),AVA_BOUNDED_PROJECT='erupted',NODE_OPTIONS='--max-old-space-size=512 --max-semi-space-size=16',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONPATH=ACTIVE['worktree'] if ACTIVE else '',PYTEST_ADDOPTS='-p no:cacheprovider')
  return e
 base.cleanenv=env
 original_profile=base.profile
 def profile(paths,network=False):
  assert not network,'Erupted development has no external network authority'
  allowed=[]
  for p in paths:
   p=pathlib.Path(p)
   if ACTIVE and p==pathlib.Path(ACTIVE['worktree']):
    allowed += [p/x for x in ACTIVE['scope']]+[p/'.git']
   else:allowed.append(p)
  s=original_profile(allowed,False)
  s=s.replace('(allow network-outbound (remote ip "localhost:*"))','(allow network-outbound (remote ip "localhost:55486"))')
  s=s.replace('(allow network-bind (local ip "localhost:*"))','(allow network-bind (local ip "localhost:55486"))')
  s += '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))\n'
  return s
 base.profile=profile

def start(id,name,scopes,owner):
 qualified();a=base.admission();assert re.fullmatch('AVA-ERUPTED-[A-Za-z0-9_-]{1,60}',id)
 assert owner and re.fullmatch('[A-Za-z0-9_-]{3,100}',owner)
 assert scopes and all(safe(x) for x in scopes)
 assert base.output(['/usr/bin/git','-C',SOURCE,'remote','get-url','origin'])=='git@github.com:flyinghampsterofdoom/erupted-blind-inventory-portal.git'
 root=pathlib.Path(a['volumes']['permanent']['mount'])/'sessions'/id
 assert not root.exists(),'Never overwrite existing sessions'
 root.mkdir(mode=0o700);temp=root/'disposable';temp.mkdir(mode=0o700)
 for n in ['lifecycle','persistent','hooks']:(root/n).mkdir(mode=0o700)
 repo=root/'source';commit=base.output(['/usr/bin/git','-C',SOURCE,'rev-parse',REV+'^{commit}'])
 base.confined(['/usr/bin/git','clone','--shared','--no-checkout',SOURCE,repo],[root],temp,capture=True)
 for args in [['config','gc.auto','0'],['checkout','-b','codex/'+id.lower(),commit]]:
  base.confined(['/usr/bin/git','-C',repo]+args,[root],temp,capture=True)
 # New local Git only; remote publication is deliberately outside this launcher.
 for op in ['commit','push']:
  f=root/'hooks'/('pre-'+op)
  f.write_text(('#!/bin/sh\nexec \"'+str(base.L/'bin/erupted-dev')+'\" git-gate --id '+id+'\n') if op=='commit' else '#!/bin/sh\necho \"Remote publication is outside this local development launcher.\" >&2\nexit 1\n');f.chmod(0o700)
 base.confined(['/usr/bin/git','-C',repo,'config','core.hooksPath',root/'hooks'],[root],temp,capture=True)
 r=dict(id=id,name=name,project='erupted',budget='permanent',sourceAuthority=str(SOURCE),owner=owner,created=time.time(),worktree=str(repo),home=str(root/'lifecycle'),scope=scopes,base=commit,branch='codex/'+id.lower(),state='Active',hooksSHA256={f.name:base.sha(f) for f in (root/'hooks').iterdir()})
 base.save(root/'session.json',r);print(json.dumps(r))

def git_gate(id):
 base.gate();assert id==os.environ['AVA_BOUNDED_SESSION']
 root=pathlib.Path(base.read(base.CONFIG)['volumes']['permanent']['mount'])/'sessions'/id
 r=base.read(root/'session.json');repo=pathlib.Path(r['worktree'])
 assert r['project']=='erupted' and r['sourceAuthority']==str(SOURCE) and repo==root/'source'
 assert pathlib.Path.cwd().resolve()==repo.resolve(),'Registered working directory required'
 assert base.output(['git','branch','--show-current'])==r['branch'],'Registered branch required'
 assert base.output(['git','config','core.hooksPath'])==str(root/'hooks'),'Registered hooks required'
 names=subprocess.check_output(['git','diff','--cached','--name-only','-z']).decode().split('\0')
 assert all(not x or safe(x) and any(x==p or x.startswith(p+'/') for p in r['scope']) for x in names),'Staged path outside registered scope'
 print('Scoped Erupted local commit admitted')

def verify():
 a=base.admission();root=pathlib.Path(a['volumes']['permanent']['mount'])/'control/erupted-qualification';root.mkdir(mode=0o700,exist_ok=True)
 checks={};outside=SOURCE/'bounded-write-must-not-exist';assert not outside.exists()
 code="import pathlib,subprocess,sys; pathlib.Path('allowed').write_text('ok'); p=subprocess.run([sys.executable,'-B','-c',"+repr("open("+repr(str(outside))+",'xb')")+"]); assert p.returncode!=0"
 base.confined([base.PY,'-I','-B','-c',code],[root],root,cwd=root,capture=True);checks['childAndGrandchildConfinement']=not outside.exists()
 link=root/'escape'
 if not link.exists():link.symlink_to(SOURCE,target_is_directory=True)
 try:
  base.confined([base.PY,'-B','-c',"open("+repr(str(link/'bounded-write-must-not-exist'))+",'xb')"],[root],root,capture=True)
  raise RuntimeError('Symlink escape succeeded')
 except subprocess.CalledProcessError:checks['symlinkEscapeDenied']=not outside.exists()
 finally:link.unlink()
 checks['permanentReservationFits']=a['legacyUpperBytes']+a['volumes']['permanent']['quotaBytes']<=base.PERM
 checks['avaControllerUnchanged']=base.sha(B/'ava-dev.py')==base.read(base.CONFIG)['implementationSHA256']
 checks['bareCheckRefused']=subprocess.run([base.PY,'-I','-B',str(B/'ava-dev.py'),'check'],capture_output=True).returncode!=0
 checks['recordedNativeQuotasQualified']=all(base.read(B/'verification.json')['checks'][x] for x in ['permanentNativeQuotaExhaustionDenied','temporaryNativeQuotaExhaustionDenied'])
 checks['permanentOnly']=True
 paths=[pathlib.Path(__file__),base.L/'bin/erupted-dev',base.L/'bin/ava-dev',B/'ava-dev.py',B/'kashew-dev.py',B/'kashew-process-stats']
 base.save(PROFILE,dict(status='PASS',time=time.time(),checks=checks,hashes={str(f):base.sha(f) for f in paths},ownerAuthorization='November 14 2026: separately scoped Erupted launcher; no deployment/destruction',permanentOnly=True))
 assert all(checks.values());print(json.dumps(checks))

def supervise(cmd):
 base.gate();assert cmd
 # Fixed runtime, at most two explicit test files; no code/eval/plugin injection.
 if pathlib.Path(cmd[0])==PYTHON:
  assert cmd[1:3]==['-m','pytest']
  files=[x for x in cmd[3:] if x.startswith('tests/') and '.py' in x]
  assert 1<=len(files)<=2 and all(pathlib.Path(x.split('::')[0]).is_file() and pathlib.Path(x.split('::')[0]).resolve().is_relative_to(pathlib.Path.cwd()/'tests') for x in files),'One or two explicit Python test files required'
  assert all(x in files or x in ['-q','-x','-s','--disable-warnings','--maxfail=1'] for x in cmd[3:]),'Only explicit test files and bounded output flags allowed'
 elif pathlib.Path(cmd[0])==pathlib.Path(base.NODE):
  assert cmd[1]=='--test'
  files=cmd[2:];assert 1<=len(files)<=2 and all(x.startswith('tests/') and x.endswith(('.cjs','.mjs','.js')) and pathlib.Path(x).is_file() and pathlib.Path(x).resolve().is_relative_to(pathlib.Path.cwd()/'tests') for x in files),'One or two explicit Node test files required'
  cmd=cmd[:1]+['--max-old-space-size=512','--max-semi-space-size=16','--test-concurrency=2']+cmd[1:]
 else:raise AssertionError('Pinned Python pytest or Node test required')
 env=dict(os.environ)
 pg=None
 if pathlib.Path(cmd[0])==PYTHON:
  # Synthetic PostgreSQL is owned by this supervised process and never uses a host service.
  work=pathlib.Path(os.environ['TMPDIR'])/'pytest-postgres';work.mkdir(mode=0o700,exist_ok=True)
  data=work/'data';pgbin=pathlib.Path('/opt/homebrew/opt/postgresql@16/bin')
  if not data.exists():subprocess.run([str(pgbin/'initdb'),'-D',str(data),'-U','erupted_test','-A','trust','--no-locale','--encoding=UTF8'],stdout=subprocess.DEVNULL,check=True)
  pg=subprocess.Popen([str(pgbin/'postgres'),'-D',str(data),'-p','55486','-h','127.0.0.1','-k','','-c','shared_memory_type=mmap','-c','dynamic_shared_memory_type=mmap','-c','shared_buffers=16MB','-c','max_connections=20'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  for attempt in range(100):
   assert pg.poll() is None,'Synthetic PostgreSQL startup failed'
   if subprocess.run([str(pgbin/'pg_isready'),'-h','127.0.0.1','-p','55486'],capture_output=True).returncode==0:break
   time.sleep(.1)
  else:raise RuntimeError('Synthetic PostgreSQL startup timed out')
  assert pg.poll() is None and int((data/'postmaster.pid').read_text().splitlines()[0])==pg.pid,'PostgreSQL port is not owned by this synthetic instance'
  observed=subprocess.check_output([str(pgbin/'psql'),'-h','127.0.0.1','-p','55486','-U','erupted_test','-d','postgres','-Atc','SHOW data_directory'],text=True).strip()
  assert pathlib.Path(observed).resolve()==data.resolve(),'Unexpected PostgreSQL data directory; no test writes permitted'
  env['TEST_POSTGRES_ADMIN_URL']='postgresql+psycopg://erupted_test@127.0.0.1:55486/postgres'
  env['DATABASE_URL']=env['TEST_POSTGRES_ADMIN_URL']
 child=subprocess.Popen(cmd,env=env);start=time.monotonic();peak=0;owned={}
 def sample():
  output=None
  for attempt in range(3):
   r=subprocess.run([str(B/'kashew-process-stats'),'--rss'],capture_output=True,text=True)
   if r.returncode==0:output=r.stdout;break
   time.sleep(.05)
  assert output is not None,'Native descendant process sampler unavailable'
  rows=[tuple(map(int,row.split())) for row in output.splitlines()]
  assert all(len(row)==6 for row in rows),'Incomplete native process identity'
  live={row[0]:row for row in rows}
  selected={pid for pid,row in live.items() if row[1]==os.getpgrp() or owned.get(pid)==row[4:6]}
  selected.add(os.getpid())
  while True:
   added={pid for pid,row in live.items() if row[3] in selected}-selected
   if not added:break
   selected.update(added)
  for pid in selected:
   if pid in live:owned[pid]=live[pid][4:6]
  return live,selected
 def cleanup():
  live,selected=sample()
  for pid in selected:
   if pid!=os.getpid() and pid in live and owned.get(pid)==live[pid][4:6]:
    try:os.kill(pid,signal.SIGTERM)
    except ProcessLookupError:pass
  deadline=time.monotonic()+5
  while time.monotonic()<deadline:
   live,_=sample()
   remaining=[pid for pid,birth in owned.items() if pid!=os.getpid() and pid in live and live[pid][4:6]==birth]
   if not remaining:return
   time.sleep(.1)
  for pid in remaining:
   try:os.kill(pid,signal.SIGKILL)
   except ProcessLookupError:pass
 try:
  while child.poll() is None:
   live,selected=sample()
   rss=sum(live[pid][2]*1024 for pid in selected if pid in live);peak=max(peak,rss)
   if rss>1536*1024*1024 or time.monotonic()-start>min(900,int(os.environ.get('AVA_TEST_TIMEOUT_SECONDS','900'))):
    cleanup();raise RuntimeError('Original aggregate RSS/deadline limit reached')
   time.sleep(.2)
  print(json.dumps({'supervised':True,'exitCode':child.returncode,'peakAggregateRSSBytes':peak,'heapMiB':512,'nurseryMiB':16,'maximumFileConcurrency':2,'detachedDescendantsIncluded':True}),flush=True)
  return child.returncode
 finally:
  cleanup()
  if child.poll() is None:child.wait(timeout=5)

def test(id,cmd):
 root,r,temp=session(id);lease=base.lock_broker(id)
 try:base.run(id,[base.PY,'-I','-B',str(pathlib.Path(__file__).resolve()),'supervise','--']+cmd)
 finally:
  run=base.read(root/'run.json') if (root/'run.json').exists() else None;alive=False
  if run:
   try:os.killpg(run['rootPID'],0);alive=True
   except ProcessLookupError:pass
  if run and run.get('state')=='Finished' and not alive:
   assert base.read(base.GLOBAL_LOCK/'owner.json')['nonce']==lease['nonce']
   (base.GLOBAL_LOCK/'owner.json').unlink();base.GLOBAL_LOCK.rmdir()

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['verify','status','start','run','write','test','supervise','check','clean','reconcile','git-gate']);p.add_argument('--id');p.add_argument('--name',default='Erupted bounded development');p.add_argument('--scope',action='append');p.add_argument('--owner',default=os.environ.get('CODEX_THREAD_ID'));p.add_argument('--path');p.add_argument('--request-bytes',type=int,default=0)
 args=sys.argv[1:];i=args.index('--') if '--' in args else len(args);a=p.parse_args(args[:i]);cmd=args[i+1:];configure()
 if a.action=='status':print(json.dumps(base.accounting(require_fresh=False,require_mount=False)));return
 if a.action=='verify':verify();return
 qualified()
 if a.action=='start':
  with base.mutex():start(a.id,a.name,a.scope,a.owner)
 elif a.action=='run':base.run(a.id,cmd)
 elif a.action=='test':test(a.id,cmd)
 elif a.action=='supervise':sys.exit(supervise(cmd))
 elif a.action=='write':base.atomic_write(a.id,a.path,sys.stdin.read(16_000_001))
 elif a.action=='check':base.gate(a.request_bytes)
 elif a.action=='git-gate':git_gate(a.id)
 elif a.action=='clean':base.clean(a.id)
 elif a.action=='reconcile':
  with base.mutex():base.reconcile(a.id)
if __name__=='__main__':
 try:main()
 except (AssertionError,RuntimeError,OSError,ValueError,subprocess.CalledProcessError) as e:print('erupted-dev: '+str(e),file=sys.stderr);sys.exit(1)
