"""A reopened usage-limit gate drains the reviews it stopped (plan 0169, ADR-0028).

A temporary fleet.db, a stubbed forge and a stubbed dispatcher that records its
calls and returns a chosen exit code. The drain child's work is run in-process
(`_run_drain`); `_try_start_locked` is replaced by a function that does that, so
a "cycle" is `review_drain_sweep`.
"""
import datetime
import json
import sqlite3
import threading
import time

import pytest

from test_mopsus_review_retry import (
    _StubForge,
    _dbpath,
    _load,
    _open_pr,
    _seed_attempt,
    _wire,  # noqa: F401  (autouse fixture)
    BIN,
)

FABLE = "claude-fable-5-1"
REPO = "operator/sniff"
HELD = "plan 0002-b waits on 0001-a: not done on main"


@pytest.fixture
def mp():
    return _load("mopsus_review_drain", BIN / "mopsus")


def _stamp(delta_s=0):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta_s)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    def __init__(self, mp, tmp_path, monkeypatch):
        self.mp, self.tmp = mp, tmp_path
        self.db = _dbpath(tmp_path)
        self.forge = _StubForge()
        mp.fwatch._forge = lambda token: self.forge
        self.codes = {}              # pr -> exit code (default 0)
        self.calls = []              # (repo, pr)
        self.live = self.peak = 0
        self.delay = 0.0
        self.on_call = None
        self._lock = threading.Lock()
        self.notes = []
        self.notify_ok = True
        self.starts = []
        monkeypatch.setattr(mp, "_dispatch_session_retry", self._dispatch)
        monkeypatch.setattr(mp.orch, "notify", self._notify)
        monkeypatch.setattr(mp, "_try_start_locked", self._start)
        monkeypatch.setattr(mp, "_probe_due", lambda *a, **k: True)
        monkeypatch.setattr(mp, "_credits_retry_bill", lambda *a, **k: None)
        self.n = 0
        self.ev = 0

    # stubs ---------------------------------------------------------------
    def _dispatch(self, repo, pr, bill=None):
        with self._lock:
            self.calls.append((repo, pr))
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.on_call:
                self.on_call(repo, pr)
            return self.codes.get(pr, 0)
        finally:
            with self._lock:
                self.live -= 1

    def _notify(self, title, body):
        self.notes.append((title, body))
        return self.notify_ok

    def _start(self, payload, db=None, lock=None, command="_review-lock-run"):
        self.starts.append((payload, command))
        if command == "_review-drain-run":
            self.mp._run_drain(int(payload), db)
        return True

    # world ---------------------------------------------------------------
    def pages(self):
        return [n for n in self.notes if "reviews retried" in n[0]]

    def seed(self, pr, head=None, body="Plan: 0009-thing", ordinal=1, outcome="quota",
             started="2026-09-27T00:00:00Z", model=FABLE, statuses=(), files=None):
        head = head or f"h{pr:07d}"
        _seed_attempt(self.db, repo=REPO, pr=pr, head=head, model=model, outcome=outcome,
                      started=started, ordinal=ordinal, session=f"s{pr}-{ordinal}")
        pull = _open_pr(head=head, body=body)
        self.forge.pulls[(REPO, pr)] = pull
        if statuses:
            self.forge.statuses[(REPO, head)] = list(statuses)
        if files:
            self.forge.files[(REPO, pr)] = files
        return head

    def close(self, scope, at=None):
        self.ev += 1
        self.mp.fquota.close(scope, None, "stop", f"ev-{scope}-{self.ev}",
                             at=at or _stamp(1), db=self.db)

    def reopen(self, scope="model:fable"):
        self.close(scope)
        assert self.mp.fquota.open_gate(scope, "meter", db=self.db)
        con = sqlite3.connect(str(self.db))
        tid = con.execute("SELECT MAX(id) FROM quota_transition WHERE scope=?",
                          (scope,)).fetchone()[0]
        con.close()
        return tid

    def sweep(self):
        self.mp.review_drain_sweep(self.db)

    def entry(self, tid):
        return self.mp._load_state()["review_drains"].get(str(tid))

    def age_requeues(self, hours=2):
        """Backdate every recorded review-requeued, so none is in flight."""
        p = self.tmp / ".fleet" / "events.jsonl"
        out = []
        for line in p.read_text().splitlines():
            ev = json.loads(line)
            if ev.get("type") == "review-requeued":
                ev["ts"] = _stamp(-3600 * hours)
            out.append(json.dumps(ev))
        p.write_text("\n".join(out) + "\n")

    def requeued(self):
        p = self.tmp / ".fleet" / "events.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines()
                if '"review-requeued"' in l]


@pytest.fixture
def w(mp, tmp_path, monkeypatch):
    return World(mp, tmp_path, monkeypatch)


def st(id_, status, context, desc="", login="mergebot"):
    return {"id": id_, "context": context, "status": status, "description": desc,
            "created_at": f"2026-10-27T10:00:{id_:02d}Z", "creator": {"login": login}}


def merger(w):
    return w.mp.lib.agent_roles()["agent_merger"].account


# ------------------------------------------------------------- when it starts


@pytest.mark.parametrize("closed,drains", [("session", False), ("weekly", False),
                                           (None, True)])
