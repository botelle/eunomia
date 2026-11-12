"""A review path that cannot run holds the fleet, and the reviews it stopped
resume (plan 0095, ADR-0018).

The probe runs a fake pin: real programs, exec'd, under a temp `bin/`. A test
that imported the file would pass the mode-0644 fault the probe exists to catch,
so the discriminating case chmods a real file.
"""
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from test_fleet_watch import (
    PLAN,
    _load as _fw_load,
    _wire as _fw_wire,
    _api_factory,
    _stub_orchestrator,
    _wait_spawns,
    _spawns,
)
from test_mopsus import _mech, _ledger
from test_mopsus_review_retry import _StubForge, _open_pr

BIN = Path(__file__).resolve().parent.parent / "bin"
PLAN_ANY_REPO = PLAN.replace("repo: operator/sniff\n", "")
REASON = ("round 1: review dispatch failed: PermissionError: [Errno 13] Permission "
          "denied: '/Users/operator/.local/share/pins/eunomia/bin/fleet-config'")

CONTRACT = {
    "v": 1,
    "calls": [
        {"id": "fleet-models-reviewers", "program": "fleet-models",
         "argv": ["reviewers", "{repo}", "{position}", "--json"],
         "positions": ["plan_reviewer", "code_reviewer"],
         "accept": {"rule": "json-list-first-ordinal-1"}},
        {"id": "fleet-config-review-bar", "program": "fleet-config",
         "argv": ["get", "review_bar", "--repo", "{repo}", "--json"],
         "accept": {"rule": "json-object-value-in", "values": ["low", "medium", "high"]}},
    ],
}

MODELS = f"""#!{sys.executable}
import json, sys
print(json.dumps([{{"ordinal": 1, "model": "m"}}]))
"""

# Reads `repo_setting` the way the strict reader does, so a database without the
# table fails as it did on 2026-09-28. `operator/bad` always fails.
CONFIG = f"""#!{sys.executable}
import json, os, sqlite3, sys
repo = sys.argv[sys.argv.index("--repo") + 1]
if repo == "operator/bad":
    sys.stderr.write("bad setting"); sys.exit(2)
db = os.path.join(os.environ["EUNOMIA_FLEET_DIR"], "fleet.db")
try:
    sqlite3.connect(db).execute("SELECT 1 FROM repo_setting LIMIT 1")
except sqlite3.OperationalError as e:
    sys.stderr.write(str(e)); sys.exit(1)
print(json.dumps({{"value": "medium"}}))
"""


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def fq():
    return _load("fleet_quota_rp", BIN / "fleet-quota")


@pytest.fixture
def mp():
    return _load("mopsus_rp", BIN / "mopsus")


@pytest.fixture
def fc():
    return _load("fleet_collect_rp", BIN / "fleet-collect")


def _migrate(fleet_dir):
    fleet_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(fleet_dir / "fleet.db"))
    con.execute("CREATE TABLE IF NOT EXISTS repo_setting (repo TEXT, key TEXT, value TEXT)")
    con.commit()
    con.close()


@pytest.fixture
def pin(tmp_path, monkeypatch):
    """A working pin, its contract and a migrated database. Returns the pin's
    bin/ and the contract path."""
    bindir = tmp_path / "pin" / "bin"
    bindir.mkdir(parents=True)
    for name, text in (("fleet-models", MODELS), ("fleet-config", CONFIG)):
        f = bindir / name
        f.write_text(text)
        f.chmod(0o755)
    contract = tmp_path / "eunomia-calls.json"
    contract.write_text(json.dumps(CONTRACT))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    monkeypatch.setenv("EUNOMIA_SESSION", "rp-test")
    monkeypatch.setenv("FLEET_REVIEW_CONTRACT", str(contract))
    monkeypatch.setenv("FLEET_REVIEW_PINS", str(bindir))
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    monkeypatch.delenv("FLEET_ANGELIA_URL", raising=False)
    monkeypatch.setenv("FLEET_QUOTA_LATEST", str(tmp_path / "no-meter.json"))
    _migrate(tmp_path / ".fleet")
    return bindir, contract


@pytest.fixture(autouse=True)
def pages(fq, mp, monkeypatch):
    calls = []
    rec = lambda title, body: (calls.append(body), True)[1]      # noqa: E731
    fq.NOTIFY = rec
    monkeypatch.setattr(mp.orch, "notify", rec)
    monkeypatch.setattr(mp.fquota, "NOTIFY", rec)
    monkeypatch.setattr(mp.fwatch, "_token", lambda: ("test-token", ""))
    monkeypatch.setattr(mp.fwatch, "marked_pr",
                        lambda repo, plan_id, token, dry_run=False: None)
    monkeypatch.setattr(mp.fwatch, "repos", lambda: ["operator/mech"])
    return calls


