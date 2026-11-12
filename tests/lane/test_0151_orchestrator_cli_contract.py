"""Blackbox baseline only; feature scenarios require the missing harness contract."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers()
def test_orchestrator_without_arguments_refuses_with_stderr(tmp_path):
    """Declared behaviour: wrong argument count exits 2; stdout is always empty."""
    result = subprocess.run(
        [str(ROOT / "bin" / "orchestrator")],
        cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert result.stderr.strip(), "Expected the declared refusal diagnostic"
