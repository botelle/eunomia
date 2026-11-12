"""fleet-drift — a deployed service that stays behind main pages once (plan 0098).

A real local HTTP stub stands in for /healthz; the clock is injected, never slept.
"""
import http.server
import importlib.machinery
import importlib.util
import json
import plistlib
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
T0 = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def fd(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet"))
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    monkeypatch.delenv("FLEET_ANGELIA_URL", raising=False)
    return _load("fleet_drift", BIN / "fleet-drift")


class Stub:
    def __init__(self):
        self.status, self.body, self.delay = 200, b"{}", 0
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if stub.delay:
                    import time
                    time.sleep(stub.delay)
                self.send_response(stub.status)
                self.end_headers()
                try:
                    self.wfile.write(stub.body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/healthz"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def set(self, status=200, body=b"{}"):
        self.status, self.body = status, body if isinstance(body, bytes) else json.dumps(body).encode()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.srv.shutdown()


def pages(fd):
    p = fd.orch.pages_path()
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def runs(fd, entry, samples):
    """samples: [(hours_from_T0, obs)] — each run fetches the given obs."""
    for h, obs in samples:
        fd.run([entry], T0 + timedelta(hours=h), fetch=lambda url, o=obs: o)


ENTRY = {"name": "edgehost:minos", "url": "http://x/", "allow": 24.0}
BEHIND = ("behind", "", "aaaa", "bbbb")
CURRENT = ("current", "", None, None)
UNKNOWN = ("unknown", "timeout", None, None)


# --- classification against a real server ------------------------------------

@pytest.mark.parametrize("status,body,want", [
    (200, {"up_to_date": True}, ("current", "")),
    (200, {"up_to_date": False}, ("behind", "")),
    (200, {"ok": True}, ("unknown", "field-missing")),
    (500, b"boom", ("unknown", "http-500")),
    (200, b"not json", ("unknown", "bad-json")),
])
def test_classification(fd, stub, status, body, want):
    stub.set(status, body)
    assert fd.sample(stub.url)[:2] == want


def test_timeout_is_unknown(fd, stub, monkeypatch):
    monkeypatch.setattr(fd, "TIMEOUT", 0.2)
    stub.delay = 1
    assert fd.sample(stub.url)[:2] == ("unknown", "timeout")


def test_unreachable(fd):
    assert fd.sample("http://127.0.0.1:1/healthz")[:2] == ("unknown", "unreachable")


# --- grace, spells, wording --------------------------------------------------

def test_grace_then_one_page_then_clear(fd):
    runs(fd, ENTRY, [(0, BEHIND), (23, BEHIND)])
    assert pages(fd) == []
    runs(fd, ENTRY, [(25, BEHIND)] + [(25 + i, BEHIND) for i in range(1, 11)])
    p = pages(fd)
    assert len(p) == 1
    assert "edgehost:minos behind main for 25h (running aaaa, main bbbb)" in p[0]["body"]
    runs(fd, ENTRY, [(40, CURRENT)])
    assert len(pages(fd)) == 2
    assert "edgehost:minos current again" in pages(fd)[1]["body"]


def test_flap_keeps_one_spell(fd):
    runs(fd, ENTRY, [(0, BEHIND), (20, BEHIND), (21, UNKNOWN), (22, BEHIND)])
    assert pages(fd) == []
    runs(fd, ENTRY, [(24.5, UNKNOWN)])
    p = pages(fd)
    assert len(p) == 1 and "cannot tell for 24h" in p[0]["body"]  # 24.5 -> "24"


def test_page_wording_follows_latest_state(fd):
    runs(fd, ENTRY, [(0, UNKNOWN), (25, BEHIND)])
    assert "behind main for 25h" in pages(fd)[0]["body"]


def test_never_paged_spell_ends_silently(fd):
    runs(fd, ENTRY, [(0, BEHIND), (2, BEHIND), (3, CURRENT)])
    assert pages(fd) == []
    assert json.loads(fd._state_path("edgehost:minos").read_text())["spell_since"] is None


def test_unknown_pages_with_reason(fd):
    runs(fd, ENTRY, [(0, UNKNOWN), (25, UNKNOWN)])
    p = pages(fd)
    assert len(p) == 1
    assert "cannot tell for 25h — timeout" in p[0]["body"]


def test_unit_on_every_page_row(fd):
    runs(fd, ENTRY, [(0, BEHIND), (25, BEHIND), (26, CURRENT)])
    p = pages(fd)
    assert len(p) == 2
    assert all(r["unit"] == "org.eunomia.fleet-drift" for r in p)


# --- bounded reason ----------------------------------------------------------

def test_long_body_never_reaches_a_page(fd, stub):
    stub.set(200, b"SECRETTEXT " * 1000)
    obs = fd.sample(stub.url)
    assert obs[:2] == ("unknown", "bad-json")
    stub.set(500, b"SECRETTEXT " * 1000)
    assert fd.sample(stub.url)[:2] == ("unknown", "http-500")
    runs(fd, ENTRY, [(0, obs), (25, obs)])
    assert "SECRETTEXT" not in json.dumps(pages(fd))


def test_long_connection_error_message_is_not_carried(fd, monkeypatch):
    def boom(*a, **k):
        raise OSError("X" * 500)
    monkeypatch.setattr(fd.urllib.request, "urlopen", boom)
    assert fd.sample("http://x/") == ("unknown", "unreachable", None, None)


def test_hostile_shas_are_not_carried(fd, stub):
    stub.set(200, {"up_to_date": False, "running_sha": "evil text", "main_sha": "ab12"})
    assert fd.sample(stub.url) == ("behind", "", "?", "ab12")


# --- dry run -----------------------------------------------------------------

def test_dry_run_pages_and_writes_nothing(fd, capsys):
    fd.run([ENTRY], T0, dry_run=True, fetch=lambda u: BEHIND)
    fd.run([ENTRY], T0 + timedelta(hours=99), dry_run=True, fetch=lambda u: BEHIND)
    assert pages(fd) == []
    assert not (fd.lib.fleet_dir() / "drift").exists()
    assert "edgehost:minos behind" in capsys.readouterr().out


# --- config ------------------------------------------------------------------

@pytest.fixture
def conf(tmp_path, monkeypatch):
    svc = tmp_path / "services.conf"
    svc.write_text("service: edgehost:minos supervisor=systemd domain=system "
                   "primary=minos.service also=- config=-\n")
    monkeypatch.setenv("FLEET_SERVICES_CONF", str(svc))

    def write(text):
        p = tmp_path / "deployed.conf"
        p.write_text(text)
        return p
    return write


def test_conf_ok(fd, conf):
    rows = fd.load_conf(conf("edgehost:minos http://edgehost:8790/healthz allow=6\n"))
    assert rows == [{"name": "edgehost:minos", "url": "http://edgehost:8790/healthz", "allow": 6.0}]
    assert fd.load_conf(conf("edgehost:minos http://h/\n"))[0]["allow"] == 24.0


@pytest.mark.parametrize("text", [
    "edgehost:nope http://h/\n",
    "edgehost:minos http://h/\nedgehost:minos http://h/\n",
    "edgehost:minos http://h/ allow=abc\n",
    "edgehost:minos http://h/ allow=-1\n",
    "edgehost:minos\n",
    "edgehost:minos ftp://h/\n",
    "edgehost:minos http://h/ extra=1\n",
])
def test_conf_refused(fd, conf, text):
    with pytest.raises(fd.ConfigError):
        fd.load_conf(conf(text))


def test_shipped_conf_parses(fd, monkeypatch):
    monkeypatch.delenv("FLEET_SERVICES_CONF", raising=False)
    rows = fd.load_conf(ROOT / "config" / "deployed.conf")
    assert [r["name"] for r in rows] == ["edgehost:minos"]


# --- unit --------------------------------------------------------------------

def test_unit_runs_the_pinned_path_every_900s():
    if shutil.which("plutil") is None:
        pytest.skip("plutil is macOS-only")
    out = subprocess.run(["plutil", "-convert", "xml1", "-o", "-",
                          str(ROOT / "launchd" / "org.eunomia.fleet-drift.plist")],
                         capture_output=True, check=True)
    d = plistlib.loads(out.stdout)
    assert d["Label"] == "org.eunomia.fleet-drift"
    assert d["ProgramArguments"][1] == \
        "/Users/operator/.local/share/pins/eunomia/bin/fleet-drift"
    assert d["StartInterval"] == 900
