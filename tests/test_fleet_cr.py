"""Tests for fleet-cr + validate_cr + the launch preamble (plan 0007 §4)."""
import json
import os
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _run(fleet_dir, args, session="s-test"):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet_dir), EUNOMIA_SESSION=session)
    env.pop("EUNOMIA_LEDGER_HOST", None)
    return subprocess.run([sys.executable, str(BIN / "fleet-cr")] + args,
                          env=env, capture_output=True, text=True)


def _claim(fleet_dir, resource_json, session):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet_dir), EUNOMIA_SESSION=session)
    env.pop("EUNOMIA_LEDGER_HOST", None)
    lid = subprocess.run([sys.executable, str(BIN / "fleet-claim"),
                          "--assign", resource_json, "--holder", session],
                         env=env, capture_output=True, text=True).stdout.strip()
    subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--activate", lid],
                   env=env, capture_output=True, text=True)
    return lid


def _events(fleet_dir, etype):
    p = fleet_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()
            if json.loads(l)["type"] == etype]


FILE_ARGS = ["file", "--repo", "operator/sniff", "--lane", "w1",
             "--target", "project.yml", "--intent", "add SniffTests target"]


# ---------------------------------------------------------------- file

def test_file_writes_wellformed_cr_and_emits_once(tmp_path):
    r = _run(tmp_path, FILE_ARGS, session="w1sess")
    assert r.returncode == 0, r.stderr
    cr_id = r.stdout.strip()
    assert cr_id == "CR-w1-001"
    rec = json.loads((tmp_path / "cr" / "operator" / "sniff" / "w1-001.json").read_text())
    assert rec["state"] == "filed" and rec["repo"] == "operator/sniff"
    assert rec["intent"] and rec["target"] == "project.yml"
    evs = _events(tmp_path, "cr-filed")
    assert len(evs) == 1 and evs[0]["detail"]["cr"] == cr_id


def test_concurrent_filers_get_distinct_ids(tmp_path):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="f")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    procs = [subprocess.Popen([sys.executable, str(BIN / "fleet-cr")] + FILE_ARGS,
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(2)]
    ids = sorted(p.communicate()[0].strip() for p in procs)
    assert ids == ["CR-w1-001", "CR-w1-002"], ids
    assert len(_events(tmp_path, "cr-filed")) == 2


def test_file_with_secret_shaped_patch_refused_before_record_write(tmp_path):
    patch = tmp_path / "p.diff"
    patch.write_text("+TOKEN = 'ghp_" + "a" * 30 + "'\n")
    r = _run(tmp_path, ["file", "--repo", "operator/x", "--lane", "w1",
                        "--target", "cfg.py", "--patch-file", str(patch)])
    assert r.returncode != 0
    assert "github-pat" in r.stderr, "the refusal names the rule"
    assert "ghp_" not in r.stderr, "the refusal never echoes the secret"
    # reservation-first (r7 L1): the burned counter dotfile is the only trace
    # a refusal leaves — it carries no request content; no RECORD is written
    assert not list((tmp_path / "cr").rglob("*.json")), "no record written"
    assert not _events(tmp_path, "cr-filed")


def test_file_needs_intent_or_patch(tmp_path):
    r = _run(tmp_path, ["file", "--repo", "operator/x", "--lane", "w1",
                        "--target", "cfg.py"])
    assert r.returncode != 0 and "intent" in r.stderr


def test_traversal_repo_and_lane_refused_before_pathing(tmp_path):
    for repo, lane in ((".." + "/etc", "w1"), ("a/b/c", "w1"),
                       ("operator/x", "W1 up"), ("operator/..", "w1")):
        r = _run(tmp_path, ["file", "--repo", repo, "--lane", lane,
                            "--target", "t", "--intent", "i"])
        assert r.returncode != 0, (repo, lane)
    assert not (tmp_path / "cr").exists()
    assert not (tmp_path.parent / "etc").exists()


# ---------------------------------------------------------------- apply / reject

def _file_one(tmp_path, session="filer"):
    return _run(tmp_path, FILE_ARGS, session=session).stdout.strip()


def test_apply_by_integrator_transitions_and_emits(tmp_path):
    cr_id = _file_one(tmp_path)
    lease = _claim(tmp_path, '{"type":"branch","repo":"operator/sniff","branch":"integration","role":"integrator"}', "intg")
    r = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "a1b2c3d"],
             session="intg")
    assert r.returncode == 0, r.stderr
    rec = json.loads((tmp_path / "cr" / "operator" / "sniff" / "w1-001.json").read_text())
    assert rec["state"] == "applied" and rec["applied_commit"] == "a1b2c3d"
    evs = _events(tmp_path, "cr-applied")
    assert len(evs) == 1 and evs[0]["detail"]["commit"] == "a1b2c3d"
    assert evs[0]["detail"]["lease"] == lease
    # second apply refused — transitions are once-only
    r2 = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "a1b2c3d"],
              session="intg")
    assert r2.returncode != 0 and "once-only" in r2.stderr
    assert len(_events(tmp_path, "cr-applied")) == 1


