"""
benchmark_sensitivity.py — offline, read-only benchmark + sensitivity
diagnostic (Round 2 post-hoc evaluation).

Answers: (1) did Hermes outperform simple alternatives over the identical
period, and (2) which RULES.md numeric parameters most affect the 16-trade
Round 2 ledger, using only mechanically recomputable data.

HARD BOUNDARY: human-evaluation-only output, exactly like geometry_scan.py
and counterfactual.py. Never wired into run_backtest.py, never read by
Hermes, never referenced in AGENTS.md or any prompt. All market-data access
is read-only (yfinance via maverick-mcp, Alpaca calendar via alpaca-mcp,
SELECT-only against Neon's portfolio_snapshots for Hermes' own equity
curve). Never resumes, resets, or advances any backtest run, and never
touches trades/agent_activity/backtest_state.json.

Uses REAL tickers throughout (SPY + the real 10-ticker watchlist) — these
benchmarks are independent of Hermes' own masked run and are not
Hermes-facing, so RULES.md §8's real symbols apply directly, same as
elsewhere in this file's own imports. No masked-alias mapping is ever
reconstructed here — see docs/evaluations/round2_benchmark_sensitivity.md's
Unknowns section for what that would have unlocked and why it was skipped.

Run: python benchmark_sensitivity.py   (writes .benchmark_sensitivity_data.json)
Test: python test_benchmark_sensitivity.py
"""
import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import pstdev

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_scan import fetch_trading_days                           # noqa: E402
from run_backtest import (                                             # noqa: E402
    REPO, STARTING_EQUITY, TIME_STOP_TRADING_DAYS, _slipped_buy_price,
    evaluate_bar_stop_target, evaluate_opening_gap, fetch_bars,
    fetch_intraday_bars, json_from, run_in,
)

RUN_ID = "e68525f9-ce89-4588-a57e-866f28cd99fd"
PERIOD_START = date(2026, 2, 17)
# The last of the 50 REAL cycles actually evaluated -- verified below by
# converting portfolio_snapshots.day_label ("Day N" = PERIOD_START + N-1
# calendar days, confirmed empirically: every one of the 50 snapshot rows'
# converted dates is a real weekday and lands on 2026-04-28 at the latest).
# 2026-04-29 (HANDOFF.md's "cursor") is the NEXT, not-yet-run trading day --
# using it here would silently compare Hermes' 50 cycles against a 51-day
# benchmark window, a real off-by-one this constant exists to prevent.
PERIOD_END = date(2026, 4, 28)
SPY = "SPY"

# RULES.md §2/§6, current aggressive Phase 1 profile (RULES.md §10) —
# mirrored as constants for the same reason geometry_scan.py mirrors them:
# independently checkable against RULES.md, not trusting this file's wiring.
RISK_PCT = 0.02
MAX_POS_PCT = 0.20
BASE_MIN_RR = 1.5
# Technical baseline's OWN stop rule (see "technical baseline definition"
# below) — deliberately a fixed 5% below the signal day's close (the
# midpoint of RULES.md §6a's 3-12% band), not maverick's real support/
# resistance, to keep this baseline computable from the same daily bars
# already fetched for buy-hold, and to keep it un-tuned to this period.
BASELINE_STOP_PCT = 0.05


class MissingDataError(RuntimeError):
    """Raised instead of silently marking a held/weighted position at $0
    or skipping it — a missing bar for a ticker the curve is currently
    exposed to invalidates that day's equity number outright."""


def watchlist_tickers() -> list[str]:
    return [t["symbol"] for t in json.loads((REPO / "watchlist" / "watchlist.json").read_text())["tickers"]]


# ---------------------------------------------------------------------
# data fetch (all read-only)
# ---------------------------------------------------------------------

def fetch_daily_range(tickers: list[str], days: list[date]) -> dict[str, dict[str, dict]]:
    """{ticker: {day_str: bar}} across `days`. One fetch_bars call per day
    (already batches every ticker) — same call geometry_scan.py uses."""
    out: dict[str, dict[str, dict]] = {t: {} for t in tickers}
    for i, day in enumerate(days):
        print(f"  daily bars [{i + 1}/{len(days)}] {day}", file=sys.stderr)
        bars = fetch_bars(tickers, day)
        for t, b in bars.items():
            out[t][str(day)] = b
    return out


