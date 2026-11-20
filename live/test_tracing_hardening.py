"""Ticket 1 review corrections: (1) redaction of secrets inside JSON-formatted text, (2) no export waits on Hermes
completion or controller shutdown, (3) no filesystem work on the producer path when the queue is full,
(4) model-request context that is inspectable or explicitly marked incomplete.

Each test was first run against the unfixed code and failed there (see the ticket's progress log)."""
import builtins
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_live  # noqa: E402
from tracing import otlp  # noqa: E402
from tracing.controller import Tracer  # noqa: E402
from test_tracing import (  # noqa: E402  (helpers shared with the main tracing tests; fixtures in conftest)
    PK, SK, REAL, Collector, dead_url, exporter, load_plugin, rec, api_pre, api_post)

SECRET = "ordinary-sensitive-value"      # deliberately has no recognisable token prefix


def finish(mod, h):
    """Hermes finishing a turn, then waiting (in the TEST only) for the background export to deliver."""
    h["post_llm_call"](turn_id="t1")
    assert mod._RUN.exporter.flush(5)


# -- finding 1: secrets inside JSON-formatted strings ------------------------------------------------------------------

BS = chr(92)
Q = chr(34)
REDACTION_CASES = {
    "compact json": '{"password":"@@"}',
    "spaced json": '{"password": "@@", "ticker": "WMG"}',
    "escaped json": '{' + BS + Q + 'password' + BS + Q + ':' + BS + Q + '@@' + BS + Q + '}',
    "double escaped json": '{' + BS * 3 + Q + 'password' + BS * 3 + Q + ':' + BS * 3 + Q + '@@' + BS * 3 + Q + '}',
    "json inside json": json.dumps({"payload": json.dumps({"client_secret": "@@"})}),
    "json inside json inside json": json.dumps({"a": json.dumps({"b": json.dumps({"password": "@@"})})}),
    "python repr": "{'password': '@@'}",
    "camelCase apiKey": '{"apiKey":"@@"}',
    "upper API_KEY": '{"API_KEY":"@@"}',
    "x-api-key": '{"x-api-key":"@@"}',
    "accessToken": '{"accessToken":"@@"}',
    "refresh_token": '{"refresh_token":"@@"}',
    "clientSecret": '{"clientSecret":"@@"}',
    "secretAccessKey": '{"secretAccessKey":"@@"}',
    "AWS env": "AWS_SECRET_ACCESS_KEY=@@",
    "passwd": '{"passwd":"@@"}',
    "pwd": '{"pwd":"@@"}',
    "basic auth header": "Authorization: Basic @@",
    "private_key": '{"private_key":"@@"}',
    "session_token": '{"session_token":"@@"}',
    "passphrase": '{"passphrase":"@@"}',
    "value with spaces": '{"password":"@@ and more words"}',
    "object value": '{"credentials": {"user": "u1", "pass": "@@"}}',
    "list value": '{"secrets": ["@@", "other"]}',
    "form encoded": "password=@@&user=x",
    "yaml": "password: @@",
    "embedded escaped quote": '{"password":"ab' + BS + Q + '@@"}',
    "prose": "the password is @@.",
    "headers dict": '{"headers": {"Authorization": "Bearer @@"}}',
    "cookie": '{"Cookie": "session=@@"}',
    "pretty json": '{' + chr(10) + '  "password": "@@"' + chr(10) + '}',
    "query string": "https://example.com/x?token=@@&y=1",
    "export line": "export DB_PASSWORD='@@'",
    "space before colon": '{"password" : "@@"}',
    "inside prose": 'The tool replied {"status": "ok", "password": "@@"} and then stopped.',
}


@pytest.mark.parametrize("name", sorted(REDACTION_CASES))
def test_secrets_inside_json_formatted_text_never_survive_redaction(name):
    text = REDACTION_CASES[name].replace("@@", SECRET)
    assert SECRET not in otlp.redact_text(text), "redact_text"
    assert SECRET not in json.dumps(otlp.sanitize(text)), "sanitize(str)"
    assert SECRET not in json.dumps(otlp.sanitize({"content": text, "n": [text]})), "sanitize(nested)"
    assert SECRET not in otlp.describe_exception(RuntimeError(text)), "exception"
    assert SECRET not in otlp.encode_attr({"input": text}), "encode_attr"


