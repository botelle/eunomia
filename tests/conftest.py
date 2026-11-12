"""Suite-wide guards against a test leaking process-global state.

There are two: `urllib.request.urlopen`, and `os.environ`. Each cost a full CI
lane before anyone saw it, and both failed the same way — see BOTH LOOK LIKE A
FLAKE at the bottom.

WHAT HAPPENED (2026-09-10, eunomia#252). Three angelia tests patched the sender
with a bare assignment:

    mod.urllib.request.urlopen = lambda *a, **k: R()

`mod.urllib` is not a per-module copy — it is the process-wide `urllib` module —
so that assignment replaced `urlopen` for every test that ran afterwards and was
never restored. All nineteen `_stub_server` tests in `tests/test_fleetforge.py`
then received the angelia body instead of the forge's:

    assert (200, {'failed': 0, 'pruned': 1, 'sent': 0}) == (200, {'id': 1})

It surfaced in exactly one of the two lanes that ran the same suite on the same
commit, because `pytest-randomly` gave them different seeds and only one ordering
put the angelia file first. `CI / tests` was green and `Plans / lint` was red,
which reads like a lint problem and is not one.

THE SAME THING AGAIN, IN THE ENVIRONMENT (2026-09-22, eunomia#522/#533).
`tests/test_verdict_sources.py` `_load()` assigns `os.environ[k] = v` and clears
those keys only on its NEXT call, so the last `_load` in the file leaves its
value in the process. One of them sets a deliberately invalid
`FLEET_VERDICT_SOURCE='cloud'`, and `verdict_source()` reads that variable live
and calls `sys.exit` on a value it does not recognise. Eleven tests in
`tests/test_repo.py` — which never mentions the variable — then died with

    SystemExit: fleet-watch: FLEET_VERDICT_SOURCE='cloud' is not 'broker' or 'direct'

on two unrelated branches at once, while `main` stayed green.

Reset-on-entry is why this survived so long. Several files keep a tuple of
variables to `pop` before each load (`_WATCH_ENV` in tests/test_fleet_watch.py
has eighteen, several added after a different escape, and the comments record
them). That protects the file
that owns the tuple and does nothing for the file that runs after it, so every
one of those comments is about a variable someone else's test set. Restoring on
the way OUT is the half nobody was doing.

WHY THIS ONE ONLY REPAIRS. The urlopen guard fails the culprit; this one does
not, yet. 18 of the 58 test files assign `os.environ[...]` directly, across 74
sites, and none of those assignments is wrong on its own — a failing guard would
turn them all red in one commit for a refactor that is not this fix. The restore
alone closes the hole: no test can inherit another's environment whether or not
the assignment is tidy. Tightening it to fail, once the call sites use
`monkeypatch.setenv`, is a follow-up and not a prerequisite.

BOTH LOOK LIKE A FLAKE, AND THAT IS THE EXPENSIVE PART. Neither leak fails the
test that causes it, and whether it fires at all is down to the order
`pytest-randomly` picks. So the same commit goes green in one lane and red in
the other, the failure names innocent tests in a file that has nothing to do
with the change under review, and the obvious readings — a bad runner, an
infrastructure flake, a lint problem — are all wrong. #522 was diagnosed as an
intermittent cihost runner, on the strength of a local suite that passed under
one pytest-randomly seed, before anyone read the job log.

WHY A GUARD AND NOT JUST THE FIX. The three call sites are fixed, but the shape
recurs: `mod.<stdlib module>.<attr> = x` looks local and is not. The failure it
produces names nineteen innocent tests in another file and never names the
culprit, so the cost of finding it again is what this file removes.

The guard REPAIRS before it fails. Leaving the leak in place would let one bad
test cascade into every later one, and the cascade is what buries the cause.

A RUN THAT NEVER ENDS (2026-09-27, eunomia#605). Two review runs of this suite
on opshost were still going after 2 days 21 hours, looping in
tests/test_watch_self_restart.py. Three things here made that possible, and
opshost ended at load average 368:

  - No test had a time limit. `_test_timeout` below fails any test that runs
    longer than EUNOMIA_TEST_TIMEOUT seconds (default 300).
  - Every `git commit` in a temp repo could start `git maintenance run --auto
    --detach`, which reparents to launchd and outlives the test and pytest.
    One commit every few seconds stacked up ~360 of them. The environment set
    at import below turns auto-maintenance off for every git this suite runs.
  - `fleet-watch resident()` installs a SIGTERM handler that only sets a flag,
    and nothing put the old one back. From then on `kill` did nothing to the
    pytest process; only `kill -9` stopped it. `_no_signal_leaks` restores the
    handlers after every test. Like the environment guard, it repairs and does
    not fail: installing that handler is resident()'s job.
"""
import importlib.machinery
import os
import signal
import sys
import urllib.request

import pytest


def _disable_git_auto_maintenance():
    """Append to GIT_CONFIG_COUNT rather than overwrite it, so a runner that
    already passes config through the environment keeps it."""
    n = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    for key, value in (("maintenance.auto", "false"), ("gc.auto", "0")):
        os.environ[f"GIT_CONFIG_KEY_{n}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{n}"] = value
        n += 1
    os.environ["GIT_CONFIG_COUNT"] = str(n)


_disable_git_auto_maintenance()

# Add a (module, attribute) pair here when a test is caught leaking one.
_GLOBALS = (
    (urllib.request, "urlopen"),
)


