# Plan dispatch — merge is ignition

*Roadmap row 6b, plans `plans/0004-plan-dispatcher.md` and
`plans/0010-implementer-session.md`. This document covers the **watcher**
(shipped) and the **orchestrator**, which is complete: parts 2a–2d are shipped
and `--ready` exits 0 (see "The orchestrator" below).*

## The one-sentence model

You write a feature plan, open it as a PR, and **merge it**. That merge is the
ignition: nothing else arms, schedules, or approves the work. `fleet-watch`
sees a `status: ready` plan reachable on `main` with no work PR against it, and
starts exactly one orchestrator for it.

Reachability on `main` **is** the authorisation. `fleet-plan lint` checks
validity, not authority — only a human's reviewed merge produces reachability,
which the watcher enforces by refusing any repo where an agent account could
push, approve, or merge (see the prerequisite table below). A `ready`
plan sitting on an unmerged branch is invisible to the watcher by design.

A plan file is `plans/<dddd>-<slug>.md` (`0000-` is the template). Only that
shape ignites — so `plans/README.md` is documentation, not a dormant ignition
file, whatever front matter it carries.

**Where it runs: opshost, as the operator's user, not root** (decided 2026-09-01,
closing r10 M3). Plan 0004 said cihost holding a `service` lease; this document
said opshost with a flock. opshost wins, and the flock is the mechanism.

The user matters as much as the host. Running as root — which the "use a root
LaunchDaemon" advice implies — makes every dispatch lease and lockfile
`root:wheel`, and then the user's own `fleet-status`, `fleet-prepush` and
`fleet-release` hit `PermissionError` and *skip with a warning*: the lane
enforcement the branch lease is said to buy for free would silently not exist
for human pushes. Root's `Path.home()` is also `/var/root`, so the ledger and
the token helper would both resolve somewhere else entirely — a second, empty
fleet directory that looks like a working one.

So: a user LaunchAgent on opshost, `_single_instance()`'s flock as the one-watcher
guarantee, and no `service` lease. The macOS Local Network Privacy caveat that
motivated the root-daemon advice does not apply here, because opshost reaches
Forgejo over the LAN as the logged-in user already.

## What the watcher will and will not do

It is deliberately mechanical: it reads Forgejo, assigns a lease, spawns a
process, emits an event, and sends a push notification. It never edits a repo,
never talks to a model, and never acts on staleness.

**It refuses more than it accepts.** Each of these is a hard stop, not a
degradation:

| Condition | Behaviour |
|---|---|
| repo not in `FLEET_WATCH_REPOS` | never read at all |
| `main` unprotected, 0 required approvals, open direct pushes, or any lane an agent could use to push/approve/merge | repo **REFUSED** with a warning — a named lane to go and fix |
| `main`'s protection could not be READ (broker unreachable, keyvault sealed, Forgejo down) | repo **UNDETERMINED**, not refused — nothing dispatched and nothing vouched for |
| forgejo-broker unreachable at the pre-flight | no repo scanned, one page (ADR-0003 §6: page once, scan nothing) |
| Forgejo token helper fails (keyvault sealed or down) | no repo scanned, one page |
| The PAT **the broker holds on vaulthost** is rejected (401 expired/rotated, 403 not admin) | every repo **UNDETERMINED**, one page, cleared on the next clean scan — fix the PAT on vaulthost, not here |
| `plans/` unreadable | repo skipped this cycle |
| a single plan file unreadable | that plan skipped, with a warning; the repo's other plans still dispatch |
| dedupe scan unreadable or truncated | **never spawn** (cannot prove undispatched) |
| no Forgejo token | not a single API call |
| a marked PR exists in ANY state | plan is done — a closed PR does not re-arm it |
| lease assign or spawn fails | lease released, `plan-failed`, push notification |

The unprotected-`main` refusal is the one place this fleet does **not** degrade
open. Everywhere else an unknown means "allow and warn"; here it would mean
"run code from a source that something other than a reviewed merge can write."

## Dedupe, and why the marker is reserved

A plan is considered dispatched when a **implbot-authored** PR in **any state**
carries `Plan: <id>` in its body. Any state, because a closed work PR must not
re-arm the plan — rejection is a human decision (set `status: abandoned`, or
re-file under a new id), never an automatic retry. Implbot-authored, because
the marker belongs to work PRs only: intakebot's plan PRs never emit it, so a plan
PR can never look like completed work.

