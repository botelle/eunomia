#!/usr/bin/env python3
"""Tests for fleet-collect / fleet-snapshot.

Run: python3 -m pytest tests/ -q      (stdlib only; no third-party deps)
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(BIN / name))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MID = [0]


def _turn(ts, model="claude-sonnet-5", out=10, tools=(), lines=1, iterations=None, mid=None):
    """One assistant message. Real transcripts emit ONE LINE PER CONTENT BLOCK,
    repeating the same message id and usage on each — `lines` reproduces that."""
    _MID[0] += 1
    msg_id = mid or f"msg_{_MID[0]}"
    usage = {"input_tokens": 1, "output_tokens": out,
             "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2}
    if iterations:
        usage["iterations"] = iterations
    content = [{"type": "text", "text": "hi"}]
    for name, inp in tools:
        content.append({"type": "tool_use", "name": name, "input": inp})
    rec = json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"id": msg_id, "model": model, "content": content, "usage": usage},
    })
    return "\n".join([rec] * lines)


def _write_transcript(root, project, sid, lines, subagent_of=None):
    d = root / project / (f"{subagent_of}/subagents" if subagent_of else "")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def _collect(tmp_path, monkeypatch, projects, fleet_dir=None, work_root=None):
    """Run the collector against a fake ~/.claude/projects, return db path.

    FLEET_DIR/WORK_ROOT are ALWAYS isolated to a tmp path too, even when the
    caller has no interest in events or run logs: this collector's default
    FLEET_DIR is the real `~/dev/.fleet`, and every session running this
    suite is itself a fleet worker with a real one on disk. Leaving it
    unmocked would ingest live production events into a test's throwaway DB."""
    mod = _load("fleet-collect")
    db = tmp_path / "fleet.db"
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "FLEET_DIR", fleet_dir or (tmp_path / "no-fleet-dir"))
    monkeypatch.setattr(mod, "WORK_ROOT", work_root or (tmp_path / "no-work-root"))
    mod.main()
    return db


def test_collects_sessions_turns_and_tokens(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [
        json.dumps({"type": "user", "timestamp": "2026-08-01T10:00:00Z",
                    "message": {"content": "do the thing"}}),
        _turn("2026-08-01T10:00:05Z", out=100,
              tools=[("Edit", {"file_path": "/repo/a.py"})]),
        _turn("2026-08-01T10:01:00Z", out=50),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    con = sqlite3.connect(db)
    row = con.execute("SELECT turns, out_tok, tool_calls, kind, first_prompt FROM session").fetchone()
    assert row[0] == 2
    assert row[1] == 150
    assert row[2] == 1
    assert con.execute("SELECT COUNT(*) FROM tool_use").fetchone()[0] == 1
    assert row[3] == "session"
    assert row[4] == "do the thing"
    assert con.execute("SELECT COUNT(*) FROM turn").fetchone()[0] == 2


def test_subagent_lineage(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "root-1", [_turn("2026-08-01T10:00:00Z")])
    _write_transcript(projects, "-Users-x", "agent-9", [_turn("2026-08-01T10:00:10Z")],
                      subagent_of="root-1")
    db = _collect(tmp_path, monkeypatch, projects)
    con = sqlite3.connect(db)
    kinds = dict(con.execute("SELECT session_uuid, kind FROM session"))
    assert kinds["root-1"] == "session"
    assert kinds["agent-9"] == "subagent"
    assert con.execute(
        "SELECT parent_uuid FROM session WHERE session_uuid='agent-9'"
    ).fetchone()[0] == "root-1"


def test_idempotent_reingest_does_not_duplicate(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [_turn("2026-08-01T10:00:00Z")])
    db = _collect(tmp_path, monkeypatch, projects)
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM tool_use").fetchone()[0]

    mod = _load("fleet-collect")
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet-dir")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work-root")
    mod.main()  # second run, nothing changed on disk
    after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM tool_use").fetchone()[0]
    assert before == after
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1


def test_grown_transcript_replaces_rows_not_appends(tmp_path, monkeypatch):
    """A live session's file grows between runs; rows must be replaced, not doubled."""
    projects = tmp_path / "projects"
    p = _write_transcript(projects, "-Users-x", "sess-a", [
        _turn("2026-08-01T10:00:00Z", tools=[("Bash", {"command": "ls -la"})]),
    ])
    db = _collect(tmp_path, monkeypatch, projects)

    with p.open("a") as fh:
        fh.write(_turn("2026-08-01T10:05:00Z", tools=[("Bash", {"command": "git status"})]) + "\n")
    mod = _load("fleet-collect")
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet-dir")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work-root")
    mod.main()

    con = sqlite3.connect(db)
    assert con.execute("SELECT turns FROM session").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM turn").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM tool_use").fetchone()[0] == 2


def test_bash_target_records_only_the_verb(tmp_path, monkeypatch):
    """Secret hygiene: a Bash command's arguments must never reach the DB."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [
        _turn("2026-08-01T10:00:00Z",
              tools=[("Bash", {"command": "curl -H 'Authorization: token SECRET123' https://x"})]),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    targets = [r[0] for r in sqlite3.connect(db).execute("SELECT target FROM tool_use")]
    assert targets == ["curl"]
    blob = Path(db).read_bytes()
    assert b"SECRET123" not in blob


def test_snapshot_is_consistent_and_prunes(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [_turn("2026-08-01T10:00:00Z")])
    db = _collect(tmp_path, monkeypatch, projects)

    snap = _load("fleet-snapshot")
    out = tmp_path / "snaps"
    for _ in range(3):
        # distinct names come from the UTC stamp; force uniqueness in-test
        import time as _t
        _t.sleep(1.01)
        snap.snapshot(db, out, keep=2)
    files = sorted(out.glob("fleet-*.db"))
    assert len(files) == 2, "retention should keep exactly --keep snapshots"
    con = sqlite3.connect(f"file:{files[-1]}?mode=ro", uri=True)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1


def test_snapshot_refuses_missing_db(tmp_path):
    snap = _load("fleet-snapshot")
    try:
        snap.snapshot(tmp_path / "nope.db", tmp_path / "out", keep=1)
    except SystemExit as e:
        assert "no database" in str(e)
    else:
        raise AssertionError("expected SystemExit for a missing database")


def test_repeated_message_lines_count_once(tmp_path, monkeypatch):
    """B1: one line per content block repeats the same id+usage — summing per line
    over-counted output tokens 2.54x on a real transcript."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [
        _turn("2026-08-01T10:00:00Z", out=100, lines=4),
        _turn("2026-08-01T10:01:00Z", out=50, lines=3),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM turn").fetchone()[0] == 2
    assert con.execute("SELECT turns, out_tok FROM session").fetchone() == (2, 150)


