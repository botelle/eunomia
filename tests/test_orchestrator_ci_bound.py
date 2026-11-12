"""Plan 0114: the wait for CI is bounded by max(30 min, 1.3 x p90 of the
repo's recent end-to-end CI times), queue time included.

Stub forges serve Forgejo 15.0.2's run shape; no network. The anchor is the
#686 incident (2026-09-29): commit 3dd6be92 queued 51 minutes, ran 8, and the
constant 30-minute wait gave up 29 minutes before it went green.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from test_orchestrator_review import (  # noqa: F401 -- reused fixtures
    CIForge, SHA1, _fleetforge, _load, _loop, _wire_forge,
)

T0 = datetime(2026, 9, 29, 13, 0, 13, tzinfo=timezone.utc)
ZERO = "0001-01-01T00:00:00Z"
REAL = 3562          # 3dd6be92: created 13:00:13 -> stopped 13:59:35


def _z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(sha, span, status="success", queue=0, created=None, stopped=None):
    """One workflow run: `span` seconds created->stopped, of which `queue` sat
    waiting. `started` is deliberately the late one, as Forgejo records it."""
    c = created or T0
    return {"commit_sha": sha, "status": status, "created": _z(c),
            "started": _z(c + timedelta(seconds=queue)),
            "stopped": stopped if stopped is not None
            else _z(c + timedelta(seconds=span)),
            "duration": (span - queue) * 1_000_000_000}


def commits(n, span, **kw):
    return [run(f"{i:040x}", span, **kw) for i in range(n)]


class RunsForge:
    """Serves `pages` (list of lists of runs) from list_action_runs."""

    def __init__(self, pages, fail_page=None, status=200):
        self.pages, self.fail_page, self.status = pages, fail_page, status
        self.calls = []

    def list_action_runs(self, repo, page=1, limit=50):
        self.calls.append(page)
        if page == self.fail_page:
            return self.status, None
        runs = self.pages[page - 1] if page <= len(self.pages) else []
        return 200, {"total_count": 999, "workflow_runs": runs}


class Log:
    def __init__(self):
        self.lines = []

    def note(self, phase, **facts):
        self.lines.append((phase, facts))


@pytest.fixture
def mod(tmp_path):
    m = _load(tmp_path)
    _wire_forge(m, object())
    return m


def test_686_replay_bound_clears_the_real_wait(mod):
    # Queue 51 min, run 8: end-to-end is the real 3,562 s on every commit.
    f = RunsForge([commits(20, REAL, queue=3060)])
    assert mod.ci_wait_bound("operator/eunomia", f) == (4631, "history")
    assert 4631 > REAL > mod.CI_WAIT_SECS


def test_queue_time_counts_not_started_or_duration(mod):
    # 20 commits, each created 50 min before started and 5 min of running.
    f = RunsForge([commits(20, 3300, queue=3000)])
    secs, _ = mod.ci_wait_bound("r/x", f)
    assert secs == math.ceil(1.3 * 3300)      # a stopped-started bound = 1800


def test_floor_on_fast_history(mod):
    assert mod.ci_wait_bound("r/x", RunsForge([commits(10, 120)])) == (
        1800, "history")


def test_no_history_is_default(mod):
    assert mod.ci_wait_bound("r/x", RunsForge([[]])) == (1800, "default")


class _NoVerb:
    pass


class _Raises:
    def list_action_runs(self, *a, **k):
        raise RuntimeError("boom")


def test_every_fallback_path(mod):
    ff = _fleetforge()
    # A full good page 1 on few distinct commits, so page 2 is really read.
    full = [run(f"{i % 3:040x}", 5000) for i in range(50)]
    cases = [
        RunsForge([full], fail_page=2, status=500),
        RunsForge([full], fail_page=2, status=0),
        RunsForge([[]], fail_page=1, status=404),
        type("F", (), {"list_action_runs": lambda s, *a, **k: (200, {"x": 1})})(),
        type("F", (), {"list_action_runs": lambda s, *a, **k: (200, [])})(),
        ff.Forge("http://example.invalid", "tok", kind="github"),
        _NoVerb(), _Raises(),
    ]
    for f in cases:
        assert mod.ci_wait_bound("r/x", f) == (1800, "fallback")


def test_exclusions_and_grouping(mod):
    bad = (commits(3, 99999, status="cancelled") + commits(3, 99999, status="running")
           + commits(3, 99999, status="waiting")
           + [run(f"z{i}", 0, stopped=ZERO) for i in range(3)])
    # Two workflows on one sha: min(created) .. max(stopped) = 5000 s.
    two = [run("a" * 40, 1000), run("a" * 40, 2000,
                                    created=T0 + timedelta(seconds=3000))]
    log = Log()
    secs, src = mod.ci_wait_bound("r/x", RunsForge([bad + two]), log=log, sha=SHA1)
    assert (secs, src) == (math.ceil(1.3 * 5000), "history")
    assert log.lines[0][1]["samples"] == 1 and log.lines[0][1]["p90"] == 5000


def test_cap_is_four_pages(mod):
    class Endless(RunsForge):
        def list_action_runs(self, repo, page=1, limit=50):
            self.calls.append(page)
            return 200, {"workflow_runs": [run("c" * 40, 9, status="cancelled")
                                           for _ in range(limit)]}
    f = Endless([])
    assert mod.ci_wait_bound("r/x", f) == (1800, "default")
    assert len(f.calls) == 4


def test_p90_is_not_max(mod):
    runs = commits(19, 1000)
    # sorted: 18 x 1000 ... nearest rank ceil(.9*20)=18 -> 18th value
    spans = [1000 + i * 100 for i in range(19)] + [6 * 3600]
    runs = [run(f"{i:040x}", s) for i, s in enumerate(spans)]
    secs, _ = mod.ci_wait_bound("r/x", RunsForge([runs]))
    assert secs == max(1800, math.ceil(1.3 * sorted(spans)[17]))
    assert secs < 6 * 3600


def test_log_lines_and_stop_reason_carry_the_bound(tmp_path):
    m = _load(tmp_path)
    log = Log()
    forge = CIForge(["pending"])
    forge.list_action_runs = RunsForge([commits(20, REAL, queue=3060)]).list_action_runs
    _wire_forge(m, forge)
    m.review_bound = lambda repo, path=None: 1
    m.dispatch_review = lambda *a, **k: pytest.fail("reviewed an unchecked sha")
    ok, why, rows = m.review_loop("operator/x", "0001-a", "plans/p.md", "PLAN",
                                  7, "br", tmp_path, None, None, "lease-1", log=log)
    assert not ok
    assert "within 360 minutes (slow after 77, history)" in why
    assert "timed out after" not in why
    phases = dict((p, f) for p, f in log.lines)
    assert phases["ci-bound"] == {"sha": SHA1[:8], "bound": 4631,
                                  "source": "history", "samples": 20,
                                  "p90": REAL}
    assert phases["ci-slow"]["bound"] == 4631
    assert phases["ci-timeout"]["bound"] == 4631
    assert phases["ci-timeout"]["ceiling"] == phases["ci-timeout"]["waited"] == 6 * 3600


def test_fallback_line_carries_a_reason_not_a_body(mod):
    log = Log()
    mod.ci_wait_bound("r/x", RunsForge([[]], fail_page=1, status=502),
                      log=log, sha=SHA1)
    assert log.lines[0][1]["reason"] == 502 and "p90" not in log.lines[0][1]


def test_unparseable_timestamps_are_a_fallback_not_an_empty_repo(mod):
    # Completed runs whose `stopped` does not parse are a read problem; calling
    # them "no history" would hide it.
    f = RunsForge([commits(3, REAL, stopped="not-a-time")])
    assert mod.ci_wait_bound("operator/eunomia", f) == (1800, "fallback")
    log = Log()
    mod.ci_wait_bound("operator/eunomia", f, log=log, sha=SHA1)
    assert log.lines[0][1]["reason"] == "unparsed"


# ------------------------------------------- slow is not stuck (2026-10-14)
def test_ci_that_goes_green_after_the_bound_is_reviewed_not_stopped(tmp_path):
    """#861 and #863, 2026-10-12: each review loop stopped at a 36-minute
    history bound, CI went green afterwards, and neither PR was reviewed for
    two days. In the review loop the bound is now where the wait is logged as
    slow; a pending state goes on being polled up to the ceiling."""
    m = _load(tmp_path)
    log = Log()
    polls_past_bound = (1800 // 30) + 40           # green 20 minutes after the bound
    forge = CIForge(["pending"] * polls_past_bound + ["success"])
    forge.list_action_runs = RunsForge([[]]).list_action_runs     # default bound, 30 min
    _wire_forge(m, forge)
    state, _ = m.await_ci("operator/x", SHA1, forge, None, log, bound=1800,
                          ceiling=m.CI_WAIT_CEILING_SECS)
    assert state == "success"
    phases = [p for p, _ in log.lines]
    assert phases.count("ci-slow") == 1 and "ci-timeout" not in phases


def test_a_direct_caller_still_stops_at_the_bound(tmp_path):
    """`ceiling` defaults to `bound`, so await_ci's other callers and the
    tests written for plan 0114 see the behaviour they always had."""
    m = _load(tmp_path)
    forge = CIForge(["pending"])
    state, _ = m.await_ci("r", SHA1, forge, None, sleep=lambda s: None,
                          bound=300, poll=30)
    assert state is None and forge.polls == 300 // 30 + 1


@pytest.mark.parametrize("states", [["unreadable"], ["", ""]])
def test_only_a_readable_pending_state_waits_past_the_bound(tmp_path, states):
    """Unreadable is not CI visibly running, and must not hold a slot for six
    hours; an empty status list is a repo without CI, decided as before."""
    m = _load(tmp_path)
    forge = CIForge(states, statuses=[] if states == ["", ""] else None)
    state, _ = m.await_ci("r", SHA1, forge, None, sleep=lambda s: None,
                          bound=300, poll=30, ceiling=6 * 3600)
    assert forge.polls * 30 <= 300 + 30
    assert state == ("success" if states == ["", ""] else None)
