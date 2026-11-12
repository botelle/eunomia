"""Blackbox integration tests for plan 0138:
An autonomous repository's lane is read by its two reviewers.

Tests invoke the CLI entry points declared in the generated contract and
inspect the schema and module docstring surfaces named in the plan.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_MEET = REPO_ROOT / "bin" / "fleet-meet"
FLEET_CONFIG = REPO_ROOT / "bin" / "fleet-config"
FLEET_MODELS = REPO_ROOT / "bin" / "fleet-models"
FLEET_DB_MIGRATE = REPO_ROOT / "bin" / "fleet-db-migrate"
MEETING_FINDINGS_SCHEMA = REPO_ROOT / "config" / "meeting-findings.schema.json"


def _run_cli(
    args: list[str],
    *,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _find_finding_item_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Locate the per-finding / per-lane object subschema in meeting-findings.schema.json."""
    candidates: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and (
                "independent_reader" in props or "read_by" in props
            ):
                candidates.append(node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    assert candidates, (
        "Could not locate finding item subschema (with 'independent_reader' / 'read_by') "
        "in config/meeting-findings.schema.json"
    )
    return candidates[0]


def _collect_comments(node: Any) -> list[str]:
    """Collect all $comment strings anywhere in a JSON schema object."""
    comments: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("$comment"), str):
            comments.append(node["$comment"])
        for value in node.values():
            comments.extend(_collect_comments(value))
    elif isinstance(node, list):
        for item in node:
            comments.extend(_collect_comments(item))
    return comments


# ---------------------------------------------------------------------------
# 1. bin/fleet-meet CLI contract tests
# ---------------------------------------------------------------------------


def test_fleet_meet_help_surface() -> None:
    res = _run_cli([str(FLEET_MEET), "--help"])
    assert res.returncode == 0, f"--help failed: {res.stderr}"
    assert "repo" in res.stdout
    assert "plan_id" in res.stdout
    assert "--impl" in res.stdout
    assert "--lane" in res.stdout
    assert "--dry-run" in res.stdout


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["operator/eunomia"],
        ["operator/eunomia", "0138"],  # missing required --impl
        ["operator/eunomia", "0138", "--impl", "not-an-int"],
        ["operator/eunomia", "0138", "--impl", "1", "--lane", "not-an-int"],
        ["operator/eunomia", "0138", "--impl", "1", "--unknown-flag"],
    ],
)
def test_fleet_meet_argparse_rejects_malformed_arguments(argv: list[str]) -> None:
    res = _run_cli([str(FLEET_MEET), *argv])
    assert res.returncode == 2, (
        f"Expected argparse exit code 2 for argv={argv}, got {res.returncode}\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )


def test_fleet_meet_module_docstring_names_autonomous_and_critical_approvals() -> None:
    """D1: fleet-meet's module docstring says which approval lets a lane run
    on each kind of repository, not only the human's.
    """
    source = FLEET_MEET.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(FLEET_MEET))
    docstring = ast.get_docstring(tree)
    assert docstring is not None, "bin/fleet-meet must have a module docstring"
    lower = docstring.lower()
    assert "autonomous" in lower, (
        "bin/fleet-meet module docstring must describe approval on an autonomous repository"
    )
    assert "reviewer" in lower, (
        "bin/fleet-meet module docstring must mention reviewers' approval"
    )


# ---------------------------------------------------------------------------
# 2. config/meeting-findings.schema.json contract tests (Deliverable D2)
# ---------------------------------------------------------------------------


def test_meeting_findings_schema_declares_optional_reader_and_review_ids() -> None:
    """D2: Each finding gains two optional properties (not in the schema's
    required list): reader ('human' or 'reviewers') and review_ids.
    """
    assert MEETING_FINDINGS_SCHEMA.is_file(), (
        f"Missing schema file: {MEETING_FINDINGS_SCHEMA}"
    )
    schema = json.loads(MEETING_FINDINGS_SCHEMA.read_text(encoding="utf-8"))
    finding_schema = _find_finding_item_schema(schema)
    props = finding_schema["properties"]
    required = set(finding_schema.get("required", []))

    assert "reader" in props, "finding schema must define 'reader' property"
    assert "review_ids" in props, "finding schema must define 'review_ids' property"

    assert "reader" not in required, (
        "'reader' must be optional (not in 'required') so pre-0138 documents still validate"
    )
    assert "review_ids" not in required, (
        "'review_ids' must be optional (not in 'required') so pre-0138 documents still validate"
    )

    reader_prop = props["reader"]
    assert set(reader_prop.get("enum", [])) == {"human", "reviewers"}, (
        f"Expected reader enum to be {{'human', 'reviewers'}}, got {reader_prop}"
    )

    review_ids_prop = props["review_ids"]
    assert review_ids_prop.get("type") == "array", (
        f"Expected review_ids type 'array', got {review_ids_prop}"
    )
    items_schema = review_ids_prop.get("items", {})
    assert items_schema.get("type") == "integer", (
        f"Expected review_ids items type 'integer', got {items_schema}"
    )