@pytest.fixture(autouse=True)
def _no_process_global_leaks():
    """Fail the test that leaks, not the twenty that follow it."""
    before = [(mod, attr, getattr(mod, attr)) for mod, attr in _GLOBALS]
    yield
    leaked = []
    for mod, attr, original in before:
        if getattr(mod, attr) is not original:
            setattr(mod, attr, original)      # repair FIRST — see the docstring
            leaked.append(f"{mod.__name__}.{attr}")
    if leaked:
        pytest.fail(
            "this test left process-global state patched: " + ", ".join(leaked) +
            ". `mod.urllib.request.urlopen = x` patches the SHARED module, not a "
            "copy — use `monkeypatch.setattr(mod.urllib.request, 'urlopen', x)`, "
            "which restores. It has been repaired so the rest of the run is not "
            "collateral."
        )


@pytest.fixture(autouse=True)
def _no_env_leaks():
    """Restore `os.environ` after every test.

    `os.environ` is process-global exactly like the module attributes above, and
    it leaks the same way: `os.environ["X"] = v` outlives the test that wrote it
    and is read by whatever runs next. See the module docstring for the run that
    paid for this.

    Only the keys a test actually changed are touched, so the repair is a no-op
    for the tests that use `monkeypatch.setenv` and already clean up after
    themselves.
    """
    before = dict(os.environ)
    yield
    for key in [k for k in os.environ if k not in before]:
        del os.environ[key]                       # added by the test
    for key, value in before.items():
        if os.environ.get(key) != value:
            os.environ[key] = value               # changed or deleted by it


@pytest.fixture(autouse=True)
def _no_reviewer_roots(tmp_path_factory, monkeypatch):
    """Point plan 0092's reviewer-published paths at nothing, so no test reads (or
    pages on) the real /Users/Shared/dispatch-review of the host running the suite.
    A test that wants a root sets the variable itself."""
    nowhere = tmp_path_factory.mktemp("no-reviewer")
    monkeypatch.setenv("FLEET_REVIEWER_TRANSCRIPTS", str(nowhere / "transcripts"))
    monkeypatch.setenv("FLEET_REVIEWER_GUARD_LOG", str(nowhere / "secret-guard.log"))


@pytest.fixture(autouse=True)
def _no_host_review_path():
    """Keep fleet-watch's review-path probe (plan 0095) off the host's real
    talos contract and eunomia pin. On opshost both exist, and a test that spawns
    would be held by whatever state the live pin is in. A test of the probe sets
    these itself."""
    os.environ["FLEET_REVIEW_CONTRACT"] = "/nonexistent/eunomia-calls.json"
    os.environ["FLEET_REVIEW_PINS"] = "/nonexistent/pins/bin"


@pytest.fixture(autouse=True)
def _config_stand_ins(_no_process_global_leaks, tmp_path_factory, monkeypatch):
    """Plan 0132: the orchestrator reads the configured reviewers and the review
    bar from `fleet-models` / `fleet-config`, and a test fleet database lists none
    (`fleet-models reviewers` exits 3). Point it at one reviewer and bar `high`;
    a test of the config reads sets its own.

    It names `_no_process_global_leaks` first so that fixture is set up before
    `monkeypatch` and torn down after it; otherwise an autouse fixture sorting
    ahead of the guard pulls `monkeypatch` in first, the guard checks before
    monkeypatch undoes, and every test that patches urlopen reads as a leak."""
    d = tmp_path_factory.mktemp("cfgbin")
    (d / "fleet-models").write_text(
        '#!/bin/sh\necho \'[{"ordinal": 1, "runner": "r", "model": "m"}]\'\n')
    (d / "fleet-config").write_text(
        '#!/bin/sh\necho \'{"key": "review_bar", "value": "high"}\'\n')
    for f in d.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("FLEET_CONFIG_BIN", str(d))


@pytest.fixture(autouse=True)
def _watch_repos_conf_stand_in(tmp_path_factory):
    """Plan 0134: keep fleet-watch off the real config/repos.conf; see
    tests/fakes/repos_conf_shim.py. FLEET_WATCH_REPOS is read by the harness,
    not by fleet-watch."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fakes"))
    try:
        import repos_conf_shim
    except ImportError:         # a copy of this file in a scratch dir (test_env_isolation)
        yield
        return
    finally:
        sys.path.pop(0)
    original = repos_conf_shim.install(tmp_path_factory.mktemp("repos-conf"))
    try:
        yield
    finally:
        importlib.machinery.SourceFileLoader.exec_module = original


_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP")
                 if hasattr(signal, name))


@pytest.fixture(autouse=True)
def _no_signal_leaks():
    """Put back any signal handler a test replaced. See A RUN THAT NEVER ENDS
    above: a leaked handler is how `kill` stopped working on a runaway run."""
    before = {sig: signal.getsignal(sig) for sig in _SIGNALS}
    yield
    for sig, handler in before.items():
        if signal.getsignal(sig) is not handler:
            signal.signal(sig, handler)


@pytest.fixture(autouse=True)
def _test_timeout(request):
    """Fail a test that runs longer than EUNOMIA_TEST_TIMEOUT seconds (default
    300; 0 turns it off). SIGALRM, not pytest-timeout: the suite runs under
    whatever python the host has (CI's venv, a review's Xcode 3.9), and this
    needs nothing installed. `pytest.fail` raises a BaseException, so it gets
    past `except Exception` handlers inside the code under test."""
    seconds = int(os.environ.get("EUNOMIA_TEST_TIMEOUT", "300") or 0)
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _expired(_signum, _frame):
        pytest.fail(f"{request.node.nodeid} ran longer than {seconds}s "
                    "(EUNOMIA_TEST_TIMEOUT)", pytrace=True)

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
