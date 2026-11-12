"""Public CLI usage checks; comparison integration is blocked by bundle gaps.

Run through the declared pytest harness. No implementation modules are imported.
These checks do not claim coverage of plan 0143's comparison deliverables.
"""

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLAN = "0143-two-lanes-are-compared-item-by-item"


@pytest.mark.parametrize(
    "program,args",
    [
        pytest.param("fleet-meet", [], id="meet-missing-required-arguments"),
        pytest.param(
            "fleet-meet", ["operator/eunomia", PLAN],
            id="meet-missing-impl",
        ),
        pytest.param(
            "fleet-meet", ["operator/eunomia", PLAN, "--impl", "not-an-int"],
            id="meet-impl-must-be-integer",
        ),
        pytest.param(
            "fleet-meet",
            ["operator/eunomia", PLAN, "--impl", "1", "--lane", "not-an-int"],
            id="meet-lane-must-be-integer",
        ),
        pytest.param(
            "fleet-meet",
            ["operator/eunomia", PLAN, "--impl", "1", "--lane", "2", "--lane"],
            id="meet-repeated-lane-requires-value",
        ),
        pytest.param("fleet-triage", [], id="triage-missing-required-arguments"),
        pytest.param(
            "fleet-triage", ["operator/eunomia", PLAN],
            id="triage-missing-impl-sha",
        ),
        pytest.param(
            "fleet-triage", ["operator/eunomia", PLAN, "--impl-sha"],
            id="triage-impl-sha-requires-value",
        ),
    ],
)
def test_malformed_cli_exits_two(program, args, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / program), *args],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"{program} {args!r}: expected declared usage exit 2; "
        f"got {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
