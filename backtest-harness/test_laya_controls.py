"""Tests for laya_controls.py at its public seam: controls, measurements and the
run() pipeline with a fake history source and a deterministic fake scorer (no
Docker, no model, no network)."""
import json
import re
from datetime import date, timedelta

import pytest

import laya_controls as lc
from laya_selector import NUMERIC_FIELDS, ScoreRecord, ScorerMetadata, build_card, make_row_ids

DAY = date(2026, 9, 28)


def _cards(n=30):
    ids = make_row_ids([f"S{i}" for i in range(n)], 1, "u")
    return [build_card(ids[f"S{i}"], {"five_day_return": (i - n / 2) / 100, "sma20_distance": 0.0, "rsi14": 50.0,
                                      "macd_direction": 0.0, "relative_volume": 1.0 + i / 100,
                                      "realized_volatility": 0.3, "range_position_20d": 0.5}) for i in range(n)]


def test_random_controls_are_fixed_per_seed_and_independent_across_days():
    ids = [c.row_id for c in _cards()]
    a, b = lc.random_shortlists(ids, DAY), lc.random_shortlists(list(reversed(ids)), DAY)
    assert a == b, "input order must not matter"
    assert set(a) == set(range(20)) and all(len(v) == 10 and len(set(v)) == 10 for v in a.values())
    other = lc.random_shortlists(ids, DAY + timedelta(days=1))
    assert sum(a[s] == other[s] for s in a) == 0, "a seed must draw independently on another day"


def test_deterministic_control_is_largest_absolute_five_day_move():
    cards = _cards()
    picked = lc.deterministic_shortlist(cards)
    moves = sorted((abs(c.five_day_return) for c in cards), reverse=True)
    assert [abs(next(c for c in cards if c.row_id == i).five_day_return) for i in picked] == moves[:10]
    assert picked == lc.deterministic_shortlist(list(reversed(cards))), "deterministic regardless of order"


def test_laya_shortlist_uses_the_production_ranking_and_ignores_confidence():
    cards = _cards(12)
    scores = {c.row_id: ScoreRecord(c.row_id, 0.2 + i / 1000, 1.0 - i / 100) for i, c in enumerate(cards)}
    assert lc.laya_shortlist(cards, scores, DAY) == [c.row_id for c in reversed(cards)][:10]


def test_measurements():
    assert lc.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert lc.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert lc.hypergeom_tail(51, 10, 10, 0) == 1.0
    assert abs(sum(lc.hypergeom_tail(51, 10, 10, x) - lc.hypergeom_tail(51, 10, 10, x + 1) for x in range(11)) - 1) < 1e-6
    s = lc.score_stats([0.3, 0.2, 0.2, 0.25, 0.1, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17])
    assert s["rows_sharing_a_score"] == 2 and s["distinct"] == 11 and s["rank10_vs_11_gap"] == round(0.12 - 0.11, 4)
    quiet = build_card("r_000000000001", {**lc.SENS_BASE})
    loud = build_card("r_000000000002", {**lc.SENS_ALL_NOTABLE})
    assert lc.notable_flags(quiet) == 0 and lc.notable_flags(loud) == 6


def test_sensitivity_cards_follow_the_plan():
    lab = lc.sensitivity_cards()
    assert len(lab) == 17 and len({c.row_id for _, c in lab}) == 17
    by = dict(lab)
    for f, (lo, hi) in lc.SENS_VARIANTS.items():
        for tag, v in (("low", lo), ("high", hi)):
            card = by[f"{f}={tag}"]
            assert getattr(card, f) == v
            assert all(getattr(card, g) == lc.SENS_BASE[g] for g in NUMERIC_FIELDS if g != f), "one field at a time"
    assert by["base_duplicate"].model_state() == by["base"].model_state()


class FakeScorer:
    """Deterministic stand-in: P = 0.2 + 0.01 * (number of notable flags)."""
    metadata = ScorerMetadata("fake", None, "fake", None, "q")

    def __init__(self):
        self.started = 0
        self.batches = []

    def _start(self):
        self.started += 1

    def score(self, cards):
        self.batches.append(len(cards))
        return [ScoreRecord(c.row_id, round(0.2 + 0.01 * lc.notable_flags(c), 4), 0.5) for c in cards]


def _history(symbols, day):
    out = {}
    for j, s in enumerate(symbols):
        if s == symbols[0]:
            out[s] = {"error": "boom"}                      # one unavailable card, every day
            continue
        bars, d, px = [], day - timedelta(days=90), 50.0 + j
        while d <= day + timedelta(days=3):                 # bars after the session must be dropped
            if d.weekday() < 5:
                px *= 1 + (((j * 7 + d.toordinal()) % 11) - 5) / 400
                bars.append({"date": d.isoformat(), "open": px, "high": px * 1.01, "low": px * 0.99,
                             "close": px, "volume": 1e6 + (d.toordinal() % 7) * 1e5 + j})
            d += timedelta(days=1)
        out[s] = bars
    return out


def test_run_pipeline_is_fair_private_and_write_once(tmp_path):
    fake = FakeScorer()
    out = lc.run(fetch_history=_history, scorer=fake, scorer_runtime={"image": "fake"}, out_dir=tmp_path, smoke_dir=None)
    assert fake.started == 1, "model loaded once"
    assert fake.batches == [50, 50, 50, 17], "one batch per session, in order, then the synthetic batch"
    pub = json.loads((out / "public_summary.json").read_text(encoding="utf-8"))
    priv = json.loads((out / "private_results.json").read_text(encoding="utf-8"))
    assert [d["session"] for d in pub["days"]] == [s.isoformat() for s in lc.SESSIONS]
    for d, p in zip(pub["days"], priv["days"]):
        assert d["cards_total"] == 51 and d["complete_cards"] == 50 and d["unavailable"] == 1
        assert d["unavailable_reasons"] == {"history_fetch_error": 1}
        complete = {c["row_id"] for c in p["cards"] if c["data_complete"]}
        sl = p["shortlists"]
        for picked in [sl["laya"], sl["deterministic"], *sl["random"].values()]:
            assert len(picked) == 10 and set(picked) <= complete, "every method draws from the same complete set"
        assert set(p["scores"]) == complete
    text = (out / "public_summary.json").read_text(encoding="utf-8")
    assert not re.search(r"r_[0-9a-f]{12}", text), "the public summary carries no row IDs"
    assert not any(s in text for s in json.loads((out / "private_mapping.json").read_text())["tickers"].values() if len(s) > 2)
    assert pub["sensitivity"]["duplicate_identical"] is True
    assert pub["plan_sha256"] == lc._sha_file(lc.PLAN_PATH)
    with pytest.raises(FileExistsError):
        from laya_demo import _write_once
        _write_once(out / "public_summary.json", "x")


def test_smoke_check_detects_changed_inputs():
    cards = _cards(5)
    latest = {c.row_id: DAY for c in cards}
    scores = {c.row_id: ScoreRecord(c.row_id, 0.2, 0.5) for c in cards}
    from laya_selector import inputs_digest
    snap = {"inputs_digest": inputs_digest(cards[:4], {c.row_id: DAY for c in cards[:4]}),
            "rows": [{"row_id": c.row_id, "review_probability": 0.2} for c in cards[:4]]}
    ok = lc.check_against_smoke(snap, cards, latest, scores)
    assert ok["inputs_digest_match"] and ok["scores_identical"] == 4
    bad = dict(snap, inputs_digest="0" * 64)
    assert not lc.check_against_smoke(bad, cards, latest, scores)["inputs_digest_match"]
