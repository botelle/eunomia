"""fleet-pin-advance's refusal of a deploy-owned pin (plan 0087 D5, ADR-0015 §4:
one mover per pin). The rest of the tool is covered by test_pin_advance.py; this
file reuses its fixtures rather than restating them."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pin_advance import (_ENV, _advance_origin, _head, _load,  # noqa: E402
                              _refuse_broker, _unknown_broker,
                              _pin_repo, _vouch)

_SVC = "service: {h}:lynceus supervisor=launchd domain=gui primary=x.y also=- config=-"


def _bind(tmp_path, pin):
    import socket
    host = socket.gethostname().split(".")[0].lower()
    svc = tmp_path / "services.conf"
    svc.write_text(_SVC.format(h=host) + "\n")
    conf = tmp_path / "environments.conf"
    conf.write_text(f"bind: dev:lynceus provider=homefleet repo=operator/lynceus "
                    f"service={host}:lynceus pin={pin} base_url=http://127.0.0.1:1\n")
    return conf, svc


def test_a_deploy_owned_pin_is_not_advanced_even_when_listed(tmp_path, monkeypatch):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    conf, svc = _bind(tmp_path, pin)
    monkeypatch.setenv("FLEET_ENVIRONMENTS_CONF", str(conf))
    monkeypatch.setenv("FLEET_SERVICES_CONF", str(svc))
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == []
    assert refused and "owned by a deployment" in refused[0][1]
    assert _head(pin) == before


def test_an_unowned_pin_still_advances_beside_an_owned_one(tmp_path, monkeypatch):
    work, pin = _pin_repo(tmp_path)
    work2, pin2 = _pin_repo(tmp_path, name="other")
    _advance_origin(work)
    _advance_origin(work2)
    conf, svc = _bind(tmp_path, pin)
    monkeypatch.setenv("FLEET_ENVIRONMENTS_CONF", str(conf))
    monkeypatch.setenv("FLEET_SERVICES_CONF", str(svc))
    mod = _load(tmp_path / "fleet", FLEET_PINS=f"{pin},{pin2}")
    _vouch(mod)
    advanced, refused = mod.run()
    assert [p for p, _ in advanced] == [str(pin2)]
    assert [p for p, _ in refused] == [str(pin)]


def test_an_unreadable_binding_file_refuses_every_pin(tmp_path, monkeypatch):
    work, pin = _pin_repo(tmp_path)
    _advance_origin(work)
    conf = tmp_path / "environments.conf"
    conf.mkdir()                      # exists, cannot be read as a file
    monkeypatch.setenv("FLEET_ENVIRONMENTS_CONF", str(conf))
    before = _head(pin)
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == [] and refused and "unknown" in refused[0][1]
    assert _head(pin) == before


# --- plan 0096: a verified eunomia advance runs the PIN's fleet-db-migrate ---

def _stub_migrate(pin, rc, marker):
    """Commit a bin/fleet-db-migrate stub into the origin that the pin will
    advance to, so the tool that runs is provably the new checkout's."""
    return f"#!/bin/sh\necho ran > {marker}\n" + (
        "echo 'FAILED fleet-config: boom'\n" if rc else "") + f"exit {rc}\n"


def _advance_with_migrate(work, rc, marker):
    (work / "bin").mkdir(exist_ok=True)
    tool = work / "bin" / "fleet-db-migrate"
    tool.write_text(_stub_migrate(work, rc, marker))
    tool.chmod(0o755)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "migrate"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)


def _mod_for(tmp_path, pin, repo):
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    _vouch(mod)
    mod._origin_repo = lambda path: repo
    return mod


def test_eunomia_advance_runs_the_pins_migrate_not_bins(tmp_path):
    work, pin = _pin_repo(tmp_path)
    marker = tmp_path / "ran"
    _advance_with_migrate(work, 0, marker)
    mod = _mod_for(tmp_path, pin, "operator/eunomia")
    advanced, _ = mod.run()
    assert len(advanced) == 1 and marker.read_text().strip() == "ran"
    assert [c[0] for c in mod.notify_calls] == ["Pin advanced"]


