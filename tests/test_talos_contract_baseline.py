"""The judgement in tests/test_talos_contract_live.py, without a forge or talos.

Plan 0117. A throwaway git repo holds stub programs in `bin/`: a
`fleet-db-migrate` that creates a table, a `fleet-models` that succeeds, and a
`caller` that reads the table and answers. Head's bin/ is a separate directory,
exactly as in the job where head is the checkout and the baseline is a clone."""
import json
import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import test_talos_contract_live as live  # noqa: E402

ENTRY = {"id": "stub-call", "program": "caller", "argv": [],
         "accept": {"rule": "json-object-value-in", "values": ["x"]}}
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _migrate(table, fail=False):
    if fail:
        return "#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n"
    return ("#!/usr/bin/env python3\nimport os, sqlite3\n"
            "c = sqlite3.connect(os.environ['FLEET_DB'])\n"
            f"c.execute('create table if not exists {table} (v text)')\n"
            "c.commit()\n")


def _caller(table=None, exit_code=0, value="x"):
    if table is None:
        return f"#!/usr/bin/env python3\nimport sys\nprint('{{\"value\": \"{value}\"}}')\nsys.exit({exit_code})\n"
    return ("#!/usr/bin/env python3\nimport os, sqlite3, sys\n"
            "try:\n"
            f"    sqlite3.connect(os.environ['FLEET_DB']).execute('select * from {table}')\n"
            "except sqlite3.Error as e:\n"
            "    print(e, file=sys.stderr)\n"
            "    sys.exit(1)\n"
            f"print('{{\"value\": \"{value}\"}}')\n")


STUB_MODELS = "#!/bin/sh\nexit 0\n"


def _write_bin(directory, files):
    (directory / "bin").mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        p = directory / "bin" / name
        p.write_text(text)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _stubs(table="t", caller=None, migrate=None):
    return {"fleet-db-migrate": migrate or _migrate(table),
            "fleet-models": STUB_MODELS,
            "caller": caller if caller is not None else _caller(table)}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A repo whose main holds the baseline stubs, and a head bin/ dir."""
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    head_db = tmp_path / "head.db"
    monkeypatch.setenv("FLEET_DB", str(head_db))

    class W:
        pass
    w = W()
    w.tmp, w.src, w.head_bin, w.head_db = tmp_path, src, tmp_path / "head", head_db

    def commit(files, branch=None):
        if branch:
            _git(src, "checkout", "-q", "-B", branch)
        _write_bin(src, files)
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "--allow-empty", "-m", "c")
        return _git(src, "rev-parse", "HEAD")

    def head(files, seed=True):
        _write_bin(w.head_bin, files)
        if seed:
            subprocess.run([str(w.head_bin / "bin" / "fleet-db-migrate")], check=True)

    def baseline(head_sha, source=None, runner="r"):
        return live.Baseline(f"file://{source or src}", head_sha, tmp_path / "work", runner)

    def run(bl, head_sha=None):
        (tmp_path / "work").mkdir(exist_ok=True)
        return live.judge(ENTRY, None, w.head_bin / "bin", bl, "talossha")

    w.commit, w.head, w.baseline, w.run = commit, head, baseline, run
    return w


def test_1_both_fail_passes_with_warning(world):
    base = world.commit(_stubs(caller=_caller(exit_code=1)))
    head = world.commit(_stubs(caller=_caller(exit_code=1)), branch="b")
    world.head(_stubs(caller=_caller(exit_code=1)))
    warning = world.run(world.baseline(head))
    assert warning.startswith("::warning::talos-contract: stub-call fails at baseline ")
    assert base in warning and "talos talossha" in warning


def test_2_baseline_answers_fails(world):
    base = world.commit(_stubs())
    head = world.commit(_stubs(), branch="b")
    world.head(_stubs(caller=_caller(exit_code=1)))
    with pytest.raises(AssertionError, match=f"baseline {base} answers"):
        world.run(world.baseline(head))


def test_3_wrong_answer_at_head_is_never_excused(world):
    world.commit(_stubs(caller=_caller(exit_code=1)))
    head = world.commit(_stubs(caller=_caller(exit_code=1)), branch="b")
    world.head(_stubs(caller=_caller(value="wrong")))
    bl = world.baseline(head)
    with pytest.raises(AssertionError, match="not in"):
        world.run(bl)
    assert bl.clones == 0


def test_4_program_absent_at_baseline_passes_with_warning(world):
    world.commit({"fleet-db-migrate": _migrate("t"), "fleet-models": STUB_MODELS})
    head = world.commit(_stubs(caller=_caller(exit_code=1)), branch="b")
    world.head(_stubs(caller=_caller(exit_code=1)))
    assert "::warning::" in world.run(world.baseline(head))


def test_5_baseline_runs_against_its_own_database(world):
    world.commit(_stubs(table="t"))
    head = world.commit(_stubs(table="t2"), branch="b")
    # head's db has t2 (head's migrate); head's caller reads a table that is gone
    world.head(_stubs(table="t2", caller=_caller("t3")))
    with pytest.raises(AssertionError, match="answers this call"):
        world.run(world.baseline(head))


def test_6_pass_at_head_never_clones_baseline(world):
    world.commit(_stubs())
    head = world.commit(_stubs(), branch="b")
    world.head(_stubs())
    bl = world.baseline(head)
    assert world.run(bl) is None
    assert bl.clones == 0


@pytest.mark.parametrize("how", ["no such sha", "clone fails", "seed fails"])
def test_7_unresolvable_baseline_fails_closed(world, how):
    if how == "seed fails":
        world.commit(_stubs(migrate=_migrate("t", fail=True)))
    else:
        world.commit(_stubs())
    head = world.commit(_stubs(migrate=_migrate("t", fail=how == "seed fails")), branch="b")
    world.head(_stubs(caller=_caller(exit_code=1)))
    bl = world.baseline("0" * 40 if how == "no such sha" else head,
                        source=world.tmp / "missing" if how == "clone fails" else None)
    with pytest.raises(AssertionError, match="failing closed"):
        world.run(bl)
    # and the failure is remembered, not retried per case
    n = bl.clones
    with pytest.raises(AssertionError):
        world.run(bl)
    assert bl.clones == n


def test_8_baseline_selection(world):
    main1 = world.commit(_stubs())
    branch = world.commit(_stubs(caller=_caller(value="y")), branch="b")
    _git(world.src, "checkout", "-q", "main")
    main2 = world.commit(_stubs(caller=_caller(value="z")))
    clone = world.tmp / "clone"
    _git(world.tmp, "clone", "-q", str(world.src), str(clone))
    assert live.choose_baseline(clone, branch) == main1   # merge-base
    assert live.choose_baseline(clone, main2) == main1    # on main: first parent
