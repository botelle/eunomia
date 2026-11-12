# The usage-limit gate

One piece of state per subscription records its usage limits: a **gate** per
scope in `fleet.db`'s `quota_gate` table. There are four subscriptions (Claude,
codex, gemini, muse), each with its own meter in UsageBar, so there is one gate
set per account: `session`, `weekly` and `credential` are Claude's, and
`vendor:codex`, `vendor:gemini` and `vendor:muse` are the others'. `bin/fleet-quota`
is the only writer (plan 0094). While a gate is closed, `fleet-watch` dispatches
nothing new **into a repo with a seat that spends that account** (D3), and
nothing else is held, so the owner can move a seat in lynceus off a limited
vendor and respawn from Minos. Once evidence shows the limit has reset, the gate
opens, and work a limit stopped before it began is requeued.

Until 2026-11-02 a closed gate held every repo, on the ground that the watcher
could not tell which routes spend the subscription, and the meter took the
highest percent across all accounts, so one vendor at its limit closed the gate
for the rest. The watcher can tell: it resolves the repo's seats the way the
pipeline does. That day the weekly limit held three merged plans whose seats had
already moved to codex and antigravity.

Measured on opshost, 2026-09-28: the session limit was hit twice in one day; 44
review runs then failed within seconds because nothing held the work that
followed; and two plans (eunomia 0085, lynceus 0017) died on `API 429 … resets
3:10am` with no work done and waited thirteen hours for a manual
`fleet-release --force`.

## D1 — the gate

Scopes: `session` and `weekly` (the Claude account's meter), `credential` (D6),
`vendor:<account>` for `codex`, `gemini` and `muse`, `model:<bucket>`, and
`review-path:<repo>` (plan 0095, below). A `vendor:<account>` gate closes when
**any** quota in that account's meter reads 100 (`resetsAt` is the latest of the
quotas at 100) and opens by the meter on the same rules as `session`: a fresh
reading below 100, dated after the close. It is not a holding scope in the old
sense (nothing holds the whole fleet) and mopsus probes no vendor gate. A bucket is UsageBar's
quota key for a model, today only `fable`; a model id maps to it when the
family token (the second hyphen-separated segment of `claude-<family>-…`)
equals the key. `claude-fable-5-1` is `model:fable`; `claude-sonnet-5` maps to
no scope.

`quota_gate` holds `scope`, `state` (`closed`/`open`), `closed_at`,
`resets_at` (nullable), `closed_by` (`meter`, `stop`, `probe`), `evidence` (a lease id,
`review_attempt:<rowid>`, or `meter`), `opened_at` and `opened_by` (`meter`,
`probe`, `owner`). Each transition is also a `quota_transition` row.

| subcommand | does |
|---|---|
| `observe` | reads UsageBar's `latest.json` and closes or opens gates |
| `close <scope> --resets-at <iso\|-> --by stop --evidence <id> [--at <iso>]` | records a stop |
| `open <scope> --by owner` | the owner's override, recorded like any other |
| `open review-path:<repo> --by owner [--for <hours>]` | the override, plus a window (default 24, 1–168) in which a failing probe still prints and pages but does not re-close the gate; the window is a `quota_transition` row, evidence `override` |
| `probe-review-path <repo>` | runs the review dispatcher's pinned calls; exit 0 fine, **75 broken**, 1 cannot judge; writes `review-path:<repo>` |
| `check [--scope S]` | exit 0 open, **75 closed**, 1 unknown; prints `closed <scope> until <resets_at or unknown>` |
| `status --json` | the rows |

**`observe`.** A snapshot is fresh when its `dataAsOf` is under 30 minutes old
— never `generatedAt`, never the file's mtime. For each scope in a fresh
snapshot: `percent >= 100` closes the gate `by meter`, `resets_at` from
`resetsAt`; `percent < 100` opens it `by meter` **only if `dataAsOf` is later
than the gate's `closed_at`**, because a reading taken before a stop says
nothing about that stop. A stale, missing or unparseable snapshot changes
nothing, and with no meter at all the fleet behaves exactly as before.
`session`, `weekly` and `model:*` read `accounts.claude` alone
(`FLEET_QUOTA_ACCOUNT` names another account for them); `accounts.codex`,
`.gemini` and `.muse` each feed their own `vendor:<account>` gate, never a
Claude one. The file is `FLEET_QUOTA_LATEST` or
UsageBar's `~/Library/Application Support/UsageBar/latest.json`.

