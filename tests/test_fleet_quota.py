"""A usage limit holds the fleet, and a reset lets it go (plan 0094).

`bin/fleet-quota` is the gate's one writer; `bin/mopsus` closes it from a stop,
probes it, and requeues what it held; `bin/fleet-watch` observes it first and
dispatches nothing while `session` or `weekly` is closed. Each layer is
exercised here against the real modules, with the real ledger and the real
`dispatch` view — only the page sender and the probe are replaced.
"""
import datetime
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from test_fleet_watch import (
    _load as _fw_load,
    _wire as _fw_wire,
    _api_factory,
    _stub_orchestrator,
    _spawns,
    _wait_spawns,
)
from test_mopsus import _mech, _ledger, _db as _mopsus_db  # noqa: F401
from test_mopsus_review_retry import _seed_attempt

BIN = Path(__file__).resolve().parent.parent / "bin"

QUOTA_REASON = ("session ended on a backend error before any work: API 429 after 2s — "
                "You've hit your session limit · resets 3:10am (America/New_York)")


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def fq():
    return _load("fleet_quota_t", BIN / "fleet-quota")


@pytest.fixture
def mp():
    return _load("mopsus_quota_t", BIN / "mopsus")


@pytest.fixture
def fc():
    return _load("fleet_collect_quota_t", BIN / "fleet-collect")


@pytest.fixture(autouse=True)
def pages(fq, mp, monkeypatch, tmp_path):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    monkeypatch.setenv("EUNOMIA_SESSION", "quota-test")
    monkeypatch.setenv("FLEET_QUOTA_LATEST", str(tmp_path / "latest.json"))
    monkeypatch.delenv("FLEET_QUOTA_ACCOUNT", raising=False)
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    monkeypatch.delenv("FLEET_ANGELIA_URL", raising=False)
    calls = []
    rec = lambda title, body: (calls.append(body), True)[1]      # noqa: E731
    fq.NOTIFY = rec
    monkeypatch.setattr(mp.orch, "notify", rec)
    monkeypatch.setattr(mp.fwatch, "_token", lambda: ("test-token", ""))
    monkeypatch.setattr(mp.fwatch, "marked_pr",
                        lambda repo, plan_id, token, dry_run=False: None)
    monkeypatch.setattr(mp.fwatch, "repos", lambda: ["operator/sniff"])
    return calls


def _ago(minutes):
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot(tmp_path, as_of, **percents):
    quotas = {k: {"percent": v, "resetsAt": "2026-09-29T16:00:00Z"}
              for k, v in percents.items()}
    (tmp_path / "latest.json").write_text(json.dumps({
        "generatedAt": _ts(_ago(0)),
        "accounts": {"claude": {"dataAsOf": _ts(as_of), "quotas": quotas}}}))


def _events(tmp_path, etype):
    return _ledger(tmp_path, etype)


def _held_pages(pages):
    return [p for p in pages if p.startswith("fleet held")]


# --- observe --------------------------------------------------------------------


def test_a_fresh_snapshot_at_100_closes_once_and_at_6_opens_once(fq, tmp_path, pages):
    _snapshot(tmp_path, _ago(1), session=100)
    assert fq.observe() == [("session", "closed")]
    g = fq.gate("session")
    assert g["state"] == "closed" and g["closed_by"] == "meter"
    assert g["resets_at"] == "2026-09-29T16:00:00Z"
    (ev,) = _events(tmp_path, "quota-closed")
    assert ev["detail"]["scope"] == "session" and ev["detail"]["percent"] == 100
    assert fq.observe() == []                       # the same snapshot again
    assert len(_events(tmp_path, "quota-closed")) == 1
    _snapshot(tmp_path, _ago(0), session=6)
    assert fq.observe() == [("session", "open")]
    assert len(_events(tmp_path, "quota-opened")) == 1
    assert fq.observe() == []
    assert pages == ["fleet held: session limit, until 2026-09-29T16:00:00Z",
                     "fleet released: session reset (meter)"]


def _accounts(tmp_path, as_of, **accounts):
    """accounts: label -> {quota key: percent}; one shared resetsAt per quota."""
    (tmp_path / "latest.json").write_text(json.dumps({
        "generatedAt": _ts(_ago(0)),
        "accounts": {
            label: {"dataAsOf": _ts(as_of),
                    "quotas": {k: {"percent": v, "resetsAt": f"2026-11-0{i + 3}T00:00:00Z"}
                               for i, (k, v) in enumerate(q.items())}}
            for label, q in accounts.items()}}))


