"""Blackbox checks limited to the supplied public contract.

Policy enforcement cases require the contract additions recorded in gaps.
No application modules are imported and no database schema is assumed.
"""

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
POSITIONS = (
    "implementer", "plan_reviewer", "code_reviewer", "tester",
    "mediator", "impl_supervisor", "test_supervisor", "style_reviewer",
)


@pytest.mark.covers("D1")
def test_shipped_policy_is_empty_version_one():
    policy = json.loads(
        (ROOT / "config" / "model-policy.json").read_text(encoding="utf-8")
    )
    assert isinstance(policy, dict)
    assert set(policy) == {"v", "model_groups", "repo_groups", "deny"}
    assert type(policy["v"]) is int
    assert policy["v"] == 1
    assert policy["model_groups"] == {}
    assert policy["repo_groups"] == {}
    assert policy["deny"] == []


@pytest.mark.covers()
@pytest.mark.parametrize("position", POSITIONS)
def test_set_requires_a_value_at_each_declared_position(tmp_path, position):
    # This tests the published CLI grammar, not policy enforcement.
    home = tmp_path / "home"
    home.mkdir()
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USER": "lane-contract-test",
        "LOGNAME": "lane-contract-test",
        "TMPDIR": str(tmp_path),
        "FLEET_DB": str(tmp_path / "fleet.db"),
        "EUNOMIA_FLEET_DIR": str(ledger),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [str(ROOT / "bin" / "fleet-models"),
         "set", "operator/lane-policy-contract", position],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    assert result.stderr.strip(), "Malformed CLI arguments must report a diagnostic"
