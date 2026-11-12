"""Tests for bin/fleet-meet — a test lane's tests meet the implementation (plan 0102).

Everything is real except the forge: local git repositories stand in for the
forge's git, and a `http.server` in a background thread answers the three read
verbs `fleet-meet` may call. No network, no vendor. The lane's tests run in a
real pytest subprocess.

By default those subprocesses run WITHOUT the sandbox profile (`USE_PROFILE`
off: the test-only seam that is also the control for every confinement probe),
so the suite passes on any host. The confinement probes, which need macOS and
`sandbox-exec`, run only with FLEET_MEET_LIVE=1 and each is run twice — confined,
and again with the profile removed, which must succeed.
"""
import contextlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


mod = _load("fleet_meet_under_test", BIN / "fleet-meet")

REPO = "operator/demo"
PLAN = "0900-a-plan-under-meeting"
HEX64 = "ab" * 32
LIVE = os.environ.get("FLEET_MEET_LIVE") == "1" and sys.platform == "darwin"
live = pytest.mark.skipif(not LIVE, reason="needs macOS and FLEET_MEET_LIVE=1")

_ENV_KEYS = ("EUNOMIA_FLEET_DIR", "EUNOMIA_SESSION", "FLEET_WORK_ROOT", "FLEET_GIT_SSH_BASE",
             "FLEET_FORGEJO_URL", "FLEET_AUTHOR_TOKEN_CMD", "FLEET_FORGE_TIMEOUT", "FLEET_DB",
             "FLEET_AUTHOR_ACCOUNT", "FLEET_MERGER_ACCOUNT", "FLEET_REVIEWER_ACCOUNT",
             "FLEET_AGENT_ACCOUNTS", "FLEET_IMPLEMENTER_LOGINS")


@pytest.fixture(autouse=True)
def _seams(monkeypatch):
    """Off-profile by default; the seams are restored after every test."""
    monkeypatch.setattr(mod, "USE_PROFILE", False)
    monkeypatch.setattr(mod, "SANDBOX_EXEC", mod.SANDBOX_EXEC)
    monkeypatch.setattr(mod, "SWEEP_ENABLED", True)


def _git(args, cwd, check=True):
    r = subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=f@e.com", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    if check:
        assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r


# ------------------------------------------------------------------ the forge
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        self.server.requests.append(("GET", self.path))
        status, body = self.server.route(u.path, q)
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):          # nothing here may ever be asked to write
        self.server.requests.append(("POST", self.path))
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_PATCH = do_PUT = do_DELETE = do_POST


def _page(items, q):
    page = int(q.get("page", ["1"])[0])
    limit = int(q.get("limit", ["50"])[0])
    return items[(page - 1) * limit: page * limit]


