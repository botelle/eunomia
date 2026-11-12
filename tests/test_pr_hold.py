"""Tests for `pr_hold` in bin/fleet-deps (plan 0170), against a stubbed forge."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


deps = _load("fleet_deps_for_pr_hold", BIN / "fleet-deps")
REPO, SHA = "operator/x", "abc12345"
WAITS = "plan 0002-b waits on 0001-a: not done on main"


def st(id_, status, desc="", login="mergebot", context="eunomia/deps"):
    return {"id": id_, "context": context, "status": status, "description": desc,
            "created_at": f"2026-10-27T10:00:{id_:02d}Z", "creator": {"login": login}}


class StubForge:
    def __init__(self, statuses=(), ok=True):
        self.statuses = list(statuses)       # newest first
        self.ok = ok
        self.calls = []

    def list_statuses(self, repo, sha, page=1, limit=50):
        self.calls.append(("list_statuses", repo, sha, page))
        if not self.ok:
            return 500, None
        return 200, (self.statuses if page == 1 else [])

    def __getattr__(self, name):
        raise AssertionError(f"pr_hold called {name}, which is not a read")


def hold(*statuses, **kw):
    return deps.pr_hold(StubForge(statuses, **kw), REPO, SHA)


def test_failure_from_the_merger_with_a_waits_on_description_is_held():
    assert hold(st(1, "failure", WAITS)) == ("held", WAITS)


def test_the_same_status_from_another_account_is_not_a_hold():
    assert hold(st(1, "failure", WAITS, login="someone")) is None


def test_older_failure_then_newer_success_is_not_a_hold():
    assert hold(st(2, "success", "plan 0002-b is ready"), st(1, "failure", WAITS)) is None


def test_latest_pending_is_not_a_hold():
    assert hold(st(2, "pending", "cannot read plans on main"), st(1, "failure", WAITS)) is None


@pytest.mark.parametrize("desc", [
    "plan id 'x y' is not a plan id",
    "plan 0002-b is not on main",
    "meeting: lane has no findings",
    "plan 0002-b is draft on main, not ready or done",
])
def test_other_failures_are_not_holds(desc):
    assert hold(st(1, "failure", desc)) is None


def test_no_deps_status_is_not_a_hold():
    assert hold(st(1, "failure", WAITS, context="ci/test")) is None
    assert hold() is None


def test_unreadable_list_is_unreadable_never_none():
    got = hold(ok=False)
    assert got is not None and got[0] == "unreadable"


def test_pr_hold_writes_nothing():
    forge = StubForge([st(1, "failure", WAITS)])
    deps.pr_hold(forge, REPO, SHA)
    assert all(c[0] == "list_statuses" for c in forge.calls)


def test_decide_plan_description_matches_the_recogniser(monkeypatch):
    desc = deps._waits_on_desc("0002-b", "0001-a", "why")
    assert deps._WAITS_ON.match(desc)
