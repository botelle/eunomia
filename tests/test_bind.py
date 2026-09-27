"""Tests for fleet-bind (plan 0012) — the SessionStart hook that binds a
session id to the OS process serving it.

`ps` is faked via a PATH shim (same idiom as FLEET_SIMCTL / test_fleet_svc's
fakebin): fleetlib.proc_table() and fleetlib._lstart() both shell out to a
literal `ps`, so a fake `ps` on PATH controls the whole ancestry walk without
touching the real process table. The one exception is the self-kill
regression test, which deliberately runs fleet-pkill for real (see below).
"""
import itertools
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"

_FAKE_PS_BODY = '''
import json, os, sys
data = json.loads(open(os.environ["FAKE_PS_DATA"]).read())
argv = sys.argv[1:]
if argv[:1] == ["-axo"]:
    for row in data.get("table", []):
        pid, ppid, comm = row
        sys.stdout.write(f"{pid} {ppid} {comm}\\n")
elif "-p" in argv:
    pid = argv[argv.index("-p") + 1]
    ls = data.get("lstart", {}).get(str(pid))
    if ls:
        sys.stdout.write(ls + "\\n")
sys.exit(0)
'''

_counter = itertools.count()


def _fakebin(tmp_path):
    d = tmp_path / "fakebin"
    exe = d / "ps"
    if not exe.exists():
        d.mkdir(exist_ok=True)
        exe.write_text(f"#!{sys.executable}\n" + _FAKE_PS_BODY)
        exe.chmod(0o755)
    return d


def _bind(tmp_path, fleet_dir, stdin_data=None, table=None, lstart=None, env_extra=None):
    fakebin = _fakebin(tmp_path)
    data_path = tmp_path / f"psdata-{next(_counter)}.json"
    data_path.write_text(json.dumps({"table": table or [], "lstart": lstart or {}}))
    env = dict(os.environ,
               EUNOMIA_FLEET_DIR=str(fleet_dir),
               PATH=f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
               FAKE_PS_DATA=str(data_path),
               HOME=str(tmp_path))          # keep the ~/Library/Logs fallback in tmp_path
    env.pop("EUNOMIA_SESSION", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(BIN / "fleet-bind")],
                          input=stdin_data, env=env, capture_output=True, text=True)


def _two_hop_table(my_pid, claude_pid):
    """The hook's own ancestry: hooks are spawned via `sh -c`, so getppid() is
    `sh`, one hop below the Claude process (plan 0012 DoD)."""
    return [[my_pid, claude_pid, "sh"], [claude_pid, 1, "claude"]]


def _lib():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("fleetlib", str(BIN / "fleetlib.py"))
    spec = importlib.util.spec_from_loader("fleetlib", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- fleetlib.nearest_ancestor_pid

def test_nearest_ancestor_matches_immediate_parent():
    lib = _lib()
    assert lib.nearest_ancestor_pid(10, "claude", table={10: (1, "claude")}) == 10


def test_nearest_ancestor_skips_non_matching_hops_to_the_nearer_match():
    lib = _lib()
    table = {10: (11, "sh"), 11: (12, "claude"), 12: (1, "claude")}
    assert lib.nearest_ancestor_pid(10, "claude", table=table) == 11


def test_nearest_ancestor_none_when_nothing_matches():
    lib = _lib()
    table = {10: (11, "sh"), 11: (1, "bash")}
    assert lib.nearest_ancestor_pid(10, "claude", table=table) is None


def test_nearest_ancestor_terminates_at_hop_bound():
    lib = _lib()
    table = {i: (i + 1, "sh") for i in range(1, 20)}
    assert lib.nearest_ancestor_pid(1, "claude", table=table, max_hops=8) is None


def test_nearest_ancestor_terminates_on_cycle():
    lib = _lib()
    table = {5: (6, "sh"), 6: (5, "sh")}
    assert lib.nearest_ancestor_pid(5, "claude", table=table) is None


# ---------------------------------------------------------------- basic bind

def test_basic_bind_writes_host(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    claude_pid = 424242
    lstart = "Thu Sep 10 10:00:00 2026"
    r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1", "source": "startup"}),
             table=_two_hop_table(my_pid, claude_pid), lstart={str(claude_pid): lstart})
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    host = json.loads((fleet / "sessions" / "s1" / "host").read_text())
    assert host == {"pid": claude_pid, "lstart": lstart}


def test_nested_ancestry_nearer_claude_wins_over_outer_capital_claude(tmp_path):
    """Measured 2026-09-06 from a Bash tool call in a desktop session:
    zsh -> claude -> disclaimer -> Claude. The nearer lowercase `claude` (the
    engine) must win over the outer `Claude` (the desktop app shell)."""
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    claude_pid, disclaimer_pid, top_pid = 67731, 67730, 80803
    table = [
        [my_pid, claude_pid, "zsh"],
        [claude_pid, disclaimer_pid, "claude"],
        [disclaimer_pid, top_pid, "disclaimer"],
        [top_pid, 1, "Claude"],
    ]
    r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1"}),
             table=table, lstart={str(claude_pid): "T"})
    assert r.returncode == 0
    host = json.loads((fleet / "sessions" / "s1" / "host").read_text())
    assert host["pid"] == claude_pid


