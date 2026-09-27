"""fleetjob.py — start, stop, and enumerate one-shot dispatch jobs.

`bin/fleet-watch` starts each orchestrator run as a supervised, labelled,
one-shot job rather than a bare child process: an orphaned `Popen` cannot open
a LAN connection on macOS once the watcher that spawned it exits (see
`spawn_mode`'s own docstring in fleet-watch — the reasoning is unchanged by
this module, only where the code that acts on it lives). This module is the
seam between that decision and the two supervisors that can carry it out —
launchd (macOS) and systemd (Linux, transient `--user` units) — chosen by
what the HOST has (`select_impl`), never hardcoded (plan 0053 D4).

NOT `import fleetjob` from bin/fleet-watch (plan 0053 boundary, the same
reason plan 0051 gives for fleetforge): fleet-watch is `SourceFileLoader`'d
from directories that are not on `sys.path` and do not contain the rest of
`bin/`, so a companion file missing there must fail by NAME — see
`tests/test_forge_call_sites.py`, which declares this file part of the
companion set — not with an opaque `ModuleNotFoundError`.

Public surface:
  select_impl(env=None)                     -> "launchd" | "systemd"
  start(label, argv, env, *,
        log_path=None, impl=None)           -> Path (the job record written)
  stop(label)                               -> (rc, output), never raises
  list_labels(prefix="")                    -> [label, ...] this JOB_DIR holds
  job_environment(label)                    -> dict | None
  remove_record(label)                      -> None

Env:
  FLEET_LAUNCHCTL      override the launchctl CLI (default "launchctl") —
                        same name and shape as bin/fleet-svc's own override,
                        stubbed the same way in tests.
  FLEET_SYSTEMCTL       override the systemctl CLI (default "systemctl")
  FLEET_SYSTEMD_RUN     override the systemd-run CLI (default "systemd-run")
  FLEET_DISPATCH_IMPL   force "launchd" or "systemd" rather than probing the
                        host — tests, and a host where both binaries happen
                        to be on PATH.

Label mapping: a label is passed to systemd verbatim as `--unit=<label>`,
which systemd appends `.service` to when the name carries no recognised
suffix. `org.eunomia.fleet-orch.<lease-id>` (fleet-watch's `job_label`) uses
only characters systemd unit names already allow — letters, digits, `-`,
`.`, `:` — so no remapping exists: the same string names the job under
either supervisor. A label that could not survive that trip would have to
fail in `job_label` itself, which this module never touches (plan 0053
boundary: do not change the label scheme).

Job records live in JOB_DIR, one file per label, `.plist` under launchd and
`.json` under systemd — the suffix is how every other function here tells
which supervisor a label belongs to, without asking the supervisor itself.
Ownership and "is this dispatch over" are read from that file, never from
the supervisor: the supervisor's own listing bears no attribution back to
the fleet dir that started the job, and — for a launchd job — RUNNING is not
the same question as "does the lease that started it still exist" (see
fleet-watch's `reap_jobs`, the caller of `list_labels`/`job_environment`
here).
"""
import json
import os
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path

# Where per-dispatch job records live. DELIBERATELY NOT ~/Library/LaunchAgents:
# launchd auto-loads that directory at login, so a plist left behind by a
# reboot mid-dispatch would resurrect a stale orchestrator against a lease it
# no longer holds. `launchctl bootstrap` accepts an arbitrary path, so nothing
# is lost by keeping job records out of it.
#
# Also NOT under ~/dev/.fleet/: SPEC principle 6 forbids secret material in
# that tree because atlas renders it, and a job record carrying an
# environment is exactly the shape of thing that acquires a secret later.
JOB_DIR = Path.home() / "Library" / "Application Support" / "eunomia" / "jobs"


def _launchctl_bin():
    return shlex.split(os.environ.get("FLEET_LAUNCHCTL", "launchctl"))


def _systemctl_bin():
    return shlex.split(os.environ.get("FLEET_SYSTEMCTL", "systemctl"))


def _systemd_run_bin():
    return shlex.split(os.environ.get("FLEET_SYSTEMD_RUN", "systemd-run"))


def _domain():
    return f"gui/{os.getuid()}"


