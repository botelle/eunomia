"""Tests for the reviewer positions (plan 0081, ADR-0013).

Every position refuses a runner with no full model version (plan 0153); the
reviewer positions also refuse an unwired runner and one with no review spawn
path (a harness talos cannot launch; plan 0157), at write (`set_runner`) and at read
(`resolve`). No class or locality refuses a seat. Fable is the default by
configuration, not by code.
"""
import json
import importlib.machinery
import importlib.util
import sqlite3
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

REVIEWERS = ("plan_reviewer", "code_reviewer", "style_reviewer")
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
                        lambda: types.SimpleNamespace(
                            RUNNERS=reg, Runner=orch.Runner, EPEIUS_BIN=orch.EPEIUS_BIN,
                            ANTIGRAVITY_BIN=orch.ANTIGRAVITY_BIN, CODEX_BIN=orch.CODEX_BIN))
    return orch.Runner


def _runner(Runner, name, model="claude-opus-5-5", **kw):
    argv = ("claude", "--model", model)
    return Runner(name, argv, kw.get("local", False), "local_cli", (),
                  kw.get("unwired", ""), "claude", provenance="us-hosted",
                  model=model)


def _stub(fm):
    """A runner whose harness derives to None, so no reviewer seat can launch it."""
    return fm._load_orchestrator().Runner(
        "stub", ("/usr/bin/true",), False, None, (), "", "x",
        provenance="us-hosted", model="claude-opus-5-5")


# ------------------------------------------------------------------ D1


def test_sub_fable_is_in_the_registry_and_pins_the_exact_model_id(orch):
    r = orch.RUNNERS["sub-fable"]
    assert r.family == "fable"
    assert r.needs == "oauth_token" and not r.local and not r.unwired
    argv = list(r.argv)
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    # The two spellings the catalog rejects, and the alias that would let the
    # model move without a decision — none of them may be what is pinned.
    assert not {"claude-fable-5.1", "claude-fable-51", "fable"} & set(argv)


def test_sub_fable_has_the_shape_of_sub_opus(orch):
    fable, opus = orch.RUNNERS["sub-fable"], orch.RUNNERS["sub-opus"]
    strip = lambda r: tuple(a for a in r.argv if a not in ("claude-fable-5-1", "claude-opus-5-5"))
    assert strip(fable) == strip(opus)
    assert (fable.local, fable.needs, fable.extra_env, fable.unwired) == \
           (opus.local, opus.needs, opus.extra_env, opus.unwired)


def test_pinned_runners_exist_with_exact_ids(orch):
    for name, model in (("sub-opus-5-5", "claude-opus-5-5"),
                        ("sub-sonnet-5", "claude-sonnet-5")):
        r = orch.RUNNERS[name]
        assert r.family == "claude" and not r.local and not r.unwired
        argv = list(r.argv)
        assert argv[argv.index("--model") + 1] == model


def test_every_runner_declares_a_full_version(orch):
    import re
    full = re.compile(r"(?:[a-z][a-z0-9._-]*/)?[a-z][a-z0-9._:-]*\d[a-z0-9._:-]*")
    for name, r in orch.RUNNERS.items():
        assert r.model and full.fullmatch(r.model), f"{name}: {r.model!r}"
        if r.argv and not orch.is_epeius_runner(r):
            argv = list(r.argv)
            # every Codex runner (there is more than one, plan 0202) pins its
            # model with `-m`; picked by harness, not by name, so a new one
            # holds for the rule too
            flag = "-m" if orch.CODEX_BIN in argv else "--model"
            assert argv[argv.index(flag) + 1] == r.model, name
    assert orch.RUNNERS["local-qwen"].model == "qwen3.6:35b-a3b-q8_0"


def test_the_alias_runners_keep_their_names_and_pin_the_measured_ids(orch):
    # docs/per-repo-models.md records the measurement (2026-10-24)
    assert orch.RUNNERS["sub-sonnet"].model == "claude-sonnet-5-5"
    assert orch.RUNNERS["api-sonnet"].model == "claude-sonnet-5-5"
    assert orch.RUNNERS["sub-opus"].model == "claude-opus-5-5"


