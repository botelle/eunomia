"""mopsus retries a review that stopped at a usage limit (plan 0090, ADR-0016).

Two surfaces are exercised here, in one file, because that is where the
plan's own `paths` lease put them: `bin/mopsus`'s D1-D3/D5-D8 (the held
queue, the probe, the retry, the second-stage classification, the bound and
its page) and `bin/fleet-watch`'s D4 (`orphan_sweep` consuming a
`resume_request`). `tests/test_fleet_watch.py` is NOT leased by this plan —
its shared fixtures (`_load`, `_wire`, `_api_factory`, `_stub_orchestrator`,
`_wait_spawns`, `_orphaned_lease`) are imported rather than restated, the
same precedent plan 0091's Handoff records for `_ForgeDouble`.
"""
import importlib.machinery
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest

from test_fleet_watch import (
    _load as _fw_load,
    _wire as _fw_wire,
    _api_factory,
    _stub_orchestrator,
    _wait_spawns,
)

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def mp():
    return _load("mopsus_review_retry", BIN / "mopsus")


@pytest.fixture(autouse=True)
def _wire(mp, monkeypatch, tmp_path):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    monkeypatch.delenv("FLEET_ANGELIA_URL", raising=False)
    calls = []
    monkeypatch.setattr(mp.orch, "notify",
                        lambda title, body: (calls.append((title, body)), True)[1])
    monkeypatch.setattr(mp.fwatch, "_token", lambda: ("test-token", ""))
    monkeypatch.setattr(mp.fwatch, "repos", lambda: ["operator/sniff"])
    return calls


def _dbpath(tmp_path):
    p = tmp_path / ".fleet" / "fleet.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _seed_attempt(db, repo="operator/sniff", pr=7, head="deadbeef", model="claude-opus-5-5",
                  outcome="quota", session="sess-1", started="2026-09-27T00:00:00Z",
                  ordinal=1, position="code_reviewer"):
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE IF NOT EXISTS review_attempt (
          repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT,
          model TEXT, outcome TEXT NOT NULL, session_uuid TEXT,
          started_at TEXT, stopped_at TEXT, ordinal INTEGER, position TEXT)""")
    con.execute("INSERT INTO review_attempt (repo, pr, head_sha, model, outcome, "
               "session_uuid, started_at, stopped_at, ordinal, position) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (repo, pr, head, model, outcome, session, started, started,
                ordinal, position))
    con.commit()
    con.close()


def _seed_review(db, repo="operator/sniff", pr=7, head="deadbeef", reviewer="revbot",
                 submitted="2026-09-27T01:00:00Z",
                 model="claude-opus-5-5", dismissed=0):
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE IF NOT EXISTS review (
          forgejo_id INTEGER PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL,
          round INTEGER, reviewer TEXT NOT NULL, model TEXT, provenance TEXT,
          verdict TEXT, head_sha TEXT, submitted_at TEXT, session_uuid TEXT,
          dismissed INTEGER DEFAULT 0, stale INTEGER DEFAULT 0)""")
    con.execute("INSERT INTO review (repo, pr, round, reviewer, model, provenance, "
               "verdict, head_sha, submitted_at, session_uuid, dismissed) VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?)",
               (repo, pr, 1, reviewer, model, "spawned-subagent",
                "APPROVE", head, submitted, "sess-2", dismissed))
    con.commit()
    con.close()


class _StubForge:
    """get_pull/list_reviews — what held_review_queue and orch.verdict_at (reused
    by it) call — plus the status and file reads the retry skips make (plan 0169):
    by default every PR is green, carries no `eunomia/deps` status and changes one
    file outside `plans/`."""

    def __init__(self, pulls=None, reviews=None, statuses=None, files=None):
        self.pulls = pulls or {}
        self.reviews = reviews or {}
        self.statuses = statuses or {}      # (repo, sha) -> [status, ...]; None = unreadable
        self.files = files or {}            # (repo, pr) -> [filename, ...]

    def get_commit_status(self, repo, sha):
        got = self.statuses.get((repo, sha), [])
        return (500, None) if got is None else (200, {"statuses": got})

    def list_statuses(self, repo, sha, page=1, limit=50):
        got = self.statuses.get((repo, sha), [])
        if got is None:
            return 500, None
        return 200, (got if page == 1 else [])

    def list_pull_files(self, repo, pr, page_cap=20, limit=50):
        names = self.files.get((repo, pr), ["bin/thing"])
        return 200, [{"filename": n} for n in names]

    def get_pull(self, repo, pr):
        body = self.pulls.get((repo, pr))
        return (404, None) if body is None else (200, body)

    def list_reviews(self, repo, pr, page=1, limit=50):
        if page != 1:
            return 200, []
        return 200, self.reviews.get((repo, pr), [])


