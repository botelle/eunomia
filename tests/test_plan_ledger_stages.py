"""Tests for `fleet-plan ledger`'s stage timeline (plan 0196).

Uses the same fleet-collect-schema-backed `Db` pattern as test_plan_ledger.py
(a hand-made `dispatch_phase`/`dispatch_session` fixture would drift silently
from the real views), extended with the three tables plan 0196 reads beyond
`fleet-collect`'s own: `review`, `review_attempt`, `lane_run`.

`fleetlib`/`orchestrator` are faked (`FakeFleetlib`/`FakeOrch`) rather than
loaded for real: the real ones read this HOST's `~/dev/.fleet/leases/` and
this repo's `config/impl-bounds.conf` respectively, neither of which a unit
test may depend on.
"""
import datetime
import importlib.machinery
import importlib.util
import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
FLEET_PLAN = BIN / "fleet-plan"


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fp = _load(FLEET_PLAN, "fleet_plan_stages")
fc = _load(BIN / "fleet-collect", "fleet_collect_for_stages")

REPO = "operator/eunomia"
NOW = datetime.datetime(2026, 11, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------- the fixtures

def _plan_text(pid, status="ready", repo=REPO):
    return f"""---
id: {pid}
status: {status}
repo: {repo}
zone: public
tier: 2
paths:
  - "bin/x"
---

## 1. Goal

g
"""


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_repo(tmp_path, ids):
    """A tiny real git checkout with every plan committed in ONE commit, so
    every plan's `approved` stage reads that commit's own committer date."""
    repo = tmp_path / f"checkout-{uuid.uuid4().hex[:8]}"
    (repo / "plans").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    files = []
    for pid, status in ids:
        f = repo / "plans" / f"{pid}.md"
        f.write_text(_plan_text(pid, status))
        files.append(f)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "file plans")
    return repo, files


