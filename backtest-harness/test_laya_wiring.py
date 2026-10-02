"""Phase 2: opt-in experimental wiring of the fixed-30 Laya selector into the
backtest controller. Drives the REAL run_day() with only the true I/O
boundary mocked (the same template test_execution_semantics.py uses) plus a
real SelectorContext over a deterministic FAKE scorer and synthetic bars.
No Docker, no Neon, no Hermes, no model weights, no trading/evaluation run.
Run: python -m pytest -q test_laya_wiring.py
"""
import json
import socket
import sys
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import laya_adapter
import laya_experiment as lx
import laya_preflight as lp
import laya_selector as ls
from anonymize import LeakDetected, RunMasking
from laya_cards import build_day_cards, compute_card_inputs
from run_backtest import (
    SELECTOR_MODE_LAYA_FIXED30, SELECTOR_MODE_OFF, HermesResult, Holding, Portfolio,
    _check_candidate_coverage, _expected_session_bar_times, _experimental_watchlist_entries,
    _full_candidate_status, _limits_for_profile, _load_current_watchlist, _names_for_masking,
    _resolve_selector_mode, _validate_selector_args, build_arg_parser, run_day,
)

DAY = date(2026, 3, 20)
SEED = 42
UNIVERSE = ls.load_universe()
WATCHLIST = _experimental_watchlist_entries(UNIVERSE)
SYMBOLS = list(UNIVERSE.symbols)


# -- fakes --------------------------------------------------------------------

def _bars(slope: float, end: date = DAY, n: int = 70, vol: float = 1_000_000.0):
    out = []
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        close = 100.0 * (1 + slope) ** i + (i % 3) * 0.2
        out.append({"date": d.isoformat(), "open": close - 0.1, "high": close + 0.6,
                    "low": close - 0.6, "close": close, "volume": vol + 10_000 * (i % 5)})
    return out


def _history(tickers, day):
    return {t: _bars(0.001 * (1 + SYMBOLS.index(t)), end=day) for t in tickers}


class FakeScorer:
    """Deterministic CardScorer; counts calls and records exactly what it was shown."""
    def __init__(self):
        self.metadata = laya_adapter.expected_metadata()
        self.calls = []

    def score(self, cards):
        self.calls.append([c.model_state() for c in cards])
        return [ls.ScoreRecord(c.row_id, min(0.999, max(0.001, 0.5 + 5 * c.model_state()["five_day_return"])), 0.77)
                for c in cards]


class FailingScorer(FakeScorer):
    def score(self, cards):
        raise ls.ScorerError("local laya worker unavailable")


def _ctx(tmp, scorer=None, run_id="run-t", fetch=None):
    scorer = scorer or FakeScorer()
    calls = []

    def fetch_history(tickers, day):
        calls.append((list(tickers), day))
        return (fetch or _history)(tickers, day)

    ctx = lx.SelectorContext(universe=UNIVERSE, seed=SEED, run_id=run_id, snapshot_dir=Path(tmp),
                             scorer=scorer, fetch_history=fetch_history)
    ctx.fetch_calls = calls
    return ctx


def _masking():
    return RunMasking(_names_for_masking(WATCHLIST), run_start=date(2026, 2, 17), seed=SEED,
                      alias_scheme="opaque", word_like_tickers=UNIVERSE.word_like_tickers)


def _tech(**over):
    t = {"price": 100.0, "rsi": 50.0, "rsi_signal": "neutral", "macd_signal": "bullish",
         "trend": "up", "outlook": "neutral", "support": [95.0], "resistance": [110.0]}
    t.update(over)
    return t


def _cycle(tmp, selector, *, portfolio=None, decisions=None, technicals=None, news=None,
           masking=None, watchlist=None, cap=None):
    """One real run_day with only I/O mocked. Returns a dict of captures."""
    portfolio = portfolio or Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6)
    masking = masking or _masking()
    watchlist = watchlist or WATCHLIST
    cap = cap if cap is not None else {}
    cap.update({"prompts": [], "tech_calls": [], "news_calls": [], "evidence": [], "queued": [],
                "sim_watchlists": []})
    tickers = [t["symbol"] for t in watchlist]

    def fake_ask(prompt, usage, is_reflection_day=False, fundamentals_enabled=False,
                 run_id=None, day_index=None, runtime=None):
        cap["prompts"].append(prompt)
        return HermesResult(response={"decisions": decisions or [], "no_action_reason": "nothing today"},
                            attempt_count=1, retry_diagnostics=[])

    def fake_tech(ts, day):
        cap["tech_calls"].append(list(ts))
        return {t: (technicals or {}).get(t, _tech()) for t in ts}

    def fake_news(ts, day, cutoff):
        cap["news_calls"].append(list(ts))
        return {t: (news or {}).get(t, []) for t in ts}

    import run_backtest as rb
    real_sim = rb.simulate_trading_day

    def spy_sim(**kw):
        cap["sim_watchlists"].append(list(kw["watchlist"]))
        return real_sim(**kw)

    needed = sorted(set(portfolio.holdings))
    session = _expected_session_bar_times("09:30", "16:00")
    intraday = {t: [{"time": s, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0} for s in session]
                for t in needed}
    tmp_dir = Path(tempfile.mkdtemp())
    with patch("run_backtest.CHECKPOINT_PATH", tmp_dir / "cycle_checkpoint.json"), \
         patch("run_backtest.fetch_bars", return_value={t: {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0} for t in tickers}), \
         patch("run_backtest.fetch_intraday_bars", return_value=intraday), \
         patch("run_backtest.fetch_trading_calendar_day", return_value={"open": "09:30", "close": "16:00"}), \
         patch("run_backtest.fetch_technicals", side_effect=fake_tech), \
         patch("run_backtest.fetch_news", side_effect=fake_news), \
         patch("run_backtest.fetch_recent_insights", return_value=[]), \
         patch("run_backtest.ask_hermes", side_effect=fake_ask), \
         patch("run_backtest.record_cycle_evidence", side_effect=lambda **kw: cap["evidence"].append(kw)), \
         patch("run_backtest.record_queued_decision", side_effect=lambda *a, **kw: cap["queued"].append((a, kw))), \
         patch("run_backtest.resolve_trade_fill", return_value=None), \
         patch("run_backtest.resolve_trade_rejection", return_value=None), \
         patch("run_backtest.record_snapshot_once", return_value=True), \
         patch("run_backtest.log_activity", return_value=None), \
         patch("run_backtest.simulate_trading_day", side_effect=spy_sim):
        working, summary, _obs = run_day(
            DAY, 31, portfolio, masking, watchlist, _limits_for_profile(portfolio.profile_level),
            str(uuid.uuid4()), selector=selector)
    cap.update(working=working, summary=summary, masking=masking,
               checkpoint_path=tmp_dir / "cycle_checkpoint.json")
    return cap


