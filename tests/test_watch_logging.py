"""Tests for the resident watcher's log rotation (plan 0187).

`rotate_watch_log` is exercised directly, with a fake clock (its own `now`
parameter) and real temporary files under `EUNOMIA_FLEET_DIR` -- never the
real `~/dev/.fleet`. `_reopen_active_log` is monkeypatched in every test
EXCEPT the dedicated descriptor-ownership test below: it calls `os.dup2` on
this PROCESS's real fd 1/2, which would corrupt pytest's own output
capturing if it ran inside the test runner itself. The one test that needs
the real thing runs it in a CHILD subprocess instead, where that is safe.
"""
import datetime as dt
import gzip
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_watch import _load  # noqa: E402

UTC = dt.timezone.utc


def _ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def mod(tmp_path):
    m = _load(tmp_path / "fleet")
    return m


def _fake_reopen(now_holder):
    """Stands in for `_reopen_active_log` without touching a real fd: writes
    the SAME marker line the real function's caller (`rotate_watch_log`,
    via `_log`) would end up as the new file's first line, through the real
    dup2'd descriptor in production -- so `_file_start_date` reads the next
    rotation's start exactly the way it would live, using the fake clock in
    `now_holder` rather than the wall clock."""
    def fn(path):
        path.write_text(f"{_ts(now_holder['now'])} fleet-watch: "
                         "watch.log rotated\n")
    return fn


def _active_with_first_line(mod, ts_line_date=None, body="hello\n"):
    path = mod._watch_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if ts_line_date is not None:
        path.write_text(f"{_ts(ts_line_date)} fleet-watch: start\n" + body)
    else:
        path.write_text(body)
    return path


# --------------------------------------------------------- _file_start_date

def test_first_line_timestamp_wins_over_birth_time(mod):
    old = dt.datetime(2026, 1, 1, tzinfo=UTC)
    path = _active_with_first_line(mod, ts_line_date=old)
    assert mod._file_start_date(path) == dt.date(2026, 1, 1)


def test_no_timestamp_falls_back_to_birth_time(mod):
    """The shape today's pre-existing, timestamp-free watch.log needs for
    its first rotation under this plan."""
    path = _active_with_first_line(mod, ts_line_date=None,
                                   body="plain, no timestamp\n")
    got = mod._file_start_date(path)
    assert got == dt.datetime.now(UTC).date()


def test_unreadable_file_reports_none(mod):
    assert mod._file_start_date(mod._watch_log_path()) is None


# ------------------------------------------------------------- due decision

def test_not_due_before_seven_days(mod, monkeypatch):
    monkeypatch.setattr(mod, "_reopen_active_log", lambda p: p.touch())
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    path = _active_with_first_line(mod, ts_line_date=start)
    before = start.date()
    name = mod.rotate_watch_log(now=start + dt.timedelta(days=6))
    assert name is None
    assert path.exists()
    assert path.read_text().startswith(_ts(start))


def test_due_at_seven_days_rotates_and_archives(mod, monkeypatch):
    now_holder = {"now": None}
    monkeypatch.setattr(mod, "_reopen_active_log", _fake_reopen(now_holder))
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    path = _active_with_first_line(mod, ts_line_date=start, body="line1\nline2\n")
    original = path.read_bytes()
    now = start + dt.timedelta(days=7)
    now_holder["now"] = now
    name = mod.rotate_watch_log(now=now)
    assert name == "watch-20260101-20260108"
    archive = mod._watch_archive_dir() / f"{name}.log.gz"
    assert archive.exists()
    assert gzip.decompress(archive.read_bytes()) == original
    # the uncompressed intermediate is gone once the archive is published
    assert not (mod._watch_logs_dir() / f"{name}.log").exists()
    # a FRESH active file exists -- the writer reopened onto the active path
    assert path.exists()
    assert path.read_text() != ""


def test_rotation_crossing_utc_midnight(mod, monkeypatch):
    now_holder = {"now": None}
    monkeypatch.setattr(mod, "_reopen_active_log", _fake_reopen(now_holder))
    start = dt.datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    _active_with_first_line(mod, ts_line_date=start)
    now = dt.datetime(2026, 1, 9, 0, 5, tzinfo=UTC)   # 7d 0h15m later, past midnight
    now_holder["now"] = now
    name = mod.rotate_watch_log(now=now)
    assert name == "watch-20260101-20260109"


