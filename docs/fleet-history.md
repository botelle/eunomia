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

**Claude Code is not the only writer (plan 0197).** Codex, antigravity and Muse
all implement plans too, and their own raw output lands beside Claude's own —
not under `~/.claude/projects`, but at `~/dev/.orchestrator-transcripts/`, the
orchestrator's own output tree (`SPEC.md` Principle 6 keeps it outside the
fleet tree, since it is the one artefact most likely to carry a secret). This
collector reads that tree too, one `session` row per writer transcript,
correlated to the runner and model that produced it through the orchestrator's
own run log rather than the transcript's filename. Full shape, including why a
Claude transcript found there is skipped rather than double-counted, is in
`docs/fleet-db.md`.

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

## CI lanes: what two eunomia runs share (plan 0113)

Until this plan, every eunomia push — on every branch, in `ci.yml`, `plans.yml`
and `deploy-docsite.yml` alike — queued behind one literal lane,
`eunomia-cihost-workspace`. On 2026-09-29 the head of #686 waited about 50
minutes behind `main`, plan 0071 and an earlier 0102 commit, and the review
ended with 0 rounds; on 2026-10-06, plans 0114 (#751) and 0115 (#753) both
timed out waiting on CI, and #752's 6-second `Plans / lint` job sat "Waiting to
run" for over two hours while later pushes from other branches ran ahead of it.
The runner itself was not the bottleneck: `capacity: 4` on the one root
LaunchDaemon, measured running four concurrent jobs on 2026-09-07. The
literal group was.

The lane existed because of run 21: a `tests (cihost)` failure on a tree
BYTE-IDENTICAL to one that had already passed twice (4347177 and ea57062 both
have tree `19c4bb0b`), attributed by the comment above `ci.yml`'s
`concurrency:` to "this runner's workspace is per-repository, so two eunomia
jobs at once share a checkout and a .venv". A tiphys probe on 2026-09-07
contradicted that specific mechanism — three sequential runs each got their
own `~/.cache/act/<hash>/hostexecutor` directory and could not see a marker
file another run had written (infra `runbooks/forgejo-runner-capacity.md`,
"What the workspace actually is") — but the probe ran at `capacity: 1`, so its
runs never actually overlapped. No two eunomia runs had ever been observed
running at once.

### What was actually shared, and what was already per-run

`ci.yml`'s `tests (cihost)` job runs a diagnostic step added for this plan,
`per-run state before redirect (plan 0113)`, before the `per-run scratch
(plan 0113)` step that does D2's redirecting — so its `STATE` lines, in the
job log of any run of this commit or later (this PR's own `CI / tests
(cihost)` run included), show the host default each row below is measured
against, the same way `probe-reach`'s `REACH`/`ROUTE` lines are this repo's
precedent for a job log as the evidence rather than a claim about the runner.

| state | touched by | status | evidence |
|---|---|---|---|
| the checkout, `$GITHUB_WORKSPACE` (`pwd -P`) | every job | **per-run** | D3's 18 cross-branch-overlapping `tests (cihost)` runs (below) were all green on the SAME tree; a shared checkout racing two `actions/checkout@v4` and two `.venv` builds could not produce that. Consistent with the 2026-09-07 tiphys probe (each run's own hostexecutor dir). |
| act's action cache, `~/.cache/act/` (the `actions/checkout@v4` clone) | every `uses:` step | shared, not redirectable | the diagnostic step's `STATE act_cache_entries=` line is nonzero every run and the directory has no per-job key; it is runner-owned, not workflow-owned, so no env var in a workflow file reaches it. |
| the pip cache (`pip cache dir`, `$PIP_CACHE_DIR`) | `tests`, `talos-contract`, `plans.yml`'s `lint` install steps | **made per-run by D2** | the diagnostic step's `STATE pip_cache_dir=` line prints the host default on an unpatched run; the `per-run scratch (plan 0113)` step in each of those three jobs now sets `PIP_CACHE_DIR` to a directory keyed by `$GITHUB_RUN_ID` and the job, under `$RUNNER_TEMP`. |
| pytest's base temp (`$TMPDIR/pytest-of-operator/`, numbered dirs pruned to the last 3) | `tests`, `talos-contract`, `plans.yml`'s `lint` | **made per-run by D2** | pytest's basetemp follows `TMPDIR`; the same `per-run scratch` step sets `TMPDIR`, so no `--basetemp` flag or `PYTEST_ADDOPTS` entry was needed (the latter is also refused: `tests/test_env_isolation.py` runs a nested pytest that inherits `PYTEST_ADDOPTS`, and a shared `--basetemp` there would let two sessions wipe each other's). |
| `/tmp` and generic `$TMPDIR` writes (e.g. `bin/fleet-bundle` running children with `cwd=tempfile.gettempdir()`) | `tests`, `plans.yml`'s `lint` | **made per-run by D2** | Python's `tempfile` module reads `TMPDIR`, so redirecting it in the same step redirects these too. The `/tmp/ci-timing-start-*` fallback in the `stamp`/`total` steps is unaffected and unchanged — it is already keyed by `$RUNNER_TEMP` or `$GITHUB_RUN_ID` (L3, predates this plan) and was already per-run. |
| the `.venv` each full-suite job creates in its checkout | `ci.yml`'s `tests`, `plans.yml`'s `lint` | **per-run** | follows directly from the checkout row above: `.venv` is a relative path inside a checkout that is itself per-run. |
| `$RUNNER_TEMP`: set, and per-job? | every job that uses it | **per-job**, confirmed | already assumed by the pre-existing `stamp`/`total` timing steps (L3) and by `talos-contract`'s own venv and clone paths; D3's 18 overlapping runs produced no cross-contamination, and D2's own scratch dirs additionally key by `$GITHUB_RUN_ID` and job name as a defensive belt-and-suspenders measure in case a runner ever shares it. |
| `$HOME`-derived paths the suite could *read* (`~/dev/.fleet`, `~/.local/share/pins`, the default `FLEET_DB`, `~/dev/.orchestrator-transcripts`) | `tests`, `plans.yml`'s `lint` | shared, read-only | the diagnostic step's `STATE home_path=... exists/absent` lines show these paths are the same for every run. Most tests that touch `FLEET_DB` override it to a `tmp_path`-scoped file first (fleet convention) — but not all: `test_plan_ledger.py`'s `test_the_live_ledger_scopes_to_this_repo_and_carries_only_what_it_can_prove` (line 677) deliberately reads whatever `FLEET_DB` already is, or the live default `~/dev/.fleet/fleet.db`, and asserts only properties that cannot drift with either. Still read-only — it never writes through that path — so the row's status stands, but "every test... overrides it" was too strong a claim. |
| the Colima Docker daemon and image pulls (`force_pull: true`) | `cihost-linux` jobs | shared, not redirectable | one Colima VM for the host; no per-job daemon exists to point at. |
| the talos `file://` clone from `/opt/homebrew/var/forgejo/data/forgejo-repositories` | `talos-contract` | **per-run already** | the job clones into `"$RUNNER_TEMP/talos"`, predating this plan; the source repository is read-only and never written by the clone. |
| the pinned pyright 1.1.414 build's npm cache (plan 0110's `typecheck` step; `~/.cache/pyright-python` by default) | `tests`' `typecheck` step | shared, not redirectable | `pyright-python` does honor `PYRIGHT_PYTHON_CACHE_DIR` (`pyright/utils.py:get_cache_dir`, verified by reading the installed 1.1.414 wheel), but redirecting it per-run was never part of the D3-stressed tree (scratch commit `04cf0a4a`, tree `0e03c4f5f5`, the one D3's 18 isolated+control runs actually exercised) — an earlier revision on this branch added the redirect afterward, unverified by any of those runs, and is reverted here. Redirecting it would also force every run to re-fetch pyright's npm package from the registry instead of reusing a warm cache, trading an unobserved collision for a real, new failure mode: a registry hiccup reds a `tests (cihost)` run on a tree that passed elsewhere, which is exactly run 21's surface shape and would wrongly trip the D5 revert trigger. Left shared on purpose; D1 found no evidence it needs to move. |

