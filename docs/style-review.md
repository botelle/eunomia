# The style review

*Plan 0150, [ADR-0025](adr/0025-a-style-only-change-may-take-a-style-review-inside-declared-paths-with-screenshots.md).
This is what the evidence producer (plan 0151) and talos's dispatcher (talos plan 0031)
build against.*

A style-only change may take a lighter review, but only inside paths eunomia declares
for the repository, and only with screenshots. One function decides, `bin/fleet-style`,
and lint, the orchestrator and talos's dispatcher all call it. Until a repository is
declared, every review is routed exactly as it is today.

## The declaration

`config/style-review.json` changes only by a reviewed eunomia pull request, and ships
empty: `{"v": 1, "repos": {}}`. An entry is keyed by `owner/repo`. There is no `default`
entry, and one is refused.

| field | meaning |
|---|---|
| `paths` | non-empty list of patterns for **text** files |
| `binary_paths` | optional list of patterns where binary files (images, fonts, asset-catalog images) may change |
| `evidence_dir` | repo-relative directory, `docs/screenshots` when absent |
| `screenshot.platform` | `ios-simulator` (the only value) |
| `screenshot.command` | repo-relative path of the command the producer runs |
| `screenshot.runtime` | a literal runtime identifier, e.g. `com.apple.CoreSimulator.SimRuntime.iOS-26-5` |
| `screenshot.device` | the device name |
| `screenshot.scale` | integer 1 to 3, the device's pixels per point |
| `screenshot.themes`, `screenshot.appearances` | non-empty lists of names matching `[a-z0-9][a-z0-9-]*` |
| `screenshot.max_image_kb` | integer 50 to 1000, 300 when absent |
| `screenshot.timeout_minutes` | integer 1 to 60 |

Declaring a pattern asserts that every file under it holds only visual values. A refused
value makes the file **malformed**, not undeclared: `fleet-style declared` exits 2 and
names the line and the rule, and `route` answers `code_reviewer`.

### Matching

Matching is written for this file and is **not `fnmatch`**, whose `*` crosses `/`:
`ios/Theme/*` would match `ios/Theme/Sub/Session.swift`, and a pattern meant for one
directory would cover its whole subtree.

- A pattern is split on `/`. `**` is zero or more whole segments. `*` and `?` work
  within one segment. There are no character classes. Matching is case-sensitive.
- A path that is absolute, holds a `..` or `.` segment, or has an empty segment
  matches nothing.

### What a declaration is refused for

Checked at every read. Widening this list is a decision, not a slip.

- A pattern that is empty, absolute, holds `..`, starts with `**`, is `*` alone, or
  uses `**` inside a segment.
- A pattern that matches a probe: a `tests`, `Tests` or `*Tests` directory;
  `*.strings`, `*.stringsdict`, `*.xcstrings`; `plans/x.md`, `docs/x.md`,
  `.forgejo/workflows/x.yml`; `.gitignore`, `project.yml`, `*.pbxproj`;
  `screenshot.command` itself; anything under `evidence_dir`. A pattern whose own
  segments or last segment name one of these is refused too.
- In `paths`, a pattern for a binary type (`.png .jpg .pdf .ttf .otf .car`, or anything
  under `*.xcassets/` other than `Contents.json`). A binary can only be declared under
  `binary_paths`, which is the riskier kind (ADR-0025 §2, §9).
- An `evidence_dir` that is absolute, holds `..`, or lies under a pattern.

## Routing