def _selected(tmp, run_id="run-t"):
    snap = ls.load_snapshot(Path(tmp), run_id, DAY.isoformat())
    ctx_ids = ls.make_row_ids(SYMBOLS, SEED, UNIVERSE.hash)
    by_row = {v: k for k, v in ctx_ids.items()}
    return [by_row[r.row_id] for r in sorted((r for r in snap.rows if r.status == "selected"),
                                             key=lambda r: r.rank)]


# -- selector mode boundary -----------------------------------------------------

def test_selector_mode_defaults_to_off_and_is_never_implicit():
    args = build_arg_parser().parse_args([])
    assert args.selector_mode is None and args.laya_image is None and args.laya_checkpoint is None
    assert _resolve_selector_mode(False, None, None) == SELECTOR_MODE_OFF
    assert _resolve_selector_mode(False, None, SELECTOR_MODE_LAYA_FIXED30) == SELECTOR_MODE_LAYA_FIXED30
    with pytest.raises(SystemExit):  # unknown values are not accepted
        build_arg_parser().parse_args(["--selector-mode", "on"])


def test_resume_keeps_stored_selector_mode_and_conflicts_fail_closed():
    assert _resolve_selector_mode(True, None, None) == SELECTOR_MODE_OFF           # legacy state
    assert _resolve_selector_mode(True, SELECTOR_MODE_LAYA_FIXED30, None) == SELECTOR_MODE_LAYA_FIXED30
    with pytest.raises(SystemExit, match="legacy state"):
        _resolve_selector_mode(True, None, SELECTOR_MODE_LAYA_FIXED30)             # can't enable mid-run
    with pytest.raises(SystemExit, match="conflicts"):
        _resolve_selector_mode(True, SELECTOR_MODE_LAYA_FIXED30, SELECTOR_MODE_OFF)


def test_laya_flags_require_the_experimental_mode_and_an_existing_checkpoint(tmp_path):
    parse = build_arg_parser().parse_args
    with pytest.raises(SystemExit, match="is off"):
        _validate_selector_args(SELECTOR_MODE_OFF, parse(["--laya-image", "x"]))
    with pytest.raises(SystemExit, match="is off"):
        _validate_selector_args(SELECTOR_MODE_OFF, parse(["--laya-checkpoint", str(tmp_path)]))
    _validate_selector_args(SELECTOR_MODE_OFF, parse([]))
    with pytest.raises(SystemExit, match="requires --laya-checkpoint"):
        _validate_selector_args(SELECTOR_MODE_LAYA_FIXED30, parse([]))
    with pytest.raises(SystemExit, match="not a directory"):
        _validate_selector_args(SELECTOR_MODE_LAYA_FIXED30, parse(["--laya-checkpoint", str(tmp_path / "no")]))
    _validate_selector_args(SELECTOR_MODE_LAYA_FIXED30, parse(["--laya-checkpoint", str(tmp_path)]))
    # the host-process worker flag no longer exists
    with pytest.raises(SystemExit):
        parse(["--laya-python", "x"])


def test_off_mode_loads_only_the_baseline_watchlist_and_experimental_only_the_fixed30():
    wl, uni = _load_current_watchlist(SELECTOR_MODE_OFF)
    assert uni is None and [t["symbol"] for t in wl] == [
        "OLN", "WMG", "PLNT", "SFM", "DINO", "RYAN", "BRKR", "ATI", "BRX", "ALGM"]
    wl30, uni30 = _load_current_watchlist(SELECTOR_MODE_LAYA_FIXED30)
    assert [t["symbol"] for t in wl30] == SYMBOLS and len(wl30) == 30 and uni30.version == "fixed30-v1"
    # ...and never reads watchlist.json while in experimental mode:
    import run_backtest as rb
    real = Path.read_text
    seen = []
    with patch.object(Path, "read_text", lambda self, *a, **k: (seen.append(self.name), real(self, *a, **k))[1]):
        rb._load_current_watchlist(SELECTOR_MODE_LAYA_FIXED30)
    assert "watchlist.json" not in seen and "experimental_fixed30.json" in seen


