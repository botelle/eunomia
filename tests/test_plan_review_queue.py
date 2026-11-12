"""bin/fleet-plan-review (plan 0188): a plan-filing PR's review survives the
session that filed it.

A fake forge (a JSON file two independent processes — this test and a fake
dispatcher script — read and write) and a fake dispatcher stand in for the
real Forgejo and `~/agent/dispatch-review.py`, through the exact seams the
real ones are reached by: `fleetforge.Forge`'s method surface for the forge,
and `FLEET_REVIEW_CMD` (`orchestrator.review_command()`'s own override) for
the dispatcher.
"""
import importlib.machinery
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


fpr = _load("fleet_plan_review", BIN / "fleet-plan-review")


# --------------------------------------------------------------------------
# The fake forge: a JSON file, so the in-process test and the subprocess fake
# dispatcher can agree on what the forge holds without either one speaking
# HTTP.
# --------------------------------------------------------------------------

class FakeForge:
    def __init__(self, state_path):
        self.state_path = Path(state_path)
        if not self.state_path.exists():
            self._write({"pulls": {}, "files": {}, "ci": {}, "reviews": {}})

    def _read(self):
        return json.loads(self.state_path.read_text())

    def _write(self, state):
        self.state_path.write_text(json.dumps(state))

    def set_pull(self, repo, pr, head_sha, state="open", draft=False, merged=False):
        s = self._read()
        s["pulls"][f"{repo}#{pr}"] = {
            "number": pr, "state": state, "draft": draft, "merged": merged,
            "head": {"sha": head_sha}}
        self._write(s)

    def set_files(self, repo, pr, files):
        s = self._read()
        s["files"][f"{repo}#{pr}"] = files
        self._write(s)

    def set_ci(self, repo, sha, statuses, state=""):
        s = self._read()
        s["ci"][f"{repo}@{sha}"] = {"state": state, "statuses": statuses}
        self._write(s)

    def add_review(self, repo, pr, commit_id, login="revbot", review_state="APPROVED"):
        s = self._read()
        key = f"{repo}#{pr}"
        s["reviews"].setdefault(key, []).append(
            {"id": len(s["reviews"].get(key, [])) + 1, "state": review_state,
             "commit_id": commit_id, "stale": False, "dismissed": False,
             "user": {"login": login}})
        self._write(s)

    # ---- fleetforge.Forge's surface, the subset this module calls --------
    def get_pull(self, repo, pr):
        body = self._read()["pulls"].get(f"{repo}#{pr}")
        return (200, body) if body is not None else (404, None)

    def list_pull_files(self, repo, pr, page_cap=20, limit=50):
        body = self._read()["files"].get(f"{repo}#{pr}")
        return (200, body) if body is not None else (0, None)

    def get_commit_status(self, repo, sha):
        # A missing entry means "no CI configured at all" — the forge answers
        # 200 with an empty statuses list, never a transport failure. Tests
        # that need an UNREADABLE commit status call `set_unreadable_ci`.
        body = self._read()["ci"].get(f"{repo}@{sha}", {"state": "", "statuses": []})
        if body == "unreadable":
            return 0, None
        return 200, body

    def set_unreadable_ci(self, repo, sha):
        s = self._read()
        s["ci"][f"{repo}@{sha}"] = "unreadable"
        self._write(s)

    def list_reviews(self, repo, pr, page=1, limit=50):
        body = self._read()["reviews"].get(f"{repo}#{pr}", [])
        return (200, body) if page == 1 else (200, [])