Which accounts count as implementers is configured: `FLEET_IMPLEMENTER_UIDS`
(numeric, rename-proof) **union** `FLEET_IMPLEMENTER_LOGINS`, which always
includes the built-in `implbot` — the env var adds to that set and cannot
remove from it (r10 L3 corrected prose that called logins a "fallback when no
uid is configured"; they are always consulted). Prefer uids, and note the union is deliberate — dedupe gates
spend, so if a rename or a partial uid rollout makes historical work PRs
invisible, every merged-and-still-`ready` plan reads as undispatched and
re-dispatches: cap-throttled, but continuous and silent.

The dedupe scan is bounded (`FLEET_PR_PAGE_CAP`, 20 pages × 50). Hitting the
bound is treated as **unreadable**, not as "no PR found" — a truncated scan
must never authorise a duplicate dispatch.

## Leases, death, and respawn

Each dispatch assigns a real `branch` lease on the branch the orchestrator will
create, held by a generated session id and noted `plan:<id>`. That choice buys
three things for free: row 6's pre-push hook enforces the lane, row 4's
heartbeat makes a dead orchestrator *derive* orphaned, and the concurrency cap
is just "how many of those leases are live."

When an orchestrator dies, the watcher **surfaces and stops** — one push
notification (once, not once per cycle), naming the repo, plan, and open PR. It
does not respawn: acting on staleness is exactly what SPEC Principle 4 forbids.

Recovery is a human act with an automated hand: comment `respawn` on the marked
PR and the next cycle spawns exactly one successor. That authorization is
**consumed, not standing** — only a comment newer than the dispatch it replaces
counts, so one old `respawn` can never auto-restart every future orphaning. The
dead lease is retired immediately AFTER the successor is assigned — retiring
first stranded the plan whenever that assign failed — and if the release
itself fails you get a page, because two live lanes on one branch would make
row 6's pre-push hook refuse the successor's own pushes. Respawn respects the cap like any other dispatch, and `--dry-run`
is dry here too.

Authorisation for that comment binds to a **numeric Forgejo user id**
(`FLEET_OPERATOR_UID`), never a display name — a rename must not be able to
grant it. With the id unset, respawn is simply disabled.

## What supervises a dispatch

Plan 0053. `FLEET_SPAWN=launchd` (the default) no longer writes a plist or
calls `launchctl` itself — it goes through `bin/fleetjob.py`'s `start()`,
which picks the actual supervisor with `fleetjob.select_impl()`: launchd if
the host has `launchctl` on `PATH`, a systemd **transient `--user` unit**
(`systemd-run --user --unit=<label>`) otherwise. Nothing on opshost changes —
`select_impl()` finds `launchctl` there and behaves exactly as before — this
is what lets the same watcher binary run on a systemd host without a
config change. Which supervisor answered is logged once per dispatch, in the
`plan-dispatched` event's `spawn` field (`bin/fleet-events --type
plan-dispatched`), not just inferred from the host.

**Label mapping.** `org.eunomia.fleet-orch.<lease-id>` (`job_label()`) is
handed to either supervisor **unchanged** — a launchd `Label` as-is, and a
systemd unit name via `--unit=<label>`, to which systemd appends `.service`
itself. The alphabet `job_label()` produces — lowercase letters, digits,
`-`, `.` — is already legal under both, so there is no remapping step, and
no label this scheme can produce is legal under one supervisor and not the
other; a lease id that is not label-safe (`lib._ID_RE`) is refused before
either is asked.

**Log destination — a real divergence, not a bug.** `fleet-watch`'s own
dispatch call passes no `log_path` to `fleetjob.start()`, so neither
supervisor is told where to put the orchestrator's stdout/stderr — matching
the historical Popen path's `stderr=DEVNULL` (see "Why a failing wrapper
keeps its lease" below): the structured `<lease-id>.log` this document names
under "Where a run leaves evidence" is the evidence channel, never the
process's own streams. The two supervisors do not behave identically here,
though. launchd with no `StandardOutPath` set discards the streams outright,
the same as always. A systemd transient unit with no `StandardOutput`
override does **not** discard them — they land in the **user journal**
(`journalctl --user -u org.eunomia.fleet-orch.<lease-id>.service`). An
operator troubleshooting a systemd-hosted watcher will therefore find MORE
there than an opshost operator ever could; that is a bonus this document is
recording so it is not mistaken for a new failure surface, and it changes
nothing about what counts as authoritative evidence.

## Running it

```
FLEET_WATCH_REPOS=operator/sniff FLEET_OPERATOR_UID=<id> \
  bin/fleet-watch --dry-run
```

`--dry-run` prints what it would dispatch and assigns nothing. Drop it to arm.
Set `FLEET_NTFY_URL`: the runbook treats pushes as *the* failure channel, and
without it every one-shot page is consumed into a log nobody reads.

**The concurrency cap is configuration, with a record (plan 0068).** Raise or
lower it from a phone, no plist edit and no `launchctl bootout`:

```
bin/fleet-config set dispatch_cap 3
```

`cycle()` resolves the cap fresh every poll, in this order:
`fleet_setting.dispatch_cap` (the row that command writes) → `FLEET_WATCH_CAP`
(the env var above, now the fallback rather than the only knob — an
unconfigured fleet behaves exactly as before) → `1`. A `fleet.db` that is
absent, locked, or lacks the table falls through the same way: the cap is a
throttle, and a throttle's failure mode is the old value, not a stop. Every
`set` writes a `fleet_setting_change` row and a `fleet-setting-changed` ledger
event before it commits — the same durability shape `bin/fleet-models` gave
per-repo runner choices in plan 0063 (`docs/fleet-db.md`) — so raising the cap
is an operator act with a record of who and when, not an untracked edit.

**`dispatch_cap 0` is the pause switch, not an error.** It resolves like any
other value and makes `cycle()` dispatch nothing this poll — `live >= cap`
holds before the first repo is even scanned, so the watcher logs `cap 0
(paused)` once per change, not once per plan, and keeps polling (leases still
release, respawn still evaluates against the same cap, orphans still surface).
`fleet-config set dispatch_cap 3` (or any N > 0) resumes it; no restart.
Proven under test only as of plan 0068 — not yet exercised against the
resident watcher on opshost.

It runs under launchd **on opshost**, the ledger host — the watcher does direct
local ledger operations (lease reads, `O_EXCL` marker files), and SPEC has only
opshost mounting the tree. *cihost* is where the orchestrators it spawns run; part
2 wires that hop.

It runs as a **user agent**, per the 2026-09-01 decision at the top of this
document; the "use a root LaunchDaemon" advice this paragraph used to carry is
superseded, and `org.eunomia.fleet-watch.plist` follows the decision. Root would
make every dispatch lease and lockfile `root:wheel`, which that decision judged
worse than the risk.

**The risk it was weighed against is real and is currently unpaged.** macOS Local
Network Privacy has already silently `EHOSTUNREACH`-ed third-party launchd
*agents* reaching the LAN on this fleet (the cihost SPIRE incident). If it
happens here, the Forgejo reads over `_api` — `plans/` listings, plan bodies,
the dedupe scan — fail, `plan_files` returns None, and
`cycle()` warns "plans/ unreadable — skipped this cycle" per repo and continues.
Nothing pages: since ADR-0003 the `unreadable` counter behind the "Watcher blind"
page is incremented **only** by a broker verdict of `PROT_UNKNOWN`, which arrives
over a local unix socket that an LNP block does not touch. So the failure mode is
per-repo stderr warnings and stalled dispatch — the stderr-only silence this
document warns about elsewhere, arriving by a different route. Closing it means
paging on repeated `_api` failure, which is not built.

One watcher, one cap, no second scheduler: the mechanism is a
non-blocking `flock` on `locks/fleet-watch.lock`, so a second invocation (a
launchd overlap, or a manual run beside the daemon) exits immediately rather
than racing the first's `inflight` snapshot.

**What the cap counts (plan 0067).** `FLEET_WATCH_CAP` bounds *spending*
orchestrators, not processes. A lease that is not orphaned and whose run log
(`work/logs/<lease-id>.log`, the path `orchestrator.run_log_path` names) ends in
one of the steward's waiting lines — `steward-start`, `steward-foreign`,
`steward-unfreeze`, `steward-followup`, `steward-instruction`,
`steward-refused` — is not counted: its pull request is approved and it is
polling the forge for a human merge, which spends nothing. Anything else counts,
including a lease with no log, an empty or unreadable log, or a last line the
watcher does not recognise, because a lease whose state cannot be read must be
assumed to be spending. A steward that resumes work writes `implementer-start`,
which is not a waiting line, so it counts again with no state kept in the
watcher. The log, not `dispatch_phase` in `fleet.db`, is the source: the table
is filled by `fleet-collect` on its own cadence and lags the log by exactly the
window in which a steward unfreezes.

The one consequence: **the number of orchestrator processes can exceed the cap
by the number of stewards waiting on merges**, which is itself bounded by the
number of approved-and-unmerged fleet pull requests. `watch.log` says so when it
matters — `respawn for <plan> deferred — at cap 1 (live 1, waiting-on-human 1
excluded)`, and the per-cycle summary reads `(cap 1, waiting-on-human 1
excluded)` — and both are unchanged when nothing was excluded.

Two things to know when this changes. The set of waiting lines is a literal in
`bin/fleet-watch` (`STEWARD_WAITING_PHASES`), never a `steward-*` match, and
`tests/test_fleet_watch.py::test_the_waiting_set_is_pinned_and_every_orchestrator_steward_line_is_classified`
greps `bin/orchestrator` for every `steward-` emitter and fails on one that is in
neither that set nor the test's own list of spending-or-ended lines
(`steward-end`, `steward-grant-spent`, `steward-head-unknown`) — a new steward
phase lands as a one-line change in one of the two, with that test. And after a
fix round the orchestrator writes no line when it returns to polling, so until
its next steward line (or `steward-end`) the last line is a spending one and the
lease is counted: conservative, and closed properly only by the orchestrator
writing a waiting line when a round ends, which is a follow-up to
`bin/orchestrator` rather than something this file can do.

## Two verdict sources: broker (default) and direct

`FLEET_VERDICT_SOURCE` (plan 0054) picks which implementation answers "is
`main` protected" — `bin/fleet-watch`'s `verdict(repo)` is the one place both
meet, and it is asked by `cycle()`, `orphan_sweep()`'s respawn gate, and
`fleet-repo check` alike, so all three agree with whichever source a
deployment has chosen.

**`broker` (the default, and what the rest of this document assumes).** The
verdict comes from keyvault's `forgejo-broker` on vaulthost, over
`FLEET_BROKER_SOCKET`. This process holds no admin credential at all — see
"Prerequisite that is not optional, part one" below. This fleet is multi-host,
and the separation buys something real: a compromise of the watcher's host
does not hand over a credential that can read (and, held by the wrong hands,
weaken) every allowlisted repo's protection rule.

**`direct`.** `main_is_protected()` runs in THIS process instead, called with
a credential from `FLEET_DIRECT_TOKEN_CMD` — a command printing an
ADMIN-scoped Forgejo token, no default supplied. This exists for a deployment
where the watcher, the forge credential and the repository already share one
trust boundary — a single-host setup with no broker to stand up — not as a
faster path on this fleet. **What it costs:** the admin-scoped credential
`branch_protections` needs (`reqAdmin()`) now lives in the watcher's own
process, in memory, once per protection check — precisely the exposure ADR-0003
§2 removed by inventing the broker. `FLEET_TOKEN_CMD`'s own default helper
(`~/bin/fetch-forgejo-token.sh`) is write-scoped, not admin, and 403s on this
endpoint; `direct` needs its own, separately-scoped helper, and a deployment
that hands the watcher one is choosing to hold that credential where the
broker used to. Whether that trade is acceptable at all is a question this
plan does not answer for you — it is an ADR-0003 §2 question, tracked on this
plan's own pull request rather than decided here.

A known rough edge: `main_is_protected`'s 401/403 message ends "the PAT is the
one keyvault holds on vaulthost at kv/forgejo/admin; fix it there, not on the
calling host" — true when the broker calls it, and **wrong** when `direct`
does, since then the credential and the calling host are the same place. The
message is shared with the broker's own contract (keyvault's `LANES` table and
`classify()` both match its fixed prefix), so it is not reworded for `direct`;
read "fix it there" as "wherever `FLEET_DIRECT_TOKEN_CMD` actually points" when
running that source.

**Selection is configuration, decided ahead of time, and never a runtime
fallback.** An unreachable broker stays `UNDETERMINED` — it does not fall
through to `direct`, because that fallthrough is exactly how a broker outage
would hand this process an admin credential it does not otherwise hold, at
the moment nobody is watching for it.

**A limit worth knowing before reaching for `direct` on a GitHub-hosted
repo.** `fleetforge`'s GitHub backend has no repo-wide "every branch's rule"
endpoint to answer `get_branch_protections` with — plan 0052 D2 left it
unsupported there, returning `(0, None)` always. Both verdict sources read
that as "could not determine," so **neither** can ever vouch for a
GitHub-hosted repo today; `direct` does not make GitHub-hosted ignition
portable, only Forgejo's admin-scoped call still gates it.

**Prerequisite that is not optional, part one — the broker, not a token.**
`/repos/{owner}/{repo}/branch_protections` is behind `reqAdmin()`, so reading it
needs the OWNER's PAT. Since infra ADR-0003 §2 the watcher **does not hold one**:
it asks keyvault's `forgejo-broker` on vaulthost for the *verdict* and receives
`{ok, lane, detail}`. **`FLEET_ADMIN_TOKEN_CMD` is retired** — `fleet-watch` no
longer reads it, mints an admin token, or makes the call.

**A knob moved hosts with it — one of them, wholly; the other only partly.**

`FLEET_AGENT_ACCOUNTS` is consulted **only** inside `main_is_protected`, which
now runs in the BROKER's process on vaulthost. Setting it here changes nothing, and
the failure is **silent and fail-OPEN**: add `queuebot` on this host, and a repo
whose merge whitelist holds `queuebot` still comes back `ok: true` and gets
vouched for. Set it on keyvault's
`net.operator.keyvault-forgejo-broker.plist` instead. It is deliberately not
carried in the request — a caller that could supply the agent list could supply
an empty one and be vouched for, which is the "authorize on something the caller
controls" shape ADR-0003 §3 forbids.

`FLEET_FORGEJO_URL` is **read in both places, and they are not the same read.**
Only the protection VERDICT moved to the broker. Every other call the watcher
still makes — `plans/` listings, plan bodies, the dedupe PR scan, respawn
comments — goes through `_api` in `bin/fleet-watch` using **this host's**
value. So keep it set here, set it on the broker's plist too, and set both to
the same instance.

Unsetting it is usually harmless — the default is this fleet's instance. The
silent stall is when the value, or that default, names an instance this host
cannot reach: every repo reports `plans/ unreadable — skipped`, which warns and
never pages, and dispatch stops in the stderr-only silence this module has
removed eight times.

And the reason both copies must name the SAME instance is fail-open, not
tidiness: the broker would vouch for `main` on instance A while `plan_files`
reads the ignition file from instance B, so a `status: ready` plan sitting on an
UNPROTECTED B would dispatch on A's protection. Merge is ignition; the two
halves of that check have to be looking at the same repository.

`~/bin/fetch-forgejo-admin-token.sh` itself is **not deleted yet**, and the
reason is worth recording: ADR-0003's Context measured "only two callers exist"
across *eunomia*, but `agent-bus/scripts/provenance-protect.sh` also uses it —
and it **PATCHes** `branch_protections` rather than reading them. The broker
cannot serve that: it is read-only by design, and returns a verdict, not a
credential. So the helper's retirement (and with it the zero-baseline issuance
alert of ADR-0003 §5) waits on a decision about that caller, which is not this
change's to make.

What to set instead: **`FLEET_BROKER_SOCKET`**, the path of a LOCAL unix socket
that reaches the broker (default `~/run/forgejo-broker.sock`). On opshost that
socket is an ssh forward to vaulthost, started by
`launchd/org.eunomia.fleet-broker-tunnel.plist`. The watcher connects to a local
socket and holds no credential — the ADR-0002 shape, with the ssh forward
standing in for the Ghostunnel/SPIFFE sidecar that will replace it.

**The degradation is a third state, not a refusal.** A broker that cannot answer
— down, tunnel down, keyvault sealed, its own Forgejo call failing — yields
`UNDETERMINED`. The watcher pages once and scans nothing; it does **not** refuse
every repo. `fleet-repo check` exits 2 for the same condition. Anything that
collapses that into "unprotected" recreates the "Watcher blind" misdiagnosis
with the alarm inverted.

The two were one variable until 2026-09-06, and pointing it at the admin helper
handed *every* call an admin token. That stopped being merely untidy when the
stored admin PAT was scoped to `read:repository`: `bin/fleet-candidate:238` posts
a commit status through the same variable, so a single `FLEET_TOKEN_CMD` naming
the admin helper would have started 403ing candidate CI. Two needs, two
variables — `branch_protections` needs admin (read-only), `POST /statuses` needs
implbot (write).

Note what this does **not** mean: do not make an agent account a repo admin to
make the read work. An account that can read a protection rule with an admin
token can also `PATCH` it — weaken the rule, push a `status: ready` plan,
restore it, and the next cycle vouches. This guard inspects a rule's *content*,
never who may rewrite it, so "human is enforced, not assumed" holds only while
no agent account holds admin.

**Prerequisite that is not optional, part two:** at least one allowlisted repo must
protect `main` so that **every** way of writing it passes through a human. The
watcher checks all eight lanes it can see in the branch-protection response,
and refuses the repo if any one of them is open:

| Lane | Required |
|---|---|
| Admin bypass | `apply_to_admins` on — it defaults *off*, and while off a repo admin bypasses every row below; admin membership is not visible in this response |
| Direct push | no unrestricted push; a whitelist must hold no agent account (`implbot`, `revbot`) |
| Push by deploy key | `push_whitelist_deploy_keys` off — a write-scoped key in agent tooling is a self-ignition credential |
| Push by team | no team whitelist; team membership is not visible in this response, so it cannot be vouched for |
| File exemptions | no `unprotected_file_patterns` that cannot be *shown to miss* `plans/` — the check proves confinement (static prefix) rather than sampling one filename, so `**/README.md` and `plans/1*.md` refuse too |
| Approval | `enable_approvals_whitelist` on, holding no agent account — otherwise Forgejo counts an approval from *any* write user, agents included |
| Stale approval | `dismiss_stale_approvals` **or** `ignore_stale_approvals` on — both default *false*, and without one an approval of an earlier tree still satisfies a head that added a `status: ready` plan |
| Merge | `enable_merge_whitelist` on, holding no agent account — otherwise anyone with write, including the merge queue, can merge |

Who counts as an agent is configurable: `FLEET_AGENT_ACCOUNTS` extends the
built-in `implbot`/`revbot` set. **If your merge queue merges under its
own account, put that account in `FLEET_AGENT_ACCOUNTS` — on keyvault's
forgejo-broker plist, NOT on the watcher host.** The rule that reads this
variable runs in the broker's process now (ADR-0003 §2); setting it here is
silently ignored and fails OPEN. Once set there, the merge
whitelist must not contain it, which means these repos are human-merge-only.
That is the honest reading of "merge is ignition"; a queue that can merge is a
queue that can ignite.

The last four matter because "merge is ignition" is a claim about *who*. A
implbot work PR that also adds a `status: ready` plan, approved by
revbot and merged by the queue, reaches `main` with no human anywhere in
the chain — and the plan ignites. Requiring the whitelists is what makes the
word "human" in this document true rather than aspirational.

A rule that merely blocks force-pushes does not qualify — the property being
checked is "only a human's reviewed merge can write main," not "a rule
exists." Until a repo is configured this way every cycle refuses everything and
dispatches nothing, which is working as designed.

## Making a repo an ignition repo

*Operator procedure. This is configuration, not code — until it is done,
`main_is_protected()` refuses the repo and nothing downstream of it ever runs.
The reasoning is [ADR-0004](adr/0004-ignition-gate.md); the spec is
`plans/0015-ignition-gate.md`.*

Measured 2026-09-06: **every one of 20 repos checked refuses**, so assume yours
does too until `fleet-repo check` says otherwise.

**1. Turn on admin enforcement.** `apply_to_admins` defaults *off* in Forgejo,
and while it is off a repo admin bypasses every rule below — admin membership
is not visible in the protection response, so the watcher cannot vouch for it
and refuses. This is the first lane every repo fails.

**2. Take agent accounts off the approvals whitelist.** The standing new-repo
script installs `revbot` + `operator` with `required_approvals: 2`. On an
ignition repo the whitelist holds **humans only**. Do not skip this because
"revbot cannot merge anyway" — the watcher refuses on *approve*, not on
merge, because an approval an agent can cast is an ignition an agent can forge.

**3. Set `required_approvals: 1`.** Not optional and not a preference: with one
human on the whitelist, a requirement of 2 is unsatisfiable — a user holds one
review state — and the repo becomes permanently unmergeable. The review that
step 2 removed comes back as the watcher's own check, bound to the merged SHA.

**4. Leave the merge whitelist alone.** `operator` only. The human merge is
still the authorisation; nothing in the gate replaces it.

**A consequence of steps 1–3 worth knowing before you hit it.** With
`apply_to_admins` on, the whitelist humans-only, `required_approvals: 1`, and
Forgejo forbidding self-approval, a **operator-authored PR to an ignition repo
becomes unmergeable** — today the admin bypass quietly covers that case. So on
these repos every PR must be authored by `implbot`
(`~/bin/forgejo-open-pr.sh`), not opened by hand with the admin token. On
2026-09-06, five PRs across the fleet were opened as `operator` that way and none
of them could reach the required approvals; on an ignition repo that stops being
recoverable by an admin merge.

**5. Enrol it** — **pending plan 0013; `bin/fleet-repo` is not on `main` yet**
(eunomia #54). Once it lands: `fleet-repo enable <owner/repo> --dispatch`, then
`fleet-repo check <owner/repo>`. Until then this step is the env var. Exit 0 eligible, 1 refused with the lane
named, 2 not determinable — and **2 is not 1**: it means the broker could not
answer (unreachable, keyvault sealed, its Forgejo call failing), which says
nothing about the repo and has a different fix. The credential involved is the
one **on vaulthost**, not on this host.

**A token note that has already cost an evening — now mostly historical.** The
`branch_protections` endpoint is behind `reqAdmin()`, and the implbot PAT that
`FLEET_TOKEN_CMD` mints is *write*-scoped but not admin, so it 403s on every
repo. The watcher then refused everything and, before r10, paged "Watcher blind
— LAN privacy", sending the operator after a network fault that did not exist.
That whole class is gone from THIS host: since ADR-0003 §2 the watcher does not
make the call, so there is no admin token here to point at anything. If repos
come back lane `token_rejected`, the PAT at fault is the one **the broker holds
on vaulthost** — fix it there. `FLEET_TOKEN_CMD` still mints the implbot PAT for
everything else, including `bin/fleet-candidate:238`'s commit statuses, which is
why the two were split in #50 and why the split still matters. And do **not**
make an agent account a repo admin to make the read work: an account that can read a protection rule
with an admin token can also `PATCH` it — weaken it, land a `status: ready`
plan, restore it — and this guard inspects a rule's *content*, never who may
rewrite it.

### What ignition will do — none of this is built yet

Stated in the future tense on purpose: the watcher today starts exactly one
orchestrator, as the one-sentence model above says, and neither the gate nor a
second lane exists.

**Planned (`plans/0015-ignition-gate.md`, not yet implemented):** a merged
`status: ready` plan will be checked against the merged SHA — reviewed there, no
open `question:`, and a declared `spec_impact` whose implied files the plan's own
`paths` cover. ADR-0004 records that decision and the alternatives it rejected.

**Planned separately:** a passing plan then spawns a second, independent test
lane. That is its own plan and is not on `main` yet; it will be linked from here
once it lands.

## What the orchestrator owes this watcher

Two of the watcher's safety invariants are promises about the binary it spawns.
They are contracts, not preferences, and part 2a honours both:

1. **Activate and heartbeat promptly.** The dispatch lease is assigned, not
   active; `EUNOMIA_ACTIVATE_TIMEOUT` (10 min) later an unactivated lease
   *derives orphaned*. An orchestrator that is alive but slow to activate
   invites a respawn — and two live processes on one branch.
2. **Never release the lease without a marked PR existing.** Dedupe is
   "a `Plan: <id>` PR exists in any state." A crash-then-release with no PR
   turns "rejection is a human decision" into an automatic hot retry: the plan
   is `ready`, unleased, undispatched, and re-dispatches every cycle — real
   model spend, forever.

While the loop was partial the watcher **refused to arm** — it asks
`orchestrator --ready`, which exited non-zero while any phase was unwired, so a
part-built deployment was inert rather than a paging storm. Since 0032 that gate
**passes**: nothing is unwired and the watcher arms. Merely existing at the path would satisfy the
executable check, which is why the question is asked rather than inferred.

## Telling whether it is working

- `bin/fleet-events --type plan-dispatched --since 24h` — what ignited.
- `bin/fleet-status` — which orchestrator leases are live, and which have
  derived orphaned (a dead orchestrator shows here before the push arrives).
- The push notifications are the failure channel: dispatch failures, dead
  orchestrators, failed respawns, and a dedupe scan that outgrew its page
  bound all surface there rather than in a log nobody reads. Silence with armed plans and no live leases means the watcher is not
  running — check its launchd job, not its logs.


## The orchestrator

`bin/orchestrator` is the per-feature wrapper the watcher spawns. Plan 0004's
structural rule governs it: **every guarantee is code here, and only judgment
lives in the prompt.** A guarantee written as prompt text is a hope.

**Part 2a (shipped)** — lease activation, heartbeat, workspace, plan read,
implementer-prompt construction, the secrecy filter, and **the implementer
session**: routed to a runner, spawned in its own process group with an
allowlisted environment, and verified afterwards from the forge.

**Part 2b (shipped, plan 0011)** — **the review loop**: a review dispatched
under the fixed mandate in `bin/reviewer-contract.md` through a command seam
(`FLEET_REVIEW_CMD`) that holds the reviewer's credential so this wrapper never
does; a verdict read back **off the forge** and bound to the head SHA it
reviewed; a bounded fix loop against HIGH findings only; an iteration ledger and
a deferred-findings comment posted to the PR thread; and exhaustion as a hard
stop that pages and **holds** its lease.

**A red build's fix round carries its stored log (plan 0069).** When CI fails,
the CI-fix prompt's failing-checks block is followed by a `BEGIN CI LOG` block
per failing status context: the excerpt `bin/fleet-ci`'s own `excerpt()`
extracts from the body `fleet-collect` already stored for the matching
`ci_task` row. The row is found by stripping one trailing ` (<event>)` group
off the Forgejo status context and splitting what remains on the first ` / `
— `CI / tests (cihost) (push)` yields the job `tests (cihost)` — then matching
`(head_sha, name)`, highest `id` wins on a rerun. The orchestrator never
fetches from Forgejo or a runner for this and never calls fleet-ci's `sync()`:
it opens `fleet.db` **read-only** and polls every 15 s for up to 210 s — one
`fleet-collect` sweep interval (120 s) plus its own budget (90 s) — for the
body to turn `stored`, giving up early on a state that will never produce one
(`over-cap`, or `absent`/`unreadable` once the collector's own retry window has
passed). Whatever it has when the wait ends is what the prompt carries;
absence is a legible line (`no ci_task row for <sha> after 210 s`), never
silence. The excerpt passes through the wrapper's own `redact()` a second time
before it reaches the prompt — the stored body already passed fleet-ci's rules
at capture; this pass is a second net, not a tripwire. The template tells the
session the stored log is the authority over a local run that cannot reproduce
the failure, and a fix round that pushes nothing gets the failing test ids and
the fleet-ci task id appended to its stop reason, after the pinned sentence
`bin/mopsus` classifies on.

**Part 2c (shipped, plan 0031)** — **the comment/ack protocol and the steward
phase**. The wrapper does not exit at DoD-verified: it stays alive on the marked
PR, heartbeating its lease and polling the thread in a **Python** loop (no model
is kept warm), invoking the model only when an instruction arrives, and releasing
its lease only when the PR is merged or closed.

A comment is an instruction when it is authored by the numeric
`FLEET_OPERATOR_UID`, is not yet acknowledged, and it is processed in ascending
**comment id** order — never timestamp order, because an edit moves a timestamp
and the queue's order is the difference between "fix the test then push" and
"push then fix the test". Each consumed instruction gets an `Ack: #<id>` reply,
and **that reply is the ledger**: there is no local state file, so a successor on
another host reconstructs consumed-versus-pending from the thread alone. An edit
to an acked comment is not new work; deleting an ack re-arms exactly that
instruction, which is the deliberate human "run that again" gesture. Comments
from any other author are quoted as data and explicitly not acted on.

**The ack is posted before the work starts.** A successor arriving mid-execution
must not re-run an instruction already in flight, and the thread is all it can
read. The opposite window — a death between the ack and the push — loses that
instruction, and its recovery is the ack-deletion gesture above.

**Part 2d (shipped, plan 0032)** — **the post-approval freeze**. The invariant
is a condition, not a counter: **pushes are refused whenever an approval stands
at the branch head**, and it is enforced in the wrapper rather than declined by
the prompt. An instruction arriving under a standing approval goes to a
**follow-up** by default — branched from the approved branch because the fix
depends on its work, targeting `main`, body carrying `Follows: #<pr>` and
deliberately **no** `Plan:` marker, which the wrapper verifies rather than
trusting to the prompt. The approved PR stays frozen to be merged first.

`unfreeze`, alone on a line in an owner comment, grants exactly one override. It
is spent by a **successful** push, not by the comment and not by a failed
attempt; that push knowingly dismisses the approval, and the branch **re-freezes
when the next approval lands**. Implementing this as a counter rather than as the
condition gets exactly that last step wrong — the counter still says a push is
owed and the second approval is pushed over.

**Nothing is unwired.** `UNBUILT_PHASES` is empty, `orchestrator --ready` exits
0, and `fleet-watch` arms.

**Transcripts are per session, not per run.** Each spawned session writes
`<lease-id>-<tag>.log`: `r<n>` for a fix round in the feature's own review loop,
`c<cid>` for a steward instruction on the branch, `f<cid>` for a follow-up
session, `f<cid>r<n>` for a fix round inside that follow-up's review, and
`u<cid>r<n>` for one in the re-review after a push. A bare `<lease-id>.log` is
the first implementer session only. Before 0032 every later session reopened one path with `O_TRUNC`
and destroyed its predecessor's output.

### The implementer session (plan 0010)

The session is the judgment core. Everything around it is a guarantee in code,
and the guarantee that matters is **`verify_marked_pr`**, which runs after the
session whatever the session reported and whatever it exited with. All five, or
the run fails: a PR exists, authored by an implementer account, head is this
lease's branch, base is `main`, its body carries `Plan: <id>`, and the plan file
at its head says `status: done`. A zero exit proves nothing — dedupe is "a marked
PR exists", so a model that reports success having opened nothing (or having
opened an unmarked PR) would leave the plan re-dispatchable with its work sitting
in a branch nobody will look at.

