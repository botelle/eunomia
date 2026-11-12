"""Blackbox contract tests for Plan 0184: Mopsus non-Claude stop recovery and fleet-quota."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MOPSUS_BIN = REPO_ROOT / "bin" / "mopsus"
FLEET_QUOTA_BIN = REPO_ROOT / "bin" / "fleet-quota"
FLEET_BUNDLE_BIN = REPO_ROOT / "bin" / "fleet-bundle"
FLEET_DB_MIGRATE_BIN = REPO_ROOT / "bin" / "fleet-db-migrate"


def _run(
    argv: list[str],
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_mopsus_and_fleet_quota_executables_exist() -> None:
    assert MOPSUS_BIN.is_file(), f"Missing executable: {MOPSUS_BIN}"
    assert os.access(MOPSUS_BIN, os.X_OK), f"Not executable: {MOPSUS_BIN}"
    assert FLEET_QUOTA_BIN.is_file(), f"Missing executable: {FLEET_QUOTA_BIN}"
    assert os.access(FLEET_QUOTA_BIN, os.X_OK), f"Not executable: {FLEET_QUOTA_BIN}"


def test_mopsus_help_declares_contract_flags() -> None:
    res = _run([str(MOPSUS_BIN), "--help"])
    assert res.returncode == 0, res.stderr
    for token in ("--conf", "--db", "--dry-run", "--format", "handle"):
        assert token in res.stdout, f"Expected {token!r} in mopsus --help output"


def test_mopsus_rejects_invalid_format_choice() -> None:
    res = _run([str(MOPSUS_BIN), "--format", "invalid-format"])
    assert res.returncode == 2
    assert "invalid choice" in res.stderr.lower() or "usage:" in res.stderr.lower()


def test_mopsus_rejects_unknown_option() -> None:
    res = _run([str(MOPSUS_BIN), "--no-such-flag-0184"])
    assert res.returncode == 2
    assert "unrecognized arguments" in res.stderr.lower() or "usage:" in res.stderr.lower()


def test_fleet_quota_help_declares_subcommands() -> None:
    res = _run([str(FLEET_QUOTA_BIN), "--help"])
    assert res.returncode == 0, res.stderr
    for subcmd in ("observe", "close", "open", "probe-review-path", "check", "status"):
        assert subcmd in res.stdout, f"Expected subcommand {subcmd!r} in fleet-quota --help"


@pytest.mark.parametrize(
    ("subcmd", "expected_tokens"),
    [
        ("observe", []),
        ("close", ["scope", "--resets-at", "--by", "--evidence", "--at"]),
        ("open", ["scope", "--by", "--for"]),
        ("probe-review-path", ["repo"]),
        ("check", ["--scope"]),
        ("status", ["--json"]),
    ],
)
def test_fleet_quota_subcommand_help_surfaces(
    subcmd: str,
    expected_tokens: list[str],
) -> None:
    res = _run([str(FLEET_QUOTA_BIN), subcmd, "--help"])
    assert res.returncode == 0, res.stderr
    for token in expected_tokens:
        assert token in res.stdout, (
            f"Expected {token!r} in `fleet-quota {subcmd} --help` output"
        )


def test_fleet_quota_close_argument_validation() -> None:
    missing_flags = _run([str(FLEET_QUOTA_BIN), "close", "session"])
    assert missing_flags.returncode == 2
    assert "required" in missing_flags.stderr.lower() or "usage:" in missing_flags.stderr.lower()

    invalid_by = _run(
        [
            str(FLEET_QUOTA_BIN),
            "close",
            "session",
            "--resets-at",
            "2026-11-03T00:00:00Z",
            "--by",
            "owner",
            "--evidence",
            "quota limit reached",
        ]
    )
    assert invalid_by.returncode == 2
    assert "invalid choice" in invalid_by.stderr.lower()


def test_fleet_quota_open_argument_validation() -> None:
    missing_by = _run([str(FLEET_QUOTA_BIN), "open", "session"])
    assert missing_by.returncode == 2
    assert "required" in missing_by.stderr.lower() or "usage:" in missing_by.stderr.lower()

    invalid_by = _run([str(FLEET_QUOTA_BIN), "open", "session", "--by", "stop"])
    assert invalid_by.returncode == 2
    assert "invalid choice" in invalid_by.stderr.lower()


def test_fleet_quota_probe_review_path_requires_repo() -> None:
    res = _run([str(FLEET_QUOTA_BIN), "probe-review-path"])
    assert res.returncode == 2
    assert "required" in res.stderr.lower() or "usage:" in res.stderr.lower()


def test_fleet_bundle_declares_mopsus_and_fleet_quota_vocabularies() -> None:
    res = _run([str(FLEET_BUNDLE_BIN), str(REPO_ROOT), "--format", "json"], timeout=60)
    assert res.returncode == 0, res.stderr
    bundle: dict[str, Any] = json.loads(res.stdout)

    clis_by_path: dict[str, dict[str, Any]] = {
        entry["path"]: entry for entry in bundle.get("clis", [])
    }
    assert "bin/mopsus" in clis_by_path
    assert "bin/fleet-quota" in clis_by_path

    vocabs: dict[tuple[str, str], list[str]] = {
        (v["source"], v["name"]): list(v["values"])
        for v in bundle.get("vocabularies", [])
    }

    assert set(vocabs.get(("bin/mopsus", "VALID_MODES"), [])) >= {
        "escalate",
        "mechanical",
        "off",
        "triage",
    }
    assert set(vocabs.get(("bin/mopsus", "BUILT_MODES"), [])) >= {
        "escalate",
        "mechanical",
        "off",
    }
    assert set(vocabs.get(("bin/mopsus", "RETRYABLE_CLASSES"), [])) >= {
        "backend-before-work",
        "transport",
    }
    assert set(vocabs.get(("bin/mopsus", "GATED_CLASSES"), [])) >= {
        "credential",
        "quota",
    }

    assert set(vocabs.get(("bin/fleet-quota", "HOLDING_SCOPES"), [])) >= {
        "credential",
        "session",
        "weekly",
    }
    assert set(vocabs.get(("bin/fleet-quota", "METER_SCOPES"), [])) >= {
        "session",
        "weekly",
    }
    assert set(vocabs.get(("bin/fleet-quota", "VENDOR_ACCOUNTS"), [])) >= {
        "codex",
        "gemini",
        "muse",
    }
    assert "fable" in set(vocabs.get(("bin/fleet-quota", "MODEL_BUCKETS"), []))

    emit_events = set(vocabs.get(("bin/fleet-emit", "EVENT_TYPES"), []))
    assert {
        "quota-closed",
        "quota-opened",
        "session-stopped",
        "seat-access-changed",
        "repo-model-changed",
    } <= emit_events


def test_fleet_db_migrate_creates_fresh_db(tmp_path: Path) -> None:
    db_path = tmp_path / "fleet.db"
    env = dict(os.environ)
    env["FLEET_DB"] = str(db_path)
    res = _run([str(FLEET_DB_MIGRATE_BIN)], env=env)
    assert res.returncode == 0, res.stderr
    assert db_path.is_file()
    assert db_path.stat().st_size > 0
