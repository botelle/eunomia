"""Tests for fleet-watch — merge-is-ignition dispatch (plan 0004 §4).

The Forgejo surface is injected (`_forge`/`_token` are module attributes), and
the orchestrator is a recording stub, so every DoD bullet runs without a
network, a model, or a real spawn."""
import base64
import ast
import importlib.machinery
import importlib.util
import json
import re
import urllib.parse
import os
import pathlib
import shutil
import stat
import subprocess
import time
import sys
import pytest
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


# Every env var fleet-watch reads AT IMPORT. Anything missing here leaks into
# the next _load and makes tests order-dependent (the r3 low this list exists
# for; the r7 identity sets are read at import too).
_WATCH_ENV = ("FLEET_WATCH_REPOS", "FLEET_WATCH_CAP", "FLEET_OPERATOR_UID",
              "FLEET_PR_PAGE_CAP", "FLEET_ORCHESTRATOR", "FLEET_NTFY_URL",
              "FLEET_IMPLEMENTER_UIDS", "FLEET_IMPLEMENTER_LOGINS",
              "FLEET_AGENT_ACCOUNTS", "FLEET_PINS",
              # read at import, and test_this_hosts_forgejo_url_still_drives...
              # sets it — without this it leaked into every later _load and
              # made the suite order-dependent, which is the exact r3 low this
              # tuple exists for (#141 r1 L1).
              "FLEET_FORGEJO_URL",
              # not read at import (fleetjob.select_impl() reads it live, per
              # call) but reset here anyway so a test that pins it cannot leak
              # into the next one that does not (same r3 low shape).
              "FLEET_DISPATCH_IMPL",
              # verdict_source() and _direct_token_cmd() are also read live,
              # not cached at import — same r3 low shape again.
              "FLEET_VERDICT_SOURCE", "FLEET_DIRECT_TOKEN_CMD",
              # pin_advance_window() caches at first read, not import, but
              # still belongs in this list for the same r3 low reason.
              "FLEET_PIN_ADVANCE_PLIST", "FLEET_PIN_PAGE_MARGIN",
              # plan 0068: read at the import of bin/fleet-config, which this
              # file's own import triggers (fleetconfig is loaded as a
              # sibling, same as fleetlib) — the same "read at import" shape,
              # one hop removed.
              "FLEET_DB",
              # read live by lib.work_root(), which names the run log the cap
              # count reads (plan 0067). Other test files set it, and it
              # outlived them: the log path then pointed into a stranger's
              # tmp_path and four tests here failed only in the full suite.
              "FLEET_WORK_ROOT")


def _load(fleet_dir, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    os.environ["EUNOMIA_SESSION"] = "watch-test"
    os.environ.pop("EUNOMIA_LEDGER_HOST", None)
    for k in _WATCH_ENV:                  # r3 low: no inheritance between
        os.environ.pop(k, None)           # tests (flakes under random order)
    # FLEET_SPAWN=popen unless a test asks otherwise. Two separate reasons, and
    # the suite failed on the second before it was noticed on the first:
    #
    #   * HERMETIC. A test reaching spawn() under the launchd default writes a
    #     plist and calls `launchctl bootstrap` against the DEVELOPER'S OWN gui
    #     domain — real labels, real jobs, from a unit test. On macOS that
    #     SUCCEEDS, which is why the suite went green locally while doing it.
    #   * PORTABLE. There is no `launchctl` on Linux, so the same tests fail on
    #     the cihost-linux CI label. Green on opshost proved nothing, exactly as
    #     the runner has taught twice before.
    #
    # The launchd path is covered deliberately instead — with _launchctl stubbed
    # and JOB_DIR pointed at tmp_path — so it is tested without being ambient.
    os.environ["FLEET_SPAWN"] = "popen"
    # plan 0059: default the advance-window plist to a path that cannot
    # exist, so `pin_advance_window()` reads None (advancer not installed —
    # today's transition-gated schedule) regardless of whether the machine
    # actually running this suite has the real unit installed. A test that
    # wants to exercise the delay overrides this explicitly.
    os.environ["FLEET_PIN_ADVANCE_PLIST"] = str(Path(fleet_dir) / "no-such-pin-advance.plist")
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("fleet_watch", str(BIN / "fleet-watch"))
    spec = importlib.util.spec_from_loader("fleet_watch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    # fleetjob.JOB_DIR is uid-global and fixed from HOME at import, so a test
    # reaching an armed cycle() would otherwise run reap_jobs against the
    # DEVELOPER'S REAL job directory — bootout is a SIGTERM, so that kills
    # whatever the fleet is working on, and the test still passes. reap_jobs
    # now refuses to touch a job another fleet dir minted, which is the real
    # fix; this is the second lock, so that a future change to the
    # attribution logic cannot re-arm the suite against production.
    mod.fleetjob.JOB_DIR = pathlib.Path(fleet_dir) / "jobs"
    return mod


def _stub_supervisor(mod, monkeypatch, rc=0, out=""):
    """Stub fleetjob's launchctl seam to succeed (or fail, via rc/out) without
    reaching the developer's own gui domain — the same HERMETIC reasoning
    `_load` documents for FLEET_SPAWN=popen, now needed per-test wherever a
    test deliberately exercises the launchd job path. Returns the call log."""
    calls = []
    monkeypatch.setattr(mod.fleetjob, "_run_launchctl",
                        lambda *a, **k: (calls.append(a), (rc, out))[1])
    return calls


def _stub_orchestrator(tmp_path):
    """Records one line per spawn; never returns a real orchestrator.

    Answers `--ready` with 0 and WITHOUT logging: readiness is a probe the
    watcher makes before arming, not a dispatch, and counting it as one would
    make every spawn assertion in this file off by the number of cycles."""
    log = tmp_path / "spawns.log"
    s = tmp_path / "orch-stub.sh"
    s.write_text('#!/bin/sh\n[ "$1" = "--ready" ] && exit 0\n'
                 f'echo "$1 $2 $3 $EUNOMIA_SESSION" >> {log}\n')
    s.chmod(s.stat().st_mode | stat.S_IXUSR)
    return str(s), log


def _spawns(log):
    """A plain read. Use _wait_spawns when a count is expected — see why there."""
    return log.read_text().splitlines() if log.exists() else []


def _wait_spawns(log, n, timeout=5.0):
    """Spawns are DETACHED — the stub may not have run when we assert.

    Wait for a KNOWN count. A settle-until-stable cannot substitute: an empty
    log is indistinguishable from a child that has not started yet.

    Most call sites in this file already used this. The ones that did NOT —
    `_orphaned_lease`, and the `before = len(_spawns(log))` captures — were
    reading the log bare, so what they measured depended on how long the
    preceding statement took. Adding one subprocess to the dispatch path (the
    readiness probe) shifted that and five tests failed at once. Those call
    sites now come through here."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cur = _spawns(log)
        if len(cur) >= n:
            return cur
        time.sleep(0.05)
    return _spawns(log)


PLAN = """---
id: 0009-thing
status: ready
repo: operator/sniff
zone: public
tier: 2
---
# Thing
"""


def _contents_list(names):
    return [{"name": n, "type": "file"} for n in names]


def _contents_file(text):
    return {"content": base64.b64encode(text.encode()).decode()}


# Every lane onto main closed: an approval is required, and only a non-agent
# account may push, approve, or merge. r6 M1-M3 made the last two lanes part of
# the property, so the shared fixture has to state them.
GOOD_BP = {
    "branch_name": "main",
    "required_approvals": 1,
    "enable_approvals_whitelist": True,
    "approvals_whitelist_username": ["operator"],
    "enable_merge_whitelist": True,
    "merge_whitelist_usernames": ["operator"],
    # r7 M1: both default FALSE in Forgejo, so the fixture has to state the
    # safe value explicitly — an approval must not survive a later push.
    "dismiss_stale_approvals": True,
    "ignore_stale_approvals": False,
    # r9 M1: defaults OFF in Forgejo, and while off an admin bypasses the
    # whole rule — so the compliant fixture has to state it.
    "apply_to_admins": True,
}


def _bp(**over):
    """A compliant rule with `over` applied. Passing rule_name drops
    branch_name, so glob-named rules can be built the same way."""
    d = dict(GOOD_BP)
    if "rule_name" in over:
        d.pop("branch_name", None)
    d.update(over)
    return d



def _instant(ts):
    """An aware datetime from an RFC3339 stamp, or None — the comparison the
    real Forgejo handler does for `since`."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _api_factory(protected=True, plans=("0009-thing.md",), plan_text=PLAN,
                 pulls=None, comments=None, calls=None):
    """A Forgejo stub keyed on (method, path-fragment), wrapped as a
    `Forge`-shaped double (plan 0051 D2) so it can be handed straight to
    `_wire`/`mod._forge`. The (method, path) routing table is unchanged from
    before the migration — only the outer shape (an object with the five
    verbs fleet-watch calls, rather than a raw callable) is new — so every
    test below that builds its own `def api(method, path, body=None,
    token=None): ...` closure around `_api_factory()(method, path, body,
    token)` keeps working unmodified: `_ForgeDouble` is itself callable with
    that same four-argument shape (see its `__call__`)."""
    def _api(method, path, body=None, token=None):
        if calls is not None:
            calls.append((method, path))
        if "/branch_protections" in path:
            return (200, [_bp()]) if protected else (200, [])
        if path.endswith("/contents/plans?ref=main"):
            return 200, _contents_list(plans)
        if "/contents/plans/" in path:
            return 200, _contents_file(plan_text)
        if "/pulls?" in path:
            # page like real Forgejo: rows on page 1, then an EMPTY page.
            # (The watcher stops only on an empty page — a SHORT page cannot
            # prove the end when an instance caps MAX_RESPONSE_ITEMS.)
            return 200, ((pulls or []) if "page=1" in path else [])
        if "/issues/" in path and "/comments" in path:
            # Modelled on the REAL handler (r10 H1). Forgejo's per-issue
            # `ListIssueComments` declares no ListOptions: it IGNORES `page`
            # and returns the whole list, oldest-first, every time. The old
            # fixture paged, so two tests "covering" the paging loop passed
            # against an API that does not exist while the loop could never
            # terminate in production. A stub that is kinder than the system
            # tests the stub.
            #
            # `since` IS supported here, and is applied as the handler does.
            rows = list(comments or [])
            m = re.search(r"[?&]since=([^&]+)", path)
            if m:
                # Compare INSTANTS, not strings. Forgejo parses both sides, so
                # `08:01-04:00` is one minute after `12:00Z` even though it
                # sorts four hours earlier as text — the r2 M1 bug, reproduced
                # in the fixture on the first attempt at writing it.
                since = _instant(urllib.parse.unquote(m.group(1)))
                rows = [c for c in rows
                        if since is None or (_instant(c.get("created_at")) or since) >= since]
            return 200, rows
        return 404, None
    return _ForgeDouble(_api)


class _ForgeDouble:
    """The test-side seam plan 0051 D2 describes: a `fleetforge.Forge`-shaped
    double exposing the five verbs `bin/fleet-watch`'s six call sites use
    (`get_contents` covers two of them), built from any `(method, path,
    body=None, token=None) -> (status, body)` dispatcher — most often one
    `_api_factory` built, occasionally a closure that wraps one to override a
    single path (`if "/branch_protections" in path: return 403, None`, and
    similar, scattered through this file).

    Each verb method reconstructs the SAME (method, path) shape the
    hand-rolled `_api` this replaces used to build, so the routing table
    inside `_api_factory` — and every closure layered on top of it — needed
    no changes; only the injection point (`_wire`, below) moved from
    `mod._api` to `mod._forge`.

    Also callable with the old four-argument shape (`__call__`), so a
    `_ForgeDouble` can itself be the `dispatch` a closure wraps — the
    dozens of `return _api_factory()(method, path, body, token)` lines in
    this file keep working unchanged.

    `token` is bound on the instance rather than threaded through every verb
    call, exactly as `fleetforge.Forge` binds it at construction: `_wire`'s
    `_forge` builder sets it on each call from the value `bin/fleet-watch`'s
    `_forge(token)` was itself called with, so
    test_the_watcher_never_reads_branch_protections_itself can still see
    which token every call carried."""

    def __init__(self, dispatch, token=None):
        self._dispatch = dispatch
        self.token = token

    def __call__(self, method, path, body=None, token=None):
        return self._dispatch(method, path, body, token if token is not None else self.token)

    def get_branch_protections(self, repo):
        return self("GET", f"/repos/{repo}/branch_protections")

    def list_contents(self, repo, dir, ref=None):
        q = f"?ref={ref}" if ref else ""
        return self("GET", f"/repos/{repo}/contents/{dir}{q}")

    def get_contents(self, repo, path, ref=None):
        q = f"?ref={ref}" if ref else ""
        enc = urllib.parse.quote(path, safe="/")
        return self("GET", f"/repos/{repo}/contents/{enc}{q}")

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        q = f"state={state}&limit={limit}&page={page}"
        if sort:
            q += f"&sort={sort}"
        return self("GET", f"/repos/{repo}/pulls?{q}")

    def list_issue_comments(self, repo, n, since=None, page=1, limit=50):
        # No page/limit in the reconstructed path: the real endpoint has no
        # ListOptions (see _comments_since's docstring) and the pre-migration
        # fixture never sent them either — reproducing that keeps the
        # existing "since=" / "/comments" substring assertions meaningful.
        path = f"/repos/{repo}/issues/{n}/comments"
        if since:
            path += f"?since={urllib.parse.quote(str(since))}"
        return self("GET", path)


def _broker_double(mod, health=(200, {})):
    """Stands in for keyvault's forgejo-broker at the socket boundary.

    It runs the REAL rule — `main_is_protected` plus fleet-repo's `classify`,
    exactly what the deployed broker imports — and formats the answer the way
    the broker's HTTP contract does. So every lane test below still exercises
    the genuine eight lanes; what is faked is the transport, not the judgement.

    Stubbing `_broker_get` rather than `broker_verdict` on purpose: the mapping
    from HTTP status to the three PROT_* codes is the thing ADR-0003 §6 says
    must not collapse, so it stays under test rather than being stubbed over.
    """
    repo_mod = _load_repo_module()

    def _get(path):
        if path == "/health":
            return health
        prefix = "/forgejo/protection/"
        assert path.startswith(prefix), path
        ok, why = mod.main_is_protected(path[len(prefix):], "admintok")
        code, detail = repo_mod.classify(ok, why)
        if code == repo_mod.EXIT_OK:
            return 200, {"ok": True, "determinable": True, "detail": detail}
        if code == repo_mod.EXIT_REFUSED:
            return 200, {"ok": False, "determinable": True,
                         "lane": "some_lane", "detail": detail}
        lane = ("token_rejected" if "rejected the token" in why.lower()
                else "unreadable")
        return 503, {"determinable": False, "lane": lane, "detail": detail}
    return _get


def _load_repo_module():
    ldr = importlib.machinery.SourceFileLoader("fleet_repo", str(BIN / "fleet-repo"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("fleet_repo", ldr))
    ldr.exec_module(m)
    return m


def _wire(mod, api, orch, broker=None):
    # `api` may already be a `_ForgeDouble` (what `_api_factory` now returns)
    # or a plain (method, path, body, token) callable (a hand-written closure)
    # — `_ForgeDouble` is happy wrapping either, since it is itself callable.
    # `mod._forge` is the builder `bin/fleet-watch` calls with the per-cycle
    # token (plan 0051 D1/D2); one double instance is reused across calls, but
    # its bound `token` is refreshed on every `_forge(token)` invocation so a
    # test can still see which token value a given call carried.
    forge = api if isinstance(api, _ForgeDouble) else _ForgeDouble(api)

    def _forge_builder(token):
        forge.token = token
        return forge

    mod._forge = _forge_builder
    mod._token = lambda: ("tok", "")
    # The protections verdict comes from the BROKER now (ADR-0003 §2), so there
    # is no admin token on this host to stub. The double still uses a distinct
    # token value internally, which is what
    # test_the_watcher_holds_no_admin_token_anywhere relies on.
    mod._broker_get = broker if broker is not None else _broker_double(mod)
    os.environ["FLEET_ORCHESTRATOR"] = orch


def _events(fleet_dir, etype=None):
    p = fleet_dir / "events.jsonl"
    if not p.exists():
        return []
    evs = [json.loads(l) for l in p.read_text().splitlines()]
    return [e for e in evs if etype is None or e["type"] == etype]


# ---------------------------------------------------------------- ignition

def test_ready_plan_dispatches_once_then_never_again_while_live(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_WATCH_CAP="1")
    _wire(mod, _api_factory(), orch)
    assert mod.cycle() == 1
    assert len(_wait_spawns(log, 1)) == 1
    line = _spawns(log)[0]
    assert "operator/sniff plans/0009-thing.md" in line and "orch-0009-thing-" in line
    evs = _events(tmp_path / "fleet", "plan-dispatched")
    assert len(evs) == 1 and evs[0]["detail"]["plan"] == "0009-thing"
    assert evs[0]["repo"] == "operator/sniff"
    assert evs[0]["lease"] is None, "plan-* is not a lease-* event (SPEC)"
    assert evs[0]["detail"]["lease"], "the lease id rides in detail instead"
    # second cycle: the lease is live -> zero spawns
    assert mod.cycle() == 0
    assert len(_spawns(log)) == 1


def test_marked_pr_in_any_state_suppresses_dispatch(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    for state in ("open", "closed"):
        fd = tmp_path / f"fleet-{state}"
        mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_WATCH_CAP="1")
        pulls = [{"user": {"login": "implbot"}, "state": state, "number": 5,
                  "body": "does things\n\nPlan: 0009-thing\n"}]
        _wire(mod, _api_factory(pulls=pulls), orch)
        assert mod.cycle() == 0, state          # closed does NOT re-arm
    assert _spawns(log) == []


def test_foreign_authored_marker_does_not_suppress(tmp_path):
    """The marker is reserved for implbot WORK PRs — intakebot never emits it, and
    a plan PR by anyone else must not look like completed work."""
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    pulls = [{"user": {"login": "operator"}, "state": "open", "number": 4,
              "body": "Plan: 0009-thing"}]
    _wire(mod, _api_factory(pulls=pulls), orch)
    assert mod.cycle() == 1


def test_unprotected_repo_refused_at_scan(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    calls = []
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(protected=False, calls=calls), orch)
    assert mod.cycle() == 0
    assert _spawns(log) == []
    assert not any("/contents/" in p for _m, p in calls), \
        "a refused repo is never even read"


def test_unlisted_repo_is_never_touched(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    calls = []
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="")
    _wire(mod, _api_factory(calls=calls), orch)
    assert mod.cycle() == 0
    assert calls == [] and _spawns(log) == []


def test_non_ready_status_is_invisible(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(plan_text=PLAN.replace("status: ready",
                                                   "status: draft")), orch)
    assert mod.cycle() == 0


def test_cap_limits_concurrency_and_release_frees_a_slot(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    two = ("0009-thing.md", "0010-other.md")
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_WATCH_CAP="1")

    def api(method, path, body=None, token=None):
        if "/contents/plans/0010-other.md" in path:
            return 200, _contents_file(PLAN.replace("0009-thing", "0010-other"))
        return _api_factory(plans=two)(method, path, body, token)
    _wire(mod, api, orch)
    assert mod.cycle() == 1, "cap 1 -> exactly one spawn"
    assert mod.cycle() == 0, "still capped while the first lives"
    assert _wait_spawns(log, 1)[0].startswith("operator/sniff plans/0009-thing.md")
    # The real completion sequence: the orchestrator opens its marked PR, then
    # its lease releases. Only THEN is the slot free for the second plan —
    # releasing alone would (correctly) leave plan 1 eligible again.
    done_pr = [{"user": {"login": "implbot"}, "state": "open", "number": 5,
                "body": "Plan: 0009-thing"}]

    def api2(method, path, body=None, token=None):
        if "/contents/plans/0010-other.md" in path:
            return 200, _contents_file(PLAN.replace("0009-thing", "0010-other"))
        return _api_factory(plans=two, pulls=done_pr)(method, path, body, token)
    mod._forge = lambda token: _ForgeDouble(api2, token=token)
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    subprocess.run([sys.executable, str(BIN / "fleet-release"), lid, "--force"],
                   capture_output=True,
                   env=dict(os.environ, EUNOMIA_FLEET_DIR=str(fd),
                            EUNOMIA_SESSION="watch-test"))
    assert mod.cycle() == 1, "freed slot dispatches the SECOND plan"
    lines = _wait_spawns(log, 2)
    assert len(lines) == 2 and "0010-other" in lines[1], lines


# ------------------------------------------ the dispatch cap is configuration (0068)


def _set_dispatch_cap(fd, value, actor="cli:test"):
    fd.mkdir(parents=True, exist_ok=True)   # _load()'s tmp fleet dir may not exist yet
    r = subprocess.run(
        [sys.executable, str(BIN / "fleet-config"), "set", "dispatch_cap", str(value),
         "--actor", actor],
        capture_output=True, text=True,
        env=dict(os.environ, PATH="/usr/bin:/bin", FLEET_DB=str(fd / "fleet.db"),
                 EUNOMIA_FLEET_DIR=str(fd)))
    assert r.returncode == 0, r.stderr
    return r


THREE = ("0009-thing.md", "0010-other.md", "0011-third.md")


def _three_plan_api(**kw):
    def api(method, path, body=None, token=None):
        if "/contents/plans/0010-other.md" in path:
            return 200, _contents_file(PLAN.replace("0009-thing", "0010-other"))
        if "/contents/plans/0011-third.md" in path:
            return 200, _contents_file(PLAN.replace("0009-thing", "0011-third"))
        return _api_factory(plans=THREE, **kw)(method, path, body, token)
    return api


def test_the_table_overrides_the_env_default_and_a_mid_run_raise_needs_no_restart(tmp_path):
    """D3's DoD, literally: `fleet_setting.dispatch_cap` beats the module's
    own default, two live leases defer a third ready plan, and raising the
    row BETWEEN cycles — no restart, same `mod`, same process — lets the next
    cycle dispatch it."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    # No FLEET_WATCH_CAP: the module default (1) is what the table must beat.
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _three_plan_api(), orch)
    _set_dispatch_cap(fd, 2)
    assert mod.cycle() == 2, "fleet_setting.dispatch_cap=2 beats the default of 1"
    _wait_spawns(log, 2)
    assert mod.cycle() == 0, "two live leases at cap 2 defer the third plan"

    _set_dispatch_cap(fd, 3)
    assert mod.cycle() == 1, "raising the cap mid-run dispatches the third plan"
    lines = _wait_spawns(log, 3)
    assert len(lines) == 3


def test_the_env_fallback_behaves_identically_with_no_table(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_WATCH_CAP="2")
    _wire(mod, _three_plan_api(), orch)
    assert not (fd / "fleet.db").exists()
    assert mod.cycle() == 2
    _wait_spawns(log, 2)
    assert mod.cycle() == 0, "third plan deferred at cap 2 (env, no table)"


def test_with_neither_table_nor_env_the_cap_is_one(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _three_plan_api(), orch)
    assert mod.cycle() == 1
    _wait_spawns(log, 1)
    assert mod.cycle() == 0


def test_cap_zero_dispatches_nothing_and_pauses_once_not_per_plan(tmp_path, capsys):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _three_plan_api(), orch)
    _set_dispatch_cap(fd, 0)
    assert mod.cycle() == 0
    out = capsys.readouterr().out
    assert out.count("cap 0 (paused)") == 1, out
    assert _spawns(log) == []


def test_the_cap_source_is_logged_once_per_transition_not_every_cycle(tmp_path, capsys):
    """`fleet.db` absent -> the env fallback is used and the source is logged
    once; a second cycle with nothing changed must not log it again."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_WATCH_CAP="2")
    _wire(mod, _three_plan_api(), orch)
    assert not (fd / "fleet.db").exists()
    mod.cycle()
    first = capsys.readouterr().out
    assert first.count("dispatch cap 2 (source=env)") == 1, first
    mod.cycle()
    second = capsys.readouterr().out
    assert "dispatch cap" not in second, "unchanged resolution must not log again"


# ------------------------------------------- a steward holds no slot (0067)

def _capped_fleet_with_one_lease(tmp_path, log_text):
    """CAP=1, two ready plans, and the first already dispatched — so its lease is
    live and not orphaned. Its run log is then written as `log_text` (None: no
    file at all). What the NEXT cycle does about the second plan is the cap
    check's outcome, which is what the tests below assert."""
    orch, spawns = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_WATCH_CAP="1")
    two = ("0009-thing.md", "0010-other.md")

    def api(method, path, body=None, token=None):
        if "/contents/plans/0010-other.md" in path:
            return 200, _contents_file(PLAN.replace("0009-thing", "0010-other"))
        return _api_factory(plans=two)(method, path, body, token)
    _wire(mod, api, orch)
    assert mod.cycle() == 1
    _wait_spawns(spawns, 1)
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    logf = mod.lib.work_root() / "logs" / f"{lid}.log"
    if log_text is not None:
        logf.parent.mkdir(parents=True, exist_ok=True)
        logf.write_text(log_text)
    return mod, spawns, logf


_T = "2026-09-21T14:53:07Z"
_STEWARD_START = f"{_T} implementer-start runner=x\n{_T} steward-start pr=5 poll=60\n"


def test_a_steward_waiting_on_a_merge_does_not_hold_the_slot(tmp_path):
    mod, spawns, _ = _capped_fleet_with_one_lease(tmp_path, _STEWARD_START)
    assert mod.cycle() == 1, "the steward is not spending, so the plan dispatches"
    assert "0010-other" in _wait_spawns(spawns, 2)[1]


def test_the_same_fleet_was_deferred_before_the_steward_was_excluded(tmp_path):
    """Without this the test above could pass for a reason other than the
    change: it asserts the fixture WAS at cap under the old count (every
    non-orphaned lease), by restoring that count."""
    mod, spawns, _ = _capped_fleet_with_one_lease(tmp_path, _STEWARD_START)
    mod.count_live = lambda inflight: (
        sum(1 for r in inflight.values() if not mod.lib.is_orphaned(r)), 0)
    assert mod.cycle() == 0, "the old count deferred it"
    assert len(_spawns(spawns)) == 1


def test_a_steward_that_resumes_spending_counts_again(tmp_path):
    """D4: after an `unfreeze` grant the orchestrator writes implementer-start,
    which is not a waiting line. No state is kept in the watcher."""
    mod, spawns, logf = _capped_fleet_with_one_lease(tmp_path, _STEWARD_START)
    with open(logf, "a") as fh:
        fh.write(f"{_T} steward-unfreeze comment=9\n"
                 f"{_T} steward-instruction comment=9 granted=True\n"
                 f"{_T} implementer-start runner=x tag=c9\n")
    assert mod.cycle() == 0, "spending again: holds the slot"
    assert len(_spawns(spawns)) == 1


@pytest.mark.parametrize("log_text", [
    None,                                     # no log file
    "",                                       # an empty one
    f"{_T} steward-mystery pr=5\n",           # a line this watcher does not know
    f"{_T} steward-end pr=5 state=merged\n",  # the exit line never qualifies
    f"{_T} steward-grant-spent comment=9\n",  # written after a session that spent
    "garbage\n",
], ids=["no-log", "empty", "unknown-line", "steward-end", "grant-spent",
        "one-token"])
def test_a_lease_whose_state_cannot_be_read_counts_as_live(tmp_path, log_text):
    mod, spawns, _ = _capped_fleet_with_one_lease(tmp_path, log_text)
    assert mod.cycle() == 0, "unknown is live: the cap is a spend throttle"
    assert len(_spawns(spawns)) == 1


def test_a_last_line_without_its_newline_is_still_read(tmp_path):
    """The writer flushes line-by-line, but a read can land before the newline."""
    mod, _, _ = _capped_fleet_with_one_lease(tmp_path, f"{_T} steward-start pr=5")
    assert mod.cycle() == 1


def test_an_unreadable_log_counts_as_live(tmp_path):
    mod, spawns, logf = _capped_fleet_with_one_lease(tmp_path, _STEWARD_START)
    logf.unlink()
    logf.mkdir()                              # open() raises: not a file
    assert mod.cycle() == 0


def test_an_orchestrator_that_will_not_load_counts_as_live(tmp_path):
    """The broker deployment does not ship bin/orchestrator. Loading this file
    there must work and must not un-count anything."""
    mod, spawns, _ = _capped_fleet_with_one_lease(tmp_path, _STEWARD_START)
    def boom():
        raise ImportError("no orchestrator here")
    mod._orchestrator_module = boom
    assert mod.cycle() == 0


def test_only_the_last_line_decides(tmp_path):
    mod, spawns, logf = _capped_fleet_with_one_lease(tmp_path, "")
    logf.write_text(_STEWARD_START.replace("steward-start", "implementer-exit"))
    assert mod.cycle() == 0, "an earlier waiting line does not survive a later one"


def test_an_orphaned_steward_is_not_counted_as_waiting(tmp_path):
    """The orphan rule runs first and owns the lease: the waiting reading is only
    for leases that are not orphaned, so a dead steward is never both."""
    mod, spawns, logf = _capped_fleet_with_one_lease(tmp_path, _STEWARD_START)
    lid = logf.stem
    p = tmp_path / "fleet" / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text())
    rec["created"] = "2020-01-01T00:00:00Z"
    p.write_text(json.dumps(rec))
    live, waiting = mod.count_live(mod.dispatch_leases())
    assert (live, waiting) == (0, 0)


