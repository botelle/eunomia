"""Blackbox checks for the attestation CLI used by plan 0121.

These cover declared argument refusals, not launchd deployment or scheduling.
"""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["report"], id="report-requires-month"),
        pytest.param(["report", "--month"], id="month-requires-value"),
        pytest.param(
            ["check", "--lane-0121-unknown-option"],
            id="check-rejects-malformed-flags",
        ),
    ],
)
def test_attest_refuses_malformed_arguments(tmp_path, arguments):
    home = tmp_path / "home"
    home.mkdir()
    spool = tmp_path / "spool"
    spool.mkdir()
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "FLEET_ATTEST_SPOOL": str(spool),
    }
    result = subprocess.run(
        [str(ROOT / "bin" / "fleet-attest"), *arguments],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"arguments={arguments!r}; exit={result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
