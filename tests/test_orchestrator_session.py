"""Tests for bin/orchestrator part 2a — the implementer session (plan 0010).

Every test here runs without a network and without a model. The session is a
real spawned process — a fake `claude` on PATH — because the things being
claimed are process facts: the environment the child actually receives, the
process GROUP a timeout kills, and what lands in the transcript file. Stubbing
`subprocess` would prove that a function was called; the whole of review 1861's
H1 is that the call can be made and the token still land.

The forge side is `verify_marked_pr` and `emit`/`notify` as module attributes,
except where the verification logic itself is under test — there a fake Forge
object answers the real function.
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

_ORCH_ENV = ("EUNOMIA_SESSION", "EUNOMIA_PLAN_ID", "EUNOMIA_PLAN_ZONE",
             "EUNOMIA_PLAN_TIER", "FLEET_WORK_ROOT", "FLEET_GIT_SSH_BASE",
             "FLEET_NTFY_URL", "FLEET_HEARTBEAT_SECS", "EUNOMIA_INITIATOR",
             "EUNOMIA_IMPL_RUNNER",
             "EUNOMIA_TRANSCRIPT_DIR", "FLEET_TOKEN_CMD",
             "FLEET_IMPLEMENTER_UIDS", "FLEET_IMPLEMENTER_LOGINS",
             # plan 0056's role table (lib.agent_roles()), read live by
             # implementer_accounts() and set_commit_identity()
             "FLEET_COMMIT_IDENTITY", "FLEET_AUTHOR_ACCOUNT",
             "FLEET_REVIEWER_ACCOUNT", "FLEET_MERGER_ACCOUNT",
             "FLEET_AGENT_ACCOUNTS")

# A 40-hex Forgejo PAT is the credential an implementer session actually holds,
# and it is the shape no pattern in FINDINGS matches — `long-hex`, in the
# PARANOID set, is the only one that does. Not a git SHA of anything.
TOKEN = "b17c0ffee5a9d3e2f10ab4c96d7e8f0123456789"

PLAN = """\
---
id: 0042-a-feature
status: ready
zone: public
tier: 1
paths: ["bin/thing"]
---

# A feature

## 3. Boundaries
- do not X, because Y — the reasoning is what transfers
"""

LEASE = "branch--0042-a-feature--001"


@pytest.fixture(autouse=True)
def _restore_environ():
    """`_load` mutates os.environ on purpose — the module reads it at import —
    so every test gets the process environment back afterwards. Without this a
    test that empties PATH to prove a missing harness breaks the NEXT test's
    `git`, and the failure lands nowhere near its cause."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _load(fleet_dir, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    for k in _ORCH_ENV:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("orchestrator", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(args, cwd):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _forge_dir(tmp_path, plan_text=PLAN):
    """A bare repo on disk that `origin` can reach over a file:// style path."""
    bare = tmp_path / "forge" / "operator" / "demo.git"
    if bare.exists():          # a test that loads the module twice reuses it
        return str(tmp_path / "forge")
    src = tmp_path / "src"
    (src / "plans").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / "plans" / "0042-a-feature.md").write_text(plan_text)
    _git(["add", "."], src)
    _git(["commit", "-qm", "plan"], src)
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return str(tmp_path / "forge")


def _forge_branch(tmp_path, branch="feat/0042-a-feature"):
    """Create `branch` in the fixture's bare repo, so a resume can adopt it the
    way it adopts a real one — fetch and all, rather than a stubbed worktree."""
    bare = tmp_path / "forge" / "operator" / "demo.git"
    subprocess.run(["git", "branch", "-f", branch, "main"], cwd=str(bare),
                   check=True, capture_output=True)
    return branch


def _fake_claude(tmp_path, body):
    """A `claude` on PATH that does exactly what a test needs and nothing else."""
    binp = tmp_path / "fakebin"
    binp.mkdir(exist_ok=True)
    exe = binp / "claude"
    # sys.executable, not `env python3`: under CI the suite runs from a venv and
    # the child must be the same interpreter that is certainly installed
    exe.write_text(f"#!{sys.executable}\n" + body)
    exe.chmod(0o755)
    return binp


def _lease(mod, lease_id=LEASE, branch="feat/0042-a-feature", state="active"):
    d = mod.lib.leases_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{lease_id}.json").write_text(json.dumps({
        "id": lease_id, "state": state, "holder": "orch-test",
        "resource": {"type": "branch", "repo": "operator/demo", "branch": branch},
    }))
    return branch


def _mod(tmp_path, plan_text=PLAN, path_extra=None, **env):
    ssh = env.pop("ssh", None) or _forge_dir(tmp_path, plan_text)
    path = env.pop("PATH", None) or os.environ["PATH"]
    if path_extra:
        path = f"{path_extra}{os.pathsep}{path}"
    defaults = {"FLEET_WORK_ROOT": str(tmp_path / "work"),
                "FLEET_GIT_SSH_BASE": ssh, "EUNOMIA_SESSION": "orch-test",
                "EUNOMIA_TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
                "PATH": path}
    defaults.update(env)
    return _load(tmp_path / "fleet", **defaults)


def _wire(mod, verified=(True, "", 7), review=None, steward=None):
    calls = {"notify": [], "emit": [], "verify": [], "review": [], "comment": [],
             "steward": []}
    mod.notify = lambda t, b: (calls["notify"].append((t, b)), True)[1]
    mod.emit = lambda et, repo, detail, pr=None: calls["emit"].append((et, repo, detail))
    mod.activate = lambda lid: (True, "")
    # The pull-listing retry sleeps 40+80+160+320s for real. Every test that
    # stubs a FAILING read would otherwise take ten minutes instead of failing.
    mod._sleep = lambda secs: None

    # `log=` since the real verify_marked_pr stamps a missing marker and
    # records it; a stub that cannot accept it hides that from every test
    # that goes through this helper.
    def _verify(repo, branch, plan_id, plan_path, forge=None, mod=None,
                log=None):
        calls["verify"].append((repo, branch, plan_id, plan_path))
        return verified
    mod.verify_marked_pr = _verify

    # The review loop is stubbed here so the tests ABOVE it keep testing what
    # they were written to test. Its own behaviour is covered in
    # tests/test_orchestrator_review.py, against the real function.
    rows = [{"round": 1, "sha": "a" * 40, "verdict": "APPROVED",
             "blocking": 0, "body": ""}]
    outcome = review if review is not None else (True, "", rows)

    def _review_loop(repo, plan_id, plan_path, plan_text, pr, branch, wt, cred,
                     route, lease_id, log=None):
        calls["review"].append((repo, pr, branch))
        return outcome
    mod.review_loop = _review_loop
    mod.post_comment = lambda repo, pr, body, forge=None, m=None: (
        calls["comment"].append(body), True)[1]

    # The steward is stubbed for the same reason as the review loop, and for one
    # more: unstubbed it is an UNBOUNDED poll loop, so a full-run test that
    # reaches it does not fail, it hangs. Its own behaviour is covered against
    # the real function, with max_cycles, in tests/test_orchestrator_steward.py.
    def _steward(repo, plan_id, plan_path, plan_text, pr, branch, wt, cred,
                 route, lease_id, log=None, max_cycles=None):
        calls["steward"].append((repo, pr, branch))
        return 0 if steward is None else steward
    mod.steward = _steward
    return calls


def _run(mod, lease_id=LEASE):
    return mod.main(["operator/demo", "plans/0042-a-feature.md", lease_id])


# ------------------------------------------------------------- the credential
def test_an_unset_initiator_is_the_operator(tmp_path):
    """Unset is the normal production path, not an error to be defaulted past."""
    mod = _mod(tmp_path)
    cred = mod.resolve_credential()
    assert cred.kind == "local_cli" and cred.initiator == "operator"


def test_an_unrecognised_initiator_stops_the_run_loudly(tmp_path):
    """Reachable by SETTING the variable — not only through a test double. A
    fallback to the operator would bill the wrong person for someone else's
    run."""
    mod = _mod(tmp_path, EUNOMIA_INITIATOR="somebody-else")
    with pytest.raises(mod.Stop) as e:
        mod.resolve_credential()
    assert "somebody-else" in e.value.reason
    assert "bill someone else" in e.value.reason


def test_an_unrecognised_initiator_fails_the_whole_run_holding_its_lease(tmp_path):
    mod = _mod(tmp_path, EUNOMIA_INITIATOR="somebody-else")
    calls = _wire(mod)
    _lease(mod)
    assert _run(mod) == 1
    assert "somebody-else" in calls["emit"][0][2]["reason"]
    assert calls["verify"] == [], "a run that never spawned must not claim a PR"
    assert "def release(" not in (BIN / "orchestrator").read_text()


# ----------------------------------------------------------------- the runner
def test_the_default_runner_is_a_registry_entry_not_a_model_name(tmp_path):
    mod = _mod(tmp_path)
    assert mod.DEFAULT_RUNNER in mod.RUNNERS
    r = mod.RUNNERS[mod.DEFAULT_RUNNER]
    assert r.name == "sub-sonnet" and r.needs == "local_cli" and r.local is False


def test_the_override_selects_a_runner_and_an_unknown_name_stops(tmp_path):
    mod = _mod(tmp_path, EUNOMIA_IMPL_RUNNER="sub-opus")
    cred = mod.resolve_credential()
    assert mod.route_implementer({"zone": "public"}, cred).name == "sub-opus"
    mod2 = _mod(tmp_path, EUNOMIA_IMPL_RUNNER="sub-gpt")
    with pytest.raises(mod2.Stop) as e:
        mod2.route_implementer({"zone": "public"}, cred)
    assert "names no runner" in e.value.reason


def test_a_runner_needing_a_credential_kind_we_do_not_have_is_a_hard_stop(tmp_path):
    """`api-sonnet` served by `local_cli` silently spends the operator's
    subscription while every event and cost report calls it metered."""
    mod = _mod(tmp_path, EUNOMIA_IMPL_RUNNER="api-sonnet")
    cred = mod.resolve_credential()
    assert cred.kind == "local_cli"
    with pytest.raises(mod.Stop) as e:
        mod.route_implementer({"zone": "public"}, cred)
    assert "api-sonnet" in e.value.reason and "local_cli" in e.value.reason


