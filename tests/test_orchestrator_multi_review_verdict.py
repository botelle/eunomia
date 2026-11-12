"""Every review at the head counts (2026-10-06, eunomia#739, lynceus#106).

`dispatch-review` runs two reviewers. When ordinal 2 raises a HIGH, talos withholds
ordinal 1's approval: ordinal 1 posts as COMMENT with a paragraph after its own
fence, and ordinal 2's HIGH lives only in ordinal 2's own review. The orchestrator
used to read just the newest review, saw no HIGH, and stopped the run on "no
parseable blocking finding" instead of sending the HIGH to a fix round. Eight fleet
runs died on it.

The bodies below are trimmed from eunomia#739 reviews 4003 (ordinal 2) and 4004
(ordinal 1); the fences, VERDICT lines, provenance lines and the withheld paragraph
are kept as posted.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

SHA1 = "1" * 40
SHA2 = "2" * 40

HIGH2 = ("after a redispatch, orphan_sweep's respawn path hardcodes feat/<slug> "
         "instead of the lease's feat/<slug>-rN branch, so `respawn` resumes onto "
         "the old merged branch or starts a duplicate fresh dispatch")

# review 4003: ordinal 2, role annotation, COMMENT, HIGH in its fence
ORD2_BODY = f"""# Review of PR #739: `redispatch` for a closed or merged marked PR (plan 0086)

## HIGH

### H1. After a redispatch, `respawn` resumes or rebuilds on the wrong branch

## Not in this PR

None.

```findings
HIGH: {HIGH2}
LOW: dispatch loop now issues a PR-comments read every cycle for each ready plan whose newest marked PR is closed (unreported per-cycle forge cost)
```

VERDICT: COMMENT

Review-provenance: model=claude-opus-5-5; session=cd7f5eed-7136-4671-b93e-04d229206dfd; position=code_reviewer; bar=high; file-list=complete; model-source=configured; same-session-as-author=spawned-subagent
Review-provenance-ordinal: 2
Review-provenance-role: annotation

"""

# review 4004: ordinal 1, role verdict, posted as COMMENT because approval withheld
ORD1_BODY = f"""# Review of PR #739: `redispatch` for a closed or merged marked PR (plan 0086)

## Not in this PR

- The `resume_request` branch of `orphan_sweep` inherits the MEDIUM above.

```findings
MEDIUM: respawn on a redispatched plan hardcodes feat/<slug>, resumes the old merged branch and releases the -rN lease that covers the open PR
LOW: fail-redispatch marker only clears on a successful spawn, so a second failed start after the owner re-posts pages nothing
```

VERDICT: APPROVE

Review-provenance: model=claude-fable-5-1; session=2d388b36-d9d6-4539-9fb6-1874061fc7d9; position=code_reviewer; bar=high; file-list=complete; model-source=configured; same-session-as-author=spawned-subagent
Review-provenance-ordinal: 1
Review-provenance-role: verdict


