"""CI is fail-closed on critical/shared/unknown changes and unexpected skips."""
from pathlib import Path
import pytest
from scripts.ci.policy import expanded, selected, GROUPS, FAST
from scripts.ci.pytest_policy import ALLOWED_SKIPS

@pytest.mark.parametrize('path', ['app/config.py', 'app/models.py', 'app/templates/v2/base.html', 'app/static/v2/scheduling.js', 'requirements.txt', 'migrations/versions/new.py', 'scripts/release/build.sh', '.github/workflows/ci.yml', 'tests/test_new.py', 'unrecognized/file'])
def test_critical_or_shared_dependency_always_expands(path):
    assert expanded('pull_request', ['docs/example.md', path])

def test_only_inert_changes_can_be_fast():
    assert not expanded('pull_request', ['README.md', 'docs/example.md', 'app/static/v2/v2.css'])
    assert expanded('push', ['README.md'])
    assert expanded('workflow_dispatch', ['README.md'])
    assert expanded('pull_request', [])

def test_every_python_file_is_in_exactly_one_expanded_group():
    root = Path(__file__).resolve().parents[1]
    all_files = sorted(str(p.relative_to(root)) for p in (root / 'tests').rglob('*.py') if p.name.startswith('test_') or p.name.endswith('_test.py'))
    grouped = [f for suite in ('fast', *GROUPS) for f in selected(suite, root)]
    assert sorted(grouped) == all_files
    assert len(grouped) == len(set(grouped))
    assert len(selected('fast', root)) == len(FAST)

def test_hosted_skip_exceptions_are_narrow():
    assert len(ALLOWED_SKIPS) == 2
    assert all('real_' in name and 'r2' in name for name in ALLOWED_SKIPS)


def test_nested_python_files_are_never_omitted(tmp_path):
    directory = tmp_path / 'tests' / 'nested'
    directory.mkdir(parents=True)
    (directory / 'test_new_critical.py').write_text('')
    (directory / 'new_critical_test.py').write_text('')
    assert selected('other', tmp_path) == [
        'tests/nested/new_critical_test.py', 'tests/nested/test_new_critical.py']
