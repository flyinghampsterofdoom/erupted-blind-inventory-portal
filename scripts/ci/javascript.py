"""Run every checked-out JavaScript controller test, including nested suites."""
from pathlib import Path
import json
import subprocess

files = sorted(str(p) for p in Path('tests').rglob('*.cjs'))
if not files:
    raise RuntimeError('Missing JavaScript tests cannot pass validation')
print('Selected JavaScript files: ' + json.dumps(files), flush=True)
raise SystemExit(subprocess.call(['node', '--test', *files]))