class Db:
    """A fleet.db with the real `dispatch`/`dispatch_session`/`dispatch_cost`
    views, plus hand-made `review`, `review_attempt` and `lane_run` tables in
    the shape their own owners (`bin/fleet-reviews`, talos, `bin/fleet-lane`)
    create — the same shape `tests/fakes/review.py` and
    `tests/test_mopsus_review_retry.py` already use for `review_attempt`."""

    def __init__(self, tmp_path, name="fleet.db"):
        self.path = tmp_path / name
        self.con = sqlite3.connect(str(self.path))
        self.con.executescript(fc.SCHEMA)
        self.con.executescript(fc.DISPATCH_VIEW)
        self.con.executescript(fc.DISPATCH_SESSION_VIEW)
        self.con.executescript(fc.DISPATCH_COST_VIEW)
        self.con.execute("""CREATE TABLE review (
            forgejo_id INTEGER PRIMARY KEY, repo TEXT, pr INTEGER, round INTEGER,
            reviewer TEXT, verdict TEXT, submitted_at TEXT, session_uuid TEXT)""")
        self.con.execute("""CREATE TABLE review_attempt (
            repo TEXT, pr INTEGER, head_sha TEXT, model TEXT, outcome TEXT,
            session_uuid TEXT, started_at TEXT, stopped_at TEXT, ordinal INTEGER,
            position TEXT)""")
        self.con.execute("""CREATE TABLE lane_run (
            run_id TEXT PRIMARY KEY, repo TEXT, plan TEXT, runner TEXT,
            started_at TEXT, ended_at TEXT, spawned INTEGER, outcome TEXT,
            usage_json TEXT)""")
        self._line = 0
        self._review_id = 0

    def _phase(self, lease, seq, ts, phase, pr=None, seconds=None, **facts):
        self._line += 1
        self.con.execute(
            "INSERT INTO dispatch_phase VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lease, seq, ts, phase, pr, None, None, seconds, None, None, None,
             json.dumps(facts), f"logs/{lease}.log", self._line))

    def _event(self, ts, etype, lease, repo, **detail):
        self._line += 1
        self.con.execute(
            "INSERT INTO event VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, "watch", etype, repo, None, None, None,
             json.dumps({"lease": lease, **detail}), "events.jsonl", self._line))

    def run(self, plan, outcome, repo=REPO, n=1, pr=None, rounds=0, impl=(1000,),
            reason=None, day="2026-09-10", lease=None, review_stopped=None):
        """One dispatch. `outcome` is merged | closed | failed | None (no
        terminal record). `review_stopped`, if given, is the detail text of
        a `review-stopped` phase written after the review rounds."""
        lease = lease or f"branch--feat-{plan}--{n:03d}"
        ts = f"{day}T10:00:00Z"
        seq = 1
        self._phase(lease, seq, ts, "start", repo=repo, plan=plan, session="s")
        for secs in impl:
            seq += 1
            self._phase(lease, seq, ts, "implementer-exit", seconds=secs)
        for r in range(rounds):
            seq += 1
            self._phase(lease, seq, ts, "review-round", round=str(r + 1))
        if review_stopped is not None:
            seq += 1
            self._phase(lease, seq, ts, "review-stopped", detail=review_stopped, rounds=rounds)
        if pr is not None:
            seq += 1
            self._phase(lease, seq, ts, "verified", pr=pr)
        if outcome in ("merged", "closed"):
            seq += 1
            self._phase(lease, seq, f"{day}T11:00:00Z", "steward-end", state=outcome)
        elif outcome == "failed":
            self._event(f"{day}T11:00:00Z", "plan-failed", lease, repo,
                        reason=reason or "boom")
        return self

    def review_round(self, pr, round_n, verdict, submitted_at, repo=REPO, session_uuid=None):
        self._review_id += 1
        self.con.execute(
            "INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict, "
            "submitted_at, session_uuid) VALUES (?,?,?,?,?,?,?,?)",
            (self._review_id, repo, pr, round_n, "revbot", verdict, submitted_at,
             session_uuid))
        return self

    def review_attempt(self, pr, outcome, started_at, stopped_at, ordinal=1, repo=REPO):
        self.con.execute(
            "INSERT INTO review_attempt (repo, pr, head_sha, model, outcome, "
            "session_uuid, started_at, stopped_at, ordinal, position) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (repo, pr, "a" * 40, "claude-opus-5-5", outcome, None, started_at,
             stopped_at, ordinal, "code_reviewer"))
        return self

    def lane_run(self, plan, runner, outcome, started_at, ended_at=None,
                usage_json=None, repo=REPO):
        self.con.execute(
            "INSERT INTO lane_run (run_id, repo, plan, runner, started_at, "
            "ended_at, spawned, outcome, usage_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), repo, plan, runner, started_at, ended_at, 1,
             outcome, usage_json))
        return self

    def commit(self):
        self.con.commit()
        self.con.close()
        return self.path


class FakeFleetlib:
    """`read_lease`/`is_orphaned` for the leases named `live` (an `active`
    lease whose heartbeat is fresh) or `dead` (an `active` lease `is_orphaned`
    says has gone silent — the 0179 shape Boundary 4 names). Any other lease
    id reads as released/absent: `read_lease` returns `None`.

    `lane_live`/`lane_dead` are lane leases, named the way `_lane_leases`
    reads them (`lane:<plan_id>/<runner>`) and surfaced only through
    `all_leases()`/`resource_of()` — the sweep shape, not `read_lease`, since
    a `lane_run` row carries no lease id of its own to look up by."""

    def __init__(self, live=(), dead=(), lane_live=(), lane_dead=(), repo=REPO):
        self.live = set(live)
        self.dead = set(dead)
        self.lane_live = set(lane_live)
        self.lane_dead = set(lane_dead)
        self.repo = repo

    def read_lease(self, lease_id):
        if lease_id in self.live or lease_id in self.dead:
            return {"id": lease_id, "state": "active"}
        return None

    def is_orphaned(self, rec):
        if rec.get("id") is not None:
            return rec.get("id") in self.dead
        return rec.get("note") in self.lane_dead

    def all_leases(self):
        return [{"note": note, "state": "active", "resource": {"repo": self.repo}}
                for note in self.lane_live | self.lane_dead]

    def resource_of(self, rec):
        res = rec.get("resource")
        return res if isinstance(res, dict) else {}


