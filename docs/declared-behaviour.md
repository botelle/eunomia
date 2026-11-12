# eunomia — declared behaviour

This document restates, for a reader outside this project, the externally
observable behaviour of every program eunomia ships: what each one exits
with, what it writes, where, and in what shape. It is not the specification.
`SPEC.md` is authoritative; where the two disagree, this document is the one
that is wrong, and it gets corrected — never the reverse.

Every name eunomia's own contract-generation tool (`fleet-bundle`) reports —
every program it can describe and every one it can only report as a gap — is
listed below exactly once, in one of two states:

- **declared** — every exit code and what it means, and everything the
  program writes: files, streams, and any object it creates on the forge
  (the git-hosting service this fleet uses), each with the shape of what
  lands there.
- **undeclared — `<class>`** — the program is out of scope for this
  document, with the reason named. A name never simply disappears; an
  absence with no class attached cannot be told apart from an oversight.

Some entries below say `UNDECIDED:` — an exit code or an output shape this
document's own writing exposed as never having been decided by anyone. That
is recorded as a finding, not silently resolved.

## Programs

### bin/fleet-arena

declared

**Purpose.** Runs one cell of a model comparison against a single arena
repository (`cell`), lists cells (`list`), or shows one cell's full record
(`show`). A cell is a real dispatch of the orchestrator with the
`implementer` position pinned to a chosen runner; `--base` is an assertion
checked against the arena's `origin/main`, not a selector.

**Exit codes.**
- `0`: success. `cell` prints the new cell id on stdout; `list` prints one
  line per cell (or "no arena_cell rows"); `show` prints the full record.
- `1`: refused or failed — an unknown or unwired position or runner, a
  `--base` that is not a full SHA or is not the arena's `origin/main`, an
  unreadable plan at the base, a dispatch failure (lease mint or
  orchestrator spawn), or `show` given an unknown cell id. Refusals happen
  before any row is written.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- The shared database: one `arena_cell` row and seven
  `arena_cell_assignment` rows per cell, written in one short transaction
  before dispatch; a dispatch failure afterward updates the row to
  `outcome='failed'` and never deletes it.
- One lease record per cell, naming the branch `cell/<plan-slug>/<cell_id>`,
  through `fleet-claim`; and the orchestrator process it spawns.
- `list` and `show` write nothing. Diagnostics go to stderr.
- No event is emitted. It never merges a branch and never changes a plan's
  status.

**Shape.** `list` lines: cell id, timestamp, repo, round, outcome, PR
number, branch. `show` prints `key: value` lines for the record followed by
one line per assignment row (position, ordinal, runner, source, effective).

### bin/fleet-attest

declared

**Purpose.** Tests every change record against checks A1-A8 of
`docs/change-management.md` and reports each exception. Read-only: it holds
neither the recorder's key nor its token, pushes to no remote, and calls no
model. Subcommands: `check`, `report --month YYYY-MM|previous [--rerun]`.

**Exit codes.**
- `0`: the pass completed, with or without exceptions.
- `2`: a refusal or an unreadable input: no start SHA for any run with no
  prior report, a month that already has a report (without `--rerun`), or
  malformed flags.

**Writes.**
- `reports/YYYY-MM[.rN].md` and `.json` and `protection/<owner>/<repo>.json`
  under `FLEET_ATTEST_SPOOL`; `fleet-record publish` is their only reader.
- A page through `fleet-watch notify`, once per `check` run, only for
  exceptions not seen in an earlier run.
- Stdout/stderr: one line per repository and per exception.

### bin/fleet-bundle

declared

**Purpose.** Read-only tool that introspects a repository and emits this
document's own carrier: its "generated contract" (an API document if one is
present, CLI argument surfaces, declared closed vocabularies, configuration
key names, and CI/test harness structure). Takes no token, writes to no
repository, opens no network connection.

**Exit codes.**
- `0`: success — contract document produced (to stdout or `--out` file).
- `2`: the given repository path is not a directory.
- argparse's own exit 2 on malformed CLI arguments.
- Internal errors while inspecting one file (a file that doesn't parse,
  declares no argument surface, times out, exits at import) are not process
  exit codes — they become `gaps` entries in the output and the run still
  exits 0.
- An internal `--introspect PATH` mode, used only when this program
  re-invokes itself as a child process, always returns 0 and writes a
  parser-structure payload or an error payload after a sentinel marker.

**Writes.**
- Stdout (default) or a file given via `--out PATH`: JSON (default) or a
  plain-text rendering of the same document. Nothing else is written —
  no database, no forge object, no event.
- To read a CLI's argument surface it imports each candidate file in a
  **child process**, given a restricted environment (a fixed allowlist of
  general-purpose variables plus two internal markers), so a default value
  read from the environment is never evaluated with a real credential
  present and cannot leak its live value into the output.

**Shape.** Top-level JSON keys: `bundle_version`, `repo`; `openapi` (list of
`{path, routes}` for any API document found on disk — never synthesised);
`clis` (list of `{path, prog, arguments, options, subcommands}`, argument
fields limited to a fixed set, with any default whose value could not be
proven literal in source replaced by the token `"<computed>"` rather than
its evaluated value); `vocabularies` (list of `{name, source, values}` —
module-level constants holding literal string collections, which is how
this repository declares closed sets such as its event type names);
`config_keys` (list of `{name, read_by}` — environment variable **names**
only, never values); `harness` (`{workflows, test_files, executables}`);
`gaps` (list of `{target, reason}` — everything the tool could not
represent, surfaced rather than omitted). Every string in the finished
document has the scanned repository's own root path and the operator's
home directory rewritten to placeholders, as a backstop.

### bin/fleet-candidate

declared

**Purpose.** Builds a scratch merge of a pull request's base and head in a
throwaway git worktree, runs a configured test command against it, and
optionally posts the result as a commit status on the forge. Implements a
"candidate is green" admission check; never pushes to a real branch.

**Exit codes.**
- `0`: build and test succeeded, and the optional status post (if
  requested) also succeeded.
- `1`: build or test failed for a substantive reason (merge conflict, test
  command exited non-zero), including the case where that failure was
  itself successfully posted as a forge status.
- `2`: everything short of producing a verdict — a malformed repository
  argument; a token-helper failure; the pull request unreadable or not
  open; a missing head or base commit; the test command environment
  variable unset (refuses to guess a command); a merge failure with no
  actual conflicting paths (a broken build environment, not a real
  conflict); an internal git-wrapper assertion (including a hard refusal
  if any git call it issues ever contains `push`); a rejected status post;
  a git or test-command timeout.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- No files persist: a scratch git worktree is created under the fleet's
  work area and always removed in a `finally` block, named uniquely per run
  so concurrent builds of the same head cannot collide.
- Stdout: a dry-run line naming the verdict it would post, or the same
  after actually posting.
- Stderr: warnings on any failure, and — with `--verbose` or on a failed
  test — the tail of the test command's captured output.
- Forge object created/modified: with `--post`, one **commit status** on
  the pull request's head commit, `context="candidate"`, `state` one of
  `success`/`failure`, and a description naming the merge base and outcome
  (or a truncated list of conflicting paths). No pull request body,
  comment, or branch is ever touched, and the tool never issues a `git
  push`.
- No event emitted, no database row written.

### bin/fleet-ci

declared

**Purpose.** Read-only CLI over CI data a separate collector has already
ingested into the fleet's shared database (`failures`, `show`, `missing`
subcommands). This file also defines the ingest routine the collector calls
into, but exposes no `ingest` subcommand of its own.

**Exit codes.**
- `0`: the subcommand produced its normal report, including "zero failing
  tasks".
- `1`: a data-availability condition specific to the subcommand — no CI
  tasks recorded at all for the requested repository (distinguished from
  "zero failures"); `show` given a task id with no match; a matched task
  with no stored log body.
- `2`: the database is unusable — no database file at the configured path,
  the database predates the CI tables, or a database read raised.
- argparse's own exit 2 on malformed CLI arguments.
- The internal collection routine does not itself call an exit; it returns
  a report and never raises for an environmental failure (missing token,
  unreachable forge, unreachable log host) — those become entries in its
  returned report, not a process exit code. Exit-code behaviour for a
  collection run is owned by `bin/fleet-collect`, not this file.

**Writes.**
- The CLI subcommands open the database read-only and write nothing to
  disk; output is to stdout (data) and stderr (diagnostics).
- The internal collection routine (invoked by the collector, not directly)
  writes into the shared database: a table of CI task metadata, one row
  per task; a table of stored log bodies — a compressed, redacted text
  blob per task that has one, with a record of which redaction rule fired
  and how many times, never the matched text; a table recording each
  repository's last sync attempt, its last success, and its last error.
  Table and schema ownership belongs to `bin/fleet-collect`; this file's
  routine only inserts and updates rows within that schema.
- Subprocesses (from the collection routine only): one call to a
  configured token-helper command to obtain a forge API token (refused
  outright if the configured command name suggests elevated/admin scope);
  one outbound call per sweep to the host holding CI logs, either local or
  over a single non-multiplexed remote shell session running a small
  embedded script that in turn decompresses each requested log through a
  standard compression tool.
- HTTP calls: GET only, against the forge's API (repository search, and a
  CI task list per repository). No forge object is ever created or
  modified. No event is emitted.
- `show` writes the decompressed, redacted log body of one task to stdout;
  every other subcommand's data output also goes to stdout, its
  diagnostics to stderr.

**Shape.** Log bodies pass through an ordered table of redaction rules
(private keys, basic-auth URLs, bearer/authorization headers, several
vendor token shapes, JWTs, cloud access-key ids, a vault token shape, a
generic `key=value` assignment rule, and a catch-all long-hex-run rule)
before storage; each matched span is replaced by a `[redacted:<rule>]`
marker, never the original text. `failures` stdout is a summary line and a
column table; `missing` stdout is counts per log-retrieval state followed
by one line per matching task.

`UNDECIDED:` this file's own documentation states there is deliberately no
`ingest` subcommand, yet the same file defines and exports the collection
routine that is the actual write path, invoked only by `bin/fleet-collect`
— a reader of the CLI surface alone would not discover that this file is a
load-bearing writer through a non-CLI entry point.

### bin/fleet-ci-stress

declared

**Purpose.** Runs plan 0113's concurrency measurement outside any model
session. `round` pushes one scratch commit to three `ci-stress` branches,
waits for their CI, and records which jobs overlapped; `rebuild` writes the
same record for a round that already ran, from the forge's task list alone;
`gate` reads the records and states whether the measurement's Gate passes.
A person starts it; nothing schedules it.

**Exit codes.**
- `round`: `0` recorded, the round counted, every job green. `1` recorded,
  but a job is not green or no two full-suite jobs on different branches
  overlapped (the round does not count and is rerun). `2` refused or failed
  before a record could be written: the queue is not empty (or could not be
  read), a branch already exists, a record already exists without
  `--replace`, the push failed, the token helper failed or names an admin
  helper. `3` the wall-clock bound expired; no record. `130`, `143`, `129`:
  interrupted by SIGINT, SIGTERM or SIGHUP; no record.
- `rebuild`: `0` record written. `2` refused: a named run is absent from the
  task list, the runs are not the three branches of that round, or they
  disagree on the commit.
- `gate`: `0` the Gate passes. `1` it does not. `2` a record is unreadable.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- One JSON record per round, `round-<kind>-<n>.json`, under the evidence
  directory (default `docs/ci-evidence/0113/`): kind, round, the scratch
  commit and its tree, the commit it was built on, every task's id, run
  number, workflow, name, branch, result and interval, the overlapping pair
  or null, and whether the round counts. An existing record is not
  overwritten without `--replace`. `gate` writes nothing.
- On the forge (`round` only): three branches `ci-stress/0113-r<n>-{a,b,c}`
  created by one atomic push to the checkout's remote, and deleted again on
  every exit path that can be caught. A branch that cannot be deleted is
  named on stderr. No pull request, comment, status, or setting is touched.
- Reads the forge's branch, commit, commit-status, task and run lists with
  the implbot token from a helper command. The token stays in memory and in
  an Authorization header; it is never in argv, a file, or any output.
- Stdout: the record's path (`round`, `rebuild`) or the verdict lines
  (`gate`). Stderr: refusals and progress. No event emitted; no database
  write.

### bin/fleet-claim

declared

**Purpose.** Grants, activates, pulls from a pool, or forcibly takes over a
lease — this fleet's resource-locking record (see the Lease record shape
below). Four mutually exclusive modes: `--assign`, `--activate`, `--next`,
`--takeover`.

**Exit codes.**
- `0`: success in every mode, including the idempotent case where
  `--assign` finds a matching lease this session already holds and reports
  its existing id rather than erroring.
- `1`, grouped by cause:
  - `--assign`: the resource argument isn't valid JSON; its `type` isn't
    one of the known resource types; `globs` present but not a list; a
    `role` present that isn't `integrator`; `integrator` used on a
    resource type that doesn't support it, or with no repository named, or
    with no explicit holder (a pool row must never silently become an
    integrator lease); a second live integrator-role lease already exists
    for that repository (one per repository); a live lease already exists
    on the exact same resource held by a different holder; the lease store
    has unreadable files (fails closed rather than risk missing a real
    conflict); a lease-id collision on allocation.
  - `--activate`: the lease id isn't found; the lease is assigned to a
    different holder than the caller (activation verifies, it does not
    acquire); the lease is already released.
  - `--next`: the resource type isn't known; no unclaimed pool row of that
    type exists; every candidate pool row was claimed by a competing
    puller before this one could take it.
  - `--takeover`: the lease id isn't found; the lease is an unclaimed pool
    row (must be claimed with `--next` instead, to preserve
    oldest-first fairness); the lease, re-checked under lock, turns out
    not to be orphaned after all (its holder is still live).
  - No session identity is set for the caller, or it isn't validly shaped;
    a lease-id argument isn't lease-id shaped.
- argparse's own exit 2 on malformed CLI arguments (the four modes form a
  required, mutually exclusive group).

**Writes.**
- One lease record per grant (path shape: a directory of one JSON file per
  lease, named by resource type and a slug), written under the lease's own
  lock. Fields as in the Lease record shape below.
- An orphan marker file for the lease, removed (not created) by
  `--activate`/`--takeover` on this session's own lease, clearing a stale
  flag an external sweep may have left.
- A per-session liveness file, touched (created/updated with no content)
  by `--activate`, `--takeover`, and a fresh pool claim via `--next`.
- Lock files used purely for serialisation: one per lease, one per
  repository's integrator role, one per resource, one per pool type.
- Events emitted (always lease-scoped): `lease-assigned` on `--assign`;
  `lease-activated` on `--activate` and again on `--next` (the pool-claim
  case carries a `detail` noting it arrived via the pool, the
  direct-activation case does not — `UNDECIDED:` these two call sites
  produce structurally different `detail` for the same event type, which a
  consumer distinguishing "how was this activated" needs to know);
  `lease-takeover` on `--takeover`, `detail` naming the previous holder.
- Stdout: the acted-on lease id, printed after the durable write.
- No database table, no forge object.

### bin/fleet-cli-canary

declared

**Purpose.** Checks a not-yet-linked codex-cli binary against everything the
fleet asks of it before the owner relinks `/opt/homebrew/bin/codex` to it:
its `--version` differs from the linked CLI's, every flag the fleet's codex
runners pass is present in `<candidate> exec --help`, the candidate starts
and answers a one-line prompt under the tester sandbox profile, the
installed reviewer sandbox profile, and with no outer profile at all, and
one real test lane (a fixed plan and sha) runs against it with its usage
compared to a recorded baseline. `codex --startup-only` runs the flag and
start-up checks alone and is allowed on the linked CLI itself, as a positive
control. It never relinks, installs, upgrades or downloads anything, posts
or pushes nothing, opens no pull request, and runs no review.

