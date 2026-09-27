"""Tests for bin/orchestrator part 2d — the post-approval freeze (plan 0032).

The invariant under test is a CONDITION, not a counter: pushes are refused
whenever an approval stands at the branch head. The difference shows up only in
the re-freeze case, which is why `test_the_branch_re_freezes_on_the_next_approval`
is the most important test in this file — a counter implementation passes every
other test here and silently pushes over the second approval.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

OWNER, BOT, HEAD, HEAD2 = "1", "3", "a" * 40, "b" * 40


def _load(tmp_path, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    os.environ["EUNOMIA_SESSION"] = "freeze-test"
    os.environ["FLEET_OPERATOR_UID"] = OWNER
    os.environ["FLEET_IMPLEMENTER_UIDS"] = BOT
    os.environ["EUNOMIA_STEWARD_POLL"] = "5"
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("orch_freeze", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orch_freeze", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _ff():
    loader = importlib.machinery.SourceFileLoader("ff_fz", str(BIN / "fleetforge.py"))
    spec = importlib.util.spec_from_loader("ff_fz", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _c(cid, uid, body, login="operator"):
    return {"id": cid, "body": body, "user": {"id": uid, "login": login}}


def _approval(sha=HEAD, stale=False, dismissed=False, who="operator"):
    return {"id": 1, "state": "APPROVED", "commit_id": sha, "stale": stale,
            "dismissed": dismissed, "user": {"id": OWNER, "login": who}, "body": ""}


class Forge:
    def __init__(self, comments=(), reviews=(), heads=(HEAD,), pulls=()):
        self.comments, self.reviews = list(comments), list(reviews)
        self.heads, self.pulls, self.posted = list(heads), list(pulls), []

    def _head(self):
        return self.heads[0] if len(self.heads) == 1 else self.heads.pop(0)

    def get_pull(self, repo, n):
        return 200, {"merged": False, "state": "open", "head": {"sha": self._head()}}

    def list_reviews(self, repo, n, page=1, limit=50):
        return (200, self.reviews) if page == 1 else (200, [])

    def list_issue_comments(self, repo, n, since=None, page=1, limit=50):
        return (200, self.comments) if page == 1 else (200, [])

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        return (200, self.pulls) if page == 1 else (200, [])

    def create_issue_comment(self, repo, n, body):
        self.posted.append(body)
        return 201, {}

    def get_commit_status(self, repo, sha):
        """Green. The review loop waits for CI before dispatching, so any
        fixture that reaches it needs an answer here."""
        return 200, {"state": "success", "statuses": [
            {"context": "ci", "status": "success"}]}


# ------------------------------------------------------------ the condition
def test_a_live_approval_at_head_freezes(tmp_path):
    mod = _load(tmp_path)
    f = Forge(reviews=[_approval()])
    stands, why = mod.approval_stands("r", 7, HEAD, f, _ff())
    assert stands and "operator" in why


def test_an_approval_at_another_sha_does_not_freeze(tmp_path):
    mod = _load(tmp_path)
    f = Forge(reviews=[_approval(sha=HEAD)])
    assert mod.approval_stands("r", 7, HEAD2, f, _ff())[0] is False


def test_a_stale_or_dismissed_approval_does_not_freeze(tmp_path):
    mod = _load(tmp_path)
    for flag in ("stale", "dismissed"):
        f = Forge(reviews=[_approval(**{flag: True})])
        assert mod.approval_stands("r", 7, HEAD, f, _ff())[0] is False


def test_an_unreadable_review_list_freezes(tmp_path):
    """Fail closed. A read that fails says nothing about whether an approval
    exists, and guessing permissively costs a dismissed human approval."""
    mod = _load(tmp_path)

    class Broken(Forge):
        def list_reviews(self, repo, n, page=1, limit=50):
            return 500, None
    stands, why = mod.approval_stands("r", 7, HEAD, Broken(), _ff())
    assert stands and "unreadable" in why


# ------------------------------------------------------------- the override
def test_unfreeze_is_only_the_bare_token_and_is_never_inferred(tmp_path):
    mod = _load(tmp_path)
    assert mod.is_unfreeze("unfreeze")
    assert mod.is_unfreeze("please do this\n\nunfreeze\n")
    for no in ("please unfreeze the branch", "do not unfreeze", "unfreezing"):
        assert not mod.is_unfreeze(no), no


# ----------------------------------------------------------- the follow-up
def test_a_followup_must_not_carry_the_plan_marker(tmp_path):
    """Dedupe counts a marked PR in ANY state, so a marked follow-up makes the
    plan look dispatched twice and unbinds the status check from the PR that
    did the work. Asserted in code, not left to the prompt."""
    mod = _load(tmp_path)
    base = {"number": 9, "head": {"ref": "fb"}, "base": {"ref": "main"}}
    ok, why, _ = mod.verify_followup_pr(
        "r", "fb", 7, "0001-a",
        Forge(pulls=[dict(base, body="Follows: #7\nPlan: 0001-a")]), _ff())
    assert not ok and "Plan marker" in why

    ok, why, num = mod.verify_followup_pr(
        "r", "fb", 7, "0001-a",
        Forge(pulls=[dict(base, body="Follows: #7")]), _ff())
    assert ok and num == 9


def test_a_followup_needs_the_follows_line_and_a_main_base(tmp_path):
    mod = _load(tmp_path)
    base = {"number": 9, "head": {"ref": "fb"}, "base": {"ref": "main"}}
    ok, why, _ = mod.verify_followup_pr("r", "fb", 7, "p",
                                        Forge(pulls=[dict(base, body="nothing")]), _ff())
    assert not ok and "Follows: #7" in why
    ok, why, _ = mod.verify_followup_pr(
        "r", "fb", 7, "p",
        Forge(pulls=[dict(base, base={"ref": "dev"}, body="Follows: #7")]), _ff())
    assert not ok and "not main" in why


# ------------------------------------------------------------- the steward
class Sess:
    timed_out = False
    exit_code = 0
    transcript = None


def _drive(mod, forge, cycles=1, moves=None, tags=None):
    """Run the steward, recording which branch each session was given.

    `moves` records every git branch operation, so a test can assert the
    worktree was put back; `tags` records transcript tags."""
    ran = []
    moves = moves if moves is not None else []
    tags = tags if tags is not None else []
    mod._forge = lambda: (_ff(), forge)
    mod.release_lease = lambda lid: (True, "")
    mod.lease_branch = lambda lid: "br"
    mod.branch_from = lambda wt, base, b: (moves.append(("from", base, b)), b)[1]
    mod.checkout = lambda wt, b: (moves.append(("checkout", b)), b)[1]
    mod.verify_followup_pr = lambda *a, **k: (True, "", 99)
    mod.review_loop = lambda *a, **k: (True, "", [])
    mod.run_implementer = lambda wt, prompt, cred, route, lid, log=None, tag=None: (
        ran.append(prompt), tags.append(tag), Sess())[2]
    mod.steward("operator/x", "0001-a", "plans/p.md", "PLAN", 7, "br",
                Path("/tmp"), None, None, "lease-1", max_cycles=cycles)
    return ran


def test_an_instruction_under_a_standing_approval_goes_to_a_followup(tmp_path):
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "fix the docstring")],
              reviews=[_approval()])
    ran = _drive(mod, f)
    assert len(ran) == 1
    assert "Follows: #7" in ran[0]
    assert "MUST NOT contain a `Plan:` line" in ran[0]
    assert "is APPROVED and frozen" in ran[0]
    assert any("frozen" in c for c in f.posted)


def test_with_no_approval_the_instruction_runs_on_the_branch(tmp_path):
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "fix the docstring")], reviews=[])
    ran = _drive(mod, f)
    assert len(ran) == 1 and "Follows:" not in ran[0]


def test_unfreeze_permits_the_next_instruction_on_the_branch(tmp_path):
    """The approval stands at the head throughout, so the ONLY thing that can
    admit this instruction to the branch is the grant. Review 2418 caught an
    earlier version where a popped head meant no approval stood and the test
    passed without exercising the grant at all."""
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "unfreeze"),
                        _c(11, OWNER, "fix the docstring")],
              reviews=[_approval(sha=HEAD)], heads=[HEAD])   # never moves
    assert mod.approval_stands("r", 7, HEAD, f, _ff())[0], "precondition"
    ran = _drive(mod, f)
    assert len(ran) == 1, "the unfreeze comment itself must not spawn a session"
    assert "Follows:" not in ran[0], "the grant was not honoured"
    assert any("unfreeze` accepted" in c for c in f.posted)


def test_unfreeze_with_an_instruction_in_the_same_comment_runs_it(tmp_path):
    """`unfreeze` is a modifier, not a terminal branch. An earlier version
    granted the override and silently discarded the rest of the comment —
    acked, so it never came back. Review 2418."""
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "unfreeze\n\nalso fix the docstring")],
              reviews=[_approval(sha=HEAD)], heads=[HEAD])
    ran = _drive(mod, f)
    assert len(ran) == 1, "the instruction was discarded with the token"
    assert "also fix the docstring" in ran[0]
    assert "Follows:" not in ran[0], "the grant in the same comment was ignored"


def test_the_followup_branch_name_survives_a_respawn(tmp_path):
    """Named by the comment id, which a successor can read. A process-local
    counter restarted at zero, reused the name, and made verify_followup_pr
    report the predecessor's PR as newly opened."""
    mod = _load(tmp_path)
    mod.lease_branch = lambda lid: "br"
    first = mod.followup_branch("lease-1", 7, 10)
    assert first == mod.followup_branch("lease-1", 7, 10)   # a successor agrees
    assert first != mod.followup_branch("lease-1", 7, 11)
    assert "c10" in first


