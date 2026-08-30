"""
counterfactual.py — offline, read-only counterfactual engine (round-2 prep).

Mechanically simulates what would have happened to a specific proposal,
using the SAME fill/exit rules run_backtest.py's real chronology uses:
entry at the next real trading day's 09:45 with execution-sim-mcp's exact
5bps slippage formula, exits on stop/target touch (stop checked before
target within a bar, an opening-gap rule for gaps through either level), or
a 15-trading-day time stop.

WHY: geometry existing (geometry_scan.py) doesn't say whether it would have
paid off. This grades setup quality directly -- did the setups Hermes
rejected, and the setups it never proposed at all, actually work?

HARD BOUNDARY: human-evaluation-only output. Never wired into
run_backtest.py, never read by Hermes, never referenced in AGENTS.md or any
prompt. Reads Neon (SELECT only, to recover run 1's own journaled
decisions) and real market data; writes only its own JSON data file. Never
resumes, resets, or advances any backtest run.

# ponytail: the entry fill mirrors real 15-minute intraday bars exactly
# (same as run_backtest.py's simulate_trading_day), but the multi-day
# holding-period walk after entry uses DAILY bars (open/high/low/close),
# not a full 15-minute walk for every day held -- grading setup quality at
# geometry_scan.py's pair volume doesn't need bar-level precision for days
# 2 through 15 of a hold, only for the entry instant itself (where RULES.md
# §7's 09:45 floor and the real slippage actually apply). Upgrade to a full
# intraday walk for the whole hold if day-level stop/target ambiguity ever
# needs resolving more precisely than "which happened first this day."
"""
import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_scan import fetch_trading_days                          # noqa: E402
from run_backtest import (                                            # noqa: E402
    REPO, TIME_STOP_TRADING_DAYS, _slipped_buy_price, evaluate_bar_stop_target,
    evaluate_opening_gap, fetch_bars, fetch_intraday_bars, json_from, run_in,
)

RUN1_ID = "26f1d371-08db-4885-ab24-b42200792fb7"


# --------------------------------------------------------------------------
# recovering run 1's own rejected proposals from Neon (read-only)
# --------------------------------------------------------------------------

