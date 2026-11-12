#!/usr/bin/env python3
"""Plan 0092: the leak watch and the collector read the reviewer's published
transcripts; a guard-log line pages by path and count only."""
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def _load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _result(text):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": text}]}})


def _watch(tmp_path, monkeypatch, alerts):
    mod = _load("fleet-leak-watch")
    monkeypatch.setattr(mod, "PROJECTS", tmp_path / "projects")
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet"))
    monkeypatch.setattr(mod, "_alert", lambda rel, ln, f: alerts.append((rel, ln, f)))
    monkeypatch.setattr(mod, "_record_page", lambda *a, **k: None)
    return mod


def test_reviewer_root_transcript_alerts_once(tmp_path, monkeypatch):
    root = tmp_path / "published"
    root.mkdir()
    (root / "abc.jsonl").write_text(_result(f"token {PAT}") + "\n")
    monkeypatch.setenv("FLEET_REVIEWER_TRANSCRIPTS", str(root))
    alerts = []
    mod = _watch(tmp_path, monkeypatch, alerts)
    mod.main([])
    assert [a[2] for a in alerts] == ["github-pat"]
    assert alerts[0][0] == "reviewer/abc.jsonl"
    alerts.clear()
    mod.main([])
    assert alerts == []


def test_absent_root_is_one_skip_line_not_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLEET_REVIEWER_TRANSCRIPTS", str(tmp_path / "nope"))
    mod = _watch(tmp_path, monkeypatch, [])
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("absent, skipped") == 1


class _FakeResponse:
    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_guard_log_growth_pages_once_by_count_only(tmp_path, monkeypatch):
    """A grown guard log pages once and dedupes on the next run -- given the
    page was actually DELIVERED. (Plan 0092 §3: the offset only advances once
    the page is sent; a delivery, not merely an attempt, is what earns the
    dedupe here. See test_guard_log_no_channel_configured_does_not_advance_offset
    and test_guard_log_page_failure_does_not_advance_offset below for the two
    undelivered cases, which must NOT dedupe.)"""
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    log = fleet / "secret-guard.log"
    log.write_text("SECRETLINE-ONE failing open\nSECRETLINE-TWO failing open\n")
    pages = []
    mod = _watch(tmp_path, monkeypatch, [])
    monkeypatch.setattr(mod, "NTFY_URL", "http://example.invalid/push")
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())
    monkeypatch.setattr(mod, "_record_page", lambda t, b, d, detail="": pages.append(b))
    mod.main([])
    assert pages == [f"{log}: 2 new line(s)"]
    state = (fleet / "leak-watch.state.json").read_text()
    assert "SECRETLINE" not in state and "SECRETLINE" not in " ".join(pages)
    pages.clear()
    mod.main([])
    assert pages == []
    with log.open("a") as fh:
        fh.write("SECRETLINE-THREE\n")
    mod.main([])
    assert pages == [f"{log}: 1 new line(s)"]


def test_guard_log_dry_run_pages_nothing_and_writes_no_state(tmp_path, monkeypatch, capsys):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "secret-guard.log").write_text("a\nb\n")
    pages = []
    mod = _watch(tmp_path, monkeypatch, [])
    monkeypatch.setattr(mod, "_record_page", lambda *a, **k: pages.append(a))
    mod.main(["--dry-run"])
    assert pages == [] and not (fleet / "leak-watch.state.json").exists()
    assert "2 line(s)" in capsys.readouterr().out