def test_apply_by_non_holder_refused_before_write(tmp_path):
    cr_id = _file_one(tmp_path)
    _claim(tmp_path, '{"type":"branch","repo":"operator/sniff","branch":"integration","role":"integrator"}', "intg")
    r = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "a1b2c3d"],
             session="notintg")
    assert r.returncode != 0 and "integrator" in r.stderr
    rec = json.loads((tmp_path / "cr" / "operator" / "sniff" / "w1-001.json").read_text())
    assert rec["state"] == "filed", "no write happened"


def test_apply_without_any_lease_refused(tmp_path):
    cr_id = _file_one(tmp_path)
    r = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "abcdef0"],
             session="whoever")
    assert r.returncode != 0 and "integrator" in r.stderr


def test_reject_records_state_and_emits_nothing(tmp_path):
    cr_id = _file_one(tmp_path)
    _claim(tmp_path, '{"type":"paths","repo":"operator/sniff","globs":["*.yml"],"role":"integrator"}', "intg")
    r = _run(tmp_path, ["reject", "operator/sniff", cr_id], session="intg")
    assert r.returncode == 0, r.stderr
    rec = json.loads((tmp_path / "cr" / "operator" / "sniff" / "w1-001.json").read_text())
    assert rec["state"] == "rejected"
    assert not _events(tmp_path, "cr-applied")


def test_apply_bad_sha_refused(tmp_path):
    cr_id = _file_one(tmp_path)
    _claim(tmp_path, '{"type":"branch","repo":"operator/sniff","branch":"m","role":"integrator"}', "intg")
    r = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "not-a-sha"],
             session="intg")
    assert r.returncode != 0 and "commit sha" in r.stderr


def test_aliased_cr_body_refused(tmp_path):
    cr_id = _file_one(tmp_path)
    p = tmp_path / "cr" / "operator" / "sniff" / "w1-001.json"
    rec = json.loads(p.read_text())
    rec["id"] = "CR-w1-777"                       # valid shape, wrong identity
    p.write_text(json.dumps(rec))
    _claim(tmp_path, '{"type":"branch","repo":"operator/sniff","branch":"m","role":"integrator"}', "intg")
    r = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "abcdef0"],
             session="intg")
    assert r.returncode != 0 and "identity" in r.stderr


def test_apply_revalidates_hand_edited_secret(tmp_path):
    cr_id = _file_one(tmp_path)
    p = tmp_path / "cr" / "operator" / "sniff" / "w1-001.json"
    rec = json.loads(p.read_text())
    rec["patch"] = "+key = 'sk-" + "b" * 30 + "'"   # laundered in by hand
    p.write_text(json.dumps(rec))
    _claim(tmp_path, '{"type":"branch","repo":"operator/sniff","branch":"m","role":"integrator"}', "intg")
    r = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "abcdef0"],
             session="intg")
    assert r.returncode != 0 and "openai-key" in r.stderr
    assert "sk-" + "b" * 4 not in r.stderr


# ---------------------------------------------------------------- list