def _open_pr(head="deadbeef", draft=False, state="open", body="Plan: 0009-thing"):
    return {"state": state, "draft": draft, "body": body,
            "head": {"sha": head}}


# --------------------------------------------------------------------- D1


def test_held_queue_returns_an_eligible_quota_attempt(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr()})
    mp.fwatch._forge = lambda token: forge
    rows = mp.held_review_queue("tok", db=db)
    assert len(rows) == 1
    assert rows[0]["repo"] == "operator/sniff" and rows[0]["pr"] == 7


def test_held_queue_reads_a_quota_attempt_at_any_ordinal(mp, tmp_path):
    """D5: ADR-0016 §1 as amended by ADR-0013 §4."""
    db = _dbpath(tmp_path)
    _seed_attempt(db, ordinal=2)
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr()})
    mp.fwatch._forge = lambda token: forge
    assert len(mp.held_review_queue("tok", db=db)) == 1


def test_held_queue_skips_a_moved_head(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db, head="deadbeef")
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr(head="c0ffee")})
    mp.fwatch._forge = lambda token: forge
    assert mp.held_review_queue("tok", db=db) == []


def test_held_queue_skips_an_approved_pr(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    forge = _StubForge(
        pulls={("operator/sniff", 7): _open_pr()},
        reviews={("operator/sniff", 7): [
            {"commit_id": "deadbeef", "stale": False, "dismissed": False,
             "user": {"login": "revbot"}}]})
    mp.fwatch._forge = lambda token: forge
    assert mp.held_review_queue("tok", db=db) == []


def test_held_queue_skips_a_draft(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr(draft=True)})
    mp.fwatch._forge = lambda token: forge
    assert mp.held_review_queue("tok", db=db) == []


def test_held_queue_skips_a_later_review_row(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    _seed_review(db)
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr()})
    mp.fwatch._forge = lambda token: forge
    assert mp.held_review_queue("tok", db=db) == []


