"""Ticket 1 tracing: redaction, bounded export, the Hermes plugin's hooks, cross-process correlation, and
trading parity with tracing off / healthy / misconfigured / throwing / hung / queue-full.

OFFLINE TRANSPORT EVIDENCE ONLY: spans go to a local capture server speaking the OTLP/HTTP-JSON shape that
Langfuse documents. Nothing here proves hosted Langfuse accepts them, or that Hermes calls these hooks in the
controller's one-shot invocation -- those are checked separately (docs/live_tracing_runbook.md)."""
import http.server
import importlib.util
import json
import re
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent  # noqa: E402
import run_live  # noqa: E402
from run_live import run_cycle  # noqa: E402
from tracing import otlp  # noqa: E402
from tracing.controller import NullTracer, Tracer, load_tracer  # noqa: E402
import test_live as tl  # noqa: E402

PK, SK = "pk-lf-00000000-aaaa-bbbb-cccc-000000000000", "sk-lf-11111111-aaaa-bbbb-cccc-111111111111"


# -- a local OTLP/JSON capture server ---------------------------------------------------------------------------

class Collector:
    def __init__(self, mode="ok", delay=0.0):
        self.bodies, self.headers, self.mode, self.delay = [], [], mode, delay
        self.release = threading.Event()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                if outer.mode == "hang":
                    outer.release.wait(30)
                if outer.delay:
                    time.sleep(outer.delay)
                outer.bodies.append(body)
                outer.headers.append(dict(self.headers))
                self.send_response(500 if outer.mode == "500" else 200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def spans(self):
        out = []
        for b in self.bodies:
            for rs in json.loads(b)["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    for s in ss["spans"]:
                        s["a"] = {a["key"]: next(iter(a["value"].values())) for a in s["attributes"]}
                        out.append(s)
        return out


def dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"          # nothing listens: connection refused


def exporter(url, **kw):
    return otlp.Exporter(url, PK, SK, **kw)


# -- redaction ------------------------------------------------------------------------------------------------------

DUMMY = ["sk-abcdefghijklmnopqrstuvwx", "hunter2hunter2", "s3cr3tvalue123", "AKIAABCDEFGHIJKLMNOP",
         "abcdef0123456789token", "PKABCDEFGHIJKLMNOPQRST", "PA3ABCDEFGH12", "dwish", "someone@example.com"]


def test_redaction_removes_secrets_from_nested_payloads_and_exceptions():
    payload = {"api_key": DUMMY[0], "headers": {"Authorization": "Bearer " + DUMMY[4], "x-api-key": DUMMY[1]},
               "nested": [{"password": DUMMY[2]}, {"note": "use token=" + DUMMY[4] + " now"}],
               "url": "postgresql://user:" + DUMMY[1] + "@db.example.com/neon?sslmode=require",
               "aws": DUMMY[3], "broker": DUMMY[5], "acct": "account " + DUMMY[6],
               "path": r"C:\Users\dwish\Downloads\hermes-trading-agent\x.py and /home/dwish/.hermes/y",
               "mail": DUMMY[8], "ok_tokens": {"input_tokens": 12, "total_tokens": 30, "max_tokens": 99},
               "account_number": "123456789", "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"}
    text = json.dumps(otlp.sanitize(payload))
    for secret in DUMMY[:7] + ["123456789", "eyJhbGciOiJIUzI1NiJ9"]:
        assert secret not in text, secret
    assert "dwish" not in text and "someone@example.com" not in text
    assert otlp.sanitize(payload)["ok_tokens"] == {"input_tokens": 12, "total_tokens": 30, "max_tokens": 99}
    try:
        raise RuntimeError("connect failed for postgresql://u:" + DUMMY[1] + "@h/db with Bearer " + DUMMY[4])
    except RuntimeError as exc:
        msg = otlp.describe_exception(exc)
    assert DUMMY[1] not in msg and DUMMY[4] not in msg and msg.startswith("RuntimeError:")


def test_truncation_and_payload_limits_are_explicitly_marked():
    s = otlp.sanitize("x" * 20_000)
    assert len(s) < 8_100 and s.endswith("...[truncated 12000 chars]")
    assert otlp.sanitize(list(range(500)))[-1] == "[... 300 more items truncated]"
    huge = otlp.encode_attr({f"k{i}": "y" * 7000 for i in range(60)})
    assert len(huge.encode()) <= otlp.MAX_ATTR_BYTES + 200 and '"truncated": true' in huge
    deep = cur = {}
    for _ in range(30):
        cur["n"] = {}
        cur = cur["n"]
    assert "max depth" in json.dumps(otlp.sanitize(deep))


# -- exporter ----------------------------------------------------------------------------------------------------------

def rec(name="s", parent=None, trace=None):
    return otlp.span_record(trace or otlp.new_trace_id(), otlp.new_span_id(), parent, name, time.time_ns(),
                            time.time_ns() + 5, otlp.observation_attrs("span", input={"a": 1}))


def test_exporter_sends_langfuse_shaped_otlp_json_with_basic_auth(collector):
    e = exporter(collector.url)
    t = otlp.new_trace_id()
    root = rec("root", None, t)
    child = rec("child", root["span_id"], t)
    e.submit(root), e.submit(child)
    assert e.flush(3)
    spans = {s["name"]: s for s in collector.spans()}
    assert spans["child"]["parentSpanId"] == root["span_id"] and spans["child"]["traceId"] == t
    assert len(spans["root"]["spanId"]) == 16 and "parentSpanId" not in spans["root"]
    assert spans["root"]["a"]["langfuse.observation.type"] == "span"
    h = {k.lower(): v for k, v in collector.headers[0].items()}
    assert h["x-langfuse-ingestion-version"] == "4" and h["authorization"].startswith("Basic ")
    assert SK not in json.dumps(collector.bodies[0].decode())


def test_unavailable_service_trips_the_circuit_and_never_blocks():
    e = exporter(dead_url(), timeout_s=0.5)
    t0 = time.monotonic()
    for _ in range(200):
        e.submit(rec())
    assert time.monotonic() - t0 < 0.5, "submit never touches the network"
    e.flush(3)
    assert e.stats["failed_batches"] >= 1 and e.stats["sent"] == 0
    t1 = time.monotonic()
    for _ in range(50):
        e.submit(rec())
    assert e.flush(1) and time.monotonic() - t1 < 1, "circuit open: later spans are dropped instantly"
    assert e.stats["dropped_circuit_open"] > 0 and e.warnings


def test_hung_service_is_abandoned_not_waited_for():
    c = Collector("hang")
    try:
        e = exporter(c.url, timeout_s=5.0)
        for _ in range(5):
            e.submit(rec())
        t0 = time.monotonic()
        e.close()                                              # intake stops; nothing is awaited
        assert time.monotonic() - t0 < 0.1
        assert not e.flush(0.3), "the stalled batch is still in flight, and nobody is waiting for it"
        e.submit(rec())
        assert e.stats["submitted"] == 5, "a closed exporter accepts nothing more"
    finally:
        c.stop()


def test_full_queue_drops_telemetry_and_never_raises():
    c = Collector("hang")
    try:
        e = exporter(c.url, queue_max=3, batch_max=1, timeout_s=5.0)
        t0 = time.monotonic()
        for _ in range(500):
            e.submit(rec())
        assert time.monotonic() - t0 < 1.0
        assert e.stats["dropped_queue_full"] >= 490
    finally:
        c.stop()


def test_throwing_transport_is_contained():
    def boom(body):
        raise OSError("socket exploded " + DUMMY[1])
    e = exporter("http://127.0.0.1:9", post=boom)
    for _ in range(5):
        e.submit(rec())
    e.flush(2)
    assert e.stats["failed_batches"] >= 1 and e.stats["sent"] == 0
    assert all(DUMMY[1] not in w for w in e.warnings)


def test_http_4xx_is_not_retried_and_5xx_is_retried_once():
    calls = []

    def post(status):
        def f(body):
            calls.append(status)
            return status
        return f
    e = exporter("http://x", post=post(401))
    e.submit(rec()), e.flush(2)
    assert calls == [401]
    calls.clear()
    e = exporter("http://x", post=post(503))
    e.submit(rec()), e.flush(2)
    assert calls == [503, 503]


# -- controller tracer ----------------------------------------------------------------------------------------------

def test_load_tracer_is_null_unless_credentials_are_valid_and_makes_no_network_call(tmp_path):
    assert isinstance(load_tracer(tmp_path), NullTracer)
    (tmp_path / "tracing.env").write_text("LANGFUSE_PUBLIC_KEY=placeholder\nLANGFUSE_SECRET_KEY=x\n")
    assert isinstance(load_tracer(tmp_path), NullTracer)
    (tmp_path / "tracing.env").write_text(f"LANGFUSE_PUBLIC_KEY={PK}\nLANGFUSE_SECRET_KEY={SK}\n"
                                          "LANGFUSE_BASE_URL=http://127.0.0.1:9\n")
    tr = load_tracer(tmp_path)
    assert isinstance(tr, Tracer) and tr.exporter._thread is None, "no worker thread until the first span"


def test_tracer_nests_spans_records_errors_and_propagates_trading_exceptions(collector):
    tr = Tracer(exporter(collector.url))
    with pytest.raises(RuntimeError, match="trading failure"):
        with tr.trace("live_cycle"):
            with tr.span("reconcile"):
                pass
            with tr.span("selection"):
                raise RuntimeError("trading failure " + DUMMY[0])
    assert tr.exporter.flush(3)
    spans = {s["name"]: s for s in collector.spans()}
    assert spans["reconcile"]["parentSpanId"] == spans["live_cycle"]["spanId"]
    assert spans["selection"]["status"]["code"] == 2 and DUMMY[0] not in json.dumps(spans["selection"])
    assert spans["live_cycle"]["a"]["langfuse.trace.name"] == "live_cycle"


# -- Hermes plugin -----------------------------------------------------------------------------------------------------

def load_plugin(tmp_path, name="hermes_trace", drain=True):
    d = tmp_path / name
    d.mkdir()
    for f, t in run_live.trace_plugin_files().items():
        (d / f).write_text(t, encoding="utf-8")
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod_name = f"hermes_plugins.{name}"
    spec = importlib.util.spec_from_file_location(mod_name, d / "__init__.py", submodule_search_locations=[str(d)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__, mod.__path__ = mod_name, [str(d)]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    hooks = {}
    mod.register(types.SimpleNamespace(register_hook=lambda n, fn: hooks.__setitem__(n, fn)))
    if drain:                                       # TEST-ONLY wait so assertions can read what was exported
        raw = hooks["post_llm_call"]

        def finish_and_drain(**kw):
            raw(**kw)
            if mod._RUN is not None and mod._RUN.exporter is not None:
                mod._RUN.exporter.flush(5)
        hooks["post_llm_call"] = finish_and_drain
    return mod, hooks


def api_pre(h, rid, n, retry=0, msgs=None):
    h["pre_api_request"](api_request_id=rid, api_call_count=n, retry_count=retry, model="gpt-5.6-terra",
                         provider="openai-codex", api_mode="codex_responses", started_at=time.time(),
                         request_messages=msgs or [{"role": "user", "content": "review the book"}],
                         message_count=1, tool_count=3, turn_id="t1", task_id="k", session_id="s")


def api_post(h, rid, n, content=None, calls=(), usage=True, extra=None):
    am = {"role": "assistant", "content": content,
          "tool_calls": [{"id": c, "type": "function", "function": {"name": "tool_call", "arguments": "{}"}}
                         for c in calls]}
    h["post_api_request"](api_request_id=rid, api_call_count=n, response={"assistant_message": am},
                          usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                                 "cache_read_tokens": 0} if usage else None,
                          finish_reason="tool_calls" if calls else "stop", response_model="gpt-5.6-terra",
                          started_at=time.time() - 1, ended_at=time.time(), api_duration=1.0, model="gpt-5.6-terra",
                          assistant_message=types.SimpleNamespace(content=content, tool_calls=[]),
                          **(extra or {}))


REAL = "mcp__maverick_mcp__technical_get_full_technical_analysis"


def tool_pre(h, cid, ticker, rid):
    """Hermes reports the resolved MCP tool name to the hook (observed in the real gateway)."""
    h["pre_tool_call"](tool_name=REAL, tool_call_id=cid, api_request_id=rid, turn_id="t1", task_id="k",
                       session_id="s", args={"ticker": ticker, "days": 120})


def tool_post(h, cid, ticker, rid, status="ok", err=None, result=None):
    h["post_tool_call"](tool_name=REAL, tool_call_id=cid, api_request_id=rid, turn_id="t1", task_id="k",
                        session_id="s", args={"ticker": ticker},
                        result=result or json.dumps({"ticker": ticker, "rsi": 51.2}), duration_ms=12.5,
                        status=status, error_type="ToolError" if err else None, error_message=err)


def test_plugin_links_to_the_controller_span_and_matches_parallel_calls_by_id(tmp_path, plugin_env, monkeypatch):
    trace_id, parent = otlp.new_trace_id(), otlp.new_span_id()
    monkeypatch.setenv("HERMES_TRACE_ID", trace_id)
    monkeypatch.setenv("HERMES_TRACE_PARENT", parent)
    mod, h = load_plugin(tmp_path)
    api_pre(h, "t1:api:1", 1)
    api_post(h, "t1:api:1", 1, calls=["c1", "c2", "c3"])
    for cid, tk in (("c1", "ALGM"), ("c2", "ATI"), ("c3", "BRKR")):       # three parallel calls
        tool_pre(h, cid, tk, "t1:api:1")
    for cid, tk in (("c3", "BRKR"), ("c1", "ALGM"), ("c2", "ATI")):       # results arrive out of order
        tool_post(h, cid, tk, "t1:api:1")
    api_pre(h, "t1:api:2", 2)
    api_post(h, "t1:api:2", 2, content='{"holdings": []}')
    h["post_llm_call"](turn_id="t1")
    plugin_env.release.set()
    spans = {}
    for s in plugin_env.spans():
        spans.setdefault(s["name"], []).append(s)
    root = spans["hermes_run"][0]
    assert root["traceId"] == trace_id and root["parentSpanId"] == parent, "nested under the controller span"
    gen1 = spans["llm_call_1"][0]
    assert gen1["parentSpanId"] == root["spanId"] and gen1["a"]["langfuse.observation.type"] == "generation"
    tools = spans["mcp__maverick_mcp__technical_get_full_technical_analysis"]
    assert len(tools) == 3 and {t["parentSpanId"] for t in tools} == {gen1["spanId"]}, "parallel calls are siblings"
    for t in tools:
        inp = json.loads(t["a"]["langfuse.observation.input"])
        out = json.loads(t["a"]["langfuse.observation.output"])
        assert inp["ticker"] == out["ticker"], "each result is matched to its own call"
        assert "langfuse.observation.metadata.wrapper_tool" not in t["a"]
        assert t["a"]["langfuse.observation.type"] == "tool"
    assert json.loads(root["a"]["langfuse.observation.output"])["counts"] == {
        "llm_calls": 2, "llm_retries": 0, "tool_calls": 3, "tool_errors": 0, "tool_lookups": 0}
    usage = json.loads(gen1["a"]["langfuse.observation.usage_details"])
    assert usage == {"input": 100, "output": 20, "total": 120}
    assert not any("cost" in k for s in plugin_env.spans() for k in s["a"]), "no estimated cost is ever exported"
    assert gen1["a"]["langfuse.observation.model.name"] == "gpt-5.6-terra"


def test_plugin_unwraps_generic_wrapper_calls_and_does_not_count_lookups_as_invocations(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    api_pre(h, "t1:api:1", 1)
    h["pre_tool_call"](tool_name="tool_describe", tool_call_id="d1", api_request_id="t1:api:1",
                       args={"name": REAL})
    h["post_tool_call"](tool_name="tool_describe", tool_call_id="d1", api_request_id="t1:api:1", args={"name": REAL},
                        result='{"parameters": {}}', duration_ms=3, status="ok")
    h["pre_tool_call"](tool_name="tool_call", tool_call_id="w1", api_request_id="t1:api:1",
                       args={"name": REAL, "arguments": {"ticker": "OLN"}})
    h["post_tool_call"](tool_name="tool_call", tool_call_id="w1", api_request_id="t1:api:1",
                        args={"name": REAL, "arguments": {"ticker": "OLN"}}, result='{"ticker": "OLN"}',
                        duration_ms=4, status="ok")
    h["post_llm_call"]()
    spans = {s["name"]: s for s in plugin_env.spans()}
    wrapped = spans[REAL]
    assert json.loads(wrapped["a"]["langfuse.observation.input"]) == {"ticker": "OLN"}
    assert wrapped["a"]["langfuse.observation.metadata.wrapper_tool"] == "tool_call"
    assert f"tool_describe:{REAL}" in spans
    counts = json.loads(spans["hermes_run"]["a"]["langfuse.observation.output"])["counts"]
    assert counts["tool_calls"] == 1 and counts["tool_lookups"] == 1


def test_plugin_reads_requested_tool_calls_from_the_raw_message_when_the_payload_is_opaque(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    api_pre(h, "t1:api:1", 1)
    fn = types.SimpleNamespace(name=REAL, arguments='{"ticker": "OLN"}')
    raw = types.SimpleNamespace(content="", tool_calls=[types.SimpleNamespace(id="c1", function=fn)])
    h["post_api_request"](api_request_id="t1:api:1", api_call_count=1, model="m", finish_reason="tool_calls",
                          response={"assistant_message": {"content": "", "tool_calls": ["<opaque object repr>"]}},
                          usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}, assistant_message=raw)
    h["post_llm_call"]()
    gen = [s for s in plugin_env.spans() if s["name"] == "llm_call_1"][0]
    out = json.loads(gen["a"]["langfuse.observation.output"])
    assert out["tool_calls"] == [{"name": REAL, "arguments": '{"ticker": "OLN"}'}]


def test_plugin_labels_retries_errors_unknown_usage_and_unfinished_work(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    api_pre(h, "t1:api:1", 1)
    api_pre(h, "t1:api:1", 1, retry=1)                       # the first attempt never returned: a retry
    api_post(h, "t1:api:1", 1, calls=["c1", "c2"], usage=False)
    tool_pre(h, "c1", "WMG", "t1:api:1")
    tool_post(h, "c1", "WMG", "t1:api:1", status="error", err="upstream 502 for Bearer " + DUMMY[4])
    tool_pre(h, "c2", "OLN", "t1:api:1")                      # never answered
    api_pre(h, "t1:api:2", 2)                                 # never answered either
    h["post_llm_call"](turn_id="t1")
    spans = {}
    for s in plugin_env.spans():
        spans.setdefault(s["name"], []).append(s)
    first = spans["llm_call_1"][0]
    second = spans["llm_call_1 (attempt 2)"][0]
    assert first["a"]["langfuse.observation.level"] == "WARNING"
    assert first["a"]["langfuse.observation.metadata.outcome"] == "retried"
    assert int(second["a"]["langfuse.observation.metadata.attempt"]) == 2
    assert second["a"]["langfuse.observation.metadata.usage"] == "unavailable"
    assert "langfuse.observation.usage_details" not in second["a"], "usage is never invented"
    tools = spans["mcp__maverick_mcp__technical_get_full_technical_analysis"]
    errored = [t for t in tools if t["status"]["code"] == 2][0]
    assert errored["a"]["langfuse.observation.level"] == "ERROR" and DUMMY[4] not in json.dumps(errored)
    unfinished = [t for t in tools if t["a"].get("langfuse.observation.metadata.outcome") == "unfinished"]
    assert len(unfinished) == 1 and unfinished[0]["a"]["langfuse.observation.level"] == "WARNING"
    assert spans["llm_call_2"][0]["a"]["langfuse.observation.metadata.outcome"] == "unfinished"
    counts = json.loads(spans["hermes_run"][0]["a"]["langfuse.observation.output"])["counts"]
    assert counts["llm_retries"] == 1 and counts["tool_errors"] == 1


def test_plugin_keeps_dummy_secrets_out_of_every_exported_byte(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    msgs = [{"role": "user", "content": f"key {DUMMY[0]} db postgresql://u:{DUMMY[1]}@h/db at C:\\Users\\dwish\\x"}]
    api_pre(h, "t1:api:1", 1, msgs=msgs)
    api_post(h, "t1:api:1", 1, content="done " + DUMMY[5], calls=["c1"])
    h["pre_tool_call"](tool_name="tool_call", tool_call_id="c1", api_request_id="t1:api:1",
                       args={"name": "mcp__x__y", "arguments": {"api_key": DUMMY[0], "q": "Bearer " + DUMMY[4]}})
    tool_post(h, "c1", "X", "t1:api:1", result=json.dumps({"secret": DUMMY[2], "conn": "postgres://a:b@c/d"}),
              err="boom " + DUMMY[3], status="error")
    h["post_llm_call"](turn_id="t1")
    wire = b"".join(plugin_env.bodies).decode()
    assert wire, "something was exported"
    for secret in DUMMY[:6] + ["postgres://a:b@c/d", SK, "dwish"]:
        assert secret not in wire, secret


def test_plugin_truncates_large_payloads_with_a_marker(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    api_pre(h, "t1:api:1", 1)
    api_post(h, "t1:api:1", 1, calls=["c1"])
    tool_pre(h, "c1", "WMG", "t1:api:1")
    tool_post(h, "c1", "WMG", "t1:api:1", result="z" * 2_000_000)
    h["post_llm_call"](turn_id="t1")
    out = [s for s in plugin_env.spans() if s["a"]["langfuse.observation.type"] == "tool"][0]
    val = out["a"]["langfuse.observation.output"]
    assert "truncated" in val and len(val) < otlp.MAX_ATTR_BYTES + 200


def test_plugin_is_inert_without_credentials_and_never_raises(tmp_path, monkeypatch):
    for k in ("HERMES_TRACE_PUBLIC_KEY", "HERMES_TRACE_SECRET_KEY", "HERMES_TRACE_DEBUG_FILE"):
        monkeypatch.delenv(k, raising=False)
    mod, h = load_plugin(tmp_path)
    api_pre(h, "t1:api:1", 1)
    tool_pre(h, "c1", "WMG", "t1:api:1")
    h["pre_tool_call"](tool_name=None, args=object(), tool_call_id=None)       # garbage in
    h["post_api_request"](response=12, usage="nope", assistant_message=None)
    h["post_llm_call"]()
    assert mod._RUN is not None and mod._RUN.exporter is None, "no exporter, hence no network"


def test_plugin_debug_file_records_load_and_spans_without_credentials(tmp_path, monkeypatch):
    dbg = tmp_path / "trace.jsonl"
    monkeypatch.setenv("HERMES_TRACE_DEBUG_FILE", str(dbg))
    for k in ("HERMES_TRACE_PUBLIC_KEY", "HERMES_TRACE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    mod, h = load_plugin(tmp_path)
    api_pre(h, "t1:api:1", 1)
    api_post(h, "t1:api:1", 1, content="hello")
    h["post_llm_call"]()
    lines = [json.loads(x) for x in dbg.read_text().splitlines()]
    assert lines[0]["event"] == "plugin_registered"
    assert {"llm_call_1", "hermes_run"} <= {x["span"]["name"] for x in lines if "span" in x}


# -- install helpers ---------------------------------------------------------------------------------------------------

def test_profile_config_enables_only_the_project_plugin_and_only_when_configured():
    base = "agent:\n  max_turns: 1\nplatform_toolsets:\n  cli: []\nmcp_servers: {}\nsession_reset:\n  mode: none\n"
    on = run_live.live_profile_config(base, tracing=True)
    assert "plugins:\n  enabled:\n    - hermes-trace\n" in on and "observability/langfuse" not in on
    assert "plugins:" not in run_live.live_profile_config(base, tracing=False)
    with pytest.raises(SystemExit):
        run_live.live_profile_config(base + "plugins:\n  enabled: []\n", tracing=True)


PROVIDER_ENV = "PROVIDER_TOKEN=keep" + chr(10)


def test_credentials_are_merged_into_the_profile_env_without_touching_other_lines():
    cfg = run_live.trace_profile_env({"LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_SECRET_KEY": SK, "OTHER": "x"})
    assert set(cfg) == {"HERMES_TRACE_PUBLIC_KEY", "HERMES_TRACE_SECRET_KEY"}
    stale = "HERMES_TRACE_PUBLIC_KEY=old" + chr(10) + "HERMES_TRACE_BASE_URL=x" + chr(10)
    merged = run_live.merge_env_text(PROVIDER_ENV + stale, cfg)
    assert "PROVIDER_TOKEN=keep" in merged and "old" not in merged and merged.count("HERMES_TRACE_PUBLIC_KEY") == 1
    assert "HERMES_TRACE_BASE_URL" not in merged, "stale tracing settings are dropped"
    assert run_live.merge_env_text(PROVIDER_ENV + "HERMES_TRACE_SECRET_KEY=s" + chr(10), {}) == PROVIDER_ENV


def test_only_correlation_ids_reach_the_hermes_command_line(monkeypatch):
    seen = {}
    monkeypatch.setattr(agent.subprocess, "run", lambda argv, **kw: seen.update(argv=argv, input=kw["input"]) or
                        types.SimpleNamespace(returncode=0, stdout=b"{}", stderr=b""))
    env = {"HERMES_TRACE_ID": "a" * 32, "HERMES_TRACE_PARENT": "b" * 16, "HERMES_TRACE_SESSION": "live-1",
           "HERMES_TRACE_SECRET_KEY": SK, "HERMES_TRACE_ID_BAD": "x", "LD_PRELOAD": "evil"}
    agent.call_hermes("the prompt", trace_env=env)
    argv = " ".join(seen["argv"])
    assert f"HERMES_TRACE_ID={'a' * 32}" in argv and f"HERMES_TRACE_PARENT={'b' * 16}" in argv
    assert SK not in argv and "LD_PRELOAD" not in argv and "the prompt" not in argv
    assert agent.trace_env_flags({"HERMES_TRACE_ID": "not-hex"}) == []


# -- cross-process correlation (controller span -> plugin) ---------------------------------------------------

def test_controller_hermes_span_is_the_parent_of_the_plugin_run(tmp_path, collector, monkeypatch):
    tr = Tracer(exporter(collector.url))
    store, b, j = tl.Store(tmp_path / "s"), tl.FakeBroker(), tl.FakeJournal()
    ids = {}

    def hermes(prompt):
        env = tr.child_env()                                  # what call_hermes would pass to the container
        ids.update(env)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("HERMES_TRACE_PUBLIC_KEY", PK)
        monkeypatch.setenv("HERMES_TRACE_SECRET_KEY", SK)
        monkeypatch.setenv("HERMES_TRACE_BASE_URL", collector.url)
        mod, h = load_plugin(tmp_path, "t_corr")
        api_pre(h, "t1:api:1", 1)
        api_post(h, "t1:api:1", 1, content="{}")
        h["post_llm_call"]()
        return json.dumps({"holdings": [], "entries": [], "no_entry_reason": "nothing qualifies"}), "sha"

    run_cycle(store, b, j, review=True, selector_fn=lambda t, s, e: ([], "ok", {"universe": tl.WL}),
              hermes_fn=hermes, sleep=tl.NOSLEEP, tracer=tr)
    assert tr.exporter.flush(3)
    spans = collector.spans()
    by = {}
    for s in spans:
        by.setdefault(s["name"], []).append(s)
    hs = by["hermes_research"][0]
    assert ids["HERMES_TRACE_ID"] == hs["traceId"] and ids["HERMES_TRACE_PARENT"] == hs["spanId"]
    run = by["hermes_run"][0]
    assert run["traceId"] == hs["traceId"] and run["parentSpanId"] == hs["spanId"]
    assert by["llm_call_1"][0]["traceId"] == hs["traceId"]
    cycle = by["live_cycle"][0]
    assert hs["parentSpanId"] == cycle["spanId"]
    names = {s["name"] for s in spans}
    assert {"reconcile", "selection", "prompt_assembly", "reply_validation", "reconcile_after"} <= names


# -- trading parity ----------------------------------------------------------------------------------------------------

def scenario_run(name, tmp_path, tracer):
    store, b, j = tl.Store(tmp_path), tl.FakeBroker(), tl.FakeJournal()
    sel_state = {"fail": False}

    def sel(today, session, excl):
        if sel_state["fail"]:
            raise RuntimeError("laya worker down")
        return [{"ticker": "WMG", "sector": "", "name": ""}], "ok", {"universe": list(tl.WL)}
    replies = {
        "hold": lambda: tl._reply("HOLD"),
        "revise": lambda: tl._reply("REVISE", revision={"horizon_trading_days": 25, "stop_price": 47.5}),
        "exit": lambda: tl._reply("EXIT"),
        "entry_rejected": lambda: json.dumps({"holdings": [], "entries": [tl.plan(ticker="WMG")]}),
        "invalid_reply": lambda: "buy everything",
        "selector_failure": lambda: json.dumps({"holdings": [], "entries": [tl.plan(ticker="WMG")]}),
        "model_failure": lambda: "",
    }
    if name in ("hold", "revise", "exit"):
        tl.do_enter(store, b, j)
    if name == "entry_rejected":
        b.quote_ts = (tl.NOW - __import__("datetime").timedelta(seconds=600)).isoformat()
    sel_state["fail"] = name == "selector_failure"

    def hermes(prompt):
        if name == "model_failure":
            raise TimeoutError("hermes timeout")
        return replies[name](), "sha"
    review = name != "reconcile_only"
    out = run_cycle(store, b, j, review=review, selector_fn=sel, hermes_fn=hermes, sleep=tl.NOSLEEP, tracer=tracer)
    res = {"out": out, "submits": b.submits, "cancels": b.cancels, "journal": j.calls, "state": store.data,
           "events": [json.loads(x)["event"] for x in (tmp_path / "events.jsonl").read_text().splitlines()]
           if (tmp_path / "events.jsonl").exists() else []}
    return normalise(res, store)


WALL_CLOCK = re.compile('"(confirmed_at|at)": "20[0-9][0-9]-[^"]*"')


def normalise(res, store):
    """Plan ids are random uuids per run (and appear in client order ids); map them to stable labels."""
    text = json.dumps(res, default=str, sort_keys=True)
    text = WALL_CLOCK.sub(lambda m: '"' + m.group(1) + '": "<wall-clock>"', text)   # datetime.now() in the engine
    for n, pid in enumerate(sorted(store.plans)):
        text = text.replace(pid, f"PLAN{n}").replace(pid[:20], f"PLAN{n}")
    return json.loads(text)


SCENARIOS = ["reconcile_only", "hold", "revise", "exit", "entry_rejected", "invalid_reply", "selector_failure",
             "model_failure"]


def tracer_variants(tmp_path):
    """name -> (tracer, cleanup)"""
    hung = Collector("hang")
    ok = Collector()
    throw = otlp.Exporter("http://127.0.0.1:9", PK, SK, post=lambda b: (_ for _ in ()).throw(OSError("boom")))
    full = otlp.Exporter(hung.url, PK, SK, queue_max=1, batch_max=1, timeout_s=5)
    return {
        "off": (None, lambda: None),
        "null_missing_credentials": (load_tracer(tmp_path / "nothing"), lambda: None),
        "healthy": (Tracer(otlp.Exporter(ok.url, PK, SK)), ok.stop),
        "throwing_exporter": (Tracer(throw), lambda: None),
        "hung_exporter": (Tracer(otlp.Exporter(hung.url, PK, SK, timeout_s=5)), hung.release.set),
        "queue_full": (Tracer(full), lambda: None),
        "unreachable": (Tracer(otlp.Exporter(dead_url(), PK, SK, timeout_s=0.5)), lambda: None),
    }, (ok, hung)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_trading_is_identical_with_tracing_off_healthy_misconfigured_throwing_hung_and_full(scenario, tmp_path):
    variants, servers = tracer_variants(tmp_path)
    results = {}
    try:
        for name, (tracer, _) in variants.items():
            t0 = time.monotonic()
            results[name] = scenario_run(scenario, tmp_path / name, tracer)
            elapsed = time.monotonic() - t0
            assert elapsed < 4.0, f"{name}: the cycle waited {elapsed:.1f}s (tracing must never block trading)"
            if tracer is not None:
                t1 = time.monotonic()
                tracer.close()
                assert time.monotonic() - t1 < 0.25, f"{name}: shutdown waited"
    finally:
        for _, cleanup in variants.values():
            cleanup()
        for s in servers:
            s.stop()
    base = results["off"]
    assert base["journal"] or base["out"], "scenario did something"
    for name, res in results.items():
        assert res == base, f"{name} diverged from tracing-off for {scenario}"


def test_a_trading_failure_is_identical_and_visible_in_the_trace(tmp_path, collector):
    class Boom(tl.FakeBroker):
        def positions(self):
            raise RuntimeError("broker unreachable")
    out = []
    for tracer in (None, Tracer(exporter(collector.url))):
        store, j = tl.Store(tmp_path / str(len(out))), tl.FakeJournal()
        with pytest.raises(RuntimeError, match="broker unreachable"):
            run_cycle(store, Boom(), j, review=True, selector_fn=lambda *a: ([], "ok", {}),
                      hermes_fn=lambda p: ("", "s"), sleep=tl.NOSLEEP, tracer=tracer)
        out.append(j.calls)
        if tracer:
            assert tracer.exporter.flush(3)
    assert out[0] == out[1]
    cycle = [s for s in collector.spans() if s["name"] == "live_cycle"][0]
    assert cycle["status"]["code"] == 2 and "broker unreachable" in cycle["status"]["message"]


def test_tracing_adds_no_trace_identifiers_to_durable_state_or_idempotency_keys(tmp_path, collector):
    tr = Tracer(exporter(collector.url))
    res = scenario_run("hold", tmp_path / "a", tr)
    assert tr.exporter.flush(3)
    ids = {s["traceId"] for s in collector.spans()} | {s["spanId"] for s in collector.spans()}
    blob = json.dumps(res["state"]) + json.dumps(res["submits"]) + json.dumps(res["journal"], default=str)
    assert not any(i in blob for i in ids)
    plain = scenario_run("hold", tmp_path / "b", None)
    assert res == plain, "identical after plan ids are normalised"
    assert all(s["client_order_id"].startswith("lv-PLAN0-") for s in res["submits"])


def test_diagnostic_fields_cannot_raise_into_trading_even_for_a_malformed_shortlist(tmp_path, collector):
    """Argument expressions for span output are evaluated even when tracing is off: they must be total."""
    reply = json.dumps({"holdings": [], "entries": [], "no_entry_reason": "nothing qualifies"})
    for n, tracer in enumerate((None, Tracer(exporter(collector.url)))):
        out = run_cycle(tl.Store(tmp_path / str(n)), tl.FakeBroker(), tl.FakeJournal(), review=True,
                        selector_fn=lambda t, s, e: (["WMG", None, 7], "ok", {"universe": tl.WL}),
                        hermes_fn=lambda p: (reply, "sha"), sleep=tl.NOSLEEP, tracer=tracer)
        assert out["no_entry_reason"] == "nothing qualifies"
