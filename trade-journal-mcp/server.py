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
        # statement_cache_size=0: NEON_DATABASE_URL is a Neon POOLED
        # (PgBouncer transaction-mode) endpoint (`-pooler` in the hostname).
        # asyncpg's default per-connection prepared-statement cache is
        # incompatible with transaction pooling -- a statement prepared on
        # one pooled backend can be reused against a different backend
        # after any DDL (e.g. this project's own migrations), surfacing as
        # asyncpg.exceptions.InvalidCachedStatementError on a query that
        # never itself changed. Found live 2026-08-28 running this
        # project's own idempotency test right after a schema migration.
        # See https://github.com/MagicStack/asyncpg/issues/750 -- the
        # documented fix is disabling the client-side cache, not retrying.
        _pool = await asyncpg.create_pool(
            os.environ["NEON_DATABASE_URL"], min_size=1, max_size=5,
            statement_cache_size=0,
        )
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


def _num_close(a: Any, b: Any, tol: float = 0.01) -> bool:
    """None-safe, tolerance-based numeric comparison -- a genuine retry
    round-trips a float through Postgres's numeric type and back, which can
    shift the last decimal place; that must never itself read as a
    'materially different payload.'"""
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


def _json_equal(a: Any, b: Any) -> bool:
    """Normalizes a DB-stored JSON value (asyncpg may hand back a str,
    depending on codec setup) against the incoming Python value before
    comparing -- key-order or str-vs-dict representation differences must
    never register as a mismatch on their own."""
    def _norm(x: Any) -> Any:
        if isinstance(x, str):
            try:
                return json.loads(x)
            except (TypeError, ValueError):
                return x
        return x
    return _norm(a) == _norm(b)


def _uuid_equal(a: Any, b: Any) -> bool:
    """None-safe UUID comparison. 2026-08-28 correctness pass (third
    pass, item 1): Neon/asyncpg returns a UUID column (e.g. `run_id`) as
    a `uuid.UUID` object, while the backtest harness always supplies a
    plain string -- comparing them with `==` directly is always False
    even for the identical value, which made every genuine identical
    retry that carried a real run_id fail with a false 'run_id' mismatch.
    Both sides are normalized to their string form before comparing."""
    if a is None or b is None:
        return a is None and b is None
    return str(a) == str(b)


