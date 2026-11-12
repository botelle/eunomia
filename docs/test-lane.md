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
7. **The target's CI would not run the lane** (plan 0137, issue #618). Every
   workflow the target repository has on `main` — `.forgejo/workflows/*.yml`
   and `*.yaml`, or `.github/workflows/` only when the first directory does
   not exist (Forgejo's own fallback) — is read from the lane store at
   `main`'s tip, not at `--sha`, because CI runs the workflow files of the
   pushed branch and the lane branch is cut from `main`'s tip at landing
   time. A workflow would run the lane when it has a `push` trigger whose
   branch filters admit `agent/tests/<plan>/<runner>`, or a `pull_request` /
   `pull_request_target` trigger whose filters admit `main`. `branches` and
   `branches-ignore` are globs where `*` stops at `/` and `**` does not;
   neither filter admits every branch; `paths` and `paths-ignore` always
   admit, since the vendor's file names are not known yet — that can refuse a
   repository that was safe, never the reverse. `on:` as a string or a list
   means no filters. The gate runs after gate 6 and before the cap is
   reserved, so a refusal spends nothing, and it runs under `--dry-run`.

   **The evaluator reads a subset of YAML and fails closed.** `bin/` takes no
   YAML package, so a small reader handles block and flow mappings and
   sequences of plain or quoted scalars in the `on:` block, comments, and a
   flow collection over several lines. Anchors, aliases, tags, block scalars,
   multi-document files, a missing `on:`, a glob using `[`, `?` or other
   regex-like syntax, a `!` pattern under `branches-ignore`, both filters at
   once, a non-UTF-8 file and a file that cannot be read each refuse the lane,
   naming the file. Only `.yml` and `.yaml` files are read.

Every refusal is logged (`~/dev/.fleet/work/logs/lane--<plan>--<runner>.log`)
and paged once; nothing is spawned and no branch is left behind.

## The adapter table

| runner | stdin shape | result shape |
| --- | --- | --- |
| `sub-antigravity` | one NDJSON line, `orchestrator.antigravity_stdin_line` (plan 0066 D2): the prompt. A second, `ANTIGRAVITY_SECOND_TURN`, only after a first turn that was denied a tool and held no document | the JSON object that `result.response` starts with, from the first `result` event that has one, `orchestrator.read_antigravity_results` |
| `sub-codex` | the prompt, raw, on stdin | the JSON document at the path the `-o` flag named |
| `sub-opus` | the prompt, raw, on stdin | `--output-format json`'s `result` (the lane document, optionally in a code fence) |

Both functions are the ones plan 0066 pinned in `bin/orchestrator` — this
program imports them, never re-declares them, so one measured shape has one
definition (D2).

**`sub-antigravity` answers on its second turn.** Measured on antigravity
1.3.3, 2026-10-12, with eunomia's own prompt for plan 0130 (214,267 bytes),
five runs: the model's first act is a tool call (`run_command ls -la`, once
`view_file` on its own conversation directory first), headless mode denies it
(`jetski: no output produced`), and the turn ends `"status": "SUCCESS"` with
`"response": ""` and a `denied_actions` list. A closing instruction in the
prompt not to call a tool did not change that, and neither did `--model
gemini-3.8-flash-high`. The CLI has no flag that removes its tools. It runs one
turn per stdin line, in one conversation, and the turn after the denial
answered in all three runs that had one. So the adapter holds stdin open,
reads the first `result` event (17.5 s into the run measured), and writes one
further line, `ANTIGRAVITY_SECOND_TURN`, only if that turn was denied a tool
and held no document. A first turn that answers ends the run on one turn. It
is never asked a third time. The line is outside `prompt.txt`, so the prompt
hash does not cover it: two lanes with equal hashes were given the same
prompt, and an antigravity lane may also have been given this sentence.
`stdin.bin` holds what was actually written, and the run log's `second-turn
sent=` line says whether it was. The stream then carries two `result`
events, with the fields inside `result`, not beside `event`. `response` is JSON
text, and in all three runs it held the document twice, the second copy carrying
`toolAction` and `toolSummary`; the adapter takes the first object. A run in
which no turn answered is `bad-result` with the denied action named in
`detail`. The first real run through this program, before this change, ended
that way (`lane_run` 2026-10-13T00:45:28Z).

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

**`sub-codex` is confined by a profile, and is wired** (plan 0101, 2026-10-13).
`codex exec -s read-only` confines writes, not reads (ADR-0006 §6), so the
confinement is `/usr/bin/sandbox-exec -f config/codex-tester.sb` at the front of
the registry argv itself — not a wrapper only this program applies — and it
wraps the whole codex process, so every child inherits it. No codex flag is the
control: codex's `deny_read` permission profiles and its `shell_tool` toggle
govern only the commands codex spawns, not its in-process tools, and were not
used. `--ephemeral` and `--ignore-rules` are surface reduction and nothing
more. The runner runs from a fresh scratch directory (the profile's `LANE`),
with the schema written into it, `-o` landing in it and codex's sqlite state
redirected into it (`-c sqlite_home`), so the owner's databases are neither
read nor written. The environment is `PATH` and `HOME`: `OPENAI_API_KEY` and
`CODEX_API_KEY` are removed even if the parent holds them, because a key would
silently move a subscription lane onto API billing.

*What wiring it rests on.* Plan 0101 §3 says a denial without its control
proves nothing. The pair was run on 2026-10-13 (the measurement record): the
confined run executed no command and returned neither nonce, and the bare
control executed one and returned both. The confined `--json` stream carries
no item for the attempted command, so the attempt itself is recorded only in
the model's own notes. The owner accepted "no command executed" as the
evidence. It does not show a read rule of the profile refusing codex itself,
and it would not cover a codex that read files without a shell.

The schema codex is given is `strict_schema`'s form (every object requires all
of its properties: OpenAI refuses the schema as written), and the result is
validated against the schema as written. Each run logs `login-file changed=`,
whether `~/.codex/auth.json` was rewritten during it. `codex login status`
is not a check on the login: it read "Logged in" throughout the 2026-10-11
stranding.

**What was measured** (codex-cli 0.153.4, macOS 27.2, opshost, 2026-10-11; the
record is in `docs/measurements/adr-0006-blackbox-test-lanes.md`):

- Codex's own per-command sandbox cannot start inside the profile
  (`sandbox-exec: sandbox_apply: Operation not permitted`), so every shell
  command the model requests fails before it runs. The model has no working
  shell; that is the `claude` tester's `--tools ""` posture reached by a
  different route. The profile would bind a command even if codex ran it
  unsandboxed.
- `codex login status` and a `codex exec` that reaches its vendor both run under
  `(deny default)`. The allow list is each line of the profile, with the
  denial that forced it in a dated comment beside it.
- Both `network-bind` and `network-inbound` are denied with **no exception**:
  a run reached its vendor with both denied.
- `signal`: `(allow signal (target same-sandbox))`. `(target pgrp)` was
  measured and is **not** enough: a confined shell that started its own child
  signalled it (`status=143`), but a child in its own process group (job
  control, which is how a runtime cancels a command) was refused. A codex that
  could not stop its own commands would hang a lane.
- Name resolution needs the AF_UNIX connect to `/var/run/mDNSResponder`;
  without that one allowance `gethostbyname` fails.

**Residual reach — what the profile does NOT confine.** Read this list before
trusting the lane with anything:

- **Outbound TCP and UDP to any address.** The CLI's own request to its vendor
  leaves from this process, so it cannot be narrowed to one host. A model that
  could run a command could send what it read there; it cannot read anything
  outside the lane, which is what the profile is for.
- **The login.** `~/.codex/auth.json` is readable and **writable**: the codex
  process holds the owner's ChatGPT tokens in memory, and a refresh must be
  persisted. Writable on purpose — see "The owner's login" below. The same
  process reads `installation_id` (and writes it; codex failed to initialise
  without that write). Nothing else in `~/.codex` is allowed: `sessions/`,
  `history.jsonl`, `session_index.jsonl`, `shell_snapshots/`, `skills/`,
  `plugins/` and every `*.sqlite` stay denied, so the owner's other codex
  conversations are **not** reachable. Codex asked for `models_cache.json`,
  `cache/`, `skills/`, `sessions/` and `shell_snapshots/`, was refused, and
  went on to reach its vendor; whether a full model turn also tolerates that is
  what the live measurement settles, and an entry is added only with its
  dated denial.
- **Mach services: one exception on 0.153.4, four as of codex-cli 0.166 —
  the Keychain, and three more below.**
  `(deny mach-lookup)` with `(allow mach-lookup (global-name
  "com.apple.SecurityServer"))`. Dropping candidate names one at a time from an
  allow-everything baseline, that was the only one codex-cli 0.153.4 could not
  reach its vendor without (`error sending request for url
  (https://auth.openai.com/oauth/token)`); `trustd.agent`, `configd`,
  `opendirectoryd.*`, `diagnosticd`, `logd`, `cfprefsd.*` and the rest were
  each dropped and the run still reached the server, **on 0.153.4** (measured
  2026-10-11). `com.apple.SecurityServer` is the Keychain server, so **the
  Keychain is reachable from the lane again**. What that buys, measured: a
  throwaway keychain file inside the lane is served (`security
  find-generic-password -w` prints the item), and the same file with that one
  name removed is not; a keychain file outside the lane is not (the client
  reads the file itself, and the profile refuses it), and the operator's
  `login.keychain-db` returned `Operation not permitted` to `security
  show-keychain-info`. So the reachable Keychain is the one the process can
  already read — but it is a service the profile does not close, and a future
  macOS that serves a keychain without the client reading its file would
  change that.

  **codex-cli 0.166 needs three more** (measured on opshost, 2026-11-05, macOS
  27.2, 0.166.0 against 0.153.4; plan 0207): `com.apple.cfprefsd.daemon` and
  `com.apple.cfprefsd.agent`, plus the shm read `(allow ipc-posix-shm-read-data
  (ipc-posix-name-prefix "apple.cfprefs"))`, because 0.166 reads macOS managed
  preferences at start-up to see whether an administrator has forced Codex
  settings (`com.openai.codex` keys `config_toml_base64`,
  `requirements_toml_base64`) — without them it exits with `failed to load
  configuration: Failed to synchronize managed preferences`; and
  `com.apple.trustd.agent`, because 0.166 makes a new "workspace routing
  discovery" TLS request that is verified through the per-user trust agent —
  without it, `workspace routing discovery failed`. Both cfprefsd names plus
  the shm read are needed together (each smaller combination failed), and
  `trustd` without `.agent` does not work. `(allow user-preference-read)` was
  **not** needed and is not added: it is the rule that would let the lane read
  other apps' settings. Three tests in `tests/test_codex_confinement.py` back
  that: `test_the_cfprefsd_and_trustd_mach_names_are_reachable_and_an_unlisted_one_is_not`
  calls `bootstrap_look_up(3)` directly for each of the four allowed names and
  shows they succeed while an unlisted real service name on this host does
  not — a positive the first version of this plan's test lacked, because
  `defaults`(1) degrades to reading the plist file directly when the service
  itself is unreachable and so cannot tell "service reachable" apart from
  "service absent, file readable anyway" (found in the 2026-11-05 PR review).
  Every name it checks is a per-user Aqua-session agent, so on a host with no
  such session (the cihost CI runner is a system LaunchDaemon) it skips with
  its own unconfined control rather than failing — found in the 2026-11-05
  round-3 PR review, where the hard assert on the unlisted control name
  (`com.apple.cfnetwork.AuthBrokerAgent`, Aqua-only) turned CI red on every
  push. Given the service is reachable,
  `test_the_preferences_service_does_not_hand_out_another_apps_settings`
  writes a marker into a throwaway domain outside the lane with a real
  `defaults write` and shows a confined **read** does not come back with it,
  and `test_the_preferences_service_does_not_let_the_lane_write_another_apps_settings`
  shows the same for a **write**: `cfprefsd` does the write unconfined on the
  caller's behalf, so the lane's own `file-write*` restriction to the lane and
  the login is not obviously what stops it, and the test shows something
  does. The trust agent evaluates certificates and holds no secrets of its
  own.

  **codex-cli cannot start under this profile at all right now, on either
  version — a blocker unrelated to the three allowances above.** Measured
  2026-11-05 (plan 0207's PR review round): both 0.166.0 and 0.153.4, run by
  hand under `config/codex-tester.sb` with a pristine `$CODEX_HOME`, exit
  before emitting any `--json` event with `WARNING: proceeding, even though
  we could not create PATH aliases: Operation not permitted (os error 1)`
  followed by a fatal `Error: Operation not permitted (os error 1)`. `log
  stream --predicate 'sender == "Sandbox"'` names the denial as
  `file-write-create` under `$CODEX_HOME/tmp/arg0-macos` / `tmp/arg0`: codex
  now treats a failure to write its PATH-alias/arg0-wrapper directory as
  fatal, where it previously only warned. 0.153.4 hits the identical error
  under the **unmodified, pre-plan-0207** profile at `origin/main`, so this is
  an environment or codex-side regression since the 2026-10-11/13
  measurements, not something this plan's diff caused or can fix within its
  boundaries (no allowance here was bisected for `tmp/arg0*`, and guessing one
  is exactly what the Boundaries section forbids). It needs its own
  measurement and its own plan; until then `test_live_codex_tester_…` (opt-in,
  `FLEET_LANE_LIVE_CODEX=1`) fails on this host for both codex versions, and
  plan 0207 stays at `status: ready`, not `done`, because its own Definition
  of done's live-start item is not met.
- **No bind exception.** `127.0.0.1`, `0.0.0.0` and `::` all fail under the
  profile and the run did not need one. If one is ever added it is an
  **all-interfaces** exception (SBPL cannot name loopback: #677), never "loopback only".
- **AF_UNIX:** connects are allowed only inside the lane and to the resolver
  (`/var/run/mDNSResponder`). `~/run/forgejo-broker.sock` and the vault socket
  are refused with `EPERM`.

**Which denies are load-bearing on this host.** The profile starts from `(deny
default)`, so each explicit deny below is redundant there by construction; the
question asked is the other one — on an `(allow default)` profile carrying just
that rule, and again with it removed, does the OS already turn the caller away?
(`tests/test_codex_confinement.py`, run with `-s`, 2026-10-11, macOS 27.2.)

| rule | with the rule | without it | verdict |
| --- | --- | --- | --- |
| `(deny job-creation)` | `launchctl submit` creates nothing | creates nothing | belt-and-braces — launchd turns the sandboxed caller away |
| `(deny lsopen)` | `open -g x.command` runs nothing | runs nothing | belt-and-braces — Launch Services turns it away |
| `(deny appleevent-send)` | not measurable | not measurable | the unconfined control did not act either (no Automation grant on this host), so the probe skips with that reason; the rule ships regardless |
| `(deny signal)` + `(allow signal (target same-sandbox))` | `kill -0 <outside pid>` fails | succeeds | **load-bearing** |
| `(deny mach-lookup)` | throwaway keychain not found | throwaway keychain **readable** | **load-bearing** — and with `SecurityServer`, the two `cfprefsd` names and `trustd.agent` re-allowed (above) those four are the holes, measured not guessed |

**The owner's login.** A ChatGPT login refreshes itself and the refresh token
rotates. The profile therefore allows writes to the real `auth.json`, and no
copy of it is made. Measured the hard way on 2026-10-11 (see the measurement
record): a confined run that could reach its vendor **before** `auth.json` was
writable refreshed server-side, could not persist the new token, and left the
owner's stored one rejected (`refresh_token_reused`). **Do not run a confined
codex against the real login with the network allowed and `auth.json` read-only.**

The remaining adapter shapes — stdin the raw prompt, the result at the `-o`
path, usage `None` until a real stream has been dumped — are exercised by the
fake vendor in `tests/test_fleet_lane.py`; the profile itself by
`tests/test_codex_confinement.py` (the profile tier runs on every push on macOS,
spawns no model, and skips with a stated reason where `/usr/bin/sandbox-exec`
is absent; its live tier runs only with `FLEET_LANE_LIVE_CODEX=1`).

## What the wrapper writes, and where

Under `~/dev/.fleet/work/lanes/<plan>/<runner>/`:

- `bundle.json` — the generated contract fleet-bundle built from the reader
  worktree
- `prompt.txt` — the exact prompt text, before any adapter wraps it; its
  sha256 is the byte-identical-input proof a second lane will need
- `stdin.bin` — the adapter's wire bytes (NOT what the sha256 is taken of)
- `stdout.redacted.txt` — what the vendor wrote, through `redact_transcript`,
  kept whatever the outcome: a `bad-result` with nothing kept could only be
  understood by spending a second run
- `cwd/` — the vendor's empty working directory, containing only
  `schema.json` (a copy of the result schema) for a runner that reads it from
  disk. (`sub-opus` and `sub-codex` run from a fresh empty system-temp
  directory instead; `sub-codex` gets `schema.json` and `result.json` there,
  `sub-opus` no schema file.)

`~/dev/.fleet/work/logs/lane--<plan>--<runner>.log` carries the run's phase
lines (`gate-*`, `bundle`, `prompt`, `spawn`, `spawn-exit`, `result`, `pr`, or
`refused`), the same `key=value` style every other unit on this fleet writes.

## The result shape

`config/lane-result.schema.json`:

```
{"files": [{"path": "tests/lane/...", "content": "...", "executable": false}],
 "gaps": ["...", ...],
 "notes": "..."}
```

Every `path` is confined to a normalised, symlink-free location under
`tests/lane/` (ADR-0014 §1–§2: the lane's output is told apart from unit tests
by path), must not already exist at the landing branch's base (main's tip at
landing time, fetched then — not at `--sha`), and is written mode `0644`. A
returned `test_*.py` or `conftest.py` whose **basename** repeats one already
under `tests/` at that base is refused too — pytest's default `prepend` import
mode would fail CI's whole collection on it — except a new
`tests/lane/conftest.py`. The preamble tells the model that a test file finds
the repository root with `Path(__file__).resolve().parents[2]`, stated
literally because "two parents above" reads both ways and both are wrong.
`"executable": true` on any entry refuses the whole result, the same as a
path that fails confinement. One offending entry refuses everything; nothing
partial lands.

## The landing pull request

- branch `agent/tests/<plan>/<runner>`, cut from `main`'s tip at landing
  time, never stacked on `--sha`
- title prefixed `WIP: ` — Forgejo's create-pull API has no draft field, so
  the prefix is what marks it draft there
- the body opens with a **header block** — the lines before the first blank
  line: `Tests-for: <plan>` (never `Plan: <id>` — the watcher's own marker,
  whose presence would tell it the plan was dispatched or done), `runner:`,
  `sha:`, `prompt sha256:` and `usage:` (the same JSON `lane_run.usage_json`
  stores, or `-`). The gaps and the notes follow inside fenced blocks, every
  line prefixed `> `, so no vendor-authored line can stand at the start of a
  line in the body and `fleet-meet` reads those five fields from the header
  block only
- **the orchestrator dispatches both configured reviewers' reviews on it, once
  per head, when it first sees the lane `opened #<n>`** (plan 0139), in the
  background and recorded as a `test-lanes` line (`review=<n> head=<sha8>`). It
  only decides *when to ask*. This is safe because the dispatcher reviews any
  pull request from `agent/tests/**`, or touching `tests/lane/**`, without
  tools whatever it is told (talos plan 0028, provenance `tools=none`), so
  nothing of the vendor's is executed. The owner's read (ADR-0006 §5) follows
  the reviews. The reviewers are the `lane_reviewer` seat (plan 0163), falling
  back to `code_reviewer` when it has no rows; eunomia expects those accounts,
  and talos plan 0034 is the part that picks the model.
- eunomia's two workflows (`ci.yml`, `plans.yml`) ignore `agent/tests/**`
  under `on.push`, and **every target now has to**: gate 7 refuses a
  repository whose workflows would run the pushed branch or its pull request,
  so nothing runs the vendor's own tests on a self-hosted runner before a
  human has read them. The lane does not change other repositories'
  workflows to make itself safe; a repository that wants lanes adds the
  exclusion itself (`branches-ignore: ['agent/tests/**']` under `push`, and
  no unfiltered `pull_request`)

The wrapper pages once on success, naming the PR, and once on refusal,
naming the reason. It also prints `pr: <repo>#<n>` on stdout on success (the
refusal goes to stderr, as before). **The page channel comes from the installed
unit's environment** (`FLEET_ANGELIA_URL` / `FLEET_NTFY_URL` live in each unit's
plist, and `fleet-svc audit` checks them there); a hand run has none, so its
page prints `NOTIFY(undelivered)` and `record_page` keeps the attempt in
`pages.jsonl` — that is the expected result of a hand run, not a defect. This
program does not scrape a plist for the channel: a second reader of unit
configuration would be kept by nobody.

The run log stops scrubbing what this program defined. `RunLog.note` writes
the closed key set `repo, plan, runner, sha, sha256, bytes, number, branch, rc,
timed_out, files, gaps, zone` verbatim after a shape check (`sha` is 40 hex,
`sha256` is 64, otherwise `invalid`), and redacts every other key's value one
value at a time — never the joined line, which had turned `sha=…` and
`sha256=…` into `[redacted:long-hex]`. `fleet-stalled` does not treat a lane PR as `unreviewed`
— that state needs CI green at head, and no CI runs on this branch shape by
design.

**After the meeting.** A lane pull request that both code reviewers approved
(their reviews are made without tools, talos plan 0028) is merged once its
meeting has run, so its tests stay on `main` under `tests/lane/` and CI runs
them as regression tests from then on; eunomia#912 was the first. A lane pull
request with a HIGH from either reviewer is closed unmerged after its
meeting: it has done its job if the meeting and triage ran, and its tests are
not kept; eunomia#879 was the first. Either way the meeting's findings
document and the triage comment on the implementation pull request are the
record. Set by the owner 2026-10-16; a different rule is one paragraph here.

## Cap and record (plan 0100)

**The owner decision (2026-09-28).** Test lanes bill the owner's ChatGPT, Gemini
and Claude subscriptions through each CLI's own login. There are no API keys and
no pay-per-token service accounts: issue #483's "What adding these would mean"
(service accounts, keys in keyvault, `model_price` rows) is **rejected** as the
path. `total_cost_usd` from a subscription run is a notional number: it is
stored as the CLI reported it and never priced. What is added instead is a cap
the owner writes, a record of every run, and a distinct ending for a vendor's
usage limit.

**The cap** is `fleet-config`'s `lane_run_cap`, a comma-separated list of
`<runner>=<n>` (`fleet-config set lane_run_cap 'sub-opus=4,sub-antigravity=2'`).
`<runner>` matches `[a-z0-9][a-z0-9-]*` and appears once; `<n>` is an integer
0..24, and `0` pauses that runner. It has no environment fallback and no
built-in default: with no row, `get` prints `unset`, and every lane refuses. It
fails **closed** — an unreadable database, an invalid row, a runner the value
does not name, or `0` all refuse, and the refusal names the command that fixes
it.

The check comes after every gate and after the prompt is built, immediately
before the spawn. In one `BEGIN IMMEDIATE` transaction the lane counts the
runner's `spawned = 1` rows whose `started_at` is within the last 24 hours (a
rolling window, not a calendar day), and either inserts a `capped` row or
reserves a `running` one — so two lanes started together cannot both take the
last slot. A gate refusal is recorded (`refused`, `spawned = 0`) but never
counts. `--dry-run` prints `cap: <runner> <count>/<n> in the last 24h` (or
`cap: <runner> unset`), writes no row and does not refuse on the cap.

