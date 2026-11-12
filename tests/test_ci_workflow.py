"""The CI workflow's concurrency key, guarded.

A YAML file gets no test by default, and this one shipped a concurrency block
whose comment declared a race fixed while the expression grouped nothing. That
is the expensive kind of wrong: merged, it retires the question, and the next
red build on identical code reads as already-handled.

Since plan 0113, the group is `eunomia-${{ github.ref }}` — one lane per
branch, not one lane for the whole repo — and the tests below guard that every
workflow shares that exact expression, that none narrows past it (a sha,
`head_ref`, `run_id` or `workflow` key), and that none of them cancels a
running job in its own lane.

No PyYAML in CI (the job installs pytest and pytest-randomly only), so this
reads the file as text and asserts the property rather than the spelling."""
import ast
import re
from pathlib import Path

WF = Path(__file__).resolve().parent.parent / ".forgejo" / "workflows"
CI = WF / "ci.yml"
PLANS = WF / "plans.yml"
# Globbed, not hand-listed: a tuple naming two files let a THIRD workflow escape
# every assertion below, and a third workflow declaring `${{ github.workflow }}`
# is a second parallel lane into one workspace — run 21's hazard by a new route,
# with nothing failing. Review 2147.
WORKFLOWS = tuple(sorted(WF.glob("*.yml")))


def _step_run(path, name):
    """The `run:` body of the named step, dedented."""
    m = re.search(rf"^\s+- name: {re.escape(name)}\n(?:\s+\w[\w-]*:.*\n)*?\s+run: \|\n((?:\s{{10}}.*\n|\n)+)",
                  path.read_text(), re.M)
    assert m, f"no step named {name!r} with a run block in {path.name}"
    return "\n".join(l[10:] for l in m.group(1).splitlines()).strip()


def _group_expr(path=CI):
    m = re.search(r"^concurrency:\n(?:\s+.*\n)*?\s+group:\s*(.+)$",
                  path.read_text(), re.M)
    assert m, f"no concurrency.group in {path.name}"
    return m.group(1).strip()


def _path_list(path, key):
    """The entries under `key:` in a workflow's `on.push` block."""
    m = re.search(rf"^\s+{key}:\n((?:\s+- .*\n)+)", path.read_text(), re.M)
    assert m, f"no {key} in {path.name}"
    # Quotes optional: an entry regex that only matched quoted globs made two
    # unquoted lists compare as [] == [], so the coverage assertion below passed
    # vacuously on lists that differed.
    entries = sorted(re.findall(r"-\s*'?([^'\s]+)'?", m.group(1)))
    assert entries, f"{key} in {path.name} parsed as empty"
    return entries


# The `cihost` runner is macOS, and its `bash` is /bin/bash 3.2: no Homebrew
# bash is installed, and hosttools's host scripts are written to the same floor
# (its README and tests say so). A bash-4-only builtin in a `shell: bash` step
# fails there at run time, as `mapfile` did in the first build of plan 0148 (#986).
_BASH4_ONLY = re.compile(
    r"\b(mapfile|readarray)\b"          # bash 4.0 builtins
    r"|\bdeclare\s+-[a-zA-Z]*A"           # associative arrays (4.0)
    r"|\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^|,|\^)\}"  # case modification (4.0)
    r"|\bcoproc\b|\|&|&>>"                # 4.0 syntax
)


