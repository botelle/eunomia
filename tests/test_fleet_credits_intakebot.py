"""Intakebot jobs billed by grant (plan 0166, ADR-0023 §6).

Same harness as test_fleet_credits.py: the CLI runs as a program against a temporary
fleet.db; grant, revoke and release go through a pseudo-terminal because there is no
override to skip the tty check.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from test_fleet_credits import (CLI, FABLE, OPUS, _load, BIN, close_gate, db, decide, env,
                                events, fc, run, tty)  # noqa: F401

P = "partner-1"


def djob(env, principal=P, meter="credits", model=FABLE, job="j1", peek=False):
    r = run(env, "decide-job", *(["--peek"] if peek else []), "--job", job, "--principal",
            principal, "--meter", meter, "--model", model, "--json")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def rjob(env, usd, job="j1", principal=P, model=FABLE, session="s1"):
    r = run(env, "record-job", "--job", job, "--principal", principal, "--model", model,
            "--usd", str(usd), "--session", session)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def grant(env, usd=10, principal=P):
    code, out = tty(env, "grant", principal, "--monthly-usd", str(usd))
    assert code == 0, out


def open_window(env):
    code, out = tty(env, "on", "--days", "1")
    assert code == 0, out


def test_no_grant_holds(env):
    a = djob(env)
    assert a["bill"] == "hold" and a["reason"] == "no grant" and a["grant"] is False
    assert set(a) == {"bill", "budget_usd", "reason", "window_ends", "grant"}


def test_grant_gives_credits_with_the_smallest_cap_as_budget(env):
    grant(env, 3.5)
    a = djob(env)
    assert a["bill"] == "credits" and a["budget_usd"] == 3.5 and a["grant"] is True
    assert a["window_ends"] is None
    row = db(env).execute("SELECT * FROM credit_job_reservation").fetchone()
    assert row["usd"] == 3.5 and row["grant_id"] is not None and row["window_id"] is None
    assert [e["detail"]["principal"] for e in events(env, "credits-grant-given")] == [P]


def test_per_job_cap_is_the_budget_when_smaller(env):
    grant(env, 20)
    assert djob(env)["budget_usd"] == 5


def test_a_granted_job_ignores_an_open_gate_and_a_window(fc, env):
    grant(env, 10)
    open_window(env)                       # window open, gate open (never closed)
    a = djob(env)
    assert a["bill"] == "credits" and a["grant"] is True


def test_credits_meter_never_answers_subscription(fc, env):
    close_gate(fc)
    open_window(env)
    assert djob(env, model=OPUS)["bill"] == "hold"        # no grant
    assert djob(env, principal="other", model=FABLE)["bill"] == "hold"
    grant(env, 10)
    assert djob(env, model=OPUS)["bill"] == "credits"      # no bucket, still by grant


def test_subscription_meter_follows_the_gate_rule(fc, env):
    assert djob(env, meter="subscription", model=OPUS)["bill"] == "subscription"
    assert djob(env, meter="subscription")["bill"] == "subscription"   # gate open
    close_gate(fc)
    assert djob(env, meter="subscription")["reason"] == "no window"
    open_window(env)
    a = djob(env, meter="subscription")
    assert a["bill"] == "credits" and a["budget_usd"] == 5 and a["grant"] is False
    assert a["window_ends"] is not None
    row = db(env).execute("SELECT * FROM credit_job_reservation").fetchone()
    assert row["grant_id"] is None and row["window_id"] is not None


def test_subscription_meter_ignores_grants(fc, env):
    grant(env, 10)
    close_gate(fc)
    assert djob(env, meter="subscription")["reason"] == "no window"


def test_peek_reserves_nothing(env):
    grant(env, 10)
    assert djob(env, peek=True)["bill"] == "credits"
    assert db(env).execute("SELECT COUNT(*) FROM credit_job_reservation").fetchone()[0] == 0


def test_grant_cap_runs_out(env):
    grant(env, 6)
    assert djob(env, job="a")["budget_usd"] == 5
    a = djob(env, job="b")                  # $1 left
    assert a["bill"] == "credits" and a["budget_usd"] == 1
    a = djob(env, job="c")
    assert a["bill"] == "hold" and a["reason"] == "grant cap"


def test_intakebot_monthly_zero_is_the_kill_switch(env):
    grant(env, 10)
    r = subprocess.run([sys.executable, str(BIN / "fleet-config"), "set",
                        "credits_intakebot_monthly_usd", "0"], env=env, capture_output=True,
                       text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    a = djob(env)
    assert a["bill"] == "hold" and a["reason"] == "intakebot monthly cap"


def test_a_review_and_a_job_race_for_the_last_dollars(fc, env):
    close_gate(fc)
    open_window(env)
    grant(env, 10)
    monthly = 100
    # leave exactly $3 of the month
    con = db(env)
    now = time.time()
    con.execute("INSERT INTO credit_spend (window_id, repo, pr, model, usd, session_id, "
                "created_at, updated_at) VALUES (NULL,'r',9,'m',?, 's', ?, ?)",
                (monthly - 3, now, now))
    con.commit()
    con.close()
    r = decide(env, pr=1)
    assert r["bill"] == "credits" and r["budget_usd"] == 3
    a = djob(env)
    assert a["bill"] == "hold" and a["reason"] == "monthly cap"


def test_a_job_reservation_makes_decide_hold(fc, env):
    close_gate(fc)
    open_window(env)
    grant(env, 10)
    con = db(env)
    now = time.time()
    con.execute("INSERT INTO credit_spend (window_id, repo, pr, model, usd, session_id, "
                "created_at, updated_at) VALUES (NULL,'r',9,'m',98, 's', ?, ?)", (now, now))
    con.commit()
    con.close()
    assert djob(env)["budget_usd"] == 2
    assert decide(env, pr=1)["bill"] == "hold"


def test_record_job_settles_and_emits(env):
    grant(env, 10)
    djob(env)
    row = rjob(env, 1.25)
    assert row["usd"] == 1.25
    con = db(env)
    res = con.execute("SELECT * FROM credit_job_reservation").fetchone()
    sp = con.execute("SELECT * FROM credit_job_spend").fetchone()
    assert res["settled_by"] == "record" and sp["usd"] == 1.25 and sp["grant_id"] == res["grant_id"]
    ev = events(env, "credits-job-spent")
    assert len(ev) == 1
    assert ev[0]["detail"] == {"job": "j1", "principal": P, "model": FABLE, "usd": 1.25,
                               "billing": "grant"}
    assert djob(env, job="j2")["budget_usd"] == 5


def test_record_job_without_a_reservation_still_writes(env):
    rjob(env, 2)
    sp = db(env).execute("SELECT * FROM credit_job_spend").fetchone()
    assert sp["usd"] == 2 and sp["grant_id"] is None and sp["window_id"] is None
    assert events(env, "credits-job-spent")[0]["detail"]["billing"] == "window"


def test_record_job_dry_run_writes_nothing(env):
    r = run(env, "record-job", "--dry-run", "--job", "j", "--principal", P, "--model", FABLE,
            "--usd", "1", "--session", "s")
    assert r.returncode == 0
    run(env, "status")                                    # creates the tables
    assert db(env).execute("SELECT COUNT(*) FROM credit_job_spend").fetchone()[0] == 0
    assert events(env, "credits-job-spent") == []


def test_record_job_cap_notices_once(fc, env):
    grant(env, 2)
    fc.record_job("j1", P, FABLE, 2.0, "s1")
    fc.record_job("j1", P, FABLE, 0.5, "s2")
    caps = events(env, "credits-cap-reached")
    assert [e["detail"]["cap"] for e in caps] == ["grant"]
    assert len(fc.pages) == 1


def test_intakebot_month_notice(fc, env):
    grant(env, 25)
    fc.record_job("j1", P, FABLE, 25.0, "s1")
    caps = events(env, "credits-cap-reached")
    assert sorted(e["detail"]["cap"] for e in caps) == ["grant", "intakebot-month"]
    keys = {r["key"] for r in db(env).execute("SELECT key FROM credit_notice")}
    assert any(k.startswith("grant:" + P) for k in keys)
    assert any(k.startswith("intakebot-month") for k in keys)


def test_overall_month_counts_job_spend_in_record(fc, env):
    fc.record_job("j1", P, FABLE, 4.0, "s1")
    fc.record("r", 1, FABLE, 1.0, "s")
    assert events(env, "credits-spent")[0]["detail"]["month_total"] == 5.0


def test_expiry_books_a_job_reservation_in_full_and_pages(fc, env):
    grant(env, 10)
    djob(env)
    con = db(env)
    old = time.time() - 5 * 3600
    con.execute("UPDATE credit_job_reservation SET created_at=?", (old,))
    con.commit()
    con.close()
    run(env, "status", "--json")
    con = db(env)
    res = con.execute("SELECT * FROM credit_job_reservation").fetchone()
    sp = con.execute("SELECT * FROM credit_job_spend").fetchone()
    assert res["settled_by"] == "expiry"
    assert sp["usd"] == 5 and sp["session_id"] == f"expired:{res['id']}"
    con.close()


def test_release_job_through_a_terminal(fc, env):
    grant(env, 10)
    djob(env)
    con = db(env)
    con.execute("UPDATE credit_job_reservation SET created_at=?", (time.time() - 5 * 3600,))
    con.commit()
    con.close()
    run(env, "status", "--json")
    sid = db(env).execute("SELECT id FROM credit_job_spend").fetchone()[0]
    assert run(env, "release", "--job", str(sid)).returncode == 1       # no terminal
    code, out = tty(env, "release", "--job", str(sid))
    assert code == 0, out
    assert db(env).execute("SELECT usd FROM credit_job_spend").fetchone()[0] == 0
    code, _ = tty(env, "release", "--job", str(sid))
    assert code == 1


def test_grant_and_revoke_refuse_without_a_terminal(env):
    r = run(env, "grant", P, "--monthly-usd", "5")
    assert r.returncode == 1 and "terminal" in r.stderr
    r = run(env, "revoke", P)
    assert r.returncode == 1 and "terminal" in r.stderr
    run(env, "status")                                    # creates the tables
    assert db(env).execute("SELECT COUNT(*) FROM credit_grant").fetchone()[0] == 0


def test_grant_validates_principal_and_cap(env):
    for bad in ("bad principal", "a/b", "x" * 129):
        code, _ = tty(env, "grant", bad, "--monthly-usd", "5")
        assert code == 2
    code, out = tty(env, "grant", P, "--monthly-usd", "26")      # above intakebot monthly 25
    assert code == 1 and "credits_intakebot_monthly_usd" in out
    code, _ = tty(env, "grant", P, "--monthly-usd", "-1")
    assert code == 2


def test_second_grant_replaces_the_cap_and_keeps_one_row(env):
    grant(env, 5)
    grant(env, 8)
    rows = db(env).execute("SELECT * FROM credit_grant").fetchall()
    assert len(rows) == 1 and rows[0]["monthly_usd"] == 8


def test_revoke_stops_new_spend_and_regrant_revives(env):
    grant(env, 10)
    djob(env)
    code, out = tty(env, "revoke", P)
    assert code == 0, out
    assert [e["detail"] for e in events(env, "credits-grant-revoked")] == [{"principal": P}]
    assert djob(env, job="j2")["reason"] == "no grant"
    code, out = tty(env, "revoke", P)                       # again: still succeeds
    assert code == 0 and len(events(env, "credits-grant-revoked")) == 1
    grant(env, 10)
    assert djob(env, job="j3")["bill"] == "credits"


def test_off_does_not_touch_grants(env):
    grant(env, 10)
    open_window(env)
    assert run(env, "off").returncode == 0
    assert djob(env)["bill"] == "credits"


def test_status_json_on_a_fresh_db(env):
    s = json.loads(run(env, "status", "--json").stdout)
    assert s["window"] is None and s["month_spent_usd"] == 0
    assert (s["credits_per_pr_usd"], s["credits_monthly_usd"], s["credits_intakebot_job_usd"],
            s["credits_intakebot_monthly_usd"]) == (50, 100, 5, 25)
    assert s["intakebot"]["grants"] == [] and s["intakebot"]["open_reservations"] == 0


def test_status_lists_grants_with_spend(env):
    grant(env, 10)
    djob(env)
    j = json.loads(run(env, "status", "--json").stdout)["intakebot"]
    assert j["grants"][0]["principal"] == P and j["grants"][0]["reserved_usd"] == 5
    assert j["month_reserved_usd"] == 5 and j["open_reservations"] == 1


def test_new_tables_are_stamped_and_events_registered(fc):
    con = fc._connect()
    for t in ("credit_grant", "credit_job_reservation", "credit_job_spend"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        assert {"created_at", "updated_at"} <= cols, t
    emit = _load("fleet_emit_t2", BIN / "fleet-emit")
    fev = _load("fleet_events_t2", BIN / "fleet-events")
    for k in ("credits-grant-given", "credits-grant-revoked", "credits-job-spent"):
        assert k in emit.EVENT_TYPES and k in fev.EVENT_TYPES


def test_the_intakebot_config_keys_validate(fc):
    for v in (fc.fconfig._validate_credits_intakebot_job_usd,
              fc.fconfig._validate_credits_intakebot_monthly_usd):
        assert v("5") == 5 and v(0) == 0 and v("200") == 200
        for bad in ("-1", "abc", True):
            with pytest.raises(ValueError):
                v(bad)
    # One job is one spend, so it keeps the ceiling; the month has none.
    with pytest.raises(ValueError):
        fc.fconfig._validate_credits_intakebot_job_usd("201")
    assert fc.fconfig._validate_credits_intakebot_monthly_usd("201") == 201
