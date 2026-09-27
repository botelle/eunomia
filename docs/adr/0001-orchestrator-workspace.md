# ADR-0001 — One git worktree per lease: where an orchestrator is allowed to write

- **Status:** Accepted
- **Date:** 2026-09-02
- **Deciders:** the operator (owner/merger)
- **Relates:** `plans/0004-plan-dispatcher.md` (the orchestrator spec, which already
  says the implementer runs "in a fresh worktree with `acceptEdits` confined to it" —
  this ADR fixes *where* that worktree lives, what it is keyed on, and who removes it).
  **[ADR-0002](0002-candidate-green.md)** reuses the same mechanism for the merge
  candidate.

## Context

`bin/fleet-watch` spawns one orchestrator per ready plan:

```python
# bin/fleet-watch:776
subprocess.Popen(
    [orch, repo, plan_path, lease_id],
    env=dict(_child_env(), EUNOMIA_SESSION=sid, ...),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True)
```

There is no `cwd=`. Every orchestrator inherits the watcher's working directory.

That would be harmless if dispatch were serialised per repository. It is not. The
lease resource is **per branch**:

```python
# bin/fleet-watch:736
resource = json.dumps({"type": "branch", "repo": repo, "branch": branch})
```

and the pre-dispatch collision check refuses only when two plan ids normalise to the
*same* slug — the r4-low case of "distinct ids normalising to one slug". Two
different plans in one repository produce two different branches, take two different
leases, and both dispatch, up to `FLEET_WATCH_CAP`. The lease protects the **branch
name**; nothing protects the **checkout**.

So the failure is not exotic. It is the second plan in any repo: two agent sessions
editing one tree, each committing the other's half-written files to its own branch.

**This is a design decision, not a live defect** — and for a stronger reason than
an earlier draft of this ADR gave. That draft said every dispatch had taken the
spawn handler's `OSError` path, releasing a lease and emitting `plan-failed`. That
is wrong, and it describes behaviour `fleet-watch` was explicitly changed to stop:

```python
# bin/fleet-watch:1188 — the r5 M2 gate
orch = os.environ.get("FLEET_ORCHESTRATOR", str(BIN / "orchestrator"))
if not os.access(orch, os.X_OK):
    warn(f"orchestrator {orch} is not executable — refusing to arm ...")
    return 0
```

The gate returns **before the repo scan**, so with `bin/orchestrator` absent no
lease is assigned, no `plan-failed` is emitted, and the ledger contains no trace of
any of it. The conclusion survives — no implementer has ever run — but the
mechanism is that the watcher never armed, not that dispatch failed repeatedly.
Anyone reconciling "ignition never ran" against zero `plan-failed` events would
otherwise conclude the evidence was invented.

That gate also constrains this ADR's implementation, which is why it is quoted
here. `os.access` is satisfied by a file that merely *exists*, so a part-built
orchestrator would re-arm the watcher and produce exactly the storm the gate
prevents — a lease assigned, failed and released, with a page, once per plan per
cycle. Readiness must therefore be **asked** (`orchestrator --ready`), not inferred
from the file being present.

## Decision

**One `git worktree` per lease, rooted outside the repo, removed when the lease
retires.**

1. **Layout.** `$FLEET_WORK_ROOT/<repo-slug>/<lease-id>/`, default
   `~/dev/.fleet/work/` — **inside the documented runtime root**, not a second
   state tree beside it. Keyed on `lease_id`: already passed to the orchestrator as
   `argv[3]`, and already the thing whose lifetime bounds the run. Not keyed on the
   branch name — slug collisions are precisely the case the watcher's r4-low check
   exists to catch, and a workspace key that can collide reintroduces the bug one
   layer down.

   **The key is unique by ledger, not by construction.** `fleet-claim._next_id`
   mints the lowest unused integer among *existing* lease files, so clearing or
   restoring `~/dev/.fleet/leases/` — declared-disposable runtime state — can mint
   an id that a surviving worktree already holds. Putting the work root inside the
   same tree is what makes that safe: the two are cleared together or not at all.
   A worktree found for a lease id whose record does not match is treated as an
   orphan, never adopted.

2. **Backing clone.** One clone per repo under `$FLEET_WORK_ROOT/<repo-slug>/.git-store`,
   with each lease's worktree attached to it. Worktrees share the object store, so the
   Nth concurrent feature costs a working tree, not another full history.

