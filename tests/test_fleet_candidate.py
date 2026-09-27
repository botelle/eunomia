"""Tests for fleet-candidate — ADR-0002's base ⊕ head candidate build.

The Forgejo surface is a real stub `http.server` in a background thread (the
migration to `fleetforge.Forge` retired the `_api` module attribute these
tests used to monkeypatch directly — plan 0022 §2 moves both migrated
consumers' tests onto a stub server that asserts the same paths and methods
the old open-coded calls hit). Git is REAL: a bare repo in tmp stands in for
the forge's git side, with `refs/pull/N/head` created by hand exactly as
Forgejo publishes it. Stubbing git would leave the one thing this tool exists
to do — merge two commits and notice a conflict — covered only by a mock's
opinion of it."""
import contextlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import threading
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"

_CAND_ENV = ("FLEET_WORK_ROOT", "FLEET_CANDIDATE_CMD", "FLEET_FORGEJO_URL",
             "FLEET_TOKEN_CMD", "FLEET_GIT_SSH_BASE")


def _load(**env):
    for k in _CAND_ENV:          # no inheritance between tests: every one of
        os.environ.pop(k, None)  # these is read AT IMPORT (test_fleet_watch r3)
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("fleet_candidate", str(BIN / "fleet-candidate"))
    spec = importlib.util.spec_from_loader("fleet_candidate", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(args, cwd):
    # identity via -c, not os.environ: a global GIT_AUTHOR_* set here would
    # outlive this module and reach every other test file under random order —
    # the same leak class ci.yml's pytest-randomly comment was written for.
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _forge(tmp_path, conflict=False):
    """A bare repo with main and a PR head, published the way Forgejo does.

    Returns (ssh_base, base_sha, head_sha)."""
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / "shared.txt").write_text("original\n")
    (src / "keep.txt").write_text("keep\n")
    _git(["add", "."], src)
    _git(["commit", "-qm", "root"], src)
    root = _git(["rev-parse", "HEAD"], src)

    # the PR branch
    _git(["checkout", "-q", "-b", "feat"], src)
    (src / "feature.txt").write_text("feature\n")
    if conflict:
        (src / "shared.txt").write_text("from the branch\n")
    _git(["add", "."], src)
    _git(["commit", "-qm", "feature"], src)
    head = _git(["rev-parse", "HEAD"], src)

    # main moves AFTER the branch was cut — the whole point of a candidate
    _git(["checkout", "-q", "main"], src)
    if conflict:
        (src / "shared.txt").write_text("from main\n")
    else:
        (src / "other.txt").write_text("other\n")
    _git(["add", "."], src)
    _git(["commit", "-qm", "main moves"], src)
    base = _git(["rev-parse", "HEAD"], src)
    assert base != root

    bare = tmp_path / "forge" / "operator" / "demo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    # Forgejo publishes the head ref and NOTHING ELSE — no refs/pull/N/merge.
    _git(["update-ref", f"refs/pull/1/head", head], bare)
    return str(tmp_path / "forge"), base, head


# ------------------------------------------------------------- stub forge API
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        status, body = self.server.route(self.command, self.path, raw_body)
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
def _stub_forge_server(route):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.route = route
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@contextlib.contextmanager
def _wire(mod, base, head, state="open", stale_base="0" * 40):
    """Stand up the stub server this test's `mod` talks to, wire `mod._token`,
    and yield (base_url, posted). `stale_base` is what the PR object reports
    as its base sha — deliberately wrong, so a tool that trusts it fails the
    test. Unexpected calls are recorded in `errors` (raising inside the
    handler thread would not surface as a Python exception here — it would
    just look like a connection reset to the client) and asserted empty by
    the caller."""
    posted = []
    errors = []

    def route(method, path, body):
        if method == "GET" and path.rstrip("/").endswith("/pulls/1"):
            return 200, {"state": state, "head": {"sha": head},
                         "base": {"ref": "main", "sha": stale_base}}
        if method == "GET" and "/branches/main" in path:
            return 200, {"commit": {"id": base}}
        if method == "POST" and "/statuses/" in path:
            posted.append({"sha": path.rsplit("/", 1)[-1], **json.loads(body)})
            return 201, {}
        errors.append((method, path))
        return 599, {"error": "unexpected call"}

    with _stub_forge_server(route) as base_url:
        mod.FORGEJO_URL = base_url
        mod._token = lambda: ("tok", "")
        yield posted
    assert not errors, f"unexpected API call(s): {errors}"


def test_clean_candidate_passes_and_names_its_base(tmp_path):
    ssh, base, head = _forge(tmp_path)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="test -f feature.txt && test -f other.txt")
    with _wire(mod, base, head) as posted:
        rc = mod.main(["operator/demo", "1", "--post"])

    assert rc == 0
    assert len(posted) == 1
    assert posted[0]["state"] == "success"
    assert posted[0]["context"] == "candidate"
    assert posted[0]["sha"] == head
    # the base it merged is IN the status, or the queue cannot detect staleness
    assert f"base={base[:7]}" in posted[0]["description"]


