"""Per-repo model configuration survives the loss of `fleet.db` (plan 0063).

`repo_model` was the one piece of fleet state with no second copy. These tests
pin the pieces that give it one, and each is written to FAIL against the failure
it guards — a durability test that passes on a broken implementation is the
failure this fleet keeps re-learning:

  D1  every write appends a `repo-model-changed` event, beside the DB written
  D2  `reconstruct_repo_model` restores into an absent table and ONLY into one
  D3  the journal mode is WAL, set at open, and a reader is not blocked
  D4  `fleet-db-snapshot` is consistent under a concurrent writer, and rotates
  D5  the launchd unit parses
  D6  the docs name the path

D2's call from `bin/fleet-collect` was wired by plan 0076, which also carries the
rest of what 0063's first pull request left: WAL at every open in `fleet-collect`
and `fleet-reviews`, the logical dump beside each snapshot, and the two named
table sets with their completeness test (the last section of this file). The
reconstruction is exercised against the `event` table `fleet-collect` itself
builds, ingested by its own `main()`.
"""
import importlib.machinery
import importlib.util
import json
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def _load(name):
    mod_name = name.replace("-", "_") + "_cd"
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """An isolated fleet dir: DB, ledger and snapshots all under tmp_path, and
    the real ~/dev/.fleet unreachable by construction."""
    fdir = tmp_path / "fleet"
    fdir.mkdir()
    monkeypatch.setenv("FLEET_DB", str(fdir / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(fdir))
    return fdir


def _events(fdir):
    p = fdir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _model_events(fdir):
    return [e for e in _events(fdir) if e["type"] == "repo-model-changed"]


def _rows(con):
    return con.execute(
        "SELECT repo, position, ordinal, runner, disabled FROM repo_model "
        "ORDER BY repo, position, ordinal").fetchall()


def _configure(fm, con):
    """A history with every event shape in it: a create, a re-set, a list
    position that grows, a disable, an enable, and a singleton that ends disabled."""
    fm.set_runner(con, "default", "implementer", "sub-sonnet", actor="cli:t")
    fm.set_runner(con, "default", "tester", "sub-opus", actor="cli:t")
    fm.set_runner(con, "operator/eunomia", "implementer", "sub-opus", actor="cli:t")
    fm.set_runner(con, "operator/eunomia", "implementer", "local-qwen", actor="cli:t")
    fm.set_runner(con, "operator/eunomia", "tester", "sub-opus", actor="cli:t")
    fm.set_runner(con, "operator/eunomia", "tester", "api-sonnet", actor="cli:t")
    fm.set_runner(con, "operator/eunomia", "tester", "local-qwen", actor="cli:t")
    fm.disable(con, "operator/eunomia", "tester", 2, actor="cli:t")
    fm.set_runner(con, "operator/lynceus", "mediator", "sub-opus", actor="cli:t")
    fm.disable(con, "operator/lynceus", "mediator", 1, actor="cli:t")
    fm.disable(con, "operator/eunomia", "tester", 3, actor="cli:t")
    fm.enable(con, "operator/eunomia", "tester", 3, actor="cli:t")


def _collect_events(fdir, monkeypatch):
    """Run the REAL collector over `fdir`, so the `event` table is the one
    fleet-collect builds (its scanner, its redaction), not a hand-made copy."""
    collect = _load("fleet-collect")
    monkeypatch.setattr(collect, "DB", fdir / "fleet.db")
    monkeypatch.setattr(collect, "PROJECTS", fdir / "no-projects")
    monkeypatch.setattr(collect, "FLEET_DIR", fdir)
    monkeypatch.setattr(collect, "WORK_ROOT", fdir / "no-work-root")
    collect.main()


# ---------------------------------------------------------------- D1: event


def test_set_writes_a_change_row_and_a_ledger_event(fleet):
    fm = _load("fleet-models")
    con = fm.connect()
    fm.set_runner(con, "operator/eunomia", "implementer", "sub-opus",
                  actor="cli:operator", source="fleet-models")
    assert con.execute("SELECT COUNT(*) FROM repo_model_change").fetchone()[0] == 1
    (change_id,) = con.execute("SELECT id FROM repo_model_change").fetchone()

    evs = _model_events(fleet)
    assert len(evs) == 1
    e = evs[0]
    assert (e["actor"], e["repo"], e["lease"], e["pr"]) == ("cli:operator", "operator/eunomia", None, None)
    assert e["detail"] == {
        "change_id": change_id, "source": "fleet-models", "position": "implementer",
        "ordinal": 1, "old_runner": None, "new_runner": "sub-opus",
        "old_disabled": None, "new_disabled": 0}


def test_every_kind_of_write_is_one_change_and_one_event(fleet):
    fm = _load("fleet-models")
    con = fm.connect()
    fm.set_runner(con, "r/a", "implementer", "sub-opus", actor="cli:t")
    fm.set_runner(con, "r/a", "implementer", "sub-opus", actor="cli:t")   # no-op: nothing
    fm.set_runner(con, "r/a", "implementer", "sub-sonnet", actor="cli:t")
    fm.disable(con, "r/a", "implementer", 1, actor="cli:t")
    fm.disable(con, "r/a", "implementer", 1, actor="cli:t")               # no-op: nothing
    fm.enable(con, "r/a", "implementer", 1, actor="cli:t")
    n_changes = con.execute("SELECT COUNT(*) FROM repo_model_change").fetchone()[0]
    assert n_changes == 4
    assert len(_model_events(fleet)) == n_changes
    ids = [e["detail"]["change_id"] for e in _model_events(fleet)]
    assert ids == [r[0] for r in con.execute("SELECT id FROM repo_model_change ORDER BY ts, rowid")]


def test_the_event_carries_only_the_pinned_keys(fleet):
    """The schema, pinned. `events.jsonl` is read by `fleet-events`, which does
    not redact, so a credential must never ride this event. Growing this set is
    not a formatting change: it fails here so the next author reads why."""
    fm = _load("fleet-models")
    assert set(fm.EVENT_DETAIL_KEYS) == {
        "change_id", "source", "position", "ordinal",
        "old_runner", "new_runner", "old_disabled", "new_disabled"}
    con = fm.connect()
    fm.set_runner(con, "r/a", "tester", "sub-opus", actor="cli:t")
    fm.disable(con, "r/a", "tester", 1, actor="cli:t")
    for e in _model_events(fleet):
        assert set(e["detail"]) == set(fm.EVENT_DETAIL_KEYS)


def test_an_isolated_database_never_appends_to_another_ledger(fleet, tmp_path, monkeypatch):
    """The ledger written is the one BESIDE the database. A test that points
    FLEET_DB at a tmp file must not reach the real fleet's ledger, even with
    EUNOMIA_FLEET_DIR aimed elsewhere."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(elsewhere))
    fm = _load("fleet-models")
    fm.set_runner(fm.connect(), "r/a", "implementer", "sub-opus", actor="cli:t")
    assert not (elsewhere / "events.jsonl").exists()
    assert len(_model_events(fleet)) == 1


def test_an_in_memory_database_emits_nothing(fleet):
    fm = _load("fleet-models")
    con = sqlite3.connect(":memory:")
    con.executescript(fm.SCHEMA)
    fm.set_runner(con, "r/a", "implementer", "sub-opus", actor="cli:t")
    assert _events(fleet) == []


def test_a_change_the_ledger_cannot_record_is_not_made(fleet):
    """Fail closed. If the append fails the write rolls back, so a retry is a
    clean retry — the alternative, write-then-lose-the-event, leaves a value in
    force that a retry would find already set and never emit."""
    fm = _load("fleet-models")
    con = fm.connect()
    (fleet / "events.jsonl").mkdir()          # appending to a directory fails
    with pytest.raises(fm.LedgerAppendFailed):
        fm.set_runner(con, "r/a", "implementer", "sub-opus", actor="cli:t")
    assert _rows(con) == []
    assert con.execute("SELECT COUNT(*) FROM repo_model_change").fetchone()[0] == 0

    (fleet / "events.jsonl").rmdir()
    ordinal, changed = fm.set_runner(con, "r/a", "implementer", "sub-opus", actor="cli:t")
    assert changed and len(_model_events(fleet)) == 1


def test_a_failed_disable_leaves_the_row_enabled(fleet):
    fm = _load("fleet-models")
    con = fm.connect()
    fm.set_runner(con, "r/a", "implementer", "sub-opus", actor="cli:t")
    (fleet / "events.jsonl").unlink()
    (fleet / "events.jsonl").mkdir()
    with pytest.raises(fm.LedgerAppendFailed):
        fm.disable(con, "r/a", "implementer", 1, actor="cli:t")
    assert _rows(con) == [("r/a", "implementer", 1, "sub-opus", 0)]


def test_the_cli_set_emits_the_event(fleet):
    r = subprocess.run(
        [sys.executable, str(BIN / "fleet-models"), "set", "operator/eunomia",
         "implementer", "sub-opus", "--actor", "cli:operator"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin",
                                             "FLEET_DB": str(fleet / "fleet.db")})
    assert r.returncode == 0, r.stderr
    evs = _model_events(fleet)
    assert [(e["actor"], e["repo"], e["detail"]["new_runner"]) for e in evs] == [
        ("cli:operator", "operator/eunomia", "sub-opus")]


def test_the_event_type_is_in_the_closed_set_everywhere():
    """tests/test_event_type_sync.py owns the three-way agreement; this names the
    one value this plan added, so a revert of any one copy reads clearly."""
    emit, events = _load("fleet-emit"), _load("fleet-events")
    assert "repo-model-changed" in emit.EVENT_TYPES
    assert events._parse_types(["repo-model-changed"]) == {"repo-model-changed"}
    assert "`repo-model-changed`" in (ROOT / "SPEC.md").read_text()


# ------------------------------------------------------- D2: reconstruction


def test_dropping_repo_model_and_reconstructing_restores_the_same_rows(fleet, monkeypatch):
    fm = _load("fleet-models")
    con = fm.connect()
    _configure(fm, con)
    before = _rows(con)
    assert len(before) >= 6 and any(r[4] == 1 for r in before)   # a disabled row rides along
    con.close()

    _collect_events(fleet, monkeypatch)
    con = sqlite3.connect(fleet / "fleet.db")
    con.execute("DROP TABLE repo_model")
    con.commit()

    report = fm.reconstruct_repo_model(con)
    assert report["action"] == "reconstructed"
    assert _rows(con) == before
    assert any("reconstructed" in line for line in fm.describe_reconstruction(report))


def test_reconstruction_also_restores_into_an_emptied_table(fleet, monkeypatch):
    """`connect()` creates the schema, so `fleet-models list` on a wiped database
    turns "absent" into "present and empty". That must not lock the restore out:
    an empty table has never carried a decision, since rows are never deleted."""
    fm = _load("fleet-models")
    con = fm.connect()
    _configure(fm, con)
    before = _rows(con)
    con.close()
    _collect_events(fleet, monkeypatch)
    con = sqlite3.connect(fleet / "fleet.db")
    con.execute("DELETE FROM repo_model")
    con.commit()
    assert fm.reconstruct_repo_model(con)["action"] == "reconstructed"
    assert _rows(con) == before


def test_a_surviving_table_is_never_changed_and_the_disagreement_is_reported(fleet, monkeypatch):
    fm = _load("fleet-models")
    con = fm.connect()
    _configure(fm, con)
    con.close()
    _collect_events(fleet, monkeypatch)

    con = sqlite3.connect(fleet / "fleet.db")
    # Three ways a surviving table can disagree with the ledger.
    con.execute("UPDATE repo_model SET runner='api-sonnet' WHERE repo='default' AND position='implementer'")
    con.execute("DELETE FROM repo_model WHERE repo='operator/lynceus'")
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner) "
                "VALUES ('operator/old', 'tester', 1, 'sub-opus')")     # pre-ledger write
    con.commit()
    before = _rows(con)

    report = fm.reconstruct_repo_model(con)
    assert report["action"] == "compared"
    assert _rows(con) == before                                  # nothing changed
    assert [d[0] for d in report["differs"]] == [("default", "implementer", 1)]
    assert [d[0] for d in report["ledger_only"]] == [("operator/lynceus", "mediator", 1)]
    assert report["table_only"] == [("operator/old", "tester", 1)]
    text = "\n".join(fm.describe_reconstruction(report))
    assert "ledger disagrees on default implementer#1" in text
    assert "table left unchanged" in text
    assert "operator/lynceus mediator#1" in text
    assert "1 row(s) have no ledger event" in text


def test_a_table_that_agrees_with_the_ledger_says_nothing(fleet, monkeypatch):
    fm = _load("fleet-models")
    con = fm.connect()
    _configure(fm, con)
    con.close()
    _collect_events(fleet, monkeypatch)
    con = sqlite3.connect(fleet / "fleet.db")
    report = fm.reconstruct_repo_model(con)
    assert report["action"] == "compared"
    assert not (report["differs"] or report["ledger_only"] or report["table_only"])
    assert fm.describe_reconstruction(report) == []


def test_no_events_means_no_table_is_created(fleet, monkeypatch):
    """A fleet that never adopted repo_model must still see NotConfigured — an
    empty table would turn "not adopted" into "adopted, and no default row",
    which makes a dispatch stop."""
    _collect_events(fleet, monkeypatch)
    fm = _load("fleet-models")
    con = sqlite3.connect(fleet / "fleet.db")
    report = fm.reconstruct_repo_model(con)
    assert report["action"] == "none"
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='repo_model'").fetchone()[0] == 0
    with pytest.raises(fm.NotConfigured):
        fm.resolve_repo("r/a", "implementer")


def test_an_incomplete_ledger_refuses_to_build_a_table(fleet, monkeypatch):
    fm = _load("fleet-models")
    con = fm.connect()
    _configure(fm, con)
    con.close()
    _collect_events(fleet, monkeypatch)
    con = sqlite3.connect(fleet / "fleet.db")
    con.execute("DROP TABLE repo_model")
    con.commit()
    report = fm.reconstruct_repo_model(con, ledger_complete=False)
    assert report["action"] == "refused"
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='repo_model'").fetchone()[0] == 0
    assert "NOT reconstructed" in "\n".join(fm.describe_reconstruction(report))


def test_a_slot_that_predates_the_ledger_is_recovered_from_a_disable_event(fleet, monkeypatch):
    """A row written before the event type existed is later disabled: the
    disable event carries `old_runner`, which is enough to restore it."""
    fm = _load("fleet-models")
    con = fm.connect()
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner) "
                "VALUES ('operator/old', 'tester', 1, 'sub-opus')")   # no event
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner) "
                "VALUES ('operator/old2', 'tester', 1, 'sub-opus')")  # no event
    con.commit()
    fm.disable(con, "operator/old", "tester", 1, actor="cli:t")
    fm.set_runner(con, "operator/old2", "tester", "local-qwen", ordinal=1, actor="cli:t")
    con.close()
    _collect_events(fleet, monkeypatch)
    con = sqlite3.connect(fleet / "fleet.db")
    con.execute("DROP TABLE repo_model")
    con.commit()
    report = fm.reconstruct_repo_model(con)
    assert _rows(con) == [("operator/old", "tester", 1, "sub-opus", 1),
                          ("operator/old2", "tester", 1, "local-qwen", 0)]
    # only the slot whose disabled state was NOT stated is flagged as assumed
    assert report["predates"] == [("operator/old2", "tester", 1)]


def test_an_unreadable_event_is_skipped_not_fatal(fleet):
    fm = _load("fleet-models")
    good = json.dumps({"change_id": "X", "source": "s", "position": "implementer",
                       "ordinal": 1, "old_runner": None, "new_runner": "sub-opus",
                       "old_disabled": None, "new_disabled": 0})
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE event (ts, actor, type, repo, pr, sha, lease, "
                      "detail_json, source, line)")
    rows = [("r/a", good), ("r/b", "not json"), ("r/c", json.dumps({"position": "nonsense", "ordinal": 1})),
            ("r/d", json.dumps({"position": "tester", "ordinal": 0, "new_runner": "x"})),
            ("r/e", json.dumps({"position": "tester", "ordinal": 1}))]
    for i, (repo, d) in enumerate(rows, 1):
        con.execute("INSERT INTO event VALUES ('t','a','repo-model-changed',?,NULL,NULL,NULL,?,'events.jsonl',?)",
                    (repo, d, i))
    report = fm.reconstruct_repo_model(con)
    assert report["action"] == "reconstructed" and report["rows"] == 1 and report["skipped"] == 4
    assert _rows(con) == [("r/a", "implementer", 1, "sub-opus", 0)]


def test_the_collector_does_not_back_itself_up():
    """Boundary: the collector is the writer. A backup verb inside it runs while
    it holds the database, and the first failure is the collector blocking on
    itself."""
    src = (BIN / "fleet-collect").read_text()
    assert ".backup(" not in src and "VACUUM INTO" not in src and "fleet-db-snapshot" not in src


# ------------------------------------------------------------------ D3: WAL


def _journal_mode(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()


def test_a_writer_puts_the_database_in_wal(fleet):
    fm = _load("fleet-models")
    fm.connect().close()
    assert _journal_mode(fleet / "fleet.db") == "wal"


def test_wal_is_set_at_open_so_a_delete_mode_copy_is_fixed_by_the_next_writer(fleet):
    """The boundary's case: journal mode is persistent, so a database restored
    in `delete` mode must be corrected by whatever opens it next, not by a
    pragma someone ran once on the live file."""
    con = sqlite3.connect(fleet / "fleet.db")
    assert con.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    con.execute("CREATE TABLE t (a)")
    con.commit()
    con.close()
    assert _journal_mode(fleet / "fleet.db") == "delete"

    _load("fleet-models").connect().close()
    assert _journal_mode(fleet / "fleet.db") == "wal"


def _hold_exclusive_writer(path):
    w = sqlite3.connect(path, isolation_level=None)
    w.execute("BEGIN EXCLUSIVE")
    w.execute("INSERT INTO repo_model (repo, position, ordinal, runner) "
              "VALUES ('r/uncommitted', 'tester', 1, 'sub-opus')")
    return w


def test_a_reader_reads_while_a_writer_holds_a_transaction(fleet):
    fm = _load("fleet-models")
    con = fm.connect()
    fm.set_runner(con, "r/a", "implementer", "sub-opus", actor="cli:t")
    con.close()

    writer = _hold_exclusive_writer(fleet / "fleet.db")
    try:
        reader = sqlite3.connect(fleet / "fleet.db", timeout=0.2)
        assert _rows(reader) == [("r/a", "implementer", 1, "sub-opus", 0)]   # committed only
        reader.close()
        # and the orchestrator's own read-only path is not blocked either
        assert fm.resolve_repo("r/a", "implementer") == ("sub-opus", "repo")
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_the_same_reader_IS_blocked_in_delete_mode(fleet):
    """The control. Without it, the test above would pass on a database that
    never left `delete` mode, because a reader is only blocked by an EXCLUSIVE
    lock — this asserts the scenario really discriminates."""
    fm = _load("fleet-models")
    con = sqlite3.connect(fleet / "fleet.db")
    con.executescript(fm.SCHEMA)          # NOT through connect(): stays in delete mode
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner) VALUES ('r/a','implementer',1,'sub-opus')")
    con.commit()
    con.close()
    assert _journal_mode(fleet / "fleet.db") == "delete"

    writer = _hold_exclusive_writer(fleet / "fleet.db")
    try:
        reader = sqlite3.connect(fleet / "fleet.db", timeout=0.2)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            reader.execute("SELECT * FROM repo_model").fetchall()
        reader.close()
    finally:
        writer.execute("ROLLBACK")
        writer.close()


# --------------------------------------------------------- D4: the snapshot


def _seed_db(fleet, n=3):
    fm = _load("fleet-models")
    con = fm.connect()
    for i in range(n):
        fm.set_runner(con, f"r/repo{i}", "implementer", "sub-opus", actor="cli:t")
    rows = _rows(con)
    con.close()
    return rows


def _snap_rows(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return _rows(con)
    finally:
        con.close()


def test_a_snapshot_opens_and_carries_the_same_rows(fleet):
    rows = _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    dest = snap.snapshot(fleet / "fleet.db", fleet / "snapshots")
    assert dest.parent == fleet / "snapshots" and dest.name.startswith("fleetdb-")
    assert _snap_rows(dest) == rows


def test_a_snapshot_carries_committed_rows_that_only_the_wal_holds(fleet):
    """The case a file copy gets wrong even with no writer running. While any
    connection stays open the WAL is not checkpointed on close, so committed rows
    can live only in `fleet.db-wal`; copying `fleet.db` alone yields a file that
    opens fine and lacks them. The other tests here close their connections,
    which checkpoints and would let a `cp` pass by accident."""
    fm = _load("fleet-models")
    con = fm.connect()                        # stays open for the whole test
    for i in range(3):
        fm.set_runner(con, f"r/repo{i}", "implementer", "sub-opus", actor="cli:t")
    wal = fleet / "fleet.db-wal"
    assert wal.exists() and wal.stat().st_size > 0, "the rows must live in the WAL"
    rows = _rows(con)
    snap = _load("fleet-db-snapshot")
    dest = snap.snapshot(fleet / "fleet.db", fleet / "snapshots")
    con.close()
    assert _snap_rows(dest) == rows


def test_a_snapshot_taken_mid_transaction_has_the_committed_rows_only(fleet):
    """The plan's DoD, literally: a writer is mid-transaction during the backup.
    The copy must open, carry the committed rows, and NOT carry the uncommitted
    one — a file copy of the live database can do neither reliably."""
    rows = _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    writer = sqlite3.connect(fleet / "fleet.db", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO repo_model (repo, position, ordinal, runner) "
                   "VALUES ('r/in-flight', 'tester', 1, 'sub-opus')")
    try:
        dest = snap.snapshot(fleet / "fleet.db", fleet / "snapshots")
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert _snap_rows(dest) == rows


def test_snapshots_are_consistent_under_a_concurrent_writer(fleet):
    """Every transaction writes a PAIR of rows, so any snapshot with an odd count
    caught a write mid-flight. Ten snapshots against a writer that never rests."""
    fm = _load("fleet-models")
    fm.connect().close()
    snap = _load("fleet-db-snapshot")
    stop = threading.Event()
    errors = []

    def write():
        con = sqlite3.connect(fleet / "fleet.db", timeout=30)
        i = 0
        try:
            while not stop.is_set():
                i += 1
                con.execute("INSERT INTO repo_model (repo, position, ordinal, runner) "
                            "VALUES (?, 'tester', 1, 'sub-opus')", (f"r/a{i}",))
                con.execute("INSERT INTO repo_model (repo, position, ordinal, runner) "
                            "VALUES (?, 'tester', 1, 'sub-opus')", (f"r/b{i}",))
                con.commit()
        except Exception as e:                       # pragma: no cover - reported below
            errors.append(e)
        finally:
            con.close()

    t = threading.Thread(target=write)
    t.start()
    try:
        base = datetime(2026, 9, 18, tzinfo=timezone.utc)
        for i in range(10):
            dest = snap.snapshot(fleet / "fleet.db", fleet / "snapshots", keep=20,
                                 now=base + timedelta(seconds=i))
            assert len(_snap_rows(dest)) % 2 == 0
    finally:
        stop.set()
        t.join()
    assert not errors


def test_a_snapshot_is_one_self_contained_file(fleet):
    _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    dest = snap.snapshot(fleet / "fleet.db", fleet / "snapshots")
    assert _journal_mode(dest) == "delete"
    # the snapshot plus its logical dump beside it (plan 0063 D4a) and nothing else
    assert sorted(p.name for p in (fleet / "snapshots").iterdir()) == [
        dest.name, snap.dump_dir_for(dest).name]


def test_snapshots_use_the_backup_api_and_never_copy_the_file():
    src = (BIN / "fleet-db-snapshot").read_text()
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert ".backup(" in code
    for forbidden in ("shutil.copy", "copyfile", "os.system", "subprocess", '"cp"', "'cp'"):
        assert forbidden not in code


def test_rotation_is_bounded_and_the_newest_is_the_newest(fleet):
    _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    out = fleet / "snapshots"
    base = datetime(2026, 9, 10, 3, 30, tzinfo=timezone.utc)
    made = [snap.snapshot(fleet / "fleet.db", out, keep=3, now=base + timedelta(days=d))
            for d in range(6)]
    left = snap.existing(out)
    assert len(left) == 3
    assert left == made[-3:]
    assert left[-1] == made[-1] and left[-1].name == "fleetdb-20260915T033000Z.db"


def test_a_snapshot_made_out_of_order_does_not_displace_a_newer_one(fleet):
    """Ordering is by the stamp in the name, not mtime — a copy restored from
    elsewhere resets mtime and must not reshuffle retention."""
    _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    out = fleet / "snapshots"
    base = datetime(2026, 9, 10, tzinfo=timezone.utc)
    snap.snapshot(fleet / "fleet.db", out, keep=2, now=base + timedelta(days=5))
    snap.snapshot(fleet / "fleet.db", out, keep=2, now=base + timedelta(days=1))
    snap.snapshot(fleet / "fleet.db", out, keep=2, now=base + timedelta(days=2))
    assert [p.name for p in snap.existing(out)] == ["fleetdb-20260912T000000Z.db",
                                                    "fleetdb-20260915T000000Z.db"]


def test_rotation_never_touches_the_other_snapshot_tools_files(fleet):
    """bin/fleet-snapshot writes `fleet-*.db` into this same directory."""
    _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    out = fleet / "snapshots"
    out.mkdir()
    theirs = out / "fleet-20260101T000000Z.db"
    theirs.write_bytes(b"not mine")
    base = datetime(2026, 9, 10, tzinfo=timezone.utc)
    for d in range(4):
        snap.snapshot(fleet / "fleet.db", out, keep=1, now=base + timedelta(days=d))
    assert theirs.read_bytes() == b"not mine"
    assert not theirs.match(f"{snap.PREFIX}*")


def test_a_failed_snapshot_keeps_the_ones_already_held(fleet):
    _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    out = fleet / "snapshots"
    kept = snap.snapshot(fleet / "fleet.db", out, keep=1,
                         now=datetime(2026, 9, 10, tzinfo=timezone.utc))
    with pytest.raises(SystemExit):
        snap.snapshot(fleet / "no-such.db", out, keep=1,
                      now=datetime(2026, 9, 11, tzinfo=timezone.utc))
    assert snap.existing(out) == [kept]


def test_a_corrupt_source_never_produces_a_snapshot(fleet):
    (fleet / "fleet.db").write_bytes(b"this is not a database" * 100)
    snap = _load("fleet-db-snapshot")
    out = fleet / "snapshots"
    with pytest.raises((SystemExit, sqlite3.DatabaseError)):
        snap.snapshot(fleet / "fleet.db", out)
    assert snap.existing(out) == []
    assert not [p for p in out.iterdir() if p.name.endswith(".partial")]


def test_a_snapshot_stamp_collision_refuses_to_overwrite(fleet):
    _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    when = datetime(2026, 9, 10, tzinfo=timezone.utc)
    first = snap.snapshot(fleet / "fleet.db", fleet / "snapshots", now=when)
    before = first.read_bytes()
    with pytest.raises(SystemExit):
        snap.snapshot(fleet / "fleet.db", fleet / "snapshots", now=when)
    assert first.read_bytes() == before


def test_the_cli_refuses_keep_zero(fleet):
    """`--keep 0` meaning "keep everything" would turn a typo into an unbounded
    disk fill on a daily unit nobody watches."""
    _seed_db(fleet)
    r = subprocess.run([sys.executable, str(BIN / "fleet-db-snapshot"), "--keep", "0"],
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "FLEET_DB": str(fleet / "fleet.db"),
                            "EUNOMIA_FLEET_DIR": str(fleet)})
    assert r.returncode != 0
    assert not (fleet / "snapshots").exists()


def test_the_cli_writes_under_the_fleet_dir_by_default(fleet):
    rows = _seed_db(fleet)
    r = subprocess.run([sys.executable, str(BIN / "fleet-db-snapshot")],
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "FLEET_DB": str(fleet / "fleet.db"),
                            "EUNOMIA_FLEET_DIR": str(fleet)})
    assert r.returncode == 0, r.stderr
    (only,) = list((fleet / "snapshots").glob("fleetdb-*.db"))
    assert _snap_rows(only) == rows
    assert f"{len(rows)} repo_model row(s)" in r.stdout


# ------------------------------------------------------------ D5: the unit


UNIT = ROOT / "launchd" / "org.eunomia.fleet-db-snapshot.plist"


def test_the_unit_parses_under_plistlib_and_plutil():
    d = plistlib.load(UNIT.open("rb"))            # strict: expat refuses `--` in a comment
    assert d["Label"] == "org.eunomia.fleet-db-snapshot"
    if shutil.which("plutil"):
        r = subprocess.run(["plutil", "-lint", str(UNIT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_the_unit_runs_the_pinned_script_daily_and_logs():
    d = plistlib.load(UNIT.open("rb"))
    args = d["ProgramArguments"]
    assert args[-1].endswith("/.local/share/pins/eunomia/bin/fleet-db-snapshot")
    assert all("/dev/eunomia/" not in a for a in args)
    assert set(d["StartCalendarInterval"]) == {"Hour", "Minute"}     # once a day, no Weekday
    assert d["StandardErrorPath"] and d["StandardOutPath"]
    assert not d.get("RunAtLoad")
    assert (BIN / "fleet-db-snapshot").stat().st_mode & 0o111


# ------------------------------------------------------------- D6: the docs


def test_the_docs_name_the_snapshot_path_and_the_tiers():
    doc = (ROOT / "docs" / "fleet-db.md").read_text()
    assert "~/dev/.fleet/snapshots/fleetdb-<UTC stamp>.db" in doc
    assert "backup/dr-snapshot.sh" in doc                      # names the change NOT made here
    assert "repo-model-changed" in doc
    low = doc.lower()
    assert "durable" in low and "derived" in low
    for t in ("repo_model", "review", "model_price"):
        assert t in doc
    assert "Not yet wired" not in doc                          # plan 0076 wired it
    assert "fleet-collect" in doc and "reconstruct_repo_model" in doc


# ------------------------------------- plan 0076: the remainder of 0063, closed


def _run_collect(fleet, monkeypatch):
    _collect_events(fleet, monkeypatch)


def test_fleet_collect_reconstructs_a_dropped_repo_model_itself(fleet, monkeypatch, capsys):
    """D2's call, wired: no test calls `reconstruct_repo_model` here — the real
    `main()` does, over a history with a set, a disable and a re-enable."""
    fm = _load("fleet-models")
    con = fm.connect()
    _configure(fm, con)
    before = _rows(con)
    assert any(r[4] == 1 for r in before)
    con.close()
    _run_collect(fleet, monkeypatch)                     # ingests the events
    con = sqlite3.connect(fleet / "fleet.db")
    con.execute("DROP TABLE repo_model")
    con.commit()
    con.close()

    capsys.readouterr()
    _run_collect(fleet, monkeypatch)
    assert "repo_model: was absent — reconstructed" in capsys.readouterr().out
    con = sqlite3.connect(fleet / "fleet.db")
    assert _rows(con) == before                          # including `disabled`


def test_fleet_collect_reports_and_changes_nothing_when_the_table_disagrees(fleet, monkeypatch, capsys):
    fm = _load("fleet-models")
    con = fm.connect()
    _configure(fm, con)
    con.close()
    _run_collect(fleet, monkeypatch)
    con = sqlite3.connect(fleet / "fleet.db")
    con.execute("UPDATE repo_model SET runner='local-qwen' WHERE repo='operator/lynceus'")
    con.commit()
    before = _rows(con)
    con.close()

    capsys.readouterr()
    _run_collect(fleet, monkeypatch)
    assert "ledger disagrees on operator/lynceus" in capsys.readouterr().out
    assert _rows(sqlite3.connect(fleet / "fleet.db")) == before


def _delete_mode(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=DELETE").fetchone()
    con.close()
    assert _journal_mode(path) == "delete"


def test_collect_and_reviews_each_leave_the_database_in_wal(fleet, monkeypatch):
    """Asserted with a second connection, and from `delete` mode each time, so
    neither writer is credited with the other's work."""
    db = fleet / "fleet.db"
    sqlite3.connect(db).close()
    _delete_mode(db)
    _run_collect(fleet, monkeypatch)
    assert _journal_mode(db) == "wal"

    _delete_mode(db)
    reviews = _load("fleet-reviews")
    monkeypatch.setattr(reviews, "DB", str(db))
    monkeypatch.setattr(sys, "argv", ["fleet-reviews", "--report"])
    reviews.main()
    assert _journal_mode(db) == "wal"


# ------------------------------------------------------- D4a: the logical dump


def _dump(snap, fleet, when):
    dest = snap.snapshot(fleet / "fleet.db", fleet / "snapshots", keep=10, now=when)
    return snap.dump_dir_for(dest)


def test_the_dump_is_one_jsonl_file_per_durable_table_and_nothing_else(fleet):
    _seed_db(fleet)
    reviews = _load("fleet-reviews")
    con = sqlite3.connect(fleet / "fleet.db")
    con.executescript(reviews.SCHEMA)
    con.execute("INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict) "
                "VALUES (7, 'operator/x', 3, 1, 'revbot', 'APPROVED')")
    con.execute("CREATE TABLE session (id TEXT)")          # a derived table: never dumped
    con.commit()
    con.close()
    snap = _load("fleet-db-snapshot")
    d = _dump(snap, fleet, datetime(2026, 9, 24, tzinfo=timezone.utc))
    names = {p.name for p in d.iterdir()}
    assert names <= {f"{t}.jsonl" for t in snap.collect.DURABLE_TABLES}
    assert {"repo_model.jsonl", "review.jsonl"} <= names
    lines = (d / "repo_model.jsonl").read_text().splitlines()
    assert len(lines) == 3
    row = json.loads(lines[0])
    assert row["repo"] == "r/repo0" and row["runner"] == "sub-opus"
    assert list(row) == sorted(row)                        # keys sorted: a stable line
    assert json.loads((d / "review.jsonl").read_text())["verdict"] == "APPROVED"


def test_a_durable_row_changed_between_two_runs_is_a_one_line_diff(fleet):
    import difflib
    _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    first = _dump(snap, fleet, datetime(2026, 9, 24, tzinfo=timezone.utc))
    fm = _load("fleet-models")
    con = fm.connect()
    fm.set_runner(con, "r/repo1", "implementer", "local-qwen", actor="cli:t")
    con.close()
    second = _dump(snap, fleet, datetime(2026, 9, 25, tzinfo=timezone.utc))

    a = (first / "repo_model.jsonl").read_text().splitlines()
    b = (second / "repo_model.jsonl").read_text().splitlines()
    changed = [l for l in difflib.unified_diff(a, b, lineterm="", n=0)
               if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    assert len(changed) == 2                               # one line out, one line in
    assert "sub-opus" in changed[0] and "local-qwen" in changed[1]
    assert "r/repo1" in changed[1]


def test_rotation_takes_a_snapshots_dump_with_it(fleet):
    _seed_db(fleet)
    snap = _load("fleet-db-snapshot")
    base = datetime(2026, 9, 10, tzinfo=timezone.utc)
    for d in range(4):
        snap.snapshot(fleet / "fleet.db", fleet / "snapshots", keep=2, now=base + timedelta(days=d))
    left = sorted(p.name for p in (fleet / "snapshots").iterdir())
    assert left == ["fleetdb-20260912T000000Z.db", "fleetdb-20260912T000000Z.dump",
                    "fleetdb-20260913T000000Z.db", "fleetdb-20260913T000000Z.dump"]


def test_the_dump_is_not_described_as_the_restore_path():
    doc = (ROOT / "docs" / "fleet-db.md").read_text()
    assert "fleetdb-<UTC stamp>.dump" in doc
    assert "not the restore path" in doc.lower()


# ----------------------------------- D4b: two named sets and a completeness test


def _all_tables(con):
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _full_schema(tmp_path):
    """Every table the fleet creates, through each owner's own SCHEMA — plus the
    collector's views, which must never read as unclassified."""
    collect = _load("fleet-collect")
    con = sqlite3.connect(tmp_path / "all.db")
    for mod in (collect, _load("fleet-models"), _load("fleet-config"), _load("fleet-reviews")):
        con.executescript(mod.SCHEMA)
    for view in (collect.DISPATCH_VIEW, collect.CI_VIEW, collect.DISPATCH_SESSION_VIEW,
                 collect.DISPATCH_COST_VIEW):
        con.executescript(view)
    return collect, con


def test_every_table_is_classified_and_the_sets_are_disjoint(tmp_path, fleet):
    collect, con = _full_schema(tmp_path)
    assert not collect.DERIVED_TABLES & collect.DURABLE_TABLES
    assert _all_tables(con) == collect.DERIVED_TABLES | collect.DURABLE_TABLES
    views = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert {"dispatch", "dispatch_session", "dispatch_cost"} <= views
    assert not views & _all_tables(con)                    # a view is never a table here


def test_a_table_in_neither_set_fails_until_it_is_classified(tmp_path, fleet):
    """The alarm, proven: `durable = schema - derived` would make this pass."""
    collect, con = _full_schema(tmp_path)
    con.execute("CREATE TABLE brand_new (a)")
    classified = collect.DERIVED_TABLES | collect.DURABLE_TABLES
    assert _all_tables(con) - classified == {"brand_new"}
    assert _all_tables(con) != classified                  # what the suite asserts, failing
    assert _all_tables(con) == classified | {"brand_new"}  # ...and passing once it is named


def test_a_name_in_both_sets_is_refused(tmp_path, fleet):
    collect, _con = _full_schema(tmp_path)
    assert (collect.DERIVED_TABLES | {"review"}) & collect.DURABLE_TABLES == {"review"}


def test_the_rebuild_drop_tuple_is_derived_tables_and_never_a_durable_one(fleet):
    """The drop tuple stays a literal (tests/test_repo_models.py reads it as
    one); this ties it to the named set so the two cannot drift."""
    import re
    collect = _load("fleet-collect")
    m = re.search(r"for t in \(([^)]*)\):", (BIN / "fleet-collect").read_text())
    dropped = set(re.findall(r'"(\w+)"', m.group(1)))
    assert dropped <= collect.DERIVED_TABLES
    assert not dropped & collect.DURABLE_TABLES
    # the three that outlive a bump are exactly the forge-derived ones
    assert collect.DERIVED_TABLES - dropped == {"ci_task", "ci_log", "ci_sync"}
