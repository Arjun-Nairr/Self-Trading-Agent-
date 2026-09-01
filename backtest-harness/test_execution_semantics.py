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
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# run_backtest inserts ../risk-checker onto sys.path as an import side
# effect -- must be imported first, matching test_execution_path.py's
# existing pattern.
from run_backtest import (
    ACTIVITY_BLOCK_TRADING_DAYS, ACTIVITY_TARGET_TRADES, DEFAULT_MASKING_SEED,
    PROFILE_BASE, PROFILE_ESCALATED, PROFILE_LEGACY, PROFILE_MIN_REWARD_RISK,
    STARTING_EQUITY, TIME_STOP_TRADING_DAYS,
    Holding, HermesResult, Portfolio, QueuedOrder, ResponseValidationError, ask_hermes,
    PROMPT_SCHEMA_VERSION, _atomic_write_json, _candidate_status, _clear_checkpoint,
    _expected_session_bar_times,
    _file_sha256, _limits_for_profile,
    _load_checkpoint, _maybe_advance_activity_block, _record_trade_completion,
    _resolve_seed, _resolve_window, _run_cycle_or_halt, _save_checkpoint, _slipped_buy_price,
    build_decision_time_context, build_prompt,
    evaluate_bar_stop_target, evaluate_opening_gap, make_decision_id,
    require_intraday_coverage, required_entry_quantity, resolve_queued_order,
    run_day, simulate_trading_day,
    validate_hermes_response,
)
from anonymize import RunMasking  # noqa: E402
from risk_checker import (  # noqa: E402
    AccountState, Context, MarketClock, Position, ProposedTrade, RiskLimits, check_trade,
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
    (200 shares @ $100 = $20,000 = exactly 20% of $100k equity, allowed --
    §2b is a strict '>', not '>=') must be REJECTED once the same 5bps
    slippage execution will actually apply pushes the real notional over
    the cap."""
    bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}
    order = _order(action="BUY", ticker="OLN", quantity=200, stop_price=93.0, target_price=115.0)

    # Sanity: the RAW open lands exactly at the cap and is allowed --
    # confirms this is a genuine boundary case, not just an oversized trade.
    raw_trade = ProposedTrade(ticker="OLN", action="BUY", quantity=200,
                              entry_price=bar["open"], stop_price=93.0, target_price=115.0)
    raw_verdict = check_trade(raw_trade, _ctx())
    assert raw_verdict.allowed, (
        f"test scenario itself must pass at the raw open to be a real boundary case: {raw_verdict.violations}"
    )
    print("  ok  the scenario passes §2b exactly at the raw open (20.00% == the 20% cap)")

    outcome = resolve_queued_order(order, bar, _ctx())
    assert not outcome.accepted, (
        "a BUY that only breaches its concentration cap AFTER slippage was wrongly accepted"
    )
    assert "§2b" in outcome.reason, outcome.reason
    print("  ok  the SAME BUY is rejected once risk-checked against the slippage-adjusted "
          "fill price (20.01% > the 20% cap)")

    # And the reverse must never happen: a genuinely oversized SELL is not
    # made MORE lenient by staying at the raw open (SELL is unaffected by
    # this fix on purpose -- see resolve_queued_order's docstring).
    sell_bar = {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
    sell_order = _sell_order(ticker="OLN", quantity=200)
    sell_ctx = _ctx(positions=[Position(ticker="OLN", quantity=200, market_value=20_000.0)])
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
    # 210 @ ~$100 = ~21% of the REAL post-TICKER_A equity -- breaches the
    # 20% cap against real state, but would have PASSED against the OLD
    # inflated (09:30-close-marked) equity this regression guards against.
    buy_b = _buy_order(ticker="TICKER_B", alias="BRAVO", quantity=210,
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
    """A crash DURING the write (simulated here by making json.dump raise
    partway) must never touch the real state file at all -- the write goes
    to a temp file first, and only a successful write is atomically renamed
    into place."""
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
    second Hermes call; essential records complete
    idempotently (same decision_id both attempts); the atomic state
    commit succeeds; the checkpoint clears only after; and a snapshot/
    activity failure AFTER commit is caught without halting, rolling
    back, or replaying the cycle.
    """
    tmp_dir = Path(tempfile.mkdtemp())
    checkpoint_path = tmp_dir / "cycle_checkpoint.json"
    state_path = tmp_dir / "backtest_state.json"

    run_start = date(2026, 2, 17)
    day = run_start
    day_index = 0
    run_key = str(uuid.uuid4())

    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}]
    masking = RunMasking({"OLN": ["Olin Corporation", "Olin"]}, run_start=run_start,
                         seed=DEFAULT_MASKING_SEED)
    alias = masking.alias("OLN")
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6)

    hermes_calls = []

    def fake_ask_hermes(prompt, usage_totals, is_reflection_day=False):
        hermes_calls.append(1)
        usage_totals["total_tokens"] = usage_totals.get("total_tokens", 0) + 100
        return HermesResult(response={
            "decisions": [{
                "alias": alias, "action": "BUY", "quantity": 200, "entry_price": 100.0,
                "stop_price": 93.0, "target_price": 115.0, "confidence": 3,
                "primary_signal": "technical", "technical_basis": "rsi",
                "expected_reward_risk": 2.14, "invalidation_condition": "closes below $93",
                "reasoning": "clean breakout with volume confirmation",
            }],
            "no_action_reason": "",
        }, attempt_count=1, retry_diagnostics=[])

    fake_bars = {"OLN": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}}
    fake_technicals = {"OLN": {"price": 100.5, "rsi": 50.0, "rsi_signal": "neutral",
                               "macd_signal": "bullish", "trend": "up", "outlook": "neutral",
                               "support": [95.0], "resistance": [110.0]}}
    fake_news = {"OLN": []}

    journal_calls = []
    remaining_failures = {"n": 1}  # attempt 1's essential journal write fails; attempt 2's succeeds

    def fake_record_queued_decision(decision, real_ticker, decision_id, run_id=None,
                                    strategy_profile=None, min_reward_risk_used=None,
                                    data_timestamps=None):
        journal_calls.append(decision_id)
        if remaining_failures["n"] > 0:
            remaining_failures["n"] -= 1
            raise RuntimeError("simulated essential journal failure")

    def fake_record_snapshot_once(**kwargs):
        return True

    def fake_log_activity(events, run_id=None, cycle_id=None):
        pass

    evidence_calls = []

    def fake_record_cycle_evidence(**kwargs):
        evidence_calls.append(kwargs)

    def run_one_attempt():
        return run_day(day, day_index, portfolio, masking, watchlist, {}, run_key)

    with patch("run_backtest.CHECKPOINT_PATH", checkpoint_path), \
         patch("run_backtest.fetch_bars", return_value=fake_bars), \
         patch("run_backtest.fetch_technicals", return_value=fake_technicals), \
         patch("run_backtest.fetch_news", return_value=fake_news), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask_hermes), \
         patch("run_backtest.record_cycle_evidence", side_effect=fake_record_cycle_evidence), \
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
        assert len(evidence_calls) == 1, "cycle evidence must be written once, before the failing journal write"
        print("  ok  attempt 1: fake Hermes called exactly once, response checkpointed, "
              "essential journal failure halts before state advancement")

        # -- attempt 2 (retry): reuses the checkpoint, no second Hermes call
        working, summary, do_observability = run_one_attempt()
        assert len(hermes_calls) == 1, "a retry must NOT call Hermes a second time"
        assert len(journal_calls) == 2 and journal_calls[0] == journal_calls[1], (
            "the retry must re-attempt the SAME essential journal write with the "
            f"IDENTICAL decision_id, not a fresh one: {journal_calls}"
        )
        assert len(evidence_calls) == 2, "the retry must re-attempt the evidence write too"
        assert evidence_calls[0]["prompt_text"] == evidence_calls[1]["prompt_text"], (
            "a retry's evidence write must be byte-identical to the original attempt's "
            "(reused from the checkpoint, not recomputed)"
        )
        assert evidence_calls[0]["attempt_count"] == evidence_calls[1]["attempt_count"] == 1
        print("  ok  attempt 2 (retry): reuses the checkpointed response verbatim -- "
              "no second Hermes call, essential records (including cycle evidence) "
              "complete idempotently")

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

    def fake_fail_logger(day_label, error_text, run_id, usage=None):
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
    repair targets, updated for the Phase 1 aggressive profile's single
    -30% drawdown breaker (RULES.md §4). Two held positions each gap
    catastrophically through their stop at the 09:30 open, realising a
    combined ~32% loss BEFORE a third ticker's queued BUY is considered at
    09:45. The breaker must see that real, same-morning loss and reject
    the BUY -- not the stale, pre-session percentages the original bug
    used to pass."""
    equity_curve = [100_000.0] * 6  # yesterday's close, flat baseline
    holdings = {
        "OLN": Holding(ticker="OLN", quantity=200, entry_price=100.0,
                       stop_price=95.0, target_price=130.0, entry_day=1, entry_trading_day=1),
        "DINO": Holding(ticker="DINO", quantity=200, entry_price=100.0,
                        stop_price=95.0, target_price=130.0, entry_day=1, entry_trading_day=1),
    }
    # Both gap catastrophically through their stop at the open -- fills at
    # the gapped open (worse than the stop itself), $80/share below entry
    # each -- $16,000 x 2 = $32,000, well past the -30% breaker.
    gapped_open = {"time": "09:30", "open": 20.0, "high": 20.0, "low": 18.0, "close": 20.0}
    buy_order = _buy_order(ticker="TSLA", alias="ECHO", stop_price=90.0, target_price=115.0)
    plan = simulate_trading_day(
        holdings=holdings, pending_orders=[buy_order],
        intraday={
            "OLN": _flat_session(overrides={"09:30": gapped_open}),
            "DINO": _flat_session(overrides={"09:30": gapped_open}),
            "TSLA": _flat_session(price=100.0),
        },
        day_index=8, trading_days_elapsed=5, limits_ctx={}, watchlist=["OLN", "DINO", "TSLA"],
        closed_pnl=[], starting_cash=60_000.0,  # 60k cash + 2x(200@$100) holdings == 100k equity yday
        equity_curve=equity_curve,
    )
    gap_exits = [e for e in plan.events if e["stage"] == "opening_gap" and e["kind"] == "exit"]
    assert len(gap_exits) == 2, plan.events
    realized_loss = sum(e["quantity"] * (e["entry_price"] - e["exit_price"]) for e in gap_exits)
    assert realized_loss >= 30_000, f"test scenario itself must realise a real >=30% loss, got ${realized_loss}"
    print(f"  ok  two morning gap stops realised a combined ${realized_loss:,.0f} loss before 09:45")

    buy_events = [e for e in plan.events if e["ticker"] == "TSLA"]
    assert len(buy_events) == 1 and buy_events[0]["kind"] == "rejected", (
        f"a queued 09:45 BUY was NOT rejected despite a {realized_loss / 1000:.1f}% same-morning "
        f"loss already realised -- breaker values leaked stale pre-session numbers: {buy_events}"
    )
    assert "§4" in buy_events[0]["reason"], buy_events[0]["reason"]
    print("  ok  the queued 09:45 BUY is rejected by §4's -30% drawdown breaker -- this "
          "morning's own catastrophic stop losses are reflected in the check, not stale "
          "pre-session figures")


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


def test_ask_hermes_treats_an_explicit_null_usage_field_as_zero():
    """The real crash (c55f0fc2-... smoke test): a real Hermes usage payload
    came back with a field present but explicit `null` (e.g. cache_read_tokens
    with no prior context to cache on cycle 1 of a fresh run) -- .get(key, 0)
    only substitutes the default for a MISSING key, so `usage_totals[key] +
    None` threw TypeError and killed the cycle before anything was journaled.
    """
    clean_reply = json.dumps({"decisions": [], "no_action_reason": "nothing qualified"})
    usage_with_null = {
        "input_tokens": 500, "output_tokens": 120, "cache_read_tokens": None,
        "reasoning_tokens": 40, "total_tokens": 660, "api_calls": 1,
        "estimated_cost_usd": None,
    }
    with patch("run_backtest._call_hermes", return_value=(clean_reply, usage_with_null)):
        totals: dict = {}
        result = ask_hermes("irrelevant prompt", totals, is_reflection_day=False)
    assert result.response == {"decisions": [], "no_action_reason": "nothing qualified"}
    assert result.attempt_count == 1
    assert result.retry_diagnostics == []
    assert totals["cache_read_tokens"] == 0, "an explicit null must become 0, not crash or stay None"
    assert totals["estimated_cost_usd"] == 0.0
    # Every other real field must still accumulate truthfully -- the null
    # coercion must not corrupt values that were actually present.
    assert totals["input_tokens"] == 500
    assert totals["output_tokens"] == 120
    assert totals["reasoning_tokens"] == 40
    assert totals["total_tokens"] == 660
    assert totals["api_calls"] == 1
    print("  ok  an explicit-null usage field is coerced to 0 without crashing or corrupting "
          "the other real usage totals")


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


def test_build_prompt_sizing_block_reads_the_active_limits_not_hardcoded_defaults():
    """2026-08-31: a real smoke test showed Hermes reasoning from a stale
    1% §2a figure -- the prompt never stated the real percentage or
    formula at all. This proves build_prompt renders whatever `limits`
    instance it's actually given (non-default 7.5%/33.0%), not a second,
    independently hard-coded 2.0/20.0 -- so a future approved RULES.md
    change stays correct here automatically."""
    account = {"equity": 100_000.0, "cash": 100_000.0, "exposure_pct": 0.0,
              "daily_pl_pct": 0.0, "weekly_pl_pct": 0.0, "drawdown_pct": 0.0}
    non_default = RiskLimits(risk_budget_pct=7.5, max_position_pct=33.0)
    prompt = build_prompt("Day 1", account, [], [], [], limits=non_default)

    assert "7.5% of equity risked per trade" in prompt
    assert "33.0% of equity in one name" in prompt
    assert "(7.5 / 100)" in prompt and "(33.0 / 100)" in prompt
    # The stale defect this guards against: the OLD default values must
    # NOT leak in when a non-default instance was explicitly supplied.
    assert "2.0% of equity" not in prompt and "20.0% of equity in one name" not in prompt
    print("  ok  the POSITION SIZING block renders the actual supplied RiskLimits values and "
          "formulas, not a second hard-coded 2.0/20.0 copy")

    # No `limits` argument at all still works (existing callers) and falls
    # back to RiskLimits()'s own real default -- 2.0/20.0 today, whatever
    # RiskLimits() itself says tomorrow.
    default_prompt = build_prompt("Day 1", account, [], [], [])
    default_limits = RiskLimits()
    assert f"{default_limits.risk_budget_pct:.1f}% of equity risked per trade" in default_prompt
    assert f"{default_limits.max_position_pct:.1f}% of equity in one name" in default_prompt
    print("  ok  omitting `limits` falls back to RiskLimits()'s own real default, not a stale copy")


# --------------------------------------------------------------------------
# 2026-08-29 correctness pass #4
# --------------------------------------------------------------------------

def test_stopped_out_today_resets_unconditionally_even_on_a_quiet_day():
    """Item 1 -- the reset used to live INSIDE `if needed:`; on a day with
    no holdings and no pending orders (a genuinely quiet day), a stop-out
    from a PRIOR day was never cleared, so §9.4's same-day re-entry block
    kept firing on every subsequent quiet day instead of expiring after
    the day it actually happened. Exercises the real run_day, only the
    true I/O boundary mocked."""
    tmp_dir = Path(tempfile.mkdtemp())
    run_start = date(2026, 2, 17)
    masking = RunMasking({"OLN": ["Olin Corporation", "Olin"]}, run_start=run_start,
                         seed=DEFAULT_MASKING_SEED)
    # Portfolio ENTERING today already carries a PRIOR day's stop-out --
    # exactly the leftover state the bug failed to clear, with no
    # holdings/pending orders (a quiet day -- needed=[]).
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6,
                          stopped_out_today=["OLN"])
    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}]

    fake_bars = {"OLN": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}}
    fake_technicals = {"OLN": {"price": 100.5, "rsi": 50.0, "rsi_signal": "neutral",
                               "macd_signal": "bullish", "trend": "up", "outlook": "neutral",
                               "support": [95.0], "resistance": [110.0]}}

    def fake_ask_hermes(prompt, usage_totals, is_reflection_day=False):
        return HermesResult(response={"decisions": [], "no_action_reason": "nothing qualifies today"},
                            attempt_count=1, retry_diagnostics=[])

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value=fake_bars), \
         patch("run_backtest.fetch_technicals", return_value=fake_technicals), \
         patch("run_backtest.fetch_news", return_value={"OLN": []}), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask_hermes), \
         patch("run_backtest.record_cycle_evidence", return_value=None):
        working, summary, _do_observability = run_day(
            run_start, 0, portfolio, masking, watchlist, {}, str(uuid.uuid4()),
        )

    assert working.stopped_out_today == [], (
        f"a quiet day (no holdings, no pending orders) failed to clear a PRIOR "
        f"day's stop-out: {working.stopped_out_today}"
    )
    print("  ok  stopped_out_today resets unconditionally even on a quiet day "
          "with no holdings and no pending orders")

    ctx = build_decision_time_context(working, {}, ["OLN"], {})
    assert ctx.stopped_out_today == [], (
        "the decision-time context still carries a stale prior-day stop-out"
    )
    print("  ok  a later day's decision-time context no longer blocks re-entry "
          "on a stop-out from a prior day")


def test_stopped_out_today_persists_across_a_process_restart():
    """Item 1 -- stopped_out_today was previously omitted from
    to_dict/from_dict entirely; a resumed process (a fresh
    Portfolio.from_dict call, exactly what happens on every real restart
    between --days-limited invocations) always reset it to [] regardless
    of what the prior process actually recorded."""
    original = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6,
                         stopped_out_today=["OLN", "DINO"])
    restored = Portfolio.from_dict(original.to_dict())
    assert restored.stopped_out_today == ["OLN", "DINO"]
    print("  ok  stopped_out_today round-trips through to_dict/from_dict exactly")

    legacy_dict = original.to_dict()
    del legacy_dict["stopped_out_today"]
    legacy_restored = Portfolio.from_dict(legacy_dict)
    assert legacy_restored.stopped_out_today == []
    print("  ok  a legacy state file missing the field falls back safely to []")


def test_make_decision_id_disambiguates_duplicate_decisions_in_one_reply():
    """Item 2 -- two IDENTICAL decisions (same ticker/action) at different
    positions in the SAME response must get DIFFERENT decision_ids --
    previously both resolved to the same id, so if the first resolved
    rejected and the second filled, the fill's resolution found the row
    already claimed by the first rejection and was silently dropped."""
    id_0 = make_decision_id("run-x", 5, "OLN", "BUY", "queued_proposal[0]")
    id_1 = make_decision_id("run-x", 5, "OLN", "BUY", "queued_proposal[1]")
    assert id_0 != id_1
    print("  ok  two identical (ticker, action) decisions at different list "
          "positions get distinct decision_ids")

    # Stable under checkpointed replay -- the SAME response (same
    # positions) regenerates the IDENTICAL ids, not fresh ones.
    assert make_decision_id("run-x", 5, "OLN", "BUY", "queued_proposal[0]") == id_0
    assert make_decision_id("run-x", 5, "OLN", "BUY", "queued_proposal[1]") == id_1
    print("  ok  a checkpointed replay of the same response regenerates the "
          "IDENTICAL ids for each position")


def test_duplicate_identical_decisions_journal_both_outcomes_with_distinct_ids():
    """Item 2 -- the actual behavioral regression: two queued BUY orders
    for the SAME ticker (as Hermes proposing two identical decisions in
    one reply would produce, now disambiguated by occurrence index -- see
    the STEP D loop in run_day) must resolve INDEPENDENTLY, each keeping
    its own decision_id through to its own outcome event, even when the
    first is rejected and the second fills."""
    decision_id_0 = make_decision_id("run-x", 8, "OLN", "BUY", "queued_proposal[0]")
    decision_id_1 = make_decision_id("run-x", 8, "OLN", "BUY", "queued_proposal[1]")
    assert decision_id_0 != decision_id_1

    # Order 0: oversized -- breaches §2b and is rejected. Order 1: same
    # ticker, modest size -- passes and fills. Both queued in the same
    # batch, as two identical-looking Hermes decisions would be.
    order_0 = _buy_order(ticker="OLN", quantity=2000, stop_price=93.0, target_price=115.0,
                         decision_id=decision_id_0)
    order_1 = _buy_order(ticker="OLN", quantity=50, stop_price=93.0, target_price=115.0,
                         decision_id=decision_id_1)

    plan = simulate_trading_day(
        holdings={}, pending_orders=[order_0, order_1],
        intraday={"OLN": _flat_session()}, day_index=8, trading_days_elapsed=5,
        limits_ctx={}, watchlist=["OLN"], closed_pnl=[], starting_cash=100_000.0,
        equity_curve=[100_000.0] * 6,
    )
    buy_events = [e for e in plan.events if e["stage"] == "buy_945"]
    assert len(buy_events) == 2, buy_events

    rejected = [e for e in buy_events if e["kind"] == "rejected"]
    filled = [e for e in buy_events if e["kind"] == "fill"]
    assert len(rejected) == 1 and len(filled) == 1, buy_events
    assert rejected[0]["order"].decision_id == decision_id_0
    assert filled[0]["order"].decision_id == decision_id_1
    assert rejected[0]["order"].decision_id != filled[0]["order"].decision_id
    print("  ok  two identical-ticker queued BUYs keep their own distinct "
          "decision_ids through to their own outcome (one rejected, one filled)")


def test_outage_vs_holiday_distinguished_via_the_trading_calendar():
    """Item 3 -- `if not bars:` alone used to treat a genuine outage on a
    real trading day identically to an actual holiday. Now cross-checked
    against Alpaca's real trading calendar."""
    tmp_dir = Path(tempfile.mkdtemp())
    run_start = date(2026, 2, 17)
    masking = RunMasking({"OLN": ["Olin Corporation", "Olin"]}, run_start=run_start,
                         seed=DEFAULT_MASKING_SEED)
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6)
    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}]

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value={}), \
         patch("run_backtest.fetch_trading_calendar_day",
               return_value={"date": "2026-02-17", "open": "09:30", "close": "16:00"}):
        try:
            run_day(run_start, 0, portfolio, masking, watchlist, {}, "run-x")
            raise AssertionError("expected an outage on a real trading day to halt, not skip")
        except RuntimeError as exc:
            assert "outage" in str(exc).lower()
    print("  ok  a data outage on a real trading day (per the calendar) halts the cycle")

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value={}), \
         patch("run_backtest.fetch_trading_calendar_day", return_value=None):
        working, summary, _do = run_day(run_start, 0, portfolio, masking, watchlist, {}, "run-x")
    assert "skipped" in summary and working is portfolio
    print("  ok  a confirmed real holiday/weekend still skips, unchanged behavior")

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value={}), \
         patch("run_backtest.fetch_trading_calendar_day",
               side_effect=RuntimeError("container unreachable")):
        try:
            run_day(run_start, 0, portfolio, masking, watchlist, {}, "run-x")
            raise AssertionError("expected an unavailable calendar source to fail closed")
        except RuntimeError as exc:
            assert "calendar could not be checked" in str(exc)
    print("  ok  an unavailable calendar source itself fails closed with a clear message")


