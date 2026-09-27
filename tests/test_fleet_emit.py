#!/usr/bin/env python3
"""Tests for fleet-emit.

Run: python3 -m pytest tests/ -q      (stdlib only; no third-party deps)
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, fleet_dir):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(BIN / name))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.FLEET_DIR = fleet_dir
    mod.EVENTS = fleet_dir / "events.jsonl"
    mod.LOCK = fleet_dir / "locks" / "events.lock"
    return mod


def _events(fleet_dir):
    p = fleet_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l]


def test_emits_one_line_with_all_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    rc = mod.main(["pr-opened", "--repo", "operator/sniff", "--pr", "41", "--sha", "a1b2c3d"])
    assert rc == 0
    events = _events(tmp_path)
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "pr-opened"
    assert e["actor"] == "session-abc"
    assert e["repo"] == "operator/sniff"
    assert e["pr"] == 41
    assert e["sha"] == "a1b2c3d"
    assert e["lease"] is None
    assert e["detail"] == {}
    assert e["ts"].endswith("Z")


def test_unknown_type_rejected_non_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    try:
        mod.main(["not-a-real-type"])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("expected SystemExit for unknown event type")
    assert _events(tmp_path) == []


def test_lease_event_requires_lease_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    try:
        mod.main(["lease-activated"])
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit: lease-* event without --lease")
    assert _events(tmp_path) == []


def test_non_lease_event_forbids_lease_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    try:
        mod.main(["pr-opened", "--lease", "branch--x--001"])
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit: non-lease event with --lease")
    assert _events(tmp_path) == []


def test_lease_event_with_lease_id_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    rc = mod.main(["lease-activated", "--lease", "branch--x--001"])
    assert rc == 0
    assert _events(tmp_path)[0]["lease"] == "branch--x--001"


def test_detail_json_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    mod.main(["budget-checkpoint", "--detail-json", '{"tokens": 5000000, "used": 120000}'])
    e = _events(tmp_path)[0]
    assert e["detail"] == {"tokens": 5000000, "used": 120000}


def test_invalid_detail_json_rejected_and_not_echoed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    try:
        mod.main(["budget-checkpoint", "--detail-json", 'not json {secretlike=hunter2'])
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit for invalid --detail-json")
    assert _events(tmp_path) == []
    out = capsys.readouterr()
    assert "hunter2" not in out.err
    assert "hunter2" not in out.out


def test_detail_json_must_be_an_object(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    try:
        mod.main(["budget-checkpoint", "--detail-json", "[1,2,3]"])
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit for a non-object --detail-json")
    assert _events(tmp_path) == []


def test_missing_actor_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("EUNOMIA_SESSION", raising=False)
    mod = _load("fleet-emit", tmp_path)
    try:
        mod.main(["session-spawned"])
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit: no EUNOMIA_SESSION and no --session")
    assert _events(tmp_path) == []


def test_session_flag_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-env")
    mod = _load("fleet-emit", tmp_path)
    mod.main(["session-spawned", "--session", "session-flag"])
    assert _events(tmp_path)[0]["actor"] == "session-flag"


def test_multiple_emits_append_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    mod.main(["session-spawned"])
    mod.main(["session-stopped"])
    mod.main(["pr-opened", "--repo", "operator/sniff", "--pr", "1"])
    events = _events(tmp_path)
    assert [e["type"] for e in events] == ["session-spawned", "session-stopped", "pr-opened"]


def test_events_file_opened_by_path_after_rotation(tmp_path, monkeypatch):
    """The append must land in whatever file is current AFTER rotation, not a
    handle opened before the lock was acquired (SPEC.md locking protocol)."""
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    mod.EVENTS.parent.mkdir(parents=True, exist_ok=True)
    mod.EVENTS.write_text("x" * (mod.ROTATE_BYTES - 1) + "\n")

    mod.main(["session-spawned"])

    archives = list(tmp_path.glob("events-*.jsonl"))
    assert len(archives) == 1
    assert archives[0].read_text() == "x" * (mod.ROTATE_BYTES - 1) + "\n"
    # The new events.jsonl holds only the fresh event, not the padding.
    fresh = _events(tmp_path)
    assert len(fresh) == 1
    assert fresh[0]["type"] == "session-spawned"


def test_rotation_collision_refuses_to_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    mod.EVENTS.parent.mkdir(parents=True, exist_ok=True)
    mod.EVENTS.write_text("x" * (mod.ROTATE_BYTES - 1) + "\n")

    stamp = "20260101-000000"
    (tmp_path / f"events-{stamp}.jsonl").write_text("pre-existing archive")

    class FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return FrozenDatetime()

        @staticmethod
        def strftime(fmt):
            return stamp

    orig = mod.datetime
    mod.datetime = FrozenDatetime
    try:
        try:
            mod.main(["session-spawned"])
        except SystemExit as e:
            assert "already exists" in str(e)
        else:
            raise AssertionError("expected SystemExit on archive-name collision")
    finally:
        mod.datetime = orig
    # events.jsonl untouched by the aborted rotation attempt's rename path.
    assert mod.EVENTS.read_text() == "x" * (mod.ROTATE_BYTES - 1) + "\n"


def test_no_rotation_under_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    mod.main(["session-spawned"])
    mod.main(["session-stopped"])
    assert list(tmp_path.glob("events-*.jsonl")) == []
    assert len(_events(tmp_path)) == 2


def test_lockfile_created_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    mod.main(["session-spawned"])
    assert mod.LOCK.exists()
    mtime1 = mod.LOCK.stat().st_mtime
    mod.main(["session-stopped"])
    # Same lockfile reused (never deleted, never rotated), per SPEC.md layout.
    assert mod.LOCK.exists()
    assert mod.LOCK.stat().st_mtime >= mtime1


def test_repo_and_lease_default_null_when_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    mod.main(["session-spawned"])
    e = _events(tmp_path)[0]
    assert e["repo"] is None
    assert e["pr"] is None
    assert e["sha"] is None
    assert e["lease"] is None


def test_eunomia_fleet_dir_env_var_is_honoured(tmp_path, monkeypatch):
    """The module reads EUNOMIA_FLEET_DIR at import time; verify via subprocess
    so the real env-var wiring (not the test's monkeypatched attrs) is exercised."""
    import subprocess
    fleet_dir = tmp_path / "custom-fleet"
    env = dict(os.environ)
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    env["EUNOMIA_SESSION"] = "session-abc"
    result = subprocess.run(
        [sys.executable, str(BIN / "fleet-emit"), "session-spawned"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (fleet_dir / "events.jsonl").exists()
    events = _events(fleet_dir)
    assert events[0]["type"] == "session-spawned"


def test_concurrent_writers_race_rotation_threshold_no_loss_no_duplication(tmp_path):
    """The scenario STEP 3 is actually about: many writers hit `--detail-json`
    append calls at once while events.jsonl sits right at the 10 MB line. Real
    subprocesses, real flock — not monkeypatched internals — because the
    property under test (rotation happens exactly once, every writer's event
    survives exactly once, nothing is corrupted) only means something under
    genuine OS-level lock contention."""
    import concurrent.futures
    import subprocess

    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir()
    events = fleet_dir / "events.jsonl"
    events.write_text("x" * (10 * 1024 * 1024 - 1) + "\n")  # at the threshold, newline-terminated

    env = dict(os.environ)
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    env["EUNOMIA_SESSION"] = "session-race"

    n = 50   # the plan DoD names fifty; test what it says

    def one_emit(i):
        return subprocess.run(
            [sys.executable, str(BIN / "fleet-emit"), "session-spawned",
             "--detail-json", json.dumps({"i": i})],
            env=env, capture_output=True, text=True,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(one_emit, range(n)))

    for r in results:
        assert r.returncode == 0, r.stderr

    archives = sorted(fleet_dir.glob("events-*.jsonl"))
    assert len(archives) == 1, "exactly one rotation, no matter how many writers raced it"
    assert archives[0].read_text() == "x" * (10 * 1024 * 1024 - 1) + "\n", "archive holds the pre-rotation content verbatim"

    live_lines = events.read_text().splitlines()
    assert len(live_lines) == n, "every writer's event landed in the fresh file, none lost, none doubled"

    seen_indices = set()
    for line in live_lines:
        e = json.loads(line)  # every line well-formed: no interleaved/torn writes
        assert e["type"] == "session-spawned"
        i = e["detail"]["i"]
        assert i not in seen_indices, f"index {i} written more than once"
        seen_indices.add(i)
    assert seen_indices == set(range(n))


def test_repeated_rotations_under_load_lose_nothing(tmp_path, monkeypatch):
    """Grafted from the losing bakeoff arm: many rotations in one run exercises the
    rename/reopen path far more times than a single-rotation test. Two adaptations
    per this repo's design decisions: the threshold is a patched module constant
    (never a production env knob — a permanent surface added for a test's
    convenience is a surface SPEC never asked for), and the clock is a stepping
    fake — fleet-emit REFUSES a same-second rotation by design (loud refusal beat
    the losing arm's busy-wait in review), so a burst test must advance time the
    way reality would, not disable the refusal."""
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    mod.ROTATE_BYTES = 400          # tiny: force a rotation every couple of events

    import datetime as _dt
    base = _dt.datetime(2026, 8, 27, 12, 0, 0, tzinfo=_dt.timezone.utc)
    tick = {"n": 0}

    class SteppingDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            tick["n"] += 1          # every call is one second later
            return base + _dt.timedelta(seconds=tick["n"])

    monkeypatch.setattr(mod, "datetime", SteppingDateTime)

    total = 60
    for i in range(total):
        assert mod.main(["session-spawned", "--detail-json", json.dumps({"i": i})]) == 0

    seen = []
    for p2 in sorted(tmp_path.glob("events*.jsonl")):
        seen += [json.loads(l)["detail"]["i"] for l in p2.read_text().splitlines() if l]
    assert sorted(seen) == list(range(total)), "an event was lost or duplicated across rotations"
    archives = list(tmp_path.glob("events-*.jsonl"))
    assert len(archives) >= 5, f"expected many rotations, got {len(archives)}"
    # every archive parses cleanly — no torn line rode through a rename
    for p2 in archives:
        for l in p2.read_text().splitlines():
            json.loads(l)


def test_torn_tail_never_corrupts_the_next_event(tmp_path, monkeypatch):
    """Review 1489 F1: a writer killed mid-append leaves no trailing newline; the
    next healthy emit used to MERGE into the torn line — exiting 0 and echoing
    success while its event was unreadable forever. The tail repair closes the
    torn line under the same lock, so the torn record is the only casualty."""
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    assert mod.main(["session-spawned"]) == 0
    with open(mod.EVENTS, "a") as fh:
        fh.write('{"ts":"2026-08-27T00:00:00Z","actor":"s","type":"sessi')  # torn
    assert mod.main(["session-stopped"]) == 0
    lines = mod.EVENTS.read_text().splitlines()
    assert len(lines) == 3
    good = [json.loads(l) for l in lines if l.strip().startswith('{"ts"') and l.strip().endswith("}")]
    types = [e["type"] for e in good if "type" in e and e.get("actor") != "s"]
    assert "session-stopped" in types, "the healthy event after a torn tail must be readable"


def test_capability_published_round_trips_and_a_typo_does_not(tmp_path, monkeypatch):
    """plan 0025 DoD: the type is emittable, and the closed set still closes."""
    monkeypatch.setenv("EUNOMIA_SESSION", "session-abc")
    mod = _load("fleet-emit", tmp_path)
    detail = '{"kind":"skill","name":"review-a-pr","where":"operator/techne"}'
    assert mod.main(["capability-published", "--detail-json", detail]) == 0
    lines = mod.EVENTS.read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["type"] == "capability-published"
    assert rec["detail"]["where"] == "operator/techne"

    before = mod.EVENTS.read_text()
    try:
        mod.main(["capability-publish"])          # a plausible typo
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("expected SystemExit for a typo'd event type")
    assert mod.EVENTS.read_text() == before, "a rejected type must not append"

    # The one thing tests/test_event_type_sync.py cannot assert: that this
    # specific type reached SPEC.md. That test proves the three sets are EQUAL,
    # which stays true if a type is missing from all three.
    spec = (Path(__file__).resolve().parents[1] / "SPEC.md").read_text()
    assert "`capability-published`" in spec, "SPEC.md must declare the type it closes over"
