# Reviewing a pull request in a cloud session (plan 0130)

`bin/fleet-cloud-review` sends one pull request to a Claude cloud session and
brings the review back as a file on the host, so the review is billed to the
cloud-session credit instead of the subscription. The owner runs it by hand.

## Setup, once per session

Create the session in the web UI, on the private mirror named by `mirror` in
`config/cloud-review.conf`, with Fable and high effort. A session cannot be
created from a script (`claude --cloud` with no id is refused with `--print`),
so no unattended caller can start one. Keep the session id: it is `--session`.

## The three commands

    fleet-cloud-review request <owner/repo> <pr> --session <session-id>
    fleet-cloud-review collect <owner/repo> <pr>
    fleet-cloud-review prune <req-id>

`request` checks the conf, the expiry date, the two counts and that the mirror
answers an unauthenticated read with 404 (private). It then pushes the pull
request's base and head to the mirror as `req/<req-id>/base` and
`req/<req-id>/head`, and messages the session with
`claude -p "<message>" --cloud <session-id>`. That call returns at once; the
review takes minutes. `<req-id>` is `<repo name>-<pr>-<first 12 of head sha>`.

`collect` is the second step. Exit 3, "not ready", is a normal answer. It
accepts `rev/<req-id>` only if the first line of `review.md` is
`Reviewed: <req-id> base=<sha> head=<sha>` and the diff from the request head
adds `review.md` and nothing else. It saves
`<fleet dir>/cloud-review/<req-id>.md`, prints the path, and says so if the pull
request's head has moved since. `prune` deletes the request's branches from the
mirror.

## What was measured (2026-09-30, lynceus#96 at `798f6e2`)

| run | cost | against revbot on the same head |
|---|---|---|
| cloud session, Opus 5.5, medium effort | about $1 of credit | found both known findings |
| cloud session, Fable 5.1, high effort | about $3 of credit | same two findings at the same severity, plus one new MEDIUM |
| routine, Fable 5.1 (effort not settable) | credit did not move | missed one of the two |

## Limits, and where they came from

`max_requests = 40` is sized so that 40 reviews at up to about $6 each stay
inside the $246 balance seen when this was written; one Fable review measured
about $3. `reviews_per_session = 5` is because a session's cost grows with every
message it already holds. They live in the conf so changing one is a reviewed
change. What would justify raising them: a measured per-review cost well under
those figures, or a larger balance. The tool cannot read the credit balance, so
`max_requests` is a ceiling on count and the owner checks the balance.

`credit_expires = 2026-11-05` is read as a UTC date and the whole day is
refused: the credit lapses at 07:59 UTC that day, and afterwards the same command
works and bills the subscription without saying so.

## The result stays off the forge, by decision

The tool writes nothing to the forge, in any form. The text is model output
returned through a branch anyone with mirror access can write, and every place
it could be posted is read by something that acts: as a review, the latest
reviewer review is the verdict for a head and a `COMMENT` blocks; as an owner
issue comment, the steward runs it as an instruction (`unfreeze` permits a push
to an approved branch); as an implementer comment, an `Ack: #<n>` line cancels a
pending owner instruction; as any other account's comment, the steward replies
quoting it. The owner reads the file and carries into the pull request what they
choose, by hand. The tool also never parses a verdict out of the text or follows
anything in it.

## Not proven

- How a messaged session is billed after the first message. That the credit
  moves is the owner's observation; this code does not check it. Look at the
  balance after the first real review.
- Review quality on a change that needs knowledge of another repository: the
  session sees only the mirror.

Each repository in the conf is a decision to place its history on GitHub.