def bash4_only_lines(text):
    """(line number, line) for each line using a bash-4-only feature, ignoring
    comments, so a comment that names one does not trip the check."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if _BASH4_ONLY.search(code):
            hits.append((n, line.strip()))
    return hits


def test_no_workflow_uses_a_bash_4_only_feature():
    found = {wf.name: bash4_only_lines(wf.read_text()) for wf in WORKFLOWS}
    found = {k: v for k, v in found.items() if v}
    assert not found, (
        "cihost's bash is 3.2, so these fail there at run time: "
        + "; ".join(f"{k}:{n}: {l}" for k, v in found.items() for n, l in v))


def test_the_bash_4_guard_catches_mapfile_and_spares_comments():
    assert bash4_only_lines("          mapfile -t fast < <(echo a)\n")
    assert bash4_only_lines("declare -A m\n")
    assert bash4_only_lines('echo "${name,,}"\n')
    assert not bash4_only_lines("          # bash 3.2 has no mapfile\n")
    assert not bash4_only_lines('while IFS= read -r t; do fast+=("$t"); done <<< "$list"\n')


def test_only_one_trigger_fires_per_commit():
    """Forgejo publishes no merge ref, so a pull_request run built the same tree
    the push run already built — two statuses, one head_sha (755a5b9). Proving
    the MERGE is fleet-candidate's job, not a second identical run's."""
    body = CI.read_text()
    on_block = re.search(r"^on:\n((?:[ \t]+.*\n|\n)*)", body, re.M)
    assert on_block, "no `on:` block"
    triggers = re.findall(r"^\s{2}(\w+):", on_block.group(1), re.M)
    assert triggers == ["push"], f"expected push only, got {triggers}"


def test_the_group_serialises_rather_than_cancels():
    """A cancelled run reports as `failure` on the commit status — measured on
    0a46e6f — and the review dispatcher refuses a red build. So a canceller
    turns a scheduling artifact into a blocked review, which is the failure this
    whole block exists to stop. Never cancel a run whose status something else
    consumes as a verdict."""
    body = CI.read_text()
    m = re.search(r"^concurrency:\n(?:\s+.*\n)*?\s+cancel-in-progress:\s*(\S+)", body, re.M)
    assert m, "no cancel-in-progress setting"
    assert m.group(1) == "false", "cancelling makes a red status out of a duplicate"


def test_every_workflow_uses_the_same_per_ref_group():
    """Plan 0113 D4: narrowed from one literal lane to one per `github.ref`,
    once 3 isolated and 3 control rounds (18 overlapping runs, 108 jobs, all
    green) measured it rather than assumed it safe — see
    docs/fleet-history.md, "What two eunomia runs share (plan 0113)".

    Globbed WORKFLOWS, not a hand-listed pair — review 2147's finding still
    applies: a fourth workflow with its own group is a second parallel lane
    into the same per-ref workspace, invisible to a test that only reads two
    files."""
    groups = {wf.name: _group_expr(wf) for wf in WORKFLOWS}
    assert set(groups.values()) == {"eunomia-${{ github.ref }}"}, (
        f"workflows do not share the per-ref lane: {groups}")


def test_the_group_is_not_narrowed_past_ref():
    """`github.ref` is the narrow step plan 0113 D3 measured: different
    BRANCHES overlapping, and nothing wider. A sha, `head_ref` or `run_id`
    would let two pushes to ONE branch overlap too, which D3 did not measure.
    `github.workflow` would split CI and Plans into two lanes on one ref —
    the unmerged 2026-09-07 branch this plan's Boundaries refuse to build on,
    and run 21's hazard by a new route."""
    for wf in WORKFLOWS:
        expr = _group_expr(wf)
        for narrowing in ("github.sha", "github.head_ref", "github.run_id",
                          "github.workflow", "matrix."):
            assert narrowing not in expr, (
                f"{wf.name}'s group is narrowed by {narrowing}, past what plan "
                f"0113 D3 measured: {expr}")


def test_cancel_in_progress_is_false_on_every_workflow():
    """A cancelled run reports as `failure` on the commit status (measured on
    0a46e6f), and the review dispatcher refuses a red build — a canceller
    turns a scheduling artifact into a blocked review on ANY of these
    workflows, not only the one `test_the_group_serialises_rather_than_cancels`
    reads."""
    for wf in WORKFLOWS:
        body = wf.read_text()
        m = re.search(r"^concurrency:\n(?:\s+.*\n)*?\s+cancel-in-progress:\s*(\S+)",
                      body, re.M)
        assert m, f"no cancel-in-progress setting in {wf.name}"
        assert m.group(1) == "false", (
            f"{wf.name} cancels in progress; a cancelled run reads as a red "
            "status and a scheduling artifact blocks a review")


def test_no_path_falls_between_the_two_workflows():
    """ci.yml skips the suite for paths plans.yml covers. A path in ci.yml's
    ignore list but NOT in plans.yml's trigger gets no gate at all — it would
    merge with nothing having run, silently, which is worse than the four
    minutes the ignore list exists to save."""
    ignored = _path_list(CI, "paths-ignore")
    covered = _path_list(PLANS, "paths")
    assert ignored == covered, (
        f"ci.yml ignores {ignored} but plans.yml covers {covered}; the "
        f"difference {sorted(set(ignored) ^ set(covered))} has no gate")


def test_branches_ignore_excludes_the_test_lane_namespace_on_both_workflows():
    """plan 0070: a test lane's result lands as a draft PR on
    `agent/tests/<plan>/<runner>`, containing vendor-authored, unreviewed
    files under tests/. ci.yml's `tests` job runs pytest on every push it
    does not ignore, and plans.yml's `lint` job would otherwise trigger too —
    so both workflows must ignore the whole namespace, identically, or one of
    them runs untested vendor code on the self-hosted runner the moment the
    wrapper pushes it.

    Asserted via `_path_list`, the same helper
    `test_no_path_falls_between_the_two_workflows` uses for `paths`/
    `paths-ignore` — it is generic on the block key, so `branches-ignore`
    reads the same way."""
    for wf in (CI, PLANS):
        assert _path_list(wf, "branches-ignore") == ["agent/tests/**"], (
            f"{wf.name} does not ignore agent/tests/** under on.push")


def test_a_lane_branch_push_triggers_neither_workflow():
    """The parsed trigger, not just the presence of the ignore list — the same
    shape `test_only_one_trigger_fires_per_commit` reads. `branches-ignore` is
    a glob match, so this is a literal reading of the pattern rather than a
    simulated Forgejo evaluation, but it pins the one thing that matters: the
    pattern is anchored at `agent/tests/`, not a bare `agent/`, so an ordinary
    `agent/…` work branch (if one ever exists) is not silently excluded too."""
    import fnmatch
    for wf in (CI, PLANS):
        patterns = _path_list(wf, "branches-ignore")
        assert fnmatch.fnmatch("agent/tests/0070-x/sub-antigravity", patterns[0])
        assert not fnmatch.fnmatch("agent/other-thing", patterns[0])
        assert not fnmatch.fnmatch("feat/0070-a-test-lane", patterns[0])


