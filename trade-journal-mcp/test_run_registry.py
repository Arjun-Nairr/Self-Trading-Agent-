"""
backtest_run_registry -- durable run classification (training / smoke /
archived), separate from the data tables. Proof, at the database level:

  1. Same run_id + same run_kind, registered twice -> idempotent no-op.
  2. Same run_id + a DIFFERENT run_kind -> fails loudly, never silently
     relabels an already-registered run.
  3. An invalid run_kind is rejected before any write.
  4. _get_run_kind: None for an unregistered run_id, the real value for a
     registered one -- what run_backtest.py's resume path relies on to
     fail closed on a pre-existing, unregistered run.

Uses fresh random run_ids only -- never touches any real historical run_id
(e68525f9-.../26f1d371-.../97d112e8-.../c55f0fc2-.../1bf9af3d-.../
81db75e5-...). Self-cleaning: deletes its own test rows on the way out.

Run directly (has NEON_DATABASE_URL + asyncpg on PATH):
    python test_run_registry.py
"""
import asyncio
import uuid

from server import get_pool, _register_run, _get_run_kind  # type: ignore


async def main():
    pool = await get_pool()
    test_ids = []

    try:
        # -- 1. idempotent same-run/same-kind registration -----------------
        run_id = str(uuid.uuid4())
        test_ids.append(run_id)
        first = await _register_run(run_id, "training")
        assert first["run_kind"] == "training"
        again = await _register_run(run_id, "training")
        assert again["run_id"] == first["run_id"]
        assert again["run_kind"] == "training"
        count = await pool.fetchval(
            "SELECT count(*) FROM backtest_run_registry WHERE run_id = $1", uuid.UUID(run_id)
        )
        assert count == 1, f"expected exactly 1 registry row, got {count}"
        print("ok  same run_id + same run_kind registered twice is a safe no-op")

        # -- 2. conflicting-kind registration fails loudly ------------------
        try:
            await _register_run(run_id, "smoke")
            assert False, "registering an existing run_id under a different run_kind must raise"
        except ValueError as e:
            assert "already registered as 'training'" in str(e)
            print("ok  same run_id + conflicting run_kind fails loudly, does not relabel")

        # the row must be completely unchanged after the rejected attempt
        row = await pool.fetchrow(
            "SELECT run_kind FROM backtest_run_registry WHERE run_id = $1", uuid.UUID(run_id)
        )
        assert row["run_kind"] == "training"
        print("ok  rejected conflicting registration left the original classification untouched")

        # -- 3. invalid run_kind rejected before any write -------------------
        bad_id = str(uuid.uuid4())
        try:
            await _register_run(bad_id, "not_a_real_kind")
            assert False, "an invalid run_kind must be rejected"
        except ValueError as e:
            assert "must be one of" in str(e)
        exists = await pool.fetchval(
            "SELECT count(*) FROM backtest_run_registry WHERE run_id = $1", uuid.UUID(bad_id)
        )
        assert exists == 0, "an invalid run_kind must never reach the table"
        print("ok  an invalid run_kind is rejected, no row written")

        # -- 4. _get_run_kind: None for unregistered, real value once set ----
        unregistered_id = str(uuid.uuid4())
        assert await _get_run_kind(unregistered_id) is None
        print("ok  _get_run_kind returns None for an unregistered run_id")

        smoke_id = str(uuid.uuid4())
        test_ids.append(smoke_id)
        await _register_run(smoke_id, "smoke")
        assert await _get_run_kind(smoke_id) == "smoke"
        print("ok  _get_run_kind returns the real stored classification once registered")

        # -- 5. the three allowed kinds all register cleanly ------------------
        archived_id = str(uuid.uuid4())
        test_ids.append(archived_id)
        row = await _register_run(archived_id, "archived")
        assert row["run_kind"] == "archived"
        print("ok  'archived' registers cleanly (migration-only in the harness, valid in the registry)")

        print("PASS: idempotent same-kind registration; conflicting-kind fails loudly; "
              "invalid kind rejected; _get_run_kind distinguishes unregistered from registered")
    finally:
        if test_ids:
            await pool.execute(
                "DELETE FROM backtest_run_registry WHERE run_id = ANY($1)",
                [uuid.UUID(i) for i in test_ids],
            )


if __name__ == "__main__":
    asyncio.run(main())
