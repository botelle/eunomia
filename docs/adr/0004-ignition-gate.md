# ADR-0004 — Ignition is gated on evidence the watcher can read, not on who may approve

- **Status:** Accepted
- **Date:** 2026-09-06
- **Deciders:** the operator (owner/merger)
- **Relates:** `plans/0015-ignition-gate.md` (the gate this decides),
  `plans/0016-independent-test-lane.md` (what a passing gate dispatches),
  `plans/0013-fleet-repo-enable.md` (enrolment, which surfaces the refusal lanes),
  **[ADR-0003](0003-adr-track.md)** — its principle that tests are written by someone who
  has not seen the code is what §2b of plan 0015 instantiates with a concrete model split;
  and its ruling that `fleet-watch` **cannot** be the authoritative spec-impact asker is
  what bounds check (c) below to an advisory one. `docs/plan-dispatch.md` §"Making a repo
  an ignition repo" is the operator procedure.
- **Spec-impact:** **none for the gate.** It reads evidence and changes no lease, event
  or contract. The two-lease change belongs to `plans/0016-independent-test-lane.md`,
  which carries it: `docs/plan-dispatch.md` §"Leases, death, and respawn" today describes
  one lease per dispatched plan, and `SPEC.md` does not describe dispatch leases at all —
  which an earlier draft of this ADR got wrong.

## Context

"Merge is ignition" is a claim about **who wrote `main`**. `main_is_protected()` therefore
refuses any repo where an agent account can approve, on the reasoning that an approval an
agent can cast is an ignition an agent can forge.

Measured 2026-09-06, running that function itself — not a reimplementation — against 20
repos: **all 20 refuse.** The first lane is universal: `apply_to_admins` is off everywhere,
which is Forgejo's default and was never changed. Stubbing that lane as fixed, the second
is universal too:

> rule 'main' lets agent account(s) ['revbot'] approve — ignition would be
> self-forgeable

That second refusal is two standing rules colliding. `~/bin/forgejo-create-repo.sh`
installs, per the 2026-07-22 standing new-repo instruction: `required_approvals: 2`,
approvals whitelist `revbot` + `operator`, merge whitelist `operator` only. So the
enrolment script **guarantees that no repo it creates can ever merge-ignite.** Neither
rule is wrong. They answer different questions and were written apart.

Two empirical findings shaped the decision, both probed rather than assumed:

- A reviewer removed from the approvals whitelist posts `official: false`. Probed on a
  scratch PR in `operator/tiphys`, protection restored afterwards. Whether a non-official
  rejection still blocks was **not** established — isolating it would have required
  casting the owner's approval — so it is treated as "probably not".
- **`official: true` does not mean "reviewed at head".** Measured across six rounds on
  eunomia #42: `official` tracks the reviewer's *most recent* review. Review 1875 was
  official until 1877 landed, and 1877 is official while sitting on `a9cfb750` — not the
  head commit `43caf715`. An approval is evidence about the tree it was cast on and
  nothing else. A field that DOES track head exists — `stale`, which
  `bin/fleet-reviews:178` already stores — and an earlier draft of this ADR wrongly said
  none did. Check (a) still compares `commit_id` to the merged SHA, because after merge
  that names the exact tree rather than a flag relative to a moving head. Two earlier
  claims here were wrong and are corrected rather than quietly dropped: that the demotion
  came from `dismiss_stale_approvals`, and that no field encoded this at all.

## Decision

**The review requirement leaves branch protection and moves into the watcher.**

Branch protection answers *who may write `main`*. It keeps answering only that: on an
ignition repo the approvals whitelist holds humans only, and `required_approvals` is 1.

`fleet-watch` answers *what evidence stands on the merged plan* — three reads, all bound
to the **merged SHA**, issued concurrently, with every failure reported in one message:

- **(a)** the reviewer's **latest, non-dismissed** review at the merged tree — resolved
  from `merge_commit_sha`, never from `head.sha`, which stays a live pointer while the
  head branch exists — and it is `APPROVED`;
- **(b)** no `fleet-ask` question (plan 0009) left unanswered on the plan's PR;
- **(c)** a declared `spec_impact` whose implied files the plan's own `paths` cover.

Check (c) is deliberately the **advisory** ask, in ADR-0003's sense. It confirms the
plan leased the files its declaration implies — the lease gap that let 0014 miss `SPEC.md`
and both `EVENT_TYPES` copies. It is not a judgment that the declaration is right, and it is not
the authoritative "has an architectural decision moved?" gate — ADR-0003 places that at
pull-request time, where a diff exists, and explicitly rules `fleet-watch` out of it
because at dispatch the implementer has not run.

