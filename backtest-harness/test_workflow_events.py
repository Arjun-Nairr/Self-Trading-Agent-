"""Workflow progress telemetry: the public-safe contract
(trade-journal-mcp/workflow_contract.py), the non-blocking emitter
(workflow_events.py), and the events the REAL run_day emits (I/O mocked via
test_laya_wiring._cycle). Every emitted event is validated through the same
contract the trade-journal server enforces. No Docker, Neon, or model.
Run: python -m pytest -q test_workflow_events.py
"""
import importlib.util
import json
import re
import threading
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import workflow_events as we
from run_backtest import Holding, Portfolio, QueuedOrder
from test_laya_wiring import SYMBOLS, UNIVERSE, _ctx, _cycle, _masking, _selected

_spec = importlib.util.spec_from_file_location(
    "workflow_contract", Path(__file__).resolve().parent.parent / "trade-journal-mcp" / "workflow_contract.py")
wc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wc)

RUN = str(uuid.uuid4())


class Recorder:
    """In-memory writer that applies the real contract, like the server."""
    def __init__(self, fail_first=0, always_fail=False):
        self.events, self.calls, self.fail_first, self.always_fail = [], 0, fail_first, always_fail
        self.seen = set()

    def __call__(self, batch):
        self.calls += 1
        if self.always_fail or self.calls <= self.fail_first:
            raise RuntimeError("neon unreachable")
        ins = dup = 0
        rejected = []
        for e in batch:
            try:
                v = wc.validate_event(e)
            except wc.EventRejected as exc:
                rejected.append(str(exc))
                continue
            key = (v["invocation_id"], v["seq"])
            if key in self.seen:
                dup += 1
                continue
            self.seen.add(key)
            self.events.append(v)
            ins += 1
        return {"inserted": ins, "duplicates": dup, "rejected": rejected}


def _emitter(rec, **kw):
    return we.WorkflowEmitter(RUN, rec, log=lambda m: None, **kw)


def _event(**over):
    e = {"run_id": RUN, "invocation_id": str(uuid.uuid4()), "seq": 0, "stage": "laya", "status": "completed",
         "occurred_at": datetime.now(timezone.utc).isoformat(), "cycle_id": str(uuid.uuid4()),
         "day_index": 3, "day_label": "Day 4", "market_date": "2026-09-30",
         "metrics": {"cards": 49, "scored": 49, "score_max": 0.2463}}
    e.update(over)
    return e


# -- contract ------------------------------------------------------------------

def test_contract_accepts_structured_events_only():
    assert wc.validate_event(_event())["metrics"] == {"cards": 49, "scored": 49, "score_max": 0.2463}
    for bad, why in [
        (_event(ticker="NUE"), "fields not allowed"),
        (_event(metrics={"top_name": "NUE"}), "not allowlisted"),
        (_event(metrics={"cards": "49"}), "non-negative int"),
        (_event(metrics={"cards": True}), "non-negative int"),
        (_event(stage="decision_risk", metrics={"rule_codes": ["buy because momentum"]}), "not allowlisted"),
        (_event(stage="decision_risk", metrics={"outcome": "maybe"}), "not in"),
        (_event(stage="prompt"), "stage"),
        (_event(status="done"), "status"),
        (_event(proposal_key="NUE-0"), "proposal_key"),
        (_event(occurred_at="2026-10-03T12:00:00"), "timezone-aware"),
        (_event(day_label="Monday"), "day_label"),
        (_event(metrics={"score_max": 1.5}), "fraction"),
        (_event(cycle_id=None), "need cycle_id"),
    ]:
        with pytest.raises(wc.EventRejected, match=why):
            wc.validate_event(bad)
    run_ev = _event(stage="run", cycle_id=None, day_index=None, day_label=None, market_date=None,
                    metrics={"sessions_requested": 2, "selector_mode": "laya"})
    assert wc.validate_event(run_ev)["cycle_id"] is None


