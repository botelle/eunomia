"""Plan 0104: the watcher starts one test lane per tester row beside the
implementation lane, and the cap counts plans.

Every property here is asserted against the real fleet-claim and the real
ledger; only the forge, the supervisor and the vendor are doubles.
"""
import base64
import json
import re
import stat
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fleet_watch import (BIN, PLAN, _ForgeDouble, _api_factory, _events,  # noqa: E402
                              _load, _stub_orchestrator, _wait_spawns, _wire)

ROOT = BIN.parent
REPO = "operator/sniff"
MAIN_SHA = "a" * 40
PLAN_ID = "0009-thing"


class LaneForge(_ForgeDouble):
    """The watcher's forge double plus the one read plan 0104 adds: main's tip."""

    def __init__(self, dispatch, tip=MAIN_SHA):
        super().__init__(dispatch)
        self.tip = tip
        self.reads = []

    def get_branch(self, repo, name):
        self.reads.append(("branch", name))
        return 200, {"commit": {"id": self.tip}}


def _plan(zone="public", status="ready", pid=PLAN_ID):
    zone_line = "" if zone is None else f"zone: {zone}\n"
    return (f"---\nid: {pid}\nstatus: {status}\nrepo: {REPO}\n{zone_line}"
            "tier: 2\n---\n# Thing\n")