def test_sensitive_key_variants_in_dictionaries_and_ordinary_content_stays_useful():
    s = otlp.sanitize({"clientSecret": SECRET, "auth": {"apiKey": SECRET}, "X-Api-Key": SECRET,
                       "secretAccessKey": SECRET, "refreshToken": SECRET, "ticker": "WMG", "rsi": 51.2,
                       "usage": {"input_tokens": 12, "token_count": 4, "max_tokens": 99},
                       "note": 'RSI is 51 and "support" sits near 47.5, resistance 52'})
    assert SECRET not in json.dumps(s)
    assert s["ticker"] == "WMG" and s["rsi"] == 51.2 and s["usage"] == {"input_tokens": 12, "token_count": 4,
                                                                         "max_tokens": 99}
    assert s["note"] == 'RSI is 51 and "support" sits near 47.5, resistance 52'
    plain = '{"ticker": "WMG", "rsi": 51.2, "levels": [47.5, 52.0]}'
    assert json.loads(otlp.sanitize(plain)) == json.loads(plain), "ordinary JSON text stays valid and unchanged"


HOSTILE = {
    "unterminated value": '{"password":"' + "a" * 3_000_000,
    "many pairs": '"password":"x" ' * 200_000,
    "open brackets": '"credentials": ' + "{" * 500_000,
    "backslashes": '"password": "' + chr(92) * 2_000_000,
    "quote run": '"password":' + chr(34) * 2_000_000,
    "colon run": "password:" * 300_000,
    "long identifier": "a" * 2_000_000 + ":",
    "deep arrays": "[" * 100_000 + "]" * 100_000,
    "huge valid json": json.dumps({"password": SECRET, "rows": [{"i": i, "n": "x" * 50} for i in range(60_000)]}),
    "huge escaped json": json.dumps(json.dumps({"password": SECRET, "rows": ["y" * 50] * 60_000})),
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_malformed_or_oversized_content_is_bounded_and_leaks_no_fragment(name):
    text = HOSTILE[name]
    t0 = time.monotonic()
    out = otlp.encode_attr({"content": text})
    assert time.monotonic() - t0 < 1.5, f"{name}: slow path"
    assert len(out) <= otlp.MAX_ATTR_BYTES + 400
    assert SECRET not in out
    if "password" in text[:40] or "credentials" in text[:40]:
        assert "a" * 40 not in out, "no unsafe fragment of an unterminated secret"
    assert "truncated" in out or len(text) < 9000


def test_redaction_applies_to_every_export_path_in_the_complete_wire_payload(tmp_path, plugin_env):
    """Model messages, request instructions, tool args/results, metadata, names, exceptions and the controller."""
    mod, h = load_plugin(tmp_path)
    jsn = lambda k: json.dumps({k: SECRET})  # noqa: E731
    items = [{"role": "system", "content": jsn("password")},
             {"role": "user", "content": "db " + jsn("api_key") + " ok"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "c1", "type": "function", "function": {"name": REAL, "arguments": jsn("client_secret")}}]},
             {"type": "function_call_output", "call_id": "c1", "output": json.dumps({"x": jsn("token")})}]
    h["pre_api_request"](api_request_id="r1", api_call_count=1, retry_count=0, model="m",
                         provider="p password=" + SECRET,
                         api_mode="x", started_at=time.time(), request_messages=items, message_count=4, tool_count=1,
                         request={"body": {"instructions": jsn("secret"), "input": items,
                                           "tools": [{"name": "tool_password=" + SECRET}]}})
    h["post_api_request"](api_request_id="r1", api_call_count=1, response={"assistant_message": {
        "content": jsn("token"),
        "tool_calls": [{"id": "c1", "function": {"name": REAL, "arguments": jsn("password")}}]}},
        usage={"input_tokens": 1, "output_tokens": 1}, finish_reason="stop", model="m",
        assistant_message={"content": jsn("token"), "reasoning": jsn("password")})
    h["pre_tool_call"](tool_name="tool_call", tool_call_id="c1", api_request_id="r1",
                       args={"name": "mcp__x__" + jsn("password"), "arguments": {"clientSecret": SECRET,
                                                                                  "q": jsn("password")}})
    h["post_tool_call"](tool_name="tool_call", tool_call_id="c1", api_request_id="r1", args={}, duration_ms=3,
                        result=json.dumps({"data": jsn("password"), "note": jsn("token")}), status="error",
                        error_type="ToolError " + jsn("password"), error_message="failed " + jsn("password"))
    h["pre_tool_call"](tool_name="tool_call", tool_call_id="c2", api_request_id="r1",
                       args={"name": "n", "arguments": {}})
    finish(mod, h)
    c = plugin_env
    wire = b"".join(c.bodies).decode()
    assert wire and SECRET not in wire, "plugin export"
    # controller path: input/output/metadata/status and a trading exception carrying a JSON secret
    tr = Tracer(exporter(c.url))
    with pytest.raises(RuntimeError):
        with tr.trace("live_cycle", input={"p": jsn("password")}, metadata={"conn": jsn("api_key")}):
            with tr.span("selection", output=jsn("secret"), metadata={"m": jsn("token")}):
                pass
            raise RuntimeError("boom " + jsn("password"))
    assert tr.exporter.flush(5)
    assert SECRET not in b"".join(c.bodies).decode(), "controller export"
    names = {s["name"] for s in c.spans()}
    assert {"live_cycle", "selection", "hermes_run", "llm_call_1"} <= names, "both paths really exported"


