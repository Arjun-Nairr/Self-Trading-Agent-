"""
backtest_run_status -- the explicit lifecycle-status record Dashboard V2's
run-status indicator reads instead of ever inferring "is this run actively
training" from a portfolio_snapshots timestamp. Proof, at the database
level:

  1. First write for a run_id sets started_at and the given status/
     current_day/total_days.
  2. A second write for the SAME run_id only advances status/current_day/
     updated_at -- started_at never changes.
  3. total_days=None on a later write preserves whatever was already
     stored (COALESCE), never overwrites it with NULL.
  4. total_days given on a later write DOES overwrite (a fresh invocation
     target).
  5. An invalid status is rejected before any write.
  6. Writing for a run_id with no backtest_run_registry row fails (FK) --
     this table can never hold an unclassified run's status.

Uses fresh random run_ids only -- never touches any real historical run_id.
Self-cleaning: deletes its own test rows (both tables) on the way out.

Run directly (has NEON_DATABASE_URL + asyncpg on PATH):
    python test_run_status.py
"""
import asyncio
import uuid

import asyncpg

from server import get_pool, _register_run, _record_run_status  # type: ignore


async def main():
    pool = await get_pool()
    test_ids = []

    try:
        # -- 1/2/3/4: upsert contract ---------------------------------------
        run_id = str(uuid.uuid4())
        test_ids.append(run_id)
        await _register_run(run_id, "training")  # FK requires a registry row first

        first = await _record_run_status(run_id, "running", 0, total_days=10)
        assert first["status"] == "running"
        assert first["current_day"] == 0
        assert first["total_days"] == 10
        started_at = first["started_at"]
        print("ok  first write sets status/current_day/total_days/started_at")

        second = await _record_run_status(run_id, "running", 5)  # total_days omitted
        assert second["current_day"] == 5
        assert second["total_days"] == 10, "omitted total_days must be preserved, not nulled"
        assert second["started_at"] == started_at, "started_at must never change after the first write"
        assert second["updated_at"] > first["updated_at"] or second["updated_at"] == first["updated_at"]
        print("ok  a later progress write (total_days=None) advances current_day, preserves total_days/started_at")

        third = await _record_run_status(run_id, "running", 5, total_days=15)
        assert third["total_days"] == 15, "an explicit total_days on a later write must overwrite"
        print("ok  a later write WITH total_days overwrites the stored target")

        count = await pool.fetchval(
            "SELECT count(*) FROM backtest_run_status WHERE run_id = $1", uuid.UUID(run_id)
        )
        assert count == 1, f"expected exactly 1 status row (upsert, not insert), got {count}"
        print("ok  repeated writes for the same run_id stay a single row")

        # -- 5: invalid status rejected --------------------------------------
        try:
            await _record_run_status(run_id, "paused", 5)
            assert False, "an invalid status must be rejected"
        except ValueError as e:
            assert "must be one of" in str(e)
            print("ok  an invalid status is rejected before any write ('paused' is dashboard-derived, never written)")

        # completed/failed both write cleanly
        completed = await _record_run_status(run_id, "completed", 15)
        assert completed["status"] == "completed"
        print("ok  'completed' writes cleanly")

        failed_id = str(uuid.uuid4())
        test_ids.append(failed_id)
        await _register_run(failed_id, "training")
        failed = await _record_run_status(failed_id, "failed", 3)
        assert failed["status"] == "failed"
        print("ok  'failed' writes cleanly")

        # -- 6: FK -- no registry row means no status row --------------------
        unregistered_id = str(uuid.uuid4())
        try:
            await _record_run_status(unregistered_id, "running", 0)
            assert False, "a run_id with no backtest_run_registry row must be rejected"
        except asyncpg.ForeignKeyViolationError:
            print("ok  a run_id with no registry row cannot acquire a status row (FK enforced)")

        print("PASS: backtest_run_status upserts correctly (started_at fixed, total_days "
              "preserved-or-overwritten as intended), rejects invalid status, and requires "
              "a registry row via FK")
    finally:
        if test_ids:
            await pool.execute(
                "DELETE FROM backtest_run_status WHERE run_id = ANY($1)",
                [uuid.UUID(i) for i in test_ids],
            )
            await pool.execute(
                "DELETE FROM backtest_run_registry WHERE run_id = ANY($1)",
                [uuid.UUID(i) for i in test_ids],
            )


if __name__ == "__main__":
    asyncio.run(main())
