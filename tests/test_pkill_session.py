"""Tests for `fleet-pkill --session` (plan 0014) — the cross-session,
human-triggered kill verb for a session that has stopped responding to its
own shell.

Most tests run the real `bin/fleet-pkill` binary as a subprocess against
REAL processes and REAL `ps` output: the pids under test are either real
short-lived subprocesses this file spawns, or a deliberately implausible
fixed pid (`DEAD_PID`, well above macOS's pid ceiling, so it is never a real
process on the test host) — a broken refusal guard then fails LOUD (a real
SIGTERM reaching something) rather than silently matching fake data that
could never occur outside the test.

The killer-attribution tests are the one place a REAL process tree cannot be
made deterministic (whether *this* test run's own ancestry happens to have a
`claude`-named process within the walk's hop bound is incidental to the
question under test), so those fake `ps` on PATH — same idiom as
test_bind.py's fakebin shim, since fleetlib.proc_table()/_lstart() and this
file's own `_proc_table()` both shell out to a literal `ps`.

The self-target regression runs fleet-pkill as a child of a disposable RELAY
process and has the relay bind ITS OWN real pid as the target, so a broken
self-target guard delivers a real SIGTERM to the relay — never to pytest.
"""
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
PKILL = BIN / "fleet-pkill"

# Comfortably above macOS's pid ceiling (~99998) — never a real process.
DEAD_PID = 999999


def _real_lstart(pid: int) -> str:
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                       capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C"})
    return r.stdout.strip()


def _write_host(fleet: Path, sid: str, pid: int, lstart: str):
    d = fleet / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "host").write_text(json.dumps({"pid": pid, "lstart": lstart}))


def _run(fleet: Path, args, env_extra=None):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet))
    env.pop("EUNOMIA_SESSION", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(PKILL), *args],
                          env=env, capture_output=True, text=True, timeout=15)


def _events(fleet: Path):
    p = fleet / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l]


def _spawn_victim(token: str):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", token])


def _reap(proc):
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


# ---------------------------------------------------------------- fakebin ps (attribution only)

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


def _run_faked(tmp_path, fleet, args, table, lstart):
    fakebin = _fakebin(tmp_path)
    data_path = tmp_path / f"psdata-{next(_counter)}.json"
    data_path.write_text(json.dumps({"table": table, "lstart": lstart}))
    env = dict(os.environ,
               EUNOMIA_FLEET_DIR=str(fleet),
               PATH=f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
               FAKE_PS_DATA=str(data_path))
    env.pop("EUNOMIA_SESSION", None)
    return subprocess.run([sys.executable, str(PKILL), *args],
                          env=env, capture_output=True, text=True, timeout=15)


# ---------------------------------------------------------------- --reason: scanned first

def test_reason_matching_secret_pattern_is_refused_before_sid_is_even_looked_up(tmp_path):
    fleet = tmp_path / "fleet"
    r = _run(fleet, ["--session", "no-such-sid", "--reason", "ghp_" + "A" * 20])
    assert r.returncode != 0
    assert "secret rule" in r.stderr
    assert "github-pat" in r.stderr
    assert "ghp_" not in r.stderr, "the matched text must never be echoed back"
    assert "unknown session" not in r.stderr, \
        "the reason scan must run before the sid is looked up at all"
    assert not fleet.exists() or not (fleet / "sessions").exists()


def test_reason_over_cap_is_refused(tmp_path):
    fleet = tmp_path / "fleet"
    r = _run(fleet, ["--session", "no-such-sid", "--reason", "x" * 600])
    assert r.returncode != 0
    assert "exceeds" in r.stderr


def test_missing_reason_is_a_usage_error(tmp_path):
    fleet = tmp_path / "fleet"
    r = _run(fleet, ["--session", "some-sid"])
    assert r.returncode != 0
    assert "usage" in r.stderr


def test_missing_session_is_a_usage_error(tmp_path):
    fleet = tmp_path / "fleet"
    r = _run(fleet, ["--reason", "hung"])
    assert r.returncode != 0
    assert "usage" in r.stderr


# ---------------------------------------------------------------- ledger-shape refusals

