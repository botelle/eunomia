"""fleet-rebase — an owner can rebase a stale pull request from a comment (plan 0115),
and re-land an approved one that no longer merges (plan 0129).

The forge remote is a local bare repository, the ledger is a temporary
directory, and `fleetforge` is a stub with the same `(status, body)` contract
`Forge` promises. Everything else — the clone, the rebase, the lease push — is
real git, because the failure modes this verb guards are git's.
"""
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "operator/eunomia"
BRANCH = "feat/x"
UID = "7"

AUTHOR_ENV = {"GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "alice@example.org",
              "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "alice@example.org"}


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


def git(cwd, *args, env=None):
    e = dict(os.environ, **AUTHOR_ENV)
    e.update(env or {})
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd,
                          env=e, capture_output=True, text=True, check=True).stdout.strip()


def commit_file(work, path, text, msg):
    p = Path(work) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(work, "add", path)
    git(work, "commit", "-q", "-m", msg)


class Remote:
    """A bare repo plus a seed worktree to move it around with."""

    def __init__(self, root):
        self.bare = Path(root) / "remotes" / f"{REPO}.git"
        self.bare.parent.mkdir(parents=True)
        git(root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.work = Path(root) / "seed"
        git(root, "clone", "-q", str(self.bare), str(self.work))
        git(self.work, "checkout", "-q", "-b", "main")
        commit_file(self.work, "a.txt", "one\n", "A")
        commit_file(self.work, "plans/p.md", "plan\n", "A plan")
        git(self.work, "push", "-q", "origin", "main")
        git(self.work, "checkout", "-q", "-b", BRANCH)
        commit_file(self.work, "feature.txt", "feature\n", "B")
        git(self.work, "push", "-q", "origin", BRANCH)

    def advance_main(self, path, text="moved\n"):
        git(self.work, "checkout", "-q", "main")
        commit_file(self.work, path, text, f"main: {path}")
        git(self.work, "push", "-q", "origin", "main")
        git(self.work, "checkout", "-q", BRANCH)

    def head(self, branch=BRANCH):
        return git(self.bare, "rev-parse", f"refs/heads/{branch}")

    def main(self):
        return git(self.bare, "rev-parse", "refs/heads/main")


class FakeForge:
    def __init__(self, remote):
        self.remote = remote
        self.reset_pull()
        self.review_calls = 0
        self.on_review_call = None
        self.reviews = []
        self.pulls = []
        self.commits = []
        self.comments = []
        self.posted = []
        self.since = []
        self.calls = []
        self.by_n = {}
        self.requests = []

    def _request(self, method, path, query=None, body=None):
        self.requests.append((method, path, body))
        return 200, {}

    def reset_pull(self):
        self.pull = {"number": 7, "state": "open", "merged": False,
                     "title": "Add x", "body": "no marker",
                     "base": {"ref": "main"},
                     "head": {"ref": BRANCH, "sha": self.remote.head(),
                              "repo": {"full_name": REPO}}}

    def get_pull(self, repo, n):
        self.calls.append("get_pull")
        if n in self.by_n:
            return 200, self.by_n[n]
        return 200, self.pull

    def list_reviews(self, repo, n, page=1, limit=50):
        self.calls.append("list_reviews")
        if page > 1:
            return 200, []
        self.review_calls += 1
        if self.on_review_call:
            self.on_review_call(self.review_calls)
        reviews = self.reviews
        if callable(reviews):
            reviews = reviews(self.review_calls)
        return 200, reviews

    def list_pulls(self, repo, state="open", page=1, limit=50, sort=None):
        self.calls.append("list_pulls")
        return 200, ([] if page > 1 else self.pulls)

    def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
        self.calls.append("list_commits")
        return 200, self.commits

    def list_issue_comments(self, repo, n, since=None, page=1, limit=50):
        self.calls.append("list_issue_comments")
        self.since.append(since)
        return 200, self.comments

    def create_issue_comment(self, repo, n, body):
        self.calls.append("create_issue_comment")
        self.posted.append((n, body))
        return 201, {}


@pytest.fixture
def fr(monkeypatch, tmp_path):
    fleet = tmp_path / "fleet"
    (fleet / "leases").mkdir(parents=True)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(fleet))
    monkeypatch.setenv("FLEET_GIT_SSH_BASE", str(tmp_path / "remotes"))
    for k in ("FLEET_COMMIT_IDENTITY", "FLEET_AUTHOR_ACCOUNT", "FLEET_WATCH_REPOS",
              "FLEET_OPERATOR_UID", "FLEET_IMPLEMENTER_UIDS"):
        monkeypatch.delenv(k, raising=False)
    tmpd = tmp_path / "tmp"
    tmpd.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpd))
    return _load("fleet_rebase", BIN / "fleet-rebase")


