"""Plan 0192: what the Gemini writer (`sub-antigravity`'s `writer_argv`) is and
is not granted, and why.

Measured live against the installed CLI on a disposable repository (CLI
1.5.0, 2026-11-04), not inferred from its docs:

* With NO `permissions.allow` entry, headless mode auto-denies `run_command`
  cleanly, before it spawns anything — the same shape twice recorded in
  production (the 0179 and 0184 writer leases). `tests/test_writer_runners.py`
  covers the orchestrator's side of that (`read_antigravity_denied_actions`,
  the Stop raised before publication).
* With ONE `permissions.allow` entry granting a single command, a TRAILING
  argument on that same invocation is not constrained by the grant: `git
  status`, granted, still ran `git status --porcelain` unchanged. For a
  generic binary — `cat`, `touch` were tried — the grant is not "run this
  command", it is "read or write anything this uid can reach": `cat
  /etc/passwd` and `touch /tmp/<anything>` both succeeded under antigravity's
  own `--sandbox` flag, which confines `write_file`/`view_file` (measured
  2026-11-02, noted on the Runner below) but not `run_command`'s own
  subprocess.
* Confining that subprocess with an outer `sandbox-exec` profile was tried
  next and found infeasible on this host: `run_command` depends on
  `job-creation` (it execs through a PTY-and-cert-secured broker, not a plain
  fork+exec), and a job created through `job-creation` runs OUTSIDE whatever
  profile wraps the parent — the exact reason `config/codex-tester.sb` and
  `config/meeting-runner.sb` already deny it unconditionally, for a different
  CLI. Allowing it back to let the broker start defeats the wrapper for
  anything that broker then runs; denying it, `run_command` never starts at
  all (`failed to create PTY: operation not permitted`), measured with every
  other read/write path it needs already granted.

So: the measured conclusion is the Boundary's second branch, not its first —
refuse the grant, rather than claim a process-boundary confinement that does
not hold. The writer still edits normally (`write_to_file`,
`replace_file_content`, `view_file`, `list_dir`, `grep_search` need no
`command` grant at all) and cannot run its own tests headlessly; `ANTIGRAVITY_WRITER_NOTE`
(bin/orchestrator) tells it so, so it does not spend a turn finding out."""
import importlib.machinery
import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(mod_name):
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_the_gemini_writer_grants_itself_no_command_permission():
    orch = _load("orchestrator_gemini_perms")
    argv = orch.RUNNERS["sub-antigravity"].writer_argv
    assert "--settings" not in argv
    # a settings path would also be a readable argv entry; a permissions grant
    # embedded as JSON would contain this substring regardless of which flag
    # carried it, so the check does not depend on `--settings` being the only
    # way one could be smuggled in
    assert not any("permissions" in a or "command(" in a for a in argv)


def test_the_gemini_writer_never_skips_its_own_permission_prompts():
    orch = _load("orchestrator_gemini_perms")
    argv = orch.RUNNERS["sub-antigravity"].writer_argv
    assert "--dangerously-skip-permissions" not in argv


def test_the_denial_classification_only_exists_for_antigravity():
    # codex and muse have no `denied_actions` concept in their own transcripts
    # (ADR-0006 §6: each CLI's confinement was measured, not assumed to be the
    # same shape) — a reader mapped to them would silently return empty
    # forever, which is indistinguishable from "never checked". Absent, not
    # mapped-to-a-no-op, is the fleet's existing convention here (see
    # WRITER_RESULT_READERS.get("claude") in tests/test_writer_runners.py).
    orch = _load("orchestrator_gemini_perms")
    assert set(orch.WRITER_DENIAL_READERS) == {"google"}


def test_antigravitys_own_sandbox_flag_is_still_the_write_confinement():
    # Boundary: "Never use ... disabled sandboxing" — `--sandbox` is the one
    # flag that confines `write_to_file`/`view_file` to the worktree (measured
    # 2026-11-02, see the comment on RUNNERS["sub-antigravity"]), and nothing
    # in this plan's "grant nothing for run_command" decision may be read as a
    # reason to drop it.
    orch = _load("orchestrator_gemini_perms")
    assert "--sandbox" in orch.RUNNERS["sub-antigravity"].writer_argv
    assert "accept-edits" in orch.RUNNERS["sub-antigravity"].writer_argv
