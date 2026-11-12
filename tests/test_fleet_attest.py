"""Tests for fleet-attest (plan 0120): fixture records, a fake forge on 127.0.0.1,
local git repositories standing in for the watched repositories, and a local
bare repository standing in for operator/recordstore."""
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
REPO = "demo/app"
OLD = "demo/old"
PLAN = "0105-a-thing"
PLAN_FILE = f"plans/{PLAN}.md"
HEAD = "a" * 40
CLEAN_ENV = ("FLEET_AUTHOR_ACCOUNT", "FLEET_REVIEWER_ACCOUNT", "FLEET_MERGER_ACCOUNT",
             "FLEET_SECOND_REVIEWER_ACCOUNT", "FLEET_AGENT_MERGER_ACCOUNT",
             "FLEET_AGENT_ACCOUNTS", "FLEET_IMPLEMENTER_LOGINS", "FLEET_RECORDER_ACCOUNT")
OWNER, AGENT, REV1, REV2, AUTHOR = "operator", "mergebot", "revbot", "rev2bot", "implbot"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


def sh(*args, cwd=None, date=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="seed", GIT_AUTHOR_EMAIL="s@x",
               GIT_COMMITTER_NAME="seed", GIT_COMMITTER_EMAIL="s@x")
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def plan_text(status="ready", body="# plan\n"):
    return f"---\nid: {PLAN}\nstatus: {status}\n---\n\n{body}"


class World:
    def __init__(self):
        self.branches = {REPO: "main", OLD: "master"}
        self.protection = {REPO: [{"branch_name": "main", "required_approvals": 2,
                                   "updated_unix": 1}],
                           OLD: [{"branch_name": "master", "required_approvals": 2}]}


def _serve(world):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path[len("/api/v1"):]
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/branch_protections)?", path)
            if not m or m.group(1) not in world.branches:
                code, body = 404, None
            elif m.group(2):
                code, body = 200, world.protection[m.group(1)]
            else:
                code, body = 200, {"default_branch": world.branches[m.group(1)]}
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Target:
    """A watched repository: a local git repo the tool fetches from."""
    def __init__(self, path, branch):
        self.path = path
        path.mkdir(parents=True)
        sh("git", "init", "-q", "-b", branch, str(path))
        self.base = self.commit({PLAN_FILE: plan_text(), "README.md": "x\n"}, "init")

    def commit(self, files, msg="change", date=None):
        for n, t in files.items():
            p = self.path / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(t)
        sh("git", "add", "-A", cwd=self.path)
        sh("git", "commit", "-q", "-m", msg, cwd=self.path, date=date)
        return sh("git", "rev-parse", "HEAD", cwd=self.path)


def good_record(repo, sha, **over):
    rec = {
        "schema": 1, "repo": repo, "pr": 1, "title": "t", "base_ref": "main",
        "head_sha": HEAD, "merge_sha": sha, "merged_at": "2026-10-02T10:00:00Z",
        "merged_by": AGENT, "author": AUTHOR, "files": ["bin/x"],
        "plan": {"id": PLAN, "status_at_merge": "ready", "commit": "c" * 40,
                 "commit_merged_by": OWNER},
        "reviews": [review(REV1), review(REV2)],
        "statuses": [{"context": "ci", "state": "success", "creator": "ci",
                      "created_at": "t"}],
        "required_contexts": ["ci"], "autonomy": "autonomous",
        "recorded_at": "2026-10-02T10:01:00Z", "recorder_rev": "r",
        "source": "observed", "supersedes": None}
    rec.update(over)
    return rec


def review(account, **over):
    rv = {"id": 1, "account": account, "state": "APPROVED", "commit_id": HEAD,
          "submitted_at": "t", "dismissed": False, "stale": False,
          "model": "model-a", "session_uuid": None}
    rv.update(over)
    return rv


def owner_record(repo, sha, **over):
    base = dict(merged_by=OWNER, plan=None, reviews=[review(REV1)])
    base.update(over)
    return good_record(repo, sha, **base)