def test_multi_iteration_usage_is_summed(tmp_path, monkeypatch):
    """B1b: top-level usage mirrors only the LAST iteration; summing is required."""
    projects = tmp_path / "projects"
    iters = [{"input_tokens": 1, "output_tokens": 147, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0},
             {"input_tokens": 1, "output_tokens": 324, "cache_read_input_tokens": 5,
              "cache_creation_input_tokens": 2}]
    _write_transcript(projects, "-Users-x", "sess-a", [
        _turn("2026-08-01T10:00:00Z", out=324, iterations=iters, lines=2),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    assert sqlite3.connect(db).execute("SELECT out_tok FROM session").fetchone()[0] == 471


def test_same_stem_in_two_projects_does_not_clobber(tmp_path, monkeypatch):
    """B2: filename stems are not unique across projects (`journal` collides 5 ways)."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-a", "shared", [
        _turn("2026-08-01T10:00:00Z", tools=[("Read", {"file_path": "/a"})])])
    _write_transcript(projects, "-Users-b", "shared", [
        _turn("2026-08-01T11:00:00Z", tools=[("Read", {"file_path": "/b"})])])
    db = _collect(tmp_path, monkeypatch, projects)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM tool_use").fetchone()[0] == 2


def test_workflow_journal_is_not_a_transcript(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [_turn("2026-08-01T10:00:00Z")])
    d = projects / "-Users-x" / "sess-a" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "journal.jsonl").write_text('{"agent":"x","result":"y"}\n')
    db = _collect(tmp_path, monkeypatch, projects)
    ids = [r[0] for r in sqlite3.connect(db).execute("SELECT session_uuid FROM session")]
    assert ids == ["sess-a"]


def test_bash_env_assignment_never_recorded(tmp_path, monkeypatch):
    """B3: `RESTIC_PASSWORD=… restic backup` — the first WORD is the credential."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [
        _turn("2026-08-01T10:00:00Z", tools=[
            ("Bash", {"command": "RESTIC_PASSWORD=hunter2secretvalue restic backup /x"})]),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    targets = [r[0] for r in sqlite3.connect(db).execute("SELECT target FROM tool_use")]
    assert targets == ["restic"]
    assert b"hunter2secretvalue" not in Path(db).read_bytes()


def test_malformed_tool_input_does_not_halt_the_sweep(tmp_path, monkeypatch):
    """B4: a whitespace-only Bash command raised IndexError, and with a single
    end-of-run commit that meant launchd relaunched into the same crash forever."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-a", "bad", [
        _turn("2026-08-01T10:00:00Z", tools=[("Bash", {"command": "   "})])])
    _write_transcript(projects, "-Users-b", "good", [
        _turn("2026-08-01T10:00:00Z", tools=[("Bash", {"command": "git status"})])])
    db = _collect(tmp_path, monkeypatch, projects)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 2
    assert sorted(r[0] for r in con.execute("SELECT target FROM tool_use")) == ["", "git"]


def test_titles_use_the_real_field_names(tmp_path, monkeypatch):
    """M5: records carry customTitle / aiTitle, not `title`."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [
        json.dumps({"type": "ai-title", "aiTitle": "auto name",
                    "timestamp": "2026-08-01T10:00:00Z"}),
        json.dumps({"type": "custom-title", "customTitle": "my name",
                    "timestamp": "2026-08-01T10:00:01Z"}),
        _turn("2026-08-01T10:00:02Z"),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    assert sqlite3.connect(db).execute("SELECT title FROM session").fetchone()[0] == "my name"


def test_first_prompt_redacts_credential_shapes(tmp_path, monkeypatch):
    """M6: first_prompt is free text a human may have pasted a secret into."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [
        json.dumps({"type": "user", "timestamp": "2026-08-01T10:00:00Z",
                    "message": {"content": "deploy with token=ghp_AAAAAAAAAAAAAAAAAAAA now"}}),
        _turn("2026-08-01T10:00:05Z"),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    fp = sqlite3.connect(db).execute("SELECT first_prompt FROM session").fetchone()[0]
    assert "ghp_AAAAAAAAAAAAAAAAAAAA" not in fp
    assert "[redacted]" in fp
    assert b"ghp_AAAAAAAAAAAAAAAAAAAA" not in Path(db).read_bytes()


def test_truncated_file_clears_stale_rows(tmp_path, monkeypatch):
    """M7: a changed file that now scans to nothing must not leave old rows."""
    projects = tmp_path / "projects"
    p = _write_transcript(projects, "-Users-x", "sess-a", [
        _turn("2026-08-01T10:00:00Z", tools=[("Bash", {"command": "ls"})])])
    db = _collect(tmp_path, monkeypatch, projects)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM turn").fetchone()[0] == 1

    p.write_text("")  # truncated
    mod = _load("fleet-collect")
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet-dir")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work-root")
    mod.main()
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM turn").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM tool_use").fetchone()[0] == 0


def test_quoted_env_assignment_leaks_nothing(tmp_path, monkeypatch):
    """M1: whitespace-splitting `PASS="two words" cmd` stores half the secret."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [
        _turn("2026-08-01T10:00:00Z", tools=[
            ("Bash", {"command": 'RESTIC_PASSWORD="hunter two" restic backup /x'})]),
        _turn("2026-08-01T10:00:01Z", tools=[
            ("Bash", {"command": "PW='multi word secret' git push"})]),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    targets = sorted(r[0] for r in sqlite3.connect(db).execute("SELECT target FROM tool_use"))
    assert targets == ["git", "restic"]
    blob = Path(db).read_bytes()
    for leaked in (b"hunter", b"two", b"multi", b"word", b"secret"):
        assert leaked not in blob, f"{leaked!r} reached the database"


def test_subagent_uuid_is_its_own_not_the_parents(tmp_path, monkeypatch):
    """M2: real subagent records carry the PARENT's sessionId (verified 6/6 on disk),
    so trusting it makes one uuid map to the parent and all N of its subagents."""
    projects = tmp_path / "projects"
    parent_uuid = "644c1652-0111-4ae2"
    _write_transcript(projects, "-Users-x", parent_uuid, [
        json.dumps({"type": "user", "sessionId": parent_uuid,
                    "timestamp": "2026-08-01T10:00:00Z",
                    "message": {"content": "go"}}),
        _turn("2026-08-01T10:00:01Z"),
    ])
    # the subagent file, as the harness really writes it: parent's sessionId inside
    d = projects / "-Users-x" / parent_uuid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-ae0f4e84.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": parent_uuid,
                    "timestamp": "2026-08-01T10:00:02Z",
                    "message": {"content": "sub"}}) + "\n"
        + _turn("2026-08-01T10:00:03Z") + "\n"
    )
    db = _collect(tmp_path, monkeypatch, projects)
    con = sqlite3.connect(db)
    rows = dict(con.execute("SELECT kind, session_uuid FROM session"))
    assert rows["session"] == parent_uuid
    assert rows["subagent"] == "agent-ae0f4e84", "subagent must carry its own id"
    assert con.execute(
        "SELECT COUNT(DISTINCT session_uuid) FROM session"
    ).fetchone()[0] == 2, "uuid must not be ambiguous between parent and subagent"


