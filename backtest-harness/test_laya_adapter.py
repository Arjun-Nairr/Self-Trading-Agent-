"""Laya adapter interface tests with a deterministic FAKE `decide_batch` --
no torch, no model weights, no network. (The real-model smoke check is a
separate, explicit step; see docs/scopes/LAYA_SELECTOR_V1.md.)
Run: python -m pytest -q test_laya_adapter.py
"""
import socket
from types import SimpleNamespace

import pytest

import laya_adapter as la
from laya_selector import (
    NUMERIC_FIELDS, ScoreRecord, ScorerError, ScorerMetadata, build_card, load_universe,
    make_row_ids, score_complete_cards,
)

META = ScorerMetadata("laya", "0.3.23", la.LAYA_MODEL_ID, la.LAYA_REVISION, la.REVIEW_QUESTION, "{}")


def _cards(n=5, **over):
    u = load_universe()
    ids = make_row_ids(list(u.symbols)[:n], 1, u.hash)
    prepared = dict(five_day_return=0.02, sma20_distance=0.01, rsi14=55.0, macd_direction=0.3,
                    relative_volume=1.1, realized_volatility=0.25, range_position_20d=0.6)
    return [build_card(rid, {**prepared, "five_day_return": 0.01 * i, **over})
            for i, rid in enumerate(ids.values())]


def _result(p, conf):
    """Shape of laya.DecisionResult for one noul field. `probabilities` is
    deliberately the rounded 4-decimal view; the adapter must NOT use it."""
    return SimpleNamespace(
        values={la.ANSWER_FIELD: p >= 0.5},
        answers={la.ANSWER_FIELD: {"type": "noul", "noul": p, "confidence": 0.1}},
        answer_confidence={la.ANSWER_FIELD: conf},
        probabilities={la.ANSWER_FIELD: {"false": round(1 - p, 4), "true": round(p, 4)}},
    )


class RecordingDecide:
    def __init__(self, results_fn):
        self.calls = []
        self._fn = results_fn

    def __call__(self, runner, states, schema=None, **kw):
        self.calls.append((runner, states, schema, kw))
        return self._fn(states)


def test_scores_a_batch_in_one_call_with_the_exact_schema_and_options():
    cards = _cards()
    decide = RecordingDecide(lambda states: [_result(0.1 * (i + 1), 0.8) for i in range(len(states))])
    scorer = la.LayaScorer("AGENT", decide, META)
    recs = scorer.score(cards)

    assert len(decide.calls) == 1, "one batched call, not N sequential ones"
    runner, states, schema, kw = decide.calls[0]
    assert runner == "AGENT"
    assert schema == la.REVIEW_SCHEMA
    assert schema["properties"][la.ANSWER_FIELD]["description"] == (
        "Is this identity-free numeric card worth spending the existing full technical/news review on today?")
    assert kw["return_details"] is True
    assert kw["batch_size"] == la.BATCH_SIZE
    assert "min_confidence" not in kw, "nothing may be silently dropped by a confidence gate"
    assert [r.row_id for r in recs] == [c.row_id for c in cards]
    assert [r.review_probability for r in recs] == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5])
    assert all(r.answer_confidence == 0.8 for r in recs)


def test_model_input_is_numeric_fields_only_in_canonical_order_without_row_id():
    cards = _cards()
    decide = RecordingDecide(lambda states: [_result(0.5, 0.5)] * len(states))
    la.LayaScorer(None, decide, META).score(cards)
    states = decide.calls[0][1]
    assert len(states) == len(cards)
    for card, state in zip(cards, states):
        assert tuple(state) == NUMERIC_FIELDS
        assert all(isinstance(v, float) for v in state.values())
        assert card.row_id not in str(state)
    # Fixed-width rounding makes the model's input text deterministic.
    state = la.card_to_state(build_card(cards[0].row_id, dict(
        five_day_return=0.123456789, sma20_distance=0.0, rsi14=50.0, macd_direction=0.0,
        relative_volume=1.0, realized_volatility=0.2, range_position_20d=0.5)))
    assert state["five_day_return"] == 0.1235