def test_the_waiting_set_is_pinned_and_every_orchestrator_steward_line_is_classified(tmp_path):
    """The literal set, and the emitters it must be complete against.

    `bin/orchestrator` owns the `steward-` namespace. A new emitter fails HERE
    until it is put in one list or the other — waiting on a human (add it to
    `STEWARD_WAITING_PHASES` in bin/fleet-watch) or spending/ended (add it to
    SPENDING_OR_ENDED below). The default for a line nobody classified is that
    it counts, so forgetting is safe at runtime and loud in CI."""
    mod = _load(tmp_path / "fleet")
    assert mod.STEWARD_WAITING_PHASES == {
        "steward-start", "steward-foreign", "steward-unfreeze",
        "steward-followup", "steward-instruction", "steward-refused"}
    SPENDING_OR_ENDED = {
        "steward-end",             # the exit line
        "steward-grant-spent",     # written after a fix session, before its re-review
        "steward-head-unknown",    # likewise
    }
    src = (BIN / "orchestrator").read_text()
    emitted = set(re.findall(r'log\.note\(\s*"(steward-[a-z-]+)"', src))
    assert len(emitted) >= 8, f"the emitter grep found {emitted} — it has rotted"
    assert emitted == mod.STEWARD_WAITING_PHASES | SPENDING_OR_ENDED, (
        f"unclassified: {emitted - (mod.STEWARD_WAITING_PHASES | SPENDING_OR_ENDED)}; "
        f"no longer emitted: {(mod.STEWARD_WAITING_PHASES | SPENDING_OR_ENDED) - emitted}")
    assert not mod.STEWARD_WAITING_PHASES & SPENDING_OR_ENDED


def _deferred_respawn(tmp_path, capsys, live_logs):
    """A dead lease with a valid `respawn` waiting, plus one live lease per entry
    of `live_logs` (each its run-log text). CAP 1. Returns (mod, stderr of one
    cycle) — the respawn defers whenever a live lease is counted."""
    orch, spawns = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3",
                FLEET_WATCH_CAP="1")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    dead = _orphaned_lease(mod, fd)
    for i, text in enumerate(live_logs):
        pid = f"01{i}0-live"
        mod.spawn("operator/sniff", f"plans/{pid}.md", {"id": pid})
        _wait_spawns(spawns, 2 + i)
        lid = [l["id"] for l in mod.lib.all_leases()
               if l.get("note") == f"plan:{pid}"][0]
        logf = mod.lib.work_root() / "logs" / f"{lid}.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        logf.write_text(text)
    mod.notify = lambda t, b: None
    capsys.readouterr()
    mod.cycle()
    return mod, capsys.readouterr().err


def test_the_respawn_deferral_names_what_was_excluded(tmp_path, capsys):
    """One spending lease holds the cap; a second, waiting steward is excluded
    and the warning says so, in the wording the plan fixes."""
    _, err = _deferred_respawn(tmp_path, capsys, [
        f"{_T} implementer-start runner=x\n", _STEWARD_START])
    assert ("respawn for 0009-thing deferred — at cap 1 "
            "(live 1, waiting-on-human 1 excluded)") in err, err


def test_the_respawn_deferral_is_unchanged_when_nothing_was_excluded(tmp_path, capsys):
    _, err = _deferred_respawn(tmp_path, capsys, [f"{_T} implementer-start runner=x\n"])
    assert "respawn for 0009-thing deferred — at cap 1\n" in err, err
    assert "waiting-on-human" not in err


def test_the_cap_summary_carries_the_exclusion_only_when_there_is_one(tmp_path):
    mod = _load(tmp_path / "fleet")
    assert mod.cap_excluded(0) == "" and mod.cap_note(3, 0) == ""
    assert mod.cap_excluded(2) == ", waiting-on-human 2 excluded"
    assert mod.cap_note(1, 1) == " (live 1, waiting-on-human 1 excluded)"


def test_cycle_publishes_the_exclusion_for_the_summary_line(tmp_path):
    mod, _, _ = _capped_fleet_with_one_lease(tmp_path, _STEWARD_START)
    mod.cycle()
    assert mod._cycle_waiting == 1
    with open(mod.lib.work_root() / "logs" / (
            [l["id"] for l in mod.lib.all_leases()][0] + ".log"), "a") as fh:
        fh.write(f"{_T} implementer-start runner=x\n")
    mod.cycle()
    assert mod._cycle_waiting == 0, "and resets when the steward spends again"


# ---------------------------------------------------------------- safety

