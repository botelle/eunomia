"""fleet-deploy (plan 0087): the homefleet provider for dev.

Each test drives a real git origin + pinned worktree, a stub health server that
answers from a file IN the pin (so a commit decides whether it is healthy), and
a stub supervisor whose pid changes on restart. Nothing touches launchd, the
network, or the broker."""
import http.server
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
HOST = socket.gethostname().split(".")[0].lower()
EXPECT = "lynceus-ok"
GOOD = {"v": 1, "name": "lynceus",
        "health": {"path": "/healthz", "expect": "\"service\":\"lynceus\"", "timeout_s": 60},
        "deps": ["api/requirements.txt"], "secrets": ["kv/lynceus/token"]}

_ENV = ("FLEET_ENVIRONMENTS_CONF", "FLEET_SERVICES_CONF", "FLEET_DEPLOY_POLL",
        "EUNOMIA_FLEET_DIR", "EUNOMIA_SESSION", "EUNOMIA_LEDGER_HOST",
        "FLEET_NTFY_URL", "FLEET_PINS", "FLEET_LAUNCH_AGENTS_DIR")


def g(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class Rig:
    """origin + work clone + pin + health server + stub supervisor + module."""

    def __init__(self, tmp_path, monkeypatch, deps=("requirements.txt",)):
        self.tmp = tmp_path
        for k in _ENV:
            monkeypatch.delenv(k, raising=False)
        self.fleet = tmp_path / "fleet"
        monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(self.fleet))
        monkeypatch.setenv("FLEET_DEPLOY_POLL", "0.01")
        monkeypatch.setenv("FLEET_SPAWN", "popen")
        origin = tmp_path / "origin.git"
        self.work = tmp_path / "work"
        self.pin = tmp_path / "pin"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(self.work)],
                       check=True, capture_output=True)
        g(self.work, "config", "user.email", "t@t")
        g(self.work, "config", "user.name", "t")
        self.manifest = dict(GOOD, deps=list(deps))
        self.manifest["health"] = dict(GOOD["health"], expect=EXPECT, timeout_s=1)
        self.c1 = self.commit(body=EXPECT)
        g(self.work, "branch", "-M", "main")
        g(self.work, "push", "-q", "-u", "origin", "main")
        g(self.work, "worktree", "add", "-q", "--detach", str(self.pin), "origin/main")

        self.force = None                   # health body override
        rig = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = rig.force
                if body is None:
                    body = (rig.pin / "body.txt").read_text()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        (tmp_path / "services.conf").write_text(
            f"service: {HOST}:lynceus supervisor=launchd domain=gui "
            "primary=org.test.lynceus also=- config=-\n")
        (tmp_path / "environments.conf").write_text(
            f"bind: dev:lynceus provider=homefleet repo=operator/lynceus "
            f"service={HOST}:lynceus pin={self.pin} "
            f"base_url=http://127.0.0.1:{self.srv.server_address[1]}\n")
        monkeypatch.setenv("FLEET_SERVICES_CONF", str(tmp_path / "services.conf"))
        monkeypatch.setenv("FLEET_ENVIRONMENTS_CONF", str(tmp_path / "environments.conf"))

        self.mod = self._load()
        self.pids, self.restarts = [100], 0
        self.restart_changes_pid = True
        self.restart_fails = False
        self.pages = []
        m = self.mod
        m.watch.notify = lambda title, body, label=None: (
            self.pages.append((title, body)), True)[1]
        m.watch.broker_verdict = lambda repo: (m.watch.PROT_OK, "", None)
        m.svc._current_pid = lambda row: (self.pids[-1], "t")
        m.svc._run_supervisor = self._restart
        self.request_close = self.srv.shutdown

    def _load(self):
        loader = importlib.machinery.SourceFileLoader("fleet_deploy_t", str(BIN / "fleet-deploy"))
        spec = importlib.util.spec_from_loader("fleet_deploy_t", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def _restart(self, row, argv):
        self.restarts += 1
        if self.restart_fails:
            raise SystemExit("stub: restart failed")
        if self.restart_changes_pid:
            self.pids.append(self.pids[-1] + 1)

    def commit(self, body=EXPECT, manifest=None, extra=None):
        (self.work / "deploy").mkdir(exist_ok=True)
        (self.work / "deploy" / "manifest.json").write_text(
            json.dumps(manifest if manifest is not None else self.manifest))
        (self.work / "body.txt").write_text(body)
        for name, text in (extra or {}).items():
            (self.work / name).write_text(text)
        g(self.work, "add", "-A")
        g(self.work, "commit", "-q", "--allow-empty", "-m", "c " + os.urandom(3).hex())
        return g(self.work, "rev-parse", "HEAD")

    def push(self, force=False, **kw):
        sha = self.commit(**kw)
        g(self.work, "push", "-q", *(["-f"] if force else []), "origin", "main")
        return sha

    def head(self):
        return g(self.pin, "rev-parse", "HEAD")

    def run(self, **kw):
        return self.mod.run(**kw)["dev:lynceus"]

    def events(self):
        p = self.fleet / "events.jsonl"
        if not p.exists():
            return []
        evs = [json.loads(x) for x in p.read_text().splitlines()]
        return [(e["type"], e["sha"], e["detail"].get("kind")) for e in evs
                if e["type"].startswith("deploy-")]

    def raw_events(self):
        return [json.loads(x) for x in (self.fleet / "events.jsonl").read_text().splitlines()]

    def leases(self):
        return [json.loads(p.read_text()) for p in (self.fleet / "leases").glob("*.json")]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.srv.shutdown()


def seed(r):
    """First cycle at a pin already on main: records the baseline."""
    assert r.run() == "baseline"
    assert r.events() == [("deploy-verified", r.c1, "baseline")]


# ------------------------------------------------------------------ D1

@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m["health"].pop("expect"), "health.expect"),
    (lambda m: m["health"].update(expect=""), "health.expect"),
    (lambda m: m["health"].update(expect="  "), "health.expect"),
    (lambda m: m.pop("health"), "health is required"),
    (lambda m: m.update(v=2), "v must be 1"),
    (lambda m: m.pop("v"), "v must be 1"),
    (lambda m: m.update(host="opshost"), "unknown key 'host'"),
    (lambda m: m.update(provider="homefleet"), "unknown key 'provider'"),
    (lambda m: m["health"].update(url="http://x"), "unknown key health.url"),
    (lambda m: m["health"].update(path="http://192.0.2.5:8795/healthz"), "health.path"),
    (lambda m: m["health"].update(expect="192.0.2.5"), "names a host"),
    (lambda m: m.update(start=["org.eunomia.lynceus"]), "names a host"),
    (lambda m: m.update(deps=["/etc/passwd"]), "inside the repository"),
    (lambda m: m["health"].update(timeout_s=0), "timeout_s"),
    (lambda m: m.update(deps="api/requirements.txt"), "deps must be a list"),
])
def test_manifest_refusals(rig, mutate, needle):
    m = json.loads(json.dumps(GOOD))
    mutate(m)
    errs = rig.mod.validate_manifest(m)
    assert any(needle in e for e in errs), errs


