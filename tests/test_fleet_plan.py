"""Tests for fleet-plan. The load-bearing one is the empty-Boundaries refusal."""
import importlib.machinery
import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_plan", str(BIN / "fleet-plan"))
    spec = importlib.util.spec_from_loader("fleet_plan", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


PLAN = """---
id: 0009-x
status: {status}
repo: operator/eunomia
zone: public
tier: 1
paths: ["a"]
---

## 1. Goal
g

## 2. Deliverables
- d

## 3. Boundaries
{boundaries}

## 4. Definition of done
- dod

## 5. Handoff
h

## 6. Resources
- lease: b
"""


def _write(tmp_path, **kw):
    kw.setdefault("status", "ready")
    kw.setdefault("boundaries", "- don't reuse the collector regex, because it guards free text")
    p = tmp_path / "0009-x.md"
    p.write_text(PLAN.format(**kw))
    return p


def test_valid_plan_passes(tmp_path):
    assert _load().lint(_write(tmp_path)) == []


def test_empty_boundaries_refused(tmp_path):
    errs = _load().lint(_write(tmp_path, boundaries=""))
    assert any("Boundaries is empty" in e for e in errs)


def test_placeholder_boundaries_refused(tmp_path):
    """Leaving the template's angle-bracket prompt in place is not a boundary."""
    errs = _load().lint(_write(tmp_path, boundaries="- <don't X, because Y>"))
    assert any("Boundaries is empty" in e for e in errs)


def test_boundaries_without_reasoning_flagged_when_ready(tmp_path):
    errs = _load().lint(_write(tmp_path, boundaries="- do not add an env knob"))
    assert any("not why" in e for e in errs)


def test_draft_may_lack_reasoning(tmp_path):
    """A draft is allowed to be rough; only 'ready' is dispatchable."""
    errs = _load().lint(_write(tmp_path, status="draft", boundaries="- do not add an env knob"))
    assert not any("not why" in e for e in errs)


def test_bad_zone_and_status_rejected(tmp_path):
    p = tmp_path / "0009-x.md"
    p.write_text(PLAN.format(status="shipped", boundaries="- x because y").replace(
        "zone: public", "zone: secret"))
    errs = _load().lint(p)
    assert any("status" in e for e in errs) and any("zone" in e for e in errs)


def test_missing_section_reported(tmp_path):
    p = tmp_path / "0009-x.md"
    body = PLAN.format(status="ready", boundaries="- x because y").replace(
        "## 5. Handoff\nh\n\n", "")
    p.write_text(body)
    assert any("5. Handoff" in e for e in _load().lint(p))


def test_repo_plans_are_valid():
    """The plans actually committed here must lint."""
    mod = _load()
    plans = sorted((Path(__file__).resolve().parent.parent / "plans").glob("*.md"))
    assert plans, "no plans found"
    errs = [e for f in plans for e in mod.lint(f)]
    assert errs == [], errs


def test_verbatim_template_copy_is_refused(tmp_path):
    """The failure the first version shipped: a renamed copy of the template, set to
    'ready', linted with zero problems — the template's own multi-line prose contains
    'because' and an em dash, so it satisfied both checks. This is the shape every
    real user starts from, so it is the case that matters most."""
    mod = _load()
    src = (Path(__file__).resolve().parent.parent / "plans" / "0000-template.md").read_text()
    p = tmp_path / "9999-copy.md"
    p.write_text(src.replace("status: draft", "status: ready")
                    .replace("id: 0000-template", "id: 9999-copy"))
    errs = mod.lint(p)
    assert any("Boundaries" in e for e in errs), errs


def test_unterminated_front_matter_does_not_crash(tmp_path):
    """A half-written draft in plans/ must not make the tool unusable fleet-wide."""
    p = tmp_path / "0009-x.md"
    p.write_text("---\nid: 0009-x\nstatus: draft\n")
    errs = _load().lint(p)          # must not raise
    assert any("not terminated" in e for e in errs), errs


def test_missing_file_reports_not_raises(tmp_path):
    errs = _load().lint(tmp_path / "nope.md")
    assert errs and all(isinstance(e, str) for e in errs)


def test_heading_inside_code_fence_does_not_end_a_section(tmp_path):
    """A boundary that quotes markdown must not truncate the section."""
    mod = _load()
    p = tmp_path / "0009-x.md"
    p.write_text(PLAN.format(status="ready", boundaries=(
        "- don't emit markdown headings in the body, because the renderer splits on them:\n"
        "\n```\n## not a section\n```\n")))
    errs = mod.lint(p)
    assert not any("Handoff" in e or "Resources" in e for e in errs), errs


def test_legit_angle_brackets_in_a_boundary_survive(tmp_path):
    """`<200ms` or `Vec<T>` is a real boundary, not a placeholder."""
    errs = _load().lint(_write(tmp_path, boundaries=(
        "- keep the p99 <200ms, because the caller times out at 250ms")))
    assert errs == [], errs


def test_section_text_is_correct_after_a_fence(tmp_path):
    """M3: _mask_fences must be LENGTH-PRESERVING. section() finds offsets in the
    masked text and slices the original body, so blanking fenced lines to "" shifted
    every later slice — which both false-refused valid plans and let an empty
    Boundaries section lint clean. Assert the returned TEXT, not just that a heading
    was found; the earlier fence test passed on the broken code precisely because it
    only checked the latter."""
    mod = _load()
    body = ("\n## 1. Goal\ng\n\n## 2. Deliverables\n- run it:\n\n```\n"
            "fleet-emit pr-opened --repo x\n## not a heading\n```\n\n"
            "## 3. Boundaries\n- don't reuse the collector regex, because it guards free text\n\n"
            "## 4. Definition of done\n- d\n")
    assert mod.section(body, "3. Boundaries").startswith("- don't reuse")
    assert "not a heading" in mod.section(body, "2. Deliverables")


def test_empty_boundaries_after_a_fence_still_refused(tmp_path):
    """The false-pass half of M3: a fence long enough to shift the slice onto an em
    dash made an empty Boundaries section lint clean."""
    p = tmp_path / "0009-x.md"
    p.write_text(PLAN.format(status="ready", boundaries="").replace(
        "## 2. Deliverables\n- d",
        "## 2. Deliverables\n- d\n\n```\n" + "x" * 53 + "\n```"))
    errs = _load().lint(p)
    assert any("Boundaries" in e for e in errs), errs


def test_unterminated_fence_does_not_swallow_later_sections(tmp_path):
    mod = _load()
    body = ("\n## 1. Goal\ng\n\n## 2. Deliverables\n```\nunterminated\n\n"
            "## 3. Boundaries\n- x because y\n")
    # An unterminated fence masks to EOF by design; the parse must not raise.
    assert isinstance(mod.section(body, "3. Boundaries"), str)


def test_in_flight_is_not_a_writable_status(tmp_path):
    """'In flight' is derived (a PR carrying the plan's marker is open), never stored —
    storing it would need a writer, and the only writer of a plan file is the PR that
    closes it."""
    errs = _load().lint(_write(tmp_path, status="in-flight"))
    assert any("status" in e for e in errs), errs


def _named(tmp_path, name, plan_id=None, **kw):
    """A valid plan at an arbitrary filename, id defaulting to the stem."""
    kw.setdefault("status", "ready")
    kw.setdefault("boundaries", "- don't reuse the collector regex, because it guards free text")
    # `plan_id or stem` would turn an explicit "" back into the stem, which is
    # exactly the case test_an_empty_id_value_is_refused needs to construct.
    ident = Path(name).stem if plan_id is None else plan_id
    text = PLAN.format(**kw).replace("id: 0009-x", f"id: {ident}")
    p = tmp_path / name
    p.write_text(text)
    return p


def test_duplicate_plan_id_is_refused(tmp_path):
    """Two plans claiming one id merge without a git conflict — different
    filenames — and `fleet-plan show <id>` then returns whichever it finds first.
    Both eunomia#93/#95 (0025) and the pre-existing 0017 pair got this far."""
    a = _named(tmp_path, "0025-capability-published.md")
    b = _named(tmp_path, "0025-reviewer-trigger.md")
    mod = _load()
    ea, eb = mod.lint(a), mod.lint(b)
    assert any("also claimed by 0025-reviewer-trigger.md" in e for e in ea), ea
    assert any("also claimed by 0025-capability-published.md" in e for e in eb), eb


def test_distinct_ids_do_not_collide_and_a_plan_never_reports_itself(tmp_path):
    """The inverse of the case above: the check must not match a file against
    itself, or it fires on every plan and is deleted within the day."""
    a = _named(tmp_path, "0025-capability-published.md")
    b = _named(tmp_path, "0026-reviewer-trigger.md")
    mod = _load()
    assert mod.lint(a) == []
    assert mod.lint(b) == []


def test_front_matter_id_must_match_the_filename(tmp_path):
    """The uniqueness check reads filenames, so it is only sufficient while the
    declared id and the filename agree — `fleet-plan show` resolves by both."""
    p = _named(tmp_path, "0031-x.md", plan_id="0009-x")
    errs = _load().lint(p)
    assert any("does not match the filename stem" in e for e in errs), errs


def test_a_filename_without_a_numeric_prefix_is_not_treated_as_an_id(tmp_path):
    """No crash, and no duplicate claim, for a file the convention does not cover."""
    p = _named(tmp_path, "notes.md", plan_id="notes")
    errs = _load().lint(p)
    assert not any("also claimed by" in e for e in errs), errs


def test_an_empty_id_value_is_refused(tmp_path):
    """Review 2099: `if fm.get("id")` exempted a blanked id — the key is present
    so REQUIRED_FM is satisfied, and "" never trips the stem comparison. It then
    lints clean, and fleet-watch would ignite it as plan_id "" on branch
    `feat/plan`. The guard tests for the key, not for a truthy value."""
    p = _named(tmp_path, "0033-x.md", plan_id="")
    errs = _load().lint(p)
    assert any("does not match the filename stem" in e for e in errs), errs


def test_a_dashless_id_file_still_collides(tmp_path):
    """Review 2137 L2: `plans/0025.md` has no dash, so a `^\\d{4}-` prefix rule
    exempted it from the duplicate check — while `fleet-plan show 0025` matched
    it anyway, because its stem splits to "0025", and returned whichever file
    sorted first. That is the silent misdirection the check exists to stop, so
    the prefix is derived with `show`'s own rule."""
    a = _named(tmp_path, "0025.md")
    b = _named(tmp_path, "0025-capability-published.md")
    mod = _load()
    assert any("also claimed by" in e for e in mod.lint(a)), mod.lint(a)
    assert any("also claimed by" in e for e in mod.lint(b)), mod.lint(b)


def test_a_non_numeric_head_is_still_not_an_id(tmp_path):
    """The rule widened; it must not widen to everything. `notes-on-0025.md`
    splits to "notes", which is not four digits, so it claims no id."""
    p = _named(tmp_path, "notes-on-0025.md")
    assert not any("also claimed by" in e for e in _load().lint(p))
