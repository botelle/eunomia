"""Plan 0093 — a watcher cycle reads each repo's pull requests once. Plan 0179
D2 — the dead leases of a finished plan are released, not skipped. Plan 0218
— the listing moved from `/pulls` to the issues index. A stub forge counts
list_pull_issues."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_watch import _load  # noqa: E402

REPO = "operator/eunomia"


class StubForge:
    def __init__(self, prs, fail_page=None, fail_status=500, fail_body=None):
        self.prs = prs
        self.fail_page = fail_page
        self.fail_status = fail_status
        self.fail_body = {} if fail_body is None else fail_body
        self.calls = []

    def list_pull_issues(self, repo, state="all", page=1, limit=50):
        self.calls.append((repo, page))
        if page == self.fail_page:
            return self.fail_status, self.fail_body
        return 200, self.prs[(page - 1) * limit: page * limit]


def _pr(n, plan, state="open", uid=42, merged=None, merged_at=None):
    """A raw issues-index item (plan 0218): `merged`/`merged_at` nested under
    `pull_request`, the same shape `list_pull_issues` returns."""
    return {"number": n, "state": state, "user": {"id": uid, "login": "x"},
            "body": f"Plan: {plan}\n",
            "pull_request": {"merged": merged, "merged_at": merged_at}}


@pytest.fixture
def mod(tmp_path, monkeypatch):
    m = _load(tmp_path, FLEET_IMPLEMENTER_UIDS="42")
    monkeypatch.setattr(m, "_notify_once", lambda *a, **k: None)
    monkeypatch.setattr(m, "_clear_notified", lambda *a, **k: None)
    return m


def _install(mod, monkeypatch, forge):
    monkeypatch.setattr(mod, "_forge", lambda token: forge)
    return forge


IDS = ["0009-auth", "0009-auth-v2", "0001-a", "nope"]
CASES = {
    "open-before-closed": [_pr(1, "0001-a", "closed"), _pr(2, "0001-a", "open")],
    "closed-before-open": [_pr(1, "0001-a", "open"), _pr(2, "0001-a", "closed")],
    "near-miss": [_pr(1, "0009-auth-v2")],
    "non-implementer": [_pr(1, "0001-a", uid=7)],
    "closed-only": [_pr(1, "0009-auth", "closed")],
}


def _answers(mod, forge_prs, memo, monkeypatch, **kw):
    forge = _install(mod, monkeypatch, StubForge(forge_prs, **kw))
    mod._pulls_memo = {} if memo else None
    try:
        return {i: mod.marked_pr(REPO, i, "t") for i in IDS}, forge
    finally:
        mod._pulls_memo = None


@pytest.mark.parametrize("name", CASES)
def test_memo_answers_match_unmemoised(mod, monkeypatch, name):
    a, _ = _answers(mod, CASES[name], True, monkeypatch)
    b, _ = _answers(mod, CASES[name], False, monkeypatch)
    assert a == b


def test_500_page_is_unreadable_and_never_retried(mod, monkeypatch):
    a, forge = _answers(mod, [_pr(1, "x")], True, monkeypatch, fail_page=1)
    assert set(a.values()) == {"unreadable"}
    assert len(forge.calls) == 1


def test_500_page_names_repo_operation_page_and_status(mod, monkeypatch):
    """Plan 0187: a failed listing explains itself -- repo, the operation
    (list_pull_issues), the page it failed on, and the HTTP status -- in ONE
    line, logged exactly once for the whole cycle no matter how many plans
    later ask about this repo's dedupe."""
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    _answers(mod, [_pr(1, "x")], True, monkeypatch, fail_page=1, fail_status=503)
    causes = [w for w in warned if "list_pull_issues" in w]
    assert len(causes) == 1, warned
    assert REPO in causes[0] and "page 1" in causes[0] and "HTTP 503" in causes[0]


def test_a_second_page_failure_names_page_two_not_page_one(mod, monkeypatch):
    """The page NUMBER in the cause line must be the page that actually
    failed -- a 50-item first page that succeeds, then a 503 on the second
    read, must say "page 2", never "page 1"."""
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    prs = [_pr(i, f"p{i}") for i in range(1, 51)]        # exactly one full page
    _answers(mod, prs, True, monkeypatch, fail_page=2, fail_status=503)
    causes = [w for w in warned if "list_pull_issues" in w]
    assert len(causes) == 1, warned
    assert "page 2" in causes[0] and "page 1" not in causes[0]


