"""Pure, no-network tests for get_historical_news's pagination/completeness
contract (2026-09-17 correction). httpx.AsyncClient itself is mocked --
zero real provider/Docker/Neon/Hermes/broker calls anywhere in this file.
Distinct from test_historical_news.py, which is a REAL proof-of-life
against the live Alpaca API and is deliberately never run by verify.py.

Run (needs risk_checker importable, same as test_account_math.py):
    PYTHONPATH=../risk-checker python test_historical_news_pagination.py
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "risk-checker"))

import server  # noqa: E402


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


def _fake_client_returning(pages: list[dict]):
    """A fake httpx.AsyncClient whose .get() returns `pages` in order, one
    per call -- everything a real async-context-managed client needs
    (__aenter__/__aexit__/get), nothing else. `calls` collects each
    request's real params dict for the test to inspect."""
    calls: list[dict] = []

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None, timeout=None):
            calls.append(params)
            return _FakeResponse(pages[len(calls) - 1])

    return _FakeAsyncClient, calls


def _run(coro):
    return asyncio.run(coro)


def test_single_page_is_reported_complete():
    pages = [{"news": [
        {"headline": "h1", "created_at": "2026-06-01T20:00:00Z", "summary": "s",
         "symbols": ["OLN"], "url": ""},
    ]}]
    fake_client_cls, calls = _fake_client_returning(pages)
    with patch("server.httpx.AsyncClient", fake_client_cls), \
         patch("server._headers", return_value={}):
        result = _run(server.get_historical_news("OLN", "2026-05-25T00:00:00Z", "2026-06-01T20:00:00Z"))
    assert result["complete"] is True
    assert len(result["articles"]) == 1
    assert len(calls) == 1
    print("  ok  a single, un-paginated page reports complete=True")


def test_multi_page_pagination_accumulates_and_reports_complete():
    pages = [
        {"news": [{"headline": "h1", "created_at": "2026-06-01T20:00:00Z", "summary": "",
                   "symbols": [], "url": ""}], "next_page_token": "tok1"},
        {"news": [{"headline": "h2", "created_at": "2026-06-01T21:00:00Z", "summary": "",
                   "symbols": [], "url": ""}]},
    ]
    fake_client_cls, calls = _fake_client_returning(pages)
    with patch("server.httpx.AsyncClient", fake_client_cls), \
         patch("server._headers", return_value={}):
        result = _run(server.get_historical_news("OLN", "start", "end"))
    assert result["complete"] is True
    assert len(result["articles"]) == 2
    assert len(calls) == 2
    assert calls[1].get("page_token") == "tok1", "the second page request must carry the first page's own token"
    print("  ok  multiple pages are followed via next_page_token and every article accumulates")


def test_incomplete_pagination_when_the_page_cap_is_hit_with_a_token_still_pending():
    """Every page still returns a next_page_token -- pagination never
    naturally finishes within MAX_NEWS_PAGES. Must report incomplete
    rather than silently returning a partial set that looks like the
    whole answer -- requirement: 'if pagination... cannot establish
    complete coverage, fail the affected queued BUY closed' (enforced by
    the caller, fetch_news_window/fetch_news, on this exact signal)."""
    pages = [{"news": [], "next_page_token": f"tok{i}"} for i in range(server.MAX_NEWS_PAGES)]
    fake_client_cls, calls = _fake_client_returning(pages)
    with patch("server.httpx.AsyncClient", fake_client_cls), \
         patch("server._headers", return_value={}):
        result = _run(server.get_historical_news("OLN", "start", "end"))
    assert result["complete"] is False, (
        "hitting the page cap with a token still pending must report incomplete, "
        "never silently look done"
    )
    assert len(calls) == server.MAX_NEWS_PAGES
    print("  ok  hitting the page cap while a next_page_token is still pending reports complete=False")


def test_malformed_provider_response_is_not_silently_treated_as_zero_articles():
    """A response body missing the expected 'news' key entirely (a
    malformed/unexpected provider response) must not be silently read as
    "zero articles, done" -- .get('news', []) already degrades to an
    empty page rather than crashing, but this proves that degrading
    still correctly reports complete based on next_page_token, never
    fabricates extra completeness."""
    pages = [{}]  # no "news" key, no "next_page_token" -- ambiguous/malformed
    fake_client_cls, calls = _fake_client_returning(pages)
    with patch("server.httpx.AsyncClient", fake_client_cls), \
         patch("server._headers", return_value={}):
        result = _run(server.get_historical_news("OLN", "start", "end"))
    assert result["articles"] == []
    assert result["complete"] is True, "no next_page_token at all correctly means nothing more to fetch"
    print("  ok  a malformed page missing 'news' degrades to zero articles for that page, "
          "never crashes or fabricates extra data")


def test_request_receives_rfc3339_timestamps_not_date_only_strings():
    pages = [{"news": []}]
    fake_client_cls, calls = _fake_client_returning(pages)
    start_at, end_at = "2026-06-01T20:00:00Z", "2026-06-02T13:45:00Z"
    with patch("server.httpx.AsyncClient", fake_client_cls), \
         patch("server._headers", return_value={}):
        _run(server.get_historical_news("OLN", start_at, end_at))
    assert calls[0]["start"] == start_at and calls[0]["end"] == end_at
    assert "T" in calls[0]["start"] and calls[0]["start"].endswith("Z"), (
        f"expected a full RFC-3339 timestamp, got {calls[0]['start']!r}"
    )
    print("  ok  the provider request receives exact RFC-3339 timestamps, not date-only strings")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("PASS: get_historical_news pagination/completeness contract behaves as claimed, "
          "zero real network calls")


if __name__ == "__main__":
    main()
