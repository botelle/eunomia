"""Tests for bin/fleet-merge (plan 0108), against a fake forge.

The fake stands in for `fleetforge.Forge` where the program builds one. The
plan files, statuses, reviews and findings document are all real data the
checks read; only the transport is replaced.
"""
import base64
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


merge = _load("fleet_merge_under_test", BIN / "fleet-merge")
REPO = "operator/x"
PLAN = "0105-a-thing"
HEAD = "a" * 40
BASE = "b" * 40
OWN = f"plans/{PLAN}.md"


def plan_text(status="ready", body="# plan\n"):
    return f"---\nid: {PLAN}\nstatus: {status}\n---\n\n{body}"


def b64(t):
    return {"content": base64.b64encode(t.encode()).decode()}


class FakeForge:
    def __init__(self):
        self.login = "mergebot"
        self.main_plans = {PLAN: plan_text("ready")}
        self.contents = {(OWN, BASE): plan_text("ready"), (OWN, HEAD): plan_text("done")}
        self.pr = {"number": 7, "body": f"Plan: {PLAN}\n", "user": {"login": "implbot"},
                   "base": {"ref": "main", "sha": BASE}, "head": {"sha": HEAD},
                   "mergeable": True}
        self.files = [{"filename": "bin/thing"}, {"filename": OWN}]
        self.reviews = [
            {"id": 1, "user": {"login": "revbot"}, "state": "APPROVED", "commit_id": HEAD},
            {"id": 2, "user": {"login": "rev2bot"}, "state": "APPROVED", "commit_id": HEAD}]
        self.statuses = [
            {"id": 1, "context": "eunomia/deps", "status": "success", "creator": {"login": "mergebot"}},
            {"id": 2, "context": "CI / tests", "status": "success", "creator": {"login": "ci"}}]
        self.branch = {"enable_status_check": True,
                       "status_check_contexts": ["CI / tests", "eunomia/deps"]}
        self.merges = []
        self.merge_status = 200
        self.writes = 0

    def get_user(self):
        return 200, {"login": self.login}

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        return 200, ([{"number": self.pr["number"]}] if page == 1 else [])

    def get_pull(self, repo, n):
        return 200, dict(self.pr)

    def list_pull_files(self, repo, n, page_cap=20, limit=50):
        return 200, self.files

    def get_contents(self, repo, path, ref=None):
        if ref == "main":
            if path.startswith("plans/") and path[6:-3] in self.main_plans:
                return 200, b64(self.main_plans[path[6:-3]])
            return 404, None
        if (path, ref) in self.contents:
            return 200, b64(self.contents[(path, ref)])
        return 404, None

    def list_reviews(self, repo, n, page=1, limit=50):
        return 200, (self.reviews if page == 1 else [])

    def list_statuses(self, repo, sha, page=1, limit=50):
        return 200, (list(reversed(self.statuses)) if page == 1 else [])

    def get_branch(self, repo, name):
        return 200, self.branch

    def merge_pull(self, repo, n, head_sha):
        self.writes += 1
        self.merges.append((n, head_sha))
        return self.merge_status, {}


def findings(tests=("pass",), head=HEAD, plan=PLAN, all_passed=True):
    return {
        "schema": "meeting-findings/1", "repo": REPO, "plan": plan,
        "generated_at": "2026-10-19T00:00:00Z",
        "impl": {"pr": 7, "head_sha": head},
        "lanes": [{
            "pr": 8, "head_sha": "c" * 40, "branch": "agent/tests/x/y", "runner": "r",
            "dispatch_sha": None, "prompt_sha256": None, "prompt_sha256_source": None,
            "prompt_sha256_verified": False, "prompt_sha256_reason": None,
            "prompt_sha256_lane_run": None, "review_id": None, "read_by": "h",
            "read_at": "t", "independent_reader": False, "files": [],
            "run": {"outcome": "ran", "pytest_exit": 0, "duration_s": 1.0, "reason": None},
            "counts": {"pass": 1, "fail": 0, "error": 0, "skip": 0},
            "tests": [{"id": f"t{i}", "outcome": o,
                       "pytest_outcome": {"pass": "passed", "fail": "failed",
                                          "error": "error", "skip": "skipped"}[o],
                       "reason": None, "reason_redacted": [], "source_path": "tests/lane/a.py",
                       "duration_s": 0.1} for i, o in enumerate(tests)]}],
        "not_run": [], "comparable": None, "comparability": None, "all_passed": all_passed,
        "confinement": {"profile_sha256": "d" * 64,
                        "network": {"outbound": "this-host-only", "bind": "any-local-address",
                                    "denied_ports": [8200]},
                        "env_keys": [], "timeout_seconds": 900},
    }


