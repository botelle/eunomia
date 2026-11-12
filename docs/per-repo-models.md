# Per-repo models — a repo chooses which runner serves each agentic position

*Plan 0048. Closes the gap ADR-0009 named: the fleet's own history could not
answer "does a bigger model change churn enough to justify the cost?" because
every one of the 34 routed dispatches used `sub-sonnet` — there was no second
arm, and no way to run one except a per-invocation environment variable.*

## What this is

`bin/fleet-models` adds two tables to `fleet.db` — `repo_model` and
`repo_model_change` — and is the one program that writes them. A human and
the control app's write route (a later plan; not built here) both go through
it, so every change lands exactly one `repo_model_change` row (ADR-0006 §1 as
amended: a configuration change replaces a reviewed merge for this class of
setting, and this is the audit trail traded for it).

```
fleet-models list                                    every row, as stored
fleet-models get <repo>                               resolved runner, per
                                                       position, with layer
fleet-models set <repo> <position> [<ordinal>] <runner>
fleet-models disable <repo> <position> [<ordinal>]    ordinal defaults to 1
fleet-models enable  <repo> <position> [<ordinal>]
```

`bin/orchestrator` is the one reader that matters operationally: it resolves
the `implementer` position from `repo_model` on every dispatch
(`resolved_implementer_env`), which is what replaced `DEFAULT_RUNNER` as the
first choice a run makes.

## The positions, and why there are exactly seven

ADR-0009 §1 sorted every position in the dispatch flow into mechanical (a
code guarantee) or agentic (a model runs there). Only the agentic ones get a
row — a dropdown that changes nothing is worse than a missing one, and the
first attempt at this screen listed seven roles before three of them turned
out to have no model at all (`fleet-watch`, `fleet-bundle`, and the
orchestrator wrapper itself).

| position | cardinality | default |
|---|---|---|
| `implementer` | exactly 1 | the `default` row |
| `plan_reviewer` | 1..n | 1 |
| `code_reviewer` | 1..n | 1 |
| `tester` | 1..n | 1 (ADR-0006 §3 as amended: one or more) |
| `mediator` | 0 or 1 | none |
| `impl_supervisor` | 0 or 1 | none (ADR-0009 §2) |
| `test_supervisor` | 0 or 1 | none (ADR-0009 §2) |
| `style_reviewer` | 1..n | none (ADR-0025 §4) |
| `lane_reviewer` | 1..2 | none (plan 0163) |

**The fleet watcher does not appear.** It is fleet-scoped, and a per-repo row
for it would imply the fleet can have more than one dispatcher.

Today, only `implementer` has a consumer (`bin/orchestrator`). The other six
positions can be set and read through `fleet-models` — the schema and the CLI
do not distinguish "wired" from "not yet wired" — but nothing dispatches a
`plan_reviewer` or a `tester` by reading this table yet. Wiring those is
follow-on work, not a gap in this one.

## Resolution: repo, then default, then nothing

For one `(repo, position, ordinal)` slot: the repo's own row where
`disabled = 0`, then the `'default'` row for that same position/ordinal where
`disabled = 0`, then **nothing** — there is no further, built-in fallback a
misconfigured repo silently lands on.

For `implementer` specifically, `bin/orchestrator`'s `EUNOMIA_IMPL_RUNNER`
sits **above** this whole table — the operator's own override always wins,
and it is what makes a bakeoff arm a one-line env var rather than a database
write. That override belongs to the orchestrator, not to this module:
`resolve_repo`/`resolve` never look at it.

**No model is inferred for a repo that has not chosen one.** The `default`
row is the whole mechanism — not tier, not language, not repo size. A repo
with no row of its own reads exactly what `default` says, and deleting the
`default` row for a position makes the next dispatch **stop**, not silently
fall back to some constant. `config/impl-bounds.conf` established the reason:
a setting that silently reverts to a code default is an unattended process
spending more than the operator set, and a model doing the same is the same
defect, just harder to notice because the run still succeeds.