### D3: three isolated and three control rounds, run by the operator

Per the plan's 2026-10-06 amendment, these rounds ran outside the fleet
(`bin/fleet-ci-stress`, plan 0128) rather than inside an implementer session's
90-minute bound. The operator ran them 2026-10-30/31; the records live under
`docs/ci-evidence/0113/`.

| round | kind | tree | cross-branch overlap (`tests (cihost)`) | jobs | result |
|---|---|---|---|---|---|
| 1 | isolated (D2 tree + D4 test change) | `0e03c4f5f5` | task `8782` (`ci-stress/0113-r1-a`) 20:51:25–21:12:22 ↔ task `8786` (`ci-stress/0113-r1-b`) 20:51:28–21:12:29 | 18 | all green |
| 2 | isolated | `0e03c4f5f5` | task `8826` (`ci-stress/0113-r2-a`) 23:15:43–23:36:36 ↔ task `8830` (`ci-stress/0113-r2-b`) 23:15:46–23:36:38 | 18 | all green |
| 3 | isolated | `0e03c4f5f5` | task `8844` (`ci-stress/0113-r3-a`) 00:08:22–00:29:14 ↔ task `8848` (`ci-stress/0113-r3-b`) 00:08:24–00:29:23 | 18 | all green |
| 1 | control (D4 test change, no D2) | `ba3440088c` | task `8866` (`ci-stress/0113-r1-a`) 00:59:36–01:20:06 ↔ task `8870` (`ci-stress/0113-r1-b`) 01:07:25–01:27:55 | 18 | all green |
| 2 | control | `ba3440088c` | task `8886` (`ci-stress/0113-r2-a`) 01:57:09–02:17:22 ↔ task `8890` (`ci-stress/0113-r2-b`) 01:57:13–02:17:51 | 18 | all green |
| 3 | control | `ba3440088c` | task `8904` (`ci-stress/0113-r3-a`) 02:47:19–03:08:00 ↔ task `8908` (`ci-stress/0113-r3-b`) 02:47:25–03:08:30 | 18 | all green |

