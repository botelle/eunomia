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
---
```

`depends_on` gates dispatch (`docs/plan-dependencies.md`, plan 0039):
`fleet-watch` does not dispatch a `ready` plan until every entry is satisfied.
Three forms:

- a **plan id** (`0002-fleet-emit-events`) — satisfied when that plan's
  `status` is `done` **on `main`**;
- **`path:<repo-relative path>`** (`path:bin/fleet-claim`) — satisfied when
  that path exists **on `main`**, regardless of any plan's status. **Prefer
  this form**: a plan's status is a claim about work, a path is the work
  (`docs/plan-triage.md`);
- **`external:<text>`** — a prerequisite that is not a plan and never will be
  (a service change in another repo, an infra step applied by hand). It
  **blocks dispatch** exactly like the other two, and nothing automatic can
  ever satisfy it: the attestation is a human deleting the entry in a
  reviewed pull request.

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

**Waiting is not undecided.** A plan that depends on unbuilt work — another
plan's deliverable or a step only the owner can do — is still filed at `ready`,
and **the merge is the hold** (owner, 2026-09-11). Nothing ignites until a plan
is merged, so the owner leaves it unmerged until the prerequisite is done. Put
the hold in the **title**, where the merger sees it, and the check in the body:

    title: plans: 0099 example-consumer (HOLD until plan 0098 is built)
    body:  Merge after: plan 0098 — done when bin/fleet-example is on main

Both are invented; neither plan nor path exists.

Whether the merge surface shows a pull request's body has not been checked, so a
hold that lives only in the body is not relied on. The record for titles is
thin. #291 (0014) carried "HOLD until #290 is built" in its title, was never
merged, and nothing dispatched early. But it was closed because review found
defects in the plan text (#294), so the hold was not overridden, not proven under
pressure.

**Whether the pinned watcher enforces `depends_on` decides the rest.** Check the
pin, not `main`: `grep -c depends_on ~/.local/share/pins/eunomia/bin/fleet-watch`
prints 0 when it does not. Enforcement (#327) reached the pin on 2026-09-11.
While it is absent, the title hold is the only hold, and it is not optional.
Once it is present, also declare plan and `path:` edges in `depends_on`; the
watcher satisfies those itself, and merge order stops mattering.

**Do not also declare a merge-held owner step as `external:`.** `external:` is
cleared only by a second reviewed pull request deleting the line
(`docs/plan-dependencies.md`). A plan held at the merge *and* carrying
`external:` sits blocked after the merge that already decided it, until that
second pull request lands.

Filing a waiting plan at `draft` instead needs a second pull request to flip it
later: a second authorisation for a decision the merge already made. Holding is
not free either. If building the prerequisite changes what the held plan should
say, its branch needs a push, `forgejo-safe-push.sh` refuses an approved branch,
and the plan is re-landed. The orchestrator's one-shot `unfreeze`
(`docs/plan-dispatch.md` Part 2d) does not help, because it covers only the
implementation pull requests the orchestrator stewards, and a plan pull request
is not one of them.

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
