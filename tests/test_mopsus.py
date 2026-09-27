"""mopsus — a failed dispatch arrives with a handle, not an investigation (plan 0046).

Builds a `fleet.db` the same way `fleet-collect` does — by loading its SCHEMA
and DISPATCH_VIEW and inserting `event`/`dispatch_phase` rows directly — so
the `dispatch` view mopsus queries is the REAL view, not a hand-rolled stand-in
that could drift from it.
"""
import importlib.machinery
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def mp():
    return _load("mopsus", BIN / "mopsus")


@pytest.fixture
def fc():
    return _load("fleet_collect", BIN / "fleet-collect")


@pytest.fixture(autouse=True)
def _wire(mp, monkeypatch, tmp_path):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    monkeypatch.delenv("FLEET_ANGELIA_URL", raising=False)
    # notify() is bin/orchestrator's real angelia/ntfy sender — replaced with a
    # recorder so tests never touch a network, while record_page() (called
    # from inside the real notify()) still exercises the real durable log.
    calls = []
    monkeypatch.setattr(mp.orch, "notify",
                        lambda title, body: (calls.append((title, body)), True)[1])
    # Default: a readable forge with no marked PR for anything — stubbed at
    # fleet-watch's own seam (never mopsus's `_marked_pr_check` wrapper, so
    # tests that exercise `_marked_pr_check` directly still run its real
    # body). Every test that does not care about the marked-PR condition
    # gets ordinary mechanical-mode behaviour without ever touching the real
    # forge or token helper; a test that does care overrides one of these two.
    monkeypatch.setattr(mp.fwatch, "_token", lambda: ("test-token", ""))
    monkeypatch.setattr(mp.fwatch, "marked_pr",
                        lambda repo, plan_id, token, dry_run=False: None)
    return calls


def _db(fc, tmp_path, phase_rows, event_rows):
    path = tmp_path / ".fleet" / "fleet.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(fc.SCHEMA)
    con.executescript(fc.DISPATCH_VIEW)
    con.executemany(
        "INSERT INTO dispatch_phase VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", phase_rows)
    con.executemany(
        "INSERT INTO event VALUES (?,?,?,?,?,?,?,?,?,?)", event_rows)
    con.commit()
    con.close()
    return path


def _start_phase(lease, repo, plan_id, session="sess-1", seq=1, source=None):
    return (lease, seq, "2026-09-13T00:00:00Z", "start", None,
           None, None, None, None, None, None,
           json.dumps({"repo": repo, "plan": plan_id, "lease": lease,
                       "session": session}),
           source or f"{lease}.log", seq)


def _exit_phase(lease, rc, timed_out, seconds, seq=2, source=None):
    return (lease, seq, "2026-09-13T01:00:00Z", "implementer-exit", None,
           rc, timed_out, seconds, None, None, None,
           "{}", source or f"{lease}.log", seq)


def _failed_event(lease, reason, log=None, transcript=None, line=1):
    detail = {"lease": lease, "reason": reason}
    if log is not None:
        detail["log"] = log
    if transcript is not None:
        detail["transcript"] = transcript
    return ("2026-09-13T01:00:01Z", "orchestrator", "plan-failed", "operator/x",
           None, None, None, json.dumps(detail), "events.jsonl", line)


# --- config/mopsus.conf ------------------------------------------------------


def test_mode_defaults_to_escalate_for_unlisted_repo(mp):
    assert mp.mode_for("operator/unlisted", {}) == "escalate"
    assert mp.mode_for("operator/unlisted", {"default": "escalate"}) == "escalate"


def test_mode_honors_repo_entry_over_default(mp):
    values = {"default": "escalate", "operator/quiet": "off"}
    assert mp.mode_for("operator/quiet", values) == "off"
    assert mp.mode_for("operator/other", values) == "escalate"


def test_conf_parses_all_four_named_modes(mp, tmp_path):
    conf = tmp_path / "mopsus.conf"
    conf.write_text(
        "default = escalate\n"
        "operator/a = mechanical\n"
        "operator/b = triage\n"
        "operator/c = off\n"
    )
    values = mp.load_conf(conf)
    assert values == {"default": "escalate", "operator/a": "mechanical",
                      "operator/b": "triage", "operator/c": "off"}