def test_a_drain_starts_only_while_session_and_weekly_are_open(w, closed, drains):
    w.seed(7)
    tid = w.reopen()
    if closed:
        w.close(closed, at=_stamp(-60))
    w.sweep()
    assert w.calls == ([(REPO, 7)] if drains else [])
    assert (w.entry(tid) is not None) == drains


def test_the_bucket_is_reserved_before_drain_plan_scans_it(w, monkeypatch):
    """round 3 MEDIUM: a concurrently-running one-row sweep must see this
    bucket as draining from the moment a fresh transition is taken up — not
    only after `_drain_plan`'s own scan (one forge read per held row, real
    wall-clock time) finishes — or it could dispatch the very head the drain
    is about to (the start-up window the finding names)."""
    w.seed(7)
    real = w.mp._drain_plan
    seen = []

    def spying(token, tid, db=None):
        seen.append("model:fable" in w.mp._drain_active_scopes())
        return real(token, tid, db=db)
    monkeypatch.setattr(w.mp, "_drain_plan", spying)
    w.reopen()
    w.sweep()
    assert seen and all(seen)


def test_a_drained_retry_that_stopped_again_is_not_drained_again(w, monkeypatch):
    w.seed(7)
    tid = w.reopen()

    def stops_again(repo, pr):
        # After, not before, the drain's own review-requeued: this is the
        # round that retry itself ran, landing a fresh (settled, not
        # in-flight) quota stop the moment it exits 75.
        _seed_attempt(w.db, repo=repo, pr=pr, head="h0000007", model=FABLE,
                      started=_stamp(1), ordinal=1, session="again")
    w.on_call = stops_again
    w.codes[7] = 75
    w.sweep()
    assert w.calls == [(REPO, 7)]
    tid2 = w.reopen()
    assert tid2 != tid
    w.sweep()
    assert w.calls == [(REPO, 7)]           # not a candidate of the newer drain
    assert w.entry(tid2)["state"] == "not-actionable"
    # the existing sweep still retries it after an answered probe
    started = []
    monkeypatch.setattr(w.mp, "_try_start_locked",
                        lambda payload, db=None, **k: started.append(json.loads(payload)))
    w.mp.review_retry_sweep(db=w.db)
    assert [p["pr"] for p in started] == [7]


def test_both_ordinals_at_one_head_are_one_dispatch(w):
    w.seed(7, ordinal=1)
    w.seed(7, ordinal=2)

    def settles(repo, pr):
        for o in (1, 2):
            _seed_attempt(w.db, repo=repo, pr=pr, head="h0000007", model=FABLE,
                          outcome="ok", started=_stamp(), ordinal=o, session=f"ok{o}")
    w.on_call = settles
    w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 7)]
    assert w.mp.held_review_queue("tok", db=w.db, outcomes=("quota",)) == []


def test_a_row_that_stopped_after_the_transition_is_not_drained(w):
    w.seed(7, started=_stamp(3600))
    w.reopen()
    w.sweep()
    assert w.calls == []


def test_no_verdict_and_budget_rows_are_not_candidates(w):
    w.seed(7, outcome="no-verdict")
    w.seed(8, outcome="budget")
    w.reopen()
    w.sweep()
    assert w.calls == []


def test_a_re_run_of_the_same_started_drain_does_not_redispatch_a_head(w):
    """round 1/2 MEDIUM: an entry left `started` (failed notify) whose dispatch
    is past DRAIN_INFLIGHT_MINUTES must not send a second round at that head —
    `prior` must count this SAME transition's own earlier results too."""
    w.seed(7)
    w.notify_ok = False
    tid = w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 7)] and w.entry(tid)["state"] == "started"
    w.age_requeues()                   # past the in-flight window; nothing landed
    w.notify_ok = True
    w.sweep()
    assert w.calls == [(REPO, 7)]       # not dispatched a second time


def test_a_no_verdict_row_is_held_back_while_the_drains_round_at_its_head_is_in_flight(
        w, monkeypatch):
    """round 1/2 HIGH: a drain's retry at a head settles every stop there
    (ADR-0028 §3), so the one-row sweep must not independently retry a
    DIFFERENT ordinal's stop at the SAME head while that round is in flight."""
    head = w.seed(7, ordinal=1, outcome="quota")
    w.seed(7, ordinal=2, outcome="no-verdict", head=head)
    w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 7)]
    started = []
    monkeypatch.setattr(w.mp, "_try_start_locked",
                        lambda payload, db=None, **k: started.append(payload))
    w.mp.review_retry_sweep(db=w.db)
    assert started == []


def test_a_non_merger_eunomia_deps_failure_is_not_red(w):
    """round 2 MEDIUM-3: only the merger's OWN latest `eunomia/deps` status
    (the same one `pr_hold` reads) counts; a different write-access account's
    unrelated failure in that context must not block the row."""
    non_merger = st(1, "failure", "eunomia/deps", "unrelated", login="rev2bot")
    w.seed(7, statuses=[non_merger])
    w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 7)]


