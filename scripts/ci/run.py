"""Run selected tests with reproducible stage/results metadata, never live services."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from scripts.ci.policy import selected

OUT = Path('ci-results')

def main():
    suite = sys.argv[1]
    files = selected(suite)
    if not files:
        raise ValueError('Empty test suite is not successful validation')
    OUT.mkdir(exist_ok=True)
    receipt = {'suite': suite, 'files': files, 'phase': 'test-execution',
               'classification': 'application-or-test-assertion; inspect JUnit/logs for cause'}
    (OUT / 'run.json').write_text(json.dumps(receipt, indent=2) + '\n')
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'scripts.ci.pytest_policy',
               '--durations=20', '--junitxml=ci-results/pytest.xml', *files]
    start = time.monotonic()
    with (OUT / 'pytest.log').open('w') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    print((OUT / 'pytest.log').read_text())
    receipt.update(exit_code=result.returncode, duration_seconds=round(time.monotonic() - start, 3))
    if (OUT / 'pytest.xml').exists():
        root = ET.parse(OUT / 'pytest.xml').getroot()
        suites = list(root.iter('testsuite'))
        receipt['counts'] = {key: sum(int(s.get(key, '0')) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
        if receipt['counts']['errors']:
            receipt['classification'] = 'test-setup-or-collection-error; inspect fixture/environment diagnostics'
    (OUT / 'run.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return result.returncode

if __name__ == '__main__':
    raise SystemExit(main())