def test_collector_attributes_published_transcript_and_joins_review(tmp_path, monkeypatch, capsys):
    root = tmp_path / "published"
    root.mkdir()
    uuid = "11111111-2222-3333-4444-555555555555"
    rec = json.dumps({"type": "assistant", "timestamp": "2026-09-27T10:00:00Z",
                      "sessionId": "some-other-id",
                      "message": {"id": "m1", "model": "claude-sonnet-5", "content": [
                          {"type": "text", "text": "hi"}],
                          "usage": {"input_tokens": 1, "output_tokens": 9}}})
    (root / f"{uuid}.jsonl").write_text(rec + "\n")
    monkeypatch.setenv("FLEET_REVIEWER_TRANSCRIPTS", str(root))
    mod = _load("fleet-collect")
    db = tmp_path / "fleet.db"
    monkeypatch.setattr(mod, "PROJECTS", tmp_path / "no-projects")
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work")
    mod.main()
    con = sqlite3.connect(db)
    assert con.execute("SELECT project, session_uuid FROM session").fetchall() == [
        ("revbot-reviews", uuid)]
    fr = _load("fleet-reviews")
    con.executescript(fr.SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(review)")]
    assert "session_uuid" in cols
    joined = con.execute(
        "SELECT COUNT(*) FROM (SELECT ?1 AS session_uuid) r "
        "JOIN session se ON se.session_uuid = r.session_uuid AND se.parent_uuid IS NULL",
        (uuid,)).fetchone()[0]
    assert joined == 1


def test_collector_absent_root_prints_one_skip_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLEET_REVIEWER_TRANSCRIPTS", str(tmp_path / "nope"))
    mod = _load("fleet-collect")
    monkeypatch.setattr(mod, "PROJECTS", tmp_path / "no-projects")
    monkeypatch.setattr(mod, "DB", tmp_path / "fleet.db")
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work")
    mod.main()
    assert capsys.readouterr().out.count("absent, skipped") == 1


# --- Plan 0092 review fix round: an unreadable published transcript must not
# crash the run, the guard-log offset must not advance on a failed/absent
# send, and an unreadable (as opposed to absent) guard log must say so. ---

def _chmod_000_is_effective() -> bool:
    """False when this uid (root, or any uid with CAP_DAC_OVERRIDE-like reach)
    can still read a chmod 000 file -- the fixture the reviews used to
    reproduce H1 is then not reachable, and the test must skip rather than
    pass for the wrong reason."""
    return os.geteuid() != 0


def test_unreadable_transcript_is_skipped_others_still_scanned_state_saved(
        tmp_path, monkeypatch, capsys):
    """Regression for reviewer H1 (both rounds): a chmod 000 transcript under
    the reviewer root must not crash fleet-leak-watch. The readable sibling
    with a planted secret is still scanned and paged, state is still saved
    (and the unreadable file's own offset is NOT advanced, so it is retried
    rather than silently marked done), and the guard-log scan -- which used to
    never be reached -- still runs."""
    if not _chmod_000_is_effective():
        pytest.skip("running as a uid that can still read chmod 000 (root)")
    root = tmp_path / "published"
    root.mkdir()
    bad = root / "unreadable.jsonl"
    bad.write_text(_result("nothing to see here") + "\n")
    bad.chmod(0o000)
    good = root / "readable.jsonl"
    good.write_text(_result(f"token {PAT}") + "\n")
    monkeypatch.setenv("FLEET_REVIEWER_TRANSCRIPTS", str(root))

    fleet = tmp_path / "fleet"
    fleet.mkdir()
    guard_log = fleet / "secret-guard.log"
    guard_log.write_text("failed open once\n")

    alerts = []
    mod = _watch(tmp_path, monkeypatch, alerts)
    try:
        rc = mod.main([])
    finally:
        bad.chmod(0o644)

    assert rc == 0, "a run with one unreadable transcript must still complete"
    assert [a[2] for a in alerts] == ["github-pat"], \
        "the readable sibling must still be scanned and alerted"
    assert alerts[0][0] == "reviewer/readable.jsonl"

    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    combined = out + err
    assert "unreadable" in combined and "unreadable.jsonl" in combined

    state_path = fleet / "leak-watch.state.json"
    assert state_path.exists(), "state must be saved even though one file failed"
    state = json.loads(state_path.read_text())
    assert state["offsets"].get("reviewer/readable.jsonl", 0) > 0
    assert state["offsets"].get("reviewer/unreadable.jsonl", 0) == 0, \
        "the unreadable file's offset must not advance -- it gets retried, not marked done"

    # The guard-log scan ran (it used to never be reached once _scan_file
    # raised): with no NTFY_URL configured, a grown guard log prints an ALERT
    # line to stdout naming the path and a count, never the log's own text.
    assert "ALERT" in out and str(guard_log) in out
    assert "failed open once" not in out and "failed open once" not in err


def test_guard_log_page_failure_does_not_advance_offset(tmp_path, monkeypatch):
    """Regression for reviewer M1/L2: when the page send fails, the guard-log
    offset must not advance -- the next run has to page again."""
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    log = fleet / "secret-guard.log"
    log.write_text("one\ntwo\n")

    mod = _watch(tmp_path, monkeypatch, [])
    monkeypatch.setattr(mod, "NTFY_URL", "http://example.invalid/push")
    monkeypatch.setattr(mod, "_record_page", lambda *a, **k: None)
    send_attempts = []

    def _boom(*a, **k):
        send_attempts.append(1)
        raise OSError("network down")

    monkeypatch.setattr(mod.urllib.request, "urlopen", _boom)

    mod.main([])
    assert len(send_attempts) == 1, "the send must actually be attempted"
    state = json.loads((fleet / "leak-watch.state.json").read_text())
    assert state["guard_offsets"].get(str(log), 0) == 0, \
        "a failed send must not advance the offset"

    mod.main([])
    assert len(send_attempts) == 2, "an unsent page must be retried on the next run"


def test_guard_log_no_channel_configured_does_not_advance_offset(tmp_path, monkeypatch):
    """Same contract as above for the other undelivered case: no ntfy channel
    configured at all (NTFY_URL empty) must also not advance the offset."""
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    log = fleet / "secret-guard.log"
    log.write_text("one\ntwo\n")

    mod = _watch(tmp_path, monkeypatch, [])
    monkeypatch.setattr(mod, "NTFY_URL", "")
    monkeypatch.setattr(mod, "_record_page", lambda *a, **k: None)

    mod.main([])
    state = json.loads((fleet / "leak-watch.state.json").read_text())
    assert state["guard_offsets"].get(str(log), 0) == 0

    mod.main([])
    state = json.loads((fleet / "leak-watch.state.json").read_text())
    assert state["guard_offsets"].get(str(log), 0) == 0


def test_unreadable_guard_log_emits_visible_line_distinct_from_absent(
        tmp_path, monkeypatch, capsys):
    """Regression for reviewer L1 (opus)/M1 (fable): an existing-but-unreadable
    guard log must say so, not vanish into the same silent path as an absent
    one."""
    if not _chmod_000_is_effective():
        pytest.skip("running as a uid that can still read chmod 000 (root)")
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    log = fleet / "secret-guard.log"
    log.write_text("one\n")
    log.chmod(0o000)

    mod = _watch(tmp_path, monkeypatch, [])
    monkeypatch.setattr(mod, "_record_page", lambda *a, **k: None)
    try:
        mod.main([])
    finally:
        log.chmod(0o644)

    err = capsys.readouterr().err
    assert "unreadable" in err and str(log) in err

    # Contrast: an ABSENT log stays a quiet skip -- nothing on stderr for it.
    absent_log = fleet / "does-not-exist.log"
    monkeypatch.setattr(mod, "_guard_logs", lambda: [absent_log])
    mod.main([])
    err2 = capsys.readouterr().err
    assert "unreadable" not in err2
