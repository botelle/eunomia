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


# --------------------------------------------------- review: (plan 0150, ADR-0025 §1)

import json

import pytest

STYLE_REPO = "acme/widgets"
STYLE_CONFIG = {"v": 1, "repos": {STYLE_REPO: {
    "paths": ["app/Theme/*.swift", "app/Colors/**/*.json"],
    "binary_paths": ["app/Art/*.png"],
    "screenshot": {"platform": "ios-simulator", "command": "tools/shoot.sh",
                   "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
                   "device": "iPhone 17", "scale": 3, "themes": ["classic"],
                   "appearances": ["light"], "timeout_minutes": 10}}}}
LEASE = "docs/screenshots/*-0009-x/**"


def _style_plan(tmp_path, monkeypatch, review="style", repo=STYLE_REPO, tier=1,
                paths=("app/Theme/*.swift", LEASE, "plans/0009-x.md")):
    cfg = tmp_path / "style-review.json"
    cfg.write_text(json.dumps(STYLE_CONFIG))
    mod = _load()
    monkeypatch.setattr(mod._style(), "CONFIG", cfg)
    p = tmp_path / "0009-x.md"
    text = PLAN.format(status="ready", boundaries="- x because y").replace(
        "repo: operator/eunomia", f"repo: {repo}\nreview: {review}").replace(
        "tier: 1", f"tier: {tier}").replace('paths: ["a"]', "paths: " + json.dumps(list(paths)))
    p.write_text(text)
    return mod, p


def test_a_declared_style_plan_with_its_lease_inside_lints_at_any_tier(tmp_path, monkeypatch):
    for tier in (0, 1, 3):
        mod, p = _style_plan(tmp_path, monkeypatch, tier=tier)
        assert mod.lint(p) == []


def test_the_full_review_and_an_absent_field_are_always_fine(tmp_path, monkeypatch):
    mod, p = _style_plan(tmp_path, monkeypatch, review="full", repo="acme/other")
    assert mod.lint(p) == []
    assert mod.lint(_write(tmp_path)) == []


def test_a_review_outside_the_closed_set_is_refused(tmp_path, monkeypatch):
    mod, p = _style_plan(tmp_path, monkeypatch, review="styel")
    assert any("review 'styel'" in e for e in mod.lint(p))


def test_a_style_plan_in_an_undeclared_repo_is_refused(tmp_path, monkeypatch):
    mod, p = _style_plan(tmp_path, monkeypatch, repo="acme/other")
    assert any("declares no style paths" in e for e in mod.lint(p))


def test_a_lease_entry_outside_the_patterns_is_refused(tmp_path, monkeypatch):
    mod, p = _style_plan(tmp_path, monkeypatch, paths=(
        "app/Theme/*.swift", "app/Other/o.swift", "app/Theme/**", LEASE))
    errs = mod.lint(p)
    assert any("'app/Other/o.swift'" in e for e in errs)
    assert any("'app/Theme/**'" in e for e in errs)   # a glob that is not a declared pattern


def test_a_literal_path_that_a_pattern_matches_is_allowed(tmp_path, monkeypatch):
    mod, p = _style_plan(tmp_path, monkeypatch, paths=(
        "app/Theme/Colors.swift", "app/Art/logo.png", LEASE))
    assert mod.lint(p) == []


def test_a_style_plan_without_its_evidence_lease_is_refused(tmp_path, monkeypatch):
    mod, p = _style_plan(tmp_path, monkeypatch, paths=("app/Theme/*.swift",))
    assert any("evidence lease 'docs/screenshots/*-0009-x/**'" in e for e in mod.lint(p))


def test_a_malformed_declaration_refuses_a_style_plan(tmp_path, monkeypatch):
    mod, p = _style_plan(tmp_path, monkeypatch)
    mod._style().CONFIG.write_text("{")
    assert any("malformed" in e for e in mod.lint(p))


# --------------------------------------------------------- spec_watch (plan 0177)

GOVERNANCE = """spec_watch:
  - name: api-routes
    kind: api
    code:
      - "api/speakhush/**"
    specs:
      - "docs/declared-behaviour.md"
  - name: screens
    kind: screen
    code:
      - "ios/SpeakHush/Leads/**"
    specs:
      - "ios/SpeakHushFlows/Leads/**"
"""

SPEC_PLAN = """---
id: 0009-x
status: ready
repo: acme/widgets
zone: public
tier: 1
paths: {paths}
---

## 1. Goal
g

## 2. Deliverables
- d

## 3. Boundaries
- don't reuse the collector regex, because it guards free text

## 4. Definition of done
{dod}

## 5. Handoff
h

## 6. Resources
- lease: b
"""


