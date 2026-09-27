#!/usr/bin/env python3
"""Tests for bin/fleet-ci — CI results ingested into fleet.db and served from it
(plan 0045).

Run: python3 -m pytest tests/test_fleet_ci.py -q      (stdlib only)

Nothing here touches Forgejo, cihost, or the real fleet tree. The API is a fake
`Forge`; the log host is `local` (the same remote script `sync` ships over ssh,
run as a child process against a temp tree), with a stand-in `zstd` that is
`cat` — so the framing, the cap and the failure paths are the real ones, and
one test uses the real zstd where it is installed.

CREDENTIAL FIXTURES ARE BUILT, NOT WRITTEN. A test file that spells out a private
key's armour or an AWS key id is a credential-shaped string in the repository,
and the same guard that refused plan 0045's first cut would refuse this file. The
armour is concatenated from halves, and every secret-looking value is synthetic
(repeated characters), which is also what fleet-leak-watch treats as a
placeholder rather than a page.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
ROOT = BIN.parent


def _load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# bin/fleet-ci reads these when it is IMPORTED, not when it is called, so a value
# another test module left in os.environ is baked into every `_load("fleet-ci")`
# after it. tests/test_fleet_reviews.py's `_load` does exactly that —
# FLEET_FORGE_OWNER="acme", never restored — which turned a bare `lynceus` into
# `acme/lynceus` and made the `failures` test fail on cihost-linux while passing on
# cihost: the same tree, a different pytest-randomly order. Cleared per test, so
# the order this file runs in cannot matter.
_IMPORT_TIME_ENV = ("FLEET_DB", "FLEET_FORGEJO_URL", "FLEET_FORGE_OWNER",
                    "FLEET_CI_LOG_HOST", "FLEET_CI_LOG_ROOT", "FLEET_CI_LOG_CAP",
                    "FLEET_CI_BUDGET")


@pytest.fixture(autouse=True)
def _no_inherited_import_time_env(monkeypatch):
    for k in _IMPORT_TIME_ENV:
        monkeypatch.delenv(k, raising=False)


# ----------------------------------------------------- synthetic credentials
# Halves joined at runtime; see the module docstring.
HEX40 = "a1" * 20
PAT_HEADER = "Authorization: token " + HEX40
GHP = "ghp_" + "A" * 36
AKIA = "AKIA" + "A" * 16
ASIA = "ASIA" + "B" * 16
HVS = "hvs." + "C" * 24
SLACK = "xoxb-" + "1" * 12 + "-" + "d" * 12
SK = "sk-" + "e" * 24
JWT = "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 12
B64 = "QUFBQUFBQUE6QkJCQkJCQkI="
PEM_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_END = "-----END " + "RSA PRIVATE KEY-----"
PEM_BODY = "MIIE" + "Z" * 60


# ------------------------------------------------------------------ fixtures
def _ts(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _task(tid, name="tests", status="success", sha=None, run=1, ago=60, **extra):
    t = {"id": tid, "name": name, "run_number": run, "head_branch": "main",
         "head_sha": sha or ("%040x" % tid), "event": "push",
         "display_title": f"commit for {tid}", "status": status,
         "workflow_id": "ci.yml", "url": f"http://forge/x/actions/runs/{run}",
         "created_at": _ts(ago + 5), "updated_at": _ts(ago), "run_started_at": _ts(ago + 5)}
    t.update(extra)
    return t


class FakeForge:
    """The two verbs `sync` uses. `tasks` is {repo: [task, ...]} newest first."""

    def __init__(self, tasks, fail=None):
        self.tasks = tasks
        self.fail = fail or {}
        self.calls = []

    def list_actions_tasks(self, repo, page=1, limit=50):
        self.calls.append((repo, page))
        if repo in self.fail:
            return self.fail[repo], None
        rows = self.tasks.get(repo, [])
        chunk = rows[(page - 1) * limit: page * limit]
        return 200, {"workflow_runs": chunk, "total_count": len(rows)}

    def search_repos(self, page=1, limit=50):
        names = [{"full_name": r, "has_actions": True} for r in self.tasks]
        names.append({"full_name": "o/no-actions", "has_actions": False})
        return 200, {"ok": True, "data": names[(page - 1) * limit: page * limit]}


CAT_ZSTD = "#!/bin/sh\nfor last; do :; done\nexec cat -- \"$last\"\n"
# Writes half a log, then dies: a log that could not be read whole.
DYING_ZSTD = "#!/bin/sh\nprintf 'half a log\\n'\nexit 1\n"


def _script(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.ci = _load("fleet-ci")
        self.fc = _load("fleet-collect")
        self.root = tmp_path / "actions_log"
        self.root.mkdir()
        self.db = tmp_path / "fleet.db"
        self.con = sqlite3.connect(self.db)
        self.con.executescript(self.fc.SCHEMA)
        self.con.executescript(self.fc.DISPATCH_VIEW)
        self.con.executescript(self.fc.CI_VIEW)
        self.tmp = tmp_path
        monkeypatch.setattr(self.ci, "LOG_HOST", "local")
        monkeypatch.setattr(self.ci, "LOG_ROOT", str(self.root))
        monkeypatch.setattr(self.ci, "ZSTD", _script(tmp_path / "zstd", CAT_ZSTD))
        monkeypatch.delenv("FLEET_CI_REPOS", raising=False)
        self.monkeypatch = monkeypatch

    def log(self, repo, tid, text):
        p = self.root / self.ci._rel_path(repo, tid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode() if isinstance(text, str) else text)
        return p

    def sync(self, tasks, repos=None, **fail):
        forge = FakeForge(tasks, fail=fail)
        self.forge = forge
        return self.ci.sync(self.con, forge=forge, repos=repos or list(tasks))

    def row(self, repo, tid):
        cur = self.con.execute(
            "SELECT status, log_state, log_detail, log_zbytes FROM ci_task WHERE repo=? AND id=?",
            (repo, tid))
        return cur.fetchone()

    def body(self, repo, tid):
        r = self.con.execute("SELECT body_z FROM ci_log WHERE repo=? AND task_id=?",
                             (repo, tid)).fetchone()
        return zlib.decompress(r[0]).decode() if r else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


LYNCEUS = "operator/lynceus"
LYNCEUS_LOG = (
    "2026-09-12T18:41:23Z ERROR: Missing required icon file. The bundle does not contain "
    "an app icon for iPhone of exactly '120x120' pixels\n"
    "2026-09-12T18:41:23Z ERROR: Missing Info.plist value. A value for the Info.plist key "
    "'CFBundleIconName' is missing in the bundle\n")


# --------------------------------------------------------- D1/D2 — the shapes
def test_lynceus_two_tasks_a_body_for_the_failure_and_none_for_the_pass(env):
    env.log(LYNCEUS, 1, LYNCEUS_LOG)
    env.log(LYNCEUS, 2, "everything passed\n")                # exists, must not be read
    report = env.sync({LYNCEUS: [_task(2, run=2, status="success"),
                                 _task(1, run=1, status="failure")]})
    assert report["failures"] == []
    rows = env.con.execute(
        "SELECT id, run_number, status FROM ci_task WHERE repo=? ORDER BY id", (LYNCEUS,)).fetchall()
    assert rows == [(1, 1, "failure"), (2, 2, "success")]
    body = env.body(LYNCEUS, 1)
    assert "120x120" in body and "CFBundleIconName" in body
    assert env.body(LYNCEUS, 2) is None
    assert env.row(LYNCEUS, 2)[1] == "not-failed"
    assert env.con.execute("SELECT COUNT(*) FROM ci_log").fetchone()[0] == 1


def test_every_column_the_api_returns_is_stored_plus_the_repo(env):
    env.sync({LYNCEUS: [_task(7, name="tests (cihost-linux)", status="failure", run=9,
                              display_title="a title", head_branch="feat/x")]})
    cols = [r[1] for r in env.con.execute("PRAGMA table_info(ci_task)")]
    for c in ("repo", "id", "name", "run_number", "head_branch", "head_sha", "event",
              "display_title", "status", "workflow_id", "url", "created_at", "updated_at",
              "run_started_at"):
        assert c in cols
    assert "conclusion" not in cols            # the API has none; `status` carries the result
    r = env.con.execute("SELECT repo, name, run_number, head_branch, event, workflow_id"
                        " FROM ci_task WHERE id=7").fetchone()
    assert r == (LYNCEUS, "tests (cihost-linux)", 9, "feat/x", "push", "ci.yml")


def test_a_two_label_matrix_is_two_rows_not_one(env):
    """`name` is load-bearing: tests (cihost) and tests (cihost-linux) are two
    tasks per push and are indistinguishable without it."""
    env.sync({LYNCEUS: [_task(11, name="tests (cihost-linux)", sha="c" * 40),
                        _task(10, name="tests (cihost)", sha="c" * 40)]})
    names = [r[0] for r in env.con.execute("SELECT name FROM ci_task ORDER BY id")]
    assert names == ["tests (cihost)", "tests (cihost-linux)"]


def test_rerunning_immediately_adds_no_rows_and_asks_for_no_logs(env):
    env.log(LYNCEUS, 1, LYNCEUS_LOG)
    tasks = {LYNCEUS: [_task(2), _task(1, status="failure")]}
    env.sync(tasks)
    snap = lambda: (env.con.execute("SELECT * FROM ci_task ORDER BY id").fetchall(),
                    env.con.execute("SELECT * FROM ci_log").fetchall())
    before = snap()
    second = env.sync(tasks)
    assert snap() == before
    assert (second["tasks_new"], second["tasks_changed"], second["logs_stored"]) == (0, 0, 0)


def test_a_missing_log_file_is_a_row_naming_the_path_and_no_ci_log(env):
    report = env.sync({LYNCEUS: [_task(5, status="failure")]})
    status, state, detail, _ = env.row(LYNCEUS, 5)
    assert state == "absent"
    assert str(env.root / env.ci._rel_path(LYNCEUS, 5)) in detail
    assert env.con.execute("SELECT COUNT(*) FROM ci_log").fetchone()[0] == 0
    assert report["logs_absent"] == 1


def test_an_absent_log_of_a_young_task_is_retried_and_an_old_one_is_final(env):
    tasks = {LYNCEUS: [_task(6, status="failure", ago=30),                 # just finished
                       _task(5, status="failure", ago=3 * 86400)]}         # long ago
    env.sync(tasks)
    assert {env.row(LYNCEUS, 5)[1], env.row(LYNCEUS, 6)[1]} == {"absent"}
    env.log(LYNCEUS, 5, "late\n")
    env.log(LYNCEUS, 6, "late\n")
    env.sync(tasks)
    assert env.row(LYNCEUS, 6)[1] == "stored"        # Forgejo archived it a moment late
    assert env.row(LYNCEUS, 5)[1] == "absent"        # old: not asked for again, ever


def test_an_over_cap_log_is_recorded_with_its_size_and_never_stored(env):
    env.ci.LOG_CAP = 1000
    env.monkeypatch.setattr(env.ci, "LOG_CAP", 1000)
    path = env.log("operator/ares", 467, "x" * 5000)
    report = env.sync({"operator/ares": [_task(467, status="failure")]})
    _, state, detail, zbytes = env.row("operator/ares", 467)
    assert state == "over-cap"
    assert zbytes == path.stat().st_size and str(zbytes) in detail and "1000" in detail
    assert env.body("operator/ares", 467) is None
    assert report["logs_over_cap"] == 1


def test_a_log_just_under_the_cap_is_stored_whole_with_the_cap_in_its_row(env):
    env.monkeypatch.setattr(env.ci, "LOG_CAP", 4000)
    env.log(LYNCEUS, 3, "y" * 4000)
    env.sync({LYNCEUS: [_task(3, status="failure")]})
    assert env.body(LYNCEUS, 3) == "y" * 4000
    raw, cap = env.con.execute("SELECT raw_bytes, cap_bytes FROM ci_log").fetchone()
    assert (raw, cap) == (4000, 4000)                # the cap in force is on the row


def test_a_log_that_could_not_be_read_whole_is_recorded_not_stored(env):
    env.log(LYNCEUS, 4, "whatever")
    env.monkeypatch.setattr(env.ci, "ZSTD", _script(env.tmp / "dying", DYING_ZSTD))
    env.sync({LYNCEUS: [_task(4, status="failure")]})
    _, state, detail, _ = env.row(LYNCEUS, 4)
    assert state == "unreadable" and "zstd exit 1" in detail
    assert env.con.execute("SELECT COUNT(*) FROM ci_log").fetchone()[0] == 0   # no half a log


def test_a_missing_zstd_is_unreadable_not_zero(env):
    env.log(LYNCEUS, 4, "whatever")
    env.monkeypatch.setattr(env.ci, "ZSTD", "/nonexistent/zstd")
    env.sync({LYNCEUS: [_task(4, status="failure")]})
    _, state, detail, _ = env.row(LYNCEUS, 4)
    assert state == "unreadable" and "/nonexistent/zstd" in detail


@pytest.mark.skipif(not shutil.which("zstd"), reason="no zstd binary to make a real .zst")
def test_a_real_zst_file_round_trips(env):
    real = shutil.which("zstd")
    p = env.root / env.ci._rel_path(LYNCEUS, 8)
    p.parent.mkdir(parents=True)
    subprocess.run([real, "-q", "-o", str(p)], input=LYNCEUS_LOG.encode(), check=True)
    env.monkeypatch.setattr(env.ci, "ZSTD", real)
    env.sync({LYNCEUS: [_task(8, status="failure")]})
    assert env.body(LYNCEUS, 8) == LYNCEUS_LOG


def test_cancelled_gets_a_body_skipped_and_running_do_not(env):
    for tid in (1, 2, 3, 4):
        env.log(LYNCEUS, tid, f"log {tid}\n")
    env.sync({LYNCEUS: [_task(4, status="running"), _task(3, status="skipped"),
                        _task(2, status="cancelled"), _task(1, status="failure")]})
    assert [env.row(LYNCEUS, t)[1] for t in (1, 2, 3, 4)] == [
        "stored", "stored", "not-failed", "in-progress"]


def test_a_task_that_finishes_later_gets_its_body_then(env):
    env.log(LYNCEUS, 1, "it broke\n")
    env.sync({LYNCEUS: [_task(1, status="running", ago=5)]})
    assert env.row(LYNCEUS, 1)[1] == "in-progress"
    env.sync({LYNCEUS: [_task(1, status="failure", ago=1)]})
    assert env.row(LYNCEUS, 1)[1] == "stored"
    assert env.body(LYNCEUS, 1) == "it broke\n"


def test_a_stored_body_survives_an_unchanged_status_with_a_new_timestamp(env):
    env.log(LYNCEUS, 1, "it broke\n")
    env.sync({LYNCEUS: [_task(1, status="failure", ago=100)]})
    env.sync({LYNCEUS: [_task(1, status="failure", ago=10)]})     # updated_at moved
    assert env.row(LYNCEUS, 1)[1] == "stored" and env.body(LYNCEUS, 1) == "it broke\n"


# ------------------------------------------------------------ the API side
def test_paging_stops_at_the_first_quiet_page_but_reaches_a_deep_open_task(env):
    tasks = [_task(i, status="success") for i in range(300, 0, -1)]           # 6 pages
    env.sync({LYNCEUS: tasks})
    env.forge.calls.clear()
    env.sync({LYNCEUS: tasks})
    assert env.forge.calls == [(LYNCEUS, 1)]          # nothing changed: one request, not six
    # A task STORED as running that lies deep in the list must keep being re-read
    # until it ends, even though every page newer than it is quiet.
    env2_tasks = [_task(i, status="success") for i in range(300, 0, -1)]
    deep_id = env2_tasks[-3]["id"]
    running = [dict(t) for t in env2_tasks]
    running[-3] = _task(deep_id, status="running")
    other = "operator/other"
    env.sync({other: running})                        # first sight: everything is new, so all pages
    assert env.row(other, deep_id)[1] == "in-progress"
    env.forge.calls.clear()
    env.sync({other: running})                        # still running, nothing else moved
    assert len(env.forge.calls) == 6                  # quiet pages 1-5 do not stop the walk
    env.sync({other: env2_tasks})                     # it finished
    assert env.row(other, deep_id)[1] == "not-failed"
    env.forge.calls.clear()
    env.sync({other: env2_tasks})
    assert env.forge.calls == [(other, 1)]            # and the walk is short again


def test_a_failed_task_list_is_recorded_per_repo_not_silently_empty(env):
    report = env.sync({LYNCEUS: [_task(1)]}, **{LYNCEUS: 500})
    assert any(LYNCEUS in f and "500" in f for f in report["failures"])
    err = env.con.execute("SELECT error FROM ci_sync WHERE repo=?", (LYNCEUS,)).fetchone()[0]
    assert err == "HTTP 500"
    assert env.con.execute("SELECT COUNT(*) FROM ci_task").fetchone()[0] == 0


def test_one_repo_failing_does_not_stop_the_others(env):
    report = env.sync({"o/bad": [], LYNCEUS: [_task(1)]}, **{"o/bad": 503})
    assert env.con.execute("SELECT COUNT(*) FROM ci_task WHERE repo=?", (LYNCEUS,)).fetchone()[0] == 1
    assert len(report["failures"]) == 1


def test_a_recovered_repo_clears_its_error_and_keeps_its_last_good_time(env):
    env.sync({LYNCEUS: [_task(1)]})
    good = env.con.execute("SELECT succeeded_at FROM ci_sync").fetchone()[0]
    env.sync({LYNCEUS: [_task(1)]}, **{LYNCEUS: 500})
    ok, err = env.con.execute("SELECT succeeded_at, error FROM ci_sync").fetchone()
    assert err == "HTTP 500" and ok == good          # the last GOOD time is not overwritten
    env.sync({LYNCEUS: [_task(1)]})
    assert env.con.execute("SELECT error FROM ci_sync").fetchone()[0] is None


def test_repos_are_discovered_when_none_are_named_and_no_actions_repos_skipped(env):
    forge = FakeForge({LYNCEUS: [_task(1)]})
    env.ci.sync(env.con, forge=forge)
    assert [c[0] for c in forge.calls] == [LYNCEUS]              # o/no-actions never asked


def test_a_repo_that_is_not_owner_slash_name_is_refused_not_used(env):
    report = env.ci.sync(env.con, forge=FakeForge({}), repos=["../etc", "a/b/c", "nope"])
    assert len(report["failures"]) == 3


def test_a_missing_token_does_not_stop_bodies_already_owed_from_being_read(env, monkeypatch):
    env.sync({LYNCEUS: [_task(1, status="failure")]})            # absent: young, retried
    env.log(LYNCEUS, 1, "arrived\n")
    monkeypatch.setenv("FLEET_CI_TOKEN_CMD", "exit 1")
    report = env.ci.sync(env.con)
    assert any("no Forgejo token" in f for f in report["failures"])
    assert env.row(LYNCEUS, 1)[1] == "stored"


# ------------------------------------------------------------ the token
def test_the_admin_helper_is_refused(monkeypatch):
    ci = _load("fleet-ci")
    monkeypatch.setenv("FLEET_CI_TOKEN_CMD", "~/bin/fetch-forgejo-admin-token.sh")
    with pytest.raises(ci.TokenError):
        ci._token()


def test_fleet_token_cmd_is_not_consulted_because_it_can_name_the_admin_helper(monkeypatch):
    ci = _load("fleet-ci")
    monkeypatch.setenv("FLEET_TOKEN_CMD", "echo admin-token-must-not-be-used")
    monkeypatch.setenv("FLEET_CI_TOKEN_CMD", "echo implbot-token")
    assert ci._token() == "implbot-token"


def test_a_failing_helper_never_puts_its_output_in_the_error(monkeypatch):
    ci = _load("fleet-ci")
    monkeypatch.setenv("FLEET_CI_TOKEN_CMD", "echo LEAKED-VALUE >&2; exit 1")
    with pytest.raises(ci.TokenError) as e:
        ci._token()
    assert "LEAKED-VALUE" not in str(e.value)


# --------------------------------------------- one ssh, no master, no argv leak
def test_one_ssh_per_sweep_with_no_master_and_paths_on_stdin_not_argv(env, tmp_path, monkeypatch):
    calls = tmp_path / "ssh-calls"
    fake = tmp_path / "fakebin"
    fake.mkdir()
    # Records its argv, then does what ssh does: hands the last word to a shell.
    args = tmp_path / "ssh-args"
    _script(fake / "ssh",
            f'#!/bin/sh\necho call >> {calls}\nfor a; do printf \'%s\\0\' "$a"; done > {args}\n'
            'for last; do :; done\nexec /bin/sh -c "$last"\n')
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(env.ci, "LOG_HOST", "cihost")
    monkeypatch.setattr(env.ci, "REMOTE_PYTHON", sys.executable)
    for tid in (1, 2, 3):
        env.log(LYNCEUS, tid, f"boom {tid}\n")
    env.sync({LYNCEUS: [_task(i, status="failure", run=i) for i in (3, 2, 1)]})
    assert calls.read_text().splitlines() == ["call"], "three logs, ONE connection"
    argv = args.read_bytes().decode().split("\0")[:-1]
    for want in ("BatchMode=yes", "ControlMaster=no", "ControlPath=none", "cihost"):
        assert want in argv
    assert env.ci.ZSTD in argv[-1]                               # by absolute path
    assert ".log.zst" not in " ".join(argv)                      # paths went on stdin, not argv
    assert [env.row(LYNCEUS, t)[1] for t in (1, 2, 3)] == ["stored"] * 3
    # A second sweep owes nothing, so it dials nothing at all.
    env.sync({LYNCEUS: [_task(i, status="failure", run=i) for i in (3, 2, 1)]})
    assert calls.read_text().splitlines() == ["call"]


def test_the_real_ssh_argv_names_no_master_and_the_absolute_zstd(env, monkeypatch):
    monkeypatch.setattr(env.ci, "LOG_HOST", "cihost")
    defaults = _load("fleet-ci")                     # the constants, before any test patches them
    assert defaults.ZSTD == "/opt/homebrew/bin/zstd"
    assert defaults.REMOTE_PYTHON == "/usr/bin/python3"
    argv = env.ci._log_argv()
    assert argv[0] == "ssh" and argv.count("ssh") == 1
    assert "ControlMaster=no" in argv and "ControlPath=none" in argv
    assert "BatchMode=yes" in argv
    assert env.ci.ZSTD in argv[-1] and "/usr/bin/python3" in argv[-1]


def test_a_host_that_begins_with_a_dash_is_refused(env, monkeypatch):
    monkeypatch.setattr(env.ci, "LOG_HOST", "-oProxyCommand=x")
    with pytest.raises(ValueError):
        env.ci._log_argv()


def test_an_unreachable_host_leaves_the_logs_pending_and_says_so(env, monkeypatch):
    monkeypatch.setattr(env.ci, "LOG_HOST", "local")
    monkeypatch.setattr(env.ci, "REMOTE_PYTHON", "/nonexistent/python")
    monkeypatch.setattr(env.ci.sys, "executable", "/nonexistent/python")
    report = env.sync({LYNCEUS: [_task(1, status="failure")]})
    status, state, detail, _ = env.row(LYNCEUS, 1)
    assert state == "pending" and "not read yet" in detail      # not "absent", not "zero"
    assert any("logs" in f for f in report["failures"])


def test_the_remote_script_refuses_to_leave_the_log_root(env):
    stream = env.ci._Stream(["../../etc/passwd", "/etc/passwd", "a//b", "a/./b"], 30)
    frames = list(stream)
    assert [h["state"] for h, _ in frames] == ["unreadable"] * 4
    assert all("refused" in h["why"] for h, _ in frames)


# --------------------------------------------- D4 — the join to dispatch
def _phase(env, lease, phase, seq, **facts):
    detail = json.dumps(facts)
    env.con.execute(
        "INSERT INTO dispatch_phase (lease, seq, ts, phase, detail_json, source, line)"
        " VALUES (?,?,?,?,?,?,?)", (lease, seq, _ts(), phase, detail, f"{lease}.log", seq))


def test_a_task_joins_to_its_dispatch_on_the_eight_character_sha_and_the_repo(env):
    sha = "8ce50dd1" + "0" * 32
    _phase(env, "L1", "start", 1, repo=LYNCEUS, plan="0011-x", session="s")
    _phase(env, "L1", "ci-resolved", 2, sha="8ce50dd1", state="failure")
    _phase(env, "L1", "review-round", 3, sha="8ce50dd1")          # same lease, same sha
    # A DIFFERENT repo whose dispatch happens to share the prefix must not join.
    _phase(env, "L2", "start", 1, repo="operator/other", plan="0099-y", session="s")
    _phase(env, "L2", "ci-waiting", 2, sha="8ce50dd1")
    env.con.commit()
    env.sync({LYNCEUS: [_task(1, status="failure", sha=sha), _task(2, sha="f" * 40)]})
    rows = env.con.execute(
        "SELECT task_id, lease, plan_id FROM ci_task_dispatch ORDER BY task_id").fetchall()
    assert rows == [(1, "L1", "0011-x"), (2, None, None)]        # one row each; no dup, no drop


def test_a_task_with_no_matching_dispatch_is_still_a_row(env):
    env.sync({LYNCEUS: [_task(1)]})
    assert env.con.execute("SELECT task_id, lease FROM ci_task_dispatch").fetchall() == [(1, None)]


# ------------------------------------------- redaction: the shape table (§4)
def _redacted(text):
    out, counts = _load("fleet-ci").redact(text)
    return out, counts


SHAPES = {
    "40-hex PAT in an Authorization header": (PAT_HEADER, HEX40),
    "a bare 40-hex string": (f"cloned with {HEX40} ok", HEX40),
    "keyword assignment": ("export TOKEN=hunter2hunter2", "hunter2hunter2"),
    "keyword assignment with a password": ("PASSWORD=correcthorsebattery", "correcthorsebattery"),
    "non-keyword-suffix assignment": ("AWS_SECRET_ACCESS_KEY=" + "K" * 40, "K" * 40),
    "AWS access key id (AKIA)": (f"key id {AKIA} rotated", AKIA),
    "AWS session key id (ASIA)": (f"key id {ASIA} rotated", ASIA),
    "OpenBao token": (f"got {HVS} from the vault", HVS),
    "basic auth header": (f"Authorization: Basic {B64}", B64),
    "basic auth in a url": ("git clone https://ci:s3cr3tpassw0rd@forge.example/x.git",
                            "s3cr3tpassw0rd"),
    # No known shape and not hex: only the header rule can catch these, and without
    # this row it could be deleted and every other row would still pass.
    "opaque bearer token": ("Authorization: Bearer " + "opaqueSecretValue123456",
                            "opaqueSecretValue123456"),
    "opaque token scheme": ("curl -H 'Authorization: token " + "Zq9-Zq9-Zq9-Zq9-Zq9-Zq9'",
                            "Zq9-Zq9-Zq9-Zq9-Zq9-Zq9"),
    "github token": (f"push with {GHP}", GHP),
    "gitlab token": ("glpat-" + "f" * 20, "f" * 20),
    "npm token": ("npm publish using " + "npm_" + "g" * 36, "g" * 36),
    "slack token": (f"webhook {SLACK}", SLACK),
    "sk- key": (f"OPENAI {SK}", SK),
    "JWT": (f"bearer {JWT} sent", JWT),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_shape_is_redacted(shape):
    text, secret = SHAPES[shape]
    out, counts = _redacted("before\n" + text + "\nafter\n")
    assert secret not in out, f"{shape}: the secret survived"
    assert "[redacted:" in out and counts
    assert out.startswith("before\n") and out.endswith("\nafter\n")      # neighbours untouched


def test_the_pat_header_is_claimed_by_the_header_rule_not_left_to_the_hex_catchall():
    out, counts = _redacted(PAT_HEADER)
    assert out == "[redacted:auth-header]" and counts == {"auth-header": 1}


def test_a_private_key_is_redacted_header_and_body_line():
    """The armour AND a body line — redacting only the header line leaves the key."""
    text = f"key follows\n{PEM_BEGIN}\n{PEM_BODY}\n{PEM_BODY}\n{PEM_END}\nafter\n"
    out, counts = _redacted(text)
    assert PEM_BODY not in out and PEM_BEGIN not in out and PEM_END not in out
    assert "MIIE" not in out
    assert out == "key follows\n[redacted:private-key]\nafter\n"
    assert counts == {"private-key": 1}


def test_a_private_key_with_a_timestamp_prefix_on_every_line_is_redacted():
    ts = "2026-09-12T18:41:23.4715730Z "
    text = "\n".join(ts + l for l in [PEM_BEGIN, PEM_BODY, PEM_BODY, PEM_END]) + "\nnext\n"
    out, _ = _redacted(text)
    assert PEM_BODY not in out and out.endswith("next\n")


def test_an_unterminated_private_key_loses_its_body_but_not_the_rest_of_the_log():
    text = f"{PEM_BEGIN}\n{PEM_BODY}\n{PEM_BODY}\nERROR: the build failed here\nmore output\n"
    out, _ = _redacted(text)
    assert PEM_BODY not in out
    assert "ERROR: the build failed here" in out and "more output" in out


def test_a_log_that_merely_mentions_the_header_keeps_what_follows():
    out, _ = _redacted(f"openssl: expected {PEM_BEGIN} line\nERROR: real cause\n")
    assert "ERROR: real cause" in out


def test_the_stored_text_carries_the_rule_name_and_not_the_matched_span(env):
    env.log(LYNCEUS, 1, f"id {AKIA}\n{PAT_HEADER}\nAWS_SECRET_ACCESS_KEY={'K' * 40}\n")
    env.sync({LYNCEUS: [_task(1, status="failure")]})
    stored = env.body(LYNCEUS, 1)
    for span in (AKIA, HEX40, "K" * 40, "Authorization: token"):
        assert span not in stored
    assert "[redacted:aws-key-id]" in stored
    assert "[redacted:auth-header]" in stored
    assert "[redacted:keyword-assignment]" in stored
    # Nor does the row's own bookkeeping carry a span: counts by name only.
    counts = json.loads(env.con.execute("SELECT redactions FROM ci_log").fetchone()[0])
    assert counts == {"aws-key-id": 1, "auth-header": 1, "keyword-assignment": 1}
    # And nothing anywhere in the database file holds the original, compressed or not.
    blob = env.db.read_bytes()
    assert AKIA.encode() not in blob and HEX40.encode() not in blob


def test_redaction_is_idempotent_and_leaves_ordinary_build_output_alone():
    ci = _load("fleet-ci")
    log = ("ERROR: Missing required icon file, exactly '120x120' pixels\n"
           "HEAD is now at 675730f plans: 0011\n"
           "npm WARN disk-image-size-warning-large-file-for-the-cache\n"
           "task-runner-configuration-value-is-fine\n"
           "cache key: node-modules-linux-x64\n"
           "FAILED tests/test_x.py::test_y - AssertionError\n")
    once, counts = ci.redact(log)
    assert once == log and counts == {}
    dirty = f"{PAT_HEADER}\nTOKEN=abcdefghij\n{GHP}\n{PEM_BEGIN}\n{PEM_BODY}\n{PEM_END}\n"
    a, _ = ci.redact(dirty)
    b, _ = ci.redact(a)
    assert a == b


def test_redaction_of_two_megabytes_of_hostile_input_is_not_quadratic():
    ci = _load("fleet-ci")
    for junk in ("a-" * (1 << 20), "A" * (2 << 20), "0123456789abcdef" * (1 << 17),
                 "token " * (1 << 18), ("x" * 63 + "\n") * (1 << 15)):
        t0 = time.monotonic()
        ci.redact(junk)
        assert time.monotonic() - t0 < 5, "redaction must stay linear on a 2 MiB log"


def test_metadata_is_shape_redacted_but_a_prose_title_and_the_sha_are_left_alone(env):
    env.sync({LYNCEUS: [_task(1, display_title=f"oops pasted {GHP}", head_branch="fix/x",
                              sha="d" * 40),
                        _task(2, display_title="fix: token: rotate weekly")]})
    t1 = env.con.execute("SELECT display_title, head_sha FROM ci_task WHERE id=1").fetchone()
    assert GHP not in t1[0] and "[redacted:github-token]" in t1[0]
    assert t1[1] == "d" * 40                                      # a sha column is not text
    assert env.con.execute("SELECT display_title FROM ci_task WHERE id=2").fetchone()[0] \
        == "fix: token: rotate weekly"


# ---------------------------------------------- plan 0069 D1 — the excerpt
def _pytest_body(n_filler=300, n_fail=2, n_tail=15):
    lines = [f"line {i}: setting up fixture" for i in range(n_filler)]
    lines += [f"FAILED tests/test_repo.py::test_{i} - AssertionError: boom {i}"
             for i in range(n_fail)]
    lines += [f"tail {i}" for i in range(n_tail)]
    return "\n".join(lines) + "\n"


def test_excerpt_keeps_the_failure_context_and_the_tail_and_states_the_counts():
    ci = _load("fleet-ci")
    body = _pytest_body()
    total = len(body.splitlines())
    out = ci.excerpt(body, redactions=3)
    header = out.splitlines()[0]
    assert header.startswith("[excerpt: ") and f"of {total} lines; " in header
    assert "3 redactions applied at capture]" in header
    assert "FAILED tests/test_repo.py::test_0" in out
    assert "FAILED tests/test_repo.py::test_1" in out
    assert "tail 14" in out                              # the very end of the tail
    assert "line 0: setting up fixture" not in out        # far filler is dropped
    assert len(out.splitlines()) - 1 <= ci.DEFAULT_EXCERPT_LINES


def test_excerpt_with_no_failure_shape_returns_the_tail_alone():
    ci = _load("fleet-ci")
    body = "\n".join(f"line {i}" for i in range(1, 500))
    out = ci.excerpt(body)
    lines = out.splitlines()
    assert lines[0].startswith("[excerpt:")
    assert lines[1:] == [f"line {i}" for i in range(500 - ci.EXCERPT_TAIL_LINES, 500)]


def test_excerpt_on_an_empty_body_is_the_header_alone():
    ci = _load("fleet-ci")
    assert ci.excerpt("") == "[excerpt: 0 of 0 lines; 0 redactions applied at capture]"
    assert ci.excerpt(None) == "[excerpt: 0 of 0 lines; 0 redactions applied at capture]"


def test_excerpt_is_bounded_by_lines_oldest_cut_first():
    ci = _load("fleet-ci")
    body = "\n".join(f"FAILED tests/test_x.py::test_{i} - Error" for i in range(200))
    out = ci.excerpt(body, lines=20)
    kept = out.splitlines()[1:]
    assert len(kept) <= 20
    assert "test_199" in out
    assert "::test_0 -" not in out                        # the oldest match is cut


def test_excerpt_is_bounded_by_bytes_oldest_cut_first():
    ci = _load("fleet-ci")
    body = "\n".join(f"FAILED tests/test_x.py::test_{i} - " + "z" * 200
                     for i in range(100))
    out = ci.excerpt(body, lines=1000)
    assert len(out.encode("utf-8")) <= ci.EXCERPT_BYTE_CAP + 200   # header + one line's slack
    assert "test_99 " in out
    assert "::test_0 -" not in out


def test_failing_test_ids_dedupes_in_order_and_caps_at_ten():
    ci = _load("fleet-ci")
    lines = [f"FAILED tests/test_x.py::test_{i} - AssertionError" for i in range(15)]
    lines.append(lines[0])                                # a duplicate
    ids = ci.failing_test_ids("\n".join(lines))
    assert len(ids) == 10
    assert ids[0] == "tests/test_x.py::test_0"


def test_excerpt_of_a_captured_body_never_adds_a_redaction_hit(env):
    """excerpt() selects whole lines out of a body ALREADY redacted at capture;
    it must never manufacture a new hit by, say, joining two now-adjacent lines
    across the ones it dropped."""
    bodies = [
        LYNCEUS_LOG,
        f"id {AKIA}\n{PAT_HEADER}\nAWS_SECRET_ACCESS_KEY={'K' * 40}\n"
        "FAILED tests/x.py::y - AssertionError\n",
        f"{PEM_BEGIN}\n{PEM_BODY}\n{PEM_BODY}\n{PEM_END}\nFAILED tests/x.py::y - Error\n",
    ]
    for i, raw in enumerate(bodies, start=100):
        env.log(LYNCEUS, i, raw)
        env.sync({LYNCEUS: [_task(i, status="failure")]})
        out = env.ci.task_excerpt(env.con, LYNCEUS, i)
        _, counts = env.ci.redact(out)
        assert counts == {}, (i, out)


# ------------------------------------------- plan 0069 D2/D3 — the read-only query surface
def test_context_job_strips_one_event_suffix_and_splits_on_the_first_slash():
    ci = _load("fleet-ci")
    assert ci.context_job("Plans / lint (push)") == "lint"
    assert ci.context_job("CI / tests (cihost) (push)") == "tests (cihost)"
    assert ci.context_job("CI / probe-reach (cihost-linux) (push)") == \
        "probe-reach (cihost-linux)"
    assert ci.context_job("no-event-suffix-here") == "no-event-suffix-here"


def test_match_context_joins_on_the_job_name_not_the_raw_context(env):
    env.sync({LYNCEUS: [_task(10, name="lint", sha="a" * 40, status="failure"),
                        _task(11, name="tests (cihost)", sha="a" * 40, status="failure")]})
    row = env.ci.match_context(env.con, LYNCEUS, "a" * 40, "Plans / lint (push)")
    assert row[0] == 10
    row2 = env.ci.match_context(env.con, LYNCEUS, "a" * 40, "CI / tests (cihost) (push)")
    assert row2[0] == 11


def test_match_context_picks_the_highest_id_on_a_rerun(env):
    env.sync({LYNCEUS: [_task(20, name="lint", sha="b" * 40, status="failure"),
                        _task(21, name="lint", sha="b" * 40, status="failure")]})
    row = env.ci.match_context(env.con, LYNCEUS, "b" * 40, "Plans / lint (push)")
    assert row[0] == 21


def test_task_excerpt_reads_the_stored_body(env):
    env.log(LYNCEUS, 30, LYNCEUS_LOG)
    env.sync({LYNCEUS: [_task(30, status="failure")]})
    out = env.ci.task_excerpt(env.con, LYNCEUS, 30)
    assert "120x120" in out and out.startswith("[excerpt:")


def test_task_excerpt_is_none_without_a_stored_body(env):
    env.sync({LYNCEUS: [_task(31, status="failure")]})           # absent: no ci_log row
    assert env.ci.task_excerpt(env.con, LYNCEUS, 31) is None


def test_open_ro_is_a_working_connection_that_cannot_write(env):
    con = env.ci.open_ro(str(env.db))
    assert con is not None
    assert con.execute("SELECT COUNT(*) FROM ci_task").fetchone() == (0,)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO ci_task (repo, id) VALUES ('o/r', 1)")


def test_open_ro_is_none_without_a_database_or_without_the_tables(tmp_path):
    ci = _load("fleet-ci")
    assert ci.open_ro(str(tmp_path / "nope.db")) is None
    old = tmp_path / "old.db"
    sqlite3.connect(old).executescript("CREATE TABLE session (id TEXT);")
    assert ci.open_ro(str(old)) is None


def test_terminal_without_body_waits_out_a_young_absent_row_but_not_an_old_one():
    ci = _load("fleet-ci")
    now = datetime.now(timezone.utc)
    assert ci.terminal_without_body("over-cap", _ts(0), now=now) is True
    assert ci.terminal_without_body("absent", _ts(60), now=now) is False
    assert ci.terminal_without_body("absent", _ts(2 * ci.RETRY_SECONDS), now=now) is True
    assert ci.terminal_without_body("pending", _ts(2 * ci.RETRY_SECONDS), now=now) is False


# ------------------------------------------------------------ D3 — the CLI
def _cli(env, capsys, *argv):
    ci = env.ci
    rc = ci.main(["--db", str(env.db), *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_failures_lists_failing_tasks_newest_first_with_their_body_state(env, capsys):
    env.log(LYNCEUS, 1, LYNCEUS_LOG)
    env.sync({LYNCEUS: [_task(3), _task(2, status="failure", run=2, ago=10),
                        _task(1, status="failure", run=1, ago=100)]})
    env.con.commit()
    rc, out, err = _cli(env, capsys, "failures", "lynceus")          # bare name is qualified
    assert rc == 0
    lines = [l for l in out.splitlines() if " failure " in l]
    assert [l.split()[0] for l in lines] == ["2", "1"]              # newest first; the pass omitted
    assert "2 failing of 3 tasks" in out
    assert "absent" in lines[0] and "stored" in lines[1]            # the body state is on the row
    assert "fleet-ci missing" in out                                # and points at why


def test_show_prints_the_stored_body_and_says_plainly_when_there_is_none(env, capsys):
    env.log(LYNCEUS, 1, LYNCEUS_LOG)
    env.sync({LYNCEUS: [_task(2, status="failure"), _task(1, status="failure"), _task(3)]})
    rc, out, err = _cli(env, capsys, "show", LYNCEUS, "1")
    assert rc == 0 and "120x120" in out and "CFBundleIconName" in out
    rc, out, err = _cli(env, capsys, "show", LYNCEUS, "2")
    assert rc == 1 and out == "" and "NO BODY: absent" in err
    rc, out, err = _cli(env, capsys, "show", LYNCEUS, "3")
    assert rc == 1 and "NO BODY: not-failed" in err
    rc, out, err = _cli(env, capsys, "show", LYNCEUS, "999")
    assert rc == 1 and "no task 999" in err


def test_excerpt_cli_prints_the_excerpt_and_says_plainly_when_there_is_none(env, capsys):
    env.log(LYNCEUS, 1, LYNCEUS_LOG)
    env.sync({LYNCEUS: [_task(2, status="failure"), _task(1, status="failure"), _task(3)]})
    rc, out, err = _cli(env, capsys, "excerpt", LYNCEUS, "1")
    assert rc == 0 and out.startswith("[excerpt:") and "120x120" in out
    rc, out, err = _cli(env, capsys, "excerpt", LYNCEUS, "2")
    assert rc == 1 and out == "" and "NO BODY: absent" in err
    rc, out, err = _cli(env, capsys, "excerpt", LYNCEUS, "999")
    assert rc == 1 and "no task 999" in err


def test_missing_says_why_for_each_task_without_a_body(env, capsys):
    env.monkeypatch.setattr(env.ci, "LOG_CAP", 100)
    env.log(LYNCEUS, 3, "x" * 500)
    env.sync({LYNCEUS: [_task(4, status="running"), _task(3, status="failure"),
                        _task(2, status="failure"), _task(1)]})
    rc, out, err = _cli(env, capsys, "missing", LYNCEUS)
    assert rc == 0
    assert "over-cap 1" in out and "absent 1" in out and "not-failed 1" in out
    assert "over-cap" in out and "absent: no file at" in out
    listed = [l for l in out.splitlines() if l[:6].strip().isdigit()]
    assert {l.split()[0] for l in listed} == {"3", "2", "4"}       # the pass is not listed...
    rc, out, _ = _cli(env, capsys, "missing", LYNCEUS, "--state", "not-failed")
    assert [l.split()[0] for l in out.splitlines() if l[:6].strip().isdigit()] == ["1"]   # ...unless asked


def test_a_database_without_the_tables_is_refused_loudly_not_answered_empty(tmp_path, capsys):
    ci = _load("fleet-ci")
    db = tmp_path / "old.db"
    sqlite3.connect(db).executescript("CREATE TABLE session (id TEXT);")
    rc = ci.main(["--db", str(db), "failures", "operator/lynceus"])
    err = capsys.readouterr().err
    assert rc == 2 and "no ci_task table" in err and "NOT 'no failing builds'" in err


def test_a_missing_database_is_refused_loudly(tmp_path, capsys):
    ci = _load("fleet-ci")
    rc = ci.main(["--db", str(tmp_path / "nope.db"), "failures", "operator/lynceus"])
    assert rc == 2 and "no database" in capsys.readouterr().err


def test_a_repo_never_collected_is_unknown_not_zero_failures(env, capsys):
    env.sync({LYNCEUS: [_task(1)]})
    rc, out, err = _cli(env, capsys, "failures", "operator/never-seen")
    assert rc == 1 and "unknown, not zero" in err and "never been synced" in err


def test_a_failed_sync_is_a_warning_above_the_answer(env, capsys):
    env.sync({LYNCEUS: [_task(1, status="failure")]})
    env.sync({LYNCEUS: []}, **{LYNCEUS: 401})
    rc, out, err = _cli(env, capsys, "failures", LYNCEUS)
    assert "WARNING: the last sync of operator/lynceus FAILED" in err and "HTTP 401" in err


def test_the_commands_only_read(env, capsys):
    env.log(LYNCEUS, 1, LYNCEUS_LOG)
    env.sync({LYNCEUS: [_task(1, status="failure")]})
    env.con.commit()
    env.con.close()
    before = env.db.read_bytes()
    for argv in (("failures", LYNCEUS), ("show", LYNCEUS, "1"), ("missing", LYNCEUS)):
        _cli(env, capsys, *argv)
    assert env.db.read_bytes() == before


# ------------------------------------- inside fleet-collect: one schema owner
def _collector(tmp_path, monkeypatch, **env_vars):
    fc = _load("fleet-collect")
    monkeypatch.setattr(fc, "DB", tmp_path / "fleet.db")
    monkeypatch.setattr(fc, "PROJECTS", tmp_path / "no-projects")
    monkeypatch.setattr(fc, "FLEET_DIR", tmp_path / "no-fleet-dir")
    monkeypatch.setattr(fc, "WORK_ROOT", tmp_path / "no-work-root")
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    return fc


class _FakeCi:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.called = 0

    def sync(self, con):
        self.called += 1
        con.execute("INSERT INTO ci_task (repo, id, status, log_state) VALUES ('o/r', 1, 'success', 'not-failed')")
        con.commit()
        return {"summary": "fake sweep", "failures": self.failures}


def test_the_ci_source_does_not_run_against_a_relocated_tree_unless_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("FLEET_CI_SOURCE", raising=False)
    fc = _collector(tmp_path, monkeypatch)
    monkeypatch.setattr(fc, "_load_ci", lambda: pytest.fail("a scratch tree must not reach the real forge"))
    assert fc.main() == 0
    assert not fc._ci_enabled()


def test_the_ci_source_runs_by_default_on_the_real_tree_and_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.delenv("FLEET_CI_SOURCE", raising=False)
    fc = _collector(tmp_path, monkeypatch)
    monkeypatch.setattr(fc, "FLEET_DIR", fc.DEFAULT_FLEET_DIR)
    assert fc._ci_enabled()
    monkeypatch.setenv("FLEET_CI_SOURCE", "0")
    assert not fc._ci_enabled()


def test_the_collector_creates_the_tables_and_runs_the_source(tmp_path, monkeypatch, capsys):
    fc = _collector(tmp_path, monkeypatch, FLEET_CI_SOURCE="1")
    fake = _FakeCi()
    monkeypatch.setattr(fc, "_load_ci", lambda: fake)
    assert fc.main() == 0
    assert fake.called == 1 and "ci: fake sweep" in capsys.readouterr().out
    con = sqlite3.connect(tmp_path / "fleet.db")
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"ci_task", "ci_log", "ci_sync", "ci_task_dispatch"} <= have


def test_a_failing_ci_source_costs_the_other_sources_nothing(tmp_path, monkeypatch, capsys):
    events = tmp_path / "fleet"
    events.mkdir()
    (events / "events.jsonl").write_text(json.dumps(
        {"ts": "2026-09-12T10:00:00Z", "actor": "a", "type": "plan-dispatched",
         "repo": "operator/ares", "detail": {}}) + "\n")
    fc = _collector(tmp_path, monkeypatch, FLEET_CI_SOURCE="1")
    monkeypatch.setattr(fc, "FLEET_DIR", events)

    def boom():
        raise RuntimeError("fleet-ci is missing from this pin")

    monkeypatch.setattr(fc, "_load_ci", boom)
    assert fc.main() == 1                                    # reported, not swallowed
    assert "ci source failed" in capsys.readouterr().err
    assert sqlite3.connect(tmp_path / "fleet.db").execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1


def test_source_failures_make_the_sweep_exit_nonzero_but_the_rows_are_kept(tmp_path, monkeypatch, capsys):
    fc = _collector(tmp_path, monkeypatch, FLEET_CI_SOURCE="1")
    monkeypatch.setattr(fc, "_load_ci", lambda: _FakeCi(failures=["o/r: task list -> HTTP 500"]))
    assert fc.main() == 1
    assert "WARN: ci: o/r: task list -> HTTP 500" in capsys.readouterr().err
    assert sqlite3.connect(tmp_path / "fleet.db").execute("SELECT COUNT(*) FROM ci_task").fetchone()[0] == 1


def test_the_ci_tables_need_no_schema_bump_and_survive_a_rebuild(tmp_path, monkeypatch):
    """Adding them is CREATE IF NOT EXISTS on a v3 database, and a later bump
    must not drop them: Forgejo can prune the logs they were read from."""
    fc = _collector(tmp_path, monkeypatch)
    assert fc.SCHEMA_VERSION == 3, "the CI tables were added without a schema bump"
    db = tmp_path / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(fc.SCHEMA)
    con.execute("INSERT INTO ci_task (repo, id, status, log_state) VALUES ('o/r', 7, 'failure', 'stored')")
    con.execute("INSERT INTO ci_log VALUES ('o/r', 7, x'00', 1, 1, '{}', 't')")
    con.execute(f"PRAGMA user_version = {fc.SCHEMA_VERSION - 1}")      # force the rebuild path
    con.commit()
    con.close()
    fc.main()
    con = sqlite3.connect(db)
    assert con.execute("SELECT id FROM ci_task").fetchall() == [(7,)]
    assert con.execute("SELECT task_id FROM ci_log").fetchall() == [(7,)]
    src = (BIN / "fleet-collect").read_text()
    drop = src[src.index("for t in ("): src.index("):", src.index("for t in ("))]
    assert "ci_" not in drop


def test_an_existing_database_gains_the_tables_without_losing_a_row(tmp_path, monkeypatch):
    fc = _collector(tmp_path, monkeypatch)
    db = tmp_path / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(fc.SCHEMA)
    for t in ("ci_task", "ci_log", "ci_sync"):                          # a pre-0045 database
        con.execute(f"DROP TABLE {t}")
    con.execute("INSERT INTO session (id) VALUES ('kept')")
    con.execute(f"PRAGMA user_version = {fc.SCHEMA_VERSION}")
    con.commit()
    con.close()
    fc.main()
    con = sqlite3.connect(db)
    assert con.execute("SELECT id FROM session").fetchall() == [("kept",)]
    assert con.execute("SELECT COUNT(*) FROM ci_task").fetchone()[0] == 0


def test_fleet_ci_writes_no_schema_and_stamps_no_version():
    """The boundary: fleet-ci is a source, not a second writer."""
    src = (BIN / "fleet-ci").read_text()
    code = src.split('"""', 2)[2]                       # past the module docstring
    code = "\n".join(l for l in code.splitlines() if not l.lstrip().startswith("#"))
    assert "CREATE TABLE" not in code and "user_version" not in code
    assert "executescript" not in code