class FakeOrch:
    REVIEW_DISPATCH_TIMEOUT = 2400

    def __init__(self, impl_minutes=90, review_timeout=2400):
        self.impl_minutes = impl_minutes
        self.review_timeout = review_timeout

    def impl_bound(self, repo):
        return self.impl_minutes, "conf-default"

    def review_position(self, plan_text):
        return "plan_reviewer"

    def _review_dispatch_timeout(self, repo, position):
        return self.review_timeout


def _ledger(tmp_path, specs, db, fleetlib=None, orch=None, with_filings=False,
           filing_lookup=None, open_filings_lookup=None, now=NOW):
    repo_root, files = _git_repo(tmp_path, specs)
    return fp.ledger(files, db=db, repo=REPO, repo_root=repo_root, ref="main",
                     fleetlib_mod=fleetlib or FakeFleetlib(), orch_mod=orch or FakeOrch(),
                     now=now, with_filings=with_filings, filing_lookup=filing_lookup,
                     open_filings_lookup=open_filings_lookup)


def _rows(data):
    return {r["id"]: r for r in data["rows"]}


def _stages(data, pid):
    return _rows(data)[pid]["stages"]


# --------------------------------------------------------------------- D2/D3

def test_a_plan_through_all_six_stages_completed(tmp_path):
    db = Db(tmp_path)
    db.run("0001-a", "merged", pr=10, rounds=1, impl=(1000,), day="2026-09-10")
    db.review_round(10, 1, "APPROVED", "2026-09-10T10:30:00Z")
    data = _ledger(tmp_path, [("0001-a", "done")], db.commit())
    stages = _stages(data, "0001-a")
    assert [stages[n]["state"] for n in fp.STAGE_NAMES] == \
        ["unknown", "completed", "completed", "completed", "empty", "completed"]
    assert stages["approved"]["started_at"] == stages["approved"]["completed_at"]
    assert stages["implementation"]["attempts"] == 1
    assert stages["review"]["completed_at"] is not None
    assert stages["merged"]["completed_at"] is not None


def test_a_plan_failed_at_implementation_with_no_pr_names_the_reason(tmp_path):
    db = Db(tmp_path)
    db.run("0002-a", "failed", reason="dependency install failed")
    data = _ledger(tmp_path, [("0002-a", "ready")], db.commit())
    impl = _stages(data, "0002-a")["implementation"]
    assert impl["state"] == "failed"
    assert impl["reason"] == "dependency install failed"
    assert impl["failed_at"] is not None and impl["completed_at"] is None


def test_a_plan_failed_at_review(tmp_path):
    db = Db(tmp_path)
    db.run("0003-a", None, pr=11, rounds=1, lease="branch--feat-0003-a--001",
          day="2026-09-10")
    db.review_round(11, 1, "COMMENT", "2026-09-10T10:30:00Z")
    data = _ledger(tmp_path, [("0003-a", "ready")], db.commit(),
                   fleetlib=FakeFleetlib(dead=["branch--feat-0003-a--001"]))
    review = _stages(data, "0003-a")["review"]
    assert review["state"] == "failed"
    assert review["reason"] == "COMMENT"


def test_a_plan_whose_test_lane_failed(tmp_path):
    db = Db(tmp_path)
    db.run("0004-a", "merged", pr=12)
    db.lane_run("0004-a", "sub-opus", "bad-result", "2026-09-10T12:00:00Z",
               "2026-09-10T12:05:00Z")
    data = _ledger(tmp_path, [("0004-a", "done")], db.commit())
    test = _stages(data, "0004-a")["test"]
    assert test["state"] == "failed"
    assert test["reason"] == "lane sub-opus: bad-result"
    assert test["attempts"] == 1


def _sess(con, uuid_, project, first, last, turns, in_tok=1000, out=30_000,
         cache_read=0, cache_write=0, parent=None):
    """A `session` row linkable by `dispatch_session`/`review.session_uuid` —
    the same shape test_plan_ledger.py's own `_sess` writes."""
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"{project}/{uuid_}.jsonl", uuid_, project, parent,
                "subagent" if parent else "session", first, last, turns, 0,
                in_tok, out, cache_read, cache_write, "[]", "", ""))


