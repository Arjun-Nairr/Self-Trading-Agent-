"""Pure behavioral checks that live and backtest journal data cannot mix.

Run: python test_mode_isolation.py
"""
import asyncio
from unittest.mock import AsyncMock, patch

import server


async def test_live_decision_is_tagged_before_execution():
    row = {
        "decision_id": "00000000-0000-0000-0000-000000000001",
        "ticker": "AAPL",
        "action": "BUY",
        "intended_quantity": 1,
        "intended_notional": 100.0,
        "order_type": "MARKET",
        "confidence": 3,
        "primary_signal": "technical",
        "technical_basis": "rsi",
        "expected_reward_risk": 2.0,
        "invalidation_condition": "closes below support",
        "strategy_notes": None,
        "stop_price": 93.0,
        "target_price": 114.0,
        "limit_price": None,
        "data_timestamps": None,
        "run_id": None,
        "execution_mode": "alpaca",
    }
    pool = AsyncMock()
    pool.fetchrow.return_value = row
    with patch("server.get_pool", AsyncMock(return_value=pool)):
        result = await server._record_decision(
            ticker="AAPL", action="BUY", intended_quantity=1,
            intended_notional=100.0, order_type="MARKET", confidence=3,
            primary_signal="technical", technical_basis="rsi",
            expected_reward_risk=2.0,
            invalidation_condition="closes below support",
            stop_price=93.0, target_price=114.0,
            execution_mode="alpaca",
        )

    assert result["execution_mode"] == "alpaca"
    sql, *params = pool.fetchrow.call_args.args
    assert "execution_mode" in sql
    assert "alpaca" in params


async def test_live_history_wrappers_filter_live_rows():
    with patch("server._get_trade_history", AsyncMock(return_value=[])) as trades:
        await server.get_trade_history(status="pending")
        assert trades.await_args.kwargs["execution_mode"] == "alpaca"

    with patch("server._get_insight_history", AsyncMock(return_value=[])) as insights:
        await server.get_insight_history()
        assert insights.await_args.kwargs["mode"] == "live"

    with patch("server._record_execution_result", AsyncMock(return_value={})) as result:
        await server.record_execution_result(
            "00000000-0000-0000-0000-000000000001", status="rejected",
        )
        assert result.await_args.args[-1] == "alpaca"


async def main():
    await test_live_decision_is_tagged_before_execution()
    print("  ok  live decision is tagged before execution")
    await test_live_history_wrappers_filter_live_rows()
    print("  ok  live history wrappers cannot read backtest rows")
    print("PASS: journal mode isolation holds at public boundaries")


if __name__ == "__main__":
    asyncio.run(main())