> The approval is withheld: another reviewer's round did not clear.
- ordinal 2 (claude-opus-5-5): HIGH: {HIGH2}
"""

CLEAN = "```findings\nLOW: a typo\n```\n\nVERDICT: APPROVE\n"


def _fake_bin(tmp_path, reviewers=(1, 2), bar="high", models_exit=0):
    """Stand-ins for `fleet-models reviewers` and `fleet-config get review_bar`,
    which would otherwise read the real fleet database (plan 0132)."""
    d = tmp_path / "cfgbin"
    d.mkdir(exist_ok=True)
    rows = [{"ordinal": o, "runner": "r", "model": "m"} for o in reviewers]
    (d / "fleet-models").write_text(
        f"#!/bin/sh\n[ {models_exit} -ne 0 ] && exit {models_exit}\n"
        f"cat <<'EOF'\n{json.dumps(rows)}\nEOF\n")
    (d / "fleet-config").write_text(
        f"#!/bin/sh\necho '{json.dumps({'key': 'review_bar', 'value': bar})}'\n")
    for f in d.iterdir():
        f.chmod(0o755)
    return d


def _load(tmp_path):
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    os.environ["EUNOMIA_SESSION"] = "multi-review-test"
    os.environ.pop("FLEET_REVIEW_CMD", None)
    loader = importlib.machinery.SourceFileLoader(
        "orchestrator_multi", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator_multi", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod._sleep = lambda seconds: None
    mod.CONFIG_BIN = _fake_bin(tmp_path)
    return mod


def _fleetforge():
    loader = importlib.machinery.SourceFileLoader(
        "ff_multi", str(BIN / "fleetforge.py"))
    spec = importlib.util.spec_from_loader("ff_multi", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _rev(id_, user, state, body="", sha=SHA1, **kw):
    return dict({"id": id_, "user": {"login": user}, "state": state,
                 "body": body, "commit_id": sha, "stale": False,
                 "dismissed": False}, **kw)


class Forge:
    def __init__(self, reviews, head=SHA1, status=200):
        self.reviews, self.head, self.status = reviews, head, status

    def get_pull(self, repo, n):
        return 200, {"head": {"sha": self.head}}

    def list_reviews(self, repo, n, page=1, limit=50):
        if self.status != 200:
            return self.status, None
        return 200, (self.reviews if page == 1 else [])

    def get_commit_status(self, repo, sha):
        return 200, {"state": "success",
                     "statuses": [{"context": "ci", "status": "success"}]}


def _outcome(mod, reviews, sha=SHA1):
    at = mod.reviews_at("operator/x", 7, sha, Forge(reviews), _fleetforge())
    assert at is not None
    return mod.head_outcome(at)


def _shape_739():
    return [_rev(4003, "revbot", "COMMENT", ORD2_BODY),
            _rev(4004, "revbot", "COMMENT", ORD1_BODY)]


# (a)
def test_a_second_reviewers_high_blocks_when_the_latest_review_is_a_comment(tmp_path):
    mod = _load(tmp_path)
    approved, blockers = _outcome(mod, _shape_739())
    assert not approved
    assert blockers == [HIGH2]


def test_the_withheld_paragraph_is_not_parsed(tmp_path):
    """Ordinal 1's body alone carries the HIGH only in prose after its fence."""
    mod = _load(tmp_path)
    approved, blockers = _outcome(mod, [_rev(4004, "revbot", "COMMENT",
                                             ORD1_BODY)])
    assert not approved and blockers == []


# (b)
def test_two_approvals_from_one_account_are_approved(tmp_path):
    mod = _load(tmp_path)
    approved, blockers = _outcome(mod, [
        _rev(1, "revbot", "APPROVED", CLEAN),
        _rev(2, "revbot", "APPROVED", CLEAN)])
    assert approved and blockers == []


# (c)
def test_one_accounts_approval_cannot_cover_anothers_comment(tmp_path):
    mod = _load(tmp_path)
    approved, blockers = _outcome(mod, [
        _rev(1, "revbot", "APPROVED", CLEAN),
        _rev(2, "rev2bot", "COMMENT", ORD2_BODY)])
    assert not approved
    assert blockers == [HIGH2]


# (d)
def test_two_accounts_both_approving_are_approved(tmp_path):
    mod = _load(tmp_path)
    approved, blockers = _outcome(mod, [
        _rev(1, "revbot", "APPROVED", CLEAN),
        _rev(2, "rev2bot", "APPROVED", CLEAN)])
    assert approved and blockers == []


def test_an_accounts_latest_review_is_the_one_that_counts(tmp_path):
    mod = _load(tmp_path)
    approved, _ = _outcome(mod, [_rev(1, "rev2bot", "COMMENT", ORD2_BODY),
                                 _rev(2, "rev2bot", "APPROVED", CLEAN)])
    assert approved


def test_identical_findings_are_deduplicated(tmp_path):
    mod = _load(tmp_path)
    _, blockers = _outcome(mod, [_rev(1, "a", "COMMENT", ORD2_BODY),
                                 _rev(2, "b", "COMMENT", ORD2_BODY)])
    assert blockers == [HIGH2]


# (e)
def test_reviews_at_an_older_sha_are_ignored(tmp_path):
    mod = _load(tmp_path)
    old = [_rev(1, "rev2bot", "COMMENT", ORD2_BODY, sha=SHA2)]
    cur = [_rev(2, "revbot", "APPROVED", CLEAN, sha=SHA1)]
    approved, blockers = _outcome(mod, old + cur, sha=SHA1)
    assert approved and blockers == []
    assert _outcome(mod, old, sha=SHA1) == (False, [])
    for flag in ("stale", "dismissed"):
        r = _rev(3, "rev2bot", "COMMENT", ORD2_BODY, **{flag: True})
        assert _outcome(mod, cur + [r]) == (True, [])


