"""Proof the account math that gates RULES.md §3/§4 is correct, including
the cases where an honest 'unknown' must not be reported as a safe zero.
Run: docker exec hermes-trading-agent-alpaca-mcp python test_account_math.py
"""
from datetime import datetime, timezone

from server import _pct_change, _summarize_history


def _ts(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def main():
    # -- _pct_change ------------------------------------------------------
    assert _pct_change(103.0, 100.0) == 3.0
    assert _pct_change(97.0, 100.0) == -3.0
    assert _pct_change(100.0, 100.0) == 0.0

    # THE SAFETY CASE: a missing/zero baseline must yield None ("unknown"),
    # never 0.0 -- 0.0 reads as "flat, no loss, breaker clear," which would
    # fabricate an all-clear on a circuit breaker that cannot actually be
    # evaluated.
    assert _pct_change(100.0, None) is None
    assert _pct_change(100.0, 0.0) is None
    print("  ok  _pct_change reports unknown as None, never a fake 0.0")

    # -- _summarize_history ----------------------------------------------
    # Aug 17 2026 is a Monday; Aug 19 is that Wednesday; Aug 14 is the
    # Friday of the PREVIOUS week.
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)

    week_start, peak = _summarize_history(
        [_ts(2026, 8, 13), _ts(2026, 8, 14), _ts(2026, 8, 17), _ts(2026, 8, 18)],
        [90_000.0, 110_000.0, 100_000.0, 98_000.0],
        now,
    )
    # Week start is Monday the 17th's value, NOT the prior week's Friday.
    assert week_start == 100_000.0, week_start
    # Peak spans all history, including the earlier week.
    assert peak == 110_000.0, peak
    print("  ok  week start uses this ISO week only; peak spans all history")

    # Pre-funding 0.0 points must be filtered, or peak/drawdown math breaks.
    week_start, peak = _summarize_history(
        [_ts(2026, 8, 17), _ts(2026, 8, 18)], [0.0, 100_000.0], now,
    )
    assert week_start == 100_000.0, week_start
    assert peak == 100_000.0, peak
    print("  ok  zero-equity (pre-funding) points filtered out")

    # No usable history at all -> honest None, not a fabricated baseline.
    assert _summarize_history([], [], now) == (None, None)
    assert _summarize_history([_ts(2026, 8, 17)], [0.0], now) == (None, None)
    print("  ok  empty/all-zero history reports None, not a guess")

    # History exists but none of it falls in the current week (e.g. first
    # cycle of a new week): peak still known, week baseline honestly None.
    week_start, peak = _summarize_history(
        [_ts(2026, 8, 13)], [95_000.0], now,
    )
    assert week_start is None, week_start
    assert peak == 95_000.0, peak
    print("  ok  no data this week -> weekly baseline None, peak still known")

    # -- the drawdown convention -----------------------------------------
    # At a new high, drawdown must read 0.0 (not positive), which is what
    # get_account's max(peak, equity) guarantees before calling _pct_change.
    assert _pct_change(120_000.0, max(110_000.0, 120_000.0)) == 0.0
    assert _pct_change(99_000.0, max(110_000.0, 99_000.0)) == -10.0
    print("  ok  drawdown is 0.0 at a new high, negative below peak")

    print("PASS: account math is correct and reports unknowns honestly")


if __name__ == "__main__":
    main()