def test_nothing_automated_reads_the_ci_tables():
    """SPEC Principle 3: awareness, not authority.

    bin/orchestrator is the one sanctioned exception (plan 0069): a fix round
    on a red build is briefed with what fleet-collect has already stored, read
    read-only through fleet-ci's own query surface (`open_ro`/`match_context`/
    `task_excerpt`) rather than a second copy of the schema. It is proven
    elsewhere (tests/test_orchestrator_ci.py) never to call fleet-ci's sync()
    or dial Forgejo/a runner for this. Every other automated surface stays out."""
    for name in ("fleet-watch", "fleetlib.py", "fleetjob.py"):
        text = (BIN / name).read_text()
        assert "ci_task" not in text and "ci_log" not in text, name


def test_no_admin_token_and_no_forgejo_database_in_the_source():
    code = (BIN / "fleet-ci").read_text()
    assert "fetch-forgejo-admin-token" not in code.replace(
        "~/bin/fetch-forgejo-admin-token.sh", "")                 # only ever named to refuse it
    assert "forgejo.db" not in code and "sqlite3.connect(\"/opt" not in code


# ------------------------------------------------------------------ D5 — docs
def test_the_docs_describe_four_sources_and_the_ci_tables():
    doc = (ROOT / "docs" / "fleet-db.md").read_text()
    assert "four sources" in doc.lower()
    for word in ("ci_task", "ci_log", "ci_sync", "ci_task_dispatch", "fleet-ci",
                 "head_sha", "over-cap", "actions_log"):
        assert word in doc, word
    assert "The three sources" not in doc