def _setup(tmp_path, monkeypatch, plan_text=None, testers=((1, "sub-opus"), (2, "sub-codex")),
           cap="3", plans=("0009-thing.md",), api=None, tip=MAIN_SHA):
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS=REPO, FLEET_WATCH_CAP=cap, FLEET_SPAWN="launchd")
    orch, log = _stub_orchestrator(tmp_path)
    lane = tmp_path / "lane-stub.sh"
    lane.write_text("#!/bin/sh\nexit 0\n")
    lane.chmod(lane.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FLEET_LANE", str(lane))
    forge = LaneForge(api or _api_factory(plans=plans, plan_text=plan_text or _plan()), tip=tip)
    _wire(mod, forge, orch)
    jobs = []
    mod.fleetjob.select_impl = lambda: "launchd"
    mod.fleetjob.start = lambda label, argv, env, **kw: jobs.append((label, list(argv)))
    mod._lane_module = lambda: types.SimpleNamespace(
        resolve_testers=lambda repo: [(o, r) for o, r in testers])
    notes = []
    mod.notify = lambda t, b: notes.append((t, b))
    return mod, fd, jobs, notes, forge


def _leases(mod, prefix):
    return [r for r in mod.lib.all_leases()
            if (r.get("note") or "").startswith(prefix) and r.get("state") != "released"]


# --------------------------------------------------------------- pair dispatch
def test_a_ready_plan_starts_one_implementation_lane_and_one_lane_per_tester(tmp_path, monkeypatch):
    mod, fd, jobs, _notes, _forge = _setup(tmp_path, monkeypatch)
    assert mod.cycle() == 1
    assert len(_leases(mod, "plan:")) == 1
    lanes = _leases(mod, "lane:")
    assert sorted(r["note"] for r in lanes) == [f"lane:{PLAN_ID}/sub-codex",
                                                f"lane:{PLAN_ID}/sub-opus"]
    for r in lanes:
        res = mod.lib.resource_of(r)
        assert res["branch"] == f"agent/tests/{PLAN_ID}/{r['note'].rsplit('/', 1)[-1]}"
        assert r["holder"].startswith("lane-0009-thing-sub-")
    labels = sorted(l for l, _ in jobs)
    assert [l for l in labels if ".fleet-orch." in l] and len(labels) == 3
    assert sum(".fleet-lane." in l for l in labels) == 2
    assert len(_events(fd, "plan-dispatched")) == 1
    ld = _events(fd, "lane-dispatched")
    assert len(ld) == 2
    shas = set()
    for label, argv in jobs:
        if ".fleet-lane." in label:
            assert argv[argv.index("--sha") + 1] == MAIN_SHA
            shas.add(argv[argv.index("--sha") + 1])
            assert "--lease" in argv and "--runner" in argv
    assert shas == {MAIN_SHA}
    assert {e["detail"]["sha"] for e in ld} == {MAIN_SHA}
    assert all(e["lease"] == e["detail"]["lease"] and e["repo"] == REPO for e in ld)


def test_a_lane_never_rides_plan_dispatched(tmp_path, monkeypatch):
    mod, fd, _jobs, _n, _f = _setup(tmp_path, monkeypatch)
    mod.cycle()
    assert all("runner" not in e["detail"] for e in _events(fd, "plan-dispatched"))


@pytest.mark.parametrize("zone", [None, "Private", "private", "[public", "Public"])
def test_a_zone_that_is_not_the_literal_public_skips_the_lanes_not_the_plan(
        tmp_path, monkeypatch, zone):
    mod, fd, jobs, _n, _f = _setup(tmp_path, monkeypatch, plan_text=_plan(zone=zone))
    mod.cycle()
    assert len(_leases(mod, "plan:")) == 1, "the implementation lane starts anyway"
    assert _leases(mod, "lane:") == []
    skipped = _events(fd, "lane-skipped")
    assert [e["detail"]["reason"] for e in skipped] == ["zone"]
    assert skipped[0]["detail"]["runner"] is None
    assert not _events(fd, "lane-dispatched")
    assert not any(".fleet-lane." in l for l, _ in jobs)


def test_no_resolved_tester_skips_with_no_tester(tmp_path, monkeypatch):
    mod, fd, _j, _n, _f = _setup(tmp_path, monkeypatch, testers=())
    mod.cycle()
    assert [e["detail"]["reason"] for e in _events(fd, "lane-skipped")] == ["no-tester"]
    assert len(_leases(mod, "plan:")) == 1


def test_an_unexecutable_fleet_lane_skips_as_unavailable(tmp_path, monkeypatch):
    mod, fd, _j, _n, _f = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FLEET_LANE", str(tmp_path / "absent"))
    mod.cycle()
    assert [e["detail"]["reason"] for e in _events(fd, "lane-skipped")] == ["unavailable"]
    assert len(_leases(mod, "plan:")) == 1


def test_the_implementation_lane_is_spawned_first_and_its_failure_starts_no_lane(
        tmp_path, monkeypatch):
    mod, fd, jobs, _n, _f = _setup(tmp_path, monkeypatch)

    def start(label, argv, env, **kw):
        if ".fleet-orch." in label:
            raise OSError("simulated")
        jobs.append((label, argv))
    mod.fleetjob.start = start
    mod.cycle()
    assert [r for r in mod.lib.all_leases() if r.get("state") != "released"] == []
    assert not _events(fd, "lane-dispatched")
    assert not jobs


def test_a_lane_that_cannot_spawn_releases_its_lease_pages_once_and_spares_the_plan(
        tmp_path, monkeypatch):
    mod, fd, jobs, notes, _f = _setup(tmp_path, monkeypatch, testers=((1, "sub-opus"),))

    def start(label, argv, env, **kw):
        if ".fleet-lane." in label:
            raise OSError("simulated")
        jobs.append((label, argv))
    mod.fleetjob.start = start
    mod.cycle()
    assert len(_leases(mod, "plan:")) == 1 and _leases(mod, "lane:") == []
    ev = _events(fd, "lane-skipped")
    assert [e["detail"]["reason"] for e in ev] == ["spawn-failed"]
    assert ev[0]["detail"]["runner"] == "sub-opus"
    assert [t for t, _ in notes].count("Test lane dispatch failed") == 1


def test_the_plan_is_skipped_when_main_moved_and_it_is_no_longer_ready(tmp_path, monkeypatch):
    base = _api_factory(plan_text=_plan())

    def api(method, path, body=None, token=None):
        if "/contents/plans/" in path and f"ref={MAIN_SHA}" in path:
            return 200, {"content": base64.b64encode(_plan(status="done").encode()).decode()}
        return base(method, path, body, token)
    mod, fd, jobs, _n, _f = _setup(tmp_path, monkeypatch, api=api)
    assert mod.cycle() == 0
    assert [r for r in mod.lib.all_leases() if r.get("state") != "released"] == []
    assert not jobs


def test_dry_run_prints_the_lanes_and_writes_nothing(tmp_path, monkeypatch, capsys):
    mod, fd, jobs, _n, _f = _setup(tmp_path, monkeypatch)
    mod.cycle(dry_run=True)
    out = capsys.readouterr().out
    assert out.count("DRY-RUN would start a test lane") == 2
    assert mod.lib.all_leases() == []
    assert not jobs
    assert not _events(fd, "lane-dispatched") and not _events(fd, "lane-skipped")


# ------------------------------------------------------------- the cap, by plan
def _two_plans_api():
    texts = {"0009-thing.md": _plan(pid="0009-thing"), "0010-other.md": _plan(pid="0010-other")}
    base = _api_factory(plans=tuple(texts))

    def api(method, path, body=None, token=None):
        m = re.search(r"/contents/plans/([^?]+)", path)
        if m and m.group(1) in texts:
            return 200, {"content": base64.b64encode(texts[m.group(1)].encode()).decode()}
        return base(method, path, body, token)
    return api


def test_one_plan_with_two_lanes_counts_one_and_blocks_a_cap_of_one(tmp_path, monkeypatch):
    mod, fd, _j, _n, _f = _setup(tmp_path, monkeypatch, cap="1", plans=("0009-thing.md",),
                                 api=_two_plans_api())
    mod.cycle()
    assert len(_leases(mod, "plan:")) == 1 and len(_leases(mod, "lane:")) == 2
    assert mod.count_live(mod.all_dispatch_leases()) == (1, 0), \
        "one implementation lease and two lane leases are ONE plan"
    # the second ready plan waits at cap 1 ...
    assert mod.cycle() == 0
    assert len(_leases(mod, "plan:")) == 1
    # ... and goes at cap 2
    mod.CAP = 2
    assert mod.cycle() == 1
    assert len(_leases(mod, "plan:")) == 2


def test_a_waiting_steward_with_one_live_lane_still_counts_one(tmp_path, monkeypatch):
    mod, fd, _j, _n, _f = _setup(tmp_path, monkeypatch, testers=((1, "sub-opus"),))
    mod.cycle()
    mod.is_waiting_on_a_human = lambda rec, orch=None: True
    assert mod.count_live(mod.all_dispatch_leases()) == (1, 0)
    # without the lane the same steward is excluded (plan 0067 is unchanged)
    assert mod.count_live(mod.dispatch_leases()) == (0, 1)


def _age(mod, fd, rec):
    p = fd / "leases" / f"{rec['id']}.json"
    body = json.loads(p.read_text())
    body["created"] = "2020-01-01T00:00:00Z"
    p.write_text(json.dumps(body))


def test_a_plan_whose_only_lease_is_an_orphaned_lane_counts_zero(tmp_path, monkeypatch):
    mod, fd, _j, _n, _f = _setup(tmp_path, monkeypatch, testers=((1, "sub-opus"),))
    mod.cycle()
    impl = _leases(mod, "plan:")[0]
    (fd / "leases" / f"{impl['id']}.json").unlink()
    _age(mod, fd, _leases(mod, "lane:")[0])
    assert mod.count_live(mod.all_dispatch_leases()) == (0, 0)


def test_a_plan_with_a_live_lane_and_no_implementation_lease_is_not_redispatched(
        tmp_path, monkeypatch):
    mod, fd, jobs, _n, _f = _setup(tmp_path, monkeypatch, testers=((1, "sub-opus"),))
    mod.cycle()
    (fd / "leases" / f"{_leases(mod, 'plan:')[0]['id']}.json").unlink()
    before = len(jobs)
    assert mod.cycle() == 0
    assert len(jobs) == before and len(_leases(mod, "lane:")) == 1


def test_all_dispatch_leases_returns_both_kinds_for_one_plan_without_either_hiding_the_other(
        tmp_path, monkeypatch):
    mod, fd, _j, _n, _f = _setup(tmp_path, monkeypatch)
    mod.cycle()
    leases = mod.all_dispatch_leases()
    assert (REPO, PLAN_ID) in leases
    assert {(REPO, PLAN_ID, "sub-opus"), (REPO, PLAN_ID, "sub-codex")} <= set(leases)
    assert len(mod.impl_leases(leases)) == 1 and len(mod.lane_leases(leases)) == 2

    # The overwrite this keying exists to prevent, reproduced with the OLD keying
    # (everything keyed (repo, plan) from the note): the lane lease replaces the
    # implementation lease in the dict, and this assertion FAILS on that code.
    old = {}
    for rec in mod.lib.all_leases():
        note = rec.get("note") or ""
        plan = note.split(":", 1)[1].split("/", 1)[0] if ":" in note else note
        old[(REPO, plan)] = rec
    assert len(old) == 1, "old keying collapses three leases into one slot"
    assert len(leases) == 3


# --------------------------------------------------------- a dead lane, surfaced once
def test_a_dead_lane_pages_once_and_ends_dead_once_across_three_cycles(tmp_path, monkeypatch):
    mod, fd, _j, notes, _f = _setup(tmp_path, monkeypatch, testers=((1, "sub-opus"),))
    mod.cycle()
    lane = _leases(mod, "lane:")[0]
    _age(mod, fd, lane)
    before = {r["id"] for r in mod.lib.all_leases()}
    notes.clear()
    for _ in range(3):
        mod.cycle()
    assert [t for t, _ in notes].count("Test lane dead") == 1
    body = [b for t, b in notes if t == "Test lane dead"][0]
    assert PLAN_ID in body and "sub-opus" in body and MAIN_SHA in body
    dead = [e for e in _events(fd, "lane-ended") if e["detail"]["outcome"] == "dead"]
    assert len(dead) == 1 and dead[0]["detail"]["lease"] == lane["id"]
    assert {r["id"] for r in mod.lib.all_leases()} == before, "no release, no new lease"
    assert [r for r in mod.lib.all_leases() if r["id"] == lane["id"]][0]["state"] != "released"


# ----------------------------------------------------------------------- reaping
def test_reap_jobs_removes_a_finished_lane_job_this_fleet_minted_and_leaves_others(
        tmp_path):
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS=REPO)
    mod.fleetjob.JOB_DIR = tmp_path / "jobs"
    calls = []
    mod.fleetjob._run_launchctl = lambda *a, **k: (calls.append(a), (0, ""))[1]
    mine = "org.eunomia.fleet-lane.branch--agent-tests-x--001"
    foreign = "org.eunomia.fleet-lane.branch--agent-tests-y--001"
    mod.fleetjob.start(mine, ["/bin/echo"], mod.job_env({}), impl="launchd")
    other = dict(mod.job_env({}), EUNOMIA_FLEET_DIR=str(tmp_path / "another-fleet"))
    mod.fleetjob.start(foreign, ["/bin/echo"], other, impl="launchd")
    assert mod.reap_jobs() == 1
    left = sorted(p.name for p in (tmp_path / "jobs").iterdir())
    assert left == [foreign + ".plist"]


