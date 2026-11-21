#!/opt/homebrew/bin/python3.12
"""Exact owner-authorized Product Explorer release; no command passthrough."""
import importlib.util,json,os,pathlib,re,subprocess,sys,time
PROFILE=pathlib.Path("/Volumes/AvaOne-Development-Retained/sessions/AVA-ERUPTED-FLAVOR-LIVE-20261119/persistent/product-explorer-release-profile")
spec=importlib.util.spec_from_file_location("product_release_development",PROFILE/"erupted-product-release-dev.py")
e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e);base=e.base
SID="AVA-ERUPTED-PRODUCT-RELEASE-20261120"
ROOT=pathlib.Path("/Volumes/AvaOne-Development-Retained/sessions")/SID
OUT=ROOT/"persistent/product-explorer-release-20261120-r4"
WORKER=OUT/"release_worker.py"
CONFIG=OUT/"config.json"
Q=OUT/"qualification.json"
BASELINE="d19e7d2abdcd7b6dc0edd538e93f49a247a48a07"
BASELINE_DEPLOY="dep-dc0b8hmi0phs73cvdlh0"
BRANCH="codex/ava-erupted-product-release-20261120"
PUBLISH_BRANCH="release/product-explorer-web-20261120-r4"
RECOVERY_BRANCH="release/product-explorer-recovery-20261120"
CRON_BASELINE="af00e6dee9738b22afeaaed9951d029be1b21cbc"
CRON_BRANCH="release/product-explorer-cron-compat-20261120"
ACTIONS={"publish","dispatch","ci","preflight","rehearse","prepare","refresh-preflight","migrate","import","bridge","watch-bridge","deploy","watch","verify","recover","watch-recovery","cron-bind","cron-deploy","watch-cron","restore-cron-branch"}
WRITE_ACTIONS={"publish","dispatch","prepare","migrate","import","bridge","deploy","recover","cron-bind","cron-deploy","restore-cron-branch"}

def configuration():
    c=base.read(CONFIG)
    assert c["session"]==SID and c["baseline"]==BASELINE and c["baseline_deploy"]==BASELINE_DEPLOY
    assert c["branch"]==BRANCH and c["recovery_branch"]==RECOVERY_BRANCH
    assert c["publish_branch"]==PUBLISH_BRANCH
    assert c["parent"]==BASELINE and c["recovery_parent"]==BASELINE
    for key in ("revision","recovery_revision"):
        assert re.fullmatch("[a-f0-9]{40}",c[key])
    assert c["revision"]!=c["recovery_revision"] and c["owner_id"]==6
    assert c["compatibility_amendment_authorized"] is True
    assert c["cron_parent"]==CRON_BASELINE and c["cron_branch"]==CRON_BRANCH
    assert re.fullmatch("[a-f0-9]{40}",c["cron_revision"])
    assert c["cron_revision"] not in {c["revision"],c["recovery_revision"],CRON_BASELINE}
    return c

def frozen_source(c):
    repo=ROOT/"source"
    assert base.output(["git","-C",repo,"rev-parse","HEAD"])==c["revision"]
    assert base.output(["git","-C",repo,"rev-parse","HEAD^"])==BASELINE
    assert base.output(["git","-C",repo,"branch","--show-current"])==BRANCH
    assert not base.output(["git","-C",repo,"status","--porcelain"])
    assert not base.output(["git","-C",repo,"ls-files","--",
        "app/services/flavor_explorer/release_assets",
        "app/services/flavor_explorer/photo-pilot-sources.json",
        "app/services/flavor_explorer/photo_pilot.py"]),"Private release inputs must stay outside public Git"
    assert base.output(["git","-C",repo,"rev-parse",c["recovery_revision"]+"^"])==BASELINE
    assert base.output(["git","-C",repo,"diff","--name-only",BASELINE,c["recovery_revision"]]).splitlines()==["app/purchasing_v4/production_target.py","app/schema_contract.py","app/services/daily_evidence_migration.py","tests/test_scheduling_0057_target_compatibility.py"]
    assert base.output(["git","-C",repo,"rev-parse",c["cron_revision"]+"^"])==CRON_BASELINE
    assert base.output(["git","-C",repo,"diff","--name-only",CRON_BASELINE,c["cron_revision"]]).splitlines()==["app/schema_contract.py"]
    source=base.output(["git","-C",repo,"show",CRON_BASELINE+":app/schema_contract.py"])
    actual=base.output(["git","-C",repo,"show",c["cron_revision"]+":app/schema_contract.py"])
    assert source.count("SUPPORTED_REVISIONS = frozenset({HEAD_REVISION})")==1
    assert actual==source.replace("SUPPORTED_REVISIONS = frozenset({HEAD_REVISION})",
        "SUPPORTED_REVISIONS = frozenset({HEAD_REVISION, '20261120_0058'})")

def qualified():
    e.qualified();c=configuration();q=base.read(Q)
    assert q["status"]=="PASS" and q["checks"] and all(q["checks"].values())
    assert q["config"]==c and set(q["actions"])==ACTIONS
    required={str(pathlib.Path(__file__).resolve()),str(WORKER),str(CONFIG),str(PROFILE/"erupted-product-release-dev.py"),
              str(PROFILE/"qualification.json"),str(base.B/"ava-dev.py")}
    assert required<=set(q["hashes"])
    for path,digest in q["hashes"].items():assert base.sha(path)==digest,"Frozen transport changed: "+path
    frozen_source(c)
    return c