class Rig:
    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for k in CLEAN_ENV:
        monkeypatch.delenv(k, raising=False)
    world = World()
    srv = _serve(world)
    r = Rig()
    r.world, r.tmp = world, tmp_path
    r.app = Target(tmp_path / "remotes" / REPO, "main")
    r.old = Target(tmp_path / "remotes" / OLD, "master")
    bare = tmp_path / "evidence.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    r.seed = tmp_path / "seed"
    sh("git", "clone", "-q", str(bare), str(r.seed))
    r.add_evidence({"README.md": "evidence\n"}, "init")
    r.fleet = tmp_path / "fleet"
    r.fleet.mkdir()
    r.spool = tmp_path / "spool"
    r.spool.mkdir()
    cmd = tmp_path / "token"
    cmd.write_text("#!/bin/sh\necho read-token\n")
    cmd.chmod(0o755)
    for k, v in {"FLEET_FORGEJO_URL": f"http://127.0.0.1:{srv.server_port}",
                 "FLEET_ATTEST_TOKEN_CMD": str(cmd),
                 "FLEET_ATTEST_REMOTE_TEMPLATE": str(tmp_path / "remotes") + "/{repo}",
                 "FLEET_ATTEST_EVIDENCE_REMOTE": str(bare),
                 "FLEET_ATTEST_EVIDENCE": str(tmp_path / "ev-clone"),
                 "FLEET_ATTEST_CLONES": str(tmp_path / "clones"),
                 "FLEET_ATTEST_SPOOL": str(r.spool),
                 "FLEET_ATTEST_STATE": str(tmp_path / "seen.json"),
                 "EUNOMIA_FLEET_DIR": str(r.fleet)}.items():
        monkeypatch.setenv(k, v)
    mod = _load("fleet_attest_under_test", BIN / "fleet-attest")
    pinned = tmp_path / "repos.conf"
    pinned.write_text(f"repo: {REPO} dispatch\nrepo: {OLD} dispatch\n")
    mod.PINNED_CONF = pinned
    r.pages = []
    mod.page = lambda title, body: r.pages.append((title, body)) or True
    r.mod = mod
    r.set_repos = lambda *rs: pinned.write_text("".join(f"repo: {x} dispatch\n" for x in rs))
    r.add_autonomy(REPO, "autonomous", "2026-09-30T00:00:00Z")
    r.add_autonomy(OLD, "autonomous", "2026-09-30T00:00:00Z")
    r.bare, r.pinned = bare, pinned
    yield r
    srv.shutdown()


def _add_evidence(self, files, msg="evidence"):
    for n, t in files.items():
        p = self.seed / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t)
    sh("git", "add", "-A", cwd=self.seed)
    sh("git", "commit", "-q", "-m", msg, cwd=self.seed)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=self.seed)
    return sh("git", "rev-parse", "HEAD", cwd=self.seed)


def _add_autonomy(self, repo, value, ts):
    line = {"ts": ts, "actor": "cli:x", "type": "fleet-setting-changed", "repo": None,
            "pr": None, "sha": None, "lease": None,
            "detail": {"key": "autonomy", "repo": repo, "old": None, "new": value}}
    with open(self.fleet / "events.jsonl", "a") as f:
        f.write(json.dumps(line, separators=(",", ":")) + "\n")


def _record(self, rec, path=None):
    t = rec["merged_at"]
    path = path or f"records/{rec['repo']}/{t[:4]}/{t[5:7]}/{rec['merge_sha']}.json"
    return self.add_evidence({path: json.dumps(rec, indent=2, sort_keys=True) + "\n"})


Rig.add_evidence, Rig.add_autonomy, Rig.record = _add_evidence, _add_autonomy, _record


def prior_report(rig, **through):
    """A published 2026-09 report: both repos attested through their base, the
    ledger through 2026-09-30, the evidence through its current head."""
    ev = sh("git", "rev-parse", "HEAD", cwd=rig.seed)
    through = through or {REPO: rig.app.base, OLD: rig.old.base}
    rep = {"schema": 1, "month": "2026-09", "name": "2026-09", "follows": None,
           "period": {"through": through, "evidence_through": ev,
                      "ledger_through": "2026-09-30T00:00:00Z"}}
    rig.add_evidence({"reports/2026-09.json": json.dumps(rep)})


def check(rig, capsys, *extra):
    rc = rig.mod.main(["check", "--json", *extra])
    out = capsys.readouterr().out
    body = json.loads(out[out.index("{\n"):]) if "{\n" in out else {}
    return rc, body.get("exceptions", []), body


