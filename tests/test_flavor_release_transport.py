"""Finite release transport boundaries; no provider access or credentials."""
from datetime import datetime
import pytest
from scripts.development.product_explorer_release_worker import (
    ENV_CHANGES,commit_payload,protected_service,validate_environment,validate_status,quoted)

def test_artifact_binding_changes_only_two_exact_values():
    before={"FORECAST_ADVISORY_ASSET_SPEC":"old","FORECAST_PRODUCT_ARTIFACT_URL":"old","SQUARE_ACCESS_TOKEN":"retained",
            "FORECAST_ADVISORY_INSTALLER_B64":"retained","BUILD_COMMAND":"retained"}
    after=dict(before,FORECAST_ADVISORY_ASSET_SPEC="new",FORECAST_PRODUCT_ARTIFACT_URL="new")
    assert validate_environment(before,after)==ENV_CHANGES
    for invalid in (dict(after,SQUARE_ACCESS_TOKEN="changed"),dict(after,EXTRA="bad"),
                    {k:v for k,v in after.items() if k!="BUILD_COMMAND"},
                    dict(after,FORECAST_PRODUCT_ARTIFACT_URL="old")):
        with pytest.raises(AssertionError):validate_environment(before,invalid)

def test_provider_progress_and_unknown_terminal_status_are_distinct():
    for value in ("created","queued","build_in_progress","pre_deploy_in_progress","update_in_progress",
                  "live","build_failed","pre_deploy_failed","update_failed","canceled","deactivated"):
        assert validate_status(value)==value
    for value in ("done","success","running","unknown",None):
        with pytest.raises(ValueError):validate_status(value)

def test_exact_unsigned_commit_identity_and_message_are_retained():
    revision=("tree "+"a"*40+"\nparent "+"b"*40+
              "\nauthor Release Owner <owner@example.invalid> 1700000000 +0000"+
              "\ncommitter Release Owner <owner@example.invalid> 1700000000 +0000\n\nExact release message\n").encode()
    value=commit_payload(revision)
    assert value["tree"]=="a"*40 and value["parents"]==["b"*40]
    assert value["message"]=="Exact release message\n"
    assert value["author"]["date"]==value["committer"]["date"]=="2023-11-14T22:13:20+00:00"
    assert value["author"]["email"]=="owner@example.invalid"
    with pytest.raises(ValueError):commit_payload(revision.replace(b"\n\n",b"\ngpgsig unsupported\n\n",1))
    with pytest.raises(AssertionError):commit_payload(revision.replace(b"\nparent "+b"b"*40,b""))

def test_protected_config_ignores_only_provider_record_timestamps():
    service={"id":"fixed","branch":"main","autoDeployTrigger":"off","serviceDetails":{"startCommand":"fixed"},
             "createdAt":"old","updatedAt":"old","dashboardUrl":"display"}
    protected=protected_service(service)
    assert protected=={"id":"fixed","branch":"main","autoDeployTrigger":"off","serviceDetails":{"startCommand":"fixed"}}
    assert protected_service(dict(service,updatedAt="later"))==protected
    assert protected_service(dict(service,branch="drift"))!=protected
    assert quoted('table"name')=='"table""name"'
    with pytest.raises(AssertionError):quoted("bad\x00name")

def test_bridge_identity_is_required_before_migration(monkeypatch):
    from types import SimpleNamespace
    from scripts.development import product_explorer_release_worker as worker
    receipts={"bridge-live":{"status":"PASS","normal_owner_authenticated":True,"id":"dep-fixed"},
              "bridge-deployment":{"id":"dep-fixed"}}
    monkeypatch.setattr(worker,"load",lambda name:receipts[name])
    current={"id":"dep-fixed","commit":{"id":"a"*40}}
    context=SimpleNamespace(recovery="a"*40,live=lambda:current)
    worker.require_bridge(context)
    current["id"]="dep-other"
    with pytest.raises(AssertionError):worker.require_bridge(context)
    current["id"]="dep-fixed";current["commit"]["id"]="b"*40
    with pytest.raises(AssertionError):worker.require_bridge(context)
    current["commit"]["id"]="a"*40;receipts["bridge-live"]["normal_owner_authenticated"]=False
    with pytest.raises(AssertionError):worker.require_bridge(context)

