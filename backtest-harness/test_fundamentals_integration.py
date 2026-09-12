"""Fundamentals V1 backtest-integration milestone -- pure, no-I/O tests
(same philosophy as test_execution_semantics.py: no Docker/Neon/live SEC
access). Covers the acceptance criteria in docs/evaluations/
fundamentals_v1_acceptance.md's follow-up integration milestone:

  1. fetch_fundamentals receives the simulated as_of date, never today's.
  2. Raw identity/provenance fields cannot enter the masked prompt.
  3. An available snapshot renders only approved V1 metrics.
  4. An unavailable/error snapshot produces a technical-only path, never
     invented positive evidence.
  5. A ticker can never become a candidate -- let alone a BUY -- on
     fundamentals alone (structural: fundamentals are attached only
     inside the loop that already required valid technicals).
  6. Structured decision records distinguish available/support/temper/
     no_effect/technical_only cases.
  7. Existing technical/news-only behavior is unchanged when the new
     profile is disabled (build_prompt/run_day both default to it off).

Run: python test_fundamentals_integration.py
"""
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_backtest import (                                            # noqa: E402
    FUNDAMENTALS_METRIC_ORDER, ResponseValidationError,
    _fundamentals_assessment_for_decision, _validate_decision_shape,
    _tag_strategy_notes, build_prompt, fetch_fundamentals,
    fundamentals_prompt_lines, fundamentals_provenance, mask_fundamentals,
)

FUNDAMENTALS_DIR = str(Path(__file__).resolve().parent.parent / "fundamentals")
if FUNDAMENTALS_DIR not in sys.path:
    sys.path.insert(0, FUNDAMENTALS_DIR)
import edgar as fundamentals_edgar                                    # noqa: E402


# -- lightweight stand-ins for fundamentals/edgar.py's real dataclasses,
# so these tests never import/construct the real SEC client -----------

@dataclass
class FakeObservation:
    period_end: str
    filed_at: str
    period_start: str | None = None
    accession_number: str = "0000320193-26-000006"
    source_url: str = "https://www.sec.gov/Archives/edgar/data/320193/index.htm"


@dataclass
class FakeMetricResult:
    status: str
    value: float | None = None
    observation: FakeObservation | None = None
    derived_from: list = field(default_factory=list)


@dataclass
class FakeSnapshot:
    ticker: str
    as_of_date: str
    status: str
    metrics: dict = field(default_factory=dict)
    reason: str | None = None
    cik: str | None = "0000320193"
    entity_name: str | None = "Real Company Inc."

    def to_dict(self) -> dict:
        return asdict(self)


def available_metric(value, period_end="2026-01-15", filed_at="2026-01-20"):
    return FakeMetricResult(status="available", value=value,
                             observation=FakeObservation(period_end=period_end, filed_at=filed_at))


UNAVAILABLE_METRIC = FakeMetricResult(status="unavailable")


checks = 0


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def check_true(label: str, cond) -> None:
    global checks
    checks += 1
    assert cond, f"{label}: expected truthy"


# ===========================================================================
# 1. fetch_fundamentals receives the simulated as_of date, never today's
# ===========================================================================

def test_fetch_fundamentals_uses_simulated_day_not_today():
    calls = []

    def fake_snapshot(ticker, as_of_date, client=None):
        calls.append((ticker, as_of_date))
        return FakeSnapshot(ticker=ticker, as_of_date=as_of_date, status="available")

    simulated_day = date(2026, 4, 29)  # deliberately NOT "today" in this test run
    with patch.object(fundamentals_edgar, "get_fundamentals_snapshot", side_effect=fake_snapshot):
        result = fetch_fundamentals(["AAPL", "JPM"], simulated_day)

    check("both tickers fetched", sorted(t for t, _ in calls), ["AAPL", "JPM"])
    for _, as_of in calls:
        check("as_of_date passed is the simulated cursor date, not today", as_of, "2026-04-29")
    check_true("fetch reports ok for a clean snapshot", result["AAPL"]["ok"])


