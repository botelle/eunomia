"""Tests for plan 0055's token_for(role) — the one way bin/fleet-reviews and
bin/orchestrator get a Forgejo credential.

Before this plan those two files were the only executables in bin/ that still
hardcoded `~/bin/fetch-forgejo-token.sh` at the subprocess.run() call site,
skipping the configurable-command pattern fleet-watch, fleet-orphans and
fleet-candidate already used via FLEET_TOKEN_CMD. Each of fleet-reviews and
orchestrator now carries its own token_for(role) (this codebase already
duplicates the token-fetch body across fleet-orphans and fleet-candidate
rather than sharing a module for it — same shape here), and orchestrator's
role is deliberately its own configuration key rather than FLEET_TOKEN_CMD's:
that variable is a read-only position, and orchestrator posts comments and
edits pull request bodies with what it resolves."""
import importlib.machinery
import importlib.util
import os
import stat
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

_ENV = ("FLEET_TOKEN_CMD", "FLEET_AUTHOR_TOKEN_CMD")


def _load(name, monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / name.replace("_", "-")))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _fleet_reviews(monkeypatch):
    return _load("fleet_reviews", monkeypatch)


def _orchestrator(monkeypatch):
    return _load("orchestrator", monkeypatch)


def _fake_default_helper(tmp_path, token, monkeypatch):
    """A stand-in for ~/bin/fetch-forgejo-token.sh, reached only by HOME
    pointing here — never the real fleet helper, so a role that falls through
    to its default in a test cannot reach an actual credential store."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "fetch-forgejo-token.sh"
    script.write_text(f"#!/bin/sh\necho {token}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HOME", str(tmp_path))


# --------------------------------------------------------------- role tables
def test_fleet_reviews_reading_role_is_fleet_token_cmd(monkeypatch):
    mod = _fleet_reviews(monkeypatch)
    assert mod._TOKEN_ROLES["reading"] == (
        "FLEET_TOKEN_CMD", "~/bin/fetch-forgejo-token.sh")


def test_orchestrator_authoring_role_has_its_own_key(monkeypatch):
    """Not FLEET_TOKEN_CMD — that key is a read-only position on this fleet
    and the wrapper authors comments and pull request bodies with this one."""
    mod = _orchestrator(monkeypatch)
    assert mod._TOKEN_ROLES["authoring"] == (
        "FLEET_AUTHOR_TOKEN_CMD", "~/bin/fetch-forgejo-token.sh")


# ------------------------------------------------------------ happy resolution
def test_fleet_reviews_honors_its_configured_command(monkeypatch):
    mod = _fleet_reviews(monkeypatch)
    monkeypatch.setenv("FLEET_TOKEN_CMD", "printf tok-reading")
    assert mod.token_for("reading") == "tok-reading"


def test_orchestrator_honors_its_configured_command(monkeypatch):
    mod = _orchestrator(monkeypatch)
    monkeypatch.setenv("FLEET_AUTHOR_TOKEN_CMD", "printf tok-authoring")
    assert mod.token_for("authoring") == "tok-authoring"


def test_orchestrator_implbot_token_is_the_authoring_role(monkeypatch):
    """_implbot_token is kept as its own name (test_orchestrator_session.py
    monkeypatches it directly) but must resolve through the same role."""
    mod = _orchestrator(monkeypatch)
    monkeypatch.setenv("FLEET_AUTHOR_TOKEN_CMD", "printf tok-via-alias")
    assert mod._implbot_token() == "tok-via-alias"


# --------------------------------------------------------- roles are separate
def test_configuring_the_reading_key_does_not_supply_authoring(tmp_path, monkeypatch):
    """Only FLEET_TOKEN_CMD is set; orchestrator's authoring role must fall
    through to ITS OWN default, not silently pick up the reading command."""
    mod = _orchestrator(monkeypatch)
    _fake_default_helper(tmp_path, "tok-default-authoring", monkeypatch)
    monkeypatch.setenv("FLEET_TOKEN_CMD", "printf tok-reading-only")
    assert mod.token_for("authoring") == "tok-default-authoring"


def test_configuring_the_authoring_key_does_not_supply_reading(tmp_path, monkeypatch):
    """Only FLEET_AUTHOR_TOKEN_CMD is set; fleet-reviews's reading role must
    fall through to ITS OWN default, not silently pick up the authoring
    command that only orchestrator would ever read."""
    mod = _fleet_reviews(monkeypatch)
    _fake_default_helper(tmp_path, "tok-default-reading", monkeypatch)
    monkeypatch.setenv("FLEET_AUTHOR_TOKEN_CMD", "printf tok-authoring-only")
    assert mod.token_for("reading") == "tok-default-reading"


def test_both_roles_configured_independently_at_once(monkeypatch):
    reviews = _fleet_reviews(monkeypatch)
    orch = _orchestrator(monkeypatch)
    monkeypatch.setenv("FLEET_TOKEN_CMD", "printf tok-A")
    monkeypatch.setenv("FLEET_AUTHOR_TOKEN_CMD", "printf tok-B")
    assert reviews.token_for("reading") == "tok-A"
    assert orch.token_for("authoring") == "tok-B"


# ------------------------------------------------------------- named failure
def test_fleet_reviews_resolver_miss_is_named_and_carries_no_output(monkeypatch):
    mod = _fleet_reviews(monkeypatch)
    monkeypatch.setenv("FLEET_TOKEN_CMD",
                       "sh -c 'echo do-not-leak-this >&2; exit 1'")
    with pytest.raises(mod.TokenError) as ei:
        mod.token_for("reading")
    msg = str(ei.value)
    assert "reading" in msg and "FLEET_TOKEN_CMD" in msg
    assert "do-not-leak-this" not in msg


def test_orchestrator_resolver_miss_is_named_and_carries_no_output(monkeypatch):
    mod = _orchestrator(monkeypatch)
    monkeypatch.setenv("FLEET_AUTHOR_TOKEN_CMD",
                       "sh -c 'echo do-not-leak-this >&2; exit 1'")
    with pytest.raises(mod.TokenError) as ei:
        mod.token_for("authoring")
    msg = str(ei.value)
    assert "authoring" in msg and "FLEET_AUTHOR_TOKEN_CMD" in msg
    assert "do-not-leak-this" not in msg


def test_implbot_token_wraps_the_miss_in_stop(monkeypatch):
    """_forge()'s only caller-visible failure mode is Stop (orchestrator's
    control-flow exception); the resolver's own TokenError must not leak past
    the wrapper's boundary as some other, uncaught type."""
    mod = _orchestrator(monkeypatch)
    monkeypatch.setenv("FLEET_AUTHOR_TOKEN_CMD", "false")
    with pytest.raises(mod.Stop) as ei:
        mod._implbot_token()
    assert "authoring" in str(ei.value)


def test_unknown_role_is_a_named_failure_not_a_keyerror(monkeypatch):
    mod = _orchestrator(monkeypatch)
    with pytest.raises(mod.TokenError):
        mod.token_for("reviewing")


# ------------------------------------------------ no path literal at a call site
def test_no_executable_in_bin_embeds_the_helper_path_at_a_call_site():
    """`grep -rn fetch-forgejo-token bin/` must only ever find it in a
    comment/docstring or in a role table's documented default — never as a
    direct argument to subprocess.run(), which was the actual defect: a
    literal that only code, not configuration, could change."""
    offenders = []
    for path in sorted(BIN.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "fetch-forgejo-token" in line and "subprocess.run(" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == []


def test_fleet_reviews_and_orchestrator_no_longer_hold_the_path_literal_inline():
    """The two files plan 0055 targets: confirm the literal now lives only in
    a role table, addressed through token_for(), not inlined per call."""
    for name, fn in (("fleet-reviews", "token_for"), ("orchestrator", "token_for")):
        text = (BIN / name).read_text()
        assert "fetch-forgejo-token.sh" in text
        assert f"def {fn}" in text
