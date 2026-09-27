# Diagrams — the source of record

`plan-dispatch-flow.svg` is **generated**, not drawn. Edit
`gen-plan-dispatch-flow.py` and re-run it; do not hand-edit the SVG, because the
next generation will silently discard the edit.

```
python3 docs/diagrams/gen-plan-dispatch-flow.py     # writes the .svg beside it
python3 docs/diagrams/check-svg-layout.py docs/diagrams/plan-dispatch-flow.svg
```

Text is escaped by `x()` before it reaches a text node. A label containing
`~/dev/<repo>` is otherwise parsed as an element and the document fails to load
entirely — at which point the geometry checker never runs, because there is no
tree to check. That happened on 2026-09-08; escaping everything is cheaper than
remembering which strings are safe.

The checker is a gate, not a linter's opinion: it fails on connectors that are
not axis-aligned, connectors that cross a box, text past the right edge of its
box, text whose baseline has dropped below its container, and a missing title
block or legend. The generator's `poly()` asserts that consecutive points share
an axis, so a slanted connector is a crash at draw time rather than something to
notice later.

`check-svg-layout.py` lives here rather than beside a `SKILL.md` because
user-level skill directories on this fleet are re-synced and keep only
`SKILL.md` — observed 2026-09-07, which deleted the first copy.

## The second figure: processes and data

`dispatch-dataflow.svg` (v1.0, 2026-09-22) is the companion cut for a reader
who has the first figure's *who decides* and wants *what runs and what it
writes*: every box is a program, every `writes` line is a file, table or forge
object, and the right rail lists each store with its writer and readers.
`docs/dispatch-dataflow.md` is the same content as tables, one row per step and
one per store. Regenerate and gate it the same way:

```
python3 docs/diagrams/gen-dispatch-dataflow.py
python3 docs/diagrams/check-svg-layout.py docs/diagrams/dispatch-dataflow.svg
```

## Two cuts, two audiences

`plan-dispatch-flow.svg` is the reference figure. It is 1900x2150, it carries
every lane and the legend column, and it is the one to argue with.

`plan-dispatch-light.svg` is the cut for readers outside the project — a social
feed, a slide, anywhere the reader is holding a phone. Same generator discipline,
separate script:

```
python3 docs/diagrams/gen-plan-dispatch-light.py
python3 docs/diagrams/check-svg-layout.py docs/diagrams/plan-dispatch-light.svg
```

It keeps the eight-step spine and drops the test lanes, the mediator, the
follow-up loop and the legend column. Three rules make it a different artefact
rather than a smaller one:

- **Plain English, deliberately.** `worktree`, `lease`, `bakeoff arm` and
  `could-not-tell` do not appear. A reader who has never seen this repo has to be
  able to follow it, and a term of art stops them dead.
- **One colour axis.** Colour encodes who acts — agent, mechanical, human — and
  nothing else. The badge repeats it in text, so the figure survives greyscale.
  The reference figure encodes build status too; this one carries that as a
  dashed border on the single box that needs it.
- **4:5 at 1080x1350.** The shape a feed renders without cropping. Verified by
  rasterising to 430px — actual mobile feed width — and reading it, not by
  trusting the checker. The reference figure at that width is a grey rectangle;
  that is why this is a redraw and not a resize.

Shrinking the reference figure is not a substitute for either one. If the spine
changes, both scripts change.

## What the current diagram shows, and what it does not

Version 1.10 depicts the plan-dispatch design as of 2026-09-13: the ignition gate,
the implementation lane, and black-box test lanes. **The left lane is built; the
right one is not.** The legend's colours are the honest part of the picture —
read them before reading the boxes.

| shipped | `fleet-watch`, the plan format, repo enrollment, plan review, `bin/fleet-bundle` and `mopsus`; the orchestrator wrapper entire — lease, heartbeat, worktree, prompt, implementer session (0010), review loop (0011), comment/ack and steward (0031), post-approval freeze (0032). `UNBUILT_PHASES` is empty and `--ready` exits 0 |
| partly built | — |
| specified, not built | the ignition gate (0015), the test lane itself (0016, parked under `docs/plans-need-parsing/`), the meeting step, the mediator. **Not** 0017 — the `extlane` consent flag shipped; what is missing is the reader that makes it fire |

