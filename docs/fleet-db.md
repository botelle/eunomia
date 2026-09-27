# fleet.db

`bin/fleet-collect` derives most of `fleet.db` from four sources — three on disk
and one on the forge (CI results, plan 0045) — and
that derived slice is a read-only *view* over history that already exists —
nothing here is authoritative for any decision an automated process makes
about what already happened. The ledger is awareness, not authority; a
queryable copy of it does not change that. If `fleet-collect` is stale or was
never installed on a host, that host's slice of history is simply invisible
here — it is not deleted, disproven, or contradicted anywhere else.

**That is no longer the whole file.** `fleet.db` also holds a second tier of
tables that are not derived from anything and do not go stale: `review` /
`model_price`, written directly from Forgejo by `bin/fleet-reviews`,
`repo_model` / `repo_model_change` (plan 0048), written directly by
`bin/fleet-models`, and `fleet_setting` / `fleet_setting_change` (plan 0068),
written directly by `bin/fleet-config`. Nothing about the DERIVED tier changed
— it is still a
view over history nothing writes back to — but "nothing writes back" stopped
being true of the file as a whole the day `bin/fleet-reviews` shipped, and a
session reading the old sentence designed around a constraint that no longer
held (plan 0048, ADR-0010 §3). See "Two tiers" below for exactly which table
is which, and why it matters on a schema bump.

## Two tiers, and why the difference is load-bearing

| tier | tables | who writes it | survives a schema-version bump? |
|---|---|---|---|
| derived | `session`, `turn`, `tool_use`, `ingest`, `event`, `dispatch_phase` | `bin/fleet-collect`, from transcripts/ledger/run logs | **No** — dropped and rebuilt from source (see `SCHEMA_VERSION` below) |
| derived from a source that prunes | `ci_task`, `ci_log`, `ci_sync` | `bin/fleet-collect` (through `bin/fleet-ci`), from Forgejo's task list and cihost's `actions_log` | **Yes** — deliberately absent from the drop tuple; see "CI results" below |
| durable | `review`, `model_price`, `repo_model`, `repo_model_change`, `fleet_setting`, `fleet_setting_change` | `bin/fleet-reviews`, `bin/fleet-models`, `bin/fleet-config` | **Yes** — `fleet-collect` never names them, so its `DROP TABLE` sweep cannot reach them |

Both sets are **named in `bin/fleet-collect`** — `DERIVED_TABLES` (the six above
plus the three `ci_*`) and `DURABLE_TABLES` (the last row) — disjoint, and a
completeness test in `tests/test_config_durability.py` fails when a table in
`sqlite_master` is in neither, so a new table is unclassified until someone
decides. The views (`dispatch`, `ci_task_dispatch`, `dispatch_session`,
`dispatch_cost`) hold no data and belong to neither set.

The derived tier can always be thrown away and rebuilt, because everything in
it is a re-read of a source that still exists on disk. The durable tier
**cannot** be rebuilt that way — a review's `Review-provenance` line or a
`repo_model` row is a decision someone made, recorded nowhere else in a form
`fleet-collect`'s three sources could re-derive it from. (`repo_model` has since
been given a second copy of its own — see below — but the second copy is the
ledger's *events* and a snapshot, not a re-read of those sources.) Dropping either durable table on a
schema bump would not "rebuild" it; it would erase it. `bin/fleet-collect`'s
drop sweep is a literal, hand-maintained tuple for exactly this reason: on a
`SCHEMA_VERSION` bump it runs `DROP TABLE IF EXISTS` over `("session", "turn",
"tool_use", "ingest", "event", "dispatch_phase")` and nothing else — `review`,
`model_price`, `repo_model`, `repo_model_change`, `fleet_setting` and
`fleet_setting_change` are absent from that tuple on purpose, and
`tests/test_repo_models.py` / `tests/test_fleet_config.py` each assert the
absence of their own tables against the source rather than trusting the
current value.

### The durable tier's second copy (plan 0063)

