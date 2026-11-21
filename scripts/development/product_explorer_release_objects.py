"""Compose exact, owner-approved support artifacts in the registered Git object store.

The working tree, index, branch refs and hooks are never altered. Both artifacts
reuse pinned existing trees and replace only the reviewed compatibility blobs.
Run only through the installed qualified Product Explorer development launcher.
"""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,os,re,subprocess,sys

SID="AVA-ERUPTED-PRODUCT-RELEASE-20261120"
ROOT=Path("/Volumes/AvaOne-Development-Retained/sessions")/SID
SOURCE=ROOT/"source"
OUT=ROOT/"persistent/product-explorer-release-20261120-r6"
PRIOR_OBJECTS=ROOT/"persistent/product-explorer-release-20261120/artifact-objects.json"
PRIOR_CANDIDATE="b81da200afc3afd52fb4c74af87286ceefdf0bff"
PINNED_RECOVERY="094c95b123cf6609b69e733c2da193d722028ff8"
PINNED_CRON="f929c22d6ac5d56a33523dbf565cc84c0695f1c7"
BASE="d19e7d2abdcd7b6dc0edd538e93f49a247a48a07"
CRON_BASE="af00e6dee9738b22afeaaed9951d029be1b21cbc"
BRANCH="codex/ava-erupted-product-release-20261120"
TWO_GATES={
 "app/purchasing_v4/production_target.py":("d7a67fd2755e818b83dd604208c9c122b2ecaaa40167bd7881f52501eaf58fe1",
                                          "9cedc1e25207374d375482aa84768ce7719a9abcfe73a62a357258fe652dd166"),
 "app/services/daily_evidence_migration.py":("2d6d639ffe2513c24ece947975cd74c397755636b5a5ac7879965e584116e0c2",
                                          "ac9a1d55d6ddbbc41f6391979831aca778188c3f70fff59bb58956fc8f87e831"),
}
SCHEMA="app/schema_contract.py"
TEST="tests/test_scheduling_0057_target_compatibility.py"
SCHEMA_AFTER_SHA="85a1a86996f816100928c69360369df227ea4aa7566abf1c5d79c2b991e36f71"

def sha(data):return hashlib.sha256(data).hexdigest()

def git(*args,input=None,env=None):
    process_env=dict(os.environ,GIT_OPTIONAL_LOCKS="0")
    process_env.update(env or {})
    return subprocess.check_output(["git","-C",str(SOURCE),*args],input=input,env=process_env)

def replace_tree(tree,edits,runner=git):
    """Replace explicit existing paths; preserve every other entry and mode."""
    if not edits:return tree
    children={}
    for name,data in edits.items():
        parts=name.split("/")
        assert all(parts) and all(part not in {".","..",".git"} for part in parts)
        head,*tail=parts
        children.setdefault(head,{})["/".join(tail)]=data
    entries=[]
    for raw in runner("ls-tree","-z",tree).split(b"\0"):
        if not raw:continue
        identity,name=raw.split(b"\t",1);mode,kind,oid=identity.decode().split()
        name=name.decode()
        if name in children:
            selected=children.pop(name)
            if "" in selected:
                assert len(selected)==1 and kind=="blob" and mode in {"100644","100755"}
                oid=runner("hash-object","-w","--stdin",input=selected[""]).decode().strip()
            else:
                assert kind=="tree" and mode=="040000"
                oid=replace_tree(oid,selected,runner)
        entries.append((mode+" "+kind+" "+oid+"\t"+name).encode()+b"\0")
    assert not children,"Compatibility edits must replace existing exact paths"
    return runner("mktree","-z",input=b"".join(entries)).decode().strip()

def schema_bridge(parent):
    raw=git("show",parent+":"+SCHEMA)
    old=b"SUPPORTED_REVISIONS = frozenset({HEAD_REVISION})"
    new=b"SUPPORTED_REVISIONS = frozenset({HEAD_REVISION, '20261120_0058'})"
    assert raw.count(old)==1 and b"HEAD_REVISION = '20261119_0057'" in raw
    result=raw.replace(old,new)
    assert sha(result)==SCHEMA_AFTER_SHA
    return result

def metadata(candidate):
    result={}
    for kind,keys in (("AUTHOR",("an","ae")),("COMMITTER",("cn","ce"))):
        name,email=(git("show","-s","--format=%"+key,candidate).decode().strip() for key in keys)
        assert name and email and "\n" not in name and "\n" not in email
        result["GIT_"+kind+"_NAME"]=name;result["GIT_"+kind+"_EMAIL"]=email
    epoch=git("show","-s","--format=%ct",candidate).decode().strip()
    assert epoch.isdigit()
    result.update(GIT_AUTHOR_DATE=epoch+" +0000",GIT_COMMITTER_DATE=epoch+" +0000")
    return result

