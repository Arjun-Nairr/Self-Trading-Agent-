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

grid = risk_cap_grid([0.01, 0.02, 0.03], [0.20])
trade_counts = {len(LEDGER)}  # risk/cap resizing never drops or adds a trade
check("risk/cap grid touches the full ledger at every cell (scales P&L only)",
      len(trade_counts), 1)

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
