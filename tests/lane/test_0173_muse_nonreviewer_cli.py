"""Blackbox checks using the CLI and fresh-database setup shown in CI."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fleet_cli(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "USER": "lane-test",
        "LOGNAME": "lane-test",
        "FLEET_DB": str(tmp_path / "fleet.db"),
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

    # The supplied talos-contract workflow uses this public initialization path.
    initialized = invoke("fleet-db-migrate")
    assert initialized.returncode == 0, (
        initialized.stdout, initialized.stderr
    )
    return invoke


@pytest.mark.covers("D2", "D3")
# `implementer` left this list when sub-muse gained a writer_argv: it implements
# now (tests/test_writer_runners.py), and only tester/mediator stay refused.
@pytest.mark.parametrize("position", ["tester", "mediator"])
def test_muse_refused_at_nonreviewer_positions(fleet_cli, position):
    before = fleet_cli("fleet-models", "list")
    assert before.returncode == 0, (before.stdout, before.stderr)

    result = fleet_cli(
        "fleet-models", "set", "operator/eunomia", position, "sub-muse",
        "--actor", "lane-test", "--source", "lane-0173",
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "no implementer/tester spawn path for muse yet" in (
        result.stdout + result.stderr
    )

    after = fleet_cli("fleet-models", "list")
    assert after.returncode == 0, (after.stdout, after.stderr)
    assert after.stdout == before.stdout, "Refused assignment changed listed rows"


@pytest.mark.covers("D3")
@pytest.mark.parametrize("position", ["impl_supervisor", "test_supervisor"])
def test_muse_refused_at_other_nonreviewer_positions(fleet_cli, position):
    result = fleet_cli(
        "fleet-models", "set", "operator/eunomia", position, "sub-muse",
        "--actor", "lane-test", "--source", "lane-0173",
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "no implementer/tester spawn path for muse yet" in (
        result.stdout + result.stderr
    )
