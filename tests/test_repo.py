"""fleet-repo — enrolment is a command with a record (plan 0013).

The surface is injected: `_watch()` is a module attribute, so the eight-lane
protection rule is exercised through the watcher's real `main_is_protected`
with a stubbed `_forge`, never through a second copy of the rule.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def repo_mod():
    return _load("fleet_repo", BIN / "fleet-repo")


def _bp(**over):
    """The canonical compliant rule from test_fleet_watch, with `over` applied.

    Imported rather than retyped: a second hand-written fixture drifts from the
    rule it is meant to exercise — mine omitted `required_approvals` and every
    protection test failed for a reason that had nothing to do with fleet-repo.
    """
    import test_fleet_watch as tfw
    return tfw._bp(**over)


def _watch_with(api, broker=None):
    """A fleet-watch module wired to a stubbed Forgejo and a stubbed BROKER.

    Since ADR-0003 §2 `check()` asks the broker rather than minting an admin
    PAT, so the seam moved from `_admin_token` to `_broker_get`. The default
    double runs the REAL rule (`main_is_protected` + `classify`, exactly what
    the deployed broker imports) and formats the broker's HTTP contract around
    it — so these tests still exercise the genuine lanes, and the status→PROT_*
    mapping that ADR-0003 §6 protects stays under test instead of stubbed.

    Since plan 0051, the injected seam is `_forge` (a `fleetforge.Forge`-shaped
    double), not `_api` — see test_fleet_watch's `_ForgeDouble`/`_wire`."""
    w = _load("fleet_watch", BIN / "fleet-watch")
    import test_fleet_watch as tfw
    forge = tfw._ForgeDouble(api)

    def _forge_builder(token):
        forge.token = token
        return forge

    w._forge = _forge_builder
    w._broker_get = broker if broker is not None else tfw._broker_double(w)
    return w


# --- the config format ----------------------------------------------------


def test_roundtrip_and_comments(repo_mod, tmp_path):
    conf = tmp_path / "repos.conf"
    conf.write_text("# a comment\n\nrepo: operator/sniff enforce dispatch\n"
                    "repo: operator/ares enforce   # trailing comment\n")
    repos, errs, kept = repo_mod.read_conf(conf)
    assert errs == [] and kept == []
    assert repos == {"operator/sniff": {"enforce": True, "dispatch": True},
                     "operator/ares": {"enforce": True}}
    repo_mod.write_conf(repos, conf)
    again, errs2, _ = repo_mod.read_conf(conf)
    assert errs2 == [] and again == repos, "write->read is not lossless"


def test_a_malformed_line_is_reported_not_guessed_at(repo_mod, tmp_path):
    conf = tmp_path / "repos.conf"
    conf.write_text("repo: operator/sniff enfrce\nnonsense\nrepo:\n")
    repos, errs, kept = repo_mod.read_conf(conf)
    assert repos == {}, "a line it could not parse must not become an enrolment"
    assert len(errs) == 3 and len(kept) == 3
    assert any("unknown flag" in e for e in errs)


def test_absent_file_is_empty_not_an_error(repo_mod, tmp_path):
    repos, errs, kept = repo_mod.read_conf(tmp_path / "nope.conf")
    assert (repos, errs, kept) == ({}, [], [])


# --- check: three exit codes, because two cannot carry three answers -------


def test_eligible_repo_exits_0(repo_mod):
    w = _watch_with(lambda m, p, body=None, token=None: (200, [_bp()]))
    assert repo_mod.check("operator/sniff", w) == (repo_mod.EXIT_OK, "eligible")


def test_an_open_lane_is_refused_and_names_the_lane(repo_mod):
    w = _watch_with(lambda m, p, body=None, token=None:
                    (200, [_bp(apply_to_admins=False)]))
    code, why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_REFUSED
    assert "admins" in why.lower(), why


def test_an_agent_that_may_approve_is_refused(repo_mod):
    """The collision that started this: the standing new-repo script puts
    revbot on the approvals whitelist of every repo."""
    w = _watch_with(lambda m, p, body=None, token=None:
                    (200, [_bp(approvals_whitelist_username=["revbot", "operator"])]))
    code, why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_REFUSED
    assert "revbot" in why


@pytest.mark.parametrize("status", [403, 401, 0, 500])
def test_unreadable_is_exit_2_not_refused(repo_mod, status):
    """403 is only one of the ways this can be undecidable. _api returns 0 on
    transport failure and passes 401/5xx through, and every one of them would
    otherwise render as 'refused, here is a lane to fix' — a lane that is not
    the problem. The watcher's runbook records that misdiagnosis sending an
    operator after a network fault that did not exist."""
    w = _watch_with(lambda m, p, body=None, token=None: (status, None))
    code, why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_UNKNOWN, f"HTTP {status} gave {code}: {why}"


