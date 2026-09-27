"""Tests for bin/orchestrator's wrapper guarantees (plan 0004).

The implementer session itself, and everything plan 0010 added around it, is
tested in tests/test_orchestrator_session.py.

Every bullet here runs without a network, a model, or a real lease: the forge
side is `activate`/`release`/`notify` as module attributes, and git is real
because the workspace is the part being claimed."""
import importlib.machinery
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

_ORCH_ENV = ("EUNOMIA_SESSION", "EUNOMIA_PLAN_ID", "EUNOMIA_PLAN_ZONE",
             "EUNOMIA_PLAN_TIER", "FLEET_WORK_ROOT", "FLEET_GIT_SSH_BASE",
             "FLEET_NTFY_URL", "FLEET_HEARTBEAT_SECS")

PLAN = """\
---
id: 0042-a-feature
status: ready
zone: public
tier: 1
---

# A feature

## 3. Boundaries
- do not X, because Y — the reasoning is what transfers
"""


def _load(fleet_dir, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    for k in _ORCH_ENV:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("orchestrator", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(args, cwd):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _forge(tmp_path, plan_text=PLAN):
    src = tmp_path / "src"
    (src / "plans").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / "plans" / "0042-a-feature.md").write_text(plan_text)
    _git(["add", "."], src)
    _git(["commit", "-qm", "plan"], src)
    bare = tmp_path / "forge" / "operator" / "demo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return str(tmp_path / "forge")


def _wire(mod, activate_ok=True):
    calls = {"activate": [], "release": [], "notify": [], "emit": []}
    mod.activate = lambda lid: (calls["activate"].append(lid), (activate_ok, ""))[1]
    mod.release = lambda lid: (calls["release"].append(lid), True)[1]
    mod.notify = lambda t, b: (calls["notify"].append((t, b)), True)[1]
    mod.emit = lambda et, repo, detail, pr=None: calls["emit"].append((et, repo, detail))
    return calls


def _run(tmp_path, **env):
    ssh = _forge(tmp_path) if "ssh" not in env else env.pop("ssh")
    mod = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"),
                FLEET_GIT_SSH_BASE=ssh, EUNOMIA_SESSION="orch-test", **env)
    return mod


# ------------------------------------------------------------------ contract
def test_usage_and_missing_identity_are_refused(tmp_path):
    mod = _run(tmp_path)
    _wire(mod)
    assert mod.main(["operator/demo"]) == 2
    os.environ.pop("EUNOMIA_SESSION")
    assert mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"]) == 2


def test_a_lease_it_cannot_activate_is_never_released(tmp_path):
    """Releasing a lease this process does not hold would strand whoever does."""
    mod = _run(tmp_path)
    calls = _wire(mod, activate_ok=False)
    rc = mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"])
    assert rc == 2
    assert calls["release"] == []
    assert [e[0] for e in calls["emit"]] == ["plan-failed"]


# ------------------------------------------------------------------ boundary
def test_the_unbuilt_boundary_surfaces_without_rearming_the_plan(tmp_path):
    """A file at this path makes fleet-watch's spawn SUCCEED, so a wrapper that
    merely exited would leave a plan reading as in-flight and never built.

    An earlier version of this test asserted the lease WAS released — it pinned
    the defect: releasing re-arms the plan, and the watcher re-dispatches it
    every cycle. Holding the lease lets it lapse into orphanhood, which is the
    fleet's designed surface-once-never-respawn path."""
    mod = _run(tmp_path)
    calls = _wire(mod)
    rc = mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"])

    assert rc == 1
    assert [e[0] for e in calls["emit"]] == ["plan-failed"]
    assert calls["emit"][0][2]["plan"] == "0042-a-feature"
    assert calls["release"] == [], "releasing re-arms the plan for the next cycle"
    assert calls["notify"], "an unbuilt boundary that does not page is a silent stall"


def test_the_worktree_is_removed_on_a_controlled_exit(tmp_path):
    mod = _run(tmp_path)
    _wire(mod)
    mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"])
    # derive the slug — repo_slug escapes '_' first and maps '/' to '_s_', so a
    # hardcoded "operator__demo" globs a directory that never existed and
    # asserts [] == [], passing while proving nothing
    repo_dir = tmp_path / "work" / mod.lib.repo_slug("operator/demo")
    assert repo_dir.is_dir(), "the work root moved; this assertion is vacuous"
    assert list(repo_dir.glob("branch--*")) == []