def test_conf_rejects_unknown_mode(mp, tmp_path):
    conf = tmp_path / "mopsus.conf"
    conf.write_text("operator/a = auto-fix\n")
    with pytest.raises(mp.ConfigError):
        mp.load_conf(conf)


def test_conf_rejects_unparseable_line(mp, tmp_path):
    conf = tmp_path / "mopsus.conf"
    conf.write_text("this is not a config line\n")
    with pytest.raises(mp.ConfigError):
        mp.load_conf(conf)


def test_missing_conf_file_defaults_every_repo_to_escalate(mp, tmp_path, capsys):
    values = mp.load_conf(tmp_path / "does-not-exist.conf")
    assert values == {}
    assert mp.mode_for("operator/anything", values) == "escalate"
    assert "escalate" in capsys.readouterr().err


# --- the brief ---------------------------------------------------------------


def test_ares_style_timeout_brief_names_timeout_bound_and_paths(mp, tmp_path):
    lease = "branch--ares-0001--001"
    log_path = tmp_path / f"{lease}.log"
    log_path.write_text("2026-09-13T00:00:00Z start\n"
                        "2026-09-13T01:30:00Z implementer-exit rc=143 "
                        "timed_out=True seconds=5400\n")
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text("SECRET-CANARY-DO-NOT-LEAK\n")

    row = {
        "lease": lease, "repo": "operator/ares", "plan_id": "0001-ranged-standoff-core",
        "pr": None,
        "failure_reason": ("implementer timed out after 90 min; process group "
                           "killed"),
        "failure_log": str(log_path), "failure_transcript": str(transcript_path),
        "rc": 143, "timed_out": 1, "seconds": 5400,
    }
    brief = mp.compose_brief(row)

    assert "90 min" in brief
    assert "timed_out:  yes" in brief
    assert str(log_path) in brief
    assert str(transcript_path) in brief
    # the path appears; the transcript's CONTENT never does
    assert "SECRET-CANARY-DO-NOT-LEAK" not in brief


def test_credential_shape_refusal_brief_has_no_transcript_at_all(mp, tmp_path):
    lease = "branch--eunomia-0045--001"
    log_path = tmp_path / f"{lease}.log"
    log_path.write_text("2026-09-13T00:00:00Z start\n"
                        "2026-09-13T00:00:01Z plan-carries-credential "
                        "shapes=github-pat\n")
    row = {
        "lease": lease, "repo": "operator/eunomia",
        "plan_id": "0045-ci-logs-ingested-and-served",
        "pr": None,
        "failure_reason": ("plan text carries credential shapes "
                           "['github-pat'] — refusing to build a prompt "
                           "from it"),
        "failure_log": str(log_path), "failure_transcript": None,
        "rc": None, "timed_out": None, "seconds": None,
    }
    brief = mp.compose_brief(row)

    assert "credential shapes" in brief
    assert "transcript: -" in brief
    assert str(log_path) in brief


def test_brief_names_branch_learned_from_the_lease_record(mp, monkeypatch, tmp_path):
    lib = mp.lib
    lease = "branch--ares-0001--001"
    lib.write_lease({"id": lease, "resource": {"type": "branch",
                                               "branch": "feat/0001-standoff"}})
    row = {"lease": lease, "repo": "operator/ares", "plan_id": "0001-x", "pr": None,
          "failure_reason": "boom", "failure_log": None, "failure_transcript": None,
          "rc": None, "timed_out": None, "seconds": None}
    brief = mp.compose_brief(row)
    assert "feat/0001-standoff" in brief


def test_brief_survives_a_lease_with_no_record_at_all(mp):
    row = {"lease": "branch--ghost--001", "repo": "operator/x", "plan_id": "0002",
          "pr": None, "failure_reason": "boom", "failure_log": None,
          "failure_transcript": None, "rc": None, "timed_out": None,
          "seconds": None}
    brief = mp.compose_brief(row)
    assert "branch:     -" in brief