def test_selector_off_run_day_is_identical_whether_selector_is_omitted_or_none():
    wl = [{"symbol": "OLN", "sector": "Chemicals"}, {"symbol": "DINO", "sector": "Energy"}]
    masking = RunMasking({"OLN": ["Olin"], "DINO": ["HF Sinclair"]}, run_start=date(2026, 2, 17), seed=SEED)
    # DINO's technicals "fail" -> the ORIGINAL excluded/technical_fetch_error status must be used.
    def run(**kw):
        import run_backtest as rb
        captured = {}
        with patch("run_backtest.CHECKPOINT_PATH", Path(tempfile.mkdtemp()) / "c.json"), \
             patch("run_backtest.fetch_bars", return_value={"OLN": {"open": 1, "high": 1, "low": 1, "close": 1},
                                                            "DINO": {"open": 1, "high": 1, "low": 1, "close": 1}}), \
             patch("run_backtest.fetch_trading_calendar_day", return_value={"open": "09:30", "close": "16:00"}), \
             patch("run_backtest.fetch_technicals", side_effect=lambda ts, d: (captured.setdefault("t", list(ts)) and
                                                                               {"OLN": _tech(), "DINO": {"error": "boom"}})), \
             patch("run_backtest.fetch_news", side_effect=lambda ts, d, c: captured.setdefault("n", list(ts)) and {}), \
             patch("run_backtest.fetch_recent_insights", return_value=[]), \
             patch("run_backtest.ask_hermes", return_value=HermesResult(
                 response={"decisions": [], "no_action_reason": "n"}, attempt_count=1, retry_diagnostics=[])), \
             patch("run_backtest.record_cycle_evidence", side_effect=lambda **k: captured.setdefault("e", k)):
            _w, summary, _o = run_day(DAY, 0, Portfolio(cash=100_000.0, equity_curve=[100_000.0] * 6),
                                      masking, wl, {}, "run-x", **kw)
        return captured, summary

    a, sa = run()
    b, sb = run(selector=None)
    assert a["t"] == b["t"] == ["OLN", "DINO"] and a["n"] == b["n"]          # every unheld name fetched, as before
    assert a["e"]["candidate_status"] == b["e"]["candidate_status"]
    assert {"alias": masking.alias("DINO"), "status": "excluded", "reason": "technical_fetch_error"} in \
        a["e"]["candidate_status"]
    assert "selector" not in sa and "selector" not in sb and sa == sb
    assert "selector" not in json.dumps(a["e"]["input_payload"])


def test_full_candidate_status_default_is_unchanged_without_selector_status():
    m = RunMasking({"OLN": ["Olin"], "DINO": ["HF Sinclair"]}, run_start=date(2026, 2, 17), seed=1)
    out = _full_candidate_status(["OLN", "DINO"], ["OLN"], [], {"decisions": []}, m)
    assert out == [{"alias": m.alias("OLN"), "status": "held"},
                   {"alias": m.alias("DINO"), "status": "excluded", "reason": "technical_fetch_error"}]


# -- cards: point-in-time + identity-free -----------------------------------------

def test_card_inputs_ignore_every_bar_after_the_as_of_date():
    bars = _bars(0.002)
    base, latest = compute_card_inputs(bars, DAY)
    future = [{"date": (DAY + timedelta(days=i)).isoformat(), "open": 1e6, "high": 1e6, "low": 1e-3,
               "close": 1e6, "volume": 1e12} for i in range(1, 6)]
    with_future, latest2 = compute_card_inputs(bars + future, DAY)
    assert with_future == base and latest == latest2 == DAY
    earlier, latest3 = compute_card_inputs(bars + future, DAY - timedelta(days=3))
    assert latest3 == DAY - timedelta(days=3) and earlier != base


def test_card_formulas_on_hand_checkable_series():
    # 60 flat bars at 100 with the last close 110: closes[-6] == 100 -> +10%; sma20 = (19*100+110)/20.
    bars = [{"date": (DAY - timedelta(days=59 - i)).isoformat(), "open": 100.0, "high": 100.0,
             "low": 100.0, "close": 100.0, "volume": 1000.0} for i in range(60)]
    bars[-1].update(close=110.0, high=110.0, volume=3000.0)
    c, latest = compute_card_inputs(bars, DAY)
    assert c["five_day_return"] == pytest.approx(0.10)
    assert c["sma20_distance"] == pytest.approx(110 / 100.5 - 1)
    assert c["relative_volume"] == pytest.approx(3.0)
    assert c["range_position_20d"] == pytest.approx(1.0)
    assert c["rsi14"] == pytest.approx(100.0)            # only gains in the window
    assert 0 < c["macd_direction"] < 1 and c["realized_volatility"] > 0
    assert set(c) == set(ls.NUMERIC_FIELDS)


def test_short_history_degrades_to_an_unavailable_card_not_an_error():
    inputs, latest = compute_card_inputs(_bars(0.001, n=45)[-10:], DAY)
    assert all(v is None for v in inputs.values()) and latest == DAY


def test_malformed_bars_are_dropped_and_a_zero_volume_baseline_is_unavailable():
    bars = _bars(0.001, n=45)
    bars[30] = {**bars[30], "close": -1.0}                     # dropped, 44 usable bars remain
    inputs, _ = compute_card_inputs(bars, DAY)
    assert all(v is not None for v in inputs.values())
    flat_volume = [{**b, "volume": 0.0} for b in _bars(0.001, n=45)]
    assert all(v is None for v in compute_card_inputs(flat_volume, DAY)[0].values())


def test_cards_for_missing_or_errored_history_are_data_unavailable_with_reasons():
    ids = ls.make_row_ids(["OLN", "DINO", "RYAN"], 1, "h")
    hist = {"OLN": _bars(0.001), "DINO": {"error": "yfinance down"}}   # RYAN missing entirely
    cards, latest, reasons = build_day_cards(hist, ["OLN", "DINO", "RYAN"], DAY, ids)
    assert [c.data_complete for c in cards] == [True, False, False]
    assert reasons == {ids["DINO"]: "history_fetch_error", ids["RYAN"]: "history_missing"}
    assert set(latest) == {ids["OLN"]}


