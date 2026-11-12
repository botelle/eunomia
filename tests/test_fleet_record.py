"""Tests for fleet-record (plan 0119): a fake forge on 127.0.0.1 and a local
bare git repository standing in for operator/recordstore."""
import base64
import datetime as dt
import http.server
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "demo/app"
CLEAN_ENV = ("FLEET_AUTHOR_ACCOUNT", "FLEET_REVIEWER_ACCOUNT", "FLEET_MERGER_ACCOUNT",
             "FLEET_AGENT_ACCOUNTS", "FLEET_IMPLEMENTER_LOGINS", "FLEET_RECORD_WINDOW",
             "FLEET_ATTEST_SPOOL", "FLEET_RECORDER_ACCOUNT")


def _pr(n, merged_at="2026-10-02T10:00:00Z", body="", merge_sha=None, **kw):
    pr = {"number": n, "title": f"pr {n}", "body": body, "merged": True,
          "merged_at": merged_at, "updated_at": merged_at, "created_at": merged_at,
          "merge_commit_sha": merge_sha or f"{n:040x}",
          "base": {"ref": "main"}, "head": {"sha": f"{n + 1000:040x}"},
          "user": {"login": "implbot"}, "merged_by": {"login": "operator"}}
    pr.update(kw)
    return pr


class World:
    """What the fake forge answers; tests mutate it between passes."""
    def __init__(self):
        self.login = "recordbot"
        self.pulls = []
        self.reviews = {}
        self.files = {}
        self.statuses = {}
        self.branch = {"enable_status_check": False, "status_check_contexts": []}
        self.plan_text = "---\nid: 0105-a-thing\nstatus: ready\n---\n"
        self.auth_seen = []
        self.requests = []
        self.history = {}
        self.default_branch = "main"