# Anything ci.yml declines to test must be checked somewhere, or it has no gate.
# Each ignored PREFIX maps to the exact suite tests the fast lane (plans.yml) runs
# for it: a path leaves the suite only with every suite check that reads it.
# Entries are a file ("tests/x.py", every test in it) or a node id
# ("tests/x.py::test_y"). plans.yml runs this list, read from here by
# `python3 tests/test_ci_workflow.py --fast-lane-tests`, so lane and guard cannot
# drift. Prefixes, not roots: `docs/adr/` must never cover `docs/launch-preamble.md`.
COVERED_BY_THE_FAST_LANE = {
    "plans/": [
        "tests/test_fleet_plan.py::test_repo_plans_are_valid",
        "tests/test_plan_dependencies.py::test_the_repos_own_depends_on_edges_are_six_warnings_zero_errors",
        "tests/test_plan_graph.py::test_graph_on_main_emits_a_node_per_plan_every_edge_tagged",
        "tests/test_plan_ledger.py::test_the_live_ledger_scopes_to_this_repo_and_carries_only_what_it_can_prove",
    ],
    "docs/adr/": [
        "tests/test_adr_status.py",
        "tests/test_orchestrator_session.py::test_the_zone_rule_matches_the_documented_rule",
    ],
}

_REPO_ANCHORS = {"ROOT", "REPO_ROOT", "__file__"}
_TMP_NAMES = {"tmp_path", "tmp_path_factory", "tmpdir", "mkdtemp", "TemporaryDirectory"}


