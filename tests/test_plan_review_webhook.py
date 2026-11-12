"""`bin/fleet-depsd`'s `pull_request` webhook handling and resident-service
integration with plan 0188's queue (plan 0189).

A `pull_request` delivery PERSISTS a `plan_review_job` row — through plan
0188's own `_enqueue()`, never a restated writer — before the handler
returns success, and never dispatches one itself: dispatch (a model call)
runs only on the resident service's separate worker loop. The handler always
re-reads the PR's CURRENT head from a fake forge rather than trusting the
delivery's own `head` field, so a replayed, duplicate, or out-of-order
delivery for one PR collapses to exactly one job for whatever the forge
says the head actually is at read time.
"""
import hashlib
import hmac
import importlib.machinery
import importlib.util
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load_depsd():
    loader = importlib.machinery.SourceFileLoader("fleet_depsd_webhook", str(BIN / "fleet-depsd"))
    spec = importlib.util.spec_from_loader("fleet_depsd_webhook", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


DEPSD = _load_depsd()


@pytest.fixture(autouse=True)
def _isolated_fleet_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet-dir"))
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))


@pytest.fixture(autouse=True)
def _reload_depsd(_isolated_fleet_dir):
    global DEPSD
    DEPSD = _load_depsd()


def _db(tmp_path):
    path = Path(DEPSD.os.environ.get("FLEET_DB") or (tmp_path / "fleet.db"))
    con = sqlite3.connect(path)
    con.executescript(DEPSD.SCHEMA)
    con.executescript(DEPSD.planreview.SCHEMA)
    con.commit()
    con.close()
    return path


# --------------------------------------------------------------------------
# A fake forge, the subset of `fleetforge.Forge`'s surface this file's own
# functions call: `get_pull`, `list_pull_files` (via `changed_paths_for_position`),
# `list_pulls` (reconciliation only). `get_commit_status` and `list_reviews`
# are added only for the end-to-end worker test below, which drives the real
# `_dispatch_one` (`bin/fleet-plan-review`) all the way to a CI read and a
# per-ordinal review read, not just the webhook's own enqueue path.
# --------------------------------------------------------------------------

class FakeForge:
    def __init__(self):
        self.pulls = {}     # (repo, pr) -> body
        self.files = {}     # (repo, pr) -> [file entries]
        self.ci = {}        # (repo, sha) -> {"statuses": [...]}
        self.reviews = {}   # (repo, pr) -> [review entries]

    def set_pull(self, repo, pr, head_sha, state="open", draft=False, merged=False):
        self.pulls[(repo, pr)] = {
            "number": pr, "state": state, "draft": draft, "merged": merged,
            "head": {"sha": head_sha}}

    def set_files(self, repo, pr, files):
        self.files[(repo, pr)] = files

    def set_ci(self, repo, sha, statuses):
        self.ci[(repo, sha)] = {"statuses": statuses}

    def add_review(self, repo, pr, commit_id, login, review_state):
        reviews = self.reviews.setdefault((repo, pr), [])
        reviews.append({
            "id": len(reviews) + 1, "state": review_state, "commit_id": commit_id,
            "stale": False, "dismissed": False, "user": {"login": login}})

    def get_pull(self, repo, pr):
        body = self.pulls.get((repo, pr))
        return (200, body) if body is not None else (404, None)

    def list_pull_files(self, repo, pr, page_cap=20, limit=50):
        # Falls back to a single plan-only file when the test never called
        # `set_files` for this pull request — most tests in this file only
        # care about draft/closed/signature handling, not path classification,
        # and `_enqueue_plan_review`'s own plan-only filter would otherwise
        # make every one of them ineligible by default.
        body = self.files.get((repo, pr))
        if body is None:
            body = [{"filename": "plans/default-fixture.md", "status": "added"}]
        return 200, body

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        rows = [b for (r, _n), b in sorted(self.pulls.items())
                if r == repo and (state in ("all", b.get("state")))]
        start = (page - 1) * limit
        return 200, rows[start:start + limit]

    def get_commit_status(self, repo, sha):
        return 200, self.ci.get((repo, sha), {"statuses": []})

    def list_reviews(self, repo, pr, page=1, limit=50):
        body = self.reviews.get((repo, pr), [])
        return (200, body) if page == 1 else (200, [])


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = _db(tmp_path)
    forge = FakeForge()
    monkeypatch.setattr(DEPSD.planreview, "_forge", lambda: forge)
    monkeypatch.setattr(DEPSD.planreview, "_FORGE", forge)
    repos_conf = tmp_path / "repos.conf"
    repos_conf.write_text("repo: operator/x\n")
    monkeypatch.setattr(DEPSD.planreview.fleet_repo, "CONF", repos_conf)

    class W:
        pass
    w = W()
    w.db, w.forge, w.tmp_path = db, forge, tmp_path

    def connect():
        return DEPSD.planreview._connect(str(db))
    w.connect = connect
    return w


