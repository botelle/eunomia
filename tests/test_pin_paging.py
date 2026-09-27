"""A self-healing condition does not page (plan 0059).

`bin/fleet-watch:check_pins` already reports pin drift on every transition
(plan 0058); this file is about the one thing on top of that this plan adds:
a STALE pin that `fleet-pin-advance` will re-point unaided does not wake
anyone, while every other bad state, and a stale pin the advancer cannot
heal, still pages on sight -- exactly as before this plan.

Reuses the same bare-origin + detached-worktree shape
`tests/test_fleet_watch.py::_pin_repo` and `tests/test_pin_advance.py` build,
so this exercises real git states rather than a stubbed pin_status. The
broker is never actually reached in these tests (no socket is configured) --
`broker_verdict` degrades to PROT_UNKNOWN ("could not determine"), which
plan 0059 treats as TRANSIENT/heals, same as a vouched-for pin. Tests that
need the STANDING (refused) broker path stub `mod.broker_verdict` directly,
the same way tests/test_pin_advance.py stubs `mod.watch.broker_verdict`.
"""
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

_ENV = ("FLEET_WATCH_REPOS", "FLEET_WATCH_CAP", "FLEET_OPERATOR_UID",
        "FLEET_PR_PAGE_CAP", "FLEET_ORCHESTRATOR", "FLEET_NTFY_URL",
        "FLEET_IMPLEMENTER_UIDS", "FLEET_IMPLEMENTER_LOGINS",
        "FLEET_AGENT_ACCOUNTS", "FLEET_PINS", "FLEET_FORGEJO_URL",
        "FLEET_BROKER_SOCKET", "FLEET_PIN_ADVANCE_PLIST",
        "FLEET_PIN_PAGE_MARGIN")


