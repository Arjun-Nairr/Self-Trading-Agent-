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


async def main():
    decision = await _record_decision(
        ticker="TEST.NS", action="BUY", intended_quantity=1,
        intended_notional=100.0, order_type="MARKET",
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