def test_the_command_runs_against_the_merge_not_the_branch(tmp_path):
    """other.txt exists only on main; feature.txt only on the branch. A command
    that sees both is running in a tree neither ref has on its own."""
    ssh, base, head = _forge(tmp_path)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="test -f other.txt")
    with _wire(mod, base, head) as posted:
        assert mod.main(["operator/demo", "1", "--post"]) == 0
    assert posted[0]["state"] == "success"


def test_conflict_is_a_failure_that_names_the_paths(tmp_path):
    ssh, base, head = _forge(tmp_path, conflict=True)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="true")
    with _wire(mod, base, head) as posted:
        rc = mod.main(["operator/demo", "1", "--post"])

    assert rc == 1
    assert posted[0]["state"] == "failure"
    assert "conflict in 1" in posted[0]["description"]
    assert "shared.txt" in posted[0]["description"]


def test_failing_tests_report_the_code_and_no_output(tmp_path):
    ssh, base, head = _forge(tmp_path)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="echo SECRETSHAPED_abc123 && exit 3")
    with _wire(mod, base, head) as posted:
        assert mod.main(["operator/demo", "1", "--post"]) == 1
    d = posted[0]["description"]
    assert posted[0]["state"] == "failure"
    assert "exit 3" in d
    # §6: structured facts on the forge, transcripts stay local
    assert "SECRETSHAPED" not in d


def test_no_command_refuses_rather_than_posting_green(tmp_path):
    """The one place this must not degrade open: absent evidence is not a pass."""
    ssh, base, head = _forge(tmp_path)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh)
    with _wire(mod, base, head) as posted:
        assert mod.main(["operator/demo", "1", "--post"]) == 2
    assert posted == []


def test_it_never_pushes(tmp_path):
    """Asserted two ways: the guard refuses a push argv, and a real run records
    zero push invocations against a git wrapper on PATH."""
    ssh, base, head = _forge(tmp_path)
    log = tmp_path / "git-calls.log"
    shim = tmp_path / "shim"
    shim.mkdir()
    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    (shim / "git").write_text(f'#!/bin/sh\necho "$@" >> {log}\nexec {real} "$@"\n')
    (shim / "git").chmod(0o755)

    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="true")
    with _wire(mod, base, head):
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{shim}:{old}"
        try:
            assert mod.main(["operator/demo", "1", "--post"]) == 0
        finally:
            os.environ["PATH"] = old

    calls = log.read_text().splitlines()
    assert calls, "the shim recorded nothing — the test proves nothing"
    assert not [c for c in calls if c.split()[:1] == ["push"]]

    try:
        mod._git(["push", "origin", "main"], cwd=tmp_path)
    except AssertionError as e:
        assert "never pushes" in str(e)
    else:
        raise AssertionError("the push guard did not fire")


def test_the_pr_head_is_untouched_and_the_worktree_is_removed(tmp_path):
    ssh, base, head = _forge(tmp_path)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="true")
    with _wire(mod, base, head):
        assert mod.main(["operator/demo", "1", "--post"]) == 0

    bare = tmp_path / "forge" / "operator" / "demo.git"
    assert _git(["rev-parse", "refs/pull/1/head"], bare) == head
    assert _git(["rev-parse", "refs/heads/main"], bare) == base
    # derive the slug: hardcoding it meant this globbed a directory that did
    # not exist and asserted [] == [] — passing while proving nothing
    repo_dir = tmp_path / "work" / mod.lib.repo_slug("operator/demo")
    assert repo_dir.is_dir(), "the work root moved; this assertion is vacuous"
    assert list(repo_dir.glob("cand-*")) == []


def test_a_closed_pr_is_refused(tmp_path):
    ssh, base, head = _forge(tmp_path)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="true")
    with _wire(mod, base, head, state="closed") as posted:
        assert mod.main(["operator/demo", "1", "--post"]) == 2
    assert posted == []


def test_dry_run_posts_nothing(tmp_path):
    ssh, base, head = _forge(tmp_path)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="true")
    with _wire(mod, base, head) as posted:
        assert mod.main(["operator/demo", "1"]) == 0
    assert posted == []


