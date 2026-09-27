#!/usr/bin/env python3
"""The light cut of the plan-dispatch flow — for readers outside the project.

`gen-plan-dispatch-flow.py` is the reference figure: every lane, every gate, the
legend column, 1900x2150. It is correct and it is unreadable on a phone.

This is the other audience. It keeps the eight-step spine and drops the test
lanes, the mediator, the follow-up loop and the legend column. Labels are plain
English on purpose: a reader who has never seen this repo should be able to
follow it, so `worktree`, `lease` and `bakeoff arm` do not appear.

Colour carries exactly one axis - who acts - and the badge repeats it in text so
the figure survives greyscale. A dashed border means specified, not yet built.

4:5 portrait at 1080x1350, which is the shape a social feed renders without
cropping; verified legible rasterised to 430px, actual mobile feed width.

    python3 gen-plan-dispatch-light.py
    python3 check-svg-layout.py plan-dispatch-light.svg
"""

VERSION = "1.10-lite"
DATE = "2026-09-14"
AUTHOR = "Fleet Operator"

W, H = 1080, 1350
BX, BW = 56, 864
BRIGHT = BX + BW
GUT = 968                        # return-path gutter, right of every box
Y0, BH, GAP = 200, 100, 40

AGENT = ("#1d4ed8", "#eef2fd", "AGENT")
MECH  = ("#4a5560", "#f2f3f4", "MECHANICAL")
HUMAN = ("#6b21a8", "#f7f0fc", "HUMAN")

# (actor, headline, one line of plain English, specified-but-not-built)
STEPS = [
    (AGENT, "① Planning session",
     "A request becomes one written plan per feature.", False),
    (AGENT, "② Plan review",
     "A second model approves it, or raises a blocking question.", False),
    (HUMAN, "③ A person approves the plan",
     "No work starts until someone signs off.", False),
    (MECH,  "④ Ignition gate",
     "Automated checks. Ambiguity never counts as approval.", True),
    (MECH,  "⑤ Work starts, isolated",
     "Each feature runs in its own workspace, so work cannot collide.", False),
    (AGENT, "⑥ Implementation",
     "A model writes the code against the approved plan.", False),
    (AGENT, "⑦ Review, with a hard limit",
     "Five attempts, then it stops and hands it to a person.", False),
    (HUMAN, "⑧ A person merges the code",
     "Nothing here ships without someone approving it.", False),
]

TITLE = "Plan dispatch — how a feature ships"
SUBTITLE = "One plan, from a request to a merge. Two gates need a person, and nothing skips them."
FOOTNOTE = "Dashed = specified, not yet built. Everything else is running."


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build():
    o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
         'font-family="-apple-system, BlinkMacSystemFont, \'Helvetica Neue\', Arial, sans-serif">'
         % (W, H, W, H)]
    o.append('''<style>
 .t{font-size:39px;font-weight:700;fill:#1c1c1c}
 .st{font-size:23px;fill:#5c6670}
 .meta{font-size:21px;fill:#8a9099}
 .h{font-size:34px;font-weight:700;fill:#1c1c1c}
 .b{font-size:24px;fill:#3d454d}
 .badge{font-size:16px;font-weight:700;letter-spacing:.09em}
 .lg{font-size:20px;fill:#3d454d}
 .fix{font-size:25px;font-weight:700;fill:#9a3412}
 .note{font-size:19px;fill:#8a9099;font-style:italic}
</style>''')
    o.append('<defs>'
             '<marker id="a" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="9" markerHeight="7" '
             'orient="auto-start-reverse"><path d="M0 0 L10 4 L0 8 z" fill="#8a9099"/></marker>'
             '<marker id="af" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="9" markerHeight="7" '
             'orient="auto-start-reverse"><path d="M0 0 L10 4 L0 8 z" fill="#9a3412"/></marker>'
             '</defs>')
    # class="bg" so the layout checker treats it as a container, not an obstacle
    o.append('<rect class="bg" width="%d" height="%d" fill="#ffffff"/>' % (W, H))

    # title block: name, version, date, author
    o.append('<text class="t" x="%d" y="62">%s</text>' % (BX, esc(TITLE)))
    o.append('<text class="st" x="%d" y="96">%s</text>' % (BX, esc(SUBTITLE)))
    o.append('<text class="meta" x="%d" y="126">v%s · %s · %s</text>' % (BX, VERSION, DATE, AUTHOR))

    # legend: one axis only - who acts
    o.append('<text class="lg" x="%d" y="168" font-weight="700">Legend</text>' % BX)
    lx = BX + 92
    for col, fill, name in (AGENT, MECH, HUMAN):
        o.append('<rect x="%d" y="150" width="22" height="22" rx="4" fill="%s" stroke="%s" '
                 'stroke-width="3"/>' % (lx, fill, col))
        o.append('<text class="lg" x="%d" y="168">%s</text>' % (lx + 32, name.title()))
        lx += 32 + len(name) * 11 + 46

    for i, (kind, head, sub, dashed) in enumerate(STEPS):
        col, fill, badge = kind
        y = Y0 + i * (BH + GAP)
        dash = ' stroke-dasharray="10 7"' if dashed else ''
        o.append('<rect x="%d" y="%d" width="%d" height="%d" rx="10" fill="%s" stroke="%s" '
                 'stroke-width="3"%s/>' % (BX, y, BW, BH, fill, col, dash))
        o.append('<text class="badge" x="%d" y="%d" fill="%s">%s</text>' % (BX + 26, y + 29, col, badge))
        o.append('<text class="h" x="%d" y="%d">%s</text>' % (BX + 26, y + 63, esc(head)))
        o.append('<text class="b" x="%d" y="%d">%s</text>' % (BX + 26, y + 90, esc(sub)))
        if i < len(STEPS) - 1:
            cx = BX + BW // 2
            o.append('<path d="M%d %d V%d" stroke="#8a9099" stroke-width="2.5" fill="none" '
                     'marker-end="url(#a)"/>' % (cx, y + BH, y + BH + GAP - 2))

    # bounded review returns to implementation: orthogonal, routed in the gutter
    y6 = Y0 + 5 * (BH + GAP) + BH // 2
    y7 = Y0 + 6 * (BH + GAP) + BH // 2
    o.append('<path d="M%d %d H%d V%d H%d" stroke="#9a3412" stroke-width="2.5" fill="none" '
             'marker-end="url(#af)"/>' % (BRIGHT, y7, GUT, y6, BRIGHT + 2))
    o.append('<text class="fix" x="%d" y="%d">fix</text>' % (GUT + 12, (y6 + y7) // 2 + 9))

    o.append('<text class="note" x="%d" y="%d">%s</text>'
             % (BX, Y0 + 8 * (BH + GAP) + 4, esc(FOOTNOTE)))
    o.append('</svg>')
    return "\n".join(o)


if __name__ == "__main__":
    import os
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan-dispatch-light.svg")
    with open(out, "w") as f:
        f.write(build())
    print("wrote", out)
