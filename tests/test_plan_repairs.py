"""Tests for `repairs` (plan 0089): the field, `lint`'s four checks on it,
`graph`'s separate `repairs` list, and `ledger`'s `repaired_by` column.

D2's checks are tested both ways — one case that trips each rule, one that
doesn't — in both the inline (`repairs: ["0057:code"]`) and block
(`repairs:\\n  - "0057:code"`) shapes `parse_list_field` already supports for
`depends_on` and `paths`.
"""
import importlib.machinery
import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
FLEET_PLAN = BIN / "fleet-plan"


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_plan", str(FLEET_PLAN))
    spec = importlib.util.spec_from_loader("fleet_plan", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


PLAN = """---
id: {id}
status: {status}
repo: operator/eunomia
zone: public
tier: 1
paths: ["a"]
{repairs}
---

## 1. Goal
g

## 2. Deliverables
- d

## 3. Boundaries
- don't reuse the collector regex, because it guards free text

## 4. Definition of done
- dod

## 5. Handoff
h

## 6. Resources
- lease: b
"""


def _write(tmp_path, name, status="ready", repairs=""):
    pid = Path(name).stem
    p = tmp_path / name
    p.write_text(PLAN.format(id=pid, status=status, repairs=repairs))
    return p


def _inline(*entries):
    return "repairs: [" + ", ".join(f'"{e}"' for e in entries) + "]"


def _block(*entries):
    return "repairs:\n" + "\n".join(f'  - "{e}"' for e in entries)


# --------------------------------------------------------------- the field itself

def test_absent_repairs_parses_as_empty_list(tmp_path):
    mod = _load()
    p = _write(tmp_path, "0100-a.md")
    fm, _body, _errs = mod.parse(p)
    assert fm["repairs"] == []


def test_inline_and_block_forms_both_parse(tmp_path):
    mod = _load()
    target = _write(tmp_path, "0057-x.md")
    inline = _write(tmp_path, "0100-inline.md", repairs=_inline("0057-x:code"))
    block = _write(tmp_path, "0101-block.md", repairs=_block("0057-x:code"))
    assert mod.parse(inline)[0]["repairs"] == ["0057-x:code"]
    assert mod.parse(block)[0]["repairs"] == ["0057-x:code"]
    # both lint clean against the real target
    assert mod.lint(inline) == []
    assert mod.lint(block) == []
    assert mod.lint(target) == []


# ------------------------------------------------------------ D2a: malformed shape

def test_entry_with_no_colon_is_refused(tmp_path):
    mod = _load()
    p = _write(tmp_path, "0100-a.md", repairs=_inline("0057-x"))
    errs = mod.lint(p)
    assert any("malformed" in e for e in errs), errs


def test_entry_with_empty_class_is_refused(tmp_path):
    mod = _load()
    p = _write(tmp_path, "0100-a.md", repairs=_inline("0057-x:"))
    errs = mod.lint(p)
    assert any("malformed" in e for e in errs), errs


def test_entry_with_empty_id_is_refused(tmp_path):
    mod = _load()
    p = _write(tmp_path, "0100-a.md", repairs=_inline(":code"))
    errs = mod.lint(p)
    assert any("malformed" in e for e in errs), errs


def test_well_formed_entry_is_not_flagged_malformed(tmp_path):
    mod = _load()
    _write(tmp_path, "0057-x.md")
    p = _write(tmp_path, "0100-a.md", repairs=_inline("0057-x:code"))
    errs = mod.lint(p)
    assert not any("malformed" in e for e in errs), errs


def test_malformed_check_applies_at_draft_status_too(tmp_path):
    """D2: malformed is an error 'at any status' — repairs never gates
    dispatch, so it is checked the same at draft as at ready."""
    mod = _load()
    p = _write(tmp_path, "0100-a.md", status="draft", repairs=_inline("0057-x"))
    errs = mod.lint(p)
    assert any("malformed" in e for e in errs), errs


# ------------------------------------------------------------- D2b: closed class set

def test_unknown_class_is_refused_and_names_the_set(tmp_path):
    mod = _load()
    _write(tmp_path, "0057-x.md")
    p = _write(tmp_path, "0100-a.md", repairs=_inline("0057-x:other"))
    errs = mod.lint(p)
    assert any("class 'other'" in e and "sequencing" in e for e in errs), errs


def test_every_real_class_is_accepted(tmp_path):
    """`code` in particular — Boundaries: 'code is a valid class, and lint
    accepts it without comment', since most HIGHs start in code."""
    mod = _load()
    _write(tmp_path, "0057-x.md")
    for i, cls in enumerate(("sequencing", "contract", "scope", "decision", "code")):
        p = _write(tmp_path, f"010{i}-a.md", repairs=_inline(f"0057-x:{cls}"))
        errs = mod.lint(p)
        assert not any("not one of" in e for e in errs), (cls, errs)


# ---------------------------------------------------------- D2c: id resolves nowhere

def test_dangling_repair_target_is_refused(tmp_path):
    mod = _load()
    p = _write(tmp_path, "0100-a.md", repairs=_inline("9999-nope:code"))
    errs = mod.lint(p)
    assert any("9999-nope" in e and "not the id of any plan" in e for e in errs), errs


def test_resolvable_repair_target_is_not_flagged(tmp_path):
    mod = _load()
    _write(tmp_path, "0057-x.md")
    p = _write(tmp_path, "0100-a.md", repairs=_inline("0057-x:code"))
    errs = mod.lint(p)
    assert not any("not the id of any plan" in e for e in errs), errs


def test_dangling_target_flagged_regardless_of_status(tmp_path):
    """Mirrors depends_on's own dangling-id rule: checked always, not only at
    'ready' — see `_resolved_deps`'s unconditional dangling check."""
    mod = _load()
    p = _write(tmp_path, "0100-a.md", status="draft", repairs=_inline("9999-nope:code"))
    errs = mod.lint(p)
    assert any("not the id of any plan" in e for e in errs), errs


# ----------------------------------------------------------- D2d: never self-repair

def test_repairs_naming_itself_is_refused(tmp_path):
    mod = _load()
    p = _write(tmp_path, "0100-a.md", repairs=_inline("0100-a:code"))
    errs = mod.lint(p)
    assert any("names the plan itself" in e for e in errs), errs


def test_repairs_naming_a_different_plan_is_not_flagged_as_self(tmp_path):
    mod = _load()
    _write(tmp_path, "0057-x.md")
    p = _write(tmp_path, "0100-a.md", repairs=_inline("0057-x:code"))
    errs = mod.lint(p)
    assert not any("names the plan itself" in e for e in errs), errs


# --------------------------------------------------- repairs never gates dispatch

def test_repairs_entry_does_not_change_edges_layers_or_unreachable(tmp_path):
    """Boundaries: 'repairs never gates dispatch'. A plan carrying a repairs
    entry must produce an identical depends_on graph to the same plan
    without one — repairs is never folded into `edges`."""
    mod = _load()
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "0057-x.md").write_text(PLAN.format(id="0057-x", status="done", repairs=""))
    (plans / "0100-a.md").write_text(
        PLAN.format(id="0100-a", status="ready", repairs=""))
    files_without = [plans / "0057-x.md", plans / "0100-a.md"]
    data_without = mod.graph(files_without, repo=tmp_path, ref="main")

    (plans / "0100-a.md").write_text(PLAN.format(
        id="0100-a", status="ready", repairs=_inline("0057-x:sequencing")))
    data_with = mod.graph(files_without, repo=tmp_path, ref="main")

    assert data_with["edges"] == data_without["edges"]
    assert [{k: n[k] for k in ("id", "layer", "unreachable")} for n in data_with["nodes"]] == \
           [{k: n[k] for k in ("id", "layer", "unreachable")} for n in data_without["nodes"]]
    assert data_with["repairs"] == [{"from": "0100-a", "to": "0057-x", "class": "sequencing"}]
    assert data_without["repairs"] == []