def fetch_hermes_equity_curve() -> list[dict]:
    """Real, run-scoped, SELECT-only read of portfolio_snapshots — Hermes'
    own authoritative daily equity, exposure_pct and drawdown_pct, in
    cycle order. No ticker identity involved at all. `real_date` is
    derived from day_label ("Day N" = PERIOD_START + N-1 calendar days —
    verified empirically in main(), not assumed here)."""
    script = f'''
import asyncio, json
from server import get_pool

async def main():
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT day_label, equity, exposure_pct, drawdown_pct, created_at FROM portfolio_snapshots "
        "WHERE run_id=$1 AND mode='backtest' ORDER BY created_at", "{RUN_ID}")
    print(json.dumps([
        {{"day_label": r["day_label"], "equity": float(r["equity"]),
          "exposure_pct": float(r["exposure_pct"]) if r["exposure_pct"] is not None else None,
          "drawdown_pct": float(r["drawdown_pct"]) if r["drawdown_pct"] is not None else None,
          "created_at": r["created_at"].isoformat()}}
        for r in rows
    ]))

asyncio.run(main())
'''
    rows = json_from_list(run_in("trade-journal", script))
    for r in rows:
        n = int(r["day_label"].split()[1])
        r["real_date"] = str(PERIOD_START + timedelta(days=n - 1))
    return rows


def json_from_list(text: str) -> list:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("["):
            return json.loads(line)
    raise RuntimeError(f"no JSON array line in output:\n{text[-500:]}")


# ---------------------------------------------------------------------
# curves
# ---------------------------------------------------------------------

@dataclass
class Curve:
    label: str
    dates: list[str]
    equity: list[float]
    exposure: list[float]  # fraction of that day's equity held in position(s), 0..~1


def cash_curve(trading_days: list[date]) -> Curve:
    n = len(trading_days)
    return Curve("Cash", [str(d) for d in trading_days], [STARTING_EQUITY] * n, [0.0] * n)