None of the three grants authority. revbot still cannot merge, so an APPROVE alone
ignites nothing; the human merge remains the authorisation.

> **Revised 2026-09-09 — this paragraph originally read:** *"Implementing this by returning
> the reviewer to any whitelist is forbidden — that is the self-forgeable shape
> `main_is_protected()` refuses, and the two checks would then contradict each other."*
> See **Revision** below. Branch protection keeps `required_approvals: 2` with the reviewer
> whitelisted; what changes is the rule `main_is_protected()` applies to it.

## Revision — 2026-09-09: sufficiency, not presence

**Decided by the owner, against the original decision above.** The requirement is stronger
than what this ADR first specified, and stating it plainly is the point: *the reviewer's
approval must land before the owner decides.* Not so the merge is gated twice, but because
the review routinely finds something the owner missed, and reviewing first is cheaper than
reversing a position already taken.

That is a claim about **merge**, where this ADR had moved the requirement to **dispatch**.
Under the original decision a plan could be merged with no reviewer approval at all; it
simply would not ignite. The owner wants it not to *merge*.

### What was wrong with the original rule

`main_is_protected()` refused any repo where an agent account could approve. Approving is
not writing: an approval satisfies a **counter** that something else must then consume.
What makes an agent approval dangerous is not its presence but its **sufficiency**.

    safe  <=>  agents_on_whitelist < required_approvals

With `required_approvals: 2` and a whitelist of one agent and one human — exactly what
`~/bin/forgejo-create-repo.sh` installs — an agent cannot reach the threshold. A human
approval is **structurally mandatory**. The original rule refused that configuration, which
is why it refused all 20 repos, and the remedy it proposed (drop to 1 approval, remove the
reviewer) is strictly weaker than what the fleet already had.

### The auto-merge fact that makes this precise

Forgejo's **scheduled auto-merge** is real on this instance — 15.0.2, and its merge endpoint
accepts `merge_when_checks_succeed` (probed 2026-09-09). So "a human must press merge" was
never the guarantee; a human may schedule the merge in advance and walk away.

This is exactly why sufficiency is the right property and presence is not. Auto-merge fires
when the conditions are met — and with two approvals required and one agent whitelisted,
they cannot be met without the human's approval. The guarantee moves from *who presses the
button* to *what the button waits for*, which is the more durable place for it.

