"""Tests for fleet-heartbeat (plan 0005) + the row-3 review fixes carried here."""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _hb(fleet_dir, stdin_data=None, env_extra=None):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet_dir))
    env.pop("EUNOMIA_SESSION", None)
    # On an off-opshost host this is set for real; unset it so the local-path tests
    # don't take the remote branch and touch the production ledger (1522 M3).
    env.pop("EUNOMIA_LEDGER_HOST", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(BIN / "fleet-heartbeat")],
                         input=stdin_data, env=env, capture_output=True, text=True)


def test_hook_json_touches_and_exits_zero(tmp_path):
    r = _hb(tmp_path, stdin_data='{"session_id":"abc-123"}')
    assert r.returncode == 0 and r.stderr == ""
    assert (tmp_path / "sessions" / "abc-123" / "hb").exists()


def test_no_identity_is_a_silent_noop(tmp_path):
    r = _hb(tmp_path, stdin_data="")
    assert r.returncode == 0 and r.stderr == ""
    assert not (tmp_path / "sessions").exists()


def test_env_fallback_when_stdin_is_not_json(tmp_path):
    r = _hb(tmp_path, stdin_data="not json", env_extra={"EUNOMIA_SESSION": "env-sid"})
    assert r.returncode == 0 and r.stderr == ""
    assert (tmp_path / "sessions" / "env-sid" / "hb").exists()


def test_unwritable_tree_still_exits_zero_and_stderr_silent(tmp_path):
    os.chmod(tmp_path, 0o555)
    try:
        r = _hb(tmp_path, stdin_data='{"session_id":"abc"}')
        assert r.returncode == 0
        assert r.stderr == "", "Stop-hook stderr can surface into the user's terminal"
    finally:
        os.chmod(tmp_path, 0o755)


def test_weird_session_ids_refused_silently(tmp_path):
    for sid in ("../../etc", "a b", "x/"+"y", "", ".", "..", "-x", "_x", "abc\n"):
        r = _hb(tmp_path, stdin_data=json.dumps({"session_id": sid}))
        assert r.returncode == 0
    assert not (tmp_path / "sessions").exists() or not any((tmp_path/"sessions").iterdir())


def test_second_touch_advances_liveness(tmp_path):
    _hb(tmp_path, stdin_data='{"session_id":"s1"}')
    hb = tmp_path / "sessions" / "s1" / "hb"
    old = time.time() - 3600
    os.utime(hb, (old, old))
    before = hb.stat().st_mtime
    _hb(tmp_path, stdin_data='{"session_id":"s1"}')
    assert hb.stat().st_mtime > before


# ---------------- row-3 review fixes (revbot 1493), carried per plan 0005

def _run_cli(fleet_dir, script, args, session="s-test"):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet_dir), EUNOMIA_SESSION=session)
    env.pop("EUNOMIA_LEDGER_HOST", None)
    return subprocess.run([sys.executable, str(BIN / script)] + args,
                          env=env, capture_output=True, text=True)


def _backdate(fleet_dir, lease_id, field, minutes):
    p = fleet_dir / "leases" / f"{lease_id}.json"
    rec = json.loads(p.read_text())
    old = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rec[field] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(json.dumps(rec))