def test_another_vendors_meter_never_closes_a_claude_gate(fq, tmp_path):
    _accounts(tmp_path, _ago(1), claude={"weekly": 40, "session": 10},
              codex={"session": 5, "weekly": 100}, gemini={"weekly": 100})
    assert fq.observe() == [("vendor:codex", "closed"), ("vendor:gemini", "closed")]
    assert fq.gate("weekly") is None and fq.gate("session") is None
    assert fq.held() is None


def test_the_claude_meter_alone_feeds_session_and_weekly(fq, tmp_path):
    _accounts(tmp_path, _ago(1), claude={"weekly": 100}, codex={"weekly": 3})
    assert fq.observe() == [("weekly", "closed")]
    assert fq.held()[0] == "weekly"
    assert fq.closed_vendor_gates() == {"claude": ("weekly", "2026-11-03T00:00:00Z")}


def test_a_vendor_gate_closes_on_any_quota_at_100_and_opens_when_all_clear(
        fq, tmp_path, pages):
    _accounts(tmp_path, _ago(1), codex={"session": 100, "weekly": 100})
    assert fq.observe() == [("vendor:codex", "closed")]
    g = fq.gate("vendor:codex")
    assert g["closed_by"] == "meter" and g["resets_at"] == "2026-11-04T00:00:00Z"
    assert fq.observe() == []                        # one stop closes once
    assert len(_events(tmp_path, "quota-closed")) == 1
    _accounts(tmp_path, _ago(0), codex={"session": 0, "weekly": 100})
    assert fq.observe() == []                        # still one at 100
    _accounts(tmp_path, _ago(0), codex={"session": 0, "weekly": 42})
    assert fq.observe() == [("vendor:codex", "open")]
    assert fq.closed_vendor_gates() == {}
    assert pages == ["codex seats held: vendor:codex limit, until 2026-11-04T00:00:00Z",
                     "codex seats released: vendor:codex reset (meter)"]


def test_a_vendor_gate_reading_from_before_the_stop_does_not_open_it(fq, tmp_path):
    fq.close("vendor:muse", None, "meter", "meter", at=_ts(_ago(0)))
    _accounts(tmp_path, _ago(5), muse={"weekly": 1})
    assert fq.observe() == []
    assert fq.gate("vendor:muse")["state"] == "closed"


def test_vendor_scopes_are_valid_and_hold_nothing_fleet_wide(fq):
    assert all(fq.valid_scope(f"vendor:{a}") for a in ("codex", "gemini", "muse"))
    assert not fq.valid_scope("vendor:claude") and not fq.valid_scope("vendor:")
    assert not set(fq.HOLDING_SCOPES) & {"vendor:codex"}
    fq.close("vendor:codex", None, "meter", "meter")
    assert fq.held() is None
    assert fq.check()[0] != fq.EX_CLOSED
    assert fq.closed_vendor_gates() == {"codex": ("vendor:codex", None)}


def test_the_account_override_picks_the_claude_meter(fq, tmp_path, monkeypatch):
    _accounts(tmp_path, _ago(1), main={"session": 100}, claude={"session": 1})
    monkeypatch.setenv("FLEET_QUOTA_ACCOUNT", "main")
    assert fq.observe() == [("session", "closed")]


def test_a_stale_missing_or_malformed_meter_changes_nothing(fq, tmp_path):
    assert fq.observe() == []                        # no file
    (tmp_path / "latest.json").write_text("{not json")
    assert fq.observe() == []
    _snapshot(tmp_path, _ago(31), session=100)       # 31 minutes old
    assert fq.observe() == []
    assert fq.gates() == []
    fq.close("session", None, "stop", "L1", at=_ts(_ago(60)))
    _snapshot(tmp_path, _ago(31), session=5)
    assert fq.observe() == []
    assert fq.gate("session")["state"] == "closed"


def test_a_passed_resets_at_is_not_a_reset(fq, tmp_path):
    fq.close("session", _ts(_ago(120)), "stop", "L1", at=_ts(_ago(300)))
    _snapshot(tmp_path, _ago(1), session=100)
    assert fq.observe() == []
    assert fq.gate("session")["state"] == "closed"


def test_a_meter_reading_from_before_the_stop_does_not_open_the_gate(fq, tmp_path):
    fq.close("session", None, "stop", "L1", at=_ts(_ago(10)))
    _snapshot(tmp_path, _ago(20), session=90)
    assert fq.observe() == []
    assert fq.gate("session")["state"] == "closed"
    _snapshot(tmp_path, _ago(1), session=90)
    assert fq.observe() == [("session", "open")]


