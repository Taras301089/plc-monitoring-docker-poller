"""Theme propagation across browser tabs and same-origin SCADA frames."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_shared_theme_browser_behaviour():
    harness = Path(__file__).with_name("shared_theme_harness.cjs")
    result = subprocess.run(
        ["node", "--test", str(harness)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
