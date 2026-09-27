"""The geometry gate discriminates — it does not merely print OK.

`docs/diagrams/check-svg-layout.py` is the gate for a GENERATED diagram: the
README says so in those words, and nobody re-reads 1900x2470 pixels of SVG by
eye after editing the generator. A gate nothing has ever been caught by is
indistinguishable from a gate that cannot catch anything, and on 2026-09-12 the
overflow check turned out to be the second kind. Adding six lines to the "What
is actually built" panel pushed its labels 95px past the panel's bottom edge and
the checker printed `OK`, because the only overflow test it had compared right
edges and skipped any rect whose vertical span did not already contain the
baseline — which is every rect a label has fallen out of.

So the tests that matter here are the ones that FAIL on a bad file.
`test_the_real_diagram_passes` is the cheap half and proves nothing on its own:
a checker with every test deleted passes it too. Each of the four overflow cases
below therefore asserts a return code of 1 AND the wording of the complaint, and
the two false-positive cases assert 0 on geometry that is deliberately outside a
box — a caption in the whitespace between two nodes, and a column of labels that
has moved on to the next panel down the page. Those two are what a naive fix
breaks, which is why they are pinned here rather than left to the reviewer.
"""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIAGRAMS = ROOT / "docs" / "diagrams"
CHECKER = DIAGRAMS / "check-svg-layout.py"
GENERATOR = DIAGRAMS / "gen-plan-dispatch-flow.py"
DIAGRAM = DIAGRAMS / "plan-dispatch-flow.svg"

VERTICAL = "text overflows the bottom of its container"
HORIZONTAL = "text overflows its box"


def _checker():
    """Load the checker by path: its filename is hyphenated, so `import` cannot."""
    spec = importlib.util.spec_from_file_location("check_svg_layout", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(tmp_path, body, name="t.svg"):
    """Check one hand-built SVG; returns (returncode, stdout)."""
    p = tmp_path / name
    p.write_text(_svg(body))
    mod = _checker()
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(str(p))
    return rc, buf.getvalue()


def _svg(body):
    """Wrap a fragment with the two things every house diagram must carry.

    Without the version/date line and the word "legend" the checker reports two
    unrelated problems, and a test asserting a returncode of 1 would pass with
    the overflow check deleted.
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 900" '
        'width="900" height="900">\n'
        '<rect class="bg" x="0" y="0" width="900" height="900"/>\n'
        '<text x="20" y="30">Version 1.0 - 2026-09-12</text>\n'
        '<text x="20" y="50">Legend: boxes are nodes</text>\n'
        + body + "\n</svg>\n"
    )


def test_the_real_diagram_passes():
    """The committed diagram is clean — the cheap half, and not the point."""
    mod = _checker()
    assert mod.main(str(DIAGRAM)) == 0


def test_six_more_lines_in_the_built_panel_is_reported(tmp_path):
    """The measured case of 2026-09-12, reproduced through the real generator.

    Not a hand-built fragment: the gap was found by editing this generator, and
    the panel it was found in is 300x214 with its label column stepping 18px, so
    the arithmetic that has to be caught is the generator's own.
    """
    src = GENERATOR.read_text()
    # Re-pointed for v1.8, which reworded this panel's last label. The anchor is
    # the panel's FINAL label column, and it moves whenever that panel is
    # reworded — which is why the assertion below tells the next person exactly
    # what to do instead of just failing.
    anchor = '"lane are specified and not built."]'
    assert anchor in src, (
        f"{GENERATOR.name} no longer contains {anchor!r}, so this test is no "
        "longer growing the 'What is actually built' panel. Re-point it at "
        "whatever label column that panel now ends with."
    )
    extra = ", ".join(f'"overflow line {i}"' for i in range(1, 7))
    grown = tmp_path / GENERATOR.name
    grown.write_text(src.replace(anchor, f'"lane are specified and not built.", {extra}]'))

    out = subprocess.run([sys.executable, str(grown)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    svg = tmp_path / DIAGRAM.name
    assert svg.exists()

    mod = _checker()
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(str(svg))
    report = buf.getvalue()
    assert rc == 1, f"six lines past the panel's bottom edge reported clean:\n{report}"
    assert VERTICAL in report, report
    # every line of the run, not just the first one to leave the panel
    assert report.count("overflow line") == 6, report
    assert "(1560,1082) 300x214" in report, report


def test_a_label_past_the_panels_bottom_edge_is_reported(tmp_path):
    rc, out = _run(tmp_path, """
<rect class="panel" x="100" y="100" width="300" height="100"/>
<text x="120" y="130">inside the panel</text>
<text x="120" y="150">also inside</text>
<text x="120" y="240">this one is not</text>
""")
    assert rc == 1, out
    assert VERTICAL in out, out
    assert "'this one is not'" in out, out


def test_a_label_that_falls_out_of_a_box_into_its_band_is_reported(tmp_path):
    """Landing inside the band that ENCLOSES the box is still an overflow.

    This is the case that makes the containment rule earn its place: the
    baseline is inside a rect (the band), so any rule that stops at "is the
    baseline in some rect" calls this clean.
    """
    rc, out = _run(tmp_path, """
<rect class="zone" x="60" y="100" width="700" height="400"/>
<rect class="ship" x="100" y="140" width="300" height="100"/>
<text x="120" y="170">in the box</text>
<text x="120" y="300">out of the box, still in the zone</text>
""")
    assert rc == 1, out
    assert VERTICAL in out, out
    assert "(100,140) 300x100" in out, out


def test_a_caption_in_the_whitespace_below_a_box_is_not_reported(tmp_path):
    """`label(800, 898, "one plan, two lanes")` — the real diagram's own shape.

    A free-floating annotation 40px below a node, horizontally inside that
    node's span, and deliberately outside it. The naive vertical check (drop the
    baseline-inside guard, test every rect above) reports this, and reports it on
    a diagram that is correct.
    """
    rc, out = _run(tmp_path, """
