"""Plain-python tests for trade_journey_report.py's pure logic. No
network, no Neon, no Hermes, no Docker -- synthetic trades/snapshots
only, same convention as test_counterfactual.py / test_benchmark_sensitivity.py.
Run: python test_trade_journey_report.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_journey_report import (                                    # noqa: E402
    build_journey, classify_exit_reason, label_journeys, pair_trades,
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


def trade(ticker, action, status, fill_price, fill_qty, created_at, updated_at, notes=None):
    return {"ticker": ticker, "action": action, "status": status, "fill_price": fill_price,
            "fill_quantity": fill_qty, "created_at": created_at, "updated_at": updated_at,
            "strategy_notes": notes}


def snap(day_label, created_at, detail):
    return {"day_label": day_label, "created_at": created_at, "open_positions_detail": detail}


def detail(ticker, price, upl, upl_pct, stop, target):
    return {"ticker": ticker, "current_price": price, "unrealized_pl": upl,
            "unrealized_pl_pct": upl_pct, "stop_price": stop, "target_price": target}


# -- classify_exit_reason -----------------------------------------------
check("mechanical stop", classify_exit_reason("Mechanical exit: stop hit"), "mechanical_stop")
check("mechanical stop, opening gap", classify_exit_reason("Mechanical exit: stop hit (opening gap)"), "mechanical_stop")
check("mechanical target", classify_exit_reason("Mechanical exit: target hit"), "mechanical_target")
check("mechanical time stop", classify_exit_reason("Mechanical exit: time stop (5 trading days)"), "mechanical_time_stop")
check("discretionary prose stays unclassified, not keyword-guessed",
      classify_exit_reason("Exiting early -- thesis invalidated, RSI diverging on stop-like weakness"), "unclassified")
check("None notes -> unclassified", classify_exit_reason(None), "unclassified")
check("empty notes -> unclassified", classify_exit_reason(""), "unclassified")

# -- allowlisted discretionary time-stop phrasings (exact prefixes only) --
check("'Closed because the time stop expired' prefix",
      classify_exit_reason("Closed because the time stop expired -- no further edge in holding."),
      "mechanical_time_stop")
check("'5-day time stop reached;' prefix",
      classify_exit_reason("5-day time stop reached; taking the exit as designed."),
      "mechanical_time_stop")
check("'The 5-day time stop has been reached' prefix",
      classify_exit_reason("The 5-day time stop has been reached, so I am closing the position "
                            "mechanically instead of stretching the hold."),
      "mechanical_time_stop")
# Near-miss: mentions the time stop but does not match any allowlisted
# prefix exactly -- must stay unclassified, not fuzzy-matched in.
check("near-miss time-stop mention stays unclassified",
      classify_exit_reason("Approaching the 5-day time stop soon, closing early to lock in gains."),
      "unclassified")

# -- pair_trades: one-lot, no-overlap, sequential per ticker -------------
rows = [
    trade("AAA", "BUY", "filled", 10.0, 100, "2026-01-01T09:00", "2026-01-02T16:00"),
    trade("AAA", "SELL", "filled", 12.0, 100, "2026-01-05T09:00", "2026-01-05T09:00", "Mechanical exit: target hit"),
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
    "exit": trade("AAA", "SELL", "filled", 12.0, 100, "2026-01-05T16:00", "2026-01-05T16:00",
                  "Mechanical exit: target hit"),
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
check("exit reason category", j["exit_reason_category"], "mechanical_target")
check("max daily-close pnl", j["max_pnl"], 100.0)
check("min daily-close pnl", j["min_pnl"], -50.0)
check("dist to stop on day 3", j["daily"][1]["dist_to_stop"], 2.0)
check("dist to target on day 3", j["daily"][1]["dist_to_target"], 2.0)

# -- build_journey: zero-day (same-session stop-out) ---------------------
same_session_pair = {
    "entry": trade("BBB", "BUY", "filled", 24.0, 800, "2026-02-10T09:00", "2026-02-18T09:30"),
    "exit": trade("BBB", "SELL", "filled", 23.22, 800, "2026-02-18T09:30", "2026-02-18T09:30",
                  "Mechanical exit: stop hit (opening gap)"),
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

# -- label_journeys: stable, chronological by entry fill, masked ---------
labeled = label_journeys([j2, j])  # j2 entered later than j
check("earlier entry gets TRADE-01", labeled[0]["ticker"], "AAA")
check("later entry gets TRADE-02", labeled[1]["ticker"], "BBB")
check("labels never expose ticker", [x["label"] for x in labeled], ["TRADE-01", "TRADE-02"])

print(f"OK — {checks} checks passed")
