# Launch preamble — every session's first minute

*The copy-paste block a coordinator bakes into a worker's launch prompt, and the
procedure an interactive session follows by hand. This is what establishes the
heartbeat↔holder join `docs/takeover.md` depends on: `EUNOMIA_SESSION` must be
the session's real id, set before any `fleet-*` call. Template, not code — the
enforcement lives in the CLIs (an unactivated lease derives orphaned at the
timeout; a wrong holder is refused at activation).*

For a **launched worker**, the coordinator substitutes `<session-id>` (the
Claude Code session UUID) and `<lease-id>` (from `fleet-claim --assign`) and
prepends this to the launch prompt. For an **interactive session**, run it by
hand with your own session id.

> **Editing this file:** every **untagged** fenced block below is executed as a
> shell script by `tests/test_fleet_cr.py::test_launch_preamble_commands_are_runnable`,
> under `set -euo pipefail`, with `bin/` rewritten to the checkout and
> `<session-id>` / `<lease-id>` substituted. Anything that is not meant to run —
> sample output, a path variant — needs an info string on the fence (```` ```sh ````,
> ```` ```text ````). A block that cannot run in CI turns main red on both lanes,
> which is what #129 did.

```
export EUNOMIA_SESSION=<session-id>
bin/fleet-emit session-spawned
bin/fleet-claim --activate <lease-id>
bin/fleet-events --since 2h
mkdir -p "${EUNOMIA_FLEET_DIR:-$HOME/dev/.fleet}/sessions/$EUNOMIA_SESSION"
printf 'starting: <one line on what this session is doing>\n' > "${EUNOMIA_FLEET_DIR:-$HOME/dev/.fleet}/sessions/$EUNOMIA_SESSION/status.md"
```

Line by line:

1. **Identity first.** Every CLI and the Stop-hook heartbeat key on
   `EUNOMIA_SESSION`. Set it before anything else or the heartbeat lands in a
   different `sessions/` directory than the lease's holder — the silent
   false-orphan `takeover.md` warns about.
2. **Announce.** `session-spawned` is how the rest of the fleet learns you
   exist without a transcript sweep.
3. **Claim.** Activation is verification, not acquisition: it refuses unless
   the lease's holder is you. A push-model worker activates the id it was
   handed; a puller uses `fleet-claim --next <type>` here instead.
4. **Read the tail before touching shared state.** What did the fleet do in
   the last two hours? An open PR, a fresh takeover, a CR against a file you
   were about to edit — cheaper to read now than to discover mid-collision.
5. **Write `status.md` before long operations.** It is the first thing a
   successor reads if this session dies (`takeover.md` §2). One line, updated
   at each phase change, is enough.

**Planning sessions only, and again on any long run:**

```sh
bin/fleet-events --type capability-published --since 24h
```

A skill, script, policy tag or repo that did not exist when you started
(plan 0025). Skills already reach a running session on their own — the harness
re-scans `~/.claude/skills/` — but nothing else does, and a session that has
been open for hours is the one most likely to act on a stale picture of the
estate. It is **awareness, not an instruction**: reading it never obliges you to
reload, abort or re-plan (Principle 4). Coding sessions are deliberately
excluded — a per-feature orchestrator reads state at spawn (plan 0004), so the
read could only ever be a no-op there.

Before editing any shared file, check `docs/change-requests.md` — outside your
lane, you file a CR, not an edit. (When infra's `controls.yml` ships — roadmap
row 11 — reading it for the repos you touch joins this list as a numbered step;
the plan names it now so the doc and the plan converge there rather than drift.)

## The self-starting variant (a hand-launched session on a branch)

The first block in this file is the **push model**: a coordinator has already run
`fleet-claim --assign` and bakes the lease id into the launch prompt. A session
started by hand in a worktree has no coordinator, and until it claims something
the fleet cannot answer *which session owns this branch*.

Measured 2026-09-07: 20 worktrees, 40 live sessions, and `fleet-status` reporting
**no leases at all**. A red build on one branch could not be handed to its owner
because nothing mapped the branch to a session; the note went on the pull request
instead. That is the gap this variant closes.

```
export EUNOMIA_SESSION="${CLAUDE_CODE_SESSION_ID:?run this inside a Claude Code session}"
LEASE=$(bin/fleet-claim --assign \
  '{"type":"branch","repo":"operator/<repo>","branch":"<branch>"}' \
  --holder "$EUNOMIA_SESSION" --ttl 240 --note "<one line on what this session is doing>" | tail -1)
bin/fleet-claim --activate "$LEASE"
bin/fleet-emit session-spawned
bin/fleet-events --since 2h
echo "lease: $LEASE"          # note it down; the release below runs in a fresh shell
```

and when the work is done, or the session ends — a fresh shell, so it re-exports
the identity rather than assuming the block above ran in this one:

```
export EUNOMIA_SESSION="${CLAUDE_CODE_SESSION_ID:?run this inside a Claude Code session}"
bin/fleet-release <lease-id>
```

Three things about it, each verified rather than assumed:

1. **`CLAUDE_CODE_SESSION_ID` is the session UUID.** It equals the basename of the
   session's own transcript under `~/.claude/projects/`. So the holder recorded in
   the lease is an id that leads back to a findable session, which a generated
   name would not be. The `:?` is deliberate: outside a Claude Code session the
   variable is unset, and a named refusal beats `EUNOMIA_SESSION=""`, which
   `fleet-claim` rejects later with "no session identity" (r7).
2. **`bin/…` here, an absolute path from elsewhere.** Sessions working in other
   repositories need this block too, and `fleet-claim` resolves its own imports
   from `__file__`, so invoking it by absolute path from any working directory
   works. From another repo, prefix each command:

   ```sh
   ~/.local/share/pins/eunomia/bin/fleet-claim --assign ...
   ```
3. **`--ttl 240` is stated rather than defaulted.** A lease outliving its session
   is worse than no lease: it names an owner who has gone.

**What happens when someone is already there — read this before relying on it.**
`--assign` refuses. A lease is one per resource (`bin/fleet-claim`), so the second
session's paste does not produce a second row; it stops, with the id of the lease
that already holds the branch:

```text
fleet-claim: branch--collide--001 already holds this resource (holder='sessA',
state=assigned). A lease is one per resource. Coordinate with that session, wait
for it to release, or — if its holder is dead — `fleet-claim --takeover
branch--collide--001` (docs/takeover.md). Never hand-delete a lease file.
```

Three exceptions, all deliberate, and worth knowing before you reach for
`--takeover`:

- **Your own re-run is idempotent.** Same holder, same resource hands back the
  existing lease id and exits 0. Pasting the block twice is safe.
- **An orphaned lease does not block.** Orphanhood is re-derived from heartbeats
  rather than stored, so a session that died without releasing does not wedge its
  branch — this is what lets plan dispatch assign a successor before retiring the
  dead lease.
- **An unheld offer does not block.** A lease with no holder is an offer, not a
  holding.

So the lease *is* a mutex against a live holder, and the honest failure is at
`--assign`: `LEASE` never gets set, and the `--activate` line below it fails too.
If you meant to take over from a session that is genuinely gone, `--takeover` is
the only path that changes a set holder, and it re-derives orphanhood first.
