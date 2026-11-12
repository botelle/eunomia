"""Tests for the two vendor test-lane runners, sub-antigravity and sub-codex
(plan 0066).

No test here spawns either CLI: both bill the owner's personal subscription,
and cihost-linux has neither binary installed. What is testable without a
network and without a model is the registry shape (D1, D3, D4, D4b), the
argv[0]-resolves-without-executing property (DoD, "resolution not spawning"),
the settings precondition (D4b) and the two small pure functions that carry
antigravity's stream-json contract (D2). The manual check the plan's DoD
requires — a real prompt through each stdin path, re-measuring antigravity's
headless default-deny, and a live `settings_must_not_grant` refusal against
the operator's own file — is deliberately not part of this suite.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _load(name, mod_name=None):
    mod_name = mod_name or name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _local_cli(orch):
    """The operator now resolves to `oauth_token` (plan 0116); the vendor runners
    still spend a `local_cli` login, so they are routed with one named outright."""
    return orch.Credential("local_cli", "operator", "a vendor login on this host")


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet"))
    return _load("orchestrator")


# ------------------------------------------------------------------ D1 / D3


def test_sub_antigravity_and_sub_codex_are_registered(orch):
    assert {"sub-antigravity", "sub-codex"} <= set(orch.RUNNERS)


def test_their_argv0_is_absolute(orch):
    assert orch.RUNNERS["sub-antigravity"].argv[0] == orch.ANTIGRAVITY_BIN
    # sub-codex starts with the sandbox, then the codex it confines (plan 0101)
    assert orch.RUNNERS["sub-codex"].argv[0] == "/usr/bin/sandbox-exec"
    assert orch.CODEX_BIN in orch.RUNNERS["sub-codex"].argv
    assert os.path.isabs(orch.ANTIGRAVITY_BIN)
    assert os.path.isabs(orch.CODEX_BIN)


def test_every_non_claude_non_fable_runner_argv0_is_absolute(orch):
    """Registry-wide, so the NEXT vendor entry cannot reintroduce the
    bare-name failure this fleet already recorded once: "runner 'sub-sonnet'
    needs 'claude', which is not on PATH on this host — refusing to spawn".

    Scoped past the Claude-family runners (`claude`, `fable`): fixing their
    own bare `"claude"` argv[0] is a separate, pre-existing issue outside this
    plan's `paths:` lane, and dozens of existing tests in
    tests/test_orchestrator_session.py depend on stubbing a fake `claude` ON
    PATH — an absolute, hardcoded argv[0] would stop that stub from ever being
    found and would try to exec whatever real binary happens to sit at that
    path on the host running the suite."""
    for name, r in orch.RUNNERS.items():
        if not r.argv or r.family in ("claude", "fable"):
            continue
        assert os.path.isabs(r.argv[0]), name


def test_their_family_is_the_vendor_not_a_shared_claude_family(orch):
    assert orch.RUNNERS["sub-antigravity"].family == "google"
    assert orch.RUNNERS["sub-codex"].family == "openai"


def test_they_are_not_local_so_the_zone_allowlist_still_gates_them(orch):
    local = {n for n, r in orch.RUNNERS.items() if r.local}
    assert all(n.startswith("local-") for n in local)
    assert orch.RUNNERS["sub-antigravity"].local is False
    assert orch.RUNNERS["sub-codex"].local is False


@pytest.mark.parametrize("name", ["sub-antigravity", "sub-codex"])
def test_they_spend_the_operators_own_login_not_an_api_key(orch, name):
    assert orch.RUNNERS[name].needs == "local_cli"


# --------------------------------------------------------------------- D4b


def test_sub_antigravity_argv_equals_its_pinned_literal_tuple(orch):
    r = orch.RUNNERS["sub-antigravity"]
    assert r.argv == (
        orch.ANTIGRAVITY_BIN, "--print=", "--input-format", "stream-json",
        "--output-format", "stream-json", "--mode", "plan", "--sandbox",
        "--json-schema", "<schema>", "--model", "gemini-4-argon-high",
    )


def test_sub_codex_argv_equals_its_pinned_literal_tuple(orch):
    r = orch.RUNNERS["sub-codex"]
    assert r.argv == (
        "/usr/bin/sandbox-exec", "-f", "<profile>",
        "-D", "LANE=<lane>", "-D", "CODEX_HOME=<codex-home>",
        orch.CODEX_BIN, "exec", "-s", "read-only", "--skip-git-repo-check",
        "--ignore-user-config", "--ephemeral", "--ignore-rules", "--json",
        "-c", 'sqlite_home="<lane>"',
        "--output-schema", "<schema>", "-o", "<out>",
        "-m", "gpt-6-astra",
    )


def test_sub_codex_confinement_is_in_the_argv_itself_not_in_a_wrapper(orch):
    """Plan 0101 D2: every spawn path that reaches this runner gets the
    confined form or a failure, including one that does not exist yet."""
    argv = orch.RUNNERS["sub-codex"].argv
    assert argv[0] == "/usr/bin/sandbox-exec"          # absolute: no PATH lookup, no fallback
    assert argv.index(orch.CODEX_BIN) > argv.index("<profile>")


def test_sub_antigravity_carries_no_add_dir(orch):
    assert "--add-dir" not in orch.RUNNERS["sub-antigravity"].argv


@pytest.mark.parametrize("name", ["sub-antigravity", "sub-codex"])
def test_neither_vendor_runner_may_skip_permissions(orch, name):
    argv = orch.RUNNERS[name].argv
    assert "--dangerously-skip-permissions" not in argv
    # codex's own spellings (plan 0101): the obvious "fix" when its inner
    # sandbox fails to apply inside the profile, and it would make the posture
    # depend on the profile never having a hole
    assert not [a for a in argv if a.startswith("--dangerously-")]
    for flag in ("--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"):
        assert flag not in argv


def test_sub_fable_declares_no_constraint_flags_and_is_unaffected(orch):
    """The constraint-flag rule is per runner, not per position: sub-fable
    (plan 0065) declares none of antigravity's or codex's flags and must not
    be refused by a literal reading of the DoD bullet about a runner missing
    one it declared. Its own pinned shape (a Claude runner) is untouched by
    this plan."""
    r = orch.RUNNERS["sub-fable"]
    for flag in ("--mode", "--sandbox", "-s", "--skip-git-repo-check",
                 "--ignore-user-config", "--json-schema", "--output-schema"):
        assert flag not in r.argv
    assert r.family == "fable"


# ------------------------------------------------------------- unwired / D5


def test_sub_codex_is_wired_only_inside_its_sandbox_profile(orch):
    """Plan 0101 D5: wired, and the confinement is the argv's own first word,
    so no spawn path can reach codex without it."""
    r = orch.RUNNERS["sub-codex"]
    assert r.unwired == ""
    assert r.argv[0] == "/usr/bin/sandbox-exec"
    assert r.argv.index("<profile>") < r.argv.index(orch.CODEX_BIN)
    assert not any(a.startswith("--dangerously") for a in r.argv)


def test_sub_antigravity_is_wired(orch):
    assert orch.RUNNERS["sub-antigravity"].unwired == ""


# ----------------------------------------------------- resolution not spawning


@pytest.mark.parametrize("name", ["sub-antigravity", "sub-codex"])
def test_argv0_resolves_under_the_launchd_path_without_spawning(orch, monkeypatch, name):
    """The launchd case that has already failed here: PATH=/usr/bin:/bin:
    /usr/sbin:/sbin, no /opt/homebrew/bin. Resolution only, never a spawn —
    existence is asserted only when the binary is actually on THIS host,
    because cihost-linux has neither CLI and a suite requiring them would turn
    a missing binary into a red build about something else."""
    runner = orch.RUNNERS[name]
    if not os.path.exists(runner.argv[0]):
        pytest.skip(f"{runner.argv[0]} is not installed on this host")
    monkeypatch.setenv("PATH", MINIMAL_PATH)
    if runner.unwired:
        # harness_argv refuses an unwired runner before it ever resolves
        # PATH, so the resolution itself is checked with the same lookup
        # harness_argv would make once wired.
        assert shutil.which(runner.argv[0], path=os.environ["PATH"]) == runner.argv[0]
        return
    argv = orch.harness_argv(runner)
    assert argv[0] == runner.argv[0]


# ------------------------------------------------------------------- zone


@pytest.mark.parametrize("name", ["sub-antigravity", "sub-codex"])
def test_a_private_zone_plan_cannot_resolve_a_vendor_runner(orch, name):
    cred = _local_cli(orch)
    with pytest.raises(orch.Stop) as e:
        orch.route_implementer({"zone": "private"}, cred,
                               env={"EUNOMIA_IMPL_RUNNER": name})
    assert "not 'public'" in e.value.reason


@pytest.mark.parametrize("name", ["sub-antigravity", "sub-codex"])
def test_a_public_zone_plan_routes_to_the_vendor_runner(orch, name):
    cred = _local_cli(orch)
    route = orch.route_implementer({"zone": "public"}, cred,
                                   env={"EUNOMIA_IMPL_RUNNER": name})
    assert route.name == name


# --------------------------------------------------------- settings_must_not_grant


def test_every_antigravity_runner_declares_settings_must_not_grant(orch):
    # plan 0202: every antigravity model variant inherits the precondition
    # from its template, and no other runner declares one.
    antigravity = {n for n, r in orch.RUNNERS.items()
                   if r.argv and r.argv[0] == orch.ANTIGRAVITY_BIN}
    assert antigravity == {"sub-antigravity", "sub-antigravity-flash"}
    for name, r in orch.RUNNERS.items():
        if name in antigravity:
            assert r.settings_must_not_grant == (
                "~/.gemini/antigravity-cli/settings.json",)
        else:
            assert r.settings_must_not_grant == (), name


def test_settings_precondition_refuses_a_permissions_allow_entry(orch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(*)"]}}))
    runner = orch.RUNNERS["sub-antigravity"]._replace(
        settings_must_not_grant=(str(settings),))
    with pytest.raises(orch.Stop) as e:
        orch.check_settings_precondition(runner)
    assert str(settings) in e.value.reason
    assert "permissions.allow" in e.value.reason


def test_settings_precondition_refuses_a_hooks_block(orch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    runner = orch.RUNNERS["sub-antigravity"]._replace(
        settings_must_not_grant=(str(settings),))
    with pytest.raises(orch.Stop) as e:
        orch.check_settings_precondition(runner)
    assert str(settings) in e.value.reason
    assert "hooks" in e.value.reason


def test_settings_precondition_passes_a_fixture_with_neither(orch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    runner = orch.RUNNERS["sub-antigravity"]._replace(
        settings_must_not_grant=(str(settings),))
    assert orch.check_settings_precondition(runner) is True


def test_settings_precondition_passes_when_the_file_is_absent(orch, tmp_path):
    runner = orch.RUNNERS["sub-antigravity"]._replace(
        settings_must_not_grant=(str(tmp_path / "nope.json"),))
    assert orch.check_settings_precondition(runner) is True


def test_settings_precondition_reads_the_declared_tilde_path(orch, tmp_path, monkeypatch):
    """The registry entry itself, not a fixture path substituted in — proving
    `~/.gemini/antigravity-cli/settings.json` is read from HOME rather than
    assumed. Pinned to this exact path because antigravity's own embedded docs
    ("## 3. Configuration") name it, not `~/.gemini/settings.json` — that file
    belongs to the legacy `gemini` CLI and antigravity never reads it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["Bash(rm -rf /)"]}}))
    with pytest.raises(orch.Stop) as e:
        orch.check_settings_precondition(orch.RUNNERS["sub-antigravity"])
    assert "settings.json" in e.value.reason


