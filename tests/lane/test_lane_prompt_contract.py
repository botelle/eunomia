import pytest
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_LANE = REPO_ROOT / "bin" / "fleet-lane"
FLEET_PLAN = REPO_ROOT / "bin" / "fleet-plan"
TEST_LANE_DOC = REPO_ROOT / "docs" / "test-lane.md"
PLAN_FILE = REPO_ROOT / "plans" / "0141-a-lane-prompt-carries-what-the-plan-touches.md"


def test_fleet_lane_cli_help_exposes_contracted_arguments_and_options():
    proc = subprocess.run(
        [sys.executable, str(FLEET_LANE), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for token in ("repo", "plan_id", "--sha", "--runner", "--schema", "--dry-run", "--lease"):
        assert token in out, f"expected {token!r} in fleet-lane --help output"


def test_fleet_lane_requires_sha_option():
    proc = subprocess.run(
        [sys.executable, str(FLEET_LANE), "operator/eunomia", "0141"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_test_lane_doc_records_runner_measurements_budget_and_refusal():
    assert TEST_LANE_DOC.is_file(), "docs/test-lane.md must exist"
    text = TEST_LANE_DOC.read_text(encoding="utf-8")
    for runner in ("sub-antigravity", "sub-codex", "sub-opus"):
        assert runner in text, f"docs/test-lane.md must record measurement for {runner}"
    assert "PROMPT_BUDGET" in text, "docs/test-lane.md must record PROMPT_BUDGET"
    assert "refused: the plan's own contract is" in text, (
        "docs/test-lane.md must document the over-budget contract refusal"
    )


@pytest.mark.skip(reason="asserts on the private fleet's plan history, which the public cut omits")
def test_plan_0141_passes_fleet_plan_lint():
    assert PLAN_FILE.is_file(), f"expected plan file at {PLAN_FILE}"
    proc = subprocess.run(
        [sys.executable, str(FLEET_PLAN), "lint", str(PLAN_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"fleet-plan lint failed: {proc.stdout}\n{proc.stderr}"