def test_laya_only_ever_sees_numeric_cards_no_identity_dates_or_text(tmp_path):
    scorer = FakeScorer()
    _ctx(tmp_path, scorer).select_for_day(DAY, set())
    assert len(scorer.calls) == 1, "ONE batched call for the whole eligible set"
    assert len(scorer.calls[0]) == 30
    blob = json.dumps(scorer.calls)
    for m in UNIVERSE.members:
        assert m.symbol not in blob and m.name not in blob
    assert str(DAY.year) not in blob and "2026" not in blob
    assert all(tuple(s) == ls.NUMERIC_FIELDS for s in scorer.calls[0])


# -- selection: eligibility, replay, failure -----------------------------------------

def test_held_and_pending_names_are_excluded_before_selection_and_never_scored(tmp_path):
    scorer = FakeScorer()
    ctx = _ctx(tmp_path, scorer)
    out = ctx.select_for_day(DAY, {"OLN", "DINO"})        # held + pending-entry union, as run_day passes it
    assert ctx.fetch_calls[0][0] == [s for s in SYMBOLS if s not in ("OLN", "DINO")]
    assert len(scorer.calls[0]) == 28
    assert out.event["eligible"] == 28 and out.event["excluded_held_or_pending"] == 2
    assert not {"OLN", "DINO"} & set(out.selected) and len(out.selected) == 10
    assert not {"OLN", "DINO"} & set(out.status_by_ticker)
    snap = ls.load_snapshot(tmp_path, "run-t", DAY.isoformat())
    assert len(snap.rows) == 28 and snap.k == 10 and snap.scorer == laya_adapter.expected_metadata()


def test_ranking_is_deterministic_and_top_10_by_review_probability(tmp_path):
    a = _ctx(tmp_path / "a").select_for_day(DAY, set())
    b = _ctx(tmp_path / "b").select_for_day(DAY, set())
    assert a.selected == b.selected and len(a.selected) == 10
    rows = ls.load_snapshot(tmp_path / "a", "run-t", DAY.isoformat()).rows
    assert [r.review_probability for r in rows if r.status == "selected"] == sorted(
        (r.review_probability for r in rows if r.status == "selected"), reverse=True)


def test_a_retry_replays_the_stored_snapshot_and_never_rescores(tmp_path):
    first = _ctx(tmp_path).select_for_day(DAY, {"OLN"})
    assert first.event["replayed"] is False
    # A brand-new context (as after a process restart) whose scorer would raise if touched:
    boom = FailingScorer()
    again = _ctx(tmp_path, boom)
    out = again.select_for_day(DAY, {"OLN"})
    assert out.ok and out.event["replayed"] is True and out.selected == first.selected
    assert out.event["snapshot_sha256"] == first.event["snapshot_sha256"]
    assert boom.calls == [] and again.fetch_calls == [], "replay must not fetch history or start the model"


def test_a_conflicting_stored_snapshot_is_never_overwritten_and_fails_closed(tmp_path):
    _ctx(tmp_path).select_for_day(DAY, set())
    path = ls.snapshot_path(tmp_path, "run-t", DAY.isoformat())
    original = path.read_text(encoding="utf-8")
    # Same date, but the run now holds OLN -> the eligible set differs from the stored snapshot.
    out = _ctx(tmp_path).select_for_day(DAY, {"OLN"})
    assert not out.ok and out.selected == () and out.event["status"] == "failed_closed"
    assert "snapshot_replay_mismatch" in out.event["reason"] and "eligible_set" in out.event["reason"]
    assert path.read_text(encoding="utf-8") == original


def test_scorer_failure_fails_closed_for_that_day_with_a_clear_event(tmp_path):
    out = _ctx(tmp_path, FailingScorer()).select_for_day(DAY, set())
    assert (out.ok, out.selected) == (False, ())
    assert out.event["status"] == "failed_closed" and "local laya worker unavailable" in out.event["reason"]
    assert out.event["selected"] == 0
    assert ls.load_snapshot(tmp_path, "run-t", DAY.isoformat()) is None, "no snapshot for a failed day"
    # the next day is independent: a good scorer works again
    assert _ctx(tmp_path).select_for_day(DAY + timedelta(days=1), set()).ok


def test_history_fetch_failure_fails_closed(tmp_path):
    def boom(tickers, day):
        raise RuntimeError("maverick-mcp failed")
    out = _ctx(tmp_path, fetch=boom).select_for_day(DAY, set())
    assert not out.ok and "maverick-mcp failed" in out.event["reason"]


def test_unavailable_cards_are_labelled_data_unavailable_not_rejected(tmp_path):
    def partial(tickers, day):
        h = _history(tickers, day)
        h["OLN"] = {"error": "no data"}
        h["DINO"] = h["DINO"][-10:]
        return h
    out = _ctx(tmp_path, fetch=partial).select_for_day(DAY, set())
    assert out.ok and out.event["data_unavailable"] == 2
    assert out.status_by_ticker["OLN"][0] == out.status_by_ticker["DINO"][0] == "data_unavailable"
    assert not {"OLN", "DINO"} & set(out.selected)


def test_selector_outcome_record_round_trips():
    o = lx.SelectorOutcome(True, ("OLN", "DINO"), {"OLN": ("selected", "r"), "RYAN": ("not_selected", "r")},
                           {"status": "selected", "selected": 2})
    assert lx.SelectorOutcome.from_record(json.loads(json.dumps(o.to_record()))) == o