# (f)
def test_an_unreadable_forge_is_none_not_a_rejection(tmp_path):
    mod = _load(tmp_path)
    forge = Forge([], status=500)
    assert mod.reviews_at("operator/x", 7, SHA1, forge, _fleetforge()) is None
    assert mod.verdict_at("operator/x", 7, SHA1, forge, _fleetforge()) is None


# (g)
def _loop(mod, tmp_path, forge, impl):
    mod.review_bound = lambda repo, path=None: 3
    mod._forge = lambda: (_fleetforge(), forge)
    mod.dispatch_review = lambda *a, **k: (True, "")
    mod.run_implementer = impl
    return mod.review_loop("operator/x", "0001-a", "plans/p.md", "PLAN", 7,
                           "br", tmp_path, None, None, "lease-1")


def test_review_loop_sends_the_second_reviewers_high_to_a_fix_round(tmp_path):
    mod = _load(tmp_path)
    prompts = []

    def impl(wt, prompt, *a, **k):
        prompts.append(prompt)
        raise mod.Stop("test ends after the first fix round")

    ok, why, rows = _loop(mod, tmp_path, Forge(_shape_739()), impl)
    assert not ok
    assert "no parseable blocking finding" not in why
    assert "fix session refused" in why
    assert len(prompts) == 1 and f"- {HIGH2}" in prompts[0]
    assert rows[0]["blocking"] == 1


def test_review_loop_still_stops_when_nothing_parses_anywhere(tmp_path):
    mod = _load(tmp_path)
    forge = Forge([_rev(1, "revbot", "COMMENT", "no fence, no findings")])
    ok, why, _ = _loop(mod, tmp_path, forge, lambda *a, **k: pytest.fail("fix ran"))
    assert not ok and "no parseable blocking finding" in why


def test_review_loop_approves_when_every_account_approves(tmp_path):
    mod = _load(tmp_path)
    forge = Forge([_rev(1, "revbot", "APPROVED", CLEAN),
                   _rev(2, "rev2bot", "APPROVED", CLEAN)])
    ok, why, _ = _loop(mod, tmp_path, forge, lambda *a, **k: pytest.fail("fix ran"))
    assert ok and why == ""


# ---- a half-approved head is neither approved nor frozen (2026-10-12,
# eunomia#837/#848/#850). The #850 shape: rev2bot COMMENT with a HIGH, then
# revbot APPROVED posted LAST at the same sha.
def _shape_850(rev2bot_first=True):
    rev2bot = _rev(10, "rev2bot", "COMMENT", ORD2_BODY)
    rbot = _rev(11, "revbot", "APPROVED", CLEAN)
    if not rev2bot_first:
        rev2bot, rbot = _rev(11, "rev2bot", "COMMENT", ORD2_BODY), _rev(
            10, "revbot", "APPROVED", CLEAN)
    return [rev2bot, rbot] if rev2bot_first else [rbot, rev2bot]


@pytest.mark.parametrize("rev2bot_first", [True, False])
def test_review_loop_does_not_approve_when_reviewers_split_in_either_posting_order(
        tmp_path, rev2bot_first):
    mod = _load(tmp_path)
    prompts = []

    def impl(wt, prompt, *a, **k):
        prompts.append(prompt)
        raise mod.Stop("test ends after the first fix round")

    ok, why, rows = _loop(mod, tmp_path, Forge(_shape_850(rev2bot_first)), impl)
    assert not ok
    assert "fix session refused" in why
    assert len(prompts) == 1 and f"- {HIGH2}" in prompts[0]
    assert rows[0]["verdict"] != "APPROVED"
    assert rows[0]["verdict"] == "COMMENT"
    assert rows[0]["blocking"] == 1


def test_review_loop_both_approved_is_still_approved(tmp_path):
    mod = _load(tmp_path)
    forge = Forge([_rev(1, "rev2bot", "APPROVED", CLEAN),
                   _rev(2, "revbot", "APPROVED", CLEAN)])
    ok, why, rows = _loop(mod, tmp_path, forge,
                          lambda *a, **k: pytest.fail("fix ran"))
    assert ok and why == "" and rows[0]["verdict"] == "APPROVED"


def test_review_loop_records_the_withholders_own_state(tmp_path):
    mod = _load(tmp_path)
    reviews = [_rev(10, "rev2bot", "REQUEST_CHANGES", ORD2_BODY),
               _rev(11, "revbot", "APPROVED", CLEAN)]

    def impl(*a, **k):
        raise mod.Stop("stop")

    _, _, rows = _loop(mod, tmp_path, Forge(reviews), impl)
    assert rows[0]["verdict"] == "REQUEST_CHANGES"