**This section was wrong for three versions, which is the failure the page below
is about.** It said 1.7 while the figure said 1.10, and it listed the bundle
builder as specified-and-not-built after `bin/fleet-bundle` shipped. A reader
checking what exists would have been told the wrong thing by the prose and the
right thing by the picture standing next to it.

Two nodes arrived after 1.7 and are worth naming. **`mopsus`** composes a brief
from a failed run and pages a human with a handle; it decides nothing and acts
on nothing, because a failure handler permitted to retry is a way for the fleet
to make progress nobody authorised. And the planning session now routes the
architecture, security and privacy questions to a human **before** a plan exists
— answering those inside a plan review is too late, because by then the shape is
already argued for.

## Publishable by construction

**The published figure carries no third-party name, no client name, and no
per-repo enrollment example**, and it must stay that way — true of 1.7 through
1.10, and a rule rather than a fact about one version. The diagram shows a
MECHANISM; which repo
opted in, which opted out, and who the second human reviewer is are facts about
this estate, not about how plan dispatch works — and they are the only reason
the picture ever needed a sanitising pass before it could be shown to anyone.

v1.6 named two private repos in the opt-in box, a third in the "Where the
decision lives" panel, and a client by first name on the test-approval gate. The
mechanism reads exactly the same without them: "opting in is a reviewed merge",
"a refusal is an ADR in the repo that refused", "a second human approves".

**v1.7 also drops the pin band.** `~/.local/share/pins/eunomia` and what the
scheduled units exec is deployment mechanics — true, load-bearing, and not part
of the path a plan takes from draft to merge. It belongs with the runbooks; on
this page it was a second diagram sharing a canvas.

**Two things v1.5 asserted that were never true.** Node ④ was labelled
"Implementation lane — Opus"; `DEFAULT_RUNNER` in `bin/orchestrator` has been
`sub-sonnet` since it was written, and ⑤ now carries the default rather than ④
carrying a model it does not use. And the test-lane vendors were drawn as Gemini
and ChatGPT; ADR-0006 measured both on 2026-09-07 and chose **Antigravity**
(`gemini-cli` 0.58.0 returns `UNSUPPORTED_CLIENT` for individuals) and
**Codex**.

## The overflow checks, and what they still miss

The overflow test **was horizontal only** until 2026-09-12. It asked whether a
text run passed a rect's right edge, and only for baselines already inside that
rect's vertical span — so a baseline that had fallen out the bottom skipped the
rect entirely, which is every rect a label has actually overflowed. Six more
lines in the "What is actually built" panel put labels 95px below it and the
checker printed `OK`.

The bottom edge is now checked too, and it needs a containment rule of its own:
the right-edge test can compare every text against every box, but a vertical one
cannot, because a caption deliberately drawn in the whitespace below a node
(`label(800, 898, "one plan, two lanes")`, 40px below the ignition gate) is
geometrically indistinguishable from a label that fell out of it. So a label is
associated with the container its **column** was laid out in: labels come in runs
at a fixed x with a stepping y, each `(x, class)` run is followed down the page,
and a label whose baseline lands in open space still answers to the container its
run started in. `vertical_overflow`'s docstring carries the four cases.

Two things it does not catch, stated here rather than rediscovered:

* **A run with no member inside its container.** Association needs an anchor, so
  a column pushed entirely below its panel (a wrong starting `y` rather than one
  line too many) has nothing to be measured against. Every column in this
  diagram starts with a title or first line inside its panel.
* **The right edge of a container.** Check (3) still reads only non-container
  rects, so a label running off a `panel`'s right edge is not reported. The
  column association added for the bottom edge would extend to it; measured on
  this diagram the tightest label has 8px of headroom, which is inside the error
  of a width estimate that counts characters rather than glyphs, so turning it on
  is its own change with its own measurement.

`tests/test_svg_layout.py` is where this is pinned, and the overflow cases there
assert a failure rather than an `OK` — the whole reason this section exists is
that a gate nobody has seen fail is not known to work.

The decisions behind it are in `docs/adr/0006-blackbox-test-lanes.md` and
`plans/0015-ignition-gate.md`. Where the diagram and an ADR disagree, the ADR
wins and the diagram is stale — say so rather than working around it.
