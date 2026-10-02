"""Phase 1 Laya-selector foundation: universe config, identity-free card,
deterministic selection, and snapshot persistence. No model weights, no
network, no Docker, no Neon -- the scorer here is a deterministic fake that
implements the CardScorer interface. Run: python -m pytest -q test_laya_selector.py
"""
import dataclasses
import json
import os
import re
from datetime import date
from pathlib import Path

import pytest

import laya_selector as ls
from laya_selector import (
    CARD_FIELDS, NUMERIC_FIELDS, CandidateCard, ForbiddenCardField, InvalidCardValue,
    ScoreRecord, ScorerMetadata, SnapshotConflict, build_card, build_snapshot,
    load_snapshot, load_universe, make_row_ids, select_pool, snapshot_from_json,
    snapshot_to_json, write_snapshot,
)

AS_OF = date(2024, 3, 15)
SEED = 20240315
FAKE_META = ScorerMetadata("fake", "0", "fake-deterministic", None, "test question")


def _prepared(**over):
    base = dict(five_day_return=0.02, sma20_distance=0.01, rsi14=55.0, macd_direction=0.3,
                relative_volume=1.1, realized_volatility=0.25, range_position_20d=0.6)
    base.update(over)
    return base


def _cards(n, universe=None, **over):
    universe = universe or load_universe()
    syms = list(universe.symbols)[:n] if n <= 30 else [f"S{i}" for i in range(n)]
    ids = make_row_ids(syms, SEED, universe.hash)
    return [build_card(ids[s], _prepared(**over)) for s in syms]


class FakeScorer:
    """Deterministic stand-in for the real adapter: probability is a pure
    function of the card's numbers. Implements the CardScorer interface."""
    metadata = FAKE_META

    def score(self, cards):
        out = []
        for c in cards:
            s = c.model_state()
            p = min(1.0, max(0.0, 0.5 + s["five_day_return"] * 5 + (s["rsi14"] - 50) / 200))
            out.append(ScoreRecord(c.row_id, p, 0.9))
        return out


# -- 1. fixed-30 universe ----------------------------------------------------

def test_universe_is_30_unique_and_experimental():
    u = load_universe()
    assert len(u.members) == 30 and len(set(u.symbols)) == 30
    assert u.version == "fixed30-v1"
    assert sum(m.group == "baseline" for m in u.members) == 10
    assert sum(m.group == "added" for m in u.members) == 20


def test_universe_keeps_baseline_10_exactly_and_never_mutates_the_baseline_watchlist():
    base = json.loads(ls.BASELINE_WATCHLIST_PATH.read_text(encoding="utf-8"))["tickers"]
    u = load_universe()
    in_u = {m.symbol: (m.sector, m.name) for m in u.members if m.group == "baseline"}
    assert in_u == {t["symbol"]: (t["sector"], t["name"]) for t in base}
    assert len(base) == 10, "the baseline watchlist must stay exactly the fixed 10"
    assert ls.UNIVERSE_PATH != ls.BASELINE_WATCHLIST_PATH
    # No added symbol may shadow a baseline one.
    assert not {m.symbol for m in u.members if m.group == "added"} & set(in_u)


def test_universe_is_sector_balanced_3_per_sector():
    u = load_universe()
    by_sector = {}
    for m in u.members:
        by_sector.setdefault(m.sector, []).append(m.symbol)
    assert len(by_sector) == 10
    assert {len(v) for v in by_sector.values()} == {3}