def test_second_rotation_same_dates_gets_dash_2(mod, monkeypatch):
    now_holder = {"now": None}
    monkeypatch.setattr(mod, "_reopen_active_log", _fake_reopen(now_holder))
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    _active_with_first_line(mod, ts_line_date=start)
    archive_dir = mod._watch_archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "watch-20260101-20260108.log.gz").write_bytes(
        gzip.compress(b"someone else's archive\n"))
    now = start + dt.timedelta(days=7)
    now_holder["now"] = now
    name = mod.rotate_watch_log(now=now)
    assert name == "watch-20260101-20260108-2"
    # the collision target is untouched
    assert gzip.decompress(
        (archive_dir / "watch-20260101-20260108.log.gz").read_bytes()
    ) == b"someone else's archive\n"


def test_three_consecutive_rotations_produce_three_distinct_archives(mod, monkeypatch):
    now_holder = {"now": None}
    monkeypatch.setattr(mod, "_reopen_active_log", _fake_reopen(now_holder))
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    _active_with_first_line(mod, ts_line_date=start)
    names = []
    now = start
    for _ in range(3):
        now = now + dt.timedelta(days=7)
        now_holder["now"] = now
        names.append(mod.rotate_watch_log(now=now))
    assert len(set(names)) == 3, names
    archive_dir = mod._watch_archive_dir()
    for name in names:
        assert (archive_dir / f"{name}.log.gz").exists()
    # none pruned -- all three still present together
    assert len(list(archive_dir.glob("watch-*.log.gz"))) == 3


# ------------------------------------------------------- atomic publish/gzip

def test_compression_failure_keeps_uncompressed_and_warns(mod, monkeypatch):
    now_holder = {"now": None}
    monkeypatch.setattr(mod, "_reopen_active_log", _fake_reopen(now_holder))
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    _active_with_first_line(mod, ts_line_date=start, body="payload\n")
    now = start + dt.timedelta(days=7)
    now_holder["now"] = now

    def boom(_data):
        raise OSError("disk full")
    monkeypatch.setattr(gzip, "compress", boom)
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    name = mod.rotate_watch_log(now=now)
    assert name == "watch-20260101-20260108"
    uncompressed = mod._watch_logs_dir() / f"{name}.log"
    assert uncompressed.exists()
    assert uncompressed.read_text() == f"{_ts(start)} fleet-watch: start\npayload\n"
    assert not (mod._watch_archive_dir() / f"{name}.log.gz").exists()
    assert any("gzip" in w and "disk full" not in w for w in warned), warned


def test_gzip_test_failure_does_not_publish(mod, monkeypatch):
    now_holder = {"now": None}
    monkeypatch.setattr(mod, "_reopen_active_log", _fake_reopen(now_holder))
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    _active_with_first_line(mod, ts_line_date=start, body="payload\n")
    now = start + dt.timedelta(days=7)
    now_holder["now"] = now

    real_run = subprocess.run
    def fake_run(args, **kw):
        if args[:2] == ["gzip", "-t"]:
            return subprocess.CompletedProcess(args, 1)
        return real_run(args, **kw)
    monkeypatch.setattr(subprocess, "run", fake_run)
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    name = mod.rotate_watch_log(now=now)
    uncompressed = mod._watch_logs_dir() / f"{name}.log"
    final = mod._watch_archive_dir() / f"{name}.log.gz"
    assert uncompressed.exists(), "a failed gzip -t must keep the uncompressed copy"
    assert not final.exists(), "a failed gzip -t must never be published"
    assert any("gzip -t" in w for w in warned), warned