@pytest.fixture
def remote(tmp_path):
    r = Remote(tmp_path)
    r.advance_main("main.txt")
    return r


@pytest.fixture
def forge(fr, remote, monkeypatch):
    f = FakeForge(remote)
    monkeypatch.setattr(fr.watch, "_forge", lambda token: f)
    monkeypatch.setattr(fr, "_token", lambda: ("tok", ""))
    return f


def tmp_leftovers(tmp_path):
    return list((tmp_path / "tmp").iterdir())


def write_lease(name, *, repo=REPO, branch=BRANCH, note=None, age_min=0, ttl=60,
                state="active", rtype="branch"):
    from datetime import timedelta
    lib = _load("fleetlib_t", BIN / "fleetlib.py")
    when = lib.ts(lib.now_utc() - timedelta(minutes=age_min))
    rec = {"id": f"branch--{name}--001", "state": state, "holder": f"sess-{name}",
           "created": when, "activated": when, "ttl_minutes": ttl,
           "resource": {"type": rtype, "repo": repo, "branch": branch}}
    if note:
        rec["note"] = note
    (lib.leases_dir() / f"{rec['id']}.json").write_text(json.dumps(rec))
    return rec


# --- 1. the success case ----------------------------------------------------


def test_a_branch_behind_main_is_rebased_and_pushed(fr, forge, remote):
    old, main = remote.head(), remote.main()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is not None and out.kind == "rebased", msg
    new = remote.head()
    assert new != old and out.old == old and out.new == new
    assert git(remote.bare, "rev-parse", f"{new}~1") == main
    assert git(remote.bare, "log", "-1", "--format=%an <%ae>", new) == "Alice <alice@example.org>"
    assert git(remote.bare, "log", "-1", "--format=%cn <%ce>", new) == \
        "implbot <implbot@example.org>"


# --- 2. approval ------------------------------------------------------------


def approved(dismissed=False):
    return {"state": "APPROVED", "dismissed": dismissed,
            "user": {"login": "revbot"}}


def test_an_approval_refuses_before_any_clone(fr, forge, remote, tmp_path, monkeypatch):
    forge.reviews = [approved()]
    made = []
    monkeypatch.setattr(fr.tempfile, "mkdtemp",
                        lambda *a, **k: made.append(1) or tempfile.mkdtemp(*a, **k))
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "check 2" in msg
    assert not made and remote.head() == old


def test_a_dismissed_approval_does_not_refuse(fr, forge, remote):
    forge.reviews = [approved(dismissed=True)]
    out, msg = fr.run_one(forge, REPO, 7, dry_run=True)
    assert out is not None and out.kind == "would", msg


def test_an_approval_arriving_before_the_push_refuses(fr, forge, remote):
    forge.reviews = lambda n: [] if n == 1 else [approved()]
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "check 2" in msg
    assert forge.review_calls == 2
    assert remote.head() == old


# --- 3. sessions and the ledger ---------------------------------------------


def test_a_live_branch_lease_refuses(fr, forge, remote):
    write_lease("live")
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "check 3" in msg and "branch--live--001" in msg
    assert remote.head() == old


def test_a_dead_lease_does_not_refuse(fr, forge, remote):
    write_lease("dead", age_min=600, ttl=10)
    out, msg = fr.run_one(forge, REPO, 7, dry_run=True)
    assert out is not None, msg


def test_a_lease_on_another_branch_does_not_refuse(fr, forge, remote):
    write_lease("other", branch="feat/else")
    out, msg = fr.run_one(forge, REPO, 7, dry_run=True)
    assert out is not None, msg


