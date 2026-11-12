"""Vendor CLIs as implementers, and the usage-limit gates scoped to the seats
that spend each subscription.

A writer runner (codex, antigravity, muse) edits the worktree inside its own
sandbox and holds no push credential; the orchestrator commits, pushes and opens
the marked pull request. There are four subscriptions, each with its own gate:
a closed gate holds only the repos with an enabled seat on that account, so the
fleet keeps dispatching on the other vendors while one is at its limit.
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
# Plan 0202: the Codex and antigravity model variants implement through their
# base runner's own `writer_argv`, so they are writers too; `sub-haiku`, like
# every Claude runner, is not.
WRITERS = ("sub-codex", "sub-codex-sol", "sub-codex-terra", "sub-codex-luna",
          "sub-antigravity", "sub-antigravity-flash", "sub-muse")

PLAN = """---
id: 0001-x
status: ready
repo: operator/x
zone: public
paths:
  - "docs/*.md"
---

# Do the thing

body
"""


def _load(name, mod_name):
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet"))
    mod = _load("orchestrator", "orchestrator_writer")
    monkeypatch.setattr(mod, "implementer_timeout_minutes", lambda repo=None: 1)
    monkeypatch.setattr(mod, "write_transcript", lambda *a, **k: ("t.txt", []))
    return mod


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def wt(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    w = tmp_path / "wt"
    _git(tmp_path, "init", "-q", "-b", "main", str(w))
    for k, v in (("user.name", "implbot"), ("user.email", "m@example.org")):
        _git(w, "config", k, v)
    (w / "plans").mkdir()
    (w / "plans" / "0001-x.md").write_text(PLAN)
    (w / "docs").mkdir()
    (w / "docs" / "a.md").write_text("a\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-qm", "base")
    _git(w, "remote", "add", "origin", str(remote))
    _git(w, "checkout", "-qb", "feat/x")
    return w


class Log:
    def __init__(self):
        self.events = []

    def note(self, kind, **kw):
        self.events.append((kind, kw))


def _opener(tmp_path, monkeypatch, orch):
    out = tmp_path / "opened"
    p = tmp_path / "open-pr"
    p.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {out}\ncat \"$5\" >> {out}\n")
    p.chmod(0o755)
    monkeypatch.setattr(orch, "OPEN_PR_SH", str(p))
    return out


def _stub(tmp_path, body, rc=0):
    """A fake vendor CLI: records its argv, stdin and environment, then runs `body`."""
    p = tmp_path / "cli-stub"
    rec = tmp_path / "rec.json"
    p.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"data = sys.stdin.read()\n"
        f"pf = sys.argv[sys.argv.index('--prompt-file') + 1] if '--prompt-file' in sys.argv else None\n"
        f"json.dump({{'argv': sys.argv[1:], 'stdin': data, 'env': dict(os.environ),\n"
        f"           'prompt_file': open(pf).read() if pf else None}}, open({str(rec)!r}, 'w'))\n"
        f"{body}\nsys.exit({rc})\n")
    p.chmod(0o755)
    return p, rec


def _runner(orch, name, stub):
    r = orch.RUNNERS[name]
    return r._replace(writer_argv=(str(stub),) + tuple(r.writer_argv[1:]),
                      settings_must_not_grant=())


def _ctx(orch):
    orch.set_run_context("operator/x", "0001-x", "plans/0001-x.md", PLAN, "feat/x",
                         "public")


CRED = types.SimpleNamespace(kind="local_cli", principal="operator",
                             initiator="operator")
WRITE = "open('docs/b.md', 'w').write('b\\n')"


# ------------------------------------------------------------ the registry

@pytest.mark.parametrize("name", WRITERS)
def test_each_writer_needs_its_own_login_and_never_a_claude_token(orch, name):
    r = orch.RUNNERS[name]
    assert r.writer_argv and r.needs == "local_cli"
    assert not set(r.extra_env) & set(orch._TOKEN_ENV.values())


def test_the_review_and_lane_argvs_are_untouched(orch):
    # the reviewer/test-lane shapes stay read-only; only writer_argv implements
    assert "read-only" in orch.RUNNERS["sub-codex"].argv
    assert "plan" in orch.RUNNERS["sub-antigravity"].argv
    assert orch.RUNNERS["sub-muse"].argv == ()


def test_no_writer_argv_disables_writes_or_bypasses_its_sandbox(orch):
    for name in WRITERS:
        argv = " ".join(orch.RUNNERS[name].writer_argv)
        assert "--dangerously" not in argv and "--disable-write" not in argv
    assert "workspace-write" in orch.RUNNERS["sub-codex"].writer_argv
    assert "--sandbox" in orch.RUNNERS["sub-antigravity"].writer_argv
    assert "--workspace" in orch.RUNNERS["sub-muse"].writer_argv


def test_every_other_runner_has_no_writer_argv(orch):
    assert {n for n, r in orch.RUNNERS.items() if r.writer_argv} == set(WRITERS)


# ------------------------------------------------------------ the credential

@pytest.mark.parametrize("name", WRITERS)
def test_the_operator_routes_to_a_writer_on_its_own_login(orch, name, monkeypatch):
    cred = orch.Credential("oauth_token", "operator", "vault")
    r = orch.route_implementer({"zone": "public", "repo": "operator/x"}, cred,
                               {"EUNOMIA_IMPL_RUNNER": name}, repo="operator/x")
    assert r.name == name
    assert orch.serving_credential(cred, r).kind == "local_cli"


def test_a_venture_is_never_served_by_the_operators_logins(orch):
    cred = orch.Credential("oauth_token", "acme", "acme's token", "acme")
    with pytest.raises(orch.Stop, match="needs a 'local_cli' credential"):
        orch.route_implementer({"zone": "public", "repo": "acme/x"}, cred,
                               {"EUNOMIA_IMPL_RUNNER": "sub-codex"}, repo="acme/x")


def test_a_claude_runner_keeps_the_oauth_token(orch):
    cred = orch.Credential("oauth_token", "operator", "vault")
    assert orch.serving_credential(cred, orch.RUNNERS["sub-sonnet"]) is cred


def test_a_private_plan_still_never_reaches_a_writer(orch):
    cred = orch.Credential("oauth_token", "operator", "vault")
    with pytest.raises(orch.Stop, match="not 'public'"):
        orch.route_implementer({"zone": "private"}, cred,
                               {"EUNOMIA_IMPL_RUNNER": "sub-codex"}, repo="operator/x")


# ------------------------------------------------------------ the spawn

def test_codex_gets_the_prompt_on_stdin_with_the_publish_note_and_no_agent(
        orch, wt, tmp_path, monkeypatch):
    _opener(tmp_path, monkeypatch, orch)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-should-not-pass")
    stub, rec = _stub(tmp_path, WRITE)
    _ctx(orch)
    orch.run_implementer(wt, "THE PROMPT", CRED, _runner(orch, "sub-codex", stub),
                         "L", Log())
    got = json.loads(rec.read_text())
    assert got["stdin"].startswith("THE PROMPT")
    assert "Do NOT run `git push`" in got["stdin"]
    assert "SSH_AUTH_SOCK" not in got["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in got["env"]
    assert got["argv"][-1] == "-"


def test_antigravity_gets_one_stream_json_line(orch, wt, tmp_path, monkeypatch):
    _opener(tmp_path, monkeypatch, orch)
    stub, rec = _stub(tmp_path, WRITE)
    monkeypatch.setattr(orch, "ANTIGRAVITY_BIN", str(stub))
    _ctx(orch)
    orch.run_implementer(wt, "THE PROMPT", CRED,
                         _runner(orch, "sub-antigravity", stub), "L", Log())
    line = json.loads(json.loads(rec.read_text())["stdin"])
    assert line["event"] == "user"
    assert line["message"]["content"].startswith("THE PROMPT")


def test_muse_gets_a_prompt_file_and_the_worktree_never_argv(
        orch, wt, tmp_path, monkeypatch):
    _opener(tmp_path, monkeypatch, orch)
    stub, rec = _stub(tmp_path, WRITE)
    _ctx(orch)
    orch.run_implementer(wt, "THE PROMPT", CRED, _runner(orch, "sub-muse", stub),
                         "L", Log())
    got = json.loads(rec.read_text())
    assert got["prompt_file"].startswith("THE PROMPT")
    assert not any("THE PROMPT" in a for a in got["argv"])
    assert got["argv"][got["argv"].index("--workspace") + 1] == str(wt.resolve())
    pf = Path(got["argv"][got["argv"].index("--prompt-file") + 1])
    assert not pf.exists(), "the prompt file outlived the run"


# ------------------------------------------------------------ the publish

def test_edits_are_committed_flipped_pushed_and_opened_by_the_orchestrator(
        orch, wt, tmp_path, monkeypatch):
    out = _opener(tmp_path, monkeypatch, orch)
    stub, _ = _stub(tmp_path, WRITE)
    _ctx(orch)
    s = orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-codex", stub), "L",
                             Log())
    assert s.exit_code == 0
    assert "status: done" in (wt / "plans/0001-x.md").read_text()
    assert _git(wt, "status", "--porcelain") == ""
    msg = _git(wt, "log", "-1", "--format=%B")
    assert "implemented by sub-codex" in msg and "Harness: openai" in msg
    assert "feat/x" in _git(wt, "ls-remote", "origin")
    opened = out.read_text()
    assert opened.splitlines()[:3] == ["operator/x", "feat/x", "main"]
    assert "Plan: 0001-x" in opened and "sub-codex" in opened


def test_a_writer_that_committed_itself_still_pushes(orch, wt, tmp_path, monkeypatch):
    _opener(tmp_path, monkeypatch, orch)
    stub, _ = _stub(tmp_path, WRITE + "\nimport subprocess\n"
                    "subprocess.run(['git', 'add', '-A'], check=True)\n"
                    "subprocess.run(['git', 'commit', '-qm', 'own'], check=True)\n")
    _ctx(orch)
    orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-codex", stub), "L", Log())
    assert "feat/x" in _git(wt, "ls-remote", "origin")
    assert (wt / "docs/b.md").exists()


def test_a_run_that_changed_nothing_publishes_nothing(orch, wt, tmp_path, monkeypatch):
    out = _opener(tmp_path, monkeypatch, orch)
    stub, _ = _stub(tmp_path, "print('I have a question')")
    _ctx(orch)
    log = Log()
    s = orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-codex", stub), "L", log)
    assert s.exit_code == 0
    assert _git(wt, "ls-remote", "origin") == ""
    assert not out.exists()
    assert ("writer-no-changes", {"runner": "sub-codex"}) in log.events


def test_a_failed_run_publishes_nothing(orch, wt, tmp_path, monkeypatch):
    out = _opener(tmp_path, monkeypatch, orch)
    stub, _ = _stub(tmp_path, WRITE, rc=1)
    _ctx(orch)
    s = orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-codex", stub), "L", Log())
    assert s.exit_code == 1
    assert _git(wt, "ls-remote", "origin") == ""
    assert not out.exists()


def test_a_fix_round_pushes_and_opens_nothing(orch, wt, tmp_path, monkeypatch):
    out = _opener(tmp_path, monkeypatch, orch)
    stub, _ = _stub(tmp_path, WRITE)
    _ctx(orch)
    orch.run_implementer(wt, "fix it", CRED, _runner(orch, "sub-codex", stub), "L",
                         Log(), tag="r1")
    assert "feat/x" in _git(wt, "ls-remote", "origin")
    assert not out.exists()


def test_a_follow_up_branch_is_refused(orch, wt, tmp_path, monkeypatch):
    stub, _ = _stub(tmp_path, WRITE)
    _ctx(orch)
    with pytest.raises(orch.Stop, match="no publish path"):
        orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-codex", stub), "L",
                             Log(), tag="f1")


# ------------------------------------------------------------ the settings precondition

@pytest.mark.parametrize("grant", [{"permissions": {"allow": ["Bash(*)"]}},
                                   {"hooks": {"PreToolUse": []}}])
def test_a_settings_file_that_grants_refuses_the_writer_spawn(
        orch, wt, tmp_path, grant):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(grant))
    stub, rec = _stub(tmp_path, WRITE)
    _ctx(orch)
    r = _runner(orch, "sub-antigravity", stub)._replace(
        settings_must_not_grant=(str(settings),))
    with pytest.raises(orch.Stop, match="refusing to spawn"):
        orch.run_implementer(wt, "p", CRED, r, "L", Log())
    assert not rec.exists()                      # nothing was spawned


def test_a_settings_file_that_grants_nothing_lets_the_writer_run(
        orch, wt, tmp_path, monkeypatch):
    _opener(tmp_path, monkeypatch, orch)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    stub, rec = _stub(tmp_path, WRITE)
    _ctx(orch)
    r = _runner(orch, "sub-antigravity", stub)._replace(
        settings_must_not_grant=(str(settings),))
    orch.run_implementer(wt, "p", CRED, r, "L", Log())
    assert rec.exists()


# ------------------------------------------------------------ the final answer

QUESTION = ("I stopped before changing anything.\n\n```plan-question\n"
            '{"section": "Scope", "question": "Which file?", "why": "two fit", '
            '"blocking": true}\n```\n')

CODEX_JSONL = "\n".join(json.dumps(r) for r in (
    {"type": "thread.started", "thread_id": "t"},
    {"type": "item.completed", "item": {"id": "i0", "type": "agent_message",
                                        "text": "working on it"}},
    {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                        "command": "ls", "aggregated_output": "x"}},
    {"type": "item.completed", "item": {"id": "i2", "type": "agent_message",
                                        "text": QUESTION}},
    {"type": "turn.completed", "usage": {"output_tokens": 9}}))

ANTIGRAVITY_STREAM = "\n".join(json.dumps(r) for r in (
    {"event": "assistant", "text": "thinking"},
    {"event": "result", "result": {"status": "ok", "response": QUESTION}}))

MUSE_JSONL = "\n".join(json.dumps(r) for r in (
    {"schema_version": 1, "record_type": "status",
     "payload_type": "run.output.delta", "payload": {}},
    {"schema_version": 1, "record_type": "event",
     "payload_type": "run.terminal.completed",
     "payload": {"kind": "run_terminal", "terminal": "completed", "text": QUESTION}}))


@pytest.mark.parametrize("name,stream", [("sub-codex", CODEX_JSONL),
                                         ("sub-antigravity", ANTIGRAVITY_STREAM),
                                         ("sub-muse", MUSE_JSONL)])
def test_a_writers_plan_question_is_found_in_its_decoded_answer(
        orch, wt, tmp_path, name, stream):
    stub, _ = _stub(tmp_path, f"sys.stdout.write({stream!r} + '\\n')")
    _ctx(orch)
    s = orch.run_implementer(wt, "p", CRED, _runner(orch, name, stub), "L", Log())
    assert s.result.strip() == QUESTION.strip()
    q, err = orch.read_plan_question(s.result)
    assert err is None and q["question"] == "Which file?"
    # the raw stream hides it: the block sits inside a JSON string
    assert orch.read_plan_question(stream) == (None, None)


@pytest.mark.parametrize("reader", ["read_codex_result",
                                    "read_antigravity_result_text",
                                    "read_muse_result"])
def test_an_undecodable_stream_falls_back_to_the_raw_text(orch, reader):
    assert getattr(orch, reader)("plain words\n") == "plain words\n"
    assert getattr(orch, reader)("") == ""


def test_a_claude_result_is_read_as_before(orch):
    stream = json.dumps({"type": "result", "result": "the answer"}) + "\n"
    assert orch.read_result_text(stream) == "the answer"
    assert orch.WRITER_RESULT_READERS.get("claude") is None


# ------------------------------------------------------- the denied action
#
# plan 0192. The exact shape twice recorded in production (the 0179 and 0184
# writer leases, both sub-antigravity): exit 0, `status: SUCCESS`, an empty
# `response`, after headless mode auto-denied a `run_command` call neither
# transcript could answer a permission prompt for.
DENIED_RECORD = {"conversation_id": "c", "status": "SUCCESS", "response": "",
                 "duration_seconds": 25.6, "num_turns": 1,
                 "denied_actions": [{"action": "command", "display_name": "RunCommand"}]}
DENIED_STREAM = json.dumps({"event": "result", "result": DENIED_RECORD})


def _prints(stream):
    return f"sys.stdout.write({stream!r} + '\\n')"


def test_read_antigravity_denied_actions_matches_the_production_shape(orch):
    assert orch.read_antigravity_denied_actions(DENIED_STREAM) == [
        {"action": "command", "display_name": "RunCommand"}]
    assert orch.read_antigravity_denied_actions("") == []
    assert orch.read_antigravity_denied_actions("not json\n") == []
    ok = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "done"}})
    assert orch.read_antigravity_denied_actions(ok) == []


def test_a_denied_action_with_no_edits_is_a_stop_not_a_silent_success(
        orch, wt, tmp_path, monkeypatch):
    out = _opener(tmp_path, monkeypatch, orch)
    stub, _ = _stub(tmp_path, _prints(DENIED_STREAM))
    monkeypatch.setattr(orch, "ANTIGRAVITY_BIN", str(stub))
    _ctx(orch)
    log = Log()
    with pytest.raises(orch.Stop, match="denied by its own headless mode"):
        orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-antigravity", stub),
                             "L", log)
    assert not out.exists()
    assert _git(wt, "ls-remote", "origin") == ""
    assert "status: ready" in (wt / "plans/0001-x.md").read_text()
    assert ("writer-denied", {"runner": "sub-antigravity", "actions": "command"}) in log.events


def test_a_denied_action_after_edits_still_refuses_to_publish(
        orch, wt, tmp_path, monkeypatch):
    # The case named explicitly in the plan: exit 0, real edits on disk, and a
    # denial later in the same turn. Today's bug would read `changed` as True
    # and publish; this must stop before that check ever runs.
    out = _opener(tmp_path, monkeypatch, orch)
    stub, _ = _stub(tmp_path, WRITE + "\n" + _prints(DENIED_STREAM))
    monkeypatch.setattr(orch, "ANTIGRAVITY_BIN", str(stub))
    _ctx(orch)
    start = _git(wt, "rev-parse", "HEAD")
    with pytest.raises(orch.Stop, match="denied by its own headless mode"):
        orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-antigravity", stub),
                             "L", Log())
    assert (wt / "docs/b.md").exists()                 # the edit is preserved
    assert _git(wt, "rev-parse", "HEAD") == start       # but never committed
    assert not out.exists()
    assert _git(wt, "ls-remote", "origin") == ""
    assert "status: ready" in (wt / "plans/0001-x.md").read_text()


def test_an_undenied_antigravity_run_still_publishes(orch, wt, tmp_path, monkeypatch):
    # The regression guard: a normal success record, with no denied_actions at
    # all, must still commit, push and open the pull request as before.
    out = _opener(tmp_path, monkeypatch, orch)
    ok = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "done"}})
    stub, _ = _stub(tmp_path, WRITE + "\n" + _prints(ok))
    monkeypatch.setattr(orch, "ANTIGRAVITY_BIN", str(stub))
    _ctx(orch)
    s = orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-antigravity", stub),
                             "L", Log())
    assert s.denied_actions == ()
    assert "feat/x" in _git(wt, "ls-remote", "origin")
    assert out.exists()


def test_only_antigravity_gets_a_denial_reader(orch):
    assert orch.WRITER_DENIAL_READERS.get("openai") is None
    assert orch.WRITER_DENIAL_READERS.get("muse") is None
    assert orch.WRITER_DENIAL_READERS["google"] is orch.read_antigravity_denied_actions


def test_only_antigravity_is_told_it_has_no_shell(orch, wt, tmp_path, monkeypatch):
    _opener(tmp_path, monkeypatch, orch)
    stub, rec = _stub(tmp_path, WRITE)
    monkeypatch.setattr(orch, "ANTIGRAVITY_BIN", str(stub))
    _ctx(orch)
    orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-antigravity", stub), "L", Log())
    line = json.loads(json.loads(rec.read_text())["stdin"])
    assert "No shell in this run" in line["message"]["content"]

    stub2, rec2 = _stub(tmp_path, WRITE)
    orch.run_implementer(wt, "p", CRED, _runner(orch, "sub-codex", stub2), "L", Log())
    assert "No shell in this run" not in json.loads(rec2.read_text())["stdin"]


# ------------------------------------------------------------ the seats

def _seed(orch, tmp_path, rows):
    models = orch._load_sibling("fleet-models", "_fm_writer_test")
    con = models.connect(str(tmp_path / "fleet.db"))
    for repo, position, runner in rows:
        models.set_runner(con, repo, position, runner, actor="cli:test")
    con.close()


@pytest.mark.parametrize("name,account", [
    ("sub-sonnet", "claude"), ("sub-opus", "claude"), ("sub-fable", "claude"),
    ("sub-codex", "codex"), ("sub-antigravity", "gemini"), ("sub-muse", "muse"),
    ("local-qwen", None), ("api-sonnet", None), ("hosted-muse-spark-open", None)])
def test_a_runner_spends_the_account_its_family_names(orch, name, account):
    got = orch.runner_account(orch.RUNNERS[name], "operator/x")
    assert got == account


def test_a_venture_spends_no_operator_claude_account(orch, monkeypatch):
    monkeypatch.setattr(orch, "principal_of", lambda repo, path=None: "acme")
    assert orch.runner_account(orch.RUNNERS["sub-sonnet"], "acme/x") is None
    assert orch.runner_account(orch.RUNNERS["sub-codex"], "acme/x") == "codex"


def test_the_implementer_override_decides_the_implementer_seat(orch):
    assert orch.seat_accounts("operator/x", {"EUNOMIA_IMPL_RUNNER": "sub-codex"}) == {"codex"}
    assert orch.seat_accounts("operator/x", {"EUNOMIA_IMPL_RUNNER": "local-qwen"}) == set()


def test_a_codex_implementer_with_a_claude_reviewer_spends_both(orch, tmp_path):
    _seed(orch, tmp_path, [("default", "implementer", "sub-codex"),
                           ("default", "code_reviewer", "sub-sonnet")])
    spend = orch.seat_spend("operator/x", positions=("implementer", "code_reviewer"))
    assert set(spend) == {"codex", "claude"}
    assert spend["claude"] == "code_reviewer#1 sub-sonnet"
    assert spend["codex"] == "implementer#1 sub-codex"


def test_a_repos_own_row_wins_over_default_and_a_disabled_seat_spends_nothing(
        orch, tmp_path):
    _seed(orch, tmp_path, [("default", "implementer", "sub-codex"),
                           ("default", "tester", "sub-sonnet"),
                           ("operator/x", "implementer", "local-qwen"),
                           ("operator/x", "plan_reviewer", "sub-antigravity")])
    models = orch._load_sibling("fleet-models", "_fm_writer_test2")
    con = models.connect(str(tmp_path / "fleet.db"))
    models.disable(con, "default", "tester", 1, actor="cli:test")
    con.close()
    assert set(orch.seat_spend("operator/x", positions=("implementer", "tester", "plan_reviewer"))) == {"gemini", "gemini-argon"}  # argon seats spend argon's own limit too


def test_a_repo_wholly_on_other_vendors_spends_no_claude(orch, tmp_path):
    _seed(orch, tmp_path, [("default", "implementer", "sub-codex"),
                           ("default", "code_reviewer", "sub-antigravity"),
                           ("default", "plan_reviewer", "sub-muse")])
    assert set(orch.seat_spend("operator/x", positions=("implementer", "code_reviewer", "plan_reviewer"))) == {"codex", "gemini", "gemini-argon", "muse"}


def test_an_unresolvable_seat_is_unknown(orch, tmp_path, monkeypatch):
    def boom(repo, env=None):
        raise orch.Stop("no implementer runner configured")
    monkeypatch.setattr(orch, "resolved_implementer_env", boom)
    assert orch.seat_accounts("operator/x") == {orch.SEAT_UNKNOWN}


def test_a_row_naming_no_runner_is_unknown(orch, tmp_path):
    import sqlite3
    _seed(orch, tmp_path, [("default", "implementer", "sub-codex")])
    con = sqlite3.connect(str(tmp_path / "fleet.db"))
    con.execute("INSERT INTO repo_model (repo, position, ordinal, runner, disabled) "
                "VALUES ('default','tester',1,'gone-runner',0)")
    con.commit()
    con.close()
    assert orch.SEAT_UNKNOWN in orch.seat_spend("operator/x", positions=("tester",))


# ------------------------------------------------------------ the hold

@pytest.fixture
def watch(monkeypatch):
    mod = _load("fleet-watch", "fleet_watch_writer")
    monkeypatch.setattr(mod, "_hold_memo", None)
    return mod


CLAUDE_CLOSED = {"claude": ("weekly", "2026-11-03T15:59:59Z")}
CODEX_CLOSED = {"codex": ("vendor:codex", "2026-11-04T00:00:00Z")}


def _seats(watch, monkeypatch, seats):
    """`seats`: repo -> {account: seat}."""
    calls = []

    def spend(repo):
        calls.append(repo)
        return seats[repo]
    monkeypatch.setattr(watch, "_hold_orch", types.SimpleNamespace(seat_spend=spend))
    return calls


def test_claudes_gate_holds_a_repo_with_a_claude_reviewer_seat_though_codex_implements(
        watch, monkeypatch):
    _seats(watch, monkeypatch, {"operator/x": {"codex": "implementer#1 sub-codex",
                                              "claude": "code_reviewer#1 sub-sonnet"}})
    hold = watch._hold_for("operator/x", CLAUDE_CLOSED)
    assert hold == ("weekly", "2026-11-03T15:59:59Z", "code_reviewer#1 sub-sonnet")


def test_a_repo_wholly_on_other_vendors_is_not_held_behind_claude(watch, monkeypatch):
    _seats(watch, monkeypatch, {"operator/x": {"codex": "implementer#1 sub-codex",
                                              "gemini": "code_reviewer#1 sub-antigravity"}})
    assert watch._hold_for("operator/x", CLAUDE_CLOSED) is None


def test_the_codex_gate_holds_a_repo_with_a_codex_seat_and_the_note_names_it(
        watch, monkeypatch):
    _seats(watch, monkeypatch, {"operator/x": {"claude": "implementer#1 sub-sonnet",
                                              "codex": "code_reviewer#1 sub-codex"},
                                "operator/y": {"claude": "implementer#1 sub-sonnet"}})
    hold = watch._hold_for("operator/x", CODEX_CLOSED)
    assert hold == ("vendor:codex", "2026-11-04T00:00:00Z", "code_reviewer#1 sub-codex")
    quota = watch._quota()
    assert quota.held_note(hold) == ("held: vendor:codex closed until "
                                     "2026-11-04T00:00:00Z (seat code_reviewer#1 sub-codex)")
    assert watch._hold_for("operator/y", CODEX_CLOSED) is None


def test_an_unknown_seat_holds_behind_any_closed_gate(watch, monkeypatch):
    _seats(watch, monkeypatch, {"operator/x": {"?": "tester#1 (UnknownRunner)"}})
    assert watch._hold_for("operator/x", CODEX_CLOSED)[2] == "tester#1 (UnknownRunner)"
    assert watch._hold_for("operator/x", CLAUDE_CLOSED) is not None


def test_no_closed_gate_asks_nothing(watch, monkeypatch):
    calls = _seats(watch, monkeypatch, {"operator/x": {"claude": "s"}})
    assert watch._hold_for("operator/x", None) is None
    assert watch._hold_for("operator/x", {}) is None
    assert calls == []


def test_a_repos_seats_are_read_once_per_cycle(watch, monkeypatch):
    calls = _seats(watch, monkeypatch, {"operator/x": {"gemini": "s"}})
    watch._hold_for("operator/x", CLAUDE_CLOSED)
    watch._hold_for("operator/x", CODEX_CLOSED)
    assert calls == ["operator/x"]


def test_an_unloadable_orchestrator_holds(watch, monkeypatch):
    monkeypatch.setattr(watch, "_hold_orch", None)

    def fail():
        raise ImportError("gone")
    monkeypatch.setattr(watch, "_orchestrator_module", fail)
    assert watch._hold_for("operator/x", CLAUDE_CLOSED) is not None

@pytest.mark.parametrize("name", WRITERS)
@pytest.mark.parametrize("answer", [QUESTION, "```plan-question\nnot json\n```\n"])
def test_partial_edits_with_a_question_are_not_published(
        orch, wt, tmp_path, monkeypatch, name, answer):
    stub, _ = _stub(tmp_path, WRITE + f"\nprint({answer!r})")
    opened = _opener(tmp_path, monkeypatch, orch)
    _ctx(orch)
    start = _git(wt, "rev-parse", "HEAD")
    orch.run_implementer(wt, "p", CRED, _runner(orch, name, stub), "L", Log())
    assert (wt / "docs/b.md").exists()   # preserve work for the question handler
    assert _git(wt, "rev-parse", "HEAD") == start
    assert "status: ready" in (wt / "plans/0001-x.md").read_text()
    assert not opened.exists()
    assert not _git(wt, "ls-remote", "origin", "refs/heads/feat/x").strip()


def test_dispatch_ignores_all_non_implementer_seats(orch, watch, tmp_path, monkeypatch):
    _seed(orch, tmp_path, [("default", "implementer", "sub-codex"),
                           ("default", "code_reviewer", "sub-sonnet"),
                           ("default", "style_reviewer", "sub-sonnet"),
                           ("default", "plan_reviewer", "sub-sonnet"),
                           ("default", "tester", "sub-opus"),
                           ("default", "mediator", "sub-opus"),
                           ("default", "lane_reviewer", "sub-sonnet")])
    monkeypatch.setattr(watch, "_hold_orch", orch)
    assert watch._hold_for("operator/x", CLAUDE_CLOSED) is None
    assert watch._hold_for("operator/x", CODEX_CLOSED)[2] == "implementer#1 sub-codex"


def test_review_gate_uses_only_the_routed_position(orch, tmp_path, monkeypatch):
    _seed(orch, tmp_path, [("default", "implementer", "sub-codex"),
                           ("default", "code_reviewer", "sub-codex"),
                           ("default", "style_reviewer", "sub-sonnet")])
    quota = types.SimpleNamespace(closed_vendor_gates=lambda: CLAUDE_CLOSED)
    load = orch._load_sibling
    monkeypatch.setattr(orch, "_load_sibling", lambda name, mod: quota if name == "fleet-quota" else load(name, mod))
    assert orch.review_quota_hold("operator/x", "code_reviewer") is None
    assert "style_reviewer#1 sub-sonnet" in orch.review_quota_hold("operator/x", "style_reviewer")


def test_venture_reviewers_still_spend_the_operator_account(orch, monkeypatch):
    monkeypatch.setattr(orch, "principal_of", lambda repo, path=None: "acme")
    runner = orch.RUNNERS["sub-sonnet"]
    assert orch.runner_account(runner, "acme/x") is None
    assert orch.runner_account(runner, "acme/x", "code_reviewer") == "claude"