def test_unknown_sid_refused(tmp_path):
    fleet = tmp_path / "fleet"
    r = _run(fleet, ["--session", "ghost", "--reason", "hung"])
    assert r.returncode != 0
    assert "unknown session" in r.stderr


def test_sid_present_but_no_host_binding_refused(tmp_path):
    fleet = tmp_path / "fleet"
    (fleet / "sessions" / "s1").mkdir(parents=True)
    r = _run(fleet, ["--session", "s1", "--reason", "hung"])
    assert r.returncode != 0
    assert "no host binding" in r.stderr


def test_malformed_host_file_refused(tmp_path):
    fleet = tmp_path / "fleet"
    d = fleet / "sessions" / "s1"
    d.mkdir(parents=True)
    (d / "host").write_text("not json")
    r = _run(fleet, ["--session", "s1", "--reason", "hung"])
    assert r.returncode != 0
    assert "malformed" in r.stderr


def test_pid_dead_refused(tmp_path):
    fleet = tmp_path / "fleet"
    _write_host(fleet, "s1", DEAD_PID, "Thu Sep 10 10:00:00 2026")
    r = _run(fleet, ["--session", "s1", "--reason", "hung"])
    assert r.returncode != 0
    assert "not alive" in r.stderr


def test_pid_alive_but_start_time_mismatch_refused(tmp_path):
    fleet = tmp_path / "fleet"
    victim = _spawn_victim("mismatch")
    try:
        _write_host(fleet, "s1", victim.pid, "Thu Jan 01 00:00:00 1970")
        r = _run(fleet, ["--session", "s1", "--reason", "hung"])
        assert r.returncode != 0
        assert "reused" in r.stderr
        assert victim.poll() is None, "a start-time mismatch must not kill the live pid"
    finally:
        _reap(victim)


def test_target_pid_bound_under_two_sids_is_the_clear_decoy_and_is_refused(tmp_path):
    """The case every other guard passes: an old sid's `host` still names a
    pid that a new sid also (correctly) claims — a failed fleet-bind prune.
    Matched on (pid, lstart), not the pid alone."""
    fleet = tmp_path / "fleet"
    victim = _spawn_victim("decoy")
    try:
        lstart = _real_lstart(victim.pid)
        assert lstart
        _write_host(fleet, "old-sid", victim.pid, lstart)
        _write_host(fleet, "new-sid", victim.pid, lstart)
        r = _run(fleet, ["--session", "old-sid", "--reason", "hung"])
        assert r.returncode != 0
        assert "more than one session" in r.stderr
        assert "old-sid" in r.stderr and "new-sid" in r.stderr
        assert victim.poll() is None, "a contested (pid, lstart) pair must not be killed"
    finally:
        _reap(victim)


def test_self_target_refused_before_anything_is_signalled(tmp_path):
    """Runs fleet-pkill as a CHILD of a disposable relay process, and has the
    relay bind ITS OWN pid as the target — the shape of an agent inside
    session S running `fleet-pkill --session S`. If the self-target guard
    fails to fire, the relay (never pytest) receives a real SIGTERM: its own
    `subprocess.run` of fleet-pkill dies by signal instead of returning, and
    the relay's `sys.exit(r.returncode)` never runs — a non-positive
    returncode on THIS test's subprocess is the tell.
    """
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    relay_code = (
        "import json, os, subprocess, sys\n"
        "fleet, sid, pkill = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "pid = os.getpid()\n"
        "lstart = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)],\n"
        "    capture_output=True, text=True,\n"
        "    env={**os.environ, 'LC_ALL': 'C'}).stdout.strip()\n"
        "d = os.path.join(fleet, 'sessions', sid)\n"
        "os.makedirs(d, exist_ok=True)\n"
        "with open(os.path.join(d, 'host'), 'w') as fh:\n"
        "    json.dump({'pid': pid, 'lstart': lstart}, fh)\n"
        "env = dict(os.environ, EUNOMIA_FLEET_DIR=fleet)\n"
        "env.pop('EUNOMIA_SESSION', None)\n"
        "r = subprocess.run([sys.executable, pkill, '--session', sid,\n"
        "                    '--reason', 'self target probe'],\n"
        "                   env=env, capture_output=True, text=True)\n"
        "sys.stdout.write(r.stdout)\n"
        "sys.stderr.write(r.stderr)\n"
        "sys.exit(r.returncode)\n"
    )
    r = subprocess.run([sys.executable, "-c", relay_code,
                        str(fleet), "s-self", str(PKILL)],
                       capture_output=True, text=True, timeout=15)
    assert r.returncode > 0, (
        f"relay exit {r.returncode} — a non-positive code means the relay was "
        f"killed by a real signal, i.e. the self-target guard did not fire; "
        f"stderr={r.stderr!r}")
    assert "own session" in r.stderr