def test_one_stop_closes_once_and_a_stop_before_the_open_changes_nothing(fq, tmp_path):
    assert fq.close("weekly", None, "stop", "L1", at=_ts(_ago(30))) is True
    assert fq.close("weekly", None, "stop", "L1", at=_ts(_ago(30))) is False
    assert fq.open_gate("weekly", "owner") is True
    assert fq.close("weekly", None, "stop", "L1", at=_ts(_ago(30))) is False
    assert fq.close("weekly", None, "stop", "L2", at=_ts(_ago(20))) is False
    assert len(_events(tmp_path, "quota-closed")) == 1
    assert fq.close("weekly", None, "stop", "L3", at=_ts(_ago(-1))) is True   # a later stop


def test_the_owner_opens_a_gate_and_it_is_recorded(fq, tmp_path):
    fq.close("session", None, "stop", "L1")
    assert fq.open_gate("session", "owner") is True
    assert fq.gate("session")["opened_by"] == "owner"
    (ev,) = _events(tmp_path, "quota-opened")
    assert ev["detail"]["by"] == "owner"
    assert fq.open_gate("session", "owner") is False        # no second event
    assert len(_events(tmp_path, "quota-opened")) == 1


def test_a_model_gate_pages_nobody(fq, tmp_path, pages):
    fq.close("model:fable", None, "stop", "review_attempt:1")
    fq.open_gate("model:fable", "probe")
    assert pages == []
    assert len(_events(tmp_path, "quota-closed")) == 1


def test_check_exits_0_75_and_1(fq, tmp_path, capsys):
    assert fq.main(["check"]) == 1
    assert fq.main(["check", "--scope", "session"]) == 1
    fq.close("weekly", None, "stop", "L0", at=_ts(_ago(9)))
    fq.open_gate("weekly", "owner")
    assert fq.main(["check"]) == 0
    fq.close("session", "2026-09-29T07:10:00Z", "stop", "L1")
    capsys.readouterr()
    assert fq.main(["check", "--scope", "session"]) == 75
    assert capsys.readouterr().out.strip() == "closed session until 2026-09-29T07:10:00Z"
    fq.open_gate("session", "owner")
    fq.close("session", None, "stop", "L2", at=_ts(_ago(-1)))
    capsys.readouterr()
    assert fq.main(["check"]) == 75
    assert capsys.readouterr().out.strip() == "closed session until unknown"


def test_status_json_lists_the_gates(fq, capsys):
    fq.close("session", None, "stop", "L1")
    capsys.readouterr()
    assert fq.main(["status", "--json"]) == 0
    (g,) = json.loads(capsys.readouterr().out)["gates"]
    assert g["scope"] == "session" and g["state"] == "closed"


def test_the_bucket_mapping(fq):
    assert fq.model_scope("claude-fable-5-1") == "model:fable"
    assert fq.model_scope("claude-sonnet-5") is None
    assert fq.model_scope(None) is None


def test_a_fresh_meter_opens_the_model_gate_after_the_close(fq, tmp_path):
    fq.close("model:fable", None, "stop", "review_attempt:1", at=_ts(_ago(10)))
    _snapshot(tmp_path, _ago(1), fable=40)
    assert fq.observe() == [("model:fable", "open")]


def test_resolving_the_reset_time_takes_the_first_occurrence_after_the_failure(fq):
    r = "resets 3:10am (America/New_York)"
    assert fq.resolve_reset(r, "2026-09-28T23:30:00Z") == "2026-09-29T07:10:00Z"
    assert fq.resolve_reset(r, "2026-09-28T05:00:00Z") == "2026-09-28T07:10:00Z"
    assert fq.resolve_reset("resets 11:20pm (America/New_York)",
                            "2026-09-28T23:30:00Z") == "2026-09-29T03:20:00Z"
    assert fq.resolve_reset("resets 3:10am", "2026-09-28T23:30:00Z") is None
    assert fq.resolve_reset("resets 3:10am (Nowhere/Land)", "2026-09-28T23:30:00Z") is None
    assert fq.scope_of_reason("You've hit your weekly limit") == "weekly"
    assert fq.scope_of_reason(QUOTA_REASON) == "session"


# --- mopsus: a stop closes, a probe opens, a reset requeues -----------------------


def _sweep(mp, db, conf):
    return mp.sweep(conf_path=conf, db=db)