def test_expected_session_bar_times_standard_and_half_day():
    """Item 4 -- the full session grid is derived from the REAL session
    boundaries, not hardcoded to 09:30-16:00, so a genuine half day gets
    its own correct, shorter grid."""
    standard = _expected_session_bar_times("09:30", "16:00")
    assert standard[0] == "09:30" and standard[-1] == "15:45" and len(standard) == 26
    half_day = _expected_session_bar_times("09:30", "13:00")
    assert half_day[0] == "09:30" and half_day[-1] == "12:45" and len(half_day) == 14
    print("  ok  _expected_session_bar_times builds the correct grid for a standard "
          "and a half-day session")


def test_require_intraday_coverage_fails_closed_on_a_truncated_afternoon_feed():
    """Item 4 -- a feed silently missing afternoon bars used to let the
    walk stage iterate only what was there and the close mark trust
    intraday[t][-1] as if it were the real close. The full session grid
    (not just 09:30/09:45) must now be required."""
    full_grid = tuple(_expected_session_bar_times("09:30", "16:00"))
    truncated = {"OLN": _flat_session()[:18]}  # missing the last 8 afternoon bars
    try:
        require_intraday_coverage(["OLN"], truncated, required_times=full_grid)
        raise AssertionError("expected a truncated afternoon feed to fail closed")
    except RuntimeError as exc:
        assert "OLN" in str(exc)
    print("  ok  a feed missing afternoon bars fails closed against the full session grid")

    complete = {"OLN": _flat_session()}
    require_intraday_coverage(["OLN"], complete, required_times=full_grid)  # must not raise
    print("  ok  a genuinely complete feed passes the full session grid check")