# -- run_day integration ------------------------------------------------------------

def test_only_selected_names_trigger_full_evidence_fetches_and_hermes_sees_no_laya_data(tmp_path):
    ctx = _ctx(tmp_path)
    cap = _cycle(tmp_path, ctx)
    selected = _selected(tmp_path)
    assert len(selected) == 10
    assert cap["tech_calls"] == [selected] and cap["news_calls"] == [selected]
    assert len(set(cap["tech_calls"][0])) == 10 < 30, "the other 20 names must trigger no technical/news fetch"

    # What Hermes was shown: only the selected, fully masked packet. (The masked RULES.md block
    # carries the baseline watchlist TABLE -- 10 masked rows -- which is unchanged rules text, not
    # selector data; the packet check below looks at everything outside that block.)
    full_prompt = cap["prompts"][0]
    head, _, rest = full_prompt.partition("===== RULES.md (verbatim")
    prompt = head + rest.partition("===== END RULES.md =====")[2]
    m = cap["masking"]
    for t in selected:
        assert m.alias(t) in prompt
    for t in set(SYMBOLS) - set(selected):
        assert m.alias(t) not in prompt, "a non-selected name leaked into the packet"
    for member in UNIVERSE.members:
        assert member.symbol not in prompt.split() and member.name not in prompt
    # Laya's outputs never reach the prompt or the recorded input payload.
    snap = ls.load_snapshot(tmp_path, "run-t", DAY.isoformat())
    forbidden = ["review_probability", "answer_confidence", "laya", "Laya", "tie_break"]
    forbidden += [r.row_id for r in snap.rows]
    forbidden += [f"{r.review_probability:.4f}" for r in snap.rows if r.review_probability is not None]
    payload = json.dumps(cap["evidence"][0]["input_payload"])
    for token in forbidden:
        assert token not in prompt, token
        assert token not in payload, token
    assert {tuple(sorted(c)) for c in cap["evidence"][0]["input_payload"]["candidates"]} == {
        ("alias", "news", "sector", "tech")}
    assert cap["summary"]["selector"]["status"] == "selected" and cap["summary"]["selector"]["selected"] == 10


def test_candidate_status_records_not_selected_and_unavailable_honestly(tmp_path):
    def partial(tickers, day):
        h = _history(tickers, day)
        h["OLN"] = {"error": "no data"}
        return h
    cap = _cycle(tmp_path, _ctx(tmp_path, fetch=partial))
    m = cap["masking"]
    status = {s["alias"]: s for s in cap["evidence"][0]["candidate_status"]}
    selected = _selected(tmp_path)
    assert status[m.alias("OLN")] == {"alias": m.alias("OLN"), "status": "selector_data_unavailable",
                                      "reason": "selector_card_unavailable"}
    for t in set(SYMBOLS) - set(selected) - {"OLN"}:
        assert status[m.alias(t)] == {"alias": m.alias(t), "status": "not_selected"}, t
    assert all(status[m.alias(t)]["status"] in ("proposed", "no_proposal") for t in selected)
    assert "technical_fetch_error" not in json.dumps(list(status.values()))


def test_held_position_keeps_exit_management_while_excluded_from_selection(tmp_path):
    held = Portfolio(cash=90_000.0, equity_curve=[100_000.0] * 6, trading_days_elapsed=3,
                     holdings={"OLN": Holding("OLN", 100, 100.0, 95.0, 120.0, 28, 2)})
    ctx = _ctx(tmp_path)
    cap = _cycle(tmp_path, ctx, portfolio=held)
    selected = _selected(tmp_path)
    assert "OLN" not in selected and "OLN" not in ctx.fetch_calls[0][0]       # excluded from selection
    assert cap["tech_calls"] == [selected + ["OLN"]] and cap["news_calls"] == [selected + ["OLN"]]
    m = cap["masking"]
    assert cap["evidence"][0]["input_payload"]["positions"][0]["alias"] == m.alias("OLN")
    assert cap["evidence"][0]["input_payload"]["positions"][0]["tech"] is not None
    assert {"alias": m.alias("OLN"), "status": "held"} in cap["evidence"][0]["candidate_status"]
    # the independent exit machinery still saw the held name
    assert cap["sim_watchlists"] and cap["sim_watchlists"][0] == SYMBOLS


def test_selector_boundary_blocks_buys_of_unselected_names_but_risk_membership_is_unchanged(tmp_path):
    ctx = _ctx(tmp_path)
    # Pre-compute which names will be selected so a BUY can target an unselected, otherwise-valid one.
    probe = _ctx(tmp_path / "probe").select_for_day(DAY, set())
    unselected = next(t for t in SYMBOLS if t not in probe.selected)
    m = _masking()
    buy = {"alias": m.alias(unselected), "action": "BUY", "quantity": 10, "entry_price": 100.0,
           "stop_price": 95.0, "target_price": 110.0, "confidence": 3, "primary_signal": "technical",
           "technical_basis": "rsi", "expected_reward_risk": 2.0,
           "invalidation_condition": "closes below $95", "reasoning": "x"}
    cap = _cycle(tmp_path, ctx, decisions=[buy], masking=m)
    blocked = cap["summary"]["blocked"]
    assert any("selector boundary rejected" in b and unselected in b for b in blocked), blocked
    assert cap["queued"] == []
    # A selected name's BUY is NOT stopped by the selector boundary (the usual risk/size checks decide).
    sel = probe.selected[0]
    buy2 = {**buy, "alias": m.alias(sel)}
    cap2 = _cycle(tmp_path / "x", _ctx(tmp_path / "x"), decisions=[buy2], masking=m)
    assert not any("selector boundary" in b for b in cap2["summary"]["blocked"])
    # Risk-checker membership stays the frozen 30-name universe -- not the day's pool.
    held = Portfolio(cash=90_000.0, equity_curve=[100_000.0] * 6,
                     holdings={"OLN": Holding("OLN", 100, 100.0, 95.0, 120.0, 28, 0)})
    cap3 = _cycle(tmp_path / "y", _ctx(tmp_path / "y"), portfolio=held)
    assert cap3["sim_watchlists"][0] == SYMBOLS