def test_universe_hash_is_stable_and_sensitive_to_content(tmp_path):
    u = load_universe()
    assert u.hash == load_universe().hash
    raw = json.loads(ls.UNIVERSE_PATH.read_text(encoding="utf-8"))
    raw["tickers"][0]["name"] = "Olin Corp"
    p = tmp_path / "u.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert load_universe(p).hash != u.hash


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r.update(experimental=False), "experimental"),
    (lambda r: r["tickers"].append(dict(r["tickers"][0])), "duplicate|expected_count"),
    (lambda r: r.update(expected_count=29), "expected_count"),
    (lambda r: r["tickers"][0].update(group="other"), "unknown group"),
])
def test_universe_loader_fails_loudly_on_bad_config(tmp_path, mutate, msg):
    raw = json.loads(ls.UNIVERSE_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "u.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=msg):
        load_universe(p)


def test_baseline_loaders_do_not_reference_the_experimental_file():
    """Default/baseline path isolation: no non-test source file outside the new
    modules mentions the experimental universe or the selector."""
    repo = ls.REPO
    offenders = []
    for path in list(repo.glob("*/*.py")) + list(repo.glob("*.py")):
        if "maverick-mcp" in str(path) or path.name.startswith(("test_", "laya_")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "experimental_fixed30" in text or "laya_selector" in text or "laya_adapter" in text:
            offenders.append(str(path.relative_to(repo)))
    assert offenders == [], f"baseline code must not import/reference the selector: {offenders}"


# -- 2. card contract --------------------------------------------------------

def test_card_has_exactly_the_contract_fields_and_no_text_field():
    assert tuple(f.name for f in dataclasses.fields(CandidateCard)) == CARD_FIELDS
    assert CARD_FIELDS == ("row_id", "five_day_return", "sma20_distance", "rsi14",
                           "macd_direction", "relative_volume", "realized_volatility",
                           "range_position_20d", "data_complete")
    # Only the row ID is a string; everything else is numeric/bool, so there
    # is no slot a ticker/name/headline/reasoning could occupy.
    types = {f.name: f.type for f in dataclasses.fields(CandidateCard)}
    assert [n for n, t in types.items() if "str" in str(t)] == ["row_id"]


def test_card_preserves_the_numeric_fields():
    rid = next(iter(make_row_ids(["X"], SEED, "h").values()))
    card = build_card(rid, _prepared())
    assert card.data_complete is True
    assert card.model_state() == _prepared()
    assert tuple(card.model_state()) == NUMERIC_FIELDS
    assert "row_id" not in card.model_state()


@pytest.mark.parametrize("bad", [
    "ticker", "symbol", "name", "company_name", "date", "as_of", "headline", "news",
    "fundamentals", "eps", "reflection", "trade_outcome", "pnl", "reasoning",
    "free_text", "sector", "data_complete", "price",
])
def test_card_rejects_every_forbidden_field(bad):
    rid = next(iter(make_row_ids(["X"], SEED, "h").values()))
    with pytest.raises(ForbiddenCardField, match=bad):
        build_card(rid, {**_prepared(), bad: "AAPL"})


@pytest.mark.parametrize("row_id", ["AAPL", "OLN", "Olin Corporation", "2024-03-15", "r_xyz", "r_" + "a" * 11, ""])
def test_card_rejects_non_opaque_row_ids(row_id):
    with pytest.raises(ForbiddenCardField, match="opaque"):
        build_card(row_id, _prepared())


@pytest.mark.parametrize("field,value", [
    ("rsi14", "55"), ("rsi14", True), ("rsi14", 101.0), ("rsi14", -1.0),
    ("macd_direction", 1.5), ("range_position_20d", 1.2), ("relative_volume", -0.1),
    ("realized_volatility", -0.01), ("five_day_return", [0.1]),
])
def test_card_rejects_non_numeric_or_out_of_range_values(field, value):
    rid = next(iter(make_row_ids(["X"], SEED, "h").values()))
    with pytest.raises(InvalidCardValue):
        build_card(rid, _prepared(**{field: value}))


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf")])
def test_missing_or_non_finite_input_clears_the_whole_card(missing):
    rid = next(iter(make_row_ids(["X"], SEED, "h").values()))
    card = build_card(rid, _prepared(rsi14=missing))
    assert card.data_complete is False
    assert all(getattr(card, n) is None for n in NUMERIC_FIELDS)
    # An absent key is also "missing", not an error.
    p = _prepared()
    del p["sma20_distance"]
    assert build_card(rid, p).data_complete is False


def test_row_ids_are_opaque_deterministic_and_seeded():
    u = load_universe()
    ids = make_row_ids(u.symbols, SEED, u.hash)
    assert ids == make_row_ids(u.symbols, SEED, u.hash)
    assert len(set(ids.values())) == 30
    assert ids != make_row_ids(u.symbols, SEED + 1, u.hash)
    for sym, rid in ids.items():
        assert re.fullmatch(r"r_[0-9a-f]{12}", rid)
        assert sym.lower() not in rid[2:]  # hex alphabet; symbols contain non-hex letters or are too short to matter


# -- 3/4. deterministic selection --------------------------------------------

def _scores(cards, probs, conf=0.9):
    return {c.row_id: ScoreRecord(c.row_id, p, conf) for c, p in zip(cards, probs)}


def test_ranks_by_descending_review_probability_and_selects_top_10_of_30():
    cards = _cards(30)
    probs = [i / 100 for i in range(30)]  # distinct, ascending with index
    rows = select_pool(cards, _scores(cards, probs), as_of=AS_OF, seed=SEED)
    assert len(rows) == 30
    selected = [r for r in rows if r.status == ls.STATUS_SELECTED]
    assert len(selected) == 10
    assert [r.rank for r in rows] == list(range(1, 31))
    assert [r.review_probability for r in rows] == sorted(probs, reverse=True)
    assert {r.row_id for r in selected} == {c.row_id for c in cards[-10:]}  # highest 10
    assert all(r.status == ls.STATUS_NOT_SELECTED for r in rows[10:])


def test_no_exploration_picks_selected_is_exactly_the_top_k():
    cards = _cards(30)
    probs = [((i * 7) % 30) / 30 for i in range(30)]
    rows = select_pool(cards, _scores(cards, probs), as_of=AS_OF, seed=SEED)
    top_ids = {r.row_id for r in rows if r.rank is not None and r.rank <= 10}
    assert {r.row_id for r in rows if r.status == ls.STATUS_SELECTED} == top_ids


def test_fewer_than_k_valid_cards_selects_them_all_and_never_pads():
    cards = _cards(6)
    rows = select_pool(cards, _scores(cards, [0.1 * i for i in range(6)]), as_of=AS_OF, seed=SEED)
    assert [r.status for r in rows] == [ls.STATUS_SELECTED] * 6


def test_exact_ties_break_by_seeded_row_id_hash_and_are_input_order_independent():
    cards = _cards(30)
    scores = _scores(cards, [0.5] * 30)  # all tied
    a = select_pool(cards, scores, as_of=AS_OF, seed=SEED)
    b = select_pool(list(reversed(cards)), scores, as_of=AS_OF, seed=SEED)
    assert a == b, "ordering must not depend on input order"
    # Tie order is exactly ascending seeded hash.
    assert [r.row_id for r in a] == sorted((c.row_id for c in cards),
                                           key=lambda rid: ls._tie_break_key(SEED, rid))
    other = select_pool(cards, scores, as_of=AS_OF, seed=SEED + 1)
    assert [r.row_id for r in other] != [r.row_id for r in a], "a different seed must change the tie-break"
    assert all(r.tie_break_key == ls._tie_break_key(SEED, r.row_id) for r in a)


def test_answer_confidence_is_recorded_but_never_affects_ranking():
    cards = _cards(12)
    probs = [i / 20 for i in range(12)]
    low = select_pool(cards, _scores(cards, probs, conf=0.51), as_of=AS_OF, seed=SEED)
    high = select_pool(cards, _scores(cards, probs, conf=0.99), as_of=AS_OF, seed=SEED)
    assert [(r.row_id, r.rank, r.status) for r in low] == [(r.row_id, r.rank, r.status) for r in high]
    assert {r.answer_confidence for r in low} == {0.51}
    # Even adversarial per-card confidence cannot reorder ties or probabilities.
    scores = {c.row_id: ScoreRecord(c.row_id, 0.5, 0.5 + i / 100) for i, c in enumerate(cards)}
    rows = select_pool(cards, scores, as_of=AS_OF, seed=SEED)
    assert [r.row_id for r in rows] == sorted((c.row_id for c in cards),
                                              key=lambda rid: ls._tie_break_key(SEED, rid))


def test_every_input_is_labelled_and_unavailable_data_is_explicit():
    u = load_universe()
    ids = make_row_ids(u.symbols, SEED, u.hash)
    s = list(u.symbols)
    ok = [build_card(ids[x], _prepared(five_day_return=0.01 * i)) for i, x in enumerate(s[:5])]
    incomplete = build_card(ids[s[5]], _prepared(rsi14=None))
    unscored = build_card(ids[s[6]], _prepared())
    stale = build_card(ids[s[7]], _prepared())
    cards = ok + [incomplete, unscored, stale]
    scores = {c.row_id: ScoreRecord(c.row_id, 0.4 + 0.01 * i, 0.8) for i, c in enumerate(ok)}
    scores[stale.row_id] = ScoreRecord(stale.row_id, 0.99, 0.99)  # stale must NOT win despite a high score
    bars = {c.row_id: AS_OF for c in cards}
    bars[stale.row_id] = date(2024, 3, 14)

    rows = {r.row_id: r for r in select_pool(cards, scores, as_of=AS_OF, seed=SEED, latest_bar_dates=bars)}
    assert set(rows) == {c.row_id for c in cards}
    assert {r.status for r in rows.values()} <= {ls.STATUS_SELECTED, ls.STATUS_NOT_SELECTED, ls.STATUS_DATA_UNAVAILABLE}
    for c in (incomplete, unscored, stale):
        r = rows[c.row_id]
        assert (r.status, r.rank, r.review_probability) == (ls.STATUS_DATA_UNAVAILABLE, None, None)
    assert "incomplete" in rows[incomplete.row_id].reason
    assert "no review score" in rows[unscored.row_id].reason
    assert rows[stale.row_id].freshness == ls.FRESHNESS_STALE and "stale" in rows[stale.row_id].reason
    assert all(rows[c.row_id].status == ls.STATUS_SELECTED for c in ok)  # 5 valid <= 10, all picked


def test_a_bar_after_the_as_of_date_is_lookahead_and_raises():
    cards = _cards(3)
    with pytest.raises(ValueError, match="lookahead"):
        select_pool(cards, _scores(cards, [0.1, 0.2, 0.3]), as_of=AS_OF, seed=SEED,
                    latest_bar_dates={cards[0].row_id: date(2024, 3, 16)})


@pytest.mark.parametrize("p", [-0.1, 1.1, float("nan")])
def test_invalid_probabilities_and_inconsistent_inputs_are_rejected(p):
    cards = _cards(2)
    with pytest.raises(ValueError, match="review_probability"):
        select_pool(cards, {cards[0].row_id: ScoreRecord(cards[0].row_id, p, 0.9)}, as_of=AS_OF, seed=SEED)


def test_duplicate_cards_orphan_scores_and_bad_k_are_rejected():
    cards = _cards(2)
    with pytest.raises(ValueError, match="duplicate"):
        select_pool([cards[0], cards[0]], {}, as_of=AS_OF, seed=SEED)
    with pytest.raises(ValueError, match="no card"):
        select_pool(cards, {"r_" + "0" * 12: ScoreRecord("r_" + "0" * 12, 0.5, 0.5)}, as_of=AS_OF, seed=SEED)
    with pytest.raises(ValueError, match="k must be"):
        select_pool(cards, {}, as_of=AS_OF, seed=SEED, k=11)


def test_fake_scorer_satisfies_the_interface_end_to_end():
    cards = _cards(30, five_day_return=0.0)
    cards = [build_card(c.row_id, _prepared(five_day_return=i / 100)) for i, c in enumerate(cards)]
    scorer = FakeScorer()
    recs = scorer.score(cards)
    assert [r.row_id for r in recs] == [c.row_id for c in cards]
    rows = select_pool(cards, {r.row_id: r for r in recs}, as_of=AS_OF, seed=SEED)
    assert [r.rank for r in rows if r.status == ls.STATUS_SELECTED] == list(range(1, 11))


# -- 5. snapshot schema + persistence ----------------------------------------

def _snapshot(cards=None, seed=SEED, **kw):
    u = load_universe()
    cards = cards or _cards(30)
    recs = {r.row_id: r for r in FakeScorer().score(cards)}
    return build_snapshot(universe=u, scorer=FAKE_META, as_of=AS_OF, seed=seed,
                          cards=cards, scores=recs, **kw)


def test_snapshot_records_every_required_field():
    snap = _snapshot()
    d = json.loads(snapshot_to_json(snap))
    assert d["schema_version"] == 1 and d["selector_version"] == ls.SELECTOR_VERSION
    assert d["scorer"] == {"provider": "fake", "package_version": "0", "checkpoint": "fake-deterministic",
                           "revision": None, "question": "test question", "settings": ""}
    assert d["universe_hash"] == load_universe().hash and d["universe_version"] == "fixed30-v1"
    assert d["as_of"] == "2024-03-15" and d["seed"] == SEED and d["k"] == 10
    assert len(d["inputs_digest"]) == 64
    assert set(d["rows"][0]) == {"row_id", "status", "reason", "rank", "review_probability",
                                 "answer_confidence", "data_complete", "freshness", "tie_break_key"}


def test_same_inputs_and_seed_give_byte_stable_snapshots_and_round_trip():
    a, b = snapshot_to_json(_snapshot()), snapshot_to_json(_snapshot())
    assert a == b and a.endswith("\n")
    assert snapshot_to_json(snapshot_from_json(a)) == a
    assert snapshot_to_json(_snapshot(cards=list(reversed(_cards(30))))) == a, "input order must not matter"


def test_snapshot_contains_no_ticker_or_company_name():
    u = load_universe()
    text = snapshot_to_json(_snapshot())
    for m in u.members:
        assert not re.search(rf"\b{re.escape(m.symbol)}\b", text), m.symbol
        assert m.name not in text


def test_changed_inputs_are_represented_not_overwritten(tmp_path):
    first = _snapshot()
    path = write_snapshot(tmp_path, "run-1", first)
    original = path.read_text(encoding="utf-8")

    # Same date, same IDs, one card's numbers differ -> different digest...
    cards = _cards(30)
    cards[0] = build_card(cards[0].row_id, _prepared(five_day_return=0.09))
    changed = _snapshot(cards=cards)
    assert changed.inputs_digest != first.inputs_digest
    # ...and persistence refuses to replace the stored record.
    with pytest.raises(SnapshotConflict, match="immutable"):
        write_snapshot(tmp_path, "run-1", changed)
    assert path.read_text(encoding="utf-8") == original
    # A different seed / scorer / freshness also cannot silently replace it.
    with pytest.raises(SnapshotConflict):
        write_snapshot(tmp_path, "run-1", _snapshot(seed=SEED + 1))


def test_identical_retry_is_an_idempotent_noop_and_other_runs_are_independent(tmp_path):
    p1 = write_snapshot(tmp_path, "run-1", _snapshot())
    mtime = p1.stat().st_mtime_ns
    assert write_snapshot(tmp_path, "run-1", _snapshot()) == p1
    assert p1.stat().st_mtime_ns == mtime
    p2 = write_snapshot(tmp_path, "run-2", _snapshot(seed=SEED + 1))
    assert p2 != p1 and p2.parent.name == "run-2"
    assert load_snapshot(tmp_path, "run-1", "2024-03-15") == snapshot_from_json(p1.read_text(encoding="utf-8"))
    assert load_snapshot(tmp_path, "run-1", "2024-03-16") is None
    assert [f.name for f in p1.parent.iterdir()] == ["2024-03-15.json"], "no temp files left behind"


def test_write_is_atomic_a_failed_replace_leaves_nothing_behind(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("disk gone")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk gone"):
        write_snapshot(tmp_path, "run-1", _snapshot())
    assert list((tmp_path / "run-1").iterdir()) == []
    assert load_snapshot(tmp_path, "run-1", "2024-03-15") is None


def test_unsupported_schema_version_and_unsafe_run_id_are_rejected(tmp_path):
    d = json.loads(snapshot_to_json(_snapshot()))
    d["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version"):
        snapshot_from_json(json.dumps(d))
    for bad in ("../x", "a/b", "", "a b"):
        with pytest.raises(ValueError, match="path-safe"):
            write_snapshot(tmp_path, bad, _snapshot())
