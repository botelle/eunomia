"""mopsus retries a quota-held review on credits, inside an owner window (plan 0146).

Real `bin/fleet-credits` against a temporary fleet.db; the forge, the lock child
and the dispatcher are replaced.
"""
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest

from test_mopsus_review_retry import _dbpath, _seed_attempt, _StubForge, _open_pr

BIN = Path(__file__).resolve().parent.parent / "bin"
CLI = str(BIN / "fleet-credits")
FABLE = "claude-fable-5-1"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def mp():
    return _load("mopsus_credits_t", BIN / "mopsus")


@pytest.fixture
def fc(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    monkeypatch.setenv("FLEET_DB", str(_dbpath(tmp_path)))
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    monkeypatch.delenv("FLEET_ANGELIA_URL", raising=False)
    mod = _load("fleet_credits_mt", BIN / "fleet-credits")
    mod.NOTIFY = lambda title, body: None
    return mod


@pytest.fixture
def world(mp, fc, tmp_path, monkeypatch):
    """A held fable review on a session PR; nothing started yet."""
    db = _dbpath(tmp_path)
    mp.fwatch._forge = lambda token: _StubForge(pulls={("operator/sniff", 7): _open_pr()})
    monkeypatch.setattr(mp.fwatch, "_token", lambda: ("tok", ""))
    monkeypatch.setattr(mp.fwatch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(mp.orch, "notify", lambda t, b: True)
    monkeypatch.setattr(mp, "review_retry_limit", lambda db=None: 2)
    monkeypatch.setattr(mp, "_review_path_open", lambda q, db=None: q)
    monkeypatch.setattr(mp, "_dispatcher_accepts_credits", lambda: True)
    started, probes = [], []
    monkeypatch.setattr(mp, "_try_start_locked",
                        lambda payload, db=None: started.append(json.loads(payload)))
    monkeypatch.setattr(mp, "_probe_due", lambda *a, **k: (probes.append(a), False)[1])
    return db, started, probes


def _close_gate(fc, db):
    fc.fquota.close("model:fable", None, "stop", "ev-1", db=str(db))


def _open_window(fc):
    con = fc._connect()
    t = time.time()
    fc._insert(con, "credit_window", opened_at=t, ends_at=t + 86400, note=None)
    con.close()


def _events(tmp_path, kind):
    p = tmp_path / ".fleet" / "events.jsonl"
    out = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in out if e["type"] == kind]


def test_a_quota_held_row_with_a_window_retries_on_credits_without_probing(
        mp, fc, world, tmp_path):
    db, started, probes = world
    _seed_attempt(db, model=FABLE)
    _close_gate(fc, db)
    _open_window(fc)
    mp.review_retry_sweep(db=db)
    assert len(started) == 1 and started[0]["bill"] == "credits"
    assert probes == []
    # the peek reserved nothing
    assert sqlite3.connect(str(db)).execute(
        "SELECT COUNT(*) FROM credit_reservation").fetchone()[0] == 0


def test_with_no_window_it_probes_as_before(mp, fc, world):
    db, started, probes = world
    _seed_attempt(db, model=FABLE)
    _close_gate(fc, db)
    mp.review_retry_sweep(db=db)
    assert started == [] and len(probes) == 1


def test_a_window_with_an_open_gate_does_not_send_work_to_credits(mp, fc, world):
    db, started, probes = world
    _seed_attempt(db, model=FABLE)
    _open_window(fc)
    mp.review_retry_sweep(db=db)
    assert len(probes) == 1 and started == []


def test_a_no_verdict_retry_is_never_billed_to_credits(mp, fc, world, monkeypatch):
    db, started, probes = world
    _seed_attempt(db, model=FABLE, outcome="no-verdict", ordinal=2)
    _close_gate(fc, db)
    _open_window(fc)
    monkeypatch.setattr(mp, "_credits_decision",
                        lambda *a, **k: pytest.fail("decide must not be asked"))
    mp.review_retry_sweep(db=db)
    assert len(started) == 1 and "bill" not in started[0]
    assert started[0]["reason"] == "no-verdict"


def test_a_budget_row_closes_no_gate(mp, fc, world):
    db, _, _ = world
    _seed_attempt(db, model=FABLE, outcome="budget")
    mp.review_stop_sweep(db)
    assert fc.fquota.gate("model:fable", str(db)) is None


def test_a_budget_row_is_retried_on_credits_when_decide_says_credits(mp, fc, world):
    db, started, probes = world
    _seed_attempt(db, model=FABLE, outcome="budget")
    _close_gate(fc, db)
    _open_window(fc)
    mp.review_retry_sweep(db=db)
    assert [s.get("bill") for s in started] == ["credits"] and probes == []


def test_a_budget_row_goes_to_the_subscription_when_the_gate_reopened(mp, fc, world):
    db, started, probes = world
    _seed_attempt(db, model=FABLE, outcome="budget")
    _open_window(fc)
    mp.review_retry_sweep(db=db)
    assert started == [] and len(probes) == 1            # today's path: probe, then retry


def test_a_budget_row_waits_on_hold(mp, fc, world):
    db, started, probes = world
    _seed_attempt(db, model=FABLE, outcome="budget")
    _close_gate(fc, db)                                  # gate closed, no window: hold
    mp.review_retry_sweep(db=db)
    assert started == [] and len(probes) == 1


def test_a_credits_retry_counts_against_the_limit(mp, fc, world, tmp_path):
    db, started, _ = world
    _seed_attempt(db, model=FABLE)
    _close_gate(fc, db)
    _open_window(fc)
    mp.review_retry_sweep(db=db)
    payload = dict(started[0], kind="session")
    dispatched = []
    mp._dispatch_session_retry = lambda repo, pr, bill=None: dispatched.append((repo, pr, bill))
    mp._attempt_retry(payload, db=db)
    assert dispatched == [("operator/sniff", 7, "credits")]
    ev = _events(tmp_path, "review-requeued")
    assert ev[0]["detail"]["billing"] == "credits" and ev[0]["detail"]["attempt"] == 1


def test_the_lock_child_runs_no_probe_for_a_credits_retry(mp, fc, world, tmp_path, monkeypatch):
    db, started, _ = world
    _seed_attempt(db, model=FABLE)
    _close_gate(fc, db)
    _open_window(fc)
    mp.review_retry_sweep(db=db)
    monkeypatch.setattr(mp, "run_probe", lambda *a, **k: pytest.fail("no probe"))
    dispatched = []
    monkeypatch.setattr(mp, "_dispatch_session_retry",
                        lambda repo, pr, bill=None: dispatched.append(bill))
    fd = os.open(str(tmp_path / "lockfd"), os.O_CREAT | os.O_RDWR)
    mp._cmd_review_lock_run([str(fd), json.dumps(dict(started[0], kind="session")), str(db)])
    assert dispatched == ["credits"]


def test_the_dispatch_argv_carries_bill_credits(mp, monkeypatch):
    seen = []
    monkeypatch.setattr(mp.subprocess, "run", lambda argv, **k: seen.append(argv))
    monkeypatch.setattr(mp.orch, "review_command", lambda: ["dispatch-review.py"])
    mp._dispatch_session_retry("operator/sniff", 7, bill="credits")
    mp._dispatch_session_retry("operator/sniff", 7)
    assert seen[0] == ["dispatch-review.py", "operator/sniff", "7", "--post", "--bill", "credits"]
    assert seen[1] == ["dispatch-review.py", "operator/sniff", "7", "--post"]


def test_a_dispatcher_without_bill_credits_holds_and_says_so_once(mp, fc, world, tmp_path,
                                                                  monkeypatch):
    db, started, probes = world
    monkeypatch.setattr(mp, "_dispatcher_accepts_credits", lambda: False)
    _seed_attempt(db, model=FABLE)
    _close_gate(fc, db)
    _open_window(fc)
    mp.review_retry_sweep(db=db)
    mp.review_retry_sweep(db=db)
    assert started == [] and len(probes) == 2
    ev = _events(tmp_path, "credits-refused")
    assert [e["detail"] for e in ev] == [
        {"repo": "operator/sniff", "pr": 7, "reason": "dispatcher"}]


# Two real argparse dispatchers shaped like ~/agent/dispatch-review.py: today's, with no
# `--bill`, and talos plan 0029's, with `--bill subscription|credits`. Not stubs that
# exit a chosen code: a stub is what hid eunomia#968, where `--bill credits --help`
# exited 0 on both because argparse runs `--help` before it reports `--bill` unknown.
_DISPATCHER = """#!{py}
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("repo", nargs="?")
ap.add_argument("pr", nargs="?")
ap.add_argument("--via-api", action="store_true")
ap.add_argument("--post", action="store_true")
{bill}
ap.parse_args()
"""
_BILL = 'ap.add_argument("--bill", choices=("subscription", "credits"), default="subscription")'


def _dispatcher(tmp_path, monkeypatch, with_bill):
    d = tmp_path / ("new.py" if with_bill else "old.py")
    d.write_text(_DISPATCHER.format(py=sys.executable, bill=_BILL if with_bill else ""))
    d.chmod(d.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FLEET_REVIEW_CMD", f"{sys.executable} {d}")
    return [sys.executable, str(d)]


@pytest.mark.parametrize("with_bill", (False, True))
def test_the_dispatcher_probe_tells_a_real_parser_with_bill_from_one_without(
        mp, fc, tmp_path, monkeypatch, with_bill):
    _dispatcher(tmp_path, monkeypatch, with_bill)
    assert mp._dispatcher_accepts_credits() is with_bill
    assert fc.dispatcher_accepts_credits() is with_bill


@pytest.mark.parametrize("with_bill", (False, True))
def test_bill_credits_help_exits_0_on_both_parsers_which_is_why_it_cannot_probe(
        tmp_path, monkeypatch, with_bill):
    import subprocess
    cmd = _dispatcher(tmp_path, monkeypatch, with_bill)
    r = subprocess.run(cmd + ["--bill", "credits", "--help"], capture_output=True, text=True)
    assert r.returncode == 0


def test_a_peek_then_the_dispatchers_decide_gets_credits_and_one_reservation(mp, fc, world):
    db, started, _ = world
    _seed_attempt(db, model=FABLE)
    _close_gate(fc, db)
    _open_window(fc)
    mp.review_retry_sweep(db=db)                         # mopsus: peek only
    assert started[0]["bill"] == "credits"
    ans = fc.decide("operator/sniff", 7, FABLE)           # the dispatcher: reserves
    assert ans["bill"] == "credits"
    assert sqlite3.connect(str(db)).execute(
        "SELECT COUNT(*) FROM credit_reservation").fetchone()[0] == 1


def test_an_unanswerable_decide_is_a_hold(mp, fc, world, tmp_path, monkeypatch):
    db, started, probes = world
    _seed_attempt(db, model=FABLE)
    _close_gate(fc, db)
    _open_window(fc)
    monkeypatch.setattr(mp, "BIN", tmp_path)             # no fleet-credits there
    mp.review_retry_sweep(db=db)
    assert started == [] and len(probes) == 1


def test_a_cycle_closes_an_ended_window(mp, fc, world, tmp_path):
    db, _, _ = world
    _open_window(fc)
    con = sqlite3.connect(str(db))
    con.execute("UPDATE credit_window SET ends_at=?, opened_at=?", (time.time() - 5,
                                                                    time.time() - 3600))
    con.commit()
    con.close()
    mp.credits_window_tick(db)
    assert [e["detail"]["by"] for e in _events(tmp_path, "credits-window-closed")] == ["expiry"]
