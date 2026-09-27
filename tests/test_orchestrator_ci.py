"""Tests for the CI-fix briefing's stored log excerpt (plan 0069).

Measured on eunomia#487 (plan 0065): a red `Plans / lint` job failed six
pytest tests while `ci_task` 3311 already carried a stored, redacted 343-line
body — and nothing read it. The fix session reproduced nothing locally, said
so, and the orchestrator stopped with a one-line reason naming only the check,
never the tests. This file proves the briefing now carries what fleet-collect
already stored, waits for it the way plan 0045 designed the ledger to be
waited on rather than fetched, and that the round's stop reason names what
actually failed.

No network, no model, and (per plan 0069 §3) no `fleet-ci` sync: fleet.db is a
real sqlite file built with `bin/fleet-collect`'s own SCHEMA, read back through
`bin/fleet-ci`'s read-only query surface exactly as the orchestrator does.
"""
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_orchestrator_review import (  # noqa: F401 -- reused fixtures, not edited
    CIForge, FakeForge, SHA1, SHA2, _fleetforge, _load, _loop, _review, _wire_forge,
)

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def _load_mod(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _ts(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _ci_db(tmp_path, tasks=(), logs=()):
    """A real fleet.db, schema owned by bin/fleet-collect (plan 0045)."""
    fc = _load_mod("fleet-collect")
    db = tmp_path / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(fc.SCHEMA)
    for t in tasks:
        con.execute(
            "INSERT INTO ci_task (repo, id, name, head_sha, status, log_state,"
            " log_detail, updated_at) VALUES (?,?,?,?,?,?,?,?)", t)
    for l in logs:
        con.execute(
            "INSERT INTO ci_log (repo, task_id, body_z, raw_bytes, cap_bytes,"
            " redactions, captured_at) VALUES (?,?,?,?,?,?,?)", l)
    con.commit()
    con.close()
    return db


def _task(repo, tid, name, sha, log_state, detail=None, ago=60):
    return (repo, tid, name, sha, "failure", log_state, detail, _ts(ago))


def _log_row(repo, tid, body, redactions=None):
    return (repo, tid, zlib.compress(body.encode()), len(body), len(body),
           json.dumps(redactions or {}), _ts(30))


REPO = "operator/x"
LINT_LOG = (
    "collecting tests\n"
    "FAILED tests/test_repo.py::test_a - AssertionError: boom\n"
    "FAILED tests/test_repo.py::test_b - AssertionError: boom\n"
    "= 2 failed, 40 passed in 3.1s =\n")


class FakeLog:
    def __init__(self):
        self.notes = []

    def note(self, phase, **facts):
        self.notes.append((phase, facts))


# ---------------------------------------------------- the briefing carries it
def test_a_red_prs_briefing_carries_its_stored_excerpt_and_the_authority_line(
        tmp_path, monkeypatch):
    mod = _load(tmp_path, FLEET_DB=str(tmp_path / "fleet.db"))
    db = _ci_db(tmp_path,
               tasks=[_task(REPO, 3311, "lint", SHA1, "stored")],
               logs=[_log_row(REPO, 3311, LINT_LOG)])
    monkeypatch.setenv("FLEET_DB", str(db))

    # No fleet-ci sync or network function anywhere in the call path.
    real_sibling = mod._load_sibling

    def _spy_sibling(name, mod_name):
        m = real_sibling(name, mod_name)
        if name == "fleet-ci":
            m.sync = lambda *a, **k: pytest.fail("must not sync fleet-ci")
        return m
    monkeypatch.setattr(mod, "_load_sibling", _spy_sibling)

    forge = CIForge(["failure"], heads=[SHA1],
                    statuses=[{"context": "Plans / lint (push)", "status": "failure"}])
    _wire_forge(mod, forge)
    mod.review_bound = lambda repo, path=None: 2
    mod.dispatch_review = lambda *a, **k: pytest.fail("no review on a red build")
    seen = []

    class S:
        timed_out = False
        exit_code = 0
        transcript = None
    mod.run_implementer = lambda wt, prompt, *a, **k: (seen.append(prompt), S())[1]
    fake_log = FakeLog()
    mod.review_loop(REPO, "0069-a", "plans/p.md", "PLAN", 7, "br", tmp_path,
                    None, None, "lease-1", log=fake_log)
    assert seen, "no fix round ran"
    prompt = seen[0]
    assert "--- BEGIN CI LOG (task 3311, Plans / lint (push)) ---" in prompt
    assert "FAILED tests/test_repo.py::test_a" in prompt
    assert "the log is the authority" in prompt.lower()
    assert any(phase == "ci-log-redacted" and facts.get("hits") == 0
              for phase, facts in fake_log.notes)


def test_the_context_prefix_and_the_parenthesised_job_name_survive_the_split(
        tmp_path, monkeypatch):
    """`CI / tests (cihost) (push)` must match the task named `tests (cihost)`,
    not `tests`, and not go unmatched because of the inner parens."""
    mod = _load(tmp_path, FLEET_DB=str(tmp_path / "fleet.db"))
    db = _ci_db(tmp_path,
               tasks=[_task(REPO, 41, "tests (cihost)", SHA1, "stored")],
               logs=[_log_row(REPO, 41, "FAILED tests/x.py::y - Error\n")])
    monkeypatch.setenv("FLEET_DB", str(db))
    entries = mod.poll_ci_log_entries(REPO, SHA1, ["CI / tests (cihost) (push)"],
                                      sleep=lambda s: None)
    context, found = entries[0]
    assert found[0] == 41 and found[1] == "stored"


# --------------------------------------------------------- absence is legible
def test_a_row_stuck_pending_the_whole_window_reads_log_not_yet_stored(tmp_path, monkeypatch):
    mod = _load(tmp_path, FLEET_DB=str(tmp_path / "fleet.db"))
    db = _ci_db(tmp_path, tasks=[_task(REPO, 9, "lint", SHA1, "pending")])
    monkeypatch.setenv("FLEET_DB", str(db))
    slept = []
    entries = mod.poll_ci_log_entries(REPO, SHA1, ["Plans / lint (push)"],
                                      sleep=slept.append, poll=15, bound=45)
    assert slept == [15, 15, 15]                    # waited the whole bound
    block, hits, task_ids, test_ids = mod.render_ci_log_block(SHA1, entries)
    assert "log not yet stored: pending" in block
    assert task_ids == [9] and hits == 0 and test_ids == []


def test_no_row_at_all_names_the_wait_and_never_syncs_or_writes(tmp_path, monkeypatch):
    mod = _load(tmp_path, FLEET_DB=str(tmp_path / "fleet.db"))
    db = _ci_db(tmp_path)                            # table exists, nothing in it
    monkeypatch.setenv("FLEET_DB", str(db))
    before = db.read_bytes()
    entries = mod.poll_ci_log_entries(REPO, SHA1, ["Plans / lint (push)"],
                                      sleep=lambda s: None, poll=15, bound=30)
    block, hits, task_ids, test_ids = mod.render_ci_log_block(SHA1, entries, bound=30)
    assert f"no ci_task row for {SHA1[:8]} after 30 s" in block
    assert "fleet-collect has not swept this failure yet" in block
    assert task_ids == [] and hits == 0
    assert db.read_bytes() == before                 # read-only, start to finish


def test_open_ro_returns_a_connection_that_cannot_write(tmp_path):
    ci = _load_mod("fleet-ci")
    db = _ci_db(tmp_path)
    con = ci.open_ro(str(db))
    assert con is not None
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO ci_task (repo, id) VALUES ('o/r', 1)")


def test_open_ro_is_none_when_the_database_or_the_tables_are_absent(tmp_path):
    ci = _load_mod("fleet-ci")
    assert ci.open_ro(str(tmp_path / "nope.db")) is None
    old = tmp_path / "old.db"
    sqlite3.connect(old).executescript("CREATE TABLE session (id TEXT);")
    assert ci.open_ro(str(old)) is None


# ----------------------------------------------------- a terminal state ends it early
def test_an_over_cap_state_arriving_mid_poll_ends_the_wait_at_once(tmp_path, monkeypatch):
    mod = _load(tmp_path, FLEET_DB=str(tmp_path / "fleet.db"))
    db = _ci_db(tmp_path, tasks=[_task(REPO, 5, "lint", SHA1, "pending")])
    monkeypatch.setenv("FLEET_DB", str(db))
    calls = []

    def sleep(secs):
        calls.append(secs)
        if len(calls) == 1:
            con = sqlite3.connect(db)
            con.execute("UPDATE ci_task SET log_state='over-cap',"
                       " log_detail='over-cap: too big' WHERE id=5")
            con.commit()
            con.close()
    entries = mod.poll_ci_log_entries(REPO, SHA1, ["Plans / lint (push)"],
                                      sleep=sleep, poll=15, bound=210)
    assert len(calls) == 1, "must not keep polling once a terminal state arrives"
    context, found = entries[0]
    assert found == (5, "over-cap", "over-cap: too big")


# ---------------------------------------------------- the second redaction net
def test_a_secret_shaped_string_in_the_stored_body_is_redacted_before_the_prompt(tmp_path):
    mod = _load(tmp_path)
    payload = f"before\nAKIA{'A' * 16}\nafter\n"
    entries = [("tests", (42, "stored", payload))]
    block, hits, task_ids, test_ids = mod.render_ci_log_block(SHA1, entries)
    assert "AKIA" not in block
    assert "[redacted:" in block
    assert hits == 1 and task_ids == [42]


# ------------------------------------------------- D4: the stop reason names it
def test_a_ci_fix_round_that_pushes_nothing_names_the_tests_and_the_task(tmp_path, monkeypatch):
    mod = _load(tmp_path, FLEET_DB=str(tmp_path / "fleet.db"))
    db = _ci_db(tmp_path,
               tasks=[_task(REPO, 3311, "lint", SHA1, "stored")],
               logs=[_log_row(REPO, 3311, LINT_LOG)])
    monkeypatch.setenv("FLEET_DB", str(db))
    forge = CIForge(["failure"], heads=[SHA1],
                    statuses=[{"context": "Plans / lint (push)", "status": "failure"}])
    _wire_forge(mod, forge)
    mod.review_bound = lambda repo, path=None: 2

    class S:
        timed_out = False
        exit_code = 0
        transcript = None
    mod.run_implementer = lambda wt, prompt, *a, **k: S()
    ok, why, rows = mod.review_loop(REPO, "0069-a", "plans/p.md", "PLAN", 7, "br",
                                    tmp_path, None, None, "lease-1")
    assert not ok
    # The pinned sentence bin/mopsus's CLASS_RULES matches, verbatim, unmoved.
    assert f"round 1: fix session left the head at {SHA1[:8]} — nothing was pushed" in why
    assert "tests/test_repo.py::test_a" in why and "tests/test_repo.py::test_b" in why
    assert "fleet-ci task 3311" in why

    mp = _load_mod("mopsus")
    assert mp.classify(why) == "work-exists-unverified"


def test_a_review_triggered_nothing_pushed_reason_is_unchanged(tmp_path):
    """No CI info exists for a review-triggered round: the sentence is exactly
    what it was before this plan, and mopsus's fixture string in
    tests/test_mopsus_classify.py still matches it byte for byte."""
    mod = _load(tmp_path)
    body = "```findings\nHIGH: still broken\n```"
    forge = FakeForge([SHA1], {SHA1: [_review("REQUEST_CHANGES", body)]})

    class S:
        timed_out = False
        exit_code = 0
        transcript = None
    ok, why, rows = _loop(mod, tmp_path, forge, "default = 3\n", lambda *a, **k: S())
    assert not ok
    assert why == f"round 1: fix session left the head at {SHA1[:8]} — nothing was pushed"
