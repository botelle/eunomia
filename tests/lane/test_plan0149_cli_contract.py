"""Public CLI validation only; feature-level harness gaps accompany this suite."""

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers()
@pytest.mark.parametrize(
    "arguments",
    [
        ["operator/eunomia", "0149"],
        ["operator/eunomia", "0149", "--impl", "not-an-integer"],
        ["operator/eunomia", "0149", "--impl", "1", "--lane", "not-an-integer"],
        ["operator/eunomia", "0149", "--impl"],
    ],
    ids=["missing-impl", "invalid-impl", "invalid-lane", "missing-impl-value"],
)
def test_fleet_meet_usage_errors_exit_two(arguments, tmp_path):
    result = subprocess.run(
        [str(ROOT / "bin/fleet-meet"), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)


@pytest.mark.covers()
@pytest.mark.parametrize(
    "arguments",
    [
        ["--format", "not-a-format"],
        ["--format"],
        ["--conf"],
        ["--db"],
    ],
    ids=["invalid-format", "missing-format-value", "missing-conf-value", "missing-db-value"],
)
def test_mopsus_usage_errors_exit_two(arguments, tmp_path):
    result = subprocess.run(
        [str(ROOT / "bin/mopsus"), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