def test_list_filters_and_degrades_corrupt_file(tmp_path):
    _file_one(tmp_path)
    _run(tmp_path, ["file", "--repo", "operator/other", "--lane", "w2",
                    "--target", "t2", "--intent", "i2"])
    bad = tmp_path / "cr" / "operator" / "sniff" / "w1-999.json"
    bad.write_text("{not json")
    r = _run(tmp_path, ["list", "--repo", "operator/sniff", "--state", "filed", "--json"])
    assert r.returncode == 0, r.stderr
    crs = json.loads(r.stdout)["crs"]
    assert [c["id"] for c in crs] == ["CR-w1-001"]
    assert "unreadable CR file" in r.stderr
    rt = _run(tmp_path, ["list"])
    assert rt.returncode == 0 and "CR-w1-001" in rt.stdout and "CR-w2-001" in rt.stdout


def test_list_strips_control_chars_from_every_field(tmp_path):
    """1522 #11 r7 L2: list renders unvalidated records by design — a
    hand-written file with ANSI or raw newlines in repo/id (not just target)
    must not forge listing rows."""
    _file_one(tmp_path)
    d = tmp_path / "cr" / "operator" / "sniff"
    (d / "w1-666.json").write_text(json.dumps(
        {"id": "CR-w1-666\x1b[31m", "repo": "operator/sniff\nappliedX",
         "lane": "w1", "target": "t\x1b[2K", "state": "filed\x07",
         "filed": "2026-08-28T00:00:00Z", "intent": "x"}))
    r = _run(tmp_path, ["list"])
    assert r.returncode == 0, r.stderr
    assert "\x1b" not in r.stdout and "\x07" not in r.stdout
    assert "\nappliedX" not in r.stdout, "an embedded newline must not open a forged row"
    assert "CR-w1-001" in r.stdout, "the real row still renders"


def test_list_strips_c1_control_chars(tmp_path):
    """1522 #15 r8 L1: a bare U+009B is CSI on xterm-family terminals — ANSI
    with no ESC byte, so an \\x1b-only strip never sees it. Escapes, not raw
    bytes, so the payload survives patches and review rendering."""
    _file_one(tmp_path)
    d = tmp_path / "cr" / "operator" / "sniff"
    (d / "w1-667.json").write_text(json.dumps(
        {"id": "CR-w1-667\u009b31m", "repo": "operator/sniff\u00852K",
         "lane": "w1", "target": "t\u009b2K", "state": "filed\u0090",
         "filed": "2026-08-28T00:00:00Z", "intent": "x"}))
    r = _run(tmp_path, ["list"])
    assert r.returncode == 0, r.stderr
    leaked = sorted({ch for ch in r.stdout if "\u0080" <= ch <= "\u009f"})
    assert not leaked, f"C1 controls reached the terminal: {leaked!r}"
    assert b"\xc2\x9b" not in r.stdout.encode("utf-8"), "no CSI on the wire either"
    assert "CR-w1-001" in r.stdout, "the real row still renders"


