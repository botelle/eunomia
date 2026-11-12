"""Blackbox CLI regression checks; feature-specific coverage needs a fixture harness."""

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers()
@pytest.mark.parametrize(
    "program,arguments",
    [
        pytest.param("fleet-lane", [], id="lane-missing-positionals-and-sha"),
        pytest.param(
            "fleet-lane", ["example/repository", "0142", "--dry-run"],
            id="lane-missing-required-sha",
        ),
        pytest.param(
            "fleet-lane", ["example/repository", "0142", "--sha"],
            id="lane-sha-missing-value",
        ),
        pytest.param("fleet-meet", [], id="meet-missing-positionals-and-impl"),
        pytest.param(
            "fleet-meet", ["example/repository", "0142", "--dry-run"],
            id="meet-missing-required-impl",
        ),
        pytest.param(
            "fleet-meet",
            ["example/repository", "0142", "--impl", "not-an-integer", "--dry-run"],
            id="meet-impl-requires-integer",
        ),
        pytest.param(
            "fleet-meet",
            ["example/repository", "0142", "--impl", "1", "--lane", "not-an-integer", "--dry-run"],
            id="meet-lane-requires-integer",
        ),
    ],
)
def test_malformed_invocation_exits_two(program, arguments, tmp_path):
    result = subprocess.run(
        [str(ROOT / "bin" / program), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"{program} {arguments!r}: expected usage exit 2, got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
