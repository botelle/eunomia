"""Plan 0202: a runner for every catalog model the owner tiered.

operator/modeltiers plan 0004 named five ids — gpt-6.1-sol, gpt-6.1-terra,
gpt-6-luna, gemini-3.8-flash-high, claude-haiku-5-5 — that no seat could
select until `RUNNERS` carried them. This file proves each one is wired the
same way as the base runner it rides on, and that the two base runners
themselves are untouched.
"""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

CODEX_VARIANTS = (("sub-codex-sol", "gpt-5.6-sol"),
                  ("sub-codex-terra", "gpt-5.6-terra"),
                  ("sub-codex-luna", "gpt-5.6-luna"))
ANTIGRAVITY_VARIANTS = (("sub-antigravity-flash", "gemini-3.8-flash-high"),)
ALL_VARIANTS = CODEX_VARIANTS + ANTIGRAVITY_VARIANTS


def _load(name, mod_name=None):
    mod_name = mod_name or name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def orch():
    return _load("orchestrator")


@pytest.fixture
def fm():
    return _load("fleet-models")


REVIEWER_POSITIONS = ("plan_reviewer", "code_reviewer", "style_reviewer",
                      "lane_reviewer")
ALL_POSITIONS = REVIEWER_POSITIONS + ("implementer",)


# ------------------------------------------------------------------ D1


@pytest.mark.parametrize("name,model", CODEX_VARIANTS + ANTIGRAVITY_VARIANTS
                         + (("sub-haiku", "claude-haiku-5-5"),))
def test_the_runner_exists_and_pins_the_exact_id(orch, name, model):
    r = orch.RUNNERS[name]
    assert r.model == model


@pytest.mark.parametrize("name,family,harness", [
    ("sub-codex-sol", "openai", "codex"),
    ("sub-codex-terra", "openai", "codex"),
    ("sub-codex-luna", "openai", "codex"),
    ("sub-antigravity-flash", "google", "antigravity"),
    ("sub-haiku", "claude", "claude"),
])
def test_family_and_harness_match_d1(orch, fm, name, family, harness):
    r = orch.RUNNERS[name]
    assert r.family == family
    assert r.provenance == "us-hosted"
    assert fm.runner_harness(r) == harness


@pytest.mark.parametrize("name", [n for n, _ in ALL_VARIANTS] + ["sub-haiku"])
@pytest.mark.parametrize("position", ALL_POSITIONS)
def test_validate_runner_passes_at_every_position(fm, name, position):
    fm.validate_runner(name, position)


@pytest.mark.parametrize("name", ["sub-codex-sol", "sub-codex-terra", "sub-codex-luna"])
def test_codex_variants_spend_the_codex_account(orch, name):
    r = orch.RUNNERS[name]
    assert orch._VENDOR_ACCOUNT[r.family] == "codex"
    assert orch.runner_account(r, "operator/x") == "codex"
    assert orch.runner_account(r, "operator/x", "implementer") == "codex"


def test_flash_spends_the_gemini_account(orch):
    r = orch.RUNNERS["sub-antigravity-flash"]
    assert orch._VENDOR_ACCOUNT[r.family] == "gemini"
    assert orch.runner_account(r, "operator/x") == "gemini"
    assert orch.runner_account(r, "operator/x", "implementer") == "gemini"


def test_haiku_spends_the_claude_account(orch):
    r = orch.RUNNERS["sub-haiku"]
    assert orch.runner_account(r, "operator/x", "code_reviewer") == "claude"


# ----------------------------------------------- the helper's field equality


@pytest.mark.parametrize("name,base", [(n, "sub-codex") for n, _ in CODEX_VARIANTS]
                         + [(n, "sub-antigravity") for n, _ in ANTIGRAVITY_VARIANTS])
def test_a_variant_equals_its_base_on_every_field_but_name_model_and_the_id(
        orch, name, base):
    variant, base_r = orch.RUNNERS[name], orch.RUNNERS[base]
    changed = {f for f in variant._fields if getattr(variant, f) != getattr(base_r, f)}
    assert changed == {"name", "model", "argv", "writer_argv"}
    # and within argv/writer_argv, only the model id token itself moved
    assert [a for a in variant.argv if a not in base_r.argv] == [variant.model]
    assert [a for a in variant.writer_argv if a not in base_r.writer_argv] == [variant.model]


def test_sub_codex_is_a_literal_copy_of_todays_runner(orch):
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
    assert r.writer_argv == (
        orch.CODEX_BIN, "exec", "-s", "workspace-write",
        "--skip-git-repo-check", "--ephemeral", "--ignore-rules",
        "--json", "-m", "gpt-6-astra", "-",
    )
    assert (r.local, r.needs, r.extra_env, r.unwired, r.family, r.model,
           r.provenance) == (False, "local_cli", (), "", "openai", "gpt-6-astra",
                             "us-hosted")


def test_sub_antigravity_is_a_literal_copy_of_todays_runner(orch):
    r = orch.RUNNERS["sub-antigravity"]
    assert r.argv == (
        orch.ANTIGRAVITY_BIN, "--print=", "--input-format", "stream-json",
        "--output-format", "stream-json", "--mode", "plan", "--sandbox",
        "--json-schema", "<schema>", "--model", "gemini-4-argon-high",
    )
    assert r.writer_argv == (
        orch.ANTIGRAVITY_BIN, "--print=", "--input-format", "stream-json",
        "--output-format", "stream-json", "--mode", "accept-edits",
        "--sandbox", "--disable-slash-commands", "--model", "gemini-4-argon-high",
    )
    assert (r.local, r.needs, r.extra_env, r.unwired, r.family, r.model,
           r.provenance, r.settings_must_not_grant) == (
        False, "local_cli", (), "", "google", "gemini-4-argon-high",
        "us-hosted", ("~/.gemini/antigravity-cli/settings.json",))


def test_flash_still_declares_settings_must_not_grant(orch):
    assert orch.RUNNERS["sub-antigravity-flash"].settings_must_not_grant == (
        "~/.gemini/antigravity-cli/settings.json",)


def test_an_argon_seat_spends_gemini_and_argons_own_limit(orch):
    assert orch.runner_accounts(orch.RUNNERS["sub-antigravity"], "operator/x",
                                "lane_reviewer") == ["gemini", "gemini-argon"]
    assert orch.runner_accounts(orch.RUNNERS["sub-antigravity-flash"], "operator/x",
                                "lane_reviewer") == ["gemini"]
    assert orch.runner_accounts(orch.RUNNERS["sub-codex-luna"], "operator/x") == ["codex"]
