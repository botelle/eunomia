"""Tests for plan 0058 — every unit that pages records it.

`bin/orchestrator`'s `record_page` already writes `pages.jsonl`, and two
pagers (the orchestrator itself, `bin/mopsus`) already reused it. This plan
closes the rest of the inventory: `bin/fleet-watch` (both its launchd
labels), `bin/fleet-leak-watch`, `bin/fleet-orphans`, and `bin/fleet-pin-advance`
now all record through the same function, and this file drives each one
through its own notify path and reads the ledger back — the same
SourceFileLoader-per-file, EUNOMIA_FLEET_DIR-per-test pattern each pager's own
test suite already uses, so nothing here needs a network or a real page.

`bin/orchestrator` and `bin/mopsus` are deliberately NOT re-tested here: this
plan does not touch either (DoD: both rows read "unchanged"), and each already
has its own suite.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"

# Every env var fleet-watch reads at import (mirrors tests/test_fleet_watch.py
# and tests/test_pin_advance.py's own lists) -- anything missing here leaks
# into the next _load and makes the suite order-dependent.
_ENV = ("FLEET_WATCH_REPOS", "FLEET_WATCH_CAP", "FLEET_OPERATOR_UID",
        "FLEET_PR_PAGE_CAP", "FLEET_ORCHESTRATOR", "FLEET_NTFY_URL",
        "FLEET_IMPLEMENTER_UIDS", "FLEET_IMPLEMENTER_LOGINS",
        "FLEET_AGENT_ACCOUNTS", "FLEET_PINS", "FLEET_FORGEJO_URL",
        "FLEET_DISPATCH_IMPL", "FLEET_VERDICT_SOURCE", "FLEET_DIRECT_TOKEN_CMD",
        "FLEET_BROKER_SOCKET", "FLEET_ANGELIA_URL", "FLEET_ANGELIA_APP",
        "FLEET_ANGELIA_USER", "FLEET_ANGELIA_TOKEN_CMD")


def _reset_env(fleet_dir, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["EUNOMIA_SESSION"] = "page-record-test"
    for k in _ENV:
        os.environ.pop(k, None)
    os.environ["FLEET_SPAWN"] = "popen"           # read by fleet-watch at import
    for k, v in env.items():
        os.environ[k] = v


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


def _load_fleet_watch(fleet_dir, **env):
    _reset_env(fleet_dir, **env)
    return _load("fleet_watch_page_record", BIN / "fleet-watch")


def _load_fleet_orphans(fleet_dir, **env):
    _reset_env(fleet_dir, **env)
    return _load("fleet_orphans_page_record", BIN / "fleet-orphans")


def _load_fleet_leak_watch(fleet_dir, **env):
    _reset_env(fleet_dir, **env)
    return _load("fleet_leak_watch_page_record", BIN / "fleet-leak-watch")


def _load_fleet_pin_advance(fleet_dir, **env):
    _reset_env(fleet_dir, **env)
    return _load("fleet_pin_advance_page_record", BIN / "fleet-pin-advance")


def _rows(fleet_dir):
    p = Path(fleet_dir) / "pages.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# --- bin/fleet-watch ---------------------------------------------------------

def test_a_watcher_page_names_the_unit(tmp_path):
    """A dispatch-path page (e.g. orphan_sweep's dead-orchestrator page) goes
    through `_notify_once`, which forwards no label of its own -- notify()'s
    default (LABEL_WATCH) must be what lands in the ledger."""
    fd = tmp_path / "fleet"
    mod = _load_fleet_watch(fd)
    assert mod._notify_once("lease-1", "Orchestrator dead", "operator/x 0001") is True
    rows = _rows(fd)
    assert len(rows) == 1
    assert rows[0]["title"] == "Orchestrator dead"
    assert rows[0]["unit"] == mod.LABEL_WATCH == "org.eunomia.fleet-watch"
    assert rows[0]["delivered"] is False           # FLEET_NTFY_URL unset
    assert rows[0]["detail"] == "no channel configured"


def test_fleet_watch_resident_label(tmp_path):
    fd = tmp_path / "fleet"
    mod = _load_fleet_watch(fd)
    assert mod.notify("t", "b", mod.LABEL_WATCH) is True
    rows = _rows(fd)
    assert rows[0]["unit"] == "org.eunomia.fleet-watch"


def test_fleet_watch_pins_only_label(tmp_path):
    """A page from `fleet-watch --pins-only` must read `fleet-pin-watch`, not
    `fleet-watch` -- the whole point of a per-call label rather than a
    module-level constant (plan 0058: two launchd identities, one file)."""
    fd = tmp_path / "fleet"
    mod = _load_fleet_watch(fd)
    assert mod.notify("t", "b", mod.LABEL_PIN_WATCH) is True
    rows = _rows(fd)
    assert rows[0]["unit"] == "org.eunomia.fleet-pin-watch"


def test_check_pins_forwards_the_caller_s_label(tmp_path):
    """check_pins() itself cannot tell `--pins-only` from the dispatch cycle
    -- both run the identical check -- so the label must come from whichever
    caller invoked it (main()'s --pins-only branch vs. resident()/the plain
    path), not from a default baked into check_pins itself."""
    fd = tmp_path / "fleet"
    mod = _load_fleet_watch(fd)
    mod.pins = lambda: [("fakepin", "origin/main")]
    mod.pin_status = lambda path, ref="origin/main": (
        "stale", "fakepin: 1234567 behind abcdefg",
        {"head": "1234567", "target": "abcdefg"})
    mod.check_pins(label=mod.LABEL_PIN_WATCH)
    rows = _rows(fd)
    assert len(rows) == 1
    assert rows[0]["unit"] == "org.eunomia.fleet-pin-watch"


def test_a_blank_ntfy_url_records_no_channel_configured(tmp_path):
    fd = tmp_path / "fleet"
    mod = _load_fleet_watch(fd, FLEET_NTFY_URL="")
    assert mod.notify("t", "b") is True
    rows = _rows(fd)
    assert rows[0]["delivered"] is False
    assert rows[0]["detail"] == "no channel configured"


def test_a_delivery_failure_records_the_exception_class_name_only(tmp_path):
    """r8 M3's schemeless URL: urlopen raises ValueError, whose message echoes
    the malformed URL. Only the CLASS NAME may reach the durable file."""
    fd = tmp_path / "fleet"
    mod = _load_fleet_watch(fd, FLEET_NTFY_URL="ntfy.sh/no-scheme")
    assert mod.notify("t", "b") is False
    rows = _rows(fd)
    assert rows[0]["delivered"] is False
    assert rows[0]["detail"] == "ValueError"
    assert "ntfy.sh" not in json.dumps(rows[0])


def test_a_fleet_pin_advance_page_names_fleet_pin_advance(tmp_path):
    fd = tmp_path / "fleet"
    mod = _load_fleet_pin_advance(fd)
    assert mod.LABEL == "fleet-pin-advance"
    assert mod.watch.notify("Pin advanced", "detail", mod.LABEL) is True
    rows = _rows(fd)
    assert rows[0]["unit"] == "fleet-pin-advance"


def test_record_page_raising_does_not_fail_the_page(tmp_path):
    """Boundaries: 'a failed record must not fail the page.' Simulated by
    breaking the lazy loader itself (record_page is loaded fresh per call,
    not at import -- see _record_page's own docstring) -- notify() must
    still return normally."""
    fd = tmp_path / "fleet"
    mod = _load_fleet_watch(fd)

    def boom(*a, **k):
        raise RuntimeError("cannot load orchestrator")
    orig_loader = mod.importlib.machinery.SourceFileLoader
    mod.importlib.machinery.SourceFileLoader = boom
    try:
        assert mod.notify("t", "b") is True            # did not raise
    finally:
        mod.importlib.machinery.SourceFileLoader = orig_loader
    assert not (fd / "pages.jsonl").exists()        # nothing was written either


# --- bin/fleet-orphans --------------------------------------------------------

def test_a_fleet_orphans_page_names_fleet_orphans(tmp_path):
    fd = tmp_path / "fleet"
    mod = _load_fleet_orphans(fd)
    assert mod._notify("fleet-orphans: orphan", "operator/x branch sha") is True
    rows = _rows(fd)
    assert rows[0]["unit"] == "fleet-orphans"
    assert rows[0]["delivered"] is False
    assert rows[0]["detail"] == "no channel configured"


def test_fleet_orphans_delivery_failure_records_class_name(tmp_path):
    fd = tmp_path / "fleet"
    mod = _load_fleet_orphans(fd, FLEET_NTFY_URL="ntfy.sh/no-scheme")
    assert mod._notify("t", "b") is False
    rows = _rows(fd)
    assert rows[0]["detail"] == "ValueError"


# --- bin/fleet-leak-watch -----------------------------------------------------

def test_a_leak_watch_page_carries_path_shape_and_line_never_a_hash(tmp_path):
    """The one pager whose subject matter is secrets: the recorded body must
    be built from path/shape/line, and must never carry `_finding_key`'s
    truncated hash of the matched text (Boundaries)."""
    fd = tmp_path / "fleet"
    mod = _load_fleet_leak_watch(fd)
    mod._alert("somefile.jsonl", 42, "github-pat")
    rows = _rows(fd)
    assert len(rows) == 1
    assert rows[0]["unit"] == "fleet-leak-watch"
    assert "somefile.jsonl" in rows[0]["body"]
    assert "42" in rows[0]["body"]
    assert "github-pat" in rows[0]["body"]
    # never the matched text or a hash of it -- there is none in scope here,
    # but assert the shape directly: no hex-looking hash fragment sneaked in.
    assert "sha256" not in json.dumps(rows[0]).lower()


def test_leak_watch_delivery_failure_records_class_name_never_the_url(tmp_path):
    fd = tmp_path / "fleet"
    mod = _load_fleet_leak_watch(fd, FLEET_NTFY_URL="ntfy.sh/no-scheme")
    mod.NTFY_URL = "ntfy.sh/no-scheme"
    mod._alert("f.jsonl", 1, "aws-key")
    rows = _rows(fd)
    assert rows[0]["detail"] == "ValueError"
    assert "ntfy.sh" not in json.dumps(rows[0])


def test_leak_watch_record_page_raising_does_not_fail_the_alert(tmp_path):
    """Boundaries: 'a failed record must not fail the page.' Simulated by
    breaking the lazy loader itself -- _alert() must still return normally."""
    fd = tmp_path / "fleet"
    mod = _load_fleet_leak_watch(fd)

    def boom(*a, **k):
        raise RuntimeError("cannot load orchestrator")
    orig_loader = mod.importlib.machinery.SourceFileLoader
    mod.importlib.machinery.SourceFileLoader = boom
    try:
        mod._alert("f.jsonl", 1, "openai-key")     # must not raise
    finally:
        mod.importlib.machinery.SourceFileLoader = orig_loader
    assert not (fd / "pages.jsonl").exists()