def test_selector_failure_blocks_new_entries_but_exit_management_stays_available(tmp_path):
    held = Portfolio(cash=90_000.0, equity_curve=[100_000.0] * 6, trading_days_elapsed=3,
                     holdings={"OLN": Holding("OLN", 100, 100.0, 95.0, 120.0, 28, 2)})
    m = _masking()
    sell = {"alias": m.alias("OLN"), "action": "SELL", "quantity": 100, "entry_price": 100.0,
            "confidence": 3, "primary_signal": "technical", "technical_basis": "rsi",
            "invalidation_condition": "n/a", "reasoning": "thesis broke"}
    buy = {"alias": m.alias("DINO"), "action": "BUY", "quantity": 10, "entry_price": 100.0,
           "stop_price": 95.0, "target_price": 110.0, "confidence": 3, "primary_signal": "technical",
           "technical_basis": "rsi", "expected_reward_risk": 2.0,
           "invalidation_condition": "x", "reasoning": "x"}
    cap = _cycle(tmp_path, _ctx(tmp_path, FailingScorer()), portfolio=held, decisions=[sell, buy], masking=m)
    ev = cap["summary"]["selector"]
    assert ev["status"] == "failed_closed" and ev["selected"] == 0 and "unavailable" in ev["reason"]
    # No new-entry candidates; the held name is still fetched and shown for exit judgment.
    assert cap["tech_calls"] == [["OLN"]] and cap["news_calls"] == [["OLN"]]
    ip = cap["evidence"][0]["input_payload"]
    assert ip["candidates"] == [] and ip["positions"][0]["alias"] == m.alias("OLN")
    # The BUY is rejected by the selector boundary (not disguised as a technical failure); the SELL goes through.
    assert any("selector boundary rejected" in b for b in cap["summary"]["blocked"])
    assert [p["action"] for p in cap["summary"]["proposals"]] == ["SELL"]
    st = {s["alias"]: s for s in cap["evidence"][0]["candidate_status"]}
    assert st[m.alias("DINO")] == {"alias": m.alias("DINO"), "status": "selector_unavailable",
                                   "reason": "selector_failed_closed"}


def test_no_fetch_at_all_when_selection_is_empty_and_nothing_is_held(tmp_path):
    cap = _cycle(tmp_path, _ctx(tmp_path, FailingScorer()))
    assert cap["tech_calls"] == [] and cap["news_calls"] == []
    assert cap["evidence"][0]["input_payload"]["candidates"] == []


def test_a_replayed_cycle_reports_the_selection_it_was_decided_under(tmp_path):
    ctx = _ctx(tmp_path)
    cap = _cycle(tmp_path, ctx)
    cp = json.loads(cap["checkpoint_path"].read_text())
    assert lx.SelectorOutcome.from_record(cp["selector"]).selected == tuple(_selected(tmp_path))
    assert "selector" not in json.dumps(
        {k: v for k, v in cp.items() if k not in ("selector",)}), "selector info lives only in its own key"


# -- masking: lowercase leaks ---------------------------------------------------------

def test_lowercase_ticker_leak_is_detected_before_a_packet_reaches_hermes(tmp_path):
    probe = _ctx(tmp_path / "probe").select_for_day(DAY, set())
    leaky = probe.selected[0]
    seen = {}
    with pytest.raises(LeakDetected):
        _cycle(tmp_path / "run", _ctx(tmp_path / "run"), cap=seen,
               technicals={leaky: _tech(outlook=f"{leaky.lower()} guidance looks strong")})
    # The cycle died at the leak gate: Hermes was never asked, no prompt was built, nothing was queued.
    assert seen["prompts"] == [] and seen["queued"] == [] and seen["evidence"] == []


def test_opaque_masking_catches_lowercase_leaks_without_corrupting_ordinary_prose():
    m = _masking()
    prose = "Stop placed on support; it holds all week. All in all, a key day -- cf. the prior week."
    assert m.mask_text(prose) == prose
    m.assert_clean(prose)
    # a lowercase ticker (non-word-like) is masked in text and detected by assert_clean
    assert m.alias("OLN") in m.mask_text("shares of oln rose")
    with pytest.raises(LeakDetected):
        m.assert_clean({"note": "oln rose"})
    with pytest.raises(LeakDetected):
        m.assert_clean({"note": "OLN rose"})
    # declared word-like tickers stay case-sensitive so names/words are not corrupted
    assert m.mask_text("ryan said the dino exhibit opened; cf. page 4") == \
        "ryan said the dino exhibit opened; cf. page 4"
    assert m.alias("DINO") in m.mask_text("DINO rose")
    assert m.word_like_tickers == ("CF", "DINO", "RYAN")


def test_masked_rules_text_is_not_corrupted_by_the_added_names():
    rules = (Path(__file__).resolve().parent.parent / "RULES.md").read_text(encoding="utf-8")
    m = _masking()
    masked = m.mask_text(rules)
    m.assert_clean(masked)
    added = [x.symbol for x in UNIVERSE.members if x.group == "added"]
    assert not any(m.alias(t) in masked for t in added), "an added name corrupted the rules prose"


