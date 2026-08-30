"""
geometry_scan.py — offline, read-only diagnostic (round-2 prep).

For every (trading day, watchlist ticker) in a date window, classifies the
entry geometry maverick-mcp's own real support/resistance algorithm would
have offered that day, using point-in-time (`as_of`-gated) data only.

WHY: run 1 (26f1d371-08db-4885-ab24-b42200792fb7) produced zero fills. This
answers a narrower question than "was Hermes too cautious": how often did a
genuinely tradeable setup (>=2:1 room to real resistance, with a valid
5-10% stop below it) actually exist on the watchlist at all? See
run1_evaluation_extract.md and HANDOFF.md's round-2 section for the context
this feeds.

HARD BOUNDARY: this is human-evaluation-only output. Never wired into
run_backtest.py, never written anywhere Hermes reads, never referenced in
AGENTS.md or any prompt. It does not touch backtest_state.json, Neon
trades/agent_activity, or any run — it only reads maverick-mcp's technical
analysis and alpaca-mcp's trading calendar, the same as run_backtest.py
does for a live cycle, and writes its own JSON data file.

Reuses run_backtest.py's fetch_technicals/run_in/json_from VERBATIM (same
container calls, same support_resistance algorithm in
maverick/technical/analysis.py) rather than reimplementing it — there is no
way for this diagnostic's numbers to silently drift from what the real
agent actually saw.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_backtest import REPO, fetch_technicals, json_from, run_in  # noqa: E402

# Mirrors risk_checker.py's RiskLimits exactly (§6a stop band + tolerance,
# §6b reward:risk minimum) -- see that module's own comment on why these
# are duplicated as constants rather than imported: this and the risk
# checker are meant to be independently checkable against RULES.md, not to
# trust each other's wiring.
MIN_STOP_PCT = 5.0
MAX_STOP_PCT = 10.0
STOP_TOLERANCE_PCT = 0.10
VALID_RR = 2.0
CLOSE_RR = 1.5


def fetch_trading_days(start: date, end: date) -> list[date]:
    """Every real trading day in [start, end], from Alpaca's own calendar
    (one ranged call, not one per day) -- the same source run_backtest.py's
    fetch_trading_calendar_day uses per-day, batched here since this
    module needs the whole window up front."""
    script = f'''
import asyncio, json, httpx
from server import _headers, BASE_URL

async def main():
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{{BASE_URL}}/v2/calendar",
                                 params={{"start": "{start}", "end": "{end}"}},
                                 headers=_headers(), timeout=30)
        resp.raise_for_status()
        rows = resp.json()
    print(json.dumps({{"days": [r["date"] for r in rows]}}))

asyncio.run(main())
'''
    days = json_from(run_in("alpaca-mcp", script))["days"]
    return [date.fromisoformat(d) for d in days]


def nearest_resistance(resistance: list[float], close: float) -> float | None:
    """The closest resistance level strictly above `close`, or None if
    every level in maverick's own returned set (window high, close*1.05,
    close*1.10) is at or below the current close. Used only for
    informational reporting -- see `classify`'s docstring for why the
    CLASSIFICATION itself does not use this."""
    above = [r for r in resistance if r > close]
    return min(above) if above else None


def valid_stop(support: list[float], close: float) -> float | None:
    """The SHALLOWEST (closest-to-entry) support level whose distance below
    `close` falls inside RULES.md §6a's 5-10% band, with risk_checker.py's
    own 0.10-point tolerance. Deliberately the tightest qualifying level,
    not the deepest: AGENTS.md's own entry guidance says to "sit
    comfortably inside the range," and a tighter valid stop is also the
    more conservative (larger) reward:risk read for the same target --
    this is the best-case defensible number, not an optimistic one."""
    candidates = []
    for s in support:
        if s >= close:
            continue
        pct_below = (close - s) / close * 100
        if MIN_STOP_PCT - STOP_TOLERANCE_PCT <= pct_below <= MAX_STOP_PCT + STOP_TOLERANCE_PCT:
            candidates.append(s)
    return max(candidates) if candidates else None


@dataclass
class GeometryResult:
    day: str
    ticker: str
    close: float
    stop: float | None
    resistance: float | None
    ratio: float | None
    klass: str  # GEOMETRY-VALID | CLOSE | INVALID


def classify(support: list[float], resistance: list[float], close: float) -> tuple[str, float | None, float | None, float | None]:
    """Pure. Returns (klass, ratio, resistance_used, stop).

    Target selection: NOT simply the nearest resistance level above close.
    maverick's own support_resistance always includes close*1.05 among the
    resistance candidates (see analysis.py), so "nearest" is almost always
    that synthetic ~5%-out level -- paired with a stop at the §6a floor
    (also ~5% out, the shallowest, lowest-risk valid stop), that mechanically
    caps every ratio near 1.0 regardless of the real setup, which is not a
    genuine "no setup existed" signal, just an artifact of always grabbing
    the closest of three candidates. Verified empirically: using strict
    nearest-resistance classified 100% of the real window as INVALID,
    including SFM's real day, whose journaled numbers (stop 77.02, target
    89.18, ratio 2.00) are ground truth from run 1's own trades row.

    Instead: risk is minimized by the shallowest valid stop (`valid_stop`
    already returns that -- minimizing risk maximizes the ratio for ANY
    target, independent of which target is picked), and the target is the
    NEAREST resistance candidate that still clears the §6b minimum with
    that stop -- the most conservative level that actually supports a
    tradeable setup, not the single farthest number available. If no
    candidate clears the minimum, the FARTHEST candidate (the best case)
    is reported so a CLOSE/INVALID call reflects the best genuinely
    available pairing, not an arbitrarily poor one. Reproduces SFM's real
    stop/target/ratio exactly when run against that day's real data."""
    stop = valid_stop(support, close)
    if stop is None:
        return "INVALID", None, None, stop
    risk = close - stop
    if risk <= 0:
        return "INVALID", None, None, stop

    candidates = sorted(r for r in resistance if r > close)
    if not candidates:
        return "INVALID", None, None, stop

    for r in candidates:
        ratio = (r - close) / risk
        if ratio >= VALID_RR:
            return "GEOMETRY-VALID", ratio, r, stop

    best = candidates[-1]
    ratio = (best - close) / risk
    klass = "CLOSE" if ratio >= CLOSE_RR else "INVALID"
    return klass, ratio, best, stop