def _decision_field_mismatches(
    row: "asyncpg.Record",
    *, ticker: str, action: str, intended_quantity: int, intended_notional: float,
    order_type: str, confidence: int, primary_signal: str, technical_basis: str | None,
    expected_reward_risk: float, invalidation_condition: str, strategy_notes: str | None,
    data_timestamps: dict[str, Any] | None, stop_price: float | None,
    target_price: float | None, limit_price: float | None, run_id: str | None,
) -> list[str]:
    """Every field that DEFINES the decision -- not only the four
    (ticker/action/quantity/order_type) the prior check compared. A genuine
    retry of the exact same event reproduces every one of these identically
    (all derived from the same real-world decision); returns the names of
    any that don't, so the caller can fail loudly and name exactly what
    differed instead of silently keeping the stale row."""
    checks: list[tuple[str, bool]] = [
        ("ticker", row["ticker"] == ticker),
        ("action", row["action"] == action),
        ("intended_quantity", int(row["intended_quantity"]) == int(intended_quantity)),
        ("order_type", row["order_type"] == order_type),
        ("intended_notional", _num_close(row["intended_notional"], intended_notional)),
        ("confidence", int(row["confidence"]) == int(confidence)),
        ("primary_signal", row["primary_signal"] == primary_signal),
        ("technical_basis", row["technical_basis"] == technical_basis),
        ("expected_reward_risk", _num_close(row["expected_reward_risk"], expected_reward_risk)),
        ("invalidation_condition",
         (row["invalidation_condition"] or "").strip() == (invalidation_condition or "").strip()),
        ("strategy_notes", (row["strategy_notes"] or "").strip() == (strategy_notes or "").strip()),
        ("stop_price", _num_close(row["stop_price"], stop_price)),
        ("target_price", _num_close(row["target_price"], target_price)),
        ("limit_price", _num_close(row["limit_price"], limit_price)),
        ("data_timestamps", _json_equal(row["data_timestamps"], data_timestamps)),
        ("run_id", _uuid_equal(row["run_id"], run_id)),
    ]
    return [name for name, ok in checks if not ok]


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
    decision_id: str | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """decision_id: optional caller-supplied id. Live Hermes never passes
    one (always gets a fresh uuid4, unchanged behavior). The backtest
    harness passes a DETERMINISTIC id derived from (run, day, ticker,
    action, kind) -- Phase 0 repair -- so that retrying a failed backtest
    day regenerates the SAME id for the SAME real-world event instead of a
    fresh random one. ON CONFLICT DO NOTHING + the fallback SELECT below
    make a retried write a safe no-op, mirroring
    _record_execution_result's own idempotency pattern (only resolves a
    row that's still 'pending').

    A decision_id COLLISION with a DIFFERENT payload fails loudly instead
    of silently returning the stale existing row -- see the mismatch check
    below. This should never happen for a genuine retry of the same event
    (deterministic ids are derived from the event's own identity); if it
    does, something is wrong with id derivation itself and silently
    returning the wrong trade's data would be far worse than an error."""
    _validate_decision_fields(
        action, confidence, primary_signal, expected_reward_risk,
        invalidation_condition, strategy_notes, technical_basis,
    )
    decision_id = decision_id or str(uuid.uuid4())
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO trades (decision_id, ticker, action, intended_quantity,
                             intended_notional, order_type, limit_price,
                             confidence, primary_signal, technical_basis,
                             expected_reward_risk, invalidation_condition,
                             strategy_notes, data_timestamps, stop_price,
                             target_price, run_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
        ON CONFLICT (decision_id) DO NOTHING
        RETURNING *
        """,
        decision_id, ticker, action, intended_quantity, intended_notional,
        order_type, limit_price, confidence, primary_signal, technical_basis,
        expected_reward_risk, invalidation_condition, strategy_notes,
        json.dumps(data_timestamps) if data_timestamps else None,
        stop_price, target_price, run_id,
    )
    if row is None:
        # A caller-supplied decision_id that already exists -- a retry of
        # an already-journaled event. Return the existing row instead of
        # a duplicate insert or an error -- UNLESS the incoming payload
        # describes a genuinely different real-world event under the same
        # id, which fails loudly (see docstring).
        row = await pool.fetchrow("SELECT * FROM trades WHERE decision_id = $1", decision_id)
        if row is None:
            raise ValueError(f"decision_id {decision_id} conflicted on insert but no row exists -- unexpected state")
        # 2026-08-28 correctness pass (item 3): every decision-defining
        # field, not only ticker/action/quantity/order_type -- see
        # _decision_field_mismatches.
        mismatches = _decision_field_mismatches(
            row, ticker=ticker, action=action, intended_quantity=intended_quantity,
            intended_notional=intended_notional, order_type=order_type,
            confidence=confidence, primary_signal=primary_signal,
            technical_basis=technical_basis, expected_reward_risk=expected_reward_risk,
            invalidation_condition=invalidation_condition, strategy_notes=strategy_notes,
            data_timestamps=data_timestamps, stop_price=stop_price, target_price=target_price,
            limit_price=limit_price, run_id=run_id,
        )
        if mismatches:
            raise ValueError(
                f"decision_id {decision_id} collision with a DIFFERENT payload -- "
                f"mismatched field(s): {', '.join(mismatches)}. This should never "
                "happen for a genuine retry of the same event -- failing loudly "
                "instead of silently returning a different trade's data."
            )
    return dict(row)


async def _record_portfolio_snapshot(
    mode: str,
    equity: float,
    cash: float,
    exposure_pct: float | None = None,
    drawdown_pct: float | None = None,
    day_label: str | None = None,
    open_positions: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    total_tokens: int | None = None,
    api_calls: int | None = None,
    hermes_calls_used: int | None = None,
    weekly_budget: int | None = None,
    open_positions_detail: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any] | None:
    """Returns the inserted row, or None if `cycle_id` already had a
    snapshot (ON CONFLICT DO NOTHING) -- the caller (run_backtest.py) uses
    that distinction to decide whether this cycle is genuinely new (and its
    activity narration/reflection should also be written) or already fully
    recorded by an earlier attempt (and must NOT be re-narrated/duplicated).
    A NULL cycle_id (always true for live) never conflicts -- Postgres
    treats every NULL as distinct in a UNIQUE constraint -- so live callers
    always get a real row back, unaffected by this mechanism."""
    if mode not in ("backtest", "live"):
        raise ValueError(f"mode must be 'backtest' or 'live', got {mode!r}")
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO portfolio_snapshots (mode, equity, cash, exposure_pct,
                                          drawdown_pct, day_label, open_positions,
                                          input_tokens, output_tokens, cache_read_tokens,
                                          reasoning_tokens, total_tokens, api_calls,
                                          hermes_calls_used, weekly_budget, open_positions_detail,
                                          run_id, cycle_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
        ON CONFLICT (cycle_id) DO NOTHING
        RETURNING *
        """,
        mode, equity, cash, exposure_pct, drawdown_pct, day_label, open_positions,
        input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, total_tokens,
        api_calls, hermes_calls_used, weekly_budget,
        json.dumps(open_positions_detail) if open_positions_detail is not None else None,
        run_id, cycle_id,
    )
    return dict(row) if row is not None else None


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
    ticker: str | None = None, status: str | None = None, limit: int = 20,
    execution_mode: str | None = None, run_id: str | None = None,
) -> list[dict[str, Any]]:
    """execution_mode/run_id: pushed into SQL (not filtered in Python by the
    caller) so a caller asking for "this run's backtest trades" gets
    exactly that from the query itself, not an incidentally-correct
    Python-side filter applied after the fact."""
    pool = await get_pool()
    conditions, params = [], []
    if ticker:
        params.append(ticker)
        conditions.append(f"ticker = ${len(params)}")
    if status:
        params.append(status)
        conditions.append(f"status = ${len(params)}")
    if execution_mode:
        params.append(execution_mode)
        conditions.append(f"execution_mode = ${len(params)}")
    if run_id:
        params.append(run_id)
        conditions.append(f"run_id = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM trades {where} ORDER BY created_at DESC LIMIT ${len(params)}", *params
    )
    return [dict(r) for r in rows]


