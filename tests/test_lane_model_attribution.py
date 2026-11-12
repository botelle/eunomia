"""Tests for plan 0190: a test-lane run records the model it believes ran,
and whether that belief came from this program's own launch-config pin
(`orchestrator.RUNNERS[...].model`, known before anything is spawned) or a
vendor's own trusted result metadata (never free-form stdout text).

No real vendor CLI is ever spawned here, for the same reason
`test_fleet_lane.py`'s own docstring gives: `spawn_lane` is replaced with a
canned `(rc, timed_out, stdout_text)` the same way that file's own
`_run_with_patched_spawn` does, so the outcome- and attribution-classification
logic AFTER the spawn is what these tests exercise."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_lane import (                                      # noqa: E402
    PLAN_ID, REPO, _agy_result, _build_target_repo, _fake_claude,
    _good_claude_stdout, _lane, _load_orch, _rows, _run, _write_fake_codex,
    mod,
)

needs_sandbox_exec = pytest.mark.skipif(
    not os.path.exists("/usr/bin/sandbox-exec"),
    reason="/usr/bin/sandbox-exec is absent (e.g. the Linux runner): the "
           "sub-muse adapter's argv names it before spawn_lane is ever "
           "reached, patched or not")


def _codex_runner(orch, script, model):
    """A fake `sub-codex` registry entry, identical in shape to
    `test_fleet_lane.py`'s own codex fixtures, but carrying `model` —
    `orch.RUNNERS["sub-codex"].model` at the call site, never hardcoded here,
    so this test cannot drift from whatever the registry actually pins."""
    return orch.Runner("sub-codex", (sys.executable, str(script), "-o", "<out>"),
                       False, "local_cli", (), "", "openai", (), model=model)


def _antigravity_runner(orch, script, model):
    return orch.Runner("sub-antigravity",
                       (sys.executable, str(script), "--json-schema", "<schema>"),
                       False, "local_cli", (), "", "google", (), model=model)


def _write_fake_codex_with_usage(cwd, result_doc, usage):
    """Like `test_fleet_lane._write_fake_codex`, plus a `turn.completed`
    `--json` event on stdout carrying `usage` — the shape `_codex_usage`
    reads — with no `model` key inside it, matching what codex is measured
    to emit (`bin/fleet-lane`'s own `_codex_usage` docstring)."""
    p = cwd / "fake_codex_usage.py"
    p.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "argv = sys.argv[1:]\n"
        "out_path = argv[argv.index('-o') + 1]\n"
        f"print(json.dumps({{'type': 'turn.completed', 'usage': {json.dumps(usage)}}}))\n"
        f"result = {json.dumps(result_doc)}\n"
        "with open(out_path, 'w') as f:\n"
        "    json.dump(result, f)\n"
    )
    return p


def _muse_stdout(model, doc, usage=None):
    """A minimal muse `--json` stream: one `run.usage` event (plan 0183's
    `_muse_usage` reads the LAST model entry) and one
    `run.terminal.completed` event carrying the document as `payload.text`
    (`_muse_parse`)."""
    usage_line = json.dumps({"payload_type": "run.usage", "payload": {
        "models": [{"model": model, **(usage or {"input_tokens": 10, "output_tokens": 5})}]}})
    terminal_line = json.dumps({"payload_type": "run.terminal.completed",
                                "payload": {"text": json.dumps(doc)}})
    return usage_line + "\n" + terminal_line + "\n"


# --------------------------------------------- D1: refused, before any spawn
@pytest.mark.parametrize("runner_name", ["sub-opus", "sub-antigravity", "sub-codex", "sub-muse"])
def test_a_refused_launch_still_records_the_launch_config_identity(tmp_path, runner_name):
    """The zone gate stops the run before `check_settings_precondition` or
    `harness_argv` ever run, so the REAL registry entry is safe to use here
    for every runner, antigravity included (plan 0190: a process that stops
    before producing anything still carries the identity `__init__` seeded
    at construction, from the registry alone)."""
    sha, _ = _build_target_repo(tmp_path, zone="private")
    orch = _load_orch()
    requested = orch.RUNNERS[runner_name].model
    assert requested, f"{runner_name} is expected to pin a model in the registry"
    # `testers=[]`: the zone gate stops the run before `check_tester` is ever
    # reached, so no tester row is needed — and `fleet-models` refuses to
    # write `sub-muse` as a `tester` row at all (plan 0173 D3, outside this
    # plan's lane), which a real seed call here would hit for no reason.
    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, runner_name, orch, cap=None, testers=[])
    assert rc == 1
    (row,) = _rows(tmp_path)
    assert row["outcome"] == "refused"
    assert row["requested_model"] == row["model"] == requested
    assert row["model_source"] == "launch-config"


