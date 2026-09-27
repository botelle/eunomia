# fleet-lane — a test lane runs once, from a bundle

*Plan `plans/0070-a-test-lane-runs-once-from-a-bundle.md`.* One command hands
a tester model the generated bundle for a plan at a pinned commit, granting
it nothing (ADR-0006 §6), and turns what comes back into a `Tests-for: <plan>`
pull request opened by this credentialed wrapper.

This is the smallest unit that can exercise the properties plan 0066's four
review rounds could only assert on literals: nothing refuses a vendor runner
for the implementer position, nobody had measured which settings file
antigravity reads, an operator skill might reach a lane, placeholders in the
pinned argv had no filler. **No orchestrator integration exists yet.** One
runner, one PR, no loop, no retry — see Boundaries below for what is
deliberately not built.

## Usage

```
bin/fleet-lane <repo> <plan-id> --sha <commit> [--runner <name>]
               [--schema <file>] [--dry-run]
```

`--runner` is optional. Omitted, the lane runs the repository's tester at
ordinal 1; given, it must be one of the repository's resolved tester rows
(gate 2). Testers are configuration, not code: the owner writes them with
`fleet-models set default tester sub-opus` (every repository) and
`fleet-models set <repo> tester <runner>` (an additional or overriding row).

`--dry-run` runs every gate and builds the prompt, prints its size and
sha256 and the bundle's gaps, and sends nothing. It is not a test mode; it
is the disclosure control — the prompt is a new egress surface every time a
repo's bundle changes, and the human read of `prompt.txt` before the first
real run against a repo is the half of the promise no automated check can
make (plan 0019 §3). **Run it before every first real run against a repo.**

## The gates, in order

1. **Zone.** The plan file at `--sha` must say `zone: public` (ADR-0006 §7 —
   an allowlist: absent, malformed or any other value refuses).
2. **Tester.** The runner must be one of `<repo>`'s resolved `tester` rows
   in `fleet-models` — each enabled ordinal, the repo's own row first and
   then the `'default'` row (ADR-0013 §2). It asks whether THIS runner is
   one the repository named, never merely whether the list is non-empty. No
   resolved ordinal 1 is its own refusal, naming ADR-0013 §2 and
   `fleet-models set default tester sub-opus`. There is no consent flag
   (`extlane` was removed, ADR-0013 §1) and no same-family refusal (§6): the
   PR names its runner, which is the record.
3. **Adapter table.** The runner must be one of the three entries below. A
   runner this program has not measured is refused whatever position it
   holds in the orchestrator's own registry (ADR-0009 §3 notwithstanding).
4. **Wired.** The runner's registry entry must not be `unwired` (plan 0066).
5. **Settings precondition.** The runner's `settings_must_not_grant` files
   (plan 0066 D4b) must carry no `permissions.allow` entry and no `hooks`
   block.
6. **Contract completeness.** The built bundle's `gaps` must not carry
   `docs/declared-behaviour.md` — without that document the lane could only
   test the contract, which passes for an implementation that does nothing
   (plan 0019).

Every refusal is logged (`~/dev/.fleet/work/logs/lane--<plan>--<runner>.log`)
and paged once; nothing is spawned and no branch is left behind.

## The adapter table

| runner | stdin shape | result shape |
| --- | --- | --- |
| `sub-antigravity` | one NDJSON line, `orchestrator.antigravity_stdin_line` (plan 0066 D2) | the `result` event's `response` field, `orchestrator.read_antigravity_result` |
| `sub-codex` | the prompt, raw, on stdin | the JSON document at the path the `-o` flag named |
| `sub-opus` | the prompt, raw, on stdin | `--output-format json`'s `result` (the lane document, optionally in a code fence) |

Both functions are the ones plan 0066 pinned in `bin/orchestrator` — this
program imports them, never re-declares them, so one measured shape has one
definition (D2).

**`sub-opus` runs a pinned argv this program owns**, not the registry's
(`RUNNERS["sub-opus"].argv` is the implementer's and carries `--allowedTools`
and a permission mode): `claude -p --output-format json --model opus --tools ""
--strict-mcp-config --setting-sources "" --disable-slash-commands
--no-session-persistence`, asserted by equality in the suite. It runs from an
empty directory under the system temp root (the CLI loads project
instructions from every ancestor of its working directory) with an
environment of `PATH`, `HOME`, `USER`, `LOGNAME`, `TMPDIR` — the CLI answers
"Not logged in" without the last three, and none of them is a credential.
Measured on Claude Code 2.1.282: `--tools ""` is what removes the tools (the
model's attempted `Read` comes back as text and nothing runs; adding
`--allowedTools Read` to it still reads nothing), and `--setting-sources ""`
is what keeps the operator's `~/.claude/CLAUDE.md` out of the prompt. Removing
`--tools ""` alone does not read a file outside the directory — default
permissions refuse that — so the controls are: without `--tools ""` an inside
file is read, and without it plus `--allowedTools Read` an outside one is.
`tests/test_fleet_lane.py::test_live_claude_tester_cannot_read_a_file_and_the_control_can`
runs all of this and is skipped unless `FLEET_LANE_LIVE_CLAUDE=1` (CI holds
no Claude login).