def test_a_combined_status_from_a_different_account_never_hides_the_mergers_own(
        w, monkeypatch):
    """round 3 MEDIUM: the real combined endpoint shows only ONE entry per
    context — its overall latest, from whichever account posted most
    recently — so a different account's later, unrelated SUCCESS for
    `eunomia/deps` can occupy that one slot even while the merger's own
    failure (the one that actually matters) sits earlier in the raw history.
    Trusting the combined endpoint's single entry for who posted it would
    miss the merger's failure entirely and dispatch a held head."""
    held = st(1, "failure", "eunomia/deps", HELD, login=merger(w))
    later_by_someone_else = st(2, "success", "eunomia/deps", "", login="rev2bot")
    head = w.seed(7, statuses=[held, later_by_someone_else])
    # `list_statuses` (what the merger-scoped read uses) sees the full raw
    # history; the combined endpoint, simulated faithfully here, shows only
    # its single latest entry, which belongs to the OTHER account.
    monkeypatch.setattr(
        w.forge, "get_commit_status",
        lambda repo, sha: (200, {"statuses": [later_by_someone_else]}))
    tid = w.reopen()
    w.sweep()
    assert w.calls == []
    assert _skipped(w, tid, head)["reason"] == "held"


# --------------------------------------------------------- the one-row sweep


@pytest.mark.parametrize("kind", ["red", "held", "base"])
def test_after_the_drain_the_one_row_sweep_skips_what_the_drain_skipped(
        w, monkeypatch, kind):
    deps = st(2, "failure", "eunomia/deps", HELD, login=merger(w))
    head = w.seed(7, statuses=[deps] if kind == "held" else
                  [st(1, "failure", "ci/tests")] if kind == "red" else [])
    if kind == "base":
        w.forge.pulls[(REPO, 7)]["merge_base"] = head
    tid = w.reopen()
    w.sweep()
    assert w.calls == [] and w.entry(tid)["state"] == "paged"
    reason = {"red": "red", "held": "held", "base": "in base"}[kind]
    assert w.entry(tid)["skips"][f"{REPO}#7@{head}"]["reason"] == reason
    started = []
    monkeypatch.setattr(w.mp, "_try_start_locked",
                        lambda payload, db=None, **k: started.append(payload))
    w.mp.review_retry_sweep(db=w.db)
    assert w.mp.cmd_review_probe(db=w.db) == 0
    assert started == [] and w.requeued() == []


def test_an_unconsumed_resume_request_is_not_asked_twice(w, monkeypatch):
    w.seed(7)
    monkeypatch.setattr(w.mp, "_row_kind", lambda row: ("plan", "0009-thing", "L1"))
    w.mp._write_resume_request("0009-thing", "L1", 7, "h0000007", w.db)
    started = []
    monkeypatch.setattr(w.mp, "_try_start_locked",
                        lambda payload, db=None, **k: started.append(payload))
    w.mp.review_retry_sweep(db=w.db)
    assert started == []


def test_a_resume_request_stuck_past_the_bound_pages_once(w, monkeypatch):
    """round 3 MEDIUM: `_resume_pending` on its own has no time bound, so a
    `resume_request` orphan_sweep never consumes (its lease never died,
    orphan_sweep itself broken) leaves its head silently 'in flight' forever
    — invisible to both the one-row sweep and a drain with nothing else to
    report. Past RESUME_PENDING_STUCK_MINUTES it must page, once, while still
    never being dispatched a second time."""
    head = w.seed(7)
    monkeypatch.setattr(w.mp, "_row_kind", lambda row: ("plan", "0009-thing", "L1"))
    w.mp._write_resume_request("0009-thing", "L1", 7, head, w.db)
    old = _stamp(-60 * (w.mp.RESUME_PENDING_STUCK_MINUTES + 5))
    con = sqlite3.connect(str(w.db))
    con.execute("UPDATE resume_request SET requested_at=? WHERE plan=?", (old, "0009-thing"))
    con.commit()
    con.close()
    started = []
    monkeypatch.setattr(w.mp, "_try_start_locked",
                        lambda payload, db=None, **k: started.append(payload))
    w.mp.review_retry_sweep(db=w.db)
    assert started == []
    assert any("stuck" in t for t, _b in w.notes)
    w.notes.clear()
    w.mp.review_retry_sweep(db=w.db)
    assert w.notes == []                    # not paged a second time


def test_a_resume_request_stuck_past_the_bound_is_reported_by_the_drain(w, monkeypatch):
    head = w.seed(7)
    monkeypatch.setattr(w.mp, "_row_kind", lambda row: ("plan", "0009-thing", "L1"))
    w.mp._write_resume_request("0009-thing", "L1", 7, head, w.db)
    old = _stamp(-60 * (w.mp.RESUME_PENDING_STUCK_MINUTES + 5))
    con = sqlite3.connect(str(w.db))
    con.execute("UPDATE resume_request SET requested_at=? WHERE plan=?", (old, "0009-thing"))
    con.commit()
    con.close()
    tid = w.reopen()
    w.sweep()
    assert w.calls == []
    assert _skipped(w, tid, head)["reason"] == "stuck"


def test_a_spent_no_verdict_row_still_pages_during_a_drain(w, _wire):
    w.seed(7)
    w.reopen()
    w.seed(8, outcome="no-verdict")
    w.mp._emit_review_requeued(REPO, 8, "h0000008", {
        "kind": "session", "pr": 8, "head": "h0000008", "attempt": 1, "limit": 1,
        "reason": "no-verdict"})
    # the drain entry is at `started`: its child has not finished
    w.mp._update_drain(99, lambda e: e.update({"state": "started", "scope": "model:fable"}))
    _seed_attempt(w.db, pr=8, head="h0000008", outcome="no-verdict", session="again",
                  ordinal=1, model=FABLE)
    w.mp.review_retry_sweep(db=w.db)
    assert any("retry limit" in t for t, _b in w.notes)