FAKE_DISPATCHER = """\
import json, os, sqlite3, sys
state_path = os.environ["FAKE_FORGE_STATE"]
counter_path = os.environ["FAKE_DISPATCH_COUNTER"]
with open(counter_path, "a") as f:
    f.write("x")
mode = os.environ.get("FAKE_DISPATCH_MODE", "post")
repo, pr = sys.argv[1], sys.argv[2]
key = f"{repo}#{pr}"
if mode == "fail":
    sys.stderr.write(os.environ.get("FAKE_DISPATCH_FAIL_MSG", "FATAL: fake failure") + "\\n")
    outcome = os.environ.get("FAKE_DISPATCH_RECORD_OUTCOME")
    if outcome:
        with open(state_path) as f:
            head = json.load(f)["pulls"][key]["head"]["sha"]
        c = sqlite3.connect(os.environ["FAKE_DISPATCH_DB"])
        c.execute("CREATE TABLE IF NOT EXISTS review_attempt (repo TEXT, pr INTEGER, "
                 "head_sha TEXT, model TEXT, outcome TEXT, session_uuid TEXT, "
                 "started_at TEXT, stopped_at TEXT, ordinal INTEGER, position TEXT)")
        c.execute("INSERT INTO review_attempt (repo, pr, head_sha, outcome, ordinal) "
                 "VALUES (?,?,?,?,1)",
                 (repo, int(pr), head, outcome))
        c.commit()
    sys.exit(1)
with open(state_path) as f:
    state = json.load(f)
if mode == "post":
    head = state["pulls"][key]["head"]["sha"]
    state.setdefault("reviews", {}).setdefault(key, []).append(
        {"id": len(state["reviews"].get(key, [])) + 1, "state": "APPROVED",
         "commit_id": head, "stale": False, "dismissed": False,
         "user": {"login": "revbot"}})
    with open(state_path, "w") as f:
        json.dump(state, f)
elif mode == "post_accounts":
    # talos's own per-ordinal idempotence (`standing_ordinals()`): posts only
    # the accounts named in FAKE_DISPATCH_ACCOUNTS that do NOT already have a
    # live review at this head, same as a real round completing a partial one.
    head = state["pulls"][key]["head"]["sha"]
    existing = {r.get("user", {}).get("login", "").lower()
               for r in state.get("reviews", {}).get(key, [])
               if r.get("commit_id") == head}
    for account in os.environ.get("FAKE_DISPATCH_ACCOUNTS", "revbot").split(","):
        if account.lower() in existing:
            continue
        state.setdefault("reviews", {}).setdefault(key, []).append(
            {"id": len(state["reviews"].get(key, [])) + 1, "state": "APPROVED",
             "commit_id": head, "stale": False, "dismissed": False,
             "user": {"login": account}})
    with open(state_path, "w") as f:
        json.dump(state, f)
sys.exit(0)
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet"))
    state_path = tmp_path / "forge.json"
    forge = FakeForge(state_path)
    monkeypatch.setattr(fpr, "_forge", lambda: forge)
    monkeypatch.setattr(fpr, "_FORGE", forge)
    repos_conf = tmp_path / "repos.conf"
    repos_conf.write_text("repo: operator/x\n")
    monkeypatch.setattr(fpr.fleet_repo, "CONF", repos_conf)

    script = tmp_path / "fake_dispatch.py"
    script.write_text(FAKE_DISPATCHER)
    counter = tmp_path / "dispatch_count.txt"
    counter.write_text("")
    monkeypatch.setenv("FLEET_REVIEW_CMD", f"{sys.executable} {script}")
    monkeypatch.setenv("FAKE_FORGE_STATE", str(state_path))
    monkeypatch.setenv("FAKE_DISPATCH_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_DISPATCH_MODE", "post")

    db = tmp_path / "fleet.db"

    class W:
        pass

    w = W()
    w.forge, w.db, w.counter, w.tmp_path = forge, db, counter, tmp_path

    def connect():
        return fpr._connect(str(db))

    def dispatch_count():
        return len(counter.read_text())

    def set_ordinals(con, repo, ordinals):
        """Configure `repo`'s `plan_reviewer` seats the way `fleet-models set`
        would — the minimal slice of its own `repo_model` DDL this worker's
        `active_ordinals()` reads, not the full schema (no `runner` validation
        here, this is a test of ordinal RESOLUTION, not of fleet-models
        itself)."""
        con.execute(
            "CREATE TABLE IF NOT EXISTS repo_model (repo TEXT, position TEXT, "
            "ordinal INTEGER, runner TEXT, disabled INTEGER DEFAULT 0)")
        for ordinal in ordinals:
            con.execute(
                "INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES (?, 'plan_reviewer', ?, 'sub-codex', 0)", (repo, ordinal))
        con.commit()

    monkeypatch.setenv("FAKE_DISPATCH_DB", str(db))
    w.connect, w.dispatch_count, w.set_ordinals = connect, dispatch_count, set_ordinals
    return w


def _enqueue(con, repo, pr, head):
    job_id, _created = fpr._enqueue(con, repo, pr, head)
    return job_id


def _claim_and_run(con, job_id, holder="w1"):
    row = con.execute(
        "SELECT id, repo, pr, head_sha, state, reason, attempts, claimed_by, "
        "claimed_at FROM plan_review_job WHERE id=?", (job_id,)).fetchone()
    job = dict(zip(("id", "repo", "pr", "head_sha", "state", "reason", "attempts",
                    "claimed_by", "claimed_at"), row))
    assert fpr._transition(con, job_id, "claimed", actor=holder, action="claim",
                           claim=True, holder=holder, expect_states=fpr.OPEN_STATES,
                           claim_cutoff=0)
    job["state"], job["claimed_by"] = "claimed", holder
    fpr._dispatch_one(con, job, holder)


def _state_of(con, job_id):
    return con.execute("SELECT state, reason, attempts FROM plan_review_job WHERE id=?",
                       (job_id,)).fetchone()


# --------------------------------------------------------------------------
# The filing classifier: agreement with talos's reviewer_position on the
# four shapes of a changed-file list (plan-only, mixed, renamed, unreadable).
# --------------------------------------------------------------------------

def test_plan_only_file_list_resolves_plan_reviewer(world):
    world.forge.set_files("operator/x", 1, [
        {"filename": "plans/0001-x.md", "status": "added"},
        {"filename": "docs/adr/0001-x.md", "status": "added"}])
    paths, complete, reason = fpr.changed_paths_for_position(world.forge, "operator/x", 1)
    assert complete and reason is None
    assert fpr.reviewer_position(paths) == "plan_reviewer"


def test_mixed_file_list_resolves_code_reviewer(world):
    world.forge.set_files("operator/x", 2, [
        {"filename": "plans/0001-x.md", "status": "added"},
        {"filename": "bin/fleet-thing", "status": "modified"}])
    paths, complete, reason = fpr.changed_paths_for_position(world.forge, "operator/x", 2)
    assert complete
    assert fpr.reviewer_position(paths) == "code_reviewer"


def test_renamed_into_prose_is_judged_by_its_old_path(world):
    # a file moved OUT of plans/docs is still judged by its old, prose path:
    # the pair stays plan-only.
    world.forge.set_files("operator/x", 3, [
        {"filename": "plans/0002-new.md", "previous_filename": "docs/old-notes.md",
         "status": "renamed"}])
    paths, complete, _ = fpr.changed_paths_for_position(world.forge, "operator/x", 3)
    assert complete
    assert fpr.reviewer_position(paths) == "plan_reviewer"

    # a file moved INTO docs/ from a non-prose path is judged by what it USED
    # TO BE: the pair is mixed, not plan-only, even though the new name alone
    # would pass.
    world.forge.set_files("operator/x", 4, [
        {"filename": "docs/new.md", "previous_filename": "bin/old.py",
         "status": "renamed"}])
    paths, complete, _ = fpr.changed_paths_for_position(world.forge, "operator/x", 4)
    assert complete
    assert fpr.reviewer_position(paths) == "code_reviewer"


def test_unreadable_file_list_is_incomplete_never_plan_reviewer(world):
    # no `set_files` call at all: the forge has nothing for PR 5, so
    # list_pull_files answers (0, None) exactly as a transport failure would.
    paths, complete, reason = fpr.changed_paths_for_position(world.forge, "operator/x", 5)
    assert not complete
    assert reason is not None
    # main() in dispatch-review.py resolves code_reviewer whenever the list is
    # incomplete, never plan_reviewer trusted on a partial read — the same
    # rule this worker's own _dispatch_one applies (holds rather than skips).


# --------------------------------------------------------------------------
# Enqueue: idempotent on (repo, pr, head_sha), and the DB enforces it too.
# --------------------------------------------------------------------------

def test_enqueue_is_idempotent_and_db_enforced(world):
    con = world.connect()
    id1, created1 = fpr._enqueue(con, "operator/x", 10, "a" * 40)
    id2, created2 = fpr._enqueue(con, "operator/x", 10, "a" * 40)
    assert id1 == id2
    assert created1 and not created2
    n = con.execute("SELECT COUNT(*) FROM plan_review_job WHERE repo=? AND pr=?",
                    ("operator/x", 10)).fetchone()[0]
    assert n == 1
    # the uniqueness is a DB constraint, not only this function's own check —
    # a direct INSERT bypassing _enqueue is refused too (D4's "plant removal
    # of the uniqueness check" proven the other direction: this is the check).
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO plan_review_job (id, repo, pr, head_sha, state, "
            "attempts, created_at, updated_at) VALUES ('other','operator/x',10,"
            "?,'queued',0,0,0)", ("a" * 40,))


# --------------------------------------------------------------------------
# CI: pending holds, then one launch once it goes green. Covers "enqueue
# twice during CI pending, exit the caller, restart the worker, then finish
# CI and observe one launch".
# --------------------------------------------------------------------------

def test_pending_ci_holds_then_one_launch_once_green(world):
    repo, pr, sha = "operator/x", 20, "b" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0020-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "pending"}])

    con = world.connect()
    job_id, _ = fpr._enqueue(con, repo, pr, sha)
    fpr._enqueue(con, repo, pr, sha)        # filed twice: still one row
    con.close()

    # "restart the worker": a fresh connection, exactly as a new process would open.
    con = world.connect()
    jobs = fpr._claim_jobs(con, "w1", limit=5, claim_ttl=1800)
    assert len(jobs) == 1
    fpr._dispatch_one(con, jobs[0], "w1")
    state, reason, _ = _state_of(con, job_id)
    assert state == "held" and "running" in reason
    assert world.dispatch_count() == 0
    con.close()

    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    con = world.connect()
    jobs = fpr._claim_jobs(con, "w1", limit=5, claim_ttl=1800)
    assert len(jobs) == 1
    fpr._dispatch_one(con, jobs[0], "w1")
    state, reason, _ = _state_of(con, job_id)
    assert state == "done"
    assert world.dispatch_count() == 1


def test_absent_ci_is_a_go(world):
    repo, pr, sha = "operator/x", 21, "c" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "docs/readme.md", "status": "added"}])
    # no set_ci call: the repo has no CI at all.
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, _reason, _ = _state_of(con, job_id)
    assert state == "done"
    assert world.dispatch_count() == 1


def test_red_ci_is_held_and_rechecked_on_a_later_observation(world):
    repo, pr, sha = "operator/x", 22, "d" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0022-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "failure"}])
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "held" and "red" in reason
    assert world.dispatch_count() == 0

    # CI reran green on the SAME head — this is a recheck, not a new job.
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    jobs = fpr._claim_jobs(con, "w1", limit=5, claim_ttl=1800)
    assert [j["id"] for j in jobs] == [job_id]
    fpr._dispatch_one(con, jobs[0], "w1")
    state, _reason, _ = _state_of(con, job_id)
    assert state == "done"
    assert world.dispatch_count() == 1


def test_eunomia_deps_failure_never_holds_a_plan_review(world):
    # ADR-0028 §4: "A plan PR is never held, because its edges gate dispatch,
    # not review." A plan filing whose named plan or depends_on is not yet on
    # main is the normal case, not a build break — real CI (`CI / tests`)
    # green plus `eunomia/deps=failure` must still dispatch.
    repo, pr, sha = "operator/x", 23, "e1" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0023-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [
        {"context": "CI / tests", "status": "success"},
        {"context": "eunomia/deps", "status": "failure"}])
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, _reason, _ = _state_of(con, job_id)
    assert state == "done"
    assert world.dispatch_count() == 1


def test_eunomia_deps_pending_alone_is_absent_ci_a_go(world):
    # eunomia/deps is the ONLY status posted yet (its own 60s poller fired
    # before the real build's). Excluding it must not fabricate a "pending"
    # from a status this worker never counts either way.
    repo, pr, sha = "operator/x", 24, "e2" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0024-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "eunomia/deps", "status": "success"}])
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, _reason, _ = _state_of(con, job_id)
    assert state == "done"
    assert world.dispatch_count() == 1


def test_eunomia_deps_does_not_mask_a_real_red_build(world):
    repo, pr, sha = "operator/x", 25, "e3" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0025-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [
        {"context": "CI / tests", "status": "failure"},
        {"context": "eunomia/deps", "status": "success"}])
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "held" and "red" in reason
    assert world.dispatch_count() == 0


# --------------------------------------------------------------------------
# Terminal skips.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field,value,fragment", [
    ("state", "closed", "closed"),
    ("draft", True, "draft"),
])
def test_closed_and_draft_prs_are_skipped(world, field, value, fragment):
    repo, pr, sha = "operator/x", 30, "e" * 40
    kwargs = {"state": "open", "draft": False}
    kwargs[field] = value
    world.forge.set_pull(repo, pr, sha, **kwargs)
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "skipped" and fragment in reason
    assert world.dispatch_count() == 0


def test_unenrolled_repo_is_skipped(world):
    repo, pr, sha = "operator/not-enrolled-anywhere", 31, "f" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0031-x.md", "status": "added"}])
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "skipped" and "enrolled" in reason
    assert world.dispatch_count() == 0


def test_mixed_change_is_skipped_as_not_a_plan_filing(world):
    repo, pr, sha = "operator/x", 32, "1" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0032-x.md", "status": "added"},
                                     {"filename": "bin/fleet-thing", "status": "modified"}])
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "skipped" and "not a plan filing" in reason
    assert world.dispatch_count() == 0


# --------------------------------------------------------------------------
# Head movement: supersede the old row, enqueue the new head.
# --------------------------------------------------------------------------

def test_head_change_supersedes_and_files_the_new_head(world):
    repo, pr = "operator/x", 40
    old_sha, new_sha = "2" * 40, "3" * 40
    world.forge.set_pull(repo, pr, old_sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0040-x.md", "status": "added"}])
    world.forge.set_ci(repo, old_sha, [{"context": "CI / tests", "status": "pending"}])
    con = world.connect()
    old_job = _enqueue(con, repo, pr, old_sha)

    world.forge.set_pull(repo, pr, new_sha)
    _claim_and_run(con, old_job)
    state, reason, _ = _state_of(con, old_job)
    assert state == "superseded" and new_sha[:8] in reason

    new_job = con.execute(
        "SELECT id FROM plan_review_job WHERE repo=? AND pr=? AND head_sha=?",
        (repo, pr, new_sha)).fetchone()
    assert new_job is not None
    assert world.dispatch_count() == 0


def test_supersede_enqueues_the_replacement_before_committing_the_old_terminal(world, monkeypatch):
    """A crash between the two writes must land on the side that loses no
    review work. Simulate it by making the `supersede` transition itself
    blow up, after `_enqueue`'s own commit has already happened, and show the
    replacement row survived — the fix this guards is the two calls' ORDER,
    so this breaks if they are ever swapped back."""
    repo, pr = "operator/x", 41
    old_sha, new_sha = "41" * 20, "42" * 20
    world.forge.set_pull(repo, pr, old_sha)
    con = world.connect()
    old_job = _enqueue(con, repo, pr, old_sha)

    world.forge.set_pull(repo, pr, new_sha)

    real_transition = fpr._transition

    def crash_on_supersede(con, job_id, state, **kwargs):
        if kwargs.get("action") == "supersede":
            raise RuntimeError("simulated crash before recording the supersede")
        return real_transition(con, job_id, state, **kwargs)

    row = con.execute(
        "SELECT id, repo, pr, head_sha, state, reason, attempts, claimed_by, "
        "claimed_at FROM plan_review_job WHERE id=?", (old_job,)).fetchone()
    job = dict(zip(("id", "repo", "pr", "head_sha", "state", "reason", "attempts",
                    "claimed_by", "claimed_at"), row))
    assert fpr._transition(con, old_job, "claimed", actor="w1", action="claim",
                           claim=True, holder="w1", expect_states=fpr.OPEN_STATES,
                           claim_cutoff=0)
    job["state"], job["claimed_by"] = "claimed", "w1"

    monkeypatch.setattr(fpr, "_transition", crash_on_supersede)
    with pytest.raises(RuntimeError):
        fpr._dispatch_one(con, job, "w1")

    new_row = con.execute(
        "SELECT state FROM plan_review_job WHERE repo=? AND pr=? AND head_sha=?",
        (repo, pr, new_sha)).fetchone()
    assert new_row is not None, (
        "the replacement row for the new head must exist even though the "
        "crash happened before the old row was marked superseded")
    assert new_row[0] == "queued"
    old_state = con.execute("SELECT state FROM plan_review_job WHERE id=?",
                            (old_job,)).fetchone()[0]
    assert old_state == "claimed"   # never reached 'superseded': the crash beat it there


def test_unreadable_head_sha_holds_rather_than_superseding(world):
    """A 200 pull body with no `head.sha` at all is unreadable, not a head
    move: superseding it would leave the row terminal with no replacement
    committed (the guard that files one only fires on a truthy new sha),
    silently dropping the review."""
    repo, pr, sha = "operator/x", 42, "43" * 20
    world.forge.set_pull(repo, pr, sha)
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    s = world.forge._read()
    s["pulls"][f"{repo}#{pr}"]["head"] = {}
    world.forge._write(s)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "held" and "unreadable" in reason
    assert world.dispatch_count() == 0


def test_completion_write_is_fenced_to_the_current_claim_holder(world):
    """D1/D3: a claim this worker lost to another worker's abandoned-claim
    recovery — however that happened, including a dispatch slower than
    `--claim-ttl` — must not have its outcome overwritten by the loser of
    that race. Simulate the steal by hand, between claim and completion, and
    show the original holder's own write is refused and the new holder's
    claim is left intact."""
    repo, pr, sha = "operator/x", 90, "90" * 20
    world.forge.set_pull(repo, pr, sha)
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    row = con.execute(
        "SELECT id, repo, pr, head_sha, state, reason, attempts, claimed_by, "
        "claimed_at FROM plan_review_job WHERE id=?", (job_id,)).fetchone()
    job = dict(zip(("id", "repo", "pr", "head_sha", "state", "reason", "attempts",
                    "claimed_by", "claimed_at"), row))
    assert fpr._transition(con, job_id, "claimed", actor="w1", action="claim",
                           claim=True, holder="w1", expect_states=fpr.OPEN_STATES,
                           claim_cutoff=0)

    # The steal: a second worker's abandoned-claim recovery reclaims the
    # SAME row while "w1" is (in this simulation) still mid-dispatch.
    assert fpr._transition(con, job_id, "claimed", actor="w2", action="claim",
                           claim=True, holder="w2", expect_states=("claimed",))

    # w1, unaware, finishes and tries to record its own result.
    assert not fpr._fenced(con, job, "w1", "done", "done", "late write")
    state, claimed_by = con.execute(
        "SELECT state, claimed_by FROM plan_review_job WHERE id=?", (job_id,)).fetchone()
    assert state == "claimed" and claimed_by == "w2"


def test_perma_held_jobs_do_not_starve_newer_ready_jobs(world):
    """Three jobs whose CI never turns green are reconfirmed `held` every
    sweep. A fourth, filed after all three, is ready right now. With a claim
    limit smaller than the stuck count, `created_at` ordering alone would
    hand the same oldest jobs the only slots forever and the ready job would
    never be claimed; `updated_at` ordering lets it through within one extra
    sweep, as soon as the stuck jobs it was filed behind have each been
    touched once."""
    repo = "operator/x"
    con = world.connect()

    stuck = []
    for i, pr in enumerate((80, 81, 82)):
        sha = f"{i}" * 40
        world.forge.set_pull(repo, pr, sha)
        world.forge.set_files(repo, pr, [{"filename": f"plans/00{pr}-x.md", "status": "added"}])
        world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "failure"}])
        job_id = _enqueue(con, repo, pr, sha)
        con.execute("UPDATE plan_review_job SET created_at=?, updated_at=? WHERE id=?",
                   (100.0 + i, 100.0 + i, job_id))
        stuck.append(job_id)
    con.commit()

    d_sha = "d" * 40
    world.forge.set_pull(repo, 83, d_sha)
    world.forge.set_files(repo, 83, [{"filename": "plans/0083-x.md", "status": "added"}])
    world.forge.set_ci(repo, d_sha, [{"context": "CI / tests", "status": "success"}])
    ready_job = _enqueue(con, repo, 83, d_sha)
    con.execute("UPDATE plan_review_job SET created_at=?, updated_at=? WHERE id=?",
               (200.0, 200.0, ready_job))
    con.commit()

    # Sweep 1, limit 2: the two never-touched, oldest jobs (A, B) win.
    jobs = fpr._claim_jobs(con, "w1", limit=2, claim_ttl=1800)
    assert [j["id"] for j in jobs] == stuck[:2]
    for job in jobs:
        fpr._dispatch_one(con, job, "w1")
    for job_id in stuck[:2]:
        assert _state_of(con, job_id)[0] == "held"
    assert _state_of(con, ready_job)[0] == "queued"   # not even claimed yet

    # Sweep 2, limit 2: A and B were just touched, so C (still untouched since
    # before sweep 1) and D (newer, also still untouched) take the slots —
    # and D, never held, finishes.
    jobs = fpr._claim_jobs(con, "w1", limit=2, claim_ttl=1800)
    assert sorted(j["id"] for j in jobs) == sorted([stuck[2], ready_job])
    for job in jobs:
        fpr._dispatch_one(con, job, "w1")
    assert _state_of(con, ready_job)[0] == "done"
    assert _state_of(con, stuck[2])[0] == "held"


# --------------------------------------------------------------------------
# Reconciliation: an existing live review means done, never a second spend —
# whether seen on the first pass or recovered from an abandoned claim after a
# crash between posting and recording.
# --------------------------------------------------------------------------

def test_already_reviewed_is_done_with_no_spend(world):
    repo, pr, sha = "operator/x", 50, "4" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0050-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    world.forge.add_review(repo, pr, sha)
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "done" and "already stands" in reason
    assert world.dispatch_count() == 0


def test_other_reviewers_review_does_not_settle_the_plan_review(world):
    """ADR-0021 §4 / ADR-0028 §1: the OTHER reviewer's review never settles a
    stop. `rev2bot` posts independently on every repo (`reviews_at`'s own
    docstring) — a `rev2bot`-only review at this head must not be read as 'the
    plan review already ran'; this worker still has to dispatch it."""
    repo, pr, sha = "operator/x", 52, "a2" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0052-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    world.forge.add_review(repo, pr, sha, login="rev2bot", review_state="APPROVED")
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "done" and "posted and verified" in reason
    assert world.dispatch_count() == 1


