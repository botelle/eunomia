from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_RELEASE = REPO_ROOT / "bin" / "fleet-release"
FLEET_WATCH = REPO_ROOT / "bin" / "fleet-watch"
ORCHESTRATOR = REPO_ROOT / "bin" / "orchestrator"


def _isolated_env(tmp_path: Path, *, session: str | None = "test-session-0179") -> dict[str, str]:
    fleet_dir = tmp_path / "fleet"
    home_dir = tmp_path / "home"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": str(tmp_path),
        "USER": "tester",
        "LOGNAME": "tester",
        "EUNOMIA_FLEET_DIR": str(fleet_dir),
        "FLEET_WATCH_REPOS": "",
        "FLEET_PINS": "",
    }
    if session is not None:
        env["EUNOMIA_SESSION"] = session
    return env


@pytest.mark.covers("D4", "DoD2")
def test_fleet_release_help_declares_expect_orphaned_and_reason() -> None:
    proc = subprocess.run(
        [sys.executable, str(FLEET_RELEASE), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "--force" in proc.stdout
    assert "--expect-orphaned" in proc.stdout
    assert "--reason" in proc.stdout


@pytest.mark.covers("D4", "DoD2")
@pytest.mark.parametrize(
    "bad_reason",
    [
        "pr-closed",
        "pr-merged",
        "plan-ready",
        "done",
        "abandoned",
        "ghp_secretTokenValue1234567890abcdef",
        "",
        "plan-done extra",
    ],
)
@pytest.mark.parametrize(
    "extra_flags",
    [
        [],
        ["--force"],
        ["--force", "--expect-orphaned", "holder-session-1"],
    ],
)
def test_fleet_release_rejects_disallowed_reason_tokens_with_exit_2(
    tmp_path: Path,
    bad_reason: str,
    extra_flags: list[str],
) -> None:
    env = _isolated_env(tmp_path)
    fleet_dir = Path(env["EUNOMIA_FLEET_DIR"])

    proc = subprocess.run(
        [
            sys.executable,
            str(FLEET_RELEASE),
            "plan:0179-a-finished-plan-releases-its-lease",
            *extra_flags,
            "--reason",
            bad_reason,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert list(fleet_dir.rglob("*")) == []


@pytest.mark.covers("D4", "DoD2")
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["plan:0179-test", "--reason"],
        ["plan:0179-test", "--expect-orphaned"],
        ["plan:0179-test", "--unknown-flag"],
    ],
)
def test_fleet_release_rejects_malformed_cli_invocations_with_exit_2(
    tmp_path: Path,
    argv: list[str],
) -> None:
    env = _isolated_env(tmp_path)
    fleet_dir = Path(env["EUNOMIA_FLEET_DIR"])

    proc = subprocess.run(
        [sys.executable, str(FLEET_RELEASE), *argv],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert list(fleet_dir.rglob("*")) == []


@pytest.mark.covers("D4", "DoD2")
@pytest.mark.parametrize("reason", ["plan-done", "plan-abandoned"])
@pytest.mark.parametrize(
    "extra_flags",
    [
        [],
        ["--force"],
        ["--force", "--expect-orphaned", "holder-session-1"],
    ],
)
def test_fleet_release_accepts_allowed_reason_tokens_and_expect_orphaned_flag(
    tmp_path: Path,
    reason: str,
    extra_flags: list[str],
) -> None:
    env = _isolated_env(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            str(FLEET_RELEASE),
            "plan:0179-nonexistent",
            *extra_flags,
            "--reason",
            reason,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # Allowed reason tokens and --expect-orphaned pass CLI validation (not exit 2)
    # and fail with exit 1 because the lease record does not exist.
    assert proc.returncode == 1
    assert proc.stdout == ""


@pytest.mark.covers("D2", "DoD2")
def test_fleet_watch_cli_help_and_dry_run_contract(tmp_path: Path) -> None:
    help_proc = subprocess.run(
        [sys.executable, str(FLEET_WATCH), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0
    assert "--dry-run" in help_proc.stdout

    bad_proc = subprocess.run(
        [sys.executable, str(FLEET_WATCH), "--unknown-flag"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_proc.returncode == 2

    env = _isolated_env(tmp_path)
    dry_proc = subprocess.run(
        [sys.executable, str(FLEET_WATCH), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_proc.returncode == 0


@pytest.mark.covers("D1", "DoD1")
def test_orchestrator_refuses_before_lease_activation_with_exit_2(tmp_path: Path) -> None:
    env_no_args = _isolated_env(tmp_path / "no_args")
    proc_no_args = subprocess.run(
        [sys.executable, str(ORCHESTRATOR)],
        env=env_no_args,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_no_args.returncode == 2

    env_no_session = _isolated_env(tmp_path / "no_session", session=None)
    fleet_dir = Path(env_no_session["EUNOMIA_FLEET_DIR"])
    proc_no_session = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "operator/eunomia",
            "plans/0179-a-finished-plan-releases-its-lease.md",
            "plan:0179-a-finished-plan-releases-its-lease",
        ],
        env=env_no_session,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_no_session.returncode == 2
    events_file = fleet_dir / "events.jsonl"
    if events_file.exists():
        assert "lease-released" not in events_file.read_text(encoding="utf-8")


@pytest.mark.covers("D1", "D2", "D3", "D4", "D5")
def test_spec_and_docs_declare_sixth_exception_and_release_flags() -> None:
    spec_text = (REPO_ROOT / "SPEC.md").read_text(encoding="utf-8")
    assert "six narrow, named exceptions" in spec_text
    assert "2026-11-02" in spec_text
    assert "ADR-0029" in spec_text
    assert "--expect-orphaned" in spec_text
    assert "--reason" in spec_text

    declared_text = (REPO_ROOT / "docs" / "declared-behaviour.md").read_text(encoding="utf-8")
    assert "--expect-orphaned" in declared_text
    assert "--reason" in declared_text

    adr_text = (
        REPO_ROOT / "docs" / "adr" / "0029-a-finished-dispatch-releases-its-lease.md"
    ).read_text(encoding="utf-8")
    assert "Accepted" in adr_text
    assert "plan-done" in adr_text
    assert "plan-abandoned" in adr_text

    for rel_path in (
        "docs/plan-dispatch.md",
        "docs/plan-dependencies.md",
        "docs/dispatch-dataflow.md",
    ):
        doc_text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert len(doc_text.strip()) > 0
