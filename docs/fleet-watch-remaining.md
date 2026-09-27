# fleet-watch — what stands between a merge and a dispatch

*Companion to `docs/operating.md` (the measured state) and `docs/plan-dispatch.md`
(the design). Rewritten 2026-09-21: the first version of this page (2026-09-08)
listed six gates, and four of them have since been passed. What follows is what
is still true, measured on opshost against `main` @ `7d91402`.*

## What is no longer between the watcher and its job

Kept as one table so a reader of the old page knows why the items are gone.

| gate (2026-09-08) | now |
|---|---|
| the unit is not installed | `org.eunomia.fleet-watch` is loaded and resident (pid live in `launchctl list`); `watch.log` has cycles every 120 s |
| the orchestrator is 1 of 5 phases | all five phases are built; `orchestrator --ready` exits 0 (plan 0032's note in the module docstring) |
| the failure channel is not connected | `FLEET_NTFY_URL` is set on every unit plan 0058 inventories; pages land in `pages.jsonl` with `delivered: true` and on the phone |
| respawn is disabled | `FLEET_OPERATOR_UID` is set on the loaded unit; the `respawn` comment path is armed |
| the broker must be reachable | `org.eunomia.fleet-broker-tunnel` is loaded; the verdict for every enrolled repo reads `ok: true` (checked 2026-09-21T23:10Z) |

The fleet has dispatched: eunomia plans 0058 through 0065 and infra 0009 all
went merge → orchestrator → pull request → review through this watcher.

## What is still between a merge and a dispatch

### 1. One slot, and a steward holds it

`FLEET_WATCH_CAP` defaults to 1 and the plist does not set it. The count is
every non-orphaned lease, and an orchestrator whose PR is approved and waiting
for a human merge is a non-orphaned lease. Measured 2026-09-21: the infra 0009
orchestrator sat in `steward-start` from 14:53Z to 23:04Z — eight hours — and
`watch.log` said `respawn for 0065-… deferred — at cap 1` on every cycle of it.
Nothing was spending; nothing could start.

Plan 0067 stops counting stewards. Plan 0068 makes the cap a `fleet.db` setting
one command changes, so raising it is not a plist edit and a restart.

### 2. A dead run is not resumed unless a person types `respawn`

The watcher pages once (`Orchestrator dead … post \`respawn\` as a TOP-LEVEL
comment`) and stops. That is the designed default and it is right for a run that
died for a reason nobody has read. It is also why eunomia#487 sat for eight hours
after its fix round pushed nothing: the page arrived, the comment did not. The
half of parked note 0035 that would adopt an open, clean branch automatically is
not built, and is the plan to file if this recurs.

### 3. Enrolment is a record, not a dispatch

`FLEET_WATCH_REPOS` on the loaded unit is the allowlist (41 repos on
2026-09-21). `config/repos.conf` is written by `fleet-repo` and read by
`fleet-orphans` and `fleet-repo` — never by the watcher. Plan 0013's
main-via-contents-API half is the only `draft` plan on `main` that is real,
unbuilt work; until it lands the environment variable is the truth and the
record is documentation.

### 4. A red build is briefed by name only

The CI-failure prompt lists failing check names, not the log. Plan 0045 stores
the log in `fleet.db`; nothing reads it back into the fix round. eunomia#487's
fixer "could not reproduce" a failure whose six test names were 343 lines away.
Plan 0069 hands the excerpt to the session.

## What the watcher will still refuse, by design

Worth knowing before reading a quiet log as a problem. These are permanent
behaviours, not remaining work:

| condition | behaviour |
|---|---|
| repo not in the allowlist | never read at all |
| `main` unprotected, or any lane an agent could use to push, approve or merge | REFUSED — "merge is ignition; an unprotected main makes ignition forgeable" |
| protection verdict undetermined (transport failure) | UNDETERMINED — nothing dispatched, nothing vouched for |
| token rejected (401/403) | its own page — a stalled token is not a network fault |
| a marked PR exists in ANY state | the plan is done; a closed work PR does not re-arm it |
| dedupe scan truncated | never spawn — a scan that cannot prove "undispatched" must not authorise spend |
| an orchestrator died | surfaced once, never respawned; recovery is a human's `respawn` comment |
| the cap is full | `deferred — at cap N`, every cycle, until a slot frees |