def test_rejection_codes_extract_only_allowlisted_rule_codes():
    assert we.rejection_codes("DINO: queued BUY rejected -- §2b: position is 20.30% of equity; "
                              "§6b: reward:risk is 1.21, minimum is 1.5") == ["§2b", "§6b"]
    assert we.rejection_codes("X: selector boundary rejected -- alias 'KV42' is not in today's pool") == [
        "selector_boundary"]
    assert we.rejection_codes("free text with no rule") == []
    for text in ("§2/§3: quantity 5 does not equal required whole-share size 4", "pre_fill_news_veto",
                 "unknown alias 'ZZ'", "BUY missing stop/target -- cannot queue"):
        wc.validate_event(_event(stage="decision_risk", metrics={"rule_codes": we.rejection_codes(text)}))


# -- emitter -------------------------------------------------------------------

def test_emitter_orders_writes_and_is_idempotent_on_retry():
    rec = Recorder(fail_first=1)          # first write fails; the retry re-sends the same batch
    em = _emitter(rec)
    c = em.for_cycle(str(uuid.uuid4()), 0, "Day 1", date(2026, 9, 28))
    for st in ("started", "completed"):
        c.emit("evidence", st, {"shortlisted": 10})
    stats = em.close()
    assert [e["seq"] for e in rec.events] == [0, 1] and stats["written"] == 2 and stats["failed_batches"] == 0
    rec2 = Recorder()
    rec2.seen = set(rec.seen)             # a re-sent batch after a lost acknowledgement
    assert rec2([{**_event(), "invocation_id": rec.events[0]["invocation_id"], "seq": 0}])["duplicates"] == 1


def test_telemetry_failure_never_raises_and_is_counted():
    em = _emitter(Recorder(always_fail=True))
    em.emit("run", "started", {"sessions_requested": 2})
    em.emit("run", "started", lambda: 1 / 0)          # a bug building metrics
    stats = em.close()
    assert stats["failed_batches"] == 1 and stats["dropped"] == 1 and stats["written"] == 0


def test_a_full_queue_drops_instead_of_blocking_the_trading_thread():
    gate = threading.Event()

    def slow(batch):
        gate.wait(5)
        return {"inserted": len(batch)}
    em = _emitter(slow, max_queue=3, batch_max=1)
    t0 = time.monotonic()
    for _ in range(20):
        em.emit("run", "started", {})
    assert time.monotonic() - t0 < 1.0, "emit must never wait on the writer"
    gate.set()
    stats = em.close()
    assert stats["dropped"] >= 15


def test_contract_rejections_are_reported_not_stored():
    rec = Recorder()
    em = _emitter(rec)
    em.emit("laya", "completed", {"top_ticker": "NUE"}, cycle_id=str(uuid.uuid4()))
    assert em.close()["rejected"] == 1 and rec.events == []


# -- run_day wiring ------------------------------------------------------------

def _run(tmp_path, selector, **kw):
    rec = Recorder()
    em = _emitter(rec)
    cap = _cycle(tmp_path, selector, progress=em, **kw)
    em.close()
    return cap, rec.events


def _stages(events):
    return [(e["stage"], e["status"]) for e in events if e["decision_id"] is None and e["proposal_key"] is None]


def _leak_free(events, masking):
    text = json.dumps(events)
    for t in SYMBOLS:
        assert not re.search(rf"\b{t}\b", text), t
        assert masking.alias(t) not in text
    for m in UNIVERSE.members:
        assert m.name not in text


