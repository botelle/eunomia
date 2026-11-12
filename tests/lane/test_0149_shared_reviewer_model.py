from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers("D1")
def test_mopsus_cli_contract_surface() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "mopsus"), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    for token in ("handle", "--conf", "--db", "--dry-run", "--format", "text", "json"):
        assert token in proc.stdout


@pytest.mark.covers()
def test_mopsus_bad_invocation_exits_2() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "mopsus"), "--format", "invalid"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


@pytest.mark.covers("D2")
def test_fleet_meet_cli_contract_surface() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "fleet-meet"), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    for token in ("repo", "plan_id", "--impl", "--lane", "--dry-run"):
        assert token in proc.stdout


@pytest.mark.covers()
def test_fleet_meet_bad_invocation_exits_2() -> None:
    missing_required = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "fleet-meet")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing_required.returncode == 2

    bad_impl_type = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "bin" / "fleet-meet"),
            "operator/eunomia",
            "0149-two-reviewers-may-share-a-model",
            "--impl",
            "not-an-int",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_impl_type.returncode == 2


@pytest.mark.covers("D2", "DoD2")
def test_no_same_model_reviewer_refusal_in_bin() -> None:
    proc = subprocess.run(
        [
            "git",
            "grep",
            "-n",
            "-i",
            "same model",
            "--",
            "bin/fleet-meet",
            "bin/mopsus",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, f"Unexpected 'same model' match: {proc.stdout}"
    assert proc.stdout == ""


@pytest.mark.covers("D3")
def test_docs_change_management_c3_names_both_reviewer_accounts() -> None:
    doc_path = REPO_ROOT / "docs" / "change-management.md"
    normalized = " ".join(doc_path.read_text(encoding="utf-8").split())
    assert "approved at its head by both reviewer accounts" in normalized
    assert "approved at its head by two reviewers on different models" not in normalized


@pytest.mark.covers("D1", "D3")
def test_docs_mopsus_describes_no_verdict_account_settling() -> None:
    doc_path = REPO_ROOT / "docs" / "mopsus.md"
    normalized = " ".join(doc_path.read_text(encoding="utf-8").split()).lower()
    assert "no-verdict" in normalized
    assert "account" in normalized
