"""Real proof-of-life for get_historical_news's point-in-time bound -- calls
the actual Alpaca News API (no mock), same pattern as the earlier real
paper-order test. Confirms both that historical data genuinely exists this
far back AND that nothing after end_at ever comes back.

Run: docker exec hermes-trading-agent-alpaca-mcp python test_historical_news.py
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

from server import get_historical_news


async def main():
    # A liquid, always-newsy ticker and a date safely in the past (not
    # today) so the point-in-time bound actually has something to prove --
    # testing with today's date wouldn't distinguish "correctly bounded"
    # from "just returned everything."
    as_of = date.today() - timedelta(days=30)
    end_at = datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc)
    start_at = end_at - timedelta(days=14)
    result = await get_historical_news("AAPL", start_at.isoformat(), end_at.isoformat())

    assert result["complete"], (
        "pagination did not complete -- investigate before trusting this tool for backtesting"
    )
    articles = result["articles"]
    assert len(articles) > 0, (
        "no articles returned -- either the query is wrong or historical "
        "coverage doesn't reach this far back; either way, investigate "
        "before trusting this tool for backtesting"
    )

    for a in articles:
        published = datetime.fromisoformat(a["created_at"].replace("Z", "+00:00"))
        assert published <= end_at, (
            f"future-data leak: article dated {published} returned for end_at={end_at.isoformat()}"
        )

    print(
        f"PASS: {len(articles)} real historical articles returned for AAPL, "
        f"all dated on/before end_at={end_at.isoformat()}"
    )


if __name__ == "__main__":
    asyncio.run(main())