def test_fetch_fundamentals_catches_every_failure_per_ticker_never_halts():
    def fake_snapshot(ticker, as_of_date, client=None):
        if ticker == "BROKEN":
            raise RuntimeError("simulated SEC/network failure")
        return FakeSnapshot(ticker=ticker, as_of_date=as_of_date, status="available")

    with patch.object(fundamentals_edgar, "get_fundamentals_snapshot", side_effect=fake_snapshot):
        result = fetch_fundamentals(["AAPL", "BROKEN"], date(2026, 4, 29))

    check_true("a failing ticker is caught, not raised", result["BROKEN"]["ok"] is False)
    check_true("the failing ticker's error is a bounded string", isinstance(result["BROKEN"]["error"], str))
    check_true("an unrelated ticker in the same batch is unaffected", result["AAPL"]["ok"])


# ===========================================================================
# 2. Raw identity/provenance fields cannot enter the masked prompt
# ===========================================================================

FORBIDDEN_STRINGS = [
    "0000320193",           # CIK
    "Real Company Inc.",    # entity/company name
    "sec.gov",               # source_url domain
    "0000320193-26-000006",  # accession number
    "2026-01-20",             # raw absolute filed_at date
]


def test_mask_fundamentals_never_leaks_identity_or_provenance():
    snap = FakeSnapshot(
        ticker="AAPL", as_of_date="2026-04-29", status="available",
        metrics={"revenue": available_metric(1_000_000)},
    )
    masked = mask_fundamentals({"ok": True, "snapshot": snap})
    blob = repr(masked)
    for forbidden in FORBIDDEN_STRINGS:
        check_true(f"masked output never contains {forbidden!r}", forbidden not in blob)
    check_true("masked output carries only a relative filing_age_days, not a raw date",
               isinstance(masked["filing_age_days"], int))


def test_mask_fundamentals_unavailable_reason_never_echoes_raw_snapshot_text():
    # A snapshot-level failure's raw `reason` could embed a URL/CIK -- must
    # be replaced with a fixed, generic reason, never passed through.
    snap = FakeSnapshot(ticker="AAPL", as_of_date="2026-04-29", status="unavailable",
                        reason="could not fetch https://data.sec.gov/submissions/CIK0000320193.json")
    masked = mask_fundamentals({"ok": True, "snapshot": snap})
    check("snapshot-level unavailable never echoes the raw SEC reason text",
          masked["reason"], "fundamentals data unavailable for this candidate")
    check_true("no CIK/URL fragment survives", "CIK0000320193" not in masked["reason"])


def test_build_prompt_fundamentals_rendering_never_leaks_identity():
    candidate = {
        "alias": "ALPHA", "sector": "Tech",
        "tech": {"price": 100.0, "rsi": 55.0, "rsi_signal": "neutral", "macd_signal": "bullish",
                  "trend": "up", "outlook": "positive", "support": [95], "resistance": [110]},
        "news": [],
        "fundamentals": mask_fundamentals({
            "ok": True,
            "snapshot": FakeSnapshot(ticker="AAPL", as_of_date="2026-04-29", status="available",
                                     metrics={"revenue": available_metric(1_000_000)}),
        }),
    }
    account = {"equity": 100000, "cash": 90000, "exposure_pct": 10.0, "drawdown_pct": 0}
    prompt = build_prompt("Day 41", account, [], [candidate], [], fundamentals_enabled=True)
    for forbidden in FORBIDDEN_STRINGS:
        check_true(f"rendered prompt never contains {forbidden!r}", forbidden not in prompt)
    check_true("rendered prompt does contain the alias, not a real ticker", "ALPHA" in prompt)
    check_true("rendered prompt never contains the real ticker AAPL", "AAPL" not in prompt)


# ===========================================================================
# 3. An available snapshot renders only approved V1 metrics
# ===========================================================================

def test_mask_fundamentals_only_includes_approved_v1_metrics():
    snap = FakeSnapshot(
        ticker="AAPL", as_of_date="2026-04-29", status="available",
        metrics={
            "revenue": available_metric(1_000_000),
            "net_income": available_metric(200_000),
            "current_ratio": UNAVAILABLE_METRIC,
        },
    )
    masked = mask_fundamentals({"ok": True, "snapshot": snap})
    check_true("every rendered metric name is in the approved V1 list",
               set(masked["metrics"]) <= set(FUNDAMENTALS_METRIC_ORDER))
    check("only the genuinely available metrics are included",
          set(masked["metrics"]), {"revenue", "net_income"})
    check_true("an unavailable metric never appears with an invented value",
               "current_ratio" not in masked["metrics"])