def test_an_owner_comment_does_not_settle_the_plan_review(world):
    """Neither does a comment-shaped review from an account that is not the
    reviewer role at all — an owner note at the head is not a plan review."""
    repo, pr, sha = "operator/x", 53, "b2" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0053-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    world.forge.add_review(repo, pr, sha, login="operator", review_state="COMMENT")
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "done" and "posted and verified" in reason
    assert world.dispatch_count() == 1


def test_abandoned_claim_after_a_crash_is_recovered_without_a_second_round(world):
    """The review was already posted (a crash happened between `post-review.py`
    publishing it and this worker recording `done`). Recovery must see the
    existing review and never pay for another round."""
    repo, pr, sha = "operator/x", 51, "5" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0051-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    # Simulate: a worker claimed this job a long time ago and crashed after
    # the dispatcher posted but before this worker recorded completion.
    now = 0
    con.execute("UPDATE plan_review_job SET state='claimed', claimed_by='dead', "
               "claimed_at=? WHERE id=?", (now, job_id))
    con.commit()
    world.forge.add_review(repo, pr, sha)   # the post that crash lost the record of

    jobs = fpr._claim_jobs(con, "w2", limit=5, claim_ttl=1800)
    assert [j["id"] for j in jobs] == [job_id]
    fpr._dispatch_one(con, jobs[0], "w2")
    state, reason, _ = _state_of(con, job_id)
    assert state == "done"
    assert world.dispatch_count() == 0   # never redispatched


