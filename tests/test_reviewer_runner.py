"""Tests for the reviewer positions' Fable-only rule (plan 0065).

`plan_reviewer` and `code_reviewer` accept a runner only if its `family` is
"fable" — the 2026-08-26 "Fable on every PR, hard stop" ruling, which used to be
a comment above `MODEL` in ~/agent/dispatch-review.py and is now code that
refuses. Enforced at write (`set_runner`) and at read (`resolve`).
"""
import importlib.machinery
import importlib.util
import sqlite3
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

REVIEWERS = ("plan_reviewer", "code_reviewer")
NOT_REVIEWERS = ("implementer", "tester", "mediator", "impl_supervisor",
                 "test_supervisor")


def _load(name, mod_name=None):
    mod_name = mod_name or name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def fm(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    return _load("fleet-models")


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    return _load("orchestrator")


def _registry(fm, monkeypatch, *runners):
    """Replace the registry `fleet-models` validates against with exactly these
    `orch.Runner`s — so the rule is tested against a runner's `family`, with
    names chosen to be misleading."""
    orch = fm._load_orchestrator()
    reg = {r.name: r for r in runners}
    monkeypatch.setattr(fm, "_load_orchestrator",
                        lambda: types.SimpleNamespace(RUNNERS=reg, Runner=orch.Runner))
    return orch.Runner


def _runner(Runner, name, family):
    return Runner(name, ("claude",), False, "local_cli", (), "", family)


# ------------------------------------------------------------------ D1


def test_sub_fable_is_in_the_registry_and_pins_the_exact_model_id(orch):
    r = orch.RUNNERS["sub-fable"]
    assert r.family == "fable"
    assert r.needs == "local_cli" and not r.local and not r.unwired
    argv = list(r.argv)
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    # The two spellings the catalog rejects, and the alias that would let the
    # model move without a decision — none of them may be what is pinned.
    assert not {"claude-fable-5.1", "claude-fable-51", "fable"} & set(argv)


def test_sub_fable_has_the_shape_of_sub_opus(orch):
    fable, opus = orch.RUNNERS["sub-fable"], orch.RUNNERS["sub-opus"]
    strip = lambda r: tuple(a for a in r.argv if a not in ("claude-fable-5-1", "opus"))
    assert strip(fable) == strip(opus)
    assert (fable.local, fable.needs, fable.extra_env, fable.unwired) == \
           (opus.local, opus.needs, opus.extra_env, opus.unwired)


def test_only_sub_fable_is_in_the_fable_family(orch):
    assert sorted(n for n, r in orch.RUNNERS.items() if r.family == "fable") == \
        ["sub-fable"]


# ------------------------------------------------------------------ D2


@pytest.mark.parametrize("position", REVIEWERS)
def test_a_reviewer_position_accepts_sub_fable_and_reads_it_back_as_repo(fm, position):
    con = fm.connect()
    ordinal, changed = fm.set_runner(con, "operator/eunomia", position, "sub-fable",
                                     actor="cli:t")
    assert changed and ordinal == 1
    assert fm.resolve(con, "operator/eunomia", position) == ("sub-fable", "repo")


@pytest.mark.parametrize("position", REVIEWERS)
@pytest.mark.parametrize("runner", ["sub-sonnet", "sub-opus", "api-sonnet",
                                    "local-qwen"])
def test_a_reviewer_position_refuses_a_non_fable_runner_and_writes_nothing(
        fm, position, runner):
    con = fm.connect()
    with pytest.raises(fm.UnknownRunner) as e:
        fm.set_runner(con, "operator/eunomia", position, runner, actor="cli:t")
    msg = str(e.value)
    assert "2026-08-26" in msg and "Fable on every PR" in msg
    assert "sub-fable" in msg, "the refusal names what would be accepted"
    assert con.execute("SELECT COUNT(*) FROM repo_model").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM repo_model_change").fetchone()[0] == 0


def test_the_cli_refusal_exits_nonzero_and_says_why(fm, capsys):
    rc = fm.main(["set", "operator/eunomia", "code_reviewer", "sub-sonnet"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "2026-08-26" in err and "sub-fable" in err
    assert fm.main(["set", "operator/eunomia", "code_reviewer", "sub-fable"]) == 0
    assert fm.main(["get", "operator/eunomia"]) == 0
    assert "sub-fable" in capsys.readouterr().out


@pytest.mark.parametrize("position", NOT_REVIEWERS)
def test_every_other_position_still_accepts_sub_sonnet(fm, position):
    """The restriction is for reviews. Extending it would be inventing a policy
    the ruling does not state — this fails if it creeps."""
    con = fm.connect()
    fm.set_runner(con, "operator/eunomia", position, "sub-sonnet", actor="cli:t")
    assert fm.resolve(con, "operator/eunomia", position) == ("sub-sonnet", "repo")


@pytest.mark.parametrize("position", REVIEWERS)
def test_a_default_row_for_a_reviewer_is_held_to_the_same_rule(fm, position):
    con = fm.connect()
    with pytest.raises(fm.UnknownRunner):
        fm.set_runner(con, "default", position, "sub-opus", actor="cli:t")
    fm.set_runner(con, "default", position, "sub-fable", actor="cli:t")
    assert fm.resolve(con, "operator/ares", position) == ("sub-fable", "default")


# ------------------------------------------------------- enforced at read


@pytest.mark.parametrize("position", REVIEWERS)
@pytest.mark.parametrize("repo", ["operator/eunomia", "default"])
def test_a_non_fable_row_inserted_directly_is_refused_at_read(fm, position, repo):
    """No supported path can create this row, so it is written with raw SQL —
    the shape of a row that predates the rule, or was hand-inserted."""
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES (?, ?, 1, 'sub-sonnet', 0)", (repo, position))
    con.commit()
    with pytest.raises(fm.UnknownRunner) as e:
        fm.resolve(con, "operator/eunomia", position)
    assert "2026-08-26" in str(e.value) and "sub-fable" in str(e.value)


def test_the_read_only_reader_refuses_it_too(fm, tmp_path):
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('operator/eunomia', 'code_reviewer', 1, 'sub-opus', 0)")
    con.commit()
    con.close()
    with pytest.raises(fm.UnknownRunner):
        fm.resolve_repo("operator/eunomia", "code_reviewer",
                        db_path=str(tmp_path / "fleet.db"))


def test_get_reports_the_refusal_rather_than_the_runner(fm, capsys):
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('operator/eunomia', 'plan_reviewer', 1, 'sub-sonnet', 0)")
    con.commit()
    fm.cmd_show(con, types.SimpleNamespace(repo="operator/eunomia"))
    captured = capsys.readouterr()
    assert "ERROR" in captured.err and "2026-08-26" in captured.err
    assert "sub-sonnet  [" not in captured.out


def test_a_non_fable_row_in_a_non_reviewer_position_still_reads(fm):
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('operator/eunomia', 'tester', 1, 'sub-sonnet', 0)")
    con.commit()
    assert fm.resolve(con, "operator/eunomia", "tester") == ("sub-sonnet", "repo")


# ------------------------------------------------------------------ D3


def test_the_rule_reads_family_not_the_runner_name(fm, monkeypatch):
    R = _registry(fm, monkeypatch, *[
        _runner(fm._load_orchestrator().Runner, "sub-sonnet-fable-ish", "claude"),
        _runner(fm._load_orchestrator().Runner, "x", "fable"),
    ])
    con = fm.connect()
    with pytest.raises(fm.UnknownRunner):
        fm.set_runner(con, "operator/eunomia", "code_reviewer", "sub-sonnet-fable-ish")
    fm.set_runner(con, "operator/eunomia", "code_reviewer", "x")
    assert fm.resolve(con, "operator/eunomia", "code_reviewer") == ("x", "repo")


def test_a_runner_with_no_family_is_not_fable(fm, monkeypatch):
    _registry(fm, monkeypatch,
              _runner(fm._load_orchestrator().Runner, "sub-fable-6", ""))
    with pytest.raises(fm.UnknownRunner):
        fm.set_runner(fm.connect(), "operator/eunomia", "code_reviewer", "sub-fable-6")


def test_every_registry_runner_declares_a_family(orch):
    for name, r in orch.RUNNERS.items():
        assert r.family, f"{name} has no family — the reviewer rule would refuse it"