def test_only_fixed_cron_branch_and_build_mutations_are_admitted(monkeypatch,tmp_path):
    import json
    from scripts.development import product_explorer_release_worker as worker
    monkeypatch.setattr(worker,"OUT",tmp_path)
    context=worker.Context.__new__(worker.Context)
    context.cfg={"cron_branch":"release/product-explorer-cron-compat-20261120"}
    calls=[]
    context.request=lambda path,method="GET",body=None:(calls.append((path,method,json.loads(body))) or {"id":"fixed"})
    cron=worker.ROLES["staff"]
    for path,method,body in (
        ("services/"+cron+"/runs","POST",{}),
        ("services/"+cron,"PATCH",{"branch":context.cfg["cron_branch"],"autoDeployTrigger":"commit"}),
        ("services/"+cron,"PATCH",{"branch":"main"}),
        ("services/"+cron+"/deploys","POST",{"commitId":"a"*40}),
        ("services/"+worker.WEB,"PATCH",{"branch":"main"}),
    ):
        with pytest.raises(AssertionError):context.mutate(path,method,body,"denied")
    assert calls==[] and not list(tmp_path.iterdir())
    context.mutate("services/"+cron,"PATCH",{"branch":context.cfg["cron_branch"]},"bound")
    assert calls==[("services/"+cron,"PATCH",{"branch":context.cfg["cron_branch"]})]

def test_cron_compatibility_proof_preserves_every_control(monkeypatch):
    from types import SimpleNamespace
    from scripts.development import product_explorer_release_worker as worker
    before={"service":{"branch":worker.CRON_ORIGINAL_BRANCH,"schedule":"0 10 * * 0","commands":"fixed"},
            "deployment":"dep-before","revision":worker.CRON_BASELINE,"environment_sha256":"fixed",
            "secret_files":["hash",0],"env_groups":["hash",0]}
    after={**before,"deployment":"dep-after","revision":"c"*40}
    guard={"revision":"c"*40,"parent":worker.CRON_BASELINE,"accepted":["20261119_0057","20261120_0058"],
           "future_0059_rejected":True,"scheduling_job_invoked":False}
    proof={"status":"PASS","revision":"c"*40,"parent":worker.CRON_BASELINE,"before":before,"after":after,
           "source_tree_sha256":"digest","schema_guard":guard,"controls_restored":True,"manual_run_triggered":False}
    receipts={"cron-compatibility":proof,"cron-deployment":{"id":"dep-after"},
              "candidate":{"artifacts":{"cron":{"source_tree_sha256":"digest"}}},"rehearsal":{"cron_guard":guard}}
    monkeypatch.setattr(worker,"load",lambda name:receipts[name])
    context=SimpleNamespace(cron="c"*40,baseline=lambda:{"roles":{"staff":before}})
    assert worker.qualified_cron_role(context)==after
    proof["after"]={**after,"service":{**after["service"],"schedule":"* * * * *"}}
    with pytest.raises(AssertionError):worker.qualified_cron_role(context)
    proof["after"]=after;proof["manual_run_triggered"]=True
    with pytest.raises(AssertionError):worker.qualified_cron_role(context)