def test_the_exporter_is_the_last_line_of_defence_for_any_attribute_that_bypassed_sanitising(collector):
    e = exporter(collector.url)
    raw = otlp.span_record(otlp.new_trace_id(), otlp.new_span_id(), None, "n password=" + SECRET, 1, 2,
                           {"langfuse.observation.input": '{"password":"' + SECRET + '"}', "x": "token=" + SECRET})
    e.submit(raw)
    assert e.flush(3)
    assert SECRET not in b"".join(collector.bodies).decode()


# -- finding 2: no export waits on completion / exit -----------------------------------------------------------------

def test_hermes_turn_completion_does_not_wait_for_a_stalled_exporter(tmp_path, monkeypatch):
    hung = Collector("hang")
    try:
        monkeypatch.setenv("HERMES_TRACE_PUBLIC_KEY", PK)
        monkeypatch.setenv("HERMES_TRACE_SECRET_KEY", SK)
        monkeypatch.setenv("HERMES_TRACE_BASE_URL", hung.url)
        mod, h = load_plugin(tmp_path, drain=False)       # the real hook, without the test-only drain
        waits = []
        monkeypatch.setattr(mod.otlp.Exporter, "flush", lambda self, timeout=0: waits.append(timeout) or True)
        for i in range(30):
            api_pre(h, f"r{i}", i)
            api_post(h, f"r{i}", i, content="x")
        t0 = time.monotonic()
        h["post_llm_call"](turn_id="t1")
        mod._finish()
        dt = time.monotonic() - t0
        assert dt < 0.25, f"turn completion took {dt:.2f}s with a stalled exporter"
        assert waits == [], "completion must not call a draining flush"
    finally:
        hung.stop()


def test_controller_shutdown_does_not_wait_for_a_stalled_exporter():
    hung = Collector("hang")
    try:
        tr = Tracer(otlp.Exporter(hung.url, PK, SK, timeout_s=5))
        with tr.trace("live_cycle"):
            with tr.span("reconcile"):
                pass
        t0 = time.monotonic()
        tr.close()
        assert time.monotonic() - t0 < 0.25, "tracer shutdown waited for the network"
    finally:
        hung.stop()


def test_no_production_path_calls_a_draining_flush_or_a_timed_close():
    paths = [HERE / "tracing" / "hermes_plugin" / "__init__.py", HERE / "tracing" / "controller.py",
             HERE / "run_live.py", HERE / "agent.py", HERE / "selector.py", HERE / "engine.py"]
    paths.append(HERE / "trace_screen.py")
    allowed = {"controller.py": "done = exporter.flush(limit)", "__init__.py": "run.exporter.flush(drain)"}
    for p in paths:
        text = p.read_text(encoding="utf-8")
        for line in (ln for ln in text.splitlines() if ".flush(" in ln):
            assert line.strip().startswith(allowed.get(p.name, "<none>")), f"{p.name} drains the exporter: {line}"
        assert "close(2.0)" not in text and "EXIT_TIMEOUT" not in text, f"{p.name} has a timed export wait"
    ctl = (HERE / "tracing" / "controller.py").read_text(encoding="utf-8")
    body = ctl.split("def diagnostic_drain(", 1)[1].split(chr(10) + "def ", 1)[0]
    assert "exporter.flush(limit)" in body, "the controller's only wait lives in diagnostic_drain"
    plug = (HERE / "tracing" / "hermes_plugin" / "__init__.py").read_text(encoding="utf-8")
    assert "drain = _diagnostic_drain_s()" in plug and "if drain:" in plug, "the plugin's wait is env-gated"
    rl = (HERE / "run_live.py").read_text(encoding="utf-8")
    assert rl.count("diagnostic_drain(") == 1
    dry = rl.split('if a.command == "dry-run":', 1)[1].split("return 0", 1)[0]
    assert "diagnostic_drain(" in dry, "the drain exists only in the dry-run branch"