def buy_hold_curve(label: str, tickers: list[str], weights: dict[str, float],
                    daily_bars: dict[str, dict[str, dict]], trading_days: list[date],
                    day1_intraday: dict[str, list[dict]]) -> Curve:
    """Enter every ticker at day 1's real 09:45 slipped price (the same
    entry instant/slippage RULES.md §7 and _slipped_buy_price give a
    Hermes BUY), split `weights` (fractions of STARTING_EQUITY) across
    tickers, then mark to each day's real daily close — cash-only, no
    rebalancing. Fails loudly (MissingDataError), never silently zeros a
    held ticker's contribution, if any day's bar for a weighted ticker is
    absent."""
    day1 = trading_days[0]
    shares: dict[str, int] = {}
    leftover_cash = 0.0
    for t in tickers:
        bar_945 = next((b for b in day1_intraday[t] if b["time"] == "09:45"), None)
        if bar_945 is None:
            raise MissingDataError(f"{t}: no day-1 09:45 bar — fails closed")
        fill = _slipped_buy_price(bar_945["open"], bar_945)
        budget = STARTING_EQUITY * weights[t]
        qty = int(budget // fill)
        shares[t] = qty
        leftover_cash += budget - qty * fill

    equity_curve, exposure_curve = [], []
    for day in trading_days:
        mark = leftover_cash
        position_value = 0.0
        for t in tickers:
            bar = daily_bars[t].get(str(day))
            if bar is None:
                raise MissingDataError(f"{t}: missing daily bar on {day} while held — fails closed")
            position_value += shares[t] * bar["close"]
        mark += position_value
        equity_curve.append(mark)
        exposure_curve.append(position_value / mark if mark else 0.0)
    return Curve(label, [str(d) for d in trading_days], equity_curve, exposure_curve)


# ---------------------------------------------------------------------
# technical baseline — SMA(10) close-crossover, ONE pooled $100,000
# account, defined completely here BEFORE any result is computed:
#
#   Signal (per ticker, independent): today's close crosses above its own
#   trailing 10-trading-day SMA of closes (yesterday close <= SMA10
#   yesterday, today close > SMA10 today), using only closes up to and
#   including "today" — no lookahead. A completely standard, un-tuned
#   trend-following entry (not fit to this window).
#
#   Entry: next trading day, 09:45, same _slipped_buy_price fill Hermes
#   itself gets.
#
#   Stop: BASELINE_STOP_PCT (5%) below the SIGNAL day's close, fixed at
#   signal time (same as a real stop being set before the fill is known).
#   Target: entry + 1.5 x risk (RULES.md §6b base tier).
#   Exit: RULES.md §6a stop / §6b target / §6c 5-trading-day time stop,
#   walked day by day with the SAME mechanical primitives Hermes' own
#   trades use (evaluate_opening_gap / evaluate_bar_stop_target).
#
#   Sizing: RULES.md §2's own formula — 2% of CURRENT account equity risk
#   per trade, capped at 20% of CURRENT account equity per position,
#   never exceeding available cash — applied against ONE pooled
#   $100,000 account shared by all 10 tickers (RULES.md §3: cash-only,
#   no position-count cap), not a pre-divided per-ticker slice. Multiple
#   concurrent positions are therefore possible, exactly as Hermes'
#   own run allows; RULES.md §9.1 (one open lot per ticker at a time)
#   still applies per ticker.
#
#   Tie-break: if more than one ticker signals entry on the same day,
#   process them in fixed alphabetical ticker order (deterministic, not
#   an attempt to rank setup quality).
# ---------------------------------------------------------------------

def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def technical_baseline_curve(tickers: list[str], daily_bars: dict[str, dict[str, dict]],
                              lookback_days: list[date], trading_days: list[date]
                              ) -> tuple[Curve, list[dict]]:
    all_days = lookback_days + trading_days
    closes: dict[str, list[float]] = {t: [] for t in tickers}
    signal_day_for_entry: dict[str, str] = {}  # entry_date_str -> ticker, filled as we scan

    # Precompute closes at every all_days index and each ticker's set of
    # signal->entry_day pairs, gated on data availability (a missing
    # lookback/period bar just means no signal that day -- it does not
    # invalidate any equity mark, unlike a bar missing for a HELD ticker
    # below).
    entries_by_day: dict[str, list[str]] = {}
    for t in tickers:
        c: list[float] = []
        for day in all_days:
            bar = daily_bars[t].get(str(day))
            if bar is None:
                continue
            c.append(bar["close"])
            if day not in trading_days or day == trading_days[-1] or len(c) < 2:
                continue
            sma_today, sma_yday = sma(c, 10), sma(c[:-1], 10)
            if sma_today is None or sma_yday is None:
                continue
            if c[-2] <= sma_yday and c[-1] > sma_today:
                entry_day = trading_days[trading_days.index(day) + 1]
                entries_by_day.setdefault(str(entry_day), []).append(t)
                # signal_close travels with the entry via a side table below
                signal_day_for_entry[f"{t}|{entry_day}"] = bar["close"]

    cash = STARTING_EQUITY
    positions: dict[str, dict] = {}  # ticker -> {qty, entry_price, stop, target, entry_idx}
    trades_log: list[dict] = []
    equity_curve, exposure_curve = [], []

    for i, day in enumerate(trading_days):
        day_s = str(day)

        # 1) resolve exits for currently open positions -----------------
        for t in list(positions):
            pos = positions[t]
            if pos["entry_idx"] == i:
                entry_bars = fetch_intraday_bars([t], day)[t]
                touch = None
                for bar in entry_bars:
                    if bar["time"] < "09:45":
                        continue
                    touch = evaluate_bar_stop_target(pos["stop"], pos["target"], bar)
                    if touch:
                        break
            else:
                bar = daily_bars[t].get(day_s)
                if bar is None:
                    raise MissingDataError(f"{t}: missing daily bar on {day} while held — fails closed")
                touch = evaluate_opening_gap(pos["stop"], pos["target"], bar["open"])
                if not touch:
                    touch = evaluate_bar_stop_target(pos["stop"], pos["target"], bar)

            held_days = i - pos["entry_idx"]
            if not touch and held_days >= TIME_STOP_TRADING_DAYS:
                open_price = (daily_bars[t].get(day_s) or {}).get("open")
                if open_price is None:
                    raise MissingDataError(f"{t}: missing daily bar on {day} for time-stop exit — fails closed")
                touch = ("time_stop", open_price)

            if touch:
                kind, exit_price = touch
                cash += pos["qty"] * exit_price
                trades_log.append({
                    "ticker": t, "signal_date": pos["signal_date"], "entry_date": pos["entry_date"],
                    "entry_price": pos["entry_price"], "stop_price": pos["stop"], "target_price": pos["target"],
                    "qty": pos["qty"], "outcome": kind, "exit_price": exit_price, "exit_date": day_s,
                    "r_multiple": round((exit_price - pos["entry_price"]) / (pos["entry_price"] - pos["stop"]), 3),
                })
                del positions[t]

        # 2) new entries today, deterministic alphabetical tie-break ----
        for t in sorted(entries_by_day.get(day_s, [])):
            if t in positions:
                continue  # RULES.md §9.1: one open lot per ticker
            entry_bars = fetch_intraday_bars([t], day)[t]
            bar_945 = next((b for b in entry_bars if b["time"] == "09:45"), None)
            if bar_945 is None:
                continue  # no fill possible; not a held-position data gap
            fill = _slipped_buy_price(bar_945["open"], bar_945)
            signal_close = signal_day_for_entry[f"{t}|{day}"]
            stop_price = round(signal_close * (1 - BASELINE_STOP_PCT), 2)
            if fill <= stop_price:
                continue
            risk_per_share = fill - stop_price
            target_price = round(signal_close + BASE_MIN_RR * (signal_close - stop_price), 2)

            open_value = sum(p["qty"] * p["entry_price"] for p in positions.values())
            equity_now = cash + open_value  # conservative same-day proxy, same convention run_backtest.py uses
            qty = max(0, min(
                int((equity_now * RISK_PCT) // risk_per_share),
                int((equity_now * MAX_POS_PCT) // fill),
                int(cash // fill),
            ))
            if qty == 0:
                continue
            cash -= qty * fill
            positions[t] = {
                "qty": qty, "entry_price": fill, "stop": stop_price, "target": target_price,
                "entry_idx": i, "entry_date": day_s, "signal_date": str(day),
            }

        # 3) end-of-day mark ---------------------------------------------
        position_value = 0.0
        for t, pos in positions.items():
            bar = daily_bars[t].get(day_s)
            if bar is None:
                raise MissingDataError(f"{t}: missing daily bar on {day} while held — fails closed")
            position_value += pos["qty"] * bar["close"]
        equity = cash + position_value
        equity_curve.append(equity)
        exposure_curve.append(position_value / equity if equity else 0.0)

    return (Curve("Technical baseline (SMA10 crossover, pooled account)",
                   [str(d) for d in trading_days], equity_curve, exposure_curve),
            trades_log)


# ---------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------

def metrics(curve: Curve) -> dict:
    eq = curve.equity
    total_return = (eq[-1] - STARTING_EQUITY) / STARTING_EQUITY
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak:
            max_dd = min(max_dd, (v - peak) / peak)
    rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1]]
    vol_annualized = pstdev(rets) * (252 ** 0.5) if len(rets) > 1 else 0.0
    avg_exposure = sum(curve.exposure) / len(curve.exposure) if curve.exposure else 0.0
    return {
        "label": curve.label,
        "starting_baseline": STARTING_EQUITY,
        "end_equity": round(eq[-1], 2),
        "total_return_pct": round(total_return * 100, 3),
        "max_drawdown_pct": round(max_dd * 100, 3),
        "annualized_vol_pct": round(vol_annualized * 100, 3),
        "avg_exposure_pct": round(avg_exposure * 100, 2),
    }


# ---------------------------------------------------------------------
# sensitivity analysis — recomputed from the REAL 16-trade Round 2
# ledger, hardcoded here from docs/history/evaluations/
# round2_50cycle_evaluation_extract_RAW.md §3.1 (hash-verified archive;
# every number below matches that table's Fill/Exit fill/Qty/Stop/
# Target/Expected R:R/Exit-reason columns exactly). Aliased row order
# preserved, chronological by entry, exactly as the archive presents it.
# No real ticker identity is needed for this section at all.
# ---------------------------------------------------------------------

LEDGER = [
    # fill, exit_fill, qty, stop, target, expected_rr, exit_reason
    {"fill": 91.16, "exit": 82.89, "qty": 219, "stop": 86.57, "target": 100.24, "rr": 2.00, "reason": "mechanical_stop"},
    {"fill": 29.23, "exit": 28.27, "qty": 671, "stop": 28.23, "target": 32.70, "rr": 1.98, "reason": "discretionary_invalidation"},
    {"fill": 73.10, "exit": 78.13, "qty": 263, "stop": 70.24, "target": 81.35, "rr": 1.99, "reason": "mechanical_time_stop"},
    {"fill": 23.52, "exit": 24.46, "qty": 826, "stop": 22.47, "target": 26.02, "rr": 2.01, "reason": "mechanical_time_stop"},
    {"fill": 24.00, "exit": 23.22, "qty": 815, "stop": 23.22, "target": 26.89, "rr": 1.98, "reason": "mechanical_stop"},
    {"fill": 55.31, "exit": 53.60, "qty": 353, "stop": 53.60, "target": 62.07, "rr": 1.99, "reason": "mechanical_stop"},
    {"fill": 148.31, "exit": 142.59, "qty": 132, "stop": 142.59, "target": 165.10, "rr": 2.00, "reason": "mechanical_stop"},
    {"fill": 59.44, "exit": 64.70, "qty": 324, "stop": 57.21, "target": 66.24, "rr": 2.00, "reason": "mechanical_time_stop"},
    {"fill": 146.27, "exit": 141.55, "qty": 131, "stop": 141.55, "target": 163.90, "rr": 2.00, "reason": "mechanical_stop"},
    {"fill": 148.57, "exit": 157.44, "qty": 130, "stop": 143.50, "target": 166.38, "rr": 1.95, "reason": "discretionary_time_stop"},
    {"fill": 28.44, "exit": 29.39, "qty": 684, "stop": 27.30, "target": 31.75, "rr": 1.85, "reason": "discretionary_time_stop"},
    {"fill": 32.83, "exit": 34.14, "qty": 597, "stop": 31.25, "target": 36.33, "rr": 1.85, "reason": "discretionary_time_stop"},
    {"fill": 28.40, "exit": 28.30, "qty": 699, "stop": 27.10, "target": 31.85, "rr": 1.57, "reason": "discretionary_time_stop"},
    {"fill": 37.43, "exit": 39.90, "qty": 529, "stop": 36.00, "target": 42.08, "rr": 1.70, "reason": "discretionary_time_stop"},
    {"fill": 27.45, "exit": 29.11, "qty": 736, "stop": 26.00, "target": 30.28, "rr": 1.80, "reason": "discretionary_time_stop"},
    {"fill": 159.36, "exit": 156.73, "qty": 127, "stop": 151.25, "target": 175.59, "rr": 1.90, "reason": "discretionary_time_stop"},
]


def risk_cap_grid(risk_pcts: list[float], cap_pcts: list[float]) -> dict:
    """Pure, deterministic. For each (risk%, cap%) cell: re-size every one
    of the 16 REAL historical trades against a sequentially-compounding
    equity curve (STARTING_EQUITY, updated in ENTRY order by each trade's
    own realized $ P&L — an approximation stated in the report: it does
    not model the exact overlap of concurrent same-day entries or
    intra-hold unrealized swings, only realized-P&L compounding between
    entries). Same entry/exit PRICES as actually happened (mechanical/
    discretionary exits were price-triggered, not size-triggered) — only
    quantity and therefore $ P&L differ. This is why the grid is labeled
    "scales P&L, does not change trade outcomes" in the report: win/loss
    and trade count never change here, only magnitude."""
    out = {}
    for risk_pct in risk_pcts:
        for cap_pct in cap_pcts:
            equity = STARTING_EQUITY
            total_pnl = 0.0
            for tr in LEDGER:
                risk_per_share = tr["fill"] - tr["stop"]
                qty = max(0, min(
                    int((equity * risk_pct) // risk_per_share) if risk_per_share > 0 else 0,
                    int((equity * cap_pct) // tr["fill"]),
                ))
                pnl = qty * (tr["exit"] - tr["fill"])
                total_pnl += pnl
                equity += pnl
            out[f"risk={risk_pct:.0%}_cap={cap_pct:.0%}"] = {
                "risk_pct": risk_pct, "cap_pct": cap_pct,
                "total_pnl": round(total_pnl, 2),
                "total_return_pct": round(total_pnl / STARTING_EQUITY * 100, 3),
                "end_equity": round(STARTING_EQUITY + total_pnl, 2),
            }
    return out


def min_rr_threshold_filter(thresholds: list[float]) -> dict:
    """Filter-only, outcome-changing counterfactual: a trade whose
    Expected R:R (as originally proposed and journaled) falls below the
    new threshold is removed entirely -- it never fills under that rule.
    Uses the REAL historical qty/fill/exit for every trade that remains
    (no resizing). Does NOT add trades that never happened: RULES.md
    §6b's escalated tier or a lower threshold might have let additional
    proposals through, but this analysis has no row-level geometry for
    proposals that were rejected before reaching the journal, so a
    LOOSER threshold's upside is not estimated here -- see the report's
    Unknowns section."""
    out = {}
    for th in thresholds:
        kept = [tr for tr in LEDGER if tr["rr"] >= th]
        total_pnl = sum(tr["qty"] * (tr["exit"] - tr["fill"]) for tr in kept)
        wins = sum(1 for tr in kept if tr["exit"] > tr["fill"])
        out[f"min_rr>={th}"] = {
            "threshold": th,
            "trades_retained": len(kept),
            "trades_dropped": len(LEDGER) - len(kept),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_pnl / STARTING_EQUITY * 100, 3),
            "win_rate_pct": round(wins / len(kept) * 100, 2) if kept else None,
        }
    return out


def sensitivity_analysis() -> dict:
    return {
        "sample_size": {"n_closed_trades": len(LEDGER),
                         "note": "16 closed trades -- RULES.md §6b's own 30-60 six-month target is not yet "
                                 "reached; every ranking below carries that small-n uncertainty."},
        "risk_pct_x_concentration_cap": {
            "classification": "fully_replayable",
            "reasoning": "Deterministic sizing formula (RULES.md §2) applied to REAL recorded entry/stop/exit "
                         "prices -- scales $ P&L only, never changes which trade wins or loses.",
            "grid": risk_cap_grid([0.01, 0.02, 0.03], [0.10, 0.20, 0.30]),
        },
        "min_reward_risk_threshold": {
            "classification": "one_sided_filter_only",
            "reasoning": "Removes trades whose recorded Expected R:R falls below the threshold; cannot add "
                         "trades a looser threshold might have admitted (their geometry was never journaled). "
                         "Outcome-changing: trade count and win rate both move, not just $ magnitude.",
            "grid": min_rr_threshold_filter([1.25, 1.5, 1.75, 2.0]),
        },
        "time_stop_horizon": {
            "classification": "not_mechanically_identifiable",
            "reasoning": "9 of 16 closed trades (56%) exited via the 5-trading-day time stop. Extending or "
                         "shortening it requires the real intraday/daily price path for each ticker beyond "
                         "(or before) its actual exit date -- recovering that needs resolving each trade's "
                         "real ticker identity from its masked alias, which this pass deliberately did not "
                         "attempt (the masking-secrecy boundary applies to this report; see Unknowns). "
                         "Without that path, no exit price at a different horizon can be computed honestly.",
        },
        "stop_distance_bounds": {
            "classification": "not_mechanically_identifiable",
            "reasoning": "RULES.md §6a's stop is Hermes' own technical-level choice (real support/resistance), "
                         "not a formula. Changing the 3-12% band would change which technical level qualifies, "
                         "which only re-running Hermes' own judgment against a new band can answer -- this "
                         "report does not simulate Hermes' reasoning.",
        },
        "time_stop_x_reward_threshold_interaction": {
            "classification": "not_mechanically_identifiable",
            "reasoning": "Inherits time_stop_horizon's own limitation -- an interaction cannot be measured "
                         "when one of its two axes is not itself measurable from stored evidence.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / ".benchmark_sensitivity_data.json"))
    args = parser.parse_args()

    trading_days = fetch_trading_days(PERIOD_START, PERIOD_END)
    print(f"trading days in period: {len(trading_days)}", file=sys.stderr)
    if len(trading_days) != 50:
        raise SystemExit(f"expected exactly 50 trading days (50 completed Hermes cycles) between "
                          f"{PERIOD_START} and {PERIOD_END}, got {len(trading_days)} — fix PERIOD_END "
                          f"before trusting any benchmark below it.")

    lookback_start = PERIOD_START - timedelta(days=30)
    lookback_days_all = fetch_trading_days(lookback_start, PERIOD_START - timedelta(days=1))
    lookback_days = lookback_days_all[-15:]  # >=10 needed for SMA10 warmup

    watchlist = watchlist_tickers()
    all_tickers = watchlist + [SPY]
    all_days = lookback_days + trading_days

    print("fetching daily bars (SPY + watchlist, with SMA warmup lookback)...", file=sys.stderr)
    daily_bars = fetch_daily_range(all_tickers, all_days)

    print("fetching day-1 intraday bars for buy-hold entries...", file=sys.stderr)
    day1_intraday = fetch_intraday_bars(all_tickers, trading_days[0])

    spy_curve = buy_hold_curve("SPY buy-and-hold", [SPY], {SPY: 1.0}, daily_bars, trading_days, day1_intraday)
    ew_weights = {t: 1.0 / len(watchlist) for t in watchlist}
    ew_curve = buy_hold_curve("Equal-weight watchlist buy-and-hold", watchlist, ew_weights,
                               daily_bars, trading_days, day1_intraday)
    cash_c = cash_curve(trading_days)

    print("simulating SMA10-crossover technical baseline (pooled account)...", file=sys.stderr)
    tech_curve, tech_trades = technical_baseline_curve(watchlist, daily_bars, lookback_days, trading_days)

    print("fetching Hermes' own equity curve from Neon (read-only)...", file=sys.stderr)
    hermes_rows = fetch_hermes_equity_curve()
    trading_day_strs = [str(d) for d in trading_days]
    misaligned = [r["real_date"] for r in hermes_rows if r["real_date"] not in trading_day_strs]
    if misaligned or len(hermes_rows) != 50:
        raise SystemExit(f"Hermes snapshot date alignment failed: {len(hermes_rows)} snapshots, "
                          f"{len(misaligned)} outside the benchmark trading-day set: {misaligned}")
    hermes_curve = Curve("Hermes (actual)", [r["real_date"] for r in hermes_rows],
                          [r["equity"] for r in hermes_rows],
                          [(r["exposure_pct"] or 0.0) / 100 for r in hermes_rows])

    print("recomputing sensitivity grid from the archived Round 2 ledger...", file=sys.stderr)
    sensitivity = sensitivity_analysis()

    out = {
        "period": {"start": str(PERIOD_START), "end": str(PERIOD_END), "trading_days": len(trading_days)},
        "metrics": [metrics(cash_c), metrics(spy_curve), metrics(ew_curve), metrics(tech_curve), metrics(hermes_curve)],
        "technical_baseline_trades": tech_trades,
        "hermes_snapshots": hermes_rows,
        "sensitivity": sensitivity,
        "curves": {c.label: {"dates": c.dates, "equity": c.equity, "exposure": c.exposure}
                   for c in [cash_c, spy_curve, ew_curve, tech_curve, hermes_curve]},
    }
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwrote {args.out}")
    for m in out["metrics"]:
        print(m)
    print(f"technical baseline: {len(tech_trades)} trades")
    print(json.dumps(sensitivity, indent=2)[:2000])


if __name__ == "__main__":
    main()
