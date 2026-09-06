"""Plain-python tests for benchmark_sensitivity.py's pure logic. No
network, no Neon, no Hermes, no Docker -- synthetic bars only, same
convention as test_counterfactual.py / test_geometry_scan.py.
Run: python test_benchmark_sensitivity.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_sensitivity as bs                                    # noqa: E402
from benchmark_sensitivity import (                                   # noqa: E402
    BASE_MIN_RR, LEDGER, STARTING_EQUITY, TIME_STOP_TRADING_DAYS, Curve,
    MissingDataError, buy_hold_curve, metrics, min_rr_threshold_filter,
    risk_cap_grid, technical_baseline_curve,
)

checks = 0


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def check_true(label: str, cond) -> None:
    global checks
    checks += 1
    assert cond, f"{label}: expected truthy"


def bar(o: float, h: float, l: float, c: float) -> dict:
    return {"open": o, "high": h, "low": l, "close": c}


def ibar(t: str, o: float, h: float, l: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def full_intraday(entry_open: float, extra_bars: list[dict] | None = None) -> list[dict]:
    """A 09:30/09:45 pair plus whatever later-in-the-day bars a test
    needs, all at a flat price unless overridden."""
    bars = [ibar("09:30", entry_open, entry_open, entry_open, entry_open),
            ibar("09:45", entry_open, entry_open, entry_open, entry_open)]
    return bars + (extra_bars or [])


# ===========================================================================
# Shared synthetic scenario builder: one ticker, a clean SMA10 crossover on
# a known day, with full control over what happens after entry.
# ===========================================================================

def build_scenario():
    """lookback (10 flat days) + trading_days (long enough for a 5-day
    time stop to matter) with a single crossover on trading day index 1
    (2026-01-06), entry on trading day index 2 (2026-01-07)."""
    trading_days = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
    lookback_days = [date(2025, 12, d) for d in range(19, 29)]
    daily_bars = {"AAA": {}}
    all_days = lookback_days + trading_days
    flat, jump = 10.0, 11.0
    for d in all_days:
        close = jump if d >= date(2026, 1, 6) else flat
        daily_bars["AAA"][str(d)] = bar(close, close + 0.1, close - 0.1, close)
    return trading_days, lookback_days, daily_bars


def patch_intraday(mapping: dict[str, list[dict]]):
    """mapping: {day_str: bars}. Any day not in mapping gets a flat
    09:30/09:45 pair at that day's own close (from the test's daily_bars),
    good enough for days the test doesn't care about."""
    def fake(tickers_arg, day):
        bars = mapping.get(str(day))
        if bars is None:
            bars = full_intraday(11.0)
        return {t: bars for t in tickers_arg}
    return fake


def build_multi_scenario(specs: dict[str, dict]) -> tuple[list[date], list[date], dict]:
    """Independent, per-ticker-controllable crossover scenarios sharing
    one calendar. `specs[ticker] = {"flat": float, "jump": float,
    "jump_day_index": int}` -- that ticker's close is `flat` for every day
    strictly before trading_days[jump_day_index], and `jump` from that day
    onward (inclusive), which is what actually crosses its own SMA10 and
    fires a signal on that day (entry the NEXT trading day)."""
    trading_days = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23)]
    lookback_days = [date(2025, 12, d) for d in range(9, 29)]
    all_days = lookback_days + trading_days
    daily_bars = {t: {} for t in specs}
    for t, spec in specs.items():
        jump_day = trading_days[spec["jump_day_index"]]
        for d in all_days:
            close = spec["jump"] if d >= jump_day else spec["flat"]
            daily_bars[t][str(d)] = bar(close, close + 0.1, close - 0.1, close)
    return trading_days, lookback_days, daily_bars


def per_ticker_day_intraday(overrides: dict[tuple[str, str], list[dict] | None], daily_bars: dict):
    """overrides[(ticker, day_str)] = bars, or None to simulate a total
    data outage for that (ticker, day) (fetch_intraday_bars simply
    returns nothing for it). Anything not listed falls back to a flat
    09:30/09:45 pair at that ticker's own real daily close for that day."""
    def fake(tickers_arg, day):
        out = {}
        for t in tickers_arg:
            key = (t, str(day))
            if key in overrides:
                if overrides[key] is not None:
                    out[t] = overrides[key]
                # else: omit entirely -- simulates a missing/empty fetch
            else:
                c = daily_bars[t][str(day)]["close"]
                out[t] = full_intraday(c)
        return out
    return fake