def ids(excs, check_id=None):
    return sorted(e["id"] for e in excs if check_id in (None, e["check"]))


def only(excs):
    assert len(excs) == 1, excs
    return excs[0]


def agent_commit(rig, files=None, rec_over=None, repo_files=None):
    sha = rig.app.commit(repo_files or {"bin/x": "x\n"}, "merge")
    rig.record(good_record(REPO, sha, files=files or ["bin/x"], **(rec_over or {})))
    return sha


# ------------------------------------------------------------ the happy path
def test_good_agent_merge_and_owner_merges_raise_nothing_and_are_counted(rig, capsys):
    prior_report(rig)
    agent_commit(rig)
    o = rig.app.commit({"docs/y.md": "y\n"}, "owner")
    rig.record(owner_record(REPO, o, files=["docs/y.md"]))
    s = rig.app.commit({"docs/z.md": "z\n"}, "owner self")
    rig.record(owner_record(REPO, s, author=OWNER, reviews=[], files=["docs/z.md"]))
    rc, excs, body = check(rig, capsys)
    assert rc == 0 and excs == [] and rig.pages == []
    repo = body["repos"][REPO]
    assert repo["merges"] == 3 and repo["evidenced"] == 3
    assert repo["by_merger"] == {AGENT: 1, OWNER: 2}
    assert repo["owner_merges"] == 2 and repo["owner_reviewed"] == 1 and repo["owner_self"] == 1


def test_owner_merge_on_critical_repo_raises_nothing(rig, capsys):
    prior_report(rig)
    rig.add_autonomy(REPO, "critical", "2026-09-30T00:00:00Z")
    s = rig.app.commit({"bin/q": "q\n"}, "owner self")
    rig.record(owner_record(REPO, s, author=OWNER, reviews=[], autonomy="critical"))
    rc, excs, body = check(rig, capsys)
    assert excs == [] and rig.pages == [] and body["repos"][REPO]["owner_self"] == 1


# -------------------------------------------------------------- A1 / D1 walk
def test_planted_direct_push_is_unrecorded_and_its_record_clears_it(rig, capsys):
    prior_report(rig)
    sha = rig.app.commit({"bin/sneak": "s\n"}, "direct push")
    rc, excs, _ = check(rig, capsys)
    e = only(excs)
    assert (e["check"], e["class"]) == ("A1", "unrecorded")
    assert e["id"] == f"A1-demo--app-{sha[:12]}"
    rig.record(good_record(REPO, sha, files=["bin/sneak"]))
    rc, excs, _ = check(rig, capsys)
    assert excs == []


def test_old_dated_commit_not_reachable_from_start_is_still_walked(rig, capsys):
    prior_report(rig)
    sha = rig.app.commit({"bin/back": "b\n"}, "backdated", date="2001-01-01T00:00:00Z")
    _, excs, _ = check(rig, capsys)
    assert only(excs)["class"] == "unrecorded" and sha[:12] in only(excs)["id"]


def test_start_sha_not_an_ancestor_is_history_rewritten(rig, capsys):
    other = rig.app.commit({"bin/gone": "g\n"}, "doomed")
    sh("git", "reset", "-q", "--hard", rig.app.base, cwd=rig.app.path)
    rig.app.commit({"bin/new": "n\n"}, "replacement")
    prior_report(rig, **{REPO: other, OLD: rig.old.base})
    _, excs, _ = check(rig, capsys)
    assert [(e["check"], e["class"]) for e in excs] == [("A1", "history-rewritten")]


def test_check_with_no_prior_report_and_no_from_refuses(rig, capsys):
    rc, excs, _ = check(rig, capsys)
    assert rc != 0 and rig.pages == []


def test_an_empty_pinned_repo_list_refuses_by_name(rig, capsys):
    """The pin's repos.conf moved or changed format: refuse naming the file,
    not as a missing start SHA."""
    prior_report(rig, **{REPO: rig.app.base})
    rig.set_repos()
    rc = rig.mod.main(["check", "--json"])
    cap = capsys.readouterr()
    assert rc != 0 and rig.pages == []
    said = cap.out + cap.err
    assert "no repositories read from" in said and "no start SHA" not in said


