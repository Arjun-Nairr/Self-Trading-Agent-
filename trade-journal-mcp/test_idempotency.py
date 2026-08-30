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


async def test_identical_retry_with_real_run_id_is_a_no_op():
    """2026-08-28 correctness pass (third pass, item 1): Neon returns a
    UUID column (run_id) as a uuid.UUID object while the backtest harness
    always supplies a plain string -- comparing them directly in
    _decision_field_mismatches used to fail every genuine identical retry
    that carried a real run_id with a false 'run_id' mismatch. This is
    the actual regression: a real non-null string run_id, an identical
    retry (must be a no-op), and a genuinely changed run_id (must still
    be rejected -- normalization must not weaken the real check)."""
    run_id = str(uuid.uuid4())
    kwargs = dict(
        ticker="TEST.NS", action="BUY", intended_quantity=5,
        intended_notional=500.0, order_type="MARKET",
        confidence=3, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=2.0, invalidation_condition="run_id normalization test",
        decision_id=str(uuid.uuid4()), run_id=run_id,
    )
    decision = await _record_decision(**kwargs)
    decision_id = decision["decision_id"]
    print(f"created pending decision {decision_id} with run_id={run_id}")

    try:
        # The exact same payload, including the SAME string run_id --
        # must be a true no-op, not a false 'run_id' mismatch.
        again = await _record_decision(**kwargs)
        assert str(again["decision_id"]) == str(decision_id)
        assert str(again["run_id"]) == run_id
        print("  ok  an identical retry with a real non-null run_id is a no-op "
              "(UUID-vs-string comparison no longer false-positives)")

        # A GENUINELY different run_id under the same decision_id must
        # still be rejected -- normalization must never weaken this check.
        different_run_id = str(uuid.uuid4())
        try:
            await _record_decision(**dict(kwargs, run_id=different_run_id))
            raise AssertionError("expected a collision error for a genuinely changed run_id")
        except ValueError as exc:
            assert "run_id" in str(exc), f"error for changed run_id did not name it: {exc}"
            print("  ok  a genuinely different run_id under the same decision_id "
                  "still fails loudly, naming it")
    finally:
        pool = await get_pool()
        await pool.execute("DELETE FROM trades WHERE decision_id = $1", decision_id)


async def test_empty_data_timestamps_retry_is_a_no_op():
    """2026-08-29 correctness pass #4, item 7: the write path stores an
    empty {} as NULL, but the compare path used to compare the raw
    incoming {} against that stored NULL directly -- an identical retry
    carrying {} false-failed as a payload mismatch. Fixed via
    _normalize_data_timestamps, collapsing empty and None identically on
    both sides."""
    kwargs = dict(
        ticker="TEST.NS", action="BUY", intended_quantity=3,
        intended_notional=300.0, order_type="MARKET",
        confidence=3, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=2.0, invalidation_condition="item-7 data_timestamps test",
        data_timestamps={}, decision_id=str(uuid.uuid4()),
    )
    decision = await _record_decision(**kwargs)
    decision_id = decision["decision_id"]
    print(f"created pending decision {decision_id} with data_timestamps={{}}")

    try:
        again = await _record_decision(**kwargs)
        assert str(again["decision_id"]) == str(decision_id)
        assert again["data_timestamps"] is None, (
            f"an empty {{}} must be stored as NULL, got {again['data_timestamps']!r}"
        )
        print("  ok  an identical retry carrying data_timestamps={} is a no-op "
              "(compares equal to the stored NULL)")

        # A GENUINELY different (non-empty) data_timestamps must still fail.
        try:
            await _record_decision(**dict(kwargs, data_timestamps={"quote": "2026-08-29T00:00:00Z"}))
            raise AssertionError("expected a collision error for a genuinely changed data_timestamps")
        except ValueError as exc:
            assert "data_timestamps" in str(exc), f"error did not name it: {exc}"
            print("  ok  a genuinely non-empty data_timestamps under the same decision_id "
                  "still fails loudly, naming it")
    finally:
        pool = await get_pool()
        await pool.execute("DELETE FROM trades WHERE decision_id = $1", decision_id)


