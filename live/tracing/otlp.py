"""Observational tracing core: redaction, OTLP/JSON encoding and a bounded background exporter.

Standard library only. It is shared by the host-side controller (`controller.py`) and, copied beside it,
by the Hermes tracing plugin that runs inside the gateway container. Nothing here may raise into, wait on
or otherwise change trading: every public entry point swallows its own errors, the queue is bounded, network
and warning-log I/O happen only on daemon threads, a failing endpoint trips a circuit breaker so later spans are
dropped without any network wait, and nothing waits for delivery at decision return, turn completion or process
exit. Dropping telemetry is always preferred to delaying trading.

Export target: Langfuse's OTLP/HTTP-JSON endpoint (`/api/public/otel/v1/traces`).
"""
from __future__ import annotations

import base64
import collections
import functools
import http.client
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any, Callable

MAX_CHARS = 8000                 # per string field, before the truncation marker
MAX_LIST = 200
MAX_KEYS = 100
MAX_DEPTH = 10
MAX_ATTR_BYTES = 65_536          # per exported attribute (JSON-encoded)
MAX_PARSE = 100_000              # longest string we will try to read as JSON (anything larger is clipped first)
REDACTED = "[REDACTED]"
SPOOL_EXIT_MAX = 2_000_000       # bytes one process exit may write to the spool (approved proposal)
SPOOL_DIR_MAX = 16_000_000       # bytes a spool directory may hold; an exit that would exceed it writes nothing
SPOOL_STALE_CLAIM_S = 900        # a claimed file untouched this long belongs to a dead process and is reclaimed

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:token|secrets?|passwords?|passwd|pwd|pass|passphrase|authorization|auth|cookies?|"
    r"credentials?|apikey|api_keys?|private_keys?|access_keys?|secret_keys?|account_(?:id|number|no)|dsn)(?:$|_)")
_BENIGN_LAST = {"count", "usage", "limit", "budget", "total", "type", "ttl", "expires", "expiry", "length", "len",
                "size", "used", "status"}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NONWORD = re.compile(r"[^a-z0-9]+")


@functools.lru_cache(maxsize=4096)                     # keys repeat enormously (every row of a table)
def _sensitive(key: str) -> bool:
    norm = _NONWORD.sub("_", _CAMEL.sub("_", key).lower()).strip("_")
    return bool(_SENSITIVE_KEY.search(norm)) and norm.rsplit("_", 1)[-1] not in _BENIGN_LAST


def is_sensitive_key(key: Any) -> bool:
    """camelCase / kebab / UPPER_SNAKE aware: `accessToken`, `x-api-key`, `AWS_SECRET_ACCESS_KEY`, `clientSecret`.
    Usage counters such as `input_tokens`, `max_tokens` or `token_count` are not secrets and stay readable."""
    return _sensitive(str(key)[:200])


# Every quantifier below is bounded where a trailing constraint could make it backtrack, so no pattern can go
# quadratic on hostile text.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[a-z][a-z0-9+.\-]{1,20}://[^\s/@:'\"<>]{1,512}:[^\s/@'\"<>]{1,512}@[^\s'\"<>]{1,1024}", re.I),
     "[REDACTED_URL_WITH_CREDENTIALS]"),
    (re.compile(r"\bpostgres(?:ql)?(?:\+\w+)?://[^\s'\"<>]+", re.I), "[REDACTED_CONNECTION_STRING]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|pk)-lf-[A-Za-z0-9\-]{8,}"), "[REDACTED_LANGFUSE_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,2048}\.[A-Za-z0-9_\-]{8,2048}\.[A-Za-z0-9_\-]{8,2048}"), "[REDACTED_JWT]"),
    (re.compile(r"\b(?:PK|AK)[A-Z0-9]{18,64}\b"), "[REDACTED_BROKER_KEY]"),
    (re.compile(r"\bPA[0-9A-Z]{8,32}\b"), "[ACCOUNT_ID]"),
    (re.compile(r"(?i)(\b(?:password|passcode|passphrase|secret|api[ _\-]?key|token)\s{1,20}(?:is|was|=)\s{1,20})"
                r"[\"']?[^\s\"',;]+"), r"\1" + REDACTED),
    (re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,100}\.[A-Za-z]{2,10}"), "[EMAIL]"),
    (re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+"), "<user-dir>"),
    (re.compile(r"(?:/home|/Users)/[^/\s\"']+"), "<user-dir>"),
]

