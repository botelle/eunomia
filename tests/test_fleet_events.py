#!/usr/bin/env python3
"""Tests for fleet-events.

Run: python3 -m pytest tests/ -q      (stdlib only; no third-party deps)
"""
import concurrent.futures
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
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
    return mod


def _write_events(fleet_dir, records):
    fleet_dir.mkdir(parents=True, exist_ok=True)
    with (fleet_dir / "events.jsonl").open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _rec(ts, type_="session-spawned", actor="session-x", **kw):
    r = {"ts": ts, "actor": actor, "type": type_, "repo": None, "pr": None,
         "sha": None, "lease": None, "detail": {}}
    r.update(kw)
    return r


def _run(mod, argv, capsys):
    rc = mod.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_reads_live_events_json_mode(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [
        _rec("2026-08-23T21:00:00Z", "session-spawned"),
        _rec("2026-08-23T21:05:00Z", "pr-opened", repo="operator/sniff", pr=41, sha="a1b2c3d"),
    ])
    rc, out, err = _run(mod, ["--json"], capsys)
    assert rc == 0
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-spawned", "pr-opened"]
    assert lines[1]["pr"] == 41


def test_human_format_omits_null_fields(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z", "session-spawned")])
    rc, out, err = _run(mod, [], capsys)
    assert "session-spawned" in out
    assert "repo=" not in out
    assert "pr=" not in out
    assert "actor=session-x" in out


def test_human_format_shows_present_fields_and_detail(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [
        _rec("2026-08-23T21:00:00Z", "pr-opened", repo="operator/sniff", pr=41, sha="a1b2c3d",
             detail={"note": "hi"}),
    ])
    rc, out, err = _run(mod, [], capsys)
    assert "repo=operator/sniff" in out
    assert "pr=41" in out
    assert "sha=a1b2c3d" in out
    assert '"note":"hi"' in out


def test_type_filter_single(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [
        _rec("2026-08-23T21:00:00Z", "session-spawned"),
        _rec("2026-08-23T21:01:00Z", "session-stopped"),
    ])
    rc, out, err = _run(mod, ["--type", "session-stopped", "--json"], capsys)
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-stopped"]


def test_type_filter_comma_and_repeated(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [
        _rec("2026-08-23T21:00:00Z", "session-spawned"),
        _rec("2026-08-23T21:01:00Z", "session-stopped"),
        _rec("2026-08-23T21:02:00Z", "pr-opened"),
    ])
    rc, out, err = _run(
        mod, ["--type", "session-spawned,session-stopped", "--type", "pr-opened", "--json"], capsys
    )
    lines = [json.loads(l) for l in out.splitlines()]
    assert {e["type"] for e in lines} == {"session-spawned", "session-stopped", "pr-opened"}


def test_unknown_type_rejected_non_zero(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z")])
    try:
        mod.main(["--type", "not-a-real-type"])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("expected SystemExit for unknown event type")


def test_since_absolute_timestamp_filters(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [
        _rec("2026-08-23T21:00:00Z", "session-spawned"),
        _rec("2026-08-23T22:00:00Z", "session-stopped"),
    ])
    rc, out, err = _run(mod, ["--since", "2026-08-23T21:30:00Z", "--json"], capsys)
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-stopped"]


def test_since_date_only_is_inclusive_prefix(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [
        _rec("2026-08-22T23:59:59Z", "session-spawned"),
        _rec("2026-08-23T00:00:00Z", "session-stopped"),
    ])
    rc, out, err = _run(mod, ["--since", "2026-08-23", "--json"], capsys)
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-stopped"]


def test_since_relative_duration(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_events(tmp_path, [
        _rec(old, "session-spawned"),
        _rec(recent, "session-stopped"),
    ])
    rc, out, err = _run(mod, ["--since", "30m", "--json"], capsys)
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-stopped"]


def test_since_without_archives_only_reads_live_file(tmp_path, capsys):
    """Default (no --since) never touches archives — the live tail is the
    default scope, per SPEC's 'tail/filter CLI' description."""
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z", "session-spawned")])
    archive = tmp_path / "events-20260101-000000.jsonl"
    archive.write_text(json.dumps(_rec("2026-01-01T00:00:00Z", "pr-merged")) + "\n")
    rc, out, err = _run(mod, ["--json"], capsys)
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-spawned"]


def test_since_reaches_into_archives(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z", "session-spawned")])
    archive = tmp_path / "events-20260101-120000.jsonl"
    archive.write_text(json.dumps(_rec("2026-01-01T11:00:00Z", "pr-merged")) + "\n")
    rc, out, err = _run(mod, ["--since", "2026-01-01T00:00:00Z", "--json"], capsys)
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["pr-merged", "session-spawned"]


def test_since_skips_archives_entirely_older_than_cutoff(tmp_path, capsys):
    """The archive's rotation-stamp filename bounds its contents; an archive
    that rotated before `since` must never even be opened."""
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z", "session-spawned")])
    too_old = tmp_path / "events-20250101-000000.jsonl"
    too_old.write_text("THIS SHOULD NEVER BE READ - not even valid json\n")
    rc, out, err = _run(mod, ["--since", "2026-01-01T00:00:00Z", "--json"], capsys)
    assert rc == 0  # if the archive had been opened, invalid JSON would just be
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-spawned"]


def test_archives_read_in_chronological_order(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z", "budget-checkpoint")])
    (tmp_path / "events-20260101-000000.jsonl").write_text(
        json.dumps(_rec("2026-01-01T00:00:00Z", "session-spawned")) + "\n"
    )
    (tmp_path / "events-20260215-000000.jsonl").write_text(
        json.dumps(_rec("2026-02-15T00:00:00Z", "session-stopped")) + "\n"
    )
    rc, out, err = _run(mod, ["--since", "2020-01-01", "--json"], capsys)
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-spawned", "session-stopped", "budget-checkpoint"]


def test_malformed_line_is_skipped_not_fatal(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    with (tmp_path / "events.jsonl").open("a") as fh:
        fh.write(json.dumps(_rec("2026-08-23T21:00:00Z", "session-spawned")) + "\n")
        fh.write("{not valid json\n")
        fh.write(json.dumps(_rec("2026-08-23T21:01:00Z", "session-stopped")) + "\n")
    rc, out, err = _run(mod, ["--json"], capsys)
    assert rc == 0
    lines = [json.loads(l) for l in out.splitlines()]
    assert [e["type"] for e in lines] == ["session-spawned", "session-stopped"]


def test_no_events_file_is_not_an_error(tmp_path, capsys):
    mod = _load("fleet-events", tmp_path)
    rc, out, err = _run(mod, ["--json"], capsys)
    assert rc == 0
    assert out == ""


def test_follow_reads_backlog_then_new_appends(tmp_path):
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z", "session-spawned")])

    seen = []
    orig_stdout = sys.stdout
    sys.stdout = io.StringIO()
    stop = threading.Event()

    def run():
        # main() with --follow blocks forever; run it in a thread and just
        # inspect stdout after giving it time to catch up + pick up an append.
        try:
            mod.main(["--json", "--follow"])
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.3)
    with (tmp_path / "events.jsonl").open("a") as fh:
        fh.write(json.dumps(_rec("2026-08-23T21:05:00Z", "pr-opened")) + "\n")
    time.sleep(0.3 + mod._FOLLOW_POLL_SECONDS * 3)
    out = sys.stdout.getvalue()
    sys.stdout = orig_stdout

    lines = [json.loads(l) for l in out.splitlines() if l.strip()]
    assert [e["type"] for e in lines] == ["session-spawned", "pr-opened"]


def test_follow_survives_rotation(tmp_path):
    """A rotation renames events.jsonl to an archive mid-follow; the follower
    must keep draining the old fd, then pick up the new file at that path."""
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z", "session-spawned")])

    orig_stdout = sys.stdout
    sys.stdout = io.StringIO()

    def run():
        try:
            mod.main(["--json", "--follow"])
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.3)

    # simulate fleet-emit's rotation: rename events.jsonl, create a fresh one
    (tmp_path / "events.jsonl").rename(tmp_path / "events-20260101-000000.jsonl")
    with (tmp_path / "events.jsonl").open("a") as fh:
        fh.write(json.dumps(_rec("2026-08-23T21:10:00Z", "pr-merged")) + "\n")

    time.sleep(0.3 + mod._FOLLOW_POLL_SECONDS * 4)
    out = sys.stdout.getvalue()
    sys.stdout = orig_stdout

    lines = [json.loads(l) for l in out.splitlines() if l.strip()]
    assert [e["type"] for e in lines] == ["session-spawned", "pr-merged"]


def _readline_with_timeout(stream, timeout):
    import select
    rlist, _, _ = select.select([stream], [], [], timeout)
    if not rlist:
        raise AssertionError(f"no output within {timeout}s — did --follow stop flushing?")
    return stream.readline()


def test_follow_flushes_to_a_real_pipe(tmp_path):
    """Regression: print() without flush=True block-buffers when stdout is not
    a TTY (the common case — piped to a file or another process), so --follow
    output can sit invisible in the buffer indefinitely. The in-process tests
    above use an io.StringIO stand-in for stdout and would NOT catch this —
    only a real subprocess with real pipe buffering exercises it. Uses
    select()-with-timeout rather than a bare readline() so a regression here
    fails loudly instead of hanging the suite."""
    import os
    import subprocess
    fleet_dir = tmp_path
    (fleet_dir / "events.jsonl").write_text(
        json.dumps(_rec("2026-08-23T21:00:00Z", "session-spawned")) + "\n"
    )
    env = dict(os.environ)
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    proc = subprocess.Popen(
        [sys.executable, str(BIN / "fleet-events"), "--json", "--follow"],
        env=env, stdout=subprocess.PIPE, text=True,
    )
    try:
        first = _readline_with_timeout(proc.stdout, 5)
        assert json.loads(first)["type"] == "session-spawned"

        with (fleet_dir / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(_rec("2026-08-23T21:05:00Z", "pr-opened")) + "\n")
        second = _readline_with_timeout(proc.stdout, 5)
        assert json.loads(second)["type"] == "pr-opened"
    finally:
        proc.kill()
        proc.wait()


def test_eunomia_fleet_dir_env_var_is_honoured(tmp_path):
    import os
    import subprocess
    fleet_dir = tmp_path / "custom-fleet"
    fleet_dir.mkdir()
    (fleet_dir / "events.jsonl").write_text(
        json.dumps(_rec("2026-08-23T21:00:00Z", "session-spawned")) + "\n"
    )
    env = dict(os.environ)
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    result = subprocess.run(
        [sys.executable, str(BIN / "fleet-events"), "--json"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = [json.loads(l) for l in result.stdout.splitlines()]
    assert [e["type"] for e in lines] == ["session-spawned"]


# ---------------------------------------------------------------------------
# Integration tests: real `fleet-emit` + `fleet-events` subprocesses, real
# flock contention, real rotation. Everything above this line unit-tests
# fleet-events' own read/filter/format logic against hand-planted JSONL
# fixtures; these instead exercise the two CLIs together the way a session
# actually would, so a mismatch between what fleet-emit writes and what
# fleet-events expects to read can't hide behind a fixture that "helpfully"
# matches the reader's assumptions instead of the writer's real output.
# ---------------------------------------------------------------------------

FLEET_EMIT = BIN / "fleet-emit"
FLEET_EVENTS = BIN / "fleet-events"


def _fleet_env(fleet_dir, session="session-it"):
    env = dict(os.environ)
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    env["EUNOMIA_SESSION"] = session
    return env


def _cli(argv0, args, env):
    return subprocess.run(
        [sys.executable, str(argv0), *args], env=env, capture_output=True, text=True
    )


def _read_events_json(fleet_dir, env, extra_args=()):
    r = _cli(FLEET_EVENTS, ["--json", *extra_args], env)
    assert r.returncode == 0, r.stderr
    return [json.loads(l) for l in r.stdout.splitlines() if l]


def test_append_and_read_round_trip(tmp_path):
    """fleet-emit appends, fleet-events reads it back — the actual contract
    between the two CLIs, not a fixture standing in for fleet-emit's output."""
    fleet_dir = tmp_path / "fleet"
    env = _fleet_env(fleet_dir)

    r1 = _cli(FLEET_EMIT, ["pr-opened", "--repo", "operator/sniff", "--pr", "41",
                           "--sha", "a1b2c3d"], env)
    assert r1.returncode == 0, r1.stderr
    r2 = _cli(FLEET_EMIT, ["lease-activated", "--lease", "branch--w1--001"], env)
    assert r2.returncode == 0, r2.stderr

    events = _read_events_json(fleet_dir, env)
    assert [e["type"] for e in events] == ["pr-opened", "lease-activated"]
    assert events[0]["repo"] == "operator/sniff"
    assert events[0]["pr"] == 41
    assert events[0]["sha"] == "a1b2c3d"
    assert events[1]["lease"] == "branch--w1--001"
    # what fleet-emit printed on success matches what landed in the log
    assert json.loads(r1.stdout) == events[0]


def test_unknown_type_rejected_and_never_reaches_the_log(tmp_path):
    """fleet-emit refuses an out-of-set type non-zero (v0.2 closed set); the
    log must end up with zero trace of the rejected attempt."""
    fleet_dir = tmp_path / "fleet"
    env = _fleet_env(fleet_dir)

    r = _cli(FLEET_EMIT, ["not-a-real-type"], env)
    assert r.returncode != 0
    assert "unknown event type" in r.stderr

    # a real event around the rejected one, to prove the log is otherwise fine
    _cli(FLEET_EMIT, ["session-spawned"], env)

    events = _read_events_json(fleet_dir, env)
    assert [e["type"] for e in events] == ["session-spawned"]
    assert not (fleet_dir / "events.jsonl").exists() or "not-a-real-type" not in (
        fleet_dir / "events.jsonl"
    ).read_text()


def test_concurrent_appends_under_contention_lose_no_lines(tmp_path):
    """N real fleet-emit subprocesses racing the same flock; fleet-events must
    read back exactly N well-formed events, none lost, none duplicated."""
    fleet_dir = tmp_path / "fleet"
    env = _fleet_env(fleet_dir)
    n = 40

    def one(i):
        return _cli(FLEET_EMIT, ["budget-checkpoint", "--detail-json", json.dumps({"i": i})], env)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(one, range(n)))
    for r in results:
        assert r.returncode == 0, r.stderr

    events = _read_events_json(fleet_dir, env)
    assert len(events) == n
    seen = {e["detail"]["i"] for e in events}
    assert seen == set(range(n)), "every writer's event must survive exactly once"
    assert all(e["type"] == "budget-checkpoint" for e in events)


def test_rotation_preserves_every_event_and_since_finds_pre_rotation_ones(tmp_path):
    """Real rotation (crossing the real 10 MB threshold via fleet-emit's own
    _rotate_if_needed), not a simulated one. Verifies: (a) fleet-events' live
    default sees only the post-rotation event, (b) --since reaching back before
    the rotation finds every pre-rotation event too, none lost across the
    rename."""
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir()
    env = _fleet_env(fleet_dir)

    # Pre-seed real, valid event records (not raw padding) so post-rotation
    # reads can confirm each individual pre-rotation event survived, not just
    # that some bytes moved to an archive. Padded via `detail` to reach the
    # 10 MB rotation threshold without ~50k real subprocess calls.
    seed_n = 50
    target_size = 10 * 1024 * 1024 + 100_000  # comfortably past the threshold
    overhead = len(json.dumps({
        "ts": "2026-01-01T00:00:00Z", "actor": "session-seed", "type": "session-spawned",
        "repo": None, "pr": None, "sha": None, "lease": None,
        "detail": {"seed_i": seed_n - 1, "pad": ""},
    })) + 1
    filler = "x" * (-(-target_size // seed_n) - overhead)  # ceil-divide, then subtract overhead
    events_path = fleet_dir / "events.jsonl"
    with events_path.open("w") as fh:
        for i in range(seed_n):
            rec = {
                "ts": f"2026-01-01T00:{i:02d}:00Z", "actor": "session-seed",
                "type": "session-spawned", "repo": None, "pr": None, "sha": None,
                "lease": None, "detail": {"seed_i": i, "pad": filler},
            }
            fh.write(json.dumps(rec) + "\n")
    assert events_path.stat().st_size >= 10 * 1024 * 1024

    r = _cli(FLEET_EMIT, ["session-stopped", "--detail-json", json.dumps({"post": True})], env)
    assert r.returncode == 0, r.stderr

    archives = list(fleet_dir.glob("events-*.jsonl"))
    assert len(archives) == 1, "the seed must have been rotated out, not appended past 10MB"

    # (a) default (live-only) scope: just the post-rotation event
    live_only = _read_events_json(fleet_dir, env)
    assert len(live_only) == 1
    assert live_only[0]["detail"] == {"post": True}

    # (b) --since reaching back before the seed's earliest ts: everything
    all_events = _read_events_json(fleet_dir, env, extra_args=["--since", "2020-01-01"])
    assert len(all_events) == seed_n + 1
    seed_indices = {e["detail"]["seed_i"] for e in all_events if "seed_i" in e["detail"]}
    assert seed_indices == set(range(seed_n)), "every pre-rotation event must still be found"
    assert sum(1 for e in all_events if e["detail"].get("post")) == 1
    # chronological: seeded events (all before the rotation) precede the new one
    assert all_events[-1]["detail"] == {"post": True}


def test_since_rejects_garbage_loudly(tmp_path, monkeypatch):
    """Grafted from the losing bakeoff arm (its judge called this its one clear
    win): the first version compared --since as a raw string, so
    `--since yesterday` silently returned zero events — a swallowed failure."""
    mod = _load("fleet-events", tmp_path)
    import pytest
    for bad in ("yesterday", "2026-8-3", "3 days", "30", "m30", ""):
        with pytest.raises(SystemExit) as e:
            mod._parse_since(bad)
        assert "invalid --since" in str(e.value), bad


def test_since_accepts_the_three_documented_forms(tmp_path):
    mod = _load("fleet-events", tmp_path)
    assert mod._parse_since("2026-08-23") == "2026-08-23T00:00:00Z"
    assert mod._parse_since("2026-08-23T21:07:40Z") == "2026-08-23T21:07:40Z"
    assert mod._parse_since("2026-08-23 21:07:40") == "2026-08-23T21:07:40Z"
    out = mod._parse_since("30m")
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", out)


def test_since_rejects_impossible_dates(tmp_path):
    """Review 1489 F5: 2026-08-32 is shape-valid; string comparison silently
    matches nothing — the swallowed-failure class the grammar exists to close."""
    mod = _load("fleet-events", tmp_path)
    import pytest
    for bad in ("2026-08-32", "2026-13-01", "2026-02-30T10:00:00Z"):
        with pytest.raises(SystemExit):
            mod._parse_since(bad)


def test_follow_never_emits_a_partial_line(tmp_path, capsys):
    """Review 1489 F2: readline() on a file mid-append returns a partial line;
    emitting it prints a torn record and loses its tail. The reader must rewind
    and wait for the newline."""
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [_rec("2026-08-23T21:00:00Z", "session-spawned")])
    with open(tmp_path / "events.jsonl", "a") as fh:
        fh.write('{"ts":"2026-08-23T22:00:00Z","actor":"x","type":"sess')  # no \n yet
    rc, out, err = _run(mod, ["--json"], capsys)
    lines = [l for l in out.splitlines() if l]
    assert len(lines) == 1, "the partial line must not be emitted"
    assert json.loads(lines[0])["type"] == "session-spawned"


def _spawn_follow(repo_bin, fleet_dir):
    import subprocess, sys as _sys, os as _os
    env = dict(_os.environ, EUNOMIA_FLEET_DIR=str(fleet_dir), EUNOMIA_SESSION="t")
    p = subprocess.Popen([_sys.executable, str(repo_bin / "fleet-events"), "--follow", "--json"],
                         env=env, stdout=subprocess.PIPE, text=True)
    _os.set_blocking(p.stdout.fileno(), False)
    return p


def _drain_until(proc, needle, deadline_s):
    import time as _t
    got, deadline = [], _t.time() + deadline_s
    while _t.time() < deadline:
        line = proc.stdout.readline()
        if line:
            got.append(line.strip())
            if needle in line:
                return got, True
        else:
            _t.sleep(0.1)
    return got, False


def test_follow_survives_torn_tail_plus_rotation(tmp_path):
    """Review 1490 M1, from the reviewer's own probe: a torn tail archived by a
    rotation used to park the follower in the partial-line branch forever — it
    seeked back and slept without ever reaching the inode check. The branch now
    falls through to the rotation check, and fleet-emit repairs the tail BEFORE
    rotating so archives are never torn in the first place."""
    import json as _json, os as _os
    rec1 = {"ts": "2026-08-27T00:00:00Z", "actor": "a", "type": "session-spawned",
            "repo": None, "pr": None, "sha": None, "lease": None, "detail": {}}
    (tmp_path / "events.jsonl").write_text(
        _json.dumps(rec1, separators=(",", ":")) + "\n"
        + '{"ts":"2026-08-27T00:00:30Z","actor":"crash","type":"sess')  # torn
    proc = _spawn_follow(BIN, tmp_path)
    try:
        _drain_until(proc, "session-spawned", 5)
        # rotate with the tail still torn (simulating the pre-repair sequence a
        # crashed writer could still leave via manual archive manipulation)
        _os.rename(tmp_path / "events.jsonl", tmp_path / "events-20260827-000100.jsonl")
        mod = _load("fleet-events", tmp_path)  # noqa: F841 (env parity)
        import subprocess, sys as _sys
        env = dict(_os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="t")
        r = subprocess.run([_sys.executable, str(BIN / "fleet-emit"), "session-stopped"],
                           env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        got, found = _drain_until(proc, "session-stopped", 6)
        assert found, f"follower stalled after torn-tail rotation; emitted only {got}"
    finally:
        proc.terminate(); proc.wait(timeout=5)


def test_follow_delivers_a_line_completed_after_partial_read(tmp_path):
    """Review 1490 m2: the first regression test for the partial-line fix passed
    against the pre-fix code (the fragment was unparseable JSON the old code
    skipped anyway, and it never used --follow). This one discriminates: pre-fix,
    readline() consumes the fragment and the completed event's tail is lost;
    post-fix the completed event arrives intact."""
    import json as _json
    rec1 = {"ts": "2026-08-27T00:00:00Z", "actor": "a", "type": "session-spawned",
            "repo": None, "pr": None, "sha": None, "lease": None, "detail": {}}
    full = {"ts": "2026-08-27T00:01:00Z", "actor": "b", "type": "session-stopped",
            "repo": None, "pr": None, "sha": None, "lease": None, "detail": {}}
    line2 = _json.dumps(full, separators=(",", ":")) + "\n"
    (tmp_path / "events.jsonl").write_text(
        _json.dumps(rec1, separators=(",", ":")) + "\n" + line2[:25])  # partial
    proc = _spawn_follow(BIN, tmp_path)
    try:
        _drain_until(proc, "session-spawned", 5)
        with open(tmp_path / "events.jsonl", "a") as fh:
            fh.write(line2[25:])            # the writer finishes the line
        got, found = _drain_until(proc, "session-stopped", 6)
        assert found, f"completed line never delivered; got {got}"
        ev = [_json.loads(l) for l in got if "session-stopped" in l][0]
        assert ev["actor"] == "b" and ev["ts"] == "2026-08-27T00:01:00Z", \
            "the event must arrive INTACT, not torn"
    finally:
        proc.terminate(); proc.wait(timeout=5)


def test_capability_published_filters_by_type_and_window(tmp_path, capsys):
    """Plan 0025's consumer side: the read a planning session is told to run is
    `--type capability-published --since <window>`, so both filters have to
    compose. One event either side of the boundary, because a window test that
    only places events inside it passes for a filter that ignores --since."""
    mod = _load("fleet-events", tmp_path)
    _write_events(tmp_path, [
        _rec("2026-09-07T09:00:00Z", "capability-published",
             detail={"kind": "skill", "name": "old-one", "where": "operator/techne"}),
        _rec("2026-09-07T11:00:00Z", "session-spawned"),
        _rec("2026-09-07T11:30:00Z", "capability-published",
             detail={"kind": "skill", "name": "review-a-pr", "where": "operator/techne"}),
    ])
    rc, out, err = _run(mod, ["--type", "capability-published",
                              "--since", "2026-09-07T10:00:00Z", "--json"], capsys)
    assert rc == 0, err
    names = [json.loads(l)["detail"]["name"] for l in out.splitlines() if l.strip()]
    assert names == ["review-a-pr"], f"window or type filter wrong: {names}"