def test_toctou_takeover_racing_sweep_no_spurious_orphan_event(tmp_path):
    """1493 finding 1: mark only after re-deriving under the lease lock."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "dead"],
                   session="coord").stdout.strip()
    _backdate(tmp_path, lid, "created", 30)
    # race: N status sweeps against one takeover
    env = lambda s: dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION=s)
    sweeps = [subprocess.Popen([sys.executable, str(BIN / "fleet-status")],
                               env=env(f"obs{i}"),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
    tk = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--takeover", lid],
                        env=env("succ"), capture_output=True, text=True)
    for p in sweeps:
        p.communicate()
    assert tk.returncode == 0, tk.stderr
    events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    orphans = [e for e in events if e["type"] == "lease-orphaned"]
    marker = (tmp_path / "leases" / f"{lid}.orphaned").exists()
    # Either a sweep legitimately observed the orphan BEFORE the takeover (fine:
    # one event, marker then cleared by takeover), or the takeover won first and
    # no orphan event exists. Never: an event emitted for a lease that was already
    # taken over, or a stale marker surviving the takeover.
    assert len(orphans) <= 1
    assert not marker, "takeover must leave no stale marker"


def test_assign_prints_id_even_when_event_log_is_broken(tmp_path):
    (tmp_path / "events.jsonl").mkdir(parents=True)   # a DIRECTORY: append will fail
    r = _run_cli(tmp_path, "fleet-claim",
                 ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1"],
                 session="coord")
    assert r.returncode != 0, "a broken log must be loud"
    lid = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    assert lid.startswith("branch--"), f"the grant id must still reach stdout: {r.stdout!r}"
    assert (tmp_path / "leases" / f"{lid}.json").exists(), "the ledger write is the authority"


def test_nonint_ttl_degrades_row_not_sweep(tmp_path):
    a = _run_cli(tmp_path, "fleet-claim",
                 ["--assign", '{"type":"branch","branch":"a"}', "--holder", "w1"],
                 session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim",
             ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w2"],
             session="coord")
    _run_cli(tmp_path, "fleet-claim", ["--activate", a], session="w1")
    p = tmp_path / "leases" / f"{a}.json"
    rec = json.loads(p.read_text()); rec["ttl_minutes"] = "lots"; p.write_text(json.dumps(rec))
    r = _run_cli(tmp_path, "fleet-status", ["--json"], session="obs")
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)["leases"]
    assert len(rows) == 2, "one bad row must not blind the sweep"
    assert "WARN non-integer ttl_minutes" in r.stderr


def test_bogus_lease_id_args_refused_before_pathing(tmp_path):
    for script, args in (("fleet-claim", ["--activate", "../../etc/passwd"]),
                         ("fleet-claim", ["--takeover", "weird id"]),
                         # 1522 H1: a traversal payload wrapped in a valid
                         # type--slug--NNN frame must be refused BEFORE pathing,
                         # not create locks/branch--.. and climb out of the tree.
                         ("fleet-claim", ["--takeover", "branch--../../x--001"]),
                         ("fleet-release", ["not-an-id"])):
        r = _run_cli(tmp_path, script, args, session="x")
        assert r.returncode != 0 and "not a lease id" in r.stderr, (script, args, r.stderr)
    # nothing escaped the fleet tree while validating
    assert not (tmp_path.parent / "x--001.lock").exists()
    assert not (tmp_path / "locks" / "branch--..").exists()


def test_takeover_runbook_commands_are_runnable(tmp_path):
    """Plan DoD: the runbook's fenced commands execute against a scratch tree."""
    doc = (Path(__file__).resolve().parent.parent / "docs" / "takeover.md").read_text()
    blocks = re.findall(r"```\n(.*?)```", doc, re.S)
    assert blocks, "runbook has no fenced commands"
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="runbook")
    # seed one lease so the observe/claim commands have something to act on
    seed = subprocess.run([sys.executable, str(BIN / "fleet-claim"),
                    "--assign", '{"type":"branch","branch":"rb"}', "--holder", "dead"],
                   env=env, capture_output=True, text=True)
    lid = seed.stdout.strip()
    saw_id = False
    for block in blocks:
        for cmd in [c for c in block.splitlines() if c.strip() and not c.startswith("#")]:
            cmd = cmd.replace("bin/", str(BIN) + "/").replace("<lease-id>", lid)
            res = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True,
                                 cwd=str(tmp_path), timeout=30)
            # observe/read commands must run; the takeover line may legitimately
            # refuse (nothing orphaned in a fresh tree) but must not crash-path
            assert res.returncode in (0, 1), f"{cmd!r} -> {res.returncode}: {res.stderr[:200]}"
            if '"id"' in res.stdout:
                saw_id = True
    # 1522 M1: the reconstruct command must actually surface a lease record, not
    # silently cat nothing — otherwise "copy-paste runnable" is vacuously true.
    assert saw_id, "no runbook command printed a lease record"


def _orphan_count(fleet_dir, lease_id):
    p = fleet_dir / "events.jsonl"
    if not p.exists():
        return 0
    return sum(1 for l in p.read_text().splitlines()
               if json.loads(l)["type"] == "lease-orphaned" and json.loads(l)["lease"] == lease_id)