# --- D1: the probe ------------------------------------------------------------------


def test_a_pinned_program_without_its_exec_bit_fails_the_probe(fq, pin):
    bindir, _ = pin
    (bindir / "fleet-config").chmod(0o644)               # valid content, not executable
    code, line, entry = fq.probe_review_path("operator/x")
    assert code == 75 and entry == "fleet-config-review-bar"
    assert "PermissionError" in line and "operator/x" in line
    (bindir / "fleet-config").chmod(0o755)
    assert fq.probe_review_path("operator/x")[0] == 0


def test_a_database_without_repo_setting_fails_the_probe_and_a_migration_fixes_it(
        fq, pin, tmp_path):
    (tmp_path / ".fleet" / "fleet.db").unlink()
    code, line, entry = fq.probe_review_path("operator/x")
    assert code == 75 and entry == "fleet-config-review-bar"
    assert "no such table" not in line or "repo_setting" in line
    _migrate(tmp_path / ".fleet")
    assert fq.probe_review_path("operator/x")[0] == 0


def test_the_contract_drives_the_probe(fq, pin, tmp_path):
    bindir, contract = pin
    third = dict(CONTRACT)
    third["calls"] = CONTRACT["calls"] + [
        {"id": "fleet-extra", "program": "fleet-extra", "argv": ["{repo}"],
         "accept": {"rule": "json-object-value-in", "values": ["ok"]}}]
    contract.write_text(json.dumps(third))
    code, line, entry = fq.probe_review_path("operator/x")
    assert code == 75 and entry == "fleet-extra"          # the third entry was run
    extra = bindir / "fleet-extra"
    extra.write_text(f"#!{sys.executable}\nprint('{{\"value\": \"ok\"}}')\n")
    extra.chmod(0o755)
    assert fq.probe_review_path("operator/x")[0] == 0


def test_a_contract_the_probe_cannot_read_exits_1_and_leaves_the_gate(fq, pin, tmp_path):
    bindir, contract = pin
    assert fq.close("review-path:operator/x", None, "probe", "probe:t0:e") is True
    for text in ('{"v": 2, "calls": []}', "{not json",
                 json.dumps({"v": 1, "calls": [dict(
                     CONTRACT["calls"][1], accept={"rule": "no-such-rule"})]})):
        contract.write_text(text)
        code, line, trans = fq.run_review_path_probe("operator/x")
        assert code == 1 and trans is None and line
        assert fq.gate("review-path:operator/x")["state"] == "closed"
    contract.unlink()
    assert fq.run_review_path_probe("operator/x")[:1] == (1,)
    assert fq.gate("review-path:operator/x")["state"] == "closed"
    assert fq.gate("review-path:operator/never") is None
    assert fq.run_review_path_probe("operator/never")[0] == 1
    assert fq.gate("review-path:operator/never") is None


def _v2(contract, *extra):
    """The pin's contract raised to v2 with talos's two fleet-credits entries
    (commit 2ddb17e, plan 0029), using talos's own rule names."""
    data = json.loads(contract.read_text())
    data["v"] = 2
    data["calls"] += [
        {"id": "fleet-credits-decide", "program": "fleet-credits",
         "argv": ["decide", "--repo", "{repo}", "--pr", "{pr}", "--model", "{model}", "--json"],
         "accept": {"rule": "json-credits-decision"}},
        {"id": "fleet-credits-record", "program": "fleet-credits",
         "argv": ["record", "--repo", "{repo}", "--pr", "{pr}", "--usd", "{usd}"],
         "accept": {"rule": "exit-zero"}},
        *extra]
    contract.write_text(json.dumps(data))


def _fleet_credits(bindir, log):
    """A pinned fleet-credits that records any run, so a test can prove the
    probe never ran it."""
    f = bindir / "fleet-credits"
    f.write_text(f"#!/bin/sh\necho ran >> {log}\n")
    f.chmod(0o755)
    return f


def test_a_v2_contract_is_read_and_its_unknown_calls_are_never_run(fq, pin, tmp_path):
    bindir, contract = pin
    log = tmp_path / "ran"
    _fleet_credits(bindir, log)
    _v2(contract)
    code, line, _ = fq.probe_review_path("operator/x")
    assert code == 0 and "fleet-credits-decide" in line and "fleet-credits-record" in line
    assert not log.exists()


@pytest.mark.parametrize("accept", [None, "exit-zero", {"values": []}])
def test_a_call_with_no_accept_rule_is_skipped_not_refused(fq, pin, tmp_path, accept):
    bindir, contract = pin
    _fleet_credits(bindir, tmp_path / "ran")
    entry = {"id": "fleet-credits-odd", "program": "fleet-credits", "argv": ["x"]}
    if accept is not None:
        entry["accept"] = accept
    _v2(contract, entry)
    assert fq.probe_review_path("operator/x")[0] == 0