def test_transport_failure_is_distinguished_from_an_http_status(mod, monkeypatch):
    """status 0 is fleetforge.py's own collapse of every transport failure
    (timeout, DNS, connection refusal) -- never a fabricated HTTP status."""
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    _answers(mod, [_pr(1, "x")], True, monkeypatch, fail_page=1, fail_status=0)
    causes = [w for w in warned if "list_pull_issues" in w]
    assert len(causes) == 1, warned
    assert "transport error" in causes[0]
    assert not re.search(r"HTTP \d", causes[0]), causes[0]


def test_malformed_200_is_distinguished_from_both(mod, monkeypatch):
    """A 200 whose body is not a list (truncated JSON, or an object instead
    of an array) is neither a transport failure nor an HTTP error -- it is
    reported as malformed, which is its own, third, category."""
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    _answers(mod, [_pr(1, "x")], True, monkeypatch, fail_page=1,
              fail_status=200, fail_body={"not": "a list"})
    causes = [w for w in warned if "list_pull_issues" in w]
    assert len(causes) == 1, warned
    assert "malformed" in causes[0]
    assert not re.search(r"HTTP \d", causes[0]) and "transport error" not in causes[0]


def test_warn_lines_carry_an_explicit_utc_timestamp(mod, capsys):
    mod.warn("something failed")
    err = capsys.readouterr().err
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z fleet-watch: WARN ", err), err


def test_dedupe_failure_warnings_point_back_to_the_logged_cause(mod, monkeypatch):
    """Plan 0187: `marked_pr`'s single per-repo-per-cycle cause (above) is the
    one place the detail lives -- every later plan-level 'dedupe unreadable'
    warning must point back to it rather than repeat (or, worse, omit) it."""
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    a, _ = _answers(mod, [_pr(1, "x")], True, monkeypatch, fail_page=1)
    assert a["0001-a"] == "unreadable"
    detail = [w for w in warned if "list_pull_issues" in w]
    assert len(detail) == 1, warned


def test_page_cap_is_unreadable_memoised(mod, monkeypatch):
    monkeypatch.setattr(mod, "ISSUE_PAGE_CAP", 2)
    prs = [_pr(i, f"p{i}") for i in range(1, 101)]
    a, forge = _answers(mod, prs, True, monkeypatch)
    assert a["nope"] == "unreadable"
    assert len(forge.calls) == 2          # one pass, not one per id
    b, _ = _answers(mod, prs, False, monkeypatch)
    assert a == b


def test_outside_a_cycle_every_call_reads(mod, monkeypatch):
    forge = _install(mod, monkeypatch, StubForge([_pr(1, "a")]))
    mod.marked_pr(REPO, "z", "t")
    mod.marked_pr(REPO, "z", "t")
    assert len(forge.calls) == 4          # page 1 + empty page 2, twice


def _sweep_setup(mod, monkeypatch, plans, plan_status):
    monkeypatch.setattr(mod, "repos", lambda: [REPO])
    leases = {(REPO, p): {"id": f"lease-{p}", "note": f"plan:{p}"} for p in plans}
    monkeypatch.setattr(mod, "dispatch_leases", lambda: leases)
    monkeypatch.setattr(mod.lib, "is_orphaned", lambda rec: True)
    monkeypatch.setattr(mod.lib, "all_leases", lambda: [])
    if plan_status is None:
        monkeypatch.setattr(mod, "plan_files", lambda r, t: None)
    else:
        monkeypatch.setattr(mod, "plan_files", lambda r, t: [
            (f"plans/{p}.md", {"id": p, "status": s})
            for p, s in plan_status.items()])
    return _install(mod, monkeypatch, StubForge([]))


def _write_orphaned_lease(mod, lease_id, repo, plan_id):
    """A real, genuinely-orphaned (ADR-0029 §3's re-derivation is real, even
    though the process under test is not) `assigned` lease on disk, so
    `fleet-release --expect-orphaned` — a separate subprocess with its own
    fresh read of fleetlib — agrees it may release it."""
    mod.lib.write_lease({
        "id": lease_id,
        "resource": {"type": "branch", "repo": repo, "branch": f"feat/{plan_id}"},
        "holder": "dead-session",
        "granted_by": "watch-test",
        "state": "assigned",
        "ttl_minutes": 60,
        "created": "2000-01-01T00:00:00Z",      # long past ACTIVATE_TIMEOUT_MIN
        "activated": None,
        "note": f"plan:{plan_id}",
    })