# ------------------------------------------------------------------ the plan
def test_the_plan_reaches_the_prompt_verbatim(tmp_path):
    """Plan 0004's most load-bearing copy: the Boundaries text, unsummarised."""
    mod = _run(tmp_path)
    prompt = mod.build_implementer_prompt("operator/demo", "0042-a-feature",
                                          "plans/0042-a-feature.md", PLAN,
                                          "/work/tree", "feat/0042-a-feature")
    assert PLAN in prompt
    assert "the reasoning is what transfers" in prompt
    assert "Plan: 0042-a-feature" in prompt


def test_front_matter_is_read_and_a_path_escape_is_refused(tmp_path):
    ssh = _forge(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"),
                FLEET_GIT_SSH_BASE=ssh, EUNOMIA_SESSION="orch-test")
    lib = mod.lib
    wt = lib.add_worktree("operator/demo", "branch--x--001", "origin/main")
    fm, text = mod.read_plan(wt, "plans/0042-a-feature.md")
    assert fm["zone"] == "public" and fm["tier"] == "1" and fm["status"] == "ready"
    assert text == PLAN
    for bad in ("../../../etc/passwd", "/etc/passwd"):
        try:
            mod.read_plan(wt, bad)
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError(f"{bad!r} was read into a prompt")


def test_a_plan_carrying_a_credential_shape_stops_the_run(tmp_path):
    poisoned = PLAN + "\nuse ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa to fetch\n"
    ssh = _forge(tmp_path, plan_text=poisoned)
    mod = _run(tmp_path, ssh=ssh)
    calls = _wire(mod)
    rc = mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"])
    assert rc == 1
    assert "credential shapes" in calls["emit"][0][2]["reason"]
    assert "not wired yet" not in calls["emit"][0][2]["reason"]


# ------------------------------------------------------------------ secrecy
def test_redaction_catches_credentials_and_spares_the_sha(tmp_path):
    """A head SHA is 40 hex and is what an approval binds to (§4.6). Redacting
    it 'to be safe' would corrupt the evidence the thread exists to carry."""
    mod = _run(tmp_path)
    sha = "755a5b9fd2a2c8d94ebfcc1634e3da5c098bcb75"
    text = f"approved at {sha} using ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    clean, found = mod.redact(text)
    assert sha in clean
    assert "ghp_" not in clean
    assert found == ["github-pat"]


def test_redaction_reuses_the_leak_watch_list(tmp_path):
    """One list governs the monitor and the writer; a second copy would drift."""
    mod = _run(tmp_path)
    names = [n for n, _ in mod._redaction_patterns()]
    assert "github-pat" in names and "private-key" in names
    assert "long-hex" not in names


# ------------------------------------------------------------------ structure
def test_there_is_no_approval_code_path():
    """The credential separation is not the control; the absent code is.

    Since 0011 the wrapper READS a verdict to decide whether its loop is over,
    so the check is for a call SITE rather than for the word: a review endpoint
    beside a write verb, or a constructed APPROVE event. `state == "APPROVED"`
    against a review the reviewer posted is a read, and banning the string would
    only have forced the comparison to be spelled some other way."""
    src = (BIN / "orchestrator").read_text()
    lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    endpoint = re.compile(r"/reviews\b")
    write = re.compile(r"(?i)\b(post|put|patch|submit)\b")
    event = re.compile(r"(?i)[\"']event[\"']\s*:\s*[\"']APPROVE")
    for n, line in enumerate(lines, 1):
        assert not (endpoint.search(line) and write.search(line)), \
            f"orchestrator:{n} looks like an approval call: {line.strip()[:80]}"
        assert not event.search(line), \
            f"orchestrator:{n} builds an approval event: {line.strip()[:80]}"
    # TWO outbound POSTs in this FILE, and both are pages: angelia (headers +
    # JSON, the fleet's push service) and the ntfy fallback (a bare text POST).
    # 0011's writes to the FORGE — the ledger and the deferred-findings comment
    # — go through fleetforge's one write verb, which cannot reach /reviews.
    #
    # Kept as a count precisely because it is a blunt instrument: a third POST
    # appearing here is worth a human look whatever it claims to be for, and
    # this test earned its keep by failing when the angelia page was added.
    posts = re.findall(r'method="POST"', "\n".join(lines))
    assert len(posts) == 2, f"unexpected outbound POST(s): {len(posts)}"