# ------------------------------------- D2: usage exists, no reported model
def test_codex_usage_with_no_reported_model_attributes_to_launch_config(tmp_path):
    """Reproduces the original null-model result (codex's `_codex_usage`
    never reports one, measured) and confirms plan 0190's fix: the row is no
    longer NULL, it is the launch-config pin, correctly SOURCED as such
    rather than claimed as served."""
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    real_model = orch.RUNNERS["sub-codex"].model
    orch.RUNNERS = dict(orch.RUNNERS)
    result_doc = {"files": [{"path": "tests/lane/test_generated.py",
                             "content": "def test_x():\n    pass\n"}],
                 "gaps": [], "notes": "codex, no reported model"}
    usage = {"input_tokens": 100, "output_tokens": 20}
    script = _write_fake_codex_with_usage(tmp_path, result_doc, usage)
    orch.RUNNERS["sub-codex"] = _codex_runner(orch, script, real_model)

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-codex", orch)
    assert rc == 0
    (row,) = _rows(tmp_path)
    assert row["model"] == row["requested_model"] == real_model
    assert row["model_source"] == "launch-config"
    assert json.loads(row["usage_json"]) == usage


def test_antigravity_usage_with_no_reported_model_attributes_to_launch_config(tmp_path, monkeypatch):
    """Reproduces the original null-model result for antigravity (its own
    `_antigravity_usage` never reports a model either, measured) the same
    way, via a patched `spawn_lane` rather than a fake vendor script — the
    outcome- and attribution-classification logic is what is under test."""
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    real_model = orch.RUNNERS["sub-antigravity"].model
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _antigravity_runner(
        orch, tmp_path / "unused_antigravity.py", real_model)
    doc = {"files": [{"path": "tests/lane/test_generated.py",
                      "content": "def test_x():\n    pass\n"}], "gaps": [], "notes": ""}
    stdout = _agy_result(response=json.dumps(doc), usage={"total_tokens": 9})
    monkeypatch.setattr(mod, "spawn_lane", lambda *a, **k: (0, False, stdout))

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 0
    (row,) = _rows(tmp_path)
    assert row["model"] == row["requested_model"] == real_model
    assert row["model_source"] == "launch-config"
    assert json.loads(row["usage_json"]) == {"total_tokens": 9}


# ------------------------------- D3: a served model differs from the pin
def test_claude_served_model_differs_from_the_configured_pin(tmp_path, monkeypatch):
    """`test_fleet_lane.py`'s own `test_a_successful_run_leaves_a_complete_row`
    already pins this through `fleet-lane`'s one real adapter that reports a
    served model (`modelUsage`) — kept here too, parametrised across the
    other three, as the Definition of Done asks for Claude by name."""
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    requested = orch.RUNNERS["sub-opus"].model
    _fake_claude(tmp_path, monkeypatch, _good_claude_stdout())
    rc, _ = _lane(tmp_path, sha)
    assert rc == 0
    (row,) = _rows(tmp_path)
    assert row["requested_model"] == requested
    assert row["model"] == "claude-opus-4-8" and row["model"] != requested
    assert row["model_source"] == "cli-report"


