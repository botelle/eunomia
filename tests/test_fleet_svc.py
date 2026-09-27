"""Tests for fleet-svc — the `service` lease gate (plan 0029 §4).

Run: python3 -m pytest tests/test_fleet_svc.py -q
"""
import atexit
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
HOST = socket.gethostname().split(".")[0].lower()

# ---------------------------------------------------------------- process hygiene
# Real `sleep` children back the pid-reresolution tests (fleet-pkill's own
# pattern: os.kill against a REAL pid, never a fake one). One atexit sweep
# covers a collection error or interrupt that skips a test's own cleanup.
_SLEEPERS = []


def _spawn_sleeper():
    p = subprocess.Popen(["sleep", "90"])
    _SLEEPERS.append(p.pid)
    return p


@atexit.register
def _reap_stragglers():
    for pid in _SLEEPERS:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _lstart(pid):
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                       capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C"})
    return r.stdout.strip()


# ---------------------------------------------------------------- fixtures

def _conf(tmp_path, rows):
    p = tmp_path / "services.conf"
    p.write_text("\n".join(rows) + "\n")
    return p


def _row(key=f"{HOST}:thing", supervisor="launchd", domain="gui",
        primary="com.example.thing", also="-", config="-"):
    return (f"service: {key} supervisor={supervisor} domain={domain} "
           f"primary={primary} also={also} config={config}")


def _stub_launchctl(tmp_path, pid=4242, fail_print=False, log="argv.log"):
    stub = tmp_path / "launchctl-stub.sh"
    logf = tmp_path / log
    print_line = "exit 1" if fail_print else f'echo "    pid = {pid}"'
    stub.write_text(f"""#!/bin/bash
echo "$@" >> {logf}
case "$1" in
  print) {print_line} ;;
  *) exit 0 ;;
esac
""")
    stub.chmod(0o755)
    return str(stub), logf


def _stub_systemctl(tmp_path, pid=4242, fail_show=False, log="systemctl-argv.log"):
    stub = tmp_path / "systemctl-stub.sh"
    logf = tmp_path / log
    show_line = "exit 1" if fail_show else f'echo "MainPID={pid}"'
    stub.write_text(f"""#!/bin/bash
echo "$@" >> {logf}
if [[ "$*" == *"show"* ]]; then {show_line}
else exit 0
fi
""")
    stub.chmod(0o755)
    return str(stub), logf


def _env(tmp_path, session="sess-a", conf=None, launchctl=None, systemctl=None,
         ledger_host=None):
    fleet = tmp_path / "fleet"
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet), EUNOMIA_SESSION=session,
              FLEET_SERVICES_CONF=str(conf or _conf(tmp_path, [_row()])))
    if launchctl:
        env["FLEET_LAUNCHCTL"] = launchctl
    if systemctl:
        env["FLEET_SYSTEMCTL"] = systemctl
    if ledger_host:
        env["EUNOMIA_LEDGER_HOST"] = ledger_host
    else:
        env.pop("EUNOMIA_LEDGER_HOST", None)
    return env, fleet


def _run(args, env):
    return subprocess.run([sys.executable, str(BIN / "fleet-svc")] + args,
                          env=env, capture_output=True, text=True)


def _lease(fleet, lease_id, resource, holder, state="active", ttl=60,
          binding=None, activated="2026-09-09T00:00:00Z"):
    d = fleet / "leases"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": lease_id, "resource": resource, "holder": holder,
          "granted_by": "coord", "state": state, "ttl_minutes": ttl,
          "created": "2026-09-09T00:00:00Z", "activated": activated, "note": ""}
    if binding is not None:
        rec["binding"] = binding
    (d / f"{lease_id}.json").write_text(json.dumps(rec))
    return rec


def _read_lease(fleet, lease_id):
    return json.loads((fleet / "leases" / f"{lease_id}.json").read_text())


def _resource(key=f"{HOST}:thing"):
    host = key.split(":", 1)[0]
    return {"type": "service", "host": host, "service": key}


def _lease_id(key=f"{HOST}:thing", n=1):
    return f"service--{key.replace(':', '-')}--{n:03d}"


