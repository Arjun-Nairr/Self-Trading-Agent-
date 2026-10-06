"""Tests for the Laya fine-tuning pilot groundwork (no model, no Docker, no private data).
They check the rubric's own consistency only -- not market usefulness."""
import csv
import hashlib
import json
import random
import re

import pytest

import laya_adapter
import laya_ft_pilot as ft
from laya_selector import NUMERIC_FIELDS, build_card

B = ft._BASE


def label(**kw):
    return ft.rubric_reference({**B, **kw})[0]


def test_synthetic_cards_get_the_label_their_situation_names():
    want = {"ordinary": {"ordinary"}, "clear": {"review_worthy"}, "borderline": set(ft.LABELS)}
    for name, situation, card in ft.SYNTHETIC:
        assert ft.rubric_reference(card)[0] in want[situation], name
    labels = [ft.rubric_reference(c)[0] for _, _, c in ft.SYNTHETIC]
    assert {l: labels.count(l) for l in ft.LABELS} == {"review_worthy": 6, "ordinary": 4, "ambiguous": 2}


def test_rendering_has_units_and_round_trips_to_the_model_state():
    card = {**B, "five_day_return": -0.0510123, "rsi14": 67.99996, "relative_volume": 1.59996}
    text = ft.render(card)
    assert text == ("5d -5.10% | vs 20d avg +0.00% | RSI14 68.0000 | MACD dir +0.0000 | rel vol 1.6000x | "
                    "ann vol 25.00% | 20d range pos 50.00%")
    nums = [float(x) for x in re.findall(r"[-+]?\d+\.\d+", text)]
    s = ft.state(card)
    back = [s["five_day_return"] * 100, s["sma20_distance"] * 100, s["rsi14"], s["macd_direction"],
            s["relative_volume"], s["realized_volatility"] * 100, s["range_position_20d"] * 100]
    assert nums == pytest.approx(back, abs=1e-9)
    # the rubric judges exactly what is rendered: 67.99996 -> 68.0000 (moderate), 1.59996 -> 1.6000 (moderate)
    assert ft.rubric_reference(card)[0] == "review_worthy"


@pytest.mark.parametrize("kw, want", [
    ({"five_day_return": 0.07}, "review_worthy"), ({"five_day_return": 0.0699}, "ordinary"),
    ({"five_day_return": 0.0399}, "ordinary"), ({"rsi14": 75}, "review_worthy"), ({"rsi14": 74.99}, "ordinary"),
    ({"rsi14": 68}, "ordinary"), ({"relative_volume": 2.5}, "review_worthy"),
    ({"relative_volume": 2.4999}, "ordinary"), ({"realized_volatility": 0.60}, "ambiguous"),
    ({"realized_volatility": 0.5999}, "ordinary"), ({"relative_volume": 0.40}, "ambiguous"),
    ({"relative_volume": 0.4001}, "ordinary"), ({"macd_direction": 0.99}, "ordinary"),
    ({"sma20_distance": -0.05, "range_position_20d": 0.08}, "ordinary"),          # one family, two fields
    ({"sma20_distance": -0.05, "rsi14": 32}, "review_worthy"),                     # two families
])
def test_rubric_boundaries(kw, want):
    assert label(**kw) == want


def test_near_anchor_values_are_flagged_but_never_decide_the_label():
    lab, _, flags, why = ft.rubric_reference({**B, "five_day_return": 0.0699})
    assert (lab, why) == ("ordinary", None) and "near_anchor:five_day_return" in flags
    lab, _, flags, _ = ft.rubric_reference({**B, "relative_volume": 2.55})
    assert lab == "review_worthy" and "near_anchor:relative_volume" in flags


def _mirror(c):
    return {**c, "five_day_return": -c["five_day_return"], "sma20_distance": -c["sma20_distance"],
            "rsi14": 100 - c["rsi14"], "macd_direction": -c["macd_direction"],
            "range_position_20d": 1 - c["range_position_20d"]}


def test_up_and_down_activity_and_conflicts_are_treated_alike():
    flip = {"up": "down", "down": "up", "mixed": "mixed", "none": "none"}
    for name, _, card in ft.SYNTHETIC:
        a, b = ft.rubric_reference(card), ft.rubric_reference(_mirror(card))
        assert a[0] == b[0] and flip[a[1]] == b[1], name
    assert ft.rubric_reference({**B, "five_day_return": -0.06, "sma20_distance": 0.045})[:2] == (
        "review_worthy", "mixed"), "conflicting indicators can warrant review"


def test_ambiguous_only_means_insufficient_evidence():
    rng = random.Random(7)
    for _ in range(3000):
        c = {"five_day_return": rng.uniform(-0.12, 0.12), "sma20_distance": rng.uniform(-0.1, 0.1),
             "rsi14": rng.uniform(15, 85), "macd_direction": rng.uniform(-1, 1),
             "relative_volume": rng.uniform(0.2, 3.5), "realized_volatility": rng.uniform(0.1, 0.9),
             "range_position_20d": rng.uniform(0, 1)}
        lab, _, _, why = ft.rubric_reference(c)
        counted = [s for s in ft.rubric_signals(c) if s[1] != "supporting"]
        assert (lab == "ambiguous") == (why is not None)
        if counted:
            assert lab != "ambiguous"
        if lab == "ambiguous":
            assert why in ft.INSUFFICIENT.values()