def test_an_unreadable_head_fails_closed_and_clears_the_grant(tmp_path):
    """This SUPERSEDES the r1 behaviour, and the reversal is the point.

    r1 (review 2418) asked that an unreadable head not spend the grant, so an
    owner's `unfreeze` was not wasted on a forge blip. r5 (review 2422) showed
    the other side: a head we cannot read around a session that DID push leaves
    the grant live, and the next approval is pushed over. The freeze invariant
    outranks a re-typed `unfreeze`, so unknown clears the grant — and the second
    instruction goes to a follow-up rather than onto the branch.
    """
    mod = _load(tmp_path)

    class NoHead(Forge):
        def get_pull(self, repo, n):
            return 500, None
    f = NoHead(comments=[_c(10, OWNER, "unfreeze"), _c(11, OWNER, "one"),
                         _c(12, OWNER, "two")],
               reviews=[_approval(sha=HEAD)])
    ran = _drive(mod, f)
    assert len(ran) == 2, ran
    assert "Follows:" not in ran[0], "the grant should admit the first one"
    assert "Follows: #7" in ran[1], "an unreadable head left the grant live"


def test_a_verified_followup_is_reviewed_like_any_other_pr(tmp_path):
    """A fix that skipped review because it arrived as a comment would be a
    second way into main with a weaker gate than the first."""
    mod = _load(tmp_path)
    seen = []
    f = Forge(comments=[_c(10, OWNER, "fix it")], reviews=[_approval()])
    mod._forge = lambda: (_ff(), f)
    mod.release_lease = lambda lid: (True, "")
    mod.lease_branch = lambda lid: "br"
    mod.branch_from = lambda wt, base, b: b
    mod.verify_followup_pr = lambda *a, **k: (True, "", 99)
    mod.run_implementer = lambda *a, **k: Sess()
    mod.review_loop = lambda repo, pid, pp, pt, pr, br, *a, **k: (
        seen.append((pr, br)), (True, "", []))[1]
    mod.steward("operator/x", "0001-a", "plans/p.md", "PLAN", 7, "br",
                Path("/tmp"), None, None, "lease-1", max_cycles=1)
    assert seen == [(99, "br-follows-7-c10")], seen