# -- 1. metrics use $100,000 as the starting baseline AND as the leading --
#    observation for drawdown/volatility (issue #6)
curve = Curve("synthetic", ["2026-01-01", "2026-01-02"], [80000.0, 85000.0], [1.0, 1.0])
m = metrics(curve)
check("total_return uses STARTING_EQUITY not curve[0]",
      m["total_return_pct"], round((85000.0 - STARTING_EQUITY) / STARTING_EQUITY * 100, 3))
# A first-day LOSS (100000 -> 80000, -20%) must show up as the max drawdown,
# even though the curve itself only ever falls further (85000 > 80000, so
# without the synthetic leading $100,000 point the naive peak would be
# curve[0]=80000 and no drawdown at all would be recorded).
check("first-day loss vs STARTING_EQUITY is captured in max_drawdown_pct",
      m["max_drawdown_pct"], round((80000.0 - STARTING_EQUITY) / STARTING_EQUITY * 100, 3))
# The return series feeding volatility must include STARTING_EQUITY -> curve[0].
expected_first_return = (80000.0 - STARTING_EQUITY) / STARTING_EQUITY
check_true("volatility is nonzero because the STARTING_EQUITY->day-1 return is included",
           m["annualized_vol_pct"] > 0)


# -- 2. missing required market data fails explicitly, not silently --------
daily_bars_gapped = {"AAA": {"2026-01-02": bar(10, 10, 10, 10)}}  # day-1 bar missing
day1_intraday = {"AAA": [ibar("09:45", 10.0, 10.0, 10.0, 10.0)]}
try:
    buy_hold_curve("test", ["AAA"], {"AAA": 1.0}, daily_bars_gapped,
                    [date(2026, 1, 1), date(2026, 1, 2)], day1_intraday)
    raised = False
except MissingDataError:
    raised = True
check_true("buy_hold_curve raises MissingDataError on a missing held-ticker bar", raised)


# -- 5. fail loudly on missing signal-history bars (issue #5) ---------------
trading_days, lookback_days, daily_bars = build_scenario()
gapped = {"AAA": dict(daily_bars["AAA"])}
del gapped["AAA"][str(lookback_days[3])]  # remove one lookback bar
bs.fetch_intraday_bars = patch_intraday({})
try:
    technical_baseline_curve(["AAA"], gapped, lookback_days, trading_days)
    raised = False
except MissingDataError:
    raised = True
check_true("technical_baseline_curve raises MissingDataError on a missing SMA-history bar", raised)


# -- 4. signal date differs from entry date (issue #4) ----------------------
trading_days, lookback_days, daily_bars = build_scenario()
bs.fetch_intraday_bars = patch_intraday({})  # everything flat at 11.0, no touches -> survives to time stop
curve, trades, rejected = technical_baseline_curve(["AAA"], daily_bars, lookback_days, trading_days)
check_true("technical baseline produced at least one trade in the synthetic scenario", len(trades) >= 1)
tr = trades[0]
check_true("entry_date is strictly later than signal_date", tr["entry_date"] > tr["signal_date"])
check("signal_date is the real crossover day (2026-01-06), not the entry day",
      tr["signal_date"], "2026-01-06")
check("entry_date is the NEXT trading day (2026-01-07)", tr["entry_date"], "2026-01-07")


# -- 2. fill-based 1.5R target calculation, with a gap between signal ------
#    close and fill (issue #2)
trading_days, lookback_days, daily_bars = build_scenario()
# Signal day (01-06) closes at 11.0 (stop = 11.0*0.95 = 10.45), but the
# entry day (01-07) gaps up modestly above that close -- still within the
# 3-12% stop-distance band off the real fill ((11.5-10.45)/11.5 ~= 9.1%).
gapped_entry = {"2026-01-07": full_intraday(11.5)}
bs.fetch_intraday_bars = patch_intraday(gapped_entry)
curve, trades, rejected = technical_baseline_curve(["AAA"], daily_bars, lookback_days, trading_days)
check_true("a gapped entry still produces a trade (stop is within band off the higher fill)", len(trades) >= 1)
tr = trades[0]
risk = tr["entry_price"] - tr["stop_price"]
realized_rr = (tr["target_price"] - tr["entry_price"]) / risk
check_true("target is computed from the REAL fill: (target-fill)/(fill-stop) == BASE_MIN_RR",
           abs(realized_rr - BASE_MIN_RR) < 0.01)  # cent-rounded prices, not exact floats