def test_an_unreachable_broker_is_exit_2_not_a_refusal(repo_mod):
    """ADR-0003 §6, the distinction the broker exists to preserve. A broker
    that cannot be reached says NOTHING about the repo — rendering it as exit 1
    would refuse every repo on the fleet because one socket is down, with a
    lane named that is not the problem. Replaces the dead-admin-helper test:
    same property, and the dependency it guards moved from a local helper to a
    service on vaulthost."""
    w = _watch_with(lambda m, p, body=None, token=None: (200, [_bp()]),
                    broker=lambda path: (0, None))     # 0 = could not connect
    code, why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_UNKNOWN, why
    assert "unreachable" in why.lower(), why


def test_a_broker_503_is_exit_2_not_a_refusal(repo_mod):
    """The broker reached, and unable to answer — a sealed keyvault, an
    unloadable rule. Still not a statement about the repo."""
    w = _watch_with(lambda m, p, body=None, token=None: (200, [_bp()]),
                    broker=lambda path: (503, {"determinable": False,
                                               "lane": "vault",
                                               "detail": "keyvault unreachable"}))
    code, why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_UNKNOWN, why
    assert "vault" in why or "keyvault" in why, why


def test_only_a_200_is_ever_trusted(repo_mod):
    """A body that says ok:true under a NON-200 must not be read as eligible —
    the caller's whole contract is 'trust ok if and only if the status is
    200'."""
    w = _watch_with(lambda m, p, body=None, token=None: (200, [_bp()]),
                    broker=lambda path: (500, {"ok": True, "detail": "lying"}))
    code, _why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_UNKNOWN


def test_check_matches_the_watcher_exactly(repo_mod):
    """check() must not be a second implementation of the rule. Same stub, same
    verdict, for a passing and a failing repo."""
    for over, expect_ok in ((dict(), True), (dict(enable_push=True), False)):
        api = lambda m, p, body=None, token=None: (200, [_bp(**over)])
        w = _watch_with(api)
        ok, _ = w.main_is_protected("operator/sniff", "admintok")
        code, _why = repo_mod.check("operator/sniff", w)
        assert ok is expect_ok
        assert (code == repo_mod.EXIT_OK) is expect_ok


# --- check() through the DIRECT verdict source (plan 0054) ------------------
#
# `check()` asks `w.verdict()`, the resolver, not `w.broker_verdict()` by
# name — these tests configure the watcher module for the direct source and
# prove all three exits are still reachable through it, exactly as they are
# through the broker above. No new `_watch_with`-shaped helper: the direct
# source has no broker to stub, only `_forge` and a credential command.


def _watch_direct(monkeypatch, api, token_cmd="echo admintok"):
    monkeypatch.setenv("FLEET_VERDICT_SOURCE", "direct")
    if token_cmd is not None:
        monkeypatch.setenv("FLEET_DIRECT_TOKEN_CMD", token_cmd)
    else:
        monkeypatch.delenv("FLEET_DIRECT_TOKEN_CMD", raising=False)
    w = _load("fleet_watch", BIN / "fleet-watch")
    import test_fleet_watch as tfw
    forge = tfw._ForgeDouble(api)
    w._forge = lambda token: forge
    return w


def test_eligible_repo_exits_0_via_the_direct_source(repo_mod, monkeypatch):
    w = _watch_direct(monkeypatch, lambda m, p, body=None, token=None: (200, [_bp()]))
    assert repo_mod.check("operator/sniff", w) == (repo_mod.EXIT_OK, "eligible")


def test_a_refused_repo_exits_1_via_the_direct_source(repo_mod, monkeypatch):
    w = _watch_direct(monkeypatch, lambda m, p, body=None, token=None: (200, []))
    code, why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_REFUSED
    assert "not branch-protected" in why


def test_an_unreadable_repo_exits_2_via_the_direct_source(repo_mod, monkeypatch):
    w = _watch_direct(monkeypatch, lambda m, p, body=None, token=None: (0, None))
    code, why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_UNKNOWN, why


def test_a_missing_direct_credential_is_exit_2_not_a_crash(repo_mod, monkeypatch):
    """D2's handoff, as a test: the direct source needs its own admin-scoped
    credential and does not fall back to FLEET_TOKEN_CMD's write-scoped
    default. Unset, it must degrade to could-not-determine, same as an
    unreachable broker does — never a refusal and never a traceback."""
    w = _watch_direct(monkeypatch, lambda m, p, body=None, token=None: (200, [_bp()]),
                      token_cmd=None)
    code, why = repo_mod.check("operator/sniff", w)
    assert code == repo_mod.EXIT_UNKNOWN, why
    assert "FLEET_DIRECT_TOKEN_CMD" in why


# --- enable / disable -----------------------------------------------------


