# ADR-0029 — A finished plan's dispatch lease is released by fleet-watch

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0012](0012-a-transient-failure-may-release-its-own-lease.md) — its "What is still not permitted" forbids releasing any lease whose failure could have left work behind; a lease of a plan that reads `done` or `abandoned` is now excepted
- **Relates:** `SPEC.md` Principle 4 (this is its sixth named exception),
  [ADR-0016](0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md)
  and [ADR-0027](0027-an-implementer-may-stop-on-a-question-and-an-answer-resumes-it.md)
  (the resumes that reuse a held lease), plan 0093 (whose §3 said releasing a
  finished plan's lease would need its own ADR; this is it), plan 0086
  (`redispatch`), plan 0171 (the overlap hold), plan 0179 (the work).
- **Spec-impact:** `SPEC.md` Principle 4 gains a sixth exception, with a
  Revisions entry, and its CLI table's `fleet-release` line gains the flags §3
  needs; `lease-released` may carry a `reason`. `docs/plan-dispatch.md`
  §"Leases, death, and respawn" (a finished plan's lease is released, not left
  for the owner) and §"What the orchestrator owes this watcher" (rule 2 names
  when a release is allowed). `bin/fleet-release` gains §3's checks. All are
  changed by plan 0179.

## Context

An orchestrator that exits anywhere but the steward's end keeps its lease, on
purpose: the held lease is what stops the watcher re-dispatching the plan, and
it is the handle `respawn`, a resume request and an answer act on. When the
plan is later finished without that orchestrator, nothing releases the lease.
The sweep warns about it every cycle, and the overlap hold counts it as a
running dispatch, so a dead run blocks other plans until the owner releases it
by hand.

## Decision

**1. `fleet-watch` releases a dispatch lease of a finished plan.** A dispatch
lease is an implementation lease (`plan:<id>`) or a test lane's lease
(`lane:<id>/<runner>`). It is released when all of these hold:

- the lease derives orphaned (`SPEC.md` §Lease record);
- the plan's file on `main`, read in the same cycle, reads `done` or
  `abandoned`;
- for a lane's lease, the lane's death is already recorded (`lane-ended` with
  outcome `dead`).

A pending question or answer does not hold the lease. If an unanswered
`plan-question`, an unconsumed `Answer:` or an unconsumed resume request
names it, the release also pages the owner once, saying the plan is finished
and cannot resume, and that setting it back to `ready` is how to act on it.

**1a. Why a pending answer does not hold a finished plan's lease.** No path
can act on that lease:
- every successor path refuses a plan that is not `ready`. `_successor_gates`
  in `bin/fleet-watch` (the `status != "ready"` refusal, about line 4637) is
  shared by `respawn`, `redispatch`, a resume request and an answer;
- the sweep skips a finished plan's lease (about line 4790) before it reads
  answers (about line 4807);
- a resume request never expires.

A hold would therefore last forever and block the overlap check while it did.
ADR-0027 §6's "no question is dropped" is kept by telling the owner, not by
keeping a lease nothing can use. A correction to a finished plan is acted on
by reopening the plan, which is a reviewed change to its status.

**2. Only the plan's status counts, never its pull requests.** A lease of a
plan that reads `ready` is never released under this record, whatever its
marked pull requests say. Which pull request belongs to which run cannot be
recovered from a branch name or a pull request: `redispatch` reuses branch
names, and an earlier run's merged or closed pull request looks like the
current run's. Two review rounds on this record found a release that would
have abandoned a redispatched run that way. For a `ready` plan the existing
path stays: the sweep's page, then the owner's `redispatch`, `respawn` or a
change of status.

**3. A release is checked under the lease's lock, and recorded.** It goes
through `bin/fleet-release --force`, which must re-derive orphanhood and
confirm the holder is still the one the watcher read, under the lease's lock,
and refuse otherwise, as `fleet-claim --takeover` does. `lease-released`
carries `forced`, `holder_was` and the fact as `reason`, which is exactly one
of `plan-done` or `plan-abandoned`; `fleet-release` refuses any other value. A
release with no reason is a defect.

**4. A release is not a dispatch.** The release spawns nothing, consumes no
`respawn`, `redispatch`, resume request or answer, and leaves the lease file in
place. A finished plan is not `ready`, so nothing re-dispatches it.

**What is still not permitted.**
- Releasing on heartbeat age or liveness alone. Orphanhood is necessary and
  never sufficient.
- Releasing any lease of a plan that is not `done` or `abandoned`, on any
  evidence from pull requests or branches.
- Releasing a lease whose holder is live. That holder releases its own lease.
  Releasing it from outside is a takeover, and would make the pre-push hook
  refuse the live process's pushes.
- Releasing a `service`, `sim`, `budget` or `paths` lease. None of them has a
  plan.
- Acting on the event log where the forge can be read (Principle 3). The
  question, answer and resume-request state in §1 is read only to decide
  whether the release also pages, never to release or hold a lease.

Each of these needs its own record.

## Consequences

`SPEC.md` Principle 4 names a sixth exception. A holder that releases its own
lease is not an exception. The steward's release when the pull request it
watched is merged or closed is unchanged: it watched that pull request itself,
so no identity is inferred. On any other exit, the orchestrator releases only
when the plan's file on `main` reads `done` or `abandoned`, with the same
reason and the same one-time page when something pending names the lease.
Orphanhood, the same-cycle read and lane death are the watcher's conditions
and do not apply to a live holder. `docs/plan-dispatch.md`'s rule 2 ("never
release the lease without a marked PR existing") is restated to say this. Its
reason, that a release must never return a `ready` plan to dispatch, is
unchanged.
