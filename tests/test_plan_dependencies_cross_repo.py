"""Plan 0159: `path:<owner/repo>:<path>` and `owner:<script>` in `depends_on`.
The forge and the hosttools pin are stubbed."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

import test_fleet_watch as tw

@pytest.fixture
def watch(tmp_path, monkeypatch):
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    monkeypatch.setattr(mod, "conf_repos",
                        lambda: (["operator/eunomia"],
                                 ["operator/eunomia", "operator/talos"]))
    pin = tmp_path / "pin"
    (pin / "bin").mkdir(parents=True)
    monkeypatch.setattr(mod, "HOSTTOOLS_PIN", pin)
    monkeypatch.setattr(mod.lib, "leases_dir", lambda: tmp_path / "leases")
    return mod


def _script(watch, name, body):
    f = Path(watch.HOSTTOOLS_PIN) / "bin" / name
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return f


def _block(watch, dep, plan_id="p"):
    return watch._plan_dependency_block("operator/talos", [dep], {}, "t",
                                        plan_id=plan_id)


def _paths(monkeypatch, watch, hits, status=None):
    calls = []

    def exists(repo, path, token):
        calls.append((repo, path))
        if status is not None:
            return False
        return (repo, path) in hits
    monkeypatch.setattr(watch, "_path_exists_on_main", exists)
    return calls


def test_cross_repo_path_met(watch, monkeypatch):
    _paths(monkeypatch, watch, {("operator/eunomia", "bin/fleet-style")})
    assert _block(watch, "path:operator/eunomia:bin/fleet-style") is None


def test_cross_repo_path_unmet(watch, monkeypatch):
    _paths(monkeypatch, watch, set())
    b = _block(watch, "path:operator/eunomia:bin/fleet-style")
    assert b[2] == "unmet" and "operator/eunomia@main" in b[3]


def test_cross_repo_path_5xx_unmet(watch, monkeypatch):
    _paths(monkeypatch, watch, set(), status=503)
    assert _block(watch, "path:operator/eunomia:bin/x")[2] == "unmet"


def test_cross_repo_unlisted_repo_refused(watch, monkeypatch):
    calls = _paths(monkeypatch, watch, {("evil/repo", "x")})
    b = _block(watch, "path:evil/repo:x")
    assert b[2] == "unmet" and "evil/repo" in b[3] and calls == []


def test_plain_path_keeps_meaning(watch, monkeypatch):
    calls = _paths(monkeypatch, watch, {("operator/talos", "bin/x")})
    assert _block(watch, "path:bin/x") is None
    assert calls == [("operator/talos", "bin/x")]


def test_owner_exit0_met(watch):
    _script(watch, "step.sh", "exit 0\n")
    assert _block(watch, "owner:step.sh") is None


def test_owner_exit1_unmet_with_apply_page(watch):
    _script(watch, "step.sh", "exit 1\n")
    b = _block(watch, "owner:step.sh")
    assert b[1] == "owner" and b[3] == "owner step: run ~/bin/step.sh --apply"


def test_owner_exit2_unmet(watch):
    _script(watch, "step.sh", "exit 2\n")
    b = _block(watch, "owner:step.sh")
    assert b[2] == "unmet" and "exited 2" in b[3]


def test_owner_timeout_unmet(watch, monkeypatch):
    monkeypatch.setattr(watch, "OWNER_CHECK_TIMEOUT", 0.2)
    _script(watch, "step.sh", "sleep 5\n")
    assert "timed out" in _block(watch, "owner:step.sh")[3]


def test_owner_missing_script_and_pin(watch, tmp_path, monkeypatch):
    assert "no script" in _block(watch, "owner:nope.sh")[3]
    monkeypatch.setattr(watch, "HOSTTOOLS_PIN", tmp_path / "absent")
    assert "pin missing" in _block(watch, "owner:nope.sh")[3]


@pytest.mark.parametrize("name", ["a/b.sh", "../x.sh", "a b.sh", "a;b.sh",
                                  "$(id).sh", "A.sh", "noext", "-x.sh"])
def test_owner_bad_name_refused_before_running(watch, monkeypatch, name):
    ran = []
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: ran.append(a))
    b = _block(watch, f"owner:{name}")
    assert b[2] == "refused" and ran == []


def test_owner_symlink_out_of_bin_refused(watch, tmp_path):
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\nexit 0\n")
    outside.chmod(0o755)
    os.symlink(outside, Path(watch.HOSTTOOLS_PIN) / "bin" / "link.sh")
    assert _block(watch, "owner:link.sh")[2] == "refused"


def test_owner_env_and_argv(watch, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    out = tmp_path / "seen"
    _script(watch, "step.sh",
            f'echo "$#:$1" > {out}; env | cut -d= -f1 | sort >> {out}\nexit 0\n')
    assert _block(watch, "owner:step.sh") is None
    lines = out.read_text().split()
    assert lines[0] == "1:--check"
    names = set(lines[1:]) - {"PWD", "SHLVL", "_", "OLDPWD"}
    assert names <= {"PATH", "HOME", "LANG"} and "SECRET_TOKEN" not in names


def test_owner_met_not_rerun(watch, tmp_path):
    marker = tmp_path / "count"
    _script(watch, "step.sh", f"echo x >> {marker}\nexit 0\n")
    assert _block(watch, "owner:step.sh") is None
    assert _block(watch, "owner:step.sh") is None
    assert len(marker.read_text().split()) == 1
    watch._clear_blocked("operator/talos", "p")
    assert _block(watch, "owner:step.sh") is None
    assert len(marker.read_text().split()) == 2
