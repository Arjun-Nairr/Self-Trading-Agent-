"""
trade-journal-mcp — Neon-backed trade journal + insight log.

Five tools: record_decision, record_execution_result, get_trade_history,
log_insight, get_insight_history.

Idempotency (§6b of the plan): a cron cycle can die between placing an
order and recording the result. record_execution_result is written so a
retry with the same decision_id never corrupts state — it only updates a
row that's still 'pending'; if the row was already resolved by an earlier
attempt, it just returns that existing result instead of a second write.
On restart, Hermes is expected to call get_trade_history(status="pending")
and reconcile each one against real broker state before doing anything
else (§6b) — decision_id already exists in that row, nothing needs to be
regenerated or remembered across the crash.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from fastmcp import FastMCP

mcp = FastMCP("trade-journal")

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.environ["NEON_DATABASE_URL"], min_size=1, max_size=5)
    return _pool


async def _record_decision(
    ticker: str,
    action: str,
    intended_quantity: int,
    intended_notional: float,
    order_type: str,
    limit_price: float | None = None,
    strategy_notes: str | None = None,
    data_timestamps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision_id = str(uuid.uuid4())
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO trades (decision_id, ticker, action, intended_quantity,
                             intended_notional, order_type, limit_price,
                             strategy_notes, data_timestamps)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING *
        """,
        decision_id, ticker, action, intended_quantity, intended_notional,
        order_type, limit_price, strategy_notes,
        json.dumps(data_timestamps) if data_timestamps else None,
    )
    return dict(row)


async def _record_execution_result(
    decision_id: str,
    status: str,
    broker_order_id: str | None = None,
    fill_price: float | None = None,
    fill_quantity: int | None = None,
    rejection_reason: str | None = None,
    execution_mode: str = "simulated",
) -> dict[str, Any]:
    pool = await get_pool()
    # Only resolves a row that's still 'pending' — a retry with the same
    # decision_id after the first attempt already succeeded is a no-op
    # here, not a second write.
    row = await pool.fetchrow(
        """
        UPDATE trades
        SET status = $2, broker_order_id = $3, fill_price = $4,
            fill_quantity = $5, rejection_reason = $6, execution_mode = $7, updated_at = now()
        WHERE decision_id = $1 AND status = 'pending'
        RETURNING *
        """,
        decision_id, status, broker_order_id, fill_price, fill_quantity, rejection_reason, execution_mode,
    )
    if row is None:
        # Already resolved by an earlier attempt (or unknown decision_id) —
        # return the current state instead of silently doing nothing.
        row = await pool.fetchrow("SELECT * FROM trades WHERE decision_id = $1", decision_id)
        if row is None:
            raise ValueError(f"unknown decision_id: {decision_id}")
    return dict(row)


async def _get_trade_history(
    ticker: str | None = None, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    pool = await get_pool()
    conditions, params = [], []
    if ticker:
        params.append(ticker)
        conditions.append(f"ticker = ${len(params)}")
    if status:
        params.append(status)
        conditions.append(f"status = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM trades {where} ORDER BY created_at DESC LIMIT ${len(params)}", *params
    )
    return [dict(r) for r in rows]


async def _log_insight(cycle_type: str, insight_text: str) -> dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO insight_log (cycle_type, insight_text) VALUES ($1, $2) RETURNING *",
        cycle_type, insight_text,
    )
    return dict(row)


async def _get_insight_history(cycle_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    pool = await get_pool()
    if cycle_type:
        rows = await pool.fetch(
            "SELECT * FROM insight_log WHERE cycle_type = $1 ORDER BY created_at DESC LIMIT $2",
            cycle_type, limit,
        )
    else:
        rows = await pool.fetch("SELECT * FROM insight_log ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


def _jsonable(obj):
    """asyncpg returns datetime/Decimal objects FastMCP's JSON encoder chokes on."""
    return json.loads(json.dumps(obj, default=str))


@mcp.tool()
async def record_decision(
    ticker: str, action: str, intended_quantity: int, intended_notional: float,
    order_type: str, limit_price: float | None = None,
    strategy_notes: str | None = None, data_timestamps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an intended trade before attempting execution. Returns the row
    including a fresh decision_id — pass that same id to record_execution_result."""
    return _jsonable(await _record_decision(
        ticker, action, intended_quantity, intended_notional, order_type,
        limit_price, strategy_notes, data_timestamps,
    ))


@mcp.tool()
async def record_execution_result(
    decision_id: str, status: str, broker_order_id: str | None = None,
    fill_price: float | None = None, fill_quantity: int | None = None,
    rejection_reason: str | None = None, execution_mode: str = "simulated",
) -> dict[str, Any]:
    """Record the outcome of attempting to execute decision_id. Safe to call
    more than once with the same decision_id — only the first call that finds
    the row still 'pending' writes; later calls return the resolved result.
    execution_mode: 'simulated' (execution-sim tool) or 'upstox_sandbox' (real
    broker) — tag honestly, this is how a future reflection review tells
    which trades were shaped by the simulator's simplified fill logic."""
    return _jsonable(await _record_execution_result(
        decision_id, status, broker_order_id, fill_price, fill_quantity,
        rejection_reason, execution_mode,
    ))


@mcp.tool()
async def get_trade_history(
    ticker: str | None = None, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """List past trades, optionally filtered by ticker/status. Use
    status="pending" on startup to reconcile against real broker state."""
    return _jsonable(await _get_trade_history(ticker, status, limit))


@mcp.tool()
async def log_insight(cycle_type: str, insight_text: str) -> dict[str, Any]:
    """Record reflective prose during a decision or weekly_reflection cycle,
    before drafting any proposed_skills/ change."""
    return _jsonable(await _log_insight(cycle_type, insight_text))


@mcp.tool()
async def get_insight_history(cycle_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """List past insight-log entries, optionally filtered by cycle_type."""
    return _jsonable(await _get_insight_history(cycle_type, limit))


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