def _jobs(world):
    con = world.connect()
    rows = con.execute(
        "SELECT repo, pr, head_sha, state, reason, actor FROM plan_review_job j "
        "JOIN (SELECT job_id, actor FROM plan_review_job_change "
        "WHERE action='enqueue') c ON c.job_id=j.id ORDER BY j.created_at").fetchall()
    con.close()
    return rows


def _sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _server(world, secret="s3cret"):
    import threading
    from http.server import ThreadingHTTPServer
    ctx = DEPSD._Ctx(str(world.db), "127.0.0.1")
    ctx.secret = secret
    ctx.lease_id = "lease-test"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), DEPSD.make_handler_class(ctx))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return srv, ctx, f"http://{host}:{port}"


def _pr_payload(repo, pr, action, head_sha="deadbeef" * 5):
    return json.dumps({
        "action": action,
        "number": pr,
        "pull_request": {"number": pr, "head": {"sha": head_sha}},
        "repository": {"full_name": repo},
    }).encode()


def _post_webhook(base, body, secret="s3cret", event="pull_request", sig=None):
    headers = {"X-Forgejo-Event": event}
    if sig is not False:
        headers["X-Forgejo-Signature"] = sig if sig else _sign(secret, body)
    req = urllib.request.Request(base + "/webhook/forgejo", data=body, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode() or "{}")
        except Exception:
            parsed = {}
        return e.code, parsed


# --------------------------------------------------------------------- D1

def test_a_signed_opened_delivery_persists_a_job_before_returning_202(world):
    world.forge.set_pull("operator/x", 7, "a" * 40)
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 7, "opened"))
        assert status == 202
        assert ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    rows = _jobs(world)
    assert len(rows) == 1
    repo, pr, head_sha, state, reason, actor = rows[0]
    assert (repo, pr, head_sha, state) == ("operator/x", 7, "a" * 40, "queued")
    assert actor == "fleet-depsd"
    assert "pull_request opened" in reason


def test_an_unsigned_pull_request_delivery_is_401_and_persists_nothing(world):
    world.forge.set_pull("operator/x", 7, "a" * 40)
    srv, ctx, base = _server(world)
    try:
        body = _pr_payload("operator/x", 7, "opened")
        status, _ = _post_webhook(base, body, sig=False)
        assert status == 401
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


def test_a_wrong_signature_pull_request_delivery_is_401(world):
    world.forge.set_pull("operator/x", 7, "a" * 40)
    srv, _ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 7, "opened"), sig="0" * 64)
        assert status == 401
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


@pytest.mark.parametrize("action", ["labeled", "assigned", "closed", "edited"])
def test_an_action_outside_the_lifecycle_allowlist_is_204_and_persists_nothing(world, action):
    world.forge.set_pull("operator/x", 7, "a" * 40)
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 7, action))
        assert status == 204
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


def test_a_delivery_for_an_unenrolled_repo_is_204(world):
    world.forge.set_pull("operator/not-enrolled", 1, "a" * 40)
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/not-enrolled", 1, "opened"))
        assert status == 204
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


def test_a_delivery_for_a_draft_pr_is_204_and_persists_nothing(world):
    world.forge.set_pull("operator/x", 15, "a" * 40, draft=True)
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 15, "opened"))
        assert status == 204
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


def test_a_delivery_for_a_closed_pr_is_204_and_persists_nothing(world):
    world.forge.set_pull("operator/x", 16, "a" * 40, state="closed")
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 16, "synchronize"))
        assert status == 204
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


def test_a_draft_delivery_leaves_the_same_head_recoverable_once_ready(world):
    """The whole point of checking draft/open BEFORE writing a row: no
    `skipped` terminal row is ever written for a head that was never
    reviewable, so a LATER delivery for that identical head — the PR taken
    out of draft with no new commit, `ready_for_review`/`reopened` — still
    gets a fresh `queued` row rather than finding a dead end `_claim_jobs`
    will never reclaim."""
    world.forge.set_pull("operator/x", 17, "a" * 40, draft=True)
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 17, "opened"))
        assert status == 204
        assert _jobs(world) == []

        world.forge.set_pull("operator/x", 17, "a" * 40, draft=False)
        status, _ = _post_webhook(base, _pr_payload("operator/x", 17, "synchronize"))
        assert status == 202
        assert ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    rows = _jobs(world)
    assert len(rows) == 1
    assert rows[0][:3] == ("operator/x", 17, "a" * 40)


def test_a_ready_for_review_delivery_queues_the_now_eligible_head(world):
    """`ready_for_review` — Gitea/Forgejo's own name for taking a PR out of
    draft — is in `_PR_LIFECYCLE_ACTIONS`: the one delivery a draft PR's
    filing session cannot itself end on, since the PR is not reviewable
    until an owner or a later push marks it ready, routinely well after the
    filing session that opened it has ended."""
    world.forge.set_pull("operator/x", 18, "a" * 40, draft=True)
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 18, "opened"))
        assert status == 204
        assert _jobs(world) == []

        world.forge.set_pull("operator/x", 18, "a" * 40, draft=False)
        status, _ = _post_webhook(base, _pr_payload("operator/x", 18, "ready_for_review"))
        assert status == 202
        assert ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    rows = _jobs(world)
    assert len(rows) == 1
    assert rows[0][:3] == ("operator/x", 18, "a" * 40)


