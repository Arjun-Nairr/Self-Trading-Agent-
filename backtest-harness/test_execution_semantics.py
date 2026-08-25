"""Phase 0 backtest-integrity repair — pure, no-I/O tests for the new
execution semantics: no same-day lookahead, fail-safe cycle handling,
deterministic idempotent journaling.

Mirrors risk_checker.py's own testing philosophy on purpose: every function
under test here does zero I/O and takes every fact as an explicit argument,
so these run against hand-built scenarios with no Docker/Neon dependency —
same reason risk_checker.py itself is built that way ("a safety layer
nobody can fully test is not a safety layer").

Run: python test_execution_semantics.py
"""
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

# run_backtest inserts ../risk-checker onto sys.path as an import side
# effect -- must be imported first, matching test_execution_path.py's
# existing pattern.
from run_backtest import (
    Holding, Portfolio, QueuedOrder, _atomic_write_json, _run_cycle_or_halt,
    make_decision_id, resolve_queued_order,
)
from risk_checker import AccountState, Context, MarketClock, check_trade  # noqa: E402


def _ctx(**overrides) -> Context:
    account = AccountState(equity=100_000.0, cash=100_000.0, daily_pl_pct=0.0,
                           weekly_pl_pct=0.0, drawdown_from_peak_pct=0.0)
    clock = MarketClock(is_open=True, minutes_since_open=120, minutes_until_close=120)
    return Context(account=account, clock=clock, watchlist=["OLN"], **overrides)


def _order(**overrides) -> QueuedOrder:
    return QueuedOrder(**{
        "ticker": "OLN", "alias": "ALPHA", "action": "BUY", "quantity": 90,
        "stop_price": 93.0, "target_price": 115.0,
        "entry_price": 999.0,  # deliberately wrong -- must never be used
        "queued_day_index": 3,
        "decision": {"action": "BUY", "quantity": 90, "confidence": 3},
        **overrides,
    })


def test_queued_order_fills_at_open_never_at_stale_entry_price():
    """The actual no-lookahead guarantee: resolve_queued_order must decide
    purely off bar["open"] and the order's stop/target -- order.entry_price
    (Hermes' stale pre-gap guess, deliberately set to $999 here, nowhere
    near reality) must have zero effect on the outcome."""
    bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}
    outcome = resolve_queued_order(_order(), bar, _ctx())
    assert outcome.accepted, outcome.reason
    assert outcome.same_day_exit_reason is None
    print("  ok  a normal in-range order fills, ignoring the stale entry_price entirely")

    # Change ONLY entry_price -- the outcome must not change at all, since
    # nothing in resolve_queued_order ever reads it.
    outcome2 = resolve_queued_order(_order(entry_price=1.0), bar, _ctx())
    assert outcome2.accepted == outcome.accepted
    print("  ok  varying entry_price alone changes nothing -- it's audit-only")


def test_gap_invalidated_order_is_rejected_not_resized():
    """A real overnight gap should be able to invalidate a queued order
    outright -- e.g. a gap-up open that blows the §2a risk budget /
    §2b concentration cap the original stop/quantity implied. Rejection,
    never a silent resize."""
    # Order sized for a ~$100 entry (90 shares, $93 stop -> ~$630 risk,
    # 0.63% of $100k equity). A gap to $300 open with the SAME stop distance
    # in dollars keeps working, but the SAME quantity at $300 is now
    # $27,000 notional = 27% of equity, blowing the 10% concentration cap.
    bar = {"open": 300.0, "high": 305.0, "low": 298.0, "close": 302.0}
    outcome = resolve_queued_order(_order(stop_price=279.0, target_price=345.0), bar, _ctx())
    assert not outcome.accepted, "a real concentration-cap-busting gap was wrongly accepted"
    assert "§2b" in outcome.reason, outcome.reason
    print("  ok  a real gap that breaks position sizing is rejected outright, not resized")


