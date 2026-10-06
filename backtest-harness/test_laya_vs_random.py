"""Laya-vs-random experiment: the random control selector, neutral candidate
order, arm guards, fact recording and config refusals. No Docker, Neon,
Hermes or model weights (run_day drives through test_laya_wiring's mocks)."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import laya_experiment as lx
import laya_selector as ls
import laya_vs_random as lvr
from test_laya_wiring import DAY, SEED, SYMBOLS, UNIVERSE, _bars, _ctx, _cycle, _history

EXP_SEED = "laya-vs-random-test"


def _rctx(tmp, run_id="run-r", fetch=None, seed=EXP_SEED):
    calls = []

    def fetch_history(tickers, day):
        calls.append((list(tickers), day))
        return (fetch or _history)(tickers, day)

    ctx = lx.RandomSelectorContext(experiment_seed=seed, universe=UNIVERSE, seed=SEED, run_id=run_id,
                                   snapshot_dir=Path(tmp), fetch_history=fetch_history)
    ctx.fetch_calls = calls
    return ctx


def _cards(n, stale=(), incomplete=()):
    ids = ls.make_row_ids([f"S{i}" for i in range(n)], SEED, "h")
    cards, latest = [], {}
    for i, rid in enumerate(ids.values()):
        vals = {} if i in incomplete else {"five_day_return": 0.01, "sma20_distance": 0.0, "rsi14": 50.0,
                                            "macd_direction": 0.0, "relative_volume": 1.0,
                                            "realized_volatility": 0.2, "range_position_20d": 0.5}
        cards.append(ls.build_card(rid, vals))
        latest[rid] = DAY - timedelta(days=3) if i in stale else DAY
    return cards, latest


def test_draw_seed_is_derived_from_the_experiment_seed_and_session_only():
    a = lx.random_draw_seed(EXP_SEED, DAY)
    assert a == lx.random_draw_seed(EXP_SEED, DAY)
    assert a != lx.random_draw_seed(EXP_SEED, DAY + timedelta(days=1)) != lx.random_draw_seed("other", DAY)


def test_random_draw_is_uniform_without_replacement_over_available_cards_only():
    cards, latest = _cards(30, stale={1, 2}, incomplete={3})
    rows = lx.select_random(cards, as_of=DAY, seed=SEED, draw_seed=7, latest_bar_dates=latest)
    by = {r.row_id: r for r in rows}
    sel = [r for r in rows if r.status == ls.STATUS_SELECTED]
    assert len(sel) == 10 and len({r.row_id for r in sel}) == 10 and [r.rank for r in sel] == list(range(1, 11))
    for i in (1, 2, 3):
        assert by[cards[i].row_id].status == ls.STATUS_DATA_UNAVAILABLE
    # availability is exactly select_pool's rule
    base = ls.select_pool(cards, {}, as_of=DAY, seed=SEED, latest_bar_dates=latest)
    assert {r.row_id for r in rows if r.status == ls.STATUS_DATA_UNAVAILABLE} == {
        r.row_id for r in base if r.reason != lx._NO_SCORE_REASON}
    assert all(r.review_probability is None and r.answer_confidence is None for r in rows), "no invented scores"
    assert rows == lx.select_random(cards, as_of=DAY, seed=SEED, draw_seed=7, latest_bar_dates=latest)
    assert {r.row_id for r in sel} != {r.row_id for r in lx.select_random(
        cards, as_of=DAY, seed=SEED, draw_seed=8, latest_bar_dates=latest) if r.status == ls.STATUS_SELECTED}
    few, few_latest = _cards(6, incomplete={0})
    picked = [r for r in lx.select_random(few, as_of=DAY, seed=SEED, draw_seed=1, latest_bar_dates=few_latest)
              if r.status == ls.STATUS_SELECTED]
    assert len(picked) == 5, "fewer than 10 available -> all of them, never padded"


def test_random_draw_frequencies_are_roughly_uniform():
    cards, latest = _cards(20)
    counts = {c.row_id: 0 for c in cards}
    for s in range(2000):
        for r in lx.select_random(cards, as_of=DAY, seed=SEED, draw_seed=s, latest_bar_dates=latest):
            counts[r.row_id] += r.status == ls.STATUS_SELECTED
    assert all(850 < n < 1150 for n in counts.values()), counts    # expectation 1000


def test_random_context_excludes_held_persists_and_replays_with_explicit_metadata(tmp_path):
    ctx = _rctx(tmp_path)
    held = {"OLN", "NUE"}
    out = ctx.select_for_day(DAY, held)
    assert out.ok and len(out.selected) == 10 and not held & set(out.selected)
    assert out.event["mode"] == lx.SELECTOR_MODE_RANDOM_CONTROL
    snap = ls.load_snapshot(tmp_path, "run-r", DAY.isoformat())
    assert snap.scorer.provider == "random_control" and EXP_SEED in snap.scorer.settings
    assert all(r.review_probability is None for r in snap.rows)
    assert str(lx.random_draw_seed(EXP_SEED, DAY)) in next(r.reason for r in snap.rows if r.status == "selected")
    again = ctx.select_for_day(DAY, held)
    assert again.selected == out.selected and again.event["replayed"] and len(ctx.fetch_calls) == 1
    other = _rctx(tmp_path, seed="different-seed").select_for_day(DAY, held)
    assert not other.ok and "scorer_metadata" in other.event["reason"], "a foreign snapshot never replays"


def test_random_context_marks_incomplete_cards_unavailable(tmp_path):
    def partial(tickers, day):
        h = _history(tickers, day)
        h[tickers[0]] = _bars(0.001, end=day, n=10)               # too short -> incomplete card
        return h
    out = _rctx(tmp_path, fetch=partial).select_for_day(DAY, set())
    assert out.ok and out.status_by_ticker[SYMBOLS[0]][0] == ls.STATUS_DATA_UNAVAILABLE
    assert SYMBOLS[0] not in out.selected


@pytest.mark.parametrize("make", [lambda t: _ctx(t), lambda t: _rctx(t, run_id="run-t")])
def test_hermes_sees_candidates_in_neutral_alias_order_for_both_arms(tmp_path, make):
    cap = _cycle(tmp_path, make(tmp_path))
    aliases = [c["alias"] for c in cap["evidence"][0]["input_payload"]["candidates"]]
    assert aliases == sorted(aliases) and len(aliases) == 10
    snap = ls.load_snapshot(tmp_path, "run-t", DAY.isoformat())
    assert all(t not in cap["prompts"][0] for t in ("random_control", "draw seed", "review_probability"))
    assert all(r.row_id not in cap["prompts"][0] for r in snap.rows)


# ---------------------------------------------------------------- arm guards and recording
CFG = {"experiment_id": "exp-t", "arms": ["laya", "random"]}
FP = {"head": "abc", "files": {"x": "1"}}


def _mark(root, arm, **body):
    d = lvr._arm_dir(CFG, arm, root)
    d.mkdir(parents=True)
    (d / "ARM.json").write_text(json.dumps(body))


def test_arm_order_single_attempt_and_identical_code_are_enforced(tmp_path):
    lvr.check_arm_allowed(CFG, "laya", FP, tmp_path)
    with pytest.raises(SystemExit):
        lvr.check_arm_allowed(CFG, "random", FP, tmp_path)               # laya not run yet
    _mark(tmp_path, "laya", status="halted", fingerprint=FP)
    with pytest.raises(SystemExit):
        lvr.check_arm_allowed(CFG, "laya", FP, tmp_path)                 # never twice
    with pytest.raises(SystemExit):
        lvr.check_arm_allowed(CFG, "random", FP, tmp_path)               # laya halted -> stop
    (lvr._arm_dir(CFG, "laya", tmp_path) / "ARM.json").write_text(json.dumps({"status": "completed",
                                                                              "fingerprint": FP}))
    with pytest.raises(SystemExit):
        lvr.check_arm_allowed(CFG, "random", {"head": "changed", "files": {}}, tmp_path)
    lvr.check_arm_allowed(CFG, "random", FP, tmp_path)


def test_recording_returns_results_unchanged_and_compare_finds_discrepancies(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.touch(), b.touch()
    fa = lvr.recording(lambda ts, day: {t: {"p": 1} for t in ts}, "technicals", a)
    fb = lvr.recording(lambda ts, day: {t: {"p": 2 if t == "Y" else 1} for t in ts}, "technicals", b)
    assert fa(["X", "Y"], DAY) == {"X": {"p": 1}, "Y": {"p": 1}}
    fb(["X", "Y", "Z"], DAY)
    cmp = lvr.compare_facts(a, b)
    assert cmp["shared"] == 2 and cmp["differing"] == 1
    assert cmp["differing_keys"] == [["technicals", "Y", [DAY.isoformat()]]]


def test_both_arms_share_one_masking_map():
    from run_backtest import _experimental_watchlist_entries
    wl = _experimental_watchlist_entries(UNIVERSE)
    cfg = {"window": {"start": "2022-07-01"}, "masking_seed": 42}
    assert lvr.masking_for(UNIVERSE, wl, cfg).mapping() == lvr.masking_for(UNIVERSE, wl, cfg).mapping()


def test_config_must_be_authorized_and_pinned_to_the_plan(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    plan.write_text("frozen plan")
    monkeypatch.setattr(lvr, "REPO", tmp_path)
    good = {"status": "authorized", "arms": ["laya", "random"],
            "plan": {"path": "plan.md", "sha256": lvr._sha_file(plan)}}
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(good))
    assert lvr.load_config(p)["status"] == "authorized"
    for bad in ({**good, "status": "proposed"}, {**good, "arms": ["random", "laya"]},
                {**good, "plan": {"path": "plan.md", "sha256": "0" * 64}}):
        p.write_text(json.dumps(bad))
        with pytest.raises(SystemExit):
            lvr.load_config(p)