def _spec_repo(tmp_path, governance=GOVERNANCE):
    """A `plans/` directory beside a `governance.yaml`, mirroring a real
    repo's layout — `_governance_path` reads the plan's grandparent."""
    (tmp_path / "plans").mkdir()
    if governance is not None:
        (tmp_path / "governance.yaml").write_text(governance)
    return tmp_path / "plans"


def _spec_plan(plans_dir, paths, dod="- dod", extra=""):
    p = plans_dir / "0009-x.md"
    text = SPEC_PLAN.format(paths=json.dumps(list(paths)), dod=dod)
    if extra:
        text = text.replace("---\n\n## 1. Goal", f"{extra.rstrip()}\n---\n\n## 1. Goal")
    p.write_text(text)
    return p


def test_a_reached_rule_with_no_spec_at_all_is_refused(tmp_path):
    plans = _spec_repo(tmp_path)
    p = _spec_plan(plans, ["api/speakhush/leads.py"])
    errs = _load().lint(p)
    assert any("api-routes" in e for e in errs), errs


def test_a_reached_rule_with_the_spec_leased_but_not_mentioned_is_still_refused(tmp_path):
    plans = _spec_repo(tmp_path)
    p = _spec_plan(plans, ["api/speakhush/leads.py", "docs/declared-behaviour.md"])
    errs = _load().lint(p)
    assert any("api-routes" in e for e in errs), errs


def test_a_reached_rule_with_the_spec_leased_and_mentioned_is_clean(tmp_path):
    plans = _spec_repo(tmp_path)
    p = _spec_plan(plans, ["api/speakhush/leads.py", "docs/declared-behaviour.md"],
                   dod="- update `docs/declared-behaviour.md` with the new route")
    errs = _load().lint(p)
    assert not any("api-routes" in e for e in errs), errs


def test_a_new_file_lease_reaches_its_rule_through_the_fixed_prefix(tmp_path):
    """No file under `ios/SpeakHush/Leads/` exists yet — the `code` glob
    can only be reached by comparing fixed prefixes, never by matching an
    existing file."""
    plans = _spec_repo(tmp_path)
    p = _spec_plan(plans, ["ios/SpeakHush/Leads/**"])
    errs = _load().lint(p)
    assert any("screens" in e for e in errs), errs


def test_spec_exempt_with_a_reason_silences_a_reached_rule(tmp_path):
    plans = _spec_repo(tmp_path)
    p = _spec_plan(plans, ["api/speakhush/leads.py"])
    p.write_text(p.read_text().replace(
        "tier: 1", 'tier: 1\nspec_exempt: "refactor, no route or screen change"'))
    errs = _load().lint(p)
    assert not any("api-routes" in e for e in errs), errs


def test_an_empty_spec_exempt_is_refused(tmp_path):
    plans = _spec_repo(tmp_path)
    p = _spec_plan(plans, ["api/speakhush/leads.py"])
    p.write_text(p.read_text().replace("tier: 1", 'tier: 1\nspec_exempt: ""'))
    errs = _load().lint(p)
    assert any("spec_exempt" in e for e in errs), errs


def test_no_governance_yaml_adds_no_problems(tmp_path):
    plans = _spec_repo(tmp_path, governance=None)
    p = _spec_plan(plans, ["api/speakhush/leads.py"])
    errs = _load().lint(p)
    assert not any("spec_watch" in e or "api-routes" in e for e in errs), errs


def test_governance_yaml_with_no_spec_watch_key_adds_no_problems(tmp_path):
    plans = _spec_repo(tmp_path, governance="other_key: 1\n")
    p = _spec_plan(plans, ["api/speakhush/leads.py"])
    errs = _load().lint(p)
    assert not any("spec_watch" in e or "api-routes" in e for e in errs), errs


def test_the_replay_speakhush_0066_through_0068_each_fail(tmp_path):
    """speakhush plans 0066-0068 (eunomia has no access to clone that repo
    from this sandbox, so these are synthetic reconstructions of the shape
    the plan's own 'Why this exists' describes: a lease reaching
    `api/speakhush/**` or a Leads screen, with neither spec leased nor
    mentioned) — each must fail the rule its own code reaches."""
    plans = _spec_repo(tmp_path)
    for name, paths in (
        ("0066-investigate-a-lead", ["api/speakhush/leads/investigate.py"]),
        ("0067-draft-a-lead-reply", ["ios/SpeakHush/Leads/DraftReply.swift"]),
        ("0068-save-or-decline-a-lead", ["api/speakhush/leads/decide.py",
                                        "ios/SpeakHush/Leads/Decide.swift"]),
    ):
        p = plans / f"{name}.md"
        p.write_text(SPEC_PLAN.format(paths=json.dumps(paths), dod="- dod")
                     .replace("id: 0009-x", f"id: {name}"))
        errs = _load().lint(p)
        assert any("spec_watch rule" in e for e in errs), (name, errs)