def _stands(mod, reviews, forge=None):
    return mod.approval_stands("operator/x", 7, SHA1, forge or Forge(reviews),
                               _fleetforge())


def test_approval_stands_half_approved_is_not_frozen_and_names_the_withholder(
        tmp_path):
    mod = _load(tmp_path)
    for state in ("COMMENT", "REQUEST_CHANGES"):
        stands, why = _stands(mod, [_rev(1, "rev2bot", state, ORD2_BODY),
                                    _rev(2, "revbot", "APPROVED", CLEAN)])
        assert stands is False
        assert "rev2bot" in why and "revbot" in why


def test_approval_stands_both_approved_is_frozen(tmp_path):
    mod = _load(tmp_path)
    stands, why = _stands(mod, [_rev(1, "revbot", "APPROVED", CLEAN),
                                _rev(2, "rev2bot", "APPROVED", CLEAN)])
    assert stands is True and "revbot" in why and "rev2bot" in why


def test_approval_stands_lone_approval_with_no_other_review_is_frozen(tmp_path):
    mod = _load(tmp_path)
    stands, why = _stands(mod, [_rev(1, "revbot", "APPROVED", CLEAN)])
    assert stands is True and "revbot approved" in why


def test_approval_stands_no_approval_is_not_frozen(tmp_path):
    mod = _load(tmp_path)
    assert _stands(mod, [_rev(1, "rev2bot", "COMMENT", ORD2_BODY)]) == (False, "")


def test_approval_stands_unreadable_is_frozen(tmp_path):
    mod = _load(tmp_path)
    stands, why = _stands(mod, [], Forge([], status=500))
    assert stands is True and "unreadable" in why


def test_approval_stands_a_non_reviewer_comment_never_unfreezes(tmp_path):
    mod = _load(tmp_path)
    base = [_rev(1, "revbot", "APPROVED", CLEAN),
            _rev(2, "rev2bot", "APPROVED", CLEAN)]
    for who in ("operator", "implbot", "someone-else"):
        stands, _ = _stands(mod, base + [_rev(3, who, "COMMENT", "inline note")])
        assert stands is True, who


def test_approval_stands_comment_after_own_approval_does_not_withhold(tmp_path):
    mod = _load(tmp_path)
    stands, why = _stands(mod, [_rev(1, "revbot", "APPROVED", CLEAN),
                                _rev(2, "revbot", "COMMENT", "a nit"),
                                _rev(3, "rev2bot", "APPROVED", CLEAN)])
    assert stands is True and why.count("revbot") == 1


def test_approval_stands_request_changes_after_approval_withholds(tmp_path):
    mod = _load(tmp_path)
    stands, why = _stands(mod, [_rev(1, "revbot", "APPROVED", CLEAN),
                                _rev(2, "rev2bot", "APPROVED", CLEAN),
                                _rev(3, "rev2bot", "REQUEST_CHANGES", ORD2_BODY)])
    assert stands is False
    assert "rev2bot withholds" in why and "revbot approved" in why
    assert "rev2bot approved" not in why and ", rev2bot approved" not in why


def test_approval_stands_reviewer_names_are_case_insensitive(tmp_path):
    mod = _load(tmp_path)
    stands, _ = _stands(mod, [_rev(1, "revbot", "APPROVED", CLEAN),
                              _rev(2, "Rev2bot", "COMMENT", ORD2_BODY)])
    assert stands is False


def test_review_loop_account_that_approved_then_withheld_is_not_approved(tmp_path):
    """Per-account-latest rule: rev2bot APPROVED then REQUEST_CHANGES at one head."""
    mod = _load(tmp_path)
    reviews = [_rev(1, "rev2bot", "APPROVED", CLEAN),
               _rev(2, "revbot", "APPROVED", CLEAN),
               _rev(3, "rev2bot", "REQUEST_CHANGES", ORD2_BODY)]

    def impl(*a, **k):
        raise mod.Stop("stop")

    ok, _, rows = _loop(mod, tmp_path, Forge(reviews), impl)
    assert not ok and rows[0]["verdict"] == "REQUEST_CHANGES"
    assert rows[0]["blocking"] == 1