async def _log_insight(
    cycle_type: str, insight_text: str, mode: str,
    sample_size: int | None = None,
    run_id: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any] | None:
    """Returns the inserted row, or None if `cycle_id` already has an
    insight (ON CONFLICT DO NOTHING) -- same duplicate-prevention purpose
    as _record_portfolio_snapshot's cycle_id handling. A NULL cycle_id
    (always true for live) never conflicts."""
    if mode not in ("backtest", "live"):
        raise ValueError(f"mode must be 'backtest' or 'live', got {mode!r}")
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO insight_log (cycle_type, insight_text, sample_size, mode, run_id, cycle_id) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (cycle_id) DO NOTHING RETURNING *",
        cycle_type, insight_text, sample_size, mode, run_id, cycle_id,
    )
    return dict(row) if row is not None else None


async def _get_insight_history(
    cycle_type: str | None = None, limit: int = 20,
    mode: str | None = None, run_id: str | None = None,
) -> list[dict[str, Any]]:
    """mode/run_id: without these, a reflection prompt or the dashboard
    could surface an insight from a DIFFERENT (discarded/reset) backtest
    run, or a live insight leaking into a training view -- see RULES.md's
    own "never let old state masquerade as current" concern, applied here."""
    pool = await get_pool()
    conditions, params = [], []
    if cycle_type:
        params.append(cycle_type)
        conditions.append(f"cycle_type = ${len(params)}")
    if mode:
        params.append(mode)
        conditions.append(f"mode = ${len(params)}")
    if run_id:
        params.append(run_id)
        conditions.append(f"run_id = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM insight_log {where} ORDER BY created_at DESC LIMIT ${len(params)}", *params
    )
    return [dict(r) for r in rows]