def put_findings(tmp_path, doc, plan=PLAN, head=HEAD):
    p = tmp_path / "work" / "meetings" / plan / head[:12] / "findings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc))


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    monkeypatch.setenv("FLEET_WORK_ROOT", str(tmp_path / "work"))
    fake = FakeForge()
    pages = []
    monkeypatch.setattr(merge, "_forge", lambda token: fake)
    monkeypatch.setattr(merge, "_token", lambda: ("tok", ""))
    monkeypatch.setattr(merge, "autonomy", lambda repo: "autonomous")
    monkeypatch.setattr(merge.watch, "conf_repos", lambda: ([REPO], [REPO]))
    monkeypatch.setattr(merge.watch, "notify",
                        lambda title, body, label=None: pages.append((title, body)) or True)
    fake.pages = pages
    fake.tmp = tmp_path
    fake.out = capsys
    put_findings(tmp_path, findings())
    return fake


def run(fake):
    assert merge.once() == 0
    return fake.out.readouterr().out


def test_happy_path_merges_once_with_checked_sha(env):
    out = run(env)
    assert env.merges == [(7, HEAD)]
    assert "merged" in out


def test_pr_not_touching_its_plan_also_merges(env):
    env.files = [{"filename": "bin/thing"}]
    run(env)
    assert env.merges == [(7, HEAD)]


def blocked(env, check):
    import shutil
    shutil.rmtree(env.tmp / "merge-seen", ignore_errors=True)   # else a repeat is logged once
    out = run(env)
    assert env.merges == [], out
    assert f"check {check}" in out, out


def test_check1_critical_repo_is_untouched(env, monkeypatch):
    monkeypatch.setattr(merge, "autonomy", lambda repo: "critical")
    assert run(env) == ""
    assert env.merges == [] and env.pages == []


def test_check1_unreadable_autonomy_pages_and_stops(env, monkeypatch):
    def boom(repo):
        raise merge.Undecidable("cannot read autonomy")
    monkeypatch.setattr(merge, "autonomy", boom)
    run(env)
    assert env.merges == [] and len(env.pages) == 1


def test_no_autonomy_row_is_skipped_without_a_forge_write(env, monkeypatch):
    # fleet-config answers `critical` for a repo with no row (builtin).
    monkeypatch.setattr(merge, "autonomy", lambda repo: "critical")
    run(env)
    assert env.writes == 0


@pytest.mark.parametrize("body", ["no marker", f"Plan: {PLAN}\nPlan: {PLAN}\n",
                                  "Plan: 0105\n", "Plan: 0105-a-thing-and-more\n"])
def test_check2_marker(env, body):
    env.pr["body"] = body
    blocked(env, "2 plan marker")


def test_check2_short_id_prefix_of_plan_is_not_the_plan(env):
    env.main_plans["0105"] = plan_text("ready")
    env.pr["body"] = "Plan: 0105\n"       # a different plan; its own file is not touched
    env.files = [{"filename": OWN}]
    blocked(env, "3 plan diff")


def test_check2_base_not_main(env):
    env.pr["base"] = {"ref": "dev", "sha": BASE}
    blocked(env, "2 plan marker")


@pytest.mark.parametrize("status", ["done", "draft"])
def test_check2_plan_not_ready_on_main(env, status):
    env.main_plans[PLAN] = plan_text(status)
    blocked(env, "2 plan marker")


def test_check2_plan_absent_on_main(env):
    env.main_plans.clear()
    blocked(env, "2 plan marker")


