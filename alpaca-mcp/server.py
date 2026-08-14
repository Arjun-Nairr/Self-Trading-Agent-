"""
alpaca-mcp — our own minimal execution tool over Alpaca's Paper Trading
API. Rebuilt after the official alpacahq/alpaca-mcp-server turned out to
bundle stock, crypto, and options order placement into one tool
(register_order_tools) with no config-level way to expose stocks only —
confirmed via their code and via Alpaca's own dashboard (account-level
options-trading-level setting doesn't touch crypto either). This tool
structurally can't place a crypto or options order because that code was
never written — not "disabled," just never exists.

Three tools, matching what the project's design actually needs (not the
official tool's full 65-tool surface):
  - place_order: submit a stock order (same interface as execution-sim,
    the tool this replaces as primary)
  - get_positions: real broker-side position state — §6c wants the
    broker feed as the AUTHORITATIVE source, not our own trade-journal
    bookkeeping, which could drift from reality if we have a bug
  - get_order_status: look up an order by decision_id (Alpaca's
    client_order_id) — needed for §6b's restart-reconciliation flow
    (check real broker state for any 'pending' trade-journal row before
    doing anything else after a restart)

decision_id is threaded through as Alpaca's client_order_id — a second,
broker-level idempotency backstop on top of trade-journal's own
decision_id uniqueness (§6b): our DB protects against retries at OUR
layer (crash before recording result); client_order_id protects against
a retry actually reaching Alpaca twice.
"""
import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastmcp import FastMCP

mcp = FastMCP("alpaca")

BASE_URL = "https://paper-api.alpaca.markets"
TERMINAL_STATUSES = {"filled", "partially_filled", "canceled", "rejected", "expired"}
POLL_ATTEMPTS = 6
POLL_DELAY_SECONDS = 1.5


def _headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }


async def _submit_order(
    ticker: str, action: str, quantity: int, order_type: str,
    limit_price: float | None, decision_id: str,
) -> dict[str, Any]:
    body = {
        "symbol": ticker,
        "qty": str(quantity),
        "side": action.lower(),
        "type": order_type.lower(),
        "time_in_force": "day",
        "client_order_id": decision_id,
    }
    if order_type.upper() == "LIMIT":
        if limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        body["limit_price"] = str(limit_price)

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BASE_URL}/v2/orders", json=body, headers=_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()


async def _poll_for_terminal(order_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        for _ in range(POLL_ATTEMPTS):
            resp = await client.get(f"{BASE_URL}/v2/orders/{order_id}", headers=_headers(), timeout=10)
            resp.raise_for_status()
            order = resp.json()
            if order["status"] in TERMINAL_STATUSES:
                return order
            await asyncio.sleep(POLL_DELAY_SECONDS)
        return order  # last-known, non-terminal — caller reports this honestly


def _to_journal_status(alpaca_status: str) -> str:
    if alpaca_status in ("filled", "partially_filled"):
        return "filled"
    if alpaca_status in ("rejected", "canceled", "expired"):
        return "rejected"
    return "pending"  # still resolving — reconciliation flow (§6b) picks this up later


def _parse_fill(order: dict[str, Any]) -> tuple[float | None, int | None]:
    """(fill_price, fill_quantity) from a raw Alpaca order.

    filled_qty is always present as the string "0" even when nothing's
    filled — filled_avg_price is the field that's genuinely null until a
    real fill happens, so gate both on that, not on filled_qty (which is
    truthy as "0" and would otherwise report a fake 0-share fill).
    """
    if not order.get("filled_avg_price"):
        return None, None
    return float(order["filled_avg_price"]), int(float(order["filled_qty"]))


@mcp.tool()
async def place_order(
    ticker: str,
    action: str,
    quantity: int,
    order_type: str,
    decision_id: str,
    limit_price: float | None = None,
) -> dict[str, Any]:
    """
    Place a real paper stock order via Alpaca. action: BUY or SELL.
    order_type: MARKET or LIMIT. decision_id (pass trade-journal's
    decision_id) becomes Alpaca's client_order_id for broker-level
    idempotency — always pass it. Returns: status, fill_price,
    fill_quantity, broker_order_id, rejection_reason.
    """
    submitted = await _submit_order(ticker, action, quantity, order_type, limit_price, decision_id)
    order = await _poll_for_terminal(submitted["id"])
    status = _to_journal_status(order["status"])
    fill_price, fill_quantity = _parse_fill(order)

    return {
        "status": status,
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "broker_order_id": order["id"],
        "rejection_reason": None if status != "rejected" else f"alpaca status: {order['status']}",
        "filled_at": order.get("filled_at") or datetime.now(timezone.utc).isoformat(),
    }


@mcp.tool()
async def get_positions() -> list[dict[str, Any]]:
    """
    Real broker-side open positions — the authoritative source for
    portfolio state per §6c (not trade-journal's own bookkeeping, which
    could drift from reality if a bug exists). Returns symbol, qty, side,
    market_value, avg_entry_price, current_price, unrealized_pl per
    position.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        resp.raise_for_status()
        positions = resp.json()

    return [
        {
            "symbol": p["symbol"],
            "qty": float(p["qty"]),
            "side": p["side"],
            "market_value": float(p["market_value"]),
            "avg_entry_price": float(p["avg_entry_price"]),
            "current_price": float(p["current_price"]),
            "unrealized_pl": float(p["unrealized_pl"]),
        }
        for p in positions
    ]


@mcp.tool()
async def get_order_status(decision_id: str) -> dict[str, Any]:
    """
    Look up a real order by decision_id (Alpaca's client_order_id) — for
    §6b's restart-reconciliation flow: after any restart, check real
    broker state for every trade-journal row still marked 'pending'
    before doing anything else.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/v2/orders:by_client_order_id",
            params={"client_order_id": decision_id},
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        order = resp.json()

    fill_price, fill_quantity = _parse_fill(order)
    return {
        "status": _to_journal_status(order["status"]),
        "alpaca_status": order["status"],
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "broker_order_id": order["id"],
    }


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