def test_late_activation_clears_marker_so_next_death_re_emits(tmp_path):
    """1522 M1: a sweep marks an assigned-orphan; a late --activate must clear the
    marker, or the NEXT genuine orphaning silently emits nothing."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"m1"}', "--holder", "w1", "--ttl", "1"],
                   session="coord").stdout.strip()
    _backdate(tmp_path, lid, "created", 30)            # blew the activate-timeout
    _run_cli(tmp_path, "fleet-status", [], session="obs1")
    assert (tmp_path / "leases" / f"{lid}.orphaned").exists()
    assert _orphan_count(tmp_path, lid) == 1
    # worker arrives late and activates — the marker must be cleared
    r = _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "leases" / f"{lid}.orphaned").exists(), "activate must clear the marker"
    # later the session genuinely dies: stale the heartbeat past ttl
    hb = tmp_path / "sessions" / "w1" / "hb"
    old = time.time() - 1800
    os.utime(hb, (old, old))
    _run_cli(tmp_path, "fleet-status", [], session="obs2")
    assert _orphan_count(tmp_path, lid) == 2, "the real death must re-emit lease-orphaned"


def test_empty_slug_still_mints_a_usable_id(tmp_path):
    """1522 L1: a resource value stripping to empty must not mint an id the
    validators then reject forever."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"---"}', "--holder", "w1"],
                   session="coord").stdout.strip()
    assert lid, "assign must print an id"
    r = _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    assert r.returncode == 0, f"minted id must be activatable, got: {r.stderr}"


def test_status_mark_is_nonblocking_under_a_held_lease_lock(tmp_path):
    """1522 L4: observation must not block. With a lease lock held (a hung-alive
    holder), the sweep skips marking — no hang, no event — and still reports the
    row; the next sweep marks once the lock frees (idempotent retry)."""
    import fcntl
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"l4"}', "--holder", "dead"],
                   session="coord").stdout.strip()
    _backdate(tmp_path, lid, "created", 30)            # derived orphan
    lockp = tmp_path / "locks" / f"{lid}.lock"
    lockp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lockp), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = _run_cli(tmp_path, "fleet-status", ["--json"], session="obs")  # must return, not hang
        assert r.returncode == 0, r.stderr
        rows = json.loads(r.stdout)["leases"]
        assert any(x["id"] == lid for x in rows), "contended row is still reported"
        assert not (tmp_path / "leases" / f"{lid}.orphaned").exists(), "no marker while lock held"
        assert _orphan_count(tmp_path, lid) == 0, "no orphan event while contended"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    _run_cli(tmp_path, "fleet-status", [], session="obs2")
    assert (tmp_path / "leases" / f"{lid}.orphaned").exists()
    assert _orphan_count(tmp_path, lid) == 1, "the freed sweep marks+emits exactly once"


