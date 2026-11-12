"""The implementer spends a minted token, not the operator's Keychain login
(plan 0116).

The token helper is a script on a temporary FLEET_CLAUDE_TOKEN_CMD: no test here
touches the vault or holds a real token. The mopsus and fleet-quota halves run
against the real modules and the real ledger, as tests/test_fleet_quota.py does.
"""
import json
import os
import subprocess

import pytest

from test_fleet_quota import (  # noqa: F401  (fixtures are imported by name)
    fq, mp, fc, pages, _sweep, _snapshot, _ago, _ts, _record_starts, _events,
)
from test_mopsus import _mech

STUB = "stub-claude-oauth-SECRETVALUE-0123456789"

# The 2026-10-05 outage, as the CLI wrote it: `subtype` and `stop_reason` read
# like a clean finish, and are not signals.
AUTH_RESULT = {
    "type": "result", "subtype": "success", "is_error": True,
    "terminal_reason": "api_error", "stop_reason": "stop_sequence",
    "result": "Failed to authenticate: OAuth session expired and could not be "
              "refreshed",
}
OVERLOAD_RESULT = {
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 529, "result": "API Error: Overloaded",
}
RATE_RESULT = {
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 429, "result": "You've hit your session limit",
}


@pytest.fixture
def orch(mp):
    return mp.orch


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """A stub token helper; `body` rewrites what it does."""
    path = tmp_path / "claude-token.sh"

    def write(body):
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        monkeypatch.setenv("FLEET_CLAUDE_TOKEN_CMD", str(path))
    write(f"echo {STUB}")
    write.path = path
    return write


