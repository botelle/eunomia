# Fleet history — the passive half of the control plane

*The observability layer: what actually ran, derived without asking anyone. Companion
to `SPEC.md`, which defines the active half (leases, events, heartbeats).*

## The finding this rests on

**Every Claude Code session is already instrumented.** Its transcript
(`~/.claude/projects/<project>/<session>.jsonl`, and `<session>/subagents/*.jsonl`)
records, per assistant turn: the model, a full token breakdown (input, output, cache
read, cache write), a timestamp, and every tool call with its input. Subagent
transcripts nest under the session that spawned them, so lineage is on disk too.

That means fleet history needs **no opt-in, no cooperation from the session, and no
new plumbing in the hot path** — and it works retroactively over history that already
exists. The 2026-08-17 postmortem was reconstructed this way by hand, with a
six-agent sweep; this makes it a query.

The first ingest of the existing corpus: **831 sessions, 61,354 turns, 62,986 tool
calls, in 8 seconds, into a 36 MB database.** It reproduces the fan-out night from raw
transcripts — 17 sessions + 91 subagents, peak 15 concurrent, 2.9M output tokens —
matching the hand-built postmortem.

### The counting trap (found in review 1414)

A transcript writes **one line per content block**, and every line of the same
assistant message repeats that message's `id` *and* its `usage`. Summing per line
over-counts: measured **2.54x on output tokens** against a real transcript. A turn is
a unique `message.id`, counted once.

Within one message, `usage.iterations` carries per-API-call usage and the top-level
fields mirror only the **last** iteration — so a multi-iteration message must be
summed across iterations or it is *under*-counted (observed: 324 top-level vs 471
actual). Both corrections are covered by tests, with fixtures that reproduce the
repeated-line shape; the original fixtures modelled one line per turn, which is
exactly why the bug survived the first round.

## Two sources, one database

| | Passive (this doc) | Active (`SPEC.md`) |
|---|---|---|
| Source | Claude Code transcripts | `~/dev/.fleet/events.jsonl` + `leases/` |
| Covers | **every** session, automatically | only sessions running `fleet-*` |
| Records | tokens, wall clock, models, tools, files, subagent tree, concurrency | leases and when they were taken, sim slots, CRs, PR/arm/merge events |
| Answers | what *happened* | what was *intended* |
| Available | now | as eunomia rows 2–6 land |

Both are needed. The passive half is the honest record; the lease half is the claimed
intent. **The panel worth building last is the one that compares them** — a session
writing to a repo it holds no lease on is exactly the class of failure that cost the
fan-out night, and it is only visible when both halves exist.

## Components

### `bin/fleet-collect`
Sweeps every transcript into `~/dev/.fleet/fleet.db` (the `SPEC.md` runtime tree).
Idempotent by `(size, mtime)`: an unchanged file is skipped, a grown file has its rows
replaced rather than appended (a live session's transcript grows between runs —
appending would double-count it), and a **truncated** file has its stale rows cleared.
Tables: `session`, `turn`, `tool_use`, `ingest`.

Rows are keyed by the transcript's **path relative to the projects root**, not its
filename stem: stems are not unique across projects (`journal` collides five ways
today), and a collision would delete another transcript's rows — the precise class of
bug the idempotence design exists to prevent. The session/agent uuid is kept alongside
as `session_uuid`, which is what joins to `EUNOMIA_SESSION`.

A subagent's transcript records carry the **parent's** `sessionId`, not its own
(verified on real files), so a subagent's identity is taken from its filename and only
a top-level session trusts the `sessionId` field. Trusting it everywhere would make one
uuid resolve to a parent *and* all of its subagents.

Ingest is **per-file transactional**: a malformed transcript records its error in
`ingest.error` and the sweep continues — and a row carrying an error is **retried on the
next sweep**, because a completed transcript never changes again, so "retry when the file
changes" would mean never (a transient `database is locked` under a snapshot's read lock
would silently drop that session forever). With a single end-of-run commit, one bad file
meant launchd relaunched into the same crash every interval with nothing ever
persisted.

Runs every 2 minutes under `launchd/org.eunomia.fleet-collect.plist`, at background
QoS and low-priority I/O so it never contends with an active session.

