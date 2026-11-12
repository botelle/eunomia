"""Process-level smoke check explicitly required by plan 0202 section 4."""

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers("D2")
def test_orchestrator_imports_cleanly():
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            'import runpy; runpy.run_path("bin/orchestrator", run_name="x")',
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"Plan 0202 requires orchestrator import to succeed; exit={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