def test_never_spawns_on_an_unreadable_dedupe_scan(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")

    def api(method, path, body=None, token=None):
        if "/pulls?" in path:
            return 500, None            # cannot prove the plan is undispatched
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    assert mod.cycle() == 0 and _spawns(log) == []


def test_no_token_means_no_scan(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    calls = []
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(calls=calls), orch)
    mod._token = lambda: (None, "token helper produced no token")
    assert mod.cycle() == 0
    assert calls == [], "no token -> not a single API call"


def test_an_unrunnable_orchestrator_is_caught_before_a_lease_is_assigned(tmp_path):
    """A directory is x-accessible, so the os.access gate passes it. Readiness
    catches it instead — better than the old assign-fail-release-page path,
    because nothing is spent to learn it."""
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    unrunnable = tmp_path / "orch-dir"
    unrunnable.mkdir()
    calls = []
    _wire(mod, _api_factory(calls=calls), str(unrunnable))
    mod.notify = lambda t, b: True
    assert mod.cycle() == 0
    assert calls == [], "refuses before touching Forgejo"
    assert [r for r in mod.lib.all_leases()] == [], "no lease was spent"


def test_spawn_failure_releases_the_lease_and_surfaces(tmp_path):
    """The handler still matters after the readiness probe: the orchestrator
    can pass --ready and then be gone by the time the spawn runs (a deploy mid
    cycle). Readiness narrows that window; it does not close it.

    Pinned to FLEET_SPAWN=popen so the failure is injected at a single known
    point; the launchd path's own failure is covered by
    test_a_refusing_launchctl_releases_the_lease below."""
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_SPAWN="popen")
    orch = tmp_path / "vanishes"
    orch.write_text('#!/bin/sh\nexit 0\n')
    orch.chmod(0o755)
    _wire(mod, _api_factory(), str(orch))
    notes = []
    mod.notify = lambda t, b: notes.append(t)
    real_popen = mod.subprocess.Popen

    def _popen(args, **kw):
        # the readiness probe goes through Popen too (subprocess.run wraps it);
        # only the DISPATCH must fail, or this tests the wrong gate
        if args and str(args[0]) == str(orch) and "--ready" not in list(args):
            raise OSError("simulated: replaced between probe and spawn")
        return real_popen(args, **kw)

    mod.subprocess.Popen = _popen
    try:
        assert mod.cycle() == 0
    finally:
        mod.subprocess.Popen = real_popen
    live = [r for r in mod.lib.all_leases() if r.get("state") != "released"]
    assert live == [], "a failed spawn leaves no dangling lease"
    assert _events(fd, "plan-failed"), "plan-failed emitted"
    assert notes, "surfaced via notify"


# ---------------------------------------------------------------- orphans

def _orphaned_lease(mod, fd, plan_id="0009-thing"):
    mod.spawn("operator/sniff", f"plans/{plan_id}.md", {"id": plan_id})
    # the stub logs from a detached child; make this helper's one spawn
    # observable before returning, so every caller's `before` is deterministic
    _wait_spawns(fd.parent / "spawns.log", 1, timeout=1.5)
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    p = fd / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text())
    rec["created"] = "2020-01-01T00:00:00Z"      # blew the activate timeout
    p.write_text(json.dumps(rec))
    return lid


def test_dead_orchestrator_surfaces_and_never_auto_respawns(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    _wire(mod, _api_factory(pulls=pulls), orch)
    _orphaned_lease(mod, fd)
    before = len(_spawns(log))
    notes = []
    mod.notify = lambda t, b: notes.append(t)
    assert mod.cycle() == 0, "staleness alone NEVER spawns (Principle 4)"
    assert len(_spawns(log)) == before
    assert any("dead" in n.lower() for n in notes)


def test_respawn_comment_from_the_pinned_uid_spawns_exactly_one(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 99}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"},          # wrong uid
                {"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]          # newer than dispatch
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    _orphaned_lease(mod, fd)
    before = len(_wait_spawns(log, 1))    # the original dispatch is detached
    mod.notify = lambda t, b: None
    assert mod.cycle() == 1
    assert len(_wait_spawns(log, before + 1)) == before + 1, "exactly one successor"


def test_respawn_survives_a_client_signature_line(tmp_path):
    """Minos appends `_via minos (<id>.access)_` to every comment it posts, so
    the owner's first real respawn (eunomia#487, 2026-09-21) read
    `Respawn\n\n_via minos (...)_` and a whole-body equality never matched.
    The instruction is a whole line, anywhere in the body — `_UNFREEZE_RE`'s
    shape — and a signature on another line must not defeat it."""
    mod = _load(tmp_path / "fleet")
    assert mod._RESPAWN_RE.search("Respawn\n\n_via minos (2d54cf11968f08fb.access)_")
    assert mod._RESPAWN_RE.search("`respawn`")
    assert mod._RESPAWN_RE.search("  respawn  ")


def test_respawn_must_stand_alone_on_its_line(tmp_path):
    """A sentence that mentions the word is not the instruction: "please
    respawn it" would let a human aside authorise spend."""
    mod = _load(tmp_path / "fleet")
    assert not mod._RESPAWN_RE.search("please respawn it")
    assert not mod._RESPAWN_RE.search("respawn tomorrow")
    assert not mod._RESPAWN_RE.search("re-spawn")


def test_respawn_requires_a_numeric_uid_not_a_name(tmp_path):
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3, "login": "operator"}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    _orphaned_lease(mod, fd)
    before = len(_spawns(log))
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0, "no pinned uid -> respawn disabled, never name-matched"
    assert len(_spawns(log)) == before


def test_stale_respawn_comment_never_rearms(tmp_path):
    """r1 H1: a `respawn` is consumed by the dispatch it triggers, not a
    standing grant — an OLD comment must not auto-respawn a later orphaning
    (that is an unbounded automatic retry, which Principle 4 forbids)."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2019-01-01T00:00:00Z"}]     # OLDER than dispatch
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    _orphaned_lease(mod, fd)             # lease created "2020-01-01"
    _wait_spawns(log, 1)                 # the original dispatch is detached
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0, "a pre-dispatch comment is not authorisation"
    assert len(_spawns(log)) == 1, "only the original dispatch ran"


def test_respawn_retires_the_dead_lease(tmp_path):
    """r1 H1: two live leases sharing note plan:<id> collapse in
    dispatch_leases() and make row 6's pre-push hook refuse the SUCCESSOR."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    dead = _orphaned_lease(mod, fd)
    mod.notify = lambda t, b: None
    assert mod.cycle() == 1
    live = [r for r in mod.lib.all_leases() if r.get("state") != "released"]
    assert len(live) == 1, f"exactly one live dispatch lease, got {live}"
    assert live[0]["id"] != dead, "the dead lease was retired, not left behind"
    # r12: this assertion used to be credited to the freshness comparison, but
    # the fixture comment is dated 2030 — NEWER than the successor's `created` —
    # so a second respawn is refused here only because the successor is alive
    # and therefore not orphaned. The test passed without exercising the
    # comparison at all. Pin the property the comment claims by making the
    # successor look dead too: only then does the comment-vs-lease timestamp
    # decide, and a 2030 comment SHOULD authorise one more respawn.
    assert mod.cycle() == 0, "a live successor is not orphaned; nothing to respawn"

    # Age the successor out the same way _orphaned_lease does, so the ONLY
    # thing left deciding is the comment-vs-lease comparison.
    succ = fd / "leases" / f"{live[0]['id']}.json"
    rec = json.loads(succ.read_text())
    rec["created"] = "2020-01-01T00:00:00Z"
    succ.write_text(json.dumps(rec))
    assert mod.cycle() == 1, \
        "successor dead and the 2030 comment is newer than its lease: respawn"


def test_dry_run_does_not_spawn_on_the_respawn_path(tmp_path):
    """r1 H2: --dry-run must be dry on EVERY path — the operator running it is
    usually debugging exactly the dead-orchestrator case that arms respawn."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    _orphaned_lease(mod, fd)
    before = len(_spawns(log))
    mod.notify = lambda t, b: None
    assert mod.cycle(dry_run=True) == 0
    assert len(_spawns(log)) == before, "--dry-run assigned and spawned nothing"


def test_prefix_plan_id_is_not_suppressed_by_a_longer_sibling(tmp_path):
    """r1 M2: `Plan: 0009-auth-v2` in a body must not make plan `0009-auth`
    look permanently done — silent loss of a merged, ready plan."""
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    short = PLAN.replace("0009-thing", "0009-auth")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 9,
              "body": "work\n\nPlan: 0009-auth-v2\n"}]
    _wire(mod, _api_factory(plan_text=short, pulls=pulls), orch)
    assert mod.cycle() == 1, "the sibling's marker must not suppress this plan"


def test_weak_protection_rules_are_refused(tmp_path):
    """r1 M1: a rule EXISTING is not the property — main must be writable only
    by a reviewed merge."""
    orch, log = _stub_orchestrator(tmp_path)
    for bp, why in (
            (_bp(required_approvals=0), "0 approvals"),
            (_bp(enable_push=True), "unrestricted direct push")):
        mod = _load(tmp_path / f"fleet-{why[:3]}", FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _bp=bp):
            if "/branch_protections" in path:
                return 200, [_bp]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == 0, why
    # whitelisted push + approvals IS acceptable
    mod = _load(tmp_path / "fleet-ok", FLEET_WATCH_REPOS="operator/sniff")

    def api_ok(method, path, body=None, token=None):
        if "/branch_protections" in path:
            # 2 approvals with revbot AND operator whitelisted: the
            # shape forgejo-create-repo.sh installs on every repo. One agent
            # cannot reach 2, so a human approval is structurally mandatory —
            # which is exactly what makes it acceptable under ADR-0004 as
            # revised. The old fixture required 2 while whitelisting only
            # operator, which no one could ever satisfy; the sufficiency check
            # noticed and this is the config it was meant to describe.
            return 200, [_bp(required_approvals=2, enable_push=True,
                             enable_push_whitelist=True,
                             push_whitelist_usernames=["operator"],
                             approvals_whitelist_username=["revbot",
                                                           "operator"])]
        return _api_factory()(method, path, body, token)
    _wire(mod, api_ok, orch)
    assert mod.cycle() == 1


def test_respawn_reads_the_real_plan_path_and_zone(tmp_path):
    """r1 M3: nothing binds front-matter id to the filename — a fabricated
    plans/<id>.md hands the successor a nonexistent path and a BLANK zone,
    which reads as 'unconstrained' rather than 'refuse'."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    odd = PLAN.replace("id: 0009-thing", "id: 0009-renamed")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-renamed"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(plan_text=odd, pulls=pulls, comments=comments), orch)
    mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-renamed"})
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    p = fd / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text()); rec["created"] = "2020-01-01T00:00:00Z"
    p.write_text(json.dumps(rec))
    mod.notify = lambda t, b: None
    assert mod.cycle() == 1
    last = _wait_spawns(log, 2)[-1]
    assert "plans/0009-thing.md" in last, \
        f"successor got the REAL path from main, not plans/<id>.md: {last}"


def test_dead_orchestrator_notifies_once_not_every_cycle(tmp_path):
    """r1 low: nothing releases an orphaned dispatch lease, so an unguarded
    notify pages forever (the fleet-status O_EXCL marker precedent)."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(pulls=[]), orch)
    _orphaned_lease(mod, fd)
    notes = []
    mod.notify = lambda t, b: notes.append(t)
    for _ in range(3):
        mod.cycle()
    assert len(notes) == 1, f"one page, not one per cycle: {notes}"


def _armed_sweep(tmp_path, mod, fd, comments=None, pulls=None, orch=None):
    """An orphaned dispatch lease with an open marked PR + fresh respawn."""
    _orphaned_lease(mod, fd)
    return fd


def test_sweep_obeys_the_allowlist(tmp_path):
    """r2 H1: the sweep's repo comes from a lease FILE, not the allowlist —
    'never read at all' has to hold on BOTH dispatch paths."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    calls = []
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments, calls=calls), orch)
    _orphaned_lease(mod, fd)
    _wait_spawns(log, 1)
    # the repo leaves the allowlist while its lease is still orphaned
    os.environ["FLEET_WATCH_REPOS"] = "operator/other"
    mod.notify = lambda t, b: None
    calls.clear()
    assert mod.cycle() == 0, "a delisted repo is never respawned"
    assert not any("operator/sniff" in p for _m, p in calls), \
        f"a delisted repo is never READ either: {calls}"


def test_protection_lapse_refuses_respawn(tmp_path):
    """r2 H1: protection can lapse between dispatch and respawn, and the
    successor reads its plan from THAT main."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    _orphaned_lease(mod, fd)
    before = len(_wait_spawns(log, 1))
    mod._forge = lambda token: _api_factory(protected=False, pulls=pulls, comments=comments)
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0
    assert len(_spawns(log)) == before, "no successor onto an unprotected main"


def test_respawn_comment_with_a_server_offset_is_compared_in_utc(tmp_path):
    """r2 M1: Forgejo stamps the SERVER's offset. Truncating to 19 chars made
    an Eastern-server comment read ~5h old, so the documented recovery did
    nothing — silently."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    # 08:01-04:00 == 12:01Z — one minute AFTER the dispatch, though its naive
    # digits look four hours older
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2020-01-01T08:01:00-04:00"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-thing"})
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    p = fd / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text())
    rec["created"] = "2020-01-01T12:00:00Z"
    p.write_text(json.dumps(rec))
    mod.notify = lambda t, b: None
    assert mod.cycle() == 1, "an offset timestamp one minute later IS fresh"


def test_unparsable_lease_created_refuses_respawn(tmp_path):
    """r2 L1: no time bound must mean REFUSE, never 'standing grant'."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-thing"})
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    p = fd / "leases" / f"{lid}.json"
    rec = json.loads(p.read_text()); rec["created"] = "not-a-timestamp"
    p.write_text(json.dumps(rec))
    before = len(_wait_spawns(log, 1))
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0
    assert len(_spawns(log)) == before


def test_dedupe_pages_over_a_stable_sort(tmp_path):
    """r2 M2: recentupdate reorders under concurrent updates, so a marked PR
    can slide into the already-scanned region and vanish — dispatching a
    duplicate for done work."""
    orch, log = _stub_orchestrator(tmp_path)
    seen = []
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")

    def api(method, path, body=None, token=None):
        if "/pulls?" in path:
            seen.append(path)
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    mod.cycle()
    assert seen and all("sort=oldest" in p for p in seen), seen
    assert not any("recentupdate" in p for p in seen)


def test_every_matching_rule_must_pass(tmp_path):
    """r2 M4: Forgejo's own precedence decides which rule governs main, so a
    strong glob must not vouch for a weak exact rule."""
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")

    def api(method, path, body=None, token=None):
        if "/branch_protections" in path:
            return 200, [_bp(rule_name="**", required_approvals=2,
                             enable_push=True, enable_push_whitelist=True,
                             push_whitelist_usernames=["operator"]),
                         _bp(enable_push=True)]          # weak: open pushes
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    assert mod.cycle() == 0, "the weak exact rule must refuse the repo"


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod cannot deny root; the cihost-linux job container runs as uid 0, so this permission-denied path is only testable on the host label")
def test_lease_assign_failure_uses_the_failure_channel(tmp_path):
    """r2 M3: the doc promises plan-failed + a push here; stderr alone would
    stall all dispatch invisibly."""
    ro = tmp_path / "readonly"
    ro.mkdir()
    fd = ro / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    orch, log = _stub_orchestrator(tmp_path)
    _wire(mod, _api_factory(), orch)
    notes = []
    mod.notify = lambda t, b: notes.append(t)
    ro.chmod(0o555)                       # fleet-claim cannot create the tree
    try:
        assert mod.spawn("operator/sniff", "plans/0009-thing.md",
                         {"id": "0009-thing"}) is None
    finally:
        ro.chmod(0o755)
    assert notes and "failed" in notes[0].lower(), notes


def test_fleet_claim_stdout_is_exactly_the_lease_id(tmp_path):
    """r2 L4: the watcher reads stdout line 1 as the lease id — pin that
    contract on fleet-claim's side so a future banner cannot become an id."""
    r = subprocess.run(
        [sys.executable, str(BIN / "fleet-claim"), "--assign",
         '{"type":"branch","repo":"operator/sniff","branch":"feat/x"}',
         "--holder", "h"],
        capture_output=True, text=True,
        env=dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path / "f"),
                 EUNOMIA_SESSION="pin"))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("branch--"), r.stdout


def test_cross_repo_same_plan_id_does_not_collide(tmp_path):
    """r3 M1: ids are per-repo sequential, so two repos both carrying
    `0009-thing` shared a key — and a collision HID one lease from both the
    cap count and the orphan sweep (silent stall, no page, ever)."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff,operator/other",
                FLEET_WATCH_CAP="2")

    def api(method, path, body=None, token=None):
        # each repo's plan declares ITS OWN repo — cycle correctly skips a
        # plan whose front matter names a different one
        who = "operator/other" if "operator/other" in path else "operator/sniff"
        if "/contents/plans/" in path:
            return 200, _contents_file(PLAN.replace("operator/sniff", who))
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    assert mod.cycle() == 2, "the same id in two repos is two dispatches"
    keys = set(mod.dispatch_leases())
    assert keys == {("operator/sniff", "0009-thing"),
                    ("operator/other", "0009-thing")}, keys
    assert mod.cycle() == 0, "and both are then correctly seen as in-flight"


def test_second_watcher_refuses_to_run(tmp_path):
    """r3 M4: the doc promised single-instance authority that did not exist —
    two concurrent cycles could each snapshot `inflight` and double-dispatch."""
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    orch, log = _stub_orchestrator(tmp_path)
    _wire(mod, _api_factory(), orch)
    held = mod._single_instance()
    assert held is not None
    try:
        r = subprocess.run([sys.executable, str(BIN / "fleet-watch")],
                           capture_output=True, text=True,
                           env=dict(os.environ, EUNOMIA_FLEET_DIR=str(fd),
                                    FLEET_WATCH_REPOS="operator/sniff"))
        assert "another fleet-watch holds the cycle lock" in r.stderr, r.stderr
        assert _spawns(log) == [], "the second watcher dispatched nothing"
    finally:
        os.close(held)


def test_failed_respawn_leaves_the_plan_recoverable(tmp_path):
    """r3 M3: retire-before-assign stranded the plan — no lease meant the
    sweep never saw it again and `respawn` was permanently inert, silently."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    dead = _orphaned_lease(mod, fd)
    _wait_spawns(log, 1)
    notes = []
    mod.notify = lambda t, b: notes.append(t)
    mod.spawn = lambda *a, **k: None          # the successor's assign fails
    assert mod.cycle() == 0
    still = [r["id"] for r in mod.lib.all_leases()
             if r.get("state") in ("assigned", "active")]
    assert dead in still, "the old lease survives, so the plan stays visible"
    assert any("respawn" in n.lower() for n in notes), notes


