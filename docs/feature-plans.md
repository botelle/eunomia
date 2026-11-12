# Feature plans — the unit of delegated work

*Why this format exists, and the one section that makes the cheap execution mode safe.
Companion to `SPEC.md` (leases, events) and `docs/fleet-history.md` (measurement).*

## The finding this rests on

A controlled bakeoff on 2026-08-26 ran the same 4-step task two ways: one long-lived
session versus a fresh session per step, identical prompts, same model, same starting
commit. Results:

- **Fresh cost 32% less** ($2.56 vs $3.74). The ~44k-token startup tax per fresh
  session is real, but smaller than the context carry it avoids.
- **Fresh lost on quality**, 15/20 against 19/20 from a blind judge.

The decisive defect is the reason this document exists. The fresh arm copied a
redaction regex verbatim out of a *neighbouring file in the same repo* and applied it
to `--sha`. Its `[A-Fa-f0-9]{40,}` branch matches every git SHA, so `pr-merged` and
`deploy-*` events silently stored `"sha": "[redacted]"` — destroying the field the
event exists to carry. It shipped untested.

In the file it was copied from, that regex is **correct**: it guards transcript free
text a human typed. In its new home it is **wrong**: `--sha` is a structured field we
defined. The fresh session saw the local cue — *this repo redacts things that look
like this* — and never saw the rule: *redact free text, never structured fields.*

That is the LOST-CONTEXT failure class from the 2026-08-17 postmortem, reproduced
under controlled conditions. The mechanism is the point: **a session that lacks the
reasoning behind a local pattern has only the pattern to go on.** Writing the
reasoning down where the next session will read it is the fix that doesn't depend on
the session being smarter.

*Scope of the evidence:* one task, one model, one run per arm. That is enough to
establish the mechanism — the copied regex is right there in the diff — and not
enough to put a number on how often it happens. The cost figures are similarly a
single pair. Treat the mechanism as demonstrated and the magnitudes as indicative.

## The rule

> A plan states not only what to build, but **which local patterns do not apply and
> why**. Deliverables tell a session what to do. Boundaries stop it from doing the
> plausible wrong thing.

A plan whose Boundaries section is empty is not finished — it just hasn't been
interrogated yet. `bin/fleet-plan lint` enforces this: it refuses a section that is
empty, still the template's text, or only placeholders, and at `status: ready` it
also flags one that says what-not-to-do without saying why. That last check is a
proxy, not a proof — it catches the honest omission, not a determined author writing
"because reasons". Per `SPEC.md` Principle 5 this tooling is cooperative; plan review
is what catches a bad-faith boundary. The question to ask before dispatch: *what would a competent
session, reading only this repo, reasonably conclude that is wrong here?*

## Format

Plans live in `plans/NNNN-slug.md`, one per feature, committed before dispatch
(canonical policy `PROVENANCE.md` §4.1 — the spec precedes generation). Front matter:

```yaml
---
id: 0007-fleet-emit
status: ready | draft | done | abandoned   # ready is the default
repo: operator/eunomia
zone: public | private          # routing: `public` DISPATCHES to a cloud backend;
                                #   anything else — private, absent, malformed,
                                #   unreadable — sends nothing (ADR-0006 §7)
tier: 0 | 1 | 2 | 3             # PROVENANCE.md §3 risk tier -> review depth
paths: ["bin/fleet-emit", "tests/**"]   # the lane; becomes the path lease
depends_on: [0002-fleet-emit-events, path:bin/fleet-claim, external:oracle-sub-backend]
repairs: [0057-ci-logs-ingested-and-served:sequencing]   # optional; see below
review: full | style            # optional; absent means full (ADR-0025)
---
```

`review:` asks for the style review (`docs/style-review.md`). `fleet-plan lint`
accepts exactly `full` or `style`, and for `style` it refuses a plan whose repository
declares no style paths in `config/style-review.json`, whose `paths` lack the plan's
evidence lease, or whose other `paths` entries fall outside the declared patterns.
`tier:` plays no part. What `review: style` does **not** do is grant anything: it is
only the plan's request. Whether a pull request actually takes the lighter review is
decided later, mechanically, from its diff by `bin/fleet-style route`, and any doubt
means the full review. The field is read from the plan on the base branch, never the head.

