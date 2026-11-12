"""Tests for the `followup_triage` seat (plan 0186).

A singleton position in `bin/fleet-models`, independently configurable from
`mediator`, resolved per repo by `bin/fleet-followups`. The plan's own
amendment drops the edge to `mediator` plan 0185 would have tied this seat
with: `followup_triage` is a seat of its own, with its own write path, its
own audit history and its own confined-reader gate.
"""
import importlib.machinery
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
REPO = "operator/thing"


def _load(name, mod_name=None):
    mod_name = mod_name or name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    fdir = tmp_path / "fleet"
    fdir.mkdir()
    monkeypatch.setenv("FLEET_DB", str(fdir / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(fdir))
    return fdir


def _fm(fleet):
    return _load("fleet-models", "fleet_models_fts")


def _followups(fleet):
    return _load("fleet-followups", "fleet_followups_fts")


def _events(fdir):
    p = fdir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# -------------------------------------------- the seat itself (bin/fleet-models)


def test_followup_triage_is_a_singleton_non_reviewer_position(fleet):
    mod = _fm(fleet)
    assert "followup_triage" in mod.POSITIONS
    assert "followup_triage" in mod.SINGLETON
    assert "followup_triage" not in mod.REVIEWER_POSITIONS
    con = mod.connect()
    assert mod.set_runner(con, "default", "followup_triage", "sub-sonnet") == (1, True)
    con.close()


def test_mediator_and_followup_triage_are_independent_seats(fleet):
    """Setting one leaves the other exactly where it was — two positions, two
    rows, no shared state. This is the edge plan 0185 would have tied them
    with, and the plan's amendment drops it."""
    mod = _fm(fleet)
    con = mod.connect()
    mod.set_runner(con, REPO, "mediator", "sub-opus")
    mod.set_runner(con, REPO, "followup_triage", "sub-sonnet")
    assert mod.resolve(con, REPO, "mediator") == ("sub-opus", "repo")
    assert mod.resolve(con, REPO, "followup_triage") == ("sub-sonnet", "repo")
    mod.disable(con, REPO, "followup_triage")
    assert mod.resolve(con, REPO, "mediator") == ("sub-opus", "repo")
    assert mod.resolve(con, REPO, "followup_triage") == (None, None)
    con.close()


# -------------------------------------------- an existing database upgrades safely


OLD_REPO_MODEL = """
CREATE TABLE repo_model (
  repo      TEXT    NOT NULL,
  position  TEXT    NOT NULL,
  ordinal   INTEGER NOT NULL DEFAULT 1,
  runner    TEXT    NOT NULL,
  disabled  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (repo, position, ordinal),
  CHECK (position IN ('implementer','plan_reviewer','code_reviewer',
                      'tester','mediator','impl_supervisor','test_supervisor',
                      'style_reviewer','lane_reviewer')),
  CHECK (ordinal >= 1),
  CHECK (disabled IN (0,1))
);
"""


def test_a_lane_reviewer_era_database_upgrades_safely_and_twice_is_a_noop(fleet):
    db = fleet / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_REPO_MODEL)
    con.executemany("INSERT INTO repo_model VALUES (?,?,?,?,?)", [
        ("default", "mediator", 1, "sub-opus", 0),
        ("operator/x", "implementer", 1, "sub-sonnet", 1)])
    con.commit()
    con.close()

    mod = _fm(fleet)
    con = mod.connect()
    assert con.execute("SELECT COUNT(*) FROM repo_model").fetchone()[0] == 2
    assert con.execute(
        "SELECT * FROM repo_model WHERE repo='default' AND position='mediator'"
    ).fetchone() == ("default", "mediator", 1, "sub-opus", 0)
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='repo_model'").fetchone()[0]
    assert "'followup_triage'" in sql
    ordinal, changed = mod.set_runner(con, "operator/x", "followup_triage", "sub-sonnet")
    assert (ordinal, changed) == (1, True)
    change = con.execute(
        "SELECT position, old_runner, new_runner FROM repo_model_change "
        "WHERE position='followup_triage'").fetchone()
    assert change == ("followup_triage", None, "sub-sonnet")
    before = con.execute("SELECT sql FROM sqlite_master WHERE name='repo_model'").fetchone()
    con.close()

    # idempotent: a second connect() neither rebuilds again nor loses a row
    con = mod.connect()
    assert con.execute("SELECT sql FROM sqlite_master WHERE name='repo_model'").fetchone() == before
    assert con.execute("SELECT COUNT(*) FROM repo_model").fetchone()[0] == 3
    assert not con.execute(
        "SELECT 1 FROM sqlite_master WHERE name='repo_model_rebuild'").fetchone()
    con.close()


