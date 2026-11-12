"""Plan 0191: a behavior-changing pull request gets a merge checklist showing
whether its spec and its regression evidence agree, scoped to its exact head.
A plan whose lease reaches no `spec_watch` rule is ineligible and gets
nothing — "unchanged behavior for ineligible PRs"."""
import base64
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "operator/x"
PLAN = "0009-x"
IMPL_PR = 7
IMPL_HEAD = "c" * 40
LANE_HEAD = "d" * 40

GOVERNANCE = """spec_watch:
  - name: api-routes
    kind: api
    code:
      - "api/speakhush/**"
    specs:
      - "docs/declared-behaviour.md"
"""

PLAN_TEMPLATE = """---
id: {id}
status: ready
repo: acme/widgets
zone: public
tier: 1
paths: {paths}
---

## 1. Goal
g

## 2. Deliverables
- d

## 3. Boundaries
- b

## 4. Definition of done
{dod}

## 5. Handoff
h

## 6. Resources
- lease: b
"""


def _module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    fd = tmp_path / "fleet"
    fd.mkdir()
    for k in ("EUNOMIA_FLEET_DIR", "FLEET_DB", "FLEET_WORK_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(fd))
    monkeypatch.setenv("FLEET_DB", str(fd / "fleet.db"))
    monkeypatch.setenv("FLEET_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("FLEET_OPERATOR_UID", "1")
    mod = _module("orch_for_spec_gate", BIN / "orchestrator")
    mod.notify = lambda t, b: None
    return mod


def _repo(wt, paths, dod="- `docs/declared-behaviour.md`", governance=GOVERNANCE, plan_id=PLAN):
    (wt / "plans").mkdir(parents=True, exist_ok=True)
    if governance is not None:
        (wt / "governance.yaml").write_text(governance)
    text = PLAN_TEMPLATE.format(id=plan_id, paths=json.dumps(list(paths)), dod=dod)
    (wt / "plans" / f"{plan_id}.md").write_text(text)
    return wt


REACHING_PATHS = ["api/speakhush/leads.py", "docs/declared-behaviour.md"]
PLAN_PATH = f"plans/{PLAN}.md"


# ---------------------------------------------------------------- eligibility
def test_a_plan_whose_lease_reaches_no_rule_is_ineligible(env, tmp_path):
    mod = env
    wt = _repo(tmp_path, ["bin/something-else.py"])
    reached, fm, err = mod.spec_watch_reached(wt, PLAN_PATH)
    assert reached == [] and err is None and fm is not None


def test_no_governance_yaml_is_ineligible(env, tmp_path):
    mod = env
    wt = _repo(tmp_path, REACHING_PATHS, governance=None)
    reached, fm, err = mod.spec_watch_reached(wt, PLAN_PATH)
    assert reached == [] and err is None


def test_a_reached_rule_is_found_by_the_plans_own_lease(env, tmp_path):
    mod = env
    wt = _repo(tmp_path, REACHING_PATHS)
    reached, fm, err = mod.spec_watch_reached(wt, PLAN_PATH)
    assert err is None
    assert [r["name"] for r in reached] == ["api-routes"]


# ------------------------------------------------------------------- missing
def test_a_reached_rule_whose_spec_the_pr_never_touched_is_missing(env, tmp_path):
    mod = env
    wt = _repo(tmp_path, REACHING_PATHS)
    reached, _fm, _err = mod.spec_watch_reached(wt, PLAN_PATH)
    files = [{"filename": "api/speakhush/leads.py"}]
    assert mod.spec_rules_missing_update(files, reached) == ["api-routes"]


def test_a_reached_rule_whose_spec_the_pr_did_touch_is_not_missing(env, tmp_path):
    mod = env
    wt = _repo(tmp_path, REACHING_PATHS)
    reached, _fm, _err = mod.spec_watch_reached(wt, PLAN_PATH)
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    assert mod.spec_rules_missing_update(files, reached) == []


# --------------------------------------------------------------------- waiver
def test_a_pure_two_line_comment_is_a_waiver(env):
    mod = env
    body = "Spec-gate-waive: known flaky, already filed as #12\nHead: " + "c" * 12
    assert mod.is_spec_gate_waiver(body)


def test_extra_text_around_the_waiver_lines_is_not_a_pure_waiver(env):
    mod = env
    body = ("please also fix the docstring\n"
            "Spec-gate-waive: known flaky\nHead: " + "c" * 12)
    assert not mod.is_spec_gate_waiver(body)


def test_a_waiver_is_scoped_to_the_exact_head(env):
    mod = env
    comments = [{"id": 1, "user": {"id": 1, "login": "operator"},
                 "body": "Spec-gate-waive: known flaky\nHead: " + "a" * 12}]
    assert mod.find_spec_gate_waiver(comments, "1", "b" * 40) is None
    found = mod.find_spec_gate_waiver(comments, "1", "a" * 40)
    assert found == ("operator", "known flaky")


def test_a_waiver_from_a_non_owner_does_not_count(env):
    mod = env
    comments = [{"id": 1, "user": {"id": 99, "login": "someone"},
                 "body": "Spec-gate-waive: nope\nHead: " + "a" * 12}]
    assert mod.find_spec_gate_waiver(comments, "1", "a" * 40) is None


def test_the_newest_waiver_wins(env):
    mod = env
    head = "a" * 40
    comments = [
        {"id": 1, "user": {"id": 1, "login": "operator"},
         "body": f"Spec-gate-waive: first reason\nHead: {head[:12]}"},
        {"id": 2, "user": {"id": 1, "login": "operator"},
         "body": f"Spec-gate-waive: second reason\nHead: {head[:12]}"},
    ]
    assert mod.find_spec_gate_waiver(comments, "1", head) == ("operator", "second reason")


# ----------------------------------------------------------------- checklist
class Forge:
    def __init__(self, pulls=None, comments=None, contents=None):
        self.pulls = pulls or {}
        self.comments = list(comments or [])
        self.contents = contents or {}
        self.posted = []
        self._files = []

    def get_pull(self, repo, n):
        p = self.pulls.get(n)
        return (200, p) if p else (404, None)

    def list_pull_files(self, repo, n, page_cap=20, limit=50):
        return 200, self._files

    def list_issue_comments(self, repo, n, since=None, page=1, limit=50):
        return (200, self.comments) if page == 1 else (200, [])

    def create_issue_comment(self, repo, n, body):
        self.posted.append((n, body))
        return 201, {"id": 1000 + len(self.posted)}

    def get_contents(self, repo, path, ref=None):
        raw = self.contents.get(path)
        if raw is None:
            return 404, None
        return 200, {"content": base64.b64encode(raw).decode("ascii")}


def _ff(mod):
    return mod._load_sibling("fleetforge.py", "_ff_spec_gate")


def _impl_pr(sha=IMPL_HEAD):
    return {"number": IMPL_PR, "body": "report\n\nPlan: 0009-x\n",
            "user": {"login": "implbot", "id": 5},
            "head": {"ref": "feat/0009-x", "sha": sha}, "base": {"ref": "main"}}


def _lane_pr(n=12, sha=LANE_HEAD):
    return {"number": n, "body": f"Tests-for: {PLAN}\n", "user": {"login": "implbot", "id": 5},
            "head": {"ref": f"agent/tests/{PLAN}/r", "sha": sha}, "base": {"ref": "main"}}


def _write_findings(mod, outcomes=("pass",), impl_head=IMPL_HEAD, lane_head=LANE_HEAD,
                     run=None, not_run=None, all_passed=None):
    """Mirrors `bin/fleet-meet`'s own `findings.json` shape (`run()`, lines
    ~1298-1316): a lane carries both a `run` outcome and its `tests`, and
    the document's own `all_passed` is the authoritative verdict — not
    something `spec_gate_checklist` may re-derive from `tests` alone."""
    path = mod.findings_path_for(PLAN, impl_head)
    path.parent.mkdir(parents=True, exist_ok=True)
    run = {"outcome": "ran"} if run is None else run
    not_run = [] if not_run is None else not_run
    tests = [{"id": f"t{i}", "outcome": o, "source_path": "tests/x.py"}
              for i, o in enumerate(outcomes)]
    if all_passed is None:
        all_passed = bool(tests and not not_run and run.get("outcome") == "ran"
                           and all(t["outcome"] == "pass" for t in tests))
    path.write_text(json.dumps({
        "plan": PLAN, "repo": REPO,
        "impl": {"pr": IMPL_PR, "head_sha": impl_head},
        "lanes": [{"pr": 12, "head_sha": lane_head, "runner": "sub-opus",
                   "run": run, "tests": tests}],
        "not_run": not_run,
        "all_passed": all_passed,
    }))
    return path


def _reached(mod, tmp_path):
    wt = _repo(tmp_path, REACHING_PATHS)
    reached, _fm, _err = mod.spec_watch_reached(wt, PLAN_PATH)
    return reached


def test_checklist_is_missing_when_the_spec_was_not_touched(env, tmp_path):
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    forge._files = [{"filename": "api/speakhush/leads.py"}]
    state, detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, forge._files, reached,
                                            [], "1", forge)
    assert state == "missing" and detail["rules"] == ["api-routes"]


def test_checklist_is_stale_with_no_findings_at_all(env, tmp_path):
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    state, _detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "stale"


def test_checklist_is_stale_when_findings_are_for_an_older_head(env, tmp_path):
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(sha=IMPL_HEAD), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, impl_head="e" * 40)   # a different, older head
    state, _detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "stale"