def test_a_claim_time_skipped_row_is_revived_not_swallowed(world):
    """The narrower race `_enqueue_plan_review`'s own docstring describes:
    the PR goes ineligible AFTER a `queued` row already exists but BEFORE
    `bin/fleet-plan-review`'s worker claims it, so `_dispatch_one`
    terminal-fences that row `skipped` itself — this function never sees
    the ineligible PR directly. A later delivery for that identical,
    now-eligible head must still revive that exact row: `_claim_jobs` never
    reclaims a `skipped` row on its own, so leaving it unchanged would
    swallow the review at that head forever."""
    world.forge.set_pull("operator/x", 21, "a" * 40)
    con = world.connect()
    job_id, created = DEPSD.planreview._enqueue(
        con, "operator/x", 21, "a" * 40, actor="test", reason="setup")
    assert created
    assert DEPSD.planreview._transition(
        con, job_id, "skipped", actor="test", action="skip",
        reason="PR is a draft")
    con.close()

    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 21, "reopened"))
        assert status == 202
        assert ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()

    con = world.connect()
    state = con.execute(
        "SELECT state FROM plan_review_job WHERE id=?", (job_id,)).fetchone()[0]
    count = con.execute(
        "SELECT COUNT(*) FROM plan_review_job WHERE repo=? AND pr=? AND head_sha=?",
        ("operator/x", 21, "a" * 40)).fetchone()[0]
    con.close()
    assert state == "queued"
    assert count == 1


def test_a_delivery_for_a_code_change_is_204_and_persists_nothing(world):
    """`pull_request` fires for every pull request in an enrolled repository,
    code changes included — `_enqueue_plan_review` must filter by the
    changed-file position the same way `_reconcile_plan_reviews` already
    does, or every code PR's lifecycle event would spend one of the worker's
    bounded per-pass claims on a job `_dispatch_one` was always going to
    skip anyway."""
    world.forge.set_pull("operator/x", 22, "a" * 40)
    world.forge.set_files("operator/x", 22, [{"filename": "bin/fleet-thing", "status": "modified"}])
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 22, "opened"))
        assert status == 204
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


def test_a_code_change_skipped_row_is_never_revived_by_a_same_head_delivery(world):
    """Unlike a draft/closed/merged flip, a change's position at a FIXED head
    cannot flip with no new commit — so a row the worker already left
    `skipped` for "not a plan filing" must stay `skipped` forever at that
    head, not get revived into a job that will only be skipped again."""
    world.forge.set_pull("operator/x", 23, "a" * 40)
    world.forge.set_files("operator/x", 23, [{"filename": "bin/fleet-thing", "status": "modified"}])
    con = world.connect()
    job_id, created = DEPSD.planreview._enqueue(
        con, "operator/x", 23, "a" * 40, actor="test", reason="setup")
    assert created
    assert DEPSD.planreview._transition(
        con, job_id, "skipped", actor="test", action="skip",
        reason="not a plan filing")
    con.close()

    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 23, "reopened"))
        assert status == 204
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()

    con = world.connect()
    state = con.execute(
        "SELECT state FROM plan_review_job WHERE id=?", (job_id,)).fetchone()[0]
    con.close()
    assert state == "skipped"


def test_a_pull_unreadable_from_the_forge_is_503_and_persists_nothing(world):
    # No set_pull() call at all: the fake forge answers 404, as an unreadable
    # or not-yet-visible PR would.
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 9, "opened"))
        assert status == 503
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


