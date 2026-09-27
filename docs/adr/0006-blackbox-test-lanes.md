# ADR-0006 — Tests are written from a generated contract, by models granted nothing

- **Status:** Accepted
- **Date:** 2026-09-06; trimmed 2026-09-22 (the probes moved to
  [measurements](../measurements/adr-0006-blackbox-test-lanes.md); section
  numbers, the rules and the amendments are unchanged)
- **Deciders:** the operator (owner/merger)
- **Supersedes:** most of [ADR-0003](0003-adr-track.md)'s Decision — §1's
  trigger, §3's inputs and single vendor, and the post-merge timing in its
  Consequences. **0003 §4 is not superseded**: the models hold nothing, a
  credentialed wrapper writes, the ADR is the referee (§5, §6 here), and its
  `zone: private` rule survives as §7.
- **Superseded in part by:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md),
  2026-09-25 — §1 in full: there is no `extlane` flag, every repository has a
  test lane, and the tester's runner is the disclosure decision. §3's minimum
  of one now binds every repository.
- **Amended by:** §§8-9 added 2026-09-23, both **Unmet today**;
  [ADR-0009](0009-a-lane-may-have-a-supervisor.md) — §3's vendor
  count is per-repo configuration, minimum one, with an optional supervisor.
  **Owner, 2026-09-13** — §1's default is unchanged; setting the flag is a
  configuration change, not a reviewed merge.
  **Plan 0070, 2026-09-22** — §6's settings-source sentence is refined: a pin
  where the CLI has a flag for it, a checked precondition where it does not.
- **Relates:** [ADR-0005](0005-pinned-repo-reads.md), whose tool-grant rule this
  depends on; `plans/0013-fleet-repo-enable.md`, whose unbuilt second half is
  a hard prerequisite (§1).

## Context

ADR-0003 made tests independent only on changes that touched `docs/adr/**`, and
handed one third-party model "the plan, the ADR and the flow spec, as text".
Three things were wrong with that. The trigger was the wrong axis: plan 0012
carried a regression test that could not fail, and it amended no ADR. "As text"
was not a design but a side effect of routing the lane through the completion
gateway. And the unit was wrong: these are integration tests, which need the
externally observable contract — routes, payloads, event names, exit codes —
not function signatures.

## Decision

**1. A test lane is opt-in per repository, and the default is off. Recording
an answer is a configuration change, not a reviewed merge.**

*(Superseded 2026-09-25 by ADR-0013 §1. Every repository now has a test lane
and `extlane` is removed. What survives of this section's reasoning is that a
generated contract is a disclosure: ADR-0013 moves that decision from a
repository-wide flag to the choice of runner at `tester`. The text below is
kept as it was decided.)*

`extlane` is not a feature flag. It answers *may this repository's generated
contract be sent to external vendors* — a disclosure decision. Of 41
repositories, 38 carry no answer; on-by-default would turn that silence into
consent, and §7's per-plan gate cannot constrain a repository-wide artifact.
The flag is the third beside `enforce` and `dispatch` in `config/repos.conf`;
a decline is an explicit `no-extlane`, because an absence cannot be told from
an oversight. eunomia is enrolled; speakhush declines and records that in its
own repository; ares is deferred until it has a declared-behaviour document
(§2) and an extractor for the surface it actually exposes.