def test_enable_requires_a_named_flag(repo_mod, tmp_path):
    """Neither flag implies the other; with neither, enable is an error rather
    than a guess."""
    with pytest.raises(SystemExit):
        repo_mod.main(["--conf", str(tmp_path / "c.conf"), "enable", "operator/sniff"])


def test_enable_dispatch_is_idempotent(repo_mod, tmp_path, monkeypatch):
    conf = tmp_path / "repos.conf"
    monkeypatch.setattr(repo_mod, "check", lambda r, w=None: (repo_mod.EXIT_OK, "eligible"))
    for _ in range(2):
        repo_mod.main(["--conf", str(conf), "enable", "operator/sniff", "--dispatch"])
    repos, errs, _ = repo_mod.read_conf(conf)
    assert errs == [] and repos == {"operator/sniff": {"dispatch": True}}


def test_enable_enforce_reports_the_installers_refusal_and_leaves_the_flag_unset(
        repo_mod, tmp_path, monkeypatch, capsys):
    """fleet-install-hooks refuses bare repos, core.hooksPath repos and foreign
    hooks. An enrolment record claiming enforcement that is not installed is
    worse than no record."""
    conf = tmp_path / "repos.conf"
    monkeypatch.setattr(repo_mod, "_checkout", lambda r: tmp_path)

    import subprocess

    class R:
        returncode = 1
        stdout = ""
        stderr = "fleet-install-hooks: refuses to clobber a foreign pre-push hook"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    rc = repo_mod.main(["--conf", str(conf), "enable", "operator/sniff", "--enforce"])
    assert rc == repo_mod.EXIT_REFUSED
    assert "foreign pre-push hook" in capsys.readouterr().err
    repos, _, _ = repo_mod.read_conf(conf)
    assert repos.get("operator/sniff", {}).get("enforce") is not True


def test_disable_enforce_says_the_hook_is_still_installed(repo_mod, tmp_path, capsys):
    conf = tmp_path / "repos.conf"
    conf.write_text("repo: operator/sniff enforce dispatch\n")
    repo_mod.main(["--conf", str(conf), "disable", "operator/sniff", "--enforce"])
    out = capsys.readouterr().out
    assert "STILL INSTALLED" in out and "pre-push" in out
    repos, _, _ = repo_mod.read_conf(conf)
    assert repos == {"operator/sniff": {"dispatch": True}}


def test_disabling_every_flag_removes_the_row(repo_mod, tmp_path):
    conf = tmp_path / "repos.conf"
    conf.write_text("repo: operator/sniff dispatch\n")
    repo_mod.main(["--conf", str(conf), "disable", "operator/sniff", "--dispatch"])
    repos, _, _ = repo_mod.read_conf(conf)
    assert repos == {}


def test_a_bad_slug_is_refused_before_it_becomes_a_path(repo_mod, tmp_path):
    """The argument becomes ~/dev/<name> and an API path segment. fleetlib's
    validator is lowercase-only on purpose: Forgejo identity is case-insensitive
    while every gate here is exact-string, and APFS is case-insensitive too, so
    an uppercase slug would enrol a row the allowlist can never match while the
    checkout path still resolves."""
    for bad in ("../etc", "operator", "Operator/Sniff", "a/b/c"):
        with pytest.raises(SystemExit):
            repo_mod.main(["--conf", str(tmp_path / "c.conf"), "enable", bad, "--dispatch"])


def test_an_unparseable_line_survives_a_rewrite(repo_mod, tmp_path, monkeypatch):
    """The typo-becomes-a-deletion bug. read_conf drops what it cannot parse, and
    both writers regenerate the file from the parse — so a one-letter flag typo
    made the row vanish from the FILE on the next enable, and a repo the operator
    believed was enrolled quietly was not."""
    conf = tmp_path / "repos.conf"
    conf.write_text("repo: operator/ares enfrce\nrepo: operator/sniff dispatch\n")
    monkeypatch.setattr(repo_mod, "check", lambda r, w=None: (repo_mod.EXIT_OK, "eligible"))
    repo_mod.main(["--conf", str(conf), "enable", "operator/zeus", "--dispatch"])
    text = conf.read_text()
    assert "operator/ares enfrce" in text, "the unparseable row was deleted by a rewrite"
    repos, errs, kept = repo_mod.read_conf(conf)
    assert set(repos) == {"operator/sniff", "operator/zeus"}
    assert errs and kept == ["repo: operator/ares enfrce"]


