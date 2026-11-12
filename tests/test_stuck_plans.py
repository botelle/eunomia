"""Plan 0211: a `ready` plan whose newest marked PR is merged or closed is
reported as blocked, with its PR, every cycle (D1); a `path:` dependency
nothing in the fleet will ever produce is reported `orphan` rather than an
ordinary `unmet` (D1b); mopsus pages each stuck plan once, with a handle and
a three-action shortlist (D4/D5).

Two surfaces, one file, because that is where the plan's own `paths` lease
put them (the same precedent `test_mopsus_review_retry.py`'s own docstring
gives): `bin/fleet-watch`'s dedupe branch and dependency check, and
`bin/mopsus`'s stuck-plan sweep. `test_fleet_watch.py`'s own fixtures
(`_rd_world`, `_load`, `_wire`, `_events`, `_merged_pr`, `_redispatch`,
`_iso`) and `test_plan_dependencies.py`'s `_api_factory_multi` are imported
rather than restated.
"""
import importlib.machinery
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_fleet_watch as tw
from test_plan_dependencies import _api_factory_multi

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


# =============================================================== fleet-watch
# D1: the dedupe branch classifies a closed/merged marked PR instead of
# skipping it, and D1b: an unproduced `path:` dependency is `orphan`.


def _world(tmp_path, **kw):
    """`test_fleet_watch._rd_world`, with the real UsageBar meter (plan 0094)
    disabled — it reads a fixed, non-test-isolated path, so a live closed
    gate on this host would otherwise hold operator/sniff's dispatch
    nondeterministically."""
    mod, fd, log, notes, reads = tw._rd_world(tmp_path, **kw)
    mod._quota = lambda: None
    return mod, fd, log, notes, reads


def test_merged_pr_with_no_partial_line_is_built_not_done(tmp_path):
    mod, fd, log, notes, _ = _world(tmp_path, comments=[])
    assert mod.cycle() == 0
    assert tw._spawns(log) == []
    evs = tw._events(fd, "plan-blocked")
    assert len(evs) == 1
    detail = evs[0]["detail"]
    assert detail["form"] == "built"
    assert detail["reason"] == "built-not-done"
    assert detail["pr"] == 7
    assert evs[0]["detail"]["dependency"] == "pr:#7"
    blocked_pages = [n for n in notes if n[0] == "Plan blocked"]
    assert blocked_pages == [], \
        "the built/rejected forms are paged by mopsus, not here"


def test_stuck_report_survives_three_cycles_as_one_event(tmp_path):
    """The regression this plan's DoD names: with `_clear_blocked` left where
    it ran before (right after the overlap check, ahead of the dedupe scan),
    every cycle clears the very state file this test checks survives, and
    the event fires again each time."""
    mod, fd, log, notes, _ = _world(tmp_path, comments=[])
    for _ in range(3):
        assert mod.cycle() == 0
    evs = tw._events(fd, "plan-blocked")
    assert len(evs) == 1


def test_partial_line_with_remainder(tmp_path):
    body = "Plan: 0009-thing\n\nPlan-partial: 0009-thing\nRemainder: 0214-successor\n"
    mod, fd, log, notes, _ = _world(
        tmp_path, pulls=[tw._merged_pr(body=body)], comments=[])
    assert mod.cycle() == 0
    detail = tw._events(fd, "plan-blocked")[0]["detail"]
    assert detail["reason"] == "partial"
    assert detail["detail"] == "0214-successor"


def test_partial_line_without_a_remainder_line_says_so(tmp_path):
    body = "Plan: 0009-thing\n\nPlan-partial: 0009-thing\n"
    mod, fd, log, notes, _ = _world(
        tmp_path, pulls=[tw._merged_pr(body=body)], comments=[])
    assert mod.cycle() == 0
    detail = tw._events(fd, "plan-blocked")[0]["detail"]
    assert detail["reason"] == "partial"
    assert detail["detail"] == "no Remainder: line"


def test_partial_line_for_another_plan_does_not_count(tmp_path):
    body = "Plan: 0009-thing\n\nPlan-partial: 0009-thing-v2\nRemainder: x\n"
    mod, fd, log, notes, _ = _world(
        tmp_path, pulls=[tw._merged_pr(body=body)], comments=[])
    assert mod.cycle() == 0
    detail = tw._events(fd, "plan-blocked")[0]["detail"]
    assert detail["reason"] == "built-not-done"


def test_closed_unmerged_pr_is_rejected(tmp_path):
    pr = tw._merged_pr(merged=False, merged_at=None)
    mod, fd, log, notes, _ = _world(tmp_path, pulls=[pr], comments=[])
    assert mod.cycle() == 0
    detail = tw._events(fd, "plan-blocked")[0]["detail"]
    assert detail["form"] == "rejected"
    assert detail["reason"] == "closed-unmerged"


