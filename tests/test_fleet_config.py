"""Tests for bin/fleet-config (plan 0068).

`fleet_setting` / `fleet_setting_change` join `repo_model` / `repo_model_change`
as durable, non-derived tables inside `fleet.db` (`bin/fleet-models`, plan 0048/
0063, is the precedent this file's shape copies). `bin/fleet-config` is the one
writer; `bin/fleet-watch`'s `cycle()` only ever reads, via `resolve_setting`.
"""
import importlib.machinery
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def _load(name, mod_name=None):
    mod_name = mod_name or name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _fc(tmp_path, monkeypatch, db_name="fleet.db"):
    """A fleet-config module reading an isolated tmp `FLEET_DB`, with the
    ledger it emits to beside it (via EUNOMIA_FLEET_DIR)."""
    monkeypatch.setenv("FLEET_DB", str(tmp_path / db_name))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    return _load("fleet-config")


def _events(fleet_dir, etype=None):
    p = Path(fleet_dir) / "events.jsonl"
    if not p.exists():
        return []
    evs = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return [e for e in evs if etype is None or e["type"] == etype]


# ------------------------------------------------------------------ D1 / D2


def test_set_writes_row_change_and_event(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    changed, value = mod.set_value(con, "dispatch_cap", "3", actor="cli:operator")
    assert changed and value == 3

    row = con.execute(
        "SELECT value FROM fleet_setting WHERE key='dispatch_cap'").fetchone()
    assert row == ("3",)
    changes = con.execute(
        "SELECT old_value, new_value, actor, source FROM fleet_setting_change"
    ).fetchall()
    assert changes == [(None, "3", "cli:operator", "fleet-config")]

    evs = _events(tmp_path, "fleet-setting-changed")
    assert len(evs) == 1
    e = evs[0]
    assert (e["actor"], e["repo"], e["lease"]) == ("cli:operator", None, None)
    assert e["detail"] == {"key": "dispatch_cap", "old": None, "new": "3"}


def test_a_set_to_the_value_already_in_force_writes_nothing(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    changed, value = mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    assert not changed and value == 3
    assert con.execute("SELECT COUNT(*) FROM fleet_setting_change").fetchone()[0] == 1
    assert len(_events(tmp_path, "fleet-setting-changed")) == 1


def test_the_event_carries_only_the_pinned_keys(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    assert mod.EVENT_DETAIL_KEYS == ("key", "old", "new")
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "2", actor="cli:t")
    mod.set_value(con, "dispatch_cap", "5", actor="cli:t")
    for e in _events(tmp_path, "fleet-setting-changed"):
        assert set(e["detail"]) == set(mod.EVENT_DETAIL_KEYS)
    evs = _events(tmp_path, "fleet-setting-changed")
    assert [e["detail"]["old"] for e in evs] == [None, "2"]
    assert [e["detail"]["new"] for e in evs] == ["2", "5"]


def test_the_event_type_is_in_the_closed_set_everywhere():
    emit, events = _load("fleet-emit"), _load("fleet-events")
    assert "fleet-setting-changed" in emit.EVENT_TYPES
    assert events._parse_types(["fleet-setting-changed"]) == {"fleet-setting-changed"}
    assert "`fleet-setting-changed`" in (ROOT / "SPEC.md").read_text()


# ----------------------------------------------------------- D1: validation


def test_unknown_key_is_refused_before_any_write(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    with pytest.raises(mod.UnknownSetting, match=r"dispatch_cap"):
        mod.set_value(con, "nosuchkey", "1", actor="cli:t")
    assert con.execute("SELECT COUNT(*) FROM fleet_setting").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM fleet_setting_change").fetchone()[0] == 0
    assert _events(tmp_path) == []


@pytest.mark.parametrize("bad", ["-1", "two", "1.5", "", " "])
def test_invalid_values_are_refused_before_any_write(tmp_path, monkeypatch, bad):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    with pytest.raises(ValueError):
        mod.set_value(con, "dispatch_cap", bad, actor="cli:t")
    assert con.execute("SELECT COUNT(*) FROM fleet_setting").fetchone()[0] == 0
    assert _events(tmp_path) == []


def test_zero_is_accepted_the_pause_switch_not_an_error(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    changed, value = mod.set_value(con, "dispatch_cap", "0", actor="cli:t")
    assert changed and value == 0


def test_the_cli_set_refuses_and_names_accepted_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    r = subprocess.run(
        [sys.executable, str(BIN / "fleet-config"), "set", "nosuchkey", "1"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "FLEET_DB": str(tmp_path / "fleet.db")})
    assert r.returncode != 0
    assert "dispatch_cap" in r.stderr


def test_the_cli_set_emits_the_event(tmp_path, monkeypatch):
    fdir = tmp_path / "fleet"
    fdir.mkdir()
    r = subprocess.run(
        [sys.executable, str(BIN / "fleet-config"), "set", "dispatch_cap", "4",
         "--actor", "cli:operator"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "FLEET_DB": str(fdir / "fleet.db"),
             "EUNOMIA_FLEET_DIR": str(fdir)})
    assert r.returncode == 0, r.stderr
    evs = _events(fdir, "fleet-setting-changed")
    assert [(e["actor"], e["detail"]["new"]) for e in evs] == [("cli:operator", "4")]


# --------------------------------------------------------------------- D3


def test_resolve_prefers_the_table_over_the_fallback(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    con.close()
    value, source, changed_at = mod.resolve_setting("dispatch_cap", 1, "default")
    assert (value, source) == (3, "fleet_setting")
    assert changed_at


def test_resolve_falls_back_when_the_database_is_absent(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    assert not Path(mod.DB).exists()
    value, source, changed_at = mod.resolve_setting("dispatch_cap", 7, "env")
    assert (value, source, changed_at) == (7, "env", None)


def test_resolve_falls_back_when_the_table_is_absent(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    # A database that exists but has never had fleet-config's schema applied.
    sqlite3.connect(mod.DB).close()
    value, source, changed_at = mod.resolve_setting("dispatch_cap", 9, "default")
    assert (value, source, changed_at) == (9, "default", None)


def test_resolve_falls_back_when_the_row_is_absent(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    mod.connect().close()                  # creates the schema, no rows
    value, source, changed_at = mod.resolve_setting("dispatch_cap", 5, "env")
    assert (value, source, changed_at) == (5, "env", None)


def test_resolve_falls_back_when_the_read_connection_cannot_open(tmp_path, monkeypatch):
    """A path that `os.path.exists` sees but sqlite cannot open as a database
    — a directory at that name — is the reliable way to force the read side's
    `OperationalError` without racing WAL's own not-blocked-by-writers
    guarantee (see `bin/fleet-models`'
    test_a_reader_reads_while_a_writer_holds_a_transaction, the same reason a
    real lock cannot be forced here)."""
    mod = _fc(tmp_path, monkeypatch)
    bogus = tmp_path / "not-a-database.db"
    bogus.mkdir()
    value, source, _ = mod.resolve_setting("dispatch_cap", 2, "default", db_path=bogus)
    assert (value, source) == (2, "default")


def test_get_reports_env_then_default_fallback(tmp_path, monkeypatch, capsys):
    mod = _fc(tmp_path, monkeypatch)
    monkeypatch.setenv("FLEET_WATCH_CAP", "6")
    assert mod.cmd_show(_ns(key="dispatch_cap", json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"key": "dispatch_cap", "value": 6, "source": "env", "changed_at": None}

    monkeypatch.delenv("FLEET_WATCH_CAP")
    assert mod.cmd_show(_ns(key="dispatch_cap", json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"key": "dispatch_cap", "value": 1, "source": "default", "changed_at": None}


class _ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ------------------------------------------------------- D4: ledger failure


def test_a_change_the_ledger_cannot_record_is_not_made(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    (tmp_path / "events.jsonl").mkdir()      # appending to a directory fails
    with pytest.raises(mod.LedgerAppendFailed):
        mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    assert con.execute("SELECT COUNT(*) FROM fleet_setting").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM fleet_setting_change").fetchone()[0] == 0

    (tmp_path / "events.jsonl").rmdir()
    changed, value = mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    assert changed and value == 3
    assert len(_events(tmp_path, "fleet-setting-changed")) == 1


def test_a_failed_second_write_leaves_the_first_value_intact(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    (tmp_path / "events.jsonl").unlink()
    (tmp_path / "events.jsonl").mkdir()
    with pytest.raises(mod.LedgerAppendFailed):
        mod.set_value(con, "dispatch_cap", "9", actor="cli:t")
    row = con.execute(
        "SELECT value FROM fleet_setting WHERE key='dispatch_cap'").fetchone()
    assert row == ("3",)
    assert con.execute("SELECT COUNT(*) FROM fleet_setting_change").fetchone()[0] == 1


def test_an_isolated_database_never_appends_to_another_ledger(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    mod = _fc(tmp_path, monkeypatch)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(elsewhere))
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    assert not (elsewhere / "events.jsonl").exists()
    assert len(_events(tmp_path, "fleet-setting-changed")) == 1


def test_an_in_memory_database_emits_nothing(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = sqlite3.connect(":memory:")
    con.executescript(mod.SCHEMA)
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    assert _events(tmp_path) == []


# --------------------------------------------------------- D4: reconstruction


def _collect_events(fdir, monkeypatch):
    """Run the REAL collector over `fdir`, so the `event` table is the one
    fleet-collect builds, not a hand-made copy."""
    collect = _load("fleet-collect")
    monkeypatch.setattr(collect, "DB", fdir / "fleet.db")
    monkeypatch.setattr(collect, "PROJECTS", fdir / "no-projects")
    monkeypatch.setattr(collect, "FLEET_DIR", fdir)
    monkeypatch.setattr(collect, "WORK_ROOT", fdir / "no-work-root")
    collect.main()


def test_dropping_fleet_setting_and_reconstructing_restores_the_same_rows(
        tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    mod.set_value(con, "dispatch_cap", "5", actor="cli:t")
    mod.set_value(con, "dispatch_cap", "1", actor="cli:t")   # a change back to the default
    con.close()

    _collect_events(tmp_path, monkeypatch)
    con = sqlite3.connect(tmp_path / "fleet.db")
    con.execute("DROP TABLE fleet_setting")
    con.commit()

    report = mod.reconstruct_fleet_setting(con)
    assert report["action"] == "reconstructed"
    assert con.execute(
        "SELECT key, value FROM fleet_setting").fetchall() == [("dispatch_cap", "1")]
    assert any("reconstructed" in line for line in mod.describe_reconstruction(report))


def test_reconstruction_also_restores_into_an_emptied_table(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "4", actor="cli:t")
    con.close()
    _collect_events(tmp_path, monkeypatch)
    con = sqlite3.connect(tmp_path / "fleet.db")
    con.execute("DELETE FROM fleet_setting")
    con.commit()
    assert mod.reconstruct_fleet_setting(con)["action"] == "reconstructed"
    assert con.execute(
        "SELECT value FROM fleet_setting WHERE key='dispatch_cap'").fetchone() == ("4",)


def test_a_surviving_table_is_never_changed_and_the_disagreement_is_reported(
        tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    con.close()
    _collect_events(tmp_path, monkeypatch)

    con = sqlite3.connect(tmp_path / "fleet.db")
    con.execute("UPDATE fleet_setting SET value='9' WHERE key='dispatch_cap'")
    con.commit()

    report = mod.reconstruct_fleet_setting(con)
    assert report["action"] == "compared"
    assert con.execute(
        "SELECT value FROM fleet_setting WHERE key='dispatch_cap'").fetchone() == ("9",)
    assert report["differs"] == [("dispatch_cap", "9", "3")]
    text = "\n".join(mod.describe_reconstruction(report))
    assert "ledger disagrees on dispatch_cap" in text
    assert "table left unchanged" in text


def test_a_table_that_agrees_with_the_ledger_says_nothing(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    con.close()
    _collect_events(tmp_path, monkeypatch)
    con = sqlite3.connect(tmp_path / "fleet.db")
    report = mod.reconstruct_fleet_setting(con)
    assert report["action"] == "compared"
    assert not (report["differs"] or report["ledger_only"] or report["table_only"])
    assert mod.describe_reconstruction(report) == []


def test_no_events_means_no_table_is_created(tmp_path, monkeypatch):
    _collect_events(tmp_path, monkeypatch)
    mod = _fc(tmp_path, monkeypatch)
    con = sqlite3.connect(tmp_path / "fleet.db")
    report = mod.reconstruct_fleet_setting(con)
    assert report["action"] == "none"
    assert con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='fleet_setting'").fetchone()[0] == 0


def test_an_incomplete_ledger_refuses_to_build_a_table(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    con.close()
    _collect_events(tmp_path, monkeypatch)
    con = sqlite3.connect(tmp_path / "fleet.db")
    con.execute("DROP TABLE fleet_setting")
    con.commit()
    report = mod.reconstruct_fleet_setting(con, ledger_complete=False)
    assert report["action"] == "refused"
    assert con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='fleet_setting'").fetchone()[0] == 0
    assert "NOT reconstructed" in "\n".join(mod.describe_reconstruction(report))


def test_an_unreadable_event_is_skipped_not_fatal(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    good = json.dumps({"key": "dispatch_cap", "old": None, "new": "3"})
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE event (ts, actor, type, repo, pr, sha, lease, "
                      "detail_json, source, line)")
    rows = [good, "not json", json.dumps({"key": "nosuchkey", "new": "1"}),
            json.dumps({"key": "dispatch_cap", "new": "two"}),
            json.dumps({"key": "dispatch_cap"})]
    for i, d in enumerate(rows, 1):
        con.execute("INSERT INTO event VALUES ('t','a','fleet-setting-changed',NULL,"
                    "NULL,NULL,NULL,?,'events.jsonl',?)", (d, i))
    report = mod.reconstruct_fleet_setting(con)
    assert report["action"] == "reconstructed" and report["rows"] == 1
    assert report["skipped"] == 4
    assert con.execute(
        "SELECT key, value FROM fleet_setting").fetchall() == [("dispatch_cap", "3")]


def test_fleet_collects_main_wires_the_reconstruction(tmp_path, monkeypatch):
    """The DoD's end-to-end shape: dropping the table and running the real
    `fleet-collect` (not calling `reconstruct_fleet_setting` by hand)
    restores it."""
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_value(con, "dispatch_cap", "3", actor="cli:t")
    con.close()
    _collect_events(tmp_path, monkeypatch)      # first sweep: writes the event table

    con = sqlite3.connect(tmp_path / "fleet.db")
    con.execute("DROP TABLE fleet_setting")
    con.commit()
    con.close()

    _collect_events(tmp_path, monkeypatch)      # second sweep: must reconstruct
    con = sqlite3.connect(tmp_path / "fleet.db")
    assert con.execute(
        "SELECT value FROM fleet_setting WHERE key='dispatch_cap'").fetchone() == ("3",)


# ---------------------------------------------------------- fleet-collect drop


def test_fleet_setting_never_enters_fleet_collects_drop_tuple():
    src = (BIN / "fleet-collect").read_text()
    m = re.search(r'for t in \(([^)]*)\):', src)
    assert m, "fleet-collect's drop tuple must be a literal this test can read"
    dropped = re.findall(r'"(\w+)"', m.group(1))
    assert "fleet_setting" not in dropped
    assert "fleet_setting_change" not in dropped


# ----------------------------------------------------------------------- WAL


def test_a_writer_puts_the_database_in_wal(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    mod.connect().close()
    con = sqlite3.connect(mod.DB)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()


# ------------------------------------------------------------- retry_limit (0078)


def test_retry_limit_set_writes_row_change_and_event(tmp_path, monkeypatch):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    changed, value = mod.set_value(con, "retry_limit", "3", actor="cli:t")
    assert (changed, value) == (True, 3)
    assert con.execute("SELECT value FROM fleet_setting WHERE key='retry_limit'"
                       ).fetchall() == [("3",)]
    assert con.execute("SELECT COUNT(*) FROM fleet_setting_change").fetchone()[0] == 1
    (ev,) = _events(tmp_path, "fleet-setting-changed")
    assert ev["detail"] == {"key": "retry_limit", "old": None, "new": "3"}


@pytest.mark.parametrize("bad", ["-1", "6", True, "two", "1.0", ""])
def test_retry_limit_refuses_bad_values_with_no_write(tmp_path, monkeypatch, bad):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    with pytest.raises(ValueError):
        mod.set_value(con, "retry_limit", bad, actor="cli:t")
    assert con.execute("SELECT COUNT(*) FROM fleet_setting").fetchone()[0] == 0
    assert _events(tmp_path) == []


@pytest.mark.parametrize("ok", ["0", "5"])
def test_retry_limit_accepts_the_bounds(tmp_path, monkeypatch, ok):
    mod = _fc(tmp_path, monkeypatch)
    con = mod.connect()
    assert mod.set_value(con, "retry_limit", ok, actor="cli:t")[1] == int(ok)


def test_retry_limit_get_json_defaults_to_2(tmp_path, monkeypatch, capsys):
    mod = _fc(tmp_path, monkeypatch)
    monkeypatch.delenv("FLEET_RETRY_LIMIT", raising=False)
    assert mod.cmd_show(_ns(key="retry_limit", json=True)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "key": "retry_limit", "value": 2, "source": "default", "changed_at": None}