# --- the page: must survive being a phone notification -----------------------


def test_page_fits_the_length_budget_with_a_very_long_reason(mp):
    reason = "x" * 500
    page = mp.compose_page("operator/ares", "0001-ranged-standoff-core", reason,
                           "h7")
    assert len(page) <= mp.PAGE_MAX_CHARS
    assert page.endswith("\nmopsus h7")


def test_page_carries_the_handle_whole_never_truncated(mp):
    page = mp.compose_page("operator/x", "0001", "short reason", "h42")
    assert page.split("\n")[-1] == "mopsus h42"


# --- idempotence: one brief per failure, not one per cycle -------------------


def test_sweep_pages_once_and_never_again_for_the_same_lease(mp, fc, tmp_path, _wire):
    lease = "branch--ares-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/ares", "0001-ranged-standoff-core"),
              _exit_phase(lease, 143, 1, 5400)],
             [_failed_event(lease, "implementer timed out after 90 min; "
                            "process group killed",
                            log=str(tmp_path / f"{lease}.log"))])

    n_paged, n_refused = mp.sweep(db=db)
    assert (n_paged, n_refused) == (1, 0)
    assert len(_wire) == 1
    handle = mp._load_state()["leases"][lease]["handle"]
    assert mp._read_brief(handle) is not None

    n_paged2, n_refused2 = mp.sweep(db=db)
    assert (n_paged2, n_refused2) == (0, 0)
    assert len(_wire) == 1, "a second sweep must not page the same failure again"


def test_off_mode_never_pages_or_writes_a_brief(mp, fc, tmp_path, _wire):
    lease = "branch--quiet-0009--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/quiet", "0009-x"),
              _exit_phase(lease, 1, 0, 10)],
             [_failed_event(lease, "boom")])
    conf = tmp_path / "mopsus.conf"
    conf.write_text("operator/quiet = off\n")

    n_paged, n_refused = mp.sweep(conf_path=conf, db=db)
    assert (n_paged, n_refused) == (0, 0)
    assert _wire == []
    assert mp._load_state()["leases"] == {}


def test_triage_mode_also_refuses_by_name(mp, fc, tmp_path, _wire, capsys):
    lease = "branch--triage-0009--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/tr", "0009-x"),
              _exit_phase(lease, 1, 0, 10)],
             [_failed_event(lease, "boom")])
    conf = tmp_path / "mopsus.conf"
    conf.write_text("operator/tr = triage\n")

    n_paged, n_refused = mp.sweep(conf_path=conf, db=db)
    assert (n_paged, n_refused) == (0, 1)
    assert _wire == []


def test_absent_repo_gets_escalate_and_pages(mp, fc, tmp_path, _wire):
    lease = "branch--unlisted-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/unlisted", "0001-x"),
              _exit_phase(lease, 1, 0, 10)],
             [_failed_event(lease, "boom")])
    conf = tmp_path / "mopsus.conf"
    conf.write_text("operator/somethingelse = off\n")   # unlisted repo, no default line

    n_paged, n_refused = mp.sweep(conf_path=conf, db=db)
    assert (n_paged, n_refused) == (1, 0)
    assert len(_wire) == 1


def test_dry_run_writes_no_state_and_pages_nobody(mp, fc, tmp_path, _wire):
    lease = "branch--dry-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/dry", "0001-x"),
              _exit_phase(lease, 1, 0, 10)],
             [_failed_event(lease, "boom")])

    n_paged, n_refused = mp.sweep(db=db, dry_run=True)
    assert (n_paged, n_refused) == (1, 0)
    assert _wire == []
    assert not mp._state_path().exists()


# --- D5: first run sends ONE summary, never one per pre-existing failure -----