def test_page_cap_exhaustion_pages_once(tmp_path):
    """r3 M2: outgrowing the bound stalls dispatch PERMANENTLY, and the
    runbook's own diagnostic misdirects — it must reach the failure channel."""
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_PR_PAGE_CAP="2")

    def api(method, path, body=None, token=None):
        if "/pulls?" in path:                 # every page full, never empty
            return 200, [{"user": {"login": "x"}, "state": "open",
                          "number": 1, "body": ""}] * 50
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    notes = []
    mod.notify = lambda t, b: notes.append(t)
    for _ in range(3):
        mod.cycle()
    assert _spawns(log) == [], "never spawn on a truncated scan"
    assert len([n for n in notes if "stalled" in n.lower()]) == 1, notes


def test_respawn_is_found_in_one_request_on_a_long_thread(tmp_path):
    """r10 H1: this replaces a test that asserted paging Forgejo does not do.

    `ListIssueComments` declares no ListOptions — `page` is ignored and the
    whole thread comes back on every call. The old loop therefore re-fetched
    the same list until PAGE_CAP and returned None, so respawn could never
    succeed on any PR that had a comment. The old test passed because the
    fixture paged; it was testing the fixture."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    # a long thread, with the authorising comment last — where a respawn is
    comments = ([{"user": {"id": 3}, "body": "looks good",
                  "created_at": "2030-01-01T00:00:00Z"}] * 120
                + [{"user": {"id": 3}, "body": "respawn",
                    "created_at": "2030-01-02T00:00:00Z"}])
    calls = []
    _wire(mod, _api_factory(pulls=pulls, comments=comments, calls=calls), orch)
    _orphaned_lease(mod, fd)
    before = len(_wait_spawns(log, 1))
    mod.notify = lambda t, b: None

    assert mod.cycle() == 1, "the respawn must be found in a single fetch"
    assert len(_wait_spawns(log, before + 1)) == before + 1

    comment_calls = [p for _, p in calls if "/comments" in p]
    assert len(comment_calls) == 1, f"one request, not a paging loop: {comment_calls}"
    assert "since=" in comment_calls[0], "the time bound is pushed to the server"


def test_undelivered_page_does_not_consume_the_one_shot(tmp_path):
    """r4 M3: a brief ntfy outage would permanently swallow the most important
    page in the system — nothing re-arms a dead orchestrator's lease."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(pulls=[]), orch)
    _orphaned_lease(mod, fd)
    attempts = []

    def flaky(title, body):
        attempts.append(title)
        return False                       # delivery failed
    mod.notify = flaky
    mod.cycle()
    mod.cycle()
    assert len(attempts) == 2, "an undelivered page is retried next cycle"
    ok = []
    mod.notify = lambda t, b: (ok.append(t), True)[1]
    mod.cycle()
    mod.cycle()
    assert len(ok) == 1, "once delivered, it is consumed"


def test_unknown_flag_is_refused(tmp_path):
    """r4 M4: `--dryrun` silently ran a fully ARMED cycle."""
    fd = tmp_path / "fleet"
    r = subprocess.run([sys.executable, str(BIN / "fleet-watch"), "--dryrun"],
                       capture_output=True, text=True,
                       env=dict(os.environ, EUNOMIA_FLEET_DIR=str(fd),
                                FLEET_WATCH_REPOS=""))
    assert r.returncode != 0 and "unrecognized arguments" in r.stderr


def test_branch_already_leased_for_another_plan_refuses(tmp_path):
    """r4 low: two distinct ids normalising to one slug would put two
    orchestrators on one branch."""
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    orch, log = _stub_orchestrator(tmp_path)
    _wire(mod, _api_factory(), orch)
    assert mod.spawn("operator/sniff", "plans/a.md", {"id": "0009-thing"})
    assert mod.spawn("operator/sniff", "plans/b.md", {"id": "0009_thing"}) is None, \
        "same slug, different plan id -> refused"


def test_refuses_to_arm_without_a_runnable_orchestrator(tmp_path):
    """r5 M2: part 1 ships without bin/orchestrator — arming as-written would
    assign, force-release, emit and PAGE every cycle, forever."""
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff",
                FLEET_ORCHESTRATOR=str(tmp_path / "nope"))
    calls = []
    _wire(mod, _api_factory(calls=calls), str(tmp_path / "nope"))
    notes = []
    mod.notify = lambda t, b: (notes.append(t), True)[1]
    for _ in range(3):
        assert mod.cycle() == 0
    assert calls == [], "refuses BEFORE touching Forgejo"
    assert len(notes) == 1, f"pages once, not per cycle: {notes}"
    assert [r for r in mod.lib.all_leases()] == [], "no lease churn"


def test_respawn_refuses_an_abandoned_plan(tmp_path):
    """r5 M1: a fresh `respawn` can outlive the human's kill gesture — comment,
    defer at cap, then `status: abandoned`. The last human act said STOP."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    _orphaned_lease(mod, fd)
    before = len(_wait_spawns(log, 1))
    # the human abandons the plan AFTER commenting respawn
    mod._forge = lambda token: _api_factory(pulls=pulls, comments=comments,
                                            plan_text=PLAN.replace("status: ready",
                                                                   "status: abandoned"))
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0, "abandoned means abandoned"
    assert len(_spawns(log)) == before


def test_agent_on_the_push_whitelist_is_refused(tmp_path):
    """r5 M3: an agent that can push main can push its own `status: ready`
    plan and SELF-IGNITE, while this check vouches for the repo."""
    orch, log = _stub_orchestrator(tmp_path)
    for wl, ok in ((["operator"], 1), (["operator", "implbot"], 0), (None, 0)):
        mod = _load(tmp_path / f"fleet-{len(wl or [])}-{ok}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _wl=wl):
            if "/branch_protections" in path:
                bp = _bp(enable_push=True, enable_push_whitelist=True)
                if _wl is not None:
                    bp["push_whitelist_usernames"] = _wl
                return 200, [bp]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == ok, f"whitelist={wl}"


def test_brace_alternation_rule_is_seen(tmp_path):
    """r5 low: Forgejo globs with gobwas (brace alternation); fnmatch does not,
    so a weak `{main,master}` rule was invisible while a strong rule vouched."""
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")

    def api(method, path, body=None, token=None):
        if "/branch_protections" in path:
            return 200, [_bp(rule_name="*", required_approvals=2),
                         _bp(rule_name="{main,master}",
                             enable_push=True)]         # weak, brace-named
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    assert mod.cycle() == 0, "the brace rule must be seen and must refuse"


def test_all_repos_unreadable_pages_once(tmp_path):
    """r5 low: the macOS Local Network Privacy signature (the cihost SPIRE
    incident) — every read returns nothing and the runbook misdirects."""
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")

    def api(method, path, body=None, token=None):
        return 0, None                       # connection refused, every call
    _wire(mod, api, orch)
    notes = []
    mod.notify = lambda t, b: (notes.append(t), True)[1]
    for _ in range(3):
        mod.cycle()
    assert len([n for n in notes if "blind" in n.lower()]) == 1, notes


# --------------------------------------------------------------------------
# r6: main_is_protected vouched on PARTIAL evidence — it refused three write
# paths onto main and was silent on three others in the same API response.
# --------------------------------------------------------------------------

def test_unprotected_file_patterns_covering_plans_is_refused(tmp_path):
    """r6 M1: `unprotected_file_patterns` exempts matching paths from the push
    restrictions — that is the field's purpose. A `**/*.md` "don't gate docs"
    rule exempts plans/*.md, which IS the ignition file."""
    orch, log = _stub_orchestrator(tmp_path)
    for pats, ok in (("**/*.md", 0),          # covers plans/ — self-ignition
                     ("*.md", 0),             # gobwas `*` crosses `/`
                     ("plans/*.md", 0),       # the ignition file, named
                     ("docs/**;CHANGELOG.md", 1),   # cannot reach plans/
                     ("", 1)):
        mod = _load(tmp_path / f"fleet-{abs(hash(pats)) % 10000}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _p=pats):
            if "/branch_protections" in path:
                return 200, [_bp(unprotected_file_patterns=_p)]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == ok, f"unprotected_file_patterns={pats!r}"


def test_unreadable_unprotected_patterns_cannot_vouch(tmp_path):
    """Unknown shape is refusal, like every other unreadable field here."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")

    def api(method, path, body=None, token=None):
        if "/branch_protections" in path:
            return 200, [_bp(unprotected_file_patterns={"weird": "shape"})]
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    assert mod.cycle() == 0


def test_an_agent_authored_approval_cannot_ignite(tmp_path):
    """r6 M2: `required_approvals >= 1` said an approval was REQUIRED, never
    who may give one. Without the whitelist Forgejo counts an approval from any
    write user — which implbot and revbot both have — so an agent could
    approve a work PR that also adds a `status: ready` plan, and ignition would
    never involve a human."""
    orch, _ = _stub_orchestrator(tmp_path)
    for over, ok in (
            ({"enable_approvals_whitelist": False}, 0),      # anyone with write
            ({"approvals_whitelist_username": ["operator", "implbot"]}, 0),
            ({"approvals_whitelist_username": ["revbot"]}, 0),
            ({"approvals_whitelist_username": None}, 0),     # unreadable
            ({"approvals_whitelist_teams": ["reviewers"]}, 0),  # not visible
            ({"approvals_whitelist_username": ["operator"]}, 1)):
        mod = _load(tmp_path / f"fleet-a{abs(hash(str(over))) % 10000}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _o=over):
            if "/branch_protections" in path:
                return 200, [_bp(**_o)]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == ok, f"approvals lane: {over}"


def test_an_unrestricted_merge_cannot_ignite(tmp_path):
    """r6 M2: nothing restricted WHO merges, and this fleet runs an automated
    merge queue — so a reviewed-by-an-agent PR could reach main with no human
    in the chain while this function vouched for it."""
    orch, _ = _stub_orchestrator(tmp_path)
    for over, ok in (
            ({"enable_merge_whitelist": False}, 0),
            ({"merge_whitelist_usernames": ["implbot"]}, 0),
            ({"merge_whitelist_usernames": None}, 0),
            ({"merge_whitelist_teams": ["mergers"]}, 0),
            ({"merge_whitelist_usernames": ["operator"]}, 1)):
        mod = _load(tmp_path / f"fleet-m{abs(hash(str(over))) % 10000}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _o=over):
            if "/branch_protections" in path:
                return 200, [_bp(**_o)]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == ok, f"merge lane: {over}"


def test_deploy_keys_and_teams_on_the_push_whitelist_are_refused(tmp_path):
    """r6 M3: the push whitelist was read as usernames only. A write-scoped
    deploy key held by agent tooling pushes main just as directly, and team
    membership is not visible in this response."""
    orch, _ = _stub_orchestrator(tmp_path)
    base = dict(enable_push=True, enable_push_whitelist=True,
                push_whitelist_usernames=["operator"])
    for over, ok in (({"push_whitelist_deploy_keys": True}, 0),
                     ({"push_whitelist_teams": ["infra"]}, 0),
                     ({"push_whitelist_deploy_keys": False}, 1)):
        mod = _load(tmp_path / f"fleet-k{abs(hash(str(over))) % 10000}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _o=over):
            if "/branch_protections" in path:
                return 200, [_bp(**dict(base, **_o))]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == ok, f"push lane: {over}"


def test_a_misconfigured_repo_is_not_counted_as_blind(tmp_path):
    """r6 low: `"unreadable" in why` also matched the whitelist-membership
    refusals, so a REACHABLE but oddly-configured repo counted toward "the
    watcher is blind" and could fire the wrong page."""
    orch, _ = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    pages = []

    def api(method, path, body=None, token=None):
        if "/branch_protections" in path:
            # reachable, answers fine, but its membership cannot be read
            return 200, [_bp(enable_push=True, enable_push_whitelist=True,
                             push_whitelist_usernames=None)]
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    mod.notify = lambda t, b: pages.append(t)
    assert mod.cycle() == 0
    assert not any("blind" in t.lower() for t in pages), \
        "a configured-but-refused repo is not a blind watcher"


def test_unreadable_comments_refuse_rather_than_decide(tmp_path):
    """Replaces the PAGE_CAP-truncation test: with one unpaginated request
    there is no truncation to detect, but an unreadable fetch must still
    refuse rather than read as "no respawn"."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]

    def api(method, path, body=None, token=None):
        if "/comments" in path:
            return 500, None
        return _api_factory(pulls=pulls)(method, path, body, token)
    _wire(mod, api, orch)
    _orphaned_lease(mod, fd)
    before = len(_wait_spawns(log, 1))
    mod.notify = lambda t, b: None

    assert mod.cycle() == 0, "an unreadable fetch must refuse"
    assert len(_spawns(log)) == before, "no successor on unreadable evidence"


def test_dry_run_does_not_rearm_pages(tmp_path):
    """r6 low: the recovered-lease branch cleared its marker unconditionally,
    so --dry-run mutated state the help text promises it never touches."""
    orch, _ = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(), orch)
    lid = mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-thing"})
    lid = [l["id"] for l in mod.lib.all_leases()][0]
    marker = mod.lib.leases_dir() / f"{lid}.watch-notified"
    marker.write_text("")                     # a page already consumed
    mod.cycle(dry_run=True)
    assert marker.exists(), "--dry-run must not clear a one-shot marker"


def test_dry_run_reports_the_collision_an_armed_run_would_hit(tmp_path):
    """r6 low: spawn() returned before the branch-collision check, so dry-run
    promised a dispatch that an armed run refuses — misleading precisely the
    operator using dry-run to find out what would happen."""
    orch, _ = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(), orch)
    # a live lease on the branch this plan's slug would claim, for another plan
    mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-thing"})
    # a DISTINCT id that normalises to the same slug — the r4-low collision
    assert mod.slug_of("0009_thing") == mod.slug_of("0009-thing")
    assert mod.spawn("operator/sniff", "plans/0009_thing.md",
                     {"id": "0009_thing"}, dry_run=True) is None, \
        "dry-run must refuse what an armed run refuses"


def test_unreadable_plans_during_respawn_says_so(tmp_path):
    """r6 low: `plan_files(...) or []` made a transient 500 indistinguishable
    from a deleted plan, sending the operator after a missing file."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    _wire(mod, _api_factory(), orch)
    _orphaned_lease(mod, fd)
    assert mod._plan_by_id("operator/sniff", "0009-thing", "tok")[0] \
        == "plans/0009-thing.md"

    def api_500(method, path, body=None, token=None):
        if path.endswith("/contents/plans?ref=main"):
            return 500, None
        return _api_factory()(method, path, body, token)
    mod._forge = lambda token: _ForgeDouble(api_500, token=token)
    assert mod._plan_by_id("operator/sniff", "0009-thing", "tok") \
        == ("unreadable", None), "unreadable must not read as absent"


def test_a_dispatch_failure_can_page_again_after_recovery(tmp_path):
    """r6 low: the synthetic fail-* keys are excluded from _sweep_markers by
    design and nothing cleared them, so a SECOND failure after an intervening
    recovery paged never, forever."""
    orch, _ = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(), orch)
    # r12: built via _marker_key rather than spelled out, because the key now
    # carries a digest of the (repo, id) pair — a hardcoded literal here would
    # pass by constructing a file the code never looks at, which is exactly the
    # class of test this suite has already been caught shipping twice.
    key = mod._safe_lease_key(
        mod._marker_key("fail-spawn", "operator/sniff", "0009-thing"))
    marker = mod.lib.leases_dir() / f"{key}.watch-notified"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")                     # an earlier failure paged once
    mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-thing"})
    assert not marker.exists(), \
        "a completed dispatch is the recovery that re-arms the page"


# --------------------------------------------------------------------------
# r7: the lanes the r6 pass still left open, and a fail-OPEN bug in the guard
# r6 added.
# --------------------------------------------------------------------------