@pytest.mark.parametrize("zone", ["private", "", "Public", "PUBLIC", "publick"])
def test_the_override_is_subject_to_the_zone_check_not_a_way_past_it(tmp_path, zone):
    """ADR-0006 §7 is an ALLOWLIST: `zone: public` dispatches, anything else —
    private, absent, malformed, mis-cased — does not. The opposite polarity
    (`if zone == "private": refuse`) sends a plan with no zone line to a
    vendor."""
    mod = _mod(tmp_path, EUNOMIA_IMPL_RUNNER="sub-opus")
    cred = mod.resolve_credential()
    with pytest.raises(mod.Stop) as e:
        mod.route_implementer({"zone": zone}, cred)
    assert "not 'public'" in e.value.reason


def test_the_zone_rule_matches_the_documented_rule(tmp_path):
    """The registry's `local` flag is the single local expression of the zone
    rule, so it is asserted against the rule as this repository states it —
    ADR-0006 §7 and docs/feature-plans.md. (`ROUTING.md` §3, which plan 0010
    names, lives in operator/agent-bus and is not readable from this suite; the
    in-repo statement of the same rule is what a test can pin.)"""
    mod = _mod(tmp_path)
    adr = (ROOT / "docs" / "adr" / "0006-blackbox-test-lanes.md").read_text()
    assert "Only `zone: public` dispatches an external lane." in adr
    assert "anything else, including absent, malformed or unreadable" in adr
    # exactly one runner may carry a non-public plan, and it is the local one
    local = {n for n, r in mod.RUNNERS.items() if r.local}
    assert local == {"local-qwen"}
    cred = mod.resolve_credential()
    assert mod.route_implementer({"zone": "public"}, cred).local is False


def test_a_declared_but_unwired_runner_stops_with_its_own_reason(tmp_path):
    """`local-qwen` is the only runner a `zone: private` plan can resolve to and
    is expected to land unexercised — so it must fail loudly and legibly rather
    than as a generic spawn OSError."""
    mod = _mod(tmp_path, EUNOMIA_IMPL_RUNNER="local-qwen")
    cred = mod.resolve_credential()
    route = mod.route_implementer({"zone": "private"}, cred)
    assert route.local is True
    with pytest.raises(mod.Stop) as e:
        mod.harness_argv(route)
    assert "declared but not wired" in e.value.reason
    assert "task file" in e.value.reason


def test_a_missing_harness_binary_stops_with_a_different_reason(tmp_path):
    """A binary that is not installed reads nothing like a runner that was never
    wired, and the operator debugging at 3am needs to know which they have."""
    mod = _mod(tmp_path, PATH=str(tmp_path / "empty-bin"))
    with pytest.raises(mod.Stop) as e:
        mod.harness_argv(mod.RUNNERS["sub-sonnet"])
    assert "not on PATH" in e.value.reason
    assert "declared but not wired" not in e.value.reason


def test_no_runner_may_skip_permissions_and_every_one_names_its_mode(tmp_path):
    """The allowlist is surface reduction, not confinement — but it is still
    passed, the mode is still named, and the escape hatch is still absent.

    The escape-hatch absence (`--dangerously-skip-permissions`) is asserted
    registry- and code-wide, unconditionally. `--permission-mode` /
    `--allowedTools` / the `WebFetch`/`Task` absence are Claude Code flags, so
    they apply only to runners whose `argv[0]` basename is `claude` — the
    property the flags actually belong to (plan 0066 D8). Two non-Claude
    vendor runners (`sub-antigravity`, `sub-codex`) are in the registry now,
    and scoping by `family` instead would silently assert on nothing the day a
    family value moves, so the scope is the binary itself."""
    mod = _mod(tmp_path)
    src = (BIN / "orchestrator").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "--dangerously-skip-permissions" not in code
    for name, r in mod.RUNNERS.items():
        if not r.argv:
            continue
        if os.path.basename(r.argv[0]) != "claude":
            continue
        assert "--permission-mode" in r.argv, name
        assert "--allowedTools" in r.argv, name
        assert "WebFetch" not in " ".join(r.argv) and "Task" not in " ".join(r.argv)


# ------------------------------------------------------------ the environment
ENV_DUMP = """
import json, os, sys
sys.stdin.read()
open({dest!r}, "w").write(json.dumps(dict(os.environ)))
print("done")
"""