def test_fresh_redispatch_spawns_and_reports_nothing(tmp_path):
    mod, fd, log, notes, _ = _world(tmp_path, comments=[tw._redispatch()])
    assert mod.cycle() == 1
    assert tw._events(fd, "plan-blocked") == []


def test_unreadable_comments_report_nothing(tmp_path, monkeypatch):
    mod, fd, log, notes, _ = _world(tmp_path, comments=[])
    monkeypatch.setattr(mod, "_comments_since", lambda *a, **k: None)
    assert mod.cycle() == 0
    assert tw._spawns(log) == []
    assert tw._events(fd, "plan-blocked") == []


def test_a_newer_marked_pr_is_a_new_event(tmp_path):
    pulls = [tw._merged_pr(5)]
    mod, fd, log, notes, _ = _world(tmp_path, pulls=pulls, comments=[])
    assert mod.cycle() == 0
    evs1 = tw._events(fd, "plan-blocked")
    assert len(evs1) == 1 and evs1[0]["detail"]["pr"] == 5
    pulls.append(tw._merged_pr(8))
    assert mod.cycle() == 0
    evs2 = tw._events(fd, "plan-blocked")
    assert len(evs2) == 2 and evs2[1]["detail"]["pr"] == 8


def test_stuck_plan_never_writes_to_the_forge_or_spawns(tmp_path, monkeypatch):
    mod, fd, log, notes, _ = _world(tmp_path, comments=[])
    methods = []
    forge = mod._forge("tok")
    orig = forge._dispatch

    def recording(method, path, body=None, token=None):
        methods.append(method)
        return orig(method, path, body, token)
    monkeypatch.setattr(forge, "_dispatch", recording)
    monkeypatch.setattr(mod, "_forge", lambda token: forge)

    real_popen = mod.subprocess.Popen

    def guarded(argv, *a, **k):
        if os.path.basename(str(argv[0])) != "orch-stub.sh" or "--ready" not in argv:
            raise AssertionError(f"Popen called for a dispatch: {argv!r}")
        return real_popen(argv, *a, **k)
    monkeypatch.setattr(mod.subprocess, "Popen", guarded)
    assert mod.cycle() == 0
    assert methods and all(m == "GET" for m in methods), methods


# --------------------------------------------------------------- D1b orphans

def _mod(tmp_path):
    return tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")


def test_orphan_when_no_plan_leases_the_path(tmp_path, monkeypatch):
    mod = _mod(tmp_path)
    monkeypatch.setattr(mod, "_path_exists_on_main", lambda repo, p, token: False)
    mod._cycle_producers = {"operator/sniff": []}
    b = mod._plan_dependency_block("operator/sniff", ["path:lib/x.md"], {}, "t",
                                   plan_id="w")
    assert b[1] == "path" and b[2] == "orphan"
    assert b[3] == "no plan leases 'lib/x.md'"


def test_not_orphan_when_a_ready_plan_leases_the_path(tmp_path, monkeypatch):
    mod = _mod(tmp_path)
    monkeypatch.setattr(mod, "_path_exists_on_main", lambda repo, p, token: False)
    mod._cycle_producers = {"operator/sniff": [("p", ["lib/x.md"])]}
    b = mod._plan_dependency_block("operator/sniff", ["path:lib/x.md"], {}, "t",
                                   plan_id="w")
    assert b[2] == "unmet"


def test_orphan_when_leased_only_by_the_waiting_plan_itself(tmp_path, monkeypatch):
    mod = _mod(tmp_path)
    monkeypatch.setattr(mod, "_path_exists_on_main", lambda repo, p, token: False)
    mod._cycle_producers = {"operator/sniff": [("w", ["lib/x.md"])]}
    b = mod._plan_dependency_block("operator/sniff", ["path:lib/x.md"], {}, "t",
                                   plan_id="w")
    assert b[2] == "orphan"


def test_orphan_when_unknown_producer(tmp_path, monkeypatch):
    """`_cycle_producers` carries no entry at all for the repo — the repo was
    held, skipped, unreadable, or past a cap break this cycle. Unknown is
    never guessed as orphan."""
    mod = _mod(tmp_path)
    monkeypatch.setattr(mod, "_path_exists_on_main", lambda repo, p, token: False)
    mod._cycle_producers = {}
    b = mod._plan_dependency_block("operator/sniff", ["path:lib/x.md"], {}, "t",
                                   plan_id="w")
    assert b[2] == "unmet"