def test_check3_other_plan_edited(env):
    env.files.append({"filename": "plans/0106-b.md"})
    blocked(env, "3 plan diff")


def test_check3_new_plan_added(env):
    env.files.append({"filename": "plans/0999-x.md"})
    blocked(env, "3 plan diff")


def test_check3_own_plan_body_edited(env):
    env.contents[(OWN, HEAD)] = plan_text("done", body="# plan\nmore\n")
    blocked(env, "3 plan diff")


def test_check3_own_plan_status_not_the_flip(env):
    env.contents[(OWN, HEAD)] = plan_text("draft")
    blocked(env, "3 plan diff")


def test_check3_plan_rename(env):
    env.files = [{"filename": OWN, "previous_filename": "plans/0105-old.md"}]
    blocked(env, "3 plan diff")


@pytest.mark.parametrize("path", ["docs/adr/0100-x.md", "tests/lane/a.py",
                                  "bin/fleet-merge", "bin/fleet-deps", "bin/fleet-watch",
                                  "bin/fleetlib.py", "bin/fleetforge.py", "bin/fleet-config",
                                  "bin/fleet-install-hooks", "bin/fleet-secret-guard",
                                  "config/secret-patterns.conf",
                                  "launchd/org.eunomia.fleet-merge.plist",
                                  "launchd/org.eunomia.fleet-deps.plist"])
def test_check4_protected_paths(env, path):
    env.files.append({"filename": path})
    blocked(env, "4 protected paths")


def test_check4_rename_out_of_protected_path(env):
    env.files.append({"filename": "elsewhere.py", "previous_filename": "bin/fleet-merge"})
    blocked(env, "4 protected paths")


@pytest.mark.parametrize("who", ["mergebot", "revbot", "rev2bot"])
def test_check5_author(env, who):
    env.pr["user"] = {"login": who}
    blocked(env, "5 author")


def test_check6_one_reviewer_only(env):
    env.reviews = env.reviews[:1]
    blocked(env, "6 reviews")


def test_check6_same_reviewer_twice_no_rev2bot(env):
    env.reviews = [env.reviews[0], dict(env.reviews[0], id=3)]
    blocked(env, "6 reviews")


def test_check6_stale_dismissed_or_other_head(env):
    for patch in ({"stale": True}, {"dismissed": True}, {"commit_id": "e" * 40}):
        env.reviews[1] = dict(env.reviews[1], **patch)
        blocked(env, "6 reviews")
        env.reviews[1] = {"id": 2, "user": {"login": "rev2bot"}, "state": "APPROVED",
                          "commit_id": HEAD}


def test_check6_later_request_changes_blocks(env):
    env.reviews.append({"id": 9, "user": {"login": "rev2bot"}, "state": "REQUEST_CHANGES",
                        "commit_id": HEAD})
    blocked(env, "6 reviews")


def test_check7_deps_status_by_implbot_does_not_count(env):
    env.statuses[0] = dict(env.statuses[0], creator={"login": "implbot"})
    blocked(env, "7 deps status")


def test_check7_later_foreign_success_over_mergebot_failure(env):
    env.statuses[0] = dict(env.statuses[0], status="failure")
    env.statuses.append({"id": 3, "context": "eunomia/deps", "status": "success",
                         "creator": {"login": "implbot"}})
    blocked(env, "7 deps status")


@pytest.mark.parametrize("state", ["failure", "pending"])
def test_check7_not_success(env, state):
    env.statuses[0] = dict(env.statuses[0], status=state)
    blocked(env, "7 deps status")


def test_check7_missing(env):
    del env.statuses[0]
    blocked(env, "7 deps status")


def test_check8_pending_and_missing_ci(env):
    env.statuses[1] = dict(env.statuses[1], status="pending")
    blocked(env, "8 ci")
    env.statuses.pop(1)
    blocked(env, "8 ci")


def test_check8_failing_other_required_context(env):
    env.branch["status_check_contexts"].append("Lint")
    blocked(env, "8 ci")


def test_check9_missing_document(env):
    for p in (env.tmp / "work").rglob("findings.json"):
        p.unlink()
    blocked(env, "9 integration tests")


