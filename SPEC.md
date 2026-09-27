# eunomia SPEC — ledger, leases, events

*v0.2, 2026-08-24 (v0.1 + revbot review 1313 findings). The contracts everything else
builds against. Design rationale lives in the coordination-plane note (athena:
`Products/agent-bus/coordination-plane.html`); the incident evidence is
`infra/runbooks/parallel-workstreams-postmortem-2026-08-17.md`.*

## Principles

1. **One serial writer per contended resource.** The merge queue writes main, the broker
   writes the vault, the deploy gate writes prod, the integrator writes shared files.
   Everything else runs parallel under a lease.
2. **No shared state outside flat files. A scheduler may be resident; a store may not.**
   Flat files under `~/dev/.fleet/`; write atomicity from `flock(2)` + rename; readers
   never lock. SQLite is out of scope until a writer lives off-opshost. A long-running
   process is permitted only where it holds a `service` lease for itself, keeps every
   fact in those files, and can be killed at any instant without losing one — it
   schedules, it never remembers. See Revisions, 2026-09-10.
3. **The log is awareness, not authority.** Forgejo's API stays ground truth for PR/merge
   state; the event log is what sessions read to know what the rest of the fleet did.
   Nothing gates on the log where a ground-truth check is available.
4. **Nothing automated acts on staleness — with one narrow, named exception.** Expiry
   makes a lease claimable and emits an event; takeover of a **lease** is always an
   explicit claim by a human-spawned successor, unchanged. The exception is a **pin**
   (`FLEET_PINS`, never a lease): `bin/fleet-pin-advance` may fast-forward one from
   `stale` to a commit already reachable on `main` of a repo the Forgejo protection
   broker vouches for — never `reset`, never a checkout of a ref that is not a
   descendant of HEAD, and never any `pin_status` state but `stale`. See Revisions,
   2026-09-10. The second exception is a failed **dispatch** lease: `bin/mopsus` in
   `mechanical` mode may release one whose class shows no work began, at most
   `retry_limit` times per plan, recording each as `plan-requeued` (ADR-0012).
5. **Cooperative, not adversarial.** Identity and enforcement assume sessions run our own
   tooling honestly; the ledger defends against races and forgetfulness, not against a
   malicious agent. (The review gates and vault policies are the adversarial layer.)
6. **Nothing under `~/dev/.fleet/` may contain secret material.** Not the log, not lease
   files, not `status.md`, not CR patch snippets — atlas renders this tree. Events about
   credentials record the fact, scope, and TTL, never the value.

## Session identity

`EUNOMIA_SESSION` — the Claude Code session UUID, set by the launch-prompt preamble and
read by every CLI and hook (`--session` overrides for tests). Subagents inherit the parent's
id; a lease is held by a session, not a subagent. Hosts other than opshost do not mount the
ledger: their `fleet-*` calls shell to opshost over ssh, and MUST use ControlMaster connection
reuse (`~/.ssh/config` `ControlPersist`) — one turn = at most one new TCP connection (the
2026-08 TIME_WAIT lesson).

## Directory layout (runtime — never committed)

```
~/dev/.fleet/
  leases/<lease-id>.json          lease record; replaced only by atomic rename
  leases/<lease-id>.orphaned      orphan marker (see Heartbeat); removed on takeover/release
  locks/<lease-id>.lock           stable per-lease lockfile — never deleted
  locks/events.lock               serializes append + rotation
  locks/pool-<type>.lock          serializes pull-model --next scans
  sessions/<session-id>/hb        heartbeat; liveness = mtime
  sessions/<session-id>/status.md free-form worker status (root/successors read it)
  events.jsonl                    append-only; rotated at 10 MB
  events-YYYYMMDD-HHMMSS.jsonl    rotated archives (timestamped — no same-day collision)
  cr/<repo>/<lane>-<nnn>.json     change-requests for integrator-owned files
```

`lease-id` = `<resource-type>--<slug>--<nnn>` (e.g. `branch--sniff-w1-screens--001`).

## Locking protocol (applies to every ledger write)

- **Writers:** `flock(EX)` the stable lockfile → re-read the data file → validate the
  expected state still holds → write a temp file in the same directory → `rename(2)` over
  the data file → unlock. The lockfile is a separate, permanent file precisely so rename
  never invalidates a held lock.
