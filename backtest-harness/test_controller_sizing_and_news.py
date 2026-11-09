"""Prompt schema 5 corrections, on the real run_day decision path (only I/O mocked):
- BUY quantity is controller-owned (required_entry_quantity); Hermes' own BUY quantity is a legacy field, ignored.
- every candidate/position carries an explicit news status: headlines, checked-empty, or UNAVAILABLE.
Run: python -m pytest -q test_controller_sizing_and_news.py
"""
import math
import tempfile
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

import run_backtest as rb
from run_backtest import DEFAULT_MASKING_SEED, Holding, Portfolio, RunMasking
from test_execution_semantics import _run_backtest_scenario

DAY = date(2026, 2, 17)
NAMES = {"OLN": ["Olin Corporation", "Olin"], "DINO": ["HF Sinclair", "Sinclair"], "ZULU": ["Zulu Corp"]}
WATCHLIST = [{"symbol": "OLN", "sector": "Chemicals"}, {"symbol": "DINO", "sector": "Energy"},
             {"symbol": "ZULU", "sector": "Industrials"}]


def _m():
    return RunMasking(NAMES, run_start=DAY, seed=DEFAULT_MASKING_SEED)


def buy(alias, entry, stop, target, quantity="omit", **kw):
    d = {"alias": alias, "action": "BUY", "entry_price": entry, "stop_price": stop, "target_price": target,
         "confidence": 3, "primary_signal": "technical", "technical_basis": "rsi",
         "expected_reward_risk": round((target - entry) / (entry - stop), 2) if entry > stop else 0.0,
         "invalidation_condition": f"closes below ${stop}", "reasoning": "fixture", **kw}
    if quantity != "omit":
        d["quantity"] = quantity
    return d


def run(decisions, portfolio=None, news=None, masking=None):
    m = masking or _m()
    p = portfolio or Portfolio(cash=100_000.0, equity_curve=[100_000.0])
    working, prompts, queued, blocked = _run_backtest_scenario(
        p, DAY, 0, str(uuid.uuid4()), m, WATCHLIST, decisions, Path(tempfile.mkdtemp()), news_payload=news)
    return working, prompts, queued, blocked, m


def held(cash, value_ticker="ZULU", qty=None):
    """Equity stays $100,000: `cash` free plus the rest held in ZULU at its $100 mark."""
    q = qty if qty is not None else int((100_000.0 - cash) / 100.0)
    return Portfolio(cash=cash, equity_curve=[100_000.0],
                     holdings={value_ticker: Holding(ticker=value_ticker, quantity=q, entry_price=100.0,
                                                     stop_price=90.0, target_price=120.0, entry_day=0,
                                                     entry_trading_day=0)})


def queued_qty(working, ticker):
    return [o.quantity for o in working.pending_orders if o.ticker == ticker]


# -- controller-owned BUY quantity ---------------------------------------------------------------------------------

@pytest.mark.parametrize("entry,stop,target,hermes_qty,required", [
    (30.72, 27.75, 35.18, 65, 650),    # smoke Day 1 (b0b16ce1): §2b binds; Hermes wrote 65
    (31.32, 27.75, 36.68, 63, 557),    # smoke Day 8: §2a binds; Hermes wrote 63
])
def test_the_observed_dropped_digit_proposals_now_queue_the_controller_size(entry, stop, target, hermes_qty,
                                                                              required):
    m = _m()
    exp = rb._expected_buy_fill_price(entry)
    assert required == min(math.floor(2000 / (exp - stop)), math.floor(20000 / exp), math.floor(100000 / exp))
    working, _, queued, blocked, _ = run([buy(m.alias("OLN"), entry, stop, target, hermes_qty)], masking=m)
    assert not blocked, blocked
    assert queued_qty(working, "OLN") == [required]
    assert working.pending_orders[0].decision["quantity"] == required, "the journaled decision carries it"


