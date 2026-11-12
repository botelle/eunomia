"""Plan 0180 D1/D4: an owner attestation satisfying `external:<slug>`, read
from `dependency_attestation`; and `bin/fleet-depsd`, that table's owner with
no write path of its own in this plan."""
import argparse
import importlib.machinery
import importlib.util
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import test_fleet_plan as tp
import test_fleet_watch as tw
from test_plan_dependencies import _plan_with_deps

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def _load_depsd():
    loader = importlib.machinery.SourceFileLoader("fleet_depsd", str(BIN / "fleet-depsd"))
    spec = importlib.util.spec_from_loader("fleet_depsd", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


DEPSD = _load_depsd()


def _migrated(db_path):
    con = sqlite3.connect(db_path)
    con.executescript(DEPSD.SCHEMA)
    con.commit()
    return con


def _attest(con, slug, actor="cli:operator", note=None):
    now = time.time()
    con.execute("INSERT INTO dependency_attestation (slug, note, created_at, updated_at) "
                "VALUES (?,?,?,?)", (slug, note, now, now))
    con.execute(
        "INSERT INTO dependency_attestation_change (id, slug, action, note, actor, source, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"01TEST{slug[:8]}", slug, "attest", note, actor, "test", now, now))
    con.commit()


def _revoke(con, slug):
    con.execute("DELETE FROM dependency_attestation WHERE slug=?", (slug,))
    con.commit()


# --------------------------------------------------------------- fleet-watch

def _watch(tmp_path, **env):
    return tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff", **env)


def test_unattested_slug_is_unmet(tmp_path):
    db = tmp_path / "fleet" / "fleet.db"
    db.parent.mkdir(parents=True)
    _migrated(db).close()
    mod = _watch(tmp_path)
    state, reason, detail = mod._attestation_check("cihost-caddy")
    assert state == "unmet" and reason == "unattested"


def test_a_fixture_row_makes_it_met_on_the_next_call(tmp_path):
    db = tmp_path / "fleet" / "fleet.db"
    db.parent.mkdir(parents=True)
    con = _migrated(db)
    mod = _watch(tmp_path)
    assert mod._attestation_check("cihost-caddy")[0] == "unmet"
    _attest(con, "cihost-caddy")
    con.close()
    assert mod._attestation_check("cihost-caddy")[0] == "met"


def test_deleting_the_row_makes_it_unmet_again_uncached(tmp_path):
    db = tmp_path / "fleet" / "fleet.db"
    db.parent.mkdir(parents=True)
    con = _migrated(db)
    _attest(con, "cihost-caddy")
    mod = _watch(tmp_path)
    assert mod._attestation_check("cihost-caddy")[0] == "met"
    _revoke(con, "cihost-caddy")
    con.close()
    assert mod._attestation_check("cihost-caddy")[0] == "unmet"


def test_free_text_is_unmet_even_with_a_matching_row(tmp_path):
    db = tmp_path / "fleet" / "fleet.db"
    db.parent.mkdir(parents=True)
    con = _migrated(db)
    text = "the owner ran the pilot and it worked"
    _attest(con, text)
    con.close()
    mod = _watch(tmp_path)
    block = mod._plan_dependency_block(
        "operator/sniff", [f"external:{text}"], {}, "t")
    assert block is not None
    dep, form, reason, detail = block
    assert form == "external" and reason == "not a slug"


def test_missing_database_is_store_unreadable_never_met(tmp_path):
    mod = _watch(tmp_path)
    state, reason, _ = mod._attestation_check("cihost-caddy")
    assert state == "unmet" and reason == "store unreadable"


def test_missing_table_is_store_unreadable_never_met(tmp_path):
    db = tmp_path / "fleet" / "fleet.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    mod = _watch(tmp_path)
    state, reason, _ = mod._attestation_check("cihost-caddy")
    assert state == "unmet" and reason == "store unreadable"


def test_fleet_db_override_is_honoured(tmp_path):
    default_db = tmp_path / "fleet" / "fleet.db"
    default_db.parent.mkdir(parents=True)
    _migrated(default_db).close()
    other_db = tmp_path / "elsewhere.db"
    con = _migrated(other_db)
    _attest(con, "elsewhere-slug")
    con.close()
    mod = _watch(tmp_path, FLEET_DB=str(other_db))
    assert mod._attestation_check("elsewhere-slug")[0] == "met"
    # the default path has no row for this slug — proof the override, not
    # the default, was actually read
    assert mod._attestation_check("cihost-caddy")[0] == "unmet"


def test_a_valid_slug_dispatches_through_the_full_block(tmp_path):
    db = tmp_path / "fleet" / "fleet.db"
    db.parent.mkdir(parents=True)
    con = _migrated(db)
    _attest(con, "cihost-caddy")
    con.close()
    mod = _watch(tmp_path)
    assert mod._plan_dependency_block(
        "operator/sniff", ["external:cihost-caddy"], {}, "t") is None


# ----------------------------------------------------------------- fleet-depsd

def test_no_attest_or_revoke_subcommand():
    for argv in (["attest", "x"], ["revoke", "x"]):
        with pytest.raises(SystemExit) as e:
            DEPSD.main(argv)
        assert e.value.code == 2


def test_attestations_lists_current_rows_with_latest_actor(tmp_path, capsys):
    db = tmp_path / "fleet.db"
    con = _migrated(db)
    _attest(con, "cihost-caddy", actor="cli:operator")
    con.execute(
        "INSERT INTO dependency_attestation_change (id, slug, action, note, actor, source, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("01TESTB", "cihost-caddy", "attest", None, "cli:later", "test",
         time.time() + 10, time.time() + 10))
    con.commit()
    con.close()
    rows = DEPSD.attestations(str(db))
    assert len(rows) == 1
    assert rows[0]["slug"] == "cihost-caddy"
    assert rows[0]["actor"] == "cli:later"


def test_attestations_writes_nothing(tmp_path):
    db = tmp_path / "fleet.db"
    con = _migrated(db)
    _attest(con, "cihost-caddy")
    con.close()
    before_mtime = db.stat().st_mtime_ns
    before_rows = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM dependency_attestation").fetchone()[0]
    r = subprocess.run([sys.executable, str(BIN / "fleet-depsd"), "attestations"],
                       capture_output=True, text=True,
                       env={"FLEET_DB": str(db), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert db.stat().st_mtime_ns == before_mtime
    after_rows = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM dependency_attestation").fetchone()[0]
    assert after_rows == before_rows


def test_attestations_on_missing_store_is_an_error(tmp_path):
    db = tmp_path / "nope.db"
    assert DEPSD.cmd_attestations(
        argparse.Namespace(json=False, db=str(db))) == 1


# --------------------------------------------------------------- fleet-db-migrate

def test_fleet_db_migrate_creates_both_tables_with_timestamp_columns(tmp_path):
    db = tmp_path / "fleet.db"
    r = subprocess.run([sys.executable, str(BIN / "fleet-db-migrate")],
                       capture_output=True, text=True,
                       env={"FLEET_DB": str(db), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    con = sqlite3.connect(db)
    for table in ("dependency_attestation", "dependency_attestation_change"):
        cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        assert {"created_at", "updated_at"} <= cols, (table, cols)


# --------------------------------------------------------------------- lint

def test_lint_free_text_external_is_error_at_ready(tmp_path):
    p = _plan_with_deps(
        tmp_path, "0009-a.md", "0009-a", "ready",
        'depends_on: ["external:the owner ran the pilot and it worked"]')
    errs = tp._load().lint(p)
    assert any("not a slug" in e for e in errs), errs


def test_lint_free_text_external_is_error_at_draft(tmp_path):
    p = _plan_with_deps(
        tmp_path, "0009-a.md", "0009-a", "draft",
        'depends_on: ["external:the owner ran the pilot and it worked"]')
    errs = tp._load().lint(p)
    assert any("not a slug" in e for e in errs), errs


def test_lint_free_text_external_is_not_checked_at_done(tmp_path):
    p = _plan_with_deps(
        tmp_path, "0009-a.md", "0009-a", "done",
        'depends_on: ["external:the owner ran the pilot and it worked"]')
    errs = tp._load().lint(p)
    assert not any("not a slug" in e for e in errs), errs


def test_lint_valid_slug_is_not_an_error(tmp_path):
    p = _plan_with_deps(
        tmp_path, "0009-a.md", "0009-a", "ready",
        'depends_on: ["external:cihost-caddy"]')
    errs = tp._load().lint(p)
    assert not any("not a slug" in e for e in errs), errs