- **Readers:** plain read of the data file, no lock. Rename is atomic, so a reader sees a
  complete old version or a complete new one, never a torn write.
- **Event appenders:** `flock(EX)` `locks/events.lock` → open `events.jsonl` **by path
  after acquiring the lock** → append one line → unlock. The rotator takes the same lock,
  renames to a timestamped archive, creates a fresh `events.jsonl`, unlocks. Open-by-path
  under the lock is what keeps a post-rotation append out of the archive.

## Lease record

```json
{
  "id": "paths--sniff-featurea--003",
  "resource": {
    "type": "branch | paths | sim | service | budget",
    "repo": "operator/sniff",            // branch, paths
    "branch": "w1/screens",             // branch
    "globs": ["ios/Sniff/FeatureA/**"], // paths
    "host": "opshost",                     // sim, service
    "udid": "E8B4-...",                 // sim (filled at activation)
    "service": "edgehost:minos.service",    // service
    "tokens": 5000000                   // budget
  },
  "holder": "session-09b0b49c",         // null on unclaimed pool rows (pull model)
  "granted_by": "session-root-abc",
  "state": "assigned | active | released",
  "ttl_minutes": 240,
  "created": "2026-08-23T21:04:11Z",
  "activated": null,
  "note": "W1 screens lane"
}
```

**Stored states are exactly `assigned | active | released`. Orphaned is a derived
condition, never a stored state:** a lease is orphaned iff `holder` is set **and** either
`state=assigned` with `created` older than `EUNOMIA_ACTIVATE_TIMEOUT` (default 10 min), or
`state=active` with the holder's liveness older than `ttl_minutes`. Liveness =
`sessions/<holder>/hb` mtime, or the lease's `activated` timestamp while no hb file exists
(so before the heartbeat hook ships, an active lease is claimable only `ttl_minutes` after
activation — never instantly). Unclaimed pool rows (`holder: null`) are queued work, never
orphans, and are claimed only through `--next`, preserving oldest-first order. Any reader derives it; nobody
rewrites a lease to record it. Takeover: `fleet-claim --takeover <id>` locks the lease's
lockfile, **re-derives orphanhood under the lock**, and only then rewrites
`holder`+`activated` (state stays/becomes `active`), removes the `.orphaned` marker, and
emits `lease-takeover`. Two racing claimants serialize on the lockfile; the loser's
re-derivation finds a fresh heartbeat and aborts.

## Grant flow

- **Push (planned fan-out):** the coordinator writes rows `state=assigned` with `holder`
  pre-set to the intended worker, and bakes each lease id into the launch prompt. The
  worker's first act is `fleet-claim --activate <id>`: verify `holder == EUNOMIA_SESSION`,
  set `activated`, start the heartbeat, emit `lease-activated`.
- **Pull (generic executors, agent-bus poller shape):** pool rows are written with
  `holder: null`. `fleet-claim --next <type>` takes `locks/pool-<type>.lock`, scans
  `leases/` oldest-`created`-first for `state=assigned && holder==null`, claims the first
  via the per-lease write protocol, unlocks the pool lock, emits `lease-activated`. The
  pool lock makes scan-then-claim atomic against other pullers.

## Heartbeat

The Claude Code Stop hook (alongside the existing workspace autosync) touches
`sessions/<id>/hb` once per turn; off-opshost sessions touch via their ControlMaster ssh
channel. Staleness = `now − mtime > ttl_minutes`. Heartbeats never write lease files —
mtime *is* the liveness record; the no-rewrite rule is scoped to liveness, not to legal
state transitions (activate/release/takeover), which go through the locking protocol.

`fleet-status` derives orphanhood and, on first observing it, creates
`leases/<id>.orphaned` with `O_CREAT|O_EXCL` (the excl create is the once-only guard) and
emits `lease-orphaned`. Takeover and release remove the marker, so a lease that orphans
again emits again.

## Event record

One JSON object per line, appended under the events lock:

```json
{"ts":"2026-08-23T21:07:40Z","actor":"session-09b0b49c","type":"pr-opened",
 "repo":"operator/sniff","pr":41,"sha":"a1b2c3d","lease":null,"detail":{}}
```

`sha` semantics: on `pr-*`/`review-posted`/`candidate-testing` events it is the PR head
SHA; on `pr-merged` and `deploy-*` events it is the merge commit on main. Lease events
(`lease-*`) carry the lease id in `lease`; non-lease events set it null, except `plan-requeued`,
which carries the lease it released.