def test_risk_concentration_and_cash_limited_sizing():
    m = _m()
    a = m.alias("OLN")
    # risk-limited: 12% stop -> floor(2000 / (100.05 - 88)) = 165 < concentration 199
    w, *_ = run([buy(a, 100.0, 88.0, 120.0)], masking=m)
    assert queued_qty(w, "OLN") == [165]
    # concentration-limited: 8% stop -> risk 248, concentration floor(20000 / 100.05) = 199
    w, *_ = run([buy(a, 100.0, 92.0, 115.0)], masking=m)
    assert queued_qty(w, "OLN") == [199]
    # cash-limited: $10,000 free -> floor(10000 / 100.05) = 99
    w, _, _, blocked, _ = run([buy(a, 100.0, 92.0, 115.0)], portfolio=held(10_000.0), masking=m)
    assert queued_qty(w, "OLN") == [99], blocked


def test_multiple_buys_share_remaining_cash_in_proposal_order():
    m = _m()
    w, _, queued, blocked, _ = run([buy(m.alias("OLN"), 100.0, 92.0, 115.0),
                                    buy(m.alias("DINO"), 100.0, 92.0, 115.0)],
                                   portfolio=held(25_000.0), masking=m)
    assert not blocked, blocked
    # OLN 199 reserves 199 * $100 = $19,900; DINO then floor(5100 / 100.05) = 50
    assert [(o.ticker, o.quantity) for o in w.pending_orders] == [("OLN", 199), ("DINO", 50)]


def test_unusable_inputs_and_zero_shares_are_rejected_explicitly_never_fabricated():
    m = _m()
    a = m.alias("OLN")
    w, _, queued, blocked, _ = run([buy(a, float("nan"), 92.0, 115.0)], masking=m)
    assert not w.pending_orders and not queued
    assert any("controller sizing gave unusable sizing inputs" in b for b in blocked), blocked
    w, _, queued, blocked, _ = run([buy(a, 100.0, 92.0, 115.0)], portfolio=held(50.0, qty=999), masking=m)
    assert not w.pending_orders and not queued
    assert any("controller sizing gave zero shares" in b for b in blocked), blocked


def test_an_invalid_stop_is_rejected_before_anything_is_queued():
    m = _m()
    w, _, queued, blocked, _ = run([buy(m.alias("OLN"), 100.0, 101.0, 115.0, 10)], masking=m)
    assert not w.pending_orders and not queued
    assert blocked == [b for b in blocked if "unusable sizing inputs (entry 100.0, stop 101.0" in b] and blocked


def test_a_legacy_buy_quantity_never_overrides_controller_sizing_and_is_reported():
    m = _m()
    w, _, _, blocked, _ = run([buy(m.alias("OLN"), 100.0, 92.0, 115.0, 5000)], masking=m)
    assert queued_qty(w, "OLN") == [199] and not blocked
    w2, *_ = run([buy(m.alias("OLN"), 100.0, 92.0, 115.0)], masking=m)
    assert queued_qty(w2, "OLN") == [199], "omitting the quantity gives the same size"


def test_sell_quantity_handling_is_unchanged():
    m = _m()
    z = m.alias("ZULU")
    sell = {"alias": z, "action": "SELL", "entry_price": 100.0, "stop_price": 90.0, "target_price": 120.0,
            "confidence": 3, "primary_signal": "technical", "technical_basis": "rsi", "expected_reward_risk": 0.0,
            "invalidation_condition": "n/a", "reasoning": "exit"}
    w, _, queued, blocked, _ = run([dict(sell, quantity=750)], portfolio=held(25_000.0), masking=m)
    assert [(o.action, o.quantity) for o in w.pending_orders] == [("SELL", 750)], blocked
    _, _, _, blocked, _ = run([dict(sell, quantity=0)], portfolio=held(25_000.0), masking=m)
    assert any("quantity must be positive" in b for b in blocked), blocked
    _, _, _, blocked, _ = run([sell], portfolio=held(25_000.0), masking=m)
    assert any("missing/invalid quantity" in b for b in blocked), blocked