def test_failed_file_is_retried_next_sweep(tmp_path, monkeypatch):
    """M3: a transient failure (e.g. `database is locked` under a snapshot's read
    lock) must not permanently exclude a transcript — completed files never change,
    so 'retry when it changes' means 'never'."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [_turn("2026-08-01T10:00:00Z")])
    db = _collect(tmp_path, monkeypatch, projects)

    # simulate a prior transient failure: rows absent, ingest row marked with an error
    con = sqlite3.connect(db)
    con.execute("DELETE FROM session")
    con.execute("DELETE FROM turn")
    con.execute("UPDATE ingest SET error='database is locked'")
    con.commit()
    con.close()

    mod = _load("fleet-collect")
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet-dir")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work-root")
    mod.main()

    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1, "must retry"
    assert con.execute("SELECT error FROM ingest").fetchone()[0] is None


def test_old_schema_db_is_rebuilt_not_crashed(tmp_path, monkeypatch):
    """A DB written by the pre-review schema must not crash every launchd tick."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [_turn("2026-08-01T10:00:00Z")])
    db = tmp_path / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, project TEXT);"
        "CREATE TABLE turn (session_id TEXT, seq INTEGER);"
        "CREATE TABLE tool_use (session_id TEXT);"
        "CREATE TABLE ingest (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, ingested_at TEXT);"
    )
    con.execute("INSERT INTO session VALUES ('stale','x')")
    con.commit()
    con.close()

    mod = _load("fleet-collect")
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet-dir")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work-root")
    mod.main()  # must not raise

    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == mod.SCHEMA_VERSION
    ids = [r[0] for r in con.execute("SELECT session_uuid FROM session")]
    assert ids == ["sess-a"], "stale rows dropped, rebuilt from transcripts"


def test_unbalanced_quotes_leak_nothing(tmp_path, monkeypatch):
    """M-A: the shlex fallback whitespace-split was the original leak path.
    A command with unbalanced quotes never ran; its verb is not worth the risk."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [
        _turn("2026-08-01T10:00:00Z", tools=[
            ("Bash", {"command": 'PASS="hunter two secret restic backup'})]),
        _turn("2026-08-01T10:00:01Z", tools=[
            ("Bash", {"command": """PW='p@ss w0rd" rsync -av /a /b"""})]),
    ])
    db = _collect(tmp_path, monkeypatch, projects)
    targets = [r[0] for r in sqlite3.connect(db).execute("SELECT target FROM tool_use")]
    assert targets == ["?", "?"]
    blob = Path(db).read_bytes()
    for leaked in (b"hunter", b"two", b"secret", b"p@ss", b"w0rd"):
        assert leaked not in blob, f"{leaked!r} reached the database"


def test_newer_schema_db_is_refused_not_downgraded(tmp_path, monkeypatch):
    """A DB from a future collector must not be stamped down and mis-ingested."""
    projects = tmp_path / "projects"
    _write_transcript(projects, "-Users-x", "sess-a", [_turn("2026-08-01T10:00:00Z")])
    db = tmp_path / "fleet.db"
    mod = _load("fleet-collect")
    con = sqlite3.connect(db)
    con.executescript(mod.SCHEMA)
    con.execute(f"PRAGMA user_version = {mod.SCHEMA_VERSION + 1}")
    con.commit()
    con.close()

    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet-dir")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work-root")
    try:
        mod.main()
    except SystemExit as e:
        assert "newer database" in str(e)
    else:
        raise AssertionError("expected a refusal against a newer-schema database")
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == mod.SCHEMA_VERSION + 1


# ------------------------------------------------------ D1/D2/D3/D4/D6/D7 ---
# fleet-collect ingests the event ledger and the orchestrator's per-lease run
# logs into `event` / `dispatch_phase`, and derives `dispatch` from them.

def _event(ts, etype, repo=None, pr=None, sha=None, lease=None, detail=None):
    return json.dumps({"ts": ts, "actor": "orchestrator", "type": etype,
                        "repo": repo, "pr": pr, "sha": sha, "lease": lease,
                        "detail": detail if detail is not None else {}})


def _write_events(fleet_dir, lines, name="events.jsonl"):
    fleet_dir.mkdir(parents=True, exist_ok=True)
    p = fleet_dir / name
    p.write_text("\n".join(lines) + "\n")
    return p


def _phase(ts, phase, **facts):
    parts = [ts, phase] + [f"{k}={v}" for k, v in facts.items()]
    return " ".join(parts)


def _write_run_log(fleet_dir, lease, lines):
    d = fleet_dir / "work" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{lease}.log"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_event_ledger_ingests_rows(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet"
    _write_events(fleet, [
        _event("2026-09-12T10:00:00Z", "plan-dispatched", repo="operator/ares",
               detail={"plan": "0001-x", "lease": "branch--0001-x--001"}),
        _event("2026-09-12T10:05:00Z", "pr-opened", repo="operator/ares", pr=7, sha="abc123"),
    ])
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 2
    row = con.execute("SELECT repo, pr, sha FROM event WHERE type='pr-opened'").fetchone()
    assert row == ("operator/ares", 7, "abc123")


