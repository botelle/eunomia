import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_fleet_bundle() -> dict:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "fleet-bundle"), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"fleet-bundle exited {proc.returncode}: stderr={proc.stderr!r}"
    )
    return json.loads(proc.stdout)


@pytest.mark.covers("D1")
def test_orchestrator_provenance_and_open_classes_vocabulary():
    bundle = _run_fleet_bundle()
    vocabs = {
        (entry["source"], entry["name"]): set(entry["values"])
        for entry in bundle.get("vocabularies", [])
    }

    assert ("bin/orchestrator", "PROVENANCE_CLASSES") in vocabs
    assert vocabs[("bin/orchestrator", "PROVENANCE_CLASSES")] == {
        "ca-open",
        "cn-hosted",
        "cn-open",
        "eu-open",
        "kr-open",
        "routed-hosted",
        "us-hosted",
        "us-open",
    }

    assert ("bin/orchestrator", "OPEN_CLASSES") in vocabs
    assert vocabs[("bin/orchestrator", "OPEN_CLASSES")] == {
        "ca-open",
        "cn-open",
        "eu-open",
        "kr-open",
        "us-open",
    }


@pytest.mark.covers()
def test_orchestrator_rejects_missing_positional_args_and_writes_no_stdout(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "orchestrator")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr != ""


@pytest.mark.covers()
def test_orchestrator_rejects_missing_session_identity_and_writes_no_stdout(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "bin" / "orchestrator"),
            "operator/eunomia",
            "plans/0154-local-runners-through-epeius.md",
            "lease-0000",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr != ""