def test_the_heartbeat_dies_with_the_run(tmp_path):
    """A surviving heartbeat keeps a dead run looking alive, and is_orphaned —
    the watcher's death detection — derives from exactly this mtime."""
    mod = _run(tmp_path)
    hb = mod.Heartbeat("orch-test", secs=1).start()
    try:
        assert hb._t.daemon is True
        assert (Path(os.environ["EUNOMIA_FLEET_DIR"]) / "sessions" / "orch-test" / "hb").exists()
    finally:
        hb.stop()


def test_readiness_tracks_the_phase_list_in_both_directions(tmp_path):
    """The upward half of the boundary. fleet-watch's r5 M2 gate checks
    os.access, which this file passes the moment it exists — so a part-built
    wrapper would re-arm the watcher and page once per plan per cycle. `--ready`
    is how the file declines to be armed.

    0032 emptied the list, so the guard is now the COUPLING rather than the
    emptiness: readiness is true exactly when nothing is unbuilt. Asserted in
    both directions, because a `readiness()` hard-wired to true would pass a
    one-directional test and arm the fleet from a half-built file."""
    mod = _run(tmp_path)
    assert mod.UNBUILT_PHASES == ()
    assert mod.main(["--ready"]) == 0
    assert mod.readiness() == (True, "")

    mod.UNBUILT_PHASES = ("a phase that is not built",)
    assert mod.main(["--ready"]) == 1
    ok, detail = mod.readiness()
    assert ok is False and "a phase that is not built" in detail

def test_ready_passes_only_when_the_list_is_empty(tmp_path):
    """Emptying UNBUILT_PHASES is what 'part 2 shipped' means — so the contract
    is exercised now, not first discovered on the day it flips."""
    mod = _run(tmp_path)
    old = mod.UNBUILT_PHASES
    mod.UNBUILT_PHASES = ()
    try:
        assert mod.main(["--ready"]) == 0
        assert mod.readiness() == (True, "")
    finally:
        mod.UNBUILT_PHASES = old


def test_it_never_releases_a_lease_without_a_marked_pr(tmp_path):
    """docs/plan-dispatch.md: never release the lease without a marked PR. The
    watcher skips a plan only while a dispatch lease is LIVE
    (bin/fleet-watch:1267), so releasing returns the plan to the undispatched
    pool — a fresh lease and a fresh page every cycle, forever. An earlier
    version released on all four exit paths while claiming it reproduced the
    watcher's own one-shot OSError behaviour."""
    assert "def release(" not in (BIN / "orchestrator").read_text()
    mod = _run(tmp_path)
    calls = _wire(mod)
    assert mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"]) == 1
    assert calls["release"] == [], "the lease was released with no marked PR"
    assert [e[0] for e in calls["emit"]] == ["plan-failed"]


def test_a_real_failure_is_not_reported_as_an_unwired_phase(tmp_path, monkeypatch):
    """A git failure paging as 'workspace setup is not wired yet' sends the
    reader to the roadmap instead of the SSH key — and the real cause cannot
    follow it, because the watcher spawns with stderr=DEVNULL."""
    mod = _run(tmp_path, ssh=str(tmp_path / "no-such-forge"))
    # A forge that does not exist fails every attempt, so this now walks the
    # whole of WORKSPACE_BACKOFF. The assertion is about the REASON reaching the
    # page, not about pacing; the pacing is tested in test_workspace_retry_*.
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = _wire(mod)
    assert mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"]) == 1
    reason = calls["emit"][0][2]["reason"]
    assert "not wired yet" not in reason, reason
    assert "workspace setup failed" in reason
    page = calls["notify"][0][1]
    assert reason in page, "the page must carry the reason"
    # and the pointer the reason needs: the watcher spawns with stderr=DEVNULL,
    # so the run log is the only place the detail survives (plan 0010)
    assert str(mod.run_log_path("branch--x--001")) in page


def test_the_unwired_boundary_helper_is_gone_with_the_last_phase():
    """`_unbuilt()` existed to make a part-built wrapper fail loudly at the exact
    phase that was missing. With UNBUILT_PHASES empty it has no caller, and a
    page reason nothing can emit is worse than no helper: docs quoted it as
    current behaviour. Removed with 0032 rather than left behind."""
    src = (BIN / "orchestrator").read_text()
    assert "def _unbuilt(" not in src
    assert "is not wired yet" not in src