def test_event_rotation_and_archive_replay_no_duplicates(tmp_path, monkeypatch):
    """D6/DoD: rotation splits the live file into an archive plus a fresh one;
    re-collecting after that, and again with no change, must never duplicate."""
    fleet = tmp_path / "fleet"
    _write_events(fleet, [
        _event("2026-09-12T10:00:00Z", "plan-dispatched", repo="operator/ares"),
        _event("2026-09-12T10:01:00Z", "plan-done", repo="operator/ares"),
    ])
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM event").fetchone()[0] == 2

    # Re-ingesting the SAME unchanged file must not duplicate (DoD).
    _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM event").fetchone()[0] == 2

    # Rotate: the live file becomes an archive, a fresh (smaller) one appears.
    (fleet / "events.jsonl").rename(fleet / "events-20260912-100200.jsonl")
    _write_events(fleet, [
        _event("2026-09-12T10:03:00Z", "plan-dispatched", repo="operator/sniff"),
    ])
    _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 3

    # Replaying (re-collecting again, archive untouched) must not duplicate.
    _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet)
    assert con.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 3
    con2 = sqlite3.connect(db)
    assert con2.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 3


def test_event_torn_line_skipped_and_picked_up_next_run(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    complete = _event("2026-09-12T10:00:00Z", "plan-dispatched", repo="operator/ares")
    (fleet / "events.jsonl").write_text(complete + "\n" + '{"ts":"2026-09-12T10:01')  # torn
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1

    # The write completes (as fleet-emit's own tail repair + next append would).
    second = _event("2026-09-12T10:01:00Z", "plan-done", repo="operator/ares")
    (fleet / "events.jsonl").write_text(complete + "\n" + second + "\n")
    _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM event").fetchone()[0] == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod cannot deny root; the cihost-linux job container runs as uid 0, so this permission-denied path is only testable on the host label")
def test_unreadable_run_log_is_reported_not_absent(tmp_path, monkeypatch):
    """Boundary: absent must not look like zero. An unreadable log must be
    recorded as a read failure, never silently skipped as if it had no phases."""
    fleet = tmp_path / "fleet"
    p = _write_run_log(fleet, "branch--0001-x--001", [
        _phase("2026-09-12T10:00:00Z", "start", repo="operator/ares", plan="0001-x",
               lease="branch--0001-x--001", session="sess-1"),
    ])
    os.chmod(p, 0o000)
    try:
        mod = _load("fleet-collect")
        db = tmp_path / "fleet.db"
        monkeypatch.setattr(mod, "PROJECTS", tmp_path / "projects")
        monkeypatch.setattr(mod, "DB", db)
        monkeypatch.setattr(mod, "FLEET_DIR", fleet)
        monkeypatch.setattr(mod, "WORK_ROOT", fleet / "work")
        rc = mod.main()
    finally:
        os.chmod(p, 0o644)
    assert rc == 1, "an unreadable source must be reported via a non-zero exit"
    con = sqlite3.connect(db)
    err = con.execute(
        "SELECT error FROM ingest WHERE path=?", (f"logs/{p.name}",)
    ).fetchone()
    assert err is not None and err[0], "the read failure must be recorded, not silent"
    assert con.execute("SELECT COUNT(*) FROM dispatch_phase").fetchone()[0] == 0


def test_all_named_phases_round_trip(tmp_path, monkeypatch):
    """D6: every phase name plan 0036 D2 lists must survive ingest -> query."""
    fleet = tmp_path / "fleet"
    lease = "branch--0001-x--001"
    named = ["start", "routed", "resumed", "verified", "unverified", "ci-waiting",
             "ci-resolved", "review-round", "review-approved", "steward-start",
             "steward-end", "workspace-failed", "session-refused", "branch-cleared"]
    lines = [_phase(f"2026-09-12T10:{i:02d}:00Z", phase, k="v")
             for i, phase in enumerate(named)]
    _write_run_log(fleet, lease, lines)
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet,
                  work_root=fleet / "work")
    con = sqlite3.connect(db)
    got = {r[0] for r in con.execute(
        "SELECT phase FROM dispatch_phase WHERE lease=?", (lease,)
    )}
    assert got == set(named)


def test_dispatch_view_success_path_terminal_via_steward_end(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet"
    lease = "branch--0002-y--001"
    _write_run_log(fleet, lease, [
        _phase("2026-09-12T10:00:00Z", "start", repo="operator/sniff", plan="0002-y",
               lease=lease, session="sess-2"),
        _phase("2026-09-12T10:01:00Z", "routed", runner="claude"),
        _phase("2026-09-12T10:05:00Z", "implementer-exit", rc=0, timed_out=False,
               seconds=300, transcript="-", shapes="-", peak_rss_mb=512,
               rss_samples=10, orch_rss_mb=64),
        _phase("2026-09-12T10:06:00Z", "verified", pr=42, detail="-"),
        _phase("2026-09-12T10:10:00Z", "review-approved", pr=42, rounds=1, sha="deadbee"),
        _phase("2026-09-12T10:20:00Z", "steward-end", pr=42, state="merged", released=True),
    ])
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet,
                  work_root=fleet / "work")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM dispatch WHERE lease=?", (lease,)).fetchone()
    assert row["repo"] == "operator/sniff"
    assert row["plan_id"] == "0002-y"
    assert row["current_phase"] == "steward-end"
    assert row["pr"] == 42
    assert row["outcome"] == "merged"
    assert row["terminal"] == 1
    assert row["rc"] == 0 and row["timed_out"] == 0 and row["seconds"] == 300
    assert row["peak_rss_mb"] == 512 and row["orch_rss_mb"] == 64 and row["rss_samples"] == 10