Setting the flag is a write through the fleet control app with a change record
(the owner's rule, 2026-09-13: a flag flip should not cost a pull request).
That is safe where flipping the default is not: an unreviewed write still needs
a person acting on that repository; a flipped default needs nobody.

**This is blocked, and the blockage is the point.** `fleet-watch` reads its
allowlist from `FLEET_WATCH_REPOS` and nothing else. Until plan 0013's
main-via-contents-API read exists, the flag is a record with no reader, and no
external lane may dispatch — default-off is a claim only a consulted flag can
make true.

**2. The lane receives a bundle of generated artifacts, and never the source.**

The plan, the ADR, the flow spec, and a **generated contract**: OpenAPI for a
service, the declared argument surface for a CLI, declared event names and
config keys, and the harness entry points. An allowlist of artifacts, not a
filtered tree: filtering means being wrong once; generating means nothing is
present unless code put it there.

**The contract is the input side; the output side cannot be generated.** Every
generated artifact says how to invoke the thing under test; none says what to
observe, because record shapes and exit codes live in function bodies, and "no
function body" is the rule that makes the bundle safe to send. So **the bundle
carries a hand-written declared-behaviour document, and its absence is a
mandatory spec gap under §4**: for a CLI, at least exit codes and their
meanings, what it writes and where, and the shape of what it writes. Without
it a lane can only test the contract, which passes for an implementation that
does nothing.

**Not raw `--help`.** Most of eunomia's CLIs print their module docstring
there, and `fleet-watch`'s describes the security model and the token-helper
path. The builder emits the parser's *structure* from a fixed field allowlist
and never reads a prose field; its test asserts both halves by parsing.

**3. One or more vendors, one bundle. At least one is required.** *(Amended by
ADR-0009; this read "two vendors".)* Every lane receives byte-identical input.
Where lanes disagree, the specification was ambiguous, and the wrapper
surfaces the disagreement rather than reconciling it. Zero configured testers
on an opted-in repo is refused.

*(Amended 2026-09-25 by ADR-0013 §2: "an opted-in repo" is now every
repository. Ordinal 1 is required; further testers are optional.)*

**4. A spec gap is an output, not a failure — and one gap is mandatory.** A
lane that cannot tell from the bundle what should happen says so. The generated
contract is the one implementer-authored input, so it carries the
implementer's misreadings; a disagreement between the plan and the contract is
therefore a required output. The builder's first run found one before a lane
existed: two declared copies of `EVENT_TYPES` that disagreed.

**5. One test pull request, approved by a different human.** Both suites, the
disagreements and the gaps land in one PR against `main`, never into the
implementation branch. **Unmet today**: the fleet has one human approver, and
an agent's approval cannot satisfy a rule whose content is "a different
person". A test PR approved by the same human is recorded as such, not counted
as independent.

**6. The lanes are granted nothing — and that is a statement about the spawn,
not the prompt.** No tool available, no filesystem reachable, working directory
outside any repository, the bundle passed as content. Only the credentialed
wrapper holds a shell and writes.

The property is established by measurement per CLI, not by a flag name: three
revisions of this section named something that did not do what it appeared to
(a gateway's structural lack of a shell, then `--allowed-tools`, then a
read-only sandbox that does not confine reads). Measured 2026-09-07: **Codex
has no no-tools mode** — `-s read-only` stops writes, not reads — so its lane
needs isolation the CLI cannot supply (a container, a VM, or an account whose
home holds only the bundle). **Antigravity denies reads by default in headless
mode**, with and without `--sandbox`, because nothing can answer a permission
prompt; a `--dangerously-skip-permissions` control run proved the file was
readable. That deny is a default, one settings file away from gone, so the
wrapper pins the settings source where the CLI has a flag for it (codex:
`--ignore-user-config`), and where it has none (antigravity) it refuses to
spawn while the operator's settings grant anything, and re-measures the
default deny on the installed version — a check, not a pin, and named as
such (amended by plan 0070, 2026-09-22). The Google lane is
Antigravity; `gemini-cli` no longer serves individuals on a subscription, and
an API key is what §1 rejects. **The two lanes therefore need different
isolation, and a plan that treats them as one shape will be wrong about one.**

Why it must be said: a model behind a gateway structurally has no shell; a
local session runs as the operator, can read `~/dev/<repo>` — ADR-0005: "the
prompt clause is an instruction, not a sandbox" — and can run the fixed-path
token helper, which turns a read into a write path.

**7. Only `zone: public` dispatches an external lane.** An allowlist, because
an implementer copying "`private` sends nothing" writes `if zone == "private"`
and a plan with no `zone:` line then sends its text to vendors; `fleet-watch`
forwards the field unvalidated and `fleet-plan lint` is a test, not an
authorization check. So: `zone: public` dispatches;
anything else, including absent, malformed or unreadable, sends nothing. Per-plan
zone cannot constrain the repository-wide contract, which is the second reason
speakhush declines outright rather than selectively.

**8. A vendor's classification of a failure is an input, never a finding.**
Deciding whether a failure is the service, the specification or the test double
requires knowing where the component boundary sits — exactly what §6 denies the
tester. Triage is a code-aware step. **Unmet today**: no lane runner exists to
route failures anywhere, so nothing yet distinguishes a classification from a
finding.

**9. A flow specification's first run is executed, not merely authored.** The
lane catches ambiguity and contract contradiction; it does not catch a
specification that is confidently wrong about *which component does what*, and
such tests pass against the double. What exposes it is being forced to make the
suites runnable against the real service. **Unmet today**: nothing requires a
first run, and §2's enrolment does not ask for one.

*§2 lists "the harness entry points" in the generated contract; what a test may
*program* through them — return this, return an unreadable body, be unreachable
— is output side, which §2 already says cannot be generated. It belongs in the
hand-written flow spec, and a suite is unrunnable without it.*

## Consequences

- **Egress is the boundary, not permissions.** The lanes run on the operator's
  own subscriptions; what crosses is content. The flag answers "may my contract
  be sent", and §6 answers "what may the models do" with nothing.
- **Enrolment costs a written specification.** A repository without its exit
  codes and output shapes written down cannot enrol. Naming the bar beats
  discovering it as an empty suite.
- **A generated contract is still a disclosure**, which is why the flag is per
  repository.
- **Rate limits become a scheduling concern.** A plan whose lanes could not run
  must say so on the implementation PR; a missing suite never reads as clean.
- **Divergence is caught before either merge**, in a scratch worktree, reported
  **by comment, not by commit status** — a failing status blocks review dispatch
  (ADR-0002 §2) and false divergences are expected.

## Alternatives considered

- **Restrict by token scope.** Forgejo has no path-level read ACL; absence is
  stronger than permission.
- **A read-only token and a checkout.** Reopens everything §6 closes.
- **Raw `--help` as the contract.** Rejected on measurement (§2).
- **Extract the output side from the implementation.** Reading function bodies
  to decide what to send fails open the first time output is expressed
  differently; a hand-written spec is weaker evidence about the code and a far
  stronger guarantee about the disclosure.
- **Extract the CLI surface statically.** An AST walk misses subcommands built
  in a loop; the builder imports and intercepts `parse_args`, and normalises host
  paths out of evaluated defaults.
- **Strip source with an AST pass.** A filtered tree; kept in reserve for a repo
  with no generated contract.
- **Keep the ADR-diff trigger.** Independence would depend on which file was
  touched.
- **One vendor.** Loses the only cheap signal about ambiguity.

## Definition of done

The bundle builder is built (`bin/fleet-bundle`, plan 0018) and wired to
nothing until §1's flag has a reader and §7's gate exists. The
declared-behaviour document (parked note 0019) and the vendor runners (plan
0066) are the next things to land; everything else here is a decision.
