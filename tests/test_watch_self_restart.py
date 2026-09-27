"""Tests for the resident watcher's self-restart (plan 0040).

A resident `fleet-watch` holds the code it imported at start, so
`fleet-pin-advance` fast-forwarding the pin it runs from changes nothing until
the watcher restarts -- and only the watcher itself is entitled to do that, per
plan 0029's service lease. These exercise `resident()`'s decision to end its
own loop, using a REAL temporary git repository with an `origin`, the same
shape fleet-watch's own pin (`~/.local/share/pins/eunomia`) is deployed as: a
bare origin, an attached clone used to push, and a detached worktree pinned at
`origin/main` that `mod.BIN` is pointed at.

A dedicated file, out of the lease `tests/test_fleet_watch.py` holds for draft
plans -- so its `_load`/`_pin_repo` helpers are duplicated here rather than
imported, small as they are.
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

_WATCH_ENV = ("FLEET_WATCH_REPOS", "FLEET_WATCH_CAP", "FLEET_OPERATOR_UID",
              "FLEET_PR_PAGE_CAP", "FLEET_ORCHESTRATOR", "FLEET_NTFY_URL",
              "FLEET_IMPLEMENTER_UIDS", "FLEET_IMPLEMENTER_LOGINS",
              "FLEET_AGENT_ACCOUNTS", "FLEET_PINS", "FLEET_FORGEJO_URL")


def _load(fleet_dir, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["EUNOMIA_SESSION"] = "watch-test"
    os.environ.pop("EUNOMIA_LEDGER_HOST", None)
    for k in _WATCH_ENV:
        os.environ.pop(k, None)
    os.environ["FLEET_SPAWN"] = "popen"          # see test_fleet_watch.py's _load
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("fleet_watch", str(BIN / "fleet-watch"))
    spec = importlib.util.spec_from_loader("fleet_watch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.JOB_DIR = pathlib.Path(fleet_dir) / "jobs"
    return mod


def _repo(tmp_path, name="eunomia"):
    """A bare origin, an attached clone to push from, and a detached worktree
    pinned at its main -- the shape fleet-watch's own deployment pin is. `BIN`
    is pointed at the pin, so `_watcher_worktree()`'s git calls resolve here."""
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