def test_manifest_example_is_accepted_and_artifact_start_are_optional(rig):
    assert rig.mod.validate_manifest(GOOD) == []
    assert rig.mod.validate_manifest(dict(GOOD, artifact="repo", start=["/x/run"])) == []


def test_validate_cli(tmp_path):
    ok, bad = tmp_path / "ok.json", tmp_path / "bad.json"
    ok.write_text(json.dumps(GOOD))
    m = json.loads(json.dumps(GOOD))
    del m["health"]["expect"]
    bad.write_text(json.dumps(m))
    def cli(p):
        return subprocess.run([sys.executable, str(BIN / "fleet-deploy"), "validate", str(p)],
                              capture_output=True, text=True)
    assert cli(ok).returncode == 0
    r = cli(bad)
    assert r.returncode == 1 and "health.expect" in r.stderr


# ------------------------------------------------------------------ D2

def test_binding_must_name_an_enrolled_service(rig, tmp_path):
    conf = tmp_path / "e2.conf"
    conf.write_text("bind: dev:x provider=homefleet repo=operator/x service=opshost:nope "
                    "pin=/p base_url=http://h:1\n")
    with pytest.raises(SystemExit) as e:
        rig.mod.parse_bindings(conf)
    assert "not enrolled" in str(e.value)


@pytest.mark.parametrize("line,needle", [
    ("bind: prod:x provider=homefleet repo=a/b service={h}:lynceus pin=/p base_url=http://h:1", "dev:<name>"),
    ("bind: dev:x provider=cloud repo=a/b service={h}:lynceus pin=/p base_url=http://h:1", "homefleet"),
    ("bind: dev:x provider=homefleet repo=a/b service={h}:lynceus pin=rel base_url=http://h:1", "absolute"),
    ("bind: dev:x provider=homefleet repo=a/b service={h}:lynceus pin=/p", "missing"),
])
def test_binding_refusals(rig, tmp_path, line, needle):
    conf = tmp_path / "e3.conf"
    conf.write_text(line.format(h=HOST) + "\n")
    with pytest.raises(SystemExit) as e:
        rig.mod.parse_bindings(conf)
    assert needle in str(e.value)


