"""bin/fleet-cli-canary — a codex-cli upgrade passes a canary before it is
linked (plan 0208).

No real vendor CLI is ever spawned by DEFAULT: a small fake codex script
plays its part, reading a `behavior.json` sitting beside it so one script
serves `--version`, `exec --help` and a real `exec` run alike. The
sandbox-exec tier (tester/reviewer profile start-up) runs only on macOS and
skips elsewhere, the same convention `tests/test_codex_confinement.py` uses.
"""
import collections
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
ROOT = Path(__file__).resolve().parent.parent
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

needs_sandbox_exec = pytest.mark.skipif(
    not os.path.exists(SANDBOX_EXEC),
    reason=f"{SANDBOX_EXEC} is absent: the sandbox tier cannot run here")


def _git(args, cwd, check=True):
    r = subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@e.com",
                        *args], cwd=str(cwd), capture_output=True, text=True)
    if check:
        assert r.returncode == 0, f"git {args} in {cwd} failed: {r.stderr}"
    return r


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


@pytest.fixture
def cc():
    return _load("fleet_cli_canary_under_test", BIN / "fleet-cli-canary")


@pytest.fixture
def lane_mod():
    return _load("fleet_lane_for_canary_test", BIN / "fleet-lane")


FakeRunner = collections.namedtuple("FakeRunner", "argv writer_argv extra_env")


def fake_orch(codex_bin, argv=None, writer_argv=None, extra_env=()):
    """A minimal duck-typed stand-in for bin/orchestrator: just enough of its
    surface (`RUNNERS`, `CODEX_BIN`, `notify`, `redact_transcript`) for the
    functions under test, never the real registry."""
    mod = type(sys)("fake_orchestrator")
    mod.CODEX_BIN = codex_bin
    mod.MUSE_BIN = "/fake/muse"   # bin/fleet-lane's build_adapters builds every
                                 # adapter eagerly, sub-muse included
    mod.read_antigravity_results = lambda text: []
    mod.read_antigravity_result = lambda text: None
    mod.antigravity_stdin_line = lambda prompt: prompt
    mod.RUNNERS = {
        "sub-codex": FakeRunner(
            argv=argv or (
                "/usr/bin/sandbox-exec", "-f", "<profile>",
                "-D", "LANE=<lane>", "-D", "CODEX_HOME=<codex-home>",
                codex_bin, "exec", "-s", "read-only", "--skip-git-repo-check",
                "--ignore-user-config", "--ephemeral", "--ignore-rules", "--json",
                "-c", 'sqlite_home="<lane>"', "--output-schema", "<schema>",
                "-o", "<out>", "-m", "gpt-6-astra"),
            writer_argv=writer_argv or (
                codex_bin, "exec", "-s", "workspace-write", "--skip-git-repo-check",
                "--ephemeral", "--ignore-rules", "--json", "-m", "gpt-6-astra", "-"),
            extra_env=extra_env),
    }
    mod.notify_calls = []

    def notify(title, body):
        mod.notify_calls.append((title, body))
        return True

    def redact_transcript(text):
        return text or "", []

    mod.notify = notify
    mod.redact_transcript = redact_transcript
    return mod