def test_settings_precondition_clean_declared_path_passes(orch, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "settings.json").write_text(json.dumps({"theme": "dark"}))
    assert orch.check_settings_precondition(orch.RUNNERS["sub-antigravity"]) is True


# ------------------------------------------------------- the stdin/result shapes


def test_antigravity_stdin_line_matches_the_measured_shape(orch):
    """One line, `{"event":"user","message":{"role":"user","content":<prompt>}}`
    — measured against a real request 2026-09-22 (plan 0066 D2)."""
    line = orch.antigravity_stdin_line("do the thing")
    assert json.loads(line) == {
        "event": "user",
        "message": {"role": "user", "content": "do the thing"},
    }
    assert "\n" not in line


# The stream shape of antigravity 1.3.3, measured 2026-10-12: the fields ride
# inside `result`, and a non-JSON line from the CLI sits between events.
SAMPLE_STREAM = (
    'Warning: some deprecation notice antigravity writes to the same stream\n'
    '{"event":"init","init":{"permission_mode":"request-review"}}\n'
    '{"event":"result","result":{"status":"SUCCESS","response":"42","num_turns":3,'
    '"usage":{"input_tokens":10,"output_tokens":5}}}\n'
)

DENIED_THEN_ANSWERED = (
    '{"event":"result","result":{"status":"SUCCESS","response":"","num_turns":1,'
    '"denied_actions":[{"action":"command","display_name":"RunCommand"}]}}\n'
    'jetski: no output produced\n'
    '{"event":"result","result":{"status":"SUCCESS","response":"{}","num_turns":3}}\n'
)