EXIT_SCRIPT = (
    "import sys, types, importlib.util, time\n"
    "d, linger = sys.argv[1], float(sys.argv[2])\n"
    "ns = types.ModuleType('hermes_plugins'); ns.__path__ = []; sys.modules['hermes_plugins'] = ns\n"
    "spec = importlib.util.spec_from_file_location('hermes_plugins.t', d + '/__init__.py', "
    "submodule_search_locations=[d])\n"
    "m = importlib.util.module_from_spec(spec); m.__package__ = 'hermes_plugins.t'; m.__path__ = [d]\n"
    "sys.modules['hermes_plugins.t'] = m; spec.loader.exec_module(m)\n"
    "h = {}; m.register(types.SimpleNamespace(register_hook=lambda n, f: h.__setitem__(n, f)))\n"
    "for i in range(40):\n"
    "    h['pre_tool_call'](tool_name='x', args={'a': i}, tool_call_id=str(i), api_request_id='r')\n"
    "    h['post_tool_call'](tool_name='x', args={}, tool_call_id=str(i), api_request_id='r', result='ok', "
    "duration_ms=1, status='ok')\n"
    "t = time.monotonic(); h['post_llm_call'](); print('FINISH', round(time.monotonic() - t, 3), flush=True)\n"
    "time.sleep(linger)\n")


def _install_plugin(tmp_path):
    plug = tmp_path / "hermes_trace"
    plug.mkdir()
    for f, t in run_live.trace_plugin_files().items():
        (plug / f).write_text(t, encoding="utf-8")
    return plug


def _run_exit(plug, env_extra, linger=0.0):
    import os
    env = {"PATH": "", "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), **env_extra}
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-c", EXIT_SCRIPT, str(plug), str(linger)], capture_output=True, text=True,
                       timeout=30, env=env)
    wall = time.monotonic() - t0
    assert r.returncode == 0 and "FINISH" in r.stdout, r.stderr[-500:]
    return wall, float(r.stdout.split("FINISH")[1].split()[0])


def test_a_hermes_process_exits_as_fast_with_a_stalled_or_dead_service_as_with_tracing_off(tmp_path):
    plug = _install_plugin(tmp_path)
    off_wall, _ = _run_exit(plug, {})
    hung = Collector("hang")
    try:
        for url in (hung.url, dead_url()):
            wall, finish_s = _run_exit(plug, {"HERMES_TRACE_PUBLIC_KEY": PK, "HERMES_TRACE_SECRET_KEY": SK,
                                              "HERMES_TRACE_BASE_URL": url})
            assert finish_s < 0.25, f"decision return took {finish_s}s"
            assert wall < off_wall + 0.6, f"process exit waited {wall:.2f}s vs {off_wall:.2f}s with tracing off"
    finally:
        hung.stop()


def test_healthy_service_still_receives_the_final_observations_while_the_process_winds_down(tmp_path):
    """No wait is added, yet delivery continues in the background for as long as the process lingers."""
    plug = _install_plugin(tmp_path)
    c = Collector()
    try:
        _run_exit(plug, {"HERMES_TRACE_PUBLIC_KEY": PK, "HERMES_TRACE_SECRET_KEY": SK, "HERMES_TRACE_BASE_URL": c.url},
                  linger=0.6)
        names = {s["name"] for s in c.spans()}
        assert "hermes_run" in names and "x" in names, names
    finally:
        c.stop()


# -- finding 3: warnings never touch the producer path ----------------------------------------------------------------

class SlowFs:
    """Record which thread does filesystem work on the warning log, and make that work slow or failing."""

    def __init__(self, monkeypatch, path, delay=0.0, fail=False):
        self.path, self.delay, self.fail, self.threads = str(path), delay, fail, []
        real_exists, real_size, real_open = otlp.os.path.exists, otlp.os.path.getsize, builtins.open

        def hit(p):
            if str(p) == self.path:
                self.threads.append(threading.current_thread())
                time.sleep(self.delay)
                if self.fail:
                    raise OSError("disk on fire")
        monkeypatch.setattr(otlp.os.path, "exists", lambda p: (hit(p), real_exists(p))[1])
        monkeypatch.setattr(otlp.os.path, "getsize", lambda p: (hit(p), real_size(p))[1])
        monkeypatch.setattr(builtins, "open", lambda p, *a, **k: (hit(p), real_open(p, *a, **k))[1])