**`sub-codex` cannot be spawned for real today.** Its registry entry is
`unwired`: `codex exec -s read-only` confines writes, not reads (ADR-0006
§6), and the isolation that would confine reads does not exist. Reaching it
is refused with its own message (gate 4 above), and the codex adapter's
stdin/result shapes are exercised only by the fake vendor in
`tests/test_fleet_lane.py` until that isolation lands.

## What the wrapper writes, and where

Under `~/dev/.fleet/work/lanes/<plan>/<runner>/`:

- `bundle.json` — the generated contract fleet-bundle built from the reader
  worktree
- `prompt.txt` — the exact prompt text, before any adapter wraps it; its
  sha256 is the byte-identical-input proof a second lane will need
- `stdin.bin` — the adapter's wire bytes (NOT what the sha256 is taken of)
- `cwd/` — the vendor's empty working directory, containing only
  `schema.json` (a copy of the result schema) and, after the run,
  `result.json` if the runner is codex. (`sub-opus` runs from a fresh empty
  system-temp directory instead and is given no schema file.)

`~/dev/.fleet/work/logs/lane--<plan>--<runner>.log` carries the run's phase
lines (`gate-*`, `bundle`, `prompt`, `spawn`, `spawn-exit`, `result`, `pr`, or
`refused`), the same `key=value` style every other unit on this fleet writes.

## The result shape

`config/lane-result.schema.json`:

```
{"files": [{"path": "tests/...", "content": "...", "executable": false}],
 "gaps": ["...", ...],
 "notes": "..."}
```

Every `path` is confined to a normalised, symlink-free location under
`tests/`, must not already exist at the landing branch's base (main's tip at
landing time, fetched then — not at `--sha`), and is written mode `0644`.
`"executable": true` on any entry refuses the whole result, the same as a
path that fails confinement. One offending entry refuses everything; nothing
partial lands.

## The landing pull request

- branch `agent/tests/<plan>/<runner>`, cut from `main`'s tip at landing
  time, never stacked on `--sha`
- title prefixed `WIP: ` — Forgejo's create-pull API has no draft field, so
  the prefix is what marks it draft there
- body carries `Tests-for: <plan>` (never `Plan: <id>` — the watcher's own
  marker, whose presence would tell it the plan was dispatched or done), the
  runner name, the sha, the prompt's sha256, the usage the CLI reported, and
  the gaps inside a fenced block with every line prefixed `> `, so no
  vendor-authored line can stand at the start of a line in the body
- **no review is dispatched on it, by anyone automatic.** The fleet's
  reviewer holds `Bash` on purpose (ADR-0005 alternatives) and runs probes
  in a worktree at the PR head — dispatching one here would execute the
  vendor's files on opshost before a person had read them. The reader is a
  human (ADR-0006 §5) until a second one exists.
- both workflows (`ci.yml`, `plans.yml`) ignore `agent/tests/**` under
  `on.push`, so nothing runs the vendor's own tests on the self-hosted
  runner before a human has read them

The wrapper pages once on success, naming the PR, and once on refusal,
naming the reason. `fleet-stalled` does not treat a lane PR as `unreviewed`
— that state needs CI green at head, and no CI runs on this branch shape by
design.

## What is not built here

- The orchestrator does not call this program. `bin/fleet-watch`,
  `bin/fleet-models` and `bin/fleet-bundle` are untouched; this program
  imports the orchestrator module read-only, the way `bin/fleet-watch`
  already does, for `RUNNERS`, `harness_argv`, the two antigravity
  functions, `check_settings_precondition`, `redact_transcript`, the shared
  page recorder, and the implbot token helper.
- No second lane, no meeting step, no mediator (parked note 0016's
  remaining shape). Two lanes on one bundle and reconciling their
  disagreement is the next plan; it needs this one's prompt hash to prove
  the inputs were identical.
- No loop, no retry. A failed run is recorded and paged; a person runs it
  again. These runners spend the owner's personal subscriptions (issue
  #483).

## Manual measurements (D7)

Recorded here, with CLI versions, the first time each is performed on opshost:

1. **Dry run against eunomia at `main`.** — *(pending)*
2. **One real antigravity run** of that prompt, its result read by the
   adapter, its PR opened. — *(pending)*
3. **codex: the hard stop.** `sub-codex` is `unwired`; the lane must refuse
   to spawn it, and this records that refusal, never a run. — *(pending)*
4. **The antigravity settings precondition, both ways**: with
   `permissions.allow` present in `~/.gemini/antigravity-cli/settings.json`
   the precondition refuses; with it removed, a by-hand (never through this
   wrapper) headless read of a marker file succeeds, proving that file is
   the one antigravity reads; with the entry removed, the same read is
   denied. — *(pending)*
5. **An operator skill in antigravity's skills directory** — whether it is
   available to a headless run, which decides whether plan 0066's argv
   tuple needs a flag. — *(pending)*

The handoff (plan §5) is the same list, filled in, plus the prompt size
measured against eunomia's own bundle and what the human egress read found.