def test_a_marked_pull_with_a_live_dispatch_lease_names_the_lease(fr, forge, remote):
    forge.pull["body"] = "Fixes it.\n\nPlan: 0042-thing\n"
    write_lease("orch", branch="plan-0042", note="plan:0042-thing")
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "check 3" in msg and "branch--orch--001" in msg
    assert "0042-thing" in msg
    assert remote.head() == old


def test_a_marked_pull_with_a_dead_dispatch_lease_does_not_refuse(fr, forge, remote):
    forge.pull["body"] = "Plan: 0042-thing\n"
    write_lease("orch", branch="plan-0042", note="plan:0042-thing", age_min=600, ttl=10)
    out, msg = fr.run_one(forge, REPO, 7, dry_run=True)
    assert out is not None, msg


def test_a_missing_ledger_refuses(fr, forge, remote):
    import shutil
    shutil.rmtree(fr.lib.leases_dir())
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "check 3" in msg and "ledger" in msg
    assert remote.head() == old


def test_one_unparseable_lease_file_refuses_though_the_rest_are_fine(fr, forge, remote):
    write_lease("fine", branch="feat/else")
    (fr.lib.leases_dir() / "branch--junk--001.json").write_text("{not json")
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "check 3" in msg and "ledger" in msg
    assert remote.head() == old


# --- 4. conflicts, cleanup ---------------------------------------------------


def test_a_conflict_refuses_names_the_path_and_leaves_no_clone(fr, forge, remote, tmp_path):
    # the branch edits a.txt; main edits it differently
    git(remote.work, "checkout", "-q", BRANCH)
    commit_file(remote.work, "a.txt", "branch edit\n", "C: branch edits a.txt")
    git(remote.work, "push", "-q", "origin", BRANCH)
    remote.advance_main("a.txt", "main edit\n")
    forge.reset_pull()
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "conflict" in msg and "`a.txt`" in msg
    assert remote.head() == old
    assert tmp_leftovers(tmp_path) == []


def test_the_clone_is_removed_after_success_too(fr, forge, remote, tmp_path):
    out, _ = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out.kind == "rebased"
    assert tmp_leftovers(tmp_path) == []


# --- 5. already on main -------------------------------------------------------


def test_a_head_already_on_main_pushes_nothing(fr, forge, remote):
    git(remote.work, "checkout", "-q", BRANCH)
    git(remote.work, "rebase", "-q", "main")
    git(remote.work, "push", "-q", "--force", "origin", BRANCH)
    forge.reset_pull()
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is not None and out.kind == "already" and "already on main" in msg
    assert remote.head() == old


# --- 6. the remote moves after the check ---------------------------------------


def test_a_head_that_moved_after_the_check_is_refused_by_the_lease(fr, forge, remote):
    moved = {}

    def move(n):
        if n == 2 and not moved:
            git(remote.work, "checkout", "-q", BRANCH)
            commit_file(remote.work, "late.txt", "late\n", "someone else pushed")
            git(remote.work, "push", "-q", "origin", BRANCH)
            moved["sha"] = remote.head()

    forge.on_review_call = move
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "push was refused" in msg
    assert remote.head() == moved["sha"]            # their commit survives


# --- 7. check 1 -----------------------------------------------------------------


@pytest.mark.parametrize("mutate,word", [
    (lambda p: p["head"].update(repo={"full_name": "someone/eunomia"}), "fork"),
    (lambda p: p.update(state="closed"), "open"),
    (lambda p: p.update(state="closed", merged=True), "merged"),
    (lambda p: p["base"].update(ref="release"), "base"),
])
def test_check_one_refusals(fr, forge, remote, mutate, word):
    mutate(forge.pull)
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False)
    assert out is None and "check 1" in msg and word in msg
    assert remote.head() == old


# --- 8. dry run ---------------------------------------------------------------------


def test_dry_run_changes_and_posts_nothing(fr, forge, remote):
    old = remote.head()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=True)
    assert out.kind == "would" and out.old == old and out.new != old
    assert remote.head() == old
    assert forge.posted == []


# --- 9. the sweep ---------------------------------------------------------------------


def _comment(cid, body="rebase", uid=int(UID), at="2026-10-01T10:00:00Z", login="operator"):
    return {"id": cid, "body": body, "created_at": at, "user": {"id": uid, "login": login}}