def test_a_retried_plan_sums_attempts_across_implementation_review_and_test(tmp_path):
    """Definition of done: a retried plan's implementation, review and test
    stages are each `completed`, with `attempts` (2 for implementation and
    test; review's own review-stopped-then-approved shape is 2 attempts
    too), `started_at` from the first attempt, and `seconds`/`tokens` equal
    to the sum of both (finding, review of #1472: the previous version of
    this test asserted none of review's attempts, or any token sum)."""
    db = Db(tmp_path)
    cwd1, cwd2 = "/tmp/fake-0005-1", "/tmp/fake-0005-2"
    lease1, lease2 = "branch--feat-0005-a--001", "branch--feat-0005-a--002"

    db._phase(lease1, 1, "2026-09-10T10:00:00Z", "start", repo=REPO, plan="0005-a",
              session="s")
    db._phase(lease1, 2, "2026-09-10T10:00:00Z", "implementer-start", cwd=cwd1)
    db._phase(lease1, 3, "2026-09-10T10:08:20Z", "implementer-exit", seconds=500)
    db._event("2026-09-10T11:00:00Z", "plan-failed", lease1, REPO,
              reason="implementer crashed")
    _sess(db.con, "uuid-impl-1", fc.fleet_slug(cwd1),
         "2026-09-10T10:00:05.000Z", "2026-09-10T10:08:15.000Z", 5,
         in_tok=1000, out=2000, cache_read=500)

    db._phase(lease2, 1, "2026-09-12T10:00:00Z", "start", repo=REPO, plan="0005-a",
              session="s")
    db._phase(lease2, 2, "2026-09-12T10:00:00Z", "implementer-start", cwd=cwd2)
    db._phase(lease2, 3, "2026-09-12T10:25:00Z", "implementer-exit", seconds=1500)
    db._phase(lease2, 4, "2026-09-12T10:25:00Z", "review-round", round="1")
    db._phase(lease2, 5, "2026-09-12T10:25:00Z", "review-stopped",
              detail="round 1: timed out", rounds=1)
    db._phase(lease2, 6, "2026-09-12T10:25:00Z", "verified", pr=13)
    db._phase(lease2, 7, "2026-09-12T11:00:00Z", "steward-end", state="merged")
    _sess(db.con, "uuid-impl-2", fc.fleet_slug(cwd2),
         "2026-09-12T10:00:05.000Z", "2026-09-12T10:24:55.000Z", 15,
         in_tok=3000, out=4000, cache_read=1500)

    _sess(db.con, "uuid-review-2", "dispatch-review-project",
         "2026-09-12T10:40:00.000Z", "2026-09-12T10:44:00.000Z", 3,
         in_tok=500, out=200, cache_read=100)
    db.review_round(13, 2, "APPROVED", "2026-09-12T10:45:00Z", session_uuid="uuid-review-2")

    db.lane_run("0005-a", "sub-opus", "bad-result", "2026-09-12T11:00:00Z",
               "2026-09-12T11:05:00Z",
               usage_json=json.dumps({"usage": {"input_tokens": 10, "output_tokens": 5,
                                                "cache_read_input_tokens": 1}}))
    db.lane_run("0005-a", "sub-opus", "pr-opened", "2026-09-12T12:00:00Z",
               "2026-09-12T12:10:00Z",
               usage_json=json.dumps({"usage": {"input_tokens": 20, "output_tokens": 15,
                                                "cache_read_input_tokens": 2}}))

    data = _ledger(tmp_path, [("0005-a", "done")], db.commit())
    stages = _stages(data, "0005-a")

    impl = stages["implementation"]
    assert impl["state"] == "completed" and impl["attempts"] == 2
    assert impl["started_at"] == "2026-09-10T10:00:00Z"
    assert impl["seconds"] == 500 + 1500
    assert impl["tokens"] == {"input": 4000, "output": 6000, "cache_read": 2000,
                              "source": "session"}

    review = stages["review"]
    assert review["state"] == "completed" and review["attempts"] == 2
    assert review["started_at"] == "2026-09-12T10:25:00Z"
    assert review["tokens"] == {"input": 500, "output": 200, "cache_read": 100,
                               "source": "partial"}

    test = stages["test"]
    assert test["state"] == "completed" and test["attempts"] == 2
    assert test["seconds"] == 300 + 600
    assert test["tokens"] == {"input": 10 + 20, "output": 5 + 15, "cache_read": 1 + 2,
                              "source": "lane"}


