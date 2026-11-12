"""Recovery follows the stopped account (plan 0184).

A quota stop and a review stop each name a model or a runner, never "the
fleet" — and today only Claude's own session/weekly/model:fable path reads
that. A Codex, Gemini or Muse stop closed nothing of its own, and
`docs/quota-gate.md` D1 says plainly: "mopsus probes no vendor gate" — that
stays true here too: `~/agent/dispatch-review.py --probe-limit` is Claude-only
end to end (it fetches a Claude credential and runs the `claude` CLI no matter
what `--model` names), so a vendor model string handed to it would run under
the REVIEWER's own Claude account, never the operator's vendor one. This file
exercises the two pieces that change: closing the WHOLE vendor account a
stop's own runner or model spends (`_account_for_lease`, `_scope_for_model`),
and confirming `_gate_probe_model` never chooses a probe for a `vendor:<account>`
gate, and retaining each account's own meter reading time so one account's
snapshot can never gate another's transition (`fleet-quota`'s
`read_snapshot`/`observe`).

Every case here runs with Claude's own `session` gate ALSO closed, the
scenario the plan names: Claude exhausted, one vendor's account stopped and
recovered on its own.
"""
import importlib.machinery
import importlib.util
import json
import sqlite3

import pytest

from test_fleet_quota import _ago, _ts
from test_mopsus_review_retry import (
    _seed_attempt,
    _wire,  # noqa: F401  (autouse fixture — EUNOMIA_FLEET_DIR, notify, tokens)
    BIN,
)

QUOTA_REASON = ("session ended on a backend error before any work: API 429 after 2s — "
                "rate limited")


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def mp():
    return _load("mopsus_vendor_recovery", BIN / "mopsus")


