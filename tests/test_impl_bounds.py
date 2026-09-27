"""Tests for the implementer's per-repo wall-clock bound (plan 0042).

`impl_bound()` deliberately does not mirror `review_bound()` all the way: a
conf with no entry for the repo AND no `default` line is a Stop, not a silent
fallback to a hardcoded constant — see plan 0042 §3, "one home for the
default, and it is the conf". `implementer_timeout_minutes()` layers a
resolve-once cache on top, because a conf edited mid-run must not change what
a fix round's timeout message quotes (plan 0042 §2/§4).
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


def _load(tmp_path, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    for k in ("EUNOMIA_SESSION", "EUNOMIA_IMPL_TIMEOUT"):
        os.environ.pop(k, None)
    os.environ["EUNOMIA_SESSION"] = "impl-bound-test"
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader(
        "orchestrator_impl", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator_impl", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _bounds(tmp_path, text, name="impl-bounds.conf"):
    p = tmp_path / name
    p.write_text(text)
    return p


# ------------------------------------------------------------------- impl_bound
def test_the_bound_is_read_per_repo(tmp_path):
    mod = _load(tmp_path)
    p = _bounds(tmp_path, "default = 90\noperator/ares = 150\n")
    assert mod.impl_bound("operator/ares", p) == (150, "repo-entry")
    assert mod.impl_bound("operator/other", p) == (90, "conf-default")


def test_a_value_above_the_ceiling_is_clamped_and_logged(tmp_path, capsys):
    mod = _load(tmp_path)
    p = _bounds(tmp_path, f"default = {mod.IMPL_BOUND_CEILING + 100}\n")
    assert mod.impl_bound("operator/x", p) == (mod.IMPL_BOUND_CEILING, "conf-default")
    assert "clamped" in capsys.readouterr().err


def test_zero_is_refused_rather_than_read_as_unlimited(tmp_path):
    mod = _load(tmp_path)
    with pytest.raises(mod.Stop) as e:
        mod.impl_bound("operator/x", _bounds(tmp_path, "default = 0\n"))
    assert "unlimited" in str(e.value)


def test_an_unparseable_bound_stops_rather_than_defaulting(tmp_path):
    mod = _load(tmp_path)
    for bad in ("default 5\n", "default = many\n"):
        with pytest.raises(mod.Stop):
            mod.impl_bound("operator/x", _bounds(tmp_path, bad))


def test_a_missing_bounds_file_stops(tmp_path):
    mod = _load(tmp_path)
    with pytest.raises(mod.Stop) as e:
        mod.impl_bound("operator/x", tmp_path / "nope.conf")
    assert "unbounded" in str(e.value)


def test_no_default_line_and_no_repo_entry_stops(tmp_path):
    """The deliberate divergence from review_bound (plan 0042 §3/§4): a conf
    that answers neither the repo nor `default` is exactly the same
    broken-checkout evidence a missing file is, so it stops rather than
    reverting to a built-in constant the way review_bound does."""
    mod = _load(tmp_path)
    p = _bounds(tmp_path, "operator/other = 30\n")
    with pytest.raises(mod.Stop) as e:
        mod.impl_bound("operator/x", p)
    assert str(p) in str(e.value)
    assert "default" in str(e.value)


def test_the_shipped_conf_parses_and_is_within_the_ceiling(tmp_path):
    """The file that actually ships, not a fixture of it."""
    mod = _load(tmp_path)
    n, source = mod.impl_bound("operator/eunomia")
    assert 1 <= n <= mod.IMPL_BOUND_CEILING
    assert source == "conf-default"


# ------------------------------------------------- resolved once, module-wide
def test_resolved_once_and_cached_across_calls(tmp_path):
    mod = _load(tmp_path)
    mod.IMPL_BOUNDS = _bounds(tmp_path, "default = 12\n")
    assert mod.implementer_timeout_minutes("operator/x") == 12
    assert mod.implementer_timeout_source() == "conf-default"


def test_a_conf_edit_mid_run_does_not_move_the_resolved_value(tmp_path):
    """Every comment site and the plan-failed reason call
    `implementer_timeout_minutes()` again later in the same run — this pins
    that a conf edit landing between those calls changes nothing they quote."""
    mod = _load(tmp_path)
    p = _bounds(tmp_path, "default = 12\n")
    mod.IMPL_BOUNDS = p
    assert mod.implementer_timeout_minutes("operator/x") == 12
    p.write_text("default = 999\n")
    assert mod.implementer_timeout_minutes("operator/x") == 12
    # Even a different repo argument does not re-resolve: one bound per run.
    assert mod.implementer_timeout_minutes("operator/other") == 12


def test_repo_entry_is_resolved_at_first_call(tmp_path):
    mod = _load(tmp_path)
    mod.IMPL_BOUNDS = _bounds(tmp_path, "default = 90\noperator/ares = 150\n")
    assert mod.implementer_timeout_minutes("operator/ares") == 150
    assert mod.implementer_timeout_source() == "repo-entry"


def test_the_env_var_is_ignored_even_when_malformed(tmp_path):
    """`EUNOMIA_IMPL_TIMEOUT` is removed, not demoted (plan 0042 §2) — the conf
    decides, and a garbage value in the environment must not even be looked
    at, let alone raise."""
    mod = _load(tmp_path, EUNOMIA_IMPL_TIMEOUT="90m")
    mod.IMPL_BOUNDS = _bounds(tmp_path, "default = 42\n")
    assert mod.implementer_timeout_minutes("operator/x") == 42


def test_a_missing_conf_is_a_stop_not_a_quiet_ninety(tmp_path):
    mod = _load(tmp_path)
    mod.IMPL_BOUNDS = tmp_path / "nope.conf"
    with pytest.raises(mod.Stop):
        mod.implementer_timeout_minutes("operator/x")
