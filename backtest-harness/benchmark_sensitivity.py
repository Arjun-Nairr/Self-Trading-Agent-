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
The sensitivity ledger below carries masked "Day N" entry/exit indices
(from the archived RAW report's own table) for chronological ordering
only — never a real ticker identity.

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
# RULES.md §6a's current (Phase 1 aggressive) stop band -- NOT
# geometry_scan.py's own 5-10% constants, which mirror the OLDER Round 1
# profile. Kept as separate constants here for the same "independently
# checkable" reason geometry_scan.py gives for its own copy.
STOP_MIN_PCT = 3.0
STOP_MAX_PCT = 12.0
STOP_TOLERANCE_PCT = 0.10
# Technical baseline's OWN stop rule (see "technical baseline definition"
# below) — deliberately a fixed 5% below the signal day's close (the
# midpoint of RULES.md §6a's 3-12% band), not maverick's real support/
# resistance, to keep this baseline computable from the same daily bars
# already fetched for buy-hold, and to keep it un-tuned to this period.
BASELINE_STOP_PCT = 0.05


class MissingDataError(RuntimeError):
    """Raised instead of silently marking a held/weighted position at $0,
    silently skipping a signal-history bar, or skipping either — a
    missing bar for a ticker the curve is currently exposed to, or a gap
    in the SMA warmup/evaluation history, invalidates the result outright
    rather than quietly compressing time or dropping value."""


def watchlist_tickers() -> list[str]:
    return [t["symbol"] for t in json.loads((REPO / "watchlist" / "watchlist.json").read_text())["tickers"]]


def stop_distance_pct(fill: float, stop: float) -> float:
    return (fill - stop) / fill * 100 if fill else float("-inf")


def stop_within_band(fill: float, stop: float) -> bool:
    pct = stop_distance_pct(fill, stop)
    return STOP_MIN_PCT - STOP_TOLERANCE_PCT <= pct <= STOP_MAX_PCT + STOP_TOLERANCE_PCT


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
    absent.

    NOTE on entry timing vs. the technical baseline and Hermes: this
    benchmark enters on trading_days[0] itself (the FIRST evaluation
    day) — a real buy-and-hold investor's only decision is "how much,"
    made before day 1 opens. Hermes and the technical baseline instead
    require a prior signal/decision and execute on the NEXT trading day
    — their earliest possible entry is trading_days[1], one day later.
    This is a real, disclosed difference in entry timing, not a flaw to
    reconcile away — see the report's benchmark-definitions section."""
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
#   trend-following entry (not fit to this window). Every daily bar
#   across the full warmup+evaluation window is required — a missing one
#   raises MissingDataError rather than silently compressing the SMA
#   window (see `_require_full_coverage`).
#
#   Entry: next trading day, 09:45, same _slipped_buy_price fill Hermes
#   itself gets. This is one full trading day LATER than the buy-and-hold
#   benchmarks' day-1 entry — see buy_hold_curve's own note.
#
#   Stop: BASELINE_STOP_PCT (5%) below the SIGNAL day's close, fixed at
#   signal time (same as a real stop being set before the fill is known).
#   Target: the REAL fill + 1.5 x (fill - stop) — RULES.md §6b base tier,
#   computed from the actual slippage-adjusted entry, not the signal
#   day's close (an overnight/opening move between signal and fill would
#   otherwise silently distort the realized reward:risk away from 1.5).
#   Before accepting the entry, the resulting stop distance (relative to
#   the REAL fill) is checked against RULES.md §6a's 3-12% band (+/-0.10
#   tolerance) — a gap that pushes the fill far enough from the
#   preselected stop invalidates that stop, and the entry is skipped and
#   recorded as a rejection rather than silently traded outside the
#   baseline's own declared rules.
#
#   Exit: RULES.md §6a stop / §6b target / §6c 5-trading-day time stop,
#   in the REAL harness's own chronology (run_backtest.py's
#   simulate_trading_day), every held day: (1) opening-gap stop/target
#   at the open, (2) a time-stop exit AT THE OPEN if the horizon has
#   expired, (3) only if still open, the 09:30-09:45 bar's OWN range --
#   never the whole day's range, which would let an afternoon touch leak
#   into the 09:45 mark. Only after 09:45 entries are sized (below) does
#   EVERY still-open position (carried survivors and new fills alike)
#   get walked from 09:45 through the close for a stop/target touch —
#   a position can close the same day it opens, but never before 09:45.
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


def _require_full_coverage(tickers: list[str], daily_bars: dict[str, dict[str, dict]],
                            all_days: list[date]) -> None:
    """Every ticker must have a real daily bar for every day in the
    combined warmup+evaluation window -- a missing one would otherwise
    be silently skipped while building the SMA close history, effectively
    compressing the 10-day window and changing which days cross it.
    Checked once, up front, before any signal is computed."""
    for t in tickers:
        for day in all_days:
            if daily_bars[t].get(str(day)) is None:
                raise MissingDataError(f"{t}: missing daily bar on {day} in the required "
                                        f"SMA warmup/evaluation history — fails closed")


def _resolve_entry_day_exit(t: str, entry_bars: list[dict], stop: float, target: float
                             ) -> tuple[str, float] | None:
    """Walk this ticker's OWN entry-day intraday bars from 09:45 onward
    (inclusive of the fill bar itself, same as run_backtest.py's own
    "its own entry bar counts, since entry happened at that bar's
    start"), stop checked before target. Returns (kind, exit_price) or
    None if the position survives the entry day."""
    for bar in entry_bars:
        if bar["time"] < "09:45":
            continue
        touch = evaluate_bar_stop_target(stop, target, bar)
        if touch:
            return touch
    return None


def _resolve_carried_pre_945(bar_930: dict, stop: float, target: float, held_days: int) -> tuple[str, float] | None:
    """A carried (non-entry-day) position's PRE-09:45 chronology, in the
    real harness's own order (run_backtest.py's simulate_trading_day
    steps 1-2): (1) opening-gap stop/target -- an overnight gap that
    already cleared a resting level fills at/near the open, checked first
    because it reflects the earliest possible price action that day; (2)
    a time-stop exit AT THE OPEN if the holding horizon has expired --
    decided on information already known before today (elapsed trading
    days), so it takes priority over anything the REST of the day's range
    does; (3) only if still open after both of those, the 09:30-09:45
    bar's OWN range for a stop/target touch -- NOT the whole day's range
    (that would let a touch from the afternoon leak into a mark taken at
    09:45, letting its proceeds retroactively fund a same-day entry that
    only had cash because of a move that hadn't happened yet by 09:45).
    `bar_930` is the real 15-minute intraday bar covering 09:30-09:45 --
    never a daily OHLC summary."""
    touch = evaluate_opening_gap(stop, target, bar_930["open"])
    if touch:
        return touch
    if held_days >= TIME_STOP_TRADING_DAYS:
        return "time_stop", bar_930["open"]
    return evaluate_bar_stop_target(stop, target, bar_930)


def technical_baseline_curve(tickers: list[str], daily_bars: dict[str, dict[str, dict]],
                              lookback_days: list[date], trading_days: list[date]
                              ) -> tuple[Curve, list[dict], list[dict]]:
    all_days = lookback_days + trading_days
    _require_full_coverage(tickers, daily_bars, all_days)

    # entry_info[f"{ticker}|{entry_day}"] = (signal_date_str, signal_close) --
    # both the REAL crossover date and the close that fixed the stop, kept
    # distinct from the entry date itself (the prior bug conflated them).
    entry_info: dict[str, tuple[str, float]] = {}
    entries_by_day: dict[str, list[str]] = {}
    for t in tickers:
        c: list[float] = []
        for day in all_days:
            bar = daily_bars[t][str(day)]
            c.append(bar["close"])
            if day not in trading_days or day == trading_days[-1] or len(c) < 2:
                continue
            sma_today, sma_yday = sma(c, 10), sma(c[:-1], 10)
            if sma_today is None or sma_yday is None:
                continue
            if c[-2] <= sma_yday and c[-1] > sma_today:
                entry_day = trading_days[trading_days.index(day) + 1]
                entries_by_day.setdefault(str(entry_day), []).append(t)
                entry_info[f"{t}|{entry_day}"] = (str(day), bar["close"])

    cash = STARTING_EQUITY
    positions: dict[str, dict] = {}  # ticker -> {qty, entry_price, stop, target, entry_idx, ...}
    trades_log: list[dict] = []
    rejected_entries: list[dict] = []
    equity_curve, exposure_curve = [], []

    for i, day in enumerate(trading_days):
        day_s = str(day)
        candidates = [t for t in sorted(entries_by_day.get(day_s, [])) if t not in positions]
        carried_tickers = sorted(positions.keys())
        needed = sorted(set(carried_tickers) | set(candidates))
        intraday_today: dict[str, list[dict]] = fetch_intraday_bars(needed, day) if needed else {}

        # 1) PRE-09:45: opening-gap / time-stop-at-open / 09:30-09:45 first-
        #    bar range for positions CARRIED IN from a prior day -- real
        #    intraday data only, never a daily OHLC summary (a summary
        #    would smuggle in afternoon price action as if it had already
        #    happened by 09:45). A position entered today has no pre-09:45
        #    life at all -- it is handled entirely in step 2/3 below.
        carried_930: dict[str, dict] = {}
        for t in carried_tickers:
            pos = positions[t]
            bars = intraday_today.get(t)
            bar_930 = next((b for b in bars if b["time"] == "09:30"), None) if bars else None
            if bar_930 is None:
                raise MissingDataError(f"{t}: missing required 09:30 bar on {day} while held — fails closed")
            held_days = i - pos["entry_idx"]
            touch = _resolve_carried_pre_945(bar_930, pos["stop"], pos["target"], held_days)
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
            else:
                carried_930[t] = bar_930  # kept for the 09:45 mark below -- no re-fetch, no re-lookup

        # 2) 09:45: size and fill every valid entry, in two phases so a
        #    same-day exit proceeds can never fund another ticker's
        #    earlier entry:
        #
        #    2a) SIZE AND FILL every valid 09:45 entry first. Sizing
        #        values each SURVIVING carried position at its real
        #        09:30-bar CLOSE (the last fully-elapsed mark by 09:45 --
        #        same convention run_backtest.py's own `marks` dict uses),
        #        never at cost basis and never at today's close or a
        #        later bar. A ticker that already filled earlier in this
        #        same phase is valued at its own real fill price.
        #    2b) ONLY AFTER every simultaneous entry has been sized does
        #        EVERY position still open (surviving carried positions
        #        AND today's new fills alike) get walked from 09:45
        #        through the close for a stop/target touch.
        carried_marks = {t: b["close"] for t, b in carried_930.items()}
        filled_today: dict[str, dict] = {}
        for t in candidates:
            entry_bars = intraday_today.get(t)
            bar_945 = next((b for b in entry_bars if b["time"] == "09:45"), None) if entry_bars else None
            if bar_945 is None:
                raise MissingDataError(f"{t}: signaled entry missing its required 09:45 bar "
                                        f"on {day} — fails closed")
            fill = _slipped_buy_price(bar_945["open"], bar_945)
            signal_date, signal_close = entry_info[f"{t}|{day}"]
            stop_price = round(signal_close * (1 - BASELINE_STOP_PCT), 2)

            if not stop_within_band(fill, stop_price):
                rejected_entries.append({
                    "ticker": t, "signal_date": signal_date, "entry_date": day_s,
                    "fill": fill, "stop_price": stop_price,
                    "stop_distance_pct": round(stop_distance_pct(fill, stop_price), 3),
                    "reason": f"stop distance outside {STOP_MIN_PCT}-{STOP_MAX_PCT}% band "
                              f"(+/-{STOP_TOLERANCE_PCT}) once priced off the real fill",
                })
                continue

            risk_per_share = fill - stop_price
            target_price = round(fill + BASE_MIN_RR * risk_per_share, 2)

            # Real 09:45 account equity: cash + surviving carried
            # positions at their 09:30-close mark (NOT cost basis, NOT
            # today's close, NOT any post-09:45 proceeds) + today's
            # already-filled entries at their own real fill price.
            carried_value = sum(positions[ct]["qty"] * carried_marks[ct] for ct in positions)
            filled_value = sum(f["qty"] * f["entry_price"] for f in filled_today.values())
            equity_now = cash + carried_value + filled_value
            qty = max(0, min(
                int((equity_now * RISK_PCT) // risk_per_share),
                int((equity_now * MAX_POS_PCT) // fill),
                int(cash // fill),
            ))
            if qty == 0:
                rejected_entries.append({
                    "ticker": t, "signal_date": signal_date, "entry_date": day_s,
                    "fill": fill, "stop_price": stop_price, "reason": "sizing rounded to zero shares",
                })
                continue

            cash -= qty * fill
            filled_today[t] = {
                "qty": qty, "entry_price": fill, "stop": stop_price, "target": target_price,
                "entry_bars": entry_bars, "signal_date": signal_date,
            }

        # 2b) NOW walk 09:45-through-close for every position still open --
        # surviving carried positions using their own already-fetched
        # intraday bars, and today's new fills using their own entry
        # bars. Every simultaneous entry has already been sized above, so
        # no entry-day/post-09:45 exit proceeds can feed back into this
        # same day's sizing (the defect this pass fixes).
        for t in carried_930:
            if t not in positions:
                continue  # shouldn't happen; guards against double-processing
            pos = positions[t]
            touch = _resolve_entry_day_exit(t, intraday_today[t], pos["stop"], pos["target"])
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

        for t, f in filled_today.items():
            exit_touch = _resolve_entry_day_exit(t, f["entry_bars"], f["stop"], f["target"])
            if exit_touch:
                kind, exit_price = exit_touch
                cash += f["qty"] * exit_price
                trades_log.append({
                    "ticker": t, "signal_date": f["signal_date"], "entry_date": day_s,
                    "entry_price": f["entry_price"], "stop_price": f["stop"], "target_price": f["target"],
                    "qty": f["qty"], "outcome": kind, "exit_price": exit_price, "exit_date": day_s,
                    "r_multiple": round((exit_price - f["entry_price"]) / (f["entry_price"] - f["stop"]), 3),
                })
            else:
                positions[t] = {
                    "qty": f["qty"], "entry_price": f["entry_price"], "stop": f["stop"], "target": f["target"],
                    "entry_idx": i, "entry_date": day_s, "signal_date": f["signal_date"],
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
        assert cash >= -1e-6, f"cash went negative on {day}: {cash}"  # pooled cash invariant

    return (Curve("Technical baseline (SMA10 crossover, pooled account)",
                   [str(d) for d in trading_days], equity_curve, exposure_curve),
            trades_log, rejected_entries)


# ---------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------

def metrics(curve: Curve) -> dict:
    eq = curve.equity
    total_return = (eq[-1] - STARTING_EQUITY) / STARTING_EQUITY
    # Drawdown and volatility both start from the real $100,000 baseline,
    # one observation BEFORE the first daily mark -- a first-day loss
    # must show up in both, not just be invisible because eq[0] itself
    # became the (wrong) initial peak.
    eq_full = [STARTING_EQUITY] + list(eq)
    peak = eq_full[0]
    max_dd = 0.0
    for v in eq_full:
        peak = max(peak, v)
        if peak:
            max_dd = min(max_dd, (v - peak) / peak)
    rets = [(eq_full[i] - eq_full[i - 1]) / eq_full[i - 1] for i in range(1, len(eq_full)) if eq_full[i - 1]]
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
# Target/Expected R:R/Exit-reason/Entry-day/Exit-day columns exactly).
# Aliased row order preserved, chronological by entry. `entry_day`/
# `exit_day` are the archive's own MASKED "Day N" trading-day indices --
# used only to order/overlap trades correctly, never a real ticker
# identity or real calendar date.
# ---------------------------------------------------------------------

LEDGER = [
    # fill, exit_fill, qty, stop, target, expected_rr, exit_reason, entry_day, exit_day
    {"fill": 91.16, "exit": 82.89, "qty": 219, "stop": 86.57, "target": 100.24, "rr": 2.00, "reason": "mechanical_stop", "entry_day": 4, "exit_day": 8},
    {"fill": 29.23, "exit": 28.27, "qty": 671, "stop": 28.23, "target": 32.70, "rr": 1.98, "reason": "discretionary_invalidation", "entry_day": 7, "exit_day": 8},
    {"fill": 73.10, "exit": 78.13, "qty": 263, "stop": 70.24, "target": 81.35, "rr": 1.99, "reason": "mechanical_time_stop", "entry_day": 9, "exit_day": 16},
    {"fill": 23.52, "exit": 24.46, "qty": 826, "stop": 22.47, "target": 26.02, "rr": 2.01, "reason": "mechanical_time_stop", "entry_day": 10, "exit_day": 17},
    {"fill": 24.00, "exit": 23.22, "qty": 815, "stop": 23.22, "target": 26.89, "rr": 1.98, "reason": "mechanical_stop", "entry_day": 18, "exit_day": 18},
    {"fill": 55.31, "exit": 53.60, "qty": 353, "stop": 53.60, "target": 62.07, "rr": 1.99, "reason": "mechanical_stop", "entry_day": 18, "exit_day": 21},
    {"fill": 148.31, "exit": 142.59, "qty": 132, "stop": 142.59, "target": 165.10, "rr": 2.00, "reason": "mechanical_stop", "entry_day": 21, "exit_day": 25},
    {"fill": 59.44, "exit": 64.70, "qty": 324, "stop": 57.21, "target": 66.24, "rr": 2.00, "reason": "mechanical_time_stop", "entry_day": 35, "exit_day": 42},
    {"fill": 146.27, "exit": 141.55, "qty": 131, "stop": 141.55, "target": 163.90, "rr": 2.00, "reason": "mechanical_stop", "entry_day": 38, "exit_day": 39},
    {"fill": 148.57, "exit": 157.44, "qty": 130, "stop": 143.50, "target": 166.38, "rr": 1.95, "reason": "discretionary_time_stop", "entry_day": 45, "exit_day": 51},
    {"fill": 28.44, "exit": 29.39, "qty": 684, "stop": 27.30, "target": 31.75, "rr": 1.85, "reason": "discretionary_time_stop", "entry_day": 45, "exit_day": 51},
    {"fill": 32.83, "exit": 34.14, "qty": 597, "stop": 31.25, "target": 36.33, "rr": 1.85, "reason": "discretionary_time_stop", "entry_day": 45, "exit_day": 51},
    {"fill": 28.40, "exit": 28.30, "qty": 699, "stop": 27.10, "target": 31.85, "rr": 1.57, "reason": "discretionary_time_stop", "entry_day": 52, "exit_day": 58},
    {"fill": 37.43, "exit": 39.90, "qty": 529, "stop": 36.00, "target": 42.08, "rr": 1.70, "reason": "discretionary_time_stop", "entry_day": 52, "exit_day": 58},
    {"fill": 27.45, "exit": 29.11, "qty": 736, "stop": 26.00, "target": 30.28, "rr": 1.80, "reason": "discretionary_time_stop", "entry_day": 52, "exit_day": 58},
    {"fill": 159.36, "exit": 156.73, "qty": 127, "stop": 151.25, "target": 175.59, "rr": 1.90, "reason": "discretionary_time_stop", "entry_day": 53, "exit_day": 59},
]


def risk_cap_grid(risk_pcts: list[float], cap_pcts: list[float]) -> dict:
    """Chronological, cash-constrained replay over the REAL 16-trade
    ledger's own masked entry/exit day indices, walked one masked day at
    a time in the real harness's own order:

      1. Close positions CARRIED INTO the day (entered on an earlier day,
         exiting today) -- their exit cash is available for step 2,
         preserving this evaluator's documented "exit frees cash before
         same-day entries are sized" approximation.
      2. Size and open every new entry scheduled for today.
      3. Close positions that BOTH entered and exit today (the Day-18
         ledger row is exactly this case) -- settled only after step 2,
         so a same-day round-trip trade is never treated as exiting
         before it exists, and never falsely stays open past its own
         recorded exit day.

    Same entry/exit PRICES as actually happened (mechanical/discretionary
    exits were price- or time-triggered, not size-triggered) — only
    quantity, and therefore $ P&L, differs from the real run.

    ONE remaining, disclosed approximation: sizing uses `equity_now =
    cash + cost basis of currently open positions`, not a live
    mark-to-market value -- the real intra-hold daily price path for
    each masked trade isn't available to this report (see the report's
    Unknowns section), so an open position's contribution to "equity"
    here is its entry cost, not its fluctuating market value. This is
    why the classification below is
    "chronological_cash_constrained_approximation", NOT "fully
    replayable" — cash, overlap and exit-timing ARE genuinely
    modeled; live equity marking during a hold is not."""
    out = {}
    min_day = min(tr["entry_day"] for tr in LEDGER)
    max_day = max(tr["exit_day"] for tr in LEDGER)
    for risk_pct in risk_pcts:
        for cap_pct in cap_pcts:
            cash = STARTING_EQUITY
            open_positions: dict[int, dict] = {}
            # "pending" until this trade completes or is explicitly skipped
            # -- every row must leave this state by the end of the walk.
            outcomes = ["pending"] * len(LEDGER)

            # Minimal structured trace of every cash-affecting event, in
            # the exact chronological order applied -- {idx, day, kind
            # ("entry"|"exit"), qty, price, cash_after}. This is the
            # smallest information a test needs to INDEPENDENTLY replay
            # cash from STARTING_EQUITY (using only qty/price/kind, not
            # trusting cash_after) and verify every prefix stays
            # nonnegative, and that the replay's own final total
            # reconciles with `end_equity` below.
            cash_events: list[dict] = []

            for day in range(min_day, max_day + 1):
                # 1) carried-in exits: entered before today, exiting today.
                for idx, tr in enumerate(LEDGER):
                    if tr["exit_day"] == day and tr["entry_day"] != day and idx in open_positions:
                        pos = open_positions.pop(idx)
                        cash += pos["qty"] * tr["exit"]
                        outcomes[idx] = "completed"
                        cash_events.append({"idx": idx, "day": day, "kind": "exit",
                                             "qty": pos["qty"], "price": tr["exit"], "cash_after": cash})

                # 2) new entries scheduled for today.
                for idx, tr in enumerate(LEDGER):
                    if tr["entry_day"] != day:
                        continue
                    risk_per_share = tr["fill"] - tr["stop"]
                    if risk_per_share <= 0:
                        outcomes[idx] = "skipped_invalid_risk"
                        continue
                    open_cost = sum(p["qty"] * p["entry_price"] for p in open_positions.values())
                    equity_now = cash + open_cost
                    qty = max(0, min(
                        int((equity_now * risk_pct) // risk_per_share),
                        int((equity_now * cap_pct) // tr["fill"]),
                        int(cash // tr["fill"]),
                    ))
                    if qty == 0:
                        outcomes[idx] = "skipped_insufficient_sizing"
                        continue
                    cash -= qty * tr["fill"]
                    open_positions[idx] = {"qty": qty, "entry_price": tr["fill"]}
                    assert cash >= -1e-6, f"cash went negative sizing trade {idx}: {cash}"
                    cash_events.append({"idx": idx, "day": day, "kind": "entry",
                                         "qty": qty, "price": tr["fill"], "cash_after": cash})

                # 3) same-day round-trips: entered AND exit today -- a
                # trade must never exit before step 2 has given it a
                # chance to exist.
                for idx, tr in enumerate(LEDGER):
                    if tr["exit_day"] == day and tr["entry_day"] == day and idx in open_positions:
                        pos = open_positions.pop(idx)
                        cash += pos["qty"] * tr["exit"]
                        outcomes[idx] = "completed"
                        cash_events.append({"idx": idx, "day": day, "kind": "exit",
                                             "qty": pos["qty"], "price": tr["exit"], "cash_after": cash})

            end_equity = cash + sum(p["qty"] * LEDGER[i]["exit"] for i, p in open_positions.items())
            total_pnl = end_equity - STARTING_EQUITY
            skipped_count = sum(1 for o in outcomes if o.startswith("skipped"))
            out[f"risk={risk_pct:.0%}_cap={cap_pct:.0%}"] = {
                "risk_pct": risk_pct, "cap_pct": cap_pct,
                "total_pnl": round(total_pnl, 2),
                "total_return_pct": round(total_pnl / STARTING_EQUITY * 100, 3),
                "end_equity": round(end_equity, 2),
                "trades_skipped_insufficient_sizing": skipped_count,
                "trade_outcomes": outcomes,
                "still_open_at_end": sorted(open_positions.keys()),
                "cash_events": cash_events,
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
            "classification": "chronological_cash_constrained_approximation",
            "reasoning": "Deterministic sizing formula (RULES.md §2) applied to REAL recorded entry/stop/exit "
                         "prices and the archive's own masked entry/exit day ordering -- cash availability, "
                         "position overlap and exit settlement are all genuinely modeled and enforced (cash "
                         "never goes negative; a trade's exit frees cash before same-day entries are sized). "
                         "NOT labeled fully replayable: sizing uses cash + cost-basis of open positions as an "
                         "equity proxy, not a live mark-to-market value, since the real intra-hold daily price "
                         "path for each masked trade is not available to this report.",
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
    tech_curve, tech_trades, tech_rejected = technical_baseline_curve(watchlist, daily_bars, lookback_days, trading_days)

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
        "technical_baseline_rejected_entries": tech_rejected,
        "hermes_snapshots": hermes_rows,
        "sensitivity": sensitivity,
        "curves": {c.label: {"dates": c.dates, "equity": c.equity, "exposure": c.exposure}
                   for c in [cash_c, spy_curve, ew_curve, tech_curve, hermes_curve]},
    }
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwrote {args.out}")
    for m in out["metrics"]:
        print(m)
    print(f"technical baseline: {len(tech_trades)} trades, {len(tech_rejected)} rejected entries")
    print(json.dumps(sensitivity, indent=2)[:2000])


if __name__ == "__main__":
    main()
