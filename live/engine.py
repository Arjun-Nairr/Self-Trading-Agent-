"""Deterministic live-paper execution (live/LIVE_POLICY.md §5-§6).

The LLM never places orders. Hermes researches and returns plans/decisions;
this module owns everything that touches the broker. It is a durable state
machine: every step persists its intent first, then asks the broker, and every
later `reconcile()` (no LLM, any number of times, after any restart) moves the
state forward from broker reality.

  entry      entry_pending -> open | entry_failed
             An IOC limit entry is not assumed terminal. Filled shares are
             recognised as soon as the broker reports them; an unresolved
             entry is cancelled (a working buy blocks a protective sell on the
             same symbol) and, once resolved, the filled quantity is protected.
  protection one broker-held OCO (stop + target) sized to the shares held and
             not committed to an exit. It counts as protection only when the
             broker reports it OPEN with the intended quantity AND prices.
             Changing it is cancel -> confirm -> re-read positions -> submit.
  exit       exit intent is persisted, protection cancelled and confirmed,
             then one market SELL for the shares actually held. Pending,
             partial, rejected and ambiguous exits are settled across cycles;
             nothing else sells or re-protects shares committed to it.
  revision   a requested stop/target change is `requested` until the broker's
             replacement protection is verified, `rejected` if it is refused.

Why IOC entries + a separate OCO: Alpaca bracket exit legs only activate once
the entry is COMPLETELY filled. Broker-held stops do not guarantee the planned
loss across a price gap, and cancel -> replace leaves a short interval without
protection (the position is flagged and new entries pause until verified).

`broker`, `journal` and the sleep function are injected, so every path is
testable with deterministic fakes.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "risk-checker"))
from risk_checker import (  # noqa: E402
    AccountState, Context, MarketClock, Position, ProposedTrade, RiskLimits, check_trade,
)

from plans import current_plan, validate_entry_plan, validate_revision  # noqa: E402

ET = ZoneInfo("America/New_York")
LIMITS = RiskLimits()                      # the existing RULES.md numbers, unchanged
QUOTE_MAX_AGE_S = 120                      # execution-price evidence must be this fresh
QUOTE_FUTURE_TOLERANCE_S = 5               # clock skew allowed before a quote counts as future-dated
NEWS_LOOKBACK_H = 72
ENTRY_LIMIT_BUFFER = 0.002                 # IOC limit = ask * (1 + 0.2%): price-bounded, never chases
OPEN_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding", "held", "partially_filled",
                 "pending_replace", "replaced", "calculated", "stopped", "pending_cancel"}
TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day", "suspended"}
POLL_S, POLL_N = 1.0, 10
PRICE_TOL = 0.005
ENTRY_GRACE_S = 120                        # an entry the broker cannot find is only declared failed after this


def _f(x: Any) -> float:
    return float(x) if x not in (None, "") else 0.0


def cid(plan_id: str, kind: str, n: int = 0) -> str:
    """Deterministic Alpaca client_order_id: a retry of the same intent can
    never become a second broker order (Alpaca rejects a reused id)."""
    return f"lv-{plan_id[:20]}-{kind}{n}"


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")


def parse_ts(s: Any) -> datetime | None:
    """One strict timestamp parser for all execution evidence (quotes, news,
    clock): RFC 3339 with an explicit offset, any fractional precision.
    Anything else is None -- callers treat unparseable evidence as invalid."""
    if isinstance(s, datetime):
        return s if s.tzinfo else None
    m = _TS.match(s.strip()) if isinstance(s, str) else None
    if not m:
        return None
    day, hms, frac, tz = m.groups()
    tz = "+00:00" if tz == "Z" else (tz if ":" in tz else tz[:3] + ":" + tz[3:])
    try:
        return datetime.fromisoformat(f"{day}T{hms}{(frac or '')[:7]}{tz}")
    except ValueError:
        return None


def broker_now(broker) -> tuple[dict, datetime | None]:
    c = broker.clock()
    return c, parse_ts(c.get("timestamp"))


def quote_problem(q: dict | None, now: datetime) -> str | None:
    if not q:
        return "no quote"
    t = parse_ts(q.get("timestamp"))
    if t is None:
        return "quote timestamp missing or invalid"
    if _f(q.get("ask")) <= 0:
        return "quote has no positive ask"
    age = (now - t).total_seconds()
    if age < -QUOTE_FUTURE_TOLERANCE_S:
        return f"quote is future-dated ({-age:.0f}s ahead of the broker clock)"
    if age > QUOTE_MAX_AGE_S:
        return f"stale quote ({age:.0f}s old, max {QUOTE_MAX_AGE_S}s)"
    return None


# -- private state ------------------------------------------------------------------------------

class StaleState(RuntimeError):
    """The state file changed since this Store loaded it."""


class Store:
    """Private, git-ignored live state (atomic JSON) + append-only event log.

    Load one Store per cycle, INSIDE the cycle lock. `save()` refuses to
    overwrite a file that another writer changed since the load (revision
    check), so a stale snapshot can never silently erase newer state."""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path, self.events = self.dir / "live_state.json", self.dir / "events.jsonl"
        self.data = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists()
                     else {"plans": {}, "stopped_out": {}, "unmanaged": {}})
        self.rev = self.data.get("rev", 0)

    def save(self) -> None:
        if self.path.exists() and json.loads(self.path.read_text(encoding="utf-8")).get("rev", 0) != self.rev:
            raise StaleState(f"{self.path.name} changed on disk since this process loaded it; refusing to overwrite")
        self.rev += 1
        self.data["rev"] = self.rev
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def event(self, name: str, /, **body: Any) -> None:
        with open(self.events, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "event": name, **body}, default=str) + "\n")

    @property
    def plans(self) -> dict:
        return self.data["plans"]


class CycleBusy(RuntimeError):
    pass


class CycleLock:
    """One decision/execution cycle at a time: an OS-level exclusive lock held
    on an open file for the whole cycle. It is released only by the holder
    (or by the OS when the holder dies), never by file age -- a slow but live
    cycle cannot lose it, and a crashed one cannot keep it."""

    def __init__(self, path: Path):
        self.path, self.fd = Path(path), None

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise CycleBusy(f"another live cycle holds {self.path.name}") from None
        os.lseek(fd, 1, os.SEEK_SET)               # byte 0 is the lock byte on Windows
        os.write(fd, f"{os.getpid()}".encode())
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            os.close(self.fd)                      # POSIX: closing releases the flock
            self.fd = None


# -- broker helpers -----------------------------------------------------------------------------

def _poll(broker, client_order_id: str, sleep: Callable[[float], None]) -> dict | None:
    """Bounded wait for a terminal state. May return a non-terminal order:
    callers must not assume resolution."""
    o = broker.order_by_client_id(client_order_id)
    for _ in range(POLL_N):
        if o is None or o["status"] in TERMINAL_STATUSES:
            return o
        sleep(POLL_S)
        o = broker.order_by_client_id(client_order_id)
    return o


def _held(broker, ticker: str) -> int:
    return int(_f(next((p["qty"] for p in broker.positions() if p["symbol"] == ticker), 0)))


def _intents(plan: dict, kind: str) -> list[dict]:
    return [i for i in plan.get("orders", []) if i["kind"] == kind]


def _next_cid(plan: dict, kind: str) -> str:
    """A fresh generation id -- or, when the last attempt never reached the
    broker, the SAME id (idempotent: the broker rejects a duplicate)."""
    ints = _intents(plan, kind)
    return ints[-1]["client_order_id"] if ints and ints[-1]["status"] == "not_found" \
        else cid(plan["id"], kind, len(ints))


def _submit(store: Store, broker, plan: dict, kind: str, body: dict) -> dict | None:
    """Persist the intent BEFORE sending; after any error ask the broker by
    client_order_id before concluding anything (never blind-retry). Returns
    the broker's order, or None when unconfirmed (the intent is recovered by a
    later reconcile)."""
    c = body["client_order_id"]
    intent = next((i for i in plan.setdefault("orders", []) if i["client_order_id"] == c), None)
    if intent is not None and intent["status"] != "not_found":
        return broker.order_by_client_id(c)               # already sent: never resend
    if intent is None:
        intent = {"kind": kind, "client_order_id": c}
        plan["orders"].append(intent)
    intent.update(status="submitting", body=body)
    store.save()
    try:
        order = broker.submit(body)
    except Exception as exc:
        store.event("submit_error", client_order_id=c, error=f"{type(exc).__name__}: {exc}"[:300])
        try:
            order = broker.order_by_client_id(c)
        except Exception:
            return None                                    # still "submitting": recovered later
    intent["status"] = "submitted" if order else "not_found"
    intent["broker_order_id"] = order["id"] if order else None
    store.save()
    store.event("order_" + intent["status"], plan_id=plan["id"], kind=kind, client_order_id=c,
                broker_status=order["status"] if order else None)
    return order


def _recover_intents(store: Store, broker, plan: dict) -> None:
    """Interrupted or unconfirmed submissions: ask the broker by client id."""
    for intent in plan.get("orders", []):
        if intent["status"] in ("submitting", "not_found"):
            try:
                o = broker.order_by_client_id(intent["client_order_id"])
            except Exception:
                continue
            if o or intent["status"] == "submitting":
                intent["status"] = "submitted" if o else "not_found"
                intent["broker_order_id"] = o["id"] if o else None
                store.event("recovered_intent", plan_id=plan["id"], client_order_id=intent["client_order_id"],
                            found=bool(o))


def _stop_leg(order: dict | None) -> dict | None:
    if not order:
        return None
    if order.get("type") in ("stop", "stop_limit"):
        return order
    return next((leg for leg in order.get("legs") or [] if leg.get("type") in ("stop", "stop_limit")), None)


def _protect_body(plan: dict, qty: int, client_order_id: str) -> dict:
    cur = current_plan(plan)
    return {"symbol": plan["ticker"], "qty": str(qty), "side": "sell", "type": "limit", "time_in_force": "gtc",
            "order_class": "oco", "take_profit": {"limit_price": f"{cur['target_price']:.2f}"},
            "stop_loss": {"stop_price": f"{cur['stop_price']:.2f}"}, "client_order_id": client_order_id}


def _matches(order: dict | None, symbol: str, qty: int, stop: float, target: float) -> bool:
    """The broker reports an OPEN sell protection for exactly this quantity at
    exactly these prices -- status alone is not protection."""
    if not order or order["status"] not in OPEN_STATUSES or order.get("side") != "sell" \
            or order.get("symbol") != symbol or int(_f(order.get("qty"))) != qty:
        return False
    leg = _stop_leg(order)
    if not leg or leg["status"] not in OPEN_STATUSES or abs(_f(leg.get("stop_price")) - stop) > PRICE_TOL:
        return False
    return order is leg or abs(_f(order.get("limit_price")) - target) <= PRICE_TOL


def _filled_leg(order: dict | None) -> dict | None:
    if not order:
        return None
    for o in [order, *(order.get("legs") or [])]:
        if o["status"] == "filled" or _f(o.get("filled_qty")) > 0:
            return o
    return None


def _day_of(ts: Any, fallback: str) -> str:
    t = parse_ts(ts)
    return t.astimezone(ET).date().isoformat() if t else fallback


def _open_protection(broker, plan: dict) -> list[dict]:
    out = []
    for i in _intents(plan, "protect"):
        if i["status"] == "submitted":
            o = broker.order_by_client_id(i["client_order_id"])
            if o and o["status"] in OPEN_STATUSES:
                out.append(o)
    return out


def _cancel_confirmed(broker, orders: list[dict], sleep) -> bool:
    """Cancel, then require each order to be resolved. False = still working."""
    for o in orders:
        broker.cancel(o["id"])
    return not any((r := _poll(broker, o["client_order_id"], sleep)) and r["status"] in OPEN_STATUSES
                   for o in orders)


# -- protection -----------------------------------------------------------------------------------

def _protect(store: Store, broker, plan: dict, sleep) -> None:
    """Drive the broker's protection to match the plan for the shares actually
    held. Never submits while a conflicting protective order is unresolved."""
    for _ in range(2):
        held = _held(broker, plan["ticker"])
        plan["held_qty"] = held
        if held <= 0:
            return                                           # caller closes the plan
        want = current_plan(plan)
        stop, target = want["stop_price"], want["target_price"]
        live = _open_protection(broker, plan)
        if len(live) == 1 and _matches(live[0], plan["ticker"], held, stop, target):
            _mark_protected(plan, live[0], held, stop, target)
            return
        if not _cancel_confirmed(broker, live, sleep):
            plan["protection"] = "unconfirmed"
            store.event("protection_cancel_unconfirmed", plan_id=plan["id"])
            return                                           # retry next pass; nothing conflicting is sent
        if live:                                             # a stop/target may have filled while cancelling
            held = _held(broker, plan["ticker"])
            plan["held_qty"] = held
            if held <= 0:
                return
        body = _protect_body(plan, held, _next_cid(plan, "protect"))
        order = _submit(store, broker, plan, "protect", body)
        fresh = broker.order_by_client_id(body["client_order_id"]) if order else None
        if _matches(fresh, plan["ticker"], held, float(body["stop_loss"]["stop_price"]),
                    float(body["take_profit"]["limit_price"])):
            _mark_protected(plan, fresh, held, stop, target)
            return
        pending = [r for r in plan.get("revisions", []) if r.get("status") == "requested"]
        if not pending:
            break
        for r in pending:                                    # the broker did not take the change: restore
            r["status"], r["rejected_reason"] = "rejected", (fresh or {}).get("status", "not_accepted")
        store.event("revision_rejected", plan_id=plan["id"], count=len(pending))
    plan["protection"] = "unprotected"


def _mark_protected(plan: dict, order: dict, qty: int, stop: float, target: float) -> None:
    plan["protection"] = "active"
    plan["protected"] = {"client_order_id": order["client_order_id"], "order_id": order["id"], "qty": qty,
                         "stop": stop, "target": target}
    for r in plan.get("revisions", []):
        if r.get("status") == "requested":
            r["status"], r["confirmed_at"] = "confirmed", datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- reconciliation (no LLM) ----------------------------------------------------------------------

def _advance_entry(store: Store, broker, journal, plan: dict, sleep) -> None:
    entry = _intents(plan, "entry")[0]
    if entry["status"] == "not_found":
        _, now = broker_now(broker)
        created = parse_ts(plan["created"])
        if now is None or created is None or (now - created).total_seconds() < ENTRY_GRACE_S:
            return                                           # a slow submission may still land: look again next pass
        plan["status"], plan["entry_result"] = "entry_failed", {"broker_status": "not_found", "filled_qty": 0}
        journal.result(plan, "rejected", reason="entry order never reached the broker")
        return
    if entry["status"] != "submitted":
        return
    o = broker.order_by_client_id(entry["client_order_id"])
    if o is None:
        store.event("entry_order_missing", plan_id=plan["id"])
        return
    if _f(o.get("filled_qty")) > 0:                          # fills are real as soon as the broker reports them
        plan["filled_qty"] = int(_f(o["filled_qty"]))
    if o["status"] not in TERMINAL_STATUSES:                 # IOC is not assumed terminal: resolve what is outstanding
        broker.cancel(o["id"])
        o = _poll(broker, entry["client_order_id"], sleep) or o
    filled = int(_f(o.get("filled_qty")))
    plan["filled_qty"] = filled
    plan["entry_result"] = {"broker_status": o["status"], "filled_qty": filled, "requested_qty": int(_f(o.get("qty"))),
                            "avg_price": _f(o.get("filled_avg_price")) or None,
                            "partial": filled < int(_f(o.get("qty")))}
    if o["status"] not in TERMINAL_STATUSES:
        plan["protection"] = "unprotected" if filled else None
        store.event("entry_unresolved", plan_id=plan["id"], broker_status=o["status"], filled_qty=filled)
        return
    if filled == 0:
        plan["status"] = "entry_failed"
        journal.result(plan, "rejected", reason=f"entry {o['status']}")
        return
    plan["status"] = "open"
    journal.result(plan, "filled", broker_order_id=o["id"], fill_price=plan["entry_result"]["avg_price"],
                   fill_qty=filled)


def _settle_exits(store: Store, broker, journal, plan: dict) -> None:
    """Record each terminal agent exit order's ACTUAL fill; a short or refused
    exit leaves the remainder to be protected, never reported as exited."""
    req = plan.get("exit_request") or {}
    for idx, i in enumerate(_intents(plan, "exit")):
        if i["status"] != "submitted" or i.get("settled"):
            continue
        o = broker.order_by_client_id(i["client_order_id"])
        if o is None or o["status"] not in TERMINAL_STATUSES:
            continue
        filled, asked = int(_f(o.get("filled_qty"))), int(_f(o.get("qty")))
        i["settled"], i["result"] = True, {"broker_status": o["status"], "filled_qty": filled, "requested_qty": asked,
                                           "avg_price": _f(o.get("filled_avg_price")) or None}
        if filled:
            journal.exit(plan, "agent_requested", {"key": f"exit{idx}", "qty": filled, "requested": asked,
                                                   "price": i["result"]["avg_price"], "kind": "agent"})
        if req:
            req["status"] = "incomplete" if filled < asked else "done"
        store.event("exit_settled", plan_id=plan["id"], **i["result"])


def _exit_pending(plan: dict) -> bool:
    return any(i["status"] == "submitting" or (i["status"] == "submitted" and not i.get("settled"))
               for i in _intents(plan, "exit"))


def _close(store: Store, broker, plan: dict, journal, today: str) -> str:
    """The position is gone at the broker. Say how, from the broker's records."""
    leg = None
    for i in reversed(_intents(plan, "protect")):
        if i["status"] == "submitted":
            leg = _filled_leg(broker.order_by_client_id(i["client_order_id"]))
            if leg:
                break
    exits = [i for i in _intents(plan, "exit") if (i.get("result") or {}).get("filled_qty")]
    kind = ("stop" if leg.get("type") in ("stop", "stop_limit") else "target") if leg \
        else "agent" if exits else "external"
    price, qty = _f((leg or {}).get("filled_avg_price")) or None, int(_f((leg or {}).get("filled_qty"))) or None
    if kind == "agent":
        qty = sum(i["result"]["filled_qty"] for i in exits)
        price = sum(i["result"]["filled_qty"] * (i["result"]["avg_price"] or 0) for i in exits) / qty
    on = _day_of((leg or {}).get("filled_at"), today)
    plan["status"], plan["protection"] = "closed", None
    plan["exit"] = {"kind": kind, "price": price, "qty": qty or plan.get("held_qty"), "on": on,
                    "reason": (plan.get("exit_request") or {}).get("reason")}
    if kind == "stop":
        store.data["stopped_out"].setdefault(on, []).append(plan["ticker"])
    if kind != "agent":                                      # agent exits were journaled per order when settled
        journal.exit(plan, {"stop": "harness_stop", "target": "harness_target"}.get(kind, "legacy_unknown"),
                     {"key": f"close-{kind}", "qty": plan["exit"]["qty"], "price": price, "kind": kind})
    store.event("position_closed", plan_id=plan["id"], ticker=plan["ticker"], kind=kind)
    return kind


