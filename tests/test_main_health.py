"""Tests for plan 0144 — a red main is seen, and does not spread.

No network and no model. The forge is a stub that serves main's first-parent
line with a commit-status per commit, so the cases that matter are the shapes
that fooled the fleet on 2026-10-16 and 2026-10-22: a plan-only merge on top of
a red tests commit, and a tip whose tests were still running the first time it
was read.
"""
import json
import os
import plistlib
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_fleet_watch import _load as _load_watch
from test_orchestrator_review import (  # noqa: F401 -- reused fixtures
    FakeForge, SHA1, SHA2, _load as _load_orch, _review, _wire_forge,
)

ROOT = Path(__file__).resolve().parent.parent
REPO = "operator/x"
TESTS = "CI / tests (push)"
LINT = "Plans / lint"


# ------------------------------------------------------------ fleet-watch side
class MainForge:
    """main as a linear first-parent line, newest first."""

    def __init__(self):
        self.line = []                       # [sha, ...] newest first
        self.statuses = {}                   # sha -> [(context, state)]
        self.status_reads = 0
        self.n = 0

    def push(self, *statuses):
        self.n += 1
        sha = f"{self.n:040x}"
        self.line.insert(0, sha)
        self.statuses[sha] = list(statuses)
        return sha

    def get_branch(self, repo, name):
        if not self.line:
            return 404, None
        return 200, {"commit": {"id": self.line[0]}}

    def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
        if sha not in self.line:
            return 404, None
        rest = self.line[self.line.index(sha):][:limit]
        return 200, [{"sha": s, "parents": ([{"sha": self.line[self.line.index(s) + 1]}]
                                            if self.line.index(s) + 1 < len(self.line)
                                            else [])} for s in rest]

    def get_commit_status(self, repo, sha):
        self.status_reads += 1
        sts = self.statuses.get(sha, [])
        return 200, {"state": "", "statuses": [{"context": c, "status": v}
                                                for c, v in sts]}


@pytest.fixture
def watch(tmp_path):
    mod = _load_watch(tmp_path / "fleet")
    forge = MainForge()
    mod._forge = lambda token: forge
    pages = []
    mod.notify = lambda title, body, label=None: (pages.append((title, body)), True)[1]
    return mod, forge, pages


def _health(mod, repo=REPO):
    return mod.main_health(repo, "tok")


def test_plan_only_commits_on_a_red_tests_commit_read_red_not_green(watch):
    mod, forge, pages = watch
    forge.push((TESTS, "success"), (LINT, "success"))
    bad = forge.push((TESTS, "failure"), (LINT, "success"))
    forge.push((LINT, "success"))
    forge.push((LINT, "success"))
    rec = _health(mod)
    assert rec["state"] == "red" and rec["sha"] == bad
    assert rec["failing"] == [TESTS]
    assert rec["red_since"] == bad
    assert len(pages) == 1 and pages[0][1].startswith(f"main is red: {REPO} {bad[:8]}")


def test_red_since_is_the_oldest_commit_of_the_current_run(watch):
    mod, forge, _ = watch
    forge.push((TESTS, "success"))
    first = forge.push((TESTS, "failure"))
    forge.push((LINT, "success"))
    forge.push((TESTS, "failure"))
    assert _health(mod)["red_since"] == first


def test_a_repository_with_no_tests_context_reads_none_pages_and_holds_nothing(watch):
    mod, forge, pages = watch
    for _ in range(60):
        forge.push((LINT, "success"))
    rec = _health(mod)
    assert rec["state"] == "none"
    assert pages == []
    assert not mod.main_hold(rec)
    assert forge.status_reads == mod.MAIN_WALK        # bounded walk


@pytest.mark.parametrize("statuses,state", [
    ([(TESTS, "success")], "green"),
    ([(TESTS, "failure")], "red"),
    ([(LINT, "success")], "none"),
])
def test_an_unchanged_settled_tip_reads_nothing(watch, statuses, state):
    mod, forge, _ = watch
    forge.push(*statuses)
    assert _health(mod)["state"] == state
    forge.status_reads = 0
    assert _health(mod)["state"] == state
    assert forge.status_reads == 0


