"""Tests for config/model-policy.json and its three enforcement points (plan 0155).

Each test runs against a copy of bin/ with its own config/ beside it, because
fleet-models reads the policy from its own checkout and from nowhere else.
"""
import importlib.machinery
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

NO_RULES = {"v": 1, "model_groups": {}, "repo_groups": {}, "deny": []}


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _checkout(tmp_path, policy):
    """A tmp checkout: a copy of bin/ and a config/ holding `policy` (a dict, a
    raw string, or None for no file)."""
    shutil.copytree(ROOT / "bin", tmp_path / "bin",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "config" / "model-policy.json").unlink()
    if policy is not None:
        text = policy if isinstance(policy, str) else json.dumps(policy)
        (tmp_path / "config" / "model-policy.json").write_text(text)
    return tmp_path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))

    def make(policy):
        co = _checkout(tmp_path / "co", policy)
        return co, _load(co / "bin" / "fleet-models", "fm_co")
    return make


def deny_a(**over):
    pol = {"v": 1,
           "model_groups": {"G": {"runners": ["sub-opus"]}},
           "repo_groups": {"A": {"repos": ["operator/a"]}},
           "deny": [{"models": "G", "repos": "A", "positions": ["*"], "why": "because"}]}
    pol.update(over)
    return pol


def test_shipped_policy_parses_and_has_no_deny_rules():
    pol = json.loads((ROOT / "config" / "model-policy.json").read_text())
    assert pol["deny"] == []
    fm = _load(ROOT / "bin" / "fleet-models", "fm_shipped")
    assert fm.load_policy()["deny"] == []


DEFECTS = {
    "missing file": (None, "cannot be read"),
    "bad json": ("{nope", "not valid JSON"),
    "unknown key": (dict(NO_RULES, extra=1), "unknown key 'extra'"),
    "unknown v": (dict(NO_RULES, v=2), "unknown v"),
    "undefined model group": (deny_a(model_groups={}), "model group 'G'"),
    "undefined repo group": (deny_a(repo_groups={}), "repo group 'A'"),
    "unknown runner": (deny_a(model_groups={"G": {"runners": ["nope"]}}),
                       "runner 'nope'"),
    "unknown family": (deny_a(model_groups={"G": {"families": ["nope"]}}),
                       "family 'nope'"),
    "unknown class": (deny_a(model_groups={"G": {"provenance": ["nope"]}}),
                      "provenance class 'nope'"),
    "unknown position": (deny_a(deny=[{"models": "G", "repos": "A",
                                       "positions": ["nope"], "why": "x"}]),
                         "position 'nope'"),
    "missing why": (deny_a(deny=[{"models": "G", "repos": "A", "positions": ["*"]}]),
                    "lacks 'why'"),
    "all_except cycle": (deny_a(repo_groups={"A": {"all_except": ["B"]},
                                             "B": {"all_except": ["A"]}}),
                         "cycle"),
}


@pytest.mark.parametrize("name", sorted(DEFECTS))
def test_defective_policy_refuses_every_set_and_resolve(env, name):
    policy, needle = DEFECTS[name]
    _co, fm = env(policy)
    con = fm.connect()
    with pytest.raises(fm.PolicyRefused, match=needle):
        fm.set_runner(con, "operator/b", "implementer", "sub-opus")
    # a row that got into the table anyway still refuses to resolve
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('operator/b','implementer',1,'sub-opus',0)")
    with pytest.raises(fm.PolicyRefused, match=needle):
        fm.resolve(con, "operator/b", "implementer")


def test_deny_for_listed_repo_at_every_position(env):
    _co, fm = env(deny_a())
    con = fm.connect()
    for pos in ("implementer", "tester", "code_reviewer"):
        with pytest.raises(fm.PolicyRefused, match="because"):
            fm.set_runner(con, "operator/a", pos, "sub-opus")
    fm.set_runner(con, "operator/b", "implementer", "sub-opus")
    assert fm.resolve(con, "operator/b", "implementer") == ("sub-opus", "repo")
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('operator/a','implementer',1,'sub-opus',0)")
    with pytest.raises(fm.PolicyRefused):
        fm.resolve(con, "operator/a", "implementer")