def test_checklist_is_passing_when_everything_at_this_head_passed(env, tmp_path):
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, outcomes=("pass", "pass"))
    state, _detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "passing"


def test_checklist_is_not_passing_when_every_lane_was_refused(env, tmp_path):
    """H1 (review of #1476): a lane refused before it runs adds no test, so
    scanning tests alone finds nothing to disagree with. `all_passed` is the
    one check that catches it."""
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, outcomes=(), run={"outcome": "refused", "reason": "no test file"})
    state, detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "failing"
    assert {"lane": "sub-opus", "test": None, "outcome": "refused"} in detail["nonpass"]


def test_checklist_is_not_passing_when_a_lane_timed_out(env, tmp_path):
    """A lane that timed out before any test reported is the same shape as a
    refused lane: no non-passing test entry, but `all_passed` is false."""
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, outcomes=(), run={"outcome": "timeout"})
    state, detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "failing"
    assert {"lane": "sub-opus", "test": None, "outcome": "timeout"} in detail["nonpass"]


def test_checklist_is_not_passing_when_a_dispatched_lane_was_never_read(env, tmp_path):
    """A lane fleet-meet could not even seat (no accepted human read yet) is
    recorded in `not_run`, never in `lanes` — the per-test scan never sees it
    at all."""
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, outcomes=("pass",),
                     not_run=[{"pr": 13, "head_sha": "a" * 40, "reason": "no human read yet"}])
    state, detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "failing"
    assert {"lane": "pr#13", "test": None, "outcome": "not_run"} in detail["nonpass"]


