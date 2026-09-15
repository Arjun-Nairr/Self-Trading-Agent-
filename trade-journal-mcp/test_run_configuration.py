"""
backtest_run_configuration -- the private, Neon-durable copy of a backtest
run's frozen watchlist snapshot and full ticker<->alias mapping (correction
pass item D2 / freeform red-team R2 durability). Proof, at the database
level:

  1. A fresh run's configuration, registered before any cycle work, is
     persisted and readable back via _get_run_configuration.
  2. The SAME values registered twice for the same run_id -> idempotent
     no-op (a retried harness invocation before its first successful cycle
     write).
  3. ANY differing value for an already-registered run_id -> fails loudly,
     naming exactly which field(s) conflict, never silently overwrites.
  4. _get_run_configuration: None for a run_id with no configuration row
     (a pre-existing run, or the preserved Round 2 run) -- what run_
     backtest.py's resume path relies on to fail closed rather than
     silently re-freezing a fresh record.
  5. Writing for a run_id with no backtest_run_registry row fails (FK) --
     this table can never hold an unclassified run's configuration.

Uses fresh random run_ids only -- never touches any real historical run_id.
Self-cleaning: deletes its own test rows (both tables) on the way out.

Run directly (has NEON_DATABASE_URL + asyncpg on PATH):
    python test_run_configuration.py
"""
import asyncio
import uuid

import asyncpg

from server import (  # type: ignore
    get_pool, _register_run, _register_run_configuration, _get_run_configuration,
)

_WATCHLIST = [{"symbol": "OLN", "name": "Olin Corporation"}, {"symbol": "ATI", "name": "ATI Inc."}]
_MAPPING = {"OLN": "ALPHA", "ATI": "BRAVO"}


async def main():
    pool = await get_pool()
    test_ids = []

    try:
        # -- 1/2: fresh registration + idempotent identical retry -----------
        run_id = str(uuid.uuid4())
        test_ids.append(run_id)
        await _register_run(run_id, "training")  # FK requires a registry row first

        first = await _register_run_configuration(
            run_id, _WATCHLIST, "wh-hash-1", _MAPPING, "ah-hash-1",
            42, "2026-01-01", "2026-03-01", False,
        )
        assert first["watchlist_snapshot_hash"] == "wh-hash-1"
        assert first["masking_seed"] == 42
        print("ok  a fresh run's configuration is persisted before any cycle work")

        again = await _register_run_configuration(
            run_id, _WATCHLIST, "wh-hash-1", _MAPPING, "ah-hash-1",
            42, "2026-01-01", "2026-03-01", False,
        )
        assert again["run_id"] == first["run_id"]
        count = await pool.fetchval(
            "SELECT count(*) FROM backtest_run_configuration WHERE run_id = $1", uuid.UUID(run_id)
        )
        assert count == 1, f"expected exactly 1 configuration row, got {count}"
        print("ok  an exact retry (identical values) is a safe idempotent no-op")

        # -- 3: a differing value fails closed, names the conflicting field --
        try:
            await _register_run_configuration(
                run_id, _WATCHLIST, "wh-hash-1", _MAPPING, "ah-hash-1",
                999, "2026-01-01", "2026-03-01", False,  # seed differs
            )
            assert False, "a differing masking_seed must fail closed"
        except ValueError as e:
            assert "masking_seed" in str(e)
            print("ok  a differing seed fails closed, names masking_seed as the conflict")

        try:
            await _register_run_configuration(
                run_id, _WATCHLIST, "DIFFERENT-HASH", _MAPPING, "ah-hash-1",
                42, "2026-01-01", "2026-03-01", False,  # watchlist hash differs
            )
            assert False, "a differing watchlist_snapshot_hash must fail closed"
        except ValueError as e:
            assert "watchlist_snapshot_hash" in str(e)
            print("ok  a differing watchlist snapshot hash fails closed, names the conflict")

        # the row must be completely unchanged after both rejected attempts
        row = await pool.fetchrow(
            "SELECT masking_seed, watchlist_snapshot_hash FROM backtest_run_configuration WHERE run_id = $1",
            uuid.UUID(run_id),
        )
        assert row["masking_seed"] == 42
        assert row["watchlist_snapshot_hash"] == "wh-hash-1"
        print("ok  rejected conflicting writes left the original frozen configuration untouched")

        # -- 4: _get_run_configuration distinguishes unregistered from real --
        unregistered_id = str(uuid.uuid4())
        assert await _get_run_configuration(unregistered_id) is None
        print("ok  _get_run_configuration returns None for a run with no configuration row "
              "(a pre-existing run, or the preserved Round 2 run)")

        fetched = await _get_run_configuration(run_id)
        assert fetched["alias_mapping_hash"] == "ah-hash-1"
        assert fetched["fundamentals_enabled"] is False
        print("ok  _get_run_configuration returns the real stored record once written")

        # -- 5: FK -- no registry row means no configuration row ------------
        orphan_id = str(uuid.uuid4())
        try:
            await _register_run_configuration(
                orphan_id, _WATCHLIST, "wh-x", _MAPPING, "ah-x", 1,
                "2026-01-01", "2026-02-01", False,
            )
            assert False, "a run_id with no backtest_run_registry row must be rejected"
        except asyncpg.ForeignKeyViolationError:
            print("ok  a run_id with no registry row cannot acquire a configuration row (FK enforced)")

        print("PASS: fresh configuration persisted before cycle work; exact retry idempotent; "
              "any differing value fails closed naming the conflict; _get_run_configuration "
              "distinguishes unregistered/legacy from real; FK requires a registry row first")
    finally:
        if test_ids:
            await pool.execute(
                "DELETE FROM backtest_run_configuration WHERE run_id = ANY($1)",
                [uuid.UUID(i) for i in test_ids],
            )
            await pool.execute(
                "DELETE FROM backtest_run_registry WHERE run_id = ANY($1)",
                [uuid.UUID(i) for i in test_ids],
            )


if __name__ == "__main__":
    asyncio.run(main())
