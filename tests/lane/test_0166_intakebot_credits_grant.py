import json
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_CREDITS = REPO_ROOT / "bin" / "fleet-credits"
FLEET_CONFIG = REPO_ROOT / "bin" / "fleet-config"
FLEET_EMIT = REPO_ROOT / "bin" / "fleet-emit"
FLEET_EVENTS = REPO_ROOT / "bin" / "fleet-events"
FLEET_PLAN = REPO_ROOT / "bin" / "fleet-plan"


@pytest.fixture
def fleet_env(tmp_path):
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    # fleet-credits writes its events beside FLEET_DB, so the db lives in the fleet dir
    db_path = fleet_dir / "fleet.db"
    env = os.environ.copy()
    env["FLEET_DB"] = str(db_path)
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    env["EUNOMIA_SESSION"] = "test-session"
    env.setdefault("USER", "operator")
    return env


def run_cli(cmd, env, *, tty=False):
    args = [sys.executable, str(cmd[0]), *[str(c) for c in cmd[1:]]]
    if not tty:
        return subprocess.run(
            args,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            args,
            env=env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
    finally:
        os.close(slave)
    try:
        stdout, stderr = proc.communicate(timeout=15)
    finally:
        os.close(master)
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def read_events(env, event_type=None):
    cmd = [FLEET_EVENTS, "--json"]
    if event_type:
        cmd.extend(["--type", event_type])
    res = run_cli(cmd, env)
    assert res.returncode == 0, res.stderr
    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


@pytest.mark.covers("D1")
def test_grant_and_revoke_require_tty(fleet_env):
    res_grant = run_cli(
        [FLEET_CREDITS, "grant", "partner-1", "--monthly-usd", "10"],
        fleet_env,
        tty=False,
    )
    assert res_grant.returncode == 1

    res_revoke = run_cli(
        [FLEET_CREDITS, "revoke", "partner-1"],
        fleet_env,
        tty=False,
    )
    assert res_revoke.returncode == 1


@pytest.mark.covers("D1")
def test_grant_validates_principal_and_cap(fleet_env):
    bad_principal = run_cli(
        [FLEET_CREDITS, "grant", "bad principal!", "--monthly-usd", "10"],
        fleet_env,
        tty=True,
    )
    assert bad_principal.returncode != 0

    over_cap = run_cli(
        [FLEET_CREDITS, "grant", "partner-1", "--monthly-usd", "30"],
        fleet_env,
        tty=True,
    )
    assert over_cap.returncode != 0


@pytest.mark.covers("D1", "D4")
def test_grant_replace_and_revoke_lifecycle_and_events(fleet_env):
    res1 = run_cli(
        [FLEET_CREDITS, "grant", "alice_01:dev", "--monthly-usd", "10", "--note", "first"],
        fleet_env,
        tty=True,
    )
    assert res1.returncode == 0, res1.stderr

    res2 = run_cli(
        [FLEET_CREDITS, "grant", "alice_01:dev", "--monthly-usd", "15", "--note", "updated"],
        fleet_env,
        tty=True,
    )
    assert res2.returncode == 0, res2.stderr

    given_events = read_events(fleet_env, "credits-grant-given")
    assert len(given_events) == 2
    assert given_events[-1]["detail"]["principal"] == "alice_01:dev"
    assert float(given_events[-1]["detail"]["monthly_usd"]) == 15.0

    # Revoking is idempotent and always succeeds on a terminal
    rev1 = run_cli([FLEET_CREDITS, "revoke", "alice_01:dev"], fleet_env, tty=True)
    assert rev1.returncode == 0, rev1.stderr

    rev2 = run_cli([FLEET_CREDITS, "revoke", "never-granted"], fleet_env, tty=True)
    assert rev2.returncode == 0, rev2.stderr

    revoked_events = read_events(fleet_env, "credits-grant-revoked")
    assert any(e["detail"].get("principal") == "alice_01:dev" for e in revoked_events)


@pytest.mark.covers("D1", "DoD2")
def test_status_json_fresh_db_and_intakebot_section(fleet_env):
    res = run_cli([FLEET_CREDITS, "status", "--json"], fleet_env)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert isinstance(data, dict)
    assert "intakebot" in data
    assert data.get("window") is None


@pytest.mark.covers("D2")
def test_decide_job_credits_no_grant_holds(fleet_env):
    res = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--job",
            "job-1",
            "--principal",
            "partner-a",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert res.returncode == 0, res.stderr
    ans = json.loads(res.stdout)
    assert set(ans.keys()) >= {"bill", "budget_usd", "reason", "window_ends", "grant"}
    assert ans["bill"] == "hold"
    assert ans["grant"] is False
    assert "no grant" in ans["reason"]


@pytest.mark.covers("D1", "D2")
def test_decide_job_credits_with_grant_and_peek(fleet_env):
    g = run_cli(
        [FLEET_CREDITS, "grant", "partner-a", "--monthly-usd", "12"],
        fleet_env,
        tty=True,
    )
    assert g.returncode == 0, g.stderr

    # --peek does not reserve budget
    peek = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--peek",
            "--job",
            "job-peek",
            "--principal",
            "partner-a",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert peek.returncode == 0, peek.stderr
    peek_ans = json.loads(peek.stdout)
    assert peek_ans["bill"] == "credits"
    assert peek_ans["grant"] is True
    assert float(peek_ans["budget_usd"]) == 5.0

    # Real decide-job reserves 5.0 (default per-job cap), leaving 7.0 on the 12.0 grant
    d1 = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--job",
            "job-1",
            "--principal",
            "partner-a",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert d1.returncode == 0, d1.stderr
    ans1 = json.loads(d1.stdout)
    assert ans1["bill"] == "credits"
    assert ans1["grant"] is True
    assert float(ans1["budget_usd"]) == 5.0

    # Second job reserves another 5.0, leaving 2.0 on the 12.0 grant
    d2 = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--job",
            "job-2",
            "--principal",
            "partner-a",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert d2.returncode == 0, d2.stderr
    ans2 = json.loads(d2.stdout)
    assert ans2["bill"] == "credits"
    assert float(ans2["budget_usd"]) == 5.0

    # Third job gets the remaining 2.0 (min of per-job 5.0 and grant remaining 2.0)
    d3 = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--job",
            "job-3",
            "--principal",
            "partner-a",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert d3.returncode == 0, d3.stderr
    ans3 = json.loads(d3.stdout)
    assert ans3["bill"] == "credits"
    assert float(ans3["budget_usd"]) == 2.0

    # Fourth job has 0 left on the grant -> hold with grant cap reason
    d4 = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--job",
            "job-4",
            "--principal",
            "partner-a",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert d4.returncode == 0, d4.stderr
    ans4 = json.loads(d4.stdout)
    assert ans4["bill"] == "hold"
    assert "grant cap" in ans4["reason"]


@pytest.mark.covers("D1", "D2")
def test_off_does_not_revoke_grants(fleet_env):
    g = run_cli(
        [FLEET_CREDITS, "grant", "partner-b", "--monthly-usd", "10"],
        fleet_env,
        tty=True,
    )
    assert g.returncode == 0, g.stderr

    off = run_cli([FLEET_CREDITS, "off"], fleet_env)
    assert off.returncode == 0, off.stderr

    d = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--job",
            "job-after-off",
            "--principal",
            "partner-b",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert d.returncode == 0, d.stderr
    ans = json.loads(d.stdout)
    assert ans["bill"] == "credits"
    assert ans["grant"] is True


@pytest.mark.covers("D2", "D4")
def test_record_job_settles_reservation_and_emits_event(fleet_env):
    run_cli(
        [FLEET_CREDITS, "grant", "partner-rec", "--monthly-usd", "10"],
        fleet_env,
        tty=True,
    )
    d1 = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--job",
            "job-r1",
            "--principal",
            "partner-rec",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert json.loads(d1.stdout)["bill"] == "credits"

    # Record $1.50 spend on job-r1 (freeing $3.50 of the $5.00 reservation)
    rec = run_cli(
        [
            FLEET_CREDITS,
            "record-job",
            "--job",
            "job-r1",
            "--principal",
            "partner-rec",
            "--model",
            "fable",
            "--usd",
            "1.5",
            "--session",
            "sess-1",
        ],
        fleet_env,
    )
    assert rec.returncode == 0, rec.stderr

    events = read_events(fleet_env, "credits-job-spent")
    assert len(events) == 1
    detail = events[0]["detail"]
    assert detail["job"] == "job-r1"
    assert detail["principal"] == "partner-rec"
    assert detail["model"] == "fable"
    assert float(detail["usd"]) == 1.5
    assert detail["billing"] == "grant"

    # Record without an open reservation still writes its row
    rec_unreserved = run_cli(
        [
            FLEET_CREDITS,
            "record-job",
            "--job",
            "job-unres",
            "--principal",
            "partner-rec",
            "--model",
            "fable",
            "--usd",
            "8.5",
            "--session",
            "sess-2",
        ],
        fleet_env,
    )
    assert rec_unreserved.returncode == 0, rec_unreserved.stderr

    # Cap-reached event emitted when grant cap ($10.0) is reached
    cap_events = read_events(fleet_env, "credits-cap-reached")
    assert len(cap_events) >= 1


@pytest.mark.covers("D2", "D3")
def test_config_keys_validation_and_kill_switch(fleet_env):
    for key, default_val in [
        ("credits_intakebot_job_usd", 5),
        ("credits_intakebot_monthly_usd", 25),
    ]:
        get_res = run_cli([FLEET_CONFIG, "get", key, "--json"], fleet_env)
        assert get_res.returncode == 0, get_res.stderr
        assert float(json.loads(get_res.stdout)["value"]) == float(default_val)

        # the monthly cap has no ceiling (ab880d9550); the per-job cap still does
        if key == "credits_intakebot_job_usd":
            bad_high = run_cli([FLEET_CONFIG, "set", key, "201"], fleet_env)
            assert bad_high.returncode == 1

        bad_low = run_cli([FLEET_CONFIG, "set", key, "-1"], fleet_env)
        assert bad_low.returncode == 1

    run_cli(
        [FLEET_CREDITS, "grant", "partner-kill", "--monthly-usd", "10"],
        fleet_env,
        tty=True,
    )

    # Setting credits_intakebot_monthly_usd to 0 is the kill switch for all intakebot credits spend
    kill = run_cli([FLEET_CONFIG, "set", "credits_intakebot_monthly_usd", "0"], fleet_env)
    assert kill.returncode == 0, kill.stderr

    d = run_cli(
        [
            FLEET_CREDITS,
            "decide-job",
            "--job",
            "job-killed",
            "--principal",
            "partner-kill",
            "--meter",
            "credits",
            "--model",
            "fable",
            "--json",
        ],
        fleet_env,
    )
    assert d.returncode == 0, d.stderr
    ans = json.loads(d.stdout)
    assert ans["bill"] == "hold"
    assert "intakebot monthly cap" in ans["reason"]


@pytest.mark.covers("D4")
def test_fleet_emit_and_events_accept_new_credit_event_types(fleet_env):
    cases = [
        ("credits-grant-given", {"principal": "p1", "monthly_usd": 10.0}),
        ("credits-grant-revoked", {"principal": "p1"}),
        (
            "credits-job-spent",
            {
                "job": "j1",
                "principal": "p1",
                "model": "fable",
                "usd": 1.25,
                "billing": "grant",
            },
        ),
    ]
    for ev_type, detail in cases:
        emit = run_cli(
            [FLEET_EMIT, ev_type, "--detail-json", json.dumps(detail)],
            fleet_env,
        )
        assert emit.returncode == 0, emit.stderr
        evs = read_events(fleet_env, ev_type)
        assert any(e["detail"] == detail for e in evs)


@pytest.mark.covers("D5")
def test_docs_credits_window_has_intakebot_jobs_section():
    doc = (REPO_ROOT / "docs" / "credits-window.md").read_text(encoding="utf-8")
    assert "intakebot jobs" in doc.lower()
    assert "decide-job" in doc
    assert "credits_intakebot_monthly_usd" in doc
    assert "grant" in doc


@pytest.mark.covers("DoD4")
@pytest.mark.skip(reason="asserts on the private fleet's plan history, which the public cut omits")
def test_fleet_plan_lint_passes(fleet_env):
    res = run_cli(
        [FLEET_PLAN, "lint", str(REPO_ROOT / "plans" / "0166-a-intakebot-job-may-bill-credits-by-grant.md")],
        fleet_env,
    )
    assert res.returncode == 0, res.stderr
