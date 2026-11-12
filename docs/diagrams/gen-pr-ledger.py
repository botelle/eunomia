#!/usr/bin/env python3
"""Draw the two plan-0209 redesign diagrams:

  pr-ledger-architecture.svg  what runs where, and the path of one PR event
  pr-ledger-sample-fleet.svg  a sample fleet, and what fleet-watch does with each PR

House style: docs/diagrams/check-svg-layout.py must pass on both.
"""
from pathlib import Path
from xml.sax.saxutils import escape

VERSION = "0.2 DRAFT"
DATE = "2026-11-05"
AUTHOR = "Fleet Operator"
OUT = Path(__file__).resolve().parent

FONT = "-apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif"
STYLE = """
 .t{font-size:34px;font-weight:700;fill:#1c1c1c}
 .st{font-size:20px;fill:#5c6670}
 .meta{font-size:18px;fill:#8a9099}
 .h{font-size:21px;font-weight:700;fill:#1c1c1c}
 .b{font-size:17px;fill:#3d454d}
 .host{font-size:20px;font-weight:700;fill:#5c6670}
 .lbl{font-size:16px;fill:#3d454d}
 .num{font-size:16px;font-weight:700;fill:#9a3412}
 .lg{font-size:17px;fill:#3d454d}
 .note{font-size:17px;fill:#8a9099;font-style:italic}
 .col{font-size:18px;font-weight:700;fill:#5c6670}
"""

FORGE = ("#1d4ed8", "#eef2fd")
SERVICE = ("#047857", "#ecfdf5")
STORE = ("#b45309", "#fffbeb")
GREY = ("#8a9099", "#ffffff")
# sample-fleet outcome kinds (one axis for that diagram)
SKIP = ("#57606a", "#f3f4f6")
HUMAN = ("#6b21a8", "#f7f0fc")
SPAWN = ("#047857", "#ecfdf5")
REPAIR = ("#be123c", "#fff1f2")
NEUTRAL = ("#8a9099", "#ffffff")


class Svg:
    def __init__(self, w, h):
        self.w, self.h, self.o = w, h, []

    def add(self, s):
        self.o.append(s)

    def head(self, title, subtitle):
        self.add(f'<rect class="bg" width="{self.w}" height="{self.h}" fill="#ffffff"/>')
        self.add(f'<text class="t" x="40" y="56">{escape(title)}</text>')
        self.add(f'<text class="st" x="40" y="90">{escape(subtitle)}</text>')
        self.add(f'<text class="meta" x="40" y="118">version {VERSION} · {DATE} · '
                 f'{AUTHOR} · plan 0209 redesign</text>')

    def box(self, x, y, w, h, color, lines, dashed=False, cls=""):
        stroke, fill = color
        dash = ' stroke-dasharray="8 6"' if dashed else ""
        c = f' class="{cls}"' if cls else ""
        self.add(f'<rect{c} x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
                 f'fill="{fill}" stroke="{stroke}" stroke-width="2.5"{dash}/>')
        n = len(lines)
        first = lines[0]
        rest = lines[1:]
        block = 24 + 22 * len(rest)
        ty = y + (h - block) / 2 + 19
        self.add(f'<text class="h" x="{x + w / 2}" y="{ty:.0f}" '
                 f'text-anchor="middle">{escape(first)}</text>')
        for i, line in enumerate(rest):
            self.add(f'<text class="b" x="{x + w / 2}" y="{ty + 24 + 22 * i:.0f}" '
                     f'text-anchor="middle">{escape(line)}</text>')
        return n

    def panel(self, x, y, w, h, label):
        self.add(f'<rect class="panel" x="{x}" y="{y}" width="{w}" height="{h}" rx="14" '
                 f'fill="#fafafa" stroke="#d0d5da" stroke-width="2"/>')
        self.add(f'<text class="host" x="{x + 20}" y="{y + 32}">{escape(label)}</text>')

    def path(self, pts, color="#8a9099", dashed=False, arrow="g"):
        d = "M%d %d" % pts[0] + "".join(" L%d %d" % p for p in pts[1:])
        dash = ' stroke-dasharray="7 6"' if dashed else ""
        self.add(f'<path d="{d}" stroke="{color}" stroke-width="2.5" fill="none"{dash} '
                 f'marker-end="url(#a{arrow})"/>')

    def text(self, x, y, s, cls="lbl", anchor="start"):
        self.add(f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{escape(s)}</text>')

    def legend(self, x, y, w, rows, line_rows=()):
        h = 46 + 30 * (len(rows) + len(line_rows))
        self.add(f'<rect class="panel" x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
                 f'fill="#ffffff" stroke="#d0d5da" stroke-width="2"/>')
        self.text(x + 18, y + 30, "Legend", "lg")
        self.o[-1] = self.o[-1].replace('class="lg"', 'class="lg" font-weight="700"')
        yy = y + 58
        for color, label, dashed in rows:
            dash = ' stroke-dasharray="6 4"' if dashed else ""
            self.add(f'<rect x="{x + 18}" y="{yy - 15}" width="34" height="20" rx="4" '
                     f'fill="{color[1]}" stroke="{color[0]}" stroke-width="2"{dash}/>')
            self.text(x + 64, yy, label, "lg")
            yy += 30
        for color, label, dashed in line_rows:
            dash = ' stroke-dasharray="7 6"' if dashed else ""
            self.add(f'<path class="legend-swatch" d="M{x + 18} {yy - 6} L{x + 52} {yy - 6}" '
                     f'stroke="{color}" stroke-width="2.5"{dash}/>')
            self.text(x + 64, yy, label, "lg")
            yy += 30

    def render(self, path):
        defs = ('<defs>'
                '<marker id="ag" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="10" '
                'markerHeight="8" orient="auto-start-reverse"><path d="M0 0 L10 4 L0 8 z" '
                'fill="#8a9099"/></marker>'
                '<marker id="ao" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="10" '
                'markerHeight="8" orient="auto-start-reverse"><path d="M0 0 L10 4 L0 8 z" '
                'fill="#9a3412"/></marker></defs>')
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
               f'viewBox="0 0 {self.w} {self.h}" font-family="{FONT}">'
               f'<style>{STYLE}</style>{defs}' + "".join(self.o) + "</svg>\n")
        path.write_text(svg)


