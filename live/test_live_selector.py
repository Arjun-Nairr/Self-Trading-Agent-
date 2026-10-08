"""Live Laya delivery against the REAL selector machinery (SelectorContext,
select_pool, immutable snapshots) and the REAL screener's records, with a fake
scorer and fake price history. No Docker, network, model or broker."""
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "backtest-harness"))
sys.path.insert(0, str(HERE.parent / "watchlist"))
import scalable_screener as ss  # noqa: E402
from laya_selector import MAX_SELECTED, ScoreRecord, ScorerMetadata, select_pool  # noqa: E402
from selector import (LIVE_SHORTLIST_MAX, LiveSelector, UniverseUnavailable, live_universe)  # noqa: E402
from test_live import WL, FakeBroker, FakeJournal, _cycle, plan  # noqa: E402

SAT = date(2026, 10, 3)                      # screen recorded on a Saturday, effective Monday 10-05
THU, SESSION = date(2026, 10, 8), date(2026, 10, 7)


def symbols(n):
    return [f"S{i:03d}" for i in range(n)]


class ScreenProviders:
    live = False

    def __init__(self, syms):
        self.syms = syms

    def universe(self):
        return {"source": "fake", "url": "fake://u", "symbols": self.syms}

    def facts(self, syms):
        return {"rows": {s: {"name": f"Company {s}", "sector": "Industrials", "industry": "Machinery",
                             "market_cap": 5e9, "avg_volume": 3e6, "price": 40.0} for s in syms}, "stats": None}

    def history(self, syms):
        return {"rows": {s: {"bars": 1200} for s in syms}, "stats": None}

    def news(self, syms, windows):
        return {"rows": {s: [{"articles": 3}] * len(windows) for s in syms}, "stats": None}


def record_screen(tmp_path, n):
    ticks = [datetime(2026, 10, 3, 12, tzinfo=timezone.utc)]

    def clock():
        ticks[0] += timedelta(seconds=1)
        return ticks[0]
    snap, _ = ss.screen(SAT, ScreenProviders(symbols(n)), tmp_path / "screens", clock=clock)
    assert snap["screen_status"] == "ok" and len(snap["pool"]) == n
    return tmp_path / "screens"


def history(tickers, day):
    """Deterministic bars ending on `day`; a higher ticker index rises faster."""
    out = {}
    for t in tickers:
        slope = int(t[1:]) * 0.0003
        bars = []
        for k in range(60):
            d = day - timedelta(days=59 - k)
            c = 50 * (1 + slope * k) * (1 + 0.004 * math.sin(k * 0.7))
            bars.append({"date": d.isoformat(), "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": 1e6 * (1 + 0.1 * math.sin(k))})
        out[t] = bars
    return out


class FakeScorer:
    metadata = ScorerMetadata("fake", "0", "fake-checkpoint", None, "q")

    def __init__(self):
        self.calls, self.seen, self.fail = 0, [], False

    def score(self, cards):
        self.calls += 1
        if self.fail:
            raise RuntimeError("laya worker down")
        self.seen = [c.row_id for c in cards]
        return [ScoreRecord(c.row_id, 1 / (1 + math.exp(-60 * c.five_day_return)), 0.5) for c in cards]


def selector(tmp_path, n=30, scorer=None, fetch=history):
    screens = record_screen(tmp_path, n)
    scorer = scorer or FakeScorer()
    return LiveSelector(tmp_path / "state", screen_dir=screens, fetch_history=fetch, scorer=scorer), scorer


def test_a_population_larger_than_twenty_yields_a_valid_shortlist_of_twenty(tmp_path):
    sel, scorer = selector(tmp_path, 30)
    picks, status, event = sel(THU, SESSION, set())
    assert [p["ticker"] for p in picks] == sorted(symbols(30)[10:]), "the 20 best-ranked, listed alphabetically"
    assert len(picks) == LIVE_SHORTLIST_MAX == 20 and status.startswith("ok (screened eligible universe (current)")
    assert all(set(p) == {"ticker", "sector", "name"} for p in picks), "no scores or ranks reach Hermes"
    assert event["universe"] == symbols(30) and scorer.calls == 1 and len(scorer.seen) == 30
    assert not any(s in r for s in symbols(30) for r in scorer.seen), "cards are identity-free (opaque row ids)"


def test_a_small_population_is_never_padded(tmp_path):
    sel, _ = selector(tmp_path, 12)
    picks, status, _ = sel(THU, SESSION, set())
    assert len(picks) == 12 and status.startswith("ok")