def test_an_implementer_quota_stop_closes_session_and_is_held_then_requeued(
        mp, fc, fq, tmp_path, pages):
    lease, db, conf = _mech(mp, fc, tmp_path, reason=QUOTA_REASON)
    assert _sweep(mp, db, conf) == (1, 0)
    g = fq.gate("session", db)
    assert g["state"] == "closed" and g["closed_by"] == "stop" and g["evidence"] == lease
    assert g["resets_at"] == "2026-09-13T07:10:00Z"       # after the failure, not before
    assert _held_pages(pages) == ["fleet held: session limit, until 2026-09-13T07:10:00Z"]
    state = mp._load_state()["leases"][lease]
    assert state["held"] == "session"
    brief = mp._read_brief(state["handle"])
    assert "held:      session closed until 2026-09-13T07:10:00Z\n" in brief
    n_pages = len(pages)
    # already in state["leases"], yet re-read on every sweep while held
    for _ in range(3):
        assert _sweep(mp, db, conf) == (0, 0)
    assert len(pages) == n_pages and _events(tmp_path, "plan-requeued") == []
    assert mp.lib.read_lease(lease)["state"] == "active"
    fq.open_gate("session", "owner", db=db)
    assert _sweep(mp, db, conf) == (0, 0)
    (ev,) = _events(tmp_path, "plan-requeued")
    assert ev["lease"] == lease and ev["detail"]["class"] == "quota"
    assert ev["detail"]["attempt"] == 1
    assert mp.lib.read_lease(lease)["state"] == "released"
    assert mp._load_state()["leases"][lease] == {"requeued": True}
    assert _sweep(mp, db, conf) == (0, 0)
    assert len(_events(tmp_path, "plan-requeued")) == 1


def test_a_weekly_reason_closes_weekly(mp, fc, fq, tmp_path):
    reason = QUOTA_REASON.replace("session limit", "weekly limit")
    lease, db, conf = _mech(mp, fc, tmp_path, reason=reason)
    _sweep(mp, db, conf)
    assert fq.gate("weekly", db)["state"] == "closed" and fq.gate("session", db) is None


def test_a_held_failure_at_the_retry_limit_becomes_an_ordinary_page(
        mp, fc, fq, tmp_path, pages):
    lease, db, conf = _mech(mp, fc, tmp_path, reason=QUOTA_REASON, requeues=2)
    _sweep(mp, db, conf)
    fq.open_gate("session", "owner", db=db)
    n = len(pages)
    assert _sweep(mp, db, conf) == (1, 0)
    assert len(pages) == n + 1                         # the fresh escalation
    entry = mp._load_state()["leases"][lease]
    assert "held" not in entry
    assert "requeued:   2 of 2 — limit reached" in mp._read_brief(entry["handle"])
    assert mp.lib.read_lease(lease)["state"] == "active"
    assert _events(tmp_path, "plan-requeued") == []


def test_a_held_failure_with_a_marked_pr_is_not_requeued(
        mp, fc, fq, tmp_path, monkeypatch):
    lease, db, conf = _mech(mp, fc, tmp_path, reason=QUOTA_REASON)
    _sweep(mp, db, conf)
    fq.open_gate("session", "owner", db=db)
    monkeypatch.setattr(mp.fwatch, "marked_pr",
                        lambda repo, plan_id, token, dry_run=False: {"number": 5})
    _sweep(mp, db, conf)
    entry = mp._load_state()["leases"][lease]
    assert "marked PR #5 exists" in mp._read_brief(entry["handle"])
    assert _events(tmp_path, "plan-requeued") == []
    assert mp.lib.read_lease(lease)["state"] == "active"


def test_a_quota_failure_after_work_began_is_never_requeued(mp, fc, fq, tmp_path):
    reason = QUOTA_REASON.replace("before any work", "after work began")
    lease, db, conf = _mech(mp, fc, tmp_path, reason=reason)
    assert mp.classify(reason) != "quota"
    _sweep(mp, db, conf)
    assert fq.gate("session", db) is None
    assert _events(tmp_path, "plan-requeued") == []


def test_a_stop_older_than_the_last_open_does_not_hold_and_requeues_at_once(
        mp, fc, fq, tmp_path):
    lease, db, conf = _mech(mp, fc, tmp_path, reason=QUOTA_REASON)
    fq.close("session", None, "stop", "earlier", at=_ts(_ago(600)), db=db)
    fq.open_gate("session", "owner", db=db)
    _sweep(mp, db, conf)
    assert fq.gate("session", db)["state"] == "open"
    assert len(_events(tmp_path, "plan-requeued")) == 1


