"""Plan 0171: a plan whose `paths:` overlap an in-flight plan's is held until
that plan's pull request merges or closes. Plan 0179 D3: an orphaned lease,
or any lease of a finished plan, is never that hold's "in flight"."""
import json

import test_fleet_watch as tw
from test_plan_dependencies import _api_factory_multi

REPO = "operator/sniff"


def _plan(pid, paths, status="ready"):
    lines = "\n".join(f'  - "{p}"' for p in paths)
    return (f"---\nid: {pid}\nstatus: {status}\nrepo: {REPO}\nzone: public\n"
            f"tier: 2\npaths:\n{lines}\ndepends_on: []\n---\n# {pid}\n")


def _pr(n, pid, state="open"):
    return {"number": n, "state": state, "merged": state == "merged",
            "user": {"id": 0, "login": "implbot"},
            "body": f"body\n\nPlan: {pid}\n"}


def _run(tmp_path, plans, pulls, cap="3", lease_setup=None):
    """One cycle; returns (spawn count, events, module). `lease_setup(mod)`,
    when given, runs after the module loads and before the cycle — the hook
    plan 0179 D3's lease-liveness tests use to plant Q's own dispatch lease."""
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO,
                   FLEET_WATCH_CAP=cap)
    texts = {f"{pid}.md": _plan(pid, paths, st)
             for pid, (paths, st) in plans.items()}
    tw._wire(mod, _api_factory_multi(plan_texts=texts, pulls=pulls), orch)
    if lease_setup is not None:
        lease_setup(mod)
    n = mod.cycle()
    return n, tw._events(tmp_path / "fleet", "plan-blocked"), mod


P = "0200-p"
Q = "0100-q"
OWN = lambda pid: f"plans/{pid}.md"


def _two(p_paths, q_paths, q_status="done"):
    return {P: (p_paths + [OWN(P)], "ready"), Q: (q_paths + [OWN(Q)], q_status)}


def test_open_pr_holds_and_names_plan_and_path(tmp_path):
    n, ev, _ = _run(tmp_path, _two(["bin/orchestrator"], ["bin/orchestrator"]),
                    [_pr(7, Q)])
    assert n == 0
    assert len(ev) == 1
    e = ev[0]["detail"]
    assert e["plan"] == P and e["form"] == "overlap"
    assert e["dependency"] == f"overlap:{Q}"
    assert "bin/orchestrator" in e["detail"] and "#7" in e["detail"]


def test_merged_pr_releases_the_hold(tmp_path):
    n, ev, _ = _run(tmp_path, _two(["bin/orchestrator"], ["bin/orchestrator"]),
                    [_pr(7, Q, "merged")])
    assert n == 1 and ev == []


def test_closed_unmerged_pr_releases_the_hold(tmp_path):
    n, ev, _ = _run(tmp_path, _two(["bin/orchestrator"], ["bin/orchestrator"]),
                    [_pr(7, Q, "closed")])
    assert n == 1 and ev == []


def test_glob_against_file_overlaps(tmp_path):
    n, _, _ = _run(tmp_path, _two(["tests/**"], ["tests/test_x.py"]), [_pr(7, Q)])
    assert n == 0


def test_directory_prefix_overlaps(tmp_path):
    n, _, _ = _run(tmp_path, _two(["docs/"], ["docs/a.md"]), [_pr(7, Q)])
    assert n == 0


def test_disjoint_paths_do_not_hold(tmp_path):
    n, ev, _ = _run(tmp_path, _two(["bin/a"], ["bin/b"]), [_pr(7, Q)])
    assert n == 1 and ev == []


def test_own_plan_file_alone_never_overlaps(tmp_path):
    n, _, _ = _run(tmp_path, _two([], []), [_pr(7, Q)])
    assert n == 1


