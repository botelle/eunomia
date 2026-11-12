"""Blackbox CLI checks; held scenarios require the missing fixture contract."""

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers()
@pytest.mark.parametrize(
    "arguments",
    [
        ["--conf"],
        ["--repos-conf"],
        ["--dry-run", "--conf"],
        ["--dry-run=unexpected"],
    ],
    ids=["missing-conf", "missing-repos-conf", "dry-run-missing-conf", "boolean-value"],
)
def test_stalled_rejects_malformed_arguments(arguments, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin/fleet-stalled"), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
