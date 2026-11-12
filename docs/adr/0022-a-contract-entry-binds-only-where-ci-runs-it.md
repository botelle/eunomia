# ADR-0022 — A cross-repo contract entry binds only in a repository whose contract CI runs

- **Status:** Accepted
- **Date:** 2026-10-07
- **Deciders:** the operator (owner/merger)
- **Amended 2026-10-18 by the owner:** §4–§7 grade missing evidence by the kind of file
  it is missing from. §1–§3 are unchanged.
- **Relates:** `plans/0097-ci-runs-talos-calls.md` (the job that runs talos's contract);
  talos plans 0016, 0017 and 0023 (the contract, the reviewer's cross-repo rule, and its
  narrowing); [ADR-0010](0010-a-document-binds-only-if-something-holds-it-true.md).

## Context

A repository that runs another repository's programs can list those calls in a
contract file, `contracts/eunomia-calls.json`. eunomia's CI runs talos's file against
every eunomia change, so eunomia cannot merge something that breaks talos.

The reviewer's rule (talos plan 0017) asked every repository for such an entry. Only
talos's file is run. On 2026-10-06 that rule stopped three fleet runs on a file nothing
would read (lynceus#106, lynceus#107, hosttools#40).

## Decision

**1. A contract entry is evidence only where CI runs the contract.** Today that is
`operator/talos`. A contract file in any other repository is run by nothing and counts
for nothing.

**2. Everywhere else, a call into another repository is evidenced by a comment at the
call site,** naming the repository, the path, and the commit or pull request on that
side that provides it.

**3. A repository joins the first group only in the change that makes CI run its
contract.** Never by adding the file.

The reason: a contract nothing runs is a hidden failure point. It reads as a guarantee,
and it is checked by no one (ADR-0010).

*(§4–§7 amended by the owner, 2026-10-18. Before this, every unevidenced cross-repo
reference was HIGH in every file. On techne#112 and #114 that held a skill document
across three rounds, as each round found the next uncited reference. A commit cited in
prose catches no drift, so §1–§3's reasoning, that only a running check is evidence,
is what grades documents below code.)*

**4. Code.** A cross-repo call in a file that executes needs the evidence of §1–§3, and
its absence is HIGH. A file that executes is source, a script, a hook, a CI workflow, a
launchd or systemd unit, configuration a program reads, a test, or a file a program
parses (a schema, a contract).

**5. Instructions.** A command that a document tells the reader to run, in a fenced
shell block or an inline span the text says to run, needs no cited commit. Its missing
evidence is LOW. For a command that spends money, deletes or overwrites data, or reads,
writes or sends a credential, missing evidence is MEDIUM, unless a CI check verifies
that the flags and paths the command names exist. It is never HIGH.

**6. Prose.** Everything else in a document carries no evidence requirement, and the
absence of evidence is not a finding.

**7. Wrong is not the same as unevidenced.** §4–§6 grade missing evidence only. A
command or claim that is wrong against the code under review is a correctness finding,
at the severity its consequence earns, in any kind of file.

## Consequences

- talos's reviewer names the contract repositories in a constant (`CONTRACT_REPOS`,
  talos plan 0023) and words its rule from it.
- `file-a-plan` asks a plan to lease `contracts/eunomia-calls.json` only in a contract
  repository.
- An unevidenced cross-repo call in code is still HIGH in every repository. Only the
  form of the evidence differs. In a document it is LOW or MEDIUM (§5), and a wrong
  command is graded as a defect (§7).
- talos's reviewer words its evidence paragraph by file kind (talos plan 0030), and
  techne's CI checks the commands its skills name (techne plan 0001).