def test_a_failed_push_under_the_grant_does_not_spend_it(tmp_path):
    """Head never moves, so the grant survives and the SECOND instruction still
    runs on the branch rather than being pushed into a follow-up."""
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "unfreeze"),
                        _c(11, OWNER, "first try"),
                        _c(12, OWNER, "second try")],
              reviews=[_approval()], heads=[HEAD])
    ran = _drive(mod, f)
    assert len(ran) == 2
    assert all("Follows:" not in p for p in ran), "the grant was spent by a failure"


def test_the_branch_re_freezes_on_the_next_approval(tmp_path):
    """The counter-versus-condition test, and the reason the plan names it.

    A counter ("one push is owed") still says a push is owed after the next
    approval, and pushes over it. Both phases below run in ONE steward call, so
    the grant is real state carried across instructions rather than reset by a
    fresh call — review 2420 caught an earlier version where a popped head left
    no approval standing and a second `_drive` reset the grant, so a counter
    implementation passed it.

    Timeline in a single run: #10 unfreezes, #11 pushes (head moves and the
    grant is spent), a fresh approval lands at the new head, #12 must go to a
    follow-up.
    """
    mod = _load(tmp_path)

    class Reapproving(Forge):
        """An approval always stands at whatever head it last served — the old
        one goes stale on a push and a fresh one lands at the new head, which is
        what the forge actually does under dismiss-stale-approvals."""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.served = HEAD

        def get_pull(self, repo, n):
            st, body = super().get_pull(repo, n)
            self.served = body["head"]["sha"]
            return st, body

        def list_reviews(self, repo, n, page=1, limit=50):
            if page > 1:
                return 200, []
            return 200, [_approval(sha=self.served)]

    f = Reapproving(comments=[_c(10, OWNER, "unfreeze"),
                              _c(11, OWNER, "do the fix"),
                              _c(12, OWNER, "one more thing")],
                    heads=[HEAD, HEAD, HEAD2, HEAD2, HEAD2, HEAD2, HEAD2, HEAD2])
    ran = _drive(mod, f)
    assert len(ran) == 2, ran
    assert "Follows:" not in ran[0], "the grant did not admit #11"
    assert "Follows: #7" in ran[1], "the second approval was pushed over"