def test_the_child_environment_is_an_allowlist(tmp_path):
    """The child runs arbitrary shell, so enumerating what to withhold is the
    wrong way round. FLEET_TOKEN_CMD in particular names the admin-token helper
    on this fleet, and an admin token can PATCH a branch protection, push a
    `status: ready` plan and restore the rule."""
    dest = tmp_path / "childenv.json"
    binp = _fake_claude(tmp_path, ENV_DUMP.format(dest=str(dest)))
    mod = _mod(tmp_path, path_extra=str(binp))
    os.environ["FLEET_TOKEN_CMD"] = "/Users/x/bin/fetch-forgejo-admin-token.sh"
    os.environ["ANTHROPIC_API_KEY"] = "sk-not-for-this-runner"
    try:
        _wire(mod)
        _lease(mod)
        assert _run(mod) == 0          # the whole pipeline, through to the steward
        child = json.loads(dest.read_text())
    finally:
        os.environ.pop("FLEET_TOKEN_CMD", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
    assert "FLEET_TOKEN_CMD" not in child
    assert "ANTHROPIC_API_KEY" not in child, "sub-sonnet does not declare it"
    assert "EUNOMIA_SESSION" not in child, (
        "not until fleet-install-hooks runs against the store — and then "
        "FLEET_PUSH_OVERRIDE stays forbidden")
    assert not [k for k in child if k.split("_")[0] in
                ("FLEET", "EUNOMIA", "ANTHROPIC", "CLAUDE", "OPENAI", "GITHUB",
                 "AWS", "FORGEJO", "VAULT", "BAO")], sorted(child)
    # What is passed IS the allowlist. What the child then OBSERVES can be a
    # superset, and that difference is worth stating rather than asserting past:
    # macOS adds __CF_USER_TEXT_ENCODING at exec, and Apple's `python3` shim
    # exports SDKROOT/CPATH/LIBRARY_PATH/MANPATH before the script runs. Neither
    # is inherited from this process, so an assertion that ignored them would be
    # testing the platform; one that demanded exact equality would fail on a
    # machine that is behaving correctly.
    injected = {"__CF_USER_TEXT_ENCODING", "SDKROOT", "CPATH", "LIBRARY_PATH",
                "MANPATH", "__PYVENV_LAUNCHER__", "PYTHONHOME", "PYTHONPATH"}
    assert set(child) - set(mod._ENV_ALLOW) <= injected, sorted(set(child) - set(mod._ENV_ALLOW))
    assert child["HOME"] == os.environ["HOME"]


def test_what_the_wrapper_passes_is_exactly_the_allowlist(tmp_path):
    """The half of the previous test that is about this code rather than the
    platform: nothing outside _ENV_ALLOW, plus only what the runner declares."""
    mod = _mod(tmp_path)
    os.environ["FLEET_TOKEN_CMD"] = "/Users/x/bin/fetch-forgejo-admin-token.sh"
    os.environ["ANTHROPIC_API_KEY"] = "sk-x"
    assert set(mod.implementer_env(mod.RUNNERS["sub-sonnet"])) <= set(mod._ENV_ALLOW)
    api = mod.implementer_env(mod.RUNNERS["api-sonnet"])
    assert set(api) - set(mod._ENV_ALLOW) == {"ANTHROPIC_API_KEY"}, (
        "a runner gets what it declares and nothing else")
    assert "FLEET_TOKEN_CMD" not in api


def test_the_allowlist_is_fleet_candidate_s_plus_exactly_the_agent(tmp_path):
    """Was an equality check, on the reasoning that a divergence would be "a
    second, quieter policy". The divergence is now deliberate and is exactly one
    entry, so the check states the relationship instead of forbidding it.

    The two children have OPPOSITE jobs. The implementer must reach the forge —
    it opens the marked pull request, and without an agent it commits work nobody
    can see (measured 2026-09-10, pr-proxy 0002-ci-logs). `fleet-candidate`'s
    child must NOT: that file's docstring says "THIS TOOL NEVER PUSHES — meaning
    its own git surface cannot", and `_git` refuses a push argv to make it
    structural. Handing that child an agent would weaken a guarantee the tool
    states in its own header.

    Still tight: any OTHER divergence, in either direction, fails here."""
    mod = _mod(tmp_path)
    cand = (BIN / "fleet-candidate").read_text()
    named = re.search(r"_ENV_ALLOW = \(([^)]*)\)", cand).group(1)
    theirs = tuple(re.findall(r'"([A-Z_]+)"', named))
    assert "SSH_AUTH_SOCK" not in theirs, (
        "fleet-candidate's child gained an ssh agent — that contradicts its own "
        "never-pushes guarantee")
    assert mod._ENV_ALLOW == theirs + ("SSH_AUTH_SOCK",), (
        "the two allowlists differ by something other than the agent")


# -------------------------------------------------------------- the transcript
SPLIT_TOKEN = """
import sys, time
sys.stdin.read()
sys.stdout.write("authenticating as implbot, using " + {head!r})
sys.stdout.flush()
time.sleep(0.4)
sys.stdout.write({tail!r} + " done\\n")
sys.stdout.flush()
"""


def test_a_token_split_across_two_reads_does_not_reach_the_transcript(tmp_path):
    """The finding this pins: `redact()` can be CALLED and the token still land.

    Two ways it lands, both covered here. The wrong pattern set — FINDINGS alone
    matches no 40-hex string, and a Forgejo PAT is exactly that — and per-chunk
    redaction, where a token arriving in two `read()`s matches nothing in either
    half. `Session.reads >= 2` is asserted so the split is known to have really
    happened: a control tested only against input it never sees is not tested."""
    binp = _fake_claude(tmp_path, SPLIT_TOKEN.format(head=TOKEN[:12], tail=TOKEN[12:]))
    mod = _mod(tmp_path, path_extra=str(binp))
    calls = _wire(mod)
    _lease(mod)
    assert _run(mod) == 0   # reaches the steward; 0031 wired it
    tpath = tmp_path / "transcripts" / f"{LEASE}.log"
    assert tpath.exists(), "no transcript was written"
    written = tpath.read_text()
    assert TOKEN not in written, "the whole token reached the transcript"
    assert TOKEN[12:] not in written, "the tail alone reached the transcript"
    assert "[redacted:long-hex]" in written, (
        "long-hex is the ONLY pattern that matches a 40-hex Forgejo PAT, and it "
        "is in the PARANOID set — the fixture avoids the words that would let "
        "the `assignment` shape catch it first and hide that")
    assert "authenticating as implbot" in written, "the transcript is not merely empty"


def test_the_split_really_happens_and_the_buffer_is_what_is_redacted(tmp_path):
    binp = _fake_claude(tmp_path, SPLIT_TOKEN.format(head=TOKEN[:12], tail=TOKEN[12:]))
    mod = _mod(tmp_path, path_extra=str(binp))
    cred = mod.resolve_credential()
    route = mod.route_implementer({"zone": "public"}, cred)
    wt = tmp_path / "wt"
    wt.mkdir()
    s = mod.run_implementer(wt, "go\n", cred, route, LEASE)
    assert s.reads >= 2, ("the fake harness did not split the token across two "
                          "reads; this test would pass against per-chunk redaction")
    assert "long-hex" in s.shapes
    assert TOKEN not in s.transcript.read_text()


def test_transcripts_use_the_paranoid_set_and_redact_does_not(tmp_path):
    """`redact()` keeps the narrow set on purpose — a head SHA is what an
    approval binds to. A transcript has no such field to protect."""
    mod = _mod(tmp_path)
    assert "long-hex" in [n for n, _ in mod._transcript_patterns()]
    assert "long-hex" not in [n for n, _ in mod._redaction_patterns()]
    sha = "755a5b9fd2a2c8d94ebfcc1634e3da5c098bcb75"
    assert sha in mod.redact(f"at {sha}")[0]
    assert sha not in mod.redact_transcript(f"at {sha}")[0]


def test_the_transcript_lives_outside_the_fleet_tree_and_is_private(tmp_path):
    """SPEC Principle 6: nothing under ~/dev/.fleet may carry secret material,
    and atlas renders that tree."""
    mod = _mod(tmp_path)
    d = mod.transcript_dir()
    fleet = mod.lib.fleet_dir().resolve()
    assert not str(d).startswith(str(fleet) + os.sep) and d != fleet
    path, _ = mod.write_transcript(LEASE, f"a token {TOKEN}\n")
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert TOKEN not in path.read_text()


def test_a_transcript_dir_inside_the_fleet_tree_is_refused(tmp_path):
    """The knob exists for the suite, and a fixture pointing it inside the tree
    would silently reintroduce exactly what the path convention prevents."""
    inside = tmp_path / "fleet" / "transcripts"
    mod = _mod(tmp_path, EUNOMIA_TRANSCRIPT_DIR=str(inside))
    with pytest.raises(mod.Stop) as e:
        mod.transcript_dir()
    assert "Principle 6" in e.value.reason


def test_the_default_transcript_root_is_the_documented_one(tmp_path):
    mod = _mod(tmp_path)
    os.environ.pop("EUNOMIA_TRANSCRIPT_DIR")
    assert mod.transcript_dir() == (Path.home() / "dev" / ".orchestrator-transcripts").resolve()


# ------------------------------------------------------------------ the log
def test_the_run_log_is_open_before_the_failure_it_records(tmp_path, monkeypatch):
    """Opened before the lease is activated, because the watcher spawns with
    stderr=DEVNULL and a log that starts after the first failure documents every
    run except the ones worth reading."""
    mod = _mod(tmp_path, ssh=str(tmp_path / "no-such-forge"))
    # A forge that does not exist fails every attempt, so this walks the whole
    # of WORKSPACE_BACKOFF. This test is about WHEN the log opens, not pacing.
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = _wire(mod)
    _lease(mod)
    assert _run(mod) == 1
    log = mod.run_log_path(LEASE)
    assert log.exists(), "the workspace failed before anything opened a log"
    text = log.read_text()
    assert "workspace-failed" in text
    assert "start" in text.splitlines()[0]
    # the page is the failure channel; the log is what it sends the reader to
    assert str(log) in calls["notify"][0][1]
    assert calls["emit"][0][2]["log"] == str(log)


def test_the_run_log_redacts_itself(tmp_path):
    mod = _mod(tmp_path)
    log = mod.RunLog(LEASE)
    log.note("phase", detail=f"token {TOKEN}")
    log.close()
    assert TOKEN not in mod.run_log_path(LEASE).read_text()


# -------------------------------------------------------------- the preamble
def test_the_preamble_reaches_the_prompt_byte_for_byte_ahead_of_the_plan(tmp_path):
    mod = _mod(tmp_path)
    text = (BIN / "implementer-preamble.md").read_text()
    prompt = mod.build_implementer_prompt("operator/demo", "0042-a-feature",
                                          "plans/0042-a-feature.md", PLAN,
                                          "/w/t", "feat/0042-a-feature")
    assert prompt.startswith(text), "the preamble was reformatted on the way in"
    assert prompt.index(text) < prompt.index(PLAN)
    assert PLAN in prompt, "the plan must arrive verbatim, boundaries and all"
    assert "Plan: 0042-a-feature" in prompt
    assert "feat/0042-a-feature" in prompt and "/w/t" in prompt


def test_a_missing_preamble_is_a_hard_stop_not_an_empty_string(tmp_path):
    """A run that quietly proceeded would look identical, in every event and
    every log, to one that briefed its session properly."""
    mod = _mod(tmp_path)
    with pytest.raises(mod.Stop) as e:
        mod.read_preamble(tmp_path / "nope.md")
    assert "unreadable" in e.value.reason


def test_the_preamble_never_names_the_admin_helper(tmp_path):
    """And that is NOT the control: the helper is a fixed path any process
    running as the operator can execute, its name is in docs/plan-dispatch.md
    inside the worktree the session is told to read, and the session runs
    arbitrary commands. It is surface reduction only — bin/orchestrator says so
    where the allowlist is defined."""
    text = (BIN / "implementer-preamble.md").read_text()
    assert "admin" not in text.lower()
    assert "fetch-forgejo-token.sh" in text


# ------------------------------------------------------------------ the branch
def test_the_branch_is_learned_from_the_lease_not_recomputed(tmp_path):
    """A second copy of the slug rule drifts, and the drift lands as a push on a
    branch the lease does not cover."""
    mod = _mod(tmp_path)
    _lease(mod, branch="feat/renamed-by-hand")
    assert mod.lease_branch(LEASE) == "feat/renamed-by-hand"


def test_a_non_branch_lease_stops_the_run(tmp_path):
    mod = _mod(tmp_path)
    d = mod.lib.leases_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "paths--x--001.json").write_text(json.dumps(
        {"id": "paths--x--001", "state": "active",
         "resource": {"type": "paths", "repo": "operator/demo", "paths": ["a"]}}))
    with pytest.raises(mod.Stop) as e:
        mod.lease_branch("paths--x--001")
    assert "not a branch lease" in e.value.reason


def test_the_worktree_is_actually_on_the_lease_branch_when_the_session_runs(tmp_path):
    """fleetlib checks out --detach at origin/main, so the branch does not exist
    until this wrapper makes it."""
    dest = tmp_path / "branch.txt"
    binp = _fake_claude(tmp_path, f"""
import subprocess, sys
sys.stdin.read()
open({str(dest)!r}, "w").write(subprocess.run(
    ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
    text=True).stdout)
""")
    mod = _mod(tmp_path, path_extra=str(binp))
    _wire(mod)
    _lease(mod, branch="feat/0042-a-feature")
    assert _run(mod) == 0   # reaches the steward; 0031 wired it
    assert dest.read_text().strip() == "feat/0042-a-feature"


# ------------------------------------------------------------------ the run
def test_a_zero_exit_with_no_marked_pr_fails_and_holds_the_lease(tmp_path):
    """The session is the judgment core; the guarantee is verify_marked_pr. A
    model reporting success having opened nothing must fail the run — dedupe is
    'a marked PR exists', so an unmarked PR re-dispatches the plan while its work
    sits in a branch nobody will look at."""
    binp = _fake_claude(tmp_path, "import sys; sys.stdin.read(); print('all done!')")
    mod = _mod(tmp_path, path_extra=str(binp))
    calls = _wire(mod, verified=(False, "no pull request with head feat/x", None))
    _lease(mod)
    assert _run(mod) == 1
    reason = calls["emit"][0][2]["reason"]
    assert reason.startswith("no verified marked PR")
    assert "session exit 0" in reason
    assert "not wired yet" not in reason
    assert calls["notify"], "a failed run that does not page is a silent stall"


# ------------------------------------------- plan 0079: the backend-error reason
def _backend_claude(tmp_path, status=500, message="API Error: 500 boom", commit=False):
    rec = {"type": "result", "is_error": True, "api_error_status": status,
           "result": message}
    commit_step = ""
    if commit:
        commit_step = (
            "import subprocess\n"
            "subprocess.run(['git','-c','user.name=t','-c','user.email=t@e',"
            "'commit','--allow-empty','-qm','work'], check=True)\n")
    return _fake_claude(tmp_path, f"""
import sys
sys.stdin.read()
{commit_step}
print({json.dumps(json.dumps(rec))})
sys.exit(1)
""")


def _reason(tmp_path, binp, forge_branch=None, verified=None):
    mod = _mod(tmp_path, path_extra=str(binp))
    calls = _wire(mod, verified=verified or
                  (False, "no pull request with head feat/x", None))
    if forge_branch is not None:
        mod._forge = forge_branch
    _lease(mod)
    assert _run(mod) == 1
    return calls["emit"][0][2]["reason"]


class _Forge:
    def __init__(self, result=(404, None), boom=False):
        self.result, self.boom = result, boom

    def get_branch(self, repo, name):
        if self.boom:
            raise RuntimeError("forge down")
        return self.result


def test_a_backend_error_before_any_work_says_so(tmp_path):
    binp = _backend_claude(tmp_path)
    reason = _reason(tmp_path, binp, lambda: (None, _Forge()))
    assert re.fullmatch(r"session ended on a backend error before any work: "
                        r"API 500 after \d+s — API Error: 500 boom", reason), reason


def test_a_backend_error_after_a_local_commit_says_work_began(tmp_path):
    binp = _backend_claude(tmp_path, commit=True)
    reason = _reason(tmp_path, binp, lambda: (None, _Forge()))
    assert reason.startswith("session ended on a backend error after work began: API 500")


def test_a_pushed_forge_branch_counts_as_work(tmp_path):
    binp = _backend_claude(tmp_path)
    forge = _Forge((200, {"commit": {"id": "f" * 40}}))
    reason = _reason(tmp_path, binp, lambda: (None, forge))
    assert "after work began" in reason