def test_held_queue_drops_a_row_the_forge_cannot_read(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    forge = _StubForge(pulls={})   # get_pull -> 404
    mp.fwatch._forge = lambda token: forge
    assert mp.held_review_queue("tok", db=db) == []


# ------------------------------------------------------- ADR-0021 (plan 0127)


def _nv_world(mp, tmp_path, monkeypatch, limit=2):
    db = _dbpath(tmp_path)
    mp.fwatch._forge = lambda token: _StubForge(
        pulls={("operator/sniff", 7): _open_pr()})
    monkeypatch.setattr(mp, "review_retry_limit", lambda db=None: limit)
    monkeypatch.setattr(mp, "_review_path_open", lambda q, db=None: q)
    return db


def _set_review_model(db, model):
    con = sqlite3.connect(str(db))
    con.execute("UPDATE review SET model=?", (model,))
    con.commit()
    con.close()


def test_no_verdict_with_the_other_reviewers_later_review_is_retried(mp, tmp_path):
    """DoD 1. The old rule ("any later row at the head supersedes") drops this
    row: the other reviewer's review is a later row at the same head."""
    db = _dbpath(tmp_path)
    _seed_attempt(db, outcome="no-verdict", ordinal=2, model="claude-opus-5-5")
    _seed_review(db, reviewer="revbot")
    _set_review_model(db, "claude-fable-5-1")
    mp.fwatch._forge = lambda token: _StubForge(
        pulls={("operator/sniff", 7): _open_pr()})
    assert len(mp.held_review_queue("tok", db=db)) == 1
    # plan 0168: a quota row is settled the same way, so it is held too
    con = sqlite3.connect(str(db))
    con.execute("UPDATE review_attempt SET outcome='quota'")
    con.commit()
    con.close()
    assert len(mp.held_review_queue("tok", db=db)) == 1


def test_no_verdict_with_its_own_later_review_is_not_retried(mp, tmp_path):
    """DoD 2."""
    db = _dbpath(tmp_path)
    _seed_attempt(db, outcome="no-verdict", ordinal=2, model="claude-opus-5-5")
    _seed_review(db, reviewer="rev2bot")           # ordinal 2's own account
    mp.fwatch._forge = lambda token: _StubForge(
        pulls={("operator/sniff", 7): _open_pr()})
    assert mp.held_review_queue("tok", db=db) == []


def _one_model_world(mp, tmp_path):
    """Both ordinals run claude-opus-5-5 (ADR-0024); a no-verdict row at ordinal 2."""
    db = _dbpath(tmp_path)
    _seed_attempt(db, outcome="no-verdict", ordinal=2, model="claude-opus-5-5")
    mp.fwatch._forge = lambda token: _StubForge(
        pulls={("operator/sniff", 7): _open_pr()})
    return db


def test_one_model_the_other_ordinals_review_does_not_settle_a_no_verdict(mp, tmp_path):
    """Plan 0149 D1: the match is the account, not the model. Matching on
    `model` would settle this row and the PR would wait with no page."""
    db = _one_model_world(mp, tmp_path)
    _seed_review(db, reviewer=mp.lib.agent_roles()["reviewer"].account)
    assert len(mp.held_review_queue("tok", db=db)) == 1


def test_one_model_its_own_accounts_review_settles_a_no_verdict(mp, tmp_path):
    db = _one_model_world(mp, tmp_path)
    _seed_review(db, reviewer=mp.lib.agent_roles()["second_reviewer"].account)
    assert mp.held_review_queue("tok", db=db) == []


def test_a_dismissed_review_from_its_own_account_does_not_settle_a_no_verdict(mp, tmp_path):
    db = _one_model_world(mp, tmp_path)
    _seed_review(db, reviewer=mp.lib.agent_roles()["second_reviewer"].account, dismissed=1)
    assert len(mp.held_review_queue("tok", db=db)) == 1


def test_no_verdict_is_settled_by_a_later_attempt_of_the_same_ordinal_only(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db, outcome="no-verdict", ordinal=2, model="m-b", session="s1")
    _seed_attempt(db, outcome="ok", ordinal=1, model="m-a", session="s2")
    mp.fwatch._forge = lambda token: _StubForge(
        pulls={("operator/sniff", 7): _open_pr()})
    assert len(mp.held_review_queue("tok", db=db)) == 1
    _seed_attempt(db, outcome="ok", ordinal=2, model="m-b", session="s3")
    assert mp.held_review_queue("tok", db=db) == []


def test_no_verdict_retry_runs_no_probe(mp, tmp_path, monkeypatch):
    """DoD 4: neither the sweep's probe gate nor the lock child probes."""
    db = _nv_world(mp, tmp_path, monkeypatch)
    _seed_attempt(db, outcome="no-verdict", ordinal=2)
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(json.loads(payload)))
    monkeypatch.setattr(mp, "_probe_due", lambda *a, **k: False)   # would block a probe
    probed = []
    monkeypatch.setattr(mp, "run_probe", lambda *a, **k: probed.append(a))
    dispatched = []
    monkeypatch.setattr(mp, "_dispatch_session_retry",
                        lambda repo, pr: dispatched.append((repo, pr)))
    mp.review_retry_sweep(db=db)
    assert len(started) == 1
    assert started[0]["reason"] == "no-verdict" and started[0]["limit"] == 1
    payload = dict(started[0], kind="session")
    fd = mp.os.open(str(tmp_path / "lockfd"), mp.os.O_CREAT | mp.os.O_RDWR)
    mp._cmd_review_lock_run([str(fd), json.dumps(payload), str(db)])
    assert probed == []
    assert dispatched == [("operator/sniff", 7)]
    events = (tmp_path / ".fleet" / "events.jsonl").read_text()
    assert '"reason":"no-verdict"' in events