def validate_commit(revision,parent,edits):
    assert re.fullmatch("[a-f0-9]{40}",revision)
    assert git("rev-parse",revision+"^").decode().strip()==parent
    paths=git("diff","--name-only",parent,revision).decode().splitlines()
    assert paths==sorted(edits)
    for path,data in edits.items():assert git("show",revision+":"+path)==data

def support_commit(parent,edits,message,environment):
    tree=replace_tree(git("rev-parse",parent+"^{tree}").decode().strip(),edits)
    revision=git("-c","commit.gpgsign=false","commit-tree",tree,"-p",parent,
                 input=(message+"\n").encode(),env=environment).decode().strip()
    validate_commit(revision,parent,edits)
    return revision

def compose():
    assert __debug__ and Path.cwd().resolve()==SOURCE
    from scripts.development.product_explorer_release_transport import e
    e.qualified();e.base.gate()
    assert os.environ.get("AVA_BOUNDED_SESSION")==SID
    assert git("branch","--show-current").decode().strip()==BRANCH
    candidate=git("rev-parse","HEAD").decode().strip()
    assert candidate!=BASE and git("rev-parse","HEAD^").decode().strip()==BASE
    assert not git("status","--porcelain","--untracked-files=all").strip()
    assert not git("ls-files","--","app/services/flavor_explorer/release_assets",
                   "app/services/flavor_explorer/photo-pilot-sources.json",
                   "app/services/flavor_explorer/photo_pilot.py").strip()
    before_index=sha((SOURCE/".git/index").read_bytes())
    before_head=git("symbolic-ref","HEAD")
    recovery={SCHEMA:schema_bridge(BASE)}
    for path,(old,new) in TWO_GATES.items():
        assert sha(git("show",BASE+":"+path))==old
        content=git("show",candidate+":"+path);assert sha(content)==new
        recovery[path]=content
    # This qualified test follows the artifact's actual HEAD and covers both
    # accepted revisions plus rejection of the unreviewed next revision.
    test=git("show",candidate+":"+TEST)
    assert b"20261119_0057" in test and b"20261120_0058" in test and b"20261121_0059" in test
    assert b"HEAD_REVISION" in test
    recovery[TEST]=test
    cron={SCHEMA:schema_bridge(CRON_BASE)}
    output=OUT/"artifact-objects.json"
    if output.exists():
        report=json.loads(output.read_text());assert report["candidate"]==candidate
        validate_commit(report["recovery"],BASE,recovery)
        validate_commit(report["cron"],CRON_BASE,cron)
    else:
        prior=json.loads(PRIOR_OBJECTS.read_text())
        assert prior["status"]=="PASS" and prior["candidate"]==PRIOR_CANDIDATE
        assert prior["recovery"]==PINNED_RECOVERY and prior["cron"]==PINNED_CRON
        validate_commit(PINNED_RECOVERY,BASE,recovery)
        validate_commit(PINNED_CRON,CRON_BASE,cron)
        recovery_sha=PINNED_RECOVERY;cron_sha=PINNED_CRON
        report={"status":"PASS","at":datetime.now(timezone.utc).isoformat(),"candidate":candidate,
                "support_reused_from_candidate":PRIOR_CANDIDATE,"prior_receipt_sha256":sha(PRIOR_OBJECTS.read_bytes()),
                "candidate_parent":BASE,"recovery":recovery_sha,"recovery_parent":BASE,
                "recovery_paths":sorted(recovery),"recovery_file_sha256":{k:sha(v) for k,v in recovery.items()},
                "cron":cron_sha,"cron_parent":CRON_BASE,"cron_paths":[SCHEMA],
                "cron_file_sha256":SCHEMA_AFTER_SHA,"working_tree_index_refs_unchanged":True}
        with output.open("x") as f:json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
    assert git("symbolic-ref","HEAD")==before_head and git("rev-parse","HEAD").decode().strip()==candidate
    assert sha((SOURCE/".git/index").read_bytes())==before_index
    assert not git("status","--porcelain","--untracked-files=all").strip()
    print(json.dumps(report),flush=True)

if __name__=="__main__":
    assert len(sys.argv)==1
    compose()