def test_graph_repairs_list_is_separate_from_edges(tmp_path):
    mod = _load()
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "0057-x.md").write_text(PLAN.format(id="0057-x", status="done", repairs=""))
    (plans / "0100-a.md").write_text(PLAN.format(
        id="0100-a", status="ready", repairs=_inline("0057-x:contract")))
    files = [plans / "0057-x.md", plans / "0100-a.md"]
    data = mod.graph(files, repo=tmp_path, ref="main")
    assert data["repairs"] == [{"from": "0100-a", "to": "0057-x", "class": "contract"}]
    assert all(e["from"] != "0100-a" or e["to"] != "0057-x" for e in data["edges"])


def test_mermaid_gives_repairs_its_own_labelled_style(tmp_path):
    mod = _load()
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "0057-x.md").write_text(PLAN.format(id="0057-x", status="done", repairs=""))
    (plans / "0100-a.md").write_text(PLAN.format(
        id="0100-a", status="ready", repairs=_inline("0057-x:decision")))
    files = [plans / "0057-x.md", plans / "0100-a.md"]
    data = mod.graph(files, repo=tmp_path, ref="main")
    mm = mod._mermaid(data)
    assert "repairs: decision" in mm


def test_svg_draws_a_distinct_repair_edge(tmp_path):
    mod = _load()
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "0057-x.md").write_text(PLAN.format(id="0057-x", status="done", repairs=""))
    (plans / "0100-a.md").write_text(PLAN.format(
        id="0100-a", status="ready", repairs=_inline("0057-x:code")))
    files = [plans / "0057-x.md", plans / "0100-a.md"]
    data = mod.graph(files, repo=tmp_path, ref="main")
    svg = mod._svg(data)
    assert 'class="repair-edge"' in svg