@pytest.fixture
def swept(fr, forge, monkeypatch):
    monkeypatch.setenv("FLEET_WATCH_REPOS", REPO)
    forge.pulls = [{"number": 7, "head": {"sha": forge.pull["head"]["sha"]}}]
    forge.commits = [{"commit": {"author": {"date": "2026-10-01T09:00:00Z"},
                                 "committer": {"date": "2026-10-01T09:30:00Z"}}}]
    return forge


def test_the_sweep_acts_on_a_rebase_comment_and_replies_once(fr, swept, remote):
    old = remote.head()
    swept.comments = [_comment(11)]
    assert fr.sweep("tok", UID, dry_run=False) == 0
    assert remote.head() != old
    assert len(swept.posted) == 1
    n, body = swept.posted[0]
    assert n == 7 and body.splitlines()[0] == "rebase: 11"
    assert old in body and remote.head() in body


def test_the_sweep_survives_a_minos_signature_line(fr, swept, remote):
    old = remote.head()
    swept.comments = [_comment(12, "`rebase`\n\n_via minos (abc.access)_")]
    fr.sweep("tok", UID, dry_run=False)
    assert remote.head() != old and len(swept.posted) == 1


@pytest.mark.parametrize("comment", [
    _comment(13, uid=99),                                        # not the owner
    _comment(14, "please rebase it"),                             # not a whole line
    _comment(15, at="2026-10-01T09:00:00Z"),                      # older than the head
])
def test_the_sweep_ignores(fr, swept, remote, comment):
    old = remote.head()
    swept.comments = [comment]
    fr.sweep("tok", UID, dry_run=False)
    assert remote.head() == old and swept.posted == []


def test_the_sweep_ignores_a_comment_that_already_has_a_reply(fr, swept, remote):
    old = remote.head()
    swept.comments = [_comment(16),
                      _comment(17, "rebase: 16\nrebased", uid=5, login="implbot",
                               at="2026-10-01T10:01:00Z")]
    fr.sweep("tok", UID, dry_run=False)
    assert remote.head() == old and swept.posted == []


def test_the_sweep_replies_once_to_a_refusal_too(fr, swept, remote):
    swept.reviews = [approved()]
    old = remote.head()
    swept.comments = [_comment(18)]
    fr.sweep("tok", UID, dry_run=False)
    assert remote.head() == old
    assert len(swept.posted) == 1
    body = swept.posted[0][1]
    assert body.splitlines()[0] == "rebase: 18" and "refused" in body and "check 2" in body


def test_a_rebased_head_whose_author_date_is_old_is_not_acted_on_again(fr, swept, remote):
    swept.comments = [_comment(19, at="2026-10-01T10:00:00Z")]
    fr.sweep("tok", UID, dry_run=False)
    assert len(swept.posted) == 1
    # The rebase keeps the author date (before the comment) and moves the
    # committer date (after it). The bound must follow the committer's.
    swept.commits = [{"commit": {"author": {"date": "2026-10-01T09:00:00Z"},
                                 "committer": {"date": "2026-10-01T10:05:00Z"}}}]
    fr.sweep("tok", UID, dry_run=False)
    assert len(swept.posted) == 1
    assert swept.since[-1] == "2026-10-01T10:05:00Z"


def test_the_sweep_dry_run_posts_nothing_and_leaves_the_remote(fr, swept, remote, capsys):
    old = remote.head()
    swept.comments = [_comment(20)]
    assert fr.sweep("tok", UID, dry_run=True) == 0
    assert remote.head() == old and swept.posted == []
    assert "sweeping 1 repositories" in capsys.readouterr().out


# --- 10. plans only -----------------------------------------------------------------------


def test_a_main_that_moved_only_in_plans_is_refused(fr, tmp_path, monkeypatch):
    r = Remote(tmp_path)
    r.advance_main("plans/0200-new.md")
    f = FakeForge(r)
    monkeypatch.setattr(fr.watch, "_forge", lambda token: f)
    old = r.head()
    out, msg = fr.run_one(f, REPO, 7, dry_run=False)
    assert out is None and "plans-only" in msg and "re-run CI" in msg
    assert r.head() == old
    assert tmp_leftovers(tmp_path) == []


# --- 11. one instance, and config ------------------------------------------------------------