def _load_mod(tmp_path, conf, session="sess-a"):
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    os.environ["EUNOMIA_SESSION"] = session
    os.environ["FLEET_SERVICES_CONF"] = str(conf)
    os.environ.pop("EUNOMIA_LEDGER_HOST", None)
    loader = importlib.machinery.SourceFileLoader("fleet_svc", str(BIN / "fleet-svc"))
    spec = importlib.util.spec_from_loader("fleet_svc", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- unenrolled / wrong host

def test_unenrolled_name_refuses_and_stub_records_nothing(tmp_path):
    conf = _conf(tmp_path, [_row()])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
    r = _run(["start", f"{HOST}:nope"], env)
    assert r.returncode != 0 and "not enrolled" in r.stderr
    assert not log.exists() or log.read_text() == ""


def test_wrong_host_refuses_and_names_both(tmp_path):
    conf = _conf(tmp_path, [_row(key="otherhost:thing")])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
    r = _run(["start", "otherhost:thing"], env)
    assert r.returncode != 0
    assert "otherhost" in r.stderr and HOST in r.stderr
    assert not log.exists() or log.read_text() == ""


# ---------------------------------------------------------------- no lease / wrong holder

def test_no_lease_no_action_across_verbs(tmp_path):
    conf = _conf(tmp_path, [_row()])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
    (fleet / "leases").mkdir(parents=True)     # ledger reachable, just empty
    for verb in ("start", "stop", "kickstart", "bootout", "kill", "edit-config"):
        r = _run([verb, f"{HOST}:thing"], env)
        assert r.returncode != 0, verb
        assert "no ACTIVE service lease" in r.stderr, (verb, r.stderr)
    assert not log.exists() or log.read_text() == ""


def test_wrong_holder_no_action(tmp_path):
    conf = _conf(tmp_path, [_row()])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub, session="sess-b")
    lid = _lease_id()
    _lease(fleet, lid, _resource(), "sess-a")
    r = _run(["start", f"{HOST}:thing"], env)
    assert r.returncode != 0
    assert "held instead by" in r.stderr and "sess-a" in r.stderr
    assert not log.exists() or log.read_text() == ""


def test_never_reaps_stop_and_kill_refuse_on_foreign_binding(tmp_path):
    conf = _conf(tmp_path, [_row()])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub, session="sess-b")
    lid = _lease_id()
    _lease(fleet, lid, _resource(), "sess-a",
          binding={"pid": 4242, "started_at": "x", "label": "com.example.thing",
                   "config_sha": None, "bound_at": "y"})
    for verb in ("stop", "kill"):
        r = _run([verb, f"{HOST}:thing"], env)
        assert r.returncode != 0, verb
    assert not log.exists() or log.read_text() == ""


# ---------------------------------------------------------------- step 4: two active leases

def test_step4_refuses_both_holders_when_two_leases_are_active(tmp_path):
    """The measurement from plan §2.8: past the ttl, fleet-claim's own orphan
    skip grants a second --assign, so two ACTIVE rows on one resource is
    reachable and BOTH holders individually satisfy step 3. Step 4 is the
    only thing that turns this into a refusal instead of a second daemon."""
    conf = _conf(tmp_path, [_row()])
    stub, log = _stub_launchctl(tmp_path)
    env_a, fleet = _env(tmp_path, conf=conf, launchctl=stub, session="sess-a")
    env_b, _ = _env(tmp_path, conf=conf, launchctl=stub, session="sess-b")
    res = _resource()
    _lease(fleet, _lease_id(n=1), res, "sess-a")
    _lease(fleet, _lease_id(n=2), res, "sess-b")
    for env in (env_a, env_b):
        r = _run(["start", f"{HOST}:thing"], env)
        assert r.returncode != 0
        assert "MORE THAN ONE active lease" in r.stderr
        assert "service--" in r.stderr
    assert not log.exists() or log.read_text() == ""