def test_list_takes_no_lock(tmp_path):
    import fcntl
    _file_one(tmp_path)
    # derived from the lib (1522 #11 r4 M3): a hardcoded name from the old
    # naive encoding locked a file nothing uses — the test was vacuous
    lockp = tmp_path / "locks" / ("cr-" + _lib()._repo_slug("operator/sniff") + ".lock")
    fd = os.open(str(lockp), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = _run(tmp_path, ["list"])
        assert r.returncode == 0 and "CR-w1-001" in r.stdout
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------- validate_cr unit

def _lib():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("fleetlib_cr", str(BIN / "fleetlib.py"))
    spec = importlib.util.spec_from_loader("fleetlib_cr", loader)
    lib = importlib.util.module_from_spec(spec)
    loader.exec_module(lib)
    return lib


def _valid_rec():
    return {"id": "CR-w1-001", "repo": "operator/x", "lane": "w1",
            "target": "t", "state": "filed", "filed": "2026-08-28T00:00:00Z",
            "intent": "do the thing"}


def test_validate_cr_rejects_bad_records(tmp_path):
    import pytest
    lib = _lib()
    ok = _valid_rec()
    assert lib.validate_cr(dict(ok)) == ok
    for mutation in (
        {"state": "pending"},                       # unknown state
        {"intent": None},                           # neither intent nor patch
        {"extra_key": 1},                           # extra key
        {"id": "CR-w2-001"},                        # id/lane mismatch
        {"id": "CR-w1-2-003"},                      # cross-lane id (prefix trap, #11 L2)
        {"target": ""},                             # empty target
    ):
        rec = dict(ok)
        rec.update(mutation)
        if mutation == {"intent": None}:
            rec.pop("intent")
        with pytest.raises(SystemExit):
            lib.validate_cr(rec)


# ---------------------------------------------------------------- preamble doc

def _runnable_blocks(doc):
    """Fenced blocks the doc offers as commands: opening fence with NO info string.

    A tagged fence (```sh, ```text) is illustrative and is skipped -- that is the
    doc's contract with this test, and the doc says so at the top.

    Line-based rather than `re.findall(r"```\n(.*?)```")`, which pairs fences
    naively: it cannot see an info string on the OPENING fence, so a tagged block
    desynchronises the pairing and every later "block" is the prose between two
    unrelated fences.

    Checked rather than assumed, because the obvious story is wrong: replayed on
    #129's doc the old regex found exactly its four real blocks and no prose, so
    it is NOT what broke #129 (that was a genuine unbound variable on a line
    somebody wrote as a command). What the old regex cannot survive is THIS
    layout, which needs tagged fences for a path variant and a sample refusal --
    on it, the old regex yields three prose or empty blocks and one that starts
    mid-list. The parser is what makes tagged fences usable at all.
    """
    out, buf, info = [], None, None
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if buf is None:                       # opening
                buf, info = [], stripped[3:].strip()
            else:                                 # closing
                if not info:
                    out.append("\n".join(buf) + "\n")
                buf, info = None, None
            continue
        if buf is not None:
            buf.append(line)
    assert buf is None, "unclosed fence in launch-preamble.md"
    return out


def test_launch_preamble_commands_are_runnable(tmp_path):
    """Plan DoD: the preamble's fenced commands execute against a scratch tree."""
    doc = (Path(__file__).resolve().parent.parent / "docs" / "launch-preamble.md").read_text()
    blocks = _runnable_blocks(doc)
    assert blocks, "preamble has no fenced commands"
    # A hand-launched session has CLAUDE_CODE_SESSION_ID; CI does not, and the
    # self-claim block reads it. Supplying it here is the same courtesy the
    # scratch fleet dir gets -- without it the block cannot be tested at all,
    # and #129 shipped precisely because the variable IS set on a developer
    # machine, so the failure was invisible outside CI.
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path),
               CLAUDE_CODE_SESSION_ID="pre-sess")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    lid = _claim(tmp_path, '{"type":"branch","branch":"pre"}', "pre-sess")
    for block in blocks:
        # run the whole block as ONE script — it is a copy-paste unit, and the
        # first line's `export` must persist to the lines that use it
        script = (block.replace("bin/", str(BIN) + "/")
                       .replace("<session-id>", "pre-sess")
                       .replace("<lease-id>", lid)
                       .replace("<one line on what this session is doing>", "test"))
        # set -e etc. so ANY failing line fails the block (#11 M3) — without
        # it the block's status is just the final printf's
        res = subprocess.run("set -euo pipefail\n" + script, shell=True, env=env,
                             capture_output=True, text=True, cwd=str(tmp_path),
                             timeout=30, executable="/bin/bash")
        assert res.returncode == 0, f"block -> {res.returncode}: {res.stderr[:300]}"
    assert (tmp_path / "sessions" / "pre-sess" / "status.md").read_text().startswith("starting:")


def test_worker_lane_lease_cannot_self_apply(tmp_path):
    """1522 #11 H1: a filer's own WORKING branch lease on the repo (the normal
    lane shape) must not satisfy the integrator gate — only a role-bearing
    lease does."""
    cr_id = _file_one(tmp_path, session="w1sess")
    _claim(tmp_path, '{"type":"branch","repo":"operator/sniff","branch":"w1/screens"}',
           "w1sess")                                # its own lane lease, no role
    r = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "abcdef0"],
             session="w1sess")
    assert r.returncode != 0 and "integrator" in r.stderr
    rec = json.loads((tmp_path / "cr" / "operator" / "sniff" / "w1-001.json").read_text())
    assert rec["state"] == "filed", "self-apply must not transition anything"