The two tiers differ in what losing the *file* costs, not only a schema bump.
Everything derived is re-read from a source that survives; a durable table is
the only copy of a decision unless something else holds one. Which "something
else" exists, per table:

| durable table | second copy | recovery |
|---|---|---|
| `repo_model`, `repo_model_change` | the ledger: one `repo-model-changed` event per write (SPEC.md), and the daily snapshot below | `reconstruct_repo_model` in `bin/fleet-models` replays the events into an **absent or empty** `repo_model`; otherwise restore the newest snapshot |
| `fleet_setting`, `fleet_setting_change` | the ledger: one `fleet-setting-changed` event per write (SPEC.md), and the daily snapshot below | `reconstruct_fleet_setting` in `bin/fleet-config`, replayed the same way — and, unlike `repo_model`'s, **wired into `fleet-collect`'s own sweep** (plan 0068 D4), so it runs on every 120-second tick, not only under test |
| `review`, `model_price` | the forge — the authority for both | `bin/fleet-reviews --backfill` |

**`repo_model` was the only durable state with no second copy** (`fleet.db` had
no backup at all, and `infra`'s `backup/dr-snapshot.sh` does not name it). The
history half of the file is rebuilt by `fleet-collect`; the configuration half is
derivable from nothing but the ledger events and the snapshot. Per plan 0048's
precedence chain a missing `default` row makes a dispatch *stop* rather than
guess — loud, but stopped.

**A restore, never an edit.** `reconstruct_repo_model` writes only into a
`repo_model` that is absent or holds no rows (rows are never deleted — removal is
`disabled = 1` — so an empty table has never carried a decision). If the table
survived, it wins whatever the ledger says, and any row the ledger would have
changed is *reported*: a ledger can be incomplete (a write from before the event
type existed, an events file rotated away), and replaying it over a surviving
table would silently revert configuration. It also refuses to build a table from
a ledger that failed to ingest, and creates nothing when there are no events, so
a fleet that never adopted `repo_model` still sees `NotConfigured`.

**Wired into `fleet-collect`.** After the event sync, each sweep calls
`reconstruct_repo_model(con, ledger_complete=…)` (plan 0063 D2, wired by plan
0076) and prints `describe_reconstruction(report)`: a dropped `repo_model` is
rebuilt on the next tick, and a surviving one that the ledger disagrees with is
*reported and left unchanged*. `reconstruct_fleet_setting` (plan 0068 D4) runs in
the same place, for the same reason.

**What the ledger cannot recover.** Rows written before `repo-model-changed`
existed have no event, and `fleet-models set` to a value already in force writes
nothing, so there is no way to backfill them by re-running it. Until each such row
is next changed, the snapshot is the only copy of it. `repo_model_change` is not
rebuilt from the ledger; its history stays queryable in `event`.

**The snapshot.** `bin/fleet-db-snapshot`, run daily by
`launchd/org.eunomia.fleet-db-snapshot.plist`, writes

```
~/dev/.fleet/snapshots/fleetdb-<UTC stamp>.db      # newest 7 kept
~/dev/.fleet/snapshots/fleetdb-<UTC stamp>.dump/   # the logical dump beside it
```

using SQLite's online backup API and never a file copy: the collector writes
every 120 seconds, and a copied file can open fine while missing rows. Each file
is a single, integrity-checked database (no `-wal` sidecar) built under a
`.partial` name and renamed only when whole, so a name matching `fleetdb-*.db` is
always complete. **This path is what the offline tier should collect.** Staging it
in `operator/infra`'s `backup/dr-snapshot.sh` is a separate change in that
repository and is not made here. (`bin/fleet-snapshot`, which uses `VACUUM INTO`
and writes `fleet-*.db` to the same directory, is unrelated and does not prune
these.)

**The logical dump** (plan 0063 D4a). Each `.dump/` directory holds one
`<table>.jsonl` per durable table: one row per line, keys sorted, rows in
primary-key order, so a durable row changed between two days is a one-line diff
between two dumps (`diff -r` the two directories). It is small, readable when you
want to know what the configuration used to say, and independent of SQLite. It is
**not the restore path**: it holds the durable tier only, so a database built
from it alone has configuration and no history, and looks healthy while missing
everything `fleet-collect` would have rebuilt from transcripts a lost machine no
longer has. The physical snapshot restores; the dump is for reading, diffing and
porting. Rotation removes a dump with its snapshot.

**WAL.** `fleet.db` runs in `journal_mode=WAL` so a writer no longer blocks
readers (`collect.log` had recorded `database is locked`, and the lynceus API
reads the same file the collector writes). It is set by each writer *at open*,
not by a one-off pragma: journal mode is persistent, so a hand-run pragma looks
fixed until a copy restored in `delete` mode arrives with nothing to set it
again. `bin/fleet-models`, `bin/fleet-config`, `bin/fleet-collect` and
`bin/fleet-reviews` each do this at open.

## `dispatch_session` and `dispatch_cost`: what a run and its reviews cost (plan 0076)

Two more views, **DERIVED** like `dispatch`, computed at query time from rows
that already exist — so they link every run already on disk, with no re-ingest.

**`dispatch_session`** — one row (`lease`, `seq`, `session_uuid`) per implementer
run that links to exactly one session. A *pair* is an `implementer-start` and the
`implementer-exit` after it in `dispatch_phase`; the initial run and each `-r<n>`
fix round is its own pair. The pair's `cwd=` becomes a project slug the way the
CLI derives it — **every character outside `[A-Za-z0-9]` becomes `-`**, so `/`,
`.` **and `_`** fold (`…/work/operator_s_eunomia/…` is
`…-work-operator-s-eunomia-…`) — and it matches the **top-level** `session` row
(`parent_uuid IS NULL`) of that project whose `first_ts` lies in the pair's window
widened by one second each side. **One match links; none or several yield no
row.** A join is unique or it is absent: a time-window guess is a derived number
standing in for a measured one, and it is wrong silently when reviews run in
parallel. `fleet-collect` prints `dispatch_session: N of M implementer pair(s)
linked, K unlinked` on every sweep. The slug function, `fleet_slug`, is defined in
`bin/fleet-collect` and **must be registered on a connection** (`register_functions`)
before either view is queried — `sqlite3` on the command line cannot see it.

**`dispatch_cost`** — per linked implementer session (`kind='implementer'`) and per
linked review round (`kind='review'`, joined through `review.session_uuid`):
`processed` (`in_tok + cache_read_tok + cache_write_tok`), `output` (`out_tok`),
`turns`, and `minutes` (`julianday(last_ts) - julianday(first_ts)` × 1440, one
decimal). An unlinked run or round has **no row**. `review.session_uuid` is filled
from the `session=` field of a `Review-provenance` line (talos plan 0007); the
reviews before that field existed stay NULL and are never backfilled by window.
The view reads `review`, which `bin/fleet-reviews` creates, so it can only be
queried once that table exists. Tokens are the unit; **no dollars** — every
session here runs on a subscription, and an API-equivalent figure would read as
a bill. `fleet-plan ledger` prints it; see `docs/plan-ledger.md`.

## The four sources, and what each can and cannot answer

| source | table(s) | can answer | cannot answer |
|---|---|---|---|
| `~/.claude/projects/**/*.jsonl` (Claude Code transcripts) | `session`, `turn`, `tool_use` | what a session/subagent did, when, at what token cost | which plan or lease it was working, or whether its PR merged |
| `~/dev/.fleet/events*.jsonl` (the ledger, SPEC.md "Event record") | `event` | that a lease/PR/plan transition happened, at what timestamp, with what `detail` | *why* an implementer session behaved as it did, or the phases inside a run — the ledger is nearly all point-in-time transitions, not a stream |
| `~/dev/.fleet/work/logs/<lease>.log` (orchestrator `RunLog`) | `dispatch_phase` | the phase-by-phase story of one dispatch: routed, ran, verified, reviewed, stewarded, and the exact fact set `log.note()` recorded at each step | the ledger's cross-repo, cross-lease view, or anything from before D2 shipped (older runs have no run log to ingest) |
| Forgejo's `/actions/tasks` and cihost's `actions_log` (plan 0045) | `ci_task`, `ci_log`, `ci_sync`, view `ci_task_dispatch` | what every CI task was and how it ended; **why** a task that did not succeed failed (its redacted log); which dispatch a task belongs to | why a **green** task did anything (no body is kept for one); a failed task whose log is over the cap, pruned, or unreadable (each says so on its row); tasks of a repo the collector's token cannot see |

Questions that need the **forge**, not this DB: current PR review state,
whether a PR is still open, comment threads, and whether a CI task is
running *right now* — `fleet.db` freezes
whatever `pr`/`sha`/`detail` a ledger event carried at emit time; it does not
poll Forgejo. `dispatch.pr` and `dispatch.outcome` are exactly as fresh as
the last `fleet-collect` sweep and the last thing the orchestrator logged,
never fresher.

## Schema

```
session(id, session_uuid, project, parent_uuid, kind, first_ts, last_ts,
        turns, tool_calls, in_tok, out_tok, cache_read_tok, cache_write_tok,
        models, title, first_prompt)
turn(session_id, seq, message_id, ts, model, in_tok, out_tok,
     cache_read_tok, cache_write_tok)
tool_use(session_id, ts, name, target)

event(ts, actor, type, repo, pr, sha, lease, detail_json, source, line)
dispatch_phase(lease, seq, ts, phase, pr,
                rc, timed_out, seconds, peak_rss_mb, orch_rss_mb, rss_samples,
                detail_json, source, line)

ingest(path, size, mtime, ingested_at, error)   -- shared by the three file sources

ci_task(repo, id, name, run_number, head_branch, head_sha, event, display_title,
        status, workflow_id, url, created_at, updated_at, run_started_at,
        log_state, log_detail, log_zbytes, log_checked_at, fetched_at)
ci_log(repo, task_id, body_z, raw_bytes, cap_bytes, redactions, captured_at)
ci_sync(repo, attempted_at, succeeded_at, error)
```

`ingest.path` is a namespaced key so the three file sources can never collide
(the CI source keeps its own keys — `(repo, id)` — and never touches `ingest`):
a transcript's key is its path relative to `PROJECTS`; an event file's key is
`events/<filename>` (`events.jsonl` or an `events-<stamp>.jsonl` archive); a
run log's key is `logs/<lease-id>.log`. All three are re-scanned only when
`(size, mtime)` changes since the last successful ingest, and a changed file
has ALL of its previous rows for that key deleted and replaced — never
appended to — which is what makes replaying an already-ingested archive, or
re-running against a file that grew, produce identical row counts.

### `event`

One row per ledger line (SPEC.md "Event record"). `lease` mirrors the
record's top-level field, which SPEC requires **null** on `plan-*` events —
their lease id instead rides inside `detail_json.lease`, so a query that
needs it does `json_extract(detail_json, '$.lease')` (the `dispatch` view
below does this for you).

### `dispatch_phase`

One row per `RunLog.note()` call, parsed from `<ts> <phase> k1=v1 k2=v2 ...`
lines. `seq` is 1-based and monotonic per lease, independent of any gaps a
skipped or malformed line might leave in the source file's line numbers
(`line`, alongside `source`, is the file-position identity used for
idempotent re-ingest; `seq` is the ordering identity a query wants). Every
fact the phase carried lands in `detail_json`; `rc`, `timed_out`, `seconds`,
`peak_rss_mb`, `orch_rss_mb`, and `rss_samples` are additionally lifted into
their own columns, but **only** on the `implementer-exit` row that carries
them (D4) — every other phase leaves them `NULL`.

## `dispatch`: a view, not a table

Every column `dispatch` reports is fully derivable from `event` and
`dispatch_phase`. A table would be a second copy of the same facts that a
collector bug could let drift out of sync with its own source rows; a view
costs nothing to keep correct because it has no state of its own to migrate.
It is dropped and recreated on every run (not gated behind `SCHEMA_VERSION`,
unlike the tables) — a view carries no data, so changing its definition
never needs a rebuild-from-source, and gating it behind the schema version
would mean a query fix sits inert until an unrelated version bump ships it.

One row per **lease** — not per plan, and not per repo. A plan retried after
a stranded run (`--001` died, `--002` resumed) produces two leases and two
`dispatch` rows, each with its own phase history; nothing here merges them,
because the orchestrator itself never merges them (a `--002` resume attaches
to a fresh branch history and gets its own `RunLog`).

Columns: `lease`, `repo`, `plan_id`, `session`, `started_ts`, `log_path` (all
from the `start` phase — absent if the run's log never reached that line, in
which case they are `NULL`, not blank strings, so a consumer can tell
"unknown" from "empty"), `current_phase` / `current_phase_ts` (the
highest-`seq` phase row for the lease), `pr` (the most recent phase row that
carried one — `verified`, `review-approved`, `steward-start`/`-end` all do),
`outcome`, `terminal`, `failure_reason` / `failure_log` / `failure_transcript`
(from a matching `plan-failed` ledger event, D7), and `rc` / `timed_out` /
`seconds` / `peak_rss_mb` / `orch_rss_mb` / `rss_samples` (from the lease's
`implementer-exit` phase, D4).

A lease can appear in `dispatch` from the ledger alone, with every
phase-sourced column `NULL`: if `RunLog` itself never opened (a full disk —
see `bin/orchestrator`'s own docstring on `RunLog`), the run still emits
`plan-failed`, and that lease must still be a row here rather than silently
missing. This is the same "absent must not look like zero" discipline
`operator/lynceus` documents for its own sources: a lease with no phase
history reads as "no phase data available", never as "zero phases happened".

### The terminal-phase rule

A lease is **terminal** iff:

- the ledger carries a `plan-failed` or `plan-done` event naming it (`outcome`
  is `'failed'` or `'done'` respectively), **or**
- its phase stream reached `steward-end` (`outcome` is whatever `state` that
  phase recorded — typically `merged` or `closed`).

**`review-approved` is deliberately NOT terminal.** The gap between
`review-approved` and `steward-end` is exactly the window where an approved
PR is sitting open, waiting on a human to merge it — `bin/orchestrator`
stewards it the whole time rather than releasing the lease. A dispatch whose
`current_phase` is `review-approved` and has sat there for an hour is not
stuck; it is doing precisely what it should. Folding that gap into
"terminal" (or, symmetrically, alerting on it as "stalled") would manufacture
exactly the false signal Boundary 3 warns about: nothing here gates on
`dispatch`, but a human reading a dashboard built on it would.

## The stuck-dispatch query (Definition of Done)

Dispatches whose latest phase is more than N minutes old and are not
terminal:

```sql
SELECT lease, repo, plan_id, current_phase, current_phase_ts
FROM dispatch
WHERE NOT terminal
  AND current_phase_ts < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 minutes');
```

## The "why did this run die" query (D7)

```sql
SELECT lease, repo, plan_id, failure_reason, failure_log, failure_transcript,
       rc, timed_out, seconds
FROM dispatch
WHERE repo = 'operator/ares' AND plan_id = '0001-ranged-standoff-core'
  AND outcome = 'failed';
```

Answers "why did this die, and where is its log/transcript" without opening
either file — every field it returns was already written by the run itself
(`plan-failed`'s `detail.reason`/`detail.log`/`detail.transcript`, and
`implementer-exit`'s `rc`/`timed_out`/`seconds`); the query only spares a
human from re-deriving them by hand.

## CI results (plan 0045)

"Why is this build red" is a query, the way "what is this dispatch doing" is.

```
fleet-ci failures lynceus            # the failing tasks, newest first, with body state
fleet-ci show lynceus 2825           # one task's stored body (redacted)
fleet-ci missing lynceus             # tasks with no body, and why
```

These read `fleet.db` read-only; there is no `ingest` command, because
`bin/fleet-ci` is a **source inside `fleet-collect`, not a second writer**. The
collector loads it every sweep; the tables above are created by
`fleet-collect`'s own `SCHEMA`, so there is one owner of the schema and of
`PRAGMA user_version`. A standalone writer that stamped its own version would stop
every existing source from ingesting on every 120-second tick, with one line in
`collect.log` to show for it.

### What is stored, and why not everything

- **`ci_task`** — every task, from `/api/v1/repos/{owner}/{repo}/actions/tasks`:
  the columns the API returns, plus `repo` (which is in the request path, not the
  response). `name` matters: this repo's own CI is a two-label matrix, `tests (cihost)` and
  `tests (cihost-linux)`, and without it two tasks per push are indistinguishable.
  There is **no `conclusion`** and no completion timestamp; `status` carries the
  result and `updated_at` is the nearest thing to when.
- **`ci_log`** — a body, and only for a task whose `status` is not a success
  (`failure`, `cancelled`, or any other terminal status). zlib over the *redacted*
  text; the original is never stored. On the day this was designed the host held
  2,679 logs, 27 MB compressed, against a 54 MB `fleet.db`: storing every body
  would roughly double it to carry output for runs nobody reads.
- **`ci_sync`** — per repo, when it was last tried, when it last succeeded, and
  the error if the last try failed. This is what separates "no failing tasks" from
  "the collector could not ask".

**Measured 2026-09-20 (36 repos visible to the collector's token, 3,132 tasks):**
2,729 `success`, 224 `skipped`, 148 `failure`, 31 `cancelled`. So **179 bodies, 5.7%**
of tasks, `1.5 MB` stored (`10.8 MB` before compression; the largest, `operator/ares`
task 467, is 1.5 MB decompressed / 155 KB compressed, and ingests under the cap). The
CI tables came to 3.6 MB in a scratch database, against the 54 MB `fleet.db`. A
first full backfill took 12 s; a steady sweep with nothing new takes ~2 s and makes
no ssh connection at all. If the failure fraction ever stops being a small fraction,
the cap and the retention are the two things to revisit.

### Configuration

Everything has a default that works on opshost, so the launchd unit needs no edit.

| variable | default | what it does |
|---|---|---|
| `FLEET_CI_SOURCE` | on for `~/dev/.fleet`, off for any other tree | `0` turns the source off (a host that cannot reach cihost); `1` turns it on for a relocated tree. Off by default off the real tree because this source reads the real forge and cihost, which a relocated tree cannot redirect |
| `FLEET_CI_REPOS` | every repo with Actions the token can list | comma-separated `owner/name` list instead |
| `FLEET_CI_LOG_HOST` | `cihost` | ssh host holding `actions_log`; `local` reads this machine's own tree and dials nothing |
| `FLEET_CI_LOG_ROOT` | `/opt/homebrew/var/forgejo/data/actions_log` | the tree on that host |
| `FLEET_CI_LOG_CAP` | `2097152` | decompressed bytes above which a log is recorded `over-cap` |
| `FLEET_CI_BUDGET` | `90` | seconds a sweep may spend on this source, under the 120 s cycle |
| `FLEET_CI_TOKEN_CMD` | `~/bin/fetch-forgejo-token.sh` | command printing the Forgejo token |
| `FLEET_FORGEJO_URL` | `http://forge.example:3000` | the forge |

### `log_state`: absent must not look like zero

A failed task with no `ci_log` row is never ambiguous, because its `ci_task` row
says which of these it is (and `log_detail` says more):

| `log_state` | meaning |
|---|---|
| `not-failed` | `success` or `skipped`: no body by design |
| `in-progress` | not finished, so its log is not final; re-read until it ends |
| `pending` | failed, body not fetched yet — or the last attempt failed (`log_detail` says why) |
| `stored` | a `ci_log` row exists |
| `over-cap` | read, but decompresses past the cap (2 MiB); **not stored, not truncated** — its compressed size is in `log_zbytes` and `log_detail` |
| `absent` | no file at the path `log_detail` names |
| `unreadable` | the file exists and could not be fully read (bad zstd, a stat error, no zstd); nothing partial is kept |

A truncated capture is never stored: a log that could not be read whole is
recorded as such, and `ci_log.cap_bytes` records the cap in force so a later
change of cap is visible. An `absent` or `unreadable` log is retried while the task is
under a day old (Forgejo finishes a task and archives its log a moment apart) and is
final after that, so a log pruned upstream is not asked for every two minutes forever.

On a host whose pin has not advanced, `fleet.db` has **no `ci_*` tables at all**, which
is normal. `fleet-ci` says so and exits 2 — *"has no ci_task table … This is NOT 'no
failing builds'"* — rather than answering an empty list.

### Where the logs come from, and the limits of that

`/opt/homebrew/var/forgejo/data/actions_log/<owner>/<repo>/<id % 256 as %02x>/<id>.log.zst`
on cihost, where Forgejo runs. The ledger is on opshost, so:

- **One ssh per sweep, no master.** All the bodies a sweep wants travel over one
  connection (`ControlMaster=no`, `ControlPath=none`, `BatchMode=yes`), and a sweep
  that owes none dials nothing. macOS's TIME_WAIT is 30 s against a 120 s cycle, so
  one dial per cycle is 720 a day and 0.25 sockets in TIME_WAIT on average; the
  2026-08 incident was ~90,000. Needing more than one connection per cycle is a
  different change, not a stretch of this one. The paths go on **stdin**, never in the
  command, so nothing derived from a task reaches a shell.
- **Decompression is on cihost, by absolute path** (`/opt/homebrew/bin/zstd`, run by
  `/usr/bin/python3`). launchd and ssh both hand a process a PATH with no brew, so
  `command -v zstd` reports an installed tool missing; and the pinned interpreter has
  no `zstandard` module (it reaches the stdlib in 3.14). The remote reads at most
  cap+1 bytes of output, so a log that expands enormously is refused there, not after
  it fills a disk.
- **It reads files and the normal API only.** It does not read Forgejo's database
  (coupling to an internal schema that upgrades without warning) and does not use the
  admin token, which is retired from fleet processes: the token is the implbot
  helper's, via `FLEET_CI_TOKEN_CMD` — *not* `FLEET_TOKEN_CMD`, which on this fleet can
  name the admin helper — and a command naming an admin helper is refused.
- **Retention is not established.** As of 2026-09-20 cihost holds 3,135 log files and
  the oldest is task 1, written 2026-06-02 — 110 days, nothing pruned yet. No
  retention key is set in cihost's Forgejo config, so Forgejo's default applies, and
  what that default is was not confirmed here. If Forgejo does prune, this data has
  the same floor problem the transcript data has: history before the floor is
  unrecoverable from this source, which is why `ci_*` are not dropped on a schema bump.
- **Coverage is what the collector's token can see.** cihost holds logs for repos the
  implbot account cannot list (a few `bakeoff-*` repos among them); their tasks are
  not in `ci_task`, and the missing repos do not appear as failures.

### `ci_task_dispatch`: the join to a dispatch

The `dispatch` view has no sha column, and `event.sha` is only ever filled by hand.
The orchestrator writes the **8-character** head sha into the run-log facts of
`ci-waiting`, `ci-resolved`, `ci-none`, `ci-timeout`, `ci-unreadable`,
`review-dispatch` and `review-round`, so that is the key that exists:

```sql
substr(ci_task.head_sha, 1, 8) = json_extract(dispatch_phase.detail_json, '$.sha')
```

then lease → `dispatch`, **qualified by repo** (two repos can share a prefix). It is
a view, dropped and recreated each run, like `dispatch`. One row per
*(task, lease)*: two leases that both looked at one sha are both listed. A task with
no matching dispatch is still a row, with `lease` NULL.

```sql
SELECT task_id, name, status, lease, plan_id, dispatch_outcome
FROM ci_task_dispatch WHERE repo = 'operator/lynceus' AND status = 'failure';
```

It cannot say *which* CI run a dispatch's verdict came from when the branch was
pushed more than once between two `ci-*` facts — only which tasks ran at a sha the
dispatch named.

### Redaction

A build log is the fleet artifact most likely to contain a credential, and this file is
read by atlas and by lynceus, which is published. Text is redacted **before** it is
stored, and the stored text carries the **rule name, never the matched span**
(`[redacted:aws-key-id]`); `ci_log.redactions` holds counts by rule name and nothing
else. The contract is a table of credential shapes in plan 0045, each proven by its own
test in `tests/test_fleet_ci.py`. None of the fleet's three older definitions
(`_SECRETISH` in `bin/fleet-collect`, `config/secret-patterns.conf`,
`bin/fleet-leak-watch`'s `FINDINGS`) is a superset of the others — the conf omits 40-hex
deliberately, `_SECRETISH` omits `AKIA`, the PEM header and `hvs.`, and its keyword
branch fires only when a name *ends in* a keyword, so `AWS_SECRET_ACCESS_KEY=` passes
all of them — so `bin/fleet-ci` has its own, and **two of its choices are deliberate and
cost something**:

- **Every standalone run of 40+ hex digits is redacted, git shas included.** A Forgejo
  PAT is 40 hex and looks exactly like a sha. The task's `head_sha` is a column, outside
  the redacted text, so the cost is shas *inside a log line*.
- **An assignment is redacted up to whitespace, not end of line**, so
  `TOKEN=x npm test` loses `TOKEN=x`, not the command; the price is that
  `password: two words` keeps its second word.

Storing failures only shrinks this surface; it does not remove it. The free-text
columns of `ci_task` (`name`, `head_branch`, `display_title`) get the shape rules only,
not the keyword or hex rules, which would mistake a commit title for a credential.

**Nothing automated may gate on these tables** (SPEC Principle 3). They are as fresh as
the last sweep — `fleet-ci` prints `synced <time>` or a `WARNING` naming the failed
sync above every answer — and a collector that is behind must not become an input to
control flow.

## What this collector must never become

Nothing in `bin/fleet-watch` or `bin/orchestrator` reads `fleet-collect`'s
**derived** tables to decide anything, and this plan does not change that.
The moment a dispatcher consults `session`, `event`, `dispatch_phase`, `ci_task` or the
rest of the derived tier, a collector that is behind — or a host where it was
never installed — becomes a silent input to control flow, which is exactly
the failure mode Principle 3 (the log is awareness, not authority) exists to
prevent. `fleet-collect` also never creates, moves, truncates, or rotates
anything under `~/dev/.fleet/` other than `fleet.db` itself: it reads the
live tree other processes depend on, and never writes to it.

**`bin/orchestrator` reading `repo_model` is not that failure mode.**
Plan 0048 has it read that one durable table to route the implementer
(`resolved_implementer_env`, replacing `DEFAULT_RUNNER` as the first choice).
The risk this section guards against is specifically *staleness* — a
dispatcher trusting a fact `fleet-collect` has not gotten around to refreshing
yet — and `repo_model` cannot be stale in that sense: `bin/fleet-models`
writes it directly, `fleet-collect` never touches it, and uninstalling
`fleet-collect` entirely would not change what it resolves to. Consulting a
directly-written, always-current config table is an ordinary read of
configuration; consulting the derived history tier to decide anything remains
the thing this collector must never become.

**`bin/fleet-watch` reading `fleet_setting` (plan 0068) is the same case,
read-only and direct.** `cycle()` resolves `dispatch_cap` every poll via
`fleetconfig.resolve_setting`, opened `mode=ro` against `fleet.db` itself —
never through `fleet-collect`'s derived tables, and never cached beyond one
cycle. A `fleet.db` that is absent, locked, or lacks the table falls back to
`FLEET_WATCH_CAP`/the built-in default rather than stopping dispatch, which is
the one deliberate difference from `repo_model`'s missing-`default`-row
*stop*: a per-repo runner choice with no answer is a routing decision with
nothing safe to guess, where the dispatch cap is a throttle whose failure mode
is the old value.
