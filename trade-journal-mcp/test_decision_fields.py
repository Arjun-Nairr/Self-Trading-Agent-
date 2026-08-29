"""Proof the "report card" validation actually enforces what RULES.md and
the tool's own docstring claim it does -- no DB needed, pure function.
Run: docker exec hermes-trading-agent-trade-journal python test_decision_fields.py
"""
from server import _validate_decision_fields


def _valid(**overrides) -> dict:
    base = dict(
        action="BUY",
        confidence=3,
        primary_signal="technical",
        expected_reward_risk=2.5,
        invalidation_condition="price closes back below the breakout level",
        strategy_notes="RSI confirmed the breakout with strong volume.",
        technical_basis="rsi",
    )
    base.update(overrides)
    return base


def _rejects(**overrides) -> str | None:
    try:
        _validate_decision_fields(**_valid(**overrides))
        return None
    except ValueError as e:
        return str(e)


def main():
    # A fully correct call passes clean.
    _validate_decision_fields(**_valid())
    print("  ok  valid call passes")

    # confidence must be 1-5.
    assert _rejects(confidence=0) is not None
    assert _rejects(confidence=6) is not None
    print("  ok  confidence out of range 1-5 rejected")

    # primary_signal must be a real category.
    assert _rejects(primary_signal="vibes") is not None
    print("  ok  invalid primary_signal rejected")

    # expected_reward_risk must be positive -- a zero or negative ratio is
    # nonsensical, not just a bad trade.
    assert _rejects(expected_reward_risk=0) is not None
    assert _rejects(expected_reward_risk=-1) is not None
    print("  ok  non-positive expected_reward_risk rejected")

    # invalidation_condition is required and can't be whitespace pretending
    # to be an answer.
    assert _rejects(invalidation_condition="") is not None
    assert _rejects(invalidation_condition="   ") is not None
    print("  ok  blank invalidation_condition rejected")

    # THE FORCED-FABRICATION CASE Arjun raised: technical_basis is required
    # when primary_signal touches 'technical', but must NOT be required
    # when it doesn't -- a pure-sentiment decision has no technical basis
    # to report, and forcing one would mean fabricating a value.
    assert _rejects(primary_signal="technical", technical_basis=None) is not None
    assert _rejects(primary_signal="both", technical_basis=None) is not None
    _validate_decision_fields(**_valid(
        action="SELL", primary_signal="sentiment", technical_basis=None
    ))
    print("  ok  technical_basis required only when actually relevant, "
          "never forced on a pure-sentiment call")

    # THE ESCAPE-HATCH CASE: 'other' must never be a free pass -- it's only
    # legitimate when actually explained.
    assert _rejects(
        primary_signal="other", technical_basis=None, strategy_notes=None
    ) is not None
    assert _rejects(
        primary_signal="other", technical_basis=None, strategy_notes="   "
    ) is not None
    _validate_decision_fields(**_valid(
        primary_signal="other", technical_basis=None,
        strategy_notes="Unusual volume spike with no clean technical or news trigger.",
    ))
    print("  ok  'other' rejected without an explanation, accepted with one")

    # Same escape-hatch requirement applies to technical_basis='other'.
    assert _rejects(technical_basis="other", strategy_notes=None) is not None
    _validate_decision_fields(**_valid(
        technical_basis="other",
        strategy_notes="A chart pattern not captured by RSI/MACD/S&R/trend.",
    ))
    print("  ok  technical_basis='other' also requires an explanation")

    # RULES.md §5a: sentiment can veto an entry, never trigger one. A BUY
    # with primary_signal='sentiment' must be rejected; the same on a SELL
    # is legitimate (§6d thesis invalidation on adverse news).
    assert _rejects(action="BUY", primary_signal="sentiment", technical_basis=None) is not None
    _validate_decision_fields(**_valid(
        action="SELL", primary_signal="sentiment", technical_basis=None,
        invalidation_condition="n/a -- this is the exit",
    ))
    print("  ok  BUY+sentiment-only rejected (§5a), SELL+sentiment-only allowed (§6d)")

    # 2026-08-29 correctness pass #4, item 9: confidence=None (a
    # mechanical exit, not a judgment call) skips the report-card checks
    # ENTIRELY -- even fields that would otherwise be flatly invalid
    # (garbage primary_signal, blank invalidation_condition) must not
    # raise, because none of that applies to a rule firing mechanically.
    _validate_decision_fields(
        action="SELL", confidence=None, primary_signal="vibes",
        expected_reward_risk=-99, invalidation_condition="",
        strategy_notes=None, technical_basis=None,
    )
    print("  ok  confidence=None skips every report-card check, even on "
          "otherwise-invalid fields")

    # And a REAL confidence still enforces everything as before -- None
    # is a deliberate opt-out, not a hole that swallows normal validation.
    assert _rejects(confidence=0) is not None
    assert _rejects(primary_signal="vibes") is not None
    print("  ok  a real (non-None) confidence still enforces every check as before")

    print("PASS: decision-field validation enforces every rule it claims to")


if __name__ == "__main__":
    main()