# ---- plan 0132: approved means every CONFIGURED reviewer approved, and a
# withholding reviewer's own findings (at the repository's bar) go to the fix round.
MED = "the retry path swallows a forge 500 and reports success"
HIGH_D = "rev2bot: the lease is released before the push lands"
LOW_D = "rev2bot: a log line misnames the round"
MED_D = "rev2bot: the bound is read once per run, not per round"


def _body(*lines):
    return "```findings\n" + "\n".join(lines) + "\n```\n\nVERDICT: COMMENT\n"


def _expect(mod, tmp_path, reviews, **cfg):
    mod.CONFIG_BIN = _fake_bin(tmp_path, **cfg)
    expected = mod.expected_reviewers("operator/x", "code_reviewer")
    at = mod.reviews_at("operator/x", 7, SHA1, Forge(reviews), _fleetforge())
    return at, expected


def _fix_prompts(mod, tmp_path, reviews, **cfg):
    mod.CONFIG_BIN = _fake_bin(tmp_path, **cfg)
    prompts = []

    def impl(wt, prompt, *a, **k):
        prompts.append(prompt)
        raise mod.Stop("test ends after the first fix round")

    return _loop(mod, tmp_path, Forge(reviews), impl), prompts


def test_d6_1_both_expected_accounts_approved(tmp_path):
    mod = _load(tmp_path)
    at, exp = _expect(mod, tmp_path, [_rev(1, "revbot", "APPROVED", CLEAN),
                                      _rev(2, "rev2bot", "APPROVED", CLEAN)])
    assert exp == ["revbot", "rev2bot"]
    assert mod.head_outcome(at, exp)[0] is True


def test_d6_2_rev2bots_high_goes_to_a_fix_round(tmp_path):
    mod = _load(tmp_path)
    (res, prompts) = _fix_prompts(mod, tmp_path, [
        _rev(1, "revbot", "APPROVED", CLEAN),
        _rev(2, "rev2bot", "COMMENT", _body(f"HIGH: {HIGH_D}"))])
    assert "fix session refused" in res[1]
    assert len(prompts) == 1 and f"- {HIGH_D}" in prompts[0]


def test_d6_3_bar_medium_a_lone_medium_is_a_fix_round(tmp_path):
    mod = _load(tmp_path)
    res, prompts = _fix_prompts(mod, tmp_path, [
        _rev(1, "revbot", "APPROVED", CLEAN),
        _rev(2, "rev2bot", "COMMENT", _body(f"MEDIUM: {MED_D}"))], bar="medium")
    assert "fix session refused" in res[1]
    assert len(prompts) == 1 and f"- {MED_D}" in prompts[0]
    assert res[2][0]["blocking"] == 1


def test_d6_4_bar_high_sends_the_high_only(tmp_path):
    mod = _load(tmp_path)
    _, prompts = _fix_prompts(mod, tmp_path, [
        _rev(1, "rev2bot", "COMMENT",
             _body(f"HIGH: {HIGH_D}", f"MEDIUM: {MED_D}", f"MEDIUM: {MED}"))])
    assert f"- {HIGH_D}" in prompts[0]
    assert MED_D not in prompts[0] and MED not in prompts[0]


def test_d6_5_findings_all_below_the_bar_are_all_sent(tmp_path):
    mod = _load(tmp_path)
    _, prompts = _fix_prompts(mod, tmp_path, [
        _rev(1, "revbot", "APPROVED", CLEAN),
        _rev(2, "rev2bot", "COMMENT", _body(f"MEDIUM: {MED_D}", f"LOW: {LOW_D}"))])
    assert f"- {MED_D}" in prompts[0] and f"- {LOW_D}" in prompts[0]


def test_d6_6_a_missing_reviewer_is_a_stop_not_a_fix_round(tmp_path):
    mod = _load(tmp_path)
    (ok, why, rows), prompts = _fix_prompts(
        mod, tmp_path, [_rev(1, "revbot", "APPROVED", CLEAN)])
    assert not ok and prompts == []
    assert f"round 1: rev2bot posted no review at {SHA1[:8]}" in why
    assert "respawn to re-review" in why


def test_d6_7_one_configured_reviewer_approved(tmp_path):
    mod = _load(tmp_path)
    at, exp = _expect(mod, tmp_path, [_rev(1, "revbot", "APPROVED", CLEAN)],
                      reviewers=(1,))
    assert exp == ["revbot"] and mod.head_outcome(at, exp)[0] is True