# Cheap substring prefilters, aligned with _PATTERNS: a pattern runs only if one of its needles occurs.
_NEEDLES: list[tuple[str, ...] | None] = [
    ("://",), ("postgres",), ("bearer",), ("-lf-",), ("sk-",), ("akia",), ("eyj",), ("pk", "ak"), ("pa",),
    ("password", "passcode", "passphrase", "secret", "key", "token"), ("@",), ("users",), ("/home/", "/users/")]
assert len(_NEEDLES) == len(_PATTERNS)
_NEEDLE_RX = [re.compile("|".join(map(re.escape, n))) for n in _NEEDLES]
_PAIR_NEEDLES = ("token", "secret", "pass", "pwd", "auth", "cookie", "credential", "key", "dsn")
_PAIR_RX = re.compile("|".join(_PAIR_NEEDLES))
_QUICK = re.compile("|".join(sorted({x for n in _NEEDLES for x in n} | set(_PAIR_NEEDLES))))   # on lower-cased text
CLIP_SLACK = 256

# `name<closing quote, maybe backslash-escaped><space>:|=<space>`: JSON, escaped/nested JSON, repr, YAML, env, query
_KEY_SEP = re.compile(r"([A-Za-z][A-Za-z0-9_.\-]{0,63})(?:\\*[\"'])?[ \t]*[:=][ \t]*")
_SCHEME = re.compile(r"(?i)(?:basic|bearer|token|digest|negotiate)[ \t]+")
_BARE = re.compile(r"[^\s\"',;&)}\]]+")
_EMPTYISH = {"null", "none", "true", "false", ""}


def _value_span(s: str, i: int) -> tuple[int, int, str] | None:
    """(start, end, replacement) of the value that starts at s[i], or None if there is nothing to hide. Linear in
    the text after i; an unterminated quote or bracket swallows the rest (over-redaction, never a fragment)."""
    n = len(s)
    if i >= n or s.startswith(REDACTED, i):
        return None
    k = i
    while k < n and s[k] == "\\":                                   # (possibly escaped) opening quote
        k += 1
    if k < n and s[k] in "\"'":
        lvl, q, p = k - i, s[k], k + 1
        while True:
            e = s.find(q, p)
            if e < 0:
                return (k + 1, n, REDACTED) if k + 1 < n else None
            b = 0
            while e - 1 - b >= k + 1 and s[e - 1 - b] == "\\":      # backslashes immediately before this quote
                b += 1
            if b == lvl:                                           # the closing quote is at the opening's escape level
                return (k + 1, e - lvl, REDACTED) if e - lvl > k + 1 else None
            p = e + 1
    if s[i] in "{[":
        depth = 0
        for j in range(i, n):
            c = s[j]
            depth += (c in "{[") - (c in "}]")
            if depth <= 0:
                return (i, j + 1, '"' + REDACTED + '"')
        return (i, n, '"' + REDACTED + '"')
    m = _SCHEME.match(s, i)
    start = m.end() if m else i
    m = _BARE.match(s, start)
    if not m or m.group(0).lower() in _EMPTYISH or m.group(0).startswith(REDACTED):
        return None
    return (start, m.end(), REDACTED)


def _redact_pairs(s: str) -> str:
    out, pos, last = [], 0, 0
    while True:
        m = _KEY_SEP.search(s, pos)
        if m is None:
            break
        pos = m.end()
        if not is_sensitive_key(m.group(1)):
            continue
        span = _value_span(s, m.end())
        if span is None:
            continue
        a, b, repl = span
        out.append(s[last:a])
        out.append(repl)
        last, pos = b, max(pos, b)
    out.append(s[last:])
    return "".join(out)


