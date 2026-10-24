"""Conservative selection: only inert docs/CSS can omit the full suite."""
from pathlib import Path
import json
import os
import subprocess

GROUPS = ('schedule-foundation', 'schedule-a', 'schedule-b', 'purchasing', 'inventory-access', 'platform-forecast', 'other')
FAST = (
    'test_ci_policy.py', 'test_release_phase3.py', 'test_runtime_identity.py', 'test_deployment_provider_contracts.py',
    'test_template_feature_binding.py', 'test_release_entrypoints.py',
    'test_canonical_migration.py', 'test_coverage_commitment_migration.py',
    'test_permission_characterization.py', 'test_csrf_header.py',
    'test_cascading_coverage_acceptance.py', 'test_square_write_boundary.py',
    'test_square_cost_workflow_boundaries.py', 'test_purchase_order_math_service.py',
    'test_recount_logic_service.py', 'test_purchasing_receiving_extension.py',
)

def expanded(event, paths):
    # Final main commits and manual qualification always validate every test file.
    if event != 'pull_request' or not paths:
        return True
    def inert(path):
        return (path == 'README.md' or (path.startswith('docs/') and path.endswith('.md'))
                or (path.startswith('app/static/') and path.endswith('.css')))
    return not all(inert(path) for path in paths)

def group(name):
    if name == 'test_v2_scheduling_foundation.py':
        return 'schedule-foundation'
    if any(word in name for word in ('scheduling', 'schedule', 'coverage', 'callout', 'employee_swap', 'publication', 'pto', 'longview', 'weekly_allocations')):
        # These larger existing suites balance the second scheduling worker.
        heavy = {'test_v2_scheduling_preopening.py', 'test_v2_weekly_allocations.py',
                 'test_v2_scheduling_closure_removals.py', 'test_v2_scheduling_history.py',
                 'test_v2_pto_workflow.py', 'test_v2_scheduling_exceptions.py',
                 'test_v2_scheduling_clear.py'}
        return 'schedule-b' if name in heavy else 'schedule-a'
    if any(word in name for word in ('purchasing', 'purchase_order', 'consignment')):
        return 'purchasing'
    if any(word in name for word in ('forecast', 'release', 'runtime', 'deployment', 'schema', 'migration', 'ci_policy', 'history_job', 'history_refresh')):
        return 'platform-forecast'
    if any(word in name for word in ('inventory', 'blind_count', 'recount', 'stocktake', 'unlock', 'permission', 'access', 'session', 'csrf', 'square_write', 'square_cost', 'operational_context')):
        return 'inventory-access'
    return 'other'

def selected(suite, root=Path('.')):
    all_files = sorted(str(p.relative_to(root / 'tests')) for p in (root / 'tests').rglob('*.py')
                       if p.name.startswith('test_') or p.name.endswith('_test.py'))
    if suite == 'fast':
        missing = set(FAST) - set(all_files)
        if missing:
            raise ValueError('Mandatory tests missing: ' + ', '.join(sorted(missing)))
        return ['tests/' + name for name in FAST]
    if suite not in GROUPS:
        raise ValueError('Unknown suite ' + suite)
    return ['tests/' + name for name in all_files if name not in FAST and group(Path(name).name) == suite]

def main():
    event = os.environ['CI_EVENT']
    paths = []
    if event == 'pull_request' and os.environ.get('CI_BASE_SHA'):
        paths = subprocess.check_output(['git', 'diff', '--no-renames', '--name-only', '-z', os.environ['CI_BASE_SHA'], 'HEAD']).decode().split('\0')[:-1]
    result = {'event': event, 'expanded': expanded(event, paths), 'changed_paths': paths,
              'reason': 'Full suite unless PR changes only Markdown documentation or CSS; shared/unknown dependencies expand.',
              'groups': list(GROUPS)}
    Path('ci-results').mkdir(exist_ok=True)
    Path('ci-results/selection.json').write_text(json.dumps(result, indent=2) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('expanded=' + str(result['expanded']).lower() + '\n')
        output.write('suites=' + json.dumps(['fast', *GROUPS] if result['expanded'] else ['fast']) + '\n')
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