def test_selector_cycle_emits_truthful_stage_order_and_links(tmp_path):
    ctx = _ctx(tmp_path)
    m = _masking()
    # Laya's picks are known only after selection, so propose on a not-yet-
    # known name list: one BUY of a name that will be selected (decided after
    # a dry selection) and one of a name that will not.
    probe = _ctx(tmp_path / "probe")
    _cycle(tmp_path / "probe", probe, masking=m)
    picked = _selected(tmp_path / "probe")
    other = next(t for t in SYMBOLS if t not in picked)
    buy = {"alias": m.alias(picked[0]), "action": "BUY", "quantity": 199, "entry_price": 100.0,
           "stop_price": 95.0, "target_price": 112.0, "confidence": 3, "primary_signal": "technical",
           "technical_basis": "trend", "expected_reward_risk": 2.4, "invalidation_condition": "x",
           "reasoning": "x"}
    cap, events = _run(tmp_path, ctx, masking=m,
                       decisions=[dict(buy), {**buy, "alias": m.alias(other)}])
    assert _stages(events) == [
        ("cycle", "started"),
        ("prefill_news", "skipped"), ("execution", "skipped"), ("positions", "skipped"),
        ("population", "completed"), ("laya", "started"), ("laya", "completed"), ("shortlist", "completed"),
        ("evidence", "started"), ("evidence", "completed"),
        ("hermes", "started"), ("hermes", "completed"),
        ("decision_risk", "started"), ("decision_risk", "completed"),
    ]
    by = {e["stage"] + ":" + e["status"]: e["metrics"] for e in events if e["proposal_key"] is None}
    assert by["population:completed"] == {"universe": 30, "excluded_held_or_pending": 0, "eligible": 30,
                                          "selector_mode": "laya"}
    laya = by["laya:completed"]
    assert laya["cards"] == 30 and laya["scored"] == 30 and laya["status"] == "selected"
    assert 0 <= laya["score_min"] <= laya["score_median"] <= laya["score_max"] <= 1
    assert by["shortlist:completed"] == {"selected": 10, "k": 10, "shortfall": False, "status": "selected"}
    assert by["evidence:completed"]["shortlisted"] == 10 and by["evidence:completed"]["fundamentals"] == "off"
    assert by["hermes:completed"]["decisions"] == 2 and by["hermes:completed"]["replayed"] is False
    props = [e for e in events if e["proposal_key"]]
    assert [p["proposal_key"] for p in props] == ["p31-0", "p31-1"]
    queued, rejected = props
    assert queued["metrics"]["outcome"] == "queued" and queued["decision_id"] == cap["queued"][0][0][2]
    assert rejected["metrics"] == {"action": "BUY", "outcome": "rejected", "rule_codes": ["selector_boundary"]}
    assert rejected["decision_id"] is None, "no fake trade row / decision id for a pre-journal rejection"
    assert by["decision_risk:completed"] == {"proposals": 2, "queued": 1, "rejected": 1}
    assert len({e["seq"] for e in events}) == len(events) and len({e["invocation_id"] for e in events}) == 1
    _leak_free(events, m)


def _order(m, did, cutoff="2026-03-19T16:00:00-04:00"):
    return QueuedOrder(ticker="OLN", alias=m.alias("OLN"), action="BUY", quantity=10, stop_price=95.0,
                       target_price=120.0, entry_price=100.0, queued_day_index=30,
                       decision={"alias": m.alias("OLN"), "action": "BUY", "quantity": 10}, decision_id=did,
                       decision_cutoff_at=cutoff)


def test_unverifiable_pre_fill_news_cancels_and_is_linked(tmp_path):
    m = _masking()
    did = str(uuid.uuid4())
    pf = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6, pending_orders=[_order(m, did, cutoff="")])
    cap, events = _run(tmp_path, _ctx(tmp_path), portfolio=pf, masking=m)
    cancel = next(e for e in events if e["stage"] == "execution" and e["decision_id"])
    assert cancel["decision_id"] == did and cancel["metrics"] == {
        "action": "BUY", "outcome": "cancelled", "rule_codes": ["pre_fill_news_unavailable"]}
    assert ("execution", "skipped") in _stages(events) and "OLN" not in cap["working"].holdings