def test_assign_unknown_role_refused(tmp_path):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="c")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    r = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--assign",
                        '{"type":"branch","repo":"a/b","branch":"m","role":"integator"}'],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "unknown resource role" in r.stderr


def test_secret_shaped_target_refused(tmp_path):
    """1522 #11 M2: target rides the rendered tree and cr-filed detail — it is
    scanned like intent/patch."""
    r = _run(tmp_path, ["file", "--repo", "operator/x", "--lane", "w1",
                        "--target", "https://h/hook?token=ghp_" + "c" * 30,
                        "--intent", "wire the hook"])
    assert r.returncode != 0 and "github-pat" in r.stderr
    assert "ghp_" + "c" * 4 not in r.stderr
    assert not list((tmp_path / "cr").rglob("*.json")), "no record written"


def test_missing_patterns_conf_fails_loud(tmp_path):
    """1522 #11 M1: a missing conf must refuse to validate, never silently
    scan against zero rules."""
    import shutil
    stripped = tmp_path / "stripped"
    (stripped / "bin").mkdir(parents=True)
    for f in ("fleet-cr", "fleetlib.py", "fleet-emit"):
        shutil.copy(BIN / f, stripped / "bin" / f)
    # no config/ dir at all
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="s")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    r = subprocess.run([sys.executable, str(stripped / "bin" / "fleet-cr")] + FILE_ARGS[0:],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "secret-patterns.conf" in r.stderr and "blind" in r.stderr
    assert not list((tmp_path / "cr").rglob("*.json")), "no record written"


def test_untyped_fields_cannot_launder_a_secret_through_apply(tmp_path):
    """1522 #11 r2 M1: for_pr/filed/applied_commit are structurally typed, so a
    hand-edited token in them is refused at the transition, not carried into
    the rendered tree."""
    cr_id = _file_one(tmp_path)
    p = tmp_path / "cr" / "operator" / "sniff" / "w1-001.json"
    _claim(tmp_path, '{"type":"branch","repo":"operator/sniff","branch":"integration","role":"integrator"}', "intg")
    rec = json.loads(p.read_text())
    rec["for_pr"] = "ghp_" + "d" * 30                  # token where an int belongs
    p.write_text(json.dumps(rec))
    r = _run(tmp_path, ["apply", "operator/sniff", cr_id, "--commit", "abcdef0"],
             session="intg")
    assert r.returncode != 0 and "for_pr must be an integer" in r.stderr
    assert "ghp_" + "d" * 4 not in r.stderr
    rec = json.loads(p.read_text())
    rec["for_pr"] = 7
    rec["applied_commit"] = "ghp_" + "d" * 30          # junk where a sha belongs
    p.write_text(json.dumps(rec))
    r2 = _run(tmp_path, ["reject", "operator/sniff", cr_id], session="intg")
    assert r2.returncode != 0 and "commit sha" in r2.stderr


def test_degraded_conf_with_zero_content_rules_fails_loud(tmp_path):
    """1522 #11 r2 M2: tabs->spaces (or a deleted content block) must refuse to
    validate, never silently scan against zero rules."""
    import shutil
    stripped = tmp_path / "stripped"
    (stripped / "bin").mkdir(parents=True)
    (stripped / "config").mkdir()
    for f in ("fleet-cr", "fleetlib.py", "fleet-emit"):
        shutil.copy(BIN / f, stripped / "bin" / f)
    conf = (BIN.parent / "config" / "secret-patterns.conf").read_text()
    kept = [l for l in conf.splitlines() if not l.startswith("content\t")]
    (stripped / "config" / "secret-patterns.conf").write_text(
        "\n".join(kept) + "\n")                        # content block deleted,
                                                       # path/cmd rows intact
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="s")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    r = subprocess.run([sys.executable, str(stripped / "bin" / "fleet-cr")] + FILE_ARGS,
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "zero content rules" in r.stderr
    assert not list((tmp_path / "cr").rglob("*.json")), "no record written"


def test_integrator_role_requires_gateable_shape(tmp_path):
    """1522 #11 r2 L1: a role on a shape the gate can never match is refused at
    assign time, not discovered weeks later at apply time."""
    for res in ('{"type":"service","host":"edgehost","service":"x","role":"integrator"}',
                '{"type":"branch","branch":"m","role":"integrator"}'):    # no repo
        env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="c")
        env.pop("EUNOMIA_LEDGER_HOST", None)
        r = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--assign", res],
                           env=env, capture_output=True, text=True)
        assert r.returncode != 0 and "integrator" in r.stderr, res