| Piece | What it is, and the one thing to know |
|---|---|
| runner registry | `RUNNERS` in `bin/orchestrator`: `sub-sonnet` (default), `sub-opus`, `api-sonnet`, `local-qwen`. A runner, not a model name — it declares the harness, whether it is local, and which credential kind it needs. |
| `EUNOMIA_IMPL_RUNNER` | per-run override, which is what makes a bakeoff arm a one-liner. It is **subject to** the zone check, not a way past it: `route_implementer` resolves the override first and applies the zone rule last. |
| zone rule | ADR-0006 §7 as an allowlist — `zone: public` may reach a cloud runner; private, absent, malformed or mis-cased may not. Nothing asks oracle: oracle is a completion gateway and has no surface naming an agentic harness (this supersedes plan 0004 §3 and reverts if that changes). `tier:` is never consulted — it is the risk tier, not a cost band. |
| `EUNOMIA_INITIATOR` | who filed the run. **Unset means the operator**, which is the normal path and today's only credential (`local_cli`). Set-and-unrecognised stops the run: a run filed by someone else must bill someone else, so there is no fallback. |
| credential kinds | a runner whose required kind the resolved credential does not provide is a hard stop. `api-sonnet` served by `local_cli` would spend the operator's subscription while every event called it metered. |
| timeout | Per-repo minutes from `config/impl-bounds.conf` (plan 0042), not an environment knob — a missing or malformed conf is a Stop, never a quiet 90. Resolved once, at the start of the run, so a conf edited mid-run cannot change what a later comment quotes. The session is spawned in its own process group and the **group** is signalled — `subprocess`'s own timeout kills the direct child, and `claude -p` has grandchildren. A marked PR that appears after the kill is a failure, not a late success. |
| tool allowlist | an explicit `--allowedTools` and a named `--permission-mode`; `--dangerously-skip-permissions` never. Treat this as surface reduction and **not** confinement — Claude Code matches Bash permissions on the command prefix, so even a git-scoped entry admits `git -c alias.x='!<anything>' x`. What bounds the damage is `verify_marked_pr`, branch protection, and the environment allowlist. |
| environment | an **allowlist** in `bin/fleet-candidate:_candidate_env`'s shape (`PATH HOME LANG LC_ALL LC_CTYPE TMPDIR TZ USER LOGNAME SHELL TERM`, plus whatever the runner declares). Not a denylist: the child runs arbitrary shell. This removes `FLEET_TOKEN_CMD` — which on this fleet names the admin helper — and **not the recipe**: that helper is a fixed path any process running as the operator can execute, and this very document names it. Closing that needs the helper gated on the implementer host; it is not closed here. |
| `EUNOMIA_SESSION` | deliberately **absent** from the child. It becomes required the day `fleet-install-hooks` runs against the orchestrator's store (`bin/fleet-prepush` would otherwise refuse the orchestrator's own push), and on that day `FLEET_PUSH_OVERRIDE` stays forbidden — it is the nearest fix a stuck session will find, and it disables row 6 entirely. |
| the branch | `fleetlib.add_worktree` checks out `--detach` at `origin/main`; the wrapper creates the branch, learning its name **from the lease record** rather than recomputing the slug. A second copy of the slug rule drifts, and the drift lands as a push on a branch the lease does not cover. |