# --------------------------------------------------- neither prompt names the other
def _module(name, path):
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


def test_neither_prompt_names_the_other_lane():
    om = _module("orch_ind", BIN / "orchestrator")
    prompt = om.build_implementer_prompt(REPO, PLAN_ID, f"plans/{PLAN_ID}.md", _plan(),
                                         Path("/tmp"), f"feat/{PLAN_ID}")
    for needle in ("agent/tests/", "Tests-for", "lane:", "sub-opus", "sub-codex",
                   "fleet-lane", "lane-"):
        assert needle not in prompt, needle
    lm = _module("lane_ind", BIN / "fleet-lane")
    lp = lm.build_prompt(_plan(), Path("/nonexistent"), "{}")
    for needle in ("feat/", "orch-0009", "plan:0009", "fleet-orch"):
        assert needle not in lp, needle


# ------------------------------------------- no review, no CI, no status on a lane PR
def test_no_code_path_reviews_or_posts_a_status_for_an_agent_tests_head():
    for name in ("orchestrator", "fleet-watch"):
        for i, line in enumerate((BIN / name).read_text().splitlines(), 1):
            if "agent/tests" in line and not line.lstrip().startswith("#"):
                assert not re.search(r"dispatch-review|post_status|create_commit_status",
                                     line), f"{name}:{i}"
    for wf in ("ci.yml", "plans.yml"):
        text = (ROOT / ".forgejo" / "workflows" / wf).read_text()
        assert "- 'agent/tests/**'" in text, wf
    assert "post_status" not in (BIN / "fleet-watch").read_text().split("def spawn_lane")[1].split("def _first_time")[0]


