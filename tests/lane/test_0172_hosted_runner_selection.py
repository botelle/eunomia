"""Exercise hosted-runner selection through the public CLI, without dispatch."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers("D2")
@pytest.mark.parametrize("runner", ["hosted-muse-spark-open", "hosted-glm-5.3"])
def test_hosted_runner_accepted_for_implementer(tmp_path, runner):
    # The supplied CI harness documents migration into a fresh FLEET_DB
    # followed by fleet-models set default POSITION RUNNER.
    home = tmp_path / "home"
    home.mkdir()
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
        "HOME": str(home),
        "USER": "lane-test",
        "LANG": "C",
        "FLEET_DB": str(tmp_path / "fleet.db"),
        "EUNOMIA_FLEET_DIR": str(fleet),
    }

    def invoke(program, *args):
        return subprocess.run(
            [str(ROOT / "bin" / program), *args],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    migrated = invoke("fleet-db-migrate")
    assert migrated.returncode == 0, (
        f"Fresh database migration failed: {migrated.stdout}\n{migrated.stderr}"
    )
    selected = invoke("fleet-models", "set", "default", "implementer", runner)
    assert selected.returncode == 0, (
        f"Implementer selection refused {runner}: "
        f"{selected.stdout}\n{selected.stderr}"
    )
