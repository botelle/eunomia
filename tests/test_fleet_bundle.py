"""fleet-bundle — the generated contract carries structure and nothing else.

ADR-0006's Consequences make this file load-bearing: "If it emits
implementation or prose, the property is silently gone. Its test asserts
structurally, by parsing, that no function body and no prose description appear
in its output."

So neither corpus is written down here. Both are derived by parsing the same
sources the builder read — the prose corpus from every docstring and every
`help=` / `description=` / `epilog=` literal, the body corpus from every
statement inside every function. A test that listed forbidden phrases would
pass forever while the sources moved underneath it, which is the failure mode
plan 0012 already demonstrated once.
"""
import ast
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

# Below this, a "fragment" is short enough to collide with an option name by
# accident: `return 0` is a function body line and also nothing.
MIN_FRAGMENT = 24


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fb():
    return _load("fleet_bundle", BIN / "fleet-bundle")


@pytest.fixture(scope="module")
def bundle(fb):
    """One build of this repository, shared: it spawns a child per CLI."""
    return fb.build(ROOT)


def _strings(node):
    """Every string value anywhere in the document, keys included.

    Keys too, because a builder that put prose in a key would satisfy a
    values-only check while shipping the prose.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _strings(value)


def _fragments(text):
    """The lines of `text` long enough to be evidence of copying."""
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= MIN_FRAGMENT:
            yield line


def _sources(fb):
    files = fb._cli_candidates(ROOT)
    assert files, "no sources scanned; the corpora below would be empty and pass"
    return files


def _prose_corpus(fb):
    """Docstrings and the argparse prose fields, from the AST."""
    corpus = {}
    for path in _sources(fb):
        tree = fb._parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                doc = ast.get_docstring(node)
                if doc:
                    for frag in _fragments(doc):
                        corpus[frag] = f"{path.name}: docstring"
            elif isinstance(node, ast.keyword) and node.arg in (
                    "help", "description", "epilog", "usage"):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    for frag in _fragments(node.value.value):
                        corpus[frag] = f"{path.name}: {node.arg}="
    return corpus


def _body_corpus(fb):
    """Every statement inside every function, as source text."""
    corpus = {}
    for path in _sources(fb):
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = fb._parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                body = body[1:]          # the docstring is the other corpus
            for stmt in body:
                segment = ast.get_source_segment(text, stmt)
                if not segment:
                    continue
                for frag in _fragments(segment):
                    corpus[frag] = f"{path.name}:{stmt.lineno}"
    return corpus


def _leaks(corpus, bundle):
    values = list(_strings(bundle))
    return [(frag, where, value) for frag, where in corpus.items()
            for value in values if frag in value]


def _without_declared_behaviour(bundle):
    """The bundle, minus the one section plan 0019 puts there on purpose.

    That section is a hand-written file, not source-derived, so it is out of
    scope for the two corpus checks below by construction rather than by
    exemption — leaving it in would make "no prose crosses" untestable the
    moment this document exists at all. Nothing else is excluded: a
    docstring leaking in anywhere else in the bundle still fails both tests.
    """
    return {**bundle, "declared_behaviour": None}


# --------------------------------------------------------------------------
# the two halves ADR-0006 names

def test_no_prose_description_appears(fb, bundle):
    corpus = _prose_corpus(fb)
    assert len(corpus) > 200, f"prose corpus is implausibly small ({len(corpus)})"
    leaks = _leaks(corpus, _without_declared_behaviour(bundle))
    assert not leaks, "prose reached the bundle: " + "; ".join(
        f"{where} -> {value[:80]!r}" for _, where, value in leaks[:5])


def test_no_function_body_appears(fb, bundle):
    corpus = _body_corpus(fb)
    assert len(corpus) > 500, f"body corpus is implausibly small ({len(corpus)})"
    leaks = _leaks(corpus, _without_declared_behaviour(bundle))
    assert not leaks, "implementation reached the bundle: " + "; ".join(
        f"{where} -> {value[:80]!r}" for _, where, value in leaks[:5])


# --------------------------------------------------------------------------
# the one hand-written section (plan 0019)

def test_declared_behaviour_section_is_byte_identical(bundle):
    """The one file this builder carries whole must arrive unmodified.

    Byte-identity, not "contains" or "matches roughly": anything this
    generator did to the text — even normalisation — would make the bundle
    a second copy of the document rather than a carrier of it.
    """
    on_disk = (ROOT / "docs" / "declared-behaviour.md").read_text(encoding="utf-8")
    assert bundle["declared_behaviour"] == on_disk


def test_declared_behaviour_gap_when_absent(fb, tmp_path):
    """Silence here is the dangerous output (ADR-0006 §4): the absence must
    be a gap entry, never a missing key nobody notices."""
    (tmp_path / "real.py").write_text("def main():\n    return 1\n")
    doc = fb.build(tmp_path)
    assert doc["declared_behaviour"] is None
    gap = next(g for g in doc["gaps"] if g["target"] == "docs/declared-behaviour.md")
    assert gap["reason"] == "no declared-behaviour document"


def test_workflow_comments_do_not_survive_the_scan(fb, bundle):
    """The harness section is parsed out of ci.yml, never copied from it.

    That file is forty lines of reasoning above twenty lines of steps, so a
    reader that emitted the file would ship four times more prose than
    contract. The comment lines are a corpus like any other.
    """
    ci = (ROOT / ".forgejo" / "workflows" / "ci.yml").read_text()
    comments = {line.strip().lstrip("# ").strip()
                for line in ci.splitlines() if line.strip().startswith("#")}
    comments = {c for c in comments if len(c) >= MIN_FRAGMENT}
    assert len(comments) > 20, "ci.yml lost its comments; this test proves nothing now"
    text = json.dumps(bundle)
    assert not [c for c in comments if c in text]


# --------------------------------------------------------------------------
# the surface is complete, and it is the host's business that is missing

def test_subcommands_built_in_a_loop_are_present(fb, bundle):
    """The case that separates parser introspection from a static walk.

    bin/fleet-repo builds `enable` and `disable` in a for loop over a tuple of
    (name, fn) pairs. An AST walker matching sub.add_parser("literal") finds
    `list` and `check`, reports two subcommands with total confidence, and a
    tester never exercises enrolment at all.
    """
    repo_cli = next(c for c in bundle["clis"] if c["path"] == "bin/fleet-repo")
    assert set(repo_cli["subcommands"]) == {"list", "enable", "disable", "check"}
    enable = repo_cli["subcommands"]["enable"]
    assert [a["dest"] for a in enable["arguments"]] == ["repo"]
    assert {f for opt in enable["options"] for f in opt["flags"]} == {
        "--enforce", "--dispatch"}


def test_every_scanned_file_is_reported_once(fb, bundle):
    """A file that is neither a CLI nor a gap has vanished silently.

    ADR-0006 §4 makes a gap an output. The dangerous bundle is not the
    incomplete one but the one whose incompleteness is invisible.
    """
    scanned = {str(p.relative_to(ROOT)) for p in fb._cli_candidates(ROOT)}
    reported = [c["path"] for c in bundle["clis"]] + \
               [g["target"] for g in bundle["gaps"] if g["target"] in scanned]
    assert sorted(reported) == sorted(scanned)
    assert len(reported) == len(set(reported)), "a file was reported twice"


def test_config_keys_are_names_without_values(bundle):
    """FLEET_TOKEN_CMD is the exact case ADR-0006 §2 and §6 are built around.

    Its default value is the path to a fixed-path, no-argument token helper
    that any process running as the operator can invoke, and bin/fleet-watch's
    module docstring prints it — which is why raw --help is not a contract
    artifact. The knob's existence is structure a tester needs; the path to the
    credential is not.
    """
    names = {c["name"] for c in bundle["config_keys"]}
    assert "FLEET_TOKEN_CMD" in names and "FLEET_WATCH_REPOS" in names
    for entry in bundle["config_keys"]:
        assert set(entry) == {"name", "read_by"}
    assert "fetch-forgejo-token.sh" not in json.dumps(bundle)


def test_no_host_path_survives(bundle):
    """Defaults are evaluated by introspection, so they carry this machine."""
    home = str(Path.home())
    for value in _strings(bundle):
        assert home not in value, f"home directory in bundle: {value!r}"
        assert str(ROOT) not in value, f"checkout path in bundle: {value!r}"


def test_declared_vocabularies_carry_the_event_names(bundle):
    events = next(v for v in bundle["vocabularies"]
                  if v["name"] == "EVENT_TYPES" and v["source"] == "bin/fleet-emit")
    assert "plan-dispatched" in events["values"] and "pr-merged" in events["values"]


def test_gaps_name_unrepresented_languages(fb, tmp_path):
    """A repo this builder cannot describe must say so, not return empty.

    Asserted on a synthetic tree rather than on eunomia, which is all Python:
    the silent case only appears where the extractors have nothing to find.
    """
    (tmp_path / "Game.swift").write_text('let a = 1  // accessibilityIdentifier\n'
                                         'view.accessibilityIdentifier = "start"\n')
    doc = fb.build(tmp_path)
    assert doc["clis"] == []
    swift = next(g for g in doc["gaps"] if g["target"] == "Swift sources")
    assert "no extractor" in swift["reason"] and "accessibilityIdentifier" in swift["reason"]
    assert any(g["target"] == "openapi" for g in doc["gaps"])


def test_text_render_shows_nothing_the_json_lacks(fb, bundle):
    """The readable form is a reading of the document, not a second extractor."""
    text = fb._render_text(bundle)
    corpus = _prose_corpus(fb)
    assert not [f for f in corpus if f in text]


# --------------------------------------------------------------------------
# the workflow scanner, on the shapes the second repository actually uses

def test_workflow_scanner_shapes(fb):
    """Three failures found by running this against ares rather than eunomia.

    A phantom job (`on:` carries two-space keys that read as job ids), a step
    counted twice (`- name:` over `uses:` is one step), and prose riding on a
    structure line (`runs-on: cihost  # self-hosted ...`). Each was invisible in
    eunomia, whose ci.yml happens not to write any of them.
    """
    wf = fb._scan_workflow(
        "name: CI\n"
        "# a comment that is forty characters of reasoning\n"
        "on:\n"
        "  push:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: cihost        # self-hosted runner with the toolchain\n"
        "    steps:\n"
        "      - name: Checkout\n"
        "        uses: actions/checkout@v4\n"
        "      - name: test\n"
        "        run: |\n"
        "          swift test\n"
        "          # a shell comment\n"
        "          swift build\n")
    assert wf["name"] == "CI"
    assert [j["id"] for j in wf["jobs"]] == ["build"], "on: produced a phantom job"
    job = wf["jobs"][0]
    assert job["runs_on"] == "cihost", "an inline comment rode in on the scalar"
    assert len(job["steps"]) == 2, "name over uses was counted as two steps"
    assert job["steps"][0] == {"step": "Checkout", "uses": "actions/checkout@v4"}
    assert job["steps"][1]["run"] == ["swift test", "swift build"]


# --------------------------------------------------------------------------
# the child's environment

def test_env_derived_default_does_not_carry_the_secret(fb, tmp_path, monkeypatch):
    """Introspection evaluates defaults, so the environment is a leak path.

    `default=os.environ.get("X_TOKEN")` puts the secret's VALUE, not its name,
    into a document ADR-0006 §2 sends to two cloud vendors — defeating this
    builder's own rule that config keys are names and never values, by a route
    that never touches the config-key extractor.

    The three assertions are one property each: the value is gone, the option
    is still declared, and the key is still named. A fix that dropped the
    option or the key would pass a value-only check while destroying the
    contract it exists to carry.
    """
    secret = "sk-live-0000-DO-NOT-EMIT"
    monkeypatch.setenv("X_TOKEN", secret)
    (tmp_path / "svc.py").write_text(
        "import argparse, os\n"
        "def main(argv=None):\n"
        "    ap = argparse.ArgumentParser(prog='svc')\n"
        "    ap.add_argument('--token', default=os.environ.get('X_TOKEN'))\n"
        "    return ap.parse_args(argv)\n")
    doc = fb.build(tmp_path)

    assert secret not in json.dumps(doc)
    cli = next(c for c in doc["clis"] if c["path"] == "svc.py")
    assert [f for opt in cli["options"] for f in opt["flags"]] == ["--token"]
    assert "X_TOKEN" in {c["name"] for c in doc["config_keys"]}


def test_child_env_is_an_allowlist(fb, monkeypatch):
    """Named separately from the behaviour above: a denylist passes that test.

    Enumerating what to withhold is wrong once and then silently, which is the
    reasoning bin/fleet-candidate records for the same decision.
    """
    monkeypatch.setenv("FLEET_TOKEN_CMD", "/should/not/reach/the/child")
    monkeypatch.setenv("SOME_FUTURE_SECRET", "nor-this")
    env = fb._child_env()
    assert set(env) <= set(fb._CHILD_ENV_ALLOW) | {"PYTHONDONTWRITEBYTECODE",
                                                   "FLEET_BUNDLE_INTROSPECTING"}
    assert "FLEET_TOKEN_CMD" not in env and "SOME_FUTURE_SECRET" not in env


# --------------------------------------------------------------------------
# defaults: declared, or marked

def test_only_source_literals_are_emitted_as_defaults(fb, tmp_path, monkeypatch):
    """The env allowlist closed one route to a secret; this closes the class.

    Introspection evaluates default expressions, so what a default computes
    FROM is unbounded — environment, file, clock, host. Rather than enumerate
    those, emit a default only where the source wrote a literal. The env case
    is kept alongside a file read precisely because the previous fix would pass
    the first and fail the second.
    """
    monkeypatch.setenv("X_TOKEN", "sk-live-FROM-THE-ENVIRONMENT")
    (tmp_path / "secret.txt").write_text("sk-live-FROM-A-FILE")
    (tmp_path / "svc.py").write_text(
        "import argparse, os\n"
        "from pathlib import Path\n"
        "HERE = Path(__file__).resolve().parent\n"
        "def main(argv=None):\n"
        "    ap = argparse.ArgumentParser(prog='svc')\n"
        "    ap.add_argument('--kept', default=8080)\n"
        "    ap.add_argument('--also-kept', default='battle')\n"
        "    ap.add_argument('--from-env', default=os.environ.get('X_TOKEN'))\n"
        "    ap.add_argument('--from-file', default=(HERE / 'secret.txt').read_text())\n"
        "    ap.add_argument('--from-host', default=str(HERE / 'runs'))\n"
        "    return ap.parse_args(argv)\n")

    doc = fb.build(tmp_path)
    text = json.dumps(doc)
    assert "sk-live-FROM-THE-ENVIRONMENT" not in text
    assert "sk-live-FROM-A-FILE" not in text

    by_flag = {opt["flags"][0]: opt for opt in
               next(c for c in doc["clis"] if c["path"] == "svc.py")["options"]}
    assert by_flag["--kept"]["default"] == 8080
    assert by_flag["--also-kept"]["default"] == "battle"
    for flag in ("--from-env", "--from-file", "--from-host"):
        assert by_flag[flag]["default"] == fb.COMPUTED, flag
    # the surface is not lost, only the values
    assert set(by_flag) == {"--kept", "--also-kept", "--from-env", "--from-file",
                            "--from-host"}


def test_a_literal_does_not_vouch_for_another_subcommand(fb, tmp_path):
    """Matching is by name AND value: `--repo` recurs across fleet-cr's subparsers.

    A name-only match would let the literal in one subparser certify a computed
    default of the same name in another, which is the quiet version of this bug
    rather than a hypothetical one.
    """
    (tmp_path / "svc.py").write_text(
        "import argparse, os\n"
        "def main(argv=None):\n"
        "    ap = argparse.ArgumentParser(prog='svc')\n"
        "    sub = ap.add_subparsers(dest='cmd')\n"
        "    a = sub.add_parser('a'); a.add_argument('--repo', default='literal')\n"
        "    b = sub.add_parser('b'); b.add_argument('--repo', default=os.environ.get('PATH'))\n"
        "    return ap.parse_args(argv)\n")
    subs = next(c for c in fb.build(tmp_path)["clis"]
                if c["path"] == "svc.py")["subcommands"]
    assert subs["a"]["options"][0]["default"] == "literal"
    assert subs["b"]["options"][0]["default"] == fb.COMPUTED


def test_normalisation_is_inert(fb, bundle):
    """The backstop must never fire; if it does, the primary mechanism has a hole.

    ADR-0006 §2 prefers generating over filtering because a filter is wrong
    once. _normalise is the one filter left in this builder, and this pins it
    to doing nothing on a real repository rather than leaving it as comfort.
    """
    again = fb._normalise(bundle, str(ROOT), str(Path.home()))
    assert json.dumps(again, sort_keys=True) == json.dumps(bundle, sort_keys=True)


# --------------------------------------------------------------- vendored trees

def _venv_shaped(dirpath):
    """The marker PEP 405 puts at a virtualenv root, plus a module to find."""
    dirpath.mkdir(parents=True)
    (dirpath / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.9.6\n")
    site = dirpath / "lib" / "python3.9" / "site-packages"
    site.mkdir(parents=True)
    (site / "vendored.py").write_text("def helper():\n    return 'vendored'\n")


def test_a_virtualenv_is_skipped_whatever_it_is_called(fb, tmp_path):
    """`_SKIP_DIRS` is a NAME list holding `.venv` and `venv`. Every virtualenv
    this repo's tooling makes is one of those two, so the gap is invisible here
    and shows up in someone else's tree: `env`, `venv39`, `.direnv`, or a scratch
    copy made beside a real one. Then a thousand vendored modules enter the
    corpus the bundle is built from and are presented as this project's
    command-line surface.

    Structure, not spelling — the same distinction test_ci_workflow.py had to
    make about the concurrency group."""
    (tmp_path / "real.py").write_text("def main():\n    return 1\n")
    _venv_shaped(tmp_path / "env39")

    names = {p.name for p in fb._cli_candidates(tmp_path)}
    assert "real.py" in names, "the control file was not scanned; the test proves nothing"
    assert "vendored.py" not in names, (
        "a virtualenv named something other than .venv/venv was walked, and its "
        "site-packages reached the bundle corpus")


def test_the_named_skips_still_hold(fb, tmp_path):
    """The structural check is an addition, not a replacement: `.venv` with no
    pyvenv.cfg in it (an interrupted `python3 -m venv`, say) must still be
    skipped by name."""
    (tmp_path / "real.py").write_text("def main():\n    return 1\n")
    half_made = tmp_path / ".venv" / "lib"
    half_made.mkdir(parents=True)
    (half_made / "halfway.py").write_text("x = 1\n")

    names = {p.name for p in fb._cli_candidates(tmp_path)}
    assert "real.py" in names
    assert "halfway.py" not in names