def test_a_missing_lease_record_is_not_reported_as_an_unwired_phase(tmp_path):
    """Same rule as the workspace failure: a real fault that pages as "not wired
    yet" sends the reader to the roadmap instead of the ledger."""
    mod = _run(tmp_path)
    calls = _wire(mod)
    assert mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"]) == 1
    reason = calls["emit"][0][2]["reason"]
    assert "not wired yet" not in reason, reason
    assert "cannot learn the branch name" in reason


def test_a_workspace_failure_of_any_class_still_pages(tmp_path, monkeypatch):
    """add_worktree's call graph raises well beyond ValueError/RuntimeError:
    subprocess.TimeoutExpired from the clone and the worktree add, OSError from
    mkdir/open/write_text on a full or read-only work root. Anything escaping
    main() loses BOTH the plan-failed event and the page, and the watcher spawns
    with stderr=DEVNULL — so the operator would get silence. Verified: with a
    narrow handler, TimeoutExpired escaped main() entirely."""
    ssh = _forge(tmp_path)          # one forge; _run would build a new one each pass
    for exc in (subprocess.TimeoutExpired("git clone", 900),
                OSError(28, "No space left on device"),
                MemoryError()):
        mod = _run(tmp_path, ssh=ssh)
        # Every one of these faults now goes through WORKSPACE_BACKOFF before it
        # gives up. This test is about the PAGE, not the pacing, and left real
        # it charged the suite 85 seconds per fault — 255s for one test. The
        # retry itself is timed in test_workspace_retry_* below.
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        calls = _wire(mod)
        mod.lib.add_worktree = lambda *a, **k: (_ for _ in ()).throw(exc)
        rc = mod.main(["operator/demo", "plans/0042-a-feature.md", "branch--x--001"])
        assert rc == 1, f"{exc.__class__.__name__} was not handled"
        reason = calls["emit"][0][2]["reason"]
        assert exc.__class__.__name__ in reason, reason
        assert calls["notify"], f"{exc.__class__.__name__} did not page"
        assert calls["release"] == [], "still must not re-arm the plan"


# --- workspace retry --------------------------------------------------------
# 2026-09-09: five consecutive autonomous dispatches died at workspace setup,
# each before opening a PR — and _stop deliberately holds the lease, so the
# documented `respawn` recovery (a comment on the marked PR) was unreachable.
# All five had to be released by hand.

def test_workspace_retry_survives_a_blip(tmp_path, monkeypatch):
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("git fetch --quiet failed: ssh: connect to host "
                               "forge.example port 3022: Undefined error: 0")
        return "/work/tree"

    assert mod._workspace_with_retry(make) == "/work/tree"
    assert calls["n"] == 3, "should have retried until it worked"


def test_workspace_retry_gives_up_and_reraises_the_last_fault(tmp_path, monkeypatch):
    """A permanent fault must still fail, and must fail as ITSELF — the caller
    puts `e.__class__.__name__: {e}` in the page, so swallowing the original
    exception would page a reason nobody can act on."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        raise OSError("No space left on device")

    with pytest.raises(OSError, match="No space left on device"):
        mod._workspace_with_retry(make)
    assert calls["n"] == len(mod.WORKSPACE_BACKOFF) + 1


def test_workspace_retry_does_not_sleep_when_it_works_first_time(tmp_path, monkeypatch):
    """The happy path is every successful run. It must cost nothing."""
    mod = _load(tmp_path / "fleet")
    slept = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
    assert mod._workspace_with_retry(lambda: "tree") == "tree"
    assert slept == [], "a first-attempt success must not pause"


def test_workspace_retry_logs_every_attempt_with_the_reason(tmp_path, monkeypatch):
    """Until now the run log said only `workspace-failed fault=RuntimeError`;
    the reason lived solely in the emitted event, so tailing the log told an
    operator nothing about WHY."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    lines = []

    class L:
        def note(self, phase, **facts):
            lines.append((phase, facts))

    def make():
        raise RuntimeError("ssh: connect to host forge.example port 3022: "
                           "Undefined error: 0")

    with pytest.raises(RuntimeError):
        mod._workspace_with_retry(make, L())
    assert len(lines) == len(mod.WORKSPACE_BACKOFF) + 1
    assert all(p == "workspace-attempt" for p, _ in lines)
    assert [f["n"] for _, f in lines] == list(range(1, len(lines) + 1))
    assert "port 3022" in lines[0][1]["detail"], "the reason must reach the log"


