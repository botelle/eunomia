"""Tests for bin/fleet-lane — a test lane runs once, from a bundle (plan 0070).

No real vendor CLI is ever spawned. A FAKE vendor — a small Python fixture
script this file writes — plays antigravity's or codex's part: it reads
stdin, checks (or records, for the test to check) the shape the adapter
should have sent, and emits a canned result in the vendor's own output
shape. Everything else is real: a real git repository as the fixture's
"forge" (local bare clones, no SSH), and a real `http.server.HTTPServer` in
a background thread standing in for Forgejo's REST API — the same two
choices `tests/test_fleetforge.py` and `tests/test_fleetlib_workspace.py`
already make, and for the same reason: a mock can only be wrong about git's
or urlopen's contract in the same way the code under test is.
"""
import base64
import contextlib
import hashlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


mod = _load("fleet_lane_under_test", BIN / "fleet-lane")

_ENV_KEYS = ("EUNOMIA_FLEET_DIR", "EUNOMIA_SESSION", "FLEET_WORK_ROOT",
            "FLEET_GIT_SSH_BASE", "FLEET_FORGEJO_URL", "FLEET_AUTHOR_TOKEN_CMD",
            "FLEET_FORGE_TIMEOUT", "FLEET_DB")


def _reset_env(fleet_dir, work_root, ssh_base, forgejo_url, **extra):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["EUNOMIA_SESSION"] = "fleet-lane-test"
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["FLEET_WORK_ROOT"] = str(work_root)
    os.environ["FLEET_GIT_SSH_BASE"] = str(ssh_base)
    os.environ["FLEET_FORGEJO_URL"] = forgejo_url
    os.environ["FLEET_AUTHOR_TOKEN_CMD"] = "echo test-implbot-token"
    for k, v in extra.items():
        os.environ[k] = v


# --------------------------------------------------------------- git fixture
def _git(args, cwd, check=True):
    r = subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@e.com",
                        *args], cwd=str(cwd), capture_output=True, text=True)
    if check:
        assert r.returncode == 0, f"git {args} in {cwd} failed: {r.stderr}"
    return r


PLAN_ID = "0070-a-test-lane-runs-once-from-a-bundle"
REPO = "operator/demo"


def _build_target_repo(tmp_path, plan_id=PLAN_ID, zone="public",
                       declared_behaviour=True, preexisting_tests=None, repo=REPO,
                       workflows=None):
    """A minimal repository, committed and cloned bare, standing in for the
    forge. Returns (sha, ssh_base)."""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    plan_text = (
        "---\n"
        f"id: {plan_id}\n"
        f"zone: {zone}\n"
        "status: ready\n"
        "---\n\n"
        "# a test plan\n\n"
        "## 6. Resources\n\n"
        "- docs/adr/0001-example.md — the one ADR this plan cites\n"
    )
    (src / "plans").mkdir()
    (src / "plans" / f"{plan_id}.md").write_text(plan_text)
    (src / "docs" / "adr").mkdir(parents=True)
    (src / "docs" / "adr" / "0001-example.md").write_text(
        "# ADR-0001 — example\n\nthis is the cited ADR's text.\n")
    if declared_behaviour:
        (src / "docs" / "declared-behaviour.md").write_text(
            "# declared behaviour\n\nexits 0 on success, 1 otherwise.\n")
    for rel, content in (workflows or {}).items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
    (src / "tests").mkdir(exist_ok=True)
    if preexisting_tests:
        for rel, content in preexisting_tests.items():
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    else:
        (src / "tests" / ".gitkeep").write_text("")
    _git(["add", "."], src)
    _git(["commit", "-qm", "root"], src)
    sha = _git(["rev-parse", "HEAD"], src).stdout.strip()

    owner, name = repo.split("/")
    bare = tmp_path / "forge" / owner / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return sha, str(tmp_path / "forge")


def _bare_repo_path(tmp_path, repo=REPO):
    owner, name = repo.split("/")
    return tmp_path / "forge" / owner / f"{name}.git"


# -------------------------------------------------------------- forge stub
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append({"method": self.command, "path": self.path, "body": raw})
        status, body = self.server.route(self.command, self.path, raw)
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle


@contextlib.contextmanager
def _stub_forge(pr_number=42, pr_status=201):
    """A Forgejo double serving exactly the one call fleet-lane makes now:
    creating the landing pull request. Anything else 404s, and every request
    is recorded so a test can assert NOTHING ELSE was ever called."""
    def route(method, path, body):
        if method == "POST" and re.match(r"^/api/v1/repos/[^/]+/[^/]+/pulls$", path):
            return pr_status, ({"number": pr_number} if pr_status < 300 else None)
        return 404, {"message": "not found"}

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.route = route
    httpd.requests = []
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        t.join(timeout=5)


# ------------------------------------------------------------- fake vendors
_FAKE_ANTIGRAVITY_TURNS = '''\
import json, os, sys
line = sys.stdin.readline()
captured = [line]
with open("env_capture.json", "w") as f:
    json.dump(dict(os.environ), f)
try:
    r = subprocess = __import__("subprocess")
    p = r.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    inside_repo = (p.returncode == 0)
except Exception:
    inside_repo = None
with open("cwd_check.json", "w") as f:
    json.dump({"inside_repo": inside_repo}, f)
req = json.loads(line)
result = {"files": [{"path": "tests/lane/test_generated.py",
                     "content": "def test_generated():\\n    assert True\\n"}],
         "gaps": ["an example gap the fake vendor reported"],
         "notes": "from the fake antigravity vendor"}
# antigravity 1.3.3 as measured 2026-10-12: one turn and one `result` line per
# stdin line, fields inside `result`, `response` a JSON string holding the
# document twice. FIRST_TURN_ANSWERS unset is the measured run: the first turn
# is a denied tool call and the turn after it answers.
again = dict(result, toolAction="x", toolSummary="y")
answered = {"status": "SUCCESS", "response": json.dumps(result) + "\\n" + json.dumps(again),
            "num_turns": 3, "usage": {"tokens": 123}}
denied = {"status": "SUCCESS", "response": "", "num_turns": 1, "usage": {"tokens": 7},
          "denied_actions": [{"action": "command", "display_name": "RunCommand"}]}
first = answered if FIRST_TURN_ANSWERS else denied
sys.stdout.write(json.dumps({"event": "result", "result": first}) + "\\n")
sys.stdout.flush()
for more in sys.stdin:
    captured.append(more)
    if not FIRST_TURN_ANSWERS:
        sys.stdout.write("jetski: no output produced\\n")
    sys.stdout.write(json.dumps({"event": "result", "result": answered}) + "\\n")
    sys.stdout.flush()
with open("stdin_capture.bin", "w") as f:
    f.write("".join(captured))
'''
_FAKE_ANTIGRAVITY = _FAKE_ANTIGRAVITY_TURNS.replace("FIRST_TURN_ANSWERS", "False")


def _write_fake_antigravity(cwd, body=None):
    p = cwd / "fake_antigravity.py"
    p.write_text(body if body is not None else _FAKE_ANTIGRAVITY)
    return p


def _write_fake_codex(cwd, result_doc):
    p = cwd / "fake_codex.py"
    p.write_text(
        "import json, sys\n"
        "prompt = sys.stdin.read()\n"
        "argv = sys.argv[1:]\n"
        "out_path = argv[argv.index('-o') + 1]\n"
        f"result = {json.dumps(result_doc)}\n"
        "with open(out_path, 'w') as f:\n"
        "    json.dump(result, f)\n"
    )
    return p


def _fake_antigravity_runner(orch_mod, script, settings_must_not_grant=()):
    return orch_mod.Runner(
        "sub-antigravity",
        (sys.executable, str(script), "--json-schema", "<schema>"),
        False, "local_cli", (), "", "google", tuple(settings_must_not_grant))


def _load_orch():
    return mod._orchestrator_module()


def _seed_testers(rows):
    """Write `repo_model` tester rows into the test's own fleet.db. `rows` is
    [(repo_or_'default', runner)], in ordinal order per repo."""
    models = mod._models_module()
    Path(models.DB).parent.mkdir(parents=True, exist_ok=True)
    con = models.connect()
    try:
        for repo, runner in rows:
            models.set_runner(con, repo, "tester", runner, actor="test", source="test")
    finally:
        con.close()


def _seed_cap(value):
    """Write the owner's `lane_run_cap` row into the test's own fleet.db."""
    cfg = mod._config_module()
    Path(cfg.DB).parent.mkdir(parents=True, exist_ok=True)
    con = cfg.connect()
    try:
        cfg.set_value(con, "lane_run_cap", value, actor="test")
    finally:
        con.close()


DEFAULT_CAP = "sub-opus=24,sub-antigravity=24,sub-codex=24"


def _run(tmp_path, repo, plan_id, sha, runner_name, orch_mod, testers=None,
         dry_run=False, schema_path=None, pr_status=201, cap=DEFAULT_CAP):
    """`testers` None means "the repo names exactly `runner_name`" — the
    happy setup. Pass [] for a fleet.db with no tester rows at all. `cap` is
    the `lane_run_cap` row (plan 0100); None writes none."""
    with _stub_forge(pr_status=pr_status) as (httpd, base_url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", base_url)
        if cap is not None:
            _seed_cap(cap)
        if testers is None:
            testers = [(repo, runner_name)] if runner_name in mod._orchestrator_module().RUNNERS else []
        if testers:
            _seed_testers(testers)
        rc = mod.run(repo, plan_id, sha, runner_name, schema_path=schema_path,
                     dry_run=dry_run, orch_mod=orch_mod)
        return rc, httpd.requests


# ------------------------------------------------------------------ dry run
def test_dry_run_writes_prompt_and_spawns_nothing(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    script = _write_fake_antigravity(tmp_path)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(orch, script)

    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch, dry_run=True)

    assert rc == 0
    lane_dir = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-antigravity"
    assert (lane_dir / "prompt.txt").is_file()
    assert (lane_dir / "bundle.json").is_file()
    assert not (lane_dir / "cwd").exists(), "a dry run must spawn nothing"
    # no PR call was made
    assert not [r for r in requests if r["method"] == "POST"]
    # both worktrees are gone
    assert not mod.lib.worktree_path(REPO, f"lane-read-{PLAN_ID}-sub-antigravity").exists()
    assert not mod.lib.worktree_path(REPO, f"lane-land-{PLAN_ID}-sub-antigravity").exists()


def test_dry_run_prompt_carries_plan_and_cited_adr_and_bundle(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))

    _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch, dry_run=True)

    lane_dir = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-antigravity"
    text = (lane_dir / "prompt.txt").read_text()
    assert "# a test plan" in text
    assert "this is the cited ADR's text." in text
    assert '"bundle_version"' in text


# --------------------------------------------------------------- happy path
def test_a_real_run_lands_a_draft_pr_with_the_fake_vendors_files(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))

    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)

    assert rc == 0, "expected a clean run"
    posts = [r for r in requests if r["method"] == "POST"]
    assert len(posts) == 1, f"expected exactly one PR creation call, got {requests}"
    pr_body = json.loads(posts[0]["body"])
    assert pr_body["title"].startswith("WIP: ")
    assert pr_body["base"] == "main"
    assert pr_body["head"] == f"agent/tests/{PLAN_ID}/sub-antigravity"
    assert f"Tests-for: {PLAN_ID}" in pr_body["body"]
    assert f"Plan: {PLAN_ID}" not in pr_body["body"].replace("> Plan:", "")

    # the branch landed on the bare "forge" with the fake vendor's file
    bare = _bare_repo_path(tmp_path)
    show = subprocess.run(
        ["git", "show", f"agent/tests/{PLAN_ID}/sub-antigravity:tests/lane/test_generated.py"],
        cwd=bare, capture_output=True, text=True)
    assert show.returncode == 0, show.stderr
    assert "def test_generated" in show.stdout

    # both worktrees gone
    assert not mod.lib.worktree_path(REPO, f"lane-read-{PLAN_ID}-sub-antigravity").exists()
    assert not mod.lib.worktree_path(REPO, f"lane-land-{PLAN_ID}-sub-antigravity").exists()