def _serve(world):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=None):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            path = u.path[len("/api/v1"):]
            world.auth_seen.append(self.headers.get("Authorization"))
            world.requests.append(path)
            page, limit = int(q.get("page", 1)), int(q.get("limit", 50))

            def paged(items):
                return self._send(200, items[(page - 1) * limit: page * limit])
            if path == "/user":
                return self._send(200, {"login": world.login})
            pre = f"/repos/{REPO}"
            if path == f"{pre}/pulls":
                return paged(world.pulls)
            if path.startswith(f"{pre}/pulls/"):
                n, what = path[len(pre) + 7:].split("/")
                src = world.reviews if what == "reviews" else world.files
                return paged(src.get(int(n), []))
            if path == pre:
                return self._send(200, {"default_branch": world.default_branch})
            if path == f"{pre}/branches/{world.default_branch}":
                return self._send(200, world.branch)
            if path.startswith(f"{pre}/commits/") and path.endswith("/status"):
                sha = path.split("/")[-2]
                return self._send(200, {"state": "success",
                                        "statuses": world.statuses.get(sha, [])})
            if path == f"{pre}/commits":
                if q.get("path"):
                    return self._send(200, [{"sha": "c" * 40}])
                if q["sha"] in world.history:
                    return self._send(200, world.history[q["sha"]])
                return self._send(200, [{"sha": q["sha"], "parents": [{"sha": "f" * 40}]}])
            if path.startswith(f"{pre}/contents/plans/"):
                if q.get("ref") != "f" * 40 or path.endswith("missing.md"):
                    return self._send(404, None)
                return self._send(200, {"content": base64.b64encode(
                    world.plan_text.encode()).decode()})
            return self._send(501, None)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _sh(*args, cwd=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="seed", GIT_AUTHOR_EMAIL="s@x",
               GIT_COMMITTER_NAME="seed", GIT_COMMITTER_EMAIL="s@x")
    return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for k in CLEAN_ENV:
        monkeypatch.delenv(k, raising=False)
    world = World()
    srv = _serve(world)
    bare = tmp_path / "evidence.git"
    _sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _sh("git", "clone", "-q", str(bare), str(seed))
    (seed / "README.md").write_text("evidence\n")
    _sh("git", "add", "-A", cwd=seed)
    _sh("git", "commit", "-q", "-m", "init", cwd=seed)
    _sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    # a fake ssh on PATH: prints the greeting held in the `greeting` file
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (tmp_path / "greeting").write_text("Hi there, recordbot! You've successfully authenticated.\n")
    ssh = fakebin / "ssh"
    ssh.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/ssh-args"\n'
                   f'cat "{tmp_path}/greeting"\nexit $(cat "{tmp_path}/ssh-exit" 2>/dev/null || echo 0)\n')
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("FLEET_RECORD_SSH_HOST", raising=False)
    cfg = tmp_path / "config-cmd"
    cfg.write_text("#!/bin/sh\n"
                   "echo '{\"key\": \"autonomy\", \"value\": \"autonomous\", \"layer\": \"builtin\"}'\n")
    cfg.chmod(0o755)
    spool = tmp_path / "spool"
    spool.mkdir()
    for k, v in {"FLEET_FORGEJO_URL": f"http://127.0.0.1:{srv.server_port}",
                 "FLEET_RECORD_TOKEN_CMD": "echo recorder-token",
                 "FLEET_RECORD_CLONE": str(tmp_path / "clone"),
                 "FLEET_RECORD_REMOTE": str(bare),
                 "FLEET_WATCH_REPOS": REPO,
                 "FLEET_RECORD_CONFIG_CMD": str(cfg),
                 "FLEET_ATTEST_SPOOL": str(spool)}.items():
        monkeypatch.setenv(k, v)
    loader = importlib.machinery.SourceFileLoader("fleet_record", str(BIN / "fleet-record"))
    spec = importlib.util.spec_from_loader("fleet_record", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    pinned = tmp_path / "pin-repos.conf"
    pinned.write_text(f"repo: {REPO} dispatch\n")
    mod.PINNED_CONF = pinned
    pages = []
    mod.page = lambda title, body: pages.append((title, body))
    clock = {"now": dt.datetime(2026, 10, 2, 10, 5, tzinfo=dt.timezone.utc)}
    mod._now = lambda: clock["now"]

    class Rig:
        pass
    r = Rig()
    r.greeting, r.ssh_exit, r.ssh_args = (tmp_path / "greeting", tmp_path / "ssh-exit",
                                         tmp_path / "ssh-args")
    r.world, r.mod, r.pages, r.clock, r.bare, r.spool = world, mod, pages, clock, bare, spool

    def upstream():
        d = tmp_path / "view"
        if d.exists():
            _sh("git", "-C", str(d), "pull", "-q", "origin", "main")
        else:
            _sh("git", "clone", "-q", str(bare), str(d))
        return d
    r.upstream = upstream
    r.records = lambda: sorted(p.relative_to(upstream()).as_posix()
                               for p in (upstream() / "records").rglob("*.json")) \
        if (upstream() / "records").exists() else []
    r.backfill = lambda: mod.main(["backfill", "--since", "2026-09-29"])
    yield r
    srv.shutdown()


def _load(rig, path):
    return json.loads((rig.upstream() / path).read_text())


def test_merged_pr_produces_one_valid_record_at_the_documented_path(rig):
    # the PR that carried the plan file's last commit; not a main merge, so it
    # is never recorded itself, only looked up for its merger
    rig.world.pulls = [_pr(1, body="Plan: 0105-a-thing\nmore"),
                       _pr(2, merge_sha="c" * 40, base={"ref": "plans"})]
    rig.world.files = {1: [{"filename": "bin/x"}, {"filename": "docs/y.md"}]}
    rig.world.reviews = {1: [{"id": 9, "user": {"login": "revbot"}, "state": "APPROVED",
                              "commit_id": "abc", "submitted_at": "t", "dismissed": False,
                              "stale": False}]}
    rig.world.statuses = {f"{1001:040x}": [{"context": "ci", "status": "success",
                                            "creator": {"login": "ci-bot"}, "created_at": "t"}]}
    assert rig.backfill() == 0
    assert rig.mod.main(["once"]) == 0
    path = f"records/{REPO}/2026/10/{1:040x}.json"
    assert rig.records() == [path]
    rec = _load(rig, path)
    assert rig.mod.validate(rec, rig.mod.load_schema()) == []
    assert rec["files"] == ["bin/x", "docs/y.md"]
    assert rec["plan"] == {"id": "0105-a-thing", "status_at_merge": "ready",
                           "commit": "c" * 40, "commit_merged_by": "operator"}
    assert rec["statuses"][0]["creator"] == "ci-bot"
    assert rec["autonomy"] == "autonomous"
    assert rec["merged_by"] == "operator" and rec["author"] == "implbot"
    assert set(rig.world.auth_seen) == {"token recorder-token"}


def test_second_once_writes_nothing(rig, capsys):
    rig.world.pulls = [_pr(1)]
    rig.backfill()
    rig.mod.main(["once"])
    head = _sh("git", "-C", str(rig.bare), "rev-parse", "main")
    capsys.readouterr()
    assert rig.mod.main(["once"]) == 0
    assert _sh("git", "-C", str(rig.bare), "rev-parse", "main") == head
    assert "recorded" not in capsys.readouterr().out


def test_existing_record_is_never_rewritten(rig):
    rig.world.pulls = [_pr(1)]
    rig.world.reviews = {1: [{"id": 9, "user": {"login": "r"}, "state": "APPROVED",
                              "dismissed": False, "stale": False}]}
    rig.backfill()
    path = f"records/{REPO}/2026/10/{1:040x}.json"
    before = (rig.upstream() / path).read_text()
    rig.world.reviews[1][0]["dismissed"] = True
    rig.world.pulls[0]["title"] = "renamed"
    rig.mod.main(["once"])
    rig.mod.main(["backfill", "--since", "2026-09-29"])
    assert (rig.upstream() / path).read_text() == before
    assert json.loads(before)["reviews"][0]["dismissed"] is False


def test_source_observed_late_and_backfill(rig):
    rig.world.pulls = [_pr(1, merged_at="2026-10-02T09:00:00Z"),
                       _pr(2, merged_at="2026-10-02T10:00:00Z")]
    rig.backfill()
    assert {_load(rig, p)["source"] for p in rig.records()} == {"backfill"}
    rig.clock["now"] = dt.datetime(2026, 10, 3, 10, 0, tzinfo=dt.timezone.utc)
    rig.world.pulls += [_pr(3, merged_at="2026-10-03T09:50:00Z"),   # 10 min: inside
                        _pr(4, merged_at="2026-10-03T09:00:00Z")]   # 60 min: outside
    rig.mod._now = lambda: rig.clock["now"]
    rig.mod.main(["once"])
    got = {p.rsplit("/", 1)[1]: _load(rig, p)["source"] for p in rig.records()}
    assert got[f"{3:040x}.json"] == "observed"
    assert got[f"{4:040x}.json"] == "late"
    assert got[f"{1:040x}.json"] == "backfill"


def test_once_never_labels_a_slow_catchup_observed(rig):
    rig.world.pulls = []
    rig.backfill()
    rig.clock["now"] = dt.datetime(2026, 10, 9, 10, 0, tzinfo=dt.timezone.utc)
    rig.world.pulls = [_pr(5, merged_at="2026-10-05T10:00:00Z")]
    rig.mod.main(["once"])
    assert _load(rig, rig.records()[0])["source"] == "late"


def test_plan_marker_is_line_anchored_and_whole_id(rig):
    m = rig.mod.PLAN_MARKER
    assert m.search("Plan: 0105\n").group(1) == "0105"
    assert m.search("Plan: 0105-a-thing").group(1) == "0105-a-thing"
    assert m.search("Plan: 0105").group(1) != "0105-a-thing"
    assert m.search("see Plan: 0105-a") is None
    rig.world.pulls = [_pr(1, body="Plan: 0105"), _pr(2, body="Plan: 0105-a-thing")]
    rig.backfill()
    plans = {_load(rig, p)["pr"]: _load(rig, p)["plan"]["id"] for p in rig.records()}
    assert plans == {1: "0105", 2: "0105-a-thing"}


def test_no_marker_means_null_plan(rig):
    rig.world.pulls = [_pr(1, body="no marker")]
    rig.backfill()
    assert _load(rig, rig.records()[0])["plan"] is None


def test_review_without_provenance_has_null_model_and_session(rig):
    rig.world.pulls = [_pr(1)]
    rig.world.reviews = {1: [
        {"id": 1, "user": {"login": "revbot"}, "state": "APPROVED", "body": "lgtm"},
        {"id": 2, "user": {"login": "revbot"}, "state": "APPROVED",
         "body": "x\nReview-provenance: model=opus-5; session=12345678-aaaa\n"}]}
    rig.backfill()
    a, b = _load(rig, rig.records()[0])["reviews"]
    assert (a["model"], a["session_uuid"]) == (None, None)
    assert (b["model"], b["session_uuid"]) == ("opus-5;", "12345678-aaaa") or b["model"] == "opus-5"


def test_identity_mismatch_writes_nothing_and_pages(rig):
    rig.world.pulls = [_pr(1)]
    rig.backfill()
    head = _sh("git", "-C", str(rig.bare), "rev-parse", "main")
    rig.world.pulls.append(_pr(2))
    rig.world.login = "someone-else"
    assert rig.mod.main(["once"]) == 1
    assert _sh("git", "-C", str(rig.bare), "rev-parse", "main") == head
    assert rig.pages and "identity" in rig.pages[-1][0]


def test_recorder_in_agent_roles_writes_nothing_and_pages(rig, monkeypatch):
    monkeypatch.setenv("FLEET_RECORDER_ACCOUNT", "implbot")
    rig.world.login = "implbot"
    rig.world.pulls = [_pr(1)]
    head = _sh("git", "-C", str(rig.bare), "rev-parse", "main")
    assert rig.mod.main(["backfill", "--since", "2026-09-29"]) == 1
    assert _sh("git", "-C", str(rig.bare), "rev-parse", "main") == head
    assert rig.pages and "identity" in rig.pages[-1][0]


def test_upstream_record_appearing_before_push_is_dropped_and_retry_succeeds(rig):
    rig.world.pulls = [_pr(1), _pr(2)]
    rig.backfill()
    rig.clock["now"] = dt.datetime(2026, 10, 2, 10, 10, tzinfo=dt.timezone.utc)
    rig.world.pulls += [_pr(3, merged_at="2026-10-02T10:06:00Z"),
                        _pr(4, merged_at="2026-10-02T10:07:00Z")]
    real_git, state = rig.mod.git, {"raced": False}

    def racing(*args, **kw):
        if args[0] == "push" and not state["raced"]:
            state["raced"] = True
            other = rig.bare.parent / "other"
            _sh("git", "clone", "-q", str(rig.bare), str(other))
            p = other / f"records/{REPO}/2026/10/{3:040x}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"from": "elsewhere"}\n')
            _sh("git", "add", "-A", cwd=other)
            _sh("git", "commit", "-q", "-m", "race", cwd=other)
            _sh("git", "push", "-q", "origin", "HEAD:main", cwd=other)
        return real_git(*args, **kw)
    rig.mod.git = racing
    assert rig.mod.main(["once"]) == 0
    assert rig.pages == []
    assert _load(rig, f"records/{REPO}/2026/10/{3:040x}.json") == {"from": "elsewhere"}
    assert _load(rig, f"records/{REPO}/2026/10/{4:040x}.json")["pr"] == 4


def test_push_still_rejected_after_retry_pages(rig):
    rig.world.pulls = [_pr(1)]
    rig.backfill()
    rig.clock["now"] = dt.datetime(2026, 10, 2, 10, 10, tzinfo=dt.timezone.utc)
    rig.world.pulls.append(_pr(2, merged_at="2026-10-02T10:06:00Z"))
    real_git = rig.mod.git
    rig.mod.git = lambda *a, **k: (subprocess.CompletedProcess(a, 1, "", "rejected")
                                   if a[0] == "push" else real_git(*a, **k))
    assert rig.mod.main(["once"]) == 1
    assert any("push rejected" in t for t, _ in rig.pages)


def test_record_missing_a_required_field_is_refused_before_any_write(rig):
    rig.world.pulls = [_pr(1)]
    real = rig.mod.build_record
    rig.mod.build_record = lambda *a: {k: v for k, v in real(*a).items() if k != "autonomy"}
    head = _sh("git", "-C", str(rig.bare), "rev-parse", "main")
    rig.backfill()
    # nothing written at all: a repo with an unwritable record is not "finished"
    assert _sh("git", "-C", str(rig.bare), "rev-parse", "main") == head
    assert rig.records() == []
    assert any("validation" in t for t, _ in rig.pages)
    schema = rig.mod.load_schema()
    assert rig.mod.validate({"schema": 1}, schema)
    rec = real(rig.mod.make_forge(), REPO, _pr(1), rig.mod._backfill_source, "rev")
    rec["extra"] = 1
    assert rig.mod.validate(rec, schema)


def test_merge_at_plus_two_lands_in_the_utc_month(rig):
    rig.world.pulls = [_pr(1, merged_at="2026-10-01T00:30:00+02:00")]
    rig.backfill()
    assert rig.records() == [f"records/{REPO}/2026/09/{1:040x}.json"]


def test_once_skips_repo_without_backfill_and_then_records_only_later_merges(rig, capsys):
    rig.world.pulls = [_pr(1, merged_at="2026-10-01T10:00:00Z")]
    assert rig.mod.main(["once"]) == 0
    assert rig.records() == []
    assert "no backfill" in capsys.readouterr().out
    rig.clock["now"] = dt.datetime(2026, 10, 2, 0, 0, tzinfo=dt.timezone.utc)
    rig.backfill()
    assert len(rig.records()) == 1
    assert _load(rig, f"backfill/{REPO}.json")["ran_at"] == "2026-10-02T00:00:00Z"
    rig.clock["now"] = dt.datetime(2026, 10, 2, 5, 0, tzinfo=dt.timezone.utc)
    rig.world.pulls += [_pr(2, merged_at="2026-10-02T04:55:00Z")]
    rig.world.pulls[0]["merged_at"] = "2026-10-01T10:00:00Z"
    rig.mod.main(["once"])
    assert len(rig.records()) == 2


def test_backfill_is_idempotent(rig):
    rig.world.pulls = [_pr(1)]
    rig.backfill()
    head = _sh("git", "-C", str(rig.bare), "rev-parse", "main")
    rig.clock["now"] += dt.timedelta(days=1)
    assert rig.backfill() == 0
    assert _sh("git", "-C", str(rig.bare), "rev-parse", "main") == head


def test_required_contexts_from_branch_endpoint(rig):
    rig.world.pulls = [_pr(1), _pr(2, merged_at="2026-10-02T11:00:00Z")]
    rig.world.branch = {"enable_status_check": True, "status_check_contexts": ["ci/a", "ci/b"]}
    rig.backfill()
    assert _load(rig, f"records/{REPO}/2026/10/{1:040x}.json")["required_contexts"] == ["ci/a", "ci/b"]
    assert f"/repos/{REPO}/branches/main" in rig.world.requests
    assert not any("branch_protections" in r for r in rig.world.requests)


def test_required_contexts_empty_when_checks_disabled(rig):
    rig.world.pulls = [_pr(1)]
    rig.world.branch = {"enable_status_check": False, "status_check_contexts": ["stale"]}
    rig.backfill()
    assert _load(rig, rig.records()[0])["required_contexts"] == []


def test_publish_commits_reports_refuses_records_and_never_overwrites(rig):
    rig.world.pulls = []
    rig.backfill()
    (rig.spool / "reports/2026").mkdir(parents=True)
    (rig.spool / "reports/2026/10.md").write_text("report\n")
    (rig.spool / "records").mkdir()
    (rig.spool / "records/evil.json").write_text("{}")
    assert rig.mod.main(["publish"]) == 1
    view = rig.upstream()
    assert (view / "reports/2026/10.md").read_text() == "report\n"
    assert not (view / "records/evil.json").exists()
    assert not (rig.spool / "reports/2026/10.md").exists()      # removed after push
    assert not (rig.spool / "records/evil.json").exists()
    assert (rig.spool / "rejected/records/evil.json").exists()  # refused, moved
    assert any("spool path refused" in t for t, _ in rig.pages)
    (rig.spool / "reports/2026/10.md").write_text("DIFFERENT\n")
    assert rig.mod.main(["publish"]) == 1
    assert (rig.upstream() / "reports/2026/10.md").read_text() == "report\n"


def test_once_runs_publish(rig):
    rig.world.pulls = []
    rig.backfill()
    (rig.spool / "protection").mkdir()
    (rig.spool / "protection/snap.json").write_text("{}\n")
    assert rig.mod.main(["once"]) == 0
    assert (rig.upstream() / "protection/snap.json").exists()


def test_plan_commit_merger_found_for_a_merge_commit_merge(rig):
    # the plan file's last commit is a branch commit of PR 1, merged with a
    # merge commit: carried by 1's merge commit, not equal to any sha on a PR
    m1, branch_commit, p1 = f"{1:040x}", "c" * 40, "e" * 40
    rig.world.history = {
        m1: [{"sha": m1, "parents": [{"sha": p1}, {"sha": "d" * 40}]},
             {"sha": "d" * 40, "parents": [{"sha": branch_commit}]},
             {"sha": branch_commit, "parents": [{"sha": p1}]},
             {"sha": p1, "parents": []}],
        p1: [{"sha": p1, "parents": []}]}
    rig.world.pulls = [_pr(1, merged_at="2026-10-02T09:00:00Z", body="Plan: x",
                           head={"sha": "d" * 40}, merged_by={"login": "operator"}),
                       _pr(2, body="Plan: 0105-a-thing")]
    assert rig.backfill() == 0
    assert rig.mod.main(["once"]) == 0
    rec = _load(rig, f"records/{REPO}/2026/10/{2:040x}.json")
    assert rec["plan"]["commit"] == branch_commit
    assert rec["plan"]["commit_merged_by"] == "operator"


def _head(rig):
    return _sh("git", "-C", str(rig.bare), "rev-parse", "main")


def test_default_remote_is_the_ssh_alias(rig, monkeypatch):
    monkeypatch.delenv("FLEET_RECORD_REMOTE")
    assert rig.mod.remote_url() == "ssh://git-recordbot/operator/recordstore.git"
    monkeypatch.setenv("FLEET_RECORD_SSH_HOST", "other")
    assert rig.mod.remote_url() == "ssh://other/operator/recordstore.git"
    monkeypatch.setenv("FLEET_RECORD_REMOTE", "ssh://x/y.git")
    assert rig.mod.remote_url() == "ssh://x/y.git"


@pytest.mark.parametrize("greeting,exit_code", [
    ("Hi there, operator! You've successfully authenticated.\n", 0),
    ("", 0),
    ("Hi there, recordbot! ok\n", 1)])
def test_bad_ssh_greeting_writes_nothing_and_pages(rig, greeting, exit_code):
    rig.world.pulls = [_pr(1)]
    rig.backfill()
    head = _head(rig)
    rig.world.pulls.append(_pr(2))
    rig.greeting.write_text(greeting)
    rig.ssh_exit.write_text(str(exit_code))
    assert rig.mod.main(["once"]) == 1
    assert _head(rig) == head
    assert rig.pages and "identity" in rig.pages[-1][0]
    if greeting.startswith("Hi there, operator"):
        assert "operator" in rig.pages[-1][1]


def test_ssh_greeting_naming_the_recorder_lets_the_push_proceed(rig):
    rig.world.pulls = [_pr(1)]
    assert rig.backfill() == 0
    assert len(rig.records()) == 1
    assert "-T" in rig.ssh_args.read_text() and "git-recordbot" in rig.ssh_args.read_text()


def test_default_branch_master_is_recorded_and_other_bases_are_not(rig):
    rig.world.default_branch = "master"
    rig.world.pulls = [_pr(1, base={"ref": "master"}),
                       _pr(2, base={"ref": "main"}),
                       _pr(3, base={"ref": "feature"})]
    rig.backfill()
    assert rig.records() == [f"records/{REPO}/2026/10/{1:040x}.json"]
    assert _load(rig, rig.records()[0])["base_ref"] == "master"


def test_required_contexts_read_from_the_default_branch(rig):
    rig.world.default_branch = "master"
    rig.world.branch = {"enable_status_check": True, "status_check_contexts": ["ci/m"]}
    rig.world.pulls = [_pr(1, base={"ref": "master"})]
    rig.backfill()
    assert _load(rig, rig.records()[0])["required_contexts"] == ["ci/m"]
    assert f"/repos/{REPO}/branches/master" in rig.world.requests
    assert f"/repos/{REPO}/branches/main" not in rig.world.requests


def test_refused_spool_file_pages_once_and_lands_in_rejected(rig):
    rig.world.pulls = []
    rig.backfill()
    (rig.spool / "records").mkdir()
    (rig.spool / "records/evil.json").write_text("{}")
    assert rig.mod.main(["publish"]) == 1
    assert rig.mod.main(["publish"]) == 0
    refused = [t for t, _ in rig.pages if "spool path refused" in t]
    assert len(refused) == 1
    assert (rig.spool / "rejected/records/evil.json").exists()
    assert not (rig.spool / "records/evil.json").exists()


# plan 0136 D3 -- the list is the pin's, wherever the run starts -----------------

def _checkout_with_other_list(tmp_path, monkeypatch):
    """A checkout (what BIN.parent and the cwd would be) listing a different repo."""
    co = tmp_path / "checkout"
    (co / "config").mkdir(parents=True)
    (co / "config" / "repos.conf").write_text("repo: operator/from-checkout dispatch\n")
    monkeypatch.chdir(co)
    return co / "config" / "repos.conf"


def test_fleet_record_reads_the_pinned_list_not_its_own_checkouts(rig, tmp_path, monkeypatch, capsys):
    own = _checkout_with_other_list(tmp_path, monkeypatch)
    assert rig.mod.repos() == [REPO]
    assert rig.mod.main(["backfill", "--since", "2026-09-29"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert str(rig.mod.PINNED_CONF) in out[0] and "repository list read from" in out[0]
    assert str(own) not in "\n".join(out)


def test_fleet_record_refuses_when_the_pinned_list_is_missing(rig, tmp_path, monkeypatch, capsys):
    own = _checkout_with_other_list(tmp_path, monkeypatch)
    rig.mod.PINNED_CONF = tmp_path / "no-such-pin" / "repos.conf"
    assert rig.mod.repos() == []                  # not the checkout's operator/from-checkout
    assert rig.mod.main(["once"]) == 1
    cap = capsys.readouterr()
    assert "refusing to run" in cap.err and "no-such-pin" in cap.err
    assert str(own) not in cap.out + cap.err
    assert rig.world.requests == [] or all("from-checkout" not in r for r in rig.world.requests)