def reconcile(store: Store, broker, journal, today: str, sleep=time.sleep) -> dict:
    """Bring private state in line with the broker. Safe to run any number of
    times, in any order relative to restarts; never places an entry."""
    report = {"protected": [], "unprotected": [], "exits": [], "unmanaged": []}
    for plan in list(store.plans.values()):
        if plan["status"] in ("closed", "entry_failed"):
            continue
        _recover_intents(store, broker, plan)
        if plan["status"] == "entry_pending":
            _advance_entry(store, broker, journal, plan, sleep)
        if plan["status"] != "open":
            if plan["status"] == "entry_pending" and plan.get("filled_qty"):
                report["unprotected"].append(plan["ticker"])
            continue
        _settle_exits(store, broker, journal, plan)
        if _exit_pending(plan):
            plan["protection"] = "exiting"                   # committed to a working exit: no competing orders
            continue
        req = plan.get("exit_request")
        if req and req["status"] == "requested":             # an interrupted exit continues; nothing is re-decided
            _drive_exit(store, broker, journal, plan, sleep)
            if _exit_pending(plan):
                continue
            _settle_exits(store, broker, journal, plan)
        held = _held(broker, plan["ticker"])
        plan["held_qty"] = held
        if held > 0:
            _protect(store, broker, plan, sleep)
        if plan["held_qty"] == 0:
            report["exits"].append({"ticker": plan["ticker"], "kind": _close(store, broker, plan, journal, today)})
            continue
        (report["protected"] if plan["protection"] == "active" else report["unprotected"]).append(plan["ticker"])
    managed = {p["ticker"] for p in store.plans.values() if p["status"] in ("open", "entry_pending")}
    store.data["unmanaged"] = {p["symbol"]: p["qty"] for p in broker.positions() if p["symbol"] not in managed}
    report["unmanaged"] = sorted(store.data["unmanaged"])
    store.save()
    return report


