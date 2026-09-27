"""Tests for fleet-prepush / fleet-install-hooks / fleet-sim / fleet-pkill
(plan 0008 §4)."""
import atexit
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
REMOTE_URL = "ssh://git@forge.example:2222/operator/sniff.git"


def _git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def _mkrepo(tmp_path):
    """Work repo + bare remote named origin, one pushed base commit."""
    work, bare = tmp_path / "work", tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-q")
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "base.txt").write_text("base\n")
    _git(work, "add", "."); _git(work, "commit", "-qm", "base")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")
    return work


def _commit(work, name, content="x\n"):
    (work / name).parent.mkdir(parents=True, exist_ok=True)
    (work / name).write_text(content)
    _git(work, "add", "."); _git(work, "commit", "-qm", f"touch {name}")
    return _git(work, "rev-parse", "HEAD")


def _lease(fleet, lease_id, resource, holder, state="active"):
    d = fleet / "leases"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{lease_id}.json").write_text(json.dumps(
        {"id": lease_id, "resource": resource, "holder": holder,
         "granted_by": "coord", "state": state, "ttl_minutes": 240,
         "created": "2026-08-28T00:00:00Z",
         "activated": "2026-08-28T00:00:00Z", "note": ""}))


def _prepush(work, fleet, sha, branch="main", session="me", env_extra=None,
             remote_url=REMOTE_URL, remote_sha=None):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet))
    env.pop("EUNOMIA_LEDGER_HOST", None)
    env.pop("FLEET_PUSH_OVERRIDE", None)
    if session is None:
        env.pop("EUNOMIA_SESSION", None)
    else:
        env["EUNOMIA_SESSION"] = session
    if env_extra:
        env.update(env_extra)
    line = (f"refs/heads/{branch} {sha} refs/heads/{branch} "
            f"{remote_sha or '0'*40}\n")
    return subprocess.run([sys.executable, str(BIN / "fleet-prepush"),
                           "origin", remote_url],
                          input=line, cwd=str(work), env=env,
                          capture_output=True, text=True)


# ---------------------------------------------------------------- pre-push

def test_foreign_branch_lease_blocks_and_holder_passes(tmp_path):
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    sha = _commit(work, "f.txt")
    _lease(fleet, "branch--sniff-main--001",
           {"type": "branch", "repo": "operator/sniff", "branch": "main"}, "worker1")
    r = _prepush(work, fleet, sha, session="someone-else")
    assert r.returncode == 3 and "held by session" in r.stderr
    r2 = _prepush(work, fleet, sha, session="worker1")
    assert r2.returncode == 0, r2.stderr


def test_no_lease_and_no_ledger_allow(tmp_path):
    work = _mkrepo(tmp_path)
    sha = _commit(work, "f.txt")
    r = _prepush(work, tmp_path / "fleet-with-leases-dir-absent", sha)
    assert r.returncode == 0 and "WARN" in r.stderr        # open, loudly
    fleet = tmp_path / "fleet"
    (fleet / "leases").mkdir(parents=True)
    r2 = _prepush(work, fleet, sha)
    assert r2.returncode == 0 and "BLOCKED" not in r2.stderr


def test_override_allows_and_names_it(tmp_path):
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    sha = _commit(work, "f.txt")
    _lease(fleet, "branch--sniff-main--001",
           {"type": "branch", "repo": "operator/sniff", "branch": "main"}, "worker1")
    r = _prepush(work, fleet, sha, session="other",
                 env_extra={"FLEET_PUSH_OVERRIDE": "1"})
    assert r.returncode == 0
    assert "OVERRIDDEN" in r.stderr and "honored" in r.stderr


def test_foreign_paths_lease_blocks_only_on_touched_globs(tmp_path):
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    _lease(fleet, "paths--sniff-w2--001",
           {"type": "paths", "repo": "operator/sniff", "globs": ["ios/FeatureB/*"]},
           "worker2")
    sha = _commit(work, "docs/readme.md")
    assert _prepush(work, fleet, sha, session="me").returncode == 0
    sha2 = _commit(work, "ios/FeatureB/View.swift")
    r = _prepush(work, fleet, sha2, session="me")
    assert r.returncode == 3 and "change-request" in r.stderr