def _dbpath(tmp_path):
    p = tmp_path / ".fleet" / "fleet.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _seed_dispatch_phase(db, lease, runner, phase="implementer-start", seq=1):
    """A bare `dispatch_phase` row naming `runner` — the one field this plan
    reads off it (`_runner_for_lease`). `implementer-start` is `_spawn_session`'s
    own phase (bin/orchestrator), written for every implementer regardless of
    vendor, so it is the one place a lease's runner is ever recorded."""
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE IF NOT EXISTS dispatch_phase (
          lease TEXT, seq INTEGER, ts TEXT, phase TEXT, pr INTEGER,
          rc INTEGER, timed_out INTEGER, seconds INTEGER,
          peak_rss_mb INTEGER, orch_rss_mb INTEGER, rss_samples INTEGER,
          detail_json TEXT, source TEXT, line INTEGER,
          PRIMARY KEY (source, line))""")
    con.execute("INSERT INTO dispatch_phase VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (lease, seq, "2026-09-13T00:00:00Z", phase, None,
                None, None, None, None, None, None,
                json.dumps({"runner": runner}), f"{lease}.log", seq))
    con.commit()
    con.close()


# --------------------------------------------------------------- _scope_for_model


def test_a_claude_bucket_model_still_resolves_the_old_way(mp):
    assert mp._scope_for_model("claude-fable-5-1") == "model:fable"


def test_an_unregistered_model_resolves_nothing(mp):
    assert mp._scope_for_model("not-a-real-model") is None


@pytest.mark.parametrize("model,scope", [
    ("gpt-6-astra", "vendor:codex"),
    ("gemini-3.8-flash-high", "vendor:gemini"),
    ("muse-wonder-1.0", "vendor:muse"),
])
def test_a_local_cli_vendor_model_resolves_its_whole_account(mp, model, scope):
    assert mp._scope_for_model(model) == scope


def test_a_model_with_its_own_limit_resolves_that_limits_gate(mp):
    # argon's weekly limit is its own gate (fleet-quota MODEL_LIMITS); a stopped
    # argon run must not hold flash seats behind the whole gemini account
    assert mp._scope_for_model("gemini-4-argon-high") == "vendor:gemini-argon"



def test_a_hosted_runner_of_a_vendor_family_is_not_the_vendor_account(mp):
    """`hosted-muse-spark-open` is `family="muse"` but spends an OpenRouter
    API key, never the muse subscription (needs is not `local_cli`)."""
    assert mp._scope_for_model("meta/muse-spark-open-20261015") is None


# ------------------------------------------------------- _runner_for_lease etc.


def test_runner_for_lease_reads_the_last_implementer_start(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_dispatch_phase(db, "L1", "sub-codex", seq=1)
    _seed_dispatch_phase(db, "L1", "sub-antigravity", seq=2)   # a respawn override
    assert mp._runner_for_lease("L1", db=str(db)) == "sub-antigravity"


def test_runner_for_lease_is_none_with_no_implementer_start(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_dispatch_phase(db, "L1", "sub-codex", phase="start")
    assert mp._runner_for_lease("L1", db=str(db)) is None


def test_runner_for_lease_is_none_with_no_db_at_all(mp, tmp_path):
    assert mp._runner_for_lease("L1", db=str(tmp_path / "nope.db")) is None


@pytest.mark.parametrize("runner,account", [
    ("sub-codex", "codex"), ("sub-antigravity", "gemini"), ("sub-muse", "muse"),
])
def test_account_for_lease_resolves_each_vendor(mp, tmp_path, runner, account):
    db = _dbpath(tmp_path)
    _seed_dispatch_phase(db, "L1", runner)
    assert mp._account_for_lease("L1", db=str(db)) == account


def test_account_for_lease_resolves_claude(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_dispatch_phase(db, "L1", "sub-sonnet")
    assert mp._account_for_lease("L1", db=str(db)) == mp.fquota.CLAUDE_ACCOUNT


def test_account_for_lease_is_none_for_an_unregistered_runner(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_dispatch_phase(db, "L1", "renamed-away-runner")
    assert mp._account_for_lease("L1", db=str(db)) is None


# --------------------------------------------------------- close_gate_for_failure


def test_codex_gemini_and_muse_stops_each_close_their_own_account_while_claude_is_exhausted(
        mp, tmp_path):
    """DoD: Codex, Gemini and Muse stops, Claude's own gate ALSO closed —
    each closes its own account and none of the others'."""
    db = _dbpath(tmp_path)
    assert mp.fquota.close("session", "2026-09-13T07:10:00Z", "stop", "claude-lease",
                           db=str(db))
    cases = [("codex-lease", "sub-codex", "vendor:codex"),
             ("gemini-lease", "sub-antigravity", "vendor:gemini"),
             ("muse-lease", "sub-muse", "vendor:muse")]
    for lease, runner, scope in cases:
        _seed_dispatch_phase(db, lease, runner)
        row = {"lease": lease, "class": "quota", "failure_reason": QUOTA_REASON,
              "current_phase_ts": "2026-09-13T06:00:00Z"}
        assert mp.close_gate_for_failure(row, db=str(db)) == scope
        g = mp.fquota.gate(scope, str(db))
        assert g["state"] == "closed" and g["closed_by"] == "stop" and g["evidence"] == lease
    # the Claude gate this scenario started with is untouched by any of them
    assert mp.fquota.gate("session", str(db))["state"] == "closed"
    assert mp.fquota.gate("session", str(db))["evidence"] == "claude-lease"


def test_a_stop_with_no_recorded_runner_still_falls_back_to_session_or_weekly(
        mp, tmp_path):
    """Every stop this suite recorded before this plan names no runner at
    all — the existing, unchanged guess."""
    db = _dbpath(tmp_path)
    row = {"lease": "old-lease", "class": "quota", "failure_reason": QUOTA_REASON,
          "current_phase_ts": "2026-09-13T06:00:00Z"}
    assert mp.close_gate_for_failure(row, db=str(db)) == "session"


