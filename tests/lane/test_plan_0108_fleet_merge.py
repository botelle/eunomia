from __future__ import annotations

import ast
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_fleet_merge_executable_and_valid_python() -> None:
    """Plan 0108 D1 & CI harness ('the bin/ tools parse'): bin/fleet-merge exists, is executable, and parses."""
    fleet_merge = REPO_ROOT / "bin" / "fleet-merge"
    assert fleet_merge.is_file(), "bin/fleet-merge must exist"
    assert os.access(fleet_merge, os.X_OK), "bin/fleet-merge must be executable"
    source = fleet_merge.read_text(encoding="utf-8")
    ast.parse(source, filename=str(fleet_merge))


def test_fleet_merge_launchd_plist() -> None:
    """Plan 0108 D4 & §4 DoD: launchd/org.eunomia.fleet-merge.plist runs every 60s and passes plutil -lint."""
    plist_path = REPO_ROOT / "launchd" / "org.eunomia.fleet-merge.plist"
    assert plist_path.is_file(), "launchd/org.eunomia.fleet-merge.plist must exist"

    with plist_path.open("rb") as fh:
        data = plistlib.load(fh)

    assert data.get("Label") == "org.eunomia.fleet-merge"
    assert data.get("StartInterval") == 60

    prog_args = data.get("ProgramArguments")
    assert isinstance(prog_args, list) and len(prog_args) >= 2
    assert any("fleet-merge" in str(arg) for arg in prog_args)
    assert "once" in prog_args

    plutil = shutil.which("plutil")
    if plutil is not None:
        proc = subprocess.run(
            [plutil, "-lint", str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"plutil -lint failed: {proc.stdout} {proc.stderr}"


def test_fleet_merge_docs_present() -> None:
    """Plan 0108 D5: docs/fleet-merge.md covers autonomous/critical repos, checks, and critical repos."""
    doc_path = REPO_ROOT / "docs" / "fleet-merge.md"
    assert doc_path.is_file(), "docs/fleet-merge.md must exist"
    text = doc_path.read_text(encoding="utf-8")
    for required_term in (
        "autonomous",
        "critical",
        "mergebot",
        "eunomia/deps",
        "tests/lane",
        "eunomia",
        "talos",
        "hosttools",
    ):
        assert required_term in text, f"docs/fleet-merge.md missing required topic: {required_term!r}"


def test_fleet_bundle_reports_fleet_merge_and_enforcing_vocabulary() -> None:
    """Contract bin/fleet-bundle: introspects repo and surfaces bin/fleet-merge, its ENFORCING vocabulary, and unit test file."""
    fleet_bundle = REPO_ROOT / "bin" / "fleet-bundle"
    proc = subprocess.run(
        [sys.executable, str(fleet_bundle), str(REPO_ROOT), "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"fleet-bundle failed: {proc.stderr}"
    bundle = json.loads(proc.stdout)

    executables = bundle.get("harness", {}).get("executables", [])
    assert "bin/fleet-merge" in executables

    test_files = bundle.get("harness", {}).get("test_files", [])
    assert "tests/test_fleet_merge.py" in test_files

    vocabs = [
        v
        for v in bundle.get("vocabularies", [])
        if v.get("source") == "bin/fleet-merge" and v.get("name") == "ENFORCING"
    ]
    assert len(vocabs) == 1, "bin/fleet-merge must declare module-level constant ENFORCING"
    enforcing_values = set(vocabs[0].get("values", []))
    expected_enforcing = {
        "bin/fleet-merge",
        "bin/fleet-deps",
        "bin/fleet-watch",
        "bin/fleetlib.py",
        "bin/fleetforge.py",
        "bin/fleet-config",
        "bin/fleet-install-hooks",
        "bin/fleet-secret-guard",
        "config/secret-patterns.conf",
        "launchd/org.eunomia.fleet-merge.plist",
    }
    missing = expected_enforcing - enforcing_values
    assert not missing, f"ENFORCING in bin/fleet-merge is missing required paths: {sorted(missing)}"
    launchd_entries = [p for p in enforcing_values if p.startswith("launchd/")]
    assert len(launchd_entries) >= 2, (
        f"D1.4 requires both launchd units in ENFORCING; found: {launchd_entries}"
    )