def test_a_skipped_call_whose_program_lost_its_exec_bit_closes_the_gate(fq, pin, tmp_path):
    bindir, contract = pin
    _fleet_credits(bindir, tmp_path / "ran").chmod(0o644)
    _v2(contract)
    code, line, entry = fq.probe_review_path("operator/x")
    assert code == 75 and entry == "fleet-credits-decide" and "not executable" in line


@pytest.mark.parametrize("v", [0, -1, "2", None, True, 1.5])
def test_a_contract_version_below_1_or_not_an_integer_cannot_be_judged(fq, pin, v):
    bindir, contract = pin
    data = json.loads(contract.read_text())
    data["v"] = v
    contract.write_text(json.dumps(data))
    assert fq._load_contract(contract)[0] is None


def test_a_missing_pin_directory_exits_75(fq, pin, monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_REVIEW_PINS", str(tmp_path / "no-pin"))
    code, line, _ = fq.probe_review_path("operator/x")
    assert code == 75 and "FileNotFoundError" in line


def test_the_cli_prints_one_line_and_exits_with_the_code(fq, pin, capsys):
    assert fq.main(["probe-review-path", "operator/x"]) == 0
    assert fq.gate("review-path:operator/x") is None      # nothing to open
    (pin[0] / "fleet-config").chmod(0o644)
    capsys.readouterr()
    assert fq.main(["probe-review-path", "operator/x"]) == 75
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    assert fq.gate("review-path:operator/x")["closed_by"] == "probe"


def test_the_same_fault_again_closes_the_gate_a_second_time(fq, pin, tmp_path):
    bindir, _ = pin
    (bindir / "fleet-config").chmod(0o644)
    assert fq.run_review_path_probe("operator/x")[2] == "closed"
    assert fq.run_review_path_probe("operator/x")[2] is None      # already closed
    (bindir / "fleet-config").chmod(0o755)
    assert fq.run_review_path_probe("operator/x")[2] == "open"
    assert fq.gate("review-path:operator/x")["opened_by"] == "probe"
    (bindir / "fleet-config").chmod(0o644)
    assert fq.run_review_path_probe("operator/x")[2] == "closed"
    rows = sqlite3.connect(str(tmp_path / ".fleet" / "fleet.db")).execute(
        "SELECT evidence FROM quota_transition WHERE to_state='closed'").fetchall()
    assert len(rows) == 2 and rows[0] != rows[1]
    assert all(r[0].startswith("probe:") and r[0].endswith(":fleet-config-review-bar")
               for r in rows)


def test_only_the_review_path_probe_opens_a_review_path_gate(fq, pin, capsys):
    fq.close("review-path:operator/x", None, "probe", "probe:t:e")
    with pytest.raises(ValueError):
        fq.open_gate("review-path:operator/x", "probe")
    with pytest.raises(ValueError):
        fq.close("session", None, "probe", "probe:t:e")
    with pytest.raises(SystemExit):
        fq.main(["open", "review-path:operator/x", "--by", "probe"])
    assert fq.gate("review-path:operator/x")["state"] == "closed"
    assert fq.main(["open", "review-path:operator/x", "--by", "owner"]) == 0
    assert fq.gate("review-path:operator/x")["opened_by"] == "owner"


def test_an_owner_override_holds_the_gate_open_against_failing_probes(fq, pin, monkeypatch):
    (pin[0] / "fleet-config").chmod(0o644)
    scope = "review-path:operator/x"
    assert fq.run_review_path_probe("operator/x")[2] == "closed"
    assert fq.main(["open", scope, "--by", "owner", "--for", "2"]) == 0
    for _ in range(3):                                   # three failing probes in the window
        code, line, trans = fq.run_review_path_probe("operator/x")
        assert code == 75 and trans == "overridden" and line
        assert fq.gate(scope)["state"] == "open"
    real = fq.lib.now_utc()
    monkeypatch.setattr(fq.lib, "now_utc",
                        lambda: real + __import__("datetime").timedelta(hours=3))
    assert fq.run_review_path_probe("operator/x")[2] == "closed"      # after the window
    assert fq.gate(scope)["state"] == "closed"


def test_the_override_window_is_bounded_and_review_path_only(fq, pin):
    with pytest.raises(SystemExit):
        fq.main(["open", "review-path:operator/x", "--by", "owner", "--for", "0"])
    with pytest.raises(SystemExit):
        fq.main(["open", "review-path:operator/x", "--by", "owner", "--for", "169"])
    with pytest.raises(SystemExit):
        fq.main(["open", "session", "--by", "owner", "--for", "2"])


def test_a_review_path_transition_pages_no_session_or_weekly_page(fq, pin, pages):
    (pin[0] / "fleet-config").chmod(0o644)
    fq.run_review_path_probe("operator/x")
    (pin[0] / "fleet-config").chmod(0o755)
    fq.run_review_path_probe("operator/x")
    assert pages == []


# --- D4: 0094's probe loop -----------------------------------------------------------


def test_the_usage_limit_probe_never_opens_a_review_path_gate(mp, fq, pin, monkeypatch):
    fq.close("review-path:operator/x", None, "probe", "probe:t:e")
    started = []
    monkeypatch.setattr(mp, "_try_start_locked", lambda *a, **k: started.append(a) or True)
    mp.quota_probe_sweep()
    assert started == []
    assert fq.gate("review-path:operator/x")["state"] == "closed"
    assert fq.run_review_path_probe("operator/x")[2] == "open"      # D1 opens it


# --- D2: fleet-watch -----------------------------------------------------------------


def _watch(tmp_path, monkeypatch, repos="operator/x,operator/y"):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _fw_load(tmp_path / ".fleet", FLEET_WATCH_REPOS=repos, FLEET_WATCH_CAP="5")
    _fw_wire(mod, _api_factory(plan_text=PLAN_ANY_REPO), orch)
    sent = []
    monkeypatch.setattr(mod, "notify", lambda t, b, *a, **k: (sent.append(b), True)[1])
    return mod, log, sent


def test_a_closed_gate_spawns_nothing_into_its_repo_and_the_rest_still_spawn(
        pin, tmp_path, monkeypatch, capsys):
    mod, log, sent = _watch(tmp_path, monkeypatch, "operator/bad,operator/y")
    assert mod.cycle() == 1
    assert len(_wait_spawns(log, 1)) == 1
    assert mod._quota().gate("review-path:operator/bad")["state"] == "closed"
    assert mod._quota().gate("review-path:operator/y") is None
    assert "review path held: operator/bad" in mod.cost_note()
    assert [b for b in sent if b.startswith("review path broken: operator/bad")]


def test_the_sweep_also_holds_and_leaves_the_resume_request_unconsumed(
        pin, tmp_path, monkeypatch):
    from test_mopsus_review_retry import _seed_resume_request
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / ".fleet"
    mod = _fw_load(fd, FLEET_WATCH_REPOS="operator/x", FLEET_OPERATOR_UID="")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    _fw_wire(mod, _api_factory(pulls=pulls, plan_text=PLAN_ANY_REPO), orch)
    monkeypatch.setattr(mod, "notify", lambda t, b, *a, **k: True)
    # the orphaned lease is made by spawn() directly, which does not probe
    dead = _orphaned_lease(mod, fd)
    _seed_resume_request(fd, "0009-thing", dead, 7, "deadbeef", "2030-01-01T00:00:00Z")
    (pin[0] / "fleet-config").chmod(0o644)
    assert mod.cycle() == 0
    assert len(_wait_spawns(log, 1)) == 1                # only the seed spawn
    con = sqlite3.connect(str(fd / "fleet.db"))
    consumed = "SELECT consumed_at FROM resume_request WHERE lease=?"
    assert con.execute(consumed, (dead,)).fetchone()[0] is None
    (pin[0] / "fleet-config").chmod(0o755)               # the same cycle, the path fixed
    assert mod.cycle() == 1
    assert _wait_spawns(log, 2)[-1].startswith("--resume ")
    assert con.execute(consumed, (dead,)).fetchone()[0] is not None


def _orphaned_lease(mod, fd, plan_id="0009-thing"):
    mod.spawn("operator/x", f"plans/{plan_id}.md", {"id": plan_id})
    _wait_spawns(fd.parent / "spawns.log", 1, timeout=1.5)
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    p = fd / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text())
    rec["created"] = "2020-01-01T00:00:00Z"
    p.write_text(json.dumps(rec))
    return lid