def test_the_one_row_sweep_leaves_a_draining_buckets_quota_rows_alone(w, monkeypatch):
    w.seed(7)
    w.mp._update_drain(5, lambda e: e.update({"state": "started", "scope": "model:fable"}))
    started = []
    monkeypatch.setattr(w.mp, "_try_start_locked",
                        lambda payload, db=None, **k: started.append(payload))
    w.mp.review_retry_sweep(db=w.db)
    w.mp.cmd_review_probe(db=w.db)
    assert started == []


def test_a_probe_of_the_session_gate_still_starts_while_a_drain_runs(w, monkeypatch):
    w.seed(7)
    w.mp._update_drain(5, lambda e: e.update({"state": "started", "scope": "model:fable"}))
    w.close("session", at=_stamp(-3600))
    started = []
    monkeypatch.setattr(w.mp, "_try_start_locked",
                        lambda payload, db=None, **k: started.append(json.loads(payload)))
    w.mp.quota_probe_sweep(db=w.db)
    assert [p.get("gate") for p in started] == ["session"]


# ------------------------------------------------------------------- the page


def test_a_failed_notify_leaves_started_and_the_next_cycle_pages_once(w):
    w.seed(7)
    tid = w.reopen()
    w.notify_ok = False
    w.sweep()
    assert w.entry(tid)["state"] == "started" and len(w.pages()) == 1
    w.notify_ok = True
    w.sweep()
    w.sweep()
    assert w.entry(tid)["state"] == "paged" and len(w.pages()) == 2
    assert w.calls == [(REPO, 7)]          # the retry itself was not repeated


def test_a_started_entry_whose_gate_closed_is_paged_once(w):
    w.seed(7)
    w.seed(8)
    tid = w.reopen()
    w.notify_ok = False
    w.codes[7] = 75
    w.sweep()                                # PR 7 exits 75, PR 8 not started
    assert w.entry(tid)["state"] == "started"
    w.close("model:fable", at=_stamp(5))
    w.notify_ok = True
    n = len(w.pages())
    w.sweep()
    w.sweep()
    assert len(w.pages()) == n + 1 and w.entry(tid)["state"] == "paged"


def test_a_forge_read_failure_with_a_started_entry_pages_nothing(w):
    head = w.seed(7)
    tid = w.reopen()
    w.mp._update_drain(tid, lambda e: e.update({
        "state": "started", "scope": "model:fable", "at": _stamp(),
        "mark": w.mp._max_attempt_rowid(w.db),
        "results": {}, "skips": {}, "not_started": {}}))
    w.forge.statuses[(REPO, head)] = None
    w.sweep()
    assert w.pages() == [] and w.entry(tid)["state"] == "started" and w.calls == []
    w.forge.statuses.pop((REPO, head))
    w.sweep()
    assert w.calls == [(REPO, 7)] and len(w.pages()) == 1


def test_an_entry_stuck_unreadable_for_a_short_while_is_not_paged(w):
    head = w.seed(7)
    tid = w.reopen()
    w.mp._update_drain(tid, lambda e: e.update({
        "state": "started", "scope": "model:fable", "at": _stamp(-3600),
        "since": _stamp(-60 * 5), "mark": w.mp._max_attempt_rowid(w.db),
        "results": {}, "skips": {}, "not_started": {}}))
    w.forge.statuses[(REPO, head)] = None
    w.sweep()
    assert w.pages() == [] and w.entry(tid)["state"] == "started"


def test_an_entry_stuck_unreadable_for_DRAIN_STUCK_MINUTES_is_paged_anyway(w):
    """round 1/2 MEDIUM: a started entry whose forge reads keep failing must
    not hold the bucket's one-row sweep and review-probe back forever."""
    head = w.seed(7)
    tid = w.reopen()
    w.mp._update_drain(tid, lambda e: e.update({
        "state": "started", "scope": "model:fable", "at": _stamp(-3600),
        "since": _stamp(-60 * (w.mp.DRAIN_STUCK_MINUTES + 5)),
        "incomplete_since": _stamp(-60 * (w.mp.DRAIN_STUCK_MINUTES + 5)),
        "mark": w.mp._max_attempt_rowid(w.db),
        "results": {}, "skips": {}, "not_started": {}}))
    w.forge.statuses[(REPO, head)] = None
    w.sweep()
    assert w.entry(tid)["state"] == "paged" and len(w.pages()) == 1
    assert w.entry(tid)["skips"][f"{REPO}#7@{head}"]["reason"] == "unreadable"


