"""Fixed Product Explorer publication, restore rehearsal and existing-web activation."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import base64,hashlib,io,json,os,re,resource,shutil,signal,socket,subprocess,sys,tarfile,tempfile,time
import urllib.error,urllib.parse,urllib.request

SID="AVA-ERUPTED-PRODUCT-RELEASE-20261120"
ROOT=Path("/Volumes/AvaOne-Development-Retained/sessions")/SID
OUT=ROOT/"persistent/product-explorer-release-20261120-r2"
PRIVATEBUNDLE=ROOT/"persistent/product-explorer-release-20261120/private/curated-bundle"
PRIVATE_MANIFEST_SHA="4953031dca21ec0e2242b5545ab41ebfff3e2695fe2666c3c62f53795a69ea41"
PRIVATE_SEED_SHA="80c4ddea65ce42a0cb4c3d4b583ee785dbb681d7bb2d3790c91c94ff03a4d17d"
REPO="flyinghampsterofdoom/erupted-blind-inventory-portal"
WEB="srv-d6ccg9ktgctc738u1c1g"
DB="dpg-d6ccc3kr85hc7384neo0-a"
ORIGIN="https://erupted-blind-inventory-portal.onrender.com"
ROLES={"staff":"crn-da6pd2qd0e5s73dbudr0","collector":"crn-dbhnbegu01pc73botgf0"}
CRON_BASELINE="af00e6dee9738b22afeaaed9951d029be1b21cbc"
CRON_ORIGINAL_BRANCH="codex/ava-erupted-scheduling-release-20261119"
PENDING={"created","queued","build_in_progress","pre_deploy_in_progress","update_in_progress"}
TERMINAL={"live","build_failed","pre_deploy_failed","update_failed","canceled","deactivated"}
ENV_CHANGES={"FORECAST_ADVISORY_ASSET_SPEC","FORECAST_PRODUCT_ARTIFACT_URL"}
MAX_DUMP=512*1024*1024
PGBIN=Path("/opt/homebrew/Cellar/postgresql@18/18.6/bin")
NATIVE_CONTROL=Path("/opt/homebrew/bin/python3.12")
GIT_ENV=dict(os.environ,GIT_OPTIONAL_LOCKS="0")
sys.path.insert(0,str(ROOT/"source"))

def stamp():return datetime.now(timezone.utc).isoformat()
def sha(value):return hashlib.sha256(value).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def git(*args):return subprocess.check_output(["git","-C",str(ROOT/"source"),*args],env=GIT_ENV)
def file_sha(path):return sha(Path(path).read_bytes())
def load(name):return json.loads((OUT/(name+".json")).read_text())
def save(name,value,once=False):
    p=OUT/(name+".json")
    with p.open("x" if once else "w") as f:
        json.dump(value,f,indent=2,default=str);f.flush();os.fsync(f.fileno())
def retain_rehearsal_stderr(ctx,label,data):
    """Keep bounded diagnostics in encrypted private output, never stdout."""
    assert label in {"pg-dump","pg-restore"} and isinstance(data,bytes)
    limit=128*1024
    if len(data)>limit:
        marker=("\n[bounded diagnostic; original bytes="+str(len(data))+"]\n").encode()
        half=(limit-len(marker))//2
        content=data[:half]+marker+data[-half:]
    else:content=data
    name=label+"-stderr-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+".log"
    target=ctx.private/name
    with target.open("xb") as f:f.write(content);f.flush();os.fsync(f.fileno())
    return {"file":name,"retained_bytes":len(content),"original_bytes":len(data),"sha256":sha(content)}

def validate_environment(before,after):
    assert set(before)==set(after)
    changed={k for k in before if before[k]!=after[k]}
    assert changed==ENV_CHANGES,"Unexpected production environment change"
    return changed
def protected_service(value):
    return {k:v for k,v in value.items() if k not in {"createdAt","updatedAt","dashboardUrl"}}
def validate_status(status):
    if status not in PENDING|TERMINAL:raise ValueError("Unknown Render deployment status")
    return status
def quoted(value):
    assert isinstance(value,str) and "\x00" not in value
    return '"'+value.replace('"','""')+'"'
def commit_payload(raw):
    headers,_,message=raw.decode().partition("\n\n")
    rows=headers.splitlines();result={"message":message,"parents":[]}
    for row in rows:
        kind,_,value=row.partition(" ")
        if kind in {"author","committer"}:
            match=re.fullmatch(r"(.*) <([^<>]+)> ([0-9]+) ([+-][0-9]{4})",value)
            assert match,"Unsigned explicit Git identity required"
            name,email,seconds,offset=match.groups()
            sign=1 if offset[0]=="+" else -1
            zone=timezone(sign*timedelta(hours=int(offset[1:3]),minutes=int(offset[3:5])))
            result[kind]={"name":name,"email":email,"date":datetime.fromtimestamp(int(seconds),zone).isoformat()}
        elif kind=="tree":result["tree"]=value
        elif kind=="parent":result["parents"].append(value)
        else:raise ValueError("Unsupported signed/extended Git commit")
    assert set(result)=={"message","parents","tree","author","committer"} and len(result["parents"])==1
    return result

class Context:
    def __init__(self):
        assert __debug__ and Path.cwd().resolve()==ROOT/"source"
        os.umask(0o077)
        self.cfg=load("config");self.rev=self.cfg["revision"];self.recovery=self.cfg["recovery_revision"]
        self.base=self.cfg["baseline"];self.cron=self.cfg["cron_revision"];self.private=OUT/"private";self.private.mkdir(mode=0o700,exist_ok=True)
        assert not self.private.is_symlink()
    def request(self,path,method="GET",body=None):
        # No redirects or retries for credential-bearing provider requests.
        token=re.search(r"(?m)^\s+key:\s*(.+)$",Path("/Users/justinrawlinson/.render/cli.yaml").read_text()).group(1).strip().strip("\"'")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):raise ValueError("Provider redirect prohibited")
        req=urllib.request.Request("https://api.render.com/v1/"+path,method=method,data=body,
            headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as r:
            raw=r.read();return json.loads(raw) if raw else None
    def github(self,path,method="GET",body=None):
        req=urllib.request.Request("https://api.github.com/repos/"+REPO+"/"+path,method=method,
            data=None if body is None else canonical(body),
            headers={"Authorization":"Bearer "+os.environ["GH_TOKEN"],"Content-Type":"application/json",
                     "Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):raise ValueError("GitHub redirect prohibited")
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as response:
            raw=response.read();return json.loads(raw) if raw else None
    def gh_mutate(self,path,body,label,expected_sha=None):
        assert path in {"git/blobs","git/trees","git/commits","git/refs","actions/workflows/ci.yml/dispatches"}
        raw=canonical(body)
        if (OUT/(label+"-intent.json")).exists():
            prior=load(label+"-intent")
            assert prior["path"]==path and prior["body_sha256"]==sha(raw),"Prior mutation payload differs"
            if path.startswith("git/") and path!="git/refs":
                assert re.fullmatch("[a-f0-9]{40}",expected_sha or "")
                try:result=self.github(path+"/"+expected_sha)
                except urllib.error.HTTPError as error:
                    assert error.code==404
                    raise RuntimeError("Object result not yet confirmed; no POST retry: "+label) from None
                assert result["sha"]==expected_sha
            elif path=="git/refs":
                assert body["ref"].startswith("refs/heads/")
                result=self.github("git/ref/"+body["ref"][5:])
                assert result["object"]["sha"]==body["sha"]
            else:
                branches={self.cfg["publish_branch"]:self.rev,self.cfg["recovery_branch"]:self.recovery,self.cfg["cron_branch"]:self.cron}
                revision=branches[body["ref"]];result=None
                report=self.ci_report(revision,body["ref"])
                assert report.get("id"),"Exact dispatch not yet confirmed; poll CI without POST retry"
            save(label+"-reconciled",{"at":stamp(),"path":path,"read_only":True,"expected_sha":expected_sha})
            return result
        save(label+"-intent",{"at":stamp(),"path":path,"body_sha256":sha(raw)},True)
        time.sleep(1.1)  # Stay below the provider's content-creation burst ceiling.
        try:result=self.github(path,"POST",body)
        except Exception as error:
            save(label+"-uncertain",{"at":stamp(),"kind":type(error).__name__},True)
            raise RuntimeError("GitHub mutation needs GET reconciliation: "+label) from None
        save(label+"-ack",{"at":stamp(),"response_sha256":sha(canonical(result))},True)
        return result
    def mutate(self,path,method,body,label):
        assert (method,path) in {("PUT","services/"+WEB+"/env-vars"),("POST","services/"+WEB+"/deploys"),
            ("PATCH","services/"+ROLES["staff"]),("POST","services/"+ROLES["staff"]+"/deploys")}
        if path=="services/"+ROLES["staff"]:
            assert method=="PATCH" and set(body)=={"branch"}
            assert body["branch"] in {CRON_ORIGINAL_BRANCH,self.cfg["cron_branch"]}
        if path=="services/"+ROLES["staff"]+"/deploys":
            assert body=={"clearCache":"do_not_clear"}  # A build only; never a run endpoint.
        raw=canonical(body)
        save(label+"-intent",{"at":stamp(),"method":method,"path":path,"body_sha256":sha(raw)},True)
        try:result=self.request(path,method,raw)
        except Exception as error:
            save(label+"-uncertain",{"at":stamp(),"kind":type(error).__name__},True)
            raise RuntimeError("Provider mutation needs GET reconciliation: "+label) from None
        save(label+"-ack",{"at":stamp(),"response_sha256":sha(canonical(result))},True)
        return result
    def environment(self,sid=WEB):
        rows=self.request("services/"+sid+"/env-vars?limit=100")
        assert isinstance(rows,list) and len(rows)<100
        return {row["envVar"]["key"]:row["envVar"]["value"] for row in rows}
    def deployments(self,sid=WEB):
        rows=self.request("services/"+sid+"/deploys?limit=20")
        assert isinstance(rows,list)
        return [row["deploy"] for row in rows]
    def live(self,sid=WEB):
        values=[r for r in self.deployments(sid) if r["status"]=="live"]
        assert len(values)==1
        return values[0]
    def role(self,sid):
        service=self.request("services/"+sid);live=self.live(sid)
        return {"service":protected_service(service),"deployment":live["id"],"revision":live["commit"]["id"],
                "environment_sha256":sha(canonical(self.environment(sid))),
                "secret_files":list(self.secret_files(sid)),"env_groups":list(self.env_groups(sid))}
    def secret_files(self,sid=WEB):
        values=self.request("services/"+sid+"/secret-files?limit=100")
        assert isinstance(values,list) and len(values)<100
        return sha(canonical(values)),len(values)
    def env_groups(self,sid=WEB):
        rows=self.request("env-groups?ownerId=tea-d6cca1h4tr6s73dmk0bg&limit=100")
        assert isinstance(rows,list) and len(rows)<100
        linked=[]
        for row in rows:
            group=row["envGroup"]
            if any(link.get("id")==sid or link.get("serviceId")==sid for link in group.get("serviceLinks",[])):
                linked.append(self.request("env-groups/"+group["id"]))
        return sha(canonical(linked)),len(linked)
    def engine(self,readonly=False):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        info=self.request("postgres/"+DB+"/connection-info")
        url=make_url(info["externalConnectionString"]).set(drivername="postgresql+psycopg")
        assert url.host.startswith(DB+".") and url.host.endswith(".oregon-postgres.render.com") and url.port in (None,5432)
        app=make_url(self.environment()["DATABASE_URL"])
        assert (app.database,app.username)==(url.database,url.username)
        options="-c statement_timeout=120000 -c lock_timeout=5000 -c timezone=UTC"
        if readonly:options+=" -c default_transaction_read_only=on"
        return create_engine(url,connect_args={"sslmode":"require","options":options},pool_size=2,max_overflow=0)
    def assert_source(self):
        assert git("rev-parse","HEAD").decode().strip()==self.rev
        assert not git("status","--porcelain").strip()
        assert git("rev-parse","HEAD^").decode().strip()==self.base
    def baseline(self):return json.loads((self.private/"baseline.json").read_text())
    def preserve_controls(self,expected_env=None):
        baseline=self.baseline()
        assert protected_service(self.request("services/"+WEB))==baseline["protected_service"]
        expected_roles=dict(baseline["roles"])
        if (OUT/"cron-compatibility.json").exists():expected_roles["staff"]=qualified_cron_role(self)
        assert {name:self.role(sid) for name,sid in ROLES.items()}==expected_roles
        assert self.secret_files()==tuple(baseline["secret_files"])
        assert self.env_groups()==tuple(baseline["env_groups"])
        if expected_env is not None:assert self.environment()==expected_env
    def ci_report(self,revision,branch):
        rows=self.github("actions/workflows/ci.yml/runs?branch="+urllib.parse.quote(branch,safe="")+"&event=workflow_dispatch&per_page=30")["workflow_runs"]
        exact=[r for r in rows if r["head_sha"]==revision]
        if not exact:return {"revision":revision,"status":"WAIT","reason":"Exact manual CI not visible"}
        run=max(exact,key=lambda r:r["id"])
        report={k:run[k] for k in ("id","status","conclusion","html_url")}
        report["revision"]=revision
        if run["status"]=="completed":
            assert run["conclusion"]=="success","Exact release CI failed"
            jobs=[];page=1
            while True:
                batch=self.github("actions/runs/"+str(run["id"])+"/jobs?per_page=100&page="+str(page))["jobs"];jobs+=batch
                if len(batch)<100:break
                page+=1;assert page<=5
            required=[j for j in jobs if j["name"]=="CI required"]
            assert len(required)==1 and required[0]["conclusion"]=="success"
            expected={"Plan validation","CI required"}|{"Validate ("+n+")" for n in
                ("fast","schedule-foundation","schedule-a","schedule-b","purchasing","inventory-access","platform-forecast","other")}
            assert {j["name"] for j in jobs}==expected and all(j["conclusion"]=="success" for j in jobs)
            report["status"]="PASS";report["jobs"]=[{"name":j["name"],"conclusion":j["conclusion"]} for j in jobs]
        return report
    def require_ci(self):
        report=load("ci")
        assert report["status"]=="PASS"
        for rev,branch in ((self.rev,self.cfg["publish_branch"]),(self.recovery,self.cfg["recovery_branch"]),(self.cron,self.cfg["cron_branch"])):
            assert self.ci_report(rev,branch)["status"]=="PASS"

def tables(c):
    from sqlalchemy import text
    return [tuple(row) for row in c.execute(text(
        "SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND n.nspname NOT LIKE 'pg_toast%' ORDER BY n.nspname,c.relname"))]
FINGERPRINT_SCHEME="pg-row-json-sha256-multiset-v1"
def fingerprints(c,names):
    from sqlalchemy import text
    result={}
    for schema,name in names:
        digest=hashlib.sha256();count=0
        # Transfer fixed-width row digests, preserving duplicate rows and identity.
        sql=("SELECT row_digest FROM (SELECT encode(sha256(convert_to(row_to_json(t)::text,'UTF8')),'hex') "
             "AS row_digest FROM "+quoted(schema)+"."+quoted(name)+" t) digests "
             "ORDER BY row_digest COLLATE \"C\"")
        for raw in c.scalars(text(sql).execution_options(stream_results=True, yield_per=1000)):
            assert isinstance(raw,str) and re.fullmatch("[a-f0-9]{64}",raw)
            digest.update(raw.encode("ascii")+b"\n");count+=1
        result[schema+"."+name]={"scheme":FINGERPRINT_SCHEME,"rows":count,"sha256":digest.hexdigest()}
    return result
def protected_tables(c):
    return [(schema,name) for schema,name in tables(c) if (schema,name) not in
            {("public","web_sessions"),("public","alembic_version")} and not name.startswith(("fi_","cp_"))]

def private_bundle():
    # These reviewed inputs are never committed to the public source repository.
    assert PRIVATEBUNDLE.is_dir() and not PRIVATEBUNDLE.is_symlink()
    manifest=PRIVATEBUNDLE/"manifest.json";seed=PRIVATEBUNDLE/"manufacturer-seed.json"
    assert manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size<=262144
    raw=manifest.read_bytes();assert sha(raw)==PRIVATE_MANIFEST_SHA
    data=json.loads(raw)
    assert set(data)=={"version","photos","excludedVariationIds"} and data["version"]==1
    assert len(data["photos"])==6 and len(set(data["excludedVariationIds"]))==28
    paths=[manifest,seed];total=0;variations=set()
    for item in data["photos"]:
        asset=item["asset"];name=item["file"]
        assert re.fullmatch("[a-f0-9]{64}",asset["id"]) and name==asset["id"]+".webp"
        path=PRIVATEBUNDLE/name
        assert path.is_file() and not path.is_symlink()
        content=path.read_bytes()
        assert asset["mime"]=="image/webp" and len(content)==asset["byte_size"] and sha(content)==asset["id"]
        assert 0<asset["width"]<=1200 and 0<asset["height"]<=1200 and 0<len(content)<=2*1024*1024
        paths.append(path);total+=len(content);variations.update(item["product"]["squareVariationIds"])
    assert total==421564 and len(variations)==21
    assert seed.is_file() and not seed.is_symlink() and seed.stat().st_size<=2000000
    assert file_sha(seed)==PRIVATE_SEED_SHA
    assert len(paths)==8 and {p.name for p in PRIVATEBUNDLE.iterdir()}=={p.name for p in paths}
    return {"paths":paths,"manifest_sha256":PRIVATE_MANIFEST_SHA,"seed_sha256":PRIVATE_SEED_SHA,
            "manufacturer_images":6,"canonical_bytes":total,"exact_variations":21}

def native_control_check(*,qualifying=False):
    # Fixed interpreter, fixed script and fixed read-only mode; no sandbox nesting.
    mode="control-check-qualify" if qualifying else "control-check"
    subprocess.run([str(NATIVE_CONTROL),"-I","-B",str(OUT/"release_transport.py"),mode],check=True)

def qualify(ctx):
    import importlib.util
    transport=OUT/"release_transport.py"
    spec=importlib.util.spec_from_file_location("qualified_product_transport",transport)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    native_control_check(qualifying=True)
    config=module.configuration();module.frozen_source(config)
    assert file_sha(OUT/"local-checks.json")==config["local_checks_sha256"] and load("local-checks")["status"]=="PASS"
    assert file_sha(OUT/"release_transport.py")==file_sha(ROOT/"source/scripts/development/product_explorer_release_transport.py")
    assert file_sha(OUT/"release_worker.py")==file_sha(ROOT/"source/scripts/development/product_explorer_release_worker.py")
    assert validate_status("pre_deploy_in_progress")=="pre_deploy_in_progress"
    for invalid in ("done","success","unknown"):
        try:validate_status(invalid)
        except ValueError:pass
        else:raise AssertionError("Unknown provider status accepted")
    original={"a":"keep","FORECAST_ADVISORY_ASSET_SPEC":"old","FORECAST_PRODUCT_ARTIFACT_URL":"old"}
    expected=dict(original,FORECAST_ADVISORY_ASSET_SPEC="new",FORECAST_PRODUCT_ARTIFACT_URL="new")
    validate_environment(original,expected)
    for bad in (dict(expected,a="changed"),dict(expected,extra="bad"),dict(expected,FORECAST_PRODUCT_ARTIFACT_URL="old")):
        try:validate_environment(original,bad)
        except AssertionError:pass
        else:raise AssertionError("Unexpected environment drift accepted")
    allowed=OUT/".own-probe";assert not allowed.exists()
    allowed.write_text("confined");allowed.unlink()
    denied=ROOT/"source/.unregistered-product-release-probe";assert not denied.exists()
    script="import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('denied')"
    result=subprocess.run([str(module.e.PYTHON),"-I","-B","-c",script,str(denied)],capture_output=True)
    assert result.returncode!=0 and not denied.exists()
    for path in (ROOT/"source/app/models.py",Path("/Users/justinrawlinson/Developer/Erupted/canonical/app/models.py")):
        result=subprocess.run([str(module.e.PYTHON),"-I","-B","-c","import sys;open(sys.argv[1],'r+')",str(path)],capture_output=True)
        assert result.returncode!=0,"Protected source write handle admitted"
    paths=[transport,OUT/"release_worker.py",OUT/"config.json",OUT/"local-checks.json",
           module.PROFILE/"erupted-product-release-dev.py",module.PROFILE/"qualification.json",
           module.base.B/"ava-dev.py",module.base.B/"kashew-process-stats"]
    paths += [Path(name) for name in module.base.read(module.PROFILE/"qualification.json")["hashes"]]
    paths += private_bundle()["paths"]
    public_seed=json.loads(git("show",ctx.rev+":app/services/flavor_explorer/compatibility/manufacturer-seed.json"))
    assert not public_seed.get("catalogAssociations") and not any(p.get("identifiers") for p in public_seed["products"])
    assert len(public_seed["products"])==157 and len(public_seed["relationships"])==388 and len(public_seed["evidence"])==33
    assert all(not str(e.get("key","")).startswith("square-catalog") for e in public_seed["evidence"])
    report={"status":"PASS","at":stamp(),"config":config,"actions":sorted(module.ACTIONS),
      "checks":{"fixedActionAndSourcePins":True,"localQualificationPassed":True,"providerStatusNegatives":True,
        "exactTwoKeyEnvironmentDelta":True,"inheritedSourceWriteDenials":True,"confinedRetainedOutput":True,
        "originalRegressionLimitsPreserved":True,"installedDevelopmentQualificationUnchanged":True,
        "noSSHNoManualCronRunNoGenericCommand":True},
      "hashes":{str(path):file_sha(path) for path in sorted(set(paths))}}
    save("qualification",report,True);print(json.dumps({"status":"PASS","checks":report["checks"]}),flush=True)

def publish_one(ctx,revision,branch,label,parent=None):
    parent=parent or ctx.base
    # API object publication avoids altering the registered pre-push denial hook.
    # Every uploaded blob, tree and commit must match the already-pinned local object.
    try:existing=ctx.github("git/ref/heads/"+branch)
    except urllib.error.HTTPError as error:
        assert error.code==404;existing=None
    if existing is not None:
        assert existing["object"]["sha"]==revision,"Unique release branch points to an unreviewed revision"
        assert ctx.github("git/commits/"+revision)["sha"]==revision
        print(json.dumps({"publication":label,"status":"EXACT_REF_ALREADY_PRESENT","revision":revision}),flush=True)
        return
    assert git("rev-parse",revision+"^").decode().strip()==parent
    changes=git("diff-tree","--no-commit-id","--raw","-r","-z",parent,revision).split(b"\x00")
    records=[];i=0
    while i<len(changes) and changes[i]:
        header=changes[i].decode();path=changes[i+1].decode();i+=2
        oldmode,newmode,oldsha,newsha,status=header.split()
        assert status in {"A","M","D"} and not path.startswith("/") and ".." not in Path(path).parts
        if status=="D":records.append({"path":path,"mode":oldmode.lstrip(":"),"type":"blob","sha":None});continue
        assert newmode in {"100644","100755"} and re.fullmatch("[a-f0-9]{40}",newsha)
        content=git("cat-file","blob",newsha)
        assert len(content)<4*1024*1024
        result=ctx.gh_mutate("git/blobs",{"content":base64.b64encode(content).decode(),"encoding":"base64"},label+"-blob-"+str(len(records)),expected_sha=newsha)
        assert result["sha"]==newsha
        records.append({"path":path,"mode":newmode,"type":"blob","sha":newsha})
        if len(records)%10==0:print(json.dumps({"publication":label,"verified_blobs":len(records)}),flush=True)
    result=ctx.gh_mutate("git/trees",{"base_tree":git("rev-parse",parent+"^{tree}").decode().strip(),"tree":records},label+"-tree",expected_sha=git("rev-parse",revision+"^{tree}").decode().strip())
    assert result["sha"]==git("rev-parse",revision+"^{tree}").decode().strip()
    payload=commit_payload(git("cat-file","commit",revision))
    result=ctx.gh_mutate("git/commits",payload,label+"-commit",expected_sha=revision)
    assert result["sha"]==revision,"GitHub commit identity differed; no branch was published"
    result=ctx.gh_mutate("git/refs",{"ref":"refs/heads/"+branch,"sha":revision},label+"-ref",expected_sha=revision)
    assert result["object"]["sha"]==revision
    assert ctx.github("git/ref/heads/"+branch)["object"]["sha"]==revision
def publish(ctx):
    ctx.assert_source()
    publish_one(ctx,ctx.cron,ctx.cfg["cron_branch"],"cron-publish",parent=CRON_BASELINE)
    publish_one(ctx,ctx.recovery,ctx.cfg["recovery_branch"],"recovery-publish")
    publish_one(ctx,ctx.rev,ctx.cfg["publish_branch"],"candidate-publish")
    report={"status":"PASS","revision":ctx.rev,"recovery_revision":ctx.recovery,"cron_revision":ctx.cron}
    if (OUT/"published.json").exists():assert load("published")==report
    else:save("published",report,True)
    print(json.dumps(load("published")),flush=True)
def dispatch(ctx):
    assert load("published")["status"]=="PASS"
    for label,revision,branch in (("candidate",ctx.rev,ctx.cfg["publish_branch"]),("recovery",ctx.recovery,ctx.cfg["recovery_branch"]),("cron",ctx.cron,ctx.cfg["cron_branch"])):
        assert ctx.github("git/ref/heads/"+branch)["object"]["sha"]==revision
        existing=ctx.ci_report(revision,branch)
        if existing.get("id"):
            save(label+"-ci-reused",{"at":stamp(),"id":existing["id"],"revision":revision,
                 "status":existing["status"],"full_manual_run":True})
        else:ctx.gh_mutate("actions/workflows/ci.yml/dispatches",{"ref":branch},label+"-ci-dispatch")
    print(json.dumps({"status":"DISPATCHED","candidate":ctx.rev,"recovery":ctx.recovery,"cron":ctx.cron}),flush=True)
def ci(ctx):
    reports=[ctx.ci_report(rev,branch) for rev,branch in
        ((ctx.rev,ctx.cfg["publish_branch"]),(ctx.recovery,ctx.cfg["recovery_branch"]),(ctx.cron,ctx.cfg["cron_branch"]))]
    report={"at":stamp(),"status":"PASS" if all(r["status"]=="PASS" for r in reports) else "WAIT","runs":reports}
    save("ci",report);print(json.dumps(report),flush=True)

def preflight(ctx):
    from sqlalchemy import text
    ctx.assert_source()
    service=ctx.request("services/"+WEB);live=ctx.live()
    assert live["commit"]["id"]==ctx.base and live["id"]==ctx.cfg["baseline_deploy"]
    assert service["branch"]=="main" and service["autoDeployTrigger"]=="off"
    assert not any(validate_status(d["status"]) in PENDING for d in ctx.deployments())
    env=ctx.environment();roles={name:ctx.role(sid) for name,sid in ROLES.items()}
    original_cron_tip=ctx.github("git/ref/heads/"+CRON_ORIGINAL_BRANCH)["object"]["sha"]
    assert re.fullmatch("[a-f0-9]{40}",original_cron_tip)
    assert roles["staff"]["service"]["branch"]==CRON_ORIGINAL_BRANCH
    assert roles["staff"]["revision"]=="af00e6dee9738b22afeaaed9951d029be1b21cbc"
    assert roles["staff"]["service"]["serviceDetails"]["schedule"]=="0 10 * * 0"
    recovery=ctx.request("postgres/"+DB+"/recovery");assert recovery["recoveryStatus"]=="AVAILABLE"
    secret_files=ctx.secret_files();groups=ctx.env_groups()
    assert secret_files[1]==groups[1]==0
    assert len(env)==61 and int(env["PURCHASING_OWNER_PRINCIPAL_ID"])==ctx.cfg["owner_id"]==6
    engine=ctx.engine(True)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            assert c.scalar(text("SHOW transaction_read_only"))=="on"
            assert c.scalar(text("SELECT version_num FROM public.alembic_version"))=="20261119_0057"
            assert c.scalar(text("SELECT version_num FROM purchasing_v4.alembic_version"))=="p4_0003"
            assert c.scalar(text("SELECT count(*) FROM principals WHERE id=6 AND active AND role::text='ADMIN'"))==1
            assert not c.scalar(text("SELECT count(*) FROM employees WHERE principal_id=6 AND NOT active"))
            assert c.scalar(text("SELECT to_regclass('public.fi_catalog_observations')")) is None
            names=protected_tables(c);rows=fingerprints(c,names)
            sessions=c.scalar(text("SELECT count(*) FROM web_sessions WHERE principal_id=6 AND revoked_at IS NULL AND expires_at>now()+interval '5 minutes'"))
            report={"status":"PASS","at":stamp(),"baseline":ctx.base,"baseline_deploy":live["id"],
              "schema":"20261119_0057","postgres_version":c.scalar(text("SHOW server_version")),"owner_id":6,
              "active_owner_sessions":sessions,"managed_recovery":recovery,"legacy_tables":len(names),"protected":rows,
              "environment_sha256":sha(canonical(env)),"environment_hashes":{k:sha(v.encode()) for k,v in env.items()},
              "protected_service_sha256":sha(canonical(protected_service(service))),"secret_files":secret_files,"env_groups":groups}
    finally:engine.dispose()
    with (ctx.private/"baseline.json").open("x") as f:
        json.dump({"service":service,"protected_service":protected_service(service),"deploy":live,"environment":env,
                   "roles":roles,"secret_files":secret_files,"env_groups":groups,"cron_original_branch_tip":original_cron_tip},f)
    save("preflight",report,True)
    print(json.dumps({k:v for k,v in report.items() if k not in {"protected","environment_hashes"}}),flush=True)

def rehearsal(ctx):
    from sqlalchemy import create_engine,text
    ctx.assert_source()
    assert PGBIN.is_dir() and (PGBIN/"postgres").is_file()
    remote=ctx.engine(True);process=None;local=None;handle=None
    work=Path(tempfile.mkdtemp(prefix="product-explorer-restore-",dir=os.environ["TMPDIR"]));marker=work/"owned.json"
    marker.write_text(json.dumps({"session":SID,"purpose":"ephemeral isolated restore and migration proof","created":stamp()}))
    dump=work/"snapshot.dump";cluster=work/"cluster";log=work/"postgres.log"
    report={"status":"FAIL","at":stamp(),"revision":ctx.rev,"database_resource":DB,"new_persistent_backup_destination":False}
    try:
        with remote.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            assert c.scalar(text("SHOW transaction_read_only"))=="on"
            assert c.scalar(text("SELECT version_num FROM public.alembic_version"))=="20261119_0057"
            snapshot=c.scalar(text("SELECT pg_export_snapshot()"));assert re.fullmatch("[A-Fa-f0-9-]+",snapshot)
            all_tables=tables(c);before=fingerprints(c,all_tables)
            url=remote.url
            env=dict(os.environ,PGPASSWORD=url.password or "",PGSSLMODE="require")
            def limit_file():resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_DUMP,MAX_DUMP))
            command=[str(PGBIN/"pg_dump"),"--format=custom","--no-owner","--no-acl","--no-comments",
                     "--host",url.host,"--port",str(url.port or 5432),"--username",url.username,"--dbname",url.database,
                     "--snapshot",snapshot,"--file",str(dump)]
            completed=subprocess.run(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=300,preexec_fn=limit_file)
            if completed.returncode or completed.stderr:
                report["pg_dump_stderr"]=retain_rehearsal_stderr(ctx,"pg-dump",completed.stderr)
            assert completed.returncode==0,"Bounded production read-only dump failed"
            assert 0<dump.stat().st_size<=MAX_DUMP
            dump_hash=file_sha(dump);dump_bytes=dump.stat().st_size
        subprocess.run([str(PGBIN/"initdb"),"-D",str(cluster),"-U","product_restore_admin","--auth=trust","--no-locale","-E","UTF8"],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        handle=log.open("wb")
        process=subprocess.Popen([str(PGBIN/"postgres"),"-D",str(cluster),"-h","127.0.0.1","-p","55486","-k","",
            "-c","shared_memory_type=mmap","-c","dynamic_shared_memory_type=mmap","-c","shared_buffers=16MB",
            "-c","max_connections=12","-c","max_locks_per_transaction=256","-c","work_mem=1MB","-c","fsync=off","-c","full_page_writes=off","-c","max_wal_size=64MB"],
            stdout=handle,stderr=subprocess.STDOUT)
        admin=create_engine("postgresql+psycopg://product_restore_admin@127.0.0.1:55486/postgres")
        deadline=time.monotonic()+20
        while True:
            assert process.poll() is None,"Owned PostgreSQL18 startup failed"
            try:
                with admin.connect() as c:
                    assert Path(c.scalar(text("SHOW data_directory"))).resolve()==cluster.resolve()
                break
            except Exception:
                if time.monotonic()>deadline:raise RuntimeError("Owned PostgreSQL18 startup deadline")
                time.sleep(.1)
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql("CREATE ROLE product_restore_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
            c.exec_driver_sql("CREATE DATABASE product_explorer_rehearsal OWNER product_restore_app")
        admin.dispose()
        toc=subprocess.check_output([str(PGBIN/"pg_restore"),"--list",str(dump)],text=True,timeout=20)
        if re.search(r"(?m)^[0-9]+;\s+[0-9]+\s+[0-9]+\s+SCHEMA\s+-\s+public\s+",toc):
            # A fresh PostgreSQL database already has an empty public namespace.
            # Retire only that owned empty namespace if the archive recreates it.
            assert process.poll() is None and int((cluster/"postmaster.pid").read_text().splitlines()[0])==process.pid
            fresh=create_engine("postgresql+psycopg://product_restore_app@127.0.0.1:55486/product_explorer_rehearsal")
            try:
                with fresh.begin() as c:
                    assert c.scalar(text("SELECT current_database()"))=="product_explorer_rehearsal" and tables(c)==[]
                    c.exec_driver_sql("DROP SCHEMA public")
            finally:fresh.dispose()
        completed=subprocess.run([str(PGBIN/"pg_restore"),"--exit-on-error","--single-transaction","--no-owner","--no-acl",
            "--host","127.0.0.1","--port","55486","--username","product_restore_app",
            "--dbname","product_explorer_rehearsal",str(dump)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=300)
        if completed.returncode or completed.stderr:
            report["pg_restore_stderr"]=retain_rehearsal_stderr(ctx,"pg-restore",completed.stderr)
        assert completed.returncode==0,"Owned PostgreSQL18 isolated restore failed"
        local=create_engine("postgresql+psycopg://product_restore_app@127.0.0.1:55486/product_explorer_rehearsal",
            connect_args={"options":"-c timezone=UTC -c statement_timeout=120000 -c lock_timeout=5000"})
        from app.services.flavor_explorer.production_schema_v0058 import upgrade,verify_schema
        with local.begin() as c:
            assert not c.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user"))
            assert c.scalar(text("SHOW server_version")).startswith("18.6")
            restored=fingerprints(c,all_tables);assert restored==before,"Isolated restore row fingerprint differs"
            legacy=protected_tables(c);legacy_before=fingerprints(c,legacy)
        cron_guard=load_cron_guard(ctx)
        assert cron_guard.settings.schema_revision_check_enabled
        cron_guard.assert_supported_schema(local)
        try:
            with local.begin() as c:c.execute(text("UPDATE public.alembic_version SET version_num='20261121_0059'"))
            try:cron_guard.assert_supported_schema(local)
            except cron_guard.UnsupportedSchemaError:pass
            else:raise AssertionError("Cron compatibility guard accepted an unreviewed future schema")
        finally:
            with local.begin() as c:c.execute(text("UPDATE public.alembic_version SET version_num='20261119_0057'"))
        with local.begin() as c:
            upgrade(c)
            c.execute(text("UPDATE public.alembic_version SET version_num='20261120_0058' WHERE version_num='20261119_0057'"))
            assert fingerprints(c,legacy)==legacy_before,"Migration modified existing business rows"
        schema=verify_schema(local)
        cron_guard.assert_supported_schema(local)
        with local.connect() as c:
            assert c.scalar(text("SELECT version_num FROM public.alembic_version"))=="20261120_0058"
            assert fingerprints(c,legacy)==legacy_before
        recovery=ctx.request("postgres/"+DB+"/recovery")
        assert recovery["recoveryStatus"]=="AVAILABLE"
        recovery_checked_at=stamp()
        report.update(status="PASS",finished_at=stamp(),dump_sha256=dump_hash,dump_bytes=dump_bytes,
            restored_tables=len(all_tables),restore_row_fingerprints_match=True,protected_tables=len(legacy),
            migration_existing_rows_unchanged=True,application_user_superuser=False,schema_guard=schema,
            shared_lock_and_original_limits=True,managed_recovery_still_available=True,managed_recovery_checked_at=recovery_checked_at,
            cron_guard={"revision":ctx.cron,"parent":CRON_BASELINE,"file_sha256":sha(git("show",ctx.cron+":app/schema_contract.py")),
                "accepted":["20261119_0057","20261120_0058"],"future_0059_rejected":True,"scheduling_job_invoked":False})
    finally:
        remote.dispose()
        if local is not None:local.dispose()
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        if handle is not None:handle.close()
        assert marker.is_file() and json.loads(marker.read_text())["session"]==SID
        if process is None or process.poll() is not None:
            shutil.rmtree(work);report["owned_fixture_removed_after_stop"]=True
        attempt="rehearsal-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        save(attempt,report,True)
        save("rehearsal",report)  # Current index; immutable attempt evidence stays retained.
    print(json.dumps(report),flush=True)

def prepare(ctx):
    import sqlite3
    from scripts.release.artifact_preflight import configuration,repackage,BUILD_COMMAND
    from scripts.release.phase3.manifest import identity_at
    ctx.assert_source();ctx.require_ci()
    reviewed_bundle=private_bundle()
    preflight=load("preflight");rehearsal=load("rehearsal")
    assert preflight["status"]==rehearsal["status"]=="PASS"
    assert rehearsal["revision"]==ctx.rev and rehearsal["restore_row_fingerprints_match"] and rehearsal["migration_existing_rows_unchanged"]
    baseline=ctx.baseline();ctx.preserve_controls(baseline["environment"])
    assert ctx.live()["id"]==ctx.cfg["baseline_deploy"] and ctx.live()["commit"]["id"]==ctx.base
    env=baseline["environment"]
    assert baseline["service"]["serviceDetails"]["envSpecificDetails"]["buildCommand"]==BUILD_COMMAND
    unchanged=("requirements.txt","requirements-test.txt",".python-version","scripts/release","frontend/purchasing-v4","app/purchasing_v4/worksheet_static")
    assert not git("diff","--name-only",ctx.base,ctx.rev,"--",*unchanged).strip()
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    credentials=json.loads(Path("/Users/justinrawlinson/Developer/Erupted/output/forecast-stage5b-20261023/r2-durable-credentials.private.json").read_text())
    assert credentials["bucket"]=="erupted-forecast-backups"
    s3=boto3.client("s3",endpoint_url=credentials["endpoint_url"],aws_access_key_id=credentials["access_key_id"],
        aws_secret_access_key=credentials["secret_access_key"],region_name="auto",
        config=Config(connect_timeout=5,read_timeout=40,retries={"max_attempts":0}))
    oldspec=json.loads(env["FORECAST_ADVISORY_ASSET_SPEC"]);assert oldspec["revision"]==ctx.base
    assert 0<oldspec["archive_bytes"]<=256*1024*1024
    oldkey="forecast-advisory-deployments/v1/"+ctx.base+"/"+oldspec["archive_sha256"]+".tar.gz"
    with s3.get_object(Bucket=credentials["bucket"],Key=oldkey)["Body"] as body:old=body.read(oldspec["archive_bytes"]+1)
    assert len(old)==oldspec["archive_bytes"] and sha(old)==oldspec["archive_sha256"]
    with tarfile.open(fileobj=io.BytesIO(old),mode="r:gz") as archive:
        members=archive.getmembers()
        assert len(members)==2 and {m.name for m in members}=={"product.sqlite","release-manifest.json"}
        assert all(m.isfile() and not m.issym() and not m.islnk() for m in members)
        assert archive.getmember("product.sqlite").size==oldspec["projection_bytes"]
        assert archive.getmember("release-manifest.json").size<65536
        manifest=json.load(archive.extractfile("release-manifest.json"));projection=archive.extractfile("product.sqlite").read()
    assert sha(projection)==oldspec["projection_sha256"] and manifest["web_candidate"]==ctx.base
    work=Path(tempfile.mkdtemp(prefix="product-release-artifact-",dir=os.environ["TMPDIR"]))
    artifacts={
        "cron":{**{k:v for k,v in identity_at(ROOT/"source",ctx.cron).items() if k!="files"},
            "parent":CRON_BASELINE,"file_sha256":sha(git("show",ctx.cron+":app/schema_contract.py"))}}
    try:
        projection_path=work/"product.sqlite";projection_path.write_bytes(projection)
        with sqlite3.connect(projection_path.resolve().as_uri()+"?mode=ro",uri=True) as db:
            assert db.execute("PRAGMA application_id").fetchone()[0]==1163282000
            assert db.execute("PRAGMA user_version").fetchone()[0]==2
            assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
            assert db.execute("SELECT count(*) FROM products").fetchone()[0]==21152
            assert db.execute("SELECT count(*) FROM details").fetchone()[0]==5288
        for label,revision in (("candidate",ctx.rev),("recovery",ctx.recovery)):
            destination=work/(label+".tar.gz")
            artifact=repackage(projection_path,manifest,oldspec["projection_sha256"],revision,destination)
            payload=destination.read_bytes()
            key="forecast-advisory-deployments/v1/"+revision+"/"+artifact["archive_sha256"]+".tar.gz"
            save(label+"-artifact-intent",{"at":stamp(),"bucket":credentials["bucket"],"key":key,
                 "sha256":sha(payload),"projection_unchanged":True,"new_backup_destination":False},True)
            try:s3.head_object(Bucket=credentials["bucket"],Key=key)
            except ClientError as error:
                assert error.response["ResponseMetadata"]["HTTPStatusCode"]==404
                s3.put_object(Bucket=credentials["bucket"],Key=key,Body=payload,ContentType="application/gzip",
                    CacheControl="private, no-store",IfNoneMatch="*",
                    Metadata={"sha256":artifact["archive_sha256"],"purpose":"staff-product-explorer-release"})
            with s3.get_object(Bucket=credentials["bucket"],Key=key)["Body"] as body:assert body.read(len(payload)+1)==payload
            after=dict(env,FORECAST_ADVISORY_ASSET_SPEC=json.dumps(artifact,separators=(",",":")),
                FORECAST_PRODUCT_ARTIFACT_URL=s3.generate_presigned_url("get_object",
                    Params={"Bucket":credentials["bucket"],"Key":key},ExpiresIn=43200))
            validate_environment(env,after)
            configuration(after,revision,BUILD_COMMAND,minimum_validity=7200)
            with (ctx.private/(label+"-environment.json")).open("x") as f:json.dump(after,f)
            identity=identity_at(ROOT/"source",revision)
            artifacts[label]={"revision":revision,"source_tree_sha256":identity["source_tree_sha256"],
                "artifact":artifact,"object_key":key,"environment_sha256":sha(canonical(after)),
                "changed_environment_keys":sorted(ENV_CHANGES)}
            save(label+"-artifact-verified",artifacts[label],True)
    finally:shutil.rmtree(work)
    report={"status":"PASS","at":stamp(),"revision":ctx.rev,"recovery_revision":ctx.recovery,"baseline":ctx.base,
            "schema_from":"20261119_0057","schema_expected":"20261120_0058","artifacts":artifacts,
            "migration_path_sha256":file_sha(ROOT/"source/migrations/versions/20261120_0058_product_explorer.py"),
            "frozen_schema_sha256":file_sha(ROOT/"source/app/services/flavor_explorer/production_schema_v0058.py"),
            "import_sha256":file_sha(ROOT/"source/app/services/flavor_explorer/production_import.py"),
            "existing_projection_unchanged":True,"native_full_CI":True,"fresh_current_isolated_restore":True,
            "no_scheduling_command_or_cadence_change":True,"new_persistent_backup_destination":False,
            "private_bundle":{k:v for k,v in reviewed_bundle.items() if k!="paths"}}
    save("candidate",report,True);print(json.dumps(report),flush=True)

def active_owner_cookie(ctx,minimum_minutes=2):
    from sqlalchemy import text
    engine=ctx.engine(True)
    try:
        with engine.connect() as c:
            assert c.scalar(text("SHOW transaction_read_only"))=="on"
            row=c.execute(text("""SELECT w.session_token,w.expires_at FROM public.web_sessions w
                JOIN public.principals p ON p.id=w.principal_id
                LEFT JOIN public.employees e ON e.principal_id=p.id
                WHERE p.id=:owner AND p.active AND p.role::text='ADMIN' AND (e.id IS NULL OR e.active)
                AND w.revoked_at IS NULL AND w.expires_at>:minimum
                ORDER BY w.last_seen_at DESC LIMIT 1"""),
                {"owner":ctx.cfg["owner_id"],"minimum":datetime.now(timezone.utc)+timedelta(minutes=minimum_minutes)}).first()
            if row is None:raise PermissionError("Normal owner sign-in is required; no active owner session is available")
    finally:engine.dispose()
    return ctx.environment().get("SESSION_COOKIE_NAME","blind_inventory_session")+"="+row.session_token
def owner_get(ctx,path,cookie,*,bytes_limit=8*1024*1024):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    request=urllib.request.Request(ORIGIN+path,method="GET",headers={"Cookie":cookie,
        "User-Agent":"Erupted owner-approved Product Explorer release verification","Accept-Encoding":"identity"})
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=45) as response:
        body=response.read(bytes_limit+1)
        assert len(body)<=bytes_limit and response.status==200
        return body,response.headers

def refresh_preflight(ctx):
    from sqlalchemy import text
    require_bridge(ctx)
    ctx.preserve_controls(json.loads((ctx.private/"recovery-environment.json").read_text()))
    engine=ctx.engine(True)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            assert c.scalar(text("SHOW transaction_read_only"))=="on"
            assert c.scalar(text("SELECT version_num FROM public.alembic_version"))=="20261119_0057"
            names=protected_tables(c);current=fingerprints(c,names);original=load("preflight")["protected"]
            assert set(current)==set(original),"Existing business table set changed"
            changed=[name for name in current if current[name]!=original[name]]
    finally:engine.dispose()
    numbers=[]
    for path in OUT.glob("preflight-refresh-*.json"):
        match=re.fullmatch(r"preflight-refresh-([0-9]{3})\.json",path.name)
        assert match
        numbers.append(int(match.group(1)))
    number=max(numbers,default=0)+1;assert number<=999
    name="preflight-refresh-"+format(number,"03")
    report={"status":"PASS","at":stamp(),"revision":ctx.rev,"bridge_deployment":load("bridge-deployment")["id"],
        "schema":"20261119_0057","protected":current,"changed_since_original":changed,
        "read_only":True,"release_mutations_performed":False}
    save(name,report,True)
    save("preflight-current",{"receipt":name,"sha256":file_sha(OUT/(name+".json"))})
    print(json.dumps({"status":"PASS","receipt":name,"protected_tables":len(current),"changed_since_original":changed}),flush=True)

def current_preflight(ctx):
    pointer=load("preflight-current")
    assert re.fullmatch(r"preflight-refresh-[0-9]{3}",pointer["receipt"])
    assert file_sha(OUT/(pointer["receipt"]+".json"))==pointer["sha256"]
    report=load(pointer["receipt"])
    assert report["status"]=="PASS" and report["revision"]==ctx.rev and report["schema"]=="20261119_0057"
    assert report["bridge_deployment"]==load("bridge-deployment")["id"] and report["read_only"]
    at=datetime.fromisoformat(report["at"]);assert at.tzinfo is not None
    assert 0<=(datetime.now(timezone.utc)-at).total_seconds()<=300,"Fresh locked-admission comparison is required"
    return report

def migrate(ctx):
    from sqlalchemy import text
    from app.services.flavor_explorer.production_schema_v0058 import upgrade,verify_schema,EXPLORER_LOCK
    ctx.assert_source();ctx.require_ci();candidate=load("candidate")
    assert candidate["status"]=="PASS" and candidate["revision"]==ctx.rev and load("rehearsal")["status"]=="PASS"
    assert candidate["frozen_schema_sha256"]==file_sha(ROOT/"source/app/services/flavor_explorer/production_schema_v0058.py")
    assert candidate["migration_path_sha256"]==file_sha(ROOT/"source/migrations/versions/20261120_0058_product_explorer.py")
    baseline=ctx.baseline();ctx.preserve_controls(json.loads((ctx.private/"recovery-environment.json").read_text()))
    require_bridge(ctx)
    require_cron_compatibility(ctx)
    recovery=ctx.request("postgres/"+DB+"/recovery");assert recovery["recoveryStatus"]=="AVAILABLE"
    cookie=active_owner_cookie(ctx,minimum_minutes=10)
    observed=json.loads(owner_get(ctx,"/management/deployment/runtime",cookie)[0]);cookie=None
    assert observed["revision"]==ctx.recovery and observed["owner_authorized"] and observed["principal_id"]==6 and observed["files_still_match"]
    assert observed["source_tree_sha256"]==candidate["artifacts"]["recovery"]["source_tree_sha256"]
    assert not (OUT/"migration-intent.json").exists(),"Prior migration requires read-only reconciliation"
    engine=ctx.engine()
    try:
        with engine.begin() as c:
            assert c.scalar(text("SELECT version_num FROM public.alembic_version"))=="20261119_0057"
            assert not c.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user"))
            assert c.scalar(text("SELECT to_regclass('public.fi_catalog_observations')")) is None
            c.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":EXPLORER_LOCK})
            names=protected_tables(c)
            c.exec_driver_sql("LOCK TABLE "+",".join(quoted(s)+"."+quoted(t) for s,t in names)+" IN SHARE MODE")
            before=fingerprints(c,names)
            assert before==current_preflight(ctx)["protected"],"Business records changed since the fresh read; refresh read-only before migration"
            save("migration-intent",{"at":stamp(),"revision":ctx.rev,"database":DB,"from":"20261119_0057",
                 "to":"20261120_0058","frozen_schema_sha256":candidate["frozen_schema_sha256"],"managed_recovery":recovery},True)
            upgrade(c)
            result=c.execute(text("UPDATE public.alembic_version SET version_num='20261120_0058' WHERE version_num='20261119_0057'"))
            assert result.rowcount==1 and fingerprints(c,names)==before
            save("migration-precommit",{"at":stamp(),"revision":ctx.rev,"protected":before,"existing_rows_unchanged":True},True)
        guard=verify_schema(engine)
        with engine.connect() as c:
            assert c.scalar(text("SELECT version_num FROM public.alembic_version"))=="20261120_0058"
            assert fingerprints(c,names)==before
        save("migration-applied",{"status":"PASS","at":stamp(),"revision":ctx.rev,"schema":"20261120_0058",
             "protected":before,"existing_rows_unchanged":True,"guard":guard},True)
        print(json.dumps({"status":"PASS","schema":"20261120_0058","existing_rows_unchanged":True}),flush=True)
    except Exception as error:
        if (OUT/"migration-intent.json").exists():
            save("migration-stopped",{"at":stamp(),"kind":type(error).__name__,"reconcile_read_only":True},True)
        else:
            save("migration-held-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
                {"at":stamp(),"kind":type(error).__name__,"no_mutation_intent_submitted":True},True)
        raise
    finally:engine.dispose()

def import_release(ctx):
    from sqlalchemy import text
    ctx.assert_source();candidate=load("candidate")
    assert load("migration-applied")["status"]=="PASS" and candidate["import_sha256"]==file_sha(ROOT/"source/app/services/flavor_explorer/production_import.py")
    require_bridge(ctx)
    ctx.preserve_controls(json.loads((ctx.private/"recovery-environment.json").read_text()))
    assert not (OUT/"import-intent.json").exists(),"Prior import requires read-only reconciliation"
    private_bundle()
    env=ctx.environment()
    # Only the fixed existing centralized read gateway receives Square credentials.
    for key in ("SQUARE_ACCESS_TOKEN","SQUARE_API_VERSION","SQUARE_API_BASE_URL","SQUARE_TIMEOUT_SECONDS"):
        if key in env:os.environ[key]=env[key]
    engine=ctx.engine()
    try:
        with engine.connect() as c:
            assert c.scalar(text("SELECT version_num FROM public.alembic_version"))=="20261120_0058"
            assert c.scalar(text("SELECT count(*) FROM principals WHERE id=6 AND active AND role::text='ADMIN'"))==1
            before=fingerprints(c,protected_tables(c))
            assert before==load("migration-applied")["protected"]
        from app.services.flavor_explorer.production_import import apply_release
        save("import-intent",{"at":stamp(),"revision":ctx.rev,"actor_id":6,"fixed_manifest":True,"photos":6,
             "canonical_bytes":421564,"excluded_variations":28,"no_accounts_or_demo_history":True},True)
        result=apply_release(engine,actor_id=6,asset_root=PRIVATEBUNDLE)
        assert result["manufacturerPhotoCount"]==6 and result["variationCount"]==21 and result["canonicalBytes"]==421564 and result["excludedVariations"]==28
        with engine.connect() as c:
            assert fingerprints(c,protected_tables(c))==before
        save("import-applied",{"status":"PASS","at":stamp(),"revision":ctx.rev,"actor_id":6,
             "result":result,"existing_rows_unchanged":True},True)
        print(json.dumps(load("import-applied")),flush=True)
    except Exception as error:
        save("import-stopped",{"at":stamp(),"kind":type(error).__name__,"reconcile_read_only":True},True);raise
    finally:engine.dispose()

def require_bridge(ctx):
    receipt=load("bridge-live")
    assert receipt["status"]=="PASS" and receipt["normal_owner_authenticated"]
    submitted=load("bridge-deployment");current=ctx.live()
    assert current["id"]==submitted["id"]==receipt["id"] and current["commit"]["id"]==ctx.recovery

def in_stabilization(deploy):
    value=deploy.get("finishedAt")
    assert isinstance(value,str),"Missing exact deploy completion time"
    finished=datetime.fromisoformat(value.replace("Z","+00:00"))
    assert finished.tzinfo is not None
    elapsed=(datetime.now(timezone.utc)-finished).total_seconds()
    return -5<=elapsed<=120

def submit(ctx,mode="candidate"):
    assert mode in {"bridge","candidate","recovery"}
    ctx.assert_source();ctx.require_ci()
    candidate=load("candidate");assert candidate["status"]=="PASS"
    baseline=ctx.baseline()
    label="candidate" if mode=="candidate" else "recovery"
    prefix="web" if mode=="candidate" else mode
    revision=ctx.rev if mode=="candidate" else ctx.recovery
    branch=ctx.cfg["publish_branch"] if mode=="candidate" else ctx.cfg["recovery_branch"]
    assert ctx.github("git/ref/heads/"+branch)["object"]["sha"]==revision
    assert not (OUT/(prefix+"-deploy-intent.json")).exists(),"Existing intent requires GET reconciliation"
    assert not any(validate_status(d["status"]) in PENDING for d in ctx.deployments())
    current=ctx.live()
    if mode=="bridge":
        assert current["id"]==ctx.cfg["baseline_deploy"] and current["commit"]["id"]==ctx.base
        assert not (OUT/"migration-intent.json").exists()
        cookie=active_owner_cookie(ctx,minimum_minutes=10)
        observed=json.loads(owner_get(ctx,"/management/deployment/runtime",cookie)[0]);cookie=None
        assert observed["revision"]==ctx.base and observed["schema"]=="20261119_0057" and observed["files_still_match"]
    elif mode=="candidate":
        require_bridge(ctx)
        assert load("migration-applied")["status"]==load("import-applied")["status"]=="PASS"
    else:assert current["commit"]["id"] in {ctx.base,ctx.recovery,ctx.rev}
    ctx.preserve_controls()
    before=ctx.environment()
    approved=[baseline["environment"]]+[json.loads((ctx.private/(name+"-environment.json")).read_text()) for name in ("candidate","recovery")]
    assert before in approved,"Unreviewed environment drift"
    if mode=="bridge":assert before==baseline["environment"]
    if mode=="candidate":assert before==json.loads((ctx.private/"recovery-environment.json").read_text())
    after=json.loads((ctx.private/(label+"-environment.json")).read_text())
    validate_environment(baseline["environment"],after)
    from scripts.release.artifact_preflight import configuration,BUILD_COMMAND
    configuration(after,revision,BUILD_COMMAND,minimum_validity=3600)
    assert ctx.request("postgres/"+DB+"/recovery")["recoveryStatus"]=="AVAILABLE"
    ctx.mutate("services/"+WEB+"/env-vars","PUT",[{"key":k,"value":v} for k,v in after.items()],prefix+"-environment")
    assert ctx.environment()==after
    deploy=ctx.mutate("services/"+WEB+"/deploys","POST",{"commitId":revision,"clearCache":"do_not_clear"},prefix+"-deploy")
    assert deploy["commit"]["id"]==revision
    save(prefix+"-deployment",{"id":deploy["id"],"revision":revision,"at":stamp(),
        "from_revision":current["commit"]["id"],"from_deployment":current["id"]},True)
    print(json.dumps({"status":"SUBMITTED","id":deploy["id"],"revision":revision,"mode":mode}),flush=True)

def watch(ctx,mode="candidate"):
    assert mode in {"bridge","candidate","recovery"}
    label="candidate" if mode=="candidate" else "recovery"
    prefix="web" if mode=="candidate" else mode
    revision=ctx.rev if mode=="candidate" else ctx.recovery
    path=OUT/(prefix+"-deployment.json")
    if not path.exists():
        intent=load(prefix+"-deploy-intent")
        matches=[d for d in ctx.deployments() if d["commit"]["id"]==revision and d["createdAt"]>=intent["at"]]
        assert len(matches)==1,"Uncertain deployment requires unique GET reconciliation"
        save(prefix+"-deployment",{"id":matches[0]["id"],"revision":revision,"at":intent["at"],"reconciled":True},True)
    submitted=load(prefix+"-deployment")
    deploy=ctx.request("services/"+WEB+"/deploys/"+submitted["id"])
    assert deploy["commit"]["id"]==revision
    status=validate_status(deploy["status"])
    report={"at":stamp(),"id":deploy["id"],"revision":revision,"status":status}
    if status=="live":
        assert ctx.live()["id"]==deploy["id"]
        expected=load("candidate")["artifacts"][label]
        service=ctx.request("services/"+WEB)
        query={"ownerId":service["ownerId"],"resource":WEB,"type":"build","startTime":deploy["createdAt"],
               "endTime":stamp(),"limit":100,"direction":"forward"}
        markers=set()
        for _ in range(30):
            page=ctx.request("logs?"+urllib.parse.urlencode(query))
            for row in page["logs"]:
                try:entry=json.loads(row["message"])
                except (ValueError,KeyError,TypeError):continue
                if not isinstance(entry,dict) or entry.get("revision")!=revision:continue
                if entry.get("source_tree_sha256")==expected["source_tree_sha256"]:markers.add("source")
                if entry.get("event")=="ERUPTED_BUILD_WORKSPACE":
                    assert not entry["status"] and not entry["runtime_changes"];markers.add(entry["phase"])
                if entry.get("event")=="ERUPTED_ADVISORY_PROJECTION_VERIFIED":
                    assert entry["sha256"]==expected["artifact"]["projection_sha256"];markers.add("projection")
            if not page["hasMore"]:break
            query.update(startTime=page["nextStartTime"],endTime=page["nextEndTime"])
        else:raise RuntimeError("Build log pagination exceeded bound")
        required={"source","before-dependencies","after-dependencies","identity","projection"}
        if not required<=markers:
            report.update(status="WAIT",reason="Exact build markers not all visible",markers=sorted(markers))
        else:
            ctx.preserve_controls(json.loads((ctx.private/(label+"-environment.json")).read_text()))
            report.update(status="PASS",build_markers=sorted(markers),source_tree_sha256=expected["source_tree_sha256"])
            if mode in {"bridge","recovery"}:
                expected_schema="20261119_0057"
                if mode=="recovery":
                    from sqlalchemy import text
                    engine=ctx.engine(True)
                    try:
                        with engine.connect() as c:
                            assert c.scalar(text("SHOW transaction_read_only"))=="on"
                            expected_schema=c.scalar(text("SELECT version_num FROM public.alembic_version"))
                            assert expected_schema in {"20261119_0057","20261120_0058"}
                    finally:engine.dispose()
                cookie=active_owner_cookie(ctx)
                try:
                    observed=json.loads(owner_get(ctx,"/management/deployment/runtime",cookie)[0])
                    ready=json.loads(owner_get(ctx,"/management/deployment/readiness",cookie)[0])
                    previous=submitted.get("from_revision",ctx.base)
                    if observed.get("revision")==previous and previous!=revision and in_stabilization(deploy):
                        report.update(status="WAIT",reason="Known previous instance during "+mode+" stabilization")
                    else:
                        assert observed["revision"]==revision and observed["source_tree_sha256"]==expected["source_tree_sha256"]
                        assert observed["status"]=="VERIFIED" and observed["files_still_match"]
                        assert observed["schema"]==expected_schema and observed["principal_id"]==6
                        assert ready["status"]=="READY" and all(ready["dependencies"].values()) and ready["web"]["revision"]==revision
                        assert ready["schema"]==expected_schema
                        report["normal_owner_authenticated"]=True;report["schema"]=expected_schema
                except urllib.error.HTTPError as error:
                    if error.code in {429,502,503,504} and in_stabilization(deploy):
                        report.update(status="WAIT",reason=mode+" GET-only stabilization",http=error.code)
                    else:raise
                finally:cookie=None
            if report["status"]=="PASS":save(prefix+"-live",report)
    save(prefix+"-progress",report);print(json.dumps(report),flush=True)
    if status not in PENDING|{"live"}:raise RuntimeError("Exact deployment terminal failure: "+status)

def verify(ctx):
    from sqlalchemy import text
    assert load("web-live")["status"]=="PASS"
    expected=load("candidate")["artifacts"]["candidate"]
    assert ctx.live()["commit"]["id"]==ctx.rev and ctx.live()["id"]==load("web-deployment")["id"]
    ctx.preserve_controls(json.loads((ctx.private/"candidate-environment.json").read_text()))
    cookie=active_owner_cookie(ctx)
    try:
        status=json.loads(owner_get(ctx,"/session-status",cookie)[0])
        assert status["authenticated"] and status["principal_id"]==6
        observed=json.loads(owner_get(ctx,"/management/deployment/runtime",cookie)[0])
        if observed.get("revision")==ctx.recovery and in_stabilization(ctx.request("services/"+WEB+"/deploys/"+load("web-deployment")["id"])):
            report={"status":"WAIT","reason":"Known previous web instance during GET-only stabilization","at":stamp()}
            save("verification-progress",report);print(json.dumps(report),flush=True);return
        assert observed["revision"]==ctx.rev and observed["source_tree_sha256"]==expected["source_tree_sha256"]
        assert observed["status"]=="VERIFIED" and observed["files_still_match"] and observed["schema"]=="20261120_0058"
        ready=json.loads(owner_get(ctx,"/management/deployment/readiness",cookie)[0])
        assert ready["status"]=="READY" and all(ready["dependencies"].values()) and ready["web"]["revision"]==ctx.rev
        bootstrap=json.loads(owner_get(ctx,"/v2/flavor-intelligence/api/bootstrap",cookie)[0])
        assert bootstrap["principal"]["id"]==6 and bootstrap["capabilities"]["manageExplorerPhotos"] and not bootstrap["developmentPreview"]
        admin=json.loads(owner_get(ctx,"/v2/flavor-intelligence/api/products",cookie)[0])
        public=json.loads(owner_get(ctx,"/explorer/api/catalog",cookie)[0])
        assert public["meta"]["staffOnly"] and public["products"] and public["stores"]
        visible={p["id"]:p for p in public["products"]}
        private_bundle()
        manifest=json.loads((PRIVATEBUNDLE/"manifest.json").read_text())
        projected_ids={v["id"] for p in public["products"] for v in p["variations"]}
        assert not projected_ids.intersection(manifest["excludedVariationIds"])
        photos=[]
        for item in manifest["photos"]:
            product=item["product"];asset=item["asset"]
            value=json.loads(owner_get(ctx,"/v2/flavor-intelligence/api/products/"+product["id"]+"/photo",cookie)[0])["photo"]
            assert value["effectiveSourceKind"]=="manufacturer" and value["assetId"]==asset["id"]
            assert value["candidates"]["manufacturer"]["assetId"]==asset["id"] and value["url"].startswith("/flavor-explorer/media/photos/")
            body,headers=owner_get(ctx,value["url"],cookie)
            assert len(body)==asset["byte_size"] and sha(body)==asset["id"] and headers["Content-Type"].startswith("image/webp")
            if product["id"] in visible:assert visible[product["id"]]["imageUrl"]==value["url"]
            photos.append({"product_id":product["id"],"sha256":asset["id"],"bytes":len(body)})
        cp=json.loads(owner_get(ctx,"/v2/compatibility-explorer/api/bootstrap",cookie)[0])
        for path in ("/explorer/","/v2/flavor-intelligence","/v2/compatibility-explorer"):
            body,_=owner_get(ctx,path,cookie);assert body
        raw_denied={}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(NoRedirect())
        for path in ("/explorer/admin.html","/v2-assets/flavor-explorer/admin.html",
                     "/explorer/staff.html","/v2-assets/flavor-explorer/compatibility.html"):
            try:
                with opener.open(urllib.request.Request(ORIGIN+path,headers={"Cookie":cookie}),timeout=30) as r:code=r.status
            except urllib.error.HTTPError as error:code=error.code
            assert code==404;raw_denied[path]=code
        anonymous={}
        for path in ("/explorer/","/explorer/api/catalog","/v2/flavor-intelligence",
                     "/v2/compatibility-explorer","/flavor-explorer/media/photos/"+photos[0]["sha256"]+".webp"):
            try:
                with opener.open(ORIGIN+path,timeout=30) as r:code=r.status
            except urllib.error.HTTPError as error:code=error.code
            assert code in {301,302,303,307,308,401,403};anonymous[path]=code
        engine=ctx.engine(True)
        try:
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
                assert c.scalar(text("SHOW transaction_read_only"))=="on"
                assert c.scalar(text("SELECT version_num FROM public.alembic_version"))=="20261120_0058"
                after=fingerprints(c,protected_tables(c))
                assert after==load("migration-applied")["protected"]
                assert c.scalar(text("SELECT count(*) FROM public.fi_notes"))==20
                assert c.scalar(text("SELECT count(*) FROM public.fi_product_photo_assets"))==6
                assert c.scalar(text("SELECT count(*) FROM public.fi_product_photo_operations"))==6
                assert c.scalar(text("SELECT count(*) FROM public.fi_product_photo_events"))==21
                assert c.scalar(text("SELECT count(*) FROM public.cp_seed_imports"))==1
                from app.services.flavor_explorer.discovery import excluded_variation_ids
                from app.services.flavor_explorer.production_storage import PostgresStorage
                assert excluded_variation_ids(PostgresStorage(engine))==set(manifest["excludedVariationIds"])
                assert c.scalar(text("SELECT count(*) FROM public.touchscreen_flavors"))==0
                assert c.scalar(text("SELECT count(*) FROM public.touchscreen_flavor_categories"))==27
        finally:engine.dispose()
        ctx.preserve_controls(json.loads((ctx.private/"candidate-environment.json").read_text()))
        report={"status":"PASS","at":stamp(),"revision":ctx.rev,"deployment":ctx.live()["id"],
            "source_tree_sha256":expected["source_tree_sha256"],"schema":"20261120_0058",
            "normal_owner_authenticated":True,"runtime_and_readiness_verified":True,
            "manufacturer_primary_photos":photos,"photo_bytes":sum(p["bytes"] for p in photos),
            "admin_products":admin["count"],"visible_products":len(public["products"]),
            "anonymous_denials":anonymous,"raw_template_denials":raw_denied,"existing_business_rows_unchanged":True,
            "protected_controls_unchanged":True,"only_forecast_artifact_binding_changed":sorted(ENV_CHANGES),
            "cron_compatibility_revision":ctx.cron,"cron_scheduling_code_changes":0,"cron_manual_runs":0,
            "cron_controls_restored":True,"natural_cron_run_verified":False,
            "Square_catalog_writes":0,"development_accounts_imported":0}
        save("verification",report,True);print(json.dumps(report),flush=True)
    except urllib.error.HTTPError as error:
        deploy=ctx.request("services/"+WEB+"/deploys/"+load("web-deployment")["id"])
        if error.code in {429,502,503,504} and in_stabilization(deploy):
            report={"status":"WAIT","at":stamp(),"reason":"Candidate GET-only stabilization","http":error.code}
            save("verification-progress",report);print(json.dumps(report),flush=True);return
        raise
    finally:cookie=None

def load_cron_guard(ctx):
    import types
    name="_erupted_exact_cron_schema_guard"
    source=git("show",ctx.cron+":app/schema_contract.py")
    assert sha(source)=="85a1a86996f816100928c69360369df227ea4aa7566abf1c5d79c2b991e36f71"
    module=types.ModuleType(name);module.__file__="<exact-af-parent-cron-schema-contract>"
    sys.modules[name]=module
    exec(compile(source,module.__file__,"exec"),module.__dict__)
    assert module.HEAD_REVISION=="20261119_0057"
    assert module.SUPPORTED_REVISIONS=={"20261119_0057","20261120_0058"}
    return module

def qualified_cron_role(ctx):
    proof=load("cron-compatibility");before=ctx.baseline()["roles"]["staff"];after=proof["after"]
    assert proof["status"]=="PASS" and proof["revision"]==ctx.cron and proof["parent"]==CRON_BASELINE
    assert proof["before"]==before and before["revision"]==CRON_BASELINE
    assert set(before)==set(after)
    assert {key for key in before if before[key]!=after[key]}=={"deployment","revision"}
    assert after["revision"]==ctx.cron and after["deployment"]==load("cron-deployment")["id"]
    assert proof["source_tree_sha256"]==load("candidate")["artifacts"]["cron"]["source_tree_sha256"]
    assert proof["schema_guard"]==load("rehearsal")["cron_guard"]
    assert proof["schema_guard"]["accepted"]==["20261119_0057","20261120_0058"]
    assert proof["schema_guard"]["future_0059_rejected"] and not proof["schema_guard"]["scheduling_job_invoked"]
    assert proof["controls_restored"] and not proof["manual_run_triggered"]
    return after

def require_cron_compatibility(ctx):
    after=qualified_cron_role(ctx)
    assert ctx.role(ROLES["staff"])==after
    assert ctx.github("git/ref/heads/"+CRON_ORIGINAL_BRANCH)["object"]["sha"]==ctx.baseline()["cron_original_branch_tip"]

def cron_controls(ctx,branch):
    assert branch in {CRON_ORIGINAL_BRANCH,ctx.cfg["cron_branch"]}
    baseline=ctx.baseline();before=baseline["roles"]["staff"];observed=ctx.role(ROLES["staff"])
    expected=dict(before,service=dict(before["service"],branch=branch),
                  deployment=observed["deployment"],revision=observed["revision"])
    assert observed==expected,"Unexpected staff cron control/environment drift"
    assert observed["revision"] in {CRON_BASELINE,ctx.cron}
    if observed["revision"]==CRON_BASELINE:assert observed["deployment"]==before["deployment"]
    else:assert observed["deployment"]==load("cron-deployment")["id"]
    assert observed["service"]["autoDeployTrigger"]=="off"
    assert observed["service"]["serviceDetails"]["schedule"]=="0 10 * * 0"
    assert ctx.role(ROLES["collector"])==baseline["roles"]["collector"]
    assert ctx.secret_files()==tuple(baseline["secret_files"])
    assert ctx.env_groups()==tuple(baseline["env_groups"])
    assert protected_service(ctx.request("services/"+WEB))==baseline["protected_service"]
    assert ctx.environment()==json.loads((ctx.private/"recovery-environment.json").read_text())
    assert ctx.github("git/ref/heads/"+CRON_ORIGINAL_BRANCH)["object"]["sha"]==baseline["cron_original_branch_tip"]
    return observed

def cron_bind(ctx):
    ctx.assert_source();ctx.require_ci();require_bridge(ctx)
    assert ctx.cfg["compatibility_amendment_authorized"] is True
    assert not (OUT/"migration-intent.json").exists()
    assert load("rehearsal")["status"]=="PASS"
    guard=load("rehearsal")["cron_guard"]
    assert guard["revision"]==ctx.cron and guard["parent"]==CRON_BASELINE
    assert guard["accepted"]==["20261119_0057","20261120_0058"] and guard["future_0059_rejected"]
    assert not guard["scheduling_job_invoked"]
    assert ctx.github("git/ref/heads/"+ctx.cfg["cron_branch"])["object"]["sha"]==ctx.cron
    assert not any(validate_status(d["status"]) in PENDING for d in ctx.deployments(ROLES["staff"]))
    if (OUT/"cron-bind-intent.json").exists():
        intent=load("cron-bind-intent")
        assert intent["method"]=="PATCH" and intent["path"]=="services/"+ROLES["staff"]
        assert intent["body_sha256"]==sha(canonical({"branch":ctx.cfg["cron_branch"]}))
        observed=cron_controls(ctx,ctx.cfg["cron_branch"])
        assert observed["revision"]==CRON_BASELINE,"Binding reconciliation cannot accept an unreviewed live artifact"
        report={"status":"PASS","at":stamp(),"revision":ctx.cron,"branch":ctx.cfg["cron_branch"],
                "original_branch":CRON_ORIGINAL_BRANCH,"manual_run_triggered":False,"reconciled_get_only":True}
        if not (OUT/"cron-branch-bound.json").exists():save("cron-branch-bound",report,True)
        print(json.dumps(load("cron-branch-bound")),flush=True);return
    ctx.preserve_controls(json.loads((ctx.private/"recovery-environment.json").read_text()))
    before=cron_controls(ctx,CRON_ORIGINAL_BRANCH)
    assert before["revision"]==CRON_BASELINE
    result=ctx.mutate("services/"+ROLES["staff"],"PATCH",{"branch":ctx.cfg["cron_branch"]},"cron-bind")
    assert result["id"]==ROLES["staff"] and result["branch"]==ctx.cfg["cron_branch"]
    cron_controls(ctx,ctx.cfg["cron_branch"])
    save("cron-branch-bound",{"status":"PASS","at":stamp(),"revision":ctx.cron,"branch":ctx.cfg["cron_branch"],
         "original_branch":CRON_ORIGINAL_BRANCH,"manual_run_triggered":False},True)
    print(json.dumps(load("cron-branch-bound")),flush=True)

def cron_deploy(ctx):
    ctx.assert_source();ctx.require_ci();require_bridge(ctx)
    assert load("cron-branch-bound")["status"]=="PASS"
    assert not (OUT/"cron-deploy-intent.json").exists(),"Prior cron build needs GET reconciliation"
    before=cron_controls(ctx,ctx.cfg["cron_branch"]);assert before["revision"]==CRON_BASELINE
    assert ctx.github("git/ref/heads/"+ctx.cfg["cron_branch"])["object"]["sha"]==ctx.cron
    assert not any(validate_status(d["status"]) in PENDING for d in ctx.deployments(ROLES["staff"]))
    deploy=ctx.mutate("services/"+ROLES["staff"]+"/deploys","POST",{"clearCache":"do_not_clear"},"cron-deploy")
    assert deploy["commit"]["id"]==ctx.cron
    save("cron-deployment",{"id":deploy["id"],"revision":ctx.cron,"at":stamp(),
        "manual_run_triggered":False,"schedule":"0 10 * * 0"},True)
    print(json.dumps({"status":"SUBMITTED","id":deploy["id"],"revision":ctx.cron,"manual_run_triggered":False}),flush=True)

def watch_cron(ctx):
    path=OUT/"cron-deployment.json"
    if not path.exists():
        intent=load("cron-deploy-intent")
        matches=[d for d in ctx.deployments(ROLES["staff"])
                 if d["commit"]["id"]==ctx.cron and d["createdAt"]>=intent["at"]]
        assert len(matches)==1,"Uncertain cron build needs unique GET reconciliation"
        save("cron-deployment",{"id":matches[0]["id"],"revision":ctx.cron,"at":intent["at"],
             "manual_run_triggered":False,"reconciled":True},True)
    submitted=load("cron-deployment")
    deploy=ctx.request("services/"+ROLES["staff"]+"/deploys/"+submitted["id"])
    assert deploy["commit"]["id"]==ctx.cron
    status=validate_status(deploy["status"])
    report={"at":stamp(),"id":deploy["id"],"revision":ctx.cron,"status":status,
            "manual_run_triggered":False,"natural_run_verified":False}
    if status=="live":
        assert ctx.live(ROLES["staff"])["id"]==deploy["id"]
        cron_controls(ctx,ctx.cfg["cron_branch"])
        expected=load("candidate")["artifacts"]["cron"]
        service=ctx.request("services/"+ROLES["staff"])
        query={"ownerId":service["ownerId"],"resource":ROLES["staff"],"type":"build","startTime":deploy["createdAt"],
               "endTime":stamp(),"limit":100,"direction":"forward"}
        markers=set()
        for _ in range(30):
            page=ctx.request("logs?"+urllib.parse.urlencode(query))
            for row in page["logs"]:
                try:entry=json.loads(row["message"])
                except (ValueError,KeyError,TypeError):continue
                if not isinstance(entry,dict) or entry.get("revision")!=ctx.cron:continue
                if entry.get("source_tree_sha256")==expected["source_tree_sha256"]:markers.add("source")
                if entry.get("event")=="ERUPTED_BUILD_WORKSPACE":
                    assert not entry["status"] and not entry["runtime_changes"];markers.add(entry["phase"])
            if not page["hasMore"]:break
            query.update(startTime=page["nextStartTime"],endTime=page["nextEndTime"])
        else:raise RuntimeError("Cron build log pagination exceeded bound")
        if {"source","before-dependencies","after-dependencies","identity"}<=markers:
            report.update(status="PASS",source_tree_sha256=expected["source_tree_sha256"],
                          schema_guard=load("rehearsal")["cron_guard"],markers=sorted(markers))
            assert report["schema_guard"]["revision"]==ctx.cron
            save("cron-live",report)
        else:report.update(status="WAIT",reason="Exact cron build markers not all visible",markers=sorted(markers))
    save("cron-progress",report);print(json.dumps(report),flush=True)
    if status not in PENDING|{"live"}:raise RuntimeError("Cron build terminal failure; restore original branch read-only reconciled: "+status)

def restore_cron_branch(ctx):
    ctx.assert_source()
    # Restore only the recorded branch; never schedule, run, cancel or downgrade.
    assert not any(validate_status(d["status"]) in PENDING for d in ctx.deployments(ROLES["staff"]))
    reconciled=(OUT/"cron-restore-intent.json").exists()
    if reconciled:
        intent=load("cron-restore-intent")
        assert intent["method"]=="PATCH" and intent["path"]=="services/"+ROLES["staff"]
        assert intent["body_sha256"]==sha(canonical({"branch":CRON_ORIGINAL_BRANCH}))
        current=cron_controls(ctx,CRON_ORIGINAL_BRANCH)
        if (OUT/"cron-compatibility.json").exists():
            assert qualified_cron_role(ctx)==current
            print(json.dumps(load("cron-compatibility")),flush=True);return
        if (OUT/"cron-branch-restored-without-compatibility.json").exists():
            assert current["revision"]==CRON_BASELINE
            print(json.dumps(load("cron-branch-restored-without-compatibility")),flush=True);return
    else:current=cron_controls(ctx,ctx.cfg["cron_branch"])
    compatible=current["revision"]==ctx.cron
    if compatible:
        proof=load("cron-live")
        assert proof["status"]=="PASS" and proof["id"]==current["deployment"]
    if not reconciled:
        result=ctx.mutate("services/"+ROLES["staff"],"PATCH",{"branch":CRON_ORIGINAL_BRANCH},"cron-restore")
        assert result["id"]==ROLES["staff"] and result["branch"]==CRON_ORIGINAL_BRANCH
    after=cron_controls(ctx,CRON_ORIGINAL_BRANCH)
    if compatible:
        report={"status":"PASS","at":stamp(),"revision":ctx.cron,"parent":CRON_BASELINE,
                "before":ctx.baseline()["roles"]["staff"],"after":after,
                "source_tree_sha256":load("cron-live")["source_tree_sha256"],
                "schema_guard":load("rehearsal")["cron_guard"],"controls_restored":True,
                "manual_run_triggered":False,"natural_run_verified":False}
        save("cron-compatibility",report,True)
        assert qualified_cron_role(ctx)==after
    else:
        report={"status":"HELD","at":stamp(),"reason":"Original branch restored; compatibility artifact not active",
                "controls_restored":True,"manual_run_triggered":False}
        save("cron-branch-restored-without-compatibility",report,True)
    print(json.dumps(report),flush=True)

def main():
    assert len(sys.argv)==2 and sys.argv[1] in {"qualify","publish","dispatch","ci","preflight","rehearse-worker",
        "prepare","refresh-preflight","migrate","import","bridge","watch-bridge","deploy","watch","verify","recover","watch-recovery","cron-bind","cron-deploy","watch-cron","restore-cron-branch"}
    action=sys.argv[1]
    native_control_check(qualifying=action=="qualify")
    ctx=Context()
    if action=="qualify":qualify(ctx);return
    commands={"publish":publish,"dispatch":dispatch,"ci":ci,"preflight":preflight,"rehearse-worker":rehearsal,
        "prepare":prepare,"refresh-preflight":refresh_preflight,"migrate":migrate,"import":import_release,"bridge":lambda c:submit(c,"bridge"),
        "watch-bridge":lambda c:watch(c,"bridge"),"deploy":lambda c:submit(c,"candidate"),
        "watch":lambda c:watch(c,"candidate"),"verify":verify,"recover":lambda c:submit(c,"recovery"),
        "watch-recovery":lambda c:watch(c,"recovery"),"cron-bind":cron_bind,"cron-deploy":cron_deploy,
        "watch-cron":watch_cron,"restore-cron-branch":restore_cron_branch}
    commands[action](ctx)

if __name__=="__main__":main()
