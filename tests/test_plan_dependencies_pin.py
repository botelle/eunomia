"""Plan 0167: `pin:<owner/repo>:<path>` in `depends_on`, against a real
temporary git repository standing in for the pin."""
import importlib.machinery
import importlib.util
import os
import stat
import subprocess
from pathlib import Path

import pytest

import test_fleet_watch as tw

BIN = Path(__file__).resolve().parent.parent / "bin"


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=a@b", "-c", "user.name=n",
                    *args], check=True, capture_output=True)


@pytest.fixture
def watch(tmp_path, monkeypatch):
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    monkeypatch.setattr(mod, "conf_repos",
                        lambda: (["operator/eunomia"],
                                 ["operator/eunomia", "operator/talos", "operator/nopin"]))
    pin = tmp_path / "pins" / "talos"
    pin.mkdir(parents=True)
    _git(pin, "init", "-q", "-b", "main")
    (pin / "bin").mkdir()
    (pin / "bin" / "tool").write_text("x\n")
    _git(pin, "add", "bin/tool")
    _git(pin, "commit", "-q", "-m", "one")
    monkeypatch.setattr(mod, "pins", lambda: [(str(pin), "origin/main")])
    mod.PIN = pin
    return mod


def _block(watch, spec):
    return watch._plan_dependency_block("operator/eunomia", [f"pin:{spec}"], {}, "t")


def test_committed_at_head_is_met(watch):
    assert _block(watch, "operator/talos:bin/tool") is None


def test_uncommitted_file_is_not_on_pin(watch):
    (watch.PIN / "new.py").write_text("x\n")
    got = _block(watch, "operator/talos:new.py")
    assert got[1:3] == ("pin", "not on pin")


def test_staged_but_uncommitted_is_not_on_pin(watch):
    (watch.PIN / "new.py").write_text("x\n")
    _git(watch.PIN, "add", "new.py")
    assert _block(watch, "operator/talos:new.py")[2] == "not on pin"


def test_path_on_other_branch_is_unmet(watch):
    _git(watch.PIN, "checkout", "-q", "-b", "other")
    (watch.PIN / "other.py").write_text("x\n")
    _git(watch.PIN, "add", "other.py")
    _git(watch.PIN, "commit", "-q", "-m", "two")
    _git(watch.PIN, "checkout", "-q", "main")
    assert _block(watch, "operator/talos:other.py")[2] == "not on pin"


def test_no_pin(watch):
    assert _block(watch, "operator/nopin:x")[2] == "no pin"


def test_pin_directory_missing(watch, tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "pins", lambda: [(str(tmp_path / "pins" / "talos-gone"),
                                                 "origin/main")])
    monkeypatch.setattr(watch, "conf_repos",
                        lambda: (["operator/eunomia"], ["operator/eunomia", "operator/talos-gone"]))
    assert _block(watch, "operator/talos-gone:x")[2] == "pin unreadable"


def test_not_a_git_checkout(watch, tmp_path, monkeypatch):
    d = tmp_path / "pins" / "plain"
    d.mkdir()
    monkeypatch.setattr(watch, "pins", lambda: [(str(d), "origin/main")])
    monkeypatch.setattr(watch, "conf_repos",
                        lambda: (["operator/eunomia"], ["operator/eunomia", "operator/plain"]))
    assert _block(watch, "operator/plain:x")[2] == "pin unreadable"


@pytest.mark.parametrize("p", ["../x", "bin/../../x", "/etc/passwd"])
def test_refused_before_git_runs(watch, monkeypatch, p):
    def boom(*a, **k):
        raise AssertionError("git must not run")
    monkeypatch.setattr(watch.subprocess, "run", boom)
    assert _block(watch, f"operator/talos:{p}")[2] == "refused"


def test_hanging_git_is_unmet(watch, tmp_path, monkeypatch):
    stub = tmp_path / "stubbin"
    stub.mkdir()
    g = stub / "git"
    g.write_text("#!/bin/sh\nexec sleep 30\n")
    g.chmod(g.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setattr(watch, "_child_env", lambda: dict(os.environ))
    monkeypatch.setattr(watch, "PIN_CHECK_TIMEOUT", 0.5)
    got = _block(watch, "operator/talos:bin/tool")
    assert got is not None and got[2] == "pin unreadable"


# lint ---------------------------------------------------------------------

@pytest.fixture
def plan(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("fleet_plan_pin", str(BIN / "fleet-plan"))
    spec = importlib.util.spec_from_loader("fleet_plan_pin", loader)
    fp = importlib.util.module_from_spec(spec)
    loader.exec_module(fp)

    class W:
        conf_repos = staticmethod(lambda: (["operator/eunomia"],
                                           ["operator/eunomia", "operator/talos", "operator/nopin"]))
        _pin_for = staticmethod(lambda r: Path("/p") if r == "operator/talos" else None)
    monkeypatch.setattr(fp, "_load_watch", lambda: W)
    return fp


def test_lint_unknown_repo_is_error(plan):
    errs = plan._lint_forge_deps("p.md", {"depends_on": ["pin:operator/zzz:x"]})
    assert len(errs) == 1 and "config/repos.conf" in errs[0]


def test_lint_malformed_is_error(plan):
    for bad in ("pin:nonsense", "pin:operator/talos:../x", "pin:operator/talos:/x"):
        assert plan._lint_forge_deps("p.md", {"depends_on": [bad]})


def test_lint_valid_no_error(plan):
    assert plan._lint_forge_deps("p.md", {"depends_on": ["pin:operator/talos:x"]}) == []


def test_lint_repo_without_pin_is_warning_only(plan):
    fm = {"depends_on": ["pin:operator/nopin:x"]}
    assert plan._lint_forge_deps("p.md", fm) == []
    assert len(plan._pin_warnings("p.md", fm)) == 1
    assert plan._pin_warnings("p.md", {"depends_on": ["pin:operator/talos:x"]}) == []