# --------------------------------------------------------------------------
# Multi-seat repos (plan 0188 D3: "Honor the current plan reviewer seats").
# This fleet configures ordinal 2 (`rev2bot`) for `plan_reviewer` on every
# production repo via fleet-models' `default` row; these tests exercise that
# with an explicit `repo_model` configuration rather than relying on it.
# --------------------------------------------------------------------------

def test_one_seat_posted_of_two_required_is_not_done(world, monkeypatch):
    repo, pr, sha = "operator/x", 54, "c2" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0054-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    world.forge.add_review(repo, pr, sha, login="revbot")  # ordinal 1 only
    monkeypatch.setenv("FAKE_DISPATCH_MODE", "post_accounts")
    monkeypatch.setenv("FAKE_DISPATCH_ACCOUNTS", "rev2bot")   # the round completes the missing seat
    con = world.connect()
    world.set_ordinals(con, repo, [1, 2])
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    # ordinal 2 (rev2bot) was still missing, so this worker had to dispatch — it
    # is not read as done off ordinal 1's standing review alone.
    state, reason, _ = _state_of(con, job_id)
    assert state == "done" and "posted and verified" in reason
    assert world.dispatch_count() == 1


def test_both_seats_already_posted_is_done_with_no_spend(world):
    repo, pr, sha = "operator/x", 55, "d2" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0055-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    world.forge.add_review(repo, pr, sha, login="revbot")
    world.forge.add_review(repo, pr, sha, login="rev2bot")
    con = world.connect()
    world.set_ordinals(con, repo, [1, 2])
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "done" and "every configured plan-reviewer seat" in reason
    assert world.dispatch_count() == 0


