"""Exact artifact tree composition keeps unrelated source, modes, index and refs."""
from pathlib import Path
import hashlib,os,subprocess
import pytest
from scripts.development.product_explorer_release_objects import replace_tree

@pytest.fixture
def git_fixture(tmp_path):
    repo=tmp_path/"synthetic-artifact"
    subprocess.run(["git","init","-q",str(repo)],check=True)
    def run(*args,input=None):
        return subprocess.check_output(["git","-C",str(repo),*args],input=input)
    (repo/"app").mkdir();(repo/"docs").mkdir()
    (repo/"app/change.py").write_text("before\n")
    (repo/"app/keep.sh").write_text("#!/bin/sh\nexit 0\n");(repo/"app/keep.sh").chmod(0o755)
    (repo/"docs/keep.txt").write_text("retained\n")
    run("add",".")
    tree=run("write-tree").decode().strip()
    return repo,run,tree

def test_exact_tree_replacement_preserves_other_entries_modes_index_and_working_tree(git_fixture):
    repo,run,tree=git_fixture
    original_index=hashlib.sha256((repo/".git/index").read_bytes()).hexdigest()
    original_head=(repo/".git/HEAD").read_bytes()
    replacement=replace_tree(tree,{"app/change.py":b"after\n"},runner=run)
    assert replacement!=tree
    assert run("show",replacement+":app/change.py")==b"after\n"
    assert run("show",replacement+":app/keep.sh")==b"#!/bin/sh\nexit 0\n"
    assert run("ls-tree",replacement+":app","keep.sh").split()[0]==b"100755"
    assert run("rev-parse",replacement+":docs")==run("rev-parse",tree+":docs")
    assert (repo/"app/change.py").read_text()=="before\n"
    assert hashlib.sha256((repo/".git/index").read_bytes()).hexdigest()==original_index
    assert (repo/".git/HEAD").read_bytes()==original_head

def test_tree_composer_refuses_new_or_escaping_paths(git_fixture):
    _,run,tree=git_fixture
    for path in ("app/missing.py","missing/file.py","../escape",".git/config","/absolute"):
        with pytest.raises(AssertionError):replace_tree(tree,{path:b"not admitted"},runner=run)