Event types (v0.2 closed set — additions require a SPEC PR):
`session-spawned` `session-stopped` `session-killed` `lease-assigned` `lease-activated` `lease-released`
`lease-orphaned` `lease-takeover` `cr-filed` `cr-applied` `pr-opened` `pr-pushed`
`review-posted` `pr-armed` `candidate-testing` `pr-merged` `pr-bounced` `token-minted`
`token-revoked` `deploy-started` `deploy-verified` `budget-checkpoint`
`plan-dispatched` `plan-done` `plan-failed` `plan-blocked` `capability-published`
`pin-drift` `repo-model-changed` `fleet-setting-changed` `plan-requeued`.

`actor` is normally the acting session's id, but `session-killed` (plan 0014,
`fleet-pkill --session`) is emitted by a verb that kills someone ELSE's
session on an operator's say-so, and the walk that attributes it can prove a
*process*, not a person: `actor` on this event type is one of a bound
session id, `session-pid:<pid>` (a Claude ancestor was found but is not
itself bound to any session), `unattributed` (no Claude ancestor at all —
never defaulted to `human`, which would be a claim the mechanism cannot
support), or an explicit value a sanctioned consumer states for itself via
`--killer`. `detail.killer-basis` (`ancestry` | `explicit`) says which.

Producers: `fleet-*` CLIs (lease + cr events), the Stop hook (`session-stopped`), the
launch preamble (`session-spawned`), the minos webhook forwarder (normalizes Forgejo
webhooks → `pr-opened`, `pr-pushed`, `review-posted` — it drops Forgejo's merged webhook),
the merge queue (sole producer of `pr-merged`; also `pr-armed`, `candidate-testing`,
`pr-bounced`), the broker (`token-*`), the deploy wrapper (`deploy-*`), and
`fleet-status --budget` (`budget-checkpoint`, from the coordinator session or cron),
the plan watcher (`plan-dispatched`, and `plan-failed` when a dispatch itself
fails) and the per-feature orchestrator (`plan-done`, `plan-failed`) — row 6b,
plan 0004 — and whoever lands a capability change (`capability-published`, plan
0025: cooperative, no merge hook, so an unemitted change stays invisible), and
the plan watcher again for `pin-drift` — a pinned worktree whose HEAD no longer
matches its `origin/main`, i.e. reviewed work that is not reaching sessions.
`capability-published` says a capability CHANGED; `pin-drift` says a change has
not arrived. `capability-published` is read by long-running **planning** sessions
and is never an input to automation (Principle 4).

`bin/fleet-pkill --session` (plan 0014) produces `session-killed` when a human
ends another session's hung host process on the ledger's say-so — `detail`
carries `session` (the sid killed), `pid`, `reason` (operator text, scanned
against `config/secret-patterns.conf` and size-capped before anything is
signalled), `killer`, and `killer-basis`.

The plan watcher also produces `plan-blocked` (plan 0039): a `ready` plan it
declined to dispatch because a `depends_on` entry (`docs/plan-dependencies.md`)
is not yet met — `detail` carries `plan`, `dependency`, `form`
(`plan`/`path`/`external`) and `reason` (`unmet`/`abandoned`/`external`/
`dangling`). Emitted **on transition only** — the first cycle a plan is
blocked, or when the reason changes — never once per cycle, the same
discipline `pin-drift` already follows. There is no `plan-unblocked`: a plan
that clears its block already produces `plan-dispatched`, which is the exit.

`bin/fleet-pin-advance` (plan 0034) also produces `pin-drift`, for the one
occurrence it is not purely awareness of someone else's change: when it fast-forwards
a `stale` pin under Principle 4's exception, the transition it just caused (`state:
"current", previous: "stale"`) is reported the same way `check_pins` reports any other
transition, with `detail["action"] = "advanced"` plus `from`/`to` SHAs marking it apart
from a human's own re-point. `repo` is the pin's own `origin`, `lease` is null (a pin is
not a lease). No new event type: `bin/fleet-emit` and `bin/fleet-events` both enforce
the closed set above in code, and reusing `pin-drift` (rather than adding a
`pin-advanced` sibling) needed no change to either.

`bin/fleet-models` (plan 0063) produces `repo-model-changed`, one per row it
writes to `repo_model_change`, so the per-repo model configuration in `fleet.db`
can be derived from this ledger if the database is lost. `actor` is the change's
actor (`cli:<user>` or a control-app identity, not a session id), `repo` is the
repo the row configures (or `default`), `lease` is null. `detail` carries
exactly: `change_id` (the ULID of the matching `repo_model_change` row),
`source`, `position`, `ordinal`, `old_runner`, `new_runner`, `old_disabled`,
`new_disabled` — a `set` fills `new_runner`, a disable/enable fills
`new_disabled`, and a field that does not apply is null. **Runner names,
positions and ordinals only. Never a credential**: this file carries every other
event and `fleet-events` does not redact, so a configuration key that holds a
secret must not be added to this event; it needs its own path, with its own
redaction, decided in its own plan. These events may be replayed into an absent
`repo_model` and never into one that survives (`reconstruct_repo_model` in
`bin/fleet-models`; `docs/fleet-db.md`).

`bin/fleet-config` (plan 0068) produces `fleet-setting-changed`, one per row it
writes to `fleet_setting_change`, the same durability shape as
`repo-model-changed` on a plainer table: a fleet-scoped setting (currently only
`dispatch_cap`, the concurrency cap `bin/fleet-watch`'s `cycle()` resolves every
poll) has no home but `fleet.db` unless this ledger carries it too. `actor` is
the change's actor (`cli:<user>` or a control-app identity, not a session id),
`repo` and `lease` are null — a setting is fleet-scoped, not repo- or
lease-scoped. `detail` carries exactly `key`, `old`, `new` — the setting's name
and its value before and after, `old` null on a first write. **The setting's
name and value only. Never a credential**, for the same reason
`repo-model-changed` states: this file carries every other event and
`fleet-events` does not redact, so a setting that holds a secret needs its own
path, with its own redaction, decided in its own plan. These events may be
replayed into an absent `fleet_setting` and never into one that survives
(`reconstruct_fleet_setting` in `bin/fleet-config`; `docs/fleet-db.md`).

`bin/mopsus` in `mechanical` mode (plan 0078, ADR-0012) produces `plan-requeued`
each time it releases a failed dispatch lease so the watcher can dispatch the
plan again. `actor` is `mopsus`, `repo` is set, `lease` is the released lease.
`detail` carries `plan`, `lease`, `class`, `attempt` (1-based) and `limit`
(`retry_limit` at the time). The count of these events per `(repo, plan)` is what
bounds the loop; mopsus's own state file is never the count.
Consumers: `fleet-events` (tail/filter CLI), sessions (read the tail before touching shared
state), the atlas Fleet page (read-only). Rotated files are the audit archive.

## Change-request record

`cr/<repo>/<lane>-<nnn>.json`:

```json
{
  "id": "CR-w1-007",
  "repo": "operator/sniff",
  "lane": "w1",
  "target": "project.yml",
  "intent": "add SniffTests target with GENERATE_INFOPLIST_FILE",
  "patch": null,
  "for_pr": 41,
  "state": "filed | applied | rejected",
  "filed": "2026-08-23T21:10:00Z"
}
```

`intent` (prose) or `patch` (unified diff), at least one; **patches must not contain secret
material** (Principle 6 — this tree is rendered by atlas). `fleet-cr file|apply|list`
manage the queue; `apply` is run only by the integrator lane, transitions state under the
per-file locking protocol, and emits `cr-applied` with the applying commit in `detail`. A
PR body's `needs: CR-w1-007` line is a merge-queue condition: the queue checks the CR's
`state=applied` **and** that its applying commit is reachable from main (Forgejo API — the
log is not the authority, per Principle 3).

## CLI surface (PRs #2–#4)

```
fleet-emit    <type> [--repo --pr --sha --lease --detail-json]   append one event
fleet-events  [--follow] [--type ...] [--since ...]              read the log
fleet-claim   --assign <resource-json> [--holder <id>]           coordinator grant (pool row if no holder)
fleet-claim   --activate <lease-id>                              worker's first act
fleet-claim   --next <type>                                      pull model
fleet-claim   --takeover <lease-id>                              claim a derived-orphan lease
fleet-release <lease-id>
fleet-status  [--json] [--budget]                                leases ⋈ heartbeats, orphans derived+marked
fleet-cr      file|apply|list ...                                change-request queue
```

All CLIs are bash or python3, stdlib only, exit non-zero loudly (no swallowed failures —
postmortem class D), and never print secret material.

## Enforcement points

- **git pre-push hook** (installer in this repo): refuses a push to a branch whose active
  lease is held by a different session, or touching integrator-owned paths outside your
  lane (`OWNERSHIP.yml` in the target repo, when present). On non-opshost hosts the check
  shells to opshost over the ControlMaster channel.
- **sim wrapper**: refuses to boot a 4th simulator on a host; binds UDID to the lease;
  provides `fleet-pkill` scoped to the caller's PID tree.
- **deploy wrapper** (infra `lib/deploy.sh` gains the check): requires an active `service`
  lease, and verifies the deployed SHA is reachable from the target repo's main **via the
  Forgejo API** (ground truth, per Principle 3); emits `deploy-started`/`deploy-verified`.
- **budget lease** (v0, advisory): the launch preamble records the slice;
  `fleet-status --budget` compares recorded slices against the usage tally and emits
  `budget-checkpoint`; hard enforcement (refusing spawns) is deferred until the tally has a
  reliable per-session source.
- **launch-prompt preamble** (template in this repo): set `EUNOMIA_SESSION`, emit
  `session-spawned`, claim-or-wait, read `controls.yml` for repos you touch, read the event
  tail, write `status.md` before long operations.

## Non-goals (v0)

Cross-host ledger mounts (opshost only; other hosts go through CLIs over ssh), auto-reap,
queue-position fairness, a web UI in this repo (atlas owns the read-only view), replacing
Forgejo state, adversarial enforcement (Principle 5), encrypting the log (Principle 6 makes
it unnecessary).

## Revisions

### 2026-09-10 — a scheduler may be resident

Principle 2 read, from v0 until this revision:

> 2. **No daemon, no database.** Flat files under `~/dev/.fleet/`; write atomicity from
>    `flock(2)` + rename; readers never lock. SQLite is out of scope until a writer lives
>    off-opshost.

Two words of that carried more than they were written to carry. "No database" is the
principle: state lives in files any session can read, and a crash costs nothing because
nothing was held in memory. "No daemon" was the *implementation* that followed from it in
v0 — and being stated as a rule, it made `fleet-watch` a `StartInterval` job that exits
every cycle, which turned out to be the reason merge-is-ignition could not work at all.

**The measurement.** `fleet-watch` spawns each orchestrator with `Popen(...,
start_new_session=True)` and then exits, so every orchestrator is reparented to launchd
within seconds. On macOS a process whose responsible process has exited cannot open a
local-network connection from a newly-launched binary. Six of six autonomous dispatches
died at their first `git fetch`, and a shape matrix run on 2026-09-10 separated it
cleanly — 15 samples, three rounds, no overlap:

| shape | parent | result |
|---|---|---|
| launchd job runs git itself | alive | 3/3 ok |
| child, parent waits | alive | 3/3 ok |
| detached child, parent stays alive | alive | 3/3 ok |
| detached child, parent exits (**v0's shape**) | pid 1 | 3/3 **fail** |
| the same, over HTTP instead of ssh | pid 1 | 3/3 **fail** |

Both transports fail identically, and raw sockets from the same orphan succeed — so it is
not the network, the port, or ssh. It is that the orphan cannot lend a launched binary a
live responsible process. `Undefined error: 0` from ssh and `Failed to connect ... after
3 ms` from curl are both an instant policy denial, not a timeout.

**What is unchanged.** Every fact still lives in flat files under `~/dev/.fleet/`, still
written under `flock(2)` + rename, still readable without a lock, and still complete
enough that killing the process loses nothing. A resident scheduler holds no state; that
is what makes it a scheduler rather than the daemon this principle refused. It must hold
a `service` lease for itself (plan 0029) so two of them cannot run — the same rule the
2026-09-07 cihost incident produced, applied to the fleet's own supervisor.

**What this does not license.** Not a process that answers questions from memory, not a
cache with a lifetime, not a queue that exists only in a running process, and not SQLite.
A reader must never have to ask a process for the fleet's state.

### 2026-09-10 — a merged commit reaches every session without a human

Principle 4 read, from v0.2 until this revision:

> 4. **Nothing automated acts on staleness.** Expiry makes a lease claimable and emits an
>    event; takeover is always an explicit claim by a human-spawned successor.

That is right for **leases**, and stays right for leases: taking one over is still always
an explicit claim by a human-spawned successor, unchanged by anything below.

It had also been read as covering **pins** — `bin/fleet-watch`'s own `check_pins`
docstring cites it verbatim: "the watcher's contract is that it spawns, emits and
notifies, never acts on staleness." That reading produced a real cost. Every scheduled
unit runs from a pin (`~/.local/share/pins/<repo>`), never from a live checkout, so a
merge changed nothing until a human moved one by hand — on 2026-09-10 the owner was
asked to do that five times in one day, once between each merge and the fix taking
effect, and twice in that gap the fleet ran code that had already been replaced on
`main` an hour earlier.

**What is now permitted, and only this.** `bin/fleet-pin-advance` may fast-forward a
pin that `pin_status` reports as `stale` to the target it is stale against, when — and
only when — that target is a descendant of the pin's current HEAD (`git merge-base
--is-ancestor`, never `reset`, never a checkout of a non-descendant ref) and the Forgejo
protection broker vouches for the pin's own repo (the same verdict `fleet-watch` itself
runs dispatch on). The argument is merge-is-ignition, applied one hop further down:
reaching that commit on a protected repo's `main` already required a reviewed human
merge, so a pin fast-forwarding onto it exercises no authority beyond what the merge
already exercised. Every other `pin_status` state — `dirty`, `attached`, `missing`,
`not-a-repo`, `unfetchable` — is still a hard refusal, named, never collapsed into a
truthiness check; an unreadable or refused broker verdict is "could not tell," never
"advance anyway."

**What is still not permitted.** Nothing here touches a **lease**. A lease's staleness
still only ever produces an event and a claimable state; nothing but a human-spawned
successor's explicit `--takeover` moves one. And advancing a pin is not the same act as
loading it: `bin/fleet-pin-advance` never restarts a resident process that reads a pin
(`fleet-watch` foremost). A resident scheduler holds the code it loaded at start, so it
does not see the advance until it is restarted — deliberately, under a `service` lease
via `bin/fleet-svc` (plan 0029), never a bare `launchctl kickstart`. `fleet-pin-advance`
reports that a restart is due; it does not perform one.

### 2026-09-11 — the resident watcher loads what was merged

The previous revision left a gap named but not closed: "under a `service` lease via
`bin/fleet-svc` … never a bare `launchctl kickstart`" is still the rule for restarting a
resident process from the outside, and stays the rule for every process that does not
hold its own lease. `fleet-watch` does hold its own — the `service` lease this same
revision series required it to take out — and `fleet-svc` refuses every caller but the
holder (plan 0029). Nothing outside it is entitled to restart it, which left only the
process itself.

**What is now permitted, and only this.** The `service` lease's holder may end its own
loop, between cycles, once it sees that the worktree its own code was loaded from has
moved to a clean commit on a reviewed trunk (the same `pin_status` states
`fleet-pin-advance` already gates on) and nothing it launched is still running. Ending
the loop through the existing shutdown path releases the lease exactly as a signal would,
and `KeepAlive` relaunches the watcher, which then loads the merged code. This is not a
new exception to "nothing automated acts on staleness": it is the one lease-holder this
principle always let take its own state, applied to a scheduler instead of a lock file.

### 2026-09-24 — a failed dispatch that did no work may be released by mopsus

Principle 4 read, from the 2026-09-10 revision until this one:

> 4. **Nothing automated acts on staleness — with one narrow, named exception.** Expiry
> makes a lease claimable and emits an event; takeover of a **lease** is always an
> explicit claim by a human-spawned successor, unchanged. The exception is a **pin**
> (`FLEET_PINS`, never a lease): `bin/fleet-pin-advance` may fast-forward one from
> `stale` to a commit already reachable on `main` of a repo the Forgejo protection
> broker vouches for — never `reset`, never a checkout of a ref that is not a
> descendant of HEAD, and never any `pin_status` state but `stale`.

It now names a second exception: `bin/mopsus` may release a failed **dispatch** lease
whose failure class shows no work began, at most `retry_limit` times per plan. Six of
30 failed dispatches measured on 2026-09-24 were forge-unreachable failures that each
waited for a person whose only act was a retry. A release is not a dispatch: the watcher
still re-dispatches under `dispatch_cap`. Takeover of a live lease is unchanged. See
`docs/adr/0012-a-transient-failure-may-release-its-own-lease.md`.