def test_non_eunomia_advance_runs_nothing(tmp_path):
    work, pin = _pin_repo(tmp_path)
    marker = tmp_path / "ran"
    _advance_with_migrate(work, 0, marker)
    mod = _mod_for(tmp_path, pin, "operator/techne")
    advanced, _ = mod.run()
    assert len(advanced) == 1 and not marker.exists()


def test_failing_migrate_pages_once_and_keeps_the_advance(tmp_path):
    work, pin = _pin_repo(tmp_path)
    marker = tmp_path / "ran"
    _advance_with_migrate(work, 1, marker)
    mod = _mod_for(tmp_path, pin, "operator/eunomia")
    advanced, _ = mod.run()
    assert len(advanced) == 1 and _head(pin) == _head(work)
    fails = [c for c in mod.notify_calls if "schema migrate failed" in c[0] + c[1]]
    assert len(fails) == 1 and "FAILED fleet-config: boom" in fails[0][1]


# --- plan 0122: --create-missing ---------------------------------------------

def _create_setup(tmp_path, monkeypatch, listed=True):
    """A bare origin served under a local clone base, a repos.conf, and a pin
    path that does not exist yet."""
    origin = tmp_path / "base" / "operator" / "thing.git"
    origin.parent.mkdir(parents=True)
    work, _ = _pin_repo(tmp_path, name="seed")
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(origin)], check=True)
    conf = tmp_path / "repos.conf"
    conf.write_text("repo: operator/thing dispatch\n" if listed else "repo: operator/x dispatch\n")
    monkeypatch.setenv("FLEET_PIN_CLONE_BASE", str(tmp_path / "base"))
    return tmp_path / "pins" / "thing", conf


def _mod(tmp_path, pin, conf):
    mod = _load(tmp_path / "fleet", FLEET_PINS=str(pin))
    mod.REPOS_CONF = conf
    return mod


def test_a_vouched_missing_pin_is_created_detached_at_main(tmp_path, monkeypatch):
    pin, conf = _create_setup(tmp_path, monkeypatch)
    mod = _mod(tmp_path, pin, conf)
    _vouch(mod)
    advanced, refused = mod.run(create_missing=True)
    assert [p for p, _ in advanced] == [str(pin)] and refused == []
    assert _head(pin)
    detached = subprocess.run(["git", "-C", str(pin), "symbolic-ref", "-q", "HEAD"])
    assert detached.returncode != 0
    assert (pin / "f").read_text() == "one\n"


def test_missing_stays_refused_without_the_flag(tmp_path, monkeypatch):
    pin, conf = _create_setup(tmp_path, monkeypatch)
    mod = _mod(tmp_path, pin, conf)
    _vouch(mod)
    advanced, refused = mod.run()
    assert advanced == [] and "missing" in refused[0][1] and not pin.exists()


def test_dry_run_creates_nothing(tmp_path, monkeypatch, capsys):
    pin, conf = _create_setup(tmp_path, monkeypatch)
    mod = _mod(tmp_path, pin, conf)
    _vouch(mod)
    mod.run(dry_run=True, create_missing=True)
    assert "would create" in capsys.readouterr().out and not pin.exists()


def test_create_is_refused_when_the_broker_refuses_or_cannot_tell(tmp_path, monkeypatch):
    pin, conf = _create_setup(tmp_path, monkeypatch)
    for setter in (_refuse_broker, _unknown_broker):
        mod = _mod(tmp_path, pin, conf)
        setter(mod)
        advanced, refused = mod.run(create_missing=True)
        assert advanced == [] and refused and not pin.exists()


def test_an_unlisted_repo_is_never_created(tmp_path, monkeypatch):
    pin, conf = _create_setup(tmp_path, monkeypatch, listed=False)
    mod = _mod(tmp_path, pin, conf)
    _vouch(mod)
    advanced, refused = mod.run(create_missing=True)
    assert advanced == [] and "repos.conf" in refused[0][1] and not pin.exists()


def test_a_non_repo_path_is_not_created_over(tmp_path, monkeypatch):
    pin, conf = _create_setup(tmp_path, monkeypatch)
    pin.mkdir(parents=True)
    mod = _mod(tmp_path, pin, conf)
    _vouch(mod)
    advanced, refused = mod.run(create_missing=True)
    assert advanced == [] and "not-a-repo" in refused[0][1]
    assert not (pin / ".git").exists()