def test_write_is_atomic_and_locked(repo_mod, tmp_path):
    """0013's DoD: concurrent writers cannot interleave, same lock idiom as the
    ledger. write_text truncates in place; a crash mid-write leaves a half-file
    the next read partially parses."""
    conf = tmp_path / "repos.conf"
    repo_mod.write_conf({"operator/sniff": {"dispatch": True}}, conf)
    lock = conf.with_suffix(conf.suffix + ".lock")
    assert lock.exists(), "no permanent lockfile beside the record"
    assert not list(tmp_path.glob("*.tmp.*")), "temp file left behind"
    # The lock is held for the duration: taking it non-blocking from outside
    # while a write runs must raise rather than let the write interleave.
    import threading
    held = threading.Event()
    with repo_mod.lib.held_lock(lock):
        held.set()
        with pytest.raises(repo_mod.lib.LockUnavailable):
            with repo_mod.lib.held_lock(lock, blocking=False):
                pass


# --- extlane is gone (ADR-0013 §1) -------------------------------------------


def test_an_old_file_carrying_extlane_reads_without_error_and_drops_it(
        repo_mod, tmp_path, monkeypatch, capsys):
    """The one place the retired token is named. `extlane` / `no-extlane` are
    read and discarded, so an old copy of the file is not a parse failure, and
    the next write does not carry them forward."""
    conf = tmp_path / "repos.conf"
    conf.write_text("repo: operator/eunomia enforce dispatch extlane\n"
                    "repo: operator/speakhush dispatch no-extlane\n"
                    "repo: operator/ares extlane no-extlane\n")
    repos, errs, kept = repo_mod.read_conf(conf)
    assert errs == [] and kept == []
    assert repos == {
        "operator/eunomia": {"enforce": True, "dispatch": True},
        "operator/speakhush": {"dispatch": True},
        "operator/ares": {},
    }
    assert repo_mod.main(["--conf", str(conf), "list"]) == repo_mod.EXIT_OK
    out = capsys.readouterr().out
    assert "extlane" not in out
    monkeypatch.setattr(repo_mod, "check", lambda r, w=None: (repo_mod.EXIT_OK, "eligible"))
    repo_mod.main(["--conf", str(conf), "enable", "operator/zeus", "--dispatch"])
    assert "extlane" not in conf.read_text()


def test_there_is_no_extlane_option(repo_mod, tmp_path):
    with pytest.raises(SystemExit):
        repo_mod.main(["--conf", str(tmp_path / "c"), "enable", "operator/a", "--extlane"])


@pytest.mark.parametrize("flag", ["no-enforce", "no-dispatch"])
def test_no_flag_has_a_written_negative(repo_mod, tmp_path, flag):
    """`enforce` and `dispatch` are off by absence. A written negative carries
    no information a reader could act on, and one that exists invites a later
    reader to branch on it."""
    conf = tmp_path / "repos.conf"
    conf.write_text(f"repo: operator/sniff {flag}\n")
    repos, errs, kept = repo_mod.read_conf(conf)
    assert repos == {} and len(errs) == 1 and "unknown flag" in errs[0]
    assert kept == [f"repo: operator/sniff {flag}"]


# --- the record must not overstate what the fleet will do with it ----------


@pytest.mark.parametrize("argv,needs_check", [
    (["list"], False),
    (["check", "operator/sniff"], True),
])
def test_list_and_check_say_the_watcher_does_not_read_this_file(
        repo_mod, tmp_path, capsys, monkeypatch, argv, needs_check):
    """`bin/fleet-watch:134` builds its allowlist from FLEET_WATCH_REPOS and
    nothing else. `enable` has always said
    so; `list` and `check` are the other two places an operator asks what the
    fleet is doing, and 'eligible' with no caveat reads as 'it will dispatch'."""
    conf = tmp_path / "repos.conf"
    conf.write_text("repo: operator/sniff dispatch\n")
    if needs_check:
        monkeypatch.setattr(repo_mod, "check",
                            lambda r, w=None: (repo_mod.EXIT_OK, "eligible"))
    repo_mod.main(["--conf", str(conf), *argv])
    err = capsys.readouterr().err
    assert "FLEET_WATCH_REPOS" in err and "not yet a dispatch" in err


def test_the_caveat_is_one_string_not_three_copies(repo_mod):
    """Three commands say it; a future edit must not be able to fix one and
    leave two."""
    src = (BIN / "fleet-repo").read_text()
    assert src.count("FLEET_WATCH_REPOS (bin/fleet-watch:134)") == 1


def test_an_empty_record_still_carries_the_caveat(repo_mod, tmp_path, capsys):
    repo_mod.main(["--conf", str(tmp_path / "nope.conf"), "list"])
    cap = capsys.readouterr()
    assert "no repos enrolled" in cap.out and "FLEET_WATCH_REPOS" in cap.err


# --- the shipped record ----------------------------------------------------


def test_the_shipped_record_parses_and_carries_no_retired_token(repo_mod):
    conf = Path(__file__).resolve().parent.parent / "config" / "repos.conf"
    repos, errs, kept = repo_mod.read_conf(conf)
    assert errs == [] and kept == [], f"the shipped record does not parse: {errs}"
    assert "extlane" not in conf.read_text()
