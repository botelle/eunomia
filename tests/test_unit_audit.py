"""Tests for `fleet-svc audit` — three registries of what runs, reconciled
(plan 0050). Run: python3 -m pytest tests/test_unit_audit.py -q
"""
import importlib.machinery
import importlib.util
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load_mod():
    loader = importlib.machinery.SourceFileLoader("fleet_svc_audit", str(BIN / "fleet-svc"))
    spec = importlib.util.spec_from_loader("fleet_svc_audit", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_mod()


def _write_plist(path, data):
    path.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_XML))


def _launchctl_stub(tmp_path, loaded_labels):
    """A stub `launchctl` whose `print <domain>` (no unit suffix) emits a
    `services = {...}` block naming each of `loaded_labels` — the exact
    shape `_domain_loaded_labels` parses, modeled on real
    `launchctl print gui/$UID` output measured on opshost."""
    stub = tmp_path / "launchctl-stub.sh"
    body = "\n".join(f'echo "    0    0    {l}"' for l in loaded_labels)
    stub.write_text(f"""#!/bin/bash
if [ "$1" = "print" ]; then
  echo "gui/501 = {{"
  echo "  services = {{"
{body}
  echo "  }}"
  echo "}}"
  exit 0
fi
exit 1
""")
    stub.chmod(0o755)
    return str(stub)


def _services_conf(tmp_path, rows):
    p = tmp_path / "services.conf"
    p.write_text("\n".join(rows) + ("\n" if rows else ""))
    return p


def _row(key, primary, also="-", config="-", supervisor="launchd", domain="gui"):
    return (f"service: {key} supervisor={supervisor} domain={domain} "
           f"primary={primary} also={also} config={config}")


def _wire(monkeypatch, tmp_path, *, launchd=None, agents=None, conf=None, loaded=()):
    launchd_dir = launchd if launchd is not None else (tmp_path / "launchd")
    agents_dir = agents if agents is not None else (tmp_path / "LaunchAgents")
    launchd_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    conf_path = conf if conf is not None else _services_conf(tmp_path, [])
    stub = _launchctl_stub(tmp_path, loaded)
    monkeypatch.setenv("FLEET_LAUNCHD_DIR", str(launchd_dir))
    monkeypatch.setenv("FLEET_LAUNCH_AGENTS_DIR", str(agents_dir))
    monkeypatch.setenv("FLEET_SERVICES_CONF", str(conf_path))
    monkeypatch.setenv("FLEET_LAUNCHCTL", stub)
    return launchd_dir, agents_dir, conf_path


_BAD_COMMENT_PLIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- a real comment with a double dash -- right here, which expat rejects -->
<plist version="1.0">
<dict>
  <key>Label</key><string>org.eunomia.widget</string>
  <key>ProgramArguments</key>
  <array><string>/bin/true</string></array>