def test_real_sample_depends_only_on_session_and_row_id():
    ids = [f"r_{i:012x}" for i in range(30)]
    by_day = {s: [build_card(r, B) for r in ids] for s in ("2026-09-28", "2026-09-29", "2026-10-02")}
    picked = ft.sample_real(by_day)
    assert len(picked) == 12 and len({c.row_id for _, c in picked}) == 12
    assert [s for s, _ in picked] == ["2026-09-28"] * 4 + ["2026-09-29"] * 4 + ["2026-10-02"] * 4
    loud = {s: [build_card(c.row_id, {**B, "five_day_return": 0.2}) for c in cs] for s, cs in by_day.items()}
    assert [c.row_id for _, c in ft.sample_real(loud)] == [c.row_id for _, c in picked], "values never steer it"


def test_records_validate_and_reject_leaks_unsupported_context_and_review_claims():
    rec = ft.make_record("synthetic", ft.example_id("synthetic", "x"), B, "syn_x")
    ft.validate_record(rec)
    for bad in ({**rec, "ticker": "X"}, {**rec, "rendering": rec["rendering"] + " laya score 0.4"},
                {**rec, "proposed_label": "buy"}, {**rec, "private_source": {"symbol": "X"}},
                {**rec, "rationale": rec["rationale"] + "; usually a calm stock"},
                {**rec, "label_status": "human_reviewed"}, {**rec, "previous_label": "ambiguous"},
                {**rec, "ambiguity_reason": "unclear"}):
        with pytest.raises(ValueError):
            ft.validate_record(bad)


def test_converters_refuse_unreviewed_or_ambiguous_labels():
    worthy = ft.make_record("synthetic", "ex_1", ft.SYNTHETIC[3][2], "g")
    with pytest.raises(ValueError):
        ft.to_typed_decisions_row(worthy)                      # AI-proposed: not trainable by default
    with pytest.raises(ValueError):
        ft.to_evals_row(worthy)
    row = ft.to_typed_decisions_row({**worthy, "label_status": "human_reviewed", "final_label": "ordinary"})
    assert set(row) == {"id", "workflow", "split", "state", "questions", "gold"}
    assert json.loads(row["questions"]) == laya_adapter.REVIEW_QUESTIONS
    gold = json.loads(row["gold"])[laya_adapter.ANSWER_FIELD]
    assert gold["label"] == "false" and gold["probabilities"] == {"false": 1.0, "true": 0.0}
    amb = ft.make_record("synthetic", "ex_2", ft.SYNTHETIC[9][2], "g")
    assert amb["proposed_label"] == "ambiguous"
    for conv in (ft.to_typed_decisions_row, ft.to_evals_row):
        with pytest.raises(ValueError):
            conv(amb, allow_provisional=True)
        with pytest.raises(ValueError):
            conv({**amb, "label_status": "human_reviewed"})


def _parent(tmp_path):
    """A v0.1-format parent pilot: one conflicting-direction card, one override, one unchanged."""
    cards = {"ex_a": dict(ft.SYNTHETIC[8][2]), "ex_b": {**B, "rsi14": 27.7}, "ex_c": dict(B)}
    prev = {"ex_a": ("ambiguous", "ambiguous", ["conflicting_directions"]),
            "ex_b": ("ambiguous", "ordinary", []), "ex_c": ("ordinary", "ordinary", [])}
    recs = [{"example_id": e, "source_type": "synthetic", "card": {f: float(c[f]) for f in NUMERIC_FIELDS},
             "proposed_label": prev[e][0], "reference_label": prev[e][1], "ambiguity_flags": prev[e][2],
             "rubric_version": ft.PREVIOUS_RUBRIC_VERSION, "group": "g_" + e, "private_source": {}}
            for e, c in cards.items()]
    d = tmp_path / "parent"
    d.mkdir()
    p = d / "pilot_dataset.json"
    p.write_text(json.dumps({"rubric_version": ft.PREVIOUS_RUBRIC_VERSION, "sample": {"salt": "x"},
                             "records": recs}), encoding="utf-8")
    return d, p, {e: (c, {}) for e, c in cards.items()}


def test_revision_preserves_the_parent_and_records_every_change(tmp_path):
    d, p, src = _parent(tmp_path)
    before = p.read_bytes()
    sha = hashlib.sha256(before).hexdigest()
    run = ft.revise(d, sha, src, out_dir=tmp_path / "out")
    assert p.read_bytes() == before, "parent never modified"
    out = {r["example_id"]: r for r in json.loads((run / "pilot_revision.json").read_text())["records"]}
    assert out["ex_a"]["proposed_label"] == "review_worthy" and "conflicting directions" in out["ex_a"]["change_reason"]
    assert out["ex_b"]["proposed_label"] == "ordinary" and "override" in out["ex_b"]["change_reason"]
    assert out["ex_c"]["change_reason"] is None
    for r in out.values():
        assert r["previous_rubric_version"] == ft.PREVIOUS_RUBRIC_VERSION and r["rubric_version"] == ft.RUBRIC_VERSION
        assert r["label_status"] == "ai_proposed_unreviewed" and r["card"] == {f: float(src[r["example_id"]][0][f])
                                                                             for f in NUMERIC_FIELDS}
    blind = list(csv.reader(open(run / "review_blind.csv", encoding="utf-8")))
    assert blind[0] == ft.BLIND_COLUMNS
    assert not any(l in cell for row in blind[1:] for cell in row for l in ("review_worthy", "ordinary", "ambiguous"))
    with pytest.raises(SystemExit):
        ft.revise(d, "0" * 64, src, out_dir=tmp_path / "out2")
    changed = {**src, "ex_c": ({**B, "rsi14": 51.0}, {})}
    with pytest.raises(SystemExit):
        ft.revise(d, sha, changed, out_dir=tmp_path / "out3")


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
