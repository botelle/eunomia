"""Tests for plan 0157: a reviewer seat takes any harness talos can launch
(claude, epeius), and `fleet-models reviewers` carries the spawn details."""
import importlib.machinery
import importlib.util
import json
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REVIEWERS = ("plan_reviewer", "code_reviewer", "style_reviewer", "lane_reviewer")
EPEIUS_KEYS = {"backend", "endpoint", "vendor_key", "digest", "num_ctx",
               "reply_reserve", "max_output_tokens", "max_input_tokens"}


def _load(name):
    mod_name = name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def fm(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    return _load("fleet-models")


def test_runner_harness_is_derivable_for_every_runner(fm):
    orch = fm._load_orchestrator()
    for name, r in orch.RUNNERS.items():
        h = fm.runner_harness(r)
        if r.unwired:
            continue
        assert h in ("claude", "codex", "antigravity", "epeius", "muse"), name
        declared = getattr(r, "harness", None)
        # A runner with a declared harness and an empty argv (sub-muse: eunomia
        # never spawns it) has no binary to derive from, so there is nothing to agree with.
        if declared and r.argv:
            assert declared == fm.runner_harness(r._replace(harness=None)), name


def test_known_harnesses(fm):
    R = fm._load_orchestrator().RUNNERS
    assert fm.runner_harness(R["sub-opus-5-5"]) == "claude"
    assert fm.runner_harness(R["sub-codex"]) == "codex"
    assert fm.runner_harness(R["sub-antigravity"]) == "antigravity"
    assert fm.runner_harness(R["local-gemma4-31b"]) == "epeius"
    assert fm.runner_harness(R["hosted-muse-spark-open"]) == "epeius"


@pytest.mark.parametrize("position", REVIEWERS)
@pytest.mark.parametrize("runner", ["local-gemma4-31b", "local-qwen"])
def test_epeius_runners_are_accepted_at_reviewer_positions(fm, position, runner):
    fm.validate_runner(runner, position)


def test_review_harnesses_is_exactly_the_five(fm):
    assert fm.REVIEW_HARNESSES == ("claude", "epeius", "codex", "antigravity", "muse")


@pytest.mark.parametrize("position", REVIEWERS)
@pytest.mark.parametrize("runner", ["sub-codex", "sub-antigravity"])
def test_codex_and_antigravity_are_accepted_at_reviewer_positions(fm, position, runner):
    fm.validate_runner(runner, position)


def test_an_underivable_harness_is_refused_at_a_reviewer_and_accepted_at_implementer(fm, monkeypatch):
    orch = fm._load_orchestrator()
    stub = orch.Runner("stub", ("/usr/bin/true",), False, None, (), "", "x",
                       provenance="us-hosted", model="claude-opus-5-5")
    monkeypatch.setattr(fm, "_load_orchestrator", lambda: types.SimpleNamespace(
        RUNNERS={"stub": stub}, Runner=orch.Runner, EPEIUS_BIN=orch.EPEIUS_BIN,
        ANTIGRAVITY_BIN=orch.ANTIGRAVITY_BIN, CODEX_BIN=orch.CODEX_BIN))
    assert fm.runner_harness(stub) is None
    with pytest.raises(fm.UnknownRunner):
        fm.validate_runner("stub", "code_reviewer")
    fm.validate_runner("stub", "implementer")


def _reviewers(fm, capsys, runner):
    assert fm.main(["set", "operator/eunomia", "code_reviewer", runner]) == 0
    capsys.readouterr()
    assert fm.main(["reviewers", "operator/eunomia", "code_reviewer", "--json"]) == 0
    return json.loads(capsys.readouterr().out)[0]


def test_reviewers_emits_the_local_epeius_keys(fm, capsys):
    r = fm._load_orchestrator().RUNNERS["local-gemma4-31b"]
    out = _reviewers(fm, capsys, "local-gemma4-31b")
    assert out["harness"] == "epeius"
    assert out["backend"] == "ollama:gemma4:31b"
    assert out["endpoint"] == "cihost-loopback"
    assert out["vendor_key"] is None
    assert out["digest"] == r.digest and out["num_ctx"] == r.num_ctx
    assert out["reply_reserve"] == r.reply_reserve
    assert out["max_output_tokens"] is None and out["max_input_tokens"] is None


def test_reviewers_emits_the_hosted_epeius_keys(fm, capsys):
    r = fm._load_orchestrator().RUNNERS["hosted-muse-spark-open"]
    out = _reviewers(fm, capsys, "hosted-muse-spark-open")
    assert out["harness"] == "epeius"
    assert out["backend"] == "openai:" + r.model
    assert out["endpoint"] == r.endpoint == "https://openrouter.ai/api/v1"
    assert r.endpoint in r.argv
    assert out["vendor_key"] == r.vendor_key
    assert out["max_output_tokens"] == r.max_output_tokens
    assert out["max_input_tokens"] == r.max_input_tokens
    assert out["digest"] is None and out["num_ctx"] is None and out["reply_reserve"] is None


def test_a_claude_reviewer_emits_the_harness_and_no_epeius_keys(fm, capsys):
    out = _reviewers(fm, capsys, "sub-opus-5-5")
    assert out["harness"] == "claude"
    assert not EPEIUS_KEYS & set(out)


@pytest.mark.parametrize("position", REVIEWERS)
def test_a_local_runner_on_another_host_is_refused_at_a_reviewer(fm, position):
    with pytest.raises(fm.UnknownRunner) as e:
        fm.validate_runner("local-gemma3-12b", position)
    assert "cihost only" in str(e.value)
    fm.validate_runner("local-gemma3-12b", "implementer")


@pytest.mark.parametrize("runner,harness", [("sub-codex", "codex"),
                                            ("sub-antigravity", "antigravity")])
def test_reviewers_emits_harness_and_no_epeius_keys_for_subscription_runners(
        fm, capsys, runner, harness):
    out = _reviewers(fm, capsys, runner)
    assert out["harness"] == harness
    assert out["model"] == fm._load_orchestrator().RUNNERS[runner].model
    for k in ("backend", "endpoint", "vendor_key", "digest", "num_ctx",
              "reply_reserve", "max_output_tokens", "max_input_tokens"):
        assert k not in out, k


# ------------------------------------------------------------ plan 0173: sub-muse

@pytest.mark.parametrize("position", REVIEWERS)
def test_sub_muse_is_accepted_at_every_reviewer_position(fm, position):
    fm.validate_runner("sub-muse", position)


def test_sub_muse_is_accepted_at_implementer_through_its_writer_argv(fm):
    fm.validate_runner("sub-muse", "implementer")


@pytest.mark.parametrize("position", ("tester", "mediator"))
def test_sub_muse_is_refused_at_the_other_positions(fm, position):
    with pytest.raises(fm.ReviewerRunnerRefused) as e:
        fm.validate_runner("sub-muse", position)
    assert "no implementer/tester spawn path for muse yet" in str(e.value)


def test_sub_muse_declares_a_harness_an_empty_argv_and_a_versioned_model(fm):
    r = fm._load_orchestrator().RUNNERS["sub-muse"]
    assert fm.runner_harness(r) == "muse"
    assert r.argv == () and r.model == "muse-wonder-1.0"
    assert r.provenance == "us-hosted" and r.family == "muse"


def test_muse_wonder_is_still_refused(fm, monkeypatch):
    orch = fm._load_orchestrator()
    stub = orch.RUNNERS["sub-muse"]._replace(model="muse-wonder")
    monkeypatch.setattr(fm, "_load_orchestrator", lambda: types.SimpleNamespace(
        RUNNERS={"sub-muse": stub}))
    with pytest.raises(fm.ReviewerRunnerRefused):
        fm.validate_runner("sub-muse", "code_reviewer")


def test_reviewers_emits_the_muse_harness_and_no_epeius_keys(fm, capsys):
    out = _reviewers(fm, capsys, "sub-muse")
    assert out["harness"] == "muse"
    assert out["model"] == "muse-wonder-1.0"
    assert not EPEIUS_KEYS & set(out)


def test_no_lane_spawn_path_reaches_sub_muse(fm, monkeypatch):
    # its review/test argv stays empty; implementing goes through writer_argv
    orch = fm._load_orchestrator()
    spawned = []
    monkeypatch.setattr(orch.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(orch.subprocess, "run", lambda *a, **k: spawned.append(a))
    cred = types.SimpleNamespace(kind="local_cli", principal="operator")
    route = orch.route_implementer({"zone": "public", "repo": "operator/x"}, cred,
                                   {"EUNOMIA_IMPL_RUNNER": "sub-muse"})
    with pytest.raises(orch.Stop):
        orch.harness_argv(route)
    assert spawned == []