def test_a_lane_that_passed_then_failed_on_its_newest_run_reads_failed(tmp_path):
    db = Db(tmp_path)
    db.run("0006-a", "merged", pr=14)
    db.lane_run("0006-a", "sub-opus", "pr-opened", "2026-09-10T10:00:00Z",
               "2026-09-10T10:05:00Z")
    db.lane_run("0006-a", "sub-opus", "timeout", "2026-09-11T10:00:00Z",
               "2026-09-11T10:20:00Z")
    data = _ledger(tmp_path, [("0006-a", "done")], db.commit())
    test = _stages(data, "0006-a")["test"]
    assert test["state"] == "failed"
    assert test["attempts"] == 2


def test_two_lanes_disagree_failed_names_the_failing_lane(tmp_path):
    db = Db(tmp_path)
    db.run("0007-a", "merged", pr=15)
    db.lane_run("0007-a", "sub-antigravity", "pr-opened", "2026-09-10T10:00:00Z",
               "2026-09-10T10:05:00Z")
    db.lane_run("0007-a", "sub-opus", "bad-result", "2026-09-10T10:00:00Z",
               "2026-09-10T10:05:00Z")
    data = _ledger(tmp_path, [("0007-a", "done")], db.commit())
    test = _stages(data, "0007-a")["test"]
    assert test["state"] == "failed"
    assert test["reason"] == "lane sub-opus: bad-result"


def test_two_lanes_one_passed_one_still_running_reads_running(tmp_path):
    db = Db(tmp_path)
    db.run("0008-a", "merged", pr=16)
    db.lane_run("0008-a", "sub-antigravity", "pr-opened", "2026-09-10T10:00:00Z",
               "2026-09-10T10:05:00Z")
    db.lane_run("0008-a", "sub-opus", "running", "2026-09-10T10:00:00Z")
    live = FakeFleetlib(lane_live={"lane:0008-a/sub-opus"})
    data = _ledger(tmp_path, [("0008-a", "done")], db.commit(), fleetlib=live)
    test = _stages(data, "0008-a")["test"]
    assert test["state"] == "running"


def test_a_dead_lane_lease_is_not_reported_running(tmp_path):
    """Boundary 4, for a lane: a `lane_run` row stored `outcome='running'`
    with no live lease behind it (its holder died uncontrolled) is a dead,
    failed attempt — never `running` forever (finding, review of #1472)."""
    db = Db(tmp_path)
    db.run("0019-a", None, pr=23)
    db.lane_run("0019-a", "sub-opus", "running", "2026-09-10T10:00:00Z")
    dead = FakeFleetlib(lane_dead={"lane:0019-a/sub-opus"})
    data = _ledger(tmp_path, [("0019-a", "ready")], db.commit(), fleetlib=dead)
    test = _stages(data, "0019-a")["test"]
    assert test["state"] == "failed"
    assert "not live" in test["reason"]


def test_a_first_implementation_attempt_still_running(tmp_path):
    db = Db(tmp_path)
    db.run("0009-a", None, lease="branch--feat-0009-a--001", day="2026-11-10")
    live = FakeFleetlib(live=["branch--feat-0009-a--001"])
    data = _ledger(tmp_path, [("0009-a", "ready")], db.commit(), fleetlib=live)
    impl = _stages(data, "0009-a")["implementation"]
    assert impl["state"] == "running"
    assert impl["seconds"] is None and impl["tokens"]["source"] == "not recorded"

    near = _ledger(tmp_path, [("0009-a", "ready")], Db(tmp_path, "near.db").run(
        "0009-a", None, lease="branch--feat-0009-a--001", day="2026-11-10").commit(),
        fleetlib=live,
        now=datetime.datetime(2026, 11, 10, 10, 10, 0, tzinfo=datetime.timezone.utc))
    assert _stages(near, "0009-a")["implementation"]["over_budget"] is False

    late = _ledger(tmp_path, [("0009-a", "ready")], Db(tmp_path, "late.db").run(
        "0009-a", None, lease="branch--feat-0009-a--001", day="2026-11-10").commit(),
        fleetlib=live,
        now=datetime.datetime(2026, 11, 10, 12, 0, 0, tzinfo=datetime.timezone.utc))
    assert _stages(late, "0009-a")["implementation"]["over_budget"] is True


