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
"""
import os
import urllib.request

import pytest

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
