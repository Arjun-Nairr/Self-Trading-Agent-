"""Plain-python tests for sec_client.py -- no live network access. Every
test patches urllib.request.urlopen (never SecClient._http_get itself),
so caching/rate-limiting/retry/User-Agent logic is exercised for real.
Run: python test_sec_client.py
"""
import gzip
import io
import json
import os
import sys
import tempfile
import urllib.error
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sec_client  # noqa: E402
from sec_client import Cache, RateLimiter, SecClient, SecFetchError, UserAgentMissingError  # noqa: E402

checks = 0


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def check_true(label: str, cond) -> None:
    global checks
    checks += 1
    assert cond, f"{label}: expected truthy"


def fake_response(body: dict, raw_bytes: bytes | None = None, headers: dict | None = None):
    """A fake urlopen() response. Plain-JSON callers pass only `body`
    (unchanged, identity-encoded, no Content-Encoding header). A
    compressed-response test passes `raw_bytes` (the actual compressed
    payload) and `headers` (a real Content-Encoding) instead."""
    payload = raw_bytes if raw_bytes is not None else json.dumps(body).encode()
    hdrs = headers or {}

    class _Resp:
        headers = hdrs

        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def gzip_response(body: dict):
    """A genuinely gzip-compressed JSON response, with the real
    Content-Encoding: gzip header a live SEC response carries -- the
    regression fixture for the UnicodeDecodeError bug (raw compressed
    bytes passed straight to json.loads)."""
    return fake_response(body, raw_bytes=gzip.compress(json.dumps(body).encode()),
                          headers={"Content-Encoding": "gzip"})


def deflate_response(body: dict):
    return fake_response(body, raw_bytes=zlib.compress(json.dumps(body).encode()),
                          headers={"Content-Encoding": "deflate"})


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="u", code=code, msg="err", hdrs=None, fp=io.BytesIO(b""))


def new_client(**kwargs) -> SecClient:
    tmp = Path(tempfile.mkdtemp()) / "test_cache.sqlite3"
    return SecClient(cache=Cache(tmp), rate_limiter=RateLimiter(min_interval=0.0),
                      user_agent="test-suite contact@example.com", **kwargs)


# -- required User-Agent ---------------------------------------------------
os.environ.pop("SEC_EDGAR_USER_AGENT", None)
try:
    sec_client.required_user_agent()
    check_true("missing SEC_EDGAR_USER_AGENT should raise", False)
except UserAgentMissingError as e:
    check_true("missing SEC_EDGAR_USER_AGENT raises UserAgentMissingError", True)
    check_true("error names the required env var", "SEC_EDGAR_USER_AGENT" in str(e))

os.environ["SEC_EDGAR_USER_AGENT"] = "hermes-trading-agent contact@example.com"
check("required_user_agent reads the env var", sec_client.required_user_agent(),
      "hermes-trading-agent contact@example.com")

try:
    SecClient(cache=Cache(Path(tempfile.mkdtemp()) / "c.sqlite3"))
    check_true("SecClient() with env var set constructs cleanly", True)
except UserAgentMissingError:
    check_true("SecClient() with env var set should not raise", False)
os.environ.pop("SEC_EDGAR_USER_AGENT", None)

# -- User-Agent actually sent on every request ------------------------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = fake_response({"ok": True})
    client.get_json("https://data.sec.gov/x.json", use_cache=False)
    sent_request = mock_urlopen.call_args[0][0]
    check("User-Agent header sent on every request", sent_request.get_header("User-agent"),
          "test-suite contact@example.com")
    check("Accept-Encoding header still advertises gzip, deflate",
          sent_request.get_header("Accept-encoding"), "gzip, deflate")

# -- plain (identity/uncompressed) response still parses -- unchanged behavior
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = fake_response({"plain": True})
    result = client.get_json("https://data.sec.gov/plain.json", use_cache=False)
    check("an uncompressed (identity) response still parses as before", result, {"plain": True})

# -- REGRESSION: a genuinely gzip-compressed response must be decoded before
# JSON parsing -- this is the real live failure the fix closes
# (UnicodeDecodeError: 'utf-8' codec can't decode byte 0x8b..., the gzip
# magic byte, because _http_get used to hand compressed bytes straight to
# json.loads). Exercised at the real get_json() seam, not _http_get
# directly, so the whole real call path (cache/rate-limit/retry) is
# covered too.
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = gzip_response({"cik": 320193, "entityName": "Apple Inc."})
    result = client.get_json("https://data.sec.gov/gzipped.json", use_cache=False)
    check("a genuinely gzip-compressed body with Content-Encoding: gzip decodes and parses correctly",
          result, {"cik": 320193, "entityName": "Apple Inc."})

