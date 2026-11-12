"""Tests for plan 0219: `fleet-followups triage` spends the vault's Claude
token (reusing `bin/orchestrator`'s `claude_token()`, exactly as plan 0116
wired the implementer), and a session that fails says why instead of every
failure reading "no output".

Reuses tests/test_fleet_followups.py's module loader and fakes, the way
test_implementer_credential.py reuses test_fleet_quota.py's fixtures: no test
here touches the vault or holds a real token.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from test_fleet_followups import (  # noqa: F401  (helpers reused by name)
    _mod, make_origin, FakeForge, REPO, OWNER_UID, failed_comments,
)

STUB_TOKEN = "stub-claude-oauth-SECRETVALUE-0123456789"


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """A stub `FLEET_CLAUDE_TOKEN_CMD`; `body` rewrites what it does."""
    path = tmp_path / "claude-token.sh"

    def write(body):
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        monkeypatch.setenv("FLEET_CLAUDE_TOKEN_CMD", str(path))
    write(f"echo {STUB_TOKEN}")
    write.path = path
    return write


@pytest.fixture
def env(tmp_path, monkeypatch, helper):
    """Like test_fleet_followups.env, but `orch.claude_token()` is left real
    (reading the stub `helper` above), since that is exactly the path these
    tests check."""
    mod = _mod(monkeypatch, tmp_path)
    make_origin(tmp_path, {"src/a.py": "x = 1\n"})
    return mod, FakeForge()


def _at(mod):
    return mod.watch._ts_utc("2026-09-27T12:00:00Z")


def _events(mod):
    path = Path(os.environ["EUNOMIA_FLEET_DIR"]) / "events.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


# --------------------------------------------------------- D1 the token spend


def test_the_token_reaches_the_env_not_argv_and_not_a_log(env, monkeypatch, capsys):
    mod, forge = env
    forge.add_issue(1, 7, ["a"])
    seen = {}
    real_run = subprocess.run

    def fake_run(argv, **kw):
        if "--output-format" not in argv:   # the token helper or `git`; let it run
            return real_run(argv, **kw)
        seen["argv"] = list(argv)
        seen["env"] = dict(kw.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
            {"result": '```json\n{"items": [{"id": "1", "verdict": "unclear"}]}\n```'}))
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    mod.triage_run(forge, [REPO], 10, OWNER_UID, None, at=_at(mod))

    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == STUB_TOKEN
    assert not any(STUB_TOKEN in a for a in seen["argv"])
    out = capsys.readouterr()
    assert STUB_TOKEN not in out.out and STUB_TOKEN not in out.err
    assert all(STUB_TOKEN not in json.dumps(e) for e in _events(mod))


def test_a_failing_helper_stops_the_run_before_any_session_and_marks_no_issue(
        env, helper):
    mod, forge = env
    forge.add_issue(1, 7, ["a"])
    helper("echo denied >&2; exit 1")

    with pytest.raises(mod.Refused) as e:
        mod.triage_run(forge, [REPO], 10, OWNER_UID, None,
                       spawn=lambda *a, **k: pytest.fail("a session was spawned"),
                       at=_at(mod))
    assert "FLEET_CLAUDE_TOKEN_CMD" in str(e.value)
    assert not forge.comments[(REPO, 1)]


def test_a_missing_helper_also_stops_before_any_session(env, monkeypatch):
    mod, forge = env
    forge.add_issue(1, 7, ["a"])
    monkeypatch.setenv("FLEET_CLAUDE_TOKEN_CMD", "/no/such/helper-0219")

    with pytest.raises(mod.Refused):
        mod.triage_run(forge, [REPO], 10, OWNER_UID, None,
                       spawn=lambda *a, **k: pytest.fail("a session was spawned"),
                       at=_at(mod))
    assert not forge.comments[(REPO, 1)]


def test_no_issue_fetches_no_token(env, monkeypatch):
    mod, forge = env
    monkeypatch.setattr(mod.orch, "claude_token",
                        lambda principal="operator": pytest.fail("token fetched"))
    assert mod.triage_run(forge, [REPO], 10, OWNER_UID, None, at=_at(mod)) == {
        "triaged": {}, "n": 0}


# -------------------------------------------------------- D2 a reasoned failure


def test_an_authentication_error_is_logged_with_its_own_reason(env, monkeypatch, capsys):
    mod, forge = env
    forge.add_issue(1, 7, ["a"])
    real_run = subprocess.run

    def fake_run(argv, **kw):
        if "--output-format" not in argv:   # the token helper or `git`; let it run
            return real_run(argv, **kw)
        return subprocess.CompletedProcess(argv, 1, stdout=json.dumps(
            {"is_error": True, "result": "Failed to authenticate: OAuth session "
             "expired and could not be refreshed"}), stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    mod.triage_run(forge, [REPO], 10, OWNER_UID, None, at=_at(mod))

    body = failed_comments(forge, 1)[0]["body"]
    assert ("triage failed:** Failed to authenticate: OAuth session expired "
            "and could not be refreshed") in body
    err = capsys.readouterr().err
    assert ("1#1: triage failed — Failed to authenticate: OAuth session "
            "expired and could not be refreshed" in err
            or "triage failed — Failed to authenticate: OAuth session expired "
               "and could not be refreshed" in err)


def test_a_long_is_error_result_is_capped_at_200_characters(env, monkeypatch):
    mod, forge = env
    forge.add_issue(1, 7, ["a"])
    long_reason = "Failed to authenticate: " + "x" * 300
    real_run = subprocess.run

    def fake_run(argv, **kw):
        if "--output-format" not in argv:   # the token helper or `git`; let it run
            return real_run(argv, **kw)
        return subprocess.CompletedProcess(argv, 1, stdout=json.dumps(
            {"is_error": True, "result": long_reason}), stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    mod.triage_run(forge, [REPO], 10, OWNER_UID, None, at=_at(mod))
    body = failed_comments(forge, 1)[0]["body"]
    assert "x" * 300 not in body
    assert "…" in body


def _tree(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    return tree


def test_a_timeout_is_its_own_reason(env, tmp_path, monkeypatch):
    mod, _ = env

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    text, usage, reason = mod.spawn_session(mod.resolve_runner("sub-sonnet"),
                                             _tree(tmp_path), "PROMPT", "tok")
    assert (text, usage) == (None, None)
    assert "timed out" in reason and str(mod.SESSION_TIMEOUT) in reason


def test_a_non_json_non_zero_exit_names_the_exit_code_and_last_stderr_line(
        env, tmp_path, monkeypatch):
    mod, _ = env

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 17, stdout="not json at all",
                                           stderr="context\nboom: permission denied\n")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    text, usage, reason = mod.spawn_session(mod.resolve_runner("sub-sonnet"),
                                             _tree(tmp_path), "PROMPT", "tok")
    assert (text, usage) == (None, None)
    assert reason == "exit 17: boom: permission denied"


def test_an_empty_exit_zero_still_reads_no_output(env, tmp_path, monkeypatch):
    mod, _ = env

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    text, usage, reason = mod.spawn_session(mod.resolve_runner("sub-sonnet"),
                                             _tree(tmp_path), "PROMPT", "tok")
    assert (text, usage, reason) == (None, None, "the session produced no output")