def test_rotation_keeps_held_names_out_and_fills_from_the_next_ranks_without_rescoring(tmp_path):
    sel, scorer = selector(tmp_path, 30)
    first, _, _ = sel(THU, SESSION, set())
    held = {"S029", "S028", "S005"}
    second, status, _ = sel(THU, SESSION, held)                  # holdings changed intraday: must not fail closed
    assert status.startswith("ok") and not held & {p["ticker"] for p in second}
    assert [p["ticker"] for p in second] == sorted(symbols(30)[8:28]), "next-ranked names fill the freed slots"
    assert scorer.calls == 1, "the day's snapshot is replayed, not rebuilt"
    sel(THU + timedelta(days=1), THU, set())
    assert scorer.calls == 2, "a new completed session refreshes the selection"


def test_selector_failure_yields_an_empty_shortlist_with_a_specific_status(tmp_path):
    scorer = FakeScorer()
    scorer.fail = True
    sel, _ = selector(tmp_path, 30, scorer=scorer)
    picks, status, _ = sel(THU, SESSION, set())
    assert picks == [] and status.startswith("unavailable:") and "laya worker down" in status

    def boom(tickers, day):
        raise RuntimeError("maverick down")
    sel2, _ = selector(tmp_path / "b", 30, fetch=boom)
    picks, status, _ = sel2(THU, SESSION, set())
    assert picks == [] and "maverick down" in status


def test_no_usable_screen_is_a_specific_blocker_not_a_silent_fallback(tmp_path):
    sel = LiveSelector(tmp_path / "state", screen_dir=tmp_path / "none", fetch_history=history, scorer=FakeScorer())
    with pytest.raises(UniverseUnavailable, match="no_successful_screen.*scalable_screener.py --write"):
        sel(THU, SESSION, set())
    screens = record_screen(tmp_path, 15)
    with pytest.raises(UniverseUnavailable, match="expired"):
        live_universe(THU + timedelta(days=30), screen_dir=screens)


def test_the_frozen_ten_names_are_used_only_when_explicitly_chosen(tmp_path):
    u, label = live_universe(THU, source="watchlist")
    assert len(u.members) == 10 and "explicit" in label
    assert [m.symbol for m in u.members] == WL


def test_a_missing_universe_blocks_entries_only_and_names_the_blocker(tmp_path):
    from run_live import run_cycle
    b, j = FakeBroker(), FakeJournal()
    from engine import Store
    store = Store(tmp_path / "s")
    sel = LiveSelector(tmp_path / "state", screen_dir=tmp_path / "none", fetch_history=history, scorer=FakeScorer())
    seen = []

    def hermes(prompt):
        seen.append(prompt)
        return '{"holdings": [], "entries": [], "no_entry_reason": "no shortlist"}', "sha"
    out = run_cycle(store, b, j, review=True, selector_fn=sel, hermes_fn=hermes, sleep=lambda s: None)
    assert out["today"] == "2026-10-08" and out["reconcile"]["protected"] == []
    assert "no_successful_screen" in seen[0] and '"shortlist": []' in seen[0]


def test_live_validation_agrees_with_the_universe_snapshot(tmp_path):
    from engine import Store, enter
    sel, _ = selector(tmp_path, 30)
    picks, _, event = sel(THU, SESSION, set())
    names = [p["ticker"] for p in picks]
    store, b, j = Store(tmp_path / "s"), FakeBroker(), FakeJournal()
    ok = enter(store, b, j, plan(ticker="S029"), shortlist=names, universe=event["universe"],
               decided_at="2026-10-08T14:59:00Z", today="2026-10-08", sleep=lambda s: None)
    assert ok.ok, ok.reasons                                      # on the screened universe, not on the frozen ten
    out = enter(store, b, j, plan(ticker="S020"), shortlist=names, universe=[u for u in event["universe"] if u != "S020"],
                decided_at="2026-10-08T14:59:00Z", today="2026-10-08", sleep=lambda s: None)
    assert not out.ok and any("watchlist" in r.lower() or "§1" in r or "universe" in r.lower() for r in out.reasons)


def test_historical_selector_defaults_are_unchanged():
    from laya_experiment import SelectorContext
    assert MAX_SELECTED == 10
    import inspect
    assert inspect.signature(SelectorContext.__init__).parameters["k"].default == MAX_SELECTED
    assert inspect.signature(select_pool).parameters["k"].default == MAX_SELECTED
    cards, scores = [], {}
    with pytest.raises(ValueError, match="1..10"):
        select_pool(cards, scores, as_of=THU, seed=0, k=11)