def test_d6_8_an_unexpected_accounts_approval_does_not_approve(tmp_path):
    mod = _load(tmp_path)
    at, exp = _expect(mod, tmp_path, [_rev(1, "operator", "APPROVED", CLEAN),
                                      _rev(2, "rev2bot", "APPROVED", CLEAN)])
    assert mod.head_outcome(at, exp)[0] is False
    # ... and an unexpected COMMENT never blocks an otherwise approved head.
    at, exp = _expect(mod, tmp_path, [_rev(1, "revbot", "APPROVED", CLEAN),
                                      _rev(2, "rev2bot", "APPROVED", CLEAN),
                                      _rev(3, "operator", "COMMENT", "hi")])
    assert mod.head_outcome(at, exp)[0] is True


def test_d6_9_the_bound_exhausted_names_the_withholders(tmp_path):
    mod = _load(tmp_path)
    mod.CONFIG_BIN = _fake_bin(tmp_path)

    class Moving(Forge):
        """Every fix session pushes: the head moves, and the same two reviewers
        stand at the new head, rev2bot still withholding."""
        def list_reviews(self, repo, n, page=1, limit=50):
            if page != 1:
                return 200, []
            return 200, [_rev(1, "revbot", "APPROVED", CLEAN, sha=self.head),
                         _rev(2, "rev2bot", "COMMENT",
                              _body(f"HIGH: {HIGH_D}", f"HIGH: {MED_D}"),
                              sha=self.head)]

    forge = Moving([])
    pushes = iter(["2" * 40, "3" * 40])

    def impl(*a, **k):
        forge.head = next(pushes)
        return type("S", (), {"timed_out": False})()

    ok, why, _ = _loop(mod, tmp_path, forge, impl)
    assert not ok and "review bound 3 exhausted" in why
    assert "rev2bot (2 findings open)" in why and "revbot (" not in why


def test_d6_10_the_739_shape_with_one_configured_reviewer(tmp_path):
    mod = _load(tmp_path)
    res, prompts = _fix_prompts(mod, tmp_path, _shape_739(), reviewers=(1,))
    assert "fix session refused" in res[1]
    assert f"- {HIGH2}" in prompts[0]


def test_d6_11_a_readded_second_reviewer_is_still_rev2bot(tmp_path):
    mod = _load(tmp_path)
    d = _fake_bin(tmp_path, reviewers=(1, 3))
    mod.CONFIG_BIN = d
    assert mod.expected_reviewers("operator/x", "code_reviewer") == [
        "revbot", "rev2bot"]


def test_d6_12_fleet_models_failing_stops_the_round(tmp_path):
    mod = _load(tmp_path)
    (ok, why, rows), prompts = _fix_prompts(
        mod, tmp_path, [_rev(1, "revbot", "APPROVED", CLEAN)],
        models_exit=3)
    assert not ok and prompts == [] and rows == []
    assert "fleet-models reviewers" in why and "exited 3" in why


def test_d6_13_a_third_reviewer_has_no_account(tmp_path):
    mod = _load(tmp_path)
    (ok, why, _), prompts = _fix_prompts(
        mod, tmp_path, [_rev(1, "revbot", "APPROVED", CLEAN)],
        reviewers=(1, 2, 3))
    assert not ok and prompts == []
    assert "reviewer 3 of code_reviewer has no account" in why


def test_a_plans_only_lane_asks_for_the_plan_reviewer(tmp_path):
    mod = _load(tmp_path)
    prose = "---\npaths:\n  - \"plans/0001-a.md\"\n  - plans/0002-b.md\n---\n"
    code = prose.replace("plans/0002-b.md", "bin/orchestrator")
    assert mod.review_position(prose) == "plan_reviewer"
    assert mod.review_position(code) == "code_reviewer"
    assert mod.review_position("PLAN") == "code_reviewer"


def test_a_docs_lane_is_not_plans_kind_and_the_plans_own_path_is_dropped(tmp_path):
    mod = _load(tmp_path)
    head = "---\nid: 0118-x\npaths:\n  - plans/0118-x.md\n  - %s\n---\n"
    assert mod.review_position(head % "docs/y.md") == "code_reviewer"
    assert mod.review_position(head % "bin/orchestrator") == "code_reviewer"
    assert mod.review_position(head % "plans/0119-z.md") == "plan_reviewer"
    only = "---\nid: 0118-x\npaths:\n  - plans/0118-x.md\n---\n"
    assert mod.review_position(only) == "plan_reviewer"