def test_a_claude_runner_stop_also_falls_back_unchanged(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_dispatch_phase(db, "L1", "sub-sonnet")
    reason = QUOTA_REASON.replace("rate limited", "You've hit your weekly limit")
    row = {"lease": "L1", "class": "quota", "failure_reason": reason,
          "current_phase_ts": "2026-09-13T06:00:00Z"}
    assert mp.close_gate_for_failure(row, db=str(db)) == "weekly"


def test_a_credential_stop_is_unaffected_by_runner_resolution(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_dispatch_phase(db, "L1", "sub-codex")
    row = {"lease": "L1", "class": "credential",
          "failure_reason": mp.orch.CREDENTIAL_REJECTED + ":  nope",
          "current_phase_ts": "2026-09-13T06:00:00Z"}
    assert mp.close_gate_for_failure(row, db=str(db)) == "credential"


# -------------------------------------------------------------- review_stop_sweep


def test_a_non_claude_review_stop_closes_its_whole_vendor_account(mp, tmp_path):
    dbpath = _dbpath(tmp_path)
    _seed_attempt(dbpath, model="gpt-6-astra", outcome="quota")
    mp.review_stop_sweep(db=str(dbpath))
    g = mp.fquota.gate("vendor:codex", str(dbpath))
    assert g is not None and g["state"] == "closed"
    assert g["evidence"] == "review_attempt:1"


def test_a_review_stop_whose_model_has_no_bucket_or_vendor_still_closes_nothing(
        mp, tmp_path):
    dbpath = _dbpath(tmp_path)
    _seed_attempt(dbpath, model="claude-sonnet-5", outcome="quota")
    mp.review_stop_sweep(db=str(dbpath))
    assert mp.fquota.gates(str(dbpath)) == []


# -------------------------------------------------------------- _gate_probe_model


def test_probe_model_for_a_vendor_gate_is_always_none_even_with_matching_evidence(
        mp, tmp_path):
    """`run_probe` only ever reaches `~/agent/dispatch-review.py --probe-limit`,
    and that call is Claude-only end to end — so even a STOPPED review_attempt
    naming Gemini's own model must not be read as a probe for `vendor:gemini`:
    probing it there would run under the REVIEWER's Claude credential, not
    Gemini's account (docs/quota-gate.md D1)."""
    dbpath = _dbpath(tmp_path)
    _seed_attempt(dbpath, model="gemini-4-argon-high", outcome="quota")
    mp.fquota.close("vendor:gemini", None, "stop", "review_attempt:1", db=str(dbpath))
    g = mp.fquota.gate("vendor:gemini", str(dbpath))
    assert mp._gate_probe_model(g, db=str(dbpath)) is None


def test_probe_model_for_a_vendor_gate_is_none_with_no_evidence_either(mp, tmp_path):
    """Every vendor account's gate before this plan — closed by the meter
    alone, with no review_attempt evidence at all — resolves no probe model,
    exactly as one with evidence: a `vendor:<account>` gate opens by the meter
    only."""
    dbpath = _dbpath(tmp_path)
    mp.fquota.close("vendor:codex", None, "meter", "meter", db=str(dbpath))
    g = mp.fquota.gate("vendor:codex", str(dbpath))
    assert mp._gate_probe_model(g, db=str(dbpath)) is None


def test_probe_model_never_crosses_vendor_evidence_from_a_different_account(
        mp, tmp_path):
    """A `review_attempt` quota row naming Muse's model must never be read as
    evidence for Codex's gate either — moot once vendor gates are never
    probed at all, but kept to pin that nothing about this changed."""
    dbpath = _dbpath(tmp_path)
    _seed_attempt(dbpath, model="muse-wonder-1.0", outcome="quota")
    mp.fquota.close("vendor:codex", None, "meter", "meter", db=str(dbpath))
    g = mp.fquota.gate("vendor:codex", str(dbpath))
    assert mp._gate_probe_model(g, db=str(dbpath)) is None


def test_probe_model_for_credential_and_holding_scopes_is_unchanged(mp, tmp_path):
    assert mp._gate_probe_model({"scope": "credential", "evidence": "x"}) is None
    assert mp._gate_probe_model({"scope": "credential:venture", "evidence": "x"}) is None
    assert mp._gate_probe_model({"scope": "session", "evidence": "x"}) == mp.QUOTA_PROBE_MODEL


# -------------------------------------------------- a probe clears only its gate


def test_a_vendor_probes_reset_opens_only_its_own_account(mp, tmp_path, monkeypatch):
    """Boundary: do not reopen one account from another account's probe.
    `_gate_probe_model` never builds this payload for a vendor gate anymore
    (above), but `_cmd_review_lock_run` is exercised directly here so the
    isolation holds regardless of what chose the model."""
    dbpath = _dbpath(tmp_path)
    assert mp.fquota.close("vendor:codex", None, "stop", "lease-codex", db=str(dbpath))
    assert mp.fquota.close("vendor:gemini", None, "stop", "lease-gemini", db=str(dbpath))
    stub = tmp_path / "probe.sh"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setattr(mp.orch, "review_command", lambda: [str(stub)])
    payload = {"model": "gpt-6-astra", "gate": "vendor:codex"}
    fd = mp.os.open(str(tmp_path / "lockfd"), mp.os.O_CREAT | mp.os.O_RDWR)
    mp._cmd_review_lock_run([str(fd), json.dumps(payload), str(dbpath)])
    assert mp.fquota.gate("vendor:codex", str(dbpath))["state"] == "open"
    assert mp.fquota.gate("vendor:gemini", str(dbpath))["state"] == "closed"


# ------------------------------------------- fleet-quota: per-account timestamps


def _snapshot_per_account(tmp_path, **accounts):
    """accounts: label -> (as_of datetime, {quota key: percent})."""
    payload = {"generatedAt": _ts(_ago(0)), "accounts": {}}
    for label, (as_of, quotas) in accounts.items():
        payload["accounts"][label] = {
            "dataAsOf": _ts(as_of),
            "quotas": {k: {"percent": v, "resetsAt": "2026-11-05T00:00:00Z"}
                      for k, v in quotas.items()},
        }
    (tmp_path / "latest.json").write_text(json.dumps(payload))


@pytest.fixture
def fqmod():
    mod = _load("fleet_quota_vendor_recovery", BIN / "fleet-quota")
    mod.NOTIFY = lambda title, body: True   # local-only; no angelia/ntfy call
    return mod


def test_a_fresher_claude_reading_opens_session_despite_an_older_vendor_snapshot(
        fqmod, tmp_path, monkeypatch):
    """DoD: unequal timestamps. `session` closed 10 minutes ago; Claude's OWN
    reading is 2 minutes old (after the close) and shows it clear — codex's
    reading, in the SAME snapshot, is older (25 minutes, still fresh) and
    must not hold session to ITS time: 25 minutes ago is before the close,
    and the old shared-minimum code read that as "too early to open"."""
    monkeypatch.setenv("FLEET_QUOTA_LATEST", str(tmp_path / "latest.json"))
    monkeypatch.delenv("FLEET_QUOTA_ACCOUNT", raising=False)
    assert fqmod.close("session", None, "stop", "L1", at=_ts(_ago(10)))
    _snapshot_per_account(tmp_path, claude=(_ago(2), {"session": 5}),
                          codex=(_ago(25), {"session": 10}))
    assert fqmod.observe() == [("session", "open")]
    assert fqmod.gate("session")["state"] == "open"


def test_a_stale_vendor_reading_does_not_block_a_fresh_claude_close(
        fqmod, tmp_path, monkeypatch):
    """The reverse direction: codex's reading is 29 minutes old (still just
    fresh) while claude's own is 1 minute old and at 100 — the close must use
    claude's OWN time, not a shared minimum dragged down by codex's."""
    monkeypatch.setenv("FLEET_QUOTA_LATEST", str(tmp_path / "latest.json"))
    monkeypatch.delenv("FLEET_QUOTA_ACCOUNT", raising=False)
    # One reading time, used for both the snapshot and the expectation:
    # calling `_ago(1)` twice straddles a second boundary now and then, and
    # the second-resolution strings then differ by one (#1451, 2026-11-09).
    claude_at = _ago(1)
    _snapshot_per_account(tmp_path, claude=(claude_at, {"session": 100}),
                          codex=(_ago(29), {"session": 10}))
    assert fqmod.observe() == [("session", "closed")]
    g = fqmod.gate("session")
    assert g["closed_at"] == _ts(claude_at)


def test_a_stale_account_reading_is_dropped_but_the_fresh_one_still_transitions(
        fqmod, tmp_path, monkeypatch):
    """A reading older than FRESH (30 min) is dropped per account, exactly as
    before — it never suppresses a DIFFERENT, fresh account's transition."""
    monkeypatch.setenv("FLEET_QUOTA_LATEST", str(tmp_path / "latest.json"))
    monkeypatch.delenv("FLEET_QUOTA_ACCOUNT", raising=False)
    _snapshot_per_account(tmp_path, claude=(_ago(45), {"session": 100}),
                          codex=(_ago(1), {"session": 100}))
    assert fqmod.observe() == [("vendor:codex", "closed")]
    assert fqmod.gate("session") is None


def test_read_snapshot_carries_each_scopes_own_observed_at(fqmod, tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_QUOTA_LATEST", str(tmp_path / "latest.json"))
    monkeypatch.delenv("FLEET_QUOTA_ACCOUNT", raising=False)
    # Each reading time is taken once and reused for its expectation: a second
    # `_ago(N)` can land in the next second (#1454 review, 2026-11-09).
    claude_at, codex_at = _ago(2), _ago(25)
    _snapshot_per_account(tmp_path, claude=(claude_at, {"session": 5}),
                          codex=(codex_at, {"session": 10}))
    snap = fqmod.read_snapshot()
    assert snap["session"][2] == claude_at.replace(microsecond=0)
    assert snap["vendor:codex"][2] == codex_at.replace(microsecond=0)
