# Reviewer contract

**This file is the reviewer's entire mandate.** It is versioned here and changing
it is a pull request to this repo, reviewed like any other change.

The orchestrator substitutes exactly four structured facts and supplies nothing
else — no framing, no per-feature emphasis, no account of what the implementer
tried, no summary of the diff:

- `{repo}` — `owner/repo`
- `{pr}` — the pull request number
- `{sha}` — the head SHA under review
- `{plan_path}` — path of the human-merged plan file in that repo

**Why the orchestrator may not add to this.** The wrapper is graded on
*approved*. A mandate it authors lets it steer the reviewer away from its own
weak spots without ever touching the reviewer's credentials, so coder ≠ reviewer
has to hold at the mandate level and not only at the credential level. That is
the whole reason this text is a file and not a string built at dispatch time.

---

## Mandate

Review pull request {pr} on {repo} at head {sha}, against the plan at
{plan_path} in that repository.

You are reviewing the pull request, which is what a human reviewer sees. You do
not have, and must not ask for, the implementer's session state, worktree diff or
transcript: being handed its reasoning biases you toward accepting that
reasoning.

Verify the change against the plan's Deliverables and Definition of done, and
against its Boundaries — a change that satisfies the deliverables while crossing
a boundary is not done. Check claims against the source rather than reading the
diff as prose. A number the change asserts about the codebase is a claim to
verify, not a fact to accept.

Report findings by severity. **Only HIGH blocks a merge.** MEDIUM and LOW are
recorded and shipped, per the fleet rule the follow-up filer states on every
issue it opens. Do not argue a finding down and do not soften a severity to be
agreeable; a reviewer that grades on cooperation is not a control.

End with a machine-readable findings block and a verdict line.

---

## Executor note — read before assuming this text reached the model

The orchestrator passes this file's path to the review command in
`FLEET_REVIEW_CONTRACT`. **The default command today,
`~/agent/dispatch-review.py`, carries its own equivalent mandate and does not
read that variable.** So on the default path this file documents the mandate the
fleet requires and does not itself deliver it.

That is written down rather than glossed because a contract declared and not
wired is the failure this repo keeps catching in its own code (`Runner.unwired`,
`UNBUILT_PHASES`). The variable is passed today so that replacing the command
with one that honours it is a configuration change and not a code change; until
such a command exists, the guarantee here is the review command's, not this
file's.
