"""Proof the execution half of the harness works end to end.

The smoke run left this untested for a legitimate reason: Hermes declined to
trade on every cycle (no candidate cleared the rules), so simulate_fill and
record_trade were never exercised in the real loop. Waiting for a day it
happens to want to trade is not a test strategy — this drives a known-good
decision straight through the same functions the loop uses.

Real containers, real fill simulator, real Neon write. Cleans up after
itself so it never pollutes backtest results.
"""
from datetime import date

from run_backtest import fetch_bars, make_decision_id, record_trade, run_in, simulate_fill

TICKER = "OLN"
DAY = date(2026, 2, 18)
# A test run_key, distinct from any real backtest run's (start|seed) key --
# cannot collide with a genuine trade even if this test's cleanup somehow
# didn't run.
TEST_RUN_KEY = "test_execution_path|999"


def main():
    bars = fetch_bars([TICKER], DAY)
    assert TICKER in bars, f"no bar data for {TICKER} on {DAY}"
    bar = bars[TICKER]
    print(f"  ok  real bar {DAY}: O{bar['open']:.2f} H{bar['high']:.2f} "
          f"L{bar['low']:.2f} C{bar['close']:.2f}")

    entry = bar["open"]
    fill = simulate_fill("ALPHA", "BUY", 100, entry, bar)
    assert fill["status"] == "filled", fill
    assert fill["fill_quantity"] == 100, fill
    # MARKET BUY slips against us — the fill must never be better than the
    # reference price, or the simulator is flattering the strategy.
    assert fill["fill_price"] >= entry, (fill["fill_price"], entry)
    print(f"  ok  sim filled 100 @ ${fill['fill_price']:.2f} "
          f"(ref ${entry:.2f}, slipped against us)")

    decision = {
        "action": "BUY", "quantity": 100, "entry_price": entry,
        "confidence": 3, "primary_signal": "technical",
        "technical_basis": "support_resistance", "expected_reward_risk": 2.5,
        "invalidation_condition": "execution-path test row, not a real trade",
        "reasoning": "Integration test of the harness execution path.",
    }
    fixed_id = make_decision_id(TEST_RUN_KEY, 1, TICKER, "BUY", "queued_fill")
    decision_id = record_trade(decision, TICKER, fill, decision_id=fixed_id)
    assert decision_id == fixed_id, (decision_id, fixed_id)
    print(f"  ok  journalled {decision_id}")

    # PHASE 0 REPAIR: prove the ACTUAL DB-level idempotency guarantee, not
    # just that the id-generation scheme is deterministic (that part is
    # tested separately, without a container, in test_execution_semantics
    # .py). Retrying the exact same call with the exact same decision_id --
    # exactly what happens when a failed backtest day is re-run -- must be
    # a safe no-op, never a second row.
    decision_id_retry = record_trade(decision, TICKER, fill, decision_id=fixed_id)
    assert decision_id_retry == fixed_id, "retry returned a different id than the original write"
    row_count = count_rows(fixed_id)
    assert row_count == 1, f"retrying an already-journaled trade created {row_count} rows, expected 1"
    print("  ok  retrying the SAME decision_id (a retried failed day) journals exactly ONE row, not two")

    try:
        verify(decision_id)
    finally:
        # Cleanup must survive a failed assertion. An earlier version only
        # deleted on the success path, and a mid-test crash left a real row
        # sitting in the shared journal tagged execution_mode='backtest' —
        # indistinguishable from genuine backtest data, which is exactly
        # the contamination that field exists to prevent.
        cleanup(decision_id)
        print("  ok  test row deleted")

    print("PASS: bars -> fill sim -> journal -> read-back all wired correctly")


def count_rows(decision_id: str) -> int:
    check = run_in("trade-journal", f'''
import asyncio
from server import get_pool

async def main():
    pool = await get_pool()
    n = await pool.fetchval("SELECT count(*) FROM trades WHERE decision_id = $1", "{decision_id}")
    print(n)

asyncio.run(main())
''')
    return int(check.strip().splitlines()[-1])


def cleanup(decision_id: str) -> None:
    run_in("trade-journal", f'''
import asyncio
from server import get_pool

async def main():
    pool = await get_pool()
    await pool.execute("DELETE FROM trades WHERE decision_id = $1", "{decision_id}")

asyncio.run(main())
''')


def verify(decision_id: str) -> None:
    check = run_in("trade-journal", f'''
import asyncio, json
from server import get_pool

async def main():
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT ticker, execution_mode, status, confidence, primary_signal, "
        "fill_price FROM trades WHERE decision_id = $1", "{decision_id}")
    print(json.dumps(dict(row), default=str))

asyncio.run(main())
''')
    row = __import__("json").loads(check[check.find("{"): check.rfind("}") + 1])

    # The real ticker must be stored, never the alias — the mask exists to
    # keep Hermes honest, not to obscure our own records.
    assert row["ticker"] == TICKER, row
    # Backtest rows must be distinguishable from stray manual dev-test rows.
    assert row["execution_mode"] == "backtest", row
    assert row["status"] == "filled" and row["confidence"] == 3, row
    print(f"  ok  read back {row['ticker']} mode={row['execution_mode']} "
          f"conf={row['confidence']} @ ${float(row['fill_price']):.2f}")


if __name__ == "__main__":
    main()