async def _log_activity(
    event_type: str, description: str, decision_id: str | None = None,
    mode: str | None = None, run_id: str | None = None, cycle_id: str | None = None,
    total_tokens: int | None = None, hermes_calls_used: int | None = None,
) -> dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO agent_activity (decision_id, event_type, description, mode, "
        "run_id, cycle_id, total_tokens, hermes_calls_used) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
        decision_id, event_type, description, mode, run_id, cycle_id,
        total_tokens, hermes_calls_used,
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
    stop_price: float | None = None, target_price: float | None = None,
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
    structured fields in strategy_notes -- it stays free-form on purpose.

    stop_price/target_price: the real levels set for this trade (RULES.md
    §6a/§6b) -- pass them on a BUY so the dashboard can show a still-open
    position's real stop/target, not just its entry price."""
    return _jsonable(await _record_decision(
        ticker, action, intended_quantity, intended_notional, order_type,
        confidence, primary_signal, expected_reward_risk, invalidation_condition,
        limit_price, technical_basis, strategy_notes, data_timestamps,
        stop_price=stop_price, target_price=target_price,
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
    before drafting any proposed_skills/ change.

    You only ever call this tool from a live cycle (per AGENTS.md, backtest
    cycles never call their own tools) — mode is tagged 'live' automatically,
    same as log_activity, without you needing to pass anything."""
    return _jsonable(await _log_insight(cycle_type, insight_text, mode="live"))


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
    view — call it at every meaningful step, not just at the end.

    You only ever call this tool from a live cycle (per AGENTS.md, backtest
    cycles never call their own tools) — mode is tagged 'live' automatically,
    always correctly, without you needing to pass anything."""
    return _jsonable(await _log_activity(event_type, description, decision_id, mode="live"))


@mcp.tool()
async def get_recent_activity(
    decision_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List recent activity-log entries, optionally filtered to one
    decision_id's full step-by-step trail."""
    return _jsonable(await _get_recent_activity(decision_id, limit))


@mcp.tool()
async def record_portfolio_snapshot(
    mode: str, equity: float, cash: float, exposure_pct: float | None = None,
    drawdown_pct: float | None = None, day_label: str | None = None,
    open_positions: int = 0,
    input_tokens: int | None = None, output_tokens: int | None = None,
    cache_read_tokens: int | None = None, reasoning_tokens: int | None = None,
    total_tokens: int | None = None, api_calls: int | None = None,
    hermes_calls_used: int | None = None, weekly_budget: int | None = None,
    open_positions_detail: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record equity/exposure at the end of one cycle. mode: 'backtest' or
    'live'. Call this once per cycle, after marking positions to market —
    it's the only source of equity-over-time data the dashboard has, since
    portfolio state otherwise only ever lived in the harness's local state
    file. day_label: the masked "Day N" string for backtest cycles (so a
    snapshot cross-references what Hermes itself saw); omit for live.

    The token/quota fields are optional real usage for THIS cycle (from
    Hermes' own --usage-file report) and the real quota_pacer.py count as of
    this cycle -- pass them when known so the dashboard reads real numbers
    instead of estimating. open_positions_detail: per-position dicts
    (ticker, quantity, entry_price, current_price, stop_price, target_price,
    days_held, unrealized_pl, unrealized_pl_pct) for a live open-positions
    view without a separate broker/journal round trip."""
    return _jsonable(await _record_portfolio_snapshot(
        mode, equity, cash, exposure_pct, drawdown_pct, day_label, open_positions,
        input_tokens, output_tokens, cache_read_tokens, reasoning_tokens,
        total_tokens, api_calls, hermes_calls_used, weekly_budget,
        open_positions_detail,
    ))


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