# ------------------------------------------------ the #863 review findings
def test_dispatch_leases_keeps_its_two_element_keys_while_lanes_are_live(tmp_path, monkeypatch):
    """Review 4569 HIGH: bin/fleet-rebase, bin/mopsus and bin/fleet-requeue
    unpack or look up (repo, plan) keys. Returning lane leases from
    dispatch_leases() crashed fleet-rebase while any lane was live."""
    mod, fd, _j, _n, _f = _setup(tmp_path, monkeypatch)
    mod.cycle()
    assert len(_leases(mod, "lane:")) == 2
    keys = list(mod.dispatch_leases())
    assert keys == [(REPO, PLAN_ID)]
    for (rp, plan), rec in mod.dispatch_leases().items():   # fleet-rebase:228's unpacking
        assert rp == REPO and plan == PLAN_ID


def test_every_caller_outside_fleet_watch_unpacks_two_element_keys():
    """The class, not the instance: no program but fleet-watch may need to know
    lane leases exist in this map."""
    import re
    root = Path(__file__).resolve().parent.parent / "bin"
    for f in sorted(root.iterdir()):
        if f.name == "fleet-watch" or not f.is_file():
            continue
        text = f.read_text(errors="replace")
        assert "all_dispatch_leases" not in text, f"{f.name} reaches lane leases"
        for m in re.finditer(r"for \(([^)]*)\)[^\n]*dispatch_leases\(\)", text):
            assert m.group(1).count(",") == 1, f"{f.name}: {m.group(0)}"


