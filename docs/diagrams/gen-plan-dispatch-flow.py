#!/usr/bin/env python3
"""Plan-dispatch flow, v1.10 — ignition gate, implementation lane, and black-box test lanes.

Publishable by construction: no third-party name, no client name, and no
per-repo enrollment example. What a repo decided is its own business; what the
MECHANISM is can be shown to anyone. Keep it that way — a diagram that has to be
sanitised before it is shown is a diagram nobody shows.
"""
W, H = 1900, 2150
out = []
def e(s): out.append(s)


def x(s):
    """Escape text destined for an SVG text node.

    Without this, a label containing `~/dev/<repo>` is parsed as an element and
    the whole document fails to load — the geometry checker never even runs,
    because there is no tree to check. Cheaper to escape everything than to
    remember which strings are safe."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def box(bx, y, w, h, kind, title, lines=(), note=(), badge=None, act=None):
    """`act` is the ACTOR axis, orthogonal to `kind`'s build state: who performs
    this step. "agent" means a model runs here and its choice is configurable;
    "mech" means the step's guarantees are code and there is no model to choose;
    "human" is a gate. It is drawn because the per-repo model settings are
    exactly the "agent" rows and nothing else — see ADR-0009."""
    e(f'<rect class="{kind}" x="{bx}" y="{y}" width="{w}" height="{h}" rx="8"/>')
    ty = y + 24
    if act:
        cls = {"agent": "actA", "mech": "actM", "human": "actH"}[act]
        txt = {"agent": "AGENT", "mech": "MECHANICAL", "human": "HUMAN"}[act]
        e(f'<text class="{cls}" x="{bx+w-16}" y="{ty}" text-anchor="end">{txt}</text>')
    if badge:
        e(f'<text class="badge {kind}b" x="{bx+16}" y="{ty}">{x(badge)}</text>'); ty += 22
    e(f'<text class="h" x="{bx+16}" y="{ty}">{x(title)}</text>'); ty += 21
    for ln in lines:
        e(f'<text class="b" x="{bx+16}" y="{ty}">{x(ln)}</text>'); ty += 17
    ty += 3
    for ln in note:
        e(f'<text class="n" x="{bx+16}" y="{ty}">{x(ln)}</text>'); ty += 15

def poly(pts, cls="fl"):
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        assert abs(x0-x1) < .01 or abs(y0-y1) < .01, f"not orthogonal: {(x0,y0)}->{(x1,y1)}"
    d = f"M{pts[0][0]},{pts[0][1]}" + "".join(f" L{x},{y}" for x, y in pts[1:])
    e(f'<path class="{cls}" d="{d}"/>')

def label(lx, y, s, cls="n", anchor="start"):
    e(f'<text class="{cls}" x="{lx}" y="{y}" text-anchor="{anchor}">{x(s)}</text>')

e(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
  'font-family="-apple-system, BlinkMacSystemFont, \'Helvetica Neue\', Arial, sans-serif">')
e('''<style>
 .t{font-size:27px;font-weight:700;fill:#1c1c1c}
 .sub{font-size:14px;fill:#5c6670}
 .h{font-size:15px;font-weight:700;fill:#1c1c1c}
 .b{font-size:13px;fill:#222}
 .n{font-size:11.5px;fill:#5c6670;font-style:italic}
 .badge{font-size:9.5px;font-weight:700;letter-spacing:.06em}
 .shipb{fill:#1a7f47}.partb{fill:#a86a00}.todob{fill:#1d4ed8}.gateb{fill:#6b21a8}.testb{fill:#9a3412}
 .zone{fill:#f4f2ee;stroke:#ddd8cf;stroke-width:1.5}
 .ship{fill:#eaf7ef;stroke:#1a7f47;stroke-width:2}
 .part{fill:#fdf3e0;stroke:#a86a00;stroke-width:2}
 .todo{fill:#eaf0ff;stroke:#1d4ed8;stroke-width:2}
 .test{fill:#fdeee6;stroke:#9a3412;stroke-width:2}
 .gate{fill:#f5ecfd;stroke:#6b21a8;stroke-width:2}
 .plain{fill:#ffffff;stroke:#9aa0a6;stroke-width:1.5}
 .panel{fill:#f7f7f9;stroke:#cccccc;stroke-width:1.5}
 .fl{fill:none;stroke:#444;stroke-width:2.2;marker-end:url(#a)}
 .ret{fill:none;stroke:#1d4ed8;stroke-width:2;stroke-dasharray:7 5;marker-end:url(#ab)}
 .bus{fill:none;stroke:#444;stroke-width:2.2}
 .fail{fill:none;stroke:#b91c1c;stroke-width:2;stroke-dasharray:7 5;marker-end:url(#af)}
 .actA{font-size:9.5px;letter-spacing:.09em;font-weight:700;fill:#7c3aed}
 .actM{font-size:9.5px;letter-spacing:.09em;font-weight:700;fill:#6b7280}
 .actH{font-size:9.5px;letter-spacing:.09em;font-weight:700;fill:#a21caf}
</style>
<defs>
 <marker id="a" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
  <path d="M0,0 L10,4 L0,8 z" fill="#444"/></marker>
 <marker id="ab" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
  <path d="M0,0 L10,4 L0,8 z" fill="#1d4ed8"/></marker>
 <marker id="af" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
  <path d="M0,0 L10,4 L0,8 z" fill="#b91c1c"/></marker>
</defs>''')
e(f'<rect class="bg" x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>')

label(950, 52, "Plan dispatch — the gate, the lane, and black-box tests", "t", "middle")
label(950, 76, "Version 1.10 — 2026-09-13 by Fleet Operator", "sub", "middle")
label(60, 46, "Tracing: one feature plan, from draft to merge.", "n")
label(60, 62, "Test lanes are opt-in per repo, default off. Setting the flag is a configuration change.", "n")
label(60, 78, "The implementation lane is built; the test lanes are not.", "n")

# legend
e('<rect class="panel" x="1560" y="96" width="300" height="322" rx="8"/>')
label(1578, 124, "Legend", "h")
ly = 144
for cls, txt in (("ship", "shipped"), ("part", "partly built"),
                 ("todo", "specified, not built"), ("test", "third-party model"),
                 ("gate", "human gate (Minos)")):
    e(f'<rect class="{cls}" x="1578" y="{ly}" width="26" height="15" rx="3"/>')
    label(1614, ly + 12, txt, "b"); ly += 24
poly([(1578, ly + 8), (1604, ly + 8)], "fl"); label(1614, ly + 12, "flow", "b"); ly += 24
poly([(1578, ly + 8), (1604, ly + 8)], "ret"); label(1614, ly + 12, "return path", "b"); ly += 24
poly([(1578, ly + 8), (1604, ly + 8)], "fail"); label(1614, ly + 12, "a run that failed", "b"); ly += 26
label(1578, ly + 12, "AGENT", "b"); label(1640, ly + 12, "a model runs here", "b"); ly += 18
label(1578, ly + 12, "MECHANICAL", "b"); label(1660, ly + 12, "code; no model", "b"); ly += 18
label(1578, ly + 12, "HUMAN", "b"); label(1640, ly + 12, "a person decides", "b")

# --- row 1
box(140, 150, 360, 156, "ship", "① Planning session",
    ["A prompt from any stakeholder. Opus turns", "it into one plan file per feature, opened", "as a plan PR."],
    ["Via intakebot it also routes the architecture,", "security and privacy questions to a human", "BEFORE there is a plan to review."], "SHIPPED", act="agent")
# mopsus sits under the planning session on purpose: a failure re-enters the
# flow the same way new work does — through a human, reachable by intakebot — and
# drawing it anywhere else hides that they are one door.
box(140, 360, 360, 150, "ship", "mopsus — a failure gets a handle",
    ["Composes a brief from the failed run and", "pages it with a handle. Opens a session", "holding the plan, the reason and the log."],
    ["Per repo: escalate (built) | mechanical | triage.", "It decides nothing and acts on nothing."], "SHIPPED", act="mech")
# The failure path: up the left margin, into mopsus, and out to a human.
poly([(140, 1470), (80, 1470), (80, 435), (136, 435)], "fail")
label(88, 1430, "failures", "n")
poly([(320, 360), (320, 314)], "ret")
label(330, 336, "a handle, via intakebot or a page", "n")

e('<rect class="zone" x="580" y="150" width="420" height="180" rx="8"/>')
label(596, 174, "plans/ on a branch", "h")
for i, (nm, sub, strong) in enumerate([
        ("plan.md", "umbrella — never ignites", False),
        ("0015-feat-a.md", "status: ready", True),
        ("0016-feat-b.md", "status: ready", True),
        ("0017-feat-c.md", "status: draft", False)]):
    yy = 186 + i * 34
    hl = ' stroke="#1a7f47" stroke-width="2"' if strong else ''
    e(f'<rect class="plain" x="596" y="{yy}" width="388" height="30" rx="4"{hl}/>')
    label(608, yy + 20, nm, "b"); label(976, yy + 20, sub, "n", "end")
poly([(500, 216), (576, 216)])
box(1080, 176, 360, 96, "gate", "② Minos: merge plan.md",
    ["Documentation only.", "Nothing ignites."], act="human")
poly([(1000, 224), (1076, 224)])

poly([(790, 330), (790, 376)])
box(580, 380, 420, 120, "ship", "revbot reviews the plan PR",
    ["APPROVE, or a blocking question a human", "must answer before anything ignites."], act="agent")
poly([(790, 500), (790, 546)])
box(580, 550, 420, 90, "gate", "③ Minos: merge the feature plan",
    ["Necessary, and no longer sufficient."], act="human")
poly([(790, 640), (790, 686)])
box(580, 690, 420, 168, "todo", "Ignition gate — fleet-watch",
    ["a. APPROVED at the MERGED PR's head SHA", "b. no blocking question left unanswered",
     "c. spec_impact covered by the plan's paths"],
    ["Three checks at once, one message on failure.", "Could-not-tell is never rendered as refused."], "TO BUILD", act="mech")

# split
poly([(790, 858), (790, 906)], "bus")
poly([(350, 906), (1230, 906)], "bus")
poly([(350, 906), (350, 942)])
poly([(1230, 906), (1230, 942)])
label(800, 898, "one plan, two lanes", "n")

# --- implementation lane
box(140, 946, 420, 116, "ship", "④ Implementation lane",
    ["The plan, the repo, a lease, a worktree.", "Opens its own branch and PR."], (), "SHIPPED", act="mech")
poly([(350, 1062), (350, 1108)])
box(140, 1112, 420, 104, "ship", "⑤ Implementation session",
    ["Runner per run, default sub-sonnet. Same", "plan, other runner = a bakeoff arm."], act="agent")
poly([(350, 1216), (350, 1262)])
box(140, 1266, 420, 104, "ship", "⑥ Review — bounded",
    ["Per-repo passes, default 5. Exhausted is", "a hard stop that pages a human."], act="agent")
poly([(140, 1318), (100, 1318), (100, 1164), (136, 1164)], "ret")
label(62, 1247, "fix", "n")
poly([(350, 1370), (350, 1416)])
box(140, 1420, 420, 100, "gate", "Minos: you merge the code PR",
    ["Records which test lanes ran — a PR that", "silently lost them is a false green."], act="human")

# --- test lane
box(1020, 946, 420, 136, "todo", "Repo opted in?",
    ["config/repos.conf, default off — an untouched", "repo discloses nothing.",
     "Setting it either way is a configuration", "change, not a reviewed merge."],
    (), "TO BUILD", act="mech")
poly([(1230, 1082), (1230, 1108)])
box(1020, 1112, 420, 130, "ship", "Bundle builder — first party",
    ["Generated contract only: OpenAPI or --help,", "event names, config keys, harness entry."],
    ["No source. Nothing is present unless code put it there."], "SHIPPED", act="mech")
poly([(1230, 1242), (1230, 1276)], "bus")
poly([(1120, 1276), (1340, 1276)], "bus")
poly([(1120, 1276), (1120, 1312)])
poly([(1340, 1276), (1340, 1312)])
box(1020, 1316, 200, 96, "test", "Antigravity", ["black-box.", "Sees the bundle,", "never the code."], act="agent")
box(1240, 1316, 200, 96, "test", "Codex", ["black-box.", "Same bundle,", "same question."], act="agent")
poly([(1120, 1412), (1120, 1444)], "bus")
poly([(1340, 1412), (1340, 1444)], "bus")
poly([(1120, 1444), (1340, 1444)], "bus")
poly([(1230, 1444), (1230, 1480)])
box(1020, 1484, 420, 130, "todo", "Wrapper opens ONE test PR",
    ["Both suites, plus where the two vendors", "disagreed — and any spec gaps they hit."],
    ["The models hold no token and write nothing."], "TO BUILD", act="mech")
poly([(1230, 1614), (1230, 1660)])
box(1020, 1664, 420, 100, "gate", "Minos: a second human approves",
    ["A second human, who reads the spec and", "the tests and never the implementation."], act="human")

# --- meet once
poly([(350, 1520), (350, 1860), (616, 1860)])
poly([(1230, 1764), (1230, 1860), (984, 1860)])
box(620, 1800, 360, 120, "todo", "They meet exactly once",
    ["The tests run against the code in a", "scratch worktree."],
    ["A failure names both SHAs. Neither side", "adjudicates it alone."], act="mech")
poly([(800, 1920), (800, 1966)])
box(620, 1970, 360, 130, "todo", "Mediator — adjudicates",
    ["Reads the ADR and returns content:", "which side is wrong, and a draft plan."],
    ["Holds no token. The wrapper posts and opens."], act="agent")
poly([(616, 2035), (564, 2035)])
box(140, 1970, 420, 130, "todo", "Follow-up plan PR (draft)",
    ["Spec gaps and non-blocking findings.", "Nothing arms until a human merges it."],
    ["The ADR is the referee: the only artefact both", "sides read and neither wrote."], act="mech")
poly([(140, 2035), (60, 2035), (60, 595), (576, 595)], "ret")
label(72, 2026, "re-enters at ③ — the fleet never arms itself", "n")

# --- panels
e('<rect class="panel" x="1560" y="460" width="300" height="304" rx="8"/>')
label(1578, 488, "Why they never see the code", "h")
for i, s in enumerate(["A tester who reads the source writes", "tests shaped by it. QA works from the", "requirement and the interface.", "",
                       "So the bundle is an allowlist of", "generated artifacts, not the tree with", "things removed. Filtering means", "deciding what to strip and being", "wrong once; generating means nothing", "is there unless code put it there.", "",
                       "Two vendors on one bundle: where they", "disagree, the spec was ambiguous."]):
    label(1578, 514 + i * 18, s, "b")

e('<rect class="panel" x="1560" y="798" width="300" height="250" rx="8"/>')
label(1578, 826, "Where the decision lives", "h")
for i, s in enumerate(["Mechanism and default-off: an ADR", "in eunomia.", "",
                       "Per-repo answer: config/repos.conf,", "read from main. Setting it is a", "configuration change, not a merge.", "",
                       "A refusal: an ADR in the repo", "that refused, so the absence reads", "as decided, not overlooked."]):
    label(1578, 852 + i * 18, s, "b")

e('<rect class="panel" x="1560" y="1082" width="300" height="214" rx="8"/>')
label(1578, 1110, "What is actually built", "h")
for i, s in enumerate(["fleet-watch, the plan format,", "repo enrollment, plan review,", "bin/fleet-bundle and mopsus:", "shipped. Orchestrator: the whole", "wrapper — lease, session, review", "loop, steward, freeze.", "",
                       "The IGNITION GATE and the test", "lane are specified and not built."]):
    label(1578, 1136 + i * 18, s, "b")

e("</svg>")
open(__file__.replace("gen-plan-dispatch-flow.py", "plan-dispatch-flow.svg"), "w").write("\n".join(out))
print("written")