def test_no_ancestor_match_writes_nothing(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1"}),
             table=[[my_pid, 1, "sh"]])
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not (fleet / "sessions").exists() or not any((fleet / "sessions").iterdir())


def test_lstart_probe_failure_writes_nothing(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    claude_pid = 555555
    r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1"}),
             table=_two_hop_table(my_pid, claude_pid), lstart={})   # pid resolves, lstart probe fails
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not (fleet / "sessions" / "s1" / "host").exists()


# ---------------------------------------------------------------- resume / rebind rules

def test_second_start_overwrites_when_old_pid_is_dead(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    pid_a, pid_b = 5001, 5002
    _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1"}),
         table=_two_hop_table(my_pid, pid_a), lstart={str(pid_a): "T1"})
    r2 = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1"}),
              table=_two_hop_table(my_pid, pid_b), lstart={str(pid_b): "T2"})  # pid_a now dead
    assert r2.returncode == 0
    hosts = list((fleet / "sessions").glob("*/host"))
    assert len(hosts) == 1
    assert json.loads(hosts[0].read_text()) == {"pid": pid_b, "lstart": "T2"}


def test_refuses_overwrite_when_existing_binding_is_still_live(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    pid_a, pid_b = 6001, 6002
    _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1"}),
         table=_two_hop_table(my_pid, pid_a), lstart={str(pid_a): "T1"})
    r2 = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1", "source": "resume"}),
              table=_two_hop_table(my_pid, pid_b),
              lstart={str(pid_a): "T1", str(pid_b): "T2"})  # pid_a still alive, unchanged lstart
    assert r2.returncode == 0 and r2.stdout == "" and r2.stderr == ""
    host = json.loads((fleet / "sessions" / "s1" / "host").read_text())
    assert host == {"pid": pid_a, "lstart": "T1"}, "must not overwrite a still-live binding"
    contested = json.loads((fleet / "sessions" / "s1" / "contested").read_text())
    assert contested == {"pid": pid_b, "lstart": "T2", "source": "resume"}


def test_resume_from_bound_session_prunes_caller_and_records_contested(tmp_path):
    """The `--resume` decoy this plan exists to close: the operator, inside
    process P (currently serving sid A), runs `claude --resume B` where B is
    served by a different still-live process Q. P must lose A's binding even
    though B's write is refused — else A renders as a live pid with a
    stalled heartbeat, which is exactly the hung-session shape."""
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    p, q = 7001, 7002
    _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "A"}),
         table=_two_hop_table(my_pid, p), lstart={str(p): "TP"})
    (fleet / "sessions" / "B").mkdir(parents=True, exist_ok=True)
    (fleet / "sessions" / "B" / "host").write_text(json.dumps({"pid": q, "lstart": "TQ"}))

    r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "B", "source": "resume"}),
             table=_two_hop_table(my_pid, p), lstart={str(p): "TP", str(q): "TQ"})
    assert r.returncode == 0

    assert not (fleet / "sessions" / "A" / "host").exists(), \
        "A's binding must be pruned: P no longer serves A"
    b_host = json.loads((fleet / "sessions" / "B" / "host").read_text())
    assert b_host == {"pid": q, "lstart": "TQ"}, "B must stay bound to the live Q"
    b_contested = json.loads((fleet / "sessions" / "B" / "contested").read_text())
    assert b_contested == {"pid": p, "lstart": "TP", "source": "resume"}

    # Q later dies; a fresh SessionStart for B (now genuinely served by P) must
    # succeed AND clear the now-stale contested record — the dead-pid case
    # must still overwrite, unlike the live-pid case just asserted above.
    r2 = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "B", "source": "resume"}),
              table=_two_hop_table(my_pid, p), lstart={str(p): "TP"})   # q absent => dead
    assert r2.returncode == 0
    assert json.loads((fleet / "sessions" / "B" / "host").read_text()) == {"pid": p, "lstart": "TP"}
    assert not (fleet / "sessions" / "B" / "contested").exists(), \
        "a later successful write of B's host must clear the stale contested record"


def test_clear_into_new_sid_prunes_old_binding(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    p = 8001
    _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "old"}),
         table=_two_hop_table(my_pid, p), lstart={str(p): "TP"})
    r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "new", "source": "clear"}),
             table=_two_hop_table(my_pid, p), lstart={str(p): "TP"})
    assert r.returncode == 0
    hosts = list((fleet / "sessions").glob("*/host"))
    assert len(hosts) == 1
    assert hosts[0].parent.name == "new"
    assert not (fleet / "sessions" / "old" / "host").exists()