def _transcript(*records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _reason(orch, rec):
    msg = orch.read_auth_rejection(_transcript({"type": "system"}, rec))
    return None if msg is None else f"{orch.CREDENTIAL_REJECTED}: {msg}"


# --- 1. the credential kind ---------------------------------------------------


def test_the_operator_resolves_to_oauth_token_and_every_claude_runner_needs_it(
        orch, monkeypatch):
    monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
    assert orch.resolve_credential().kind == "oauth_token"
    # every claude-harness runner (family claude or fable, both resolve to
    # bin/fleet-models runner_harness()=="claude") except api-sonnet needs the
    # subscription's oauth token (plan 0202 added sub-haiku to this set, not a
    # new rule)
    claude = [r for r in orch.RUNNERS.values() if r.family in ("claude", "fable")
              and r.name != "api-sonnet"]
    assert {r.name for r in claude} == {"sub-sonnet", "sub-opus", "sub-fable",
                                        "sub-opus-5-5", "sub-sonnet-5",
                                        "sub-haiku"}
    for r in claude:
        assert r.needs == "oauth_token"
        assert r.extra_env == ("CLAUDE_CODE_OAUTH_TOKEN",)
    for name in ("sub-antigravity", "sub-codex"):
        assert orch.RUNNERS[name].needs == "local_cli"
    assert orch.RUNNERS["api-sonnet"].needs == "api_key"


# --- 2. the token reaches the child and nothing else --------------------------


class _Spawned(Exception):
    def __init__(self, env):
        self.env = env


def _spawn_env(orch, monkeypatch, tmp_path, route):
    monkeypatch.setattr(orch, "harness_argv", lambda r: ["child"])

    real_popen = subprocess.Popen

    def popen(argv, *a, **kw):
        if argv != ["child"]:           # the helper runs through subprocess.run
            return real_popen(argv, *a, **kw)
        raise _Spawned(kw["env"])
    monkeypatch.setattr(orch.subprocess, "Popen", popen)
    cred = orch.Credential("oauth_token", "operator", "")
    with pytest.raises(_Spawned) as e:
        orch.run_implementer(tmp_path, "prompt", cred, route, "L1")
    return e.value.env


def test_the_child_gets_the_token_and_os_environ_does_not(
        orch, helper, monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    before = dict(os.environ)
    env = _spawn_env(orch, monkeypatch, tmp_path, orch.RUNNERS["sub-sonnet"])
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == STUB
    assert dict(os.environ) == before
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_the_last_non_empty_line_is_the_token(orch, helper, monkeypatch, tmp_path):
    helper(f"echo 'minting...'; echo {STUB}; echo")
    env = _spawn_env(orch, monkeypatch, tmp_path, orch.RUNNERS["sub-opus"])
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == STUB


@pytest.mark.parametrize("name", ["local-qwen", "sub-codex", "sub-antigravity"])
def test_other_runners_never_get_the_variable_even_when_the_parent_has_it(
        orch, monkeypatch, name):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", STUB)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in orch.implementer_env(orch.RUNNERS[name])
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in orch._ENV_ALLOW


# --- 3. a helper that fails stops the run, quoting nothing --------------------


@pytest.mark.parametrize("body", [
    f"echo {STUB}; exit 3",
    "exit 0",
    "echo; echo",
    "sleep 5",
], ids=["nonzero", "silent", "blank", "timeout"])
def test_a_failing_helper_raises_stop_and_spawns_nothing(
        orch, helper, monkeypatch, tmp_path, body):
    helper(body)
    real_run = subprocess.run
    if body.startswith("sleep"):
        def quick(*a, **kw):
            kw["timeout"] = 0.2
            return real_run(*a, **kw)
        monkeypatch.setattr(orch.subprocess, "run", quick)
    monkeypatch.setattr(orch, "harness_argv", lambda r: ["child"])
    real_popen = subprocess.Popen

    def popen(argv, *a, **kw):
        if argv == ["child"]:
            pytest.fail("a child was spawned without a token")
        return real_popen(argv, *a, **kw)
    monkeypatch.setattr(orch.subprocess, "Popen", popen)
    cred = orch.Credential("oauth_token", "operator", "")
    with pytest.raises(orch.Stop) as e:
        orch.run_implementer(tmp_path, "prompt", cred, orch.RUNNERS["sub-sonnet"], "L1")
    reason = e.value.reason
    assert reason.startswith(orch.CREDENTIAL_REJECTED)
    assert "FLEET_CLAUDE_TOKEN_CMD" in reason
    assert STUB not in reason and "SECRETVALUE" not in reason


def test_a_missing_helper_raises_stop(orch, monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_CLAUDE_TOKEN_CMD", str(tmp_path / "nope.sh"))
    with pytest.raises(orch.Stop) as e:
        orch.claude_token()
    assert e.value.reason.startswith(orch.CREDENTIAL_REJECTED)


# --- 4. classification and the gate -------------------------------------------


def test_the_2026_10_05_record_is_a_credential_failure(orch, mp):
    reason = _reason(orch, AUTH_RESULT)
    assert "Failed to authenticate" in reason
    assert mp.classify(reason) == "credential"


@pytest.mark.parametrize("status", [401, 403])
def test_a_401_or_403_is_one_whatever_it_says(orch, mp, status):
    rec = {"type": "result", "is_error": True, "api_error_status": status,
           "result": "something unrecognised"}
    assert mp.classify(_reason(orch, rec)) == "credential"


@pytest.mark.parametrize("phrase", ["Not logged in", "Invalid API key"])
def test_the_other_auth_phrases_are_credential_failures(orch, mp, phrase):
    rec = {"type": "result", "is_error": True, "result": phrase + " · Please run /login"}
    assert mp.classify(_reason(orch, rec)) == "credential"


def test_a_429_or_an_overload_is_not_a_credential_failure(orch, mp):
    for rec in (RATE_RESULT, OVERLOAD_RESULT):
        assert orch.read_auth_rejection(_transcript(rec)) is None
    assert mp.classify("session ended on a backend error before any work: API 529 "
                       "after 2s — API Error: Overloaded") != "credential"
    # a clean result is not one either, whatever its subtype says
    ok = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    assert orch.read_auth_rejection(_transcript(ok)) is None
    # only the LAST result counts
    assert orch.read_auth_rejection(_transcript(AUTH_RESULT, ok)) is None


def test_a_credential_failure_closes_the_gate_with_the_lease_as_evidence(
        orch, mp, fc, fq, tmp_path, pages):
    lease, db, conf = _mech(mp, fc, tmp_path, reason=_reason(orch, AUTH_RESULT))
    assert _sweep(mp, db, conf) == (1, 0)
    g = fq.gate("credential", db)
    assert g["state"] == "closed" and g["closed_by"] == "stop"
    assert g["evidence"] == lease and g["resets_at"] is None
    assert mp._load_state()["leases"][lease]["held"] == "credential"
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert len(_events(tmp_path, "quota-closed")) == 1
    assert any("credential" in p for p in pages)


# --- 5. only the owner opens it -----------------------------------------------


def test_a_closed_credential_gate_holds_dispatch(fq, tmp_path):
    fq.close("credential", None, "stop", "L1", at=_ts(_ago(30)))
    assert fq.held() == ("credential", None)
    assert fq.check()[0] == fq.EX_CLOSED


def test_the_meter_cannot_open_it_and_no_probe_starts(
        mp, fq, tmp_path, monkeypatch):
    db = tmp_path / ".fleet" / "fleet.db"
    started = _record_starts(mp, monkeypatch)
    fq.close("credential", None, "stop", "L1", at=_ts(_ago(30)), db=db)
    _snapshot(tmp_path, _ago(1), session=0, weekly=0, fable=0, credential=0)
    assert fq.observe(db=db) == []
    assert fq.gate("credential", db)["state"] == "closed"
    mp.quota_probe_sweep(db)
    assert started == []
    assert mp._gate_probe_model(fq.gate("credential", db), db) is None


# --- 6. the owner opens it, and what was held is requeued ---------------------


def test_the_owner_opens_it_and_a_held_failure_without_a_pr_is_released(
        orch, mp, fc, fq, tmp_path, capsys):
    lease, db, conf = _mech(mp, fc, tmp_path, reason=_reason(orch, AUTH_RESULT))
    _sweep(mp, db, conf)
    assert _events(tmp_path, "plan-requeued") == []
    assert fq.main(["open", "credential", "--by", "owner"]) == 0
    assert capsys.readouterr().out.strip() == "opened"
    assert _sweep(mp, db, conf) == (0, 0)
    (ev,) = _events(tmp_path, "plan-requeued")
    assert ev["lease"] == lease and ev["detail"]["class"] == "credential"
    assert mp.lib.read_lease(lease)["state"] == "released"


def test_a_held_failure_with_a_marked_pr_is_left_alone(
        orch, mp, fc, fq, tmp_path, monkeypatch):
    lease, db, conf = _mech(mp, fc, tmp_path, reason=_reason(orch, AUTH_RESULT))
    _sweep(mp, db, conf)
    fq.open_gate("credential", "owner", db=db)
    monkeypatch.setattr(mp.fwatch, "marked_pr",
                        lambda repo, plan_id, token, dry_run=False: {"number": 5})
    _sweep(mp, db, conf)
    assert _events(tmp_path, "plan-requeued") == []
    assert mp.lib.read_lease(lease)["state"] == "active"


def test_credential_is_not_retryable_by_the_retry_limit(mp):
    assert "credential" not in mp.RETRYABLE_CLASSES
    assert "credential" in mp.GATED_CLASSES


# --- 7. the token appears nowhere on disk -------------------------------------


def test_no_log_event_or_page_carries_the_token(
        orch, mp, fc, fq, helper, tmp_path, monkeypatch, pages):
    helper(f"echo {STUB}; exit 3")
    with pytest.raises(orch.Stop) as e:
        orch.claude_token()
    # the refusal rides into the reason, the class, the gate and the brief
    lease, db, conf = _mech(mp, fc, tmp_path, reason=e.value.reason)
    assert mp.classify(e.value.reason) == "credential"
    _sweep(mp, db, conf)
    fq.open_gate("credential", "owner", db=db)
    _sweep(mp, db, conf)
    assert not any(STUB in p for p in pages)
    for root, _dirs, files in os.walk(tmp_path):
        for f in files:
            p = os.path.join(root, f)
            if p == str(helper.path):
                continue
            try:
                text = open(p, errors="replace").read()
            except OSError:
                continue
            assert STUB not in text, p