@pytest.mark.parametrize("outcome", ["skip", "fail", "error"])
def test_check9_non_pass_outcome(env, outcome):
    put_findings(env.tmp, findings(tests=("pass", outcome), all_passed=False))
    blocked(env, "9 integration tests")


def test_check9_a_skip_even_if_document_says_all_passed(env):
    put_findings(env.tmp, findings(tests=("pass", "skip"), all_passed=True))
    blocked(env, "9 integration tests")


def test_check9_no_tests(env):
    put_findings(env.tmp, findings(tests=()))
    blocked(env, "9 integration tests")


def test_check9_other_head(env):
    put_findings(env.tmp, findings(head="f" * 40))   # at the right path, for another head
    blocked(env, "9 integration tests")


def test_check9_invalid_document(env):
    doc = findings()
    del doc["confinement"]
    put_findings(env.tmp, doc)
    blocked(env, "9 integration tests")


def test_check10_not_mergeable(env):
    env.pr["mergeable"] = False
    blocked(env, "10 mergeable")


def test_identity_other_than_mergebot_refuses_everything(env):
    env.login = "implbot"
    assert merge.once() == 2
    assert env.merges == [] and len(env.pages) == 1


def test_blocked_pr_is_logged_once(env):
    env.pr["mergeable"] = False
    assert "check 10" in run(env)
    assert run(env) == ""
    assert env.pages == []


def test_failed_merge_is_not_retried_in_the_pass(env):
    env.merge_status = 409
    out = run(env)
    assert len(env.merges) == 1 and "refused by the forge" in out


def test_unreadable_read_pages_not_merges(env, monkeypatch):
    monkeypatch.setattr(env, "list_reviews", lambda *a, **k: (500, None))
    run(env)
    assert env.merges == [] and len(env.pages) == 1


# ------------------------------------------------------------- tests PRs
def tests_pr(env):
    env.pr["body"] = f"Tests-for: {PLAN}\n\nrunner: x\n"
    env.files = [{"filename": "tests/lane/test_a.py"}]
    env.statuses = [s for s in env.statuses if s["context"] != "eunomia/deps"]
    env.branch["status_check_contexts"] = ["CI / tests"]
    for p in (env.tmp / "work").rglob("findings.json"):
        p.unlink()          # a tests PR needs no integration findings


def test_tests_pr_meeting_d1b_merges(env):
    tests_pr(env)
    run(env)
    assert env.merges == [(7, HEAD)]


def test_tests_pr_for_done_plan_merges(env):
    tests_pr(env)
    env.main_plans[PLAN] = plan_text("done")
    run(env)
    assert env.merges == [(7, HEAD)]


def test_tests_pr_outside_lane_does_not_merge(env):
    tests_pr(env)
    env.files.append({"filename": "bin/x"})
    blocked(env, "t3 files")


def test_tests_pr_renamed_file_does_not_merge(env):
    tests_pr(env)
    env.files = [{"filename": "tests/lane/a.py", "previous_filename": "bin/a"}]
    blocked(env, "t3 files")


def test_tests_pr_one_reviewer_does_not_merge(env):
    tests_pr(env)
    env.reviews = env.reviews[:1]
    blocked(env, "6 reviews")


def test_tests_pr_by_a_reviewer_does_not_merge(env):
    tests_pr(env)
    env.pr["user"] = {"login": "rev2bot"}
    blocked(env, "t4 author")


def test_tests_pr_unknown_plan_does_not_merge(env):
    tests_pr(env)
    env.main_plans.clear()
    blocked(env, "t2 marker")


def test_tests_pr_ci_red_does_not_merge(env):
    tests_pr(env)
    env.statuses[0] = dict(env.statuses[0], status="failure")
    blocked(env, "8 ci")


def test_enforcing_names_the_plan_list():
    assert "bin/fleet-merge" in merge.ENFORCING and len(merge.ENFORCING) == 13


def test_token_command_with_arguments_runs_through_a_shell(monkeypatch):
    monkeypatch.setattr(merge, "TOKEN_CMD", "printf 'tok'")
    assert merge._token() == ("tok", "")