`depends_on` gates dispatch (`docs/plan-dependencies.md`, plan 0039):
`fleet-watch` does not dispatch a `ready` plan until every entry is satisfied.
The forms:

- a **plan id** (`0002-fleet-emit-events`) — satisfied when that plan's
  `status` is `done` **on `main`**;
- **`path:<repo-relative path>`** (`path:bin/fleet-claim`) — satisfied when
  that path exists **on `main`**, regardless of any plan's status. **Prefer
  this form**: a plan's status is a claim about work, a path is the work
  (`docs/plan-triage.md`);
- **`plan:<owner/repo>:<id>`** (plan 0180) — the plan-id form for a plan in
  another repository: satisfied when that repository's own `plans/<id>.md`
  reads `status: done` **on its `main`**;
- **`external:<slug>`** (plan 0180) — a prerequisite that is not a plan and
  never will be (a service change in another repo, an infra step applied by
  hand). It **blocks dispatch** exactly like the other forms, and nothing
  automatic can satisfy it until an owner attests `<slug>` — a row in
  `fleet.db`'s `dependency_attestation` table, written only by plan 0181's
  authenticated service, never by editing the plan.

`repairs` (plan 0089) is a separate list, and it **never gates dispatch** —
`fleet-watch` does not read it. Each entry is `<plan-id>:<class>`, naming an
earlier plan this one fixes, reverses or completes, and the kind of defect
that made it necessary:

| class | meaning |
|---|---|
| `sequencing` | built in the wrong order, or an edge was missing or pointed the wrong way |
| `contract` | two plans disagreed about an interface, a schema or a shared constant |
| `scope` | the plan's deliverables or lease left out something it needed |
| `decision` | the plan's decision was reversed or superseded |
| `code` | the plan was right and the implementation was wrong |

The class set is closed — `fleet-plan lint` refuses anything else. `code` is
an ordinary, expected class, not an admission: most defects start in code, and
a repair plan for one is the baseline the other classes are compared against.
`lint` also refuses a malformed entry, one naming the plan's own id, or one
whose id resolves nowhere in this repo — at any status, since a repair record
is history, not something dispatch waits on. `fleet-plan graph` draws it as a
separate `repairs` list, never folded into `edges`; `fleet-plan ledger` counts
it on the plan that was repaired, as `repaired_by`. Whether an existing plan
should carry this field is the owner's judgment — nothing infers it from
prose, and nothing backfills it.

Then six sections, in this order:

### 1. Goal
One sentence. What is true when this is done that isn't true now.

### 2. Deliverables
The files and behaviour. Concrete enough that "done" is not a judgement call.

### 3. Boundaries — *the load-bearing section*
What NOT to do, and **why**, in the specific terms a session would otherwise get
wrong. Each entry is a claim plus its reasoning, because the reasoning is what
transfers:

- *Don't apply `fleet-collect`'s `_SECRETISH` redaction to fields this CLI defines.
  It guards human-typed free text, where a 40-hex run is probably a token. Here
  `--sha` is a git SHA and redacting it destroys the event's payload.*
- *Don't add an env knob to make the 10MB rotation threshold testable. Test at the
  real threshold; a permanent production surface added for a test's convenience is a
  surface the spec never asked for.*

Sources for these: the SPEC's principles, the repo's own postmortems and runbooks,
and — most usefully — **the findings from the last review of adjacent code**. If a
reviewer already caught a mistake near this lane, its lesson belongs here.

### 4. Definition of done
Verifiable, and runnable by the session itself. "Tests pass" is not enough; name
which properties the tests must actually prove.

A plan that adds or changes a route or a screen leases **and updates** the spec
its repository's `governance.yaml` names for that kind of surface (`spec_watch:`,
speakhush plan 0073 §2) — the declared-behaviour document for a route, the flow
spec for a screen — in this same plan, never deferred to a later handoff note.
`fleet-plan lint` refuses a plan whose lease reaches a declared route or screen
file without reaching the matching spec too (plan 0177); the cross-repo status
`eunomia/plan-lint` runs the same check on every fleet repository's plan pull
requests, not only eunomia's own.

