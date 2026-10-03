"""Experimental fixed-30 prompt packaging (run_backtest.package_rules_for_experiment):
the masked RULES.md §8 baseline table is replaced by the run's frozen masked
universe ONLY when the experimental selector is on; RULES.md and the
selector-off prompt are unchanged. Pure -- no Docker, Neon, or Hermes.
Run: python -m pytest -q test_experimental_rules_packaging.py
"""
import re
from datetime import date
from pathlib import Path

import pytest

import laya_selector as ls
import run_backtest as rb
from anonymize import RunMasking

REPO = Path(__file__).resolve().parent.parent
RULES = (REPO / "RULES.md").read_text(encoding="utf-8")
UNIVERSE = ls.load_universe()
WATCHLIST = rb._experimental_watchlist_entries(UNIVERSE)
BASELINE = [m for m in UNIVERSE.members if m.group == "baseline"]
ACCOUNT = {"equity": 100_000.0, "cash": 100_000.0, "exposure_pct": 0.0,
           "daily_pl_pct": 0.0, "weekly_pl_pct": 0.0, "drawdown_pct": 0.0}


def _masking():
    return RunMasking(rb._names_for_masking(WATCHLIST), run_start=date(2022, 7, 1), seed=11,
                      alias_scheme="opaque", word_like_tickers=list(UNIVERSE.word_like_tickers))


def _rules_block(prompt):
    return prompt.partition("===== RULES.md (verbatim")[2].partition("===== END RULES.md =====")[0]


def test_experimental_prompt_lists_the_frozen_universe_and_no_baseline_table():
    m = _masking()
    block = _rules_block(rb.build_prompt("Day 1", ACCOUNT, [], [], [], masking=m,
                                         experimental_universe=WATCHLIST))
    m.assert_clean(block)
    s8 = block.partition("## 8. Watchlist")[2].partition("### Criteria used")[0]
    table = re.findall(r"^\| (\w+) \| ([A-Za-z ]+) \|$", s8, re.M)
    table = [(a, sec) for a, sec in table if a != "Alias"]
    assert sorted(a for a, _ in table) == sorted(m.alias(s) for s in UNIVERSE.symbols)
    assert [a for a, _ in table] == sorted(a for a, _ in table), "row order is alias order only"
    assert {m.alias(t["symbol"]): t["sector"] for t in WATCHLIST} == dict(table)
    assert rb.EXPERIMENTAL_RULES_PACKAGING in s8
    for obsolete in ("| Symbol | Sector | Company |", "Ten names, ten sectors", "Populated 2026-08-17",
                     "keep the two in sync", "Re-screening?"):
        assert obsolete not in block, obsolete
    for phrase in ("Allowed names", "CANDIDATES", "Held positions", "exit management"):
        assert phrase in s8
    for forbidden in ("review_probability", "answer_confidence", "Laya", "laya", "rank"):
        assert forbidden not in block


def test_every_other_rules_line_is_delivered_unchanged():
    m = _masking()
    masked = m.mask_text(RULES)
    block = _rules_block(rb.build_prompt("Day 1", ACCOUNT, [], [], [], masking=m,
                                         experimental_universe=WATCHLIST))
    head = masked.partition("\n## 8. Watchlist\n")[0]
    tail = "### Criteria used" + masked.partition("\n### Criteria used")[2]
    assert head in block and tail in block
    for section in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5", "## 6", "## 7.", "## 9"):
        assert section in block, section


def test_selector_off_prompt_is_unchanged_and_carries_the_baseline_table():
    m = RunMasking({x.symbol: [x.name] for x in BASELINE}, run_start=date(2022, 7, 1), seed=11)
    prompt = rb.build_prompt("Day 1", ACCOUNT, [], [], [], masking=m)
    assert m.mask_text(RULES) in prompt, "selector-off delivers exactly the masked RULES.md"
    assert "| Symbol | Sector | Company |" in prompt and rb.EXPERIMENTAL_RULES_PACKAGING not in prompt


def test_missing_section_headings_fail_closed():
    with pytest.raises(ValueError, match="refusing to send the baseline"):
        rb.package_rules_for_experiment("no section eight here", _masking(), WATCHLIST)


def test_rules_md_itself_still_holds_the_baseline_ten():
    for x in BASELINE:
        assert f"| {x.symbol} |" in RULES