def test_one_stable_30_name_mapping_regardless_of_daily_selection():
    a, b = _masking(), _masking()
    assert a.mapping() == b.mapping() and len(set(a.mapping().values())) == 30
    assert set(a.mapping()) == set(SYMBOLS)


# -- private Laya worker boundary ----------------------------------------------------

_FAKE_WORKER = (
    "import sys, laya_adapter, laya_selector as ls, laya_worker\n"
    "class S:\n"
    "    metadata = laya_adapter.expected_metadata()\n"
    "    def score(self, cards):\n"
    "        sys.stderr.write('request cards=%d\\n' % len(cards)); sys.stderr.flush()\n"
    "        return [ls.ScoreRecord(c.row_id, 0.25 + 0.5 * c.model_state()['rsi14'] / 100, 0.9) for c in cards]\n"
    "laya_worker.serve(S(), sys.stdin, sys.stdout)\n"
)


def _cards(n=3):
    ids = ls.make_row_ids(SYMBOLS[:n], 1, "h")
    return [ls.build_card(rid, dict(five_day_return=0.01, sma20_distance=0.0, rsi14=40.0 + i,
                                    macd_direction=0.1, relative_volume=1.0, realized_volatility=0.2,
                                    range_position_20d=0.5)) for i, rid in enumerate(ids.values())]


def test_worker_scorer_loads_once_batches_every_request_and_answers_in_order(tmp_path):
    log = tmp_path / "w.log"
    s = lx.LayaWorkerScorer("unused", worker_cmd=[sys.executable, "-c", _FAKE_WORKER], log_path=log)
    try:
        first = s.score(_cards(3))
        second = s.score(_cards(2))
        assert [r.row_id for r in first] == [c.row_id for c in _cards(3)]
        assert first[0].review_probability == pytest.approx(0.25 + 0.5 * 0.40)
        assert len(second) == 2
        assert s._proc is not None and s._proc.poll() is None, "one long-lived worker, not one per call"
    finally:
        s.close()
    assert log.read_text().count("request cards=3") == 1 and "request cards=2" in log.read_text()
    assert s.score([]) == []


def test_worker_handshake_must_match_the_pinned_scorer(tmp_path):
    bad = _FAKE_WORKER.replace("laya_adapter.expected_metadata()",
                               "laya_adapter.expected_metadata().__class__('laya','9.9','x','r','q','s')")
    s = lx.LayaWorkerScorer("unused", worker_cmd=[sys.executable, "-c", bad])
    with pytest.raises(ls.ScorerError, match="handshake"):
        s.score(_cards(1))
    assert s._proc is None


def test_worker_death_and_timeout_fail_closed_and_a_later_call_restarts_it(tmp_path):
    die = _FAKE_WORKER.replace("return [ls.ScoreRecord", "import os; os._exit(3)\n        return [ls.ScoreRecord")
    s = lx.LayaWorkerScorer("unused", worker_cmd=[sys.executable, "-c", die])
    with pytest.raises(ls.ScorerError, match="exited"):
        s.score(_cards(1))
    ok = lx.LayaWorkerScorer("unused", worker_cmd=[sys.executable, "-c", _FAKE_WORKER])
    try:
        assert len(ok.score(_cards(1))) == 1
        ok.close()                                   # simulate a dead worker
        assert len(ok.score(_cards(1))) == 1         # restarted transparently
    finally:
        ok.close()
    slow = _FAKE_WORKER.replace("return [ls.ScoreRecord", "import time; time.sleep(30)\n        return [ls.ScoreRecord")
    s2 = lx.LayaWorkerScorer("unused", worker_cmd=[sys.executable, "-c", slow], request_timeout=1.0)
    with pytest.raises(ls.ScorerError, match="did not answer"):
        s2.score(_cards(1))


def test_worker_rejects_cards_with_forbidden_fields_and_reports_instead_of_dying():
    import io
    import laya_worker

    class S:
        metadata = laya_adapter.expected_metadata()
        def score(self, cards):
            return [ls.ScoreRecord(c.row_id, 0.5, 0.5) for c in cards]

    bad = {"id": 1, "cards": [{"row_id": ls.make_row_ids(["OLN"], 1, "h")["OLN"], "ticker": "OLN"}]}
    good = {"id": 2, "cards": [{"row_id": _cards(1)[0].row_id, **_cards(1)[0].model_state()}]}
    out = io.StringIO()
    laya_worker.serve(S(), io.StringIO(json.dumps(bad) + "\n" + json.dumps(good) + "\n"), out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0]["event"] == "ready"
    assert "ForbiddenCardField" in lines[1]["error"] and lines[1]["id"] == 1
    assert lines[2]["id"] == 2 and lines[2]["records"][0]["review_probability"] == 0.5