def test_dispatch_completes_the_missing_second_seat_only(world, monkeypatch):
    """talos's own per-ordinal idempotence skips an already-standing ordinal
    (`standing_ordinals()`) — this worker's round must rely on that rather
    than re-posting ordinal 1."""
    repo, pr, sha = "operator/x", 56, "e4" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0056-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    world.forge.add_review(repo, pr, sha, login="revbot")
    monkeypatch.setenv("FAKE_DISPATCH_MODE", "post_accounts")
    monkeypatch.setenv("FAKE_DISPATCH_ACCOUNTS", "revbot,rev2bot")
    con = world.connect()
    world.set_ordinals(con, repo, [1, 2])
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, _reason, _ = _state_of(con, job_id)
    assert state == "done"
    logins = [r["user"]["login"] for r in world.forge._read()["reviews"][f"{repo}#{pr}"]]
    assert logins.count("revbot") == 1
    assert logins.count("rev2bot") == 1


def test_ordinal_two_quota_stop_held_for_mopsus_ordinal_one_already_posted(world, monkeypatch):
    """The exact scenario the finding names: ordinal 1 posted, ordinal 2 (the
    SAME round's other seat) stopped on a quota outcome. The job must hold for
    mopsus's retry (ADR-0028 §1 settles each ordinal by its own account/row),
    never read as done off ordinal 1 alone, and never redispatched here while
    mopsus still owns ordinal 2's stop."""
    repo, pr, sha = "operator/x", 57, "f4" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0057-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    world.forge.add_review(repo, pr, sha, login="revbot")
    con = world.connect()
    world.set_ordinals(con, repo, [1, 2])
    con.execute("CREATE TABLE review_attempt (repo TEXT, pr INTEGER, head_sha TEXT, "
               "model TEXT, outcome TEXT, session_uuid TEXT, started_at TEXT, "
               "stopped_at TEXT, ordinal INTEGER, position TEXT)")
    con.execute("INSERT INTO review_attempt (repo, pr, head_sha, outcome, ordinal) "
               "VALUES (?,?,?,?,2)", (repo, pr, sha, "quota"))
    con.commit()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "held"
    assert "ordinal 2" in reason and "quota" in reason and "mopsus" in reason
    assert world.dispatch_count() == 0