### `bin/fleet-snapshot`
`VACUUM INTO` — the supported way to copy a live SQLite database. It takes a read
lock and writes a defragmented copy that cannot contain a half-committed
transaction; `cp` of a live DB can capture a torn page plus a stale `-wal` and
restore as corrupt. The snapshot is then integrity-checked and discarded if it fails,
because a snapshot that cannot be opened is worse than no snapshot — it looks like one.
`--keep N` prunes old snapshots; `--verify` reports backup coverage honestly.

## Secret hygiene

The database is **identity-tier**: it holds first-prompt text, session titles, and
file paths. Three rules, all tested:

- A `Bash` tool call records **only the command's verb** (`curl`, `git`), never its
  arguments — a command line is exactly where a credential shows up, and the
  2026-08-17 night leaked two that way. Leading `VAR=value` assignments are dropped
  rather than recorded: the *first word* of `RESTIC_PASSWORD=… restic backup` is the
  credential itself, which the first version of this collector would have stored.
  Splitting uses `shlex`, since whitespace-splitting a *quoted* assignment stores half
  the secret; a command with unbalanced quotes (a heredoc, typically) records `?`
  rather than falling back to the leaky split. That costs the verb label on ~0.8% of
  real tool calls, which is the right trade against writing fragments of a credential
  into an identity-tier database.
- `first_prompt` is free text a human may have pasted a secret into, so it is passed
  through a redactor for common credential shapes (`ghp_…`, `xox…`, `sk-…`, JWTs,
  `token=…`, long hex) before storage.

- Nothing under `~/dev/.fleet/` may contain secret material (`SPEC.md` principle 6),
  since the atlas Fleet view renders this tree.

Both rules are asserted against the database *file* — the test greps the bytes, not
just the column, so an encoding that smuggled the value through would still fail.

## Backup

`fleet.db` belongs to the **crown-jewels backup set** — `operator/infra`
`runbooks/dr-plan.md` item 2 already names "service DBs" as members; this is one.

Encryption at rest is **restic's job**, per dr-plan item 3 (engine decided 2026-08-07:
restic over raw tar+age). This repo deliberately ships no parallel `age` pipeline that
would be torn out when item 3 lands, and rolls no crypto of its own.

Until then, `fleet-snapshot --verify` states the true position rather than implying
coverage: snapshots are opshost-local, FileVault at rest, **not** offsite. Add
`~/dev/.fleet/snapshots/` to the restic set when dr-plan item 3 is done.

## CI lanes: what the container costs (plan 0024, Track A)

cihost carries two runner labels. `cihost` is the host executor — jobs run as the
operator's own user, with the operator's home directory, `~/.claude/settings.json`
and the Forgejo data directory all in reach. `cihost-linux` is
`docker://ghcr.io/catthehacker/ubuntu:act-22.04` on the colima VM. The same
`tests` job runs on both, so the difference between the two columns is the
container and nothing else.

The plan expected a tax and asked for its size. **There is no tax.** The container
lane finished faster than the host lane in all three runs, and it is the lane where
the operator's credentials are not reachable.

### Containment, measured rather than assumed

`probe-reach` prints reachability only — never a path's contents. Verbatim from
run 254 (`9833d7f`), both jobs of the same commit:

| | `cihost` | `cihost-linux` |
|---|---|---|
| `/Users/operator/agent` | reachable=yes | reachable=no |
| `/Users/operator/.claude/settings.json` | reachable=yes | reachable=no |
| `/opt/homebrew/var/forgejo/data` | reachable=yes | reachable=no |
| docker socket | **open** | closed |
| host mount | n/a | closed |

`DOCKER_HOST` is set in the runner's LaunchDaemon and is therefore inherited by
both, but inside the container the socket path it names does not exist, so the
route is closed by absence rather than by policy. The gate treats a `unix://`
URL whose path is absent in-container as closed; it stays fail-closed for `tcp://`
and `ssh://` and for any socket file that is actually present.

The host row is not hypothetical. talos #12 failed CI because four tests read the
operator's real settings file out of a job.

### Timing

Per-step seconds from the `TIMING` lines in each job's log. Queue wait is
`job.started - run.created` from the Forgejo database, not a stopwatch.