# -------------------------------------------------------------------------- ledger

def test_repaired_by_lands_on_the_target_row_not_the_source_row(tmp_path):
    mod = _load()
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "0057-x.md").write_text(PLAN.format(id="0057-x", status="done", repairs=""))
    (plans / "0100-a.md").write_text(PLAN.format(
        id="0100-a", status="ready", repairs=_inline("0057-x:code")))
    files = [plans / "0057-x.md", plans / "0100-a.md"]
    data = mod.ledger(files, db=tmp_path / "no-such.db", repo="operator/eunomia")
    rows = {r["id"]: r for r in data["rows"]}
    assert rows["0057-x"]["repaired_by"] == [{"id": "0100-a", "class": "code"}]
    assert rows["0100-a"]["repaired_by"] == []
    assert data["counts"]["repaired"] == 1


def test_repairs_to_own_file_is_never_written(tmp_path):
    """Boundaries: 'never write to the repaired plan's file' — the ledger
    only reads; running it must not touch either plan file."""
    mod = _load()
    plans = tmp_path / "plans"
    plans.mkdir()
    target = plans / "0057-x.md"
    target.write_text(PLAN.format(id="0057-x", status="done", repairs=""))
    source = plans / "0100-a.md"
    source.write_text(PLAN.format(id="0100-a", status="ready",
                                  repairs=_inline("0057-x:code")))
    before = {f: f.read_bytes() for f in (target, source)}
    mod.ledger([target, source], db=tmp_path / "no-such.db", repo="operator/eunomia")
    assert {f: f.read_bytes() for f in (target, source)} == before


def test_no_repairs_means_an_empty_repaired_by_and_zero_count(tmp_path):
    mod = _load()
    plans = tmp_path / "plans"
    plans.mkdir()
    p = plans / "0100-a.md"
    p.write_text(PLAN.format(id="0100-a", status="ready", repairs=""))
    data = mod.ledger([p], db=tmp_path / "no-such.db", repo="operator/eunomia")
    assert data["rows"][0]["repaired_by"] == []
    assert data["counts"]["repaired"] == 0


_OLD_ROW_KEYS = {"id", "status", "runs", "outcome", "pr", "pr_state", "pr_source",
                 "pr_as_of", "rounds", "impl_seconds", "class", "reason",
                 "probe", "dispatches", "review_rounds", "total"}
_OLD_COUNTS_KEYS = {"plans", "claimed", "stalled", "unknown"}


def test_ledger_json_only_grows(tmp_path):
    """Plan 0089 D4/D.o.D: the shape lynceus already decodes must be a strict
    subset of the new shape — a row or a counts dict with keys beyond the old
    set plus exactly the new ones would be a rename or removal a fielded
    decoder would ignore or choke on, not a growth."""
    mod = _load()
    plans = tmp_path / "plans"
    plans.mkdir()
    p = plans / "0100-a.md"
    p.write_text(PLAN.format(id="0100-a", status="ready", repairs=""))
    data = mod.ledger([p], db=tmp_path / "no-such.db", repo="operator/eunomia")
    # plan 0196: `stages` and `submitted_by` are the newest growth, on top of
    # plan 0089's own `repaired_by`.
    assert set(data["rows"][0]) == _OLD_ROW_KEYS | {"repaired_by", "stages", "submitted_by"}
    assert set(data["counts"]) == _OLD_COUNTS_KEYS | {"repaired"}


# --------------------------------------------------------------------------- docs

def test_docs_name_repairs_and_its_classes():
    doc = (FLEET_PLAN.parent.parent / "docs" / "feature-plans.md").read_text()
    assert "repairs" in doc
    for cls in ("sequencing", "contract", "scope", "decision", "code"):
        assert cls in doc


def test_plan_graph_doc_names_repairs():
    doc = (FLEET_PLAN.parent.parent / "docs" / "plan-graph.md").read_text()
    assert "repairs" in doc


def test_plan_ledger_doc_names_repaired_by():
    doc = (FLEET_PLAN.parent.parent / "docs" / "plan-ledger.md").read_text()
    assert "repaired_by" in doc