_FAKE_CODEX = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    HERE = os.path.dirname(os.path.abspath(sys.argv[0]))
    with open(os.path.join(HERE, "behavior.json")) as f:
        behavior = json.load(f)
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        sys.stdout.write(behavior.get("version", "codex-cli 0.0.0") + "\\n")
        sys.exit(0)
    if argv[:2] == ["exec", "--help"]:
        sys.stdout.write(behavior.get("help", ""))
        sys.exit(0)
    sys.stdin.read()
    sys.stdout.write(behavior.get("stream", ""))
    sys.stdout.flush()
    if "-o" in argv:
        out_path = argv[argv.index("-o") + 1]
        doc = behavior.get("doc")
        if doc is not None:
            with open(out_path, "w") as f:
                json.dump(doc, f)
    sys.exit(behavior.get("rc", 0))
    ''')


def write_fake_codex(dirpath, version="codex-cli 0.166.0", help_text=None,
                     stream="", doc=None, rc=0):
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    script = dirpath / "codex"
    script.write_text(_FAKE_CODEX)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    behavior = {"version": version, "help": help_text or "", "stream": stream, "rc": rc}
    if doc is not None:
        behavior["doc"] = doc
    (dirpath / "behavior.json").write_text(json.dumps(behavior))
    return script


ALL_FLAGS_HELP = (
    "-s, --sandbox <SANDBOX>\n--skip-git-repo-check\n--ignore-user-config\n"
    "--ephemeral\n--ignore-rules\n--json\n-c, --config <K=V>\n"
    "--output-schema <SCHEMA>\n-o, --output-last-message <OUT>\n-m, --model <MODEL>\n")


def ok_stream():
    return json.dumps({"type": "item.completed",
                       "item": {"id": "i0", "type": "agent_message", "text": "ok"}}) + "\n"


def turn_failed_stream(reason="boom"):
    return (ok_stream().replace('"text": "ok"', '"text": "ok"')
            + json.dumps({"type": "turn.failed", "reason": reason}) + "\n")


# --------------------------------------------------------------------- step 1


def test_same_file_as_linked_refuses_exit_2(cc, tmp_path, lane_mod):
    # D1 item 1: "Refuse with exit 2" — a usage error about the invocation,
    # so it is `cc.Stop` (main() turns that into rc 2), not a step fail, and
    # no report is written (same as every other exit-2 refusal).
    script = write_fake_codex(tmp_path / "linked")
    orch = fake_orch(str(script))
    with pytest.raises(cc.Stop, match="linked"):
        cc.run_codex_canary(str(script), False, orch_mod=orch, lane_mod=lane_mod,
                            lib_mod=_fake_lib(tmp_path))
    assert not (tmp_path / "work").exists()


def test_same_version_as_linked_refuses(cc, tmp_path, lane_mod):
    cand = write_fake_codex(tmp_path / "candidate", version="codex-cli 0.153.4")
    linked = write_fake_codex(tmp_path / "linked", version="codex-cli 0.153.4")
    orch = fake_orch(str(linked))
    with pytest.raises(cc.Stop, match="same version"):
        cc.run_codex_canary(str(cand), False, orch_mod=orch, lane_mod=lane_mod,
                            lib_mod=_fake_lib(tmp_path))
    assert not (tmp_path / "work").exists()


_PASS_STARTUP = {
    "tester": {"status": "pass", "detail": None, "rc": 0, "timed_out": False},
    "reviewer": {"status": "skipped", "detail": "stubbed for this test"},
    "writer": {"status": "pass", "detail": None, "rc": 0, "timed_out": False},
}


def test_startup_only_skips_the_version_refusal_on_the_linked_cli(cc, tmp_path, lane_mod,
                                                                   monkeypatch):
    linked = write_fake_codex(tmp_path / "linked", version="codex-cli 0.153.4",
                              help_text=ALL_FLAGS_HELP, stream=ok_stream())
    orch = fake_orch(str(linked))
    monkeypatch.setattr(cc, "run_startup_steps", lambda *a, **k: dict(_PASS_STARTUP))
    rc = cc.run_codex_canary(str(linked), True, orch_mod=orch, lane_mod=lane_mod,
                             lib_mod=_fake_lib(tmp_path))
    report = json.loads((tmp_path / "work" / "canary" / "codex" / "codex-cli-0.153.4.json").read_text())
    assert report["steps"]["version"]["status"] == "skip"
    assert rc == 0, report


# --------------------------------------------------------------------- step 2


def test_a_missing_flag_fails_step_2_and_names_it(cc):
    orch = fake_orch("/opt/homebrew/bin/codex")
    flags = cc.codex_tester_flags(orch)
    assert "--output-schema" in flags and "-m" in flags and "-" not in flags
    assert "-f" not in flags and "-D" not in flags   # sandbox-exec's own, before CODEX_BIN


def test_check_codex_flags_names_what_is_missing(tmp_path):
    cc = _load("fleet_cli_canary_flags_test", BIN / "fleet-cli-canary")
    script = write_fake_codex(tmp_path, help_text="--json\n--ephemeral\n")
    missing = cc.check_codex_flags(str(script), ["--json", "--ephemeral", "-m"])
    assert missing == ["-m"]


def test_flag_match_is_a_whole_token_never_a_substring(tmp_path):
    cc = _load("fleet_cli_canary_flags_test2", BIN / "fleet-cli-canary")
    script = write_fake_codex(tmp_path, help_text="--output-schema-extended <X>\n")
    missing = cc.check_codex_flags(str(script), ["--output-schema"])
    assert missing == ["--output-schema"]


# --------------------------------------------------------------------- step 3


def test_tester_argv_tail_carries_neither_output_schema_nor_dash_o(cc):
    orch = fake_orch("/opt/homebrew/bin/codex")
    tail = cc.tester_argv_tail(orch)
    assert "--output-schema" not in tail and "-o" not in tail
    assert "<schema>" not in tail and "<out>" not in tail
    assert "-m" in tail and "gpt-6-astra" in tail


def test_writer_argv_tail_differs_from_tester_argv_tail(cc):
    # the writer probe (no outer profile) must start-up-test the real,
    # unconfined writer invocation — its own flags, never the tester argv's.
    orch = fake_orch("/opt/homebrew/bin/codex")
    writer_tail = cc.writer_argv_tail(orch)
    tester_tail = cc.tester_argv_tail(orch)
    assert "workspace-write" in writer_tail
    assert "read-only" not in writer_tail
    assert "--ignore-user-config" not in writer_tail
    assert writer_tail != tester_tail


def test_run_startup_steps_runs_the_writer_probe_with_writer_argv(cc, lane_mod, tmp_path):
    script = write_fake_codex(tmp_path / "cand", help_text=ALL_FLAGS_HELP, stream=ok_stream())
    orch = fake_orch(str(script))
    cc.REVIEWER_PROFILE_PATH = str(tmp_path / "no-reviewer-profile-here.sb")
    startup = cc.run_startup_steps(str(script), orch, lane_mod)
    assert startup["writer"]["status"] == "pass", startup["writer"]


def test_an_ok_stream_passes_step_3(cc):
    orch = fake_orch("/opt/homebrew/bin/codex")
    ok, detail = cc.codex_startup_outcome(ok_stream(), orch)
    assert ok is True and detail is None


def test_a_turn_failed_stream_fails_with_the_redacted_line(cc):
    orch = fake_orch("/opt/homebrew/bin/codex")
    stream = json.dumps({"type": "turn.failed", "reason": "sandbox denied"}) + "\n"
    ok, detail = cc.codex_startup_outcome(stream, orch)
    assert ok is False
    assert "sandbox denied" in detail


def test_turn_failed_after_ok_still_fails(cc):
    orch = fake_orch("/opt/homebrew/bin/codex")
    stream = ok_stream() + json.dumps({"type": "turn.failed", "reason": "late crash"}) + "\n"
    ok, detail = cc.codex_startup_outcome(stream, orch)
    assert ok is False and "late crash" in detail


def test_a_schema_shaped_reply_does_not_count_as_ok(cc):
    orch = fake_orch("/opt/homebrew/bin/codex")
    schema_doc = json.dumps({"files": [], "gaps": [], "notes": None})
    stream = json.dumps({"type": "item.completed",
                         "item": {"id": "i0", "type": "agent_message", "text": schema_doc}}) + "\n"
    ok, detail = cc.codex_startup_outcome(stream, orch)
    assert ok is False


def test_nonjson_crash_output_is_reported_as_the_last_line(cc):
    orch = fake_orch("/opt/homebrew/bin/codex")
    out = ("WARNING: proceeding, even though we could not create PATH aliases\n"
          "Error: Operation not permitted (os error 1)\n")
    ok, detail = cc.codex_startup_outcome(out, orch)
    assert ok is False
    assert "Operation not permitted" in detail


def test_reviewer_run_is_skipped_with_no_lane_or_codex_home_param(cc, tmp_path, lane_mod):
    profile = tmp_path / "bad.sb"
    profile.write_text("(version 1)\n(deny default)\n")
    cc.REVIEWER_PROFILE_PATH = str(profile)
    orch = fake_orch("/opt/homebrew/bin/codex")
    result = cc._run_reviewer_startup("/bin/true", ["exec"], lane_mod, orch, tmp_path)
    assert result["status"] == "skipped"
    assert "LANE" in result["detail"] or "CODEX_HOME" in result["detail"]


def test_reviewer_run_is_skipped_when_it_grants_no_auth_json_write(cc, tmp_path, lane_mod):
    profile = tmp_path / "noauth.sb"
    profile.write_text(
        '(version 1)\n(deny default)\n'
        '(allow file-read* (subpath (param "LANE")))\n'
        '(allow file-read* (subpath (param "CODEX_HOME")))\n'
        '(allow file-write* (subpath (param "LANE")))\n')
    cc.REVIEWER_PROFILE_PATH = str(profile)
    orch = fake_orch("/opt/homebrew/bin/codex")
    result = cc._run_reviewer_startup("/bin/true", ["exec"], lane_mod, orch, tmp_path)
    assert result["status"] == "skipped"
    assert "auth.json" in result["detail"]


def test_reviewer_run_is_skipped_when_the_profile_is_not_installed(cc, tmp_path, lane_mod):
    cc.REVIEWER_PROFILE_PATH = str(tmp_path / "nope.sb")
    orch = fake_orch("/opt/homebrew/bin/codex")
    result = cc._run_reviewer_startup("/bin/true", ["exec"], lane_mod, orch, tmp_path)
    assert result["status"] == "skipped"


def test_profile_grants_auth_write_reads_the_real_tester_profile(cc):
    text = (ROOT / "config" / "codex-tester.sb").read_text(encoding="utf-8")
    assert cc.profile_takes_param(text, "LANE")
    assert cc.profile_takes_param(text, "CODEX_HOME")
    assert cc.profile_grants_auth_write(text)


def test_a_fake_codex_passes_startup_under_no_outer_profile(cc, lane_mod, tmp_path):
    script = write_fake_codex(tmp_path / "cand", help_text=ALL_FLAGS_HELP, stream=ok_stream())
    orch = fake_orch(str(script))
    tail = cc.tester_argv_tail(orch)
    result = cc._run_startup(str(script), tail, None, lane_mod, orch,
                             tmp_path / "codex-home", "writer")
    assert result["status"] == "pass", result


def test_run_startup_resolves_a_symlinked_mkdtemp_for_the_sandbox_lane(cc, lane_mod, tmp_path,
                                                                        monkeypatch):
    """`/var/folders` (macOS's `$TMPDIR`) is itself a symlink to
    `/private/var/folders`; the sandbox profile's `(subpath (param "LANE"))`
    rules are checked against the resolved path, same as `fleet-lane`'s own
    `cwd = ...resolve()` (bin/fleet-lane)."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked-tmp"
    link.symlink_to(real)

    real_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(prefix=""):
        return real_mkdtemp(prefix=prefix, dir=str(link))

    monkeypatch.setattr(cc.tempfile, "mkdtemp", fake_mkdtemp)

    captured = {}

    def fake_spawn_lane(argv, cwd, env, stdin_bytes, timeout):
        captured["argv"] = argv
        return 0, False, ok_stream()

    monkeypatch.setattr(lane_mod, "spawn_lane", fake_spawn_lane)
    orch = fake_orch("/opt/homebrew/bin/codex")
    tail = cc.tester_argv_tail(orch)
    profile = tmp_path / "profile.sb"
    profile.write_text("(version 1)\n")
    cc._run_startup("/bin/true", tail, profile, lane_mod, orch, tmp_path / "home", "tester")
    lane_arg = next(a for a in captured["argv"] if a.startswith("LANE="))
    assert "linked-tmp" not in lane_arg[len("LANE="):]


@needs_sandbox_exec
def test_a_fake_codex_passes_startup_under_a_permissive_profile(cc, lane_mod, tmp_path):
    """A profile that takes LANE/CODEX_HOME, grants the auth.json write, and
    otherwise allows default — real `sandbox-exec`, a real confined spawn,
    just not denying anything the fake script needs."""
    script = write_fake_codex(tmp_path / "cand", help_text=ALL_FLAGS_HELP, stream=ok_stream())
    profile = tmp_path / "permissive.sb"
    profile.write_text(
        '(version 1)\n(allow default)\n'
        '(allow file-read* (subpath (param "LANE")))\n'
        '(allow file-write* (literal (string-append (param "CODEX_HOME") "/auth.json")))\n')
    orch = fake_orch(str(script))
    tail = cc.tester_argv_tail(orch)
    result = cc._run_startup(str(script), tail, profile, lane_mod, orch,
                             tmp_path / "codex-home", "tester")
    assert result["status"] == "pass", result


# --------------------------------------------------------------------- step 4


def test_lane_fails_when_no_files_are_returned(cc, lane_mod, tmp_path, monkeypatch):
    # `run_lane_step` does `git archive <sha>` in `cc.ROOT`. A real run points
    # that at the owner's full eunomia clone, but a CI checkout is shallow
    # (`actions/checkout@v4` default `fetch-depth: 1`) and would not have any
    # commit but the one just pushed, so this test gives it its own tiny repo
    # and sha instead of reaching into the real one's history.
    repo = tmp_path / "canary-src"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    (repo / "plans").mkdir()
    (repo / "plans" / "0202-every-catalog-model-has-a-runner.md").write_text("# a plan\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "canary fixture"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    cc.ROOT = repo

    cfg = tmp_path / "cli-canary.json"
    cfg.write_text(json.dumps({
        "plan": "0202-every-catalog-model-has-a-runner",
        "sha": sha,
        "baseline": {"input_tokens": 46623, "output_tokens": 898, "test_files": 1}}))
    cc.CLI_CANARY_CONFIG = cfg
    script = write_fake_codex(tmp_path / "cand", doc={"files": [], "gaps": ["nope"], "notes": ""})
    # no sandbox-exec wrapper here: this test is about the "no files" outcome,
    # not about real confinement, which needs a profile that admits tmp_path
    unwrapped_argv = (str(script), "exec", "-s", "read-only", "--skip-git-repo-check",
                      "--ignore-user-config", "--ephemeral", "--ignore-rules", "--json",
                      "-c", 'sqlite_home="<lane>"', "--output-schema", "<schema>",
                      "-o", "<out>", "-m", "gpt-6-astra")
    orch = fake_orch(str(script), argv=unwrapped_argv)
    plan_id, sha_cfg, baseline = cc._lane_config()
    result = cc.run_lane_step(plan_id, sha_cfg, baseline, str(script), orch, lane_mod)
    assert result["status"] == "fail"
    assert "no test file" in result["detail"]


def test_git_archive_failure_is_a_lane_fail_not_a_stop(cc, lane_mod):
    """A bad sha is a failure of step 4's own work, not a usage error about
    this program — reported and paged like any other step fail, never a
    `Stop` that discards the already-spent version/flags/startup steps."""
    orch = fake_orch("/opt/homebrew/bin/codex")
    result = cc.run_lane_step("0202-every-catalog-model-has-a-runner",
                              "0" * 40, {"input_tokens": 1}, "/bin/true", orch, lane_mod)
    assert result["status"] == "fail"
    assert "git archive" in result["detail"]


def test_lane_config_missing_is_a_pre_flight_stop(cc, tmp_path, lane_mod):
    cc.CLI_CANARY_CONFIG = tmp_path / "does-not-exist.json"
    orch = fake_orch("/opt/homebrew/bin/codex")
    with pytest.raises(cc.Stop, match="unreadable"):
        cc.run_codex_canary("/bin/true", False, orch_mod=orch, lane_mod=lane_mod,
                            lib_mod=_fake_lib(tmp_path))
    assert not (tmp_path / "work").exists()


# --------------------------------------------------------------- confinement


def _fake_lib(tmp_path):
    mod = type(sys)("fake_fleetlib_for_canary")
    mod.work_root = lambda: tmp_path / "work"
    return mod


def test_startup_only_never_touches_codex_bin(cc, tmp_path, lane_mod, monkeypatch):
    """D5's "`/opt/homebrew/bin/codex` is never touched" guarantee, exercised
    with CODEX_BIN pointed at a symlink: the link's target is read, by
    `--version`, and never rewritten."""
    target = write_fake_codex(tmp_path / "real-linked", version="codex-cli 0.153.4",
                              help_text=ALL_FLAGS_HELP, stream=ok_stream())
    link = tmp_path / "codex-bin-symlink"
    link.symlink_to(target)
    before = os.readlink(link)
    cand = write_fake_codex(tmp_path / "cand", version="codex-cli 0.166.0",
                            help_text=ALL_FLAGS_HELP, stream=ok_stream())
    orch = fake_orch(str(link))
    monkeypatch.setattr(cc, "run_startup_steps", lambda *a, **k: dict(_PASS_STARTUP))
    rc = cc.run_codex_canary(str(cand), True, orch_mod=orch, lane_mod=lane_mod,
                             lib_mod=_fake_lib(tmp_path))
    assert os.readlink(link) == before
    assert os.path.realpath(link) == os.path.realpath(target)


def test_a_pass_still_names_a_skipped_reviewer_probe(cc, tmp_path, lane_mod, monkeypatch):
    """A clean pass must not read as "every check ran" when the
    reviewer-profile startup probe was `skipped`, not run."""
    linked = write_fake_codex(tmp_path / "linked", version="codex-cli 0.153.4",
                              help_text=ALL_FLAGS_HELP, stream=ok_stream())
    orch = fake_orch(str(linked))
    monkeypatch.setattr(cc, "run_startup_steps", lambda *a, **k: dict(_PASS_STARTUP))
    rc = cc.run_codex_canary(str(linked), True, orch_mod=orch, lane_mod=lane_mod,
                             lib_mod=_fake_lib(tmp_path))
    assert rc == 0
    assert len(orch.notify_calls) == 1
    _title, body = orch.notify_calls[0]
    assert "startup:reviewer" in body


@needs_sandbox_exec
def test_canary_writes_only_the_report_under_work_root(cc, tmp_path, lane_mod, monkeypatch):
    """Everything else it creates is a `mkdtemp` it also removes."""
    created = []
    import tempfile as _tempfile
    real_mkdtemp = _tempfile.mkdtemp

    def tracking_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        created.append(d)
        return d

    monkeypatch.setattr(cc.tempfile, "mkdtemp", tracking_mkdtemp)
    profile = tmp_path / "permissive.sb"
    profile.write_text(
        '(version 1)\n(allow default)\n'
        '(allow file-read* (subpath (param "LANE")))\n'
        '(allow file-write* (literal (string-append (param "CODEX_HOME") "/auth.json")))\n')
    cc.CODEX_TESTER_PROFILE = profile
    cc.REVIEWER_PROFILE_PATH = str(tmp_path / "no-reviewer-profile-here.sb")
    cand = write_fake_codex(tmp_path / "cand", help_text=ALL_FLAGS_HELP, stream=ok_stream())
    orch = fake_orch(str(cand) + "-linked-does-not-exist")
    rc = cc.run_codex_canary(str(cand), True, orch_mod=orch, lane_mod=lane_mod,
                             lib_mod=_fake_lib(tmp_path))
    report = json.loads(next((tmp_path / "work").rglob("*.json")).read_text())
    assert rc == 0, report
    for d in created:
        assert not os.path.exists(d), f"{d} was not cleaned up"
    work_files = sorted(p for p in (tmp_path / "work").rglob("*") if p.is_file())
    assert len(work_files) == 1
    assert work_files[0].suffix == ".json"