def test_repo_with_no_start_is_skipped_and_reported_and_others_are_checked(rig, capsys):
    prior_report(rig, **{REPO: rig.app.base})
    rig.app.commit({"bin/sneak": "s\n"}, "direct push")
    _, excs, body = check(rig, capsys)
    assert sorted((e["repo"], e["check"], e["class"]) for e in excs) == [
        (REPO, "A1", "unrecorded"), (OLD, "A1", "rule")]
    assert "no start SHA" in [e for e in excs if e["repo"] == OLD][0]["detail"]


def test_from_supplies_a_start_for_a_repo_with_no_report(rig, capsys):
    rig.app.commit({"bin/sneak": "s\n"}, "direct push")
    rc, excs, _ = check(rig, capsys, "--from", f"{REPO}={rig.app.base}",
                        "--from", f"{OLD}={rig.old.base}")
    assert rc == 0
    assert [(e["check"], e["class"]) for e in excs
            if e["repo"] == REPO and e["check"] == "A1"] == [("A1", "unrecorded")]


def test_default_branch_master_is_walked_on_master(rig, capsys):
    prior_report(rig)
    rig.old.commit({"bin/sneak": "s\n"}, "direct push")
    _, excs, body = check(rig, capsys)
    e = only(excs)
    assert (e["repo"], e["class"]) == (OLD, "unrecorded")
    assert body["repos"][OLD]["default_branch"] == "master"


# ---------------------------------------------------------------- A2 - A6
def fails_alone(rig, capsys, check_id, klass="rule", **over):
    prior_report(rig)
    over.setdefault("files", ["bin/x"])
    sha = rig.app.commit({"bin/x": "x\n"}, "merge")
    rig.record(good_record(REPO, sha, **over))
    _, excs, _ = check(rig, capsys)
    assert [(e["check"], e["class"]) for e in excs] == [(check_id, klass)], excs
    return excs[0]


def test_a2_agent_merge_without_a_plan_fails(rig, capsys):
    fails_alone(rig, capsys, "A2", plan=None)


def test_a2_plan_not_ready_or_not_owner_merged_fails(rig, capsys):
    fails_alone(rig, capsys, "A2", plan={"id": PLAN, "status_at_merge": "done",
                                         "commit": "c" * 40, "commit_merged_by": OWNER})


def test_a2_plan_commit_merged_by_an_agent_fails(rig, capsys):
    fails_alone(rig, capsys, "A2", plan={"id": PLAN, "status_at_merge": "ready",
                                         "commit": "c" * 40, "commit_merged_by": AGENT})


def test_a3_missing_second_approval_fails(rig, capsys):
    fails_alone(rig, capsys, "A3", reviews=[review(REV1)])


def test_a3_approval_at_an_older_sha_fails(rig, capsys):
    fails_alone(rig, capsys, "A3", reviews=[review(REV1), review(REV2, commit_id="b" * 40)])


def test_a3_null_model_fails_as_unprovable(rig, capsys):
    e = fails_alone(rig, capsys, "A3", reviews=[review(REV1), review(REV2, model=None)])
    assert "unprovable" in e["detail"]


def test_a3_dismissed_or_stale_approval_fails(rig, capsys):
    fails_alone(rig, capsys, "A3", reviews=[review(REV1, dismissed=True), review(REV2)])


@pytest.mark.parametrize("models", [("m", "m"), ("m", "n")])
def test_a3_one_model_or_two_both_pass(rig, capsys, models):
    prior_report(rig)
    agent_commit(rig, rec_over={"reviews": [review(REV1, model=models[0]),
                                            review(REV2, model=models[1])]})
    assert check(rig, capsys)[1] == []


def test_a4_agent_merging_its_own_change_fails(rig, capsys):
    fails_alone(rig, capsys, "A4", author=AGENT)


def test_a4_a_reviewer_approving_its_own_change_fails(rig, capsys):
    fails_alone(rig, capsys, "A4", author=REV1)


def test_a4_recorder_in_a_role_fails(rig, capsys):
    fails_alone(rig, capsys, "A4", reviews=[review(REV1), review(REV2),
                                            review("recordbot", state="COMMENT")])


def test_a5_missing_or_red_required_context_is_red_ci(rig, capsys):
    fails_alone(rig, capsys, "A5", "red-ci", statuses=[
        {"context": "ci", "state": "failure", "creator": "ci", "created_at": "t"}])


