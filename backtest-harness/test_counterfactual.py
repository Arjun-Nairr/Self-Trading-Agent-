"""Plain-python tests for counterfactual.py's pure `_resolve_from_bars`
core (and the small pure helpers around it). No network, no Neon, no
Hermes, no Docker -- synthetic bars only. Run: python test_counterfactual.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from counterfactual import (                                          # noqa: E402
    _resolve_from_bars, aggregate, json_ticker_from_line, next_trading_day,
)
from run_backtest import TIME_STOP_TRADING_DAYS, _slipped_buy_price      # noqa: E402

checks = 0


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def bar(t: str, o: float, h: float, l: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


# -- next_trading_day -------------------------------------------------------
days = [date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)]
check("next_trading_day finds the day after", next_trading_day(days, date(2026, 3, 1)), date(2026, 3, 2))
check("next_trading_day returns None past the end", next_trading_day(days, date(2026, 3, 3)), None)

# -- json_ticker_from_line ----------------------------------------------
check("json_ticker_from_line splits on the first colon",
      json_ticker_from_line("ALGM: decision-time risk check rejected -- x"), "ALGM")
check("json_ticker_from_line handles no colon", json_ticker_from_line("no colon here"), None)

# -- _resolve_from_bars: entry-day stop hit ----------------------------------
entry_price = 100.0
stop, target = 95.0, 110.0
entry_bars = [bar("09:45", 99.0, 99.5, 94.0, 94.5)]  # low breaches stop same bar
outcome = _resolve_from_bars("TICK", "2026-03-01", "2026-03-02", entry_price, stop, target,
                             entry_bars, [])
check("entry-day stop hit -> outcome", outcome.outcome, "stop")
check("entry-day stop hit -> days_held 0", outcome.days_held, 0)
check("entry-day stop hit -> r_multiple is -1 (full stop distance)", outcome.r_multiple, -1.0)

# -- _resolve_from_bars: entry-day target hit, stop checked first --------
# A single bar whose range crosses BOTH stop and target -- must resolve to
# stop (evaluate_bar_stop_target's own documented conservative tie-break).
entry_bars_both = [bar("09:45", 100.0, 111.0, 94.0, 105.0)]
outcome = _resolve_from_bars("TICK", "2026-03-01", "2026-03-02", entry_price, stop, target,
                             entry_bars_both, [])
check("bar crossing both stop and target resolves to stop (conservative)", outcome.outcome, "stop")

# -- _resolve_from_bars: target hit on a later day -----------------------
entry_bars_flat = [bar("09:45", 100.0, 101.0, 99.0, 100.5)]
subsequent = [
    ("2026-03-03", {"open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0}),
    ("2026-03-04", {"open": 102.0, "high": 111.0, "low": 101.0, "close": 110.5}),  # crosses target
]
outcome = _resolve_from_bars("TICK", "2026-03-01", "2026-03-02", entry_price, stop, target,
                             entry_bars_flat, subsequent)
check("later-day target hit -> outcome", outcome.outcome, "target")
check("later-day target hit -> exit_date", outcome.exit_date, "2026-03-04")
check("later-day target hit -> days_held", outcome.days_held, 2)
check("later-day target hit -> exit price is exactly the target (no slippage on exits)",
      outcome.exit_price, target)
check("later-day target hit -> r_multiple is +2.0 (10pt reward / 5pt risk)", outcome.r_multiple, 2.0)

# -- _resolve_from_bars: opening gap through the stop --------------------
subsequent_gap = [("2026-03-03", {"open": 90.0, "high": 90.5, "low": 89.0, "close": 89.5})]
outcome = _resolve_from_bars("TICK", "2026-03-01", "2026-03-02", entry_price, stop, target,
                             entry_bars_flat, subsequent_gap)
check("opening gap through stop -> outcome", outcome.outcome, "stop")
check("opening gap through stop -> fills at the worse of open/stop (min)",
      outcome.exit_price, min(90.0, stop))

# -- _resolve_from_bars: time stop at exactly TIME_STOP_TRADING_DAYS -----
flat_bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
subsequent_flat = [(f"2026-03-{3 + i:02d}", flat_bar) for i in range(TIME_STOP_TRADING_DAYS)]
outcome = _resolve_from_bars("TICK", "2026-03-01", "2026-03-02", entry_price, stop, target,
                             entry_bars_flat, subsequent_flat)
check("time stop fires at exactly TIME_STOP_TRADING_DAYS", outcome.outcome, "time_stop")
check("time stop fires on the right day_offset", outcome.days_held, TIME_STOP_TRADING_DAYS)
check("time stop exits at that day's open", outcome.exit_price, 100.0)

# -- _resolve_from_bars: window ends before a resolution --------------------
outcome = _resolve_from_bars("TICK", "2026-03-01", "2026-03-02", entry_price, stop, target,
                             entry_bars_flat, subsequent_flat[:3])
check("data window ending early -> window_end", outcome.outcome, "window_end")
check("window_end has no exit price", outcome.exit_price, None)
check("window_end still reports MFE/MAE", outcome.mfe_r is not None, True)

# -- _resolve_from_bars: missing a day's bar mid-hold fails closed, not
# fabricated. ----------------------------------------------------------
subsequent_missing = [("2026-03-03", flat_bar), ("2026-03-04", None)]
outcome = _resolve_from_bars("TICK", "2026-03-01", "2026-03-02", entry_price, stop, target,
                             entry_bars_flat, subsequent_missing)
check("missing daily bar mid-hold -> no_data, not a fabricated exit", outcome.outcome, "no_data")
check("missing daily bar mid-hold -> days_held counts only the resolved days", outcome.days_held, 1)

# -- _resolve_from_bars: non-positive risk (stop at/above entry) fails
# closed rather than dividing by zero/negative. --------------------------
outcome = _resolve_from_bars("TICK", "2026-03-01", "2026-03-02", 100.0, 100.0, 110.0, [], [])
check("stop at entry price -> no_data (zero risk, not a free trade)", outcome.outcome, "no_data")

# -- _slipped_buy_price mirrors execution-sim's real MARKET-fill formula
# exactly (5bps against direction, clamped to the bar range). -----------
b = bar("09:45", 100.0, 100.5, 99.5, 100.2)
expected = round(min(max(100.0 + 100.0 * 0.0005, b["low"]), b["high"]), 2)
check("_slipped_buy_price applies 5bps and clamps to bar range",
      _slipped_buy_price(b["open"], b), expected)

# -- aggregate -------------------------------------------------------------
class FakeOutcome:
    def __init__(self, outcome, r_multiple, days_held):
        self.outcome = outcome
        self.r_multiple = r_multiple
        self.days_held = days_held


outcomes = [FakeOutcome("target", 2.0, 3), FakeOutcome("stop", -1.0, 1), FakeOutcome("no_data", None, None)]
agg = aggregate(outcomes)
check("aggregate n", agg["n"], 3)
check("aggregate n_resolved excludes unresolved", agg["n_resolved"], 2)
check("aggregate win_rate", agg["win_rate"], 0.5)
check("aggregate avg_r", agg["avg_r"], 0.5)

# -- regression: fetch_run1_rejected_proposals' two cycle-date-matching
# rules (agent_activity events use nearest-PRECEDING cycle_start;
# trades-table rows use nearest-FOLLOWING) -- see counterfactual.py's own
# real_date_for_event/real_date_for_trade docstrings for why these differ.
# Reimplemented here as pure Python (the real logic runs inside a
# container-side script string, not importable directly) against
# SFM's real ground-truth timestamps from run 1, to guard against
# reintroducing the off-by-one-cycle bug caught during round-2 prep.
def _date_from(desc):
    import re
    m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", desc)
    return m.group(1) if m else None


def _preceding(ts, cycle_rows):
    best = None
    for ts_c, desc in cycle_rows:
        if ts_c <= ts:
            best = desc
        else:
            break
    return _date_from(best) if best else None


def _following(ts, cycle_rows):
    for ts_c, desc in cycle_rows:
        if ts_c >= ts:
            return _date_from(desc)
    return None


cycle_rows = [
    (1000, "Day 28 (2026-03-16) -- reconciling and fetching data"),
    (1100, "Day 29 (2026-03-17) -- reconciling and fetching data"),
]
# A trades-table row is written mid-cycle, BEFORE its own cycle's
# cycle_start batch -- e.g. ts=1050 (between the two cycle_starts) really
# belongs to Day 29, not Day 28.
check("trade row mid-cycle maps to the FOLLOWING cycle_start (Day 29)",
      _following(1050, cycle_rows), "2026-03-17")
check("naive PRECEDING match would wrongly give Day 28 -- documenting the bug this replaced",
      _preceding(1050, cycle_rows), "2026-03-16")
# An agent_activity event (risk_check/order_queued) is written in the SAME
# end-of-cycle batch as its own cycle_start, always AFTER it -- e.g.
# ts=1105 (just after Day 29's cycle_start at 1100) belongs to Day 29.
check("agent_activity event just after its own cycle_start maps to the PRECEDING one (Day 29)",
      _preceding(1105, cycle_rows), "2026-03-17")

print(f"OK — {checks} checks passed")
