"""The credits window and its ledger (plan 0146, ADR-0023).

`bin/fleet-credits` is run as a program against a temporary fleet.db, the way the
dispatcher runs it; the few checks that need to see a page load it in-process with
the page sender replaced. The owner-only commands run through a pseudo-terminal:
there is no override to skip the tty check, so the test cannot have one.
"""
import importlib.machinery
import importlib.util
import json
import os
import pty
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
CLI = str(BIN / "fleet-credits")
FABLE, OPUS = "claude-fable-5-1", "claude-opus-5-5"
REPO = "operator/sniff"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / ".fleet"
    d.mkdir()
    monkeypatch.setenv("FLEET_DB", str(d / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(d))
    for k in ("FLEET_CREDITS_PER_PR_USD", "FLEET_CREDITS_MONTHLY_USD", "FLEET_NTFY_URL",
              "FLEET_ANGELIA_URL"):
        monkeypatch.delenv(k, raising=False)
    return dict(os.environ)


@pytest.fixture
def fc(env, monkeypatch):
    mod = _load("fleet_credits_t", BIN / "fleet-credits")
    pages = []
    monkeypatch.setattr(mod, "NOTIFY", lambda title, body: pages.append((title, body)))
    mod.pages = pages
    return mod


def run(env, *args, stdin=subprocess.DEVNULL):
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                          env=env, stdin=stdin)


def decide(env, pr=1, model=FABLE, peek=False, repo=REPO):
    r = run(env, "decide", *(["--peek"] if peek else []), "--repo", repo, "--pr", str(pr),
            "--model", model, "--json")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def db(env):
    con = sqlite3.connect(env["FLEET_DB"])
    con.row_factory = sqlite3.Row
    return con


def events(env, kind=None):
    p = Path(env["EUNOMIA_FLEET_DIR"]) / "events.jsonl"
    out = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in out if kind is None or e["type"] == kind]


def close_gate(fc, scope="model:fable"):
    fc.fquota.close(scope, None, "stop", "ev-1", db=str(fc.db_path()))


def open_window(fc, days=1, opened_ago=0):
    """A window as the owner's `on` leaves it (that command needs a tty)."""
    con = fc._connect()
    t = time.time() - opened_ago
    wid = fc._insert(con, "credit_window", opened_at=t, ends_at=t + days * 86400, note=None)
    con.close()
    return wid


def seed_spend(fc, usd, repo="operator/other", pr=99, when=None, window_id=None):
    con = fc._connect()
    sid = fc._insert(con, "credit_spend", window_id=window_id, repo=repo, pr=pr, model=FABLE,
                     usd=usd, session_id="seed")
    if when is not None:
        con.execute("UPDATE credit_spend SET created_at=? WHERE id=?", (when, sid))
    con.close()
    return sid