def _overflowing_exporter(tmp_path, collector_mode="hang", **kw):
    c = Collector(collector_mode)
    wp = tmp_path / "warn.log"
    return c, wp, otlp.Exporter(c.url, PK, SK, queue_max=2, batch_max=1, timeout_s=5, warn_path=str(wp), **kw)


def test_queue_overflow_does_no_filesystem_work_on_the_caller_thread(tmp_path, monkeypatch):
    c, wp, e = _overflowing_exporter(tmp_path)
    fs = SlowFs(monkeypatch, wp, delay=0.4)
    try:
        t0 = time.monotonic()
        for _ in range(300):
            e.submit(rec())
        dt = time.monotonic() - t0
        assert dt < 0.3, f"submit blocked {dt:.2f}s on warning persistence"
        assert e.stats["dropped_queue_full"] >= 290
        assert threading.current_thread() not in fs.threads, "the caller performed file I/O"
    finally:
        c.stop()


def test_slow_warning_persistence_does_not_stop_the_exporter_progressing(tmp_path, monkeypatch):
    gate, posted = threading.Event(), []

    def post(body):
        posted.append(body)
        if len(posted) == 1:
            gate.wait(5)                                  # first send stalls so that the queue overflows
        return 200
    wp = tmp_path / "warn.log"
    e = otlp.Exporter("http://x", PK, SK, queue_max=2, batch_max=1, post=post, warn_path=str(wp))
    fs = SlowFs(monkeypatch, wp, delay=2.0)
    for _ in range(20):
        e.submit(rec())                                   # overflow: persistence is now (slowly) under way
    gate.set()
    t0 = time.monotonic()
    assert e.flush(1.5), "the exporter was blocked by slow warning persistence"
    assert time.monotonic() - t0 < 1.5 and len(posted) >= 3
    e.submit(rec("later"))                                # and it keeps accepting work
    assert e.flush(1.5) and threading.current_thread() not in fs.threads


def test_failing_warning_persistence_is_contained_and_overflow_is_still_counted(tmp_path, monkeypatch):
    c, wp, e = _overflowing_exporter(tmp_path)
    SlowFs(monkeypatch, wp, fail=True)
    try:
        for _ in range(50):
            e.submit(rec())
        assert e.stats["dropped_queue_full"] >= 40
        assert any("queue full" in w for w in e.warnings), "kept in memory regardless of the disk"
    finally:
        c.stop()


def test_warning_log_and_threads_stay_bounded_under_sustained_overflow(tmp_path):
    c, wp, e = _overflowing_exporter(tmp_path)
    before = threading.active_count()                       # after the test's own collector threads exist
    try:
        for i in range(20_000):
            e.submit(rec())
            if i % 2_000 == 0:
                time.sleep(0.05)
        time.sleep(0.3)
        assert threading.active_count() - before <= 3, "worker + warning writer (+ the collector's handler) only"
        assert not wp.exists() or wp.stat().st_size <= 16_384 + 400
        assert len(e.warnings) <= 20 and len(e._q) <= 2
    finally:
        c.stop()


# -- finding 4: model-request context ----------------------------------------------------------------------------------

POLICY = "POLICY: risk per position never exceeds 5 percent; stops sit at technical levels."


def pre(h, rid, n, items, instructions=None, tools=None, retry=0):
    body = {"input": items}
    if instructions is not None:
        body["instructions"] = instructions
    if tools is not None:
        body["tools"] = tools
    h["pre_api_request"](api_request_id=rid, api_call_count=n, retry_count=retry, model="m", provider="openai-codex",
                         api_mode="codex_responses", started_at=time.time(), request_messages=list(items),
                         message_count=len(items), tool_count=len(tools or []),
                         request={"method": "POST", "body": body})


def post(h, rid, n):
    h["post_api_request"](api_request_id=rid, api_call_count=n, response={"assistant_message": {"content": "ok"}},
                          usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}, finish_reason="stop",
                          model="m", assistant_message={"content": "ok"})