def test_first_run_with_a_backlog_sends_one_summary_page_and_writes_every_brief(
        mp, fc, tmp_path, _wire):
    leases = ["branch--a-0001--001", "branch--b-0002--001", "branch--c-0003--001"]
    phase_rows, event_rows = [], []
    for i, lease in enumerate(leases):
        phase_rows += [_start_phase(lease, f"operator/r{i}", f"000{i}-x"),
                       _exit_phase(lease, 1, 0, 10)]
        event_rows.append(_failed_event(lease, f"boom-{i}", line=i + 1))
    db = _db(fc, tmp_path, phase_rows, event_rows)

    assert not mp._state_path().exists()
    n_paged, n_refused = mp.sweep(db=db)
    assert (n_paged, n_refused) == (1, 0), "a first-ever sweep pages ONE summary"
    assert len(_wire) == 1
    assert "3" in _wire[0][1]

    state = mp._load_state()
    assert len(state["leases"]) == 3
    for lease in leases:
        handle = state["leases"][lease]["handle"]
        assert mp._read_brief(handle) is not None, "every brief is still written"

    # the NEXT run sends nothing for these already-paged failures
    n_paged2, n_refused2 = mp.sweep(db=db)
    assert (n_paged2, n_refused2) == (0, 0)
    assert len(_wire) == 1, "the next run must not re-page the backlog"


def test_state_file_present_two_new_failures_send_two_separate_pages(
        mp, fc, tmp_path, _wire):
    # prime the state file with a real (empty) sweep first — first_run is
    # decided by the file's presence, not by whether anything paged.
    empty_db = _db(fc, tmp_path, [], [])
    n0, r0 = mp.sweep(db=empty_db)
    assert (n0, r0) == (0, 0)
    assert mp._state_path().exists()
    assert _wire == []

    lease_a, lease_b = "branch--x-0001--001", "branch--y-0002--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease_a, "operator/x", "0001-x"), _exit_phase(lease_a, 1, 0, 10),
              _start_phase(lease_b, "operator/y", "0002-x"), _exit_phase(lease_b, 1, 0, 10)],
             [_failed_event(lease_a, "boom-a", line=1), _failed_event(lease_b, "boom-b", line=2)])
    n_paged, n_refused = mp.sweep(db=db)
    assert (n_paged, n_refused) == (2, 0), "an ordinary cycle pages each failure separately"
    assert len(_wire) == 2

    state = mp._load_state()
    handle_a = state["leases"][lease_a]["handle"]
    handle_b = state["leases"][lease_b]["handle"]
    bodies = [body for _, body in _wire]
    assert any(f"mopsus {handle_a}" in b for b in bodies)
    assert any(f"mopsus {handle_b}" in b for b in bodies)


# --- D5b: `mopsus list` -------------------------------------------------------


def test_list_names_every_handle_with_repo_and_plan_id_from_its_own_brief(
        mp, fc, tmp_path, _wire):
    lease = "branch--ares-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/ares", "0001-ranged-standoff-core"),
              _exit_phase(lease, 143, 1, 5400)],
             [_failed_event(lease, "boom")])
    mp.sweep(db=db)
    handle = mp._load_state()["leases"][lease]["handle"]

    entries = mp.list_handles()
    assert entries == [(lease, handle, "operator/ares", "0001-ranged-standoff-core")]


def test_list_names_a_handle_with_a_missing_brief_rather_than_skipping_it(mp, tmp_path):
    mp._state_path().parent.mkdir(parents=True, exist_ok=True)
    mp._save_state({"next_id": 2, "leases": {"branch--ghost--001": {"handle": "h1"}}})
    assert mp._read_brief("h1") is None       # never written / since removed

    entries = mp.list_handles()
    assert entries == [("branch--ghost--001", "h1", None, None)]


def test_cli_list_prints_every_handle(mp, fc, tmp_path, _wire, capsys):
    lease = "branch--cli4-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/cli4", "0001-x"),
              _exit_phase(lease, 1, 0, 10)],
             [_failed_event(lease, "boom")])
    mp.sweep(db=db)
    rc = mp.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "operator/cli4" in out and "0001-x" in out


def test_cli_list_with_no_handles_says_so(mp, capsys):
    rc = mp.main(["list"])
    assert rc == 0
    assert "no handles" in capsys.readouterr().out


def test_no_fleet_db_reads_as_no_failures_not_an_error(mp, tmp_path):
    missing = tmp_path / "does-not-exist" / "fleet.db"
    assert mp.failed_dispatches(missing) == []


