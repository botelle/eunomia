# ADR-0014 — A pull request changes one kind of thing, and each kind has its own reviewer

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0003](0003-adr-track.md) §2 (the ADR pull request no longer
  carries the flow-spec change; the ordering survives);
  [ADR-0009](0009-a-lane-may-have-a-supervisor.md) §1 (three agentic review
  positions); [ADR-0011](0011-a-runner-has-a-provenance-class.md) §2 (which
  classes may hold them).
- **Relates:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md)
  §3–§5 (reviewers are per-repo configuration; this adds three positions to
  that table), [ADR-0006](0006-blackbox-test-lanes.md) (the test lane),
  `bin/fleet-models`, `bin/fleet-lane`, `docs/feature-plans.md`,
  `docs/plan-dependencies.md`, `dispatch-review.py` in `operator/talos`.
- **Spec-impact:** `docs/plan-dispatch.md` Part 2b (the review loop routes by
  kind). It is updated by a following `docs` pull request, as §3 requires.
  `docs/per-repo-models.md` and `docs/test-lane.md` are operational docs, and
  they change with the `src` pull requests that change their subjects.

## Context

A pull request that changes an ADR, a flow spec, tests, code and how the thing
is deployed is reviewed by one reviewer against one rubric, and merged by one
click. The flow spec is what the test lane writes from (ADR-0003 §2, ADR-0006),
so a flow change that rides along with code reaches the tests unreviewed as a
flow change. A deployment change that rides along with code is reviewed by a
rubric written for code. Nothing today tells these apart.

## Decision

**1. A pull request changes exactly one kind.** The kinds are:

| kind | what it holds |
|---|---|
| `adr` | architecture decision records |
| `plan` | feature plans |
| `docs` | flow specs and design documents: what the system is meant to do |
| `tests` | the black-box tests the test lane writes, under `tests/lane/**` |
| `deploy` | how the thing runs: service units, start wrappers, deploy manifests, CI and release workflows, and their docs |
| `src` | everything else: code, its unit tests, and operational docs |

An implementation pull request that carries `Plan: <id>` may also change
`plans/<id>.md`, the plan it implements. The classifier drops that one path
before deciding the kind. That keys on the marker and the path, not on
reading the change.

**2. Kind is decided by path, never by reading content or by branch name.** A
fleet-wide default map assigns paths to kinds, and a repository may override
it in reviewed eunomia configuration. A map entry may name a single file,
because `docs/` directories mix flow specs with operational docs. A path the
map does not name is `src`, so an unmapped path gets the strictest automatic
review rather than none. The default map lives in eunomia configuration and
changes only by a reviewed pull request. The test lane writes only under
`tests/lane/`, so its output is separable from unit tests by path.

**3. A change that needs more than one kind is a chain, in this order:** `adr`,
if the change conflicts with an accepted ADR → `plan` → `docs`, if the change
alters a flow spec → `tests` and `src` in parallel → `deploy`, if how it runs
changes. Each link is its own plan or pull request, ordered by `depends_on`
edges where an edge can tell the difference. A plan whose `flow:` names a spec
depends on the plan that updates that spec, so its test lane runs against the
current spec. A `path:` edge cannot see an amendment to a file that already
exists. So a link that waits on an ADR **amendment** is held at the merge: its
pull request stays unmerged, with the hold in its title, until the ADR pull
request merges.

**4. A plan declares its flow impact.** Front matter carries `flow: none` or
`flow: <spec path>#<section>`. The plan reviewer checks the declaration against
the flow spec, and checks the plan against the accepted ADRs. A conflict
with an ADR is a blocking question: file the ADR first. The code reviewer
checks the diff against the declaration. A diff that changes a flow its plan
declared `none` is a HIGH finding.

**5. The rule is enforced at review dispatch, mechanically, before any model
runs.** A pull request that changes more than one kind receives a
`REQUEST_CHANGES` review that names the split, and no model is invoked. It
starts in warn mode: the split is posted as a comment, and the pull request
is reviewed by `code_reviewer` as it is today. Moving to block is an owner
configuration change, made once the implementation lane produces single-kind
pull requests.

**6. Each kind is reviewed by its own position.**
- `adr` → `adr_reviewer`
- `plan` → `plan_reviewer`
- `docs` → `docs_reviewer`
- `deploy` → `deploy_reviewer`
- `src` → `code_reviewer`

`tests` gets **no automatic review**. The code reviewer holds `Bash` and runs
probes at the pull request's head, so dispatching one would execute
vendor-authored files on a fleet host before a person has read them. The
reader is a human (ADR-0006 §5), and the pull request still needs the usual
approvals, so placing files under `tests/lane/` skips the model but not the
merge gate.

`deploy_reviewer` and `docs_reviewer` are dispatched **without shell or
write tools**. They read the diff and the files; they execute nothing. This is
a property of the dispatch, not an instruction in the rubric.

`adr_reviewer`, `docs_reviewer` and `deploy_reviewer` are reviewer positions
under ADR-0013 §3–§5. They take one or more runners, ordinal 1 gives the
verdict, the `'default'` row is Fable, and the review names its model.

## Amendments

**ADR-0003 §2.** The flow spec is still updated before any test is written.
What changes is where: an ADR pull request carries `Spec-impact: none` or
`Spec-impact: <spec path>`, and a named spec impact is a following `docs`
pull request (§3 above), not an edit inside the ADR pull request. The test
lane runs per plan (ADR-0006), and §3's edge makes a plan that names a spec
wait for the plan that updates it. What holds this is review, not a mechanism:
the plan reviewer checks the `flow:` declaration and its edge (§4). The old
mechanical check, whether the ADR PR touched the spec, cannot survive a rule
that forbids that PR from touching it.

**ADR-0009 §1.** The positions table gains **ADR-PR review**, **docs-PR
review** and **deploy-PR review**, all agentic. The per-repo screen's rows
gain ADR reviewer(s), docs reviewer(s) and deploy reviewer(s).

**ADR-0011 §2.** `adr_reviewer`, `docs_reviewer` and `deploy_reviewer` are
held by `us-hosted` runners only, like `plan_reviewer` and `code_reviewer`.

## Consequences

- `bin/fleet-models` gains the three positions. Its `CHECK` is fixed at table
  creation, so the live `fleet.db` needs a table rebuild.
- `bin/fleet-lane` lands test files under `tests/lane/`. This must be on the
  pin before the classifier's `tests` rule is enabled. Until then, lane pull
  requests stay excluded from automatic review by `docs/test-lane.md`'s
  existing rule.
- `dispatch-review.py` classifies a pull request by kind, applies §5, and
  resolves the reviewer from §6's position. `docs_reviewer` and
  `deploy_reviewer` each get their own rubric.
- `docs/feature-plans.md` gains `flow:`, and planning splits a multi-kind
  feature into a chain.
- A repository with no flow spec declares `flow: none` on every plan. Until it
  writes one, §4's check has nothing to compare against there.