### Where a run leaves evidence

Two paths, and every page names them:

- **`<work root>/logs/<lease-id>.log`** — structured facts (phase, exit code,
  fault class), opened **before** the lease is activated so a failure that
  happens before the repo is reachable still leaves something to read. Redacted
  like everything else. It is *not* the failure channel — the push notification
  is; the log is what the page sends the reader to.
- **`~/dev/.orchestrator-transcripts/<lease-id>[-<tag>].log`** — the session's own
  output, mode 0600, deliberately **outside `~/dev/.fleet`** because SPEC
  Principle 6 forbids secret material under a tree atlas renders. Only the path
  is ever quoted into an event or a page.

  Transcripts are redacted with `FINDINGS` **+ `PARANOID_FINDINGS`**, which is
  not the set `redact()` uses for PR comments. The credential a session actually
  holds is a Forgejo PAT — 40 hex characters — and `long-hex`, in the paranoid
  set, is the only shape that matches it. The whole buffer is redacted at the
  end, never per read: a token split across two reads defeats a per-chunk regex.
  (`redact()` keeps the narrow set on purpose; a head SHA is what an approval
  binds to.)

### What was exercised, and what is only declared

For whoever picks up 0011, 0012 or a bakeoff:

- **Exercised:** `sub-sonnet` and the routing/credential/env/timeout/transcript
  machinery, against a fake harness in `tests/test_orchestrator_session.py`.
