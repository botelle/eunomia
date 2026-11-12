"""Blackbox CLI checks; prompt-content coverage requires the harness in gaps."""

from pathlib import Path
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("argv", [
    [],
    ["example/project"],
    ["example/project", "0141"],
    ["example/project", "0141", "--dry-run"],
    ["example/project", "0141", "--sha"],
])
def test_lane_requires_repository_plan_and_sha(tmp_path, argv):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("FLEET_", "EUNOMIA_"))
    }
    env.update({
        "EUNOMIA_FLEET_DIR": str(tmp_path / "fleet"),
        "FLEET_WORK_ROOT": str(tmp_path / "work"),
        "FLEET_DB": str(tmp_path / "fleet.db"),
    })
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin/fleet-lane"), *argv],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"Malformed invocation must exit 2; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