def test_cross_repo_edge_produced_elsewhere_is_not_orphan(tmp_path, monkeypatch):
    mod = _mod(tmp_path)
    monkeypatch.setattr(mod, "conf_repos",
                        lambda: (["operator/sniff"],
                                 ["operator/sniff", "operator/other"]))
    monkeypatch.setattr(mod, "_path_exists_on_main", lambda repo, p, token: False)
    mod._cycle_producers = {"operator/sniff": [], "operator/other": [("p", ["lib/x.md"])]}
    b = mod._plan_dependency_block(
        "operator/sniff", ["path:operator/other:lib/x.md"], {}, "t", plan_id="w")
    assert b[2] == "unmet"


def test_cross_repo_edge_into_an_unread_repo_is_not_orphan(tmp_path, monkeypatch):
    mod = _mod(tmp_path)
    monkeypatch.setattr(mod, "conf_repos",
                        lambda: (["operator/sniff"],
                                 ["operator/sniff", "operator/other"]))
    monkeypatch.setattr(mod, "_path_exists_on_main", lambda repo, p, token: False)
    mod._cycle_producers = {"operator/sniff": []}   # "operator/other" never scanned
    b = mod._plan_dependency_block(
        "operator/sniff", ["path:operator/other:lib/x.md"], {}, "t", plan_id="w")
    assert b[2] == "unmet"


W_DEP = """---
id: 0010-w
status: ready
repo: operator/sniff
zone: public
tier: 2
depends_on: ["path:lib/x.md"]
---
# W
"""

D_DONE = """---
id: 0011-d
status: done
repo: operator/sniff
zone: public
tier: 2
paths: ["lib/x.md"]
---
# D
"""

P_GLOB = """---
id: 0012-g
status: ready
repo: operator/sniff
zone: public
tier: 2
paths: ["lib/**"]
---
# G
"""

W_TWO_DEPS = """---
id: 0013-w
status: ready
repo: operator/sniff
zone: public
tier: 2
depends_on: ["0099-missing", "path:lib/y.md"]
---
# W
"""


def _watch_multi(tmp_path, plan_texts, cap="3"):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP=cap)
    api = _api_factory_multi(plan_texts=plan_texts)
    tw._wire(mod, api, orch)
    # The real UsageBar meter (plan 0094) is read from a fixed path outside
    # tmp_path — never test-isolated — so a live closed gate on THIS host
    # would otherwise hold operator/sniff's dispatch nondeterministically.
    mod._quota = lambda: None
    return mod


def test_orphan_when_only_producer_is_done(tmp_path):
    mod = _watch_multi(tmp_path, {"0010-w.md": W_DEP, "0011-d.md": D_DONE})
    assert mod.cycle() == 0
    evs = tw._events(tmp_path / "fleet", "plan-blocked")
    e = next(e for e in evs if e["detail"]["plan"] == "0010-w")
    assert e["detail"]["reason"] == "orphan"


def test_not_orphan_under_a_glob_lease(tmp_path):
    mod = _watch_multi(tmp_path, {"0010-w.md": W_DEP, "0012-g.md": P_GLOB})
    assert mod.cycle() == 1    # 0012-g dispatches; 0010-w stays blocked
    evs = tw._events(tmp_path / "fleet", "plan-blocked")
    e = next(e for e in evs if e["detail"]["plan"] == "0010-w")
    assert e["detail"]["reason"] == "unmet"


def test_orphans_field_names_a_later_orphaned_edge(tmp_path):
    mod = _watch_multi(tmp_path, {"0013-w.md": W_TWO_DEPS})
    assert mod.cycle() == 0
    data = json.loads((tmp_path / "fleet" / "blocked.json").read_text())
    entry = next(e for e in data["blocked"] if e["plan"] == "0013-w")
    assert entry["dependency"] == "0099-missing"
    assert entry["reason"] == "dangling"
    assert entry["orphans"] == ["path:lib/y.md"]


# ===================================================================== mopsus
# D4/D5: the stuck-plan sweep.


def _mp():
    return _load("mopsus_for_stuck_plans", BIN / "mopsus")


@pytest.fixture
def mp():
    return _mp()


@pytest.fixture(autouse=True)
def _wire_mp(mp, monkeypatch, tmp_path):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    monkeypatch.delenv("FLEET_ANGELIA_URL", raising=False)
    calls = []
    monkeypatch.setattr(mp.orch, "notify",
                        lambda title, body: (calls.append((title, body)), True)[1])
    return calls