def _redact_core(s: str) -> str:
    low = s.lower()
    if _QUICK.search(low) is None:                      # nothing in here can match any pattern (most short strings)
        return s
    for (rx, repl), needle in zip(_PATTERNS, _NEEDLE_RX, strict=True):
        if needle.search(low):
            s = rx.sub(repl, s)
    if _PAIR_RX.search(low):
        s = _redact_pairs(s)
    return s


def redact_text(s: Any) -> str:
    """Redact one piece of text. Always bounded: anything longer than a field is clipped (and marked) first."""
    s = s if isinstance(s, str) else str(s)
    return clip_redact(s) if len(s) > MAX_CHARS + CLIP_SLACK else _redact_core(s)


def truncate(s: str, limit: int = MAX_CHARS) -> str:
    return s if len(s) <= limit else s[:limit] + f"...[truncated {len(s) - limit} chars]"


def clip_redact(s: str, limit: int = MAX_CHARS) -> str:
    """Bound the text BEFORE running any pattern over it (so a multi-megabyte value costs O(limit)), cutting
    back to a separator so no half-token is left at the edge; redact the kept part; mark the truncation."""
    n = len(s)
    if n <= limit + CLIP_SLACK:
        return truncate(_redact_core(s), limit)
    cut = s[:limit + CLIP_SLACK]
    sep = max(cut.rfind(c) for c in (" ", chr(10), ",", '"', ";"))
    if sep > limit - 200:
        cut = cut[:sep]
    red = _redact_core(cut)
    return red[:limit] + f"...[truncated {n - min(len(red), limit)} chars]"


def _as_json(s: str, limit: int, depth: int) -> str | None:
    """A string that is itself a JSON object/array is redacted by structure (sensitive KEYS, at any nesting
    depth, including JSON encoded inside JSON strings); None when it is not JSON or is too large to parse."""
    if len(s) > MAX_PARSE or s.lstrip()[:1] not in ("{", "["):
        return None
    try:
        obj = json.loads(s)
    except Exception:                                  # ValueError, RecursionError, ...
        return None
    if not isinstance(obj, (dict, list)):
        return None
    return truncate(json.dumps(sanitize(obj, limit, depth + 1), ensure_ascii=False, default=str), limit)


def sanitize(obj: Any, limit: int = MAX_CHARS, _depth: int = 0) -> Any:
    """Redact-then-truncate any value into JSON-safe primitives. Truncation is always marked in-band."""
    if _depth > MAX_DEPTH:
        return "[max depth reached]"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        as_json = _as_json(obj, limit, _depth)
        return as_json if as_json is not None else clip_redact(obj, limit)
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= MAX_KEYS:
                out["__truncated__"] = f"{len(obj) - MAX_KEYS} more keys"
                break
            key = redact_text(str(k))[:200]
            hide = is_sensitive_key(k) and v is not None and not isinstance(v, bool)
            out[key] = REDACTED if hide else sanitize(v, limit, _depth + 1)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = list(obj)
        out_l = [sanitize(v, limit, _depth + 1) for v in items[:MAX_LIST]]
        if len(items) > MAX_LIST:
            out_l.append(f"[... {len(items) - MAX_LIST} more items truncated]")
        return out_l
    return clip_redact(repr(obj), limit)


def describe_exception(exc: BaseException) -> str:
    """Type and redacted message only: no traceback (paths, locals), never the raw secret-bearing text."""
    try:
        text = str(exc)
    except Exception:
        text = "<unprintable>"
    return truncate(redact_text(f"{type(exc).__name__}: {text}"), 500)