@pytest.mark.parametrize("first", ["pending", "unknown"])
def test_an_unchanged_running_tip_is_read_again_and_turns_red(watch, first):
    mod, forge, pages = watch
    forge.push((TESTS, "success"))
    sha = forge.push((TESTS, "pending"))
    if first == "unknown":
        forge.get_commit_status = lambda repo, s: (500, None)
    assert _health(mod)["state"] in ("running", "unknown")
    assert pages == [] or first == "unknown"
    if first == "unknown":
        del forge.get_commit_status
    forge.statuses[sha] = [(TESTS, "failure")]            # no further push to main
    forge.status_reads = 0
    rec = _health(mod)
    assert forge.status_reads > 0
    assert rec["state"] == "red"
    assert len(pages) == 1 and "main is red" in pages[0][1]


def test_a_fix_that_lands_reads_running_then_green_once_and_releases_the_hold(watch):
    mod, forge, pages = watch
    forge.push((TESTS, "failure"))
    assert mod.main_hold(_health(mod))
    fix = forge.push((TESTS, "pending"))
    rec = _health(mod)
    assert rec["state"] == "running" and mod.main_hold(rec)       # still held
    assert len(pages) == 1
    forge.statuses[fix] = [(TESTS, "success")]                    # no further push
    rec = _health(mod)
    assert rec["state"] == "green" and not mod.main_hold(rec)
    assert len(pages) == 2 and pages[1][1].startswith(f"main is green: {REPO}")
    _health(mod)
    assert len(pages) == 2


def test_red_pages_once_red_running_red_pages_nothing_new_green_pages_once(watch):
    mod, forge, pages = watch
    forge.push((TESTS, "success"))
    assert _health(mod)["state"] == "green" and pages == []        # first sight: quiet
    a = forge.push((TESTS, "failure"))
    _health(mod)
    _health(mod)
    assert len(pages) == 1
    b = forge.push((TESTS, "pending"))
    _health(mod)
    forge.statuses[b] = [(TESTS, "failure")]
    _health(mod)
    assert len(pages) == 1, pages                                 # red-running-red
    forge.push((TESTS, "success"))
    _health(mod)
    _health(mod)
    assert len(pages) == 2 and "green" in pages[1][1]


def test_a_page_that_did_not_deliver_is_tried_again_next_cycle(watch):
    mod, forge, pages = watch
    forge.push((TESTS, "failure"))
    mod.notify = lambda *a, **k: False
    _health(mod)
    mod.notify = lambda title, body, label=None: (pages.append(body), True)[1]
    _health(mod)
    _health(mod)
    assert len(pages) == 1


def test_unknown_for_six_hours_pages_once(watch):
    mod, forge, pages = watch
    forge.push((TESTS, "success"))
    forge.get_branch = lambda repo, name: (500, None)
    mod._write_main_health(REPO, {"repo": REPO, "tip": "t", "state": "unknown",
                                  "unknown_since": time.time() - 7 * 3600})
    assert _health(mod)["state"] == "unknown"
    assert len(pages) == 1 and "unreadable" in pages[0][0].lower()
    _health(mod)
    assert len(pages) == 1


@pytest.mark.parametrize("script,held", [
    ([(TESTS, "failure")], True),
    ([(TESTS, "failure"), (TESTS, "pending")], True),      # running after red
    ([(LINT, "success")], False),                          # none
    ([(TESTS, "success")], False),
])
def test_dispatch_is_deferred_on_red_and_running_after_red_only(watch, capsys, script,
                                                                held):
    mod, forge, _ = watch
    for st in script:
        forge.push(st)
        out = mod.main_health_sweep([REPO], "tok")
    assert (REPO in out) is held
    assert ("deferred — main red at" in capsys.readouterr().out) is held


def test_unknown_does_not_hold_dispatch(watch):
    mod, forge, _ = watch
    forge.get_branch = lambda repo, name: (500, None)
    assert mod.main_health_sweep([REPO], "tok") == {}


def test_the_deferral_is_logged_once_per_state_change(watch, capsys):
    mod, forge, _ = watch
    forge.push((TESTS, "failure"))
    for _ in range(3):
        mod.main_health_sweep([REPO], "tok")
    assert capsys.readouterr().out.count("deferred — main red at") == 1


def test_a_cycle_dispatches_nothing_onto_a_red_main(tmp_path, capsys):
    from test_fleet_watch import (_wire, _api_factory, _stub_orchestrator,
                                  _ForgeDouble)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load_watch(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                      FLEET_WATCH_CAP="1")
    base = _api_factory()
    forge = MainForge()
    forge.push((TESTS, "failure"))

    class Both(_ForgeDouble):
        def get_branch(self, repo, name):
            return forge.get_branch(repo, name)

        def get_commit_status(self, repo, sha):
            return forge.get_commit_status(repo, sha)

        def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
            if path is None and sha in forge.line:
                return forge.list_commits(repo, sha=sha, limit=limit)
            return super().list_commits(repo, sha=sha, path=path, page=page,
                                        limit=limit)

    _wire(mod, Both(base), orch)
    assert mod.cycle() == 0
    assert not log.exists() or log.read_text() == ""
    assert "deferred — main red at" in capsys.readouterr().out


