"""Tests for plan 0056 — the one role table (`fleetlib.agent_roles()`) that
fleet-watch and the orchestrator both resolve author/reviewer/merger from.

Author, reviewer and merger are the design (coder != reviewer != merger, two
approvals, never self-merge); the forge account names filling them are not.
Before this plan, fleet-watch computed its own `AGENT_ACCOUNTS` /
`IMPLEMENTER_LOGINS` from a private `_name_set` helper, and the orchestrator
separately unioned `{"implbot"}` into its own login set by hand
(`implementer_accounts()`) — two places computing the same fact, one of them
one `| {"implbot"}` away from silently reintroducing r7 M2 (a rename making
every historical marked PR invisible, re-dispatching every merged plan).
`agent_roles()` centralises both, and refuses at construction to build a role
whose own account does not count as itself.
"""
import ast
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

_ENV = ("FLEET_AGENT_ACCOUNTS", "FLEET_IMPLEMENTER_LOGINS",
       "FLEET_IMPLEMENTER_UIDS", "FLEET_COMMIT_IDENTITY",
       "FLEET_AUTHOR_ACCOUNT", "FLEET_REVIEWER_ACCOUNT",
       "FLEET_MERGER_ACCOUNT")


def _clean(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def _fleetlib(monkeypatch):
    _clean(monkeypatch)
    loader = importlib.machinery.SourceFileLoader("fleetlib", str(BIN / "fleetlib.py"))
    spec = importlib.util.spec_from_loader("fleetlib", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _fleet_watch(monkeypatch, **env):
    _clean(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    loader = importlib.machinery.SourceFileLoader("fleet_watch", str(BIN / "fleet-watch"))
    spec = importlib.util.spec_from_loader("fleet_watch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _orchestrator(monkeypatch, **env):
    _clean(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    loader = importlib.machinery.SourceFileLoader("orchestrator", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- the table itself
def test_default_roles_are_this_fleets_own_accounts(monkeypatch):
    mod = _fleetlib(monkeypatch)
    roles = mod.agent_roles()
    assert roles["author"].account == "implbot"
    assert roles["reviewer"].account == "revbot"
    assert roles["merger"].account == "operator"


def test_each_role_account_is_configurable_independently(monkeypatch):
    mod = _fleetlib(monkeypatch)
    monkeypatch.setenv("FLEET_AUTHOR_ACCOUNT", "coder-2")
    monkeypatch.setenv("FLEET_REVIEWER_ACCOUNT", "critic-2")
    monkeypatch.setenv("FLEET_MERGER_ACCOUNT", "human-2")
    roles = mod.agent_roles()
    assert roles["author"].account == "coder-2"
    assert roles["reviewer"].account == "critic-2"
    assert roles["merger"].account == "human-2"
    # renaming one position does not touch the others' defaults
    assert "coder-2" in roles["author"].logins
    assert roles["reviewer"].logins == {"critic-2"}


# ------------------------------------------------------ D3: refused at construction
def test_a_role_omitting_its_own_login_is_refused_at_construction(monkeypatch):
    mod = _fleetlib(monkeypatch)
    with pytest.raises(mod.RoleError) as ei:
        mod._role("author", "implbot", {"someone-else"})
    assert "author" in str(ei.value)


def test_a_role_with_no_logins_at_all_is_also_refused(monkeypatch):
    mod = _fleetlib(monkeypatch)
    with pytest.raises(mod.RoleError):
        mod._role("reviewer", "revbot", set())


def test_the_constructed_table_never_omits_its_own_account(monkeypatch):
    """agent_roles() itself must never hit the refusal above — every role it
    builds includes its own account in its login set by construction, not by
    the caller remembering to union it in."""
    mod = _fleetlib(monkeypatch)
    monkeypatch.setenv("FLEET_IMPLEMENTER_LOGINS", "someone-else")
    roles = mod.agent_roles()
    assert "implbot" in roles["author"].logins
    assert "someone-else" in roles["author"].logins


# ---------------------------------------------- D4: defaults reproduce today's values
def test_defaults_reproduce_fleet_watchs_agent_accounts(monkeypatch):
    mod = _fleet_watch(monkeypatch)
    assert mod.AGENT_ACCOUNTS == {"implbot", "revbot"}


def test_defaults_reproduce_fleet_watchs_implementer_logins(monkeypatch):
    mod = _fleet_watch(monkeypatch)
    assert mod.IMPLEMENTER_LOGINS == {"implbot"}


def test_agent_accounts_still_extends_with_flag_agent_accounts(monkeypatch):
    mod = _fleet_watch(monkeypatch, FLEET_AGENT_ACCOUNTS="intakebot,queuebot")
    assert mod.AGENT_ACCOUNTS == {"implbot", "revbot", "intakebot", "queuebot"}


def test_agent_accounts_never_include_the_merger(monkeypatch):
    """merger (`operator`) is this fleet's one human position — the account the
    merge whitelist exists for — and must never be folded into the set that
    main_is_protected treats as an automated account."""
    mod = _fleet_watch(monkeypatch, FLEET_AGENT_ACCOUNTS="intakebot")
    assert "operator" not in mod.AGENT_ACCOUNTS


def test_implementer_logins_still_extends_with_flag_implementer_logins(monkeypatch):
    mod = _fleet_watch(monkeypatch, FLEET_IMPLEMENTER_LOGINS="implbot-2")
    assert mod.IMPLEMENTER_LOGINS == {"implbot", "implbot-2"}


def test_defaults_reproduce_orchestrators_implementer_accounts(monkeypatch):
    mod = _orchestrator(monkeypatch, FLEET_IMPLEMENTER_UIDS="4242")
    uids, logins = mod.implementer_accounts()
    assert uids == {"4242"}
    assert logins == {"implbot"}


def test_defaults_reproduce_orchestrators_commit_identity_default(monkeypatch):
    mod = _orchestrator(monkeypatch)
    assert mod.lib.agent_roles()["author"].commit_identity == (
        "implbot <implbot@example.org>")


# --------------------------------------------------- D4: no literal account compare
def test_no_executable_in_bin_compares_against_a_literal_account_name():
    """AST, not grep: a docstring or comment mentioning an account name in a
    sentence is fine (it is a long string, never equal to just the name); the
    defect this guards is a bare `"implbot"` / `"revbot"` literal used
    as a comparison operand, a set/dict member, or built into a string —
    which after this plan lives only in fleetlib's documented role table and
    its (untouched, per this plan's boundary) git ssh default."""
    offenders = []
    for name in ("fleet-watch", "orchestrator"):
        path = BIN / name
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.strip().lower() in ("implbot", "revbot"):
                    offenders.append(f"{name}:{node.lineno}: {node.value!r}")
    assert offenders == []


def test_fleetlibs_only_literal_account_names_are_the_documented_defaults():
    """fleetlib.py is where the role table's documented defaults legitimately
    live (D1) plus the untouched `git_ssh_base` default (boundary: do not
    change bin/fleetlib.py:686). Anywhere else in the file, a bare literal
    account name would be exactly the comparison this plan removes."""
    path = BIN / "fleetlib.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    allowed_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "git_ssh_base",):
            allowed_lines.update(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_ROLE_ACCOUNT_DEFAULTS"
                for t in node.targets):
            allowed_lines.update(range(node.lineno, node.end_lineno + 1))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip().lower() in ("implbot", "revbot"):
                if node.lineno not in allowed_lines:
                    offenders.append(f"{node.lineno}: {node.value!r}")
    assert offenders == []