def test_checklist_is_not_passing_on_an_empty_suite(env, tmp_path):
    """plan 0191 Boundaries: an empty suite must never read as passing."""
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, outcomes=())
    state, detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "failing"
    assert any(n["outcome"] == "empty-suite" for n in detail["nonpass"])


def test_checklist_is_failing_on_an_unresolved_nonpass_test(env, tmp_path):
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, outcomes=("pass", "fail"))
    state, detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "failing" and len(detail["nonpass"]) == 1


def test_a_classification_alone_does_not_clear_a_failing_test(env, tmp_path):
    """Triage having run (and even classifying the failure) does not move the
    state off `failing` by itself (Boundaries): only a repaired re-met head or
    an owner waiver does."""
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    findings_path = _write_findings(mod, outcomes=("fail",))
    (findings_path.parent / "triage.json").write_text(json.dumps(
        {"runner": "sub-opus", "document": {"items": [{"class": "implementation-defect"}]}}))
    state, _detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, [], "1", forge)
    assert state == "failing"


def test_checklist_is_waived_by_a_matching_owner_comment(env, tmp_path):
    mod = env
    reached = _reached(mod, tmp_path)
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, outcomes=("fail",))
    comments = [{"id": 1, "user": {"id": 1, "login": "operator"},
                 "body": f"Spec-gate-waive: known flaky\nHead: {IMPL_HEAD[:12]}"}]
    state, detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, comments,
                                            "1", forge)
    assert state == "waived" and detail["author"] == "operator"


def test_a_waiver_at_a_stale_head_does_not_waive_the_new_head(env, tmp_path):
    """Merge ordering / head-advance: the owner waived an earlier head; a new
    push must be met and triaged again, not waved through by the old note."""
    mod = env
    reached = _reached(mod, tmp_path)
    old_head = "f" * 40
    forge = Forge(pulls={IMPL_PR: _impl_pr(sha=IMPL_HEAD), 12: _lane_pr()})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    _write_findings(mod, outcomes=("fail",))     # at IMPL_HEAD
    comments = [{"id": 1, "user": {"id": 1, "login": "operator"},
                 "body": f"Spec-gate-waive: known flaky\nHead: {old_head[:12]}"}]
    state, _detail = mod.spec_gate_checklist(REPO, PLAN, IMPL_HEAD, files, reached, comments,
                                            "1", forge)
    assert state == "failing"


# ------------------------------------------------------------------ dedupe
def test_last_posted_state_reads_the_newest_matching_comment(env):
    mod = env
    head = "c" * 40
    comments = [
        {"id": 1, "body": mod.spec_gate_marker(PLAN, head, "stale") + "\n\nfirst\n"},
        {"id": 2, "body": mod.spec_gate_marker(PLAN, head, "passing") + "\n\nsecond\n"},
    ]
    assert mod.spec_gate_last_posted_state(comments, PLAN, head) == "passing"
    # comment order in the list must not matter — only the id does.
    assert mod.spec_gate_last_posted_state(list(reversed(comments)), PLAN, head) == "passing"