def encode_attr(value: Any, limit: int = MAX_CHARS) -> str:
    """JSON-encode one exported attribute with a hard cap of MAX_ATTR_BYTES UTF-8 bytes on the complete result.
    An oversized value becomes a marked envelope whose preview (a prefix of the redacted encoding) is shrunk until
    the whole envelope, escapes included, fits; a few passes at most, since each shrinks in proportion."""
    s = json.dumps(sanitize(value, limit), ensure_ascii=False, default=str)
    size = len(s.encode("utf-8", "surrogatepass"))
    if size <= MAX_ATTR_BYTES:
        return s
    keep = MAX_ATTR_BYTES // 2
    while True:
        out = json.dumps({"truncated": True, "original_bytes": size, "original_chars": len(s),
                          "preview_chars": keep, "preview": s[:keep]}, ensure_ascii=False)
        n = len(out.encode("utf-8", "surrogatepass"))
        if n <= MAX_ATTR_BYTES or keep == 0:
            return out
        keep = max(0, min(keep - 1, keep * (MAX_ATTR_BYTES - 200) // n))


# -- ids / spans / OTLP ---------------------------------------------------------------------------------

def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def valid_id(value: Any, n: int) -> bool:
    return isinstance(value, str) and len(value) == n and re.fullmatch(r"[0-9a-f]+", value) is not None \
        and set(value) != {"0"}


def _label(v: Any, limit: int) -> str:
    return truncate(redact_text(v), limit)


def observation_attrs(kind: str, *, input: Any = None, output: Any = None, metadata: dict | None = None,
                      input_limit: int = MAX_CHARS,
                      level: str | None = None, status_message: str | None = None, usage: dict | None = None,
                      model: str | None = None, trace_name: str | None = None, session: str | None = None,
                      environment: str | None = None, release: str | None = None) -> dict[str, Any]:
    a: dict[str, Any] = {"langfuse.observation.type": kind}
    if input is not None:
        a["langfuse.observation.input"] = encode_attr(input, input_limit)
    if output is not None:
        a["langfuse.observation.output"] = encode_attr(output)
    for k, v in ((k, v) for k, v in (metadata or {}).items() if v is not None):
        a[f"langfuse.observation.metadata.{re.sub(r'[^A-Za-z0-9_.-]', '_', str(k))[:60]}"] = (
            v if isinstance(v, (bool, int, float)) else encode_attr(v) if not isinstance(v, str)
            else _label(v, 1000))
    if level:
        a["langfuse.observation.level"] = level
    if status_message:
        a["langfuse.observation.status_message"] = _label(status_message, 500)
    if usage:
        a["langfuse.observation.usage_details"] = json.dumps({k: v for k, v in usage.items()
                                                              if isinstance(v, (int, float))})
    if model:
        a["langfuse.observation.model.name"] = _label(model, 200)
    if trace_name:
        a["langfuse.trace.name"] = _label(trace_name, 200)
    if session:
        a["langfuse.session.id"] = _label(session, 200)
    if environment:
        a["langfuse.environment"] = _label(environment, 200)
    if release:
        a["langfuse.release"] = _label(release, 200)
    return a


def span_record(trace_id: str, span_id: str, parent_id: str | None, name: str, start_ns: int, end_ns: int,
                attrs: dict[str, Any], error: bool = False, message: str | None = None) -> dict:
    return {"trace_id": trace_id, "span_id": span_id, "parent_id": parent_id, "name": _label(name, 200),
            "start_ns": int(start_ns), "end_ns": int(max(end_ns, start_ns)), "attrs": attrs,
            "error": bool(error), "message": message}


def _any_value(v: Any) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": _final(str(v))}


def _final(text: str) -> str:
    """Last line of defence, on the exporter thread: every exported string passes the redactor again, whatever
    built it. Idempotent on already-redacted text."""
    return _redact_core(text) if len(text) <= 2 * MAX_ATTR_BYTES else clip_redact(text, MAX_ATTR_BYTES)


def to_otlp(spans: list[dict], service: str, scope: str = "hermes-trading-agent.live") -> dict:
    out = []
    for s in spans:
        o = {"traceId": s["trace_id"], "spanId": s["span_id"], "name": _final(s["name"]), "kind": 1,
             "startTimeUnixNano": str(s["start_ns"]), "endTimeUnixNano": str(s["end_ns"]),
             "attributes": [{"key": k, "value": _any_value(v)} for k, v in s["attrs"].items()],
             "status": {"code": 2 if s["error"] else 1}}
        if s.get("parent_id"):
            o["parentSpanId"] = s["parent_id"]
        if s.get("message"):
            o["status"]["message"] = _label(s["message"], 500)
        out.append(o)
    return {"resourceSpans": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"scope": {"name": scope, "version": "1"}, "spans": out}]}]}