def test_ambiguous_object_publication_reconciles_with_get_only(monkeypatch,tmp_path):
    import json
    from scripts.development import product_explorer_release_worker as worker
    monkeypatch.setattr(worker,"OUT",tmp_path)
    body={"content":"c3ludGhldGlj","encoding":"base64"}
    worker.save("blob-intent",{"at":"fixture","path":"git/blobs","body_sha256":worker.sha(worker.canonical(body))},True)
    context=worker.Context.__new__(worker.Context)
    calls=[]
    context.github=lambda path,method="GET",body=None:(calls.append((path,method)) or {"sha":"d"*40})
    result=context.gh_mutate("git/blobs",body,"blob",expected_sha="d"*40)
    assert result["sha"]=="d"*40 and calls==[("git/blobs/"+"d"*40,"GET")]
    assert json.loads((tmp_path/"blob-reconciled.json").read_text())["read_only"]
    with pytest.raises(AssertionError):
        context.gh_mutate("git/blobs",{"content":"Y2hhbmdlZA==","encoding":"base64"},"blob",expected_sha="d"*40)
    assert len(calls)==1

def test_github_credentials_do_not_follow_redirects(monkeypatch):
    import io,urllib.request
    from scripts.development import product_explorer_release_worker as worker
    context=worker.Context.__new__(worker.Context)
    seen=[]
    class Response(io.BytesIO):
        pass
    class Opener:
        def open(self,request,timeout):
            seen.append((request.full_url,request.get_header("Authorization")))
            return Response(b'{"sha":"fixture"}')
    def build(handler):
        with pytest.raises(ValueError,match="redirect prohibited"):handler.redirect_request(None,None,302,"",{},"https://other.invalid")
        return Opener()
    monkeypatch.setenv("GH_TOKEN","SYNTHETIC_TEST_ONLY")
    monkeypatch.setattr(urllib.request,"build_opener",build)
    assert context.github("git/commits/"+"a"*40)["sha"]=="fixture"
    assert seen==[("https://api.github.com/repos/"+worker.REPO+"/git/commits/"+"a"*40,"Bearer SYNTHETIC_TEST_ONLY")]

def test_private_release_bundle_has_no_implicit_public_fallback(monkeypatch,tmp_path):
    from scripts.development import product_explorer_release_worker as worker
    monkeypatch.setattr(worker,"PRIVATEBUNDLE",tmp_path)
    (tmp_path/"manifest.json").write_text("{}")
    with pytest.raises(AssertionError):worker.private_bundle()
    (tmp_path/"manifest.json").unlink()
    with pytest.raises(AssertionError):worker.private_bundle()

