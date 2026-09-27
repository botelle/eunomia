"""fleet-pin-advance's refusal of a deploy-owned pin (plan 0087 D5, ADR-0015 §4:
one mover per pin). The rest of the tool is covered by test_pin_advance.py; this
file reuses its fixtures rather than restating them."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pin_advance import (_ENV, _advance_origin, _head, _load,  # noqa: E402
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