# -- bounded background exporter -----------------------------------------------------------------------------

class Exporter:
    """Non-blocking span sink. `submit` never waits and never touches the network or the filesystem; a daemon
    thread sends batches; failures open a circuit so subsequent batches are not sent (kept for the spool instead).

    Nothing in production waits for delivery. Without `spool_dir`, `close()` only stops intake and a process that
    exits abandons whatever is still queued. With `spool_dir` (the approved exit spool), `close()` also writes every
    span not yet acknowledged -- queued, in flight, failed, or held back by the open circuit -- already redacted and
    OTLP-encoded, to one new file there: a bounded local write (<= SPOOL_EXIT_MAX bytes, directory <= SPOOL_DIR_MAX),
    never a network wait. The next invocation's worker sends those files first, in the background, and deletes a
    file only once every line in it was accepted. Delivery therefore needs another invocation, a re-sent span keeps
    its original ids (the backend upserts by id), and the last invocation's records can stay local indefinitely.
    `flush()` exists for tests and diagnostics and has no production caller."""

    def __init__(self, base_url: str, public_key: str, secret_key: str, *, service: str = "hermes-live",
                 queue_max: int = 512, batch_max: int = 64, timeout_s: float = 2.0, interval_s: float = 0.5,
                 cooldown_s: float = 60.0, fail_threshold: int = 2, post: Callable[[bytes], int] | None = None,
                 warn_path: str | None = None, spool_dir: str | None = None,
                 scope: str = "hermes-trading-agent.live"):
        self.base_url, self.service, self.scope = base_url.rstrip("/"), service, scope
        self._auth = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.queue_max, self.batch_max, self.timeout_s = queue_max, batch_max, timeout_s
        self.interval_s, self.cooldown_s, self.fail_threshold = interval_s, cooldown_s, fail_threshold
        self._post = post or self._http_post
        self._q: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self._inflight = 0
        self._thread: threading.Thread | None = None
        self._closed = False
        self._fails = 0
        self._open_until = 0.0
        self.warnings: collections.deque = collections.deque(maxlen=20)      # in memory, always
        self.warn_path = warn_path
        self._unsaved: collections.deque = collections.deque(maxlen=20)      # waiting for the writer thread
        self._unsaved_ev = threading.Event()
        self._warn_thread: threading.Thread | None = None
        self._saved_drops = 0
        self.spool_dir = spool_dir
        self._sending: list | None = None                                    # the batch on the wire right now
        self._unacked: collections.deque = collections.deque()               # encoded bodies that failed / held back
        self._unacked_bytes = 0
        self._claim: dict | None = None                                      # the spool file being replayed
        self._replaying = False
        self.stats = {"submitted": 0, "sent": 0, "dropped_queue_full": 0, "dropped_circuit_open": 0,
                      "failed_batches": 0, "spooled": 0, "dropped_spool_full": 0, "spool_write_failed": 0,
                      "replayed": 0, "spool_invalid": 0, "spool_rejected": 0}

    # -- public, never raises
    def submit(self, span: dict) -> None:
        try:
            with self._cv:
                if self._closed:
                    return
                if len(self._q) >= self.queue_max:
                    self.stats["dropped_queue_full"] += 1                    # O(1), in memory: no I/O on this path
                    self._note("export queue full; dropping spans")
                    return
                self._q.append(span)
                self.stats["submitted"] += 1
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="trace-export", daemon=True)
                    self._thread.start()
                    if self.warn_path:
                        self._warn_thread = threading.Thread(target=self._warn_run, name="trace-warn", daemon=True)
                        self._warn_thread.start()
                self._cv.notify_all()
        except Exception:
            pass

    def close(self) -> None:
        """Stop accepting spans. Never waits for the network. Without a spool, queued spans are still sent for as
        long as the process lives; with one, everything unacknowledged is written to it now (see the class doc)."""
        try:
            with self._cv:
                if self._closed:
                    return
                self._closed = True
                taken = None
                if self.spool_dir:                            # take ownership under the lock: the worker sends no more
                    taken = (list(self._unacked), list(self._sending or []) + list(self._q), self._claim)
                    self._q.clear(), self._unacked.clear()
                    self._claim = None
                self._cv.notify_all()
            if taken:
                self._spool_exit(*taken)
        except Exception:
            pass

    def flush(self, timeout: float = 2.0) -> bool:
        """TESTS AND DIAGNOSTICS ONLY (no production caller): True if everything queued has been handed off
        (sent or deliberately dropped) within `timeout`."""
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            with self._cv:
                self._cv.notify_all()
                while self._q or self._inflight:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._cv.wait(min(left, 0.05))
            return True
        except Exception:
            return False

    # -- worker
    def _run(self) -> None:
        if self.spool_dir:
            self._replay()
        while True:
            with self._cv:
                while not self._q and not self._closed:
                    self._cv.wait(self.interval_s)
                if self._closed and not self._q:
                    return
                batch = [self._q.popleft() for _ in range(min(self.batch_max, len(self._q)))]
                self._inflight += 1
                self._sending = batch
            try:
                self._send(batch)
            except Exception as exc:                          # never let the worker die noisily
                self._note(f"export worker error: {type(exc).__name__}")
            finally:
                with self._cv:
                    self._inflight -= 1
                    self._sending = None
                    self._cv.notify_all()

    def _send(self, batch: list[dict]) -> None:
        if time.monotonic() < self._open_until:               # circuit open: no network wait
            if self.spool_dir:
                self._keep(json.dumps(to_otlp(batch, self.service, self.scope)).encode("utf-8"), len(batch))
            else:
                self.stats["dropped_circuit_open"] += len(batch)
            return
        body = json.dumps(to_otlp(batch, self.service, self.scope)).encode("utf-8")
        status = None
        for attempt in (1, 2):                                # bounded retry: one retry, retryable errors only
            try:
                status = self._post(body)
            except Exception as exc:
                status = None
                err = type(exc).__name__
            else:
                err = f"HTTP {status}"
            if status is not None and 200 <= status < 300:
                self._fails = 0
                self.stats["sent"] += len(batch)
                return
            if status is not None and 400 <= status < 500 and status != 429:
                break                                         # auth / schema problem: retrying cannot help
            if attempt == 1:
                time.sleep(0.2)
        self._fails += 1
        self.stats["failed_batches"] += 1
        if self.spool_dir and not (status is not None and 400 <= status < 500 and status not in (401, 403, 408, 429)):
            self._keep(body, len(batch))                      # recoverable: kept for the exit spool
            self._note(f"export failed ({err}); {len(batch)} spans kept for the exit spool")
        else:
            self._note(f"export failed ({err}); {len(batch)} spans dropped")
        if self._fails >= self.fail_threshold:
            self._open_until = time.monotonic() + self.cooldown_s

    # -- exit spool (approved): local files, written once per exit, replayed by the next invocation's worker
    def _keep(self, body: bytes, n: int) -> None:
        with self._cv:
            if self._closed:                                  # already in the exit spool as the in-flight copy
                return
            if self._unacked_bytes + len(body) > SPOOL_EXIT_MAX:
                self.stats["dropped_spool_full"] += n
                return
            self._unacked.append(body)
            self._unacked_bytes += len(body)

    def _spool_exit(self, bodies: list[bytes], spans: list[dict], claim: dict | None) -> None:
        """At close: hand back an interrupted replay, then write the unacknowledged spans as one new file. Every
        drop is counted in `stats` and, if a warning log is configured, written to it (a single append)."""
        lines = list(bodies)
        try:
            for i in range(0, len(spans), self.batch_max):
                chunk = spans[i:i + self.batch_max]
                lines.append(json.dumps(to_otlp(chunk, self.service, self.scope)).encode("utf-8"))
        except Exception:
            pass
        total = sum(_span_count(b) for b in lines)
        problems = []
        try:
            if claim is not None:                             # unsent rest of a replay this process had claimed
                rest = claim["lines"][claim["next"]:]
                if rest:
                    _spool_write(self.spool_dir, rest)
                os.remove(claim["path"])
            keep, size = [], 0
            for b in lines:
                if size + len(b) + 1 > SPOOL_EXIT_MAX:
                    break
                keep.append(b)
                size += len(b) + 1
            if keep and _spool_size(self.spool_dir) + size > SPOOL_DIR_MAX:
                keep = []
                problems.append(f"spool directory full ({SPOOL_DIR_MAX} bytes)")
            elif len(keep) < len(lines):
                problems.append(f"exit spool capped at {SPOOL_EXIT_MAX} bytes")
            if keep:
                _spool_write(self.spool_dir, keep)
            kept = sum(_span_count(b) for b in keep)
            self.stats["spooled"] += kept
            self.stats["dropped_spool_full"] += total - kept
        except Exception as exc:
            self.stats["spool_write_failed"] += total
            problems.append(f"spool write failed ({type(exc).__name__})")
        if problems:
            lost = self.stats["dropped_spool_full"] + self.stats["spool_write_failed"]
            self._persist([f"{p}; {lost} spans not spooled at exit" for p in problems])

    def _replay(self) -> None:
        """Send earlier invocations' spool files, oldest first. A file is claimed by an atomic rename (so two
        processes never replay the same file), sent line by line, and deleted only when every valid line was
        accepted; on a failure the unsent rest goes back to the spool and replay stops until the next invocation."""
        self._replaying = True
        try:
            for path in _spool_candidates(self.spool_dir):
                claimed = f"{path.split('.jsonl')[0]}.jsonl.claimed-{os.getpid()}-{secrets.token_hex(4)}"
                try:
                    os.rename(path, claimed)
                    os.utime(claimed)
                    raw = open(claimed, "rb").read()
                except OSError:
                    continue                                  # another process claimed it first
                valid = [ln for ln in raw.split(b"\n") if ln.strip() and _valid_line(ln)]
                self.stats["spool_invalid"] += sum(1 for ln in raw.split(b"\n") if ln.strip()) - len(valid)
                with self._cv:
                    if self._closed:
                        if valid:
                            _spool_write(self.spool_dir, valid)
                        os.remove(claimed)
                        return
                    self._claim = {"path": claimed, "lines": valid, "next": 0}
                ok = True
                for ln in valid:
                    try:
                        status = self._post(ln)
                    except Exception:
                        status = None
                    with self._cv:
                        if self._claim is None:               # this process is exiting: close() took the rest
                            return
                        if status is not None and 200 <= status < 300:
                            self.stats["replayed"] += _span_count(ln)
                        elif status is not None and 400 <= status < 500 and status not in (401, 403, 408, 429):
                            self.stats["spool_rejected"] += _span_count(ln)     # the backend refuses it: never resend
                        else:
                            ok = False
                            break
                        self._claim["next"] += 1
                with self._cv:
                    if self._claim is None:
                        return
                    claim, self._claim = self._claim, None
                rest = claim["lines"][claim["next"]:]
                if rest:
                    _spool_write(self.spool_dir, rest)
                os.remove(claimed)
                if not ok:
                    self._note("spool replay paused: backend unavailable; the rest stays in the spool")
                    return
        except Exception as exc:
            self._note(f"spool replay error: {type(exc).__name__}")
        finally:
            self._replaying = False

    def _http_post(self, body: bytes) -> int:
        u = urllib.parse.urlsplit(self.base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("bad base url")
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(u.hostname, u.port, timeout=self.timeout_s)
        try:
            conn.request("POST", (u.path or "") + "/api/public/otel/v1/traces", body, {
                "Content-Type": "application/json", "Authorization": self._auth,
                "x-langfuse-ingestion-version": "4"})
            resp = conn.getresponse()
            resp.read(1024)
            return resp.status
        finally:
            conn.close()

    # -- warnings: remembered in memory by whoever notices them; written to disk only by one writer thread
    def _note(self, msg: str) -> None:
        """O(1) and I/O-free, so it is safe on the producer path and under the queue lock."""
        try:
            if not self.warnings or self.warnings[-1] != msg:
                self.warnings.append(msg)
                self._unsaved.append(msg)
            self._unsaved_ev.set()
        except Exception:
            pass

    def _warn_run(self) -> None:
        """The only place that touches the warning log. At most one batch of lines per second, a 16 KB file,
        a 20-line backlog; a slow or failing disk stalls only this thread."""
        while True:
            self._unsaved_ev.wait()
            time.sleep(1.0)                                   # coalesce bursts and rate-limit the disk
            self._unsaved_ev.clear()
            lines = []
            while self._unsaved:
                try:
                    lines.append(self._unsaved.popleft())
                except IndexError:
                    break
            drops = self.stats["dropped_queue_full"]
            if drops > self._saved_drops:
                lines.append(f"{drops - self._saved_drops} spans dropped on a full queue (total {drops})")
                self._saved_drops = drops
            self._persist(lines)

    def _persist(self, lines: list[str]) -> None:
        try:
            if not (self.warn_path and lines):
                return
            try:
                size = os.path.getsize(self.warn_path)
            except OSError:
                size = 0
            if size < 16_384:
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
                with open(self.warn_path, "a", encoding="utf-8") as f:
                    f.write("".join(f"{stamp} {m}{chr(10)}" for m in lines))
        except Exception:
            pass


def _span_count(body: bytes) -> int:
    try:
        return sum(len(ss["spans"]) for rs in json.loads(body)["resourceSpans"] for ss in rs["scopeSpans"])
    except Exception:
        return 0


def _valid_line(line: bytes) -> bool:
    """A spool line is replayed only if it is a bounded OTLP body this exporter could have written."""
    if len(line) > SPOOL_EXIT_MAX:
        return False
    try:
        doc = json.loads(line)
        return isinstance(doc, dict) and isinstance(doc.get("resourceSpans"), list) and _span_count(line) > 0
    except Exception:
        return False


def _spool_size(d: str) -> int:
    try:
        return sum(e.stat().st_size for e in os.scandir(d) if e.is_file())
    except FileNotFoundError:
        return 0


def _spool_write(d: str, lines: list[bytes]) -> None:
    """One new file per call: unique name, owner-only permissions where the OS supports them, written to a
    temporary name and renamed into place, so a reader never sees a half-written file. Raises on failure."""
    os.makedirs(d, mode=0o700, exist_ok=True)
    name = f"{time.time_ns():020d}-{os.getpid()}-{secrets.token_hex(4)}"
    tmp, final = os.path.join(d, name + ".tmp"), os.path.join(d, name + ".jsonl")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"\n".join(lines) + b"\n")
        os.replace(tmp, final)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _spool_candidates(d: str) -> list[str]:
    """Spool files to replay, oldest first: unclaimed ones, plus claims and temp files left by a dead process."""
    try:
        entries = list(os.scandir(d))
    except OSError:
        return []
    now, out = time.time(), []
    for e in entries:
        try:
            stale = now - e.stat().st_mtime > SPOOL_STALE_CLAIM_S
        except OSError:
            continue
        if e.name.endswith(".jsonl") or (".jsonl.claimed-" in e.name and stale):
            out.append(e.path)
        elif e.name.endswith(".tmp") and stale:
            try:
                os.remove(e.path)                             # an exit that died mid-write: nothing recoverable
            except OSError:
                pass
    return sorted(out, key=os.path.basename)