def test_a_repo_with_nothing_to_spawn_and_an_open_gate_is_not_probed(
        pin, tmp_path, monkeypatch):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _fw_load(tmp_path / ".fleet", FLEET_WATCH_REPOS="operator/y",
                   FLEET_WATCH_CAP="5")
    _fw_wire(mod, _api_factory(plans=(), plan_text=PLAN_ANY_REPO), orch)
    probed = []
    real = mod._quota().run_review_path_probe
    monkeypatch.setattr(mod._quota(), "run_review_path_probe",
                        lambda repo, **k: probed.append(repo) or real(repo, **k))
    assert mod.cycle() == 0
    assert probed == []


def test_every_closed_gate_is_reprobed_each_cycle_and_a_pass_opens_it(
        pin, tmp_path, monkeypatch):
    mod, log, sent = _watch(tmp_path, monkeypatch, "operator/y")
    q = mod._quota()
    q.close("review-path:operator/gone", None, "probe", "probe:t:e")   # nothing to spawn there
    assert mod.cycle() == 1
    assert q.gate("review-path:operator/gone")["state"] == "open"
    assert [b for b in sent if b == "review path restored: operator/gone"]


def test_the_broken_page_names_every_repo_once_and_the_next_failing_cycle_sends_none(
        pin, tmp_path, monkeypatch):
    mod, log, sent = _watch(tmp_path, monkeypatch, "operator/y")
    (pin[0] / "fleet-config").chmod(0o644)
    mod._rp_state = mod._rp_new()
    for repo in ("operator/a", "operator/b", "operator/c"):
        assert mod._review_path_allows(repo) is False
    mod._review_path_finish()
    broken = [b for b in sent if b.startswith("review path broken:")]
    assert len(broken) == 1
    assert all(r in broken[0] for r in ("operator/a", "operator/b", "operator/c"))
    assert "PermissionError" in broken[0]
    mod._rp_state = mod._rp_new()                        # the next failing cycle
    mod._review_path_finish()
    assert len([b for b in sent if b.startswith("review path broken:")]) == 1
    (pin[0] / "fleet-config").chmod(0o755)
    mod._rp_state = mod._rp_new()
    mod._review_path_finish()
    restored = [b for b in sent if b.startswith("review path restored:")]
    assert len(restored) == 1
    assert all(r in restored[0] for r in ("operator/a", "operator/b", "operator/c"))
    assert not [b for b in sent if b.startswith("fleet held")]