@pytest.mark.parametrize("position", REVIEWERS)
@pytest.mark.parametrize("runner", ["sub-fable", "sub-opus-5-5", "sub-sonnet-5"])
def test_a_reviewer_position_accepts_a_pinned_runner(fm, position, runner):
    con = fm.connect()
    ordinal, changed = fm.set_runner(con, "operator/eunomia", position, runner,
                                     actor="cli:t")
    assert changed and ordinal == 1
    assert fm.resolve(con, "operator/eunomia", position) == (runner, "repo")


@pytest.mark.parametrize("position", REVIEWERS)
@pytest.mark.parametrize("runner", ["sub-sonnet", "sub-opus", "api-sonnet"])
def test_a_reviewer_position_accepts_the_formerly_alias_runners(fm, position, runner):
    con = fm.connect()
    fm.set_runner(con, "operator/eunomia", position, runner, actor="cli:t")
    assert fm.resolve(con, "operator/eunomia", position) == (runner, "repo")


@pytest.mark.parametrize("position", REVIEWERS)
def test_a_reviewer_position_refuses_and_writes_nothing(fm, monkeypatch, position):
    runner, reason = "stub", "no review spawn path yet for its harness None"
    _registry(fm, monkeypatch, _stub(fm))
    con = fm.connect()
    with pytest.raises(fm.UnknownRunner) as e:
        fm.set_runner(con, "operator/eunomia", position, runner, actor="cli:t")
    assert reason in str(e.value)
    assert "cn-open" not in str(e.value) and "ADR-0011" not in str(e.value)
    assert con.execute("SELECT COUNT(*) FROM repo_model").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM repo_model_change").fetchone()[0] == 0


@pytest.mark.parametrize("runner", ["sub-codex", "sub-antigravity"])
def test_the_vendor_runners_are_accepted_at_tester(fm, runner):
    con = fm.connect()
    fm.set_runner(con, "operator/eunomia", "tester", runner, actor="cli:t")
    assert fm.resolve(con, "operator/eunomia", "tester") == (runner, "repo")


def test_unwired_is_refused_even_when_pinned(fm, monkeypatch):
    Runner = fm._load_orchestrator().Runner
    _registry(fm, monkeypatch, _runner(Runner, "u", unwired="not wired"))
    with pytest.raises(fm.UnknownRunner) as e:
        fm.set_runner(fm.connect(), "operator/eunomia", "code_reviewer", "u")
    assert "unwired" in str(e.value)


def test_local_is_not_refused_for_being_local(fm, monkeypatch):
    Runner = fm._load_orchestrator().Runner
    _registry(fm, monkeypatch, _runner(Runner, "l", local=True))
    fm.set_runner(fm.connect(), "operator/eunomia", "code_reviewer", "l")


@pytest.mark.parametrize("position", REVIEWERS + NOT_REVIEWERS)
@pytest.mark.parametrize("model", ["sonnet", "opus", "", None])
def test_an_alias_or_missing_model_is_refused_at_every_position(fm, monkeypatch,
                                                                 position, model):
    Runner = fm._load_orchestrator().Runner
    _registry(fm, monkeypatch, _runner(Runner, "r", model))
    with pytest.raises(fm.UnknownRunner) as e:
        fm.set_runner(fm.connect(), "operator/eunomia", position, "r")
    assert "'r'" in str(e.value) and position in str(e.value) and repr(model) in str(e.value)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1", "gpt-6-astra",
                                   "gemini-4-argon-medium", "qwen3.6:35b-a3b-q8_0"])
def test_a_full_version_from_any_family_passes(fm, monkeypatch, model):
    Runner = fm._load_orchestrator().Runner
    _registry(fm, monkeypatch, _runner(Runner, "r", model))
    fm.set_runner(fm.connect(), "operator/eunomia", "code_reviewer", "r")


def test_the_pin_reads_the_declared_model_not_the_runner_name(fm, monkeypatch):
    Runner = fm._load_orchestrator().Runner
    _registry(fm, monkeypatch,
              _runner(Runner, "sub-fable-5-1", "opus"),
              _runner(Runner, "whatever", "claude-haiku-4-5"))
    con = fm.connect()
    with pytest.raises(fm.UnknownRunner):
        fm.set_runner(con, "operator/eunomia", "code_reviewer", "sub-fable-5-1")
    fm.set_runner(con, "operator/eunomia", "code_reviewer", "whatever")


def test_no_same_family_refusal(fm):
    fm.set_runner(fm.connect(), "operator/eunomia", "code_reviewer", "sub-sonnet-5",
                  actor="cli:t")