def test_a_forge_branch_check_that_raises_counts_as_work(tmp_path):
    binp = _backend_claude(tmp_path)
    reason = _reason(tmp_path, binp, lambda: (None, _Forge(boom=True)))
    assert "after work began" in reason


def test_a_stream_without_a_result_error_keeps_the_old_reason_byte_for_byte(tmp_path):
    binp = _fake_claude(tmp_path, "import sys; sys.stdin.read(); "
                        "print('{\"type\":\"result\",\"is_error\":false}')")
    reason = _reason(tmp_path, binp)
    assert reason == ("no verified marked PR: no pull request with head feat/x "
                      "(session exit 0)")


def test_a_token_shaped_message_is_redacted_and_cut(tmp_path):
    tok = "ghp_" + "a1B2c3D4e5" * 4
    binp = _backend_claude(tmp_path, message=f"API Error: {tok} " + "x" * 400)
    reason = _reason(tmp_path, binp, lambda: (None, _Forge()))
    assert tok not in reason
    assert "[redacted:" in reason
    assert len(reason.split(" — ", 1)[1]) <= 120


def test_read_backend_error_uses_only_the_last_result_record(tmp_path):
    mod = _mod(tmp_path)
    err = json.dumps({"type": "result", "is_error": True,
                      "api_error_status": 500, "result": "x"})
    ok = json.dumps({"type": "result", "is_error": False, "result": "fine"})
    assert mod.read_backend_error(err + "\n" + ok + "\n") is None
    assert mod.read_backend_error("not json\n" + err) == {"status": 500,
                                                           "message": "x"}
    assert mod.read_backend_error("") is None


def test_verification_runs_even_when_the_session_exits_non_zero(tmp_path):
    """Both halves of the same rule: the wrapper trusts the forge, not the
    report. A crashed session that nonetheless landed a verified marked PR has
    produced the artefact the next phase acts on."""
    binp = _fake_claude(tmp_path, "import sys; sys.stdin.read(); sys.exit(3)")
    mod = _mod(tmp_path, path_extra=str(binp))
    calls = _wire(mod, verified=(True, "", 11))
    _lease(mod)
    assert _run(mod) == 0
    assert calls["verify"], "verification was skipped on a non-zero exit"
    # 0031 wired the steward, so a crashed session that nonetheless landed a
    # verified marked PR now runs all the way through to stewarding it — which
    # is what "the wrapper trusts the forge, not the report" was always for.
    assert calls["steward"] == [("operator/demo", 11, mod.lease_branch(LEASE))]
    assert calls["emit"] == [], "a completed run emits no plan-failed"


def test_an_unmarked_pr_is_reported_as_such(tmp_path):
    binp = _fake_claude(tmp_path, "import sys; sys.stdin.read()")
    mod = _mod(tmp_path, path_extra=str(binp))
    calls = _wire(mod, verified=(False, "#9 carries no 'Plan: 0042-a-feature' line", 9))
    _lease(mod)
    assert _run(mod) == 1
    assert "carries no" in calls["emit"][0][2]["reason"]


def test_the_prompt_reaches_the_session_on_stdin_verbatim(tmp_path):
    """Never in argv — argv is world-readable in `ps` and this prompt is the
    whole plan — and never the redacted copy, which would be a silently
    different prompt."""
    dest = tmp_path / "prompt.txt"
    binp = _fake_claude(tmp_path, f"""
import sys
open({str(dest)!r}, "w").write(sys.stdin.read())
""")
    mod = _mod(tmp_path, path_extra=str(binp))
    _wire(mod)
    _lease(mod)
    assert _run(mod) == 0   # reaches the steward; 0031 wired it
    got = dest.read_text()
    assert PLAN in got
    assert got.startswith((BIN / "implementer-preamble.md").read_text())


# ------------------------------------------------------------------ timeouts
GRANDCHILD = """
import os, subprocess, sys, time
sys.stdin.read()
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
open({dest!r}, "w").write(str(p.pid))
print("spawned", p.pid)
sys.stdout.flush()
time.sleep(300)
"""


def _proc_state(pid):
    """The process's scheduler state, or "" when the pid is gone.

    `os.kill(pid, 0)` is NOT this, and the difference failed a CI lane: a
    reaped-but-unwaited process is a ZOMBIE, and a signal probe reports a zombie
    as alive forever. On macOS that rarely shows, because launchd reaps
    reparented children promptly; inside the cihost-linux container the job's
    shell is PID 1 and reaps nothing, so every killed grandchild lingers.
    Demonstrated rather than assumed: fork a child, let it exit, never wait —
    `os.kill(pid, 0)` succeeds while `ps -o state=` prints `Z`.

    A zombie is dead and can open no pull request, which is the property this
    test is actually about. The instrument has to be able to say so."""
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():                       # Linux, including the container
        try:
            return proc_stat.read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return ""
    r = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _alive(pid):
    state = _proc_state(pid)
    return bool(state) and not state.startswith("Z")


def test_a_timeout_kills_the_process_group_not_just_the_child(tmp_path):
    """`subprocess.run(timeout=)` kills the direct child; `claude -p` has
    grandchildren. A grandchild that survives can open the marked PR after the
    wrapper declared failure — and the plan then dedupes as done on a PR nothing
    verified, forever, with an orphaned lease."""
    dest = tmp_path / "grandchild.pid"
    binp = _fake_claude(tmp_path, GRANDCHILD.format(dest=str(dest)))
    mod = _mod(tmp_path, path_extra=str(binp))
    mod.implementer_timeout_minutes = lambda env=None: 3.0 / 60
    cred = mod.resolve_credential()
    route = mod.route_implementer({"zone": "public"}, cred)
    wt = tmp_path / "wt2"
    wt.mkdir()
    s = mod.run_implementer(wt, "go\n", cred, route, LEASE)
    assert s.timed_out is True
    pid = int(dest.read_text())
    deadline = time.time() + 10
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not _alive(pid), (f"grandchild {pid} outlived the timeout "
                             f"(state {_proc_state(pid)!r})")
    assert s.transcript.exists(), "a timed-out run is the one worth reading"


STUBBORN = """
import os, signal, subprocess, sys, time
sys.stdin.read()
p = subprocess.Popen([sys.executable, "-c",
                      "import signal, time; signal.signal(signal.SIGTERM, "
                      "signal.SIG_IGN); time.sleep(300)"])
open({dest!r}, "w").write(str(p.pid))
print("spawned", p.pid)
sys.stdout.flush()
time.sleep(300)
"""


def test_a_grandchild_that_ignores_sigterm_is_still_killed(tmp_path):
    """The direct child exiting says nothing about the rest of the group. An
    earlier _kill_group returned as soon as the child was reaped, leaving a
    SIGTERM-ignoring grandchild — the one process that can still open the marked
    PR after the wrapper declared failure — running."""
    dest = tmp_path / "stubborn.pid"
    binp = _fake_claude(tmp_path, STUBBORN.format(dest=str(dest)))
    mod = _mod(tmp_path, path_extra=str(binp))
    mod.implementer_timeout_minutes = lambda env=None: 3.0 / 60
    cred = mod.resolve_credential()
    route = mod.route_implementer({"zone": "public"}, cred)
    wt = tmp_path / "wt3"
    wt.mkdir()
    s = mod.run_implementer(wt, "go\n", cred, route, LEASE)
    assert s.timed_out is True
    pid = int(dest.read_text())
    deadline = time.time() + 15
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not _alive(pid), (f"grandchild {pid} ignored SIGTERM and was never "
                             f"SIGKILLed (state {_proc_state(pid)!r})")


def test_a_marked_pr_appearing_after_the_kill_is_not_a_late_success(tmp_path):
    dest = tmp_path / "grandchild2.pid"
    binp = _fake_claude(tmp_path, GRANDCHILD.format(dest=str(dest)))
    mod = _mod(tmp_path, path_extra=str(binp))
    mod.implementer_timeout_minutes = lambda env=None: 3.0 / 60
    calls = _wire(mod, verified=(True, "", 12))     # the PR IS there afterwards
    _lease(mod)
    assert _run(mod) == 1
    reason = calls["emit"][0][2]["reason"]
    assert "timed out" in reason
    assert "NOT accepted as a late success" in reason
    assert "#12" in reason


# The implementer timeout is no longer an env knob at all — it is a per-repo
# conf read once per run (plan 0042), covered in tests/test_impl_bounds.py.


# ------------------------------------------------------------- the verification
class _FakeForge:
    """Answers the verbs verify_marked_pr uses, and nothing else."""

    def __init__(self, pulls, contents=None, fail_at=None):
        self.pulls, self.contents, self.fail_at = pulls, contents or {}, fail_at
        self.edits = []

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        if self.fail_at == "pulls":
            return 401, None
        return (200, self.pulls if page == 1 else [])

    def get_pull(self, repo, n):
        for pr in self.pulls:
            if pr.get("number") == n:
                return 200, pr
        return 404, None

    def edit_pull_body(self, repo, n, body):
        """Mutates the stored pull, so a read-back sees the stamp — the thing
        stamp_marker actually checks."""
        self.edits.append((n, body))
        if self.fail_at == "edit":
            return 403, None
        if self.fail_at == "edit-silent":
            return 200, None          # reports success, changes nothing
        for pr in self.pulls:
            if pr.get("number") == n:
                pr["body"] = body
        return 200, {}

    def get_contents(self, repo, path, ref=None):
        import base64
        body = self.contents.get(ref)
        if body is None:
            return 404, None
        return 200, {"content": base64.b64encode(body.encode()).decode()}


def _pull(n=5, login="implbot", base="main", branch="feat/0042-a-feature",
          body="Plan: 0042-a-feature", sha="abc1234"):
    return {"number": n, "body": body, "user": {"id": 9, "login": login},
            "base": {"ref": base}, "head": {"ref": branch, "sha": sha}}


def _verify(mod, forge, plan_path="plans/0042-a-feature.md"):
    fmod = mod._load_sibling("fleetforge.py", "_fleetforge")
    # `fail_at="pulls"` drives the retry, whose real backoff is ten minutes.
    # Neutralised here rather than shortened in the module, so the production
    # policy is the one under test everywhere else.
    mod._sleep = lambda secs: None
    return mod.verify_marked_pr("operator/demo", "feat/0042-a-feature",
                                "0042-a-feature", plan_path, forge=forge, mod=fmod)