def test_a_second_no_verdict_pages_once_and_does_not_retry(mp, tmp_path, monkeypatch,
                                                           _wire):
    """DoD 3."""
    db = _nv_world(mp, tmp_path, monkeypatch)
    _seed_attempt(db, outcome="no-verdict", ordinal=2, session="sess-A")
    ok, _ = mp._emit_review_requeued(
        "operator/sniff", 7, "deadbeef",
        {"kind": "session", "pr": 7, "head": "deadbeef", "attempt": 1, "limit": 1,
         "reason": "no-verdict"})
    assert ok
    _seed_attempt(db, outcome="no-verdict", ordinal=2, session="sess-B")
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(payload))
    mp.review_retry_sweep(db=db)
    mp.review_retry_sweep(db=db)
    assert started == []
    pages = [t for t in _wire if "operator/sniff" in t[0]]
    assert len(pages) == 1
    assert "deadbeef"[:8] in pages[0][1] and "claude-opus-5-5" in pages[0][1]
    assert "sess-A" in pages[0][1] and "sess-B" in pages[0][1]
    assert "#7" in pages[0][1]


def test_a_no_verdict_retry_in_flight_is_not_paged_and_keeps_the_page_key(
        mp, tmp_path, monkeypatch, _wire):
    """The requeue event is recorded before the retry's own row exists; until a
    second no-verdict row lands nothing is paged, so the real page still goes."""
    db = _nv_world(mp, tmp_path, monkeypatch)
    _seed_attempt(db, outcome="no-verdict", ordinal=2, session="sess-A")
    ok, _ = mp._emit_review_requeued(
        "operator/sniff", 7, "deadbeef",
        {"kind": "session", "pr": 7, "head": "deadbeef", "attempt": 1, "limit": 1,
         "reason": "no-verdict"})
    assert ok
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(payload))
    mp.review_retry_sweep(db=db)
    mp.review_retry_sweep(db=db)
    assert started == []
    assert [t for t in _wire if "operator/sniff" in t[0]] == []
    assert mp._load_state()["review_paged"] == {}
    _seed_attempt(db, outcome="no-verdict", ordinal=2, session="sess-B")
    mp.review_retry_sweep(db=db)
    assert len([t for t in _wire if "operator/sniff" in t[0]]) == 1


def test_a_no_verdict_retry_that_never_recorded_an_attempt_pages_and_frees_the_queue(
        mp, tmp_path, monkeypatch, _wire):
    """Past the in-flight bound, a retry with no second row is a failed retry:
    it pages once, and does not hold the rows behind it."""
    db = _nv_world(mp, tmp_path, monkeypatch)
    _seed_attempt(db, outcome="no-verdict", ordinal=2, session="sess-A")
    ok, _ = mp._emit_review_requeued(
        "operator/sniff", 7, "deadbeef",
        {"kind": "session", "pr": 7, "head": "deadbeef", "attempt": 1, "limit": 1,
         "reason": "no-verdict"})
    assert ok
    late = mp.lib.now_utc() + mp.datetime.timedelta(
        minutes=mp.NO_VERDICT_INFLIGHT_MINUTES + 1)
    monkeypatch.setattr(mp.lib, "now_utc", lambda: late)
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(payload))
    mp.review_retry_sweep(db=db)
    mp.review_retry_sweep(db=db)
    assert started == []
    assert len([t for t in _wire if "operator/sniff" in t[0]]) == 1


def test_a_quota_retry_does_not_spend_the_no_verdict_retry(mp, tmp_path, monkeypatch):
    db = _nv_world(mp, tmp_path, monkeypatch)
    _seed_attempt(db, outcome="no-verdict", ordinal=2)
    ok, _ = mp._emit_review_requeued("operator/sniff", 7, "deadbeef",
                                     {"kind": "session", "pr": 7, "head": "deadbeef",
                                      "attempt": 1, "limit": 2})
    assert ok
    # Backdate past plan 0169's DRAIN_INFLIGHT_MINUTES window: this test is
    # about `_review_requeue_count`'s reason filter (ADR-0021 §2), not about
    # whether a round still running at this head should hold the row back —
    # a fresh requeue for an unrelated ordinal's stop at the SAME head would,
    # correctly, since one retry round settles every stop there (ADR-0028 §3).
    p = tmp_path / ".fleet" / "events.jsonl"
    events = [json.loads(line) for line in p.read_text().splitlines()]
    for ev in events:
        if ev.get("type") == "review-requeued":
            ev["ts"] = "2026-01-01T00:00:00Z"
    p.write_text("\n".join(json.dumps(ev) for ev in events) + "\n")
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(payload))
    mp.review_retry_sweep(db=db)
    assert len(started) == 1