def _names(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
    return out


def _div_chain(node):
    """Flatten `a / "b" / "c"` into [a, "b", "c"] (strings kept, others None)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_chain(node.left) + _div_chain(node.right)
    return [node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None]


def _mentions(node, prefix):
    """Does this expression node spell the prefix: a string containing it, an
    f-string part containing it, or a `"docs" / "adr"` path join?"""
    segs = prefix.strip("/").split("/")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return re.search(rf"(?<![\w.-]){re.escape(prefix)}", node.value) is not None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        chain = _div_chain(node)
        return any(chain[i:i + len(segs)] == segs for i in range(len(chain)))
    return False


def real_readers(source, prefix):
    """(line, scope) of every place `source` reads a REAL file under `prefix`.

    How a real read is told from a fixture: the reference must sit in a scope
    (the enclosing function, or the module for a top-level statement) that is
    anchored to the repository — it names ROOT/REPO_ROOT/`__file__` — and does not
    build its tree under tmp_path. A test that builds `src / "plans"` in tmp_path,
    or passes the string "plans/0001-x.md" as an argument, is not a read of this
    repo's files. `scope` is the test function's name, or None for module level
    and non-test helpers, which are attributed to every test in the file."""
    tree = ast.parse(source)
    hits = []
    # A docstring talks ABOUT a tree; it reads nothing.
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)}

    def visit(node, func, stmt):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node
        elif func is None and isinstance(node, ast.stmt) and stmt is None:
            stmt = node
        if id(node) not in docstrings and _mentions(node, prefix):
            scope = func if func is not None else stmt
            names = _names(scope)
            if names & _REPO_ANCHORS and not names & _TMP_NAMES:
                is_test = func is not None and func.name.startswith("test_")
                hits.append((node.lineno, func.name if is_test else None))
        for child in ast.iter_child_nodes(node):
            visit(child, func, stmt)

    visit(tree, None, None)
    return sorted(set(hits), key=lambda h: (h[0], h[1] or ""))


def unguarded_readers(tests_dir, ignored_prefixes, covered):
    """Every `file:line` that reads a real file under an ignored prefix and is not
    named in that prefix's fast-lane list."""
    out = []
    for src in sorted(tests_dir.glob("*.py")):
        rel = f"tests/{src.name}"
        for prefix in ignored_prefixes:
            allowed = covered.get(prefix, [])
            for line, scope in real_readers(src.read_text(), prefix):
                ok = rel in allowed or (scope is not None and f"{rel}::{scope}" in allowed)
                if not ok:
                    out.append(f"{rel}:{line} reads {prefix}")
    return out


def _ignored_prefixes():
    """Each ignored glob's FULL prefix (`plans/`, `docs/adr/`), not its root."""
    return [g.removesuffix("**") for g in _path_list(CI, "paths-ignore")]


def test_nothing_the_suite_reads_is_ignored_by_ci():
    """An ignored tree that the SUITE reads is a hole with a green over it.

    Measured: `docs/**` was in the ignore list while tests/test_fleet_cr.py
    EXECUTES every fenced block of docs/launch-preamble.md and
    tests/test_fleet_heartbeat.py executes docs/takeover.md. A docs-only push
    carrying a broken command would have skipped the suite, taken a green from
    plans.yml, merged, skipped the suite again on main — that push being
    docs-only too — and surfaced as a red main under the next unrelated author.

    Compares each ignored glob's full PREFIX, so `docs/adr/**` does not cover
    `docs/launch-preamble.md`. Reads test sources with `ast`, so it sees string
    literals, f-strings and `"docs" / "adr"` joins; see `real_readers` for how a
    read of this repo is told from a tmp_path fixture. Limit: it inspects only
    `tests/*.py`, so a test that runs a `bin/` tool which itself reads an ignored
    tree is not seen."""
    prefixes = _ignored_prefixes()
    here = Path(__file__).resolve().parent
    bad = unguarded_readers(here, prefixes, COVERED_BY_THE_FAST_LANE)
    assert not bad, (
        "ci.yml ignores trees the suite reads, so a push touching only them gets "
        f"no gate: {bad}. Either drop the prefix from paths-ignore, or add the "
        "reader to COVERED_BY_THE_FAST_LANE (plans.yml then runs it).")
    assert set(prefixes) == set(COVERED_BY_THE_FAST_LANE), (
        "paths-ignore and COVERED_BY_THE_FAST_LANE name different prefixes: "
        f"{sorted(set(prefixes) ^ set(COVERED_BY_THE_FAST_LANE))}")


def test_the_guard_catches_a_new_reader(tmp_path):
    """The guard's own test: a new test reading a real ADR is reported by file and
    line, and the lookalikes the guard must not flag are not."""
    (tmp_path / "test_new.py").write_text(
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parent.parent\n"
        "def test_reads_an_adr():\n"
        "    return (ROOT / 'docs' / 'adr' / '0001-x.md').read_text()\n"
        "def test_reads_via_string():\n"
        "    return open(f'{ROOT}/docs/adr/0001-x.md').read()\n"
        "def test_fixture_only(tmp_path):\n"
        "    (tmp_path / 'docs' / 'adr').mkdir(parents=True)\n"
        "def test_launch_preamble():\n"
        "    return (ROOT / 'docs' / 'launch-preamble.md').read_text()\n")
    covered = {"docs/adr/": ["tests/test_adr_status.py"]}
    bad = unguarded_readers(tmp_path, ["docs/adr/"], covered)
    assert bad == ["tests/test_new.py:4 reads docs/adr/",
                   "tests/test_new.py:6 reads docs/adr/"], bad
    covered["docs/adr/"].append("tests/test_new.py::test_reads_an_adr")
    assert len(unguarded_readers(tmp_path, ["docs/adr/"], covered)) == 1


def test_the_fast_lane_never_covers_the_executed_docs():
    """`docs/adr/` must not be widened to `docs/`: the suite executes
    docs/launch-preamble.md and docs/takeover.md."""
    for prefix in _ignored_prefixes():
        assert prefix != "docs/" and not "docs/launch-preamble.md".startswith(prefix)
        assert not "docs/takeover.md".startswith(prefix)


def test_plans_yml_runs_the_list_from_this_file():
    body = PLANS.read_text()
    assert "tests/test_ci_workflow.py --fast-lane-tests" in body
    for entries in COVERED_BY_THE_FAST_LANE.values():
        for e in entries:
            assert (Path(__file__).resolve().parent.parent / e.split("::")[0]).is_file(), e


def fast_lane_tests():
    return [e for entries in COVERED_BY_THE_FAST_LANE.values() for e in entries]


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--fast-lane-tests"]:
        print("\n".join(fast_lane_tests()))
    else:
        sys.exit("usage: test_ci_workflow.py --fast-lane-tests")


def test_the_mitigation_suite_matches_ci():
    """plans.yml carries a verbatim copy of ci.yml's suite step, for the pushes
    where the forge's own file list sends a code change down this lane.

    A copy that drifts is worse than no copy: it keeps reporting green while
    testing something other than what CI tests, and the review dispatcher — the
    automated consumer of that green — cannot tell. So the two are pinned equal
    here rather than trusted to be maintained together."""
    assert _step_run(CI, "tests") == _step_run(
        PLANS, "full suite (when the change leaves the fast lane)"), (
        "plans.yml's mitigation suite has drifted from ci.yml's `tests` step; "
        "a mitigation that runs a different suite is not a mitigation")


def test_the_mitigation_is_gated_on_the_change_not_the_push_shape():
    """The gate must ask what `merge-base..HEAD` contains.

    It used to ask `github.event.before == <zero sha>` — "is this a first
    push?" — which is a PROXY for "is the forge's file list untrustworthy?".
    The proxy was wrong for a force push: `before` is a real SHA, so the gate
    said no, while `GetFilesChangedBetween` returned the rebase delta rather
    than the change. Measured on #211, where a one-file test change carried
    nothing but a `Plans / lint` green.

    Pinning the range rather than the wording, because the range is the claim:
    anything computed from `merge-base` is unaffected by how the branch was
    pushed, which is the whole property (a), (b) and (c) break."""
    body = PLANS.read_text()
    scope = _step_run(PLANS, "scope — does this change leave the fast lane?")
    assert scope, "the scope step is gone; the mitigation is now ungated"
    assert "merge-base origin/main HEAD" in scope, (
        "the gate must be computed from the merge base — an event-payload "
        "proxy is what #211 defeated")
    assert "full_suite=true" in scope and "full_suite=false" in scope, (
        "the scope step must decide both ways")
    assert re.search(r"^\s+if: \$\{\{ steps\.scope\.outputs\.full_suite == 'true' \}\}",
                     body, re.M), "the suite is not gated on the scope output"
    assert "github.event.before" not in body, (
        "the push-shape proxy is back in plans.yml; #211 is the reason it "
        "cannot be trusted")


def test_the_mitigation_stays_narrow():
    """A plans-only change must still skip the suite.

    This is the other half of the gate's value and the reason the old test
    warned against widening: if the suite ran on every plans push, the fast
    lane stops being fast, the ignore list stops paying for itself, and — worse
    — the dispatcher's new missing-context warning would fire on every plan PR
    the fleet opens and be trained away. The filter is `grep -v '^plans/'`, so
    it is anchored: a sibling like `plansible/` is NOT excused."""
    scope = _step_run(PLANS, "scope — does this change leave the fast lane?")
    assert "grep -v -e '^plans/' -e '^docs/adr/'" in scope, (
        "the fast-lane exclusions must be anchored at the start of the path, and "
        "must be exactly ci.yml's paths-ignore globs")


def test_the_scope_step_can_see_the_merge_base():
    r"""`merge-base` against a shallow clone returns nothing, and the step would
    then take its `on main (or an ancestor)` branch and skip the suite — a
    silent failure in the unsafe direction. The checkout must be unshallow.

    ANCHORED, and that is the whole finding. The first version of this asserted
    `re.search(r"fetch-depth:\s*0", body)`, which matched the COMMENT above the
    key ("# fetch-depth: 0 so `scope` below can diff..."). Deleting the real
    `with:` block left the test green — it was counted as coverage for the one
    key whose loss makes this gate fail open silently. A test that passes on
    the broken tree is worse than no test."""
    body = PLANS.read_text()
    assert re.search(r"^\s+fetch-depth:\s*0\s*$", body, re.M), (
        "plans.yml checks out shallow; merge-base cannot resolve and the gate "
        "fails open")


def test_the_scope_step_ignores_renames():
    """`git diff` defaults to rename detection and prints only the destination,
    so moving a file into plans/ reads as a plans-only change and skips the
    suite over a deletion. Reproduced on git 2.50.1 with
    docs/launch-preamble.md, a file the suite executes.

    SCOPED TO THE COMMAND LINE, then asserted on the line rather than on the
    whole body. Both halves are findings paid for once each: `"--no-renames" in
    scope` passed with the flag deleted, because the shell comment explaining
    the flag lives in the same `run:` body; the correction to that then pinned
    the literal `git diff --no-renames --name-only`, so swapping two flags
    failed an identical command (2981 LOW 2). The property is "rename detection
    is off on the line that computes `outside`" — find that line, then ask only
    that, which is what the module header means by asserting the property
    rather than the spelling.
    """
    scope = _step_run(PLANS, "scope — does this change leave the fast lane?")
    line = next((l for l in scope.splitlines()
                 if re.match(r"\s*outside=\$\(git diff\b", l)), None)
    assert line, "the scope step no longer computes `outside` from a git diff"
    assert re.search(r"\B--no-renames\b", line), (
        "the scope diff must disable rename detection, or a `git mv` out of a "
        "tracked directory into plans/ takes the fast lane over a deleted file")


def _run_scope(tmp_path, status, runs, token="t", keep_stderr=False):
    """Run the real scope step on a branch that changes code, against a fake
    forge answering `status` with `runs`. Returns (stdout, the step's output)."""
    import json
    import os
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    def git(*a, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                       cwd=cwd, check=True, capture_output=True)

    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    (work / "plans").mkdir()
    (work / "plans" / "p.md").write_text("plan\n")
    git("add", ".", cwd=work)
    git("commit", "-qm", "base", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    (work / "bin.py").write_text("code\n")
    git("add", ".", cwd=work)
    git("commit", "-qm", "code", cwd=work)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                          capture_output=True, text=True).stdout.strip()

    seen = []

    class Forge(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization", "").strip() in ("", "token"):
                # A private repo answers an empty token as anonymous.
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"workflow_runs": [
                {"id": i, "workflow_id": wf, "commit_sha": head if sha is None else sha}
                for i, (wf, sha) in enumerate(runs, 1)]}).encode()
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Forge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = tmp_path / "out"
    out.write_text("")
    env = {**os.environ, "GITHUB_OUTPUT": str(out), "GITHUB_REPOSITORY": "o/r",
           "GITHUB_SERVER_URL": f"http://127.0.0.1:{srv.server_port}"}
    env.pop("FORGE_TOKEN", None)
    if token is not None:
        env["FORGE_TOKEN"] = token
    try:
        p = subprocess.run(["bash", "-eo", "pipefail", "-c",
                            _step_run(PLANS, "scope — does this change leave the fast lane?")],
                           cwd=work, env=env, capture_output=True, text=True)
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stderr
    if keep_stderr:
        return p.stdout + p.stderr, out.read_text()
    if status == 200 and token:
        assert seen and f"head_sha={head}" in seen[0][0] and seen[0][1] == f"token {token}", seen
    return p.stdout, out.read_text()


