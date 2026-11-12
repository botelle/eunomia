"""Tests for config/model-prices.json's shape and bin/fleet-reviews' reading of
it (plan 0197 D4).

Loaded the same way fleet-reviews reads its own siblings — by path, not by
package name, since this tool runs from a pinned worktree."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
CONFIG = Path(__file__).resolve().parent.parent / "config"
SHIPPED = CONFIG / "model-prices.json"


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_reviews_prices", str(BIN / "fleet-reviews"))
    spec = importlib.util.spec_from_loader("fleet_reviews_prices", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# The owner-approved table, plan 0197 D4, verbatim. `None` throughout means
# "the page does not give this price" (long-context fields omitted entirely on
# a row with no `long_context` block mean the same thing).
_SHIPPED_TABLE = [
    {"model": "claude-fable-5-1", "effective_from": None,
     "input": 10.00, "cached_input": 0.25, "cache_write_short": 12.50,
     "cache_write_long": 20.00, "output": 50.00, "long_context": None},
    {"model": "claude-opus-5-5", "effective_from": None,
     "input": 4.00, "cached_input": 0.20, "cache_write_short": 5.00,
     "cache_write_long": 8.00, "output": 20.00, "long_context": None},
    {"model": "claude-sonnet-5-5", "effective_from": None,
     "input": 2.00, "cached_input": 0.10, "cache_write_short": 2.50,
     "cache_write_long": 4.00, "output": 10.00, "long_context": None},
    {"model": "claude-sonnet-5", "effective_from": None,
     "input": 2.00, "cached_input": 0.20, "cache_write_short": 2.50,
     "cache_write_long": 4.00, "output": 10.00, "long_context": None},
    {"model": "gpt-6-astra", "effective_from": None,
     "input": 10.00, "cached_input": 1.00, "cache_write_short": 12.50,
     "cache_write_long": 25.00, "output": 50.00,
     "long_context": {"threshold_input_tokens": 272000, "input": 20.00,
                       "cached_input": 2.00, "cache_write_short": 25.00,
                       "cache_write_long": 50.00, "output": 75.00}},
    {"model": "gemini-4-argon-high", "effective_from": None,
     "input": 2.00, "cached_input": 0.10, "cache_write_short": None,
     "cache_write_long": None, "output": 10.00, "long_context": None},
    {"model": "gemini-4-argon-high", "effective_from": "2026-12-01",
     "input": 4.00, "cached_input": 0.20, "cache_write_short": None,
     "cache_write_long": None, "output": 20.00, "long_context": None},
]
_PRICE_KEYS = ("effective_from", "input", "cached_input", "cache_write_short",
              "cache_write_long", "output", "long_context")


def test_shipped_file_equals_d4s_table():
    mod = _load()
    rows = mod.load_prices(SHIPPED)
    got = [{k: r.get(k) for k in ("model",) + _PRICE_KEYS} for r in rows]
    assert got == _SHIPPED_TABLE
    # No entry for muse-wonder-1.0 (D4): Meta publishes no official per-token
    # price, so it must stay absent, not a row of nulls.
    assert "muse-wonder-1.0" not in {r["model"] for r in rows}


def test_every_shipped_row_has_checked_on_and_source():
    mod = _load()
    for row in mod.load_prices(SHIPPED):
        assert row.get("checked_on"), row
        assert row.get("source", "").startswith("https://"), row


def test_rejects_a_missing_checked_on(tmp_path):
    mod = _load()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"model": "x", "source": "https://x", "input": 1}]))
    try:
        mod.load_prices(bad)
        assert False, "a missing checked_on must be rejected"
    except mod.PriceFileError:
        pass


def test_rejects_a_missing_source(tmp_path):
    mod = _load()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"model": "x", "checked_on": "2026-11-03", "input": 1}]))
    try:
        mod.load_prices(bad)
        assert False, "a missing source must be rejected"
    except mod.PriceFileError:
        pass


def test_rejects_a_negative_price(tmp_path):
    mod = _load()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"model": "x", "checked_on": "2026-11-03",
                                "source": "https://x", "input": -1}]))
    try:
        mod.load_prices(bad)
        assert False, "a negative price must be rejected"
    except mod.PriceFileError:
        pass


def test_rejects_a_negative_long_context_price(tmp_path):
    mod = _load()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"model": "x", "checked_on": "2026-11-03",
                                "source": "https://x",
                                "long_context": {"threshold_input_tokens": 1000,
                                                  "output": -5}}]))
    try:
        mod.load_prices(bad)
        assert False, "a negative long_context price must be rejected"
    except mod.PriceFileError:
        pass


def test_rejects_two_entries_for_one_model_with_the_same_effective_from(tmp_path):
    mod = _load()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([
        {"model": "x", "effective_from": None, "checked_on": "2026-11-03", "source": "https://x"},
        {"model": "x", "effective_from": None, "checked_on": "2026-11-04", "source": "https://x"},
    ]))
    try:
        mod.load_prices(bad)
        assert False, "a duplicate (model, effective_from) must be rejected"
    except mod.PriceFileError:
        pass


def test_a_run_before_december_prices_gemini_argon_high_at_the_earlier_rate():
    mod = _load()
    prices = mod.load_prices(SHIPPED)
    row = mod._price_row_for(prices, "gemini-4-argon-high", "2026-11-30T23:59:59Z")
    assert (row["input"], row["output"]) == (2.00, 10.00)


def test_a_run_on_december_first_prices_gemini_argon_high_at_the_new_rate():
    mod = _load()
    prices = mod.load_prices(SHIPPED)
    row = mod._price_row_for(prices, "gemini-4-argon-high", "2026-12-01T00:00:00Z")
    assert (row["input"], row["output"]) == (4.00, 20.00)


def test_gemini_argon_medium_aliases_to_the_same_rows_as_high():
    """The page prices the model, not the effort level (D4's own note)."""
    mod = _load()
    prices = mod.load_prices(SHIPPED)
    before = mod._price_row_for(prices, "gemini-4-argon-medium", "2026-11-30T00:00:00Z")
    after = mod._price_row_for(prices, "gemini-4-argon-medium", "2026-12-01T00:00:00Z")
    assert (before["input"], before["output"]) == (2.00, 10.00)
    assert (after["input"], after["output"]) == (4.00, 20.00)


def test_a_model_with_no_price_row_returns_none():
    mod = _load()
    prices = mod.load_prices(SHIPPED)
    assert mod._price_row_for(prices, "muse-wonder-1.0", "2026-11-05T00:00:00Z") is None


def test_cost_for_sums_the_four_buckets_at_their_own_rate():
    mod = _load()
    prices = mod.load_prices(SHIPPED)
    # claude-sonnet-5-5: input 2.00, cached 0.10, cache_write_short 2.50, output 10.00
    cost = mod.cost_for(prices, "claude-sonnet-5-5", "2026-11-05T00:00:00Z",
                        1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert cost == 2.00 + 0.10 + 2.50 + 10.00


def test_cost_for_is_none_without_a_price_or_without_any_usage():
    mod = _load()
    prices = mod.load_prices(SHIPPED)
    assert mod.cost_for(prices, "muse-wonder-1.0", "2026-11-05T00:00:00Z", 1, 0, 0, 1) is None
    assert mod.cost_for(prices, "claude-sonnet-5-5", "2026-11-05T00:00:00Z",
                        None, None, None, None) is None


def test_cost_for_switches_to_the_long_context_tier_past_the_threshold():
    mod = _load()
    prices = mod.load_prices(SHIPPED)
    under = mod.cost_for(prices, "gpt-6-astra", "2026-11-05T00:00:00Z", 271_999, 0, 0, 0)
    over = mod.cost_for(prices, "gpt-6-astra", "2026-11-05T00:00:00Z", 272_001, 0, 0, 0)
    assert under == 271_999 * 10.00 / 1_000_000
    assert over == 272_001 * 20.00 / 1_000_000