def conversation():
    """A codex-style (Responses API) request body: the shape the pinned gateway actually sends."""
    call = lambda cid, tk: {"type": "function_call", "call_id": cid, "name": "tool_call", "arguments": json.dumps(  # noqa
        {"name": REAL, "arguments": {"ticker": tk}})}
    out = lambda cid, tk: {"type": "function_call_output", "call_id": cid, "output": json.dumps(  # noqa
        {"ticker": tk, "rsi": 51.2})}
    return [{"role": "user", "content": "Review the book."}, call("c1", "WMG"), call("c2", "ATI"),
            out("c1", "WMG"), out("c2", "ATI"), {"role": "assistant", "content": "checking the next names"},
            call("c3", "OLN"), out("c3", "OLN"), {"role": "user", "content": "continue"}]


def gen_spans(c):
    d = {}
    for s in c.spans():
        if s["a"].get("langfuse.observation.type") == "generation":
            d[s["name"]] = s
    return d


def request_input(span):
    return json.loads(span["a"]["langfuse.observation.input"])


def rebuild(spans_by_id, span):
    """The messages a request received, rebuilt ONLY from exported spans (its own plus the earlier ones it cites)."""
    inp = request_input(span)
    got = {m["index"]: {k: v for k, v in m.items() if k != "index"} for m in inp["messages"]}
    for ref in inp.get("inherited", []):
        src = {m["index"]: {k: v for k, v in m.items() if k != "index"}
               for m in request_input(spans_by_id[ref["span_id"]])["messages"]}
        for i in range(ref["indexes"][0], ref["indexes"][1] + 1):
            got[i] = src[i]
    return got


def test_requests_longer_than_three_messages_are_inspectable_without_duplicating_history(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    items = conversation()
    for n, upto in ((1, 1), (2, 5), (3, 9)):
        pre(h, f"r{n}", n, items[:upto], instructions=POLICY, tools=[{"name": "tool_call"}, {"name": "tool_describe"}])
        post(h, f"r{n}", n)
    finish(mod, h)
    gens = gen_spans(plugin_env)
    by_id = {s["spanId"]: s for s in plugin_env.spans()}
    third = request_input(gens["llm_call_3"])
    assert third["message_count"] == 9 and third["complete"] is True
    assert [m["index"] for m in third["messages"]] == [5, 6, 7, 8], "only the new messages are exported again"
    cited = {r["span_id"] for r in third["inherited"]}
    assert gens["llm_call_1"]["spanId"] in cited, "cites where history was recorded"
    rebuilt = rebuild(by_id, gens["llm_call_3"])
    assert sorted(rebuilt) == list(range(9)), "no silently missing message"
    assert [rebuilt[i].get("role") or rebuilt[i]["type"] for i in range(9)] == [
        "user", "function_call", "function_call", "function_call_output", "function_call_output", "assistant",
        "function_call", "function_call_output", "user"], "order and roles preserved"
    assert rebuilt[3]["call_id"] == "c1" and rebuilt[1]["call_id"] == "c1", "call/result association preserved"
    assert json.loads(rebuilt[1]["arguments"]) == {"name": REAL, "arguments": {"ticker": "WMG"}}
    assert json.loads(rebuilt[7]["output"])["ticker"] == "OLN"
    first = request_input(gens["llm_call_1"])
    assert POLICY in json.dumps(first["system"]), "policy is visible in the request that introduced it"
    assert request_input(gens["llm_call_3"])["system"]["same_as"] == "llm_call_1"
    assert first["tools"]["names"] == ["tool_call", "tool_describe"] and "schemas" in first["tools"]["not_exported"]


def test_chat_completions_tool_call_associations_are_preserved(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    items = [{"role": "system", "content": POLICY}, {"role": "user", "content": "go"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "k1", "type": "function", "function": {"name": REAL, "arguments": '{"ticker": "WMG"}'}}]},
             {"role": "tool", "tool_call_id": "k1", "content": '{"rsi": 51.2}'},
             {"role": "assistant", "content": "done"}]
    h["pre_api_request"](api_request_id="r1", api_call_count=1, retry_count=0, model="m", provider="p", api_mode="chat",
                         started_at=time.time(), request_messages=items, message_count=5, tool_count=0)
    post(h, "r1", 1)
    finish(mod, h)
    rebuilt = rebuild({}, gen_spans(plugin_env)["llm_call_1"])
    assert rebuilt[0]["content"] == POLICY and rebuilt[2]["tool_calls"][0]["id"] == "k1"
    assert rebuilt[2]["tool_calls"][0]["function"]["name"] == REAL and rebuilt[3]["tool_call_id"] == "k1"


