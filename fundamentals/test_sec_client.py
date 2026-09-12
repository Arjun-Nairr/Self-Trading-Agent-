"""Plain-python tests for sec_client.py -- no live network access. Every
test patches urllib.request.urlopen (never SecClient._http_get itself),
so caching/rate-limiting/retry/User-Agent logic is exercised for real.
Run: python test_sec_client.py
"""
import io
import json
import os
import sys
import tempfile
import urllib.error
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


def fake_response(body: dict):
    class _Resp:
        def read(self):
            return json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


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
