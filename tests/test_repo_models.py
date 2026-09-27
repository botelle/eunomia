"""Tests for bin/fleet-models and bin/orchestrator's use of it (plan 0048).

`repo_model` / `repo_model_change` join `review` / `model_price` as durable,
non-derived tables inside `fleet.db` (bin/fleet-reviews is the precedent).
`bin/fleet-models` is the one writer; `bin/orchestrator` only ever reads, via
`resolve_repo`/`resolved_implementer_env`.
"""
import importlib.machinery
import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, mod_name=None):
    mod_name = mod_name or name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _fm(tmp_path, monkeypatch, db_name="fleet.db"):
    """A fleet-models module reading an isolated tmp `FLEET_DB`.

    An env var, not a post-load `mod.DB = ...` assignment: `bin/orchestrator`
    loads its OWN fresh copy of this module via `_load_sibling` — a distinct
    module object that only agrees on the db path if both read it from the
    same place, and an env var is process-global where an attribute on one
    copy is not."""
    monkeypatch.setenv("FLEET_DB", str(tmp_path / db_name))
    return _load("fleet-models")


# ------------------------------------------------------------------ D1 / D5


def test_set_writes_row_and_one_change_and_get_reports_layer(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    con = mod.connect()
    ordinal, changed = mod.set_runner(con, "operator/eunomia", "implementer",
                                      "sub-opus", actor="cli:t")
    assert changed and ordinal == 1
    row = con.execute("SELECT runner, disabled FROM repo_model WHERE "
                      "repo='operator/eunomia' AND position='implementer'").fetchone()
    assert row == ("sub-opus", 0)
    changes = con.execute("SELECT old_runner, new_runner FROM repo_model_change"
                          ).fetchall()
    assert changes == [(None, "sub-opus")]

    name, layer = mod.resolve(con, "operator/eunomia", "implementer")
    assert (name, layer) == ("sub-opus", "repo")


def test_repo_with_no_row_falls_back_to_default_and_missing_default_stops(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    con = mod.connect()
    assert mod.resolve(con, "operator/ares", "implementer") == (None, None)

    mod.set_runner(con, "default", "implementer", "sub-sonnet", actor="cli:t")
    assert mod.resolve(con, "operator/ares", "implementer") == ("sub-sonnet", "default")

    mod.disable(con, "default", "implementer", 1, actor="cli:t")
    assert mod.resolve(con, "operator/ares", "implementer") == (None, None), (
        "a disabled default must not silently keep resolving")


# --------------------------------------------------------------------- D2


def test_disable_leaves_row_present_and_next_set_skips_the_ordinal(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_runner(con, "operator/eunomia", "tester", "sub-sonnet")
    mod.set_runner(con, "operator/eunomia", "tester", "sub-opus")
    ordinals = [r[0] for r in con.execute(
        "SELECT ordinal FROM repo_model WHERE repo='operator/eunomia' AND "
        "position='tester' ORDER BY ordinal")]
    assert ordinals == [1, 2]

    changed = mod.disable(con, "operator/eunomia", "tester", 2, actor="cli:t")
    assert changed
    row = con.execute(
        "SELECT runner, disabled FROM repo_model WHERE repo='operator/eunomia' AND "
        "position='tester' AND ordinal=2").fetchone()
    assert row == ("sub-opus", 1), "the row is soft-deleted, never removed"

    new_ordinal, _ = mod.set_runner(con, "operator/eunomia", "tester", "sub-sonnet")
    assert new_ordinal == 3, "ordinal 2 must never be reused"


# --------------------------------------------------------------------- D3


def test_env_override_wins_over_repo_and_default(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_runner(con, "operator/ares", "implementer", "sub-sonnet")
    con.close()

    orch = _load("orchestrator")
    env = orch.resolved_implementer_env("operator/ares", env={"EUNOMIA_IMPL_RUNNER": "sub-opus"})
    assert env["EUNOMIA_IMPL_RUNNER"] == "sub-opus"


def test_orchestrator_resolves_repo_then_default_then_stops(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    orch = _load("orchestrator")

    con = mod.connect()
    mod.set_runner(con, "default", "implementer", "sub-sonnet")
    con.close()
    env = orch.resolved_implementer_env("operator/ares", env={})
    assert env["EUNOMIA_IMPL_RUNNER"] == "sub-sonnet"

    con = mod.connect()
    mod.set_runner(con, "operator/ares", "implementer", "sub-opus")
    con.close()
    env = orch.resolved_implementer_env("operator/ares", env={})
    assert env["EUNOMIA_IMPL_RUNNER"] == "sub-opus", "the repo's own row beats 'default'"

    con = mod.connect()
    mod.disable(con, "default", "implementer", 1)
    mod.disable(con, "operator/ares", "implementer", 1)
    con.close()
    with pytest.raises(orch.Stop) as e:
        orch.resolved_implementer_env("operator/ares", env={})
    assert "operator/ares" in e.value.reason


def test_an_unconfigured_install_falls_back_to_default_runner(tmp_path, monkeypatch):
    """No fleet.db at all — plan 0048 has not been adopted here yet — must not
    Stop the whole fleet; route_implementer's own DEFAULT_RUNNER applies."""
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "never-created" / "fleet.db"))
    orch = _load("orchestrator")
    env = orch.resolved_implementer_env("operator/ares", env={})
    assert "EUNOMIA_IMPL_RUNNER" not in env
    route = orch.route_implementer({"zone": "public"}, orch.resolve_credential(), env=env)
    assert route.name == orch.DEFAULT_RUNNER


# --------------------------------------------------------------------- D4


def test_two_sets_produce_two_changes_and_a_noop_set_produces_none(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_runner(con, "operator/eunomia", "implementer", "sub-sonnet")
    mod.set_runner(con, "operator/eunomia", "implementer", "sub-opus")
    assert con.execute("SELECT COUNT(*) FROM repo_model_change").fetchone()[0] == 2

    _ordinal, changed = mod.set_runner(con, "operator/eunomia", "implementer", "sub-opus")
    assert changed is False
    assert con.execute("SELECT COUNT(*) FROM repo_model_change").fetchone()[0] == 2


# --------------------------------------------------------- runner validation


def test_set_with_unknown_runner_is_refused_at_write_time(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    con = mod.connect()
    with pytest.raises(mod.UnknownRunner) as e:
        mod.set_runner(con, "operator/eunomia", "implementer", "sub-gpt")
    assert "sub-gpt" in str(e.value)
    assert con.execute("SELECT COUNT(*) FROM repo_model").fetchone()[0] == 0


def test_a_runner_retired_from_runners_is_refused_at_read_time(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    con = mod.connect()
    mod.set_runner(con, "operator/eunomia", "implementer", "sub-opus")
    # A runner can leave RUNNERS after the row was written — simulate that by
    # writing a name straight past validation, the way a retired registry
    # entry would look to a later read.
    con.execute("UPDATE repo_model SET runner='sub-gpt' WHERE repo='operator/eunomia'")
    con.commit()
    with pytest.raises(mod.UnknownRunner):
        mod.resolve(con, "operator/eunomia", "implementer")


# ------------------------------------------------------------------- CLI


def test_cli_set_get_disable_enable_round_trip(tmp_path, monkeypatch, capsys):
    mod = _fm(tmp_path, monkeypatch)
    assert mod.main(["set", "operator/eunomia", "implementer", "sub-opus"]) == 0
    out = capsys.readouterr().out
    assert "sub-opus" in out

    assert mod.main(["get", "operator/eunomia"]) == 0
    out = capsys.readouterr().out
    assert "sub-opus" in out and "[repo]" in out

    assert mod.main(["disable", "operator/eunomia", "implementer"]) == 0
    assert mod.main(["get", "operator/eunomia"]) == 0
    out = capsys.readouterr().out
    assert "implementer" in out

    assert mod.main(["enable", "operator/eunomia", "implementer"]) == 0
    con = mod.connect()
    assert con.execute("SELECT disabled FROM repo_model WHERE position='implementer'"
                       ).fetchone()[0] == 0


def test_cli_set_refuses_a_position_outside_adr0009(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        mod.main(["set", "operator/eunomia", "fleet-watch", "sub-sonnet"])


# ------------------------------------------------------------- DDL portability


def test_schema_ddl_is_portable(tmp_path, monkeypatch):
    mod = _fm(tmp_path, monkeypatch)
    ddl = mod.SCHEMA.upper()
    for forbidden in ("AUTOINCREMENT", "SERIAL", "BOOLEAN"):
        assert forbidden not in ddl, f"{forbidden} is not portable SQL"
    # No SQLite-only function (e.g. julianday, strftime) in the DDL itself.
    for sqlite_only in ("JULIANDAY(", "STRFTIME(", "RANDOMBLOB("):
        assert sqlite_only not in ddl


# --------------------------------------------------------- fleet-collect drop


def test_repo_model_never_enters_fleet_collects_drop_tuple():
    src = (BIN / "fleet-collect").read_text()
    m = re.search(r'for t in \(([^)]*)\):', src)
    assert m, "fleet-collect's drop tuple must be a literal this test can read"
    dropped = re.findall(r'"(\w+)"', m.group(1))
    assert "repo_model" not in dropped
    assert "repo_model_change" not in dropped


def test_repo_model_tables_survive_a_schema_version_bump(tmp_path, monkeypatch):
    collect = _load("fleet-collect")
    fm = _fm(tmp_path, monkeypatch)
    db = tmp_path / "fleet.db"

    con = sqlite3.connect(db)
    con.executescript(collect.SCHEMA)
    con.executescript(fm.SCHEMA)
    con.execute(
        "INSERT INTO repo_model (repo, position, ordinal, runner) VALUES "
        "('default', 'implementer', 1, 'sub-sonnet')")
    con.execute(
        "INSERT INTO repo_model_change (id, ts, actor, source, repo, position, "
        "ordinal, old_runner, new_runner, old_disabled, new_disabled) VALUES "
        "('01TESTULID000000000000000', '2026-09-14T00:00:00Z', 'cli:t', "
        "'fleet-models', 'default', 'implementer', 1, NULL, 'sub-sonnet', NULL, 0)")
    con.commit()
    # Simulate a DB written by an OLDER collector: `has_session` must be true
    # for fleet-collect to take the rebuild path at all.
    con.execute(f"PRAGMA user_version = {collect.SCHEMA_VERSION - 1}")
    con.commit()
    con.close()

    monkeypatch.setattr(collect, "DB", db)
    monkeypatch.setattr(collect, "PROJECTS", tmp_path / "no-projects")
    monkeypatch.setattr(collect, "FLEET_DIR", tmp_path / "no-fleet-dir")
    monkeypatch.setattr(collect, "WORK_ROOT", tmp_path / "no-work-root")
    collect.main()

    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == collect.SCHEMA_VERSION
    assert con.execute("SELECT COUNT(*) FROM repo_model").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM repo_model_change").fetchone()[0] == 1


# --------------------------------------------------------------- docs (D7)


def test_docs_describe_two_tiers_and_name_the_drop_list():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "fleet-db.md").read_text()
    assert "repo_model" in doc
    assert "session" in doc and "dispatch_phase" in doc
    assert "derived" in doc.lower() and "durable" in doc.lower()
