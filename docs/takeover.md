# Takeover — picking up a dead session's work

*The successor's procedure. Written for the person arriving mid-incident, on a
phone or a fresh terminal, who did not watch the session die. Nothing here may be
automated as written: expiry makes a lease claimable, and a human decision makes it
claimed (`SPEC.md` Principle 4). The 2026-08-17 night's ad-hoc version of this — a
root session force-pushing inside a dead worker's worktree — is the anti-pattern
this procedure replaces.*

## 1. Observe

```
bin/fleet-status
```

Orphans are flagged `ORPHANED`. A lease is derived orphaned when its holder is set
and either it was never activated within the timeout, or its liveness (heartbeat
mtime, falling back to `activated`) is older than its ttl. `fleet-status` marks and
emits `lease-orphaned` exactly once per orphaning — it never reaps.

Before anything else, ask whether the session is dead or merely quiet: a session in
a long tool call has a stale-looking heartbeat. And check the lease's `ttl_minutes`
against the session's actual turn cadence: a ttl SHORTER than the gap between
turns makes the lease genuinely flap — one `lease-orphaned` per cycle — so a
mis-set ttl shows up as event-log noise, not as a visible misconfiguration
(1522 r10). Fix the ttl; don't chase the events. The ttl already budgets for that;
trust the derivation, but if the session might still be alive on another screen,
check there first — takeover of a live session is refused by the lock-side
re-derivation, and racing it is noise.

## 2. Reconstruct before you claim

The dead session's durable state, in order of authority:

```
cat "${EUNOMIA_FLEET_DIR:-$HOME/dev/.fleet}/leases/<lease-id>.json"
```

- **The lease record** (`leases/<id>.json`): what was held, since when, granted by
  whom, the note field.
- **`sessions/<holder>/status.md`**: the worker's own last status, if it kept one.
- **The work itself**: the branch the lease names — `git log`, uncommitted changes
  in its worktree, an open PR and its thread. Under the dispatch model the PR
  thread *is* the handoff: read the ack ledger before acting on any instruction.
- **The event tail**: `bin/fleet-events --since 2h` for what the fleet did around
  the death.

Decide continue-or-abandon **now**, from the evidence — not after you hold the
lease and feel committed.

## 3. Claim

```
bin/fleet-claim --takeover <lease-id>
```

`fleet-claim` keys the holder on `EUNOMIA_SESSION`; the Stop-hook heartbeat touches
`sessions/<the hook's session id>/hb`. For a heartbeat to keep *your* takeover
alive those two must be the same value — `EUNOMIA_SESSION` must equal your Claude
session id. The launch preamble (roadmap row 5) sets that automatically for launched
sessions; **until it ships there is no automatic join.** So today, treat every
takeover — interactive or bare-terminal — like the bare-terminal case: nothing
heartbeats the holder, so finish the work or `bin/fleet-release <lease-id>` **within
`ttl_minutes`**, or the lease re-derives orphaned while you are still working and
invites a second takeover. If you do know your session id,
`EUNOMIA_SESSION=<it> bin/fleet-claim --takeover <lease-id>` restores the join; from
a bare terminal `EUNOMIA_SESSION=$(uuidgen)` at least gives the lease a stable
holder for the DB, but still will not be heartbeated.

The takeover re-derives orphanhood under the lease's lock: if the holder came back
or another successor won, you are refused with `not orphaned` — that is the race
working, not an error to fight. The winner's takeover rewrites `activated`, clears
the orphan marker, and emits `lease-takeover`.

## 4. Continue or abandon

- **Continue**: work in a fresh worktree, not the dead session's — its checkout
  state is evidence until you have extracted what you need. Commit-or-discard its
  WIP deliberately.
- **Abandon**: `bin/fleet-release <lease-id>`, and set the plan's `status:
  abandoned` through a PR if a plan drove the work. Closing a work PR unmerged does
  NOT re-arm its plan — rejection is a human decision, recorded, not an automatic
  retry.

## 5. Afterwards

The takeover is already in the event log. If the death exposed a control gap —
something a lease, a bound, or a runbook should have caught — that is a postmortem
line or a plan boundary, written down where the next session will read it, not a
memory in your head.