def test_the_prompt_no_longer_asks_hermes_for_buy_share_arithmetic():
    prompt = rb.build_prompt("Day 1", {"equity": 1e5, "cash": 1e5, "exposure_pct": 0.0, "daily_pl_pct": 0.0,
                                       "weekly_pl_pct": 0.0, "drawdown_pct": 0.0}, [], [], [])
    assert "quantity must equal the required quantity" not in prompt
    assert "Omit quantity on a BUY" in prompt and "SELL only -- omit on a BUY" in prompt
    assert "The CONTROLLER computes every BUY's share quantity" in prompt
    assert rb.PROMPT_SCHEMA_VERSION == 5


# -- explicit news status ---------------------------------------------------------------------------------------------

def _block(prompt, alias):
    lines = prompt.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.strip().startswith(f"{alias} ["))
    return "\n".join(lines[i:i + 3])


def test_a_complete_empty_fetch_says_no_headlines_found_and_still_allows_an_entry():
    m = _m()
    w, prompts, _, blocked, _ = run([buy(m.alias("OLN"), 100.0, 92.0, 115.0)],
                                    news={"OLN": [], "DINO": [], "ZULU": []}, masking=m)
    block = _block(prompts[0], m.alias("OLN"))
    assert rb.NEWS_EMPTY_LINE.strip() in block and "not proof that no adverse news exists" in block
    assert "UNAVAILABLE" not in block
    assert queued_qty(w, "OLN") == [199], blocked


def test_headlines_keep_the_existing_masked_rendering():
    m = _m()
    art = {"headline": "Olin Corporation beats estimates", "created_at": "2026-02-16T14:30:00+00:00",
           "summary": "OLN shares rose", "symbols": ["OLN"]}
    _, prompts, _, _, _ = run([], news={"OLN": [art], "DINO": [], "ZULU": []}, masking=m)
    block = _block(prompts[0], m.alias("OLN"))
    assert "news (1d ago):" in block and m.alias("OLN") in block
    assert "No headlines found" not in block and "UNAVAILABLE" not in block
    m.assert_clean(prompts[0])                     # no real ticker or company name survives anywhere


def test_unavailable_news_is_labelled_and_blocks_a_new_entry():
    m = _m()
    w, prompts, queued, blocked, _ = run([buy(m.alias("OLN"), 100.0, 92.0, 115.0)],
                                         news={"DINO": [], "ZULU": []}, masking=m)   # no complete OLN result
    block = _block(prompts[0], m.alias("OLN"))
    assert rb.NEWS_UNAVAILABLE_LINE.strip() in block and "No headlines found" not in block
    assert not w.pending_orders and not queued
    assert any("§5a: news unavailable" in b for b in blocked), blocked
    m.assert_clean(prompts[0])


def test_held_positions_get_the_same_news_status():
    m = _m()
    _, prompts, _, _, _ = run([], portfolio=held(25_000.0), news={"OLN": [], "DINO": []}, masking=m)
    pos = prompts[0].split("OPEN POSITIONS", 1)[1].split("CANDIDATES", 1)[0]
    assert rb.NEWS_UNAVAILABLE_LINE.strip() in pos


def test_fetch_news_keeps_a_ticker_with_no_complete_result_out_instead_of_empty():
    with patch("run_backtest.run_in", return_value="x"), \
         patch("run_backtest.json_from", return_value={"news": {"OLN": []}}):
        out = rb.fetch_news(["OLN", "DINO"], DAY, "2026-02-17T16:00:00-05:00")
    assert out == {"OLN": []}
    assert rb.news_status(out, "OLN") == rb.NEWS_CHECKED and rb.news_status(out, "DINO") == rb.NEWS_UNAVAILABLE
    # an incomplete provider fetch still fails the whole cycle, as before
    with patch("run_backtest.run_in", side_effect=RuntimeError("incomplete pagination coverage")):
        with pytest.raises(RuntimeError):
            rb.fetch_news(["OLN"], DAY, "2026-02-17T16:00:00-05:00")


def test_a_missing_status_is_never_read_as_a_checked_empty_window():
    assert rb._news_lines({"news": []}) == [rb.NEWS_UNAVAILABLE_LINE]
    assert rb._news_lines({"news": [], "news_status": rb.NEWS_CHECKED}) == [rb.NEWS_EMPTY_LINE]
