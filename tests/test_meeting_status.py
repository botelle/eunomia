"""Tests for `eunomia/meeting` in bin/fleet-deps (plan 0145), against a fake forge.

Findings documents are written into a temporary work root and validated by the
real schema; only the forge is replaced.
"""
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


deps = _load("fleet_deps_for_meeting_status", BIN / "fleet-deps")
REPO = "operator/x"
PLAN = "0105-a-thing"
IMPL_HEAD = "a" * 40
LANE_HEAD = "c" * 40
LANE_PR, IMPL_PR = 8, 7
CTX = deps.MEETING_CONTEXT


def lane_pull(number=LANE_PR, sha=LANE_HEAD, user="implbot", plan=PLAN, body=None):
    if body is None:
        body = f"Tests-for: {plan}\nrunner: r\n"
    return {"number": number, "body": body, "user": {"login": user},
            "base": {"ref": "main"},
            "head": {"sha": sha, "ref": f"agent/tests/{plan}/r"}}


def doc(tests=("pass",), outcome="ran", lane_head=LANE_HEAD, impl_head=IMPL_HEAD,
        not_run=(), generated="2026-10-23T00:00:00Z", lanes=True):
    outc = {"pass": "passed", "fail": "failed", "error": "error", "skip": "skipped"}
    lane = {
        "pr": LANE_PR, "head_sha": lane_head, "branch": f"agent/tests/{PLAN}/r", "runner": "r",
        "dispatch_sha": None, "prompt_sha256": None, "prompt_sha256_source": None,
        "prompt_sha256_verified": False, "prompt_sha256_reason": None,
        "prompt_sha256_lane_run": None, "review_id": None, "read_by": "h",
        "read_at": "t", "independent_reader": False, "files": [],
        "run": {"outcome": outcome, "pytest_exit": 0, "duration_s": 1.0, "reason": None},
        "counts": {"pass": 0, "fail": 0, "error": 0, "skip": 0},
        "tests": [{"id": f"t{i}", "outcome": o, "pytest_outcome": outc[o], "reason": None,
                   "reason_redacted": [], "source_path": "tests/lane/a.py", "duration_s": 0.1}
                  for i, o in enumerate(tests)]}
    return {
        "schema": "meeting-findings/1", "repo": REPO, "plan": PLAN, "generated_at": generated,
        "impl": {"pr": IMPL_PR, "head_sha": impl_head},
        "lanes": [lane] if lanes else [],
        "not_run": list(not_run), "comparable": None, "comparability": None,
        "all_passed": False,
        "confinement": {"profile_sha256": "d" * 64,
                        "network": {"outbound": "this-host-only", "bind": "any-local-address",
                                    "denied_ports": [8200]},
                        "env_keys": [], "timeout_seconds": 900},
    }


class FakeForge:
    def __init__(self):
        self.login = "mergebot"
        self.prs = []
        self.files = {}
        self.impl = {"number": IMPL_PR, "merged": True, "head": {"sha": IMPL_HEAD}}
        self.impl_reads = 0
        self.statuses = {}
        self.posts = []

    def get_user(self):
        return 200, {"login": self.login}

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        return 200, (list(self.prs) if page == 1 else [])

    def list_pull_files(self, repo, n, page_cap=20, limit=50):
        return 200, [{"filename": f} for f in self.files.get(n, [])]

    def get_pull(self, repo, n):
        self.impl_reads += 1
        return (200, self.impl) if n == IMPL_PR else (404, None)

    def get_contents(self, repo, path, ref=None):
        return 404, None

    def list_statuses(self, repo, sha, page=1, limit=50):
        return 200, (self.statuses.get(sha, []) if page == 1 else [])

    def post_status(self, repo, sha, state, description, context):
        self.posts.append((sha, state, description, context))
        self.statuses.setdefault(sha, []).insert(0, {
            "id": len(self.posts) + 100, "context": context, "status": state,
            "description": description, "creator": {"login": self.login}})
        return 201, {}


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    monkeypatch.setenv("FLEET_WORK_ROOT", str(tmp_path / "work"))
    fake = FakeForge()
    monkeypatch.setattr(deps, "_forge", lambda token: fake)
    monkeypatch.setattr(deps.watch, "_forge", lambda token: fake)
    monkeypatch.setattr(deps, "_token", lambda: ("tok", ""))
    monkeypatch.setattr(deps.watch, "conf_repos", lambda: ([REPO], [REPO]))
    monkeypatch.setattr(deps.watch, "notify", lambda *a, **k: True)
    fake.tmp, fake.out = tmp_path, capsys
    return fake


