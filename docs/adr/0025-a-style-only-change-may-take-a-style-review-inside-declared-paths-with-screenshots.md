# ADR-0025 — A style-only change may take a style review, but only inside paths eunomia declares for it, and only with screenshots

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md) §1 and
  §6 (a `src` pull request may route to `style_reviewer`, and its screenshot folder
  does not count as a second kind); [ADR-0009](0009-a-lane-may-have-a-supervisor.md)
  §1 (the positions table gains style review); [ADR-0011](0011-a-runner-has-a-provenance-class.md)
  §2 (`style_reviewer` is `us-hosted` only).
- **Relates:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md)
  §1 and §3–§5 and [ADR-0024](0024-no-review-requires-a-named-model.md) (every
  repository is tested, reviewers are configuration, and accounts name ordinals);
  [ADR-0022](0022-a-contract-entry-binds-only-where-ci-runs-it.md) (how the
  cross-repo calls below are evidenced); plans 0150 and 0151; talos plan 0031;
  `PROVENANCE.md` §3 in each product repository.

## Context

A re-skin changes colour, type and spacing values. Today it is reviewed like any
other `src` change: a code reviewer with a shell reads hex values it cannot see.
A screenshot shows the change better, to the reviewer and to the owner, and it
stays as a record of what the product looked like.

A lighter review is safe only when the change under review cannot be anything
but style. Declaring that a change is "style" proves nothing; checking which
files the diff touches does.

## Decision

**1. A plan may ask for the style review with `review: style`.** The field is a
closed set, `full` or `style`, and absent means `full`. `fleet-plan lint`
refuses any other value. It also refuses a `review: style` plan when its
repository declares no style paths, or when any `paths` entry is outside them,
other than the plan's own file and its evidence folder (§6). The field is not
called `lane:`, because "lane" already names the test lane and the path lease.
`tier:` does not gate the style review: style is style, whatever tier the plan
carries.

**2. Which paths count as style is eunomia configuration, per repository, and
nothing else.** It lives in `config/style-review.json` and changes only by a
reviewed eunomia pull request. A repository with no entry cannot use the style
review. It is not declared in the product repository, because a style pull
request there could then widen its own declaration.

An entry names path patterns, a screenshot command and the evidence directory
(§6). Declaring a pattern asserts that every file under it holds only visual
values: colour, type, spacing, shape. A path holding server code, tests or
their assertions, copy or strings, accessibility identifiers, or behaviour is
not declarable.

Declared style paths cover **text files only** by default. A binary file, such
as an image, a font or an asset-catalog image, is never style-routable unless
the entry declares its pattern separately as a binary style path. That is
riskier than text, for the reason in §9.

**3. The diff decides, mechanically, before any model runs.** A pull request
goes to `style_reviewer` only when all of these hold:

