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

### bin/fleet-config

declared

**Purpose.** The single writer for a small, closed set of fleet-scoped
settings (currently only the dispatch concurrency cap) held in the fleet's
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
  (or the same as JSON); `list` prints one line per row; `set` prints a
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
  read-only for `ledger`), and, for `ledger --verdict`, the forge.
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
explaining what that classification does and doesn't mean.

### bin/fleet-release

declared

**Purpose.** Release a lease you hold. Refuses to release another
session's lease unless forced.

**Exit codes.** `0`: released, or idempotently already released (releasing
twice is not an error). `2`: a missing lease-id argument or malformed
flags. `1`: no such lease exists; the lease is held by a different session
and force wasn't given.

**Writes.**
- The lease's own record: sets its state to released, written under the
  lease's own lock.
- Deletes the lease's orphan marker if present.
- Event `lease-released`, `detail` present only on a forced release of
  someone else's lease, naming the previous holder.
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
  duplicating rows.
- No write back to the forge — every call is a read; no forge object is
  created or modified.
- Stdout: `--backfill` prints a one-line count summary; `--report` prints
  a fixed-width table (repository, pull-request number, review rounds,
  approval count, review time span) and a total, followed by a standing
  caveat about a field this table cannot reliably report.

`UNDECIDED:` a second table for future pricing data is created by every
run but nothing in this file ever reads or writes to it — declared as
future-use schema only.

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

### bin/fleet-stalled

declared

**Purpose.** Meant to run on a schedule: scans every enrolled repository
and pages once for each open pull request stuck unreviewed (CI green,
never reviewed), with a stale review (a review predates the current
head), with a stale-base failure (CI red only because the base branch
moved on and is itself green), or genuinely red. It never rebases,
dispatches a review, or comments — it only detects and pages.

**Exit codes.**
- `0`: clean scan, nothing stalled, nothing unreadable.
- `1`: at least one stalled pull request found, no scan trouble.
- `2`: one or more scan troubles occurred (takes priority over reporting
  findings) — a bad configuration row, a token-helper failure (the whole
  scan is then skipped), a per-repository mode that is named but not yet
  built (refused explicitly rather than silently treated as the default
  mode), or an unreadable/truncated read of pull requests, reviews, or
  commit statuses.
- argparse's own exit 2 on malformed CLI arguments.

**Writes.**
- A dedupe/state file recording pages already sent (keyed by repository,
  pull request, and head commit, so a new push re-pages and an unchanged
  head stays silent) and scan troubles already paged once per reason per
  day. Written atomically; not written under `--dry-run`.
- One push notification per new finding and per new trouble, through a
  configurable channel if set, recorded either way to the fleet's shared
  page log.
- Only reads from the forge — commit statuses, pull-request lists, review
  lists; no forge object created or modified.
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
  tools own the record shape.
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
- Events (appended to the fleet's shared event ledger): `plan-dispatched`
  on every successful spawn (detail: plan id, session, branch, lease,
  spawn mechanism); `plan-failed` with a reason of "lease assign failed"
  or "spawn failed"; `plan-blocked`, emitted only on a transition in the
  blocking reason, never once per cycle (detail: plan id, the unmet
  dependency, its form, and the reason); `pin-drift`, with the pin's path,
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
  if unset.
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
  problem rather than a failed plan.
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
  request creation is left to the spawned session. It edits an existing
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

**Purpose.** The collector: ingests four local and remote sources into the
fleet's shared database — session transcripts, the event ledger, per-lease
run logs, and (by default only against the live fleet area) CI task data
from the forge and a remote log archive.

**Exit codes.**
- `0`: the sweep completed with zero per-file or per-source ingest
  failures, and no failure reconstructing the settings table.
- `1`, grouped (all converge on the same final code, so the cause is not
  distinguishable from the exit code alone): one or more transcript files
  failed to parse or ingest; one or more event-ledger files (current or
  rotated) failed to ingest; one or more run-log files failed to ingest;
  the settings-table reconstruction step raised while replaying ledger
  events; the CI source raised outright, or reported one or more
  individual log-fetch failures; the target database's schema version is
  newer than this build understands — refused unconditionally before any
  source is scanned.
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
  transcript tree, the event ledger, and per-lease run logs); a fourth,
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
  a row; the lease store unreachable (fails closed for every mutating
  verb); no active lease held by the invoking session for this unit
  (distinguishing unclaimed from held-by-someone-else); more than one
  active lease on the same unit with different holders (refuses rather
  than pick one); the underlying supervisor call itself failing or
  returning non-zero; `kill` — an invalid signal name, or the pid the
  supervisor reports right now no longer matching what the held lease's
  binding recorded (refuses rather than risk signalling a respawned
  process), or no permission to signal it; `edit-config` — no gated
  config file configured for the target, or it can't be read.

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
runner, one invocation, no loop and no retry.

**Exit codes.**
- `0`: a dry run completed, or a real run validated the vendor's result and
  opened the pull request.
- `1`: any gate refused (plan zone, a runner that is not one of the repository's resolved tester
  rows or no tester at ordinal 1, unmeasured or unwired
  runner, a settings precondition, a mandatory bundle gap), the vendor's
  result failed validation or path confinement, or a git or forge step
  after that point failed.
- `2`: a malformed CLI argument, including argparse's own exit 2.

**Writes.**
- A prompt file, its wire-encoded form, the built contract, and (on a real
  run) the vendor's own scratch working directory, all under the fleet's
  work area, keyed by plan id and runner name; none of it is committed.
- A per-run log recording each gate and phase, appended under the fleet's
  work area's log directory.
- On success: one new branch under `agent/tests/<plan>/<runner>`, pushed to
  the target repository, and one pull request against its `main`, titled
  with a `WIP: ` prefix, carrying a `Tests-for: <plan>` marker (never the
  dispatcher's own `Plan: <id>` marker) and the vendor's reported gaps.
- One page (delivered or not) recording a refusal or the opened pull
  request, through the same durable page log every other fleet unit writes.
- No review is ever dispatched on the pull request this program opens.

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
`plan-dispatched` `plan-done` `plan-failed` `plan-blocked`
`capability-published` `pin-drift` `repo-model-changed`
`fleet-setting-changed` `plan-requeued`.
