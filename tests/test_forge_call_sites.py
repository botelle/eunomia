"""bin/fleet-watch opens no forge connection of its own (plan 0051).

Two properties, checked statically rather than by exercising a cycle:

  * `bin/fleet-watch` builds no forge URL and makes no `urllib.request` call
    against one — every Forgejo call it makes goes through `fleetforge.Forge`,
    which owns `{base_url}/api/v1{path}` in one place (fleetforge.py's own
    module docstring). The one surviving `urllib.request` call site left in
    `bin/fleet-watch` is the ntfy notification POST inside `notify()` —
    docs/forge.md's migration ledger names it, and it is not a forge call.
  * The COMPANION-FILE SET: `bin/fleet-watch` is loaded by `SourceFileLoader`
    from two directories that are not on `sys.path` and do not contain the
    rest of `bin/` — `~/.local/share/pins/eunomia/bin`, and keyvault's
    forgejo-broker deployment on vaulthost (see the loader comment in
    `bin/fleet-watch` itself). Whatever that directory must contain for the
    watcher to import is declared here, once, so plans 0052/0053/0054/0056
    have one place to gate on rather than rediscovering it by a failed
    deployment. `fleetjob.py` joined the set in plan 0053: fleet-watch loads
    it the same way it loads fleetlib/fleetforge, for the same reason (the
    loader comment beside `_job_loader` in bin/fleet-watch).
"""
import importlib.machinery
import importlib.util
import shutil
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

# The companion-file set: everything a directory needs to contain for
# `bin/fleet-watch` to import standalone. `fleetlib.py` because fleet-watch
# loads it directly (and so does fleetforge.py, for `validate_repo`);
# `fleetforge.py` because plan 0051 is the door this file is named for;
# `fleetjob.py` because plan 0053 loads it the same way, for dispatch.
# `fleet-config` joined in plan 0068: fleet-watch loads it the same way, to
# resolve `dispatch_cap` every cycle (the `_config_loader` comment in
# bin/fleet-watch).
COMPANIONS = ("fleet-watch", "fleetlib.py", "fleetforge.py", "fleetjob.py",
             "fleet-config")


def _load_from(directory, name="fleet_watch_under_test"):
    loader = importlib.machinery.SourceFileLoader(name, str(directory / "fleet-watch"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _populate(directory, omit=()):
    for name in COMPANIONS:
        if name in omit:
            continue
        shutil.copy(BIN / name, directory / name)


# ------------------------------------------------------- no open-coded forge


def test_fleet_watch_constructs_no_forge_url():
    """`_api()` used to build `f"{FORGEJO_URL}/api/v1{path}"` itself; that
    string now appears nowhere outside fleetforge.py's own `_request`."""
    text = (BIN / "fleet-watch").read_text()
    assert "api/v1" not in text


def test_fleet_lane_constructs_no_forge_url_and_has_no_open_coded_call():
    """plan 0070: `bin/fleet-lane` is the second program in this repository
    that talks to the forge (to read eunomia's own repos.conf and to open
    the landing pull request) — the same one-door rule `fleet-watch` holds,
    checked the same way rather than assumed because it is new code with the
    most reason to get this wrong first."""
    text = (BIN / "fleet-lane").read_text()
    assert "api/v1" not in text
    assert "_api(" not in text
    assert "_get(" not in text
    assert "urlopen(" not in text
    assert "http.client" not in text


def test_fleet_watch_makes_no_urllib_request_call_against_the_forge():
    """The only `urllib.request.urlopen` call site left in bin/fleet-watch is
    the ntfy notification POST inside `notify()` — not a forge call. Every
    Forgejo call goes through `_forge(token)`, i.e. through fleetforge.Forge's
    own `_request`, which this file never open-codes again."""
    lines = (BIN / "fleet-watch").read_text().splitlines()
    urlopen_lines = [(i, l) for i, l in enumerate(lines, 1) if "urlopen(" in l]
    assert len(urlopen_lines) == 1, (
        f"expected exactly one urllib.request.urlopen call site (the ntfy "
        f"post), found {len(urlopen_lines)}: {urlopen_lines}")
    lineno, line = urlopen_lines[0]
    # forge_timeout() is the marker of a forge call (see _forge's own
    # docstring and tests/test_fleet_watch.py's literal grep for it); the
    # ntfy call uses a fixed, short timeout instead.
    assert "forge_timeout" not in line, (
        f"bin/fleet-watch:{lineno} looks like a forge call, not ntfy: {line}")


def test_fleet_watch_defines_no_api(tmp_path):
    """`_api` is gone; every one of its six former call sites now goes
    through `_forge(token)`."""
    _populate(tmp_path)
    mod = _load_from(tmp_path)
    assert not hasattr(mod, "_api")
    assert callable(mod._forge)


# ------------------------------------------------------- the companion set


def test_the_companion_set_is_sufficient(tmp_path):
    """Loading bin/fleet-watch from a directory containing ONLY the declared
    companions succeeds — the property the broker deployment and the pinned
    worktrees both depend on."""
    _populate(tmp_path)
    mod = _load_from(tmp_path)
    assert callable(mod.main_is_protected)
    assert hasattr(mod, "fleetforge")


def test_removing_fleetforge_fails_by_name_not_an_opaque_import_error(tmp_path):
    """A broker deployment missing fleetforge.py must not fail with a bare
    `ModuleNotFoundError: No module named 'fleetforge'` — that names the
    module, not the file an operator needs to go copy. SourceFileLoader
    (never `import fleetforge` — see the loader comment in bin/fleet-watch)
    fails with the actual missing PATH instead."""
    _populate(tmp_path, omit=("fleetforge.py",))
    with pytest.raises(FileNotFoundError) as exc:
        _load_from(tmp_path)
    assert "fleetforge.py" in str(exc.value)


def test_removing_fleetlib_also_fails_by_name(tmp_path):
    """The same property for fleetlib.py, which both bin/fleet-watch and
    fleetforge.py load directly."""
    _populate(tmp_path, omit=("fleetlib.py",))
    with pytest.raises(FileNotFoundError) as exc:
        _load_from(tmp_path)
    assert "fleetlib.py" in str(exc.value)


def test_removing_fleetjob_also_fails_by_name(tmp_path):
    """The same property for fleetjob.py (plan 0053): a broker deployment
    missing it must fail with the file the operator needs to go copy, not an
    opaque ModuleNotFoundError — see the `_job_loader` comment in
    bin/fleet-watch, the same shape as fleetforge's own loader."""
    _populate(tmp_path, omit=("fleetjob.py",))
    with pytest.raises(FileNotFoundError) as exc:
        _load_from(tmp_path)
    assert "fleetjob.py" in str(exc.value)


def test_removing_fleet_config_also_fails_by_name(tmp_path):
    """The same property for bin/fleet-config (plan 0068): a broker
    deployment missing it must fail with the file the operator needs to go
    copy, not an opaque ModuleNotFoundError — see the `_config_loader`
    comment in bin/fleet-watch, the same shape as the other three loaders."""
    _populate(tmp_path, omit=("fleet-config",))
    with pytest.raises(FileNotFoundError) as exc:
        _load_from(tmp_path)
    assert "fleet-config" in str(exc.value)