def test_a_second_instance_exits_nonzero_without_reading(fr, forge, remote, capsys):
    lock = fr.lib.locks_dir()
    lock.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock / "fleet-rebase.lock"), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert fr.main([REPO, "7"]) != 0
    finally:
        os.close(fd)
    assert "another fleet-rebase is running" in capsys.readouterr().err
    assert forge.calls == []


@pytest.mark.parametrize("env,name", [
    ({"FLEET_OPERATOR_UID": UID}, "repos.conf"),
    ({"FLEET_WATCH_REPOS": REPO}, "FLEET_OPERATOR_UID"),
])
def test_the_sweep_refuses_to_start_unconfigured(fr, forge, monkeypatch, capsys, env, name):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert fr.main(["--sweep"]) != 0
    assert name in capsys.readouterr().err
    assert forge.calls == []


def test_the_command_line_runs_the_verb(fr, forge, remote, capsys):
    old = remote.head()
    assert fr.main([REPO, "7"]) == 0
    out = capsys.readouterr().out
    assert old in out and remote.head() in out
    assert remote.head() != old


def test_the_command_line_exits_nonzero_on_a_refusal(fr, forge, remote, capsys):
    forge.reviews = [approved()]
    assert fr.main([REPO, "7"]) == 1
    assert "check 2" in capsys.readouterr().out


# --- follow-ups to #753's deferred findings ---------------------------------------------------


def test_a_differently_cased_repo_name_still_meets_a_live_lease(fr, forge, remote):
    write_lease("live")
    out, msg = fr.run_one(forge, REPO.upper(), 7, dry_run=True)
    assert out is None and "check 3" in msg


def test_a_plans_path_with_a_space_is_not_misread(fr, tmp_path, monkeypatch):
    r = Remote(tmp_path)
    r.advance_main("plans/0200 new.md")
    f = FakeForge(r)
    out, msg = fr.run_one(f, REPO, 7, dry_run=True)
    assert out is None and "plans-only" in msg and "`CI / tests`" in msg


