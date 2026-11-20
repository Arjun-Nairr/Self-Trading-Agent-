"""Ticket 2: complete live-pipeline tracing -- controller operations, Laya selection and the weekly screen.

OFFLINE EVIDENCE: fake broker/journal (test_live's model of Alpaca), fake Laya scorer and history, fake screen
providers. These branches (order lifecycle, screening, inference) are not exercised by the read-only dry run."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_live as tl  # noqa: E402
from run_live import ReadOnlyBroker, run_cycle  # noqa: E402
from tracing import otlp  # noqa: E402
from tracing.controller import Tracer  # noqa: E402
from test_tracing import exporter  # noqa: E402  (fixtures in conftest)


def traced(collector_):
    return Tracer(exporter(collector_.url))


def spans_of(c, tr):
    assert tr.exporter.flush(5)
    out = []
    for s in c.spans():
        s["in"] = json.loads(s["a"]["langfuse.observation.input"]) if "langfuse.observation.input" in s["a"] else None
        raw_out = s["a"].get("langfuse.observation.output")
        s["out"] = json.loads(raw_out) if raw_out is not None else None
        out.append(s)
    return out


def named(spans, prefix):
    return [s for s in spans if s["name"].startswith(prefix)]


def entry_cycle(store, b, j, tr, ticker="WMG"):
    def sel(today, session, excl):
        return [{"ticker": ticker, "sector": "", "name": ""}], "ok", {"universe": list(tl.WL)}
    reply = json.dumps({"holdings": [], "entries": [tl.plan(ticker=ticker)]})
    return run_cycle(store, b, j, review=True, selector_fn=sel, hermes_fn=lambda p: (reply, "sha"),
                     sleep=tl.NOSLEEP, tracer=tr)


# -- controller: broker attempts vs observations vs fills, correlation -------------------------------------------------

def test_one_trace_links_plan_client_order_broker_order_and_journal_rows(tmp_path, collector):
    store, b, j = tl.Store(tmp_path), tl.FakeBroker(), tl.FakeJournal()
    tr = traced(collector)
    out = entry_cycle(store, b, j, tr)
    assert out["entries"][0]["ok"], out
    spans = spans_of(collector, tr)
    assert len({s["traceId"] for s in spans}) == 1, "one connected trace"
    pid = next(iter(store.plans))
    entry = named(spans, "entry_validation")[0]
    assert entry["out"]["plan_id"] == pid and entry["in"]["ticker"] == "WMG", "the plan's actual input and identity"
    submits = named(spans, "broker.submit")
    cids = {s["in"]["args"][0]["client_order_id"] for s in submits}
    assert cids and all(c.startswith(f"lv-{pid[:20]}-") for c in cids), "client order ids derive from the plan id"
    assert {s["out"]["id"] for s in submits} == {"id-" + c for c in cids}, "broker order ids recorded"
    assert all(s["a"]["langfuse.observation.metadata.operation"] == '"mutation"' or
               s["a"]["langfuse.observation.metadata.operation"] == "mutation" for s in submits)
    journal = named(spans, "journal.")
    assert {s["name"] for s in journal} >= {"journal.decision", "journal.result"}
    assert all(s["in"]["plan_id"] == pid for s in journal)
    events = [s for s in spans if s["a"]["langfuse.observation.type"] == "event"]
    assert any(e["name"].startswith("engine.order_") and e["in"]["plan_id"] == pid for e in events)
    assert store.plans[pid]["id"] == pid and "trace" not in json.dumps(store.data), "no trace ids in trading state"


def test_repeated_reconciliation_observes_a_fill_without_inventing_new_ones(tmp_path, collector):
    store, b, j = tl.Store(tmp_path), tl.FakeBroker(), tl.FakeJournal()
    tr = traced(collector)
    entry_cycle(store, b, j, tr)
    for _ in range(3):                                         # reconcile-only ticks: the same filled order again
        run_cycle(store, b, j, review=False, selector_fn=None, hermes_fn=None, sleep=tl.NOSLEEP, tracer=tr)
    spans = spans_of(collector, tr)
    observations = named(spans, "broker.order_by_client_id")
    filled_obs = [s for s in observations if (s["out"] or {}).get("status") == "filled"]
    assert filled_obs and len(observations) >= len(filled_obs) + 3, "broker state is re-observed on every tick"
    fills_recorded = [s for s in named(spans, "journal.result") if s["in"]["args"][:1] == ["filled"]]
    assert len(fills_recorded) == 1 and j.calls.count(("result", "WMG", "filled", fills_recorded[0]["in"].get(
        "fill_qty"))) == 1, "exactly one execution row, however often it is observed"
    assert all(s["a"]["langfuse.observation.metadata.operation"].strip('"') == "observation" for s in observations)
    assert len([s for s in spans if s["name"] == "live_cycle"]) == 4, "reconcile-only ticks are traced too"


def test_exit_revision_and_broker_errors_are_visible_and_trading_matches_tracing_off(tmp_path, collector):
    results = {}
    for mode in ("off", "on"):
        store, b, j = tl.Store(tmp_path / mode), tl.FakeBroker(), tl.FakeJournal()
        tr = traced(collector) if mode == "on" else None
        tl.do_enter(store, b, j)
        b.cancel_plan = ["ok"]
        run_cycle(store, b, j, review=True, selector_fn=lambda *a: ([], "ok", {"universe": tl.WL}),
                  hermes_fn=lambda p: (tl._reply("EXIT"), "sha"), sleep=tl.NOSLEEP, tracer=tr)
        results[mode] = (b.submits, b.cancels, j.calls, {k: v["status"] for k, v in store.plans.items()})
        if tr:
            spans = spans_of(collector, tr)
    assert [len(x) for x in results["off"][:2]] == [len(x) for x in results["on"][:2]]
    assert results["off"][2] == results["on"][2], "identical journal rows"
    assert list(results["off"][3].values()) == list(results["on"][3].values()), "identical plan states"
    holding = named(spans, "holding_action")[0]
    assert holding["in"]["action"] == "EXIT" and holding["in"]["reason"] == "thesis check", "the decision as made"
    assert named(spans, "broker.cancel") and any(s["in"]["args"][0]["side"] == "sell"
                                                 for s in named(spans, "broker.submit"))
    assert any(s["name"] == "journal.exit" for s in spans)


def test_a_dry_run_guard_refusal_is_an_errored_attempt_and_changes_nothing(tmp_path, collector):
    outs = {}
    for mode in ("off", "on"):
        store, inner, j = tl.Store(tmp_path / mode), tl.FakeBroker(), tl.FakeJournal()
        tr = traced(collector) if mode == "on" else None
        outs[mode] = (entry_cycle(store, ReadOnlyBroker(inner), j, tr), inner.submits, j.calls,
                      [p["status"] for p in store.plans.values()])
    assert outs["off"][1:] == outs["on"][1:] and outs["off"][0]["entries"] == outs["on"][0]["entries"]
    assert outs["on"][1] == [], "nothing reached the broker"
    attempts = named(spans_of(collector, tr), "broker.submit")
    assert attempts and all(s["status"]["code"] == 2 and "dry-run" in s["status"]["message"] for s in attempts)


def test_broker_payloads_are_redacted_and_bounded(tmp_path, collector):
    store, b, j = tl.Store(tmp_path), tl.FakeBroker(), tl.FakeJournal()
    b.news_result = {"articles": [{"title": "x", "api_key": "ordinary-sensitive-value",
                                   "body": "y" * 50_000}], "complete": True}
    tr = traced(collector)
    entry_cycle(store, b, j, tr)
    spans = spans_of(collector, tr)
    wire = b"".join(collector.bodies).decode()
    assert "ordinary-sensitive-value" not in wire
    news = named(spans, "broker.news")
    assert news and "truncated" in news[0]["a"]["langfuse.observation.output"]
    assert max(len(v) for s in spans for v in s["a"].values() if isinstance(v, str)) <= otlp.MAX_ATTR_BYTES + 400


# -- Laya selection: actual inference vs snapshot reuse, ranking, exclusions -------------------------------------------

import test_live_selector as tsel  # noqa: E402
from selector import LiveSelector  # noqa: E402


def make_selector(tmp_path, tr=None, scorer=None, n=30):
    screens = tsel.record_screen(tmp_path, n)
    sc = scorer or tsel.FakeScorer()
    sel = LiveSelector(tmp_path / "state", screen_dir=screens, fetch_history=tsel.history, scorer=sc)
    sel.tracer = tr
    return sel, sc


def in_span(tr, fn):
    with tr.trace("live_cycle"):
        with tr.span("selection"):
            return fn()


def test_selection_records_inference_then_honest_cache_reuse_and_matches_untraced(tmp_path, collector):
    tr = traced(collector)
    sel, scorer = make_selector(tmp_path / "t", tr)
    held = {"S029", "S028"}
    first = in_span(tr, lambda: sel(tsel.THU, tsel.SESSION, held))
    second = in_span(tr, lambda: sel(tsel.THU, tsel.SESSION, held))
    plain, plain_scorer = make_selector(tmp_path / "p")
    assert first == plain(tsel.THU, tsel.SESSION, held), "tracing changes no shortlist, status or event"
    assert scorer.calls == plain_scorer.calls == 1 and first[0] == second[0]
    spans = spans_of(collector, tr)
    inference = named(spans, "laya.inference")
    assert len(inference) == 1, "one real inference; the second day-run reused the snapshot"
    inf = inference[0]
    assert inf["a"]["langfuse.observation.type"] == "generation"
    assert inf["a"]["langfuse.observation.model.name"] == "fake-checkpoint"
    assert inf["in"]["question"] == "q" and len(inf["in"]["cards"]) == 30
    card = next(iter(inf["in"]["cards"].values()))
    assert set(card) >= {"five_day_return", "rsi14", "sma20_distance"} and "row_id" not in json.dumps(inf["in"])
    assert set(inf["out"]) == set(inf["in"]["cards"]), "every card's score, by ticker"
    laya = named(spans, "selection.laya")
    assert laya[0]["out"]["inference"].startswith("ran: 1") and laya[0]["out"]["replayed"] is False
    assert laya[1]["out"]["inference"].startswith("snapshot reuse") and laya[1]["out"]["replayed"] is True
    assert laya[0]["out"]["snapshot_sha256"] == laya[1]["out"]["snapshot_sha256"], "the same snapshot identity"
    ranking = laya[0]["out"]["ranking"]["rows"]
    ranked = [r["ticker"] for r in ranking if r["rank"] is not None]
    assert ranked[:2] == ["S029", "S028"] and ranking[0]["review_probability"] is not None
    excl = named(spans, "selection.exclusions")[0]["out"]
    assert excl["removed_in_rank_order"] == ["S029", "S028"], "held/pending names removed in rank order"
    assert excl["delivered"] == [c["ticker"] for c in first[0]] and len(excl["delivered"]) == 20
    assert len(named(spans, "laya.fetch_history")) == 1, "history is fetched only when the snapshot is built"
    universe = named(spans, "selection.universe")[0]["out"]
    assert universe["universe_version"].startswith("live-screen-") and universe["members"] == 30
    parents = {s["spanId"]: s["name"] for s in spans}
    assert parents[laya[0]["parentSpanId"]] == "selection" and parents[inf["parentSpanId"]] == "selection.laya"


def test_selector_failure_is_an_errored_selection_with_no_invented_inference(tmp_path, collector):
    tr = traced(collector)
    broken = tsel.FakeScorer()
    broken.fail = True
    sel, _ = make_selector(tmp_path / "t", tr, scorer=broken)
    traced_out = in_span(tr, lambda: sel(tsel.THU, tsel.SESSION, set()))
    plain_broken = tsel.FakeScorer()
    plain_broken.fail = True
    plain, _ = make_selector(tmp_path / "p", scorer=plain_broken)
    assert traced_out == plain(tsel.THU, tsel.SESSION, set()) and traced_out[0] == []
    spans = spans_of(collector, tr)
    inf = named(spans, "laya.inference")[0]
    assert inf["status"]["code"] == 2 and "laya worker down" in inf["status"]["message"], "the attempt, with its error"
    laya = named(spans, "selection.laya")[0]
    assert laya["a"]["langfuse.observation.level"] == "ERROR" and laya["out"]["ranking"] is None


def test_a_missing_universe_is_an_errored_universe_span_and_blocks_entries_only(tmp_path, collector):
    tr = traced(collector)
    sel = LiveSelector(tmp_path / "state", screen_dir=tmp_path / "no-screens", fetch_history=tsel.history,
                       scorer=tsel.FakeScorer())
    sel.tracer = tr
    store, b, j = tl.Store(tmp_path / "s"), tl.FakeBroker(), tl.FakeJournal()
    reply = json.dumps({"holdings": [], "entries": [], "no_entry_reason": "no shortlist"})
    out = run_cycle(store, b, j, review=True, selector_fn=sel, hermes_fn=lambda p: (reply, "sha"),
                    sleep=tl.NOSLEEP, tracer=tr)
    assert out["entries"] == []
    spans = spans_of(collector, tr)
    u = named(spans, "selection.universe")[0]
    assert u["status"]["code"] == 2 and "no usable eligible-universe screen" in u["status"]["message"]
    assert [s for s in spans if s["name"] == "selection"][0]["out"]["status"].startswith("unavailable")


# -- weekly screen: its own trace, real stages, identity, reuse, failure -----------------------------------------------

import scalable_screener as ss  # noqa: E402
import trace_screen  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402


def sat_clock():
    ticks = [datetime(2026, 10, 3, 12, tzinfo=timezone.utc)]

    def clock():
        ticks[0] += timedelta(seconds=1)
        return ticks[0]
    return clock


class FailingHistory(tsel.ScreenProviders):
    def history(self, syms):
        raise RuntimeError("history provider down")


def test_weekly_screen_has_its_own_trace_with_real_stages_and_a_published_identity(tmp_path, collector):
    tr = traced(collector)
    d = tmp_path / "screens"
    rc = trace_screen.run(["--write", "--output-dir", str(d)], tracer=tr, providers=tsel.ScreenProviders(
        tsel.symbols(12)), clock=sat_clock())
    plain_dir = tmp_path / "plain"
    ss.main(["--write", "--output-dir", str(plain_dir)], providers=tsel.ScreenProviders(tsel.symbols(12)),
            clock=sat_clock())
    assert rc == 0
    traced_rec = ss.load_result(d, ss.screen_id(tsel.SAT, ss.DEFAULT_CONFIG))
    assert traced_rec == ss.load_result(plain_dir, ss.screen_id(tsel.SAT, ss.DEFAULT_CONFIG)), "identical record"
    spans = spans_of(collector, tr)
    assert len({s["traceId"] for s in spans}) == 1
    root = [s for s in spans if s["name"] == "weekly_screen"][0]
    assert root["a"]["langfuse.trace.name"] == "weekly_screen"
    assert [s["name"] for s in sorted(named(spans, "screen."), key=lambda s: int(s["startTimeUnixNano"]))] == [
        "screen.universe", "screen.facts", "screen.history", "screen.news"]
    out = root["out"]
    assert out["screen_id"] == traced_rec["screen_id"] and out["content_hash"] == traced_rec["content_hash"]
    assert out["persisted"] == "result published (write-once)" and out["pool_size"] == 12
    assert out["config_hash"] and out["news_windows"] and out["telemetry"]["facts"]["requested"] == 12
    assert named(spans, "screen.facts")[0]["in"]["requested"] == 12
    assert ss.run_screen.__module__ == "scalable_screener" and ss.screen.__name__ == "screen", "module restored"


def test_rerunning_the_screen_reuses_the_result_without_fetching_and_says_so(tmp_path, collector):
    d = tmp_path / "screens"
    ss.main(["--write", "--output-dir", str(d)], providers=tsel.ScreenProviders(tsel.symbols(5)), clock=sat_clock())
    tr = traced(collector)
    assert trace_screen.run(["--write", "--output-dir", str(d)], tracer=tr,
                            providers=tsel.ScreenProviders(tsel.symbols(5)), clock=sat_clock()) == 0
    spans = spans_of(collector, tr)
    assert not named(spans, "screen."), "no stage ran: nothing was fetched"
    out = [s for s in spans if s["name"] == "weekly_screen"][0]["out"]
    assert out["reused_existing_result"] is True and out["persisted"].startswith("existing result reused")


def test_a_failed_stage_is_explicit_unavailable_data_and_no_result_is_published(tmp_path, collector):
    tr = traced(collector)
    d = tmp_path / "screens"
    rc = trace_screen.run(["--write", "--output-dir", str(d)], tracer=tr, providers=FailingHistory(tsel.symbols(8)),
                          clock=sat_clock())
    assert rc == 1 and not ss.result_path(d, ss.screen_id(tsel.SAT, ss.DEFAULT_CONFIG)).exists()
    spans = spans_of(collector, tr)
    hist = named(spans, "screen.history")[0]
    assert hist["status"]["code"] == 2 and "history provider down" in hist["status"]["message"]
    assert not named(spans, "screen.news"), "a stage that never ran is absent, not reported empty"
    root = [s for s in spans if s["name"] == "weekly_screen"][0]
    assert "history_provider_failed" in root["out"]["failure_reasons"]
    assert root["out"]["counts"]["data_unavailable"] == 8 and root["a"]["langfuse.observation.level"] == "WARNING"
    assert root["out"]["persisted"] == "failed attempt recorded; no result published"


def test_a_cycle_names_the_screen_it_consumed_and_never_runs_one(tmp_path, collector):
    tr = traced(collector)
    sel, _ = make_selector(tmp_path, tr, n=12)
    store, b, j = tl.Store(tmp_path / "s"), tl.FakeBroker(), tl.FakeJournal()
    reply = json.dumps({"holdings": [], "entries": [], "no_entry_reason": "none"})
    run_cycle(store, b, j, review=True, selector_fn=sel, hermes_fn=lambda p: (reply, "sha"), sleep=tl.NOSLEEP,
              tracer=tr)
    spans = spans_of(collector, tr)
    assert not named(spans, "screen.") and not any(s["name"] == "weekly_screen" for s in spans)
    sid = ss.screen_id(tsel.SAT, ss.DEFAULT_CONFIG)
    assert named(spans, "selection.universe")[0]["out"]["universe_version"] == f"live-screen-{sid}"


def test_reconcile_only_ticks_say_what_they_skipped_and_runs_carry_their_versions(tmp_path, collector):
    import run_live
    tr = traced(collector)
    tr.context = {"mode": "cycle", "policy_sha256": run_live.policy_version()}
    run_cycle(tl.Store(tmp_path), tl.FakeBroker(), tl.FakeJournal(), review=False, selector_fn=None, hermes_fn=None,
              sleep=tl.NOSLEEP, tracer=tr)
    root = [s for s in spans_of(collector, tr) if s["name"] == "live_cycle"][0]
    assert "reconcile-only" in root["a"]["langfuse.observation.metadata.skipped"]
    assert len(run_live.policy_version()) == 12 and root["a"]["langfuse.observation.metadata.mode"] == "cycle"