def test_a_dead_lease_is_not_reported_running(tmp_path):
    db = Db(tmp_path)
    db.run("0010-a", None, lease="branch--feat-0010-a--001", day="2026-09-10")
    dead = FakeFleetlib(dead=["branch--feat-0010-a--001"])
    data = _ledger(tmp_path, [("0010-a", "ready")], db.commit(), fleetlib=dead)
    impl = _stages(data, "0010-a")["implementation"]
    assert impl["state"] != "running"
    assert impl["state"] == "failed"


def test_a_non_claude_lane_has_null_tokens_and_not_recorded(tmp_path):
    db = Db(tmp_path)
    db.run("0011-a", "merged", pr=17)
    db.lane_run("0011-a", "sub-codex", "pr-opened", "2026-09-10T10:00:00Z",
               "2026-09-10T10:05:00Z",
               usage_json=json.dumps({"input_tokens": 100, "cached_input_tokens": 10,
                                      "output_tokens": 20}))
    data = _ledger(tmp_path, [("0011-a", "done")], db.commit())
    tok = _stages(data, "0011-a")["test"]["tokens"]
    assert tok == {"input": None, "output": None, "cache_read": None,
                   "source": "not recorded"}


def test_a_claude_lane_parses_its_own_usage_shape(tmp_path):
    db = Db(tmp_path)
    db.run("0012-a", "merged", pr=18)
    db.lane_run("0012-a", "sub-opus", "pr-opened", "2026-09-10T10:00:00Z",
               "2026-09-10T10:05:00Z",
               usage_json=json.dumps({"usage": {
                   "input_tokens": 100, "cache_read_input_tokens": 10,
                   "output_tokens": 20}}))
    data = _ledger(tmp_path, [("0012-a", "done")], db.commit())
    tok = _stages(data, "0012-a")["test"]["tokens"]
    assert tok == {"input": 100, "output": 20, "cache_read": 10, "source": "lane"}


def test_a_ledger_run_without_forge_flags_leaves_submitted_unknown(tmp_path):
    db = Db(tmp_path)
    db.run("0013-a", "merged", pr=19)
    data = _ledger(tmp_path, [("0013-a", "done")], db.commit())
    submitted = _stages(data, "0013-a")["submitted"]
    assert submitted["state"] == "unknown"
    assert submitted["reason"] == "forge not read"
    assert _rows(data)["0013-a"]["submitted_by"] is None


def test_verdict_confirmed_merged_stage_reads_the_forges_merge_time(tmp_path):
    """Finding, review of #1472: `--verdict`'s own forge read nulls
    `pr_as_of` on purpose (LEDGER_COLUMNS' own documented meaning — `forge`
    is live, never "as of" a stored time), and the `merged` stage read that
    same nulled field, so every plan `--verdict` confirmed merged reported
    `merged` as `empty`."""
    db = Db(tmp_path)
    db.run("0020-a", "merged", pr=24, day="2026-09-10")
    repo_root, files = _git_repo(tmp_path, [("0020-a", "done")])
    merged_pr = {"number": 24, "state": "closed", "merged": True,
                "merged_at": "2026-09-10T11:00:00Z"}
    data = fp.ledger(files, db=db.commit(), repo=REPO, repo_root=repo_root, ref="main",
                     fleetlib_mod=FakeFleetlib(), orch_mod=FakeOrch(), now=NOW,
                     verdict=True, pr_lookup=lambda repo, pid, stored_pr=None: merged_pr,
                     probe=lambda pid: {"state": "x", "detail": ""})
    row = _rows(data)["0020-a"]
    assert row["pr_state"] == "merged" and row["pr_as_of"] is None
    assert row["stages"]["merged"]["state"] == "completed"
    assert row["stages"]["merged"]["completed_at"] == "2026-09-10T11:00:00Z"


# ------------------------------------------------------------------- D1 / D4

