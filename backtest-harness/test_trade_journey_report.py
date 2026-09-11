"""Plain-python tests for trade_journey_report.py's pure logic. No
network, no Neon, no Hermes, no Docker -- synthetic trades/snapshots
only, same convention as test_counterfactual.py / test_benchmark_sensitivity.py.
Run: python test_trade_journey_report.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_journey_report import (                                    # noqa: E402
    build_journey, classify_provenance, label_journeys, pair_trades,
)

checks = 0


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def check_true(label: str, cond) -> None:
    global checks
    checks += 1
    assert cond, f"{label}: expected truthy"


def trade(ticker, action, status, fill_price, fill_qty, created_at, updated_at, notes=None,
          confidence=None, primary_signal=None, invalidation_condition=None, exit_provenance=None):
    return {"ticker": ticker, "action": action, "status": status, "fill_price": fill_price,
            "fill_quantity": fill_qty, "created_at": created_at, "updated_at": updated_at,
            "strategy_notes": notes, "confidence": confidence, "primary_signal": primary_signal,
            "invalidation_condition": invalidation_condition, "exit_provenance": exit_provenance}


def harness_exit(ticker, fill_price, fill_qty, ts, reason_notes):
    """A mechanical exit row shaped exactly as record_exit() writes it
    (pre-migration: no durable exit_provenance yet) -- the structural
    fingerprint classify_provenance must recognize."""
    return trade(ticker, "SELL", "filled", fill_price, fill_qty, ts, ts, reason_notes,
                 confidence=None, primary_signal="other",
                 invalidation_condition="n/a -- mechanical exit, not a discretionary decision")


def agent_exit(ticker, fill_price, fill_qty, ts, notes, invalidation="thesis stands or falls on X"):
    """A real Hermes-authored SELL row shape (pre-migration) -- passes
    _validate_decision_fields, so confidence/primary_signal are real."""
    return trade(ticker, "SELL", "filled", fill_price, fill_qty, ts, ts, notes,
                 confidence=3, primary_signal="technical", invalidation_condition=invalidation)


def snap(day_label, created_at, detail):
    return {"day_label": day_label, "created_at": created_at, "open_positions_detail": detail}


def detail(ticker, price, upl, upl_pct, stop, target):
    return {"ticker": ticker, "current_price": price, "unrealized_pl": upl,
            "unrealized_pl_pct": upl_pct, "stop_price": stop, "target_price": target}


# -- classify_provenance: durable field takes precedence, trusted as-is --
for dp, want in [("harness_stop", "harness_stop"), ("harness_target", "harness_target"),
                  ("harness_time_stop", "harness_time_stop")]:
    row = trade("AAA", "SELL", "filled", 10, 1, "t", "t", "whatever prose", exit_provenance=dp)
    check(f"durable exit_provenance={dp!r} trusted directly", classify_provenance(row), (want, None))

agent_row = trade("AAA", "SELL", "filled", 10, 1, "t", "t",
                   "The 5-day time stop has been reached, closing now.", exit_provenance="agent_requested")
check("durable agent_requested -> agent_requested_exit with rationale label",
      classify_provenance(agent_row), ("agent_requested_exit", "agent time-stop rationale"))

legacy_row = trade("AAA", "SELL", "filled", 10, 1, "t", "t", "old note", exit_provenance="legacy_unknown")
check("durable legacy_unknown falls through to fingerprint reconciliation, "
      "and an unrecognized fingerprint stays legacy_or_unresolved",
      classify_provenance(legacy_row), ("legacy_or_unresolved", None))

# -- classify_provenance: historical fingerprint reconciliation (no durable
# field -- every real Round 2 row) -- every deterministic harness path ----
check("harness fingerprint + 'stop hit' note -> harness_stop",
      classify_provenance(harness_exit("AAA", 10, 1, "t", "Mechanical exit: stop hit")),
      ("harness_stop", None))
check("harness fingerprint + 'stop hit (opening gap)' note -> harness_stop",
      classify_provenance(harness_exit("AAA", 10, 1, "t", "Mechanical exit: stop hit (opening gap)")),
      ("harness_stop", None))
check("harness fingerprint + 'target hit' note -> harness_target",
      classify_provenance(harness_exit("AAA", 10, 1, "t", "Mechanical exit: target hit")),
      ("harness_target", None))
check("harness fingerprint + 'time stop' note -> harness_time_stop",
      classify_provenance(harness_exit("AAA", 10, 1, "t", "Mechanical exit: time stop (5 trading days)")),
      ("harness_time_stop", None))

# -- agent-requested provenance (fingerprint path) ------------------------
check("agent fingerprint, ordinary thesis-invalidation prose -> agent_requested_exit, no rationale label",
      classify_provenance(agent_exit("AAA", 10, 1, "t",
                                      "The stop is already broken; closing before considering anything new.")),
      ("agent_requested_exit", None))

# -- CORE regression: an agent note that mentions "time stop" must stay
# agent_requested_exit, never get promoted to harness_time_stop, even
# though it uses one of the known rationale phrasings ---------------------
for notes in [
    "Closed because the time stop expired before the setup reached its target or stop.",
    "5-day time stop reached; exit is mechanical and does not require waiting for stop or target.",
    "The 5-day time stop has been reached, so I am closing the position mechanically instead of stretching the hold.",
]:
    got = classify_provenance(agent_exit("AAA", 10, 1, "t", notes))
    check(f"agent note mentioning time stop stays agent_requested_exit: {notes[:30]!r}",
          got, ("agent_requested_exit", "agent time-stop rationale"))

# Near-miss: mentions the time stop but not via any allowlisted exact
# prefix -- no secondary label, never fuzzy-matched in.
check("near-miss time-stop mention gets no rationale label",
      classify_provenance(agent_exit("AAA", 10, 1, "t",
                                      "Approaching the 5-day time stop soon, closing early to lock in gains.")),
      ("agent_requested_exit", None))

# -- legacy/unresolved: neither fingerprint matches -- never forced -------
ambiguous_row = trade("AAA", "SELL", "filled", 10, 1, "t", "t", "some old row",
                       confidence=None, primary_signal=None, invalidation_condition=None)
check("row matching neither fingerprint -> legacy_or_unresolved, not fabricated",
      classify_provenance(ambiguous_row), ("legacy_or_unresolved", None))

# -- pair_trades: one-lot, no-overlap, sequential per ticker -------------
rows = [
    trade("AAA", "BUY", "filled", 10.0, 100, "2026-01-01T09:00", "2026-01-02T16:00"),
    harness_exit("AAA", 12.0, 100, "2026-01-05T09:00", "Mechanical exit: target hit"),
    trade("AAA", "BUY", "rejected", 11.0, None, "2026-01-06T09:00", "2026-01-06T09:00"),  # not filled -- ignored
    trade("BBB", "BUY", "filled", 20.0, 50, "2026-01-01T09:00", "2026-01-02T16:00"),  # still open, no SELL
]
pairs = pair_trades(rows)
check("only one ticker has a closed pair", len(pairs), 1)
check("closed pair is AAA", pairs[0]["entry"]["ticker"], "AAA")
check("rejected BUY never becomes a new entry", pairs[0]["exit"]["fill_price"], 12.0)

# -- build_journey: full coverage, entry keyed on FILL (updated_at) not
# the queued created_at ----------------------------------------------
pair = {
    "entry": trade("AAA", "BUY", "filled", 10.0, 100, "2026-01-01T09:00", "2026-01-02T16:00"),
    "exit": harness_exit("AAA", 12.0, 100, "2026-01-05T16:00", "Mechanical exit: target hit"),
}
snaps = [
    snap("Day 1", "2026-01-01T16:00", []),  # before real fill -- correctly excluded
    snap("Day 2", "2026-01-02T16:00", [detail("AAA", 10.5, 50.0, 5.0, 9.0, 13.0)]),
    snap("Day 3", "2026-01-03T16:00", [detail("AAA", 11.0, 100.0, 10.0, 9.0, 13.0)]),
    snap("Day 4", "2026-01-04T16:00", [detail("AAA", 9.5, -50.0, -5.0, 9.0, 13.0)]),
    snap("Day 5", "2026-01-05T16:00", []),  # exit day itself -- excluded (half-open window)
]
j = build_journey(pair, snaps)
check("full coverage", j["coverage"], "full")
check("3 trading days captured (day 2,3,4 -- not day 1 or day 5)", j["captured_days"], 3)
check("expected == captured", j["expected_days"], 3)
check("realized pnl", j["pnl"], 200.0)
check("provenance", j["provenance"], "harness_target")
check("no rationale label on a harness exit", j["rationale_label"], None)
check("max daily-close pnl", j["max_pnl"], 100.0)
check("min daily-close pnl", j["min_pnl"], -50.0)
check("dist to stop on day 3", j["daily"][1]["dist_to_stop"], 2.0)
check("dist to target on day 3", j["daily"][1]["dist_to_target"], 2.0)

# -- build_journey: zero-day (same-session stop-out) ---------------------
same_session_pair = {
    "entry": trade("BBB", "BUY", "filled", 24.0, 800, "2026-02-10T09:00", "2026-02-18T09:30"),
    "exit": harness_exit("BBB", 23.22, 800, "2026-02-18T09:30", "Mechanical exit: stop hit (opening gap)"),
}
j2 = build_journey(same_session_pair, snaps)
check("zero-day coverage when no snapshot falls in the half-open window",
      j2["coverage"], "zero_day")
check("zero expected days", j2["expected_days"], 0)
check("no intermediate journey rows", j2["daily"], [])
check("max/min pnl are None with no daily rows", (j2["max_pnl"], j2["min_pnl"]), (None, None))

# -- build_journey: partial coverage is flagged, never hidden -----------
partial_snaps = [
    snap("Day 2", "2026-01-02T16:00", [detail("AAA", 10.5, 50.0, 5.0, 9.0, 13.0)]),
    snap("Day 3", "2026-01-03T16:00", []),  # ticker missing this day -- a real gap
    snap("Day 4", "2026-01-04T16:00", [detail("AAA", 9.5, -50.0, -5.0, 9.0, 13.0)]),
]
j3 = build_journey(pair, partial_snaps)
check("partial coverage detected", j3["coverage"], "partial")
check("expected 3, captured 2", (j3["expected_days"], j3["captured_days"]), (3, 2))

# -- build_journey: agent-requested exit with time-stop rationale --------
agent_pair = {
    "entry": trade("CCC", "BUY", "filled", 10.0, 100, "2026-01-01T09:00", "2026-01-02T16:00"),
    "exit": agent_exit("CCC", 11.0, 100, "2026-01-05T16:00",
                        "5-day time stop reached; the position should be exited mechanically."),
}
j4 = build_journey(agent_pair, snaps)
check("agent exit provenance", j4["provenance"], "agent_requested_exit")
check("agent exit carries the secondary rationale label", j4["rationale_label"], "agent time-stop rationale")

# -- label_journeys: stable, chronological by entry fill, masked ---------
labeled = label_journeys([j2, j])  # j2 entered later than j
check("earlier entry gets TRADE-01", labeled[0]["ticker"], "AAA")
check("later entry gets TRADE-02", labeled[1]["ticker"], "BBB")
check("labels never expose ticker", [x["label"] for x in labeled], ["TRADE-01", "TRADE-02"])

print(f"OK — {checks} checks passed")