async def test_null_confidence_collision_reports_named_mismatch_not_typeerror():
    """2026-08-29 correctness pass #4, item 8: confidence can be NULL for
    a mechanical exit (item 9). The prior direct int(row["confidence"])
    cast crashed with TypeError on a NULL row instead of reporting a
    clean, named mismatch -- the same way every other comparator here
    already handles None."""
    # primary_signal="technical" (not "other"/None, as a real mechanical
    # exit uses) deliberately -- this shape must pass validation at BOTH
    # confidence=None and confidence=3, so the retry below actually
    # reaches the collision comparator instead of failing earlier at
    # _validate_decision_fields (which is exactly what a primary_signal
    # requiring an explanation would do on the confidence=3 retry).
    kwargs = dict(
        ticker="TEST.NS", action="SELL", intended_quantity=7,
        intended_notional=700.0, order_type="MARKET",
        confidence=None, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=1.0,
        invalidation_condition="n/a -- mechanical exit, item-8 test",
        decision_id=str(uuid.uuid4()),
    )
    decision = await _record_decision(**kwargs)
    decision_id = decision["decision_id"]
    print(f"created pending decision {decision_id} with confidence=None")

    try:
        # An IDENTICAL retry (both None) must stay a no-op, no TypeError.
        again = await _record_decision(**kwargs)
        assert str(again["decision_id"]) == str(decision_id)
        assert again["confidence"] is None
        print("  ok  an identical retry with confidence=None on both sides is a no-op")

        # A collision against a REAL confidence must report a named
        # mismatch -- not crash with TypeError on int(None).
        try:
            await _record_decision(**dict(kwargs, confidence=3))
            raise AssertionError("expected a collision error for a NULL-vs-real confidence mismatch")
        except ValueError as exc:
            assert "confidence" in str(exc), f"error did not name it: {exc}"
            print("  ok  a NULL-vs-real confidence collision reports a named mismatch, "
                  "not a TypeError")
        except TypeError as exc:
            raise AssertionError(f"confidence comparator crashed instead of failing loudly: {exc}") from exc
    finally:
        pool = await get_pool()
        await pool.execute("DELETE FROM trades WHERE decision_id = $1", decision_id)


async def test_execution_result_retry_does_not_double_write():
    decision = await _record_decision(
        ticker="TEST.NS", action="BUY", intended_quantity=1,
        intended_notional=100.0, order_type="MARKET",
        confidence=3, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=2.5,
        invalidation_condition="idempotency test -- not a real trade",
    )
    decision_id = decision["decision_id"]
    print(f"created pending decision {decision_id}")

    try:
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

        print("PASS: retrying record_execution_result did not double-write")
    finally:
        # 2026-08-29 correctness pass #4 hygiene: cleanup is now
        # unconditional -- a failed assertion above used to skip this and
        # strand the row in Neon permanently.
        pool = await get_pool()
        await pool.execute("DELETE FROM trades WHERE decision_id = $1", decision_id)


async def test_strategy_profile_collision_and_null_rows_stay_readable():
    """Phase 1 aggressive profile (docs/PHASE1_AGGRESSIVE_DESIGN.md):
    strategy_profile/min_reward_risk_used are additive, nullable columns.
    Proves both directions -- a real (non-null) pair round-trips and
    detects a genuine mismatch, and an OLD (pre-Phase-1) row with both
    NULL remains fully readable and its own identical-retry stays a
    no-op, never misread as a mismatch against the new fields."""
    kwargs = dict(
        ticker="TEST.NS", action="BUY", intended_quantity=10,
        intended_notional=1000.0, order_type="MARKET",
        confidence=3, primary_signal="technical", technical_basis="rsi",
        expected_reward_risk=1.6, invalidation_condition="closes below $93",
        strategy_notes="phase 1 profile-field collision test",
        stop_price=93.0, target_price=115.0,
        strategy_profile="aggressive_v1_base", min_reward_risk_used=1.5,
        decision_id=str(uuid.uuid4()),
    )
    decision = await _record_decision(**kwargs)
    decision_id = decision["decision_id"]
    print(f"created pending decision {decision_id} with strategy_profile=aggressive_v1_base")

    try:
        again = await _record_decision(**kwargs)
        assert str(again["decision_id"]) == str(decision_id)
        assert again["strategy_profile"] == "aggressive_v1_base"
        assert float(again["min_reward_risk_used"]) == 1.5
        print("  ok  an identical retry with real profile fields is a no-op and round-trips them")

        for field, override in (
            ("strategy_profile", {"strategy_profile": "aggressive_v1_escalated"}),
            ("min_reward_risk_used", {"min_reward_risk_used": 1.25}),
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

    # A pre-Phase-1-shaped row (both fields genuinely NULL, exactly what
    # every historical row -- including Run 1's own -- looks like) must
    # remain readable and its own identical retry must stay a no-op, not
    # a false mismatch against the new nullable columns.
    null_kwargs = dict(kwargs, decision_id=str(uuid.uuid4()),
                       strategy_profile=None, min_reward_risk_used=None)
    null_decision = await _record_decision(**null_kwargs)
    null_id = null_decision["decision_id"]
    print(f"created pending decision {null_id} with NULL profile fields (pre-Phase-1 shape)")
    try:
        assert null_decision["strategy_profile"] is None
        assert null_decision["min_reward_risk_used"] is None
        again_null = await _record_decision(**null_kwargs)
        assert str(again_null["decision_id"]) == str(null_id)
        print("  ok  a pre-Phase-1 row (NULL profile fields) stays readable and its "
              "identical retry is still a no-op")
    finally:
        pool = await get_pool()
        await pool.execute("DELETE FROM trades WHERE decision_id = $1", null_id)


async def main():
    await test_decision_collision_detects_every_field()
    await test_identical_retry_with_real_run_id_is_a_no_op()
    await test_empty_data_timestamps_retry_is_a_no_op()
    await test_null_confidence_collision_reports_named_mismatch_not_typeerror()
    await test_execution_result_retry_does_not_double_write()
    await test_strategy_profile_collision_and_null_rows_stay_readable()


if __name__ == "__main__":
    asyncio.run(main())