def test_orphan_emit_failure_leaves_no_suppressing_marker(tmp_path):
    """1522 r3 L2: if emit fails after the marker create, the marker must not
    persist and silently suppress this orphaning on every future sweep."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"l2"}', "--holder", "dead"],
                   session="coord").stdout.strip()
    _backdate(tmp_path, lid, "created", 30)            # derived orphan
    ev = tmp_path / "events.jsonl"
    if ev.exists():
        ev.unlink()
    ev.mkdir()                                         # append target is a dir -> emit fails
    r = _run_cli(tmp_path, "fleet-status", [], session="obs")
    assert r.returncode != 0, "a broken event log must be loud"
    assert lid in r.stdout, \
        "the incident-time tool must still DISPLAY the sweep when the log is broken"
    assert not (tmp_path / "leases" / f"{lid}.orphaned").exists(), \
        "emit-failed marker must be unlinked, not left to suppress future emits"
    ev.rmdir()                                         # repair the log
    _run_cli(tmp_path, "fleet-status", [], session="obs2")
    assert (tmp_path / "leases" / f"{lid}.orphaned").exists()
    assert _orphan_count(tmp_path, lid) == 1, "the repaired sweep emits the orphaning"


def test_sweep_survives_lease_body_with_missing_or_traversal_id(tmp_path):
    """1522 r4 M2: a hand-edited lease whose BODY id is missing or traversal-shaped
    must neither KeyError the whole sweep nor path outside the tree — the row still
    reports; only its mark is skipped."""
    good = _run_cli(tmp_path, "fleet-claim",
                    ["--assign", '{"type":"branch","branch":"good"}', "--holder", "dead"],
                    session="coord").stdout.strip()
    _backdate(tmp_path, good, "created", 30)           # a legit derived orphan
    leases = tmp_path / "leases"
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (leases / "branch--noid--001.json").write_text(json.dumps(          # no "id"
        {"resource": {"type": "branch"}, "holder": "dead", "state": "assigned", "created": old}))
    (leases / "branch--evil--001.json").write_text(json.dumps(          # traversal id
        {"id": "../../../tmp/evil", "resource": {"type": "branch"}, "holder": "dead",
         "state": "assigned", "created": old}))
    (leases / "branch--numid--001.json").write_text(json.dumps(         # non-string id
        {"id": 123, "resource": {"type": "branch"}, "holder": "dead",
         "state": "assigned", "created": old}))
    (leases / "branch--dictid--001.json").write_text(json.dumps(        # dict id: truthy,
        {"id": {"x": 1}, "resource": {"type": "branch"}, "holder": "dead",   # crashes width
         "state": "assigned", "created": old}))                              # specs (r7 M2)
    (leases / "branch--strres--001.json").write_text(json.dumps(        # non-dict resource
        {"id": "branch--strres--001", "resource": "branch", "holder": "dead",  # (r8 M2)
         "state": "assigned", "created": old}))
    (leases / "branch--listbody--001.json").write_text("[1]")           # non-dict body (r8 M2)
    (leases / "branch--badholder--001.json").write_text(json.dumps(     # non-string holder
        {"id": "branch--badholder--001", "resource": {"type": "branch"}, "holder": 123,
         "state": "active", "activated": old, "ttl_minutes": 1, "created": old}))  # (r9 M1)
    # BOTH modes must survive: default text mode formats the id, --json does not,
    # so the earlier --json-only test missed the None/int format crash (1522 r5 M2).
    rt = _run_cli(tmp_path, "fleet-status", [], session="obs")
    assert rt.returncode == 0, f"default text mode crashed: {rt.stderr}"
    r = _run_cli(tmp_path, "fleet-status", ["--json"], session="obs")
    assert r.returncode == 0, r.stderr                 # completed, not KeyError'd
    ids = [row["id"] for row in json.loads(r.stdout)["leases"]]  # list: dict ids unhashable
    assert good in ids and None in ids                 # legit + the id-less row both reported
    assert "branch--strres--001" in ids                # non-dict resource degrades, still rows
    assert (tmp_path / "leases" / f"{good}.orphaned").exists()   # legit orphan marked
    assert not (tmp_path / "leases" / "branch--evil--001.orphaned").exists()
    assert r.stderr.count("un-markable lease id") == 4           # all four poisoned rows skipped
    assert "non-record lease file" in r.stderr                   # list-body file skipped (r8 M2)


def test_next_skips_a_poisoned_pool_row(tmp_path):
    """1522 r5 M1: a hand-edited pool row with a traversal body id sorts first
    (oldest) but must be skipped, not pathed — the legit row is still claimed."""
    leases = tmp_path / "leases"
    leases.mkdir(parents=True, exist_ok=True)
    leases.joinpath("branch--evil--001.json").write_text(json.dumps(
        {"id": "../../../tmp/evil", "resource": {"type": "branch"}, "holder": None,
         "state": "assigned", "created": "2000-01-01T00:00:00Z"}))     # oldest, poisoned
    leases.joinpath("branch--alias--001.json").write_text(json.dumps(
        {"id": "branch--elsewhere--042", "resource": {"type": "branch"}, "holder": None,
         "state": "assigned", "created": "2000-01-02T00:00:00Z"}))     # aliased body id (r9 M3)
    good = _run_cli(tmp_path, "fleet-claim",
                    ["--assign", '{"type":"branch","branch":"poolgood"}'],
                    session="coord").stdout.strip()
    r = _run_cli(tmp_path, "fleet-claim", ["--next", "branch"], session="w1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == good, "the poisoned oldest rows must be skipped, legit row claimed"
    assert "unclaimable" in r.stderr, "the aliased row is skipped with a warning, not a wedge"


def test_next_prints_id_even_when_event_log_is_broken(tmp_path):
    """1522 r9 M2: the pool claim's id must reach stdout before the fallible
    emit — --next's caller has no other way to learn WHICH lease it now holds."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"pn"}'],
                   session="coord").stdout.strip()
    ev = tmp_path / "events.jsonl"
    if ev.exists():
        ev.unlink()
    ev.mkdir()                                     # emit target is a dir -> fails
    r = _run_cli(tmp_path, "fleet-claim", ["--next", "branch"], session="w1")
    assert r.returncode != 0, "a broken log must be loud"
    assert lid in r.stdout, "the claimed id must still reach stdout"
    assert json.loads((tmp_path / "leases" / f"{lid}.json").read_text())["holder"] == "w1"