</dict>
</plist>
"""


# --- the 2026-09-13 shape: shipped, not enrolled, not installed --------------


def test_shipped_not_enrolled_not_installed_is_reported_and_exits_nonzero(mod, tmp_path, monkeypatch):
    launchd, agents, conf = _wire(monkeypatch, tmp_path)
    _write_plist(launchd / "org.eunomia.mopsus.plist",
                {"Label": "org.eunomia.mopsus",
                 "ProgramArguments": ["/usr/bin/python3", "/x/mopsus"]})

    data = mod.audit_collect(host="opshost")
    assert len(data["units"]) == 1
    unit = data["units"][0]
    assert unit["label"] == "org.eunomia.mopsus"
    assert unit["shipped"] is True
    assert unit["installed"] is False
    assert unit["loaded"] is False
    assert unit["problem"] is True
    assert mod.audit_has_problem(data) is True

    lines = mod.render_audit(data)
    assert any("org.eunomia.mopsus" in l for l in lines)


# --- strict parsing: plistlib rejects what plutil accepts --------------------


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available")
def test_expaterror_plist_reported_and_proven_stricter_than_plutil(mod, tmp_path, monkeypatch):
    launchd, agents, conf = _wire(
        monkeypatch, tmp_path,
        conf=_services_conf(tmp_path, [_row("opshost:widget", "org.eunomia.widget")]),
        loaded=["org.eunomia.widget"])
    _write_plist(launchd / "org.eunomia.widget.plist",
                {"Label": "org.eunomia.widget", "ProgramArguments": ["/bin/true"]})
    installed_path = agents / "org.eunomia.widget.plist"
    installed_path.write_bytes(_BAD_COMMENT_PLIST)

    # plutil (and launchd) tolerate this file — proving the strict parser
    # below is doing real work, not just agreeing with a weaker check.
    r = subprocess.run(["plutil", "-lint", str(installed_path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout

    data = mod.audit_collect(host="opshost")
    unit = data["units"][0]
    assert unit["installed_error"] is not None
    assert "ExpatError" in unit["installed_error"]
    assert unit["problem"] is True
    lines = mod.render_audit(data)
    assert any("parse-error installed org.eunomia.widget" in l for l in lines)


# --- ABSENT / EMPTY / SET: three distinct answers -----------------------------


def test_absent_empty_set_are_three_distinct_states(mod):
    set_env = {"FLEET_NTFY_URL": "https://ntfy.example/x"}
    empty_env = {"FLEET_NTFY_URL": ""}
    absent_env = {}
    assert mod._kv_state(set_env, "FLEET_NTFY_URL") == "SET"
    assert mod._kv_state(empty_env, "FLEET_NTFY_URL") == "EMPTY"
    assert mod._kv_state(absent_env, "FLEET_NTFY_URL") == "ABSENT"
    assert mod._kv_state(None, "FLEET_NTFY_URL") == "ABSENT"


def test_alert_state_end_to_end_through_real_plists(mod, tmp_path, monkeypatch):
    launchd, agents, conf = _wire(
        monkeypatch, tmp_path,
        conf=_services_conf(tmp_path, [
            _row("opshost:set", "org.eunomia.set"),
            _row("opshost:empty", "org.eunomia.empty"),
            _row("opshost:absent", "org.eunomia.absent"),
        ]),
        loaded=["org.eunomia.set", "org.eunomia.empty", "org.eunomia.absent"])
    for label, ship_env, install_env in (
        ("org.eunomia.set", {"FLEET_NTFY_URL": ""}, {"FLEET_NTFY_URL": "https://ntfy.example/real"}),
        ("org.eunomia.empty", {"FLEET_NTFY_URL": ""}, {"FLEET_NTFY_URL": ""}),
        ("org.eunomia.absent", {}, {}),
    ):
        base = {"Label": label, "ProgramArguments": ["/bin/true"]}
        if ship_env:
            base = {**base, "EnvironmentVariables": ship_env}
        _write_plist(launchd / f"{label}.plist", base)
        ibase = {"Label": label, "ProgramArguments": ["/bin/true"]}
        if install_env:
            ibase = {**ibase, "EnvironmentVariables": install_env}
        _write_plist(agents / f"{label}.plist", ibase)

    data = mod.audit_collect(host="opshost")
    by_label = {u["label"]: u for u in data["units"]}
    assert by_label["org.eunomia.set"]["ntfy"] == "SET"
    assert by_label["org.eunomia.empty"]["ntfy"] == "EMPTY"
    assert by_label["org.eunomia.absent"]["ntfy"] == "ABSENT"


# --- drift, proved by class, never by a list ----------------------------------


def test_drift_classes_proved_with_an_invented_key(mod):
    invented = "FLEET_TOTALLY_INVENTED_PLACEHOLDER_KEY"

    # shipped empty, installed filled -> install-filled (information)
    assert mod._classify_env_drift({invented: ""}, {invented: "now-set"}) == \
        [(invented, "install-filled")]

    # shipped empty, installed empty/absent -> unfilled (information)
    assert mod._classify_env_drift({invented: ""}, {invented: ""}) == \
        [(invented, "unfilled")]
    assert mod._classify_env_drift({invented: ""}, {}) == \
        [(invented, "unfilled")]

    # shipped non-empty, installed different -> drifted
    assert mod._classify_env_drift({invented: "abc"}, {invented: "xyz"}) == \
        [(invented, "drifted")]

    # shipped present, installed absent -> drifted (the install LOST a key)
    assert mod._classify_env_drift({invented: "abc"}, {}) == \
        [(invented, "drifted")]

    # present only in the installed copy -> install-added (information, and
    # NOT verified as safe — a mistaken key reads exactly the same)
    assert mod._classify_env_drift({}, {invented: "added"}) == \
        [(invented, "install-added")]

    # a difference in ProgramArguments is drift
    assert mod._classify_structural_drift(
        {"ProgramArguments": ["/bin/true"]},
        {"ProgramArguments": ["/bin/false"]}) == [("ProgramArguments", "drifted")]


def test_no_environment_variables_dict_at_all_compares_clean_against_empty_installed(mod):
    assert mod._classify_env_drift(None, {}) == []
    assert mod._classify_env_drift(None, None) == []


def test_drift_end_to_end_flags_the_unit_and_is_visible_in_the_report(mod, tmp_path, monkeypatch):
    launchd, agents, conf = _wire(
        monkeypatch, tmp_path,
        conf=_services_conf(tmp_path, [_row("opshost:thing", "org.eunomia.thing")]),
        loaded=["org.eunomia.thing"])
    _write_plist(launchd / "org.eunomia.thing.plist",
                {"Label": "org.eunomia.thing",
                 "ProgramArguments": ["/bin/true", "--flag-a"]})
    _write_plist(agents / "org.eunomia.thing.plist",
                {"Label": "org.eunomia.thing",
                 "ProgramArguments": ["/bin/true", "--flag-b"]})

    data = mod.audit_collect(host="opshost")
    unit = data["units"][0]
    assert unit["drifted"] is True
    assert unit["problem"] is True
    lines = mod.render_audit(data)
    assert any(l.startswith("  drift org.eunomia.thing ProgramArguments") for l in lines)


# --- scope and exit status (D1b) ----------------------------------------------


def test_unshipped_label_parse_failure_is_reported_and_never_flips_the_exit(mod, tmp_path, monkeypatch):
    launchd, agents, conf = _wire(monkeypatch, tmp_path, loaded=["org.eunomia.lynceus"])
    (agents / "org.eunomia.lynceus.plist").write_bytes(
        _BAD_COMMENT_PLIST.replace(b"org.eunomia.widget", b"org.eunomia.lynceus"))

    data = mod.audit_collect(host="opshost")
    assert data["units"] == []
    assert len(data["out_of_scope"]) == 1
    out = data["out_of_scope"][0]
    assert out["label"] == "org.eunomia.lynceus"
    assert out["parse_error"] is not None
    assert mod.audit_has_problem(data) is False
    lines = mod.render_audit(data)
    assert any("org.eunomia.lynceus" in l for l in lines)


def test_loaded_but_unshipped_label_never_flips_the_exit(mod, tmp_path, monkeypatch):
    launchd, agents, conf = _wire(monkeypatch, tmp_path, loaded=["org.eunomia.usagebar-mirror"])
    _write_plist(agents / "org.eunomia.usagebar-mirror.plist",
                {"Label": "org.eunomia.usagebar-mirror", "ProgramArguments": ["/bin/true"]})

    data = mod.audit_collect(host="opshost")
    assert data["units"] == []
    assert data["out_of_scope"] == [{"label": "org.eunomia.usagebar-mirror", "parse_error": None}]
    assert mod.audit_has_problem(data) is False


def test_fleet_orch_lease_jobs_are_absent_from_the_report_entirely(mod, tmp_path, monkeypatch):
    leases = ["org.eunomia.fleet-orch.branch--x-0001--001",
             "org.eunomia.fleet-orch.branch--y-0002--001"]
    launchd, agents, conf = _wire(monkeypatch, tmp_path, loaded=leases)

    data = mod.audit_collect(host="opshost")
    assert data["units"] == []
    assert data["out_of_scope"] == []
    assert mod.audit_has_problem(data) is False
    lines = mod.render_audit(data)
    assert not any("fleet-orch" in l for l in lines)


def test_other_host_services_conf_row_is_informational_only(mod, tmp_path, monkeypatch):
    launchd, agents, conf = _wire(
        monkeypatch, tmp_path,
        conf=_services_conf(tmp_path, [_row("cihost:forgejo-runner", "org.eunomia.forgejo-runner")]))

    data = mod.audit_collect(host="opshost")
    assert data["other_host_rows"] == [
        {"key": "cihost:forgejo-runner", "host": "cihost", "unit": "forgejo-runner"}]
    assert data["units"] == []
    assert mod.audit_has_problem(data) is False
    lines = mod.render_audit(data)
    assert any("cihost:forgejo-runner" in l and "not audited here" in l for l in lines)


# --- D4: shipped-but-not-enrolled is information, never a fault --------------


def test_shipped_installed_loaded_not_enrolled_is_information_with_the_criterion_quoted(
        mod, tmp_path, monkeypatch):
    launchd, agents, conf = _wire(monkeypatch, tmp_path, loaded=["org.eunomia.mopsus"])
    plist = {"Label": "org.eunomia.mopsus", "ProgramArguments": ["/usr/bin/python3", "/x/mopsus"]}
    _write_plist(launchd / "org.eunomia.mopsus.plist", plist)
    _write_plist(agents / "org.eunomia.mopsus.plist", plist)

    data = mod.audit_collect(host="opshost")
    unit = data["units"][0]
    assert unit["enrolled_key"] is None
    assert unit["problem"] is False, "shipped-not-enrolled must never fail the audit"
    lines = mod.render_audit(data)
    assert any("not enrolled" in l and mod.SERVICES_CONF_CRITERION in l for l in lines)


# --- D1d: no secret VALUE ever appears, only key names and states ------------


def test_no_secret_value_appears_anywhere_in_the_report(mod, tmp_path, monkeypatch):
    ntfy_secret = "https://ntfy.sh/CANARY-DO-NOT-LEAK-9f8e7d"
    angelia_secret = "https://angelia.internal/CANARY-ANGELIA-9f8e7d"
    launchd, agents, conf = _wire(
        monkeypatch, tmp_path,
        conf=_services_conf(tmp_path, [_row("opshost:thing", "org.eunomia.thing")]),
        loaded=["org.eunomia.thing"])
    _write_plist(launchd / "org.eunomia.thing.plist",
                {"Label": "org.eunomia.thing", "ProgramArguments": ["/bin/true"],
                 "EnvironmentVariables": {"FLEET_NTFY_URL": "", "FLEET_ANGELIA_URL": ""}})
    _write_plist(agents / "org.eunomia.thing.plist",
                {"Label": "org.eunomia.thing", "ProgramArguments": ["/bin/true"],
                 "EnvironmentVariables": {"FLEET_NTFY_URL": ntfy_secret,
                                         "FLEET_ANGELIA_URL": angelia_secret}})

    data = mod.audit_collect(host="opshost")
    text = "\n".join(mod.render_audit(data))
    assert ntfy_secret not in text
    assert angelia_secret not in text
    assert "SET" in text          # the state IS reported — just never the value


# --- CLI wiring ----------------------------------------------------------------


def test_cli_audit_exits_nonzero_on_a_problem(tmp_path, monkeypatch):
    launchd = tmp_path / "launchd"; launchd.mkdir()
    agents = tmp_path / "LaunchAgents"; agents.mkdir()
    conf = _services_conf(tmp_path, [])
    _write_plist(launchd / "org.eunomia.thing.plist",
                {"Label": "org.eunomia.thing", "ProgramArguments": ["/bin/true"]})
    stub = _launchctl_stub(tmp_path, [])
    env = dict(os.environ, FLEET_LAUNCHD_DIR=str(launchd),
              FLEET_LAUNCH_AGENTS_DIR=str(agents), FLEET_SERVICES_CONF=str(conf),
              FLEET_LAUNCHCTL=stub, EUNOMIA_FLEET_DIR=str(tmp_path / "fleet"))
    env.pop("EUNOMIA_LEDGER_HOST", None)
    r = subprocess.run([sys.executable, str(BIN / "fleet-svc"), "audit"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 1, r.stdout
    assert "org.eunomia.thing" in r.stdout


def test_cli_audit_takes_no_lease(tmp_path, monkeypatch):
    """audit never requires a lease (§3 boundary) — an empty/absent ledger
    must not make it refuse, unlike every mutating verb."""
    launchd = tmp_path / "launchd"; launchd.mkdir()
    agents = tmp_path / "LaunchAgents"; agents.mkdir()
    conf = _services_conf(tmp_path, [])
    stub = _launchctl_stub(tmp_path, [])
    env = dict(os.environ, FLEET_LAUNCHD_DIR=str(launchd),
              FLEET_LAUNCH_AGENTS_DIR=str(agents), FLEET_SERVICES_CONF=str(conf),
              FLEET_LAUNCHCTL=stub, EUNOMIA_FLEET_DIR=str(tmp_path / "fleet-does-not-exist"))
    env.pop("EUNOMIA_LEDGER_HOST", None)
    r = subprocess.run([sys.executable, str(BIN / "fleet-svc"), "audit"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ledger" not in r.stderr.lower()


# --- live, on opshost itself ------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("launchctl") is None,
                    reason="needs a real launchd domain")
def test_live_on_this_host_drift_is_rare_and_fleet_watch_is_never_drifted(mod):
    data = mod.audit_collect()
    drifted = [u["label"] for u in data["units"] if u["drifted"]]
    assert len(drifted) <= 1, drifted
    fw = next((u for u in data["units"] if u["label"] == "org.eunomia.fleet-watch"), None)
    if fw is not None:
        assert fw["drifted"] is False, fw["drift_detail"]
