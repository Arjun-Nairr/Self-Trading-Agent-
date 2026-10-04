"""Tests for the Laya framing experiment at its public seam (no Docker, no model)."""
import json
import re

import pytest

import laya_adapter
import laya_framing as lf
import laya_framing_worker as fw


def test_questions_change_only_the_model_facing_labels():
    base = laya_adapter.REVIEW_QUESTIONS
    assert lf.question("default") == base
    for name, want in (("neutral", {"false": "B", "true": "A"}), ("neutral_swapped", {"false": "A", "true": "B"})):
        q = lf.question(name)
        inner = q[laya_adapter.ANSWER_FIELD]
        assert inner.pop("labels") == want
        assert q == base, "type, instructions and criteria unchanged"
    assert lf.question("default") == base, "the production dict was not mutated"


def test_worker_accepts_only_the_production_question_plus_labels():
    assert fw._check_question(lf.question("neutral"), laya_adapter) == laya_adapter.ANSWER_FIELD
    bad = lf.question("default")
    bad[laya_adapter.ANSWER_FIELD]["criteria"] = {"true": "x", "false": "y"}
    with pytest.raises(ValueError):
        fw._check_question(bad, laya_adapter)
    with pytest.raises(ValueError):
        fw._check_question({"other": lf.question("default")[laya_adapter.ANSWER_FIELD]}, laya_adapter)


def test_both_renderings_carry_exactly_the_production_numbers():
    for cid, card in lf.synthetic_cards():
        j = lf.render_state(card, "json")
        assert j == laya_adapter.card_to_state(card)
        text = lf.render_state(card, "explained")
        assert lf.parse_explained(text) == j, cid
        assert len(text.split("\n")) == 8, "header + all seven fields"


def test_explained_state_has_no_answer_labels_identity_or_verdict():
    for _, card in lf.synthetic_cards():
        text = lf.render_state(card, "explained")
        assert not re.search(r"\b(yes|no|true|false|notable|ordinary|worth|overbought|oversold)\b", text, re.I)
        assert not re.search(r"(^|\s)[AB]:", text)
        assert not re.search(r"\b[A-Z]{2,5}\b(?<!RSI)(?<!MACD)(?<!US)", text.replace("RSI(14)", "").replace("MACD(12,26,9)", ""))


def test_cases_are_fixed_balanced_and_labelled_only_where_unambiguous():
    ids = [c[0] for c in lf.CASES]
    assert len(ids) == len(set(ids)) == 30
    for split in ("dev", "conf"):
        rows = [c for c in lf.CASES if c[1] == split]
        assert len(rows) == 15
        assert sum(c[2] is False for c in rows) == 3 and sum(c[2] is True for c in rows) == 8
        assert sum(c[2] is None for c in rows) == 4
    for cid, _, exp, v in lf.CASES:
        notable = (abs(v["five_day_return"]) >= 0.07 or abs(v["sma20_distance"]) >= 0.07 or v["rsi14"] <= 25
                   or v["rsi14"] >= 75 or abs(v["macd_direction"]) >= 0.7 or v["relative_volume"] >= 2.5
                   or v["range_position_20d"] <= 0.05 or v["range_position_20d"] >= 0.95)
        ordinary = (abs(v["five_day_return"]) <= 0.005 and abs(v["sma20_distance"]) <= 0.005 and 48 <= v["rsi14"] <= 52
                    and abs(v["macd_direction"]) <= 0.05 and 0.95 <= v["relative_volume"] <= 1.05
                    and 0.45 <= v["range_position_20d"] <= 0.55)
        if exp is True:
            assert notable, cid
        if exp is False:
            assert ordinary, cid


def test_metrics_and_decision_rule():
    assert lf.auroc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert lf.auroc([0.1], [0.9]) == 0.0
    assert lf.auroc([0.5], [0.5]) == 0.5
    p = {"P_base": 0.2, **{f"P_{f}={s}": 0.2 + (0.01 if f == "five_day_return" else -0.01) for f, s, _, _ in lf.PAIRED}}
    m = lf.paired_metrics(p)
    assert m["predicted_up"] == 11 and m["agree_up"] == 2
    dev = {"V1_json_default": {"auroc": 0.5, "agree_up": 2, "malformed": 0, "truncated": 0},
           "V2_explained_default": {"auroc": 0.8, "agree_up": 6, "malformed": 0, "truncated": 0},
           "V3_json_neutral": {"auroc": 0.9, "agree_up": 7, "malformed": 0, "truncated": 0,
                               "swap_mean_abs_delta": 0.05, "swap_auroc": 0.9},
           "V4_explained_neutral": {"auroc": 0.7, "agree_up": 4, "malformed": 0, "truncated": 0,
                                    "swap_mean_abs_delta": 0.01, "swap_auroc": 0.75}}
    d = lf.choose(dev)
    assert d["qualified"] == ["V2_explained_default"] and d["chosen"] == "V2_explained_default"
    assert "label-swap mean |dp| > 0.02" in d["reasons"]["V3_json_neutral"]
    assert any("paired" in r for r in d["reasons"]["V4_explained_neutral"])
    dev["V2_explained_default"]["truncated"] = 3
    assert lf.choose(dev)["chosen"] is None


def test_worker_container_keeps_production_isolation_and_image():
    cmd = lf.docker_command("C:/ck", "laya-framing-x")
    s = " ".join(cmd)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--user 10001:10001"):
        assert flag in s
    vols = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert len(vols) == 2 and all(v.endswith(":ro") for v in vols)
    assert lf.IMAGE == "hermes-laya-worker:2" and "-p" not in cmd and "--publish" not in s


def test_top_k_uses_production_ranking_semantics():
    from laya_selector import ScoreRecord, build_card, select_pool, STATUS_SELECTED
    from datetime import date
    cards = [c for _, c in lf.synthetic_cards()][:14]
    p = [0.2, 0.2, 0.3, 0.1, 0.25, 0.2, 0.15, 0.3, 0.22, 0.21, 0.2, 0.19, 0.18, 0.2]
    rows = select_pool(cards, {c.row_id: ScoreRecord(c.row_id, x, 0.5) for c, x in zip(cards, p)},
                       as_of=date(2026, 9, 28), seed=42)
    want = [r.row_id for r in sorted((r for r in rows if r.status == STATUS_SELECTED), key=lambda r: r.rank)]
    assert lf.top_k([c.row_id for c in cards], p) == want