def fetch_run1_rejected_proposals(run_id: str = RUN1_ID) -> list[dict]:
    """SELECT-only. Two different reconstructions, deliberately not
    blended:

    - SFM is the one proposal that passed decision-time review and was
      QUEUED -- its stop/target/quantity are durably journaled in `trades`
      (record_queued_decision writes them before execution-time
      resolution). Real numbers, safe to replay.
    - ALGM/ATI/DINO(x2) were rejected at DECISION time, before
      record_queued_decision is ever called (see run_backtest.py's
      Step D) -- their stop/target/entry_price were never persisted
      anywhere in Neon, only a free-text rejection-reason line in
      agent_activity. This function does NOT fabricate values for them;
      it returns what's recoverable (ticker, real signal date, the
      verbatim rejection reason) and marks them `no_geometry`, so the
      caller can report honestly rather than replay invented numbers.

    The real SIGNAL DATE for each event is recovered by matching its
    timestamp to the latest preceding `cycle_start` row for the same
    run_id -- cycle_start descriptions embed the real calendar date
    ("Day N (YYYY-MM-DD) -- reconciling and fetching data"), the ONLY
    place run 1's simulated-day <-> real-date mapping is durably recorded.
    """
    script = f'''
import asyncio, json, re
from server import get_pool

RUN_ID = "{run_id}"

async def main():
    pool = await get_pool()
    cycles = await pool.fetch(
        "SELECT description, created_at FROM agent_activity "
        "WHERE run_id=$1 AND event_type='cycle_start' ORDER BY created_at", RUN_ID)
    events = await pool.fetch(
        "SELECT event_type, description, created_at FROM agent_activity "
        "WHERE run_id=$1 AND event_type IN ('risk_check','order_queued','order_rejected') "
        "ORDER BY created_at", RUN_ID)
    trades = await pool.fetch(
        "SELECT ticker, status, stop_price, target_price, intended_quantity, "
        "intended_notional, rejection_reason, created_at FROM trades WHERE run_id=$1", RUN_ID)

    cycle_rows = [(c["created_at"], c["description"]) for c in cycles]

    def _date_from(desc):
        m = re.search(r"\\((\\d{{4}}-\\d{{2}}-\\d{{2}})\\)", desc)
        return m.group(1) if m else None

    def real_date_for_event(ts):
        # agent_activity rows (risk_check/order_queued/order_rejected AND
        # cycle_start itself) are all written together in ONE batch by
        # do_observability()'s log_activity() call at the END of their
        # cycle (see run_backtest.py's locked essential-vs-best-effort
        # split) -- cycle_start is always list-position 0 in that same
        # batch (run_day prepends it first), so within one cycle's batch
        # cycle_start's created_at is the EARLIEST, and every other event
        # from that cycle sorts after it. The nearest PRECEDING cycle_start
        # is therefore the event's own cycle.
        best = None
        for ts_c, desc in cycle_rows:
            if ts_c <= ts:
                best = desc
            else:
                break
        return _date_from(best) if best else None

    def real_date_for_trade(ts):
        # `trades` rows (record_queued_decision) are written DURING
        # essential Step D, mid-cycle -- BEFORE that same cycle's own
        # do_observability/cycle_start batch has run. So a trade row's
        # created_at is EARLIER than its own cycle's cycle_start row,
        # unlike agent_activity events above -- the correct match is the
        # NEXT cycle_start chronologically (the first one >= ts), not the
        # preceding one. Verified against SFM's real ground truth: this
        # correctly resolves to 2026-03-17 (Day 29), matching the real
        # journaled stop/target's own maverick technicals for that exact day.
        for ts_c, desc in cycle_rows:
            if ts_c >= ts:
                return _date_from(desc)
        return None

    out = {{
        "events": [{{"event_type": e["event_type"], "description": e["description"],
                     "created_at": e["created_at"].isoformat(),
                     "signal_date": real_date_for_event(e["created_at"])}} for e in events],
        "trades": [{{"ticker": t["ticker"], "status": t["status"],
                     "stop_price": float(t["stop_price"]) if t["stop_price"] is not None else None,
                     "target_price": float(t["target_price"]) if t["target_price"] is not None else None,
                     "intended_quantity": t["intended_quantity"],
                     "intended_notional": float(t["intended_notional"]) if t["intended_notional"] is not None else None,
                     "rejection_reason": t["rejection_reason"],
                     "signal_date": real_date_for_trade(t["created_at"])}} for t in trades],
    }}
    print(json.dumps(out))

asyncio.run(main())
'''
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "trade-journal", "python", "-c", script],
        input="", capture_output=True, text=True, cwd=REPO, timeout=60, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"trade-journal query failed:\n{result.stderr[-2000:]}")
    data = json_from(result.stdout)

    proposals = []
    seen_tickers_with_trade_row = {t["ticker"] for t in data["trades"]}
    for t in data["trades"]:
        entry_price = round(t["intended_notional"] / t["intended_quantity"], 2) if t["intended_quantity"] else None
        proposals.append({
            "ticker": t["ticker"], "signal_date": t["signal_date"], "status": t["status"],
            "stop_price": t["stop_price"], "target_price": t["target_price"],
            "quantity": t["intended_quantity"], "entry_price": entry_price,
            "rejection_reason": t["rejection_reason"], "has_geometry": True,
        })
    for e in data["events"]:
        if e["event_type"] != "risk_check":
            continue
        m = json_ticker_from_line(e["description"])
        if m and m not in seen_tickers_with_trade_row:
            proposals.append({
                "ticker": m, "signal_date": e["signal_date"], "status": "decision_time_rejected",
                "stop_price": None, "target_price": None, "quantity": None, "entry_price": None,
                "rejection_reason": e["description"], "has_geometry": False,
            })
    return proposals


def json_ticker_from_line(description: str) -> str | None:
    """`risk_check`/rejection descriptions all start "TICKER: ..." --
    see run_backtest.py's own line-building at Step D."""
    if ":" not in description:
        return None
    return description.split(":", 1)[0].strip()


