# fleet-requeue

`fleet-requeue <repo> <plan-id> [--dry-run]` hands a failed dispatch back to
the watcher. It releases the dispatch lease, through `fleet-release --force`
(the one writer of `lease-released`), and stops there. It never spawns: the
watcher re-dispatches under the cap and the quota gate, and a verb that spawned
would bypass both (ADR-0012 §4).

## Which route fits

| state | route |
|---|---|
| failed, **open** marked PR | owner posts `respawn` |
| failed, **closed/merged** marked PR | owner posts `redispatch` |
| failed, **no** marked PR, retryable class | mopsus releases it, up to `retry_limit` |
| failed, **no** marked PR, any other class | `fleet-requeue <repo> <plan-id>` |

## The four checks, and what to do on a refusal

1. **Exactly one dispatch lease** in `active` or `assigned`. None: nothing to
   requeue. More than one: the command lists them; work out which is real.
2. **It is dead** (`is_orphaned`). A lease inside its heartbeat TTL belongs to a
   running orchestrator; wait for it to lapse. There is no `--force`.
3. **The run failed**: a `plan-failed` event names the lease and is newer than
   its `activated`. A dead lease with none is a crash of unknown cause;
   `fleet-orphans` pages about it, so look at the run log first.
4. **No marked PR, and the check could be made.** An open PR: post `respawn`.
   A closed or merged one: post `redispatch`. An unreadable forge refuses,
   because requeuing a plan that has a PR strands it silently.

   This check reads the forge's issues index, not its `/pulls` listing (plan
   0218) — about 3s for a repo the size of operator/eunomia rather than the
   ~9 minutes `/pulls` took, which is what let this command's own timeout,
   and Lynceus's Requeue button, answer for a eunomia plan at all. See
   [plan-dispatch.md](plan-dispatch.md)'s "Dedupe" section for the detail.

`--dry-run` runs every check and writes nothing.

## Identity and counts

The actor is `EUNOMIA_SESSION` when set, else `owner-<user>` from `$USER`. It
never borrows `mopsus`'s or an orchestrator's id. No `plan-requeued` event is
emitted, so an owner's retry does not count against mopsus's `retry_limit`.
Any class of failure may be requeued: the person deciding is the judgment
ADR-0012's retryable list stands in for.