def test_second_integrator_lease_on_repo_refused(tmp_path):
    """1522 #11 r2 L2: one integrator-role lease per repo — a second --assign
    while one is assigned/active is refused."""
    _claim(tmp_path, '{"type":"branch","repo":"operator/sniff","branch":"integration","role":"integrator"}', "intg1")
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="c")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    r = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--assign",
                        '{"type":"branch","repo":"operator/sniff","branch":"other","role":"integrator"}',
                        "--holder", "intg2"],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "already holds the integrator role" in r.stderr


def test_oversize_patch_refused(tmp_path):
    """1522 #11 r2 L4: a dumped corpus is not a patch."""
    big = tmp_path / "big.diff"
    big.write_text("+x\n" * 300000)                    # ~900KB > 512KB cap
    r = _run(tmp_path, ["file", "--repo", "operator/x", "--lane", "w1",
                        "--target", "t", "--patch-file", str(big)])
    assert r.returncode != 0 and "exceeds" in r.stderr
    assert not (tmp_path / "cr").exists()


def test_concurrent_integrator_assigns_exactly_one_wins(tmp_path):
    """1522 #11 r3 MEDIUM: two concurrent integrator --assigns for one repo
    (different branches -> different lease locks) must serialize on the shared
    role lock — exactly one grant, one refusal."""
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="c")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    procs = [subprocess.Popen(
        [sys.executable, str(BIN / "fleet-claim"), "--assign",
         '{"type":"branch","repo":"operator/sniff","branch":"int-%d","role":"integrator"}' % i,
         "--holder", "intg%d" % i],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(2)]
    results = [p.communicate() + (p.returncode,) for p in procs]
    winners = [r for r in results if r[2] == 0]
    losers = [r for r in results if r[2] != 0]
    assert len(winners) == 1 and len(losers) == 1, results
    assert "already holds the integrator role" in losers[0][1]


def test_single_mangled_conf_line_fails_loud(tmp_path):
    """1522 #11 r3 low: ONE tabs->spaces content line must refuse validation,
    not silently un-enforce that one rule while the rest keep the parse alive."""
    import shutil
    stripped = tmp_path / "stripped"
    (stripped / "bin").mkdir(parents=True)
    (stripped / "config").mkdir()
    for f in ("fleet-cr", "fleetlib.py", "fleet-emit"):
        shutil.copy(BIN / f, stripped / "bin" / f)
    lines = (BIN.parent / "config" / "secret-patterns.conf").read_text().splitlines()
    out = []
    broke = False
    for l in lines:
        if not broke and l.startswith("content\tgithub-pat"):
            out.append(l.replace("\t", "  "))          # mangle exactly one rule
            broke = True
        else:
            out.append(l)
    assert broke
    (stripped / "config" / "secret-patterns.conf").write_text("\n".join(out) + "\n")
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="s")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    r = subprocess.run([sys.executable, str(stripped / "bin" / "fleet-cr")] + FILE_ARGS,
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "unparseable rule line" in r.stderr


def test_binary_patch_file_named_refusal(tmp_path):
    """1522 #11 r3 low: a binary --patch-file gets a named refusal, not a
    UnicodeDecodeError traceback."""
    b = tmp_path / "blob.bin"
    b.write_bytes(bytes(range(256)) * 4)
    r = _run(tmp_path, ["file", "--repo", "operator/x", "--lane", "w1",
                        "--target", "t", "--patch-file", str(b)])
    assert r.returncode != 0
    assert "not UTF-8" in r.stderr and "Traceback" not in r.stderr


def test_integrator_role_requires_holder_and_valid_repo(tmp_path):
    """1522 #11 r4 M1+M2: a role pool row (no --holder) is refused — a random
    --next puller must never silently become the integrator — and the repo is
    SHAPE-validated, not just present."""
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="c")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    r = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--assign",
                        '{"type":"branch","repo":"operator/sniff","branch":"i","role":"integrator"}'],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "requires --holder" in r.stderr
    for repo_json in ('"operator/sniff "', '"operator/sniff/"', '5'):
        r = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--assign",
                            '{"type":"branch","repo":%s,"branch":"i","role":"integrator"}' % repo_json,
                            "--holder", "intg"],
                           env=env, capture_output=True, text=True)
        assert r.returncode != 0 and "owner/name repo" in r.stderr, repo_json
        assert "Traceback" not in r.stderr, repo_json