def test_quota_at_ordinal_two_is_held_and_retried_like_ordinal_one(mp, tmp_path, monkeypatch):
    """DoD 5."""
    db = _nv_world(mp, tmp_path, monkeypatch)
    _seed_attempt(db, outcome="quota", ordinal=2)
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(json.loads(payload)))
    mp.review_retry_sweep(db=db)
    assert len(started) == 1 and started[0]["reason"] is None
    assert started[0]["limit"] == 2


def test_a_zero_retry_limit_retries_nothing_of_either_kind(mp, tmp_path, monkeypatch):
    """DoD 6."""
    db = _nv_world(mp, tmp_path, monkeypatch, limit=0)
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(payload))
    for outcome in ("no-verdict", "quota"):
        _seed_attempt(db, outcome=outcome, ordinal=2)
        mp.review_retry_sweep(db=db)
        assert mp.cmd_review_probe(db=db) == 0
    assert started == []


def test_a_no_verdict_row_closes_no_quota_gate(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db, outcome="no-verdict", ordinal=2)
    assert mp._not_superseded_review_attempts(db, ("quota",)) == []


# --------------------------------------------------------------------- D2


def test_probe_reset_is_recorded_and_returned(mp, tmp_path, monkeypatch):
    db = _dbpath(tmp_path)
    stub = tmp_path / "probe.sh"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setattr(mp.orch, "review_command", lambda: [str(stub)])
    outcome = mp.run_probe("claude-opus-5-5", db=db)
    assert outcome == "reset"
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT model, outcome FROM review_probe").fetchone()
    con.close()
    assert row == ("claude-opus-5-5", "reset")


def test_probe_held_is_recorded_and_returned(mp, tmp_path, monkeypatch):
    db = _dbpath(tmp_path)
    stub = tmp_path / "probe.sh"
    stub.write_text("#!/bin/sh\nexit 75\n")
    stub.chmod(0o755)
    monkeypatch.setattr(mp.orch, "review_command", lambda: [str(stub)])
    assert mp.run_probe("claude-opus-5-5", db=db) == "held"


def test_probe_any_other_exit_is_an_error_never_a_reset(mp, tmp_path, monkeypatch):
    db = _dbpath(tmp_path)
    stub = tmp_path / "probe.sh"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    monkeypatch.setattr(mp.orch, "review_command", lambda: [str(stub)])
    assert mp.run_probe("claude-opus-5-5", db=db) == "error"
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT outcome FROM review_probe").fetchone()
    con.close()
    assert row == ("error",)


def test_error_or_held_probe_leaves_the_attempt_row_untouched(mp, tmp_path, monkeypatch):
    """DoD 2: no row changes on a held or an error probe."""
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    stub = tmp_path / "probe.sh"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    monkeypatch.setattr(mp.orch, "review_command", lambda: [str(stub)])
    payload = {"model": "claude-opus-5-5", "repo": "operator/sniff", "pr": 7,
              "head": "deadbeef", "kind": "session", "plan_id": None,
              "lease": None, "limit": 2}
    calls = []
    monkeypatch.setattr(mp, "_attempt_retry", lambda p, db=None: calls.append(p))
    outcome = mp.run_probe(payload["model"], db=db)
    if outcome == "reset":
        mp._attempt_retry(payload, db=db)
    assert outcome == "error"
    assert calls == []
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM review_attempt").fetchone()[0]
    con.close()
    assert n == 1   # the seeded row, unchanged — nothing else written


# --------------------------------------------------------------------- D3


def test_retry_emits_review_requeued_before_acting(mp, tmp_path, monkeypatch):
    dispatched = []
    monkeypatch.setattr(mp, "_dispatch_session_retry",
                        lambda repo, pr: dispatched.append((repo, pr)))
    db = _dbpath(tmp_path)
    payload = {"model": "claude-opus-5-5", "repo": "operator/sniff", "pr": 7,
              "head": "deadbeef", "kind": "session", "plan_id": None,
              "lease": None, "limit": 2}
    mp._attempt_retry(payload, db=db)
    assert dispatched == [("operator/sniff", 7)]
    events = (tmp_path / ".fleet" / "events.jsonl").read_text().splitlines()
    assert len([e for e in events if '"review-requeued"' in e]) == 1