def test_every_page_row_carries_its_unit(pin, tmp_path, monkeypatch):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _fw_load(tmp_path / ".fleet", FLEET_WATCH_REPOS="operator/bad",
                   FLEET_WATCH_CAP="5")
    _fw_wire(mod, _api_factory(plan_text=PLAN_ANY_REPO), orch)
    mod.cycle()
    rows = [json.loads(l) for l in mod._orchestrator_module().pages_path()
            .read_text().splitlines()]
    broken = [r for r in rows if r["body"].startswith("review path broken:")]
    assert len(broken) == 1 and broken[0]["unit"] == mod.LABEL_WATCH


def test_a_probe_that_cannot_judge_pages_once_per_spell_and_holds_nothing(
        pin, tmp_path, monkeypatch):
    mod, log, sent = _watch(tmp_path, monkeypatch, "operator/y")
    pin[1].unlink()
    assert mod.cycle() == 1
    assert mod.cycle() >= 0
    cannot = [b for b in sent if b.startswith("review-path probe cannot judge:")]
    assert len(cannot) == 1
    assert [g for g in mod._quota().gates() if g["scope"].startswith("review-path:")] == []


# --- D3 / D4: mopsus -----------------------------------------------------------------


class _RPForge(_StubForge):
    """_StubForge plus the commit read the head baseline makes."""

    committed = "2000-01-01T00:00:00Z"
    commits_status = 200

    def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
        return self.commits_status, [{"sha": sha,
                                      "commit": {"committer": {"date": self.committed}}}]


def _stopped(mp, fc, tmp_path, monkeypatch, pr=7, head="deadbeef", reason=REASON):
    lease, db, conf = _mech(mp, fc, tmp_path, reason=reason)
    con = sqlite3.connect(str(db))
    con.execute("UPDATE dispatch_phase SET pr=? WHERE lease=?", (pr, lease))
    con.commit()
    con.close()
    forge = _RPForge(pulls={("operator/mech", pr): _open_pr(head=head)})
    monkeypatch.setattr(mp.fwatch, "_forge", lambda token: forge)
    rec = {"id": "dead-lease", "state": "active"}
    monkeypatch.setattr(mp.fwatch, "dispatch_leases",
                        lambda: {("operator/mech", "0009-x"): rec})
    monkeypatch.setattr(mp.fwatch.lib, "is_orphaned", lambda r: True)
    return lease, db, conf, forge


def _requests(db):
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT plan, lease, pr, head FROM resume_request "
                           "WHERE consumed_at IS NULL").fetchall()
    except sqlite3.OperationalError:
        return []


def test_a_review_dispatch_failure_with_a_failing_probe_is_held_and_escalated_once(
        mp, fc, fq, pin, tmp_path, monkeypatch, pages):
    (pin[0] / "fleet-config").chmod(0o644)
    lease, db, conf, _ = _stopped(mp, fc, tmp_path, monkeypatch)
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    g = fq.gate("review-path:operator/mech", db)
    assert g["state"] == "closed" and g["evidence"].startswith("probe:")
    entry = mp._load_state()["leases"][lease]
    assert entry["held"] == "review-path:operator/mech" and entry["lease"] == lease
    brief = mp._read_brief(entry["handle"])
    assert "held:      review-path:operator/mech closed until its probe passes\n" in brief
    assert "--- class: review-path ---" in brief
    n = len(pages)
    for _ in range(3):                                   # re-read, not re-paged
        assert mp.sweep(conf_path=conf, db=db) == (0, 0)
    assert len(pages) == n
    assert len(_ledger(tmp_path, "quota-closed")) == 1   # the probe's line only
    assert _requests(db) == []


