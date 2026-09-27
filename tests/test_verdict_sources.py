"""Tests for plan 0054 — the ignition verdict has two sources, broker
(default) and direct, and both must answer identically.

`bin/fleet-watch`'s `verdict(repo)` is the resolver; `broker_verdict` and
`_direct_verdict` are its two implementations. This file proves the property
D4 asks for: fed the SAME underlying Forgejo response, both implementations
produce the same `(code, detail, lane)` triple — one parameterised test body
run twice, not two tests that could quietly drift apart — and that an
unreachable broker never falls through to the direct implementation.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

import test_fleet_watch as tfw

BIN = Path(__file__).resolve().parent.parent / "bin"

# Every env var either verdict source reads live. Reset before each load so
# one test's configuration cannot leak into the next (the same r3 low shape
# test_fleet_watch's own _WATCH_ENV exists for).
_ENV = ("FLEET_VERDICT_SOURCE", "FLEET_DIRECT_TOKEN_CMD", "FLEET_WATCH_REPOS")


def _load(**env):
    for k in _ENV:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    ldr = importlib.machinery.SourceFileLoader("fleet_watch", str(BIN / "fleet-watch"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("fleet_watch", ldr))
    ldr.exec_module(mod)
    return mod


def _underlying_forge(status, body):
    """A Forge double answering EVERY call — not just branch_protections —
    with the same (status, body). Both verdict sources only ever call
    `get_branch_protections` on it, so this is deliberately not routed by
    path the way `_api_factory` is."""
    def api(method, path, req_body=None, token=None):
        return status, body
    return tfw._ForgeDouble(api)


def _local_broker(mod):
    """A broker double that runs the REAL rule (`main_is_protected` plus
    `classify()`) and formats it the way keyvault's broker does, WITHOUT
    inventing a lane keyvault's own contract has not documented. Unlike
    test_fleet_watch's `_broker_double`, this does not fabricate `some_lane`
    for a plain refusal (that string exists there only to prove
    `broker_verdict` forwards whatever lane the wire sends, not to model a
    real broker response) — here the point is to compare against
    `_direct_verdict`, which cannot invent a lane the wire never carries
    either, and the one lane value both sides actually agree on is
    `token_rejected` (docs/plan-dispatch.md's own table names it)."""
    repo_mod = tfw._load_repo_module()

    def _get(path):
        if path == "/health":
            return 200, {}
        prefix = "/forgejo/protection/"
        assert path.startswith(prefix), path
        ok, why = mod.main_is_protected(path[len(prefix):], "admintok")
        code, detail = repo_mod.classify(ok, why)
        if code == repo_mod.EXIT_OK:
            return 200, {"ok": True, "detail": detail}
        if code == repo_mod.EXIT_REFUSED:
            return 200, {"ok": False, "detail": detail}
        resp = {"determinable": False, "detail": detail}
        if detail.startswith("branch protections rejected the token"):
            resp["lane"] = "token_rejected"
        return 503, resp
    return _get


CASES = [
    ("protected", 200, [tfw._bp()]),
    ("unprotected", 200, []),
    ("unreadable", 500, None),
    ("401", 401, None),
    ("403", 403, None),
    ("no_connection", 0, None),
]


EXPECT_CODE = {"protected": "PROT_OK", "unprotected": "PROT_REFUSED",
              "unreadable": "PROT_UNKNOWN", "401": "PROT_UNKNOWN",
              "403": "PROT_UNKNOWN", "no_connection": "PROT_UNKNOWN"}
EXPECT_LANE = {"protected": None, "unprotected": None, "unreadable": None,
              "401": "token_rejected", "403": "token_rejected",
              "no_connection": None}


@pytest.mark.parametrize("case,status,body", CASES, ids=[c[0] for c in CASES])
def test_broker_and_direct_agree_on_the_verdict(case, status, body):
    """One body, run against both sources over the identical underlying
    Forgejo response (D4).

    `code` and `lane` are the two elements anything downstream actually
    branches on (`cycle()`'s two alarm chains key on `lane`; nothing parses
    `detail`), so those two are asserted for an EXACT match on all six cases.

    `detail` is compared exactly only for the two cases where the wire
    carries it verbatim both ways (protected, unprotected) — `broker_verdict`
    forwards `classify()`'s own message unchanged there. For the four
    could-not-determine cases `broker_verdict` (untouched, per this plan's own
    boundary) wraps the reason in its OWN 'forgejo-broker could not determine
    (HTTP ...)' framing, because a broker genuinely is answering on keyvault's
    behalf; `_direct_verdict` has no broker to attribute the answer to and
    reports `classify()`'s plain text instead. Both are true; neither is
    padding out the other's wording, so this does not compare them by string
    equality — only that each is non-empty exactly when the code says it
    should be.
    """
    mod = _load(FLEET_WATCH_REPOS="operator/sniff")
    forge = _underlying_forge(status, body)
    mod._forge = lambda token: forge
    mod._broker_get = _local_broker(mod)
    broker_code, broker_detail, broker_lane = mod.broker_verdict("operator/sniff")

    os.environ["FLEET_DIRECT_TOKEN_CMD"] = "echo admintok"
    try:
        direct_code, direct_detail, direct_lane = mod._direct_verdict("operator/sniff")
    finally:
        os.environ.pop("FLEET_DIRECT_TOKEN_CMD", None)

    expect_code = getattr(mod, EXPECT_CODE[case])
    assert broker_code == expect_code, (case, "broker", broker_code)
    assert direct_code == expect_code, (case, "direct", direct_code)
    assert broker_lane == EXPECT_LANE[case], (case, "broker", broker_lane)
    assert direct_lane == EXPECT_LANE[case], (case, "direct", direct_lane)
    assert bool(broker_detail) == bool(direct_detail) == (expect_code != mod.PROT_OK), (
        case, broker_detail, direct_detail)
    if case in ("protected", "unprotected"):
        assert broker_detail == direct_detail, (case, broker_detail, direct_detail)


def test_direct_source_with_no_credential_is_undetermined_not_a_crash():
    mod = _load(FLEET_WATCH_REPOS="operator/sniff", FLEET_VERDICT_SOURCE="direct")
    code, why, lane = mod.verdict("operator/sniff")
    assert code == mod.PROT_UNKNOWN, (code, why)
    assert "FLEET_DIRECT_TOKEN_CMD" in why
    assert lane is None


def test_resolver_defaults_to_broker():
    mod = _load(FLEET_WATCH_REPOS="operator/sniff")
    assert mod.verdict_source() == "broker"
    calls = []
    mod._forge = lambda token: _underlying_forge(200, [tfw._bp()])
    mod._broker_get = _local_broker(mod)
    mod._direct_verdict = lambda repo: calls.append(repo) or (mod.PROT_OK, "", None)
    code, _why, _lane = mod.verdict("operator/sniff")
    assert code == mod.PROT_OK
    assert calls == [], "the default source must ask the broker, not direct"


def test_an_unreachable_broker_does_not_fall_through_to_direct():
    """D3, and the Definition of Done's own bullet for it: the resolver must
    never treat an unreachable broker as a reason to try the direct
    implementation instead — that fallthrough is exactly how a broker outage
    would hand this process an admin credential it does not otherwise hold."""
    mod = _load(FLEET_WATCH_REPOS="operator/sniff")
    mod._broker_get = lambda path: (0, None)   # unreachable
    calls = []
    mod._direct_verdict = lambda repo: calls.append(repo) or (mod.PROT_OK, "", None)
    code, why, _lane = mod.verdict("operator/sniff")
    assert code == mod.PROT_UNKNOWN, (code, why)
    assert calls == [], "an unreachable broker must never invoke the direct source"


def test_an_invalid_source_is_refused_not_guessed_at():
    mod = _load(FLEET_WATCH_REPOS="operator/sniff", FLEET_VERDICT_SOURCE="cloud")
    with pytest.raises(SystemExit):
        mod.verdict_source()


def test_direct_source_is_never_the_default_even_with_a_credential_configured():
    """D3: 'direct' must be an explicit opt-in, never inferred from a
    credential merely being present. Configuring FLEET_DIRECT_TOKEN_CMD alone
    (with FLEET_VERDICT_SOURCE unset) must still ask the broker."""
    mod = _load(FLEET_WATCH_REPOS="operator/sniff",
               FLEET_DIRECT_TOKEN_CMD="echo admintok")
    assert mod.verdict_source() == "broker"