def test_a_stale_approval_cannot_ignite(tmp_path):
    """r7 M1: both stale-approval fields default FALSE in Forgejo, so the
    common config lets operator's approval of one tree satisfy a LATER head that
    added a `status: ready` plan. The approval is genuinely human — of a
    different tree."""
    orch, _ = _stub_orchestrator(tmp_path)
    for over, ok in (
            ({"dismiss_stale_approvals": False,
              "ignore_stale_approvals": False}, 0),
            ({"dismiss_stale_approvals": None}, 0),        # not reported
            ({"dismiss_stale_approvals": False,
              "ignore_stale_approvals": True}, 1),
            ({"dismiss_stale_approvals": True}, 1)):
        mod = _load(tmp_path / f"fleet-s{abs(hash(str(over))) % 10000}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _o=over):
            if "/branch_protections" in path:
                return 200, [_bp(**_o)]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == ok, f"stale approvals: {over}"


def test_multi_group_and_nested_braces_fail_closed(tmp_path):
    """r7 M3: one-level expansion left a literal `{` that fnmatch never
    matches, so these patterns covered the ignition file while the guard saw
    nothing — fail-OPEN, the opposite of the docstring's claim."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    probe = "plans/0001-probe.md"
    for pat in ("{plans,docs}/{0001,0002}*.md",
                "{plans/*,docs/*}.{md,txt}",
                "{plans/**,{docs,notes}/**}",
                "plans/{0001,{0002,0003}}*.md"):
        assert mod._glob_hits(pat, probe), f"{pat} must be seen to cover {probe}"
    # unparseable answers TRUE — cannot vouch, at both call sites
    assert mod._glob_hits("plans/{unbalanced.md", probe)
    assert mod._expand_braces("plans/{unbalanced.md") is None
    # and it still says NO to patterns that genuinely miss
    assert not mod._glob_hits("{docs,notes}/*.md", probe)


def test_a_renamed_implementer_does_not_re_ignite_every_plan(tmp_path):
    """r7 M2: dedupe gates SPEND and was bound to the literal login. A rename
    made every historical marked PR invisible, so every merged-and-still-ready
    plan read as undispatched and re-dispatched, silently."""
    orch, log = _stub_orchestrator(tmp_path)
    pulls = [{"user": {"login": "implbot-2", "id": 42}, "state": "open",
              "number": 7, "body": "Plan: 0009-thing"}]
    # bound to a uid: the rename is invisible to the check, dedupe still holds
    mod = _load(tmp_path / "fleet-uid", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_IMPLEMENTER_UIDS="42")
    _wire(mod, _api_factory(pulls=pulls), orch)
    assert mod.cycle() == 0, "a uid-bound dedupe survives a rename"
    # or by configured login, so an existing deployment keeps working
    mod = _load(tmp_path / "fleet-login", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_IMPLEMENTER_LOGINS="implbot-2")
    _wire(mod, _api_factory(pulls=pulls), orch)
    assert mod.cycle() == 0, "a configured login works too"
    # unconfigured, the old hardcoded name no longer matches -> re-dispatch,
    # which is exactly the silent spend this finding is about
    mod = _load(tmp_path / "fleet-bare", FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(pulls=pulls), orch)
    assert mod.cycle() == 1, "documents the cost of leaving it unconfigured"


def test_agent_accounts_are_env_extendable(tmp_path):
    """r7 L5: a closed two-name set vouched for every other automated account
    — and the merge queue necessarily runs as one."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_AGENT_ACCOUNTS="intakebot,queuebot")
    assert "intakebot" in mod.AGENT_ACCOUNTS and "implbot" in mod.AGENT_ACCOUNTS

    def api(method, path, body=None, token=None):
        if "/branch_protections" in path:
            return 200, [_bp(merge_whitelist_usernames=["queuebot"])]
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    assert mod.cycle() == 0, "a configured agent account must refuse"


def test_a_non_ascii_plan_filename_does_not_kill_the_cycle(tmp_path):
    """r7 L1: unquoted, it raises UnicodeEncodeError inside urlopen — a
    ValueError the hand-rolled _api this file's production code used to call
    did not catch, breaking the "never raises" contract and crash-looping
    under launchd. fleetforge.Forge's get_contents percent-encodes every
    path segment itself now, but the test double must still quote to keep
    this pinned."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    seen = []

    def api(method, path, body=None, token=None):
        if "/contents/plans/" in path:
            seen.append(path)
            path.encode("ascii")          # what http.client does to the URL
        return _api_factory(plans=("0009-café.md",))(method, path, body, token)
    _wire(mod, api, orch)
    mod.cycle()                            # must not raise
    assert seen and "%C3%A9" in seen[0], f"filename must be quoted: {seen}"


def test_dry_run_refuses_when_the_orchestrator_is_missing(tmp_path):
    """r7 L3: the gate was armed-only, so on a part-1-only box --dry-run
    promised dispatches an armed run refuses to arm for at all."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(), str(tmp_path / "nonexistent-orchestrator"))
    pages = []
    mod.notify = lambda t, b: pages.append(t)
    assert mod.cycle(dry_run=True) == 0, "dry-run must refuse what cannot arm"
    assert not pages, "but must not page"


def test_a_live_successor_blocks_a_second_respawn(tmp_path):
    """r7 L4: what stopped the dead lease re-firing its consumed `respawn` was
    all_leases()'s sorted glob hiding it — an upstream detail nothing pins.
    The property is now checked directly."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    _orphaned_lease(mod, fd)
    # a live successor already holds the same note for this plan
    mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-thing"})
    before = len(_wait_spawns(log, 2))
    mod.notify = lambda t, b: None
    assert mod.cycle() == 0, "the consumed respawn must not fire again"
    assert len(_spawns(log)) == before, "no second successor"


# --------------------------------------------------------------------------
# r8: the exemption guard checked a weaker property than the doc claimed, and
# two "never raises / always pages" contracts had holes.
# --------------------------------------------------------------------------

def test_exemptions_must_be_provably_outside_the_plan_namespace(tmp_path):
    """r8 M1: probing ONE representative filename was strictly weaker than the
    doc's claim — `**/README.md` and `plans/1*.md` both miss
    plans/0001-probe.md while exempting a real ignition file."""
    orch, _ = _stub_orchestrator(tmp_path)
    for pats, ok in (("**/README.md", 0),        # missed the old probe
                     ("plans/1*.md", 0),         # ditto
                     ("plans/2*.md", 0),
                     ("{plans,docs}/*.md", 0),
                     ("**/*.md", 0),
                     ("docs/**", 1),             # provably confined
                     ("CHANGELOG.md", 1),
                     ("docs/**;CHANGELOG.md", 1),
                     ("", 1)):
        mod = _load(tmp_path / f"fleet-x{abs(hash(pats)) % 10000}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _p=pats):
            if "/branch_protections" in path:
                return 200, [_bp(unprotected_file_patterns=_p)]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == ok, f"unprotected_file_patterns={pats!r}"


def test_only_numbered_plan_files_can_ignite(tmp_path):
    """r8 M1: "any .md" made plans/README.md a dormant ignition file if anyone
    gave it front matter — and made the exemption probe unrepresentative."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(plans=("README.md",)), orch)
    assert mod.cycle() == 0, "README.md is documentation, not ignition"
    assert mod.plan_files("operator/sniff", "tok") == []


def test_a_dead_token_helper_pages(tmp_path):
    """r8 M2: every other stall on this path pages on the argument that the
    runbook's "silence means it is not running" is actively wrong. A keyvault
    outage is a documented shape on this fleet and stalled every plan
    silently."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    _wire(mod, _api_factory(), orch)
    mod._token = lambda: ("", "token helper failed")
    pages = []
    mod.notify = lambda t, b: pages.append(t) or True
    assert mod.cycle() == 0
    assert pages == ["Watcher cannot scan"], pages
    mod.cycle()
    assert len(pages) == 1, "one-shot: it must not page every cycle"
    # recovery re-arms it
    mod._token = lambda: ("tok", "")
    mod.cycle()
    mod._token = lambda: ("", "token helper failed")
    mod.cycle()
    assert len(pages) == 2, "a second outage after recovery must page again"


def test_a_schemeless_ntfy_url_does_not_kill_the_cycle(tmp_path):
    """r8 M3: urlopen raises ValueError on "ntfy.sh/topic" — not URLError,
    OSError or HTTPException. It escaped notify(), crash-looped the cycle, and
    because the O_EXCL marker is written BEFORE delivery and unlinked only on a
    False return, the exception left the marker with the page undelivered."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_NTFY_URL="ntfy.sh/no-scheme")
    assert mod.notify("t", "b") is False, "must report failure, not raise"
    # r12: this line was `is not None or True`, which is true for every value
    # and therefore asserted nothing — it neither pinned the return nor proved
    # the call did not raise, which was the whole point of the test. The real
    # contract is that an undeliverable page reports False and, per the line
    # below, leaves its one-shot unconsumed.
    assert mod._notify_once("lease-1", "t", "b") is False, \
        "an undeliverable page must report False, not raise and not claim success"
    marker = mod.lib.leases_dir() / "lease-1.watch-notified"
    assert not marker.exists(), \
        "an undelivered page must not leave its one-shot consumed"


def test_uids_and_logins_are_a_union(tmp_path):
    """r8 low: giving uids total precedence meant configuring a SECOND
    implementer's uid, and forgetting implbot's, re-dispatched every merged
    plan — the r7 M2 failure re-entered through its own config step."""
    orch, _ = _stub_orchestrator(tmp_path)
    pulls = [{"user": {"login": "implbot", "id": 7}, "state": "open",
              "number": 7, "body": "Plan: 0009-thing"}]
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_IMPLEMENTER_UIDS="42")      # the NEW implementer only
    _wire(mod, _api_factory(pulls=pulls), orch)
    assert mod.cycle() == 0, "the login fallback must still count"


# --------------------------------------------------------------------------
# r9: an eighth lane, the module's last un-guarded failure page, and config
# that must degrade rather than crash.
# --------------------------------------------------------------------------

