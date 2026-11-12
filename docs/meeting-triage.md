# fleet-triage — a meeting failure is triaged by a step that can read the code

*Plan `plans/0103-a-meeting-failure-is-triaged-by-a-code-aware-step.md`.* Every
test that did not pass when a lane's suite met an implementation (`fleet-meet`,
plan 0102) gets a classification from a runner that can read the
implementation. The wrapper checks each classification's evidence against the
files, and the result is posted once, as one comment on the implementation pull
request. This is what ADR-0006 §8 asks for ("triage is a code-aware step"). That
ADR's "Unmet today" line is amended by an owner-approved `adr` pull request,
not by this build.

Why a code-aware step: a vendor classifying its own failing output was
measured, and it is wrong. In the brianpoints run (2026-09-13) antigravity got 3
of 6 wrong. It called the real unreachable-core defect a harness gap, because
the traceback pointed at `conftest.py`, and it invented "two completed trade
records" for an assertion that reads `len(settlement.calls)`. Classifying a
failure needs the component boundary, and the tester is denied exactly that
(ADR-0006 §6).

## Usage

```
bin/fleet-triage <repo> <plan-id> --impl-sha <sha> [--dry-run] [--no-post]
```

It reads `meetings/<plan>/<impl-sha12>/findings.json` under the fleet work root.
By hand it works the way `fleet-lane` does; plan 0104's steward calls it with
the same arguments. `--dry-run` runs every gate, builds the directory and the
prompt, prints the prompt's size and sha256, and sends nothing. `--no-post`
runs the triager and prints the comment instead of posting it; a later run for
the same findings posts what that run found without spawning the triager again.

Exit codes: `0` posted, already posted, nothing to triage, `--dry-run` or
`--no-post`; `1` refused; `2` usage; `3` the triager did not produce valid
output, and the untriaged comment was still posted.

## The gates

In order. Every refusal is logged, paged once, spawns nothing and posts nothing.

1. **Findings.** The file exists, validates against
   `config/meeting-findings.schema.json` (read from that path, never copied),
   and names this plan, this repository and this implementation head.
2. **Zone.** The plan file on `main` — NEVER at `--impl-sha`, which is the
   implementer's own commit and inside its own lease — says `zone: public`. An
   allowlist (ADR-0006 §7): this step sends source to a cloud backend. An
   absent, `private` or malformed zone refuses. Read the same way fleet-watch's
   `_plan_by_id` reads it, off the store's `origin/main` (`lib.ensure_store`'s
   ordinary clone, refreshed by its own `git fetch --prune origin`): an
   implementation branch that edits `plans/<id>.md` to flip `zone: private` to
   `zone: public` under its own lease no longer moves this gate.
3. **Mediator.** `<repo>`'s `mediator` at ordinal 1 resolves through
   `fleet-models`. If nothing resolves it refuses and names the owner's command,
   `fleet-models set default mediator sub-opus`. There is no built-in triager:
   a constant in code is a setting nobody wrote (plan 0080 §3), and an
   unattended run spends the owner's subscription.
4. **Adapter table.** The resolved runner must be in this program's table,
   which holds exactly one entry, `sub-opus`. Anything else is refused by name,
   whatever `fleet-models` allows at `mediator`. This step sends source. ADR-0006
   §2 sends vendors "never the source", and no ADR decides whether source may
   go to Google or OpenAI. `sub-opus` goes to Anthropic, which already holds the
   code through the implementer. Read confinement is measured per CLI, and
   widening the table needs an ADR and a measurement, not a table edit.
5. **Nothing to triage.** Every test passed: a zero-item `triage.json`, no
   spawn, no comment ON THE IMPLEMENTATION PR, exit 0. A skip is not a pass,
   so a skipped test is triaged. This is still the all-pass case "What
   happens to the lane" below describes: an open, marked defect issue still
   gets its one "no defect" notice comment, because an all-pass meeting is
   the strongest form of "no lane kept for a defect."