- **Declared, untested:** `sub-opus` (same harness, different model flag),
  `api-sonnet` (unreachable until a `resolve_credential` branch yields
  `api_key`), and `local-qwen`, which is **declared but not wired** — the
  agent-bus worker consumes a task file from the bus, not a prompt on stdin, so
  reaching it is a hard stop with its own message rather than a spawn error.
  A bakeoff must not discover that mid-run.
- **`resolve_credential` branches:** `local_cli` (exists), and two that do not —
  a Forgejo-uid→person map is what an initiator-per-merger needs (0015), and an
  API key path (a keyvault pull at startup, never an inherited variable) is what
  `api_key` needs.
- **The tool allowlist and transcript path convention are inherited**: 0011's
  failure comments name the transcript path, and 0012's successor sessions
  inherit the allowlist.

### Why the boundary is loud

This is the part to understand before reading a `plan-failed` from it.

Until `bin/orchestrator` existed, the watcher's spawn raised `OSError`, and its
handler released the lease, emitted `plan-failed` and paged. **The moment a file
exists at that path the spawn succeeds** — the watcher emits `plan-dispatched`
and the plan reads as in-flight. A half-built wrapper that simply exited would
therefore convert a loud failure into a plan that *looks armed and is not*,
which is the one thing merge-is-ignition cannot tolerate.

