"""bin/orchestrator: a style pull request gets its screenshots before the review
round (plan 0151 D2).

No network, no simulator, no model. The forge is a fake, the three style tools are
seams the test replaces (one test runs the real argv against stub executables), and
`dispatch_review` records the order of events. What is held here: evidence is made
before the review is asked for and never after a failure, an approved pull request
is never pushed to, and a code review changes nothing from before.
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

BASE = "b" * 40
CODE = "1" * 40
EVID = "2" * 40
ACCOUNTS = ["revbot", "rev2bot"]


def _load(tmp_path):
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    os.environ["EUNOMIA_SESSION"] = "style-review-test"
    loader = importlib.machinery.SourceFileLoader("orchestrator_style", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator_style", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod._sleep = lambda seconds: None
    return mod


def _fleetforge():
    loader = importlib.machinery.SourceFileLoader("ff_style", str(BIN / "fleetforge.py"))
    spec = importlib.util.spec_from_loader("ff_style", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class Forge:
    def __init__(self, reviews=None):
        self.head = CODE
        self.reviews = reviews or {}           # sha -> [review]

    def get_pull(self, repo, n):
        return 200, {"head": {"sha": self.head}, "base": {"sha": BASE, "ref": "main"},
                     "merge_base": BASE}

    def list_pull_files(self, repo, n, page_cap=20, limit=50):
        return 200, [{"filename": "app/Theme/Colors.swift", "status": "modified"}]

    def list_reviews(self, repo, n, page=1, limit=50):
        if page > 1:
            return 200, []
        out = []
        for sha, revs in self.reviews.items():
            out.extend(dict(r, commit_id=sha) for r in revs)
        return 200, out

    def create_issue_comment(self, repo, n, body):
        return 201, {}

    def get_commit_status(self, repo, sha):
        return 200, {"state": "success", "statuses": [{"context": "ci", "status": "success"}]}


def _reviews(state):
    return [{"id": i + 1, "state": state, "body": "", "stale": False, "dismissed": False,
             "user": {"login": who}} for i, who in enumerate(ACCOUNTS)]


class Wired:
    """The module with its three style tools and the dispatcher replaced."""

    def __init__(self, tmp_path, forge, position="style_reviewer", valid=False,
                 produce=(True, ""), plan_text="---\nid: 0007-reskin\nreview: style\n---\n"):
        self.plan_text = plan_text
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        self.mod = mod = _load(tmp_path)
        self.forge, self.events, self.positions = forge, [], []
        self.tmp = tmp_path
        ff = _fleetforge()
        mod._forge = lambda: (ff, forge)
        mod.review_bound = lambda repo, path=None: 2

        def route(repo, pr, plan_text, head, base, git_dir, files):
            self.events.append(("route", head))
            return {"position": position, "evidence_id": "0007-reskin",
                    "screenshot": {"timeout_minutes": 3}}

        def ok(repo, pr, ident, head, base, git_dir):
            self.events.append(("verify", head))
            return valid

        def make(repo, pr, plan_text, wt, base, head, tm):
            self.events.append(("produce", head, tm))
            if produce[0]:
                forge.head = EVID
                forge.reviews.setdefault(EVID, forge.reviews.get(CODE, []))
            return produce

        def dispatch(repo, pr, sha, plan_path, log=None, timeout=2400):
            self.events.append(("dispatch", sha))
            return True, ""

        def expected(repo, pos):
            self.positions.append(pos)
            return ACCOUNTS

        mod.style_route, mod.style_evidence_ok, mod.style_produce = route, ok, make
        mod.dispatch_review, mod.expected_reviewers = dispatch, expected
        mod.repo_review_bar = lambda repo: "high"
        mod.run_implementer = lambda *a, **k: pytest.fail("a fix round ran")

    def run(self):
        return self.mod.review_loop("operator/x", "0007-reskin", "plans/0007-reskin.md",
                                    self.plan_text, 7, "br", self.tmp, None, None, "lease-1")

    def names(self):
        return [e[0] for e in self.events]


def _approving():
    return {EVID: _reviews("APPROVED"), CODE: []}


def test_a_style_route_with_no_valid_evidence_produces_it_before_dispatch(tmp_path):
    w = Wired(tmp_path, Forge(_approving()))
    ok, why, rows = w.run()
    assert ok, why
    assert w.names() == ["route", "verify", "produce", "dispatch"]
    assert ("produce", CODE, 3) in w.events
    assert ("dispatch", EVID) in w.events          # the round reviews the evidence head
    assert rows[0]["sha"] == EVID
    assert w.positions == ["style_reviewer"]


def test_valid_evidence_at_head_is_not_reproduced(tmp_path):
    forge = Forge({CODE: _reviews("APPROVED")})
    w = Wired(tmp_path, forge, valid=True)
    ok, why, _ = w.run()
    assert ok, why
    assert w.names() == ["route", "verify", "dispatch"]
    assert w.positions == ["style_reviewer"]


def test_a_produce_failure_stops_the_run_without_dispatching(tmp_path):
    w = Wired(tmp_path, Forge(), produce=(False, "step 4: the head run exited 3"))
    with pytest.raises(w.mod.Stop) as e:
        w.run()
    assert "step 4" in e.value.reason and "#7" in e.value.reason
    assert "dispatch" not in w.names()


def test_an_approved_pull_request_is_never_pushed_to(tmp_path):
    w = Wired(tmp_path, Forge({CODE: _reviews("APPROVED")}))
    ok, why, _ = w.run()
    assert "produce" not in w.names()
    assert w.names()[-1] == "dispatch"


def test_unreadable_reviews_stop_rather_than_push(tmp_path):
    class Broken(Forge):
        def list_reviews(self, repo, n, page=1, limit=50):
            return 500, None
    w = Wired(tmp_path, Broken())
    with pytest.raises(w.mod.Stop):
        w.run()
    assert "produce" not in w.names()


def test_a_code_reviewer_route_never_calls_the_producer(tmp_path):
    w = Wired(tmp_path, Forge({CODE: _reviews("APPROVED")}), position="code_reviewer")
    ok, why, _ = w.run()
    assert ok, why
    assert w.names() == ["route", "dispatch"]
    assert w.positions == ["code_reviewer"]


def test_a_forge_read_that_fails_is_a_code_review(tmp_path):
    class NoFiles(Forge):
        def list_pull_files(self, repo, n, page_cap=20, limit=50):
            return 0, None
    w = Wired(tmp_path, NoFiles({CODE: _reviews("APPROVED")}))
    ok, why, _ = w.run()
    assert ok, why
    assert w.names() == ["dispatch"]


def test_the_dispatch_argv_is_unchanged(tmp_path):
    out = tmp_path / "argv.txt"
    p = tmp_path / "fake.py"
    p.write_text(f"import sys; open({str(out)!r},'w').write(' '.join(sys.argv[1:]))")
    os.environ["FLEET_REVIEW_CMD"] = f"{sys.executable} {p}"
    try:
        mod = _load(tmp_path)
        ok, why = mod.dispatch_review("operator/x", 7, CODE, "plans/p.md")
    finally:
        del os.environ["FLEET_REVIEW_CMD"]
    assert ok, why
    assert out.read_text() == "operator/x 7 --post --no-followups"


def test_the_tools_are_called_with_the_contracted_argv(tmp_path):
    """Real subprocesses against stub executables: the route is asked with the plan
    text and an override of `none`, and the producer gets no plan-less shortcut."""
    mod = _load(tmp_path)
    binp = tmp_path / "stubbin"
    binp.mkdir()
    log = tmp_path / "calls.log"
    for tool, reply in (("fleet-style", '{"position": "style_reviewer", "ok": true}'),
                        ("fleet-style-evidence", "")):
        f = binp / tool
        f.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nprev=\nfor a in \"$@\"; do case $prev in --plan-file|--plan) "
                     f"cat \"$a\" >> {log};; esac; prev=$a; done\necho '{reply}'\n")
        f.chmod(0o755)
    mod.CONFIG_BIN = binp
    got = mod.style_route("operator/x", 7, "PLANTEXT", CODE, BASE, "/g", [
        {"filename": "a", "status": "added", "previous_filename": None, "extra": 1}])
    assert got["position"] == "style_reviewer"
    assert mod.style_evidence_ok("operator/x", 7, "0007-reskin", CODE, BASE, "/g")
    ok, _ = mod.style_produce("operator/x", 7, "PLANTEXT", tmp_path, BASE, CODE, 3)
    assert ok
    text = log.read_text()
    assert "route operator/x --pr 7" in text and "--override none" in text
    assert "--base " + BASE in text and "--head " + CODE in text
    assert "PLANTEXT" in text
    assert "verify operator/x --pr 7 --plan-id 0007-reskin" in text
    assert "produce operator/x 7 --plan" in text and f"--worktree {tmp_path}" in text


def test_a_plan_that_does_not_opt_in_costs_no_forge_read(tmp_path):
    w = Wired(tmp_path, Forge({CODE: _reviews("APPROVED")}),
              plan_text="---\nid: 0007-reskin\n---\n")
    ok, why, _ = w.run()
    assert ok, why
    assert w.names() == ["dispatch"]

@pytest.mark.parametrize("position", ["style_reviewer", "code_reviewer"])
def test_closed_routed_reviewer_gate_prevents_dispatch(tmp_path, position):
    w = Wired(tmp_path, Forge(_approving()), position=position)
    checked = []

    def hold(repo, routed):
        checked.append(routed)
        return "held: weekly (seat " + routed + "#1 sub-sonnet)"

    w.mod.review_quota_hold = hold
    ok, why, rows = w.run()
    assert not ok
    assert checked == [position]
    assert "held: weekly" in why
    assert "dispatch" not in w.names()