# ---------------------------------------------------------------- contested + heartbeat display

def test_contested_record_is_shown_but_never_acted_on(tmp_path):
    fleet = tmp_path / "fleet"
    victim = _spawn_victim("contested-display")
    try:
        lstart = _real_lstart(victim.pid)
        _write_host(fleet, "s1", victim.pid, lstart)
        (fleet / "sessions" / "s1" / "contested").write_text(
            json.dumps({"pid": 424242, "lstart": "Thu Sep 10 10:00:00 2026", "source": "resume"}))
        r = _run(fleet, ["--session", "s1", "--reason", "hung"])
        assert r.returncode == 0
        assert "CONTESTED" in r.stdout
        try:
            victim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError("the kill must still proceed; contested is shown, not acted on")
    finally:
        _reap(victim)


def test_no_hb_file_renders_without_throwing_or_implying_zero(tmp_path):
    fleet = tmp_path / "fleet"
    victim = _spawn_victim("no-hb")
    try:
        lstart = _real_lstart(victim.pid)
        _write_host(fleet, "s1", victim.pid, lstart)
        r = _run(fleet, ["--session", "s1", "--reason", "hung"])
        assert r.returncode == 0
        assert "no heartbeat recorded" in r.stdout
        assert "0s ago" not in r.stdout
    finally:
        _reap(victim)


# ---------------------------------------------------------------- successful kill + event

def test_successful_kill_terminates_and_emits_session_killed(tmp_path):
    fleet = tmp_path / "fleet"
    victim = _spawn_victim("kill-me")
    try:
        lstart = _real_lstart(victim.pid)
        _write_host(fleet, "s1", victim.pid, lstart)
        r = _run(fleet, ["--session", "s1", "--reason", "stuck since lunch"])
        assert r.returncode == 0, r.stderr
        assert f"TERM {victim.pid}" in r.stdout
        try:
            victim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError("victim survived the SIGTERM")

        events = _events(fleet)
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "session-killed"
        assert e["lease"] is None
        assert e["detail"]["session"] == "s1"
        assert e["detail"]["pid"] == victim.pid
        assert e["detail"]["reason"] == "stuck since lunch"
        # Killer-basis is deterministic (no --killer given, so it's always
        # "ancestry"); the exact killer STRING depends on whether this test
        # run's own real ancestry happens to have a `claude`-named process,
        # which the dedicated fakebin attribution tests below pin down
        # precisely. What must hold regardless: never the bare word "human".
        assert e["detail"]["killer-basis"] == "ancestry"
        assert e["detail"]["killer"] != "human"
        assert e["actor"] == e["detail"]["killer"]
    finally:
        _reap(victim)


def test_explicit_killer_overrides_ancestry_attribution(tmp_path):
    fleet = tmp_path / "fleet"
    victim = _spawn_victim("kill-me-explicit")
    try:
        lstart = _real_lstart(victim.pid)
        _write_host(fleet, "s1", victim.pid, lstart)
        r = _run(fleet, ["--session", "s1", "--reason", "hung", "--killer", "row19-web"])
        assert r.returncode == 0, r.stderr
        events = _events(fleet)
        assert events[0]["detail"]["killer"] == "row19-web"
        assert events[0]["detail"]["killer-basis"] == "explicit"
    finally:
        _reap(victim)


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod cannot deny root; the cihost-linux job container runs as uid 0, so this permission-denied path is only testable on the host label")
def test_unrecorded_kill_still_kills_and_exits_3(tmp_path):
    """Kill first, then emit: a ledger write that cannot land must never be a
    reason the hung session survives, but it is not silent either."""
    fleet = tmp_path / "fleet"
    victim = _spawn_victim("kill-unrecorded")
    try:
        lstart = _real_lstart(victim.pid)
        _write_host(fleet, "s1", victim.pid, lstart)
        os.chmod(fleet, 0o555)          # fleet-emit cannot create locks/ or events.jsonl
        try:
            r = _run(fleet, ["--session", "s1", "--reason", "hung"])
        finally:
            os.chmod(fleet, 0o755)
        assert r.returncode == 3, r.stderr
        assert "could not be recorded" in r.stderr
        try:
            victim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError("victim survived even though the kill is supposed to run first")
    finally:
        _reap(victim)


