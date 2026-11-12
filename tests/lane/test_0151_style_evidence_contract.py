import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_BIN = REPO_ROOT / "bin" / "fleet-style-evidence"


@pytest.mark.covers("D1")
def test_fleet_style_evidence_executable_exists():
    """D1 delivers bin/fleet-style-evidence as an executable."""
    assert EVIDENCE_BIN.is_file(), f"Missing executable {EVIDENCE_BIN}"


@pytest.mark.covers("D1")
def test_produce_cli_accepts_documented_arguments():
    """D1 specifies: fleet-style-evidence produce <repo> <pr> [--plan <path>] --worktree <dir> --base <sha> --head <sha>."""
    if not EVIDENCE_BIN.is_file():
        pytest.skip("bin/fleet-style-evidence does not exist yet")

    proc = subprocess.run(
        [sys.executable, str(EVIDENCE_BIN), "produce", "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"produce --help failed: {proc.stderr}"
    out = proc.stdout
    for expected in ("repo", "pr", "--plan", "--worktree", "--base", "--head"):
        assert expected in out, f"Expected '{expected}' in `produce --help` output:\n{out}"


@pytest.mark.covers("D1")
def test_produce_cli_rejects_missing_required_flags():
    """Calling `produce` without required flags (--worktree, --base, --head) exits non-zero."""
    if not EVIDENCE_BIN.is_file():
        pytest.skip("bin/fleet-style-evidence does not exist yet")

    proc = subprocess.run(
        [sys.executable, str(EVIDENCE_BIN), "produce", "operator/undeclared-repo", "1"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


@pytest.mark.covers("D1")
def test_produce_undeclared_repository_refuses_and_names_step(tmp_path):
    """D1 step 1: reads declaration with fleet-style declared, refuses an undeclared repository, exits non-zero and names the step."""
    if not EVIDENCE_BIN.is_file():
        pytest.skip("bin/fleet-style-evidence does not exist yet")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    base_sha = "0" * 40
    head_sha = "1" * 40

    proc = subprocess.run(
        [
            sys.executable,
            str(EVIDENCE_BIN),
            "produce",
            "operator/definitely-undeclared-style-repo",
            "42",
            "--worktree",
            str(worktree),
            "--base",
            base_sha,
            "--head",
            head_sha,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, "Expected non-zero exit for undeclared repository"
    combined = (proc.stdout + "\n" + proc.stderr).lower()
    assert any(
        token in combined
        for token in ("step 1", "step1", "declared", "undeclared", "1")
    ), f"Expected failure output to name step 1 / declared check, got:\n{proc.stdout}\n{proc.stderr}"


@pytest.mark.covers("D3", "DoD1")
def test_deliverable_test_files_exist():
    """D3 and DoD1 require tests/test_fleet_style_evidence.py and tests/test_orchestrator_style_review.py."""
    t1 = REPO_ROOT / "tests" / "test_fleet_style_evidence.py"
    t2 = REPO_ROOT / "tests" / "test_orchestrator_style_review.py"
    assert t1.is_file(), f"Missing {t1}"
    assert t2.is_file(), f"Missing {t2}"


@pytest.mark.covers("D4")
def test_style_evidence_doc_exists_and_covers_required_topics():
    """D4 requires docs/style-evidence.md covering argv contract, simulator lifecycle, size rules, hash record, failures, and manual run."""
    doc = REPO_ROOT / "docs" / "style-evidence.md"
    assert doc.is_file(), f"Missing {doc}"
    text = doc.read_text(encoding="utf-8")
    assert len(text.strip()) > 0, "docs/style-evidence.md is empty"
    for term in ("--out", "--udid", "--themes", "--appearances", "max_image_kb", "produce"):
        assert term in text, f"Expected '{term}' to be documented in docs/style-evidence.md"


@pytest.mark.covers("DoD4")
def test_no_forbidden_model_names_in_deliverables():
    """DoD4: git grep -n -i 'fable\\|opus\\|sonnet' bin/fleet-style-evidence docs/style-evidence.md prints nothing."""
    targets = [
        REPO_ROOT / "bin" / "fleet-style-evidence",
        REPO_ROOT / "docs" / "style-evidence.md",
    ]
    missing = [str(p.relative_to(REPO_ROOT)) for p in targets if not p.is_file()]
    if missing:
        pytest.fail(f"Deliverable files missing for DoD4 check: {missing}")

    proc = subprocess.run(
        [
            "git",
            "grep",
            "-n",
            "-i",
            r"fable\|opus\|sonnet",
            "--",
            "bin/fleet-style-evidence",
            "docs/style-evidence.md",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # git grep exits 1 when no lines match, 0 when matches are found
    assert proc.returncode == 1, f"Forbidden terms found:\n{proc.stdout}{proc.stderr}"
    assert proc.stdout == ""