# --------------------------------------------------------------------------
# Dispatch outcomes: mopsus-owned stops are held (not retried here); any
# other stop is bounded, then failed with the actionable detail.
# --------------------------------------------------------------------------

def test_quota_outcome_is_held_for_mopsus_not_retried_here(world, monkeypatch):
    repo, pr, sha = "operator/x", 60, "6" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0060-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    monkeypatch.setenv("FAKE_DISPATCH_MODE", "fail")
    monkeypatch.setenv("FAKE_DISPATCH_FAIL_MSG", "FATAL: You've reached your weekly limit")
    con = world.connect()
    con.execute("CREATE TABLE review_attempt (repo TEXT, pr INTEGER, head_sha TEXT, "
               "model TEXT, outcome TEXT, session_uuid TEXT, started_at TEXT, "
               "stopped_at TEXT, ordinal INTEGER, position TEXT)")
    con.execute("INSERT INTO review_attempt (repo, pr, head_sha, outcome, ordinal) "
               "VALUES (?,?,?,?,1)", (repo, pr, sha, "quota"))
    con.commit()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, attempts = _state_of(con, job_id)
    assert state == "held"
    assert "quota" in reason and "mopsus" in reason
    # the stop was already recorded (as mopsus's own retry would leave it
    # before this worker ever saw the job) — never spent here at all.
    assert world.dispatch_count() == 0
    assert attempts == 0

    # a second sweep does not redispatch while mopsus still owns the stop.
    jobs = fpr._claim_jobs(con, "w1", limit=5, claim_ttl=1800)
    assert [j["id"] for j in jobs] == [job_id]
    fpr._dispatch_one(con, jobs[0], "w1")
    assert world.dispatch_count() == 0