def test_only_terminal_failures_are_swept(mp, fc, tmp_path, _wire):
    lease = "branch--live-0001--001"
    db = _db(fc, tmp_path, [_start_phase(lease, "operator/live", "0001-x")], [])
    n_paged, n_refused = mp.sweep(db=db)
    assert (n_paged, n_refused) == (0, 0)
    assert _wire == []


# --- opening a handle ---------------------------------------------------------


def test_open_session_hands_the_stored_brief_to_exec(mp, fc, tmp_path, _wire):
    lease = "branch--ares-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/ares", "0001-ranged-standoff-core"),
              _exit_phase(lease, 143, 1, 5400)],
             [_failed_event(lease, "implementer timed out after 90 min; "
                            "process group killed")])
    mp.sweep(db=db)
    handle = mp._load_state()["leases"][lease]["handle"]

    seen = []
    rc = mp.open_session(handle, exec_fn=lambda brief: seen.append(brief))
    assert rc == 0
    assert len(seen) == 1
    assert "operator/ares" in seen[0]
    assert "90 min" in seen[0]


def test_open_session_on_an_unknown_handle_fails_without_exec(mp, capsys):
    seen = []
    rc = mp.open_session("h404", exec_fn=lambda brief: seen.append(brief))
    assert rc == 1
    assert seen == []
    assert "h404" in capsys.readouterr().err


# --- CLI -----------------------------------------------------------------------


def test_main_with_no_args_sweeps(mp, fc, tmp_path, _wire, capsys):
    lease = "branch--cli-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/cli", "0001-x"),
              _exit_phase(lease, 1, 0, 10)],
             [_failed_event(lease, "boom")])
    rc = mp.main(["--db", str(db)])
    assert rc == 0
    assert "1 paged" in capsys.readouterr().out


def test_main_exits_nonzero_when_a_refusal_happened(mp, fc, tmp_path, _wire):
    lease = "branch--cli2-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/cli2", "0001-x"),
              _exit_phase(lease, 1, 0, 10)],
             [_failed_event(lease, "boom")])
    conf = tmp_path / "mopsus.conf"
    conf.write_text("operator/cli2 = triage\n")
    rc = mp.main(["--db", str(db), "--conf", str(conf)])
    assert rc == 2


def test_main_with_a_handle_opens_it(mp, fc, tmp_path, _wire, monkeypatch):
    lease = "branch--cli3-0001--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/cli3", "0001-x"),
              _exit_phase(lease, 1, 0, 10)],
             [_failed_event(lease, "boom")])
    mp.sweep(db=db)
    handle = mp._load_state()["leases"][lease]["handle"]

    seen = []
    monkeypatch.setattr(mp, "_default_exec", lambda brief: seen.append(brief))
    rc = mp.main([handle])
    assert rc == 0
    assert len(seen) == 1


# --- mechanical: requeue a failure that happened before any work began (0078) --

TRANSPORT = "ssh: connect to host forge.example port 3022: Connection refused"


def _mech(mp, fc, tmp_path, reason=TRANSPORT, holder="sess-1", state="active",
          lease="branch--mech-0009--001", requeues=0, limit=None, extra_events=()):
    """A failed lease under `default = mechanical`, with a real lease record."""
    mp.lib.write_lease({"id": lease, "holder": holder, "state": state,
                        "resource": {"type": "branch", "branch": "feat/0009-x"}})
    events = [_failed_event(lease, reason)]
    for i in range(requeues):
        events.append(("2026-09-13T02:00:0%dZ" % i, "mopsus", "plan-requeued",
                       "operator/mech", None, None, f"old-{i}",
                       json.dumps({"plan": "0009-x", "lease": f"old-{i}",
                                   "class": "transport", "attempt": i + 1,
                                   "limit": 2}), "events.jsonl", 10 + i))
    events.extend(extra_events)
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/mech", "0009-x"),
              _exit_phase(lease, 1, 0, 10)], events)
    if limit is not None:
        import sqlite3 as _sq
        con = _sq.connect(str(db))
        con.execute("CREATE TABLE fleet_setting (key TEXT PRIMARY KEY, value TEXT, "
                    "changed_at TEXT)")
        con.execute("INSERT INTO fleet_setting VALUES ('retry_limit', ?, 'now')",
                    (str(limit),))
        con.commit()
        con.close()
    conf = tmp_path / "mopsus.conf"
    conf.write_text("default = mechanical\n")
    return lease, db, conf


