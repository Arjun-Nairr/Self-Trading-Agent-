"""Exit spool (Tickets 2-3 batch, approved 2026-11-06): spans not acknowledged when a one-shot process exits are
written, redacted, to a local spool and sent in the background by the next invocation. Offline tests; each
"invocation" is a fresh Exporter on the same spool directory."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from tracing import otlp
from test_tracing import PK, SK

SECRET = "ordinary-sensitive-value"


class Backend:
    """Stand-in for the OTLP endpoint: records each accepted body; `status` / `gate` script outages and hangs."""

    def __init__(self, status=200, gate=None):
        self.status, self.gate, self.accepted, self.calls = status, gate, [], 0
        self.lock = threading.Lock()

    def __call__(self, body: bytes) -> int:
        with self.lock:
            self.calls += 1
        if self.gate is not None:
            self.gate.wait(30)
        status = self.status() if callable(self.status) else self.status
        if isinstance(status, Exception):
            raise status
        if 200 <= status < 300:
            with self.lock:
                self.accepted.append(json.loads(body))
        return status

    def span_ids(self):
        return [s["spanId"] for b in self.accepted for rs in b["resourceSpans"] for ss in rs["scopeSpans"]
                for s in ss["spans"]]

    def spans(self):
        return [s for b in self.accepted for rs in b["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]


def exporter(spool, post, **kw):
    return otlp.Exporter("http://127.0.0.1:9", PK, SK, service="svc", spool_dir=str(spool), post=post,
                         cooldown_s=60, **kw)


def cycle_spans(n=38, secret=False):
    """A controller-shaped trace: root last (it ends last), children before it."""
    tid, root = otlp.new_trace_id(), otlp.new_span_id()
    out = []
    for i in range(n - 1):
        inp = {"i": i, "note": f"password={SECRET}" if secret else "x"}
        out.append(otlp.span_record(tid, otlp.new_span_id(), root, f"child_{i}", time.time_ns(), time.time_ns() + 5,
                                    otlp.observation_attrs("span", input=inp)))
    out.append(otlp.span_record(tid, root, None, "live_cycle", time.time_ns(), time.time_ns() + 9,
                                otlp.observation_attrs("chain", input={"root": True}, trace_name="live_cycle")))
    return out


def files(spool):
    return sorted(p.name for p in Path(spool).glob("*")) if Path(spool).exists() else []


def run_next(spool, backend, timeout=5.0):
    """The next invocation: its first span starts the worker, which replays the spool first."""
    e = exporter(spool, backend)
    e.submit(otlp.span_record(otlp.new_trace_id(), otlp.new_span_id(), None, "next", time.time_ns(),
                              time.time_ns() + 1, otlp.observation_attrs("span")))
    assert e.flush(timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and e._replaying:
        time.sleep(0.02)
    return e


# -- survival across a normal one-shot exit -----------------------------------------------------------------------

def test_final_and_root_records_survive_a_one_shot_exit_and_replay_with_their_ids(tmp_path):
    gate = threading.Event()                                 # the network is slow: nothing is acknowledged in time
    first = Backend(gate=gate)
    e = exporter(tmp_path, first, batch_max=8)
    spans = cycle_spans()
    for s in spans:
        e.submit(s)
    time.sleep(0.1)                                          # the worker has one batch in flight, the rest queued
    t0 = time.perf_counter()
    e.close()                                                # process exit
    exit_ms = (time.perf_counter() - t0) * 1000
    assert exit_ms < 100, f"exit spool took {exit_ms:.1f} ms"
    assert e.stats["spooled"] == len(spans) and len(files(tmp_path)) == 1
    nxt = Backend()
    run_next(tmp_path, nxt)
    got = {s["spanId"]: s for s in nxt.spans()}
    for s in spans:                                          # every span, the root included, with ids unchanged
        o = got[s["span_id"]]
        assert o["traceId"] == s["trace_id"] and o.get("parentSpanId") == s["parent_id"]
    assert any(s["name"] == "live_cycle" and "parentSpanId" not in s for s in nxt.spans())
    assert files(tmp_path) == [], "deleted only after acceptance"
    gate.set()


def test_records_stay_through_an_outage_and_are_sent_once_it_ends(tmp_path):
    e = exporter(tmp_path, Backend(status=503))
    spans = cycle_spans(10)
    for s in spans:
        e.submit(s)
    assert e.flush(5)                                        # attempted and failed: kept, not dropped
    e.close()
    assert e.stats["spooled"] == 10
    before = files(tmp_path)
    run_next(tmp_path, Backend(status=lambda: ConnectionRefusedError()))   # still down at the next invocation
    assert files(tmp_path) and len(files(tmp_path)) == len(before), "a failed upload discards nothing"
    up = Backend()
    run_next(tmp_path, up)
    assert {s["span_id"] for s in spans} <= set(up.span_ids()) and files(tmp_path) == []


def test_in_flight_batch_is_spooled_without_waiting_and_a_double_send_keeps_identical_ids(tmp_path):
    gate = threading.Event()
    first = Backend(gate=gate)
    e = exporter(tmp_path, first, batch_max=64)
    spans = cycle_spans(5)
    for s in spans:
        e.submit(s)
    time.sleep(0.1)
    t0 = time.perf_counter()
    e.close()                                                # the batch is still on the wire
    assert time.perf_counter() - t0 < 0.1, "close never waits for the in-flight request"
    gate.set()                                               # ...which then succeeds after the spool was written
    time.sleep(0.2)
    nxt = Backend()
    run_next(tmp_path, nxt)
    a, b = sorted(first.span_ids()), sorted(x["spanId"] for x in nxt.spans() if x["name"] != "next")
    assert a == b == sorted(s["span_id"] for s in spans), "a re-send repeats the same ids (backend upserts by id)"
    assert first.spans() == [s for s in nxt.spans() if s["name"] != "next"], "identical content, not a new record"


# -- bounds, malformed input, failures -----------------------------------------------------------------------------

def test_the_per_exit_and_directory_bounds_hold_and_drops_are_reported(tmp_path):
    big = [otlp.span_record(otlp.new_trace_id(), otlp.new_span_id(), None, f"big{i}", 1, 2,
                            otlp.observation_attrs("span", input="z" * 60_000, input_limit=60_000)) for i in range(60)]
    e = exporter(tmp_path, Backend(gate=threading.Event()), warn_path=str(tmp_path / "w.log"), batch_max=4,
                 queue_max=512)
    for s in big:
        e.submit(s)
    e.close()
    written = sum(p.stat().st_size for p in tmp_path.glob("*.jsonl"))
    assert written <= otlp.SPOOL_EXIT_MAX
    assert e.stats["dropped_spool_full"] > 0 and e.stats["spooled"] + e.stats["dropped_spool_full"] == 60
    assert "spool" in (tmp_path / "w.log").read_text()
    for i in range(16):                                      # fill the directory to its bound
        (tmp_path / f"{i:03d}-filler.jsonl").write_bytes(b"x" * (otlp.SPOOL_EXIT_MAX // 2))
    e2 = exporter(tmp_path, Backend(gate=threading.Event()))
    e2.submit(cycle_spans(2)[0])
    e2.close()
    assert sum(p.stat().st_size for p in tmp_path.glob("*.jsonl")) <= otlp.SPOOL_DIR_MAX + otlp.SPOOL_EXIT_MAX
    assert e2.stats["dropped_spool_full"] == 1 and e2.stats["spooled"] == 0


def test_malformed_spool_lines_are_dropped_counted_and_valid_ones_still_delivered(tmp_path):
    e = exporter(tmp_path, Backend(status=503))
    spans = cycle_spans(3)
    for s in spans:
        e.submit(s)
    assert e.flush(5)
    e.close()
    f = next(tmp_path.glob("*.jsonl"))
    f.write_bytes(b"not json\n" + b'{"no": "spans"}\n' + b"\xff\xfe\n" + f.read_bytes())
    up = Backend()
    nxt = run_next(tmp_path, up)
    assert {s["span_id"] for s in spans} <= set(up.span_ids())
    assert nxt.stats["spool_invalid"] == 3 and files(tmp_path) == []


def test_a_spool_that_cannot_be_written_never_raises_or_waits_and_is_reported(tmp_path):
    blocker = tmp_path / "spool"
    blocker.write_text("a file where the directory should be")
    e = exporter(blocker, Backend(gate=threading.Event()))
    for s in cycle_spans(4):
        e.submit(s)
    t0 = time.perf_counter()
    e.close()
    assert time.perf_counter() - t0 < 0.1
    assert e.stats["spool_write_failed"] == 4 and e.stats["spooled"] == 0


def test_no_credentials_or_unredacted_secrets_reach_the_spool(tmp_path):
    e = exporter(tmp_path, Backend(gate=threading.Event()))
    for s in cycle_spans(6, secret=True):
        e.submit(s)
    e.close()
    data = b"".join(p.read_bytes() for p in tmp_path.glob("*"))
    assert data and SECRET.encode() not in data and PK.encode() not in data and SK.encode() not in data
    assert b"Authorization" not in data and b"Basic " not in data
    if os.name == "posix":
        assert all((p.stat().st_mode & 0o077) == 0 for p in tmp_path.glob("*.jsonl")), "owner-only files"


# -- concurrency -----------------------------------------------------------------------------------------------------

def test_two_invocations_replaying_the_same_spool_never_send_a_file_twice(tmp_path):
    for _ in range(6):                                       # six earlier exits, six files
        e = exporter(tmp_path, Backend(status=503))
        for s in cycle_spans(4):
            e.submit(s)
        assert e.flush(5)
        e.close()
    assert len(files(tmp_path)) == 6
    shared = Backend(status=lambda: (time.sleep(0.01), 200)[1])
    a = threading.Thread(target=run_next, args=(tmp_path, shared))
    b = threading.Thread(target=run_next, args=(tmp_path, shared))
    a.start(), b.start(), a.join(30), b.join(30)
    ids = [i for i in shared.span_ids()]
    replayed = [i for s, i in zip(shared.spans(), ids, strict=True) if s["name"] != "next"]
    assert len(replayed) == 24 and len(set(replayed)) == 24, "each spooled span sent exactly once here"
    assert files(tmp_path) == []


def test_exit_during_replay_hands_the_unsent_rest_back_instead_of_losing_it(tmp_path):
    e = exporter(tmp_path, Backend(status=503), batch_max=1)
    spans = cycle_spans(6)
    for s in spans:
        e.submit(s)
    assert e.flush(5)
    e.close()
    gate = threading.Event()
    slow = Backend(gate=gate)
    e2 = exporter(tmp_path, slow)
    e2.submit(cycle_spans(1)[0])
    deadline = time.monotonic() + 5
    while not any(".claimed-" in f for f in files(tmp_path)) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert any(".claimed-" in f for f in files(tmp_path))
    e2.close()                                               # exits while the first replayed line is on the wire
    gate.set()
    assert not any(".claimed-" in f for f in files(tmp_path)), "the claim is released"
    up = Backend()
    run_next(tmp_path, up)
    assert {s["span_id"] for s in spans} <= set(up.span_ids() + slow.span_ids())
    assert files(tmp_path) == []


def test_a_claim_left_by_a_crashed_process_is_recovered_after_it_goes_stale(tmp_path):
    e = exporter(tmp_path, Backend(status=503))
    spans = cycle_spans(3)
    for s in spans:
        e.submit(s)
    assert e.flush(5)
    e.close()
    f = next(tmp_path.glob("*.jsonl"))
    crashed = f.with_name(f.name + ".claimed-99999-dead")
    f.rename(crashed)
    up = Backend()
    run_next(tmp_path, up)
    assert crashed.exists() and not up.span_ids()[:-1], "a fresh claim belongs to a live process: left alone"
    old = time.time() - otlp.SPOOL_STALE_CLAIM_S - 5
    os.utime(crashed, (old, old))
    up2 = Backend()
    run_next(tmp_path, up2)
    assert {s["span_id"] for s in spans} <= set(up2.span_ids()) and files(tmp_path) == []


@pytest.mark.parametrize("n", [38, 512])
def test_exit_overhead_is_a_bounded_local_write(tmp_path, n):
    e = exporter(tmp_path, Backend(gate=threading.Event()), queue_max=512)
    for s in cycle_spans(n):
        e.submit(s)
    t0 = time.perf_counter()
    e.close()
    ms = (time.perf_counter() - t0) * 1000
    print(f"exit spool of {n} spans: {ms:.1f} ms, {sum(p.stat().st_size for p in tmp_path.glob('*.jsonl'))} bytes")
    assert ms < (100 if n == 38 else 600)


# -- wiring: the controller and the short-lived Hermes process ------------------------------------------------------

def test_controller_tracer_spools_at_close_and_the_next_invocation_sends_the_root(tmp_path, collector):
    from tracing.controller import load_tracer
    from test_tracing import dead_url
    (tmp_path / "tracing.env").write_text(f"LANGFUSE_PUBLIC_KEY={PK}\nLANGFUSE_SECRET_KEY={SK}\n"
                                          f"LANGFUSE_BASE_URL={dead_url()}\n")
    tr = load_tracer(tmp_path)
    with tr.trace("live_cycle") as root:
        with tr.span("reconcile"):
            pass
    tid = root.trace_id
    tr.close()                                               # one-shot exit with the backend down
    assert list((tmp_path / "trace_spool").glob("*.jsonl"))
    (tmp_path / "tracing.env").write_text(f"LANGFUSE_PUBLIC_KEY={PK}\nLANGFUSE_SECRET_KEY={SK}\n"
                                          f"LANGFUSE_BASE_URL={collector.url}\n")
    nxt = load_tracer(tmp_path)
    with nxt.trace("live_cycle"):
        pass
    assert nxt.exporter.flush(5)
    got = [s for s in collector.spans() if s["traceId"] == tid]
    assert {s["name"] for s in got} == {"live_cycle", "reconcile"}
    assert not list((tmp_path / "trace_spool").glob("*.jsonl"))


def test_hermes_process_spools_its_run_at_exit_and_the_next_hermes_call_sends_it(tmp_path, monkeypatch, collector):
    from test_tracing import api_post, api_pre, dead_url, load_plugin
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TRACE_PUBLIC_KEY", PK)
    monkeypatch.setenv("HERMES_TRACE_SECRET_KEY", SK)
    monkeypatch.setenv("HERMES_TRACE_BASE_URL", dead_url())
    mod, h = load_plugin(tmp_path, name="hermes_trace_a", drain=False)
    api_pre(h, "r1", 1)
    api_post(h, "r1", 1, content="done")
    h["post_llm_call"](turn_id="t1")                         # Hermes finishes and exits at once
    tid = mod._RUN.trace_id
    assert list((home / "trace_spool").glob("*.jsonl")), "unsent Hermes spans kept locally"
    monkeypatch.setenv("HERMES_TRACE_BASE_URL", collector.url)
    mod2, h2 = load_plugin(tmp_path, name="hermes_trace_b", drain=False)
    api_pre(h2, "r1", 1)
    api_post(h2, "r1", 1, content="ok")                      # its first span starts the worker, which replays first
    assert mod2._run().exporter.flush(5)
    got = {s["name"] for s in collector.spans() if s["traceId"] == tid}
    assert {"hermes_run", "llm_call_1"} <= got
