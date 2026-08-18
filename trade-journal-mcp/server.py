"""
trade-journal-mcp — Neon-backed trade journal + insight log + activity feed.

Seven tools: record_decision, record_execution_result, get_trade_history,
log_insight, get_insight_history, log_activity, get_recent_activity.

log_activity/get_recent_activity are step-by-step narration (fetching
data, reasoning, checking risk, placing an order) — separate from trades
(outcomes) and insight_log (weekly reflection prose). This is what a live
"what is Hermes up to" dashboard view reads from. Stays idle/empty until
SOUL.md is written to actually call log_activity.

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


_VALID_PRIMARY_SIGNALS = {"technical", "sentiment", "both", "other"}
_VALID_TECHNICAL_BASES = {"rsi", "macd", "support_resistance", "trend", "multiple", "other"}


def _validate_decision_fields(
    action: str,
    confidence: int,
    primary_signal: str,
    expected_reward_risk: float,
    invalidation_condition: str,
    strategy_notes: str | None,
    technical_basis: str | None,
) -> None:
    """Pure validation, no DB -- the "report card" fields are required for
    every real decision (per RULES.md), and the 'other' escape hatch must
    never be a free pass: choosing it requires actually explaining what it
    was in strategy_notes, or 'other' just becomes a way to skip the whole
    point of structured logging. Raises ValueError naming exactly which
    rule failed; callers never get a silently-accepted bad row.
    """
    if not 1 <= confidence <= 5:
        raise ValueError(f"confidence must be 1-5, got {confidence}")
    if primary_signal not in _VALID_PRIMARY_SIGNALS:
        raise ValueError(
            f"primary_signal must be one of {sorted(_VALID_PRIMARY_SIGNALS)}, "
            f"got {primary_signal!r}"
        )
    if expected_reward_risk <= 0:
        raise ValueError(f"expected_reward_risk must be > 0, got {expected_reward_risk}")
    if not invalidation_condition or not invalidation_condition.strip():
        raise ValueError("invalidation_condition is required and cannot be blank")

    # RULES.md §5a: sentiment can only VETO an entry, never trigger one --
    # a BUY driven purely by sentiment would violate that rule structurally.
    # (SELL is exempt: §6d's thesis-invalidation exit is legitimately
    # sentiment-only -- adverse news alone is a valid reason to close.)
    if action == "BUY" and primary_signal == "sentiment":
        raise ValueError(
            "primary_signal='sentiment' is not valid on a BUY -- RULES.md "
            "§5a: sentiment may only veto an entry, never trigger one"
        )

    needs_technical_basis = primary_signal in ("technical", "both")
    if needs_technical_basis and technical_basis is None:
        raise ValueError(
            f"technical_basis is required when primary_signal={primary_signal!r}"
        )
    if technical_basis is not None and technical_basis not in _VALID_TECHNICAL_BASES:
        raise ValueError(
            f"technical_basis must be one of {sorted(_VALID_TECHNICAL_BASES)}, "
            f"got {technical_basis!r}"
        )

    # The escape hatch is only legitimate if it's actually explained --
    # otherwise 'other' becomes a way to write nothing at all.
    chose_other = primary_signal == "other" or technical_basis == "other"
    if chose_other and (not strategy_notes or not strategy_notes.strip()):
        raise ValueError(
            "strategy_notes must explain the choice whenever primary_signal "
            "or technical_basis is 'other'"
        )


async def _record_decision(
    ticker: str,
    action: str,
    intended_quantity: int,
    intended_notional: float,
    order_type: str,
    confidence: int,
    primary_signal: str,
    expected_reward_risk: float,
    invalidation_condition: str,
    limit_price: float | None = None,
    technical_basis: str | None = None,
    strategy_notes: str | None = None,
    data_timestamps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_decision_fields(
        action, confidence, primary_signal, expected_reward_risk,
        invalidation_condition, strategy_notes, technical_basis,
    )
    decision_id = str(uuid.uuid4())
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO trades (decision_id, ticker, action, intended_quantity,
                             intended_notional, order_type, limit_price,
                             confidence, primary_signal, technical_basis,
                             expected_reward_risk, invalidation_condition,
                             strategy_notes, data_timestamps)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        RETURNING *
        """,
        decision_id, ticker, action, intended_quantity, intended_notional,
        order_type, limit_price, confidence, primary_signal, technical_basis,
        expected_reward_risk, invalidation_condition, strategy_notes,
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