def test_the_same_reason_with_a_passing_probe_stays_unclassified(
        mp, fc, fq, pin, tmp_path, monkeypatch):
    lease, db, conf, _ = _stopped(mp, fc, tmp_path, monkeypatch)
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    entry = mp._load_state()["leases"][lease]
    assert "held" not in entry
    assert "--- class: unclassified ---" in mp._read_brief(entry["handle"])
    assert fq.gate("review-path:operator/mech", db) is None


def test_a_row_review_quota_claimed_is_never_probed(mp, fc, fq, pin, tmp_path, monkeypatch):
    lease, db, conf, _ = _stopped(mp, fc, tmp_path, monkeypatch)
    monkeypatch.setattr(mp, "_apply_review_quota_reclass",
                        lambda rows, db=None: [r.update({"class": "review-quota"})
                                               for r in rows])
    probed = []
    monkeypatch.setattr(mp.fquota, "run_review_path_probe",
                        lambda *a, **k: probed.append(a) or (75, "x", None))
    mp.sweep(conf_path=conf, db=db)
    assert probed == []


def test_once_the_probe_opens_the_gate_the_review_resumes_through_a_request(
        mp, fc, fq, pin, tmp_path, monkeypatch):
    (pin[0] / "fleet-config").chmod(0o644)
    lease, db, conf, _ = _stopped(mp, fc, tmp_path, monkeypatch)
    mp.sweep(conf_path=conf, db=db)
    (pin[0] / "fleet-config").chmod(0o755)
    assert mp.sweep(conf_path=conf, db=db) == (0, 0)     # closed until the probe runs
    assert _requests(db) == []
    assert fq.run_review_path_probe("operator/mech", db=db)[2] == "open"
    assert mp.sweep(conf_path=conf, db=db) == (0, 0)
    assert _requests(db) == [("0009-x", "dead-lease", 7, "deadbeef")]
    (ev,) = _ledger(tmp_path, "review-requeued")
    assert ev["detail"]["cause"] == "review-path" and ev["detail"]["attempt"] == 1
    assert mp._load_state()["leases"][lease] == {"resumed": True}
    for _ in range(2):
        mp.sweep(conf_path=conf, db=db)
    assert len(_requests(db)) == 1 and len(_ledger(tmp_path, "review-requeued")) == 1


def test_the_event_is_emitted_before_the_request_is_written(
        mp, fc, fq, pin, tmp_path, monkeypatch):
    (pin[0] / "fleet-config").chmod(0o644)
    lease, db, conf, _ = _stopped(mp, fc, tmp_path, monkeypatch)
    mp.sweep(conf_path=conf, db=db)
    fq.open_gate("review-path:operator/mech", "owner", db=db)
    order = []
    real_emit, real_write = mp._emit_review_requeued, mp._write_resume_request
    monkeypatch.setattr(mp, "_emit_review_requeued",
                        lambda *a, **k: order.append("emit") or real_emit(*a, **k))
    monkeypatch.setattr(mp, "_write_resume_request",
                        lambda *a, **k: order.append("write") or real_write(*a, **k))
    mp.sweep(conf_path=conf, db=db)
    assert order == ["emit", "write"]


def _held_then_open(mp, fc, fq, pin, tmp_path, monkeypatch, **kw):
    (pin[0] / "fleet-config").chmod(0o644)
    lease, db, conf, forge = _stopped(mp, fc, tmp_path, monkeypatch, **kw)
    mp.sweep(conf_path=conf, db=db)
    fq.open_gate("review-path:operator/mech", "owner", db=db)
    return lease, db, conf, forge