`fleet-style route <repo> --pr N --plan-file F|none --changes F --git-dir D --base SHA
--head SHA --override full|none --json` prints
`{"position", "review", "reasons", "evidence_id", "evidence_folder", "screenshot"}`.
`--changes` is the forge's changed-file list as JSON: `filename`, `status`, and
`previous_filename` for a rename. The caller supplies the plan text, because talos reads
it from the **base** branch (the head's copy is author-writable); route reads
`review:` from nothing else.

`position` is `style_reviewer` only when all of these hold. Otherwise it is
`code_reviewer`, and `reasons` names each failed condition in this order:

1. **Opt-in.** With a plan file, the plan says `review: style` and its `repo:` is
   `<repo>`. With `none`, the pull request has no plan and is routed by its diff alone.
   The evidence id is the plan id, or `pr<n>`.
2. `<repo>` is declared and its file is well formed.
3. `--override` is `none`. The caller passes `full` for an owner-account `Review: full`
   line. An override can only ever tighten.
4. Once the plan's own file is dropped, every change, a rename's old path included, is a
   text file matching `paths` (a file `git diff --numstat` reports as binary must match
   `binary_paths` instead), or a file **added** under one folder of the evidence dir.
5. That one folder is new at the base, named `<YYYY-MM-DD>-<id>` with a real calendar
   date, and holds only `README.md` at its root and
   `<theme>-<appearance>/<screen>-before.png` / `-after.png` in declared names. No change
   under `evidence_dir` has any status other than `added`.
6. `fleet-models reviewers <repo> style_reviewer` resolves as many ordinals as
   `code_reviewer`.

Exit 0 means the routing was decided. Any failure to decide is a non-zero exit, and the
caller treats that as `code_reviewer`.

## The evidence folder

```
<evidence_dir>/<YYYY-MM-DD>-<plan-id or prN>/README.md
<evidence_dir>/<YYYY-MM-DD>-<plan-id or prN>/<theme>-<appearance>/<screen>-before.png
<evidence_dir>/<YYYY-MM-DD>-<plan-id or prN>/<theme>-<appearance>/<screen>-after.png
```

The date is the day the images were taken. A dated folder on the default branch is never
modified or deleted, and a re-shoot is a new folder, so the folders form a visual history.
The producer commits the images in their own commit, after the code. The author never does.

### The folder's `README.md`

1. A before | after table with one row per screen × theme × appearance, as relative image
   links, readable in the forge's file view.
2. The image count and the folder's total size.
3. One fenced `json` block, the evidence block, with these keys:

| key | value |
|---|---|
| `v` | `1` |
| `id` | the plan id, or `pr<n>` |
| `pr` | the pull request number |
| `taken` | ISO-8601 UTC time; its date is the folder's date |
| `base` | the base commit (full hash) |
| `code_head` | the commit the images show, the evidence commit's parent (full hash) |
| `platform`, `runtime`, `device` | as declared |
| `command` | the declared command path |
| `command_blob` | that command's blob hash at `base` |
| `images` | a list of `{path, bytes, sha256}`, `path` being repo-relative |

### The producer's record

The same `images` list, plus `repo`, `pr` and `evidence_commit` (the evidence commit's
hash), as JSON at `<fleet dir>/style-evidence/<owner>/<repo>/<n>/<evidence-sha>.json`,
outside every repository.

### The PR-body grid

The same table between `<!-- style-evidence:begin -->` and `<!-- style-evidence:end -->`.
Its images are raw URLs pinned to the evidence commit,
`<forge>/<owner>/<repo>/raw/commit/<evidence-sha>/<path>`, because the branch is deleted
on merge.

## Verifying the evidence

`fleet-style verify <repo> --pr N --plan-id ID|none --git-dir D --base SHA --head SHA
--json` prints `{"ok", "folder", "code_head", "problems", "images", "total_bytes"}`. Exit 0
means it decided, whether `ok` is true or not. `ok` is true only when all of these hold,
read from git objects and never from a working tree:

- the head commit changes only files it adds inside the one evidence folder, and nothing in
  that folder exists at `<head>^1`;
- the README block names `<head>^1` as `code_head`, names the base, the declared `runtime`
  and `device`, and its date equals the folder's;
- every declared theme × appearance holds `before` and `after` for the same non-empty set of
  screens;
- every image decodes as PNG (signature, IHDR, every chunk's CRC, IDAT inflating to the size
  IHDR implies), carries no chunk outside `IHDR PLTE IDAT IEND tRNS sRGB gAMA cHRM`, and is
  within `max_image_kb`. Checked with `zlib` and `struct`, so the check means the same thing
  on every host;
- every image's SHA-256 is in the README block and in the producer's record, and the two lists
  equal the committed images.

With no valid evidence there is no style review, and the normal review is not a fallback
(ADR-0025 §7).