def test_the_state_file_paths_agree(tmp_path):
    mod = _load_watch(tmp_path / "fleet")
    orch = _load_orch(tmp_path)
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    assert mod.main_health_path(REPO) == orch.main_health_path(REPO)


# --------------------------------------------------------------------- notify
def _angelia(mod, ok=True):
    sent = []

    def send(title, body):
        sent.append((title, body))
        return (ok, "sent=1" if ok else "no device registered for this app and user")
    mod._orchestrator_module = lambda: SimpleNamespace(send_angelia=send)
    return sent


def test_every_page_goes_angelia_first(tmp_path):
    mod = _load_watch(tmp_path / "fleet", FLEET_ANGELIA_URL="http://a",
                      FLEET_NTFY_URL="http://n")
    sent = _angelia(mod)
    recorded = []
    mod._record_page = lambda title, body, ok, detail, label: recorded.append(
        (ok, detail, label))
    mod.urlopen_calls = []
    assert mod.notify("t", "b") is True
    assert mod._notify_once("some-lease", "t2", "b2") is True
    assert mod.notify("t3", "b3", mod.LABEL_PIN_WATCH) is True
    assert [t for t, _ in sent] == ["t", "t2", "t3"]
    assert all(ok and "angelia" in d for ok, d, _ in recorded)


def test_a_page_falls_back_to_ntfy_when_angelia_is_not_configured(tmp_path, monkeypatch):
    mod = _load_watch(tmp_path / "fleet", FLEET_NTFY_URL="http://n/topic")
    sent = _angelia(mod)
    posted = []
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=0: (posted.append(req.full_url),
                                                SimpleNamespace(read=lambda: b""))[1])
    mod._record_page = lambda *a: None
    assert mod.notify("t", "b") is True
    assert sent == [] and posted == ["http://n/topic"]


def test_a_page_falls_back_to_ntfy_when_angelia_delivers_nobody(tmp_path, monkeypatch):
    mod = _load_watch(tmp_path / "fleet", FLEET_ANGELIA_URL="http://a",
                      FLEET_NTFY_URL="http://n/topic")
    _angelia(mod, ok=False)
    posted = []
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=0: (posted.append(req.full_url),
                                                SimpleNamespace(read=lambda: b""))[1])
    rows = []
    mod._record_page = lambda title, body, ok, detail, label: rows.append((ok, detail))
    assert mod.notify("t", "b") is True
    assert posted == ["http://n/topic"] and rows == [(True, "")]


def test_angelia_that_delivers_nobody_with_no_fallback_is_a_failed_page(tmp_path):
    mod = _load_watch(tmp_path / "fleet", FLEET_ANGELIA_URL="http://a")
    _angelia(mod, ok=False)
    rows = []
    mod._record_page = lambda title, body, ok, detail, label: rows.append((ok, detail))
    assert mod.notify("t", "b") is False
    assert rows[0][0] is False and "no device" in rows[0][1]


def test_the_shipped_plist_names_the_angelia_keys_and_holds_no_token():
    env = plistlib.loads(
        (ROOT / "launchd" / "org.eunomia.fleet-watch.plist").read_bytes()
    )["EnvironmentVariables"]
    for key in ("FLEET_ANGELIA_URL", "FLEET_ANGELIA_APP", "FLEET_ANGELIA_TOKEN_CMD"):
        assert key in env
    assert "FLEET_NTFY_URL" in env                     # the fallback stays
    assert "FLEET_ANGELIA_TOKEN" not in env


def test_ci_inherited_is_a_waiting_phase(tmp_path):
    mod = _load_watch(tmp_path / "fleet")
    assert "ci-inherited" in mod.STEWARD_WAITING_PHASES