def _advance(work, pin):
    """Push a new commit to origin and fast-forward the pin onto it -- the
    same net effect `fleet-pin-advance` has, without invoking it."""
    (work / "f").write_text(f"{time.time()}\n")
    subprocess.run(["git", "-C", str(work), "commit", "-qam", "advance"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)
    subprocess.run(["git", "-C", str(pin), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(pin), "checkout", "-q", "--detach",
                    "origin/main"], check=True, capture_output=True)


def _quiet_resident(mod, monkeypatch, lease_id="service--x--001"):
    """Wire the parts of resident() that are not this plan's concern: the
    service lease (plan 0029, exercised elsewhere), the FLEET_PINS drift
    report, the cadence, and heartbeats."""
    retired = []
    monkeypatch.setattr(mod, "acquire_service_lease", lambda sid: lease_id)
    monkeypatch.setattr(mod, "_retire", lambda lid: retired.append(lid) or True)
    monkeypatch.setattr(mod, "check_pins", lambda dry_run=False: [])
    monkeypatch.setattr(mod, "watch_interval", lambda env=None: 0)
    monkeypatch.setattr(mod.lib, "touch_heartbeat", lambda sid: None)
    return retired


# --------------------------------------------------------------- the cases

def test_head_unchanged_never_calls_pin_status(tmp_path, monkeypatch):
    """The cheap, local `git rev-parse HEAD` comparison must gate the
    network-touching part of the check: pin_status fetches, and a watcher that
    never advances must not pay for that every cycle."""
    _work, pin = _repo(tmp_path)
    mod = _load(tmp_path / "fleet")
    mod.BIN = pin
    _quiet_resident(mod, monkeypatch)
    seen = []
    real_pin_status = mod.pin_status

    def spy(*a, **k):
        seen.append(a)
        return real_pin_status(*a, **k)

    monkeypatch.setattr(mod, "pin_status", spy)
    calls = {"n": 0}

    def fake_cycle(dry_run=False):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(mod, "cycle", fake_cycle)
    with pytest.raises(KeyboardInterrupt):
        mod.resident(dry_run=False)
    assert calls["n"] >= 2, "the loop did not continue"
    assert seen == [], "pin_status was called though HEAD never moved"


def test_head_advanced_and_current_restarts(tmp_path, monkeypatch):
    """The whole point: a merge that lands while the watcher is resident ends
    its loop between cycles, and the lease is released for KeepAlive to pick
    back up."""
    work, pin = _repo(tmp_path)
    mod = _load(tmp_path / "fleet")
    mod.BIN = pin
    retired = _quiet_resident(mod, monkeypatch)

    def fake_cycle(dry_run=False):
        _advance(work, pin)          # simulate fleet-pin-advance, mid-cycle
        return 0

    monkeypatch.setattr(mod, "cycle", fake_cycle)
    rc = mod.resident(dry_run=False)
    assert rc == 0
    assert retired == ["service--x--001"], "restart did not release the lease"


def test_head_advanced_but_dirty_declines_and_names_it(tmp_path, monkeypatch):
    """dirty, attached, not-a-repo, missing and unfetchable are all not a
    reviewed trunk commit and must never be restarted onto -- and the decline
    must be visible in the log, not silent."""
    work, pin = _repo(tmp_path)
    mod = _load(tmp_path / "fleet")
    mod.BIN = pin
    _quiet_resident(mod, monkeypatch)
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    calls = {"n": 0}

    def fake_cycle(dry_run=False):
        calls["n"] += 1
        if calls["n"] == 1:
            _advance(work, pin)
            (pin / "f").write_text("uncommitted\n")
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(mod, "cycle", fake_cycle)
    with pytest.raises(KeyboardInterrupt):
        mod.resident(dry_run=False)
    assert calls["n"] >= 2, "a declined restart must not end the loop"
    assert any("dirty" in w for w in warned), warned


def test_running_child_declines_then_restarts_once_it_exits(tmp_path, monkeypatch):
    """FLEET_SPAWN=popen: orchestrators are this process's children, and
    exiting would orphan them. 'Still running' is asked of the OS -- this
    process's own children by ppid -- never an in-memory list, so a real
    subprocess is spawned here rather than a fake bookkeeping entry."""
    work, pin = _repo(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_SPAWN="popen")
    mod.BIN = pin
    retired = _quiet_resident(mod, monkeypatch)
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    calls = {"n": 0}

    def fake_cycle(dry_run=False):
        calls["n"] += 1
        if calls["n"] == 1:
            _advance(work, pin)
        if calls["n"] == 2:
            child.terminate()
            child.wait(timeout=5)
        return 0

    monkeypatch.setattr(mod, "cycle", fake_cycle)
    try:
        rc = mod.resident(dry_run=False)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    assert rc == 0
    assert retired == ["service--x--001"]
    assert any("running" in w for w in warned), warned
    assert calls["n"] == 3, ("expected: decline while running, a skipped "
                             f"backoff cycle, then restart -- got {calls}")


def test_dry_run_never_restarts(tmp_path, monkeypatch, capsys):
    """The person running --dry-run is debugging; restarting mid-session would
    change the thing being observed."""
    work, pin = _repo(tmp_path)
    mod = _load(tmp_path / "fleet")
    mod.BIN = pin
    monkeypatch.setattr(mod, "check_pins", lambda dry_run=False: [])
    monkeypatch.setattr(mod, "watch_interval", lambda env=None: 0)
    calls = {"n": 0}

    def fake_cycle(dry_run=False):
        calls["n"] += 1
        if calls["n"] == 1:
            _advance(work, pin)
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(mod, "cycle", fake_cycle)
    with pytest.raises(KeyboardInterrupt):
        mod.resident(dry_run=True)
    out = capsys.readouterr().out
    assert "would" in out.lower() and "restart" in out.lower(), out
    assert calls["n"] >= 2, "--dry-run must keep cycling"


def test_the_unit_is_resident_with_keepalive_and_no_interval():
    """The template a fresh install copies: without KeepAlive the self-restart
    above would end the process and nothing would ever bring it back."""
    plist = BIN.parent / "launchd" / "org.eunomia.fleet-watch.plist"
    d = plistlib.load(plist.open("rb"))
    assert "--resident" in d["ProgramArguments"], d["ProgramArguments"]
    assert d.get("KeepAlive") is True
    assert "StartInterval" not in d