def test_a_rule_that_admins_bypass_cannot_vouch(tmp_path):
    """r9 M1: `apply_to_admins` defaults OFF in Forgejo, and while off a repo
    admin bypasses the ENTIRE rule — pushes, approvals, whitelists, stale
    dismissal. Admin membership is no more visible here than team membership,
    which this function already refuses to vouch for."""
    orch, _ = _stub_orchestrator(tmp_path)
    for over, ok in (({"apply_to_admins": False}, 0),
                     ({"apply_to_admins": None}, 0),
                     ({"apply_to_admins": True}, 1)):
        mod = _load(tmp_path / f"fleet-ad{abs(hash(str(over))) % 10000}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _o=over):
            if "/branch_protections" in path:
                return 200, [_bp(**_o)]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == ok, f"apply_to_admins={over}"


def test_a_persistently_failing_respawn_pages_once(tmp_path):
    """r9 M2: the module's only non-one-shot failure page. A `respawn` is
    consumed by a SUCCESSFUL dispatch only, so while spawn fails the comment
    stays fresh and every cycle re-attempted and paged — the storm r5 M2
    fixed, re-entered through the sweep."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID="3")
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    comments = [{"user": {"id": 3}, "body": "respawn",
                 "created_at": "2030-01-01T00:00:00Z"}]
    _wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    _orphaned_lease(mod, fd)
    pages = []
    mod.notify = lambda t, b: pages.append(t) or True
    mod.spawn = lambda *a, **k: None          # persistent spawn failure
    for _ in range(4):
        mod.cycle()
    assert pages.count("Respawn failed") == 1, \
        f"a persistent failure must page once, not every cycle: {pages}"


def test_marker_keys_are_safe_filenames(tmp_path):
    """r9 low: keys become leases/<key>.watch-notified. A `/` in a plan id
    nested the marker where the top-level sweep glob never sees it; `..` let
    mkdir(parents=True)/unlink reach outside leases/."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    for plan_id in ("../../escape", "a/b/c", "0009-thing"):
        key = mod._marker_key("fail-spawn", "operator/sniff", plan_id)
        assert "/" not in key and ".." not in key, key
        # and it must still not look like a lease id, or _sweep_markers eats it
        assert not mod.lib._ID_RE.match(key), key


def test_a_bad_integer_env_degrades_instead_of_crashing(tmp_path):
    """r9 low: int() at import raised ValueError on a typo — an unhandled
    crash-loop under launchd, the class r7 L1 and r8 M3 were fixed for."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_WATCH_CAP="one", FLEET_PR_PAGE_CAP="20  # pages")
    assert mod.CAP == 1 and mod.PAGE_CAP == 20


def test_a_backslash_escape_cannot_vouch(tmp_path):
    """r9 low: gobwas reads `\\p` as literal `p`, so `\\plans/*.md` covers the
    ignition file while a static-prefix comparison matches nothing."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    assert mod._may_touch_plans(r"\plans/*.md")
    assert not mod._may_touch_plans("docs/**")


def test_exemption_patterns_are_matched_case_insensitively(tmp_path):
    """r10 M2: Forgejo lower-cases both the pattern (`getFilePatterns`) and the
    path (`IsUnprotectedFile`) before matching, so `Plans/**` exempts plans/
    exactly as `plans/**` does. A case-sensitive comparison here vouched for
    precisely the rule that opens the r6 M1 self-ignition path."""
    orch, _ = _stub_orchestrator(tmp_path)
    for pats in ("Plans/**", "PLANS/*.md", "PlAnS/0001-x.md"):
        mod = _load(tmp_path / f"fleet-c{abs(hash(pats)) % 10000}",
                    FLEET_WATCH_REPOS="operator/sniff")

        def api(method, path, body=None, token=None, _p=pats):
            if "/branch_protections" in path:
                return 200, [_bp(unprotected_file_patterns=_p)]
            return _api_factory()(method, path, body, token)
        _wire(mod, api, orch)
        assert mod.cycle() == 0, f"{pats!r} exempts plans/ and must refuse"


def test_a_403_on_protections_is_not_reported_as_a_blind_watcher(tmp_path):
    """r10 M1: `/branch_protections` is behind reqAdmin(), and the default
    token helper mints a write-scoped PAT. Every repo then 403s, which the
    blind-watcher counter read as "the LAN is unreachable" and paged the
    operator toward a network fault that does not exist."""
    orch, _ = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff")
    pages = []

    def api(method, path, body=None, token=None):
        if "/branch_protections" in path:
            return 403, None
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    mod.notify = lambda t, b: pages.append(t) or True

    assert mod.cycle() == 0, "a 403 still refuses to dispatch"
    assert not any("blind" in t.lower() for t in pages), \
        "a permissions problem must not page as a connectivity problem"
    # r11 M1: but it MUST page. Making the 403 quiet stalled every plan with
    # only a stderr warn — the silence r3 M2, r5 low and r8 M2 each removed
    # once already, reintroduced by the fix for the misdirected page.
    assert pages == ["Watcher token rejected"], pages
    mod.cycle()
    assert len(pages) == 1, "one-shot: a persistent rejection pages once"
    ok, why = mod.main_is_protected("operator/sniff", "tok")
    assert not ok and "insufficient scope" in why, why


def test_a_lease_id_from_a_body_cannot_escape_the_leases_dir(tmp_path):
    """r10 L2: the sweep takes the id from the lease BODY, so a hand-edited
    `../x` reached leases/../x.watch-notified via mkdir(parents=True)."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    for bad in ("../escape", "a/b/c", "../../etc/passwd"):
        key = mod._safe_lease_key(bad)
        assert "/" not in key and ".." not in key, key
    good = "branch--feat-0009-thing--001"
    assert mod._safe_lease_key(good) == good, "a real lease id is left alone"


def test_an_expired_token_pages_and_says_unauthenticated(tmp_path):
    """r11 M1: 401 and 403 are different faults — unauthenticated vs
    insufficient scope — and the first cut reported both as "minting a
    write-scoped PAT", which misdiagnoses a rotated credential."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    pages = []

    def api(method, path, body=None, token=None):
        if "/branch_protections" in path:
            return 401, None
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    mod.notify = lambda t, b: pages.append(t) or True

    assert mod.cycle() == 0
    assert pages == ["Watcher token rejected"], pages
    ok, why = mod.main_is_protected("operator/sniff", "tok")
    assert not ok and "unauthenticated" in why, why
    assert "write-scoped" not in why, "401 is not a scope problem"


def test_a_clean_scan_rearms_the_token_page(tmp_path):
    """The one-shot must clear, or a fixed credential can never page again."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    pages = []
    state = {"code": 403}
    # A marked PR suppresses dispatch, so the recovery cycle SCANS without
    # spawning. Without it the recovery cycle dispatches, the next cycle hits
    # the cap and breaks before `scanned += 1`, and the clear never runs — a
    # fixture artefact that looks exactly like a broken re-arm.
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]

    def api(method, path, body=None, token=None):
        if "/branch_protections" in path and state["code"] != 200:
            return state["code"], None
        return _api_factory(pulls=pulls)(method, path, body, token)
    _wire(mod, api, orch)
    mod.notify = lambda t, b: pages.append(t) or True

    mod.cycle()
    assert len(pages) == 1
    state["code"] = 200          # credential fixed
    mod.cycle()
    state["code"] = 403          # and rejected again later
    mod.cycle()
    assert len(pages) == 2, "a second rejection after recovery must page again"


def test_sibling_plan_ids_do_not_share_a_one_shot_marker(tmp_path):
    """r12 L3: slug_of caps at 40 chars for BRANCH names, and _safe_lease_key
    was applying that cap to FILENAMES. `0004-plan-dispatcher` and its `-v2`
    sibling cut to one key, so the second plan's failure page was swallowed
    while the first's marker existed and either plan's success cleared the
    other's. The runbook's own recovery advice ("re-file under a new id")
    produces exactly this pair."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/eunomia")
    a, b = ("0004-plan-dispatcher", "0004-plan-dispatcher-v2")
    ka = mod._safe_lease_key(mod._marker_key("fail-respawn", "operator/eunomia", a))
    kb = mod._safe_lease_key(mod._marker_key("fail-respawn", "operator/eunomia", b))
    assert ka != kb, f"sibling plans share the marker {ka}"


def test_the_orchestrator_never_inherits_the_token_helper(tmp_path):
    """r12: spawn() passed os.environ straight through, handing the child
    FLEET_TOKEN_CMD — on this fleet the admin-token helper, which
    docs/plan-dispatch.md says an agent must never reach: an admin token can
    PATCH the protection rule, push a ready plan, and restore it, forging the
    ignition this module exists to make unforgeable."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_TOKEN_CMD="/usr/local/bin/fetch-forgejo-admin-token.sh")
    env = mod._child_env()
    assert "FLEET_TOKEN_CMD" not in env, "the token recipe reached the child"
    assert env.get("EUNOMIA_SESSION"), "the child still needs its session identity"



@pytest.mark.parametrize("mode", ["popen", "launchd"])
def test_the_orchestrator_does_not_inherit_the_token_helper(tmp_path, monkeypatch, mode):
    """r13: r12 fixed the wrong subprocess. Its commit said "the orchestrator
    inherited FLEET_TOKEN_CMD ... one line" and changed the fleet-claim call,
    not the orchestrator Popen — so the child the mitigation was written for
    kept receiving the recipe for minting an admin token, which can PATCH the
    branch-protection rule, push a ready plan, and restore it.

    Parametrised over BOTH spawn modes as of 2026-09-10. The property is about
    what the orchestrator receives, not about how it is started, and a launchd
    job carries its environment in a FILE — so under that mode the recipe would
    not merely be inherited, it would be written to disk. A test pinned to Popen
    would have gone green while the default mode moved out from under it, which
    is the same shape of miss as r12 itself."""
    orch, _log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    # FLEET_DISPATCH_IMPL pinned to "launchd": select_impl() would otherwise
    # probe the REAL host, and the mode=="launchd" branch below asserts a
    # `.plist` was written -- which only fleetjob's launchd path produces.
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_SPAWN=mode,
                FLEET_DISPATCH_IMPL="launchd",
                FLEET_TOKEN_CMD="/usr/local/bin/fetch-forgejo-admin-token.sh")
    mod.fleetjob.JOB_DIR = tmp_path / "jobs"
    seen = {}

    real_popen = mod.subprocess.Popen

    def fake_popen(*a, **kw):
        # Only intercept the orchestrator launch. `subprocess.run` (used for
        # fleet-claim) calls Popen as a context manager, so a blanket stub
        # breaks the lease assignment before spawn ever reaches this line.
        argv = a[0] if a else kw.get("args") or []
        if argv and str(argv[0]) == str(orch):
            seen.update(kw.get("env") or {})
            return None                  # spawn() ignores the return value
        return real_popen(*a, **kw)

    _wire(mod, _api_factory(), orch)
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    _stub_supervisor(mod, monkeypatch)
    mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-thing"})
    if mode == "launchd":
        # read the environment back out of the job definition on disk — that is
        # what launchd will hand the child, and what a reader of the file sees
        import plistlib
        jobs = sorted((tmp_path / "jobs").glob("*.plist"))
        assert jobs, "no job definition was written"
        seen = plistlib.loads(jobs[0].read_bytes())["EnvironmentVariables"]
    assert seen, "orchestrator was never spawned"
    assert "FLEET_TOKEN_CMD" not in seen, \
        "the orchestrator still receives the admin-token recipe"
    assert seen.get("EUNOMIA_SESSION"), "the child still needs its identity"
    assert seen.get("EUNOMIA_PLAN_ID") == "0009-thing", "plan context preserved"


def test_an_executable_but_unready_orchestrator_does_not_arm(tmp_path):
    """The r5 M2 gate checks os.access, which a PART-BUILT orchestrator passes.

    Arming on that would restore the very storm the gate prevents — assign,
    fail at the wrapper's own boundary, release, page, once per plan per cycle
    — so readiness is asked rather than inferred from the file existing."""
    fd = tmp_path / "fleet"
    orch = tmp_path / "half-built"
    orch.write_text('#!/bin/sh\n[ "$1" = "--ready" ] && { echo "part 1 only" >&2; exit 1; }\nexit 0\n')
    orch.chmod(0o755)
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_ORCHESTRATOR=str(orch))
    calls = []
    _wire(mod, _api_factory(calls=calls), str(orch))
    notes = []
    mod.notify = lambda t, b: (notes.append(t), True)[1]

    for _ in range(3):
        assert mod.cycle() == 0

    assert calls == [], "refuses BEFORE touching Forgejo"
    assert len(notes) == 1, f"pages once, not per cycle: {notes}"
    assert [r for r in mod.lib.all_leases()] == [], "no lease churn"


def test_an_orchestrator_that_cannot_answer_ready_is_not_ready(tmp_path):
    """Fail closed: one page for refusing, versus a page per plan per cycle for
    arming wrongly. An orchestrator predating this contract is not ready."""
    fd = tmp_path / "fleet"
    orch = tmp_path / "ancient"
    orch.write_text('#!/bin/sh\nexit 7\n')
    orch.chmod(0o755)
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_ORCHESTRATOR=str(orch))
    calls = []
    _wire(mod, _api_factory(calls=calls), str(orch))
    mod.notify = lambda t, b: True
    assert mod.cycle() == 0
    assert calls == []


def test_a_ready_orchestrator_rearms_the_unready_page(tmp_path):
    """The re-arm is the property, so assert it: a marker left by an earlier
    deployment must be CLEARED, or the next genuine unready state pages nobody.
    The previous version of this test set the marker and only checked that the
    cycle dispatched — which it would have with the marker still in place."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_ORCHESTRATOR=orch)
    _wire(mod, _api_factory(), orch)
    mod.notify = lambda t, b: True
    key = f"orchestrator-unready-{Path(orch).name}"
    mod._notify_once(key, "old", "page")
    marker = mod.lib.leases_dir() / f"{mod._safe_lease_key(key)}.watch-notified"
    assert marker.exists(), "the fixture did not arm the marker"

    assert mod.cycle() == 1
    assert not marker.exists(), "a recovered orchestrator did not re-arm the page"


def test_the_dispatch_lease_carries_a_ttl_sized_against_the_heartbeat(tmp_path):
    """fleet-claim's --ttl defaults to 240 minutes. Unset, a SIGKILLed
    orchestrator stays invisible for four hours once it has activated."""
    orch, log = _stub_orchestrator(tmp_path)
    fd = tmp_path / "fleet"
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_ORCHESTRATOR=orch)
    _wire(mod, _api_factory(), orch)
    mod.notify = lambda t, b: True
    assert mod.cycle() == 1
    leases = [r for r in mod.lib.all_leases() if (r.get("note") or "").startswith("plan:")]
    assert leases, "no dispatch lease minted"
    ttl = leases[0].get("ttl_minutes")
    assert ttl and ttl <= 10, f"ttl_minutes is {ttl}; the 240 default is back"


# --- separate fleet tokens (0013 lane) -------------------------------------


def test_the_watcher_never_reads_branch_protections_itself(tmp_path):
    """ADR-0003 §2, and the reason the admin token could be retired at all.

    `branch_protections` is behind reqAdmin(), which the implbot PAT cannot
    satisfy — so this process used to need the OWNER's PAT purely to make one
    read. It no longer makes that read: the broker does, on vaulthost. Every call
    this process still makes is an ordinary one the implbot token serves.

    Replaces test_protections_read_uses_the_admin_token_and_nothing_else_does,
    which after the rewiring asserted a property of the test DOUBLE (the double
    calls main_is_protected with a fake admin token) rather than of the code —
    a green that meant nothing.
    """
    orch, log = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    seen = []
    inner = _api_factory()

    def api(method, path, body=None, token=None):
        seen.append((path, token))
        return inner(method, path, body=body, token=token)

    # A broker that always answers 'protected', so the cycle proceeds past the
    # verdict and any branch_protections traffic recorded below can only be
    # traffic THIS process originated.
    _wire(mod, api, orch,
          broker=lambda path: (200, {} if path == "/health"
                               else {"ok": True, "determinable": True}))
    mod.cycle(dry_run=True)

    prot = [p for p, _tok in seen if "branch_protections" in p]
    assert prot == [], f"the watcher still reads protections itself: {prot}"
    assert seen, "no Forgejo calls at all — the fixture stopped exercising it"
    assert set(t for _p, t in seen) == {"tok"}, \
        f"a non-implbot token appeared: {set(t for _p, t in seen)}"


def test_child_env_pops_both_token_recipes(tmp_path):
    """r12 popped FLEET_TOKEN_CMD so a child could not mint a token. Splitting
    the variables without popping both would reopen it by the back door — and
    FLEET_ADMIN_TOKEN_CMD is the sharper edge, being the recipe for a token that
    reads protection rules."""
    mod = _load(tmp_path / "fleet")
    os.environ["FLEET_TOKEN_CMD"] = "echo general"
    os.environ["FLEET_ADMIN_TOKEN_CMD"] = "echo admin"
    try:
        env = mod._child_env()
    finally:
        os.environ.pop("FLEET_TOKEN_CMD", None)
        os.environ.pop("FLEET_ADMIN_TOKEN_CMD", None)
    assert "FLEET_TOKEN_CMD" not in env
    assert "FLEET_ADMIN_TOKEN_CMD" not in env


def test_a_dead_broker_pages_once_and_scans_nothing(tmp_path):
    """ADR-0003 §6. An undeterminable protection is the blind signature: page
    once and scan NOTHING, rather than refusing each repo for a reason that is
    not about the repo. Replaces the dead-admin-token-helper test — the
    property is identical, the dependency moved from a helper on this host to
    a service on vaulthost, and ADR-0003 made that explicit rather than leaving it
    an ssh timeout."""
    orch, log = _stub_orchestrator(tmp_path)
    calls = []
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    pages = []
    _wire(mod, _api_factory(calls=calls), orch, broker=lambda path: (0, None))
    mod.notify = lambda t, b: pages.append(t) or True
    assert mod.cycle(dry_run=True) == 0
    # It must not have gone to Forgejo at all: the pre-flight stops the cycle
    # before any repo is scanned, which is what "scan nothing" means.
    assert not any("branch_protections" in c for c in calls), calls
    # ...and a DRY RUN mutates nothing, pages included (r5, r6 low). The
    # pre-flight inherited its un-guarded _notify_once/_clear_notified from the
    # admin-token block it replaced; this is the assertion that was missing.
    assert pages == [], f"--dry-run paged: {pages}"


def test_the_broker_preflight_pages_when_armed(tmp_path):
    """The other half of the dry-run guard: it must still page for real."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    pages = []
    _wire(mod, _api_factory(), orch, broker=lambda path: (0, None))
    mod.notify = lambda t, b: pages.append(t) or True
    assert mod.cycle() == 0
    assert pages == ["Watcher cannot scan"], pages


def test_a_broker_answering_json_that_is_not_an_object_does_not_crash(tmp_path):
    """`body or {}` was not enough: a JSON ARRAY is truthy and `.get` on a list
    raises AttributeError — under launchd that is the crash-loop class r7 L1
    and r8 M3 were fixed for, and `_broker_get` promises never to raise. The
    broker never sends one today; the promise should not depend on that."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    for junk in ([1, 2], "a string", 7):
        mod._broker_get = lambda path, j=junk: (200, j)
        code, why, lane = mod.broker_verdict("operator/sniff")
        assert code == mod.PROT_UNKNOWN, (junk, code)
        assert lane is None or isinstance(lane, str)


def test_a_broker_timeout_covers_a_cold_pat_cache(tmp_path):
    """The broker's worst case is an AppRole login + kv read + revoke (10s
    each) before a 20s Forgejo call, and a rejected cached token buys a second
    round. A 25s ceiling would report that path as UNREACHABLE, which is the
    opposite of what the constant's comment claims."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    assert mod.BROKER_TIMEOUT >= 60, mod.BROKER_TIMEOUT


def test_an_unreachable_broker_pages_and_does_not_refuse_every_repo(tmp_path):
    """The collapse ADR-0003 §6 forbids, stated as a test: a broker that is
    down must not read as 'every repo is unprotected'. It pages, and it warns
    UNDETERMINED — never REFUSED."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    pages, warns = [], []
    _wire(mod, _api_factory(), orch, broker=lambda path: (0, None))
    mod.notify = lambda t, b: pages.append(t) or True
    mod.warn = lambda m: warns.append(m)
    assert mod.cycle() == 0
    assert pages == ["Watcher cannot scan"], pages
    assert not any("REFUSED" in w for w in warns), warns


def test_a_broker_that_answers_undeterminable_is_not_a_refusal(tmp_path):
    """Past the pre-flight: /health is fine but the per-repo answer is a 503
    (keyvault sealed, say). Still not a refusal of the repo."""
    orch, _ = _stub_orchestrator(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    warns = []

    def broker(path):
        if path == "/health":
            return 200, {}
        return 503, {"determinable": False, "lane": "vault",
                     "detail": "keyvault unreachable"}
    _wire(mod, _api_factory(), orch, broker=broker)
    mod.warn = lambda m: warns.append(m)
    assert mod.cycle() == 0
    assert any("UNDETERMINED" in w for w in warns), warns
    assert not any("REFUSED" in w for w in warns), warns


def test_the_watcher_holds_no_admin_token_anywhere(tmp_path):
    """ADR-0003 §2: the retirement, asserted rather than assumed. The watcher
    must have no way to mint the owner's PAT — no helper, no variable, no
    function — because the whole point is that it holds nothing to leak."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    assert not hasattr(mod, "_admin_token"), "the admin-token minter came back"
    assert not hasattr(mod, "ADMIN_TOKEN_CMD"), "the admin recipe came back"
    # Structural, with DOCSTRINGS EXCLUDED — the same correction review 2369 L2
    # made to a sibling check: prose ABOUT a thing is not a use of it. A bare
    # substring search over the file forbids ever explaining why the helper is
    # retired, and _child_env's docstring has to name both helpers to correct a
    # false claim about which one this fleet actually uses. A name inside a
    # string literal cannot mint a token; a call can, and this still catches one.
    src = (BIN / "fleet-watch").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                src = src.replace(doc, "")
    assert "fetch-forgejo-admin-token" not in src, \
        "fleet-watch names the retired helper OUTSIDE a docstring"


def test_the_broker_imports_this_rule(tmp_path):
    """`main_is_protected` and `classify` have no caller left INSIDE eunomia —
    keyvault's broker imports both. A cleanup grepping for callers would find
    only tests and could delete them, breaking the fleet's protection check
    from a repo that never mentions either.

    An EXISTENCE tripwire, deliberately: it cannot check that the broker still
    imports them (keyvault is a different repo), only that they are still here
    to be imported. test_the_token_rejected_lane_is_a_cross_repo_contract below
    covers the other direction when both repos are present."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    assert callable(getattr(mod, "main_is_protected", None)), \
        "main_is_protected is gone — keyvault's forgejo-broker imports it"
    repo_mod = _load_repo_module()
    assert callable(getattr(repo_mod, "classify", None)), \
        "fleet-repo.classify is gone — keyvault's forgejo-broker imports it"
    # and classify still maps the three states the broker depends on
    assert repo_mod.classify(True, "")[0] == repo_mod.EXIT_OK
    assert repo_mod.classify(False, "main is not branch-protected")[0] == repo_mod.EXIT_REFUSED
    assert repo_mod.classify(
        False, "branch protections unreadable (HTTP 0)")[0] == repo_mod.EXIT_UNKNOWN


def test_the_token_rejected_lane_is_a_cross_repo_contract(tmp_path):
    """#97 r2 L3. `cycle()` splits its two alarm chains on
    `lane == "token_rejected"`: that value pages "Watcher token rejected" and is
    NOT counted blind, while anything else counts blind. The string is produced
    by keyvault's broker, so a one-character change there silently sends a
    rejected PAT to the wrong page — with the right detail in the warn text,
    which is what makes it hard to notice.

    Read from keyvault's origin/main REF, never its working tree: that checkout
    is shared and was found sitting on a pre-broker branch while this test was
    being written, which would have made it skip (or assert against an
    unrelated branch) and look fine either way.
    """
    import re
    import subprocess
    from pathlib import Path
    repo = Path.home() / "dev" / "keyvault"
    if not (repo / ".git").exists():
        # NB this branch is the one CI takes — the runner has no keyvault
        # checkout — and it is why `import pytest` at the top of this module is
        # load-bearing. The first cut called pytest.skip() without it and was
        # green locally, where the directory exists and this line never ran.
        pytest.skip("keyvault not cloned beside eunomia — cannot verify the "
                    "cross-repo lane contract from here")
    r = subprocess.run(["git", "-C", str(repo), "show",
                        "origin/main:broker/forgejo_broker.py"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("keyvault origin/main has no broker yet (%s)"
                    % (r.stderr or "").strip()[:60])
    m = re.search(r'\("branch protections rejected the token",\s*"([a-z_]+)"\)',
                  r.stdout)
    assert m, ("keyvault's LANES no longer maps the rejection message at all — "
               "fleet-watch's token-rejected page would be unreachable")
    assert m.group(1) == "token_rejected", (
        "keyvault labels a rejected token %r; fleet-watch keys its page on "
        "'token_rejected', so a rejected PAT would page 'Watcher blind' instead"
        % m.group(1))

def test_the_rejection_prefix_classify_keys_on_is_unchanged(tmp_path):
    """The other half of the same contract, and this one IS local: `classify`
    and keyvault's LANES both match the PREFIX of this message, so rewording
    the front of it silently turns a rejected token into a plain refusal."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    mod._forge = lambda token: _ForgeDouble(lambda m, p, body=None, token=None: (401, None), token=token)
    ok, why = mod.main_is_protected("operator/sniff", "tok")
    assert ok is False
    assert why.startswith("branch protections rejected the token"), why
    repo_mod = _load_repo_module()
    assert repo_mod.classify(ok, why)[0] == repo_mod.EXIT_UNKNOWN


# --- pin staleness -----------------------------------------------------------
#
# The pins are how fleet-secret-guard and the skill corpus reach every session.
# Both were, until 2026-09-07, plain checkouts: whatever branch someone left
# them on was what every session loaded. The skills one was found five commits
# behind origin/main, serving a skill whose bug had been fixed and merged hours
# earlier, and nothing said so from inside a session. These assert the states
# separately because the useful ones are the states that are NOT "stale".

def _pin_repo(tmp_path, name="origin"):
    """A bare origin plus a detached worktree pinned at its main."""
    origin = tmp_path / (name + ".git")
    work = tmp_path / (name + "-work")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(work)] + cmd, check=True)
    (work / "f").write_text("one\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "one"], check=True)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "main"], check=True)
    pin = tmp_path / (name + "-pin")
    subprocess.run(["git", "-C", str(work), "worktree", "add", "--detach",
                    str(pin), "origin/main"], check=True,
                   capture_output=True)
    return work, pin


def test_a_pinned_worktree_at_origin_main_is_current(tmp_path):
    mod = _load(tmp_path / "fleet")
    _, pin = _pin_repo(tmp_path)
    assert mod.pin_status(str(pin))[0] == "current"


def test_a_pin_left_behind_origin_main_is_stale(tmp_path):
    mod = _load(tmp_path / "fleet")
    work, pin = _pin_repo(tmp_path)
    (work / "f").write_text("two\n")
    subprocess.run(["git", "-C", str(work), "commit", "-qam", "two"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)
    state, detail, _ = mod.pin_status(str(pin))
    assert state == "stale", detail
    assert "not reaching sessions" in detail


def test_a_pin_on_a_branch_is_not_a_pin(tmp_path):
    """The condition pinning removed: on a branch, someone else's checkout can
    move what every session loads."""
    mod = _load(tmp_path / "fleet")
    _, pin = _pin_repo(tmp_path)
    subprocess.run(["git", "-C", str(pin), "checkout", "-q", "-b", "wip"],
                   check=True, capture_output=True)
    state, detail, _ = mod.pin_status(str(pin))
    assert state == "attached", detail
    assert "not detached" in detail


def test_an_unreachable_remote_is_not_reported_as_current(tmp_path):
    """The fail-open this fleet keeps rediscovering: answering 'in sync' from a
    ref that could not be refreshed."""
    mod = _load(tmp_path / "fleet")
    _, pin = _pin_repo(tmp_path)
    subprocess.run(["git", "-C", str(pin), "remote", "set-url", "origin",
                    str(tmp_path / "gone.git")], check=True)
    assert mod.pin_status(str(pin))[0] == "unfetchable"


def test_a_missing_or_non_repo_path_is_named_as_such(tmp_path):
    mod = _load(tmp_path / "fleet")
    assert mod.pin_status(str(tmp_path / "nope"))[0] == "missing"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert mod.pin_status(str(plain))[0] == "not-a-repo"


def test_an_unconfigured_watcher_checks_no_pins(tmp_path):
    """Same rule FLEET_WATCH_REPOS follows: no default allowlist."""
    mod = _load(tmp_path / "fleet")
    assert mod.pins() == []
    assert mod.check_pins(dry_run=True) == []


def test_check_pins_never_moves_a_pin(tmp_path):
    """The watcher spawns, emits and notifies; it does not act on staleness.
    Moving these paths is a deployment decision with a security surface."""
    work, pin = _pin_repo(tmp_path)
    before = subprocess.run(["git", "-C", str(pin), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    (work / "f").write_text("three\n")
    subprocess.run(["git", "-C", str(work), "commit", "-qam", "three"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    drift = mod.check_pins(dry_run=True)
    after = subprocess.run(["git", "-C", str(pin), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    assert [d[1] for d in drift] == ["stale"]
    assert after == before, "check_pins moved the pin"


def test_the_pin_watch_unit_arms_nothing():
    """The plist runs fleet-watch, so the ONLY thing keeping it from dispatching
    is the absence of FLEET_WATCH_REPOS. That is a one-line edit away from
    arming plan dispatch on a timer, so it is asserted rather than left to the
    comment that explains it.

    Parsed via `plutil -convert`, not plistlib directly: launchd's parser
    tolerates `--` inside XML comments, which XML forbids and Python's expat
    refuses. plutil is the authority for a file launchd reads, and it also
    strips the comments. NOT every plist in this repo uses `--`; which ones
    still do is pinned by `test_units_parse_strictly` in
    tests/test_launchd_units.py, and this unit is the one that does.
    """
    import plistlib
    plist = BIN.parent / "launchd" / "org.eunomia.fleet-pin-watch.plist"
    if not plist.exists():                      # unit ships with the check
        pytest.skip("pin-watch unit not present")
    # shutil.which, not returncode: subprocess.run RAISES when the binary is
    # absent, so a guard written on the returncode could only fire when plutil
    # existed and FAILED -- exactly backwards.
    if shutil.which("plutil") is None:
        pytest.skip("plutil is macOS-only; this unit is only read by launchd")
    out = subprocess.run(["plutil", "-convert", "xml1", "-o", "-", str(plist)],
                         capture_output=True)
    # plutil is HERE and said no, so the unit is broken -- fail, do not skip.
    # A skip on this branch means a plist that has stopped being a plist ships
    # green: the lenient parser rejects it, the strict test exempts it, and
    # nothing is left to notice. Only the absent-binary case above is a skip.
    assert out.returncode == 0, (
        f"plutil could not read {plist.name}: "
        f"{out.stderr.decode(errors='replace').strip()}\n"
        "launchd reads this file; if plutil cannot parse it, launchd cannot "
        "either and the unit is dead on the next bootstrap.")
    d = plistlib.loads(out.stdout)
    env = d.get("EnvironmentVariables", {})
    assert "FLEET_WATCH_REPOS" not in env, (
        "the pin-watch unit must not carry a dispatch allowlist — adding one "
        "arms plan dispatch on a timer")
    assert env.get("FLEET_PINS"), "the unit exists to check pins; none configured"
    assert "--pins-only" in d["ProgramArguments"], (
        "without --pins-only this runs the dispatch path and takes the cycle "
        "lock across two network fetches")
    # and it must run the PINNED eunomia, not the shared clone whose branch
    # anyone can change -- the defect this whole check reports on.
    assert "/dev/eunomia/" not in d["ProgramArguments"][1], (
        "run the pinned worktree, not ~/dev/eunomia")


def test_this_hosts_forgejo_url_still_drives_the_watchers_own_calls(tmp_path):
    """#110 r1 M1. The --help text claimed this host's FLEET_FORGEJO_URL "does
    not decide which Forgejo is asked". Only the protection VERDICT moved to
    the broker; `_forge` (plan 0051's builder around `fleetforge.Forge`) still
    uses this value for plans/ listings, plan bodies, the dedupe PR scan and
    respawn comments.

    That matters because dropping the variable does not fail loudly — every
    repo reports `plans/ unreadable — skipped`, which warns and never pages.
    This pins the corrected claim so the prose cannot drift from the code."""
    mod = _load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                FLEET_FORGEJO_URL="http://forge.example:9999")
    assert mod.FORGEJO_URL == "http://forge.example:9999"
    seen = []

    def fake_urlopen(req, *a, **kw):
        # OSError specifically: fleetforge.Forge promises never to raise and
        # catches it, returning (0, None). Anything else escapes and the test
        # would be asserting on a crash rather than on the URL.
        seen.append(req.full_url)
        raise OSError("no network in this test")

    # The urlopen fleet-watch's own forge call reaches lives in fleetforge.py's
    # module namespace (loaded via SourceFileLoader, not `import`), not in
    # fleet-watch's own `urllib` — the two are separate module objects.
    real = mod.fleetforge.urllib.request.urlopen
    mod.fleetforge.urllib.request.urlopen = fake_urlopen
    try:
        st, _body = mod._forge("t").get_contents("operator/sniff", "plans")
    finally:
        mod.fleetforge.urllib.request.urlopen = real
    assert st == 0, "a transport failure should be (0, None), not %r" % st
    assert seen, "the forge made no request at all"
    assert seen[0].startswith("http://forge.example:9999/api/v1/"), seen


# --- the r1 findings, each with the case that reproduced it ------------------

def test_a_pin_that_goes_stale_twice_pages_twice(tmp_path):
    """r1 M1. The first version keyed a one-shot on path+state and never
    cleared it, so a pin paged ONCE per install -- and the plist's own
    verification step (move it back, watch it page) spent that only page.
    The everyday cycle is merge -> stale -> re-point -> merge -> stale.

    Re-pointed for plan 0059: staleness now delays a page until
    fleet-pin-advance's own window has passed, so a staling that pages on
    SIGHT no longer proves the one-shot works -- it would page once whether
    or not re-arming worked. Both stalings here are pushed OUTSIDE a
    (tiny, fake) advance window before being checked, so this still proves
    two independent stale episodes each page exactly once."""
    import plistlib
    work, pin = _pin_repo(tmp_path)
    plist = tmp_path / "fake-pin-advance.plist"
    with plist.open("wb") as fh:
        plistlib.dump({"StartInterval": 1}, fh)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin), FLEET_NTFY_URL="",
               FLEET_PIN_ADVANCE_PLIST=str(plist), FLEET_PIN_PAGE_MARGIN="0")
    pages = []
    # plan 0058: check_pins() now forwards a paging-identity label as a 3rd
    # positional arg to notify(); accept and ignore it here.
    mod.notify = lambda title, body, label=None: (pages.append(title), True)[1]
    clock = mod.lib.now_utc()
    mod._now = lambda: clock

    def advance():
        (work / "f").write_text(str(len(pages)) + "x\n")
        subprocess.run(["git", "-C", str(work), "commit", "-qam", "n"], check=True)
        subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)

    def tick(seconds):
        nonlocal clock
        clock = clock + timedelta(seconds=seconds)
        mod._now = lambda: clock

    mod.check_pins()                                    # current: silent
    assert pages == []
    advance(); mod.check_pins()                         # stale #1: seen, not due
    assert pages == [], "must not page inside the advance window"
    tick(5)
    mod.check_pins()                                    # stale #1: now overdue
    assert len(pages) == 1, pages
    subprocess.run(["git", "-C", str(pin), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(pin), "checkout", "-q", "--detach",
                    "origin/main"], check=True, capture_output=True)
    mod.check_pins()                                    # back to current
    advance(); mod.check_pins()                         # stale #2: seen, not due
    assert len(pages) == 1, "the second staling must wait out its own window too"
    tick(5)
    mod.check_pins()                                    # stale #2: now overdue
    assert len(pages) == 2, f"re-arm failed after a re-point: {pages}"


def test_an_unchanged_state_is_silent(tmp_path):
    """r1 L2. A pin sits stale for days by design; 30-minute cycles wrote 48
    rows a day into the tail sessions are told to read."""
    work, pin = _pin_repo(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    mod.notify = lambda *a: True
    (work / "f").write_text("two\n")
    subprocess.run(["git", "-C", str(work), "commit", "-qam", "two"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)
    events = mod.lib.fleet_dir() / "events.jsonl"
    mod.check_pins()
    first = events.read_text().count("pin-drift")
    mod.check_pins(); mod.check_pins()
    assert events.read_text().count("pin-drift") == first == 1


def test_dry_run_writes_no_event(tmp_path):
    """r1 L1."""
    work, pin = _pin_repo(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    (work / "f").write_text("two\n")
    subprocess.run(["git", "-C", str(work), "commit", "-qam", "two"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)
    mod.check_pins(dry_run=True)
    events = mod.lib.fleet_dir() / "events.jsonl"
    assert not events.exists() or "pin-drift" not in events.read_text()


def test_the_event_detail_is_an_object(tmp_path):
    """r1 M2. Every other producer passes a dict and SPEC shows detail as an
    object; the module-level emit bypassed the CLI's check, so this was the
    first non-object detail in an append-only log -- unfixable retroactively."""
    work, pin = _pin_repo(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    mod.notify = lambda *a: True
    (work / "f").write_text("two\n")
    subprocess.run(["git", "-C", str(work), "commit", "-qam", "two"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)
    mod.check_pins()
    row = [json.loads(l) for l in
           (mod.lib.fleet_dir() / "events.jsonl").read_text().splitlines()
           if "pin-drift" in l][0]
    assert isinstance(row["detail"], dict), type(row["detail"])
    assert row["detail"]["state"] == "stale"
    assert row["detail"]["path"] == str(pin)
    assert row["detail"]["head"] and row["detail"]["target"]


def test_a_plain_directory_is_not_classified_by_its_parent_repo(tmp_path):
    """r1 L4. `git -C` walks upward, so a plain dir inside any repo reported
    the PARENT's branch -- a deleted-and-recreated pin under a home that is
    itself a repo would be called `attached`."""
    work, _ = _pin_repo(tmp_path)
    inner = work / "not-a-pin"
    inner.mkdir()
    mod = _load(tmp_path / "fleet")
    assert mod.pin_status(str(inner))[0] == "not-a-repo"


def test_a_pin_with_local_edits_is_not_current(tmp_path):
    """r1 L5. HEAD matching is not the tree matching, and `current` would read
    as 'sessions run what is on the trunk' while they run someone's patch."""
    _, pin = _pin_repo(tmp_path)
    mod = _load(tmp_path / "fleet")
    assert mod.pin_status(str(pin))[0] == "current"
    (pin / "f").write_text("patched in place\n")
    state, detail, _ = mod.pin_status(str(pin))
    assert state == "dirty", detail
    assert "unreviewed" in detail


def test_the_target_ref_is_configurable(tmp_path):
    """r1 L6. origin/main was hard-coded."""
    work, pin = _pin_repo(tmp_path)
    subprocess.run(["git", "-C", str(work), "branch", "release"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "release"], check=True)
    mod = _load(tmp_path / "fleet", FLEET_PINS=f"{pin}=origin/release")
    assert mod.pins() == [(str(pin), "origin/release")]
    assert mod.pin_status(str(pin), "origin/release")[0] == "current"


def test_pins_only_takes_no_cycle_lock(tmp_path):
    """r1 L3. Sharing locks/fleet-watch.lock meant this held it across two 60s
    fetches, or was skipped for 30 minutes by a running dispatch cycle, or
    tracebacked on a root-owned lockfile."""
    _, pin = _pin_repo(tmp_path)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    calls = []
    mod._single_instance = lambda: calls.append(1)
    assert mod.main(["--pins-only"]) == 0
    assert calls == [], "--pins-only must not take the dispatch cycle lock"


# ------------------------------- ADR-0004 revised: sufficiency, not presence
def _approvals_bp(**over):
    base = dict(required_approvals=2,
                enable_approvals_whitelist=True,
                approvals_whitelist_username=["revbot", "operator"])
    base.update(over)
    return _bp(**base)


def _cycle_with(tmp_path, bp, orch, tag):
    mod = _load(tmp_path / f"fleet-{tag}", FLEET_WATCH_REPOS="operator/sniff")

    def api(method, path, body=None, token=None, _b=bp):
        if "/branch_protections" in path:
            return 200, [_b]
        return _api_factory()(method, path, body, token)
    _wire(mod, api, orch)
    return mod.cycle()


def test_the_production_shape_is_acceptable(tmp_path):
    """One agent and one human on the whitelist, two approvals required — what
    forgejo-create-repo.sh installs on every repo, and what the old rule refused
    fleet-wide. An agent cannot reach 2, so a human approval is structurally
    mandatory and Forgejo's scheduled auto-merge cannot route around it."""
    orch, _ = _stub_orchestrator(tmp_path)
    assert _cycle_with(tmp_path, _approvals_bp(), orch, "prod") == 1


def test_an_agent_that_could_reach_the_threshold_alone_is_refused(tmp_path):
    """The property that actually matters. One agent and ONE required approval
    means the agent alone satisfies it — and with auto-merge scheduled, main is
    written with no human in the chain."""
    orch, _ = _stub_orchestrator(tmp_path)
    assert _cycle_with(tmp_path, _approvals_bp(required_approvals=1), orch,
                       "suff") == 0


def test_two_agents_and_two_approvals_is_refused(tmp_path):
    """Sufficiency is a COUNT, not a name: adding a second agent account
    restores the agent-only path even though the number required went up."""
    orch, _ = _stub_orchestrator(tmp_path)
    bp = _approvals_bp(approvals_whitelist_username=["revbot", "implbot"])
    assert _cycle_with(tmp_path, bp, orch, "two") == 0


def test_an_unsatisfiable_rule_is_refused(tmp_path):
    """Not a safety refusal — nothing can ever satisfy it, so vouching would
    promise a dispatch that cannot happen."""
    orch, _ = _stub_orchestrator(tmp_path)
    bp = _approvals_bp(approvals_whitelist_username=["operator"])
    assert _cycle_with(tmp_path, bp, orch, "unsat") == 0


def test_an_unrestricted_approvals_whitelist_is_still_refused(tmp_path):
    """Nothing bounds the agents, so nothing makes them insufficient."""
    orch, _ = _stub_orchestrator(tmp_path)
    assert _cycle_with(tmp_path, _approvals_bp(enable_approvals_whitelist=False),
                       orch, "open") == 0


def test_the_merge_lane_stays_absolute(tmp_path):
    """A merge is not counted. One whitelisted agent writes main by itself, so
    presence — not sufficiency — is the test there."""
    orch, _ = _stub_orchestrator(tmp_path)
    bp = _approvals_bp(merge_whitelist_usernames=["operator", "revbot"])
    assert _cycle_with(tmp_path, bp, orch, "merge") == 0


def test_the_watcher_timeout_clears_the_measured_cost(tmp_path):
    """20s was below what this fleet's own pull listing costs, and the
    consequence was not a slow scan but a silent refusal to work.

    Measured 2026-09-09 on operator/eunomia (247 PRs): one page of
    `pulls?state=all&limit=50` takes 22.6s. The dedupe read is a pull listing,
    so the first armed cycle reported "dedupe unreadable for 0029-service-lease
    — not spawning" and dispatched nothing. Failing closed there is right; the
    timeout is what made it unreadable."""
    mod = _load(tmp_path / "fleet-t", FLEET_WATCH_REPOS="operator/sniff")
    assert mod.forge_timeout({}) == 60
    assert mod.forge_timeout({}) > 23, "must clear the measured 22.6s cost"


def test_the_watcher_timeout_is_configurable_and_refuses_nonsense(tmp_path):
    mod = _load(tmp_path / "fleet-t2", FLEET_WATCH_REPOS="operator/sniff")
    assert mod.forge_timeout({"FLEET_FORGE_TIMEOUT": "90"}) == 90
    for bad in ("soon", "0", "-5"):
        with pytest.raises(SystemExit):
            mod.forge_timeout({"FLEET_FORGE_TIMEOUT": bad})


def test_api_uses_it_rather_than_a_literal(tmp_path):
    """The bug was a hardcoded 20 at the call site. A test that only exercised
    forge_timeout() would pass against exactly that, which is how the same fix
    in bin/orchestrator failed to reach this file."""
    src = (BIN / "fleet-watch").read_text()
    assert "timeout=forge_timeout()" in src
    assert "urlopen(req, timeout=20)" not in src


# --- the orchestrator as its own launchd job (option D) ---------------------
# 2026-09-10: every dispatch died at its first `git fetch` because the watcher
# exits each cycle, reparenting the orchestrator to launchd as an ORPHAN. A
# process whose responsible process is gone cannot open a local-network
# connection from a newly-launched binary. A launchd JOB also reports ppid 1 —
# the difference is that launchd is its supervisor, not the reaper it fell back
# to — and its git calls succeed (matrix, 3/3).

def test_spawn_mode_defaults_to_launchd(tmp_path):
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    assert mod.spawn_mode({}) == "launchd"


def test_spawn_mode_keeps_popen_reachable(tmp_path):
    """popen is correct again the moment the watcher is RESIDENT — the orphaning
    breaks it, not the Popen — so the knob has to turn both ways."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    assert mod.spawn_mode({"FLEET_SPAWN": "popen"}) == "popen"
    with pytest.raises(SystemExit):
        mod.spawn_mode({"FLEET_SPAWN": "daemonize"})


def test_job_label_refuses_an_id_a_lease_body_could_choose(tmp_path):
    """The label reaches launchctl. Lease ids are `type--slug--NNN` and already
    safe, but the id is read from a lease BODY, and `_safe_lease_key` exists
    because a hand-edited body once reached outside its directory."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    assert mod.job_label("branch--feat-0002-ci-logs--002") == \
        "org.eunomia.fleet-orch.branch--feat-0002-ci-logs--002"
    for bad in ("../../etc/x", "a b", "", None, "x; launchctl bootout gui/501"):
        assert mod.job_label(bad) is None, bad


def test_job_env_carries_config_and_not_the_whole_environment(tmp_path):
    """The job definition is a FILE on disk. Copying os.environ wholesale means
    the day something secret enters the watcher's environment, that file
    silently becomes a leak."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    env = mod.job_env({
        "FLEET_FORGEJO_URL": "http://forge:3001",
        "EUNOMIA_SESSION": "orch-x",
        "HOME": "/Users/x", "PATH": "/usr/bin",
        "AWS_SECRET_ACCESS_KEY": "nope",
        "CLAUDE_CODE_OAUTH_TOKEN": "nope",
        "GITHUB_TOKEN": "nope",
    })
    assert env["FLEET_FORGEJO_URL"] == "http://forge:3001"
    assert env["EUNOMIA_SESSION"] == "orch-x"
    assert env["HOME"] == "/Users/x" and env["PATH"] == "/usr/bin"
    for leaked in ("AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN"):
        assert leaked not in env, f"{leaked} would be written to a plist on disk"


def test_reap_leaves_a_job_whose_lease_is_still_held(tmp_path, monkeypatch):
    """The LEASE is the authority on 'this dispatch is over', not the job's exit
    status — asking the supervisor for that would be reading the log for
    authority, which principle 3 refuses."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    mod.fleetjob.JOB_DIR = tmp_path / "jobs"
    _stub_supervisor(mod, monkeypatch)
    # job_env(), not a bare dict: reap_jobs only touches jobs THIS fleet dir
    # minted, and attribution rides in the record's EUNOMIA_FLEET_DIR.
    mod.fleetjob.start("org.eunomia.fleet-orch.branch--live--001", ["/bin/echo"],
                       mod.job_env({}), impl="launchd")
    mod.fleetjob.start("org.eunomia.fleet-orch.branch--done--001", ["/bin/echo"],
                       mod.job_env({}), impl="launchd")
    monkeypatch.setattr(mod.lib, "all_leases", lambda: [
        {"id": "branch--live--001", "state": "active"},
        {"id": "branch--done--001", "state": "released"},
    ])
    booted = _stub_supervisor(mod, monkeypatch)
    assert mod.reap_jobs() == 1
    left = sorted(x.name for x in (tmp_path / "jobs").iterdir())
    assert left == ["org.eunomia.fleet-orch.branch--live--001.plist"]
    assert any("branch--done--001" in " ".join(a) for a in booted)


def test_reap_writes_nothing_under_dry_run(tmp_path, monkeypatch):
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    mod.fleetjob.JOB_DIR = tmp_path / "jobs"
    _stub_supervisor(mod, monkeypatch)
    mod.fleetjob.start("org.eunomia.fleet-orch.branch--done--001", ["/bin/echo"],
                       mod.job_env({}), impl="launchd")
    monkeypatch.setattr(mod.lib, "all_leases", lambda: [])
    booted = _stub_supervisor(mod, monkeypatch)
    assert mod.reap_jobs(dry_run=True) == 1
    assert booted == [], "dry-run must not touch the supervisor"
    assert (tmp_path / "jobs" / "org.eunomia.fleet-orch.branch--done--001.plist").exists()


def test_a_refusing_launchctl_releases_the_lease(tmp_path, monkeypatch):
    """The launchd path's own version of the spawn failure above. A supervisor
    that refuses must leave the plan retryable rather than holding a lease no
    orchestrator is behind — the failure mode that stranded six plans on
    2026-09-09 and had to be cleared by hand."""
    fd = tmp_path / "fleet"
    orch, _log = _stub_orchestrator(tmp_path)
    mod = _load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_SPAWN="launchd",
                FLEET_DISPATCH_IMPL="launchd")
    mod.fleetjob.JOB_DIR = tmp_path / "jobs"
    _wire(mod, _api_factory(), str(orch))
    notes = []
    mod.notify = lambda t, b: notes.append(t)
    _stub_supervisor(mod, monkeypatch, rc=1,
                     out="Bootstrap failed: 5: Input/output error")
    sid = mod.spawn("operator/sniff", "plans/0009-thing.md", {"id": "0009-thing"})
    assert sid is None, "a refused bootstrap must not report a dispatch"
    assert notes, "a refused bootstrap must page"
    live = [r for r in mod.lib.all_leases()
            if r.get("state") in ("assigned", "active")]
    assert live == [], f"lease left behind with no orchestrator: {live}"


def test_no_test_reaches_the_real_launchd_domain(tmp_path):
    """The harness pins FLEET_SPAWN=popen so a unit test cannot bootstrap a job
    into the developer's own gui domain. This asserts the pin is still there,
    because losing it is silent on macOS — the bootstrap succeeds — and only
    shows up as a cihost-linux CI failure, one platform away from whoever
    removed it."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    assert mod.spawn_mode() == "popen", (
        "the test harness no longer pins the spawn mode; a dispatching test "
        "will now write plists and call launchctl for real")


# --- the watcher may be resident (option A) ---------------------------------
# SPEC principle 2, revised 2026-09-10. As a StartInterval job the watcher exits
# every cycle, reparenting every orchestrator it spawned to launchd as an ORPHAN
# — and an orphan cannot open a local-network connection from a newly-launched
# binary. The same Popen with the parent STILL ALIVE succeeded 3/3 in the matrix.

def test_the_cadence_does_not_change_when_the_process_model_does(tmp_path):
    """120s is what StartInterval was. One variable at a time: this change is
    about process lifetime, not about how often the fleet scans."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    assert mod.watch_interval({}) == 120
    assert mod.watch_interval({"FLEET_WATCH_INTERVAL": "30"}) == 30
    for bad in ("soon", "0", "4", "3601", "-1"):
        with pytest.raises(SystemExit):
            mod.watch_interval({"FLEET_WATCH_INTERVAL": bad})


def test_the_service_resource_is_keyed_on_the_unit_not_the_label(tmp_path):
    """Plan 0029's whole point: the 2026-09-07 incident was ONE runner
    registered under TWO labels, which would have been two accepted leases if
    identity had been the label."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    r = mod._service_resource()
    assert r["type"] == "service"
    assert r["service"].endswith(":fleet-watch")
    assert r["service"].startswith(r["host"] + ":")
    assert "com.operator" not in str(r), "keyed on the launchd label"


def test_a_foreign_active_holder_blocks_a_second_scheduler(tmp_path, monkeypatch):
    """Regardless of orphanhood — fleet-svc step 4, and for its reason: past a
    lease's ttl fleet-claim's orphan-skip grants a SECOND --assign, so two
    active rows on one unit is reachable and both holders pass 'is it mine'."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    res = mod._service_resource()
    ancient = {"id": "service--fleet-watch--001", "state": "active",
               "holder": "watch-someone-else", "resource": res,
               "activated": "2000-01-01T00:00:00Z", "hb": "2000-01-01T00:00:00Z"}
    monkeypatch.setattr(mod.lib, "all_leases", lambda: [ancient])
    monkeypatch.setattr(mod.lib, "resource_of", lambda rec: rec["resource"])
    # "Regardless of orphanhood" is asserted structurally rather than by ageing a
    # fixture: the check must never CONSULT orphanhood, so making is_orphaned
    # explode proves it is not on the path. A fixture aged past ttl would only
    # prove it for that one age.
    def _boom(*a, **k):
        raise AssertionError("_foreign_service_holder consulted is_orphaned")
    monkeypatch.setattr(mod.lib, "is_orphaned", _boom)
    assert mod._foreign_service_holder("watch-me") is ancient
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    assert mod.acquire_service_lease("watch-me") is None
    assert any("refusing to start a second scheduler" in w for w in warned)


def test_my_own_lease_is_not_foreign(tmp_path, monkeypatch):
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    res = mod._service_resource()
    mine = {"id": "service--fleet-watch--001", "state": "active",
            "holder": "watch-me", "resource": res}
    monkeypatch.setattr(mod.lib, "all_leases", lambda: [mine])
    monkeypatch.setattr(mod.lib, "resource_of", lambda rec: rec["resource"])
    assert mod._foreign_service_holder("watch-me") is None


def test_a_raising_cycle_does_not_kill_the_scheduler(tmp_path, monkeypatch):
    """Under KeepAlive a crash WOULD be restarted — and restarting is the
    loudest possible way to lose every in-flight orchestrator, because they are
    this process's children and the restart orphans all of them. That is the
    exact fault resident mode exists to remove."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    calls = {"n": 0}

    def flaky(dry_run=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("forge unreachable this cycle")
        if calls["n"] >= 3:
            raise KeyboardInterrupt      # end the test
        return 0

    monkeypatch.setattr(mod, "cycle", flaky)
    monkeypatch.setattr(mod, "check_pins", lambda dry_run=False: [])
    monkeypatch.setattr(mod, "watch_interval", lambda env=None: 5)
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    with pytest.raises(KeyboardInterrupt):
        mod.resident(dry_run=True)
    assert calls["n"] >= 3, "the loop stopped at the first exception"
    assert any("continuing" in w for w in warned)


def test_resident_releases_its_lease_on_the_way_out(tmp_path, monkeypatch):
    """A scheduler that exits still holding the lease makes the next start
    refuse — the deadlock this lease exists to prevent, caused by the lease."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    monkeypatch.setattr(mod, "acquire_service_lease", lambda sid: "service--x--001")
    monkeypatch.setattr(mod, "check_pins", lambda dry_run=False: [])
    monkeypatch.setattr(mod, "watch_interval", lambda env=None: 5)
    monkeypatch.setattr(mod.lib, "touch_heartbeat", lambda sid: None)
    retired = []
    monkeypatch.setattr(mod, "_retire", lambda lid: retired.append(lid) or True)

    def once(dry_run=False):
        raise KeyboardInterrupt

    monkeypatch.setattr(mod, "cycle", once)
    with pytest.raises(KeyboardInterrupt):
        mod.resident(dry_run=False)
    assert retired == ["service--x--001"], "lease not released on exit"


def test_resident_refuses_to_run_without_a_lease(tmp_path, monkeypatch):
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    monkeypatch.setattr(mod, "acquire_service_lease", lambda sid: None)
    ran = []
    monkeypatch.setattr(mod, "cycle", lambda dry_run=False: ran.append(1))
    assert mod.resident(dry_run=False) == 1
    assert ran == [], "cycled anyway without a lease"


def test_no_module_level_fleet_state(tmp_path):
    """What makes resident mode safe: nothing in this module holds fleet state
    in memory, so a cycle leaves nothing for the next to inherit and killing the
    process at any instant loses nothing. That is principle 2 having been obeyed
    while the process happened to be short-lived — and it is the condition the
    revised principle attaches to being resident. A module-level dict or set
    added later would quietly void it."""
    import ast
    tree = ast.parse((BIN / "fleet-watch").read_text())
    bad = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, (ast.Dict, ast.List, ast.Set)):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and not t.id.isupper():
                bad.append(t.id)
    assert bad == [], f"mutable module-level state would persist across cycles: {bad}"


def test_reap_never_touches_a_job_another_fleet_dir_minted(tmp_path, monkeypatch):
    """Review of #264, HIGH, reproduced by execution before it shipped.

    fleetjob.JOB_DIR is uid-global and fixed from HOME at import; `live` comes
    from EUNOMIA_FLEET_DIR. A watcher pointed at a different ledger therefore
    finds every OTHER watcher's job records, concludes their leases do not
    exist, and stops them — and that stop on a running job is a SIGTERM. That
    is not tidy-up; it is killing an orchestrator mid-implement and deleting
    the evidence.

    The test suite is exactly such a watcher: `_load` redirects the fleet dir to
    tmp_path and dozens of tests reach an armed cycle(), so on the dispatch host
    `pytest tests/test_fleet_watch.py` would SIGTERM whatever the fleet was
    working on and still pass.
    """
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    mod.fleetjob.JOB_DIR = tmp_path / "jobs"
    _stub_supervisor(mod, monkeypatch)
    # one job minted by THIS fleet dir, one by somebody else's
    mod.fleetjob.start("org.eunomia.fleet-orch.branch--mine--001", ["/bin/echo"],
                       mod.job_env({}), impl="launchd")
    mod.fleetjob.start("org.eunomia.fleet-orch.branch--theirs--001", ["/bin/echo"],
                       {"EUNOMIA_FLEET_DIR": "/Users/someone/dev/.fleet"},
                       impl="launchd")
    monkeypatch.setattr(mod.lib, "all_leases", lambda: [])   # neither lease exists here
    booted = _stub_supervisor(mod, monkeypatch)
    assert mod.reap_jobs() == 1, "should reap exactly its own"
    left = sorted(x.name for x in (tmp_path / "jobs").iterdir())
    assert left == ["org.eunomia.fleet-orch.branch--theirs--001.plist"], left
    assert not any("theirs" in " ".join(a) for a in booted), \
        "SIGTERMed an orchestrator this watcher did not start"


def test_an_unattributable_job_is_left_alone(tmp_path, monkeypatch):
    """A record we cannot attribute is one we must not destroy — fail toward
    not touching, because the cost of a wrong reap is a killed run and the
    cost of a missed one is a stale file."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    mod.fleetjob.JOB_DIR = tmp_path / "jobs"
    mod.fleetjob.JOB_DIR.mkdir(parents=True, exist_ok=True)
    (mod.fleetjob.JOB_DIR / "org.eunomia.fleet-orch.branch--junk--001.plist").write_text(
        "this is not a plist")
    monkeypatch.setattr(mod.lib, "all_leases", lambda: [])
    booted = _stub_supervisor(mod, monkeypatch)
    assert mod.reap_jobs() == 0
    assert booted == []
    assert (mod.fleetjob.JOB_DIR /
           "org.eunomia.fleet-orch.branch--junk--001.plist").exists()


def test_a_job_carries_the_fleet_dir_that_minted_it(tmp_path, monkeypatch):
    """Stamped explicitly, not passed through: on a default fleet dir the
    variable is unset in the environment, so a prefix filter would leave every
    production job unattributable and therefore never reaped."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    env = mod.job_env({"PATH": "/usr/bin"})
    assert env["EUNOMIA_FLEET_DIR"] == str(mod.lib.fleet_dir())
    mod.fleetjob.JOB_DIR = tmp_path / "jobs"
    _stub_supervisor(mod, monkeypatch)
    label = "org.eunomia.fleet-orch.branch--x--001"
    mod.fleetjob.start(label, ["/bin/echo"], env, impl="launchd")
    assert mod.fleetjob.job_environment(label)["EUNOMIA_FLEET_DIR"] == \
        str(mod.lib.fleet_dir())


def test_the_harness_cannot_reach_the_real_job_directory(tmp_path):
    """The second lock. The attribution check above is the real fix; this asserts
    the harness redirect is still in place, so a future change to attribution
    cannot silently re-arm the suite against the developer's own launchd domain."""
    mod = _load(tmp_path / "f", FLEET_WATCH_REPOS="operator/sniff")
    assert str(mod.fleetjob.JOB_DIR).startswith(str(tmp_path)), (
        f"the suite would reap production job definitions: {mod.fleetjob.JOB_DIR}")