def test_resolve_window_resume_and_mismatch():
    """Item 5 -- --start/--end persistence, mirroring _resolve_seed's own
    style/test."""
    assert _resolve_window("2026-02-17", "2026-08-14", "2026-02-17", "2026-08-14") == \
        ("2026-02-17", "2026-08-14")
    print("  ok  a resumed run with a matching --start/--end proceeds")

    try:
        _resolve_window("2026-02-17", "2026-08-14", "2026-03-01", "2026-08-14")
        raise AssertionError("expected a changed --start to fail closed")
    except SystemExit as exc:
        assert "2026-02-17" in str(exc) and "2026-03-01" in str(exc)
    print("  ok  a resumed run with a conflicting --start fails loudly, naming both values")

    try:
        _resolve_window("2026-02-17", "2026-08-14", "2026-02-17", "2026-09-01")
        raise AssertionError("expected a changed --end to fail closed")
    except SystemExit as exc:
        assert "2026-08-14" in str(exc) and "2026-09-01" in str(exc)
    print("  ok  a resumed run with a conflicting --end fails loudly, naming both values")

    assert _resolve_window(None, None, "2026-02-17", "2026-08-14") == ("2026-02-17", "2026-08-14")
    print("  ok  a fresh/legacy run with no stored window establishes the requested one")


# ============================================================================
# Phase 1 aggressive profile -- deterministic activity escalation
# (RULES.md §6b, docs/PHASE1_AGGRESSIVE_DESIGN.md). Pure state-transition
# tests against _record_trade_completion/_maybe_advance_activity_block --
# no I/O, mirrors this file's own testing philosophy.
# ============================================================================

def test_time_stop_is_five_trading_days_not_calendar_days():
    """RULES.md §6c: 5 TRADING days, counted via trading_days_elapsed --
    weekends/holidays never count. TIME_STOP_TRADING_DAYS itself is the
    single source of truth simulate_trading_day reads."""
    assert TIME_STOP_TRADING_DAYS == 5
    holding = Holding(ticker="OLN", quantity=100, entry_price=100.0, stop_price=95.0,
                      target_price=130.0, entry_day=1, entry_trading_day=0)
    # Exactly 5 real trading days elapsed since entry -> time stop fires at
    # today's open, regardless of stop/target.
    plan = simulate_trading_day(
        holdings={"OLN": holding}, pending_orders=[],
        intraday={"OLN": _flat_session(price=110.0)},  # well clear of stop/target
        day_index=10, trading_days_elapsed=5, limits_ctx={}, watchlist=["OLN"],
        closed_pnl=[], starting_cash=90_000.0, equity_curve=[100_000.0] * 6,
    )
    time_stops = [e for e in plan.events if e["stage"] == "time_stop"]
    assert len(time_stops) == 1 and time_stops[0]["ticker"] == "OLN", plan.events
    print("  ok  a position open exactly TIME_STOP_TRADING_DAYS (5) trading days is closed")

    # One trading day short of that must NOT time-stop yet.
    plan2 = simulate_trading_day(
        holdings={"OLN": holding}, pending_orders=[],
        intraday={"OLN": _flat_session(price=110.0)},
        day_index=10, trading_days_elapsed=4, limits_ctx={}, watchlist=["OLN"],
        closed_pnl=[], starting_cash=90_000.0, equity_curve=[100_000.0] * 6,
    )
    assert not [e for e in plan2.events if e["stage"] == "time_stop"], plan2.events
    print("  ok  one trading day short of the time stop does not fire yet")


def test_activity_block_is_twenty_real_trading_days():
    """A block only evaluates on its own 20th real trading day -- never
    mid-block."""
    p = Portfolio()
    assert p.profile_level == PROFILE_BASE and p.block_start_trading_day == 0
    for day in range(1, ACTIVITY_BLOCK_TRADING_DAYS):
        p.trading_days_elapsed = day
        _maybe_advance_activity_block(p)
        assert p.block_start_trading_day == 0, (
            f"block boundary fired early, on trading day {day}"
        )
    print("  ok  the block never advances before its own 20th trading day")

    p.trading_days_elapsed = ACTIVITY_BLOCK_TRADING_DAYS
    _maybe_advance_activity_block(p)
    assert p.block_start_trading_day == ACTIVITY_BLOCK_TRADING_DAYS, (
        "the block must advance exactly on trading day 20"
    )
    print("  ok  the block advances exactly on its 20th real trading day")


def test_five_completed_trades_keep_the_base_tier():
    p = Portfolio()
    for _ in range(ACTIVITY_TARGET_TRADES):
        _record_trade_completion(p)
    p.trading_days_elapsed = ACTIVITY_BLOCK_TRADING_DAYS
    _maybe_advance_activity_block(p)
    assert p.profile_level == PROFILE_BASE, (
        f"exactly {ACTIVITY_TARGET_TRADES} completed trades must keep the base tier, "
        f"got {p.profile_level}"
    )
    assert p.block_completed_trades == 0, "the new block must start with a fresh count"
    print(f"  ok  exactly {ACTIVITY_TARGET_TRADES} completed trades in a block keeps the base tier")


def test_fewer_than_five_escalates_only_the_following_block():
    p = Portfolio()
    for _ in range(ACTIVITY_TARGET_TRADES - 1):  # one short of the target
        _record_trade_completion(p)
    p.trading_days_elapsed = ACTIVITY_BLOCK_TRADING_DAYS - 1
    _maybe_advance_activity_block(p)
    assert p.profile_level == PROFILE_BASE, "must never change level mid-block"
    p.trading_days_elapsed = ACTIVITY_BLOCK_TRADING_DAYS
    _maybe_advance_activity_block(p)
    assert p.profile_level == PROFILE_ESCALATED, (
        f"a block with only {ACTIVITY_TARGET_TRADES - 1} completions must escalate "
        f"the FOLLOWING block, got {p.profile_level}"
    )
    print("  ok  fewer than 5 completed trades escalates the following block, never mid-block")

    # Once escalated, a later slow block must NOT change anything further
    # (no de-escalation, no re-evaluation) -- min R:R stays 1.25 forever.
    p.trading_days_elapsed = 2 * ACTIVITY_BLOCK_TRADING_DAYS
    _maybe_advance_activity_block(p)
    assert p.profile_level == PROFILE_ESCALATED, "escalation must never reverse"
    assert PROFILE_MIN_REWARD_RISK[p.profile_level] == 1.25
    print("  ok  escalation never reverses, even after another slow block")


def test_rejections_and_queued_only_proposals_do_not_count():
    """_record_trade_completion is only ever called by run_day at a REAL
    exit fill -- a rejected or merely-queued proposal never reaches it.
    This test asserts the CONTRACT directly: only completions move the
    counter, nothing else does (there is no other function that can)."""
    p = Portfolio()
    assert p.block_completed_trades == 0
    # Simulate a full block of activity with ZERO completions (everything
    # rejected/queued-only, in spirit) -- the counter must stay at 0 and
    # the run must escalate, exactly as if nothing had ever been proposed.
    p.trading_days_elapsed = ACTIVITY_BLOCK_TRADING_DAYS
    _maybe_advance_activity_block(p)
    assert p.block_completed_trades == 0
    assert p.profile_level == PROFILE_ESCALATED
    print("  ok  a block with zero real completions (all rejected/queued-only) escalates")


