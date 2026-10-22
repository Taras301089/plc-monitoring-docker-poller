"""Exercise SCADA navigation with the real PMM tab handler and feature script."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_scada_navigation_lifecycle_and_keyboard():
    subprocess.run(
        ["node", str(Path(__file__).with_name("navigation_harness.js")),
         str(ROOT / "Scada/static/scada-navigation.js"),
         str(ROOT / "fastapi-api/static/index.html")],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