def test_dispatch_view_multiple_fix_rounds_yield_one_row(tmp_path, monkeypatch):
    """A review loop with fix rounds calls run_implementer more than once, so
    implementer-exit is NOT one-shot per lease (found on real fleet data: a
    naive join fanned out into two `dispatch` rows for one lease). The view
    must report exactly one row, carrying the LAST exit's cost/rc."""
    fleet = tmp_path / "fleet"
    lease = "branch--0006-u--002"
    _write_run_log(fleet, lease, [
        _phase("2026-09-12T10:00:00Z", "start", repo="operator/speakhush", plan="0006-u",
               lease=lease, session="sess-6"),
        _phase("2026-09-12T10:01:00Z", "verified", pr=161, detail="-"),
        _phase("2026-09-12T10:02:00Z", "implementer-start", runner="sub-sonnet"),
        _phase("2026-09-12T10:05:00Z", "implementer-exit", rc=1, timed_out=False,
               seconds=154, transcript="-", shapes="-", peak_rss_mb=341,
               rss_samples=77, orch_rss_mb=37),
        _phase("2026-09-12T10:06:00Z", "implementer-start", runner="sub-sonnet"),
        _phase("2026-09-12T10:09:00Z", "implementer-exit", rc=0, timed_out=False,
               seconds=167, transcript="-", shapes="-", peak_rss_mb=345,
               rss_samples=82, orch_rss_mb=37),
        _phase("2026-09-12T10:20:00Z", "steward-end", pr=161, state="merged", released=True),
    ])
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet,
                  work_root=fleet / "work")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM dispatch WHERE lease=?", (lease,)).fetchall()
    assert len(rows) == 1, "one lease must yield exactly one dispatch row"
    assert rows[0]["rc"] == 0 and rows[0]["seconds"] == 167, "must report the LAST exit"


def test_dispatch_view_review_approved_is_not_terminal(tmp_path, monkeypatch):
    """Handoff: review-approved is the gap where a PR waits on a human merge —
    it must not read as stuck or done."""
    fleet = tmp_path / "fleet"
    lease = "branch--0003-z--001"
    _write_run_log(fleet, lease, [
        _phase("2026-09-12T10:00:00Z", "start", repo="operator/sniff", plan="0003-z",
               lease=lease, session="sess-3"),
        _phase("2026-09-12T10:10:00Z", "review-approved", pr=9, rounds=1, sha="cafefee"),
    ])
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet,
                  work_root=fleet / "work")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM dispatch WHERE lease=?", (lease,)).fetchone()
    assert row["current_phase"] == "review-approved"
    assert row["outcome"] is None
    assert row["terminal"] == 0


def test_dispatch_view_failure_path_answers_why_it_died(tmp_path, monkeypatch):
    """D7: the ares 0001 scenario — one query answers why a run died, with the
    run-log and transcript paths and the implementer's rc/timed_out/seconds,
    without opening either file."""
    fleet = tmp_path / "fleet"
    lease = "branch--0001-ranged-standoff-core--001"
    log_path = f"/x/.fleet/work/logs/{lease}.log"
    transcript_path = f"/x/.orchestrator-transcripts/{lease}.log"
    _write_events(fleet, [
        _event("2026-09-12T10:00:00Z", "plan-dispatched", repo="operator/ares",
               detail={"plan": "0001-ranged-standoff-core", "session": "sess-1",
                       "lease": lease}),
        _event("2026-09-12T11:30:00Z", "plan-failed", repo="operator/ares",
               detail={"plan": "0001-ranged-standoff-core", "lease": lease,
                       "reason": "implementer timed out after 90 min; process group killed",
                       "log": log_path, "transcript": transcript_path}),
    ])
    _write_run_log(fleet, lease, [
        _phase("2026-09-12T10:00:00Z", "start", repo="operator/ares",
               plan="0001-ranged-standoff-core", lease=lease, session="sess-1"),
        _phase("2026-09-12T10:00:05Z", "routed", runner="claude"),
        _phase("2026-09-12T11:30:05Z", "implementer-exit", rc=-9, timed_out=True,
               seconds=5400, transcript=transcript_path, shapes="-",
               peak_rss_mb=812, rss_samples=44, orch_rss_mb=120),
    ])
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet,
                  work_root=fleet / "work")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM dispatch WHERE repo=? AND plan_id=? AND outcome='failed'",
        ("operator/ares", "0001-ranged-standoff-core"),
    ).fetchone()
    assert row is not None, "one query must find the failed dispatch"
    assert row["failure_reason"] == "implementer timed out after 90 min; process group killed"
    assert row["failure_log"] == log_path
    assert row["failure_transcript"] == transcript_path
    assert row["timed_out"] == 1
    assert row["seconds"] == 5400
    assert row["terminal"] == 1


def test_event_and_phase_facts_are_redacted_on_read(tmp_path, monkeypatch):
    """Boundary: re-redact; do not trust the writer. A secret-shaped string in
    an event's detail or a run-log fact must not survive into the DB."""
    fleet = tmp_path / "fleet"
    secret = "ghp_AAAAAAAAAAAAAAAAAAAA"
    _write_events(fleet, [
        _event("2026-09-12T10:00:00Z", "plan-failed", repo="operator/ares",
               detail={"reason": f"push rejected: token={secret}"}),
    ])
    _write_run_log(fleet, "branch--0004-w--001", [
        _phase("2026-09-12T10:00:00Z", "branch-cleared", branch="feat/x",
               detail=f"remote said token={secret}"),
    ])
    db = _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet,
                  work_root=fleet / "work")
    blob = Path(db).read_bytes()
    assert secret.encode() not in blob


def test_collector_does_not_touch_the_fleet_tree(tmp_path, monkeypatch):
    """DoD: ~/dev/.fleet is byte-identical before and after a collection run.
    The collector writes its own DB and nothing else under that tree."""
    fleet = tmp_path / "fleet"
    ev = _write_events(fleet, [
        _event("2026-09-12T10:00:00Z", "plan-dispatched", repo="operator/ares"),
    ])
    lg = _write_run_log(fleet, "branch--0005-v--001", [
        _phase("2026-09-12T10:00:00Z", "start", repo="operator/ares", plan="0005-v",
               lease="branch--0005-v--001", session="sess-5"),
    ])
    before = {p: p.read_bytes() for p in fleet.rglob("*") if p.is_file()}
    db = tmp_path / "fleet.db"          # deliberately OUTSIDE `fleet`
    _collect(tmp_path, monkeypatch, tmp_path / "projects", fleet_dir=fleet,
             work_root=fleet / "work")
    after = {p: p.read_bytes() for p in fleet.rglob("*") if p.is_file()}
    assert before == after, "the fleet tree must be untouched by a collection run"
    assert not db.exists() or db.resolve().parent == tmp_path.resolve()