# --------------------------------------------------------------------------
# the mechanical simulation itself -- pure given fetched bars, but the
# fetchers below do real I/O (Alpaca market data), cached per (ticker, day)
# --------------------------------------------------------------------------

@dataclass
class TradeOutcome:
    ticker: str
    signal_date: str
    entry_date: str
    entry_price: float
    stop_price: float
    target_price: float
    outcome: str            # "target" | "stop" | "time_stop" | "window_end" | "no_data"
    exit_price: float | None
    exit_date: str | None
    r_multiple: float | None
    mfe_r: float | None
    mae_r: float | None
    days_held: int | None


_daily_cache: dict[tuple[str, str], dict | None] = {}
_intraday_cache: dict[tuple[str, str], list[dict]] = {}


def _daily_bar(ticker: str, day: date) -> dict | None:
    """Cached: many GEOMETRY-VALID pairs for the same ticker have adjacent
    signal days, so their post-entry holding windows overlap heavily --
    this is what keeps the real call count tractable (comparable to
    geometry_scan.py's own ~1250 calls, not 1250 x 15)."""
    key = (ticker, str(day))
    if key not in _daily_cache:
        bars = fetch_bars([ticker], day)
        _daily_cache[key] = bars.get(ticker)
    return _daily_cache[key]


def _intraday_bars(ticker: str, day: date) -> list[dict]:
    key = (ticker, str(day))
    if key not in _intraday_cache:
        _intraday_cache[key] = fetch_intraday_bars([ticker], day).get(ticker, [])
    return _intraday_cache[key]


def next_trading_day(trading_days: list[date], after: date) -> date | None:
    for d in trading_days:
        if d > after:
            return d
    return None


def _resolve_from_bars(ticker: str, signal_date: str, entry_date: str, entry_price: float,
                       stop_price: float, target_price: float, entry_day_bars: list[dict],
                       subsequent_days: list[tuple[str, dict | None]]) -> TradeOutcome:
    """Pure core: given the entry-day intraday bars (from 09:45 on) and an
    ordered list of (day, daily_bar_or_None) for the days after entry
    (already capped at TIME_STOP_TRADING_DAYS, already end-of-window
    truncated by the caller), decide the outcome. No I/O -- this is what
    test_counterfactual.py exercises directly with synthetic bars."""
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return TradeOutcome(ticker, signal_date, entry_date, entry_price, stop_price,
                            target_price, "no_data", None, None, None, None, None, None)

    max_high = entry_price
    min_low = entry_price

    def r_of(price: float) -> float:
        return (price - entry_price) / risk_per_share

    # -- entry-day walk: bars from 09:45 onward, same tie-break as the real
    # harness (evaluate_bar_stop_target: stop checked before target). ------
    for bar in entry_day_bars:
        if bar["time"] < "09:45":
            continue
        max_high = max(max_high, bar["high"])
        min_low = min(min_low, bar["low"])
        touch = evaluate_bar_stop_target(stop_price, target_price, bar)
        if touch:
            kind, exit_price = touch
            return TradeOutcome(ticker, signal_date, entry_date, entry_price, stop_price,
                                target_price, kind, exit_price, entry_date,
                                round(r_of(exit_price), 3), round(r_of(max_high), 3),
                                round(r_of(min_low) * -1, 3), 0)

    # -- subsequent trading days: daily bars, gap-check then range-check,
    # time-stop at TIME_STOP_TRADING_DAYS -- same order as
    # simulate_trading_day's 09:30 stage. ---------------------------------
    for day_offset, (day, daily) in enumerate(subsequent_days, start=1):
        if daily is None:
            return TradeOutcome(ticker, signal_date, entry_date, entry_price, stop_price,
                                target_price, "no_data", None, None, None,
                                round(r_of(max_high), 3), round(r_of(min_low) * -1, 3), day_offset - 1)

        if day_offset == TIME_STOP_TRADING_DAYS:
            exit_price = daily["open"]
            max_high = max(max_high, daily["high"])
            min_low = min(min_low, daily["low"])
            return TradeOutcome(ticker, signal_date, entry_date, entry_price, stop_price,
                                target_price, "time_stop", exit_price, day,
                                round(r_of(exit_price), 3), round(r_of(max_high), 3),
                                round(r_of(min_low) * -1, 3), day_offset)

        gap = evaluate_opening_gap(stop_price, target_price, daily["open"])
        max_high = max(max_high, daily["high"])
        min_low = min(min_low, daily["low"])
        if gap:
            kind, exit_price = gap
            return TradeOutcome(ticker, signal_date, entry_date, entry_price, stop_price,
                                target_price, kind, exit_price, day,
                                round(r_of(exit_price), 3), round(r_of(max_high), 3),
                                round(r_of(min_low) * -1, 3), day_offset)
        touch = evaluate_bar_stop_target(stop_price, target_price, daily)
        if touch:
            kind, exit_price = touch
            return TradeOutcome(ticker, signal_date, entry_date, entry_price, stop_price,
                                target_price, kind, exit_price, day,
                                round(r_of(exit_price), 3), round(r_of(max_high), 3),
                                round(r_of(min_low) * -1, 3), day_offset)

    # Ran out of subsequent days without a stop/target/time-stop touch --
    # either the window ended before TIME_STOP_TRADING_DAYS elapsed, or the
    # caller supplied fewer days than that on purpose.
    return TradeOutcome(ticker, signal_date, entry_date, entry_price, stop_price, target_price,
                        "window_end", None, None, None, round(r_of(max_high), 3),
                        round(r_of(min_low) * -1, 3), len(subsequent_days))