**Exit codes.**
- `0`: every step that ran passed.
- `1`: a step failed — the report names which one. This covers a failure of
  step 4's own work too (a `git archive`/extraction failure, or the pinned
  plan file missing at the pinned sha): the fixed sha not existing or not
  extracting is a failure of the lane this step runs, not a usage error, so
  it is reported and paged like any other step fail — never silently
  discarded after steps 1-3 already ran and spent their vendor turns.
- `2`: a refusal before any step runs (the candidate is not executable, its
  `--version` or `exec --help` could not be read, `config/cli-canary.json`
  is missing or unreadable, or the candidate is the same file as, or reports
  the same version as, the linked CLI), or argparse's own exit on malformed
  arguments. `config/cli-canary.json` is read and validated up front, before
  step 1, so a broken config is caught before any step runs rather than only
  once step 4 is reached.

**Writes.**
- `<work root>/canary/codex/<candidate's own version>.json`: every step's
  outcome, the lane's usage, and the elapsed time.
- A page through the orchestrator's `notify`, once per run that reaches a
  pass or a fail, naming the version, the first failed step on a fail, and
  any startup probe left `skipped` even on a pass (so a clean pass is never
  read as "every check ran" when one did not).
- A `mkdtemp` scratch directory per start-up probe and per lane run, removed
  before the process exits; nothing under the candidate's own path, nothing
  under `/opt/homebrew/bin/codex`, no branch, no pull request, no lease, no
  `fleet.db` row.

