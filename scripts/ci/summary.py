"""Always produce a concise summary, including failures before tests could start."""
from pathlib import Path
import json
import os

out = Path('ci-results'); out.mkdir(exist_ok=True)
run = json.loads((out / 'run.json').read_text()) if (out / 'run.json').exists() else {}
skips = json.loads((out / 'skips.json').read_text()) if (out / 'skips.json').exists() else []
row = {'setup': os.environ.get('SETUP_RESULT'), 'build': os.environ.get('BUILD_RESULT'),
       'synthetic-render-artifact-build': os.environ.get('ARTIFACT_BUILD_RESULT', 'not-run'),
       'tests': os.environ.get('TEST_RESULT'), 'javascript': os.environ.get('JS_RESULT')}
summary = ['## CI validation', '', '| Stage | Outcome |', '|---|---|']
summary += ['| ' + k + ' | ' + str(v) + ' |' for k, v in row.items()]
summary += ['', 'Test result: `' + json.dumps(run.get('counts', {})) + '`',
            'Python duration: ' + str(run.get('duration_seconds', 'not run')) + ' seconds.',
            'Classification: ' + run.get('classification', 'Setup/build/environment failure or cancellation; tests not started.'),
            '', '### Skips']
summary += ['- `' + r['nodeid'] + '`: ' + r['reason'] + ' (allowed=' + str(r['allowed']) + ')' for r in skips] or ['- None.']
summary += ['', 'Synthetic Render artifact rehearsal runs on fast only; other matrix suites skip the duplicate. It never qualifies live R2 access.', 'Exact file selection and JUnit, skip reasons, logs, stage outcomes and source/build proof are attached.']
(out / 'stages.json').write_text(json.dumps(row, indent=2) + '\n')
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
    f.write('\n'.join(summary) + '\n')
