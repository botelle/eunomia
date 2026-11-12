from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_MODELS = REPO_ROOT / "bin" / "fleet-models"
FLEET_DB_MIGRATE = REPO_ROOT / "bin" / "fleet-db-migrate"
DOCS_PER_REPO_MODELS = REPO_ROOT / "docs" / "per-repo-models.md"

CLAUDE_RUNNERS = (
    "sub-fable",
    "sub-opus-5-5",
    "sub-sonnet-5",
    "sub-sonnet",
    "sub-opus",
    "api-sonnet",
)

NON_CLAUDE_CLI_RUNNERS = (
    "sub-codex",
    "sub-antigravity",
)

ALL_NAMED_RUNNERS = (
    *CLAUDE_RUNNERS,
    *NON_CLAUDE_CLI_RUNNERS,
    "local-qwen",
)

NON_REVIEWER_POSITIONS = (
    "implementer",
    "tester",
    "mediator",
    "impl_supervisor",
    "test_supervisor",
)

REVIEWER_POSITIONS = (
    "plan_reviewer",
    "code_reviewer",
)

SPAWN_PATH_MESSAGE = "no review spawn path yet for its harness"


@pytest.fixture()
def fleet_env(tmp_path: Path) -> dict[str, str]:
    db_path = tmp_path / "fleet.db"
    fleet_dir = tmp_path / "fleet-dir"
    fleet_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["FLEET_DB"] = str(db_path)
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    env["EUNOMIA_SESSION"] = "test-session-0153"
    env["USER"] = "tester"

    proc = subprocess.run(
        [sys.executable, str(FLEET_DB_MIGRATE)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"fleet-db-migrate failed (rc={proc.returncode}):\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return env


def run_fleet_models(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FLEET_MODELS), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.covers("D1", "D2", "D3", "D4", "D5")
@pytest.mark.parametrize("runner", CLAUDE_RUNNERS)
@pytest.mark.parametrize("position", (*NON_REVIEWER_POSITIONS, *REVIEWER_POSITIONS))
def test_every_claude_runner_accepted_at_every_position(
    fleet_env: dict[str, str], runner: str, position: str
) -> None:
    """Every Claude runner, including formerly alias-pinned sub-sonnet, sub-opus,
    and api-sonnet, has a full version and is accepted at every position."""
    res = run_fleet_models(fleet_env, "set", "operator/eunomia", position, runner)
    assert res.returncode == 0, (
        f"Expected {runner} to be accepted at {position}, got rc={res.returncode}: "
        f"stderr={res.stderr!r}"
    )

    get_res = run_fleet_models(fleet_env, "get", "operator/eunomia")
    assert get_res.returncode == 0, (
        f"fleet-models get failed after setting {position}={runner}: "
        f"stderr={get_res.stderr!r}"
    )
    assert runner in get_res.stdout


@pytest.mark.covers("D1", "D2", "D3", "D4", "D5")
@pytest.mark.parametrize("runner", ("sub-sonnet", "sub-opus", "api-sonnet"))
@pytest.mark.parametrize("position", REVIEWER_POSITIONS)
def test_formerly_alias_claude_runners_resolve_in_reviewers_json(
    fleet_env: dict[str, str], runner: str, position: str
) -> None:
    """Formerly alias-pinned Claude runners can be configured at reviewer positions
    and resolved via `fleet-models reviewers --json`."""
    set_res = run_fleet_models(fleet_env, "set", "operator/eunomia", position, runner)
    assert set_res.returncode == 0, f"stderr={set_res.stderr!r}"

    rev_res = run_fleet_models(
        fleet_env, "reviewers", "operator/eunomia", position, "--json"
    )
    assert rev_res.returncode == 0, f"stderr={rev_res.stderr!r}"
    payload = json.loads(rev_res.stdout)
    assert runner in json.dumps(payload)


@pytest.mark.covers("D1", "D2", "D3", "D5")
@pytest.mark.parametrize("runner", NON_CLAUDE_CLI_RUNNERS)
@pytest.mark.parametrize("position", NON_REVIEWER_POSITIONS)
def test_non_claude_cli_runners_accepted_at_tester_and_non_reviewer_positions(
    fleet_env: dict[str, str], runner: str, position: str
) -> None:
    """sub-codex and sub-antigravity declare full versions from their families and
    are accepted at tester and other non-reviewer positions."""
    res = run_fleet_models(fleet_env, "set", "operator/eunomia", position, runner)
    assert res.returncode == 0, (
        f"Expected {runner} to be accepted at {position}, got rc={res.returncode}: "
        f"stderr={res.stderr!r}"
    )


@pytest.mark.covers("D4", "D5")
@pytest.mark.parametrize("runner", NON_CLAUDE_CLI_RUNNERS)
@pytest.mark.parametrize("position", REVIEWER_POSITIONS)
def test_non_claude_cli_runners_accepted_at_reviewer_positions(
    fleet_env: dict[str, str], runner: str, position: str
) -> None:
    """Plan 0158: sub-codex and sub-antigravity hold reviewer seats; nothing refuses."""
    res = run_fleet_models(fleet_env, "set", "operator/eunomia", position, runner)
    assert res.returncode == 0, (
        f"Expected {runner} to be accepted at {position}, got rc={res.returncode}: "
        f"{res.stdout!r} {res.stderr!r}"
    )
    assert SPAWN_PATH_MESSAGE not in f"{res.stdout}\n{res.stderr}"


@pytest.mark.covers("D1", "D2", "D3", "D5")
@pytest.mark.parametrize("position", REVIEWER_POSITIONS)
def test_local_qwen_accepted_at_reviewer_positions(
    fleet_env: dict[str, str], position: str
) -> None:
    """local-qwen is an epeius runner, which talos can launch (plan 0157), so it is
    accepted at reviewer positions; neither locality nor cn-open refuses it."""
    res = run_fleet_models(fleet_env, "set", "operator/eunomia", position, "local-qwen")
    assert res.returncode == 0, (
        f"Expected local-qwen to be accepted at {position}, got rc={res.returncode}: "
        f"{res.stdout!r} {res.stderr!r}"
    )


@pytest.mark.covers("D1", "D2", "D3", "D5")
@pytest.mark.parametrize("position", NON_REVIEWER_POSITIONS)
def test_local_qwen_not_refused_by_provenance_or_locality_at_non_reviewer_positions(
    fleet_env: dict[str, str], position: str
) -> None:
    """No position refuses local-qwen by provenance class (cn-open) or locality, and
    its full model version qwen3.6:35b-a3b-q8_0 is accepted at non-reviewer positions."""
    res = run_fleet_models(fleet_env, "set", "operator/eunomia", position, "local-qwen")
    combined = f"{res.stdout}\n{res.stderr}"
    assert "cn-open" not in combined
    assert res.returncode == 0, (
        f"Expected local-qwen to be accepted at non-reviewer position {position}, "
        f"got rc={res.returncode}: {combined!r}"
    )


@pytest.mark.covers("D2")
def test_per_repo_models_doc_records_measured_versions() -> None:
    """docs/per-repo-models.md records the measured model id for each runner."""
    assert DOCS_PER_REPO_MODELS.is_file(), "docs/per-repo-models.md must exist"
    text = DOCS_PER_REPO_MODELS.read_text(encoding="utf-8")
    for runner in (
        "sub-sonnet",
        "sub-opus",
        "api-sonnet",
        "sub-codex",
        "sub-antigravity",
        "local-qwen",
    ):
        assert runner in text, f"Expected {runner!r} measurement in docs/per-repo-models.md"
    assert "qwen3.6:35b-a3b-q8_0" in text