def test_ttl_measurement_reproduces_dual_active_via_real_fleet_claim(tmp_path):
    """Regression harness for the exact §2.8 sequence, using the REAL
    fleet-claim (untouched by this plan): assign+activate as sess-a, age its
    heartbeat past the ttl, assign+activate as sess-b on the SAME resource —
    granted, because fleet-claim's orphan skip is deliberate. fleet-svc's
    step 4 is what fleet-claim is not supposed to be."""
    fleet = tmp_path / "fleet"

    def claim(session, args):
        env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet), EUNOMIA_SESSION=session)
        return subprocess.run([sys.executable, str(BIN / "fleet-claim")] + args,
                              env=env, capture_output=True, text=True)

    resource = json.dumps(_resource())
    r = claim("sess-a", ["--assign", resource, "--holder", "sess-a", "--ttl", "60"])
    assert r.returncode == 0, r.stderr
    lid_a = r.stdout.strip()
    assert claim("sess-a", ["--activate", lid_a]).returncode == 0

    hb = fleet / "sessions" / "sess-a" / "hb"
    old = datetime.now(timezone.utc) - timedelta(minutes=90)
    os.utime(hb, (old.timestamp(), old.timestamp()))

    r2 = claim("sess-b", ["--assign", resource, "--holder", "sess-b", "--ttl", "60"])
    assert r2.returncode == 0, ("orphan skip must still grant a second --assign", r2.stderr)
    lid_b = r2.stdout.strip()
    assert claim("sess-b", ["--activate", lid_b]).returncode == 0

    both = {_read_lease(fleet, lid_a)["state"], _read_lease(fleet, lid_b)["state"]}
    assert both == {"active"}, "both rows are ACTIVE — the reachable state step 4 exists for"


# ---------------------------------------------------------------- identity, labels, domain

def test_identity_is_unit_a_second_holder_on_the_same_unit_is_refused(tmp_path):
    fleet = tmp_path / "fleet"

    def claim(session, args):
        env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet), EUNOMIA_SESSION=session)
        return subprocess.run([sys.executable, str(BIN / "fleet-claim")] + args,
                              env=env, capture_output=True, text=True)

    resource = json.dumps({"type": "service", "host": "cihost",
                           "service": "cihost:forgejo-runner"})
    r1 = claim("sess-a", ["--assign", resource, "--holder", "sess-a"])
    assert r1.returncode == 0, r1.stderr
    r2 = claim("sess-b", ["--assign", resource, "--holder", "sess-b"])
    assert r2.returncode != 0
    assert "already holds this resource" in r2.stderr


def test_second_assign_refused_when_binding_is_already_written(tmp_path):
    """The regression test for §2.5: binding must live at the lease's TOP
    LEVEL. If it were written into `resource` (fleet-sim's placement), the
    resource dict would no longer equal the bare identity JSON and
    fleet-claim's exact-equality conflict check would silently stop
    matching — this is the measured `--002` hole the plan's boundary names."""
    fleet = tmp_path / "fleet"
    lid = "service--cihost-forgejo-runner--001"
    _lease(fleet, lid, {"type": "service", "host": "cihost",
                       "service": "cihost:forgejo-runner"}, "sess-a",
          binding={"pid": 4242, "started_at": "x", "label": "org.eunomia.forgejo-runner",
                   "config_sha": None, "bound_at": "y"})
    # a fresh heartbeat: liveness must be CURRENT, or fleet-claim's own orphan
    # skip (deliberate, §2.8) grants the second --assign for an unrelated
    # reason and this test would prove nothing about binding placement.
    hb = fleet / "sessions" / "sess-a" / "hb"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.touch()
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet), EUNOMIA_SESSION="sess-c")
    resource = json.dumps({"type": "service", "host": "cihost",
                           "service": "cihost:forgejo-runner"})
    r = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--assign", resource,
                        "--holder", "sess-c"], env=env, capture_output=True, text=True)
    assert r.returncode != 0, ("a lease with a binding must still refuse a "
                              "conflicting --assign", r.stdout)
    assert "already holds this resource" in r.stderr