def test_a_review_quota_stop_closes_only_its_models_scope(mp, fq, tmp_path):
    db = tmp_path / ".fleet" / "fleet.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    _seed_attempt(db, model="claude-fable-5-1", started="2026-09-27T00:00:00Z")
    _seed_attempt(db, pr=8, model="claude-sonnet-5", started="2026-09-27T00:00:00Z")
    for _ in range(10):
        mp.review_stop_sweep(db)
    assert [g["scope"] for g in fq.gates(db)] == ["model:fable"]
    assert fq.gate("model:fable", db)["evidence"] == "review_attempt:1"
    assert len(_events(tmp_path, "quota-closed")) == 1
    # a meter open, then the same row read again: it does not close it twice
    _snapshot(tmp_path, _ago(1), fable=10)
    assert fq.observe(db=db) == [("model:fable", "open")]
    mp.review_stop_sweep(db)
    assert fq.gate("model:fable", db)["state"] == "open"
    assert len(_events(tmp_path, "quota-closed")) == 1


def _record_starts(mp, monkeypatch):
    started = []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: (started.append(json.loads(payload)), True)[1])
    return started


def test_a_gate_is_probed_at_resets_at_plus_two_minutes_and_not_before(
        mp, fq, tmp_path, monkeypatch):
    db = tmp_path / ".fleet" / "fleet.db"
    started = _record_starts(mp, monkeypatch)
    fq.close("session", _ts(_ago(-10)), "stop", "L1", db=db)
    mp.quota_probe_sweep(db)
    assert started == []                                  # 10 minutes ahead
    fq.open_gate("session", "owner", db=db)
    fq.close("session", _ts(_ago(1)), "stop", "L2", at=_ts(_ago(-1)), db=db)
    mp.quota_probe_sweep(db)
    assert started == []                                  # 1 minute past: grace not over
    fq.open_gate("session", "owner", db=db)
    fq.close("session", _ts(_ago(3)), "stop", "L3", at=_ts(_ago(-2)), db=db)
    mp.quota_probe_sweep(db)
    assert started == [{"model": "claude-sonnet-5", "gate": "session"}]


def test_a_session_gate_with_no_meter_and_no_review_is_probed_and_opened_by_probe(
        mp, fq, tmp_path, monkeypatch):
    db = tmp_path / ".fleet" / "fleet.db"
    started = _record_starts(mp, monkeypatch)
    fq.close("session", None, "stop", "L1", db=db)         # no resets_at, no latest.json
    mp.quota_probe_sweep(db)
    (payload,) = started
    assert payload == {"model": "claude-sonnet-5", "gate": "session"}
    lock = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(mp, "run_probe", lambda model, db=None: "held")
    mp._cmd_review_lock_run([str(lock), json.dumps(payload), str(db)])
    assert fq.gate("session", db)["state"] == "closed"     # held: nothing opens
    lock = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(mp, "run_probe", lambda model, db=None: "reset")
    mp._cmd_review_lock_run([str(lock), json.dumps(payload), str(db)])
    g = fq.gate("session", db)
    assert g["state"] == "open" and g["opened_by"] == "probe"


def test_a_probe_error_opens_nothing(mp, fq, tmp_path, monkeypatch):
    db = tmp_path / ".fleet" / "fleet.db"
    fq.close("session", None, "stop", "L1", db=db)
    lock = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(mp, "run_probe", lambda model, db=None: "error")
    mp._cmd_review_lock_run([str(lock), json.dumps(
        {"model": "claude-sonnet-5", "gate": "session"}), str(db)])
    assert fq.gate("session", db)["state"] == "closed"


def test_a_model_gate_is_probed_with_the_stopped_attempts_model(
        mp, fq, tmp_path, monkeypatch):
    db = tmp_path / ".fleet" / "fleet.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    _seed_attempt(db, model="claude-fable-5-1", started="2026-09-27T00:00:00Z")
    started = _record_starts(mp, monkeypatch)
    mp.review_stop_sweep(db)
    mp.quota_probe_sweep(db)
    assert started == [{"model": "claude-fable-5-1", "gate": "model:fable"}]


# --- fleet-watch: a closed gate holds all dispatch ------------------------------------