So the built/unbuilt boundary is an explicit stop: emit `plan-failed`, page,
exit non-zero — **and keep the lease**, for the reason "Why a failing wrapper
keeps its lease" gives below. (This sentence used to say "release the lease";
that was never what the code did, and releasing is precisely the re-dispatch loop
the next section describes.) The diagnosis names the phase that is actually
unwired. Every phase is wired now, so that helper is gone; each merged PR moved
the boundary later and none of them ever made it silent.

### What it refuses

| Condition | Behaviour |
|---|---|
| `EUNOMIA_SESSION` unset | refuse before touching the lease — no holder identity, no run |
| lease will not activate | `plan-failed`, page, and **no release** — a lease it does not hold is not its to retire |
| workspace or plan failure of ANY class | `plan-failed`, page — a timeout or a full disk must not escape and page nobody |
| plan path escapes the worktree | refuse; a path that escaped would read the operator's disk into a prompt |
| plan text carries a credential shape | refuse — a secret in a plan file is a human problem, not something to scrub and proceed on |
| initiator set and unrecognised | refuse — a run filed by someone else must bill someone else, so there is no fallback to the operator |
| runner needs a credential kind we do not have | refuse; never a fallback |
| non-public zone with a non-local runner | refuse — ADR-0006 §7 is an allowlist |
| runner harness unwired or not installed | refuse, with the two cases distinguished by their reasons |
| session exits 0 with no verified marked PR | `plan-failed` — the forge is the evidence, not the exit code |
| session times out | process **group** killed; a marked PR appearing afterwards is not a late success |
| any failure exit | **the lease is NOT released** — see below |
| any controlled exit | worktree removed; an uncontrolled death leaves it for the orphan sweep |