def test_two_label_row_starts_only_the_primary_label(tmp_path):
    conf = _conf(tmp_path, [_row(also="com.example.thing.secondary")])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
    _lease(fleet, _lease_id(), _resource(), "sess-a")
    r = _run(["start", f"{HOST}:thing"], env)
    assert r.returncode == 0, r.stderr
    lines = [l for l in log.read_text().splitlines() if l]
    # one kickstart (the action) + one print (pid resolution for the bind) —
    # never a second invocation for the `also` label (the two-daemon fault,
    # performed under a lease).
    assert len(lines) == 2, lines
    assert not any("secondary" in l for l in lines)
    assert any(l.startswith("kickstart") and "com.example.thing" in l for l in lines)

    r2 = _run(["status", f"{HOST}:thing"], env)
    assert "com.example.thing.secondary" in r2.stdout, "also= is reported"


def test_domain_gui_vs_system_target_strings_differ(tmp_path):
    conf = _conf(tmp_path, [
        _row(key=f"{HOST}:g", domain="gui", primary="com.example.g"),
        _row(key=f"{HOST}:s", domain="system", primary="com.example.s"),
    ])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
    _lease(fleet, "service--" + HOST + "-g--001",
          {"type": "service", "host": HOST, "service": f"{HOST}:g"}, "sess-a")
    _lease(fleet, "service--" + HOST + "-s--001",
          {"type": "service", "host": HOST, "service": f"{HOST}:s"}, "sess-a")
    assert _run(["start", f"{HOST}:g"], env).returncode == 0
    assert _run(["start", f"{HOST}:s"], env).returncode == 0
    lines = [l for l in log.read_text().splitlines() if l]
    assert any("gui/" in l and "com.example.g" in l for l in lines)
    assert any(l.strip().startswith("kickstart system/com.example.s") for l in lines)


def test_systemd_row_drives_systemctl_never_launchctl(tmp_path):
    conf = _conf(tmp_path, [_row(key=f"{HOST}:svc", supervisor="systemd",
                                 domain="system", primary="svc.service")])
    lstub, llog = _stub_launchctl(tmp_path)
    sstub, slog = _stub_systemctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=lstub, systemctl=sstub)
    _lease(fleet, "service--" + HOST + "-svc--001",
          {"type": "service", "host": HOST, "service": f"{HOST}:svc"}, "sess-a")
    r = _run(["start", f"{HOST}:svc"], env)
    assert r.returncode == 0, r.stderr
    assert not llog.exists() or llog.read_text() == ""
    assert "start svc.service" in slog.read_text()


# ---------------------------------------------------------------- binding

def test_bind_writes_fields_and_holder_change_under_lock_aborts(tmp_path):
    conf = _conf(tmp_path, [_row()])
    mod = _load_mod(tmp_path, conf)
    fleet = tmp_path / "fleet"
    lid = _lease_id()
    _lease(fleet, lid, _resource(), "sess-a")
    binding = {"pid": 111, "started_at": "t1", "label": "com.example.thing",
              "config_sha": "abc", "bound_at": "2026-09-09T00:00:00Z"}
    assert mod._bind_locked(lid, "sess-a", binding) is True
    rec = _read_lease(fleet, lid)
    assert rec["binding"] == binding
    assert rec["resource"] == _resource(), "resource must stay byte-identical"

    # holder changed under the lock (a takeover, say) -> bind aborts, no write
    rec["holder"] = "sess-z"
    (fleet / "leases" / f"{lid}.json").write_text(json.dumps(rec))
    ok = mod._bind_locked(lid, "sess-a", {"pid": 222, "started_at": "t2",
                                          "label": "x", "config_sha": None,
                                          "bound_at": "z"})
    assert ok is False
    assert _read_lease(fleet, lid)["binding"] == binding, "aborted bind must not write"


def test_successful_start_binds_pid_label_and_preserves_prior_sha(tmp_path):
    conf = _conf(tmp_path, [_row()])
    stub, log = _stub_launchctl(tmp_path, pid=5150)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
    lid = _lease_id()
    _lease(fleet, lid, _resource(), "sess-a",
          binding={"pid": 1, "started_at": "old", "label": "com.example.thing",
                   "config_sha": "keep-me", "bound_at": "old"})
    r = _run(["start", f"{HOST}:thing"], env)
    assert r.returncode == 0, r.stderr
    rec = _read_lease(fleet, lid)
    assert rec["binding"]["pid"] == 5150
    assert rec["binding"]["label"] == "com.example.thing"
    assert rec["binding"]["config_sha"] == "keep-me"
    assert rec["resource"] == _resource()


