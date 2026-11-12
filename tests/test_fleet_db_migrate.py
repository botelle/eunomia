"""fleet-db-migrate (plan 0096): every owner's tables exist before a reader asks."""
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def _run(tool, db, *args, fleet_dir=None):
    env = dict(os.environ, FLEET_DB=str(db), EUNOMIA_FLEET_DIR=str(fleet_dir or db.parent))
    return subprocess.run([sys.executable, str(tool), *args], capture_output=True,
                          text=True, env=env)


def _master(db):
    con = sqlite3.connect(db)
    try:
        return sorted(con.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
    finally:
        con.close()


def _names(db):
    return {n for t, n, _ in _master(db) if t == "table"}


def _copy_bin(tmp_path):
    dst = tmp_path / "bin"
    shutil.copytree(BIN, dst)
    return dst


def test_fresh_db_gets_every_owner_table(tmp_path):
    db = tmp_path / "fleet.db"
    r = _run(BIN / "fleet-db-migrate", db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("ok fleet-") == 6   # plan 0188 adds fleet-plan-review
    assert "lane_run" in _names(db)
    assert {"fleet_setting", "repo_setting", "repo_setting_change", "repo_model",
            "repo_model_change", "review", "model_price"} <= _names(db)


def test_discriminating_pair_repo_setting_read(tmp_path):
    db = tmp_path / "fleet.db"
    sqlite3.connect(db).close()
    get = [str(BIN / "fleet-config"), "get", "review_bar", "--repo", "operator/x", "--json"]
    env = dict(os.environ, FLEET_DB=str(db), EUNOMIA_FLEET_DIR=str(tmp_path))
    before = subprocess.run([sys.executable, *get], capture_output=True, text=True, env=env)
    assert before.returncode != 0
    assert "no such table: repo_setting" in before.stdout + before.stderr
    assert _run(BIN / "fleet-db-migrate", db).returncode == 0
    after = subprocess.run([sys.executable, *get], capture_output=True, text=True, env=env)
    assert after.returncode == 0, after.stdout + after.stderr
    assert "builtin" in after.stdout and "high" in after.stdout


def test_idempotent(tmp_path):
    db = tmp_path / "fleet.db"
    _run(BIN / "fleet-db-migrate", db)
    first = _master(db)
    assert _run(BIN / "fleet-db-migrate", db).returncode == 0
    assert _master(db) == first


def test_data_survives(tmp_path):
    db = tmp_path / "fleet.db"
    _run(BIN / "fleet-db-migrate", db)
    con = sqlite3.connect(db)
    rs = con.execute("SELECT sql FROM sqlite_master WHERE name='repo_setting'").fetchone()[0]
    cols = [r[1] for r in con.execute("PRAGMA table_info(repo_setting)")]
    rv = [r[1] for r in con.execute("PRAGMA table_info(review)")]
    con.execute(f"INSERT INTO repo_setting ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [f"v{i}" for i in range(len(cols))])
    con.execute(f"INSERT INTO review ({','.join(rv)}) VALUES ({','.join('?' * len(rv))})",
                [i for i in range(len(rv))])
    con.commit()
    want = (con.execute("SELECT * FROM repo_setting").fetchall(),
            con.execute("SELECT * FROM review").fetchall())
    con.close()
    assert rs
    assert _run(BIN / "fleet-db-migrate", db).returncode == 0
    con = sqlite3.connect(db)
    got = (con.execute("SELECT * FROM repo_setting").fetchall(),
           con.execute("SELECT * FROM review").fetchall())
    assert got == want and got[0] and got[1]


def test_new_owner_table_is_migrated_with_no_change_here(tmp_path):
    b = _copy_bin(tmp_path)
    cfg = b / "fleet-config"
    text = cfg.read_text()
    assert 'SCHEMA = """' in text
    cfg.write_text(text.replace(
        'SCHEMA = """', 'SCHEMA = """\nCREATE TABLE IF NOT EXISTS brand_new_thing (k TEXT);\n', 1))
    db = tmp_path / "fleet.db"
    r = _run(b / "fleet-db-migrate", db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "brand_new_thing" in _names(db)
    assert (b / "fleet-db-migrate").read_text() == (BIN / "fleet-db-migrate").read_text()


def test_dry_run_lists_and_changes_nothing(tmp_path):
    db = tmp_path / "fleet.db"
    sqlite3.connect(db).close()
    before = _master(db)
    r = _run(BIN / "fleet-db-migrate", db, "--dry-run")
    assert r.returncode == 0
    assert "repo_setting" in r.stdout and "review" in r.stdout
    assert _master(db) == before == []


def test_dry_run_on_missing_db_creates_nothing(tmp_path):
    db = tmp_path / "sub" / "fleet.db"
    assert _run(BIN / "fleet-db-migrate", db, "--dry-run").returncode == 0
    assert not db.exists()


def test_a_failing_owner_fails_the_run_but_others_apply(tmp_path):
    b = _copy_bin(tmp_path)
    cfg = b / "fleet-models"
    cfg.write_text(cfg.read_text().replace(
        'CREATE TABLE IF NOT EXISTS repo_model_change (',
        'CREATE TABLE broken (;\nCREATE TABLE IF NOT EXISTS repo_model_change (', 1))
    db = tmp_path / "fleet.db"
    r = _run(b / "fleet-db-migrate", db)
    assert r.returncode != 0
    assert "FAILED fleet-models:" in r.stdout
    assert "ok fleet-config" in r.stdout and "ok fleet-reviews" in r.stdout
    assert {"repo_setting", "review"} <= _names(db)


def test_collect_user_version_untouched(tmp_path):
    db = tmp_path / "fleet.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=7")
    con.commit()
    con.close()
    assert _run(BIN / "fleet-db-migrate", db).returncode == 0
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 7
