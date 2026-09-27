# ADR-0008 — A feature is a sequence of plans ordered by risk, and it is watchable

- **Status:** Accepted
- **Date:** 2026-09-11
- **Deciders:** the operator (owner/merger)
- **Relates:** `plans/0039-a-plan-waits-for-what-it-needs.md` (the edges this
  needs), `plans/0036-dispatch-state-is-queryable.md` (the data the live view
  needs), `docs/plan-triage.md` (why a plan's status is evidence, not proof),
  [ADR-0005](0005-pinned-repo-reads.md) (read `main`, never a working tree).

## Context

A feature plan today describes **one unit of work in isolation**: six sections,
no relationship to anything else. That was right while the fleet ran one plan at
a time and a human held the ordering in their head.

It stopped being right on 2026-09-11, in three observable ways.

**Ordering exists only as prose.** `depends_on` is documentation — nothing in
`bin/` reads it. Plans #290 and #291 had to be split into separate pull requests
with *"HOLD until #290 is built"* typed into a title, because a sentence was the
only sequencing device available.

**The accidental serialiser was removed.** Plans used to be filed at `draft` and
armed one at a time, which ordered them by luck. Filing now defaults to `ready`,
so merging three plans arms three, and `CAP=1` serialises execution without
ordering it.

**Nothing shows where a feature actually is.** Getting a push notification to a
phone turned out to be seven linked steps across four repositories and two
external consoles. That chain was discovered by asking, then maintained by hand
on a web page, and it was wrong twice within an hour of being written — because
the shape of a feature lived nowhere but in a person's attention.

## Decision

### 1. A feature is a named sequence of plans, ordered to retire risk first

Not ordered by layer, and not by convenience. **The first step is whichever one,
if it fails, kills or reshapes the feature** — and the sequence exists to find
that out cheaply.

The canonical shape is **API before interface**: build the part that can be
verified without a human looking at it, prove it, then build the part that needs
eyes. An interface built first looks like progress and proves nothing; when the
API underneath turns out to be wrong, the interface is rework.

This is the house phrase, and it is the rule: **build a little, test a little.**

### 2. The planning session produces that decomposition, and is a distinct prompt

A feature-planning session is not an implementation session with a bigger brief.
It asks three questions and its output is the answers:

- **What are the key elements?** The parts that must exist. Not tasks — the
  things whose absence means no feature.
- **What are the key risks?** For each element: what would make this not work,
  and what is the cheapest thing that would tell us.
- **What is the sequence?** Ordered so each step *tests* a risk rather than
  merely advancing. A step that retires no risk and enables no later step does
  not belong in the sequence yet.

The session's product is a set of plans plus the edges between them. It writes
no code.

### 3. Concurrency between features is not planned centrally

**The second feature to arrive adapts.** When two features want different things
from the same component, that is the later one's problem to solve in its own
design — including redesigning the component to serve both, if that is what it
takes.

This is a deliberate refusal to build a global scheduler. The alternative —
upfront cross-feature coordination — requires knowing every future feature's
needs at the time the first is written, which is exactly the knowledge nobody
has. It also centralises a decision that the arriving implementer is better
placed to make, because it can see both requirements while the original author
could see one.

**What is refused is central *scheduling*, not declared edges** (review 2601 M1).
An earlier draft of this section said `depends_on` sequences only *within* a
feature and offered the lease model across features. That is wrong, and it
contradicts `plans/0039`'s own boundary: **a lease cannot express "exists on
main."** It governs what runs at once, not what may run at all. A planner
following the earlier text would omit a real cross-feature edge and reproduce
exactly the #290/#291 early dispatch this ADR cites as motivation — and
`0012-session-pid-binding` and `0014-fleet-pkill-session` are themselves two
features, not one.

So: **declare the edge wherever it is real, including across features.** That is
the adaptation of §3, not an exception to it — the later feature discovers the
dependency and writes it down, which is the same act as redesigning the shared
component. What no one does is maintain a global ordering on everyone's behalf.

### 4. A feature is visible twice: as planned, and as it is going

**Before execution:** the elements, the risks each step retires, and the order.
This is the artefact the planning session produces, and it is a claim.

**During:** the same shape with reality drawn over it — which steps are done,
which is running, which are blocked and on what.

**Done and running come from plan 0036's dispatch state. Blocked does not, and
cannot** (review 2601 M2). 0036 ingests the event ledger and the per-lease run
logs, and **all of it comes into existence at dispatch**: a plan withheld because
a dependency is unmet has no lease, no run log and no event, so it is invisible
to exactly the view whose job is to show it. A blocked plan looks identical to
one nobody has got to yet — which is the `draft`-on-`main` failure a third time.

Closing that is `0039`'s work, not 0036's: the withholding decision is made in
`fleet-watch`, and it is the only place that knows both the plan and the unmet
edge. Whether it lands as a new event type (which needs a SPEC change, since the
set is closed) or as state the collector reads is left to that plan — but the
requirement is named here: **"blocked, and on what" must be queryable, not only
printed to stderr and paged.**

**And it shows where the pain is.** Not only progress: which step took five
review rounds, which failed twice and was re-cut, where the retries cluster. A
step that cost three attempts is the most useful thing on the page, because it is
where the design was wrong and nobody has said so yet.

### 5. What is deliberately not added

**No estimates, no dates, no burndown.** The fleet has no basis for any of them
and they would be fiction with a number attached — which is worse than silence,
because a number invites planning against it.

**No status beyond what exists.** `draft`, `ready`, `done`, `abandoned` stay the
closed set. A feature's progress is derived from its plans and their dispatch
state, never stored as a fifth thing that can disagree with them.

## Consequences

A feature-planning session becomes a distinct thing to invoke, with its own
prompt, and its output is reviewable **as a sequence** — the ordering argument
becomes something a reviewer can disagree with before any code is written. That
is new: today a reviewer sees one plan and cannot see what it is part of.

The risk-first rule will sometimes produce a sequence that looks backwards, where
an unglamorous verification step precedes obvious work. That is the point, and it
should be defended in review rather than smoothed.

The live view depends on plan 0036 existing. Until it does, the "during" half is
hand-maintained and will be wrong — as the first one was, twice, within an hour.

Cross-feature adaptation puts real design load on whichever implementer arrives
second. That is accepted. The cost is uneven and occasionally unfair; the
alternative is a coordination layer that must predict the future.

## Alternatives considered

**Phases by layer (data → API → UI).** Rejected: it orders by architecture
rather than by uncertainty, so the risky part can sit in the last phase, which is
the standard way a project discovers it was impossible after spending its budget.

**A global dependency graph across all features.** Rejected per §3.

**Estimates and a critical path.** Rejected per §5. The fleet does not know how
long anything takes, and the durations it does have — nineteen minutes here,
three review rounds there — are outcomes, not forecasts.

**Leave feature shape in the owner's head.** This is the status quo and it worked
until three features were in flight at once. The push-notification chain is the
counter-example: seven steps, four repositories, two consoles, and the only place
it was written down was a page I built after being asked why nothing arrived.

## Definition of done

- A feature-planning prompt exists and is invocable, and produces elements,
  risks, and a risk-ordered sequence with `depends_on` edges between the plans.
- One real feature is planned this way end to end. **The push-notification chain
  is the obvious candidate**: it is already decomposed, its edges are known, and
  four of its steps are unbuilt.
- The planned sequence is reviewable as a unit — a reviewer can object to the
  ordering, not only to each plan.
- The live view derives from dispatch state rather than being written by hand,
  and shows blocked-and-on-what alongside done and running — with "blocked"
  sourced from the withholding decision itself, since no dispatch record for it
  exists.
- Retry and review-round counts are visible per step, so the expensive steps are
  legible without reading logs.
