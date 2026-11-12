"""fleet-cloud-review (plan 0130).

The forge and the mirror are local bare repositories, `claude` is a shim this
file writes onto PATH that records its argv, and the forge API is a double that
records every call. Nothing reaches GitHub, the forge or a model.
"""
import datetime
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "operator/lynceus"
MIRROR = "operator/review-mirror"
ENV = {"GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@example.org",
       "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@example.org"}


def git(cwd, *args):
    e = dict(os.environ, **ENV)
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=e,
                          capture_output=True, text=True, check=True).stdout.strip()


def _load():
    ldr = importlib.machinery.SourceFileLoader("fleet_cloud_review", str(BIN / "fleet-cloud-review"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("fleet_cloud_review", ldr))
    ldr.exec_module(mod)
    return mod


class FakeForge:
    """Read-only double. Any other attribute is a write, and records itself."""

    def __init__(self, base, head):
        self.base, self.head = base, head
        self.writes = []

    def get_pull(self, repo, n):
        return 200, {"base": {"sha": self.base, "ref": "main"},
                     "head": {"sha": self.head, "ref": "feat/x"}}

    def __getattr__(self, name):
        def rec(*a, **k):
            self.writes.append(name)
            return 200, {}
        return rec


@pytest.fixture
def env(tmp_path, monkeypatch):
    forge_root = tmp_path / "forge"
    bare = forge_root / "operator" / "lynceus.git"
    bare.mkdir(parents=True)
    git(bare, "init", "--bare", "--quiet", "-b", "main")
    work = tmp_path / "work"
    git(tmp_path, "init", "--quiet", "-b", "main", str(work))
    (work / "a.txt").write_text("1\n")
    git(work, "add", "."); git(work, "commit", "-qm", "base")
    base = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-qb", "feat/x")
    (work / "a.txt").write_text("2\n")
    git(work, "commit", "-qam", "head")
    head = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", str(bare), "main", "feat/x")

    mirror_root = tmp_path / "mirror"
    mirror = mirror_root / "operator" / "review-mirror.git"
    mirror.mkdir(parents=True)
    git(mirror, "init", "--bare", "--quiet")

    conf = tmp_path / "cloud-review.conf"
    conf.write_text(f"mirror = {MIRROR}\ncredit_expires = 2026-11-05\nmax_requests = 40\n"
                    f"reviews_per_session = 5\nrepo = {REPO}\n")
    shim = tmp_path / "shimbin"
    shim.mkdir()
    argv_log = tmp_path / "claude-argv"
    (shim / "claude").write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\0\' "$a"; done >> "{argv_log}"\n'
        f'printf \'\\n--END--\\n\' >> "{argv_log}"\nexit 0\n')
    (shim / "claude").chmod(0o755)

    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.setenv("FLEET_CLOUD_REVIEW_CONF", str(conf))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet"))
    monkeypatch.setenv("FLEET_GIT_SSH_BASE", f"file://{forge_root}")
    monkeypatch.setenv("FLEET_CLOUD_MIRROR_GIT_BASE", f"file://{mirror_root}")
    mod = _load()
    monkeypatch.setattr(mod, "_mirror_status", lambda m: 404)
    monkeypatch.setattr(mod, "_today", lambda: datetime.date(2026, 10, 11))

    class E:
        pass
    e = E()
    e.mod, e.conf, e.mirror, e.base, e.head, e.work = mod, conf, mirror, base, head, work
    e.forge = FakeForge(base, head)
    e.argv_log = argv_log
    e.fleet = tmp_path / "fleet" / "cloud-review"
    e.rid = f"lynceus-96-{head[:12]}"

    def run(*a):
        return mod.main(list(a), forge=e.forge)
    e.run = run
    e.req = lambda sess="s1", pr=96: run("request", REPO, str(pr), "--session", sess)
    e.refs = lambda: git(mirror, "for-each-ref", "--format=%(refname)").splitlines()
    e.claude_calls = lambda: argv_log.read_text().count("--END--") if argv_log.exists() else 0

    def push_review(first=None, extra=None):
        first = first or f"Reviewed: {e.rid} base={base} head={head}"
        git(work, "checkout", "-q", "--detach", head)
        (work / "review.md").write_text(first + "\nHIGH: nothing\n")
        git(work, "add", "review.md")
        if extra:
            (work / extra).write_text("x\n"); git(work, "add", extra)
        git(work, "commit", "-qm", "review")
        git(work, "push", "-q", str(mirror), f"HEAD:refs/heads/rev/{e.rid}")
    e.push_review = push_review
    return e


