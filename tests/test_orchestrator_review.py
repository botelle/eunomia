"""Tests for bin/orchestrator part 2b — the review loop (plan 0011).

No network and no model. The forge is a fake object answering the real
functions, and the review command is a real spawned script, because the two
things most worth proving are process facts: which flags the command is
actually given, and that the verdict is read back off the forge rather than
from that command's stdout.

The distinction the whole file exists to hold: this wrapper READS a verdict and
never writes one. Every path out of `review_loop` that is not an APPROVED review
bound to the current head is a failure, including the one that looks most like
success — a clean review with nothing parseable in it.
"""
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

SHA1 = "1" * 40
SHA2 = "2" * 40
TOKEN = "b17c0ffee5a9d3e2f10ab4c96d7e8f0123456789"


def _load(tmp_path, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    for k in ("FLEET_REVIEW_CMD", "EUNOMIA_SESSION"):
        os.environ.pop(k, None)
    os.environ["EUNOMIA_SESSION"] = "review-test"
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader(
        "orchestrator_rev", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator_rev", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    # A unit test must not spend wall-clock in a poll interval. Tests that care
    # about pacing pass their OWN `sleep` to await_ci and are unaffected by
    # this; the rest reach it through review_loop, where the interval is not
    # theirs to pass. Left real, the settle poll added by the CI-settle change
    # charged this file 180 seconds across six tests that assert nothing about
    # timing.
    mod._sleep = lambda seconds: None
    return mod


class FakeForge:
    """Answers the real verdict_at/head_sha. Records every write."""

    def __init__(self, heads, reviews, ci="success"):
        self._heads = list(heads)          # popped per get_pull call
        self._reviews = reviews            # sha -> list of review dicts
        self.comments = []
        self.ci = ci

    def get_pull(self, repo, n):
        sha = self._heads[0] if len(self._heads) == 1 else self._heads.pop(0)
        return 200, {"head": {"sha": sha}}

    def list_reviews(self, repo, n, page=1, limit=50):
        if page > 1:
            return 200, []
        out = []
        for sha, revs in self._reviews.items():
            out.extend(dict(r, commit_id=sha) for r in revs)
        return 200, out

    def create_issue_comment(self, repo, n, body):
        self.comments.append(body)
        return 201, {}

    def get_commit_status(self, repo, sha):
        """Green unless a test says otherwise. The loop waits for CI before
        asking for a review, so every fixture needs an answer here — a missing
        one used to surface as an AttributeError deep in ci_status."""
        return 200, {"state": self.ci, "statuses": [
            {"context": "ci", "status": self.ci}]}


def _fleetforge():
    loader = importlib.machinery.SourceFileLoader(
        "ff_rev", str(BIN / "fleetforge.py"))
    spec = importlib.util.spec_from_loader("ff_rev", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _wire_forge(mod, forge):
    ff = _fleetforge()
    mod._forge = lambda: (ff, forge)
    return ff


def _bounds(tmp_path, text):
    p = tmp_path / "review-bounds.conf"
    p.write_text(text)
    return p


def _review(state, body=""):
    return {"id": 1, "state": state, "body": body, "stale": False,
            "dismissed": False}


# ------------------------------------------------------------------- bounds
def test_the_bound_is_read_per_repo(tmp_path):
    mod = _load(tmp_path)
    p = _bounds(tmp_path, "default = 5\noperator/noisy = 8\n")
    assert mod.review_bound("operator/noisy", p) == 8
    assert mod.review_bound("operator/other", p) == 5


def test_a_value_above_the_ceiling_is_clamped_not_obeyed(tmp_path):
    mod = _load(tmp_path)
    p = _bounds(tmp_path, f"default = {mod.REVIEW_BOUND_CEILING + 40}\n")
    assert mod.review_bound("operator/x", p) == mod.REVIEW_BOUND_CEILING


def test_zero_is_refused_rather_than_read_as_unlimited(tmp_path):
    """The conventional reading of a config integer is the misreading that
    costs money here, so it is a hard stop rather than a clamp."""
    mod = _load(tmp_path)
    with pytest.raises(mod.Stop) as e:
        mod.review_bound("operator/x", _bounds(tmp_path, "default = 0\n"))
    assert "unlimited" in str(e.value)


def test_an_unparseable_bound_stops_rather_than_defaulting(tmp_path):
    mod = _load(tmp_path)
    for bad in ("default 5\n", "default = many\n"):
        with pytest.raises(mod.Stop):
            mod.review_bound("operator/x", _bounds(tmp_path, bad))


def test_a_missing_bounds_file_stops(tmp_path):
    mod = _load(tmp_path)
    with pytest.raises(mod.Stop) as e:
        mod.review_bound("operator/x", tmp_path / "nope.conf")
    assert "unbounded" in str(e.value)


def test_the_shipped_conf_parses_and_is_within_the_ceiling(tmp_path):
    """The file that actually ships, not a fixture of it."""
    mod = _load(tmp_path)
    n = mod.review_bound("operator/eunomia")
    assert 1 <= n <= mod.REVIEW_BOUND_CEILING


# ----------------------------------------------------------------- contract
def test_the_contract_takes_exactly_four_facts_and_no_framing(tmp_path):
    mod = _load(tmp_path)
    out = mod.read_reviewer_contract("operator/x", 9, SHA1, "plans/0001-a.md")
    assert "operator/x" in out and "9" in out and SHA1 in out
    assert "plans/0001-a.md" in out
    assert "{repo}" not in out and "{sha}" not in out


def test_a_missing_contract_stops_rather_than_dispatching_bare(tmp_path):
    mod = _load(tmp_path)
    with pytest.raises(mod.Stop) as e:
        mod.read_reviewer_contract("r", 1, SHA1, "p", tmp_path / "gone.md")
    assert "no mandate" in str(e.value)


# ----------------------------------------------------------------- dispatch
def _cmd(tmp_path, script):
    p = tmp_path / "fake-review.py"
    p.write_text(script)
    return f"{sys.executable} {p}"


def test_dispatch_passes_post_and_no_followups(tmp_path):
    """--post or the verdict never lands; --no-followups or every round mints
    an issue into a backlog nobody is draining."""
    out = tmp_path / "argv.txt"
    mod = _load(tmp_path, FLEET_REVIEW_CMD=_cmd(tmp_path,
        f"import sys; open({str(out)!r},'w').write(' '.join(sys.argv[1:]))"))
    ok, why = mod.dispatch_review("operator/x", 7, SHA1, "plans/p.md")
    assert ok, why
    argv = out.read_text().split()
    assert argv[:2] == ["operator/x", "7"]
    assert "--post" in argv and "--no-followups" in argv


def test_a_failing_review_command_is_reported_not_swallowed(tmp_path):
    mod = _load(tmp_path, FLEET_REVIEW_CMD=_cmd(tmp_path,
        "import sys; sys.stderr.write('CI is still running\\n'); sys.exit(2)"))
    ok, why = mod.dispatch_review("operator/x", 7, SHA1, "plans/p.md")
    assert not ok and "CI is still running" in why


def test_the_dispatch_return_value_cannot_be_a_verdict(tmp_path):
    """A command that prints APPROVE and posts nothing must not be believed;
    the loop's only source of a verdict is the forge."""
    mod = _load(tmp_path, FLEET_REVIEW_CMD=_cmd(tmp_path,
        "print('VERDICT: APPROVE')"))
    ok, why = mod.dispatch_review("operator/x", 7, SHA1, "plans/p.md")
    assert ok and why == ""
    forge = FakeForge([SHA1], {})
    _wire_forge(mod, forge)
    assert mod.verdict_at("operator/x", 7, SHA1, forge, _fleetforge()) == {}


# ------------------------------------------------------------------ verdict
def test_a_verdict_is_bound_to_the_sha_it_reviewed(tmp_path):
    mod = _load(tmp_path)
    ff = _fleetforge()
    forge = FakeForge([SHA2], {SHA1: [_review("APPROVED")]})
    # approved at the OLD head; the current head is SHA2
    assert mod.verdict_at("operator/x", 7, SHA2, forge, ff) == {}
    got = mod.verdict_at("operator/x", 7, SHA1, forge, ff)
    assert got["state"] == "APPROVED"


def test_a_stale_or_dismissed_review_does_not_count(tmp_path):
    mod = _load(tmp_path)
    ff = _fleetforge()
    for flag in ("stale", "dismissed"):
        r = _review("APPROVED")
        r[flag] = True
        forge = FakeForge([SHA1], {SHA1: [r]})
        assert mod.verdict_at("operator/x", 7, SHA1, forge, ff) == {}


def test_an_unreadable_forge_is_none_not_a_rejection(tmp_path):
    """None means unreadable, and no caller may read it as 'not approved'."""
    mod = _load(tmp_path)
    ff = _fleetforge()

    class Broken(FakeForge):
        def list_reviews(self, repo, n, page=1, limit=50):
            return 500, None
    assert mod.verdict_at("operator/x", 7, SHA1, Broken([SHA1], {}), ff) is None


# ----------------------------------------------------------------- findings
def test_only_high_blocks(tmp_path):
    mod = _load(tmp_path)
    body = ("```findings\n"
            "HIGH: the lease is released before the PR exists\n"
            "MEDIUM: a docstring is stale\n"
            "LOW: a typo\n"
            "```")
    assert mod.blocking_findings(body) == [
        "the lease is released before the PR exists"]
    assert len(mod.parse_findings(body)) == 3


def test_the_bold_heading_form_is_parsed_when_there_is_no_fence(tmp_path):
    mod = _load(tmp_path)
    body = "**HIGH — the token reaches argv**\n\nsome prose\n"
    assert mod.blocking_findings(body) == ["the token reaches argv"]


def test_deferred_findings_round_trip_through_the_parser(tmp_path):
    """0014 parses this comment. A format that drifts between the writer and
    the reader silently drops follow-up work, so the writer's output is fed
    back through the reader here."""
    mod = _load(tmp_path)
    body = ("```findings\nHIGH: blocking one\nMEDIUM: deferred one\n"
            "LOW: deferred two\n```")
    rows = [{"round": 1, "sha": SHA1, "verdict": "APPROVED", "blocking": 0,
             "body": body}]
    comment = mod.deferred_comment(rows, body)
    assert mod.DEFERRED_MARKER in comment
    back = mod.parse_findings(comment)
    assert ("MEDIUM", "deferred one") in back
    assert ("LOW", "deferred two") in back
    assert not any(s == "HIGH" for s, _ in back)


# --------------------------------------------------------------- the comment
def test_a_structured_secret_never_reaches_the_thread(tmp_path):
    mod = _load(tmp_path)
    forge = FakeForge([SHA1], {})
    ff = _wire_forge(mod, forge)
    pat = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    assert mod.post_comment("operator/x", 7, f"leaked {pat}", forge, ff)
    assert pat not in forge.comments[0]
    assert "github-pat" in forge.comments[0]


def test_a_head_sha_survives_the_comment_redactor(tmp_path):
    """The narrow redactor is the RIGHT one here and this test says why.

    `redact_transcript()` adds `long-hex`, which matches a bare 40-hex Forgejo
    PAT — and every git SHA. A comment is not free text: the ledger's entire
    job is binding a verdict to a head SHA, so redacting one would destroy the
    field the binding is made of. That is the bakeoff's copied-regex failure
    (`[A-Fa-f0-9]{40,}` applied to `--sha`) which docs/feature-plans.md exists
    to prevent, and it would arrive here as a plausible security improvement.

    The cost is stated rather than hidden: a bare PAT pasted into a review body
    would reach the thread. That is the accepted trade, made in
    `_transcript_patterns()`, and the transcript path is where the paranoid set
    is applied instead."""
    mod = _load(tmp_path)
    forge = FakeForge([SHA1], {})
    ff = _wire_forge(mod, forge)
    rows = [{"round": 1, "sha": SHA1, "verdict": "APPROVED", "blocking": 0,
             "body": ""}]
    assert mod.post_comment("operator/x", 7, mod.ledger_comment(rows), forge, ff)
    assert SHA1[:8] in forge.comments[0]
    assert "redacted" not in forge.comments[0]


# ------------------------------------------------------------------ the loop
def _loop(mod, tmp_path, forge, bound="default = 3\n", impl=None):
    mod.review_bound = lambda repo, path=None: mod.__dict__["_b"]
    mod.__dict__["_b"] = int(bound.split("=")[1])
    _wire_forge(mod, forge)
    mod.dispatch_review = lambda *a, **k: (True, "")
    mod.run_implementer = impl or (lambda *a, **k: pytest.fail(
        "a fix round ran when none was expected"))
    return mod.review_loop("operator/x", "0001-a", "plans/p.md", "PLAN", 7,
                           "br", tmp_path, None, None, "lease-1")


def test_an_approval_at_head_ends_the_loop(tmp_path):
    mod = _load(tmp_path)
    forge = FakeForge([SHA1], {SHA1: [_review("APPROVED")]})
    ok, why, rows = _loop(mod, tmp_path, forge)
    assert ok and why == "" and len(rows) == 1
    assert rows[0]["sha"] == SHA1


def test_no_blockers_and_no_approval_does_not_end_the_loop(tmp_path):
    """'No blockers, therefore approved' invents an approval in a system whose
    central control is that it has none — and makes no API call doing it."""
    mod = _load(tmp_path)
    forge = FakeForge([SHA1], {SHA1: [_review("COMMENT", "looks fine to me")]})
    ok, why, rows = _loop(mod, tmp_path, forge)
    assert not ok
    assert "refusing to read an unparsed review as an approval" in why


def test_a_command_that_posts_nothing_is_a_failure(tmp_path):
    mod = _load(tmp_path)
    forge = FakeForge([SHA1], {})
    ok, why, rows = _loop(mod, tmp_path, forge)
    assert not ok and "the verdict did not land" in why


def test_exhaustion_reports_the_bound_and_spends_no_more(tmp_path):
    mod = _load(tmp_path)
    body = "```findings\nHIGH: still broken\n```"
    heads = [SHA1, SHA2, SHA1, SHA2, SHA1, SHA2]
    forge = FakeForge(heads, {SHA1: [_review("REQUEST_CHANGES", body)],
                              SHA2: [_review("REQUEST_CHANGES", body)]})
    seen = []

    class S:
        timed_out = False
        exit_code = 0
        transcript = None

    def impl(*a, **k):
        seen.append(1)
        return S()
    ok, why, rows = _loop(mod, tmp_path, forge, "default = 3\n", impl)
    assert not ok and "bound 3 exhausted" in why
    assert len(rows) == 3, rows
    assert len(seen) == 2, "one fix round per gap between reviews, no more"


def test_a_fix_round_that_pushes_nothing_stops(tmp_path):
    """Otherwise the loop re-reviews an unchanged tree until the bound, paying
    for a review each time and blaming convergence for a session that pushed
    nothing."""
    mod = _load(tmp_path)
    body = "```findings\nHIGH: still broken\n```"
    forge = FakeForge([SHA1], {SHA1: [_review("REQUEST_CHANGES", body)]})

    class S:
        timed_out = False
        exit_code = 0
        transcript = None
    ok, why, rows = _loop(mod, tmp_path, forge, "default = 3\n",
                          lambda *a, **k: S())
    assert not ok and "nothing was pushed" in why


def test_the_fix_prompt_carries_the_reviewer_words_not_a_summary(tmp_path):
    mod = _load(tmp_path)
    (BIN / "implementer-preamble.md").read_text()   # must exist
    p = mod.build_fix_prompt("operator/x", "plans/p.md", "PLAN TEXT", tmp_path,
                             "br", 7, ["the lease is released too early"], 2, 3)
    assert "the lease is released too early" in p
    assert "PLAN TEXT" in p and "round:       2 of 3" in p


def test_the_ledger_records_every_round(tmp_path):
    mod = _load(tmp_path)
    rows = [{"round": 1, "sha": SHA1, "verdict": "REQUEST_CHANGES",
             "blocking": 2, "body": ""},
            {"round": 2, "sha": SHA2, "verdict": "APPROVED", "blocking": 0,
             "body": ""}]
    out = mod.ledger_comment(rows)
    assert mod.LEDGER_MARKER in out
    assert SHA1[:8] in out and SHA2[:8] in out
    assert "REQUEST_CHANGES" in out and "APPROVED" in out


# ------------------------------------------------- CI is a wait, not a failure
class CIForge(FakeForge):
    """Serves a sequence of CI states, one per poll."""

    def __init__(self, states, heads=(SHA1,), reviews=None, statuses=None):
        super().__init__(heads, reviews or {})
        self._states = list(states)
        self._statuses = statuses
        self.polls = 0

    def get_commit_status(self, repo, sha):
        self.polls += 1
        s = self._states[0] if len(self._states) == 1 else self._states.pop(0)
        if s == "unreadable":
            return 500, None
        body = {"state": s}
        if self._statuses is None:
            body["statuses"] = [{"context": "ci", "status": s}]
        elif self._statuses and isinstance(self._statuses[0], list):
            i = min(self.polls - 1, len(self._statuses) - 1)
            body["statuses"] = self._statuses[i]
        else:
            body["statuses"] = self._statuses
        return 200, body


def test_a_running_ci_is_waited_out_not_treated_as_a_failure(tmp_path):
    """2026-09-09: the loop pushed a fix and asked for a review three seconds
    later, `dispatch-review` refused because CI had not started, and the loop
    treated that refusal as terminal — abandoning #245 one CI cycle short."""
    mod = _load(tmp_path)
    slept = []
    forge = CIForge(["pending", "pending", "success"])
    state, failed = mod.await_ci("r", SHA1, forge, _fleetforge(),
                                 sleep=slept.append, poll=30)
    assert (state, failed) == ("success", [])
    # 4 polls, not 3: the resolved set must hold still for one extra look before
    # it counts (see await_ci). eunomia#252 approved a red PR because a green
    # observed once was taken as final.
    assert forge.polls == 4 and slept == [30, 30, 30]


def test_waiting_for_ci_is_bounded(tmp_path):
    mod = _load(tmp_path)
    slept = []
    forge = CIForge(["pending"])
    state, _ = mod.await_ci("r", SHA1, forge, _fleetforge(), sleep=slept.append,
                            bound=120, poll=30)
    assert state is None, "an unresolved CI must not wait forever"
    assert sum(slept) <= 120


def test_an_unreadable_status_is_retried_then_refused(tmp_path):
    """None is never success. But one bad response is not fatal either —
    read_pulls in the same file retries a flaky forge, and ending the whole loop
    on a single unreadable status was an inconsistency rather than a policy
    (review 2441). A generous bound is used deliberately: with `bound=0` this
    test would pass even if unreadable were treated as pending."""
    mod = _load(tmp_path)
    slept = []
    forge = CIForge(["unreadable"])
    state, _ = mod.await_ci("r", SHA1, forge, _fleetforge(), sleep=slept.append,
                            bound=3600, poll=30)
    assert state is None
    assert forge.polls == mod.CI_READ_ATTEMPTS + 1, forge.polls
    assert slept, "gave up without retrying"


def test_an_unreadable_read_that_recovers_does_not_end_the_loop(tmp_path):
    mod = _load(tmp_path)
    forge = CIForge(["unreadable", "unreadable", "success"])
    state, _ = mod.await_ci("r", SHA1, forge, _fleetforge(),
                            sleep=lambda s: None, poll=30)
    assert state == "success"


def test_a_repo_with_no_ci_configured_resolves_after_confirming(tmp_path):
    """Forgejo returns state "" with an empty list for a commit with no
    statuses — MEASURED on operator/ladon. The earlier version read that empty
    string as unreadable, which would have failed round one on every repo
    without CI (review 2441 HIGH), and its fixture served `pending` with an
    empty list, a shape the forge never produces.

    Green by absence, but NOT on sight: a commit pushed a second ago also has no
    statuses yet. Confirming one poll apart separates "no CI here" from "CI has
    not started"."""
    mod = _load(tmp_path)
    slept = []
    forge = CIForge(["", ""], statuses=[])
    state, _ = mod.await_ci("r", SHA1, forge, _fleetforge(), sleep=slept.append,
                            poll=30)
    assert state == "success"
    assert forge.polls == 2 and slept == [30], "declared green without confirming"


def test_a_just_pushed_commit_is_not_mistaken_for_a_repo_without_ci(tmp_path):
    """The MEDIUM this confirmation exists for: empty on the first poll, then
    Forgejo creates the pending row, then it goes green. Must NOT short-circuit
    to success on that first empty look."""
    mod = _load(tmp_path)
    forge = CIForge(["", "pending", "success"],
                    statuses=[[], [{"context": "ci", "status": "pending"}],
                              [{"context": "ci", "status": "success"}]])
    state, _ = mod.await_ci("r", SHA1, forge, _fleetforge(),
                            sleep=lambda s: None, poll=30)
    # 4, not 3 — one confirming look after the green, same reason as above.
    assert state == "success" and forge.polls == 4


def test_a_red_ci_becomes_a_blocking_finding_and_drives_a_fix_round(tmp_path):
    """Better than spending a review on a commit that does not build, and
    better than stopping: the session is told what broke."""
    mod = _load(tmp_path)
    forge = CIForge(["failure"], heads=[SHA1],
                    statuses=[{"context": "tests", "status": "failure"},
                              {"context": "lint", "status": "success"}])
    _wire_forge(mod, forge)
    mod.review_bound = lambda repo, path=None: 2
    mod.dispatch_review = lambda *a, **k: pytest.fail(
        "must not spend a review on a red build")
    seen = []

    class S:
        timed_out = False
        exit_code = 0
        transcript = None
    mod.run_implementer = lambda wt, prompt, *a, **k: (seen.append(prompt), S())[1]
    ok, why, rows = mod.review_loop("operator/x", "0001-a", "plans/p.md", "PLAN",
                                    7, "br", tmp_path, None, None, "lease-1")
    assert not ok
    assert rows and rows[0]["verdict"] == "ci-failure"
    assert seen, "a red build did not drive a fix round"
    assert "CI is failure" in seen[0] and "tests" in seen[0]
    assert "lint" not in seen[0].split("--- END BLOCKING")[0], \
        "a passing context must not be reported as failing"


def test_a_red_build_is_briefed_as_a_build_failure_not_as_a_review(tmp_path):
    """FIX_TEMPLATE opens with "your pull request was reviewed and the reviewer
    raised findings". That is false for a red build: no review was requested,
    because the loop does not spend one on a tree that fails to build. Telling
    a session it has reviewer findings when it has a stack trace sends it
    hunting a review that does not exist (review 2441)."""
    mod = _load(tmp_path)
    forge = CIForge(["failure"], statuses=[{"context": "tests", "status": "failure"}])
    _wire_forge(mod, forge)
    mod.review_bound = lambda repo, path=None: 2
    mod.dispatch_review = lambda *a, **k: pytest.fail("no review on a red build")
    seen = []

    class S:
        timed_out = False
        exit_code = 0
        transcript = None
    mod.run_implementer = lambda wt, prompt, *a, **k: (seen.append(prompt), S())[1]
    mod.review_loop("operator/x", "0001-a", "plans/p.md", "PLAN", 7, "br",
                    tmp_path, None, None, "lease-1")
    assert seen, "no fix round ran"
    assert "The build is failing" in seen[0]
    assert "No review has been requested" in seen[0]
    assert "the reviewer raised findings" not in seen[0]


def test_warning_counts_as_resolved(tmp_path):
    """Forgejo's combined state can be `warning`. Left out of CI_RESOLVED it
    was waited out for the whole bound and then reported unresolved."""
    mod = _load(tmp_path)
    assert "warning" in mod.CI_RESOLVED
    slept = []
    state, _ = mod.await_ci("r", SHA1, CIForge(["warning"]), _fleetforge(),
                            sleep=slept.append, poll=30)
    # One poll, not zero: `warning` is resolved, and resolved still has to
    # settle. The point this test guards is that warning RESOLVES rather than
    # being waited out for the whole bound, and one poll is not that.
    assert state == "warning" and slept == [30]


# --- a resolved status set is not a settled one -----------------------------
# 2026-09-10, eunomia#252: `Plans / lint` reported FAILURE seconds after the four
# `CI /` contexts had gone green. A review was dispatched against that momentary
# green and revbot APPROVED a pull request whose CI was red.

def _ci_seq(observations):
    """A ci_status stub that returns each observation once, then repeats the last."""
    seq = list(observations)

    def _stub(repo, sha, forge=None, mod=None):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return _stub


def test_a_late_red_is_caught_before_the_review(tmp_path, monkeypatch):
    """The exact #252 sequence: four green, then a fifth arrives red."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod, "ci_status", _ci_seq([
        ("success", [], 4),                      # the momentary green
        ("failure", ["Plans / lint"], 5),        # lint lands, red
    ]))
    state, failed = mod.await_ci("operator/eunomia", "d7e465d8", forge=object(),
                                 mod=object(), sleep=lambda s: None)
    assert state == "failure", "dispatched a review against a momentary green"
    assert failed == ["Plans / lint"]


def test_a_genuinely_green_head_still_passes(tmp_path, monkeypatch):
    """The cost of settling must be one poll, not a refusal."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod, "ci_status", _ci_seq([("success", [], 4)]))
    slept = []
    state, failed = mod.await_ci("operator/eunomia", "abc", forge=object(),
                                 mod=object(), sleep=lambda s: slept.append(s))
    assert (state, failed) == ("success", [])
    assert len(slept) == 1, f"settling should cost exactly one poll, slept {slept}"


def test_a_growing_status_set_keeps_waiting(tmp_path, monkeypatch):
    """Green at 2 checks, then 4, then 5 — only the stable observation counts."""
    mod = _load(tmp_path / "fleet")
    monkeypatch.setattr(mod, "ci_status", _ci_seq([
        ("success", [], 2), ("success", [], 4), ("success", [], 5),
        ("success", [], 5),
    ]))
    slept = []
    state, _ = mod.await_ci("operator/eunomia", "abc", forge=object(),
                            mod=object(), sleep=lambda s: slept.append(s))
    assert state == "success"
    assert len(slept) == 3, f"returned before the set stopped growing: {slept}"


def test_settling_cannot_outlast_the_wait_bound(tmp_path, monkeypatch):
    """A forge that adds a status every poll forever must not hold a plan open
    past its budget — the bound still wins."""
    mod = _load(tmp_path / "fleet")
    n = {"i": 3}

    def forever(repo, sha, forge=None, mod=None):
        n["i"] += 1
        return ("success", [], n["i"])

    monkeypatch.setattr(mod, "ci_status", forever)
    state, _ = mod.await_ci("operator/eunomia", "abc", forge=object(),
                            mod=object(), sleep=lambda s: None, bound=90, poll=30)
    assert state == "success", "the bound must resolve, not hang"