class World:
    """A fixture repository (main, an implementation branch, lane branches), the
    bare repo standing in for the forge's git, and a stub forge over both."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.src = tmp_path / "src"
        self.src.mkdir()
        _git(["init", "-q", "-b", "main"], self.src)
        self._write(self.src, {
            "tests/conftest.py": "# existing conftest\n",
            "tests/test_existing.py": "def test_e():\n    pass\n",
            "tests/lane/test_base.py": "def test_b():\n    pass\n",
            "README": "demo\n"})
        _git(["add", "."], self.src)
        _git(["commit", "-qm", "root"], self.src)
        self.bare = tmp_path / "forge" / "operator" / "demo.git"
        self.bare.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(self.src), str(self.bare)], check=True)
        self.pulls = {}
        self.reviews = {}
        self.extra_list = []
        self.list_status = 200
        self.on_reviews = None
        self.n = 10
        self.impl_pr = self.impl_sha = None

    @staticmethod
    def _write(root, files, modes=None):
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

    def branch(self, name, files, base="main", symlinks=None, executable=(), message="c"):
        _git(["checkout", "-q", "-B", name, base], self.src)
        self._write(self.src, files)
        for rel, target in (symlinks or {}).items():
            (self.src / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, self.src / rel)
        for rel in executable:
            os.chmod(self.src / rel, 0o755)
        _git(["add", "-A"], self.src)
        _git(["commit", "-qm", message, "--allow-empty"], self.src)
        sha = _git(["rev-parse", "HEAD"], self.src).stdout.strip()
        _git(["push", "-q", "-f", str(self.bare), f"{name}:{name}"], self.src)
        _git(["checkout", "-q", "main"], self.src)
        return sha

    def _pull(self, n, user, ref, sha, body, state="open", merged=False):
        self.pulls[n] = {"number": n, "state": state, "merged": merged, "user": {"login": user},
                         "body": body, "head": {"ref": ref, "sha": sha}}
        self.reviews.setdefault(n, [])
        return n

    def impl(self, files=None, user="implbot", body=None, **kw):
        sha = self.branch("agent/impl", files or {"bin/thing": "x\n"})
        self.impl_sha = sha
        self.impl_pr = self._pull(1, user, "agent/impl", sha,
                                  body if body is not None else f"Plan: {PLAN}\n", **kw)
        return self.impl_pr

    def lane(self, files, runner="sub-opus", plan=PLAN, prompt=HEX64, approve="operator",
             body=None, user="implbot", branch_name=None, **branch_kw):
        n = self.n = self.n + 1
        ref = branch_name or f"agent/tests/{plan}/{runner}"
        sha = self.branch(ref, files, **branch_kw)
        if body is None:
            body = mod.lane.build_pr_body(plan, runner, "1" * 40, prompt, None, [], "n")
        self._pull(n, user, ref, sha, body)
        if approve:
            self.approve(n, approve, sha)
        return n, sha

    def approve(self, n, by="operator", commit=None, state="APPROVED", **flags):
        rid = len(self.reviews[n]) + 100
        self.reviews[n].append({"id": rid, "state": state, "commit_id": commit or self.pulls[n]["head"]["sha"],
                                "user": {"login": by}, "dismissed": False, "stale": False,
                                "submitted_at": "2026-09-29T10:00:00Z", **flags})
        return rid

    def route(self, path, q):
        m = re.fullmatch(r"/api/v1/repos/operator/demo/pulls(?:/(\d+)(/reviews)?)?", path)
        if not m:
            return 404, None
        n, rv = m.group(1), m.group(2)
        if n is None:
            if self.list_status != 200 and int(q.get("page", ["1"])[0]) > 1:
                return self.list_status, None
            return 200, _page(list(self.pulls.values()) + self.extra_list, q)
        n = int(n)
        if n not in self.pulls:
            return 404, None
        if rv:
            if self.on_reviews:
                self.on_reviews(n)
            r = self.reviews[n]
            if callable(r):
                return r(q)
            return 200, _page(r, q)
        return 200, self.pulls[n]


@contextlib.contextmanager
def _serve(world):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.route, httpd.requests = world.route, []
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    with _serve(w) as (httpd, url):
        w.httpd = httpd
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({"EUNOMIA_FLEET_DIR": str(tmp_path / "fleet"),
                           "EUNOMIA_SESSION": "meet-test", "FLEET_WORK_ROOT": str(tmp_path / "work"),
                           "FLEET_GIT_SSH_BASE": str(tmp_path / "forge"), "FLEET_FORGEJO_URL": url,
                           "FLEET_AUTHOR_TOKEN_CMD": "echo test-token"})
        yield w


def meet(w, *extra, dry=False, impl=None):
    return mod.main([REPO, PLAN, "--impl", str(impl or w.impl_pr), *map(str, extra)]
                    + (["--dry-run"] if dry else []))


def doc_path(w):
    return mod.findings_path(PLAN, w.impl_sha)


def load_doc(w):
    doc = json.loads(doc_path(w).read_text())
    schema = json.loads((ROOT / "config" / "meeting-findings.schema.json").read_text())
    assert mod.lane.validate_schema(doc, schema) == []
    return doc


def marker_test(marker, name="test_m.py"):
    """A lane file that writes `marker` when IMPORTED — so a lane that ran (or
    was even imported) leaves a mark, and one that was refused leaves none."""
    return {f"tests/lane/{name}": f"open({str(marker)!r}, 'w').write('imported')\n\n\ndef test_ok():\n    pass\n"}


# ============================================================ the precondition
def test_an_approval_by_the_human_at_the_head_runs_and_exits_zero(world, capsys):
    world.impl()
    marker = world.tmp / "marker"
    n, sha = world.lane(marker_test(marker))
    assert meet(world) == 0
    assert marker.exists()
    assert capsys.readouterr().out.strip().splitlines()[-1] == str(doc_path(world))
    doc = load_doc(world)
    (ln,) = doc["lanes"]
    assert ln["head_sha"] == sha and ln["review_id"] and ln["read_by"] == "operator"
    assert ln["independent_reader"] is False
    assert doc["impl"] == {"pr": 1, "head_sha": world.impl_sha}


def _no_review(w, n, sha):
    w.reviews[n] = []


def _by_agent(w, n, sha):
    w.reviews[n] = []
    w.approve(n, "revbot", sha)


def _by_implbot(w, n, sha):
    w.reviews[n] = []
    w.approve(n, "implbot", sha)


def _earlier_head(w, n, sha):
    w.reviews[n] = []
    w.approve(n, "operator", "2" * 40)


def _dismissed(w, n, sha):
    w.reviews[n] = []
    w.approve(n, "operator", sha, dismissed=True)


def _stale(w, n, sha):
    w.reviews[n] = []
    w.approve(n, "operator", sha, stale=True)


def _comment(w, n, sha):
    w.reviews[n] = []
    w.approve(n, "operator", sha, state="COMMENT")


def _merged_no_approval(w, n, sha):
    w.reviews[n] = []
    w.pulls[n].update(state="closed", merged=True)


def _review_500(w, n, sha):
    w.reviews[n] = lambda q: (500, None)


def _review_paging_stops(w, n, sha):
    full = [{"id": i, "state": "COMMENT", "commit_id": sha, "user": {"login": "x"}} for i in range(50)]
    w.reviews[n] = lambda q: (200, full) if q.get("page") == ["1"] else (500, None)


@pytest.mark.parametrize("break_it", [
    _no_review, _by_agent, _by_implbot, _earlier_head, _dismissed, _stale, _comment,
    _merged_no_approval, _review_500, _review_paging_stops])
def test_each_failure_of_the_precondition_runs_nothing_and_exits_75(world, break_it):
    world.impl()
    marker = world.tmp / "marker"
    n, sha = world.lane(marker_test(marker), approve=None)
    break_it(world, n, sha)
    assert meet(world) == mod.EXIT_LATER == 75
    assert not marker.exists(), "a refused lane must not even be imported"
    assert not doc_path(world).exists()


def test_a_review_listing_that_raises_is_not_met(world, monkeypatch):
    world.impl()
    marker = world.tmp / "marker"
    world.lane(marker_test(marker))

    def boom(*a, **k):
        raise RuntimeError("listing exploded")
    real = mod.fleetforge.Forge.list_reviews
    monkeypatch.setattr(mod.fleetforge.Forge, "list_reviews", boom)
    assert meet(world) == 75
    assert not marker.exists() and not doc_path(world).exists()
    monkeypatch.setattr(mod.fleetforge.Forge, "list_reviews", real)   # the control: healthy, it runs
    assert meet(world) == 0 and marker.exists()


def test_a_push_between_the_check_and_the_fetch_runs_the_approved_file(world):
    """The stub reports head H with an approval at H; once the reviews have been
    read the lane branch moves to H2, whose file writes a different marker."""
    world.impl()
    good, moved = world.tmp / "marker-H", world.tmp / "marker-H2"
    n, sha = world.lane(marker_test(good))

    def push(_n):
        world.on_reviews = None
        world.branch(world.pulls[n]["head"]["ref"], marker_test(moved))
    world.on_reviews = push
    assert meet(world) == 0
    assert good.exists() and not moved.exists()
    assert load_doc(world)["lanes"][0]["head_sha"] == sha


# ========================================================================= paths
def _paths_case(world, files, **kw):
    world.impl(files={"bin/thing": "x\n", "tests/lane/test_clash.py": "def test_c():\n    pass\n"})
    marker = world.tmp / "marker"
    files = {**files, "tests/lane/test_imports.py": f"open({str(marker)!r}, 'w').write('x')\n"}
    world.lane(files, **kw)
    assert meet(world) == 0
    assert not marker.exists(), "a refused lane's tests are not imported"
    (ln,) = load_doc(world)["lanes"]
    assert ln["run"]["outcome"] == "refused" and ln["tests"] == []
    return ln["run"]["reason"]


def test_a_lane_that_modifies_tests_conftest_is_refused(world):
    assert "status 'M'" in _paths_case(world, {"tests/conftest.py": "# changed\n"})


def test_a_lane_that_modifies_a_tests_lane_file_at_its_merge_base_is_refused(world):
    assert "tests/lane/test_base.py: status 'M'" in _paths_case(
        world, {"tests/lane/test_base.py": "def test_b():\n    assert 0\n"})


def test_a_lane_that_adds_outside_tests_lane_is_refused(world):
    assert "bin/x: not under tests/lane/" in _paths_case(world, {"bin/x": "#!/bin/sh\n"})


def test_a_lane_that_adds_an_executable_is_refused(world):
    reason = _paths_case(world, {"tests/lane/test_x.py": "def test_x():\n    pass\n"},
                         executable=("tests/lane/test_x.py",))
    assert "mode 100755" in reason


def test_a_lane_that_adds_a_symlink_is_refused(world):
    assert "mode 120000" in _paths_case(world, {}, symlinks={"tests/lane/link.py": "/etc/passwd"})


def test_a_lane_whose_file_already_exists_at_the_implementation_head_is_refused(world):
    assert "already exists at the implementation head" in _paths_case(
        world, {"tests/lane/test_clash.py": "def test_c():\n    assert 0\n"})


# ===================================================================== selection
def test_a_named_lane_of_another_plan_is_refused_with_exit_1(world):
    world.impl()
    n, _ = world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"}, plan="0901-another")
    assert meet(world, "--lane", n) == 1
    assert not doc_path(world).exists()


def test_a_named_lane_without_tests_for_in_its_header_is_refused(world):
    world.impl()
    body = f"runner: sub-opus\n\nTests-for: {PLAN}\n"        # after the blank line: not the header
    n, _ = world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"}, body=body)
    assert meet(world, "--lane", n) == 1


def test_a_lane_authored_by_someone_else_is_refused(world):
    world.impl()
    n, _ = world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"}, user="stranger")
    assert meet(world, "--lane", n) == 1


@pytest.mark.parametrize("bad", [dict(user="stranger"), dict(body="no marker\n"),
                                 dict(state="closed", merged=False)])
def test_a_bad_implementation_pr_is_refused_with_exit_1(world, bad):
    world.impl(**bad)
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    assert meet(world) == 1 and not doc_path(world).exists()


def test_a_merged_implementation_pr_is_accepted(world):
    world.impl(state="closed", merged=True)
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    assert meet(world) == 0


def test_discovery_finds_a_lane_on_page_three_of_120_pulls(world):
    world.impl()
    world.extra_list = [{"number": 1000 + i, "state": "closed", "merged": True,
                         "user": {"login": "x"}, "body": "b", "head": {"ref": f"r{i}", "sha": "a" * 40}}
                        for i in range(120)]
    n, _ = world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    assert meet(world) == 0
    assert [l["pr"] for l in load_doc(world)["lanes"]] == [n]
    pages = [p for m, p in world.httpd.requests if m == "GET" and p.split("?")[0].endswith("/pulls")]
    assert any("page=3" in p for p in pages)


def test_a_listing_that_does_not_finish_exits_1_and_writes_nothing(world):
    world.impl()
    world.extra_list = [{"number": 2000 + i, "state": "closed", "user": {"login": "x"}, "body": "",
                         "head": {"ref": "r", "sha": "a" * 40}} for i in range(60)]
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    world.list_status = 500
    assert meet(world) == 1 and not doc_path(world).exists()


def test_usage_errors_exit_2(world):
    with pytest.raises(SystemExit) as e:
        mod.main([REPO, PLAN])
    assert e.value.code == 2
    assert mod.main(["nonsense", PLAN, "--impl", "1"]) == 2
    assert mod.main([REPO, "a/b", "--impl", "1"]) == 2


# ==================================================================== the outcomes
OUTCOME_FILE = '''\
import pytest


@pytest.fixture
def broken():
    raise RuntimeError("fixture went wrong")


def test_passes():
    pass


def test_assert_fails():
    assert 1 == 2, "the sum is wrong"


def test_fixture_error(broken):
    pass


def test_contract_silent():
    pytest.skip("contract silent")


@pytest.mark.xfail(reason="known gap")
def test_expected_failure():
    assert 0


@pytest.mark.xfail(reason="maybe fixed")
def test_unexpected_pass():
    pass
'''

EXPECTED = {
    "test_passes": ("pass", "passed"), "test_assert_fails": ("fail", "failed"),
    "test_fixture_error": ("error", "error"), "test_contract_silent": ("skip", "skipped"),
    "test_expected_failure": ("skip", "xfailed"), "test_unexpected_pass": ("pass", "xpassed")}


def check_outcomes(tests):
    by = {t["id"].split("::")[-1]: t for t in tests if "::" in t["id"]}
    for name, (outcome, raw) in EXPECTED.items():
        assert (by[name]["outcome"], by[name]["pytest_outcome"]) == (outcome, raw), name
        assert by[name]["source_path"] == "tests/lane/test_kinds.py"
    assert by["test_passes"]["reason"] is None
    assert "the sum is wrong" in by["test_assert_fails"]["reason"]
    assert "fixture went wrong" in by["test_fixture_error"]["reason"]
    assert by["test_contract_silent"]["reason"] == "contract silent"
    assert by["test_expected_failure"]["reason"] == "xfail: known gap"


def test_a_lane_with_one_test_of_each_kind_has_the_right_outcomes(world):
    world.impl()
    world.lane({"tests/lane/test_kinds.py": OUTCOME_FILE,
                "tests/lane/test_syntax.py": "def test_x(:\n",
                "tests/lane/test_nothing.py": "x = 1\n"})
    assert meet(world) == 0
    (ln,) = load_doc(world)["lanes"]
    check_outcomes(ln["tests"])
    syntax = [t for t in ln["tests"] if t["id"] == "tests/lane/test_syntax.py"]
    assert len(syntax) == 1 and syntax[0]["outcome"] == "error"
    (none,) = [t for t in ln["tests"] if t["id"] == "tests/lane/test_nothing.py"]
    assert (none["outcome"], none["reason"]) == ("error", "no tests collected")
    assert ln["counts"] == {"pass": 2, "fail": 1, "error": 3, "skip": 2}
    assert ln["run"]["outcome"] == "collection-error"
    assert load_doc(world)["all_passed"] is False


def test_the_control_skip_mapped_to_pass_fails_the_outcome_check(world, monkeypatch):
    world.impl()
    world.lane({"tests/lane/test_kinds.py": OUTCOME_FILE})
    real = mod._classify

    def skip_is_pass(r):
        got = real(r)
        return ("pass", "passed", None) if got and got[0] == "skip" else got
    monkeypatch.setattr(mod, "_classify", skip_is_pass)
    assert meet(world) == 0
    with pytest.raises(AssertionError):
        check_outcomes(load_doc(world)["lanes"][0]["tests"])


def test_all_passed_is_false_with_one_skip_and_true_for_passes_only(world):
    world.impl()
    world.lane({"tests/lane/test_a.py": "import pytest\n\n\ndef test_a():\n    pass\n\n\n"
                                        "def test_b():\n    pytest.skip('why')\n"})
    assert meet(world) == 0
    assert load_doc(world)["all_passed"] is False
    world.lane({"tests/lane/test_c.py": "def test_c():\n    pass\n"}, runner="sub-codex")
    world.reviews[11] = []          # the first lane is no longer approved: only the passing one runs
    assert meet(world) == 0
    doc = load_doc(world)
    assert [l["runner"] for l in doc["lanes"]] == ["sub-codex"]
    assert doc["not_run"] and doc["all_passed"] is False        # an unread lane is not a green
    world.pulls.pop(11)
    world.reviews.pop(11)
    assert meet(world) == 0
    assert load_doc(world)["all_passed"] is True


def test_a_lane_with_no_tests_is_never_all_passed(world):
    world.impl()
    world.lane({"tests/lane/helper.py": "x = 1\n"})
    assert meet(world) == 0
    doc = load_doc(world)
    assert doc["lanes"][0]["run"]["outcome"] == "refused" and doc["all_passed"] is False


def test_a_timeout_is_recorded_and_exits_zero(world, monkeypatch):
    monkeypatch.setattr(mod, "MEET_TIMEOUT_SECONDS", 2)
    world.impl()
    world.lane({"tests/lane/test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"})
    assert meet(world) == 0
    (ln,) = load_doc(world)["lanes"]
    assert ln["run"]["outcome"] == "timeout" and ln["run"]["pytest_exit"] is None


# ========================================================================= isolation
def test_two_lanes_each_adding_a_conftest_see_only_their_own_fixture(world):
    world.impl()
    for runner, val in (("sub-opus", "A"), ("sub-codex", "B")):
        world.lane({"tests/lane/conftest.py": f"import pytest\n\n\n@pytest.fixture\ndef which():\n    return {val!r}\n",
                    "tests/lane/test_iso.py": f"def test_iso(which):\n    assert which == {val!r}\n"},
                   runner=runner)
    assert meet(world) == 0
    lanes = load_doc(world)["lanes"]
    assert len(lanes) == 2
    assert all(l["counts"] == {"pass": 1, "fail": 0, "error": 0, "skip": 0} for l in lanes)


# ==================================================================== process sweep
_CHILD_TEST = '''\
import os, time


def test_forks_a_setsid_child():
    pid = os.fork()
    if pid == 0:
        os.setsid()
        with open({out!r}, "w") as f:
            f.write(f"{{os.getpid()}} {{os.environ['MEET_RUN_NONCE']}}")
        time.sleep(600)
        os._exit(0)
    while not os.path.exists({out!r}) or not open({out!r}).read().strip().count(" "):
        time.sleep(0.05)
'''


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a killed orphan nobody reaps (no init in a container) is a zombie, not alive
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        return not r.stdout.strip().startswith("Z")
    except OSError:
        return True


def test_no_process_outlives_a_meeting_and_the_control_shows_the_sweep_is_what_kills_it(world, monkeypatch):
    world.impl()
    out = world.tmp / "child.txt"
    world.lane({"tests/lane/test_child.py": _CHILD_TEST.format(out=str(out))})
    assert meet(world) == 0
    pid, nonce = out.read_text().split()
    # `meet()` only returns once `sweep()` itself has converged (or given up
    # after SWEEP_SECONDS and raised) — but SIGKILL does not make a process
    # vanish from `ps`/`kill(pid, 0)` the instant it is sent, it makes it a
    # zombie until its parent (here: launchd, once the run's subprocess tree
    # has exited) reaps it. That reap raced a hardcoded 5s deadline here and
    # lost once under CI load (this line, 2026-09-29); the bound below is tied
    # to the same constant `sweep()` itself is bound by, with margin, rather
    # than a second guess at how long "under load" can take.
    deadline = time.time() + mod.SWEEP_SECONDS + 10
    while _alive(int(pid)) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(int(pid)) and mod._nonce_pids(nonce) == []
    # the control: with the sweep disabled the same child is still there
    out.unlink()
    monkeypatch.setattr(mod, "SWEEP_ENABLED", False)
    assert meet(world) == 0
    pid, nonce = out.read_text().split()
    try:
        assert _alive(int(pid)) and mod._nonce_pids(nonce) == [int(pid)]
    finally:
        os.kill(int(pid), signal.SIGKILL)


_GROUP_CHILD_ENV_SCRUBBED_TEST = '''\
import os, time


def test_forks_a_child_that_stays_in_the_group_and_scrubs_its_nonce():
    pid = os.fork()
    if pid == 0:
        os.environ.pop("MEET_RUN_NONCE", None)   # evades the nonce sweep; never calls os.setsid()
        with open({out!r}, "w") as f:
            f.write(str(os.getpid()))
        time.sleep(600)
        os._exit(0)
    while not os.path.exists({out!r}):
        time.sleep(0.05)
'''


def test_the_recorded_pgid_kills_a_group_mate_the_nonce_sweep_cannot_see(world, monkeypatch):
    """#705 MEDIUM a (`bin/fleet-meet` comment 17911 item 4): a child that
    stays in the run's process group (it never calls `os.setsid()`) but
    scrubs `MEET_RUN_NONCE` from its own environment is invisible to the
    nonce sweep BY CONSTRUCTION. With the sweep disabled (a test-only seam,
    isolating this from that separate mechanism), only the process-group
    kill can catch it — and only if the pgid was recorded AT SPAWN. The old
    code queried `getpgid(proc.pid)` after `proc.wait()` had already reaped
    the normally-exited leader, which raises ESRCH and turns the whole call
    into a no-op; under that code this child would survive the meeting."""
    world.impl()
    out = world.tmp / "group_child.txt"
    world.lane({"tests/lane/test_group_child.py": _GROUP_CHILD_ENV_SCRUBBED_TEST.format(out=str(out))})
    monkeypatch.setattr(mod, "SWEEP_ENABLED", False)
    assert meet(world) == 0
    pid = int(out.read_text().strip())
    deadline = time.time() + 5
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    try:
        assert not _alive(pid)
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


# ============================================================================ fail closed
def test_a_missing_sandbox_exec_exits_1_writes_nothing_and_imports_nothing(world, monkeypatch):
    world.impl()
    marker = world.tmp / "marker"
    world.lane(marker_test(marker))
    monkeypatch.setattr(mod, "USE_PROFILE", True)
    monkeypatch.setattr(mod, "SANDBOX_EXEC", str(world.tmp / "no-such-sandbox-exec"))
    assert meet(world) == 1
    assert not marker.exists() and not doc_path(world).exists()


@pytest.mark.skipif(not os.access("/usr/bin/sandbox-exec", os.X_OK), reason="needs sandbox-exec")
def test_a_profile_that_will_not_start_exits_1(world, monkeypatch, tmp_path):
    world.impl()
    marker = world.tmp / "marker"
    world.lane(marker_test(marker))
    bad = tmp_path / "bad.sb"
    bad.write_text("(this is not a profile\n")
    monkeypatch.setattr(mod, "USE_PROFILE", True)
    monkeypatch.setattr(mod, "PROFILE_PATH", bad)
    assert meet(world) == 1
    assert not marker.exists() and not doc_path(world).exists()


def test_the_runner_env_is_exactly_the_documented_key_set():
    env = mod.make_env(Path("/s"), "/site", "n")
    assert sorted(env) == ["HOME", "LANG", "MEET_RUN_NONCE", "PATH", "PYTHONDONTWRITEBYTECODE",
                           "PYTHONPATH", "TMPDIR"]


# =========================================================================== the header
def _body(*lines):
    return "\n".join(lines) + "\n"


def _hdr(prompt=HEX64, extra=()):
    return _body(f"Tests-for: {PLAN}", "runner: sub-opus", f"sha: {'1' * 40}",
                 f"prompt sha256: {prompt}", "usage: -", *extra, "", "gaps:")


# eunomia#653's body, VERBATIM from the forge (fetched via the API, 2026-09-29):
# `Tests-for:`, ONE blank line, then the four field lines, then the blank line
# that ends the block. That is not today's `build_pr_body` layout (no blank
# line after `Tests-for:`) -- it is the layout #653 actually has, dispatched
# before #677 amended plan 0102 D4 to say a body may have either one.
PR653_BODY = "Tests-for: 0093-a-cycle-reads-the-forge-once\n\nrunner: sub-opus\nsha: 4e256e2b33e97ca3a932d05ee098553ded49d804\nprompt sha256: b83e870c1959a9b2a3cb60ac4746c17bf712951570c0f9f50a493ae5664e19fe\nusage: {'input_tokens': 2, 'cache_creation_input_tokens': 63823, 'cache_read_input_tokens': 531, 'output_tokens': 51341, 'output_tokens_details': {'thinking_tokens': 35884}, 'server_tool_use': {'web_search_requests': 0, 'web_fetch_requests': 0}, 'service_tier': 'standard', 'cache_creation': {'ephemeral_1h_input_tokens': 63823, 'ephemeral_5m_input_tokens': 0}, 'inference_geo': 'not_available', 'iterations': [{'input_tokens': 2, 'output_tokens': 51341, 'cache_read_input_tokens': 531, 'cache_creation_input_tokens': 63823, 'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 63823}, 'type': 'message'}], 'speed': 'standard'}\n\ngaps:\n```\n> Declared behaviour for bin/fleet-watch was not updated for D3. It says only 'a per-cycle summary' on stdout. It does not declare the duration field, the page-count field, or the skipped-leases clause. The tests match the duration and page count with loose regexes, and match the exact skipped-leases wording from plan D3.\n> Contradiction: declared behaviour says bin/mopsus 'Never touches the forge'. Plan 0093 D1 and section 3 say mopsus's _marked_pr_check() imports marked_pr and reads the forge outside a cycle. One of the two is wrong.\n> 'Outside a cycle, marked_pr() reads the forge on every call (two calls, two reads)' has no blackbox entry point. marked_pr is internal, and mopsus's use of it is not reachable from any declared CLI shape. Not tested.\n> Resident mode is not tested. That covers the memo resetting between cycles in one process, and the memo ending when a cycle raises. The contract gives no way to run exactly N resident cycles without the singleton service lease. Only reads across separate one-shot processes are tested. The one-shot exception path is itself UNDECIDED in declared behaviour, and memo teardown cannot be observed from outside.\n> Several ways the watcher reaches main are undeclared: how it finds and reads plan files (contents API, git trees, raw, or a local clone), and how its protection check works (forge endpoint vs FLEET_BROKER_SOCKET, the FLEET_DIRECT_TOKEN_CMD and FLEET_OPERATOR_UID semantics). The stub serves several shapes. FLEET_BROKER_SOCKET points at a nonexistent socket so no test can reach a real broker. If that makes the cycle refuse, the tests skip rather than pass.\n> Several config value formats are undeclared: FLEET_WATCH_REPOS (separator), FLEET_TOKEN_CMD (path vs shell string), FLEET_FORGEJO_URL (with or without /api/v1), and FLEET_IMPLEMENTER_UIDS. The readiness-probe protocol for FLEET_ORCHESTRATOR is also undeclared. The tests guess and gate on a control run.\n> It is undeclared whether a repo must also be enrolled with 'fleet-repo enable --dispatch' before the watcher scans it, and where the default enrolment file lives. The tests do not enrol, to avoid writing to an unknown, possibly real, config path.\n> It is undeclared what makes a lease an orphaned dispatch lease, and how the sweep maps a lease to a plan id (resource globs, note, branch, granted_by). Liveness recording is also undeclared. The tests build a paths lease with fleet-claim, age the holder's files, and use fleet-status --json as the orphan oracle. A draft-plan control must show a forge read before the done/abandoned assertions count.\n> The shape of --dry-run per-plan decision output is undeclared. Dedupe answers are compared only differentially (alone vs shared cycle, before vs after), never against literal text.\n> Today's meaning of a closed-only marked PR for dispatch (skip, or re-dispatch) is not declared. The tests only check that the memoised answer equals the first-read answer.\n> The plan says the memo never retries a bad page within the cycle. The contract does not say whether the forge client retries at the transport level. The tests assert that page reads do not grow with the number of plans, not that each page is read exactly once.\n> PAGE_CAP = 20 comes from the plan's handoff note, not from the contract. It is also undeclared whether the scan reads page PAGE_CAP+1 to detect truncation. The tests assert at most 20 pages.\n> D3's 'number of forge list pages read' is ambiguous: it may count only pull-request listing pages, or also plan-directory listings. The tests assert the reported count is at least the pull-listing pages observed.\n> The page (FLEET_NTFY_URL) body and header format is undeclared. 'No page for a finished lease' is checked by the plan id and lease id being absent from every page body and header.\n> D2 memoises plan_files() per repo per cycle. This is not asserted, because the plan-file fetch mechanism is undeclared.\n> Declared behaviour makes every one-shot early refusal exit 0, and the UNDECIDED note says 'could not scan' cannot be told from 'scanned and dispatched nothing' by exit code. So the tests cannot use the exit code to detect that setup failed, and they gate on observed forge reads instead.\n> Handoff risk, not tested: past 1,000 PRs, PAGE_CAP makes every eunomia plan unreadable, so nothing in eunomia dispatches. This plan leaves that open, and it needs a decision.\n```\n\nnotes:\nOne file, tests/test_blackbox_0093_forge_reads.py. It runs bin/fleet-watch --dry-run as a subprocess against an in-process, read-only stub forge. The stub records every request and every FLEET_NTFY_URL page, and serves pull-request listings (paged, with induced 500s and endless pages) plus several plan-file shapes. Every host-facing setting points into a scratch directory: HOME, EUNOMIA_FLEET_DIR, FLEET_DB, the token command, the orchestrator, launchctl/systemctl, the broker socket, and ntfy. No run touches a real fleet area, broker, supervisor, or push channel, and nothing runs outside --dry-run.\n\nWhat the tests cover:\n- one listing pass per repo per cycle, whether driven by ready plans, orphaned leases, or both (the section 4 3+4 case);\n- a failed page and the page cap both answer 'unreadable', and are read once regardless of plan count;\n- answers in a shared cycle equal a first read, for the open-vs-closed, near-miss, and foreign-author cases, plus today's line-anchor, author, and open-preferred rules;\n- a fresh read on the next cycle;\n- the D3 summary fields;\n- D2: done/abandoned leases cost no read and no page, keep their state, and are counted in the summary; an unreadable status is swept as today;\n- the D4 doc paragraph;\n- the declared CLI surface: --help lists the three flags; an unknown flag exits 2;\n- the declared 'forge calls are reads only' invariant.\n\nWhere the contract does not say how to reach a step (plan discovery, protection check, lease-to-plan mapping, orphan detection), each test runs a control first and skips with a stated reason rather than passing vacuously. Run with -rs to see any skips; a skip means the harness needs the contract detail named in gaps, not that the feature passed.\n"


def _old_rule_header_fields(body):
    """The #659 rule #677's revision replaced: the header block is every line
    BEFORE the body's first blank line, full stop -- no allowance for a blank
    line straight after `Tests-for:`. Kept here only as the control fixture:
    applying it to PR653_BODY must still read `missing`, which is what proves
    the real fix (`mod.header_fields`) is doing something PR653_BODY needs."""
    out = {}
    for line in (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            break
        m = mod._HEADER_LINE.match(line)
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


def test_653s_body_reads_runner_sha_and_prompt_hash_whole():
    """#677's D4: the layout #653 actually has must yield the same fields as
    today's layout. `sha` and `prompt sha256` are what the pre-fix code lost --
    header_fields() stopped at the FIRST blank line, which in #653's body is
    the one right after `Tests-for:`, before any of the four field lines."""
    fields = mod.header_fields(PR653_BODY)
    assert fields["Tests-for"] == ["0093-a-cycle-reads-the-forge-once"]
    assert fields["runner"] == ["sub-opus"]
    assert fields["sha"] == ["4e256e2b33e97ca3a932d05ee098553ded49d804"]
    assert fields["prompt sha256"] == ["b83e870c1959a9b2a3cb60ac4746c17bf712951570c0f9f50a493ae5664e19fe"]
    assert len(fields["usage"]) == 1                     # present; a repr, never parsed

    prompt_fields, dispatch = mod.prompt_hash(REPO, "0093-a-cycle-reads-the-forge-once",
                                              "sub-opus", 653, PR653_BODY)
    assert dispatch == "4e256e2b33e97ca3a932d05ee098553ded49d804"
    assert prompt_fields["prompt_sha256"] == "b83e870c1959a9b2a3cb60ac4746c17bf712951570c0f9f50a493ae5664e19fe"
    # no lane_run row for this fixture (it ran before plan 0100), so it is
    # UNVERIFIED for that reason alone -- never "missing", which is the bug
    assert prompt_fields["prompt_sha256_verified"] is False
    assert prompt_fields["prompt_sha256_reason"] == "no lane_run row: the hash comes from the PR body alone"


def test_the_control_the_old_first_blank_line_rule_loses_653s_fields():
    """The discriminating control: the OLD rule (stop at the first blank line,
    no exception) reads #653's body and finds no `runner`, `sha` or
    `prompt sha256` at all -- they are all written after the blank line right
    after `Tests-for:`, which is exactly the line the old rule broke on."""
    old = _old_rule_header_fields(PR653_BODY)
    assert old.get("Tests-for") == ["0093-a-cycle-reads-the-forge-once"]
    for missing in ("runner", "sha", "prompt sha256", "usage"):
        assert missing not in old, (missing, old)


def test_653_run_through_a_lane_reads_its_sha_and_hash_end_to_end(world):
    """The fixture through the full pipeline, not just the helper functions:
    a lane PR carrying #653's exact body, on #653's own plan id (its
    `Tests-for:` line names it, so the branch and the run must match), reads
    the sha and prompt hash whole in the findings document -- not `null`."""
    plan = "0093-a-cycle-reads-the-forge-once"
    world.impl(body=f"Plan: {plan}\n")
    n, sha = world.lane({"tests/lane/test_x.py": "def test_x():\n    pass\n"},
                        plan=plan, body=PR653_BODY)
    assert mod.main([REPO, plan, "--impl", str(world.impl_pr)]) == 0
    path = mod.findings_path(plan, world.impl_sha)
    schema = json.loads((ROOT / "config" / "meeting-findings.schema.json").read_text())
    doc = json.loads(path.read_text())
    assert mod.lane.validate_schema(doc, schema) == []
    ln = doc["lanes"][0]
    assert ln["dispatch_sha"] == "4e256e2b33e97ca3a932d05ee098553ded49d804"
    assert ln["prompt_sha256"] == "b83e870c1959a9b2a3cb60ac4746c17bf712951570c0f9f50a493ae5664e19fe"


def _forge_edit_653(insert_after, extra_line=None, replace=None):
    """#653's body as a forge edit would leave it: insert a line right after
    `insert_after` inside the header block, or replace a line outright."""
    lines = PR653_BODY.split("\n")
    if replace is not None:
        lines = [replace if l.startswith(replace.split(":")[0] + ":") else l for l in lines]
    if extra_line is not None:
        i = lines.index(insert_after)
        lines.insert(i + 1, extra_line)
    return "\n".join(lines)


def test_653_with_a_repeated_prompt_hash_inside_the_block_is_unverified():
    """A forge edit into the header block (D4's 'inside the block, by a forge
    edit' case), on #653's own layout: a second `prompt sha256:` line gives
    `prompt_sha256: null`, unverified, `repeated header field`, and the lane
    still runs and the document still validates."""
    body = _forge_edit_653(
        "prompt sha256: b83e870c1959a9b2a3cb60ac4746c17bf712951570c0f9f50a493ae5664e19fe",
        extra_line=f"prompt sha256: {'c' * 64}")
    fields, dispatch = mod.prompt_hash(REPO, "0093-a-cycle-reads-the-forge-once", "sub-opus", 653, body)
    assert fields["prompt_sha256"] is None and fields["prompt_sha256_verified"] is False
    assert "repeated header field 'prompt sha256'" in fields["prompt_sha256_reason"]
    assert dispatch == "4e256e2b33e97ca3a932d05ee098553ded49d804"       # sha is untouched


def test_653_with_sha_removed_from_the_block_gives_missing_dispatch_sha():
    body = "\n".join(l for l in PR653_BODY.split("\n")
                     if not l.startswith("sha: 4e256e2b"))
    fields, dispatch = mod.prompt_hash(REPO, "0093-a-cycle-reads-the-forge-once", "sub-opus", 653, body)
    assert dispatch is None
    assert "missing header field 'sha'" in fields["prompt_sha256_reason"]


def test_653_with_a_malformed_prompt_hash_is_unverified():
    body = _forge_edit_653(None, replace="prompt sha256: [redacted:long-hex]")
    fields, dispatch = mod.prompt_hash(REPO, "0093-a-cycle-reads-the-forge-once", "sub-opus", 653, body)
    assert fields["prompt_sha256"] is None and fields["prompt_sha256_verified"] is False
    assert "not 64 hex" in fields["prompt_sha256_reason"]
    assert dispatch == "4e256e2b33e97ca3a932d05ee098553ded49d804"


def test_a_repeated_header_field_makes_the_lane_unverified():
    fields, _ = mod.prompt_hash(REPO, PLAN, "sub-opus", 5, _hdr(extra=(f"prompt sha256: {'c' * 64}",)))
    assert fields["prompt_sha256_verified"] is False
    assert "repeated header field 'prompt sha256'" in fields["prompt_sha256_reason"]


def test_a_forged_line_after_the_blank_line_leaves_the_header_value_in_force():
    body = _hdr() + _body("> ok", f"prompt sha256: {'c' * 64}", f"sha: {'d' * 40}")
    fields, dispatch = mod.prompt_hash(REPO, PLAN, "sub-opus", 5, body)
    assert fields["prompt_sha256"] == HEX64 and dispatch == "1" * 40
    assert "repeated" not in (fields["prompt_sha256_reason"] or "")


def test_todays_unquoted_notes_cannot_reach_the_header_and_the_new_body_quotes_them():
    forged = f"prompt sha256: {'c' * 64}"
    body = mod.lane.build_pr_body(PLAN, "sub-opus", "1" * 40, HEX64, None,
                                  [f"x\n{forged}"], f"n\n{forged}")
    assert len([l for l in body.split("\n") if l.startswith("prompt sha256:")]) == 1
    fields, _ = mod.prompt_hash(REPO, PLAN, "sub-opus", 5, body)
    assert fields["prompt_sha256"] == HEX64


# ======================================================================= comparability
def _seed_lane_run(world, runner, pr, prompt):
    db = Path(os.environ["EUNOMIA_FLEET_DIR"]) / "fleet.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(mod.lane.SCHEMA)
    con.execute("INSERT INTO lane_run (run_id, repo, plan, runner, started_at, spawned, outcome, "
                "prompt_sha256, pr) VALUES (?,?,?,?,?,1,'pr-opened',?,?)",
                (f"r{pr}", REPO, PLAN, runner, "2026-09-29T00:00:00Z", prompt, pr))
    con.commit()
    con.close()


def _two_lanes(world, hashes, seed=True):
    world.impl()
    prs = []
    for runner, h in zip(("sub-opus", "sub-codex"), hashes):
        n, _ = world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"}, runner=runner, prompt=h)
        if seed:
            _seed_lane_run(world, runner, n, h)
        prs.append(n)
    assert meet(world) == 0
    return load_doc(world)


def test_two_lanes_with_equal_verified_hashes_are_comparable(world):
    doc = _two_lanes(world, [HEX64, HEX64])
    assert doc["comparable"] is True and doc["comparability"] is None
    assert all(l["prompt_sha256_verified"] for l in doc["lanes"])


def test_equal_body_hashes_with_no_lane_run_row_are_not_comparable(world):
    doc = _two_lanes(world, [HEX64, HEX64], seed=False)
    assert doc["comparable"] is False
    assert not any(l["prompt_sha256_verified"] for l in doc["lanes"])
    assert all(l["prompt_sha256_source"] == "pr-body" for l in doc["lanes"])


def test_different_hashes_are_not_comparable_and_the_sentence_names_both(world):
    other = "cd" * 32
    doc = _two_lanes(world, [HEX64, other])
    assert doc["comparable"] is False
    assert HEX64 in doc["comparability"] and other in doc["comparability"]
    assert "byte-identical" in doc["comparability"]
    assert len(doc["lanes"]) == 2          # still run


def test_a_body_and_a_row_that_disagree_record_both_and_still_run(world):
    world.impl()
    n, _ = world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    _seed_lane_run(world, "sub-opus", n, "ef" * 32)
    assert meet(world) == 0
    (ln,) = load_doc(world)["lanes"]
    assert ln["prompt_sha256_verified"] is False and ln["prompt_sha256"] == HEX64
    assert ln["prompt_sha256_lane_run"] == "ef" * 32 and ln["counts"]["pass"] == 1


def test_one_lane_is_null_comparable(world):
    world.impl()
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    assert meet(world) == 0
    doc = load_doc(world)
    assert doc["comparable"] is None and doc["comparability"] == "one lane: nothing to compare"


# ============================================================================= document
def test_reasons_are_redacted_but_structured_shas_are_whole(world):
    world.impl()
    tok = "0123456789abcdef" * 2 + "01234567"
    n, sha = world.lane({"tests/lane/test_a.py": f"def test_a():\n    assert False, 'token {tok}'\n"})
    assert meet(world) == 0
    doc = load_doc(world)
    (t,) = doc["lanes"][0]["tests"]
    assert tok not in t["reason"] and t["reason_redacted"]
    assert doc["impl"]["head_sha"] == world.impl_sha and len(doc["impl"]["head_sha"]) == 40
    assert doc["lanes"][0]["head_sha"] == sha
    assert doc["lanes"][0]["prompt_sha256"] == HEX64
    assert len(t["reason"]) <= mod.REASON_MAX


def test_a_reason_is_bounded():
    text, _ = mod.clean_reason("x" * 10000, tail=True)
    assert len(text) <= mod.REASON_MAX


def test_the_confinement_record_matches_the_profile_not_a_hoped_for_claim(world):
    """#677 measured that SBPL cannot express a loopback-only bind on this
    host (`localhost` matches every local interface), so the document must
    record what the profile actually does. This ties the record to the
    literal rules in `config/meeting-runner.sb`, so a profile edit that
    changes what is denied has to change this assertion too."""
    world.impl()
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    assert meet(world) == 0
    doc = load_doc(world)
    assert doc["confinement"]["network"] == {
        "outbound": "this-host-only", "bind": "any-local-address", "denied_ports": [8200]}
    assert "loopback-only" not in json.dumps(doc)
    profile = mod.PROFILE_PATH.read_text()
    assert '(deny network-bind (local tcp "localhost:8200"))' in profile
    assert '(deny network-inbound (local tcp "localhost:8200"))' in profile
    assert '(deny network-outbound (remote tcp "localhost:8200"))' in profile


def test_a_rerun_keeps_the_old_document_beside_the_new_one(world):
    world.impl()
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    assert meet(world) == 0
    first = json.loads(doc_path(world).read_text())["generated_at"]
    assert meet(world) == 0
    archive = doc_path(world).with_name(f"findings.{first}.json")
    assert archive.exists() and json.loads(archive.read_text())["generated_at"] == first
    assert doc_path(world).exists()


def test_a_document_that_fails_its_schema_is_never_written(tmp_path):
    with pytest.raises(mod.HostError):
        mod.write_document({"schema": "meeting-findings/1"}, tmp_path / "findings.json")
    assert not (tmp_path / "findings.json").exists()


def test_outcome_enums_and_patterns_are_enforced_by_the_schema():
    schema = json.loads((ROOT / "config" / "meeting-findings.schema.json").read_text())
    test = schema["properties"]["lanes"]["items"]["properties"]["tests"]["items"]["properties"]
    assert test["outcome"]["enum"] == ["pass", "fail", "error", "skip"]
    assert test["pytest_outcome"]["enum"] == ["passed", "failed", "error", "skipped", "xfailed", "xpassed"]
    lane_props = schema["properties"]["lanes"]["items"]["properties"]
    assert lane_props["head_sha"]["pattern"] == "^[0-9a-f]{40}$"
    assert lane_props["prompt_sha256"]["pattern"] == "^[0-9a-f]{64}$"
    net = schema["properties"]["confinement"]["properties"]["network"]
    assert net["properties"]["outbound"]["enum"] == ["this-host-only"]
    assert net["properties"]["bind"]["enum"] == ["any-local-address"]
    assert net["properties"]["denied_ports"]["enum"] == [[8200]]


def test_the_schema_refuses_the_old_loopback_only_string():
    """A regression guard for the false confinement claim: a document that
    still writes the old `"network": "loopback-only"` string must fail the
    schema, not validate quietly beside the real record."""
    schema = json.loads((ROOT / "config" / "meeting-findings.schema.json").read_text())
    net_schema = schema["properties"]["confinement"]["properties"]["network"]
    assert mod.lane.validate_schema("loopback-only", net_schema) != []
    good = {"outbound": "this-host-only", "bind": "any-local-address", "denied_ports": [8200]}
    assert mod.lane.validate_schema(good, net_schema) == []


# ============================================================================== dry run
def test_a_dry_run_lists_files_and_verdicts_whole_and_runs_nothing(world, capsys):
    world.impl()
    marker = world.tmp / "marker"
    n, sha = world.lane(marker_test(marker))
    n2, sha2 = world.lane(marker_test(marker), runner="sub-codex", approve=None)
    assert meet(world, dry=True) == 0
    out = capsys.readouterr().out
    assert world.impl_sha in out and sha in out and sha2 in out
    assert f"lane {n} at {sha}" in out and f"lane {n2} at {sha2}: NOT RUN" in out
    assert "tests/lane/test_m.py" in out and "comparable:" in out
    assert not marker.exists() and not doc_path(world).exists()


# ===================================================================== forge is read-only
def test_the_meeting_only_ever_reads_the_forge(world):
    world.impl()
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    assert meet(world) == 0
    assert {m for m, _ in world.httpd.requests} == {"GET"}


# ============================================================ confinement, LIVE on opshost
# Each probe is a lane test that reports what it could do as `PROBE <name> <result>`
# (through pytest.fail, so the words arrive in the findings document's `reason`).
# The whole file is run through fleet-meet's own runner twice: confined, and again
# with the profile removed — the test-only seam, never a flag. A probe counts only
# if it is DENIED confined AND succeeds as the control.
_PROBES = '''\
import http.server, json, os, socket, subprocess, threading, time, urllib.request
import pytest

P = json.loads({params!r})


def report(name, fn):
    try:
        got = fn()
    except Exception as e:
        got = "denied:" + type(e).__name__
    pytest.fail("PROBE %s %s" % (name, got))


def tcp(host, port):
    socket.create_connection((host, port), timeout=3).close()
    return "allowed"


def unix(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    try:
        s.connect(path)
    finally:
        s.close()
    return "allowed"


def write(path):
    with open(path, "w") as f:
        f.write("x")
    return "allowed"


def test_lan_tcp():
    report("lan_tcp", lambda: tcp(P["lan_ip"], P["lan_port"]))


def test_read_home():
    report("read_home", lambda: "allowed" if open(P["home_file"]).read() == P["nonce"] else "denied:wrong")


def test_home_unix():
    report("home_unix", lambda: unix(P["home_sock"]))


def test_ssh_auth_sock():
    report("ssh_auth_sock", lambda: unix(P["agent_sock"]))


def test_keychain():
    def f():
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", "acct", "-s", "svc",
                            "-w", P["keychain"]], capture_output=True, text=True, timeout=30)
        return "allowed" if P["secret"] in r.stdout else "denied:rc%d" % r.returncode
    report("keychain", f)


def test_write_homebrew():
    report("write_homebrew", lambda: write(P["homebrew_dir"] + "/probe"))


def test_write_shared():
    report("write_shared", lambda: write(P["shared_dir"] + "/probe"))


def test_write_real_tmp():
    report("write_real_tmp", lambda: write(P["real_tmp"] + "/" + P["nonce"]))


def test_read_shared():
    report("read_shared", lambda: "allowed" if open(P["shared_dir"] + "/readme").read() == P["nonce"] else "denied:wrong")


def test_job_creation():
    def f():
        subprocess.run(["/bin/launchctl", "submit", "-l", P["label"], "--", "/usr/bin/touch", P["job_file"]],
                       capture_output=True, timeout=30)
        time.sleep(2)
        return "allowed" if os.path.exists(P["job_file"]) else "denied:no-file"
    report("job_creation", f)


def test_appleevent():
    def f():
        subprocess.run(["/usr/bin/osascript", "-e", P["script"]], capture_output=True, timeout=30)
        return "allowed" if os.path.exists(P["ae_file"]) else "denied:no-file"
    report("appleevent", f)


def test_lsopen():
    def f():
        subprocess.run(["/usr/bin/open", "-g", P["command_file"]], capture_output=True, timeout=30)
        for _ in range(10):
            if os.path.exists(P["open_file"]):
                return "allowed"
            time.sleep(0.5)
        return "denied:no-file"
    report("lsopen", f)


def test_port_8200():
    report("port_8200", lambda: tcp("127.0.0.1", 8200))


def test_loopback_round_trip():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        a = urllib.request.urlopen("http://127.0.0.1:%d/" % srv.server_address[1], timeout=5).read()
        b = urllib.request.urlopen("http://localhost:%d/" % srv.server_address[1], timeout=5).read()
    finally:
        srv.shutdown()
    assert a == b == b"ok"


def test_env_keys():
    # pytest sets PYTEST_* itself, and macOS adds __CF_USER_TEXT_ENCODING to a child
    keys = [k for k in os.environ if not k.startswith("PYTEST_") and k != "__CF_USER_TEXT_ENCODING"]
    pytest.fail("PROBE env_keys " + json.dumps(sorted(keys)))
'''


def _remote_host():
    """A reachable NON-loopback TCP endpoint (the forge, by address so that DNS is
    not what stops the confined run). Connecting to this host's own LAN address
    does not do: the profile's `localhost` matches every local interface."""
    try:
        ip = socket.gethostbyname("forge.example")
        socket.create_connection((ip, 3001), timeout=3).close()
        return ip, 3001
    except OSError:
        return None


def _listen_tcp(host, port):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    threading.Thread(target=lambda: _accept_forever(srv), daemon=True).start()
    return srv


def _listen_unix(path):
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(8)
    threading.Thread(target=lambda: _accept_forever(srv), daemon=True).start()
    return srv


def _accept_forever(srv):
    while True:
        try:
            srv.accept()[0].close()
        except OSError:
            return


def _probe_results(doc):
    out = {}
    for t in doc["lanes"][0]["tests"]:
        m = re.search(r"^E\s+Failed: PROBE (\S+) (.*)$", t["reason"] or "", re.M)
        if m:
            out[m.group(1)] = m.group(2).strip()
        elif t["id"].endswith("test_loopback_round_trip"):
            out["loopback_round_trip"] = "allowed" if t["outcome"] == "pass" else t["reason"]
    return out


@live
def test_confinement_probes_are_denied_confined_and_succeed_as_the_control(world, tmp_path, monkeypatch):
    import pwd
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    nonce = os.urandom(6).hex()
    cleanup = []
    try:
        params = {"nonce": nonce}
        # a non-loopback endpoint, a nonce file and a unix socket in the real ~,
        # a stand-in agent socket outside ~, a throwaway keychain outside ~
        remote = _remote_host()
        if remote is None:
            pytest.skip("no reachable non-loopback TCP endpoint (forge.example:3000) from here")
        params.update(lan_ip=remote[0], lan_port=remote[1])
        home_file = real_home / f".fleet-meet-probe-{nonce}"
        home_file.write_text(nonce)
        cleanup.append(home_file.unlink)
        params["home_file"] = str(home_file)
        home_sock = real_home / f".fmp-{nonce}.sock"
        hs = _listen_unix(str(home_sock))
        cleanup += [hs.close, home_sock.unlink]
        params["home_sock"] = str(home_sock)
        agent_dir = Path(os.path.realpath("/tmp")) / f"fmp-agent-{nonce}"
        agent_dir.mkdir()
        agent_sock = agent_dir / "agent"
        ags = _listen_unix(str(agent_sock))
        cleanup += [lambda: shutil.rmtree(agent_dir, ignore_errors=True), ags.close]
        params["agent_sock"] = str(agent_sock)
        kc = f"/tmp/fmp-{nonce}.keychain-db"
        secret = "SECRET" + nonce
        subprocess.run(["security", "create-keychain", "-p", "pw", kc], check=True, capture_output=True)
        cleanup.append(lambda: subprocess.run(["security", "delete-keychain", kc], capture_output=True))
        subprocess.run(["security", "add-generic-password", "-a", "acct", "-s", "svc", "-w", secret, kc],
                       check=True, capture_output=True)
        subprocess.run(["security", "unlock-keychain", "-p", "pw", kc], check=True, capture_output=True)
        params.update(keychain=kc, secret=secret)
        # write escapes: probe dirs made for the test, and the operator's real temp dir
        for key, base in (("homebrew_dir", "/opt/homebrew"), ("shared_dir", "/Users/Shared")):
            d = Path(base) / f"fleet-meet-probe-{nonce}"
            try:
                d.mkdir()
            except OSError:
                pytest.skip(f"cannot make a probe directory under {base}")
            cleanup.append(lambda d=d: shutil.rmtree(d, ignore_errors=True))
            params[key] = str(d)
        (Path(params["shared_dir"]) / "readme").write_text(nonce)
        real_tmp = Path(os.path.realpath(subprocess.run(
            ["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()))
        params["real_tmp"] = str(real_tmp)
        cleanup.append(lambda: (real_tmp / nonce).unlink() if (real_tmp / nonce).exists() else None)
        # job creation, Apple Event and lsopen escapes, each with a side effect outside the sandbox
        params.update(label=f"org.eunomia.fleet-meet-probe.{nonce}", job_file=f"/tmp/fmp-job-{nonce}")
        cleanup += [lambda: subprocess.run(["launchctl", "remove", params["label"]], capture_output=True),
                    lambda: Path(params["job_file"]).unlink() if Path(params["job_file"]).exists() else None]
        ae = real_home / f"fmp-ae-{nonce}.txt"
        params.update(ae_file=str(ae), script=(
            'tell application "System Events" to make new file at end of folder "Users:'
            f'{real_home.name}" with properties {{name:"{ae.name}"}}'))
        cleanup.append(lambda: ae.unlink() if ae.exists() else None)
        cmd = Path(f"/tmp/fmp-{nonce}.command")
        cmd.write_text(f"#!/bin/sh\ntouch /tmp/fmp-open-{nonce}\n")
        cmd.chmod(0o755)
        params.update(command_file=str(cmd), open_file=f"/tmp/fmp-open-{nonce}")
        terminal_was_running = subprocess.run(["pgrep", "-x", "Terminal"], capture_output=True).returncode == 0
        cleanup += [cmd.unlink, lambda: Path(params["open_file"]).unlink() if Path(params["open_file"]).exists() else None]
        # the 8200 listener, bound OUTSIDE the sandbox: nothing listens there on opshost today
        try:
            l8200 = _listen_tcp("127.0.0.1", 8200)
            cleanup.append(l8200.close)
        except OSError:
            pytest.skip("127.0.0.1:8200 is in use here; the probe would not discriminate")

        world.impl()
        world.lane({"tests/lane/test_probes.py": _PROBES.format(params=json.dumps(params))})
        monkeypatch.setattr(mod, "USE_PROFILE", True)
        assert meet(world) == 0
        confined = _probe_results(load_doc(world))
        for f in (Path(params["job_file"]), ae, Path(params["open_file"]), real_tmp / nonce):
            if f.exists():
                f.unlink()
        monkeypatch.setattr(mod, "USE_PROFILE", False)         # the control: same runner, no profile
        assert meet(world) == 0
        control = _probe_results(load_doc(world))
        if not terminal_was_running:
            # the control's `open` started Terminal to run the .command file; a shell
            # keeps it from quitting politely, so it is ended — only if we started it
            subprocess.run(["pkill", "-x", "Terminal"], capture_output=True)
    finally:
        for fn in reversed(cleanup):
            try:
                fn()
            except Exception:
                pass

    expected_env = sorted(["HOME", "LANG", "MEET_RUN_NONCE", "PATH", "PYTHONDONTWRITEBYTECODE",
                           "PYTHONPATH", "TMPDIR"])
    assert json.loads(confined.pop("env_keys")) == expected_env
    control.pop("env_keys")
    # allowed both ways: reads outside ~ are not denied (measured, and said so in the doc)
    for name in ("read_shared", "loopback_round_trip"):
        assert confined[name] == "allowed" == control[name], (name, confined[name], control[name])
    for name in ("lan_tcp", "read_home", "home_unix", "ssh_auth_sock", "keychain", "write_homebrew",
                 "write_shared", "write_real_tmp", "job_creation", "appleevent", "lsopen", "port_8200"):
        assert confined[name].startswith("denied"), (name, confined[name])
        assert control[name] == "allowed", (name, "the control must succeed", control[name])


# ============================================ live: the bind and signal gaps (#705, item 3/5)
_BIND_8200_PROBE = '''\
import socket
import pytest


def test_bind_8200():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", 8200))
        s.listen(1)
        result = "allowed"
    except PermissionError:
        result = "denied:PermissionError"
    finally:
        s.close()
    pytest.fail("PROBE bind_8200 " + result)
'''


@live
def test_binding_127_0_0_1_8200_is_denied_confined_and_succeeds_unconfined(world, monkeypatch):
    """Plan 0102 D3, amended by #677, and #686 comment 17911 item 1: the
    profile denied CONNECTING to 8200 but not BINDING it. `lsof` first proves
    nothing already listens there on this host, so the probe discriminates;
    if something does, this skips with that reason rather than binding over a
    real listener, as the DoD instructs."""
    r = subprocess.run(["lsof", "-nP", "-iTCP:8200", "-sTCP:LISTEN"], capture_output=True, text=True)
    if r.stdout.strip():
        pytest.skip(f"something already listens on 8200 here: {r.stdout.strip()!r}")
    world.impl()
    world.lane({"tests/lane/test_bind8200.py": _BIND_8200_PROBE})
    monkeypatch.setattr(mod, "USE_PROFILE", True)
    assert meet(world) == 0
    confined = _probe_results(load_doc(world))
    monkeypatch.setattr(mod, "USE_PROFILE", False)          # the control: same runner, no profile
    assert meet(world) == 0
    control = _probe_results(load_doc(world))
    assert confined["bind_8200"].startswith("denied"), confined["bind_8200"]
    assert control["bind_8200"] == "allowed", control["bind_8200"]


_SIGNAL_PROBE = '''\
import json, os
import pytest

PID = json.loads({params!r})["pid"]


def test_signal_outside():
    try:
        os.kill(PID, 0)
        result = "allowed"
    except PermissionError:
        result = "denied:PermissionError"
    except ProcessLookupError:
        result = "denied:no-such-process"
    pytest.fail("PROBE signal_outside " + result)
'''


@live
def test_a_confined_test_cannot_signal_a_sleeper_started_outside_the_sandbox(world, monkeypatch):
    """#705 MEDIUM b: `meeting-runner.sb` left `signal` at its `(allow default)`,
    so confined code could kill any same-uid process — fleet-watch, the
    orchestrator, fleet-meet itself. The sleeper here stands in for one of
    those: started OUTSIDE any sandbox. `os.kill(pid, 0)` sends no real signal
    but is checked against the same policy, so it discriminates without
    disturbing the sleeper. Denied confined; the control (no profile) succeeds,
    which is why this needs a live measurement and not just a profile diff."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.3)
        world.impl()
        params = json.dumps({"pid": sleeper.pid})
        world.lane({"tests/lane/test_signal.py": _SIGNAL_PROBE.format(params=params)})
        monkeypatch.setattr(mod, "USE_PROFILE", True)
        assert meet(world) == 0
        confined = _probe_results(load_doc(world))
        monkeypatch.setattr(mod, "USE_PROFILE", False)      # the control: same runner, no profile
        assert meet(world) == 0
        control = _probe_results(load_doc(world))
    finally:
        sleeper.terminate()
        try:
            sleeper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sleeper.kill()
    assert confined["signal_outside"].startswith("denied"), confined["signal_outside"]
    assert control["signal_outside"] == "allowed", control["signal_outside"]


_PARENT_PROBE = '''\
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_collected_and_ran():
    assert (ROOT / "tests").is_dir()


def test_the_parent_can_be_statted_and_nothing_more():
    os.stat(ROOT.parent)
    with pytest.raises(PermissionError):
        os.listdir(ROOT.parent)
    with pytest.raises(PermissionError):
        os.stat(ROOT.parent / "sibling.txt")
    with pytest.raises(PermissionError):
        open(ROOT.parent / "sibling.txt").read()
    with pytest.raises(PermissionError):
        os.listdir(ROOT.parent.parent)      # plan 0135: its metadata is admitted now, its listing is not
'''


@live
def test_a_lane_is_collected_when_the_work_root_is_under_the_real_home(world, monkeypatch):
    """The first real meeting (plan 0130, 2026-10-13) collected nothing:
    pytest stats the directory above its root, the profile denied every read
    under the real home outside the worktree, and the run ended `PermissionError
    … '<worktree parent>'`. Every other live test here keeps its work root
    under the system temp directory, where that denial does not reach. This one
    puts it where the fleet's is. The profile admits that one directory's
    metadata and nothing else of it; the control is the same lane's own
    assertions that a listing, a sibling and the grandparent are still denied."""
    import pwd
    real_home = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
    base = Path(tempfile.mkdtemp(dir=str(real_home), prefix=".fleet-meet-test-"))
    try:
        monkeypatch.setenv("FLEET_WORK_ROOT", str(base / "work"))
        world.impl()
        world.lane({"tests/lane/test_parent.py": _PARENT_PROBE})
        monkeypatch.setattr(mod, "USE_PROFILE", True)
        real_add = mod.lib.add_worktree

        def add_with_sibling(*a, **k):
            wt = real_add(*a, **k)
            (Path(os.path.realpath(wt)).parent / "sibling.txt").write_text("not for the lane")
            return wt
        monkeypatch.setattr(mod.lib, "add_worktree", add_with_sibling)
        assert meet(world) == 0
        lane = load_doc(world)["lanes"][0]
    finally:
        shutil.rmtree(base, ignore_errors=True)
    assert lane["counts"] == {"pass": 2, "fail": 0, "error": 0, "skip": 0}, lane["tests"]


# ================================================== git inside the meeting (plan 0135)

def test_the_profile_admits_only_literal_metadata_above_the_worktree(tmp_path, monkeypatch):
    """Runs in CI: no sandbox needed, only the parameters and the profile's text."""
    home = tmp_path / "home"
    wt = home / "dev" / ".fleet" / "work" / "r" / "wt"
    store = home / "dev" / ".fleet" / "r" / ".git-store"
    scratch = tmp_path / "tmpdir" / "meet-run-x"
    for d in (wt, store, scratch):
        d.mkdir(parents=True)
    monkeypatch.setattr(mod, "_real_home", lambda: os.path.realpath(str(home)))
    params = dict(a.split("=", 1) for a in mod._sb_params(wt, store, scratch, "/py")[1::2])
    real = os.path.realpath
    chain = {v for k, v in params.items() if k.startswith("META")}
    assert {real(str(home / "dev")), real(str(home / "dev" / ".fleet")),
            real(str(home / "dev" / ".fleet" / "work")),
            real(str(home / "dev" / ".fleet" / "work" / "r")),
            real(str(home / "dev" / ".fleet" / "r"))} <= chain
    assert len([k for k in params if k.startswith("META")]) == mod.META_SLOTS
    # Unused slots repeat an admitted path; none is the home or outside the chains.
    assert chain <= {real(str(p)) for p in (home / "dev", home / "dev" / ".fleet",
                     home / "dev" / ".fleet" / "work", home / "dev" / ".fleet" / "work" / "r",
                     home / "dev" / ".fleet" / "r", wt)}
    # More directories than slots is a host failure, never a truncation.
    deep = home.joinpath(*[f"d{i}" for i in range(mod.META_SLOTS + 1)])
    deep.mkdir(parents=True)
    with pytest.raises(mod.HostError):
        mod._sb_params(deep, store, scratch, "/py")
    text = (ROOT / "config" / "meeting-runner.sb").read_text()
    text = "\n".join(l.split(";;")[0] for l in text.splitlines())
    rules = [m for m in re.findall(r"\(allow file-read-metadata.*?\)\)", text, re.S)]
    assert rules
    for r in rules:
        assert "subpath" not in r and "regex" not in r and "file-read*" not in r
        assert "literal" in r
    assert not re.search(r"file-read-metadata[^)]*\((subpath|regex)", text)
    assert "WORKTREE_PARENT" not in text
    for i in range(mod.META_SLOTS):
        assert f'(param "META{i}")' in text


def test_a_scratch_directory_is_under_the_per_user_temp_dir(world):
    s = mod._new_scratch()
    try:
        assert str(s).startswith(os.path.realpath(tempfile.gettempdir()) + os.sep)
        assert not str(s).startswith("/private/tmp/")
    finally:
        shutil.rmtree(s, ignore_errors=True)


_GIT_TMP_PROBE = '''\
import subprocess


def _git(*a, cwd):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.com", *a], cwd=cwd,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_git_in_tmp_path(tmp_path):
    _git("init", "--bare", str(tmp_path / "m.git"), cwd=tmp_path)
    repo = tmp_path / "r"
    repo.mkdir()
    _git("init", cwd=repo)
    _git("commit", "--allow-empty", "-m", "x", cwd=repo)
    assert "x" in _git("log", "--oneline", cwd=repo)
'''

_GIT_WT_PROBE = '''\
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_git_in_the_worktree():
    for a in (["rev-parse", "HEAD"], ["status", "--short"]):
        r = subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
'''

_DENIED_PROBE = '''\
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_admitted_directories_give_metadata_and_nothing_else():
    d = ROOT.parent
    while d.name != "work":
        d = d.parent
    for admitted in (ROOT.parent, d, d.parent):
        os.stat(admitted)
        with pytest.raises(PermissionError):
            os.listdir(admitted)
        with pytest.raises(PermissionError):
            os.stat(admitted / "sibling.txt")
        with pytest.raises(PermissionError):
            open(admitted / "sibling.txt").read()
'''


def _under_home(world, monkeypatch, probes, *, without_rules=False):
    import pwd
    real_home = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
    base = Path(tempfile.mkdtemp(dir=str(real_home), prefix=".fleet-meet-test-"))
    try:
        monkeypatch.setenv("FLEET_WORK_ROOT", str(base / "work"))
        world.impl()
        world.lane(probes)
        monkeypatch.setattr(mod, "USE_PROFILE", True)
        if without_rules:       # the control: every slot repeats the worktree, no chain rule
            real_params = mod._sb_params

            def stripped(*a):
                out = real_params(*a)
                wt = next(x for x in out if x.startswith("WORKTREE=")).split("=", 1)[1]
                return [f"{x.split('=')[0]}={wt}" if x.startswith("META") else x for x in out]
            monkeypatch.setattr(mod, "_sb_params", stripped)
        # the sibling file and a file in each admitted directory
        real_add = mod.lib.add_worktree

        def add_with_siblings(*a, **k):
            wt = Path(os.path.realpath(real_add(*a, **k)))
            for d in (wt.parent, base / "work", base):
                (d / "sibling.txt").write_text("not for the lane")
            return wt
        monkeypatch.setattr(mod.lib, "add_worktree", add_with_siblings)
        assert meet(world) == 0
        return load_doc(world)["lanes"][0]
    finally:
        shutil.rmtree(base, ignore_errors=True)


@live
@pytest.mark.parametrize("probe,name", [(_GIT_TMP_PROBE, "git_tmp"), (_GIT_WT_PROBE, "git_wt")])
def test_git_runs_confined(world, monkeypatch, probe, name):
    lane = _under_home(world, monkeypatch, {f"tests/lane/test_{name}.py": probe})
    assert lane["counts"]["pass"] == 1 and lane["counts"]["fail"] == 0, lane["tests"]


@live
@pytest.mark.parametrize("probe,name", [(_GIT_TMP_PROBE, "git_tmp"), (_GIT_WT_PROBE, "git_wt")])
def test_control_git_fails_without_the_metadata_rules(world, monkeypatch, probe, name):
    lane = _under_home(world, monkeypatch, {f"tests/lane/test_{name}.py": probe}, without_rules=True)
    assert lane["counts"]["pass"] == 0, lane["tests"]


@live
def test_admitted_directories_are_metadata_only(world, monkeypatch):
    lane = _under_home(world, monkeypatch, {"tests/lane/test_denied.py": _DENIED_PROBE})
    assert lane["counts"]["pass"] == 1 and lane["counts"]["fail"] == 0, lane["tests"]


# ------------------------------- a merged lane PR (eunomia#912, 2026-10-16)
def test_a_merged_lane_pr_is_read_by_the_branch_in_its_label():
    """Forgejo deletes a merged PR's branch and reports `head.ref` as
    `refs/pull/<n>/head`, keeping the branch in `head.label`. The meeting
    refused eunomia#912 for that, after it had been read, reviewed and merged."""
    branch = f"agent/tests/{PLAN}/sub-codex"
    merged = {"merged": True, "head": {"ref": "refs/pull/912/head", "label": branch}}
    assert mod.lane_branch(merged) == branch
    # an open PR's ref is its branch; the label is never consulted
    open_pr = {"merged": False, "head": {"ref": "refs/pull/912/head", "label": branch}}
    assert mod.lane_branch(open_pr) == "refs/pull/912/head"
    # a cross-repository label (owner:branch) is not a branch of this repo
    fork = {"merged": True, "head": {"ref": "refs/pull/9/head", "label": f"someone:{branch}"}}
    assert mod.lane_branch(fork) == "refs/pull/9/head"
    # an ordinary ref is returned as it is
    assert mod.lane_branch({"merged": True, "head": {"ref": branch, "label": "x"}}) == branch


def test_a_merged_lane_is_diffed_against_its_recorded_merge_base(tmp_path):
    """Once merged, the lane commit is on main, so its merge-base with main is
    itself and the lane looked as if it added nothing (eunomia#912, "the lane
    adds no files"). The pull request's own `merge_base` is the base."""
    def git(*a):
        return subprocess.run(["git", *a], cwd=str(tmp_path), check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "README").write_text("x\n")
    git("add", "."); git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "tests" / "lane").mkdir(parents=True)
    (tmp_path / "tests" / "lane" / "test_x.py").write_text("def test_x():\n    pass\n")
    git("add", "."); git("commit", "-q", "-m", "lane")
    lane = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", lane)        # the lane is merged
    files, problems = mod.lane_paths(tmp_path, lane, base)
    assert files == [] and problems == ["the lane adds no files"], \
        "without the recorded base the merged lane adds nothing"
    files, problems = mod.lane_paths(tmp_path, lane, base, base=base)
    assert [p for p, _ in files] == ["tests/lane/test_x.py"] and problems == []
    _, problems = mod.lane_paths(tmp_path, lane, base, base="not-a-sha")
    assert problems and "not a sha" in problems[0]


# ================================================== the reviewers' read (plan 0138)
def _trailer(head, model="opus", tools="none", extra=""):
    """The dispatcher's three-line trailer, in the shape measured on eunomia#912."""
    return (f"Review-provenance: model={model}; session=0a1b2c3d-0000-4000-8000-000000000001; "
            f"head={head}; file-list=complete; model-source=configured; tools={tools}; {extra}"
            "same-session-as-author=no\n"
            "Review-provenance-ordinal: 1\n"
            "Review-provenance-role: verdict")


def _read(w, n, by, model, sha=None, tools="none", body=None, **kw):
    sha = sha or w.pulls[n]["head"]["sha"]
    text = body if body is not None else f"Looks right.\n\n{_trailer(sha, model, tools)}\n"
    return w.approve(n, by, sha, body=text, **kw)


@pytest.fixture
def reviewed(world, monkeypatch):
    """An autonomous repository with two configured reviewers and one lane that
    nobody has approved yet."""
    state = {"autonomy": "autonomous", "reviewers": ["revbot", "rev2bot"]}
    monkeypatch.setattr(mod, "read_autonomy", lambda repo: state["autonomy"])

    def reviewers(repo):
        if isinstance(state["reviewers"], Exception):
            raise state["reviewers"]
        return state["reviewers"]
    monkeypatch.setattr(mod, "expected_reviewers", reviewers)
    world.impl()
    world.marker = world.tmp / "marker"
    world.n1, world.sha1 = world.lane(marker_test(world.marker), approve=None)
    world.state = state
    return world


def _reasons(w):
    return {x["pr"]: x["reason"] for x in load_doc(w)["not_run"]}


def test_both_reviewers_at_the_head_without_tools_run_the_lane(reviewed):
    w = reviewed
    a = _read(w, w.n1, "revbot", "opus")
    b = _read(w, w.n1, "rev2bot", "gpt")
    assert meet(w) == 0 and w.marker.exists()
    (ln,) = load_doc(w)["lanes"]
    assert ln["reader"] == "reviewers" and ln["read_by"] == "revbot,rev2bot"
    assert ln["review_id"] == a and ln["review_ids"] == [a, b]
    assert ln["independent_reader"] is False


def test_two_reviewers_on_one_model_run_the_lane(reviewed):
    w = reviewed
    _read(w, w.n1, "revbot", "opus")
    _read(w, w.n1, "rev2bot", "opus")
    assert meet(w) == 0 and w.marker.exists()
    (ln,) = load_doc(w)["lanes"]
    assert ln["reader"] == "reviewers" and ln["read_by"] == "revbot,rev2bot"


def test_one_configured_reviewer_is_not_two(reviewed):
    w = reviewed
    w.state["reviewers"] = ["revbot"]
    _read(w, w.n1, "revbot", "opus")
    assert meet(w) == 75 and not doc_path(w).exists() and not w.marker.exists()


def test_a_stop_from_expected_reviewers_leaves_the_human_lane_running(reviewed):
    w = reviewed
    w.state["reviewers"] = mod._orch().Stop("fleet-models would not answer")
    _read(w, w.n1, "revbot", "opus")
    _read(w, w.n1, "rev2bot", "gpt")
    n2, _ = w.lane({"tests/lane/test_h.py": "def test_h():\n    pass\n"}, runner="sub-codex")
    assert meet(w) == 0
    doc = load_doc(w)
    assert [l["pr"] for l in doc["lanes"]] == [n2] and doc["lanes"][0]["reader"] == "human"
    assert doc["lanes"][0]["review_ids"] == [doc["lanes"][0]["review_id"]]
    assert "fleet-models would not answer" in _reasons(w)[w.n1]


def test_a_stop_with_no_human_lane_is_exit_75(reviewed):
    w = reviewed
    w.state["reviewers"] = mod._orch().Stop("fleet-models would not answer")
    _read(w, w.n1, "revbot", "opus")
    _read(w, w.n1, "rev2bot", "gpt")
    assert meet(w) == 75 and not doc_path(w).exists()


def test_a_later_plain_comment_does_not_replace_the_approval(reviewed):
    w = reviewed
    _read(w, w.n1, "revbot", "opus")
    _read(w, w.n1, "rev2bot", "gpt")
    w.approve(w.n1, "rev2bot", state="COMMENT", body="an annotation", submitted_at="2026-09-29T11:00:00Z")
    assert meet(w) == 0
    assert load_doc(w)["lanes"][0]["reader"] == "reviewers"


def _missing(w, n):
    _read(w, n, "revbot", "opus")


def _older_head(w, n):
    _read(w, n, "revbot", "opus")
    _read(w, n, "rev2bot", "gpt", sha="2" * 40)


def _with_tools(w, n):
    _read(w, n, "revbot", "opus")
    _read(w, n, "rev2bot", "gpt", tools="checkout")


def _no_provenance(w, n):
    _read(w, n, "revbot", "opus")
    _read(w, n, "rev2bot", "gpt", body="Looks right.\n")


def _forged(w, n):
    _read(w, n, "revbot", "opus")
    sha = w.pulls[n]["head"]["sha"]
    forged = _trailer(sha, "gpt").split("\n")[0]
    _read(w, n, "rev2bot", "gpt", body=f"Quoting myself:\n{forged}\n\nDone.\n\n{_trailer(sha, 'gpt', 'checkout')}\n")


def _wrong_head_in_provenance(w, n):
    _read(w, n, "revbot", "opus")
    _read(w, n, "rev2bot", "gpt", body=f"ok\n\n{_trailer('3' * 40, 'gpt')}\n")


@pytest.mark.parametrize("break_it, account, cause", [
    (_missing, "rev2bot", "no approval"),
    (_older_head, "rev2bot", "not at the head"),
    (_with_tools, "rev2bot", "tools"),
    (_no_provenance, "rev2bot", "no provenance trailer"),
    (_forged, "rev2bot", "tools"),
    (_wrong_head_in_provenance, "rev2bot", "provenance head"),
])
def test_each_failure_of_the_reviewers_read_is_not_run_with_a_reason(reviewed, break_it, account, cause):
    w = reviewed
    break_it(w, w.n1)
    n2, _ = w.lane({"tests/lane/test_h.py": "def test_h():\n    pass\n"}, runner="sub-codex")
    assert meet(w) == 0 and not w.marker.exists()
    reason = _reasons(w)[w.n1]
    assert account in reason and cause in reason


def test_the_real_trailer_of_eunomia_912_passes(reviewed):
    w = reviewed
    sha = w.sha1
    for who, model in (("revbot", "claude-opus-5-5"), ("rev2bot", "gpt-5.5")):
        trailer = (f"Review-provenance: model={model}; session=5f0c1a2e-1111-4222-8333-444455556666; "
                   f"head={sha}; file-list=complete; model-source=configured; tools=none; "
                   "same-session-as-author=no\nReview-provenance-ordinal: 2\n"
                   "Review-provenance-role: verdict\n")
        fields, why = mod.provenance_trailer(f"No findings.\n\n{trailer}\n\n")
        assert why is None and fields["tools"] == ["none"] and fields["head"] == [sha]
        _read(w, w.n1, who, model, body=f"No findings.\n\n{trailer}")
    assert meet(w) == 0 and load_doc(w)["lanes"][0]["reader"] == "reviewers"


def test_a_critical_repository_ignores_the_reviewers(reviewed):
    w = reviewed
    w.state["autonomy"] = "critical"
    _read(w, w.n1, "revbot", "opus")
    _read(w, w.n1, "rev2bot", "gpt")
    assert meet(w) == 75 and not doc_path(w).exists() and not w.marker.exists()


def test_an_unreadable_autonomy_is_critical(world, monkeypatch, tmp_path):
    bad = tmp_path / "bad-config"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    monkeypatch.setattr(mod, "BIN", tmp_path)
    (tmp_path / "fleet-config").write_text("#!/bin/sh\necho garbage\nexit 3\n")
    (tmp_path / "fleet-config").chmod(0o755)
    assert mod.read_autonomy(REPO) == "critical"
    (tmp_path / "fleet-config").write_text('#!/bin/sh\necho \'{"value": "autonomous"}\'\n')
    assert mod.read_autonomy(REPO) == "autonomous"


def test_the_humans_approval_still_passes_on_an_autonomous_repository(reviewed):
    w = reviewed
    w.approve(w.n1, "operator")
    assert meet(w) == 0
    (ln,) = load_doc(w)["lanes"]
    assert ln["reader"] == "human" and ln["read_by"] == "operator" and ln["independent_reader"] is False


# ========================================== plan 0175: named lanes, and "later"
def _list_requests(w):
    return [p for m, p in w.httpd.requests
            if m == "GET" and p.split("?")[0].endswith("/pulls")]


def test_with_lane_the_pull_listing_is_never_read(world, monkeypatch):
    world.impl()
    n, _ = world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})

    def boom(*a, **k):
        raise AssertionError("list_pulls called")
    monkeypatch.setattr(mod._Forge, "list_pulls", boom)
    assert meet(world, "--lane", n) == 0
    assert _list_requests(world) == []


def test_without_lane_the_listing_is_read_with_state_all_and_finds_a_merged_lane(world):
    world.impl(state="closed", merged=True)
    n, _ = world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    assert meet(world) == 0
    assert [l["pr"] for l in load_doc(world)["lanes"]] == [n]
    assert any("state=all" in p for p in _list_requests(world))


def test_a_listing_page_that_times_out_exits_75_and_writes_nothing(world, monkeypatch, capsys):
    world.impl()
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    real = mod.urllib.request.urlopen

    def slow(req, *a, **k):
        if "state=all" in req.full_url:
            raise socket.timeout("timed out")
        return real(req, *a, **k)
    monkeypatch.setattr(mod.urllib.request, "urlopen", slow)
    assert meet(world) == 75 and not doc_path(world).exists()
    assert "listing did not finish" in capsys.readouterr().err


def test_a_listing_page_with_status_401_exits_1(world, monkeypatch):
    world.impl()
    world.lane({"tests/lane/test_a.py": "def test_a():\n    pass\n"})
    monkeypatch.setattr(mod._Forge, "list_pulls", lambda self, *a, **k: (401, None))
    assert meet(world) == 1 and not doc_path(world).exists()


def test_a_read_that_never_left_the_process_is_not_later(monkeypatch):
    forge = mod._Forge("http://127.0.0.1:9", "tok")
    assert forge.get_pull("not a repo", 1) == (0, None)    # malformed repo
    assert not forge.transport_failed
    assert type(mod._read_failure(forge, "x")) is mod.HostError
    monkeypatch.setattr(type(forge._backend), "get_pull", lambda *a, **k: None)
    assert forge.get_pull("operator/eunomia", 1) == (0, None)   # verb the backend lacks
    assert not forge.transport_failed
    assert type(mod._read_failure(forge, "x")) is mod.HostError


def test_a_refused_connection_is_later(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    forge = mod._Forge(f"http://127.0.0.1:{port}", "tok", timeout=2)
    assert forge.get_pull("operator/eunomia", 1) == (0, None)
    assert forge.transport_failed
    assert type(mod._read_failure(forge, "x")) is mod.ForgeLater