def test_position_list_limits_the_rule(env):
    pol = deny_a()
    pol["deny"][0]["positions"] = ["tester"]
    _co, fm = env(pol)
    con = fm.connect()
    fm.set_runner(con, "operator/a", "implementer", "sub-opus")
    with pytest.raises(fm.PolicyRefused):
        fm.set_runner(con, "operator/a", "tester", "sub-opus")


ALL_EXCEPT = {"v": 1,
              "model_groups": {"G": {"runners": ["sub-opus"]}},
              "repo_groups": {"bake": {"repos": ["operator/bake"]},
                              "rest": {"all_except": ["bake"]}},
              "deny": [{"models": "G", "repos": "rest", "positions": ["*"],
                        "why": "bakeoff only"}]}


def test_all_except_denies_a_repo_named_nowhere(env):
    _co, fm = env(ALL_EXCEPT)
    con = fm.connect()
    fm.set_runner(con, "operator/bake", "implementer", "sub-opus")
    with pytest.raises(fm.PolicyRefused, match="bakeoff only"):
        fm.set_runner(con, "operator/created-tomorrow", "implementer", "sub-opus")


def test_default_is_refused_at_set_and_judged_by_real_repo_at_resolve(env):
    _co, fm = env(ALL_EXCEPT)
    con = fm.connect()
    with pytest.raises(fm.PolicyRefused):
        fm.set_runner(con, "default", "implementer", "sub-opus")
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('default','implementer',1,'sub-opus',0)")
    with pytest.raises(fm.PolicyRefused):
        fm.resolve(con, "operator/other", "implementer")
    assert fm.resolve(con, "operator/bake", "implementer") == ("sub-opus", "default")


def test_groups_by_family_and_by_provenance_match(env):
    for group in ({"families": ["fable"]}, {"provenance": ["cn-open"]}):
        pol = deny_a(model_groups={"G": group})
        _co, fm = env(pol)
        runner = "sub-fable" if "families" in group else "local-qwen"
        with pytest.raises(fm.PolicyRefused):
            fm.check_policy(runner, "implementer", "operator/a")
        fm.check_policy("sub-opus", "implementer", "operator/a")
        shutil.rmtree(_co)


def test_env_override_naming_a_denied_runner_stops_in_route_implementer(env):
    co, _fm = env(deny_a())
    orch = _load(co / "bin" / "orchestrator", "orch_co")
    cred = orch.Credential("oauth_token", "x", "x", "x") \
        if hasattr(orch, "Credential") else None
    fm = {"repo": "operator/a", "zone": "public"}
    with pytest.raises(orch.Stop, match="because"):
        orch.route_implementer(fm, cred, env={"EUNOMIA_IMPL_RUNNER": "sub-opus"})
    fm["repo"] = "operator/b"
    assert orch.route_implementer(
        fm, cred, env={"EUNOMIA_IMPL_RUNNER": "sub-opus"}).name == "sub-opus"


def test_dispatched_repo_beats_plan_front_matter(env):
    co, _fm = env(deny_a())
    orch = _load(co / "bin" / "orchestrator", "orch_co2")
    cred = orch.Credential("oauth_token", "x", "x", "x") \
        if hasattr(orch, "Credential") else None
    ov = {"EUNOMIA_IMPL_RUNNER": "sub-opus"}
    for fm in ({"zone": "public"}, {"zone": "public", "repo": "operator/b"}):
        with pytest.raises(orch.Stop, match="because"):
            orch.route_implementer(fm, cred, env=ov, repo="operator/a")
    assert orch.route_implementer({"zone": "public", "repo": "operator/a"}, cred,
                                  env=ov, repo="operator/b").name == "sub-opus"


def test_policy_check_exit_codes(env):
    co, _fm = env(deny_a())
    cli = [sys.executable, str(co / "bin" / "fleet-models"), "policy", "check"]
    ok = subprocess.run(cli + ["operator/b", "implementer", "sub-opus"],
                        capture_output=True, text=True)
    assert (ok.returncode, ok.stdout.strip()) == (0, "allowed")
    no = subprocess.run(cli + ["operator/a", "implementer", "sub-opus"],
                        capture_output=True, text=True)
    assert no.returncode == 4 and "because" in no.stdout
    show = subprocess.run([sys.executable, str(co / "bin" / "fleet-models"),
                           "policy", "show", "--json"], capture_output=True, text=True)
    assert json.loads(show.stdout) == deny_a()