def test_read_antigravity_result_skips_the_interleaved_non_json_line(orch):
    result = orch.read_antigravity_result(SAMPLE_STREAM)
    assert result == {
        "status": "SUCCESS", "response": "42", "num_turns": 3,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_read_antigravity_result_takes_the_first_turn_that_answered(orch):
    assert orch.read_antigravity_result(DENIED_THEN_ANSWERED)["num_turns"] == 3
    both = SAMPLE_STREAM + DENIED_THEN_ANSWERED
    assert orch.read_antigravity_result(both)["response"] == "42"


def test_read_antigravity_result_is_the_last_turn_when_none_answered(orch):
    denied = DENIED_THEN_ANSWERED.splitlines()[0] + "\n"
    result = orch.read_antigravity_result(denied + denied.replace('"num_turns":1', '"num_turns":2'))
    assert result["response"] == "" and result["num_turns"] == 2
    assert result["denied_actions"] == [{"action": "command", "display_name": "RunCommand"}]


def test_read_antigravity_result_keeps_a_result_line_with_no_inner_object(orch):
    flat = '{"event":"result","status":"ok","response":"42"}\n'
    assert orch.read_antigravity_result(flat)["response"] == "42"


def test_read_antigravity_result_is_none_with_no_result_event(orch):
    assert orch.read_antigravity_result('{"event":"system"}\n') is None


def test_read_antigravity_result_is_none_on_an_empty_stream(orch):
    assert orch.read_antigravity_result("") is None


# -------------------------------------------------------------------- docs


def test_docs_state_tester_rows_are_recorded_and_not_yet_read():
    doc = (ROOT / "docs" / "per-repo-models.md").read_text()
    assert "tester" in doc
    assert "0013" in doc
    assert "sub-antigravity" in doc and "sub-codex" in doc