def _mp_iso(delta_hours=0):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry(repo="operator/sniff", plan="0009-thing", pr=7, form="built",
          reason="built-not-done",
          detail="#7 merged 2026-11-05T00:00:00Z; the plan still says ready"):
    return {"repo": repo, "plan": plan, "dependency": f"pr:#{pr}", "form": form,
           "reason": reason, "detail": detail, "since": _mp_iso(-1), "pr": pr,
           "handle": None, "orphans": []}


def _write_blocked(fleet_dir, entries, written_at=None):
    data = {"v": 1, "written_at": written_at or _mp_iso(), "cycle_complete": True,
           "held_repos": [], "skipped_repos": [], "blocked": entries}
    fleet_dir.mkdir(parents=True, exist_ok=True)
    (fleet_dir / "blocked.json").write_text(json.dumps(data))


def test_stuck_sweep_pages_once_across_three_sweeps(mp, tmp_path, _wire_mp):
    fd = tmp_path / ".fleet"
    _write_blocked(fd, [_entry()])
    for _ in range(3):
        mp.stuck_sweep()
    assert len(_wire_mp) == 1
    state = mp._load_state()
    assert len(state["stuck_paged"]) == 1


def test_a_new_pr_on_the_same_plan_pages_again(mp, tmp_path, _wire_mp):
    fd = tmp_path / ".fleet"
    _write_blocked(fd, [_entry(pr=7)])
    mp.stuck_sweep()
    assert len(_wire_mp) == 1
    _write_blocked(fd, [_entry(pr=8)])
    mp.stuck_sweep()
    assert len(_wire_mp) == 2


def test_stale_snapshot_pages_nothing(mp, tmp_path, _wire_mp):
    fd = tmp_path / ".fleet"
    _write_blocked(fd, [_entry()], written_at=_mp_iso(-3))
    mp.stuck_sweep()
    assert _wire_mp == []
    state = mp._load_state()
    assert state["stuck_paged"] == {}


def test_mode_off_pages_nothing(mp, tmp_path, _wire_mp, monkeypatch):
    fd = tmp_path / ".fleet"
    _write_blocked(fd, [_entry(repo="operator/off")])
    monkeypatch.setattr(mp, "load_conf", lambda path=None: {"operator/off": "off"})
    mp.stuck_sweep()
    assert _wire_mp == []


def test_first_sweep_sends_one_summary_for_three_plans(mp, tmp_path, _wire_mp):
    fd = tmp_path / ".fleet"
    _write_blocked(fd, [_entry(plan="0001-a", pr=1), _entry(plan="0002-b", pr=2),
                        _entry(plan="0003-c", pr=3)])
    mp.stuck_sweep()
    assert len(_wire_mp) == 1
    title, body = _wire_mp[0]
    assert title == "mopsus: first stuck-plan sweep"
    assert "3 stuck plans found" in body
    assert "mopsus list" in body
    assert len(mp._load_state()["stuck_paged"]) == 3


def test_list_and_json_show_the_stuck_handle(mp, tmp_path, _wire_mp, capsys):
    fd = tmp_path / ".fleet"
    _write_blocked(fd, [_entry()])
    mp.stuck_sweep()
    handle = mp.list_stuck_handles()[0][0]
    mp.cmd_list()
    out = capsys.readouterr().out
    assert f"{handle}\toperator/sniff\t0009-thing\tstuck:#7" in out
    view = mp.handle_view(handle)
    assert view["class"] == "stuck-plan"
    assert view["repo"] == "operator/sniff" and view["plan_id"] == "0009-thing"
    assert view["lease"] is None and view["pr"] == 7
    assert view["actions"][0]["action"] == "redispatch"
    assert view["actions"][0]["pr"] == 7
    assert "minos" in view["actions"][0]["links"]
    assert [a["action"] for a in view["actions"]] == \
        ["redispatch", "mark-done", "abandon"]


def test_partial_reason_brief_shows_remainder(mp, tmp_path, _wire_mp):
    fd = tmp_path / ".fleet"
    _write_blocked(fd, [_entry(reason="partial", detail="0214-successor")])
    mp.stuck_sweep()
    handle = mp.list_stuck_handles()[0][0]
    brief = mp._read_brief(handle)
    assert "Remainder: 0214-successor" in brief


def test_stuck_sweep_never_spawns_or_writes(mp, tmp_path, _wire_mp, monkeypatch):
    calls = []

    def no_popen(*a, **k):
        calls.append(a)
        raise AssertionError("Popen called")

    def no_run(*a, **k):
        calls.append(a)
        raise AssertionError("subprocess.run called")
    monkeypatch.setattr(mp.subprocess, "Popen", no_popen)
    monkeypatch.setattr(mp.subprocess, "run", no_run)
    fd = tmp_path / ".fleet"
    _write_blocked(fd, [_entry()])
    mp.stuck_sweep()
    assert calls == []
