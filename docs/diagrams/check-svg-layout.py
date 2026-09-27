#!/usr/bin/env python3
"""check-svg-layout — the geometry gate for house-style diagrams.

Checks what a reviewer otherwise catches by eye:
  1. connectors that are not axis-aligned
  2. connectors that cross a box
  3. text that overflows the right edge of the box it sits in
  4. text whose baseline has fallen out the BOTTOM of its container
  5. a missing title block (version + date) or legend

It reports; it does not rewrite. A complaint is either fixed or answered out
loud — "that diagonal is a deliberate accent" is a valid answer, silence is not.
Give a path `class="accent"` to opt it out of (1) and (2).

Lives here rather than beside a SKILL.md because user-level skill directories on
this fleet are re-synced and drop everything but SKILL.md — observed 2026-09-07,
which removed the first copy of this file.
"""
import collections
import re
import sys
import xml.etree.ElementTree as ET

NS = "{http://www.w3.org/2000/svg}"
TOL = 1.5    # px off-axis before a segment counts as slanted
VTOL = 1.0   # px past a container's bottom edge before (4) complains
DESC = 0.22  # of font-size: how far a descender drops below the baseline

# containers, not obstacles: a band or panel is drawn around content on purpose
CONTAINER_CLASSES = ("bg", "lane", "band", "panel", "zone")
# template geometry: an arrowhead is not a connector, a swatch is not a box
SKIP_TAGS = ("defs", "marker", "symbol", "clipPath", "pattern")


def parse_path(d):
    """[(x0,y0,x1,y1), ...] for M/L/H/V/Z; curves counted separately."""
    toks = re.findall(r"([MmLlHhVvCcSsQqZz])([^MmLlHhVvCcSsQqZz]*)", d or "")
    segs, curves = [], 0
    cx = cy = sx = sy = 0.0
    for cmd, raw in toks:
        nums = [float(n) for n in re.findall(r"-?\d*\.?\d+(?:e-?\d+)?", raw)]
        up, rel = cmd.upper(), cmd.islower()
        if up == "M":
            for i in range(0, len(nums) - 1, 2):
                x, y = nums[i], nums[i + 1]
                if rel:
                    x, y = cx + x, cy + y
                if i == 0:
                    cx = sx = x; cy = sy = y
                else:
                    segs.append((cx, cy, x, y)); cx, cy = x, y
        elif up == "L":
            for i in range(0, len(nums) - 1, 2):
                x, y = nums[i], nums[i + 1]
                if rel:
                    x, y = cx + x, cy + y
                segs.append((cx, cy, x, y)); cx, cy = x, y
        elif up == "H":
            for x in nums:
                if rel:
                    x = cx + x
                segs.append((cx, cy, x, cy)); cx = x
        elif up == "V":
            for y in nums:
                if rel:
                    y = cy + y
                segs.append((cx, cy, cx, y)); cy = y
        elif up in ("C", "S", "Q"):
            curves += 1
            if len(nums) >= 2:
                cx, cy = nums[-2], nums[-1]
        elif up == "Z":
            segs.append((cx, cy, sx, sy)); cx, cy = sx, sy
    return segs, curves


