"""docs/declared-behaviour.md — the half of ADR-0006 §2 that cannot be
generated (plan 0019).

Three things are pinned here, all against the LIVE source rather than a
retyped copy, because a fourth copy is a fourth thing to drift:

1. The document's name set must equal the union of `fleet-bundle`'s own
   `clis` and `gaps` targets on the day the test runs — never a count
   written into a plan or into this file.
2. The `Records` section must equal SPEC.md's three record shapes and
   `bin/fleet-emit`'s EVENT_TYPES, field-for-field.
3. The document's TEXT must clear the same egress gate
   `bin/fleet-secret-guard` and `fleetlib.validate_cr` enforce elsewhere —
   every rule in config/secret-patterns.conf, plus the host/address/path
   checks this document's own Boundaries section commits to — because this
   is the one hand-written disclosure surface in the whole bundle and has
   none of the generator's structural guarantees.

When (1) or (2) fails, the fix is a decision about which side is wrong
(SPEC.md and the live code are always the authority), never a regeneration
of the document — see docs/declared-behaviour.md's own Boundaries, and
plan 0019 Boundaries, "When the pin fails, do not regenerate the document."
"""
import importlib.machinery
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
DOC_PATH = ROOT / "docs" / "declared-behaviour.md"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fb():
    return _load("fleet_bundle_for_declared_behaviour", BIN / "fleet-bundle")


@pytest.fixture(scope="module")
def doc_text():
    return DOC_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# D2a — the name set: document vs. fleet-bundle's live report, both directions

def _doc_program_names(text):
    m = re.search(r"^## Programs\n(.*?)^## Records\n", text, re.M | re.S)
    assert m, "docs/declared-behaviour.md has no ## Programs .. ## Records span"
    return set(re.findall(r"^### (.+)$", m.group(1), re.M))


def _bundle_names(bundle):
    return {c["path"] for c in bundle["clis"]} | {g["target"] for g in bundle["gaps"]}


def _assert_names_match(doc_names, bundle_names):
    only_doc = doc_names - bundle_names
    only_bundle = bundle_names - doc_names
    assert not only_doc, (
        "docs/declared-behaviour.md names programs fleet-bundle does not report: "
        f"{sorted(only_doc)}")
    assert not only_bundle, (
        "fleet-bundle reports programs docs/declared-behaviour.md does not name: "
        f"{sorted(only_bundle)}")


def test_document_names_match_fleet_bundles_live_report(fb, doc_text):
    bundle = fb.build(ROOT)
    _assert_names_match(_doc_program_names(doc_text), _bundle_names(bundle))


def test_pin_fails_when_the_document_is_missing_a_name(fb, tmp_path):
    """One direction of the pin, on a synthetic tree: the code has a program
    the document does not name."""
    (tmp_path / "with_argparse.py").write_text(
        "import argparse\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    return ap.parse_args()\n")
    (tmp_path / "no_argparse.py").write_text("def main():\n    return 1\n")
    bundle = fb.build(tmp_path)
    bundle_names = _bundle_names(bundle)
    assert "with_argparse.py" in bundle_names and "no_argparse.py" in bundle_names

    incomplete_doc_names = bundle_names - {"no_argparse.py"}
    with pytest.raises(AssertionError, match="fleet-bundle reports programs"):
        _assert_names_match(incomplete_doc_names, bundle_names)


def test_pin_fails_when_the_document_names_something_code_lacks(fb, tmp_path):
    """The other direction, on the same shape of synthetic tree: the
    document names a program the code does not have."""
    (tmp_path / "with_argparse.py").write_text(
        "import argparse\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    return ap.parse_args()\n")
    bundle = fb.build(tmp_path)
    bundle_names = _bundle_names(bundle)

    invented_doc_names = bundle_names | {"bin/does-not-exist"}
    with pytest.raises(AssertionError, match="docs/declared-behaviour.md names"):
        _assert_names_match(invented_doc_names, bundle_names)


# --------------------------------------------------------------------------
# D2b — the Records section: document vs. SPEC.md and fleet-emit's EVENT_TYPES

def _spec_text():
    return (ROOT / "SPEC.md").read_text(encoding="utf-8")


def _spec_record_fields(heading):
    """The field set SPEC.md declares for one record, parsed from its own
    fenced ```json block (JSONC — `//` trailing comments stripped first)."""
    text = _spec_text()
    idx = text.index("## " + heading)
    m = re.search(r"```json\n(.*?)\n```", text[idx:], re.S)
    assert m, f"SPEC.md's {heading!r} section carries no fenced json block"
    block = re.sub(r"//.*$", "", m.group(1), flags=re.M)
    data = json.loads(block)
    fields = set(data.keys())
    if "resource" in data and isinstance(data["resource"], dict):
        fields |= set(data["resource"].keys())
    return fields


def _doc_record_fields(text, heading):
    """The field set docs/declared-behaviour.md declares for one record: the
    first column of the fenced table under its own `### <heading>`."""
    idx = text.index("### " + heading)
    m = re.search(r"\|---.*?\n((?:\|.+\n)+)", text[idx:])
    assert m, f"docs/declared-behaviour.md's {heading!r} entry carries no table"
    return {row.split("|")[1].strip() for row in m.group(1).strip().splitlines()}