def test_a_commit_ci_yml_already_tests_does_not_run_the_suite_twice(tmp_path):
    """The duplicate suite was the ~15-minute `Plans / lint` on every PR commit
    that touched plans/ AND code: ci.yml ran the suite, then this lane ran it
    again behind it on the one cihost runner, and the commit's CI outlived the
    review dispatcher's 1800 s wait (eunomia #1062, #1064)."""
    stdout, out = _run_scope(tmp_path, 200, [("plans.yml", None), ("ci.yml", None)])
    assert out == "full_suite=false\n", stdout


def test_anything_short_of_a_ci_yml_run_on_this_commit_runs_the_suite(tmp_path):
    """Fail closed. Each of these is a commit the forge may have sent down this
    lane alone, and skipping the suite on it is a green over untested code —
    the #211 failure this lane exists to prevent."""
    cases = {
        "no ci.yml run": (200, [("plans.yml", None)], "t"),
        "a ci.yml run on another commit": (200, [("ci.yml", "0" * 40)], "t"),
        "the forge errors": (500, [("ci.yml", None)], "t"),
        # `FORGE_TOKEN: ${{ secrets.GITHUB_TOKEN }}` always sets the
        # variable; an unprovisioned secret makes it empty, so the reachable
        # failure is a 401. The absent case stays as the belt.
        "an empty token": (200, [("ci.yml", None)], ""),
        "no token variable": (200, [("ci.yml", None)], None),
    }
    for name, (status, runs, token) in cases.items():
        d = tmp_path / name.replace(" ", "-")
        d.mkdir()
        stdout, out = _run_scope(d, status, runs, token)
        assert out == "full_suite=true\n", f"{name}: {stdout}"


