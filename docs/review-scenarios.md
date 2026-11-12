# Review scenarios: the review loop, rehearsed with no model and no forge

`tests/test_review_scenarios.py` runs the real `bin/orchestrator` as a
subprocess, the way `fleet-watch` spawns it, through every review outcome the
fleet has hit. Each outcome is one row. A regression in how the loop reads,
retries, fails or reports a review goes red in CI at no model cost. (Plan 0099.)

**The suite pins what the code does today. It does not judge it.** A row whose
pinned behaviour looks wrong says so in its docstring and is asserted as it is;
changing the loop is a separate, deliberate change that turns that row red.

## The three fakes

| Fake | File | Stands in for |
|---|---|---|
| Forge | `tests/fakes/forge.py` | Forgejo: pulls, reviews, issue comments, commit statuses, contents, and the ntfy POST at `/ntfy` |
| Reviewer | `tests/fakes/review.py` | the review dispatcher (`FLEET_REVIEW_CMD`) |
| Implementer | `tests/fakes/claude` | the `claude -p` session |

**Forge.** A threaded `http.server` on `127.0.0.1`, port 0, state in memory. It
serves only the `/api/v1` paths `bin/fleetforge.py`'s Forgejo backend builds for
the verbs the loop uses. Any other path answers **501 and is recorded**;
`assert_no_unmodelled()` (run at the end of every row) fails the row naming the
path. A request whose token is unknown gets 401, and the account the token maps
to authors whatever it creates. The forge and the scenario's bare git remote are
one thing: a pull's head sha is whatever the branch is in the bare repo now, so a
fix round moves the head with no bookkeeping. An `APPROVED` review marks the pull
merged (`merge_on_approve`), standing in for the human who merges, because
otherwise the steward would poll forever.

**Reviewer.** Reads a JSON list from `FAKE_REVIEW_SCRIPT`, one action per round,
counted in a sidecar file (`<script>.state`; every argv in `<script>.calls`).

| Action | Effect |
|---|---|
| `approve` | posts `APPROVED` on the PR's current head |
| `comment` + `findings` | posts `COMMENT` with a `MACHINE-READABLE FINDINGS` block |
| `request_changes` + `findings` | the same, as `REQUEST_CHANGES` |
| `unparseable` | posts `COMMENT` with prose and no findings block |
| `post_nothing` | prints `VERDICT: APPROVE`, posts nothing, exits 0 |
| `stale` | posts `APPROVED` on the previous head |
| `exit` + `code`, `stderr` | posts nothing, exits |
| `quota` | inserts a `review_attempt` row (`outcome='quota'`) into `FLEET_DB`, exits 75 |
| `probe` + `code` | answers `--probe-limit` (mopsus's `run_probe`); not a round |

Reviews are posted through the fake forge's HTTP API as the reviewer role's
account (`revbot`), the way the real dispatcher posts. **Its stdout means
nothing**: the orchestrator reads the verdict from the forge, and a fake that
approved on stdout would reproduce the 2026-09-07 failure inside the test that
exists to catch it. An exhausted script exits 98 and an unknown action 99.

The findings body is built by `findings_body()`, never typed into a row, and a
test round-trips it through the orchestrator's own `_FENCE` / `_SEV_LINE`.

**Implementer.** Reads `FAKE_CLAUDE_SCRIPT`: `work` (commit a file, flip the plan
to `done`, push the branch; the first also opens the PR), `noop`, or `exit`. A fix
round is simply its next invocation. Every briefing it was given is kept in
`<script>.prompts/<n>.txt`. The orchestrator gives the implementer an
allowlisted environment, so the scenario puts a two-line `claude` shim on `PATH`
that exports the script path and forge URL before exec-ing `tests/fakes/claude`.

## Production seams used

No production code changes. Everything below already exists.

- `FLEET_REVIEW_CMD` (`review_command()`), the reviewer.
- `claude` on `PATH` (`harness_argv`), the implementer.
- `FLEET_FORGEJO_URL`, and `FLEET_AUTHOR_TOKEN_CMD` / `FLEET_TOKEN_CMD` for tokens.
- `FLEET_NTFY_URL`, so pages are captured at `/ntfy`.
- `FLEET_DB`, `EUNOMIA_FLEET_DIR`, `FLEET_WORK_ROOT`, `FLEET_GIT_SSH_BASE`,
  `EUNOMIA_TRANSCRIPT_DIR`, `EUNOMIA_IMPL_RUNNER`.

**One thing is not production: sleeps.** `CI_POLL_SECS` (30 s), the CI-log poll
and the pull-read backoff have no environment override, and every review round
waits two CI polls. Each scenario therefore puts a `sitecustomize.py` on
`PYTHONPATH` that caps any `time.sleep` of a second or more at 20 ms. It changes
pace only. A seam for the poll interval would replace it.

Every URL in the subprocess env is asserted to be `127.0.0.1`, and the developer's
own `FLEET_*` / `EUNOMIA_*` variables are stripped, so a row cannot pass on one
machine and fail on another.

## The rows

1. `approve` → success, 1 round, 1 implementer run.
2. `comment[HIGH]`, then `approve` → success, 2 rounds, 2 runs.
3. `request_changes[HIGH]`, then `approve` → success, 2 rounds.
4. `comment[HIGH]` every round → fails at `review_bound` (5 rounds).
5. `comment[MEDIUM, LOW]` → fails `no parseable blocking finding` (pinned; see the row).
6. `unparseable` → `refusing to read an unparsed review as an approval` (#627).
7. `post_nothing` → `the verdict did not land` (2026-09-07).
8. `stale` → the earlier-head approval is not read.
9. `exit` with the `PermissionError` → `review dispatch failed: PermissionError`;
   mopsus classes it `unclassified` (plan 0095 changes that to `review-path`).
10. `quota`, `probe 0`, `approve` → the attempt is held; `bin/mopsus` finds it,
    probes, and re-runs the review, which posts.
11. red commit status → 0 review rounds, a CI-fix briefing instead.

## Adding a row

1. Write the reviewer script (and the implementer's, if it is not `work` every
   time). An empty script fails the row on purpose.
2. `scenario(reviewer, implementer=None, ci=None).run()` returns a `Result`:
   `rc`, `failure_reason`, `rounds`, `impl_runs`, `events`, `pages`, `ntfy`,
   `comments()`, `review_states()`, `reviewer_calls`, `implementer_prompts`.
3. Assert the outcome **as it is today**. If it looks wrong, say so in the
   docstring and list it for its own plan.
4. If the row makes the loop call something the fake forge does not serve, the
   row fails naming the path: add the route to `tests/fakes/forge.py`, modelled on
   what Forgejo really does.

A regression that turns a failure into a success makes the steward poll for a
merge that never comes, so an affected row ends at its 60 s subprocess timeout
rather than at an assertion. That is still red; it is just slow.