@pytest.mark.parametrize("why", ["head moved", "closed", "slot", "limit"])
def test_a_failed_check_gives_no_resume_and_an_ordinary_page(
        why, mp, fc, fq, pin, tmp_path, monkeypatch, pages):
    lease, db, conf, forge = _held_then_open(mp, fc, fq, pin, tmp_path, monkeypatch)
    if why == "head moved":
        forge.pulls[("operator/mech", 7)] = _open_pr(head="cafef00d")
        want = "head moved"
    elif why == "closed":
        forge.pulls[("operator/mech", 7)] = _open_pr(state="closed")
        want = "is not open"
    elif why == "slot":
        import fcntl
        lock = mp.lib.fleet_dir() / "locks" / mp._REVIEW_LOCK
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        want = "slot is taken"
    else:
        monkeypatch.setattr(mp, "review_retry_limit", lambda db=None: 1)
        monkeypatch.setattr(mp, "_review_requeue_count", lambda *a, **k: 1)
        want = "limit reached"
    n = len(pages)
    try:
        assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    finally:
        if why == "slot":
            os.close(fd)
    assert len(pages) == n + 1
    assert _requests(db) == [] and _ledger(tmp_path, "review-requeued") == []
    entry = mp._load_state()["leases"][lease]
    assert "held" not in entry
    assert want in mp._read_brief(entry["handle"])


def test_a_resume_counts_on_the_counter_a_quota_retry_uses(
        mp, fc, fq, pin, tmp_path, monkeypatch):
    lease, db, conf, _ = _held_then_open(mp, fc, fq, pin, tmp_path, monkeypatch)
    mp.sweep(conf_path=conf, db=db)
    assert mp._review_requeue_count("operator/mech", 7, "deadbeef", db) == 1
    (ev,) = _ledger(tmp_path, "review-requeued")
    assert ev["repo"] == "operator/mech" and ev["pr"] == 7 and ev["sha"] == "deadbeef"


def test_a_resume_request_is_consumed_with_resume_true(pin, tmp_path, monkeypatch):
    from test_mopsus_review_retry import _seed_resume_request, _orphaned_lease_local
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / ".fleet"
    mod = _fw_load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    _fw_wire(mod, _api_factory(pulls=pulls, plan_text=PLAN_ANY_REPO), orch)
    dead = _orphaned_lease_local(mod, fd)
    _seed_resume_request(fd, "0009-thing", dead, 7, "deadbeef", "2030-01-01T00:00:00Z")
    monkeypatch.setattr(mod, "notify", lambda t, b, *a, **k: True)
    assert mod.cycle() == 1
    assert _wait_spawns(log, 2)[-1].startswith("--resume ")


def test_reader_accepts_the_talos_v1_contract_shape(fq):
    """Call-site evidence: talos `contracts/eunomia-calls.json` v1 (talos plan
    0016, commit d045409) lists `resolve_reviewers` (dispatch-review.py:
    `[FLEET_MODELS, "reviewers", repo, position, "--json"]`, ordinal 1 first)
    and `resolve_review_bar` (`[FLEET_CONFIG, "get", "review_bar", "--repo",
    repo, "--json"]`, `value` in low/medium/high) with dict-form `accept`. The
    fixture above is that shape; the reader must load it, or the feature is
    inert (exit 1) on the real host."""
    calls, reason = fq._load_contract(_write_contract_fixture())
    assert reason is None and [c["id"] for c in calls] == [
        "fleet-models-reviewers", "fleet-config-review-bar"]


def _write_contract_fixture():
    import tempfile
    f = Path(tempfile.mkdtemp()) / "eunomia-calls.json"
    f.write_text(json.dumps(CONTRACT))
    return f


def test_reader_accepts_the_deployed_contract_when_present(fq):
    """On a host that carries talos's deployed contract, the real file must
    parse under the reader: a shape mismatch must fail CI here, not silently
    make the probe exit 1 (inert) in production."""
    real = Path.home() / "agent" / "contracts" / "eunomia-calls.json"
    if not real.exists():
        pytest.skip("no deployed talos contract on this host")
    calls, reason = fq._load_contract(real)
    assert reason is None, reason



# --- the deferred findings of #693's review ------------------------------------------


@pytest.mark.parametrize("mutate", [
    lambda c: c["calls"][1]["argv"].append("{typo}"),
    lambda c: c["calls"][0].update(argv=["reviewers", "{repo}", "--json"]),
    lambda c: c["calls"][1].update(argv=["get", "{position}", "{repo}"]),
])
def test_an_unrenderable_argv_exits_1_and_changes_nothing(fq, pin, mutate):
    (pin[0] / "fleet-config").chmod(0o644)          # a call that would fail first
    contract = json.loads(pin[1].read_text())
    mutate(contract)
    pin[1].write_text(json.dumps(contract))
    code, line, trans = fq.run_review_path_probe("operator/x")
    assert code == 1 and trans is None and line
    assert fq.gate("review-path:operator/x") is None


def test_a_review_path_scope_closes_only_by_the_probe(fq, pin):
    with pytest.raises(SystemExit):
        fq.main(["close", "review-path:operator/x", "--resets-at", "-", "--by", "stop",
                 "--evidence", "e"])
    with pytest.raises(ValueError):
        fq.close("review-path:operator/x", None, "stop", "e")
    assert fq.gate("review-path:operator/x") is None