**One exception, and it is a bootstrap concession, not a loophole.** An
install where `repo_model` has never been created at all — no `fleet-models`
write has ever happened here — is not "misconfigured," it is "has not adopted
this feature yet," and `bin/orchestrator` falls back to its own
`DEFAULT_RUNNER` exactly as it always did. The first `fleet-models` write
anywhere creates the table, and from that point on the strict rule above
applies in full — including to a `default` row someone then deletes.

## Removal is `disabled = 1`, and an ordinal is never reused

A row is never deleted. `fleet-models disable <repo> <position> [<ordinal>]`
sets `disabled = 1` and leaves the row exactly where it was; `enable` reverses
it. Adding a new reviewer after disabling #2 of 3 allocates ordinal **4**, not
2 — the change log, and later, review records, name ordinals, and renumbering
would silently rewrite what an existing record meant.

`ordinal` is optional on `set`. A singleton position (`implementer`,
`mediator`, `impl_supervisor`, `test_supervisor`) always targets ordinal 1.
A list position (`plan_reviewer`, `code_reviewer`, `tester`, `style_reviewer`) allocates the
next ordinal that has never been used for that `(repo, position)` — the
maximum existing ordinal plus one, counting disabled rows, so a slot is never
handed out twice.

## A runner name is validated against `RUNNERS`, at write and at read