def test_a_failed_emit_means_nothing_runs(mp, tmp_path, monkeypatch):
    dispatched = []
    monkeypatch.setattr(mp, "_dispatch_session_retry",
                        lambda repo, pr: dispatched.append((repo, pr)))
    monkeypatch.setattr(mp, "_emit_review_requeued",
                        lambda repo, pr, head, detail: (False, "boom"))
    db = _dbpath(tmp_path)
    payload = {"model": "claude-opus-5-5", "repo": "operator/sniff", "pr": 7,
              "head": "deadbeef", "kind": "session", "plan_id": None,
              "lease": None, "limit": 2}
    mp._attempt_retry(payload, db=db)
    assert dispatched == []


def test_plan_kind_writes_a_resume_request_and_spawns_nothing(mp, tmp_path, monkeypatch):
    dispatched = []
    monkeypatch.setattr(mp, "_dispatch_session_retry",
                        lambda repo, pr: dispatched.append((repo, pr)))
    db = _dbpath(tmp_path)
    payload = {"model": "claude-opus-5-5", "repo": "operator/sniff", "pr": 7,
              "head": "deadbeef", "kind": "plan", "plan_id": "0009-thing",
              "lease": "orch-0009-thing-abcd1234", "limit": 2}
    mp._attempt_retry(payload, db=db)
    assert dispatched == []
    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT plan, lease, pr, head, consumed_at FROM resume_request").fetchone()
    con.close()
    assert row == ("0009-thing", "orch-0009-thing-abcd1234", 7, "deadbeef", None)


# --------------------------------------------------------------------- D6


def test_at_the_retry_limit_a_handle_is_paged_and_nothing_retries(mp, tmp_path, monkeypatch):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr()})
    mp.fwatch._forge = lambda token: forge
    monkeypatch.setattr(mp, "review_retry_limit", lambda db=None: 2)
    monkeypatch.setenv("EUNOMIA_SESSION", "mopsus-test")
    # Seed two prior review-requeued events directly on the ledger (the
    # authority _review_requeue_count reads first).
    for i in range(2):
        mp.subprocess.run(
            [mp.sys.executable, str(BIN / "fleet-emit"), "review-requeued",
             "--repo", "operator/sniff", "--pr", "7", "--sha", "deadbeef",
             "--detail-json", json.dumps({"kind": "session", "repo": "operator/sniff",
                                          "pr": 7, "head": "deadbeef",
                                          "plan": None, "lease": None,
                                          "attempt": i + 1, "limit": 2})],
            env=mp._clean_env(), check=True)
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(payload))
    mp.review_retry_sweep(db=db)
    assert started == []
    state = mp._load_state()
    assert any(k.startswith("operator/sniff#7@") for k in state["review_paged"])
    # idempotent — a second sweep does not page again, and still no retry
    mp.review_retry_sweep(db=db)
    assert started == []


# --------------------------------------------------------------------- D7


def test_review_probe_reports_nothing_held_when_the_queue_is_empty(mp, tmp_path):
    db = _dbpath(tmp_path)
    started = time.monotonic()
    rc = mp.cmd_review_probe(db=db)
    assert rc == 0
    assert time.monotonic() - started < 2.0


def test_review_probe_reports_started(mp, tmp_path, monkeypatch, capsys):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr()})
    mp.fwatch._forge = lambda token: forge
    monkeypatch.setattr(mp, "_try_start_locked", lambda payload, db=None: True)
    started = time.monotonic()
    rc = mp.cmd_review_probe(db=db)
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out
    assert rc == 0
    assert "started" in out
    assert elapsed < 2.0


def test_review_probe_reports_busy_when_the_lock_is_held(mp, tmp_path, monkeypatch, capsys):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr()})
    mp.fwatch._forge = lambda token: forge
    monkeypatch.setattr(mp, "_try_start_locked", lambda payload, db=None: False)
    rc = mp.cmd_review_probe(db=db)
    out = capsys.readouterr().out
    assert rc == 0
    assert "busy" in out


