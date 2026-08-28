"""
Smallest possible proof that the 2026-08-28 dashboard-correctness migration
actually persists and reads back correctly: stop_price/target_price on a
decision, mode on an insight, and the usage/quota/positions_detail fields on
a portfolio snapshot. Each of these was silently dropped before this
migration -- this is the check that catches a regression back to that.

Run inside the container (has NEON_DATABASE_URL + asyncpg already):
    docker exec hermes-trading-agent-trade-journal python test_dashboard_fields.py
"""
import asyncio

from server import (  # type: ignore
    get_pool, _record_decision, _log_insight, _record_portfolio_snapshot,
)


async def main():
    pool = await get_pool()

    # -- stop_price / target_price persist on a decision -------------------
    decision = await _record_decision(
        ticker="TEST.NS", action="BUY", intended_quantity=1,
        intended_notional=100.0, order_type="MARKET",
        confidence=3, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=2.5,
        invalidation_condition="dashboard-fields test -- not a real trade",
        stop_price=95.0, target_price=110.0,
    )
    assert str(decision["stop_price"]) == "95.00", decision["stop_price"]
    assert str(decision["target_price"]) == "110.00", decision["target_price"]
    print("ok  stop_price/target_price round-trip on a decision")
    await pool.execute("DELETE FROM trades WHERE decision_id = $1", decision["decision_id"])

    # -- mode is required and enforced on an insight ------------------------
    insight = await _log_insight("decision", "dashboard-fields test insight", mode="backtest")
    assert insight["mode"] == "backtest"
    print("ok  mode persists on an insight")
    try:
        await _log_insight("decision", "bad mode", mode="nonsense")
        raise AssertionError("invalid mode should have been rejected")
    except ValueError:
        print("ok  invalid mode rejected")
    await pool.execute("DELETE FROM insight_log WHERE id = $1", insight["id"])

    # -- usage / quota / positions_detail persist on a snapshot -------------
    snap = await _record_portfolio_snapshot(
        mode="backtest", equity=100000.0, cash=90000.0,
        exposure_pct=10.0, drawdown_pct=0.0, day_label="Day 1",
        open_positions=1,
        input_tokens=1000, output_tokens=200, cache_read_tokens=50,
        reasoning_tokens=300, total_tokens=1550, api_calls=1,
        hermes_calls_used=35, weekly_budget=175,
        open_positions_detail=[{"ticker": "OLN", "quantity": 10, "unrealized_pl": 12.5}],
    )
    assert snap["total_tokens"] == 1550
    assert snap["hermes_calls_used"] == 35
    assert snap["weekly_budget"] == 175
    import json
    detail = json.loads(snap["open_positions_detail"])
    assert detail[0]["ticker"] == "OLN"
    print("ok  usage/quota/positions_detail round-trip on a snapshot")
    await pool.execute("DELETE FROM portfolio_snapshots WHERE id = $1", snap["id"])

    print("PASS: 2026-08-28 dashboard-correctness fields all persist and read back correctly")


if __name__ == "__main__":
    asyncio.run(main())