def scan(watchlist: list[str], start: date, end: date, progress: bool = True) -> list[GeometryResult]:
    trading_days = fetch_trading_days(start, end)
    results: list[GeometryResult] = []
    for i, day in enumerate(trading_days):
        if progress:
            print(f"  [{i + 1}/{len(trading_days)}] {day}", file=sys.stderr)
        tech = fetch_technicals(watchlist, day)
        for ticker in watchlist:
            t = tech.get(ticker, {})
            if "error" in t or t.get("price") is None:
                results.append(GeometryResult(str(day), ticker, float("nan"), None, None, None, "INVALID"))
                continue
            close = float(t["price"])
            klass, ratio, target, stop = classify(t.get("support", []), t.get("resistance", []), close)
            results.append(GeometryResult(str(day), ticker, close, stop, target, ratio, klass))
    return results


def summarize(results: list[GeometryResult]) -> dict:
    total = len(results)
    counts = Counter(r.klass for r in results)
    by_ticker: dict[str, Counter] = defaultdict(Counter)
    by_month: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        by_ticker[r.ticker][r.klass] += 1
        by_month[r.day[:7]][r.klass] += 1
    valid_pairs = [
        {"day": r.day, "ticker": r.ticker, "close": r.close, "stop": r.stop,
         "resistance": r.resistance, "ratio": round(r.ratio, 3)}
        for r in results if r.klass == "GEOMETRY-VALID"
    ]
    return {
        "total": total,
        "counts": dict(counts),
        "pct": {k: round(v / total * 100, 2) for k, v in counts.items()} if total else {},
        "by_ticker": {t: dict(c) for t, c in sorted(by_ticker.items())},
        "by_month": {m: dict(c) for m, c in sorted(by_month.items())},
        "valid_pairs": valid_pairs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-02-17")
    parser.add_argument("--end", default="2026-08-14")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / ".geometry_scan_data.json"))
    args = parser.parse_args()

    # watchlist.json's "tickers" is a list of {symbol, sector, name} objects
    # (see watchlist/watchlist.json), not bare symbols -- mirrors
    # run_backtest.py's own `tickers = [t["symbol"] for t in watchlist]`.
    watchlist = [t["symbol"] for t in json.loads((REPO / "watchlist" / "watchlist.json").read_text())["tickers"]]
    results = scan(watchlist, date.fromisoformat(args.start), date.fromisoformat(args.end))
    summary = summarize(results)

    Path(args.out).write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nwrote {args.out}")
    print(f"total (day, ticker) pairs: {summary['total']}")
    print(f"counts: {summary['counts']}")
    print(f"pct: {summary['pct']}")


if __name__ == "__main__":
    main()