def test_DRAIN_STUCK_MINUTES_is_measured_from_the_first_failing_read(w):
    """round 3 MEDIUM: `since` is when the entry was CREATED, not when its
    reads started failing. An entry already old when a read first fails must
    get its own full DRAIN_STUCK_MINUTES, not be paged on the very next cycle
    just because `since` is already old."""
    head = w.seed(7)
    tid = w.reopen()
    w.mp._update_drain(tid, lambda e: e.update({
        "state": "started", "scope": "model:fable", "at": _stamp(-3600),
        "since": _stamp(-60 * (w.mp.DRAIN_STUCK_MINUTES + 5)),   # entry itself is old
        "mark": w.mp._max_attempt_rowid(w.db),
        "results": {}, "skips": {}, "not_started": {}}))
    w.forge.statuses[(REPO, head)] = None                        # reads start failing NOW
    w.sweep()
    assert w.entry(tid)["state"] == "started" and w.pages() == []
    assert w.entry(tid)["incomplete_since"] is not None
    # Backdate the first-failure mark itself: only now has it been failing
    # DRAIN_STUCK_MINUTES straight.
    w.mp._update_drain(tid, lambda e: e.update({
        "incomplete_since": _stamp(-60 * (w.mp.DRAIN_STUCK_MINUTES + 5))}))
    w.sweep()
    assert w.entry(tid)["state"] == "paged" and len(w.pages()) == 1


def test_a_child_that_never_records_anything_is_paged_after_DRAIN_STUCK_MINUTES(w, monkeypatch):
    """round 3 MEDIUM: a persistent child-side failure — e.g. the hardcoded
    fallback `_clean_env` leaves as the drain child's only token source
    failing every time — means every cycle respawns a child that dies before
    recording even one result. `plan["complete"]` is True throughout (THIS
    process reads the forge fine), so the read-failure DRAIN_STUCK_MINUTES
    check above never sees it; a separate bound must still release the
    bucket rather than hold it silently forever."""
    w.seed(7)
    monkeypatch.setattr(w.mp, "_run_drain", lambda tid, db=None: None)  # dies before recording
    tid = w.reopen()
    w.sweep()
    assert w.entry(tid)["state"] == "started" and w.pages() == []
    w.mp._update_drain(tid, lambda e: e.update({
        "since": _stamp(-60 * (w.mp.DRAIN_STUCK_MINUTES + 5))}))
    w.sweep()
    assert w.entry(tid)["state"] == "paged" and len(w.pages()) == 1
    assert w.calls == []


def test_a_started_entry_whose_transition_row_is_gone_is_paged_from_what_it_has(w):
    """round 2 MEDIUM: a lost-and-rebuilt fleet.db must not leave a bucket
    blocked forever by a `started` entry nothing can finish reading."""
    head = w.seed(7)
    tid = w.reopen()
    w.mp._update_drain(tid, lambda e: e.update({
        "state": "started", "scope": "model:fable", "at": _stamp(-10),
        "since": _stamp(-10), "mark": w.mp._max_attempt_rowid(w.db),
        "results": {f"{REPO}#7@{head}": {"repo": REPO, "pr": 7, "head": head,
                                         "result": 0}},
        "skips": {}, "not_started": {}}))
    con = sqlite3.connect(str(w.db))
    con.execute("DELETE FROM quota_transition WHERE id=?", (tid,))
    con.commit()
    con.close()
    w.sweep()
    assert w.entry(tid)["state"] == "paged" and len(w.pages()) == 1


def test_a_restarted_drain_dispatching_a_previously_skipped_head_clears_the_skip(w):
    """round 1/2 LOW: a PR must never be listed under both Skipped and
    Dispatched on the same page."""
    head = w.seed(7)
    tid = w.reopen()
    key = f"{REPO}#7@{head}"
    w.mp._update_drain(tid, lambda e: e.update({
        "state": "started", "scope": "model:fable", "at": _stamp(-10),
        "since": _stamp(-10), "mark": w.mp._max_attempt_rowid(w.db),
        "results": {}, "skips": {key: {"repo": REPO, "pr": 7, "reason": "in flight",
                                       "detail": ""}},
        "not_started": {}}))
    w.mp._run_drain(tid, w.db)
    assert w.calls == [(REPO, 7)]
    entry = w.entry(tid)
    assert key not in (entry.get("skips") or {})
    assert key in entry["results"]


def test_the_page_title_and_dispatched_list_exclude_not_dispatched_results(w, monkeypatch):
    """round 1/2 LOW: the page's "N reviews retried" and its Dispatched
    section must count only results that reached the dispatcher."""
    w.seed(7)
    w.seed(8)
    real_emit = w.mp._emit_review_requeued

    def flaky(repo, pr, head, detail):
        if pr == 8:
            return False, "boom"
        return real_emit(repo, pr, head, detail)
    monkeypatch.setattr(w.mp, "_emit_review_requeued", flaky)
    w.reopen()
    w.sweep()
    assert sorted(pr for _r, pr in w.calls) == [7]
    title, body = w.pages()[0]
    assert title == "mopsus: 1 reviews retried after model:fable reopened"
    assert "not dispatched" in body


def test_five_held_rows_run_two_at_a_time_plan_documents_first_one_page(w):
    for pr in range(1, 6):
        w.seed(pr, started=f"2026-09-27T00:00:0{pr}Z",
               files=["plans/0001-x.md"] if pr == 5 else None)
    w.delay = 0.05
    tid = w.reopen()
    w.sweep()
    assert sorted(pr for _r, pr in w.calls) == [1, 2, 3, 4, 5]
    # The plan-document PR is scheduled first — submitted into the FIRST
    # concurrency-wide batch, ahead of every non-plan PR. With concurrency 2
    # it races the batch's other slot (PR 1), submitted right behind it,
    # for which one's dispatch is actually RECORDED first — round 3's own
    # pre-dispatch recheck (`_recheck_dispatch`) does enough extra work before
    # that recording to make that particular race genuinely unpredictable —
    # but it must never race PRs 2-4, submitted only once a slot frees up.
    order = [pr for _r, pr in w.calls]
    assert order.index(5) < 2
    assert w.peak == 2
    assert len(w.pages()) == 1
    title, body = w.pages()[0]
    assert title == "mopsus: 5 reviews retried after model:fable reopened"
    assert all(f"{REPO}#{pr}" in body and "exit 0" in body for pr in range(1, 6))
    w.sweep()
    assert len(w.pages()) == 1 and len(w.calls) == 5
    assert w.entry(tid)["state"] == "paged"