def test_killed_gzip_recovers_clean_on_the_next_attempt(mod, monkeypatch):
    """A gzip killed mid-write (simulated here as a failed `gzip -t`, the
    same shape a torn write produces) must never publish a corrupt archive
    -- and the NEXT rotation attempt, with nothing else wrong, must finish
    the job rather than stay stuck."""
    now_holder = {"now": None}
    monkeypatch.setattr(mod, "_reopen_active_log", _fake_reopen(now_holder))
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    _active_with_first_line(mod, ts_line_date=start, body="payload\n")
    now = start + dt.timedelta(days=7)
    now_holder["now"] = now

    real_run = subprocess.run
    killed = {"on": True}
    def fake_run(args, **kw):
        if args[:2] == ["gzip", "-t"] and killed["on"]:
            return subprocess.CompletedProcess(args, 1)
        return real_run(args, **kw)
    monkeypatch.setattr(subprocess, "run", fake_run)
    name = mod.rotate_watch_log(now=now)
    final = mod._watch_archive_dir() / f"{name}.log.gz"
    assert not final.exists(), "a killed gzip must never publish under the final name"
    assert not list(mod._watch_archive_dir().glob("watch-*.log.gz")), (
        "no watch-*.log.gz may exist until one PASSES gzip -t")
    # the failed attempt's own .partial is exactly the leftover the plan
    # says is cleaned up on the NEXT attempt, not deleted the instant the
    # failure is detected.
    assert (mod._watch_archive_dir() / f".{name}.log.gz.partial").exists()

    killed["on"] = False
    recovered = mod._recover_stuck_gzips()
    assert recovered == [name]
    assert final.exists()
    assert gzip.decompress(final.read_bytes()) == (
        f"{_ts(start)} fleet-watch: start\npayload\n".encode())


def test_no_watch_gz_is_ever_partial(mod, monkeypatch):
    """The publish name (`watch-*.log.gz`) and the in-progress name
    (`.watch-*.log.gz.partial`) are disjoint under the SAME glob a backup's
    restore probe would use -- `watch-*.log.gz` can never match a partial."""
    now_holder = {"now": None}
    monkeypatch.setattr(mod, "_reopen_active_log", _fake_reopen(now_holder))
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    _active_with_first_line(mod, ts_line_date=start, body="x\n" * 1000)
    now = start + dt.timedelta(days=7)
    now_holder["now"] = now
    name = mod.rotate_watch_log(now=now)
    archive_dir = mod._watch_archive_dir()
    matches = list(archive_dir.glob("watch-*.log.gz"))
    assert [m.name for m in matches] == [f"{name}.log.gz"]
    assert not list(archive_dir.glob(".watch-*.log.gz.partial"))


# -------------------------------------------------------------- recovery

def test_leftover_partial_is_removed_and_gzip_redone(mod):
    """A `.partial` left by a crashed gzip (the rename-out happened; the
    compress-and-publish did not finish) is cleaned up and redone on the
    next rotation attempt -- never left as a stale, half-written file, and
    never mistaken for a completed archive."""
    logs_dir, archive_dir = mod._watch_logs_dir(), mod._watch_archive_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = "watch-20260101-20260108"
    (logs_dir / f"{name}.log").write_text("the real content\n")
    (archive_dir / f".{name}.log.gz.partial").write_bytes(b"garbage, half-written")
    recovered = mod._recover_stuck_gzips()
    assert recovered == [name]
    assert not (archive_dir / f".{name}.log.gz.partial").exists()
    assert not (logs_dir / f"{name}.log").exists()
    final = archive_dir / f"{name}.log.gz"
    assert final.exists()
    assert gzip.decompress(final.read_bytes()) == b"the real content\n"


def test_recovery_of_an_already_finished_gzip_just_removes_the_stray_copy(mod):
    """A crash between the final rename and the uncompressed delete (both in
    `_compress_rotated`) leaves a COMPLETE archive and a stray uncompressed
    copy beside it -- recovery must not re-gzip (the archive is already
    good) and must only remove the stray."""
    logs_dir, archive_dir = mod._watch_logs_dir(), mod._watch_archive_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = "watch-20260101-20260108"
    (logs_dir / f"{name}.log").write_text("stray\n")
    (archive_dir / f"{name}.log.gz").write_bytes(gzip.compress(b"already published\n"))
    recovered = mod._recover_stuck_gzips()
    assert recovered == [name]
    assert not (logs_dir / f"{name}.log").exists()
    assert gzip.decompress(
        (archive_dir / f"{name}.log.gz").read_bytes()) == b"already published\n"