def test_a6_unknown_merger_fails(rig, capsys):
    fails_alone(rig, capsys, "A6", merged_by="stranger", author=OWNER)


def test_a6_agent_merge_on_a_critical_repo_fails(rig, capsys):
    prior_report(rig)
    rig.add_autonomy(REPO, "critical", "2026-09-30T12:00:00Z")
    agent_commit(rig)
    # the autonomy event is after the report's ledger cursor: also a control change
    excs = check(rig, capsys)[1]
    assert sorted((e["check"], e["class"]) for e in excs) == [
        ("A6", "rule"), ("A7", "control-change")]


def test_autonomy_is_judged_at_merged_at_not_now(rig, capsys):
    prior_report(rig)
    rig.add_autonomy(REPO, "critical", "2026-10-03T00:00:00Z")   # after the merge
    agent_commit(rig)
    excs = check(rig, capsys)[1]
    assert [(e["check"]) for e in excs] == ["A7"]


def test_autonomy_made_after_the_merge_is_judged_critical(rig, capsys):
    prior_report(rig)
    (rig.fleet / "events.jsonl").unlink()
    rig.add_autonomy(REPO, "autonomous", "2026-10-03T00:00:00Z")   # after merged_at
    agent_commit(rig)
    excs = check(rig, capsys)[1]
    assert sorted(e["check"] for e in excs) == ["A6", "A7"]


def test_autonomy_resolves_with_an_empty_repo_setting_change_table(rig, capsys):
    prior_report(rig)
    con = sqlite3.connect(rig.fleet / "fleet.db")
    con.execute("CREATE TABLE repo_setting_change (id TEXT, repo TEXT, key TEXT, "
                "old_value TEXT, new_value TEXT, ts TEXT)")
    con.commit()
    con.close()
    agent_commit(rig)
    assert check(rig, capsys)[1] == []


def a6_case(rig, capsys, files, repo_files, plan_after=None):
    prior_report(rig)
    if plan_after is not None:
        repo_files = {**repo_files, PLAN_FILE: plan_after}
    sha = rig.app.commit(repo_files, "merge")
    rig.record(good_record(REPO, sha, files=files))
    return check(rig, capsys)[1]


@pytest.mark.parametrize("path", ["plans/0999-other.md", "tests/lane/t.py",
                                  "launchd/com.x.plist", "docs/adr/0099-x.md",
                                  "bin/fleet-merge", ".forgejo/workflows/a.yml"])
def test_a6_src_pr_outside_its_limits_fails(rig, capsys, path):
    excs = a6_case(rig, capsys, ["bin/x", path], {"bin/x": "x\n", path: "z\n"})
    assert [(e["check"], e["class"]) for e in excs] == [("A6", "rule")]


def test_a6_src_pr_shipping_an_operational_doc_passes(rig, capsys):
    assert a6_case(rig, capsys, ["bin/x", "docs/fleet-x.md"],
                   {"bin/x": "x\n", "docs/fleet-x.md": "d\n"}) == []


def test_a6_status_flip_of_its_own_plan_passes(rig, capsys):
    assert a6_case(rig, capsys, ["bin/x", PLAN_FILE], {"bin/x": "x\n"},
                   plan_after=plan_text("done")) == []


def test_a6_edit_to_its_own_plan_body_fails(rig, capsys):
    excs = a6_case(rig, capsys, ["bin/x", PLAN_FILE], {"bin/x": "x\n"},
                   plan_after=plan_text("done", "# a different plan\n"))
    assert [(e["check"]) for e in excs] == ["A6"]


def test_a6_tests_pr_passes_only_inside_the_lane(rig, capsys):
    assert a6_case(rig, capsys, ["tests/lane/a.py"], {"tests/lane/a.py": "t\n"}) == []


# ------------------------------------------------------------------------ A7
def test_a7_autonomy_change_in_the_period_is_a_control_change(rig, capsys):
    prior_report(rig)
    rig.add_autonomy(REPO, "critical", "2026-10-04T00:00:00Z")
    e = only(check(rig, capsys)[1])
    assert (e["check"], e["class"], e["repo"]) == ("A7", "control-change", REPO)
    assert re.fullmatch(r"A7-demo--app-[0-9a-f]{12}", e["id"])