def architecture():
    s = Svg(1600, 1130)
    s.head("PR ledger fed by webhooks — what runs where",
           "One PR event, from Forgejo to a dispatch decision. The ledger is fast; the search makes it safe.")

    s.panel(40, 160, 440, 720, "cihost (macOS) — Forgejo 15")
    s.panel(560, 160, 1000, 720, "opshost (macOS) — the fleet")

    s.box(80, 220, 360, 110, FORGE, ["Forgejo repos", "operator/* — PRs, reviews,", "plans/ on main"])
    s.box(80, 400, 360, 110, FORGE, ["Repo webhook (new)", "pull_request + push", "→ opshost :8798, HMAC"], dashed=True)
    s.box(80, 600, 360, 140, FORGE, ["REST API :3001", "GET /pulls/{n}", "GET /issues?q=<plan id>", "GET /pulls (full list)"])

    s.box(620, 220, 400, 120, SERVICE, ["fleet-prhook :8798", "checks HMAC; the payload", "is a trigger, never a fact"], dashed=True)
    s.box(620, 420, 400, 110, STORE, ["fleet.db — pr_ledger", "one row per PR, any state", "plus last event time"], dashed=True)
    s.box(620, 640, 400, 140, SERVICE, ["Backstops", "catch-up walk every 10 min", "daily full listing", "missed event → logged"], dashed=True)

    s.box(1120, 220, 400, 120, SERVICE, ["fleet-watch --resident", "sleeps 120 s between", "cycles (cycle: 10–20 min)"])
    s.box(1120, 420, 400, 110, SERVICE, ["Dispatch gate", "canary search, then", "“Plan: <id>” must be absent"], dashed=True)
    s.box(1120, 640, 280, 90, SERVICE, ["orchestrator", "spawned for the plan"])

    # 0: forge emits the event
    s.path([(260, 330), (260, 400)])
    s.text(275, 370, "PR opened / edited /")
    s.text(275, 390, "closed / merged")
    # 1: delivery
    s.path([(440, 455), (520, 455), (520, 260), (620, 260)])
    s.text(532, 250, "1", "num")
    s.text(548, 250, "delivery")
    # 2: re-read the PR (payload is only a trigger)
    s.path([(620, 310), (540, 310), (540, 650), (440, 650)])
    s.text(552, 566, "2", "num")
    s.text(552, 588, "GET")
    s.text(552, 610, "/pulls/{n}")
    # 3: upsert
    s.path([(820, 340), (820, 420)])
    s.text(834, 386, "3", "num")
    s.text(850, 386, "upsert row")
    # 5: dedupe reads the ledger, not the forge
    s.path([(1120, 320), (1070, 320), (1070, 475), (1020, 475)])
    s.text(1080, 372, "4", "num")
    s.text(1096, 372, "read")
    s.text(1080, 394, "ledger each cycle")
    # 6: ledger says none
    s.path([(1320, 340), (1320, 420)])
    s.text(1334, 386, "5", "num")
    s.text(1350, 386, "if about to dispatch")
    # 7: the search confirms (orange: the dispatch-safety path)
    s.path([(1480, 530), (1480, 840), (260, 840), (260, 740)], color="#9a3412", arrow="o")
    s.text(700, 830, "6", "num")
    s.text(716, 830, "search “Plan: <id>” — one call, ~50 ms; any hit = no spawn")
    # 8: spawn
    s.path([(1260, 530), (1260, 640)], color="#9a3412", arrow="o")
    s.text(1274, 596, "7", "num")
    s.text(1290, 596, "none: spawn")
    # backstops
    s.path([(620, 700), (440, 700)], dashed=True)
    s.text(452, 690, "list")
    s.path([(820, 640), (820, 530)], dashed=True)
    s.text(834, 596, "repair")

    s.legend(40, 920, 760, [
        (FORGE, "Forgejo on cihost", False),
        (SERVICE, "fleet process on opshost", False),
        (STORE, "data in fleet.db", False),
        (GREY, "dashed box = proposed, not built", True),
    ])
    s.legend(840, 920, 720, [], [
        ("#8a9099", "event or read path", False),
        ("#9a3412", "dispatch-safety path (search, then spawn)", False),
        ("#8a9099", "backstop read (rare)", True),
    ])
    s.render(OUT / "pr-ledger-architecture.svg")


