import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_PLAN = REPO_ROOT / "bin" / "fleet-plan"


def _write_plan(tmp_path: Path, plan_id: str, depends_on: list[str]) -> Path:
    deps_yaml = "\n".join(f'  - "{dep}"' for dep in depends_on)
    content = f"""---
id: {plan_id}
status: draft
repo: operator/eunomia
zone: public
tier: 3
spec_impact: "none: test fixture plan."
paths:
  - "bin/fleet-watch"
depends_on:
{deps_yaml}
---

# Test plan for pin dependencies

## 1. Goal

Verify pin dependency handling in fleet-plan.

## 2. Deliverables

### D1. Deliverable

Test deliverable body.

## 3. Boundaries

- Do not mutate state.

## 4. Definition of done

1. Tests pass.

## 5. Handoff

- None.

## 6. Resources

- `docs/plan-dependencies.md`
"""
    plan_file = tmp_path / f"{plan_id}.md"
    plan_file.write_text(content, encoding="utf-8")
    return plan_file


@pytest.mark.covers("D2")
def test_fleet_plan_lint_accepts_valid_pin_dependency(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        "9991-valid-pin-dep",
        ["pin:operator/eunomia:bin/fleet-watch"],
    )
    proc = subprocess.run(
        [sys.executable, str(FLEET_PLAN), "lint", str(plan_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


@pytest.mark.covers("D1", "D2")
@pytest.mark.parametrize(
    "bad_dep",
    [
        "pin:",
        "pin:not-a-repo-slug:some/file.py",
        "pin:operator/eunomia:",
        "pin:operator/eunomia:/leading/slash.py",
        "pin:operator/eunomia:../traversal.py",
        "pin:operator/eunomia:sub/../traversal.py",
        "pin:bad owner/repo:some/file.py",
    ],
)
def test_fleet_plan_lint_rejects_malformed_pin_dependency(
    tmp_path: Path, bad_dep: str
) -> None:
    plan_file = _write_plan(
        tmp_path,
        "9992-malformed-pin-dep",
        [bad_dep],
    )
    proc = subprocess.run(
        [sys.executable, str(FLEET_PLAN), "lint", str(plan_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, (
        f"Expected exit 1 for {bad_dep!r}, got {proc.returncode}: "
        f"stdout={proc.stdout} stderr={proc.stderr}"
    )
    combined = (proc.stdout + proc.stderr).strip()
    assert combined, f"Expected diagnostic message for {bad_dep!r}"


@pytest.mark.covers("D2")
def test_fleet_plan_lint_rejects_unknown_repo_in_pin_dependency(
    tmp_path: Path,
) -> None:
    plan_file = _write_plan(
        tmp_path,
        "9993-unknown-repo-pin-dep",
        ["pin:nonexistent-owner/nonexistent-repo:some/path.py"],
    )
    proc = subprocess.run(
        [sys.executable, str(FLEET_PLAN), "lint", str(plan_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, (
        f"Expected exit 1 for unknown repo, got {proc.returncode}: "
        f"stdout={proc.stdout} stderr={proc.stderr}"
    )
    combined = (proc.stdout + proc.stderr).strip()
    assert combined


@pytest.mark.covers("D2")
def test_fleet_plan_graph_draws_pin_dependency(tmp_path: Path) -> None:
    dep = "pin:operator/eunomia:bin/fleet-watch"
    plan_file = _write_plan(tmp_path, "9994-graph-pin-dep", [dep])

    json_proc = subprocess.run(
        [
            sys.executable,
            str(FLEET_PLAN),
            "graph",
            "--format",
            "json",
            str(plan_file),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert json_proc.returncode == 0, (
        f"stdout={json_proc.stdout}\nstderr={json_proc.stderr}"
    )
    parsed = json.loads(json_proc.stdout)
    assert isinstance(parsed, dict)
    assert "operator/eunomia" in json_proc.stdout
    assert "bin/fleet-watch" in json_proc.stdout

    mermaid_proc = subprocess.run(
        [
            sys.executable,
            str(FLEET_PLAN),
            "graph",
            "--format",
            "mermaid",
            str(plan_file),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mermaid_proc.returncode == 0, (
        f"stdout={mermaid_proc.stdout}\nstderr={mermaid_proc.stderr}"
    )
    assert "bin/fleet-watch" in mermaid_proc.stdout


@pytest.mark.covers("D2", "DoD2")
def test_fleet_plan_lint_passes_on_all_repo_plans() -> None:
    plans_dir = REPO_ROOT / "plans"
    plan_files = sorted(str(p) for p in plans_dir.glob("*.md"))
    assert plan_files, "Expected plan files in plans/"
    proc = subprocess.run(
        [sys.executable, str(FLEET_PLAN), "lint", *plan_files],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


@pytest.mark.covers("D1", "D3")
def test_docs_plan_dependencies_describes_pin_form_and_reasons() -> None:
    doc_path = REPO_ROOT / "docs" / "plan-dependencies.md"
    assert doc_path.is_file(), "docs/plan-dependencies.md must exist"
    text = doc_path.read_text(encoding="utf-8")
    assert "pin:" in text
    assert "path:" in text
    for reason in ("no pin", "pin unreadable", "not on pin"):
        assert reason in text, f"Expected unmet reason {reason!r} in docs/plan-dependencies.md"


@pytest.mark.covers("D4", "DoD1")
def test_unit_test_suite_for_pin_dependencies_passes() -> None:
    test_file = REPO_ROOT / "tests" / "test_plan_dependencies_pin.py"
    assert test_file.is_file(), "tests/test_plan_dependencies_pin.py must exist"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