def test_no_verdict_outcome_is_held_for_mopsus_not_retried_here(world, monkeypatch):
    """D5's 'missing verdict': ADR-0021's no-verdict stop is mopsus's own
    retry territory exactly like quota (MOPSUS_OWNED_OUTCOMES) — never
    redispatched by this worker."""
    repo, pr, sha = "operator/x", 64, "b1" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0064-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    monkeypatch.setenv("FAKE_DISPATCH_MODE", "fail")
    monkeypatch.setenv("FAKE_DISPATCH_FAIL_MSG", "FATAL: no verdict was reached")
    monkeypatch.setenv("FAKE_DISPATCH_RECORD_OUTCOME", "no-verdict")
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, attempts = _state_of(con, job_id)
    assert state == "held"
    assert "no-verdict" in reason and "mopsus" in reason
    assert world.dispatch_count() == 1
    assert attempts == 1


def test_high_finding_review_is_terminal_no_redispatch(world):
    """D5: 'A HIGH is terminal for this head' — a seat's own review that
    withholds (posted as COMMENT, never APPROVED) settles that seat exactly
    like an approval: no further dispatch is owed at this head, and the
    per-seat outcome is recorded in `reason` rather than collapsed to a bare
    'posted'."""
    repo, pr, sha = "operator/x", 65, "c1" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0065-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    world.forge.add_review(repo, pr, sha, login="revbot", review_state="COMMENT")
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "done"
    assert "not approved" in reason
    assert world.dispatch_count() == 0


def test_quota_outcome_recorded_during_this_dispatch_is_held_not_failed(world, monkeypatch):
    """Unlike the pre-existing-row case above: here nothing knew about the
    stop until THIS worker's own dispatch attempt spent on it and the
    dispatcher recorded the outcome. The job still ends up held, not failed —
    mopsus's retry owns it from that point on, exactly as it would have if
    mopsus itself had dispatched first."""
    repo, pr, sha = "operator/x", 63, "a1" * 20
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0063-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    monkeypatch.setenv("FAKE_DISPATCH_MODE", "fail")
    monkeypatch.setenv("FAKE_DISPATCH_FAIL_MSG", "FATAL: You've reached your weekly limit")
    monkeypatch.setenv("FAKE_DISPATCH_RECORD_OUTCOME", "quota")
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, attempts = _state_of(con, job_id)
    assert state == "held"
    assert "quota" in reason and "mopsus" in reason
    assert world.dispatch_count() == 1
    assert attempts == 1


def test_unsupported_failure_is_bounded_then_failed_with_actionable_detail(world, monkeypatch):
    repo, pr, sha = "operator/x", 61, "7" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0061-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    monkeypatch.setenv("FAKE_DISPATCH_MODE", "fail")
    monkeypatch.setenv("FAKE_DISPATCH_FAIL_MSG", "FATAL: host-missing-tool: no such tool")
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)

    for _ in range(fpr.MAX_DISPATCH_ATTEMPTS - 1):
        jobs = fpr._claim_jobs(con, "w1", limit=5, claim_ttl=1800)
        fpr._dispatch_one(con, jobs[0], "w1")
        state, _reason, _attempts = _state_of(con, job_id)
        assert state == "held"

    jobs = fpr._claim_jobs(con, "w1", limit=5, claim_ttl=1800)
    fpr._dispatch_one(con, jobs[0], "w1")
    state, reason, attempts = _state_of(con, job_id)
    assert state == "failed"
    assert attempts == fpr.MAX_DISPATCH_ATTEMPTS
    assert "host-missing-tool" in reason
    assert world.dispatch_count() == fpr.MAX_DISPATCH_ATTEMPTS


def test_exit_zero_with_no_posted_review_is_never_trusted_as_a_verdict(world, monkeypatch):
    """The dispatcher can exit 0 having posted only a note (plan 0026's own
    measurement on epeius#1). Exit is not a verdict; only a review at the head
    is."""
    repo, pr, sha = "operator/x", 62, "8" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0062-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])
    monkeypatch.setenv("FAKE_DISPATCH_MODE", "post_no_review")
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    _claim_and_run(con, job_id)
    state, reason, _ = _state_of(con, job_id)
    assert state == "held"
    assert "not a verdict" in reason


# --------------------------------------------------------------------------
# `cmd_worker` itself: token-before-claim ordering, and one job's exception
# must not strand the rest of the sweep.
# --------------------------------------------------------------------------

class _WorkerArgs:
    def __init__(self, db, limit=5, claim_ttl=1800):
        self.db, self.limit, self.claim_ttl = str(db), limit, claim_ttl


