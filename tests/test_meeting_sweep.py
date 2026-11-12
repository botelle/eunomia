"""Plan 0140 — the meeting sweep starts a job for a merged implementation's
lanes. The forge, the event file and the spawn are stubbed."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_watch import _load  # noqa: E402

REPO = "operator/eunomia"
PLAN = "0138-x"
SHA = "a" * 40
LANE_SHA = "b" * 40


def _ts(age_days=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_days * 86400))


class Forge:
    """`get_pull` distinguishes the implementation PR (923) from a lane PR:
    plan 0218 made `meeting_sweep` fetch the implementation's own head with
    `get_pull` rather than reading it off `marked_pr`'s answer, which now
    comes from the issues index and carries no `head` at all."""

    def __init__(self):
        self.lane_head = LANE_SHA
        self.impl_number = 923
        self.impl_head = SHA

    def get_pull(self, repo, n):
        if str(n) == str(self.impl_number):
            return 200, {"head": {"sha": self.impl_head}}
        return 200, {"head": {"sha": self.lane_head}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    m = _load(tmp_path, FLEET_IMPLEMENTER_UIDS="42")
    forge = Forge()
    # No `head` — the issues index `marked_pr` reads since plan 0218 carries
    # none; `meeting_sweep` fetches it with `forge.get_pull` instead.
    impl = {"number": 923, "state": "closed", "merged": True}
    spawned, pages, current = [], [], {"v": False}
    monkeypatch.setattr(m, "repos", lambda: [REPO])
    monkeypatch.setattr(m, "marked_pr", lambda r, p, t: impl)
    monkeypatch.setattr(m, "_forge", lambda t: forge)
    monkeypatch.setattr(m, "notify", lambda *a, **k: pages.append(a))
    monkeypatch.setattr(m, "spawn_mode", lambda: "popen")
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda argv, **k: spawned.append(argv))
    fake = type("O", (), {"findings_current":
                          staticmethod(lambda *a: current["v"])})
    monkeypatch.setattr(m, "_orchestrator_module", lambda: fake)

    def events(*evs):
        with open(tmp_path / "events.jsonl", "w") as fh:
            for ev in evs:
                fh.write(json.dumps(ev) + "\n")

    def lane(age=0, pr=922, outcome="pr-opened"):
        return {"type": "lane-ended", "ts": _ts(age), "repo": REPO,
                "detail": {"plan": PLAN, "runner": "r", "pr": pr, "outcome": outcome}}

    events(lane())
    return type("E", (), dict(m=m, impl=impl, spawned=spawned, pages=pages,
                              current=current, forge=forge, events=events,
                              lane=staticmethod(lane), dir=tmp_path))


def test_merged_without_findings_starts_one_job(env):
    assert env.m.meeting_sweep("t") == 1
    argv = env.spawned[0]
    assert argv[1:3] == [str(env.m.BIN / "fleet-watch"), "--meeting-job"]
    assert argv[3:7] == [REPO, PLAN, "923", SHA]


def test_current_findings_start_nothing(env):
    env.current["v"] = True
    assert env.m.meeting_sweep("t") == 0 and not env.spawned


def test_moved_lane_head_starts_a_job(env):
    env.current["v"] = False
    env.forge.lane_head = "c" * 40
    assert env.m.meeting_sweep("t") == 1


def test_open_implementation_is_the_stewards(env):
    env.impl.update(merged=False, state="open")
    assert env.m.meeting_sweep("t") == 0 and not env.spawned


def test_old_lane_event_ignored(env):
    env.events(env.lane(age=15))
    assert env.m.meeting_sweep("t") == 0 and not env.spawned


def _run_job(env, monkeypatch, meet_code):
    calls = []

    def run(argv, **k):
        calls.append(Path(argv[1]).name)
        code = meet_code if argv[1].endswith("fleet-meet") else 0
        return type("R", (), dict(returncode=code, stdout="", stderr="boom"))
    monkeypatch.setattr(env.m.subprocess, "run", run)
    env.m.meeting_sweep("t")
    argv = env.spawned[-1]
    rc = env.m.meeting_job(*argv[3:8])
    return calls, rc


def test_exit_75_no_triage_and_waits_30_minutes(env, monkeypatch):
    calls, rc = _run_job(env, monkeypatch, 75)
    assert calls == ["fleet-meet"] and rc == 0 and not env.pages
    assert env.m.meeting_sweep("t") == 0           # inside the window
    key = env.m._meeting_key(REPO, PLAN)
    sf = env.m._meeting_dir() / f"{key}.state"
    st = json.loads(sf.read_text())
    st["ts"] -= 31 * 60
    sf.write_text(json.dumps(st))
    assert env.m.meeting_sweep("t") == 1


def test_exit_1_pages_once_and_is_not_retried(env, monkeypatch):
    calls, rc = _run_job(env, monkeypatch, 1)
    assert calls == ["fleet-meet"] and rc == 1 and len(env.pages) == 1
    assert env.m.meeting_sweep("t") == 0
    env.forge.lane_head = "c" * 40                  # a new pair of heads
    assert env.m.meeting_sweep("t") == 1


def test_success_runs_triage(env, monkeypatch):
    calls, rc = _run_job(env, monkeypatch, 0)
    assert calls == ["fleet-meet", "fleet-triage"] and rc == 0


def test_running_marker_blocks_and_stale_marker_is_replaced(env):
    assert env.m.meeting_sweep("t") == 1
    assert env.m.meeting_sweep("t") == 0           # marker held
    marker = env.m._meeting_dir() / f"{env.m._meeting_key(REPO, PLAN)}.running"
    old = time.time() - 3700
    os.utime(marker, (old, old))
    assert env.m.meeting_sweep("t") == 1


def test_launchd_spawn_accepts_the_real_meeting_key(env, monkeypatch):
    """The label check once used the lease-id regex, which a meeting key can
    never match, so every launchd spawn was refused."""
    started = []
    monkeypatch.setattr(env.m, "spawn_mode", lambda: "launchd")
    monkeypatch.setattr(env.m.fleetjob, "start",
                        lambda label, *a, **k: started.append(label))
    monkeypatch.setattr(env.m.fleetjob, "stop", lambda label: (0, ""))
    monkeypatch.setattr(env.m.fleetjob, "remove_record", lambda label: None)
    monkeypatch.setattr(env.m.fleetjob, "select_impl", lambda: "launchd")
    assert env.m.meeting_sweep("t") == 1
    assert len(started) == 1
    assert started[0].startswith(env.m.MEETING_LABEL_PREFIX)


def _job_argv(env):
    env.m.meeting_sweep("t")
    return env.spawned[-1]


def test_job_argv_carries_the_lane_numbers_sorted(env):
    env.events(env.lane(pr=930), env.lane(pr=922))
    argv = _job_argv(env)
    i = argv.index("--meeting-lanes")
    assert argv[i + 1] == "922,930"


def test_launchd_job_argv_carries_the_lane_numbers(env, monkeypatch):
    started = []
    monkeypatch.setattr(env.m, "spawn_mode", lambda: "launchd")
    monkeypatch.setattr(env.m.fleetjob, "stop", lambda *a, **k: None)
    monkeypatch.setattr(env.m.fleetjob, "remove_record", lambda *a, **k: None)
    monkeypatch.setattr(env.m.fleetjob, "select_impl", lambda: None)
    monkeypatch.setattr(env.m.fleetjob, "start",
                        lambda label, argv, e, impl=None: started.append(argv))
    env.events(env.lane(pr=930), env.lane(pr=922))
    assert env.m.meeting_sweep("t") == 1
    assert started[0][started[0].index("--meeting-lanes") + 1] == "922,930"


def _meet_argv(env, monkeypatch, lanes, code=0, stderr=""):
    seen = []

    def run(argv, **k):
        seen.append(list(argv))
        return type("R", (), dict(returncode=code, stdout="", stderr=stderr))
    monkeypatch.setattr(env.m.subprocess, "run", run)
    env.m.meeting_job(REPO, PLAN, "923", SHA, "pair1", lanes)
    return seen[0]


def test_meeting_job_passes_one_lane_flag_per_lane(env, monkeypatch):
    argv = _meet_argv(env, monkeypatch, [922, 930])
    assert argv[argv.index("--impl"):] == ["--impl", "923", "--lane", "922",
                                           "--lane", "930"]


def test_meeting_job_without_lanes_passes_no_lane_flag(env, monkeypatch):
    assert "--lane" not in _meet_argv(env, monkeypatch, [])


def test_a_malformed_lane_list_is_usage_exit_2_and_starts_nothing(env, monkeypatch):
    ran = []
    monkeypatch.setattr(env.m.subprocess, "run", lambda *a, **k: ran.append(a))
    for bad in ("x", "1,,x", "0", "-3"):
        with pytest.raises(SystemExit) as e:
            env.m.main(["--meeting-job", REPO, PLAN, "923", SHA, "p",
                        "--meeting-lanes", bad])
        assert e.value.code == 2
    assert not ran


def test_a_forge_75_pages_once_per_pair(env, monkeypatch):
    err = "fleet-meet: the pull-request listing did not finish (listing ended failed (status 0))"
    _meet_argv(env, monkeypatch, [922], code=75, stderr=err)
    assert len(env.pages) == 1 and "did not answer in time" in env.pages[0][1]
    _meet_argv(env, monkeypatch, [922], code=75, stderr=err)
    assert len(env.pages) == 1                      # the retry pages nothing
    env.m.meeting_job(REPO, PLAN, "923", SHA, "pair2", [922])
    assert len(env.pages) == 2                      # a new pair is a new page