@pytest.mark.parametrize("heading", ["Event record", "Lease record", "Change-request record"])
def test_record_fields_match_spec(doc_text, heading):
    spec_fields = _spec_record_fields(heading)
    doc_fields = _doc_record_fields(doc_text, heading)
    assert spec_fields == doc_fields, (
        f"{heading}: only in SPEC.md: {sorted(spec_fields - doc_fields)}; "
        f"only in docs/declared-behaviour.md: {sorted(doc_fields - spec_fields)}")


def _spec_event_types():
    m = re.search(r"^Event types \(v0\.2 closed set[^)]*\):\n((?:.+\n)+?)\n",
                   _spec_text(), re.M)
    assert m, "SPEC.md no longer declares the event types where this test looks"
    return set(re.findall(r"`([^`]+)`", m.group(1)))


def _fleet_emit_event_types():
    tree = __import__("ast").parse((BIN / "fleet-emit").read_text())
    for node in tree.body:
        if isinstance(node, __import__("ast").Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], __import__("ast").Name) \
                and node.targets[0].id == "EVENT_TYPES":
            return set(__import__("ast").literal_eval(node.value))
    pytest.fail("bin/fleet-emit declares no module-level EVENT_TYPES")


def _doc_event_types(text):
    idx = text.index("### Event types")
    m = re.search(r"\n\n((?:`[a-z0-9-]+`[ \n]*)+)", text[idx:])
    assert m, "docs/declared-behaviour.md's Event types entry carries no backticked list"
    return set(re.findall(r"`([^`]+)`", m.group(1)))


def test_event_types_match_spec_and_fleet_emit(doc_text):
    spec_types = _spec_event_types()
    emit_types = _fleet_emit_event_types()
    doc_types = _doc_event_types(doc_text)
    assert doc_types == spec_types, (
        f"only in SPEC.md: {sorted(spec_types - doc_types)}; "
        f"only in docs/declared-behaviour.md: {sorted(doc_types - spec_types)}")
    assert doc_types == emit_types, (
        f"only in bin/fleet-emit: {sorted(emit_types - doc_types)}; "
        f"only in docs/declared-behaviour.md: {sorted(doc_types - emit_types)}")


# --------------------------------------------------------------------------
# D5 — the egress gate: this document is a hand-written disclosure surface,
# so it gets the same mechanical checks a pasted secret would have to clear.

def _secret_rules():
    rules = {"path": [], "cmd": [], "content": []}
    for line in (ROOT / "config" / "secret-patterns.conf").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        kind, name, pattern = line.split("\t", 2)
        rules[kind].append((name, re.compile(pattern, re.I)))
    return rules


def _service_hosts():
    hosts = set()
    for line in (ROOT / "config" / "services.conf").read_text().splitlines():
        if line.startswith("service:"):
            hosts.add(line.split(":", 2)[1].strip())
    # These three run no fleet service, so they never appear in services.conf,
    # but they are still real fleet hosts and still must not be named here.
    return hosts | {"nashost", "tunnelhost", "gpuhost"}


def _egress_findings(text):
    """Every D5 violation in `text`, as (rule, line-number, line) triples.
    A pure function so both the real document and the fixture lines below
    can be run through the identical check."""
    findings = []
    rules = _secret_rules()
    all_rules = rules["path"] + rules["cmd"] + rules["content"]
    hosts = _service_hosts()
    host_re = re.compile(r"\b(" + "|".join(re.escape(h) for h in hosts) + r")\b", re.I)
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in all_rules:
            if pattern.search(line):
                findings.append((f"secret-pattern:{name}", lineno, line))
        if host_re.search(line):
            findings.append(("host-name", lineno, line))
        if "example.org" in line:
            findings.append(("example.org", lineno, line))
        if re.search(r"\b192\.168\.|\b10\.0\.", line):
            findings.append(("ip-address", lineno, line))
        if "~/bin/" in line or "~/agent/" in line:
            findings.append(("tilde-path", lineno, line))
        if "/Users/" in line or "/home/" in line:
            findings.append(("absolute-home-path", lineno, line))
    return findings


def test_document_clears_the_egress_gate(doc_text):
    findings = _egress_findings(doc_text)
    assert not findings, "egress violation(s) in docs/declared-behaviour.md: " + "; ".join(
        f"{rule} line {lineno}: {line!r}" for rule, lineno, line in findings[:5])


def test_egress_gate_catches_a_host_name():
    hosts = _service_hosts()
    host = sorted(hosts)[0]
    findings = _egress_findings(f"this line was built and deployed on {host}\n")
    assert any(rule == "host-name" for rule, _, _ in findings)


def test_egress_gate_catches_a_path_rule():
    findings = _egress_findings("the credential lives at config/secrets.env\n")
    assert any(rule.startswith("secret-pattern:env-file") for rule, _, _ in findings)


def test_egress_gate_catches_a_content_rule():
    """The case gitleaks would have covered: a token-shaped string pasted
    into prose, caught by a `content` rule rather than a `path`/`cmd` one."""
    findings = _egress_findings(
        "a stray example: ghp_ABCDEFGHIJ0123456789KLMNOPQRSTUV0000\n")
    assert any(rule.startswith("secret-pattern:github-pat") for rule, _, _ in findings)
