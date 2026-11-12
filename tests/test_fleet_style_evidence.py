"""bin/fleet-style-evidence: a style pull request carries its screenshots, committed
by the producer (plan 0151, ADR-0025 §6).

No simulator, no network beyond loopback, no model. `FLEET_SIMCTL` is a stub that
keeps a state file, the screenshot command is a stub that writes PNGs from data
files in whichever tree it runs in, `sips` is a PATH shim, the remote is a bare
repository in a temporary directory and the forge is a loopback HTTP stub. Only an
invented repository appears here.
"""
import hashlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import zlib
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "acme/widgets"
PR = 7
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-5"
ED = "docs/screenshots"
COMMAND = "tools/shoot.py"
SHOT = {"platform": "ios-simulator", "command": COMMAND, "runtime": RUNTIME,
        "device": "iPhone 17", "scale": 3, "themes": ["classic", "bold"],
        "appearances": ["light", "dark"], "timeout_minutes": 2, "max_image_kb": 50}
ENTRY = {"paths": ["app/Theme/*.swift"], "screenshot": SHOT}

STUB_COMMAND = r'''#!/usr/bin/env python3
import os, random, struct, sys, zlib
a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
for need in ("--out", "--udid", "--themes", "--appearances"):
    assert need in a, need
if os.path.exists("fail.txt"):
    sys.exit(3)
color = int(open("color.txt").read().strip()) if os.path.exists("color.txt") else 0
screens = open("screens.txt").read().split()
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
def png(w, h, noise):
    rows = b"".join(b"\x00" + (os.urandom(w * 3) if noise else bytes([color]) * (w * 3)) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
for t in a["--themes"].split(","):
    for ap in a["--appearances"].split(","):
        d = os.path.join(a["--out"], t, ap)
        os.makedirs(d)
        for s in screens:
            open(os.path.join(d, s + ".png"), "wb").write(png(*((200, 300) if os.path.exists("noise.txt") else (30, 60)), os.path.exists("noise.txt")))
        open(os.path.join(d, "debug.log"), "w").write("not a screen")
'''

STUB_SIPS = r'''#!/usr/bin/env python3
import struct, sys, zlib
args = sys.argv[1:]
src, dst = args[2], args[4]
open(__import__("os").environ["STUB_SIPS_LOG"], "a").write(args[1] + "\n")
data = open(src, "rb").read()
t = b"tEXt"; d = b"Software\0sips"
extra = struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
i = data.rindex(struct.pack(">I", 0) + b"IEND")
open(dst, "wb").write(data[:i] + extra + data[i:])
'''

STUB_SIMCTL = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["STUB_SIM_STATE"]
log = open(os.environ["STUB_SIM_LOG"], "a")
log.write(json.dumps(sys.argv[1:]) + "\n"); log.close()
s = json.load(open(state)) if os.path.exists(state) else {"devices": {}}
a = sys.argv[1:]
def save(): json.dump(s, open(state, "w"))
if a[0] == "create":
    if os.environ.get("STUB_CREATE_FAIL"): sys.exit(1)
    udid = "AAAA1111-0000-0000-0000-%012d" % (len(s["devices"]) + 1)
    s["devices"][udid] = {"runtime": a[3], "booted": False}; save(); print(udid)
elif a[0] == "boot":
    s["devices"][a[1]]["booted"] = True; save()
elif a[0] == "shutdown":
    if a[1] in s["devices"]: s["devices"][a[1]]["booted"] = False; save()
elif a[0] == "delete":
    s["devices"].pop(a[1], None); save()
elif a[0] == "list":
    out = {}
    for u, d in s["devices"].items():
        if "booted" in a and not d["booted"]: continue
        rt = os.environ.get("STUB_RUNTIME_LIES") or d["runtime"]
        out.setdefault(rt, []).append({"udid": u, "state": "Booted" if d["booted"] else "Shutdown"})
    print(json.dumps({"devices": out}))
else:
    sys.exit(64)