def seg_hits_rect(seg, r):
    """Liang-Barsky. A graze along an edge is not a crossing."""
    x0, y0, x1, y1 = seg
    rx, ry, rw, rh = r
    if rw <= 0 or rh <= 0:
        return False
    if rx < x0 < rx + rw and ry < y0 < ry + rh:
        return True
    if rx < x1 < rx + rw and ry < y1 < ry + rh:
        return True
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - rx), (dx, rx + rw - x0), (-dy, y0 - ry), (dy, ry + rh - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                if t > t1: return False
                t0 = max(t0, t)
            else:
                if t < t0: return False
                t1 = min(t1, t)
    return (t1 - t0) * (abs(dx) + abs(dy)) > 2.0


Label = collections.namedtuple("Label", "s tx ty size cls x0 x1")


def measure(el, dx, dy):
    """One text node's geometry, or None for a node with nothing in it.

    The width is an ESTIMATE — 0.52em a character, no font metrics — which is
    why both overflow tests below keep slack. `size` is read from an inline
    `font-size` only; a class-based size in the stylesheet is not resolved, so
    it falls back to 13. That fallback is load-bearing for nothing: the width
    estimate is already crude, and the vertical test spends `size` only on a
    descender allowance of a couple of pixels.
    """
    try:
        tx = float(el.get("x", 0)) + dx
        ty = float(el.get("y", 0)) + dy
    except ValueError:
        return None
    s = "".join(el.itertext())
    if not s.strip():
        return None
    size = 13.0
    m = re.search(r"font-size:\s*([\d.]+)", el.get("style", "") or "")
    if m:
        size = float(m.group(1))
    w = len(s) * size * 0.52
    anchor = el.get("text-anchor") or ""
    if anchor == "middle":
        x0, x1 = tx - w / 2, tx + w / 2
    elif anchor == "end":
        x0, x1 = tx - w, tx
    else:
        x0, x1 = tx, tx + w
    return Label(s, tx, ty, size, el.get("class", "") or "", x0, x1)


def encloses(outer, inner):
    """True when `outer` wholly contains `inner` — a page or band around a panel.

    Strictly larger, so a rect never encloses itself.
    """
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (ox <= ix and oy <= iy and ox + ow >= ix + iw and oy + oh >= iy + ih
            and ow * oh > iw * ih)


def innermost(frames, tx, ty):
    """The tightest rect whose box contains the point, or None.

    Tightest = opened last (largest y), smaller area breaking a tie. Both parts
    matter on a real diagram: the panels are nested inside a full-canvas `bg`,
    and two panels in one column are siblings whose spans do not overlap.
    """
    best = None
    for rx, ry, rw, rh in frames:
        if rw <= 0 or rh <= 0:
            continue
        if not (rx <= tx <= rx + rw and ry <= ty <= ry + rh):
            continue
        if best is None or ry > best[1] or (ry == best[1] and rw * rh < best[2] * best[3]):
            best = (rx, ry, rw, rh)
    return best


def vertical_overflow(labels, frames):
    """Check (4): a label whose baseline has dropped below its container.

    WHY THIS NEEDS ITS OWN CONTAINMENT RULE. Check (3) tests every text against
    every box, which is sound for a right edge — a text crossing one is wrong
    whichever box it is. It cannot work vertically. The obvious form,

        if ry < ty < ry + rh and ty + descender > ry + rh

    can never fire: the guard asks the baseline to be INSIDE the rect, which is
    the one thing an overflowing baseline is not. That was the shipped bug, and
    it meant the measured case — six more lines in the "What is actually built"
    panel, the last label 95px below its bottom edge — printed OK (2026-09-12).

    Dropping the guard is worse than useless. Every label then owes an answer to
    every rect drawn above it anywhere in its column, and a free-floating caption
    in the whitespace between two boxes reads as an overflow of the box above it.
    `label(800, 898, "one plan, two lanes")` in the real diagram is exactly that
    shape, 40px below the ignition gate, deliberately outside it.

    THE RULE. A label belongs to the container its COLUMN was laid out in. Labels
    are emitted in runs at a fixed x with a stepping y — by a generator here, by
    hand everywhere else — so each (x, class) pair is followed down the page and
    carries the frame its earlier members sat inside:

      * baseline inside some frame -> that frame owns the label, and becomes the
        column's frame. A label cannot overflow the bottom of a box it is in.
      * baseline in a frame that merely ENCLOSES the column's frame (the page, a
        band) -> the label has fallen out of its container into open space, and
        the column's frame still owns it. This is the reported case, and it keeps
        reporting for the rest of the run rather than only its first line.
      * baseline in some other frame -> the column has moved on to a new
        container (the next panel down the page). Re-home it, report nothing.
      * no column and no frame -> a free-floating label. Nothing owns it.

    KNOWN LIMIT, stated rather than discovered later: a label that overflows one
    container and lands inside an unrelated one below is owned by where it
    landed, so it is not reported. It is a text-in-the-wrong-box defect, which
    this geometry cannot separate from a label legitimately drawn there.
    """
    problems, column = [], {}
    for t in sorted(labels, key=lambda t: (t.ty, t.tx)):
        key = (round(t.tx), t.cls)
        home = innermost(frames, t.tx, t.ty)
        owner = column.get(key)
        if owner is not None and home is not None and not encloses(home, owner):
            owner = home            # a different container, not open space
        if owner is None:
            owner = home
        if owner is None:
            continue
        column[key] = owner
        depth = t.ty + t.size * DESC - (owner[1] + owner[3])
        if depth > VTOL:
            problems.append(("text overflows the bottom of its container",
                             f"{t.s[:44]!r} ~{depth:.0f}px below "
                             f"({owner[0]:.0f},{owner[1]:.0f}) {owner[2]:.0f}x{owner[3]:.0f}"))
    return problems


def collect(root):
    """Depth-first with accumulated translate(); template subtrees skipped."""
    out = []

    def walk(el, dx, dy):
        tag = el.tag.replace(NS, "")
        if tag in SKIP_TAGS:
            return
        m = re.search(r"translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)", el.get("transform", "") or "")
        if m:
            dx += float(m.group(1)); dy += float(m.group(2))
        out.append((tag, el, dx, dy))
        for child in el:
            walk(child, dx, dy)

    walk(root, 0.0, 0.0)
    return out


def main(path):
    root = ET.parse(path).getroot()
    # `rects` are obstacles for (2) and (3); `frames` is every rect, because a
    # container is the one thing a label CAN overflow the bottom of — see (4).
    rects, frames, texts, paths = [], [], [], []
    for tag, el, dx, dy in collect(root):
        cls = el.get("class", "") or ""
        if tag == "rect":
            try:
                r = (float(el.get("x", 0)) + dx, float(el.get("y", 0)) + dy,
                     float(el.get("width", 0)), float(el.get("height", 0)))
            except ValueError:
                continue
            frames.append(r)
            if any(k in cls for k in CONTAINER_CLASSES):
                continue
            rects.append(r)
        elif tag == "text":
            texts.append((el, dx, dy))
        elif tag == "path":
            paths.append((el.get("d", ""), cls, dx, dy))

    problems = []
    for d, cls, dx, dy in paths:
        if "accent" in cls:
            continue
        segs, curves = parse_path(d)
        for a, b, c, e in ((s[0] + dx, s[1] + dy, s[2] + dx, s[3] + dy) for s in segs):
            if abs(a - c) > TOL and abs(b - e) > TOL:
                problems.append(("slanted connector",
                                 f"({a:.0f},{b:.0f})->({c:.0f},{e:.0f}) class={cls or '-'}"))
            for r in rects:
                if seg_hits_rect((a, b, c, e), r):
                    problems.append(("line crosses a box",
                                     f"({a:.0f},{b:.0f})->({c:.0f},{e:.0f}) through "
                                     f"({r[0]:.0f},{r[1]:.0f}) {r[2]:.0f}x{r[3]:.0f}"))
                    break
        if curves:
            problems.append(("curve, check by eye", f"{curves} segment(s) class={cls or '-'}"))

    labels = [lb for lb in (measure(el, dx, dy) for el, dx, dy in texts) if lb]

    for t in labels:
        for rx, ry, rw, rh in rects:
            if ry < t.ty < ry + rh and rx <= t.x0 <= rx + rw and t.x1 > rx + rw + 4:
                problems.append(("text overflows its box",
                                 f"{t.s[:44]!r} ~{t.x1 - (rx + rw):.0f}px past ({rx:.0f},{ry:.0f})"))
                break

    problems += vertical_overflow(labels, frames)

    body = " ".join("".join(t.itertext()) for t, _, _ in texts).lower()
    if not re.search(r"\bv(?:ersion)?\s*\d|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", body):
        problems.append(("no title block", "expected a version and a date"))
    if "legend" not in body:
        problems.append(("no legend", "add one, or say why the vocabulary is self-evident"))

    if not problems:
        print(f"{path}: OK")
        return 0
    counts = {}
    for kind, _ in problems:
        counts[kind] = counts.get(kind, 0) + 1
    print(f"{path}: {len(problems)} problem(s)")
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"\n  {kind} - {n}")
        shown = [d for k, d in problems if k == kind][:6]
        for d in shown:
            print(f"    {d}")
        if n > len(shown):
            print(f"    ... and {n - len(shown)} more")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: check-svg-layout.py <file.svg>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