**One stop closes once.** `close` changes nothing when the scope is already
closed, when that evidence already closed it, or when the evidence is dated at
or before the scope's last `opened_at`. A stop that happened before a reset
cannot undo it. The same holds for a meter reading dated before the last open.

**Transitions.** Each emits `quota-closed` or `quota-opened` (`scope`, `by`,
`evidence`, `resets_at`, `percent` when there is one). For `session` and
`weekly` it also sends one page: `fleet held: <scope> limit, until
<time|unknown>` or `fleet released: <scope> reset (<by>)`. A write that changes
nothing emits and pages nothing.

## D2 — where the gate is closed and observed

- `fleet-watch`'s `cycle()` runs `observe` first: a local file read and one
  database write, before any forge read.
- **An implementer stop.** When mopsus classifies a failure as `quota`, it
  closes before composing the brief, `--evidence <lease id>`. The scope is
  `weekly` when the reason names a weekly limit, else `session`. `resets_at` is
  the reason's `resets <time> (<zone>)`, resolved to the **first occurrence
  after the failure's own timestamp** (`current_phase_ts`): a `3:10am`
  (America/New_York) failure at 23:30Z on the 28th resolves to 07:10Z on the
  29th. An unparseable time is unknown.
- **A review stop.** A `review_attempt` row with outcome `quota` closes
  `model:<bucket>` for that row's model, `--evidence review_attempt:<rowid>`,
  and never `session` or `weekly`. A model with no bucket closes nothing. An
  account-wide limit is seen by the meter or an implementer stop; guessing
  `session` from a review stop would hold all dispatch on what may be one
  model's limit.

## D3 — a closed gate holds the phase that spends it

The watcher checks the implementer account before fresh dispatch, respawn,
redispatch or answered-resume. A held `respawn` stays fresh and a held
`resume_request` stays unconsumed. The implementer resolves as `main()` does,
including `EUNOMIA_IMPL_RUNNER`. An unknown implementer holds behind any closed
gate. Housekeeping, pages and pin checks still run.

Completed plan reviews and unused style, tester, lane-reviewer or mediator seats
are not prerequisites for implementation. A Codex implementer may start while
Claude is exhausted even if the repository has a Claude style reviewer.

After the PR exists and full/style routing has selected a reviewer position,
the orchestrator checks only that position's enabled ordinals before dispatching
its review. A closed account reports the scope and selected seat to move in
lynceus; no reviewer is spawned for that held round. A style route falling back
to full review checks `code_reviewer`, not `style_reviewer`. An unreadable review
gate holds that review. Existing lane/meeting scheduling is unchanged.

Accounts are mapped by runner credential and family, not runner name. Claude
reviewer accounts spend the operator's subscription; a venture implementer uses
its own principal's credential. Vendor CLI families map to `codex`, `gemini` or
`muse`. API and epeius runners spend no subscription account. `model:*` gates
do not hold dispatch.

## D4 — a reset is probed for, for every closed gate

mopsus probes each closed gate that is due, through `run_probe`
(`dispatch-review.py --probe-limit --model <id>`), detached under the one
review-retry lock. `model:<bucket>` uses the stopped attempt's model;
`session` and `weekly` use `claude-sonnet-5` (`QUOTA_PROBE_MODEL` in
`bin/mopsus`), whose family has no bucket, so its answer reflects only the
account-wide limits. A gate with a known `resets_at` is due at `resets_at` + 2
minutes and not before, then every `review_probe_minutes`; one with none is due
every `review_probe_minutes`. An answered probe opens the gate it was run for,
`by probe`. Any other outcome opens nothing (ADR-0016 §3). `fleet-quota open
<scope> --by owner` is always available.

A `model:<bucket>` gate closed by the meter alone, with no stopped review
attempt to name a model, has no probe; the meter or the owner opens it. It holds
no dispatch, so nothing waits on it.

A `model:<bucket>` gate that opens, by the meter, a probe or the owner, while
neither `session` nor `weekly` is closed, also starts a **drain**: mopsus retries
the reviews that gate stopped, `review_drain_concurrency` at a time, without a
probe per row, and sends one page (ADR-0028 §2-§3; `docs/mopsus.md`). While
either account-wide gate is closed a `model:` reopening drains nothing, because
the bucket's meter cannot see an account-wide limit.

## D5 — a `quota` failure with no work is requeued once the gate opens