'''


def _load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / file))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ev = _load("fleet_style_evidence", "fleet-style-evidence")


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ForgeStub(http.server.BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        st = self.state
        path = self.path.split("?")[0]
        if path.endswith(f"/pulls/{PR}"):
            return self._send(200, {"number": PR, "state": st["state"], "body": st["body"],
                                    "head": {"sha": st["head"], "ref": st["branch"]}})
        if path.endswith(f"/pulls/{PR}/reviews"):
            page = int(self.path.split("page=")[1].split("&")[0]) if "page=" in self.path else 1
            return self._send(200, st["reviews"] if page == 1 else [])
        self._send(404, {})

    def do_PATCH(self):
        n = int(self.headers["Content-Length"])
        self.state["body"] = json.loads(self.rfile.read(n))["body"]
        self.state["patches"] += 1
        self._send(201, {})


class World:
    """One invented repository, its remote, a worktree on the pull request branch."""

    def __init__(self, tmp, monkeypatch):
        self.tmp = tmp
        self.fleet = tmp / "fleet"
        self.remote = tmp / "remote.git"
        self.wt = tmp / "wt"
        self.simlog = tmp / "sim.log"
        self.simstate = tmp / "sim.json"
        self.sipslog = tmp / "sips.log"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.remote))
        sh("git", "init", "-q", "-b", "main", str(self.wt))
        for k, v in (("user.name", "Fleet Bot"), ("user.email", "bot@example.org")):
            sh("git", "config", k, v, cwd=self.wt)
        sh("git", "remote", "add", "origin", str(self.remote), cwd=self.wt)
        bindir = tmp / "shim"
        bindir.mkdir()
        (bindir / "sips").write_text(STUB_SIPS)
        (bindir / "sips").chmod(0o755)
        simctl = tmp / "simctl-stub"
        simctl.write_text(STUB_SIMCTL)
        simctl.chmod(0o755)
        cfg = tmp / "style-review.json"
        self.cfg = cfg
        self.set_entry(ENTRY)
        ForgeStub.state = {"state": "open", "body": "Implementer's report.\n", "head": "",
                           "branch": "feat/reskin", "reviews": [], "patches": 0}
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ForgeStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.forge = ForgeStub.state
        monkeypatch.setattr(ev.style, "CONFIG", cfg)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(self.fleet))
        monkeypatch.setenv("EUNOMIA_SESSION", "evidence-test")
        monkeypatch.setenv("FLEET_SIMCTL", str(simctl))
        monkeypatch.setenv("FLEET_FORGEJO_URL", f"http://127.0.0.1:{self.server.server_port}")
        monkeypatch.setenv("FLEET_AUTHOR_TOKEN_CMD", "echo tok")
        monkeypatch.setenv("STUB_SIM_STATE", str(self.simstate))
        monkeypatch.setenv("STUB_SIM_LOG", str(self.simlog))
        monkeypatch.setenv("STUB_SIPS_LOG", str(self.sipslog))
        monkeypatch.delenv("EUNOMIA_LEDGER_HOST", raising=False)
        self.base = self.head = None

    def set_entry(self, entry):
        self.cfg.write_text(json.dumps({"v": 1, "repos": {REPO: entry}}))

    def commit(self, files, msg):
        for rel, text in files.items():
            p = self.wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        sh("git", "add", "-A", cwd=self.wt)
        sh("git", "commit", "-q", "-m", msg, cwd=self.wt)
        return sh("git", "rev-parse", "HEAD", cwd=self.wt)

    def build(self, base_files=None, head_files=None):
        files = {COMMAND: STUB_COMMAND, "screens.txt": "home settings\n", "color.txt": "10\n",
                 "app/Theme/Colors.swift": "let c = 1\n"}
        files.update(base_files or {})
        self.base = self.commit(files, "base")
        sh("git", "push", "-q", "origin", "main", cwd=self.wt)
        sh("git", "checkout", "-q", "-b", "feat/reskin", cwd=self.wt)
        self.head = self.commit({"color.txt": "200\n", "app/Theme/Colors.swift": "let c = 2\n",
                                 **(head_files or {})}, "reskin")
        sh("git", "push", "-q", "origin", "feat/reskin", cwd=self.wt)
        self.forge["head"] = self.head
        return self

    def produce(self, plan=None, head=None):
        argv = ["produce", REPO, str(PR), "--worktree", str(self.wt), "--base", self.base,
                "--head", head or self.head]
        if plan:
            argv += ["--plan", str(plan)]
        return ev.main(argv)

    def sim_calls(self):
        if not self.simlog.exists():
            return []
        return [json.loads(l) for l in self.simlog.read_text().splitlines()]

    def devices(self):
        return json.load(open(self.simstate))["devices"] if self.simstate.exists() else {}

    def leases(self):
        d = self.fleet / "leases"
        return [json.loads(p.read_text()) for p in d.glob("sim--*.json")] if d.exists() else []

    def remote_tree(self, rev):
        return sh("git", "ls-tree", "-r", "--name-only", rev, cwd=self.remote).splitlines()

    def plan(self, pid="0007-reskin"):
        p = self.tmp / "plan.md"
        p.write_text(f"---\nid: {pid}\nstatus: ready\nrepo: {REPO}\nreview: style\n---\n")
        return p


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    w.server.shutdown()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_a_produced_folder_passes_verify_and_is_pinned(world, capsys):
    world.build()
    assert world.produce(plan=world.plan()) == 0
    sha = sh("git", "rev-parse", "HEAD", cwd=world.wt)
    assert sha != world.head and sh("git", "rev-parse", "HEAD^", cwd=world.wt) == world.head
    folder = f"{ED}/{today()}-0007-reskin"
    changed = sh("git", "diff", "--name-only", world.head, sha, cwd=world.wt).splitlines()
    assert changed and all(c.startswith(folder + "/") for c in changed)
    assert f"{folder}/classic-light/home-before.png" in changed
    assert sh("git", "log", "-1", "--format=%s", cwd=world.wt) == f"screenshots: 0007-reskin at {world.head[:8]}"
    assert sh("git", "rev-parse", "feat/reskin", cwd=world.remote) == sha
    res = ev.style.verify(REPO, PR, "0007-reskin", ev.style.Git(world.wt / ".git"), world.base, sha)
    assert res["ok"], res["problems"]
    # the record lists exactly the committed images' hashes
    rec = json.loads(ev.style.record_path(REPO, PR, sha).read_text())
    committed = sorted(
        (f, hashlib.sha256(subprocess.run(["git", "show", f"{sha}:{f}"], cwd=world.wt,
                                          capture_output=True).stdout).hexdigest())
        for f in changed if f.endswith(".png"))
    assert sorted((i["path"], i["sha256"]) for i in rec["images"]) == committed
    assert oct(ev.style.record_path(REPO, PR, sha).stat().st_mode & 0o777) == "0o600"
    # every raw URL is pinned to the evidence commit, never the branch
    body = world.forge["body"]
    urls = re.findall(r"\((http[^)]+)\)", body)
    assert urls and all(f"/{REPO}/raw/commit/{sha}/{folder}/" in u for u in urls)
    assert "feat/reskin" not in body
    assert body.startswith("Implementer's report.\n")
    assert "Total size" in body
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ok"] is True


def test_runtime_is_passed_literally_and_the_simulator_is_cleaned_up(world):
    world.build()
    assert world.produce() == 0
    create = next(c for c in world.sim_calls() if c[0] == "create")
    assert create[2:] == ["iPhone 17", RUNTIME]
    assert world.devices() == {}
    assert [l["state"] for l in world.leases()] == ["released"]
    readme = Path(world.wt / f"{ED}/{today()}-pr{PR}/README.md").read_text()
    assert RUNTIME in readme                    # what the device reported


def test_an_empty_runtime_refuses_before_simctl_runs(world, capsys):
    world.build()
    world.set_entry(dict(ENTRY, screenshot=dict(SHOT, runtime="")))
    assert world.produce() == 1
    assert "step 1" in capsys.readouterr().err
    assert world.sim_calls() == []


def test_a_runtime_the_device_does_not_report_refuses(world, monkeypatch, capsys):
    world.build()
    monkeypatch.setenv("STUB_RUNTIME_LIES", "com.apple.CoreSimulator.SimRuntime.iOS-99-0")
    assert world.produce() == 1
    assert "step 3" in capsys.readouterr().err
    assert world.devices() == {} and [l["state"] for l in world.leases()] == ["released"]


def test_the_heads_copy_of_the_command_is_never_executed(world):
    sentinel = world.tmp / "head-command-ran"
    evil = STUB_COMMAND + f"\nopen({str(sentinel)!r}, 'w').write('x')\n"
    world.build(head_files={COMMAND: evil})
    assert world.produce() == 0
    assert not sentinel.exists()
    readme = (world.wt / f"{ED}/{today()}-pr{PR}/README.md").read_text()
    blob = sh("git", "rev-parse", f"{world.base}:{COMMAND}", cwd=world.wt)
    assert blob in readme


def test_the_command_runs_with_no_credentials(world, monkeypatch):
    world.build()
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    seen, argvs = [], []
    real = ev.subprocess.Popen

    def spy(argv, **kw):
        if str(argv[0]).endswith("screenshot-command"):
            seen.append(kw["env"])
            argvs.append(" ".join(argv[1:]))
        return real(argv, **kw)

    monkeypatch.setattr(ev.subprocess, "Popen", spy)
    assert world.produce() == 0
    assert seen and all("SSH_AUTH_SOCK" not in e for e in seen)
    assert len(argvs) == 2 and all(
        "--themes classic,bold --appearances light,dark" in a and "--udid AAAA1111" in a
        for a in argvs)


def test_the_command_is_bounded_by_timeout_minutes(world, monkeypatch, capsys):
    world.build(base_files={COMMAND: "#!/bin/sh\nsleep 30\n"})
    monkeypatch.setattr(ev.style, "CONFIG", world.cfg)
    world.set_entry(dict(ENTRY, screenshot=dict(SHOT, timeout_minutes=1)))
    real = ev.subprocess.Popen.communicate

    def quick(self, input=None, timeout=None):
        return real(self, input, 0.3 if timeout else None)

    monkeypatch.setattr(ev.subprocess.Popen, "communicate", quick)
    assert world.produce() == 1
    assert "timeout_minutes" in capsys.readouterr().err
    assert world.devices() == {}


def test_a_failing_command_names_step_4_and_cleans_up(world, capsys):
    world.build(head_files={"fail.txt": "x"})
    assert world.produce() == 1
    assert "step 4" in capsys.readouterr().err
    assert world.devices() == {} and [l["state"] for l in world.leases()] == ["released"]
    assert sh("git", "rev-parse", "HEAD", cwd=world.wt) == world.head


def test_a_screen_missing_on_one_side_refuses(world, capsys):
    world.build(head_files={"screens.txt": "home\n"})
    assert world.produce() == 1
    err = capsys.readouterr().err
    assert "step 4" in err and "disagree" in err
    assert sh("git", "rev-parse", "HEAD", cwd=world.wt) == world.head
    assert world.forge["patches"] == 0


def test_an_image_over_the_cap_refuses_without_recompressing(world, capsys):
    world.build(base_files={"noise.txt": "x"})
    assert world.produce() == 1
    err = capsys.readouterr().err
    assert "step 5" in err and "exceeds the cap" in err
    assert world.devices() == {}
    assert sh("git", "rev-parse", "HEAD", cwd=world.wt) == world.head


def test_images_are_resized_to_point_resolution_and_stripped(world):
    world.build()
    assert world.produce() == 0
    assert set(world.sipslog.read_text().split()) == {"20"}     # 60px tall / scale 3
    sha = sh("git", "rev-parse", "HEAD", cwd=world.wt)
    data = subprocess.run(["git", "show", f"{sha}:{ED}/{today()}-pr{PR}/classic-dark/home-after.png"],
                          cwd=world.wt, capture_output=True).stdout
    assert b"tEXt" not in data and ev.style.png_problems(data, 50 * 1024) == []


def test_the_evidence_commit_is_not_made_when_the_head_moved(world, capsys):
    world.build()
    world.forge["head"] = "f" * 40
    assert world.produce() == 1
    assert "step 1" in capsys.readouterr().err
    assert world.sim_calls() == []


def test_head_must_be_the_checked_out_commit(world, capsys):
    world.build()
    assert world.produce(head=world.base) == 1
    assert "step 1" in capsys.readouterr().err


def test_an_approved_pull_request_is_never_pushed_to(world, capsys):
    world.build()
    world.forge["reviews"] = [{"state": "APPROVED", "commit_id": world.head}]
    assert world.produce() == 1
    assert "frozen" in capsys.readouterr().err
    assert sh("git", "rev-parse", "feat/reskin", cwd=world.remote) == world.head
    assert world.sim_calls() == []


def test_an_undeclared_repository_refuses(world, capsys):
    world.build()
    world.cfg.write_text(json.dumps({"v": 1, "repos": {}}))
    assert world.produce() == 1
    assert "declares no style paths" in capsys.readouterr().err


def test_a_second_round_replaces_the_earlier_folder_in_the_pull_request(world):
    world.build()
    assert world.produce(plan=world.plan()) == 0
    # a fix round: code changes on top of the evidence commit
    fix = world.commit({"color.txt": "90\n"}, "fix")
    sh("git", "push", "-q", "origin", "feat/reskin", cwd=world.wt)
    world.forge["head"] = fix
    assert world.produce(plan=world.plan(), head=fix) == 0
    sha = sh("git", "rev-parse", "HEAD", cwd=world.wt)
    folders = {p.split("/")[2] for p in world.remote_tree(sha) if p.startswith(ED + "/")}
    assert folders == {f"{today()}-0007-reskin"}                     # one folder, not two
    # the pull request's net change holds the one new folder, nothing removed
    net = sh("git", "diff", "--name-status", world.base, sha, cwd=world.wt).splitlines()
    assert {l.split("\t")[0] for l in net if ED in l} == {"A"}
    res = ev.style.verify(REPO, PR, "0007-reskin", ev.style.Git(world.wt / ".git"), world.base, sha)
    assert res["ok"], res["problems"]
    assert world.forge["body"].count(ev.BEGIN) == 1


def test_a_folder_that_exists_at_base_is_never_touched(world):
    old = f"{ED}/2026-01-01-0007-reskin"
    world.build(base_files={f"{old}/README.md": "history\n"})
    assert world.produce(plan=world.plan()) == 0
    assert (world.wt / old / "README.md").read_text() == "history\n"
    assert sh("git", "diff", "--name-only", world.base, "HEAD", cwd=world.wt).count(old) == 0
    # the base's folder stays in the pull request's tree beside the new one
    assert f"{old}/README.md" in world.remote_tree("feat/reskin")


def test_the_body_section_is_replaced_and_the_implementers_text_kept(world):
    world.build()
    world.forge["body"] = ("Report line.\n\n" + ev.BEGIN + "\nstale grid\n" + ev.END
                           + "\n\nTrailing text.\n")
    assert world.produce() == 0
    body = world.forge["body"]
    assert "stale grid" not in body
    assert body.startswith("Report line.\n\n") and body.rstrip().endswith("Trailing text.")
    assert body.count(ev.BEGIN) == 1 and body.count(ev.END) == 1


def test_a_run_without_a_plan_names_the_folder_for_the_pull_request(world):
    world.build()
    assert world.produce() == 0
    assert (world.wt / f"{ED}/{today()}-pr{PR}/README.md").is_file()
    sha = sh("git", "rev-parse", "HEAD", cwd=world.wt)
    assert ev.style.verify(REPO, PR, "none", ev.style.Git(world.wt / ".git"),
                           world.base, sha)["ok"]


def test_the_simulator_is_deleted_when_the_create_fails(world, monkeypatch, capsys):
    world.build()
    monkeypatch.setenv("STUB_CREATE_FAIL", "1")
    assert world.produce() == 1
    assert "step 3" in capsys.readouterr().err
    assert world.devices() == {} and [l["state"] for l in world.leases()] == ["released"]


def test_a_held_lease_is_reused_and_left_alone(world):
    world.build()
    import socket
    host = socket.gethostname().split(".")[0].lower()
    d = world.fleet / "leases"
    d.mkdir(parents=True)
    (d / f"sim--{host}--001.json").write_text(json.dumps(
        {"id": f"sim--{host}--001", "resource": {"type": "sim", "host": host},
         "holder": "evidence-test", "granted_by": "x", "state": "active", "ttl_minutes": 240,
         "created": "2026-08-28T00:00:00Z", "activated": "2026-08-28T00:00:00Z", "note": ""}))
    assert world.produce() == 0
    assert [l["state"] for l in world.leases()] == ["active"]
    assert world.devices() == {}