def _run(binary_argv, *args, timeout=30):
    """(rc, output). Never raises: a supervisor that is not answering must
    degrade to a refusal, not a traceback inside a dispatch or a reap."""
    try:
        r = subprocess.run(list(binary_argv) + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, f"{e.__class__.__name__}: {e}"


def _run_launchctl(*args, timeout=30):
    return _run(_launchctl_bin(), *args, timeout=timeout)


def _run_systemctl(*args, timeout=30):
    return _run(_systemctl_bin(), *args, timeout=timeout)


def _run_systemd_run(*args, timeout=30):
    return _run(_systemd_run_bin(), *args, timeout=timeout)


def select_impl(env=None):
    """launchd on a host that has it, systemd otherwise — "what the host
    has" (plan 0053 D4), not a knob an operator sets per dispatch. Checks
    launchctl before systemctl so a host carrying both (a Mac with Homebrew
    systemd, say) still gets the supervisor native to it."""
    env = os.environ if env is None else env
    forced = (env.get("FLEET_DISPATCH_IMPL") or "").strip().lower()
    if forced:
        if forced not in ("launchd", "systemd"):
            raise ValueError(
                f"FLEET_DISPATCH_IMPL={forced!r} is not 'launchd' or 'systemd'")
        return forced
    if shutil.which(_launchctl_bin()[0]):
        return "launchd"
    if shutil.which(_systemctl_bin()[0]):
        return "systemd"
    raise OSError("fleetjob: neither launchctl nor systemctl found on PATH")


def _record_path(label):
    for suffix in (".plist", ".json"):
        path = JOB_DIR / f"{label}{suffix}"
        if path.exists():
            return path
    return None


def start(label, argv, env, *, log_path=None, impl=None):
    """Start LABEL as a one-shot job running ARGV with ENV. Raises OSError on
    any failure — writing the record, or the supervisor refusing it — so the
    caller decides what a failed dispatch means (fleet-watch releases the
    lease and pages; see its own `spawn`)."""
    impl = impl or select_impl()
    if impl == "launchd":
        return _start_launchd(label, argv, env, log_path)
    if impl == "systemd":
        return _start_systemd(label, argv, env, log_path)
    raise ValueError(f"fleetjob: unknown impl {impl!r} (expected launchd|systemd)")


def _start_launchd(label, argv, env, log_path):
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    path = JOB_DIR / f"{label}.plist"
    body = {
        "Label": label,
        "ProgramArguments": list(argv),
        "EnvironmentVariables": dict(env),
        "RunAtLoad": True,
        # No KeepAlive: one dispatch, one run. launchd keeps the job listed
        # with its exit status after it finishes, which is what `list_labels`
        # / `job_environment` read afterward.
        "ProcessType": "Standard",
    }
    if log_path is not None:
        body["StandardOutPath"] = str(log_path)
        body["StandardErrorPath"] = str(log_path)
    try:
        with open(path, "wb") as fh:
            plistlib.dump(body, fh)
        os.chmod(path, 0o600)
    except (OSError, ValueError, TypeError) as e:
        raise OSError(f"could not write job definition for {label}: "
                      f"{e.__class__.__name__}") from e
    # Boot out any same-label remnant FIRST. A one-shot job stays LISTED after
    # it exits, and bootstrapping over a listed label fails with "service
    # already loaded" — which would read as a spawn failure for a label that
    # has simply been used before. This order is load-bearing, not
    # incidental: `launchctl bootstrap` accepts an arbitrary plist at a path,
    # so re-bootstrapping over a LIVE label is the hazard bootout-first
    # avoids.
    _run_launchctl("bootout", f"{_domain()}/{label}")
    rc, out = _run_launchctl("bootstrap", _domain(), str(path))
    if rc != 0:
        raise OSError(f"launchctl bootstrap rc={rc}: {out[:120]}")
    return path


def _start_systemd(label, argv, env, log_path):
    """A transient `--user` unit. The record is written BEFORE `systemd-run`
    runs, mirroring the launchd order above: a failed launch then leaves an
    orphaned record rather than a running-but-unrecorded job, and the orphan
    self-heals the same way an orphaned plist does — the next reap finds its
    lease gone and removes it."""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    path = JOB_DIR / f"{label}.json"
    try:
        with open(path, "w") as fh:
            json.dump({"label": label, "unit": f"{label}.service",
                      "env": dict(env)}, fh)
        os.chmod(path, 0o600)
    except (OSError, ValueError, TypeError) as e:
        raise OSError(f"could not write job record for {label}: "
                      f"{e.__class__.__name__}") from e
    cmd = ["--user", "--collect", f"--unit={label}"]
    for k, v in env.items():
        cmd.append(f"--setenv={k}={v}")
    if log_path is not None:
        cmd.append(f"--property=StandardOutput=append:{log_path}")
        cmd.append(f"--property=StandardError=append:{log_path}")
    cmd.append("--")
    cmd.extend(str(a) for a in argv)
    rc, out = _run_systemd_run(*cmd)
    if rc != 0:
        raise OSError(f"systemd-run rc={rc}: {out[:120]}")
    return path


def stop(label):
    """Stop a job by label. Never raises — a supervisor that will not answer
    must degrade to a refusal, not a traceback inside a reap or a cancel.
    Reads the record to know which supervisor owns the label; falls back to
    `select_impl()` when no record is found (a cancel that arrives before or
    after the record's lifetime)."""
    path = _record_path(label)
    if path is not None:
        impl = "launchd" if path.suffix == ".plist" else "systemd"
    else:
        impl = select_impl()
    if impl == "launchd":
        return _run_launchctl("bootout", f"{_domain()}/{label}")
    return _run_systemctl("--user", "stop", f"{label}.service")


def list_labels(prefix=""):
    """Labels with a job record in JOB_DIR, matching PREFIX. The record is
    the source of truth for "does this dispatch still exist" — the caller
    asks the LEASE ledger, never the supervisor, whether it is still live
    (see fleet-watch's `reap_jobs`)."""
    try:
        entries = sorted(JOB_DIR.iterdir())
    except OSError:
        return []
    labels = []
    for path in entries:
        if path.suffix not in (".plist", ".json"):
            continue
        if path.stem.startswith(prefix):
            labels.append(path.stem)
    return labels


def job_environment(label):
    """The environment `start()` wrote for LABEL, or None if its record is
    missing or unreadable. Read back from the record file rather than asked
    of the supervisor: only the record can say who minted it."""
    path = _record_path(label)
    if path is None:
        return None
    try:
        if path.suffix == ".plist":
            with open(path, "rb") as fh:
                return plistlib.load(fh).get("EnvironmentVariables") or {}
        with open(path, "r") as fh:
            return json.load(fh).get("env") or {}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def remove_record(label):
    path = _record_path(label)
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass
