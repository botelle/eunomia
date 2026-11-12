# ADR-0020 — A merge is witnessed by an account that took no part in it, and the record lives outside the repositories it describes

- **Status:** Accepted
- **Date:** 2026-10-06
- **Deciders:** the operator (owner/merger)
- **Amended 2026-10-06 by the owner** — Consequences: what the records do not prove about the forge administrator (review 4046 M1).
- **Relates:** [ADR-0019](0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md)
  (the controls these records evidence); [ADR-0004](0004-ignition-gate.md)
  (agent accounts); [ADR-0010](0010-a-document-binds-only-if-something-holds-it-true.md);
  plans 0118–0121.

## Context

ADR-0019 lets a program merge code on autonomous repositories. The controls
it relies on are visible on the forge only while nothing changes them.
Reviews can be dismissed, branches deleted, statuses overwritten, and
protection rules edited without history. A claim that the controls held on
every merge needs a record taken at the time, by something that did not take
part in the merge.

## Decision

**1. Every merge to `main` of a fleet repository gets one change record.**
That includes the owner's merges and critical repositories. A record
describes what the forge showed about that merge: who authored, approved,
and merged it, at which SHAs, and with which statuses. It does not judge
whether the merge was allowed. Judging is a separate check that reads
records.

**2. The recorder is its own account, and holds no other role.** It is not
the author, a reviewer or a merger, and it never pushes to, reviews or merges
a fleet repository. It is a program: no model decides what a record says.
The merger does not write its own record. A witness to a merge cannot be a
party to it.

**3. Records live in `operator/recordstore`, and are never edited.**
Only the recorder may push to that repository's `main`. Force-push and branch
deletion are off. A correction is a new record that names the one it
supersedes. A commit that changes or removes an existing record is itself a
finding. That repository holds records, the monthly attestations and owner
dispositions. It holds no code.

**4. A missing record is a finding, not an absence.** Every first-parent
commit on a fleet repository's `main` either matches a record or is reported.

**5. A record made after the fact says so.** A record built from the forge
later than the recorder's normal window, including any backfill, is marked
as such. It is never presented as observed at merge time.

## Consequences

- `operator/recordstore` departs from the new-repository checklist: no
  required approvals, a push whitelist naming only the recorder, and no PR
  webhook. The owner adds dispositions by pull request and merges
  them. §3 makes any edit to a record visible, whoever pushes it.
- The owner creates the recorder account, `recordbot`, and gives it read
  access to every fleet repository and write access to
  `operator/recordstore` only. Its credentials go in keyvault under their
  own AppRole.
- A merge made while the recorder is down is recorded late, under §5. A
  merge it never records shows up under §4.
- The records bind the agent accounts. They are not tamper-evidence against
  the forge administrator, who is also the human merger, because the
  evidence repository is on the same forge. For an owner merge, a record is
  a dated statement by a program on the owner's own infrastructure, not an
  independent witness.
  *(Added 2026-10-06. The rest of this ADR is unchanged.)*