def test_assigned_orphan_with_live_holder_emits_once_not_per_sweep(tmp_path):
    """1522 r9 H1: an assigned-state orphan whose holder is heartbeating (worker
    keeps turning but never ran --activate) must emit once, not on every sweep —
    the r7 liveness-vs-marker resolution applies only to ACTIVE-state orphans."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"h1"}', "--holder", "w1"],
                   session="coord").stdout.strip()
    _backdate(tmp_path, lid, "created", 30)        # blew the activate-timeout
    hbdir = tmp_path / "sessions" / "w1"
    hbdir.mkdir(parents=True, exist_ok=True)
    (hbdir / "hb").touch()                         # the holder is alive and turning
    _run_cli(tmp_path, "fleet-status", [], session="obs1")
    assert _orphan_count(tmp_path, lid) == 1
    time.sleep(0.02)
    (hbdir / "hb").touch()                         # another turn AFTER the marker
    _run_cli(tmp_path, "fleet-status", [], session="obs2")
    _run_cli(tmp_path, "fleet-status", [], session="obs3")
    assert _orphan_count(tmp_path, lid) == 1, \
        "the SAME assigned orphaning must not re-emit per sweep"
    assert (tmp_path / "leases" / f"{lid}.orphaned").exists()


def test_heartbeat_resume_clears_marker_so_next_death_re_emits(tmp_path):
    """1522 r6 MEDIUM: an orphaning that resolves via heartbeat RESUME (a long
    tool call ending — no ledger write) must not leave a marker that suppresses
    the next genuine death's event."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"hbres"}', "--holder", "w1", "--ttl", "1"],
                   session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    hb = tmp_path / "sessions" / "w1" / "hb"
    old = time.time() - 1800
    os.utime(hb, (old, old))                     # a long tool call: hb goes stale
    _run_cli(tmp_path, "fleet-status", [], session="obs1")
    assert (tmp_path / "leases" / f"{lid}.orphaned").exists()
    assert _orphan_count(tmp_path, lid) == 1
    hb.touch()                                   # the turn ends: heartbeat resumes
    _run_cli(tmp_path, "fleet-status", [], session="obs2")
    assert not (tmp_path / "leases" / f"{lid}.orphaned").exists(), \
        "a resumed heartbeat must clear the stale marker"
    assert _orphan_count(tmp_path, lid) == 1     # the recovery itself emits nothing
    os.utime(hb, (old, old))                     # hours later: the real death
    _run_cli(tmp_path, "fleet-status", [], session="obs3")
    assert _orphan_count(tmp_path, lid) == 2, "the real death must emit again"


def test_id_re_rejects_trailing_newline_and_accepts_wide_counter():
    """1522 r6 lows: `$` admitted a trailing newline; `\\d{3}` hard-failed the
    1000th lease for a hot slug."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("fleetlib_idre", str(BIN / "fleetlib.py"))
    spec = importlib.util.spec_from_loader("fleetlib_idre", loader)
    flib = importlib.util.module_from_spec(spec)
    loader.exec_module(flib)
    assert not flib._ID_RE.match("branch--x--001\n")
    assert flib._ID_RE.match("branch--x--001")
    assert flib._ID_RE.match("branch--x--1000")


def test_re_orphaning_with_no_sweep_in_live_window_still_emits(tmp_path):
    """1522 r7 M1: mark+emit, heartbeat resumes, NOBODY sweeps during the live
    window (sweeps are ad-hoc — the common case), then the real death. The mark
    path must self-heal via liveness-newer-than-marker, not depend on an
    intermediate sweep having run."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"nosweep"}', "--holder", "w1", "--ttl", "1"],
                   session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    hb = tmp_path / "sessions" / "w1" / "hb"
    now = time.time()
    os.utime(hb, (now - 3600, now - 3600))         # first orphaning
    _run_cli(tmp_path, "fleet-status", [], session="obs1")
    marker = tmp_path / "leases" / f"{lid}.orphaned"
    assert marker.exists() and _orphan_count(tmp_path, lid) == 1
    # simulate elapsed time: the marker is 30 min old; the heartbeat resumed
    # 15 min ago (AFTER the marker) and the session then died (stale vs ttl=1m).
    # No sweep ran in between.
    os.utime(marker, (now - 1800, now - 1800))
    os.utime(hb, (now - 900, now - 900))
    _run_cli(tmp_path, "fleet-status", [], session="obs2")
    assert _orphan_count(tmp_path, lid) == 2, \
        "the real death must emit even though no sweep saw the live window"
    assert marker.exists(), "a fresh marker stands for the new orphaning"
    _run_cli(tmp_path, "fleet-status", [], session="obs3")
    assert _orphan_count(tmp_path, lid) == 2, "the SAME orphaning must not re-emit"