def test_next_never_claims_a_role_bearing_row(tmp_path):
    """1522 #11 r4 M1: even a hand-written role pool row is skipped by --next."""
    leases = tmp_path / "leases"
    leases.mkdir(parents=True, exist_ok=True)
    leases.joinpath("branch--introw--001.json").write_text(json.dumps(
        {"id": "branch--introw--001",
         "resource": {"type": "branch", "repo": "operator/sniff", "branch": "i",
                      "role": "integrator"},
         "holder": None, "state": "assigned", "created": "2000-01-01T00:00:00Z"}))
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path), EUNOMIA_SESSION="w9")
    env.pop("EUNOMIA_LEDGER_HOST", None)
    good = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--assign",
                           '{"type":"branch","branch":"plain"}'],
                          env=env, capture_output=True, text=True).stdout.strip()
    r = subprocess.run([sys.executable, str(BIN / "fleet-claim"), "--next", "branch"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == good, "the role row (oldest) must be skipped"
    rec = json.loads(leases.joinpath("branch--introw--001.json").read_text())
    assert rec["holder"] is None, "the role row is untouched"


def test_hyphenated_prose_is_not_an_openai_key(tmp_path):
    """1522 #11 r4 L1: 'sk-learn-based-classifier' prose files fine; a
    real-shaped key still refuses."""
    r = _run(tmp_path, ["file", "--repo", "operator/x", "--lane", "w1",
                        "--target", "clf.py",
                        "--intent", "swap in the sk-learn-based-classifier here"])
    assert r.returncode == 0, r.stderr
    r2 = _run(tmp_path, ["file", "--repo", "operator/x", "--lane", "w1",
                         "--target", "clf.py",
                         "--intent", "key is sk-" + "a1" * 15])
    assert r2.returncode != 0 and "openai-key" in r2.stderr


def test_cr_ids_never_recycle_after_rm(tmp_path):
    """1522 #11 r5 M1 + r7 L1: the poisoned-CR remedy is rm — a later filing
    must mint a NEW number, or a PR's needs: line could be satisfied by a
    different request. Reservation-first makes this unconditional: the counter
    records N+1 BEFORE the record for N exists, so there is no crash point
    where a record sits on disk unreserved."""
    first = _file_one(tmp_path)
    assert first == "CR-w1-001"
    d = tmp_path / "cr" / "operator" / "sniff"
    assert (d / ".w1-next").read_text() == "2", \
        "the reservation precedes the record — never the other way around"
    (d / "w1-001.json").unlink()                      # the remedy
    second = _run(tmp_path, FILE_ARGS, session="filer").stdout.strip()
    assert second == "CR-w1-002", "monotonic — the freed number is never reused"


def test_refused_filing_burns_its_reserved_number(tmp_path):
    """1522 #11 r7 L1: a validation refusal lands AFTER the reservation, so the
    number is burned and the next filing skips it — a gap, never a reuse."""
    assert _file_one(tmp_path) == "CR-w1-001"
    r = _run(tmp_path, FILE_ARGS[:-1] + ["use hvs." + "Ab1" * 10 + " here"])
    assert r.returncode != 0 and "bao-token" in r.stderr
    d = tmp_path / "cr" / "operator" / "sniff"
    assert not (d / "w1-002.json").exists(), "the refused CR left no record"
    nxt = _run(tmp_path, FILE_ARGS, session="filer").stdout.strip()
    assert nxt == "CR-w1-003", "002 was reserved by the refused filing — burned"


def test_failed_reservation_refuses_loudly_and_writes_no_record(tmp_path):
    """1522 #11 r7 L1: the old order WARNed and exited 0 on a failed bump (the
    record already existed); reservation-first has written nothing yet, so a
    failed reservation refuses outright."""
    import pytest
    if os.geteuid() == 0:
        # mode bits do not bind root — the write would SUCCEED and the test
        # would pass vacuously (#15 r8 nit)
        pytest.skip("mode-bit denial is meaningless as root")
    assert _file_one(tmp_path) == "CR-w1-001"
    d = tmp_path / "cr" / "operator" / "sniff"
    os.chmod(d, 0o555)                 # the counter temp cannot be created
    try:
        r = _run(tmp_path, FILE_ARGS, session="filer")
    finally:
        os.chmod(d, 0o755)
    assert r.returncode != 0 and "reservation failed" in r.stderr
    assert r.stdout.strip() == "", "no id printed for a filing that never was"
    assert not (d / "w1-002.json").exists()
    assert (d / ".w1-next").read_text() == "2", "the floor is untouched"


def test_keyvault_token_shape_refused(tmp_path):
    """1522 #11 r5 M2: the fleet's own currency (OpenBao hvs. tokens) is in the
    deny list — the likeliest real paste refuses, naming the rule only."""
    r = _run(tmp_path, ["file", "--repo", "operator/x", "--lane", "w1",
                        "--target", "t",
                        "--intent", "use hvs." + "Ab1" * 10 + " for the broker"])
    assert r.returncode != 0 and "bao-token" in r.stderr
    assert "hvs." + "Ab1" * 2 not in r.stderr
    assert not list((tmp_path / "cr").rglob("*.json")), "no record written"


def test_torn_counter_refuses_loudly(tmp_path):
    """1522 #11 r6 M: an empty/torn counter file is the floor at risk — filing
    refuses with repair guidance, writes nothing (the torn-read refusal lands
    BEFORE the reservation, so not even a number is burned)."""
    first = _file_one(tmp_path)
    assert first == "CR-w1-001"
    d = tmp_path / "cr" / "operator" / "sniff"
    (d / ".w1-next").write_text("")                                       # torn
    r = _run(tmp_path, FILE_ARGS, session="filer")
    assert r.returncode != 0 and "never-reuse floor" in r.stderr
    assert not (d / "w1-002.json").exists()
    (d / ".w1-next").write_text("2")                                  # repaired
    assert _run(tmp_path, FILE_ARGS, session="filer").stdout.strip() == "CR-w1-002"
    assert (d / ".w1-next").read_text() == "3", \
        "the repaired filing re-reserved ahead of its record"


def test_uppercase_repo_refused(tmp_path):
    """1522 #11 r6 low: Forgejo identity is case-insensitive, our gates are
    exact-string — a case-variant repo is refused, not a parallel universe."""
    r = _run(tmp_path, ["file", "--repo", "Operator/sniff", "--lane", "w1",
                        "--target", "t", "--intent", "i"])
    assert r.returncode != 0 and "owner/name repo" in r.stderr


def test_only_untagged_fences_are_treated_as_commands():
    """The doc's contract, asserted -- a tagged fence is documentation.

    Without this the parser could quietly go back to executing prose: the naive
    regex it replaced did exactly that, and the symptom surfaced as a shell
    error attributed to whichever line of English happened to come first.
    """
    doc = ("intro\n"
           "```\n"
           "echo run-me\n"
           "```\n"
           "prose that must never be executed\n"
           "```sh\n"
           "echo illustrative-only\n"
           "```\n"
           "more prose\n"
           "```text\n"
           "sample output, not a command\n"
           "```\n")
    blocks = _runnable_blocks(doc)
    assert blocks == ["echo run-me\n"], blocks