def test_ownership_integrator_globs_need_the_role(tmp_path):
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    (fleet / "leases").mkdir(parents=True)
    (work / "OWNERSHIP.yml").write_text(
        "# shared surfaces\nintegrator: project.yml\nintegrator: *.lock\n")
    _git(work, "add", "."); _git(work, "commit", "-qm", "ownership")
    sha = _commit(work, "project.yml", "targets: {}\n")
    r = _prepush(work, fleet, sha, session="w1sess")
    assert r.returncode == 3 and "integrator-owned" in r.stderr
    _lease(fleet, "branch--sniff-integration--001",
           {"type": "branch", "repo": "operator/sniff", "branch": "integration",
            "role": "integrator"}, "w1sess")
    r2 = _prepush(work, fleet, sha, session="w1sess")
    assert r2.returncode == 0, r2.stderr


def test_deletes_tags_and_bad_urls_pass(tmp_path):
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    _lease(fleet, "branch--sniff-main--001",
           {"type": "branch", "repo": "operator/sniff", "branch": "main"}, "worker1")
    # branch deletion: local sha is zeros
    r = _prepush(work, fleet, "0" * 40, session="other")
    assert r.returncode == 0
    # tag ref passes through
    sha = _commit(work, "f.txt")
    line = f"refs/tags/v1 {sha} refs/tags/v1 {'0'*40}\n"
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path / "fleet"),
               EUNOMIA_SESSION="other")
    r2 = subprocess.run([sys.executable, str(BIN / "fleet-prepush"),
                         "origin", REMOTE_URL],
                        input=line, cwd=str(work), env=env,
                        capture_output=True, text=True)
    assert r2.returncode == 0
    # underivable remote URL: WARN + allow
    r3 = _prepush(work, fleet, sha, session="other",
                  remote_url="not a url at all")
    assert r3.returncode == 0 and "WARN" in r3.stderr


def test_install_hooks_end_to_end_idempotent_and_foreign_refusal(tmp_path):
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    _lease(fleet, "branch--sniff-main--001",
           {"type": "branch", "repo": "operator/sniff", "branch": "main"}, "worker1")
    r = subprocess.run([sys.executable, str(BIN / "fleet-install-hooks"), str(work)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "installed" in r.stdout
    r2 = subprocess.run([sys.executable, str(BIN / "fleet-install-hooks"), str(work)],
                        capture_output=True, text=True)
    assert r2.returncode == 0 and "already installed" in r2.stdout
    # the installed hook FIRES on a real push (integration, per plan DoD):
    # a RELATIVE-path remote whose URL derives to operator/sniff, backed by a
    # local bare repo — so the foreign lease genuinely blocks `git push`.
    bare2 = tmp_path / "operator" / "sniff.git"
    bare2.mkdir(parents=True)
    _git(bare2, "init", "--bare", "-q")
    _git(work, "remote", "add", "forge", "../operator/sniff.git")
    _commit(work, "f.txt")
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet), EUNOMIA_SESSION="other")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    env.pop("FLEET_PUSH_OVERRIDE", None)
    p = subprocess.run(["git", "push", "forge", "main"], cwd=str(work),
                       env=env, capture_output=True, text=True)
    assert p.returncode != 0 and "BLOCKED" in p.stderr, \
        f"installed hook did not fire/block: {p.stderr[:200]}"
    env["FLEET_PUSH_OVERRIDE"] = "1"
    p2 = subprocess.run(["git", "push", "forge", "main"], cwd=str(work),
                        env=env, capture_output=True, text=True)
    assert p2.returncode == 0 and "OVERRIDDEN" in p2.stderr
    # foreign hook refusal
    hook = work / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    r3 = subprocess.run([sys.executable, str(BIN / "fleet-install-hooks"), str(work)],
                        capture_output=True, text=True)
    assert r3.returncode != 0 and "not ours" in r3.stderr


# ---------------------------------------------------------------- fleet-sim