DONE = PLAN.replace("status: ready", "status: done")


def test_all_five_checks_pass_on_a_real_marked_pr(tmp_path):
    mod = _mod(tmp_path)
    ok, why, num = _verify(mod, _FakeForge([_pull()], {"abc1234": DONE}))
    assert (ok, num) == (True, 5), why


def test_a_missing_pr_a_wrong_base_and_a_wrong_author_each_fail(tmp_path):
    mod = _mod(tmp_path)
    for forge, expect in (
            (_FakeForge([], {}), "no pull request with head"),
            (_FakeForge([_pull(branch="feat/other")], {}), "no pull request with head"),
            (_FakeForge([_pull(login="operator")], {"abc1234": DONE}), "not an implementer"),
            (_FakeForge([_pull(base="develop")], {"abc1234": DONE}), "not main"),
            # an unmarked PR is no longer a failure — it is STAMPED (see below);
            # what still fails is a stamp that cannot be made to stick
            (_FakeForge([_pull(body="did some work")], {"abc1234": DONE},
                        fail_at="edit"), "could not be added"),
            (_FakeForge([_pull(body="did some work")], {"abc1234": DONE},
                        fail_at="edit-silent"), "could not be added"),
            (_FakeForge([_pull()], {"abc1234": PLAN}), "still says status: ready"),
            (_FakeForge([_pull()], {}), "unreadable at"),
    ):
        ok, why, _ = _verify(mod, forge)
        assert ok is False and expect in why, why


def test_an_unreadable_pr_listing_is_never_read_as_no_pr(tmp_path):
    """A truncated or failed read that reads as absence lets a run fail while its
    work exists — and (once 0011 lands) comment on it as if nothing happened."""
    mod = _mod(tmp_path)
    ok, why, _ = _verify(mod, _FakeForge([], fail_at="pulls"))
    assert ok is False
    assert "could not read" in why and "no pull request" not in why


def test_the_implementer_set_is_a_union_including_implbot(tmp_path):
    mod = _mod(tmp_path, FLEET_IMPLEMENTER_UIDS="4242")
    uids, logins = mod.implementer_accounts()
    assert uids == {"4242"} and "implbot" in logins
    ok, _, _ = _verify(mod, _FakeForge([_pull(login="implbot")], {"abc1234": DONE}))
    assert ok, "adding a uid must not make login-authored PRs invisible"


def test_verification_never_uses_the_configurable_token_command(tmp_path):
    """FLEET_TOKEN_CMD names the admin helper on this fleet, and a read-only
    verification has no business holding a token that can rewrite branch
    protection."""
    src = (BIN / "orchestrator").read_text()
    assert "FLEET_TOKEN_CMD" not in src.replace("# ", "#")[src.index("def _implbot_token"):]
    assert "fetch-forgejo-token.sh" in src
    assert "admin-token" not in src.split("_ENV_ALLOW")[-1]


# ------------------------------------------------------------------ structure
def test_ready_now_passes_because_nothing_is_unwired(tmp_path):
    """0032 emptied the list, which is what lets fleet-watch arm. The guard
    against emptying it early is now the coupling, asserted in both directions
    in test_orchestrator.py: readiness is true exactly when nothing is
    unbuilt."""
    mod = _mod(tmp_path)
    assert mod.UNBUILT_PHASES == ()
    assert mod.main(["--ready"]) == 0
    assert mod.readiness() == (True, "")


def test_the_file_does_not_understate_what_is_wired(tmp_path):
    """This guard has tracked the boundary the whole way. It began as 'the file
    must not still claim part 1', and its job now is the opposite direction: a
    complete wrapper must not describe itself as partial, because the docstring
    is the operator's first read and an understated one sends them looking for a
    phase to build that is already there."""
    src = (BIN / "orchestrator").read_text()
    assert "part 1" not in src
    assert "Part 2 is complete." in src
    for phase in ("the review loop", "the comment/ack protocol",
                  "the post-approval freeze"):
        assert phase in src, phase
    assert "are NOT wired yet" not in src


def test_no_approval_api_call_exists_anywhere_in_this_lane():
    """The standing assertion, extended to the files this plan adds: the
    orchestrator is graded on approval, so the absence of the code path is the
    control, not the credential separation.

    The needles are assembled rather than written out, because this file is one
    of the files being scanned. The two test files are checked for a CALL SITE
    (a review endpoint next to a write verb) rather than for the word: they
    both legitimately contain the scanner's own pattern, and a test that cannot
    describe what it forbids is a test nobody can extend."""
    endpoint = re.compile(r"/re" + r"views\b")
    verdict = re.compile(r"(?i)['\"]APPRO" + r"VED?['\"]")
    write = re.compile(r"(?i)\b(post|put|patch|submit)\b")
    event = re.compile(r"(?i)[\"']event[\"']\s*:\s*[\"']APPRO" + r"VE")

    # The preamble has no business naming a verdict at all: it is text handed to
    # the implementer, which never reads one.
    body = (BIN / "implementer-preamble.md").read_text()
    assert not endpoint.search(body) and not verdict.search(body)

    # The orchestrator is checked for a CALL SITE, not for the word — the same
    # rule this test already applied to the two test files, and for the same
    # stated reason: a test that cannot describe what it forbids is a test
    # nobody can extend. Since 0011 the wrapper legitimately READS a verdict
    # (`state == "APPROVED"`, bound to a head SHA) to decide whether the loop is
    # over. Reading a verdict the reviewer posted is the opposite of creating
    # one, and a word-ban cannot tell them apart — it would have forced the loop
    # to spell the string it compares against some other way, which is gaming a
    # guard rather than satisfying it.
    for path in [BIN / "orchestrator", Path(__file__),
                 Path(__file__).parent / "test_orchestrator.py"]:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if endpoint.search(line) and write.search(line):
                raise AssertionError(f"{path}:{n} looks like an approval call")
            if event.search(line):
                raise AssertionError(f"{path}:{n} builds an approval event")


# --------------------------------------------- the retry that salvages a run
class _FlakyForge(_FakeForge):
    """Fails the pull listing `fail_times` times, then answers normally."""

    def __init__(self, pulls, contents=None, fail_times=0):
        super().__init__(pulls, contents)
        self.fail_times, self.calls = fail_times, 0

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        if page == 1:
            self.calls += 1
            if self.calls <= self.fail_times:
                return 401, None
        return (200, self.pulls if page == 1 else [])


def _read(mod, forge, slept):
    fmod = mod._load_sibling("fleetforge.py", "_fleetforge")
    return mod.read_pulls(fmod, forge, "operator/demo", sleep=slept.append)


def test_a_transient_read_is_retried_rather_than_discarding_the_run(tmp_path):
    """2026-09-09: the first real dispatch produced a complete, correct PR and
    this listing failed ONCE, 39 seconds later. The wrapper reported "could not
    read pull requests" and threw away 34 minutes of model time over one blip;
    the same read worked on every attempt minutes afterwards."""
    mod = _mod(tmp_path)
    slept = []
    pulls, outcome = _read(mod, _FlakyForge([_pull()], fail_times=1), slept)
    assert outcome is None and len(pulls) == 1
    assert slept == [40], "did not back off before retrying"


def test_it_recovers_on_the_last_attempt(tmp_path):
    mod = _mod(tmp_path)
    slept = []
    pulls, outcome = _read(mod, _FlakyForge([_pull()], fail_times=4), slept)
    assert outcome is None and len(pulls) == 1
    assert slept == [40, 80, 160, 320]


def test_it_gives_up_after_five_attempts_and_says_so(tmp_path):
    mod = _mod(tmp_path)
    slept = []
    forge = _FlakyForge([_pull()], fail_times=99)
    pulls, outcome = _read(mod, forge, slept)
    assert pulls is None and outcome
    assert forge.calls == mod.READ_ATTEMPTS == 5
    assert slept == [40, 80, 160, 320], "must not sleep after the last attempt"


def test_the_backoff_is_exponential_and_spans_ten_minutes(tmp_path):
    """Long enough to outlive a forge restart or a reboot of the box serving
    it, which is the case the owner asked this to survive."""
    mod = _mod(tmp_path)
    assert sum(mod.READ_BACKOFF) == 600
    assert len(mod.READ_BACKOFF) == mod.READ_ATTEMPTS - 1
    for a, b in zip(mod.READ_BACKOFF, mod.READ_BACKOFF[1:]):
        assert b == a * 2, mod.READ_BACKOFF


def test_a_clean_read_never_sleeps(tmp_path):
    mod = _mod(tmp_path)
    slept = []
    pulls, outcome = _read(mod, _FlakyForge([_pull()], fail_times=0), slept)
    assert outcome is None and len(pulls) == 1 and slept == []


def test_verification_survives_a_flaky_listing_end_to_end(tmp_path):
    """The whole point: five checks still pass when the first read fails."""
    mod = _mod(tmp_path)
    mod._sleep = lambda secs: None
    fmod = mod._load_sibling("fleetforge.py", "_fleetforge")
    forge = _FlakyForge([_pull()], {"abc1234": DONE}, fail_times=2)
    ok, why, num = mod.verify_marked_pr("operator/demo", "feat/0042-a-feature",
                                        "0042-a-feature",
                                        "plans/0042-a-feature.md",
                                        forge=forge, mod=fmod)
    assert (ok, num) == (True, 5), why


# ------------------------------------------- the timeout that was under cost
def test_the_forge_timeout_clears_the_measured_cost(tmp_path):
    """20s was below what this fleet's own listing costs.

    Measured 2026-09-09: `pulls?state=all&limit=50` on operator/eunomia took
    22.6s for ONE page (247 PRs, ~0.45s of Forgejo work each), confirmed by
    curl and urllib agreeing at 23.7s and 22.7s. The read therefore failed
    deterministically rather than transiently, which is why #246's retry could
    not rescue it — all five attempts hit the same wall."""
    mod = _mod(tmp_path)
    assert mod.forge_timeout({}) == 60
    assert mod.forge_timeout({}) > 23, "must clear the measured 22.6s cost"


def test_the_forge_timeout_is_configurable_and_refuses_nonsense(tmp_path):
    mod = _mod(tmp_path)
    assert mod.forge_timeout({"FLEET_FORGE_TIMEOUT": "90"}) == 90
    for bad in ("soon", "0", "-5"):
        with pytest.raises(mod.Stop):
            mod.forge_timeout({"FLEET_FORGE_TIMEOUT": bad})


