"""Exact owner-authorized Erupted scope qualification, November 19, 2026."""
import ast, ctypes, hashlib, importlib.util, json, os, pathlib, subprocess, sys, time

B = pathlib.Path('/Users/justinrawlinson/Library/Application Support/Ava One Development Lifecycle/storage-controls/bounded-development')
ROOT = pathlib.Path('/Volumes/AvaOne-Development-Retained/sessions/AVA-ERUPTED-SWAP-OWNER-20261119')
EXPECTED = '294c5a8889f0f1b6d136ef353101acb06434111dbefec877796e1a8ab85933bf'
ADDED = (
    'app/models.py',
    'app/schema_contract.py',
    'migrations/versions/20261119_0057_owner_callout_coverage.py',
    'app/templates/v2/scheduling/_shift_dialog.html',
)
OLD = "EXTRA=('AGENTS.md','app/main.py','app/config.py','requirements-test.in','requirements-test.txt','.github/workflows/ci.yml')"
NEW = OLD[:-1] + ',' + ','.join(repr(p) for p in ADDED) + ')'
AUTHORITY = 'November 19 2026: owner explicitly authorized exact four-file scheduling scope extension and qualification'

def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()

def main():
    assert sys.argv[1:] == ['--maintenance-qualification']
    original = (B / 'erupted-dev.py').read_text()
    assert sha_bytes(original.encode()) == EXPECTED, 'Installed predecessor changed'
    assert original.count(OLD) == 1
    candidate = original.replace(OLD, NEW)
    assert candidate.replace(NEW, OLD) == original
    funcs = lambda text: {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(text).body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert funcs(original) == funcs(candidate), 'Enforcement function changed'
    assert [(type(a).__name__, getattr(a, 'lineno', 0)) for a in ast.parse(original).body] == [
        (type(a).__name__, getattr(a, 'lineno', 0)) for a in ast.parse(candidate).body]
    ns = {'__file__': str(B / 'erupted-dev.py'), '__name__': 'erupted_scope_candidate'}
    exec(compile(candidate, str(B / 'erupted-dev.py'), 'exec'), ns)
    base = ns['base']
    ns['qualified']()
    before = base.admission()
    ns['configure']()
    root = ROOT / 'disposable/tmp/owner-scope-qualification-final'
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    repo = root / 'source'
    for rel in ADDED:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    denied = repo / 'unregistered/must-not-exist'
    denied.parent.mkdir()
    also_denied = repo / 'app/services/must-not-exist'
    also_denied.parent.mkdir(parents=True, exist_ok=True)
    sibling = repo / 'app/models-unregistered.py'
    escape = repo / ADDED[3]
    escape.symlink_to(denied)
    source = ns['SOURCE']
    common = pathlib.Path(subprocess.check_output(
        ['git', '-C', str(source), 'rev-parse', '--git-common-dir'], text=True).strip())
    common = common if common.is_absolute() else (source / common).resolve()
    protected = [source / 'bounded-owner-scope-must-not-exist',
                 common / 'bounded-owner-scope-must-not-exist']
    assert all(not p.exists() for p in protected)
    ns['session'].__globals__['ACTIVE'] = {'worktree': str(repo), 'scope': list(ADDED)}
    forbidden_write = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('forbidden')"
    grandchild = ("import subprocess,sys; p=subprocess.run([sys.executable,'-B','-c',"
                  + repr(forbidden_write)
                  + ",sys.argv[1]],capture_output=True); assert p.returncode!=0")
    child = ("import ctypes,pathlib,subprocess,sys\n"
             "for name in sys.argv[1:4]:\n"
             "    pathlib.Path(name).write_text('qualified exact scoped write')\n"
             "grandchild = " + repr(grandchild) + "\n"
             "for name in sys.argv[4:8]:\n"
             "    p=subprocess.run([sys.executable,'-B','-c',grandchild,name],capture_output=True)\n"
             "    assert p.returncode==0, (name,p.stderr)\n"
             "    assert not pathlib.Path(name).exists()\n"
             "lib=ctypes.CDLL('/usr/lib/libsandbox.1.dylib')\n"
             "lib.sandbox_check.restype=ctypes.c_int\n"
             "for name in sys.argv[8:]:\n"
             "    assert lib.sandbox_check(0,b'file-write-data',1,name.encode())!=0, name\n")
    # Last exact allowed file is a symlink escape; it must remain unwritable.
    base.confined([base.PY, '-I', '-B', '-c', child]
                  + [str(repo / p) for p in ADDED[:3]]
                  + [str(denied), str(also_denied), str(sibling), str(escape)]
                  + [str(p) for p in protected], [repo], root, cwd=repo, capture=True)
    assert all((repo / p).read_text() == 'qualified exact scoped write' for p in ADDED[:3])
    assert not denied.exists() and not also_denied.exists() and not sibling.exists()
    assert escape.is_symlink() and not escape.exists()
    assert all(not p.exists() for p in protected)
    escape.unlink()
    second_child = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('qualified exact template write')"
    base.confined([base.PY, '-I', '-B', '-c', second_child, str(escape)], [repo], root, cwd=repo, capture=True)
    assert escape.read_text() == 'qualified exact template write'
    assert all(ns['safe'](p) for p in ADDED)
    bad = ['app', 'migrations', 'app/templates', 'app/models-unregistered.py',
           'migrations/versions/unregistered.py', 'app/templates/v2/scheduling/unregistered.html',
           '../escape', '/tmp/escape', 'app/../escape', '.git/config']
    assert not any(ns['safe'](p) for p in bad)
    bare_env = {k:v for k,v in os.environ.items() if not k.startswith('AVA_BOUNDED')}
    bare = subprocess.run([base.PY, '-I', '-B', str(B / 'erupted-dev.py'), 'check'],
                          env=bare_env, capture_output=True, text=True)
    assert bare.returncode != 0 and 'Unbounded growth refused' in bare.stderr
    checks = dict(exactExtraOnlyChange=True, allEnforcementFunctionsASTIdentical=True,
                  exactRegisteredPathsWritable=True, unregisteredPathsDenied=True,
                  childAndGrandchildConfinement=True, siblingFileDenied=True,
                  symlinkEscapeDenied=True, protectedSourceAndGitDenied=True,
                  bareCheckRefused=True, encryptedRecordedMountsAdmitted=True,
                  resourceGuardsUnchanged=True, noQuotaOrBudgetChanges=True)
    report = dict(status='PASS', time=time.time(), ownerAuthorization=AUTHORITY,
                  checks=checks, predecessorSHA256=EXPECTED,
                  candidateSHA256=sha_bytes(candidate.encode()), oldExtra=OLD, newExtra=NEW,
                  addedScope=list(ADDED), unchangedFunctions=sorted(funcs(original)),
                  accounting=before, protectedSourceUnchanged=True,
                  productFilesEdited=False)
    destination = ROOT / 'source/docs/development/owner-callout-scope-qualification-20261119.json'
    subprocess.run([str(base.L / 'bin/erupted-dev'), 'write', '--id',
                    'AVA-ERUPTED-SWAP-OWNER-20261119', '--path',
                    'docs/development/owner-callout-scope-qualification-20261119.json'],
                   input=json.dumps(report, indent=2) + '\n', text=True, check=True)
    (root / 'erupted-dev.candidate.py').write_text(candidate)
    print('Exact four-file scope qualification PASS')
    print(json.dumps(dict(report=str(destination), predecessorSHA256=EXPECTED,
                          candidateSHA256=report['candidateSHA256'])))
if __name__ == '__main__':
    main()
