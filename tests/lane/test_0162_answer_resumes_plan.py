from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_CONFIG = REPO_ROOT / "bin" / "fleet-config"
FLEET_EVENTS = REPO_ROOT / "bin" / "fleet-events"


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["FLEET_DB"] = str(tmp_path / "fleet.db")
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    env["EUNOMIA_SESSION"] = "test-0162-session"
    env["USER"] = "tester"
    return env


@pytest.mark.covers("DoD5")
def test_question_page_minutes_default_and_bounds(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)

    get_default = subprocess.run(
        [sys.executable, str(FLEET_CONFIG), "get", "question_page_minutes", "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert get_default.returncode == 0, get_default.stderr
    payload = json.loads(get_default.stdout)
    assert int(payload["value"]) == 240

    for invalid_value in ("10", "5000"):
        refused = subprocess.run(
            [
                sys.executable,
                str(FLEET_CONFIG),
                "set",
                "question_page_minutes",
                invalid_value,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert refused.returncode == 1, (
            f"expected fleet-config to refuse {invalid_value}, got rc={refused.returncode}"
        )

    for valid_value in ("30", "2880"):
        accepted = subprocess.run(
            [
                sys.executable,
                str(FLEET_CONFIG),
                "set",
                "question_page_minutes",
                valid_value,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert accepted.returncode == 0, accepted.stderr

        get_after = subprocess.run(
            [
                sys.executable,
                str(FLEET_CONFIG),
                "get",
                "question_page_minutes",
                "--json",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert get_after.returncode == 0, get_after.stderr
        after_payload = json.loads(get_after.stdout)
        assert int(after_payload["value"]) == int(valid_value)


@pytest.mark.covers("DoD6")
def test_question_answered_event_type_registered_and_synced(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)

    events_proc = subprocess.run(
        [
            sys.executable,
            str(FLEET_EVENTS),
            "--type",
            "question-answered",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert events_proc.returncode == 0, events_proc.stderr

    sync_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(REPO_ROOT / "tests" / "test_event_type_sync.py"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert sync_proc.returncode == 0, sync_proc.stdout + sync_proc.stderr