def test_a_rejected_status_post_on_the_conflict_path_is_an_error_not_a_verdict(tmp_path):
    """The conflict path read `return 1, desc if not post else _post(...)`,
    which binds as `return 1, (...)` — _post's rc was discarded, so a forge that
    REFUSED the status still reported a clean 'candidate failure', and the
    operator message was a tuple. The suite missed it because the happy path
    posts fine; only a rejected POST separates the two.

    Raising inside `route` happens in the server thread, not this one — the
    client just sees a dropped connection, i.e. `(0, None)`, and `main()`
    also exits 2 on that (review 2146 LOW: this test could pass on a route
    mismatch elsewhere, proving nothing about the 403 path it names). `calls`
    and `errors` make the two distinguishable: an unexpected call is recorded
    rather than raised, and the assertions below require both that nothing
    unexpected happened AND that the rejected status POST was actually
    reached."""
    ssh, base, head = _forge(tmp_path, conflict=True)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="true")

    calls = []
    errors = []

    def route(method, path, body):
        calls.append((method, path.partition("?")[0]))
        if method == "GET" and path.rstrip("/").endswith("/pulls/1"):
            return 200, {"state": "open", "head": {"sha": head},
                         "base": {"ref": "main", "sha": "0" * 40}}
        if method == "GET" and "/branches/main" in path:
            return 200, {"commit": {"id": base}}
        if method == "POST" and "/statuses/" in path:
            return 403, None
        errors.append((method, path))
        return 404, None

    with _stub_forge_server(route) as base_url:
        mod.FORGEJO_URL = base_url
        mod._token = lambda: ("tok", "")
        assert mod.main(["operator/demo", "1", "--post"]) == 2

    assert not errors, f"unexpected API call(s): {errors}"
    assert any(m == "POST" and "/statuses/" in p for m, p in calls), (
        f"never reached the rejected status POST — got {calls}; without this "
        "the test would also pass on an unrelated route mismatch (both exit 2)")


def test_concurrent_builds_of_one_head_do_not_share_a_worktree(tmp_path):
    """add_worktree REPLACES an existing tree for a key, so a key of
    cand-<pr>-<sha> meant a second concurrent build yanked the first's tree
    mid-run and posted a false red."""
    ssh, base, head = _forge(tmp_path)
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_CANDIDATE_CMD="true")
    with _wire(mod, base, head):
        keys = []
        real_add = mod.lib.add_worktree
        mod.lib.add_worktree = lambda repo, key, ref, **kw: (
            keys.append(key), real_add(repo, key, ref, **kw))[1]

        real_pid = os.getpid
        for fake in (111, 222):
            mod.os.getpid = lambda f=fake: f
            assert mod.main(["operator/demo", "1", "--post"]) == 0
        mod.os.getpid = real_pid

    assert len(keys) == 2 and keys[0] != keys[1], keys


def test_the_candidate_command_gets_an_allowlisted_environment(tmp_path):
    """It runs UNREVIEWED code from the branch under test. fleet-watch's
    _child_env pops FLEET_TOKEN_CMD because its child is our own orchestrator;
    here the child is whatever the PR contains, so anything not allowlisted is
    absent. FLEET_TOKEN_CMD names the ADMIN token helper on this fleet — an
    admin token can PATCH a branch protection, push a `status: ready` plan and
    restore the rule, forging the ignition the fleet exists to make unforgeable.

    Verified leaking against the live forge before this test existed."""
    ssh, base, head = _forge(tmp_path)
    out = tmp_path / "env.txt"
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh,
                FLEET_TOKEN_CMD="/Users/x/bin/fetch-forgejo-admin-token.sh",
                FLEET_CANDIDATE_CMD=f"env > {out}")
    with _wire(mod, base, head):
        assert mod.main(["operator/demo", "1", "--post"]) == 0

    seen = dict(l.split("=", 1) for l in out.read_text().splitlines() if "=" in l)
    assert "FLEET_TOKEN_CMD" not in seen, "the admin-token recipe reached the child"
    for leaked in ("FLEET_WORK_ROOT", "FLEET_GIT_SSH_BASE", "FLEET_CANDIDATE_CMD"):
        assert leaked not in seen, f"{leaked} was not withheld"
    assert seen.get("FLEET_CANDIDATE") == "1"
    assert "PATH" in seen, "an allowlist that drops PATH breaks every command"


# --------------------------------------------------------------- plan 0022
def test_repo_shape_is_validated_with_fleetlib_not_a_second_regex(tmp_path, capsys):
    """plan 0022 §3: fleetlib.validate_repo, not a new regex — but it must not
    change fleet-candidate's own exit code (2) or message convention."""
    mod = _load(FLEET_WORK_ROOT=str(tmp_path / "work"))
    assert mod.main(["not-a-repo", "1"]) == 2
    assert "repo must be owner/repo" in capsys.readouterr().err


def test_no_open_coded_forge_calls_remain():
    text = (BIN / "fleet-candidate").read_text()
    assert "_api(" not in text
    assert "urlopen(" not in text
    assert "http.client" not in text
    for i, line in enumerate(text.splitlines(), 1):
        assert "api/v1" not in line, f"fleet-candidate:{i} still says api/v1: {line!r}"
