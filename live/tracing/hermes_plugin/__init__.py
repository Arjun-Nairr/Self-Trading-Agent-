"""hermes-trace: observational tracing of one live Hermes run (model requests + tool calls).

Installed into the LIVE profile's own plugin directory (never the default or backtest profiles) together with a
copy of `../otlp.py`. Standard library only; nothing is imported from Langfuse. It records what the Hermes hooks
actually report -- never anything reconstructed from final prose -- and exports it, redacted, on a bounded
background thread. Every handler swallows its own exceptions, so it cannot change what Hermes (or the trading
controller waiting on it) does. Do not enable it together with the stock `observability/langfuse` plugin.

Configuration (all via the live profile's `.env`, never visible to the model: the profile has no terminal/file
tools and MCP tools run in other containers):
  HERMES_TRACE_PUBLIC_KEY / HERMES_TRACE_SECRET_KEY   pk-lf-... / sk-lf-...   (absent => no network export)
  HERMES_TRACE_BASE_URL                               default https://cloud.langfuse.com
  HERMES_TRACE_DEBUG_FILE                             optional local JSONL of the redacted spans (bounded)
Nothing here ever waits for the exporter: the final spans are queued at turn completion and the process exits
without draining them (a daemon thread keeps sending for as long as the process lingers). A stalled or dead
service therefore costs the invocation nothing; unsent telemetry is dropped, never waited for.
Passed per run by the controller (ids only, not secret):
  HERMES_TRACE_ID / HERMES_TRACE_PARENT / HERMES_TRACE_SESSION
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any

from . import otlp

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUN: "_Run | None" = None
_DEBUG_MAX = 262_144
_WRAPPERS = ("tool_call", "tool_describe", "tool_search")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _debug_write(obj: dict) -> None:
    path = _env("HERMES_TRACE_DEBUG_FILE")
    if not path:
        return
    try:
        if os.path.exists(path) and os.path.getsize(path) > _DEBUG_MAX:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str) + "\n")
    except Exception:
        pass


class _Run:
    """Everything one Hermes process records. One trace per run, nested under the controller span if given."""

    def __init__(self) -> None:
        tid, parent = _env("HERMES_TRACE_ID"), _env("HERMES_TRACE_PARENT")
        self.linked = otlp.valid_id(tid, 32)
        self.trace_id = tid if self.linked else otlp.new_trace_id()
        self.parent_id = parent if (self.linked and otlp.valid_id(parent, 16)) else None
        self.session = _env("HERMES_TRACE_SESSION") or None
        self.root_id = otlp.new_span_id()
        self.start_ns = time.time_ns()
        self.gens: dict[str, dict] = {}          # api_request_id -> open generation
        self.gen_ids: dict[str, str] = {}        # api_request_id -> span id (kept after the generation ends)
        self.tools: dict[str, dict] = {}         # tool_call_id -> open tool span
        self.fifo: dict[str, list] = {}          # fallback when no tool_call_id
        self.attempts: dict[str, int] = {}
        self.counts = {"llm_calls": 0, "llm_retries": 0, "tool_calls": 0, "tool_errors": 0, "tool_lookups": 0}
        self.ctx: list = []                      # latest request, per message: (digest|None, where recorded, clipped)
        self.ctx_lock = threading.Lock()
        self.sys_seen: list[dict] = []           # instructions recorded so far, newest first (<= SYS_SEEN_MAX)
        self.final: Any = None
        self.finished = False
        self.exporter = None
        pk, sk = _env("HERMES_TRACE_PUBLIC_KEY"), _env("HERMES_TRACE_SECRET_KEY")
        if pk.startswith("pk-lf-") and sk.startswith("sk-lf-"):
            self.exporter = otlp.Exporter(
                _env("HERMES_TRACE_BASE_URL") or "https://cloud.langfuse.com", pk, sk, service="hermes-live-gateway",
                timeout_s=2.0, queue_max=512,
                spool_dir=os.path.join(_env("HERMES_HOME"), "trace_spool") if _env("HERMES_HOME") else None)

    @property
    def active(self) -> bool:
        return self.exporter is not None or bool(_env("HERMES_TRACE_DEBUG_FILE"))

    def emit(self, rec: dict) -> None:
        if self.exporter is not None:
            self.exporter.submit(rec)
        _debug_write({"span": {k: rec[k] for k in ("trace_id", "span_id", "parent_id", "name", "start_ns",
                                                   "end_ns", "error")}, "attrs": rec["attrs"]})


def _run() -> "_Run | None":
    global _RUN
    with _LOCK:
        if _RUN is None:
            _RUN = _Run()
            atexit.register(_finish)
        return _RUN if _RUN.active else None


def _safe(fn):
    def wrapper(*a: Any, **kw: Any) -> None:
        try:
            fn(*a, **kw)
        except Exception as exc:                  # tracing must never affect Hermes
            logger.debug("hermes-trace %s failed: %s", fn.__name__, type(exc).__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def _ns(seconds: Any, fallback: int) -> int:
    return int(seconds * 1e9) if isinstance(seconds, (int, float)) and seconds > 1e9 else fallback


def _usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    m = {"input": "input_tokens", "output": "output_tokens", "total": "total_tokens",
         "cache_read_input_tokens": "cache_read_tokens", "cache_creation_input_tokens": "cache_write_tokens",
         "reasoning_tokens": "reasoning_tokens"}
    return {k: int(raw[v]) for k, v in m.items() if isinstance(raw.get(v), (int, float)) and raw.get(v)}


MSG_LIMIT = 4000        # chars kept per string inside one exported request message
SYSTEM_LIMIT = 20000    # chars kept of the request's instructions (the policy text)
REQ_BUDGET = 18_000     # bytes of messages exported per request; the rest is listed as omitted, with a reason
MAX_MSGS = 60           # messages exported per request
MAX_TOOL_NAMES = 30
_KNOWN_BODY = {"input", "messages", "instructions", "system", "tools"}
_CLIP_MARK = "...[truncated "
_UPSTREAM_CLIP = re.compile(r"\.\.\. ?\[truncated \d+ chars\]$")   # Hermes' own cut at the end of a string
FP_ITEM_MAX = 1_000_000     # chars compared per request item; a larger item is never asserted unchanged
FP_REQ_MAX = 8_000_000      # chars compared per request in total (bounds the work for very long histories)
SYS_KEEP = 100_000          # chars of each recorded instructions text kept to recognise later clipped copies
SYS_SEEN_MAX = 8            # distinct instructions texts remembered per run


def _digest(obj: Any, budget: int) -> tuple[str | None, int]:
    """Exact digest of an item's observed content (a type-tagged walk) and the characters it cost. None when the
    item holds more than `budget` characters or a value with no stable text form: equality is then NOT
    established, and the item is never cited as unchanged."""
    h = hashlib.blake2b(digest_size=16)
    stack, used = [obj], 0
    while stack:
        o = stack.pop()
        if isinstance(o, str):
            tag, text = "s", o
        elif o is None or isinstance(o, (bool, int, float)):
            tag, text = "n", repr(o)
        elif isinstance(o, dict):
            tag, text = "d", str(len(o))
            if used + 2 * len(o) > budget:
                return None, used
            for k, v in reversed(list(o.items())):
                stack.extend((v, k))
        elif isinstance(o, (list, tuple)):
            tag, text = "l", str(len(o))
            if used + len(o) > budget:
                return None, used
            stack.extend(reversed(o))
        else:
            return None, used
        used += len(text) + 1
        if used > budget:
            return None, used
        h.update(f"{tag}{len(text)}:".encode())
        h.update(text.encode("utf-8", "surrogatepass"))
    return h.hexdigest(), used


def _role(m: Any) -> str:
    return str((m.get("role") or m.get("type") or "?") if isinstance(m, dict) else type(m).__name__)[:40]


def _runs(pairs: list[tuple[int, str]], items: list) -> list[dict]:
    """Consecutive indexes that share a reason become one `omitted` entry (explicit, never silent)."""
    out: list[dict] = []
    for i, why in sorted(pairs):
        if out and out[-1]["indexes"][1] == i - 1 and out[-1]["reason"] == why:
            out[-1]["indexes"][1] = i
            out[-1]["roles"] = sorted(set(out[-1]["roles"]) | {_role(items[i])})[:6]
        else:
            out.append({"indexes": [i, i], "roles": [_role(items[i])], "reason": why})
    return out


def _request_input(run: "_Run", name: str, sid: str, kw: dict) -> dict:
    """What the model was sent, bounded and redacted, with every gap named.

    Requests in one run normally repeat the whole earlier history, so a message already exported by an earlier
    request of this trace is cited (`inherited`: index range -> the span that recorded it) instead of being
    exported again; only new or changed messages are exported. Messages are kept whole (role/type, ids, tool
    calls, call ids) so call/result associations survive. Anything not exported appears in `omitted` with a
    reason, any clipped text in `clipped`, and `complete` says whether the request is fully inspectable."""
    raw = kw.get("request_messages")
    items = raw if isinstance(raw, list) else []
    n = len(items)
    fps: list[str | None] = []
    left = FP_REQ_MAX
    for m in items:
        d, used = _digest(m, min(FP_ITEM_MAX, left)) if left > 0 else (None, 0)
        left -= used
        fps.append(d)
    with run.ctx_lock:
        prev = run.ctx
        lcp = 0
        while lcp < min(n, len(prev)) and fps[lcp] is not None and prev[lcp][0] == fps[lcp]:
            lcp += 1
        cand = list(range(lcp, n))
        chosen: dict[int, dict] = {}
        clipped: set[int] = set()
        gaps: list[tuple[int, str]] = []
        used = 0
        for i in cand[:2] + cand[2:][::-1]:                       # the opening task/policy first, then the newest
            if used >= REQ_BUDGET or len(chosen) >= MAX_MSGS:
                gaps.append((i, f"request export budget reached ({REQ_BUDGET} bytes / {MAX_MSGS} messages)"))
                continue
            m = items[i]
            rec = {"index": i, **({k: v for k, v in otlp.sanitize(m, MSG_LIMIT).items() if k != "index"}
                                  if isinstance(m, dict) else {"value": otlp.sanitize(m, MSG_LIMIT)})}
            size = len(json.dumps(rec, ensure_ascii=False, default=str))
            if used + size > REQ_BUDGET:
                gaps.append((i, f"message too large for the remaining request export budget ({size} bytes)"))
                continue
            used += size
            chosen[i] = rec
            if _CLIP_MARK in json.dumps(rec, ensure_ascii=False, default=str):
                clipped.add(i)
        ctx = list(prev[:lcp]) + [(fps[i], (name, sid) if i in chosen else None, i in clipped) for i in cand]
        run.ctx = ctx
        diverged = bool(prev) and lcp < len(prev)
        unknown = diverged and lcp < n and (fps[lcp] is None or prev[lcp][0] is None)
        history_changed = lcp if diverged and not unknown else None
    inherited: list[dict] = []
    earlier_gaps: list[tuple[int, str]] = []
    for i in range(lcp):
        if ctx[i][1] is None:
            earlier_gaps.append((i, "not exported by the earlier request in this run that first carried it"))
        elif inherited and inherited[-1]["span_id"] == ctx[i][1][1] and inherited[-1]["indexes"][1] == i - 1:
            inherited[-1]["indexes"][1] = i
        else:
            inherited.append({"indexes": [i, i], "recorded_in": ctx[i][1][0], "span_id": ctx[i][1][1]})
    omitted = _runs(gaps + earlier_gaps, items)
    clipped_all = [i for i, c in enumerate(ctx) if c[2]][:100]
    inp: dict[str, Any] = {"message_count": n, "messages": [chosen[i] for i in sorted(chosen)],
                           "inherited": inherited, "omitted": omitted, "clipped": clipped_all}
    if history_changed is not None:
        inp["history_changed_at_index"] = history_changed
    unverified = [i for i, d in enumerate(fps) if d is None][:100]
    if unverified:
        inp["equality_unverified"] = unverified
        inp["equality_note"] = (f"items too large to compare (> {FP_ITEM_MAX} chars, or the request's "
                                f"{FP_REQ_MAX}-char comparison budget) are never cited as unchanged")
        if unknown:
            inp["history_unverified_from_index"] = lcp
    if not isinstance(raw, list):
        inp["note"] = "the hook payload carried no request_messages list"
    req = kw.get("request")
    body = req.get("body") if isinstance(req, dict) and isinstance(req.get("body"), dict) else {}
    sysv = body.get("instructions", body.get("system"))
    system_clipped = False
    with run.ctx_lock:
        seen = run.sys_seen
        if sysv is None and isinstance(req, dict) and req.get("_truncated"):
            # Hermes replaced the whole payload by a preview (it was over HERMES_PLUGIN_PAYLOAD_MAX_CHARS)
            inp["system"] = ({"same_as": seen[0]["name"], "span_id": seen[0]["sid"], "assumed": True,
                              "note": "Hermes truncated the request payload; instructions not re-observed"} if seen
                             else {"unavailable": "Hermes truncated the request payload before the hook"})
            system_clipped = True
        elif sysv is not None:
            # Cite earlier instructions only when the observed text is identical (exact digest). Hermes clips long
            # strings differently per request, so a shorter clipped copy is labelled, never called identical.
            dig, _ = _digest(sysv, FP_ITEM_MAX)
            obs = sysv if isinstance(sysv, str) else None
            hit = next((r for r in seen if dig is not None and r["digest"] == dig), None)
            cut = _UPSTREAM_CLIP.search(obs) if obs is not None and hit is None else None
            base = obs[:cut.start()] if cut else None
            pre = next((r for r in seen if base is not None and len(r["text"]) > len(base)
                        and r["text"].startswith(base)), None)
            if hit:
                inp["system"] = {"same_as": hit["name"], "span_id": hit["sid"]}
                if hit["clip"]:
                    inp["system"]["equality"] = ("identical observed text, but Hermes clipped it: content past "
                                                 "the cut is unverified")
                system_clipped = hit["clip"]
            elif pre:
                inp["system"] = {"prefix_of": pre["name"], "span_id": pre["sid"], "observed_chars": len(base),
                                 "equality": (f"unverified: Hermes clipped this copy to {len(base)} chars, which match "
                                              f"the start of what {pre['name']} recorded; the rest was not observed")}
                system_clipped = True
            else:
                text = otlp.sanitize(sysv, SYSTEM_LIMIT)
                clip = _CLIP_MARK in json.dumps(text, ensure_ascii=False, default=str)
                inp["system"] = {"content": text, "clipped": clip}
                if clip:
                    inp["system"]["note"] = ("text cut: Hermes itself clips every string in the request hook payload "
                                             "(8,000 chars; 1,000 for very large requests) before any plugin sees it")
                if dig is None:
                    inp["system"]["equality"] = "unverified: too large to compare, so recorded again"
                run.sys_seen = [{"digest": dig, "name": name, "sid": sid, "clip": clip,
                                 "text": (obs or "")[:SYS_KEEP]}] + seen[:SYS_SEEN_MAX - 1]
                system_clipped = clip or dig is None
    tools = body.get("tools")
    if isinstance(tools, list) or kw.get("tool_count"):
        names = []
        for t in (tools if isinstance(tools, list) else [])[:MAX_TOOL_NAMES]:
            f = t.get("function") if isinstance(t, dict) else None
            nm = (t.get("name") or (f.get("name") if isinstance(f, dict) else None)) if isinstance(t, dict) else None
            names.append(otlp.truncate(otlp.redact_text(nm if isinstance(nm, str) else repr(t)[:80]), 80))
        inp["tools"] = {"count": len(tools) if isinstance(tools, list) else kw.get("tool_count"), "names": names,
                        "not_exported": "tool schemas (descriptions and parameters) are not exported; names only"}
    rest = sorted(str(k) for k in body if k not in _KNOWN_BODY)[:20]
    if rest:
        inp["request_fields_not_exported"] = rest
    inp["complete"] = (isinstance(raw, list) and not omitted and not clipped_all and not system_clipped
                       and not unverified)
    return inp


def _tool_calls(msg: Any) -> list:
    """Tool calls the model requested, from either an object or a dict; opaque entries are kept as text."""
    calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
    out = []
    for c in calls or []:
        if isinstance(c, str):
            out.append({"raw": c})
            continue
        f = c.get("function") if isinstance(c, dict) else getattr(c, "function", None)
        src = f if f is not None else c
        name = src.get("name") if isinstance(src, dict) else getattr(src, "name", None)
        arg = src.get("arguments") if isinstance(src, dict) else getattr(src, "arguments", None)
        if name is None and arg is None:
            out.append({"raw": repr(c)})
        else:
            out.append({"name": name, "arguments": arg})
    return out


def _unwrap(tool_name: str, args: Any) -> tuple[str, Any, str | None]:
    """The underlying MCP tool behind Hermes's generic wrappers (tool_call / tool_describe / tool_search)."""
    if tool_name in _WRAPPERS and isinstance(args, dict) and isinstance(args.get("name"), str):
        return args["name"], (args.get("arguments") if tool_name == "tool_call" else {}), tool_name
    return tool_name, args, None


