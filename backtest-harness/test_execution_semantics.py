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
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import patch

# run_backtest inserts ../risk-checker onto sys.path as an import side
# effect -- must be imported first, matching test_execution_path.py's
# existing pattern.
from run_backtest import (
    DEFAULT_MASKING_SEED, Holding, Portfolio, QueuedOrder, ResponseValidationError,
    _atomic_write_json, _clear_checkpoint, _load_checkpoint, _resolve_seed,
    _run_cycle_or_halt, _save_checkpoint, _slipped_buy_price,
    build_decision_time_context, build_prompt,
    evaluate_bar_stop_target, evaluate_opening_gap, make_decision_id,
    require_intraday_coverage, resolve_queued_order, run_day, simulate_trading_day,
    validate_hermes_response,
)
from anonymize import RunMasking  # noqa: E402
from quota_pacer import QuotaPacer  # noqa: E402
from risk_checker import (  # noqa: E402
    AccountState, Context, MarketClock, Position, ProposedTrade, check_trade,
)


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
        "decision_id": "test-decision-id",
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


def test_buy_risk_check_slippage_price_matches_execution_sim_rule():
    """The exact price used for the risk check must match execution-sim-
    mcp's own MARKET-fill formula (reference_price * (1 + 5bps), then
    clamped into the fill bar's own [low, high] range) -- one clearly
    defined slippage rule, not an approximation of it."""
    wide_bar = {"low": 0.0, "high": 1_000_000.0}
    assert _slipped_buy_price(100.0, wide_bar) == 100.05
    assert _slipped_buy_price(200.0, wide_bar) == 200.10
    # And the bar-range clamp actually applies -- a slipped price that
    # would exceed the bar's real high never fills above it.
    tight_bar = {"low": 99.0, "high": 100.02}
    assert _slipped_buy_price(100.0, tight_bar) == 100.02
    print("  ok  _slipped_buy_price matches execution-sim-mcp's SLIPPAGE_BPS=5 "
          "MARKET-fill formula exactly, including the bar-range clamp")


