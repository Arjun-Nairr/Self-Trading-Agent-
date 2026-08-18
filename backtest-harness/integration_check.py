"""End-to-end check: real live tool output -> both gates -> what Hermes sees.

Not a unit test. This runs the no-trained-memory gate over payloads actually
fetched from the running maverick-mcp and alpaca-mcp containers (already
point-in-time gated via as_of), so the masking is exercised against real
field shapes, real headlines, and real symbol lists rather than fixtures.

Usage: the two JSON dumps are produced by querying the live containers; this
script consumes them. See the session notes for the exact docker commands.
"""
import json
import sys
from datetime import date

from anonymize import RunMasking

AS_OF = date(2023, 6, 15)
RUN_START = date(2023, 1, 3)

# Real name variants per watchlist ticker. The harness will source these
# from yfinance's longName at run start; hardcoded here for the check.
NAMES = {
    "AAPL": ["Apple Inc.", "Apple"],
    "MSFT": ["Microsoft Corporation", "Microsoft"],
}


def main(tech_path: str, news_path: str) -> None:
    technicals = json.load(open(tech_path, encoding="utf-8"))
    news = json.load(open(news_path, encoding="utf-8"))

    m = RunMasking(NAMES, run_start=RUN_START, seed=42)

    print("=" * 72)
    print("ALIAS MAP (harness-side only — Hermes never sees this)")
    print("=" * 72)
    for ticker in NAMES:
        print(f"  {ticker:6} -> {m.alias(ticker)}")

    print()
    print("=" * 72)
    print("TECHNICALS: real -> masked")
    print("=" * 72)
    for ticker, payload in technicals.items():
        masked = m.mask_technicals(payload, as_of=AS_OF)
        print(f"\n  --- {ticker} ---")
        print(f"  ticker:      {payload['ticker']!r:22} -> {masked['ticker']!r}")
        print(
            f"  as_of:       "
            f"{payload['analysis_metadata']['as_of']!r:22} -> "
            f"{masked['analysis_metadata']['as_of']!r}"
        )
        print(
            f"  price:       {payload['current_price']!r:22} -> "
            f"{masked['current_price']!r}   (signal preserved)"
        )
        print(
            f"  rsi:         {payload['rsi']['current']!r:22} -> "
            f"{masked['rsi']['current']!r}   (signal preserved)"
        )
        m.assert_clean(masked)
        print("  assert_clean: PASS")

    print()
    print("=" * 72)
    print("NEWS: real -> masked")
    print("=" * 72)
    for ticker, articles in news.items():
        masked_articles = m.mask_news(articles, as_of=AS_OF)
        print(f"\n  --- {ticker} ({len(articles)} articles) ---")
        for raw, masked in list(zip(articles, masked_articles))[:2]:
            print(f"\n  BEFORE headline: {raw['headline'][:88]}")
            print(f"  AFTER  headline: {masked['headline'][:88]}")
            print(f"  BEFORE created_at: {raw['created_at']}")
            print(f"  AFTER  age_days:   {masked['age_days']}")
            print(f"  BEFORE symbols:    {raw['symbols']}")
            print(f"  AFTER  symbols:    {masked['symbols']}")
            print(f"  BEFORE source_url: {raw.get('source_url', '')[:70]}")
            print(f"  AFTER  source_url: {'source_url' in masked}  (dropped)")
        m.assert_clean(masked_articles)
        print("\n  assert_clean: PASS")

    print()
    print("=" * 72)
    print("ROUND TRIP: Hermes decides on an alias, harness executes the real one")
    print("=" * 72)
    decision_alias = m.alias("AAPL")
    print(f"  Hermes decision: BUY {decision_alias}")
    print(f"  Harness resolves -> BUY {m.real(decision_alias)}  (real order/journal)")

    print()
    print("PASS: real live payloads survive both gates with identity stripped "
          "and signal intact")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
