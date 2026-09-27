"""Tests for fleet-pin-advance — the mover that closes the gap
fleet-pin-watch's report exists to name (plan 0034, SPEC.md Revisions
2026-09-10).

Reuses the exact bare-origin + detached-worktree shape
`tests/test_fleet_watch.py::_pin_repo` builds, so both suites exercise the
same real git states — and the broker verdict is stubbed exactly like that
file's own broker tests, so nothing here needs a socket or the network."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

# Every env var fleet-watch (loaded underneath fleet-pin-advance) reads at
# import, plus this tool's own. Anything missing here leaks into the next
# _load and makes the suite order-dependent — the exact reason
# tests/test_fleet_watch.py keeps the same list.
_ENV = ("FLEET_WATCH_REPOS", "FLEET_WATCH_CAP", "FLEET_OPERATOR_UID",
        "FLEET_PR_PAGE_CAP", "FLEET_ORCHESTRATOR", "FLEET_NTFY_URL",
        "FLEET_IMPLEMENTER_UIDS", "FLEET_IMPLEMENTER_LOGINS",
        "FLEET_AGENT_ACCOUNTS", "FLEET_PINS", "FLEET_FORGEJO_URL",
        "FLEET_BROKER_SOCKET")


def _load(fleet_dir, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["EUNOMIA_SESSION"] = "pin-advance-test"
    os.environ.pop("EUNOMIA_LEDGER_HOST", None)
    for k in _ENV:
        os.environ.pop(k, None)
    os.environ["FLEET_SPAWN"] = "popen"           # read by fleet-watch at import
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader(
        "fleet_pin_advance", str(BIN / "fleet-pin-advance"))
    spec = importlib.util.spec_from_loader("fleet_pin_advance", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.notify_calls = []
    # plan 0058: watch.notify() now takes a paging-identity label too --
    # this tool's own LABEL constant, per its call site in _do_advance.
    mod.watch.notify = lambda title, body, label=None: (
        mod.notify_calls.append((title, body, label)), True)[1]
    return mod


def _pin_repo(tmp_path, name="origin"):
    """A bare origin plus a detached worktree pinned at its main."""
    origin = tmp_path / (name + ".git")
    work = tmp_path / (name + "-work")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(work)] + cmd, check=True)
    (work / "f").write_text("one\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "one"], check=True)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "main"], check=True)
    pin = tmp_path / (name + "-pin")
    subprocess.run(["git", "-C", str(work), "worktree", "add", "--detach",
                    str(pin), "origin/main"], check=True, capture_output=True)
    return work, pin


def _advance_origin(work, content="two\n"):
    """One more commit on `work`, pushed — makes any pin tracking it stale."""
    (work / "f").write_text(content)
    subprocess.run(["git", "-C", str(work), "commit", "-qam", "advance"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)


def _vouch(mod):
    """Broker says the pin's repo is protected — the happy path."""
    mod.watch.broker_verdict = lambda repo: (mod.watch.PROT_OK, "", None)


def _refuse_broker(mod, why="main is not branch-protected"):
    mod.watch.broker_verdict = lambda repo: (mod.watch.PROT_REFUSED, why, None)


def _unknown_broker(mod, why="forgejo-broker unreachable"):
    mod.watch.broker_verdict = lambda repo: (mod.watch.PROT_UNKNOWN, why, None)


def _head(path):
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


# --- one refusal test per pin_status state (section 3, bullet 2) -----------

def test_a_dirty_pin_is_refused_not_advanced(tmp_path):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    (pin / "f").write_text("patched in place\n")
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "dirty" in refused[0][1]
    assert _head(pin) == before, "a dirty pin must never be checked out over"


def test_an_attached_pin_is_refused_not_advanced(tmp_path):
    _, pin = _pin_repo(tmp_path)
    subprocess.run(["git", "-C", str(pin), "checkout", "-q", "-b", "wip"],
                   check=True, capture_output=True)
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "attached" in refused[0][1]
    assert _head(pin) == before


def test_a_missing_pin_is_refused(tmp_path):
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(tmp_path / "nope"))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "missing" in refused[0][1]


def test_a_non_repo_path_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(plain))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "not-a-repo" in refused[0][1]