# -- entries ------------------------------------------------------------------------------------

@dataclass
class EntryResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    plan_id: str | None = None
    reassess: bool = False             # new evidence arrived after the decision: research it again


def _reserved_cash(broker) -> float:
    """Cash already committed to open BUY orders (not yet in positions)."""
    return sum(_f(o.get("qty")) * _f(o.get("limit_price") or o.get("notional"))
               for o in broker.orders() if o.get("side") == "buy" and o["status"] in OPEN_STATUSES)


def size(equity: float, cash_available: float, entry: float, stop: float) -> int:
    """§2a stop-based size, §2b concentration cap, §3 available cash -- the
    smallest wins; computed in code, never by the model."""
    if entry <= stop or equity <= 0:
        return 0
    return max(0, math.floor(min(LIMITS.risk_budget_pct / 100 * equity / (entry - stop),
                                 LIMITS.max_position_pct / 100 * equity / entry,
                                 cash_available / entry)))


def unprotected_holdings(store: Store) -> list[str]:
    """Held shares without broker-verified protection (or an entry still
    unresolved with fills): new entries pause until this is empty."""
    return sorted(p["ticker"] for p in store.plans.values()
                  if (p["status"] == "open" and p.get("protection") != "active")
                  or (p["status"] == "entry_pending" and p.get("filled_qty")))


