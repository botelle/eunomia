"""Public CLI regression checks; feature scenarios need the missing contracts.

Run with: python3 -m pytest -q tests/lane/test_0144_cli_contract.py
No repository implementation modules are imported.
"""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli_env(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FLEET_DB": str(tmp_path / "fleet.db"),
        "EUNOMIA_FLEET_DIR": str(tmp_path / "fleet"),
        "USER": "lane-contract-test",
    }
    return env


def invoke(program, arguments, env, cwd):
    return subprocess.run(
        [str(ROOT / "bin" / program), *arguments],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.covers()
@pytest.mark.parametrize(
    "arguments",
    [
        ["get"],
        ["set", "inherited_red_wait_hours"],
        ["get", "main_tests_context_prefix", "--repo"],
        ["set", "inherited_red_wait_hours", "12", "--actor"],
    ],
)
def test_config_malformed_arguments_exit_two(arguments, cli_env, tmp_path):
    result = invoke("fleet-config", arguments, cli_env, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stderr.strip(), "Malformed arguments must produce a diagnostic"


@pytest.mark.covers()
def test_watch_rejects_unrecognized_flag(cli_env, tmp_path):
    result = invoke(
        "fleet-watch", ["--lane-contract-invalid-option"], cli_env, tmp_path
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stderr.strip(), "Malformed arguments must produce a diagnostic"