3. **Confinement.** The orchestrator passes its worktree path as the only writable
   root for `acceptEdits`. The wrapper — not the prompt — sets `cwd` on the
   implementer session, per plan 0004's rule that guarantees live in code.

4. **Cleanup on the orchestrator's own controlled exit** — explicitly **not** in
   `_retire()`. That distinction is load-bearing and an earlier draft got it
   backwards. `_retire`'s call sites include the orphan sweep
   (`bin/fleet-watch:1137`), which retires the lease of an orchestrator that
   *died*; wiring removal into that path would delete the tree in exactly the case
   the tree is the only remaining evidence of how it died.

   So: the wrapper removes its own worktree when it exits under its own control,
   success or handled failure. An uncontrolled death (SIGKILL, power) skips that
   and leaves the tree standing, which is the case the sweep exists for.

   A worktree keyed on a lease that is gone or released is an **orphan**: reported,
   counted, and never silently reused. Keys that are not lease-shaped belong to
   short-lived callers that clean up their own (`fleet-candidate`'s
   `cand-<pr>-<sha>`) and are not reported — otherwise every candidate build in
   flight would show up as an orphan.

   `git worktree remove --force` discards uncommitted state without a record. That
   is correct for a tree this wrapper created and is finished with, and wrong for
   anything else, which is the second reason removal is not wired into a generic
   retire path.

5. **Fetch serialisation.** Concurrent `git fetch` into one shared object store can
   collide on `refs/` lock files. Fetches take a per-repo `flock` on the backing
   clone. Note this is a **blocking** `LOCK_EX`, where `_single_instance()` uses a
   *non-blocking* one: the watcher wants a second instance to die immediately,
   while a second fetch wants to wait its turn. Same primitive, opposite mode, and
   the difference is the point.

6. **Readiness is asked, not inferred.** `orchestrator --ready` exits non-zero
   while any phase of the loop is unwired, and the watcher's arm gate consults it
   alongside `os.access`. Failing closed is the cheap direction: refusing costs one
   page total, arming a half-built wrapper costs a page per plan per cycle. An
   orchestrator that does not understand `--ready` predates the contract and is
   therefore not ready either.

## Consequences

- Concurrency survives. The cap stays "how many branch leases are live," which is
  what plan 0004's rollout step 3 assumes.
- Disk cost is one working tree per in-flight feature, not one history per feature.
- A crashed orchestrator leaves a worktree behind. That is **wanted**: it is
  inspectable evidence, and the lease it is keyed to is already the fleet's signal
  for "this run died." Orphan sweep reports rather than deletes on sight.
- Row 6's pre-push hook still enforces the lane: each worktree pushes one branch, and
  that branch is the leased resource.
- The backing clone is a new thing to keep healthy. A corrupt object store takes out
  every in-flight feature for that repo, where separate clones would take out one.
  Accepted: the failure is loud and the fix is `rm -rf` plus a re-clone, and the
  alternative pays a full history per feature to avoid a fault we have not seen.

## Alternatives considered

**One orchestrator per repository (serialise dispatch).** Simplest — make the lease
resource `{type: repo}` and the collision disappears. Rejected: it throws away the
concurrency the branch-lease design was built for, and it would make two independent
features in one repo queue behind each other for no reason other than a shared
directory.

**A full clone per lease.** Complete isolation, no shared object store, no fetch
lock. Rejected on cost, not on correctness — it is the fallback if the shared store
proves fragile.

**Fixed per-repo directories (`work/<repo>/`).** Only correct while dispatch is
serialised per repo, which is exactly what it is not.

## Definition of done

- Two ready plans in one repo at cap 2 → two orchestrators, two worktrees, two
  distinct paths; neither session's `cwd` is the watcher's.
- The orchestrator's own controlled exit removes its worktree; the backing
  clone survives. `_retire()` removes nothing — the sweep retires the lease of
  a run that *died*, and that is the case whose tree is the evidence.
- A worktree whose lease is absent is reported by the sweep and not reused.
- Two concurrent fetches on one repo do not fail on a ref lock.
- Killing an orchestrator leaves the worktree in place and the lease derivable as
  orphaned; a normal exit removes it.
- A candidate worktree in flight is not reported as an orphan.
- With `bin/orchestrator` present but reporting not-ready, three cycles produce
  **one** page, zero leases, and zero Forgejo calls.
- With the phase list emptied, `--ready` exits 0 and the watcher arms.