def test_a_closed_session_gate_holds_the_cycle_and_opening_it_releases(
        tmp_path, capsys, monkeypatch):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _fw_load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP="1")
    _fw_wire(mod, _api_factory(), orch)
    os.environ["FLEET_QUOTA_LATEST"] = str(tmp_path / "latest.json")
    sent = []
    monkeypatch.setattr(mod, "notify", lambda t, b, *a, **k: (sent.append(b), True)[1])
    q = mod._quota()
    assert q.close("session", "2099-01-01T00:00:00Z", "stop", "L1")
    for _ in range(2):
        assert mod.cycle() == 0
        assert "held: session closed until 2099-01-01T00:00:00Z" in capsys.readouterr().out
    _snapshot(tmp_path, _ago(1), session=100)
    for _ in range(3):
        assert q.observe() == []
    assert _spawns(log) == []
    assert len([b for b in sent if b.startswith("fleet held")]) == 1
    assert q.open_gate("session", "owner")
    assert mod.cycle() == 1
    assert len(_wait_spawns(log, 1)) == 1


def test_a_closed_model_gate_does_not_hold_dispatch(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _fw_load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP="1")
    _fw_wire(mod, _api_factory(), orch)
    os.environ["FLEET_QUOTA_LATEST"] = str(tmp_path / "latest.json")
    mod._quota().close("model:fable", None, "stop", "review_attempt:1")
    assert mod.cycle() == 1
    assert len(_wait_spawns(log, 1)) == 1


def test_a_closed_codex_gate_does_not_hold_a_repo_with_no_codex_seat(tmp_path, capsys):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _fw_load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP="1")
    _fw_wire(mod, _api_factory(), orch)
    os.environ["FLEET_QUOTA_LATEST"] = str(tmp_path / "latest.json")
    mod._quota().close("vendor:codex", "2099-01-01T00:00:00Z", "meter", "meter")
    assert mod.cycle() == 1
    assert "held: vendor:codex closed until 2099-01-01T00:00:00Z" in capsys.readouterr().out
    assert len(_wait_spawns(log, 1)) == 1


def test_with_no_meter_and_no_gate_the_cycle_is_unchanged(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _fw_load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP="1")
    _fw_wire(mod, _api_factory(), orch)
    os.environ["FLEET_QUOTA_LATEST"] = str(tmp_path / "absent.json")
    assert mod.cycle() == 1
    assert mod._cycle_held is None


def test_a_gemini_third_party_quota_does_not_close_the_gemini_gate(fq):
    # `3p-*` is antigravity's allowance for other vendors' models; no runner spends it
    read = fq._vendor_reading({
        "3p-5h": {"percent": 100, "resetsAt": "2026-11-03T01:00:00Z"},
        "argon-weekly": {"percent": 13.6, "resetsAt": "2026-11-08T15:56:55Z"}})
    assert read[0] == 13.6
    closed = fq._vendor_reading({
        "argon-weekly": {"percent": 100, "resetsAt": "2026-11-08T15:56:55Z"}})
    assert closed[0] == 100.0 and closed[1]


def test_argon_at_100_closes_its_own_gate_and_leaves_gemini_open(fq, tmp_path):
    # 2026-11-06: argon-weekly read 100 while every other gemini quota read 0-1%;
    # one vendor gate held flash seats behind argon's limit
    _accounts(tmp_path, _ago(1), gemini={"session": 0, "weekly": 0,
                                         "argon-weekly": 100, "3p-weekly": 0.7})
    assert fq.observe() == [("vendor:gemini-argon", "closed")]
    assert fq.gate("vendor:gemini") is None
    assert set(fq.closed_vendor_gates()) == {"gemini-argon"}
    assert fq.valid_scope("vendor:gemini-argon")


def test_a_gemini_gate_closed_by_argon_reopens_once_argon_is_split_out(fq, tmp_path):
    # the gate the old meter closed for argon opens on the next fresh reading
    fq.close("vendor:gemini", "2026-11-08T15:56:55Z", "meter", "meter",
             at=_ts(_ago(3)))
    _accounts(tmp_path, _ago(1), gemini={"session": 0, "weekly": 0,
                                         "argon-weekly": 100})
    made = fq.observe()
    assert ("vendor:gemini", "open") in made
    assert ("vendor:gemini-argon", "closed") in made


def test_a_gemini_weekly_at_100_still_closes_the_vendor_gate(fq, tmp_path):
    _accounts(tmp_path, _ago(1), gemini={"weekly": 100, "argon-weekly": 10})
    assert fq.observe() == [("vendor:gemini", "closed")]
    assert fq.gate("vendor:gemini-argon") is None