@needs_sandbox_exec
def test_muse_served_model_differs_from_the_configured_pin(tmp_path, monkeypatch):
    """`fleet-models` has no tester spawn path for muse yet (plan 0173 D3,
    outside this plan's lane): `check_tester` is stubbed out so this test can
    still reach fleet-lane's OWN attribution logic past that gate."""
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    requested = orch.RUNNERS["sub-muse"].model
    served = requested + "-preview"             # deliberately NOT the pin
    doc = {"files": [{"path": "tests/lane/test_generated.py",
                      "content": "def test_x():\n    pass\n"}], "gaps": [], "notes": ""}
    monkeypatch.setattr(mod, "spawn_lane", lambda *a, **k: (0, False, _muse_stdout(served, doc)))
    monkeypatch.setattr(mod, "check_tester", lambda repo, runner_name: None)

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-muse", orch, cap="sub-muse=24", testers=[])
    assert rc == 0
    (row,) = _rows(tmp_path)
    assert row["requested_model"] == requested
    assert row["model"] == served and row["model"] != requested
    assert row["model_source"] == "cli-report"


# ---------------------------- D4: a process stops before its first result
def test_a_timeout_before_any_result_keeps_the_launch_config_identity(tmp_path, monkeypatch):
    """`spawn_lane` returns with `timed_out=True` and an empty stream: no
    adapter's `usage()` can read anything out of it, so `model` must stay
    exactly what `__init__` seeded from the registry, never erased, never
    guessed from the empty stream."""
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    real_model = orch.RUNNERS["sub-codex"].model
    orch.RUNNERS = dict(orch.RUNNERS)
    script = _write_fake_codex(tmp_path, {"files": [], "gaps": [], "notes": ""})
    orch.RUNNERS["sub-codex"] = _codex_runner(orch, script, real_model)
    monkeypatch.setattr(mod, "spawn_lane", lambda *a, **k: (-9, True, ""))

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-codex", orch)
    assert rc == 1
    (row,) = _rows(tmp_path)
    assert row["outcome"] == "timeout"
    assert row["model"] == row["requested_model"] == real_model
    assert row["model_source"] == "launch-config"
    assert row["usage_json"] is None


