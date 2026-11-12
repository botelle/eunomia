"""Tests for the local runners spawned through epeius (plan 0154)."""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

D1 = ("local-qwen", "local-qwen3.8-27b", "local-qwen3.6-27b-mlx",
      "local-qwen3-coder-next", "local-qwen3-coder-30b", "local-qwen2.5-coder-32b",
      "local-deepseek-r1-70b", "local-deepseek-coder-v2-16b", "local-gemma3-12b",
      "local-gemma4-31b", "local-gpt-oss-120b", "local-muse-glimmer-30b",
      "local-devstral-small-2-24b", "local-exaone-deep-32b", "local-north-mini-code",
      "local-ornith-35b")

PLAN = """---
id: 0001-x
status: ready
repo: operator/x
zone: public
paths:
  - "docs/*.md"
  - "src/new.py"
---

# Do the thing

body
"""


OLLAMA_ONLY_GPU = "/usr/local/bin/llama-server, 9500 MiB\n"


def _load(name, mod_name):
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet"))
    mod = _load("orchestrator", "orchestrator_epeius")
    monkeypatch.setattr(mod, "implementer_timeout_minutes", lambda repo=None: 1)
    monkeypatch.setattr(mod, "write_transcript", lambda *a, **k: ("t.txt", []))
    monkeypatch.setattr(mod, "CIHOST_LOCK_POLL_SECS", 0.02)
    # No test reaches a real gpuhost: its GPU holds only ollama unless a test says so.
    mod.real_ssh_capture = mod._ssh_capture
    monkeypatch.setattr(mod, "_ssh_capture", lambda host, cmd: OLLAMA_ONLY_GPU)
    return mod


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def wt(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    w = tmp_path / "wt"
    _git(tmp_path, "init", "-q", "-b", "main", str(w))
    for k, v in (("user.name", "implbot"), ("user.email", "m@example.org")):
        _git(w, "config", k, v)
    (w / "plans").mkdir()
    (w / "plans" / "0001-x.md").write_text(PLAN)
    (w / "docs").mkdir()
    (w / "docs" / "a.md").write_text("a\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-qm", "base")
    _git(w, "remote", "add", "origin", str(remote))
    _git(w, "checkout", "-qb", "feat/x")
    return w


def _ctx(orch, branch="feat/x"):
    orch.set_run_context("operator/x", "0001-x", "plans/0001-x.md", PLAN, branch, "public")


CRED = types.SimpleNamespace(kind="none", principal="operator")


class Log:
    def __init__(self):
        self.events = []

    def note(self, kind, **kw):
        self.events.append((kind, kw))


def _tags(orch, monkeypatch, digest="f4031aab637dffff", models=("gemma3:12b",),
          calls=None):
    calls = calls if calls is not None else []

    def fake(endpoint, path, payload=None, timeout=120):
        calls.append((path, payload))
        if path == "/api/tags":
            return {"models": [{"name": m, "digest": digest} for m in models]}
        return {}
    monkeypatch.setattr(orch, "_ollama", fake)
    return calls


def _fake_forward(orch, monkeypatch):
    script = ("import socket,sys,time\ns=socket.socket()\n"
              "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
              "s.bind(('127.0.0.1',int(sys.argv[1])))\ns.listen()\ntime.sleep(600)\n")
    class L(list):
        pass
    procs = L()
    real = orch.open_forward
    monkeypatch.setattr(orch, "_forward_argv",
                        lambda port, host="cihost": [sys.executable, "-c", script, str(port)])

    def spy(host="cihost"):
        p, port = real(host)
        procs.append(p)
        hosts.append(host)
        return p, port
    hosts = procs.hosts = []
    monkeypatch.setattr(orch, "open_forward", spy)
    return procs


def _no_spawn(orch, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("epeius must not be spawned")
    monkeypatch.setattr(orch, "_spawn_session", boom)


# ------------------------------------------------------------------ D1

def test_every_d1_runner_is_declared_as_the_plan_says(orch):
    import re
    full = re.compile(r"[a-z][a-z0-9._:-]*\d[a-z0-9._:-]*")
    assert set(D1) <= set(orch.RUNNERS)
    for name in D1:
        r = orch.RUNNERS[name]
        assert r.local is True and r.needs is None and r.extra_env == ()
        assert r.provenance in orch.OPEN_CLASSES
        assert full.fullmatch(r.model) and not r.model.endswith(":latest")
        assert re.fullmatch(r"[0-9a-f]{12}", r.digest)
        assert r.num_ctx > 0
        assert not r.unwired and orch.is_epeius_runner(r)
        orch.check_open_weights_box(r)


# ------------------------------------------------------------------ D2

def test_the_filled_argv_is_the_pinned_tuple_plus_target_files(orch):
    r = orch.RUNNERS["local-gemma4-31b"]
    got = orch.fill_epeius_argv(r.argv, r, "http://127.0.0.1:5555", "public",
                                ["docs/a.md", "plans/p.md"])
    assert got == (orch.EPEIUS_BIN, "run", "--worktree", ".", "--task", "-",
                   "--backend", "ollama:gemma4:31b", "--endpoint",
                   "http://127.0.0.1:5555", "--zone", "public", "--num-ctx",
                   str(r.num_ctx), "--json", "--target-file", "docs/a.md",
                   "--target-file", "plans/p.md")
    assert not any("<" in a for a in got)


def test_an_unfilled_placeholder_is_a_stop(orch):
    r = orch.RUNNERS["local-gemma4-31b"]
    with pytest.raises(orch.Stop):
        orch.fill_epeius_argv(r.argv + ("<extra>",), r, "http://127.0.0.1:1", "public")
    with pytest.raises(orch.Stop):
        orch.fill_epeius_argv(r.argv, r._replace(num_ctx=None), "http://x", "public")


def test_targets_expand_globs_and_always_include_the_plan(orch, wt):
    assert orch.epeius_targets(wt, PLAN, "plans/0001-x.md") == [
        "docs/a.md", "plans/0001-x.md"]


def test_the_child_env_is_exactly_the_allowlist(orch, tmp_path, monkeypatch):
    src = {"PATH": "/bin", "LANG": "C", "LC_ALL": "C", "LC_CTYPE": "C", "TZ": "UTC",
           "HOME": "/Users/real", "SSH_AUTH_SOCK": "/tmp/agent", "USER": "u",
           "TMPDIR": "/real/tmp"}
    env = orch.epeius_env(tmp_path, src)
    assert "SSH_AUTH_SOCK" not in env and "USER" not in env
    assert env == {"PATH": "/bin", "LANG": "C", "LC_ALL": "C", "LC_CTYPE": "C",
                   "TZ": "UTC", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}


def test_the_spawned_child_gets_that_env(orch, wt, monkeypatch):
    seen = {}
    _ctx(orch)
    _fake_forward(orch, monkeypatch)
    _tags(orch, monkeypatch)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent")

    def spawn(argv, env, *a, **k):
        seen["env"] = env
        return orch.Session(3, False, "t", [], 0, 0)
    monkeypatch.setattr(orch, "_spawn_session", spawn)
    monkeypatch.setattr(orch, "harness_argv", lambda r: r.argv)
    orch.run_implementer(wt, "p", None, orch.RUNNERS["local-gemma3-12b"], "L", Log())
    assert "SSH_AUTH_SOCK" not in seen["env"]
    assert seen["env"]["HOME"] != os.environ["HOME"]
    assert seen["env"]["HOME"] == seen["env"]["TMPDIR"]


def test_the_prompt_bound_includes_the_reply_reserve(orch):
    r = orch.RUNNERS["local-gemma3-12b"]._replace(num_ctx=1000, reply_reserve=400)
    orch.check_prompt_fits(r, 1800, 0)          # 600 + 400 <= 1000
    with pytest.raises(orch.Stop):
        orch.check_prompt_fits(r, 1803, 0)      # 601 + 400 > 1000
    with pytest.raises(orch.Stop):
        orch.check_prompt_fits(r, 900, 1000)


def test_a_moved_or_missing_model_stops_before_epeius(orch, wt, monkeypatch):
    _ctx(orch)
    _fake_forward(orch, monkeypatch)
    _no_spawn(orch, monkeypatch)
    monkeypatch.setattr(orch, "harness_argv", lambda r: r.argv)
    r = orch.RUNNERS["local-gemma3-12b"]
    _tags(orch, monkeypatch, digest="0000000000000000")
    with pytest.raises(orch.Stop, match="moved"):
        orch.run_implementer(wt, "p", None, r, "L", Log())
    _tags(orch, monkeypatch, models=("other:1b",))
    with pytest.raises(orch.Stop, match="no model"):
        orch.run_implementer(wt, "p", None, r, "L", Log())


def test_unload_follows_a_raising_run_and_precedes_the_lock_release(orch, wt, monkeypatch):
    _ctx(orch)
    procs = _fake_forward(orch, monkeypatch)
    calls = _tags(orch, monkeypatch, digest="6316f0629137ffff", models=("gemma4:31b",))
    monkeypatch.setattr(orch, "harness_argv", lambda r: r.argv)
    path = orch.lib.locks_dir() / "cihost-ollama.lock"
    held_during_unload = []
    real = orch.unload_model

    def unload(endpoint, runner, log=None):
        try:
            with orch.lib.held_lock(path, blocking=False):
                held_during_unload.append(False)
        except orch.lib.LockUnavailable:
            held_during_unload.append(True)
        real(endpoint, runner, log)
    monkeypatch.setattr(orch, "unload_model", unload)

    def spawn(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(orch, "_spawn_session", spawn)
    with pytest.raises(RuntimeError):
        orch.run_implementer(wt, "p", None, orch.RUNNERS["local-gemma4-31b"], "L", Log())
    assert ("/api/generate", {"model": "gemma4:31b", "keep_alive": 0}) in calls
    assert held_during_unload == [True]
    with orch.lib.held_lock(path, blocking=False):
        pass
    assert procs and procs[0].poll() is not None, "the forward outlived the run"


def test_a_second_run_waits_on_the_lock(orch):
    entered, release = threading.Event(), threading.Event()

    def holder():
        with orch.cihost_lock(5):
            entered.set()
            release.wait(5)
    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(5)
    with pytest.raises(orch.Stop, match="lock"):
        with orch.cihost_lock(0):
            pass
    got = []

    def waiter():
        with orch.cihost_lock(5):
            got.append(time.time())
    w = threading.Thread(target=waiter)
    w.start()
    time.sleep(0.2)
    assert not got
    release.set()
    t.join()
    w.join()
    assert got


# ------------------------------------------------------------------ D3

def _stub_epeius(tmp_path, rc, write=True):
    p = tmp_path / "epeius-stub"
    p.write_text("#!%s\nimport sys\nsys.stdin.read()\n%sprint('{}')\nsys.exit(%d)\n"
                 % (sys.executable,
                    "open('docs/b.md','w').write('b\\n')\n" if write else "", rc))
    p.chmod(0o755)
    return p


def _opener(tmp_path, monkeypatch, orch):
    out = tmp_path / "opened"
    p = tmp_path / "open-pr"
    p.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {out}\ncat \"$5\" >> {out}\n")
    p.chmod(0o755)
    monkeypatch.setattr(orch, "OPEN_PR_SH", str(p))
    return out


def _run_stub(orch, wt, tmp_path, monkeypatch, rc):
    stub = _stub_epeius(tmp_path, rc)
    monkeypatch.setattr(orch, "EPEIUS_BIN", str(stub))
    r = orch.RUNNERS["local-gemma3-12b"]._replace(
        argv=(str(stub),) + orch.EPEIUS_ARGV[1:])
    _ctx(orch)
    _fake_forward(orch, monkeypatch)
    _tags(orch, monkeypatch)
    return orch.run_implementer(wt, "p", CRED, r, "L", Log())


def test_edits_produced_are_committed_flipped_pushed_and_opened(orch, wt, tmp_path, monkeypatch):
    out = _opener(tmp_path, monkeypatch, orch)
    s = _run_stub(orch, wt, tmp_path, monkeypatch, 0)
    assert s.exit_code == 0
    assert "status: done" in (wt / "plans/0001-x.md").read_text()
    assert _git(wt, "status", "--porcelain") == ""
    assert "implemented by local-gemma3-12b" in _git(wt, "log", "-1", "--format=%B")
    assert "feat/x" in _git(wt, "ls-remote", "origin")
    args = out.read_text().splitlines()
    assert args[:3] == ["operator/x", "feat/x", "main"]
    assert "Plan: 0001-x" in out.read_text()


def test_edits_rejected_commits_and_pushes_nothing(orch, wt, tmp_path, monkeypatch):
    out = _opener(tmp_path, monkeypatch, orch)
    before = _git(wt, "rev-parse", "HEAD")
    s = _run_stub(orch, wt, tmp_path, monkeypatch, 4)
    assert s.exit_code == 4
    assert _git(wt, "rev-parse", "HEAD") == before
    assert _git(wt, "ls-remote", "origin") == ""
    assert not out.exists()


def test_a_fix_round_pushes_and_opens_nothing(orch, wt, tmp_path, monkeypatch):
    out = _opener(tmp_path, monkeypatch, orch)
    stub = _stub_epeius(tmp_path, 0)
    monkeypatch.setattr(orch, "EPEIUS_BIN", str(stub))
    r = orch.RUNNERS["local-gemma3-12b"]._replace(
        argv=(str(stub),) + orch.EPEIUS_ARGV[1:])
    _ctx(orch)
    _fake_forward(orch, monkeypatch)
    _tags(orch, monkeypatch)
    orch.run_implementer(wt, "fix it", CRED, r, "L", Log(), tag="r1")
    assert "feat/x" in _git(wt, "ls-remote", "origin")
    assert not out.exists()


# ------------------------------------------------------------------ plan 0160

def test_every_runner_has_a_known_host_and_only_gemma3_is_on_gpuhost(orch):
    for name in D1:
        assert orch.RUNNERS[name].host in orch.OLLAMA_HOSTS
    assert {n for n in D1 if orch.RUNNERS[n].host == "gpuhost"} == {"local-gemma3-12b"}


def _spawn_ok(orch, monkeypatch):
    monkeypatch.setattr(orch, "harness_argv", lambda r: r.argv)
    monkeypatch.setattr(orch, "_spawn_session",
                        lambda *a, **k: orch.Session(3, False, "t", [], 0, 0))


def test_the_forward_opens_to_the_runners_host(orch, monkeypatch):
    argv = orch._forward_argv(1234, "gpuhost")
    assert argv[-1] == "gpuhost" and "127.0.0.1:1234:127.0.0.1:11434" in argv
    assert orch._forward_argv(1234)[-1] == "cihost"


def test_a_gpuhost_run_forwards_to_gpuhost_and_skips_the_unload(orch, wt, monkeypatch):
    _ctx(orch)
    procs = _fake_forward(orch, monkeypatch)
    calls = _tags(orch, monkeypatch)
    _spawn_ok(orch, monkeypatch)
    orch.run_implementer(wt, "p", None, orch.RUNNERS["local-gemma3-12b"], "L", Log())
    assert procs.hosts == ["gpuhost"]
    assert not [c for c in calls if c[0] == "/api/generate"]


def test_a_cihost_run_forwards_to_cihost_and_unloads(orch, wt, monkeypatch):
    _ctx(orch)
    procs = _fake_forward(orch, monkeypatch)
    calls = _tags(orch, monkeypatch, digest="6316f0629137ffff", models=("gemma4:31b",))
    _spawn_ok(orch, monkeypatch)
    orch.run_implementer(wt, "p", None, orch.RUNNERS["local-gemma4-31b"], "L", Log())
    assert procs.hosts == ["cihost"]
    assert ("/api/generate", {"model": "gemma4:31b", "keep_alive": 0}) in calls


def test_the_two_hosts_locks_are_independent(orch):
    with orch.ollama_lock("gpuhost", 5):
        with orch.ollama_lock("cihost", 0):
            pass
        with pytest.raises(orch.Stop, match="gpuhost ollama lock"):
            with orch.ollama_lock("gpuhost", 0):
                pass


def test_a_python_process_on_gpuhosts_gpu_stops_before_any_spawn(orch, wt, monkeypatch):
    _ctx(orch)
    _fake_forward(orch, monkeypatch)
    _tags(orch, monkeypatch)
    _no_spawn(orch, monkeypatch)
    monkeypatch.setattr(orch, "harness_argv", lambda r: r.argv)
    monkeypatch.setattr(orch, "_ssh_capture", lambda h, c: OLLAMA_ONLY_GPU
                        + "/usr/bin/python3, 12000 MiB\n")
    with pytest.raises(orch.Stop, match=r"GPU is in use by /usr/bin/python3"):
        orch.run_implementer(wt, "p", None, orch.RUNNERS["local-gemma3-12b"], "L", Log())


def test_a_failing_ssh_stops_a_gpuhost_run(orch, wt, monkeypatch):
    _ctx(orch)
    _fake_forward(orch, monkeypatch)
    _tags(orch, monkeypatch)
    _no_spawn(orch, monkeypatch)
    monkeypatch.setattr(orch, "harness_argv", lambda r: r.argv)
    monkeypatch.setattr(orch, "_ssh_capture", orch.real_ssh_capture)
    for outcome in (subprocess.CompletedProcess([], 255, "", "refused"),
                    subprocess.TimeoutExpired("ssh", 30), OSError("no ssh")):
        def run(*a, _o=outcome, **k):
            if isinstance(_o, BaseException):
                raise _o
            return _o
        monkeypatch.setattr(orch.subprocess, "run", run)
        with pytest.raises(orch.Stop, match="GPU"):
            orch.run_implementer(wt, "p", None, orch.RUNNERS["local-gemma3-12b"], "L", Log())
