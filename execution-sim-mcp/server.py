"""
execution-sim-mcp — historical fill simulator for the backtest harness.
(Originally built as a live-mode Upstox fallback; repurposed post-Alpaca —
`alpaca-mcp` is real-time-only and structurally cannot execute an order
against a past date, so this tool is what resolves fills during backtest
cycles instead. Same interface either way, so upstream code doesn't care
which mode produced a fill — see execution_mode below.)

Deliberately shaped to match a real broker tool's interface
(place_order(ticker, action, quantity, order_type, ...)) — nothing
upstream (Hermes' judgment, the risk-checker, trade-journal wiring) has to
know which execution path it's talking to.

Stateless single-shot fill logic — no order book, no waiting:
  MARKET: fills immediately at reference_price plus/minus a small fixed
    slippage against the trade direction.
  LIMIT: if day_low/day_high are supplied (backtest mode — the historical
    day's actual range), fills at limit_price when the range crosses it;
    a single reference_price is a live spot quote and can't tell you
    whether a limit order would've crossed *at some point* during a whole
    historical day, so backtest callers should pass the range, not just a
    point value. Falls back to the old point-comparison behavior
    (reference_price vs limit_price) when no range is given, e.g. live-shaped
    callers passing only a spot quote. Either way, a real limit order could
    instead sit in the book waiting across multiple days — that's real
    broker behavior we do NOT have here; see the ponytail note below.

reference_price/day_low/day_high are supplied by the caller (Hermes via
the point-in-time gated data feed), not fetched by this tool — matches
§12B's hub-and-spoke isolation (only Hermes talks to each tool; tools
never call each other).

Dashboard note: this tool's output is never read directly by the
dashboard — it only ever flows through trade-journal-mcp's
record_execution_result (execution_mode='simulated'), same as a real
alpaca-mcp fill flows through with execution_mode='alpaca'. The dashboard
reads only from that journal, so it's already decoupled from which
execution path produced a row — nothing new needed there for this tool
to be "dashboard-compatible."
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("execution-sim")

# ponytail: fixed 5bp slippage against the trade direction, not a real
# depth/liquidity model. Upgrade to volume-aware slippage if fill
# realism ever needs to be closer to a live order book — not needed to
# unblock the judgment-layer build, which is what this tool exists for.
SLIPPAGE_BPS = 5


def _simulate_fill(
    action: str,
    order_type: str,
    reference_price: float,
    limit_price: float | None,
    day_low: float | None = None,
    day_high: float | None = None,
) -> tuple[str, float | None, str | None]:
    """Returns (status, fill_price, rejection_reason)."""
    if order_type.upper() == "MARKET":
        slippage = reference_price * (SLIPPAGE_BPS / 10_000)
        fill_price = reference_price + slippage if action.upper() == "BUY" else reference_price - slippage
        if day_low is not None and day_high is not None:
            # Backtest mode: reference_price is Hermes' own stated
            # entry_price, not fetched here (see module docstring) -- a
            # stale or simply wrong figure previously filled at a price
            # that never traded that day. Clamping into the day's real
            # range is the same fix LIMIT orders already had; MARKET orders
            # had no such check at all.
            fill_price = min(max(fill_price, day_low), day_high)
        return "filled", round(fill_price, 2), None

    if order_type.upper() == "LIMIT":
        if limit_price is None:
            return "rejected", None, "LIMIT order requires limit_price"

        if day_low is not None and day_high is not None:
            # Backtest mode: did the historical day's range ever cross the
            # limit, not just where the price happened to sit at one point.
            crosses = day_low <= limit_price <= day_high
            if crosses:
                return "filled", limit_price, None
            return "rejected", None, (
                f"LIMIT not crossed: day_range=[{day_low}, {day_high}] vs "
                f"limit_price={limit_price}"
            )

        # Live/point mode: only a spot quote to compare against.
        crosses = (
            reference_price <= limit_price if action.upper() == "BUY"
            else reference_price >= limit_price
        )
        if crosses:
            return "filled", limit_price, None
        return "rejected", None, (
            f"LIMIT not crossed: reference_price={reference_price} vs limit_price={limit_price} "
            "(real broker would rest this order instead of rejecting outright — not modeled here)"
        )

    return "rejected", None, f"unsupported order_type: {order_type}"


@mcp.tool()
def place_order(
    ticker: str,
    action: str,
    quantity: int,
    order_type: str,
    reference_price: float,
    limit_price: float | None = None,
    day_low: float | None = None,
    day_high: float | None = None,
) -> dict[str, Any]:
    """
    Simulate a fill. action: BUY or SELL. order_type: MARKET or LIMIT.
    reference_price: the price you already looked up (live spot quote, or
    the historical bar's price for a backtest cycle) — this tool does not
    fetch its own price. day_low/day_high: that historical day's actual
    price range — pass these for LIMIT orders during backtesting so the
    fill check reflects whether the limit crossed at any point that day,
    not just at one snapshot price; omit for live-shaped calls where only
    a spot quote exists. Returns a broker-order-shaped result: status,
    fill_price, fill_quantity, broker_order_id, rejection_reason.
    """
    status, fill_price, reason = _simulate_fill(
        action, order_type, reference_price, limit_price, day_low, day_high
    )
    return {
        "status": status,
        "fill_price": fill_price,
        "fill_quantity": quantity if status == "filled" else None,
        "broker_order_id": f"SIM-{uuid.uuid4().hex[:12]}" if status == "filled" else None,
        "rejection_reason": reason,
        "simulated": True,
        "filled_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