# ---------------------------------------------------------------- kill re-resolution

def test_kill_refuses_on_pid_mismatch_and_sends_no_signal(tmp_path):
    proc = _spawn_sleeper()
    try:
        real_lstart = _lstart(proc.pid)
        conf = _conf(tmp_path, [_row()])
        stub, log = _stub_launchctl(tmp_path, pid=proc.pid)
        env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
        lid = _lease_id()
        _lease(fleet, lid, _resource(), "sess-a",
              binding={"pid": 999999, "started_at": "bogus",
                       "label": "com.example.thing", "config_sha": None,
                       "bound_at": "x"})
        r = _run(["kill", f"{HOST}:thing"], env)
        assert r.returncode != 0
        assert "refusing to signal" in r.stderr
        assert proc.poll() is None, "the mismatched pid must not be signaled"

        # same pid, different (fresher) start time — also a mismatch
        _lease(fleet, lid, _resource(), "sess-a",
              binding={"pid": proc.pid, "started_at": "not-" + real_lstart,
                       "label": "com.example.thing", "config_sha": None,
                       "bound_at": "x"})
        r2 = _run(["kill", f"{HOST}:thing"], env)
        assert r2.returncode != 0
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_kill_succeeds_and_rebinds_when_pid_and_start_match(tmp_path):
    proc = _spawn_sleeper()
    try:
        real_lstart = _lstart(proc.pid)
        conf = _conf(tmp_path, [_row()])
        stub, log = _stub_launchctl(tmp_path, pid=proc.pid)
        env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
        lid = _lease_id()
        _lease(fleet, lid, _resource(), "sess-a",
              binding={"pid": proc.pid, "started_at": real_lstart,
                       "label": "com.example.thing", "config_sha": None,
                       "bound_at": "x"})
        r = _run(["kill", f"{HOST}:thing"], env)
        assert r.returncode == 0, r.stderr
        time.sleep(0.3)
        assert proc.poll() is not None, "the matching pid must be signaled"
        rec = _read_lease(fleet, lid)
        assert rec["binding"]["pid"] == proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------- config sha / drift

def test_edit_config_updates_sha_then_raw_edit_shows_drift(tmp_path):
    cfg = tmp_path / "runner-config.yaml"
    cfg.write_text("v1\n")
    conf = _conf(tmp_path, [_row(config=str(cfg))])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
    lid = _lease_id()
    _lease(fleet, lid, _resource(), "sess-a")

    r = _run(["edit-config", f"{HOST}:thing"], env)
    assert r.returncode == 0, r.stderr
    assert not log.exists() or log.read_text() == "", \
        "edit-config must never touch the supervisor"

    r2 = _run(["status", f"{HOST}:thing"], env)
    assert "config_sha=ok" in r2.stdout

    cfg.write_text("v2 -- edited behind the wrapper's back\n")
    r3 = _run(["status", f"{HOST}:thing"], env)
    assert "config_sha=DRIFT" in r3.stdout
    assert not log.exists() or log.read_text() == "", \
        "DRIFT is displayed, never acted on"


