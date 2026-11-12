"""Tests for the implementer sandbox argv (plan 0073, ADR-0005 §5 writer half)."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
SEVEN = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"]


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    loader = importlib.machinery.SourceFileLoader("orchestrator_sandbox", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator_sandbox", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _claude_runners(orch):
    return {n: r for n, r in orch.RUNNERS.items()
            if r.argv and os.path.basename(r.argv[0]) == "claude"}


def _arg(argv, flag):
    return argv[list(argv).index(flag) + 1]


def test_every_claude_runner_names_tools_strict_mcp_and_settings(orch):
    runners = _claude_runners(orch)
    assert runners
    for name, r in runners.items():
        assert "--tools" in r.argv, name
        assert _arg(r.argv, "--tools").split(",") == SEVEN, name
        assert "--strict-mcp-config" in r.argv, name
        assert "--settings" in r.argv, name
        # allowedTools stays: -p degrades Bash without it.
        assert "--allowedTools" in r.argv, name
        assert "--restricted" not in r.argv, name   # would ignore the operator's hooks
        assert "--permission-mode" in r.argv and "acceptEdits" in r.argv, name


def test_settings_document_is_this_runners_own(orch):
    for name, r in _claude_runners(orch).items():
        doc = json.loads(_arg(r.argv, "--settings"))
        # Off until the allowlist is re-measured (docs/implementer-sandbox.md):
        # on, it refuses the vault, the ssh push and the git store.
        assert doc["sandbox"]["enabled"] is False, name
        # never lets the model retry a refusal with dangerouslyDisableSandbox
        assert doc["sandbox"]["allowUnsandboxedCommands"] is False, name
        domains = doc["sandbox"]["network"]["allowedDomains"]
        assert "api.anthropic.com" in domains, name
        assert "forge.example" in domains and "127.0.0.1" in domains, name
        assert not any("ollama" in d.lower() for d in domains), name
        creds = doc["sandbox"].get("credentials", {}).get("envVars", [])
        denied = {c["name"] for c in creds}
        # each runner strips exactly the credential its `needs` puts in the env
        want = {"oauth_token": {"CLAUDE_CODE_OAUTH_TOKEN"},
                "api_key": {"ANTHROPIC_API_KEY"}}[orch.RUNNERS[name].needs]
        assert denied == want, name
        assert all(c["mode"] == "deny" for c in creds), name


def test_runner_without_overlay_refuses_rather_than_spawning_unsandboxed(orch):
    with pytest.raises(orch.Stop):
        orch.sandbox_settings("no-such-runner")


def test_settings_document_carries_no_hooks_or_permissions(orch):
    """Additive: nothing here may replace or widen the operator's settings."""
    for name, r in _claude_runners(orch).items():
        doc = json.loads(_arg(r.argv, "--settings"))
        assert set(doc) == {"sandbox"}, name


def test_tool_count_offline_matches_the_named_seven(orch):
    """The shipped argv grants exactly the seven names, nothing wider."""
    for name, r in _claude_runners(orch).items():
        assert sorted(_arg(r.argv, "--tools").split(",")) == sorted(orch.TOOL_ALLOWLIST), name
        assert len(orch.TOOL_ALLOWLIST) == 7


@pytest.mark.skipif(os.environ.get("EUNOMIA_SANDBOX_LIVE") != "1",
                    reason="live arm spends a model call (set EUNOMIA_SANDBOX_LIVE=1)")
def test_live_init_message_reports_only_the_named_tools(orch):
    argv = list(orch.harness_argv(orch.RUNNERS["sub-sonnet"]))
    p = subprocess.run(argv, input="say ok", capture_output=True, text=True, timeout=180)
    init = next(json.loads(l) for l in p.stdout.splitlines()
                if l.startswith("{") and '"subtype":"init"' in l.replace(" ", ""))
    # Measured 2026-10-08, CLI 2.1.295: 6 tools. TodoWrite is named but this
    # build does not ship it, so the assertion is "within the named seven".
    assert init["tools"] and set(init["tools"]) <= set(SEVEN)
    assert "Bash" in init["tools"]