def simulate_trade(ticker: str, signal_date: date, stop_price: float, target_price: float,
                    trading_days: list[date]) -> TradeOutcome:
    """Thin I/O wrapper: fetches the real bars _resolve_from_bars needs and
    delegates to it."""
    entry_date = next_trading_day(trading_days, signal_date)
    if entry_date is None:
        return TradeOutcome(ticker, str(signal_date), None, None, stop_price, target_price,
                            "no_data", None, None, None, None, None, None)

    bars = _intraday_bars(ticker, entry_date)
    bar_945 = next((b for b in bars if b["time"] == "09:45"), None)
    if bar_945 is None:
        return TradeOutcome(ticker, str(signal_date), str(entry_date), None, stop_price,
                            target_price, "no_data", None, None, None, None, None, None)
    entry_price = _slipped_buy_price(bar_945["open"], bar_945)

    entry_idx = trading_days.index(entry_date)
    subsequent_days = []
    for day_offset in range(1, TIME_STOP_TRADING_DAYS + 1):
        idx = entry_idx + day_offset
        if idx >= len(trading_days):
            break
        day = trading_days[idx]
        subsequent_days.append((str(day), _daily_bar(ticker, day)))

    return _resolve_from_bars(ticker, str(signal_date), str(entry_date), entry_price, stop_price,
                              target_price, bars, subsequent_days)