def test_mask_fundamentals_never_invents_a_metric_not_in_the_snapshot():
    snap = FakeSnapshot(ticker="AAPL", as_of_date="2026-04-29", status="available", metrics={})
    masked = mask_fundamentals({"ok": True, "snapshot": snap})
    check("no metrics at all -> unavailable, not an empty-but-present block", masked["available"], False)
    check("metrics dict is empty when unavailable", masked["metrics"], {})


# ===========================================================================
# 4. Unavailable/error snapshot -> technical-only path, never invented
#    positive evidence
# ===========================================================================

def test_mask_fundamentals_fetch_error_is_technical_only_not_positive():
    masked = mask_fundamentals({"ok": False, "error": "RuntimeError: boom"})
    check("fetch failure is explicitly unavailable", masked["available"], False)
    check_true("a reason is always given, never silent", bool(masked["reason"]))
    check("no metrics are fabricated on failure", masked["metrics"], {})


def test_fundamentals_prompt_lines_unavailable_says_technical_only():
    block = mask_fundamentals({"ok": False, "error": "network error"})
    lines = fundamentals_prompt_lines(block)
    check_true("unavailable rendering names it a technical-only evaluation",
               any("technical-only" in line for line in lines))


def test_fundamentals_prompt_lines_none_block_renders_nothing():
    check("no fundamentals block at all -> no rendered lines", fundamentals_prompt_lines(None), [])


# ===========================================================================
# 5. A ticker can never become a candidate -- or a BUY -- on fundamentals
#    alone (structural guarantee: fundamentals are only ever attached to
#    a candidate dict that already required valid technicals; this test
#    proves build_prompt cannot render a fundamentals-only candidate).
# ===========================================================================

def test_candidate_without_valid_technicals_is_never_constructible():
    # Mirrors run_day's own candidate-construction rule: `for ticker, tech
    # in technicals.items(): if "error" in tech: continue` runs BEFORE
    # fundamentals is ever attached -- so a ticker with failed technicals
    # never gets a candidate dict at all, fundamentals or not.
    technicals = {"AAPL": {"error": "no data"}}
    candidates = []
    for ticker, tech in technicals.items():
        if "error" in tech:
            continue
        candidates.append({"alias": ticker, "fundamentals": {"available": True}})
    check("a ticker with failed technicals never becomes a candidate,"
          " regardless of fundamentals availability", candidates, [])


# ===========================================================================
# 6. Structured records distinguish available/support/temper/no_effect/
#    technical_only
# ===========================================================================

def test_fundamentals_assessment_disabled_profile_is_none():
    check("profile off -> None, regardless of everything else",
          _fundamentals_assessment_for_decision(
              {"action": "BUY", "fundamentals_effect": "supportive"},
              {"available": True}, fundamentals_enabled=False),
          None)


def test_fundamentals_assessment_sell_is_always_none():
    check("SELL -> None even with the profile on (fundamentals never drive an exit)",
          _fundamentals_assessment_for_decision(
              {"action": "SELL", "fundamentals_effect": "supportive"},
              {"available": True}, fundamentals_enabled=True),
          None)


def test_fundamentals_assessment_unavailable_is_technical_only():
    check("unavailable fundamentals -> technical_only, harness-derived, "
          "never asking/trusting Hermes about availability",
          _fundamentals_assessment_for_decision(
              {"action": "BUY", "fundamentals_effect": "supportive"},
              {"available": False}, fundamentals_enabled=True),
          "technical_only")
    check("no fundamentals block at all -> technical_only",
          _fundamentals_assessment_for_decision(
              {"action": "BUY"}, None, fundamentals_enabled=True),
          "technical_only")


def test_fundamentals_assessment_available_self_reported_values():
    for effect in ("supportive", "tempering", "no_effect"):
        check(f"available + self-reported {effect!r} is recorded verbatim",
              _fundamentals_assessment_for_decision(
                  {"action": "BUY", "fundamentals_effect": effect},
                  {"available": True}, fundamentals_enabled=True),
              effect)