def test_budget_with_broken_log_still_displays(tmp_path):
    """1522 r7 low: a failing budget-checkpoint emit must not blank the output —
    display first, then fail non-zero with the error named."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"budget","tokens":1000}', "--holder", "w1"],
                   session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    ev = tmp_path / "events.jsonl"
    if ev.exists():
        ev.unlink()
    ev.mkdir()                                     # emit target is a dir -> fails
    r = _run_cli(tmp_path, "fleet-status", ["--budget"], session="obs")
    assert r.returncode != 0, "a broken log stays loud"
    assert lid in r.stdout, "rows and budget must still display"
    assert "budget-checkpoint emit failed" in r.stderr


def test_aliased_body_id_is_refused_loudly(tmp_path):
    """1522 r8 M1: a body id that is a DIFFERENT valid id (the template-copy
    mistake) must be refused — not silently write the alias target's file under
    the argument's lock while the argument lease goes stale."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"al"}', "--holder", "w1"],
                   session="coord").stdout.strip()
    p = tmp_path / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text())
    rec["id"] = "branch--other--007"                # valid shape, wrong identity
    p.write_text(json.dumps(rec))
    r = _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    assert r.returncode != 0 and "does not match its filename" in r.stderr
    assert not (tmp_path / "leases" / "branch--other--007.json").exists(), \
        "the alias target must never be written"
    r2 = _run_cli(tmp_path, "fleet-release", [lid], session="w1")
    assert r2.returncode != 0 and "does not match its filename" in r2.stderr


def test_budget_tokens_junk_degrades_row_not_run(tmp_path):
    """1522 r8 M2: non-int budget tokens is the ttl_minutes lesson again — warn,
    treat as 0, never blank the --budget run."""
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"budget","tokens":1000}', "--holder", "w1"],
                   session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    p = tmp_path / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text())
    rec["resource"]["tokens"] = "lots"
    p.write_text(json.dumps(rec))
    r = _run_cli(tmp_path, "fleet-status", ["--budget"], session="obs")
    assert r.returncode == 0, r.stderr
    assert "non-integer budget tokens" in r.stderr
    assert lid in r.stdout


def test_invalid_payload_sid_falls_back_to_env(tmp_path):
    """1522 r8 L1: a payload session_id that fails validation must not suppress a
    valid EUNOMIA_SESSION fallback."""
    r = _hb(tmp_path, stdin_data=json.dumps({"session_id": "bad sid"}),
            env_extra={"EUNOMIA_SESSION": "env-ok"})
    assert r.returncode == 0 and r.stderr == ""
    assert (tmp_path / "sessions" / "env-ok" / "hb").exists()


def test_marker_hygiene_survives_torn_lease_file(tmp_path):
    """1522 r10 low 1: _clear_stale_marker hitting a torn lease file (out-of-
    library writer between scan and clear) must warn and continue, not kill the
    incident-time tool. Driven as a unit call — the race window is not
    reproducible through the CLI (all_leases skips torn files at scan time)."""
    import importlib.machinery
    import importlib.util
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path)
    try:
        loader = importlib.machinery.SourceFileLoader("fs_torn", str(BIN / "fleet-status"))
        spec = importlib.util.spec_from_loader("fs_torn", loader)
        st = importlib.util.module_from_spec(spec)
        loader.exec_module(st)
        leases = tmp_path / "leases"
        leases.mkdir(parents=True, exist_ok=True)
        (leases / "branch--torn--001.json").write_text("{not json")
        (leases / "branch--torn--001.orphaned").touch()
        st._clear_stale_marker("branch--torn--001")     # must not raise/exit
        assert (leases / "branch--torn--001.orphaned").exists(), \
            "hygiene must not remove the marker when the record is unreadable"
    finally:
        os.environ.pop("EUNOMIA_FLEET_DIR", None)


def test_assign_non_list_globs_named_refusal(tmp_path):
    """1522 r10 low 4: a non-list globs value gets a named refusal, not a raw
    KeyError traceback."""
    r = _run_cli(tmp_path, "fleet-claim",
                 ["--assign", '{"type":"paths","globs":{}}'], session="coord")
    assert r.returncode != 0
    assert "globs must be a list" in r.stderr
    assert "Traceback" not in r.stderr
