from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MOPSUS_BIN = REPO_ROOT / "bin" / "mopsus"
MIGRATE_BIN = REPO_ROOT / "bin" / "fleet-db-migrate"


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, *argv],
        cwd=REPO_ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.covers("D3")
def test_mopsus_docs_cite_adr_0028_settling_rule() -> None:
    doc_path = REPO_ROOT / "docs" / "mopsus.md"
    assert doc_path.is_file(), "docs/mopsus.md must exist"
    text = doc_path.read_text(encoding="utf-8")
    assert "0028" in text, "docs/mopsus.md must cite ADR-0028"
    assert "quota" in text
    assert "no-verdict" in text


@pytest.mark.covers("D4")
def test_mopsus_quota_settle_test_file_exists_and_parses() -> None:
    test_path = REPO_ROOT / "tests" / "test_mopsus_quota_settle.py"
    assert test_path.is_file(), "tests/test_mopsus_quota_settle.py must exist"
    source = test_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(test_path))
    test_funcs = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    assert len(test_funcs) >= 1, "expected test functions in test_mopsus_quota_settle.py"


@pytest.mark.covers()
def test_mopsus_cli_surface_and_declared_exit_codes(tmp_path: Path) -> None:
    db_path = tmp_path / "fleet.db"
    mig = _run([str(MIGRATE_BIN)], env={"FLEET_DB": str(db_path)})
    assert mig.returncode == 0, mig.stderr

    help_proc = _run([str(MOPSUS_BIN), "--help"])
    assert help_proc.returncode == 0
    for flag in ("--conf", "--db", "--dry-run", "--format"):
        assert flag in help_proc.stdout

    bad_flag = _run([str(MOPSUS_BIN), "--format", "invalid"])
    assert bad_flag.returncode == 2

    list_proc = _run([str(MOPSUS_BIN), "list"])
    assert list_proc.returncode == 0

    unknown_handle = _run(
        [
            str(MOPSUS_BIN),
            "--db",
            str(db_path),
            "--format",
            "json",
            "nonexistent-handle-0168",
        ]
    )
    assert unknown_handle.returncode == 1
