"""sec_client.py — polite, testable HTTP client for SEC EDGAR's public
JSON endpoints. Standard library only (urllib/sqlite3): this is a
standalone host-run module, not a container with its own dependency set,
and stdlib already covers everything it needs.

Owns exactly three things: the required User-Agent contract, rate
limiting + bounded retries, and a local SQLite response cache. Fact
selection/normalization lives in edgar.py -- this file only fetches and
caches raw JSON.

`SecClient._http_get` is the one real network call. Every test in
test_sec_client.py / test_edgar.py patches `urllib.request.urlopen`
(never `_http_get` itself), so caching/rate-limiting/retry logic is
exercised for real -- nothing here is mocked away.
"""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"

# SEC's documented cap is 10 requests/second; V1 runs conservatively
# below that per the project's own SEC access rules.
MAX_REQUESTS_PER_SECOND = 5.0
MIN_REQUEST_INTERVAL = 1.0 / MAX_REQUESTS_PER_SECOND

# Bounded, not infinite: at most this many retries beyond the first
# attempt, only for transient failures (429, 5xx, or a raw network
# error/timeout with no HTTP status at all).
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0

CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_DB_PATH = CACHE_DIR / "sec_edgar_cache.sqlite3"


class UserAgentMissingError(RuntimeError):
    """Raised instead of silently sending an unlabeled request -- SEC
    requires a declared User-Agent on every automated request, and a
    missing one must fail loudly and immediately, not fall back to a
    generic default that could get the project's IP blocked."""


class SecFetchError(RuntimeError):
    """A SEC request failed and either wasn't transient (4xx other than
    429) or exhausted its bounded retries."""


def required_user_agent() -> str:
    """Reads SEC_EDGAR_USER_AGENT from the environment. Never a default,
    never a personal email hardcoded here -- see fundamentals/README.md
    for the exact format SEC expects ('ProjectName contact@example.com')."""
    ua = os.environ.get(USER_AGENT_ENV, "").strip()
    if not ua:
        raise UserAgentMissingError(
            f"{USER_AGENT_ENV} is required and not set. SEC EDGAR requires a "
            "declared User-Agent identifying the requester on every request "
            "(format: 'ProjectName contact@example.com') -- refusing to fetch "
            "without one rather than sending an unlabeled or generic request."
        )
    return ua


class RateLimiter:
    """ponytail: process-local, single-threaded pacing via a simple
    'wait until min_interval has elapsed since the last call' clock --
    correct for an on-demand CLI/module that is never called concurrently
    or inside a hot loop (V1's whole point). A shared/multi-process
    limiter would need real token storage; add one only if this module
    is ever called from more than one process at once."""

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL):
        self.min_interval = min_interval
        self._last_call = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()


class Cache:
    """SQLite cache of raw SEC JSON responses, keyed by URL. Lives under
    this module's own gitignored runtime cache directory (fundamentals/.cache/)
    -- a data cache for avoiding repeat SEC fetches, never a second trade
    journal. `fetched_at` is stored alongside the body so a caller can
    reason about staleness later without a second table."""

    def __init__(self, db_path: Path = CACHE_DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "  url TEXT PRIMARY KEY,"
            "  body TEXT NOT NULL,"
            "  fetched_at TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    def get(self, url: str) -> dict | None:
        row = self._conn.execute(
            "SELECT body FROM responses WHERE url = ?", (url,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, url: str, body: dict) -> None:
        self._conn.execute(
            "INSERT INTO responses (url, body, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET body = excluded.body, fetched_at = excluded.fetched_at",
            (url, json.dumps(body), datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class SecClient:
    """Fetches SEC's three public JSON endpoints, cache-first, with a
    required User-Agent, paced requests, and bounded retries. No API key
    -- these are public, unauthenticated endpoints."""

    TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
    COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

    def __init__(self, cache: Cache | None = None, rate_limiter: RateLimiter | None = None,
                 max_retries: int = MAX_RETRIES, user_agent: str | None = None):
        # Resolved eagerly (not lazily on first request) so a missing
        # User-Agent fails at construction, before any caller assumes a
        # working client exists.
        self.user_agent = user_agent or required_user_agent()
        self.cache = cache if cache is not None else Cache()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.max_retries = max_retries

    def _http_get(self, url: str) -> dict:
        """The one real network call -- tests patch urllib.request.urlopen,
        not this method, so everything around it stays real."""
        request = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
        return json.loads(raw)

    def get_json(self, url: str, use_cache: bool = True) -> dict:
        """Cache-first read of one SEC JSON endpoint. Only 429/5xx/raw
        network errors are retried, bounded by max_retries -- a genuine
        4xx (404, 403, ...) fails immediately, never silently retried
        into a false "still trying" state."""
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.rate_limiter.wait()
            try:
                body = self._http_get(url)
            except urllib.error.HTTPError as e:
                if e.code != 429 and not (500 <= e.code < 600):
                    raise SecFetchError(f"SEC request failed ({e.code}) for {url}") from e
                last_error = e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
            else:
                self.cache.set(url, body)
                return body

            if attempt < self.max_retries:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

        raise SecFetchError(
            f"SEC request failed after {self.max_retries + 1} attempts: {url}"
        ) from last_error