# --------------------------------------------------------------- fleet-config
def test_the_two_keys_are_validated_like_the_others(tmp_path):
    mod = _load_watch(tmp_path / "fleet")
    cfg = mod.fleetconfig
    assert cfg.SETTINGS["main_tests_context_prefix"]["default"] == "CI / tests"
    assert cfg.SETTINGS["inherited_red_wait_hours"]["default"] == 12
    v = cfg.SETTINGS["inherited_red_wait_hours"]["validate"]
    assert v("1") == 1 and v(72) == 72
    for bad in ("0", "73", "x", True, ""):
        with pytest.raises(ValueError):
            v(bad)
    p = cfg.SETTINGS["main_tests_context_prefix"]["validate"]
    assert p("  CI / tests ") == "CI / tests"
    for bad in ("", "  ", "a\nb", 3):
        with pytest.raises(ValueError):
            p(bad)


# --------------------------------------------------------------- orchestrator
class CtxForge(FakeForge):
    """CI is red on SHA1 with the given contexts and green on any other sha."""

    def __init__(self, failing, **kw):
        super().__init__(**kw)
        self.failing = failing
        self.moved = False                  # a session pushed: the head is SHA2

    def get_pull(self, repo, n):
        return 200, {"head": {"sha": SHA2 if self.moved else SHA1}}

    def get_commit_status(self, repo, sha):
        if sha == SHA1:
            return 200, {"state": "failure", "statuses": [
                {"context": c, "status": "failure"} for c in self.failing]}
        return 200, {"state": "success", "statuses": [
            {"context": "CI / tests (push)", "status": "success"}]}


def _write_main(mod, state="red", failing=(TESTS,), red_since="a" * 40, age=0):
    path = mod.main_health_path(REPO)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "repo": REPO, "state": state, "failing": list(failing), "sha": "b" * 40,
        "red_since": red_since, "written_at": time.time() - age}))


def _orch_loop(tmp_path, *, failing=(TESTS,), reviews=None, moves=False,
               branched_after=True, sleep=None, impl=None):
    mod = _load_orch(tmp_path)
    forge = CtxForge(list(failing), heads=[SHA1],
                     reviews=reviews if reviews is not None
                     else {SHA2: [_review("APPROVED")]})
    _wire_forge(mod, forge)
    mod.review_bound = lambda repo, path=None: 3
    mod.dispatch_review = lambda *a, **k: (True, "")
    mod._branched_after = lambda wt, sha, oldest: branched_after
    pages, ran = [], []
    mod.notify = lambda title, body: (pages.append((title, body)), True)[1]
    if sleep is not None:
        mod._sleep = sleep

    def default_impl(wt, prompt, *a, **k):
        ran.append(prompt)
        forge.moved = moves
        return SimpleNamespace(timed_out=False)
    mod.run_implementer = impl(ran) if impl else default_impl
    mod.read_preamble = lambda path=None: "PREAMBLE"
    return mod, forge, pages, ran


def _run(mod, tmp_path):
    return mod.review_loop(REPO, "0001-a", "plans/p.md", "PLAN", 7, "br",
                           tmp_path, None, None, "lease-1")


def test_an_inherited_red_waits_then_merges_main_and_continues(tmp_path):
    mod, forge, pages, ran = _orch_loop(tmp_path, moves=True)
    _write_main(mod)
    polls = []

    def sleep(secs):
        if secs != mod.DEFAULT_STEWARD_POLL_SECS:             # await_ci's own naps
            return
        polls.append(secs)
        if len(polls) == 3:
            _write_main(mod, state="green")
    mod._sleep = sleep
    ok, why, rows = _run(mod, tmp_path)
    assert ok, why
    assert len(polls) == 3 and len(ran) == 1                  # one session only
    assert "git merge origin/main" in ran[0]
    assert "ci-inherited" in [r["verdict"] for r in rows]
    assert pages == []


def test_ci_inherited_is_written_once_and_no_fix_round_runs_while_waiting(tmp_path):
    mod, forge, pages, ran = _orch_loop(tmp_path, moves=True)
    _write_main(mod)
    notes = []
    log = SimpleNamespace(note=lambda phase, **kw: notes.append(phase))
    n = []

    def sleep(secs):
        if secs != mod.DEFAULT_STEWARD_POLL_SECS:
            return
        n.append(secs)
        assert ran == []                                     # nothing started
        if len(n) == 5:
            _write_main(mod, state="green")
    mod._sleep = sleep
    mod.review_loop(REPO, "0001-a", "plans/p.md", "PLAN", 7, "br", tmp_path,
                    None, None, "lease-1", log=log)
    assert notes.count("ci-inherited") == 1