def test_contested_cleared_when_the_claiming_process_moves_to_another_sid(tmp_path):
    """Review 2547 M1: sid B carries a refusal claim naming process P. The
    operator then `/clear`s inside P into an UNRELATED sid C — no conflict
    for C at all. P's old claim on B is stale the instant P serves C, and
    nothing but a per-turn sweep over every sid's `contested` can see it."""
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    p = 9001
    (fleet / "sessions" / "B").mkdir(parents=True, exist_ok=True)
    (fleet / "sessions" / "B" / "host").write_text(json.dumps({"pid": 9099, "lstart": "TDEAD"}))
    (fleet / "sessions" / "B" / "contested").write_text(
        json.dumps({"pid": p, "lstart": "TP", "source": "resume"}))

    r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "C", "source": "clear"}),
             table=_two_hop_table(my_pid, p), lstart={str(p): "TP"})
    assert r.returncode == 0
    assert not (fleet / "sessions" / "B" / "contested").exists()
    assert (fleet / "sessions" / "B" / "host").exists(), "B's own (unrelated) host is untouched"
    c_host = json.loads((fleet / "sessions" / "C" / "host").read_text())
    assert c_host == {"pid": p, "lstart": "TP"}


# ---------------------------------------------------------------- malformed input / degraded tree

def test_weird_or_missing_identity_is_a_silent_noop(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    table = _two_hop_table(my_pid, 11001)
    for stdin_data in ("", "not json", json.dumps({}), json.dumps({"session_id": ""}),
                      json.dumps({"session_id": "../../etc"}), json.dumps({"session_id": "a b"}),
                      json.dumps({"session_id": "."}), json.dumps({"session_id": ".."}),
                      json.dumps({"session_id": "-x"}), json.dumps({"session_id": "abc\n"})):
        r = _bind(tmp_path, fleet, stdin_data=stdin_data, table=table, lstart={"11001": "T"})
        assert r.returncode == 0 and r.stdout == "" and r.stderr == "", (stdin_data, r.stderr)
    assert not fleet.exists() or not (fleet / "sessions").exists() or \
        not any((fleet / "sessions").iterdir())


def test_unwritable_tree_still_exits_zero_and_silent(tmp_path):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    os.chmod(fleet, 0o555)
    my_pid = os.getpid()
    try:
        r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s1"}),
                 table=_two_hop_table(my_pid, 12001), lstart={"12001": "T"})
        assert r.returncode == 0
        assert r.stdout == "" and r.stderr == ""
    finally:
        os.chmod(fleet, 0o755)


# ---------------------------------------------------------------- self-kill regression (real fleet-pkill)

def test_bound_process_is_not_written_into_the_pids_registry_and_is_spared(tmp_path):
    """0012's whole reason for a SEPARATE `host` file: writing the session's own
    Claude process into `sessions/<sid>/pids/` would make it a legitimate
    substring target for `fleet-pkill` from inside that very session. Proves
    it two ways: `pids/` stays empty after a bind, and real, unmodified
    fleet-pkill still reports the bound (but unregistered) process as not
    provably its own and does not touch it.

    EUNOMIA_SESSION must be set to the bound sid for this to have teeth:
    `_registered()` is gated on it, and 0014 measured it unset on every agent
    session on the fleet — unset, `_registered` returns empty regardless of
    what's in `pids/`, masking the exact defect this test exists to catch.
    """
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    token = "bindmark" + uuid.uuid4().hex
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)", token])
    try:
        victim_pid = proc.pid
        real_lstart = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(victim_pid)],
            capture_output=True, text=True,
            env={**os.environ, "LC_ALL": "C"}).stdout.strip()
        assert real_lstart, "could not read the spawned victim's own start time"

        r = _bind(tmp_path, fleet, stdin_data=json.dumps({"session_id": "s-selfkill"}),
                 table=_two_hop_table(my_pid, victim_pid),
                 lstart={str(victim_pid): real_lstart})
        assert r.returncode == 0
        assert json.loads((fleet / "sessions" / "s-selfkill" / "host").read_text()) == {
            "pid": victim_pid, "lstart": real_lstart}
        assert not (fleet / "sessions" / "s-selfkill" / "pids").exists()

        # Run real fleet-pkill nested under an intermediate process (not pytest
        # itself), so its ppid-based "live descendant" check has the same shape
        # as the real thing: the Claude engine is fleet-pkill's ANCESTOR, never
        # its descendant, and the victim here — spawned as pytest's direct
        # child, a SIBLING of the relay below — must not be swept in either.
        relay = (
            "import subprocess, sys\n"
            f"r = subprocess.run([sys.executable, {str(BIN / 'fleet-pkill')!r}, {token!r}], "
            "capture_output=True, text=True)\n"
            "sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); sys.exit(r.returncode)\n"
        )
        pkill_env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet), EUNOMIA_SESSION="s-selfkill")
        pk = subprocess.run([sys.executable, "-c", relay], env=pkill_env,
                            capture_output=True, text=True, timeout=15)
        assert f"NOT killing {victim_pid}" in pk.stderr, pk.stderr
        assert "not provably yours" in pk.stderr
        assert proc.poll() is None, "the bound-but-unregistered process must survive"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
