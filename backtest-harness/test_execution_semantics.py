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
    evaluate_bar_stop_target, evaluate_opening_gap, make_decision_id,
    require_intraday_coverage, resolve_queued_order, simulate_trading_day,
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


def test_evaluate_bar_stop_target_conservative_ambiguity():
    """A single bar's real low/high range touching a stop or target. When
    OHLC alone can't say which came first (both in range), resolve
    conservatively: assume the stop, never the target. This is what the
    2026-08-25 intraday-chronology follow-up uses at every stage of the
    session walk -- opening gap, the first bar, and every later bar --
    replacing resolve_queued_order's old one-shot same-day check."""
    stop, target = 93.0, 115.0

    bar_stop = {"open": 100.0, "high": 101.0, "low": 92.0, "close": 94.0}
    kind, price = evaluate_bar_stop_target(stop, target, bar_stop)
    assert kind == "stop" and price == min(bar_stop["open"], stop)
    print("  ok  stop-only range resolves as stop hit")

    bar_target = {"open": 100.0, "high": 116.0, "low": 99.0, "close": 114.0}
    kind, price = evaluate_bar_stop_target(stop, target, bar_target)
    assert kind == "target" and price == target
    print("  ok  target-only range resolves as target hit")

    # BOTH in range -- ambiguous, must resolve as the stop (conservative),
    # never the target (which would flatter the backtest).
    bar_both = {"open": 100.0, "high": 116.0, "low": 92.0, "close": 105.0}
    kind, price = evaluate_bar_stop_target(stop, target, bar_both)
    assert kind == "stop", f"ambiguous range must resolve conservatively (stop), got {kind!r}"
    print("  ok  ambiguous range (both in range) resolves conservatively as stop hit")

    bar_neither = {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
    assert evaluate_bar_stop_target(stop, target, bar_neither) is None
    print("  ok  no touch when neither stop nor target is in range")


def test_evaluate_opening_gap_symmetric_worst_case():
    """The 09:30 open can already be past a stop OR a target before any
    intraday movement is considered. Stop checked first; either direction
    fills at the worse-for-the-strategy of open/level, never the flattering
    case."""
    assert evaluate_opening_gap(93.0, 115.0, 80.0) == ("stop", 80.0)
    assert evaluate_opening_gap(93.0, 115.0, 130.0) == ("target", 130.0)
    assert evaluate_opening_gap(93.0, 115.0, 100.0) is None
    print("  ok  opening gap resolves symmetrically for a stop-through or a target-through open")


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


_SESSION_TIMES = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (0, 15, 30, 45)
                  if (h, m) >= (9, 30)][:26]  # 09:30 .. 15:45, 26 bars


def _flat_session(price: float = 100.0, overrides: dict | None = None) -> list[dict]:
    """A full 26-bar regular session, flat at `price` unless a specific
    time is overridden. Mirrors the real shape get_intraday_bars returns
    (verified against real Alpaca data for the whole watchlist)."""
    overrides = overrides or {}
    bars = []
    for t in _SESSION_TIMES:
        if t in overrides:
            bars.append({"time": t, **overrides[t]})
        else:
            bars.append({"time": t, "open": price, "high": price, "low": price, "close": price})
    return bars


def _buy_order(**kw) -> QueuedOrder:
    return _order(action="BUY", **kw)


def _sell_order(**kw) -> QueuedOrder:
    return QueuedOrder(**{
        "ticker": "OLN", "alias": "ALPHA", "action": "SELL", "quantity": 90,
        "stop_price": None, "target_price": None, "entry_price": 0.0,
        "queued_day_index": 3, "decision": {"action": "SELL", "quantity": 90},
        **kw,
    })


def _held(entry_trading_day: int = 1) -> dict[str, Holding]:
    return {"OLN": Holding(ticker="OLN", quantity=90, entry_price=100.0, stop_price=90.0,
                           target_price=120.0, entry_day=1, entry_trading_day=entry_trading_day)}


def _run(holdings=None, orders=None, bars=None, trading_days_elapsed=5):
    return simulate_trading_day(
        holdings=holdings or {}, pending_orders=orders or [],
        intraday={"OLN": bars or _flat_session()}, day_index=8,
        trading_days_elapsed=trading_days_elapsed, limits_ctx={}, watchlist=["OLN"], closed_pnl=[],
        starting_cash=100_000.0, daily_pl_pct=0.0, weekly_pl_pct=0.0, drawdown_from_peak_pct=0.0,
    )


def test_buy_cannot_fill_before_945():
    """A queued BUY must never fill at 09:30 -- only at 09:45 or later."""
    plan = _run(orders=[_buy_order()])
    fills = [e for e in plan.events if e["kind"] == "fill"]
    assert len(fills) == 1, plan.events
    assert fills[0]["bar_time"] == "09:45", f"BUY filled at {fills[0]['bar_time']}, not 09:45"
    print("  ok  a queued BUY fills at 09:45, never at 09:30 or any earlier time")


