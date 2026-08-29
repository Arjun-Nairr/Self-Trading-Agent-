"""Behavioral checks for the live-news veto feed.

Run: python test_news_fetch.py
"""
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from server import fetch_headlines


def test_success_returns_plain_headline_data():
    feed = SimpleNamespace(
        bozo=False,
        status=200,
        entries=[{
            "title": "Example headline",
            "source": {"title": "Example Wire"},
            "published": "Sat, 29 Aug 2026 10:00:00 GMT",
            "link": "https://example.test/story",
        }],
    )
    with patch("server._fetch_feed_bytes", return_value=b"") as fetch, \
         patch("server.feedparser.parse", return_value=feed):
        result = fetch_headlines("AAPL", freshness_hours=2, max_results=1)

    url = fetch.call_args.args[0]
    assert "AAPL+when%3A2h" in url
    assert result["query"] == "AAPL"
    assert result["headlines"] == [{
        "title": "Example headline",
        "source": "Example Wire",
        "published": "Sat, 29 Aug 2026 10:00:00 GMT",
        "link": "https://example.test/story",
    }]


def test_feed_failure_is_not_reported_as_no_news():
    failed_feed = SimpleNamespace(
        bozo=True,
        bozo_exception=RuntimeError("network unavailable"),
        status=503,
        entries=[],
    )
    with patch("server._fetch_feed_bytes", return_value=b""), \
         patch("server.feedparser.parse", return_value=failed_feed):
        try:
            fetch_headlines("AAPL")
        except RuntimeError as exc:
            assert "Google News RSS fetch failed" in str(exc)
        else:
            raise AssertionError("a failed veto feed must fail closed")


def test_bozo_with_real_entries_is_not_treated_as_a_failure():
    """feedparser sets bozo=True for plenty of non-fatal XML quirks that
    Google News RSS trips routinely while still returning real entries --
    that must NOT read as a fetch failure (it isn't the "looks identical
    to zero adverse headlines" ambiguity this guard exists for)."""
    quirky_feed = SimpleNamespace(
        bozo=True,
        bozo_exception=RuntimeError("undeclared namespace prefix"),
        status=200,
        entries=[{
            "title": "Real headline despite a minor XML quirk",
            "source": {"title": "Example Wire"},
            "published": "Sat, 29 Aug 2026 10:00:00 GMT",
            "link": "https://example.test/story",
        }],
    )
    with patch("server._fetch_feed_bytes", return_value=b""), \
         patch("server.feedparser.parse", return_value=quirky_feed):
        result = fetch_headlines("AAPL")
    assert len(result["headlines"]) == 1
    assert result["headlines"][0]["title"] == "Real headline despite a minor XML quirk"


def test_a_hung_or_unreachable_fetch_fails_closed_with_a_timeout():
    """2026-08-29 correctness pass #4 hygiene: the fetch itself is bounded
    (~10s timeout) and any network-level failure (timeout, DNS, connection
    refused) fails closed with a named error -- it can no longer hang the
    whole cycle indefinitely."""
    with patch("server.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        try:
            fetch_headlines("AAPL")
        except RuntimeError as exc:
            assert "Google News RSS fetch failed" in str(exc)
        else:
            raise AssertionError("a hung/unreachable fetch must fail closed, not hang or crash raw")

    with patch(
        "server.urllib.request.urlopen",
        side_effect=urllib.error.URLError("name resolution failed"),
    ):
        try:
            fetch_headlines("AAPL")
        except RuntimeError as exc:
            assert "Google News RSS fetch failed" in str(exc)
        else:
            raise AssertionError("a DNS/connection failure must fail closed, not hang or crash raw")


def main():
    test_success_returns_plain_headline_data()
    print("  ok  successful feed returns plain headline data")
    test_feed_failure_is_not_reported_as_no_news()
    print("  ok  failed feed cannot masquerade as no adverse news")
    test_bozo_with_real_entries_is_not_treated_as_a_failure()
    print("  ok  bozo=True with real entries present is not a false failure")
    test_a_hung_or_unreachable_fetch_fails_closed_with_a_timeout()
    print("  ok  a timed-out or unreachable fetch fails closed with a named error")
    print("PASS: live-news veto feed fails closed")


if __name__ == "__main__":
    main()