@pytest.mark.parametrize("kw,why", [
    ({"branched_after": False}, "branched before the red run began"),
    ({"failing": (TESTS, "Other / check")}, "an extra failing context"),
])
def test_other_reds_run_the_fix_round_as_today(tmp_path, kw, why):
    mod, forge, pages, ran = _orch_loop(tmp_path, **kw)
    _write_main(mod)
    ok, reason, rows = _run(mod, tmp_path)
    assert len(ran) == 1, why
    assert "nothing was pushed" in reason


@pytest.mark.parametrize("state_kw", [
    {"age": 3600}, {"state": "unknown"}, {"state": "none"}, {"state": "running"},
    None,
])
def test_a_stale_missing_or_unsettled_state_file_fails_open(tmp_path, state_kw):
    mod, forge, pages, ran = _orch_loop(tmp_path)
    if state_kw is not None:
        _write_main(mod, **state_kw)
    ok, reason, rows = _run(mod, tmp_path)
    assert len(ran) == 1 and "nothing was pushed" in reason


def test_the_wait_passing_the_configured_hours_stops_and_pages_once(tmp_path):
    mod, forge, pages, ran = _orch_loop(tmp_path)
    _write_main(mod)
    mod.inherited_wait_hours = lambda: 1
    slept = []
    mod._sleep = lambda s: slept.append(s) if s == mod.DEFAULT_STEWARD_POLL_SECS else None
    ok, reason, rows = _run(mod, tmp_path)
    assert not ok and "inherited red outlasted the wait" in reason
    assert ran == [] and len(pages) == 1
    assert sum(slept) == 3600


def test_a_state_file_that_goes_stale_while_waiting_stops_and_pages_once(tmp_path):
    mod, forge, pages, ran = _orch_loop(tmp_path)
    _write_main(mod)

    def sleep(secs):
        if secs == mod.DEFAULT_STEWARD_POLL_SECS:
            _write_main(mod, age=3600)
    mod._sleep = sleep
    ok, reason, rows = _run(mod, tmp_path)
    assert not ok and "inherited red outlasted the wait" in reason
    assert ran == [] and len(pages) == 1


def test_an_approved_pull_request_with_an_inherited_red_is_not_pushed_to(tmp_path):
    mod, forge, pages, ran = _orch_loop(
        tmp_path, reviews={SHA1: [_review("APPROVED")]})
    _write_main(mod)
    mod._sleep = lambda s: (pytest.fail("a frozen pull request must not wait")
                            if s == mod.DEFAULT_STEWARD_POLL_SECS else None)
    ok, reason, rows = _run(mod, tmp_path)
    assert not ok
    assert "inherited red on a frozen pull request: re-run its CI" in reason
    assert ran == [] and len(pages) == 1


def test_a_merge_that_conflicts_stops_naming_the_files(tmp_path):
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True,
                                    capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "br")
    (tmp_path / "f.txt").write_text("br\n")
    git("commit", "-qam", "br")
    git("checkout", "-q", "main")
    (tmp_path / "f.txt").write_text("main\n")
    git("commit", "-qam", "main")
    git("checkout", "-q", "br")

    def impl(ran):
        def run(wt, prompt, *a, **k):
            ran.append(prompt)
            subprocess.run(["git", "merge", "main"], cwd=wt, capture_output=True)
            return SimpleNamespace(timed_out=False)
        return run
    mod, forge, pages, ran = _orch_loop(tmp_path, impl=impl)
    _write_main(mod)
    mod._sleep = lambda s: (_write_main(mod, state="green")
                            if s == mod.DEFAULT_STEWARD_POLL_SECS else None)
    ok, reason, rows = _run(mod, tmp_path)
    assert not ok and "conflicts in: f.txt" in reason


def test_branched_after_is_decided_by_the_merge_base(tmp_path):
    def git(*a, cwd):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    git("config", "user.email", "t@t", cwd=origin)
    git("config", "user.name", "t", cwd=origin)
    shas = []
    for i in range(3):
        (origin / "f").write_text(str(i))
        git("add", ".", cwd=origin)
        git("commit", "-qm", f"c{i}", cwd=origin)
        shas.append(git("rev-parse", "HEAD", cwd=origin))
    clone = tmp_path / "clone"
    git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    mod = _load_orch(tmp_path)
    red_since = shas[1]
    before = git("rev-parse", shas[0], cwd=clone)
    assert mod._branched_after(clone, before, red_since) is False
    assert mod._branched_after(clone, shas[1], red_since) is True
    assert mod._branched_after(clone, shas[2], red_since) is True
    assert mod._branched_after(clone, shas[2], "f" * 40) is False   # unknown: no