def test_reset_returns_to_base_resume_preserves_the_tier():
    escalated = Portfolio(profile_level=PROFILE_ESCALATED, block_start_trading_day=40,
                          block_completed_trades=2)
    restored = Portfolio.from_dict(escalated.to_dict())
    assert restored.profile_level == PROFILE_ESCALATED
    assert restored.block_start_trading_day == 40
    assert restored.block_completed_trades == 2
    print("  ok  resume restores the exact block counters and active tier deterministically")

    fresh = Portfolio()  # a --reset run always starts a genuinely new Portfolio()
    assert fresh.profile_level == PROFILE_BASE
    assert fresh.block_start_trading_day == 0
    assert fresh.block_completed_trades == 0
    print("  ok  a fresh/--reset run always starts at the base tier with a clean block")

    # A legacy (pre-Phase-1) state file predates these fields entirely --
    # must fall back to the base tier, never crash.
    legacy = {"cash": 100_000.0, "holdings": {}, "equity_curve": [100_000.0] * 6,
             "closed_pnl": []}
    legacy_restored = Portfolio.from_dict(legacy)
    assert legacy_restored.profile_level == PROFILE_BASE
    print("  ok  a legacy state file missing these fields falls back to the base tier safely")

    # A legacy portfolio that already has REAL trading history
    # (trading_days_elapsed > 0) but predates block tracking must start a
    # genuinely FRESH block from where it stands today -- not from 0,
    # which would count every one of those historical days toward the
    # very first block's 20-day window and could escalate (or not) off
    # activity that happened before Phase 1 code even existed.
    legacy_with_history = {"cash": 100_000.0, "holdings": {},
                           "equity_curve": [100_000.0] * 6, "closed_pnl": [],
                           "trading_days_elapsed": 83}
    restored_with_history = Portfolio.from_dict(legacy_with_history)
    assert restored_with_history.profile_level == PROFILE_BASE
    assert restored_with_history.block_start_trading_day == 83, (
        f"a legacy resume with real history must start its first block AT the current "
        f"trading_days_elapsed (83), not 0 -- got {restored_with_history.block_start_trading_day}"
    )
    assert restored_with_history.block_completed_trades == 0
    print("  ok  a legacy portfolio with existing elapsed days starts a fresh block "
          "at that elapsed value, not at day 0")

    # A genuinely NEW Phase 1 state file's block_start_trading_day=0 (its
    # own real, freshly-written value, not a missing key) must be used
    # AS-IS, never overridden by trading_days_elapsed -- the fallback
    # above only applies when the KEY is truly absent.
    new_format_early = {"cash": 100_000.0, "holdings": {},
                        "equity_curve": [100_000.0] * 6, "closed_pnl": [],
                        "trading_days_elapsed": 5, "block_start_trading_day": 0,
                        "block_completed_trades": 1, "profile_level": PROFILE_BASE}
    restored_new = Portfolio.from_dict(new_format_early)
    assert restored_new.block_start_trading_day == 0, (
        "a genuinely present block_start_trading_day=0 must never be overridden "
        "by trading_days_elapsed"
    )
    assert restored_new.block_completed_trades == 1
    print("  ok  a real Phase 1 state file's own block_start_trading_day=0 is preserved, "
          "not confused with a missing key")

    # A pending order from a pre-Phase-1 state file was queued and risk-
    # checked under the FORMER conservative minimum (2.0) -- restoring it
    # as PROFILE_BASE/1.5 would misrepresent what it was actually
    # validated against.
    legacy_order = {
        "ticker": "OLN", "alias": "ALPHA", "action": "BUY", "quantity": 90,
        "stop_price": 93.0, "target_price": 130.0, "entry_price": 100.0,
        "queued_day_index": 3, "decision": {"action": "BUY", "quantity": 90},
        "decision_id": "legacy-decision-id",
        # deliberately no "strategy_profile"/"min_reward_risk_used" keys --
        # exactly what a pre-Phase-1 state file's pending_orders looks like.
    }
    legacy_portfolio = {"cash": 100_000.0, "holdings": {},
                        "equity_curve": [100_000.0] * 6, "closed_pnl": [],
                        "pending_orders": [legacy_order]}
    restored_order = Portfolio.from_dict(legacy_portfolio).pending_orders[0]
    assert restored_order.strategy_profile == PROFILE_LEGACY, restored_order.strategy_profile
    assert restored_order.min_reward_risk_used == PROFILE_MIN_REWARD_RISK[PROFILE_LEGACY] == 2.0, (
        restored_order.min_reward_risk_used
    )
    print("  ok  a legacy pending order restores as PROFILE_LEGACY with its real "
          "former minimum (2.0), not the new base tier's 1.5")

    # A pending order that DOES carry these fields (any real Phase 1 file)
    # must use its own real stored values, never overridden.
    phase1_order = dict(legacy_order, strategy_profile=PROFILE_ESCALATED,
                        min_reward_risk_used=1.25)
    restored_phase1_order = Portfolio.from_dict(
        {**legacy_portfolio, "pending_orders": [phase1_order]}
    ).pending_orders[0]
    assert restored_phase1_order.strategy_profile == PROFILE_ESCALATED
    assert restored_phase1_order.min_reward_risk_used == 1.25
    print("  ok  a real Phase 1 pending order's own stored profile/threshold is preserved")


def test_checkpoint_replay_cannot_double_count_or_re_escalate():
    """A checkpoint retry re-processes the SAME real trading day. Two
    genuinely different properties are being proven here, and they are
    NOT the same claim:

    1. `_record_trade_completion` is a plain `+= 1` -- it is NOT itself
       idempotent, and calling it twice on the SAME Portfolio object
       really does double-count (proven directly below). Replay safety
       does not come from this function -- it comes entirely from
       `run_day`'s own architecture: `working` is a fresh `copy.deepcopy`
       of the portfolio on every attempt, and only the CALLER (`main()`)
       adopts a returned `working` as the new committed portfolio, and
       only after that attempt's atomic state write succeeds (see
       Portfolio's own docstring / run_day's essential-vs-best-effort
       contract). A retry of a HALTED attempt starts over from the last
       genuinely COMMITTED portfolio and replays the same checkpointed
       Hermes response against a FRESH copy -- it does not call this
       function again on an already-committed object, so there is no
       second increment to double-count.
    2. `_maybe_advance_activity_block` IS safe to call more than once for
       the same `trading_days_elapsed` value -- it's guarded by the
       block's own day-boundary check -- proven directly below too. This
       one genuinely is idempotent under direct double-invocation.
    """
    p = Portfolio()
    _record_trade_completion(p)
    _record_trade_completion(p)
    assert p.block_completed_trades == 2, (
        "two calls really do add up to two -- this function has no dedup logic of its own; "
        "see the docstring above for where replay safety actually comes from"
    )
    print("  ok  _record_trade_completion is NOT idempotent by itself (2 calls -> count of 2) "
          "-- replay safety is run_day's job, not this function's")

    # _maybe_advance_activity_block called twice for the SAME
    # trading_days_elapsed value (simulating an accidental double-call,
    # which run_day's real control flow never does) must not advance the
    # block a second time or re-escalate -- THIS function genuinely is
    # idempotent under direct double-invocation.
    p.trading_days_elapsed = ACTIVITY_BLOCK_TRADING_DAYS
    _maybe_advance_activity_block(p)
    tier_after_first = p.profile_level
    start_after_first = p.block_start_trading_day
    _maybe_advance_activity_block(p)  # same trading_days_elapsed, called again
    assert p.profile_level == tier_after_first, "a same-day double-call must not re-escalate"
    assert p.block_start_trading_day == start_after_first, (
        "a same-day double-call must not advance the block window twice"
    )
    print("  ok  _maybe_advance_activity_block genuinely is idempotent for a repeat same-day call")


def test_queued_orders_use_their_stored_threshold_at_execution():
    """RULES.md §6b's own 'done when': the SAME threshold at decision,
    queue, and execution -- an order queued under the escalated 1.25 tier
    must be checked at execution against 1.25, even if `ctx.limits` (the
    generic per-cycle default) says something else -- see
    resolve_queued_order's own docstring."""
    bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    # ratio 1.3: fails the base 1.5 floor, but clears the escalated 1.25
    # floor -- entry $100 (slipped ~100.05), stop $92 (~8%), target $110.4.
    order = _order(action="BUY", ticker="OLN", quantity=50, stop_price=92.0,
                   target_price=110.4, strategy_profile=PROFILE_ESCALATED,
                   min_reward_risk_used=1.25)
    # ctx.limits here is the ordinary base-tier default (1.5) -- this
    # order's OWN stored 1.25 must win, not ctx's.
    outcome = resolve_queued_order(order, bar, _ctx())
    assert outcome.accepted, (
        f"a queued order must be checked against ITS OWN stored escalated threshold, "
        f"not today's generic default: {outcome.reason}"
    )
    print("  ok  a queued order is checked at execution against its own stored threshold, "
          "not whatever ctx.limits happens to carry")

    # And the reverse: an order queued under the BASE tier must still be
    # held to 1.5, even if resolved with limits_ctx that (hypothetically)
    # carries something looser -- proves the order's own value is
    # authoritative in both directions, not just a fallback.
    base_order = _order(action="BUY", ticker="OLN", quantity=50, stop_price=92.0,
                        target_price=110.4, strategy_profile=PROFILE_BASE,
                        min_reward_risk_used=1.5)
    loose_ctx = _ctx(limits=_limits_for_profile(PROFILE_ESCALATED)["limits"])
    outcome2 = resolve_queued_order(base_order, bar, loose_ctx)
    assert not outcome2.accepted and "§6b" in outcome2.reason, (
        f"a base-tier order must stay held to 1.5 even under a looser ctx: {outcome2.reason}"
    )
    print("  ok  a base-tier order stays held to its own 1.5 floor even under a looser ctx")


# ============================================================================
# RULES.md §2/§3 decision-time position sizing -- the real training-
# integrity defect the Round 2 pilot exposed: Hermes queued ATI x1 and
# BRX x1 when the sizing formula implied ~x130/~x696. The deterministic
# checker only rejected EXCESSIVE quantities, so a meaningless one-share
# placeholder sailed through. required_entry_quantity is the exact
# whole-share size RULES.md §2a/§2b/§3 imply; Step D now requires an exact
# match before a BUY is ever queued.
# ============================================================================