def _ledger(tmp_path, etype):
    p = tmp_path / ".fleet" / "events.jsonl"
    if not p.exists():
        return []
    evs = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return [e for e in evs if e["type"] == etype]


@pytest.fixture
def release_spy(mp, monkeypatch):
    """Spies on subprocess.run inside mopsus, still running the real command."""
    calls = []
    real = mp.subprocess.run

    def spy(cmd, *a, **kw):
        calls.append((cmd, kw.get("env", {}).get("EUNOMIA_SESSION")))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(mp.subprocess, "run", spy)
    return calls


def test_transport_failure_is_requeued_without_a_page(mp, fc, tmp_path, _wire, release_spy):
    lease, db, conf = _mech(mp, fc, tmp_path)
    assert mp.sweep(conf_path=conf, db=db) == (0, 0)
    releases = [c for c in release_spy if "fleet-release" in c[0][1]]
    assert len(releases) == 1 and releases[0][1] == "mopsus"
    assert mp.lib.read_lease(lease)["state"] == "released"
    (ev,) = _ledger(tmp_path, "plan-requeued")
    assert ev["repo"] == "operator/mech" and ev["lease"] == lease
    assert ev["detail"] == {"plan": "0009-x", "lease": lease, "class": "transport",
                            "attempt": 1, "limit": 2}
    assert _wire == []
    rel = _ledger(tmp_path, "lease-released")
    assert rel[0]["detail"] == {"forced": True, "holder_was": "sess-1"}
    # a second sweep does nothing
    assert mp.sweep(conf_path=conf, db=db) == (0, 0)
    assert len(_ledger(tmp_path, "plan-requeued")) == 1
    assert _wire == []


def test_limit_reached_escalates_with_the_attempts_in_the_brief(mp, fc, tmp_path, _wire):
    lease, db, conf = _mech(mp, fc, tmp_path, requeues=2)
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert len(_wire) == 1
    handle = mp._load_state()["leases"][lease]["handle"]
    brief = mp._read_brief(handle)
    assert f"reason:     {TRANSPORT}\nrequeued:   2 of 2 — limit reached\n" in brief
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []


def test_attempts_are_counted_from_events_jsonl_too(mp, fc, tmp_path, _wire):
    lease, db, conf = _mech(mp, fc, tmp_path)
    fleet = tmp_path / ".fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    with open(fleet / "events.jsonl", "w") as fh:
        for i in range(2):
            fh.write(json.dumps({"type": "plan-requeued", "repo": "operator/mech",
                                 "detail": {"plan": "0009-x"}}) + "\n")
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert "2 of 2 — limit reached" in mp._read_brief("h1")


def test_retry_limit_zero_escalates_and_releases_nothing(mp, fc, tmp_path, _wire):
    lease, db, conf = _mech(mp, fc, tmp_path, limit=0)
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert len(_wire) == 1
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []


def test_a_raised_retry_limit_allows_a_third_requeue(mp, fc, tmp_path, _wire):
    lease, db, conf = _mech(mp, fc, tmp_path, requeues=2, limit=3)
    assert mp.sweep(conf_path=conf, db=db) == (0, 0)
    (ev,) = _ledger(tmp_path, "plan-requeued")
    assert ev["detail"]["attempt"] == 3 and ev["detail"]["limit"] == 3


@pytest.mark.parametrize("cls,reason", [
    ("work-exists-unverified", "no verified marked PR: none"),
    ("timeout", "implementer timed out after 90 min"),
    ("raced-ci", "CI is still running"),
    ("branch-collision", "could not create branch feat/x: already exists"),
])
def test_a_class_outside_retryable_is_escalated_never_released(
        mp, fc, tmp_path, _wire, cls, reason):
    assert mp.classify(reason) == cls
    lease, db, conf = _mech(mp, fc, tmp_path, reason=reason)
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []
    assert "requeued:" not in mp._read_brief("h1")