def test_a_stale_classification_is_rechecked_before_it_uses_a_slot(w, monkeypatch):
    """round 3 MEDIUM: classification happens once, at the top of the drain;
    with bounded concurrency a row near the back of the dispatch list can wait
    for the row(s) ahead of it. In that wait, the one-row sweep may already
    have retried this exact head on its own (e.g. a no-verdict row at the same
    head) — the drain must re-check immediately before it dispatches, not act
    on a now-stale verdict and review the head a second time."""
    w.seed(7, started="2026-09-27T00:00:01Z")
    head8 = w.seed(8, started="2026-09-27T00:00:02Z")

    def on_call(repo, pr):
        if pr == 7:
            # While PR 7 is still dispatching, the one-row sweep retries PR
            # 8's head on its own — a requeue the drain's classification,
            # taken before this call started, never saw.
            w.mp._emit_review_requeued(REPO, 8, head8, {
                "kind": "session", "pr": 8, "head": head8, "attempt": 1, "limit": 2})
    w.on_call = on_call
    w.delay = 0.05
    monkeypatch.setattr(w.mp, "review_drain_concurrency", lambda db=None: 1)
    tid = w.reopen()
    w.sweep()
    assert sorted(pr for _r, pr in w.calls) == [7]      # 8 is skipped, not dispatched again
    assert _skipped(w, tid, head8, pr=8)["reason"] == "in flight"


def test_a_head_moved_between_classification_and_dispatch_is_rechecked(w, monkeypatch):
    """round 3 MEDIUM: the same stale-classification risk for a head that
    moved (a new push) rather than one the sweep already retried."""
    w.seed(7, started="2026-09-27T00:00:01Z")
    w.seed(8, started="2026-09-27T00:00:02Z")

    def on_call(repo, pr):
        if pr == 7:
            w.forge.pulls[(REPO, 8)]["head"] = {"sha": "moved-on"}
    w.on_call = on_call
    w.delay = 0.05
    monkeypatch.setattr(w.mp, "review_drain_concurrency", lambda db=None: 1)
    tid = w.reopen()
    w.sweep()
    assert sorted(pr for _r, pr in w.calls) == [7]
    entry = w.entry(tid)
    assert any(s["pr"] == 8 and s["reason"] == "head moved"
               for s in entry["skips"].values())


def test_concurrency_follows_the_config_key(w, monkeypatch):
    for pr in range(1, 5):
        w.seed(pr)
    monkeypatch.setattr(w.mp, "review_drain_concurrency", lambda db=None: 1)
    w.delay = 0.02
    w.reopen()
    w.sweep()
    assert len(w.calls) == 4 and w.peak == 1


# ----------------------------------------------------------- D2's skip table


def _skipped(w, tid, head, pr=7):
    return w.entry(tid)["skips"][f"{REPO}#{pr}@{head}"]


def test_closed_merged_draft_head_moved_and_reviewed_are_reported(w):
    for pr in (1, 2, 3, 4, 5):
        w.seed(pr)
    w.forge.pulls[(REPO, 1)]["state"] = "closed"
    w.forge.pulls[(REPO, 2)].update(state="closed", merged=True)
    w.forge.pulls[(REPO, 3)]["draft"] = True
    w.forge.pulls[(REPO, 4)]["head"] = {"sha": "moved"}
    w.forge.reviews[(REPO, 5)] = [{"commit_id": "h0000005", "stale": False,
                                   "dismissed": False,
                                   "user": {"login": w.mp.lib.agent_roles()["reviewer"].account}}]
    tid = w.reopen()
    plan = w.mp._drain_plan("tok", tid, w.db)
    got = {s["pr"]: s["reason"] for s in plan["skips"].values()}
    assert got == {1: "closed", 2: "merged", 3: "draft", 4: "head moved", 5: "reviewed"}
    # None of these is actionable: a drain of nothing but report-only skips
    # starts no drain and pages nobody (round 1/2 MEDIUM, "every reopening
    # pages a list of old quota stops ... and the list only grows"). An entry
    # is still written, marked `not-actionable` rather than left absent, so
    # the next cycle does not repeat this same full held-queue scan for a
    # verdict that cannot change (round 3 MEDIUM, "re-runs _drain_plan's full
    # held-queue forge scan every cycle").
    w.sweep()
    assert w.calls == [] and w.requeued() == []
    assert w.entry(tid)["state"] == "not-actionable" and w.pages() == []


