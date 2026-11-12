"""Plan 0134: enrolling a repository is one reviewed line in config/repos.conf.
The units read it from the checkout they run from; FLEET_WATCH_REPOS is gone.

Every test assigns `mod.REPOS_CONF` itself after loading, which is what keeps
the suite-wide FLEET_WATCH_REPOS stand-in (tests/fakes/repos_conf_shim.py) out
of the way: it only acts while REPOS_CONF is still the file it made."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

CONF = ("# three listed, two dispatching\n"
        "repo: operator/alpha dispatch\n"
        "repo: operator/beta enforce\n"
        "repo: operator/gamma enforce dispatch extlane\n")
DISPATCH = ["operator/alpha", "operator/gamma"]


def _module(name, script):
    ldr = importlib.machinery.SourceFileLoader(name, str(BIN / script))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet"
    (fleet / "leases").mkdir(parents=True)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(fleet))
    monkeypatch.setenv("EUNOMIA_SESSION", "enrolment-test")
    monkeypatch.setenv("FLEET_SPAWN", "popen")
    monkeypatch.setenv("FLEET_PIN_ADVANCE_PLIST", str(tmp_path / "no-such.plist"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for k in ("FLEET_WATCH_REPOS", "FLEET_PINS", "FLEET_NTFY_URL"):
        monkeypatch.delenv(k, raising=False)
    conf = tmp_path / "repos.conf"
    conf.write_text(CONF)
    return tmp_path, conf


def _watch(conf):
    mod = _module("fleet_watch_enrol", "fleet-watch")
    mod.REPOS_CONF = conf
    return mod


# 1 -- the three consumers each see exactly the dispatch repositories ---------

def test_the_watcher_the_rebase_sweep_and_fleet_record_see_exactly_the_dispatch_repos(env):
    _, conf = env
    assert _watch(conf).repos() == DISPATCH

    rebase = _module("fleet_rebase_enrol", "fleet-rebase")
    rebase.watch.REPOS_CONF = conf
    assert rebase.watch.repos() == DISPATCH

    record = _module("fleet_record_enrol", "fleet-record")
    record.PINNED_CONF = conf               # plan 0136: fleet-record reads the pin
    assert record.repos() == DISPATCH


# 2 -- the pin set ---------------------------------------------------------------

def test_the_pin_set_is_the_listed_repos_plus_the_non_repo_extras(env, monkeypatch):
    tmp, conf = env
    monkeypatch.setenv("FLEET_PINS", f"{tmp}/agent, {tmp}/held=origin/release")
    base = f"{tmp}/home/.local/share/pins"
    assert _watch(conf).pins() == [
        (f"{base}/alpha", "origin/main"), (f"{base}/beta", "origin/main"),
        (f"{base}/gamma", "origin/main"),
        (f"{tmp}/agent", "origin/main"), (f"{tmp}/held", "origin/release")]


def test_an_unreadable_file_leaves_only_the_extras(env, monkeypatch):
    tmp, _ = env
    monkeypatch.setenv("FLEET_PINS", f"{tmp}/agent")
    mod = _watch(tmp / "absent.conf")
    assert mod.pins() == [(f"{tmp}/agent", "origin/main")]


# 3 -- the old variable is not a source, and a difference is named ---------------

def test_a_differing_FLEET_WATCH_REPOS_is_ignored_and_warned_about_once(env, monkeypatch, capsys):
    tmp, conf = env
    monkeypatch.setenv("FLEET_WATCH_REPOS", "operator/alpha,operator/stale")
    # an orchestrator that cannot arm ends the cycle right after the list is
    # read, which is all this test needs
    monkeypatch.setenv("FLEET_ORCHESTRATOR", str(tmp / "no-orchestrator"))
    mod = _watch(conf)
    mod.notify = lambda *a, **k: True
    assert mod.repos() == DISPATCH                      # stale is not scanned
    mod.cycle(dry_run=True)
    err = capsys.readouterr().err
    warns = [l for l in err.splitlines() if "FLEET_WATCH_REPOS" in l and "differs" in l]
    assert len(warns) == 1
    assert "operator/stale" in warns[0] and "operator/gamma" in warns[0]
    assert "operator/beta" not in warns[0]
    assert mod._env_repos_differ(DISPATCH) is not None
    monkeypatch.setenv("FLEET_WATCH_REPOS", ",".join(DISPATCH))
    assert mod._env_repos_differ(DISPATCH) is None


# 4 -- unreadable is not empty ---------------------------------------------------

@pytest.mark.parametrize("how", ["missing", "unreadable", "no-repo-lines"])
def test_a_repos_conf_that_cannot_be_read_scans_nothing_and_pages_once(env, monkeypatch, how):
    tmp, _ = env
    # the old variable names a repo: a fallback would scan it
    monkeypatch.setenv("FLEET_WATCH_REPOS", "operator/alpha")
    (tmp / "d").mkdir()
    conf = tmp / "d" / "repos.conf"
    if how == "unreadable":
        conf.mkdir()                       # exists, cannot be read as a file
    elif how == "no-repo-lines":
        conf.write_text("# nothing here\nnot a repo line\n")
    mod = _watch(conf)
    pages = []
    mod.notify = lambda title, body, label=None: (pages.append((title, body)), True)[1]

    def forbidden(*a, **k):
        raise AssertionError("a cycle with no readable list reached the forge")
    mod._forge = forbidden
    mod._token = forbidden
    assert mod.conf_repos() is None
    assert mod.repos() == []
    assert mod.cycle() == 0
    assert mod.cycle() == 0
    # plan 0136: a file whose lines are unparseable also pages for the lines
    pages = [p for p in pages if p[0] == "Watcher cannot scan"]
    assert len(pages) == 1, pages
    assert "repos.conf" in pages[0][1]


def test_a_dry_run_pages_nothing_for_an_unreadable_file(env):
    tmp, _ = env
    mod = _watch(tmp / "absent.conf")
    pages = []
    mod.notify = lambda *a, **k: pages.append(a) or True
    assert mod.cycle(dry_run=True) == 0
    assert pages == []


# 5 -- the pin-watch mode never reaches a cycle ----------------------------------

def test_pins_only_returns_before_any_cycle_with_dispatch_repos_listed(env, monkeypatch, capsys):
    _, conf = env
    mod = _watch(conf)
    assert mod.repos() == DISPATCH                      # the list IS present
    reached = []
    mod.cycle = lambda *a, **k: reached.append("cycle") or 0
    mod._cycle = lambda *a, **k: reached.append("_cycle") or 0
    mod.resident = lambda *a, **k: reached.append("resident") or 0
    mod._single_instance = lambda *a, **k: reached.append("lock") or None
    mod.check_pins = lambda dry_run=False, label=None: []
    assert mod.main(["--pins-only"]) == 0
    assert reached == []
    assert "0 pin(s) drifted" in capsys.readouterr().out