6. **Already done.** `triage.json` for this findings digest (the sha256 of
   `findings.json`'s bytes) holds a posted comment id AND no recorded failure,
   or the thread already holds a comment by the author role carrying this
   digest's `status=ok` marker: exit 0, no spawn. A comment marked
   `status=failed` never satisfies this gate, so a person re-running
   fleet-triage by hand for the exact same digest — no new `fleet-meet` run
   required — spawns the triager again. A thread that cannot be read in full is
   a refusal; a truncated read is never taken as "no comment".

The seat is `mediator` (ADR-0009 §1). It sits right after the mechanical meeting
and is the step that decides, when tests and code disagree, which side is wrong
and why. A new position would need a `repo_model` migration, an ADR amendment and
a fresh ADR-0011 class decision.

## The directory and the argv

The triager's working directory is built by the wrapper under the system
temporary root (the CLI loads instructions from every ancestor of its working
directory), made read-only, and removed when the run ends:

- `impl/` is the tree at `--impl-sha`, exported with `git archive`. It is not a
  worktree: a linked worktree's `.git` points into a store that holds every other
  branch, so there is none. Every symlink is dropped and listed in the prompt as
  omitted, because a committed symlink is a path out of the directory that the
  permission check was never measured against.
- `lanes/<runner>/<path>` holds each non-pass test's source, read with
  `git show <lane head>:<path>` (the lane's content, not `main`'s).
- `meeting/` holds `findings.json` and each lane's gaps, parsed from the lane PR
  body's `gaps:` block. If the block is missing or does not parse, the prompt says
  `gaps unreadable for <runner>`, never "no gaps". A gap that spans several lines
  parses as one gap per line: `fleet-lane`'s renderer keeps no boundary.

The argv is a literal this program owns, asserted by equality:

```
claude -p --output-format json --model opus --tools Read,Grep,Glob
       --restricted --strict-mcp-config --setting-sources ""
       --disable-slash-commands --no-session-persistence
```

- `--tools Read,Grep,Glob` is the code-aware grant. There is no `Bash` (the
  meeting already executed the tests), no `Write`, `Edit` or web tool: the inputs
  are vendor-authored test source and implementer-authored code, and both are
  injection surfaces.
- `--restricted` is PINNED, not relied on as an unmeasured default. `claude
  --help` on 2.1.284 documents it as confining "the file tools to the working
  directories (`--add-dir` included)", refusing `bypassPermissions`, and
  ignoring user/project/local settings files. Measured directly on this host
  with a discriminating probe (`--tools Read,Grep,Glob --restricted`, no
  `--allowedTools`): a Grep for a marker string *inside* the working directory
  found it; the identical Grep for the identical marker in a sibling directory
  was refused, with a `permission_denials` entry naming that exact call. There
  is no `--allowedTools` and never a permission-mode flag: either would reopen
  exactly what `--restricted` closes.
- `--strict-mcp-config` (no `--mcp-config`) leaves no MCP server;
  `--setting-sources ""` is belt-and-suspenders with `--restricted`, keeping the
  operator's `~/.claude/CLAUDE.md` out; `--disable-slash-commands` and
  `--no-session-persistence` leave no skills and no transcript.

The environment is `PATH`, `HOME`, `USER`, `LOGNAME` and `TMPDIR`. There is one
timeout and no retry: the run spends the owner's Claude subscription. The
findings digest makes a second call for a SUCCESSFUL triage harmless (the
comment's marker carries `status=ok` and gate 6 exits 0, no spawn). A failed
triage's comment instead carries `status=failed`, which gate 6 does not count as
done: a person re-runs it by hand for the same digest, with no new `fleet-meet`
run required, and that rerun spawns the triager again.

The live confinement test (`FLEET_TRIAGE_LIVE_CLAUDE=1`, opshost only; CI holds no
Claude login) checks the pinned argv reads a marker inside the directory and not
one outside it, by `Read`, `Grep` or `Glob`, does not surface a `CLAUDE.md`
sentinel, and that adding `--allowedTools Read` reads the outside marker. **CLI
version:** `--restricted`'s confinement was measured directly on 2.1.284 (the
version installed on opshost) with the discriminating Grep probe above; the fuller
live pytest (`Read`/`Grep`/`Glob`, inside and outside, plus the `CLAUDE.md`
sentinel) still needs a run on 2.1.284 to replace the 2.1.282 record it carries
today — run it and put the version it passed on here.

## The triager's answer is a claim, and the wrapper checks it

The document is validated against `config/meeting-triage.schema.json`. Items
carry `lane`, `test`, a class, evidence and a rationale. `gaps` lists only the
gaps the triager judges to be contradictions.

Classes: `implementation-defect`, `spec-or-contract-defect`, `test-harness-gap`,
`skip-needs-contract`, and `undetermined`. `undetermined` is a class on purpose:
the measured failure is a confident wrong answer, and "could not tell, and here
is why" is a finding the owner can act on.

Each citation is `{root, path, lines, quote}` and must resolve mechanically:

- `root` is `impl` or a `lanes/<runner>` that exists. `meeting/` is never
  citable: a gap, a reason text or a vendor note is a claim, not evidence.
- `path` is relative, has no `..` and no control character, and names a file
  inside the root.
- `lines` is `[start, end]`, 1-based, inside the file.
- `quote`, whitespace collapsed, has at least 8 non-space characters and occurs
  in those lines, also whitespace collapsed. A line number alone is not checked
  against anything: fabricated evidence has been measured (a confident
  description of an assertion that did not say it), and the quote is what
  catches it.

Each class has an evidence rule:

| class | needs |
|---|---|
| `implementation-defect` | a verified `impl` citation outside every lane test path |
| `test-harness-gap` | a verified citation into that test's OWN `lanes/<runner>/` — never another lane's |
| `spec-or-contract-defect`, gap `contradiction` | two verified citations at different locations |
| `skip-needs-contract` | a verified citation in that lane's own files |
| `undetermined` | none |

Any citation that fails fails its item, whatever the class. An item that fails
is kept and rendered under "evidence did not verify" with its claimed class and
the check that failed; it never appears in the table or the counts. Every
non-pass appears exactly once: a missing one renders as `not triaged`; an item
naming a test that is not a non-pass, or naming it twice, is dropped, and the drop
is noted.

**What this proves.** It proves the cited text exists where the triager said. It
does not prove the class is right. The per-class rules stop the specific
inversion measured (a harness gap asserted on code-only evidence, or the
reverse). The class is still the triager's judgment, and the owner's.

## The record and the comment

`triage.json` sits beside `findings.json`: the findings digest, the runner, the
model the CLI reported (never a constant), the prompt sha256 (`prompt.txt` sits
there too), the triager's raw document, each item's verification result and the
posted comment id. There is no `fleet.db` table and no event type.

The comment is posted as the author role through `create_issue_comment`. It opens
with `<!-- eunomia:meeting-triage findings=<digest> -->`, names the plan, the
implementation sha and each lane PR with its head sha and prompt sha256, and gives
the triager's runner and reported model. Then, in order: a table of verified
classifications (each citation a forge link to `src/commit/<sha>/<path>#L<a>-L<b>`),
the verified gap contradictions, "evidence did not verify", and `not triaged`. All
triager and vendor free text sits inside fenced blocks with every line prefixed
`> ` and every `<!--` removed, and table cells carry no markup or line break.

When the triager fails (timeout, non-zero exit, unparseable output, a schema
failure) the comment is still posted, listing every non-pass as `not triaged` with
the reason, and the exit code is `3`.

**Assert before posting.** The body is refused if any line matches the
orchestrator's `_ACK_RE` (imported), if any line starts `Plan:`, or if it carries
an `<!-- eunomia:` marker other than its own. This comment is posted as an
implementer-authored account and quotes vendor text, and an `Ack: #n` line in an
implementer-authored comment would consume an owner instruction.

It is a report only. It is never a commit status (a failing status blocks review
dispatch, and false divergences are expected), never a pull review, and never
starts a fix round: a fix round starts only from an owner instruction.

Structured fields (the sha, the findings digest, the prompt sha256) are written to
the run log verbatim, checked for shape rather than redacted. Only the triager's
free-text stdout passes through `redact_transcript`.

## What happens to the lane

*Plan `plans/0176-a-triaged-lane-is-closed-or-kept.md`.* After the comment above
is decided (whether or not it posts), every lane PR named in the findings
document gets one disposition, judged only by the non-pass tests `findings.json`
records for it, each matched to a verified, classified item in THIS run's
document:

| the lane's non-pass tests | disposition |
|---|---|
| none: every test passed | nothing (but see "A defect" below: a GENUINE all-pass meeting still gets the no-defect notice) |
| every one verified `test-harness-gap` | **close** the lane PR |
| any verified `implementation-defect` or `spec-or-contract-defect` | **keep** it open; one issue per plan |
| otherwise, any verified `skip-needs-contract` | **keep** it open; one lane comment naming the contract it needs |
| otherwise (`undetermined`, unverified, untriaged, or the triager itself failed) | **keep** it open; page once |

A non-pass the triager never answered (it is in the verified document's
`untriaged`) or whose item failed verification counts as `undetermined` for this
table, whatever class it was claimed under — a lane is closed only when EVERY
one of its non-pass tests is a verified `test-harness-gap`, and that citation
must be into THAT test's own `lanes/<runner>/`: a citation into a different
lane's directory proves that other lane's gap, never this one's.

Before acting, the rule re-reads the lane PR: it acts only when the PR is open
and its current head still equals the `head_sha` findings.json recorded for it.
A moved head, or one that is already closed, is a genuine, permanent skip for
this lane head — logged, never re-closed, never re-commented, never paged (a
later meeting at the lane's new state judges it afresh). A lane the gate could
not even read is a DIFFERENT case: it is paged once per (lane, digest), exactly
like a failed write, and tried again on the next run — an unreadable
prerequisite must reach the owner, never only the log. Either way, a lane the
gate turned away this run never appears in the defect issue's body or on the
undetermined page for this run: its disposition no longer applies here, and a
stale re-render would re-PATCH the issue forever over a lane that moved on.

**Closing.** The comment posted on the lane PR — before the close, never after —
names the triage comment, the implementation sha, the lane head, and each
failing test's class and rationale, and carries
`<!-- eunomia:lane-closed plan=<plan> head=<lane head sha> -->`. That marker
proves a close was ATTEMPTED; it is written to the comment and sent before the
close PATCH is even tried, so it is never, by itself, proof that the close
completed.

The one marker the reopen guard trusts is a SEPARATE, second comment,
`<!-- eunomia:lane-closed-confirmed plan=<plan> head=<lane head sha> -->`,
posted only after the close PATCH has actually returned success. Before every
close attempt, a later run checks the lane's own thread for that exact
confirmed marker, at this exact lane head, from ANY digest. Found, the fleet
closed this lane at this head before and a person has since reopened it
— the rule leaves it alone until the head moves. Not found, nothing durable
says a close ever completed at this head, so it is safe to proceed: either no
close was ever attempted, or one was attempted and the comment landed while the
close PATCH itself kept failing (D2a's measured shape) — never a human's
reopen.

An earlier design tried to tell these two cases apart with local bookkeeping
instead — a flag carried forward from whichever `triage.json` on disk still
recorded a confirmed close. That broke: `triage.json` for one implementation
head is rewritten by every new digest at that head, so the one record proving
a close had ever completed could be, and was, overwritten before a THIRD
digest needed it, and the guard then re-closed a lane a person had reopened in
between. Checking the forge comment thread directly needs no local history to
survive at all — the confirmed marker lives exactly where the closed state
itself does.

Posting the confirmed comment is its own retried step, owed for as long as it
takes even once the PR shows closed — exactly the state that step itself
causes, so it is never mistaken for "this lane moved on" the way an ordinary
pending step would be. `fleetforge` gains no verb for any of this — both
comments and the close PATCH go straight through its transport, the same way
`bin/fleet-rebase`'s `_close_relanded` closes a re-landed pull request.

The lane gate's own "is this PR still open, at this head" re-read, done right
before acting, has one exception besides the confirm-only case above: when the
CLOSE step is still pending but the COMMENT step already succeeded (proof this
disposition really did attempt the close) and the gate finds the PR already
closed — not merged — at exactly this lane head, that is D2a's measured lie:
the close PATCH landed even though the write that attempted it reported
failure. Read as an ordinary "not open", that used to be a terminal skip,
which abandoned the confirm step forever and left a later owner reopen with no
confirmed marker on record to stop a future digest re-closing it. The gate now
treats this one shape as the close having succeeded and lets the confirm step
run. A lane found closed before this program ever touched it — the comment
step never even attempted — still gets the ordinary "already closed" skip:
no comment, no close, no event.

**A defect.** One issue per `(repo, plan)`, titled `lane tests for <plan> found
a defect`, marked `Lane-defects-for: <plan>` in its body, found by listing the
repository's issues (also through `fleetforge`'s transport, not a verb — capped
at 20 pages, the same bound `orchestrator` already uses for its own `state=all`
listings, since this lookup runs on every meeting that keeps no lane for a
defect, including every all-pass one; hitting the cap reads as unreadable, the
same page-and-retry path a transport failure already takes, never as "no
issue") rather than trusted to `triage.json`, which a later lane's digest
overwrites. Its body
is rebuilt from scratch each run, from every lane this run's document keeps for
a defect (never one the gate turned away) — never accumulated across meetings.
The issue's STATE is never changed by a new defect: this rule holds no
issue-reopen capability, any more than it holds an issue-close one, and a
closed marked issue stays closed even when this run's body is rewritten onto
it — rewriting the body costs no state and keeps the record accurate for
whoever reopens it later, but reopening it is that person's call alone, the
same boundary the plan draws around a closed LANE PR.
The no-defect notice below is the one place state is READ, never written: the
lookup is always re-run and re-checked for "still open" immediately before
that notice posts (never a defect-issue edit, which runs regardless of
state), never trusted from an earlier call in the same run or a prior one —
the owner can close the issue in the gap, and the notice must never land on a
closed one.
A later meeting with no defect lane adds one comment to the open marked
issue saying so; if the owner has since closed it, nothing is posted — this
check (and this silence) fires when every lane GENUINELY passed, because
that is the strongest form of "no lane kept for a defect"; there, the
comment names the findings digest rather than a triage comment, since that
meeting posts none. It never fires when the triager itself failed (exit
`3`), NOR when no lane ran at all (one refused, timed out or failed to
collect — the same empty-tests shape as "every test passed", but
`findings["all_passed"]` is false): nothing was classified in either case,
which is a different claim from "no defect was found", so an undetermined
meeting pages (D4) instead and leaves the notice alone. Closing the issue is
always the owner's call. Each defect lane also gets one short comment
linking the issue.
If more than one issue carries the marker — never this program's own doing,
but a possibility it does not rule out — the lookup logs every match and
picks the lowest-numbered, deterministically, rather than whichever page
order happened to surface first. The body this writes has the same size cap
the main triage comment does; one that would exceed it pages instead of
being sent.

**A write that fails.** Each write (a lane comment, the close, the shared
issue's create or edit) is retried up to three times, 2s then 8s apart,
within the run, EXCEPT a comment post: because a POST is not idempotent and
can report failure for a write that landed, every attempt — including the
first — first checks the target thread for a comment that already carries
this exact post's own marker, and recovers it instead of ever sending a
second one. A lane comment's own marker (any disposition — close, defect,
contract or undetermined) is `<!-- eunomia:lane-disposition plan=<plan>
digest=<findings digest> head=<lane head> action=<action> -->`, keyed on the
findings digest rather than the triage comment's own url: a hand rerun of a
FAILED digest re-spawns the triager and posts a brand-new triage comment
every retry, so a needle built from that url would differ on every retry and
never find the lane comment an earlier retry already posted — re-sending the
undetermined page and a near-duplicate lane comment on every hand rerun of
the same failed digest, against D4's "once per (plan, digest)" wording. Keyed
this way, a retry of the SAME digest recovers its own earlier comment, and a
GENUINELY new digest — even at the same implementation head, even for the
same lane head — always posts its own, accurate comment rather than reusing
an earlier one: an earlier design carried a close's `comment_id` forward from
any prior digest ever recorded for the plan, across every implementation
head, which recovered a comment citing THAT earlier digest's own triage
comment and rationale, not the meeting that was actually performing the
close. The confirmed-close marker (above) and the all-pass notice's own
wording (the findings digest, or the triage comment's url) are unaffected —
neither one is reused across a digest change. A lane comment over the main
triage comment's same size cap pages and is retried exactly like a write the
forge itself rejected, rather than going out truncated — the same handling
the shared defect-issue body already had.
A hand rerun of a digest whose triage itself failed still needs D4's "once
per (plan, digest)" kept for its undetermined page, which has no forge-side
marker of its own to check: the fresh disposition this rerun computes
carries forward its PRIOR attempt's `paged` list, for a lane whose action is
unchanged, from this exact digest's own `triage.json` alone — never a scan
across other digests or implementation heads, for the same reason the
comment marker above does not reuse a different digest's content.
The issue lookup (the listing that finds the shared defect issue by its
`Lane-defects-for:` marker) is itself attempted once per call that needs it
(deciding create-vs-edit, or the no-defect notice) and is never retried AS A
READ within the run: a single failed page there pages once immediately and
is retried only by the NEXT run, the same as a lane-gate or close-guard read
that could not complete. The one exception is the CREATE path: because a
POST is not idempotent there either, each create retry (never the first
attempt) repeats this same listing first, to find a duplicate its own
earlier POST actually wrote before trying to create a second one — the
identical idempotency pattern comment posts use above, not an independent
retry of a failed read. A step still pending (or a lookup or read that
never completed) after its own in-run retries, or after the one attempt the
issue lookup gets, pages once, naming the lane, the step, and the exact
`fleet-triage <repo> <plan> --impl-sha <sha>` to retry it by hand, and
whether that rerun replays pending disposition steps or re-spawns the
triager — there is no automatic second triage of the same digest, and
`--dry-run` or `--no-post` send nothing even when a digest is already fully
posted and only a disposition step is still pending from an earlier run. The
decision and every step's pending state are written to `triage.json` before
the triage comment is even attempted, so a POST that reports failure but
actually landed is recovered by the next run's thread-marker check, which
runs the pending steps before returning "already posted".