def test_finished_plans_are_released_with_their_reason(mod, monkeypatch):
    monkeypatch.setattr(mod, "repos", lambda: [REPO])
    monkeypatch.setattr(mod, "plan_files", lambda r, t: [
        ("plans/a.md", {"id": "a", "status": "done"}),
        ("plans/b.md", {"id": "b", "status": "abandoned"})])
    forge = _install(mod, monkeypatch, StubForge([]))
    _write_orphaned_lease(mod, "branch--a--001", REPO, "a")
    _write_orphaned_lease(mod, "branch--b--001", REPO, "b")
    pages = []
    monkeypatch.setattr(mod, "_notify_once", lambda *a, **k: pages.append(a) or True)
    mod._stat_finished_released = mod._stat_pages = 0
    assert mod.orphan_sweep("t", 0) == 0
    assert forge.calls == [], "no pull-request read for a finished plan's lease"
    assert pages == [], "no page when nothing is pending on either lease"
    recs = {r["id"]: r for r in mod.lib.all_leases()}
    assert recs["branch--a--001"]["state"] == "released"
    assert recs["branch--b--001"]["state"] == "released"
    events = [json.loads(l) for l in
             (mod.lib.fleet_dir() / "events.jsonl").read_text().splitlines()]
    reasons = {e["lease"]: (e.get("detail") or {}).get("reason")
              for e in events if e.get("type") == "lease-released"}
    assert reasons == {"branch--a--001": "plan-done",
                       "branch--b--001": "plan-abandoned"}
    assert mod._stat_finished_released == 2
    assert "2 finished-plan lease(s) released" in mod.cost_note()


def test_unreadable_status_sweeps_as_today(mod, monkeypatch):
    forge = _sweep_setup(mod, monkeypatch, ["a"], None)
    mod._stat_finished_released = mod._stat_pages = 0
    mod.orphan_sweep("t", 0)
    assert forge.calls                    # the listing was read
    assert mod._stat_finished_released == 0


def test_cycle_reads_listing_once_per_repo(mod, monkeypatch, tmp_path):
    """The real cycle(): 3 orphaned leases and 4 ready plans in one repo."""
    import test_fleet_watch as tfw
    repo = "operator/sniff"
    orph, ready = ["o1", "o2", "o3"], ["r0", "r1", "r2", "r3"]
    prs = [_pr(i, p) for i, p in enumerate(orph + ready, 1)]
    calls = []
    orch, _ = tfw._stub_orchestrator(tmp_path)
    tfw._wire(mod, tfw._api_factory(pulls=prs, calls=calls), orch)
    monkeypatch.setattr(mod, "repos", lambda: [repo])
    monkeypatch.setattr(mod, "plan_files", lambda r, t: [
        (f"plans/{p}.md", {"id": p, "status": "ready", "repo": repo})
        for p in orph + ready])
    leases = {(repo, p): {"id": f"lease-{p}", "note": f"plan:{p}"}
              for p in orph}
    monkeypatch.setattr(mod, "dispatch_leases", lambda: leases)
    monkeypatch.setattr(mod.lib, "is_orphaned", lambda rec: True)
    monkeypatch.setattr(mod.lib, "all_leases", lambda: [])
    mod.cycle()
    reads = [c for c in calls
            if "/issues?" in c[1] and "type=pulls" in c[1] and "page=1" in c[1]]
    assert len(reads) == 1
    assert mod._pulls_memo is None and mod._plans_memo is None
    assert mod._stat_pages == len(
        [c for c in calls if "/issues?" in c[1] and "type=pulls" in c[1]])


def test_memo_closed_on_exception(mod, monkeypatch):
    def boom(dry_run=False):
        raise RuntimeError("x")
    monkeypatch.setattr(mod, "_cycle", boom)
    with pytest.raises(RuntimeError):
        mod.cycle()
    assert mod._pulls_memo is None and mod._plans_memo is None


def test_cost_note_has_duration_and_pages(mod):
    mod._stat_secs, mod._stat_pages = 12.4, 3
    mod._stat_finished_released = mod._stat_finished_pending = 0
    mod._stat_finished_failed = 0
    mod._stat_phase_question = mod._stat_phase_orphan = 0.0
    mod._stat_phase_lane = mod._stat_phase_meeting = 0.0
    mod._stat_phase_main_health = mod._stat_phase_dispatch = 0.0
    assert mod.cost_note() == (
        "; took 12s, 3 forge list page(s); phases: question=0s orphan=0s "
        "lane=0s meeting=0s main-health=0s dispatch=0s")