def test_two_ready_overlapping_plans_dispatch_only_the_lower_id(tmp_path):
    plans = {P: (["bin/x", OWN(P)], "ready"), Q: (["bin/x", OWN(Q)], "ready")}
    n, ev, _ = _run(tmp_path, plans, [])
    assert n == 1
    assert [e["detail"]["plan"] for e in ev] == [P]


def test_a_reland_pr_keeps_the_plan_in_flight(tmp_path):
    pulls = [_pr(7, Q, "merged"), _pr(9, Q, "open")]
    n, ev, _ = _run(tmp_path, _two(["bin/x"], ["bin/x"]), pulls)
    assert n == 0 and len(ev) == 1


def test_a_plan_in_another_repository_never_holds(tmp_path):
    plans = _two(["bin/x"], ["bin/x"])
    texts = {f"{pid}.md": _plan(pid, paths, st)
             for pid, (paths, st) in plans.items()}
    texts[f"{Q}.md"] = texts[f"{Q}.md"].replace(f"repo: {REPO}",
                                                "repo: operator/other")
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO,
                   FLEET_WATCH_CAP="3")
    tw._wire(mod, _api_factory_multi(plan_texts=texts, pulls=[_pr(7, Q)]), orch)
    assert mod.cycle() == 1


# ---------------------------------------------------------- D3: lease liveness
# Plan 0179 D3 (ADR-0029): an orphaned lease never holds ANOTHER plan out of
# dispatch, whatever its own plan's status; a `done`/`abandoned` plan is never
# in flight because of a lease, live or not. This only READS — it releases
# nothing, so Q's own lease is exactly as it was before the cycle either way.
def _q_lease(mod, plan_id=Q, live=True):
    """Q's own dispatch lease: `active` and live, or `active` but stale
    (orphaned by heartbeat) — never `released`, so a passing test can assert
    the cycle touched nothing about it."""
    mod.lib.write_lease({
        "id": "branch--q--001",
        "resource": {"type": "branch", "repo": REPO, "branch": f"feat/{plan_id}"},
        "holder": "some-session",
        "granted_by": "watch-test",
        "state": "active",
        "ttl_minutes": 60,
        "created": "2000-01-01T00:00:00Z",
        "activated": (mod.lib.ts(mod.lib.now_utc()) if live
                     else "2000-01-01T00:00:00Z"),
        "note": f"plan:{plan_id}",
    })


def _q_lease_state(mod):
    return json.loads((mod.lib.leases_dir() / "branch--q--001.json").read_text())


def test_a_done_plans_live_lease_does_not_hold(tmp_path):
    n, ev, mod = _run(tmp_path, _two(["bin/x"], ["bin/x"], q_status="done"), [],
                      lease_setup=lambda m: _q_lease(m, live=True))
    assert n == 1 and ev == []
    assert _q_lease_state(mod)["state"] == "active"


def test_a_ready_plans_orphaned_lease_does_not_hold(tmp_path):
    n, ev, mod = _run(tmp_path, _two(["bin/x"], ["bin/x"], q_status="ready"), [],
                      lease_setup=lambda m: _q_lease(m, live=False))
    assert n == 1 and ev == []
    assert _q_lease_state(mod)["state"] == "active", \
        "D3 only reads the lease — it is never released by the overlap check"


def test_a_ready_plans_live_lease_holds(tmp_path):
    n, ev, mod = _run(tmp_path, _two(["bin/x"], ["bin/x"], q_status="ready"), [],
                      lease_setup=lambda m: _q_lease(m, live=True))
    assert n == 0
    assert len(ev) == 1 and ev[0]["detail"]["dependency"] == f"overlap:{Q}"


def test_a_ready_plans_orphaned_lease_with_an_open_pr_holds(tmp_path):
    n, ev, mod = _run(tmp_path, _two(["bin/x"], ["bin/x"], q_status="ready"),
                      [_pr(7, Q)], lease_setup=lambda m: _q_lease(m, live=False))
    assert n == 0
    assert len(ev) == 1 and ev[0]["detail"]["dependency"] == f"overlap:{Q}"
