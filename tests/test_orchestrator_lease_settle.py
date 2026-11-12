"""Plan 0179 D1 — the orchestrator settles its own lease on every exit.

`settle_lease()` is exercised directly (forge and `release_lease` stubbed) for
the pure status-reading decisions, and through a full `main()` run (the real
fake-`claude` harness from test_orchestrator_session) for the question of
WHEN it runs relative to every other exit path."""
import base64
import json

import test_orchestrator_session as S

LEASE = S.LEASE
REPO = "operator/demo"
PLAN_ID = "0042-a-feature"
PLAN_PATH = "plans/0042-a-feature.md"

GOOD_Q = {"section": "3. Boundaries", "question": "Which table holds X?",
         "why": "two tables fit", "blocking": True, "options": ["a", "b"]}


def _block(obj):
    return "```plan-question\n" + json.dumps(obj) + "\n```"


class _Log:
    def __init__(self):
        self.notes = []

    def note(self, phase, **facts):
        self.notes.append((phase, facts))


class _Forge:
    """Answers `get_contents` for the plan's status at a ref — the only forge
    verb `settle_lease()` itself ever calls (plan 0179 D1: it never reads a
    pull request) — plus the few verbs a posted `plan-question` needs."""

    def __init__(self, contents=None, pulls=None):
        self.contents = contents or {}
        self.pulls = pulls or []
        self.comments = []
        self.issues = []

    def get_contents(self, repo, path, ref=None):
        body = self.contents.get(ref)
        if body is None:
            return 404, None
        return 200, {"content": base64.b64encode(body.encode()).decode()}

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        return (200, self.pulls if page == 1 else [])

    def get_pull_by_branch(self, repo, base, head):
        for pr in self.pulls:
            if (((pr.get("head") or {}).get("ref") or "") == head
                    and ((pr.get("base") or {}).get("ref") or "") == base):
                return 200, pr
        return 404, None

    def get_pull(self, repo, n):
        for pr in self.pulls:
            if pr.get("number") == n:
                return 200, pr
        return 404, None

    def create_issue_comment(self, repo, n, body):
        self.comments.append((n, body))
        return 201, {}

    def _request(self, method, path, query=None, body=None):
        assert method == "POST" and path.endswith("/issues")
        self.issues.append(body)
        return 201, {"number": 31}


def _status_text(status):
    return S.PLAN.replace("status: ready", f"status: {status}")


def _wire_forge(mod, contents, pulls=None):
    fmod = mod._load_sibling("fleetforge.py", "_fleetforge")
    forge = _Forge(contents, pulls)
    mod._forge = lambda: (fmod, forge)
    return forge


def _rec(mod, lease_id=LEASE):
    return json.loads((mod.lib.leases_dir() / f"{lease_id}.json").read_text())


def _log_lines(mod, lease_id=LEASE):
    path = mod.lib.work_root() / "logs" / f"{lease_id}.log"
    return path.read_text().splitlines() if path.exists() else []


def _lease_events(mod, lease_id=LEASE):
    path = mod.lib.fleet_dir() / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        ev = json.loads(line)
        if ev.get("lease") == lease_id:
            out.append(ev)
    return out


# -------------------------------------------- settle_lease(), called directly
def _settle(mod, status="ready", pending=False, released_first=False):
    log = _Log()
    _wire_forge(mod, {"main": _status_text(status)})
    S._lease(mod, state="released" if released_first else "active")
    calls = []
    real_release = mod.release_lease

    def release(lid, reason=None):
        calls.append((lid, reason))
        return real_release(lid, reason=reason)
    mod.release_lease = release
    mod.settle_lease(REPO, PLAN_ID, PLAN_PATH, LEASE, log, pending=pending)
    return _rec(mod), log, calls


def test_a_ready_plan_holds(tmp_path):
    mod = S._mod(tmp_path)
    rec, log, calls = _settle(mod, status="ready")
    assert rec["state"] == "active"
    assert calls == []
    assert ("lease-settled", {"released": 0, "held": "ready"}) in log.notes


def test_an_unreadable_plan_holds(tmp_path):
    mod = S._mod(tmp_path)
    log = _Log()
    _wire_forge(mod, {})          # no "main" entry: get_contents 404s
    S._lease(mod, state="active")
    mod.settle_lease(REPO, PLAN_ID, PLAN_PATH, LEASE, log)
    assert _rec(mod)["state"] == "active"
    assert ("lease-settled", {"released": 0, "held": "unread"}) in log.notes


def test_an_abandoned_plan_releases_with_its_reason(tmp_path):
    mod = S._mod(tmp_path)
    rec, log, calls = _settle(mod, status="abandoned")
    assert rec["state"] == "released"
    assert calls == [(LEASE, "plan-abandoned")]
    assert ("lease-settled", {"released": 1, "reason": "plan-abandoned"}) in log.notes


def test_a_done_plan_releases_with_its_reason(tmp_path):
    mod = S._mod(tmp_path)
    rec, log, calls = _settle(mod, status="done")
    assert rec["state"] == "released"
    assert calls == [(LEASE, "plan-done")]
    assert ("lease-settled", {"released": 1, "reason": "plan-done"}) in log.notes