def test_omitted_messages_are_listed_with_a_reason_and_the_request_is_marked_incomplete(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    items = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "w" * 5000} for i in range(40)]
    pre(h, "r1", 1, items, instructions=POLICY)
    post(h, "r1", 1)
    pre(h, "r2", 2, items + [{"role": "user", "content": "next"}], instructions=POLICY)
    post(h, "r2", 2)
    finish(mod, h)
    gens = gen_spans(plugin_env)
    for name, total in (("llm_call_1", 40), ("llm_call_2", 41)):
        inp = request_input(gens[name])
        assert inp["message_count"] == total and inp["complete"] is False
        exported = {m["index"] for m in inp["messages"]}
        inherited = {i for r in inp.get("inherited", []) for i in range(r["indexes"][0], r["indexes"][1] + 1)}
        omitted = {i for o in inp["omitted"] for i in range(o["indexes"][0], o["indexes"][1] + 1)}
        assert exported | inherited | omitted == set(range(total)), "every message is accounted for"
        assert not (exported & omitted) and not (inherited & omitted) and not (exported & inherited)
        assert omitted and all(o["reason"] for o in inp["omitted"]) and all(o["roles"] for o in inp["omitted"])
        assert 0 in exported or 0 in inherited, "the first message (task and policy) is kept"
        assert (total - 1) in exported, "the newest message is kept"
    assert 'truncated' in json.dumps(request_input(gens["llm_call_1"])["messages"]), "clipping is marked in-band"
    assert request_input(gens["llm_call_1"])["clipped"], "and listed"