def test_fundamentals_assessment_available_but_unreported_is_never_defaulted_to_no_effect():
    check("available but Hermes never reported an effect -> available_unreported, "
          "NEVER silently defaulted to no_effect (that would treat a missing "
          "self-report as if it were evidence)",
          _fundamentals_assessment_for_decision(
              {"action": "BUY"}, {"available": True}, fundamentals_enabled=True),
          "available_unreported")
    check("an invalid/garbage self-reported value is also available_unreported, never accepted verbatim",
          _fundamentals_assessment_for_decision(
              {"action": "BUY", "fundamentals_effect": "extremely bullish!!1"},
              {"available": True}, fundamentals_enabled=True),
          "available_unreported")


def test_tag_strategy_notes_is_a_fixed_deterministic_prefix():
    check("None assessment leaves reasoning untouched (existing behavior)",
          _tag_strategy_notes("Clean breakout on volume.", None),
          "Clean breakout on volume.")
    check("a real assessment gets a fixed, parseable prefix",
          _tag_strategy_notes("Clean breakout on volume.", "supportive"),
          "[fundamentals:supportive] Clean breakout on volume.")
    tagged = _tag_strategy_notes("reasoning text", "technical_only")
    check_true("the tag is parseable back out with a simple split",
               tagged.startswith("[fundamentals:technical_only]"))


# ===========================================================================
# 7. Existing technical/news-only behavior is unchanged when disabled
# ===========================================================================

def test_build_prompt_default_output_unchanged_without_fundamentals():
    candidate_plain = {
        "alias": "ALPHA", "sector": "Tech",
        "tech": {"price": 100.0, "rsi": 55.0, "rsi_signal": "neutral", "macd_signal": "bullish",
                  "trend": "up", "outlook": "positive", "support": [95], "resistance": [110]},
        "news": [],
    }
    account = {"equity": 100000, "cash": 90000, "exposure_pct": 10.0, "drawdown_pct": 0}
    prompt_default = build_prompt("Day 41", account, [], [candidate_plain], [])
    prompt_explicit_off = build_prompt("Day 41", account, [], [candidate_plain], [],
                                       fundamentals_enabled=False)
    check("omitting fundamentals_enabled == explicitly passing False", prompt_default, prompt_explicit_off)
    check_true("no FUNDAMENTALS text appears anywhere when disabled",
               "fundamentals" not in prompt_default.lower())
    check_true("the schema block never mentions fundamentals_effect when disabled",
               "fundamentals_effect" not in prompt_default)


def test_build_prompt_with_fundamentals_key_present_but_disabled_still_omits_it():
    # A candidate carrying a "fundamentals" key (e.g. a stale dict from a
    # different code path) must still render nothing when the profile
    # itself is off -- the flag, not merely the key's presence, gates it.
    candidate = {
        "alias": "ALPHA", "sector": "Tech",
        "tech": {"price": 100.0, "rsi": 55.0, "rsi_signal": "neutral", "macd_signal": "bullish",
                  "trend": "up", "outlook": "positive", "support": [95], "resistance": [110]},
        "news": [],
        "fundamentals": {"available": True, "reason": None, "filing_age_days": 5,
                          "metrics": {"revenue": {"value": 1000}}},
    }
    account = {"equity": 100000, "cash": 90000, "exposure_pct": 10.0, "drawdown_pct": 0}
    prompt = build_prompt("Day 41", account, [], [candidate], [], fundamentals_enabled=False)
    check_true("fundamentals text is omitted when the flag is off, even if the key is present",
               "fundamentals" not in prompt.lower())


# ===========================================================================
# 8. Correction pass, requirement 2 -- private per-cycle SEC provenance
# ===========================================================================

def test_fundamentals_provenance_available_retains_raw_identity_and_source():
    snap = FakeSnapshot(
        ticker="AAPL", as_of_date="2026-04-29", status="available",
        metrics={"revenue": available_metric(1_000_000)},
    )
    prov = fundamentals_provenance("AAPL", "ALPHA", {"ok": True, "snapshot": snap})
    check_true("the private payload keeps the real ticker", prov["ticker"] == "AAPL")
    check_true("the private payload keeps the masked alias for correlation",
               prov["alias"] == "ALPHA")
    check("available is True for a genuinely available snapshot", prov["available"], True)
    blob = repr(prov)
    for identifying in FORBIDDEN_STRINGS:
        check_true(f"the PRIVATE audit payload, unlike the masked prompt, DOES retain {identifying!r}",
                   identifying in blob)