def test_the_cli_refusal_exits_nonzero_and_says_why(fm, capsys, monkeypatch):
    _registry(fm, monkeypatch, _stub(fm), _runner(fm._load_orchestrator().Runner, "sub-opus-5-5"))
    assert fm.main(["set", "operator/eunomia", "code_reviewer", "stub"]) == 1
    assert "no review spawn path" in capsys.readouterr().err
    assert fm.main(["set", "operator/eunomia", "code_reviewer", "sub-opus-5-5"]) == 0


@pytest.mark.parametrize("position", NOT_REVIEWERS)
def test_every_other_position_still_accepts_sub_sonnet(fm, position):
    con = fm.connect()
    fm.set_runner(con, "operator/eunomia", position, "sub-sonnet", actor="cli:t")
    assert fm.resolve(con, "operator/eunomia", position) == ("sub-sonnet", "repo")


@pytest.mark.parametrize("position", REVIEWERS)
@pytest.mark.parametrize("repo", ["operator/eunomia", "default"])
def test_a_refused_row_inserted_directly_is_refused_at_read(fm, monkeypatch, position, repo):
    _registry(fm, monkeypatch, _stub(fm))
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES (?, ?, 1, 'stub', 0)", (repo, position))
    con.commit()
    with pytest.raises(fm.UnknownRunner) as e:
        fm.resolve(con, "operator/eunomia", position)
    assert "no review spawn path" in str(e.value)


def test_the_read_only_reader_refuses_it_too(fm, tmp_path, monkeypatch):
    _registry(fm, monkeypatch, _stub(fm))
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('operator/eunomia', 'code_reviewer', 1, 'stub', 0)")
    con.commit()
    con.close()
    with pytest.raises(fm.UnknownRunner):
        fm.resolve_repo("operator/eunomia", "code_reviewer",
                        db_path=str(tmp_path / "fleet.db"))


def test_get_reports_the_refusal_rather_than_the_runner(fm, capsys, monkeypatch):
    _registry(fm, monkeypatch, _stub(fm))
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('operator/eunomia', 'plan_reviewer', 1, 'stub', 0)")
    con.commit()
    fm.cmd_show(con, types.SimpleNamespace(repo="operator/eunomia"))
    captured = capsys.readouterr()
    assert "ERROR" in captured.err and "spawn path" in captured.err
    assert "stub  [" not in captured.out


def test_a_non_pinned_row_in_a_non_reviewer_position_still_reads(fm):
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('operator/eunomia', 'tester', 1, 'sub-sonnet', 0)")
    con.commit()
    assert fm.resolve(con, "operator/eunomia", "tester") == ("sub-sonnet", "repo")


def test_every_registry_runner_declares_a_family(orch):
    for name, r in orch.RUNNERS.items():
        assert r.family, f"{name} has no family"


# ------------------------------------------------------------------ D3


def test_reviewers_prints_the_default_and_the_repo_ab_arm(fm, capsys):
    fm.main(["set", "default", "code_reviewer", "sub-fable"])
    fm.main(["set", "operator/eunomia", "code_reviewer", "2", "sub-opus-5-5"])
    capsys.readouterr()
    assert fm.main(["reviewers", "operator/eunomia", "code_reviewer", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"ordinal": 1, "runner": "sub-fable", "model": "claude-fable-5-1",
         "family": "fable", "layer": "default", "harness": "claude",
         "access": "repo"},
        {"ordinal": 2, "runner": "sub-opus-5-5", "model": "claude-opus-5-5",
         "family": "claude", "layer": "repo", "harness": "claude",
         "access": "repo"}]


def test_reviewers_exits_3_without_ordinal_1_and_names_the_fix(fm, capsys):
    assert fm.main(["reviewers", "operator/eunomia", "code_reviewer", "--json"]) == 3
    err = capsys.readouterr().err
    assert "fleet-models set default code_reviewer <runner>" in err
    assert "sub-fable" not in err
    fm.main(["set", "operator/eunomia", "code_reviewer", "2", "sub-opus-5-5"])
    assert fm.main(["reviewers", "operator/eunomia", "code_reviewer", "--json"]) == 3


def test_reviewers_is_limited_to_reviewer_positions(fm):
    with pytest.raises(SystemExit):
        fm.main(["reviewers", "operator/eunomia", "tester", "--json"])
