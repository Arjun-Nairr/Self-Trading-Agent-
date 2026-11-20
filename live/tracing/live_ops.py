"""Controller-side observation of broker, journal and engine-event activity inside one traced cycle.

Strictly observational and transparent: the proxies forward every attribute and call to the real object and
re-raise its exceptions unchanged; tracing work happens around the call and swallows its own errors. They are
installed only when a real tracer is active, so a tracing-disabled run touches none of this code.

What counts as what (spec: "distinguish broker orders, attempts and individual observed fills"):
- `broker.submit|cancel|replace` spans are ATTEMPTS to change broker state (kind `tool`, metadata
  `operation: mutation`), whether they succeed, fail or raise.
- `broker.order_by_client_id|orders|positions` spans are OBSERVATIONS (`operation: observation`). Seeing the same
  filled order on every reconciliation is not a new fill.
- Fills and other state transitions come only from the engine's own events (`order_filled`, `exit_settled`,
  `position_closed`, ...), mirrored as `event` observations. The engine emits those once per transition, which is
  what makes them the authority for "a fill happened".
- `journal.decision|result|exit` spans are the Neon writes (their idempotency keys are untouched).
"""
from __future__ import annotations

import contextlib
from typing import Any

MUTATIONS = ("submit", "cancel", "replace")
OBSERVATIONS = ("order_by_client_id", "orders", "positions", "latest_quote", "news", "daily_bars")
_ORDER_FIELDS = ("id", "client_order_id", "symbol", "side", "type", "qty", "status", "filled_qty",
                 "filled_avg_price", "limit_price", "stop_price", "order_class", "filled_at")


def order_view(o: Any) -> Any:
    """The correlation-relevant fields of a broker order (never account identifiers)."""
    if not isinstance(o, dict):
        return o
    v = {k: o.get(k) for k in _ORDER_FIELDS if o.get(k) is not None}
    if o.get("legs"):
        v["legs"] = [order_view(leg) for leg in o["legs"] if isinstance(leg, dict)]
    return v


def _summary(name: str, result: Any) -> Any:
    if name in ("submit", "replace", "order_by_client_id"):
        return order_view(result) if result is not None else {"found": False}
    if name == "orders":
        return {"count": len(result), "orders": [order_view(o) for o in result[:50]]}
    if name == "positions":
        return [{k: p.get(k) for k in ("symbol", "qty", "current_price", "unrealized_pl")} for p in result[:50]]
    if name == "daily_bars":
        return {"bars": len(result), "last": result[-1] if result else None}
    return result


class TracedBroker:
    def __init__(self, inner: Any, tracer: Any):
        self._inner, self._tr = inner, tracer

    def __getattr__(self, name: str) -> Any:
        if name not in MUTATIONS and name not in OBSERVATIONS:
            return getattr(self._inner, name)
        tr, inner = self._tr, self._inner

        def call(*args: Any, **kw: Any) -> Any:
            kind = "mutation" if name in MUTATIONS else "observation"
            with tr.span(f"broker.{name}", kind="tool", input={"args": list(args), **kw},
                         metadata={"operation": kind}) as sp:
                result = getattr(inner, name)(*args, **kw)        # resolved at call time: a guard may raise here
                try:
                    sp.set(output=_summary(name, result))
                except Exception:
                    pass
                return result
        return call


class TracedJournal:
    def __init__(self, inner: Any, tracer: Any):
        self._inner, self._tr = inner, tracer

    def __getattr__(self, name: str) -> Any:
        if name not in ("decision", "result", "exit"):
            return getattr(self._inner, name)
        tr, inner = self._tr, self._inner

        def call(plan: Any, *args: Any, **kw: Any) -> Any:
            ident = {"plan_id": plan.get("id"), "ticker": plan.get("ticker")} if isinstance(plan, dict) else {}
            with tr.span(f"journal.{name}", kind="tool", input={**ident, "args": list(args), **kw},
                         metadata={"operation": "journal_write"}) as sp:
                result = getattr(inner, name)(plan, *args, **kw)
                sp.set(output={"recorded": True})
                return result
        return call


@contextlib.contextmanager
def observed(store: Any, broker: Any, journal: Any, tracer: Any):
    """-> (broker, journal) to use for this cycle. With a real tracer: transparent proxies, and every engine event
    (`store.event`) also becomes an `event` observation. The store is restored on the way out."""
    if not getattr(tracer, "enabled", False):
        yield broker, journal
        return
    original = store.event

    def event(name: str, /, **body: Any) -> None:
        original(name, **body)                       # the authoritative local record, exactly as before
        try:
            tracer.event(f"engine.{name}", **body)
        except Exception:
            pass
    store.event = event
    try:
        yield TracedBroker(broker, tracer), TracedJournal(journal, tracer)
    finally:
        try:
            del store.event                          # back to the class method
        except AttributeError:
            pass