### Why a failing wrapper keeps its lease

Every exit part 2a can reach is either a failure with no marked PR, or a verified
marked PR that is open and unreviewed — and this document's own contract says
never to release a lease without a merged one. The reason is
mechanical: the watcher skips a plan only while a dispatch lease is live
(`bin/fleet-watch:1267`). Release, and the plan is undispatched again — a fresh
lease, a fresh spawn and a fresh page **every cycle, forever**. That was
measured, not reasoned about: three cycles produced three leases, three
`plan-dispatched` events and three `plan-failed` events.

Holding the lease instead lets it lapse into orphanhood by heartbeat, and the
sweep then does what it already does for any dead orchestrator — surface once,
never respawn, and wait for a human's `respawn` comment.

One consequence worth knowing at 3am: the page's `reason` is the only channel
that survives. The watcher spawns the orchestrator with `stderr=DEVNULL`, so
anything written there is gone. That is why the reason names the actual fault
("workspace setup failed: git clone failed …") rather than the phase.

### Workspaces

One `git worktree` per lease, attached to a shared per-repo clone, keyed on the
lease id (**[ADR-0001](adr/0001-orchestrator-workspace.md)**). The dispatch lease
is on the *branch*, not the repo, so two plans in one repository dispatch
concurrently — without this they would share one directory. `fleetlib`'s
`orphan_worktrees()` reports trees whose lease is gone and deliberately does not
delete them: the tree is the evidence of how the run died.

### No approval code path

There is no function in `bin/orchestrator` that posts a Forgejo review, and
there must never be. The orchestrator is graded on "approved," so credential
separation alone is not the control — the absent code is, and the test suite
asserts its absence rather than trusting it.