def test_the_forge_is_built_with_that_timeout_not_the_library_default(tmp_path):
    """The bug was that `_forge()` took whatever the library defaulted to. A
    test that only checks `forge_timeout()` would still pass with the call site
    unchanged, which is exactly how this survived."""
    mod = _mod(tmp_path)
    seen = {}

    class _FF:
        FAILED = "failed"

        def Forge(self, base, token, kind="forgejo", timeout=None):
            seen["timeout"] = timeout
            return object()
    mod._load_sibling = lambda *a, **k: _FF()
    mod._implbot_token = lambda: "t" * 40
    mod._forge()
    assert seen["timeout"] == 60
# ------------------------------------------------ adopting a stranded PR
def test_resume_runs_no_implementer_and_goes_straight_to_verification(tmp_path):
    """Both real dispatches on 2026-09-09 died after their implementer had
    already pushed a correct PR: the wrapper owns the lifecycle in one process,
    so #245 and speakhush#161 were stranded with no review loop and no steward,
    and dedupe refused a fresh dispatch because the marker was there.

    `--resume` adopts the branch instead of building it. A `claude` that would
    fail loudly proves no session is spawned."""
    binp = _fake_claude(tmp_path, "import sys; sys.exit(99)")
    mod = _mod(tmp_path, path_extra=str(binp))
    calls = _wire(mod, verified=(True, "", 7))
    branch = _lease(mod)
    _forge_branch(tmp_path, branch)
    attached = []
    mod.attach_branch = lambda wt, b: (attached.append(b), b)[1]
    mod.create_branch = lambda wt, b: pytest.fail("resume must not create a branch")
    mod.run_implementer = lambda *a, **k: pytest.fail("resume must not spawn a session")
    rc = mod.main(["--resume", "operator/demo", "plans/0042-a-feature.md", LEASE])
    assert rc == 0                      # verified, reviewed, stewarded
    assert attached == [branch]
    assert calls["verify"], "verification did not run on a resumed branch"
    assert calls["steward"], "a resumed run must reach the steward"


def test_resume_fetches_the_branch_it_adopts(tmp_path):
    """`origin/main` is the wrong place to stand when the work is on a branch,
    and the local store may predate that branch entirely."""
    mod = _mod(tmp_path)
    _wire(mod)
    branch = _lease(mod)
    seen = {}

    def _add(repo, key, ref, ssh_base=None, refspecs=()):
        seen["ref"], seen["refspecs"] = ref, tuple(refspecs)
        return tmp_path / "wt"
    (tmp_path / "wt").mkdir(exist_ok=True)
    mod.lib.add_worktree = _add
    mod.attach_branch = lambda wt, b: b
    mod.run_implementer = lambda *a, **k: pytest.fail("no session on resume")
    mod.main(["--resume", "operator/demo", "plans/0042-a-feature.md", LEASE])
    assert seen["ref"] == f"origin/{branch}"
    assert seen["refspecs"] == (f"+refs/heads/{branch}:refs/remotes/origin/{branch}",)


def test_a_normal_run_is_unchanged_by_the_resume_flag(tmp_path):
    """The flag is opt-in: without it the implementer still runs."""
    binp = _fake_claude(tmp_path, "import sys; sys.stdin.read(); sys.exit(0)")
    mod = _mod(tmp_path, path_extra=str(binp))
    calls = _wire(mod, verified=(True, "", 7))
    _lease(mod)
    mod.attach_branch = lambda wt, b: pytest.fail("a normal run must not adopt")
    assert _run(mod) == 0
    assert calls["steward"]


def test_resume_survives_having_no_session_to_report(tmp_path):
    """Every `session.` reference in main() has to tolerate None, or resume
    dies in the failure path — the one place it is most needed."""
    mod = _mod(tmp_path)
    calls = _wire(mod, verified=(False, "no marked PR", None))
    branch = _lease(mod)
    _forge_branch(tmp_path, branch)
    mod.attach_branch = lambda wt, b: b
    mod.run_implementer = lambda *a, **k: pytest.fail("no session on resume")
    rc = mod.main(["--resume", "operator/demo", "plans/0042-a-feature.md", LEASE])
    assert rc == 1
    reason = calls["emit"][0][2]["reason"]
    assert "no marked PR" in reason and "resumed" in reason


# --- peak RSS of the implementer group -------------------------------------
# The measurement the operator asked for on 2026-09-09 ("capture orchestrator memory,
# I think that's the big drain"). BSD process accounting was the obvious
# instrument and the wrong one: struct acct carries `u_int16_t ac_mem`, an
# AVERAGE, written only at exit.

def test_peak_rss_measures_the_whole_group_not_just_the_leader(tmp_path):
    """The point of sampling the group is the grandchildren — `claude -p` has
    them, which is why _kill_group exists. A sampler that watched only the
    direct child would report the wrapper and miss the memory."""
    mod = _load(tmp_path / "fleet")
    # A leader plus two children, all in one new process group, each holding a
    # few MB so the sum is distinguishable from any one of them.
    script = (
        "import subprocess,sys,time\n"
        "kids=[subprocess.Popen([sys.executable,'-c',"
        "\"x=bytearray(12*1024*1024); import time; time.sleep(3)\"]) for _ in range(2)]\n"
        "y=bytearray(12*1024*1024)\n"
        "time.sleep(3)\n"
        "[k.wait() for k in kids]\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        p = mod._PeakRSS(proc.pid, interval=0.2)
        p.start()
        time.sleep(1.5)
        p.stop()
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert p.samples > 0, "sampled nothing at all"
    # Three processes each holding 12MB. Asserting only a floor: RSS is not a
    # promise, and the docstring says this number is itself a floor.
    assert p.peak_mb >= 24, f"group peak {p.peak_mb}MB — looks like one process"


def test_unsampled_is_none_not_zero(tmp_path):
    """None means "not measured"; 0 means "used no memory". Charting a floor of
    zeros as real data is the failure this distinction exists to prevent."""
    mod = _load(tmp_path / "fleet")
    p = mod._PeakRSS(999999, interval=0.05)      # a pgid that cannot exist
    assert p.peak_mb is None
    p.stop()
    assert p.samples == 0
    assert p.peak_mb is None, "a dead group must not report 0 MB"


def test_sampler_never_raises_when_ps_fails(tmp_path, monkeypatch):
    """A resource number must not be able to fail a plan that otherwise worked."""
    mod = _load(tmp_path / "fleet")

    def boom(*a, **k):
        raise OSError("no ps on this host")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    p = mod._PeakRSS(1, interval=0.05)
    p._sample()                               # must not propagate
    p.stop()
    assert p.peak_mb is None


def test_sampler_ignores_unparseable_ps_rows(tmp_path, monkeypatch):
    mod = _load(tmp_path / "fleet")

    class R:
        stdout = "RSS\n1024\ngarbage\n2048\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    p = mod._PeakRSS(1, interval=0.05)
    p._sample()
    assert p.peak_kb == 3072, "should sum the integers and skip the rest"


def test_self_peak_normalises_darwin_bytes_to_kb(tmp_path, monkeypatch):
    """ru_maxrss is BYTES on Darwin and KILOBYTES on Linux. This fleet runs
    both — the orchestrator on opshost, the suite on the cihost-linux CI label — so
    getting it wrong is a silent 1024x in whichever direction you are not
    looking."""
    mod = _load(tmp_path / "fleet")

    class RU:
        ru_maxrss = 512 * 1024 * 1024          # 512 MB expressed in BYTES

    monkeypatch.setattr(mod.resource, "getrusage", lambda who: RU())
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    assert mod._self_peak_kb() == 512 * 1024

    # The same raw number on Linux means 512 GB, and must NOT be divided.
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert mod._self_peak_kb() == 512 * 1024 * 1024


# --- follow-ups from review of #223 (issue #228), re-cut ---------------------
# #241 carried these and went 43 commits stale. Each is re-verified against the
# source here rather than trusted from that branch's description.

def test_the_lease_id_is_validated_where_it_becomes_a_path(tmp_path):
    """L3: RunLog opens FIRST and did not validate, while write_transcript did.
    `../../x` was appended under the work root before anything rejected it."""
    mod = _mod(tmp_path)
    assert mod.lease_key("branch--feat-x--001") == "branch--feat-x--001"
    for bad in ("../../etc/x", "a b", "", None, "x/../y"):
        with pytest.raises(mod.Stop):
            mod.lease_key(bad)
    with pytest.raises(mod.Stop):
        mod.RunLog("../../escape")


def test_evidence_paths_are_resolved_before_the_session(tmp_path, monkeypatch):
    """M3: transcript_dir() was first called AFTER the session, so a bad
    transcript root failed a 90-minute run at the very end — and skipped
    verify_marked_pr, leaving a plan deduped as done on a PR nothing verified."""
    mod = _mod(tmp_path)
    called = []
    monkeypatch.setattr(mod, "transcript_dir", lambda: called.append(1) or tmp_path)
    assert mod.check_evidence_paths("branch--feat-x--001") == tmp_path
    assert called, "the transcript root was not resolved"
    with pytest.raises(mod.Stop):
        mod.check_evidence_paths("../../escape")


def test_the_group_is_reaped_on_a_normal_exit_too(tmp_path, monkeypatch):
    """L1: it was signalled only on timeout. A session that detaches a child and
    exits 0 leaves that child able to push AFTER the verdict is read."""
    mod = _mod(tmp_path)
    src = (BIN / "orchestrator").read_text()
    i = src.index("rc = proc.wait(timeout=mins * 60)")
    window = src[i:i + 1400]
    assert window.count("_kill_group(proc)") == 2, \
        "the group is still reaped on only one of the two exit paths"
    assert "else:" in window, "the normal-exit path does not reap"


def test_kill_group_refuses_to_signal_its_own_group(tmp_path, monkeypatch):
    """The guard L1 needs: the child is spawned with start_new_session=True, but
    if that had not taken effect its group is OURS — and this now runs on every
    exit, so an unguarded SIGKILL would take down the orchestrator every run."""
    mod = _mod(tmp_path)
    warned, killed = [], []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    monkeypatch.setattr(mod.os, "getpgid", lambda pid: os.getpgrp())
    monkeypatch.setattr(mod.os, "killpg", lambda *a: killed.append(a))

    class P:
        pid = 4242
    mod._kill_group(P())
    assert killed == [], "signalled its own process group"
    assert any("start_new_session did not take effect" in w for w in warned), warned


def test_the_worktree_commits_as_implbot_not_the_operator(tmp_path, monkeypatch):
    """From the live run: sessions committed as `Fleet Operator
    <operator@opshost.example>` — three such commits are on main — because
    verify_marked_pr reads the PULL REQUEST's author and never the commit's."""
    mod = _mod(tmp_path)
    calls = []

    class R:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or R())
    assert mod.set_commit_identity(tmp_path) is True
    got = {c[3]: c[4] for c in calls}
    assert got == {"user.name": "implbot", "user.email": "implbot@example.org"}
    assert all("--local" in c for c in calls), "wrote the operator's global config"