def test_only_the_no_work_classes_are_retryable_and_every_other_class_is_not(mp):
    # plan 0079 added backend-before-work: the backend failed before any commit
    no_work = {"transport", "backend-before-work"}
    assert mp.RETRYABLE_CLASSES == no_work
    for cls in mp.CLASS_ACTIONS:
        assert (cls in mp.RETRYABLE_CLASSES) == (cls in no_work)


@pytest.mark.parametrize("holder,state", [("someone-else", "active"),
                                          ("sess-1", "released"),
                                          ("sess-1", "orphaned")])
def test_a_lease_that_is_not_the_failed_holders_active_one_is_escalated(
        mp, fc, tmp_path, _wire, holder, state):
    lease, db, conf = _mech(mp, fc, tmp_path, holder=holder, state=state)
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert mp.lib.read_lease(lease)["state"] == state
    assert _ledger(tmp_path, "plan-requeued") == []
    assert len(_wire) == 1


def test_a_failing_release_escalates_with_its_stderr_and_the_event_still_counts(
        mp, fc, tmp_path, _wire, monkeypatch):
    """Emit happens BEFORE release (ADR-0012 §2 / eunomia#559): a release that
    fails after a successful emit still leaves the attempt counted, because
    the count is the bound and over-counting is the safe direction — the
    alternative, an uncounted release, would let the same lease requeue past
    `retry_limit` with nothing to show for it."""
    lease, db, conf = _mech(mp, fc, tmp_path)
    calls = []
    monkeypatch.setattr(mp, "_release_lease",
                        lambda l: (calls.append(l), (False, "fleet-release: kaboom"))[1])
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert calls == [lease]                       # never retried
    assert "fleet-release: kaboom" in mp._read_brief("h1")
    (ev,) = _ledger(tmp_path, "plan-requeued")     # the emit succeeded and counts
    assert ev["detail"]["attempt"] == 1
    assert len(_wire) == 1


def test_emit_failing_stops_the_release_and_records_nothing(
        mp, fc, tmp_path, _wire, monkeypatch):
    """The reverse case: if plan-requeued cannot be recorded, mopsus must not
    release the lease at all — an uncounted release is the failure mode
    ADR-0012 §2 exists to close off."""
    lease, db, conf = _mech(mp, fc, tmp_path)
    monkeypatch.setattr(mp, "_emit_requeued", lambda row, attempt, limit: (False, "disk full"))
    release_calls = []
    monkeypatch.setattr(mp, "_release_lease",
                        lambda l: (release_calls.append(l), (True, ""))[1])
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert release_calls == []
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []
    assert "requeue:    skipped — plan-requeued not recorded: disk full" in mp._read_brief("h1")
    assert len(_wire) == 1


def _pr_event(lease, repo, plan_id, pr, seq=3, source=None):
    """A `pr-marked`-shaped phase row is overkill here — the dispatch view's
    `pr` column is what try_requeue reads, and the simplest way to populate it
    through the same view fleet-collect builds is a review-start phase row
    carrying the pr number, exactly as test_mopsus_classify.py's
    test_format_json_carries_class_actions_and_pr already does."""
    return (lease, seq, "2026-09-13T01:00:00Z", "review-start", pr,
           None, None, None, None, None, None, "{}", source or f"{lease}.log", seq)


