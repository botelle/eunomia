# Enforcement — where the ledger grows teeth

*Row 6. Until now every control was advisory: the ledger recorded who held
what, and honesty did the rest. These three installers put the checks at the
boundaries where the 2026-08-17 failures actually happened — the push, the
simulator boot, the stray pkill.*

## The polarity rule (read this first)

Every check here blocks **only on positive evidence** — a foreign active
lease, an integrator-owned path without the role. Anything the check cannot
prove degrades **open with a loud stderr WARN**: ledger unreachable, repo
underivable, `OWNERSHIP.yml` unparseable, off-opshost with no ControlMaster.

Why: an enforcement layer that fails closed on infrastructure flake is the
2026-08-27 secret-guard incident at push granularity — one missing file froze
every session on the host. A missed block costs one coordination accident; a
false block costs the fleet. Principle 5 (cooperative, not adversarial) picks
the polarity.

## fleet-prepush + fleet-install-hooks

```
bin/fleet-install-hooks ~/dev/<repo>
```

Installs `.git/hooks/pre-push` (refusing to clobber a hook it didn't write;
idempotent on its own). On every push the hook refuses:

1. **A push to a branch another session's ACTIVE lease holds** — the 8/17
   anti-pattern (a root session force-pushing into a dead worker's branch).
   The fix is coordination or `fleet-claim --takeover`, not the override.
   A dead session's lease keeps blocking until takeover or release — that is
   Principle 4 on purpose (nothing automated acts on staleness), and this hook
   is deliberately no exception.
2. **Commits touching another session's ACTIVE `paths`-lease globs** — that's
   their lane; file a change-request (`docs/change-requests.md`).
3. **Commits touching `integrator:` globs from the repo's `OWNERSHIP.yml`**
   when you hold no integrator-role lease for the repo — same rule, same role,
   one ownership model (plan 0007).

`OWNERSHIP.yml` lives at the target repo's root; the format is deliberately
not YAML-general — `#` comments plus `integrator: <glob>` lines, matched with
fnmatch against repo-relative paths:

```
# who owns the shared surfaces
integrator: project.yml
integrator: *.lock
integrator: OWNERSHIP.yml
```

(fnmatch `*` crosses `/`: `*.lock` matches nested lockfiles too — usually what
an owner wants, but write globs knowing it. And the example's
`integrator: OWNERSHIP.yml` line is LOAD-BEARING, not decoration: the base-
commit read is per-push, so without it a branch can edit the rules in push 1
and launder anything past push 2's gutted copy — always list the file in
itself. And the self-listing defense engages only when a COMMITTED base is
resolvable — on a first push to a never-fetched remote the hook falls back to
the uncommitted worktree copy, which the pusher can edit without a commit
trail; `--no-verify` already concedes deliberate bypass, but know the
guarantee's edge.)

**Keeping a branch current with main does not false-block** on leased content
that already landed there: a path is excused — loudly, on stderr, naming the
paths — when its content is identical to the tracked default tip AND the
branch never touched it (both conditions, or a revert could wipe someone's
unlanded work — 1522 #12 r9). One deliberate edge (r10 L1): a keep-current
merge carrying main's **deletion** of a leased file still blocks, because
absent-at-local is never excusable — the presence guard that keeps "identical
to main" honest cannot distinguish your deletion from main's. It is a
false-block by design, on the conservative side of the polarity rule; the
override (below) is the intended path for that push.

One documented exemption: **branch deletion** (`git push origin :branch`)
passes all checks — the hook sees no new content to examine. A foreign-leased
branch's deletion is still destructive; treat it as takeover-only by
convention until a follow-up row closes it.

**A plain human shell** (no `EUNOMIA_SESSION`) is treated as *not the
holder* — a leased branch blocks even its own human's push, because that is
exactly the 8/17 root-session shape. The block message says which session to
export if the lease is yours.

**git's own bypass exists and is silent**: `git push --no-verify` skips
pre-push hooks entirely — no WARN, no trace (r7 M2). The cooperative model
tolerates its existence, but know the loud-override story below holds only for
pushes that run the hook at all; `--no-verify` in an agent's hands is the
same violation as an exported override, just quieter. If you reach for it,
say so in the PR thread.

**The override** is `FLEET_PUSH_OVERRIDE=1` on one push. It prints exactly
what it overrode. Per-invocation is a CONVENTION the hook cannot enforce — an
`export` in a shell rc is standing permission on your own head (1522 #12 r3:
the earlier "no config can grant it" claim overreached). Off-opshost, the hook reads the ledger over an existing ControlMaster
only (the heartbeat idiom); no master means WARN + allow, never a fresh dial.

The hook never writes the ledger and never emits events.

## fleet-sim

```
bin/fleet-sim boot <udid>
bin/fleet-sim shutdown <udid>
```

`boot` requires an ACTIVE `sim` lease for this host held by your session,
refuses the **4th** booted simulator (the cap that kept cihost's OOM reaper at
bay), and binds the UDID onto your lease under its lock so `fleet-status`
answers "whose sim is that". At capacity it lists the holders and stops — it
refuses, **never reaps** (Principle 4) — and `shutdown` holds that line for
every LEASE-BOUND sim: a UDID bound to another session's active lease is
refused with the holder named. A sim booted outside the ledger has no
ownership record to check — UDID-exact addressing bounds the blast, but the
guarantee is only as wide as the ledger. Raw `simctl` stays the human escape
hatch, and a sim shut down that way leaves a stale binding the next
`fleet-sim shutdown <udid>` clears without complaint (already-gone is the
goal state). The ≤3 check runs under a
host lock, so two racing boots cannot land at four. `FLEET_SIMCTL` overrides
the simctl command (the test seam).

## fleet-pkill

```
bin/fleet-pkill <substring>
```

Kills matching processes only if they are provably **yours** — a live
descendant of your shell, or a pid your session REGISTERED at spawn time:

```
my-server & bin/fleet-pkill --register $!
```

Registration verifies the pid is your descendant *right then* (when ownership
is provable) and records it — with its process START TIME, which survives the
argv retitling setproctitle servers (gunicorn, postgres, nginx) perform, still
catches pid reuse (a stranger wearing the pid started at a different instant,
and is pruned, not killed), and puts nothing sensitive in the tree. That proof
survives the reparenting-to-launchd every backgrounded server undergoes, which
is why live ancestry alone was not enough. (Environment marking via
`ps -E` was the obvious alternative; it returns nothing on modern macOS —
probed, not assumed.) Everything unproven is named on stderr and spared —
ownership beats matching. Simulators are
CoreSimulatorService's children: `fleet-sim shutdown` is the verb for sims.
SIGTERM only.

### `fleet-pkill --session` — a cross-session kill, human-triggered (plan 0014)

```
bin/fleet-pkill --session <sid> --reason "<text>"
```

The default above kills only what is provably the CALLER's own. This is the
other case: a session that has stopped responding cannot register a pid,
release a lease, or be reached from its own shell, because its own shell is
the thing that is stuck. `--session` ends it from outside, on the ledger's
`sessions/<sid>/host` binding (`fleet-bind`, plan 0012) rather than a live
process tree, and every refusal here is a distinct message on stderr, never a
silent no-op:

* **`--reason` is scanned against `config/secret-patterns.conf` and
  size-capped BEFORE ANYTHING IS SIGNALLED** — at argument-parse time, not
  after the kill. `detail.reason` in `events.jsonl` is atlas-rendered; a
  refusal names the matched RULE, never the matched text, so the refusal
  itself cannot become the leak. (Not implemented as an argparse `type=`
  validator: argparse echoes the raw value into its own error message on a
  `ValueError`, which would print the very thing the scan exists to keep out
  of a terminal.)
* Refuses a sid the ledger has no record of, a sid with no `host` binding, a
  malformed `host` file, a dead pid, and a live pid whose start time no
  longer matches the recorded one (pid reuse) — each with its own message.
* **Refuses a pid bound under more than one session** — matched on
  `(pid, start time)`, not the pid alone. This is `fleet-bind`'s `/clear`
  decoy closed from the other side: a stale `host` record left behind by a
  failed prune (unwritable tree at bind time) would otherwise pass every
  other guard and TERM the operator's own live session under its dead name.
* **Refuses a self-target**: if the named session's pid is the CALLER's own
  ancestor, refusing before anything is signalled. Without it, a session
  running this on itself would TERM its own host process with leases still
  held — the mid-task self-kill 0012's `host`/`pids` split exists to keep out
  of the *substring* path, reachable here one flag away without this check.
* Shows `sessions/<sid>/contested` (0012) in the pre-action message when
  present — never acts on it, since which of two claimants is real is not
  derivable here; a human decides.
* SIGTERM only, same as the default verb — no `-9`, not behind a flag.
* **Kill first, then emit `session-killed`.** A ledger write failing must
  never become a reason the hung session survives — but "killed and
  unrecorded" is not silent either: it prints loudly on stderr and exits
  **3**, distinct from success (0) and a pre-kill refusal (1).
* **The killer is attributed, never defaulted to `human`.** An agent
  invoking this from a Bash tool call has no `EUNOMIA_*` variables set — the
  launch preamble sets them, Claude Code does not — so this reuses
  `fleetlib`'s ancestry walk on the CALLER's own process, the same one
  `fleet-bind` uses: a Claude ancestor names that session (`session:<sid>`
  when it is itself bound, else `session-pid:<pid>`); no Claude ancestor at
  all is `unattributed` — a positive claim about a *person* the mechanism
  cannot support, not `human`. `--killer <value>` lets a sanctioned consumer
  state its own identity instead, recorded with `killer-basis: explicit`
  rather than `ancestry`.
* Never touches the target's leases — a dead session's lease keeps blocking
  until takeover or release, on purpose (Principle 4); `fleet-claim
  --takeover` is the separate verb for that.
* Same cooperative-strength bound as the default verb: the ownership proof
  here is a ledger record, not a privilege boundary, and a same-user process
  can still lie to `ps`.

Two holes stay open rather than papered over: a `/clear` racing an unwritable
fleet tree can leave the old sid bound live with no ledger trace at all
(`fleet-bind`'s own failure mode — see its section below), and the
self-target check shares the ancestry walk's reparenting blind spot, so
`nohup fleet-pkill --session <own-sid> … &` has no Claude ancestor to catch
it and records `unattributed` instead of refusing.

## fleet-bind

A `SessionStart` hook — fires on `startup|resume|clear|compact|fork` — that
makes the pid behind a session id knowable, so something other than that
session's own shell can act on it (row 19, `fleet-pkill --session`, is the
first reader — plan 0014).

Reads the hook's JSON on stdin for `session_id` and `source`, resolves the
Claude Code process by walking up the hook's OWN ancestry — bounded to 8 hops,
matching the NEAREST ancestor whose executable basename is exactly `claude`
(case-sensitive: the desktop app's own shell process is `Claude`, capital, a
few hops further out, and binding that one would record the wrong pid — see
`fleetlib.nearest_ancestor_pid`) — and writes `sessions/<sid>/host`: a single
file holding that pid and its process start time (`fleetlib._lstart`, the
same start-time identity `fleet-pkill` registers pids under).

**`host/` is deliberately not `pids/`.** `fleet-pkill`'s substring kill path
builds its "provably yours" set from live shell descendants plus
`sessions/<sid>/pids/` — pids a session *registered* while it could still
prove ownership. The session's own Claude process is neither: it is
`fleet-pkill`'s ANCESTOR, never its descendant, and it was never registered.
Writing it into `pids/` would make `fleet-pkill claude` (or `model`, or any
other substring of that long, argument-laden command line) a legitimate
self-kill target from inside the very session it TERMs — mid-task, leases
still held. `host/` answers "what am I"; `pids/` answers "what did I spawn".
Same start-time guard, different directory, because the two questions have
different answers.

**One process serves one session at a time.** `/clear` and interactive
`/resume` both fire `SessionStart` with a NEW session id inside the SAME OS
process, so writing a session's `host` first PRUNES every OTHER
`sessions/*/host` that already names this exact `(pid, start time)` — without
it, the old session id renders as a live pid whose heartbeat has stopped
advancing, which is precisely the shape `fleet-pkill --session` is trying to
detect as hung.

**One session is served by one process at a time.** A binding whose recorded
`(pid, start time)` is still LIVE is never overwritten: a blind overwrite
would let a hung session's `--resume` from a fresh terminal window rebind its
session id to the *new* window's pid, leaving the actually-hung process both
unbound and invisible. The refusal is recorded at `sessions/<sid>/contested`
— the refused `(pid, start time, source)` — so `fleet-pkill --session` (0014)
can show the disagreement next to the heartbeat instead of trusting a
heartbeat that a stuck process's own Stop hook has stopped touching.
`contested` is cleared the moment it stops being true: when the hook writes
that session's `host`, when the recorded pair is no longer live, or — swept
on every invocation, for every session id — when the contested record names
THIS process (the case where the operator `/clear`s inside the very process
that made the refused claim, moving it onto an unrelated session entirely).

Install (opshost, one entry in `~/.claude/settings.json` beside
`fleet-heartbeat`'s `Stop` hook, from the same detached worktree — ADR-0005):

```
{"hooks": {"SessionStart": [{"hooks": [{"type": "command",
  "command": "python3 ~/.local/share/pins/eunomia/bin/fleet-bind"}]}]}}
```

Same silence contract as the heartbeat, one notch stricter: a `SessionStart`
hook's stdout is injected into the session's own context, so nothing is ever
printed there. Failures append to `<fleet>/bind.log`, falling back to a file
under `~/Library/Logs` when the fleet tree itself is the thing that's
unwritable — the one failure a log placed inside that tree could never
record.

## What this row does NOT cover

The deploy wrapper (`service` lease + merged-SHA check) is infra's row 14; hard
budget enforcement stays post-v0 (SPEC §Enforcement). Installing the hook into
each real repo is a one-time human act after merge — never CI's.