def test_review_probability_is_the_providers_raw_noul_answer_unmodified():
    # Distinct values in `answers` vs the `probabilities` view prove which one is read,
    # and that the adapter applies no rounding/transform of its own.
    p = 0.123456789012
    res = _result(p, 0.9)
    res.probabilities[la.ANSWER_FIELD]["true"] = 0.5
    recs = la.LayaScorer(None, RecordingDecide(lambda s: [res] * len(s)), META).score(_cards(1))
    assert recs[0].review_probability == p


def test_answer_confidence_is_captured_and_independent_of_the_probability():
    results = iter([_result(0.9, 0.55), _result(0.1, 0.99)])
    recs = la.LayaScorer(None, RecordingDecide(lambda s: [next(results) for _ in s]), META).score(_cards(2))
    assert [(r.review_probability, r.answer_confidence) for r in recs] == [(0.9, 0.55), (0.1, 0.99)]


@pytest.mark.parametrize("bad", [
    lambda s: [],                                                    # wrong count
    lambda s: [SimpleNamespace(answers={}, answer_confidence={}) for _ in s],   # no answer
    lambda s: [_result(1.5, 0.5) for _ in s],                        # p out of range
    lambda s: [_result(float("nan"), 0.5) for _ in s],
    lambda s: [_result(0.5, None) for _ in s],                       # no answer_confidence
])
def test_malformed_provider_output_fails_closed(bad):
    with pytest.raises(ScorerError):
        la.LayaScorer(None, RecordingDecide(bad), META).score(_cards(3))


def test_incomplete_card_is_never_sent_to_the_model():
    inc = build_card(_cards(4)[3].row_id, dict(rsi14=None))  # distinct row ID from the 3 complete cards
    with pytest.raises(ValueError, match="incomplete"):
        la.card_to_state(inc)
    # ...and the selector-side helper filters them out before scoring.
    decide = RecordingDecide(lambda s: [_result(0.5, 0.5)] * len(s))
    cards = _cards(3) + [inc]
    out = score_complete_cards(la.LayaScorer(None, decide, META), cards)
    assert len(decide.calls[0][1]) == 3 and inc.row_id not in out and len(out) == 3
    assert score_complete_cards(la.LayaScorer(None, decide, META), [inc]) == {}
    assert len(decide.calls) == 1, "no call at all when nothing is scoreable"


def test_score_complete_cards_rejects_a_scorer_that_breaks_its_contract():
    class Misordered:
        metadata = META

        def score(self, cards):
            return [ScoreRecord(c.row_id, 0.5, 0.5) for c in reversed(cards)]

    with pytest.raises(ScorerError, match="expected"):
        score_complete_cards(Misordered(), _cards(3))

    class Wrong:
        metadata = META

        def score(self, cards):
            return [ScoreRecord(c.row_id, 2.0, 0.5) for c in cards]

    with pytest.raises(ScorerError, match="review_probability"):
        score_complete_cards(Wrong(), _cards(2))


def test_scoring_makes_no_network_call(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("network access attempted while scoring")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    la.LayaScorer(None, RecordingDecide(lambda s: [_result(0.5, 0.5)] * len(s)), META).score(_cards())


def test_pins_are_exact_and_load_refuses_a_missing_directory(tmp_path):
    assert la.LAYA_PACKAGE_VERSION == "0.3.23"
    assert len(la.LAYA_REVISION) == 40 and len(la.LAYA_SHA256["model.safetensors"]) == 64
    with pytest.raises(FileNotFoundError, match="provision_checkpoint"):
        la.LayaScorer.load(str(tmp_path / "does-not-exist"))


def test_review_question_and_schema_are_the_one_approved_boolean_question():
    assert list(la.REVIEW_SCHEMA["properties"]) == [la.ANSWER_FIELD]
    assert la.REVIEW_SCHEMA["properties"][la.ANSWER_FIELD]["type"] == "boolean"  # laya maps this to `noul`
    assert "buy" not in la.REVIEW_QUESTION.lower() and "profit" not in la.REVIEW_QUESTION.lower()