def test_worker_resolves_the_forge_token_before_claiming_anything(world, monkeypatch):
    """D4: a token failure must not strand jobs claimed-but-unworked for the
    full --claim-ttl — enforced by resolving (or refusing) the forge BEFORE
    `_claim_jobs` ever runs, so a failure here leaves the queue untouched."""
    repo, pr, sha = "operator/x", 93, "93" * 20
    world.forge.set_pull(repo, pr, sha)
    con = world.connect()
    job_id = _enqueue(con, repo, pr, sha)
    con.close()

    def boom():
        raise RuntimeError("no forge token")
    monkeypatch.setattr(fpr, "_forge", boom)

    with pytest.raises(RuntimeError):
        fpr.cmd_worker(_WorkerArgs(world.db))

    con = world.connect()
    state, _reason, _ = _state_of(con, job_id)
    assert state == "queued"   # never claimed


def test_worker_releases_a_job_whose_dispatch_one_raised_and_continues(world, monkeypatch):
    """One job's exception must not strand the rest of a sweep's claims for
    the full --claim-ttl, nor abort the batch. The failing job goes back to
    `held` (fenced to this worker's own holder) and the next claimed job in
    the SAME sweep still runs to completion."""
    repo = "operator/x"
    sha_bad, sha_good = "aa" * 20, "bb" * 20
    world.forge.set_pull(repo, 95, sha_bad)
    world.forge.set_files(repo, 95, [{"filename": "plans/0095-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha_bad, [{"context": "CI / tests", "status": "success"}])
    world.forge.set_pull(repo, 96, sha_good)
    world.forge.set_files(repo, 96, [{"filename": "plans/0096-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha_good, [{"context": "CI / tests", "status": "success"}])

    con = world.connect()
    bad_id = _enqueue(con, repo, 95, sha_bad)
    good_id = _enqueue(con, repo, 96, sha_good)
    con.close()

    real_dispatch_one = fpr._dispatch_one

    def flaky(con, job, holder):
        if job["pr"] == 95:
            raise RuntimeError("simulated crash mid-dispatch")
        return real_dispatch_one(con, job, holder)

    monkeypatch.setattr(fpr, "_dispatch_one", flaky)
    assert fpr.cmd_worker(_WorkerArgs(world.db)) == 0

    con = world.connect()
    bad_state, bad_reason, _ = _state_of(con, bad_id)
    good_state, _good_reason, _ = _state_of(con, good_id)
    assert bad_state == "held" and "RuntimeError" in bad_reason
    assert good_state == "done"


# --------------------------------------------------------------------------
# Two concurrent workers: the atomic claim's own property — every job is
# claimed by exactly one of them, none lost, none doubled.
# --------------------------------------------------------------------------

def test_two_concurrent_workers_claim_each_job_exactly_once(world):
    con = world.connect()
    ids = [_enqueue(con, "operator/x", 100 + i, f"{i:040d}") for i in range(20)]
    con.close()

    results = []
    lock = threading.Lock()

    def run(n):
        c = world.connect()
        jobs = fpr._claim_jobs(c, f"worker-{n}", limit=20, claim_ttl=1800)
        with lock:
            results.append([j["id"] for j in jobs])
        c.close()

    threads = [threading.Thread(target=run, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed = [jid for r in results for jid in r]
    assert sorted(claimed) == sorted(ids)
    assert len(claimed) == len(set(claimed))


# --------------------------------------------------------------------------
# The CLI surface.
# --------------------------------------------------------------------------

def _run_cli(*args, env=None, db=None):
    full_env = dict(env or {})
    if db:
        full_env.setdefault("FLEET_DB", str(db))
    import os
    full_env = {**os.environ, **full_env}
    return subprocess.run([sys.executable, str(BIN / "fleet-plan-review"), *args],
                          capture_output=True, text=True, env=full_env)


def test_status_cli_reports_state_and_reason(world):
    con = world.connect()
    job_id = _enqueue(con, "operator/x", 70, "9" * 40)
    fpr._transition(con, job_id, "held", actor="test", action="hold",
                    reason="CI is still running on 99999999")
    con.close()
    r = _run_cli("status", "--repo", "operator/x", "--json", db=world.db)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert len(rows) == 1 and rows[0]["state"] == "held"
    assert "still running" in rows[0]["reason"]

    r = _run_cli("status", "--repo", "operator/x", db=world.db)
    assert "held" in r.stdout and "still running" in r.stdout


# --------------------------------------------------------------------------
# The migration: additive, idempotent, preserves existing data.
# --------------------------------------------------------------------------

def test_migration_is_idempotent_and_preserves_data(tmp_path):
    db = tmp_path / "fleet.db"
    import os
    env = dict(os.environ, FLEET_DB=str(db), EUNOMIA_FLEET_DIR=str(tmp_path))
    r = subprocess.run([sys.executable, str(BIN / "fleet-db-migrate")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok fleet-plan-review" in r.stdout

    con = sqlite3.connect(db)
    con.execute("INSERT INTO plan_review_job (id, repo, pr, head_sha, state, "
               "attempts, created_at, updated_at) VALUES "
               "('x','operator/x',1,'deadbeef','queued',0,1.0,1.0)")
    con.commit()
    con.close()

    r = subprocess.run([sys.executable, str(BIN / "fleet-db-migrate")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr

    con = sqlite3.connect(db)
    rows = con.execute("SELECT id, repo, pr FROM plan_review_job").fetchall()
    assert rows == [("x", "operator/x", 1)]