def test_a_rejected_token_never_reaches_the_job_log(tmp_path):
    """http.client refuses a header value with a control character and quotes
    the whole value, token included, in the ValueError. Uncaught, the traceback
    goes to stderr, which is the job log (review 5742). The step must print the
    failure without the token, and still fail closed."""
    secret = "s3cr3t-value\nX-Injected: 1"
    stdout, out = _run_scope(tmp_path, 200, [("ci.yml", None)], secret, keep_stderr=True)
    assert out == "full_suite=true\n", stdout
    assert "s3cr3t-value" not in stdout, stdout
    assert "forge query failed: ValueError" in stdout, stdout


def _extract_job_block(job_name):
    """Extract a job's YAML block from ci.yml by indentation.

    Returns the text of the job block starting from the job name at 2-space
    indentation through all its indented content (4+ spaces), or None if the
    job is not found. This allows tests to scope assertions to a specific job."""
    body = CI.read_text()
    # Find the job name at 2-space indentation (jobs are at level 2 in 'jobs:')
    pattern = rf"^  {re.escape(job_name)}:\n((?:    .*\n|\n)*)"
    m = re.search(pattern, body, re.M)
    if not m:
        return None
    # Return the job block content (not including the job name line itself,
    # but including everything indented under it)
    return m.group(1)


def test_tests_job_runs_on_cihost_only():
    """The tests job runs on the macOS host label only.

    ADR-0007, amended 2026-11-08: the full suite no longer runs a second time
    on `cihost-linux`. The matrix keeps one label so the status name
    `CI / tests (cihost)` and the CI_LABEL variable are unchanged."""
    job_block = _extract_job_block("tests")
    assert job_block, "tests job not found"
    m = re.search(r"^\s*label:\s*\[([^\]]*)\]", job_block, re.M)
    assert m, "tests job has no flow-style matrix label list"
    labels = [l.strip() for l in m.group(1).split(",") if l.strip()]
    assert labels == ["cihost"], f"tests matrix should be [cihost], got {labels}"
    assert re.search(r"runs-on:\s*\$\{\{\s*matrix\.", job_block), \
        "runs-on does not reference matrix variable"


def test_probe_reach_job_exists_and_covers_both_labels():
    """A job named probe-reach in the same workflow tests reachability of host
    paths and runner configuration, on both labels.

    Plan §2: 'A reach probe job, probe-reach, in the same workflow, on both
    labels, that tests absolute host paths...'"""
    body = CI.read_text()
    assert re.search(r"^  probe-reach:", body, re.M), "probe-reach job not found"
    job_block = _extract_job_block("probe-reach")
    assert job_block, "probe-reach job not found or empty"
    # Should have a matrix with both labels
    assert re.search(r"matrix:", job_block), "probe-reach job has no matrix"
    assert re.search(r"cihost", job_block), "probe-reach matrix missing 'cihost' label"
    assert re.search(r"cihost-linux", job_block), "probe-reach matrix missing 'cihost-linux' label"


def test_probe_reach_uses_absolute_paths_not_tilde():
    """The probe checks absolute host paths, never tilde paths, because inside
    the job container $HOME=/root, not /Users/operator.

    Plan §2: 'Never ~: inside the image $HOME is /root, so a tilde probe
    reports not reachable because /root/agent does not exist, not because the
    host is out of reach.' The three paths checked are: /Users/operator/agent,
    /Users/operator/.claude, /opt/homebrew/var/forgejo/data."""
    job_block = _extract_job_block("probe-reach")
    assert job_block, "probe-reach job not found"
    # Should have all three absolute paths
    assert re.search(r"/Users/operator/agent", job_block), \
        "probe does not check /Users/operator/agent"
    assert re.search(r"/Users/operator/\.claude", job_block), \
        "probe does not check /Users/operator/.claude"
    assert re.search(r"/opt/homebrew/var/forgejo/data", job_block), \
        "probe does not check /opt/homebrew/var/forgejo/data"
    # Should NOT have tilde paths or $HOME/ references in the probe job
    assert not re.search(r"~/", job_block), "probe uses tilde path"
    assert not re.search(r"\$HOME/", job_block), "probe uses $HOME/ path"


