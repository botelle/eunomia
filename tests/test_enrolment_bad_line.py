"""Plan 0136: a line of config/repos.conf the parser cannot read is reported to
the owner, once per distinct set of errors, while every line that parses is
still scanned. Nothing here guesses the flags of the bad line."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
GOOD = ("repo: operator/alpha dispatch\n"
        "repo: operator/beta dispatch enforce\n"
        "repo: operator/gamma dispatch\n")
BAD = "repo: operator/x dispach\n"


def _watch(conf):
    ldr = importlib.machinery.SourceFileLoader("fleet_watch_badline", str(BIN / "fleet-watch"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("fleet_watch_badline", ldr))
    ldr.exec_module(mod)
    mod.REPOS_CONF = conf              # also keeps the suite's FLEET_WATCH_REPOS shim out
    return mod


@pytest.fixture
def rig(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet"
    (fleet / "leases").mkdir(parents=True)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(fleet))
    monkeypatch.setenv("EUNOMIA_SESSION", "badline-test")
    monkeypatch.setenv("FLEET_SPAWN", "popen")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FLEET_ORCHESTRATOR", str(tmp_path / "no-orchestrator"))
    for k in ("FLEET_WATCH_REPOS", "FLEET_PINS", "FLEET_NTFY_URL"):
        monkeypatch.delenv(k, raising=False)
    conf = tmp_path / "repos.conf"

    class Rig:
        pass
    r = Rig()
    r.conf, r.pages = conf, []
    conf.write_text(GOOD + BAD)
    r.mod = _watch(conf)

    def notify(title, body, label=None):
        if "no-orchestrator" not in body:       # the cycle's own arming page is not under test
            r.pages.append((title, body))
        return True
    r.mod.notify = notify
    return r


def test_the_good_lines_are_scanned_the_bad_one_is_warned_and_paged_once(rig, capsys):
    assert rig.mod.repos() == ["operator/alpha", "operator/beta", "operator/gamma"]
    assert "operator/x" not in rig.mod.pins().__repr__()
    rig.mod.cycle()
    rig.mod.cycle()
    err = capsys.readouterr().err
    warns = [l for l in err.splitlines() if "cannot read" in l and "repos.conf:4" in l]
    assert len(warns) == 2                                  # one per cycle
    assert "unknown flag" in warns[0] and "operator/x" in warns[0]
    assert len(rig.pages) == 1, rig.pages
    title, body = rig.pages[0]
    assert title == "repos.conf has a line the watcher cannot read"
    assert "repos.conf:4" in body and "operator/x" in body and "not being scanned" in body


def test_fixing_the_line_clears_the_page_and_a_different_typo_pages_again(rig):
    rig.mod.cycle()
    assert len(rig.pages) == 1
    rig.conf.write_text(GOOD + "repo: operator/x dispatch\n")
    rig.mod.cycle()
    assert len(rig.pages) == 1                              # clean parse pages nothing
    rig.conf.write_text(GOOD + "repo: operator/x dispatc\n")
    rig.mod.cycle()
    assert len(rig.pages) == 2                              # the same typo type, re-made
    rig.conf.write_text(GOOD + "repo: operator/y enfrce\n")
    rig.mod.cycle()
    assert len(rig.pages) == 3 and "operator/y" in rig.pages[2][1]


def test_a_file_whose_only_repo_line_is_bad_pages_both_ways(rig):
    rig.conf.write_text(BAD)
    assert rig.mod.cycle() == 0
    titles = sorted(t for t, _ in rig.pages)
    assert titles == ["Watcher cannot scan", "repos.conf has a line the watcher cannot read"]
    assert any("operator/x" in b for _, b in rig.pages)


def test_a_dry_run_warns_but_pages_nothing(rig, capsys):
    rig.mod.cycle(dry_run=True)
    assert rig.pages == []
    assert "repos.conf:4" in capsys.readouterr().err