def test_a_changed_history_is_reported_not_silently_treated_as_a_continuation(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    items = conversation()
    pre(h, "r1", 1, items[:6])
    post(h, "r1", 1)
    pre(h, "r2", 2, items[:1] + items[4:7])                     # the middle was compacted away between requests
    post(h, "r2", 2)
    finish(mod, h)
    second = request_input(gen_spans(plugin_env)["llm_call_2"])
    assert second["history_changed_at_index"] == 1 and second["message_count"] == 4
    assert [m["index"] for m in second["messages"]] == [1, 2, 3] and second["complete"] is True
    assert second["inherited"][0]["indexes"] == [0, 0]


def test_request_context_is_redacted_and_malformed_shapes_never_raise(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    items = [{"role": "user", "content": json.dumps({"password": SECRET})},
             {"type": "function_call", "call_id": "c1", "name": "tool_call",
              "arguments": json.dumps({"name": REAL, "arguments": {"client_secret": SECRET}})},
             "bare string " + "password=" + SECRET, 42, None, {"weird": {1, 2}}]
    pre(h, "r1", 1, items, instructions={"nested": ["password: " + SECRET]})
    post(h, "r1", 1)
    for bad in (None, 12, "text", {"a": 1}):
        h["pre_api_request"](api_request_id="rb", api_call_count=2, request_messages=bad, request=bad)
    h["pre_api_request"](api_request_id="rc", api_call_count=3)
    finish(mod, h)
    assert SECRET not in b"".join(plugin_env.bodies).decode()
    assert request_input(gen_spans(plugin_env)["llm_call_1"])["message_count"] == 6


def test_instructions_up_to_the_budget_are_exported_whole_and_longer_ones_are_flagged(tmp_path, plugin_env):
    """Regression: encode_attr used to re-clip the already-bounded input at the default 8,000 characters."""
    mod, h = load_plugin(tmp_path)
    policy = ("RULE: stops stay at technical levels. " * 400)[:15_000]
    pre(h, "r1", 1, [{"role": "user", "content": "go"}], instructions=policy)
    post(h, "r1", 1)
    pre(h, "r2", 2, [{"role": "user", "content": "go"}, {"role": "assistant", "content": "ok"}],
        instructions="Z" * 30_000)
    post(h, "r2", 2)
    finish(mod, h)
    gens = gen_spans(plugin_env)
    first = request_input(gens["llm_call_1"])
    assert first["system"]["content"] == policy and first["system"]["clipped"] is False and first["complete"] is True
    second = request_input(gens["llm_call_2"])
    assert second["system"]["clipped"] is True and "truncated" in second["system"]["content"]
    assert second["complete"] is False, "a clipped policy is never presented as complete"


def test_hermes_side_clipping_of_instructions_is_reported_and_not_mistaken_for_a_policy_change(tmp_path, plugin_env):
    """The pinned Hermes clips strings in its sanitised request payload (8,000 chars, then 1,000, then a preview)."""
    mod, h = load_plugin(tmp_path)
    full = "RULE: stops stay at technical levels. " * 300
    clipped8k = full[:8000] + "...[truncated %d chars]" % (len(full) - 8000)
    pre(h, "r1", 1, [{"role": "user", "content": "go"}], instructions=clipped8k)
    post(h, "r1", 1)
    pre(h, "r2", 2, [{"role": "user", "content": "go"}, {"role": "assistant", "content": "a"}],
        instructions=full[:1000] + "...[truncated %d chars]" % (len(full) - 1000))
    post(h, "r2", 2)
    h["pre_api_request"](api_request_id="r3", api_call_count=3,
                         request_messages=[{"role": "user", "content": "go"}] * 3,
                         request={"_truncated": True, "original_type": "dict", "preview": "{...}"})
    post(h, "r3", 3)
    finish(mod, h)
    gens = gen_spans(plugin_env)
    one, two, three = (request_input(gens[f"llm_call_{i}"]) for i in (1, 2, 3))
    assert one["system"]["clipped"] is True and "Hermes itself clips" in one["system"]["note"]
    assert one["complete"] is False
    assert two["system"]["prefix_of"] == "llm_call_1" and "unverified" in two["system"]["equality"], (
        "a differently clipped copy is linked to the earlier text, but never claimed identical")
    assert three["system"]["assumed"] is True and "not re-observed" in three["system"]["note"]


# -- Ticket 1 carry-forward: the diagnostic, dry-run-only drain --------------------------------------------------------

@pytest.mark.parametrize("argv", [["cycle", "--trace-drain", "2"], ["loop", "--trace-drain", "2"],
                                  ["dry-run", "--trace-drain", "11"], ["dry-run", "--trace-drain", "0"],
                                  ["setup-profile", "--trace-drain", "1"]])
def test_the_diagnostic_drain_is_refused_outside_a_bounded_dry_run(argv):
    with pytest.raises(SystemExit):
        run_live.main(argv)


def test_only_a_well_formed_drain_value_reaches_the_hermes_command_line():
    import agent
    assert agent.trace_env_flags({"HERMES_TRACE_DIAGNOSTIC_DRAIN_S": "2.5"}) == [
        "-e", "HERMES_TRACE_DIAGNOSTIC_DRAIN_S=2.5"]
    for bad in ("99", "1; rm -rf /", "-1", "2.55", ""):
        assert agent.trace_env_flags({"HERMES_TRACE_DIAGNOSTIC_DRAIN_S": bad}) == []


def test_controller_diagnostic_drain_is_bounded_and_reports_what_it_did():
    from tracing.controller import NullTracer, diagnostic_drain
    assert diagnostic_drain(NullTracer(), 5)["drain"].startswith("skipped")
    hung = Collector("hang")
    try:
        tr = Tracer(otlp.Exporter(hung.url, PK, SK, timeout_s=5))
        with tr.trace("live_cycle"):
            pass
        t0 = time.monotonic()
        r = diagnostic_drain(tr, 0.5)
        assert time.monotonic() - t0 < 0.9 and r["drain"].startswith("deadline reached") and r["limit_s"] == 0.5
        assert diagnostic_drain(tr, 999)["limit_s"] == 10.0, "capped"
    finally:
        hung.stop()


def test_plugin_drains_only_when_the_controller_asked_for_a_diagnostic_drain(tmp_path, monkeypatch):
    plug = _install_plugin(tmp_path)
    c = Collector(delay=0.3)                                   # a service with realistic latency
    try:
        base = {"HERMES_TRACE_PUBLIC_KEY": PK, "HERMES_TRACE_SECRET_KEY": SK, "HERMES_TRACE_BASE_URL": c.url}
        _, normal = _run_exit(plug, base)
        normal_names = {s["name"] for s in c.spans()}
        c.bodies.clear()
        wall, drained = _run_exit(plug, {**base, "HERMES_TRACE_DIAGNOSTIC_DRAIN_S": "3"})
        assert normal < 0.25, "without the flag, completion never waits"
        assert 0.25 <= drained < 3.5, "with it, completion waits (bounded) for the last spans"
        assert "hermes_run" in {s["name"] for s in c.spans()}, "the diagnostic drain delivered the root"
        print("survival without drain:", sorted(normal_names))
    finally:
        c.stop()
