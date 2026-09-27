"""Tests for bin/fleetjob.py — the launchd/systemd dispatch seam (plan 0053).

`bin/fleet-watch` no longer writes a plist or calls `launchctl` itself; both
live here, behind `start`/`stop`/`list_labels`/`job_environment`, chosen by
`select_impl()` rather than hardcoded. Every test stubs the supervisor
binary — `FLEET_LAUNCHCTL`/`FLEET_SYSTEMCTL`/`FLEET_SYSTEMD_RUN`, or the
`_run_*` seams directly — the same way `tests/test_fleet_svc.py` stubs
`FLEET_LAUNCHCTL`/`FLEET_SYSTEMCTL`: no test here may reach a real supervisor
domain.

Run: python3 -m pytest tests/test_dispatch_launcher.py -q
"""
import importlib.machinery
import importlib.util
import os
import plistlib
import re

import pytest

BIN = __import__("pathlib").Path(__file__).resolve().parent.parent / "bin"


def _load():
    """A fresh module object per call — no shared state between tests, and no
    need to reset FLEET_DISPATCH_IMPL/JOB_DIR between them."""
    loader = importlib.machinery.SourceFileLoader(
        "fleetjob_under_test", str(BIN / "fleetjob.py"))
    spec = importlib.util.spec_from_loader("fleetjob_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _stub_bin(tmp_path, name):
    """An executable that always exits 0 — enough for `shutil.which` to find
    it regardless of what the test host actually has installed."""
    p = tmp_path / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    return str(p)


def _stub_all(mod, monkeypatch, rc=0, out=""):
    """Stub every supervisor seam to the same recording responder. Which one a
    given call actually reaches depends on impl, so tests that do not care
    which binary was invoked can use this rather than three separate stubs."""
    calls = []

    def responder(*a, **k):
        calls.append(a)
        return (rc, out)

    monkeypatch.setattr(mod, "_run_launchctl", responder)
    monkeypatch.setattr(mod, "_run_systemctl", responder)
    monkeypatch.setattr(mod, "_run_systemd_run", responder)
    return calls


# ------------------------------------------------------------- select_impl

def test_select_impl_prefers_launchd_when_both_present(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("FLEET_LAUNCHCTL", _stub_bin(tmp_path, "launchctl-stub"))
    monkeypatch.setenv("FLEET_SYSTEMCTL", _stub_bin(tmp_path, "systemctl-stub"))
    monkeypatch.delenv("FLEET_DISPATCH_IMPL", raising=False)
    assert mod.select_impl() == "launchd"


def test_select_impl_falls_back_to_systemd(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("FLEET_LAUNCHCTL", str(tmp_path / "no-such-launchctl"))
    monkeypatch.setenv("FLEET_SYSTEMCTL", _stub_bin(tmp_path, "systemctl-stub"))
    monkeypatch.delenv("FLEET_DISPATCH_IMPL", raising=False)
    assert mod.select_impl() == "systemd"


def test_select_impl_refuses_when_neither_present(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("FLEET_LAUNCHCTL", str(tmp_path / "nope-1"))
    monkeypatch.setenv("FLEET_SYSTEMCTL", str(tmp_path / "nope-2"))
    monkeypatch.delenv("FLEET_DISPATCH_IMPL", raising=False)
    with pytest.raises(OSError):
        mod.select_impl()


def test_select_impl_forced_by_env_var():
    mod = _load()
    assert mod.select_impl({"FLEET_DISPATCH_IMPL": "systemd"}) == "systemd"
    assert mod.select_impl({"FLEET_DISPATCH_IMPL": " Launchd "}) == "launchd"


def test_select_impl_forced_rejects_garbage():
    mod = _load()
    with pytest.raises(ValueError):
        mod.select_impl({"FLEET_DISPATCH_IMPL": "daemonize"})


# --------------------------------------------------------- start/stop/sweep,
# the same shape proven against both implementations (plan 0053 D5)

@pytest.mark.parametrize("impl", ["launchd", "systemd"])
def test_start_creates_a_record_this_module_can_read_back(tmp_path, monkeypatch, impl):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    _stub_all(mod, monkeypatch)
    label = "org.eunomia.fleet-orch.branch--x--001"
    mod.start(label, ["/bin/echo", "hi"], {"FLEET_X": "1"}, impl=impl)
    assert mod.job_environment(label) == {"FLEET_X": "1"}
    assert mod.list_labels(prefix="org.eunomia.fleet-orch.") == [label]


@pytest.mark.parametrize("impl", ["launchd", "systemd"])
def test_a_refusing_supervisor_raises_oserror(tmp_path, monkeypatch, impl):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    _stub_all(mod, monkeypatch, rc=1, out="boom")
    with pytest.raises(OSError):
        mod.start("org.eunomia.fleet-orch.branch--x--001", ["/bin/echo"], {},
                  impl=impl)


@pytest.mark.parametrize("impl", ["launchd", "systemd"])
def test_stop_never_raises_even_when_the_supervisor_refuses(tmp_path, monkeypatch, impl):
    """A supervisor that will not answer must degrade to a refusal, not a
    traceback inside a reap or a cancel."""
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    _stub_all(mod, monkeypatch)
    label = "org.eunomia.fleet-orch.branch--x--001"
    mod.start(label, ["/bin/echo"], {}, impl=impl)
    _stub_all(mod, monkeypatch, rc=1, out="not found")
    rc, out = mod.stop(label)
    assert rc == 1 and "not found" in out


@pytest.mark.parametrize("impl", ["launchd", "systemd"])
def test_sweep_shape_start_then_stop_then_remove(tmp_path, monkeypatch, impl):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    _stub_all(mod, monkeypatch)
    label = "org.eunomia.fleet-orch.branch--x--001"
    mod.start(label, ["/bin/echo"], {}, impl=impl)
    assert mod.list_labels() == [label]
    mod.stop(label)
    mod.remove_record(label)
    assert mod.list_labels() == []


def test_stop_falls_back_to_select_impl_when_no_record_exists(tmp_path, monkeypatch):
    """A cancel that arrives with no record on disk (already reaped, or never
    started) still has to pick a supervisor to ask."""
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    monkeypatch.setenv("FLEET_DISPATCH_IMPL", "systemd")
    calls = []
    monkeypatch.setattr(mod, "_run_systemctl",
                        lambda *a, **k: (calls.append(a), (0, ""))[1])
    mod.stop("org.eunomia.fleet-orch.branch--ghost--001")
    assert calls == [("--user", "stop",
                      "org.eunomia.fleet-orch.branch--ghost--001.service")]


# ---------------------------------------------------------------- launchd

def test_launchd_start_boots_out_before_it_bootstraps(tmp_path, monkeypatch):
    """`launchctl bootstrap` accepts an arbitrary plist at a path, so
    re-bootstrapping over a LIVE label is the hazard bootout-first avoids —
    see the comment beside `_start_launchd` in fleetjob.py."""
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    calls = []
    monkeypatch.setattr(mod, "_run_launchctl",
                        lambda *a, **k: (calls.append(a), (0, ""))[1])
    mod.start("org.eunomia.fleet-orch.branch--x--001", ["/bin/echo"], {},
             impl="launchd")
    verbs = [c[0] for c in calls]
    assert verbs.index("bootout") < verbs.index("bootstrap")


def test_launchd_stop_uses_the_gui_domain(tmp_path, monkeypatch):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    monkeypatch.setattr(mod, "_run_launchctl", lambda *a, **k: (0, ""))
    mod.start("org.eunomia.fleet-orch.branch--x--001", ["/bin/echo"], {},
             impl="launchd")
    calls = []
    monkeypatch.setattr(mod, "_run_launchctl",
                        lambda *a, **k: (calls.append(a), (0, ""))[1])
    mod.stop("org.eunomia.fleet-orch.branch--x--001")
    assert calls == [("bootout",
                      f"gui/{os.getuid()}/org.eunomia.fleet-orch.branch--x--001")]


def test_launchd_plist_is_a_one_shot_and_not_keepalive(tmp_path, monkeypatch):
    """KeepAlive would restart a finished orchestrator — re-running a
    completed plan against a lease that is already released."""
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    monkeypatch.setattr(mod, "_run_launchctl", lambda *a, **k: (0, ""))
    path = mod.start("org.eunomia.fleet-orch.branch--x--001",
                     ["/bin/echo", "hi"], {"FLEET_X": "1"}, impl="launchd")
    body = plistlib.loads(path.read_bytes())
    assert body["Label"] == "org.eunomia.fleet-orch.branch--x--001"
    assert body["ProgramArguments"] == ["/bin/echo", "hi"]
    assert body["RunAtLoad"] is True
    assert "KeepAlive" not in body
    assert body["EnvironmentVariables"] == {"FLEET_X": "1"}
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_launchd_plist_carries_no_log_path_unless_given(tmp_path, monkeypatch):
    """D2: the launchd implementation is the current behaviour MOVED, not
    rewritten — same plist contents. fleet-watch's own dispatch call passes
    no log_path, so the plist it writes must be byte-for-byte what it always
    was; StandardOutPath/StandardErrorPath are opt-in, not ambient."""
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    monkeypatch.setattr(mod, "_run_launchctl", lambda *a, **k: (0, ""))
    path = mod.start("org.eunomia.fleet-orch.branch--x--001", ["/bin/echo"], {},
                     impl="launchd")
    body = plistlib.loads(path.read_bytes())
    assert "StandardOutPath" not in body
    assert "StandardErrorPath" not in body


def test_launchd_log_path_is_opt_in(tmp_path, monkeypatch):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    monkeypatch.setattr(mod, "_run_launchctl", lambda *a, **k: (0, ""))
    path = mod.start("org.eunomia.fleet-orch.branch--x--001", ["/bin/echo"], {},
                     impl="launchd", log_path="/tmp/x.log")
    body = plistlib.loads(path.read_bytes())
    assert body["StandardOutPath"] == "/tmp/x.log"
    assert body["StandardErrorPath"] == "/tmp/x.log"


def test_run_launchctl_never_raises_into_a_dispatch(tmp_path, monkeypatch):
    """A supervisor that is not answering must degrade to a refusal, not a
    traceback inside spawn()."""
    mod = _load()

    def boom(*a, **k):
        raise OSError("launchctl is gone")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    rc, out = mod._run_launchctl("bootstrap", "gui/501", "/tmp/x.plist")
    assert rc == -1 and "OSError" in out


def test_job_dir_is_outside_launchagents_and_outside_the_fleet_tree():
    """Two separate hazards. ~/Library/LaunchAgents is auto-loaded at login,
    so a plist left by a reboot mid-dispatch would resurrect a stale
    orchestrator against a lease it no longer holds. ~/dev/.fleet/ is
    rendered by atlas and principle 6 forbids secret material there — a job
    record carrying an environment is exactly the shape of thing that
    acquires one later."""
    mod = _load()
    d = str(mod.JOB_DIR)
    assert "LaunchAgents" not in d, d
    assert "/.fleet" not in d, d


# ---------------------------------------------------------------- systemd

def test_systemd_start_invokes_a_user_transient_unit(tmp_path, monkeypatch):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    calls = []
    monkeypatch.setattr(mod, "_run_systemd_run",
                        lambda *a, **k: (calls.append(a), (0, ""))[1])
    mod.start("org.eunomia.fleet-orch.branch--x--001", ["/bin/echo", "hi"],
             {"FLEET_X": "1"}, impl="systemd")
    (argv,) = calls
    assert "--user" in argv
    assert "--unit=org.eunomia.fleet-orch.branch--x--001" in argv
    assert "--setenv=FLEET_X=1" in argv
    assert argv[-2:] == ("/bin/echo", "hi")


def test_systemd_log_path_sets_append_properties(tmp_path, monkeypatch):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    calls = []
    monkeypatch.setattr(mod, "_run_systemd_run",
                        lambda *a, **k: (calls.append(a), (0, ""))[1])
    mod.start("org.eunomia.fleet-orch.branch--x--001", ["/bin/echo"], {},
             impl="systemd", log_path="/tmp/x.log")
    (argv,) = calls
    assert "--property=StandardOutput=append:/tmp/x.log" in argv
    assert "--property=StandardError=append:/tmp/x.log" in argv


def test_systemd_stop_appends_the_service_suffix(tmp_path, monkeypatch):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    monkeypatch.setattr(mod, "_run_systemd_run", lambda *a, **k: (0, ""))
    mod.start("org.eunomia.fleet-orch.branch--x--001", ["/bin/echo"], {},
             impl="systemd")
    calls = []
    monkeypatch.setattr(mod, "_run_systemctl",
                        lambda *a, **k: (calls.append(a), (0, ""))[1])
    mod.stop("org.eunomia.fleet-orch.branch--x--001")
    assert calls == [("--user", "stop",
                      "org.eunomia.fleet-orch.branch--x--001.service")]


def test_run_systemctl_never_raises_into_a_dispatch(tmp_path, monkeypatch):
    mod = _load()

    def boom(*a, **k):
        raise OSError("systemctl is gone")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    rc, out = mod._run_systemctl("--user", "stop", "x.service")
    assert rc == -1 and "OSError" in out


def test_the_label_alphabet_needs_no_systemd_remapping():
    """fleetjob.py's module docstring claims job_label()'s output already fits
    systemd's legal unit-name alphabet, so no remapping code exists. Assert
    the claim against a real label rather than leaving it only asserted in
    prose."""
    label = "org.eunomia.fleet-orch.branch--feat-0002-ci-logs--002"
    assert re.fullmatch(r"[A-Za-z0-9:_.-]+", label)


# --------------------------------------------------------- record bookkeeping

def test_list_labels_matches_prefix_across_both_suffixes(tmp_path, monkeypatch):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    _stub_all(mod, monkeypatch)
    mod.start("org.eunomia.fleet-orch.branch--a--001", ["/bin/echo"], {},
             impl="launchd")
    mod.start("org.eunomia.fleet-orch.branch--b--002", ["/bin/echo"], {},
             impl="systemd")
    mod.start("com.other.thing", ["/bin/echo"], {}, impl="launchd")
    assert sorted(mod.list_labels(prefix="org.eunomia.fleet-orch.")) == [
        "org.eunomia.fleet-orch.branch--a--001",
        "org.eunomia.fleet-orch.branch--b--002",
    ]


def test_job_environment_returns_none_for_an_unreadable_record(tmp_path):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    mod.JOB_DIR.mkdir(parents=True, exist_ok=True)
    (mod.JOB_DIR / "org.eunomia.fleet-orch.branch--junk--001.plist").write_text(
        "this is not a plist")
    assert mod.job_environment("org.eunomia.fleet-orch.branch--junk--001") is None


def test_job_environment_returns_none_for_a_missing_label(tmp_path):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    assert mod.job_environment("org.eunomia.fleet-orch.branch--ghost--001") is None


def test_remove_record_of_a_missing_label_does_not_raise(tmp_path):
    mod = _load()
    mod.JOB_DIR = tmp_path / "jobs"
    mod.remove_record("org.eunomia.fleet-orch.branch--ghost--001")
