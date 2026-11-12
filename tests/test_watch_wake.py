"""Tests for `bin/fleet-watch`'s wake-spool consumer (plan 0181 D4):
`_take_wakes`, `_cycle_preflight`, `_service_wakes`, and `_dispatch_one`
(the per-plan body `_cycle`'s own dispatch loop also calls, with no change
in behaviour — that equivalence is what `test_fleet_watch.py`'s existing
dispatch suite already proves by continuing to pass unmodified)."""
import hashlib
import json
import time
from pathlib import Path

import pytest

import test_fleet_watch as tw

PLAN = tw.PLAN   # 0009-thing, ready, operator/sniff, no depends_on

DEPENDENT = """---
id: 0009-thing
status: ready
repo: operator/sniff
zone: public
tier: 2
depends_on: [0008-dep]
---
# Thing
"""

DEP_NOT_DONE = """---
id: 0008-dep
status: ready
repo: operator/sniff
zone: public
tier: 2
---
# Dep
"""

DEP_DONE = """---
id: 0008-dep
status: done
repo: operator/sniff
zone: public
tier: 2
---
# Dep
"""


def _wake_name(repo, plan):
    return hashlib.sha1(f"{repo}|{plan}".encode()).hexdigest() + ".json"


def _write_wake(fleet_dir, repo, plan, reason="dependency-cleared"):
    d = Path(fleet_dir) / "wake"
    d.mkdir(parents=True, exist_ok=True)
    p = d / _wake_name(repo, plan)
    p.write_text(json.dumps({"repo": repo, "plan": plan, "reason": reason,
                             "at": time.time()}))
    return p


def _setup(tmp_path, plans=("0009-thing.md",), plan_text=PLAN, protected=True,
          orch_env=None, pulls=None):
    fd = tmp_path / "fleet"
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_WATCH_CAP="2")
    tw._wire(mod, tw._api_factory(protected=protected, plans=plans,
                                  plan_text=plan_text, pulls=pulls), orch)
    # Isolate from this host's real, date-dependent usage-limit config and
    # main-health cache: wake dispatch is under test here, not those.
    mod._quota = lambda: None
    mod.main_health = lambda repo, token, dry_run=False: {"state": "green"}
    return mod, fd, log


# ----------------------------------------------------------------- _take_wakes

def test_a_wake_file_is_consumed_once_and_a_malformed_file_is_removed(tmp_path):
    mod, fd, _log = _setup(tmp_path)
    good = _write_wake(fd, "operator/sniff", "0009-thing")
    bad = (fd / "wake" / "deadbeef0000000000000000000000000000beef.json")
    bad.write_text("{not json")
    out = mod._take_wakes()
    assert out == [("operator/sniff", "0009-thing")]
    assert not good.exists()
    assert not bad.exists()
    # consume-once: a second call finds nothing
    assert mod._take_wakes() == []


def test_take_wakes_leaves_a_tmp_file_in_place(tmp_path):
    mod, fd, _log = _setup(tmp_path)
    d = fd / "wake"
    d.mkdir(parents=True)
    tmp = d / ".tmp-abcdef"
    tmp.write_text("partial")
    assert mod._take_wakes() == []
    assert tmp.exists()


# ------------------------------------------------------------- _service_wakes

def test_a_woken_plan_whose_hold_has_cleared_is_dispatched(tmp_path):
    mod, fd, log = _setup(tmp_path)
    _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes()
    assert n == 1
    assert len(tw._wait_spawns(log, 1)) == 1


def test_a_woken_plan_that_is_still_blocked_is_consumed_without_a_dispatch(tmp_path):
    import test_plan_dependencies as tpd
    fd = tmp_path / "fleet"
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_WATCH_CAP="2")
    api = tpd._api_factory_multi(
        plan_texts={"0008-dep.md": DEP_NOT_DONE, "0009-thing.md": DEPENDENT})
    tw._wire(mod, api, orch)
    mod._quota = lambda: None
    mod.main_health = lambda repo, token, dry_run=False: {"state": "green"}
    _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes()
    assert n == 0
    assert tw._spawns(log) == []
    assert not (fd / "wake" / _wake_name("operator/sniff", "0009-thing")).exists()


def test_a_wake_for_a_repo_not_in_repos_does_nothing(tmp_path):
    mod, fd, log = _setup(tmp_path)
    _write_wake(fd, "operator/other", "0001-x")
    n = mod._service_wakes()
    assert n == 0
    assert tw._spawns(log) == []


def test_a_closed_quota_gate_holds_the_wake_dispatch(tmp_path):
    mod, fd, log = _setup(tmp_path)

    class _Quota:
        def observe(self):
            pass

        def closed_vendor_gates(self):
            return {"vendor:codex": ("closed", "2099-01-01T00:00:00Z")}
    mod._quota = lambda: _Quota()
    mod._hold_orch = None

    class _Orch:
        def seat_spend(self, repo):
            return {"vendor:codex": "implementer#1"}
    mod._orchestrator_module = lambda: _Orch()
    _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes()
    assert n == 0
    assert tw._spawns(log) == []


def test_an_unprotected_repo_is_not_dispatched_by_a_wake(tmp_path):
    mod, fd, log = _setup(tmp_path, protected=False)
    _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes()
    assert n == 0
    assert tw._spawns(log) == []


def test_a_red_main_holds_the_wake_dispatch(tmp_path):
    mod, fd, log = _setup(tmp_path)
    mod.main_health = lambda repo, token, dry_run=False: {"state": "red"}
    _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes()
    assert n == 0
    assert tw._spawns(log) == []


def test_a_plan_dispatched_by_a_wake_is_not_dispatched_again_by_the_loop(tmp_path):
    mod, fd, log = _setup(tmp_path)
    _write_wake(fd, "operator/sniff", "0009-thing")
    assert mod._service_wakes() == 1
    assert len(tw._wait_spawns(log, 1)) == 1
    assert mod.cycle() == 0
    assert len(tw._spawns(log)) == 1


def test_an_empty_spool_is_a_cheap_no_op(tmp_path):
    mod, fd, log = _setup(tmp_path)
    assert mod._service_wakes() == 0
    assert tw._spawns(log) == []


def test_a_failed_preflight_leaves_the_wake_file_in_place(tmp_path):
    mod, fd, log = _setup(tmp_path)
    mod._token = lambda: (None, "token helper failed")
    p = _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes()
    assert n == 0
    assert p.exists()


def test_dry_run_counts_but_writes_nothing(tmp_path):
    mod, fd, log = _setup(tmp_path)
    p = _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes(dry_run=True)
    assert n == 1
    assert tw._spawns(log) == []
    # A dry run previews and mutates nothing (Boundaries) — the wake file is
    # a fact on disk, and consuming it here would cost an armed rerun the one
    # real wake that cleared the hold.
    assert p.exists()


def test_dry_run_cycle_does_not_double_count_a_woken_plan(tmp_path):
    """`_cycle`'s dispatch loop re-scans every repo after its checkpoints —
    under `--dry-run` a dry-run "spawn" writes no lease, so without a dedupe
    set SHARED between every `_wake_checkpoint` and the loop, the loop would
    count the same plan a wake checkpoint already counted a second time."""
    mod, fd, log = _setup(tmp_path)
    _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod.cycle(dry_run=True)
    assert n == 1
    assert tw._spawns(log) == []


# -------------------------------------------------------- in-cycle checkpoint

def test_a_wake_written_during_a_cycle_phase_is_serviced_before_the_dispatch_loop(tmp_path):
    """plan 0181 D4: `_cycle` must check the spool after each top-level sweep,
    not only from the resident sleep tick — a cycle that runs as long as the
    647-1061s measured on opshost never leaves a sleep remainder for that tick
    to run in, against the 120s default `FLEET_WATCH_INTERVAL`.

    Writing the wake as a side effect of `lane_sweep` stands in for
    `fleet-depsd` writing it mid-phase. A spy on `_wake_checkpoint` tells
    apart a checkpoint consuming it from the unrelated, always-on dispatch
    loop reaching the same (otherwise undispatchable-by-wake) plan on its
    own: if no checkpoint ran, the spy sees nothing and the wake file is
    never touched, even though the plan still gets dispatched once by the
    loop below."""
    mod, fd, log = _setup(tmp_path)
    real_lane_sweep = mod.lane_sweep

    def _lane_sweep_then_wake(token, dry_run=False):
        _write_wake(fd, "operator/sniff", "0009-thing")
        return real_lane_sweep(token, dry_run=dry_run)
    mod.lane_sweep = _lane_sweep_then_wake

    checkpoint_counts = []
    real_checkpoint = mod._wake_checkpoint

    def _spy(dry_run=False, dry_lanes=None, dry_planned=None):
        n = real_checkpoint(dry_run=dry_run, dry_lanes=dry_lanes,
                            dry_planned=dry_planned)
        checkpoint_counts.append(n)
        return n
    mod._wake_checkpoint = _spy

    n = mod.cycle()
    assert n == 1
    assert len(tw._wait_spawns(log, 1)) == 1
    assert sum(checkpoint_counts) == 1
    assert not (fd / "wake" / _wake_name("operator/sniff", "0009-thing")).exists()


def test_a_failed_preflight_in_the_sleep_loop_leaves_the_wake_file_in_place(tmp_path):
    """The resident sleep tick's own checkpoint call (not `_service_wakes`
    directly, as test_a_failed_preflight_leaves_the_wake_file_in_place already
    covers) must not raise past a failed preflight or lose the file."""
    mod, fd, log = _setup(tmp_path)
    mod._token = lambda: (None, "token helper failed")
    p = _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._wake_checkpoint()
    assert n == 0
    assert p.exists()


# ------------------------------------------------------- _plans_memo bypass

def test_a_woken_plan_bypasses_a_stale_plans_memo(tmp_path):
    """The memo, if it is open (as it would be mid-cycle), may predate the
    change that cleared the hold — `_service_wakes` must still dispatch
    through a FRESH read, and must restore the memo it found afterwards."""
    mod, fd, log = _setup(tmp_path)
    stale = [("plans/0009-thing.md", {"id": "0009-thing", "status": "draft",
                                      "repo": "operator/sniff"})]
    mod._plans_memo = {"operator/sniff": stale}
    mod._pulls_memo = {}
    _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes()
    assert n == 1
    assert len(tw._wait_spawns(log, 1)) == 1
    # restored to exactly what this test set, for the rest of the (simulated)
    # cycle to keep using
    assert mod._plans_memo == {"operator/sniff": stale}


CROSS_REPO_DEPENDENT = """---
id: 0009-thing
status: ready
repo: operator/sniff
zone: public
tier: 2
depends_on: [plan:operator/evalrig:0008-dep]
---
# Thing
"""

CROSS_REPO_DEP_DONE = """---
id: 0008-dep
status: done
repo: operator/evalrig
zone: public
tier: 2
---
# Dep
"""


def test_a_woken_plan_bypasses_a_stale_plans_memo_of_another_repo(tmp_path):
    """The SAME bypass as the test above, but for a `plan:<other-repo>:<id>`
    edge (plan 0180 D3's cross-repo form) — `other`'s own memo entry, not the
    waiting plan's own repo's, is what must be stale-and-bypassed here.
    `operator/evalrig` must be named in `FLEET_WATCH_REPOS` alongside
    `operator/sniff` — `tests/fakes/repos_conf_shim.py` (plan 0134) builds the
    `config/repos.conf` this process reads from exactly that variable, and the
    `plan:` form's own "is this a repository in repos.conf" check reads it."""
    import test_plan_dependencies as tpd
    fd = tmp_path / "fleet"
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(fd, FLEET_WATCH_REPOS="operator/sniff,operator/evalrig",
                   FLEET_WATCH_CAP="2")
    api = tpd._api_factory_multi(plan_texts={
        "0008-dep.md": CROSS_REPO_DEP_DONE, "0009-thing.md": CROSS_REPO_DEPENDENT})
    tw._wire(mod, api, orch)
    mod._quota = lambda: None
    mod.main_health = lambda repo, token, dry_run=False: {"state": "green"}
    stale = [("plans/0008-dep.md", {"id": "0008-dep", "status": "draft",
                                    "repo": "operator/evalrig"})]
    mod._plans_memo = {"operator/evalrig": stale}
    mod._pulls_memo = {}
    _write_wake(fd, "operator/sniff", "0009-thing")
    n = mod._service_wakes()
    assert n == 1
    assert len(tw._wait_spawns(log, 1)) == 1
    # restored to exactly what this test set, for the rest of the (simulated)
    # cycle to keep using
    assert mod._plans_memo == {"operator/evalrig": stale}