def _end_gen(run: _Run, g: dict, *, level: str | None = None, message: str | None = None, output: Any = None,
             usage: dict | None = None, end_ns: int | None = None, extra: dict | None = None) -> None:
    md = dict(g["metadata"], **(extra or {}))
    attrs = otlp.observation_attrs("generation", input=g["input"], input_limit=SYSTEM_LIMIT, output=output,
                                   metadata=md, level=level,
                                   status_message=message, usage=usage, model=g["model"])
    run.emit(otlp.span_record(run.trace_id, g["id"], run.root_id, g["name"], g["start_ns"], end_ns or time.time_ns(),
                              attrs, error=level == "ERROR", message=message))


@_safe
def on_pre_api_request(**kw: Any) -> None:
    run = _run()
    if run is None or run.finished:
        return
    rid = str(kw.get("api_request_id") or f"{kw.get('turn_id')}:api:{kw.get('api_call_count')}")
    n, retry = kw.get("api_call_count"), int(kw.get("retry_count") or 0)
    with _LOCK:
        prev = run.gens.pop(rid, None)
        attempt = run.attempts[rid] = run.attempts.get(rid, 0) + 1
        run.counts["llm_calls"] += 1 if prev is None else 0
        if attempt > 1:
            run.counts["llm_retries"] += 1
        sid = otlp.new_span_id()
    name = f"llm_call_{n}" + (f" (attempt {attempt})" if attempt > 1 else "")
    try:
        request = _request_input(run, name, sid, kw)
    except Exception as exc:                       # say so in the trace rather than silently exporting nothing
        request = {"complete": False, "error": f"request context unavailable ({type(exc).__name__})"}
    g = {"id": sid, "start_ns": _ns(kw.get("started_at"), time.time_ns()), "name": name,
         "model": kw.get("model"), "input": request,
         "metadata": {"provider": kw.get("provider"), "api_mode": kw.get("api_mode"), "api_call_count": n,
                      "attempt": attempt, "retry_count": retry, "is_retry": attempt > 1,
                      "message_count": kw.get("message_count"), "tool_count": kw.get("tool_count")}}
    with _LOCK:
        run.gens[rid] = g
        run.gen_ids[rid] = sid
    if prev is not None:                           # the earlier attempt never produced a response
        _end_gen(run, prev, level="WARNING", message="attempt returned no response; the request was retried",
                 extra={"outcome": "retried"})