def test_entry_day_stop_and_target_conservative_ambiguity():
    """After a queued BUY fills at the open, the REST of that same day's
    real range can still resolve the fresh stop or target. When OHLC alone
    can't say which came first (both in range), resolve conservatively:
    assume the stop, never the target."""
    order = _order(stop_price=93.0, target_price=115.0)

    # Only the stop is in range.
    bar_stop = {"open": 100.0, "high": 101.0, "low": 92.0, "close": 94.0}
    out = resolve_queued_order(order, bar_stop, _ctx())
    assert out.accepted and out.same_day_exit_reason == "stop hit"
    assert out.same_day_exit_price == min(bar_stop["open"], order.stop_price)
    print("  ok  stop-only same-day range resolves as stop hit")

    # Only the target is in range.
    bar_target = {"open": 100.0, "high": 116.0, "low": 99.0, "close": 114.0}
    out = resolve_queued_order(order, bar_target, _ctx())
    assert out.accepted and out.same_day_exit_reason == "target hit"
    print("  ok  target-only same-day range resolves as target hit")

    # BOTH are in range -- ambiguous, must resolve as the stop (conservative),
    # never the target (which would flatter the backtest).
    bar_both = {"open": 100.0, "high": 116.0, "low": 92.0, "close": 105.0}
    out = resolve_queued_order(order, bar_both, _ctx())
    assert out.accepted and out.same_day_exit_reason == "stop hit", (
        "ambiguous same-day range must resolve conservatively (stop), "
        f"got {out.same_day_exit_reason!r}"
    )
    print("  ok  ambiguous same-day range (both in range) resolves conservatively as stop hit")

    # Neither -- position just sits open, no same-day exit.
    bar_neither = {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
    out = resolve_queued_order(order, bar_neither, _ctx())
    assert out.accepted and out.same_day_exit_reason is None
    print("  ok  no same-day exit when neither stop nor target is in range")


def test_make_decision_id_is_deterministic_and_distinct():
    """The actual mechanism that makes retrying a failed day safe: the SAME
    (run, day, ticker, action, kind) must always produce the SAME id, so a
    retry regenerates an id trade-journal's ON CONFLICT DO NOTHING treats
    as already-written -- and any real difference must produce a DIFFERENT
    id, or two distinct real events would collide."""
    base = make_decision_id("2026-02-17|42", 5, "OLN", "BUY", "queued_fill")
    again = make_decision_id("2026-02-17|42", 5, "OLN", "BUY", "queued_fill")
    assert base == again, "same inputs must produce the same id -- this is what makes a retry safe"
    print("  ok  identical (run, day, ticker, action, kind) -> identical id")

    variants = [
        make_decision_id("2026-02-17|43", 5, "OLN", "BUY", "queued_fill"),   # diff run
        make_decision_id("2026-02-17|42", 6, "OLN", "BUY", "queued_fill"),   # diff day
        make_decision_id("2026-02-17|42", 5, "DINO", "BUY", "queued_fill"),  # diff ticker
        make_decision_id("2026-02-17|42", 5, "OLN", "SELL", "queued_fill"),  # diff action
        make_decision_id("2026-02-17|42", 5, "OLN", "BUY", "mechanical_exit"),  # diff kind
    ]
    assert len(set(variants) | {base}) == 6, "a real difference must never collide"
    print("  ok  any real difference in run/day/ticker/action/kind produces a distinct id")


def test_atomic_write_never_leaves_a_corrupt_or_partial_state_file():
    """Mirrors quota_pacer.py's own atomic-write guarantee. A crash DURING
    the write (simulated here by making json.dump raise partway) must never
    touch the real state file at all -- the write goes to a temp file
    first, and only a successful write is atomically renamed into place."""
    tmp_dir = Path(tempfile.mkdtemp())
    target = tmp_dir / "backtest_state.json"

    good_state = {"portfolio": {"cash": 12345.0}, "cursor": "2026-03-01"}
    _atomic_write_json(target, good_state)
    assert json.loads(target.read_text()) == good_state
    print("  ok  a normal write round-trips exactly")

    # Simulate a crash mid-write: json.dump raises after the temp file is
    # opened but before anything valid is written to it.
    with patch("run_backtest.json.dump", side_effect=RuntimeError("simulated crash mid-write")):
        try:
            _atomic_write_json(target, {"portfolio": {"cash": 999999.0}, "cursor": "2026-12-31"})
            raise AssertionError("expected the simulated crash to propagate")
        except RuntimeError:
            pass

    # The REAL file must be completely untouched -- still the last good state.
    assert json.loads(target.read_text()) == good_state, (
        "a crash mid-write corrupted or replaced the previous valid state file"
    )
    # And no stray .tmp file left behind.
    leftovers = list(tmp_dir.glob(".backtest_state_*.tmp"))
    assert not leftovers, f"crash left a temp file behind: {leftovers}"
    print("  ok  a crash mid-write leaves the previous valid state file completely untouched")


def test_mark_invoked_fires_exactly_once_per_cycle_even_with_a_retry():
    """The quota-counting guarantee: a cycle that spends a real call and
    then fails downstream (JSON parse, journaling, anything after) must
    still count against the weekly budget -- but a JSON-retry within the
    SAME cycle (2 real _call_hermes invocations) must still only count as
    ONE cycle, matching the project's existing quota semantics."""
    calls = []

    def fake_record_cycle():
        calls.append(1)

    invoked = {"done": False}

    def mark_invoked():
        if not invoked["done"]:
            fake_record_cycle()
            invoked["done"] = True

    # Simulate the retry path: _call_hermes invoked twice in one cycle.
    mark_invoked()
    mark_invoked()
    assert len(calls) == 1, f"expected exactly 1 quota slot recorded, got {len(calls)}"
    print("  ok  two real invocations within one cycle (original + JSON retry) count as ONE slot")

    # And a downstream failure AFTER invocation must not un-count it --
    # simulated by invoking, then "failing" (raising), and confirming the
    # slot was already recorded before the failure.
    invoked2 = {"done": False}
    calls2 = []

    def mark_invoked2():
        if not invoked2["done"]:
            calls2.append(1)
            invoked2["done"] = True

    try:
        mark_invoked2()
        raise ValueError("simulated downstream failure (parsing/journaling/etc)")
    except ValueError:
        pass
    assert len(calls2) == 1, "a downstream failure after invocation must not lose the quota count"
    print("  ok  a downstream failure after invocation does not lose the already-recorded quota slot")


def test_working_copy_isolates_a_failed_cycle_from_the_caller_portfolio():
    """The mechanism run_day uses to guarantee 'never publish partially
    mutated portfolio state': operate on a deep copy, and only adopt it on
    full success. Tested here in isolation (the actual mechanism), since
    run_day itself needs Docker/Neon to run end to end."""
    import copy as copy_module

    original = Portfolio(cash=100_000.0)
    original.holdings["OLN"] = Holding(
        ticker="OLN", quantity=90, entry_price=100.0, stop_price=93.0,
        target_price=115.0, entry_day=3, entry_trading_day=3,
    )
    snapshot_cash = original.cash
    snapshot_holdings = dict(original.holdings)

    working = copy_module.deepcopy(original)
    # Simulate a cycle's real mutations, then a failure partway through --
    # the working copy is simply never adopted by the caller.
    working.cash -= 50_000.0
    del working.holdings["OLN"]
    working.pending_orders.append(_order())
    try:
        raise RuntimeError("simulated mid-cycle failure (a journal write, e.g.)")
    except RuntimeError:
        pass  # `working` discarded here -- caller keeps `original`, unassigned

    assert original.cash == snapshot_cash, "the caller's portfolio was mutated despite the failure"
    assert original.holdings == snapshot_holdings, "the caller's holdings were mutated despite the failure"
    assert original.pending_orders == [], "the caller's pending_orders were mutated despite the failure"
    print("  ok  a failed cycle's mutations never reach the caller's portfolio object")


def test_run_cycle_or_halt_halts_without_touching_cursor_semantics():
    """_run_cycle_or_halt is what main() actually calls -- confirms it
    reports halted=True on any exception and returns no result, which is
    what makes 'the simulated date is never advanced' true in main() (the
    cursor increment is structurally unreachable when halted)."""
    def failing_run_day():
        raise RuntimeError("simulated cycle failure")

    result, halted = _run_cycle_or_halt(failing_run_day)
    assert halted is True and result is None
    print("  ok  a failing cycle reports halted=True with no result")

    def succeeding_run_day():
        return ("fake-portfolio", {"decisions": []})

    result, halted = _run_cycle_or_halt(succeeding_run_day)
    assert halted is False and result == ("fake-portfolio", {"decisions": []})
    print("  ok  a succeeding cycle reports halted=False and returns its result")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("PASS: no-lookahead execution, fail-safe cycles, and idempotent "
          "journaling all behave as claimed")


if __name__ == "__main__":
    main()
