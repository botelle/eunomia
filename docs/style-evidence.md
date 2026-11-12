# Style evidence

*Plan 0151, [ADR-0025](adr/0025-a-style-only-change-may-take-a-style-review-inside-declared-paths-with-screenshots.md) §6–§7.
The declaration, the routing and `fleet-style verify` are in [style-review.md](style-review.md);
this is the program that makes what `verify` checks.*

A pull request that `fleet-style route` sends to `style_reviewer` carries its screenshots.
The orchestrator photographs the base and the head with the repository's declared command,
commits the images to the pull request branch in a dated folder, in their own commit after
the code, and writes the before/after grid into the pull request body. The implementer
session never makes or commits them: evidence its author produced is a claim.

Until a repository is declared in `config/style-review.json`, none of this runs.

## Where it runs

`bin/orchestrator` asks, at the start of every review round of the review loop:

1. Does the plan say `review: style`? If not, nothing changes.
2. `fleet-style route` on the pull request's changed files and the plan text it dispatched
   from. The route is asked on the code head, before production: the evidence commit adds
   one new folder, which the route already allows, so it does not change the answer. Any
   failure to decide is `code_reviewer`, and a `code_reviewer` round is exactly the round
   there was before. The `dispatch-review.py` argv is the same either way.
3. `style_reviewer`: `fleet-style verify` at the head. If it is `ok`, the round goes on.
4. Otherwise, if the pull request has an approval at the head, nothing is pushed (the
   freeze, plan 0032). If it has none, the orchestrator runs `produce`, then waits for CI
   on the new head as it does for any push.
5. `expected_reviewers` is asked for the routed position.

A `produce` that exits non-zero is a `Stop` that pages and names the step. No review is
dispatched after it: it would be refused for missing evidence, and a fix round would hand
the implementer a failure it cannot fix.

## `fleet-style-evidence produce`

```
fleet-style-evidence produce <owner/repo> <pr> [--plan <path>] --worktree <dir> --base <sha> --head <sha>
```

Without `--plan` the pull request has no plan and the evidence id is `pr<pr>`.

| step | what it does |
|---|---|
| 1 | Reads the declaration. Refuses an undeclared or malformed repository, any `platform` other than `ios-simulator`, a worktree with uncommitted tracked changes, and a `--head` that is not both the worktree's checked-out commit and the pull request's current head. Refuses a closed pull request, and one with an approval. |
| 2 | Creates a detached worktree at base outside the run's worktree. Reads `screenshot.command` from the **base** blob into a temporary executable outside both trees and records the blob's SHA. |
| 3 | Takes a `sim` lease (reusing one the session already holds, and leaving that one alone), creates a simulator with the declared `device` and `runtime`, boots it through `fleet-sim`, and checks in `simctl list devices -j` that it reports the declared runtime. |
| 4 | Runs the command once in the base tree and once in the head tree. |
| 5 | Resizes each image to point resolution, strips it to the chunks `verify` allows, and refuses it if it is still over `max_image_kb`. |
| 6 | Writes `<evidence_dir>/<YYYY-MM-DD>-<id>/` (UTC date). |
| 7 | Commits only that folder, writes the producer's record, runs `verify`, pushes. |
| 8 | Writes the grid between `<!-- style-evidence:begin -->` and `<!-- style-evidence:end -->` in the body and reads it back. |

Exit 0 only when step 8 verified. Anything else exits 1 and the message begins
`step N:`.

### The command's contract

`screenshot.command` is a repo-relative path. It runs with the tree as its working
directory and exactly these arguments:

```
<command> --out <dir> --udid <udid> --themes <a,b> --appearances <x,y>
```

It must exit 0 and write `<dir>/<theme>/<appearance>/<screen>.png` for every declared theme
and appearance, with `<screen>` matching `[a-z0-9][a-z0-9-]*`. Nothing else counts: other
files are ignored, a symlink is refused, and the set of screens must be the same on both
sides and in every combination. Each run is bounded by `timeout_minutes` and its process
group is killed at the bound. There is no override for the bound.

It runs with `PATH`, `HOME`, locale, `TMPDIR`, `USER`, `SHELL`, `TERM`, `DEVELOPER_DIR` and
`FLEET_SIMCTL` and nothing else: no forge token, no agent socket. It builds code the pull
request's author wrote.

