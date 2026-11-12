"""Tests for bin/fleet-deps (plan 0107), against a fake forge.

The fake stands in for `fleetforge.Forge` at the two places the program builds
one (`fleet-deps` and `fleet-watch`'s `_forge`), so `plan_files`,
`_plan_dependency_block` and the status verbs all run for real over it.
"""
import base64
import hashlib
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


deps = _load("fleet_deps_under_test", BIN / "fleet-deps")
REPO = "operator/x"


def plan_text(pid, status="ready", depends=()):
    dep = "".join(f'  - "{d}"\n' for d in depends)
    block = f"depends_on:\n{dep}" if depends else ""
    return f"---\nid: {pid}\nstatus: {status}\n{block}---\n\n# {pid}\n"


class FakeForge:
    def __init__(self):
        self.login = "mergebot"
        self.plans = {}            # id -> text, or None for "unreadable"
        self.paths = set()
        self.prs = []              # dicts: number, body, sha, files
        self.statuses = {}         # sha -> list of status dicts (newest first)
        self.posts = []
        self.list_ok = True
        self.governance = None     # text, or None for "no governance.yaml"
        self.governance_status = None   # set to e.g. 500 to simulate unreadable

    def deps_posts(self):
        return [p for p in self.posts if p[3] == deps.CONTEXT]

    def lint_posts(self):
        return [p for p in self.posts if p[3] == deps.LINT_CONTEXT]

    def get_user(self):
        return 200, {"login": self.login}

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        if not self.list_ok:
            return 500, None
        if page > 1:
            return 200, []
        return 200, [{"number": p["number"], "body": p["body"],
                      "base": {"ref": "main"}, "head": {"sha": p["sha"]}}
                     for p in self.prs]

    def list_pull_files(self, repo, n, page_cap=20, limit=50):
        for p in self.prs:
            if p["number"] == n:
                return 200, [{"filename": f} for f in p.get("files", [])]
        return 200, []

    def get_pull(self, repo, n):
        return 404, None

    def list_contents(self, repo, d, ref=None):
        assert d == "plans"
        return 200, [{"name": f"{i}.md"} for i in self.plans]

    @staticmethod
    def _sha_for(text):
        """A content-addressed stand-in for a real blob sha: changing the
        text changes the sha, the same property plan 0216's cache relies on
        to never go stale."""
        return "b" + hashlib.sha1((text or "").encode()).hexdigest()[:16]

    def get_git_tree(self, repo, sha):
        """The two-level tree `_read_plan_dir` walks: a root (any sha other
        than the `plans` subtree's own) carrying a `plans` entry and, if
        configured, a `governance.yaml` entry; and the `plans` subtree
        itself, carrying one blob entry per `self.plans`."""
        if sha == "plans-tree":
            entries = [
                {"path": f"{pid}.md", "type": "blob",
                 "sha": f"unreadable-{pid}" if text is None else self._sha_for(text)}
                for pid, text in self.plans.items()]
            return 200, {"tree": entries, "truncated": False}
        entries = [{"path": "plans", "type": "tree", "sha": "plans-tree"}]
        if self.governance_status is not None:
            entries.append({"path": "governance.yaml", "type": "blob", "sha": "gov-unreadable"})
        elif self.governance is not None:
            entries.append({"path": "governance.yaml", "type": "blob",
                            "sha": self._sha_for(self.governance)})
        return 200, {"tree": entries, "truncated": False}

    def get_git_blob(self, repo, sha):
        if sha == "gov-unreadable":
            return self.governance_status, None
        if sha.startswith("unreadable-"):
            return 500, None
        if self.governance is not None and sha == self._sha_for(self.governance):
            return 200, {"content": base64.b64encode(self.governance.encode()).decode()}
        for text in self.plans.values():
            if text is not None and sha == self._sha_for(text):
                return 200, {"content": base64.b64encode(text.encode()).decode()}
        return 404, None

    def get_contents(self, repo, path, ref=None):
        if path == "governance.yaml":
            if self.governance_status is not None:
                return self.governance_status, None
            if self.governance is None:
                return 404, None
            return 200, {"content": base64.b64encode(self.governance.encode()).decode()}
        if path.startswith("plans/"):
            pid = path[len("plans/"):-len(".md")]
            if pid not in self.plans:
                return 404, None
            text = self.plans[pid]
            if text is None:
                return 500, None
            return 200, {"content": base64.b64encode(text.encode()).decode()}
        return (200, {}) if path in self.paths else (404, None)

    def list_statuses(self, repo, sha, page=1, limit=50):
        return 200, (self.statuses.get(sha, []) if page == 1 else [])

    def post_status(self, repo, sha, state, description, context):
        self.posts.append((sha, state, description, context))
        n = len(self.posts) + 100
        self.statuses.setdefault(sha, []).insert(0, {
            "id": n, "context": context, "status": state,
            "description": description, "creator": {"login": self.login}})
        return 201, {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    fake = FakeForge()
    pages = []
    monkeypatch.setattr(deps, "_forge", lambda token: fake)
    monkeypatch.setattr(deps.watch, "_forge", lambda token: fake)
    monkeypatch.setattr(deps, "_token", lambda: ("tok", ""))
    monkeypatch.setattr(deps.watch, "conf_repos", lambda: ([REPO], [REPO]))
    monkeypatch.setattr(deps.watch, "notify",
                        lambda title, body, label=None: pages.append((title, body)) or True)
    fake.pages = pages
    return fake


def pr(fake, number, body, sha=None, files=None):
    fake.prs.append({"number": number, "body": body, "sha": sha or f"sha{number}",
                     "files": files or []})


def last(fake, sha):
    s = [p for p in fake.deps_posts() if p[0] == sha]
    return s[-1][1:3] if s else None


def last_lint(fake, sha):
    s = [p for p in fake.lint_posts() if p[0] == sha]
    return s[-1][1:3] if s else None


def test_plan_absent_then_draft_is_failure(env):
    pr(env, 1, "Plan: 0999-x")
    assert deps.once() == 0
    assert last(env, "sha1")[0] == "failure"
    env.plans["0999-x"] = plan_text("0999-x", "draft")
    deps.once()
    assert last(env, "sha1")[0] == "failure"
    assert "draft" in last(env, "sha1")[1]


def test_dependency_then_success_without_push(env):
    env.plans["0998-dep"] = plan_text("0998-dep", "ready")
    env.plans["0999-x"] = plan_text("0999-x", "ready", ["0998-dep"])
    pr(env, 1, "Plan: 0999-x")
    deps.once()
    assert last(env, "sha1")[0] == "failure"
    assert "0998-dep" in last(env, "sha1")[1]
    env.plans["0998-dep"] = plan_text("0998-dep", "done")
    deps.once()
    assert last(env, "sha1")[0] == "success"


def test_pr_adding_two_plans_is_success(env):
    # a plan PR names no plan: it adds plans/ files, none on main yet
    pr(env, 1, "Adds 0997-a and 0998-b (b depends on a).")
    deps.once()
    assert last(env, "sha1") == ("success", "no plan named")


def test_no_plan_line_is_success(env):
    pr(env, 1, "just docs")
    deps.once()
    assert last(env, "sha1") == ("success", deps.NO_PLAN)


def test_unreadable_plan_is_pending_and_pages_once(env):
    env.plans["0999-x"] = None
    pr(env, 1, "Plan: 0999-x")
    deps.once()
    deps.once()
    assert last(env, "sha1")[0] == "pending"
    assert len(env.pages) == 1


def test_pending_page_rearms_after_decision(env):
    env.plans["0999-x"] = None
    pr(env, 1, "Plan: 0999-x")
    deps.once()
    env.plans["0999-x"] = plan_text("0999-x")
    deps.once()
    env.plans["0999-x"] = None
    deps.once()
    assert len(env.pages) == 2


def test_unreadable_pr_list_pages_and_posts_nothing(env):
    env.list_ok = False
    deps.once()
    assert env.posts == []
    assert len(env.pages) == 1


def test_wrong_identity_refuses_run(env):
    env.login = "implbot"
    pr(env, 1, "plain")
    assert deps.once() == 2
    assert env.posts == []
    assert len(env.pages) == 1


def test_second_pass_posts_nothing(env):
    pr(env, 1, "plain")
    deps.once()
    deps.once()
    assert len(env.deps_posts()) == 1


def test_other_accounts_status_does_not_suppress(env):
    pr(env, 1, "plain")
    env.statuses["sha1"] = [{"id": 5, "context": deps.CONTEXT, "status": "success",
                             "description": deps.NO_PLAN,
                             "creator": {"login": "implbot"}}]
    deps.once()
    assert len(env.deps_posts()) == 1


def test_no_fallback_to_default_token_cmd():
    assert "mergebot" in deps.TOKEN_CMD or deps.os.environ.get("FLEET_DEPS_TOKEN_CMD")


def test_list_pull_files_fails_rather_than_truncating():
    full = [{"filename": "a"}]

    class Scripted(deps.fleetforge.Forge):
        __slots__ = ("replies",)

        def _dispatch(self, spec):
            return self.replies()

    forge = Scripted("http://x", "t")
    forge.replies = lambda: (200, full)
    assert forge.list_pull_files(REPO, 1, page_cap=3) == (0, None)
    seq = iter([(200, full), (200, full), (200, [])])
    forge.replies = lambda: next(seq)
    assert forge.list_pull_files(REPO, 1, page_cap=3) == (200, full + full)


def test_token_command_with_arguments_runs_through_a_shell(monkeypatch):
    monkeypatch.setattr(deps, "TOKEN_CMD", "printf 'tok'")
    assert deps._token() == ("tok", "")


# --------------------------------------------------- eunomia/plan-lint (plan 0177)

def _valid_plan(pid, paths=("a",)):
    paths_text = "[" + ", ".join(f'"{p}"' for p in paths) + "]"
    return f"""---
id: {pid}
status: ready
repo: operator/x
zone: public
tier: 1
paths: {paths_text}
---

## 1. Goal
g

## 2. Deliverables
- d

## 3. Boundaries
- don't reuse the collector regex, because it guards free text

## 4. Definition of done
- dod

## 5. Handoff
h

## 6. Resources
- lease: b
"""


def _broken_plan(pid):
    """Missing §5 Handoff — an ordinary `lint()` problem, nothing to do with
    spec_watch; this status wraps the whole of `lint()`, not only the new
    rule."""
    return _valid_plan(pid).replace("## 5. Handoff\nh\n\n", "")


def test_a_pr_changing_a_broken_plan_is_failure(env):
    env.plans["0999-x"] = _broken_plan("0999-x")
    pr(env, 1, "adds a plan", files=["plans/0999-x.md"])
    deps.once()
    assert last_lint(env, "sha1")[0] == "failure"


def test_a_fixed_head_is_success(env):
    env.plans["0999-x"] = _broken_plan("0999-x")
    pr(env, 1, "adds a plan", sha="sha-broken", files=["plans/0999-x.md"])
    deps.once()
    assert last_lint(env, "sha-broken")[0] == "failure"
    env.plans["0999-x"] = _valid_plan("0999-x")
    env.prs[0]["sha"] = "sha-fixed"
    deps.once()
    assert last_lint(env, "sha-fixed") == ("success", "0 problems")


def test_a_pr_with_no_plan_change_gets_no_lint_status(env):
    pr(env, 1, "just code", files=["bin/thing.py"])
    deps.once()
    assert env.lint_posts() == []


def test_an_unreadable_governance_yaml_is_pending_and_pages_once(env):
    env.plans["0999-x"] = _valid_plan("0999-x")
    env.governance_status = 500
    pr(env, 1, "adds a plan", files=["plans/0999-x.md"])
    deps.once()
    deps.once()
    assert last_lint(env, "sha1")[0] == "pending"
    assert len(env.pages) == 1


def test_a_repeat_run_with_no_change_posts_nothing_again(env):
    env.plans["0999-x"] = _valid_plan("0999-x")
    pr(env, 1, "adds a plan", files=["plans/0999-x.md"])
    deps.once()
    deps.once()
    assert len(env.lint_posts()) == 1


def test_a_spec_watch_problem_reaches_the_lint_status_through_governance_yaml(env):
    """End-to-end: `governance.yaml` is read from the forge same as any other
    plan file, and the spec_watch rule it declares is one of the problems
    `lint()` can report through this status."""
    env.governance = ('spec_watch:\n  - name: api-routes\n    kind: api\n'
                      '    code:\n      - "api/x/**"\n'
                      '    specs:\n      - "docs/declared-behaviour.md"\n')
    env.plans["0999-x"] = _valid_plan("0999-x", paths=("api/x/handler.py",))
    pr(env, 1, "adds a plan", files=["plans/0999-x.md"])
    deps.once()
    state, desc = last_lint(env, "sha1")
    assert state == "failure"
    assert "api-routes" in desc


def test_a_bad_plan_already_on_main_does_not_fail_an_unrelated_pr(env):
    """`0998-stale` is a broken plan already sitting in `plans/` (as if
    merged to main) that this PR neither adds nor modifies — only
    `0999-x`, which is clean, is in the diff. `lint()` still reads
    `0998-stale` for the index, but its own problem must not surface."""
    env.plans["0998-stale"] = _broken_plan("0998-stale")
    env.plans["0999-x"] = _valid_plan("0999-x")
    pr(env, 1, "adds 0999-x only", files=["plans/0999-x.md"])
    deps.once()
    assert last_lint(env, "sha1") == ("success", "0 problems")
