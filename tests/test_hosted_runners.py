"""Tests for the hosted open-model runners spawned through epeius (plan 0156)."""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
KEY = "sk-PLANTED0123456789abcdefghijklmnop"

# The DeepSeek and Kimi runners wait on D1's measurement of the vendors' own ids
# (their model lists need the vendor key); only runners the registry carries are
# held to the table.
HOSTED = {
    "hosted-muse-spark-open": ("https://openrouter.ai/api/v1", "routed-hosted"),
    "hosted-glm-5.3": ("https://openrouter.ai/api/v1", "routed-hosted"),
    "hosted-deepseek-v4-pro": ("https://api.deepseek.com", "cn-hosted"),
    "hosted-kimi-k3.1-pro": ("https://api.moonshot.ai/v1", "cn-hosted"),
}

PLAN = """---
id: 0001-x
status: ready
repo: operator/x
zone: public
paths:
  - "docs/*.md"
---

# Do the thing
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
    mod = _load("orchestrator", "orchestrator_hosted")
    monkeypatch.setattr(mod, "implementer_timeout_minutes", lambda repo=None: 1)
    mod.transcripts = []
    monkeypatch.setattr(mod, "write_transcript",
                        lambda lease, text, tag: (mod.transcripts.append(text) or ("t.txt", [])))
    return mod


@pytest.fixture
def wt(tmp_path):
    w = tmp_path / "wt"
    (w / "plans").mkdir(parents=True)
    (w / "docs").mkdir()
    (w / "plans" / "0001-x.md").write_text(PLAN)
    (w / "docs" / "a.md").write_text("a\n")
    return w


class Log:
    def __init__(self):
        self.events = []

    def note(self, kind, **kw):
        self.events.append((kind, kw))


CRED = types.SimpleNamespace(kind="none", principal="operator")


def _script(path, body):
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o755)
    return str(path)


def _helper(tmp_path, body=None):
    return _script(tmp_path / "helper.py", body or f"import sys\nsys.stdout.write({KEY!r})\n")


STUB = '''
import hashlib, json, os, sys
a = sys.argv
fd = int(a[a.index("--key-fd") + 1])
got = os.read(fd, 4096)
print(json.dumps({"argv": a[1:], "env": dict(os.environ), "sha": hashlib.sha256(got).hexdigest(),
                  "cwd": os.getcwd()}))
sys.exit(3)
'''


def _route(orch, tmp_path, name="hosted-muse-spark-open", helper=None, **over):
    r = orch.RUNNERS[name]
    stub = _script(tmp_path / "epeius", STUB)
    return r._replace(argv=(stub,) + r.argv[1:], vendor_key=helper or _helper(tmp_path), **over)


def _ctx(orch, plan=PLAN, zone="public"):
    orch.set_run_context("operator/x", "0001-x", "plans/0001-x.md", plan, "feat/x", zone)


def _run(orch, wt, route, log=None):
    _ctx(orch)
    return orch._run_epeius(route.argv, wt, "", CRED, route, "lease", log, None)


# ------------------------------------------------------------------ D1

@pytest.mark.parametrize("name", [n for n in HOSTED])
def test_each_registered_hosted_runner_is_declared_as_the_plan_says(orch, name):
    import re
    if name not in orch.RUNNERS:
        pytest.skip(f"{name} waits on the vendor-id measurement (plan 0156 D1)")
    endpoint, prov = HOSTED[name]
    r = orch.RUNNERS[name]
    assert r.local is False and r.needs is None and tuple(r.extra_env) == ()
    assert r.provenance == prov
    assert re.fullmatch(r"~/bin/fetch-[a-z]+-key\.sh", r.vendor_key)
    # The helper lives in the owner's ~/bin; CI has no such home, so only its shape is checked.
    # Plan 0205: GLM-5.3 alone carries the 128,000 output cap; every other hosted
    # runner keeps 16,000. Plan 0210: GLM-5.3 alone carries the 400,000 input
    # cap; every other hosted runner keeps 200,000.
    expected_output = 128000 if name == "hosted-glm-5.3" else 16000
    expected_input = 400000 if name == "hosted-glm-5.3" else 200000
    assert r.max_output_tokens == expected_output
    assert r.max_input_tokens == expected_input
    fm = _load("fleet-models", "fleet_models_hosted")
    assert fm.FULL_MODEL.fullmatch(r.model)
    assert r.argv == (orch.EPEIUS_BIN, "run", "--worktree", ".", "--task", "-",
                      "--backend", "openai:<model>", "--endpoint", endpoint,
                      "--zone", "<zone>", "--key-fd", "<fd>",
                      "--max-output-tokens", "<max_output_tokens>", "--json")


def test_muse_spark_open_is_the_dated_version(orch):
    assert orch.RUNNERS["hosted-muse-spark-open"].model == "meta/muse-spark-open-20261015"


def test_glm_5_3_is_the_dated_openrouter_slug(orch):
    r = orch.RUNNERS["hosted-glm-5.3"]
    assert r.model == "z-ai/glm-5.3-20260816"
    assert r.vendor_key == "~/bin/fetch-openrouter-key.sh"
    assert r.provenance == "routed-hosted"


def test_fleet_models_reviewers_emits_glms_128000_cap(orch, capsys):
    fm = _load("fleet-models", "fleet_models_hosted_reviewers")
    assert fm.main(["set", "operator/eunomia", "code_reviewer", "hosted-glm-5.3"]) == 0
    capsys.readouterr()
    assert fm.main(["reviewers", "operator/eunomia", "code_reviewer", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)[0]
    assert out["max_output_tokens"] == 128000
    assert out["max_input_tokens"] == 400000


def test_the_registry_is_still_classified(orch):
    assert orch.registry_errors(orch.RUNNERS) == []


# ------------------------------------------------------------------ D2

@pytest.mark.parametrize("model,ok", [
    ("meta/muse-spark-open-20261015", True), ("z-ai/glm-5.3-20260816", True), ("claude-opus-5-5", True),
    ("qwen3.6:35b-a3b-q8_0", True), ("sonnet", False), ("meta/", False),
    ("/x1", False), ("~deepseek/deepseek-pro-latest", False),
    ("a/b/c1", False), ("meta/muse", False),
])
def test_full_model_accepts_one_namespace_segment(model, ok):
    fm = _load("fleet-models", "fleet_models_hosted")
    assert bool(fm.FULL_MODEL.fullmatch(model)) is ok


# ------------------------------------------------------------------ D3 / D3b

@pytest.mark.parametrize("name,model", [
    ("hosted-muse-spark-open", "meta/muse-spark-open-20261015"),
    ("hosted-glm-5.3", "z-ai/glm-5.3-20260816"),
])
def test_the_key_reaches_epeius_on_the_fd_and_nowhere_else(orch, wt, tmp_path, name, model):
    route = _route(orch, tmp_path, name=name)
    log = Log()
    seen = []
    real = orch._spawn_session

    def spy(*a, **k):
        seen.append(a[0])
        return real(*a, **k)
    orch._spawn_session = spy
    session = _run(orch, wt, route, log)
    assert session.exit_code == 3
    out = json.loads(orch.transcripts[-1].strip().splitlines()[-1])
    assert out["sha"] == hashlib.sha256(KEY.encode()).hexdigest()
    argv = out["argv"]
    fd = argv[argv.index("--key-fd") + 1]
    assert fd.isdigit() and KEY not in " ".join(argv)
    assert "--backend" in argv and argv[argv.index("--backend") + 1] == f"openai:{model}"
    expected_output = 128000 if name == "hosted-glm-5.3" else 16000
    assert argv[argv.index("--max-output-tokens") + 1] == str(expected_output)
    assert "--target-file" in argv
    blob = json.dumps([seen, log.events, orch.transcripts], default=str)
    assert KEY not in blob and "PLANTED" not in blob


def test_the_orchestrator_never_reads_the_key_pipe(orch, wt, tmp_path, monkeypatch):
    route = _route(orch, tmp_path)
    opened = []
    real_open = orch.open_key_pipe

    def spy(runner):
        r = real_open(runner)
        opened.append(r)
        return r
    monkeypatch.setattr(orch, "open_key_pipe", spy)
    real_read, real_fdopen, real_close = os.read, os.fdopen, os.close
    closed = []

    def spy_close(fd):
        if fd in opened and not closed:
            closed.append(fd)
            opened.remove(fd)  # the number may be reused once it is closed
        return real_close(fd)
    monkeypatch.setattr(os, "close", spy_close)

    def no_read(fd, n):
        assert fd not in opened, "orchestrator read the key fd"
        return real_read(fd, n)

    def no_fdopen(fd, *a, **k):
        assert fd not in opened, "orchestrator opened the key fd"
        return real_fdopen(fd, *a, **k)
    monkeypatch.setattr(os, "read", no_read)
    monkeypatch.setattr(os, "fdopen", no_fdopen)
    assert _run(orch, wt, route).exit_code == 3
    assert closed and not opened  # closed in the parent, after the spawn


def test_the_epeius_child_environment_is_exactly_the_bare_set(orch, wt, tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("FLEET_TOKEN_CMD", "x")
    _run(orch, wt, _route(orch, tmp_path))
    env = json.loads(orch.transcripts[-1].strip().splitlines()[-1])["env"]
    assert "SSH_AUTH_SOCK" not in env and "FLEET_TOKEN_CMD" not in env
    assert set(env) - {"__CF_USER_TEXT_ENCODING"} <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "HOME"}
    assert env["HOME"] == env["TMPDIR"] != os.path.expanduser("~")
    assert not os.path.exists(env["HOME"])  # removed afterwards


def _boom(orch, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("epeius must not be spawned")
    monkeypatch.setattr(orch, "_spawn_session", boom)


def test_a_failing_helper_stops_without_spawning_or_leaking(orch, wt, tmp_path, monkeypatch):
    _boom(orch, monkeypatch)
    h = _helper(tmp_path, f"import sys\nsys.stdout.write({KEY!r})\n"
                          "sys.stderr.write('SECRETERR')\nsys.exit(7)\n")
    with pytest.raises(orch.Stop) as e:
        _run(orch, wt, _route(orch, tmp_path, helper=h))
    assert "7" in str(e.value) and h in str(e.value)
    assert "PLANTED" not in str(e.value) and "SECRETERR" not in str(e.value)


def test_an_empty_helper_is_caught_by_fionread_not_a_read(orch, wt, tmp_path, monkeypatch):
    _boom(orch, monkeypatch)
    real_read, real_pipe = os.read, os.pipe
    key_fds = []

    def pipe():
        r, w = real_pipe()
        if not key_fds:  # the first pipe made is open_key_pipe's own
            key_fds.append(r)
        return r, w
    monkeypatch.setattr(os, "pipe", pipe)
    monkeypatch.setattr(os, "read", lambda fd, n: pytest.fail("read on the key pipe")
                        if fd in key_fds else real_read(fd, n))
    with pytest.raises(orch.Stop) as e:
        _run(orch, wt, _route(orch, tmp_path, helper=_helper(tmp_path, "pass\n")))
    assert "wrote nothing" in str(e.value) and key_fds


def test_a_hung_helper_is_killed_and_stops(orch, wt, tmp_path, monkeypatch):
    _boom(orch, monkeypatch)
    monkeypatch.setattr(orch, "HOSTED_KEY_HELPER_SECS", 0.5)
    h = _helper(tmp_path, f"import sys, time\nsys.stdout.write({KEY!r})\n"
                          "sys.stdout.flush()\ntime.sleep(60)\n")
    with pytest.raises(orch.Stop) as e:
        _run(orch, wt, _route(orch, tmp_path, helper=h))
    assert "killed" in str(e.value) and "PLANTED" not in str(e.value)


def test_a_prompt_over_max_input_tokens_stops_before_the_helper(orch, wt, tmp_path, monkeypatch):
    _boom(orch, monkeypatch)
    monkeypatch.setattr(orch, "open_key_pipe",
                        lambda r: pytest.fail("helper spawned for an oversized prompt"))
    route = _route(orch, tmp_path, max_input_tokens=10)
    with pytest.raises(orch.Stop) as e:
        _run(orch, wt, route)
    assert "max_input_tokens" in str(e.value)


@pytest.mark.parametrize("name", ["hosted-muse-spark-open", "hosted-glm-5.3"])
def test_an_unfilled_fd_is_a_stop(orch, name):
    r = orch.RUNNERS[name]
    with pytest.raises(orch.Stop):
        orch.fill_epeius_argv(r.argv, r, None, "public")


def test_the_spawn_site_names_its_cross_repo_pins():
    src = (BIN / "orchestrator").read_text()
    assert "operator/epeius, bin/epeius, pinned at 18d3b38" in src
    assert "operator/hosttools, bin/fetch-<vendor>-key.sh, adopted" in src


# ------------------------------------------------------------------ D4

def test_an_open_class_runner_declaring_a_vendor_key_stops(orch):
    r = orch.RUNNERS["local-qwen"]._replace(vendor_key="~/bin/fetch-x-key.sh")
    with pytest.raises(orch.Stop) as e:
        orch.check_open_weights_box(r)
    assert "vendor_key" in str(e.value)


def test_hosted_classes_are_not_open(orch):
    for c in ("cn-hosted", "routed-hosted"):
        assert c in orch.PROVENANCE_CLASSES and c not in orch.OPEN_CLASSES
    for n in ("hosted-muse-spark-open", "hosted-glm-5.3"):
        orch.check_open_weights_box(orch.RUNNERS[n])


def test_a_private_plan_cannot_reach_a_hosted_runner(orch):
    with pytest.raises(orch.Stop) as e:
        orch.route_implementer({"zone": "private"}, CRED,
                               {"EUNOMIA_IMPL_RUNNER": "hosted-muse-spark-open"})
    assert "ADR-0006" in str(e.value)


def test_a_public_plan_routes_to_a_hosted_runner(orch):
    r = orch.route_implementer({"zone": "public", "repo": "operator/x"}, CRED,
                               {"EUNOMIA_IMPL_RUNNER": "hosted-muse-spark-open"})
    assert r.name == "hosted-muse-spark-open"


# ------------------------------------------------------------ no restriction

def test_a_hosted_runner_is_selectable_on_a_repo_and_as_the_default(tmp_path):
    env = dict(os.environ, FLEET_DB=str(tmp_path / "fleet.db"),
               EUNOMIA_FLEET_DIR=str(tmp_path / "fleet"))
    for repo in ("operator/eunomia", "default"):
        for name in ("hosted-muse-spark-open", "hosted-glm-5.3"):
            r = subprocess.run([sys.executable, str(BIN / "fleet-models"), "set", repo,
                                "implementer", name],
                               env=env, capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