# --------------------------------------------- a lease known only from the ledger

def _view_db(fc, tmp_path, phase_rows=(), event_rows=()):
    """`dispatch` over hand-built rows — the shape tests/test_mopsus.py uses."""
    con = sqlite3.connect(str(tmp_path / "v.db"))
    con.executescript(fc.SCHEMA)
    con.executescript(fc.DISPATCH_VIEW)
    con.executemany("INSERT INTO dispatch_phase VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    list(phase_rows))
    con.executemany("INSERT INTO event VALUES (?,?,?,?,?,?,?,?,?,?)", list(event_rows))
    con.commit()
    return con


def _dispatched_event(ts, lease, repo, plan, line=1):
    """`line` is not decoration: `event` is PRIMARY KEY (source, line), so two
    events sharing one both refuses the insert. The respawn test needs two."""
    detail = json.dumps({"lease": lease, "plan": plan, "branch": "feat/x",
                         "session": "sess-1"})
    return (ts, "watch", "plan-dispatched", repo, None, None, lease, detail,
            "events.jsonl", line)


def test_lease_known_only_from_the_ledger_still_names_its_repo_and_plan(tmp_path):
    """An orchestrator that dies before writing a run log leaves a
    `plan-dispatched` event and no `dispatch_phase` rows. Every field used to
    come from `dispatch_phase`, so the row was all NULL and `terminal = 0`
    forever.

    Measured 2026-09-16: three `operator/pr-proxy` leases from 2026-09-10
    rendered in the phone app as "3 running now" with no repo and no plan, while
    the one genuinely running dispatch was not shown as running at all.
    """
    fc = _load("fleet-collect")
    con = _view_db(fc, tmp_path, event_rows=[
        _dispatched_event("2026-09-10T02:29:03Z", "branch--ghost--001",
                          "operator/pr-proxy", "0002-ci-logs")])
    row = con.execute("SELECT repo, plan_id, session, started_ts, current_phase, "
                      "current_phase_ts, terminal FROM dispatch").fetchone()
    assert row[0] == "operator/pr-proxy"
    assert row[1] == "0002-ci-logs"
    assert row[2] == "sess-1"
    assert row[3] == "2026-09-10T02:29:03Z"
    assert row[4] == "dispatched"
    # THE load-bearing assertion. A consumer derives "stuck" from idle time; a
    # NULL here makes idle unknowable, and the lynceus client then falls through
    # to "running" — which is how a six-day-dead lease was counted as live.
    assert row[5] == "2026-09-10T02:29:03Z"
    assert row[6] == 0          # still non-terminal: nothing concluded it


def test_run_log_facts_still_win_over_the_ledger(tmp_path):
    """The fallback is a fallback. Where `dispatch_phase` has the fact, it is
    used — otherwise a respawn's second `plan-dispatched` could overwrite the
    repo and plan a real run recorded."""
    fc = _load("fleet-collect")
    start_detail = json.dumps({"repo": "operator/real", "plan": "0001-real",
                               "session": "sess-real"})
    con = _view_db(
        fc, tmp_path,
        phase_rows=[("L1", 1, "2026-09-12T00:00:00Z", "start", None, None, None,
                     None, None, None, None, start_detail, "L1.log", 1),
                    ("L1", 2, "2026-09-12T00:05:00Z", "implementer-start", None,
                     None, None, None, None, None, None, "{}", "L1.log", 2)],
        event_rows=[_dispatched_event("2026-09-12T00:00:01Z", "L1",
                                      "operator/WRONG", "0099-wrong", line=3)])
    repo, plan, ts, phase = con.execute(
        "SELECT repo, plan_id, started_ts, current_phase FROM dispatch").fetchone()
    assert (repo, plan) == ("operator/real", "0001-real")
    assert ts == "2026-09-12T00:00:00Z"
    assert phase == "implementer-start"


def test_a_respawn_does_not_duplicate_an_event_only_lease(tmp_path):
    """Two `plan-dispatched` events for one lease — the respawn path — must
    still yield ONE row, dated from the first. This is the fan-out the other
    CTEs already guard against, arriving through a new join."""
    fc = _load("fleet-collect")
    con = _view_db(fc, tmp_path, event_rows=[
        _dispatched_event("2026-09-10T02:00:00Z", "L2", "operator/r", "0003-p", line=1),
        _dispatched_event("2026-09-10T09:00:00Z", "L2", "operator/r", "0003-p", line=2)])
    rows = con.execute("SELECT lease, started_ts FROM dispatch").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "2026-09-10T02:00:00Z"


def _released_event(ts, lease, line):
    """Shaped like `fleet-release` actually writes it: the id in the `lease`
    COLUMN and an EMPTY detail.

    This fixture used to put the id in both places, and that is what let the
    first version of the released CTE ship reading `detail_json.$.lease` — a
    query that matched every row here and nothing at all in production, where
    all 152 lease-released rows carry `{}` (review 3232). `bin/fleet-emit`
    requires --lease for a lease-* event and requires lease=null for anything
    else, so a detail-carried id is not merely unusual here, it is the shape
    the emitter refuses to write. Keep this in step with the emitter: a fixture
    more generous than the producer tests a query that cannot run."""
    return (ts, "watch", "lease-released", "operator/r", None, None, lease,
            "{}", "events.jsonl", line)


def test_a_released_lease_is_terminal(tmp_path):
    """A lease the orchestrator released is over, whatever else was recorded.

    `terminal` read only failed/done/stewarded, so a run released without one of
    those stayed non-terminal forever and was counted as running. Measured
    2026-09-20: six such rows on opshost, three of them released by hand that day
    and three released on 2026-09-10.
    """
    fc = _load("fleet-collect")
    con = _view_db(fc, tmp_path, event_rows=[
        _dispatched_event("2026-09-10T02:00:00Z", "L9", "operator/r", "0003-p", line=11),
        _released_event("2026-09-10T03:00:00Z", "L9", line=12)])
    outcome, terminal, ts = con.execute(
        "SELECT outcome, terminal, current_phase_ts FROM dispatch").fetchone()
    assert terminal == 1
    assert outcome == "released"
    assert ts == "2026-09-10T03:00:00Z"


def test_a_recorded_outcome_beats_released(tmp_path):
    """`released` says the run is OVER, not how it went. Every dispatch ends by
    releasing its lease, so if release won this chain every successful run would
    report `released` instead of its real outcome."""
    fc = _load("fleet-collect")
    con = _view_db(fc, tmp_path, event_rows=[
        _dispatched_event("2026-09-10T02:00:00Z", "L8", "operator/r", "0004-p", line=21),
        ("2026-09-10T02:30:00Z", "w", "plan-failed", "operator/r", None, None, "L8",
         json.dumps({"lease": "L8", "reason": "boom"}), "events.jsonl", 22),
        _released_event("2026-09-10T03:00:00Z", "L8", line=23)])
    outcome, terminal, reason = con.execute(
        "SELECT outcome, terminal, failure_reason FROM dispatch").fetchone()
    assert (outcome, terminal, reason) == ("failed", 1, "boom")


# ------------------------------- plan 0076: dispatch_session and dispatch_cost
#
# The slug below is the literal the CLI wrote for plan 0068's second run,
# copied from fleet.db — NOT derived by the code under test, which is exactly
# how a wrong rule (`.` and `/` folded, `_` forgotten) would pass its own test.

REAL_CWD = ("/Users/operator/dev/.fleet/work/operator_s_eunomia/"
            "branch--feat-0068-the-dispatch-cap-is-configurat--002")
REAL_SLUG = ("-Users-operator-dev--fleet-work-operator-s-eunomia-"
             "branch--feat-0068-the-dispatch-cap-is-configurat--002")
LEASE = "branch--feat-0068-the-dispatch-cap-is-configurat--002"
RUN_UUID = "a41acbce-5428-423e-b522-bac0235ce8f9"


def test_the_slug_folds_slash_dot_and_underscore_each_on_its_own():
    fc = _load("fleet-collect")
    assert fc.fleet_slug("a/b") == "a-b"
    assert fc.fleet_slug("a.b") == "a-b"
    assert fc.fleet_slug("a_b") == "a-b"
    assert fc.fleet_slug("a b-c") == "a-b-c"       # a space, and an existing `-`, are one char each
    assert fc.fleet_slug("Ab9") == "Ab9"           # the kept set is [A-Za-z0-9]
    assert fc.fleet_slug("é") == "-"
    assert fc.fleet_slug(None) is None
    assert fc.fleet_slug(REAL_CWD) == REAL_SLUG


def _pair_db(fc, tmp_path, pairs, sessions):
    """`pairs`: [(start_ts, exit_ts)] on one lease, all in REAL_CWD.
    `sessions`: [(uuid, project, first_ts, parent)]."""
    con = _view_db(fc, tmp_path)
    fc.register_functions(con)
    con.executescript(fc.DISPATCH_SESSION_VIEW)
    con.executescript(fc.DISPATCH_COST_VIEW)
    seq = 0
    for i, (a, b) in enumerate(pairs):
        for ts, phase, facts in ((a, "implementer-start", {"cwd": REAL_CWD}),
                                 (b, "implementer-exit", {"rc": "0"})):
            seq += 1
            con.execute("INSERT INTO dispatch_phase VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (LEASE, seq, ts, phase, None, None, None, None, None, None, None,
                         json.dumps(facts), f"{LEASE}.log", seq))
    for uuid, project, first, parent in sessions:
        con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{project}/{uuid}.jsonl", uuid, project, parent, "session", first,
                     "2026-09-22T02:28:11.619Z", 204, 0, 100, 200, 300, 400, "[]", "", ""))
    con.commit()
    return con


RUN = ("2026-09-22T01:53:19Z", "2026-09-22T02:28:12Z")


def test_a_pair_links_to_the_one_session_the_cli_wrote(tmp_path):
    fc = _load("fleet-collect")
    con = _pair_db(fc, tmp_path, [RUN],
                   [(RUN_UUID, REAL_SLUG, "2026-09-22T01:53:23.461Z", None)])
    assert con.execute("SELECT lease, seq, session_uuid FROM dispatch_session").fetchall() \
        == [(LEASE, 2, RUN_UUID)]
    assert fc.unlinked_pairs(con) == (1, 0)


def test_no_session_and_two_sessions_both_leave_the_pair_unlinked(tmp_path):
    fc = _load("fleet-collect")
    (tmp_path / "a").mkdir()
    con = _pair_db(fc, tmp_path / "a", [RUN], [])
    assert con.execute("SELECT COUNT(*) FROM dispatch_session").fetchone()[0] == 0
    assert fc.unlinked_pairs(con) == (1, 1)

    (tmp_path / "b").mkdir()
    con = _pair_db(fc, tmp_path / "b", [RUN], [
        (RUN_UUID, REAL_SLUG, "2026-09-22T01:53:23.461Z", None),
        ("bbbbbbbb-0000-4000-8000-000000000002", REAL_SLUG, "2026-09-22T02:00:00.000Z", None)])
    assert con.execute("SELECT COUNT(*) FROM dispatch_session").fetchone()[0] == 0
    assert fc.unlinked_pairs(con) == (1, 1)


def test_a_subagent_in_the_same_project_is_not_a_second_match(tmp_path):
    fc = _load("fleet-collect")
    con = _pair_db(fc, tmp_path, [RUN], [
        (RUN_UUID, REAL_SLUG, "2026-09-22T01:53:23.461Z", None),
        ("cccccccc-0000-4000-8000-000000000003", REAL_SLUG, "2026-09-22T02:00:00.000Z", RUN_UUID)])
    assert con.execute("SELECT session_uuid FROM dispatch_session").fetchall() == [(RUN_UUID,)]


def test_the_window_is_the_pair_widened_by_one_second_and_the_project_must_match(tmp_path):
    fc = _load("fleet-collect")
    cases = [("2026-09-22T01:53:18.200Z", 1),     # 0.8 s before the start: inside the margin
             ("2026-09-22T01:53:15.000Z", 0),     # 4 s before: outside
             ("2026-09-22T02:28:12.800Z", 1),     # just after the exit
             ("2026-09-22T02:28:20.000Z", 0)]
    for i, (first, want) in enumerate(cases):
        d = tmp_path / f"w{i}"
        d.mkdir()
        con = _pair_db(fc, d, [RUN], [(RUN_UUID, REAL_SLUG, first, None)])
        assert con.execute("SELECT COUNT(*) FROM dispatch_session").fetchone()[0] == want, first
    d = tmp_path / "other"
    d.mkdir()
    con = _pair_db(fc, d, [RUN], [(RUN_UUID, REAL_SLUG + "x", "2026-09-22T01:53:23.461Z", None)])
    assert con.execute("SELECT COUNT(*) FROM dispatch_session").fetchone()[0] == 0


