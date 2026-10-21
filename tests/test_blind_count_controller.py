import shutil
import subprocess
import pytest

def test_count_browser_controller():
    node=shutil.which('node')
    if node is None: pytest.skip('Node.js is required for browser-controller behavior tests')
    result=subprocess.run([node,'--test','tests/blind_count_controller_test.cjs'],capture_output=True,text=True)
    assert result.returncode==0, result.stdout+result.stderr