def aggregate(outcomes: list[TradeOutcome]) -> dict:
    resolved = [o for o in outcomes if o.r_multiple is not None]
    wins = [o for o in resolved if o.outcome == "target"]
    return {
        "n": len(outcomes),
        "n_resolved": len(resolved),
        "n_unresolved": len(outcomes) - len(resolved),
        "outcome_counts": {k: sum(1 for o in outcomes if o.outcome == k)
                           for k in {"target", "stop", "time_stop", "window_end", "no_data"}},
        "win_rate": round(len(wins) / len(resolved), 4) if resolved else None,
        "avg_r": round(sum(o.r_multiple for o in resolved) / len(resolved), 3) if resolved else None,
        "avg_days_held": (round(sum(o.days_held for o in resolved if o.days_held is not None) /
                                len([o for o in resolved if o.days_held is not None]), 2)
                          if any(o.days_held is not None for o in resolved) else None),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-02-17")
    parser.add_argument("--end", default="2026-08-14")
    parser.add_argument("--geometry-data",
                        default=str(Path(__file__).resolve().parent / ".geometry_scan_data.json"))
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / ".counterfactual_data.json"))
    parser.add_argument("--resume", action="store_true",
                        help="continue from --out's own checkpoint (same geometry-data file, "
                             "same pair order) instead of starting over -- a long real-data "
                             "run can exceed the harness's background-process time limit; "
                             "write_out()'s periodic checkpoint is what this resumes from.")
    args = parser.parse_args()

    trading_days = fetch_trading_days(date.fromisoformat(args.start), date.fromisoformat(args.end))

    geometry_valid = []
    geo_path = Path(args.geometry_data)
    if geo_path.exists():
        geometry_valid = json.loads(geo_path.read_text())["valid_pairs"]
    else:
        print(f"WARNING: {geo_path} not found -- run geometry_scan.py first for part (2)", file=sys.stderr)

    run1_outcomes = []
    run1_skipped = []
    geo_outcomes = []
    geo_errors = 0
    resume_from = 0

    out_path = Path(args.out)
    if args.resume and out_path.exists():
        prev = json.loads(out_path.read_text())
        run1_outcomes = [TradeOutcome(**o) for o in prev["run1_outcomes"]]
        run1_skipped = [{"ticker": s["ticker"], "signal_date": s["signal_date"],
                         "rejection_reason": s["rejection_reason"]} for s in prev["run1_skipped_no_geometry"]]
        geo_outcomes = [TradeOutcome(**o) for o in prev["geometry_valid_outcomes"]]
        geo_errors = prev.get("geometry_valid_fetch_errors", 0)
        resume_from = prev.get("geometry_valid_pairs_seen", len(geo_outcomes))
        print(f"resuming: {len(geo_outcomes)} outcomes + {geo_errors} errors already checkpointed, "
              f"continuing from pair {resume_from}/{len(geometry_valid)}", file=sys.stderr)
    else:
        print("recovering run 1's rejected proposals from Neon (read-only)...", file=sys.stderr)
        proposals = fetch_run1_rejected_proposals()
        for p in proposals:
            if not p["has_geometry"] or not p["signal_date"]:
                run1_skipped.append(p)
                continue
            outcome = simulate_trade(p["ticker"], date.fromisoformat(p["signal_date"]),
                                     p["stop_price"], p["target_price"], trading_days)
            run1_outcomes.append(outcome)

    print(f"simulating {len(geometry_valid)} geometry-valid pairs...", file=sys.stderr)

    def write_out() -> None:
        out = {
            "run1_proposals_total": len(run1_outcomes) + len(run1_skipped),
            "run1_skipped_no_geometry": [
                {"ticker": p["ticker"], "signal_date": p["signal_date"], "rejection_reason": p["rejection_reason"]}
                for p in run1_skipped
            ],
            "run1_outcomes": [vars(o) for o in run1_outcomes],
            "run1_aggregate": aggregate(run1_outcomes),
            "geometry_valid_outcomes": [vars(o) for o in geo_outcomes],
            "geometry_valid_aggregate": aggregate(geo_outcomes),
            "geometry_valid_fetch_errors": geo_errors,
            "geometry_valid_pairs_seen": len(geo_outcomes) + geo_errors,
            "geometry_valid_pairs_total": len(geometry_valid),
        }
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))

    for i, pair in enumerate(geometry_valid):
        if i < resume_from:
            continue
        if i % 25 == 0:
            print(f"  [{i}/{len(geometry_valid)}]", file=sys.stderr)
            write_out()  # periodic checkpoint -- a long real-data run surviving a late crash
                        # keeps everything simulated so far rather than losing it all.
        try:
            outcome = simulate_trade(pair["ticker"], date.fromisoformat(pair["day"]),
                                     pair["stop"], pair["resistance"], trading_days)
            geo_outcomes.append(outcome)
        except Exception as exc:
            # A single transient container/network hiccup fails this ONE
            # pair, not the whole multi-hour run -- consistent with this
            # module's fail-CLOSED contract elsewhere (a bad fetch never
            # fabricates an outcome; it's just excluded and counted).
            print(f"  ERROR simulating {pair['ticker']} {pair['day']}: {exc}", file=sys.stderr)
            geo_errors += 1

    write_out()
    print(f"\nwrote {args.out}")
    print("run1:", aggregate(run1_outcomes))
    print("geometry-valid:", aggregate(geo_outcomes), "errors:", geo_errors)


if __name__ == "__main__":
    main()