def test_review_probe_scans_at_most_the_cap(mp, tmp_path, monkeypatch, capsys):
    """plan 0169: a long run of red rows at the front of the held queue must
    not turn D2's one forge read per candidate into an unbounded scan."""
    db = _dbpath(tmp_path)
    n = mp.PROBE_SCAN_LIMIT + 9
    pulls = {}
    statuses = {}
    for pr in range(1, n + 1):
        head = f"h{pr:07d}"
        _seed_attempt(db, pr=pr, head=head, started=f"2026-09-27T00:00:{pr:02d}Z")
        pulls[("operator/sniff", pr)] = _open_pr(head=head)
        if pr < n:                          # every row but the last is red
            statuses[("operator/sniff", head)] = [
                {"context": "ci/tests", "status": "failure"}]
    forge = _StubForge(pulls=pulls, statuses=statuses)
    calls = []
    real = forge.get_commit_status
    monkeypatch.setattr(forge, "get_commit_status",
                        lambda repo, sha: (calls.append(sha), real(repo, sha))[1])
    mp.fwatch._forge = lambda token: forge
    monkeypatch.setattr(mp, "_try_start_locked", lambda payload, db=None: True)
    rc = mp.cmd_review_probe(db=db)
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing-held" in out              # the one dispatchable row is past the cap
    assert len(calls) == mp.PROBE_SCAN_LIMIT


# --------------------------------------------------------------------- D9


def test_no_argv_or_output_carries_a_token(mp, tmp_path, monkeypatch):
    sentinel = "SECRET-TOKEN-VALUE-DO-NOT-LEAK"
    monkeypatch.setenv("FLEET_TOKEN_CMD", f"echo {sentinel}")
    monkeypatch.setenv("FLEET_ADMIN_TOKEN_CMD", f"echo {sentinel}")
    env = mp._clean_env()
    assert "FLEET_TOKEN_CMD" not in env
    assert "FLEET_ADMIN_TOKEN_CMD" not in env
    assert "FLEET_DIRECT_TOKEN_CMD" not in env

    db = _dbpath(tmp_path)
    payload = {"model": "claude-opus-5-5", "repo": "operator/sniff", "pr": 7,
              "head": "deadbeef", "kind": "session", "plan_id": None,
              "lease": None, "limit": 2}
    monkeypatch.setattr(mp, "_dispatch_session_retry", lambda repo, pr: None)
    mp._attempt_retry(payload, db=db)
    events = (tmp_path / ".fleet" / "events.jsonl").read_text()
    assert sentinel not in events


# --------------------------------------------------------------------- D5


def test_review_quota_reclass_only_widens_an_unclassified_round_n_failure(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db)
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr()})
    mp.fwatch._forge = lambda token: forge
    mp.fwatch._token = lambda: ("tok", "")
    rows = [{"class": "unclassified", "pr": 7, "repo": "operator/sniff",
            "failure_reason": "round 3: review dispatch failed: FATAL: quota"}]
    mp._apply_review_quota_reclass(rows, db)
    assert rows[0]["class"] == "review-quota"


def test_review_quota_reclass_leaves_an_already_classified_row_alone(mp, tmp_path):
    db = _dbpath(tmp_path)
    rows = [{"class": "timeout", "pr": 7, "repo": "operator/sniff",
            "failure_reason": "round 3: review dispatch failed: whatever"}]
    mp._apply_review_quota_reclass(rows, db)
    assert rows[0]["class"] == "timeout"


def test_review_quota_reclass_needs_an_eligible_queue_row(mp, tmp_path):
    db = _dbpath(tmp_path)
    mp.fwatch._forge = lambda token: _StubForge(pulls={})
    mp.fwatch._token = lambda: ("tok", "")
    rows = [{"class": "unclassified", "pr": 7, "repo": "operator/sniff",
            "failure_reason": "round 3: review dispatch failed: FATAL: quota"}]
    mp._apply_review_quota_reclass(rows, db)
    assert rows[0]["class"] == "unclassified"


# --------------------------------------------------------------------- fleet-config


def test_review_retry_limit_bounds(mp):
    fc = _load("fleet_config_rr", BIN / "fleet-config")
    assert fc._validate_review_retry_limit(0) == 0
    assert fc._validate_review_retry_limit("5") == 5
    with pytest.raises(ValueError):
        fc._validate_review_retry_limit(6)
    with pytest.raises(ValueError):
        fc._validate_review_retry_limit(-1)