def test_meeting_findings_schema_keeps_independent_reader_false_with_comment() -> None:
    """D2 & Boundaries: independent_reader stays false for both human and
    reviewers; the schema's $comment says so and its enum is unchanged.
    """
    schema = json.loads(MEETING_FINDINGS_SCHEMA.read_text(encoding="utf-8"))
    finding_schema = _find_finding_item_schema(schema)
    props = finding_schema["properties"]

    indep_prop = props["independent_reader"]
    assert indep_prop.get("enum") == [False], (
        f"independent_reader enum must stay [false], got {indep_prop}"
    )

    all_comments = " ".join(_collect_comments(schema)).lower()
    assert "reviewer" in all_comments or "autonomous" in all_comments, (
        "Schema $comment must explain why independent_reader stays false for reviewers "
        "on an autonomous repository"
    )


# ---------------------------------------------------------------------------
# 3. bin/fleet-config autonomy & bin/fleet-models reviewers contract tests
# ---------------------------------------------------------------------------


def test_fleet_config_autonomy_read_and_write(tmp_path: Path) -> None:
    """D1: fleet-meet reads the lane's repository's autonomy through fleet-config."""
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "fleet.db"
    env = {
        "EUNOMIA_FLEET_DIR": str(fleet_dir),
        "FLEET_DB": str(db_path),
        "EUNOMIA_SESSION": "test-session-0138",
        "USER": "tester",
    }

    mig = _run_cli([str(FLEET_DB_MIGRATE)], env_overrides=env)
    assert mig.returncode == 0, f"fleet-db-migrate failed: {mig.stderr}"

    get_default = _run_cli(
        [str(FLEET_CONFIG), "get", "autonomy", "--repo", "operator/eunomia", "--json"],
        env_overrides=env,
    )
    assert get_default.returncode == 0, f"fleet-config get failed: {get_default.stderr}"
    payload_default = json.loads(get_default.stdout)
    assert payload_default["key"] == "autonomy"
    assert payload_default["value"] == "critical"

    set_auto = _run_cli(
        [
            str(FLEET_CONFIG),
            "set",
            "autonomy",
            "autonomous",
            "--repo",
            "operator/eunomia",
        ],
        env_overrides=env,
    )
    assert set_auto.returncode == 0, f"fleet-config set failed: {set_auto.stderr}"

    get_auto = _run_cli(
        [str(FLEET_CONFIG), "get", "autonomy", "--repo", "operator/eunomia", "--json"],
        env_overrides=env,
    )
    assert get_auto.returncode == 0, f"fleet-config get failed: {get_auto.stderr}"
    payload_auto = json.loads(get_auto.stdout)
    assert payload_auto["value"] == "autonomous"

    set_bad = _run_cli(
        [
            str(FLEET_CONFIG),
            "set",
            "autonomy",
            "not-a-valid-autonomy",
            "--repo",
            "operator/eunomia",
        ],
        env_overrides=env,
    )
    assert set_bad.returncode == 1


def test_fleet_models_reviewers_cli_contract(tmp_path: Path) -> None:
    """D1: expected_reviewers(repo, 'code_reviewer') relies on fleet-models reviewers."""
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "fleet.db"
    env = {
        "EUNOMIA_FLEET_DIR": str(fleet_dir),
        "FLEET_DB": str(db_path),
        "EUNOMIA_SESSION": "test-session-0138",
        "USER": "tester",
    }

    mig = _run_cli([str(FLEET_DB_MIGRATE)], env_overrides=env)
    assert mig.returncode == 0, f"fleet-db-migrate failed: {mig.stderr}"

    # Missing required --json flag must exit 2
    no_json = _run_cli(
        [str(FLEET_MODELS), "reviewers", "operator/eunomia", "code_reviewer"],
        env_overrides=env,
    )
    assert no_json.returncode == 2

    # Invalid position outside {plan_reviewer, code_reviewer} must exit 2
    bad_pos = _run_cli(
        [str(FLEET_MODELS), "reviewers", "operator/eunomia", "implementer", "--json"],
        env_overrides=env,
    )
    assert bad_pos.returncode == 2
