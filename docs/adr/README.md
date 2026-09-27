# ADRs — what the status field means here

Every record in this directory carries `- **Status:** <word>` on its third line.
The word is one of:

| status | meaning | who sets it |
|---|---|---|
| Accepted | the record is merged, or its pull request is open — the PR *is* the proposal | the author, in the PR |
| Superseded by ADR-NNNN | a later record replaced it; this one stays for the citations | the author of the later record, in the same PR |
| Deprecated | withdrawn without a replacement | a PR that says why |

**Merge is acceptance.** This repo has no post-merge writer: `main` is
protected, so nothing can flip a word after the merge, and a record that reaches
`main` still saying `Proposed` reads as undecided for as long as nobody notices.
Measured 2026-09-21: all ten records here said `Proposed`, every one merged and
built against, some for two weeks. The rule is the one `docs/feature-plans.md`
adopted for plans on 2026-09-11 — file at the state the merge makes true — and
it is held true by `tests/test_adr_status.py`, which fails the suite — on the
branch as well as on `main` — if a record on disk says `Proposed`. The word is
not written in this repo: an open pull request is the proposal, and the review
on it is the deliberation.

If a decision is genuinely still open, it is not an ADR yet: keep it in the pull
request body or in a plan at `status: draft`, and file the record when the
decision is made. ADR-0010 is the reason this needs a test rather than a
convention: a document binds only if something holds it true.

Writing one: the `write-an-adr` skill in `operator/techne`.
