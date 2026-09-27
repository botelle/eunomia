"""The closed event set is declared three times, and all three must agree.

`bin/fleet-emit` and `bin/fleet-events` each hold their own copy — deliberately,
per the standalone-scripts comment above `EVENT_TYPES` in `bin/fleet-events`:
these are standalone scripts and a shared module would have to be importable by
hyphenated filenames. (Named rather than cited by line, which rots.) SPEC.md
declares the set a third time, in prose, and it is the authority.

Nothing compared them. They drifted by three values (`plan-dispatched`,
`plan-done`, `plan-failed` — plan 0004's dispatch events), so `fleet-emit` would
write an event `fleet-events --type` refused to read: the watcher's own events
were unqueryable. It surfaced only because `bin/fleet-bundle` happened to print
both sets side by side.

SPEC's list is PARSED here rather than retyped. A test carrying a fourth copy
would be a fourth thing to drift, and would pass while disagreeing with the
document it is supposed to enforce.
"""
import ast
import importlib.machinery
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


def _spec_event_types():
    """The set SPEC.md declares, read out of the document.

    The block is the run of lines after the "Event types" heading up to the
    first blank line; members are the backticked tokens in it.
    """
    text = (ROOT / "SPEC.md").read_text()
    m = re.search(r"^Event types \(v0\.2 closed set[^)]*\):\n((?:.+\n)+?)\n",
                  text, re.M)
    assert m, "SPEC.md no longer declares the event types where this test looks"
    tokens = re.findall(r"`([^`]+)`", m.group(1))
    accepted = {t for t in tokens if re.fullmatch(r"[a-z0-9][a-z0-9-]*", t)}
    # Every backticked token in the block must be accepted. An under-reading
    # parser does not fail here, it fails LATER and blames the CLI: drop
    # `v2-thing` from the spec set and the message reads "only in the CLI:
    # ['v2-thing']" while the CLI and SPEC actually agree. A test that
    # misattributes is worse than one that misses.
    assert set(tokens) == accepted, (
        "SPEC declares tokens this parser does not accept: "
        f"{sorted(set(tokens) - accepted)}")
    assert len(accepted) > 15, f"SPEC block parsed to {len(accepted)} types; parser is wrong"
    return accepted


def _module_event_types(filename):
    """The set a CLI declares, from its AST — no import, no side effects."""
    tree = ast.parse((BIN / filename).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "EVENT_TYPES":
            return set(ast.literal_eval(node.value))
    pytest.fail(f"bin/{filename} declares no module-level EVENT_TYPES")


def test_the_two_cli_copies_agree():
    emit = _module_event_types("fleet-emit")
    events = _module_event_types("fleet-events")
    assert emit == events, (
        "fleet-emit and fleet-events disagree; "
        f"only in emit: {sorted(emit - events)}; only in events: {sorted(events - emit)}")


def test_both_copies_match_the_spec():
    """SPEC.md is the authority — on disagreement the CLIs are wrong, not it."""
    spec = _spec_event_types()
    for name in ("fleet-emit", "fleet-events"):
        declared = _module_event_types(name)
        assert declared == spec, (
            f"bin/{name} disagrees with SPEC.md; "
            f"only in the CLI: {sorted(declared - spec)}; "
            f"only in SPEC: {sorted(spec - declared)}")


def test_the_dispatch_events_are_readable():
    """The regression itself, named — the case the drift actually broke.

    Asserted through the real validator rather than on the constant, because
    the constant being right is not the property that failed; being able to
    query the watcher's own events is.
    """
    events = _load("fleet_events", BIN / "fleet-events")
    assert events._parse_types(["plan-dispatched,plan-done,plan-failed"]) == {
        "plan-dispatched", "plan-done", "plan-failed"}