async def _log_activity(
    event_type: str, description: str, decision_id: str | None = None
) -> dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO agent_activity (decision_id, event_type, description) VALUES ($1, $2, $3) RETURNING *",
        decision_id, event_type, description,
    )
    return dict(row)


async def _get_recent_activity(
    decision_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    pool = await get_pool()
    if decision_id:
        rows = await pool.fetch(
            "SELECT * FROM agent_activity WHERE decision_id = $1 ORDER BY created_at DESC LIMIT $2",
            decision_id, limit,
        )
    else:
        rows = await pool.fetch("SELECT * FROM agent_activity ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


def _jsonable(obj):
    """asyncpg returns datetime/Decimal objects FastMCP's JSON encoder chokes on."""
    return json.loads(json.dumps(obj, default=str))


@mcp.tool()
async def record_decision(
    ticker: str, action: str, intended_quantity: int, intended_notional: float,
    order_type: str, confidence: int, primary_signal: str,
    expected_reward_risk: float, invalidation_condition: str,
    limit_price: float | None = None, technical_basis: str | None = None,
    strategy_notes: str | None = None, data_timestamps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an intended trade before attempting execution. Returns the row
    including a fresh decision_id — pass that same id to record_execution_result.

    The "report card" fields are required for every real decision, so this
    stays queryable across hundreds of cycles instead of relying on reading
    prose by hand:
      confidence: how sure you are, 1 (low) to 5 (high) -- rate it honestly,
        this only has value if it actually correlates with real outcomes.
      primary_signal: 'technical', 'sentiment', 'both', or 'other'. What
        actually drove this decision, per RULES.md §5a (technicals trigger,
        sentiment only vetoes -- 'sentiment' alone should be rare).
      technical_basis: required when primary_signal is 'technical' or
        'both' -- which maverick-mcp signal ('rsi', 'macd',
        'support_resistance', 'trend', 'multiple', or 'other').
      expected_reward_risk: the actual ratio you calculated (target distance
        ÷ stop distance) -- must be ≥ 2.0 per RULES.md §6b.
      invalidation_condition: one sentence -- what specific thing happening
        would prove this trade wrong. Be concrete (a price level, a signal
        reversing), not vague ("if it goes down").

    'other' (for primary_signal or technical_basis) is a legitimate answer,
    never a shortcut -- if you use it, strategy_notes MUST explain what it
    actually was, or the call is rejected. Put any reasoning beyond the
    structured fields in strategy_notes -- it stays free-form on purpose."""
    return _jsonable(await _record_decision(
        ticker, action, intended_quantity, intended_notional, order_type,
        confidence, primary_signal, expected_reward_risk, invalidation_condition,
        limit_price, technical_basis, strategy_notes, data_timestamps,
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
    execution_mode: 'alpaca' (real broker), 'backtest' (execution-sim-mcp's
    historical-fill mode, real backtest volume), or 'simulated' (execution-
    sim-mcp used ad hoc for manual dev testing, NOT backtest data) — tag
    honestly and don't default to 'simulated' during a real backtest run,
    or its rows become indistinguishable from stray manual-test rows. This
    is how a future reflection review tells which trades were shaped by
    the simulator's simplified fill logic."""
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


@mcp.tool()
async def log_activity(
    event_type: str, description: str, decision_id: str | None = None
) -> dict[str, Any]:
    """Narrate a step as you work through a cycle — fetching data,
    reasoning, checking risk, placing an order, updating the journal.
    event_type is free-form (e.g. "data_fetch", "reasoning", "risk_check",
    "order_placed") — pass decision_id once one exists for this trade, omit
    it for steps that happen before a decision is made (e.g. pulling a
    quote). This is what powers a live "what is Hermes doing right now"
    view — call it at every meaningful step, not just at the end."""
    return _jsonable(await _log_activity(event_type, description, decision_id))


@mcp.tool()
async def get_recent_activity(
    decision_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List recent activity-log entries, optionally filtered to one
    decision_id's full step-by-step trail."""
    return _jsonable(await _get_recent_activity(decision_id, limit))


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