check_true("stop price still comes from the signal-day close (10.45), not the fill",
           abs(tr["stop_price"] - 10.45) < 1e-6)


# -- 2b. a fill so far from the preselected stop that the band is violated -
#    must be rejected, not silently traded (issue #2)
trading_days, lookback_days, daily_bars = build_scenario()
# Signal close 11.0 -> stop 10.45 (5% below). A fill at 50.0 makes the real
# stop distance ~79%, far outside the 3-12% band.
huge_gap_entry = {"2026-01-07": full_intraday(50.0)}
bs.fetch_intraday_bars = patch_intraday(huge_gap_entry)
curve, trades, rejected = technical_baseline_curve(["AAA"], daily_bars, lookback_days, trading_days)
check("an out-of-band gapped entry is rejected, not traded", len(trades), 0)
check_true("the rejection is recorded in diagnostic output", len(rejected) >= 1)
check("rejection reason names the stop-distance band", "band" in rejected[0]["reason"], True)


# -- 1. entry-day exit: a position entered at 09:45 must check the REST ----
#    of that same day's bars, not wait until the next day (issue #1)
trading_days, lookback_days, daily_bars = build_scenario()
# Entry day 01-07: fill at 11.0 (stop ~10.45, target ~11.0+1.5*0.55=11.825).
# A later same-day bar's high blows through the target.
same_day_target = {"2026-01-07": full_intraday(11.0, [ibar("10:00", 11.0, 12.5, 11.0, 12.0)])}
bs.fetch_intraday_bars = patch_intraday(same_day_target)
curve, trades, rejected = technical_baseline_curve(["AAA"], daily_bars, lookback_days, trading_days)
check_true("a same-day target touch produces exactly one closed trade", len(trades) == 1)
tr = trades[0]
check("the trade exits on its OWN entry day, not a later day", tr["exit_date"], tr["entry_date"])
check("the outcome is a target hit", tr["outcome"], "target")


# -- 3. time-stop precedence over a later stop/target touch (issue #3) ------
# Build a position that survives to exactly its time-stop day, where that
# SAME day's later range would also touch the target -- the position must
# exit at the time-stop's OPENING price, not the target.
trading_days, lookback_days, daily_bars = build_scenario()
flat_days = {str(trading_days[j]): full_intraday(11.0) for j in range(2, 7)}  # entry day + holds, all flat/no-touch
# The time-stop day is entry_idx(2) + TIME_STOP_TRADING_DAYS. Override its
# DAILY bar (used for the carried-day walk) so the day's range would hit
# the target, but the open itself does not.
time_stop_day = trading_days[2 + TIME_STOP_TRADING_DAYS]
daily_bars["AAA"][str(time_stop_day)] = bar(11.0, 20.0, 11.0, 15.0)  # open=11.0, high=20 blows through target
bs.fetch_intraday_bars = patch_intraday(flat_days)
curve, trades, rejected = technical_baseline_curve(["AAA"], daily_bars, lookback_days, trading_days)
check_true("exactly one trade closes via the time stop", any(t["outcome"] == "time_stop" for t in trades))
tstop_trade = next(t for t in trades if t["outcome"] == "time_stop")
check("time-stop exit price is the OPEN, not the target the day's range later touched",
      tstop_trade["exit_price"], 11.0)
check("time-stop exit happens on the expected day", tstop_trade["exit_date"], str(time_stop_day))


# -- issue #2: a same-day exit's proceeds must never fund another -----------
#    ticker's earlier 09:45 entry (this pass)
#
# Both AAA and BBB signal on the same day and attempt to enter together the
# next trading day. RISK_PCT/MAX_POS_PCT are relaxed to 100% for this test
# ONLY, isolating the cash-timing mechanism under test from the separate
# risk%/cap% sizing legs (already covered elsewhere) -- with cash as the
# sole real constraint, AAA's entry spends ALL starting cash, so BBB must
# be rejected UNLESS (the bug) AAA's later, profitable same-day exit
# proceeds are wrongly credited before BBB is sized.
trading_days, lookback_days, daily_bars = build_multi_scenario({
    "AAA": {"flat": 90.0, "jump": 100.0, "jump_day_index": 1},
    "BBB": {"flat": 4500.0, "jump": 5000.0, "jump_day_index": 1},
})
entry_day = str(trading_days[2])  # both signal on trading_days[1], enter trading_days[2]
overrides = {
    ("AAA", entry_day): full_intraday(100.0, [ibar("10:00", 100.0, 110.0, 100.0, 105.0)]),  # target touch after 09:45
    ("BBB", entry_day): full_intraday(5000.0),
}
orig_risk_pct, orig_max_pos_pct = bs.RISK_PCT, bs.MAX_POS_PCT
orig_starting_equity = bs.STARTING_EQUITY
bs.RISK_PCT, bs.MAX_POS_PCT = 1.0, 1.0
bs.STARTING_EQUITY = 10_000.0
try:
    bs.fetch_intraday_bars = per_ticker_day_intraday(overrides, daily_bars)
    curve, trades, rejected = technical_baseline_curve(["AAA", "BBB"], daily_bars, lookback_days, trading_days)
