"""fleet-requeue (plan 0112), against a temporary fleet dir with marked_pr stubbed."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO, PLAN = "operator/x", "0009-thing"
LEASE = "branch--feat-0009-thing--001"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def rq(monkeypatch, tmp_path):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    monkeypatch.setenv("EUNOMIA_SESSION", "operator-1")
    mod = _load("fleet_requeue", BIN / "fleet-requeue")
    monkeypatch.setattr(mod.fwatch, "_token", lambda: ("t", None))
    monkeypatch.setattr(mod.fwatch, "marked_pr", lambda *a, **k: None)
    return mod


def _lease(rq, lid=LEASE, plan=PLAN, activated="2026-09-01T00:00:00Z", ttl=10,
           hb=True, state="active"):
    rq.lib.write_lease({"id": lid, "holder": "orch-1", "state": state,
                        "note": "plan:" + plan, "activated": activated,
                        "created": activated, "ttl_minutes": ttl,
                        "resource": {"type": "branch", "branch": "feat/x", "repo": REPO}})


def _events(rq):
    p = rq.lib.fleet_dir() / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _failed(rq, ts="2026-09-02T00:00:00Z", lease=LEASE, reason="boom"):
    p = rq.lib.fleet_dir() / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": ts, "actor": "orch-1", "type": "plan-failed",
                            "repo": REPO, "lease": None,
                            "detail": {"plan": PLAN, "lease": lease, "reason": reason}}) + "\n")


def _run(rq, *extra):
    return rq.main([REPO, PLAN, *extra])


def _releases(rq):
    return [e for e in _events(rq) if e["type"] == "lease-released"]


def test_dead_failed_lease_is_released(rq, capsys):
    _lease(rq); _failed(rq)
    assert _run(rq) == 0
    (ev,) = _releases(rq)
    assert ev["actor"] == "operator-1" and ev["lease"] == LEASE
    assert ev["detail"]["forced"] is True
    out = capsys.readouterr().out
    assert LEASE in out and "boom" in out and "re-dispatches" in out
    assert rq.lib.read_lease(LEASE)["state"] == "released"


def test_live_lease_refused(rq, capsys):
    _lease(rq, activated=rq.lib.ts(rq.lib.now_utc())); _failed(rq, ts=rq.lib.ts(rq.lib.now_utc()))
    assert _run(rq) == 1
    assert "TTL" in capsys.readouterr().err
    assert not _releases(rq) and rq.lib.read_lease(LEASE)["state"] == "active"


def test_no_failure_or_stale_failure_refused(rq, capsys):
    _lease(rq)
    assert _run(rq) == 1
    _failed(rq, ts="2026-08-01T00:00:00Z")
    assert _run(rq) == 1
    assert "plan-failed" in capsys.readouterr().err
    assert not _releases(rq)


def test_failure_of_another_lease_refused(rq):
    _lease(rq); _failed(rq, lease="other")
    assert _run(rq) == 1


@pytest.mark.parametrize("state,gesture", [("open", "respawn"), ("closed", "redispatch")])
def test_marked_pr_refused_names_gesture(rq, monkeypatch, capsys, state, gesture):
    _lease(rq); _failed(rq)
    monkeypatch.setattr(rq.fwatch, "marked_pr", lambda *a, **k: {"number": 7, "state": state})
    assert _run(rq) == 1
    assert gesture in capsys.readouterr().err
    assert not _releases(rq)


def test_unreadable_refused(rq, monkeypatch, capsys):
    _lease(rq); _failed(rq)
    monkeypatch.setattr(rq.fwatch, "marked_pr", lambda *a, **k: "unreadable")
    assert _run(rq) == 1
    assert "could not be read" in capsys.readouterr().err
    assert not _releases(rq)


def test_two_leases_refused_and_listed(rq, capsys):
    _lease(rq); _lease(rq, lid="branch--feat-0009-thing--002"); _failed(rq)
    assert _run(rq) == 1
    err = capsys.readouterr().err
    assert LEASE in err and "--002" in err
    assert not _releases(rq)


def test_zero_leases_refused(rq, capsys):
    assert _run(rq) == 1
    assert "nothing to requeue" in capsys.readouterr().err


def test_dry_run_writes_nothing(rq, capsys):
    _lease(rq); _failed(rq)
    assert _run(rq, "--dry-run") == 0
    assert "would release" in capsys.readouterr().out
    assert not _releases(rq) and rq.lib.read_lease(LEASE)["state"] == "active"


def test_actor_defaults_to_owner_user(rq, monkeypatch):
    monkeypatch.delenv("EUNOMIA_SESSION")
    monkeypatch.setenv("USER", "operator")
    _lease(rq); _failed(rq)
    assert _run(rq) == 0
    assert _releases(rq)[0]["actor"] == "owner-operator"


def test_invalid_user_refuses(rq, monkeypatch, capsys):
    monkeypatch.delenv("EUNOMIA_SESSION")
    monkeypatch.setenv("USER", "../evil")
    _lease(rq); _failed(rq)
    assert _run(rq) == 1
    assert not _releases(rq)
