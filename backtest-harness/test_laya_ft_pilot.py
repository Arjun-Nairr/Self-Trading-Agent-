"""Tests for the Laya fine-tuning pilot groundwork (no model, no Docker, no private data)."""
import json

import pytest

import laya_adapter
import laya_ft_pilot as ft
from laya_selector import build_card


def test_synthetic_cards_get_the_label_their_situation_names():
    want = {"ordinary": {"ordinary"}, "clear": {"review_worthy"}, "borderline": {"review_worthy", "ambiguous"}}
    for name, situation, card in ft.SYNTHETIC:
        assert ft.rubric_reference(card)[0] in want[situation], name
    labels = [ft.rubric_reference(c)[0] for _, _, c in ft.SYNTHETIC]
    assert {l: labels.count(l) for l in ft.LABELS} == {"review_worthy": 5, "ordinary": 3, "ambiguous": 4}


def test_rubric_counts_families_not_correlated_fields():
    base = dict(ft._BASE)
    assert ft.rubric_reference({**base, "macd_direction": 0.99})[0] == "ordinary", "MACD only supports"
    location_only = {**base, "sma20_distance": -0.05, "range_position_20d": 0.08}
    assert ft.rubric_reference(location_only)[0] == "ordinary", "one family, two correlated fields"
    assert ft.rubric_reference({**location_only, "rsi14": 30})[0] == "review_worthy", "two families"
    assert ft.rubric_reference({**base, "five_day_return": -0.06, "sma20_distance": 0.05})[0] == "ambiguous"
    assert ft.rubric_reference({**base, "relative_volume": 2.6})[0] == "ambiguous", "lone strong near anchor"
    assert ft.rubric_reference({**base, "rsi14": 70})[0] == "ordinary", "RSI nearness measured from 50"


def test_real_sample_depends_only_on_session_and_row_id():
    ids = [f"r_{i:012x}" for i in range(30)]
    by_day = {s: [build_card(r, ft._BASE) for r in ids] for s in ("2026-09-28", "2026-09-29", "2026-10-02")}
    picked = ft.sample_real(by_day)
    assert len(picked) == 12 and len({c.row_id for _, c in picked}) == 12
    assert [s for s, _ in picked] == ["2026-09-28"] * 4 + ["2026-09-29"] * 4 + ["2026-10-02"] * 4
    loud = {s: [build_card(c.row_id, {**ft._BASE, "five_day_return": 0.2}) for c in cs] for s, cs in by_day.items()}
    assert [c.row_id for _, c in ft.sample_real(loud)] == [c.row_id for _, c in picked], "values never steer it"


def test_records_validate_and_reject_leaks():
    rec = ft.make_record("synthetic", ft.example_id("synthetic", "x"), ft._BASE, "syn_x")
    ft.validate_record(rec)
    for bad in ({**rec, "ticker": "X"}, {**rec, "rendering": rec["rendering"] + " laya score 0.4"},
                {**rec, "proposed_label": "buy"}, {**rec, "private_source": {"symbol": "X"}}):
        with pytest.raises(ValueError):
            ft.validate_record(bad)
    over = ft.make_record("synthetic", "ex_0", ft._BASE, "g", None, {"label": "ambiguous", "reason": "why"})
    assert (over["reference_label"], over["proposed_label"]) == ("ordinary", "ambiguous")
    assert "AI judgement" in over["rationale"]


def test_converters_match_upstream_layouts_and_refuse_unreviewed_or_ambiguous_labels():
    worthy = ft.make_record("synthetic", "ex_1", ft.SYNTHETIC[3][2], "g")
    with pytest.raises(ValueError):
        ft.to_typed_decisions_row(worthy)                      # AI-proposed: not trainable by default
    row = ft.to_typed_decisions_row({**worthy, "label_status": "human_reviewed", "final_label": "ordinary"})
    assert set(row) == {"id", "workflow", "split", "state", "questions", "gold"}
    assert all(isinstance(v, str) for v in row.values())
    assert json.loads(row["questions"]) == laya_adapter.REVIEW_QUESTIONS
    gold = json.loads(row["gold"])[laya_adapter.ANSWER_FIELD]
    assert gold["label"] == "false" and gold["probabilities"] == {"false": 1.0, "true": 0.0}
    assert json.loads(row["state"]) == laya_adapter.card_to_state(build_card("r_000000000000", worthy["card"]))
    ev = ft.to_evals_row(worthy, allow_provisional=True)
    assert ev["expected"] == {laya_adapter.ANSWER_FIELD: True} and ev["questions"] == laya_adapter.REVIEW_QUESTIONS
    amb = ft.make_record("synthetic", "ex_2", ft.SYNTHETIC[8][2], "g")
    with pytest.raises(ValueError):
        ft.to_evals_row(amb, allow_provisional=True)


def test_partition_checks_catch_leakage():
    def row(part, session, state=1, src="real"):
        return {"partition": part, "session": session, "state": {"x": state}, "source_type": src}
    clean = [row("train", "2025-01-10", 1), row("validation", "2025-03-01", 2),
             row("calibration", "2025-04-15", 3), row("test", "2025-06-01", 4)]
    assert ft.check_partitions(clean) == []
    probs = ft.check_partitions(clean + [row("test", "2026-09-29", 5), row("validation", "2025-03-02", 1),
                                         row("calibration", "2025-04-16", 6, "synthetic")])
    assert any("explored development session" in p for p in probs)
    assert any("identical card" in p for p in probs)
    assert any("synthetic card in calibration" in p for p in probs)
    assert ft.check_partitions([row("train", "2025-01-10"), row("test", "2025-01-20", 2)]) == [
        "train->test gap 10 days < 30"]