def test_pending_still_releases_and_pages_once(tmp_path):
    mod = S._mod(tmp_path)
    pages = []
    mod.notify = lambda t, b: (pages.append((t, b)), True)[1]
    rec, log, calls = _settle(mod, status="done", pending=True)
    assert rec["state"] == "released"
    assert len(pages) == 1
    assert "pending on a finished plan" in pages[0][1]
    assert "set the plan back to ready" in pages[0][1]


def test_already_released_makes_no_second_call(tmp_path):
    """The steward's own end: settle_lease() sees `released` and stops there."""
    mod = S._mod(tmp_path)
    rec, log, calls = _settle(mod, status="done", released_first=True)
    assert rec["state"] == "released"
    assert calls == []
    assert log.notes == []


# --------------------------------------------- full main() runs: WHEN it runs
def test_a_failure_with_no_marked_pr_holds_as_ready(tmp_path):
    binp = S._fake_claude(tmp_path, "import sys; sys.stdin.read()")
    mod = S._mod(tmp_path, path_extra=str(binp))
    _wire_forge(mod, {"main": S.PLAN})
    S._wire(mod, verified=(False, "no pull request with head feat/0042-a-feature",
                           None))
    S._lease(mod)
    assert S._run(mod) == 1
    assert _rec(mod)["state"] == "active"
    assert any("lease-settled released=0 held=ready" in l for l in _log_lines(mod))


def test_a_failure_on_a_closed_pr_holds_as_ready(tmp_path):
    binp = S._fake_claude(tmp_path, "import sys; sys.stdin.read()")
    mod = S._mod(tmp_path, path_extra=str(binp))
    _wire_forge(mod, {"main": S.PLAN})
    S._wire(mod, verified=(False, "#9 carries no 'Plan: 0042-a-feature' line", 9))
    S._lease(mod)
    assert S._run(mod) == 1
    assert _rec(mod)["state"] == "active"
    assert any("lease-settled released=0 held=ready" in l for l in _log_lines(mod))


def test_a_review_stopped_on_quota_with_the_pr_open_holds_as_ready(tmp_path):
    binp = S._fake_claude(tmp_path, "import sys; sys.stdin.read()")
    mod = S._mod(tmp_path, path_extra=str(binp))
    _wire_forge(mod, {"main": S.PLAN})
    S._wire(mod, verified=(True, "", 7),
           review=(False, "review stopped: usage limit", []))
    S._lease(mod)
    assert S._run(mod) == 1
    assert _rec(mod)["state"] == "active"
    assert any("lease-settled released=0 held=ready" in l for l in _log_lines(mod))


def test_an_exception_inside_the_work_is_paged_then_settled(tmp_path):
    binp = S._fake_claude(tmp_path, "import sys; sys.stdin.read()")
    mod = S._mod(tmp_path, path_extra=str(binp))
    _wire_forge(mod, {"main": S.PLAN})
    calls = S._wire(mod)

    def boom(*a, **k):
        raise RuntimeError("forge exploded")
    mod.verify_marked_pr = boom
    S._lease(mod)
    assert S._run(mod) == 1
    assert [e[0] for e in calls["emit"]] == ["plan-failed"]
    assert len(calls["notify"]) == 1
    assert _rec(mod)["state"] == "active"
    assert any("lease-settled released=0 held=ready" in l for l in _log_lines(mod))


def test_activation_failure_never_settles(tmp_path):
    binp = S._fake_claude(tmp_path, "import sys; sys.stdin.read()")
    mod = S._mod(tmp_path, path_extra=str(binp))
    S._wire(mod)
    mod.activate = lambda lid: (False, "boom")
    settled = []
    real_settle = mod.settle_lease
    mod.settle_lease = lambda *a, **k: (settled.append(1), real_settle(*a, **k))[1]
    S._lease(mod)
    assert S._run(mod) == 2
    assert settled == []


def test_a_posted_question_on_a_done_plan_releases_and_pages_once(tmp_path):
    rec_result = json.dumps({"type": "result", "is_error": False,
                             "result": "pushed.\n" + _block(GOOD_Q)})
    binp = S._fake_claude(
        tmp_path, "import sys\nsys.stdin.read()\nprint(%r)\n" % rec_result)
    mod = S._mod(tmp_path, path_extra=str(binp))
    calls = S._wire(mod)
    pages = []
    mod.notify = lambda t, b: (pages.append((t, b)), True)[1]
    forge = _wire_forge(mod, {"main": S.DONE}, pulls=[S._pull(n=5)])
    S._lease(mod)
    assert S._run(mod) == 1
    assert [e[0] for e in calls["emit"]] == ["plan-question"]
    assert calls["comment"], "the question was posted on the marked PR"
    assert len(pages) == 1
    assert "pending on a finished plan" in pages[0][1]
    assert "set the plan back to ready" in pages[0][1]
    assert _rec(mod)["state"] == "released"
    released = [e for e in _lease_events(mod) if e.get("type") == "lease-released"]
    assert len(released) == 1
    assert (released[0].get("detail") or {}).get("reason") == "plan-done"