### 5. Handoff
What to write when finished, for whoever picks up next: decisions taken and their
boundaries — not a changelog of files touched. A handoff that lists what was built
reproduces the bakeoff failure; one that records *why the boundary is where it is*
prevents it.

### 6. Resources
Which leases the session must claim (`SPEC.md` §Lease record): branch, path globs,
sim slots, service. **A plan's own file is implicitly in-lane** for the session
executing it — it has to be, since closing a plan means editing its `status:`, and
listing `plans/*.md` in every plan's globs would make every lane overlap and defeat
the disjointness check below. Named here so a fan-out can be checked for path-disjointness at
plan review, before anyone writes code — the 2026-08-17 `RootView.swift` collision was
a plan defect that four workers then paid for.

## Lifecycle

```
draft ──(plan review: boundaries interrogated, paths disjoint)──> ready
ready ──(DoD met, PR opened with a Plan: <id> marker, handoff written)──> done
        └─ abandoned, when a plan is dropped rather than built
```

**File at `ready`; `draft` is the deliberate exception** (revised 2026-09-11).
Nothing automatic moves a plan along this line. `draft → ready` is a human act,
and `ready → done` is written by the implementer in its own work pull request —
the reviewer changes neither, and merging changes neither.

That is exactly why the default matters. Filing at `draft` puts the
authorisation in a *second* pull request, and the flip cannot be pushed into the
first once it is approved. In one session that cost two re-lands (talos #39 →
#41, eunomia #290 → #296) for a one-word change, and left four freshly merged
plans inert beside twenty already sitting here at `draft` — filed,
settled-looking, and unreachable.

**A plan that cannot dispatch is indistinguishable from one nobody has
authorised yet.** The status field is the only thing that could tell them apart
and it says the same word for both. Defaulting to `ready` means the ambiguous
state is the one you have to ask for.

Use `draft` when the plan genuinely is not a decision: the scope is still being
argued, or it was written to think with. Say which, in the pull request, along
with what would make it ready.

**Waiting is not undecided.** A plan that depends on unbuilt work is still
filed at `ready`, and a plan pull request is merged once it is approved. Every
hold is a `depends_on` edge, which `fleet-watch` enforces: a plan id, `path:`
(this repository, or `path:<owner/repo>:<path>` for another's `main`),
`plan:<owner/repo>:<id>` for another repository's plan, or `owner:<script>`.
The title never carries HOLD. An owner step is an `owner:`
edge on a script in `operator/hosttools` `bin/` whose `--check` the fleet reads
and whose `--apply` the owner runs; file the script in hosttools first.
`external:` is for a prerequisite no script can check
(`docs/plan-dependencies.md`). This replaces the 2026-09-11 rule that held a
plan by leaving its pull request unmerged under a HOLD title.

Filing a waiting plan at `draft` instead needs a second pull request to flip it
later; the `depends_on` edge needs none.

`status` in the front matter is the state, and only these four values are written.
**"In flight" is derived, never stored**: a plan is in flight exactly when a PR
carrying its marker is open. Storing it would need a writer, and the only writer of a
plan file is the PR that closes it.

A plan is dispatched only from `ready`, and only once it is reachable on `main` —
which for a plan filed remotely means only once a human has merged it.
One session per plan, and the session ends when the plan does — that is the cheap
mode from the bakeoff, made safe by §3.

A plan or ADR is **filed** when its pull request is **open** — pushing the branch
and opening the PR are one step, not two, because a branch that carries the
commit and nothing else is unapprovable and unreviewable, yet later work can
still cite it as if it were authority. `fleet-orphans` (plan 0020) is the
backstop for the sessions that stop between the two: it names, once a day, any
branch whose content never reached `main` and that carries no pull request.

## Why plans and not issues

An issue is a request. A plan is a **contract with a boundary section**, versioned
next to the code it constrains, reviewed before it is dispatched, and read by the
session that executes it. The distinction is the whole point: issues accumulate,
plans get closed.