def test_a_bad_for_opens_nothing(fq, pin):
    fq.close("review-path:operator/x", None, "probe", "probe:t:e")
    with pytest.raises(ValueError):
        fq.owner_open("review-path:operator/x", 500)
    assert fq.gate("review-path:operator/x")["state"] == "closed"


def test_the_pinned_programs_do_not_inherit_the_token_helper_recipes(fq, pin, monkeypatch):
    (pin[0] / "fleet-models").write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "sys.exit(3) if os.environ.get('FLEET_TOKEN_CMD') else None\n"
        "print(json.dumps([{'ordinal': 1}]))\n")
    monkeypatch.setenv("FLEET_TOKEN_CMD", "/bin/leak")
    assert fq.probe_review_path("operator/x")[0] == 0


def test_a_mopsus_closed_gate_still_gets_its_one_broken_page(
        pin, tmp_path, monkeypatch):
    mod, log, sent = _watch(tmp_path, monkeypatch, "operator/y")
    (pin[0] / "fleet-config").chmod(0o644)
    mod._quota().run_review_path_probe("operator/a")      # mopsus got there first
    mod._rp_state = mod._rp_new()
    assert mod._review_path_allows("operator/a") is False
    mod._review_path_finish()
    assert len([b for b in sent if b.startswith("review path broken: operator/a")]) == 1
    mod._rp_state = mod._rp_new()
    mod._review_path_allows("operator/a")
    mod._review_path_finish()
    assert len([b for b in sent if b.startswith("review path broken:")]) == 1


def test_a_second_override_spell_pages_again(pin, tmp_path, monkeypatch):
    mod, log, sent = _watch(tmp_path, monkeypatch, "operator/y")
    q = mod._quota()
    (pin[0] / "fleet-config").chmod(0o644)
    scope = "review-path:operator/a"
    q.run_review_path_probe("operator/a")
    for spell in (1, 2):
        q.owner_open(scope, 2)
        mod._rp_state = mod._rp_new()
        mod._review_path_allows("operator/a")
        mod._rp_state = mod._rp_new()
        mod._review_path_allows("operator/a")             # same spell: no second page
        assert len([b for b in sent if "under owner override" in b]) == spell
        # the window ends and the next failing probe closes the gate again
        con = sqlite3.connect(str(mod.lib.fleet_dir() / "fleet.db"))
        con.execute("UPDATE quota_transition SET resets_at='2000-01-01T00:00:00Z' "
                    "WHERE evidence='override'")
        con.commit()
        con.close()
        mod._rp_state = mod._rp_new()
        mod._review_path_allows("operator/a")
        assert q.gate(scope)["state"] == "closed"


def test_the_quota_retry_waits_while_the_review_path_gate_is_closed(
        mp, fc, fq, pin, tmp_path, monkeypatch):
    fq.close("review-path:operator/mech", None, "probe", "probe:t:e")
    rows = [{"repo": "operator/mech"}, {"repo": "operator/other"}]
    assert mp._review_path_open(rows) == [{"repo": "operator/other"}]
    started = []
    monkeypatch.setattr(mp, "_emit_review_requeued",
                        lambda *a, **k: started.append(a) or (True, ""))
    mp._attempt_retry({"repo": "operator/mech", "pr": 7, "head": "d", "kind": "plan",
                       "plan_id": "0009-x", "lease": "l", "limit": 3})
    assert started == []


def test_a_head_pushed_after_the_failure_is_not_held(
        mp, fc, fq, pin, tmp_path, monkeypatch, pages):
    (pin[0] / "fleet-config").chmod(0o644)
    lease, db, conf, forge = _stopped(mp, fc, tmp_path, monkeypatch)
    forge.committed = "2999-01-01T00:00:00Z"
    assert mp.sweep(conf_path=conf, db=db) == (1, 0)
    entry = mp._load_state()["leases"][lease]
    assert "held" not in entry
    assert "head moved before classification" in mp._read_brief(entry["handle"])


def test_an_unreadable_forge_after_the_gate_opens_keeps_the_entry_held(
        mp, fc, fq, pin, tmp_path, monkeypatch, pages):
    lease, db, conf, forge = _held_then_open(mp, fc, fq, pin, tmp_path, monkeypatch)
    monkeypatch.setattr(mp.fwatch, "_token", lambda: (None, "helper failed"))
    n = len(pages)
    assert mp.sweep(conf_path=conf, db=db) == (0, 0)
    assert len(pages) == n and _requests(db) == []
    assert mp._load_state()["leases"][lease]["held"] == "review-path:operator/mech"
    monkeypatch.setattr(mp.fwatch, "_token", lambda: ("test-token", ""))
    mp.sweep(conf_path=conf, db=db)                      # the forge is back: it resumes
    assert len(_requests(db)) == 1
