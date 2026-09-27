# ADR-0010 — Only an ADR binds, an ADR can be amended, and an ADR is short

- **Status:** Accepted
- **Date:** 2026-09-13
- **Deciders:** the operator (owner/merger)
- **Relates:** **[ADR-0003](0003-adr-track.md)** (the track this extends: 0003
  says how a changed decision is verified, this says which artifacts are
  decisions), **[ADR-0006](0006-blackbox-test-lanes.md)** (amended twice on the
  day this was written — the worked example of §2), `docs/fleet-db.md` and
  `plans/0048-a-repo-chooses-its-models.md` (first document corrected under §3).

## Context

`docs/fleet-db.md` states: *"It is a read-only view over history that already
exists — nothing writes back."* That was true when written. `bin/fleet-reviews`
now creates `review` (1,170 rows) and `model_price` in that database and fills
them from Forgejo.

A session read the sentence, concluded configuration could not live in
`fleet.db`, and designed around a constraint that had stopped existing. The
schema contradicting it was on screen at the time.

The owner's framing:

> at the beginning of the project you know the least, so taking everything
> that's not an ADR — and not double checking ADRs that will need to change as
> well — and then having it hamstring the rest of the project is something that
> we don't want to do.

## Decision

**1. Only an ADR binds.** Docstrings, `README`s and everything under `docs/`
describe intent at time of writing. Quote them with their source and their age —
*"`docs/x.md` says Y"*, never *"Y"* — so a reader can check the claim in one
step. An uncited prohibition is a present-tense assertion with no present-tense
evidence.

**2. An ADR can be amended, and one that blocks the work is a decision to
surface.** Three moves exist: route around it (forbidden), accept it silently
(the failure above), or raise the conflict and let the owner decide. The same
applies with more force to a non-ADR document: **widening a store, format or
scope defined at creation is a question to ask, not a constraint to design
around.**

**3. A document that constrains design carries a test, or is marked as
description.** *"The watcher checks and moves on"* is description. *"Nothing
writes back to this database"* is a factual claim someone will design against;
that kind gets a test that fails when it stops being true. Where a claim cannot
be tested, mark it as description and date it. This applies to sentences that
would change someone's design, not to every sentence.

**4. Prose is not a lease.** A document cannot reserve a namespace, a table or a
file format. Constraints that matter live in code that refuses, in a `CHECK`, in
a test, or here. `fleet-secret-guard` is the model: it does not ask sessions to
remember, it refuses, and names the pull request that would change its mind.

**5. An ADR states the decision and the reason, and stops.** Because §1 makes
everything here binding, **length is a liability**: a sentence added for colour
becomes a requirement nobody chose. Narrative, worked examples, measurements and
postmortems belong in the pull request body or a `docs/` page, where §1 says they
do not bind. If a paragraph would not survive being read as a rule, it does not
belong in an ADR.

## Consequences

`docs/fleet-db.md` is the first document corrected under §3, in plan 0048: two
tiers — tables `fleet-collect` drops on a schema bump, and durable tables it must
not — with a test asserting the tiers match the source.

Naming the artifact behind a prohibition costs a sentence, and makes staleness
visible rather than invisible. Documents will be found wrong; that is the normal
case, not a defect, and the fix is a dated correction in the pull request that
found it.

**This ADR is subject to itself**, §2 and §5 included.