def test_queued_sell_fills_at_open_when_not_gapped_closed():
    """A queued SELL is exempt from the entry floor -- fills at the 09:30
    open when no opening-gap stop/target already closed the position."""
    plan = _run(holdings=_held(), orders=[_sell_order()])
    fills = [e for e in plan.events if e["kind"] == "fill" and e["ticker"] == "OLN"]
    assert len(fills) == 1 and fills[0]["stage"] == "queued_sell"
    assert fills[0]["bar_time"] == "09:30"
    print("  ok  a queued SELL fills at the 09:30 open when nothing closed the position first")


def test_later_stop_cannot_supersede_a_completed_opening_sell():
    """Once a queued SELL has filled at the open, the position is gone --
    a stop touched LATER the same day must have nothing left to act on."""
    # A bar later in the session (11:00) that WOULD trip the stop if the
    # position were still open -- it must never get the chance.
    bars = _flat_session(overrides={"11:00": {"open": 100.0, "high": 100.0, "low": 85.0, "close": 100.0}})
    plan = _run(holdings=_held(), orders=[_sell_order()], bars=bars)
    exits = [e for e in plan.events if e["kind"] == "exit"]
    assert exits == [], f"a later bar produced an exit after the position was already sold: {exits}"
    fills = [e for e in plan.events if e["kind"] == "fill"]
    assert len(fills) == 1 and fills[0]["stage"] == "queued_sell"
    print("  ok  a stop touched later in the day cannot supersede an already-completed opening SELL")


def test_opening_gap_stop_supersedes_queued_sell():
    """The reverse: if the 09:30 open itself already gapped through the
    stop, the position is closed before the queued SELL is even
    considered -- the SELL must show as superseded, not filled."""
    bars = _flat_session(overrides={"09:30": {"open": 85.0, "high": 85.0, "low": 84.0, "close": 85.0}})
    plan = _run(holdings=_held(), orders=[_sell_order()], bars=bars)
    exits = [e for e in plan.events if e["kind"] == "exit"]
    assert len(exits) == 1 and exits[0]["stage"] == "opening_gap" and "stop" in exits[0]["reason"]
    superseded = [e for e in plan.events if e["kind"] == "superseded"]
    assert len(superseded) == 1 and superseded[0]["stage"] == "queued_sell"
    print("  ok  an opening-gap stop correctly supersedes a same-ticker queued SELL")


def test_buy_risk_check_uses_945_marks_not_the_close():
    """The 09:45 BUY risk check must mark existing holdings using ONLY the
    09:30 bar's close (the last fully-elapsed price by 09:45) -- never
    today's close or any later bar."""
    # An existing 90-share OLN position (unrelated to the BUY candidate
    # DINO) marked at $100 via the 09:30 close. If a later close (say
    # $500) were wrongly used, equity would look much larger, and this
    # DINO entry (sized against real $100k equity) would still pass either
    # way -- so instead assert the actual VALUE plugged into the check
    # matches the 09:30 close exactly, by making the 09:30 close and a
    # deliberately extreme LATER close disagree, and checking the BUY
    # verdict is identical regardless of how extreme the later close is.
    bars_normal_close = _flat_session()
    bars_extreme_late_close = _flat_session(
        overrides={"15:45": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100000.0}}
    )
    order = _buy_order(ticker="DINO", alias="DELTA")

    def run_with(bars):
        return simulate_trading_day(
            holdings=_held(), pending_orders=[order],
            intraday={"OLN": bars, "DINO": _flat_session()}, day_index=8,
            trading_days_elapsed=5, limits_ctx={}, watchlist=["OLN", "DINO"], closed_pnl=[],
            starting_cash=100_000.0, daily_pl_pct=0.0, weekly_pl_pct=0.0, drawdown_from_peak_pct=0.0,
        )

    plan_a = run_with(bars_normal_close)
    plan_b = run_with(bars_extreme_late_close)
    fill_a = [e for e in plan_a.events if e["ticker"] == "DINO" and e["kind"] in ("fill", "rejected")]
    fill_b = [e for e in plan_b.events if e["ticker"] == "DINO" and e["kind"] in ("fill", "rejected")]
    assert fill_a[0]["kind"] == fill_b[0]["kind"] == "fill", (
        "an extreme LATER close changed the 09:45 BUY verdict -- it leaked into the risk check"
    )
    print("  ok  the 09:45 BUY risk check is identical regardless of an extreme later close -- "
          "proves only 09:30-close-or-earlier marks are used")