# ------------------------------------------------------------------ D3 / D4

def test_first_run_records_a_baseline_when_healthy(rig):
    seed(rig)
    assert rig.restarts == 0 and rig.pages == []


def test_first_run_refuses_and_pages_when_unhealthy(rig):
    rig.force = "an access page"
    assert rig.run() == "refused: baseline"
    assert rig.events() == [] and len(rig.pages) == 1
    assert rig.run() == "refused: baseline"          # standing: still one page
    assert len(rig.pages) == 1


def test_forward_deploy_verifies_with_events_in_order(rig):
    seed(rig)
    c2 = rig.push()
    assert rig.run() == "verified"
    assert rig.head() == c2
    assert rig.events() == [("deploy-verified", rig.c1, "baseline"),
                            ("deploy-started", c2, "deploy"),
                            ("deploy-verified", c2, "deploy")]
    assert rig.restarts == 1 and rig.pages == []
    assert all(e["actor"] == "svc:fleet-deploy" for e in rig.raw_events()
               if e["type"].startswith("deploy-"))
    assert all(l["state"] == "released" for l in rig.leases()
               if l["holder"] == "fleet-deploy")
    assert rig.run() == "current"                    # nothing further to do


def test_bad_body_rolls_back_to_the_verified_sha_reverifies_and_pages(rig):
    seed(rig)
    c2 = rig.push(body="a login page")
    assert rig.run() == "rolled-back"
    assert rig.head() == rig.c1
    assert rig.events() == [("deploy-verified", rig.c1, "baseline"),
                            ("deploy-started", c2, "deploy"),
                            ("deploy-started", rig.c1, "rollback"),
                            ("deploy-verified", rig.c1, "rollback")]
    assert rig.restarts == 2
    assert len(rig.pages) == 1 and "rolled back" in rig.pages[0][0].lower()


def test_failed_rollback_stops_with_no_retry_next_cycle(rig):
    seed(rig)
    rig.push(body="broken")
    rig.force = "broken"                             # even the old SHA fails now
    assert rig.run() == "rollback-failed"
    n, restarts = len(rig.events()), rig.restarts
    assert "rollback failed" in rig.pages[-1][0].lower()
    assert rig.run() == "skipped: already attempted"
    assert len(rig.events()) == n and rig.restarts == restarts
    assert all(l["state"] == "released" for l in rig.leases())


def test_a_failed_commit_is_not_redeployed_but_a_newer_one_is(rig):
    seed(rig)
    rig.push(body="broken")
    assert rig.run() == "rolled-back"
    restarts = rig.restarts
    assert rig.run() == "skipped: already attempted"
    assert rig.restarts == restarts
    c3 = rig.push(body=EXPECT)
    assert rig.run() == "verified"
    assert rig.head() == c3


def test_retry_ignores_the_skip_for_that_sha(rig):
    seed(rig)
    c2 = rig.push(body="broken")
    assert rig.run() == "rolled-back"
    rig.mod.run(binding="dev:lynceus", retry=True, sha=c2[:7])
    started = [e for e in rig.events() if e[:2] == ("deploy-started", c2)]
    assert len(started) == 2                         # tried again on request


def test_retry_with_a_sha_that_is_not_main_refuses(rig):
    seed(rig)
    rig.push()
    assert rig.mod.run(binding="dev:lynceus", retry=True, sha="deadbee") == {
        "dev:lynceus": "sha-mismatch"}


def test_a_refused_lease_claim_leaves_the_pin_unmoved(rig):
    seed(rig)
    rig.push()
    claim, os_env = rig.mod.claim, os.environ
    os_env["EUNOMIA_SESSION"] = "someone-else"
    rig.mod.lib.touch_heartbeat("someone-else")
    res = rig.mod._quiet(claim.main, ["--assign", json.dumps(
        {"type": "service", "host": HOST, "service": f"{HOST}:lynceus"}),
        "--holder", "someone-else"])
    assert res[0] == 0
    os_env["EUNOMIA_SESSION"] = "fleet-deploy"
    assert rig.run() == "lease-refused"
    assert rig.head() == rig.c1 and rig.restarts == 0
    assert [e[0] for e in rig.events()] == ["deploy-verified"]
    assert rig.pages == []