def test_ambiguous_cron_branch_patch_reconstructs_receipt_without_resubmit(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from scripts.development import product_explorer_release_worker as worker
    monkeypatch.setattr(worker,"OUT",tmp_path)
    branch="release/product-explorer-cron-compat-20261120"
    guard={"revision":"c"*40,"parent":worker.CRON_BASELINE,"accepted":["20261119_0057","20261120_0058"],
           "future_0059_rejected":True,"scheduling_job_invoked":False}
    worker.save("rehearsal",{"status":"PASS","cron_guard":guard})
    worker.save("cron-bind-intent",{"method":"PATCH","path":"services/"+worker.ROLES["staff"],
                "body_sha256":worker.sha(worker.canonical({"branch":branch}))})
    monkeypatch.setattr(worker,"require_bridge",lambda ctx:None)
    observed=[]
    monkeypatch.setattr(worker,"cron_controls",lambda ctx,value:(observed.append(value) or {"revision":worker.CRON_BASELINE}))
    def forbidden(*args,**kwargs):raise AssertionError("Existing PATCH must only be reconciled with GET")
    context=SimpleNamespace(cfg={"cron_branch":branch,"compatibility_amendment_authorized":True},cron="c"*40,
        assert_source=lambda:None,require_ci=lambda:None,github=lambda path:{"object":{"sha":"c"*40}},
        deployments=lambda sid:[],mutate=forbidden,preserve_controls=forbidden)
    worker.cron_bind(context)
    assert observed==[branch]
    assert worker.load("cron-branch-bound")["reconciled_get_only"]

def test_ambiguous_cron_restore_patch_reconstructs_compatibility_without_resubmit(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from scripts.development import product_explorer_release_worker as worker
    monkeypatch.setattr(worker,"OUT",tmp_path)
    worker.save("cron-restore-intent",{"method":"PATCH","path":"services/"+worker.ROLES["staff"],
                "body_sha256":worker.sha(worker.canonical({"branch":worker.CRON_ORIGINAL_BRANCH}))})
    worker.save("cron-live",{"status":"PASS","id":"dep-after","source_tree_sha256":"digest"})
    worker.save("rehearsal",{"cron_guard":{"scheduling_job_invoked":False}})
    before={"revision":worker.CRON_BASELINE,"deployment":"dep-before"}
    after={"revision":"c"*40,"deployment":"dep-after"}
    observed=[]
    def controls(ctx,branch):
        assert branch==worker.CRON_ORIGINAL_BRANCH
        observed.append(branch);return after
    monkeypatch.setattr(worker,"cron_controls",controls)
    monkeypatch.setattr(worker,"qualified_cron_role",lambda ctx:after)
    def forbidden(*args,**kwargs):raise AssertionError("Existing PATCH must only be reconciled with GET")
    context=SimpleNamespace(assert_source=lambda:None,deployments=lambda sid:[],cron="c"*40,
        cfg={"cron_branch":"release/product-explorer-cron-compat-20261120"},mutate=forbidden,
        baseline=lambda:{"roles":{"staff":before}})
    worker.restore_cron_branch(context)
    assert observed==[worker.CRON_ORIGINAL_BRANCH,worker.CRON_ORIGINAL_BRANCH]
    assert worker.load("cron-compatibility")["status"]=="PASS"

@pytest.mark.parametrize("qualifying,expected",[(False,"control-check"),(True,"control-check-qualify")])
def test_app_worker_uses_native_inherited_control_runtime(monkeypatch,tmp_path,qualifying,expected):
    import json,shutil
    from pathlib import Path
    from scripts.development import product_explorer_release_worker as worker
    native=shutil.which("python3.12")
    assert native,"The supported local/Ubuntu24.04 fixture needs its native Python3.12"
    monkeypatch.setattr(worker,"NATIVE_CONTROL",Path(native))
    monkeypatch.setattr(worker,"OUT",tmp_path)
    (tmp_path/"release_transport.py").write_text(
        "from pathlib import Path\nimport hashlib,io,json,sys\n"
        "assert sys.version_info[:2]==(3,12)\n"
        "digest=hashlib.file_digest(io.BytesIO(b'synthetic control'),'sha256').hexdigest()\n"
        "assert sys.argv[1] in {'control-check','control-check-qualify'}\n"
        "(Path(__file__).parent/'observed.json').write_text(json.dumps({'mode':sys.argv[1],'digest':digest}))\n")
    worker.native_control_check(qualifying=qualifying)
    observed=json.loads((tmp_path/"observed.json").read_text())
    assert observed["mode"]==expected
    assert observed["digest"]==worker.sha(b"synthetic control")

def test_native_control_failure_stops_before_context_or_application_work(monkeypatch,tmp_path):
    import shutil,subprocess
    from pathlib import Path
    from scripts.development import product_explorer_release_worker as worker
    native=shutil.which("python3.12");assert native
    monkeypatch.setattr(worker,"NATIVE_CONTROL",Path(native));monkeypatch.setattr(worker,"OUT",tmp_path)
    (tmp_path/"release_transport.py").write_text("raise SystemExit(23)\n")
    monkeypatch.setattr(worker.sys,"argv",["release_worker.py","ci"])
    def forbidden(*args,**kwargs):raise AssertionError("Application work started before native control passed")
    monkeypatch.setattr(worker,"Context",forbidden);monkeypatch.setattr(worker,"ci",forbidden)
    with pytest.raises(subprocess.CalledProcessError) as error:worker.main()
    assert error.value.returncode==23