**Shape.** The report's `steps` holds `version` (`pass`/`fail`/`skip`),
`flags` (`checked`, `missing`), `startup.tester` / `startup.reviewer` /
`startup.writer` (`pass`/`fail`/`skipped`, the last only for the reviewer
profile when it cannot be confirmed to grant the one write it needs), and
`lane` (`pass`/`fail`, `usage`, `baseline`). The `writer` probe runs with no
outer sandbox profile AND the real `writer_argv`'s own flags, never the
tester argv's — it exists to start-up-test the actual unconfined writer
invocation, not a tester-flavoured one. `brew` names whether the codex
cask is still installed (D3's upgrade procedure assumes it is not).

### bin/fleet-cloud-review

declared

**Purpose.** Sends one pull request to a Claude cloud session the owner
created by hand, so the review is billed to the cloud credit, and brings the
review back as a file. `request` refuses unless the repository is listed in
`config/cloud-review.conf`, the credit's expiry date (UTC) has not been
reached, the request counts are under their limits, and an unauthenticated
read of the mirror answers 404; it then pushes the pull request's base and
head to the mirror and messages the session. `collect` accepts the session's
branch only when its first line names the request and its diff adds
`review.md` alone. `prune` deletes a request's branches from the mirror.

**Exit codes.**
- `0`: done.
- `1`: a refusal, with the check named.
- `2`: argparse's exit on malformed arguments.
- `3`: `collect` found no review branch yet.

**Writes.**
- Branches `req/<req-id>/*` on the mirror, deleted by `prune`.
- One line per request appended to `requests.jsonl`, and one review file per
  collected request, under `cloud-review/` in the fleet directory.
- Never the forge: no review, no comment, no status.
- A temporary clone, removed on every path. It touches no shared checkout,
  pin or worktree.

### bin/fleet-config

declared

**Purpose.** The single writer for a small, closed set of fleet-scoped
settings (the dispatch concurrency cap, and the per-runner test-lane run
cap `lane_run_cap`, which has no default) held in the fleet's
shared database, with every change also appended as a durable event so the
setting can be rebuilt from the event ledger alone if the database is
lost. Subcommands: `get`, `set`, `list`.

**Exit codes.**
- `0`: success for `get`, `list`, and `set` (including a no-op `set` to an
  already-current value).
- `1`: `get` given a key outside the closed settings set; `set` given an
  unknown key or a value that fails that key's own validation, or the
  durability write to the event ledger failing — in which case the whole
  database write is rolled back, so a change is never made unless the
  ledger accepted it too.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- Database: a settings table (one row per key: current value, last-changed
  timestamp) and an append-only change-audit table (one row per write: an
  id, timestamp, an actor string, a source label, the key, and its old and
  new value).
- Event emitted on every value-changing `set`: `fleet-setting-changed`,
  appended before the database transaction commits. `detail` carries
  exactly the key and its old and new value — documented as never to carry
  a credential.
- Stdout: `get` prints the key, its value, source, and last-changed time
  (or the same as JSON). A key with neither an environment fallback nor a
  default (`lane_run_cap`) with no row prints `unset`, source `unset`
  (JSON `"value": null`) and exits 0; `list` prints one line per row; `set` prints a
  confirmation or "unchanged" line.
- No forge object; no filesystem writes beyond the database file and its
  own journal sidecars.

### bin/fleet-cr

declared

**Purpose.** The change-request queue used when a lane needs a change made
to a file it doesn't own: `file` submits a request; `apply`/`reject`
transition it, gated to the session holding an active integrator-role
lease on that repository; `list` reads the queue.

**Exit codes.**
- `0`: success for `file`, `apply`, `reject`, `list`.
- `1` (this program uses no other numeric code of its own; every refusal
  exits this way), grouped by cause: a patch argument exceeding the size
  cap, unreadable, or not valid text; the per-lane id counter unreadable or
  torn (refused before anything is written, to protect the guarantee that
  an id is never reused); the counter's own atomic write failing; `apply`
  or `reject` invoked by a session with no active integrator-role lease on
  the target repository; acting on an id that doesn't exist or isn't in
  the filed state (a transition happens at most once); `list --state`
  given a value outside the closed state set; anything the shared record
  validator rejects — malformed shape, an invalid repository/lane/id
  shape, neither intent nor patch given, an oversized field, or content
  matching a secret-shaped pattern in the intent, patch, or target (this
  queue is rendered into a tree read by internal tooling, so secret-shaped
  content is refused outright, naming only the rule that matched, never
  the content).
- argparse's own exit 2 on malformed CLI arguments or a missing subcommand.

**Writes.**
- One JSON file per change request (path shape: a directory tree keyed by
  repository and lane, filename `<lane>-<nnn>.json`), written
  temp-file-then-rename. Fields as in the Change-request record shape
  below, plus `applied_commit`/`applied_at` once applied.
- A per-lane monotonic counter file, never reused even across deletions,
  written with an explicit fsync of both the file and its directory.
- Lock files used only to serialise filing and transitions.
- Events: `cr-filed` on a successful `file` (detail: the id, repository,
  lane, target); `cr-applied` on a successful `apply` (detail: the id,
  repository, applying commit, lease). No event on `reject` — the closed
  event-type set has no rejection type; the queue record itself is the
  only record of a rejection, by design.
- Stdout: `file` prints the newly allocated id; `apply`/`reject` print the
  id acted on; `list` prints JSON or one fixed-width text line per record,
  with control characters stripped so a hand-edited record cannot forge a
  terminal escape sequence.
- No database table; no forge object — a change request is a fleet-local
  queue, and the actual change still lands only through a normal pull
  request.

### bin/fleet-credits

declared

**Purpose.** The one writer of the API-credits ledger (plan 0146, ADR-0023). The
owner opens a window of at most seven days (`on --days N`), closes it at once
(`off`), and frees an expired reservation by hand (`release ID`); `on` and
`release` refuse unless stdin is a terminal. `status [--json]` prints the open
window, the month's spend against its cap, and per-pull-request spend. `decide`
answers `subscription`, `credits` or `hold` for one review and, on `credits`,
reserves the budget it hands out; `decide --peek` answers the same and reserves
and emits nothing. `record` stores the spend the reviewer reported and settles
the newest open reservation; `record --dry-run` prints the row and writes
nothing. `refused` records a refusal, at most once per repository, pull request
and reason per window. A reservation nothing settled within four hours is
booked at its full amount. The program never reads or holds the API key.

**Exit codes.**
- `0`: any answer the program could make, including `off` with no window open.
- `1`: `on` or `release` without a terminal or refused by a check; `decide`
  could not read the database or the gate (callers treat that as `hold`).
- `2`: argparse's exit on malformed arguments, such as `--days` outside 1 to 7
  or a bad `--usd`.

**Writes.**
- `fleet.db`: the tables `credit_window`, `credit_reservation`, `credit_spend`,
  `credit_notice` and `credit_release`, created on first use, every row stamped
  with `created_at` and `updated_at`.
- Events: `credits-window-opened`, `credits-window-closed`, `credits-spent`,
  `credits-cap-reached`, `credits-reservation-expired`, `credits-refused`,
  `credits-grant-given`, `credits-grant-revoked` and `credits-job-spent`,
  each with the detail keys SPEC.md names. Pages for a window opening or
  closing, a cap reached and an expired reservation, each once per window.
- Stdout: `decide` prints one JSON object with `bill`, `budget_usd`, `reason`
  and `window_ends`; `record` prints the row; `status` prints text or JSON.
- No forge object, and no network call except the dispatcher probe `on` runs.

### bin/fleet-db-migrate

declared

**Purpose.** Creates every table the fleet's schema owners define in the
shared database, so a reader that refuses a missing table finds it. Applies
each owner's own schema definition (never a copy of it) and that owner's own
additive migration step, and nothing else.

**Exit codes.**
- `0`: every owner's schema applied (or, under `--dry-run`, listed).
- `1`: at least one owner failed to load or apply; the others are still applied.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- Database (path from `FLEET_DB`, else the fleet's data area; created if
  absent): additive `CREATE ... IF NOT EXISTS` and the review table's
  add-missing-column step only. Never drops, deletes or removes a column.
  The collector's derived tables and its schema version are not touched.
- `--dry-run` writes nothing and does not create the database file.
- No event, no forge interaction. Stdout: one `ok <owner> (<n> tables)` or
  `FAILED <owner>: <error>` line per owner.

### bin/fleet-db-snapshot

declared

**Purpose.** Takes a consistent, dated copy of the fleet's shared database
for the offline tier, using the database engine's own online backup
mechanism rather than a plain file copy.

**Exit codes.**
- `0`: snapshot taken and verified, old copies beyond the retention count
  pruned.
- `2`: unrecognised flags, or a `--keep` value below 1 (rejected explicitly
  — zero would mean "keep everything" and silently fill disk on an
  unwatched schedule).
- `1`: no database found at the resolved path; a snapshot for the current
  second already exists (refuses to overwrite); the new copy fails an
  integrity check, or a leftover write-ahead-log sidecar still holds
  unflushed data when flattening to one file — in both cases the partial
  file is deleted before exit, never left looking valid.

**Writes.**
- Snapshot directory (default under the fleet's data area, overridable): a
  hidden, partial file during the copy, atomically renamed on success to a
  timestamped filename with a prefix distinct from the fleet's other
  snapshot tool so their retention sweeps never touch each other's files.
- Retention: with `--keep N` (default 7), deletes the oldest snapshots
  beyond the newest N once the new one is confirmed good.
- No event, no forge interaction. Stdout: one summary line (path, size,
  a row count from a representative table, integrity status), then one
  line per pruned file.

### bin/fleet-deploy

declared

**Purpose.** The `homefleet` provider for the `dev` environment: for each
binding in the environments configuration whose host is this machine, it
notices a new commit on the pin's default branch, moves the pin, restarts
the unit through `fleet-svc`'s gate under a `service` lease, verifies the
service's health body (and that its process id changed), and rolls back once
to the last verified commit if verification fails. Subcommands: `validate`
(check a service's deploy manifest), `run`, and `retry` (one cycle for one
binding that ignores the already-attempted skip).

**Exit codes.**
- `0`: `validate` on a manifest with no errors; `run` and `retry` on any
  normal cycle, including a refused, skipped, rolled-back or failed
  deployment (those are reported per binding and paged, never turned into a
  nonzero exit), and "another run is already in progress" (a non-blocking
  single-instance lock).
- `1`: `validate` on a manifest that cannot be read as JSON or fails
  validation.
- Non-zero with a message: an unparseable or invalid environments
  configuration, or `--binding` naming no configured binding.
- `2`: argparse's own code on a malformed invocation.

**Writes.**
- Its own non-blocking lock file.
- A `service` lease held under the session id `fleet-deploy` for the length
  of a deployment, released at the end.
- On a deployment: moves the pin worktree's detached head to the target
  commit, and on rollback back to the last verified commit.
- Events `deploy-started` and `deploy-verified`, written by actor
  `svc:fleet-deploy`, `detail` recording environment, service, binding,
  provider, and the kind of deployment.
- A push notification on a refusal (once per binding, target commit and
  reason), a rollback, a failed rollback, and a needed host preparation,
  through the same channel as the other watchers.
- Marker files recording which refusals have already been paged.
- Never installs dependencies and never restarts a unit any way but through
  `fleet-svc`'s gate. `--dry-run` prints each step and changes and emits
  nothing.
- Stdout: one status line per binding.

### bin/fleet-deps

declared

**Purpose.** Keeps up to three commit statuses on the head of every open pull
request targeting `main` in each repository the watcher scans. `eunomia/deps`:
`failure` when the pull request names a plan (`Plan: <id>`) that is not on
`main` at `ready` or `done`, or whose dependencies are unmet; `success` for
everything else, including a pull request naming no plan; `pending` when it
cannot decide. `eunomia/plan-lint` (plan 0177): `failure` when `bin/fleet-plan`'s
own `lint()`, run read-only against that repository's `plans/` directory and
`governance.yaml` at the pull request's head, finds a problem in a plan file
the pull request adds or modifies; `success` at 0 problems; `pending` when the
files cannot be read; no status at all when the pull request changes no file
under `plans/`. Subcommand: `once` (one pass).

**Exit codes.**
- `0`: a pass completed, including one where some pull requests were
  undecidable (those post `pending` and page) or the repository list could
  not be read (paged, nothing checked).
- `2`: a refused run (no token from the configured helper, or the token's
  account is not the agent merger; nothing is posted and it pages), or a
  missing or unknown subcommand.

**Writes.**
- A commit status in the context `eunomia/deps`, and one in `eunomia/plan-lint`
  when the pull request touches `plans/`, on each pull request head — each
  posted only when its state or description differs from the latest status
  in that context created by the agent merger account itself; another
  account's status in the same context never suppresses a post.
- A push notification once per episode for a refusal, an unreadable list,
  or a `pending` pull request (either context), through the same channel as
  the other watchers, and marker files recording which episodes have been
  paged.
- Never uses any other account's token and never falls back to one. Reads
  the watcher's dependency check and `fleet-plan`'s lint without modifying
  either. Nothing is executed from a pull request's checkout: only its
  `plans/*.md` and `governance.yaml`, read through the forge into a
  temporary directory removed at the end of each check, are ever passed to
  `lint()`.
- Stdout: nothing. Stderr: one line per status posted or refusal.

### bin/fleet-depsd

declared

**Purpose.** The table owner for an owner attestation that satisfies an
`external:<slug>` entry in a plan's `depends_on` (plan 0180), and (plan 0181)
a resident service, `serve`, that re-checks every held plan when a watched
repository's main moves or the owner attests a slug. Subcommands:
`attestations [--json]` and `serve [--once [--dry-run]]`. There is no
`attest` or `revoke` subcommand, and no library function writes
`dependency_attestation` outside `serve`'s own authenticated POST/DELETE.

Plan 0189 extends `serve`'s own `POST /webhook/forgejo` (the same signed
route plan 0181's `push` handling already answers) with a `pull_request`
branch: a lifecycle delivery (opened, reopened, a push onto the PR's own
branch, or taking it out of draft) persists a `plan_review_job` row
through plan 0188's existing `bin/fleet-plan-review` queue — reading the
pull request's CURRENT head from the forge, never trusted from the
delivery's own `head` field — before answering `202`, and never dispatches
one itself. `serve` (resident) runs a SECOND worker loop, on its own
thread, that reconciles a bounded slice of
the enrolled registry (a resumable cursor in `fleet.db`, recovering a
missed delivery) and then claims and dispatches a bounded worker pass
through `bin/fleet-plan-review`'s own `_claim_jobs()`/`_dispatch_one()` —
never a restated claim or dispatch path. This loop is deliberately separate
from the one that triggers `recompute()`, so a slow or waiting review can
never delay push processing. See docs/depsd.md and
docs/plan-review-dispatch.md.

**Exit codes.**
- `0`: `attestations` printed its listing; `serve --once` completed one
  `recompute()` pass; `serve` (resident) returning after a clean shutdown.
- `1`: `attestations` on an unreadable store; `serve` (resident) refused to
  start because its service lease is held elsewhere, or the lock file is
  held by another instance on this host.
- `2`: argparse's own code on a missing or unrecognised subcommand
  (including `attest`/`revoke`, which do not exist), or a configuration
  value outside its accepted range (a wildcard bind address; a poll interval
  outside 60..3600 seconds).

**Writes.**
- `attestations` and `serve --once --dry-run` write nothing; `attestations`
  opens the database read-only (`mode=ro`).
- `serve`'s `recompute()` owns `dependency_hold` and `dependency_scan`
  outright, replacing one repository's hold rows in one transaction per
  pass, and never deletes a repository's rows on a read failure. It also
  deletes both tables' rows for any repository no longer in `repos.conf`'s
  dispatch list, so a dropped repo's holds do not go on satisfying the
  attestation POST's "cited by a held plan" check forever.
- `serve`'s POST/DELETE on `/api/v1/attestations[/<slug>]` are the only
  writers of `dependency_attestation`/`_change`, gated by a same-host peer
  check and a bearer token read from a configured helper's stdout — held in
  memory only, never written to disk, argv, a log or a response.
- `serve` writes a wake file per `(repo, plan)` whose last hold clears,
  under the fleet state directory's `wake/` spool, for `bin/fleet-watch` to
  consume.
- `serve` (resident) takes a `service` lease on `<host>:fleet-depsd`, keyed
  by this machine's hostname (never `FLEET_DEPSD_BIND`'s address), and
  heartbeats it; releases it on SIGTERM.
- Plan 0189: a signed `pull_request` delivery whose `action` is one this
  file treats as lifecycle-worthy, for an enrolled repository, writes one
  `plan_review_job`/`plan_review_job_change` row pair — through
  `bin/fleet-plan-review`'s own `_enqueue()`, the one writer plan 0188
  already established, never a second one here. `serve` (resident) also
  owns `plan_review_reconcile_cursor` outright (one row, advanced one
  bounded slice of the enrolled registry per pass) and, through
  `bin/fleet-plan-review`'s own `_claim_jobs()`/`_dispatch_one()`, claims
  and may dispatch plan-review jobs the same way that file's own `worker`
  subcommand does — this file adds no second writer of `plan_review_job`
  either. One check runs only here, between the claim and `_dispatch_one`:
  `bin/orchestrator`'s `review_quota_hold`, which `_dispatch_one` itself
  never calls — holding (no attempt spent) while the owner's
  session/weekly/model gates are closed, since this wake, unlike a
  hand-run `fleet-plan-review worker`, fires unattended. It also never
  reclaims the one job still ready after holding it within the SAME pass,
  so a transient failure spends at most one of `MAX_DISPATCH_ATTEMPTS`
  before the next, separately-timed pass.
- Forge: beyond `recompute()`'s own reads, a `pull_request` delivery reads
  the pull request once (its current head) and, unless it is already a
  draft, closed or merged, its changed-file list (to confirm the change is
  plan-only before any row is written); the reconciliation pass pages
  through each chosen repository's open pull requests (bounded per pass —
  see docs/depsd.md) and reads every one's changed-file list the same way;
  a dispatched job may post a plan review to the pull request under the
  fleet's existing reviewer identity, exactly as `bin/fleet-plan-review
  worker` posting one itself would.
- Subprocesses: plan 0189's worker pass, through `bin/fleet-plan-review`'s
  own `_forge()`, runs the SAME token helper `bin/orchestrator`'s own
  "authoring" role does (`FLEET_AUTHOR_TOKEN_CMD`) — the write-scoped
  credential every forge call in that file makes, read or write — and
  holds it in memory for the rest of this process's life, cached once by
  `_forge()`'s own module-level singleton — a FOURTH in-memory credential
  beside `recompute()`'s own read-only watch token and the webhook secret
  and attest token documented above, and the only one of the four that can
  write to a repository. A dispatched job, through
  `bin/fleet-plan-review`'s `_dispatch_one()` and `bin/orchestrator`'s
  `dispatch_review()`, spawns `dispatch-review.py` as a subprocess (given
  `--post --no-followups`) — this is the one place that actually posts the
  review; this process never reads or logs that credential itself, and
  never reads a verdict from the subprocess's own output, only from the
  forge afterward (`bin/fleet-plan-review`'s own `_dispatch_one`).
- Stdout: `attestations` prints one line per attested slug (slug, latest
  actor, note) or the `--json` array, and `no dependency_attestation rows`
  when the table is empty; `serve --once` prints the holds and wakes it
  recorded (or, with `--dry-run`, would have); `serve` (resident) prints one
  line per webhook delivery (its id, the repository and the outcome code —
  never a header or the body), one per refused attestation write (the
  peer address only), and one per bounded worker pass that claimed at least
  one plan-review job (how many, and how many completed without raising).

### bin/fleet-drift

declared

**Purpose.** A read-only watcher, meant to run on a schedule, that polls
each configured service's own health URL for its verdict on whether it is
running the default branch's code, and pages once when a service has stayed
behind (or unanswerable) for longer than its allowance. It never deploys,
restarts, or rolls back, and never computes a verdict of its own.

**Exit codes.**
- `0`: the run completed, whatever the services said.
- `2`: the configuration was refused (a malformed line, an unknown service
  name, or a duplicate) or the invocation was malformed.

**Writes.**
- One small state file (JSON) per watched service under the fleet state
  directory: when the current spell of non-current samples began, the latest
  state and its reason, and whether the spell has been paged.
- One page per spell once it outlasts the allowance, worded by the latest
  state, and one "current again" page when a paged spell ends; each page is
  recorded in the page log with this unit's name. A spell that was never
  paged ends silently.
- Page reasons come from a fixed vocabulary, never a response body or an
  exception message. `--dry-run` prints each classification and writes and
  pages nothing.
- Stdout: one line per run summarising the counts.

### bin/fleet-emit

declared

**Purpose.** Append exactly one event record to the fleet's append-only
event ledger — the single write path every other fleet tool that emits an
event goes through.

**Exit codes.**
- `0`: event appended; the record just written is printed to stdout.
- `2`: the positional event type is missing, or an unrecognised flag is
  given.
- `1`, grouped by cause: an event type outside the closed set (rejected
  rather than silently accepted); no actor available (no session identity
  set and none given explicitly); a lease-scoped event given without a
  lease id, or a non-lease-scoped event given one (the record shape
  requires a null lease for the latter); a detail argument that isn't
  valid JSON, or valid JSON that isn't an object (the raw value is never
  echoed back, in case it is a mistyped credential paste); the rotation
  archive target for this second already exists.

**Writes.**
- The event ledger: appends one compact JSON line per call, under an
  exclusive lock on a companion lock file. Before appending, repairs a
  torn final line (a missing trailing newline left by a prior writer that
  was killed mid-append), then rotates the ledger to a timestamped archive
  once it crosses a fixed size threshold — all while still holding the
  lock, so a rotation racing an append cannot lose data.
- No forge or database write.

**Shape of one record** (see also the Event record table below):
`{"ts", "actor", "type", "repo", "pr", "sha", "lease", "detail"}` — `ts` a
UTC timestamp, `type` one of the closed event-type set, `lease` required
for lease-scoped types and null otherwise, `detail` a producer-defined
object defaulting to empty.

### bin/fleet-events

declared

**Purpose.** Read-only tail/filter CLI over the same event ledger
`fleet-emit` writes — how a session checks recent fleet activity before
touching shared state. Never takes a lock; reads a complete old or new
file, relying on the ledger's atomic-rename rotation discipline.

**Exit codes.**
- `0`: normal completion, including a `--follow` run that is interrupted
  externally.
- `2`: malformed flags.
- `1`: an unrecognised `--type` value (checked against the same closed set
  `fleet-emit` enforces); an unparseable `--since` value — not a relative
  duration, not a bare date, not a full timestamp, including a
  shape-valid but calendrically impossible date.

**Writes.** None to disk. All output is stdout: one compact JSON line per
matching event (`--json`), or a formatted human-readable summary line.
With `--follow`, keeps the file open across a rotation, detects the
change, and reopens/replays any newly created archive so nothing in
between is skipped, polling on a short fixed interval. With `--since`, may
also read rotated archives whose filename-embedded rotation time is
provably after every event they contain.

### bin/fleet-leak-watch

declared

**Purpose.** A detection backstop, not a prevention control: scans
newly-appended bytes of session transcripts for the shape of a leaked
credential (several vendor token shapes, a JWT, a private-key header, a
cloud access-key id, and — under `--paranoid` — noisier shapes such as long
hex runs or `key: value` assignments), and pages once per distinct
finding.

**Exit codes.** `0` always on completion, in both normal and `--dry-run`
modes; `2` on malformed flags. No other exit path exists.

**Writes.**
- A state file (path shape under the fleet's data area) recording, per
  scanned transcript, the byte offset already scanned, and a list of
  dedupe keys already paged. Written atomically; not written at all under
  `--dry-run`.
- No file, event, or database write this tool produces ever contains the
  matched secret text itself — only its hash, the matched rule's name, the
  transcript's relative path, and a line number.
- On a new, non-duplicate, non-fixture finding: sends a plain-text alert
  (naming the rule, transcript path, and line) through a configurable push
  channel if one is set, else prints it to stdout; either way appends one
  row to the fleet's shared page log recording the attempt.
- Recognises and discounts (but still counts, in its own summary) matches
  that are the test suite's own placeholder credential, so it never pages
  on its own fixtures.

**Shape.** Reads only the `tool_result` content of session transcripts —
never prompt or assistant text — so a credential a tool call surfaced can
be caught without scanning the model's own reasoning or output.

### bin/fleet-merge

declared

**Purpose.** Merges, as the agent merger account, the pull requests ADR-0019 §4
allows on an autonomous repository: a `src` pull request under a live plan, or
that plan's `tests` pull request. Every other pull request is left alone and
the deciding check is logged. It is mechanical: no model is called. Subcommand:
`once` (one pass).

**Exit codes.**
- `0`: a pass completed, including one where some pull requests were
  undecidable (those page) or the repository list could not be read (paged,
  nothing checked).
- `2`: a refused run (no token from the configured helper, or the token's
  account is not the agent merger; nothing is merged and it pages), or a
  missing or unknown subcommand.

**Writes.**
- A merge of a pull request that passed every check, with the checked head
  commit as the expected head so a later push makes the forge refuse it. Never
  schedules an auto-merge, never rebases, never pushes, never retries a failed
  merge in the same pass.
- One log line per merge and per refusal past the autonomy check, naming the
  pull request, the head commit and the deciding check; a pull request blocked
  on the same check at the same head is logged once. A `src` pull request
  touching a protected path (check 4) does not stop there: every later check
  still runs, and the refusal logged is whichever one of those fails, or check
  4 again if none do.
- A push notification for an undecidable read or an identity failure, through
  the same channel as the other watchers, and marker files recording which
  episodes have been paged. The same channel also carries, once per pull
  request head, a page for a `src` pull request refused only at check 4 with
  every later check holding: title "Ready for your merge", body naming the
  repository, pull request number, plan, head and protected path, and the
  pull request's web URL. A pull request refused at any other check, or at
  check 4 together with a later one, never pages.
- Never touches a critical repository, not even to comment.
- Stdout: the log lines.

### bin/fleet-models

declared

**Purpose.** The single writer of per-repository "which runner fills which
agentic role" configuration (implementer, plan reviewer, code reviewer,
tester, and other named roles) in the fleet's shared database, with every
write mirrored to the event ledger for durability, and a reconstruction
routine able to rebuild the table from that ledger alone.

**Exit codes.**
- `0`: `list`, `get`, `set`, `disable`, `enable` all completed — including
  "no rows", "unchanged", and "already disabled/enabled" as normal
  outcomes.
- `1`: `set` given a runner name outside the known runner registry, an
  invalid position, or a value failing an internal check; the ledger
  append for a write failing (the whole write is rolled back — see the
  Records section for the durability convention this shares with
  `fleet-config`); `disable`/`enable` given a row that doesn't exist.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- Database: a configuration table (one row per repository, position, and
  ordinal — runner name and a disabled flag; rows are never deleted,
  removal is the disabled flag, and a disabled slot's ordinal is never
  reused) and an append-only change-audit table (one row per write: id,
  timestamp, actor, source, repository, position, ordinal, old/new runner,
  old/new disabled flag).
- Event `repo-model-changed` appended per config change, before the
  database transaction commits. `detail` carries the change id, source,
  position, ordinal, and old/new runner and disabled values — documented
  as never carrying a credential.
- Event `seat-access-changed` appended per reviewer-seat access change
  (`access set`), before the transaction commits. `detail` carries the
  change id, source, position, ordinal and old/new access — never a credential.
- Stdout: `list` prints one line per row; `get` prints one line per
  position with the resolved runner and which layer resolved it, or a
  marker if nothing resolves (an unresolvable runner name prints an error
  line to stderr rather than failing the whole command); `set` /
  `disable` / `enable` each print one confirmation line.
- No forge object; no subprocess beyond loading sibling modules in-process
  (not `subprocess`).

`UNDECIDED:` unlike `fleet-ci`, this file does not wrap its database
operations in a caught, dedicated exit path — a locked or corrupt database
would surface as an unhandled exception rather than a documented exit
code.

### bin/fleet-orphans

declared

**Purpose.** A read-only scanner, meant to run on a schedule, that walks
every configured repository's non-default branches and classifies each as
an orphan (content not on the default branch, not covered by any pull
request, and older than an age threshold), stale (already landed by some
other path, such as a squash merge that left the branch undeleted), or
quiet (uncovered but still young) — and pages once per newly discovered
orphan. It never deletes, closes, or comments on anything.

**Exit codes.**
- `0`: scan completed, no unresolved read failures, no orphans found.
- `1`: scan completed cleanly but at least one orphan branch was found.
- `2`: one or more read failures occurred during the scan (token-helper
  failure, an unreadable repository search, branch list, pull-request
  list, or commit walk, an unusable default branch, a repository that
  dropped out of visibility between runs, or a malformed configuration
  row) — this takes priority over reporting orphans, since an incomplete
  scan must never be reported as "no orphans" or conflated with a clean
  orphans-found result. Numerically the same code argparse itself uses on
  a malformed invocation, so from the exit code alone a caller cannot
  distinguish "the scan ran and hit trouble" from "the scan never ran".

**Writes.**
- A state file (JSON) recording findings already paged (keyed by
  repository and commit, so a given orphan is paged at most once ever),
  the set of repository names seen on the last full scan (to detect one
  silently disappearing), and read failures already paged once per
  reason per calendar day. Written atomically; not written under
  `--dry-run`.
- A push notification per newly found orphan and per newly seen distinct
  failure reason, through a configurable channel if set, else a stderr
  line; either way recorded to the fleet's shared page log.
- Only read calls to the forge (repository search, get-repository,
  list-branches, list-pulls, list-commits, get-contents) — no branch,
  pull request, issue, or comment is ever created, modified, or deleted.
- No event emitted; no database write.
- Stdout: a scan-summary line, then one line per stale or orphan branch
  found. Stderr: one line per read failure, plus notification lines when
  the push channel is unconfigured or fails.

**Shape.** "Landed" is determined structurally, not by commit count: the
tool walks every path a branch's own commits touched and checks whether
the same content is reachable in the default branch's own history of that
path, so a rebased or squash-merged branch is correctly recognised as
landed even though its raw commit list never appears on the default
branch.

`UNDECIDED:` (named in the tool's own source) the credential it uses is a
full read/write-scoped token even though every call it makes is a read,
because no read-only credential currently exists on this fleet — a known,
accepted gap rather than something this tool closes.

### bin/fleet-pin-advance

declared

**Purpose.** Fast-forwards a stale, detached-HEAD "pin" worktree — the kind
every scheduled or background unit reads instead of a session's own
checkout — onto its configured upstream, but only once a check confirms
the target repository's default branch is protected, i.e. reaching that
commit already required a reviewed merge. Never restarts any process that
reads the pin.

**Exit codes.** `0` always on a normal run, including "nothing configured
to advance" and "another instance is already running" (a non-blocking
single-instance lock, so a second run exits cleanly rather than racing).
`2` on malformed flags. Per-pin failures are collected and reported, never
turned into a nonzero process exit.

**What it refuses** (reported per pin, process still exits 0): a pin that
isn't cleanly stale (dirty tree, attached branch, missing worktree, not a
repository, unreachable upstream); a stale pin whose current head isn't an
ancestor of the target; uncommitted local changes immediately before the
checkout; a repository the protection check can't resolve or won't vouch
for.

**Writes.**
- Its own non-blocking lock file.
- On advance: moves the pin worktree's detached head to the new commit —
  nothing else in that worktree changes.
- Event `pin-drift` (reused, not a new event type), `detail` recording the
  path, ref, the transition from stale to current, and the action taken
  with old and new commits.
- A push notification stating a restart is due and that this tool never
  performs one itself, recorded to the fleet's shared page log the same
  way `fleet-leak-watch` records its own.
- Stdout: one line per advanced pin, plus a summary count of advanced
  versus refused.

### bin/fleet-plan

declared

**Purpose.** Subcommands over feature plan files: `lint`, `list`, `show`,
`audit`, `graph`, `ledger`.

**Exit codes.**
- `0`: `list`; `show` (id found); `ledger` (repository scope resolved,
  regardless of database state); `lint` with zero errors; `audit` with no
  refusal (a finding of "not built" is itself a normal, non-failing
  result); `graph` with no dependency cycle.
- `1`, grouped: `lint` — at least one plan fails validation (missing or
  invalid required front-matter, a missing required section, an id not
  matching its filename, an id claimed twice, an unresolvable or cyclical
  dependency, or an unfilled required section); `show` — no plan resolves
  under the given id; `audit` — refused outright because the checkout is a
  shallow clone (or shallowness couldn't be confirmed) or the target
  ref's tree couldn't be read; `graph` — the dependency graph contains a
  cycle (the graph is still emitted; the exit code is the only failure
  signal).
- `2`: `ledger` — the repository couldn't be determined and none was
  given explicitly; argparse's own exit 2 on a missing or unknown
  subcommand or an invalid choice value.

**Writes.**
- Nothing on disk by default — every subcommand except `graph --out` is
  read-only over the plan files, the pointed-at database (opened
  read-only for `ledger`), and, for `ledger --verdict` or
  `ledger --with-filings`, the forge.
- `graph --out PATH` writes the chosen format (a graph document, a diagram
  description, or an image) to that path; with no `--out`, the same text
  goes to stdout.
- No event, no forge object created or modified, no database table
  written — the whole program is a reader and reporter.

**Shape.** `lint` errors and dependency warnings are one string per
finding. `audit`'s per-plan result names its state (not built, touched
after filing, undecidable, error) plus supporting evidence, scoped to
draft-status plans only. `graph` emits a node/edge/cycle structure, each
edge carrying its form (a plan dependency, a path dependency, or an
external one) and whether it is satisfied. `ledger` emits per-plan rows
(status, run count, outcome, pull-request state, review rounds, a
classification of claimed/stalled) alongside a fixed set of caveat notes
explaining what that classification does and doesn't mean. In
`--format json` only (plan 0196), every row also carries `submitted_by`
(`"intakeapp"`, `"cli"`, or `null`) and `stages` — a fixed-order object
keyed `submitted`/`approved`/`implementation`/`review`/`test`/`merged`,
each naming its own state (`empty`/`running`/`completed`/`failed`/
`unknown`), timestamps, attempt count, budget and token usage; see
`docs/plan-ledger.md` for the full shape. `--with-filings` additionally
reads the forge for each row's filing pull request (same effect on
`submitted`/`submitted_by` as `--verdict`) and appends rows, `status:
"in-review"`, for plan files added by an open pull request not yet on
`main`.

### bin/fleet-plan-review

declared

**Purpose.** A durable queue and bounded worker so a plan-filing pull
request's review survives the session (or worker run) that filed it:
`enqueue <repo> <pr>`, `status`, `worker`.

**Exit codes.**
- `0`: `enqueue` queued the job or found it already tracked; `status`,
  whatever rows it found or didn't; `worker`, whatever it claimed, including
  zero jobs.
- `1`: `enqueue` could not read the pull request or its head sha from the
  forge; any command whose authoring token could not be resolved.
- argparse's own exit 2 on a missing or unknown subcommand or an invalid
  argument.

**Writes.**
- Database (`plan_review_job`, keyed uniquely on repo, pull request and
  head sha; `plan_review_job_change`, its append-only history, one row per
  transition with an actor and a timestamp): `enqueue` inserts a `queued`
  row if none exists for that exact head; `worker` claims ready rows
  (`queued`, `held`, or an abandoned `claimed` row past its TTL) and moves
  each to `held`, `claimed`, `done`, `superseded`, `skipped`, or `failed`
  after re-reading the pull request, its changed-file list and its CI
  status from the forge.
- Forge: `worker` posts no review itself; on an eligible job it calls the
  same dispatcher the orchestrator uses, which may post a plan review to
  the pull request under the fleet's existing reviewer identity. A head
  that moves between claim and dispatch supersedes the row and enqueues a
  fresh one for the new head rather than posting against stale content.
- No event. Stdout: `enqueue` prints the job id and whether it was newly
  created; `status` prints one line per row (or the same as JSON with
  `--json`); `worker` prints how many jobs it claimed.

### bin/fleet-quota

declared

**Purpose.** The one writer of the usage-limit gate (plan 0094): one row per
scope (`session`, `weekly`, `model:<bucket>`) saying whether new work may
start. `observe` reads UsageBar's `latest.json`; `close` records a stop;
`open` is the owner's override (`open review-path:<repo> --by owner [--for <hours>]`, default 24, 1–168, also holds the gate open against failing review-path probes for that window); `check` answers a one-off script; `status`
prints the rows. `probe-review-path <repo>` (plan 0095) runs the review
dispatcher's pinned prerequisites for one repo and writes `review-path:<repo>`
from the result; `close` refuses a `review-path` scope, which only that
subcommand closes. `observe`, `close`, `open`, `check` and `status` read one
local file and never contact a network service; `probe-review-path` also reads
the review dispatcher's published call contract (`FLEET_REVIEW_CONTRACT`) and
execs the pinned programs (`FLEET_REVIEW_PINS`) as subprocesses with the
token-helper variables removed from their environment.

**Exit codes.**
- `0`: every subcommand but `check` succeeds, including a write that changed
  nothing; `check` finds the scope (or, with none named, session and weekly)
  open.
- `75` — `check`: a gate is closed. A script stops here and starts no further
  run. `check` with no `--scope` ignores `review-path:*` scopes;
  `check --scope review-path:<repo>` reports that one. `probe-review-path`: a
  pinned call failed, or a pinned program or the pin directory is missing.
- `1` — `check`: no gate is recorded for the scope, so its state is unknown.
  `probe-review-path`: the probe cannot judge (contract missing or unparseable,
  `v` not 1, an unknown rule, or an `argv` that does not render); it changes
  nothing.
- argparse's own exit 2 on an unknown scope, an unparseable `--resets-at` or
  malformed CLI arguments.

**Writes.**
- `fleet.db`: the `quota_gate` table (one row per scope) and the
  `quota_transition` table (one row per close or open), both created on first
  use.
- One `quota-closed` or `quota-opened` event per transition, through
  `fleet-emit`, `detail` carrying scope, by, evidence, `resets_at` and
  `percent` when a meter read one. A write that changes nothing emits nothing.
- For `session` and `weekly` only, one page per transition through
  `bin/orchestrator`'s `notify()`: `fleet held: <scope> limit, until <time|unknown>`
  or `fleet released: <scope> reset (<by>)`.
- `probe-review-path` on exit 75 closes `review-path:<repo>` `by probe`, on 0
  opens it, on 1 writes nothing; the gate's `evidence` is
  `probe:<UTC timestamp>:<entry id>` and never stderr. `open review-path:<repo>
  --by owner --for <hours>` (1–168, default 24) also records an `override`
  transition: for that long a failing probe prints and pages but does not
  close the gate. `--for` is refused for any other scope, and validated before
  the gate opens.
- Stdout: `closed`/`opened`/`unchanged`, one line per gate for `status`, for
  `probe-review-path` the one-line result, and for `check` the line `closed <scope> until <resets_at or unknown>`,
  `open <scopes>` or `unknown <scopes>`.

### bin/fleet-record

declared

**Purpose.** Writes one change record per pull request merged to `main` of
a fleet repository into a local clone of `operator/recordstore`, then
pushes it over ssh. Runs as the recorder account, never the author,
reviewer or merger, and records what the forge shows without judging it.
Subcommands: `once`, `backfill --since DATE`, `publish`.

**Exit codes.**
- `0`: the pass completed; nothing to record is also `0`.
- `1`: an identity failure, a repository the recorder could not read, a
  record that failed schema validation, or a push still rejected after its
  one retry. Each also pages through `fleet-watch notify`.
- `2`: malformed flags.

**Writes.**
- `records/<owner>/<repo>/<YYYY>/<MM>/<merge_sha>.json` in the evidence
  clone: one JSON object per merge, shaped by `config/change-record.schema.json`.
  An existing record is never rewritten.
- `backfill/<owner>/<repo>.json` once a backfill of that repository finishes.
- Under `publish`: files from the attestation spool under `reports/**` and
  `protection/**`, never overwriting an existing path.
- One git commit per pass, pushed to `origin/main` of the evidence clone.
- Stdout: one line per record written.

### bin/fleet-release

declared

**Purpose.** Release a lease you hold. Refuses to release another
session's lease unless forced. `--expect-orphaned HOLDER` (plan 0179 D4,
ADR-0029 §3) is the check a caller that is NOT the holder uses — under the
lease's lock it re-reads the record and re-derives orphanhood, the same
re-derivation `fleet-claim --takeover` makes, and refuses unless the lease is
still orphaned and its holder is still `HOLDER`. `--reason TOKEN` is recorded
in the event; `TOKEN` must be exactly `plan-done` or `plan-abandoned`, a
closed allowlist enforced by argparse's own `choices`.

**Exit codes.** `0`: released, or idempotently already released (releasing
twice is not an error). `2`: a missing lease-id argument, a malformed flag,
or a `--reason` value outside the closed allowlist. `1`: no such lease
exists; the lease is held by a different session and force wasn't given; or
`--expect-orphaned` found the lease no longer orphaned, or held by someone
else, re-derived under the lock.

**Writes.**
- The lease's own record: sets its state to released, written under the
  lease's own lock. `--expect-orphaned`'s refusal writes nothing.
- Deletes the lease's orphan marker if present.
- Event `lease-released`, `detail` present when the release was forced
  (`forced`, `holder_was`, naming the previous holder), when `--reason` was
  given (`reason`), or both; absent when neither was given.
- Stdout: the lease id, on success.

### bin/fleet-repo

declared

**Purpose.** Subcommands (`list [--check]`, `enable`/`disable <owner/repo>
[--enforce] [--dispatch]`, `check <owner/repo>`) recording,
in a flat config file, which repositories the fleet is enrolled to act on
for two independent concerns: a local pre-push hook and eligibility for
automated merge dispatch. (There is no test-lane consent flag: every
repository has a tester, ADR-0013; a row still carrying the retired
`extlane`/`no-extlane` token is read without error and the token dropped on
the next write.) It never itself grants dispatch — it records
intent, and for the hook concern, triggers hook installation; a separate
live check decides dispatch eligibility at run time.

**Exit codes.**
- `0`: `list` with no config errors; `enable`/`disable` completing the
  requested flags; `check` reporting the repository eligible.
- `1`, grouped: `check`/`list --check` finding a protection rule that
  actively blocks dispatch eligibility; `list` when the config file had
  one or more unparseable lines (still prints what it could read);
  `enable --enforce` when no local checkout exists at the derived path,
  or the hook-installer subprocess exits non-zero; `enable`/`disable`
  called with neither flag named, or an owner/repo argument
  not in that shape.
- `2`: `check` (and `list --check`) when eligibility could not be
  determined at all (the resolver or its broker unreachable, a sealed
  credential store, a failed forge call) — deliberately never conflated
  with a refusal, since eligible/refused/undeterminable are three
  different answers.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- One line per enrolled repository in a flat config file, written
  atomically under a lock; a flag is written only when true.
  Any previously unparseable line is carried through verbatim rather than
  dropped on rewrite.
- `--enforce` shells out to a separate installer against the repository's
  local checkout; `fleet-repo` itself never touches the checkout's hook
  directory, only records success or failure.
- No forge object is created or modified — the dispatch/check paths only
  read a protection verdict.
- No event, no database table, no subprocess signal.
- Stdout: per-repository listing lines, a post-enable summary, and
  `check`'s eligible/refused/undeterminable line.

### bin/fleet-requeue

declared

**Purpose.** Hand a failed dispatch back to the watcher. Releases the plan's
dispatch lease only when it is the single live lease for the (repo, plan),
is dead, has a `plan-failed` event newer than its activation, and no marked
PR exists (and the forge could be read). Never spawns; has no force flag.

**Exit codes.** `0`: released, or `--dry-run` and every check passed. `1`: a
check refused (the message names which), or `fleet-release` failed. `2`:
missing arguments or malformed flags.

**Writes.**
- Via `bin/fleet-release --force`, the one writer: the lease's record set to
  released, its orphan marker deleted, and event `lease-released` with
  `detail` `{forced: true, holder_was}`, actor `EUNOMIA_SESSION` or
  `owner-<user>`.
- Nothing on `--dry-run`.
- Stdout: the lease id, the recorded failure reason, and a line saying the
  watcher re-dispatches under the cap.

### bin/fleet-rebase

declared

**Purpose.** Rebases an open pull request's head branch onto the base
branch in a fresh temporary clone and force-pushes it with a lease, so CI
runs again on the new head. Runs on one pull request by hand, or as a
sweep that acts on the owner's `rebase` comments. It refuses, saying which
check failed, when the pull request is merged, closed, not based on the
default branch or from a fork; when any undismissed approval exists; when a
live session lease holds the branch or its plan, or the lease ledger is
unreadable; when the branch already contains the base; when the rebase
conflicts; or when the rebase delta touches only plan files, because CI
would then skip the `CI / tests` suite and leave the new head without that
status; or when the rebase delta is empty.

**Exit codes.**
- `0`: the branch was rebased and pushed, would be (`--dry-run`), or was
  already current; a sweep that completed.
- `1`: a refusal, another instance holding the lock, or a sweep that could
  not read a repository or post a reply.
- `2`: a sweep started without its repository list or the owner's numeric
  id, or no token from the author-role helper.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- Force-with-lease push of the pull request's head branch only, never
  against an approved pull request and never a bare force.
- In sweep mode, one reply comment per owner request acted on, starting
  with a line `rebase: <comment id>`. Not under `--dry-run`.
- A non-blocking lock file in the fleet's lock directory.
- A temporary clone, removed on every path. It touches no shared checkout,
  pin or worktree.
- Reads the forge, the lease ledger, and the repository over ssh. Adds no
  event type and dispatches no review.

### bin/fleet-reviews

declared

**Purpose.** Builds and reports a table of code reviews in the fleet's
history database, backfilled read-only from the forge for a configured
reviewer identity — because the history database's own session records
can't reconstruct which pull request or repository a review was about.

**Exit codes.**
- `0`: success, whether run with `--backfill`, `--report`, both, or
  neither (neither defaults to `--report`).
- `1`, grouped: no history database exists yet at the configured path; the
  configured token-helper command failed, timed out, or produced no
  usable token; one or more paginated forge requests failed partway
  through a backfill (reported with a count and a sample of the failing
  requests, so a partial run is never silently reported as complete).
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- Database: creates (if absent) a review table and migrates in any
  missing columns on an older one; rows are upserted keyed on the forge's
  own review id, so a review's state can be corrected on re-run without
  duplicating rows. Plan 0197: `--backfill` now also reads each review's
  own body for talos plan 0042's `Review-provenance-usage:` line (tokens
  and seconds a reviewer's harness reported) and for its HIGH/MEDIUM/LOW
  finding counts, by the same two rules talos `file-followups.py` uses
  (its ```findings fenced block, exact, and its heading/bold-section prose
  tiers otherwise); both sets of columns fill once and are never
  overwritten on a later backfill, the same rule the session-id column
  already followed. A body that cannot be read this way stores every such
  column `NULL`, never `0`.
- No write back to the forge — every call is a read; no forge object is
  created or modified.
- Reads `config/model-prices.json`, an owner-maintained price sheet (plan
  0197), fresh on every `--report` call; a malformed file (missing
  `checked_on`/`source`, a negative price, or two rows for one model at
  the same `effective_from`) is caught and reported to stderr rather than
  raised, and the report continues with every cost shown as `—`.
- Stdout: `--backfill` prints a one-line count summary; `--report` prints
  a fixed-width table (repository, pull-request number, review rounds,
  approval count, finding counts by severity, an API-equivalent cost, and
  review time span), a totals line naming how many of the underlying
  reviews that cost excludes, a second fixed-width table of the same
  facts grouped by model, and a standing caveat about a field this table
  cannot reliably report.

`UNDECIDED:` a second table for pricing data (`model_price`) is created by
every run but nothing in this file ever reads or writes to it — plan 0197
answered the same need with a file instead (`config/model-prices.json`,
above), and this table remains declared as future-use schema only, now
with no plan expected to claim it.

### bin/fleet-snapshot

declared

**Purpose.** Takes a consistent point-in-time copy of the fleet's shared
database for its backup set, using the database engine's own "vacuum into"
mechanism — crash-safe against a live writer, and a separate schedule and
filename prefix from `fleet-db-snapshot`.

**Exit codes.**
- `0`: snapshot taken and verified; with `--verify`, also confirms backup
  tooling is present and configured.
- `2`: malformed flags.
- `1`: no database at the given path; a snapshot for the current second
  already exists; the new snapshot fails an integrity check (deleted
  before exit). Separately, `1` with `--verify` when the snapshot
  directory is not actually covered by any backup mechanism yet — a
  deliberate "true but unwelcome" report, not a tool malfunction.

**Writes.**
- Snapshot directory (default under the fleet's data area, overridable): a
  timestamped file built directly by the vacuum-into call (which itself
  refuses to overwrite an existing destination, so there is no separate
  partial-file stage).
- Retention: with `--keep N` (default 7; `0` means keep all, unlike
  `fleet-db-snapshot`, where 0 is rejected), deletes the oldest snapshots
  beyond the newest N.
- No event, no forge interaction. Encryption and offsite copying are
  explicitly out of scope, deferred to separate backup tooling.
- Stdout: a summary line (path, size, a row count, integrity status),
  pruned-file lines, and, with `--verify`, a small report on whether
  backup tooling is installed and configured.

### bin/fleet-followups

declared

**Purpose.** Meant to run hourly, on a per-repository timer
(`config/followups.conf`, hours; `-1`, the default for every repository,
is off): re-checks the open `Follow-ups from review of #N` issues
(implementer-authored, in enrolled repositories whose timer is due)
against `main`, at most `followup_triage_batch` per run, with one
read-only model session per issue, under the repo's `followup_triage`
seat (`bin/fleet-models`, plan 0186 — independent of `mediator`; falls
back to `sub-sonnet` when unset). It posts one wrapper-written triage
comment per issue and acts only on the owner's `close`, `keep` and `fix`
verbs. See `docs/followups.md`.

**Exit codes.** `0` on a run (including a paused one, one with every
repository off or none due, which reads nothing from the forge, and one
that lost the lock), `2` on a refused runner, a bad timeout or no token.

**Writes.** Issue comments; on the owner's `close`, ticks the `gone`
items and closes the issue. `followup-triaged` and `followup-closed`
events, and one page per run that triaged anything. After a completed
run, the time of that run for each repository it covered, in a small
state file under the fleet directory (a refused run records nothing, so
the timer stays due). It opens no pull request and edits nothing without
the owner's verb.

### bin/fleet-stalled

declared

**Purpose.** Meant to run on a schedule: scans every enrolled repository
and pages once for each open pull request stuck unreviewed (CI green,
never reviewed), with a stale review (a review predates the current
head), with a stale-base failure (CI red only because the base branch
moved on and is itself green), or genuinely red. It never rebases,
dispatches a review, or comments — it only detects and pages. A sixth
state, `failed-plan` (plan 0194), is per PLAN rather than per pull
request: a `status: ready` plan on main whose newest `fleet.db` dispatch
is terminal with a non-`merged` outcome, has no live dispatch lease
(fleetlib's TTL/heartbeat verdict, never the lease's `state` field), and
has no open pull request a live steward still owns. It is named with
`fleet-requeue`, a `respawn` comment, or a status decision, depending on
whether a marked pull request exists and what state it is in — this tool
never runs any of the three itself.

**Exit codes.**
- `0`: clean scan, nothing stalled, nothing unreadable.
- `1`: at least one stalled pull request or failed plan found, no scan
  trouble.
- `2`: one or more scan troubles occurred (takes priority over reporting
  findings) — a bad configuration row, a token-helper failure (the whole
  scan is then skipped), a per-repository mode that is named but not yet
  built (refused explicitly rather than silently treated as the default
  mode), an unreadable/truncated read of pull requests, reviews, or
  commit statuses, or (for `failed-plan`) an unreadable `plans/` listing
  or an unreadable marked-pull-request dedupe scan. A `fleet.db` that is
  absent or lacks the `dispatch` view (eunomia plan 0036) is NOT a
  trouble — a normal state on a host whose pin has not advanced —
  `failed-plan` simply finds nothing there.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- A dedupe/state file recording pages already sent — per-head
  (repository, pull request, head commit) for the five pull-request
  states, so a new push re-pages and an unchanged head stays silent; per
  (repository, plan id, newest dispatch lease, UTC day) for `failed-plan`,
  so it re-pages at most once a day until the plan is settled — and scan
  troubles already paged once per reason per day. Written atomically; not
  written under `--dry-run`. A stored page entry only satisfies dedupe
  once `delivered` is true; an entry left `delivered: false` (the
  notification channel was down) is retried on the next run rather than
  treated as sent, for every state.
- One push notification per new finding and per new trouble, through a
  configurable channel if set, recorded either way to the fleet's shared
  page log.
- Reads from the forge (commit statuses, pull-request lists, review
  lists, and, for `failed-plan`, the plans tree on main and marked
  pull-request listings, through `bin/fleet-watch`'s own functions); no
  forge object created or modified. Reads `fleet.db` (`$FLEET_DB`, default
  `$EUNOMIA_FLEET_DIR/fleet.db`) read-only for `failed-plan`; never writes
  it. Reads dispatch-lease records on disk read-only, through fleetlib,
  for `failed-plan`'s liveness check; never claims, releases, or touches
  one.
- One subprocess: the configured token-helper command.
- Stdout: a scanning summary line, then one line per finding. Stderr: one
  line per trouble, and a refusal line for any repository configured to
  an unbuilt mode.

### bin/fleet-status

declared

**Purpose.** Primarily a read-only report: joins lease records with
liveness state and derives which active leases are orphaned. Never reaps,
releases, or reassigns anything — orphaning is reported, not acted on; the
one state it does write is a once-only "this orphan has been announced"
marker.

**Exit codes.** `0`: normal completion with no event-emission failures.
`2`: malformed flags. `1`: printed after the full report is produced, if
one or more of this run's own event emissions failed — a broken event
ledger degrades awareness, not the report itself.

**Writes.**
- An orphan marker for a lease, created the first time it is observed
  orphaned (gating a one-time event), recreated if the holder was later
  seen alive and has since gone quiet again (a new orphaning), and
  deleted again on a genuine heartbeat resume so the next real death
  re-announces.
- Event `lease-orphaned` on each newly observed orphan, under the lease's
  own lock so a racing takeover cannot land between the marker write and
  the event.
- With `--budget`: for every active budget-type lease, compares its
  declared token allotment against actual usage read from the fleet's
  database, and emits one `budget-checkpoint` event per lease (advisory
  only, never a refusal).
- Stdout (or `--json`): a table, or object, of lease id, resource type,
  state, holder, orphaned flag, last-known-alive time, TTL, creation
  time, and note; with `--budget`, each budget lease's used and allotted
  token counts and where that usage figure came from.

### bin/fleet-watch

declared

**Purpose.** The plan dispatcher: on each poll cycle, scans a configured
set of repositories on their default branch for feature plans ready to
build with no dispatch already in flight, confirms the default branch is
unforgeable by an automated account, and spawns one implementer process
per eligible plan up to a concurrency cap. Also reports drift in pinned
worktrees and surfaces dead dispatch processes. Deliberately mechanical:
it never edits a repository and never runs a model itself — everything
judgment-shaped is delegated to the implementer it spawns. Runs either
one cycle per invocation (the default, meant for a periodic scheduled
job) or as a resident loop (`--resident`) cycling on a configurable
interval.

**Exit codes.**

One-shot invocation:
- `0` — normal completion in every case: a full cycle whether or not
  anything was dispatched, `--dry-run`, `--pins-only`, and every
  early-return refusal inside a cycle (a missing or unusable implementer
  entry point, an unreachable protection check, no forge token, an empty
  repository list) — all reported to stderr and/or a push notification,
  never by a nonzero exit. `UNDECIDED:` whether "this cycle could not
  scan" should ever be distinguishable by exit code from "this cycle
  scanned and dispatched nothing" — today it never is. A second
  concurrent instance, detected via a lock file, also exits 0 after a
  warning, by design (one watcher, one cap, no second scheduler).
- `1` — configuration validation failures (an unrecognised verdict
  source, a malformed request timeout, a malformed page-margin setting,
  an unrecognised spawn mechanism, a cycle interval outside its allowed
  range), each read live rather than once at startup, so this can happen
  at any point in a cycle; and, in `--resident` mode only, an inability to
  acquire its own singleton service lease at startup.
- `2` — argparse's own exit on an unrecognised flag or bad usage.
- `UNDECIDED:` in one-shot mode, an exception raised inside a cycle is
  **not** caught — it propagates as an uncaught exception (exit 1, full
  stack trace on stderr). This is inconsistent with `--resident` mode,
  where the equivalent exception is deliberately caught and logged so the
  scheduler survives; nobody appears to have decided whether a one-shot
  run crashing loudly on a bad cycle is intended or incidental.

Resident mode does not normally exit: it loops until a termination signal
(finishes the in-flight cycle, releases its service lease, exits 0 — never
mid-cycle, so it never drops an in-flight dispatch as an orphan) or until
it detects its own code has moved to a new, non-diverging commit and
nothing it spawned is still running, at which point it deliberately stops
so an external supervisor relaunches it on the new code (also exit 0). A
cycle-body exception in this mode is caught, logged, and the loop
continues.

**Writes.**
- Lease records for each dispatched plan and for its own resident-mode
  singleton lease, created and released by shelling out to the lease
  tools described above — this program decides when and what, the lease
  tools own the record shape. This includes releasing an orphaned dispatch
  lease (implementation or test-lane) once its plan reads `done` or
  `abandoned` on `main` (plan 0179 D2, ADR-0029) — through
  `fleet-release --force --expect-orphaned`, recording `plan-done` or
  `plan-abandoned` as the release's reason. A lease of a plan still `ready`
  is never released here, whatever its pull requests say.
- One-shot notification markers: empty marker files, created once per
  distinct condition (a real lease id, or a synthetic key for a condition
  like a rejected token, an unreachable broker, or a blocked plan),
  deleted on recovery so the same condition can page again later. A
  periodic sweep also deletes any marker whose apparent lease is no
  longer live.
- Blocked-plan state files: one short line of plain text recording the
  unmet dependency and reason, used only to gate re-emitting the
  `plan-blocked` event on a transition, deleted when the plan is no
  longer blocked.
- Pin-drift state files: one JSON object per watched pin, recording its
  state, head commit, first-seen time, and whether it has been paged.
  Written only on a state transition, never under `--dry-run`.
- A session liveness file (resident mode only), touched once per cycle.
- Its own process lock file — a pure mutex, holds no data.
- Resident mode only (plan 0187): between cycles, rotates its own
  `watch.log` (the file `StandardOutPath`/`StandardErrorPath` both name)
  once it is about seven days old, to a dated, gzipped weekly archive under
  `logs/archive/` beside it — `logs/archive/watch-<start>-<end>.log.gz`,
  both dates UTC and inclusive, `-2`/`-3`/... appended on a name collision.
  Every archive is kept; nothing here prunes one. Publication is atomic: a
  `.`-prefixed, non-`watch-*.log.gz`-matching partial name is fsynced and
  checked with `gzip -t` before the final rename, and the uncompressed
  intermediate is deleted only after that rename succeeds, so a crash mid-
  gzip leaves either the intermediate or a recoverable partial, never a
  truncated archive. Reopens its own stdout/stderr onto a fresh file at the
  active path immediately after the rename, so the inherited descriptor
  launchd handed it at start never keeps writing into the now-archived
  inode. A `.partial` left by an interrupted rotation is cleaned up and its
  gzip redone on the next attempt, independent of whether a new rotation is
  due that cycle.
- Reads, and (outside `--dry-run`) deletes, the wake spool `bin/fleet-depsd`
  writes (plan 0181): one small JSON file per `(repo, plan)` whose last
  dependency hold just cleared, named by a content hash so a burst of writes
  coalesces. A matching file is removed before its plan is dispatched
  (consume-once); under `--dry-run` nothing is removed, matching every other
  mutation this program skips in that mode — a dry run must not spend the
  one real wake an armed rerun needs. A file this program cannot parse is
  removed with a warning (even under `--dry-run`, since it will never parse
  differently) rather than left to jam every later pass. A wake is a hint,
  never an instruction — this program re-checks every gate (the quota hold,
  the cap, the protection verdict, main's own health, dependencies, overlap,
  dedupe, the review path and the human-merge gate) before acting on one,
  the same as it would on its own next cycle.
- Events (appended to the fleet's shared event ledger): `plan-dispatched`
  on every successful spawn (detail: plan id, session, branch, lease,
  spawn mechanism); `plan-failed` with a reason of "lease assign failed"
  or "spawn failed"; `plan-blocked`, emitted only on a transition in the
  blocking reason, never once per cycle (detail: plan id, the unmet
  dependency, its form, and the reason — plan 0180 adds `unattested`,
  `not a slug` and `store unreadable` for an `external:<slug>` entry, and
  `unmet`/`dangling`/`abandoned`/`unreadable` under form `plan` for a
  `plan:<owner/repo>:<id>` entry); `pin-drift`, with the pin's path,
  ref, head, target, and its state transition.
- A push-notification attempt log: one row per attempt, delivered or not,
  appended to the fleet's shared page log, whether delivery succeeds,
  fails, or (when no push channel is configured) degrades to a single
  stderr line — which is still treated as delivered, so an unconfigured
  watcher does not repeat the same page every cycle forever. `UNDECIDED:`
  this means a deployment that never configures a push channel gets every
  page exactly once, ever, with no way to rediscover it short of clearing
  the marker file by hand.
- Stdout: one line per dispatch, per dry-run decision, per concurrency-cap
  change, per reaped stale job record, and a per-cycle summary. Stderr:
  every degraded/refused/blocked/undetermined condition, one line per
  occurrence.
- Forge objects: none created or modified — every forge call this program
  makes is a read (branch protection, plan file contents, pull-request
  list for dedupe, issue comments for a respawn request). It reads
  pull-request and comment bodies but never writes them.
- Database: reads (never writes) a settings table to resolve the dispatch
  concurrency cap, opened read-only, falling back to a configured default
  if unset. Also reads (never writes, never caches) `dependency_attestation`
  for each unmet `external:<slug>` entry, opened read-only (plan 0180); a
  missing database or table reads as unmet, never met.
- Subprocesses it runs: a token-helper command to mint a forge API token
  (its output is never logged); read-only git commands for worktree/pin
  inspection (never a push, checkout, or reset); a process-table read
  before a self-restart; the lease-grant and lease-release tools
  described above; a readiness probe against its implementer entry point
  before arming any dispatch; and the actual dispatch itself, launched
  either as a detached background process or registered as a supervised
  job with the host's own process supervisor — not a bare background
  process, because an orphaned one cannot open outbound network
  connections on this platform.

**What it explicitly does not do:** move or re-point a pin, act on
staleness itself, run a model, or write anything to the repositories it
scans.

### bin/mopsus

declared

**Purpose.** Triage for a plan dispatch that failed: sweeps newly failed
leases, classifies each failure, and — mode permitting — issues a short
"handle" that, when given back to this program, opens an interactive
session pre-loaded with a brief describing the failure and candidate next
actions. Reads a per-repository mode from a config file (`escalate`,
`mechanical`, `triage`, or `off`); only `escalate` is built today.

**Exit codes.**
- `0`: a sweep with no unbuilt-mode refusals; `mopsus list` with or
  without recorded handles; `--format json` printed successfully for a
  known handle.
- `1`, grouped: a handle given on the command line resolves to no held
  brief (never issued, or since rotated away); `--format json` given a
  handle that resolves to no known failure.
- `2`, grouped: the mode-config file is unparseable, or names a mode
  outside the closed set for any repository or the default; a plain sweep
  (no handle given) found a newly failed lease under a mode that is named
  but not built — refused, though every `escalate`-mode failure in the
  same sweep is still paged before the refusal; argparse's own exit 2 on
  a bad invocation.
- Opening a handle with a brief replaces the mopsus process with a
  configured interactive program, handing it the brief text — the
  real-world exit code from that point on is whatever that program
  produces, not one mopsus itself chooses.

**Writes.**
- A lease-to-handle map plus a handle counter, rewritten atomically under
  a lock.
- One file per issued handle, holding its brief text, written once and
  never overwritten.
- Pages (one per newly failed lease, or a single summary page on a fleet
  area's first-ever sweep) through the same shared notification and
  page-log mechanism `bin/orchestrator` provides.
- Reads (never writes) the fleet's shared database, issuing only reads.
- Never touches the forge — the only forge-shaped output is two read-only
  links (a generic deep link and the direct pull-request link) shown to a
  human inside a brief when a failed lease left one.
- No event emitted; no database table written — a pure reader of tables
  `bin/fleet-collect` fills.

**Shape.** A brief: fixed fields (repository, plan id, lease, branch,
failure reason, process exit code, whether it timed out, elapsed time,
pull request if any, run-log and transcript paths), the tail of the run
log, and — once classified — a numbered list of candidate actions with
their cost. `mopsus list` stdout: one line per handle.

`UNDECIDED:` `mopsus list` is special-cased ahead of argument parsing, so
trailing tokens after `list` are silently ignored rather than rejected,
unlike every other subcommand.

### bin/orchestrator

declared

**Purpose.** A deterministic wrapper that carries one feature plan through
its whole dispatch lifecycle: workspace setup, building a prompt from the
plan file, spawning an implementer session, verifying a marked pull
request exists, running a bounded review-and-fix loop, then stewarding the
approved pull request — taking instructions from its owner through
comments and enforcing a post-approval freeze — until it reaches a
terminal state, then releasing the lease. Invoked by the watcher with a
repository, a plan path, and a lease id, or with a single readiness-probe
flag the watcher polls before it will arm at all.

**Exit codes.**
- `0`: the readiness probe when nothing is left unbuilt; the only success
  outcome of a real dispatch — the review loop reached approval, the pull
  request later reached a terminal forge state, and the lease was
  released.
- `1` — every one of these funnels through one handler that writes a
  `plan-failed` event, pages a human best-effort, and warns to stderr,
  covering: workspace setup failing after several retries; the plan file
  unreadable from the checked-out worktree; the plan's raw text matching
  a credential-shaped pattern (refuses to build a prompt from it at all);
  routing unresolved (an unrecognised initiator, no runner configured for
  this repository or a fleet default, a named runner that doesn't exist,
  a credential-kind mismatch, a plan zone routed to the wrong class of
  runner, a missing or wrong-shaped lease, an unreadable prompt preamble,
  a missing or invalid wall-clock bound); branch/session setup refused
  (harness not available; a runner declared but not actually wired up;
  the branch already existing on the remote; a local-only branch that
  won't clean-delete); an unexpected exception while spawning or running
  the implementer session; verification of a marked pull request unable
  to even start (token-helper failure, a malformed timeout setting); an
  unexpected exception during verification; the implementer session
  hitting its wall-clock bound (terminated; a marked pull request found
  afterward is explicitly not accepted as a late success); no verified
  marked pull request found once the session finished (covering: the
  pull-request list unreadable after retries; no pull request with this
  lease's branch; wrong author; wrong base branch; the required marker
  line missing and this program's own attempt to add it failing; the
  plan's status at the pull request's head not `done`); the review loop
  unable to start, or raising unexpectedly; the review loop finishing
  without approval (bound exhausted, or any single round failing for an
  unreadable head, CI never resolving or becoming unreadable, the
  external review dispatcher failing or timing out, reviews unreadable, a
  reviewer exiting cleanly but posting no review at the reviewed commit, a
  non-approving verdict carrying no readable blocking finding, a fix
  session refused/erroring/timing out, or a fix session pushing no new
  commits); and — the one case that returns `1` directly rather than
  through the shared handler — the steward unable to release the lease
  after the pull request reached a terminal state, which pages but does
  **not** emit a `plan-failed` event, since it is treated as an operator
  problem rather than a failed plan. Every exit above, once the lease
  activated, passes afterward through a last settling step (plan 0179 D1,
  ADR-0029): it reads the plan file on `main` and, when that reads `done` or
  `abandoned`, releases the lease there too — recording the fact as the
  release's reason — rather than leaving it held. A lease that would not
  release at that point pages (but, like the steward's own case, emits no
  `plan-failed`) and the run's own exit code is unchanged either way.
- `2`: wrong number of positional arguments, or no session identity set
  (both warn-only, logged/emitted/paged); the lease failing to activate
  (this one does log, emit, and page).
- `UNDECIDED:` two failure sites — a malformed lease id caught at the very
  start of the run, and an invalid evidence-path check re-raised rather
  than handled — are not routed through the shared failure handler at
  all, and escape as an uncaught exception (exit 1 in practice, but
  without a logged/emitted/paged failure). This contradicts this file's
  own stated invariant that every refusal goes through exactly one place.
- `UNDECIDED:` a fully successful run never emits any event from this
  file — the only event type this file ever emits is `plan-failed`.
  There is no positive counterpart, so nothing in the event stream
  signals a plan's success from this program.
- `UNDECIDED:` the readiness probe only inspects its first argument, so
  extra trailing arguments are silently accepted rather than rejected.

**Writes.**
- A structured, per-lease run log (path shape keyed by lease id), opened
  in append mode before the lease is even activated: one line per fact,
  timestamped and phase-tagged, passed through a redaction pattern set
  before being written. Its own open failure is swallowed, never fatal.
- An ssh handshake trace file, same path with a different suffix, written
  only from the second workspace-setup retry onward — verified by its own
  author to carry no private key material, password, or token.
- A session transcript per spawned session (directory kept deliberately
  outside the fleet's own ledger tree; refuses a configured directory
  that resolves inside it), one file per session, written once and never
  appended to, holding the session's whole merged output, redacted as one
  block using a wider rule set than the log/comment redactor —
  deliberately, so a credential split across two read chunks is still
  caught.
- An append-only page-attempt log, shared fleet-wide, one row per
  notification attempt whether or not delivered.
- The worktree's own local git identity configuration (best-effort; a
  malformed value just leaves it untouched).
- Forge objects: never creates a pull request, issue, or label, and never
  posts an approving or any other review — asserted by this program's own
  design and covered by a test asserting zero approval calls; all pull
  request creation is left to the spawned session. The final lease-settling
  step (plan 0179 D1) reads the plan file's contents at `main`, and nothing
  else — never a pull request — to decide whether to release. It edits an existing
  pull request's body exactly once, appending (never replacing) a plan
  marker line, only after independently verifying the pull request is
  this run's own. It posts several fixed shapes of issue-thread comment:
  a machine-readable review-round ledger after every review loop; a
  deferred-findings comment after an approval; acknowledgement comments
  for owner instructions, freeze-exception grants, and any other comment
  it observed but did not act on (quoted back, explicitly marked
  not-acted-on); and assorted plain-text status comments (follow-up
  routed/opened/failed, session-timeout notices, a frozen-branch-moved
  alert, a re-review outcome, a worktree-checkout failure). All comments
  are redacted before posting.
- A spec-regression-gate checklist comment (plan 0191), posted whenever its
  computed state differs from the state of the newest such comment already
  on this exact implementation head (so a state that recurs at a head is
  reposted, not silently skipped), on an implementation pull request whose
  plan's own `paths` lease reaches a `spec_watch` rule from the target
  repo's `governance.yaml` — a plan reaching none gets no comment at all.
  `passing` is `fleet-meet`'s own `all_passed`, never re-derived from
  per-test outcomes alone: a refused or timed-out lane, a dispatched lane
  never read, and an empty suite are all evidence against a pass, even
  though none of them is itself a non-passing test. It opens with
  `<!-- eunomia:spec-regression-gate plan=<id> head=<sha12> state=<state> -->`
  and `state` is exactly one of `missing`, `stale`, `failing`, `passing` or
  `waived`; see `docs/spec-regression-flow.md`. It carries the sha256 of
  every spec document the pull request's own diff touched, the mediator's
  identity when `fleet-triage` has already run, and, when waived, the
  waiving owner's login and reason. It is never a commit status and never a
  review, it asserts the same way `fleet-triage`'s own comment does that it
  carries no `Ack:` line, no `Plan:` line, and no second `eunomia:` marker,
  and it never spawns `fleet-meet`, `fleet-triage` or an implementer
  session — it only reads their already-written `findings.json` and
  `triage.json`, and the thread, for an owner-authored two-line
  `Spec-gate-waive: <reason>` / `Head: <sha>` exception scoped to the exact
  head.
- Event `plan-failed` only, `detail` carrying the plan id, lease id,
  reason, and optionally a log/transcript reference, pull request number,
  or round count. A failed emit is swallowed, never blocking the exit
  path it is attached to.
- Database: writes none of the fleet's own tables directly; the lease
  record itself is only touched by spawning the separate lease-activation
  and lease-release tools.
- Subprocesses: the implementer/fix/instruction session harness itself,
  given the whole prompt on standard input only, never on the command
  line, run in its own process group so a timeout can be swept including
  any grandchild, and killed by an escalating signal on both timeout and
  normal exit; a background sampler tracking that process group's peak
  memory, recorded only into the run log; the lease-activation and
  lease-release tools, re-invoked as separate processes; read-only git
  plumbing in the worktree (never a push — pushing is left to the spawned
  session's own credentials); a token-helper subprocess for the one
  "authoring" credential this whole program uses for every forge call; an
  optional push-notification subprocess and HTTP call, best-effort,
  recorded to the page log regardless of outcome; and an external review
  dispatcher command, given the full parent environment (unlike the
  tightly restricted session child) plus one added variable naming a
  reviewer-mandate file — this subprocess is the one that actually holds
  the reviewing credential and posts the review; this program never sees
  that credential and never reads a verdict from the subprocess's own
  output, only from the forge afterward.

**Shape.** Stdout: none, ever — every diagnostic goes to stderr, and in
normal operation the watcher discards this program's stderr, so the run
log and the page record are the only channels that survive. Pages use a
handful of fixed titles covering the failure classes above. Run-log lines
are single-line space-joined facts, never multi-line structured data — a
deliberate distinction from the transcript, which is the session's raw,
unsummarised output. Every pull-request comment this program writes is
markdown; the two "ledger" comment types carry a machine-readable anchor
so a later run can parse its own prior state back out of the thread
rather than relying on any local file.

### bin/fleet-collect

declared

**Purpose.** The collector: ingests local and remote sources into the
fleet's shared database — session transcripts (Claude's own, and, plan
0197, every other writer family's own raw output), the event ledger,
per-lease run logs, and (by default only against the live fleet area) CI
task data from the forge and a remote log archive.

**Exit codes.**
- `0`: the sweep completed with zero per-file or per-source ingest
  failures, and no failure reconstructing the settings table.
- `1`, grouped (all converge on the same final code, so the cause is not
  distinguishable from the exit code alone): one or more transcript files
  failed to parse or ingest; one or more writer transcripts could not yet
  be correlated to the run log that names the runner and model that
  produced it (plan 0197 D1 — retried on the next sweep, not dropped);
  one or more event-ledger files (current or rotated) failed to ingest;
  one or more run-log files failed to ingest; the settings-table
  reconstruction step raised while replaying ledger events; the CI source
  raised outright, or reported one or more individual log-fetch failures;
  the target database's schema version is newer than this build
  understands — refused unconditionally before any source is scanned.
- No argparse (its only input is an optional positional database path),
  so there is no argparse exit-2 path.

**Writes.**
- The database at the given path, or a default path under the fleet's
  data area, created with parent directories if absent.
- Owns and fills tables for: session summaries, individual conversation
  turns, tool calls, per-source ingest bookkeeping (path key, size,
  modification time, ingested-at, last error), a mirrored copy of the
  event ledger, and the run-log phase stream; on the CI path, also the CI
  task/log/sync tables described under `bin/fleet-ci`. Two derived views
  are dropped and recreated every run.
- Also fills the settings and settings-change-audit tables the first time
  they are absent or empty, by replaying `fleet-setting-changed` events
  just ingested.
- Reads three local sources under the fleet area it is pointed at (a
  transcript tree, the event ledger, and per-lease run logs); a fourth
  local source, the orchestrator's own writer-output tree (plan 0197 D1),
  runs under the same default-area gate as the CI source below — a
  relocated fleet area (every test's own) reads nothing there unless told
  to, so a test cannot read the real tree of the host running it; a fifth,
  network source runs by default only against the live fleet area (or
  when explicitly enabled): the forge's task list over HTTPS, and one
  remote call per sweep to fetch a compressed CI-log archive, decompressed
  locally through a standard compression tool.
- No forge object is ever created or modified — every remote call is a
  read; no event is emitted — this program only derives from the ledger,
  never adds to it.
- Stdout: an ingest summary, the database path and size, a CI summary
  when that source ran, and settings-reconstruction status. Stderr: one
  warning line per failed file or source, naming the key and continuing;
  the first several individual CI task failures are also listed.

**Shape.** Every stored free-text field that could carry sensitive
content (a run-log fact blob, an event detail blob, a tool call's target)
is redacted before storage, and a short "target" label for a tool call is
kept deliberately narrow — a file path, a shell command's verb only
(never its arguments), or a subagent's type and description.

### bin/fleet-install-hooks

declared

No argparse — this program hand-parses a single required repository path;
usage errors take the same `sys.exit`-to-stderr path as every other
refusal, not a distinct argparse code.

**Purpose.** Installs the fleet's pre-push git hook into exactly one
target repository (handling a linked worktree correctly, not just a plain
clone). Meant to be run once per repository by a human or a coordinator —
never by CI.

**Exit codes.**
- `0`: hook installed; already installed and byte-identical (a no-op);
  or existing unrecognised content preserved as a backup and the new hook
  installed alongside it.
- `1`, grouped: wrong argument count; the path isn't a git repository, or
  is a bare one (hooks belong where a push actually originates); the
  repository's hook path is configured somewhere non-default (refuses
  rather than write a hook git would silently never run); the hooks
  directory can't be resolved; the companion pre-push script expected as
  a sibling file is missing; the target hook path is itself a symlink
  (refuses to write through it); an existing hook file doesn't carry this
  program's own marker (refuses to overwrite a hook it didn't author).

**Writes.**
- The hook file itself: a small wrapper carrying a fixed marker comment,
  made executable. The wrapper checks its runtime and companion script
  are both present, and fails open (warns, allows the push) if either is
  missing, rather than blocking pushes on a broken toolchain; otherwise it
  runs the companion script and maps exactly one of its exit codes to a
  blocked push, warning and allowing on any other nonzero exit.
- If a marked-but-different hook already exists, the previous file's raw
  bytes are preserved first, at a backup path chosen to never collide
  with an earlier backup, before the new hook is written.
- Stdout: an installed / already-installed / backup-taken line.
- No event, no forge interaction, no database write — this program only
  ever touches the one repository's local hooks directory.

### bin/fleet-pkill

declared

No argparse — this program hand-parses its arguments; usage errors take
the same `sys.exit`-to-stderr path as every other refusal, not a distinct
argparse code.

**Purpose.** Three modes: `--register <pid>` records a pid, at spawn
time, as belonging to the caller's session; a bare substring signals every
currently matching process the caller can prove it owns; `--session <sid>
--reason "<text>" [--killer <value>]` is a cross-session, human-triggered
kill for a session that has stopped responding to itself. Ownership in
the default mode is proven either by the process being a live descendant
of the invoking shell, or by a prior registration matched against the
process's start time (which survives process-title rewriting and still
catches pid reuse). All signalling is a termination signal only; this
program never touches a lease.

**Exit codes.**
- `0`: a registration succeeds; a default-mode kill signals or finds
  already-gone at least one owned, matching process; `--session` mode
  signals (or finds already-gone) the target and successfully records the
  outcome.
- `1`: a default-mode kill matches nothing owned (no match, every match
  unproven, or every signal attempt met a permission error); every other
  refusal below.
- `3` — `--session` mode only: the target was successfully signalled (or
  already gone), but recording the outcome to the event ledger then
  failed — the kill happened but is unrecorded.
- `1`, grouped by cause (`sys.exit`): usage errors; `--register` given no
  valid session identity, a non-integer pid, an orphaned invoking shell
  (refuses to treat the process supervisor as an ownership root), a pid
  that isn't currently a live descendant, or a pid that vanished before
  its start time could be captured; the process table unreadable;
  `--session` mode — a reason exceeding a fixed size cap, a reason
  matching a secret-shaped pattern (checked before anything is signalled),
  a malformed session id, no record of that session, no host binding for
  it, an unreadable binding file, the bound pid no longer alive, the
  bound pid alive but its start time no longer matching (pid reuse,
  refuses rather than risk signalling an unrelated process), the pid
  bound to more than one session at once (refuses rather than guess), the
  target resolving to the caller's own session (this mode is
  cross-session only, by design), or no permission to signal the target.

**Writes.**
- One registration file per registered pid (path shape under the
  session's own area of the fleet's state tree), holding the process's
  start-time string. A later default-mode kill (or another registration)
  prunes entries whose pid is no longer alive or whose start time no
  longer matches, as a side effect of the ownership check.
- `--session` mode appends one `session-killed` event to the fleet's
  shared event ledger, `detail` carrying the target session, pid, the
  operator-supplied reason, the attributed killer, and how that
  attribution was determined. It never touches the target session's own
  lease or ledger files.
- No forge object, no database table. The only subprocess is a read of
  the system process table.
- Stdout: a registration confirmation; a kill/already-gone line per
  owned match, naming only the executable's base name, never its full
  command line, so a secret embedded in a command line is never echoed.
  Stderr: one line per matched-but-unowned process, permission-denied
  lines, and a note when the invoking shell has already exited.

### bin/fleet-sim

declared

No argparse — this program hand-parses its arguments; usage errors take
the same `sys.exit`-to-stderr path as every other refusal, not a distinct
argparse code.

**Purpose.** Gates booting and shutting down iOS Simulator instances so at
most a fixed small number are booted on a host at once, and every booted
simulator is attributed to the fleet lease that asked for it. Refuses at
capacity or on conflict; never shuts down another session's simulator to
make room.

**Exit codes.**
- `0`: `boot`/`shutdown` completed, including idempotent cases — booting
  an already-booted target just confirms the binding, shutting down an
  already-shut-down one still clears it.
- `1`, grouped: wrong argument count or verb, or a malformed target
  identifier; no active simulator-type lease held by this session on this
  host to boot against; the target already bound to another session's
  active lease (refused on both boot and shutdown — this program never
  reclaims another session's binding); every simulator lease this session
  holds already bound to some other target (refuses to silently rebind
  and orphan the old attribution); simulator enumeration failing (refuses
  to boot blind rather than guess capacity); at capacity (lists what's
  currently booted and, where known, which session holds each); the
  underlying boot/shutdown call itself failing (a report of "already
  shut down" is treated as success, not an error).

**Writes.**
- No files of its own; mutates the matching lease's own record, under
  the lease's own lock, to bind or clear a booted simulator's identifier.
- Also holds a whole-host lock for the duration of a boot, so two
  sessions racing to boot the same target can't both pass the
  conflict check and double-bind it.
- Calls the platform's own simulator control tool to actually boot or
  shut down — a real side effect outside the fleet's own files.
- No event, no forge interaction, no database write.
- Stdout: the target identifier, on success.

### bin/fleet-style

declared

**Purpose.** The one decision on whether a pull request takes the style review
(ADR-0025 §3) and whether the screenshots committed for it are valid evidence
(§6-§7). It reads `config/style-review.json` beside itself and changes nothing.

**Exit codes.**
- `0`: `declared`, `route` and `verify` decided, including "the repository is not
  declared", "routed to the full review" and "the evidence is not ok".
- `1`: a decision could not be made (an unreadable input, a failing git call, a
  value that is not a commit hash). A caller treats this as the full review.
- `2`: `declared` found the declaration file malformed, naming the line and the
  rule; also argparse's own exit on malformed CLI arguments.

**Writes.** None. `route` asks `fleet-models reviewers` for the ordinals and
`verify` reads the producer's record under the fleet directory; neither is
modified.

**Shape.** `route` prints `position`, `review`, `reasons`, `evidence_id`,
`evidence_folder` and `screenshot`; `verify` prints `ok`, `folder`, `code_head`,
`problems`, `images` and `total_bytes`. Both are specified in `docs/style-review.md`.

### bin/fleet-style-evidence

declared

**Purpose.** Produces the screenshots a style pull request carries (ADR-0025
§6): photographs the base and the head with the repository's declared command,
run from the base commit, commits the images to the pull request branch in one
commit after the code, records their hashes outside every repository, pushes,
and writes the before/after grid into the marked section of the pull request
body. Specified in `docs/style-evidence.md`.

**Exit codes.**
- `0`: `produce` completed and the body section was verified by reading it back.
- non-zero: any other outcome; the message names the step that stopped.

**Writes.** The evidence folder commit pushed to the pull request branch, the
producer's record under the fleet directory, and the marked section of the pull
request body. It creates and deletes its own simulator and holds a `sim` lease
for the run.

### bin/fleet-svc

declared

No argparse — this program hand-parses its arguments; usage errors take
the same `sys.exit`-to-stderr path as every other refusal, not a distinct
argparse code.

**Purpose.** The sole sanctioned path for starting, stopping, killing, or
reconfiguring a supervised process-supervisor unit, gated on holding an
active service-type lease for that exact unit, and failing closed if it
cannot reach the lease store. Verbs: `status`; the mutating verbs
`start`/`stop`/`restart`/`kickstart`/`bootstrap`/`bootout`/`enable`/
`disable`; `kill [--signal SIG]`; `edit-config [--sync-sha]`; a read-only
`audit` reconciling what is shipped, what is enrolled, and what the local
supervisor actually has loaded — `audit` never installs, loads, repairs,
or enrols anything.

**Exit codes.**
- `0`: `status` always (an unreachable lease store degrades to a
  warning, not a refusal); a mutating verb, `kill`, or `edit-config`
  completing its operation — note this is `0` even when the follow-up
  write of the outcome into the lease record itself fails (see
  `UNDECIDED:` below); `audit` when nothing in scope has a problem.
- `1`: `audit` when at least one in-scope unit has a problem (a reporting
  code, not necessarily a failure of this run); every refusal below.
- `1`, grouped by cause: usage errors; an unreadable enrolment config, or
  one with a row that fails to parse; a malformed unit identifier, or one
  naming a host other than the machine this is running on; a unit not
  enrolled at all; an unrecognised supervisor-domain value configured for
  a row (refused when the enrolment config is read, for every verb
  including `audit` — `system|gui` for launchd, `system|user` for
  systemd); the lease store unreachable (fails closed for every mutating
  verb); no active lease held by the invoking session for this unit
  (distinguishing unclaimed from held-by-someone-else); more than one
  active lease on the same unit with different holders (refuses rather
  than pick one); the underlying supervisor call itself failing or
  returning non-zero; `kill` — an invalid signal name, or the pid the
  supervisor reports right now no longer matching what the held lease's
  binding recorded (refuses rather than risk signalling a respawned
  process), or no permission to signal it; `edit-config` — no gated
  config file configured for the target, or it can't be read;
  `bootstrap` — no installed plist for the unit in the directory its
  domain names (the per-user LaunchAgents directory for `gui`,
  `/Library/LaunchDaemons` for `system`), refused before the supervisor
  is called.

**Writes.**
- The lease record for the resource being operated on: after every
  successful mutating verb, overwrites that lease's binding with the
  current pid, start time, label, a config hash, and the time of the
  bind — never creating or deleting a lease record, and never touching
  the lease's own resource field. When this program runs on a different
  host than the one holding the lease store, the bind is relayed there
  over one reused connection to this program's own internal target, at
  a path under the remote session's own home directory.
- No unit definition files are created or edited by this program —
  the mutating verbs load, enable, or signal an existing on-disk unit;
  `audit` installs nothing.
- Invokes the host's own supervisor tool to perform the requested
  operation, scoped to the enrolled unit's primary label only, never any
  secondary label the same unit also registers; `kill` sends a signal
  directly to the pid the supervisor currently reports.
- No forge object, no event, no database table.
- `status`/`audit` only read: the enrolment config, the lease store
  (locally, or via a read-only remote probe when it lives elsewhere), the
  local process/supervisor state, and, for `audit`, the shipped unit
  definitions and what the host actually has installed.

`UNDECIDED:` a mutating verb's or `kill`'s supervisor operation can
succeed while the follow-up write of its outcome into the lease record
fails — the process is then running unattributed in the ledger, with only
a stderr warning and no change to the exit code.

### bin/fleet-bind

undeclared — hook

Invoked by the session-start hook to bind a session's id to the operating
system process serving it, writing one liveness/ownership record under
the fleet tree. Never invoked directly by a lane or session; always exits
0 and is deliberately silent on stdout/stderr, since a session-start
hook's own stdout is injected into the session's context.

### bin/fleet-heartbeat

undeclared — hook

Invoked after every turn by the stop hook to touch a session's liveness
file, optionally relaying the touch over an already-open remote channel
when the session runs off the primary host. Never invoked directly by a
lane or session; always exits 0 and writes nothing to stderr, since a stop
hook's stderr can surface into the user's own terminal.

### bin/fleet-prepush

undeclared — hook

The body of the git pre-push hook: given the remote and the refs being
pushed, blocks a push only on positive evidence of a lease conflict or
ownership violation, and otherwise degrades open with a warning on any
infrastructure failure. Invoked by git itself, never directly by a lane or
session, and never mutates the lease store.

### bin/fleet-secret-guard

undeclared — hook

A pre-tool-use hook that blocks a read or write call before it can pull a
credential file into a session's transcript, matching against a
configurable rules file of path and command patterns. Fails open on an
unrelated runtime error but fails closed (blocks everything) if its own
rules file is unreadable or malformed. Invoked by the hook mechanism,
never directly by a lane or session.

### bin/fleetforge.py

undeclared — library module

The single module through which this repository talks to the forge's API:
one method per call shape (pull request, branch, commit, status, and
content lookups; comments; status posts) behind one client class, so a
second forge backend would be an added class rather than a sweep across
every caller. Every call returns a status-and-body pair and never raises;
imported by other programs, not run directly.

### bin/fleetjob.py

undeclared — library module

Library for starting, stopping, and listing one-shot dispatch jobs as
supervised operating-system units, choosing the implementation the host
actually provides. Loaded dynamically by `bin/fleet-watch`, not run
directly.

### bin/fleetlib.py

undeclared — library module

Shared library implementing the ledger's core locking protocol and record
contracts: paths, timestamps, session and liveness helpers, lease
read/write/validation, event emission, process-table lookups, and
change-request record handling, used by the various `fleet-*` programs.
Imported, not run directly; centralises the write-then-rename discipline
the rest of the fleet relies on for an atomic record update.

### bin/fleet-lane

declared

**Purpose.** Spawns one blackbox test lane against one plan at a pinned
commit: builds the repository's generated contract, hands it to one of the
repository's resolved `tester` runners (an omitted `--runner` means ordinal
1) with no filesystem or tool access of its own,
validates what comes back, and lands it as a draft pull request. One
runner, one invocation, no loop and no retry. A subscription CLI whose own
process can otherwise read anywhere (`sub-codex`, `sub-muse`) is spawned
under an outer `sandbox-exec` profile named in its own adapter entry
(`config/codex-tester.sb`, `config/muse-tester.sb`); the profile, never a
CLI flag, is what confines it (ADR-0006 §6).

**Exit codes.**
- `0`: a dry run completed, or a real run validated the vendor's result and
  opened the pull request; on success it prints `pr: <repo>#<number>` on
  standard output.
- `1`: any gate refused (plan zone, a runner that is not one of the repository's resolved tester
  rows or no tester at ordinal 1, unmeasured or unwired
  runner, a settings precondition, a mandatory bundle gap, or a
  `lane_run_cap` that is unset, unreadable, `0` or silent about the runner),
  the vendor's result failed validation or path confinement, the vendor
  stopped on an error that is not a usage limit, or a git or forge step
  after that point failed.
- `75`: "later, unchanged, this may pass" — the runner has reached its
  `lane_run_cap` in the last 24 hours (`capped`), or the vendor refused on
  a usage limit (`quota`). The `lane_run` row's outcome tells them apart.
- `2`: a malformed CLI argument, including argparse's own exit 2.

**Writes.**
- A prompt file, its wire-encoded form, the built contract, and (on a real
  run) the vendor's own scratch working directory, all under the fleet's
  work area, keyed by plan id and runner name; none of it is committed.
- A per-run log recording each gate and phase, appended under the fleet's
  work area's log directory.
- One `lane_run` row in the fleet database per run (a durable table): repo,
  plan, runner, commit, prompt hash, the usage the CLI reported and how the
  run ended. The cap is checked and the row reserved in one transaction just
  before the vendor is spawned; a run that a gate refuses is recorded as
  unspawned and never counts against the cap.
- On success: one new branch under `agent/tests/<plan>/<runner>`, pushed to
  the target repository, and one pull request against its `main`, titled
  with a `WIP: ` prefix. Its body opens with a header block (the lines before
  the first blank line): `Tests-for: <plan>` (never the dispatcher's own
  `Plan: <id>` marker), `runner:`, `sha:`, `prompt sha256:` and `usage:` (the
  same JSON the `lane_run` row stores, or `-`). Everything a vendor wrote —
  each gap and the notes — follows, every line prefixed `> `. Files land only
  under `tests/lane/`, mode 0644, and a returned file whose basename repeats a
  test module already under `tests/` is refused.
- One page (delivered or not) recording a refusal or the opened pull
  request, through the same durable page log every other fleet unit writes;
  a vendor usage-limit stop sends its own separate page.
- No review is ever dispatched on the pull request this program opens.

### bin/fleet-meet

declared

**Purpose.** Runs a test lane's pull-request files against an implementation
pull request's head, in a confined runner, and writes one findings document.
Invoked as `fleet-meet <owner/repo> <plan-id> --impl <pr> [--lane <pr>]...
[--dry-run]`. A lane runs only after the fleet's human position has approved
the lane pull request at the exact commit that will run; nothing of an
unapproved lane is imported. Every test gets one outcome — pass, fail, error or
skip — and a skip is never counted as a pass. It only reads the forge: it posts
no comment, status or review.

**Exit codes.**
- `0`: a findings document was written, even one full of failures or of
  lanes that were refused for their paths or timed out (findings are output),
  or a dry run completed.
- `1`: a host-level failure, and no document is written — the sandbox tool is
  missing or its start-up probe failed, a forge read or listing ended on an HTTP error status,
  the object store could not fetch — or the implementation pull request is not
  open or merged, is not authored by the author role, or lacks a `Plan: <id>`
  line of its own, or a named lane is not a lane of that plan.
- `2`: a usage error, including argparse's own exit 2.
- `75`: "later, unchanged, this may pass" — no lane has an approval by the
  human position at its current head, the review list could not be read, or a
  forge read failed in transport (timeout, connection refused or reset, DNS).
  Nothing ran and no document is written.

**Writes.**
- One document under the fleet work area, at
  `meetings/<plan>/<first 12 hex of the implementation head>/findings.json`,
  validated against `config/meeting-findings.schema.json` before it is renamed
  into place; its path is printed on standard output. A rerun for the same head
  keeps the old document beside it as `findings.<its generated_at>.json`.
- Nothing else durable: each lane's worktree and scratch directory are removed
  when its run ends, and no process carrying the run's nonce outlives it.

**Shape.** By reference to the schema. Per lane: the pull request, the
approved head, the branch, the runner, the prompt hash and whether it was
verified against the `lane_run` row, who read it and when, the files, the run
outcome (`ran`, `timeout`, `collection-error` or `refused`) and one entry per
test with `outcome` (`pass`, `fail`, `error`, `skip`), the raw pytest outcome,
the redacted reason and the source path. `comparable` is true only for two or
more lanes whose verified prompt hashes are equal. `all_passed` is false when
any skip, failure, error, unread or refused lane exists.

### bin/fleet-triage

declared

**Purpose.** Triages every test that did not pass in a meeting's findings
document, by a runner that can read the implementation, and posts the result once
per outcome as a comment on the implementation pull request. Invoked as
`fleet-triage <owner/repo> <plan-id> --impl-sha <sha> [--dry-run] [--no-post]`. It
refuses unless the findings validate and match the plan and sha, the plan on
`main` (never at `--impl-sha`, which is implementer-authored) says
`zone: public`, the repository resolves a `mediator` at ordinal 1, and that
runner is `sub-opus` (the one runner in its adapter table). The triager holds
`Read`, `Grep` and `Glob`, confined to its working directories by `--restricted`
(measured on the installed CLI, not assumed), over an exported copy of the
implementation and no other tool. Every citation it returns is checked against
the files, and the wrapper, not the triager, decides what counts as verified. It
never posts a commit status or a review and never starts a fix round. After the
comment is decided (or, when every test passed or none ran, in its place), it
also acts on every lane PR this findings document names a non-pass test for,
or that never ran at all (plan 0176): closed when every one of its non-pass
tests verified `test-harness-gap`, kept open with one shared issue when a
defect verified, kept open with a contract-needed comment, or kept open and
paged once (a lane that never ran gets this last one, with no test of its own
to show). A lane whose own tests all passed gets no disposition at all —
`docs/meeting-triage.md`, "What happens to the lane".

**Exit codes.**
- `0`: the comment was posted, was already posted for this findings digest, every
  test passed or no test ran for this meeting (nothing to triage either way), or
  a dry run or `--no-post` completed.
- `1`: refused — findings missing, invalid or for another plan or sha, a zone that
  is not `public`, no `mediator`, a runner outside the adapter table, a thread
  that could not be read in full, a comment that failed the assert-before-post, or
  a post that failed. Nothing was spawned by a refusal at a gate.
- `2`: a usage error, including argparse's own exit 2.
- `3`: the triager timed out, exited non-zero or returned output that was not a
  valid document; the comment listing every non-pass as not triaged was still
  posted.

**Writes.**
- Beside the findings document, `triage.json` (the findings digest, the runner,
  the model the CLI reported, the prompt hash, the triager's raw document, each
  item's verification result, the posted comment id, and every lane's
  disposition with its pending steps) and `prompt.txt`.
- One comment on the implementation pull request, opening with
  `<!-- eunomia:meeting-triage findings=<digest> status=ok -->` or
  `... status=failed -->`. At most one `status=ok` comment per findings digest;
  a `status=failed` comment does not count as done, so a by-hand rerun for the
  same digest spawns the triager again.
- Per lane PR (plan 0176): a comment (capped at the main triage comment's own
  size; one that would exceed it pages instead of being sent), opening with
  `<!-- eunomia:lane-disposition plan=<plan> digest=<findings digest>
  head=<lane head> action=<action> -->` — this is the dedup marker a retry of
  the SAME digest is found by, never the triage comment's own url (which
  changes on every hand rerun of a FAILED digest); a genuinely new digest,
  even at the same implementation or lane head, always posts its own,
  accurate comment rather than recovering an earlier digest's — and — only
  when every one of that lane's non-pass tests verified `test-harness-gap` —
  a PATCH closing it, carrying
  `<!-- eunomia:lane-closed plan=<plan> head=<lane head sha> -->` (proof a
  close was attempted, never that it completed), followed, once that PATCH
  actually returns success, by a SECOND comment carrying
  `<!-- eunomia:lane-closed-confirmed plan=<plan> head=<lane head sha> -->`.
  Before every close attempt, a later run checks the lane's own thread for
  that confirmed marker at this exact lane head, from ANY digest — found, a
  close for this head genuinely completed before and the PR is open now only
  because a person reopened it, so the rule leaves it alone until the head
  moves. This is read straight off the forge, never from local history: an
  earlier design inferred the same fact from whichever implementation head's
  `triage.json` on disk still recorded a confirmed close, which broke once
  that one record was overwritten by a later digest's own write at the same
  implementation head. The gate re-reading a lane before acting also treats
  one further shape as a completed close rather than a terminal skip: the
  close step still pending, the comment step already done, and the PR found
  closed (not merged) at exactly this lane head — D2a's measured lie, where
  the close PATCH landed despite its own write reporting failure; the confirm
  step still runs in that case, never abandoned. A lane found closed before
  this program ever attempted its own comment keeps the ordinary "already
  closed" skip. `fleetforge` gains no verb for any of this: both
  comments and the close PATCH, and the one issue per `(repo, plan)` a
  verified `implementation-defect` or `spec-or-contract-defect` gets filed
  under (`Lane-defects-for: <plan>`, created or edited — its STATE never
  changed, whatever the lookup finds it in, since this rule holds no
  issue-reopen capability any more than an issue-close one — never
  duplicated), go through `fleetforge`'s transport directly, the
  `fleet-rebase` `_close_relanded` precedent. A lane the gate finds closed or
  moved is skipped, silently, forever at that head; one the gate could not
  even read is paged, like any other failed step, and retried. A write still
  failing after three tries, 2s/8s apart, pages once naming the retry
  command, worded for whether that retry replays pending steps or (a digest
  recorded with a failure) re-spawns the triager; a comment post is never
  blindly retried (it is not idempotent) — each attempt first checks the
  thread for its own already-landed copy. The issue lookup behind all of this
  is a single read per call, never retried AS A READ within the run (its own
  failure pages immediately instead); the one exception is the issue CREATE
  path, whose own retries repeat this same listing first, to find a duplicate
  its earlier POST actually wrote, the identical idempotency pattern comment
  posts use.
- One comment on the shared `Lane-defects-for: <plan>` issue, only while it is
  open, when this run keeps no lane for a defect and every lane GENUINELY
  passed — the strongest form of "no lane kept for a defect"
  (`docs/meeting-triage.md`, "A defect"). This fires even on the "nothing to
  triage" exit `0` path, which otherwise posts no comment on the
  implementation pull request at all; it never fires when the triager itself
  failed (exit `3`), nor when no lane ran at all (one refused, timed out or
  failed to collect — the same empty-tests shape as "every test passed", but
  `findings["all_passed"]` is false), because nothing was classified in
  either case, and it posts nothing when the issue the lookup finds is
  closed. The issue this looks up is re-checked "still open" immediately
  before every post, never cached from an earlier attempt; more than one
  marked issue is logged and resolved to the lowest-numbered,
  deterministically; its body carries the same size cap the main triage
  comment does. The lookup's own listing is capped at 20 pages; hitting the
  cap pages and retries like any other unreadable prerequisite, never reads
  as "no issue".
- A run log under the fleet work area. No database table and no event — plan
  0176 left `lane-closed`/`lane-kept` event-type registration (`bin/fleet-emit`,
  `bin/fleet-events`) as a follow-up, because the event set needs a SPEC PR.

**Shape.** The comment lists the plan, the implementation sha, each lane pull
request with its head and prompt hash, and the triager's runner and reported
model, then a table of classifications whose evidence verified, the verified gap
contradictions, the items whose evidence did not verify, and the tests not
triaged. Classes are `implementation-defect`, `spec-or-contract-defect`,
`test-harness-gap`, `skip-needs-contract` and `undetermined`. A verified citation
proves the cited text exists, not that the class is right.

### docs/diagrams/check-svg-layout.py

undeclared — diagram tool

A geometry linter for this project's house-style diagrams: checks a
generated image for non-axis-aligned or box-crossing connectors, text
overflowing its container, and a missing title or legend, reporting
problems rather than fixing them. Run by hand against a diagram's output,
not invoked by any lane or session workflow.

### docs/diagrams/gen-dispatch-dataflow.py

undeclared — diagram tool

Generates a diagram showing what each step of the plan-dispatch process
runs and what it writes, as a companion to the full dispatch-flow diagram.
A standalone script producing a static image, checked afterward by
`check-svg-layout.py`.

### docs/diagrams/gen-plan-dispatch-flow.py

undeclared — diagram tool

Generates the full reference diagram of the plan-dispatch flow. A
standalone script producing a static image, meant to be paired with
`check-svg-layout.py` for geometry validation.

### docs/diagrams/gen-plan-dispatch-light.py

undeclared — diagram tool

Generates a simplified variant of the plan-dispatch flow diagram for an
audience outside the project. A standalone script producing a static
image.

### docs/diagrams/gen-pr-ledger.py

undeclared — diagram tool

Generates the two plan-0209 diagrams: what runs where for the webhook-fed
pull-request ledger, and how a sample fleet's pull requests are read. A
standalone script writing two static images beside itself, checked
afterward by `check-svg-layout.py`.

### tools/ctr-bench.py

undeclared — bench

A benchmarking script comparing cold-start and warm-restart times of two
container runtimes running the same image on the same host, alternating
trials between them to cancel out load drift. Run by hand to produce
timing numbers, not part of any lane or session workflow.

### openapi

undeclared — absent artifact

eunomia is not a service and ships no OpenAPI document; `fleet-bundle`
reports this as a gap so the absence reads as a decision rather than an
oversight.

## Records

The three record shapes below restate `SPEC.md`'s "Event record", "Lease
record", and "Change-request record" sections for this audience: field
name, type, and meaning, with no example values — `SPEC.md`'s own examples
carry values not appropriate for this document. On any disagreement
between this section and `SPEC.md`, `SPEC.md` is right and this section is
wrong.

### Event record

One JSON object per line, appended to the fleet's event ledger under its
own lock.

| field | type | meaning |
|---|---|---|
| ts | string | UTC timestamp the event was recorded |
| actor | string | the acting session's id, or an attributed killer/actor for a small set of event types that name one explicitly |
| type | string | one of the closed event types listed below |
| repo | string or null | `owner/name` the event concerns, or null for a fleet-scoped event |
| pr | integer or null | the pull request number the event concerns, or null |
| sha | string or null | a commit sha whose meaning depends on the event type (a pull request's head commit for most `pr-*` events; the merge commit on the default branch for a merge or deploy event) |
| lease | string or null | the lease id for a lease-scoped event, else null |
| detail | object | a producer-defined object, shape depends on `type`, defaults to empty |

### Lease record

One JSON file per lease.

| field | type | meaning |
|---|---|---|
| id | string | the lease's own id |
| resource | object | what the lease grants (see the nested fields below) |
| type | string | (inside `resource`) one of `branch`, `paths`, `sim`, `service`, `budget` |
| repo | string | (inside `resource`) `owner/name` — for `branch` and `paths` resources |
| branch | string | (inside `resource`) the branch name — for `branch` resources |
| globs | array of strings | (inside `resource`) path globs the lease covers — for `paths` resources |
| host | string | (inside `resource`) the host the resource is scoped to — for `sim` and `service` resources |
| udid | string | (inside `resource`) a bound simulator's identifier — for `sim` resources, filled at activation |
| service | string | (inside `resource`) the supervised unit's own name — for `service` resources |
| tokens | integer | (inside `resource`) the token allotment — for `budget` resources |
| holder | string or null | the session holding the lease, or null on an unclaimed pool row |
| granted_by | string | the session or process that granted the lease |
| state | string | one of `assigned`, `active`, `released` |
| ttl_minutes | integer | how long the holder's liveness may go stale before the lease is considered orphaned |
| created | string | UTC timestamp the lease was created |
| activated | string or null | UTC timestamp the lease was activated, or null |
| note | string | a free-text note naming the lease's purpose |

### Change-request record

One JSON file per change request.

| field | type | meaning |
|---|---|---|
| id | string | the change request's own id |
| repo | string | `owner/name` the change targets |
| lane | string | the lane that filed the request |
| target | string | the path the change targets |
| intent | string or null | a prose description of the requested change; at least one of `intent`/`patch` is required |
| patch | string or null | a unified diff of the requested change; at least one of `intent`/`patch` is required |
| for_pr | integer or null | the pull request the change supports, if any |
| state | string | one of `filed`, `applied`, `rejected` |
| filed | string | UTC timestamp the request was filed |

### Event types

The closed v0.2 event-type set restated from SPEC.md's Event record
section. Additions require a change to that section.

`session-spawned` `session-stopped` `session-killed` `lease-assigned`
`lease-activated` `lease-released` `lease-orphaned` `lease-takeover`
`cr-filed` `cr-applied` `pr-opened` `pr-pushed` `review-posted` `pr-armed`
`candidate-testing` `pr-merged` `pr-bounced` `token-minted`
`token-revoked` `deploy-started` `deploy-verified` `budget-checkpoint`
`plan-dispatched` `plan-done` `plan-failed` `plan-blocked` `plan-question` `question-answered`
`capability-published` `pin-drift` `repo-model-changed` `seat-access-changed`
`fleet-setting-changed` `plan-requeued` `review-requeued` `followup-triaged`
`followup-closed` `quota-closed` `quota-opened` `lane-dispatched`
`lane-skipped` `lane-ended` `credits-window-opened`
`credits-window-closed` `credits-spent` `credits-cap-reached`
`credits-reservation-expired` `credits-refused` `credits-grant-given`
`credits-grant-revoked` `credits-job-spent`.
