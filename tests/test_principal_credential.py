"""A venture's repository spends the venture's Claude credential (plan 0125).

Helpers are scripts on a temporary path; no test touches the vault or holds a real
token. The mopsus and fleet-quota halves run against the real modules and ledger,
as tests/test_implementer_credential.py does.
"""
import subprocess

import pytest

from test_fleet_quota import (  # noqa: F401  (fixtures are imported by name)
    fq, mp, fc, pages, _sweep,
)
from test_implementer_credential import (  # noqa: F401
    orch, helper, AUTH_RESULT, STUB, _reason, _Spawned,
)
from test_mopsus import _mech

VSTUB = "venture-stub-VENTURESECRET-0123456789"
VENTURE_REPO = "operator/venture-app"


@pytest.fixture
def conf(orch, tmp_path, monkeypatch):
    """A principals.conf stand-in, and the orchestrator pointed at it."""
    path = tmp_path / "principals.conf"
    monkeypatch.setattr(orch, "PRINCIPALS_CONF", path)

    def write(text):
        path.write_text(text)
    write.path = path
    return write


@pytest.fixture
def venture(conf, tmp_path):
    """Principal `v` (oauth_token) owning VENTURE_REPO, with a working helper."""
    h = tmp_path / "v-helper.sh"
    h.write_text(f"#!/bin/sh\necho {VSTUB}\n")
    h.chmod(0o755)
    conf(f"principal: v  kind: oauth_token  cmd: {h}\nrepo: {VENTURE_REPO}  principal: v\n")
    return h


def _spawn_env(orch, monkeypatch, tmp_path, cred, route):
    monkeypatch.setattr(orch, "harness_argv", lambda r: ["child"])
    real_popen = subprocess.Popen

    def popen(argv, *a, **kw):
        if argv != ["child"]:
            return real_popen(argv, *a, **kw)
        raise _Spawned(kw["env"])
    monkeypatch.setattr(orch.subprocess, "Popen", popen)
    with pytest.raises(_Spawned) as e:
        orch.run_implementer(tmp_path, "prompt", cred, route, "L1")
    return e.value.env


# --- D1. the file -------------------------------------------------------------


def test_the_shipped_file_declares_nothing(orch):
    assert orch.load_principals(orch.BIN.parent / "config" / "principals.conf") == ({}, {})


def test_no_file_means_every_repository_is_the_operators(orch, conf, monkeypatch):
    monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
    cred = orch.resolve_credential(repo=VENTURE_REPO)   # conf never written
    assert (cred.kind, cred.initiator, cred.principal) == ("oauth_token", "operator", "operator")


@pytest.mark.parametrize("text", [
    "repo: operator/x  principal: ghost\n",                          # undeclared
    "principal: v  kind: password  cmd: /bin/true\n",               # unknown kind
    "principal: operator  kind: oauth_token  cmd: /bin/true\n",     # reserved
    "principal: v  kind: api_key  cmd: /bin/true\nprincipal: v  kind: api_key  cmd: /bin/true\n",
    "principal: v  kind: api_key  cmd: /bin/true\nrepo: a/b  principal: v\nrepo: a/b  principal: v\n",
    "nonsense\n",
], ids=["undeclared", "kind", "operator", "dup-principal", "dup-repo", "garbage"])
def test_a_bad_file_is_a_parse_error(orch, conf, text):
    conf(text)
    with pytest.raises(orch.Stop):
        orch.load_principals()


def test_comments_and_a_repo_line_naming_the_operator_parse(orch, conf):
    conf("# note\n\nrepo: operator/x  principal: operator  # explicit\n")
    assert orch.load_principals() == ({}, {"operator/x": "operator"})


# --- D2. resolution -----------------------------------------------------------


def test_resolution_order_initiator_then_repository_then_operator(
        orch, venture, monkeypatch):
    monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
    assert orch.resolve_credential(repo=VENTURE_REPO).principal == "v"
    assert orch.resolve_credential(repo="operator/eunomia").principal == "operator"
    monkeypatch.setenv("EUNOMIA_INITIATOR", "operator")
    assert orch.resolve_credential(repo=VENTURE_REPO).principal == "operator"
    monkeypatch.setenv("EUNOMIA_INITIATOR", "ghost")
    with pytest.raises(orch.Stop):
        orch.resolve_credential(repo=VENTURE_REPO)


# --- D3. the token for that principal -----------------------------------------


def test_an_existing_repository_runs_the_operators_helper_unchanged(
        orch, helper, venture, monkeypatch, tmp_path):
    monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
    cred = orch.resolve_credential(repo="operator/eunomia")
    env = _spawn_env(orch, monkeypatch, tmp_path, cred, orch.RUNNERS["sub-sonnet"])
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == STUB
    assert VSTUB not in env.values()