def enter(store: Store, broker, journal, raw_plan: dict, *, shortlist: list[str], universe: list[str],
          decided_at: str, today: str, sleep=time.sleep) -> EntryResult:
    """Validate and execute one BUY plan against evidence gathered NOW.
    `decided_at` is the research cutoff (when Hermes's evidence was taken); it
    is not execution time, and news newer than it was never reviewed."""
    errs = validate_entry_plan(raw_plan)
    if errs:
        return EntryResult(False, errs)
    t = raw_plan["ticker"].upper()
    if t not in shortlist:
        return EntryResult(False, [f"{t} is not on today's Laya shortlist (entries are shortlist-driven)"])
    if any(p["ticker"] == t and p["status"] in ("entry_pending", "open") for p in store.plans.values()):
        return EntryResult(False, [f"{t} already has a live plan/position/pending order"])
    if unprot := unprotected_holdings(store):
        return EntryResult(False, [f"new entries paused: holdings without verified broker protection {unprot}"])
    cutoff = parse_ts(decided_at)
    if cutoff is None:
        return EntryResult(False, ["research cutoff timestamp invalid"])
    clock, now = broker_now(broker)                          # execution time is read NOW, after any research
    if now is None:
        return EntryResult(False, ["broker clock timestamp invalid"])
    if not clock["is_open"]:
        return EntryResult(False, ["§7: market is closed"])
    q = broker.latest_quote(t)
    if bad := quote_problem(q, now):
        return EntryResult(False, [f"no fresh quote for {t}: {bad}"])
    limit = round(_f(q["ask"]) * (1 + ENTRY_LIMIT_BUFFER), 2)
    n = raw_plan["stop_basis"]["lookback_sessions"]
    bars = broker.daily_bars(t, n)
    if not bars or len(bars) < n:
        return EntryResult(False, ["observed level could not be verified (no/short daily bars)"])
    observed = round(min(_f(b["low"]) for b in bars[-n:]), 2)
    if abs(observed - raw_plan["stop_basis"]["level"]) > 0.01:
        return EntryResult(False, [f"stop_basis level {raw_plan['stop_basis']['level']} is not the observed "
                                   f"{n}-session low {observed}"])
    try:
        news = broker.news(t, NEWS_LOOKBACK_H)
    except Exception as exc:
        return EntryResult(False, [f"news verification failed: {type(exc).__name__}"])
    if not news or not news.get("complete"):
        return EntryResult(False, ["news verification incomplete/unavailable -- unavailable news never reads as clear"])
    unseen = [a for a in news["articles"] if (ts := parse_ts(a.get("created_at"))) is None or ts > cutoff]
    if unseen:                                               # undated counts as unseen: never silently passes
        return EntryResult(False, [f"{len(unseen)} headline(s) published after the research cutoff (or undated) -- "
                                   "reassess with the new evidence"], reassess=True)
    acct = broker.account()
    positions = broker.positions()
    avail = _f(acct["cash"]) - _reserved_cash(broker)
    qty = size(_f(acct["equity"]), avail, limit, raw_plan["stop_price"])
    if qty < 1:
        return EntryResult(False, ["sizing gives 0 shares under §2a/§2b/§3 with current cash and reservations"])
    pending = [Position(ticker=p["ticker"], quantity=1, market_value=0.0)
               for p in store.plans.values() if p["status"] == "entry_pending"]
    verdict = check_trade(
        ProposedTrade(ticker=t, action="BUY", quantity=qty, entry_price=limit,
                      stop_price=raw_plan["stop_price"], target_price=raw_plan["target_price"]),
        Context(account=AccountState(equity=_f(acct["equity"]), cash=avail, daily_pl_pct=acct.get("daily_pl_pct"),
                                     weekly_pl_pct=acct.get("weekly_pl_pct"),
                                     drawdown_from_peak_pct=acct.get("drawdown_from_peak_pct"),
                                     trading_blocked=bool(acct.get("trading_blocked"))),
                clock=MarketClock(is_open=True),
                positions=[Position(ticker=p["symbol"], quantity=_f(p["qty"]), market_value=_f(p["market_value"]))
                           for p in positions] + pending,
                watchlist=universe, stopped_out_today=store.data["stopped_out"].get(today, [])))
    if not verdict.allowed:
        return EntryResult(False, verdict.violations)
    clock, now = broker_now(broker)                          # validation took time: re-check just before sending
    bad = quote_problem(q, now) if now else "broker clock timestamp invalid"
    if bad or not clock["is_open"]:
        return EntryResult(False, [f"evidence expired during validation: {bad or 'market closed'}"])
    plan_id = uuid.uuid4().hex
    plan = {"id": plan_id, "ticker": t, "status": "entry_pending", "created": now.isoformat(),
            "original": {k: raw_plan[k] for k in raw_plan}, "revisions": [], "filled_qty": 0,
            "sizing": {"qty": qty, "limit": limit, "equity": _f(acct["equity"]), "cash_available": round(avail, 2),
                       "risk_usd": round(qty * (limit - raw_plan["stop_price"]), 2),
                       "notional_usd": round(qty * limit, 2)},
            "evidence_at_entry": {"quote": q, "observed_low": observed, "news_articles": len(news["articles"]),
                                  "research_cutoff": decided_at, "executed_at": now.isoformat()}}
    store.plans[plan_id] = plan
    journal.decision(plan)
    order = _submit(store, broker, plan, "entry", {
        "symbol": t, "qty": str(qty), "side": "buy", "type": "limit", "limit_price": f"{limit:.2f}",
        "time_in_force": "ioc", "client_order_id": cid(plan_id, "entry")})
    if order is not None:
        _poll(broker, cid(plan_id, "entry"), sleep)
    reconcile(store, broker, journal, today, sleep)
    return EntryResult(True, [], plan_id)