def test_workspace_backoff_reaches_past_the_observed_failure_window(tmp_path):
    """The first cut of this asserted the budget stayed UNDER 120s, on the
    reasoning that the fault was a blip. The retry disproved that: on 2026-09-10
    four attempts across 85 seconds all failed identically, and the same command
    succeeded two minutes later. A budget sized for a blip lands entirely inside
    the bad window, which is the one place it cannot help.

    So the tail has to outlast the longest window actually seen. That window has
    been observed ONCE, so this is a hypothesis with a number attached, not a
    measurement — the ssh trace is what turns it into one."""
    mod = _load(tmp_path / "fleet")
    assert max(mod.WORKSPACE_BACKOFF) >= 300, "no attempt outlasts the window"
    assert sum(mod.WORKSPACE_BACKOFF) < 900, (
        "the budget must stay under one clone timeout, or a doomed workspace "
        "outlives the mechanism that is supposed to bound it")
    assert len(mod.WORKSPACE_BACKOFF) >= 2, "one retry is not a backoff"


def test_the_first_attempt_is_not_traced(tmp_path, monkeypatch):
    """Tracing the happy path would write a handshake file on every successful
    dispatch, for a fault that has never once appeared on attempt 1."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    seen = []
    mod._workspace_with_retry(
        lambda: seen.append(os.environ.get("GIT_SSH_COMMAND")) or "tree",
        None, tmp_path / "t.ssh.log")
    assert seen == [None], "attempt 1 must run clean"


def test_a_retried_attempt_traces_the_handshake(tmp_path, monkeypatch):
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    trace = tmp_path / "logs" / "branch--x--001.ssh.log"
    seen = []

    def make():
        seen.append(os.environ.get("GIT_SSH_COMMAND"))
        if len(seen) < 2:
            raise RuntimeError("ssh: connect ... Undefined error: 0")
        return "tree"

    assert mod._workspace_with_retry(make, None, trace) == "tree"
    assert seen[0] is None
    assert "-vvv" in seen[1] and str(trace) in seen[1], seen[1]


def test_the_trace_env_is_always_restored(tmp_path, monkeypatch):
    """Leaving GIT_SSH_COMMAND set would hand -vvv to every later git call in
    the run, and to the implementer session, which inherits this environment."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    trace = tmp_path / "t.ssh.log"

    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    with pytest.raises(RuntimeError):
        mod._workspace_with_retry(
            lambda: (_ for _ in ()).throw(RuntimeError("x")), None, trace)
    assert "GIT_SSH_COMMAND" not in os.environ, "leaked into a clean env"

    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /some/key")
    with pytest.raises(RuntimeError):
        mod._workspace_with_retry(
            lambda: (_ for _ in ()).throw(RuntimeError("x")), None, trace)
    assert os.environ["GIT_SSH_COMMAND"] == "ssh -i /some/key", "clobbered"


def test_an_unwritable_trace_dir_does_not_cost_the_attempt(tmp_path, monkeypatch):
    """A resource the retry cannot write must not fail a plan that would
    otherwise have worked on attempt 2."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("blip")
        return "tree"

    bad = tmp_path / "nope"
    bad.write_text("i am a file, not a directory")
    assert mod._workspace_with_retry(make, None, bad / "deep" / "t.ssh.log") == "tree"


def test_the_log_line_points_at_the_trace(tmp_path, monkeypatch):
    """A trace nobody can find is not evidence."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    trace = tmp_path / "logs" / "branch--x--001.ssh.log"
    lines = []

    class L:
        def note(self, phase, **facts):
            lines.append(facts)

    with pytest.raises(RuntimeError):
        mod._workspace_with_retry(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")), L(), trace)
    assert lines[0]["ssh_trace"] == "-", "attempt 1 has no trace to point at"
    assert all(f["ssh_trace"] == str(trace) for f in lines[1:])


def test_ssh_debug_path_sits_beside_the_run_log(tmp_path):
    mod = _load(tmp_path / "fleet")
    run = mod.run_log_path("branch--feat-x--001")
    ssh = mod.ssh_debug_path("branch--feat-x--001")
    assert ssh.parent == run.parent
    assert ssh.name == "branch--feat-x--001.ssh.log"