def test_the_fake_vendors_stdin_matches_the_adapters_documented_shape(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    script = _write_fake_antigravity(tmp_path)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(orch, script)

    prompt_holder = {}
    real_build_prompt = mod.build_prompt

    def _spy(plan_text, worktree, bundle_text):
        p = real_build_prompt(plan_text, worktree, bundle_text)
        prompt_holder["prompt"] = p
        return p
    mod.build_prompt = _spy
    try:
        rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    finally:
        mod.build_prompt = real_build_prompt
    assert rc == 0

    lane_dir = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-antigravity"
    captured = (lane_dir / "cwd" / "stdin_capture.bin").read_bytes()
    expected = "".join(orch.antigravity_stdin_line(t) + "\n" for t in (
        prompt_holder["prompt"], mod.ANTIGRAVITY_SECOND_TURN)).encode("utf-8")
    assert captured == expected, "the fake vendor's stdin must match the adapter byte for byte"
    assert mod.ANTIGRAVITY_SECOND_TURN not in prompt_holder["prompt"]
    assert (lane_dir / "stdin.bin").read_bytes() == expected


def test_an_antigravity_first_turn_that_answers_is_sent_no_second_line(tmp_path):
    """The second line follows a denied first turn only. A first turn that
    held the document ends the run: one stdin line, and one turn's usage."""
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    fake = _write_fake_antigravity(
        tmp_path, _FAKE_ANTIGRAVITY_TURNS.replace("FIRST_TURN_ANSWERS", "True"))
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(orch, fake)
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 0
    lane_dir = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-antigravity"
    captured = (lane_dir / "cwd" / "stdin_capture.bin").read_text()
    assert captured.count("\n") == 1 and mod.ANTIGRAVITY_SECOND_TURN not in captured
    assert (lane_dir / "stdin.bin").read_bytes() == captured.encode("utf-8")
    assert (lane_dir / "stdout.redacted.txt").read_text().count('"event": "result"') == 1


def test_the_vendor_env_carries_only_path_home_and_extra_env(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 0

    lane_dir = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-antigravity"
    env_seen = json.loads((lane_dir / "cwd" / "env_capture.json").read_text())
    # A Python fixture script is not a hermetic probe of what fleet-lane
    # actually put in the `env=` dict passed to Popen: CPython's own PEP 538
    # locale coercion, and macOS's CoreFoundation process bootstrap, both
    # write a couple of keys into the CHILD's os.environ from inside the
    # child itself, after exec — not because they were in the dict this
    # program built. Tolerated here by name; anything ELSE, and in
    # particular anything FLEET_/EUNOMIA_-prefixed or token-shaped, is not.
    _RUNTIME_INJECTED = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert set(env_seen) <= {"PATH", "HOME"} | _RUNTIME_INJECTED, env_seen
    assert {"PATH", "HOME"} <= set(env_seen)
    for bad_prefix in ("FLEET_", "EUNOMIA_"):
        assert not any(k.startswith(bad_prefix) for k in env_seen), env_seen


def test_the_vendor_cwd_is_outside_every_git_repository(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 0

    lane_dir = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-antigravity"
    check = json.loads((lane_dir / "cwd" / "cwd_check.json").read_text())
    assert check["inside_repo"] is False


def test_no_review_is_dispatched(tmp_path):
    """`review_command` names the capability this program must never reach
    for — nothing in bin/fleet-lane calls it, or anything shaped like it, and
    this pins that with both a static check and a live stub that would fail
    the test if fleet-lane ever called it."""
    calls = []

    def review_command(*a, **k):
        calls.append((a, k))
        raise AssertionError("fleet-lane must never dispatch a review")
    mod.review_command = review_command
    try:
        sha, ssh_base = _build_target_repo(tmp_path)
        orch = _load_orch()
        orch.RUNNERS = dict(orch.RUNNERS)
        orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
            orch, _write_fake_antigravity(tmp_path))
        rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
        assert rc == 0
    finally:
        del mod.review_command
    assert calls == []
    text = (BIN / "fleet-lane").read_text()
    for needle in ("dispatch-review", "review_command", "revbot"):
        assert needle not in text


# ---------------------------------------------------------------- the gates
def test_zone_gate_refuses_a_private_plan(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path, zone="private")
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))

    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_zone_gate_refuses_an_absent_zone(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    (src / "plans").mkdir()
    (src / "plans" / f"{PLAN_ID}.md").write_text(
        f"---\nid: {PLAN_ID}\nstatus: ready\n---\n\n# no zone line\n")
    (src / "docs").mkdir()
    (src / "docs" / "declared-behaviour.md").write_text("exits 0\n")
    _git(["add", "."], src)
    _git(["commit", "-qm", "root"], src)
    sha = _git(["rev-parse", "HEAD"], src).stdout.strip()
    bare = tmp_path / "forge" / "operator" / "demo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)

    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def _antigravity_orch(tmp_path):
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))
    return orch


def test_tester_gate_refuses_when_no_tester_resolves(tmp_path, capsys):
    sha, ssh_base = _build_target_repo(tmp_path)
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity",
                        _antigravity_orch(tmp_path), testers=[], dry_run=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ADR-0013 §2" in err and "fleet-models set default tester sub-opus" in err
    assert requests == []


def test_tester_gate_refuses_a_runner_the_repo_did_not_name(tmp_path, capsys):
    """A non-empty list is not enough: the repo names sub-opus, the caller asked
    for antigravity, and a bundle must not reach a vendor the repo never chose."""
    sha, ssh_base = _build_target_repo(tmp_path)
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity",
                        _antigravity_orch(tmp_path), testers=[("default", "sub-opus")],
                        dry_run=True)
    assert rc == 1
    assert "not one of" in capsys.readouterr().err
    assert requests == []


def test_default_row_passes_gate_two_with_no_runner_flag(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, None, _load_orch(),
                        testers=[("default", "sub-opus")], dry_run=True)
    assert rc == 0
    log = (mod.lib.work_root() / "logs" / f"lane--{PLAN_ID}--sub-opus.log").read_text()
    assert "gate-tester" in log


def test_omitted_runner_with_no_ordinal_one_refuses(tmp_path, capsys):
    sha, ssh_base = _build_target_repo(tmp_path)
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, None, _load_orch(), testers=[], dry_run=True)
    assert rc == 1
    assert "ADR-0013 §2" in capsys.readouterr().err


def test_a_second_tester_row_is_a_legitimate_runner_and_a_stranger_is_not(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    rows = [("default", "sub-opus"), (REPO, "sub-antigravity")]
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity",
                 _antigravity_orch(tmp_path), testers=rows, dry_run=True)
    assert rc == 0
    # ...and the repo's own row shadows default at the SAME ordinal, so the
    # omitted runner is the repo's, not the default's.
    assert mod.default_tester(REPO) == "sub-antigravity"


def test_no_fleet_db_is_a_refusal_not_a_pass(tmp_path, capsys):
    _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", "http://127.0.0.1:1")
    with pytest.raises(mod.Stop):
        mod.check_tester(REPO, "sub-opus")


def test_adapter_table_refuses_a_runner_this_program_has_not_measured(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    # sub-sonnet is a real, WIRED registry runner (the implementer's own) —
    # exactly the case ADR-0009 §3 says any position may take, and exactly
    # the one this program must still refuse: it has no adapter table entry.
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-sonnet", orch)
    assert rc == 1
    assert requests == [], "an unmeasured runner must be refused before any forge call"


def test_unknown_runner_name_is_refused(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-does-not-exist", orch)
    assert rc == 1
    assert requests == []


def test_unwired_runner_is_refused_and_never_spawned(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    # sub-codex is wired (plan 0101), and nothing else in the registry is both
    # unwired and in the adapter table, so gate 4 is reached with a patched entry.
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-codex"] = orch.RUNNERS["sub-codex"]._replace(unwired="not wired: a test entry")
    assert orch.RUNNERS["sub-codex"].unwired
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-codex", orch)
    assert rc == 1
    assert requests == [], "an unwired runner must never reach the forge or a worktree"
    assert not mod.lib.worktree_path(REPO, f"lane-read-{PLAN_ID}-sub-codex").exists()


def test_settings_precondition_refuses_a_permissions_allow_grant(tmp_path):
    settings = tmp_path / "fixture-settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["read_file"]}}))
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path), settings_must_not_grant=(str(settings),))
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_settings_precondition_refuses_a_hooks_block(tmp_path):
    settings = tmp_path / "fixture-settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path), settings_must_not_grant=(str(settings),))
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_declared_behaviour_gap_refuses_the_bundle(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path, declared_behaviour=False)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


# ------------------------------------------------------------- the result
def _run_with_result(tmp_path, result_doc, preexisting_tests=None):
    sha, ssh_base = _build_target_repo(tmp_path, preexisting_tests=preexisting_tests)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    script_body = (
        "import json, sys\n"
        "sys.stdin.readline()\n"
        f"result = {json.dumps(result_doc)}\n"
        "out = {'event': 'result', 'response': result, 'usage': None}\n"
        "sys.stdout.write(json.dumps(out) + chr(10))\n"
    )
    script = tmp_path / "fake_bad_vendor.py"
    script.write_text(script_body)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(orch, script)
    return _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)


@pytest.mark.parametrize("bad_path", [
    "../x", "/etc/x", "tests/../.forgejo/workflows/x.yml",
    ".forgejo/workflows/x.yml", ".claude/settings.json",
])
def test_a_confined_bad_path_refuses_the_whole_result_and_leaves_no_branch(tmp_path, bad_path):
    rc, requests = _run_with_result(
        tmp_path, {"files": [{"path": bad_path, "content": "x"}], "gaps": [], "notes": ""})
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]
    bare = _bare_repo_path(tmp_path)
    br = subprocess.run(["git", "branch", "-a"], cwd=bare, capture_output=True, text=True)
    assert f"agent/tests/{PLAN_ID}" not in br.stdout


