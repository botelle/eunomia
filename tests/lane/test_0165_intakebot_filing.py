"""Blackbox checks using only public CLI entry points and scratch state."""

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
KEY = "intakebot_filing"
REPO = "operator/intakebot"
OTHER_REPO = "operator/lynceus"


@pytest.fixture
def cli(tmp_path):
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "USER": "lane-test",
        "FLEET_DB": str(tmp_path / "fleet.db"),
        "EUNOMIA_FLEET_DIR": str(fleet_dir),
    }

    def run(program, *args):
        return subprocess.run(
            [str(ROOT / "bin" / program), *args],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    migrated = run("fleet-db-migrate")
    assert migrated.returncode == 0, (migrated.stdout, migrated.stderr)

    def config(*args):
        return run("fleet-config", *args)

    return config


def get_setting(cli, repo):
    result = cli("get", KEY, "--repo", repo, "--json")
    assert result.returncode == 0, (result.stdout, result.stderr)
    data = json.loads(result.stdout)
    assert isinstance(data, dict), data
    assert data["key"] == KEY
    return data


def set_setting(cli, value, repo):
    result = cli("set", KEY, value, "--repo", repo, "--actor", "lane-test")
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.covers("D2", "DoD1")
def test_missing_repo_row_uses_disabled_builtin(cli):
    data = get_setting(cli, REPO)
    assert data["value"] == "disabled"
    assert data["source"] == "builtin"


@pytest.mark.covers("D2", "DoD1")
def test_repo_can_enable_then_disable_without_enabling_another_repo(cli):
    set_setting(cli, "enabled", REPO)
    assert get_setting(cli, REPO)["value"] == "enabled"
    other = get_setting(cli, OTHER_REPO)
    assert other["value"] == "disabled"
    assert other["source"] == "builtin"

    set_setting(cli, "disabled", REPO)
    assert get_setting(cli, REPO)["value"] == "disabled"


@pytest.mark.covers("D2", "DoD1")
def test_default_cannot_enable_filing(cli):
    set_setting(cli, "disabled", "default")
    result = cli("set", KEY, "enabled", "--repo", "default")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert get_setting(cli, "default")["value"] == "disabled"
    assert get_setting(cli, REPO)["value"] == "disabled"


@pytest.mark.covers("D2", "DoD1")
def test_on_is_rejected_without_changing_existing_value(cli):
    set_setting(cli, "enabled", REPO)
    result = cli("set", KEY, "on", "--repo", REPO)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert get_setting(cli, REPO)["value"] == "enabled"