def test_a_malformed_identity_is_refused_not_guessed(tmp_path, monkeypatch):
    mod = _mod(tmp_path, FLEET_COMMIT_IDENTITY="just-a-name")
    warned = []
    monkeypatch.setattr(mod, "warn", lambda m: warned.append(m))
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran git with a bad identity"))
    assert mod.set_commit_identity(tmp_path) is False
    assert any("Name <email>" in w for w in warned)


def test_an_unsettable_identity_does_not_lose_the_plan(tmp_path, monkeypatch):
    """A run that cannot set an identity still produces work a human reviews;
    refusing would trade a wrong Author: line for no plan at all."""
    mod = _mod(tmp_path)

    class R:
        returncode = 1
        stderr = "not a git repository"

    monkeypatch.setattr(mod, "warn", lambda m: None)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    assert mod.set_commit_identity(tmp_path) is False


# ------------------------------------------------ the page, and its durable half
def _pages(mod):
    p = mod.pages_path()
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_an_undelivered_page_is_still_recorded(tmp_path):
    """The whole point. The watcher spawns with stderr=DEVNULL, so an
    undelivered page vanished without trace — which is how this fleet ran for
    weeks with FLEET_NTFY_URL empty on every unit and nobody noticing."""
    mod = _mod(tmp_path)
    assert mod.notify("Orchestrator stopped", "something broke") is False
    rows = _pages(mod)
    assert len(rows) == 1
    assert rows[0]["delivered"] is False
    assert rows[0]["title"] == "Orchestrator stopped"
    assert rows[0]["detail"] == "no channel configured"


def test_a_200_that_delivered_to_nobody_is_not_a_page(tmp_path, monkeypatch):
    """angelia reports sent/pruned/failed counts. A send authenticates fine and
    succeeds with `sent: 0` when the device registry is empty for this app and
    user — reading the status code alone is how a channel comes to be believed
    while silent."""
    mod = _mod(tmp_path, FLEET_ANGELIA_URL="http://x", FLEET_ANGELIA_APP="fleet",
               FLEET_ANGELIA_TOKEN_CMD="/bin/echo tok")

    class R:
        status = 200
        def read(self): return b'{"sent": 0, "pruned": 1, "failed": 0}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    # monkeypatch, NOT `mod.urllib.request.urlopen = ...`. `mod.urllib` is the
    # process-wide module object, so a bare assignment here leaked into every
    # later test: it made all 19 `_stub_server` tests in test_fleetforge.py
    # receive this angelia body instead of the forge's, and surfaced only in the
    # lane whose random seed happened to order this file first.
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: R())
    ok, detail = mod.send_angelia("t", "b")
    assert ok is False and "sent=0" in detail
    assert mod.notify("t", "b") is False
    assert _pages(mod)[0]["delivered"] is False


def test_a_real_delivery_is_recorded_as_delivered(tmp_path, monkeypatch):
    mod = _mod(tmp_path, FLEET_ANGELIA_URL="http://x", FLEET_ANGELIA_APP="fleet",
               FLEET_ANGELIA_TOKEN_CMD="/bin/echo tok")

    class R:
        status = 200
        def read(self): return b'{"sent": 1, "pruned": 0, "failed": 0}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: R())
    assert mod.notify("t", "b") is True
    row = _pages(mod)[0]
    assert row["delivered"] is True and "sent=1" in row["detail"]


def test_no_registered_device_says_so(tmp_path, monkeypatch):
    """404 means authenticated fine, nobody listening — a materially different
    problem from a bad token, and worth naming in the log."""
    mod = _mod(tmp_path, FLEET_ANGELIA_URL="http://x", FLEET_ANGELIA_APP="fleet",
               FLEET_ANGELIA_TOKEN_CMD="/bin/echo tok")

    def boom(*a, **k):
        raise mod.urllib.error.HTTPError("u", 404, "nf", None, None)
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    ok, detail = mod.send_angelia("t", "b")
    assert ok is False and "no device registered" in detail


def test_the_token_comes_from_a_command_not_a_value(tmp_path):
    """A plist value is world-readable; a command is not. Absent means the
    channel is unconfigured, which is a state to report rather than an error."""
    mod = _mod(tmp_path)
    assert mod.angelia_token() is None
    mod2 = _mod(tmp_path / "b", FLEET_ANGELIA_TOKEN_CMD="/bin/echo s3cret")
    assert mod2.angelia_token() == "s3cret"
    mod3 = _mod(tmp_path / "c", FLEET_ANGELIA_TOKEN_CMD="/usr/bin/false")
    assert mod3.angelia_token() is None


def test_a_credential_shape_never_reaches_the_page_log(tmp_path):
    """The log is a file a human reads and lynceus serves."""
    mod = _mod(tmp_path)
    pat = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    mod.notify("leak", f"token {pat} in a failure")
    body = mod.pages_path().read_text()
    assert pat not in body and "github-pat" in body


def test_the_page_log_survives_an_unwritable_tree(tmp_path):
    """A logging failure must not take down the thing it is logging."""
    mod = _mod(tmp_path)
    mod.pages_path = lambda: Path("/nonexistent-root/pages.jsonl")
    assert mod.notify("t", "b") is False       # returned, did not raise


def test_the_implementer_can_reach_the_ssh_agent(tmp_path):
    """Without this the implementer cannot push, and the fleet's output is
    orphaned commits rather than pull requests.

    Measured 2026-09-10: pr-proxy 0002-ci-logs ran 700s, closed both gaps in its
    plan, committed b4cfac4, and then `git push` returned `Permission denied
    (publickey)` while `ssh-add -l` reported no agent at all. A human had to
    rescue the commit out of the shared store.

    DO NOT "harden" this by removing it. Withholding the variable is not a
    control: a child holding only this allowlist recovers the agent in one line,
    because `/var/run/com.apple.launchd.*/Listeners` is enumerable by the owner.
    It stops the honest session and not the determined one — which is the
    inversion SPEC principle 5 exists to reject. The narrowing that WOULD work is
    a push identity scoped to one repo and one branch, and that is a change, not
    a deletion."""
    mod = _mod(tmp_path)
    assert "SSH_AUTH_SOCK" in mod._ENV_ALLOW, \
        "the implementer cannot push without an agent; see the comment above _ENV_ALLOW"


def test_the_allowlist_still_withholds_the_token_recipe(tmp_path):
    """Adding one variable must not become a habit of adding variables. The
    FLEET_* recipes stay out — that paragraph's reasoning is unchanged."""
    mod = _mod(tmp_path)
    for never in ("FLEET_TOKEN_CMD", "FLEET_ADMIN_TOKEN_CMD", "EUNOMIA_SESSION",
                  "FLEET_PUSH_OVERRIDE", "ANTHROPIC_API_KEY"):
        assert never not in mod._ENV_ALLOW, f"{never} must not be in the allowlist"


def test_the_agent_actually_reaches_the_child(tmp_path):
    """The allowlist is a filter over the PARENT's environment, so the entry is
    only worth anything if the value survives implementer_env()."""
    mod = _mod(tmp_path)
    runner = mod.RUNNERS["sub-sonnet"]
    env = mod.implementer_env(runner, {"SSH_AUTH_SOCK": "/var/run/x/Listeners",
                                       "HOME": "/Users/x", "PATH": "/usr/bin",
                                       "FLEET_TOKEN_CMD": "/bin/leak"})
    assert env["SSH_AUTH_SOCK"] == "/var/run/x/Listeners"
    assert "FLEET_TOKEN_CMD" not in env


def test_an_unmarked_pr_of_ours_is_stamped_not_abandoned(tmp_path):
    """speakhush#162 — the fleet's first fully autonomous pull request — carried
    301 lines of finished work and no `Plan:` line, so dedupe could not see it and
    the plan stayed re-dispatchable. The preamble asks for the marker twice; the
    model did not produce it. pr-proxy #42 and #79 produced it as a PATH instead
    of an id, with the same result. Two failures, two shapes, one hope.

    The orchestrator knows the id, so it writes it — the move commit-identity
    already made, for the reason that commit stated: a guarantee written as
    prompt text is a hope."""
    mod = _mod(tmp_path)
    forge = _FakeForge([_pull(body="## Summary\n\nDid the work.")],
                       {"abc1234": DONE})
    ok, why, num = _verify(mod, forge)
    assert ok is True, why
    assert num == 5
    assert forge.edits, "nothing was stamped"
    _, written = forge.edits[0]
    assert written.rstrip().endswith("Plan: 0042-a-feature"), written
    assert "Did the work." in written, "the implementer's report was destroyed"


def test_a_stamp_is_only_written_to_a_pr_already_proven_ours(tmp_path):
    """The licence to write is the three checks above it — implementer author,
    our branch, base main. A PR failing any of them must be left alone."""
    mod = _mod(tmp_path)
    for forge in (_FakeForge([_pull(login="operator", body="x")], {"abc1234": DONE}),
                  _FakeForge([_pull(base="develop", body="x")], {"abc1234": DONE}),
                  _FakeForge([_pull(branch="feat/other", body="x")], {"abc1234": DONE})):
        ok, _why, _n = _verify(mod, forge)
        assert ok is False
        assert forge.edits == [], "wrote to a pull request it had not proven was its own"


# --- the redacted transcript must be the stream that carries tool output -----
# Measured 2026-09-10, controlled A/B, same prompt and same tool call against a
# file whose contents appear only in tool output:
#     text format          5 bytes    tool output captured: NO
#     stream-json     15,556 bytes    tool output captured: YES