def test_unlisted_missing_and_empty_conf_push_nothing(env):
    env.conf.write_text(env.conf.read_text().replace(f"repo = {REPO}", "repo = operator/other"))
    assert env.req() == 1
    env.conf.write_text("")
    assert env.req() == 1
    env.conf.unlink()
    assert env.req() == 1
    assert env.refs() == [] and env.claude_calls() == 0


@pytest.mark.parametrize("day,ok", [((2026, 11, 4), True), ((2026, 11, 5), False),
                                     ((2026, 11, 6), False)])
def test_expiry_day_is_refused(env, monkeypatch, day, ok):
    monkeypatch.setattr(env.mod, "_today", lambda: datetime.date(*day))
    assert env.req() == (0 if ok else 1)
    assert bool(env.refs()) is ok and env.claude_calls() == (1 if ok else 0)


def test_per_session_and_total_limits(env):
    env.fleet.mkdir(parents=True)
    log = env.fleet / "requests.jsonl"
    log.write_text("".join(json.dumps({"id": f"x-{i}", "session": "s1"}) + "\n" for i in range(5)))
    assert env.req("s1") == 1
    assert env.claude_calls() == 0
    assert env.req("s2") == 0
    log.write_text("".join(json.dumps({"id": f"x-{i}", "session": f"s{i}"}) + "\n" for i in range(40)))
    assert env.req("new") == 1
    assert env.claude_calls() == 1


def test_public_mirror_refused_before_push(env, monkeypatch):
    monkeypatch.setattr(env.mod, "_mirror_status", lambda m: 200)
    assert env.req() == 1
    assert env.refs() == [] and env.claude_calls() == 0


def test_accepted_request(env):
    assert env.req("sess-9") == 0
    assert sorted(env.refs()) == [f"refs/heads/req/{env.rid}/base", f"refs/heads/req/{env.rid}/head"]
    assert git(env.mirror, "rev-parse", f"req/{env.rid}/base") == env.base
    assert git(env.mirror, "rev-parse", f"req/{env.rid}/head") == env.head
    assert env.claude_calls() == 1
    argv = env.argv_log.read_text().split("--END--")[0].split("\0")
    assert argv[0] == "-p" and argv[2:4] == ["--cloud", "sess-9"]
    assert env.rid in argv[1] and env.base in argv[1] and env.head in argv[1]
    lines = (env.fleet / "requests.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == env.rid


def test_collect_not_ready(env):
    env.req()
    assert env.run("collect", REPO, "96") == 3
    assert not (env.fleet / f"{env.rid}.md").exists()


def test_collect_refuses_wrong_head_and_extra_file(env):
    env.req()
    env.push_review(first=f"Reviewed: {env.rid} base={env.base} head={env.base}")
    assert env.run("collect", REPO, "96") == 1
    assert not (env.fleet / f"{env.rid}.md").exists()
    git(env.mirror, "update-ref", "-d", f"refs/heads/rev/{env.rid}")
    env.push_review(extra="other.txt")
    assert env.run("collect", REPO, "96") == 1
    assert not (env.fleet / f"{env.rid}.md").exists()


def test_collect_saves_and_nothing_is_written_to_forge(env, capsys):
    env.req()
    env.push_review()
    assert env.run("collect", REPO, "96") == 0
    out = env.fleet / f"{env.rid}.md"
    assert out.read_text().startswith(f"Reviewed: {env.rid} ")
    assert str(out) in capsys.readouterr().out
    assert env.run("prune", env.rid) == 0
    assert env.refs() == []
    assert env.forge.writes == []


def test_collect_says_older_head(env, capsys):
    env.req()
    env.push_review()
    env.forge.head = "f" * 40
    assert env.run("collect", REPO, "96") == 0
    assert "older head" in capsys.readouterr().out


def test_exec_bit_in_git():
    out = subprocess.run(["git", "ls-files", "-s", "bin/fleet-cloud-review"],
                         cwd=BIN.parent, capture_output=True, text=True).stdout
    assert out.startswith("100755")
