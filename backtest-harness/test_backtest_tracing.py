"""Backtest Langfuse tracing (backtest_tracing.py): optional, export-only, fail-open, identity-free.
Offline: the real run_day with only I/O mocked (test_laya_wiring's harness) and a recording fake endpoint.
Run: python -m pytest -q test_backtest_tracing.py
"""
import dataclasses
import json
import os
import re
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import backtest_tracing as bt
import run_backtest as rb
import test_laya_wiring as tw
from run_backtest import Holding, Portfolio, _names_for_masking
from tracing import controller, otlp

RUN_KEY = "11111111-2222-4333-8444-555555555555"
NAMES = _names_for_masking(tw.WATCHLIST)


class Backend:
    """Recording stand-in for the OTLP endpoint (status scripts outages; 'raise' raises)."""

    def __init__(self, status=200):
        self.status, self.bodies, self.lock = status, [], threading.Lock()

    def __call__(self, body: bytes) -> int:
        if self.status == "raise":
            raise ConnectionRefusedError("down")
        with self.lock:
            self.bodies.append(body)
        return self.status

    def spans(self):
        return [s for b in self.bodies for rs in json.loads(b)["resourceSpans"] for ss in rs["scopeSpans"]
                for s in ss["spans"]]


def env_dir(tmp_path, name="bt"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "tracing.env").write_text("LANGFUSE_PUBLIC_KEY=pk-lf-test-0000\nLANGFUSE_SECRET_KEY=sk-lf-test-1111\n"
                                   "LANGFUSE_BASE_URL=http://127.0.0.1:9\n")
    return d


def session(tmp_path, masking, backend, **kw):
    tracer, status = bt.load(RUN_KEY, masking, trace_dir=env_dir(tmp_path), post=backend)
    assert status == "on", status
    if kw:
        for k, v in kw.items():
            setattr(tracer.exporter, k, v)
    return bt.TraceSession(rb, tracer, meta={"run_kind": "smoke"})


def finish(s, deliver=True):
    """TEST ONLY: let the background sender deliver before close() (which would spool the rest)."""
    if deliver:
        assert s.tracer.exporter.flush(5)
    s.close()


def scenario(tmp, run_day_fn=None, buy_qty=10):
    """Held OLN + a SELL of it and a BUY of a selected name, with real Laya selection over a fake scorer."""
    m = tw._masking()
    probe = tw._ctx(tmp / "probe").select_for_day(tw.DAY, {"OLN"})
    pick = next(t for t in probe.selected if t != "OLN")
    held = Portfolio(cash=90_000.0, equity_curve=[100_000.0] * 6, trading_days_elapsed=3,
                     holdings={"OLN": Holding("OLN", 100, 100.0, 95.0, 120.0, 28, 2)})
    sell = {"alias": m.alias("OLN"), "action": "SELL", "quantity": 100, "entry_price": 100.0, "confidence": 3,
            "primary_signal": "technical", "technical_basis": "rsi", "invalidation_condition": "n/a",
            "reasoning": "thesis broke"}
    buy = {"alias": m.alias(pick), "action": "BUY", "quantity": buy_qty, "entry_price": 100.0, "stop_price": 95.0,
           "target_price": 110.0, "confidence": 3, "primary_signal": "technical", "technical_basis": "rsi",
           "expected_reward_risk": 2.0, "invalidation_condition": "closes below $95", "reasoning": "x"}
    if buy_qty is None:
        del buy["quantity"]                               # prompt schema 5: a BUY need not send a quantity
    ctx = tw._ctx(tmp / "snap")
    if run_day_fn is None:
        cap = tw._cycle(tmp, ctx, portfolio=held, decisions=[sell, buy], masking=m, run_key=RUN_KEY,
                        checkpoint_dir=tmp)
    else:
        with patch("test_laya_wiring.run_day", run_day_fn):
            cap = tw._cycle(tmp, ctx, portfolio=held, decisions=[sell, buy], masking=m, run_key=RUN_KEY,
                            checkpoint_dir=tmp)
    snaps = {p.name: p.read_bytes() for p in sorted((tmp / "snap").rglob("*")) if p.is_file()}
    keep = {k: cap[k] for k in ("prompts", "tech_calls", "news_calls", "evidence", "queued", "sim_watchlists")}
    keep.update(rejections=cap.get("rejections"), fills=cap.get("fills"), exits=cap.get("exits"),
                working=dataclasses.asdict(cap["working"]), summary=cap["summary"], snapshots=snaps)
    return json.loads(json.dumps(keep, default=str)), m


