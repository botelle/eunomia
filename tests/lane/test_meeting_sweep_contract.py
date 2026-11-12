"""Blackbox integration tests for plan 0140: A merged implementation's lanes are still met.

Tests exercise the contracted CLI entry points (bin/fleet-watch, bin/fleet-meet,
bin/fleet-triage, bin/fleet-emit, bin/fleet-events) and the documented deliverables
(docs/plan-dispatch.md and tests/test_meeting_sweep.py) using only externally
declared interfaces from the contract bundle and declared-behaviour specification.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = REPO_ROOT / "bin"


def _run_cli(
    prog: str,
    args: list[str],
    *,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(BIN_DIR / prog), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_fleet_meet_cli_contract_accepts_plan_d1_argv_shape() -> None:
    """D1 specifies invoking `fleet-meet <repo> <plan> --impl <pr>`.

    Verify the CLI surface accepts positional `repo` and `plan_id` and required
    integer `--impl` option, and exits 2 on missing `--impl` or non-integer `--impl`.
    """
    help_proc = _run_cli("fleet-meet", ["--help"])
    assert help_proc.returncode == 0
    assert "--impl" in help_proc.stdout
    assert "--lane" in help_proc.stdout
    assert "--dry-run" in help_proc.stdout

    missing_impl = _run_cli("fleet-meet", ["operator/eunomia", "0140"])
    assert missing_impl.returncode == 2

    bad_impl = _run_cli("fleet-meet", ["operator/eunomia", "0140", "--impl", "not-an-int"])
    assert bad_impl.returncode == 2


def test_fleet_triage_cli_contract_accepts_plan_d1_argv_shape() -> None:
    """D1 specifies invoking `fleet-triage <repo> <plan> --impl-sha <merged head>`.

    Verify the CLI surface accepts positional `repo` and `plan_id` and required
    `--impl-sha` option, and exits 2 on missing `--impl-sha`.
    """
    help_proc = _run_cli("fleet-triage", ["--help"])
    assert help_proc.returncode == 0
    assert "--impl-sha" in help_proc.stdout
    assert "--dry-run" in help_proc.stdout
    assert "--no-post" in help_proc.stdout

    missing_sha = _run_cli("fleet-triage", ["operator/eunomia", "0140"])
    assert missing_sha.returncode == 2


def test_fleet_watch_cli_surface_and_malformed_arg_exit_code() -> None:
    """Verify `bin/fleet-watch` exposes its contracted flags and exits 2 on bad flags."""
    help_proc = _run_cli("fleet-watch", ["--help"])
    assert help_proc.returncode == 0
    assert "--dry-run" in help_proc.stdout
    assert "--resident" in help_proc.stdout
    assert "--pins-only" in help_proc.stdout

    bad_flag = _run_cli("fleet-watch", ["--no-such-flag"])
    assert bad_flag.returncode == 2


def test_lane_ended_event_roundtrip_for_meeting_sweep_inputs(tmp_path: Path) -> None:
    """D1 reads `lane-ended` events with `plan`, `runner`, `pr`, and `repo`.

    Verify `fleet-emit` and `fleet-events` accept `lane-ended` with these fields
    and round-trip the event in an isolated `EUNOMIA_FLEET_DIR`.
    """
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir()
    env = {
        "EUNOMIA_FLEET_DIR": str(fleet_dir),
        "EUNOMIA_SESSION": "test-session-0140",
    }
    detail = json.dumps(
        {
            "plan": "0140-a-merged-implementation-s-lanes-are-still-met",
            "runner": "claude",
            "pr": 922,
            "outcome": "pr-opened",
        }
    )
    emit_proc = _run_cli(
        "fleet-emit",
        [
            "lane-ended",
            "--repo",
            "operator/eunomia",
            "--pr",
            "922",
            "--detail-json",
            detail,
        ],
        env_overrides=env,
    )
    assert emit_proc.returncode == 0, emit_proc.stderr
    emitted = json.loads(emit_proc.stdout.strip())
    assert emitted["type"] == "lane-ended"
    assert emitted["repo"] == "operator/eunomia"
    assert emitted["detail"]["plan"] == "0140-a-merged-implementation-s-lanes-are-still-met"
    assert emitted["detail"]["runner"] == "claude"
    assert emitted["detail"]["pr"] == 922

    events_proc = _run_cli(
        "fleet-events",
        ["--type", "lane-ended", "--json"],
        env_overrides=env,
    )
    assert events_proc.returncode == 0, events_proc.stderr
    lines = [json.loads(line) for line in events_proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["detail"]["pr"] == 922


def test_fleet_watch_one_shot_dry_run_exits_zero_with_lane_ended_events(tmp_path: Path) -> None:
    """Verify `bin/fleet-watch --dry-run` and `--pins-only` exit 0 in a sandboxed fleet dir."""
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir()
    pins_conf = tmp_path / "pins.conf"
    pins_conf.write_text("", encoding="utf-8")
    repos_conf = tmp_path / "repos.conf"
    repos_conf.write_text("", encoding="utf-8")

    now = dt.datetime.now(dt.timezone.utc)
    recent_ts = (now - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_ts = (now - dt.timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")

    events_file = fleet_dir / "events.jsonl"
    events = [
        {
            "ts": old_ts,
            "actor": "lane-old",
            "type": "lane-ended",
            "repo": "operator/eunomia",
            "pr": 800,
            "sha": "a" * 40,
            "lease": None,
            "detail": {
                "plan": "0100-old-plan",
                "runner": "claude",
                "pr": 800,
                "outcome": "pr-opened",
            },
        },
        {
            "ts": recent_ts,
            "actor": "lane-recent",
            "type": "lane-ended",
            "repo": "operator/eunomia",
            "pr": 922,
            "sha": "b" * 40,
            "lease": None,
            "detail": {
                "plan": "0138-some-plan",
                "runner": "claude",
                "pr": 922,
                "outcome": "pr-opened",
            },
        },
    ]
    events_file.write_text(
        "".join(json.dumps(ev) + "\n" for ev in events),
        encoding="utf-8",
    )

    env = {
        "EUNOMIA_FLEET_DIR": str(fleet_dir),
        "EUNOMIA_SESSION": "watch-test-0140",
        "FLEET_PINS": str(pins_conf),
        "FLEET_WATCH_REPOS": str(repos_conf),
        "FLEET_TOKEN_CMD": "false",
    }

    pins_proc = _run_cli("fleet-watch", ["--pins-only", "--dry-run"], env_overrides=env)
    assert pins_proc.returncode == 0, pins_proc.stderr

    dry_proc = _run_cli("fleet-watch", ["--dry-run"], env_overrides=env)
    assert dry_proc.returncode == 0, dry_proc.stderr


def test_deliverable_d3_unit_test_file_present() -> None:
    """D3 requires `tests/test_meeting_sweep.py` to exist at its declared path."""
    target = REPO_ROOT / "tests" / "test_meeting_sweep.py"
    assert target.is_file(), "Deliverable D3 file tests/test_meeting_sweep.py is missing"


def test_deliverable_d4_plan_dispatch_docs_describe_meeting_sweep() -> None:
    """D4 requires `docs/plan-dispatch.md` to document the meeting sweep after the steward's meeting step.

    Specifically: when it runs, what it starts (`fleet-meet` and `fleet-triage`),
    and its retry rules (exit 75 / 30 minutes).
    """
    doc_path = REPO_ROOT / "docs" / "plan-dispatch.md"
    assert doc_path.is_file(), "docs/plan-dispatch.md is missing"
    text = doc_path.read_text(encoding="utf-8")

    assert "fleet-meet" in text
    assert "fleet-triage" in text
    assert "75" in text, "docs/plan-dispatch.md should mention exit 75 retry rule"
    assert "30" in text, "docs/plan-dispatch.md should mention 30-minute retry backoff"
    assert "14" in text, "docs/plan-dispatch.md should mention the 14-day lane-ended window"