# ------------------------------------------------------------------- armed
def test_the_wrapper_reports_ready(tmp_path):
    mod = _load(tmp_path)
    assert mod.UNBUILT_PHASES == ()
    assert mod.main(["--ready"]) == 0


def test_no_approval_endpoint_is_reachable_from_the_freeze(tmp_path):
    """The absence of the call is the control, and this phase is the one that
    would most plausibly want it."""
    src = (BIN / "orchestrator").read_text()
    for n, line in enumerate(src.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        assert not ("/reviews" in line and
                    any(w in line.lower() for w in ("post", "put", "patch", "submit"))), n


# ------------------------------------------- what round 2 of review 2418 found
def test_the_worktree_is_returned_to_the_approved_branch_after_a_followup(tmp_path):
    """Left on the follow-up branch, a later granted instruction is told in its
    RUN FACTS that it is on the approved branch while HEAD is elsewhere — and
    would commit that fix onto the follow-up. Review 2419."""
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "fix it")], reviews=[_approval()])
    moves = []
    _drive(mod, f, moves=moves)
    assert ("from", "br", "br-follows-7-c10") in moves
    assert moves[-1] == ("checkout", "br"), moves


def test_a_frozen_branch_that_moves_during_a_followup_pages(tmp_path):
    """The freeze is enforced, not merely routed around: the session held a
    token and could have pushed the approved branch anyway."""
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "fix it")], reviews=[_approval()],
              heads=[HEAD, HEAD, HEAD2, HEAD2])
    paged = []
    mod.notify = lambda t, b: (paged.append(t), True)[1]
    _drive(mod, f)
    assert paged == ["Frozen branch was pushed"], paged
    assert any("frozen branch moved" in c.lower() for c in f.posted)


def test_transcript_tags_do_not_collide_across_the_three_loops(tmp_path):
    """The initial loop, a follow-up's and a post-unfreeze re-review all used
    r1, r2, ... and O_TRUNC'd each other. Review 2419."""
    mod = _load(tmp_path)
    seen = []
    mod.run_implementer = lambda *a, **k: (seen.append(k.get("tag")), Sess())[1]
    forge = Forge(reviews=[{"id": 1, "state": "REQUEST_CHANGES", "commit_id": HEAD,
                            "stale": False, "dismissed": False,
                            "body": "```findings\nHIGH: broken\n```",
                            "user": {"id": OWNER, "login": "operator"}}],
                  heads=[HEAD, HEAD2, HEAD2, HEAD2, HEAD2, HEAD2])
    mod._forge = lambda: (_ff(), forge)
    mod.dispatch_review = lambda *a, **k: (True, "")
    mod.review_bound = lambda repo, path=None: 2
    mod.review_loop("r", "p", "pp", "PLAN", 7, "br", Path("/tmp"), None, None,
                    "lease-1", tag_prefix="f10r")
    assert seen and all(t.startswith("f10r") for t in seen), seen