def test_next_day_order_path_links_the_fill_to_its_originating_decision(tmp_path):
    m = _masking()
    did = str(uuid.uuid4())
    order = _order(m, did)
    pf = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6, pending_orders=[order])
    cap, events = _run(tmp_path, _ctx(tmp_path), portfolio=pf, masking=m)
    seq = _stages(events)
    assert seq[:7] == [("cycle", "started"), ("prefill_news", "started"), ("prefill_news", "completed"),
                       ("execution", "started"), ("positions", "skipped"), ("execution", "completed"),
                       ("population", "completed")], "the order path runs before the candidate path"
    gate = next(e for e in events if e["stage"] == "prefill_news" and e["status"] == "completed")
    assert gate["metrics"] == {"queued_buys": 1, "confirmed": 1, "cancelled": 0, "articles": 0,
                               "hermes_calls": 0, "replayed": False}
    fill = next(e for e in events if e["stage"] == "execution" and e["decision_id"])
    assert fill["decision_id"] == did and fill["metrics"] == {"action": "BUY", "outcome": "filled"}
    assert "OLN" in cap["working"].holdings
    pop = next(e for e in events if e["stage"] == "population")
    assert pop["metrics"]["excluded_held_or_pending"] == 1, "the new holding is excluded from selection"
    _leak_free(events, m)


def test_a_mechanical_exit_is_linked_and_independent_of_the_shortlist(tmp_path):
    m = _masking()
    held = Portfolio(cash=90_000.0, equity_curve=[100_000.0] * 6, trading_days_elapsed=3,
                     holdings={"OLN": Holding("OLN", 100, 90.0, 95.0, 99.0, 28, 2)})  # target 99 < open 100
    cap, events = _run(tmp_path, _ctx(tmp_path), portfolio=held, masking=m)
    ex = [e for e in events if e["stage"] == "positions" and e["decision_id"]]
    assert len(ex) == 1 and ex[0]["metrics"] == {"exit_kind": "opening_gap", "exit_reason": "target"}
    done = next(e for e in events if e["stage"] == "positions" and e["status"] == "completed"
                and not e["decision_id"])
    assert done["metrics"] == {"monitored": 1, "exits": 1, "open_after": 0}


def test_selector_off_marks_laya_and_shortlist_skipped(tmp_path):
    from test_laya_wiring import WATCHLIST
    cap, events = _run(tmp_path, None, masking=_masking(), watchlist=WATCHLIST[:10])
    st = dict(((e["stage"], e["status"]), e["metrics"]) for e in events if e["proposal_key"] is None)
    assert ("laya", "skipped") in st and ("shortlist", "skipped") in st
    assert st[("population", "completed")]["selector_mode"] == "off"
    assert not any(e["stage"] == "laya" and e["status"] != "skipped" for e in events)


def test_hermes_failure_is_recorded_and_still_propagates(tmp_path):
    rec = Recorder()
    em = _emitter(rec)
    with pytest.raises(RuntimeError, match="model unavailable"):
        _cycle(tmp_path, _ctx(tmp_path), progress=em, hermes_error=RuntimeError("model unavailable"))
    em.close()
    assert _stages(rec.events)[-2:] == [("hermes", "started"), ("hermes", "failed")]


def test_telemetry_failure_does_not_change_the_cycle(tmp_path):
    m = _masking()
    did = str(uuid.uuid4())
    order = _order(m, did)

    def run(progress, sub):
        pf = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6, pending_orders=[order])
        return _cycle(tmp_path / sub, _ctx(tmp_path / sub), portfolio=pf, masking=m, progress=progress)
    broken = _emitter(Recorder(always_fail=True))
    a, b = run(None, "plain"), run(broken, "broken")
    stats = broken.close()
    assert stats["failed_batches"] >= 1 and stats["written"] == 0
    for k in ("decisions", "blocked", "exits", "fills", "proposals"):
        assert a["summary"][k] == b["summary"][k], k
    assert a["working"].to_dict() == b["working"].to_dict()
    assert a["prompts"] == b["prompts"] and a["queued"] == b["queued"]
