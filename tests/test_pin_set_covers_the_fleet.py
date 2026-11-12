"""Plans 0122 and 0134: the pin set is computed from config/repos.conf, not copied
into a plist. A repository added to the reviewed list needs no plist edit, and no
plist may carry a per-repo pin or a watch list, because a hand-filled copy on the
host is exactly what drifted (three installed pins against 42 listed)."""
import pytest
import importlib.machinery
import importlib.util
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN_UNITS = ("org.eunomia.fleet-pin-advance", "org.eunomia.fleet-pin-watch")
LIST_UNITS = PIN_UNITS + ("org.eunomia.fleet-watch", "org.eunomia.fleet-rebase")
# Pins that are not a repos.conf repository: talos's deployed checkout.
EXTRAS = {"/Users/operator/agent"}


def _env(unit):
    with open(ROOT / "launchd" / f"{unit}.plist", "rb") as f:
        return plistlib.load(f)["EnvironmentVariables"]


def _conf_names():
    out = set()
    for line in (ROOT / "config" / "repos.conf").read_text().splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] == "repo:":
            out.add(parts[1].split("/", 1)[1])
    return out


def test_no_plist_names_a_watch_list():
    for unit in LIST_UNITS:
        assert "FLEET_WATCH_REPOS" not in _env(unit), unit


def test_no_plist_names_a_per_repo_pin():
    for unit in PIN_UNITS:
        pins = {p.strip() for p in _env(unit).get("FLEET_PINS", "").split(",") if p.strip()}
        assert pins == EXTRAS, f"{unit}: FLEET_PINS carries more than the non-repo pins"


@pytest.mark.skip(reason="asserts on the fleet's own enrolment list; the cut ships an example config/repos.conf")
def test_the_pin_set_computed_from_repos_conf_covers_every_listed_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FLEET_PINS", ",".join(sorted(EXTRAS)))
    ldr = importlib.machinery.SourceFileLoader("fleet_watch_pinset", str(ROOT / "bin" / "fleet-watch"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("fleet_watch_pinset", ldr))
    ldr.exec_module(mod)
    mod.REPOS_CONF = ROOT / "config" / "repos.conf"       # the file under review
    conf = _conf_names()
    assert len(conf) > 3
    pins = {p for p, _ in mod.pins()}
    assert {f"{tmp_path}/.local/share/pins/{n}" for n in conf} <= pins
    assert pins - {f"{tmp_path}/.local/share/pins/{n}" for n in conf} == EXTRAS


def test_no_pin_unit_arms_dispatch():
    # with the list now readable by every unit, the pin-watch unit's only guard
    # is the flag that returns before cycle() (tests/test_enrolment_is_one_list.py)
    args = plistlib.load(open(ROOT / "launchd" / "org.eunomia.fleet-pin-watch.plist", "rb"))["ProgramArguments"]
    assert "--pins-only" in args


def test_no_pin_unit_creates_missing_pins():
    for unit in PIN_UNITS:
        text = (ROOT / "launchd" / f"{unit}.plist").read_text()
        assert "--create-missing" not in text.split("-->", 1)[1]
