"""Blackbox CLI checks for the unambiguous supplied contract surface.

Feature-specific coverage is blocked by the contract gaps reported with
this file. These usage checks deliberately claim no plan-item coverage.
"""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def invoke(tool, args, tmp_path):
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C",
        "FLEET_DB": str(tmp_path / "fleet.db"),
        "EUNOMIA_FLEET_DIR": str(tmp_path / "fleet"),
        "FLEET_WORK_ROOT": str(tmp_path / "work"),
    }
    return subprocess.run(
        [str(ROOT / "bin" / tool), *args],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.covers()
@pytest.mark.parametrize("position", ["plan_reviewer", "code_reviewer", "style_reviewer"])
def test_reviewers_requires_json_flag(position, tmp_path):
    result = invoke(
        "fleet-models", ["reviewers", "example/contract-test", position], tmp_path
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "--json" in result.stderr, result.stderr


@pytest.mark.covers()
@pytest.mark.parametrize(
    "args, diagnostic",
    [
        (["example/contract-test", "0163"], "--impl"),
        (["example/contract-test", "0163", "--impl", "not-an-integer"], "--impl"),
        (["example/contract-test", "0163", "--impl", "1", "--lane", "not-an-integer"], "--lane"),
    ],
)
def test_meet_rejects_malformed_arguments(args, diagnostic, tmp_path):
    result = invoke("fleet-meet", args, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert diagnostic in result.stderr, result.stderr