# ---------------------------------------------------------------- killer attribution (fakebin)

def _two_hop_claude_table(my_pid, claude_pid):
    return [[my_pid, claude_pid, "sh"], [claude_pid, 1, "claude"]]


def test_killer_attribution_no_claude_ancestor_is_unattributed(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    victim = _spawn_victim("attr-none")
    try:
        _write_host(fleet, "s1", victim.pid, "V-LSTART")
        r = _run_faked(tmp_path, fleet, ["--session", "s1", "--reason", "hung"],
                      table=[[my_pid, 1, "zsh"]], lstart={str(victim.pid): "V-LSTART"})
        assert r.returncode == 0, r.stderr
        e = _events(fleet)[0]
        assert e["detail"]["killer"] == "unattributed"
        assert e["detail"]["killer-basis"] == "ancestry"
        try:
            victim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError("victim survived")
    finally:
        _reap(victim)


def test_killer_attribution_claude_ancestor_bound_to_a_session(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    claude_pid = 555001
    victim = _spawn_victim("attr-bound")
    try:
        _write_host(fleet, "killer-sid", claude_pid, "C-LSTART")
        _write_host(fleet, "s1", victim.pid, "V-LSTART")
        r = _run_faked(
            tmp_path, fleet, ["--session", "s1", "--reason", "hung"],
            table=_two_hop_claude_table(my_pid, claude_pid),
            lstart={str(claude_pid): "C-LSTART", str(victim.pid): "V-LSTART"})
        assert r.returncode == 0, r.stderr
        e = _events(fleet)[0]
        assert e["detail"]["killer"] == "session:killer-sid"
        assert e["detail"]["killer-basis"] == "ancestry"
        try:
            victim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError("victim survived")
    finally:
        _reap(victim)


def test_killer_attribution_claude_ancestor_not_bound_is_session_pid(tmp_path):
    fleet = tmp_path / "fleet"
    my_pid = os.getpid()
    claude_pid = 555002
    victim = _spawn_victim("attr-unbound")
    try:
        _write_host(fleet, "s1", victim.pid, "V-LSTART")
        r = _run_faked(
            tmp_path, fleet, ["--session", "s1", "--reason", "hung"],
            table=_two_hop_claude_table(my_pid, claude_pid),
            lstart={str(claude_pid): "C-LSTART", str(victim.pid): "V-LSTART"})
        assert r.returncode == 0, r.stderr
        e = _events(fleet)[0]
        assert e["detail"]["killer"] == f"session-pid:{claude_pid}"
        assert e["detail"]["killer-basis"] == "ancestry"
        try:
            victim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError("victim survived")
    finally:
        _reap(victim)


# ---------------------------------------------------------------- existing verb untouched

def test_existing_substring_kill_mode_is_unaffected(tmp_path):
    fleet = tmp_path / "fleet"
    token = "still-works-marker"
    victim = _spawn_victim(token)
    try:
        env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet))
        env.pop("EUNOMIA_SESSION", None)
        # Run nested under an intermediate process (not pytest itself), so
        # fleet-pkill's ppid-based "live descendant" check has the same shape
        # as the real thing (the victim is pytest's own direct child, a
        # SIBLING of this relay, and must not be swept in).
        relay = (
            "import subprocess, sys\n"
            f"r = subprocess.run([sys.executable, {str(PKILL)!r}, {token!r}], "
            "capture_output=True, text=True)\n"
            "sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); sys.exit(r.returncode)\n"
        )
        pk = subprocess.run([sys.executable, "-c", relay], env=env,
                            capture_output=True, text=True, timeout=15)
        assert f"NOT killing {victim.pid}" in pk.stderr
        assert victim.poll() is None
    finally:
        _reap(victim)