def _stub_simctl(tmp_path, booted_udids):
    # JSON, like `simctl list devices booted -j` (1522 #12 r10 L2). The device
    # NAMES embed a "(hex) (Booted)" lookalike — the shape that fooled the old
    # freetext parse into extracting a name-embedded fake UDID — and one
    # Shutdown device rides along to prove the state filter, not just the
    # simctl-side `booted` filter, decides.
    stub = tmp_path / "simctl-stub.sh"
    devs = [{"udid": u, "state": "Booted",
             "name": "Evil (DEAD0000-0000-0000-0000-000000000000) (Booted)"}
            for u in booted_udids]
    devs.append({"udid": "51DEAD00-0000-0000-0000-000000000000",
                 "state": "Shutdown", "name": "iPhone 15"})
    payload = json.dumps(
        {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-17-0": devs}})
    stub.write_text(f"""#!/bin/bash
case "$1 $2 $3 $4" in
  "list devices booted -j")
    cat <<'JSON'
{payload}
JSON
    ;;
  "boot "*) exit 0 ;;
  "shutdown "*) exit 0 ;;
  *) exit 64 ;;
esac
""")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return str(stub)


def _sim(tmp_path, args, booted, session="simsess"):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path / "fleet"),
               EUNOMIA_SESSION=session,
               FLEET_SIMCTL=_stub_simctl(tmp_path, booted))
    env.pop("EUNOMIA_LEDGER_HOST", None)
    return subprocess.run([sys.executable, str(BIN / "fleet-sim")] + args,
                          env=env, capture_output=True, text=True)


def _sim_lease(tmp_path, holder="simsess"):
    import socket
    host = socket.gethostname().split(".")[0].lower()
    _lease(tmp_path / "fleet", "sim--" + host + "--001",
           {"type": "sim", "host": host}, holder)


UD = ["AAAA1111-0000-0000-0000-00000000000%d" % i for i in range(4)]


def test_sim_boot_needs_lease_then_binds_udid(tmp_path):
    r = _sim(tmp_path, ["boot", UD[0]], booted=[])
    assert r.returncode != 0 and "no ACTIVE sim lease" in r.stderr
    _sim_lease(tmp_path)
    r2 = _sim(tmp_path, ["boot", UD[0]], booted=[])
    assert r2.returncode == 0, r2.stderr
    import socket
    host = socket.gethostname().split(".")[0].lower()
    rec = json.loads((tmp_path / "fleet" / "leases" / f"sim--{host}--001.json").read_text())
    assert rec["resource"]["udid"] == UD[0], "UDID bound onto the lease"
    r3 = _sim(tmp_path, ["shutdown", UD[0]], booted=[UD[0]])
    assert r3.returncode == 0
    rec = json.loads((tmp_path / "fleet" / "leases" / f"sim--{host}--001.json").read_text())
    assert "udid" not in rec["resource"], "binding cleared on shutdown"


def test_sim_refuses_fourth_and_is_idempotent_on_booted(tmp_path):
    _sim_lease(tmp_path)
    r = _sim(tmp_path, ["boot", UD[3]], booted=UD[:3])
    assert r.returncode != 0 and "refuses, never reaps" in r.stderr
    r2 = _sim(tmp_path, ["boot", UD[1]], booted=UD[:3])   # already booted: fine
    assert r2.returncode == 0
    r3 = _sim(tmp_path, ["boot", "not a udid"], booted=[])
    assert r3.returncode != 0 and "not a simulator UDID" in r3.stderr


# ---------------------------------------------------------------- fleet-pkill

# Every marker sleeper's pid lands here, and one atexit sweep SIGKILLs the
# stragglers (r10 hygiene): the per-test finally blocks cover pass/fail, but
# an interpreter exit that skips them (collection error after spawn, interrupt
# mid-teardown) would leak ~9-hour sleepers. A raw SIGKILL of pytest itself
# still leaks them — nothing in-process can cover that — but the 3133x markers
# keep the leftovers greppable.
_SLEEPERS = []


def _track(pid):
    _SLEEPERS.append(pid)
    return pid


@atexit.register
def _reap_stragglers():
    for pid in _SLEEPERS:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def _orphan_sleep(marker, env):
    """Background a sleeper via a subshell so it reparents to launchd —
    genuinely outside the test's process tree. Returns its pid (for pid-exact
    cleanup, not a pattern kill)."""
    out = subprocess.run(["bash", "-c",
                          f"( sleep {marker} >/dev/null 2>&1 & echo $! )"],
                         capture_output=True, text=True, env=env)
    return _track(int(out.stdout.strip()))


