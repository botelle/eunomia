"""Blackbox contract tests for plan 0170 (held state in fleet-stalled and pr_hold in fleet-deps)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_bin_module(script_name: str, module_alias: str):
    bin_dir = str(REPO_ROOT / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    script_path = REPO_ROOT / "bin" / script_name
    loader = importlib.machinery.SourceFileLoader(module_alias, str(script_path))
    spec = importlib.util.spec_from_loader(module_alias, loader)
    assert spec is not None, f"Unable to create module spec for {script_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_alias] = module
    loader.exec_module(module)
    return module


@pytest.mark.covers("D1")
def test_pr_hold_signature_and_context_in_fleet_deps() -> None:
    fleet_deps = _load_bin_module("fleet-deps", "_lane_0170_fleet_deps")
    assert getattr(fleet_deps, "CONTEXT", None) == "eunomia/deps"
    pr_hold = getattr(fleet_deps, "pr_hold", None)
    assert callable(pr_hold), "bin/fleet-deps must export callable pr_hold(forge, repo, sha)"
    sig = inspect.signature(pr_hold)
    params = list(sig.parameters.keys())
    assert params[:3] == ["forge", "repo", "sha"]


@pytest.mark.covers("D1")
def test_fleet_deps_refuses_unknown_subcommand_with_exit_2() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "fleet-deps"), "no-such-subcommand"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


@pytest.mark.covers("D2")
def test_fleet_stalled_states_vocabulary_includes_held() -> None:
    fleet_stalled = _load_bin_module("fleet-stalled", "_lane_0170_fleet_stalled")
    states = getattr(fleet_stalled, "STATES", None)
    assert states is not None, "bin/fleet-stalled must expose STATES vocabulary"
    assert "held" in states
    assert {"red", "stale-base", "stale-review", "unreviewed", "held"}.issubset(set(states))


@pytest.mark.covers("D2")
def test_fleet_stalled_cli_contract_flags_and_malformed_args() -> None:
    help_proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "fleet-stalled"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0
    for flag in ("--dry-run", "--conf", "--repos-conf"):
        assert flag in help_proc.stdout

    bad_proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "fleet-stalled"), "--unknown-flag-0170"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_proc.returncode == 2


@pytest.mark.covers("D3")
def test_docs_stalled_documents_held_state_and_boundaries() -> None:
    doc_path = REPO_ROOT / "docs" / "stalled.md"
    assert doc_path.is_file(), "docs/stalled.md must exist"
    content = doc_path.read_text(encoding="utf-8")
    assert "held" in content
    assert "plan" in content.lower()
    assert "HOLD" in content


@pytest.mark.covers("D4", "DoD1")
def test_plan_test_suites_exist_and_pass() -> None:
    pr_hold_tests = REPO_ROOT / "tests" / "test_pr_hold.py"
    stalled_tests = REPO_ROOT / "tests" / "test_stalled.py"
    assert pr_hold_tests.is_file(), "tests/test_pr_hold.py must exist"
    assert stalled_tests.is_file(), "tests/test_stalled.py must exist"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(pr_hold_tests),
            str(stalled_tests),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"pytest failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
