"""Public CLI baseline; host-specific coverage needs a dispatch fixture contract."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers()
def test_orchestrator_without_arguments_refuses_with_stderr(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "EUNOMIA_FLEET_DIR": str(tmp_path / "fleet"),
        "FLEET_WORK_ROOT": str(tmp_path / "work"),
        "FLEET_DB": str(tmp_path / "fleet.db"),
        "EUNOMIA_TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
    }
    result = subprocess.run(
        [str(ROOT / "bin" / "orchestrator")],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stderr.decode(errors="replace")
    assert result.stdout == b""
    assert result.stderr.strip(), "Argument refusal must warn on stderr"