def test_an_orphaned_lane_does_not_hold_its_plan_out_of_dispatch(tmp_path, monkeypatch):
    """Review 4569/4567 MEDIUM: D6 never releases a dead lane, so counting it
    in plan_in_flight blocked a requeued plan forever with nothing saying so."""
    mod, fd, jobs, _n, _f = _setup(tmp_path, monkeypatch, testers=((1, "sub-opus"),))
    mod.cycle()
    lane = _leases(mod, "lane:")[0]
    (fd / "leases" / f"{_leases(mod, 'plan:')[0]['id']}.json").unlink()
    _age(mod, fd, lane)
    assert not mod.plan_in_flight(mod.all_dispatch_leases(), REPO, PLAN_ID)


def test_a_redispatched_plan_does_not_start_its_lanes_again(tmp_path, monkeypatch):
    """Review 4567 MEDIUM: a mechanical requeue goes through the normal
    dispatch path, and starting the lanes again spends the subscription twice
    and fails to land on the branch the first run pushed."""
    mod, fd, jobs, _n, _f = _setup(tmp_path, monkeypatch, testers=((1, "sub-opus"),))
    mod.cycle()
    assert len(_leases(mod, "lane:")) == 1
    assert mod.lane_ever_dispatched(REPO, PLAN_ID, "sub-opus")
    assert not mod.lane_ever_dispatched(REPO, PLAN_ID, "sub-codex")
    assert not mod.lane_ever_dispatched("operator/other", PLAN_ID, "sub-opus")
    before = len(jobs)
    mod.dispatch_lanes(REPO, PLAN_ID, ("ok", "a" * 40, {"zone": "public"}))
    assert len(jobs) == before, "a lane was started a second time"
    assert len(_leases(mod, "lane:")) == 1


def test_lane_report_lines_do_not_make_a_waiting_steward_count(tmp_path):
    """Review 4567 MEDIUM: an approved steward that rewrote the lane lines or
    ran a meeting read as spending until the PR merged."""
    import importlib.machinery, importlib.util
    path = Path(__file__).resolve().parent.parent / "bin" / "fleet-watch"
    loader = importlib.machinery.SourceFileLoader("fw_phase", str(path))
    spec = importlib.util.spec_from_loader("fw_phase", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    log = tmp_path / "run.log"
    log.write_text("t1 steward-start pr=7\nt2 test-lanes pr=7 lanes=1\n"
                   "t3 meeting-done pr=7 head=abc\n")
    assert mod._last_phase(log) == "steward-start"
    log.write_text("t1 review-dispatch pr=7\nt2 test-lanes pr=7 lanes=1\n")
    assert mod._last_phase(log) == "review-dispatch"
    log.write_text("t2 test-lanes pr=7 lanes=1\n")
    assert mod._last_phase(log) is None