@_safe
def on_post_api_request(**kw: Any) -> None:
    run = _run()
    if run is None or run.finished:
        return
    rid = str(kw.get("api_request_id") or f"{kw.get('turn_id')}:api:{kw.get('api_call_count')}")
    with _LOCK:
        g = run.gens.pop(rid, None)
    if g is None:
        return
    resp = kw.get("response") if isinstance(kw.get("response"), dict) else {}
    am = resp.get("assistant_message")
    am = am if isinstance(am, dict) else kw.get("assistant_message")
    content = am.get("content") if isinstance(am, dict) else getattr(am, "content", None)
    reasoning = (am.get("reasoning_content") or am.get("reasoning")) if isinstance(am, dict) else (
        getattr(am, "reasoning_content", None) or getattr(am, "reasoning", None))
    usage = _usage(kw.get("usage"))
    extra = {"finish_reason": kw.get("finish_reason"), "api_duration_s": kw.get("api_duration"),
             "usage": "provider-reported" if usage else "unavailable"}
    if isinstance(reasoning, str) and reasoning:
        extra["returned_reasoning"] = reasoning
    calls = _tool_calls(kw.get("assistant_message")) or _tool_calls(am)
    out = {"content": content, "tool_calls": calls or None, "finish_reason": kw.get("finish_reason")}
    if content and not calls:
        run.final = content
    g["model"] = kw.get("response_model") or g["model"]
    _end_gen(run, g, output=out, usage=usage or None, extra=extra,
             end_ns=_ns(kw.get("ended_at"), time.time_ns()))


