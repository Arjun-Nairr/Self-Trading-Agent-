"""Live-paper controller: the critical paths, with an INJECTED broker that
simulates Alpaca order states -- including the awkward ones (delayed fills,
cancels that do not land, a protective fill racing a cancel, ambiguous or lost
submissions, a crash mid-replacement). The broker enforces the two Alpaca
rules that matter here: a sell is rejected while a buy is working on the same
symbol, and a sell cannot exceed the shares not already committed to another
sell. No Docker, broker, model or database call. These prove the controller's
logic against a MODEL of Alpaca, not Alpaca itself."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
from agent import holding_view  # noqa: E402
from engine import (CycleBusy, CycleLock, StaleState, Store, enter, exit_position, parse_ts,  # noqa: E402
                    reconcile, revise, size)
from run_live import review_slot, run_cycle  # noqa: E402

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)          # 11:00 ET
TODAY = "2026-10-08"
WL = ["OLN", "WMG", "PLNT", "SFM", "DINO", "RYAN", "BRKR", "ATI", "BRX", "ALGM"]
NOSLEEP = lambda s: None  # noqa: E731


class Crash(BaseException):
    """A process dying mid-step (not catchable by `except Exception`)."""


class FakeBroker:
    def __init__(self, entry="full"):
        self.is_open, self.now, self.tick = True, NOW, 0
        self.acct = {"equity": 100_000.0, "cash": 100_000.0, "daily_pl_pct": 0.0, "weekly_pl_pct": 0.0,
                     "drawdown_from_peak_pct": 0.0, "trading_blocked": False}
        self.pos, self.ords, self.submits, self.cancels = {}, {}, [], []
        self.entry = entry                 # full | ioc_partial | delayed_partial | none
        self.exit_mode = "filled"          # filled | pending | partial_open | rejected
        self.cancel_plan = []              # per cancel call: ok | ignore | stop_fills
        self.rules = []                    # [{"when": fn(body), "mode": lost | ambiguous | crash}] each used once
        self.ask, self.quote_ts = 50.0, None
        self.news_result, self.held_sessions = {"articles": [], "complete": True}, 1

    # -- read
    def clock(self):
        c = {"is_open": self.is_open, "timestamp": self.now.isoformat()}
        self.now += timedelta(seconds=self.tick)
        return c

    def account(self): return dict(self.acct)
    def positions(self): return [dict(p) for p in self.pos.values()]
    def orders(self): return [json.loads(json.dumps(o)) for o in self.ords.values() if o["status"] in engine.OPEN_STATUSES]
    def order_by_client_id(self, c): return json.loads(json.dumps(self.ords[c])) if c in self.ords else None

    def latest_quote(self, s):
        return {"ask": self.ask, "bid": self.ask - 0.02,
                "timestamp": self.quote_ts or (self.now - timedelta(seconds=5)).isoformat()}

    def news(self, s, h):
        if isinstance(self.news_result, Exception):
            raise self.news_result
        return self.news_result

    def daily_bars(self, s, n): return [{"date": f"d{i}", "low": 46.0 + (i % 3), "high": 55.0} for i in range(n)]
    def sessions_since(self, day, today): return self.held_sessions
    def last_completed_session(self, today, is_open): return datetime(2026, 10, 7).date()
    def session(self, day): return {"open": "09:30", "close": "16:00"}

    # -- write
    def submit(self, body):
        self.submits.append(body)
        mode = next((r["mode"] for r in self.rules if r["when"](body)), None)
        self.rules = [r for r in self.rules if not r["when"](body)] if mode else self.rules
        if mode == "lost":
            raise TimeoutError("no response; never reached the broker")
        if mode == "crash":
            raise Crash()
        c = body["client_order_id"]
        if c in self.ords:
            raise RuntimeError("client_order_id must be unique")
        if body["side"] == "sell":
            if any(o["symbol"] == body["symbol"] and o["side"] == "buy" and o["status"] in engine.OPEN_STATUSES
                   for o in self.ords.values()):
                raise RuntimeError("potential wash trade: a buy is working on this symbol")
            committed = sum(int(o["qty"]) - int(o["filled_qty"]) for o in self.ords.values()
                            if o["symbol"] == body["symbol"] and o["side"] == "sell"
                            and o["status"] in engine.OPEN_STATUSES)
            if int(body["qty"]) > int(self.pos.get(body["symbol"], {"qty": 0})["qty"]) - committed:
                raise RuntimeError("insufficient qty available for sell")
        o = self._make(body)
        if mode == "ambiguous":
            raise TimeoutError("response lost after the broker accepted it")
        return json.loads(json.dumps(o))

    def _make(self, b):
        c, qty = b["client_order_id"], int(b["qty"])
        o = {"id": "id-" + c, "client_order_id": c, "symbol": b["symbol"], "side": b["side"], "qty": str(qty),
             "type": b["type"], "limit_price": b.get("limit_price"), "status": "new", "filled_qty": "0",
             "filled_avg_price": None, "order_class": b.get("order_class", "simple"), "legs": None}
        self.ords[c] = o
        if b.get("order_class") == "oco":
            o["limit_price"] = b["take_profit"]["limit_price"]
            o["legs"] = [{"id": "leg-" + c, "type": "stop", "stop_price": b["stop_loss"]["stop_price"],
                          "status": "held", "filled_qty": "0", "filled_avg_price": None, "qty": str(qty)}]
        elif b["side"] == "buy":
            fill, status = {"full": (qty, "filled"), "ioc_partial": (qty // 2, "canceled"),
                            "delayed_partial": (qty // 2, "partially_filled"), "none": (0, "canceled")}[self.entry]
            o["status"] = status
            self.apply(o, fill)
        else:
            mode = self.exit_mode
            if mode == "filled":
                o["status"] = "filled"
                self.apply(o, qty)
            elif mode == "partial_open":
                o["status"] = "partially_filled"
                self.apply(o, qty // 4)
            elif mode == "rejected":
                o["status"] = "rejected"
            else:
                o["status"] = "accepted"
        return o

    def apply(self, o, n):
        """Fill `n` more shares of order `o` and move the position."""
        if n <= 0:
            return
        done = int(o["filled_qty"]) + n
        o["filled_qty"], o["filled_avg_price"] = str(done), o.get("limit_price") or str(self.ask)
        sym, sign = o["symbol"], 1 if o["side"] == "buy" else -1
        p = self.pos.setdefault(sym, {"symbol": sym, "qty": 0.0, "market_value": 0.0,
                                      "current_price": self.ask, "unrealized_pl": 0.0})
        p["qty"] += sign * n
        p["market_value"] = p["qty"] * self.ask
        if p["qty"] <= 0:
            del self.pos[sym]

    def complete(self, c):
        """Fill the rest of order `c`."""
        o = self.ords[c]
        self.apply(o, int(o["qty"]) - int(o["filled_qty"]))
        o["status"] = "filled"

    def finish(self, c, status):
        self.ords[c]["status"] = status

    def trigger(self, sym, leg_kind):
        o = next(o for o in self.ords.values() if o["symbol"] == sym and o["order_class"] == "oco"
                 and o["status"] in engine.OPEN_STATUSES)
        q = int(o["qty"])
        if leg_kind == "stop":
            leg = o["legs"][0]
            leg.update(status="filled", filled_qty=str(q), filled_avg_price=leg["stop_price"],
                       filled_at="2026-10-08T15:01:00Z")
            o["status"] = "canceled"
        else:
            o.update(status="filled", filled_qty=str(q), filled_avg_price=o["limit_price"],
                     filled_at="2026-10-08T15:01:00Z")
            o["legs"][0]["status"] = "canceled"
        p = self.pos[sym]
        p["qty"] -= q
        if p["qty"] <= 0:
            del self.pos[sym]

    def cancel(self, oid):
        self.cancels.append(oid)
        mode = self.cancel_plan.pop(0) if self.cancel_plan else "ok"
        o = next((o for o in self.ords.values() if o["id"] == oid), None)
        if not o or o["status"] not in engine.OPEN_STATUSES or mode == "ignore":
            return
        if mode == "stop_fills" and o["order_class"] == "oco":
            self.trigger(o["symbol"], "stop")
            return
        o["status"] = "canceled"
        for leg in o.get("legs") or []:
            leg["status"] = "canceled"

    # -- inspection
    def oco(self, only_open=True):
        return [o for o in self.ords.values() if o["order_class"] == "oco"
                and (o["status"] in engine.OPEN_STATUSES or not only_open)]

    def sells(self):
        return [s for s in self.submits if s["side"] == "sell" and s.get("order_class") != "oco"]


class FakeJournal:
    def __init__(self): self.calls = []
    def decision(self, plan): self.calls.append(("decision", plan["ticker"]))
    def result(self, plan, status, **k): self.calls.append(("result", plan["ticker"], status, k.get("fill_qty")))
    def exit(self, plan, prov, e): self.calls.append(("exit", plan["ticker"], prov, e["qty"]))


def plan(**over):
    p = {"ticker": "OLN", "setup": "pullback within an uptrend", "why_now": "held the 20-day low on volume",
         "evidence": ["maverick 2026-10-08: price above 50-day SMA"], "contrary_evidence": ["sector weak"],
         "stop_price": 46.0, "stop_basis": {"kind": "observed_low", "level": 46.0, "lookback_sessions": 20},
         "invalidation": "close below 46", "target_price": 60.0, "target_rationale": "prior high",
         "horizon_trading_days": 10, "next_review": "tomorrow's session-end: check volume"}
    p.update(over)
    return p


def do_enter(store, b, j, p=None, shortlist=("OLN", "WMG"), decided="2026-10-08T14:59:00Z"):
    return enter(store, b, j, p or plan(), shortlist=list(shortlist), universe=WL, decided_at=decided,
                 today=TODAY, sleep=NOSLEEP)


def rec(store, b, j):
    return reconcile(store, b, j, TODAY, NOSLEEP)


@pytest.fixture
def env(tmp_path):
    return Store(tmp_path), FakeBroker(), FakeJournal()


def opened(store, b, j):
    r = do_enter(store, b, j)
    assert r.ok, r.reasons
    return r.plan_id


def ox(c):
    return "id-" + c


# 1 -------------------------------------------------------------------------------------------------
def test_valid_entry_records_the_plan_and_submits_real_broker_protection(env):
    store, b, j = env
    r = do_enter(store, b, j)
    assert r.ok, r.reasons
    p = store.plans[r.plan_id]
    entry, prot = b.submits
    assert entry["type"] == "limit" and entry["time_in_force"] == "ioc" and entry["limit_price"] == "50.10"
    qty = size(100_000, 100_000, 50.10, 46.0)
    assert int(entry["qty"]) == qty == 399, "min of §2a 487, §2b 399, cash 1996"
    assert prot == {"symbol": "OLN", "qty": "399", "side": "sell", "type": "limit", "time_in_force": "gtc",
                    "order_class": "oco", "take_profit": {"limit_price": "60.00"},
                    "stop_loss": {"stop_price": "46.00"}, "client_order_id": engine.cid(r.plan_id, "protect", 0)}
    assert p["status"] == "open" and p["protection"] == "active" and p["held_qty"] == 399
    assert p["protected"]["stop"] == 46.0 and p["protected"]["qty"] == 399
    assert p["original"]["why_now"] and p["sizing"]["risk_usd"] == round(399 * 4.10, 2)
    assert ("decision", "OLN") in j.calls and ("result", "OLN", "filled", 399) in j.calls
    assert json.loads((store.dir / "live_state.json").read_text())["plans"][r.plan_id]["status"] == "open"


# 2 -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mut, why", [
    (lambda b: None, "§6a"),                                                     # stop 19% away (plan below)
    (lambda b: setattr(b, "quote_ts", (NOW - timedelta(seconds=600)).isoformat()), "stale quote"),
    (lambda b: setattr(b, "quote_ts", (NOW + timedelta(seconds=120)).isoformat()), "future-dated"),
    (lambda b: setattr(b, "quote_ts", "yesterday-ish"), "timestamp missing or invalid"),
    (lambda b: setattr(b, "news_result", {"articles": [], "complete": False}), "news verification incomplete"),
    (lambda b: setattr(b, "news_result", RuntimeError("feed down")), "news verification failed"),
    (lambda b: setattr(b, "news_result", {"complete": True, "articles": [{"created_at": "2026-10-08T14:59:30Z"}]}),
     "after the research cutoff"),
    (lambda b: setattr(b, "news_result", {"complete": True, "articles": [{"headline": "undated"}]}), "or undated"),
    (lambda b: setattr(b, "is_open", False), "market is closed"),
])
def test_risk_and_stale_or_missing_news_rejections_place_no_order(env, mut, why):
    store, b, j = env
    mut(b)
    p = plan(stop_price=40.0, stop_basis={"kind": "observed_low", "level": 46.0, "lookback_sessions": 20}) \
        if why == "§6a" else plan()
    r = do_enter(store, b, j, p)
    assert not r.ok and any(why in x for x in r.reasons), r.reasons
    assert b.submits == [] and store.plans == {}


def test_unverifiable_stop_level_and_off_shortlist_names_are_rejected(env):
    store, b, j = env
    bad = plan(stop_price=45.0, stop_basis={"kind": "observed_low", "level": 45.0, "lookback_sessions": 20})
    assert "not the observed" in do_enter(store, b, j, bad).reasons[0]
    assert "not on today's Laya shortlist" in do_enter(store, b, j, shortlist=("WMG",)).reasons[0]
    assert b.submits == []


def test_timestamps_parse_consistently_and_invalid_ones_do_not_parse():
    for s in ("2026-10-08T14:59:58.123456789Z", "2026-10-08T14:59:58Z", "2026-10-08T10:59:58-04:00",
              "2026-10-08T14:59:58+0000", "2026-10-08 14:59:58.5+00:00"):
        assert parse_ts(s) == parse_ts(s) and parse_ts(s).tzinfo is not None, s
    assert parse_ts("2026-10-08T14:59:58Z") == parse_ts("2026-10-08T10:59:58-04:00")
    for bad in (None, "", "2026-10-08", "2026-10-08T14:59:58", 12345, "soon"):
        assert parse_ts(bad) is None, bad


# 3 -------------------------------------------------------------------------------------------------
def test_ioc_partial_fill_is_protected_at_the_filled_quantity(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker("ioc_partial"), FakeJournal()
    r = do_enter(store, b, j)
    p = store.plans[r.plan_id]
    assert p["status"] == "open" and p["entry_result"]["partial"] and p["held_qty"] == 199
    assert b.submits[-1]["qty"] == "199" and p["protection"] == "active"


def test_delayed_nonterminal_partial_entry_is_recognised_then_resolved_and_protected(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker("delayed_partial"), FakeJournal()
    b.cancel_plan = ["ignore"]                                   # the first cancel does not land
    r = do_enter(store, b, j)
    p = store.plans[r.plan_id]
    assert p["status"] == "entry_pending" and p["filled_qty"] == 199, "fills are recognised while unresolved"
    assert p["protection"] == "unprotected" and "OLN" in engine.unprotected_holdings(store)
    assert len(b.submits) == 1, "no protective sell while the buy is still working (the broker would refuse it)"
    paused = do_enter(store, b, j, plan(ticker="WMG"))
    assert not paused.ok and "paused" in paused.reasons[0]
    rec(store, b, j)                                             # cancel lands now; the outstanding qty is resolved
    assert p["status"] == "open" and p["held_qty"] == 199 and p["protection"] == "active"
    assert b.submits[-1]["qty"] == "199" and b.submits[-1]["order_class"] == "oco"
    n = len(b.submits)
    for _ in range(3):                                           # repeated cycles, each a fresh process, are idempotent
        rec(Store(tmp_path), b, j)
    assert len(b.submits) == n and j.calls.count(("result", "OLN", "filled", 199)) == 1
    assert not engine.unprotected_holdings(Store(tmp_path))


def test_delayed_entry_that_completes_is_protected_for_the_full_quantity(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker("delayed_partial"), FakeJournal()
    b.cancel_plan = ["ignore", "ignore"]
    r = do_enter(store, b, j)
    b.complete(engine.cid(r.plan_id, "entry"))                   # the rest filled before the cancel landed
    rec(store, b, j)
    p = store.plans[r.plan_id]
    assert p["status"] == "open" and p["held_qty"] == 399 and b.submits[-1]["qty"] == "399"


# 4 -------------------------------------------------------------------------------------------------
def test_ambiguous_submission_is_resolved_by_client_order_id_without_a_duplicate(env):
    store, b, j = env
    b.rules = [{"when": lambda x: x["side"] == "buy", "mode": "ambiguous"}]
    r = do_enter(store, b, j)
    assert r.ok and store.plans[r.plan_id]["status"] == "open"
    assert sum(1 for s in b.submits if s["side"] == "buy") == 1


def test_lost_submission_fails_without_retry_after_the_grace_period(env):
    store, b, j = env
    b.rules = [{"when": lambda x: x["side"] == "buy", "mode": "lost"}]
    r = do_enter(store, b, j)
    assert r.ok and store.plans[r.plan_id]["status"] == "entry_pending", "not yet declared failed"
    b.now += timedelta(seconds=engine.ENTRY_GRACE_S + 1)
    rec(store, b, j)
    assert store.plans[r.plan_id]["status"] == "entry_failed" and len(b.submits) == 1
    assert j.calls.count(("result", "OLN", "rejected", None)) == 1


def test_a_slow_submission_that_lands_within_the_grace_period_is_adopted_and_protected(env):
    store, b, j = env
    b.rules = [{"when": lambda x: x["side"] == "buy", "mode": "lost"}]
    r = do_enter(store, b, j)
    b._make(b.submits[0])                                        # the order reaches the broker after all
    rec(store, b, j)
    p = store.plans[r.plan_id]
    assert p["status"] == "open" and p["protection"] == "active" and p["held_qty"] == 399
    assert sum(1 for s in b.submits if s["side"] == "buy") == 1


def test_restart_recovers_an_interrupted_submission_and_never_resubmits(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker(), FakeJournal()
    r = do_enter(store, b, j)
    p = store.plans[r.plan_id]
    p["orders"][-1]["status"] = "submitting"                     # crash between persisting intent and learning the result
    p["protection"] = None
    store.save()
    n = len(b.submits)
    fresh = Store(tmp_path)                                      # process restart: state from disk
    rec(fresh, b, j)
    q = fresh.plans[r.plan_id]
    assert q["orders"][-1]["status"] == "submitted" and q["protection"] == "active"
    assert len(b.submits) == n, "existing broker protection adopted, nothing resubmitted"
    assert do_enter(fresh, b, j).reasons[0].startswith("OLN already has"), "no duplicate entry after restart"


# 5 -------------------------------------------------------------------------------------------------
def _reply(action, **extra):
    return json.dumps({"holdings": [{"ticker": "OLN", "action": action, "reason": "thesis check",
                                     "evidence": ["maverick 2026-10-16 trend intact"], **extra}],
                       "entries": [], "no_entry_reason": "nothing qualifies"})


def _cycle(store, b, j, reply, shortlist=("WMG",), status="ok", hermes_error=None, seen=None, during=None):
    def sel(today, session, excl):
        if seen is not None:
            seen.append(set(excl))
        if status != "ok":
            raise RuntimeError(status)
        return [{"ticker": t, "sector": "", "name": ""} for t in shortlist], "ok", {"universe": list(WL)}

    def hermes(prompt):
        if seen is not None:
            seen.append(prompt)
        if during:
            during()
        if hermes_error:
            raise hermes_error
        return reply() if callable(reply) else reply, "sha"
    return run_cycle(store, b, j, review=True, selector_fn=sel, hermes_fn=hermes, sleep=NOSLEEP)


def _state(prompt):
    return json.loads(prompt.split("===== CURRENT STATE (reconciled with the broker just now) =====\n")[1]
                      .rsplit("\nResearch with", 1)[0])


def test_hold_beyond_five_days_is_never_auto_liquidated(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.held_sessions = 8
    seen = []
    out = _cycle(store, b, j, _reply("HOLD"), seen=seen)
    assert out["holdings"] == [{"ticker": "OLN", "action": "HOLD", "problems": []}]
    assert store.plans[pid]["status"] == "open" and not b.sells()
    h = _state(seen[-1])["holdings"][0]
    assert h["trading_days_held"] == 8 and h["past_intended_horizon"] is False


def test_early_exit_cancels_protection_then_sells_exactly_the_broker_quantity(env):
    store, b, j = env
    pid = opened(store, b, j)
    _cycle(store, b, j, _reply("EXIT"))
    (sell,) = b.sells()
    assert sell["type"] == "market" and sell["qty"] == "399"
    assert b.cancels == [ox(engine.cid(pid, "protect", 0))]
    p = store.plans[pid]
    assert p["status"] == "closed" and p["exit"]["kind"] == "agent" and p["exit"]["qty"] == 399
    assert ("exit", "OLN", "agent_requested", 399) in j.calls and "OLN" not in b.pos


def test_justified_horizon_extension_and_raise_only_stops_are_confirmed_by_the_broker(env):
    store, b, j = env
    pid = opened(store, b, j)
    _cycle(store, b, j, _reply("REVISE", revision={"horizon_trading_days": 25, "stop_price": 47.5}))
    p = store.plans[pid]
    assert p["original"]["horizon_trading_days"] == 10 and p["original"]["stop_price"] == 46.0
    assert p["revisions"][0]["horizon_trading_days"] == 25 and p["revisions"][0]["status"] == "confirmed"
    (live,) = b.oco()
    assert live["legs"][0]["stop_price"] == "47.50" and live["client_order_id"] == engine.cid(pid, "protect", 1)
    assert p["protected"]["stop"] == 47.5 and p["protected"]["client_order_id"] == live["client_order_id"]
    errs = revise(store, b, pid, {"reason": "give it room", "evidence": ["x"], "stop_price": 44.0}, 50.0, "t")
    assert errs and "only be raised" in errs[0] and len(p["revisions"]) == 1
    assert revise(store, b, pid, {"reason": "r", "horizon_trading_days": 40}, 50.0, "t"), "extension needs evidence"


# 5b: exits that are pending / partial / rejected / lost, across cycles and restarts -------------------
def test_pending_exit_is_not_resold_or_reprotected_across_cycles_and_restart(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker(), FakeJournal()
    pid = opened(store, b, j)
    b.exit_mode = "pending"
    exit_position(store, b, j, pid, "thesis broke", TODAY, NOSLEEP)
    p = store.plans[pid]
    assert p["protection"] == "exiting" and p["status"] == "open" and len(b.sells()) == 1
    n = len(b.submits)
    for _ in range(3):                                           # every pass is a fresh process
        rec(Store(tmp_path), b, j)
        assert exit_position(Store(tmp_path), b, j, pid, "again", TODAY, NOSLEEP) == []
    assert len(b.submits) == n, "no second SELL and no competing protection over shares committed to the exit"
    assert len(b.oco()) == 0 and j.calls.count(("exit", "OLN", "agent_requested", 399)) == 0
    b.complete(engine.cid(pid, "exit", 0))
    store = Store(tmp_path)
    rec(store, b, j)
    p = store.plans[pid]
    assert p["status"] == "closed" and p["exit"]["kind"] == "agent" and p["exit"]["qty"] == 399
    assert j.calls.count(("exit", "OLN", "agent_requested", 399)) == 1
    rec(store, b, j)
    assert j.calls.count(("exit", "OLN", "agent_requested", 399)) == 1


def test_partial_exit_is_journaled_as_partial_and_the_remainder_is_protected(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker(), FakeJournal()
    pid = opened(store, b, j)
    b.exit_mode = "partial_open"
    exit_position(store, b, j, pid, "thesis broke", TODAY, NOSLEEP)
    n = len(b.submits)
    rec(store, b, j)
    p = store.plans[pid]
    assert p["status"] == "open" and p["protection"] == "exiting" and len(b.submits) == n
    b.finish(engine.cid(pid, "exit", 0), "canceled")             # the market order ends short: 99 of 399 sold
    rec(Store(tmp_path), b, j)
    p = Store(tmp_path).plans[pid]
    assert p["status"] == "open" and p["held_qty"] == 300 and p["protection"] == "active"
    assert p["exit_request"]["status"] == "incomplete"
    assert ("exit", "OLN", "agent_requested", 99) in j.calls, "the ACTUAL fill is journaled"
    assert not any(c[0] == "exit" and c[3] == 399 for c in j.calls), "never reported as a complete exit"
    (live,) = b.oco()
    assert live["qty"] == "300"


def test_rejected_exit_restores_full_protection(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.exit_mode = "rejected"
    exit_position(store, b, j, pid, "thesis broke", TODAY, NOSLEEP)
    p = store.plans[pid]
    assert p["status"] == "open" and p["protection"] == "active" and p["exit_request"]["status"] == "incomplete"
    (live,) = b.oco()
    assert live["qty"] == "399" and not any(c[0] == "exit" for c in j.calls)
    n = len(b.submits)
    rec(store, b, j)
    assert len(b.submits) == n, "a refused exit is not retried by the controller on its own"


def test_lost_exit_submission_does_not_loop_and_keeps_protection(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.rules = [{"when": lambda x: x["side"] == "sell" and x["type"] == "market", "mode": "lost"}]
    exit_position(store, b, j, pid, "thesis broke", TODAY, NOSLEEP)
    p = store.plans[pid]
    assert p["exit_request"]["status"] == "incomplete" and p["protection"] == "active"
    assert len(b.sells()) == 1 and len(b.oco()) == 1
    rec(store, b, j)
    assert len(b.sells()) == 1


def test_ambiguous_exit_submission_is_found_by_client_id_and_not_resold(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.rules = [{"when": lambda x: x["side"] == "sell" and x["type"] == "market", "mode": "ambiguous"}]
    exit_position(store, b, j, pid, "thesis broke", TODAY, NOSLEEP)
    assert len(b.sells()) == 1 and store.plans[pid]["status"] == "closed"


def test_interrupted_exit_continues_after_restart_without_a_second_decision(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker(), FakeJournal()
    pid = opened(store, b, j)
    b.rules = [{"when": lambda x: x["side"] == "sell" and x["type"] == "market", "mode": "crash"}]
    with pytest.raises(Crash):
        exit_position(store, b, j, pid, "thesis broke", TODAY, NOSLEEP)
    assert Store(tmp_path).plans[pid]["exit_request"]["status"] == "requested"
    assert b.oco() == [], "protection was cancelled before the crash"
    rec(Store(tmp_path), b, j)
    p = Store(tmp_path).plans[pid]
    assert p["status"] == "closed" and p["exit"]["kind"] == "agent" and len(b.sells()) == 2
    assert b.sells()[0]["client_order_id"] == b.sells()[1]["client_order_id"], "same client id: idempotent"


# 5c: cancellation races -----------------------------------------------------------------------------
def test_protective_fill_during_exit_cancellation_means_no_second_sell(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.cancel_plan = ["stop_fills"]                               # the stop fills while we try to cancel
    exit_position(store, b, j, pid, "thesis broke", TODAY, NOSLEEP)
    p = store.plans[pid]
    assert b.sells() == [], "nothing left to sell: no excess SELL"
    assert p["status"] == "closed" and p["exit"]["kind"] == "stop"
    assert store.data["stopped_out"] == {TODAY: ["OLN"]}
    assert ("exit", "OLN", "harness_stop", 399) in j.calls


def test_failed_cancellation_blocks_the_sell_until_it_is_confirmed(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.cancel_plan = ["ignore", "ignore"]                         # this pass and the immediate follow-up reconcile
    exit_position(store, b, j, pid, "thesis broke", TODAY, NOSLEEP)
    p = store.plans[pid]
    assert b.sells() == [] and len(b.oco()) == 1, "the stop is still live at the broker, so nothing else is sold"
    assert p["protection"] == "active" and p["exit_request"]["status"] == "requested"
    rec(store, b, j)                                             # next cycle: the cancel lands, the persisted exit proceeds
    assert len(b.sells()) == 1 and store.plans[pid]["status"] == "closed"
    assert len(b.sells()) == 1


# 5d: protection replacement ----------------------------------------------------------------------------
def test_rejected_stop_replacement_restores_the_previous_protection(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.rules = [{"when": lambda x: x.get("order_class") == "oco" and x["stop_loss"]["stop_price"] == "47.50",
                "mode": "lost"}]
    revise(store, b, pid, {"reason": "lock in gains", "evidence": ["x"], "stop_price": 47.5}, 50.0, "t", NOSLEEP)
    p = store.plans[pid]
    assert p["revisions"][0]["status"] == "rejected" and p["protection"] == "active"
    assert p["original"]["stop_price"] == 46.0 and engine.current_plan(p)["stop_price"] == 46.0
    (live,) = b.oco()
    assert live["legs"][0]["stop_price"] == "46.00" and live["qty"] == "399"
    view = holding_view(p, b.pos["OLN"], 3)
    assert view["protection_confirmed"]["stop"] == 46.0 and view["revisions"][0]["status"] == "rejected"


def test_ambiguous_replacement_that_landed_is_verified_and_confirmed(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.rules = [{"when": lambda x: x.get("order_class") == "oco" and x["stop_loss"]["stop_price"] == "47.50",
                "mode": "ambiguous"}]
    revise(store, b, pid, {"reason": "lock in gains", "evidence": ["x"], "stop_price": 47.5}, 50.0, "t", NOSLEEP)
    p = store.plans[pid]
    assert p["revisions"][0]["status"] == "confirmed" and p["protected"]["stop"] == 47.5
    assert len(b.oco()) == 1 and p["protected"]["client_order_id"].endswith("protect1")


def test_replacement_interrupted_by_a_crash_completes_after_restart(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker(), FakeJournal()
    pid = opened(store, b, j)
    b.rules = [{"when": lambda x: x.get("order_class") == "oco" and x["stop_loss"]["stop_price"] == "47.50",
                "mode": "crash"}]
    with pytest.raises(Crash):
        revise(store, b, pid, {"reason": "lock in gains", "evidence": ["x"], "stop_price": 47.5}, 50.0, "t", NOSLEEP)
    s2 = Store(tmp_path)
    assert s2.plans[pid]["revisions"][0]["status"] == "requested", "requested, NOT presented as effective"
    assert s2.plans[pid]["protected"]["stop"] == 46.0 and b.oco() == []
    rec(s2, b, j)
    p = Store(tmp_path).plans[pid]
    assert p["revisions"][0]["status"] == "confirmed" and p["protection"] == "active" and p["protected"]["stop"] == 47.5
    (live,) = b.oco()
    assert live["qty"] == "399" and live["legs"][0]["stop_price"] == "47.50"


def test_unconfirmed_cancellation_sends_no_replacement_and_keeps_the_request_unconfirmed(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.cancel_plan = ["ignore"]
    n = len(b.submits)
    revise(store, b, pid, {"reason": "lock in gains", "evidence": ["x"], "stop_price": 47.5}, 50.0, "t", NOSLEEP)
    p = store.plans[pid]
    assert len(b.submits) == n and p["revisions"][0]["status"] == "requested" and p["protection"] == "unconfirmed"
    assert p["protected"]["stop"] == 46.0 and "OLN" in engine.unprotected_holdings(store)
    rec(store, b, j)
    assert p["revisions"][0]["status"] == "confirmed" and p["protected"]["stop"] == 47.5 and len(b.oco()) == 1


def test_a_protective_fill_during_replacement_cancellation_leaves_nothing_to_resubmit(env):
    store, b, j = env
    pid = opened(store, b, j)
    b.cancel_plan = ["stop_fills"]
    revise(store, b, pid, {"reason": "lock in gains", "evidence": ["x"], "stop_price": 47.5}, 50.0, "t", NOSLEEP)
    assert len(b.submits) == 2, "no new protection over a position that no longer exists"
    rec(store, b, j)
    assert store.plans[pid]["status"] == "closed" and store.plans[pid]["exit"]["kind"] == "stop"


def test_protection_is_only_claimed_when_quantity_and_prices_match_the_broker(env):
    store, b, j = env
    pid = opened(store, b, j)
    live = b.oco()[0]
    live["legs"][0]["stop_price"] = "45.00"                      # broker-side drift: status open, but the wrong stop
    rec(store, b, j)
    p = store.plans[pid]
    (now_live,) = b.oco()
    assert now_live["legs"][0]["stop_price"] == "46.00" and p["protected"]["stop"] == 46.0
    now_live["qty"] = "300"                                      # wrong size
    rec(store, b, j)
    (again,) = b.oco()
    assert again["qty"] == "399"


# 6 -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("leg, prov", [("stop", "harness_stop"), ("target", "harness_target")])
def test_broker_stop_or_target_exit_reconciles_with_the_journal(env, leg, prov):
    store, b, j = env
    pid = opened(store, b, j)
    b.trigger("OLN", leg)
    rep = rec(store, b, j)
    p = store.plans[pid]
    assert p["status"] == "closed" and p["exit"]["kind"] == leg and rep["exits"] == [{"ticker": "OLN", "kind": leg}]
    assert ("exit", "OLN", prov, 399) in j.calls
    rec(store, b, j)
    assert j.calls.count(("exit", "OLN", prov, 399)) == 1 and not b.sells()
    if leg == "stop":
        assert any("§9.4" in x for x in do_enter(store, b, j).reasons), "no same-day re-entry after a stop-out"


# 7 -------------------------------------------------------------------------------------------------
def test_shortlist_rotation_keeps_holdings_and_pending_orders_managed(tmp_path):
    store, b, j = Store(tmp_path), FakeBroker(), FakeJournal()
    pid = opened(store, b, j)
    b.entry, b.cancel_plan = "delayed_partial", ["ignore"] * 20
    do_enter(store, b, j, plan(ticker="WMG"), shortlist=("WMG",))         # pending, partially filled, cancel not landing
    for sl in (("ATI", "BRX"), ("SFM",)):
        seen = []
        _cycle(store, b, j, _reply("HOLD"), shortlist=sl, seen=seen)
        excluded, prompt = seen[0], seen[1]
        assert {"OLN", "WMG"} <= excluded, "held and pending names never enter selection"
        assert '"ticker": "OLN"' in prompt and store.plans[pid]["protection"] == "active"
    wmg = next(p for p in store.plans.values() if p["ticker"] == "WMG")
    assert wmg["status"] == "entry_pending", "pending order stays independently reconciled"
    assert "WMG already has" in do_enter(store, b, j, plan(ticker="WMG"), shortlist=("WMG",)).reasons[0]


# 8 -------------------------------------------------------------------------------------------------
def test_selector_failure_blocks_entries_but_not_management(env):
    store, b, j = env
    pid = opened(store, b, j)
    reply = json.dumps({"holdings": [{"ticker": "OLN", "action": "EXIT", "reason": "adverse news"}],
                        "entries": [plan(ticker="WMG")]})
    out = _cycle(store, b, j, reply, status="laya worker down")
    assert out["entries"][0]["ok"] is False and "shortlist" in out["entries"][0]["reasons"][0]
    assert store.plans[pid]["status"] == "closed", "exit still executed"


def test_model_failure_leaves_protection_and_reconciliation_intact(env):
    store, b, j = env
    pid = opened(store, b, j)
    out = _cycle(store, b, j, "", hermes_error=TimeoutError("hermes timeout"))
    assert out["hermes"] == "failed" and store.plans[pid]["protection"] == "active"
    out = _cycle(store, b, j, "I think we should buy everything")
    assert out["hermes"].startswith("rejected reply") and len(b.submits) == 2


def test_a_position_closed_by_the_broker_during_research_is_not_sold_again(env):
    store, b, j = env
    pid = opened(store, b, j)
    out = _cycle(store, b, j, _reply("EXIT"), during=lambda: b.trigger("OLN", "target"))
    assert out["holdings"][0]["problems"] == ["no longer an open position"] and not b.sells()
    assert store.plans[pid]["exit"]["kind"] == "target"


# 8b: execution freshness across a long research call ----------------------------------------------------
def _entry_reply(**over):
    return json.dumps({"holdings": [], "entries": [plan(ticker="WMG", **over)]})


def test_a_long_research_call_cannot_make_an_expired_quote_pass(env):
    store, b, j = env
    b.quote_ts = (NOW - timedelta(seconds=5)).isoformat()        # a quote taken before Hermes started
    out = _cycle(store, b, j, _entry_reply(), shortlist=("WMG",),
                 during=lambda: setattr(b, "now", b.now + timedelta(seconds=600)))
    (e,) = out["entries"]
    assert not e["ok"] and "stale quote" in e["reasons"][0] and b.submits == []


def test_future_dated_quote_is_rejected_after_research(env):
    store, b, j = env
    b.quote_ts = (NOW + timedelta(seconds=90)).isoformat()
    out = _cycle(store, b, j, _entry_reply(), shortlist=("WMG",))
    assert "future-dated" in out["entries"][0]["reasons"][0] and b.submits == []


def test_execution_uses_the_clock_at_submission_and_keeps_the_research_cutoff_separate(env):
    store, b, j = env

    def research():
        b.now += timedelta(seconds=600)                          # Hermes took ten minutes; the quote feed is live
    out = _cycle(store, b, j, _entry_reply(), shortlist=("WMG",), during=research)
    (e,) = out["entries"]
    assert e["ok"], e
    ev = next(iter(store.plans.values()))["evidence_at_entry"]
    assert parse_ts(ev["executed_at"]) - parse_ts(ev["research_cutoff"]) >= timedelta(seconds=600)
    assert parse_ts(ev["quote"]["timestamp"]) >= parse_ts(ev["research_cutoff"]) + timedelta(seconds=590)


def test_evidence_that_ages_out_during_validation_is_rejected_before_sending(env):
    store, b, j = env
    b.tick, b.quote_ts = 125, (NOW - timedelta(seconds=5)).isoformat()   # each clock read is two minutes later
    r = do_enter(store, b, j)
    assert not r.ok and "expired during validation" in r.reasons[0] and b.submits == []


def test_news_published_during_research_returns_the_candidate_for_reassessment(env):
    store, b, j = env

    def research():
        b.news_result = {"complete": True, "articles": [{"created_at": (NOW + timedelta(seconds=240)).isoformat()}]}
        b.now += timedelta(seconds=300)
    out = _cycle(store, b, j, _entry_reply(), shortlist=("WMG",), during=research)
    assert not out["entries"][0]["ok"] and b.submits == []
    assert store.data["reassess"] == ["WMG"]
    seen = []
    b.news_result = {"complete": True, "articles": []}
    _cycle(store, b, j, json.dumps({"holdings": [], "entries": [], "no_entry_reason": "rechecking"}),
           shortlist=("WMG",), seen=seen)
    assert _state(seen[-1])["reassess"]["tickers"] == ["WMG"] and "reassess" not in store.data


# 8c: competing controllers / lock ----------------------------------------------------------------------
def test_overlapping_cycles_are_prevented(tmp_path):
    with CycleLock(tmp_path / "cycle.lock"):
        with pytest.raises(CycleBusy):
            with CycleLock(tmp_path / "cycle.lock"):
                pass
    with CycleLock(tmp_path / "cycle.lock"):                         # released after the first
        pass


def test_a_slow_but_live_cycle_never_loses_its_lock_to_a_file_age_threshold(tmp_path):
    lock = tmp_path / "cycle.lock"
    with CycleLock(lock):
        old = time.time() - 7 * 24 * 3600
        os.utime(lock, (old, old))                                   # a week-old mtime means nothing now
        with pytest.raises(CycleBusy):
            with CycleLock(lock):
                pass
    with CycleLock(lock):
        pass


def test_a_lock_held_by_another_process_is_respected_and_released_when_it_dies(tmp_path):
    lock = tmp_path / "cycle.lock"
    holder = subprocess.Popen([sys.executable, "-c",
                               "import sys,time; sys.path.insert(0, sys.argv[1]); from engine import CycleLock; "
                               "l = CycleLock(sys.argv[2]); l.__enter__(); print('held', flush=True); time.sleep(60)",
                               str(HERE), str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(CycleBusy):
            with CycleLock(lock):
                pass
    finally:
        holder.kill()
        holder.wait()
    with CycleLock(lock):                                            # the OS released it with the process
        pass


def test_a_stale_snapshot_cannot_overwrite_newer_state(tmp_path):
    first, second = Store(tmp_path), Store(tmp_path)                 # two controllers loaded the same state
    b, j = FakeBroker(), FakeJournal()
    r = do_enter(first, b, j)
    assert r.ok
    with pytest.raises(StaleState):
        second.save()
    with pytest.raises(StaleState):
        do_enter(second, b, j, plan(ticker="WMG"))
    assert r.plan_id in json.loads((tmp_path / "live_state.json").read_text())["plans"]
    assert sum(1 for s in b.submits if s["side"] == "buy") == 1


def test_sequential_controllers_and_a_restart_preserve_every_plan_and_intent(tmp_path):
    b, j = FakeBroker(), FakeJournal()
    with CycleLock(tmp_path / "cycle.lock"):
        s1 = Store(tmp_path)
        r1 = do_enter(s1, b, j)
    with CycleLock(tmp_path / "cycle.lock"):                         # a second instance loads state inside the lock
        s2 = Store(tmp_path)
        assert r1.plan_id in s2.plans and not do_enter(s2, b, j).ok
        r2 = do_enter(s2, b, j, plan(ticker="WMG"))
    with CycleLock(tmp_path / "cycle.lock"):                         # restart
        s3 = Store(tmp_path)
        rec(s3, b, j)
    assert {r1.plan_id, r2.plan_id} <= set(Store(tmp_path).plans)
    assert [s["symbol"] for s in b.submits if s["side"] == "buy"] == ["OLN", "WMG"]


def test_review_schedule_follows_the_broker_session():
    clock = lambda h, m: {"is_open": True, "timestamp": f"2026-10-08T{h:02d}:{m:02d}:00-04:00"}  # noqa: E731
    sess = {"open": "09:30", "close": "16:00"}
    assert review_slot(clock(9, 45), sess, []) is None
    assert review_slot(clock(10, 5), sess, []) == "2026-10-08-30"
    assert review_slot(clock(10, 5), sess, ["2026-10-08-30"]) is None
    assert review_slot(clock(15, 45), sess, []) == "2026-10-08-end"
    assert review_slot(clock(12, 50), {"open": "09:30", "close": "13:00"}, []) == "2026-10-08-end"   # half day
    assert review_slot({"is_open": False, "timestamp": "2026-10-08T20:00:00-04:00"}, sess, []) is None


# 9 -------------------------------------------------------------------------------------------------
def test_historical_defaults_and_shared_instructions_are_unchanged():
    repo = HERE.parent
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "RULES.md", "AGENTS.md", "SOUL.md",
                           "risk-checker/risk_checker.py", "watchlist/watchlist.json", "docker-compose.yml"],
                          cwd=repo, capture_output=True, text=True).stdout.strip()
    assert diff == "", diff
    assert engine.LIMITS.risk_budget_pct == 2.0 and engine.LIMITS.max_stop_pct == 12.0
    src = (repo / "backtest-harness/run_backtest.py").read_text(encoding="utf-8")
    assert "live/" not in src and "LIVE_POLICY" not in src, "the backtest never reads the live policy"


# 10 -------------------------------------------------------------------------------------------------
def test_live_hermes_profile_whitelists_research_tools_only():
    import run_live
    base = "model:\n  default: x\nagent:\n  max_turns: 1\nplatform_toolsets:\n  cli: []\nmcp_servers: {}\nsession_reset:\n  mode: none\n"
    cfg = run_live.live_profile_config(base)
    assert "max_turns: 60" in cfg and "cli: []" in cfg and "mcp_servers: {}" not in cfg
    listed = {t for tools in run_live.LIVE_MCP_TOOLS.values() for t in tools}
    assert all(t in cfg for t in listed)
    banned = ("portfolio_", "screening_", "backtesting_", "research_", "clear_market_cache", "place_order",
              "record_", "execution", "trade-journal", "terminal")
    assert not any(b in t for t in listed for b in banned) and not any(b in cfg for b in ("execution", "trade-journal"))
    with pytest.raises(SystemExit):
        run_live.live_profile_config(base.replace("mcp_servers: {}", "mcp_servers:\n  x: {}"))