def test_a_plan_with_a_marked_pr_already_recorded_is_never_requeued(
        mp, fc, tmp_path, _wire, monkeypatch):
    """ADR-0012 §1 (amended): row["pr"] is not None is the cheapest and first
    check — no forge read needed when the dispatch view already names one."""
    lease = "branch--mech-0009--001"
    mp.lib.write_lease({"id": lease, "holder": "sess-1", "state": "active",
                        "resource": {"type": "branch", "branch": "feat/0009-x"}})
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/mech", "0009-x"),
              _exit_phase(lease, 1, 0, 10, seq=2),
              _pr_event(lease, "operator/mech", "0009-x", 77)],
             [_failed_event(lease, TRANSPORT)])
    conf = tmp_path / "mopsus.conf"
    conf.write_text("default = mechanical\n")

    called = []
    monkeypatch.setattr(mp.fwatch, "marked_pr",
                        lambda *a, **k: (called.append(a), None)[1])
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert called == [], "row['pr'] alone must short-circuit — no forge read at all"
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []
    brief = mp._read_brief("h1")
    assert "requeue:    skipped — marked PR #77 exists" in brief


def test_a_marked_pr_found_on_the_forge_is_never_requeued(mp, fc, tmp_path, _wire, monkeypatch):
    lease, db, conf = _mech(mp, fc, tmp_path)
    monkeypatch.setattr(mp.fwatch, "marked_pr",
                        lambda repo, plan_id, token, dry_run=False: {"number": 99})
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []
    assert "requeue:    skipped — marked PR #99 exists" in mp._read_brief("h1")


def test_an_unreadable_forge_counts_as_a_marked_pr_and_is_never_requeued(
        mp, fc, tmp_path, _wire, monkeypatch):
    lease, db, conf = _mech(mp, fc, tmp_path)
    monkeypatch.setattr(mp.fwatch, "marked_pr",
                        lambda repo, plan_id, token, dry_run=False: "unreadable")
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []
    assert ("requeue:    skipped — could not read the forge for a marked PR"
           in mp._read_brief("h1"))


def test_a_failed_token_fetch_counts_as_a_marked_pr_and_is_never_requeued(
        mp, fc, tmp_path, _wire, monkeypatch):
    lease, db, conf = _mech(mp, fc, tmp_path)
    monkeypatch.setattr(mp.fwatch, "_token", lambda: (None, "token helper produced no token"))
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []
    assert ("requeue:    skipped — could not read the forge for a marked PR"
           in mp._read_brief("h1"))


def test_a_raising_forge_scan_escalates_rather_than_crashing_the_sweep(
        mp, fc, tmp_path, _wire, monkeypatch):
    """fleet-watch's own `marked_pr` raising (a network error `Forge` did not
    catch, say) must not crash the sweep — `_marked_pr_check` catches it and
    reads it the same as "unreadable": no requeue, escalate."""
    lease, db, conf = _mech(mp, fc, tmp_path)

    def boom(repo, plan_id, token, dry_run=False):
        raise RuntimeError("forge exploded")

    monkeypatch.setattr(mp.fwatch, "marked_pr", boom)
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _ledger(tmp_path, "plan-requeued") == []
    assert ("requeue:    skipped — could not read the forge for a marked PR"
           in mp._read_brief("h1"))


def test_marked_pr_check_delegates_to_fleet_watchs_own_scan_with_its_own_token(
        mp, monkeypatch):
    """Never a second forge-scanning implementation — the seam is a thin
    pass-through to fleet-watch's `marked_pr`, called with the token
    fleet-watch's own `_token()` resolves."""
    seen = []

    def fake_marked_pr(repo, plan_id, token, dry_run=False):
        seen.append((repo, plan_id, token))
        return None

    monkeypatch.setattr(mp.fwatch, "_token", lambda: ("tok-123", ""))
    monkeypatch.setattr(mp.fwatch, "marked_pr", fake_marked_pr)
    assert mp._marked_pr_check("operator/x", "0001-x") is None
    assert seen == [("operator/x", "0001-x", "tok-123")]


def test_a_requeued_lease_is_not_listed_as_a_missing_brief(mp, fc, tmp_path, _wire):
    lease, db, conf = _mech(mp, fc, tmp_path)
    mp.sweep(conf_path=conf, db=db)
    assert mp.list_handles() == []


def test_the_shipped_conf_defaults_to_mechanical_and_triage_still_refuses(mp):
    values = mp.load_conf()
    assert values["default"] == "mechanical"
    assert "triage" not in mp.BUILT_MODES and "mechanical" in mp.BUILT_MODES