def test_a_not_actionable_transition_is_not_replanned_on_the_next_cycle(w, monkeypatch):
    """round 3 MEDIUM: once a transition's verdict is recorded as
    `not-actionable`, a later cycle must not re-run `_drain_plan`'s full
    held-queue forge scan for it — the `mine()` filter it depends on (this
    scope, stopped before the transition's `at`) cannot change afterwards."""
    w.seed(7)
    w.forge.pulls[(REPO, 7)]["state"] = "closed"
    tid = w.reopen()
    calls = []
    real = w.mp._drain_plan

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(w.mp, "_drain_plan", counting)
    w.sweep()
    assert w.entry(tid)["state"] == "not-actionable" and len(calls) == 1
    w.sweep()
    w.sweep()
    assert len(calls) == 1          # not replanned on either later cycle


def test_in_base_red_held_spent_unreadable_and_in_flight_skip_with_a_reason(w):
    held = st(3, "failure", "eunomia/deps", HELD, login=merger(w))
    heads = {
        1: w.seed(1),
        2: w.seed(2, statuses=[st(1, "failure", "ci/tests")]),
        3: w.seed(3, statuses=[held]),
        4: w.seed(4),
        5: w.seed(5),
        6: w.seed(6),
    }
    w.forge.pulls[(REPO, 1)]["merge_base"] = heads[1]
    w.forge.statuses[(REPO, heads[5])] = None
    for _ in range(2):                                 # spent: two requeues already
        w.mp._emit_review_requeued(REPO, 4, heads[4], {
            "kind": "session", "pr": 4, "head": heads[4], "attempt": 1, "limit": 2})
    w.age_requeues()
    w.mp._emit_review_requeued(REPO, 6, heads[6], {
        "kind": "session", "pr": 6, "head": heads[6], "attempt": 2, "limit": 2})
    before = len(w.requeued())
    tid = w.reopen()
    w.sweep()                       # PR 5 is unreadable: the sweep waits, dispatches nothing
    # The bucket is reserved (entry `started`) from this very first cycle —
    # before, not after, the scan that found PR 5 unreadable (round 3 MEDIUM,
    # "a start-up window where the one-row sweep and a just-started drain can
    # both dispatch the same head") — so nothing is dispatched yet either way.
    assert w.entry(tid)["state"] == "started" and w.calls == []
    w.forge.statuses.pop((REPO, heads[5]))
    w.sweep()
    got = {s["pr"]: s["reason"] for s in w.entry(tid)["skips"].values()}
    assert got == {1: "in base", 2: "red", 3: "held", 4: "spent", 6: "in flight"}
    assert "plan 0002-b waits on" in _skipped(w, tid, heads[3], 3)["detail"]
    assert w.calls == [(REPO, 5)]
    assert len(w.requeued()) == before + 1                # only PR 5's own retry


def test_a_red_context_beats_held_and_a_rerun_green_is_not_red(w):
    held = st(3, "failure", "eunomia/deps", HELD, login=merger(w))
    h1 = w.seed(1, statuses=[st(4, "failure", "ci/tests"), held])
    # the combined endpoint de-duplicates: a lane re-run green replaced the failure
    w.seed(2, statuses=[st(5, "success", "ci/tests")])
    tid = w.reopen()
    w.sweep()
    assert _skipped(w, tid, h1, 1)["reason"] == "red"
    assert w.calls == [(REPO, 2)]


def test_a_plan_pr_is_never_held_and_a_non_held_deps_failure_is_red(w):
    held = st(3, "failure", "eunomia/deps", HELD, login=merger(w))
    w.seed(1, statuses=[held], files=["plans/0003-x.md"])
    h2 = w.seed(2, statuses=[st(3, "failure", "eunomia/deps", "something else",
                                login=merger(w))])
    tid = w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 1)]
    assert _skipped(w, tid, h2, 2)["reason"] == "red"


def test_held_comes_from_pr_hold(w, monkeypatch):
    head = w.seed(7, statuses=[st(1, "failure", "eunomia/deps", "why", login=merger(w))])
    monkeypatch.setattr(w.mp.fdeps, "pr_hold", lambda forge, repo, sha: ("held", "why"))
    tid = w.reopen()
    w.sweep()
    assert _skipped(w, tid, head)["reason"] == "held" and w.calls == []


def test_a_requeue_ten_minutes_old_with_no_attempt_since_is_in_flight(w):
    head = w.seed(7)
    w.mp._emit_review_requeued(REPO, 7, head, {
        "kind": "session", "pr": 7, "head": head, "attempt": 1, "limit": 2})
    tid = w.reopen()
    w.sweep()
    assert _skipped(w, tid, head)["reason"] == "in flight" and w.calls == []


def test_a_forge_read_error_is_unreadable_and_not_dispatched(w):
    head = w.seed(7)
    w.seed(8)
    w.forge.statuses[(REPO, head)] = None
    tid = w.reopen()
    plan = w.mp._drain_plan("tok", tid, w.db)
    assert plan["complete"] is False
    assert plan["skips"][f"{REPO}#7@{head}"]["reason"] == "unreadable"
    assert [r["pr"] for r in plan["dispatch"]] == [8]


# --------------------------------------------------------- stopping, resuming