# -- holdings -----------------------------------------------------------------------------------

def _drive_exit(store: Store, broker, journal, plan: dict, sleep) -> list[str]:
    """Advance a persisted exit request one safe step at a time."""
    if _exit_pending(plan):
        return []
    clock, _ = broker_now(broker)
    if not clock["is_open"]:
        return ["§7: market closed -- the broker-held protection stays in place"]
    if not _cancel_confirmed(broker, _open_protection(broker, plan), sleep):
        plan["protection"] = "unconfirmed"
        return ["protection did not cancel in time; not selling (no double exit)"]
    held = _held(broker, plan["ticker"])                     # re-read AFTER cancellation: protection may have filled
    plan["held_qty"] = held
    if held <= 0:
        return []                                            # reconcile closes the plan from broker facts
    plan["protection"] = "exiting"
    body = {"symbol": plan["ticker"], "qty": str(held), "side": "sell", "type": "market", "time_in_force": "day",
            "client_order_id": _next_cid(plan, "exit")}
    order = _submit(store, broker, plan, "exit", body)
    if order is not None:
        _poll(broker, body["client_order_id"], sleep)
    elif _intents(plan, "exit")[-1]["status"] == "not_found":   # never reached the broker: do not loop on it
        plan["exit_request"]["status"] = "incomplete"
        store.event("exit_not_sent", plan_id=plan["id"])
    _settle_exits(store, broker, journal, plan)
    store.save()
    return []


