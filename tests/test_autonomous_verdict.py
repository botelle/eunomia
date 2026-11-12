"""Plan 0106 — the protection verdict knows the agent merger and the second
reviewer (ADR-0019, ADR-0004 as amended), and dispatch requires a human merge.

Drives the REAL `main_is_protected()` with rule fixtures, and the real
`plan_merged_by_human()` with a forge double."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

_ENV = ("FLEET_AGENT_ACCOUNTS", "FLEET_AUTHOR_ACCOUNT", "FLEET_REVIEWER_ACCOUNT",
        "FLEET_MERGER_ACCOUNT", "FLEET_SECOND_REVIEWER_ACCOUNT",
        "FLEET_AGENT_MERGER_ACCOUNT", "FLEET_IMPLEMENTER_LOGINS")


@pytest.fixture
def mod(monkeypatch, tmp_path):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    loader = importlib.machinery.SourceFileLoader("fleet_watch", str(BIN / "fleet-watch"))
    spec = importlib.util.spec_from_loader("fleet_watch", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class _Forge:
    def __init__(self, rules):
        self.rules = rules

    def get_branch_protections(self, repo):
        return 200, self.rules


def _rule(**over):
    d = {
        "branch_name": "main",
        "required_approvals": 2,
        "enable_approvals_whitelist": True,
        "approvals_whitelist_username": ["revbot", "rev2bot", "operator"],
        "enable_merge_whitelist": True,
        "merge_whitelist_usernames": ["operator"],
        "dismiss_stale_approvals": True,
        "ignore_stale_approvals": False,
        "apply_to_admins": True,
    }
    d.update(over)
    return d


def _deps(**over):
    return _rule(enable_status_check=True, status_check_contexts=["eunomia/deps"],
                 **over)


def _verdict(mod, monkeypatch, rule):
    monkeypatch.setattr(mod, "_forge", lambda token: _Forge([rule]))
    return mod.main_is_protected("operator/x", "tok")


# ------------------------------------------------------------- the merge lane
def test_the_agent_merger_may_merge_when_deps_is_required(mod, monkeypatch):
    ok, why = _verdict(mod, monkeypatch, _deps(
        merge_whitelist_usernames=["mergebot"],
        approvals_whitelist_username=["revbot", "rev2bot"]))
    assert ok, why


def test_the_agent_merger_without_the_requirement_is_refused_by_name(mod, monkeypatch):
    ok, why = _verdict(mod, monkeypatch, _rule(
        merge_whitelist_usernames=["operator", "mergebot"]))
    assert not ok and "mergebot" in why


@pytest.mark.parametrize("who", ["implbot", "revbot", "rev2bot"])
@pytest.mark.parametrize("required", [True, False])
def test_every_other_agent_on_the_merge_whitelist_is_refused(mod, monkeypatch,
                                                             who, required):
    build = _deps if required else _rule
    ok, why = _verdict(mod, monkeypatch, build(
        merge_whitelist_usernames=["mergebot", who]))
    assert not ok and who in why


# ----------------------------------------------------------- the approvals lane
def test_two_reviewers_at_two_with_deps_is_ok(mod, monkeypatch):
    ok, why = _verdict(mod, monkeypatch, _deps(
        merge_whitelist_usernames=["mergebot"],
        approvals_whitelist_username=["revbot", "rev2bot"]))
    assert ok, why


def test_two_reviewers_at_one_with_deps_is_refused(mod, monkeypatch):
    ok, _ = _verdict(mod, monkeypatch, _deps(
        required_approvals=1, merge_whitelist_usernames=["mergebot"],
        approvals_whitelist_username=["revbot", "rev2bot"]))
    assert not ok


def test_two_reviewers_with_an_agent_merger_and_no_deps_is_refused(mod, monkeypatch):
    ok, _ = _verdict(mod, monkeypatch, _rule(
        merge_whitelist_usernames=["mergebot"],
        approvals_whitelist_username=["revbot", "rev2bot"]))
    assert not ok


def test_another_agent_on_the_approvals_whitelist_is_refused(mod, monkeypatch):
    ok, why = _verdict(mod, monkeypatch, _deps(
        merge_whitelist_usernames=["mergebot"],
        approvals_whitelist_username=["revbot", "rev2bot", "mergebot"]))
    assert not ok and "mergebot" in why


def test_an_unsatisfiable_rule_counts_the_reviewers(mod, monkeypatch):
    ok, why = _verdict(mod, monkeypatch, _deps(
        required_approvals=3, merge_whitelist_usernames=["mergebot"],
        approvals_whitelist_username=["revbot", "rev2bot"]))
    assert not ok and "unsatisfiable" in why


# ---------------------------------------------------------------------- D4b
def test_the_fleets_current_shape_is_ok_without_deps(mod, monkeypatch):
    ok, why = _verdict(mod, monkeypatch, _rule())
    assert ok, why


def test_an_agent_merger_beside_the_human_without_deps_is_refused(mod, monkeypatch):
    ok, _ = _verdict(mod, monkeypatch, _rule(
        merge_whitelist_usernames=["operator", "mergebot"]))
    assert not ok


def test_a_non_reviewer_agent_beside_the_reviewers_is_refused(mod, monkeypatch):
    ok, why = _verdict(mod, monkeypatch, _rule(
        approvals_whitelist_username=["revbot", "rev2bot", "implbot", "operator"]))
    assert not ok and "implbot" in why


def test_a_single_reviewer_at_one_stays_refused_with_a_human_merge_lane(mod, monkeypatch):
    ok, _ = _verdict(mod, monkeypatch, _rule(
        required_approvals=1, approvals_whitelist_username=["revbot"]))
    assert not ok


# ------------------------------------------------------------- the status match
@pytest.mark.parametrize("ctxs", [["eunomia/deps-x"], ["EUNOMIA/DEPS"],
                                  ["eunomia/dep"], "eunomia/deps", None])
def test_only_the_exact_context_counts_as_required(mod, ctxs):
    assert not mod._requires_deps({"enable_status_check": True,
                                   "status_check_contexts": ctxs})


def test_the_context_is_not_required_when_the_check_is_off(mod):
    assert not mod._requires_deps({"enable_status_check": False,
                                   "status_check_contexts": ["eunomia/deps"]})
    assert mod._requires_deps({"enable_status_check": True,
                               "status_check_contexts": ["x", "eunomia/deps"]})


def test_a_near_miss_context_does_not_buy_the_agent_merger(mod, monkeypatch):
    ok, _ = _verdict(mod, monkeypatch, _rule(
        enable_status_check=True, status_check_contexts=["eunomia/deps-x"],
        merge_whitelist_usernames=["mergebot"],
        approvals_whitelist_username=["revbot", "rev2bot"]))
    assert not ok


# --------------------------------------------------------- D6 human-merge gate
class _Plans:
    def __init__(self, commits, pull, pull_status=200):
        self.commits, self.pull, self.pull_status = commits, pull, pull_status
        self.asked = None

    def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
        self.asked = (sha, path)
        return 200, self.commits

    def get_commit_pull(self, repo, sha):
        return self.pull_status, self.pull


class _MergeCommitForge(_Plans):
    """Main as a merge-commit merge leaves it: the path log names the branch
    commit B, whose own /pull is empty; only the merge commit M has a PR."""
    def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
        if path:
            return 200, [{"sha": "b" * 40}]
        if page > 1:
            return 200, []
        return 200, [
            {"sha": "m" * 40, "parents": [{"sha": "0" * 40}, {"sha": "b" * 40}]},
            {"sha": "b" * 40, "parents": [{"sha": "0" * 40}]},
            {"sha": "0" * 40, "parents": []},
        ]

    def get_commit_pull(self, repo, sha):
        return (200, self.pull) if sha == "m" * 40 else (404, None)


def test_a_merge_commit_merge_resolves_through_the_merge_commit(mod, monkeypatch):
    ok, why = _gate(mod, monkeypatch, _MergeCommitForge([], _merged("operator")))
    assert ok, why
    ok, _ = _gate(mod, monkeypatch, _MergeCommitForge([], _merged("mergebot")))
    assert not ok


class _RebasedForge(_Plans):
    """mergebot rebased the plan edit E onto main; operator's later, unrelated
    merge M has a branch F forked after E, so M's second parent reaches E."""
    def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
        if path:
            return 200, [{"sha": "e" * 40}]
        if page > 1:
            return 200, []
        return 200, [
            {"sha": "m" * 40, "parents": [{"sha": "e" * 40}, {"sha": "f" * 40}]},
            {"sha": "f" * 40, "parents": [{"sha": "e" * 40}]},
            {"sha": "e" * 40, "parents": [{"sha": "0" * 40}]},
            {"sha": "0" * 40, "parents": []},
        ]

    def get_commit_pull(self, repo, sha):
        return (200, _merged("operator")) if sha == "m" * 40 else (404, None)