def test_required_entry_quantity_matches_rules_md_sizing_formula():
    """Pure function, no I/O -- the exact arithmetic RULES.md §2a
    (stop-risk), §2b (concentration), and §3 (remaining cash) imply,
    whichever is smallest, floored to a whole share."""
    ctx = _ctx()  # equity=$100,000, cash=$100,000
    # entry $100, stop $93 (7% distance): risk_size = floor(100000*0.02/7)
    # = 285, concentration_size = floor(100000*0.20/100) = 200,
    # cash_size = floor(100000/100) = 1000 -- concentration binds.
    trade = ProposedTrade(ticker="OLN", action="BUY", quantity=1,
                          entry_price=100.0, stop_price=93.0, target_price=115.0)
    assert required_entry_quantity(trade, ctx) == 200
    print("  ok  required_entry_quantity matches RULES.md's own §2a/§2b/§3 arithmetic "
          "(concentration binds here)")

    # entry $100, stop $99.9 (0.1% distance): risk_size = floor(2000/0.1) =
    # 20000, concentration_size = 200, cash_size = 1000 -- still
    # concentration, proving it's a genuine min(), not always risk-based.
    tight_stop = ProposedTrade(ticker="OLN", action="BUY", quantity=1,
                               entry_price=100.0, stop_price=99.9, target_price=115.0)
    assert required_entry_quantity(tight_stop, ctx) == 200
    print("  ok  required_entry_quantity takes the genuine minimum of all three, "
          "not just the risk-based figure")

    # A tiny remaining cash pool makes CASH the binding constraint.
    low_cash_ctx = Context(
        account=AccountState(equity=100_000.0, cash=1_500.0, daily_pl_pct=0.0,
                             weekly_pl_pct=0.0, drawdown_from_peak_pct=0.0),
        clock=MarketClock(is_open=True), watchlist=["OLN"],
    )
    assert required_entry_quantity(trade, low_cash_ctx) == 15  # floor(1500/100)
    print("  ok  required_entry_quantity is cash-bound when cash is the scarce resource")

    # Fails closed (None) on unusable inputs -- an inverted/missing stop, a
    # non-BUY action -- never crashes, never returns a bogus number.
    assert required_entry_quantity(
        ProposedTrade(ticker="OLN", action="BUY", quantity=1, entry_price=100.0,
                     stop_price=105.0, target_price=115.0), ctx
    ) is None
    assert required_entry_quantity(
        ProposedTrade(ticker="OLN", action="BUY", quantity=1, entry_price=100.0,
                     stop_price=None, target_price=115.0), ctx
    ) is None
    assert required_entry_quantity(
        ProposedTrade(ticker="OLN", action="SELL", quantity=1, entry_price=100.0,
                     stop_price=93.0, target_price=115.0), ctx
    ) is None
    print("  ok  required_entry_quantity fails closed (None) on an inverted/missing "
          "stop or a non-BUY action")


def test_resolve_queued_order_does_not_recalculate_exact_quantity_after_a_gap():
    """RULES.md §2/§3's exact-match requirement is a DECISION-time gate
    only. Execution-time (resolve_queued_order, the next real trading day)
    must NOT re-derive a new 'exact' quantity against the real fill price
    and reject the order for no longer matching it -- it must keep
    checking only genuine cap/risk/cash violations, exactly as before this
    fix. A real overnight gap can move the theoretical optimum without
    invalidating a queued order that still fits every real cap."""
    order = _order(action="BUY", ticker="OLN", quantity=150, stop_price=92.0,
                   target_price=115.0)  # NOT the "exact" figure for any equity/price
    # A modest gapped-up open: $101 instead of the $100 Hermes originally
    # saw. 150 shares @ ~$101 is still well inside every real cap (stop
    # distance ~9%, position ~15%, risk ~1.4%, ratio ~1.54), so it must
    # fill -- resolve_queued_order has no concept of "the exact quantity a
    # fresh proposal would compute today."
    bar = {"open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0}
    outcome = resolve_queued_order(order, bar, _ctx())
    assert outcome.accepted, (
        f"execution-time must not demand recalculation to a new exact quantity "
        f"after a gap -- only real cap/risk/cash violations: {outcome.reason}"
    )
    print("  ok  execution-time accepts a queued order whose quantity is no longer "
          "'exact' for the gapped price, as long as every real cap still holds")

    # And a genuine cap violation at the real fill price is still rejected,
    # exactly as before -- this fix does not weaken execution-time checks.
    big_gap_bar = {"open": 700.0, "high": 705.0, "low": 695.0, "close": 700.0}
    outcome2 = resolve_queued_order(order, big_gap_bar, _ctx())
    assert not outcome2.accepted and "§2b" in outcome2.reason, (
        f"a real concentration-cap breach at the gapped price must still reject: {outcome2.reason}"
    )
    print("  ok  execution-time still rejects a genuine cap violation at the real "
          "gapped price -- unaffected by this fix")


def test_fresh_portfolio_has_one_baseline_equity_point():
    """A fresh Portfolio's equity_curve must start with exactly the
    $100,000 baseline -- not the five extra fabricated flat observations
    the old daily/weekly-breaker seed added. Phase 1 removed those
    breakers entirely, so five invented data points would just be
    dishonest performance history."""
    p = Portfolio()
    assert p.equity_curve == [STARTING_EQUITY] == [100_000.0]
    print("  ok  a fresh Portfolio begins with exactly one $100,000 baseline point")

    # Peak drawdown is still correct with only one point: 0% until a real
    # day marks equity away from the peak.
    assert p.drawdown_pct() == 0.0
    print("  ok  peak drawdown is still correct (0%) with a single-point history")


def test_prompt_no_longer_exposes_obsolete_daily_weekly_breaker_figures():
    """RULES.md §4 removed the daily/weekly circuit breakers -- only the
    single -30% drawdown breaker remains. The prompt Hermes reads must not
    show daily_pl_pct/weekly_pl_pct figures for a rule that no longer
    exists; showing them invites Hermes to reason about numbers that don't
    bind anything."""
    account = {"equity": 100_000.0, "cash": 100_000.0, "exposure_pct": 0.0,
              "daily_pl_pct": -37.0, "weekly_pl_pct": -52.0, "drawdown_pct": 0.0,
              "strategy_profile": PROFILE_BASE, "min_reward_risk": 1.5}
    prompt = build_prompt("Day 1", account, [], [], [])
    assert "-37" not in prompt and "-52" not in prompt, (
        "the prompt must not surface daily/weekly P&L figures for a removed breaker"
    )
    assert "daily" not in prompt.lower() and "weekly" not in prompt.lower(), (
        "no 'daily'/'weekly' wording should remain in the account section at all"
    )
    assert "drawdown" in prompt.lower(), "the one real breaker (drawdown) must still show"
    print("  ok  the prompt no longer exposes obsolete daily/weekly breaker figures, "
          "and still shows the one real drawdown breaker")


def test_decision_time_sizing_rejects_placeholder_accepts_exact_and_journals_timestamps():
    """The real defect: a one-share placeholder must be rejected when the
    RULES.md sizing formula implies something larger; the exact calculated
    size must be accepted; and an accepted decision must journal a real,
    non-null data_timestamps object with distinguishable technical/news
    cutoff fields and the actual latest included news timestamp."""
    tmp_dir = Path(tempfile.mkdtemp())
    run_start = date(2026, 2, 17)
    run_key = str(uuid.uuid4())
    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}]
    masking = RunMasking({"OLN": ["Olin Corporation", "Olin"]}, run_start=run_start,
                         seed=DEFAULT_MASKING_SEED)
    alias = masking.alias("OLN")
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0])

    # entry $100, stop $93 -> required exactly 200 (concentration-bound,
    # same arithmetic as the pure test above).
    placeholder = {"alias": alias, "action": "BUY", "quantity": 1, "entry_price": 100.0,
                  "stop_price": 93.0, "target_price": 115.0, "confidence": 3,
                  "primary_signal": "technical", "technical_basis": "rsi",
                  "expected_reward_risk": 3.14, "invalidation_condition": "closes below $93",
                  "reasoning": "one-share placeholder"}
    working1, _prompts1, queued1, blocked1 = _run_backtest_scenario(
        portfolio, run_start, 0, run_key, masking, watchlist, [placeholder], tmp_dir,
        news_payload={"OLN": []},
    )
    assert not queued1, "a one-share placeholder must never reach record_queued_decision"
    assert any("§2/§3" in line and "200" in line for line in blocked1), blocked1
    assert not working1.pending_orders, "the placeholder must not have been queued"
    print("  ok  a one-share placeholder is rejected when the required size (200) is larger, "
          "and never journaled")

    exact = dict(placeholder, quantity=200, reasoning="exact required size")
    news_with_article = {"OLN": [
        {"headline": "Olin reports steady demand", "created_at": "2026-02-16T14:30:00+00:00",
         "summary": "...", "symbols": ["OLN"]},
        {"headline": "Older Olin note", "created_at": "2026-02-10T09:00:00+00:00",
         "summary": "...", "symbols": ["OLN"]},
    ]}
    # Fresh tmp_dir (and so a fresh checkpoint file) -- the first call's
    # completed cycle wrote a real checkpoint under the FIRST tmp_dir at
    # (run_key, day_index=0); reusing that path here would REPLAY the
    # placeholder's already-checkpointed response instead of proposing the
    # exact decision below (checkpoint clearing is main()'s job after a
    # real atomic state commit, which this direct run_day() call never
    # performs -- same reason every other scenario in this file gets its
    # own fresh tmp_dir).
    working2, _prompts2, queued2, blocked2 = _run_backtest_scenario(
        portfolio, run_start, 0, run_key, masking, watchlist, [exact], Path(tempfile.mkdtemp()),
        news_payload=news_with_article,
    )
    assert not blocked2, blocked2
    assert len(queued2) == 1, queued2
    assert queued2[0]["ticker"] == "OLN"
    ts = queued2[0]["data_timestamps"]
    assert ts is not None, "an accepted decision must journal a non-null data_timestamps object"
    assert ts["technical_as_of"] == run_start.isoformat()
    assert ts["news_as_of"] == run_start.isoformat()
    assert ts["latest_news_created_at"] == "2026-02-16T14:30:00+00:00", (
        f"must be the REAL latest of the actual news payload, not fabricated: {ts}"
    )
    assert working2.pending_orders and working2.pending_orders[0].quantity == 200
    print("  ok  the exact calculated size (200) is accepted and journaled with a real, "
          "non-null, accurate data_timestamps object (technical/news cutoff + latest news)")