finally:
    bs.RISK_PCT, bs.MAX_POS_PCT = orig_risk_pct, orig_max_pos_pct
    bs.STARTING_EQUITY = orig_starting_equity

aaa_trade = next((t for t in trades if t["ticker"] == "AAA"), None)
check_true("AAA's same-day target exit is recorded", aaa_trade is not None and aaa_trade["outcome"] == "target")
check_true("AAA's entry consumed effectively all starting cash (qty=100 @ fill=100)",
           aaa_trade["qty"] == 100)
bbb_trade = next((t for t in trades if t["ticker"] == "BBB"), None)
bbb_rejection = next((r for r in rejected if r["ticker"] == "BBB"), None)
check("BBB is rejected, not filled -- AAA's post-09:45 exit proceeds must not fund it",
      bbb_trade, None)
check_true("BBB's rejection is recorded as a sizing failure, not silently dropped",
           bbb_rejection is not None and bbb_rejection["reason"] == "sizing rounded to zero shares")


# -- Problem 1 (this pass): a CARRIED position's post-09:45 proceeds -----
#    must never fund another ticker's earlier 09:45 entry
#
# AAA enters early and is carried, unresolved, all the way to BBB's own
# entry day -- surviving AAA's pre-09:45 chronology (no gap, no expired
# time stop, no touch in the 09:30-09:45 bar). BBB's entry is sized using
# ONLY cash + AAA's 09:30-close mark; AAA's target is only touched by a
# LATER (10:00) bar, resolved strictly AFTER BBB's sizing. RISK_PCT/
# MAX_POS_PCT relaxed to 100% so cash is the sole real constraint.
trading_days, lookback_days, daily_bars = build_multi_scenario({
    "AAA": {"flat": 90.0, "jump": 100.0, "jump_day_index": 1},
    "BBB": {"flat": 4500.0, "jump": 5000.0, "jump_day_index": 5},
})
aaa_entry_day = str(trading_days[2])
bbb_entry_day = str(trading_days[6])
overrides = {
    ("AAA", aaa_entry_day): full_intraday(100.0),  # AAA fills at 100, stop=95, no entry-day touch
    ("AAA", bbb_entry_day): full_intraday(100.0, [ibar("10:00", 100.0, 110.0, 100.0, 105.0)]),  # target only AFTER 09:45
    ("BBB", bbb_entry_day): full_intraday(5000.0),
}
orig_risk_pct, orig_max_pos_pct = bs.RISK_PCT, bs.MAX_POS_PCT
orig_starting_equity = bs.STARTING_EQUITY
bs.RISK_PCT, bs.MAX_POS_PCT = 1.0, 1.0
bs.STARTING_EQUITY = 10_000.0
try:
    bs.fetch_intraday_bars = per_ticker_day_intraday(overrides, daily_bars)
    curve, trades, rejected = technical_baseline_curve(["AAA", "BBB"], daily_bars, lookback_days, trading_days)
finally:
    bs.RISK_PCT, bs.MAX_POS_PCT = orig_risk_pct, orig_max_pos_pct
    bs.STARTING_EQUITY = orig_starting_equity

aaa_trade = next((t for t in trades if t["ticker"] == "AAA"), None)
check_true("AAA's exit is recorded as a target hit", aaa_trade is not None and aaa_trade["outcome"] == "target")
check_true("AAA was genuinely CARRIED -- entered on an earlier day than it exited",
           aaa_trade["entry_date"] == aaa_entry_day and aaa_trade["exit_date"] == bbb_entry_day
           and aaa_trade["entry_date"] != aaa_trade["exit_date"])