def test_a7_protection_change_is_a_control_change_and_first_snapshot_is_not(rig, capsys):
    prior_report(rig)
    assert check(rig, capsys)[1] == []
    path = rig.spool / "protection" / f"{REPO}.json"
    assert json.loads(path.read_text())["rule"]["required_approvals"] == 2
    assert "updated_unix" not in path.read_text()
    rig.add_evidence({f"protection/{REPO}.json": path.read_text()})
    assert check(rig, capsys)[1] == []
    rig.world.protection[REPO][0]["required_approvals"] = 0
    e = only(check(rig, capsys)[1])
    assert (e["check"], e["class"]) == ("A7", "control-change")


# ------------------------------------------------------------------------ A8
def test_a8_modifying_an_existing_record_is_record_edited(rig, capsys):
    sha = rig.app.base
    rig.record(good_record(REPO, sha))
    prior_report(rig)
    path = f"records/{REPO}/2026/10/{sha}.json"
    rig.add_evidence({path: json.dumps(good_record(REPO, sha, title="edited"))}, "tamper")
    e = only(check(rig, capsys)[1])
    assert (e["check"], e["class"], e["repo"]) == ("A8", "record-edited", "operator/recordstore")


def test_a8_adding_records_and_late_backfill_records_are_not_exceptions(rig, capsys):
    prior_report(rig)
    sha = rig.app.commit({"bin/x": "x\n"}, "m")
    rig.record(good_record(REPO, sha, source="backfill"))
    rc, excs, body = check(rig, capsys)
    assert excs == [] and body["repos"][REPO]["by_source"] == {"backfill": 1}


# ------------------------------------------------------------- ids, dispositions, paging
def test_exception_ids_have_no_slash(rig, capsys):
    prior_report(rig)
    rig.app.commit({"bin/sneak": "s\n"}, "direct push")
    rig.old.commit({"bin/sneak": "s\n"}, "direct push")
    excs = check(rig, capsys)[1]
    assert len(excs) == 2 and all("/" not in e["id"] for e in excs)


def test_a_disposition_file_marks_the_exception_disposed_and_it_is_not_paged(rig, capsys):
    prior_report(rig)
    sha = rig.app.commit({"bin/sneak": "s\n"}, "direct push")
    rig.add_evidence({f"dispositions/A1-demo--app-{sha[:12]}.md": "owner pushed this\n"})
    excs = check(rig, capsys)[1]
    assert only(excs)["status"] == "disposed" and rig.pages == []


def test_check_pages_once_per_run_and_only_for_unseen_exceptions(rig, capsys):
    prior_report(rig)
    rig.app.commit({"bin/a": "a\n"}, "one")
    rig.app.commit({"bin/b": "b\n"}, "two")
    check(rig, capsys)
    assert len(rig.pages) == 1 and "2 unrecorded" in rig.pages[0][1]
    check(rig, capsys)
    assert len(rig.pages) == 1
    rig.app.commit({"bin/c": "c\n"}, "three")
    check(rig, capsys)
    assert len(rig.pages) == 2 and "1 unrecorded" in rig.pages[1][1]


# --------------------------------------------------------------------- report
def report(rig, *args):
    return rig.mod.main(["report", *args])


def test_report_writes_md_and_json_with_the_documented_facts(rig, capsys):
    prior_report(rig)
    agent_commit(rig)
    rig.app.commit({"bin/sneak": "s\n"}, "direct push")
    assert report(rig, "--month", "2026-10") == 0
    j = json.loads((rig.spool / "reports" / "2026-10.json").read_text())
    md = (rig.spool / "reports" / "2026-10.md").read_text()
    assert j["month"] == "2026-10" and j["follows"] is None
    assert j["period"]["through"][REPO] == sh("git", "rev-parse", "HEAD", cwd=rig.app.path)
    assert j["period"]["from"][REPO] == rig.app.base
    assert j["period"]["evidence_through"] == sh("git", "rev-parse", "HEAD", cwd=rig.seed)
    assert j["eunomia_commit"] == sh("git", "rev-parse", "HEAD", cwd=ROOT)
    assert j["repos"][REPO]["merges"] == 1
    assert j["exceptions_by_class"]["unrecorded"] == {"open": 1, "disposed": 0}
    assert j["exceptions"][0]["id"] in md and "unrecorded" in md