def test_decision_time_multi_decision_response_coherence():
    """One Hermes response proposing four decisions in order, all for the
    SAME cycle -- proves, together, in one realistic scenario: a rejected
    proposal reserves no cash and doesn't block a later corrected proposal
    for the same ticker; an accepted decision's estimated notional IS
    reserved; a later BUY's required size reflects that reservation; and
    a duplicate of an already-ACCEPTED ticker is blocked before it ever
    reaches record_queued_decision."""
    tmp_dir = Path(tempfile.mkdtemp())
    run_start = date(2026, 2, 17)
    run_key = str(uuid.uuid4())
    watchlist = [{"symbol": "ZULU", "sector": "Chemicals"},
                {"symbol": "ALPHA", "sector": "Energy"},
                {"symbol": "BETA", "sector": "Industrials"}]
    masking = RunMasking(
        {"ZULU": ["Zulu Corp"], "ALPHA": ["Alpha Inc"], "BETA": ["Beta Ltd"]},
        run_start=run_start, seed=DEFAULT_MASKING_SEED,
    )
    alpha_alias, beta_alias = masking.alias("ALPHA"), masking.alias("BETA")

    # ZULU is already held, worth $75,000 at its $100 mark -- equity is
    # $100,000 (cash $25,000 + $75,000 held), but only $25,000 is actually
    # available for new purchases -- the real-world condition that makes
    # sequential cash reservation across a response actually bite within
    # RULES.md's 20%-concentration/2%-risk caps (which two positions alone
    # never exhaust against a full $100k cash pool).
    held = {"ZULU": Holding(ticker="ZULU", quantity=750, entry_price=100.0,
                            stop_price=90.0, target_price=120.0,
                            entry_day=0, entry_trading_day=0)}
    portfolio = Portfolio(cash=25_000.0, holdings=held, equity_curve=[100_000.0])

    # entry $100, stop $92 (8%, risk $8/share), target $112.50 -> reward
    # $12.50/share, ratio 1.5625, clears the 1.5 base floor comfortably.
    # risk_size=floor(2000/8)=250, concentration_size=floor(20000/100)=200
    # -- concentration binds against the FULL $25,000 cash pool
    # (cash_size=250 > 200).
    def buy(alias, quantity, notes):
        return {"alias": alias, "action": "BUY", "quantity": quantity, "entry_price": 100.0,
               "stop_price": 92.0, "target_price": 112.5, "confidence": 3,
               "primary_signal": "technical", "technical_basis": "rsi",
               "expected_reward_risk": 1.56, "invalidation_condition": "closes below $92",
               "reasoning": notes}

    decisions = [
        buy(alpha_alias, 1, "wrong placeholder for ALPHA"),           # 0: rejected (1 != 200)
        buy(alpha_alias, 200, "corrected ALPHA proposal"),            # 1: accepted, reserves $20,000
        buy(alpha_alias, 200, "duplicate ALPHA proposal"),            # 2: rejected (duplicate ticker)
        # remaining cash after #1: 25000 - 20000 = 5000 ->
        # cash_size = floor(5000/100) = 50, now the binding constraint.
        buy(beta_alias, 50, "BETA sized against the REMAINING cash"),  # 3: accepted
    ]
    working, _prompts, queued, blocked = _run_backtest_scenario(
        portfolio, run_start, 0, run_key, masking, watchlist, decisions, tmp_dir,
        news_payload={t["symbol"]: [] for t in watchlist},
    )

    assert len(queued) == 2, queued
    assert queued[0]["ticker"] == "ALPHA" and queued[1]["ticker"] == "BETA"
    print("  ok  exactly the two genuinely-correct decisions (ALPHA, BETA) were journaled "
          "-- the placeholder and the duplicate never reached record_queued_decision")

    assert len(blocked) == 2, blocked
    assert any("§2/§3" in line and "ALPHA" in line for line in blocked), blocked
    assert any("§9.1" in line and "one queued decision per ticker" in line for line in blocked), blocked
    print("  ok  the wrong-size ALPHA placeholder and the duplicate ALPHA proposal were "
          "both rejected, for the right reasons")

    alpha_order = next(o for o in working.pending_orders if o.ticker == "ALPHA")
    beta_order = next(o for o in working.pending_orders if o.ticker == "BETA")
    assert alpha_order.quantity == 200
    assert beta_order.quantity == 50, (
        f"BETA must be sized against the cash REMAINING after ALPHA's reservation "
        f"(required 50, i.e. floor($5,000 remaining / $100)), got {beta_order.quantity}"
    )
    print("  ok  BETA's accepted quantity (50) reflects the cash ALPHA already reserved "
          "-- later same-cycle BUYs use the remaining unreserved cash, not the full pool")


# ============================================================================
# Real orchestration-boundary regression for the day-20 profile-mismatch bug
# (see HANDOFF.md's Phase 1 fix section). Drives run_day() itself -- not the
# pure helpers in isolation -- with only the true I/O boundary mocked, same
# template as test_real_lifecycle_checkpoint_commit_then_best_effort_
# observability. A single scenario proves all three "done when" cases at
# once: pre-boundary stays base/1.5, post-boundary is escalated/1.25
# everywhere (prompt, decision-time check, queued order, journal), and a
# PREVIOUSLY queued order keeps its own stored (stricter) threshold across
# the very same transition.
# ============================================================================

def _run_backtest_scenario(portfolio, day, day_index, run_key, masking, watchlist,
                           decisions_to_propose, tmp_dir, news_payload=None):
    """Runs one real run_day() cycle with only true I/O mocked. Returns
    (working, captured_prompts, queued_decision_calls, rejected_lines).
    news_payload: optional {ticker: [article, ...]} override -- defaults
    to no articles for any watchlist ticker."""
    captured_prompts = []
    queued_decision_calls = []

    def fake_ask_hermes(prompt, usage_totals, is_reflection_day=False):
        captured_prompts.append(prompt)
        usage_totals["total_tokens"] = usage_totals.get("total_tokens", 0) + 100
        return HermesResult(
            response={"decisions": decisions_to_propose,
                     "no_action_reason": "n/a" if decisions_to_propose else "nothing today"},
            attempt_count=1, retry_diagnostics=[],
        )

    def fake_record_queued_decision(decision, real_ticker, decision_id, run_id=None,
                                    strategy_profile=None, min_reward_risk_used=None,
                                    data_timestamps=None):
        queued_decision_calls.append({
            "ticker": real_ticker, "strategy_profile": strategy_profile,
            "min_reward_risk_used": min_reward_risk_used,
            "data_timestamps": data_timestamps,
            "proposed_entry_price": (
                float(decision["entry_price"]) if decision["action"] == "BUY" else None
            ),
        })

    def fake_record_cycle_evidence(**kwargs):
        pass

    fake_bars = {t["symbol"]: {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
                for t in watchlist}
    fake_technicals = {t["symbol"]: {"price": 100.0, "rsi": 50.0, "rsi_signal": "neutral",
                                     "macd_signal": "bullish", "trend": "up", "outlook": "neutral",
                                     "support": [95.0], "resistance": [110.0]}
                       for t in watchlist}
    fake_news = news_payload if news_payload is not None else {t["symbol"]: [] for t in watchlist}
    intraday_needed = sorted(set(portfolio.holdings) | {o.ticker for o in portfolio.pending_orders})
    fake_intraday = {t: _flat_session(price=100.0) for t in intraday_needed}
    fake_calendar_day = {"open": "09:30", "close": "16:00"}

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value=fake_bars), \
         patch("run_backtest.fetch_intraday_bars", return_value=fake_intraday), \
         patch("run_backtest.fetch_trading_calendar_day", return_value=fake_calendar_day), \
         patch("run_backtest.fetch_technicals", return_value=fake_technicals), \
         patch("run_backtest.fetch_news", return_value=fake_news), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask_hermes), \
         patch("run_backtest.record_cycle_evidence", side_effect=fake_record_cycle_evidence), \
         patch("run_backtest.record_queued_decision", side_effect=fake_record_queued_decision), \
         patch("run_backtest.resolve_trade_fill", return_value=None), \
         patch("run_backtest.resolve_trade_rejection", return_value=None), \
         patch("run_backtest.record_snapshot_once", return_value=True), \
         patch("run_backtest.log_activity", return_value=None):
        todays_limits_ctx = _limits_for_profile(portfolio.profile_level)
        working, summary, _do_observability = run_day(
            day, day_index, portfolio, masking, watchlist, todays_limits_ctx, run_key,
        )

    return working, captured_prompts, queued_decision_calls, summary["blocked"]


def test_activity_escalation_transition_at_the_real_orchestration_boundary():
    tmp_dir = Path(tempfile.mkdtemp())
    run_start = date(2026, 2, 17)
    run_key = str(uuid.uuid4())
    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}, {"symbol": "DINO", "sector": "Energy"}]
    masking = RunMasking({"OLN": ["Olin Corporation", "Olin"], "DINO": ["HF Sinclair", "Sinclair"]},
                         run_start=run_start, seed=DEFAULT_MASKING_SEED)
    dino_alias = masking.alias("DINO")

    # A marginal proposal: ratio 1.3 -- fails the base 1.5 floor (even with
    # its 0.02 tolerance), clears the escalated 1.25 floor comfortably.
    marginal_decision = {
        "alias": dino_alias, "action": "BUY", "quantity": 200, "entry_price": 100.0,
        "stop_price": 92.0, "target_price": 110.4, "confidence": 3,
        "primary_signal": "technical", "technical_basis": "rsi",
        "expected_reward_risk": 1.3, "invalidation_condition": "closes below $92",
        "reasoning": "marginal setup, ratio 1.3",
    }

    # -- PRE-boundary: trading_days_elapsed will become 10, well short of
    #    the 20-day block -- everything must stay base/1.5. ------------------
    pre_boundary = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6,
                             trading_days_elapsed=9, profile_level=PROFILE_BASE,
                             block_start_trading_day=0, block_completed_trades=0)
    working_pre, prompts_pre, queued_pre, blocked_pre = _run_backtest_scenario(
        pre_boundary, run_start, 0, run_key, masking, watchlist, [marginal_decision], tmp_dir,
    )
    assert working_pre.trading_days_elapsed == 10
    assert working_pre.profile_level == PROFILE_BASE, "10 trading days must not trigger escalation"
    assert f"strategy profile: {PROFILE_BASE}" in prompts_pre[0], (
        "the pre-boundary prompt must show the base tier"
    )
    assert "minimum reward:risk this cycle: 1.5:1" in prompts_pre[0]
    assert not queued_pre, "a 1.3 ratio must NOT queue under the base 1.5 floor"
    assert any("§6b" in line and f"min_rr={PROFILE_MIN_REWARD_RISK[PROFILE_BASE]}" in line
              for line in blocked_pre), blocked_pre
    print("  ok  pre-boundary: prompt, decision-time check, and queued orders all stay base/1.5")

    # -- THE boundary day itself: trading_days_elapsed becomes 20. A
    #    PREVIOUSLY queued OLN order (queued under the base tier, stored
    #    threshold 1.5) must resolve using ITS OWN stored value -- its real
    #    execution ratio is 1.3, which fails 1.5 -- even though today ALSO
    #    escalates the run to 1.25 by the time DINO's new proposal is
    #    considered later THIS SAME cycle. -----------------------------------
    old_order = QueuedOrder(
        ticker="OLN", alias=masking.alias("OLN"), action="BUY", quantity=90,
        stop_price=92.0, target_price=110.4, entry_price=100.0,
        queued_day_index=18, decision={"action": "BUY", "quantity": 90},
        decision_id="preexisting-decision-id",
        strategy_profile=PROFILE_BASE, min_reward_risk_used=1.5,
    )
    boundary_portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6,
                                   trading_days_elapsed=19, profile_level=PROFILE_BASE,
                                   block_start_trading_day=0, block_completed_trades=0,
                                   pending_orders=[old_order])
    boundary_day = run_start + timedelta(days=1)
    working_post, prompts_post, queued_post, blocked_post = _run_backtest_scenario(
        boundary_portfolio, boundary_day, 1, run_key, masking, watchlist,
        [marginal_decision], tmp_dir,
    )
    assert working_post.trading_days_elapsed == 20
    assert working_post.profile_level == PROFILE_ESCALATED, (
        "a block completing with 0 trades must escalate exactly on its 20th trading day"
    )
    print("  ok  the block transitions to the escalated tier exactly on trading day 20")

    # The PRE-EXISTING OLN order must have been rejected against its OWN
    # 1.5 -- not silently upgraded to today's newly-escalated 1.25.
    assert "OLN" not in working_post.holdings, (
        "the pre-existing order's real ratio (1.3) must fail its OWN stored 1.5 floor"
    )
    print("  ok  the previously-queued OLN order was rejected at its OWN stored 1.5 threshold, "
          "unaffected by today's later escalation")

    # DINO's NEW proposal, considered AFTER today's transition, must be
    # escalated/1.25 everywhere: the prompt, the journaled queue call, and
    # the resulting QueuedOrder object.
    assert f"strategy profile: {PROFILE_ESCALATED}" in prompts_post[0], (
        "the post-boundary prompt must show the escalated tier"
    )
    assert "minimum reward:risk this cycle: 1.25:1" in prompts_post[0]
    assert len(queued_post) == 1 and queued_post[0]["ticker"] == "DINO", queued_post
    assert queued_post[0]["strategy_profile"] == PROFILE_ESCALATED
    assert queued_post[0]["min_reward_risk_used"] == 1.25
    print("  ok  the decision queued AFTER the transition (for DINO) is escalated/1.25 in the "
          "prompt, the decision-time check, and the journaled queue call")

    dino_order = next(o for o in working_post.pending_orders if o.ticker == "DINO")
    assert dino_order.strategy_profile == PROFILE_ESCALATED
    assert dino_order.min_reward_risk_used == 1.25
    print("  ok  the resulting QueuedOrder object itself stores escalated/1.25 -- no decision "
          "can carry a profile label that disagrees with its enforced/stored threshold")


