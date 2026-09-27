# Change-requests — how shared files stop being fought over

*The 2026-08-17 postmortem's class: two parallel lanes silently editing the same
`project.yml`, last-writer-wins. The CR queue replaces that with one serial
applier per repo and a written request from everyone else.*

## When to file vs. when to edit

Edit directly when the file is inside your lane (your lease's branch or globs).
**File a CR when the file belongs to another lane or to the integrator** —
`project.yml`, lockfiles, `OWNERSHIP.yml`, shared config. The test: if another
session could legitimately be editing it this hour, it is not yours to edit.

```
bin/fleet-cr file --repo operator/sniff --lane w1 --target project.yml \
  --intent "add SniffTests target with GENERATE_INFOPLIST_FILE"
```

`--intent` (prose) or `--patch-file` (unified diff), at least one. **Nothing
secret-shaped rides either field** — the cr/ tree is rendered by atlas
(Principle 6); `validate_cr` refuses token shapes from the shared
`config/secret-patterns.conf` at file time and again at apply time. Reference a
keyvault kv path instead of a value, always.

## The integrator contract

Anyone may `file` and `list`. Only the **integrator** — the session holding an
active `branch`/`paths` lease on the repo whose resource carries
`"role": "integrator"` — may `apply` or `reject`. The role field is the
discriminator: a worker lane is *also* a branch lease on the repo, so repo-match
alone would let any filer self-apply. The coordinator grants one
integrator-role lease per repo (`--assign` refuses a second while one is
assigned or active). Like the rest of the ledger, this gate is cooperative — it
stops accidents and forgetfulness, not a session determined to grant itself the
role (Principle 5; the review gates are the adversarial layer). If the integrator
session dies, its lease still holds the gate AND blocks a replacement grant —
that is the designed handover: a successor runs `fleet-claim --takeover` on it
(`docs/takeover.md`), never hand-deletes the lease file:

```
bin/fleet-claim --assign '{"type":"branch","repo":"operator/sniff","branch":"integration","role":"integrator"}' --holder <integrator-session>
```

Then:

```
bin/fleet-cr apply operator/sniff CR-w1-007 --commit a1b2c3d
```

`apply` marks the CR applied and records the commit; **it never makes the
commit**. The actual edit lands in the integrator's own PR, in queue order —
the ledger observes, Forgejo state stays the authority (Principle 3). A CR that
is already `applied` or `rejected` refuses further transitions.

`reject` is the human verb for a stale filed CR (there is no auto-expiry and no
timer — Principle 4). The closed event set has no `cr-rejected`, so the record's
`state` field is where a rejection is recorded.

## `needs:` — a condition, not an action

A PR body line `needs: CR-w1-007` makes the CR a **merge-queue condition** (row
7, minos): the queue merges only when the CR's `state=applied` **and** its
`applied_commit` is reachable from the target repo's main — checked via the
Forgejo API, not the event log (Principle 3). Filing or applying a CR never
merges anything, and nothing in `fleet-cr` reads `needs:` lines.

## Reading the queue

```
bin/fleet-cr list --state filed
```

`list` takes no lock (readers never lock) and degrades one corrupt file to a
stderr WARN, never the whole listing.

## If a CR file is poisoned

A CR hand-edited to contain a secret refuses **every** transition — apply and
reject both re-validate — so the tool will not touch it. That is deliberate:
the on-disk file is already the leak. The remedy is manual and immediate:
delete the file (`rm <fleet>/cr/<owner>/<name>/<lane>-<nnn>.json`) — its number
is never reused, so a PR's `needs:` line can never be satisfied by a later,
different request — then treat
the value as exposed and rotate it — the cr/ tree is rendered by atlas, and
fleet-leak-watch pages on the shapes it knows.

Never-reuse survives this `rm`, and a crash at any point during a filing,
because allocation is **reservation-first**: the per-lane counter records N+1
— fsync'd, file and directory — *before* the record for N is written, so by
the time a CR exists on disk its number is already spent. The flip side is
that a lane's sequence may show **gaps**: a filing refused after its number
was reserved (a secret-shaped field, a degraded conf) burns that number. A
gap is harmless; reuse is what the design forbids.

The guarantee rests on one thing: **the per-lane counter file surviving**
(`<fleet>/cr/<owner>/<name>/.<lane>-next`). It is the durable floor; the
directory scan is only a backstop, and the scan cannot see numbers whose
records were deleted. So a torn counter refuses filing loudly with repair
guidance — but a counter *deleted* along with its lane directory (an
`rm -rf` cleanup, a restore that skips dotfiles) reads as a lane's first
filing and re-mints from the scan floor. Back the dotfiles up with the
records, and never hand-delete one to "reset" a lane.