108 jobs total, all green. Every round's counted pair satisfies the plan's
overlap rule (`A.start + 60s < B.updated_at` and `B.start + 60s < A.updated_at`)
on two DIFFERENT scratch branches' `tests (cihost)` jobs — the full task
records, including every job's `run_started_at`/`updated_at` and the branches
that did not count, are in the six `round-*.json` files.
`bin/fleet-ci-stress gate --out-dir docs/ci-evidence/0113` reports `GATE: PASS`.

**What the control result means.** Both isolated and control rounds came back
green — nothing was reproduced in these 18 overlapping runs. That is the
**both-green** case the plan's Gate names: run 21 is **not explained** by this
exercise, and this section does not claim it was. What D2 bought is a
measured absence of collision on the paths it redirected, under real
cross-branch overlap, not a diagnosis of what run 21 actually was.

**A gap this exercise did not close: `plans.yml`'s `lint` never ran its own
full suite.** D3 step 1 requires every scratch commit to touch a path under
`plans/` *and* a workflow file, so `ci.yml` always ran alongside `plans.yml`
on every scratch branch. `lint`'s own dedupe check (the `scope` step) exists
to skip the suite whenever a covering `ci.yml` run is found on the same head
— which is every scratch push, by that same construction — so `full_suite`
was `false` in all 18 `lint` tasks across all six rounds. Their recorded
durations confirm it: 14–34 seconds each, every one starting within a few
seconds of that branch's own `talos-contract` task finishing — far too short
for the suite `tests (cihost)` took 15–21 minutes to run, and consistent only
with `install`, the fast-lane subset and `fleet-plan lint` having executed.
So the pip-cache and `TMPDIR` redirects in `lint` were exercised, under real
overlap, by those unconditional steps — but `lint`'s `full suite (when the
change leaves the fast lane)` step itself was never measured by D3, and the
rows above should not be read as having covered it. That step only runs when
a change leaves `plans/`/`docs/adr/` on a head `ci.yml` skipped entirely
(e.g. a rebase-shaped push, FORCE PUSH case (c) in `ci.yml`'s own comment),
which D3's construction could not produce without contradicting its own
requirement to touch a workflow file. Measuring it would need a different
scratch shape than this plan's D3 recipe describes; it is not done here.

### D4: the group, narrowed to one lane per ref

With the Gate passing, all three workflows now key their `concurrency.group`
on `eunomia-${{ github.ref }}` rather than the literal `eunomia-cihost-workspace`
(`.forgejo/workflows/ci.yml`, `plans.yml`, `deploy-docsite.yml`). One ref's CI,
Plans and deploy-docsite runs still queue together, and successive pushes to
one branch still queue behind each other; different branches no longer wait on
each other. `cancel-in-progress: false` is unchanged in all three, for the
reason it always was: a cancelled run reports as `failure` on the commit
status (measured on `0a46e6f`), and the review dispatcher refuses a red build.

### D5: the revert trigger

Go back to `group: eunomia-cihost-workspace` in all three files (one PR against
`main` reverting the D4 commit and its `tests/test_ci_workflow.py` change) when
any eunomia CI job goes red on a tree that already has a green run elsewhere
(compare `git rev-parse <sha>^{tree}`, the run-21 signature) **and** the
failure does not reproduce locally under the same `pytest-randomly` seed
(`-p randomly --randomly-seed=<seed from the log>`; `-p` takes a plugin module
name, so the seed is never passed through it). A failure that reproduces under
its seed is an order-dependent test, not concurrency. Also revert if a red log
names one of this section's *shared, not redirectable* rows (act's cache, or
the Colima VM). This trigger does not expire — the signature means the same
thing whenever it appears. The same text is in the `concurrency:` comment in
`.forgejo/workflows/ci.yml`.

### Handoff

- **What was actually shared**: the pip cache, pytest's base temp (and
  therefore most of `/tmp`/`$TMPDIR`), and the pinned pyright build. All three
  are now per-run (D2). The checkout, `.venv`, `$RUNNER_TEMP` and the talos
  clone were already per-run, contrary to the comment run 21's fix originally
  shipped with. Act's own action cache and the single Colima VM remain
  genuinely shared and are not redirectable from a workflow file.
- **Run 21**: not explained by this plan. The tiphys probe (2026-09-07)
  disproved the "per-repository workspace" mechanism the original comment
  named; this plan's D3 rounds additionally found no collision across 18
  real overlapping runs on the paths D2 redirected. Neither result identifies
  what run 21 actually was.
- **The revert trigger** is above and in `ci.yml`'s `concurrency:` comment.
- `infra`'s `runbooks/forgejo-runner-capacity.md` ("Narrowing a repo's own
  concurrency group is not settled") is now out of date given this plan's
  result. It needs a separate operator/infra PR citing this one; not edited
  here, since it is another repo.
- **D5 applied, 2026-11-09, task `10567` on this plan's own implementation PR
  (#1442, tree `0b640b74`):** `tests (cihost)` went red with the *same seven*
  `tests/test_stalled.py::test_a_*` failures (`assert 0 == 1`,
  `fleet-stalled: scanning 1 repository`) a sibling branch,
  `fix/test-stalled-ignores-ambient-fleet-db` (commit `1ca3380`, not yet on
  `main`), independently diagnosed and reproduced with
  `FLEET_DB=/tmp/nonexistent` outside any CI run at all. The mechanism: two
  pre-existing test modules, `tests/test_fleet_reviews.py` and
  `tests/test_orchestrator_review_dispatch.py`, set `os.environ["FLEET_DB"]`
  directly (no `monkeypatch`, no teardown), and `tests/test_stalled.py`'s
  `_wire` fixture pins `EUNOMIA_FLEET_DIR` but never `delenv`s `FLEET_DB` —
  which `fleet-stalled`'s `_dispatch_db_path()` prefers over it. Whichever of
  those two runs last under `pytest-randomly`'s shuffled order leaves
  `FLEET_DB` pointed at a since-reused `tmp_path`, so every later
  `test_stalled.py` `failed-plan` test queries the wrong (or a now-absent)
  database and finds nothing. This is in-process state carried between two
  *tests*, not a resource two *runs* share — it reproduces at
  `concurrency: 1` exactly as it does here, and would have failed before D4
  on any random order that placed the leak first. Applying the D5 test: the
  failure is order-dependent, not concurrency (confirmed by an independent
  reproduction, not merely "could not reproduce locally" — five local
  `pytest-randomly` runs of just these two files did not trigger it, because
  the leak lives in two *other* files D3 never put in the same scratch run).
  **No revert.** The fix is one `monkeypatch.delenv("FLEET_DB", ...)` line in
  `tests/test_stalled.py`'s `_wire`, outside every one of this plan's
  `paths:` — it is not made here, per this section's own boundary against
  widening into `tests/**`. It ships on whichever branch owns that file
  first; `fix/test-stalled-ignores-ambient-fleet-db` already does.
- **Recurred, 2026-11-09, task `10570`, same PR (#1442, tree `1fa4929c`):**
  the identical seven `tests/test_stalled.py::test_a_*` failures, same
  mechanism, same two leaking files — confirmed again by local reproduction
  with an ambient `FLEET_DB` set. `fix/test-stalled-ignores-ambient-fleet-db`
  is now open as PR #1441 against `main`, mergeable, and still unmerged. This
  branch stays exposed to the flake on whatever `pytest-randomly` seed a push
  draws until #1441 merges; no action is taken on `tests/test_stalled.py` from
  here, for the same boundary reason as the first occurrence. Named in a
  comment on #1442.
- **Recurred again, 2026-11-09, task `10573`, same PR (#1442, tree
  `4e30185d`):** the identical seven `tests/test_stalled.py::test_a_*`
  failures, same mechanism, confirmed a third time by local reproduction with
  an ambient `FLEET_DB` set. `fix/test-stalled-ignores-ambient-fleet-db`
  (PR #1441) is still open and still unmerged. No action is taken on
  `tests/test_stalled.py` from here, for the same boundary reason as the
  first two occurrences — three failures on three different pushes to this
  one branch says the leak's order-dependent probability on this suite is
  high, not that it has become a concurrency symptom; it still reproduces at
  `concurrency: 1` outside any CI run. Named in a comment on #1442.
- **Recurred a fourth time, 2026-11-09, task `10576`, same PR (#1442, tree
  `ca9613d6`):** the identical seven `tests/test_stalled.py::test_a_*`
  failures, plus their expected derivative,
  `tests/lane/test_0170_stalled_held_contract.py::test_plan_test_suites_exist_and_pass`
  (it reruns `test_pr_hold.py` and `test_stalled.py` in a `pytest` subprocess
  that inherits the leaked `FLEET_DB` from the parent process's environment,
  so it fails for the same root cause, not a new one) — `8 failed, 4956
  passed` matches this. Confirmed a fourth time by local reproduction with an
  ambient `FLEET_DB` set. `fix/test-stalled-ignores-ambient-fleet-db` (PR
  #1441) is still open and still unmerged. This was a retry push (new
  `pytest-randomly` seed, no CI-configuration change) made in response to
  this round's "make the build pass" instruction; it drew another
  leak-triggering order. No further retry is made from here: the fix is a
  one-line change to `tests/test_stalled.py`, outside this plan's `paths:`
  lease, and repeatedly pushing to redraw a seed is not progress toward that
  fix. The build stays red until #1441 merges to `main` and this branch picks
  it up, or until a push happens to draw a non-triggering order. Named in a
  comment on #1442.
- **Recurred a fifth time, 2026-11-09, task `10579`, same PR (#1442, tree
  `a4d8ba85`):** the identical seven `tests/test_stalled.py::test_a_*`
  failures (`7 failed, 4957 passed`; this run's seed did not also trip the
  `test_0170_stalled_held_contract.py` derivative). This push was the docs
  commit recording the fourth occurrence, not a deliberate retry — the
  previous entry already said no further retry would be made, and this is
  that statement holding: a push made for an unrelated reason still redraws
  the seed and still lands on the leak. Five occurrences across five pushes
  to one branch (tasks 10567, 10570, 10573, 10576, 10579) is not a rare draw;
  this suite's `pytest-randomly` ordering hits the leak on most, perhaps
  nearly all, seeds. Retrying further is not a credible path to green from
  this plan's lease. The fix remains the one-line
  `monkeypatch.delenv("FLEET_DB", ...)` in `tests/test_stalled.py`'s `_wire`,
  sitting unmerged in PR #1441 against `main`, outside every one of this
  plan's `paths:`. This PR's build cannot be made reliably green until #1441
  merges. Named in a comment on #1442.

## Roadmap position

This is roadmap row 6a (see `ROADMAP.md`). The atlas Fleet tab that renders it is a
follow-on PR; the collector ships first because **history not collected is gone
forever**, while a view over collected history can be built any time.