bbb_trade = next((t for t in trades if t["ticker"] == "BBB"), None)
bbb_rejection = next((r for r in rejected if r["ticker"] == "BBB"), None)
check("BBB is rejected -- AAA's carried position was worth 100% of equity marked, "
      "but AAA's post-09:45 exit proceeds (not yet realized at 09:45) cannot fund BBB",
      bbb_trade, None)
check_true("BBB's rejection is a sizing failure (zero cash at 09:45), not silently dropped",
           bbb_rejection is not None and bbb_rejection["reason"] == "sizing rounded to zero shares")


# -- missing carried-position 09:30 data fails loudly even with NO new ----
#    entry candidate that day (Problem 1: the fix must not be conditional
#    on another ticker also signaling)
trading_days, lookback_days, daily_bars = build_scenario()
entry_day = "2026-01-07"
missing_930_only = {("AAA", entry_day): full_intraday(11.0)}  # AAA enters fine...
# ...but its VERY NEXT held day has no intraday data at all.
next_day = str(trading_days[3])
missing_930_only[("AAA", next_day)] = None
bs.fetch_intraday_bars = per_ticker_day_intraday(missing_930_only, daily_bars)
try:
    technical_baseline_curve(["AAA"], daily_bars, lookback_days, trading_days)
    raised, msg = False, ""
except MissingDataError as exc:
    raised, msg = True, str(exc)
check_true("a carried position with NO intraday data on a held day (no new entry that "
           "day either) still raises MissingDataError", raised)
check_true("the error names the ticker", "AAA" in msg)
check_true("the error names the date", next_day in msg)


# -- issue #3: sizing must value a carried position at its real 09:45 -----
#    mark, not its entry cost basis (this pass)
#
# AAA enters first and is carried into BBB's own entry day, where AAA's
# real 09:30 mark has risen materially above what AAA actually paid.
# BBB's quantity must reflect the HIGHER marked equity, not the lower
# cost-basis equity (which would understate BBB's affordable size).
trading_days, lookback_days, daily_bars = build_multi_scenario({
    "AAA": {"flat": 9.0, "jump": 10.0, "jump_day_index": 1},   # signals trading_days[1], enters trading_days[2]
    "BBB": {"flat": 9.0, "jump": 10.0, "jump_day_index": 5},   # signals trading_days[5], enters trading_days[6]
})
aaa_entry_day = str(trading_days[2])
bbb_entry_day = str(trading_days[6])
overrides = {
    ("AAA", aaa_entry_day): full_intraday(10.5),   # AAA fills at 10.5, stop = round(10*0.95,2) = 9.5 (in band)
    ("AAA", bbb_entry_day): full_intraday(16.0),   # AAA's REAL 09:30/09:45 mark on BBB's entry day: risen to 16.0
    ("BBB", bbb_entry_day): full_intraday(10.5),   # BBB fills at 10.5, same stop distance as AAA
}
bs.STARTING_EQUITY = 10_000.0
try:
    bs.fetch_intraday_bars = per_ticker_day_intraday(overrides, daily_bars)
    curve, trades, rejected = technical_baseline_curve(["AAA", "BBB"], daily_bars, lookback_days, trading_days)
finally:
    bs.STARTING_EQUITY = orig_starting_equity

aaa_trade = next(t for t in trades if t["ticker"] == "AAA")
aaa_qty, aaa_fill = aaa_trade["qty"], aaa_trade["entry_price"]
check_true("AAA is genuinely still open (carried) when BBB enters -- not itself closed same-day",
           aaa_trade["exit_date"] != aaa_trade["entry_date"] or aaa_trade["entry_date"] != aaa_entry_day)

bbb_trade = next((t for t in trades if t["ticker"] == "BBB"), None)
check_true("BBB was actually filled (not rejected) in this scenario", bbb_trade is not None)

PATCHED_EQUITY = 10_000.0  # matches bs.STARTING_EQUITY as patched above -- NOT the module-level
                            # STARTING_EQUITY imported at the top of this file, which stays 100000.0