# -- 1. parity ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["healthy", "raising", "outage", "full_queue"])
def test_tracing_on_or_failing_never_changes_trading_outputs_or_persisted_state(tmp_path, mode):
    base, m = scenario(tmp_path / "off")
    again, _ = scenario(tmp_path / "off2")
    assert base == again, "the scenario itself is deterministic"
    backend = Backend({"healthy": 200, "raising": "raise", "outage": 503, "full_queue": 200}[mode])
    s = session(tmp_path / mode, m, backend, **({"queue_max": 1} if mode == "full_queue" else {}))
    try:
        traced, _ = scenario(tmp_path / f"on-{mode}", s._traced_run_day)
    finally:
        finish(s, deliver=mode == "healthy")
    assert traced == base
    assert rb.run_day is s._run_day, "run_day restored"
    assert not hasattr(rb.fetch_bars, "__wrapped__") and not hasattr(rb.check_trade, "__wrapped__")
    if mode == "healthy":
        names = [x["name"] for x in backend.spans()]
        for n in ("backtest_day", "selection.laya", "laya.fetch_history", "laya.inference", "prompt_assembly",
                  "risk_check", "sizing_check", "controller.fetch.technicals", "controller.fetch.news",
                  "journal.evidence"):
            assert n in names, (n, sorted(set(names)))
        sizing = next(x for x in backend.spans() if x["name"] == "sizing_check")
        out = json.loads({a["key"]: a["value"] for a in sizing["attributes"]}[
            "langfuse.observation.output"]["stringValue"])
        assert out["outcome"] == "sized" and out["computed_quantity"] > 0, out


