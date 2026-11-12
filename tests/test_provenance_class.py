"""Tests for the runner provenance class (plan 0072, ADR-0011)."""
import importlib.machinery
import importlib.util
import sqlite3
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

ALL = ("implementer", "plan_reviewer", "code_reviewer", "tester", "mediator",
       "impl_supervisor", "test_supervisor", "style_reviewer")
CLASSES = ("us-hosted", "us-open", "cn-open", "eu-open", "kr-open", "ca-open",
           "cn-hosted", "routed-hosted")


def _load(name, mod_name=None):
    mod_name = mod_name or name.replace("-", "_") + "_prov"
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    return _load("orchestrator")


@pytest.fixture
def fm(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    return _load("fleet-models")


def _runner(orch, name, provenance, needs=None, extra_env=()):
    # A pinned, wired, hosted-shaped runner so only the class can refuse it.
    return orch.Runner(name, ("claude", "--model", "claude-opus-5-5"), False,
                       needs, extra_env, "", "claude", provenance=provenance,
                       model="claude-opus-5-5")


def _registry(fm, monkeypatch, *runners):
    orch = fm._load_orchestrator()
    reg = {r.name: r for r in runners}
    monkeypatch.setattr(fm, "_load_orchestrator",
                        lambda: types.SimpleNamespace(
                            RUNNERS=reg, Runner=orch.Runner, EPEIUS_BIN=orch.EPEIUS_BIN,
                            ANTIGRAVITY_BIN=orch.ANTIGRAVITY_BIN, CODEX_BIN=orch.CODEX_BIN))


# ------------------------------------------------------------------ D1

def test_the_shipped_registry_is_classified(orch):
    assert orch.registry_errors(orch.RUNNERS) == []
    assert orch.RUNNERS["local-qwen"].provenance == "cn-open"
    assert orch.RUNNERS["sub-sonnet"].provenance == "us-hosted"


def test_a_runner_without_a_class_is_a_registry_error(orch):
    bad = {"x": orch.Runner("x", (), False, None, (), "", "claude")}
    errs = orch.registry_errors(bad)
    assert len(errs) == 1 and "'x'" in errs[0]


def test_a_runner_with_a_word_outside_the_set_is_a_registry_error(orch):
    bad = {"x": _runner(orch, "x", "unknown")}
    assert len(orch.registry_errors(bad)) == 1


def test_the_set_is_closed_at_eight(orch):
    assert len(orch.PROVENANCE_CLASSES) == 8


# ------------------------------------------------------------------ D2

@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("position", ALL)
def test_no_position_refuses_by_class(fm, monkeypatch, cls, position):
    orch = fm._load_orchestrator()
    # a name that says nothing about the class; ADR-0026 §1: class refuses nothing
    _registry(fm, monkeypatch, _runner(orch, "api-fable", cls))
    fm.validate_runner("api-fable", position)


def test_shipped_local_qwen_is_accepted_at_code_reviewer(fm):
    fm.validate_runner("local-qwen", "code_reviewer")


def test_shipped_sub_codex_is_accepted_at_code_reviewer(fm):
    fm.validate_runner("sub-codex", "code_reviewer")


def test_the_new_classes_are_open(orch):
    assert orch.PROVENANCE_CLASSES == CLASSES
    for c in ("eu-open", "kr-open", "ca-open"):
        assert c in orch.OPEN_CLASSES
    assert "us-hosted" not in orch.OPEN_CLASSES


def test_the_hosted_classes_are_classes_and_not_open(orch):
    for c in ("cn-hosted", "routed-hosted"):
        assert c in orch.PROVENANCE_CLASSES
        assert c not in orch.OPEN_CLASSES


def test_a_kr_open_runner_with_a_credential_stops(orch):
    r = _runner(orch, "x", "kr-open", needs="api_key")
    with pytest.raises(orch.Stop):
        orch.check_open_weights_box(r)


def test_the_read_side_does_not_refuse_by_class(fm, monkeypatch, tmp_path):
    orch = fm._load_orchestrator()
    _registry(fm, monkeypatch, _runner(orch, "r1", "cn-open"))
    con = sqlite3.connect(tmp_path / "fleet.db")
    con.executescript(fm.SCHEMA)
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner) "
                "VALUES ('operator/x', 'tester', 1, 'r1')")
    con.commit()
    assert fm.resolve_repo("operator/x", "tester") == ("r1", "repo")


# ------------------------------------------------------------------ D3

def _route(orch, runner, monkeypatch, cred_kind="local_cli"):
    monkeypatch.setitem(orch.RUNNERS, runner.name, runner)
    cred = types.SimpleNamespace(kind=cred_kind, initiator="t")
    return orch.route_implementer({"zone": "public"}, cred,
                                  {"EUNOMIA_IMPL_RUNNER": runner.name})


def test_cn_open_with_a_credential_stops(orch, monkeypatch):
    r = _runner(orch, "t-cn", "cn-open", needs="local_cli", extra_env=("OLLAMA_HOST",))
    with pytest.raises(orch.Stop) as e:
        _route(orch, r, monkeypatch)
    assert "needs" in str(e.value) and "local_cli" in str(e.value)


def test_us_open_with_extra_environment_stops(orch, monkeypatch):
    r = _runner(orch, "t-us", "us-open", extra_env=("OLLAMA_HOST", "ANTHROPIC_API_KEY"))
    with pytest.raises(orch.Stop) as e:
        _route(orch, r, monkeypatch)
    assert "extra_env" in str(e.value) and "ANTHROPIC_API_KEY" in str(e.value)


def test_a_boxed_open_runner_routes(orch, monkeypatch):
    r = _runner(orch, "t-ok", "us-open", extra_env=("OLLAMA_HOST",))
    assert _route(orch, r, monkeypatch) is r


def test_a_hosted_runner_with_a_credential_and_env_still_routes(orch, monkeypatch):
    r = _runner(orch, "t-host", "us-hosted", needs="api_key",
                extra_env=("ANTHROPIC_API_KEY", "OTHER"))
    assert _route(orch, r, monkeypatch, cred_kind="api_key") is r
