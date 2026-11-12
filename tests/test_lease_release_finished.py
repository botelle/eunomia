"""Plan 0179 D2/D4 (ADR-0029) — a finished plan's orphaned dispatch lease is
released by the sweep, never on liveness alone, and never a lease nothing
proves is still orphaned."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_watch import _load  # noqa: E402

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "operator/eunomia"


@pytest.fixture
def mod(tmp_path, monkeypatch):
    m = _load(tmp_path, FLEET_IMPLEMENTER_UIDS="42")
    monkeypatch.setattr(m, "repos", lambda: [REPO])
    pages = []
    monkeypatch.setattr(m, "_notify_once", lambda *a, **k: (pages.append(a), True)[1])
    m._test_pages = pages
    return m


def _write_lease(mod, lease_id, repo, note, created="2000-01-01T00:00:00Z",
                 holder="dead-session"):
    mod.lib.write_lease({
        "id": lease_id,
        "resource": {"type": "branch", "repo": repo, "branch": "feat/x"},
        "holder": holder,
        "granted_by": "watch-test",
        "state": "assigned",
        "ttl_minutes": 60,
        "created": created,
        "activated": None,
        "note": note,
    })


def _plan_files(mod, monkeypatch, status):
    monkeypatch.setattr(mod, "plan_files", lambda r, t: [
        (f"plans/{p}.md", {"id": p, "status": s}) for p, s in status.items()])


def _lease_rec(mod, lease_id):
    return json.loads((mod.lib.leases_dir() / f"{lease_id}.json").read_text())


def _events(mod):
    path = mod.lib.fleet_dir() / "events.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def _write_resume_request(mod, lease_id, reason="question-answered", consumed=False):
    con = sqlite3.connect(str(mod.lib.fleet_dir() / "fleet.db"))
    con.execute("""CREATE TABLE IF NOT EXISTS resume_request (
        plan TEXT NOT NULL, lease TEXT NOT NULL, pr INTEGER, head TEXT,
        requested_at TEXT NOT NULL, consumed_at TEXT, reason TEXT,
        comment_id INTEGER)""")
    con.execute(
        "INSERT INTO resume_request (plan, lease, pr, head, requested_at, "
        "consumed_at, reason, comment_id) VALUES (?,?,?,?,?,?,?,?)",
        ("a", lease_id, None, None, mod.lib.ts(mod.lib.now_utc()),
         mod.lib.ts(mod.lib.now_utc()) if consumed else None, reason, 1))
    con.commit()
    con.close()


def _write_question(mod, lease_id, n=1):
    import time
    path = mod.lib.fleet_dir() / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": mod.lib.ts(mod.lib.now_utc()), "type": "plan-question",
                       "actor": "orchestrator", "repo": REPO, "pr": None,
                       "lease": None,
                       "detail": {"plan": "a", "question_id": f"q-{lease_id}-{n}",
                                  "section": "x", "question": "y",
                                  "blocking": True, "where": {"pr": 1}}})
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------- finished plans
def test_done_and_abandoned_leases_release_with_their_reason(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "done", "b": "abandoned"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a")
    _write_lease(mod, "branch--b--001", REPO, "plan:b")
    assert mod.orphan_sweep("t", 0) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "released"
    assert _lease_rec(mod, "branch--b--001")["state"] == "released"
    reasons = {e["lease"]: e["detail"]["reason"] for e in _events(mod)
              if e.get("type") == "lease-released"}
    assert reasons == {"branch--a--001": "plan-done", "branch--b--001": "plan-abandoned"}
    assert mod._test_pages == [], "no pending question or answer: no page"


def test_an_unanswered_question_still_releases_and_pages_once(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "done"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a")
    _write_question(mod, "branch--a--001")
    assert mod.orphan_sweep("t", 0) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "released"
    assert len(mod._test_pages) == 1
    body = mod._test_pages[0][2]
    assert "pending on a finished plan" in body and "reopen" not in body
    assert "set the plan back to ready" in body
    # nothing resumes: no new dispatch leases were assigned for this plan
    assert mod.dispatch_leases() == {}


def test_an_unconsumed_resume_request_still_releases_and_pages_once(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "done"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a")
    _write_resume_request(mod, "branch--a--001")
    assert mod.orphan_sweep("t", 0) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "released"
    assert len(mod._test_pages) == 1
    assert "pending on a finished plan" in mod._test_pages[0][2]
    # the row is read, never consumed or acted on
    con = sqlite3.connect(str(mod.lib.fleet_dir() / "fleet.db"))
    row = con.execute("SELECT consumed_at FROM resume_request WHERE "
                      "lease=?", ("branch--a--001",)).fetchone()
    con.close()
    assert row[0] is None


def test_a_consumed_resume_request_pages_nothing(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "done"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a")
    _write_resume_request(mod, "branch--a--001", consumed=True)
    assert mod.orphan_sweep("t", 0) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "released"
    assert mod._test_pages == []


# --------------------------------------------------------------- ready plans
def test_a_live_lease_of_a_done_plan_is_not_released(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "done"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a",
                created=mod.lib.ts(mod.lib.now_utc()))
    monkeypatch.setattr(mod.lib, "liveness", lambda rec: mod.lib.now_utc())
    rec = _lease_rec(mod, "branch--a--001")
    rec["state"] = "active"
    rec["ttl_minutes"] = 60
    mod.lib.write_lease(rec)
    assert mod.orphan_sweep("t", 0) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "active"


def test_a_ready_plans_orphaned_lease_is_never_released_by_this_path(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "ready"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a")
    assert mod.orphan_sweep("t", 0) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "assigned"


def test_an_unreadable_status_is_never_treated_as_finished(mod, monkeypatch):
    monkeypatch.setattr(mod, "plan_files", lambda r, t: None)
    _write_lease(mod, "branch--a--001", REPO, "plan:a")
    assert mod.orphan_sweep("t", 0) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "assigned"


def test_a_delisted_repos_lease_is_never_read_or_released(mod, monkeypatch):
    monkeypatch.setattr(mod, "repos", lambda: ["operator/other"])
    _plan_files(mod, monkeypatch, {"a": "done"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a")
    read = []
    real = mod._finished_status
    monkeypatch.setattr(mod, "_finished_status",
                        lambda *a, **k: (read.append(a), real(*a, **k))[1])
    assert mod.orphan_sweep("t", 0) == 0
    assert read == []
    assert _lease_rec(mod, "branch--a--001")["state"] == "assigned"


def test_dry_run_releases_nothing_and_prints_the_line(mod, monkeypatch, capsys):
    _plan_files(mod, monkeypatch, {"a": "done"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a")
    assert mod.orphan_sweep("t", 0, dry_run=True) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "assigned"
    out = capsys.readouterr().out
    assert "DRY-RUN would release branch--a--001 (plan-done)" in out


def test_a_release_that_fails_pages_and_counts_it(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "done"})
    _write_lease(mod, "branch--a--001", REPO, "plan:a")

    def boom(argv, **k):
        class R:
            returncode = 1
            stderr = "boom"
            stdout = ""
        return R()
    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod.orphan_sweep("t", 0) == 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "assigned"
    assert any("would not release" in p[1] for p in mod._test_pages)


# ------------------------------------------------------------------ lanes
def test_a_lane_lease_releases_only_after_its_death_is_recorded(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "done"})
    _write_lease(mod, "lane--a-x--001", REPO, "lane:a/x")
    # first sweep: records the death, no release yet
    assert mod.lane_sweep("t") == 0
    assert _lease_rec(mod, "lane--a-x--001")["state"] == "assigned"
    assert any(e.get("type") == "lane-ended" for e in _events(mod))
    # second sweep: the death is already recorded, so it releases now
    assert mod.lane_sweep("t") == 1
    assert _lease_rec(mod, "lane--a-x--001")["state"] == "released"


def test_a_ready_plans_lane_lease_is_never_released(mod, monkeypatch):
    _plan_files(mod, monkeypatch, {"a": "ready"})
    _write_lease(mod, "lane--a-x--001", REPO, "lane:a/x")
    mod.lane_sweep("t")
    mod.lane_sweep("t")
    assert _lease_rec(mod, "lane--a-x--001")["state"] == "assigned"


# -------------------------------------------------------------- fleet-release
def _release_argv(lease_id, *extra):
    return [sys.executable, str(BIN / "fleet-release"), lease_id, *extra]


def test_reason_is_recorded_with_and_without_force(mod):
    _write_lease(mod, "branch--a--001", REPO, "plan:a", holder="EUNOMIA_SESSION")
    import os
    env = dict(os.environ, EUNOMIA_SESSION="EUNOMIA_SESSION",
              EUNOMIA_FLEET_DIR=str(mod.lib.fleet_dir()))
    r = subprocess.run(_release_argv("branch--a--001", "--reason", "plan-done"),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert _lease_rec(mod, "branch--a--001")["state"] == "released"
    ev = [e for e in _events(mod) if e.get("type") == "lease-released"][0]
    assert ev["detail"] == {"reason": "plan-done"}

    _write_lease(mod, "branch--b--001", REPO, "plan:b", holder="somebody-else")
    r = subprocess.run(_release_argv("branch--b--001", "--force", "--reason",
                                     "plan-abandoned"),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    ev = [e for e in _events(mod) if e.get("type") == "lease-released"
         and e["lease"] == "branch--b--001"][0]
    assert ev["detail"] == {"forced": True, "holder_was": "somebody-else",
                            "reason": "plan-abandoned"}


def test_a_bad_reason_token_exits_2_and_writes_nothing(mod):
    _write_lease(mod, "branch--a--001", REPO, "plan:a", holder="EUNOMIA_SESSION")
    import os
    env = dict(os.environ, EUNOMIA_SESSION="EUNOMIA_SESSION",
              EUNOMIA_FLEET_DIR=str(mod.lib.fleet_dir()))
    for bad in ("pr-closed", "plan-done; rm -rf", "secretsqueezedin1234567890"):
        r = subprocess.run(_release_argv("branch--a--001", "--reason", bad),
                           capture_output=True, text=True, env=env)
        assert r.returncode == 2, r.stderr
    assert _lease_rec(mod, "branch--a--001")["state"] == "assigned"
    assert _events(mod) == []


def test_expect_orphaned_refuses_a_lease_that_recovered(mod):
    _write_lease(mod, "branch--a--001", REPO, "plan:a", holder="dead-session")
    import os
    env = dict(os.environ, EUNOMIA_SESSION="watcher",
              EUNOMIA_FLEET_DIR=str(mod.lib.fleet_dir()))
    # the heartbeat recovered: touch it fresh and mark the lease active
    rec = _lease_rec(mod, "branch--a--001")
    rec["state"] = "active"
    rec["ttl_minutes"] = 60
    mod.lib.write_lease(rec)
    mod.lib.touch_heartbeat("dead-session")
    r = subprocess.run(_release_argv("branch--a--001", "--force",
                                     "--expect-orphaned", "dead-session"),
                       capture_output=True, text=True, env=env)
    assert r.returncode != 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "active"
    assert _events(mod) == []


def test_expect_orphaned_refuses_a_lease_taken_over(mod):
    _write_lease(mod, "branch--a--001", REPO, "plan:a", holder="dead-session")
    import os
    env = dict(os.environ, EUNOMIA_SESSION="watcher",
              EUNOMIA_FLEET_DIR=str(mod.lib.fleet_dir()))
    r = subprocess.run(_release_argv("branch--a--001", "--force",
                                     "--expect-orphaned", "somebody-else"),
                       capture_output=True, text=True, env=env)
    assert r.returncode != 0
    assert _lease_rec(mod, "branch--a--001")["state"] == "assigned"
    assert _events(mod) == []