def _load(fleet_dir, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["EUNOMIA_SESSION"] = "pin-paging-test"
    os.environ.pop("EUNOMIA_LEDGER_HOST", None)
    for k in _ENV:
        os.environ.pop(k, None)
    os.environ["FLEET_SPAWN"] = "popen"
    # Hermetic default (plan 0059): a path that cannot exist, so
    # pin_advance_window() reads None regardless of whether the machine
    # running this suite has the real unit installed. Tests that want the
    # delay override this explicitly with a fake plist of their own.
    os.environ["FLEET_PIN_ADVANCE_PLIST"] = str(Path(fleet_dir) / "no-such-plist")
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("fleet_watch_pin_paging", str(BIN / "fleet-watch"))
    spec = importlib.util.spec_from_loader("fleet_watch_pin_paging", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
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
    (work / "f").write_text(content)
    subprocess.run(["git", "-C", str(work), "commit", "-qam", "advance"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)


def _write_advance_plist(path, interval):
    with open(path, "wb") as fh:
        plistlib.dump({"StartInterval": interval}, fh)


def _pages(mod):
    log = []
    mod.notify = lambda title, body, label=None: (log.append((title, body)), True)[1]
    return log


def _events(mod):
    ev = mod.lib.fleet_dir() / "events.jsonl"
    if not ev.exists():
        return []
    return [json.loads(l) for l in ev.read_text().splitlines() if l.strip()]


def _windowed(tmp_path, **env):
    """A loaded module with a tiny (1s) advance window and zero margin, so a
    test can push time past it with a handful of seconds rather than really
    waiting half an hour."""
    plist = tmp_path / "fake-pin-advance.plist"
    _write_advance_plist(plist, 1)
    mod = _load(tmp_path / "fleet", FLEET_PIN_ADVANCE_PLIST=str(plist),
               FLEET_PIN_PAGE_MARGIN="0", **env)
    clock = mod.lib.now_utc()
    mod._now = lambda: clock

    def tick(seconds):
        nonlocal clock
        clock = clock + timedelta(seconds=seconds)
        mod._now = lambda: clock
    mod._tick = tick
    return mod


# --- a self-healing stale pin does not page on sight ------------------------

def test_a_stale_pin_the_advancer_can_heal_does_not_page_on_sight(tmp_path):
    """The whole plan in one test: a merge lands, the pin goes stale, and
    fleet-pin-advance is positioned to fast-forward it -- so the FIRST cycle
    that sees it must emit pin-drift (staleness is never silenced) but must
    NOT push a notification."""
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    _advance_origin(work)
    bad = mod.check_pins()
    assert [state for _p, state, _d in bad] == ["stale"], (
        "the bad-pin report is unaffected by the paging delay")
    assert pages == [], "a healable stale pin must not page on sight"
    rows = [e for e in _events(mod) if e["type"] == "pin-drift"]
    assert len(rows) == 1 and rows[0]["detail"]["state"] == "stale", (
        "the transition must still be emitted even though it did not page")


def test_a_stale_pin_pages_once_the_window_and_margin_have_passed(tmp_path):
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    _advance_origin(work)
    mod.check_pins()                       # seen, not yet due
    assert pages == []
    mod._tick(5)                           # past the 1s window + 0s margin
    mod.check_pins()                       # now overdue
    assert len(pages) == 1
    assert pages[0][0] == "Pin stale"
    mod.check_pins()                       # steady state: exactly one page
    mod.check_pins()
    assert len(pages) == 1, "an unchanged stale state must page only once"


def test_three_quick_merges_each_healed_never_page(tmp_path):
    """Plan 0059's own DoD row: several merges healed in seconds must not
    accumulate into a page on the last one -- the clock resets on each new
    HEAD, not on wall-clock time since the pin first went bad."""
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    for i in range(3):
        _advance_origin(work, content=f"{i}\n")
        mod.check_pins()                   # stale: seen, not due
        subprocess.run(["git", "-C", str(pin), "fetch", "-q", "origin"], check=True)
        subprocess.run(["git", "-C", str(pin), "checkout", "-q", "--detach",
                        "origin/main"], check=True, capture_output=True)
        mod.check_pins()                   # healed back to current
    assert pages == [], f"no merge in this run ever outlived the window: {pages}"
    rows = [e for e in _events(mod) if e["type"] == "pin-drift"]
    assert len(rows) == 6, "every transition still emits, even though none paged"


def test_a_delayed_page_that_fails_to_deliver_is_retried(tmp_path):
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    attempts = []

    def flaky_notify(title, body, label=None):
        attempts.append(1)
        return len(attempts) > 1        # first attempt fails, second succeeds
    mod.notify = flaky_notify
    _advance_origin(work)
    mod.check_pins()
    mod._tick(5)
    mod.check_pins()                       # due, but delivery fails
    assert len(attempts) == 1
    mod.check_pins()                       # retried: still due, not "paged"
    assert len(attempts) == 2, "a failed delayed page must be retried, not swallowed"
    mod.check_pins()
    assert len(attempts) == 2, "once delivered, the flag must stick"


# --- states that never self-heal keep paging on sight, window or not -------

def test_a_diverged_stale_pin_pages_immediately(tmp_path):
    """The advancer refuses a non-fast-forward move -- see
    tests/test_pin_advance.py::test_a_diverged_pin_is_refused_with_a_real_repository
    for the mover's own proof of the same refusal. Diverging never heals
    itself, so delaying the page buys nothing."""
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    _advance_origin(work)
    subprocess.run(["git", "-C", str(pin), "commit", "--allow-empty", "-qm",
                    "diverged"], check=True)
    mod.check_pins()
    assert len(pages) == 1, "a standing (diverged) refusal must page on sight"


def test_a_stale_pin_with_uncommitted_changes_pages_immediately(tmp_path):
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    _advance_origin(work)
    (pin / "f").write_text("uncommitted\n")
    mod.check_pins()
    assert len(pages) == 1, "a standing (dirty) refusal must page on sight"


def test_a_stale_pin_with_an_unparseable_origin_pages_immediately(tmp_path):
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    _advance_origin(work)
    called = []
    mod.lib.pin_origin_repo = lambda path: called.append(path) or None
    mod.check_pins()
    assert len(pages) == 1, "an unresolvable origin must page on sight"


def test_a_broker_refusal_pages_immediately(tmp_path):
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    mod.broker_verdict = lambda repo: (mod.PROT_REFUSED, "main is not protected", None)
    _advance_origin(work)
    mod.check_pins()
    assert len(pages) == 1, "a broker that actively refuses must page on sight"


def test_a_broker_that_cannot_be_reached_keeps_waiting(tmp_path):
    """The opposite of the refusal above: PROT_UNKNOWN is transient -- the
    advancer will re-check on its own next run -- so this must NOT page on
    sight, same as an ordinary healable stale pin."""
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    mod.broker_verdict = lambda repo: (mod.PROT_UNKNOWN, "forgejo-broker unreachable", None)
    _advance_origin(work)
    mod.check_pins()
    assert pages == [], "a broker that could not be reached must not page yet"
    mod._tick(5)
    mod.check_pins()
    assert len(pages) == 1, "but it still pages once the window runs out"


@pytest.mark.parametrize("setup,want_state", [
    (lambda w, p: subprocess.run(["git", "-C", str(p), "checkout", "-q", "-b", "wip"],
                                 check=True, capture_output=True), "attached"),
    (lambda w, p: subprocess.run(["git", "-C", str(p), "remote", "set-url", "origin",
                                  str(p / ".." / "gone.git")], check=True), "unfetchable"),
])
def test_non_stale_bad_states_page_on_sight_regardless_of_the_window(tmp_path, setup, want_state):
    """The delay is `stale`-only (plan 0059 Boundaries): every other bad
    state pages exactly as it did before this plan, even with the advancer
    installed and its window wide open."""
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    setup(work, pin)
    state = mod.pin_status(str(pin))[0]
    assert state == want_state, state
    mod.check_pins()
    assert len(pages) == 1, f"{want_state} must page on sight regardless of the window"


def test_a_missing_pin_pages_on_sight_regardless_of_the_window(tmp_path):
    mod = _windowed(tmp_path, FLEET_PINS=str(tmp_path / "does-not-exist"))
    pages = _pages(mod)
    mod.check_pins()
    assert len(pages) == 1


# --- the advancer being absent falls back to today's schedule ---------------

def test_advancer_not_installed_pages_on_todays_schedule(tmp_path):
    """No FLEET_PIN_ADVANCE_PLIST override beyond `_load`'s own hermetic
    default (a path that cannot exist) -- `pin_advance_window()` reads None,
    and a stale pin must page exactly as it did before this plan."""
    work, pin = _pin_repo(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    pages = _pages(mod)
    assert mod.pin_advance_window() is None
    _advance_origin(work)
    mod.check_pins()
    assert len(pages) == 1, "with nothing installed to heal it, delaying buys nothing"


# --- --dry-run reports, writes nothing, pages nothing -----------------------

def test_dry_run_neither_pages_nor_writes_for_a_healable_stale_pin(tmp_path):
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    _advance_origin(work)
    mod.check_pins(dry_run=True)
    assert pages == []
    assert _events(mod) == []
    f = mod._pin_state_file(str(pin))
    assert not f.exists(), "--dry-run must write nothing, even a fresh episode"


# --- legacy state files upgrade instead of replaying a page -----------------

def test_a_legacy_state_file_upgrades_without_replaying_a_page(tmp_path):
    """Boundaries: a pin already recorded stale under the plan-0058 one-line
    format must not page again on the strength of a format upgrade alone --
    treat it as state known, first-seen unknown, ALREADY PAGED."""
    work, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    _advance_origin(work)
    f = mod._pin_state_file(str(pin))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("stale\n")                # the plan-0058-era format
    mod.check_pins()
    assert pages == [], "an upgraded-but-unchanged condition must not replay a page"
    rec = json.loads(f.read_text())
    assert rec["state"] == "stale"
    assert rec["paged"] is True
    assert rec["head"], "the head observed at upgrade time must be recorded"
    # steady state thereafter: still silent
    mod.check_pins()
    assert pages == []


def test_a_legacy_file_whose_pin_already_healed_re_arms_normally(tmp_path):
    """If the recorded condition and the live one disagree, the upgrade must
    not suppress that real transition -- migrating the file is not the same
    thing as the pin having been silently forgiven."""
    _, pin = _pin_repo(tmp_path)
    mod = _windowed(tmp_path, FLEET_PINS=str(pin))
    pages = _pages(mod)
    f = mod._pin_state_file(str(pin))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("stale\n")                # stale on file, but the pin is current now
    mod.check_pins()
    assert pages == [], "current never pages"
    rows = [e for e in _events(mod) if e["type"] == "pin-drift"]
    assert len(rows) == 1 and rows[0]["detail"]["state"] == "current"


# --- fleetlib's shared predicate, directly ----------------------------------

def test_pin_advance_category_heals_when_everything_lines_up(tmp_path):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    mod = _load(tmp_path / "fleet")
    state, _detail, facts = mod.pin_status(str(pin))
    assert state == "stale"
    category, reason = mod.lib.pin_advance_category(
        str(pin), facts["head"], facts["target"],
        mod.lib.pin_origin_repo, lambda repo: ("ok", ""))
    assert category == mod.lib.PIN_ADVANCE_HEALS, reason


def test_pin_advance_category_standing_on_divergence(tmp_path):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    subprocess.run(["git", "-C", str(pin), "commit", "--allow-empty", "-qm",
                    "diverged"], check=True)
    mod = _load(tmp_path / "fleet")
    state, _detail, facts = mod.pin_status(str(pin))
    assert state == "stale"
    category, reason = mod.lib.pin_advance_category(
        str(pin), facts["head"], facts["target"],
        mod.lib.pin_origin_repo, lambda repo: ("ok", ""))
    assert category == mod.lib.PIN_ADVANCE_STANDING
    assert "ancestor" in reason


def test_pin_advance_category_never_calls_the_broker_when_locally_refused(tmp_path):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    (pin / "f").write_text("uncommitted\n")
    mod = _load(tmp_path / "fleet")
    state, _detail, facts = mod.pin_status(str(pin))
    called = []
    category, _reason = mod.lib.pin_advance_category(
        str(pin), facts["head"], facts["target"],
        mod.lib.pin_origin_repo, lambda repo: called.append(repo) or ("ok", ""))
    assert category == mod.lib.PIN_ADVANCE_STANDING
    assert called == [], "a locally-refused pin must never cost a broker round trip"


# --- pin_advance_window / pin_page_margin, in isolation ---------------------

def test_pin_advance_window_reads_start_interval(tmp_path):
    plist = tmp_path / "p.plist"
    _write_advance_plist(plist, 1800)
    mod = _load(tmp_path / "fleet", FLEET_PIN_ADVANCE_PLIST=str(plist))
    assert mod.pin_advance_window() == 1800


def test_pin_advance_window_is_none_when_the_plist_is_absent(tmp_path):
    mod = _load(tmp_path / "fleet",
               FLEET_PIN_ADVANCE_PLIST=str(tmp_path / "nope.plist"))
    assert mod.pin_advance_window() is None


def test_pin_advance_window_is_none_on_a_malformed_plist(tmp_path):
    plist = tmp_path / "bad.plist"
    plist.write_text("not a plist")
    mod = _load(tmp_path / "fleet", FLEET_PIN_ADVANCE_PLIST=str(plist))
    assert mod.pin_advance_window() is None


def test_pin_advance_window_is_read_once_per_process(tmp_path):
    """Boundaries: zero subprocesses, and the plist is read ONCE -- a later
    edit to the file must not change the answer within the same process."""
    plist = tmp_path / "p.plist"
    _write_advance_plist(plist, 1800)
    mod = _load(tmp_path / "fleet", FLEET_PIN_ADVANCE_PLIST=str(plist))
    assert mod.pin_advance_window() == 1800
    _write_advance_plist(plist, 60)
    assert mod.pin_advance_window() == 1800, "must not re-read after the first call"


def test_pin_page_margin_defaults_and_parses(tmp_path):
    mod = _load(tmp_path / "fleet")
    assert mod.pin_page_margin() == 600
    assert mod.pin_page_margin({"FLEET_PIN_PAGE_MARGIN": "30"}) == 30
    with pytest.raises(SystemExit):
        mod.pin_page_margin({"FLEET_PIN_PAGE_MARGIN": "not-a-number"})
    with pytest.raises(SystemExit):
        mod.pin_page_margin({"FLEET_PIN_PAGE_MARGIN": "-1"})