@_safe
def on_pre_tool_call(**kw: Any) -> None:
    run = _run()
    if run is None or run.finished:
        return
    name, inner, wrapper = _unwrap(str(kw.get("tool_name") or "tool"), kw.get("args"))
    lookup = wrapper in ("tool_describe", "tool_search")           # schema lookups, not invocations
    name = f"{wrapper}:{name}" if lookup else name
    tid = str(kw.get("tool_call_id") or "")
    rid = str(kw.get("api_request_id") or "")
    t = {"id": otlp.new_span_id(), "start_ns": time.time_ns(), "name": name, "input": inner,
         "parent": run.gen_ids.get(rid) or run.root_id, "wrapper": wrapper, "call_id": tid, "api_request_id": rid}
    with _LOCK:
        run.counts["tool_lookups" if lookup else "tool_calls"] += 1
        if tid:
            run.tools[tid] = t
        else:
            run.fifo.setdefault(str(kw.get("tool_name")), []).append(t)


@_safe
def on_post_tool_call(**kw: Any) -> None:
    run = _run()
    if run is None or run.finished:
        return
    tid = str(kw.get("tool_call_id") or "")
    with _LOCK:
        t = run.tools.pop(tid, None) if tid else None
        if t is None:
            q = run.fifo.get(str(kw.get("tool_name")))
            t = q.pop(0) if q else None
    now = time.time_ns()
    dur = kw.get("duration_ms")
    if t is None:                                  # post without a pre: reconstruct only the timing we were given
        name, inner, wrapper = _unwrap(str(kw.get("tool_name") or "tool"), kw.get("args"))
        name = f"{wrapper}:{name}" if wrapper in ("tool_describe", "tool_search") else name
        start = now - int(dur * 1e6) if isinstance(dur, (int, float)) else now
        t = {"id": otlp.new_span_id(), "start_ns": start, "name": name, "input": inner, "parent": run.root_id,
             "wrapper": wrapper, "call_id": tid, "api_request_id": str(kw.get("api_request_id") or "")}
    result = kw.get("result")
    if isinstance(result, str) and result[:1] in ("{", "[") and len(result) <= 200_000:   # never parse giant payloads
        try:
            result = json.loads(result)
        except ValueError:
            pass
    status = kw.get("status")
    failed = status not in (None, "ok", "success", "completed")
    if failed:
        run.counts["tool_errors"] += 1
    md = {"tool_call_id": t["call_id"], "wrapper_tool": t["wrapper"], "duration_ms": dur, "status": status,
          "error_type": kw.get("error_type")}
    attrs = otlp.observation_attrs("tool", input=t["input"], output=result, metadata=md,
                                   level="ERROR" if failed else None,
                                   status_message=(f"{kw.get('error_type')}: {kw.get('error_message')}"
                                                   if failed else None))
    run.emit(otlp.span_record(run.trace_id, t["id"], t["parent"], t["name"], t["start_ns"], now, attrs,
                              error=failed, message=str(kw.get("error_message") or "") if failed else None))