def test_last_posted_state_is_none_for_a_head_with_no_gate_comment(env):
    mod = env
    assert mod.spec_gate_last_posted_state([], PLAN, "c" * 40) is None
    other_head = "b" * 40
    comments = [{"id": 1, "body": mod.spec_gate_marker(PLAN, other_head, "passing")}]
    assert mod.spec_gate_last_posted_state(comments, PLAN, "c" * 40) is None


def test_a_recurring_state_at_the_same_head_is_reposted(env, tmp_path):
    """H2 (review of #1476): stale -> failing -> stale at one head must post
    all three times. Dedupe keyed on "this exact (plan, head, state) was
    ever posted" would suppress the third post and leave `failing` as the
    thread's newest comment even though the true state had gone back to
    `stale`."""
    mod = env
    wt = _repo(tmp_path, REACHING_PATHS)
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    forge = Forge(pulls={IMPL_PR: _impl_pr(), 12: _lane_pr()})
    forge._files = files

    def _sync_thread():
        forge.comments = [{"id": 1000 + i, "user": {"login": "implbot", "id": 5}, "body": b}
                           for i, (_, b) in enumerate(forge.posted)]

    mod.spec_gate_step(REPO, PLAN, PLAN_PATH, IMPL_PR, wt, forge, _ff(mod))
    assert len(forge.posted) == 1 and "State: **stale**" in forge.posted[-1][1]
    _sync_thread()

    _write_findings(mod, outcomes=("fail",))
    mod.spec_gate_step(REPO, PLAN, PLAN_PATH, IMPL_PR, wt, forge, _ff(mod))
    assert len(forge.posted) == 2 and "State: **failing**" in forge.posted[-1][1]
    _sync_thread()

    mod.findings_path_for(PLAN, IMPL_HEAD).unlink()
    mod.spec_gate_step(REPO, PLAN, PLAN_PATH, IMPL_PR, wt, forge, _ff(mod))
    assert len(forge.posted) == 3 and "State: **stale**" in forge.posted[-1][1]


# --------------------------------------------------------------------- digests
def test_spec_digests_cover_every_touched_spec_file(env, tmp_path):
    mod = env
    reached = _reached(mod, tmp_path)
    raw = b"the declared-behaviour text"
    forge = Forge(contents={"docs/declared-behaviour.md": raw})
    files = [{"filename": "api/speakhush/leads.py"}, {"filename": "docs/declared-behaviour.md"}]
    digests = mod.spec_doc_digests(forge, REPO, IMPL_HEAD, files, reached)
    assert digests == {"docs/declared-behaviour.md": hashlib.sha256(raw).hexdigest()}


# ------------------------------------------------------------------ the step
def test_an_ineligible_plan_posts_no_comment(env, tmp_path):
    mod = env
    wt = _repo(tmp_path, ["bin/unrelated.py"])
    forge = Forge(pulls={IMPL_PR: _impl_pr()})
    forge._files = []
    mod.spec_gate_step(REPO, PLAN, PLAN_PATH, IMPL_PR, wt, forge, _ff(mod))
    assert forge.posted == []


def test_an_eligible_plan_with_a_missing_spec_posts_once(env, tmp_path):
    mod = env
    wt = _repo(tmp_path, REACHING_PATHS)
    forge = Forge(pulls={IMPL_PR: _impl_pr()})
    forge._files = [{"filename": "api/speakhush/leads.py"}]
    mod.spec_gate_step(REPO, PLAN, PLAN_PATH, IMPL_PR, wt, forge, _ff(mod))
    assert len(forge.posted) == 1
    n, body = forge.posted[0]
    assert n == IMPL_PR and "State: **missing**" in body
    assert body.startswith("<!-- eunomia:spec-regression-gate ")

    # same state, same head: not reposted, because the thread now carries
    # the marker and `list_issue_comments` reflects the post.
    forge.comments = [{"id": 1, "user": {"login": "implbot", "id": 5}, "body": body}]
    mod.spec_gate_step(REPO, PLAN, PLAN_PATH, IMPL_PR, wt, forge, _ff(mod))
    assert len(forge.posted) == 1


def test_the_comment_never_carries_an_ack_or_plan_line(env, tmp_path):
    """Assert-before-post, mirrored from fleet-triage's own rule: the
    checklist reports and must never be readable as an instruction."""
    mod = env
    marker = mod.spec_gate_marker(PLAN, IMPL_HEAD, "missing")
    with pytest.raises(mod.Stop):
        mod.assert_spec_gate_body_safe(marker + "\n\nAck: #3\n", marker)
    with pytest.raises(mod.Stop):
        mod.assert_spec_gate_body_safe(marker + "\n\nPlan: 0009-x\n", marker)
    mod.assert_spec_gate_body_safe(marker + "\n\nfine.\n", marker)