def test_a_rebased_edit_is_not_credited_to_a_later_human_merge(mod, monkeypatch):
    ok, _ = _gate(mod, monkeypatch, _RebasedForge([], None))
    assert not ok


def _merged(login, n=7):
    return {"number": n, "merged_by": {"login": login}}


def _gate(mod, monkeypatch, forge):
    monkeypatch.setattr(mod, "_forge", lambda token: forge)
    return mod.plan_merged_by_human("operator/x", "plans/0001-a.md", "tok")


def test_a_plan_merged_by_the_human_dispatches(mod, monkeypatch):
    f = _Plans([{"sha": "b" * 40}], _merged("operator"))
    ok, why = _gate(mod, monkeypatch, f)
    assert ok, why
    assert f.asked == ("main", "plans/0001-a.md")


def test_a_plan_merged_by_the_agent_merger_is_refused(mod, monkeypatch):
    ok, why = _gate(mod, monkeypatch, _Plans([{"sha": "b" * 40}], _merged("mergebot")))
    assert not ok and "mergebot" in why


def test_a_later_agent_merged_edit_is_refused(mod, monkeypatch):
    # the newest commit touching the file is the one asked about, not the first
    f = _Plans([{"sha": "c" * 40}, {"sha": "b" * 40}], _merged("mergebot", 9))
    ok, _ = _gate(mod, monkeypatch, f)
    assert not ok