@_safe
def on_post_llm_call(**kw: Any) -> None:
    """Turn finished: close the run and queue the remaining spans. Returns immediately; nothing is awaited."""
    _finish()


def _diagnostic_drain_s() -> float:
    """Seconds to wait for export at turn end: 0 unless the controller passed HERMES_TRACE_DIAGNOSTIC_DRAIN_S for an
    orders-disabled verification call (`run_live.py dry-run --trace-drain`). Capped at 10 s."""
    try:
        return max(0.0, min(float(_env("HERMES_TRACE_DIAGNOSTIC_DRAIN_S") or 0), 10.0))
    except ValueError:
        return 0.0


def _finish() -> None:
    try:
        run = _RUN
        if run is None or run.finished or not run.active:
            return
        _debug_write({"event": "finish_started", "time": time.time()})
        with _LOCK:
            if run.finished:
                return
            run.finished = True
            open_gens, open_tools = list(run.gens.values()), list(run.tools.values()) + [
                t for q in run.fifo.values() for t in q]
            run.gens.clear(), run.tools.clear(), run.fifo.clear()
        for g in open_gens:
            _end_gen(run, g, level="WARNING", message="no response recorded before the run ended",
                     extra={"outcome": "unfinished"})
        for t in open_tools:
            attrs = otlp.observation_attrs("tool", input=t["input"], level="WARNING",
                                           status_message="no result recorded before the run ended",
                                           metadata={"wrapper_tool": t["wrapper"], "outcome": "unfinished"})
            run.emit(otlp.span_record(run.trace_id, t["id"], t["parent"], t["name"], t["start_ns"], time.time_ns(),
                                      attrs))
        attrs = otlp.observation_attrs(
            "agent", output={"final": run.final, "counts": run.counts}, metadata=dict(run.counts, linked=run.linked),
            trace_name=None if run.linked else "hermes_run", session=run.session)
        run.emit(otlp.span_record(run.trace_id, run.root_id, run.parent_id, "hermes_run", run.start_ns,
                                  time.time_ns(), attrs))
        if run.exporter is not None:
            drain = _diagnostic_drain_s()
            if drain:                              # DIAGNOSTIC: only when the controller's `dry-run --trace-drain`
                run.exporter.flush(drain)          # passed it for this one orders-disabled call; never otherwise
            run.exporter.close()                   # stops intake, spools what is unacknowledged; no network wait
        _debug_write({"event": "finish_returned", "time": time.time(),
                      "exporter": dict(run.exporter.stats) if run.exporter is not None else None})
    except Exception:
        pass


def register(ctx) -> None:
    _debug_write({"event": "plugin_registered", "pid": os.getpid(), "time": time.time()})
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