def test_an_unreadable_queue_database_is_503_not_a_dropped_connection(world, monkeypatch):
    """The documented 503 for "the queue database could not be read"
    (docs/depsd.md) is answered even when the forge read succeeds and only
    the persist step fails — a `sqlite3.Error` from `_enqueue` must not
    propagate out of the HTTP handler and abort the connection with no
    response at all."""
    world.forge.set_pull("operator/x", 19, "a" * 40)

    def boom_enqueue(con, repo, pr, head_sha, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(DEPSD.planreview, "_enqueue", boom_enqueue)
    srv, ctx, base = _server(world)
    try:
        status, _ = _post_webhook(base, _pr_payload("operator/x", 19, "opened"))
        assert status == 503
        assert not ctx.review_dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _jobs(world) == []


def test_the_payloads_own_head_field_is_never_trusted(world):
    """D1/Boundaries: "the payload is a trigger, never a fact". The delivery
    claims a head the forge does not agree with; the persisted job names the
    forge's own head, not the payload's."""
    world.forge.set_pull("operator/x", 7, "c" * 40)
    srv, _ctx, base = _server(world)
    try:
        status, _ = _post_webhook(
            base, _pr_payload("operator/x", 7, "opened", head_sha="b" * 40))
        assert status == 202
    finally:
        srv.shutdown()
        srv.server_close()
    rows = _jobs(world)
    assert len(rows) == 1 and rows[0][2] == "c" * 40


def test_replaying_the_same_delivery_twice_writes_one_job(world):
    world.forge.set_pull("operator/x", 7, "a" * 40)
    srv, _ctx, base = _server(world)
    try:
        body = _pr_payload("operator/x", 7, "opened")
        assert _post_webhook(base, body)[0] == 202
        assert _post_webhook(base, body)[0] == 202   # Forgejo's own redelivery
    finally:
        srv.shutdown()
        srv.server_close()
    assert len(_jobs(world)) == 1


def test_out_of_order_deliveries_at_one_head_still_write_one_job(world):
    """`opened` then a later `synchronize` whose own payload embeds an OLDER
    head than `opened`'s (an out-of-order delivery, or one simply slower in
    transit) — since both reads go straight to the forge's CURRENT head,
    both collapse to the one row for whatever that head actually was at
    read time, never two rows disagreeing about it."""
    world.forge.set_pull("operator/x", 7, "a" * 40)
    srv, _ctx, base = _server(world)
    try:
        assert _post_webhook(
            base, _pr_payload("operator/x", 7, "opened", head_sha="a" * 40))[0] == 202
        assert _post_webhook(
            base, _pr_payload("operator/x", 7, "synchronize", head_sha="00" * 20))[0] == 202
    finally:
        srv.shutdown()
        srv.server_close()
    rows = _jobs(world)
    assert len(rows) == 1 and rows[0][2] == "a" * 40


def test_a_real_head_move_between_deliveries_writes_a_second_job(world):
    """Ground truth itself changing between two deliveries (a genuine push)
    is NOT collapsed — it is the normal case this queue's own `superseded`
    state (plan 0188) exists for; a later claim resolves which row stands."""
    srv, _ctx, base = _server(world)
    try:
        world.forge.set_pull("operator/x", 7, "a" * 40)
        assert _post_webhook(base, _pr_payload("operator/x", 7, "opened"))[0] == 202
        world.forge.set_pull("operator/x", 7, "b" * 40)
        assert _post_webhook(base, _pr_payload("operator/x", 7, "synchronize"))[0] == 202
    finally:
        srv.shutdown()
        srv.server_close()
    rows = _jobs(world)
    assert {r[2] for r in rows} == {"a" * 40, "b" * 40}


# --------------------------------------------------------------------- D2

def test_reconcile_calls_the_real_forge_list_pulls_signature(world):
    """`_reconcile_plan_reviews` calls `forge.list_pulls(repo, state="open",
    limit=prs_per_repo)` — every other test in this file proves that call
    shape works against `FakeForge.list_pulls`, which this file's own
    author wrote to match `fleetforge.Forge.list_pulls`'s signature by
    hand. That proves the two agree with EACH OTHER, never that either
    agrees with the real wrapper `bin/fleetforge.py` ships, which this
    plan did not touch and whose signature could drift out from under a
    hand-copied fake with every test here still green. Bound against the
    REAL class's own signature instead, with no instance and no network
    call: `inspect.Signature.bind` raises `TypeError` for exactly the same
    reason a live call would — an unknown keyword, a positional that
    doesn't exist — without needing a `Forge` to construct."""
    import inspect
    sig = inspect.signature(DEPSD.planreview.fleetforge.Forge.list_pulls)
    sig.bind(object(), "operator/x", state="open", limit=10)


def test_reconcile_recovers_a_lost_delivery(world):
    """No webhook ever arrived for this PR (the delivery lost, the repo
    enrolled before any hook existed, or the owner has not confirmed the
    pull_request action names yet — see docs/depsd.md); the bounded sweep
    alone finds it."""
    world.forge.set_pull("operator/x", 11, "d" * 40)
    world.forge.set_files("operator/x", 11, [{"filename": "plans/0011-x.md", "status": "added"}])
    DEPSD._reconcile_plan_reviews(str(world.db))
    rows = _jobs(world)
    assert len(rows) == 1
    assert rows[0][:3] == ("operator/x", 11, "d" * 40)
    assert "reconcile" in rows[0][4]


def test_reconcile_skips_a_non_plan_filing(world):
    """A code change is not this queue's business — talos's own
    `reviewer_position` would hand it to `code_reviewer`."""
    world.forge.set_pull("operator/x", 12, "e" * 40)
    world.forge.set_files("operator/x", 12, [{"filename": "bin/fleet-thing", "status": "modified"}])
    DEPSD._reconcile_plan_reviews(str(world.db))
    assert _jobs(world) == []


def test_reconcile_skips_a_draft(world):
    world.forge.pulls[("operator/x", 13)] = {
        "number": 13, "state": "open", "draft": True, "merged": False,
        "head": {"sha": "f" * 40}}
    world.forge.set_files("operator/x", 13, [{"filename": "plans/0013-x.md", "status": "added"}])
    DEPSD._reconcile_plan_reviews(str(world.db))
    assert _jobs(world) == []


def test_reconcile_excludes_an_already_completed_current_head(world):
    """`_enqueue`'s own idempotence on (repo, pr, head_sha) IS "exclude
    completed current heads": a row already standing for this exact head —
    whatever its state — is never duplicated by the sweep."""
    world.forge.set_pull("operator/x", 14, "9" * 40)
    world.forge.set_files("operator/x", 14, [{"filename": "plans/0014-x.md", "status": "added"}])
    con = world.connect()
    job_id, _ = DEPSD.planreview._enqueue(con, "operator/x", 14, "9" * 40)
    DEPSD.planreview._transition(con, job_id, "done", actor="test", action="done",
                                 reason="already reviewed")
    con.close()
    DEPSD._reconcile_plan_reviews(str(world.db))
    rows = _jobs(world)
    assert len(rows) == 1   # still the one row — no duplicate written


def test_reconcile_revives_a_stale_skipped_row(world):
    """The same dead end `_enqueue_plan_review`'s own docstring describes for
    the webhook handler, reached instead through the sweep: a plan PR's
    `skipped` row at its current head (drafted, or closed, when the worker
    last claimed it) must be revived once the sweep re-confirms the PR
    eligible again — `_claim_jobs` never reclaims a `skipped` row on its
    own, and without this the sweep alone could never recover it either."""
    world.forge.set_pull("operator/x", 24, "b" * 40)
    world.forge.set_files("operator/x", 24, [{"filename": "plans/0024-x.md", "status": "added"}])
    con = world.connect()
    job_id, created = DEPSD.planreview._enqueue(
        con, "operator/x", 24, "b" * 40, actor="test", reason="setup")
    assert created
    assert DEPSD.planreview._transition(
        con, job_id, "skipped", actor="test", action="skip",
        reason="PR is a draft")
    con.close()

    DEPSD._reconcile_plan_reviews(str(world.db))

    con = world.connect()
    state = con.execute(
        "SELECT state FROM plan_review_job WHERE id=?", (job_id,)).fetchone()[0]
    count = con.execute(
        "SELECT COUNT(*) FROM plan_review_job WHERE repo=? AND pr=? AND head_sha=?",
        ("operator/x", 24, "b" * 40)).fetchone()[0]
    con.close()
    assert state == "queued"
    assert count == 1


def test_reconcile_pages_past_the_first_page_to_recover_a_late_pull_request(world):
    """A repository holding more open pull requests than one page of
    `prs_per_repo` must still have ITS LATER pages visited in the same
    pass — truncating at the first page would silently and permanently
    never recover a plan PR that happens to sort past it."""
    for n in range(1, 13):   # 12 open PRs in one repo, page size 5 below
        world.forge.set_pull("operator/x", n, f"{n:02x}" * 20)
        world.forge.set_files("operator/x", n, [{"filename": "bin/noise", "status": "modified"}])
    # The one plan PR sorts onto the THIRD page at page size 5 (PRs 11-12).
    world.forge.set_files("operator/x", 12, [{"filename": "plans/0012-x.md", "status": "added"}])

    DEPSD._reconcile_plan_reviews(str(world.db), prs_per_repo=5, max_pages_per_repo=3)

    rows = _jobs(world)
    assert len(rows) == 1
    assert rows[0][:3] == ("operator/x", 12, f"{12:02x}" * 20)


def test_reconcile_warns_rather_than_silently_truncating_past_the_page_cap(world, capsys):
    """When a repository's open-PR backlog outruns `max_pages_per_repo`
    itself, the remainder waits for a later pass rather than vanishing with
    no trace at all."""
    for n in range(1, 11):
        world.forge.set_pull("operator/x", n, f"{n:02x}" * 20)
        world.forge.set_files("operator/x", n, [{"filename": "bin/noise", "status": "modified"}])

    DEPSD._reconcile_plan_reviews(str(world.db), prs_per_repo=5, max_pages_per_repo=2)

    err = capsys.readouterr().err
    assert "more open pull requests than" in err


def test_reconcile_cursor_advances_and_wraps_across_passes(world):
    """The sweep is BOUNDED per pass (D2: "avoid flooding ... on initial
    enrollment") and resumes from a persisted cursor rather than restarting
    from the registry's first repo every time."""
    repos_conf = world.tmp_path / "repos.conf"
    names = [f"operator/r{i}" for i in range(5)]
    repos_conf.write_text("".join(f"repo: {n}\n" for n in names))
    for i, n in enumerate(sorted(names)):
        world.forge.set_pull(n, 1, f"{i:02x}" * 20)
        world.forge.set_files(n, 1, [{"filename": "plans/x.md", "status": "added"}])

    DEPSD._reconcile_plan_reviews(str(world.db), repos_per_pass=2, prs_per_repo=5)
    first_pass_repos = {r[0] for r in _jobs(world)}
    assert len(first_pass_repos) == 2

    DEPSD._reconcile_plan_reviews(str(world.db), repos_per_pass=2, prs_per_repo=5)
    second_pass_repos = {r[0] for r in _jobs(world)} - first_pass_repos
    assert len(second_pass_repos) == 2
    assert first_pass_repos.isdisjoint(second_pass_repos)

    con = world.connect()
    cur = DEPSD._reconcile_cursor(con)
    con.close()
    assert cur == 4   # 2 + 2, not yet wrapped

    DEPSD._reconcile_plan_reviews(str(world.db), repos_per_pass=2, prs_per_repo=5)
    con = world.connect()
    cur = DEPSD._reconcile_cursor(con)
    con.close()
    assert cur == 1   # (4 + 2) % 5 — wrapped around the registry


def test_reconcile_with_no_enrolled_repos_does_nothing(world, monkeypatch):
    monkeypatch.setattr(DEPSD.planreview.fleet_repo, "CONF", world.tmp_path / "missing.conf")
    DEPSD._reconcile_plan_reviews(str(world.db))   # must not raise
    assert _jobs(world) == []


# ------------------------------------------------------------ worker wake

def test_the_bounded_worker_wake_claims_and_dispatches_through_plan_0188(world, monkeypatch):
    """`_run_plan_review_worker` is a thin caller: it claims through plan
    0188's own `_claim_jobs()` and dispatches through its own
    `_dispatch_one()` — never a restated version of either."""
    world.forge.set_pull("operator/x", 20, "1" * 40)
    con = world.connect()
    DEPSD.planreview._enqueue(con, "operator/x", 20, "1" * 40)
    con.close()

    calls = []

    def fake_dispatch_one(con, job, holder):
        calls.append((job["repo"], job["pr"], job["head_sha"], holder))
        DEPSD.planreview._fenced(con, job, holder, "done", "done", "fake dispatch")

    monkeypatch.setattr(DEPSD.planreview, "_dispatch_one", fake_dispatch_one)
    DEPSD._run_plan_review_worker(str(world.db))

    assert calls and calls[0][:3] == ("operator/x", 20, "1" * 40)
    con = world.connect()
    state = con.execute("SELECT state FROM plan_review_job WHERE repo='operator/x' AND pr=20"
                        ).fetchone()[0]
    con.close()
    assert state == "done"


def test_the_worker_wake_claims_nothing_when_the_forge_is_unavailable(world, monkeypatch):
    """A token-helper failure surfaces as `_forge()`'s own `sys.exit()` —
    `SystemExit`, a `BaseException` no `except Exception` catches. Resolved
    BEFORE claiming (the same ordering, and the same reason, as
    `fleet-plan-review`'s own `cmd_worker`): the call must not raise out of
    this function, the job must stay untouched (never claimed, so never
    stranded for the full claim TTL), and no job-claim code may run at
    all — proven by making `_claim_jobs` itself an error if reached."""
    world.forge.set_pull("operator/x", 22, "3" * 40)
    con = world.connect()
    DEPSD.planreview._enqueue(con, "operator/x", 22, "3" * 40)
    con.close()

    def boom_forge():
        raise SystemExit("fleet-plan-review: token helper failed")

    def boom_claim(*a, **k):
        raise AssertionError("must not claim jobs when the forge is unavailable")

    monkeypatch.setattr(DEPSD.planreview, "_forge", boom_forge)
    monkeypatch.setattr(DEPSD.planreview, "_claim_jobs", boom_claim)

    DEPSD._run_plan_review_worker(str(world.db))   # must not raise

    con = world.connect()
    state = con.execute("SELECT state FROM plan_review_job WHERE repo='operator/x' AND pr=22"
                        ).fetchone()[0]
    con.close()
    assert state == "queued"


def test_the_worker_wake_releases_a_job_whose_dispatch_raised(world, monkeypatch):
    """One job's exception must not strand it `claimed` forever — the same
    recovery `fleet-plan-review worker`'s own entry point gives it."""
    world.forge.set_pull("operator/x", 21, "2" * 40)
    con = world.connect()
    DEPSD.planreview._enqueue(con, "operator/x", 21, "2" * 40)
    con.close()

    def boom(con, job, holder):
        raise RuntimeError("simulated crash mid-dispatch")

    monkeypatch.setattr(DEPSD.planreview, "_dispatch_one", boom)
    DEPSD._run_plan_review_worker(str(world.db))

    con = world.connect()
    state, reason = con.execute(
        "SELECT state, reason FROM plan_review_job WHERE repo='operator/x' AND pr=21"
        ).fetchone()
    con.close()
    assert state == "held" and "RuntimeError" in reason


def test_the_worker_wake_releases_a_job_whose_dispatch_raised_systemexit(world, monkeypatch):
    """A `SystemExit` raised from anywhere inside `_dispatch_one` — not only
    from `_forge()`'s own, already-guarded call — is a `BaseException` no
    `except Exception` stops. It must be caught per job, the same as any
    other exception, rather than propagating out of the worker and killing
    the resident service's review thread silently."""
    world.forge.set_pull("operator/x", 23, "4" * 40)
    con = world.connect()
    DEPSD.planreview._enqueue(con, "operator/x", 23, "4" * 40)
    con.close()

    def boom(con, job, holder):
        raise SystemExit("an unanticipated sys.exit() deep in dispatch_review")

    monkeypatch.setattr(DEPSD.planreview, "_dispatch_one", boom)
    DEPSD._run_plan_review_worker(str(world.db))   # must not raise

    con = world.connect()
    state, reason = con.execute(
        "SELECT state, reason FROM plan_review_job WHERE repo='operator/x' AND pr=23"
        ).fetchone()
    con.close()
    assert state == "held" and "SystemExit" in reason


def test_the_worker_claims_one_job_at_a_time_not_a_batch(world, monkeypatch):
    """`DEFAULT_CLAIM_TTL` is sized for exactly one in-flight dispatch's own
    headroom, never for `limit` of them claimed in one batch and dispatched
    sequentially — claiming one at a time, immediately before dispatching
    it, keeps every job's `claimed_at` fresh relative to its own dispatch
    regardless of how many jobs a pass works through."""
    for pr in (30, 31, 32):
        world.forge.set_pull("operator/x", pr, f"{pr:02x}" * 20)
        con = world.connect()
        DEPSD.planreview._enqueue(con, "operator/x", pr, f"{pr:02x}" * 20)
        con.close()

    calls = []
    real_claim_jobs = DEPSD.planreview._claim_jobs

    def spy_claim_jobs(con, holder, limit, claim_ttl):
        calls.append(limit)
        return real_claim_jobs(con, holder, limit, claim_ttl)

    def fake_dispatch_one(con, job, holder):
        DEPSD.planreview._fenced(con, job, holder, "done", "done", "fake dispatch")

    monkeypatch.setattr(DEPSD.planreview, "_claim_jobs", spy_claim_jobs)
    monkeypatch.setattr(DEPSD.planreview, "_dispatch_one", fake_dispatch_one)
    DEPSD._run_plan_review_worker(str(world.db), limit=3)

    assert calls == [1, 1, 1]   # three separate claims of one job each, never a single claim of three
    con = world.connect()
    states = con.execute(
        "SELECT state FROM plan_review_job WHERE repo='operator/x'").fetchall()
    con.close()
    assert [s for (s,) in states] == ["done", "done", "done"]


def test_the_worker_wake_does_not_reclaim_the_sole_held_job_within_one_pass(world, monkeypatch):
    """A transient dispatch failure must get `MAX_DISPATCH_ATTEMPTS` spread
    across SEPARATE passes, not spent back to back in the same few seconds:
    with no other job in the queue, `_claim_jobs(..., limit=1)` would
    otherwise hand this worker the exact same job again on the very next
    turn of its own `while` loop, up to `limit` times, exhausting every
    attempt in one pass and marking it permanently `failed` for a failure
    that may have cleared by the next, later poll."""
    world.forge.set_pull("operator/x", 40, "9" * 40)
    con = world.connect()
    DEPSD.planreview._enqueue(con, "operator/x", 40, "9" * 40)
    con.close()

    calls = []

    def flaky(con, job, holder):
        calls.append(job["id"])
        DEPSD.planreview._fenced(con, job, holder, "held", "hold",
                                 "simulated transient failure", bump_attempt=True)

    monkeypatch.setattr(DEPSD.planreview, "_dispatch_one", flaky)
    DEPSD._run_plan_review_worker(str(world.db), limit=3)

    assert len(calls) == 1   # not reclaimed a second or third time this pass
    con = world.connect()
    state, attempts = con.execute(
        "SELECT state, attempts FROM plan_review_job WHERE repo='operator/x' AND pr=40"
        ).fetchone()
    con.close()
    assert state == "held" and attempts == 1   # one attempt spent, not MAX_DISPATCH_ATTEMPTS


def test_the_worker_wake_holds_a_job_without_dispatching_when_the_review_quota_is_closed(
        world, monkeypatch):
    """`_dispatch_one` itself never calls `review_quota_hold` — that gate is
    `bin/orchestrator`'s own first-attempt review loop's, taken before ITS
    OWN call to `dispatch_review()`. This worker wakes with nobody watching
    (a timer, a webhook), so it must take the same gate itself, before
    `_dispatch_one` ever reaches the forge — proven here by making
    `_dispatch_one` an assertion failure if reached. No attempt is spent: a
    closed gate is not a dispatch failure."""
    world.forge.set_pull("operator/x", 41, "8" * 40)
    con = world.connect()
    DEPSD.planreview._enqueue(con, "operator/x", 41, "8" * 40)
    con.close()

    class FakeOrch:
        @staticmethod
        def review_quota_hold(repo, position):
            assert (repo, position) == ("operator/x", "plan_reviewer")
            return "held: session closed until unknown (seat claude)"

    def boom_dispatch_one(con, job, holder):
        raise AssertionError("must not dispatch while the review quota is closed")

    monkeypatch.setattr(DEPSD.planreview, "_orch_mod", lambda: FakeOrch)
    monkeypatch.setattr(DEPSD.planreview, "_dispatch_one", boom_dispatch_one)
    DEPSD._run_plan_review_worker(str(world.db))

    con = world.connect()
    state, reason, attempts = con.execute(
        "SELECT state, reason, attempts FROM plan_review_job WHERE repo='operator/x' AND pr=41"
        ).fetchone()
    con.close()
    assert state == "held" and "closed" in reason and attempts == 0


def test_the_disposable_pr_flow_survives_a_restart_to_both_seat_outcomes(world, monkeypatch):
    """Definition of done: "Prove the disposable-PR flow against a fake
    forge and a fake reviewer in tests: a plan PR with pending CI, the
    filing session ended, the worker restarted, and both seat outcomes on
    that exact head after CI." Driven through `_run_plan_review_worker` and
    the REAL `_dispatch_one` (`bin/fleet-plan-review`'s own queue, claim and
    fencing machinery — never mocked here, unlike every worker-wake test
    above): only the two boundaries a test cannot cross for real are faked —
    this file's `FakeForge` stands in for Forgejo, and `dispatch_review` (a
    subprocess model call) is replaced with one that posts both ordinals'
    reviews directly onto the fake forge, the same shape a real
    `dispatch-review.py --post` round leaves behind.

    Two `_run_plan_review_worker` calls stand in for "the filing session
    ended, the worker restarted": each opens its own fresh connection and
    holder id against the SAME `fleet.db`, exactly as a separate resident-
    service process would after a restart — nothing but the database
    persists between them. The ordinal 2 outcome is a HIGH (posted as
    COMMENT, never APPROVED — D5: "A HIGH is terminal for this head"),
    proving the terminal `done` state is reached on a withheld verdict just
    as it is on an approval, with no further dispatch owed."""
    repo, pr, sha = "operator/x", 50, "f" * 40
    world.forge.set_pull(repo, pr, sha)
    world.forge.set_files(repo, pr, [{"filename": "plans/0050-x.md", "status": "added"}])
    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "pending"}])

    con = world.connect()
    con.execute(
        "CREATE TABLE IF NOT EXISTS repo_model (repo TEXT, position TEXT, "
        "ordinal INTEGER, runner TEXT, disabled INTEGER DEFAULT 0)")
    for ordinal in (1, 2):
        con.execute(
            "INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
            "VALUES (?, 'plan_reviewer', ?, 'sub-codex', 0)", (repo, ordinal))
    con.commit()
    DEPSD.planreview._enqueue(
        con, repo, pr, sha, actor="fleet-depsd",
        reason="webhook delivery: pull_request opened")
    con.close()

    # "the filing session ended, the worker restarted": CI is still pending,
    # so this pass holds the job rather than dispatching anything.
    DEPSD._run_plan_review_worker(str(world.db))
    con = world.connect()
    state, reason = con.execute(
        "SELECT state, reason FROM plan_review_job WHERE repo=? AND pr=?",
        (repo, pr)).fetchone()
    con.close()
    assert state == "held" and "still running" in reason

    world.forge.set_ci(repo, sha, [{"context": "CI / tests", "status": "success"}])

    calls = []

    def fake_dispatch_review(repo_, pr_, sha_, plan_path, log=None, timeout=None):
        calls.append((repo_, pr_, sha_))
        world.forge.add_review(repo_, pr_, sha_, login="revbot", review_state="APPROVED")
        world.forge.add_review(repo_, pr_, sha_, login="rev2bot", review_state="COMMENT")
        return True, ""

    monkeypatch.setattr(DEPSD.planreview._orch_mod(), "dispatch_review", fake_dispatch_review)

    # "the worker restarted": a second, independent pass, now that CI is green.
    DEPSD._run_plan_review_worker(str(world.db))

    con = world.connect()
    state, reason = con.execute(
        "SELECT state, reason FROM plan_review_job WHERE repo=? AND pr=?",
        (repo, pr)).fetchone()
    con.close()
    assert state == "done"
    assert "ordinal 1: approved" in reason
    assert "ordinal 2: not approved" in reason
    assert len(calls) == 1   # one spend, not re-dispatched for the HIGH seat


# ----------------------------------------------------------- resident wiring

def test_review_dirty_is_a_separate_flag_from_dependency_dirty(world):
    """Boundaries: "do not replace dependency recomputation with review
    work" — the two triggers must stay independent flags, each woken by its
    own source, so a slow review pass can never suppress or stand in for a
    pending dependency recompute."""
    ctx = DEPSD._Ctx(str(world.db), "127.0.0.1")
    assert ctx.dirty is not ctx.review_dirty
    ctx.review_dirty.set()
    assert not ctx.dirty.is_set()