def test_probe_reach_checks_docker_socket_and_proc_mounts():
    """The probe asserts two routes back to the host are closed: no Docker
    socket and no bind mount from the host.

    Plan §2: 'The probe also asserts the two routes back to the host are
    closed: no Docker socket (/var/run/docker.sock absent and DOCKER_HOST
    unset) and no bind mount from the host at all...'"""
    job_block = _extract_job_block("probe-reach")
    assert job_block, "probe-reach job not found"
    # Should check Docker socket availability
    assert re.search(r"/var/run/docker\.sock", job_block), \
        "probe does not check /var/run/docker.sock"
    # Should check DOCKER_HOST environment variable
    assert re.search(r"DOCKER_HOST", job_block), \
        "probe does not check DOCKER_HOST environment"
    # Should check /proc/mounts for bind mounts
    assert re.search(r"/proc/mounts", job_block), \
        "probe does not check /proc/mounts for host mounts"


def test_probe_reach_never_prints_file_content():
    """The probe prints reachability only, never file content.

    Plan §2: 'The probe prints reachability only, never content, and never a
    path outside the ones it names.' This means no cat, head, sed -n, tail,
    less, or more applied to the three probed paths. A 'test -e' or 'test -r'
    style check is what is expected: presence/readability, not content
    extraction. Check line-by-line: no line combines a content-reading command
    with any probed path, and variables set from those paths only appear with
    test/bracket checks."""
    job_block = _extract_job_block("probe-reach")
    assert job_block, "probe-reach job not found"

    probed_paths = [
        "/Users/operator/agent",
        "/Users/operator/.claude",
        "/opt/homebrew/var/forgejo/data"
    ]
    forbidden_cmds = ["cat ", "head ", "tail ", "sed -n", "less ", "more "]
    forbidden_bare = ["cat", "head", "tail"]  # bare invocations

    # L4b: Check line by line. No line should combine a content-reading command
    # with any of the probed paths.
    for line in job_block.split('\n'):
        for cmd in forbidden_cmds:
            if cmd in line:
                for path in probed_paths:
                    assert path not in line, \
                        f"line contains '{cmd}' applied to {path}: {line}"

        # Also check for bare cat/head/tail of a variable set from probed paths.
        # A variable like $p should only appear with test -e, test -r, [ -e, [ -r,
        # or echo "REACH in a safe context.
        if any(f"${{{var}}}" in line or f"${var}" in line
               for var in ["p"]):  # variables that might be set from paths
            # These are safe contexts
            if any(safe in line for safe in ["test -e", "test -r", "[ -e", "[ -r",
                                               "echo", "REACH", "TIMING", "ROUTE"]):
                continue
            # If we're here and have a path variable, check it's not a bare cat/head/tail
            for bare_cmd in forbidden_bare:
                # Looking for patterns like: "cat $p" or "head $p" etc.
                if re.search(rf"\b{bare_cmd}\s+\$", line):
                    # But only fail if the variable might be set from a probed path
                    assert False, \
                        f"line uses bare '{bare_cmd}' of a variable that may be set from probed paths: {line}"


def test_workflow_does_not_grant_container_extra_reach():
    """The workflow must not declare its own mounts or grant privileges to
    bypass the runner config.

    Plan §2: 'container.privileged: false; container.valid_volumes: [] so a
    workflow cannot declare its own mounts'. The workflow file itself should
    have no 'privileged' key, no 'volumes' key, and no 'container' block naming
    'python:*-slim' (the plan explains why: git and node are already in the
    act-convention image)."""
    body = CI.read_text()
    # No privileged key in the workflow
    assert not re.search(r"^\s+privileged:", body, re.M), \
        "workflow has 'privileged' key (should be in runner config only)"
    # No volumes key in the workflow
    assert not re.search(r"^\s+volumes:", body, re.M), \
        "workflow has 'volumes' key (should be in runner config only)"
    # No container block naming python:*-slim
    assert not re.search(r"container:\s*python:\d+-slim", body, re.M), \
        "workflow declares python:*-slim container (plan specifies act-convention image)"


def test_tests_job_emits_timing_line():
    """The tests job emits machine-readable timing for checkout, dependency
    install, tests, and total.

    Plan §2: 'Timing, recorded per run in the workflow summary: checkout,
    dependency install, tests, total. Both numbers go into docs/fleet-history.md
    so the cost of isolation is a number.' The workflow must emit a line
    containing the literal token 'TIMING' with 'seconds=' nearby."""
    job_block = _extract_job_block("tests")
    assert job_block, "tests job not found"
    assert re.search(r"TIMING", job_block), \
        "tests job does not emit TIMING line"
    assert re.search(r"seconds=", job_block), \
        "tests job timing line missing 'seconds=' field"


