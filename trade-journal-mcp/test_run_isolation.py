"""
Final pre-training correctness pass (2026-08-28, second pass) -- proof that
run/cycle idempotency actually holds at the database level, not just in
run_backtest.py's control flow:

  1. A decision_id collision with a DIFFERENT payload fails loudly instead
     of silently returning the stale existing row (item 2).
  2. A portfolio_snapshots write is idempotent per cycle_id -- a retry
     returns None (no duplicate row), a different cycle_id always writes
     (item 5/6).
  3. Same idempotency for insight_log.cycle_id (item 5).

Run inside the container (has NEON_DATABASE_URL + asyncpg already):
    docker exec hermes-trading-agent-trade-journal python test_run_isolation.py
"""
import asyncio
import uuid

from server import (  # type: ignore
    get_pool, _record_decision, _record_portfolio_snapshot, _log_insight,
)


async def main():
    pool = await get_pool()

    # -- 1. decision_id collision, different payload -> fails loudly ------
    decision_id = str(uuid.uuid4())
    first = await _record_decision(
        decision_id=decision_id, ticker="OLN", action="BUY",
        intended_quantity=10, intended_notional=500.0, order_type="MARKET",
        confidence=3, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=2.0, invalidation_condition="run-isolation test",
    )
    assert first["ticker"] == "OLN"
    # Genuine retry -- IDENTICAL payload -- must be a safe no-op.
    retry = await _record_decision(
        decision_id=decision_id, ticker="OLN", action="BUY",
        intended_quantity=10, intended_notional=500.0, order_type="MARKET",
        confidence=3, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=2.0, invalidation_condition="run-isolation test",
    )
    assert retry["decision_id"] == first["decision_id"]
    print("ok  identical-payload retry under the same decision_id is a safe no-op")

    # Same decision_id, DIFFERENT ticker -- must fail loudly, not silently
    # return OLN's row under an ALGM lookup.
    try:
        await _record_decision(
            decision_id=decision_id, ticker="ALGM", action="BUY",
            intended_quantity=10, intended_notional=500.0, order_type="MARKET",
            confidence=3, primary_signal="technical", technical_basis="rsi",
            expected_reward_risk=2.0, invalidation_condition="run-isolation test",
        )
        raise AssertionError("a decision_id collision with a different payload should have raised")
    except ValueError as exc:
        assert "collision" in str(exc)
        print("ok  decision_id collision with a different payload fails loudly")
    await pool.execute("DELETE FROM trades WHERE decision_id = $1", decision_id)

    # -- 2. portfolio_snapshots.cycle_id idempotency -----------------------
    run_id = str(uuid.uuid4())
    cycle_id = str(uuid.uuid4())
    snap1 = await _record_portfolio_snapshot(
        mode="backtest", equity=100000.0, cash=100000.0, day_label="Day 1",
        run_id=run_id, cycle_id=cycle_id,
    )
    assert snap1 is not None and snap1["cycle_id"] is not None
    # A retry with the SAME cycle_id must return None -- no duplicate row.
    snap2 = await _record_portfolio_snapshot(
        mode="backtest", equity=999999.0, cash=999999.0, day_label="Day 1 RETRY",
        run_id=run_id, cycle_id=cycle_id,
    )
    assert snap2 is None, "a retried cycle_id must not write a second snapshot row"
    count = await pool.fetchval(
        "SELECT count(*) FROM portfolio_snapshots WHERE cycle_id = $1", uuid.UUID(cycle_id)
    )
    assert count == 1, f"expected exactly 1 snapshot for this cycle_id, got {count}"
    print("ok  portfolio_snapshots is idempotent per cycle_id (retry returns None, no duplicate)")

    # A DIFFERENT cycle_id (a genuinely new day) always writes.
    cycle_id_2 = str(uuid.uuid4())
    snap3 = await _record_portfolio_snapshot(
        mode="backtest", equity=100500.0, cash=100500.0, day_label="Day 2",
        run_id=run_id, cycle_id=cycle_id_2,
    )
    assert snap3 is not None
    print("ok  a different cycle_id (a new day) always writes normally")
    await pool.execute("DELETE FROM portfolio_snapshots WHERE cycle_id = ANY($1)",
                       [uuid.UUID(cycle_id), uuid.UUID(cycle_id_2)])

    # -- 3. insight_log.cycle_id idempotency -------------------------------
    insight_cycle_id = str(uuid.uuid4())
    ins1 = await _log_insight("weekly_reflection", "first pass", mode="backtest",
                              run_id=run_id, cycle_id=insight_cycle_id)
    assert ins1 is not None
    ins2 = await _log_insight("weekly_reflection", "retried pass -- must not double-write",
                              mode="backtest", run_id=run_id, cycle_id=insight_cycle_id)
    assert ins2 is None, "a retried cycle_id must not write a second insight row"
    count = await pool.fetchval(
        "SELECT count(*) FROM insight_log WHERE cycle_id = $1", uuid.UUID(insight_cycle_id)
    )
    assert count == 1, f"expected exactly 1 insight for this cycle_id, got {count}"
    print("ok  insight_log is idempotent per cycle_id (retry returns None, no duplicate)")
    await pool.execute("DELETE FROM insight_log WHERE cycle_id = $1", uuid.UUID(insight_cycle_id))

    print("PASS: decision collisions fail loudly; snapshot/insight writes are idempotent per cycle_id")


if __name__ == "__main__":
    asyncio.run(main())