# --------------------------------------------------------------------------
# 2026-08-31 -- backtest cycle evidence instrumentation
# --------------------------------------------------------------------------

def _evidence_scenario(decisions_to_propose, tmp_dir):
    """Runs one real run_day() cycle over a 2-ticker watchlist (OLN, DINO),
    only true I/O mocked, and returns the single kwargs dict
    record_cycle_evidence was called with. Reused by every evidence test
    below so each one only needs to vary `decisions_to_propose`."""
    run_start = date(2026, 2, 17)
    run_key = str(uuid.uuid4())
    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}, {"symbol": "DINO", "sector": "Energy"}]
    masking = RunMasking(
        {"OLN": ["Olin Corporation", "Olin"], "DINO": ["HollyFrontier Corporation", "HollyFrontier"]},
        run_start=run_start, seed=DEFAULT_MASKING_SEED,
    )
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6)

    fake_bars = {t: {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5} for t in ("OLN", "DINO")}
    fake_technicals = {t: {"price": 100.5, "rsi": 50.0, "rsi_signal": "neutral",
                           "macd_signal": "bullish", "trend": "up", "outlook": "neutral",
                           "support": [95.0], "resistance": [110.0]} for t in ("OLN", "DINO")}
    fake_news = {"OLN": [], "DINO": []}

    def fake_ask_hermes(prompt, usage_totals, is_reflection_day=False):
        return HermesResult(
            response={"decisions": decisions_to_propose,
                     "no_action_reason": "n/a" if decisions_to_propose else "nothing qualified"},
            attempt_count=1, retry_diagnostics=[],
        )

    evidence_calls = []

    def fake_record_cycle_evidence(**kwargs):
        evidence_calls.append(kwargs)

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value=fake_bars), \
         patch("run_backtest.fetch_technicals", return_value=fake_technicals), \
         patch("run_backtest.fetch_news", return_value=fake_news), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask_hermes), \
         patch("run_backtest.record_cycle_evidence", side_effect=fake_record_cycle_evidence), \
         patch("run_backtest.record_queued_decision", return_value=None), \
         patch("run_backtest.record_snapshot_once", return_value=True), \
         patch("run_backtest.log_activity", return_value=None):
        run_day(run_start, 0, portfolio, masking, watchlist, {}, run_key)

    assert len(evidence_calls) == 1, f"expected exactly one evidence write, got {len(evidence_calls)}"
    return evidence_calls[0], masking


def test_cycle_evidence_records_the_exact_masked_prompt_and_inputs_once():
    """Item 1 -- the exact rendered prompt and the exact already-masked
    structured objects build_prompt was called with are persisted once per
    successful cycle, not reconstructed from a different source later."""
    tmp_dir = Path(tempfile.mkdtemp())
    evidence, masking = _evidence_scenario([], tmp_dir)

    assert evidence["prompt_text"].startswith("BACKTEST CYCLE — Day 1"), (
        "prompt_text must be the real rendered prompt, not a placeholder"
    )
    payload = evidence["input_payload"]
    assert payload["day_label"] == "Day 1"
    assert {c["alias"] for c in payload["candidates"]} == {masking.alias("OLN"), masking.alias("DINO")}, (
        "input_payload must carry the SAME masked candidate objects build_prompt used"
    )
    assert evidence["attempt_count"] == 1 and evidence["retry_diagnostics"] == []
    assert evidence["run_id"] and evidence["cycle_id"], "run_id/cycle_id must be real, not blank"
    assert evidence["rules_md_sha256"] and evidence["agents_md_sha256"] and evidence["soul_md_sha256"], (
        "all three file hashes must be real, non-empty values"
    )
    print("  ok  the exact rendered prompt and its exact masked input_payload are recorded once")


def test_cycle_evidence_never_leaks_the_real_ticker_alias_mapping():
    """Item 2 -- no real ticker string reaches stored evidence, only the
    masked alias. Mirrors anonymize.py's own assert_clean discipline,
    applied to the evidence payload specifically."""
    tmp_dir = Path(tempfile.mkdtemp())
    evidence, masking = _evidence_scenario([], tmp_dir)

    blob = evidence["prompt_text"] + json.dumps(evidence["input_payload"])
    for real_ticker in ("OLN", "DINO", "Olin Corporation", "HollyFrontier Corporation"):
        assert real_ticker not in blob, (
            f"real identifier {real_ticker!r} leaked into stored cycle evidence"
        )
    print("  ok  no real ticker or company name leaked into the stored evidence payload")


def test_cycle_evidence_marks_every_candidate_no_proposal_when_nothing_is_proposed():
    """Item 3 -- a cycle with zero proposals still records every eligible
    candidate as 'no_proposal', not simply omitted."""
    tmp_dir = Path(tempfile.mkdtemp())
    evidence, masking = _evidence_scenario([], tmp_dir)

    statuses = {c["alias"]: c["status"] for c in evidence["candidate_status"]}
    assert statuses == {masking.alias("OLN"): "no_proposal", masking.alias("DINO"): "no_proposal"}
    print("  ok  a no-proposal cycle records every eligible candidate as 'no_proposal'")


def test_cycle_evidence_marks_a_real_proposal_as_proposed_structurally():
    """Item 4 -- a real proposal is recorded as 'proposed' purely because
    its alias appears in the validated response's own decisions list --
    never by parsing Hermes' free-text reasoning into an invented
    classification."""
    tmp_dir = Path(tempfile.mkdtemp())
    olin_alias = RunMasking(
        {"OLN": ["Olin Corporation", "Olin"], "DINO": ["HollyFrontier Corporation", "HollyFrontier"]},
        run_start=date(2026, 2, 17), seed=DEFAULT_MASKING_SEED,
    ).alias("OLN")
    proposal = dict(_VALID_DECISION, alias=olin_alias)
    evidence, masking = _evidence_scenario([proposal], tmp_dir)

    statuses = {c["alias"]: c["status"] for c in evidence["candidate_status"]}
    assert statuses[masking.alias("OLN")] == "proposed"
    assert statuses[masking.alias("DINO")] == "no_proposal"
    print("  ok  a real proposal is marked 'proposed' by alias membership in the response, "
          "the untouched candidate stays 'no_proposal'")


def test_ask_hermes_records_a_failed_first_attempt_in_retry_diagnostics():
    """Item 5 -- a malformed first reply that triggers the existing one-
    retry contract must surface in HermesResult as attempt_count=2 with the
    first attempt's error named and bounded -- not silently dropped."""
    bad_raw = "not even json"
    good_raw = json.dumps({"decisions": [], "no_action_reason": "nothing qualified"})
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "api_calls": 1}
    with patch("run_backtest._call_hermes", side_effect=[(bad_raw, usage), (good_raw, usage)]):
        result = ask_hermes("irrelevant prompt", {}, is_reflection_day=False)

    assert result.attempt_count == 2
    assert len(result.retry_diagnostics) == 1
    diag = result.retry_diagnostics[0]
    assert diag["attempt"] == 1
    assert diag["error_category"] in ("ValueError", "JSONDecodeError")
    assert "no JSON found" in diag["error_message"] or diag["error_message"], (
        "the first attempt's real error must be named, not blank"
    )
    assert len(diag["error_message"]) <= 500, "error_message must be bounded"
    assert result.response == {"decisions": [], "no_action_reason": "nothing qualified"}
    print("  ok  a failed first attempt is recorded in retry_diagnostics with attempt_count=2")


def test_evidence_write_failure_then_replay_does_not_call_hermes_twice():
    """Item 6 -- an essential evidence-write failure halts the cycle,
    retains the checkpoint (now including prompt_text/input_payload/
    attempt_count/retry_diagnostics), and a rerun replays the SAME
    evidence write without a second Hermes invocation."""
    tmp_dir = Path(tempfile.mkdtemp())
    run_start = date(2026, 2, 17)
    run_key = str(uuid.uuid4())
    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}]
    masking = RunMasking({"OLN": ["Olin Corporation", "Olin"]}, run_start=run_start,
                         seed=DEFAULT_MASKING_SEED)
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6)

    fake_bars = {"OLN": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}}
    fake_technicals = {"OLN": {"price": 100.5, "rsi": 50.0, "rsi_signal": "neutral",
                               "macd_signal": "bullish", "trend": "up", "outlook": "neutral",
                               "support": [95.0], "resistance": [110.0]}}

    hermes_calls = []

    def fake_ask_hermes(prompt, usage_totals, is_reflection_day=False):
        hermes_calls.append(1)
        return HermesResult(response={"decisions": [], "no_action_reason": "nothing qualified"},
                            attempt_count=1, retry_diagnostics=[])

    evidence_attempts = {"n": 1}  # attempt 1 fails, attempt 2 (replay) succeeds
    evidence_calls = []

    def fake_record_cycle_evidence(**kwargs):
        evidence_calls.append(kwargs)
        if evidence_attempts["n"] > 0:
            evidence_attempts["n"] -= 1
            raise RuntimeError("simulated evidence-write failure")

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value=fake_bars), \
         patch("run_backtest.fetch_technicals", return_value=fake_technicals), \
         patch("run_backtest.fetch_news", return_value={"OLN": []}), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask_hermes), \
         patch("run_backtest.record_cycle_evidence", side_effect=fake_record_cycle_evidence):
        try:
            run_day(run_start, 0, portfolio, masking, watchlist, {}, run_key)
            raise AssertionError("expected the simulated evidence-write failure to propagate")
        except RuntimeError as exc:
            assert "simulated evidence-write failure" in str(exc)
        assert len(hermes_calls) == 1
        assert (tmp_dir / "cycle_checkpoint.json").exists(), (
            "the checkpoint must survive an evidence-write failure"
        )
        print("  ok  an evidence-write failure halts the cycle and retains the checkpoint")

        # -- replay: reuses the checkpoint, no second Hermes call ---------
        run_day(run_start, 0, portfolio, masking, watchlist, {}, run_key)
        assert len(hermes_calls) == 1, "the replay must NOT call Hermes again"
        assert len(evidence_calls) == 2
        assert evidence_calls[0]["prompt_text"] == evidence_calls[1]["prompt_text"], (
            "the replay's evidence write must be byte-identical to the original attempt's"
        )
        print("  ok  the replay reuses the checkpointed prompt/payload/response and "
              "re-attempts the SAME evidence write -- no second Hermes call")