def test_probe_reach_host_mount_gate_is_default_deny():
    """T1: Host-mount gate is default-deny with an allowlist.

    Plan §2: 'read as: every `/proc/mounts` entry whose source device is a
    virtiofs or 9p share, or whose mount point is anything other than the
    image's own layers, `/proc`, `/sys`, `/dev` and the workspace the runner
    creates.' The probe-reach job must contain `/proc/mounts` AND an allowlist
    of benign mount points. Assert the script contains these strings: `/proc`,
    `/sys`, `/dev`, `/etc/resolv.conf`, `/etc/hosts`, `/var/run/act` (or
    `/run/act`), and `GITHUB_WORKSPACE`. Assert the script emits a line
    beginning `HOSTMOUNT point=` for an unlisted mount. The old denylist-only
    shape (relying solely on virtiofs/9p) must be gone."""
    job_block = _extract_job_block("probe-reach")
    assert job_block, "probe-reach job not found"

    # Must check /proc/mounts
    assert re.search(r"/proc/mounts", job_block), \
        "probe does not read /proc/mounts for mount checks"

    # Must have allowlist entries for benign mounts
    allowlist_items = [
        r"/proc",
        r"/sys",
        r"/dev",
        r"/etc/resolv\.conf",
        r"/etc/hosts",
        r"(/var/run/act|/run/act)",  # Either one is acceptable
        r"GITHUB_WORKSPACE"
    ]
    for item in allowlist_items:
        assert re.search(item, job_block), \
            f"probe missing allowlist entry for {item}"

    # Must emit HOSTMOUNT point= token for unlisted mounts (the default-deny gate)
    assert re.search(r"HOSTMOUNT point=", job_block), \
        "probe does not emit HOSTMOUNT point= token for unlisted mounts"


def test_tests_job_emits_timing_for_all_steps():
    """T2: Timing columns must be emitted for each step.

    Plan §2 and §4: 'Timing, recorded per run in the workflow summary: checkout,
    dependency install, tests, total.' The tests job block must emit TIMING
    lines with literal step= tokens for checkout, install, tests, and total."""
    job_block = _extract_job_block("tests")
    assert job_block, "tests job not found"

    # Each step must emit a TIMING line with step=<name> and seconds=
    steps = ["checkout", "install", "tests", "total"]
    for step in steps:
        pattern = rf"TIMING.*step={step}.*seconds="
        assert re.search(pattern, job_block), \
            f"tests job does not emit TIMING line for step={step}"


def test_tests_job_timing_start_uses_runner_temp_or_run_id():
    """T3: Start timestamp file must reference RUNNER_TEMP or GITHUB_RUN_ID.

    Plan §2 Review comment L3: if the job writes a start timestamp to a file,
    the path must reference RUNNER_TEMP or GITHUB_RUN_ID, not be a bare
    workspace file like `.ci-timing-start` with no directory."""
    job_block = _extract_job_block("tests")
    assert job_block, "tests job not found"

    # If a file is written to store timing, it must reference RUNNER_TEMP or GITHUB_RUN_ID
    # Check for bare paths like ".ci-timing-start" without a directory
    for line in job_block.split('\n'):
        # Look for file writes (> operator)
        if ' > ' in line or ' >> ' in line:
            # Extract the file path after > or >>
            m = re.search(r'>\s*([^\s]+)', line)
            if m:
                filepath = m.group(1)
                # Bare workspace files should not be used; must reference env vars
                if filepath.startswith('.') and '/' not in filepath and \
                   '$' not in filepath:
                    assert False, \
                        f"timing file uses bare workspace path '{filepath}'; " \
                        f"must reference $RUNNER_TEMP or $GITHUB_RUN_ID"


def test_tests_job_pytest_runs_with_show_summary():
    """T4: pytest must run with -rs to show skip reasons.

    Plan §2: 'a test that the image cannot satisfy is recorded in the handoff
    with its reason rather than skipped quietly.' The pytest invocation must
    include the `-rs` flag to show skip and other summary info."""
    job_block = _extract_job_block("tests")
    assert job_block, "tests job not found"

    # pytest invocation must include -rs
    assert re.search(r"pytest.*-rs", job_block) or \
           re.search(r"pytest\s+-[a-z]*rs", job_block), \
        "pytest invocation missing -rs flag (needed to show skip reasons)"


def test_talos_contract_job_pins_plan_0097():
    """Plan 0097: the job that runs talos's declared calls against this
    checkout's bin/. Each assertion is a way it could go green having checked
    nothing, or reach beyond what it may."""
    job = _extract_job_block("talos-contract")
    assert job, "talos-contract job not found"
    assert re.search(r"^    runs-on: cihost$", job, re.M), "talos-contract must run on cihost only"
    assert re.search(r"^    needs: tests$", job, re.M), \
        "needs: tests keeps it out of the tests job's shared workspace"
    assert "git clone" in job and "file://" in job and "--depth 1" in job
    assert "secrets." not in job, "the job reads talos credential-free"
    migrate = job.index("bin/fleet-db-migrate")
    seed_code = job.index("fleet-models set default code_reviewer")
    seed_plan = job.index("fleet-models set default plan_reviewer")
    test = job.index("tests/test_talos_contract_live.py")
    assert migrate < seed_code < test and migrate < seed_plan < test, \
        "order must be migrate, seed, then the test"
    assert re.search(r"TIMING.*step=total.*seconds=", job)