- it opted in. A pull request whose `Plan:` marker names a plan opts in only
  when that plan's front matter **on the base branch** says `review: style`
  (the head's copy is author-writable). A pull request with no plan is routed
  by its diff alone. The owner ruled on 2026-10-22 that hand-made pull requests
  follow the same path as the repository's planned ones;
- every changed path, including a rename's old path and a deletion, matches a
  declared text pattern case-sensitively, or matches a declared binary pattern.
  The plan's own file is dropped first, as ADR-0014 §1 drops it. The one
  exception is the evidence folder (§6). The pull request may add files under
  exactly one new folder for this plan or pull request, and only `.png` images
  and that folder's `README.md`. The folder must not exist at the base, and the
  pull request may not modify or delete anything in an existing dated folder;
- no owner-account comment or review on the pull request carries the line
  `Review: full`. That is how the owner forces the full review from a session.
  The forge records who wrote a comment, so the dispatcher can check the author
  instead of trusting author-writable text. Such a line can only make review
  stricter;
- `style_reviewer` resolves as many ordinals for the repository as
  `code_reviewer` does.

If any of these fails, the pull request gets the normal review from
`code_reviewer`, with a note saying which condition failed. A failed check
never refuses the pull request, and it never gets the lighter review. One
eunomia function makes this decision, and lint, the orchestrator and talos's
dispatcher all call it, so the three cannot disagree. **This check is what makes
a lighter review safe.** The style reviewer reads a change already shown to
touch only files declared to hold visual values. The owner may later retire
hand-made pull requests altogether. This ADR does not decide that.

**4. `style_reviewer` is a reviewer position under ADR-0013 §3–§5.** It takes
ordinals 1..n of pinned, hosted runners, and only `us-hosted` ones. `us-open` and
`cn-open` runners never review. Its `'default'` row and every repository row are
owner configuration in `bin/fleet-models`, and this ADR names no runner. Its
ordinals post as the same accounts as every reviewer position: ordinal 1 as
`revbot`, ordinal 2 as `rev2bot` (ADR-0024 §1). Branch protection, the number
of approvals and ADR-0024 §3 are unchanged.

**5. The review is lighter in cost only.** The style reviewer is dispatched
without shell or write tools. It reads only the evidence folder and reviews
against a rubric for visual change. The review bar, the CI gate, what a HIGH
means and the approvals required are the same as for any review. A hunk that is
not a visual value is a HIGH. That is the reviewer's check behind the path
check, because a declared file written in a programming language can still hold
code.

**6. The evidence is screenshots committed to the product repository by the
fleet's producer, never by the author.** The producer runs the repository's
declared command, read from the base commit, once at the base and once at the
head. The orchestrator runs it for planned work, and a session runs the same
tool by hand for a pull request with no plan. It runs on a simulator with an
explicitly pinned OS runtime and device, for every theme and appearance the
entry declares.

The images go in their own commit, after the code, under
`<evidence_dir>/<YYYY-MM-DD>-<plan-id or prN>/<theme>-<appearance>/<screen>-before.png`
and `-after.png`. `evidence_dir` is declared with the style paths and defaults
to `docs/screenshots`. The date is the day the images were taken. The folder's
`README.md` holds the before/after table as relative image links, and an
evidence block naming the base commit, the code commit the images show (the
evidence commit's parent), the runtime, the device, and each image's SHA-256.
The producer also keeps its own record of those hashes outside the repository.

A dated folder on the default branch is never modified or deleted. A later
re-shoot is a new folder, so the folders form a visual history. Images are at
point resolution, carry no metadata, and stay under the declared size cap. The
pull request body shows the same grid through raw URLs pinned to the evidence
commit, because the branch is deleted on merge.

*Why in the repository (the owner's choice, 2026-10-22).* Committed images
grow every clone's history, and every re-skin adds more. In exchange, the trail
is in every clone and survives the loss of the forge or its backups, which pull
request attachments would not. The size rules bound the growth but do not
remove it.

**7. Screenshots are required. With no valid evidence there is no style
review.** The dispatcher posts `REQUEST_CHANGES` naming what is wrong, and runs
no model, in any of these cases:

- the evidence folder is missing;
- the head commit changes anything outside the folder;
- anything in the folder existed before the head commit;
- the folder's `README.md` does not name the head commit's parent as the code
  it shows;
- a declared combination lacks a before or an after image;
- an image does not decode as PNG, carries metadata, or exceeds the cap;
- an image's SHA-256 is missing from the evidence block or from the producer's
  record;
- the `README.md` names a different runtime or device.

The normal review is not a fallback here, because the record is the reason the
lane exists. This differs from §3, where the fallback is always the full review.

**8. A style plan keeps its test lane (ADR-0013 §1).** A test-lane finding on a
style-routed pull request is triaged first as a problem with the tests: a test
that over-specifies presentation. It is not first read as a defect in the style
change. The finding still blocks like any finding until it is triaged.

**9. A style pull request must not become a covert channel.** A lighter review
means less human reading, and images are where data hides unseen
(steganography). Two guards address this:

- Binary files are outside the style paths unless a repository declares them,
  and that declaration is the riskier kind (§2). A reviewer can read every
  character of a text diff, but no reviewer can see everything a changed image
  or font carries.
- Evidence images come only from the producer's run. Every committed PNG must
  appear by SHA-256 in the evidence block and in the producer's own record, and
  it must be added by the evidence commit itself. An author therefore cannot
  slip an image into the folder.

**10. `tier:` still does not set review depth.** `docs/feature-plans.md`
describes `tier:` as the PROVENANCE risk tier that drives review depth.
`bin/orchestrator` deliberately does not consult it (`route_implementer`), and
neither does the review dispatcher. This ADR does not change that, and it adds
no tier rule of its own.

## Amendments

**ADR-0014 §1.** On a style route the classifier drops the evidence folder
(§6) before deciding the kind, as it drops the plan's own file.

**ADR-0014 §6.** `src` → `code_reviewer`, or `style_reviewer` when ADR-0025 §3
holds.

**ADR-0009 §1.** The positions table gains **style-PR review**, agentic.

**ADR-0011 §2.** `style_reviewer` is held by `us-hosted` runners only.

## Consequences

- **Landing order.** Eunomia plan 0150 lands first. It brings the declaration
  file, the routing and evidence checks, lint, the position and the
  `repo_model` table rebuild. Then plan 0151 (the evidence producer) and talos
  plan 0031 (the dispatcher, the reviewer-uid wrapper and the contract
  entries). After those merge, the owner reinstalls the wrapper, sets
  `style_reviewer` rows, and declares a repository in a reviewed eunomia pull
  request. Until a repository is declared, every review is routed exactly as it
  is today.
- **speakhush is not declarable yet.** `ios/SpeakHush/Theme/AppTheme.swift`
  holds copy (room taglines, empty-state prompts) and the room-to-mode mapping
  (`mode: .privateMode`, `ritual: .burn`), which speakhush's PROVENANCE §3 puts
  at tier 3. speakhush plan 0051 moves those out of `Theme/`. Its theme
  directory can be declared only after that plan has merged.
