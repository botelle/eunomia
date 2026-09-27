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
                       declared_behaviour=True, preexisting_tests=None, repo=REPO):
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
_FAKE_ANTIGRAVITY = '''\
import json, os, sys
line = sys.stdin.readline()
with open("stdin_capture.bin", "w") as f:
    f.write(line)
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
result = {"files": [{"path": "tests/test_generated.py",
                     "content": "def test_generated():\\n    assert True\\n"}],
         "gaps": ["an example gap the fake vendor reported"],
         "notes": "from the fake antigravity vendor"}
out = {"event": "result", "response": result, "usage": {"tokens": 123}}
sys.stdout.write(json.dumps(out) + "\\n")
'''


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


def _run(tmp_path, repo, plan_id, sha, runner_name, orch_mod, testers=None,
         dry_run=False, schema_path=None, pr_status=201):
    """`testers` None means "the repo names exactly `runner_name`" — the
    happy setup. Pass [] for a fleet.db with no tester rows at all."""
    with _stub_forge(pr_status=pr_status) as (httpd, base_url):
        _reset_env(tmp_path / "fleet", tmp_path / "work", tmp_path / "forge", base_url)
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
        ["git", "show", f"agent/tests/{PLAN_ID}/sub-antigravity:tests/test_generated.py"],
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
    expected = (orch.antigravity_stdin_line(prompt_holder["prompt"]) + "\n").encode("utf-8")
    assert captured == expected, "the fake vendor's stdin must match the adapter byte for byte"


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
    orch = _load_orch()   # unpatched: sub-codex is unwired in the real registry
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
    (src / "tests" / "evil").symlink_to("/etc")
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
        "result = {'files': [{'path': 'tests/evil/passwd', 'content': 'x'}],"
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
        tmp_path, {"files": [{"path": "tests/test_x.py", "content": "x",
                              "executable": True}], "gaps": [], "notes": ""})
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_a_path_that_already_exists_at_the_landing_base_refuses(tmp_path):
    rc, requests = _run_with_result(
        tmp_path,
        {"files": [{"path": "tests/conftest.py", "content": "x"}], "gaps": [], "notes": ""},
        preexisting_tests={"tests/conftest.py": "# existing\n"})
    assert rc == 1
    assert not [r for r in requests if r["method"] == "POST"]


def test_the_landing_check_is_against_mains_tip_at_landing_time_not_sha(tmp_path):
    """D1.6: 'the check is against that tip (fetched then), not against
    `<sha>` — a test merged after `<sha>` must be just as unwritable.' A
    file absent at `--sha` but present on `main` by landing time must still
    refuse."""
    sha, ssh_base = _build_target_repo(tmp_path)   # no tests/conftest.py at sha
    src = tmp_path / "src"
    (src / "tests" / "conftest.py").write_text("# added after sha\n")
    _git(["add", "."], src)
    _git(["commit", "-qm", "add conftest after sha"], src)
    bare = _bare_repo_path(tmp_path)
    _git(["push", "-q", str(bare), "main"], src)

    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    script_body = (
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "result = {'files': [{'path': 'tests/conftest.py', 'content': 'x'}],"
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
        ["git", "ls-tree", f"agent/tests/{PLAN_ID}/sub-antigravity", "tests/test_generated.py"],
        cwd=bare, capture_output=True, text=True)
    assert ls.returncode == 0
    assert ls.stdout.startswith("100644"), ls.stdout


# -------------------------------------------------------------------- codex
def test_codex_adapter_reads_the_out_file_via_the_fake_vendor(tmp_path):
    """sub-codex is unwired for real spawns (see
    test_unwired_runner_is_refused_and_never_spawned); this exercises the
    CODEX ADAPTER shape in isolation with a fake, wired runner, per D7's
    "the codex adapter is exercised only by the fake vendor" note."""
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    result_doc = {"files": [{"path": "tests/test_from_codex.py",
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
        ["git", "show", f"agent/tests/{PLAN_ID}/sub-codex:tests/test_from_codex.py"],
        cwd=bare, capture_output=True, text=True)
    assert show.returncode == 0
    assert "def test_c" in show.stdout


def test_codex_stdin_is_the_raw_prompt_not_wrapped(tmp_path):
    sha, ssh_base = _build_target_repo(tmp_path)
    orch = _load_orch()
    orch.RUNNERS = dict(orch.RUNNERS)
    result_doc = {"files": [{"path": "tests/test_stdin_capture.py",
                             "content": "def test_x():\n    assert True\n"}],
                 "gaps": [], "notes": ""}
    script_path = tmp_path / "fake_codex_capture.py"
    script_path.write_text(
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        "open('captured_prompt.txt', 'w').write(prompt)\n"
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
    lane_dir = mod.lib.work_root() / "lanes" / PLAN_ID / "sub-codex"
    captured = (lane_dir / "cwd" / "captured_prompt.txt").read_text()
    assert captured.startswith(mod.LANE_PREAMBLE.splitlines()[0])


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
    (tmp_path / "tests").mkdir()
    for bad in ("../x", "/etc/x", "tests/../x", "", "tests/", None, 5):
        with pytest.raises(mod.Stop):
            mod.confine_path(bad, tmp_path)
    assert mod.confine_path("tests/a/b.py", tmp_path) == "tests/a/b.py"


def test_validate_schema_against_the_shipped_schema(tmp_path):
    schema = json.loads((ROOT / "config" / "lane-result.schema.json").read_text())
    ok = {"files": [{"path": "tests/x.py", "content": "y"}], "gaps": [], "notes": None}
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
doc = {{"files": [{{"path": "tests/test_generated.py", "content": "def test_x():\\n    pass\\n"}}],
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