def test_a_standing_refusal_pages_once_not_per_cycle(rig):
    seed(rig)
    rig.push()
    (rig.pin / "body.txt").write_text("local edit")
    for _ in range(3):
        assert rig.run() == "refused: dirty"
    assert len(rig.pages) == 1
    assert rig.head() == rig.c1


def test_a_body_that_passes_with_an_unchanged_pid_is_not_verified(rig):
    seed(rig)
    c2 = rig.push()
    rig.restart_changes_pid = False                  # the old process still answers
    assert rig.run() == "rollback-failed"
    assert ("deploy-verified", c2, "deploy") not in rig.events()


def test_a_refused_restart_counts_as_a_failed_check(rig):
    seed(rig)
    c2 = rig.push()
    rig.restart_fails = True
    assert rig.run() == "rollback-failed"
    assert rig.head() == rig.c1, "the pin must not sit at a SHA that is not running"
    assert ("deploy-verified", c2, "deploy") not in rig.events()


def test_a_deps_change_pages_and_leaves_the_pin_unmoved(rig):
    seed(rig)
    rig.push(extra={"requirements.txt": "flask\n"})
    assert rig.run() == "refused: deps"
    assert rig.head() == rig.c1 and rig.restarts == 0
    assert "host preparation needed" in rig.pages[0][1]
    rig.run()
    assert len(rig.pages) == 1


def test_a_non_descendant_target_is_refused(rig):
    seed(rig)
    c2 = rig.push()
    g(rig.pin, "fetch", "-q", "origin")
    g(rig.pin, "checkout", "-q", "--detach", c2)      # the pin sits on c2 ...
    g(rig.work, "reset", "-q", "--hard", rig.c1)      # ... and main is rewritten
    rig.push(force=True, extra={"other.txt": "diverged\n"})
    assert rig.run() == "refused: not-descendant"
    assert rig.head() == c2 and rig.restarts == 0
    assert len(rig.pages) == 1


def test_a_dirty_tree_is_refused(rig):
    seed(rig)
    rig.push()
    (rig.pin / "body.txt").write_text("tampered")
    assert rig.run() == "refused: dirty"
    assert rig.head() == rig.c1 and rig.restarts == 0


def test_an_unvouched_main_is_refused(rig):
    seed(rig)
    rig.push()
    rig.mod.watch.broker_verdict = lambda repo: (rig.mod.watch.PROT_UNKNOWN, "down", None)
    assert rig.run() == "refused: broker"
    assert rig.head() == rig.c1


def test_a_target_whose_manifest_does_not_validate_is_refused(rig):
    seed(rig)
    bad = json.loads(json.dumps(rig.manifest))
    del bad["health"]["expect"]
    rig.push(manifest=bad)
    assert rig.run() == "refused: manifest"
    assert rig.head() == rig.c1


def test_a_manifest_start_that_disagrees_with_the_unit_is_refused(rig, tmp_path, monkeypatch):
    import plistlib
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "org.test.lynceus.plist").write_bytes(
        plistlib.dumps({"Label": "org.test.lynceus", "ProgramArguments": ["/bin/a"]}))
    monkeypatch.setenv("FLEET_LAUNCH_AGENTS_DIR", str(la))
    seed(rig)
    rig.push(manifest=dict(rig.manifest, start=["/bin/other"]))
    assert rig.run() == "refused: manifest"
    rig.push(manifest=dict(rig.manifest, start=["/bin/a"]))
    assert rig.run() == "verified"


def test_dry_run_changes_nothing_and_emits_nothing(rig, capsys):
    seed(rig)
    rig.push()
    before = (rig.head(), len(rig.raw_events()), rig.restarts)
    assert rig.run(dry_run=True) == "dry-run"
    assert (rig.head(), len(rig.raw_events()), rig.restarts) == before
    assert rig.leases() == [] and rig.pages == []
    assert "DRY-RUN would claim the service lease" in capsys.readouterr().out


def test_a_binding_for_another_host_is_left_alone(rig, tmp_path):
    seed(rig)
    rig.push()
    b = rig.mod.parse_bindings()["dev:lynceus"]
    b["host"] = "elsewhere"
    assert rig.mod.cycle(b).startswith("skipped")
    assert rig.head() == rig.c1


def test_overlapping_runs_are_refused_by_the_flock(rig):
    fd = rig.mod._flock()
    try:
        assert rig.mod.run() == {}
    finally:
        os.close(fd)


def test_the_session_id_the_lease_uses_is_valid(rig):
    assert rig.mod.lib._SID_RE.match(rig.mod.SESSION)
    assert rig.mod.ACTOR == "svc:fleet-deploy"