def test_a_fix_round_is_its_own_pair_and_an_unlinked_one_is_counted(tmp_path):
    fc = _load("fleet-collect")
    fix = ("2026-09-22T03:00:00Z", "2026-09-22T03:10:00Z")
    both = [(RUN_UUID, REAL_SLUG, "2026-09-22T01:53:23.461Z", None),
            ("dddddddd-0000-4000-8000-000000000004", REAL_SLUG, "2026-09-22T03:00:04.000Z", None)]
    con = _pair_db(fc, tmp_path, [RUN, fix], both)
    assert con.execute("SELECT seq FROM dispatch_session ORDER BY seq").fetchall() == [(2,), (4,)]
    (tmp_path / "x").mkdir()
    con = _pair_db(fc, tmp_path / "x", [RUN, fix], both[:1])
    assert con.execute("SELECT seq FROM dispatch_session").fetchall() == [(2,)]
    assert fc.unlinked_pairs(con) == (2, 1)


def test_a_start_with_no_exit_is_an_unlinked_pair_not_a_crash(tmp_path):
    fc = _load("fleet-collect")
    con = _pair_db(fc, tmp_path, [RUN], [(RUN_UUID, REAL_SLUG, "2026-09-22T01:53:23.461Z", None)])
    con.execute("DELETE FROM dispatch_phase WHERE phase = 'implementer-exit'")
    assert con.execute("SELECT COUNT(*) FROM dispatch_session").fetchone()[0] == 0
    assert fc.unlinked_pairs(con) == (1, 1)


def test_rows_ingested_by_the_old_collector_link_without_a_reingest(tmp_path, monkeypatch, capsys):
    """Backfill. A database the previous collector built has the run-log rows and
    the session rows but neither view; one sweep of the new collector, which
    ingests nothing, is enough for them to link."""
    fc = _load("fleet-collect")
    db = tmp_path / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(fc.SCHEMA)
    con.executescript(fc.DISPATCH_VIEW)              # what the old collector created
    con.execute(f"PRAGMA user_version = {fc.SCHEMA_VERSION}")
    con.commit()
    con.close()
    con = sqlite3.connect(db)
    for seq, (ts, phase, facts) in enumerate((
            (RUN[0], "implementer-start", {"cwd": REAL_CWD}),
            (RUN[1], "implementer-exit", {"rc": "0"}),
            ("2026-09-22T03:00:00Z", "implementer-start", {"cwd": REAL_CWD}),
            ("2026-09-22T03:10:00Z", "implementer-exit", {"rc": "0"})), 1):
        con.execute("INSERT INTO dispatch_phase VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (LEASE, seq, ts, phase, None, None, None, None, None, None, None,
                     json.dumps(facts), f"{LEASE}.log", seq))
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("p/s.jsonl", RUN_UUID, REAL_SLUG, None, "session",
                 "2026-09-22T01:53:23.461Z", "2026-09-22T02:28:11.619Z",
                 204, 0, 100, 200, 300, 400, "[]", "", ""))
    con.commit()
    con.close()
    assert not sqlite3.connect(db).execute(
        "SELECT 1 FROM sqlite_master WHERE name='dispatch_session'").fetchone()

    mod = _load("fleet-collect")
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "PROJECTS", tmp_path / "no-projects")
    monkeypatch.setattr(mod, "FLEET_DIR", tmp_path / "no-fleet")
    monkeypatch.setattr(mod, "WORK_ROOT", tmp_path / "no-work")
    mod.main()
    out = capsys.readouterr().out
    assert "dispatch_session: 1 of 2 implementer pair(s) linked, 1 unlinked" in out
    con = sqlite3.connect(db)
    mod.register_functions(con)
    assert con.execute("SELECT session_uuid FROM dispatch_session").fetchall() == [(RUN_UUID,)]


def _with_review_table(fc, con):
    fr = _load("fleet-reviews")
    con.executescript(fr.SCHEMA)


def test_dispatch_cost_reports_a_linked_run_and_a_linked_round_and_no_row_for_an_unlinked_one(tmp_path):
    fc = _load("fleet-collect")
    con = _pair_db(fc, tmp_path, [RUN], [(RUN_UUID, REAL_SLUG, "2026-09-22T01:53:23.461Z", None)])
    _with_review_table(fc, con)
    rev = "eeeeeeee-0000-4000-8000-000000000005"
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r/s.jsonl", rev, "-dispatch-review", None, "session",
                 "2026-09-22T03:11:00.000Z", "2026-09-22T03:16:00.000Z",
                 12, 0, 50_000, 21_000, 600_000, 0, "[]", "", ""))
    for fid, rnd, sess in ((1, 1, rev), (2, 2, None), (3, 3, "ffffffff-no-such-session")):
        con.execute("INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict, session_uuid) "
                    "VALUES (?,?,?,?,?,?,?)", (fid, "operator/eunomia", 68, rnd, "revbot", "COMMENT", sess))
    con.commit()
    cols = "kind, lease, seq, repo, pr, round, session_uuid, processed, output, turns, minutes"
    rows = con.execute(f"SELECT {cols} FROM dispatch_cost ORDER BY kind, round").fetchall()
    assert rows == [
        ("implementer", LEASE, 2, None, None, None, RUN_UUID, 100 + 300 + 400, 200, 204, 34.8),
        ("review", None, None, "operator/eunomia", 68, 1, rev, 650_000, 21_000, 12, 5.0)]
    # minutes come from julianday, not from subtracting text: 01:53:23 -> 02:28:11 crosses the hour
    assert rows[0][-1] == 34.8


def test_the_views_are_classified_as_neither_table_set(tmp_path):
    fc = _load("fleet-collect")
    con = _pair_db(fc, tmp_path, [RUN], [])
    _with_review_table(fc, con)
    views = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert {"dispatch", "dispatch_session", "dispatch_cost"} <= views
    assert not views & (fc.DERIVED_TABLES | fc.DURABLE_TABLES)