def _pr(number, body, state="closed", created="2026-09-01T00:00:00Z",
       merged_at=None, closed_at=None):
    return {"number": number, "body": body, "state": state, "created_at": created,
            "merged_at": merged_at, "closed_at": closed_at,
            "merged": merged_at is not None}


def test_a_intakebot_filed_plan_reads_intakeapp(tmp_path):
    db = Db(tmp_path)
    db.run("0014-a", "merged", pr=20)
    pr = _pr(500, fp.INTAKEBOT_MARKER + "\n\nPlan: 0014-a", merged_at="2026-09-02T00:00:00Z")
    repo_root, files = _git_repo(tmp_path, [("0014-a", "done")])
    data = fp.ledger(files, db=db.commit(), repo=REPO, repo_root=repo_root, ref="main",
                     fleetlib_mod=FakeFleetlib(), orch_mod=FakeOrch(), now=NOW,
                     with_filings=True,
                     filing_lookup=lambda repo, pid, relpath: [pr])
    row = _rows(data)["0014-a"]
    assert row["submitted_by"] == "intakeapp"
    assert row["stages"]["submitted"]["state"] == "completed"


def test_a_cli_filed_plan_reads_cli(tmp_path):
    db = Db(tmp_path)
    db.run("0015-a", "merged", pr=21)
    pr = _pr(501, "Plan: 0015-a", merged_at="2026-09-02T00:00:00Z")
    repo_root, files = _git_repo(tmp_path, [("0015-a", "done")])
    data = fp.ledger(files, db=db.commit(), repo=REPO, repo_root=repo_root, ref="main",
                     fleetlib_mod=FakeFleetlib(), orch_mod=FakeOrch(), now=NOW,
                     with_filings=True,
                     filing_lookup=lambda repo, pid, relpath: [pr])
    row = _rows(data)["0015-a"]
    assert row["submitted_by"] == "cli"


def test_a_refiled_plan_submitted_has_two_attempts_from_the_first_pr(tmp_path):
    db = Db(tmp_path)
    db.run("0016-a", "merged", pr=22)
    first = _pr(600, "Plan: 0016-a", state="closed",
               created="2026-08-01T00:00:00Z", closed_at="2026-08-02T00:00:00Z")
    second = _pr(650, "Plan: 0016-a", state="closed",
                created="2026-08-10T00:00:00Z", merged_at="2026-08-11T00:00:00Z")
    repo_root, files = _git_repo(tmp_path, [("0016-a", "done")])
    data = fp.ledger(files, db=db.commit(), repo=REPO, repo_root=repo_root, ref="main",
                     fleetlib_mod=FakeFleetlib(), orch_mod=FakeOrch(), now=NOW,
                     with_filings=True,
                     filing_lookup=lambda repo, pid, relpath: [first, second])
    submitted = _rows(data)["0016-a"]["stages"]["submitted"]
    assert submitted["state"] == "completed"
    assert submitted["attempts"] == 2
    assert submitted["started_at"] == "2026-08-01T00:00:00Z"


def test_an_in_review_filing_appears_only_with_with_filings(tmp_path):
    db = Db(tmp_path)
    repo_root, files = _git_repo(tmp_path, [("0017-a", "ready")])
    open_pr = _pr(700, "", state="open", created="2026-11-01T00:00:00Z")

    without = fp.ledger(files, db=db.commit(), repo=REPO, repo_root=repo_root, ref="main",
                        fleetlib_mod=FakeFleetlib(), orch_mod=FakeOrch(), now=NOW)
    assert "0018-still-filing" not in _rows(without)

    with_f = fp.ledger(files, db=Db(tmp_path, "db2.db").commit(), repo=REPO,
                       repo_root=repo_root, ref="main", fleetlib_mod=FakeFleetlib(),
                       orch_mod=FakeOrch(), now=NOW, with_filings=True,
                       filing_lookup=lambda repo, pid, relpath: [],
                       open_filings_lookup=lambda: [{"id": "0018-still-filing", "pr": open_pr}])
    rows = _rows(with_f)
    assert rows["0018-still-filing"]["status"] == "in-review"
    assert rows["0018-still-filing"]["stages"]["submitted"]["state"] == "running"
    assert rows["0018-still-filing"]["stages"]["approved"]["state"] == "empty"