def tty(env, *args):
    """Run the CLI with a pseudo-terminal as stdin/stdout. (exit code, output)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, CLI, *args], env)
    out = b""
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


# ------------------------------------------------------------------ the answer


def test_status_on_a_fresh_db(env):
    r = run(env, "status", "--json")
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert s["window"] is None and s["month_spent_usd"] == 0 and s["prs"] == []
    assert s["credits_per_pr_usd"] == 50 and s["credits_monthly_usd"] == 100


def test_a_model_with_no_bucket_gets_the_subscription(fc, env):
    close_gate(fc)
    open_window(fc)
    assert decide(env, model=OPUS)["bill"] == "subscription"


def test_an_open_gate_gets_the_subscription_even_inside_a_window(fc, env):
    open_window(fc)
    assert decide(env)["bill"] == "subscription"


def test_a_closed_gate_inside_a_window_gets_credits_and_reserves(fc, env):
    close_gate(fc)
    wid = open_window(fc)
    a = decide(env)
    assert a["bill"] == "credits" and a["budget_usd"] == 50 and a["window_ends"]
    rows = db(env).execute("SELECT * FROM credit_reservation").fetchall()
    assert len(rows) == 1 and rows[0]["usd"] == 50 and rows[0]["window_id"] == wid


def test_hold_reasons(fc, env):
    close_gate(fc)
    assert decide(env)["reason"] == "no window"
    wid = open_window(fc)
    con = db(env)
    con.execute("UPDATE credit_window SET ends_at=?, opened_at=? WHERE id=?",
                (time.time() - 5, time.time() - 3600, wid))
    con.commit()
    a = decide(env)
    assert a["bill"] == "hold" and a["reason"] == "window ended" and a["budget_usd"] is None
    assert db(env).execute("SELECT closed_at FROM credit_window").fetchone()[0] is not None
    open_window(fc)
    seed_spend(fc, 50, repo=REPO, pr=1)
    assert decide(env)["reason"] == "per-PR cap"
    seed_spend(fc, 50)
    assert decide(env, pr=2)["reason"] == "monthly cap"


def test_budget_is_the_smaller_remainder_and_below_a_dollar_is_a_hold(fc, env):
    close_gate(fc)
    open_window(fc)
    seed_spend(fc, 20, repo=REPO, pr=1)                  # per-PR left 30
    seed_spend(fc, 60)                                   # month left 20
    assert decide(env, peek=True)["budget_usd"] == 20
    seed_spend(fc, 19.5)                                 # month left 0.5
    a = decide(env, peek=True)
    assert a["bill"] == "hold" and a["reason"] == "monthly cap"


def test_a_cap_of_zero_holds(fc, env):
    close_gate(fc)
    open_window(fc)
    assert run(env, "decide", "--peek", "--repo", REPO, "--pr", "1", "--model", FABLE,
               "--json").returncode == 0
    fc.fconfig.set_value(fc.fconfig.connect(env["FLEET_DB"]), "credits_per_pr_usd", "0")
    assert decide(env)["reason"] == "per-PR cap"


def test_decide_exits_1_when_it_cannot_read_the_database(env, tmp_path):
    bad = dict(env, FLEET_DB=str(tmp_path))              # a directory
    r = run(bad, "decide", "--repo", REPO, "--pr", "1", "--model", FABLE, "--json")
    assert r.returncode == 1


def test_spend_in_a_previous_utc_month_does_not_count(fc, env):
    close_gate(fc)
    open_window(fc)
    lo, _, _ = fc._month_bounds(time.time())
    seed_spend(fc, 100, repo=REPO, pr=1, when=lo - 1)
    assert decide(env, peek=True)["budget_usd"] == 50
    s = json.loads(run(env, "status", "--json").stdout)
    assert s["month_spent_usd"] == 0 and s["prs"] == []


# ------------------------------------------------------------------- reserving


def test_peek_answers_like_decide_and_changes_nothing(fc, env):
    close_gate(fc)
    open_window(fc)
    before = events(env)
    peeked = decide(env, peek=True)
    assert db(env).execute("SELECT COUNT(*) FROM credit_reservation").fetchone()[0] == 0
    assert events(env) == before
    assert decide(env) == peeked
    assert db(env).execute("SELECT COUNT(*) FROM credit_reservation").fetchone()[0] == 1


def test_peek_never_emits_credits_refused(fc, env):
    close_gate(fc)
    open_window(fc)
    seed_spend(fc, 50, repo=REPO, pr=1)
    assert decide(env, peek=True)["bill"] == "hold"
    assert events(env, "credits-refused") == []


def test_five_concurrent_decides_grant_at_most_what_is_left(fc, env):
    close_gate(fc)
    open_window(fc)
    seed_spend(fc, 70)                                   # $30 of the month left
    procs = [subprocess.Popen(
        [sys.executable, CLI, "decide", "--repo", REPO, "--pr", str(n), "--model", FABLE,
         "--json"], stdout=subprocess.PIPE, text=True, env=env) for n in range(1, 6)]
    answers = [json.loads(p.communicate()[0]) for p in procs]
    granted = sum(a["budget_usd"] or 0 for a in answers)
    assert granted <= 30
    reserved = db(env).execute("SELECT SUM(usd) FROM credit_reservation").fetchone()[0]
    assert reserved == granted and sum(a["bill"] == "credits" for a in answers) == 1


def test_record_settles_its_reservation_and_frees_the_difference(fc, env):
    close_gate(fc)
    open_window(fc)
    assert decide(env)["budget_usd"] == 50
    assert decide(env, pr=2)["budget_usd"] == 50
    assert decide(env, pr=3)["budget_usd"] is None       # month: 100 all reserved
    r = run(env, "record", "--repo", REPO, "--pr", "1", "--model", FABLE, "--usd", "4.25",
            "--session", "s1")
    assert r.returncode == 0, r.stderr
    row = db(env).execute("SELECT * FROM credit_reservation WHERE pr=1").fetchone()
    assert row["settled_by"] == "record"
    assert decide(env, pr=3)["budget_usd"] == 45.75      # 100 - 50 - 4.25
    spent = events(env, "credits-spent")
    assert [e["detail"]["usd"] for e in spent] == [4.25]
    assert set(spent[0]["detail"]) == {"window_id", "repo", "pr", "model", "usd", "pr_total",
                                       "month_total"}


def test_record_settles_the_newest_reservation_and_keeps_its_window(fc, env):
    close_gate(fc)
    w1 = open_window(fc)
    con = fc._connect()
    old = fc._insert(con, "credit_reservation", window_id=w1, repo=REPO, pr=1, model=FABLE,
                     usd=10)
    w2 = open_window(fc)
    new = fc._insert(con, "credit_reservation", window_id=w2, repo=REPO, pr=1, model=FABLE,
                     usd=10)
    con.execute("UPDATE credit_window SET closed_at=? WHERE id=?", (time.time(), w2))
    con.close()
    assert run(env, "record", "--repo", REPO, "--pr", "1", "--model", FABLE, "--usd", "3",
               "--session", "s").returncode == 0
    c = db(env)
    assert c.execute("SELECT settled_at FROM credit_reservation WHERE id=?",
                     (old,)).fetchone()[0] is None
    assert c.execute("SELECT settled_by FROM credit_reservation WHERE id=?",
                     (new,)).fetchone()[0] == "record"
    assert c.execute("SELECT window_id FROM credit_spend").fetchone()[0] == w2


def test_record_with_no_reservation_and_no_window_keeps_the_spend(env):
    r = run(env, "record", "--repo", REPO, "--pr", "1", "--model", FABLE, "--usd", "2",
            "--session", "s")
    assert r.returncode == 0, r.stderr
    row = db(env).execute("SELECT * FROM credit_spend").fetchone()
    assert row["window_id"] is None and row["usd"] == 2


def test_record_dry_run_writes_nothing(fc, env):
    close_gate(fc)
    open_window(fc)
    decide(env)
    tables = ("credit_spend", "credit_reservation", "credit_notice", "credit_window")
    snap = lambda: ([list(map(tuple, db(env).execute(f"SELECT * FROM {t}"))) for t in tables],
                    events(env))
    before = snap()
    r = run(env, "record", "--dry-run", "--repo", REPO, "--pr", "1", "--model", FABLE,
            "--usd", "5", "--session", "s")
    assert r.returncode == 0 and json.loads(r.stdout)["usd"] == 5
    assert snap() == before
    assert run(env, "record", "--dry-run", "--repo", REPO, "--pr", "1", "--model", FABLE,
               "--usd", "-1", "--session", "s").returncode == 2


def test_record_past_a_cap_pages_once(fc, env):
    close_gate(fc)
    wid = open_window(fc)
    for session, usd in (("a", 50.0), ("b", 10.0)):
        con = fc._connect()
        fc._insert(con, "credit_reservation", window_id=wid, repo=REPO, pr=1, model=FABLE,
                   usd=5)
        con.close()
        fc.record(REPO, 1, FABLE, usd, session)
    caps = events(env, "credits-cap-reached")
    assert [e["detail"]["cap"] for e in caps] == ["pr"]
    assert set(caps[0]["detail"]) == {"window_id", "cap", "repo", "pr", "total", "limit"}
    assert len([p for p in fc.pages if "cap reached" in p[1]]) == 1


# ------------------------------------------------------------------- expiry


def test_an_old_reservation_is_booked_in_full_and_stays_counted(fc, env):
    close_gate(fc)
    open_window(fc)
    assert decide(env)["budget_usd"] == 50
    con = db(env)
    con.execute("UPDATE credit_reservation SET created_at=created_at-5*3600")
    con.commit()
    assert decide(env, pr=2, peek=True)["budget_usd"] == 50   # month: 100 - 50 booked
    s = json.loads(run(env, "status", "--json").stdout)
    assert s["month_spent_usd"] == 50
    c = db(env)
    res = c.execute("SELECT * FROM credit_reservation").fetchone()
    assert res["settled_by"] == "expiry"
    spend = c.execute("SELECT * FROM credit_spend").fetchall()
    assert len(spend) == 1 and spend[0]["usd"] == 50
    assert spend[0]["session_id"] == f"expired:{res['id']}"
    ev = events(env, "credits-reservation-expired")
    assert len(ev) == 1
    assert set(ev[0]["detail"]) == {"window_id", "reservation_id", "repo", "pr", "model", "usd"}
    # a later record does not settle it and writes its own row
    assert run(env, "record", "--repo", REPO, "--pr", "1", "--model", FABLE, "--usd", "3",
               "--session", "late").returncode == 0
    assert db(env).execute("SELECT COUNT(*) FROM credit_spend").fetchone()[0] == 2
    assert db(env).execute(
        "SELECT settled_by FROM credit_reservation").fetchone()[0] == "expiry"


def test_expiry_pages(fc, env):
    close_gate(fc)
    open_window(fc)
    decide(env)
    con = db(env)
    con.execute("UPDATE credit_reservation SET created_at=created_at-5*3600")
    con.commit()
    fc.main(["status"])
    assert len(fc.pages) == 1 and "never settled" in fc.pages[0][1]


def test_release_zeroes_the_booked_row_through_a_terminal(fc, env):
    close_gate(fc)
    open_window(fc)
    decide(env)
    con = db(env)
    con.execute("UPDATE credit_reservation SET created_at=created_at-5*3600")
    con.commit()
    run(env, "status")
    rid = db(env).execute("SELECT id FROM credit_reservation").fetchone()[0]
    assert run(env, "release", str(rid)).returncode == 1       # no tty: refused
    code, out = tty(env, "release", str(rid))
    assert code == 0, out
    c = db(env)
    assert c.execute("SELECT usd FROM credit_spend").fetchone()[0] == 0
    rel = c.execute("SELECT * FROM credit_release").fetchone()
    assert rel["old_usd"] == 50
    assert tty(env, "release", str(rid))[0] == 1               # nothing left to release


def test_the_window_ending_is_closed_by_status_and_paged_once(fc, env):
    wid = open_window(fc)
    con = db(env)
    con.execute("UPDATE credit_window SET ends_at=?, opened_at=? WHERE id=?",
                (time.time() - 5, time.time() - 3600, wid))
    con.commit()
    fc.main(["status"])
    fc.main(["status"])
    closed = events(env, "credits-window-closed")
    assert [e["detail"] for e in closed] == [{"window_id": wid, "by": "expiry"}]
    assert len(fc.pages) == 1


def test_credits_refused_is_emitted_once_per_repo_pr_and_reason(fc, env):
    close_gate(fc)
    wid = open_window(fc)
    seed_spend(fc, 50, repo=REPO, pr=1)
    for _ in range(3):
        assert decide(env)["bill"] == "hold"
    ev = events(env, "credits-refused")
    assert [e["detail"] for e in ev] == [{"repo": REPO, "pr": 1, "reason": "per-PR cap"}]
    decide(env, pr=2, repo="operator/other2")             # a different PR is its own notice
    assert db(env).execute("SELECT COUNT(*) FROM credit_notice").fetchone()[0] == 1
    assert db(env).execute("SELECT window_id FROM credit_notice").fetchone()[0] == wid


# ------------------------------------------------------------------ the owner


def test_on_is_refused_without_a_terminal(env):
    r = run(env, "on", "--days", "1")
    assert r.returncode == 1 and "terminal" in r.stderr
    assert not Path(env["FLEET_DB"]).exists() or db(env).execute(
        "SELECT COUNT(*) FROM credit_window").fetchone()[0] == 0


@pytest.mark.parametrize("days", ["0", "8", "1.5", "abc", "-1"])
def test_on_refuses_a_bad_day_count(env, days):
    code, _ = tty(env, "on", "--days", days)
    assert code == 2


def test_on_through_a_terminal_opens_a_window(env):
    code, out = tty(env, "on", "--days", "2", "--note", "fable week")
    assert code == 0, out
    w = db(env).execute("SELECT * FROM credit_window").fetchone()
    assert abs((w["ends_at"] - w["opened_at"]) - 2 * 86400) < 5 and w["note"] == "fable week"
    ev = events(env, "credits-window-opened")
    assert len(ev) == 1 and set(ev[0]["detail"]) == {"window_id", "ends_at", "days"}


def test_a_repeated_on_never_passes_seven_days_from_the_first_open(fc, env):
    wid = open_window(fc, days=7, opened_ago=6 * 86400 + 3600)   # opened 6d1h ago, 23h left
    code, out = tty(env, "on", "--days", "7")
    assert code == 0, out
    w = db(env).execute("SELECT * FROM credit_window").fetchone()
    assert w["id"] == wid and w["ends_at"] <= w["opened_at"] + 7 * 86400 + 1
    assert db(env).execute("SELECT COUNT(*) FROM credit_window").fetchone()[0] == 1


def test_off_with_no_window_exits_0_and_says_what_is_open(fc, env):
    r = run(env, "off")
    assert r.returncode == 0 and "no window" in r.stdout
    close_gate(fc)
    open_window(fc)
    decide(env)
    r = run(env, "off")
    assert r.returncode == 0 and "1 reservation(s) still open, $50.00" in r.stdout
    ev = events(env, "credits-window-closed")
    assert ev[-1]["detail"]["by"] == "owner"
    assert decide(env)["reason"] == "no window"


def test_every_table_has_created_at_and_updated_at(fc):
    con = fc._connect()
    for t in ("credit_window", "credit_reservation", "credit_spend", "credit_notice",
              "credit_release"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        assert {"created_at", "updated_at"} <= cols, t


def test_the_events_are_registered(fc):
    emit = _load("fleet_emit_t", BIN / "fleet-emit")
    fev = _load("fleet_events_t", BIN / "fleet-events")
    for k in ("credits-window-opened", "credits-window-closed", "credits-spent",
              "credits-cap-reached", "credits-reservation-expired", "credits-refused"):
        assert k in emit.EVENT_TYPES and k in fev.EVENT_TYPES


def test_the_config_keys_validate(fc):
    v = fc.fconfig._validate_credits_per_pr_usd
    assert v("50") == 50 and v(12.5) == 12.5 and v(0) == 0 and v("200") == 200
    for bad in ("-1", "201", "abc", True, "1e3", ""):
        with pytest.raises(ValueError):
            v(bad)


def test_the_monthly_caps_have_no_ceiling(fc):
    # The owner sets the month; the prepaid Console balance bounds it. The
    # per-PR and per-job keys keep 200 because each becomes one review's budget.
    for v in (fc.fconfig._validate_credits_monthly_usd,
              fc.fconfig._validate_credits_intakebot_monthly_usd):
        assert v("370") == 370 and v(1000.5) == 1000.5 and v("0") == 0
        for bad in ("-1", "abc", True, "1e3", ""):
            with pytest.raises(ValueError):
                v(bad)
    with pytest.raises(ValueError):
        fc.fconfig._validate_credits_intakebot_job_usd("201")