def test_the_worktree_returns_only_after_the_followups_own_review(tmp_path):
    """The HIGH from review 2420, introduced by the fix for review 2419.

    The follow-up's review loop spawns fix sessions of its own. Returning the
    worktree to the approved branch BEFORE that loop meant every one of those
    sessions was told it was on `fb` while HEAD sat on the frozen branch — and
    the freeze check had already run, so a push there was undetected as well as
    unblocked. The order is: branch, work, verify, review, THEN return.
    """
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "fix it")], reviews=[_approval()])
    order = []
    mod._forge = lambda: (_ff(), f)
    mod.release_lease = lambda lid: (True, "")
    mod.lease_branch = lambda lid: "br"
    mod.branch_from = lambda wt, base, b: (order.append("branch_from"), b)[1]
    mod.checkout = lambda wt, b: (order.append("checkout"), b)[1]
    mod.verify_followup_pr = lambda *a, **k: (order.append("verify"), (True, "", 99))[1]
    mod.review_loop = lambda *a, **k: (order.append("review"), (True, "", []))[1]
    mod.run_implementer = lambda *a, **k: (order.append("session"), Sess())[1]
    mod.steward("operator/x", "0001-a", "plans/p.md", "PLAN", 7, "br",
                Path("/tmp"), None, None, "lease-1", max_cycles=1)
    assert order == ["branch_from", "session", "verify", "review", "checkout"], order
    assert order.index("review") < order.index("checkout"), \
        "fix sessions in the follow-up's review ran on the frozen branch"


def test_the_freeze_check_runs_even_when_the_followup_did_not_verify(tmp_path):
    """A session that failed verification is the one most likely to have pushed
    somewhere unintended, so the head check must not be skipped for it."""
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "fix it")], reviews=[_approval()],
              heads=[HEAD, HEAD, HEAD2, HEAD2])
    paged = []
    mod.notify = lambda t, b: (paged.append(t), True)[1]
    mod._forge = lambda: (_ff(), f)
    mod.release_lease = lambda lid: (True, "")
    mod.lease_branch = lambda lid: "br"
    mod.branch_from = lambda wt, base, b: b
    mod.checkout = lambda wt, b: b
    mod.verify_followup_pr = lambda *a, **k: (False, "no such PR", None)
    mod.review_loop = lambda *a, **k: pytest.fail("must not review an unverified PR")
    mod.run_implementer = lambda *a, **k: Sess()
    mod.steward("operator/x", "0001-a", "plans/p.md", "PLAN", 7, "br",
                Path("/tmp"), None, None, "lease-1", max_cycles=1)
    assert paged == ["Frozen branch was pushed"], paged


def test_a_followup_carrying_any_plan_marker_is_rejected(tmp_path):
    """The docstring and the prompt promise no `Plan:` line at all; an earlier
    version only rejected THIS plan's id, so a follow-up marked with a different
    plan passed. Review 2420."""
    mod = _load(tmp_path)
    base = {"number": 9, "head": {"ref": "fb"}, "base": {"ref": "main"},
            "body": "Follows: #7\nPlan: 0099-something-else"}
    ok, why, _ = mod.verify_followup_pr("r", "fb", 7, "0001-a",
                                        Forge(pulls=[base]), _ff())
    assert not ok and "Plan marker" in why


# --------------------------------------------- what round 4 of the loop found
def test_unfreeze_matches_the_backticked_form_the_ack_asks_for(tmp_path):
    """The wrapper's own ack says "post `unfreeze` on its own line". An owner
    copying that literally posted a token the pattern did not match. Review 2421."""
    mod = _load(tmp_path)
    assert mod.is_unfreeze("`unfreeze`")
    assert mod.is_unfreeze("do the thing\n\n`unfreeze`\n")
    for no in ("please `unfreeze` the branch", "`unfreezing`"):
        assert not mod.is_unfreeze(no), no


def test_the_follows_line_is_anchored_not_a_substring(tmp_path):
    """`Follows: #71` satisfied PR 7 under a substring test. Review 2421."""
    mod = _load(tmp_path)
    base = {"number": 9, "head": {"ref": "fb"}, "base": {"ref": "main"}}
    ok, why, _ = mod.verify_followup_pr(
        "r", "fb", 7, "p", Forge(pulls=[dict(base, body="Follows: #71")]), _ff())
    assert not ok and "Follows: #7" in why
    ok, _, _ = mod.verify_followup_pr(
        "r", "fb", 7, "p", Forge(pulls=[dict(base, body="Follows: #7")]), _ff())
    assert ok


def test_a_push_on_an_unfrozen_branch_is_still_reviewed(tmp_path):
    """Re-review is keyed on the head MOVING, not on the grant. An instruction
    on an unfrozen branch pushes too, and keying this on the grant left that
    push unreviewed. Review 2421."""
    mod = _load(tmp_path)
    reviewed = []
    f = Forge(comments=[_c(10, OWNER, "fix it")], reviews=[],   # nothing stands
              heads=[HEAD, HEAD, HEAD2, HEAD2, HEAD2])
    mod._forge = lambda: (_ff(), f)
    mod.release_lease = lambda lid: (True, "")
    mod.lease_branch = lambda lid: "br"
    mod.run_implementer = lambda *a, **k: Sess()
    mod.review_loop = lambda *a, **k: (reviewed.append(k.get("tag_prefix")),
                                       (True, "", []))[1]
    mod.steward("operator/x", "0001-a", "plans/p.md", "PLAN", 7, "br",
                Path("/tmp"), None, None, "lease-1", max_cycles=1)
    assert reviewed == ["u10r"], reviewed


