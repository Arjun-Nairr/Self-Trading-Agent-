"""
execution-sim-mcp — paper-trading fallback for the real Upstox Sandbox tool
(blocked on an Upstox-side OTP delivery issue as of this build).

Deliberately shaped to match what a real broker execution tool's interface
looks like (place_order(ticker, action, quantity, order_type, ...)) so
swapping the real Upstox sandbox in later is a config change (point
mcp_servers.execution at the new container) — nothing upstream (Hermes'
judgment, the risk-checker, trade-journal wiring) has to change, because
none of it talks to this tool's internals, only to place_order's
inputs/outputs.

Stateless single-shot fill logic — no order book, no waiting:
  MARKET: fills immediately at reference_price minus a small fixed
    slippage against the trade direction.
  LIMIT: fills immediately if reference_price already satisfies
    limit_price, else rejected outright (a real limit order could instead
    sit in the book waiting — that's real broker behavior we do NOT have
    here; see the ponytail note below).

reference_price is supplied by the caller (Hermes), not fetched by this
tool — Hermes already calls maverick-mcp's get_quote itself. Keeping tools
hub-and-spoke (only Hermes talks to each tool; tools never call each
other) matches §12B's isolation topology and matches how a real broker
tool works too: you place an order against a price you already know, the
broker doesn't look it up for you.
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
    action: str, order_type: str, reference_price: float, limit_price: float | None
) -> tuple[str, float | None, str | None]:
    """Returns (status, fill_price, rejection_reason)."""
    if order_type.upper() == "MARKET":
        slippage = reference_price * (SLIPPAGE_BPS / 10_000)
        fill_price = reference_price + slippage if action.upper() == "BUY" else reference_price - slippage
        return "filled", round(fill_price, 2), None

    if order_type.upper() == "LIMIT":
        if limit_price is None:
            return "rejected", None, "LIMIT order requires limit_price"
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
) -> dict[str, Any]:
    """
    Simulate placing a paper order. action: BUY or SELL. order_type: MARKET
    or LIMIT. reference_price: the current price you already looked up
    (e.g. via maverick-mcp's get_quote) — this tool does not fetch its own
    price. Returns a broker-order-shaped result: status, fill_price,
    fill_quantity, broker_order_id, rejection_reason.
    """
    status, fill_price, reason = _simulate_fill(action, order_type, reference_price, limit_price)
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