`bin/orchestrator`'s `RUNNERS` registry is a Python dict, not a SQL fact, so
it cannot be a `CHECK` constraint. `fleet-models set` refuses to write a name
`RUNNERS` does not carry, with the known names in the error. A row can still
go stale if a runner is later retired from the registry — `resolve` (and
therefore `get` and `bin/orchestrator`'s read) raises the same refusal in that
case, rather than silently dispatching to a name that no longer means
anything. This is `impl-bounds.conf`'s rule again: a hard stop that was
deliberately reachable (a runner declared `unwired`) must stay a hard
stop, not become a dispatch-time crash on some repository nobody was
watching.

## A runner has a provenance class, which is a record and refuses nothing

Authority: [ADR-0011](adr/0011-a-runner-has-a-provenance-class.md), as amended by
ADR-0026 §§1 and 3, built by plan 0072 and changed by plan 0153. Every `RUNNERS`
entry still declares `provenance` (`us-hosted`, `us-open`, `cn-open`) and a
runner with none is a registry error, but no class and no locality keeps a
runner out of a position. The old position table and `ProvenanceRunnerRefused`
are gone. The box that `bin/orchestrator` enforces at spawn time for open
runners (no credential, no extra environment) is unchanged.

## Every position needs a full model version; reviewers also need a spawn path

*Plan 0153, ADR-0026 §§4-5.* Each `Runner` declares its model id in a `model`
field, and `fleet-models` reads that field (`runner_model`), never the name and
never argv. `validate_runner(name, position)` refuses, at write and at read:

- at **every** position, a runner whose `model` is not a full version: a name
  carrying a digit, from any family (`claude-opus-5-5`, `gpt-6-astra`,
  `qwen3.6:35b-a3b-q8_0`); `sonnet`, `opus`, an empty string and `None` fail;
- at `plan_reviewer`, `code_reviewer` and `style_reviewer`, a runner that is
  `unwired`, with its reason;
- at those same positions, a runner whose harness talos cannot launch (plan 0157).
  `runner_harness(runner)` returns one of four values, derived from the declared
  binaries (first match wins: `EPEIUS_BIN`, `ANTIGRAVITY_BIN`, a `claude`
  basename, `CODEX_BIN` in argv; an optional `harness` field overrides, and a
  test asserts it agrees). Derived, not a required field, so runners added by
  later plans are covered without editing them. all five (`claude`, `epeius`, `codex`, `antigravity`, `muse`) are in
  `REVIEW_HARNESSES` and launchable. `muse` is launchable by talos plan 0035
  (operator/talos#216, #218) only: `sub-muse` declares `harness="muse"` and an empty
  argv, so eunomia never spawns it as a reviewer or tester, and `validate_runner`
  refuses it at `tester` and `mediator` ("no implementer/tester spawn path for
  muse yet"). It implements through its `writer_argv` (below). Its model is `muse-wonder-1.0`, the versioned id of the owner's
  preference, Muse Wonder. Muse's server accepts it on the owner's subscription,
  and refuses the bare `muse-wonder` (measured 2026-10-30). It replaced
  `muse-spark-1.3` that day. `codex` and `antigravity` run through the
  root-owned `review-as-revbot` wrapper as the reviewer uid
  ([talos plan 0033](https://forge.example/operator/talos), operator/talos#190).
  A runner whose harness derives to none is still refused. Consumers are talos
  plans 0032 and 0033.

`fleet-models reviewers <repo> <position> --json` adds `harness` to each
reviewer; an `epeius` reviewer also carries `backend`, `endpoint`, `vendor_key`,
`digest`, `num_ctx`, `reply_reserve`, `max_output_tokens` and `max_input_tokens`
(`null` where the kind does not apply), each read from the runner's declaration.

**Live check (pending).** One review by `local-gemma4-31b` on a throwaway pull
request on a bakeoff repository, with its verdict and provenance trailer, is
still to be recorded here; it needs talos 0032 deployed.

```
$ fleet-models set operator/eunomia code_reviewer sub-codex
fleet-models: 'sub-codex' cannot serve code_reviewer: no review spawn path yet
for its harness 'codex' (launchable: claude, epeius; ADR-0026 §4)
```

Fable is the default by configuration, not by code. The owner records it once
per position:

```
fleet-models set default code_reviewer sub-fable
fleet-models set default plan_reviewer sub-fable
```

**Aliases were fixed, not refused.** `sub-sonnet`, `sub-opus` and `api-sonnet`
keep their names (`DEFAULT_RUNNER` and stored rows refer to them) and now pin the
full id their alias resolved to when measured, so what they run is unchanged.

| runner | declared model | measured 2026-10-24 by |
|---|---|---|
| `sub-sonnet`, `api-sonnet` | `claude-sonnet-5-5` | `claude -p "say ok" --model sonnet --output-format json`, `modelUsage` key |
| `sub-opus` | `claude-opus-5-5` | the same with `--model opus` |
| `sub-fable` | `claude-fable-5-1` | already pinned |
| `sub-opus-5-5`, `sub-sonnet-5` | `claude-opus-5-5`, `claude-sonnet-5` | already pinned |
| `sub-codex` | `gpt-6-astra` (`-m`) | codex's own session record under `~/.codex/sessions` for runs given no model (`"model":"gpt-6-astra"`); `codex exec` was not re-run |
| `sub-codex-sol` | `gpt-6.1-sol` (`-m`) | `~/.codex/models_cache.json` (plan 0202); built from `sub-codex`'s own template, so it carries the same confinement |
| `sub-codex-terra` | `gpt-6.1-terra` (`-m`) | `~/.codex/models_cache.json` (plan 0202); same template |
| `sub-codex-luna` | `gpt-6-luna` (`-m`) | `~/.codex/models_cache.json` (plan 0202); same template |
| `sub-antigravity` | `gemini-4-argon-high` (`--model`) | `antigravity models`, 2026-10-30: the highest Gemini 4 Argon tier it offers (`-low`, `-medium`, `-high`). The owner chose the highest model for reviewer seats on 2026-10-30. Until then this was `gemini-4-argon-medium`, the CLI's default label in `~/.gemini/antigravity-cli/cli.log` |
| `sub-antigravity-flash` | `gemini-3.8-flash-high` (`--model`) | `antigravity models`, read for plan 0202; built from `sub-antigravity`'s own template |
| `sub-haiku` | `claude-haiku-5-5` (`--model`) | the catalog id itself (plan 0202); seated through `_claude_argv` exactly like `sub-sonnet-5` |
| `sub-muse` | `muse-wonder-1.0` (no argv: talos launches it) | a one-prompt `muse exec --model muse-wonder-1.0` answered on the owner's subscription, 2026-10-30, while `muse-wonder` and `muse-wonder-full-1.0` were refused by the server ("does not exist or you lack access"). `muse model-profile show` resolves any name, so it proves nothing about access. Replaced `muse-spark-1.3` (2026-10-29) |
| `hosted-glm-5.3` | `z-ai/glm-5.3-20260816` | OpenRouter's `canonical_slug` for `z-ai/glm-5.3`, 2026-10-29 (plan 0172) |
| `local-qwen` | `qwen3.6:35b-a3b-q8_0` | agent-bus `poll.sh` `MODEL_CODE`; wired through epeius by plan 0154 |

`sub-codex-sol`, `sub-codex-terra`, `sub-codex-luna`, `sub-antigravity-flash`
and `sub-haiku` (plan 0202) are implementer and reviewer runners, like their
base — none of them is a tester: no tester adapter exists for any of these
CLIs yet (see the `tester` section below), so seating one at `tester` stops
loud at `fleet-lane`'s `check_runner`, which is not a regression.

**There is no same-family refusal** (ADR-0013 §6). Sonnet may review Sonnet; the
review's provenance line says so.

**Enforced at write and at read.** `set` refuses at write. `resolve` — and so
`get`, and any reader that goes through it — refuses the same way at read, so a
row inserted directly, or one that predates a refusal, does not stay live by
surviving.

### Which reviewers does a repo have

```
$ fleet-models reviewers operator/eunomia code_reviewer --json
[{"ordinal": 1, "runner": "sub-fable", "model": "claude-fable-5-1",
  "family": "fable", "layer": "default"}, ...]
```

One object per enabled ordinal, in order. It exits 3 when ordinal 1 does not
resolve, naming the fix (`fleet-models set default code_reviewer sub-fable`).
`position` is limited to the two reviewer positions. This is what
`~/agent/dispatch-review.py` (`operator/talos`) asks; reviews are not routed
through `bin/orchestrator`, because plan reviews are dispatched by the author
session and the one place both paths pass through is `dispatch-review.py`.

### The A/B recipe

```
fleet-models set operator/eunomia code_reviewer sub-opus-5-5
```

adds ordinal 2 for that repo, which annotates beside Fable's verdict at ordinal
1 (the default). Disable it with `fleet-models disable operator/eunomia
code_reviewer 2`.

Setting a reviewer row changes nothing about what runs until `dispatch-review.py`
reads `reviewers`; that is its own plan in `operator/talos`.

## The `tester` position can now name a vendor, and nothing reads it yet

*Plan 0066.* `bin/orchestrator`'s `RUNNERS` registry carries two vendor
runners, `sub-antigravity` (Google's Antigravity CLI) and `sub-codex` (OpenAI's
Codex CLI), so a repo can record

```
fleet-models set operator/eunomia tester sub-antigravity
fleet-models set operator/eunomia tester 2 sub-codex
```

and read both back, in ordinal order, exactly as any other `tester` row does.
Both are cloud backends behind ADR-0006 §7's zone allowlist: only a
`zone: public` plan can resolve either, the same rule that already gates
`sub-sonnet` and the rest of the Claude-family runners.

**`sub-codex` is registered, confined and wired.** `codex exec -s
read-only` confines writes, not reads (ADR-0006 §6); plan 0101 supplies the
isolation outside the CLI as a macOS sandbox profile at the front of the
registry argv (`config/codex-tester.sb`, see `docs/test-lane.md`). It was wired
on 2026-10-13, on the confined-versus-bare live measurement that plan requires.

**Setting a `tester` row changes nothing about what runs**, and not because of
a bug: **nothing dispatches a test lane today.** ADR-0006 §1's flag has no
reader (`plans/0013-fleet-repo-enable.md`'s main-via-contents-API half is
unbuilt), and no code anywhere constructs a tester spawn — no worktree, no
cwd-outside-every-work-root, no settings precondition applied at spawn time.
Recording a runner here is exactly as inert as recording a `plan_reviewer` or
`code_reviewer` row was before plan 0065 gave those a consumer: real, safe to
hold, in the change log and the ledger, and not consumed by anything until
plan 0013's other half exists and something calls `check_settings_precondition`
and builds the blind workspace ADR-0006 §6 requires before it spawns either
vendor CLI.

## Where the rows live

`repo_model` and `repo_model_change` are tables in `fleet.db`, alongside
`review` / `model_price` — `bin/fleet-reviews` is the precedent for a
non-derived, directly-written table sharing that file with `bin/fleet-collect`'s
derived history. See `docs/fleet-db.md` for the full two-tier account: which
tables `fleet-collect` can throw away and rebuild from transcripts, which it
must never touch, and why `bin/orchestrator` reading `repo_model` is not the
staleness risk that doc warns dispatchers away from elsewhere.

## What this plan does not do

**It does not serve HTTP.** The control app's read and write routes are a
later plan. Building them before anything obeyed this table would put an
endpoint in front of a setting nothing consumed yet — the orchestrator
obeying the table is the risk worth taking first; the screen is not.

**It does not wire `plan_reviewer`, `code_reviewer`, `tester`, `mediator`,
`impl_supervisor` or `test_supervisor` into a dispatcher.** Only
`implementer` has a consumer today. The schema and CLI treat all seven
positions alike so that wiring the rest later is a read, not a migration.

## `style_reviewer` (plan 0150, ADR-0025)

The position that reviews a pull request routed to the style review. It is a
reviewer position like `code_reviewer`: it takes ordinals 1..n of pinned, hosted
runners, and only `us-hosted` ones. It is a list position, and no row is written
by code: which runner reviews is owner configuration. A repository takes the style
review only when `style_reviewer` resolves **as many ordinals as `code_reviewer`**
(`docs/style-review.md`).

Adding it widened `repo_model`'s position `CHECK`, which SQLite cannot alter. The
table is rebuilt once by `fleet-models`'s `_migrate`, which `connect()` and
`fleet-db-migrate` both run: one `BEGIN IMMEDIATE` transaction that creates the new
table, copies every row, compares counts, drops the old table and renames it, and
rolls back on any mismatch. The columns are unchanged, and a second run does nothing.
`fleet-arena` keeps its own seven positions (`ARENA_POSITIONS`): its cells record past
bakeoffs, and its table was not rebuilt.

## `lane_reviewer` (plan 0163, ADR-0019)

The position that reviews a test-lane pull request (`agent/tests/**`). It is a
reviewer position like `code_reviewer`, with every reviewer check unchanged, but
**it holds at most two ordinals**: `set` at ordinal 3 or higher is refused,
because ADR-0019 names exactly two reviewer accounts and a third row would make
`expected_reviewers` raise `Stop`. No row is written by code; which runner reviews
is owner configuration. Ordinal 1 posts as revbot, ordinal 2 as rev2bot.

**Fallback.** The orchestrator's lane review (`review_opened_lanes`) and
`fleet-meet` expect `lane_reviewer` when `fleet-models reviewers <repo>
lane_reviewer --json` exits 0, whatever the count. On exit 2 (an older pin) or
exit 3 (no reviewer) they expect `code_reviewer` and log one line naming the repo
and the exit code. Any other failure is a `Stop`; nobody is never expected.

**A one-row seat.** talos then posts one review and the orchestrator expects that
one account. `fleet-merge` still requires both reviewer accounts for a `tests` PR
(ADR-0019 §4), so on an autonomous repository that lane waits for the owner
instead of auto-merging.

Which model reviews is decided by talos plan 0034, not here: lane reviews leave
the `code_reviewer` models only when both plans are live and the rows are set.
The `repo_model` CHECK was widened the same way as for `style_reviewer`, by
`_migrate`, which now runs when the stored CHECK does not name `'lane_reviewer'`.

## The local runners (plan 0154)

Every non-duplicate ollama model on cihost, plus Muse Glimmer, is an implementer
runner, spawned through epeius. Detail and the run recipe: `docs/epeius-runners.md`.

| runner | model | digest | class |
|---|---|---|---|
| `local-qwen` | `qwen3.6:35b-a3b-q8_0` | `0218f872e86b` | cn-open |
| `local-qwen3.8-27b` | `qwen3.8:27b-q8_0` | `8f5fb6b71ea0` | cn-open |
| `local-qwen3.6-27b-mlx` | `qwen3.6:27b-mlx` | `60b0437bbd02` | cn-open |
| `local-qwen3-coder-next` | `qwen3-coder-next:80b-q4_k_m` | `ca06e9e4087c` | cn-open |
| `local-qwen3-coder-30b` | `qwen3-coder:30b-a3b-q4_k_m` | `06c1097efce0` | cn-open |
| `local-qwen2.5-coder-32b` | `qwen2.5-coder:32b` | `b92d6a0bd47e` | cn-open |
| `local-deepseek-r1-70b` | `deepseek-r1:70b` | `d37b54d01a76` | cn-open |
| `local-deepseek-coder-v2-16b` | `deepseek-coder-v2:16b` | `63fb193b3a9b` | cn-open |
| `local-gemma3-12b` | `gemma3:12b` | `f4031aab637d` | us-open |
| `local-gemma4-31b` | `gemma4:31b` | `6316f0629137` | us-open |
| `local-gpt-oss-120b` | `gpt-oss:120b` | `a951a23b46a1` | us-open |
| `local-muse-glimmer-30b` | `muse-glimmer:30b` | `de878ce33ad8` | us-open |
| `local-devstral-small-2-24b` | `devstral-small-2:24b-instruct-2512-q8_0` | `691a8e03a6dd` | eu-open |
| `local-exaone-deep-32b` | `exaone-deep:32b` | `a9422f9a5071` | kr-open |
| `local-north-mini-code` | `north-mini-code-1.0:q8_0` | `b6c144236e03` | ca-open |
| `local-ornith-35b` | `ornith-1.0:35b-q8_0` | `9330ee859a36` | cn-open |

Digests are the first 12 hex of what cihost's `/api/tags` reported on 2026-10-26.
`eu-open`, `kr-open` and `ca-open` are new provenance classes and are open-weights
classes: the ADR-0011 §3 box applies to them as to `us-open` and `cn-open`.

**The digest is the version.** A registry tag moves: `gemma3:12b`, `gemma4:31b`,
`gpt-oss:120b` and `qwen3.6:35b-a3b-q8_0` on cihost no longer match the registry's
manifest for the same tag. Each run reads `/api/tags` through its forward and
refuses (a `Stop`) unless the installed digest starts with the runner's `digest`.
A mismatch never re-pins; updating a `digest` is a reviewed change.

**Excluded.** `ornith-35b-bakeoff`, `ornith-35b-native`, `qwen3.6-bakeoff`,
`devstral-small-2-bakeoff` and `gpt-oss-bakeoff` are bakeoff Modelfile copies of
models above (duplicates). `shieldgemma` is a safety classifier, not a coder.
`qwen3-coder-next:latest` and `qwen3-coder:latest` are the same digests as the
lowercase names in the table, which the owner created with `ollama cp`; no runner
names `:latest`. `ornith-1.0:35b-q8_0` is an `ollama cp` of
`hf.co/deepreinforce-ai/Ornith-1.0-35B-GGUF:q8_0`; its card states no jurisdiction
and says it is post-trained on Qwen 3.5, so it is `cn-open`.

These runners are implementer-only. `bin/fleet-models` still refuses a non-`claude`
argv at reviewer positions (no review spawn path).

A reviewed policy file can deny a group of runners at positions for a group of
repositories: see [model-policy.md](model-policy.md).

## Vendor CLIs as implementers (`writer_argv`, 2026-11-02)

`sub-codex`, `sub-antigravity` and `sub-muse` can take the `implementer` seat, so
a repo keeps shipping while the Claude subscription is at its limit. Each
declares a `writer_argv` beside its pinned review/test-lane `argv`, and
`run_implementer` spawns that instead:

| runner | writer mode | prompt in by |
|---|---|---|
| `sub-codex` | `codex exec -s workspace-write` | stdin |
| `sub-antigravity` | `--mode accept-edits --sandbox` | one `stream-json` line |
| `sub-muse` | `muse exec --workspace <worktree>`, write and shell left on | `--prompt-file` (0600, removed after) |

Measured 2026-11-02 on opshost, each in a throwaway repo: all three wrote inside
their workspace, and all three refused a path under `$HOME` outside it (codex
"operation not permitted", antigravity a denied `write_file`, muse "outside the
workspace sandbox"). codex and muse can still write `$TMPDIR`/`/tmp`, as a Claude
implementer's sandbox can. Reads are not confined, for any runner.

The child gets the implementer environment **without `SSH_AUTH_SOCK`** and no
Claude token, plus a closing block in its prompt saying it must not push. The
orchestrator then does what it does for an epeius run (`publish_worktree`):
commits what the worktree holds (or keeps the commits the runner made), sets
the plan to `done`, pushes the lease branch and, on the first run, opens the
marked PR through `forgejo-open-pr.sh` as implbot. A fix round pushes only. A
run that changed nothing publishes nothing, and `verify_marked_pr` still decides
success.

The credential is the operator's own login for that vendor on this host
(`local_cli`): `serving_credential` gives it to an operator run of a writer
runner and to nothing else, so a venture repo is never served by the operator's
logins. Dispatch and respawn check only the implementer account. Reviewer
accounts are checked after the PR's full/style route is selected, using only
that reviewer position. Completed plan reviews and unrelated seats do not hold
implementation ([quota-gate.md](quota-gate.md), D3).

### Gemini's headless command permission (plan 0192)

Antigravity's headless mode auto-denies any tool that would need a permission
prompt it cannot show — `run_command` among them — and says so in the SAME
`result` record its final answer rides in: `status: SUCCESS`, `response: ""`,
`denied_actions: [{"action": "command", ...}]`. Two real writer leases hit this
before anything here read it: `branch--feat-0179-a-finished-plan-releases-its-l--001.log`
and `branch--feat-0184-recovery-follows-the-stopped-a--001.log`, both exit 0,
both a single denied `run_command`, neither read as anything but a normal
success until this plan. `read_antigravity_denied_actions` (bin/orchestrator)
now reads that field, and `_run_writer` raises `Stop` on it before `changed`
is ever checked — including when the run DID edit the worktree first: a
denial later in the same turn must not let those edits publish as finished
work. The plan stays `ready`; nothing is pushed.

**No `permissions.allow` entry was added to close the denial, after measuring
why one would not.** Live, on a disposable repository, CLI 1.5.0
(2026-11-04):

- With nothing granted, the denial above happens before antigravity spawns
  anything — clean, no partial process, same shape every time.
- Granting one command is not "run this command": trailing arguments on that
  SAME invocation are not constrained by it. `command(git status)` still let
  `git status --porcelain` through unchanged (an added token, not a second
  command) — safe enough for git's own subcommands, which have no general
  "read this arbitrary path" behaviour. It is not safe for anything that
  does: `command(cat)` and `command(touch)`, each granted alone, still let
  `cat /etc/passwd` and `touch /tmp/<anything>` succeed. `--sandbox` (already
  passed, and the flag that confines `write_file`/`view_file` to the
  worktree — measured 2026-11-02, above) does **not** confine `run_command`'s
  own subprocess the same way.
- Wrapping the whole writer in an outer `sandbox-exec` profile (the shape of
  `config/codex-tester.sb`, for a different CLI) was tried next, and found
  infeasible rather than merely harder: `run_command` execs through a
  PTY-and-TLS-cert-secured broker, which needs `job-creation` to start at
  all (`failed to create PTY: operation not permitted` with it denied, every
  other read/write path already granted). A job started through
  `job-creation` runs OUTSIDE whatever profile wraps the parent — the exact
  property `config/codex-tester.sb` and `config/meeting-runner.sb` already
  deny it for — so allowing it back to let the broker start reopens the same
  hole the wrapper exists to close, for whatever the broker then runs.

So the writer is granted nothing: no `--settings`, no `permissions.allow`
entry, ever (`tests/test_gemini_writer_permissions.py` pins this). It still
edits normally — `write_to_file`, `replace_file_content`, `view_file`,
`list_dir` and `grep_search` need no `command` grant — and cannot run its own
tests headlessly; `ANTIGRAVITY_WRITER_NOTE` tells it so up front, so a run
does not spend a turn finding out. This is the Boundary's second branch
("enforce confinement at the process boundary, **or refuse that grant**"),
chosen because the first was measured and did not hold, not assumed.

## The hosted runners (plan 0156)

`hosted-muse-spark-open` is spawned through epeius's `openai:` backend against
OpenRouter, with its key delivered on a pipe (`docs/epeius-runners.md`). Like the
local runners it is selectable on every repository: this plan adds no
per-repository restriction (owner, 2026-10-25). One the owner wants later is a
provenance rule in `config/model-policy.json` (plan 0155, ADR-0026 §2).

Its class, `routed-hosted`, and `cn-hosted` (a Chinese lab's own service) are
records of where text is served. They are not in `OPEN_CLASSES`, because these
runs carry a key by design, and they refuse nothing.

`FULL_MODEL` accepts one `/`-separated vendor namespace before the id
(`meta/muse-spark-open-20261015`); `~`-prefixed OpenRouter aliases still fail.

### Vendor ids, measured 2026-10-28

`curl https://openrouter.ai/api/v1/models/meta/muse-spark-open-20261015/endpoints`
(no key, no tokens) resolved the id, with six providers at $1.25 / $4.25 per
million input / output tokens: Makora, Modal, Together, Nebius, Fireworks, BaseTen.
The request pins none of them.

**Not measured, so not added:** `hosted-deepseek-v4-pro` and `hosted-kimi-k3.1-pro`.
DeepSeek's `GET /models` and Moonshot's `GET /v1/models` answer 401 without a key
(checked 2026-10-28), and this run is not permitted to read a key out of keyvault.
The runners wait for the owner, or a session allowed to run the fetch helpers, to
list the vendors' dated ids with D1's command and add them through
`_hosted_runner` in `bin/orchestrator`.

## Seat access (plan 0178)

Each reviewer seat (repo, position, ordinal) carries an access setting: `repo`
(the reviewer reads the repository) or `change` (it sees only the change).
Set it with `fleet-models access set <repo> <position> <ordinal> repo|change|inherit`;
read it with `fleet-models access show <repo>|--all --json`. `reviewers --json`
carries the resolved value as `access`. Rows live in `seat_access`, with their
history in `seat_access_change` and a `seat-access-changed` event per write, so
the table is rebuilt from the ledger like `repo_model`. Clearing a setting writes
`inherit`; rows are never deleted.

**Inheritance**, first hit wins: the repo's own row, then the `default` row for
that position and ordinal, then the harness default (`repo` for a `claude`
runner, `change` for every other harness). With no rows every seat resolves to
what `dispatch-review.py` grants today.

**Two ceilings**, which no row can lift:

- A runner whose harness is not `claude` reviews without tools (talos plan 0033;
  an epeius reviewer gets the prompt only), so it resolves to `change`.
- `lane_reviewer` (talos plan 0028) and `style_reviewer` (talos plan 0031) always
  resolve to `change`. A lane PR carries vendor-written tests nobody has read, and a
  style review reads only its evidence folder. talos enforces both whatever it is told.

`set … repo` at a ceiling is refused and writes nothing; `change` and `inherit`
are always accepted. talos also withholds tools from a lane pull request by the
pull request, not the position, so a `code_reviewer` seat set to `repo` still
reviews a lane PR without tools; `access show` says so in `note`.

**Why the setting exists.** The review replay (talos `replay-review.py`, corpus
`2026-10-29-open-models-4`, talos `a8f2e1a`, measured 2026-10-31) ran Opus 5.5 on
eunomia #951, a status deadlock, four times with read access to the repository
and four times with the change only. With access it rated the bug HIGH 4 of 4
times; with the change only it rated it lower 4 of 4 times and approved. Access
mattered as much as the model.

Access is not a provenance decision: whether a model may see a repository at all
is the model policy's job (plan 0155).