An earlier attempt (PR #251) relaxed the approvals lane on the reasoning that nothing on
this fleet auto-merges. That reasoning checked minos and missed Forgejo's own feature. The
rule below does not depend on the absence of auto-merge.

### The rule as revised

- **Merge whitelist: absolute.** A merge is not counted, so one whitelisted agent writes
  `main` by itself. No agent may be on it. Unchanged.
- **Approvals whitelist: sufficiency.** Agents may be on it, provided they cannot reach
  `required_approvals` between them. A whitelist that is disabled, unreadable, or team-based
  is refused, because nothing bounds the agents.
- **Unsatisfiable is also refused** — `required_approvals` higher than the number of
  non-agent accounts whitelisted means nothing can ever merge, and vouching for it would
  promise a dispatch that cannot happen. The old rule never noticed this; a test fixture in
  this repo had carried exactly that configuration.

### What does not change

`apply_to_admins` must still be ON — an admin bypasses every lane above it, and that is the
refusal that actually applies to all 30 protected repos today.

The three ignition-gate checks (a), (b) and (c) are unchanged and still required. The
reviewer's approval now gates the merge **and** the dispatch, which is redundant on purpose:
the first is the owner's workflow and the second is the machine's evidence, and they fail
independently.

### Actors and steps

Node kinds: **human**, **agent**, **service**, **artefact**, **decision**.

| # | actor | step |
|---|---|---|
| 1 | agent / human | plan PR opened against `main` |
| 2 | revbot (agent) | reviews; APPROVE recorded against the head SHA |
| 3 | **the operator (human)** | merges — **this, and only this, is the authorisation** |
| 4 | fleet-watch (service) | cycle sees a `status: ready` plan on `main` |
| 5 | fleet-watch | gate: (a), (b), (c) issued **concurrently** against the merged SHA |
| 6 | decision | any failure → refuse, reporting **all** failures at once; no dispatch |
| 7 | fleet-watch | on pass, spawn **two** lanes in parallel |
| 7a | implementation lane (Opus) | branch `agent/<plan>-impl`, own lease, own worktree |
| 7b | test lane (Gemini via oracle) | receives the plan, ADR and flow spec **as text** — no repo, no branch, no clone, no credential; returns file content — **`zone: public` plans only** |
| 8 | credentialed wrapper | commits the test lane's returned content to `agent/<plan>-test` and opens its PR. The test agent holds no write path at all — that is what makes "never receives credentials" architectural rather than configured |
| 9 | — | the tests land as **their own PR with their own human merge**, never into 7a's branch: a feature carrying its own tests can pass by adjusting them |
| 10 | mediator (Fable) | on divergence, reads the ADR and returns an adjudication and a `status: draft` follow-up plan **as content**; the wrapper posts them. The mediator holds no token and arms nothing |
| 11 | **the operator (human)** | reviews and merges the implementation PR |

The only edge between 7a and 7b is step 9, it is mechanical, and it is mediated by
committed artefacts rather than prose.

## Alternatives rejected

**1. Drop `revbot` from the approvals whitelist, change nothing else.**
Rejected. The operator is the only human on the whitelist, so `required_approvals: 2` becomes
unsatisfiable — one user holds one review state — and must fall to 1. The gate halves. And
because a non-whitelisted review is `official: false`, Fable's REQUEST_CHANGES very likely
stops blocking too, so the review degrades to advisory text with no force at any point in
the pipeline. This buys the watcher's lane at the cost of the property the review existed
for.

**2. Relax the watcher's approval lane for repos whose merge whitelist is human-only.**
Tempting, and the stronger argument on its face: the runbook's own worst case turns on
*the queue merging*, and with merge locked to `operator` a human is structurally in the
chain no matter who approved. Rejected because it weakens a deliberate check on the
strength of an argument about Forgejo semantics rather than a property the code can read —
and because an approval *count* is not the property we want. What we want is a review **of
this tree**, which is check (a), and no branch-protection setting can express it.

**3. Do nothing; never arm dispatch.**
The honest status quo: `FLEET_WATCH_REPOS` is set nowhere — not in a shell rc, not in the
environment — there is no `fleet-watch` launchd unit, and the watcher has never armed.
Rejected because the plan is the artefact an unsupervised session reads, and PR #42 is the
argument: six review rounds on a **plans-only** change found five distinct paths that would
TERM the wrong process, two of them the operator's own live session.

## Consequences

- **`required_approvals` drops 2 → 1 on ignition repos.** Framed honestly: today's "two
  approvals" is *the operator + Fable*, so what is lost is Fable's vote at merge time. It
  reappears at dispatch time as check (a), bound to the merged SHA — which is *stricter*
  than the approval it replaces, because an approval of an earlier tree no longer counts.
  If the 2 was ever meant to be two independent **humans**, this decision does not deliver
  that and does not pretend to.
- **And check (a) restores that force for PLAN pull requests only.** Alternative 1 is
  rejected below because a non-whitelisted review is `official: false`, so Fable's
  REQUEST_CHANGES very likely stops blocking and the review degrades to advisory text.
  This decision is alternative 1 plus `apply_to_admins` plus check (a) — and check (a)
  runs at dispatch, on plans. For every **other** PR on an ignition repo, including the
  implementation PR at step 11, the chosen decision shares exactly the property
  alternative 1 was rejected for: Fable reviews, and nothing makes the verdict binding.
  That is a real cost of this decision and it belongs here rather than in the rejection
  of the option that owns it. Closing it needs a separate control — a required status
  check fed by the review, or a merge queue that refuses on an open REQUEST_CHANGES —
  and neither is in scope here.
- **A new refusal surface that is not the plan's fault.** An unreadable review list, a
  5xx, an expired token — none of these is a missing approval. They page; they must never
  refuse silently, which is the same distinction plan 0013 draws for the protections and
  allowlist reads. A control plane that turns itself off quietly on infrastructure flake
  is the failure this fleet keeps rediscovering.
- **Ignition repos need an operator change** that is not code: `apply_to_admins` on,
  approvals whitelist humans-only, `required_approvals: 1`. Until then
  `main_is_protected()` refuses the repo and the gate is never reached. Procedure in
  `docs/plan-dispatch.md`.
- **The cloud test lane is `zone: public` only.** `zone: private` already means a plan's
  work never reaches a cloud backend, and the test lane sends the plan, ADR and flow
  spec to Google. The
  first concrete case is oracle's own `plans/0001-gemini-backend.md`, which is
  private-zone and therefore cannot be implemented by the lanes it exists to enable.
- **A dead test lane must be visible.** A PR that silently lost its independent tests is a
  false green, and false green is the failure mode every other control here is shaped
  against.
- **The cap now counts plans, not orchestrators**, or two spawns against a cap of 1
  deadlock or halve throughput depending on where the check sits.