ROWS = [
    (["0301-alpha", "ready on main"], ["PR #501 merged", "Plan: 0301-alpha"],
     ["#501 closed, merged", "from webhook: closed"], ["marked PR exists"],
     SKIP, ["skip: done", "meeting sweep runs"]),
    (["0302-beta", "ready on main"], ["PR #502 open", "orchestrator alive"],
     ["#502 open", "from webhook: opened"], ["marked PR exists", "(in flight)"],
     SKIP, ["skip; overlap check", "holds plans on its files"]),
    (["0303-gamma", "ready on main"], ["PR #503 open", "orchestrator died"],
     ["#503 open"], ["marked PR exists", "its lease is dead"],
     HUMAN, ["orphan sweep pages", "respawn on your word"]),
    (["0304-delta", "ready on main"], ["PR #498 closed,", "not merged (rejected)"],
     ["#498 closed"], ["marked PR exists"],
     HUMAN, ["never retried", "you re-dispatch it"]),
    (["0305-epsilon", "ready, holds met"], ["no PR"],
     ["no row"], ["ledger: none", "search: none"],
     SPAWN, ["spawn orchestrator"]),
    (["0306-zeta", "ready, holds met"], ["PR #510 open", "(receiver was down)"],
     ["no row", "(delivery missed)"], ["ledger: none", "search finds #510"],
     REPAIR, ["no spawn", "row repaired, logged"]),
    (["0307-eta", "ready, dispatched"], ["branch feat/0307", "pushed, no PR yet"],
     ["no row"], ["dispatch lease held"],
     SKIP, ["skip: in flight;", "fleet-orphans names", "a stale branch daily"]),
]


def sample_fleet():
    cols = [(40, 250), (330, 270), (640, 270), (950, 270), (1260, 300)]
    heads = ["Plan on main", "What Forgejo holds", "pr_ledger row", "Dedupe answer", "fleet-watch does"]
    top, rh, gap = 200, 104, 28
    h = top + len(ROWS) * (rh + gap) + 230
    s = Svg(1600, h)
    s.head("PR ledger — how a sample fleet is read",
           "Seven plans, each in a different state. Read across a row: plan, forge, ledger, answer, action.")
    for (x, w), head in zip(cols, heads):
        s.text(x + w / 2, top - 22, head, "col", "middle")
    for i, (plan, forge, ledger, answer, kind, action) in enumerate(ROWS):
        y = top + i * (rh + gap)
        cells = [(plan, NEUTRAL), (forge, FORGE), (ledger, STORE), (answer, NEUTRAL), (action, kind)]
        for (x, w), (lines, color) in zip(cols, cells):
            s.box(x, y, w, rh, color, lines)
        for (x1, w1), (x2, _w2) in zip(cols, cols[1:]):
            s.path([(x1 + w1, y + rh / 2), (x2, y + rh / 2)])
    ly = top + len(ROWS) * (rh + gap) + 10
    s.legend(40, ly, 700, [
        (FORGE, "Forgejo state", False),
        (STORE, "ledger row (from webhooks)", False),
        (SKIP, "action: skip, nothing to do", False),
    ])
    s.legend(780, ly, 780, [
        (HUMAN, "action: waits for you", False),
        (SPAWN, "action: dispatch", False),
        (REPAIR, "action: missed event caught, repaired", False),
    ])
    s.render(OUT / "pr-ledger-sample-fleet.svg")


if __name__ == "__main__":
    architecture()
    sample_fleet()
