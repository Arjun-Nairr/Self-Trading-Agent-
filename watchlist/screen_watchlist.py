"""
screen_watchlist.py — build the RULES.md §8 watchlist from real market data.

WHY NOT maverick-mcp's screener: it screens over symbols already cached in
its own local SQLite DB (2 rows at time of writing — just the tickers we
happened to fetch during testing), and it filters on momentum/volume, with
no market-cap, sector, or minimum-price filter at all. §8 is mostly a
market-cap and sector spec, so that tool structurally cannot do this job.

WHY THE S&P MIDCAP 400 AS THE STARTING UNIVERSE: §8 wants mid-caps, and this
index *is* the definition of US mid-cap — membership is decided by S&P
against published rules, not by anyone's recall of which companies are
memorable. That matters here specifically: picking names from memory biases
toward famous companies, which is the exact mega-cap memorization leak the
no-trained-memory gate exists to fight (see anonymize.py's KNOWN CEILING).
Every filter below is then applied against live data, not assumption.

Run inside the maverick-mcp container (has yfinance + pandas):
    docker compose exec -T maverick-mcp uv run python - < watchlist/screen_watchlist.py
"""
import json
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd
import yfinance as yf

WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
CACHE_PATH = "/app/data/universe_snapshot.json"

# RULES.md §8 thresholds. Mirrored, same drift caveat as risk_checker.py.
MIN_MARKET_CAP = 2_000_000_000       # $2B
MAX_MARKET_CAP = 50_000_000_000      # $50B
MIN_AVG_VOLUME = 1_000_000           # shares/day
# Upper volume bound — NOT in §8, added after the first real run exposed a
# flaw in the selection method. Sorting by liquidity to pick "the most
# tradeable" name per sector actually selects for the most *talked about*
# ones (the first run returned AAL at 109M shares/day, plus HIMS, CHWY,
# PINS, CELH — all heavy retail-narrative names). Trading volume and
# narrative saturation correlate, so optimising for liquidity quietly
# optimised for memorisation leak, the very thing §8's mid-cap preference
# exists to reduce. §8 needs *enough* liquidity for realistic fills; there
# is no benefit beyond that, and real cost above it.
MAX_AVG_VOLUME = 12_000_000
MIN_PRICE = 10.0
TARGET_SIZE = 10                     # §8 wants 8-12, sector-diversified

# §8 excludes binary-event stocks. "Pending FDA decision" isn't a queryable
# field anywhere, so this uses industry as the best available proxy:
# clinical-stage biotech is where scheduled coin-flip gaps actually live.
# Honest limitation — it will not catch a pending merger vote in another
# sector. Those need a manual pass over the finalists.
EXCLUDED_INDUSTRIES = {"Biotechnology"}


def fetch_universe() -> list[str]:
    """S&P 400 constituents. Wikipedia 403s a default user-agent."""
    resp = httpx.get(
        WIKI, headers={"User-Agent": "Mozilla/5.0 (watchlist-screener)"},
        timeout=30, follow_redirects=True,
    )
    resp.raise_for_status()
    for table in pd.read_html(StringIO(resp.text)):
        cols = [str(c) for c in table.columns]
        if any("Symbol" in c or "Ticker" in c for c in cols):
            col = next(c for c in table.columns if "Symbol" in str(c) or "Ticker" in str(c))
            return [str(s).replace(".", "-").strip() for s in table[col]]
    raise RuntimeError("no constituent table found on the Wikipedia page")


def fetch_one(symbol: str) -> dict | None:
    try:
        info = yf.Ticker(symbol).info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None
        return {
            "symbol": symbol,
            "name": info.get("shortName", ""),
            "market_cap": info.get("marketCap"),
            "avg_volume": info.get("averageVolume"),
            "price": price,
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
        }
    except Exception:
        return None  # a dead ticker shouldn't kill a 400-symbol run


def passes(row: dict) -> bool:
    if not row["market_cap"] or not row["avg_volume"]:
        return False
    return (
        MIN_MARKET_CAP <= row["market_cap"] <= MAX_MARKET_CAP
        and MIN_AVG_VOLUME <= row["avg_volume"] <= MAX_AVG_VOLUME
        and row["price"] >= MIN_PRICE
        and row["industry"] not in EXCLUDED_INDUSTRIES
    )


MIN_HISTORY_YEARS = 3


def has_enough_history(symbol: str) -> bool:
    """Reject recent listings/spinoffs.

    Found by a real check, not anticipated: the first clean run picked SOLS
    (a 2025 Honeywell spinoff) with only 206 bars of history. maverick needs
    ~200 bars of warmup before `as_of` just to compute its 200-day SMA, so a
    name like that yields no technicals at all at a past `as_of` — it would
    have failed silently partway through a backtest rather than up front.
    Cheap to check here; expensive to discover later.
    """
    try:
        history = yf.Ticker(symbol).history(period="5y")
        return len(history) >= MIN_HISTORY_YEARS * 250
    except Exception:
        return False


def diversify(rows: list[dict], target: int) -> list[dict]:
    """One name per sector, taking the MEDIAN-liquidity name in each.

    Deliberately not the most liquid: within an already volume-banded pool,
    the top name is the most heavily traded and therefore the most heavily
    written about. The median is comfortably tradeable while carrying less
    narrative — an objective, data-driven tiebreak (no judgement call, no
    recall of which companies seem familiar) that does not quietly select
    for memorisation risk.
    """
    by_sector: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: -r["avg_volume"]):
        by_sector.setdefault(row["sector"], []).append(row)

    picked: list[dict] = []
    for names in by_sector.values():
        if len(picked) >= target:
            break
        # Start at the median and walk outward until one has usable history,
        # rather than dropping the whole sector over a single bad candidate.
        order = sorted(range(len(names)), key=lambda i: abs(i - len(names) // 2))
        for i in order:
            if has_enough_history(names[i]["symbol"]):
                picked.append(names[i])
                break
    return sorted(picked, key=lambda r: r["sector"])


def main() -> None:
    # Cache the raw fetch so selection rules can be re-tried instantly.
    # Iterating on *which* names to pick shouldn't cost 400 network calls
    # each time — and it keeps the exact snapshot the choice was made from.
    cache = Path(CACHE_PATH)
    if cache.exists():
        fetched = json.loads(cache.read_text())
        print(f"loaded {len(fetched)} cached rows from {CACHE_PATH}")
    else:
        universe = fetch_universe()
        print(f"universe: {len(universe)} S&P MidCap 400 constituents")
        with ThreadPoolExecutor(max_workers=12) as pool:
            fetched = [r for r in pool.map(fetch_one, universe) if r]
        cache.write_text(json.dumps(fetched, indent=1))
        print(f"fetched live data for {len(fetched)} (cached to {CACHE_PATH})")

    eligible = [r for r in fetched if passes(r)]
    print(f"pass §8 filters: {len(eligible)}\n")

    picked = diversify(eligible, TARGET_SIZE)
    print(f"{'SYMBOL':<8}{'SECTOR':<26}{'MKT CAP':>10}{'AVG VOL':>12}{'PRICE':>10}  NAME")
    print("-" * 100)
    for r in picked:
        print(
            f"{r['symbol']:<8}{r['sector'][:24]:<26}"
            f"{r['market_cap']/1e9:>9.1f}B{r['avg_volume']:>12,}"
            f"{r['price']:>10.2f}  {r['name'][:28]}"
        )
    print(f"\nWATCHLIST = {[r['symbol'] for r in picked]}")
    print(f"sectors covered: {len({r['sector'] for r in picked})}")


if __name__ == "__main__":
    main()