# -- deflate-encoded response also decodes correctly -------------------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = deflate_response({"v": "deflated"})
    result = client.get_json("https://data.sec.gov/deflated.json", use_cache=False)
    check("a deflate-encoded body decodes and parses correctly", result, {"v": "deflated"})

# -- a malformed/truncated gzip body raises a clear SecFetchError, never a
# raw decoding traceback -----------------------------------------------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = fake_response({}, raw_bytes=b"not actually gzip data",
                                                headers={"Content-Encoding": "gzip"})
    try:
        client.get_json("https://data.sec.gov/badgzip.json", use_cache=False)
        check_true("a malformed gzip body should raise", False)
    except SecFetchError as e:
        check_true("malformed gzip body raises SecFetchError, not a raw decoding traceback", True)
        check_true("error names the URL", "badgzip.json" in str(e))
        check_true("error names the encoding", "gzip" in str(e))
    check("a decode failure is never retried (not a transient network error)", mock_urlopen.call_count, 1)

# -- an unsupported Content-Encoding raises a clear SecFetchError ------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = fake_response({}, raw_bytes=b"whatever",
                                                headers={"Content-Encoding": "br"})
    try:
        client.get_json("https://data.sec.gov/brotli.json", use_cache=False)
        check_true("an unsupported Content-Encoding should raise", False)
    except SecFetchError as e:
        check_true("unsupported Content-Encoding raises SecFetchError", True)
        check_true("error names the unsupported encoding", "br" in str(e))

# -- cache hit avoids a second network request ------------------------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = fake_response({"v": 1})
    first = client.get_json("https://data.sec.gov/cached.json")
    second = client.get_json("https://data.sec.gov/cached.json")
    check("cached body returned on second call", second, {"v": 1})
    check("only one real network call made", mock_urlopen.call_count, 1)
check("first call also got the real body", first, {"v": 1})

# -- bypassing cache re-fetches ----------------------------------------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = fake_response({"v": 1})
    client.get_json("https://data.sec.gov/nocache.json")
    client.get_json("https://data.sec.gov/nocache.json", use_cache=False)
    check("use_cache=False always makes a fresh request", mock_urlopen.call_count, 2)

# -- retry on transient failure (5xx) then success --------------------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.side_effect = [http_error(503), fake_response({"v": "recovered"})]
    with patch("sec_client.time.sleep"):
        result = client.get_json("https://data.sec.gov/retry.json")
    check("recovers after one transient 503", result, {"v": "recovered"})
    check("exactly two attempts made", mock_urlopen.call_count, 2)

# -- retry on 429 then success -----------------------------------------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.side_effect = [http_error(429), fake_response({"v": "ok"})]
    with patch("sec_client.time.sleep"):
        result = client.get_json("https://data.sec.gov/ratelimited.json")
    check("recovers after one 429", result, {"v": "ok"})

# -- retry is BOUNDED, never infinite ----------------------------------------
client = new_client(max_retries=2)
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.side_effect = http_error(500)
    with patch("sec_client.time.sleep"):
        try:
            client.get_json("https://data.sec.gov/always500.json")
            check_true("persistent 5xx should eventually raise", False)
        except SecFetchError:
            check_true("persistent 5xx raises SecFetchError after bounded retries", True)
    check("exactly max_retries+1 attempts made, not infinite", mock_urlopen.call_count, 3)

# -- a genuine (non-transient) 4xx is never retried --------------------------
client = new_client()
with patch("sec_client.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.side_effect = http_error(404)
    try:
        client.get_json("https://data.sec.gov/missing.json")
        check_true("404 should raise", False)
    except SecFetchError:
        check_true("404 raises SecFetchError", True)
    check("404 is never retried -- exactly one attempt", mock_urlopen.call_count, 1)

# -- rate limiter paces calls -------------------------------------------------
limiter = RateLimiter(min_interval=0.2)
calls = {"monotonic": [0.0], "slept": []}


def fake_monotonic():
    return calls["monotonic"][-1]


def fake_sleep(secs):
    calls["slept"].append(secs)
    calls["monotonic"][-1] += secs


with patch("sec_client.time.monotonic", side_effect=lambda: calls["monotonic"][-1]), \
     patch("sec_client.time.sleep", side_effect=fake_sleep):
    limiter.wait()  # first call: nothing elapsed yet, but _last_call starts at 0.0 too
    calls["monotonic"][-1] += 0.05  # only 50ms passed before the next call
    limiter.wait()
check_true("rate limiter sleeps when calls arrive faster than min_interval", len(calls["slept"]) >= 1)
check_true("sleep duration brings total spacing up to min_interval",
           abs(calls["slept"][-1] - 0.15) < 1e-6 if calls["slept"] else False)

print(f"OK — {checks} checks passed")
