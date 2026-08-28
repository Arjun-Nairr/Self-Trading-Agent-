"""
Smallest possible proof that record_execution_result can't double-write.

Simulates the exact failure §6b worries about: a cycle calls
record_execution_result for a decision, then (crash, retry, whatever)
calls it again with the same decision_id. Second call must return the
SAME result as the first, not overwrite it or error.

Run inside the container (has NEON_DATABASE_URL + asyncpg already):
    docker exec hermes-trading-agent-trade-journal python test_idempotency.py
"""
import asyncio
import uuid

from server import get_pool, _record_decision, _record_execution_result  # type: ignore


async def test_decision_collision_detects_every_field():
    """2026-08-28 correctness pass (item 3): a decision_id collision must
    compare EVERY decision-defining field, not just ticker/action/quantity/
    order_type. A genuine identical retry stays a no-op; a materially
    different payload under the SAME id (stop, target, notional, or
    metadata like strategy_notes) must fail loudly, naming the field."""
    kwargs = dict(
        ticker="TEST.NS", action="BUY", intended_quantity=10,
        intended_notional=1000.0, order_type="MARKET",
        confidence=4, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=2.2, invalidation_condition="closes below $93",
        strategy_notes="clean breakout, item-3 collision test",
        stop_price=93.0, target_price=115.0,
        decision_id=str(uuid.uuid4()),
    )
    decision = await _record_decision(**kwargs)
    decision_id = decision["decision_id"]
    print(f"created pending decision {decision_id} for collision test")

    try:
        # An IDENTICAL retry (same decision_id, same payload) must stay a
        # true no-op -- returns the existing row, no error.
        again = await _record_decision(**kwargs)
        assert str(again["decision_id"]) == str(decision_id)
        print("  ok  an identical retry under the same decision_id is a no-op")

        for field, override in (
            ("stop_price", {"stop_price": 88.0}),
            ("target_price", {"target_price": 999.0}),
            ("intended_notional", {"intended_notional": 55555.0}),
            ("strategy_notes", {"strategy_notes": "a completely different rationale"}),
        ):
            bad_kwargs = dict(kwargs, **override)
            try:
                await _record_decision(**bad_kwargs)
                raise AssertionError(f"expected a collision error for changed {field}")
            except ValueError as exc:
                assert field in str(exc), f"error for changed {field} did not name it: {exc}"
                print(f"  ok  a changed {field} under the same decision_id fails loudly, naming it")
    finally:
        pool = await get_pool()
        await pool.execute("DELETE FROM trades WHERE decision_id = $1", decision_id)


async def main():
    await test_decision_collision_detects_every_field()

    decision = await _record_decision(
        ticker="TEST.NS", action="BUY", intended_quantity=1,
        intended_notional=100.0, order_type="MARKET",
        confidence=3, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=2.5,
        invalidation_condition="idempotency test -- not a real trade",
    )
    decision_id = decision["decision_id"]
    print(f"created pending decision {decision_id}")

    first = await _record_execution_result(
        decision_id, status="filled", broker_order_id="ORD123",
        fill_price=99.5, fill_quantity=1,
    )
    print(f"first call: status={first['status']} fill_price={first['fill_price']}")
    assert first["status"] == "filled"
    assert str(first["fill_price"]) == "99.50"

    # Retry with different (wrong) fill data — must be ignored, not applied.
    second = await _record_execution_result(
        decision_id, status="failed", broker_order_id="SHOULD-NOT-APPLY",
        rejection_reason="this must not overwrite the first result",
    )
    print(f"second call (retry): status={second['status']} broker_order_id={second['broker_order_id']}")
    assert second["status"] == "filled", "retry must not have changed the resolved status"
    assert second["broker_order_id"] == "ORD123", "retry must not have overwritten broker_order_id"

    # Cleanup so repeated test runs don't pile up rows.
    pool = await get_pool()
    await pool.execute("DELETE FROM trades WHERE decision_id = $1", decision_id)

    print("PASS: retrying record_execution_result did not double-write")


if __name__ == "__main__":
    asyncio.run(main())
