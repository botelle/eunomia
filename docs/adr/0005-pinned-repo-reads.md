# ADR-0005 — An agent reads a commit, not a directory

- **Status:** Accepted
- **Date:** 2026-09-06; trimmed 2026-09-22 (the evidence moved to
  [measurements](../measurements/adr-0005-pinned-repo-reads.md); the rules,
  their numbers and the normative vector are unchanged)
- **Deciders:** the operator (owner/merger)
- **Relates:** [ADR-0001](0001-orchestrator-workspace.md) gave the orchestrator's
  implementer a worktree per lease. This generalises that to every agent handed
  filesystem access, and adds the distinction ADR-0001 does not draw: between
  reading a tree and being configured by it. [ADR-0003](0003-adr-track.md) for
  the independence this protects.

## Context

`~/dev/<repo>` is a shared checkout on whatever branch the last session left.
ADR-0001 made that safe for writers. Readers had no rule, and on
operator/eunomia#42 two reviews described another session's in-progress branch
as the change under review, filed findings against a file the PR did not touch,
and errored nowhere. The attached context was correct; the reviewer's own tools
opened the door.

Fixing that one tool (talos #2, #4, #12) found the same defect one layer further
in each round: a `cwd` inside the checkout let the author's commit configure the
reviewer; `git worktree add` ran a hook from the branch another session had
checked out; a "no tools" spawn held 64 tools because `--allowed-tools` is an
allowlist, not an availability set. Each was a derived answer standing in for
an authoritative one. The measurements page records every probe; the rules
below are what they forced.

## Decision

**Five rules, for any tool that gives a model filesystem access to a repository
the model did not check out itself** — reviewers, judges, triagers,
implementers, eval and bakeoff harnesses.

### 1. Address content by SHA. Never ask a working tree what the change is.

File bodies come from `git show <sha>:<path>`; the changed-file list comes from
the forge. A branch name moves under a force-push and does not exist for a
fork. If the SHA is not present locally, fetch `refs/pull/<n>/head`; if it still
is not, **refuse**. Reviewing whatever the clone contains is the failure this
ADR is about.

### 2. Every agent with file access gets a private worktree at that SHA.

Detached, rooted outside the repo, created for the run and removed at its end.
Assert the SHA before use and refuse rather than fall back to the shared clone.
Name the path in the prompt absolutely and say not to read `~/dev/`.

Cleanup must survive a kill, and the sweep is for **reader** worktrees only. A
reader's tree is detached and never written, so deleting it loses nothing. A
writer's tree may hold the only copy of uncommitted work and is the evidence of
how its run died; ADR-0001 §4 keeps those reported, never deleted. One helper
may do both, told which kind it holds, and the default when unknown is
ADR-0001's.

### 3. Read access and configuration authority are different grants. `cwd` conflates them.

A **reader** gets an **empty** working directory outside any repository tree
and the checkout via `--add-dir`. It reads the tree; the tree does not configure
it. The flags are rule 5's vector and are not restated here: three rounds of
this document drifted by restating them, and the casualty each time was the
operator's `fleet-secret-guard`.

A **writer** stands in its worktree and is configured by the tree it edits.
That is acceptable only because ADR-0003 holds downstream: the reader that
judges its output does not share that configuration.

Two facts make the reader half harder than it looks. `--add-dir` also loads the
added tree's skills, commands and agents unless project settings are excluded;
under rule 5's `--restricted` they are excluded outright. And `CLAUDE.md` has no
flag: it is excluded only by the empty cwd lying outside every repository
tree, with `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` removed from the
environment. The measurements page has the probe.

### 4. Creating a worktree must not execute the shared clone's hooks.

`core.hooksPath=/dev/null` on **every** git invocation the helper makes, in
`fleetlib._git` itself. Unconditional, because the setting arrives with a
dependency install, not a decision, and `git fetch` fires hooks too. Pointing
at `/dev/null` also replaces the `.git/hooks/*` lookup that lefthook uses, so
one flag closes both directions.

### 5. Availability, permission and scope are different grants. This is the vector.

Four flags, none interchangeable: `--tools` is what a session **holds**;
`--allowed-tools` pre-approves and removes nothing; `--strict-mcp-config` drops
the MCP servers `--tools` does not govern; `--restricted` confines the file
tools to the working directories and removes the code-running tools unless
`--tools` names them.

**The reader spawn vector, normative.** Every other paragraph refers here.

```
cwd                       an empty directory outside any repository tree
--add-dir <worktree>      the checkout, by absolute path
--tools <names>           the availability set — a reader's Bash is a positive
                          grant written down here, or it is absent
--strict-mcp-config       drops the MCP servers, configured in ~/.claude.json
                          and not in any settings file
--restricted              confines file tools to the working directories
--settings <a HOOKS-ONLY JSON document whose hook commands name paths
            OUTSIDE any git checkout — not the operator's settings file>
                          MANDATORY with --restricted; without it the operator's
                          PreToolUse hooks are gone. The string form is
                          required: the operator's file also carries
                          permissions.allow, which under -p is the difference
                          between `ssh` refused and `ssh` executed
--allowed-tools <the names the reader needs>
                          MANDATORY under -p; without it Bash degrades to the
                          read-only-classified subset. It bounds nothing: any
                          Bash prefix that runs code or drives git is full
                          operator capability
env                       CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD removed;
                          it makes --add-dir carry CLAUDE.md and .claude/rules/*
```

Two limits are part of the rule, not footnotes. **`--restricted` requires
`--settings`**: it ignores the settings files where `fleet-secret-guard` is
installed, so alone it removes the fleet's only code-enforced control against
credential reads from the one session that ingests author-controlled text.
**Prefix scoping of `Bash` is real only for prefixes that cannot execute code
or drive git.** `Bash(git *)` pre-approves `git push`; `Bash(python3 *)` is
arbitrary code; `Bash(pytest *)` runs the reviewed tree's own `conftest.py`
inside the reader. Bounding that needs a sandbox, which this ADR does not
specify, so it does not claim the property. The guard that survives through
`--settings` must itself name a path outside any git checkout, or a `git *`
grant can replace it.

The implementation lives in `bin/fleetlib.py`: the hooks pin, the SHA
assertion, the owner-aware sweep, and a spawn helper carrying the vector
verbatim. Tools outside this repo cannot import it; a copy that drifts, a
`~/bin` script, or moving those tools into an importing repo are the options,
and picking one is a plan.

## Consequences

- Every agent-facing tool needs a change. `dispatch-review.py` has rules 1–3
  and most of rule 5; what it lacked on 2026-09-08 was the hooks-only
  `--settings` document, and that is a talos change this ADR obliges first.
  `fleetlib.add_worktree` needs rules 2 and 4; the orchestrator's implementer
  spawn needs rule 3's writer half made explicit.
- A refusal replaces a silent wrong answer in three places: SHA absent,
  worktree not at the SHA, changed-file list unavailable. Each will sometimes
  stop a run that would have produced *something*. That is the trade.
- Rules 1, 3 and 4 hold mechanically, and so do rule 5's `--tools`,
  `--strict-mcp-config` and `--restricted`. Rule 5's `--allowed-tools` bounds
  nothing and exists to make the grant usable. So for `Bash`, and only for
  `Bash`, the prompt clause is an instruction, not a sandbox — the wording
  ADR-0006 quotes, kept so that citation resolves. What limits a reader's Bash
  is the prompt and `fleet-secret-guard`.

## Alternatives considered

**Warn when the shared checkout is on another branch.** On #42 there was
nothing to warn about: the SHAs matched and the reviewer still read another
branch through a channel no check watched. Removing the channel ends it.

**Give readers no tools at all.** Sound, and wrong for review: the strongest
findings in this series came from a reviewer that executed probes — an invalid
flag caught by running `bao <cmd> -h`, a hook probe re-run rather than
believed. Removing tools removes the findings that justify the gate. The grant
is deliberate; its limits are rule 5's.

**One dedicated clone per tool.** Solves only the branch problem: a clone still
has a checked-out branch, runs its own hooks, and configures an agent whose cwd
is inside it.