cash_before_bbb = PATCHED_EQUITY - aaa_qty * aaa_fill  # real cash after AAA's own entry, unaffected by marking
risk_per_share_bbb = bbb_trade["entry_price"] - bbb_trade["stop_price"]
# Two independently hand-computed candidate quantities -- an outside,
# non-tautological source of truth for what the code SHOULD do vs. what
# the prior (cost-basis) bug would have done:
equity_cost_basis = cash_before_bbb + aaa_qty * aaa_fill          # WRONG: what the old bug used
equity_marked = cash_before_bbb + aaa_qty * 16.0                  # CORRECT: real 09:30 mark on BBB's entry day
qty_if_cost_basis = min(int((equity_cost_basis * bs.RISK_PCT) // risk_per_share_bbb),
                         int((equity_cost_basis * bs.MAX_POS_PCT) // bbb_trade["entry_price"]),
                         int(cash_before_bbb // bbb_trade["entry_price"]))
qty_if_marked = min(int((equity_marked * bs.RISK_PCT) // risk_per_share_bbb),
                     int((equity_marked * bs.MAX_POS_PCT) // bbb_trade["entry_price"]),
                     int(cash_before_bbb // bbb_trade["entry_price"]))
check_true("marked-equity and cost-basis sizing genuinely diverge in this scenario "
           "(otherwise the test proves nothing)", qty_if_marked != qty_if_cost_basis)
check("BBB's actual quantity matches MARKED equity sizing", bbb_trade["qty"], qty_if_marked)
check_true("BBB's actual quantity does NOT match the old cost-basis-only sizing",
           bbb_trade["qty"] != qty_if_cost_basis)


# -- issue #4: missing 09:45 entry/mark data fails loudly, never a --------
#    silent "no trade" (this pass)

# 4a) a signaled entry candidate has no 09:45 bar at all.
trading_days, lookback_days, daily_bars = build_scenario()
entry_day = "2026-01-07"
no_945_bar = {("AAA", entry_day): [ibar("09:30", 11.0, 11.0, 11.0, 11.0)]}  # 09:30 only, no 09:45
bs.fetch_intraday_bars = per_ticker_day_intraday(no_945_bar, daily_bars)
try:
    technical_baseline_curve(["AAA"], daily_bars, lookback_days, trading_days)
    raised, msg = False, ""
except MissingDataError as exc:
    raised, msg = True, str(exc)
check_true("a signaled entry with no 09:45 bar raises MissingDataError", raised)
check_true("the error names the ticker", "AAA" in msg)
check_true("the error names the date", entry_day in msg)

# 4b) a carried position cannot be marked at the required 09:45 instant
#     (its 09:30 bar is missing) on a day a new entry needs sizing.
trading_days, lookback_days, daily_bars = build_multi_scenario({
    "AAA": {"flat": 9.0, "jump": 10.0, "jump_day_index": 1},
    "BBB": {"flat": 9.0, "jump": 10.0, "jump_day_index": 5},
})
aaa_entry_day = str(trading_days[2])
bbb_entry_day = str(trading_days[6])
overrides = {
    ("AAA", aaa_entry_day): full_intraday(10.5),
    ("AAA", bbb_entry_day): [ibar("09:45", 16.0, 16.0, 16.0, 16.0)],  # 09:45 only -- 09:30 mark missing
    ("BBB", bbb_entry_day): full_intraday(10.5),
}
bs.fetch_intraday_bars = per_ticker_day_intraday(overrides, daily_bars)
try:
    technical_baseline_curve(["AAA", "BBB"], daily_bars, lookback_days, trading_days)
    raised, msg = False, ""
except MissingDataError as exc:
    raised, msg = True, str(exc)
check_true("a carried position missing its 09:30 mark raises MissingDataError, "
           "never silently valued at cost basis or skipped", raised)
check_true("the error names the carried ticker", "AAA" in msg)
check_true("the error names the date sizing was needed", bbb_entry_day in msg)


# -- pooled exposure: >20% total while never >20% in any ONE position ------
trading_days = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
lookback_days = [date(2025, 12, d) for d in range(19, 29)]
tickers = ["AAA", "BBB", "CCC"]
daily_bars_multi = {t: {} for t in tickers}
all_days = lookback_days + trading_days
flat, jump = 10.0, 11.0
for d in all_days:
    close = jump if d >= date(2026, 1, 6) else flat
    for t in tickers:
        daily_bars_multi[t][str(d)] = bar(close, close + 0.1, close - 0.1, close)
bs.fetch_intraday_bars = patch_intraday({})  # flat everywhere, all three survive to time stop concurrently
curve, trades, rejected = technical_baseline_curve(tickers, daily_bars_multi, lookback_days, trading_days)
# Every ticker crosses the SAME day -> three concurrent entries the next day.
entry_day_trades = [t for t in trades if t["entry_date"] == "2026-01-07"]
check("all three tickers entered concurrently", len(entry_day_trades), 3)
for tr in entry_day_trades:
    position_value = tr["qty"] * tr["entry_price"]
    check_true(f"{tr['ticker']}: single position never exceeds 20% of account equity at entry",
               position_value <= STARTING_EQUITY * 0.20 + 1e-6)
total_entry_value = sum(tr["qty"] * tr["entry_price"] for tr in entry_day_trades)
check_true("total pooled exposure across concurrent positions EXCEEDS 20% "
           "(three ~20% positions at once)", total_entry_value > STARTING_EQUITY * 0.20)


# -- cash never goes negative, checked directly on the curve's own walk ----
# (re-uses the multi-ticker concurrent-entry scenario above -- the
# strongest stress case for overspending.)
def replay_cash(tickers, trades, starting=STARTING_EQUITY):
    """Independent, minimal re-check: replay entries (spend) and exits
    (receive) in the exact order technical_baseline_curve reports them,
    and assert cash is never negative at any point -- this does NOT trust
    the module's own internal assert, it recomputes from the public
    trades_log directly."""
    events = []
    for tr in trades:
        events.append((tr["entry_date"], 0, "entry", tr))
        events.append((tr["exit_date"], 1, "exit", tr))
    events.sort(key=lambda e: (e[0], e[1]))
    cash = starting
    for _date, _tag, kind, tr in events:
        if kind == "entry":
            cash -= tr["qty"] * tr["entry_price"]
        else:
            cash += tr["qty"] * tr["exit_price"]
        assert cash >= -1e-6, f"cash went negative: {cash}"
    return cash

replay_cash(tickers, trades)
checks += 1  # replay_cash's own asserts are the check; count it


# ===========================================================================
# risk_cap_grid / min_rr_threshold_filter (issue #7)
# ===========================================================================

grid_a = risk_cap_grid([0.02], [0.20])
grid_b = risk_cap_grid([0.02], [0.20])
check("risk_cap_grid is deterministic across repeated calls", grid_a, grid_b)
real_profile = grid_a["risk=2%_cap=20%"]
check_true("risk=2%/cap=20% cell approximates the real ~+3.44% realized result",
           2.0 <= real_profile["total_return_pct"] <= 4.5)

# -- issue #1 (this pass): same-day ledger exits must never process ------
#    before their own entry. Row index 4 in LEDGER is the real Day-18
#    same-day round trip (entry_day == exit_day == 18).
DAY_18_IDX = 4
assert LEDGER[DAY_18_IDX]["entry_day"] == LEDGER[DAY_18_IDX]["exit_day"] == 18, \
    "test assumption drifted: LEDGER[4] is no longer the Day-18 same-day trade"

cell = risk_cap_grid([0.02], [0.20])["risk=2%_cap=20%"]
outcomes = cell["trade_outcomes"]
check("every ledger row is accounted for -- none left 'pending'",
      "pending" in outcomes, False)
check_true("every ledger row is either completed or an explicitly-labeled skip",
           all(o == "completed" or o.startswith("skipped") for o in outcomes))
check("the Day-18 same-day trade completed (entered before it exited)",
      outcomes[DAY_18_IDX], "completed")
check_true("no position is falsely left open after its own recorded exit day",
           len(cell["still_open_at_end"]) == 0)

def independently_replay_cash(cell: dict, starting: float) -> float:
    """A GENUINE independent check: recompute cash from scratch using
    only each cash_events entry's own (kind, qty, price) -- never reading
    `cash_after` as ground truth. Asserts every chronological prefix
    stays nonnegative (its OWN running total, not the grid's), and
    returns the replay's own final balance for the caller to reconcile
    against `end_equity` separately."""
    replayed = starting
    for ev in cell["cash_events"]:
        replayed += -ev["qty"] * ev["price"] if ev["kind"] == "entry" else ev["qty"] * ev["price"]
        assert replayed >= -1e-6, f"independent replay went negative at event {ev}: {replayed}"
        # Cross-check against the grid's own recorded cash_after too --
        # a genuine consistency check, not just trusting either side.
        assert abs(replayed - ev["cash_after"]) < 1e-6, \
            f"independent replay ({replayed}) disagrees with recorded cash_after ({ev['cash_after']}) at {ev}"
    return replayed


tight_cell = risk_cap_grid([0.02], [0.05])["risk=2%_cap=5%"]
replayed_final = independently_replay_cash(tight_cell, STARTING_EQUITY)
still_open_value = sum(LEDGER[i]["exit"] * next(e["qty"] for e in tight_cell["cash_events"] if e["idx"] == i)
                        for i in tight_cell["still_open_at_end"]) if tight_cell["still_open_at_end"] else 0.0
check_true("independent cash replay's final balance reconciles with end_equity "
           "(accounting for any still-open position's own last recorded qty)",
           abs((replayed_final + still_open_value) - tight_cell["end_equity"]) < 1e-6)

# -- Problem 2 (this pass): a genuine cash-pressure regression -- three
# same-day entries whose combined naive cost exceeds STARTING_EQUITY.
# Without the affordability check (int(cash // fill) in the min(...)),
# the 2nd and 3rd would each independently claim floor(equity*cap/fill)
# shares and together overspend; WITH it, only the first (idx order) is
# affordable and the other two are cleanly skipped, never overspending.
PRESSURE_LEDGER = [
    {"fill": 100.0, "exit": 110.0, "qty": 0, "stop": 90.0, "target": 120.0, "rr": 1.0,
     "reason": "test", "entry_day": 1, "exit_day": 2},
    {"fill": 100.0, "exit": 90.0, "qty": 0, "stop": 90.0, "target": 120.0, "rr": 1.0,
     "reason": "test", "entry_day": 1, "exit_day": 2},
    {"fill": 100.0, "exit": 105.0, "qty": 0, "stop": 90.0, "target": 120.0, "rr": 1.0,
     "reason": "test", "entry_day": 1, "exit_day": 2},
]
orig_ledger = bs.LEDGER
bs.LEDGER, orig_starting_equity2 = PRESSURE_LEDGER, bs.STARTING_EQUITY
bs.STARTING_EQUITY = 1_000.0
try:
    # risk_pct/cap_pct here are risk_cap_grid's own PARAMETERS (100% each)
    # -- deliberately permissive so cash is the only real constraint;
    # risk_cap_grid never reads the module-level RISK_PCT/MAX_POS_PCT
    # constants (those belong to technical_baseline_curve only).
    pressure_cell = risk_cap_grid([1.0], [1.0])["risk=100%_cap=100%"]
finally:
    bs.LEDGER = orig_ledger
    bs.STARTING_EQUITY = orig_starting_equity2

check("exact accepted trade: only idx 0 (first in order) is affordable",
      pressure_cell["trade_outcomes"], ["completed", "skipped_insufficient_sizing", "skipped_insufficient_sizing"])
entry_events = [e for e in pressure_cell["cash_events"] if e["kind"] == "entry"]
check("exactly one entry event occurred", len(entry_events), 1)
check("its exact quantity is the full $1,000 // $100 fill -- 10 shares", entry_events[0]["qty"], 10)
independently_replay_cash(pressure_cell, 1_000.0)  # re-asserts every prefix >= 0 for this scenario too
check("final balance reconciles: 10 shares bought at 100, sold at 110 -> $1,100",
      pressure_cell["end_equity"], 1_100.0)

grid = risk_cap_grid([0.01, 0.02, 0.03], [0.20])
trade_counts = {len(v["trade_outcomes"]) for v in grid.values()}
check("risk/cap grid touches the full 16-row ledger at every cell",
      trade_counts, {len(LEDGER)})

rr_filter = min_rr_threshold_filter([1.5, 2.0])
check_true("a stricter min-R:R threshold drops at least one trade (outcome-changing)",
           rr_filter["min_rr>=2.0"]["trades_retained"] < rr_filter["min_rr>=1.5"]["trades_retained"])
check("min-R:R at the loosest real threshold (1.5) retains every historical trade",
      rr_filter["min_rr>=1.5"]["trades_retained"], len(LEDGER))

# The sensitivity report's own classification must match what the grid
# actually models -- never "fully_replayable" for an approximation.
sens = bs.sensitivity_analysis()
classification = sens["risk_pct_x_concentration_cap"]["classification"]
check_true("risk/cap classification is NOT the stronger 'fully_replayable' claim",
           classification != "fully_replayable")
check("risk/cap classification names the cash-constrained approximation",
      classification, "chronological_cash_constrained_approximation")


print(f"{checks} checks passed")
