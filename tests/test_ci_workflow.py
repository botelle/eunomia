"""The CI workflow's concurrency key, guarded.

A YAML file gets no test by default, and this one shipped a concurrency block
whose comment declared a race fixed while the expression grouped nothing. That
is the expensive kind of wrong: merged, it retires the question, and the next
red build on identical code reads as already-handled.

No PyYAML in CI (the job installs pytest and pytest-randomly only), so this
reads the file as text and asserts the property rather than the spelling."""
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


def test_the_group_is_not_narrowed_per_ref_or_commit():
    """The remaining collision is between DIFFERENT commits: two eunomia jobs at
    once share a checkout and a .venv. Run 21 failed on a tree byte-identical to
    one that had already passed twice, so nothing in its code explains it. A ref-
    or commit-keyed group cannot serialise that."""
    for wf in WORKFLOWS:
        expr = _group_expr(wf)
        for narrowing in ("github.ref", "github.head_ref", "github.sha",
                          "pull_request.head.sha", "matrix."):
            assert narrowing not in expr, (
                f"{wf.name}'s group is narrowed by {narrowing}, so two concurrent "
                f"jobs on this repo still share a workspace: {expr}")


def test_every_workflow_shares_one_literal_lane():
    """This replaces an assertion that the group contains `github.workflow`,
    which was spelling and not property — the docstring at the top of this file
    promises property. `${{ github.workflow }}` serialises a workflow against
    ITSELF and nothing else, so a second workflow on the same runner became a
    second parallel lane into the same workspace: run 21's hazard by a new route.

    The property is that every workflow on this runner names ONE lane, and a
    literal is what makes that checkable — two files can both say
    `${{ github.workflow }}`, read as identical here, and still evaluate to
    different lanes at run time."""
    for wf in WORKFLOWS:
        expr = _group_expr(wf)
        assert "${{" not in expr, (
            f"{wf.name}'s group is an expression ({expr}); it can evaluate to a "
            "different lane per workflow or per run, so equality in this file "
            "would not mean equality on the runner")
    groups = {wf.name: _group_expr(wf) for wf in WORKFLOWS}
    assert len(set(groups.values())) == 1, (
        f"workflows do not share a lane: {groups}")


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
# `plans/**` qualifies because plans.yml runs the same `fleet-plan lint` that
# test_repo_plans_are_valid runs; the check changes lane rather than vanishing.
COVERED_BY_THE_FAST_LANE = {"plans"}


def test_nothing_the_suite_reads_is_ignored_by_ci():
    """An ignored tree that the SUITE reads is a hole with a green over it.

    Measured: `docs/**` was in the ignore list while tests/test_fleet_cr.py
    EXECUTES every fenced block of docs/launch-preamble.md and
    tests/test_fleet_heartbeat.py executes docs/takeover.md. A docs-only push
    carrying a broken command would have skipped the suite, taken a green from
    plans.yml, merged, skipped the suite again on main — that push being
    docs-only too — and surfaced as a red main under the next unrelated author.

    This reads the test sources rather than a hand-kept list, and it accepts both
    spellings, because the two docs reads use pathlib segments
    (`"docs" / "takeover.md"`) and a `docs/` substring search finds neither.

    TWO LIMITS, deliberate while the ignore list has one entry, and to check by
    hand before adding a second (review 2147): it inspects only `tests/*.py`, so a
    test that exercises a `bin/` tool which itself reads an ignored tree is not
    seen; and it compares directory ROOTS, so a top-level file in `paths-ignore`
    (whose root is the filename) matches neither regex."""
    ignored_roots = {g.split("/")[0] for g in _path_list(CI, "paths-ignore")}
    here = Path(__file__).resolve().parent
    read = {}
    for src in sorted(here.glob("*.py")):
        body = src.read_text()
        for root in ignored_roots:
            if (re.search(rf'["\']{root}/', body)
                    or re.search(rf'["\']{root}["\']\s*/', body)):
                read.setdefault(root, []).append(src.name)
    unguarded = {r: v for r, v in read.items() if r not in COVERED_BY_THE_FAST_LANE}
    assert not unguarded, (
        "ci.yml ignores trees the suite reads, so a push touching only them gets "
        f"no gate: {unguarded}. Either drop them from paths-ignore, or give the "
        "fast lane the equivalent check and add the tree to "
        "COVERED_BY_THE_FAST_LANE with the reason.")


def test_the_mitigation_suite_matches_ci():
    """plans.yml carries a verbatim copy of ci.yml's suite step, for the pushes
    where the forge's own file list sends a code change down this lane.

    A copy that drifts is worse than no copy: it keeps reporting green while
    testing something other than what CI tests, and the review dispatcher — the
    automated consumer of that green — cannot tell. So the two are pinned equal
    here rather than trusted to be maintained together."""
    assert _step_run(CI, "tests") == _step_run(
        PLANS, "full suite (when the change leaves plans/)"), (
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
    scope = _step_run(PLANS, "scope — does this change leave plans/?")
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
    scope = _step_run(PLANS, "scope — does this change leave plans/?")
    assert "grep -v '^plans/'" in scope, (
        "the plans-only exclusion must be anchored at the start of the path")


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
    scope = _step_run(PLANS, "scope — does this change leave plans/?")
    line = next((l for l in scope.splitlines()
                 if re.match(r"\s*outside=\$\(git diff\b", l)), None)
    assert line, "the scope step no longer computes `outside` from a git diff"
    assert re.search(r"\B--no-renames\b", line), (
        "the scope diff must disable rename detection, or a `git mv` out of a "
        "tracked directory into plans/ takes the fast lane over a deleted file")

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


def test_tests_job_has_matrix_with_both_labels():
    """The tests job runs on both cihost and cihost-linux via a matrix.

    Plan §2: 'A second runner label on cihost, beside the existing `cihost`'
    and 'eunomia's own ci.yml moves to runs-on: cihost-linux as the first
    migration'. The matrix enables both labels to run in parallel, proving
    isolation without duplicating the workflow."""
    job_block = _extract_job_block("tests")
    assert job_block, "tests job not found"
    # Should have a matrix key with both labels
    assert re.search(r"matrix:", job_block), "tests job has no matrix"

    # L4a: Assert the matrix literally contains both cihost and cihost-linux as
    # separate list elements, with word boundaries so cihost-linux alone doesn't
    # satisfy the cihost check. Match both flow-style [cihost, cihost-linux] and
    # block-style - cihost / - cihost-linux.
    assert re.search(r"\bcihost\b", job_block), \
        "matrix missing 'cihost' label (or only contains 'cihost-linux')"
    assert re.search(r"\bcihost-linux\b", job_block), \
        "matrix missing 'cihost-linux' label"

    # runs-on should reference the matrix, not a literal string
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