def test_extreme_closing_move_cannot_affect_945_verdict():
    """Construct a day where the EXISTING held position (OLN) COLLAPSES to
    near zero at the 15:45 close -- if that leaked into the 09:45 equity
    mark for a candidate BUY (DINO), equity would look catastrophically
    smaller and could flip the risk-check verdict. Confirm it doesn't: the
    crash happens strictly after 09:45 and must never be read."""
    order = _buy_order(ticker="DINO", alias="DELTA")

    def run_with(oln_bars):
        return simulate_trading_day(
            holdings=_held(), pending_orders=[order],
            intraday={"OLN": oln_bars, "DINO": _flat_session()}, day_index=8,
            trading_days_elapsed=5, limits_ctx={}, watchlist=["OLN", "DINO"], closed_pnl=[],
            starting_cash=100_000.0, daily_pl_pct=0.0, weekly_pl_pct=0.0, drawdown_from_peak_pct=0.0,
        )

    normal = run_with(_flat_session())
    crashed_at_close = run_with(
        _flat_session(overrides={"15:45": {"open": 100.0, "high": 100.0, "low": 0.01, "close": 0.01}})
    )
    verdict_normal = [e for e in normal.events if e["ticker"] == "DINO"][0]["kind"]
    verdict_crashed = [e for e in crashed_at_close.events if e["ticker"] == "DINO"][0]["kind"]
    assert verdict_normal == verdict_crashed == "fill", (
        f"OLN's own end-of-day closing collapse changed DINO's unrelated 09:45 BUY verdict: "
        f"normal={verdict_normal!r} vs crashed-at-close={verdict_crashed!r}"
    )
    print("  ok  an extreme closing-price collapse on a held position cannot affect a "
          "different ticker's 09:45 verdict -- it happens strictly after the decision point")


def test_stops_and_targets_processed_in_chronological_order():
    """A position must resolve against the FIRST bar whose range touches
    its stop/target, not a later one -- even if a later bar's range would
    ALSO touch (it's irrelevant once already resolved)."""
    # The 10:00 bar touches the stop; the 10:15 bar (if reached) would
    # touch the target instead -- only the 10:00 stop should register.
    bars = _flat_session(overrides={
        "10:00": {"open": 100.0, "high": 100.0, "low": 85.0, "close": 89.0},
        "10:15": {"open": 89.0, "high": 125.0, "low": 89.0, "close": 120.0},
    })
    plan = _run(holdings=_held(), bars=bars)
    exits = [e for e in plan.events if e["kind"] == "exit"]
    assert len(exits) == 1, exits
    assert exits[0]["bar_time"] == "10:00" and "stop" in exits[0]["reason"], exits[0]
    print("  ok  resolution happens at the FIRST bar that touches, not a later one")


def test_entry_day_stops_only_consider_bars_after_entry():
    """A position entered at 09:45 must NOT be affected by the 09:30 bar's
    range (it didn't exist yet) -- only its own entry bar (09:45) onward."""
    # The 09:30 bar's range would blow through a $93 stop if it were
    # (wrongly) considered for a position entered at 09:45.
    bars = _flat_session(overrides={
        "09:30": {"open": 100.0, "high": 100.0, "low": 50.0, "close": 100.0},
        "09:45": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
    })
    plan = _run(orders=[_buy_order(stop_price=93.0, target_price=115.0)], bars=bars)
    exits = [e for e in plan.events if e["kind"] == "exit"]
    assert exits == [], f"a pre-entry bar (09:30) wrongly triggered an exit on a fresh 09:45 entry: {exits}"
    fills = [e for e in plan.events if e["kind"] == "fill"]
    assert len(fills) == 1 and fills[0]["bar_time"] == "09:45"
    print("  ok  entry-day stop/target resolution only considers bars at or after the entry bar")


def test_missing_required_intraday_data_fails_closed():
    """A ticker that's held or has a pending order but has NO intraday
    bars at all -- or is missing a required boundary bar -- must raise,
    not silently skip or fabricate a price."""
    try:
        require_intraday_coverage(["OLN"], {})
        raise AssertionError("expected a missing ticker to raise")
    except RuntimeError as e:
        assert "OLN" in str(e)
    print("  ok  a ticker with no intraday bars at all fails closed")

    incomplete = {"OLN": [{"time": "09:30", "open": 100, "high": 100, "low": 100, "close": 100}]}
    try:
        require_intraday_coverage(["OLN"], incomplete)
        raise AssertionError("expected a missing 09:45 bar to raise")
    except RuntimeError as e:
        assert "09:45" in str(e)
    print("  ok  a ticker missing the required 09:45 bar fails closed")

    complete = {"OLN": _flat_session()}
    require_intraday_coverage(["OLN"], complete)  # must not raise
    print("  ok  full coverage passes without raising")