def _reap(pid):
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def test_pkill_ancestry_mode_kills_descendant_spares_outsider():
    m_in, m_out = "31337", "31338"
    env = dict(os.environ)
    env.pop("EUNOMIA_SESSION", None)               # pure ancestry mode
    child = subprocess.Popen(["sleep", m_in], env=env)   # descendant of pytest
    _track(child.pid)
    out_pid = _orphan_sleep(m_out, env)
    time.sleep(0.4)
    try:
        r = subprocess.run([sys.executable, str(BIN / "fleet-pkill"),
                            f"sleep {m_in}"],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0 and "TERM" in r.stdout, r.stderr
        assert child.wait(timeout=5) != 0, "descendant was terminated"
        r2 = subprocess.run([sys.executable, str(BIN / "fleet-pkill"),
                             f"sleep {m_out}"],
                            capture_output=True, text=True, env=env)
        assert r2.returncode == 1, "outsider match must not count as a kill"
        assert "NOT killing" in r2.stderr and "not provably yours" in r2.stderr
        r3 = subprocess.run([sys.executable, str(BIN / "fleet-pkill"),
                             "no-such-process-xyzzy"],
                            capture_output=True, text=True, env=env)
        assert r3.returncode == 1 and "NOT killing" not in r3.stderr
    finally:
        _reap(out_pid)
        if child.poll() is None:
            child.kill()


def _orphan_registered(marker, env):
    """Spawn + REGISTER in the same subshell (while the sleeper is still that
    shell's descendant), then let the shell exit so it reparents to launchd —
    the real dev-server lifecycle. Returns the pid."""
    script = (f"( sleep {marker} >/dev/null 2>&1 & pid=$!; "
              f"{sys.executable} {BIN / 'fleet-pkill'} --register $pid "
              f">/dev/null && echo $pid )")
    out = subprocess.run(["bash", "-c", script], capture_output=True,
                         text=True, env=env)
    assert out.stdout.strip(), out.stderr
    return _track(int(out.stdout.strip()))


def test_pkill_registry_reaches_reparented_own_spares_other(tmp_path):
    """1522 #12 M1: a dev server backgrounded by an earlier shell has
    reparented to launchd — ancestry can't see it, the spawn-time REGISTRATION
    can. Another session's registered orphan is spared. (ps -E is empty on
    modern macOS, so env-marking was not an option — probed.)"""
    m = "31339"
    fleet = str(tmp_path / "fleet")
    env_mine = dict(os.environ, EUNOMIA_SESSION="pk-mine", EUNOMIA_FLEET_DIR=fleet)
    env_other = dict(os.environ, EUNOMIA_SESSION="pk-other", EUNOMIA_FLEET_DIR=fleet)
    mine_pid = _orphan_registered(m, env_mine)
    other_pid = _orphan_registered(m, env_other)
    time.sleep(0.4)
    try:
        r = subprocess.run([sys.executable, str(BIN / "fleet-pkill"),
                            f"sleep {m}"],
                           capture_output=True, text=True, env=env_mine)
        assert r.returncode == 0, r.stderr
        assert f"TERM {mine_pid}" in r.stdout, "my registered orphan is reachable"
        assert str(other_pid) in r.stderr and "NOT killing" in r.stderr, \
            "the other session's orphan is spared"
        # pid-reuse guard: a registry entry whose recorded START TIME
        # mismatches is pruned, never killed
        reg = tmp_path / "fleet" / "sessions" / "pk-mine" / "pids"
        (reg / str(other_pid)).write_text("BOGUS START TIME\n")   # wrong identity
        r2 = subprocess.run([sys.executable, str(BIN / "fleet-pkill"),
                             f"sleep {m}"],
                            capture_output=True, text=True, env=env_mine)
        assert f"TERM {other_pid}" not in r2.stdout, \
            "start-time-mismatched pid spared"
        assert not (reg / str(other_pid)).exists(), "stale entry pruned"
    finally:
        _reap(mine_pid)
        _reap(other_pid)


def test_pkill_register_refuses_non_descendant(tmp_path):
    env = dict(os.environ, EUNOMIA_SESSION="pk-x",
               EUNOMIA_FLEET_DIR=str(tmp_path / "fleet"))
    r = subprocess.run([sys.executable, str(BIN / "fleet-pkill"),
                        "--register", "1"],                   # launchd: never ours
                       capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "not currently a descendant" in r.stderr


def test_install_refuses_hookspath_and_handles_worktree(tmp_path):
    """1522 #12 M3: core.hooksPath makes .git/hooks inert — refuse rather than
    report success-without-enforcement; linked worktrees install into the
    COMMON hooks dir (the fleet's own practice is worktree-per-session)."""
    work = _mkrepo(tmp_path)
    _git(work, "config", "core.hooksPath", ".husky")
    r = subprocess.run([sys.executable, str(BIN / "fleet-install-hooks"), str(work)],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "core.hooksPath" in r.stderr
    _git(work, "config", "--unset", "core.hooksPath")
    wt = tmp_path / "wt"
    _git(work, "worktree", "add", "-q", str(wt), "-b", "wtb")
    r2 = subprocess.run([sys.executable, str(BIN / "fleet-install-hooks"), str(wt)],
                        capture_output=True, text=True)
    assert r2.returncode == 0 and "installed" in r2.stdout, r2.stderr
    assert (work / ".git" / "hooks" / "pre-push").exists(),         "worktree install lands in the common hooks dir"


def test_wrapper_fails_open_when_checkout_breaks(tmp_path):
    """1522 #12 r2 H1: the polarity holds at the WRAPPER layer — a crashing
    fleet-prepush or a moved checkout WARNs and allows; only exit 3 (the
    explicit BLOCK code) aborts the push."""
    import shutil
    binc = tmp_path / "bincopy"
    binc.mkdir()
    for f in ("fleet-prepush", "fleet-install-hooks"):
        shutil.copy(BIN / f, binc / f)
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    _lease(fleet, "branch--sniff-main--001",
           {"type": "branch", "repo": "operator/sniff", "branch": "main"}, "worker1")
    bare2 = tmp_path / "operator" / "sniff.git"
    bare2.mkdir(parents=True)
    _git(bare2, "init", "--bare", "-q")
    _git(work, "remote", "add", "forge", "../operator/sniff.git")
    r = subprocess.run([sys.executable, str(binc / "fleet-install-hooks"), str(work)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    _commit(work, "f.txt")
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet), EUNOMIA_SESSION="other")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    env.pop("FLEET_PUSH_OVERRIDE", None)
    p0 = subprocess.run(["git", "push", "forge", "main"], cwd=str(work), env=env,
                        capture_output=True, text=True)
    assert p0.returncode != 0 and "BLOCKED" in p0.stderr, "intact hook blocks"
    (binc / "fleet-prepush").write_text("def broken(:\n")   # a bad merge lands
    p1 = subprocess.run(["git", "push", "forge", "main"], cwd=str(work), env=env,
                        capture_output=True, text=True)
    assert p1.returncode == 0 and "hook crashed" in p1.stderr, p1.stderr
    _commit(work, "g.txt")
    (binc / "fleet-prepush").unlink()                        # checkout moved
    p2 = subprocess.run(["git", "push", "forge", "main"], cwd=str(work), env=env,
                        capture_output=True, text=True)
    assert p2.returncode == 0 and "checkout missing" in p2.stderr, p2.stderr


def test_pkill_register_validates_sid(tmp_path):
    """1522 #12 r2 M2: the sid becomes a registry path — same rule as the
    library, refused before any write."""
    env = dict(os.environ, EUNOMIA_SESSION="../evil",
               EUNOMIA_FLEET_DIR=str(tmp_path / "fleet"))
    child = subprocess.Popen(["sleep", "31341"], env=env)
    _track(child.pid)
    try:
        r = subprocess.run([sys.executable, str(BIN / "fleet-pkill"),
                            "--register", str(child.pid)],
                           capture_output=True, text=True, env=env)
        assert r.returncode != 0 and "not a valid" in r.stderr
        assert not (tmp_path / "fleet" / "evil").exists()   # r3 L4: the real
                                                             # traversal target
    finally:
        child.kill()


def test_install_preserves_hand_merged_hook(tmp_path):
    """1522 #12 r3 M1: marked-but-different content (the hand-merge our own
    refusals prescribe) is preserved at pre-push.fleet-replaced, not destroyed."""
    work = _mkrepo(tmp_path)
    r = subprocess.run([sys.executable, str(BIN / "fleet-install-hooks"), str(work)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    hook = work / ".git" / "hooks" / "pre-push"
    merged = hook.read_text() + "\n# my custom pre-push logic\necho custom >&2\n"
    hook.write_text(merged)
    r2 = subprocess.run([sys.executable, str(BIN / "fleet-install-hooks"), str(work)],
                        capture_output=True, text=True)
    assert r2.returncode == 0 and "preserved" in r2.stdout
    backup = work / ".git" / "hooks" / "pre-push.fleet-replaced"
    assert backup.read_text() == merged, "the displaced merge is recoverable"


def test_sim_two_leases_select_by_binding(tmp_path):
    """1522 #12 r3 M2: with L1 bound to booted A and L2 unbound, boot B must
    use L2 (not refuse), and shutdown A must clear L1 (not L2)."""
    import socket
    host = socket.gethostname().split(".")[0].lower()
    fleet = tmp_path / "fleet"
    _lease(fleet, f"sim--{host}--001", {"type": "sim", "host": host, "udid": UD[0]},
           "simsess")
    _lease(fleet, f"sim--{host}--002", {"type": "sim", "host": host}, "simsess")
    r = _sim(tmp_path, ["boot", UD[1]], booted=[UD[0]])
    assert r.returncode == 0, r.stderr
    rec2 = json.loads((fleet / "leases" / f"sim--{host}--002.json").read_text())
    assert rec2["resource"]["udid"] == UD[1], "the UNBOUND lease took the boot"
    rec1 = json.loads((fleet / "leases" / f"sim--{host}--001.json").read_text())
    assert rec1["resource"]["udid"] == UD[0], "the bound lease is untouched"
    r2 = _sim(tmp_path, ["shutdown", UD[0]], booted=[UD[0], UD[1]])
    assert r2.returncode == 0, r2.stderr
    rec1 = json.loads((fleet / "leases" / f"sim--{host}--001.json").read_text())
    assert "udid" not in rec1["resource"], "shutdown cleared the MATCHING lease"
    rec2 = json.loads((fleet / "leases" / f"sim--{host}--002.json").read_text())
    assert rec2["resource"]["udid"] == UD[1], "the other binding survives"
    # both bound and both booted: boot of a third refuses with the bound list
    r3 = _sim(tmp_path, ["boot", UD[2]], booted=[UD[1]])
    # (001 is clear again, so this actually binds 001 — rebind-refusal needs
    # both bound AND still booted)
    assert r3.returncode == 0
    r4 = _sim(tmp_path, ["boot", UD[3]], booted=[UD[1], UD[2]])
    assert r4.returncode != 0 and "already bound" in r4.stderr


def test_exact_range_and_rewind_block(tmp_path):
    """1522 #12 r4 H1: with a known remote tip enumeration is the ENDPOINT
    diff — a normal exact-range push touching a leased path blocks, and a pure
    REWIND (whose rev-list range is empty) blocks too instead of silently
    force-pushing leased work out of existence."""
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    _lease(fleet, "paths--sniff-w2--001",
           {"type": "paths", "repo": "operator/sniff", "globs": ["ios/FeatureB/*"]},
           "worker2")
    base = _git(work, "rev-parse", "HEAD")
    sha_a = _commit(work, "ios/FeatureB/View.swift")
    r = _prepush(work, fleet, sha_a, session="me", remote_sha=base)
    assert r.returncode == 3 and "change-request" in r.stderr
    r2 = _prepush(work, fleet, base, session="me", remote_sha=sha_a)
    assert r2.returncode == 3, f"a rewind must block: {r2.stderr[:200]}"


def test_sim_shutdown_recovers_from_escape_hatch(tmp_path):
    """1522 #12 r4 M1: a sim shut down via raw simctl (the documented escape
    hatch) leaves a binding this tool must be able to clear — already-gone is
    the goal state, however reached."""
    import socket
    host = socket.gethostname().split(".")[0].lower()
    _lease(tmp_path / "fleet", f"sim--{host}--001",
           {"type": "sim", "host": host, "udid": UD[0]}, "simsess")
    r = _sim(tmp_path, ["shutdown", UD[0]], booted=[])
    assert r.returncode == 0, r.stderr
    rec = json.loads((tmp_path / "fleet" / "leases" /
                      f"sim--{host}--001.json").read_text())
    assert "udid" not in rec["resource"], "stale binding cleared"


def test_ledger_host_set_local_shadow_warns(tmp_path):
    """1522 #12 r4 M3: with a ledger host configured, a local tree is the
    FALLBACK (loud), never a silent shadow."""
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    (fleet / "leases").mkdir(parents=True)
    sha = _commit(work, "f.txt")
    r = _prepush(work, fleet, sha, session="me",
                 env_extra={"EUNOMIA_LEDGER_HOST": "no-such-ledger-host-xyz"})
    assert r.returncode == 0 and "shadowing" in r.stderr


def test_rename_out_of_leased_glob_blocks(tmp_path):
    """1522 #12 r5 M1: `git mv leased/file elsewhere` must block — porcelain
    diff's default rename detection hid the SOURCE path."""
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    _lease(fleet, "paths--sniff-w2--001",
           {"type": "paths", "repo": "operator/sniff", "globs": ["ios/FeatureB/*"]},
           "worker2")
    _commit(work, "ios/FeatureB/View.swift")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "mv", "ios/FeatureB/View.swift", "Shared.swift")
    _git(work, "commit", "-qm", "move it out of the lane")
    moved = _git(work, "rev-parse", "HEAD")
    r = _prepush(work, fleet, moved, session="me", remote_sha=base)
    assert r.returncode == 3, f"the rename's SOURCE must match the glob: {r.stderr[:200]}"


def _orphan_exec(cmdline, out, env):
    """Run cmdline with ppid GUARANTEED to be 1 (r7 L4: launching-and-hoping
    raced reparenting): the backgrounded subshell polls its own ppid until
    launchd adopts it, THEN execs the command."""
    script = ("( while [ \"$(ps -o ppid= -p $$ | tr -d ' ')\" -ne 1 ]; do "
              "sleep 0.05; done; "
              f"exec {cmdline} > {out} 2>&1 ) &")
    subprocess.run(["bash", "-c", script], env=env)


def test_pkill_orphaned_invocation_never_uses_launchd_as_root(tmp_path):
    """1522 #12 r6 H1: an orphaned fleet-pkill (ppid reparented to launchd)
    must fall back to registry-only — descendants-of-pid-1 is every process on
    the machine. The victim sleeper (a non-descendant, unregistered) must
    survive, and the WARN must name the fallback."""
    m = "31342"
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path / "fleet"))
    env.pop("EUNOMIA_SESSION", None)
    victim = _orphan_sleep(m, env)                 # reparented: would be a
    out = tmp_path / "out.txt"                     # "descendant of pid 1"
    _orphan_exec(f"{sys.executable} {BIN / 'fleet-pkill'} 'sleep {m}'", out, env)
    try:
        for _ in range(60):
            if out.exists() and "not an ownership root" in out.read_text():
                break
            time.sleep(0.1)
        text = out.read_text() if out.exists() else ""
        assert "not an ownership root" in text, text
        time.sleep(0.2)
        assert subprocess.run(["kill", "-0", str(victim)]).returncode == 0, \
            "the unowned sleeper must SURVIVE an orphaned pkill"
    finally:
        _reap(victim)


def test_pkill_blank_pattern_refused_on_both_paths(tmp_path):
    for args in (["--", ""], ["--", "  "], [""]):
        r = subprocess.run([sys.executable, str(BIN / "fleet-pkill")] + args,
                           capture_output=True, text=True)
        assert r.returncode != 0 and "usage" in r.stderr, args


def test_pkill_register_refused_from_orphaned_shell(tmp_path):
    """r6 H1, registration side: with ppid=1 any same-user pid would pass the
    descendant check — refused outright."""
    env = dict(os.environ, EUNOMIA_SESSION="pk-orph",
               EUNOMIA_FLEET_DIR=str(tmp_path / "fleet"))
    victim = _orphan_sleep("31343", env)
    out = tmp_path / "reg-out.txt"
    _orphan_exec(f"{sys.executable} {BIN / 'fleet-pkill'} --register {victim}",
                 out, env)
    try:
        for _ in range(60):
            if out.exists() and out.read_text().strip():
                break
            time.sleep(0.1)
        text = out.read_text() if out.exists() else ""
        assert "orphaned shell" in text, text
        assert not (tmp_path / "fleet" / "sessions" / "pk-orph" / "pids"
                    / str(victim)).exists()
    finally:
        _reap(victim)


def test_merge_main_into_feature_not_false_blocked(tmp_path):
    """1522 #12 r7 M1: keeping a branch current with main must not block on
    leased content that already LANDED on main — while unlanded rewinds and
    renames (the r4/r5 holes) still block (their tests stand above, and the
    presence guard is why: absent-at-local is never 'identical to main')."""
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    _lease(fleet, "paths--sniff-w2--001",
           {"type": "paths", "repo": "operator/sniff", "globs": ["ios/FeatureB/*"]},
           "worker2")
    _git(work, "checkout", "-qb", "featx")
    pre = _commit(work, "docs/notes.md")
    _git(work, "checkout", "-q", "main")
    _commit(work, "ios/FeatureB/View.swift")        # worker2's PR, landed
    _git(work, "push", "-q", "origin", "main")      # tracking ref advances
    _git(work, "checkout", "-q", "featx")
    _git(work, "merge", "-q", "--no-edit", "main")
    merged = _git(work, "rev-parse", "HEAD")
    r = _prepush(work, fleet, merged, branch="featx", session="me",
                 remote_sha=pre)
    assert r.returncode == 0, f"keep-current merge must not block: {r.stderr[:300]}"
    assert "excused" in r.stderr, "the excusal is LOUD (r8 M2)"


def test_sim_boot_refuses_foreign_bound_udid(tmp_path):
    """1522 #12 r8 M1: booting (or adopting) a UDID another session's active
    lease binds must refuse — the double-bind wedged BOTH sessions' shutdowns."""
    import socket
    host = socket.gethostname().split(".")[0].lower()
    fleet = tmp_path / "fleet"
    _lease(fleet, f"sim--{host}--001", {"type": "sim", "host": host, "udid": UD[0]},
           "sessB")
    _lease(fleet, f"sim--{host}--002", {"type": "sim", "host": host}, "simsess")
    # adopt path (already booted) refuses
    r = _sim(tmp_path, ["boot", UD[0]], booted=[UD[0]])
    assert r.returncode != 0 and "double-bind" in r.stderr
    rec = json.loads((fleet / "leases" / f"sim--{host}--002.json").read_text())
    assert "udid" not in rec["resource"], "no binding written on refusal"
    # fresh-boot path (foreign STALE binding, sim currently down) refuses too
    r2 = _sim(tmp_path, ["boot", UD[0]], booted=[])
    assert r2.returncode != 0 and "double-bind" in r2.stderr


def test_install_refuses_symlinked_hook(tmp_path):
    """1522 #12 r8 low: writing through a symlinked pre-push would clobber a
    SHARED hooks file used by other repos."""
    work = _mkrepo(tmp_path)
    shared = tmp_path / "shared-hook.sh"
    shared.write_text("#!/bin/sh\n# fleet-install-hooks v1 (hand-merged)\nexit 0\n")
    hook = work / ".git" / "hooks" / "pre-push"
    hook.symlink_to(shared)
    r = subprocess.run([sys.executable, str(BIN / "fleet-install-hooks"), str(work)],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "symlink" in r.stderr
    assert shared.read_text().endswith("exit 0\n"), "the shared target is untouched"


def test_revert_to_main_over_unlanded_work_blocks(tmp_path):
    """1522 #12 r9 HIGH: the shared branch's remote tip carries worker2's
    UNLANDED leased-path change; pushing that file reverted to main's content
    (an honest conflict resolution taking main's side) must BLOCK — content
    identical to main is excusable only when the branch never touched it."""
    work = _mkrepo(tmp_path)
    fleet = tmp_path / "fleet"
    _lease(fleet, "paths--sniff-w2--001",
           {"type": "paths", "repo": "operator/sniff", "globs": ["ios/FeatureB/*"]},
           "worker2")
    _commit(work, "ios/FeatureB/View.swift", "V1 landed\n")   # main's version
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-qb", "shared")
    tip2 = _commit(work, "ios/FeatureB/View.swift", "V2 unlanded by worker2\n")
    # "me" reverts the file to main's content and pushes over worker2's tip
    (work / "ios/FeatureB/View.swift").write_text("V1 landed\n")
    _git(work, "add", "."); _git(work, "commit", "-qm", "take main's side")
    local = _git(work, "rev-parse", "HEAD")
    r = _prepush(work, fleet, local, branch="shared", session="me",
                 remote_sha=tip2)
    assert r.returncode == 3, \
        f"revert-over-unlanded must block: {r.stderr[:300]}"
    # and the legitimate keep-current shape still passes (branch never touched
    # the leased file): untouched-since-merge-base content is excused
    _git(work, "checkout", "-qb", "clean", "main")
    pre = _commit(work, "docs/other.md")
    _git(work, "checkout", "-q", "main")
    _commit(work, "ios/FeatureB/New.swift")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "clean")
    _git(work, "merge", "-q", "--no-edit", "main")
    merged = _git(work, "rev-parse", "HEAD")
    r2 = _prepush(work, fleet, merged, branch="clean", session="me",
                  remote_sha=pre)
    assert r2.returncode == 0, f"keep-current still passes: {r2.stderr[:300]}"
    assert "excused" in r2.stderr and "New.swift" in r2.stderr, \
        "the excusal names its paths"