def test_a_persistent_refusal_pages_once_not_every_cycle(tmp_path):
    """An unreadable lease refuses every cycle; at a 60s poll that is a page a
    minute for one fault. The log keeps every occurrence. Review 2421."""
    mod = _load(tmp_path)
    paged = []
    f = Forge(comments=[_c(10, OWNER, "fix it")], reviews=[_approval()])
    mod._forge = lambda: (_ff(), f)
    mod.release_lease = lambda lid: (True, "")
    mod.notify = lambda t, b: (paged.append(b), True)[1]
    mod.lease_branch = lambda lid: (_ for _ in ()).throw(
        mod.Stop("lease-1 has no record — cannot learn the branch name"))
    mod.steward("operator/x", "0001-a", "plans/p.md", "PLAN", 7, "br",
                Path("/tmp"), None, None, "lease-1", max_cycles=3)
    assert len(paged) == 1, paged


# ------------------------------------------------ what round 5 of the loop found
def test_every_tag_the_code_generates_is_actually_writable(tmp_path):
    """The HIGH from review 2422, and the test that would have caught it.

    Tags were validated against `lib._ID_RE` — the LEASE-ID shape
    (`type--slug--nnn`) — which no tag this file generates can match. Every fix
    round, steward instruction and follow-up session therefore raised Stop AFTER
    doing its work and was reported as refused: the whole of 0011/0031/0032 was
    broken at runtime.

    The suite did not see it because every test that reaches those paths stubs
    `run_implementer`. So this one calls the REAL write_transcript with the real
    shapes, which is the only thing that could have failed.
    """
    mod = _load(tmp_path)
    os.environ["EUNOMIA_TRANSCRIPT_DIR"] = str(tmp_path / "transcripts")
    lease = "branch--demo--001"
    for tag in ("r1", "r12", "c10", "f10", "f10r1", "u10r2"):
        path, _ = mod.write_transcript(lease, "hello", tag)
        assert path.exists() and path.name == f"{lease}-{tag}.log", tag
    path, _ = mod.write_transcript(lease, "hello")
    assert path.name == f"{lease}.log"


def test_a_traversing_tag_is_still_refused(tmp_path):
    """Loosening the pattern must not have opened the hole it was guarding."""
    mod = _load(tmp_path)
    os.environ["EUNOMIA_TRANSCRIPT_DIR"] = str(tmp_path / "transcripts")
    for bad in ("../x", "a/b", ".", "..", "", "a.b", "-lead"):
        with pytest.raises(mod.Stop):
            mod.write_transcript("branch--demo--001", "x", bad)


def test_a_followup_that_raises_still_returns_the_worktree_and_checks_the_head(tmp_path):
    """A raise AFTER the session pushed left the worktree on `fb` and the frozen
    branch possibly moved, with the cleanup skipped by a `continue`. Review 2422."""
    mod = _load(tmp_path)
    f = Forge(comments=[_c(10, OWNER, "fix it")], reviews=[_approval()],
              heads=[HEAD, HEAD, HEAD2, HEAD2])
    moves, paged = [], []
    mod._forge = lambda: (_ff(), f)
    mod.release_lease = lambda lid: (True, "")
    mod.lease_branch = lambda lid: "br"
    mod.notify = lambda t, b: (paged.append(t), True)[1]
    mod.branch_from = lambda wt, base, b: (moves.append(("from", b)), b)[1]
    mod.checkout = lambda wt, b: (moves.append(("checkout", b)), b)[1]
    mod.review_loop = lambda *a, **k: pytest.fail("must not review a failed session")

    def boom(*a, **k):
        raise RuntimeError("died after pushing")
    mod.run_implementer = boom
    mod.steward("operator/x", "0001-a", "plans/p.md", "PLAN", 7, "br",
                Path("/tmp"), None, None, "lease-1", max_cycles=1)
    assert ("checkout", "br") in moves, moves
    assert paged == ["Frozen branch was pushed"], paged