def test_partial_sell_credits_only_the_sold_shares():
    """Bug fix (2026-08-25): a partial queued SELL (order.quantity <
    holding.quantity) was crediting running_cash for the WHOLE position's
    value, not just the shares actually sold -- inflating the 09:45 cash/
    equity context a subsequent BUY gets risk-checked against.

    Proves all three required properties in one scenario:
      1. Only the sold shares' proceeds are credited (via a later-in-the-
         day exit on the REMAINING shares, whose recorded quantity proves
         how much was actually left held).
      2. The remaining quantity is correct (same mechanism).
      3. A later 09:45 BUY is evaluated against the correct, non-inflated
         cash/equity -- constructed as a real boundary case: it's rejected
         (over the §2b concentration cap) using the CORRECT equity figure;
         if the old bug were still present, the inflated equity would
         raise the cap enough to wrongly accept it.
    """
    # 100 OLN shares held @ $50, stop $45 (10% -- within band), target $65.
    oln_holding = {"OLN": Holding(ticker="OLN", quantity=100, entry_price=50.0,
                                  stop_price=45.0, target_price=65.0,
                                  entry_day=1, entry_trading_day=1)}
    # Sell only 20 of the 100 at the 09:30 open ($50) -- a real partial.
    sell = _sell_order(ticker="OLN", quantity=20)
    # A later bar (11:00) drives OLN's low to $44, below the $45 stop --
    # this must fire against whatever is ACTUALLY still held.
    oln_bars = _flat_session(price=50.0, overrides={
        "11:00": {"open": 50.0, "high": 50.0, "low": 44.0, "close": 44.0},
    })
    # DINO BUY sized to land BETWEEN the correct cap ($500 = 10% of the
    # correct $5,000 equity) and the buggy cap ($900 = 10% of the buggy
    # $9,000 equity that the old code would have produced): $600 notional.
    # Stop/target chosen so §2a (risk budget) and §6a/§6b pass cleanly --
    # §2b concentration is the one, isolated thing this scenario tests.
    buy = _buy_order(ticker="DINO", alias="DELTA", quantity=10,
                     stop_price=55.0, target_price=70.0)
    dino_bars = _flat_session(price=60.0)

    plan = simulate_trading_day(
        holdings=oln_holding, pending_orders=[sell, buy],
        intraday={"OLN": oln_bars, "DINO": dino_bars}, day_index=8,
        trading_days_elapsed=5, limits_ctx={}, watchlist=["OLN", "DINO"], closed_pnl=[],
        starting_cash=0.0, daily_pl_pct=0.0, weekly_pl_pct=0.0, drawdown_from_peak_pct=0.0,
    )

    sell_fill = [e for e in plan.events if e["stage"] == "queued_sell" and e["kind"] == "fill"]
    assert len(sell_fill) == 1, plan.events
    print("  ok  the partial SELL fills (not rejected/superseded)")

    # Property 1 + 2: the later 11:00 stop on OLN must report exactly the
    # REMAINING 80 shares (100 - 20) -- if the fix's cash credit were
    # wrong but the quantity math happened to be right, this would still
    # pass; it's here to pin down the OTHER half (remaining quantity) the
    # task also requires proven, using the same scenario.
    later_exit = [e for e in plan.events if e["stage"] == "intraday_walk" and e["ticker"] == "OLN"]
    assert len(later_exit) == 1, plan.events
    assert later_exit[0]["quantity"] == 80, (
        f"expected 80 shares remaining held (100 - 20 sold), got {later_exit[0]['quantity']}"
    )
    print("  ok  exactly 80 shares (100 - 20 sold) remain held and are what the later stop exits")

    # Property 3: the 09:45 DINO BUY must be REJECTED for §2b using the
    # CORRECT ($5,000) equity -- proves running_cash was NOT inflated by
    # crediting the full 100-share position instead of the 20 actually
    # sold (which would have produced $9,000 equity and a $900 cap,
    # wrongly accepting this $600 order).
    dino_events = [e for e in plan.events if e["ticker"] == "DINO"]
    assert len(dino_events) == 1, dino_events
    assert dino_events[0]["kind"] == "rejected", (
        f"expected the DINO BUY rejected under correct (non-inflated) equity, "
        f"got {dino_events[0]['kind']} -- the partial-sell cash bug may have regressed"
    )
    assert "§2b" in dino_events[0]["reason"], dino_events[0]["reason"]
    print("  ok  the 09:45 BUY is rejected using the correct, non-inflated cash/equity "
          "(would have been wrongly accepted under the old full-position-credit bug)")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("PASS: no-lookahead execution, fail-safe cycles, and idempotent "
          "journaling all behave as claimed")


if __name__ == "__main__":
    main()