# ------------------------------------------------------- D5: quota stops
def test_a_codex_quota_stop_still_attributes_to_launch_config(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    real_model = orch.RUNNERS["sub-codex"].model
    orch.RUNNERS = dict(orch.RUNNERS)
    script = _write_fake_codex(tmp_path, {"files": [], "gaps": [], "notes": ""})
    orch.RUNNERS["sub-codex"] = _codex_runner(orch, script, real_model)
    stdout = "connecting...\nYou've hit your usage limit\n"
    monkeypatch.setattr(mod, "spawn_lane", lambda *a, **k: (1, False, stdout))

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-codex", orch)
    assert rc == 75
    (row,) = _rows(tmp_path)
    assert row["outcome"] == "quota"
    assert row["model"] == row["requested_model"] == real_model
    assert row["model_source"] == "launch-config"


def test_an_antigravity_quota_stop_still_attributes_to_launch_config(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    real_model = orch.RUNNERS["sub-antigravity"].model
    orch.RUNNERS = dict(orch.RUNNERS)
    orch.RUNNERS["sub-antigravity"] = _antigravity_runner(
        orch, tmp_path / "unused_antigravity.py", real_model)
    stdout = "connecting...\nYou have exhausted your quota on this model.\n"
    monkeypatch.setattr(mod, "spawn_lane", lambda *a, **k: (1, False, stdout))

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-antigravity", orch)
    assert rc == 75
    (row,) = _rows(tmp_path)
    assert row["outcome"] == "quota"
    assert row["model"] == row["requested_model"] == real_model
    assert row["model_source"] == "launch-config"


@needs_sandbox_exec
def test_a_muse_quota_stop_still_attributes_to_launch_config(tmp_path, monkeypatch):
    sha, _ = _build_target_repo(tmp_path)
    orch = _load_orch()
    real_model = orch.RUNNERS["sub-muse"].model
    stdout = "connecting...\nusage limit reached\n"
    monkeypatch.setattr(mod, "spawn_lane", lambda *a, **k: (1, False, stdout))
    monkeypatch.setattr(mod, "check_tester", lambda repo, runner_name: None)

    rc, _ = _run(tmp_path, REPO, PLAN_ID, sha, "sub-muse", orch, cap="sub-muse=24", testers=[])
    assert rc == 75
    (row,) = _rows(tmp_path)
    assert row["outcome"] == "quota"
    assert row["model"] == row["requested_model"] == real_model
    assert row["model_source"] == "launch-config"


# ------------------------------------------- D6: old records through migration
def test_a_legacy_row_is_untouched_and_unbackfilled(tmp_path):
    """A `lane_run` row written before `requested_model`/`model_source`
    existed has neither column — simulated here by inserting straight
    against the OLD schema, columns this plan added omitted entirely. The
    writer's own self-healing `_migrate` (run by `LaneRecord._connect`, the
    same path every read/write in this program takes) must add the two
    columns as NULL and must NOT invent a `requested_model` for the old row
    from this host's CURRENT registry pin — a seat's pin can move after a
    row was written, so backfilling from "now" would misattribute history
    (Boundaries)."""
    db = tmp_path / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE lane_run (run_id TEXT PRIMARY KEY, repo TEXT NOT NULL, "
        "plan TEXT NOT NULL, runner TEXT NOT NULL, family TEXT, model TEXT, "
        "sha TEXT, prompt_sha256 TEXT, prompt_bytes INTEGER, started_at TEXT NOT NULL, "
        "ended_at TEXT, spawned INTEGER NOT NULL, outcome TEXT NOT NULL, rc INTEGER, "
        "usage_json TEXT, detail TEXT, pr INTEGER)")
    con.execute(
        "INSERT INTO lane_run (run_id, repo, plan, runner, family, model, started_at, "
        "spawned, outcome) VALUES ('legacy-1', ?, ?, 'sub-codex', 'openai', "
        "'gpt-5-whatever-was-pinned-then', '2026-01-01T00:00:00Z', 1, 'pr-opened')",
        (REPO, PLAN_ID))
    con.commit()
    con.close()

    orch = _load_orch()
    rec = mod.LaneRecord(REPO, "other-plan", "sub-codex", "openai",
                         orch, str(db), model=orch.RUNNERS["sub-codex"].model)
    rec.reserve("sub-codex=24", lambda s: {"sub-codex": 24})

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        legacy = dict(con.execute(
            "SELECT * FROM lane_run WHERE run_id='legacy-1'").fetchone())
        fresh = dict(con.execute(
            "SELECT * FROM lane_run WHERE run_id=?", (rec.run_id,)).fetchone())
    finally:
        con.close()
    assert legacy["model"] == "gpt-5-whatever-was-pinned-then"
    assert legacy["requested_model"] is None and legacy["model_source"] is None
    assert fresh["requested_model"] == orch.RUNNERS["sub-codex"].model
    assert fresh["model_source"] == "launch-config"


def test_fleet_db_migrate_adds_the_new_columns_idempotently(tmp_path):
    """`fleet-db-migrate` is the OTHER path that can meet an old `lane_run`
    (a fresh-install host, or one whose collector ran before a writer did) —
    `_apply` runs `fleet-lane`'s own `_migrate` generically, so no change to
    `bin/fleet-db-migrate` itself was needed; this proves that integration
    rather than assuming it."""
    db = tmp_path / "fleet.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE lane_run (run_id TEXT PRIMARY KEY, repo TEXT NOT NULL, "
        "plan TEXT NOT NULL, runner TEXT NOT NULL, family TEXT, model TEXT, "
        "sha TEXT, prompt_sha256 TEXT, prompt_bytes INTEGER, started_at TEXT NOT NULL, "
        "ended_at TEXT, spawned INTEGER NOT NULL, outcome TEXT NOT NULL, rc INTEGER, "
        "usage_json TEXT, detail TEXT, pr INTEGER)")
    con.commit()
    con.close()

    BIN = Path(__file__).resolve().parent.parent / "bin"
    env = dict(os.environ, FLEET_DB=str(db), EUNOMIA_FLEET_DIR=str(tmp_path))
    r1 = subprocess.run([sys.executable, str(BIN / "fleet-db-migrate")],
                        capture_output=True, text=True, env=env)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(lane_run)")}
    con.close()
    assert {"requested_model", "model_source"} <= cols

    before = subprocess.run([sys.executable, str(BIN / "fleet-db-migrate")],
                            capture_output=True, text=True, env=env)
    assert before.returncode == 0, before.stdout + before.stderr