def test_status_shows_unknown_never_ok_when_config_unreadable(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    conf = _conf(tmp_path, [_row(config=str(missing))])
    env, fleet = _env(tmp_path, conf=conf)
    lid = _lease_id()
    _lease(fleet, lid, _resource(), "sess-a",
          binding={"pid": 1, "started_at": "x", "label": "com.example.thing",
                   "config_sha": "whatever", "bound_at": "y"})
    r = _run(["status", f"{HOST}:thing"], env)
    assert "config_sha=UNKNOWN" in r.stdout
    assert "config_sha=ok" not in r.stdout


def test_edit_config_refuses_when_no_config_is_gated(tmp_path):
    conf = _conf(tmp_path, [_row(config="-")])
    env, fleet = _env(tmp_path, conf=conf)
    _lease(fleet, _lease_id(), _resource(), "sess-a")
    r = _run(["edit-config", f"{HOST}:thing"], env)
    assert r.returncode != 0 and "no gated config file" in r.stderr


# ---------------------------------------------------------------- fail closed

def test_fail_closed_when_ledger_dir_absent(tmp_path):
    conf = _conf(tmp_path, [_row()])
    stub, log = _stub_launchctl(tmp_path)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub)
    assert not (fleet / "leases").exists()
    for verb in ("start", "stop", "kill", "edit-config"):
        r = _run([verb, f"{HOST}:thing"], env)
        assert r.returncode != 0
        assert "ledger" in r.stderr.lower()
    assert not log.exists() or log.read_text() == ""
    # read verb still works
    r = _run(["status"], env)
    assert r.returncode == 0


def test_fail_closed_off_opshost_when_ssh_check_fails(tmp_path):
    conf = _conf(tmp_path, [_row()])
    stub, log = _stub_launchctl(tmp_path)
    sshstub = tmp_path / "ssh"
    sshstub.write_text("#!/bin/bash\nexit 1\n")     # -O check always fails
    sshstub.chmod(0o755)
    env, fleet = _env(tmp_path, conf=conf, launchctl=stub, ledger_host="opshost")
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    r = _run(["start", f"{HOST}:thing"], env)
    assert r.returncode != 0 and "ledger" in r.stderr.lower()
    assert not log.exists() or log.read_text() == ""
    r2 = _run(["status"], env)
    assert r2.returncode == 0
    assert "WARN" in r2.stderr


# ---------------------------------------------------------------- off-opshost connection budget

def _stub_ssh(tmp_path, lease_json, log="ssh.log"):
    logf = tmp_path / log
    ssh = tmp_path / "ssh"
    ssh.write_text(f"""#!/bin/bash
echo "$@" >> {logf}
if [[ "$*" == *"-O check"* ]]; then
  exit 0
fi
if [[ "$*" == *"--bind"* ]]; then
  echo "OK"
  exit 0
fi
echo '{lease_json}'
""")
    ssh.chmod(0o755)
    return logf


def test_off_opshost_makes_exactly_two_connecting_invocations(tmp_path):
    conf = _conf(tmp_path, [_row()])
    lstub, llog = _stub_launchctl(tmp_path, pid=4242)
    lease_json = json.dumps({"id": _lease_id(), "resource": _resource(),
                             "holder": "sess-a", "granted_by": "coord",
                             "state": "active", "ttl_minutes": 60,
                             "created": "2026-09-09T00:00:00Z",
                             "activated": "2026-09-09T00:00:00Z", "note": ""})
    sshlog = _stub_ssh(tmp_path, lease_json)
    env, fleet = _env(tmp_path, conf=conf, launchctl=lstub, ledger_host="opshost")
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]

    r = _run(["start", f"{HOST}:thing"], env)
    assert r.returncode == 0, r.stderr

    lines = [l for l in sshlog.read_text().splitlines() if l]
    checks = [l for l in lines if "-O check" in l]
    connecting = [l for l in lines if "ProxyCommand=false" in l]
    assert len(checks) == 2, lines
    assert len(connecting) == 2, lines
    for l in connecting:
        assert "ControlMaster=no" in l and "ProxyCommand=false" in l


def test_off_opshost_bind_internal_writes_the_local_ledger(tmp_path):
    """`--bind` is what the remote ssh command actually runs ON the ledger
    host — exercised directly here (as it would run under
    `ssh <ledger-host> python3 fleet-svc --bind ...`)."""
    conf = _conf(tmp_path, [_row()])
    env, fleet = _env(tmp_path, conf=conf)
    lid = _lease_id()
    _lease(fleet, lid, _resource(), "sess-a")
    binding = {"pid": 77, "started_at": "t", "label": "x",
              "config_sha": None, "bound_at": "y"}
    r = _run(["--bind", lid, json.dumps(binding)], env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"
    assert _read_lease(fleet, lid)["binding"] == binding
