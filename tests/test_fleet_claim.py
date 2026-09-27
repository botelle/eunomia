"""Tests for fleet-claim / fleet-release / fleet-status (plan 0003 §4).

Run: python3 -m pytest tests/ -q   (stdlib + pytest only)
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, fleet_dir, session="s-test"):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["EUNOMIA_SESSION"] = session
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / name))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _run_cli(fleet_dir, script, args, session="s-test"):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet_dir), EUNOMIA_SESSION=session)
    return subprocess.run([sys.executable, str(BIN / script)] + args,
                          env=env, capture_output=True, text=True)


def _read_lease(fleet_dir, lease_id):
    return json.loads((fleet_dir / "leases" / f"{lease_id}.json").read_text())


def _backdate(fleet_dir, lease_id, field, minutes):
    p = fleet_dir / "leases" / f"{lease_id}.json"
    rec = json.loads(p.read_text())
    old = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rec[field] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(json.dumps(rec))
    return rec


def _events(fleet_dir):
    p = fleet_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l]


# ---------------------------------------------------------------- lifecycle

def test_assign_activate_round_trip(tmp_path):
    r = _run_cli(tmp_path, "fleet-claim",
                 ["--assign", '{"type":"branch","repo":"x/y","branch":"w1/s"}',
                  "--holder", "w1", "--ttl", "60"], session="coord")
    assert r.returncode == 0, r.stderr
    lid = r.stdout.strip()
    rec = _read_lease(tmp_path, lid)
    assert rec["state"] == "assigned" and rec["holder"] == "w1" and rec["activated"] is None

    r = _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    assert r.returncode == 0, r.stderr
    rec = _read_lease(tmp_path, lid)
    assert rec["state"] == "active" and rec["activated"]
    assert (tmp_path / "sessions" / "w1" / "hb").exists(), "activation starts the heartbeat"
    types = [e["type"] for e in _events(tmp_path)]
    assert types == ["lease-assigned", "lease-activated"]


def test_activate_by_wrong_session_refuses(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1"],
                   session="coord").stdout.strip()
    r = _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w2")
    assert r.returncode != 0
    assert "verification, not acquisition" in r.stderr
    assert _read_lease(tmp_path, lid)["holder"] == "w1", "refusal must not mutate"


# ---------------------------------------------------------------- pool

def test_next_claims_oldest_and_concurrent_pullers_get_distinct_rows(tmp_path):
    a = _run_cli(tmp_path, "fleet-claim", ["--assign", '{"type":"sim","host":"opshost"}'],
                 session="coord").stdout.strip()
    _backdate(tmp_path, a, "created", 5)   # a is older
    b = _run_cli(tmp_path, "fleet-claim", ["--assign", '{"type":"sim","host":"opshost"}'],
                 session="coord").stdout.strip()

    procs = [subprocess.Popen(
        [sys.executable, str(BIN / "fleet-claim"), "--next", "sim"],
        env=dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION=f"p{i}"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
    outs = [p.communicate() for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    got = sorted(o[0].strip() for o in outs)
    assert got == sorted([a, b]), "two concurrent pullers must claim two DIFFERENT rows"
    # the older row went to a puller first (fairness)
    assert _read_lease(tmp_path, a)["holder"] is not None


def test_pool_row_never_orphans_and_takeover_refuses(tmp_path, monkeypatch):
    lid = _run_cli(tmp_path, "fleet-claim", ["--assign", '{"type":"sim","host":"opshost"}'],
                   session="coord").stdout.strip()
    _backdate(tmp_path, lid, "created", 600)   # ancient pool row
    st = _load("fleet-status", tmp_path)
    rows, _ = st._rows("t")
    assert rows[0]["orphaned"] is False, "pool rows are queued work, never orphans"
    r = _run_cli(tmp_path, "fleet-claim", ["--takeover", lid], session="thief")
    assert r.returncode != 0 and "pool row" in r.stderr


# ---------------------------------------------------------------- orphan derivation

def test_assigned_with_holder_orphans_after_timeout(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1"],
                   session="coord").stdout.strip()
    st = _load("fleet-status", tmp_path)
    assert st._rows("t")[0][0]["orphaned"] is False
    _backdate(tmp_path, lid, "created", 30)
    assert st._rows("t")[0][0]["orphaned"] is True


def test_active_orphans_only_after_activated_plus_ttl_without_hb(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1",
                    "--ttl", "60"], session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    (tmp_path / "sessions" / "w1" / "hb").unlink()   # no heartbeat hook yet (row 4)
    st = _load("fleet-status", tmp_path)
    assert st._rows("t")[0][0]["orphaned"] is False, "fresh activation is live via fallback"
    _backdate(tmp_path, lid, "activated", 120)
    assert st._rows("t")[0][0]["orphaned"] is True, "ttl past activated with no hb = orphan"


def test_fresh_heartbeat_unorphans(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1",
                    "--ttl", "60"], session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    _backdate(tmp_path, lid, "activated", 120)
    hb = tmp_path / "sessions" / "w1" / "hb"
    hb.touch()   # a live heartbeat overrides the stale activated
    st = _load("fleet-status", tmp_path)
    assert st._rows("t")[0][0]["orphaned"] is False


# ---------------------------------------------------------------- marker + events

def test_orphan_marker_emits_exactly_once_and_rearms_after_takeover(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1"],
                   session="coord").stdout.strip()
    _backdate(tmp_path, lid, "created", 30)
    for script_run in range(3):
        r = _run_cli(tmp_path, "fleet-status", [], session="obs")
        assert r.returncode == 0
    orphan_events = [e for e in _events(tmp_path) if e["type"] == "lease-orphaned"]
    assert len(orphan_events) == 1, "O_CREAT|O_EXCL is the once-only guard"
    assert (tmp_path / "leases" / f"{lid}.orphaned").exists()

    r = _run_cli(tmp_path, "fleet-claim", ["--takeover", lid], session="w2")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "leases" / f"{lid}.orphaned").exists(), "takeover clears the marker"

    # orphan again: backdate activated AND age the successor's heartbeat —
    # takeover touched sessions/w2/hb, and a fresh hb correctly keeps the lease
    # live regardless of activated (liveness prefers the heartbeat).
    _backdate(tmp_path, lid, "activated", 600)
    hb = tmp_path / "sessions" / "w2" / "hb"
    old_ts = time.time() - 600 * 60
    os.utime(hb, (old_ts, old_ts))
    _run_cli(tmp_path, "fleet-status", [], session="obs")
    orphan_events = [e for e in _events(tmp_path) if e["type"] == "lease-orphaned"]
    assert len(orphan_events) == 2, "a re-orphaned lease must emit again"


# ---------------------------------------------------------------- takeover

def test_racing_takeovers_exactly_one_winner(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "dead"],
                   session="coord").stdout.strip()
    _backdate(tmp_path, lid, "created", 30)
    procs = [subprocess.Popen(
        [sys.executable, str(BIN / "fleet-claim"), "--takeover", lid],
        env=dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION=f"succ{i}"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
    results = [(p.communicate(), p.returncode) for p in procs]
    codes = sorted(rc for _, rc in results)
    assert codes[0] == 0 and codes[1] != 0, f"exactly one winner: {results}"
    loser_err = [out[1] for out, rc in results if rc != 0][0]
    assert "not orphaned" in loser_err, "loser must see fresh liveness under the lock"
    assert _read_lease(tmp_path, lid)["holder"].startswith("succ")


def test_takeover_of_live_lease_refuses(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1",
                    "--ttl", "60"], session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="w1")
    r = _run_cli(tmp_path, "fleet-claim", ["--takeover", lid], session="thief")
    assert r.returncode != 0 and "not orphaned" in r.stderr


# ---------------------------------------------------------------- release

def test_release_by_nonholder_refuses_and_force_records(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1"],
                   session="coord").stdout.strip()
    r = _run_cli(tmp_path, "fleet-release", [lid], session="w2")
    assert r.returncode != 0 and "held by" in r.stderr
    r = _run_cli(tmp_path, "fleet-release", [lid, "--force"], session="w2")
    assert r.returncode == 0, r.stderr
    ev = [e for e in _events(tmp_path) if e["type"] == "lease-released"][0]
    assert ev["detail"] == {"forced": True, "holder_was": "w1"}, \
        "the override is recorded, not papered over"
    # idempotent double release
    r = _run_cli(tmp_path, "fleet-release", [lid], session="w2")
    assert r.returncode == 0


# ---------------------------------------------------------------- reader integrity

def test_readers_never_see_torn_records(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"branch","branch":"b"}', "--holder", "w1"],
                   session="coord").stdout.strip()
    mod = _load("fleet-claim", tmp_path, session="w1")
    lease_file = tmp_path / "leases" / f"{lid}.json"
    for i in range(200):
        rec = json.loads(lease_file.read_text())
        rec["note"] = "x" * (i % 97)
        mod.lib.write_lease(rec)          # temp+rename under the writer protocol
        seen = json.loads(lease_file.read_text())   # lock-free reader
        assert seen["id"] == lid, "reader saw a torn or partial record"


# ---------------------------------------------------------------- budget

def test_budget_reports_against_scratch_meter_and_degrades_without_it(tmp_path):
    lid = _run_cli(tmp_path, "fleet-claim",
                   ["--assign", '{"type":"budget","tokens":50000}', "--holder", "sess-b"],
                   session="coord").stdout.strip()
    _run_cli(tmp_path, "fleet-claim", ["--activate", lid], session="sess-b")

    # no meter: degrades, still reports
    r = _run_cli(tmp_path, "fleet-status", ["--budget", "--json"], session="obs")
    assert r.returncode == 0, r.stderr
    b = json.loads(r.stdout)["budget"][0]
    assert b["meter"] == "no meter" and b["used_out_tokens"] is None

    # seed a scratch fleet.db shaped like the collector's schema
    import sqlite3
    con = sqlite3.connect(tmp_path / "fleet.db")
    con.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, session_uuid TEXT);"
        "CREATE TABLE turn (session_id TEXT, ts TEXT, out_tok INTEGER);")
    con.execute("INSERT INTO session VALUES ('p1','sess-b')")
    future = "2099-01-01T00:00:00Z"; past = "2000-01-01T00:00:00Z"
    con.execute("INSERT INTO turn VALUES ('p1', ?, 1200)", (future,))
    con.execute("INSERT INTO turn VALUES ('p1', ?, 999)", (past,))   # pre-activation
    con.commit(); con.close()

    r = _run_cli(tmp_path, "fleet-status", ["--budget", "--json"], session="obs")
    b = json.loads(r.stdout)["budget"][0]
    assert b["used_out_tokens"] == 1200, "only usage since activation counts"
    assert any(e["type"] == "budget-checkpoint" for e in _events(tmp_path))


# ---------------------------------------------------------------- id allocation

def test_assign_ids_do_not_collide(tmp_path):
    ids = set()
    for _ in range(3):
        lid = _run_cli(tmp_path, "fleet-claim",
                       ["--assign", '{"type":"sim","host":"opshost"}'],
                       session="coord").stdout.strip()
        ids.add(lid)
    assert len(ids) == 3
    assert sorted(ids) == ["sim--opshost--001", "sim--opshost--002", "sim--opshost--003"]


# ------------------------------------------- one holder per resource (mutex)

_RES = '{"type":"branch","repo":"operator/x","branch":"feat/one"}'


def _assign(fleet_dir, resource, holder, note="t"):
    return _run_cli(fleet_dir, "fleet-claim",
                    ["--assign", resource, "--holder", holder, "--ttl", "60", "--note", note])


def test_a_second_holder_on_one_resource_is_refused(tmp_path):
    """Until 2026-09-07 --assign refused nothing here: two sessions naming one
    branch both succeeded and fleet-status showed two rows. The ledger recorded
    the collision instead of preventing it."""
    first = _assign(tmp_path, _RES, "session-a")
    assert first.returncode == 0, first.stderr
    lease_id = first.stdout.strip().splitlines()[-1]

    second = _assign(tmp_path, _RES, "session-b")
    assert second.returncode != 0, "a different holder must be refused"
    assert lease_id in second.stderr, "the refusal must name the lease that holds it"
    assert "takeover" in second.stderr, "and point at the sanctioned path"
    # and it must not have written a second row
    rows = list((tmp_path / "leases").glob("*.json"))
    assert len(rows) == 1, [p.name for p in rows]


def test_the_same_holder_is_idempotent(tmp_path):
    """A session re-running its launch block should get its lease id back —
    not a second row, and not a failure."""
    first = _assign(tmp_path, _RES, "session-a")
    lease_id = first.stdout.strip().splitlines()[-1]

    again = _assign(tmp_path, _RES, "session-a", note="re-run")
    assert again.returncode == 0, again.stderr
    assert again.stdout.strip().splitlines()[-1] == lease_id
    assert len(list((tmp_path / "leases").glob("*.json"))) == 1


def test_a_different_resource_is_unaffected(tmp_path):
    assert _assign(tmp_path, _RES, "session-a").returncode == 0
    other = '{"type":"branch","repo":"operator/x","branch":"feat/two"}'
    assert _assign(tmp_path, other, "session-b").returncode == 0


def test_release_frees_the_resource(tmp_path):
    first = _assign(tmp_path, _RES, "session-a")
    lease_id = first.stdout.strip().splitlines()[-1]
    assert _run_cli(tmp_path, "fleet-release", [lease_id], session="session-a").returncode == 0
    assert _assign(tmp_path, _RES, "session-b").returncode == 0, "released is not held"


def test_an_unreadable_lease_file_refuses_the_grant(tmp_path):
    """all_leases skips a corrupt row with a warning — right for a reader sweep,
    wrong for a conflict check, because the row it skipped may BE the conflict.
    Not being able to prove there is no conflict is not the same as there being
    none."""
    assert _assign(tmp_path, _RES, "session-a").returncode == 0
    (tmp_path / "leases" / "corrupt.json").write_text("{not json")

    other = '{"type":"branch","repo":"operator/x","branch":"feat/three"}'
    r = _assign(tmp_path, other, "session-b")
    assert r.returncode != 0, "must fail closed on an unreadable ledger"
    assert "unreadable" in r.stderr


def test_an_orphaned_lease_does_not_block_a_successor(tmp_path):
    """Respawn assigns the successor BEFORE retiring the dead lease, because
    retiring first "stranded the plan whenever that assign failed"
    (docs/plan-dispatch.md). A lease whose holder stopped heartbeating holds
    nothing, so it must not refuse the successor. Blocking on one broke three
    watcher respawn tests before this exemption existed."""
    first = _assign(tmp_path, _RES, "session-a")
    lease_id = first.stdout.strip().splitlines()[-1]
    # assigned-but-never-activated orphans after the activate timeout
    _backdate(tmp_path, lease_id, "created", 600)

    successor = _assign(tmp_path, _RES, "session-b", note="successor")
    assert successor.returncode == 0, successor.stderr
    assert successor.stdout.strip().splitlines()[-1] != lease_id


def test_a_pool_row_neither_blocks_nor_is_blocked(tmp_path):
    """An --assign with no holder is an offer claimed later through --next, and
    several offers on one resource are how capacity is expressed. An earlier
    revision refused any second row and broke the puller model."""
    pool = '{"type":"sim","host":"opshost"}'
    a = _run_cli(tmp_path, "fleet-claim", ["--assign", pool], session="coord")
    b = _run_cli(tmp_path, "fleet-claim", ["--assign", pool], session="coord")
    assert a.returncode == 0 and b.returncode == 0, (a.stderr, b.stderr)
    assert a.stdout.strip() != b.stdout.strip(), "two offers, two rows"
    # and a held lease is not blocked by an outstanding offer
    held = _run_cli(tmp_path, "fleet-claim",
                    ["--assign", pool, "--holder", "session-a", "--ttl", "60"])
    assert held.returncode == 0, held.stderr