def release_profile(paths,record,original,action):
    assert action in ACTIONS|{"qualify"}
    # All release source is read-only, including the Git index and original objects.
    value=original([OUT,ROOT/"disposable"],action!="qualify")
    value=value.replace('(remote ip "localhost:*")','(remote ip "localhost:55486")')
    value=value.replace('(local ip "localhost:*")','(local ip "localhost:55486")')
    value+='(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))\n'
    if action!="qualify":value+='(allow network-outbound (literal "/private/var/run/mDNSResponder"))\n'
    if action in {"preflight","rehearse","refresh-preflight","migrate","import","bridge","watch-bridge","watch-recovery","verify"}:
        value+='(allow network-outbound (remote tcp "*:5432"))\n'
    return value

def control_check(*,qualifying=False):
    # This read-only child inherits the existing sandbox and real launcher lease.
    # Installed control hashing uses Python3.12; app dependencies stay on3.10.
    assert pathlib.Path(sys.executable).resolve()==pathlib.Path(base.PY).resolve()
    assert sys.version_info[:2]==(3,12)
    base.gate()
    if qualifying:
        e.qualified();config=configuration();frozen_source(config)
    else:qualified()

def cleanup_lock(lease):
    receipt=base.read(ROOT/"run.json");alive=False
    try:os.killpg(receipt["rootPID"],0);alive=True
    except ProcessLookupError:pass
    if receipt.get("state")=="Finished" and not alive:
        assert base.read(base.GLOBAL_LOCK/"owner.json")["nonce"]==lease["nonce"]
        (base.GLOBAL_LOCK/"owner.json").unlink();base.GLOBAL_LOCK.rmdir()

def run(action):
    assert __debug__ and action in ACTIONS|{"qualify"}
    c=configuration() if action=="qualify" else qualified()
    if action=="qualify":e.qualified();frozen_source(c)
    original=base.profile;e.configure();root,record,temp=e.session(SID)
    assert root==ROOT
    original_env=base.cleanenv
    token=subprocess.check_output(["/opt/homebrew/bin/gh","auth","token"],stderr=subprocess.DEVNULL,text=True).strip() if action!="qualify" else None
    def clean(temp):
        env=original_env(temp)
        env.update(GIT_OPTIONAL_LOCKS="0",GODEBUG="netdns=go")
        if token:env["GH_TOKEN"]=token
        return env
    base.cleanenv=clean
    base.profile=lambda paths,network=False:release_profile(paths,record,original,action)
    command=([str(base.PY),"-I","-B",str(pathlib.Path(__file__).resolve()),"rehearse-supervisor"]
             if action=="rehearse" else [str(e.PYTHON),"-I","-B",str(WORKER),action])
    lease=base.lock_broker(SID) if action=="rehearse" else None
    try:base.run(SID,command)
    finally:
        if lease:cleanup_lock(lease)
    frozen_source(c)

def rehearse_supervisor():
    # One fixed rehearsal worker, original shared regression lock and original limits.
    base.gate();qualified()
    command=[str(e.PYTHON),"-I","-B",str(WORKER),"rehearse-worker"]
    child=subprocess.Popen(command);start=time.monotonic();peak=0;owned={}
    def sample():
        output=None
        for _ in range(3):
            r=subprocess.run([str(base.B/"kashew-process-stats"),"--rss"],capture_output=True,text=True)
            if r.returncode==0:output=r.stdout;break
            time.sleep(.05)
        assert output is not None,"Original descendant sampler unavailable"
        rows=[tuple(map(int,row.split())) for row in output.splitlines()]
        assert all(len(row)==6 for row in rows)
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
    def stop():
        live,selected=sample()
        for pid in selected:
            if pid!=os.getpid() and pid in live and owned.get(pid)==live[pid][4:6]:
                try:os.kill(pid,15)
                except ProcessLookupError:pass
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            live,_=sample()
            remaining=[pid for pid,birth in owned.items() if pid!=os.getpid() and pid in live and live[pid][4:6]==birth]
            if not remaining:return
            time.sleep(.1)
        for pid in remaining:
            try:os.kill(pid,9)
            except ProcessLookupError:pass
    try:
        while child.poll() is None:
            live,selected=sample();rss=sum(live[pid][2]*1024 for pid in selected if pid in live);peak=max(peak,rss)
            if rss>1536*1024*1024 or time.monotonic()-start>900:
                stop();raise RuntimeError("Original aggregate RSS/deadline limit reached")
            time.sleep(.2)
        print(json.dumps(dict(supervised=True,exitCode=child.returncode,peakAggregateRSSBytes=peak,
             heapMiB=512,nurseryMiB=16,maximumFileConcurrency=2,detachedDescendantsIncluded=True)),flush=True)
        return child.returncode
    finally:
        stop()
        if child.poll() is None:child.wait(timeout=5)

if __name__=="__main__":
    try:
        assert __debug__ and len(sys.argv)==2
        if sys.argv[1]=="rehearse-supervisor":sys.exit(rehearse_supervisor())
        if sys.argv[1] in {"control-check","control-check-qualify"}:
            control_check(qualifying=sys.argv[1]=="control-check-qualify")
        else:run(sys.argv[1])
    except (AssertionError,RuntimeError,OSError,ValueError,subprocess.CalledProcessError) as error:
        print("product-explorer-release: "+str(error),file=sys.stderr);sys.exit(1)
