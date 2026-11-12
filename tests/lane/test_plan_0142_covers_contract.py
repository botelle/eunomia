from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_test_entry_schemas(node: object) -> list[dict]:
    """Recursively locate schema objects that describe a per-test findings entry."""
    found: list[dict] = []
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and "outcome" in props and (
            "reason" in props or "covers" in props or "source" in props
        ):
            found.append(node)
        for value in node.values():
            found.extend(_find_test_entry_schemas(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_test_entry_schemas(item))
    return found


@pytest.mark.covers("D1", "D2", "D3")
def test_fleet_lane_and_fleet_meet_cli_surface() -> None:
    fleet_lane = REPO_ROOT / "bin" / "fleet-lane"
    fleet_meet = REPO_ROOT / "bin" / "fleet-meet"
    assert fleet_lane.is_file()
    assert fleet_meet.is_file()
    assert os.access(fleet_lane, os.X_OK)
    assert os.access(fleet_meet, os.X_OK)

    lane_help = subprocess.run(
        [sys.executable, str(fleet_lane), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert lane_help.returncode == 0, lane_help.stderr
    for token in ("repo", "plan_id", "--sha", "--runner", "--schema", "--dry-run", "--lease"):
        assert token in lane_help.stdout

    lane_bad = subprocess.run(
        [sys.executable, str(fleet_lane)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert lane_bad.returncode == 2

    meet_help = subprocess.run(
        [sys.executable, str(fleet_meet), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert meet_help.returncode == 0, meet_help.stderr
    for token in ("repo", "plan_id", "--impl", "--lane", "--dry-run"):
        assert token in meet_help.stdout

    meet_bad = subprocess.run(
        [sys.executable, str(fleet_meet), "operator/eunomia", "0142", "--impl", "not-an-int"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert meet_bad.returncode == 2


@pytest.mark.covers("D3")
def test_meeting_findings_schema_covers_fields_optional() -> None:
    schema_path = REPO_ROOT / "config" / "meeting-findings.schema.json"
    assert schema_path.is_file(), f"Missing schema file: {schema_path}"

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    candidates = _find_test_entry_schemas(schema)
    assert candidates, "Could not locate test entry definition in meeting-findings.schema.json"

    matched = [
        entry
        for entry in candidates
        if "covers" in entry.get("properties", {})
        and "covers_unknown" in entry.get("properties", {})
    ]
    assert matched, (
        "Expected test entry schema in config/meeting-findings.schema.json to define "
        "both 'covers' and 'covers_unknown' properties"
    )

    for entry in matched:
        props = entry["properties"]
        for field_name in ("covers", "covers_unknown"):
            field_schema = props[field_name]
            assert field_schema.get("type") == "array", (
                f"{field_name} must have type 'array', got {field_schema!r}"
            )
            items_schema = field_schema.get("items", {})
            assert items_schema.get("type") == "string", (
                f"{field_name} items must have type 'string', got {items_schema!r}"
            )

        required = entry.get("required", [])
        assert "covers" not in required, "'covers' must be optional in meeting-findings.schema.json"
        assert "covers_unknown" not in required, (
            "'covers_unknown' must be optional in meeting-findings.schema.json"
        )


@pytest.mark.covers("D5")
def test_docs_test_lane_documents_ids_marker_and_findings_fields() -> None:
    doc_path = REPO_ROOT / "docs" / "test-lane.md"
    assert doc_path.is_file(), f"Missing documentation file: {doc_path}"
    text = doc_path.read_text(encoding="utf-8")

    assert "covers" in text
    assert "covers_unknown" in text
    assert "pytest.mark.covers" in text or "mark.covers" in text
    assert "DoD" in text


@pytest.mark.covers("D2")
def test_lane_result_schema_not_changed_to_carry_covers() -> None:
    fleet_lane_src = (REPO_ROOT / "bin" / "fleet-lane").read_text(encoding="utf-8")
    assert "@pytest.mark.covers" in fleet_lane_src

    for candidate in (REPO_ROOT / "config").glob("*lane*schema*.json"):
        data = json.loads(candidate.read_text(encoding="utf-8"))
        top_props = data.get("properties", {})
        if {"files", "gaps", "notes"}.issubset(top_props.keys()):
            assert "covers" not in top_props
            file_items = (
                top_props.get("files", {})
                .get("items", {})
                .get("properties", {})
            )
            assert "covers" not in file_items


@pytest.mark.covers("D1", "D2", "D3", "D4", "DoD1")
def test_lane_covers_test_module_passes_via_pytest(tmp_path: Path) -> None:
    target_test = REPO_ROOT / "tests" / "test_lane_covers.py"
    assert target_test.is_file(), f"Expected deliverable test file {target_test} to exist"

    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)

    cache_dir = tmp_path / "pytest_cache"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            f"cache_dir={cache_dir}",
            str(target_test),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "PytestUnknownMarkWarning" not in proc.stdout
    assert "PytestUnknownMarkWarning" not in proc.stderr


@pytest.mark.skip(reason="asserts on the private fleet's plan history, which the public cut omits")
def test_untagged_lane_test_is_permitted_and_plan_file_valid() -> None:
    """Intentionally carries no @pytest.mark.covers marker (Boundary 1 & D2)."""
    plan_path = REPO_ROOT / "plans" / "0142-a-lane-test-says-what-it-covers.md"
    assert plan_path.is_file()
    content = plan_path.read_text(encoding="utf-8")
    for heading in ("### D1.", "### D2.", "### D3.", "### D4.", "### D5."):
        assert heading in content