def exit_position(store: Store, broker, journal, plan_id: str, reason: str, today: str,
                  sleep=time.sleep) -> list[str]:
    plan = store.plans[plan_id]
    if plan["status"] != "open":
        return [f"plan is {plan['status']}, not open"]
    if (req := plan.get("exit_request")) and req["status"] == "requested":
        return []                                            # already in progress; reconcile settles it
    clock, _ = broker_now(broker)
    if not clock["is_open"]:
        return ["§7: market closed -- the broker-held protection stays in place"]
    plan["exit_request"] = {"reason": reason, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "status": "requested"}
    store.save()                                             # exit intent is durable before any broker call
    problems = _drive_exit(store, broker, journal, plan, sleep)
    reconcile(store, broker, journal, today, sleep)
    return problems


def revise(store: Store, broker, plan_id: str, rev: dict, current_price: float | None, at: str,
           sleep=time.sleep) -> list[str]:
    plan = store.plans[plan_id]
    if plan["status"] != "open":
        return [f"plan is {plan['status']}, not open"]
    errs = validate_revision(plan, rev, current_price)
    if errs:
        return errs
    prices = "stop_price" in rev or "target_price" in rev
    plan["revisions"].append({**rev, "at": at, "status": "requested" if prices else "confirmed"})
    store.save()                                             # the REQUEST is durable; it is not yet protection
    store.event("plan_revised", plan_id=plan_id, fields=sorted(k for k in rev if k not in ("reason", "evidence")))
    if prices and not _exit_pending(plan):
        _protect(store, broker, plan, sleep)
        store.save()
    return []