def test_dry_run_recovery_touches_nothing(mod):
    logs_dir, archive_dir = mod._watch_logs_dir(), mod._watch_archive_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = "watch-20260101-20260108"
    (logs_dir / f"{name}.log").write_text("the real content\n")
    (archive_dir / f".{name}.log.gz.partial").write_bytes(b"garbage")
    recovered = mod._recover_stuck_gzips(dry_run=True)
    assert recovered == [name]
    assert (logs_dir / f"{name}.log").exists()
    assert (archive_dir / f".{name}.log.gz.partial").exists()
    assert not (archive_dir / f"{name}.log.gz").exists()


def test_dry_run_never_rotates(mod, monkeypatch):
    reopened = []
    monkeypatch.setattr(mod, "_reopen_active_log", lambda p: reopened.append(p))
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    path = _active_with_first_line(mod, ts_line_date=start)
    now = start + dt.timedelta(days=7)
    name = mod.rotate_watch_log(now=now, dry_run=True)
    assert name == "watch-20260101-20260108"
    assert reopened == []
    assert path.exists()
    assert path.read_text().startswith(_ts(start))
    assert not (mod._watch_archive_dir() / f"{name}.log.gz").exists()


def test_no_active_file_is_a_no_op(mod):
    assert mod.rotate_watch_log(now=dt.datetime.now(UTC)) is None


# ------------------------------------------ real descriptor reopening (child)

_CHILD_SCRIPT = textwrap.dedent("""
    import datetime as dt
    import importlib.machinery
    import importlib.util
    import os
    import sys
    from pathlib import Path

    fleet_dir = Path(sys.argv[1])
    bin_dir = Path(sys.argv[2])
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["EUNOMIA_SESSION"] = "watch-test"
    os.environ["FLEET_SPAWN"] = "popen"
    loader = importlib.machinery.SourceFileLoader(
        "fleet_watch", str(bin_dir / "fleet-watch"))
    spec = importlib.util.spec_from_loader("fleet_watch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    active = mod._watch_log_path()
    active.parent.mkdir(parents=True, exist_ok=True)
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    active.write_text(f"{start:%Y-%m-%dT%H:%M:%SZ} fleet-watch: start\\n")

    # Reopen fd 1/2 onto the SAME path fleet-watch's own launchd unit would
    # have handed this process via StandardOutPath/StandardErrorPath --
    # i.e. simulate what launchd does at exec, since this child was not
    # actually started under launchd.
    fd = os.open(str(active), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)

    print("before-rotation line")
    now = start + dt.timedelta(days=7)
    mod.rotate_watch_log(now=now)
    print("after-rotation line")
""")


def test_live_writer_reopens_onto_the_new_inode_after_rotation(tmp_path):
    """The real `_reopen_active_log`, exercised through `os.dup2` on an
    ACTUAL fd 1/2 -- done in a child process, never in-process, because the
    same trick run on the pytest process's own fds would corrupt its output
    capturing.

    Demonstrates the descriptor-ownership requirement directly: a line
    printed before rotation lands in the archived file; a line printed
    after lands in the fresh active file, not the (by then compressed)
    old one -- which is exactly what would NOT be true if the reopen were
    missing, since the inherited fd would keep pointing at the renamed,
    soon-to-be-archived inode."""
    fleet_dir = tmp_path / "fleet"
    bin_dir = Path(__file__).resolve().parent.parent / "bin"
    r = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(fleet_dir), str(bin_dir)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr

    active = fleet_dir / "watch.log"
    assert active.exists()
    new_content = active.read_text()
    assert "after-rotation line" in new_content
    assert "before-rotation line" not in new_content, (
        "a line printed before rotation leaked into the NEW active file -- "
        "the old fd was still being written through")

    archives = list((fleet_dir / "logs" / "archive").glob("watch-*.log.gz"))
    assert len(archives) == 1
    import gzip as _gzip
    old_content = _gzip.decompress(archives[0].read_bytes()).decode()
    assert "before-rotation line" in old_content
    assert "after-rotation line" not in old_content, (
        "a line printed after rotation leaked into the OLD archived file")