**It is the base commit's copy that runs, for both sides.** A pull request that could change
the program photographing it could photograph anything. A change to the command in the pull
request therefore never takes effect in its own evidence. Plan 0150 also refuses the
command's path as a style pattern; this is the second check.

### The simulator

The run creates its own simulator and never uses one it did not create: two sessions on one
device photograph each other's builds. `runtime` is passed to `simctl create` literally and
quoted, because given no runtime, or an empty unquoted one, `simctl create` exits 0 on the
newest installed OS, which on opshost is a beta. An empty runtime refuses before `simctl` runs,
and a booted device that reports another runtime refuses. The README records the runtime the
device reported, not the one requested. The simulator is booted only through `fleet-sim`, whose
cap protects the build host's memory. In a `finally`, on every exit path, it is shut down and
deleted, and a lease the run took is released.

### Size rules

Images are shrunk to point resolution (pixel size ÷ `scale`) with `sips -Z`, and re-serialised
with only `IHDR PLTE IDAT IEND tRNS sRGB gAMA cHRM`. An image still over `max_image_kb` is
refused, not recompressed until it fits: the owner picks the cap. No video and no GIF is ever
produced.

### The commits

A folder this pull request already carries from an earlier round (named for the same id,
absent at the base) is removed first, in a commit of its own, so the pull request's net
change stays one new folder. Then the images go in the evidence commit, whose parent is
that commit (or the code head, in the first round). Two commits, because `fleet-style
verify` accepts a head commit that only adds files inside one folder, and a replace in a
single commit is a modification whenever the date and id repeat. A folder that exists at
the base is never touched.

The evidence commit's subject is `screenshots: <id> at <head-short>`, and its identity is the
worktree's.

### The hash record

`<fleet dir>/style-evidence/<owner>/<repo>/<pr>/<evidence-sha>.json`, mode 0600, outside every
repository. Only `produce` writes it. It is the anchor ADR-0025 §9 relies on: an image an
author slipped into the folder has no entry in it, and a record kept in the repository or
the pull request would be author-writable, like the README. Its shape is in
[style-review.md](style-review.md#the-producers-record).

### The body grid

Image links are `<forge>/<owner>/<repo>/raw/commit/<evidence-sha>/<path>`, with the forge base
from `FLEET_FORGEJO_URL`. They are pinned to the evidence commit, never the branch, because
the branch is deleted on merge. Only the marked section is replaced (or appended when absent);
the implementer's text is kept. The section ends with the folder's total size.

## What a failure means

| message begins | meaning |
|---|---|
| `step 1` | The declaration, the head, or the pull request is not something to photograph: undeclared, malformed, wrong platform, a stale `--head`, a dirty worktree, an approval (frozen), or a forge read that failed. |
| `step 2` | The base worktree or the base's command could not be set up. |
| `step 3` | No `sim` lease, no simulator, `fleet-sim` refused (a fourth booted simulator, no lease), or the device reports another runtime. |
| `step 4` | The command failed, timed out, wrote no screens, or the base and head disagree on them. The declared command is the owner's to fix. |
| `step 5` | An image is not a PNG, `sips` failed, or an image is over `max_image_kb`. Raise the cap by a reviewed change to the declaration, or shrink the screen. |
| `step 7` | The evidence commit, `fleet-style verify` or the push refused. `verify`'s first problems are in the message. |
| `step 8` | Images are pushed but the body was not written. Run `produce` again by hand: the earlier folder is replaced. |

## Producing evidence by hand

For a hand-made pull request, which ADR-0025 §3 routes by the same rule, on opshost (the host
where iOS builds and simulators belong), from a worktree on the pull request branch with
`EUNOMIA_SESSION` set:

```bash
BASE=$(git merge-base origin/main HEAD)
~/dev/eunomia/bin/fleet-style-evidence produce <owner/repo> <pr> \
    --worktree "$PWD" --base "$BASE" --head "$(git rev-parse HEAD)"
```

Add `--plan plans/<id>.md` when the pull request has a plan. Run it only before the pull
request has an approval; once it has one, `produce` refuses.