def test_a_symlinked_ancestor_in_the_fixture_worktree_is_refused(tmp_path):
    """The symlink is placed in the SOURCE tree that gets committed and
    cloned bare, so it is present in the landing worktree before the result
    is checked — exactly "a symlink placed in the fixture worktree"."""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    (src / "plans").mkdir()
    (src / "plans" / f"{PLAN_ID}.md").write_text(
        f"---\nid: {PLAN_ID}\nzone: public\nstatus: ready\n---\n\n# plan\n")
    (src / "docs").mkdir()
    (src / "docs" / "declared-behaviour.md").write_text("exits 0\n")
    (src / "tests").mkdir()
    (src / "tests" / "lane").mkdir()
    (src / "tests" / "lane" / "evil").symlink_to("/etc")
    _git(["add", "."], src)
    _git(["commit", "-qm", "root"], src)
    sha = _git(["rev-parse", "HEAD"], src).stdout.strip()
    bare = tmp_path / "forge" / "operator" / "demo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)

    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    script_body = (
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "result = {'files': [{'path': 'tests/lane/evil/passwd', 'content': 'x'}],"
        " 'gaps': [], 'notes': ''}\n"
        "out = {'event': 'result', 'response': result, 'usage': None}\n"
        "sys.stdout.write(json.dumps(out) + chr(10))\n"
    )
    script = tmp_path / "fake_symlink_vendor.py"
    script.write_text(script_body)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(orch, script)
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_an_executable_marked_file_refuses_the_whole_result(tmp_path):
    rc, requests = _run_with_result(
        tmp_path, {"files": [{"path": "tests/lane/test_x.py", "content": "x",
                              "executable": True}], "gaps": [], "notes": ""})
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_a_path_that_already_exists_at_the_landing_base_refuses(tmp_path):
    rc, requests = _run_with_result(
        tmp_path,
        {"files": [{"path": "tests/lane/conftest.py", "content": "x"}], "gaps": [], "notes": ""},
        preexisting_tests={"tests/lane/conftest.py": "# existing\n"})
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_the_landing_check_is_against_mains_tip_at_landing_time_not_sha(tmp_path):
    """D1.6: 'the check is against that tip (fetched then), not against
    `<sha>` — a test merged after `<sha>` must be just as unwritable.' A
    file absent at `--sha` but present on `main` by landing time must still
    refuse."""
    sha, ssh_base = _build_target_repo(tmp_path)   # no tests/conftest.py at sha
    src = tmp_path / "src"
    (src / "tests" / "lane").mkdir()
    (src / "tests" / "lane" / "conftest.py").write_text("# added after sha\n")
    _git(["add", "."], src)
    _git(["commit", "-qm", "add conftest after sha"], src)
    bare = _bare_repo_path(tmp_path)
    _git(["push", "-q", str(bare), "main"], src)

    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    script_body = (
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "result = {'files': [{'path': 'tests/lane/conftest.py', 'content': 'x'}],"
        " 'gaps': [], 'notes': ''}\n"
        "out = {'event': 'result', 'response': result, 'usage': None}\n"
        "sys.stdout.write(json.dumps(out) + chr(10))\n"
    )
    script = tmp_path / "fake_landing_tip_vendor.py"
    script.write_text(script_body)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(orch, script)

    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_a_non_shape_result_refuses(tmp_path):
    rc, requests = _run_with_result(tmp_path, {"not": "the right shape"})
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_a_valid_file_lands_mode_0644(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 0
    land_wt = mod.lib.worktree_path(REPO, f"lane-land-{PLAN_ID}-sub-antigravity")
    # the worktree is removed after landing; re-checkout the pushed branch to
    # inspect the mode it was committed with, via git's own recorded mode bits
    bare = _bare_repo_path(tmp_path)
    ls = subprocess.run(
        ["git", "ls-tree", f"agent/tests/{PLAN_ID}/sub-antigravity", "tests/lane/test_generated.py"],
        cwd=bare, capture_output=True, text=True)
    assert ls.returncode == 0
    assert ls.stdout.startswith("100644"), ls.stdout


# -------------------------------------------------------------------- codex
def test_codex_adapter_reads_the_out_file_via_the_fake_vendor(tmp_path):
    """The CODEX ADAPTER shape in isolation, with a fake runner in place of
    the registry's sandboxed argv: no test outside the opt-in live tier spawns
    the real codex."""
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    result_doc = {"files": [{"path": "tests/lane/test_from_codex.py",
                             "content": "def test_c():\n    assert True\n"}],
                 "gaps": [], "notes": "from fake codex"}
    script = _write_fake_codex(tmp_path, result_doc)
    fake_runner = orch.Runner("sub-codex", (sys.executable, str(script), "-o", "<out>"),
                              False, "local_cli", (), "", "openai", ())
    orch.RUNNERS["sub-codex"] = fake_runner

    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-codex", orch)
    assert rc == 0
    posts = [r for r in requests if r["method"] == "POST"]
    assert len(posts) == 1
    bare = _bare_repo_path(tmp_path)
    show = subprocess.run(
        ["git", "show", f"agent/tests/{PLAN_ID}/sub-codex:tests/lane/test_from_codex.py"],
        cwd=bare, capture_output=True, text=True)
    assert show.returncode == 0
    assert "def test_c" in show.stdout


def test_codex_stdin_is_the_raw_prompt_not_wrapped(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    result_doc = {"files": [{"path": "tests/lane/test_stdin_capture.py",
                             "content": "def test_x():\n    assert True\n"}],
                 "gaps": [], "notes": ""}
    script_path = tmp_path / "fake_codex_capture.py"
    script_path.write_text(
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        f"open({str(tmp_path / 'captured_prompt.txt')!r}, 'w').write(prompt)\n"
        "argv = sys.argv[1:]\n"
        "out_path = argv[argv.index('-o') + 1]\n"
        "import json\n"
        f"json.dump({json.dumps(result_doc)}, open(out_path, 'w'))\n"
    )
    fake_runner = orch.Runner("sub-codex", (sys.executable, str(script_path), "-o", "<out>"),
                              False, "local_cli", (), "", "openai", ())
    orch.RUNNERS["sub-codex"] = fake_runner
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-codex", orch)
    assert rc == 0
    captured = (tmp_path / "captured_prompt.txt").read_text()
    assert captured.startswith(mod.LANE_PREAMBLE.splitlines()[0])


def test_codex_placeholders_are_filled_and_no_api_key_reaches_the_child(tmp_path, monkeypatch):
    """Plan 0101 D4: the registry argv's `<profile>`, `<lane>`, `<codex-home>`,
    `<schema>` and `<out>` are all filled, the schema is in the scratch cwd the
    profile lets the process read, and `OPENAI_API_KEY` / `CODEX_API_KEY` never
    reach the child even when the parent holds them (a key would silently move
    a subscription lane onto API billing)."""
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    seen = tmp_path / "seen.json"
    script = tmp_path / "fake_codex_probe.py"
    script.write_text(
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        "argv = sys.argv[1:]\n"
        "out = argv[argv.index('-o') + 1]\n"
        f"json.dump({{'argv': argv, 'cwd': os.getcwd(), 'env': dict(os.environ),\n"
        "           'schema_in_cwd': os.path.isfile(os.path.join(os.getcwd(), 'schema.json'))},\n"
        f"          open({str(seen)!r}, 'w'))\n"
        "json.dump({'files': [{'path': 'tests/lane/test_probe.py', 'content': 'def test_p():\\n    pass\\n'}],\n"
        "           'gaps': [], 'notes': ''}, open(out, 'w'))\n"
    )
    orch.RUNNERS["sub-codex"] = orch.Runner(
        "sub-codex",
        (sys.executable, str(script), "-f", "<profile>", "-D", "LANE=<lane>",
         "-D", "CODEX_HOME=<codex-home>", "-c", 'sqlite_home="<lane>"',
         "--output-schema", "<schema>", "-o", "<out>"),
        False, "local_cli", (), "", "openai", ())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-arrive")
    monkeypatch.setenv("CODEX_API_KEY", "sk-must-not-arrive-either")
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-codex", orch)
    assert rc == 0
    got = json.loads(seen.read_text())
    argv = got["argv"]
    assert not [a for a in argv if "<" in a and ">" in a], argv
    assert argv[argv.index("-f") + 1] == str(mod.CODEX_TESTER_PROFILE)
    assert argv[argv.index("-D") + 1] == f"LANE={got['cwd']}"
    assert f'sqlite_home="{got["cwd"]}"' in argv
    assert argv[argv.index("--output-schema") + 1] == os.path.join(got["cwd"], "schema.json")
    assert argv[argv.index("-o") + 1] == os.path.join(got["cwd"], "result.json")
    assert [a for a in argv if a.startswith("CODEX_HOME=")][0].endswith("/.codex")
    assert got["schema_in_cwd"] is True
    assert "lanes" not in got["cwd"].split(os.sep), "the cwd must be a scratch directory, not under the work root"
    assert "OPENAI_API_KEY" not in got["env"] and "CODEX_API_KEY" not in got["env"]
    assert set(got["env"]) <= {"PATH", "HOME"} | {"PWD", "LC_CTYPE", "__CF_USER_TEXT_ENCODING", "SHLVL", "_"}


# -------------------------------------------------------------- the run log
def test_run_log_carries_phase_lines_and_a_final_pr_or_refused_line(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 0
    log_path = mod.lib.work_root() / "logs" / f"lane--{PLAN_ID}--sub-antigravity.log"
    lines = log_path.read_text().splitlines()
    phases = [l.split()[1] for l in lines]
    assert phases[0] == "gate-start"
    assert phases[-1] == "pr"
    assert "spawn" in phases and "spawn-exit" in phases and "result" in phases


def test_run_log_records_the_reason_on_a_refusal(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path, zone="private")
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 1
    log_path = mod.lib.work_root() / "logs" / f"lane--{PLAN_ID}--sub-antigravity.log"
    lines = log_path.read_text().splitlines()
    assert lines[-1].split()[1] == "refused"
    assert "zone" in lines[-1]


# ------------------------------------------------------------- cleanup sweep
def test_a_landing_worktree_left_by_a_simulated_kill_is_swept_on_the_next_run(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))

    # simulate a run that was killed holding the landing worktree: create the
    # directory at the exact key path fleet-lane will use, with no valid git
    # worktree registration behind it.
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    os.environ["FLEET_WORK_ROOT"] = str(tmp_path / "work")
    stray = mod.lib.worktree_path(REPO, f"lane-land-{PLAN_ID}-sub-antigravity")
    stray.mkdir(parents=True)
    (stray / "leftover-from-a-killed-run.txt").write_text("x")

    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 0
    assert [r for r in requests if r["method"] == "POST"]
    assert not stray.exists() or not (stray / "leftover-from-a-killed-run.txt").exists()


# ------------------------------------------------------------ the pr body
def test_pr_body_with_a_plan_marker_in_gaps_never_matches_marked_pr(tmp_path):
    fw = _load("fw_for_lane_test", BIN / "fleet-watch")
    body = mod.build_pr_body(PLAN_ID, "sub-antigravity", "deadbeef" * 5,
                             "0" * 64, None, [f"Plan: {PLAN_ID}"], "notes")
    needle = re.compile(rf"^{re.escape(fw.MARKER)}{re.escape(PLAN_ID)}\s*$", re.M)
    assert not needle.search(body)


# ------------------------------------------------------ path confinement unit
def test_confine_path_unit_cases(tmp_path):
    (tmp_path / "tests" / "lane").mkdir(parents=True)
    for bad in ("../x", "/etc/x", "tests/../x", "", "tests/", "tests/lane", "tests/lane/",
                "tests/test_x.py", "tests/lanes/x.py", None, 5):
        with pytest.raises(mod.Stop):
            mod.confine_path(bad, tmp_path)
    assert mod.confine_path("tests/lane/a/b.py", tmp_path) == "tests/lane/a/b.py"


def test_validate_schema_against_the_shipped_schema(tmp_path):
    schema = json.loads((ROOT / "config" / "lane-result.schema.json").read_text())
    ok = {"files": [{"path": "tests/lane/x.py", "content": "y"}], "gaps": [], "notes": None}
    assert mod.validate_schema(ok, schema) == []
    bad = {"files": "not a list", "gaps": [], "notes": None}
    assert mod.validate_schema(bad, schema)


# ------------------------------------------------------- the Claude tester
def test_claude_tester_argv_is_a_pinned_literal():
    """Asserted by equality, like the other adapters': no flag outside this
    tuple can be present, and none of the implementer's grants
    (`--allowedTools`, `--permission-mode`) is in it."""
    assert mod.CLAUDE_TESTER_ARGV == (
        "claude", "-p", "--output-format", "json", "--model", "opus",
        "--tools", "", "--strict-mcp-config", "--setting-sources", "",
        "--disable-slash-commands", "--no-session-persistence")
    orch = _load_orch()
    assert mod.build_adapters(orch)["sub-opus"]["argv"] == mod.CLAUDE_TESTER_ARGV
    assert mod.CLAUDE_TESTER_ARGV != orch.RUNNERS["sub-opus"].argv[:len(mod.CLAUDE_TESTER_ARGV)]
    assert "--allowedTools" not in mod.CLAUDE_TESTER_ARGV
    assert "--permission-mode" not in mod.CLAUDE_TESTER_ARGV


@pytest.mark.parametrize("stdout,ok", [
    ('{"result": "{\\"files\\": [], \\"gaps\\": []}", "usage": {"output_tokens": 5}}', True),
    ('{"result": "```json\\n{\\"files\\": [], \\"gaps\\": []}\\n```"}', True),
    ('warning: something on stderr\n{"result": "{\\"files\\": []}"}\n', True),
    ('{"is_error": true, "result": "Not logged in"}', False),
    ('{"result": "I could not do that"}', False),
    ("not json at all", False),
])
def test_claude_parse_reads_the_result_field(stdout, ok):
    doc, _usage = mod.build_adapters(_load_orch())["sub-opus"]["parse"](stdout, "/nonexistent")
    assert (doc is not None) is ok


_FAKE_CLAUDE = '''\
#!{py}
import json, os, sys
prompt = sys.stdin.read()
json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": sorted(os.environ),
           "cwd_entries": sorted(os.listdir(".")), "prompt_len": len(prompt)}},
          open({capture!r}, "w"))
doc = {{"files": [{{"path": "tests/lane/test_generated.py", "content": "def test_x():\\n    pass\\n"}}],
       "gaps": [], "notes": "fake claude"}}
print(json.dumps({{"result": json.dumps(doc), "usage": {{"output_tokens": 1}}}}))
'''


def test_claude_lane_spawns_the_pinned_argv_from_an_empty_temp_dir_with_a_clean_env(
        tmp_path, monkeypatch):
    sha, ssh_base = _build_target_repo(tmp_path)
    capture = tmp_path / "capture.json"
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(_FAKE_CLAUDE.format(py=sys.executable, capture=str(capture)))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-be-passed")
    monkeypatch.setenv("FORGEJO_TOKEN", "must-not-be-passed")

    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-opus", _load_orch())

    assert rc == 0
    got = json.loads(capture.read_text())
    assert tuple(got["argv"]) == mod.CLAUDE_TESTER_ARGV[1:]
    assert got["prompt_len"] > 0
    # working directory: empty, system temp, and under no work root or repo
    assert got["cwd_entries"] == []
    work_root = mod.lib.work_root().resolve()
    assert work_root not in Path(got["cwd"]).parents
    assert not (Path(got["cwd"]) / "CLAUDE.md").exists()
    # environment: the allowlist, and no credential
    assert set(got["env"]) <= {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR",
                               "PWD", "SHLVL", "_", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert "ANTHROPIC_API_KEY" not in got["env"] and "FORGEJO_TOKEN" not in got["env"]


@pytest.mark.skipif(not os.environ.get("FLEET_LANE_LIVE_CLAUDE"),
                    reason="manual: needs a Claude login on this host; CI holds none "
                           "(set FLEET_LANE_LIVE_CLAUDE=1 on opshost)")
def test_live_claude_tester_cannot_read_a_file_and_the_control_can(tmp_path):
    """ADR-0006 §6, measured on the installed CLI rather than read off flag
    names. The pinned argv must fail to read a canary — outside its working
    directory AND inside it — and each control shows the canary was reachable:
    the same argv with `--tools ""` removed reads the inside one, and with
    `--allowedTools Read` added instead reads the outside one (default
    permissions alone also refuse an outside read, so removing the flag by
    itself is not the control for that case)."""
    exe = shutil.which("claude")
    assert exe, "claude is not on PATH"
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR")
           if k in os.environ}
    outside = tmp_path / "outside.txt"
    outside.write_text("CANARY-outside-7731\n")

    def ask(argv, path, cwd):
        prompt = (f"What are the contents of the file {path}? Reply with the exact "
                  "contents if you can read it, otherwise reply CANNOT READ.")
        r = subprocess.run(argv, input=prompt.encode(), cwd=str(cwd), env=env,
                           capture_output=True, timeout=120)
        return json.loads(r.stdout.decode())["result"]

    pinned = (exe,) + mod.CLAUDE_TESTER_ARGV[1:]
    without_tools = tuple(a for i, a in enumerate(pinned)
                          if not (a == "--tools" or (i and pinned[i - 1] == "--tools")))
    assert without_tools != pinned
    cwd = Path(tempfile.mkdtemp()).resolve()
    inside = cwd / "inside.txt"
    inside.write_text("CANARY-inside-7731\n")

    assert "CANARY" not in ask(pinned, outside, cwd)
    assert "CANARY" not in ask(pinned, inside, cwd)
    assert "CANARY-inside" in ask(without_tools, inside, cwd)
    assert "CANARY-outside" in ask(without_tools + ("--allowedTools", "Read"), outside, cwd)
    assert "CANARY-outside" not in ask(pinned + ("--allowedTools", "Read"), outside, cwd)


# ------------------------------------------------ plan 0100: cap and record
import sqlite3  # noqa: E402

QUOTA_TEXT = "You've hit your session limit · resets 3:10am (America/New_York)"
_FAKE_CLAUDE_SCRIPTED = '''\
#!{py}
import sys
sys.stdin.read()
with open({counter!r}, "a") as f:
    f.write("x")
sys.stdout.write({stdout!r})
sys.exit({rc})
'''


def _fake_claude(tmp_path, monkeypatch, stdout, rc=0):
    """A `claude` on PATH that prints `stdout`, exits `rc`, and appends one byte
    to a counter file per spawn. Returns the counter path."""
    counter = tmp_path / "spawns"
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(_FAKE_CLAUDE_SCRIPTED.format(
        py=sys.executable, counter=str(counter), stdout=stdout, rc=rc))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return counter


def _spawns(counter):
    return len(counter.read_text()) if counter.exists() else 0


def _good_claude_stdout(content="def test_x():\n    pass\n"):
    doc = {"files": [{"path": "tests/lane/test_generated.py", "content": content}],
           "gaps": [], "notes": "fake claude"}
    return json.dumps({
        "type": "result", "is_error": False, "result": json.dumps(doc),
        "usage": {"output_tokens": 51341, "cache_creation_input_tokens": 63823},
        "modelUsage": {"claude-opus-4-8": {"outputTokens": 51341},
                       "claude-haiku": {"outputTokens": 3}},
        "total_cost_usd": 1.5, "duration_ms": 435000, "num_turns": 1}) + "\n"


def _quota_stdout(status=429, text=QUOTA_TEXT):
    return json.dumps({"type": "result", "is_error": True, "api_error_status": status,
                       "result": text}) + "\n"


def _rows(tmp_path):
    db = tmp_path / "fleet" / "fleet.db"
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM lane_run ORDER BY started_at, run_id")]
    finally:
        con.close()


class _Pages:
    def __init__(self, orch):
        self.pages = []
        orch.notify = lambda title, body="": self.pages.append((title, body))


def _lane(tmp_path, sha, cap=DEFAULT_CAP, **kw):
    orch = _load_orch()
    pages = _Pages(orch)
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-opus", orch, cap=cap, **kw)
    return rc, pages.pages


def test_no_cap_row_refuses_before_spawn_and_names_the_fix(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    counter = _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    rc, pages = _lane(tmp_path, sha, cap=None)
    assert rc == 1
    assert _spawns(counter) == 0
    (row,) = _rows(tmp_path)
    assert (row["outcome"], row["spawned"]) == ("refused", 0)
    assert "fleet-config set lane_run_cap" in row["detail"]
    assert pages and pages[0][0].startswith("test lane refused")


@pytest.mark.parametrize("cap", ["sub-opus=0", "sub-antigravity=3"])
def test_a_paused_or_unnamed_runner_refuses(tmp_path, monkeypatch, cap):
    sha, _ = _build_target_repo(tmp_path)
    counter = _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    rc, _ = _lane(tmp_path, sha, cap=cap)
    assert rc == 1 and _spawns(counter) == 0
    (row,) = _rows(tmp_path)
    assert (row["outcome"], row["spawned"]) == ("refused", 0)


def test_an_invalid_cap_row_refuses(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    counter = _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    orch = _load_orch()
    _Pages(orch)
    with _stub_forge() as (_h, url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", url)
        _seed_cap("sub-opus=2")
        _seed_testers([(REPO, "sub-opus")])
        con = sqlite3.connect(tmp_path / "fleet" / "fleet.db")
        con.execute("UPDATE fleet_setting SET value='sub-opus=99' WHERE key='lane_run_cap'")
        con.commit()
        con.close()
        rc = mod.run(REPO, PLAN_ID, sha, "sub-opus", orch_mod=orch)
    assert rc == 1 and _spawns(counter) == 0


def test_an_unreadable_database_reads_as_no_cap(tmp_path):
    bad = tmp_path / "fleet.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    cfg = mod._config_module()
    cfg.DB = str(bad)
    assert mod._read_cap(cfg) is None


def test_the_cap_admits_n_runs_then_records_capped_and_exits_75(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    counter = _fake_claude(tmp_path, monkeypatch, _quota_stdout(), rc=1)
    codes = [_lane(tmp_path, sha, cap="sub-opus=2")[0] for _ in range(3)]
    assert codes == [75, 75, 75]
    assert _spawns(counter) == 2, "the third run must not spawn"
    rows = _rows(tmp_path)
    assert [r["outcome"] for r in rows] == ["quota", "quota", "capped"]
    assert [r["spawned"] for r in rows] == [1, 1, 0]
    assert rows[2]["detail"] == "2/2 runs in the last 24h"


def test_a_row_25_hours_old_does_not_count(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    counter = _fake_claude(tmp_path, monkeypatch, _quota_stdout(), rc=1)
    orch = _load_orch()
    _Pages(orch)
    with _stub_forge() as (_h, url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", url)
        Path(tmp_path / "fleet").mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(tmp_path / "fleet" / "fleet.db")
        con.executescript(mod.SCHEMA)
        for age, label in (("-25 hours", "old"), ("-1 hours", "recent")):
            con.execute(
                "INSERT INTO lane_run (run_id, repo, plan, runner, started_at, spawned, "
                "outcome) VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now',?),1,'pr-opened')",
                (label, REPO, "p", "sub-opus", age))
        con.commit()
        con.close()
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-opus", orch, cap="sub-opus=2")
    assert rc == 75 and _spawns(counter) == 1      # 1 recent < 2: admitted


def test_two_lanes_released_together_cannot_both_take_the_last_slot(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    src = tmp_path / "src"
    plan2 = PLAN_ID + "-two"
    text = (src / "plans" / f"{PLAN_ID}.md").read_text().replace(PLAN_ID, plan2)
    (src / "plans" / f"{plan2}.md").write_text(text)
    _git(["add", "."], src)
    _git(["commit", "-qm", "plan two"], src)
    sha2 = _git(["rev-parse", "HEAD"], src).stdout.strip()
    _git(["push", "-q", str(_bare_repo_path(tmp_path)), "HEAD:main"], src)
    counter = _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    barrier = threading.Barrier(2)
    results = {}

    with _stub_forge() as (_h, url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", url)
        _seed_cap("sub-opus=1")
        _seed_testers([(REPO, "sub-opus")])
        real_reserve = mod.LaneRecord.reserve

        def synced(self, *a, **k):
            barrier.wait(timeout=60)
            return real_reserve(self, *a, **k)
        monkeypatch.setattr(mod.LaneRecord, "reserve", synced)

        def go(plan):
            orch = _load_orch()
            _Pages(orch)
            results[plan] = mod.run(REPO, plan, sha2, "sub-opus", orch_mod=orch)
        threads = [threading.Thread(target=go, args=(p,)) for p in (PLAN_ID, plan2)]
        [t.start() for t in threads]
        [t.join(timeout=120) for t in threads]
    assert sorted(results.values()) == [0, 75], results
    assert sorted(r["outcome"] for r in _rows(tmp_path)) == ["capped", "pr-opened"]
    assert _spawns(counter) == 1


def test_a_successful_run_leaves_a_complete_row(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    rc, _ = _lane(tmp_path, sha)
    assert rc == 0
    (row,) = _rows(tmp_path)
    assert all(row[k] is not None for k in row), row
    assert re.fullmatch(r"[0-9a-f]{40}", row["sha"]) and row["sha"] == sha
    assert re.fullmatch(r"[0-9a-f]{64}", row["prompt_sha256"])
    lane_dir = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-opus"
    import hashlib
    prompt = (lane_dir / "prompt.txt").read_text(encoding="utf-8")
    assert row["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert row["prompt_bytes"] == len(prompt.encode())
    assert "[redacted" not in json.dumps({k: v for k, v in row.items() if k != "detail"})
    assert (row["outcome"], row["spawned"], row["rc"], row["pr"]) == ("pr-opened", 1, 0, 42)
    assert (row["repo"], row["plan"], row["runner"], row["family"]) == (
        REPO, PLAN_ID, "sub-opus", "claude")
    assert row["model"] == "claude-opus-4-8"
    # plan 0190: the served model (modelUsage's) differs from the registry's
    # launch-config pin, and both survive the mismatch, distinctly sourced.
    assert row["requested_model"] == "claude-opus-5-5"
    assert row["model_source"] == "cli-report"
    usage = json.loads(row["usage_json"])
    assert usage["usage"]["output_tokens"] == 51341 and usage["total_cost_usd"] == 1.5


def test_a_gate_refusal_leaves_one_refused_unspawned_row(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path, zone="private")
    counter = _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    rc, _ = _lane(tmp_path, sha)
    assert rc == 1 and _spawns(counter) == 0
    (row,) = _rows(tmp_path)
    assert (row["outcome"], row["spawned"], row["rc"]) == ("refused", 0, None)
    assert row["ended_at"] and "zone" in row["detail"]


def test_an_exception_after_the_reservation_still_ends_the_row(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    monkeypatch.setattr(mod, "check_result_files",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        _lane(tmp_path, sha)
    (row,) = _rows(tmp_path)
    assert row["ended_at"] and row["outcome"] != "running"
    assert "boom" in row["detail"]


def test_a_record_that_cannot_be_written_still_refuses_and_says_so(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path, zone="private")
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    monkeypatch.setattr(mod.LaneRecord, "_connect",
                        lambda self: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O")))
    rc, pages = _lane(tmp_path, sha)
    assert rc == 1
    assert "could not be written" in pages[0][1]


def test_the_claude_quota_record_ends_quota_exits_75_and_pages_separately(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _quota_stdout(), rc=1)
    rc, pages = _lane(tmp_path, sha, cap="sub-opus=4")
    assert rc == 75
    (row,) = _rows(tmp_path)
    assert row["outcome"] == "quota" and QUOTA_TEXT in row["detail"]
    assert len(pages) == 1
    title, body = pages[0]
    assert title == f"test lane stopped by vendor quota: {PLAN_ID} (sub-opus)"
    assert QUOTA_TEXT in body and "1/4 runs in the last 24h" in body
    assert "nothing will retry" in body
    assert not any(t.startswith("test lane refused") for t, _ in pages)


def test_a_non_quota_claude_error_ends_vendor_error(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _quota_stdout(500, "Internal server error"), rc=1)
    rc, _ = _lane(tmp_path, sha)
    assert rc == 1
    assert _rows(tmp_path)[0]["outcome"] == "vendor-error"


def test_a_good_reply_quoting_the_limit_sentence_is_never_a_quota(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout(
        f'def test_it():\n    assert "{QUOTA_TEXT}"\n'))
    rc, _ = _lane(tmp_path, sha)
    assert rc == 0
    assert _rows(tmp_path)[0]["outcome"] == "pr-opened"


def test_without_the_quota_function_the_claude_fixture_is_not_a_quota(tmp_path, monkeypatch):
    """The recogniser is what the quota tests depend on: remove it and the
    same fixture stops being a quota."""
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _quota_stdout(), rc=1)
    real = mod.build_adapters

    def without(orch):
        adapters = real(orch)
        adapters["sub-opus"] = {k: v for k, v in adapters["sub-opus"].items() if k != "quota"}
        return adapters
    monkeypatch.setattr(mod, "build_adapters", without)
    rc, _ = _lane(tmp_path, sha)
    assert rc == 1
    assert _rows(tmp_path)[0]["outcome"] != "quota"


def test_claude_quota_text_fallback_without_an_error_record():
    quota = mod.build_adapters(_load_orch())["sub-opus"]["quota"]
    assert quota(1, f"noise\n{QUOTA_TEXT}\n", "/x") == QUOTA_TEXT
    assert quota(0, f"noise\n{QUOTA_TEXT}\n", "/x") is None
    assert quota(1, "segfault", "/x") is None
    assert quota(1, _quota_stdout(500, "boom"), "/x") is None
    assert quota(1, _quota_stdout(500, "You've reached your monthly limit"), "/x")


def test_codex_and_antigravity_recognisers(tmp_path):
    adapters = mod.build_adapters(_load_orch())
    codex = adapters["sub-codex"]["quota"]
    text = ("You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits")
    assert codex(1, text, str(tmp_path / "none")) == text
    assert codex(1, "workspace_member_credits_depleted", str(tmp_path / "none"))
    assert codex(1, "error: model overloaded", str(tmp_path / "none")) is None
    ok = tmp_path / "out.json"
    ok.write_text("{}")
    assert codex(0, text, str(ok)) is None
    agy = adapters["sub-antigravity"]["quota"]
    assert agy(1, "You have exhausted your quota on this model.\n", "/x") == \
        "You have exhausted your quota on this model."
    # A bare RESOURCE_EXHAUSTED is NOT a quota (issue #662): measured in the
    # antigravity binary as a retry-status code, not the terminal sentence —
    # it is what a per-minute-retried 429 logs, and the retry can still
    # succeed or the process can still crash/time out afterwards.
    assert agy(1, "rpc error: RESOURCE_EXHAUSTED", "/x") is None
    assert agy(1, "panic: nil pointer", "/x") is None
    good = json.dumps({"event": "result", "result": {"response": '{"files": []}'}})
    assert agy(0, good + "\nRESOURCE_EXHAUSTED", "/x") is None


def test_the_codex_schema_requires_every_property_and_admits_nothing_new():
    """OpenAI refuses `--output-schema` unless every object's `required` lists
    all of its properties (measured 2026-10-13). The codex adapter's form of
    the schema does, at every depth, and a document it admits is one the
    schema as written admits."""
    schema = json.loads((ROOT / "config" / "lane-result.schema.json").read_text())
    text = mod.build_adapters(_load_orch())["sub-codex"]["schema_text"](json.dumps(schema))
    strict = json.loads(text)

    def objects(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    found = list(objects(strict))
    assert len(found) == 2
    for obj in found:
        assert sorted(obj["required"]) == sorted(obj["properties"])
    assert "executable" not in schema["properties"]["files"]["items"]["required"]
    doc = {"files": [{"path": "tests/lane/t.py", "content": "", "executable": False}],
           "gaps": [], "notes": None}
    assert mod.validate_schema(doc, strict) == [] and mod.validate_schema(doc, schema) == []
    assert mod.validate_schema({"files": [{"path": "p", "content": ""}], "gaps": [], "notes": ""},
                               strict) != []


def test_a_codex_run_logs_whether_it_rewrote_the_login_file(tmp_path, monkeypatch):
    """Plan 0101 §4 item 3: a lane run may refresh the owner's token, and the
    log says whether the login file changed. Never the real one here."""
    sha, _ = _build_target_repo(tmp_path)
    login = tmp_path / "auth.json"
    login.write_text("{}")
    monkeypatch.setattr(mod, "codex_login_file", lambda: login)
    for touch, expected in ((False, "login-file changed=False"), (True, "login-file changed=True")):
        def spawn(*a, **k):
            if touch:
                os.utime(login, ns=(1, 1))
            return 0, False, ""
        monkeypatch.setattr(mod, "spawn_lane", spawn)
        orch = _load_orch()
        orch.RUNNERS = dict(orch.RUNNERS)
        script = _write_fake_codex(tmp_path, {"files": [], "gaps": [], "notes": ""})
        orch.RUNNERS["sub-codex"] = orch.Runner(
            "sub-codex", (sys.executable, str(script), "-o", "<out>"),
            False, "local_cli", (), "", "openai", ())
        _run(tmp_path, REPO, PLAN_ID, sha, "sub-codex", orch)
        log = mod.lib.work_root() / "logs" / f"lane--{PLAN_ID}--sub-codex.log"
        assert [l for l in log.read_text().splitlines() if "login-file" in l][-1].endswith(expected)
        login.write_text("{}")


# --------------------------------------------- issue #662: timeout vs quota
def _run_with_patched_spawn(tmp_path, sha, runner_name, monkeypatch, rc, timed_out,
                            stdout_text, cap=DEFAULT_CAP):
    """Exercises `run()` end to end with `spawn_lane` itself replaced by a
    canned (rc, timed_out, stdout_text) — the outcome-classification logic
    after the spawn is what these tests are about, not a real (or even a
    real fake-vendor) process. The runner is still a wired one, registered
    the same way the other antigravity/codex tests register it, so the run
    reaches the classification code instead of being refused earlier as
    unwired."""
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    if runner_name == "sub-antigravity":
        orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
            orch, _write_fake_antigravity(tmp_path))
    elif runner_name == "sub-codex":
        script = _write_fake_codex(tmp_path, {"files": [], "gaps": [], "notes": ""})
        orch.RUNNERS["sub-codex"] = orch.Runner(
            "sub-codex", (sys.executable, str(script), "-o", "<out>"),
            False, "local_cli", (), "", "openai", ())
    else:
        raise AssertionError(runner_name)
    monkeypatch.setattr(mod, "spawn_lane", lambda *a, **k: (rc, timed_out, stdout_text))
    return _run(tmp_path, REPO, PLAN_ID, sha, runner_name, orch, cap=cap)


def test_a_timeout_with_resource_exhausted_in_the_stream_is_never_a_quota(tmp_path, monkeypatch):
    """issue #662 (PR #657 review M1): `timed_out` is decided BEFORE the
    quota recogniser runs. A run this program itself killed on
    SPAWN_TIMEOUT_SECONDS must be recorded `timeout`, whatever quota-shaped
    text its captured stream carries — antigravity retries per-minute 429s
    in-process, so a RESOURCE_EXHAUSTED warning can appear in a stream that
    then simply hangs until this program kills it."""
    sha, _ = _build_target_repo(tmp_path)
    stdout = "connecting...\nrpc error: RESOURCE_EXHAUSTED\n"
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-antigravity", monkeypatch, rc=-9, timed_out=True, stdout_text=stdout)
    assert rc == 1
    assert _rows(tmp_path)[0]["outcome"] == "timeout"


_AGY_DENIED = json.dumps({"event": "result", "result": {
    "status": "SUCCESS", "response": "", "num_turns": 1,
    "usage": {"input_tokens": 7, "output_tokens": 1},
    "denied_actions": [{"action": "command", "display_name": "RunCommand"}]}})


def test_an_antigravity_run_whose_every_turn_was_a_denied_tool_call_says_so(tmp_path, monkeypatch):
    """The first real run (2026-10-12): exit 0, one `result` line, an empty
    response and `denied_actions`. It is `bad-result`, the row names the tool
    and keeps the usage the CLI reported, and the stream is kept."""
    sha, _ = _build_target_repo(tmp_path)
    stdout = _AGY_DENIED + "\njetski: no output produced\n" + _AGY_DENIED + "\n"
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-antigravity", monkeypatch, rc=0, timed_out=False, stdout_text=stdout)
    assert rc == 1
    row = _rows(tmp_path)[0]
    assert row["outcome"] == "bad-result"
    assert "called a tool" in row["detail"] and "command" in row["detail"]
    assert json.loads(row["usage_json"]) == {"input_tokens": 7, "output_tokens": 1}
    kept = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-antigravity" / "stdout.redacted.txt"
    assert "jetski: no output produced" in kept.read_text()


def _agy_result(**fields):
    return json.dumps({"event": "result", "result": fields}) + "\n"


def test_an_antigravity_turn_that_only_talked_does_not_hide_the_turn_that_answered(tmp_path):
    """Gemini's review of #877: a first turn whose response is prose, not
    empty, must not be taken as the result. And the usage is the last turn's,
    which is the run's total, whichever turn held the document."""
    adapter = mod.build_adapters(_load_orch())["sub-antigravity"]
    doc = {"files": [], "gaps": [], "notes": "n"}
    talked = _agy_result(response="Let me look at the workspace first.", usage={"total_tokens": 5})
    answered = _agy_result(response=json.dumps(doc), usage={"total_tokens": 9})
    assert adapter["parse"](talked + answered, "/x") == (doc, {"total_tokens": 9})
    assert adapter["parse"](answered + answered.replace("9", "20"), "/x") == (doc, {"total_tokens": 20})
    assert adapter["usage"](answered + answered.replace("9", "20")) == ({"total_tokens": 20}, None)


def test_antigravity_tool_metadata_on_the_only_copy_is_dropped(tmp_path):
    """2026-10-16: the model answered on its first turn and the only document
    carried `toolAction` and `toolSummary`, so a valid result was refused at
    schema validation. Those two keys are the CLI's, and are dropped."""
    adapter = mod.build_adapters(_load_orch())["sub-antigravity"]
    doc = {"files": [], "gaps": ["g"], "notes": "n"}
    tagged = dict(doc, toolAction="x", toolSummary="y")
    parsed, _ = adapter["parse"](_agy_result(response=json.dumps(tagged)), "/x")
    assert parsed == doc


def test_any_other_unknown_antigravity_key_still_reaches_validation(tmp_path):
    adapter = mod.build_adapters(_load_orch())["sub-antigravity"]
    doc = {"files": [], "gaps": [], "notes": "n", "toolAction": "x", "extra": 1}
    parsed, _ = adapter["parse"](_agy_result(response=json.dumps(doc)), "/x")
    assert parsed == {"files": [], "gaps": [], "notes": "n", "extra": 1}
    schema = json.loads((ROOT / "config" / "lane-result.schema.json").read_text())
    assert mod.validate_schema(parsed, schema) != []


def test_a_denial_in_an_earlier_antigravity_turn_is_still_named(tmp_path):
    adapter = mod.build_adapters(_load_orch())["sub-antigravity"]
    stream = _AGY_DENIED + "\n" + _agy_result(response="")
    assert "command" in adapter["no_document"](stream)
    assert adapter["no_document"](_agy_result(response="")) is None


def test_an_antigravity_response_that_is_not_a_document_is_a_plain_bad_result(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    for response in ("I could not do that.", "[1, 2]"):
        stdout = json.dumps({"event": "result", "result": {"response": response}}) + "\n"
        rc, _ = _run_with_patched_spawn(
            tmp_path, sha, "sub-antigravity", monkeypatch, rc=0, timed_out=False, stdout_text=stdout)
        assert rc == 1
        row = _rows(tmp_path)[-1]
        assert row["outcome"] == "bad-result"
        assert row["detail"] == "the vendor's result did not parse into a document"


def test_a_crashed_antigravity_run_after_a_retried_warning_is_not_a_quota(tmp_path, monkeypatch):
    """A RESOURCE_EXHAUSTED warning followed by a crash with no `result`
    event (not a timeout — the process exited on its own, non-zero) must not
    be recorded `quota`. `RESOURCE_EXHAUSTED` alone is a retry-status code,
    not the CLI's terminal word — see the comment above
    `_ANTIGRAVITY_QUOTA_TEXTS` in bin/fleet-lane."""
    sha, _ = _build_target_repo(tmp_path)
    stdout = "rpc error: RESOURCE_EXHAUSTED\npanic: nil pointer dereference\n"
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-antigravity", monkeypatch, rc=2, timed_out=False, stdout_text=stdout)
    assert rc == 1
    assert _rows(tmp_path)[0]["outcome"] != "quota"


def test_model_text_quoting_the_quota_sentence_in_an_assistant_event_is_not_a_quota(
        tmp_path, monkeypatch):
    """The quota sentence appearing inside an `assistant` event's `message`
    content is model-authored text, not a CLI failure signal — it must never
    be matched, even as the stream's own last line."""
    sha, _ = _build_target_repo(tmp_path)
    assistant_line = json.dumps({
        "event": "assistant",
        "message": {"content": "You have exhausted your quota on this model."}})
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-antigravity", monkeypatch, rc=1, timed_out=False,
        stdout_text=assistant_line + "\n")
    assert rc == 1
    assert _rows(tmp_path)[0]["outcome"] != "quota"


def test_a_genuine_terminal_antigravity_quota_error_is_quota(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    stdout = "connecting...\nYou have exhausted your quota on this model.\n"
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-antigravity", monkeypatch, rc=1, timed_out=False, stdout_text=stdout)
    assert rc == 75
    assert _rows(tmp_path)[0]["outcome"] == "quota"


def test_a_codex_timeout_is_never_a_quota(tmp_path, monkeypatch):
    """The codex equivalent of the antigravity timeout case above."""
    sha, _ = _build_target_repo(tmp_path)
    stdout = "usage_limit_reached\n"
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-codex", monkeypatch, rc=-9, timed_out=True, stdout_text=stdout)
    assert rc == 1
    assert _rows(tmp_path)[0]["outcome"] == "timeout"


def test_a_crashed_codex_run_after_a_credits_depleted_line_and_more_output_is_not_a_quota(
        tmp_path, monkeypatch):
    """The codex text matches only the stream's own LAST non-empty line: a
    quota-shaped line superseded by further output is not the run's
    outcome."""
    sha, _ = _build_target_repo(tmp_path)
    stdout = "workspace_member_credits_depleted\nunexpected panic, exiting\n"
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-codex", monkeypatch, rc=2, timed_out=False, stdout_text=stdout)
    assert rc == 1
    assert _rows(tmp_path)[0]["outcome"] != "quota"


def test_model_text_quoting_codexs_quota_phrase_is_not_a_quota(tmp_path, monkeypatch):
    """The codex equivalent of the antigravity assistant-content case above:
    a JSON agent-message line quoting the phrase is model-authored content,
    never matched, even as the stream's own last line."""
    sha, _ = _build_target_repo(tmp_path)
    agent_line = json.dumps({"type": "agent_message",
                             "message": "usage_limit_reached, the user asked about"})
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-codex", monkeypatch, rc=1, timed_out=False,
        stdout_text=agent_line + "\n")
    assert rc == 1
    assert _rows(tmp_path)[0]["outcome"] != "quota"


def test_a_genuine_terminal_codex_quota_error_is_quota(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    stdout = ("connecting...\nYou've hit your usage limit. Visit "
             "https://chatgpt.com/codex/settings/usage to purchase more credits\n")
    rc, _ = _run_with_patched_spawn(
        tmp_path, sha, "sub-codex", monkeypatch, rc=1, timed_out=False, stdout_text=stdout)
    assert rc == 75
    assert _rows(tmp_path)[0]["outcome"] == "quota"


def test_usage_json_is_capped_at_8_kib_without_cutting_a_string():
    assert json.loads(mod.usage_json({"b": 1, "a": 2})) == {"a": 2, "b": 1}
    assert mod.usage_json(None) is None
    big = json.loads(mod.usage_json({"blob": "x" * 20000}))
    assert big["truncated"] is True and big["bytes"] > 8192


def test_dry_run_prints_the_cap_line_and_writes_no_row(tmp_path, capsys):
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch, dry_run=True,
                 cap="sub-antigravity=2")
    assert rc == 0
    assert "cap: sub-antigravity 0/2 in the last 24h" in capsys.readouterr().out
    assert not (tmp_path / "fleet" / "fleet.db").exists() or _rows_or_none(tmp_path) in (None, [])
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch, dry_run=True, cap=None)
    assert rc == 0


def _rows_or_none(tmp_path):
    try:
        return _rows(tmp_path)
    except sqlite3.OperationalError:
        return None


def test_the_two_documented_queries_run(tmp_path):
    con = sqlite3.connect(":memory:")
    con.executescript(mod.SCHEMA)
    con.execute("INSERT INTO lane_run (run_id, repo, plan, runner, started_at, spawned, outcome,"
                " prompt_sha256, pr, usage_json) VALUES ('a','r','p1','sub-opus',"
                "strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,'pr-opened','h',7,"
                "'{\"usage\":{\"output_tokens\":51341}}')")
    con.execute("INSERT INTO lane_run (run_id, repo, plan, runner, started_at, spawned, outcome)"
                " VALUES ('b','r','p1','sub-opus',strftime('%Y-%m-%dT%H:%M:%SZ','now'),0,'capped')")
    doc = (ROOT / "docs" / "test-lane.md").read_text()
    queries = re.findall(r"```sql\n(.*?)```", doc, re.S)
    per_runner = next(q for q in queries if "GROUP BY runner" in q)
    per_plan = next(q for q in queries if "WHERE plan = ?" in q)
    assert con.execute(per_runner).fetchall() == [("sub-opus", 1)]
    assert con.execute(per_plan, ("p1",)).fetchall() == [
        ("sub-opus", "pr-opened", 7, "h", 51341), ("sub-opus", "capped", None, None, None)]


# ============================================================ plan 0102 (D6)
HEX40 = "0123456789abcdef" * 2 + "01234567"
HEX64 = "0123456789abcdef" * 4


def _log_text(plan=PLAN_ID, runner="sub-antigravity"):
    return (mod.lib.work_root() / "logs" / f"lane--{plan}--{runner}.log").read_text()


def _sha_fields_whole(text, sha):
    return f"sha={sha} " in text and re.search(r"sha256=[0-9a-f]{64}\b", text) is not None


def _antigravity_run(tmp_path, **kw):
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _fake_antigravity_runner(
        orch, _write_fake_antigravity(tmp_path))
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch, **kw)
    return sha, rc, requests


def test_the_run_log_writes_sha_and_prompt_hash_whole(tmp_path):
    sha, rc, _ = _antigravity_run(tmp_path)
    assert rc == 0
    text = _log_text()
    assert _sha_fields_whole(text, sha), text
    assert "redacted:long-hex" not in text.split(" spawn ")[0]


def test_the_control_whole_line_redaction_scrubs_the_sha(tmp_path, monkeypatch):
    """The control for the test above: the old `note`, which redacted the
    joined line, fails the very same assertion."""
    def whole_line(self, phase, **facts):
        parts = ["t", phase] + [f"{k}={v}" for k, v in facts.items()]
        clean, _ = self._orch.redact_transcript(" ".join(str(x) for x in parts))
        self._fh.write(clean + "\n")
        self._fh.flush()
    monkeypatch.setattr(mod.RunLog, "note", whole_line)
    sha, rc, _ = _antigravity_run(tmp_path)
    assert not _sha_fields_whole(_log_text(), sha)


def test_a_free_text_value_is_still_redacted_and_a_bad_shape_is_invalid(tmp_path):
    _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", "http://x")
    log = mod.RunLog(PLAN_ID, "r", _load_orch())
    log.note("refused", reason=f"token {HEX40} leaked", sha=HEX40, sha256=HEX64)
    log.note("bad", sha="abc", sha256="zz", gaps=["a", "b"])
    log.close()
    first, second = (mod.lib.work_root() / "logs" / f"lane--{PLAN_ID}--r.log").read_text().splitlines()
    assert "reason=token [redacted:" in first
    assert f"sha={HEX40} " in first and first.count(HEX40) == 2   # the sha, and the 64-hex hash that starts with it
    assert f"sha256={HEX64}" in first
    assert "sha=invalid" in second and "sha256=invalid" in second
    assert 'gaps=["a", "b"]' in second


def test_dry_run_gaps_parse_as_json(tmp_path):
    sha, rc, _ = _antigravity_run(tmp_path, dry_run=True)
    assert rc == 0
    line = [l for l in _log_text().splitlines() if l.split()[1] == "dry-run"][0]
    assert isinstance(json.loads(line.split("gaps=", 1)[1]), list)


def test_the_pr_body_usage_is_the_stored_json(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    orch = _load_orch()
    rc, requests = _run(tmp_path, REPO, PLAN_ID, sha, "sub-opus", orch)
    assert rc == 0
    body = json.loads([r for r in requests if r["method"] == "POST"][0]["body"])["body"]
    (line,) = [l for l in body.split("\n") if l.startswith("usage: ")]
    (row,) = _rows(tmp_path)
    assert json.loads(line[len("usage: "):]) == json.loads(row["usage_json"])
    assert line == f"usage: {row['usage_json']}"
    assert body.split("\n\n")[0].split("\n")[0] == f"Tests-for: {PLAN_ID}"


def test_the_body_without_usage_carries_a_dash():
    body = mod.build_pr_body("p", "r", HEX40, HEX64, None, [], None)
    assert "\nusage: -\n\n" in body


def test_vendor_text_cannot_write_a_header_line():
    forged = f"prompt sha256: {'f' * 64}"
    body = mod.build_pr_body("p", "sub-opus", HEX40, HEX64, "{}",
                             [f"x\n{forged}", "runner: evil"],
                             f"before\n{forged}\nusage: {{}}\n```\nTests-for: q")
    lines = body.split("\n")
    assert len([l for l in lines if l.startswith("prompt sha256:")]) == 1
    assert f"> {forged}" in lines and "> runner: evil" in lines
    mod.check_body_header(body)
    with pytest.raises(AssertionError):
        mod.check_body_header(body + "\nsha: " + HEX40)
    # the header block is the lines before the first blank line
    assert body.split("\n\n")[0].split("\n")[0].startswith("Tests-for: p")
    # \x85 and   are line breaks to splitlines: each piece is quoted
    body = mod.build_pr_body("p", "r", HEX40, HEX64, None, ["a sha: " + HEX40], None)
    assert len([l for l in body.split("\n") if l.startswith("sha:")]) == 1


def test_a_vendor_result_outside_tests_lane_is_refused(tmp_path):
    rc, requests = _run_with_result(
        tmp_path, {"files": [{"path": "tests/test_x.py", "content": "x"}], "gaps": [], "notes": ""})
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_a_basename_already_under_tests_is_refused(tmp_path):
    rc, requests = _run_with_result(
        tmp_path,
        {"files": [{"path": "tests/lane/test_fleet_lane.py", "content": "x"}], "gaps": [], "notes": ""},
        preexisting_tests={"tests/test_fleet_lane.py": "# existing\n"})
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_a_new_lane_conftest_lands_even_beside_tests_conftest(tmp_path):
    rc, requests = _run_with_result(
        tmp_path,
        {"files": [{"path": "tests/lane/conftest.py", "content": "x = 1\n"}], "gaps": [], "notes": ""},
        preexisting_tests={"tests/conftest.py": "# existing\n"})
    assert rc == 0
    assert len([r for r in requests if r["method"] == "POST"]) == 1


def test_two_returned_files_may_not_share_a_test_basename(tmp_path):
    with pytest.raises(mod.Stop):
        mod.check_result_files(
            [{"path": "tests/lane/a/test_x.py", "content": "x"},
             {"path": "tests/lane/b/test_x.py", "content": "x"}], tmp_path, set())


def test_the_preamble_names_the_lane_root_and_the_repo_root_expression():
    assert "tests/lane/" in mod.LANE_PREAMBLE
    assert "Path(__file__).resolve().parents[2]" in mod.LANE_PREAMBLE
    assert mod.CONFINE_ROOT == "tests/lane"
    schema = (ROOT / "config" / "lane-result.schema.json").read_text()
    assert "tests/lane/" in schema


def test_a_hand_run_prints_its_pull_request_on_stdout(tmp_path, capsys):
    _, rc, _ = _antigravity_run(tmp_path)
    assert rc == 0
    assert f"pr: {REPO}#42" in capsys.readouterr().out.splitlines()


def test_validate_schema_enum_pattern_and_integer():
    schema = {"type": "object", "properties": {
        "o": {"enum": ["a", "b"]}, "h": {"type": "string", "pattern": "^[0-9a-f]{4}$"},
        "n": {"type": "integer"}, "x": {"type": ["string", "null"], "pattern": "^a$"}}}
    assert mod.validate_schema({"o": "a", "h": "beef", "n": 3, "x": None}, schema) == []
    assert len(mod.validate_schema({"o": "c", "h": "xyz", "n": 3.5}, schema)) == 3
    assert mod.validate_schema({"n": True}, schema)
    assert mod.validate_schema({"o": True}, {"properties": {"o": {"enum": [1]}}})


# ============================================================ plan 0104 (D5)
def _push_feature_branch(tmp_path, sentinel="SENTINEL-IMPLEMENTATION-TEXT"):
    """Push `feat/0070-x`, carrying a sentinel file and string, to the fixture's
    bare origin. Returns the feat commit's sha."""
    work = tmp_path / "pusher"
    bare = _bare_repo_path(tmp_path)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    _git(["checkout", "-q", "-b", "feat/0070-x"], work)
    (work / "sentinel_impl.py").write_text(f"# {sentinel}\n")
    _git(["add", "."], work)
    _git(["commit", "-qm", "the implementation"], work)
    _git(["push", "-q", "origin", "feat/0070-x"], work)
    return _git(["rev-parse", "HEAD"], work).stdout.strip()


def _refs(store):
    r = subprocess.run(["git", "for-each-ref", "--format=%(refname)"], cwd=str(store),
                       capture_output=True, text=True, check=True)
    return r.stdout.split()


def test_the_reader_store_holds_no_implementation_ref_and_the_shared_store_does(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    orch = _load_orch()
    _Pages(orch)

    def prompt_sha():
        rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-opus", orch, dry_run=True, cap=DEFAULT_CAP)
        assert rc == 0
        text = (mod.lib.work_root() / "lanes" / PLAN_ID / "sub-opus" / "prompt.txt").read_text()
        return hashlib.sha256(text.encode()).hexdigest(), text

    before, _ = prompt_sha()
    feat = _push_feature_branch(tmp_path)
    after, text = prompt_sha()

    store = mod.lane_store_path(REPO)
    assert _refs(store) == ["refs/remotes/origin/main"], "only main, nothing else"
    assert subprocess.run(["git", "cat-file", "-e", feat], cwd=str(store),
                          capture_output=True).returncode != 0
    assert "SENTINEL-IMPLEMENTATION-TEXT" not in text and "sentinel_impl" not in text
    assert after == before, "the prompt is the same bytes before and after feat/ existed"

    # CONTROL: the shared store, the one the old reader attached to, DOES hold it.
    # Without this the assertions above prove nothing about the construction.
    shared = mod.lib.ensure_store(REPO)
    assert "refs/remotes/origin/feat/0070-x" in _refs(shared)
    assert subprocess.run(["git", "cat-file", "-e", feat], cwd=str(shared),
                          capture_output=True).returncode == 0


def test_a_sha_that_is_not_on_main_is_refused_by_the_reader_store(tmp_path):
    sha, _ = _build_target_repo(tmp_path)
    feat = _push_feature_branch(tmp_path)
    _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", "http://127.0.0.1:1")
    with pytest.raises(mod.Stop):
        mod.ensure_lane_store(REPO, feat)


def _assign_lane_lease(tmp_path):
    holder = "lane-0070-sub-opus-abcd1234"
    os.environ["EUNOMIA_SESSION"] = holder
    res = json.dumps({"type": "branch", "repo": REPO,
                      "branch": f"agent/tests/{PLAN_ID}/sub-opus"})
    r = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--assign", res, "--holder",
                        holder, "--note", f"lane:{PLAN_ID}/sub-opus", "--ttl", "30"],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[0]


def _lease_state(lease_id):
    p = Path(os.environ["EUNOMIA_FLEET_DIR"]) / "leases" / f"{lease_id}.json"
    return json.loads(p.read_text())["state"]


def _lane_ended(tmp_path):
    p = tmp_path / "fleet" / "events.jsonl"
    evs = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in evs if e["type"] == "lane-ended"]


def _leased_run(tmp_path, sha, monkeypatch, cap=DEFAULT_CAP, seed=True):
    orch = _load_orch()
    _Pages(orch)
    with _stub_forge() as (httpd, base_url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", base_url)
        if seed:
            _seed_cap(cap)
            _seed_testers([(REPO, "sub-opus")])
        lease = _assign_lane_lease(tmp_path)
        seen = []
        real_spawn = mod.spawn_lane

        def spawn(*a, **k):
            seen.append(_lease_state(lease))     # what the lease is DURING the run
            return real_spawn(*a, **k)
        monkeypatch.setattr(mod, "spawn_lane", spawn)
        rc = mod.run_leased(REPO, PLAN_ID, sha, "sub-opus", lease, orch_mod=orch)
    return rc, lease, seen


def test_a_capped_leased_lane_ends_capped_not_refused(tmp_path, monkeypatch):
    """2026-10-16, plan 0138's sub-codex lane: the row said `capped` and
    lane-ended said `refused`, because a row inserted already-ended never set
    the outcome that run_leased reports."""
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    rc, _lease, _seen = _leased_run(tmp_path, sha, monkeypatch, cap="sub-opus=1")
    assert rc == 0                                # the one run the cap admits
    rc, _lease, seen = _leased_run(tmp_path, sha, monkeypatch, cap="sub-opus=1",
                                   seed=False)
    assert rc == 75 and seen == [], "the capped run must not spawn"
    assert [e["detail"]["outcome"] for e in _lane_ended(tmp_path)] == ["pr-opened", "capped"]
    assert _rows(tmp_path)[-1]["outcome"] == "capped"


def test_a_leased_lane_is_active_during_the_run_and_released_after_success(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    rc, lease, seen = _leased_run(tmp_path, sha, monkeypatch)
    assert rc == 0 and seen == ["active"]
    assert _lease_state(lease) == "released"
    (ended,) = _lane_ended(tmp_path)
    (row,) = _rows(tmp_path)
    assert ended["detail"]["outcome"] == row["outcome"] == "pr-opened"
    assert ended["detail"]["pr"] == 42 and ended["detail"]["lease"] == lease
    assert ended["detail"]["sha"] == sha and ended["detail"]["runner"] == "sub-opus"
    # SPEC: lane-ended carries the lane's lease on the event, not only in detail
    # (review 4569 MEDIUM: it was emitted with lease=None).
    assert ended["lease"] == lease


def test_a_gate_refusal_releases_the_lease_and_ends_with_the_recorded_outcome(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path, zone="private")
    rc, lease, _seen = _leased_run(tmp_path, sha, monkeypatch)
    assert rc == 1 and _lease_state(lease) == "released"
    (ended,) = _lane_ended(tmp_path)
    (row,) = _rows(tmp_path)
    assert ended["detail"]["outcome"] == row["outcome"] == "refused"
    assert ended["detail"]["pr"] is None


@pytest.mark.parametrize("stdout,outcome", [
    (None, "vendor-error"),
])
def test_a_vendor_failure_releases_the_lease(tmp_path, monkeypatch, stdout, outcome):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, "boom\n", rc=1)
    rc, lease, _ = _leased_run(tmp_path, sha, monkeypatch)
    assert rc == 1 and _lease_state(lease) == "released"
    (ended,) = _lane_ended(tmp_path)
    (row,) = _rows(tmp_path)
    assert ended["detail"]["outcome"] == row["outcome"] == outcome


def test_a_timeout_releases_the_lease_and_ends_timeout(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, "")
    monkeypatch.setattr(mod, "spawn_lane", lambda *a, **k: (-9, True, ""))
    orch = _load_orch()
    _Pages(orch)
    with _stub_forge() as (_h, base_url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", base_url)
        _seed_cap(DEFAULT_CAP)
        _seed_testers([(REPO, "sub-opus")])
        lease = _assign_lane_lease(tmp_path)
        rc = mod.run_leased(REPO, PLAN_ID, sha, "sub-opus", lease, orch_mod=orch)
    assert rc == 1 and _lease_state(lease) == "released"
    (ended,) = _lane_ended(tmp_path)
    (row,) = _rows(tmp_path)
    assert ended["detail"]["outcome"] == row["outcome"] == "timeout"


def test_a_sigkilled_lane_leaves_the_lease_active_and_it_derives_orphaned(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    orch = _load_orch()
    _Pages(orch)
    with _stub_forge() as (_h, base_url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", base_url)
        _seed_cap(DEFAULT_CAP)
        _seed_testers([(REPO, "sub-opus")])
        lease = _assign_lane_lease(tmp_path)
        child = (
            "import importlib.machinery, importlib.util, os, sys\n"
            f"l = importlib.machinery.SourceFileLoader('fl', {str(BIN / 'fleet-lane')!r})\n"
            "m = importlib.util.module_from_spec(importlib.util.spec_from_loader('fl', l))\n"
            "l.exec_module(m)\n"
            "m.spawn_lane = lambda *a, **k: os.kill(os.getpid(), 9)\n"
            f"m.run_leased({REPO!r}, {PLAN_ID!r}, {sha!r}, 'sub-opus', {lease!r})\n")
        # a real process, not a fork: forking a threaded pytest crashed on macOS
        r = subprocess.run([sys.executable, "-c", child], env=dict(os.environ),
                           capture_output=True, text=True, timeout=120)
        status = r.returncode
    assert status == -9, (status, r.stderr[-400:])
    assert _lease_state(lease) == "active", "nothing released it"
    assert _lane_ended(tmp_path) == []
    rec = json.loads((Path(os.environ["EUNOMIA_FLEET_DIR"]) / "leases" / f"{lease}.json").read_text())
    assert not mod.lib.is_orphaned(rec)
    later = datetime.now(timezone.utc) + timedelta(minutes=int(rec["ttl_minutes"]) + 5)
    assert mod.lib.is_orphaned(rec, at=later)


# ------------------------------------------- gate 7: the target's CI (plan 0137)
LANE_BRANCH = f"agent/tests/{PLAN_ID}/sub-opus"

# The `on:` blocks of the four fleet repositories' workflows on main, 2026-10-15.
WF_TALOS = {".forgejo/workflows/tests.yml": "name: Tests\non:\n  push:\n\njobs:\n  tests:\n    runs-on: cihost\n"}
WF_LYNCEUS = {
    ".forgejo/workflows/ci.yml": "name: CI\n# a comment\non:\n  push:\n\nconcurrency:\n  group: x\n",
    ".forgejo/workflows/plans.yml": "name: Plans\non:\n  push:\n\njobs: {}\n",
}
WF_ARES = {".forgejo/workflows/ci.yml":
           "name: CI\non:\n  pull_request:\n    branches: [main]\n  push:\n    branches: [main]\n\njobs: {}\n"}
WF_EUNOMIA = {
    ".forgejo/workflows/ci.yml":
        "name: CI\non:\n  push:\n    paths-ignore:\n      - 'plans/**'\n    # why\n"
        "    branches-ignore:\n      - 'agent/tests/**'\n\njobs: {}\n",
    ".forgejo/workflows/plans.yml":
        "name: Plans\non:\n  push:\n    paths:\n      - 'plans/**'\n"
        "    branches-ignore:\n      - 'agent/tests/**'\n\njobs: {}\n",
    ".forgejo/workflows/deploy-docsite.yml":
        "name: Deploy\non:\n  workflow_dispatch:\n  push:\n    branches: [main]\n",
}


def _runs(text):
    return mod.workflow_runs_lane(text, LANE_BRANCH)


def _gate7(tmp_path, workflows, monkeypatch):
    sha, _ = _build_target_repo(tmp_path, workflows=workflows)
    counter = _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    rc, pages = _lane(tmp_path, sha)
    return rc, pages, counter


def test_gate7_refuses_talos_lynceus_and_ares_and_admits_eunomia(tmp_path, monkeypatch):
    for n, (wf, names) in enumerate([
            (WF_TALOS, ["tests.yml", "push"]), (WF_LYNCEUS, ["ci.yml", "push"]),
            (WF_ARES, ["ci.yml", "pull_request"])]):
        d = tmp_path / str(n)
        d.mkdir()
        rc, pages, counter = _gate7(d, wf, monkeypatch)
        (row,) = _rows(d)
        assert rc == 1 and _spawns(counter) == 0
        assert all(x in row["detail"] for x in names), row["detail"]
        assert pages and "refused" in pages[0][0]
    d = tmp_path / "eunomia"
    d.mkdir()
    rc, _, counter = _gate7(d, WF_EUNOMIA, monkeypatch)
    assert rc == 0 and _spawns(counter) == 1


def test_gate7_refuses_before_the_cap_is_reserved(tmp_path, monkeypatch):
    rc, _, counter = _gate7(tmp_path, WF_TALOS, monkeypatch)
    (row,) = _rows(tmp_path)
    assert (row["outcome"], row["spawned"]) == ("refused", 0)
    assert "tests.yml runs on push to agent/tests/" in row["detail"]
    assert _spawns(counter) == 0


def test_gate7_falls_back_to_github_workflows_only_without_forgejo_ones(tmp_path, monkeypatch):
    wf = {".github/workflows/t.yml": "on: push\n"}
    rc, _, _ = _gate7(tmp_path, wf, monkeypatch)
    assert rc == 1
    d = tmp_path / "b"
    d.mkdir()
    wf = dict(WF_EUNOMIA, **{".github/workflows/t.yml": "on: push\n"})
    rc, _, _ = _gate7(d, wf, monkeypatch)
    assert rc == 0


@pytest.mark.parametrize("text, runs", [
    ("on:\n  push:\n    branches: ['main']\n", False),
    ("on:\n  push:\n    branches:\n      - main\n      - 'agent/**'\n", True),
    ("on:\n  push:\n    branches-ignore: ['agent/**']\n", False),
    ("on:\n  push:\n    branches-ignore: ['agent/*']\n", True),
    ("on:\n  push:\n    branches-ignore:\n    - 'agent/tests/**'\n", False),
    ("on: push\n", True),
    ("on: [push, pull_request]\n", True),
    ("on: [workflow_dispatch, schedule]\n", False),
    ("on:\n  push:\n    paths: ['docs/**']\n", True),
    ("on:\n  pull_request_target:\n    branches: [main]\n", True),
    ("on:\n  pull_request:\n    branches: [release]\n", False),
    ("on: {push: {branches: [main]}}\n", False),
    ('"on":\n  push:\n', True),
    ("---\non:\n  push:\n    branches: [\n      'main',\n    ]\n", False),
])
def test_gate7_filter_semantics(text, runs):
    assert bool(_runs(text)) is runs


@pytest.mark.parametrize("text", [
    "on: &t\n  push:\n",
    "on:\n  push: *t\n",
    "on:\n  push:\n    branches: |\n      main\n",
    "on: !!str push\n",
    "name: no triggers\n",
    "on: push\n---\non: push\n",
    "on:\n  push:\n    branches: ['a[bc]']\n",
    "on:\n  push:\n    branches: [a]\n    branches-ignore: [b]\n",
])
def test_gate7_fails_closed_on_what_it_does_not_read(text):
    with pytest.raises(mod._Unsupported):
        _runs(text)


def test_gate7_refuses_an_anchor_and_an_unreadable_file_in_a_real_run(tmp_path, monkeypatch):
    rc, _, counter = _gate7(tmp_path, {".forgejo/workflows/a.yml": "on: &x push\n"}, monkeypatch)
    (row,) = _rows(tmp_path)
    assert rc == 1 and "could not be evaluated" in row["detail"] and _spawns(counter) == 0
    d = tmp_path / "bin"
    d.mkdir()
    rc, _, _ = _gate7(d, {".forgejo/workflows/a.yml": b"\xff\xfe on: [push\n"}, monkeypatch)
    assert rc == 1 and _rows(d)[0]["outcome"] == "refused"


def test_a_result_with_no_files_ends_no_tests_and_opens_no_pull_request(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    doc = {"files": [], "gaps": ["gap-a", "gap-b"], "notes": ""}
    out = json.dumps({"type": "result", "is_error": False, "result": json.dumps(doc),
                      "usage": {}, "modelUsage": {}}) + "\n"
    counter = _fake_claude(tmp_path, monkeypatch, out)
    orch = _load_orch()
    pages = _Pages(orch)
    with _stub_forge() as (httpd, base_url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", base_url)
        _seed_cap(DEFAULT_CAP)
        _seed_testers([(REPO, "sub-opus")])
        rc = mod.run(REPO, PLAN_ID, sha, "sub-opus", orch_mod=orch)
        requests = httpd.requests
    assert rc == 1 and _spawns(counter) == 1
    (row,) = _rows(tmp_path)
    assert row["outcome"] == "no-tests" and row["pr"] is None
    assert "2 gaps" in row["detail"] and "gap-a" in row["detail"] and "gap-b" in row["detail"]
    assert not any(r[0] == "POST" for r in requests)
    assert [t for t, _ in pages.pages] == [f"test lane wrote no tests: {PLAN_ID} (sub-opus), 2 gaps"]
    assert not list((tmp_path / "work").glob("**/lane-land-*"))
    assert "no-tests" in mod.OUTCOMES


# ------------------------------------------- plan 0141: the prompt's contents
def _prompt_worktree(tmp_path, adrs=("0006-blackbox.md",)):
    wt = tmp_path / "wt"
    (wt / "docs" / "adr").mkdir(parents=True)
    for name in adrs:
        (wt / "docs" / "adr" / name).write_text(f"# ADR text of {name}\n")
    return wt


def _plan(body="", paths=("bin/fleet-meet",)):
    lease = "".join(f'  - "{p}"\n' for p in paths)
    return f"---\nid: p\nzone: public\npaths:\n{lease}---\n\n# plan\n\n{body}\n"


def _bundle(extra_sections=0, section_bytes=0, extra=None):
    db = ("# declared behaviour\n\nintro.\n\n"
          "## Programs\n\n### bin/fleet-meet\n\nMEET-SECTION exits 0.\n\n"
          "### bin/fleet-pkill\n\nPKILL-SECTION exits 1.\n")
    for i in range(extra_sections):
        db += f"\n### bin/other-{i}\n\n" + "x" * section_bytes + "\n"
    b = {
        "bundle_version": 1, "repo": "demo", "openapi": [],
        "clis": [{"path": "bin/fleet-meet", "prog": "fleet-meet", "description": "meet"},
                 {"path": "bin/fleet-pkill", "prog": "fleet-pkill", "description": "kills things"}],
        "vocabularies": [],
        "config_keys": [{"name": "MEET_KEY", "read_by": ["bin/fleet-meet"]},
                        {"name": "OTHER_KEY", "read_by": ["bin/fleet-pkill"]}],
        "harness": [], "declared_behaviour": db, "gaps": [],
    }
    b.update(extra or {})
    return json.dumps(b, indent=2)


def test_prompt_carries_the_leased_programs_section_ahead_of_the_index(tmp_path):
    wt = _prompt_worktree(tmp_path)
    p = mod.build_prompt(_plan(), wt, _bundle(), budget=100_000)
    assert "MEET-SECTION exits 0." in p
    assert "MEET_KEY" in p
    assert p.index("MEET-SECTION") < p.index("--- index: the other programs ---")
    assert "fleet-pkill — kills things" in p
    assert "PKILL-SECTION" in p          # the budget allows the rest


def test_prompt_with_a_tight_budget_drops_the_rest_but_keeps_the_touched_part(tmp_path):
    wt = _prompt_worktree(tmp_path)
    full = mod.build_prompt(_plan(), wt, _bundle(), budget=100_000)
    budget = len(full.encode()) - 40
    p = mod.build_prompt(_plan(), wt, _bundle(), budget=budget)
    assert "MEET-SECTION" in p
    assert "PKILL-SECTION" not in p
    assert "--- omitted:" in p and "bytes) ---" in p
    assert len(p.encode()) <= budget


def test_an_adr_cited_by_bare_name_is_included_and_a_missing_one_is_noted(tmp_path):
    wt = _prompt_worktree(tmp_path)
    p = mod.build_prompt(_plan("See ADR-0006 and ADR-0099."), wt, _bundle(), budget=100_000)
    assert "ADR text of 0006-blackbox.md" in p
    assert "ADR-0099: no such file in the worktree; skipped" in p
    assert "ADR text of 0099" not in p


def test_an_ambiguous_adr_name_is_skipped_not_guessed(tmp_path):
    wt = _prompt_worktree(tmp_path, adrs=("0006-a.md", "0006-b.md"))
    p = mod.build_prompt(_plan("ADR-0006"), wt, _bundle(), budget=100_000)
    assert "ADR-0006: matches more than one file" in p
    assert "ADR text of" not in p


def test_a_path_and_a_name_for_one_adr_include_it_once(tmp_path):
    wt = _prompt_worktree(tmp_path)
    p = mod.build_prompt(_plan("docs/adr/0006-blackbox.md and ADR-0006"), wt, _bundle(),
                         budget=100_000)
    assert p.count("ADR text of 0006-blackbox.md") == 1


def test_the_cut_falls_on_a_section_boundary_and_never_exceeds_the_budget(tmp_path):
    wt = _prompt_worktree(tmp_path)
    bundle = _bundle(extra_sections=20, section_bytes=2000)
    budget = 12_000
    p = mod.build_prompt(_plan(), wt, bundle, budget=budget)
    assert len(p.encode()) <= budget
    assert "--- omitted:" in p
    kept = re.findall(r"### bin/other-(\d+)\n", p)
    assert 0 < len(kept) < 20
    for m in re.finditer(r"### bin/other-\d+\n\n(x+)\n", p):
        assert len(m.group(1)) == 2000      # no section is cut short


def test_the_plans_own_contract_over_budget_is_refused(tmp_path):
    wt = _prompt_worktree(tmp_path)
    with pytest.raises(mod.Stop) as e:
        mod.build_prompt(_plan(), wt, _bundle(), budget=500)
    assert str(e.value).startswith("refused: the plan's own contract is ")
    assert "over the 500-byte prompt budget" in str(e.value)


def test_the_refusal_comes_before_the_cap_is_reserved(tmp_path, monkeypatch):
    sha, ssh_base = _build_target_repo(tmp_path)
    monkeypatch.setattr(mod, "PROMPT_BUDGET", 100)
    orch = _load_orch()
    reserved = []
    monkeypatch.setattr(mod.LaneRecord, "reserve", lambda self, *a: reserved.append(1))
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc != 0
    assert reserved == []


def test_the_same_plan_and_bundle_give_byte_identical_prompts(tmp_path):
    wt = _prompt_worktree(tmp_path)
    a = mod.build_prompt(_plan("ADR-0006"), wt, _bundle(), budget=50_000)
    b = mod.build_prompt(_plan("ADR-0006"), wt, _bundle(), budget=50_000)
    assert a == b
    assert hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest()
