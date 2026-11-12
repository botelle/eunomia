from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _emit_plan_question(
    env: dict[str, str],
    detail: dict[str, object],
    *,
    with_lease: bool,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "bin" / "fleet-emit"),
        "plan-question",
        "--repo",
        "operator/eunomia",
        "--session",
        "test-session-0161",
        "--detail-json",
        json.dumps(detail),
    ]
    if with_lease:
        cmd.extend(["--lease", "lease-0161"])
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.covers("D1")
def test_implementer_preamble_documents_plan_question_block() -> None:
    preamble_path = REPO_ROOT / "bin" / "implementer-preamble.md"
    assert preamble_path.is_file(), "bin/implementer-preamble.md must exist"
    text = preamble_path.read_text(encoding="utf-8")

    assert "When the plan does not decide" in text
    assert "plan-question" in text
    for field in ("section", "question", "why", "blocking", "options"):
        assert f'"{field}"' in text or f"`{field}`" in text


@pytest.mark.covers("D3", "DoD6")
def test_fleet_events_accepts_plan_question_type(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["EUNOMIA_FLEET_DIR"] = str(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "bin" / "fleet-events"),
            "--type",
            "plan-question",
            "--json",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"fleet-events rejected --type plan-question (rc={proc.returncode}): "
        f"{proc.stderr}"
    )


@pytest.mark.covers("D3", "DoD6")
def test_fleet_emit_and_fleet_events_roundtrip_plan_question(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["EUNOMIA_FLEET_DIR"] = str(tmp_path)
    env["EUNOMIA_SESSION"] = "test-session-0161"

    detail = {
        "plan": "0161-an-implementer-may-stop-on-a-question",
        "question_id": "q-lease-0161-1",
        "section": "2. Deliverables",
        "question": "Which exit code should stop-on-question return?",
        "blocking": True,
        "where": {"pr": 42},
    }

    proc = _emit_plan_question(env, detail, with_lease=True)
    if proc.returncode != 0:
        proc = _emit_plan_question(env, detail, with_lease=False)

    assert proc.returncode == 0, (
        f"fleet-emit failed to emit plan-question (rc={proc.returncode}): "
        f"{proc.stderr}"
    )
    record = json.loads(proc.stdout.strip())
    assert record["type"] == "plan-question"
    assert record["repo"] == "operator/eunomia"
    assert record["detail"] == detail

    events_proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "bin" / "fleet-events"),
            "--type",
            "plan-question",
            "--json",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert events_proc.returncode == 0, events_proc.stderr
    lines = [line for line in events_proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    read_back = json.loads(lines[0])
    assert read_back["type"] == "plan-question"
    assert read_back["detail"] == detail


@pytest.mark.covers("D3", "DoD6")
def test_event_type_sync_passes_with_plan_question() -> None:
    spec_text = (REPO_ROOT / "SPEC.md").read_text(encoding="utf-8")
    assert "plan-question" in spec_text

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(REPO_ROOT / "tests" / "test_event_type_sync.py"),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