**The record** is `lane_run`, owned by `bin/fleet-lane` and created by its own
`SCHEMA` (also by `fleet-db-migrate`). It is a durable table; its second copy is
the daily snapshot. `run_id` is a ULID; `sha` (40 hex) and `prompt_sha256` (64
hex) are written as values, never through redaction — only `detail`, free text
from a refusal or a vendor, is redacted and bounded to 300 characters.
`usage_json` is what the CLI reported (`sub-opus`: `usage`, `modelUsage`,
`total_cost_usd`, `duration_ms`, `num_turns`; `sub-antigravity`: the `result`
event's `usage`; `sub-codex`: the last `turn.completed` event's `usage` from its `--json` stream, or NULL when the stream has no such event — that shape is documented by codex and **not yet measured** on a real stream here), capped at 8 KiB.

**Outcomes:** `running` (reserved; a row still there after its process is gone
is a wrapper that died), `pr-opened`, `quota`, `vendor-error`, `timeout`,
`bad-result`, `landing-failed`, `refused`, `capped`, `no-tests` (the document
validated and held no files: nothing is landed and no pull request is opened;
`detail` carries the gap count and as many gaps as fit, the run pages once,
exit 1).

**Quota.** Each adapter has a `quota` function, tried only when the run produced
no lane document (a reply that parses is never a quota, whatever it says), and
only after `timed_out` is checked (issue #662): a run this program killed on
its own timeout is never `quota`. `sub-opus` is structural first — an
`is_error` `result` record with `api_error_status == 429` — and falls back to
the texts `You've hit your <x> limit`, `You've reached your <x> limit` and
`(spend|usage) limit`, searched anywhere in stdout only when the CLI exited
non-zero with no parseable `result` record — stdout cannot carry model text in
that shape. The `sub-codex` and `sub-antigravity` recognisers match refusal
texts measured in their binaries, not observed in a run; the first real one
confirms or amends them. Unlike `sub-opus`, their stdout interleaves CLI
events with model-authored text, so their fallback (`_terminal_quota_text`)
matches only the stream's own last non-empty line, and only when that line is
not JSON — never a substring anywhere in the stream, and never inside a parsed
event's assistant/message/result content. `RESOURCE_EXHAUSTED` alone does not
count for antigravity: measured (`strings` on the installed binary) as a bare
retry-status code, distinct from the dedicated terminal sentence "You have
exhausted your quota on this model.". A quota sends its own page (`test lane
stopped by vendor quota: …`) and nothing retries.

**Exit codes.** `0` a pull request opened; `75` the runner is `capped` or the
vendor stopped on a `quota` (both mean "later, unchanged"; the row's outcome
tells them apart); `1` every other failure.

Two queries:

```sql
-- runs per runner in the last 24h, as the cap counts them
SELECT runner, COUNT(*) FROM lane_run
 WHERE spawned = 1 AND started_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')
 GROUP BY runner;
```

```sql
-- what a plan's lanes produced
SELECT runner, outcome, pr, prompt_sha256, json_extract(usage_json,'$.usage.output_tokens')
  FROM lane_run WHERE plan = ? ORDER BY started_at;
```

## The meeting

*Plan `plans/0102-lane-tests-meet-the-implementation.md`.* `bin/fleet-meet`
runs a lane's pull-request files against an implementation pull request's head
and writes one findings document.

```
bin/fleet-meet <repo> <plan-id> --impl <pr> [--lane <pr>]... [--dry-run]
```

1. **The implementation PR** is read through `get_pull` and must be open or
   merged, authored by the author role, and carry `Plan: <plan-id>` on a line of
   its own (matched the way `marked_pr()` builds it, from `MARKER`). Its head sha
   is read once.
2. **The lanes** are the PRs named by `--lane`, or else every PR in any state
   (listed to exhaustion; a listing that times out or otherwise fails in
   transport exits 75, one that ends on an HTTP error status exits 1) that is authored
   by the author role, has head branch `agent/tests/<plan>/<runner>` and
   `Tests-for: <plan>` in the body's header block. A named PR that is none of
   those is refused with exit 1.
3. **The precondition** — a lane runs only when it has an `APPROVED` review,
   not `dismissed` and not `stale`, by the human position (`agent_roles()["merger"]`,
   and not an agent account), whose `commit_id` equals the lane PR's head. That
   head is read once, with the reviews, and is the sha fetched and run; the
   branch is never re-read. An unreadable, failed or unfinished review list is
   *not met*. A merge, a comment, an agent's approval or a human approval at an
   earlier head does not count. It is deliberately not `orchestrator.approval_stands`,
   which returns true when the reviews cannot be read. The document records the
   review id, `read_by`, `read_at` and `independent_reader: false`: the reader is
   the fleet's one human, who also approves the implementation (ADR-0006 §5).
4. **The paths**: the approved sha is diffed against its merge-base with main;
   every changed path must be an added, mode-`100644` file under `tests/lane/`
   that does not exist at the implementation head. Otherwise the lane is
   `refused` in the document and nothing of it is imported.
5. **Each lane runs separately**, in a fresh detached worktree at the
   implementation head with only that lane's files (from blobs at the approved
   sha) written in, so two lanes' `conftest.py` cannot meet.
6. **The prompt hash** is read from the header block and from 0100's `lane_run`
   row. Two lanes are `comparable` only when both hashes are *verified* (body and
   row agree) and equal; the sha, runner and branch are never compared, because
   the same sha can produce different prompts.
7. **The document** (`config/meeting-findings.schema.json`) is written
   atomically, after validating against the schema, to
   `<work root>/meetings/<plan>/<impl-sha12>/findings.json`; the path is printed.

**Exit codes.** `0` a document was written (a document full of failures is still
`0`; so are a lane refused for its paths and a lane timeout — findings are
output); `1` a host-level failure (no `sandbox-exec`, the start-up probe failed,
a forge read that returned an HTTP error status, the store cannot fetch, or a refused implementation/named
lane) — no document is written, because a broken sandbox recorded as a finished
meeting would never be retried until a head moved; `2` a usage error; `75` no lane
has the human approval at its head, or a forge read failed in transport (timeout,
connection refused or reset, DNS) — later, unchanged, this may pass. `--dry-run`
evaluates the precondition, lists each lane's files (names only — nothing is
imported) and the comparability verdict, runs nothing and exits `0`.

**What a skip means.** A skip is *untested*, not green. The first lane written,
eunomia#653, calls `pytest.skip(<reason>)` in at least eight places where the
contract is silent, which is the right behaviour for a black-box test. A count of
"passed" that drops them would read as green for a suite that tested none of
those cases. So a skip is its own outcome; `xfailed` maps to `skip` with the
reason prefixed `xfail: `; and `all_passed` is false whenever a skip, failure,
error, unread or refused lane exists. Outcomes come from a small pytest plugin
(`meet_report`, written into the scratch plugin directory, never the worktree) that
records pytest's own structured report fields — not JUnit XML (a non-strict xpass is
a plain pass there) and not `-rA` text (it carries vendor stdout). A file that
fails to import is one `error` entry; one that yields no tests is one `error`,
`no tests collected`. The runner passes `--continue-on-collection-errors` so one
broken file does not hide the rest.

### The confined runner

`config/meeting-runner.sb` run under `/usr/bin/sandbox-exec`, its parameters
filled with `-D`, never by string formatting. The environment is built from
nothing: `PATH`, `HOME` and `TMPDIR` (scratch), `PYTHONPATH` (the scratch plugin
directory and the directory holding `pytest`, resolved at run time),
`PYTHONDONTWRITEBYTECODE`, `LANG` and a per-run `MEET_RUN_NONCE`. Nothing else:
no `FLEET_*`, no `EUNOMIA_*`, no `SSH_AUTH_SOCK`, no token. The run times out at
`MEET_TIMEOUT_SECONDS = 900`. When it ends, the pgid is the one recorded AT SPAWN
(`Popen(..., start_new_session=True)` makes the leader its own process group, so
its pgid equals its pid the moment it starts) — never re-read afterward with
`getpgid(proc.pid)`, which raises once the leader has been reaped by a normal
exit and silently skips the kill on exactly the run most likely to leave a
group member behind. `SIGTERM` then `SIGKILL` are always both sent to that
group, whether the run finished, was killed by, or timed out. Then every
process of this user whose environment carries the nonce (`ps -E`; a child
that called `os.setsid()` has left the group but keeps the variable) is swept,
repeated until none remain or the meeting is refused after 10 s. **What the
nonce sweep cannot catch:** a grandchild that re-execs (or is otherwise
launched) with a scrubbed environment — one that drops `MEET_RUN_NONCE` before
`ps -E` can see it — carries no mark the sweep matches, and is invisible to it
whether or not it also left the process group. There is no unconfined fallback
and no flag to skip the profile; the seams the tests use are module variables.

The profile denies writes by default and allows only the lane worktree, the scratch
directory, `/dev/null`, `/dev/tty` and `/dev/dtracehelper` (a deny-list of
places fails open on the directory it forgot — `/opt/homebrew/bin` holds the
`claude` and `codex` symlinks the fleet later runs unconfined). It denies reads
under the real home except the worktree, the `.git-store`, the `pytest` directory
and the scratch directory, and the metadata (not the listing, not anything in
it) of the one directory above the worktree: pytest stats that directory while
collecting, and without it the first real meeting collected nothing. pytest is
given `--rootdir <worktree>`. Network is denied except loopback, and TCP to
`localhost:8200` (keyvault) is denied by name on all three axes: outbound,
`network-bind` and `network-inbound` (a confined test may not connect to it, bind
it on any address, or accept on it). It denies `job-creation`,
`appleevent-send`, `lsopen` and all `mach-lookup`. It denies `signal` to every
process outside the sandbox (`(allow signal (target same-sandbox))` is the one
exception) — without it, confined code could `kill()` any process of this uid,
including `fleet-watch`, the orchestrator or `fleet-meet` itself.

Measured live on opshost, 2026-09-29: macOS 27.0 (26A5406e), `/usr/bin/python3`
3.9.6 (Xcode), pytest 8.4.2. Every probe below is a lane test run through
`fleet-meet`'s own runner, and again with the profile removed (a test-only seam);
`FLEET_MEET_LIVE=1 python3 -m pytest tests/test_fleet_meet.py -k confinement`.

| probe | confined | control, no profile |
|---|---|---|
| TCP to a non-loopback host (the forge, by address) | denied | connects |
| loopback HTTP round trip, by IP and by `localhost` | passes | passes |
| TCP to `127.0.0.1:8200`, listener bound outside the sandbox | denied | connects |
| read a nonce file in the real `~` | denied | reads |
| connect to a unix socket bound in the real `~` | denied | connects |
| connect to a unix socket outside `~` (stand-in for `$SSH_AUTH_SOCK`) | denied | connects |
| read an item in a throwaway keychain (file outside `~`) | denied | reads the secret |
| write into a directory made under `/opt/homebrew` | denied | writes |
| write into a directory made under `/Users/Shared` | denied | writes |
| write into the operator's real `/private/var/folders` temp directory | denied | writes |
| **read** a nonce file under `/Users/Shared` | **allowed** | reads |
| `launchctl submit -- /usr/bin/touch <nonce>` | no file | file created |
| Apple Event to System Events that makes a file in the real `~` | no file | file created |
| `open -g` on a `.command` file that touches a nonce path | no file | file created |
| environment seen from a test | exactly the key set above | — |
| bind `127.0.0.1:8200`, listener bound outside the sandbox first stopped | denied | binds |
| `kill(pid, 0)` on a process started outside the sandbox | denied | succeeds |

Reads outside `~` are **not** denied — the `/Users/Shared` row is asserted, not
assumed. Four findings that differ from what the plan expected:

- **The Keychain.** With every Keychain service name denied *by name*
  (`SecurityServer`, `securityd`, `secd`, …) a throwaway keychain item was still
  readable, confined. Denying **all** `mach-lookup` is what closes it, and pytest,
  `subprocess`, `git`, `sqlite3`, `curl` and a loopback HTTP server all run under it,
  so that form ships with no named exceptions. Not run under it: the repo's own
  programs such as `bin/fleet-watch` beyond what the suite spawns.
- **`8200`.** `(deny network-outbound (remote ip "localhost:8200"))` did **not**
  stop a connect to a real listener; `(remote tcp …)` did. The first measurement
  could not show that because nothing listens on 8200 here.
- **Apple Events.** `tell application "System Events" to get name` is *answered*
  confined (the name comes from the application dictionary, not an event that does
  anything), so the probe makes System Events create a file in the real `~`, which
  it does in the control and is refused (`-10827`) confined.
- **The three process-escape rules are not shown load-bearing.** Under a profile of
  `(allow default)` alone, all three escapes were already refused on this host — a
  sandboxed caller is turned away by launchd, LaunchServices and System Events
  themselves. `job-creation`, `appleevent-send` and `lsopen` are kept as
  belt-and-braces; dropping any one still left every probe denied.

An in-test `os.environ` also carries `PYTEST_*` (set by pytest) and macOS's
`__CF_USER_TEXT_ENCODING`, which the probe ignores.

## What is not built here

- No mediator (parked note 0016's remaining shape). Triaging what the
  meeting finds is plan 0103, and the steward calls it (plan 0104, below).
- No loop, no retry. A failed run is recorded and paged; a person runs it
  again. These runners spend the owner's personal subscriptions (issue
  #483).

## Dispatched lanes (plan 0104)

`bin/fleet-watch` starts a lane for every resolved `tester` row when it
dispatches a `ready` plan (`docs/plan-dispatch.md` §What ignition does). Three
things are specific to a dispatched lane.

**`--lease <id>`.** With it, `fleet-lane` activates the lease before its first
gate (`orchestrator.activate`), heartbeats for the run, and releases the lease
on **every controlled exit** — success, refusal or failure — then emits
`lane-ended` with the outcome 0100's record holds, the lane PR number if one was
opened, and the dispatch sha. Releasing re-arms nothing: a lane is only ever
dispatched with its implementation lane. An uncontrolled death (SIGKILL, power)
releases nothing and the lease derives orphaned; that is the `dead` signal, paged
once by the watcher. Without `--lease` the program behaves as it always did.

**A lane-private reader store.** The reader worktree is attached to
`<work root>/<repo slug>/.lane-store`, fetched with the single refspec
`+refs/heads/main:refs/remotes/origin/main` and no tags, and checked out at
`--sha`, which must be an ancestor of `origin/main` or the run refuses. No
implementation branch, and no other head, is ever an object in it. The shared
per-repo store the implementation pushes through holds
`refs/remotes/origin/feat/<slug>`, and every linked worktree of it can read that
(parked note 0016 measured it), so the reader does not use it. The vendor still
receives only prompt bytes; this makes the wrapper and `fleet-bundle` equally
unable to read the implementation, so a future bundle extractor that reads git
cannot leak it by accident.

What this does **not** claim: that the implementer cannot see the tests. It
holds a implbot token and can list pull requests. The guarantee is the
direction ADR-0006 requires: the tester never sees the implementation.

**`Test-lane:` lines.** The orchestrator writes one line per lane into the
implementation PR's body, appended under `<!-- eunomia:test-lanes -->` and
closed by `<!-- /eunomia:test-lanes -->`; only that section is ever rewritten,
and only on a change. A line is derived from durable state alone, so a respawned
orchestrator renders the same lines, by the first row that answers:

| source | state shown |
|---|---|
| a implbot PR, head `agent/tests/<plan>/<runner>`, body `Tests-for: <plan>` (read from the forge) | `opened #N` |
| the `lane_run` record for `(repo, plan, runner, dispatch sha)` | its outcome, in 0100's words |
| a live lane lease | `running` |
| an orphaned lane lease with no record | `dead — no result` |
| a `lane-skipped` event | `skipped — <reason>` |
| nothing | `missing — no lane record` |

A change into `dead`, `skipped`, `missing` or a refusal outcome also posts one
comment. Vendor-derived text (a refusal's detail) is quoted with every line
prefixed `> ` and every `<!--` removed, and the whole text is checked against
the orchestrator's own `_ACK_RE` and for any `<!-- eunomia:` marker other than
the test-lanes pair; if either check fails the text is replaced with `(vendor
text withheld)`. A implbot comment is exactly what `acked_ids()` reads, so an
unfenced `Ack: #n` in vendor text would consume an owner instruction.

Whenever the steward (or the review loop) refreshes those lines and finds a lane
`opened #<n>` with no review by each expected reviewer at its head, it starts
the review of `#<n>` in the background (plan 0139) and logs `test-lanes
review=<n> head=<sha8>`. It does not wait for it, and `test-lanes` is a report
phase, so the steward is still read as waiting on a human. A failed dispatch is
paged once per lane head.

The steward also runs `fleet-meet` and `fleet-triage` for the PR (see
`docs/plan-dispatch.md`). The meeting runs once per sha pair, read from
`findings.json` itself: it is skipped when one exists for the current
implementation head and every lane head it records still equals that lane PR's
head. A lane PR approved later than the first meeting is picked up on the next
implementation push.

## Meeting measurements (plan 0102)

Measured on opshost, 2026-09-29 (macOS 27.0 26A5406e, Python 3.9.6, pytest 8.4.2):

1. **Live confinement** — the table under "The confined runner", from
   `FLEET_MEET_LIVE=1 python3 -m pytest tests/test_fleet_meet.py -k confinement`.
2. **`--dry-run` against a real implementation PR and a real lane PR**:
   `fleet-meet operator/eunomia 0093-a-cycle-reads-the-forge-once --impl 636 --lane 653 --dry-run`
   accepted #636 (merged, `Plan:` line present), reported lane 653 at
   `0fd43f6b2f0800840f90021fa03aea3f32abfe5f` as **NOT RUN — no review**, and
   showed that it would be refused: `tests/test_blackbox_0093_forge_reads.py: not
   under tests/lane/` (D8 — #653 landed before the move to `tests/lane/`). The
   listing of the repo's 406 pull requests to find #636 took several minutes at
   Forgejo's measured cost per page.
3. **A real meeting and its document** — 2026-10-13, plan 0130, lane
   eunomia#879 (`sub-antigravity`, approved by the owner at `6135701`, review
   4562) against implementation eunomia#861 at `348a885`.
   - *As shipped:* 0 tests ran. Collection ended `PermissionError: Operation
     not permitted: '…/work/operator_s_eunomia'`, the directory above the
     worktree, and the file was recorded `no tests collected`. Every live test
     of this program had kept its work root under the system temp directory,
     outside the profile's home rule.
   - *With the parent's metadata admitted:* the lane ran in 6.3 s: 6 passed,
     2 failed, 6 errors, 0 skipped, `all_passed: false`. The six errors are one
     cause, a fixture's `git init --bare` exiting 128 under the profile. None
     of the eight has been triaged.

## The prompt: order and budget (plan 0141)

`build_prompt` assembles, in this order, and stops before `PROMPT_BUDGET` bytes:

1. the preamble and the plan, verbatim (never cut);
2. every ADR the plan cites, by `docs/adr/NNNN-*.md` path **or** by bare name
   (`ADR-NNNN` resolves to the one matching file; none, or more than one, is
   named under `--- notes ---` and skipped, never guessed). This reverses plan
   0070's "a name without a path is not an ADR reference" for the prompt only:
   plans cite ADRs by name, so that rule kept every ADR out of plan 0138's lanes;
3. from the bundle, in full: the CLI entries of the programs the plan's `paths`
   lease names, the `config_keys` read by them, and each `declared_behaviour`
   section whose heading names a leased path;
4. an index: every other program's name and one line of its synopsis;
5. the rest of the bundle, as budget allows, cut only at whole top-level keys
   or whole `declared_behaviour` sections, then
   `--- omitted: <names> (<n> bytes) ---`.

If items 1-3 alone exceed the budget the lane refuses before the cap is
reserved: `refused: the plan's own contract is <n> bytes, over the <budget>-byte
prompt budget`. A tester that cannot see the plan's own contract would only
guess. The bundle itself is unchanged; the prompt's sha256 is of the exact text
sent.

## A test says what it covers (plan 0142)

`fleet-lane` derives the plan's item ids from the plan text: `D<n>` for each
`### D<n>.` heading or `**D<n>:**` lead under `## 2.`, and `DoD<n>` for each
numbered item under `## 4.`, in order, deduplicated. The number is the one the
plan wrote; an item it did not number gets no id. A plan with none has no ids,
and the prompt then says nothing about them.

The preamble lists each id with its first line of text (a `--- PLAN ITEMS ---`
block before the plan, inside the prompt budget) and asks the tester to mark
every test with `@pytest.mark.covers("D2", "DoD4")` naming the ids it checks,
and only those. A test may name none. A missing marker never fails a test or a
lane, and coverage is never inferred from test names or docstrings.

`fleet-meet`'s plugin registers the `covers` marker (no unknown-marker warning)
and records, for each test in the findings document:

- `covers`: the ids its `covers` markers name (`[]` for an untagged test);
- `covers_unknown`: those ids that the plan at the implementation head
  (`plans/<plan>.md`) does not list. Absent when that file cannot be read there.

Both are optional in `config/meeting-findings.schema.json`, so a document
written before this plan still validates. The lane result schema is unchanged.

### Prompt budget (D1) — NOT YET MEASURED

The budget is to be the smallest of the largest prompts each tester reads
without truncation (antigravity: no `<truncated N bytes>` in its view; codex:
the whole prompt reaches the model in one message; opus), less a quarter, using
a prompt that asks the model to quote a marker placed at its end.

| runner | CLI version | largest prompt read whole | date |
|---|---|---|---|
| `sub-antigravity` | 1.3.3 / 1.4.0 | *(pending)* | |
| `sub-codex` | 0.153.4 | *(pending)* | |
| `sub-opus` | 2.1.301 | *(pending)* | |

Until it is recorded, `PROMPT_BUDGET` is 140,000: a provisional figure, not a
measurement. Its only evidence is eunomia#922, where a 214-218 KB prompt reached
antigravity 1.3.3 with 31,011 bytes cut (so it read at most ~187 KB whole), less
a quarter. Eunomia's largest plans come out at about 139.5 KB with it.

## Two lanes are compared, item by item (plan 0143)

When a meeting has two or more lanes and `compare` says they saw the same
verified prompt, `findings.json` gains `comparison`; otherwise it is absent and
`comparability` keeps the reason. It is a count over recorded outcomes. No
model writes it, and each lane keeps its own pull request.

- `items`: one entry per plan item id (plan 0142), with its first line and, per
  lane, `tests`, `pass`, `fail` (failed or errored) and `skip`, and a `verdict`:
  - `agree-pass`: every lane covers it and all those tests pass;
  - `agree-fail`: every lane covers it and each has a failure;
  - `disagree`: every lane covers it, one lane's tests all pass and another's
    do not. This is where the specification is ambiguous, or one lane's test is
    wrong (ADR-0006 §3);
  - `one-lane`: only some lanes cover it;
  - `uncovered`: no lane covers it;
  - `inconclusive`: every lane covers it, no lane's tests all pass, and not every
    lane has a failure (skips). The plan names five verdicts; this is the
    remainder, so such an item is not called agreement.
- `untagged`: per lane, the tests with no `covers`, with their outcomes.
- `lanes_missing`: lanes the plan dispatched that are not in the meeting, with
  the reason (from `not_run` and the `lane-dispatched` events). Any of them sets
  `partial: true`.
- `gaps`: each lane's gaps as written in its pull request body (`null` when the
  block does not parse). Gaps carry no item ids, so they are not matched.

`fleet-triage` renders a `### Lanes compared` section before its
classifications: a row per item (id, first line, each lane's pass/fail/skip,
verdict), the untagged counts, any missing lane, and each lane's gaps fenced
side by side. A `disagree` row names the classified rows of its failing tests.
The section comes from the findings document alone; triage's gap check is
unchanged.

## Manual measurements (D7)

Recorded here, with CLI versions, the first time each is performed on opshost:

1. **Dry run against eunomia at `main`.** — 2026-10-12, `main` 79cbdc5,
   antigravity 1.3.3, plan `0130-a-pr-can-be-reviewed-on-the-cloud-credit`:
   every gate passed, prompt 214,267 bytes, sha256 `54258f5e…`, 19 bundle
   gaps (18 programs with no declared surface, and `openapi`).
2. **One real antigravity run** of that prompt, its result read by the
   adapter, its PR opened. — 2026-10-12: the run through this program ended
   `bad-result` in 20 s (see "`sub-antigravity` answers on its second turn").
   The same prompt through this program's `spawn_lane` and adapter, by hand
   (no landing): the second line was sent after the denied first turn, and
   the result was one file under `tests/lane/`, 9 gaps, no schema errors,
   130,065 tokens as the CLI counts them, 207 s. *(A run through this
   program that opens the PR is still pending.)*
3. **codex: the confinement, then the control.** Plan 0101. The live pair ran
   2026-10-13 (codex-cli 0.153.4): confined, a valid document, neither nonce
   and no command executed; control, both nonces. The confined stream carries
   no item for the attempt; the owner accepted "no command executed" and
   `sub-codex` is wired. Counts and `auth.json` mtimes are in the measurement
   record. *(A run through this program that opens the PR is still pending.)*
4. **The antigravity settings precondition, both ways**: with
   `permissions.allow` present in `~/.gemini/antigravity-cli/settings.json`
   the precondition refuses; with it removed, a by-hand (never through this
   wrapper) headless read of a marker file succeeds, proving that file is
   the one antigravity reads; with the entry removed, the same read is
   denied. — the last third only, 2026-10-12, antigravity 1.3.3, no
   `settings.json` in `~/.gemini/antigravity-cli/`: a headless `view_file` on
   a marker outside the working directory was denied (`denied_actions`:
   `read_file`) and the marker's text did not come back. `view_file` on the
   run's own `~/.gemini/antigravity-cli/brain/<conversation>/` is allowed.
   *(The two runs with `permissions.allow` present are still pending.)*
5. **An operator skill in antigravity's skills directory** — whether it is
   available to a headless run, which decides whether plan 0066's argv
   tuple needs a flag. — *(pending)*

The handoff (plan §5) is the same list, filled in, plus the prompt size
measured against eunomia's own bundle and what the human egress read found.
