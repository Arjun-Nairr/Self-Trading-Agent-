"""
check_news_coverage.py — verify every watchlist name actually has news.

WHY THIS IS SEPARATE FROM screen_watchlist.py: that script runs in the
maverick-mcp container (which has yfinance/pandas but no Alpaca
credentials); this one needs the Alpaca news API, so it runs in the
alpaca-mcp container. Two containers, two scripts — run BOTH after any
re-screen.

WHY IT EXISTS AT ALL: RULES.md §8 requires "regular news coverage," on the
reasoning that the §5a sentiment veto is inert on a stock nobody writes
about — half the decision system would never fire. That criterion went
unverified until a real check caught two names (ASB, EXLS) returning ~zero
articles across the whole backtest window. Sector and market cap do not
predict coverage; only asking the real API does.

Run:
    docker compose exec -T alpaca-mcp python - < watchlist/check_news_coverage.py
"""
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone

from server import get_historical_news

# Probe dates spread across the backtest window, so a name that was briefly
# newsworthy once doesn't pass as "regularly covered."
PROBE_DATES = ("2026-02-20", "2026-05-15", "2026-08-14")
LOOKBACK_DAYS = 14
MIN_TOTAL_ARTICLES = 6

WATCHLIST = [
    "OLN", "WMG", "PLNT", "SFM", "DINO", "RYAN", "BRKR", "ATI", "BRX", "ALGM",
]


async def coverage(symbol: str) -> list[int]:
    # 2026-09-17 correction: get_historical_news now takes exact RFC-3339
    # start_at/end_at, not a date + lookback_days -- end_at is the probe
    # date's own end of day (UTC), preserving this script's original
    # "up through as_of" coverage-counting semantics.
    counts = []
    for as_of in PROBE_DATES:
        d = date.fromisoformat(as_of)
        end_at = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
        start_at = end_at - timedelta(days=LOOKBACK_DAYS)
        result = await get_historical_news(symbol, start_at.isoformat(), end_at.isoformat())
        if not result["complete"]:
            print(f"WARNING: {symbol} {as_of}: incomplete pagination, count may undershoot")
        counts.append(len(result["articles"]))
    return counts


async def main() -> None:
    header = "".join(f"{d[:7]:>10}" for d in PROBE_DATES)
    print(f"{'SYM':<7}{header}{'TOTAL':>8}   verdict")
    print("-" * (17 + 10 * len(PROBE_DATES) + 12))

    failures = []
    for symbol in WATCHLIST:
        counts = await coverage(symbol)
        total = sum(counts)
        ok = total >= MIN_TOTAL_ARTICLES
        if not ok:
            failures.append(symbol)
        print(
            f"{symbol:<7}" + "".join(f"{c:>10}" for c in counts)
            + f"{total:>8}   {'ok' if ok else 'INSUFFICIENT — replace'}"
        )

    if failures:
        print(
            f"\nFAIL: {', '.join(failures)} lack the coverage §8 requires. The "
            "sentiment veto would never fire for these names — replace them "
            "with the next candidate in their sector's pick order."
        )
        sys.exit(1)
    print(f"\nPASS: all {len(WATCHLIST)} names carry real news coverage")


if __name__ == "__main__":
    asyncio.run(main())