def test_the_runner_asks_for_the_stream_that_carries_tool_results(tmp_path):
    """Under `-p`, text format prints the final assistant message and nothing
    else, so a credential that lands in a TOOL RESULT never reaches this
    wrapper's pipe and redact_transcript runs over a stream that cannot contain
    it. That is what the 2026-08-28 PAT did."""
    mod = _mod(tmp_path)
    for name in ("sub-sonnet", "sub-opus", "api-sonnet"):
        argv = list(mod.RUNNERS[name].argv)
        assert "--output-format" in argv, f"{name} does not ask for a format"
        assert argv[argv.index("--output-format") + 1] == "stream-json", name
        assert "--verbose" in argv, (
            f"{name}: the CLI REFUSES --output-format=stream-json under --print "
            "without --verbose, so the spawn would fail at startup")


def test_redaction_still_finds_a_secret_inside_a_json_record(tmp_path):
    """The stream is now JSON, and the redactor is regex over the buffered
    whole — so this pins that the change of shape did not quietly change what
    gets caught."""
    mod = _mod(tmp_path)
    # The shape this fleet's implementer actually holds: a Forgejo PAT, 40 hex
    # characters, no prefix and no structure. _transcript_patterns' docstring is
    # explicit that this is why the PARANOID set is used here and not FINDINGS —
    # a prefixed vendor token would test a shape the redactor never claimed.
    tok = "a1b2c3d4" * 5
    record = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": f"cat creds\n{tok}\n"}]}})
    out, shapes = mod.redact_transcript(record)
    assert tok not in out, "a token inside a tool_result survived redaction"
    assert shapes, "the shape was not reported"


def test_the_transcript_is_still_redacted_as_a_whole_not_per_line(tmp_path):
    """stream-json arrives as many lines, which makes per-line redaction look
    natural and would reintroduce the split-token hole redact_transcript's
    docstring exists for: a token split across two reads matches no per-chunk
    regex, and the writer that rejoined them would have written it out."""
    mod = _mod(tmp_path)
    tok = "9f8e7d6c" * 5
    joined = mod.redact_transcript(tok[:12] + tok[12:])[0]
    assert tok not in joined
    halves = mod.redact_transcript(tok[:12])[0] + mod.redact_transcript(tok[12:])[0]
    assert tok in halves, (
        "redacting the halves separately let the token through — which is why "
        "the reader thread buffers and redacts once, at the end")


def test_long_hex_is_bounded_on_the_hex_class_not_on_word_edges(tmp_path):
    """`\\b` asks "is the neighbour a word character", so a NON-HEX word
    character abutting the run hides it. That is not an edge case once anything
    JSON-encoded is scanned: a newline becomes backslash-n, and the character
    before the token is then `n`.

    Found 2026-09-10 while moving the transcript to stream-json — the same 40-hex
    Forgejo PAT was redacted as plain text and NOT redacted inside a tool_result.
    Without this the format change would have put redaction onto a stream the
    patterns match LESS well: a control that reads as strengthened while doing
    less."""
    mod = _mod(tmp_path)
    tok = "a1b2c3d4" * 5
    for label, sample in (
            ("after a space", f"cat {tok}"),
            ("after an escaped newline", f"cat\\n{tok}"),
            ("after a quote", f'"{tok}"'),
            ("after a non-hex word char", f"sha{tok}"),
    ):
        out, shapes = mod.redact_transcript(sample)
        assert tok not in out, f"{label}: the token survived redaction"
        assert "long-hex" in shapes, label


# --- a respawn can create its branch (review 2369 M2) ------------------------
# The ref lives in the shared store and survives remove_worktree, so a second run
# of the same plan hit `fatal: a branch named 'feat/x' already exists`, exit 128 —
# and every retry after it failed the same way, forever. Measured again on
# 2026-09-10: speakhush 0001 failed once on PATH and the leftover ref then
# refused every subsequent dispatch.

def _repo_with_branch(tmp_path, name, on_origin=False, unpushed=False):
    """A worktree-shaped git repo that already carries `name`.

    `unpushed` puts a commit ON that branch and nowhere else — the shape a run
    leaves when it commits and cannot push (review 2530 HIGH)."""
    import subprocess as sp
    # NOT tmp_path/"src": _mod() already creates that, and the collision is a
    # FileExistsError that reads like a test bug rather than a fixture clash.
    d = (tmp_path /
         f"branchcase-{name.replace('/', '-')}-{int(on_origin)}-{int(unpushed)}")
    d.mkdir(parents=True)
    g = lambda *a: sp.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *a],
                          cwd=str(d), capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    (d / "f").write_text("x")
    g("add", "."); g("commit", "-qm", "one")
    g("branch", name)
    if unpushed:
        g("checkout", "-q", name)
        (d / "unpushed").write_text("work that never reached the forge")
        g("add", "."); g("commit", "-qm", "committed, never pushed")
        g("checkout", "-q", "main")
    if on_origin:
        g("update-ref", f"refs/remotes/origin/{name}", "HEAD")
    g("checkout", "-q", "--detach", "HEAD")
    return d


def test_a_dead_local_ref_is_cleared_not_fatal(tmp_path):
    """The common case: a run died, its worktree went, its branch did not."""
    mod = _mod(tmp_path)
    d = _repo_with_branch(tmp_path, "feat/x")
    assert mod.create_branch(d, "feat/x") == "feat/x"
    import subprocess as sp
    head = sp.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=str(d),
                  capture_output=True, text=True).stdout.strip()
    assert head == "feat/x", "did not end up on the branch"


def test_a_branch_on_origin_is_refused_not_reset(tmp_path):
    """`-B` would reset over commits that may BE the marked pull request which
    retires this plan. Refusing is the whole point of not using -B."""
    mod = _mod(tmp_path)
    d = _repo_with_branch(tmp_path, "feat/x", on_origin=True)
    with pytest.raises(mod.Stop) as e:
        mod.create_branch(d, "feat/x")
    assert "ON ORIGIN" in str(e.value)
    assert "resume" in str(e.value), "the message must say what to do instead"


def test_an_undeletable_branch_is_refused_and_the_evidence_kept(tmp_path, monkeypatch):
    """A surviving worktree holding the branch is how the earlier run died.
    Destroying it to make room would delete the diagnosis."""
    mod = _mod(tmp_path)
    d = _repo_with_branch(tmp_path, "feat/x")
    real = mod.subprocess.run

    def refuse_delete(argv, **kw):
        if argv[:3] == ["git", "branch", "-d"]:
            class R:
                returncode = 1
                stdout = ""
                stderr = "error: cannot delete branch 'feat/x' used by worktree"
            return R()
        return real(argv, **kw)

    monkeypatch.setattr(mod.subprocess, "run", refuse_delete)
    with pytest.raises(mod.Stop) as e:
        mod.create_branch(d, "feat/x")
    assert "would not delete" in str(e.value)
    assert "evidence" in str(e.value)
    # 2530 LOW: a worktree whose DIRECTORY was removed by hand still blocks the
    # delete with this same stderr, and the remedy is a prune — which the first
    # cut's message did not name, so it sent the operator to look for a tree
    # that is not there.
    assert "worktree prune" in str(e.value)


def test_an_unrelated_checkout_failure_is_still_fatal(tmp_path, monkeypatch):
    """Only 'already exists' takes the recovery path. Anything else must fail as
    itself rather than be swallowed by a branch-clearing routine."""
    mod = _mod(tmp_path)
    d = _repo_with_branch(tmp_path, "feat/x")

    class R:
        returncode = 128
        stderr = "fatal: not a git repository"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(mod.Stop) as e:
        mod.create_branch(d, "feat/y")
    assert "not a git repository" in str(e.value)


def test_a_local_branch_with_unpushed_commits_is_refused(tmp_path):
    """Review 2530 HIGH. The first cut used `git branch -D` on the reasoning
    that a local-only branch is unreachable leftover — but a run that commits
    and then dies before it can push leaves exactly this, and pr-proxy
    0002-ci-logs did on 2026-09-10 (b4cfac4 sat in the shared store until a
    human rescued it).

    `-D` would delete the branch AND its reflog, leaving the commit reachable
    from no ref at all and findable only through `git fsck --lost-found` until
    gc — with nothing logged, because this function has no failure channel."""
    import subprocess as sp
    mod = _mod(tmp_path)
    d = _repo_with_branch(tmp_path, "feat/x", unpushed=True)
    sha = sp.run(["git", "rev-parse", "feat/x"], cwd=str(d),
                 capture_output=True, text=True).stdout.strip()

    with pytest.raises(mod.Stop) as e:
        mod.create_branch(d, "feat/x")

    msg = str(e.value)
    assert "never pushed" in msg
    # the refusal has to say why --resume is not the way out: it checks out
    # origin/<branch>, and these commits are not on origin.
    assert "resume" in msg.lower() and "origin" in msg

    # the commit is still there, which is the entire point
    still = sp.run(["git", "cat-file", "-e", sha + "^{commit}"], cwd=str(d))
    assert still.returncode == 0, "the unpushed commit was destroyed"
    heads = sp.run(["git", "branch", "--contains", sha], cwd=str(d),
                   capture_output=True, text=True).stdout
    assert "feat/x" in heads, "the branch that reaches it was deleted"


def test_clearing_a_dead_ref_is_recorded(tmp_path):
    """A ref vanishing with no record is how a later reader loses the thread."""
    mod = _mod(tmp_path)
    d = _repo_with_branch(tmp_path, "feat/x")
    notes = []

    class L:
        def note(self, phase, **kw):
            notes.append((phase, kw))

    assert mod.create_branch(d, "feat/x", L()) == "feat/x"
    assert any(p == "branch-cleared" for p, _ in notes), notes


def test_recovery_survives_a_localised_git(tmp_path, monkeypatch):
    """Review 2530 LOW. Keying recovery on the English 'already exists' meant a
    non-English host skipped it entirely and regressed to fails-forever. The
    branch is now asked for as a ref, so git's language cannot matter."""
    mod = _mod(tmp_path)
    d = _repo_with_branch(tmp_path, "feat/x")
    real = mod.subprocess.run

    def localised(argv, **kw):
        r = real(argv, **kw)
        if argv[:3] == ["git", "checkout", "-b"] and r.returncode != 0:
            class R:
                returncode = 128
                stdout = ""
                stderr = "schwerwiegend: Branch 'feat/x' existiert bereits"
            return R()
        return r

    monkeypatch.setattr(mod.subprocess, "run", localised)
    assert mod.create_branch(d, "feat/x") == "feat/x"