def put(env, document, name="aaaaaaaaaaaa"):
    p = env.tmp / "work" / "meetings" / PLAN / name / "findings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(document))
    return p


def meeting(env, sha=LANE_HEAD):
    got = [p[1:3] for p in env.posts if p[0] == sha and p[3] == CTX]
    return got[-1] if got else None


def run(env):
    assert deps.once() == 0


def test_no_document_posts_nothing(env):
    env.prs = [lane_pull()]
    run(env)
    assert meeting(env) is None


def test_all_pass_is_success(env):
    env.prs = [lane_pull()]
    put(env, doc(("pass", "pass")))
    run(env)
    assert meeting(env) == ("success", "met: 2 passed")


@pytest.mark.parametrize("tests,outcome,want", [
    (("pass", "fail"), "ran", "1 passed, 1 failed"),
    (("error",), "ran", "1 errored"),
    (("pass", "skip"), "ran", "1 skipped"),
    ((), "ran", "no tests recorded"),
    ((), "timeout", "timeout"),
    ((), "refused", "refused"),
    ((), "collection-error", "collection-error"),
])
def test_anything_but_all_pass_is_failure(env, tests, outcome, want):
    env.prs = [lane_pull()]
    put(env, doc(tests, outcome))
    run(env)
    state, desc = meeting(env)
    assert state == "failure" and want in desc


def test_not_run_posts_nothing(env):
    env.prs = [lane_pull()]
    put(env, doc(lanes=False, not_run=[{"pr": LANE_PR, "head_sha": LANE_HEAD, "reason": "unread"}]))
    run(env)
    assert meeting(env) is None


def test_open_implementation_posts_nothing(env):
    env.prs = [lane_pull()]
    env.impl = {"number": IMPL_PR, "merged": False, "head": {"sha": IMPL_HEAD}}
    put(env, doc())
    run(env)
    assert meeting(env) is None


def test_implementation_merged_at_another_head_posts_nothing(env):
    env.prs = [lane_pull()]
    env.impl = {"number": IMPL_PR, "merged": True, "head": {"sha": "e" * 40}}
    put(env, doc())
    run(env)
    assert meeting(env) is None


def test_moved_lane_head_posts_nothing(env):
    new = "f" * 40
    env.prs = [lane_pull(sha=new)]
    put(env, doc())
    run(env)
    assert meeting(env, new) is None


@pytest.mark.parametrize("pull", [
    lane_pull(user="someone-else"),
    lane_pull(body="no header here"),
])
def test_refused_lane_shape_posts_nothing(env, pull):
    env.prs = [pull]
    put(env, doc())
    run(env)
    assert meeting(env) is None


def test_no_tests_marker_is_success(env):
    env.prs = [{"number": 3, "body": "docs", "user": {"login": "x"}, "base": {"ref": "main"},
                "head": {"sha": "9" * 40, "ref": "docs/x"}}]
    env.files[3] = ["docs/a.md"]
    run(env)
    assert meeting(env, "9" * 40) == ("success", deps.NOT_LANE)


def test_changed_file_under_tests_lane_is_a_marker(env):
    env.prs = [{"number": 3, "body": "docs", "user": {"login": "x"}, "base": {"ref": "main"},
                "head": {"sha": "9" * 40, "ref": "feat/x"}}]
    env.files[3] = ["tests/lane/test_a.py"]
    run(env)
    assert meeting(env, "9" * 40) is None


def test_unreadable_document_posts_nothing_and_logs_once(env):
    env.prs = [lane_pull()]
    p = put(env, doc())
    p.write_text("{not json")
    env.out.readouterr()
    run(env)
    assert meeting(env) is None
    err = env.out.readouterr().err
    assert err.count("unreadable or invalid") == 1 and str(p) in err


def test_second_pass_posts_nothing(env):
    env.prs = [lane_pull()]
    put(env, doc())
    run(env)
    n = len(env.posts)
    run(env)
    assert len(env.posts) == n


def test_implementation_is_read_once_per_pass(env):
    env.prs = [lane_pull(), lane_pull(number=9, sha="d" * 40)]
    put(env, doc())
    run(env)
    assert env.impl_reads == 1