A `quota` failure is recorded in mopsus's state as `{"held": <scope>}`
(mechanical mode). It is escalated once, its brief carrying `held:      <scope>
closed until <time>`, and re-read on **every** sweep while it stays held. On the
first sweep that finds the gate open, mopsus requeues it exactly as ADR-0012 §1
does — `plan-requeued` first, then `fleet-release --force`; counted against
`retry_limit`; no marked PR — and records `{"requeued": true}`. At
`retry_limit`, or when any ADR-0012 condition fails, the entry becomes an
ordinary paged entry with the reason on the brief. `quota` is retryable only
through this gate condition, never unconditionally, and a `quota` failure
*after* work began is not a `quota` failure at all (`backend-after-work`) and is
never requeued.

## D6 — the `credential` scope (plan 0116)

The implementer authenticates with a minted OAuth token (`docs/plan-dispatch.md`,
"The implementer's credential"). When the CLI rejects it, retrying fails
identically, so that is a gate too, on the same table and the same
`quota-closed` / `quota-opened` events.

- **Closing.** The orchestrator ends a run whose final `result` record is an
  auth rejection (`is_error` with `api_error_status` 401/403, or "Failed to
  authenticate", "Not logged in", "OAuth session expired", "Invalid API key"),
  or whose token fetch failed, with a `plan-failed` reason starting
  `implementer credential rejected:`. mopsus classifies that `credential` and
  closes the `credential` gate with `resets_at` unknown and the lease as
  evidence. The orchestrator never closes it itself: mopsus is the one writer
  that turns a failure into a transition. So the gate closes on mopsus's next
  sweep, and up to `dispatch_cap` runs may fail before it holds.
- **Holding.** `credential` is the Claude account's token, so it holds as
  `session` and `weekly` do (D3): the repos with a seat that spends `claude`,
  and no others. The page reads
  `fleet held: the implementer credential was rejected`.
- **Opening: the owner only.** After a re-mint:
  `fleet-quota open credential --by owner`. `observe` cannot open it (the meter
  knows nothing about credentials) and mopsus starts no probe for it: a probe
  is a model call on a schedule against the credential the gate says is dead,
  and a credential does not recover by itself.
- **Requeue.** Once open, mopsus releases a held `credential` failure that has
  no marked PR, as it does a `quota` one, and leaves one with a marked PR alone.
  `credential` is not in `RETRYABLE_CLASSES`: the gate decides when it retries,
  not `retry_limit`.

## The `credential:<principal>` scope (plan 0125)

A venture's repository spends the venture's credential (`docs/plan-dispatch.md`,
"Which credential a run spends"), so its rejection must not stop the operator's.
When the reason reads `implementer credential rejected (principal <name>): ...`,
mopsus closes `credential:<name>` instead of `credential`; the operator's reason, and
so its fleet-wide gate, is unchanged.

- **Scope of the hold.** `fleet-quota held(principal=<name>)` returns the fleet-wide
  gates and that principal's own; `held()` without a principal, and for the operator,
  never sees a venture's gate. `credential:<name>` is deliberately not in
  `HOLDING_SCOPES`.
- **Same rules otherwise.** `resets_at` unknown, no probe, only the owner opens it
  (`fleet-quota open credential:<name> --by owner`), and mopsus then requeues what it
  held. The page names the principal.
- **Not yet wired in `bin/fleet-watch`.** It still calls `held()` once per cycle,
  fleet-wide, so a venture's gate is closed and recorded but the watcher does not
  consult it per repository until a plan in `fleet-watch`'s lane passes
  `principal_of(repo)` to `held()`.

## The review-path scope (plan 0095, ADR-0018)

`review-path:<repo>` holds dispatch into ONE repo while the review dispatcher
cannot run its pinned prerequisites there. It is not a usage limit and never
holds `session` or `weekly`.

**`fleet-quota probe-review-path <repo>`** reads the calls from
`~/agent/contracts/eunomia-calls.json` (talos's contract for what
`dispatch-review.py` runs; `FLEET_REVIEW_CONTRACT` overrides the path) and runs
each entry, for each of its `positions`, as `subprocess.run([<pin>/bin/<program>,
…])` under `~/.local/share/pins/eunomia/bin` (`FLEET_REVIEW_PINS`). The program
is exec'd, never imported: an import passes on a lost exec bit, which is the
fault it exists to catch. It applies the entry's rule
(`json-list-first-ordinal-1`, `json-object-value-in`). It never changes the
host: no `chmod`, no schema, no setting.

*Contract shape, verified against the call sites.* The reader requires the
dict-form `accept` (`{"rule": …, "values": […]}`) that talos
`contracts/eunomia-calls.json` v1 carries (talos plan 0016, commit `d045409`;
present in the `~/agent` checkout at `18e49f7`). Its two entries match
`dispatch-review.py`'s `resolve_reviewers` (`[FLEET_MODELS, "reviewers", repo,
position, "--json"]`, `[0]["ordinal"] == 1`) and `resolve_review_bar`
(`[FLEET_CONFIG, "get", "review_bar", "--repo", repo, "--json"]`, `value` in
low/medium/high). `tests/test_review_path_gate.py` loads that shape, and the
real deployed file where present, so a talos shape change fails CI here rather
than making the probe inert (exit 1) or holding the fleet (exit 75).

A later contract version is read as long as `v` is an integer >= 1. Calls whose
`accept` rule the probe does not know, or that have none, are skipped, never
run: v2 (talos plan 0029, commit `2ddb17e`, 2026-10-17) adds `fleet-credits
decide` (`json-credits-decision`) and `record` (`exit-zero`), and `record` books
spend. A skipped call's program must still be present and executable, or the
gate closes; the ok line names the skipped calls. From 10-17 until this change the probe refused v2 outright and could not
judge on opshost.

| exit | meaning | gate |
|---|---|---|
| 0 | every call it has a rule for passed, and every skipped call's program is executable | opens `review-path:<repo>` `by probe` |
| 75 | a call failed, or a pinned program or the pin directory is missing | closes it `by probe` |
| 1 | the probe cannot judge: contract missing or unparseable, `v` not an integer >= 1, no call with a rule the probe knows, or an `argv` that does not render | changes nothing |

Every call is rendered before any is run, so a template the probe cannot
render (a `{`/`}` left after substituting `{repo}` and `{position}`, `positions`
with no `{position}` in `argv`, or `{position}` with no `positions`) exits 1 even
when an earlier call would have failed. Pinned programs run with
`FLEET_TOKEN_CMD`, `FLEET_ADMIN_TOKEN_CMD` and `FLEET_DIRECT_TOKEN_CMD` removed
from their environment. A `review-path` scope is closed only by this subcommand;
`close --by stop` on one is refused.

It prints one line: the repo, the failing entry `id`, and the exception class or
exit code, plus the first 200 characters of stderr. A close's `evidence` is
`probe:<UTC timestamp>:<entry id>`, unique per run, so the same fault after an
open closes the gate again despite "one stop closes once".

Only that subcommand opens a `review-path` gate `by probe` (`open --by probe` on
one is refused), apart from the owner's `open --by owner`. **0094's probe loop
(D4) skips `review-path:*`**: a model answering is not the review path working.
No transition pages through 0094's `session`/`weekly` page; `fleet-watch` pages
per cycle instead. `fleet-watch` probes a repo before it spawns into it, from
the scan or the orphan sweep, and re-probes every closed `review-path:*` scope
once per cycle, so each has an opener. It sends at most one `review path broken:
<repo>, … — <first line>` and one `review path restored: <repo>, …` page per
cycle, and one `review-path probe cannot judge: <reason>` per spell, which holds
nothing (and stays silent on a host with neither the contract nor the pin, which
runs no dispatcher). The summary line names each held repo. A gate mopsus's own
probe closed first is still paged once, by the cycle that first finds it broken
(a per-repo marker, cleared when a probe passes); an owner-override page is
likewise re-armed when the window ends and the gate closes again.
**ADR-0016's review retry waits too**: while a repo's `review-path` gate is
closed, mopsus's held-review queue skips its rows, so a retry is not spent
against a path known to be broken.

## For one-off scripts

```bash
fleet-quota check --scope session; rc=$?
[ "$rc" -eq 75 ] && { echo "usage limit closed — not starting another run"; exit 75; }
```

Stop on **75** and do not start another run. Exit 1 means no gate has ever been
recorded, which is not a reason to stop. This is opt-in; nothing enforces it on a
script that does not ask.

## What never opens a gate

Not text, and not the clock. The reset time in a CLI message is the backend's
wording, and `resetsAt` said 2026-09-29 16:00Z both before and after the weekly
and Fable meters dropped to near zero on 2026-09-27. Only a fresh meter below
100 dated after the close, a probe the model answers, or the owner opens a gate
(the `credential` gate: the owner alone).
Text and `resetsAt` may close one, or schedule the first probe: that is the safe
direction. `fleet-quota` never reads UsageBar's credential and never calls
Anthropic's API; `latest.json` is the only thing it reads.
