"""Blackbox checks limited to the supplied orchestrator CLI declaration."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers()
@pytest.mark.parametrize("args", [[], ["example/repo"], ["example/repo", "plans/example.md"]])
def test_incomplete_dispatch_arguments_are_refused(tmp_path, args):
    home = tmp_path / "home"
    home.mkdir()
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "EUNOMIA_FLEET_DIR": str(tmp_path / "fleet"),
        "FLEET_DB": str(tmp_path / "fleet.db"),
        "EUNOMIA_TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
        "LANG": "C",
    }
    result = subprocess.run(
        [str(ROOT / "bin/orchestrator"), *args],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert result.stderr.strip(), "A refused invocation must warn on stderr."