def test_a_second_report_for_the_same_month_is_refused(rig, capsys):
    prior_report(rig)
    assert report(rig, "--month", "2026-10") == 0
    assert report(rig, "--month", "2026-10") != 0
    assert not (rig.spool / "reports" / "2026-10.r2.json").exists()


def test_rerun_writes_r2_naming_the_first_report(rig, capsys):
    prior_report(rig)
    assert report(rig, "--month", "2026-10") == 0
    assert report(rig, "--month", "2026-10", "--rerun") == 0
    j = json.loads((rig.spool / "reports" / "2026-10.r2.json").read_text())
    assert (rig.spool / "reports" / "2026-10.r2.md").exists()
    assert j["follows"] == "reports/2026-10.json"
    assert j["period"]["from"] == json.loads(
        (rig.spool / "reports" / "2026-10.json").read_text())["period"]["from"]
    assert report(rig, "--month", "2026-10", "--rerun") == 0
    assert json.loads((rig.spool / "reports" / "2026-10.r3.json").read_text())["follows"] \
        == "reports/2026-10.r2.json"


def test_month_previous_is_the_calendar_month_before_today_in_utc(rig, capsys):
    import datetime as dt
    prior_report(rig)
    rig.mod._now = lambda: dt.datetime(2026, 1, 1, 0, 30, tzinfo=dt.timezone.utc)
    assert report(rig, "--month", "previous") == 0
    assert (rig.spool / "reports" / "2025-12.json").exists()


def test_the_next_check_starts_from_the_published_report(rig, capsys):
    prior_report(rig)
    report(rig, "--month", "2026-10")
    for p in (rig.spool / "reports").iterdir():
        rig.add_evidence({f"reports/{p.name}": p.read_text()})
    tip = sh("git", "rev-parse", "HEAD", cwd=rig.app.path)
    sha = rig.app.commit({"bin/sneak": "s\n"}, "direct push")
    e = only(check(rig, capsys)[1])
    assert e["id"].endswith(sha[:12]) and tip != sha


# ------------------------------------------------------------ the tool's own bounds
def test_the_tool_never_touches_the_recorders_credentials_or_a_remote_write():
    src = (BIN / "fleet-attest").read_text()
    assert "FLEET_RECORD_" not in src and "token_cmd" not in src.lower().replace(
        "fleet_attest_token_cmd", "")
    assert not re.search(r"\.ssh|id_ed25519|id_rsa|git-recordbot", src)
    assert not re.search(r"""["']push["']|git push""", src)
    assert "fleet-merge" not in re.sub(r"#.*|\"\"\".*?\"\"\"", "", src, flags=re.S).replace(
        '"bin/fleet-merge",', "")


def test_enforcing_equals_fleet_merge_enforcing():
    attest = _load("attest_for_enforcing", BIN / "fleet-attest")
    merge = _load("merge_for_enforcing", BIN / "fleet-merge")
    assert attest.ENFORCING == merge.ENFORCING


def _doc_rows():
    rows = {}
    for line in (ROOT / "docs" / "change-management.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6 and re.fullmatch(r"C\d", cells[0]):
            rows[cells[5]] = (cells[0], cells[4])
    return rows


def _schema_has(schema, dotted):
    node = schema
    for part in dotted.split("."):
        name = part.removesuffix("[]")
        node = node["properties"][name]
        if part.endswith("[]"):
            node = node["items"]
        elif "properties" not in node and "type" in node and "object" in node["type"]:
            pass
    return True


def test_the_check_table_matches_the_documents_control_table_and_the_schema():
    attest = _load("attest_for_table", BIN / "fleet-attest")
    schema = json.loads((ROOT / "config" / "change-record.schema.json").read_text())
    rows = _doc_rows()
    assert set(rows) == set(attest.CHECKS)
    for cid, (control, evidence) in rows.items():
        mine = attest.CHECKS[cid]
        assert mine["control"] == control
        fields = re.findall(r"`([^`]+)`", evidence)
        assert set(fields) == set(mine["fields"]), cid
        rest = re.sub(r"`[^`]+`", "", evidence)
        others = {x.strip() for x in re.split(r",|;", rest) if x.strip()}
        assert others == set(mine["other"]), cid
        for f in mine["fields"]:
            assert _schema_has(schema, f), f
    for cid, mine in attest.CHECKS.items():
        for f in mine["fields"]:
            assert f in rows[cid][1]