def test_an_oauth_venture_runs_its_own_helper_and_holds_nothing_of_the_operators(
        orch, helper, venture, monkeypatch, tmp_path):
    monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", STUB)      # a stale parent value
    cred = orch.resolve_credential(repo=VENTURE_REPO)
    env = _spawn_env(orch, monkeypatch, tmp_path, cred, orch.RUNNERS["sub-sonnet"])
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == VSTUB
    assert STUB not in env.values()
    assert "ANTHROPIC_API_KEY" not in env


def test_an_api_key_venture_resolves_only_an_api_key_runner(
        orch, helper, conf, tmp_path, monkeypatch):
    h = tmp_path / "k-helper.sh"
    h.write_text(f"#!/bin/sh\necho {VSTUB}\n")
    h.chmod(0o755)
    conf(f"principal: k  kind: api_key  cmd: {h}\nrepo: {VENTURE_REPO}  principal: k\n")
    monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", STUB)
    cred = orch.resolve_credential(repo=VENTURE_REPO)
    assert cred.kind == "api_key"
    with pytest.raises(orch.Stop):
        orch.route_implementer({"zone": "public"}, cred,
                               env={"EUNOMIA_IMPL_RUNNER": "sub-sonnet"})
    route = orch.route_implementer({"zone": "public"}, cred,
                                   env={"EUNOMIA_IMPL_RUNNER": "api-sonnet"})
    env = _spawn_env(orch, monkeypatch, tmp_path, cred, route)
    assert env["ANTHROPIC_API_KEY"] == VSTUB
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_a_failing_venture_helper_never_falls_back_to_the_operators(
        orch, helper, venture, monkeypatch, tmp_path):
    venture.write_text("#!/bin/sh\nexit 3\n")
    monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
    cred = orch.resolve_credential(repo=VENTURE_REPO)
    monkeypatch.setattr(orch, "harness_argv", lambda r: ["child"])
    real_popen = subprocess.Popen
    monkeypatch.setattr(orch.subprocess, "Popen", lambda argv, *a, **kw: (
        pytest.fail("a child was spawned") if argv == ["child"]
        else real_popen(argv, *a, **kw)))
    with pytest.raises(orch.Stop) as e:
        orch.run_implementer(tmp_path, "prompt", cred, orch.RUNNERS["sub-sonnet"], "L1")
    r = e.value.reason
    assert r.startswith(f"{orch.CREDENTIAL_REJECTED} (principal v):")
    assert STUB not in r and VSTUB not in r


# --- D4. the gate is the principal's ------------------------------------------


def test_the_operators_rejection_reason_is_unchanged(orch):
    assert orch.rejected_prefix("operator") == orch.CREDENTIAL_REJECTED


def test_a_failing_venture_helper_closes_its_gate_and_only_its_gate(
        orch, mp, fc, fq, venture, tmp_path, pages):
    venture.write_text("#!/bin/sh\nexit 3\n")
    with pytest.raises(orch.Stop) as e:
        orch.claude_token("v")
    assert mp.classify(e.value.reason) == "credential"
    lease, db, conf_path = _mech(mp, fc, tmp_path, reason=e.value.reason)
    assert _sweep(mp, db, conf_path) == (1, 0)
    g = fq.gate("credential:v", db)
    assert g["state"] == "closed" and g["evidence"] == lease
    assert fq.gate("credential", db) is None
    assert mp._load_state()["leases"][lease]["held"] == "credential:v"
    # a second plan on v is held; a plan on an operator repository still dispatches
    assert fq.held(db, principal="v") == ("credential:v", None)
    assert fq.held(db, principal="operator") is None
    assert fq.held(db) is None
    # the gate is never probed, and only the owner opens it
    assert mp._gate_probe_model(g, db) is None
    assert fq.open_gate("credential:v", "owner", db=db) is True
    assert fq.held(db, principal="v") is None


def test_the_operators_rejection_still_closes_the_fleet_wide_gate(
        orch, mp, fc, fq, tmp_path, pages):
    lease, db, conf_path = _mech(mp, fc, tmp_path, reason=_reason(orch, AUTH_RESULT))
    _sweep(mp, db, conf_path)
    assert fq.gate("credential", db)["state"] == "closed"
    assert fq.held(db, principal="v") == ("credential", None)     # holds everyone
    assert fq.held(db) == ("credential", None)


def test_a_venture_scope_is_a_valid_scope_for_the_cli(fq):
    assert fq.valid_scope("credential:v") and not fq.valid_scope("credential:")