def test_the_gate_closing_after_the_first_retry_leaves_the_rest_not_started(w):
    for pr in (1, 2, 3):
        w.seed(pr, started=f"2026-09-27T00:00:0{pr}Z")
    w.mp.review_drain_concurrency = lambda db=None: 1
    w.on_call = lambda repo, pr: w.close("model:fable", at=_stamp(5))
    tid = w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 1)]
    e = w.entry(tid)
    assert {s["reason"] for s in e["not_started"].values()} == {"gate closed (model:fable)"}
    assert len(e["not_started"]) == 2 and "Not started:" in w.pages()[0][1]


def test_a_session_gate_closing_mid_drain_is_named(w):
    for pr in (1, 2):
        w.seed(pr, started=f"2026-09-27T00:00:0{pr}Z")
    w.mp.review_drain_concurrency = lambda db=None: 1
    w.on_call = lambda repo, pr: w.close("session", at=_stamp(5))
    tid = w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 1)]
    assert {s["reason"] for s in w.entry(tid)["not_started"].values()} == {
        "gate closed (session)"}


def test_one_exit_75_starts_nothing_more(w):
    for pr in (1, 2, 3):
        w.seed(pr, started=f"2026-09-27T00:00:0{pr}Z")
    w.mp.review_drain_concurrency = lambda db=None: 1
    w.codes[1] = 75
    tid = w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 1)]
    assert {s["reason"] for s in w.entry(tid)["not_started"].values()} == {
        "retry hit the limit"}
    assert "exit 75" in w.pages()[0][1]


def test_a_new_quota_stop_in_the_bucket_starts_nothing_more(w):
    for pr in (1, 2):
        w.seed(pr, started=f"2026-09-27T00:00:0{pr}Z")
    w.mp.review_drain_concurrency = lambda db=None: 1
    w.on_call = lambda repo, pr: _seed_attempt(
        w.db, pr=99, head="other", model=FABLE, started=_stamp(), session="new")
    tid = w.reopen()
    w.sweep()
    assert w.calls == [(REPO, 1)]
    assert {s["reason"] for s in w.entry(tid)["not_started"].values()} == {"new quota stop"}


def test_a_child_killed_mid_run_is_finished_by_the_next_cycle(w):
    for pr in (1, 2, 3):
        w.seed(pr, started=f"2026-09-27T00:00:0{pr}Z")
    w.mp.review_drain_concurrency = lambda db=None: 1

    def settle(repo, pr):
        _seed_attempt(w.db, repo=repo, pr=pr, head=f"h{pr:07d}", model=FABLE,
                      outcome="ok", started=_stamp(), session="ok")
    w.on_call = settle
    tid = w.reopen()
    real = w.mp._dispatch_session_retry
    state = {"n": 0}

    def killed(repo, pr, bill=None):
        state["n"] += 1
        if state["n"] == 2:
            raise KeyboardInterrupt           # the child dies mid-run
        return real(repo, pr)
    w.mp._dispatch_session_retry = killed
    with pytest.raises(KeyboardInterrupt):
        w.sweep()
    assert w.entry(tid)["state"] == "started" and w.pages() == []
    w.mp._dispatch_session_retry = real
    w.sweep()
    w.sweep()
    assert len(w.pages()) == 1 and w.entry(tid)["state"] == "paged"
    # PR 2 was requeued and its dispatcher may still be running: not reviewed twice
    assert sorted(pr for _r, pr in w.calls) == [1, 3]
    assert set(w.entry(tid)["results"]) == {f"{REPO}#{pr}@h{pr:07d}" for pr in (1, 3)}
    assert w.entry(tid)["skips"][f"{REPO}#2@h0000002"]["reason"] == "in flight"


def test_a_child_killed_after_its_last_dispatch_is_paged_without_a_new_child(w):
    w.seed(7)

    def settle(repo, pr):
        _seed_attempt(w.db, repo=repo, pr=pr, head="h0000007", model=FABLE,
                      outcome="ok", started=_stamp(), session="ok")
    w.on_call = settle
    tid = w.reopen()
    real_page = w.mp._page_drain
    w.mp._page_drain = lambda t: False       # the child dies before it can page
    w.sweep()
    assert w.entry(tid)["state"] == "started" and len(w.starts) == 1
    w.mp._page_drain = real_page
    w.sweep()
    assert len(w.starts) == 1                # no second child
    assert len(w.pages()) == 1 and w.entry(tid)["state"] == "paged"
    assert w.calls == [(REPO, 7)]


# -------------------------------------------------------------- config, dry run


def test_review_drain_concurrency_is_bounded_and_defaults_to_2(w):
    mp = w.mp
    assert mp.review_drain_concurrency(w.db) == 2
    for bad in ("0", "5", True):
        with pytest.raises(ValueError):
            mp.fconfig._validate_review_drain_concurrency(bad)


def test_review_drain_dry_run_changes_nothing(w, capsys):
    w.seed(1, files=["plans/0001-x.md"])
    w.seed(2)
    w.seed(3, statuses=[st(1, "failure", "ci/tests")])
    w.reopen()
    state_before = w.mp._state_path().exists()
    notes = len(w.notes)
    assert w.mp.cmd_review_drain(w.db, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "1. dispatch" in out and "sniff#1" in out and "skip: red" in out
    assert out.index("sniff#1") < out.index("sniff#2")
    assert w.calls == [] and len(w.notes) == notes and w.requeued() == []
    assert w.mp._state_path().exists() == state_before


def test_review_drain_without_dry_run_refuses(w):
    assert w.mp.cmd_review_drain(w.db, dry_run=False) == 2
