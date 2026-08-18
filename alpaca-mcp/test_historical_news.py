"""Real proof-of-life for get_historical_news's point-in-time bound -- calls
the actual Alpaca News API (no mock), same pattern as the earlier real
paper-order test. Confirms both that historical data genuinely exists this
far back AND that nothing after as_of ever comes back.

Run: docker exec hermes-trading-agent-alpaca-mcp python test_historical_news.py
"""
import asyncio
from datetime import date, timedelta

from server import get_historical_news


async def main():
    # A liquid, always-newsy ticker and a date safely in the past (not
    # today) so the point-in-time bound actually has something to prove --
    # testing with today's date wouldn't distinguish "correctly bounded"
    # from "just returned everything."
    as_of = (date.today() - timedelta(days=30)).isoformat()
    articles = await get_historical_news("AAPL", as_of, lookback_days=14)

    assert len(articles) > 0, (
        "no articles returned -- either the query is wrong or historical "
        "coverage doesn't reach this far back; either way, investigate "
        "before trusting this tool for backtesting"
    )

    as_of_date = date.fromisoformat(as_of)
    for a in articles:
        published = date.fromisoformat(a["created_at"][:10])
        assert published <= as_of_date, (
            f"future-data leak: article dated {published} returned for "
            f"as_of={as_of_date}"
        )

    print(
        f"PASS: {len(articles)} real historical articles returned for AAPL, "
        f"all dated on/before as_of={as_of}"
    )


if __name__ == "__main__":
    asyncio.run(main())