def test_review_probe_minutes_bounds_and_cli_refusal(mp, tmp_path, monkeypatch, capsys):
    """DoD 7: `fleet-config set review_probe_minutes 5` is refused."""
    fc = _load("fleet_config_rp", BIN / "fleet-config")
    assert fc._validate_review_probe_minutes(15) == 15
    assert fc._validate_review_probe_minutes(1440) == 1440
    with pytest.raises(ValueError):
        fc._validate_review_probe_minutes(5)
    with pytest.raises(ValueError):
        fc._validate_review_probe_minutes(1441)

    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    fc.DB = str(tmp_path / "fleet.db")
    rc = fc.main(["set", "review_probe_minutes", "5"])
    assert rc != 0


# --------------------------------------------------------------------- D4 (fleet-watch)


def _seed_resume_request(fleet_dir, plan_id, lease, pr, head, requested_at,
                         consumed_at=None):
    db = Path(fleet_dir) / "fleet.db"
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE IF NOT EXISTS resume_request (
          plan TEXT NOT NULL, lease TEXT NOT NULL, pr INTEGER, head TEXT,
          requested_at TEXT NOT NULL, consumed_at TEXT)""")
    con.execute("INSERT INTO resume_request (plan, lease, pr, head, "
               "requested_at, consumed_at) VALUES (?,?,?,?,?,?)",
               (plan_id, lease, pr, head, requested_at, consumed_at))
    con.commit()
    con.close()


def _orphaned_lease_local(mod, fd, plan_id="0009-thing"):
    """The same shape as test_fleet_watch.py's own `_orphaned_lease` (not
    imported: it reaches for that file's own `fd.parent / 'spawns.log'`
    convention through a closure over a stub orchestrator path this file
    sets up identically)."""
    mod.spawn("operator/sniff", f"plans/{plan_id}.md", {"id": plan_id})
    _wait_spawns(fd.parent / "spawns.log", 1, timeout=1.5)
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    p = fd / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text())
    rec["created"] = "2020-01-01T00:00:00Z"
    p.write_text(json.dumps(rec))
    return lid


def test_resume_request_triggers_a_resumed_respawn(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _fw_load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    _fw_wire(mod, _api_factory(pulls=pulls), orch)
    dead = _orphaned_lease_local(mod, fd)
    _seed_resume_request(fd, "0009-thing", dead, 7, "deadbeef",
                         "2030-01-01T00:00:00Z")
    mod.notify = lambda t, b: None
    assert mod.cycle() == 1
    spawns = _wait_spawns(log, 2)
    assert spawns[-1].startswith("--resume ")
    con = sqlite3.connect(str(fd / "fleet.db"))
    row = con.execute(
        "SELECT consumed_at FROM resume_request WHERE lease=?", (dead,)).fetchone()
    con.close()
    assert row[0] is not None


def test_resume_request_for_a_different_lease_does_nothing(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _fw_load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    _fw_wire(mod, _api_factory(pulls=pulls), orch)
    _orphaned_lease_local(mod, fd)
    _seed_resume_request(fd, "0009-thing", "some-other-lease-id", 7, "deadbeef",
                         "2030-01-01T00:00:00Z")
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0
    assert len(_wait_spawns(log, 1)) == 1


def test_resume_request_older_than_dispatch_does_nothing(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _fw_load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    _fw_wire(mod, _api_factory(pulls=pulls), orch)
    dead = _orphaned_lease_local(mod, fd)   # created 2020-01-01
    _seed_resume_request(fd, "0009-thing", dead, 7, "deadbeef",
                         "2019-01-01T00:00:00Z")
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0
    assert len(_wait_spawns(log, 1)) == 1


def test_resume_request_already_consumed_does_nothing(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _fw_load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    _fw_wire(mod, _api_factory(pulls=pulls), orch)
    dead = _orphaned_lease_local(mod, fd)
    _seed_resume_request(fd, "0009-thing", dead, 7, "deadbeef",
                         "2030-01-01T00:00:00Z", consumed_at="2030-01-02T00:00:00Z")
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0
    assert len(_wait_spawns(log, 1)) == 1