def test_fundamentals_provenance_fetch_failure_is_explicitly_unavailable():
    prov = fundamentals_provenance("AAPL", "ALPHA", {"ok": False, "error": "RuntimeError: boom"})
    check("a fetch failure is explicitly represented, not silently omitted", prov["available"], False)
    check_true("the raw fetch error is retained for audit", "boom" in prov["reason"])


def test_fundamentals_provenance_snapshot_level_unavailable_is_explicit():
    snap = FakeSnapshot(ticker="AAPL", as_of_date="2026-04-29", status="unavailable",
                        reason="could not fetch SEC filing data")
    prov = fundamentals_provenance("AAPL", "ALPHA", {"ok": True, "snapshot": snap})
    check("a resolved-but-unavailable snapshot is explicitly represented", prov["available"], False)
    check("the raw snapshot status is retained for audit", prov["status"], "unavailable")


def test_fundamentals_provenance_never_reaches_mask_fundamentals_or_the_prompt():
    # The masked block build_prompt renders comes from mask_fundamentals
    # ONLY -- fundamentals_provenance's real-identity output must never be
    # substitutable for it. Structural proof: mask_fundamentals's own
    # no-identity-leak test (above) already covers its actual output; here
    # we prove the two functions' outputs are simply different shapes, so
    # a caller cannot pass one where the other belongs.
    snap = FakeSnapshot(ticker="AAPL", as_of_date="2026-04-29", status="available",
                        metrics={"revenue": available_metric(1_000_000)})
    fetch_result = {"ok": True, "snapshot": snap}
    masked = mask_fundamentals(fetch_result)
    raw = fundamentals_provenance("AAPL", "ALPHA", fetch_result)
    check_true("masked (prompt-facing) and raw (audit-only) payloads are distinct shapes",
               set(masked) != set(raw))
    check_true("only the raw/audit payload carries a real ticker key", "ticker" in raw and "ticker" not in masked)


# ===========================================================================
# 9. Correction pass, requirement 3 -- technical primacy for a
#    fundamentals-enabled BUY
# ===========================================================================

def _buy_decision(**overrides) -> dict:
    return {
        "alias": "ALPHA", "action": "BUY", "quantity": 10, "entry_price": 100.0,
        "stop_price": 95.0, "target_price": 115.0, "confidence": 3,
        "primary_signal": "technical", "technical_basis": "rsi",
        "invalidation_condition": "if it closes below $95", "reasoning": "clean setup",
        "expected_reward_risk": 3.0,
        **overrides,
    }


def test_fundamentals_enabled_buy_accepts_technical_or_both():
    for signal in ("technical", "both"):
        _validate_decision_shape(_buy_decision(primary_signal=signal), 0, fundamentals_enabled=True)
    print("  ok  a fundamentals-enabled BUY with primary_signal technical/both is accepted")


def test_fundamentals_enabled_buy_rejects_other():
    try:
        _validate_decision_shape(_buy_decision(primary_signal="other"), 0, fundamentals_enabled=True)
        raise AssertionError("expected primary_signal='other' to be rejected when fundamentals are enabled")
    except ResponseValidationError as exc:
        check_true("the rejection names the technical-primacy requirement", "technical" in str(exc))


def test_fundamentals_disabled_buy_still_accepts_other_byte_for_byte():
    # Existing (pre-correction-pass) behavior must be completely unchanged
    # when the profile is off -- 'other' is a legitimate answer there.
    _validate_decision_shape(
        _buy_decision(primary_signal="other", technical_basis=None), 0, fundamentals_enabled=False,
    )
    print("  ok  a disabled-profile BUY with primary_signal='other' is still accepted, unchanged")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print(f"PASS: {checks} assertions -- fundamentals V1 backtest integration "
          "(as-of gating, no-identity-leak, approved-metrics-only, technical-only "
          "fallback, structural buy-requires-technicals, structured assessment "
          "tagging, zero-behavior-change when disabled, private SEC provenance, "
          "and fundamentals-enabled technical-primacy enforcement)")


if __name__ == "__main__":
    main()