def test_an_unfetchable_pin_is_refused(tmp_path):
    _, pin = _pin_repo(tmp_path)
    subprocess.run(["git", "-C", str(pin), "remote", "set-url", "origin",
                    str(tmp_path / "gone.git")], check=True)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "unfetchable" in refused[0][1]


def test_a_current_pin_is_silently_left_alone(tmp_path):
    """`current` is not one of section 3's refusals — it is the goal state,
    so it must produce neither an advance nor a warned refusal."""
    _, pin = _pin_repo(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == [] and refused == []


# --- fast-forward only (section 3, bullet 1) --------------------------------

def test_a_diverged_pin_is_refused_with_a_real_repository(tmp_path):
    """DoD: proven with a real diverged repository, not a mock. The pin's own
    HEAD carries a commit `main` never got, so neither ref is the other's
    ancestor and `git merge-base --is-ancestor` refuses in both directions."""
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)                                    # main moves on
    subprocess.run(["git", "-C", str(pin), "commit", "--allow-empty", "-qm",
                    "diverged"], check=True)                  # pin moves too
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "ancestor" in refused[0][1]
    assert _head(pin) == before, "a diverged pin must never be moved"


def test_a_pin_ahead_of_its_target_is_refused_not_reset(tmp_path):
    """The target being BEHIND head is the same hazard from the other side:
    a mover that could go backwards could serve chosen old code."""
    work, pin = _pin_repo(tmp_path)
    subprocess.run(["git", "-C", str(pin), "commit", "--allow-empty", "-qm",
                    "ahead"], check=True)
    before = _head(pin)
    # HEAD != origin/main now, so pin_status reports "stale" even though the
    # real hazard is direction, not distance.
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "ancestor" in refused[0][1]
    assert _head(pin) == before


def test_a_stale_pin_with_uncommitted_changes_is_refused(tmp_path):
    """pin_status's own `dirty` state cannot see this while stale — its
    porcelain check only runs once HEAD == target — so this tool must check
    it independently rather than trust `stale` alone."""
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    (pin / "f").write_text("uncommitted\n")
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "uncommitted" in refused[0][1]
    assert _head(pin) == before


# --- the broker (section 3, bullet 3) ---------------------------------------