def _attrs(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


def _io(span, which="output"):
    v = _attrs(span).get(f"langfuse.observation.{which}")
    return json.loads(v["stringValue"]) if v else None


def _traced_scenario(tmp, buy_qty, backend=None):
    plain, m = scenario(tmp / "off", buy_qty=buy_qty)
    backend = backend or Backend()
    s = session(tmp / "tr", m, backend)
    try:
        traced, _ = scenario(tmp / "on", s._traced_run_day, buy_qty=buy_qty)
    finally:
        finish(s, deliver=backend.status == 200)
    assert traced == plain, "trading outputs and persisted state identical with tracing on"
    return traced, backend.spans()


def test_sizing_check_records_controller_ownership_inputs_and_outcome(tmp_path):
    """Real run_day path: the controller sizes the BUY; Hermes' quantity (10, 9999 or none) never changes it."""
    results = {}
    for qty in (10, 9999, None):
        out, spans = _traced_scenario(tmp_path / f"q{qty}", qty)
        sizing = [x for x in spans if x["name"] == "sizing_check"]
        assert len(sizing) == 1
        sin, sout = _io(sizing[0], "input"), _io(sizing[0])
        assert sin["owner"] == bt.SIZING_OWNER and sin["entry_price"] == 100.0 and sin["stop_price"] == 95.0
        assert {"equity", "remaining_cash", "risk_budget_pct", "max_position_pct"} <= set(sin)
        assert "proposed_quantity" not in sin, "Hermes' number is not a sizing input"
        assert sout["outcome"] == "sized" and isinstance(sout["computed_quantity"], int)
        buy = [o for o in out["working"]["pending_orders"] if o["action"] == "BUY"]
        assert [o["quantity"] for o in buy] == [sout["computed_quantity"]]
        root = _io(next(x for x in spans if x["name"] == "backtest_day"))
        assert "proposals" not in root and [p["quantity"] for p in root["queued_proposals"]
                                            if p["action"] == "BUY"] == [sout["computed_quantity"]]
        line = next(d for d in root["decisions"] if d.startswith("BUY"))
        assert ("ignored" in line) is (qty is not None) and "controller-sized" in line
        # the rule check runs on the controller quantity and is labelled as not final
        buy_check = next(x for x in spans if x["name"] == "risk_check" and _io(x, "input")["action"] == "BUY")
        assert _io(buy_check, "input")["quantity"] == sout["computed_quantity"]
        assert _io(buy_check) == {"check_trade_allowed": True, "violations": []}
        assert _io(buy_check, "input")["stage"] == bt.DECISION_STAGE and "not final" in bt.DECISION_STAGE
        assert buy_check["parentSpanId"] == sizing[0]["parentSpanId"], "siblings under the same day"
        results[qty] = {k: v for k, v in out.items() if k not in ("summary", "prompts", "evidence")}
    assert results[10] == results[9999] == results[None], "Hermes' BUY quantity changes nothing it trades"


def test_a_skipped_day_is_labelled_skipped_not_an_empty_session(tmp_path):
    def holiday(day, day_index, portfolio, masking, *a, **kw):
        return portfolio, {"skipped": "no market data (confirmed holiday/weekend via trading calendar)"}, lambda: None
    backend = Backend()
    with patch("run_backtest.run_day", holiday):
        s = session(tmp_path, tw._masking(), backend)
        s._traced_run_day(tw.DAY, 3, Portfolio(), tw._masking(), [], None, RUN_KEY)
        finish(s)
    root = next(x for x in backend.spans() if x["name"] == "backtest_day")
    assert _io(root) == {"skipped": "no market data (confirmed holiday/weekend via trading calendar)"}


def test_sizing_and_risk_spans_fail_open_when_the_exporter_raises(tmp_path):
    _traced_scenario(tmp_path / "boom", 10, Backend("raise"))


# -- episode-end resolution (eval_suite.resolve_episode_end_pending) -------------------------------------------------

def _pending(m, n):
    from run_backtest import QueuedOrder
    return [QueuedOrder(ticker="OLN", alias=m.alias("OLN"), action="BUY", quantity=10 + i, stop_price=90.0,
                        target_price=110.0, entry_price=100.0, queued_day_index=4, decision={},
                        decision_id=f"dec-{i}") for i in range(n)]


def _episode_end(tmp, n, journal, backend=None):
    import eval_suite as es
    m = tw._masking()
    held = {"OLN": Holding("OLN", 5, 100.0, 95.0, 120.0, 28, 2)}
    calls_plain, calls_traced = [], []

    def fake(log):
        def f(decision_id, reason):
            log.append((decision_id, reason))
            journal(decision_id)
            return {"status": "rejected", "applied": True}       # the journal's reply for a still-pending row
        return f
    p1 = Portfolio(cash=1.0, holdings=dict(held), pending_orders=_pending(m, n))
    with patch("eval_suite.resolve_trade_rejection", side_effect=fake(calls_plain)):
        plain = es.resolve_episode_end_pending(p1)
    backend = backend or Backend()
    s = session(tmp, m, backend)
    assert es.resolve_episode_end_pending is not s._episode_end_fn, "installed while the session is open"
    p2 = Portfolio(cash=1.0, holdings=dict(held), pending_orders=_pending(m, n))
    try:
        with patch("eval_suite.resolve_trade_rejection", side_effect=fake(calls_traced)):
            traced = es.resolve_episode_end_pending(p2)
    finally:
        finish(s, deliver=backend.status == 200)
    assert es.resolve_episode_end_pending is s._episode_end_fn, "restored on close"
    assert traced == plain and calls_traced == calls_plain
    assert dataclasses.asdict(p2) == dataclasses.asdict(p1) and p2.pending_orders == []
    return backend.spans(), m


def test_episode_end_records_each_pending_orders_actual_outcome_under_its_own_root(tmp_path):
    spans, m = _episode_end(tmp_path, 2, journal=lambda d: None)
    root = next(x for x in spans if x["name"] == "backtest_episode_end")
    kids = [x for x in spans if x["name"] == "journal.rejection"]
    assert len(kids) == 2 and all(k["parentSpanId"] == root["spanId"] for k in kids)
    assert all(k["traceId"] == root["traceId"] for k in kids)
    a = _attrs(root)
    assert a["langfuse.environment"]["stringValue"] == "backtest"
    assert a["langfuse.session.id"]["stringValue"] == RUN_KEY
    out = _io(root)
    assert out["resolution"] == bt.EPISODE_END_REASON and out["resolved"] == 2 and out["open_positions_kept"] == 1
    assert [o["outcome"] for o in out["orders"]] == ["rejected; journal row written"] * 2
    assert {o["ticker"] for o in out["orders"]} == {m.alias("OLN")}, "alias only"
    assert not _leaks(json.dumps(spans)), _leaks(json.dumps(spans))


def test_episode_end_with_nothing_pending_is_recorded_as_such(tmp_path):
    spans, _ = _episode_end(tmp_path, 0, journal=lambda d: None)
    out = _io(next(x for x in spans if x["name"] == "backtest_episode_end"))
    assert out["resolution"] == "no pending orders" and out["resolved"] == 0 and out["orders"] == []
    assert not [x for x in spans if x["name"] == "journal.rejection"]


def test_episode_end_shows_a_swallowed_journal_failure_and_stays_fail_open(tmp_path):
    def broken(decision_id):
        raise RuntimeError("journal down for OLN")
    spans, _ = _episode_end(tmp_path / "a", 1, journal=broken)
    out = _io(next(x for x in spans if x["name"] == "backtest_episode_end"))
    assert out["orders"][0]["outcome"] == "journal write failed (RuntimeError)"
    assert not _leaks(json.dumps(spans))
    _episode_end(tmp_path / "b", 1, journal=lambda d: None, backend=Backend("raise"))


def test_hermes_calls_retries_validation_and_usage_are_recorded_and_the_result_is_unchanged(tmp_path):
    replies = [b'{"decisions": "not a list"}', b'{"decisions": [], "no_action_reason": "nothing today"}']
    usage = {"input_tokens": 900, "output_tokens": 40, "total_tokens": 940, "cache_read_tokens": 100,
             "reasoning_tokens": 7, "api_calls": 1, "model": "gpt-test", "provider": "prov",
             "estimated_cost_usd": 0.5}

    def fake_call(prompt, runtime=None):
        return replies.pop(0), dict(usage)

    with patch("run_backtest._call_hermes", side_effect=fake_call):
        plain = rb.ask_hermes("PROMPT KV42", {})
    replies[:] = [b'{"decisions": "not a list"}', b'{"decisions": [], "no_action_reason": "nothing today"}']
    backend = Backend()
    s = session(tmp_path, tw._masking(), backend)
    with patch("run_backtest._call_hermes", side_effect=fake_call), s._installed():
        with s.tracer.trace("backtest_day"):
            traced = rb.ask_hermes("PROMPT KV42", {})
    finish(s)
    assert dataclasses.asdict(traced) == dataclasses.asdict(plain)
    by = {x["name"]: x for x in backend.spans()}
    attrs = {k: {a["key"]: a["value"] for a in v["attributes"]} for k, v in by.items()}
    assert {"hermes_decision", "hermes_call_1", "hermes_call_2", "response_validation"} <= set(by)
    g = attrs["hermes_call_2"]
    assert g["langfuse.observation.type"]["stringValue"] == "generation"
    assert json.loads(g["langfuse.observation.usage_details"]["stringValue"]) == {
        "input": 900, "output": 40, "total": 940, "cache_read_input_tokens": 100, "reasoning_tokens": 7}
    assert g["langfuse.observation.model.name"]["stringValue"] == "gpt-test"
    assert "0.5" not in json.dumps(g), "no cost exported"
    assert by["hermes_call_1"]["parentSpanId"] == by["hermes_decision"]["spanId"]
    assert attrs["hermes_decision"]["langfuse.environment"]["stringValue"] == "backtest"
    assert attrs["hermes_decision"]["langfuse.session.id"]["stringValue"] == RUN_KEY


# -- 2. directory boundary -----------------------------------------------------------------------------------------

def test_the_live_state_directory_and_the_repository_are_refused(tmp_path, monkeypatch):
    live = tmp_path / "live"
    monkeypatch.setattr(bt, "LIVE_STATE_DIR", live)
    for d in (live, live / "sub", bt.REPO / "backtest-harness" / ".trace"):
        tr, status = bt.load(RUN_KEY, tw._masking(), trace_dir=d)
        assert not tr.enabled and "refused" in status, (d, status)
    assert bt.trace_dir_problem(bt.DEFAULT_DIR) is None


def test_backtest_tracing_never_replays_the_live_spool(tmp_path, monkeypatch):
    live = tmp_path / "live"
    (live / "trace_spool").mkdir(parents=True)
    body = json.dumps(otlp.to_otlp([otlp.span_record("a" * 32, "b" * 16, None, "LIVE", 1, 2, {})], "x")).encode()
    live_file = live / "trace_spool" / "00000000000000000001-1-live.jsonl"
    live_file.write_bytes(body + b"\n")
    monkeypatch.setattr(bt, "LIVE_STATE_DIR", live)
    backend = Backend()
    s = session(tmp_path, tw._masking(), backend)
    with s.tracer.trace("backtest_day"):
        pass
    assert s.tracer.exporter.flush(5)
    s.close()
    assert live_file.read_bytes() == body + b"\n", "the live spool file is untouched"
    assert "LIVE" not in [x["name"] for x in backend.spans()]
    assert Path(s.tracer.exporter.spool_dir).parent == tmp_path / "bt"


# -- 3. Hermes profile untouched -------------------------------------------------------------------------------------

def test_no_plugin_credential_or_tool_reaches_the_backtest_hermes_profile(tmp_path):
    runtime = rb.HermesRuntime(name="btrun-x", home="/opt/data/profiles/btrun-x",
                               workdir="/opt/data/profiles/btrun-x/workspace", fingerprint="f")
    calls = []

    class R:
        returncode, stdout, stderr = 0, b'{"decisions": []}', b""

    def fake_run(cmd, **kw):
        cmd = [re.sub(r"hermes_usage_[0-9a-f]+", "hermes_usage_<id>", str(c)) for c in cmd]  # random per call
        calls.append((cmd, {k: v for k, v in kw.items() if k != "input"}))
        r = R()
        if "cat" in cmd:
            r.stdout = '{"input_tokens": 1}'
        return r

    with patch("run_backtest.subprocess.run", side_effect=fake_run):
        rb._call_hermes("PROMPT", runtime)
    plain = list(calls)
    calls.clear()
    s = session(tmp_path, tw._masking(), Backend())
    with patch("run_backtest.subprocess.run", side_effect=fake_run), s._installed():
        rb._call_hermes("PROMPT", runtime)
    s.close()
    assert calls == plain, "identical docker commands: no extra env, flag, file or profile change"
    blob = json.dumps(calls, default=str)
    assert "LANGFUSE" not in blob and "HERMES_TRACE" not in blob and "pk-lf" not in blob and "plugins" not in blob
    assert all("env" not in kw for _, kw in calls), "the subprocess environment is not modified"
    assert not any(k.startswith("LANGFUSE") for k in os.environ)
    src = Path(bt.__file__).read_text(encoding="utf-8")
    assert "hermes_plugin" not in src and "profile create" not in src and "HERMES_HOME" not in src


# -- 4. identities -----------------------------------------------------------------------------------------------------

def _leaks(data: str) -> list[str]:
    found = []
    for ticker, variants in NAMES.items():
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", data):
            found.append(ticker)
        for v in variants:
            if len(v) > 3 and re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", data):
                found.append(v)
    return found


def test_exported_and_spooled_payloads_carry_no_real_identity_or_alias_mapping(tmp_path):
    m = tw._masking()
    assert _leaks(json.dumps(NAMES)), "the detector itself finds real identities"
    backend = Backend()
    s = session(tmp_path / "ok", m, backend)
    try:
        scenario(tmp_path / "a", s._traced_run_day)
        boom = RuntimeError(f"provider error for {next(iter(NAMES))}: {NAMES[next(iter(NAMES))][0]} timed out")
        with s.tracer.trace("backtest_day"), pytest.raises(RuntimeError):
            with s.tracer.span("controller.fetch.news"):
                raise boom
    finally:
        finish(s)
    assert backend.bodies
    sent = b"".join(backend.bodies).decode("utf-8")
    assert not _leaks(sent), _leaks(sent)[:5]
    assert json.dumps(m.mapping()) not in sent
    down = Backend(503)
    s2 = session(tmp_path / "down", m, down)
    try:
        scenario(tmp_path / "b", s2._traced_run_day)
    finally:
        s2.close()
    spooled = b"".join(p.read_bytes() for p in (tmp_path / "down" / "bt" / "trace_spool").glob("*.jsonl"))
    assert spooled and not _leaks(spooled.decode("utf-8"))
    warn = tmp_path / "down" / "bt" / "tracing_warnings.log"
    assert not warn.exists() or not _leaks(warn.read_text(encoding="utf-8"))


def test_decision_ids_are_exported_only_as_keyed_refs_and_the_run_id_is_kept(tmp_path):
    """uuid5(run, day, REAL ticker, ...) next to an alias would let a repo holder rebuild the mapping."""
    m = tw._masking()
    real_id = rb.make_decision_id(RUN_KEY, 4, "OLN", "BUY", "queued_proposal[0]")
    backend = Backend()
    s = session(tmp_path, m, backend)
    for _ in range(2):
        with s.tracer.trace("backtest_day"), s.tracer.span("journal.queued_decision",
                                                            input={"decision_id": real_id, "alias": m.alias("OLN")}):
            pass
    finish(s)
    sent = b"".join(backend.bodies).decode("utf-8")
    assert real_id not in sent and RUN_KEY in sent
    refs = [_io(x, "input")["decision_id"] for x in backend.spans() if x["name"] == "journal.queued_decision"]
    assert refs[0] == refs[1] and refs[0].startswith("ref-"), "stable, so queued and resolved rows still link"
    key = tmp_path / "bt" / "decision_ref.key"
    assert len(key.read_bytes()) == 32 and key.read_bytes().hex() not in sent
    assert bt.ref_key(tmp_path / "bt") == key.read_bytes(), "created once, reused"


def test_a_record_that_cannot_be_scrubbed_is_dropped_not_sent(tmp_path):
    backend = Backend()
    s = session(tmp_path, tw._masking(), backend)
    s.tracer.scrubber = None                              # any scrub failure
    with s.tracer.trace("backtest_day"):
        pass
    s.close()
    assert s.tracer.dropped_unscrubbable == 1 and not backend.spans()


# -- 5. live defaults -------------------------------------------------------------------------------------------------

def test_live_defaults_are_unchanged():
    body = otlp.to_otlp([otlp.span_record("a" * 32, "b" * 16, None, "s", 1, 2, {})], "svc")
    assert body["resourceSpans"][0]["scopeSpans"][0]["scope"]["name"] == "hermes-trading-agent.live"
    assert otlp.Exporter("http://x", "pk-lf-a", "sk-lf-b").scope == "hermes-trading-agent.live"
    t = controller.Tracer(otlp.Exporter("http://x", "pk-lf-a", "sk-lf-b", post=lambda b: 200))
    assert t.environment == "live-paper" and t.session.startswith("live-")
    seen = []
    t._emit = seen.append
    with t.trace("live_cycle"):
        pass
    assert "langfuse.observation.usage_details" not in seen[0]["attrs"], "no usage unless a span sets it"


def test_tracing_is_off_without_credentials(tmp_path):
    tr, status = bt.load(RUN_KEY, tw._masking(), trace_dir=tmp_path / "empty")
    assert not tr.enabled and "no tracing.env" in status
    sess, status = bt.start(rb, run_id=RUN_KEY, masking=tw._masking(), trace_dir=tmp_path / "empty")
    assert sess is None and rb.run_day.__name__ == "run_day"


# -- evaluation runner (eval_suite.run_configuration -> _run_episode) ------------------------------------------------

def _spans_from(backend, spool_dir):
    bodies = list(backend.bodies)
    for f in sorted(Path(spool_dir).glob("*.jsonl")) if Path(spool_dir).exists() else []:
        bodies += [ln for ln in f.read_bytes().splitlines() if ln.strip()]
    return [sp for b in bodies for rs in json.loads(b)["resourceSpans"] for ss in rs["scopeSpans"]
            for sp in ss["spans"]]


def _manifest():
    """Built once per test: approval stamps a time, so each call is a different (equally valid) manifest."""
    import eval_suite as es
    import test_eval_suite as te
    from datetime import date as _date
    manifest = te._approve(te._propose(seed=307))
    monday = _date(2023, 1, 2)
    manifest["episodes"] = [{"year": 2023, "month": 1, "regime": "rally", "start": monday.isoformat(),
                             "end": monday.isoformat(), "watchlist_viability": te._valid_watchlist_viability()}]
    manifest["content_hash"] = es.compute_content_hash(manifest)
    return manifest


def _eval_episode(tmp_path, monkeypatch, manifest, *, pending, journal_fails=False, mode="off"):
    """The REAL evaluation orchestration (manifest approval, run registration, isolated checkpoint, day loop,
    episode-end resolution) with only run_day and the journal mocked. mode: off | on | raising."""
    import eval_suite as es
    import test_eval_suite as te
    from run_backtest import QueuedOrder

    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]

    def fake_run_day(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                     fundamentals_enabled=False, runtime=None):
        if pending:
            portfolio.pending_orders.append(QueuedOrder(
                ticker="OLN", alias=masking.alias("OLN"), action="BUY", quantity=10, stop_price=90.0,
                target_price=110.0, entry_price=100.0, queued_day_index=day_index, decision={},
                decision_id=rb.make_decision_id(run_id, day_index, "OLN", "BUY", "queued_proposal[0]")))
        return portfolio, te._base_summary(day), (lambda: None)

    journal, registered = [], []

    def rejection(decision_id, reason):
        journal.append(reason)
        if journal_fails:
            raise RuntimeError("journal down for OLN Olin Corporation")
        return {"status": "rejected", "applied": True}

    backend = Backend("raise" if mode == "raising" else 200)
    trace_dir = tmp_path / "bt"
    if mode != "off":
        env_dir(tmp_path)
        monkeypatch.setattr(bt, "DEFAULT_DIR", trace_dir)
        real_load = bt.__dict__.get("_unpatched_load", bt.load)      # never wrap an earlier wrapper
        monkeypatch.setattr(bt, "_unpatched_load", real_load, raising=False)
        monkeypatch.setattr(bt, "load", lambda *a, **kw: real_load(*a, **{**kw, "post": backend}))
    original_end = es.resolve_episode_end_pending
    with patch("eval_suite.run_day", side_effect=fake_run_day) as fake, \
         patch("eval_suite.register_run", side_effect=lambda rid, kind: registered.append(rid)), \
         patch("eval_suite.resolve_trade_rejection", side_effect=rejection):
        report = es.run_configuration(manifest, es.EpisodeConfig("baseline"), es.EVAL_RUN_KIND, watchlist,
                                      tmp_path / "ckpt")
        assert es.run_day is fake, "the runner's run_day is restored"
    assert es.resolve_episode_end_pending is original_end, "episode-end helper restored"
    ep = report["per_episode"][0]
    stripped = {k: v for k, v in ep.items() if k not in ("run_id", "provenance")}
    stripped["provenance"] = {k: v for k, v in ep["provenance"].items() if k != "run_id"}
    return stripped, journal, registered[0], _spans_from(backend, trace_dir / "trace_spool")