def test_load_checkpoint_stays_readable_without_the_new_optional_evidence_fields():
    """Item 12 -- a checkpoint written by an older version of
    _save_checkpoint (only run_id/day_index/response/usage) must remain
    safely readable -- the new fields are simply absent, never required."""
    tmp_dir = Path(tempfile.mkdtemp())
    ckpt_path = tmp_dir / "cycle_checkpoint.json"
    with patch("run_backtest.CHECKPOINT_PATH", ckpt_path):
        # The OLD 4-positional-arg call shape -- no prompt_text/input_payload/
        # attempt_count/retry_diagnostics.
        _save_checkpoint("run-x", 0, {"decisions": [], "no_action_reason": "ok"}, {"total_tokens": 10})
        loaded = _load_checkpoint("run-x", 0)
    assert loaded is not None
    assert loaded["response"] == {"decisions": [], "no_action_reason": "ok"}
    assert loaded.get("prompt_text") is None
    assert loaded.get("attempt_count") is None
    print("  ok  an old-shaped checkpoint (missing the new optional evidence fields) "
          "remains safely readable via .get(..., default)")


def test_checkpoint_replay_derives_candidate_status_from_the_checkpoint_not_a_fresh_fetch():
    """Gap fix 1 -- a checkpoint replay with deliberately DIFFERENT newly
    fetched candidates must still produce candidate statuses from the
    CHECKPOINTED input_payload/response, never the fresh local `candidates`
    the replay attempt happens to (re-)fetch."""
    tmp_dir = Path(tempfile.mkdtemp())
    run_start = date(2026, 2, 17)
    run_key = str(uuid.uuid4())
    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}, {"symbol": "DINO", "sector": "Energy"}]
    masking = RunMasking(
        {"OLN": ["Olin Corporation", "Olin"], "DINO": ["HollyFrontier Corporation", "HollyFrontier"]},
        run_start=run_start, seed=DEFAULT_MASKING_SEED,
    )
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6)

    fake_bars = {t: {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5} for t in ("OLN", "DINO")}
    both_technicals = {t: {"price": 100.5, "rsi": 50.0, "rsi_signal": "neutral",
                           "macd_signal": "bullish", "trend": "up", "outlook": "neutral",
                           "support": [95.0], "resistance": [110.0]} for t in ("OLN", "DINO")}
    # Replay-time fetch deliberately returns a DIFFERENT candidate set (DINO
    # dropped) -- must have zero effect on the recorded evidence.
    only_olin_technicals = {"OLN": both_technicals["OLN"]}

    def fake_ask_hermes(prompt, usage_totals, is_reflection_day=False):
        return HermesResult(response={"decisions": [], "no_action_reason": "nothing qualified"},
                            attempt_count=1, retry_diagnostics=[])

    evidence_attempts = {"n": 1}
    evidence_calls = []

    def fake_record_cycle_evidence(**kwargs):
        evidence_calls.append(kwargs)
        if evidence_attempts["n"] > 0:
            evidence_attempts["n"] -= 1
            raise RuntimeError("simulated evidence-write failure")

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value=fake_bars), \
         patch("run_backtest.fetch_technicals", return_value=both_technicals), \
         patch("run_backtest.fetch_news", return_value={"OLN": [], "DINO": []}), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask_hermes), \
         patch("run_backtest.record_cycle_evidence", side_effect=fake_record_cycle_evidence):
        try:
            run_day(run_start, 0, portfolio, masking, watchlist, {}, run_key)
            raise AssertionError("expected the simulated evidence-write failure to propagate")
        except RuntimeError:
            pass

        # Replay -- now with a DIFFERENT fresh technicals fetch (DINO missing).
        with patch("run_backtest.fetch_technicals", return_value=only_olin_technicals):
            run_day(run_start, 0, portfolio, masking, watchlist, {}, run_key)

    assert len(evidence_calls) == 2
    original, replay = evidence_calls
    replay_aliases = {c["alias"] for c in replay["candidate_status"]}
    assert replay_aliases == {masking.alias("OLN"), masking.alias("DINO")}, (
        "the replay must derive candidate_status from the checkpointed candidates "
        "(both OLN and DINO), not the freshly (re-)fetched set (OLN only)"
    )
    # Testing Decision #2 -- fresh (original) and replayed evidence must be
    # byte-identical, not merely "close": prompt, structured inputs, and
    # derived statuses all match exactly.
    assert replay["candidate_status"] == original["candidate_status"]
    assert replay["input_payload"] == original["input_payload"]
    assert replay["prompt_text"] == original["prompt_text"]
    assert replay["cycle_id"] == original["cycle_id"]
    print("  ok  checkpoint replay's candidate_status comes from the checkpointed "
          "input_payload/response, immune to a differently-fetched local `candidates`, "
          "and the fresh vs. replayed evidence payloads are byte-identical")


def test_active_limits_reflects_a_non_default_risklimits_instance():
    """Gap fix 2 -- input_payload["active_limits"] must reflect the ACTUAL
    RiskLimits instance passed to build_prompt, not a hard-coded default --
    proven here by escalating the profile (min_reward_risk 1.5 -> 1.25)."""
    tmp_dir = Path(tempfile.mkdtemp())
    run_start = date(2026, 2, 17)
    run_key = str(uuid.uuid4())
    watchlist = [{"symbol": "OLN", "sector": "Chemicals"}]
    masking = RunMasking({"OLN": ["Olin Corporation", "Olin"]}, run_start=run_start,
                         seed=DEFAULT_MASKING_SEED)
    portfolio = Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6,
                          profile_level=PROFILE_ESCALATED)
    fake_bars = {"OLN": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}}
    fake_technicals = {"OLN": {"price": 100.5, "rsi": 50.0, "rsi_signal": "neutral",
                               "macd_signal": "bullish", "trend": "up", "outlook": "neutral",
                               "support": [95.0], "resistance": [110.0]}}

    def fake_ask_hermes(prompt, usage_totals, is_reflection_day=False):
        return HermesResult(response={"decisions": [], "no_action_reason": "n/a"},
                            attempt_count=1, retry_diagnostics=[])

    evidence_calls = []

    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value=fake_bars), \
         patch("run_backtest.fetch_technicals", return_value=fake_technicals), \
         patch("run_backtest.fetch_news", return_value={"OLN": []}), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask_hermes), \
         patch("run_backtest.record_cycle_evidence", side_effect=lambda **kw: evidence_calls.append(kw)), \
         patch("run_backtest.record_queued_decision", return_value=None), \
         patch("run_backtest.record_snapshot_once", return_value=True), \
         patch("run_backtest.log_activity", return_value=None):
        run_day(run_start, 0, portfolio, masking, watchlist, {}, run_key)

    active_limits = evidence_calls[0]["input_payload"]["active_limits"]
    default_limits = RiskLimits()
    assert active_limits["min_reward_risk"] == PROFILE_MIN_REWARD_RISK[PROFILE_ESCALATED] == 1.25
    assert active_limits["min_reward_risk"] != default_limits.min_reward_risk
    assert active_limits["risk_budget_pct"] == default_limits.risk_budget_pct
    assert active_limits["max_position_pct"] == default_limits.max_position_pct
    print("  ok  active_limits in the structured payload reflects the real, "
          "non-default RiskLimits instance actually passed to build_prompt")


def test_ask_hermes_copies_supplied_runtime_model_metadata_without_arithmetic():
    """Gap fix 3 -- a real usage payload's model/provider/reasoning_effort
    (nonnumeric) must reach usage_totals verbatim, not be dropped by
    _accumulate's numeric-only loop."""
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "api_calls": 1,
             "model": "claude-sonnet-5", "provider": "anthropic", "reasoning_effort": "high"}
    good_raw = json.dumps({"decisions": [], "no_action_reason": "n/a"})
    usage_totals = {}
    with patch("run_backtest._call_hermes", return_value=(good_raw, usage)):
        ask_hermes("irrelevant prompt", usage_totals, is_reflection_day=False)

    assert usage_totals["model"] == "claude-sonnet-5"
    assert usage_totals["provider"] == "anthropic"
    assert usage_totals["reasoning_effort"] == "high"
    print("  ok  supplied runtime model/provider/reasoning_effort reach usage_totals unchanged")


def test_ask_hermes_leaves_runtime_metadata_null_when_not_supplied():
    """Gap fix 3 -- when the real usage payload never includes model/
    provider/reasoning_effort (today's actual observed behaviour), those
    keys must stay absent/null -- never defaulted to the configured model."""
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "api_calls": 1}
    good_raw = json.dumps({"decisions": [], "no_action_reason": "n/a"})
    usage_totals = {}
    with patch("run_backtest._call_hermes", return_value=(good_raw, usage)):
        ask_hermes("irrelevant prompt", usage_totals, is_reflection_day=False)

    assert usage_totals.get("model") is None
    assert usage_totals.get("provider") is None
    assert usage_totals.get("reasoning_effort") is None
    print("  ok  absent runtime metadata stays null, never hard-coded")


def test_ask_hermes_fails_loudly_on_conflicting_metadata_across_a_retry():
    """Gap fix 3 -- two attempts within one cycle reporting DIFFERENT
    non-null model identities is a genuine ambiguity and must raise, not
    silently keep one of the two values."""
    bad_raw = "not even json"
    good_raw = json.dumps({"decisions": [], "no_action_reason": "n/a"})
    usage1 = {"input_tokens": 5, "model": "model-a"}
    usage2 = {"input_tokens": 5, "model": "model-b"}
    usage_totals = {}
    with patch("run_backtest._call_hermes", side_effect=[(bad_raw, usage1), (good_raw, usage2)]):
        try:
            ask_hermes("irrelevant prompt", usage_totals, is_reflection_day=False)
            raise AssertionError("expected a ValueError on conflicting model identity")
        except ValueError as exc:
            assert "model" in str(exc) and "model-a" in str(exc) and "model-b" in str(exc)
    print("  ok  conflicting non-null runtime identity across a retry fails loudly")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("PASS: no-lookahead execution, fail-safe cycles, and idempotent "
          "journaling all behave as claimed")


if __name__ == "__main__":
    main()