<rect class="todo" x="100" y="100" width="400" height="200"/>
<text x="120" y="130">in the box</text>
<text x="300" y="340">one plan, two lanes</text>
""")
    assert rc == 0, out


def test_a_column_moving_to_the_next_panel_down_is_not_reported(tmp_path):
    """Stacked panels share a label column: x and class are identical.

    The real diagram has three of these at x=1578. A rule that homes the whole
    column on its first member reports every label in the second panel as an
    overflow of the first.
    """
    rc, out = _run(tmp_path, """
<rect class="panel" x="500" y="100" width="300" height="120"/>
<text class="b" x="520" y="130">first panel, line one</text>
<text class="b" x="520" y="150">first panel, line two</text>
<rect class="panel" x="500" y="300" width="300" height="120"/>
<text class="b" x="520" y="330">second panel, line one</text>
<text class="b" x="520" y="350">second panel, line two</text>
""")
    assert rc == 0, out


def test_a_descender_hanging_past_the_edge_is_reported(tmp_path):
    """A baseline just inside the edge: the glyph's tail is not.

    The second label's baseline is 2px ABOVE the panel's bottom, so nothing but
    the descender allowance can report it — at 20px type it drops ~4.4px. Pinned
    because DESC is a constant someone will want to tune, and tuning it to zero
    turns this test green while quietly removing a class of complaint.
    """
    rc, out = _run(tmp_path, """
<rect class="panel" x="100" y="100" width="300" height="100"/>
<text style="font-size:20" x="120" y="140">first line, inside</text>
<text style="font-size:20" x="120" y="198">gypsy</text>
""")
    assert rc == 1, out
    assert VERTICAL in out, out
    assert "'gypsy'" in out, out


def test_the_right_edge_check_still_fires(tmp_path):
    """Check (3) is untouched by (4) and keeps its own test."""
    rc, out = _run(tmp_path, """
<rect class="ship" x="100" y="100" width="120" height="100"/>
<text x="110" y="140">a label far wider than the box it was put in</text>
""")
    assert rc == 1, out
    assert HORIZONTAL in out, out


@pytest.mark.parametrize("cls", ["bg", "lane", "band", "panel", "zone"])
def test_every_container_class_is_checked_for_vertical_overflow(tmp_path, cls):
    """CONTAINER_CLASSES stay out of the OBSTACLE list and inside this one.

    They are excluded from checks (2) and (3) on purpose — a band drawn around
    content is not a box a connector crosses — and that exclusion is why the
    measured case was invisible: the panel was not in the only rect list the
    overflow test read.
    """
    mod = _checker()
    assert cls in mod.CONTAINER_CLASSES
    rc, out = _run(tmp_path, f"""
<rect class="{cls}" x="100" y="300" width="300" height="100"/>
<text x="120" y="330">inside</text>
<text x="120" y="460">below the bottom edge</text>
""")
    assert rc == 1, out
    assert VERTICAL in out, out
    assert "'below the bottom edge'" in out, out


# --- the diagram is published as-is, so it may not name a private repo --------

# Minos is deliberately allowed: github.com/operator/minos is public, and naming
# the actual human gate is more useful to a reader than a euphemism. eunomia is
# the repo the diagram lives in and describes.
PUBLISHABLE_ALLOWLIST = {"eunomia", "minos"}


def test_the_generator_names_no_private_repo():
    """v1.7 removed two private repo names and a client's first name so the
    diagram could be published without a sanitising pass. That property has to
    be enforced, not remembered: the names were there for months, and the next
    person adding a concrete example will reach for the repo they are working
    in. Derived from config/repos.conf rather than a fixed list, so a repo
    enrolled tomorrow is covered without editing this test.
    """
    conf = GENERATOR.parent.parent.parent / "config" / "repos.conf"
    names = set()
    for line in conf.read_text().splitlines():
        line = line.strip()
        if line.startswith("repo:"):
            slug = line.split()[1]
            names.add(slug.split("/", 1)[1].lower())
    names -= PUBLISHABLE_ALLOWLIST
    assert names, "no repo names parsed from repos.conf — the parser is wrong"

    src = GENERATOR.read_text().lower()
    found = sorted(n for n in names if n in src)
    assert not found, (
        f"{GENERATOR.name} names private repositories {found}. The diagram is "
        "copied verbatim to example.org; a concrete example belongs in a "
        "runbook, and the mechanism belongs here. See docs/diagrams/README.md."
    )