# --- 12. reland (plan 0129) ------------------------------------------------------------------


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Stand-ins for ~/bin/forgejo-safe-push.sh and forgejo-open-pr.sh, which
    log their argv. The safe-push stand-in pushes for real, so the remote can be
    inspected; the opener prints the line the real one prints."""
    log = tmp_path / "tools.log"
    sp = tmp_path / "safe-push"
    sp.write_text(f'#!/bin/sh\necho "safe-push $*" >> {log}\nexec git push --quiet "$@"\n')
    op = tmp_path / "open-pr"
    op.write_text(f'#!/bin/sh\necho "open-pr $1 $2 $3 $4" >> {log}\n'
                  f'cp "$5" {tmp_path}/opened-body.md\n'
                  'echo "$1#9 by implbot — http://forge/$1/pulls/9"\n')
    for f in (sp, op):
        f.chmod(0o755)
    monkeypatch.setenv("FLEET_SAFE_PUSH_CMD", str(sp))
    monkeypatch.setenv("FLEET_OPEN_PR_CMD", str(op))
    return log


def test_an_approved_pr_is_relanded_onto_a_fresh_branch(fr, forge, remote, tools, tmp_path):
    forge.reviews = [approved()]
    forge.pull["body"] = "Does x.\n\nPlan: 0042-thing\n"
    old, main = remote.head(), remote.main()
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False, verb="reland")
    assert out is not None and out.kind == "relanded", msg
    new = remote.head(f"{BRANCH}-relanded-1")
    assert git(remote.bare, "rev-parse", f"{new}~1") == main
    assert remote.head() == old                       # the approved branch is untouched
    assert out.pr == 9 and out.branch == f"{BRANCH}-relanded-1"
    log = tools.read_text().splitlines()
    assert log[0] == f"safe-push origin {BRANCH}-relanded-1"
    assert log[1].startswith(f"open-pr {REPO} {BRANCH}-relanded-1 main")
    opened = (tmp_path / "opened-body.md").read_text()
    assert opened.startswith("Follows: #7") and "Does x." in opened
    assert not fr._PLAN_RE.search(opened) and "0042-thing" not in opened


def test_the_sweep_relands_on_a_comment_and_replies_once_leaving_the_old_open(
        fr, swept, remote, tools):
    swept.reviews = [approved()]
    swept.comments = [_comment(30, "reland")]
    assert fr.sweep("tok", UID, dry_run=False) == 0
    assert len(swept.posted) == 1
    n, body = swept.posted[0]
    assert n == 7 and body.splitlines()[0] == "reland: 30 → #9"
    assert swept.requests == []                       # nothing closed
    assert git(remote.bare, "rev-parse", f"refs/heads/{BRANCH}-relanded-1")


def test_an_unapproved_pr_is_refused_and_the_refusal_names_rebase(fr, forge, remote, tools):
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False, verb="reland")
    assert out is None and "check 2" in msg and "`rebase`" in msg
    assert not tools.exists()


def test_a_conflicting_replay_pushes_nothing_and_lists_the_paths(fr, forge, remote, tools):
    git(remote.work, "checkout", "-q", BRANCH)
    commit_file(remote.work, "a.txt", "branch edit\n", "C: branch edits a.txt")
    git(remote.work, "push", "-q", "origin", BRANCH)
    remote.advance_main("a.txt", "main edit\n")
    forge.reset_pull()
    forge.reviews = [approved()]
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False, verb="reland")
    assert out is None and "`a.txt`" in msg and "needs a session" in msg
    assert not tools.exists()
    assert "refs/heads/feat/x-relanded-1" not in git(remote.bare, "for-each-ref")


def test_a_live_lease_refuses_a_reland(fr, forge, remote, tools):
    forge.reviews = [approved()]
    write_lease("live")
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False, verb="reland")
    assert out is None and "check 3" in msg and not tools.exists()


def test_n_increments_when_relanded_1_exists(fr, forge, remote, tools):
    forge.reviews = [approved()]
    git(remote.work, "push", "-q", "origin", f"{BRANCH}:refs/heads/{BRANCH}-relanded-1")
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False, verb="reland")
    assert out is not None and out.branch == f"{BRANCH}-relanded-2", msg


def test_the_push_goes_through_the_guard_and_a_refusal_stops_everything(
        fr, forge, remote, tools, tmp_path, monkeypatch):
    forge.reviews = [approved()]
    bad = tmp_path / "refuse"
    bad.write_text("#!/bin/sh\necho refused by guard >&2\nexit 1\n")
    bad.chmod(0o755)
    monkeypatch.setenv("FLEET_SAFE_PUSH_CMD", str(bad))
    out, msg = fr.run_one(forge, REPO, 7, dry_run=False, verb="reland")
    assert out is None and "refused by guard" in msg
    assert "relanded" not in git(remote.bare, "for-each-ref")
    assert not tools.exists()                         # no PR opened


def test_reland_dry_run_creates_nothing(fr, forge, remote, tools):
    forge.reviews = [approved()]
    out, msg = fr.run_one(forge, REPO, 7, dry_run=True, verb="reland")
    assert out.kind == "would" and f"{BRANCH}-relanded-1" in msg
    assert "relanded" not in git(remote.bare, "for-each-ref")
    assert not tools.exists() and forge.posted == []


def _with_reply(swept, new_state):
    swept.comments = [_comment(40, "reland: 31 → #9", uid=5, login="implbot",
                               at="2026-10-01T10:01:00Z")]
    swept.by_n[9] = {"number": 9, "state": new_state[0], "merged": new_state[1]}


def test_the_sweep_closes_the_old_pr_only_after_the_replacement_merges(fr, swept, remote):
    _with_reply(swept, ("open", False))
    fr.sweep("tok", UID, dry_run=False)
    assert swept.requests == [] and swept.posted == []
    _with_reply(swept, ("closed", False))             # closed unmerged: do nothing
    fr.sweep("tok", UID, dry_run=False)
    assert swept.requests == [] and swept.posted == []
    _with_reply(swept, ("closed", True))
    fr.sweep("tok", UID, dry_run=False)
    assert swept.requests == [("PATCH", f"/repos/{REPO}/pulls/7", {"state": "closed"})]
    assert len(swept.posted) == 1 and "#9" in swept.posted[0][1]


def test_the_command_line_takes_reland(fr, forge, remote, tools, capsys):
    forge.reviews = [approved()]
    assert fr.main([REPO, "7", "--reland", "--dry-run"]) == 0
    assert "would push" in capsys.readouterr().out