| run | commit | lane | queue | checkout | install | tests | total | suite |
|---|---|---|---|---|---|---|---|---|
| 235 | `921443e` | `cihost` | 558 | 5 | 18 | **233** | 260 | 442 passed |
| 235 | `921443e` | `cihost-linux` | 826 | 2 | 2 | **21** | 27 | 440 passed, 2 skipped |
| 254 | `9833d7f` | `cihost` | 17 | 2 | 2 | **76** | 81 | 519 passed, 1 skipped |
| 254 | `9833d7f` | `cihost-linux` | 17 | 0 | 3 | **43** | 46 | 516 passed, 4 skipped |
| 262 | `abc253e` | `cihost` | 1 | 3 | 3 | **78** | 84 | 521 passed, 1 skipped |
| 262 | `abc253e` | `cihost-linux` | 2 | 1 | 3 | **38** | 43 | 518 passed, 4 skipped |

Run 262 is the merge of #131 to `main`; 235 and 254 are branch pushes. The suite
grows across the three because the commits between them add tests — compare lanes
within a row, never `tests` down a column.

Both lanes build a venv and `pip install` every run. The host was expected to win
that step on a warm pip cache and does not win it by enough to matter: 2–18 s
against 2–3 s. A persistent cache volume does not need its own plan on this
evidence.

### What the timing does not prove

Two things moved during the window these runs span, and only one of them is the
container:

- Runner capacity went 1 → 4 at 17:34. Runs 235 (17:03) are before it, 254 (17:52)
  and 262 (18:08) after. The queue column collapses across that boundary —
  558/826 s down to 1–17 s — and the host `tests` column falls with it, 233 s to
  76/78 s. **Neither number is attributable to the container**, and the drop in
  host test time is contention on cihost easing, not the capacity setting doing
  work inside a job.
- cihost also runs Xcode builds and resident models. What the host lane competes
  with is not constant and was not controlled.

The container's own `tests` column is the steadier of the two — 43 s and 38 s on
the 520-test suite — but three runs is three runs. The claim this table supports
is *the container lane is not slower*, which is what the decision needed. It does
not support a speedup figure, and none is quoted here.

### The uid-0 asymmetry

Host jobs run as `operator`. Container jobs run as **uid 0**. Both lanes collect
522 tests on `abc253e`; the container skips three more, each with a printed reason
(`pytest -q -rs`, so the reasons are in the log rather than inferred):

- `test_fleet_cr.py:592` — mode-bit denial is meaningless as root
- `test_fleet_watch.py:777` — chmod cannot deny root
- `test_fleet_watch.py:2199` — `plutil` unavailable on a non-macOS runner

The first two are the asymmetry itself: a permission-denied path is only testable
on the host label. That is an argument for keeping `cihost` alive alongside
`cihost-linux`, not for moving everything. The fourth skip
(`test_fleet_watch.py:2053`, keyvault not cloned beside eunomia) is on both lanes
and is about the checkout, not the runtime.

Xcode workflows do not move: a Linux container has no Xcode. The label split is
the design — the job declares which world it needs.

### Environment pinned to these numbers

- Runner: `capacity: 4`, `privileged: false`, `valid_volumes: []`,
  `docker_host: "-"` (any URL here would mount the socket into job containers),
  `.runner` address `http://192.0.2.4:3001` (containers cannot reach `127.0.0.1`)
- Labels: `cihost:host`, `cihost-linux:docker://ghcr.io/catthehacker/ubuntu:act-22.04`
- Image digest: `ghcr.io/catthehacker/ubuntu@sha256:f5f5c29208c4fd541704fe7b8df33d3bf620ce4ac46f36853b7abeb0159705c4`
- colima: `vz`, `virtiofs`, aarch64, 4 CPU / 8 GiB / 60 GiB, `mounts: []`
- One runner supervisor (system LaunchDaemon). The user LaunchAgent
  `org.eunomia.forgejo.runner.plist` is `.disabled`.

## Roadmap position

This is roadmap row 6a (see `ROADMAP.md`). The atlas Fleet tab that renders it is a
follow-on PR; the collector ships first because **history not collected is gone
forever**, while a view over collected history can be built any time.