@pytest.mark.parametrize("pending,journal_fails", [(True, False), (False, False), (True, True)])
def test_evaluation_episodes_trace_days_and_episode_end_and_tracing_never_changes_results(
        tmp_path, monkeypatch, pending, journal_fails):
    manifest = _manifest()
    off, j_off, _, none = _eval_episode(tmp_path / "off", monkeypatch, manifest, pending=pending,
                                        journal_fails=journal_fails)
    assert not none, "no tracing.env: nothing exported"
    on, j_on, run_id, spans = _eval_episode(tmp_path / "on", monkeypatch, manifest, pending=pending,
                                            journal_fails=journal_fails, mode="on")
    boom, j_boom, _, _ = _eval_episode(tmp_path / "boom", monkeypatch, manifest, pending=pending,
                                       journal_fails=journal_fails, mode="raising")
    assert off == on == boom and j_off == j_on == j_boom, "identical results with tracing off, on or failing"
    assert list((tmp_path / "boom" / "bt" / "trace_spool").glob("*.jsonl")), "the failing tracer was live (spooled)"
    assert off["dangling_pending_orders_resolved"] == (1 if pending else 0)

    assert {_attrs(x)["langfuse.session.id"]["stringValue"] for x in spans} == {run_id}
    assert {_attrs(x)["langfuse.environment"]["stringValue"] for x in spans} == {"backtest"}
    day = [x for x in spans if x["name"] == "backtest_day"]
    end = [x for x in spans if x["name"] == "backtest_episode_end"]
    assert len(day) == 1 and len(end) == 1 and day[0]["traceId"] != end[0]["traceId"]
    assert "eval_suite._run_episode" in json.dumps(_attrs(day[0])), "runner recorded in the day's metadata"
    out = _io(end[0])
    kids = [x for x in spans if x["name"] == "journal.rejection"]
    if not pending:
        assert out["resolution"] == "no pending orders" and out["orders"] == [] and not kids
    else:
        assert len(kids) == 1 and kids[0]["parentSpanId"] == end[0]["spanId"]
        want = "journal write failed (RuntimeError)" if journal_fails else "rejected; journal row written"
        assert [o["outcome"] for o in out["orders"]] == [want]
        assert out["orders"][0]["decision_id"].startswith("ref-"), "no raw uuid5 decision id"
    assert not _leaks(json.dumps(spans)), _leaks(json.dumps(spans))