def test_a_broker_refused_repo_is_never_advanced(tmp_path):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _refuse_broker(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "not protected" in refused[0][1]
    assert _head(pin) == before


def test_a_broker_that_cannot_tell_is_never_advance_anyway(tmp_path):
    """The degradation is a third state, not a refusal fleet-watch collapses
    into permission — 'could not determine' must never read as 'go ahead'."""
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _unknown_broker(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "could not determine" in refused[0][1]
    assert _head(pin) == before


# --- the happy path, dry-run, and the event ---------------------------------

def test_a_stale_vouched_for_pin_advances(tmp_path):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    target = subprocess.run(["git", "-C", str(work), "rev-parse", "origin/main"],
                            capture_output=True, text=True).stdout.strip()
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert refused == []
    assert [p for p, _ in advanced] == [str(pin)]
    assert _head(pin) == target


def test_dry_run_writes_nothing(tmp_path):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run(dry_run=True)
    assert refused == []
    assert [p for p, _ in advanced] == [str(pin)]
    assert _head(pin) == before, "--dry-run must write nothing"
    events = mod.lib.fleet_dir() / "events.jsonl"
    assert not events.exists() or '"advanced"' not in events.read_text()


def test_advancing_emits_a_pin_drift_event_marked_as_an_advance(tmp_path):
    """No new event type: `bin/fleet-emit`/`bin/fleet-events` enforce the
    closed set in code and neither is in this tool's paths, so an advance
    reuses `pin-drift` — the same type `check_pins` reports any other
    transition with — and marks the occurrence with `detail.action`."""
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    target = subprocess.run(["git", "-C", str(work), "rev-parse", "origin/main"],
                            capture_output=True, text=True).stdout.strip()
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    mod.run()
    events = mod.lib.fleet_dir() / "events.jsonl"
    rows = [json.loads(l) for l in events.read_text().splitlines()
           if '"advanced"' in l]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["type"] == "pin-drift"
    assert row["lease"] is None, "a pin is not a lease"
    assert row["detail"]["action"] == "advanced"
    assert row["detail"]["state"] == "current"
    assert row["detail"]["previous"] == "stale"
    assert row["detail"]["path"] == str(pin)
    assert row["detail"]["to"] == target
    assert row["detail"]["from"] and row["detail"]["from"] != target


def test_advancing_notifies_that_a_restart_is_due_and_never_restarts_anything(tmp_path):
    """Section 3, bullet 4: this tool decides to REPORT a due restart, never to
    perform one. Nothing in this module may shell out to launchctl/fleet-svc —
    asserted on the notification text, since there is no launchctl call to
    intercept in the first place."""
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    mod.run()
    assert mod.notify_calls, "a successful advance must notify"
    title, body, label = mod.notify_calls[0]
    assert label == mod.LABEL, "a page from here must name this tool, not fleet-watch"
    assert "restart" in body.lower()
    assert "does not restart" in body.lower()
    assert "launchctl" not in "".join(open(BIN / "fleet-pin-advance").readlines()[40:]), (
        "fleet-pin-advance must never itself invoke launchctl/fleet-svc — "
        "restarting a resident process is a supervised operation under a "
        "service lease, not something this scheduled unit does on its own")


def test_an_unconfigured_run_advances_nothing(tmp_path):
    """Same rule FLEET_WATCH_REPOS and FLEET_PINS itself already follow: no
    default allowlist."""
    mod = _load(tmp_path / "fleet")
    assert mod.watch.pins() == []
    rc = mod.main([])
    assert rc == 0
    events = mod.lib.fleet_dir() / "events.jsonl"
    assert not events.exists()


# --- one-instance guarantee --------------------------------------------------

def test_a_second_instance_refuses_to_run(tmp_path):
    fd = tmp_path / "fleet"
    _, pin = _pin_repo(tmp_path)
    mod = _load(fd, FLEET_PINS=str(pin))
    held = mod._single_instance()
    assert held is not None
    try:
        r = subprocess.run([sys.executable, str(BIN / "fleet-pin-advance")],
                           capture_output=True, text=True,
                           env=dict(os.environ, EUNOMIA_FLEET_DIR=str(fd),
                                    FLEET_PINS=str(pin)))
        assert "another instance is running" in r.stderr, r.stderr
    finally:
        os.close(held)


# --- refusing to guess the repo ----------------------------------------------

def test_an_unparseable_origin_refuses_rather_than_guesses(tmp_path):
    """A real, fetchable remote (needed for pin_status to ever reach `stale`
    at all) always has enough path segments to parse SOMETHING — so this
    exercises the None branch directly, the way `plan_pin` itself would see
    a remote URL with no owner/repo shape (e.g. a bare hostname alias)."""
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    mod._origin_repo = lambda path: None
    called = []
    mod.watch.broker_verdict = lambda repo: called.append(repo) or (
        mod.watch.PROT_OK, "", None)
    advanced, refused = mod.run()
    assert advanced == []
    assert called == [], "the broker must never be asked about a guessed repo"
    assert refused and "could not determine the Forgejo owner/repo" in refused[0][1]
    assert _head(pin) == before


def test_origin_repo_parses_forgejo_shaped_urls(tmp_path):
    """`_origin_repo` in isolation, across the URL shapes a real `origin`
    takes on this fleet — http(s), ssh, with or without `.git` or a trailing
    slash — and refuses (returns None) rather than guessing on anything else."""
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    mod = _load(tmp_path / "fleet")
    cases = {
        "http://forge.example:3000/operator/eunomia.git": "operator/eunomia",
        "http://forge.example:3000/operator/eunomia": "operator/eunomia",
        "http://forge.example:3000/operator/eunomia/": "operator/eunomia",
        "git@forge.example:operator/eunomia.git": "operator/eunomia",
        "ssh://git@forge.example:2222/operator/eunomia.git": "operator/eunomia",
    }
    for url, want in cases.items():
        subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"],
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url],
                       check=True)
        assert mod._origin_repo(str(repo)) == want, url
    subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], check=True)
    assert mod._origin_repo(str(repo)) is None, "no origin remote at all"