def test_docker_cli_environment_is_scrubbed(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setenv("HERMES_HOME", "/h")
    env = lx._docker_cli_env()
    assert not {"ALPACA_API_KEY", "DATABASE_URL", "HERMES_HOME"} & set(env)


def test_worker_defensive_socket_deny_covers_every_python_level_capability():
    import laya_worker
    missing = object()  # some methods (sendmsg, ...) do not exist on every platform
    saved_methods = {n: getattr(socket.socket, n, missing) for n in laya_worker._BLOCKED_SOCKET_METHODS}
    saved_funcs = {n: getattr(socket, n, missing) for n in laya_worker._BLOCKED_SOCKET_FUNCS}
    try:
        laya_worker._deny_network()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        calls = [lambda: s.connect(("127.0.0.1", 9)), lambda: s.connect_ex(("127.0.0.1", 9)),
                 lambda: s.bind(("127.0.0.1", 0)), lambda: s.listen(1), lambda: s.accept(),
                 lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9)),
                 lambda: socket.getaddrinfo("localhost", 80), lambda: socket.gethostbyname("localhost"),
                 lambda: socket.gethostbyname_ex("localhost"), lambda: socket.gethostbyaddr("127.0.0.1"),
                 lambda: socket.getnameinfo(("127.0.0.1", 80), 0),
                 lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
                 lambda: socket.create_server(("127.0.0.1", 0))]
        for call in calls:
            with pytest.raises(RuntimeError, match="network access is disabled"):
                call()
    finally:
        for owner, saved in ((socket.socket, saved_methods), (socket, saved_funcs)):
            for n, f in saved.items():
                if f is missing:
                    delattr(owner, n) if hasattr(owner, n) else None
                else:
                    setattr(owner, n, f)
        s.close()


# -- preflight --------------------------------------------------------------------------

def _price_history(start, end, symbols, drop=None):
    days = [start - timedelta(days=100) + timedelta(days=i)
            for i in range((end - (start - timedelta(days=100))).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    out = {}
    for s in symbols:
        out[s] = [{"date": d.isoformat(), "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 100.0}
                  for d in days if not (drop and s in drop and d == drop[s])]
    return out


def test_price_coverage_preflight_is_deterministic_and_per_symbol():
    start, end = date(2026, 2, 17), date(2026, 3, 31)
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    hist = _price_history(start, end, syms, drop={"BBB": date(2026, 3, 10)})
    hist["CCC"] = {"error": "yfinance down"}
    hist["DDD"] = hist["DDD"][-10:]
    r1 = lp.evaluate_price_coverage(hist, syms, start, end)
    assert r1 == lp.evaluate_price_coverage(json.loads(json.dumps(hist)), syms, start, end)
    assert r1["AAA"]["pass"] and r1["AAA"]["missing_days"] == 0
    assert not r1["BBB"]["pass"] and r1["BBB"]["reasons"] == ["missing_1_trading_days"]
    assert r1["CCC"] == {"pass": False, "bars_in_window": 0, "missing_days": None, "warmup_bars": 0,
                         "bad_bars": None, "reasons": ["history_fetch_error"]}
    assert not r1["DDD"]["pass"] and any(x.startswith("warmup_") for x in r1["DDD"]["reasons"])
    # a holiday (nobody traded) is not held against any symbol
    for s in ("AAA", "BBB"):
        hist[s] = [b for b in hist[s] if b["date"] != "2026-03-02"]
    assert lp.evaluate_price_coverage(hist, ["AAA", "BBB"], start, end)["AAA"]["missing_days"] == 0


def test_news_coverage_preflight_applies_the_existing_criterion():
    arts = lambda n: {"articles": [{"headline": "h"}] * n}
    probes = [{"A": arts(3), "B": arts(1), "C": {"error": "incomplete pagination"}},
              {"A": arts(3), "B": arts(1), "C": arts(9)},
              {"A": arts(0), "B": arts(1), "C": arts(9)}]
    r = lp.evaluate_news_coverage(probes, ["A", "B", "C"])
    assert r["A"] == {"pass": True, "counts": [3, 3, 0], "total": 6, "reasons": []}
    assert not r["B"]["pass"] and r["B"]["reasons"] == ["only_3_articles_need_6"]
    assert not r["C"]["pass"] and r["C"]["reasons"] == ["probe_1_unavailable"]  # unproven coverage fails


def test_news_probe_days_are_deterministic_and_inside_the_window():
    a = lp.news_probe_days(date(2026, 2, 17), date(2026, 8, 14))
    assert a == lp.news_probe_days(date(2026, 2, 17), date(2026, 8, 14))
    assert a[0] == date(2026, 3, 3) and a[2] == date(2026, 8, 14) and a[0] < a[1] < a[2]


def test_preflight_report_and_exit_codes(capsys):
    start, end = date(2026, 2, 17), date(2026, 3, 31)
    hist = _price_history(start, end, SYMBOLS)
    hist[SYMBOLS[1]] = {"error": "down"}
    arts = {"articles": [{"headline": "h"}] * 2}
    probes = [{s: arts for s in SYMBOLS}] * 3
    price = lp.evaluate_price_coverage(hist, SYMBOLS, start, end)
    news = lp.evaluate_news_coverage(probes, SYMBOLS)
    report, ok = lp.build_report(UNIVERSE, price, news, start, end)
    assert not ok and f"FAIL: {SYMBOLS[1]}" in report and report.count("\n") >= 32
    assert all(s in report for s in SYMBOLS)
    assert report == lp.build_report(UNIVERSE, price, news, start, end)[0]

    # Infrastructure down -> exit 2, nothing verified, nothing mutated.
    with patch("run_backtest.run_in", side_effect=RuntimeError("docker engine is not running")):
        assert lp.main(["--start", "2026-02-17", "--end", "2026-03-31"]) == 2
    assert "nothing was verified" in capsys.readouterr().err

    # Healthy mocks -> exit 0 and a PASS line.
    full_hist = _price_history(start, end, SYMBOLS)
    ok_news = {s: {"articles": [{"headline": "h"}] * 3} for s in SYMBOLS}
    with patch("run_backtest.run_in", return_value=json.dumps({"history": full_hist})), \
         patch("run_backtest.fetch_news_window", return_value=ok_news):
        assert lp.main(["--start", "2026-02-17", "--end", "2026-03-31"]) == 0
    assert "PASS: all 30 symbols" in capsys.readouterr().out