def test_fleet_db_migrate_widens_an_existing_database(fleet):
    import os
    db = fleet / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_REPO_MODEL)
    con.execute("INSERT INTO repo_model VALUES ('default','mediator',1,'sub-opus',0)")
    con.commit()
    con.close()
    env = dict(os.environ, FLEET_DB=str(db), EUNOMIA_FLEET_DIR=str(fleet))
    r = subprocess.run([sys.executable, str(BIN / "fleet-db-migrate")], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    con = sqlite3.connect(db)
    assert "'followup_triage'" in con.execute(
        "SELECT sql FROM sqlite_master WHERE name='repo_model'").fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM repo_model").fetchone()[0] == 1
    con.close()


# -------------------------------------------- audit history (ledger durability)


def test_a_followup_triage_write_appends_a_ledger_event_and_a_change_row(fleet):
    mod = _fm(fleet)
    con = mod.connect()
    mod.set_runner(con, REPO, "followup_triage", "sub-sonnet", actor="cli:t")
    mod.set_runner(con, REPO, "followup_triage", "sub-opus", actor="cli:t")
    mod.disable(con, REPO, "followup_triage", actor="cli:t")
    con.close()

    events = [e for e in _events(fleet) if e["type"] == "repo-model-changed"]
    assert [e["detail"]["position"] for e in events] == ["followup_triage"] * 3
    assert [e["detail"]["new_runner"] for e in events] == ["sub-sonnet", "sub-opus", None]

    con = sqlite3.connect(fleet / "fleet.db")
    changes = con.execute(
        "SELECT old_runner, new_runner, old_disabled, new_disabled FROM repo_model_change "
        "WHERE position='followup_triage' ORDER BY ts, id").fetchall()
    assert changes == [(None, "sub-sonnet", None, 0), ("sub-sonnet", "sub-opus", None, None),
                       ("sub-opus", None, 0, 1)]
    con.close()


# -------------------------------------------- resolution (bin/fleet-followups)


def test_the_claude_default_still_triages_when_the_seat_is_unset(fleet):
    mod = _followups(fleet)
    runner = mod.resolve_runner(repo=REPO)
    assert runner.name == mod.DEFAULT_RUNNER == "sub-sonnet"


def test_a_configured_claude_seat_overrides_the_default(fleet):
    fm = _fm(fleet)
    con = fm.connect()
    fm.set_runner(con, REPO, "followup_triage", "sub-opus")
    con.close()
    mod = _followups(fleet)
    runner = mod.resolve_runner(repo=REPO)
    assert runner.name == "sub-opus"


def test_a_non_claude_seat_is_refused_naming_the_missing_sandbox(fleet):
    fm = _fm(fleet)
    con = fm.connect()
    fm.set_runner(con, REPO, "followup_triage", "sub-codex")
    con.close()
    mod = _followups(fleet)
    with pytest.raises(mod.Refused, match="confined-reader sandbox"):
        mod.resolve_runner(repo=REPO)


def test_the_env_override_still_sits_above_the_seat(fleet, monkeypatch):
    fm = _fm(fleet)
    con = fm.connect()
    fm.set_runner(con, REPO, "followup_triage", "sub-opus")
    con.close()
    monkeypatch.setenv("FLEET_FOLLOWUP_RUNNER", "sub-sonnet")
    mod = _followups(fleet)
    runner = mod.resolve_runner(repo=REPO)
    assert runner.name == "sub-sonnet"


def test_an_unconfigured_table_resolves_to_the_prior_default(fleet):
    """No fleet-db-migrate has run yet: `repo_model` does not exist at all.
    `NotConfigured`, not a Refused — an installation that never adopted the
    seat keeps behaving exactly as it did before it existed."""
    mod = _followups(fleet)
    (fleet / "fleet.db").unlink(missing_ok=True)
    runner = mod.resolve_runner(repo=REPO)
    assert runner.name == "sub-sonnet"