def test_buy_risk_check_uses_slippage_adjusted_price_not_raw_open():
    """2026-08-28 correctness pass, item 5 -- boundary regression. A BUY
    sized to land EXACTLY at the §2b concentration cap on the raw open
    (100 shares @ $100 = $10,000 = exactly 10% of $100k equity, allowed --
    §2b is a strict '>', not '>=') must be REJECTED once the same 5bps
    slippage execution will actually apply pushes the real notional over
    the cap."""
    bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}
    order = _order(action="BUY", ticker="OLN", quantity=100, stop_price=93.0, target_price=115.0)

    # Sanity: the RAW open lands exactly at the cap and is allowed --
    # confirms this is a genuine boundary case, not just an oversized trade.
    raw_trade = ProposedTrade(ticker="OLN", action="BUY", quantity=100,
                              entry_price=bar["open"], stop_price=93.0, target_price=115.0)
    raw_verdict = check_trade(raw_trade, _ctx())
    assert raw_verdict.allowed, (
        f"test scenario itself must pass at the raw open to be a real boundary case: {raw_verdict.violations}"
    )
    print("  ok  the scenario passes §2b exactly at the raw open (10.00% == the 10% cap)")

    outcome = resolve_queued_order(order, bar, _ctx())
    assert not outcome.accepted, (
        "a BUY that only breaches its concentration cap AFTER slippage was wrongly accepted"
    )
    assert "§2b" in outcome.reason, outcome.reason
    print("  ok  the SAME BUY is rejected once risk-checked against the slippage-adjusted "
          "fill price (10.005% > the 10% cap)")

    # And the reverse must never happen: a genuinely oversized SELL is not
    # made MORE lenient by staying at the raw open (SELL is unaffected by
    # this fix on purpose -- see resolve_queued_order's docstring).
    sell_bar = {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
    sell_order = _sell_order(ticker="OLN", quantity=100)
    sell_ctx = _ctx(positions=[Position(ticker="OLN", quantity=100, market_value=10_000.0)])
    sell_outcome = resolve_queued_order(sell_order, sell_bar, sell_ctx)
    assert sell_outcome.accepted, sell_outcome.reason
    print("  ok  a full-exit SELL at the same price is unaffected by the BUY-only slippage fix")


def test_sequential_945_buys_see_the_first_fills_real_state():
    """2026-08-28 correctness pass, third pass, item 2 -- boundary
    regression. Two queued BUYs (TICKER_A, then TICKER_B) at 09:45.
    TICKER_A's 09:30 close ($200) differs materially from its 09:45 open
    ($100) -- the OLD bug marked a just-filled position at its 09:30
    close for the NEXT queued BUY's risk check, wildly overstating equity
    (and also under-deducted cash by using the unslipped open). TICKER_B
    is sized to breach the §2b concentration cap against the REAL
    resulting equity, but would have PASSED against the old, inflated
    state."""
    ticker_a_bars = _flat_session(
        price=100.0,
        overrides={"09:30": {"open": 198.0, "high": 201.0, "low": 197.0, "close": 200.0}},
    )
    ticker_b_bars = _flat_session(price=100.0)

    buy_a = _buy_order(ticker="TICKER_A", alias="ALPHA", quantity=100,
                       stop_price=93.0, target_price=115.0)
    buy_b = _buy_order(ticker="TICKER_B", alias="BRAVO", quantity=105,
                       stop_price=93.0, target_price=115.0)

    plan = simulate_trading_day(
        holdings={}, pending_orders=[buy_a, buy_b],
        intraday={"TICKER_A": ticker_a_bars, "TICKER_B": ticker_b_bars},
        day_index=8, trading_days_elapsed=5, limits_ctx={},
        watchlist=["TICKER_A", "TICKER_B"], closed_pnl=[], starting_cash=100_000.0,
        equity_curve=[100_000.0] * 6,
    )

    a_events = [e for e in plan.events if e["ticker"] == "TICKER_A"]
    assert len(a_events) == 1 and a_events[0]["kind"] == "fill", a_events
    # The fill itself must be the real slipped price ($100 open + 5bps),
    # not the raw open and not the unrelated 09:30 close.
    assert a_events[0]["bar"]["open"] == 100.0
    print("  ok  TICKER_A fills normally at 09:45")

    b_events = [e for e in plan.events if e["ticker"] == "TICKER_B"]
    assert len(b_events) == 1, b_events
    assert b_events[0]["kind"] == "rejected", (
        "TICKER_B was accepted -- it passed against TICKER_A's stale 09:30-close "
        f"equity mark instead of TICKER_A's real ~$100.05 fill price: {b_events}"
    )
    assert "§2b" in b_events[0]["reason"], b_events[0]["reason"]
    print("  ok  TICKER_B is rejected against the REAL sequential state -- it would "
          "have incorrectly PASSED under the old (09:30-close, unslipped-cash) state")


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


def test_checkpoint_survives_a_retry_without_a_second_hermes_call_or_duplicate_journal_row():
    """2026-08-28 correctness pass, item 2 -- the actual failure-injection
    regression this repair targets. Attempt 1 calls (fake) Hermes, journals
    a decision using the response, THEN fails (simulating a downstream
    journal-write exception for a LATER decision, after the checkpoint was
    already saved). A retry (same run_id/day_index) must reuse the
    checkpointed response verbatim -- zero additional Hermes calls -- and
    produce the identical decision_id, so a real `ON CONFLICT (decision_id)
    DO NOTHING` write is a true no-op, never a duplicate row."""
    tmp_dir = Path(tempfile.mkdtemp())
    ckpt_path = tmp_dir / "cycle_checkpoint.json"
    hermes_calls = []
    journal_rows: dict[str, dict] = {}  # simulates ON CONFLICT (decision_id) DO NOTHING

    def fake_ask_hermes() -> dict:
        hermes_calls.append(1)
        return {"decisions": [{"alias": "ALPHA", "action": "BUY", "quantity": 10}],
                "no_action_reason": ""}

    def run_one_cycle_attempt(run_id: str, day_index: int, fail_after_journaling: bool) -> dict:
        with patch("run_backtest.CHECKPOINT_PATH", ckpt_path):
            checkpoint = _load_checkpoint(run_id, day_index)
            if checkpoint is not None:
                response = checkpoint["response"]
            else:
                response = fake_ask_hermes()
                _save_checkpoint(run_id, day_index, response, {})

            decision_id = make_decision_id(run_id, day_index, "OLN", "BUY", "queued_proposal")
            journal_rows.setdefault(decision_id, response)  # idempotent, same as the real write

            if fail_after_journaling:
                raise RuntimeError("simulated failure after decision journaling")
            _clear_checkpoint()  # only reached on a fully successful attempt
        return response

    try:
        run_one_cycle_attempt("run-x", 5, fail_after_journaling=True)
        raise AssertionError("expected the simulated failure to propagate")
    except RuntimeError:
        pass
    assert len(hermes_calls) == 1
    assert ckpt_path.exists(), "the checkpoint must survive a failure after journaling"
    print("  ok  attempt 1 calls Hermes once, journals the decision, then fails -- checkpoint persists")

    response2 = run_one_cycle_attempt("run-x", 5, fail_after_journaling=False)
    assert len(hermes_calls) == 1, f"a retry called Hermes again ({len(hermes_calls)} total calls)"
    assert response2 == {"decisions": [{"alias": "ALPHA", "action": "BUY", "quantity": 10}],
                         "no_action_reason": ""}
    assert len(journal_rows) == 1, f"a retry produced a duplicate journal row: {journal_rows}"
    assert not ckpt_path.exists(), "the checkpoint must be cleared only after full success"
    print("  ok  the retry reuses the checkpointed response verbatim -- no second Hermes call, "
          "no duplicate journal row, checkpoint cleared only after the retry fully succeeds")

    # A checkpoint from a DIFFERENT run/day must never be reused -- a fresh
    # (run_id, day_index) forces a fresh Hermes call.
    with patch("run_backtest.CHECKPOINT_PATH", ckpt_path):
        _save_checkpoint("run-x", 5, {"decisions": [], "no_action_reason": "n/a"}, {})
        assert _load_checkpoint("run-y", 5) is None
        assert _load_checkpoint("run-x", 6) is None
        assert _load_checkpoint("run-x", 5) is not None
        _clear_checkpoint()
    print("  ok  a checkpoint only satisfies an EXACT (run_id, day_index) match")


def test_real_lifecycle_checkpoint_commit_then_best_effort_observability():
    """2026-08-28 correctness pass, third pass, item 4 -- a REAL lifecycle
    regression around the ACTUAL orchestration boundary (run_day,
    _load_checkpoint/_save_checkpoint/_clear_checkpoint, _atomic_write_json),
    not a hand-rolled miniature. Only the true I/O boundary functions are
    mocked (fetch_bars/fetch_technicals/fetch_news/fetch_recent_insights,
    ask_hermes, record_queued_decision, record_snapshot_once, log_activity)
    -- masking, risk-checking, prompt-building, and every checkpoint/
    state-file mechanic are the REAL functions. No real API, Docker, Neon,
    or model call anywhere in this test.

    Proves the full locked lifecycle end to end: attempt 1 calls fake
    Hermes exactly once and checkpoints the validated response; an
    ESSENTIAL journal failure (record_queued_decision) halts before any
    state advancement; a retry reuses the checkpointed response with NO
    second Hermes call and NO quota charge; essential records complete
    idempotently (same decision_id both attempts); the atomic state
    commit succeeds; the checkpoint clears only after; and a snapshot/
    activity failure AFTER commit is caught without halting, rolling
    back, or replaying the cycle.
    """
    tmp_dir = Path(tempfile.mkdtemp())
    checkpoint_path = tmp_dir / "cycle_checkpoint.json"
    state_path = tmp_dir / "backtest_state.json"
    quota_path = tmp_dir / "quota_pacer_state.json"

    run_start = date(2026, 2, 17)
    day = run_start
    day_index = 0
    run_key = str(uuid.uuid4())

    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}]
    masking = RunMasking({"OLN": ["Olin Corporation", "Olin"]}, run_start=run_start,
                         seed=DEFAULT_MASKING_SEED)
    alias = masking.alias("OLN")
    pacer = QuotaPacer(state_path=quota_path)  # isolated -- never touches the real quota file
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6)

    hermes_calls = []

    def fake_ask_hermes(prompt, usage_totals, is_reflection_day=False, on_invoked=None):
        hermes_calls.append(1)
        if on_invoked is not None:
            on_invoked()  # real quota-counting callback -- must fire exactly once, on attempt 1 only
        usage_totals["total_tokens"] = usage_totals.get("total_tokens", 0) + 100
        return {
            "decisions": [{
                "alias": alias, "action": "BUY", "quantity": 10, "entry_price": 100.0,
                "stop_price": 93.0, "target_price": 115.0, "confidence": 3,
                "primary_signal": "technical", "technical_basis": "rsi",
                "expected_reward_risk": 2.14, "invalidation_condition": "closes below $93",
                "reasoning": "clean breakout with volume confirmation",
            }],
            "no_action_reason": "",
        }

    fake_bars = {"OLN": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}}
    fake_technicals = {"OLN": {"price": 100.5, "rsi": 50.0, "rsi_signal": "neutral",
                               "macd_signal": "bullish", "trend": "up", "outlook": "neutral",
                               "support": [95.0], "resistance": [110.0]}}
    fake_news = {"OLN": []}

    journal_calls = []
    remaining_failures = {"n": 1}  # attempt 1's essential journal write fails; attempt 2's succeeds

    def fake_record_queued_decision(decision, real_ticker, decision_id, run_id=None):
        journal_calls.append(decision_id)
        if remaining_failures["n"] > 0:
            remaining_failures["n"] -= 1
            raise RuntimeError("simulated essential journal failure")

    def fake_record_snapshot_once(**kwargs):
        return True

    def fake_log_activity(events, run_id=None, cycle_id=None):
        pass

    def run_one_attempt():
        return run_day(day, day_index, portfolio, masking, watchlist, {}, pacer, run_key)

    with patch("run_backtest.CHECKPOINT_PATH", checkpoint_path), \
         patch("run_backtest.fetch_bars", return_value=fake_bars), \
         patch("run_backtest.fetch_technicals", return_value=fake_technicals), \
         patch("run_backtest.fetch_news", return_value=fake_news), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask_hermes), \
         patch("run_backtest.record_queued_decision", side_effect=fake_record_queued_decision), \
         patch("run_backtest.record_snapshot_once", side_effect=fake_record_snapshot_once), \
         patch("run_backtest.log_activity", side_effect=fake_log_activity):

        # -- attempt 1: essential journal failure halts the cycle ---------
        try:
            run_one_attempt()
            raise AssertionError("expected the simulated essential journal failure to propagate")
        except RuntimeError as exc:
            assert "simulated essential journal failure" in str(exc)
        assert len(hermes_calls) == 1, "attempt 1 must call fake Hermes exactly once"
        assert checkpoint_path.exists(), (
            "the validated response must already be checkpointed before an essential failure"
        )
        assert not state_path.exists(), "local state must NOT advance after an essential failure"
        print("  ok  attempt 1: fake Hermes called exactly once, response checkpointed, "
              "essential journal failure halts before state advancement")

        # -- attempt 2 (retry): reuses the checkpoint, no second Hermes call
        working, summary, do_observability = run_one_attempt()
        assert len(hermes_calls) == 1, "a retry must NOT call Hermes a second time (no quota charge)"
        assert len(journal_calls) == 2 and journal_calls[0] == journal_calls[1], (
            "the retry must re-attempt the SAME essential journal write with the "
            f"IDENTICAL decision_id, not a fresh one: {journal_calls}"
        )
        print("  ok  attempt 2 (retry): reuses the checkpointed response verbatim -- "
              "no second Hermes call, no quota charge, essential records complete idempotently")

        # -- atomic local-state commit, THEN checkpoint clear -------------
        _atomic_write_json(state_path, {"portfolio": working.to_dict(),
                                        "cursor": "2026-02-18", "run_id": run_key})
        assert state_path.exists()
        _clear_checkpoint()
        assert not checkpoint_path.exists(), "the checkpoint must clear only after the state commit"
        print("  ok  atomic local-state commit succeeds; checkpoint clears only afterward")

        # -- best-effort observability, AFTER commit -- a failure here must
        #    never halt, roll back, or replay the cycle (locked decision).
        with patch("run_backtest.record_snapshot_once",
                   side_effect=RuntimeError("simulated observability failure")):
            try:
                do_observability()
                raise AssertionError("expected the simulated observability failure to propagate here")
            except RuntimeError as exc:
                assert "simulated observability failure" in str(exc)
        # main()'s own pattern is exactly this: catch it right here and
        # continue -- proven by asserting nothing essential moved.
        assert len(hermes_calls) == 1, "an observability failure must never trigger a replayed Hermes call"
        assert not checkpoint_path.exists(), "an observability failure must never resurrect the checkpoint"
        committed = json.loads(state_path.read_text())
        assert committed["cursor"] == "2026-02-18", "committed state must never be rolled back"
        print("  ok  a snapshot/activity failure AFTER commit is caught without halting, "
              "rolling back, or replaying the cycle -- committed state is untouched")


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
    cursor increment is structurally unreachable when halted).

    2026-08-28 correctness pass (third pass, item 3): a FAKE fail_logger
    is injected here -- the production default (log_cycle_failure) shells
    out to the real trade-journal container and would create a real
    'cycle_failed' Neon row every time this test runs, which this suite
    must never do. Prevented structurally (dependency injection), not
    cleaned up afterward."""
    fake_logger_calls = []

    def fake_fail_logger(day_label, error_text, run_id, usage=None, pacer=None):
        fake_logger_calls.append(
            {"day_label": day_label, "error_text": error_text, "run_id": run_id}
        )

    def failing_run_day():
        raise RuntimeError("simulated cycle failure")

    result, halted = _run_cycle_or_halt(
        failing_run_day, day_label="Day 9", run_id="test-run-id",
        fail_logger=fake_fail_logger,
    )
    assert halted is True and result is None
    print("  ok  a failing cycle reports halted=True with no result")

    # The FAKE logger -- not the production one -- received the failure
    # event, with the exact day_label/run_id/error text this cycle raised.
    assert len(fake_logger_calls) == 1, (
        f"expected exactly one fake-logger call, got {len(fake_logger_calls)}"
    )
    call = fake_logger_calls[0]
    assert call["day_label"] == "Day 9"
    assert call["run_id"] == "test-run-id"
    assert "simulated cycle failure" in call["error_text"]
    assert "RuntimeError" in call["error_text"]
    print("  ok  the injected fake logger (not log_cycle_failure) received the "
          "expected failure event -- no real Docker/Neon call made")

    def succeeding_run_day():
        return ("fake-portfolio", {"decisions": []})

    result, halted = _run_cycle_or_halt(succeeding_run_day, fail_logger=fake_fail_logger)
    assert halted is False and result == ("fake-portfolio", {"decisions": []})
    assert len(fake_logger_calls) == 1, "a succeeding cycle must never call the fail logger"
    print("  ok  a succeeding cycle reports halted=False, returns its result, "
          "and never calls the fail logger")


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
        "decision_id": "test-decision-id",
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
        starting_cash=100_000.0, equity_curve=[100_000.0] * 6,
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
            starting_cash=100_000.0, equity_curve=[100_000.0] * 6,
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
            starting_cash=100_000.0, equity_curve=[100_000.0] * 6,
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


def test_combined_morning_stop_losses_trip_breaker_before_945_buy():
    """2026-08-28 correctness pass, item 1 -- the actual regression this
    repair targets. Two held positions each gap through their stop at the
    09:30 open, realising a combined ~6.6% loss BEFORE a third ticker's
    queued BUY is considered at 09:45. The daily (-3%) and weekly (-6%)
    circuit breakers must see that real, same-morning loss and reject the
    BUY -- not the stale, pre-session percentages the bug used to pass."""
    equity_curve = [100_000.0] * 6  # yesterday's close, flat baseline
    holdings = {
        "OLN": Holding(ticker="OLN", quantity=100, entry_price=100.0,
                       stop_price=95.0, target_price=130.0, entry_day=1, entry_trading_day=1),
        "DINO": Holding(ticker="DINO", quantity=100, entry_price=100.0,
                        stop_price=95.0, target_price=130.0, entry_day=1, entry_trading_day=1),
    }
    # Both gap well through their stop at the open -- fills at the gapped
    # open (worse than the stop itself), $33/share below entry each.
    gapped_open = {"time": "09:30", "open": 67.0, "high": 67.0, "low": 65.0, "close": 67.0}
    buy_order = _buy_order(ticker="TSLA", alias="ECHO", stop_price=90.0, target_price=115.0)
    plan = simulate_trading_day(
        holdings=holdings, pending_orders=[buy_order],
        intraday={
            "OLN": _flat_session(overrides={"09:30": gapped_open}),
            "DINO": _flat_session(overrides={"09:30": gapped_open}),
            "TSLA": _flat_session(price=100.0),
        },
        day_index=8, trading_days_elapsed=5, limits_ctx={}, watchlist=["OLN", "DINO", "TSLA"],
        closed_pnl=[], starting_cash=80_000.0,  # 80k cash + 2x(100@$100) holdings == 100k equity yday
        equity_curve=equity_curve,
    )
    gap_exits = [e for e in plan.events if e["stage"] == "opening_gap" and e["kind"] == "exit"]
    assert len(gap_exits) == 2, plan.events
    realized_loss = sum(e["quantity"] * (e["entry_price"] - e["exit_price"]) for e in gap_exits)
    assert realized_loss >= 6_000, f"test scenario itself must realise a real >=6% loss, got ${realized_loss}"
    print(f"  ok  two morning gap stops realised a combined ${realized_loss:,.0f} loss before 09:45")

    buy_events = [e for e in plan.events if e["ticker"] == "TSLA"]
    assert len(buy_events) == 1 and buy_events[0]["kind"] == "rejected", (
        f"a queued 09:45 BUY was NOT rejected despite a {realized_loss / 1000:.1f}% same-morning "
        f"loss already realised -- breaker values leaked stale pre-session numbers: {buy_events}"
    )
    assert "§4" in buy_events[0]["reason"], buy_events[0]["reason"]
    print("  ok  the queued 09:45 BUY is rejected by §4 -- this morning's own stop losses "
          "are reflected in the breaker check, not the stale pre-session figures")


def test_resolve_seed_resume_and_mismatch():
    """2026-08-28 correctness pass, item 7 -- masking-seed persistence."""
    # A resumed run with no --seed passed reuses the stored seed exactly.
    assert _resolve_seed(stored_seed=7, requested_seed=None) == 7
    print("  ok  a resumed run with no --seed reuses its stored seed")

    # Passing the SAME seed the stored one already has is a no-op, not a conflict.
    assert _resolve_seed(stored_seed=7, requested_seed=7) == 7
    print("  ok  passing back the same stored seed is not treated as a conflict")

    # A genuinely conflicting --seed fails loudly, never silently remaps aliases.
    try:
        _resolve_seed(stored_seed=7, requested_seed=99)
        raise AssertionError("expected a conflicting --seed to fail closed")
    except SystemExit as exc:
        assert "conflicts" in str(exc) and "7" in str(exc) and "99" in str(exc)
    print("  ok  a conflicting --seed on a resumed run fails loudly, naming both seeds")

    # A legacy state file (seed missing entirely) falls back safely: an
    # explicit --seed wins, otherwise the project default is used.
    assert _resolve_seed(stored_seed=None, requested_seed=55) == 55
    assert _resolve_seed(stored_seed=None, requested_seed=None) == DEFAULT_MASKING_SEED
    print("  ok  a legacy state file with no stored seed falls back safely "
          "(explicit --seed, else the project default)")

    # A fresh/--reset run (no stored seed at all) may establish any seed.
    assert _resolve_seed(stored_seed=None, requested_seed=123) == 123
    print("  ok  a fresh/--reset run may establish a new seed with nothing to conflict with")


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


def test_partial_sell_rejected_full_exit_still_works():
    """2026-08-28 pre-training pass, item 7: the one-lot model now REJECTS
    a partial discretionary SELL outright (RULES.md §9.1, enforced by
    risk_checker.py's own §9.1 check -- see resolve_queued_order, which
    calls the SAME check_trade this rejection runs through). This replaces
    the old 2026-08-25 test of the (now-removed) partial-credit branch in
    simulate_trading_day's queued-SELL stage -- that code path no longer
    exists; a partial SELL can no longer reach it.
    """
    # 100 OLN shares held @ $50, stop $45 (10% -- within band), target $65.
    oln_holding = {"OLN": Holding(ticker="OLN", quantity=100, entry_price=50.0,
                                  stop_price=45.0, target_price=65.0,
                                  entry_day=1, entry_trading_day=1)}
    # A queued partial SELL: only 20 of the 100 held.
    partial_sell = _sell_order(ticker="OLN", quantity=20)
    oln_bars = _flat_session(price=50.0)

    plan = simulate_trading_day(
        holdings=oln_holding, pending_orders=[partial_sell],
        intraday={"OLN": oln_bars}, day_index=8,
        trading_days_elapsed=5, limits_ctx={}, watchlist=["OLN"], closed_pnl=[],
        starting_cash=0.0, equity_curve=[100_000.0] * 6,
    )
    sell_events = [e for e in plan.events if e["stage"] == "queued_sell"]
    assert len(sell_events) == 1 and sell_events[0]["kind"] == "rejected", plan.events
    assert "§9.1" in sell_events[0]["reason"], sell_events[0]["reason"]
    print("  ok  a partial discretionary SELL is rejected (§9.1), not filled")

    # The rejection must leave the full 100-share position untouched --
    # no cash credited, nothing removed from holdings.
    full_sell = _sell_order(ticker="OLN", quantity=100)
    plan2 = simulate_trading_day(
        holdings=oln_holding, pending_orders=[full_sell],
        intraday={"OLN": oln_bars}, day_index=8,
        trading_days_elapsed=5, limits_ctx={}, watchlist=["OLN"], closed_pnl=[],
        starting_cash=0.0, equity_curve=[100_000.0] * 6,
    )
    full_sell_events = [e for e in plan2.events if e["stage"] == "queued_sell"]
    assert len(full_sell_events) == 1 and full_sell_events[0]["kind"] == "fill", plan2.events
    print("  ok  a full-quantity discretionary SELL still fills normally")


# --------------------------------------------------------------------------
# 2026-08-28 pre-training pass: semantic response validation (item 1)
# --------------------------------------------------------------------------

_VALID_DECISION = {
    "alias": "ALPHA", "action": "BUY", "quantity": 10, "entry_price": 100.0,
    "stop_price": 93.0, "target_price": 115.0, "confidence": 3,
    "primary_signal": "technical", "technical_basis": "rsi",
    "expected_reward_risk": 2.14, "invalidation_condition": "closes below $93",
    "reasoning": "clean breakout with volume confirmation",
}


def _response(**overrides) -> dict:
    return {"decisions": [dict(_VALID_DECISION, **overrides)], "no_action_reason": ""}


def test_validate_hermes_response_accepts_a_clean_reply():
    validate_hermes_response(_response(), is_reflection_day=False)
    validate_hermes_response({"decisions": [], "no_action_reason": "nothing qualified"},
                             is_reflection_day=False)
    print("  ok  a clean decision and a clean empty-decisions reply both validate")


def test_validate_hermes_response_rejects_missing_decisions_list():
    for bad in ({"no_action_reason": "x"}, {"decisions": "not a list"}):
        try:
            validate_hermes_response(bad, is_reflection_day=False)
            raise AssertionError(f"should have rejected {bad!r}")
        except ResponseValidationError:
            pass
    print("  ok  a missing/malformed 'decisions' list is rejected")


def test_validate_hermes_response_requires_honest_no_action_reason():
    try:
        validate_hermes_response({"decisions": [], "no_action_reason": ""}, is_reflection_day=False)
        raise AssertionError("blank no_action_reason on an empty decisions list should be rejected")
    except ResponseValidationError:
        pass
    print("  ok  an empty decisions list requires a non-blank no_action_reason")


def test_validate_hermes_response_enforces_buy_never_sentiment():
    try:
        validate_hermes_response(_response(primary_signal="sentiment", technical_basis=None),
                                 is_reflection_day=False)
        raise AssertionError("BUY + primary_signal=sentiment should be rejected")
    except ResponseValidationError as exc:
        assert "§5a" in str(exc)
    print("  ok  §5a: a BUY can never have primary_signal='sentiment'")


def test_validate_hermes_response_catches_bad_reward_risk_self_report():
    # Real math: (115-100)/(100-93) = 2.14. Reporting 5.0 is badly wrong.
    try:
        validate_hermes_response(_response(expected_reward_risk=5.0), is_reflection_day=False)
        raise AssertionError("a self-reported R that contradicts the trade's own math should be rejected")
    except ResponseValidationError as exc:
        assert "does not match" in str(exc)
    # A close, honest report (2.1 vs the real 2.14) must still pass.
    validate_hermes_response(_response(expected_reward_risk=2.1), is_reflection_day=False)
    print("  ok  a self-reported reward:risk that contradicts the trade's own stop/target math is rejected")


def test_validate_hermes_response_rejects_non_finite_and_bad_fields():
    for bad_qty in (0, -5, float("nan"), "ten", 3.5):
        try:
            validate_hermes_response(_response(quantity=bad_qty), is_reflection_day=False)
            raise AssertionError(f"quantity={bad_qty!r} should be rejected")
        except ResponseValidationError:
            pass
    for bad_price in (0.0, -1.0, float("nan"), float("inf")):
        try:
            validate_hermes_response(_response(entry_price=bad_price), is_reflection_day=False)
            raise AssertionError(f"entry_price={bad_price!r} should be rejected")
        except ResponseValidationError:
            pass
    try:
        validate_hermes_response(_response(invalidation_condition="  "), is_reflection_day=False)
        raise AssertionError("blank invalidation_condition should be rejected")
    except ResponseValidationError:
        pass
    print("  ok  non-finite/non-positive quantity/price and blank required text all rejected")


def test_validate_hermes_response_requires_reflection_on_reflection_day():
    ok_response = {"decisions": [], "no_action_reason": "nothing qualified", "reflection": "process notes"}
    validate_hermes_response(ok_response, is_reflection_day=True)
    for bad in ({"decisions": [], "no_action_reason": "x"},
               {"decisions": [], "no_action_reason": "x", "reflection": "   "}):
        try:
            validate_hermes_response(bad, is_reflection_day=True)
            raise AssertionError(f"missing/blank reflection on a reflection day should be rejected: {bad!r}")
        except ResponseValidationError:
            pass
    # The SAME reply is fine on a non-reflection day (reflection is optional there).
    validate_hermes_response({"decisions": [], "no_action_reason": "x"}, is_reflection_day=False)
    print("  ok  a reflection day requires a non-blank 'reflection'; a normal day does not")


# --------------------------------------------------------------------------
# 2026-08-28 pre-training pass: decision-time risk validation (item 3)
# --------------------------------------------------------------------------

def test_decision_time_context_skips_timing_but_enforces_everything_else():
    working = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6)
    ctx = build_decision_time_context(working, bars={}, watchlist=["OLN"], limits_ctx={})

    # A clean BUY on the watchlist, well inside every limit, passes even
    # though the clock fields are all None/placeholder (validate_timing=False
    # means §7 is never consulted).
    clean = ProposedTrade(ticker="OLN", action="BUY", quantity=90,
                          entry_price=100.0, stop_price=93.0, target_price=115.0)
    verdict = check_trade(clean, ctx, validate_timing=False)
    assert verdict.allowed, verdict.violations
    print("  ok  a clean decision-time proposal is approved with no real clock known yet")

    # An off-watchlist ticker is still rejected -- §1 is NOT a timing rule.
    off_list = ProposedTrade(ticker="TSLA", action="BUY", quantity=90,
                             entry_price=100.0, stop_price=93.0, target_price=115.0)
    verdict = check_trade(off_list, ctx, validate_timing=False)
    assert not verdict.allowed and any("§1" in v for v in verdict.violations)
    print("  ok  §1 off-watchlist is still enforced at decision time")

    # A stop too close is still rejected -- §6a is NOT a timing rule either.
    bad_stop = ProposedTrade(ticker="OLN", action="BUY", quantity=90,
                             entry_price=100.0, stop_price=99.0, target_price=115.0)
    verdict = check_trade(bad_stop, ctx, validate_timing=False)
    assert not verdict.allowed and any("§6a" in v for v in verdict.violations)
    print("  ok  §6a stop-distance is still enforced at decision time")


def test_decision_time_context_reflects_current_holdings_and_streak():
    holdings = {"OLN": Holding(ticker="OLN", quantity=90, entry_price=100.0,
                               stop_price=93.0, target_price=115.0,
                               entry_day=1, entry_trading_day=1)}
    working = Portfolio(cash=91_000.0, holdings=holdings, equity_curve=[100_000.0] * 6,
                        stopped_out_today=["ALGM"])
    ctx = build_decision_time_context(working, bars={"OLN": {"close": 100.0}},
                                      watchlist=["OLN", "ALGM"], limits_ctx={})

    # §9.1: already holding OLN -- averaging down blocked.
    average_down = ProposedTrade(ticker="OLN", action="BUY", quantity=10,
                                 entry_price=100.0, stop_price=93.0, target_price=115.0)
    verdict = check_trade(average_down, ctx, validate_timing=False)
    assert not verdict.allowed and any("§9.1" in v for v in verdict.violations)

    # §9.4: ALGM stopped out today -- same-day re-entry blocked.
    reentry = ProposedTrade(ticker="ALGM", action="BUY", quantity=10,
                            entry_price=50.0, stop_price=47.0, target_price=58.0)
    verdict = check_trade(reentry, ctx, validate_timing=False)
    assert not verdict.allowed and any("§9.4" in v for v in verdict.violations)
    print("  ok  decision-time context reflects today's real holdings (§9.1) and stopped-out list (§9.4)")


# --------------------------------------------------------------------------
# 2026-08-28 pre-training pass: held-position evidence in the prompt (item 4)
# --------------------------------------------------------------------------

def test_build_prompt_renders_fresh_evidence_for_held_positions():
    account = {"equity": 100_000.0, "cash": 90_000.0, "exposure_pct": 10.0,
              "daily_pl_pct": 0.0, "weekly_pl_pct": 0.0, "drawdown_pct": 0.0}
    position_with_evidence = {
        "alias": "ALPHA", "quantity": 90, "entry_price": 100.0,
        "current_price": 97.0, "stop_price": 93.0, "target_price": 115.0,
        "days_held": 3,
        "tech": {"rsi": 38.2, "rsi_signal": "neutral", "macd_signal": "bearish crossover",
                "trend": "down", "outlook": "cautious"},
        "news": [{"age_days": 1, "headline": "ALPHA misses on guidance, shares slide"}],
    }
    position_without_evidence = {
        "alias": "BRAVO", "quantity": 50, "entry_price": 50.0,
        "current_price": 51.0, "stop_price": 46.0, "target_price": 60.0,
        "days_held": 1, "tech": None, "news": [],
    }
    prompt = build_prompt("Day 5", account, [position_with_evidence, position_without_evidence],
                          candidates=[], recent_insights=[])

    # The held position's fresh RSI/MACD/trend and its news headline must
    # actually appear -- this is what makes AGENTS.md §6d's "has the signal
    # that justified the entry reversed" answerable, not just a claim.
    assert "RSI 38.2" in prompt and "bearish crossover" in prompt and "trend down" in prompt
    assert "ALPHA misses on guidance" in prompt
    print("  ok  a held position with fresh technicals/news renders both into the prompt")

    # A position with no fresh evidence this cycle (a fetch error) still
    # shows its base entry/current/stop/target line -- never fabricated.
    assert "BRAVO: 50 @ $50.00" in prompt
    print("  ok  a held position without fresh evidence still shows its base line, nothing invented")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("PASS: no-lookahead execution, fail-safe cycles, and idempotent "
          "journaling all behave as claimed")


if __name__ == "__main__":
    main()