def test_a_plan_whose_pull_cannot_be_found_is_refused(mod, monkeypatch):
    ok, _ = _gate(mod, monkeypatch, _Plans([{"sha": "b" * 40}], None, 404))
    assert not ok


def test_a_plan_with_no_commit_is_refused(mod, monkeypatch):
    ok, _ = _gate(mod, monkeypatch, _Plans([], None))
    assert not ok


def test_a_pull_with_no_merger_recorded_is_refused(mod, monkeypatch):
    ok, _ = _gate(mod, monkeypatch, _Plans([{"sha": "b" * 40}], {"number": 3}))
    assert not ok


def test_the_gate_pages_and_blocks_dispatch(mod, monkeypatch):
    pages = []
    monkeypatch.setattr(mod, "_notify_once", lambda k, t, b: pages.append(t))
    monkeypatch.setattr(mod, "warn", lambda m: None)
    monkeypatch.setattr(mod, "_forge", lambda token: _Plans(
        [{"sha": "b" * 40}], _merged("mergebot")))
    assert not mod._human_merge_gate("operator/x", "plans/0001-a.md", "0001-a", "tok")
    assert pages == ["Plan not human-merged"]
    monkeypatch.setattr(mod, "_clear_notified", lambda k: None)
    monkeypatch.setattr(mod, "_forge", lambda token: _Plans(
        [{"sha": "b" * 40}], _merged("operator")))
    assert mod._human_merge_gate("operator/x", "plans/0001-a.md", "0001-a", "tok")
