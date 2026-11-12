"""Tests for plan 0215 — the orchestrator reads its own pull request by
branch, not by listing every PR on the repository.

What is under test is the property the plan exists to establish, not merely
that each caller still finds its PR: `_NoListingForge.list_pulls` raises the
moment a FULL listing (`state="all"`, what `read_pulls` used) is called, so
every test here is itself the assertion that no such listing happened, not a
`calls` log read after the fact. A bounded `state="open"` call is the one
exception — the review of PR #1437 added `_prefer_open_on_branch`'s
conditional second read (plan 0215 D2), and `list_pulls` answers that one
from `self.open_pulls` instead of raising, so the tests below can tell "no
full listing" apart from "no listing of any kind", which is not the
property this plan establishes.
"""
import base64
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import test_orchestrator_lane_status as L

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "operator/demo"
PLAN_ID = "0001-a-feature"
PLAN_PATH = "plans/0001-a-feature.md"
BRANCH = "feat/0001-a-feature"

PLAN_DONE = """\
---
id: 0001-a-feature
status: done
zone: public
tier: 1
paths: ["bin/thing"]
---

# A feature
"""


def _load(tmp_path, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    os.environ["EUNOMIA_SESSION"] = "pr-by-branch-test"
    os.environ.pop("FLEET_IMPLEMENTER_UIDS", None)
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("orch_pbb", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orch_pbb", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    m._sleep = lambda s: None
    return m


def _ff():
    loader = importlib.machinery.SourceFileLoader("ff_pbb", str(BIN / "fleetforge.py"))
    spec = importlib.util.spec_from_loader("ff_pbb", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _pr(n=5, head=BRANCH, base="main", body="Plan: 0001-a-feature",
       login="implbot", sha="abc1234", state="open"):
    return {"number": n, "body": body, "user": {"id": 9, "login": login},
            "base": {"ref": base}, "head": {"ref": head, "sha": sha},
            "state": state}


class _NoListingForge:
    """Answers `get_pull_by_branch` (and the few other verbs each caller
    needs) and fails the test outright the moment a FULL listing
    (`state="all"`) is called — the property plan 0215 exists to establish.
    `state="open"` is answered from `open_pulls`, bounded, never raising:
    the one listing `_prefer_open_on_branch`'s fallback (plan 0215 D2, taken
    up by the review of PR #1437) is allowed to make.

    `repo_reachable=False` makes `get_repo` fail, so a 404 from
    `get_pull_by_branch` cannot be confirmed as "no PR" (the other half of
    that same review)."""

    def __init__(self, pulls=(), contents=None, by_branch_sequence=None,
                open_pulls=None, repo_reachable=True):
        self.pulls = list(pulls)
        self.contents = contents or {}
        self.by_branch_sequence = (list(by_branch_sequence)
                                   if by_branch_sequence is not None else None)
        self.open_pulls = (list(open_pulls) if open_pulls is not None else
                          [p for p in self.pulls
                           if (p.get("state") or "").lower() == "open"])
        self.repo_reachable = repo_reachable
        self.by_branch_calls = []
        self.list_pulls_calls = []
        self.edits = []
        self.comments = []

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        if state != "open":
            raise AssertionError(f"a full listing (state={state!r}) must not "
                                 "be called (plan 0215)")
        self.list_pulls_calls.append((repo, state, page, limit))
        start = (page - 1) * limit
        page_items = self.open_pulls[start:start + limit]
        return 200, page_items

    def get_pull_by_branch(self, repo, base, head):
        self.by_branch_calls.append((repo, base, head))
        if self.by_branch_sequence is not None:
            return self.by_branch_sequence.pop(0)
        for pr in self.pulls:
            if (((pr.get("head") or {}).get("ref") or "") == head
                    and ((pr.get("base") or {}).get("ref") or "") == base):
                return 200, pr
        return 404, None

    def get_repo(self, repo):
        if self.repo_reachable:
            return 200, {"full_name": repo}
        return 404, None

    def create_issue_comment(self, repo, n, body):
        self.comments.append((n, body))
        return 201, {}

    def edit_pull_body(self, repo, n, body):
        self.edits.append((n, body))
        for pr in self.pulls:
            if pr.get("number") == n:
                pr["body"] = body
        return 200, {}

    def get_pull(self, repo, n):
        for pr in self.pulls:
            if pr.get("number") == n:
                return 200, pr
        return 404, None

    def get_contents(self, repo, path, ref=None):
        body = self.contents.get(ref)
        if body is None:
            return 404, None
        return 200, {"content": base64.b64encode(body.encode()).decode()}


class _PagingOnly:
    """NOT used by any caller under test — only here to demonstrate, for the
    same fake repository, what `read_pulls` (removed by this plan) would
    have hit: a 1,201-pull repository needs 25 pages at 50/page, past
    `READ_ATTEMPTS`' page cap of 20."""

    def __init__(self, pulls):
        self.pulls = pulls

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        start = (page - 1) * limit
        return 200, self.pulls[start:start + limit]


# ----------------------------------------------------- the four D2 callers
def test_post_question_finds_its_pr_by_branch_with_no_listing(tmp_path):
    mod = _load(tmp_path)
    forge = _NoListingForge(pulls=[_pr()])
    q = {"section": "3. Boundaries", "question": "which?", "why": "two fit",
         "blocking": True, "options": []}
    where = mod.post_question(REPO, PLAN_ID, BRANCH, "q-1", q, forge=forge, mod=_ff())
    assert where == {"pr": 5}
    assert forge.comments == [(5, mod.question_comment(PLAN_ID, "q-1", q))]
    assert forge.by_branch_calls == [(REPO, "main", BRANCH)]


def test_open_implementation_pr_finds_its_pr_by_branch_with_no_listing(tmp_path):
    mod = _load(tmp_path)
    forge = _NoListingForge(pulls=[_pr()])
    pr = mod._open_implementation_pr(REPO, PLAN_ID, BRANCH, forge=forge, mod=_ff())
    assert pr["number"] == 5
    assert forge.by_branch_calls == [(REPO, "main", BRANCH)]


def test_verify_marked_pr_finds_its_pr_by_branch_with_no_listing(tmp_path):
    mod = _load(tmp_path)
    forge = _NoListingForge(pulls=[_pr()], contents={"abc1234": PLAN_DONE})
    ok, why, num = mod.verify_marked_pr(REPO, BRANCH, PLAN_ID, PLAN_PATH,
                                        forge=forge, mod=_ff())
    assert (ok, num) == (True, 5), why
    assert forge.by_branch_calls == [(REPO, "main", BRANCH)]


def test_verify_followup_pr_finds_its_pr_by_branch_with_no_listing(tmp_path):
    mod = _load(tmp_path)
    forge = _NoListingForge(pulls=[_pr(n=9, body="Follows: #7")])
    ok, why, num = mod.verify_followup_pr(REPO, BRANCH, 7, PLAN_ID,
                                          forge=forge, mod=_ff())
    assert (ok, num) == (True, 9), why
    assert forge.by_branch_calls == [(REPO, "main", BRANCH)]


# ------------------------- D2 fallback: a shared head prefers the open PR
def test_verify_marked_pr_prefers_an_open_pr_over_a_closed_one_on_the_same_head(tmp_path):
    """Review of PR #1437: ADR-0029 §2 names a closed and an open pull
    request sharing one head as reachable through `redispatch`, even though
    a scan of eunomia's history (recorded in `get_pull_by_branch`'s
    docstring) found no case of it. `get_pull_by_branch` is simulated here
    as if it had answered with the stale, closed PR for the shared head —
    `_prefer_open_on_branch`'s bounded `state="open"` read must still find
    and prefer the open one, not the one `verify_marked_pr` would otherwise
    verify and let the steward release the lease on dead work."""
    mod = _load(tmp_path)
    closed = _pr(n=5, state="closed", body="")
    opened = _pr(n=11, state="open")
    forge = _NoListingForge(pulls=[closed], open_pulls=[opened],
                            contents={"abc1234": PLAN_DONE})
    ok, why, num = mod.verify_marked_pr(REPO, BRANCH, PLAN_ID, PLAN_PATH,
                                        forge=forge, mod=_ff())
    assert (ok, num) == (True, 11), why
    assert forge.list_pulls_calls == [(REPO, "open", 1, 50)]


def test_verify_marked_pr_keeps_the_closed_pr_when_no_open_one_matches(tmp_path):
    """The fallback narrows toward a better answer and never invents one:
    with no open pull request on this head, the closed match from
    `get_pull_by_branch` stands."""
    mod = _load(tmp_path)
    closed = _pr(n=5, state="closed")
    forge = _NoListingForge(pulls=[closed], open_pulls=[],
                            contents={"abc1234": PLAN_DONE})
    ok, why, num = mod.verify_marked_pr(REPO, BRANCH, PLAN_ID, PLAN_PATH,
                                        forge=forge, mod=_ff())
    assert (ok, num) == (True, 5), why
    assert forge.list_pulls_calls == [(REPO, "open", 1, 50)]


# --------------------------------------------------- 404 is "no PR", never unreadable
def test_a_404_reads_as_no_pr_not_unreadable(tmp_path):
    mod = _load(tmp_path)
    forge = _NoListingForge(pulls=[])
    ok, why, num = mod.verify_marked_pr(REPO, BRANCH, PLAN_ID, PLAN_PATH,
                                        forge=forge, mod=_ff())
    assert (ok, num) == (False, None)
    assert why == f"no pull request with head {BRANCH}"


def test_a_404_with_an_unreachable_repo_is_unreadable_not_no_pr(tmp_path):
    """Review of PR #1437: a 404 from `get_pull_by_branch` also covers a
    repository the token can no longer see — renamed, deleted, or a token
    that lost scope. That must not read as 'no PR'; it is confirmed against
    the repository itself first (`_repo_reachable`), and an unreachable
    repository makes the read retry and then report unreadable, exactly
    like any other failed read."""
    mod = _load(tmp_path)
    forge = _NoListingForge(pulls=[], repo_reachable=False)
    ok, why, num = mod.verify_marked_pr(REPO, BRANCH, PLAN_ID, PLAN_PATH,
                                        forge=forge, mod=_ff())
    assert ok is False and num is None
    assert "could not read" in why and "no pull request" not in why
    assert len(forge.by_branch_calls) == mod.READ_ATTEMPTS


def test_a_500_then_success_recovers_through_the_retry(tmp_path):
    mod = _load(tmp_path)
    forge = _NoListingForge(
        pulls=[], contents={"abc1234": PLAN_DONE},
        by_branch_sequence=[(500, None), (200, _pr())])
    ok, why, num = mod.verify_marked_pr(REPO, BRANCH, PLAN_ID, PLAN_PATH,
                                        forge=forge, mod=_ff())
    assert (ok, num) == (True, 5), why
    assert len(forge.by_branch_calls) == 2


def test_every_attempt_failing_is_unreadable_not_no_pr(tmp_path):
    mod = _load(tmp_path)
    forge = _NoListingForge(
        pulls=[], by_branch_sequence=[(500, None)] * mod.READ_ATTEMPTS)
    ok, why, num = mod.verify_marked_pr(REPO, BRANCH, PLAN_ID, PLAN_PATH,
                                        forge=forge, mod=_ff())
    assert ok is False and num is None
    assert "could not read" in why and "no pull request" not in why
    assert len(forge.by_branch_calls) == mod.READ_ATTEMPTS


# --------------------------------------- past the 1,000-pull-request page cap
def test_verify_marked_pr_succeeds_past_the_thousand_pr_cap(tmp_path):
    """eunomia had 821 pull requests on 2026-11-07 and adds about 12 a day —
    it crosses 1,000 around 2026-11-19. On the pre-0215 code, `read_pulls`
    paged `list_pulls` to a cap of 20 pages at 50/page, so a repository past
    1,000 pull requests hit `CAP_HIT` and `verify_marked_pr` failed with its
    own finished work sitting right there. The fake repository below has
    1,201 pulls — one more than the old cap could ever finish reading."""
    others = [_pr(n=i, head=f"feat/other-{i}", body="") for i in range(1, 1201)]
    mine = _pr()
    all_pulls = others + [mine]

    # Demonstrate what the OLD approach hits on this same fake repository —
    # a separate, paging-only double, never touched by verify_marked_pr below.
    ffmod = _ff()
    paged = ffmod.paged(_PagingOnly(all_pulls).list_pulls, REPO, state="all", cap=20)
    for _ in paged:
        pass
    assert paged.outcome == ffmod.CAP_HIT, "the old cap must still bite on this fixture"

    mod = _load(tmp_path)
    forge = _NoListingForge(pulls=all_pulls, contents={"abc1234": PLAN_DONE})
    ok, why, num = mod.verify_marked_pr(REPO, BRANCH, PLAN_ID, PLAN_PATH,
                                        forge=forge, mod=ffmod)
    assert (ok, num) == (True, 5), why
    assert forge.by_branch_calls == [(REPO, "main", BRANCH)]


# ------------------------------------------------------------- D3: lane PRs
LANE_PLAN = L.PLAN


def test_derive_lanes_finds_an_open_and_a_merged_lane_pr_by_branch(tmp_path):
    mod = _load(tmp_path)
    fd = mod.lib.fleet_dir()
    fd.mkdir(parents=True, exist_ok=True)
    L._dispatched(fd, runner="sub-opus", lease="lane--1")
    L._dispatched(fd, runner="sub-codex", lease="lane--2")
    open_pr = L._lane_pr(12, runner="sub-opus", plan=LANE_PLAN, head=L.LANE_HEAD)
    merged_pr = dict(L._lane_pr(13, runner="sub-codex", plan=LANE_PLAN, head="e" * 40),
                     merged=True, state="closed")
    forge = _NoListingForge(pulls=[open_pr, merged_pr])
    lanes = mod.derive_lanes(L.REPO, LANE_PLAN, forge, _ff())
    states = {l["runner"]: l["state"] for l in lanes}
    assert states == {"sub-opus": "opened #12", "sub-codex": "opened #13"}
    assert sorted(forge.by_branch_calls) == sorted([
        (L.REPO, "main", f"agent/tests/{LANE_PLAN}/sub-opus"),
        (L.REPO, "main", f"agent/tests/{LANE_PLAN}/sub-codex"),
    ])


def test_derive_lanes_is_unreadable_not_missing_on_a_failed_read(tmp_path):
    mod = _load(tmp_path)
    fd = mod.lib.fleet_dir()
    fd.mkdir(parents=True, exist_ok=True)
    L._dispatched(fd, runner="sub-opus", lease="lane--1")
    forge = _NoListingForge(pulls=[], by_branch_sequence=[(500, None)])
    assert mod.derive_lanes(L.REPO, LANE_PLAN, forge, _ff()) is None


def test_lane_pr_by_branch_prefers_an_open_pr_over_a_closed_one_on_the_same_head(tmp_path):
    """The same fallback `read_pull_by_branch` applies (review of PR #1437):
    a lane's branch (unlike a plan's) has no `-r<N>` suffix keeping a
    redispatched lane off an earlier one's head, so `_lane_pr_by_branch`
    carries the identical defence."""
    mod = _load(tmp_path)
    head = f"agent/tests/{LANE_PLAN}/sub-opus"
    closed = dict(L._lane_pr(12, runner="sub-opus", plan=LANE_PLAN), state="closed")
    opened = dict(L._lane_pr(20, runner="sub-opus", plan=LANE_PLAN), state="open")
    forge = _NoListingForge(pulls=[closed], open_pulls=[opened])
    pr = mod._lane_pr_by_branch(L.REPO, LANE_PLAN, "sub-opus", forge, _ff())
    assert pr["number"] == 20
    assert forge.list_pulls_calls == [(L.REPO, "open", 1, 50)]
    assert forge.by_branch_calls == [(L.REPO, "main", head)]


def test_lane_pr_by_branch_404_with_an_unreachable_repo_is_unreadable(tmp_path):
    """The same repo-reachability confirmation as `read_pull_by_branch`
    (review of PR #1437): an unconfirmed 404 is `mod.FAILED`, never `None`,
    the sentinel `derive_lanes` reads as 'unreadable, stop' rather than
    'this lane has no pull request yet'."""
    mod = _load(tmp_path)
    ffmod = _ff()
    forge = _NoListingForge(pulls=[], repo_reachable=False)
    result = mod._lane_pr_by_branch(L.REPO, LANE_PLAN, "sub-opus", forge, ffmod)
    assert result is ffmod.FAILED
