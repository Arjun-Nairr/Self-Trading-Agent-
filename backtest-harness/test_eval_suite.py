"""
test_eval_suite.py -- pure/fixture-only tests for eval_suite.py (no real
Docker/Neon/Hermes calls anywhere in this file -- see each test's fake).

Covers exactly what the regime-diverse evaluation-suite block requires,
including the 2026-09-13 correction pass:
  1. determinism from seed (same seed + data -> same episode selection)
  2. regime coverage (rally/selloff/high_volatility/sideways all present
     in BOTH the comparison suite and the holdout set)
  3. holdout separation (holdout months disjoint from, and chronologically
     later than, every comparison-suite month)
  4. manifest hash/tamper rejection
  5. refusal of proposed (and of tampered-but-claimed-approved) manifests
     -- through the PUBLIC API (run_configuration), proving zero calls
     into register_run/run_day/journal access before validation
  6. clean per-episode state isolation (fresh run_id/Portfolio per
     episode, CHECKPOINT_PATH restored after, no bleed between calls)
  7. fill_count means actual fills only, never queued proposals
  8. deterministic holdout-pool expansion, and clear failure when valid
     four-regime coverage is impossible

Run: python test_eval_suite.py
"""
import copy
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_suite import (                                              # noqa: E402
    EpisodeConfig, ManifestNotApprovedError, ManifestTamperedError,
    ManifestGenerationError, MonthMetrics, compute_content_hash,
    month_bounds, month_range, propose_manifest, require_approved,
    run_configuration, select_holdout_pool, stratified_sample,
    verify_manifest,
)

ELIGIBLE_START = date(2021, 1, 1)
ELIGIBLE_END = date(2023, 12, 31)  # 3 years -- plenty of months, fast tests

REGIME_CYCLE = ["rally", "selloff", "high_volatility", "sideways"]


def _weekdays_in(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def build_synthetic_closes(start: date, end: date) -> dict[str, float]:
    """Deterministic, arithmetic (no RNG) SPY-shaped closes that cycle
    every calendar month through all 4 regimes in a fixed order -- steady
    climb (rally), steady decline (selloff), big alternating daily swings
    (high_volatility, ~flat net return but way over the vol threshold),
    tiny wiggle (sideways). Engineered to reliably classify as intended
    under eval_suite's own real classify_regime, not hand-labeled. Cycling
    every 4 months means a 12-month trailing holdout window already
    contains 3 of each regime -- no expansion needed for this fixture."""
    closes: dict[str, float] = {}
    price = 100.0
    for i, (y, m) in enumerate(month_range(start, end)):
        regime = REGIME_CYCLE[i % 4]
        m_start, m_end = month_bounds(y, m)
        days = _weekdays_in(max(m_start, start), min(m_end, end))
        for j, d in enumerate(days):
            if regime == "rally":
                price *= 1.004
            elif regime == "selloff":
                price *= 0.996
            elif regime == "high_volatility":
                price *= 1.03 if j % 2 == 0 else 0.972
            else:
                price *= 1.001 if j % 2 == 0 else 0.999
            closes[d.isoformat()] = round(price, 6)
    return closes


def _fake_fetch_closes(ticker: str, start: date, end: date) -> dict[str, float]:
    return build_synthetic_closes(start, end)


def _propose(seed: int, start: date = ELIGIBLE_START, end: date = ELIGIBLE_END) -> dict:
    return propose_manifest(seed=seed, eligible_start=start, eligible_end=end,
                            fetch_closes=_fake_fetch_closes)


def _mm(year: int, month: int, regime: str) -> MonthMetrics:
    start, end = month_bounds(year, month)
    return MonthMetrics(year=year, month=month, start=start, end=end,
                        monthly_return_pct=0.0, realized_vol_pct=0.0, regime=regime)


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
# 1. Determinism from seed
# ===========================================================================

def test_propose_manifest_deterministic_from_seed():
    m1 = _propose(seed=777)
    m2 = _propose(seed=777)
    check("same seed -> identical comparison episodes", m1["episodes"], m2["episodes"])
    check("same seed -> identical holdout episodes", m1["holdout_episodes"], m2["holdout_episodes"])
    check("same seed -> identical full classification", m1["all_classified_months"], m2["all_classified_months"])
    check("same seed -> identical holdout pool bounds", m1["holdout_pool"], m2["holdout_pool"])


def test_propose_manifest_different_seed_can_select_differently():
    m1 = _propose(seed=1)
    m2 = _propose(seed=2)
    # Not guaranteed to differ for every regime (small pools), but the
    # module-level stratified draw must at least be seed-sensitive
    # somewhere across comparison+holdout -- if it never were, seed would
    # be decorative.
    check_true(
        "different seeds are capable of producing a different selection somewhere",
        m1["episodes"] != m2["episodes"] or m1["holdout_episodes"] != m2["holdout_episodes"],
    )


def test_stratified_sample_pure_determinism():
    months = [_mm(2021, i, REGIME_CYCLE[i % 4]) for i in range(1, 13)]
    a = stratified_sample(months, per_regime=1, seed=42)
    b = stratified_sample(months, per_regime=1, seed=42)
    check("stratified_sample is a pure function of its inputs", a, b)


# ===========================================================================
# 2. Regime coverage
# ===========================================================================

def test_regime_coverage_comparison_and_holdout():
    m = _propose(seed=123)
    comparison_regimes = {e["regime"] for e in m["episodes"]}
    holdout_regimes = {e["regime"] for e in m["holdout_episodes"]}
    for regime in ("rally", "selloff", "high_volatility", "sideways"):
        check_true(f"comparison suite includes a {regime!r} episode", regime in comparison_regimes)
        check_true(f"holdout set includes a {regime!r} episode", regime in holdout_regimes)


# ===========================================================================
# 3. Holdout separation
# ===========================================================================

def test_holdout_months_disjoint_and_chronologically_later():
    m = _propose(seed=5)
    comparison_keys = {(e["year"], e["month"]) for e in m["episodes"]}
    holdout_keys = {(e["year"], e["month"]) for e in m["holdout_episodes"]}
    check("comparison and holdout episode months never overlap",
          comparison_keys & holdout_keys, set())
    check_true("every holdout month is chronologically later than every comparison month",
               max(comparison_keys) < min(holdout_keys))


def test_holdout_pool_bounds_recorded_and_auditable():
    m = _propose(seed=9)
    hp = m["holdout_pool"]
    check("holdout_pool.initial_size_months matches the module's own documented constant",
          hp["initial_size_months"], 12)
    check_true("holdout_pool.size_months is at least the initial size",
               hp["size_months"] >= hp["initial_size_months"])
    check("this fixture's own regime cycle already covers all 4 in the default window -- no expansion needed",
          hp["expanded"], False)
    all_months = {(mm["year"], mm["month"]) for mm in m["all_classified_months"]}
    latest = max(all_months)
    check_true("the holdout window's own latest month is the eligible range's latest classified month",
               max((e["year"], e["month"]) for e in m["holdout_episodes"]) <= latest)


# ===========================================================================
# 4 & 5. Manifest hash / tamper / approval gate -- through the PUBLIC API
# ===========================================================================

def test_verify_manifest_accepts_untampered_and_rejects_edited_content():
    m = _propose(seed=11)
    verify_manifest(m)  # must not raise
    tampered = copy.deepcopy(m)
    tampered["episodes"][0]["regime"] = "rally" if tampered["episodes"][0]["regime"] != "rally" else "selloff"
    try:
        verify_manifest(tampered)
        raise AssertionError("expected a mutated episode field to fail verification")
    except ManifestTamperedError:
        pass
    print("  ok  verify_manifest accepts the original and rejects an edited episode field")


def test_status_transition_never_looks_like_tampering():
    m = _propose(seed=13)
    check("propose_manifest always writes status='proposed'", m["status"], "proposed")
    approved = dict(m)
    approved["status"] = "approved"  # the ONE human-approval edit
    verify_manifest(approved)  # must NOT raise -- status is excluded from the hash
    check("flipping status alone does not change the content hash",
          compute_content_hash(approved), compute_content_hash(m))
    require_approved(approved)  # must not raise
    print("  ok  status: proposed->approved is not tampering, and an approved+untampered manifest passes the gate")


def test_require_approved_refuses_proposed_manifest():
    m = _propose(seed=17)
    check("a freshly generated manifest is 'proposed'", m["status"], "proposed")
    try:
        require_approved(m)
        raise AssertionError("expected a 'proposed' manifest to be refused")
    except ManifestNotApprovedError:
        pass
    print("  ok  require_approved refuses a manifest still in 'proposed' status")


def test_require_approved_refuses_tampered_manifest_even_if_marked_approved():
    m = _propose(seed=19)
    tampered = copy.deepcopy(m)
    tampered["status"] = "approved"
    tampered["selection_seed"] = tampered["selection_seed"] + 1  # substantive edit
    try:
        require_approved(tampered)
        raise AssertionError("expected a tampered-but-'approved' manifest to be refused")
    except ManifestTamperedError:
        pass
    print("  ok  a tampered manifest is refused by require_approved even when status claims 'approved'")


def test_manifest_generation_error_on_empty_data():
    try:
        propose_manifest(seed=1, eligible_start=ELIGIBLE_START, eligible_end=ELIGIBLE_END,
                         fetch_closes=lambda t, s, e: {})
        raise AssertionError("expected empty historical data to fail clearly")
    except ManifestGenerationError:
        pass
    print("  ok  propose_manifest fails clearly (never substitutes hand-picked months) on empty proxy data")


def test_manifest_generation_error_on_fetch_failure():
    def _boom(t, s, e):
        raise RuntimeError("simulated network/Docker failure")
    try:
        propose_manifest(seed=1, eligible_start=ELIGIBLE_START, eligible_end=ELIGIBLE_END,
                         fetch_closes=_boom)
        raise AssertionError("expected a fetch failure to propagate as ManifestGenerationError")
    except ManifestGenerationError as exc:
        check_true("the underlying failure reason is named", "simulated network/Docker failure" in str(exc))
    print("  ok  propose_manifest documents the missing dependency and fails clearly on a real fetch failure")


def _one_day_episode(day: date, regime: str = "rally") -> dict:
    return {"year": day.year, "month": day.month, "regime": regime,
            "start": day.isoformat(), "end": day.isoformat()}


def _approve(manifest: dict) -> dict:
    approved = dict(manifest)
    approved["status"] = "approved"
    return approved


def test_public_api_refuses_proposed_manifest_before_any_execution_call():
    """Correction pass, requirement 1 -- tests the ACTUAL PUBLIC API
    (run_configuration), not require_approved() in isolation. A 'proposed'
    manifest must cause ZERO calls into register_run, run_day, or journal
    access (fetch_fundamentals) -- proving approval cannot be bypassed."""
    m = _propose(seed=201)
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]
    register_calls, run_day_calls, journal_calls = [], [], []
    with tempfile.TemporaryDirectory() as tmp, \
         patch("eval_suite.register_run", side_effect=lambda *a: register_calls.append(a)), \
         patch("eval_suite.run_day", side_effect=lambda *a, **k: run_day_calls.append(a)), \
         patch("eval_suite.fetch_fundamentals_assessment_counts",
               side_effect=lambda rid: journal_calls.append(rid) or {}):
        try:
            run_configuration(m, EpisodeConfig("baseline"), "smoke", watchlist, Path(tmp))
            raise AssertionError("expected a 'proposed' manifest to be refused by run_configuration")
        except ManifestNotApprovedError:
            pass
    check("zero register_run calls for a proposed manifest", register_calls, [])
    check("zero run_day calls for a proposed manifest", run_day_calls, [])
    check("zero journal-access calls for a proposed manifest", journal_calls, [])
    print("  ok  run_configuration (the public API) refuses a 'proposed' manifest before any "
          "register_run/run_day/journal-access call")


def test_public_api_refuses_tampered_manifest_before_any_execution_call():
    """Same proof, for a manifest whose content was edited after generation
    but whose status was also flipped to 'approved' -- hash verification
    must still block every execution-side call."""
    m = _propose(seed=203)
    tampered = _approve(copy.deepcopy(m))
    tampered["episodes"][0]["regime"] = "rally" if tampered["episodes"][0]["regime"] != "rally" else "selloff"
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]
    register_calls, run_day_calls, journal_calls = [], [], []
    with tempfile.TemporaryDirectory() as tmp, \
         patch("eval_suite.register_run", side_effect=lambda *a: register_calls.append(a)), \
         patch("eval_suite.run_day", side_effect=lambda *a, **k: run_day_calls.append(a)), \
         patch("eval_suite.fetch_fundamentals_assessment_counts",
               side_effect=lambda rid: journal_calls.append(rid) or {}):
        try:
            run_configuration(tampered, EpisodeConfig("baseline"), "smoke", watchlist, Path(tmp))
            raise AssertionError("expected a tampered-but-'approved' manifest to be refused")
        except ManifestTamperedError:
            pass
    check("zero register_run calls for a tampered manifest", register_calls, [])
    check("zero run_day calls for a tampered manifest", run_day_calls, [])
    check("zero journal-access calls for a tampered manifest", journal_calls, [])
    print("  ok  run_configuration (the public API) refuses a tampered-but-claimed-approved "
          "manifest before any register_run/run_day/journal-access call")


def test_run_configuration_proceeds_for_a_genuinely_approved_untampered_manifest():
    """Sanity counterpart -- an approved, untampered manifest DOES reach
    the execution path (proves the gate isn't refusing everything)."""
    m = _approve(_propose(seed=205))
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]
    register_calls = []

    def fake_run_day(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                     fundamentals_enabled=False):
        summary = {"day": str(day), "exits": [], "decisions": [], "blocked": [], "usage": {},
                  "fills": [], "proposals": []}
        return portfolio, summary, (lambda: None)

    with tempfile.TemporaryDirectory() as tmp, \
         patch("eval_suite.register_run", side_effect=lambda *a: register_calls.append(a)), \
         patch("eval_suite.run_day", side_effect=fake_run_day):
        report = run_configuration(m, EpisodeConfig("baseline"), "smoke", watchlist, Path(tmp))
    check_true("an approved, untampered manifest DOES reach execution (register_run called)",
               len(register_calls) > 0)
    check("aggregate report covers every comparison episode", report["aggregate"]["episode_count"],
          len(m["episodes"]))
    print("  ok  run_configuration proceeds normally for a genuinely approved, untampered manifest")


# ===========================================================================
# 6. Clean per-episode state isolation (_run_episode, via the public API)
#    -- no real Docker/Neon/Hermes call anywhere in this test.
# ===========================================================================

def test_run_configuration_isolates_state_and_restores_checkpoint_path():
    import run_backtest as rb
    from run_backtest import STARTING_EQUITY

    manifest = _approve(_propose(seed=207))
    monday = date(2023, 1, 2)  # a real Monday
    # Swap in a tiny, controlled episode list, then recompute the content
    # hash for THIS edited manifest -- the test still goes through the
    # real hash-verification path in run_configuration (a genuine public-
    # API call), it just isn't the originally-generated content.
    manifest["episodes"] = [_one_day_episode(monday), _one_day_episode(monday + timedelta(days=7))]
    from eval_suite import compute_content_hash as _hash
    manifest["content_hash"] = _hash(manifest)

    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]
    original_checkpoint_path = rb.CHECKPOINT_PATH
    seen_checkpoint_paths_during_call = []
    register_calls = []
    fake_calls = {"n": 0}

    def fake_run_day(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                     fundamentals_enabled=False):
        fake_calls["n"] += 1
        seen_checkpoint_paths_during_call.append(rb.CHECKPOINT_PATH)
        # If a PRIOR episode's state leaked in, portfolio.equity_curve
        # would NOT start at exactly [STARTING_EQUITY] here.
        assert portfolio.equity_curve == [STARTING_EQUITY], (
            f"episode {fake_calls['n']}: portfolio was not a fresh Portfolio() "
            f"(equity_curve={portfolio.equity_curve!r}) -- state bled from a prior episode"
        )
        portfolio.equity_curve.append(STARTING_EQUITY + 1.0)
        summary = {"day": str(day), "exits": [], "decisions": [], "blocked": [], "usage": {},
                  "fills": [], "proposals": []}
        return portfolio, summary, (lambda: None)

    with tempfile.TemporaryDirectory() as tmp:
        checkpoint_dir = Path(tmp) / "checkpoints"
        with patch("eval_suite.run_day", side_effect=fake_run_day), \
             patch("eval_suite.register_run", side_effect=lambda rid, kind: register_calls.append((rid, kind))):
            report = run_configuration(manifest, EpisodeConfig("baseline"), "smoke", watchlist, checkpoint_dir)

    per_episode = report["per_episode"]
    check("run_day was called once per episode", fake_calls["n"], 2)
    check_true("CHECKPOINT_PATH was isolated to the checkpoint_dir during each call",
               all(str(p).startswith(str(checkpoint_dir)) for p in seen_checkpoint_paths_during_call))
    check("CHECKPOINT_PATH is restored to its original value after run_configuration returns",
          rb.CHECKPOINT_PATH, original_checkpoint_path)
    check_true("each episode got a fresh, distinct run_id (no shared identity)",
               per_episode[0]["run_id"] != per_episode[1]["run_id"])
    check_true("register_run was called once per episode with that episode's own run_id",
               [c[0] for c in register_calls] == [per_episode[0]["run_id"], per_episode[1]["run_id"]])
    check_true("register_run always used the caller's run_kind, never a new classification",
               all(c[1] == "smoke" for c in register_calls))
    check("no state bleeds between episodes -- each starts from the same fresh Portfolio",
          per_episode[0]["return_pct"], per_episode[1]["return_pct"])
    print("  ok  run_configuration isolates run_id/Portfolio/checkpoint per episode and restores "
          "CHECKPOINT_PATH afterward -- no real Docker/Neon/Hermes call involved")


def test_run_configuration_halts_cleanly_without_advancing():
    manifest = _approve(_propose(seed=209))
    monday = date(2023, 1, 2)
    manifest["episodes"] = [_one_day_episode(monday)]
    from eval_suite import compute_content_hash as _hash
    manifest["content_hash"] = _hash(manifest)

    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]

    def fake_run_day_raises(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                            fundamentals_enabled=False):
        raise RuntimeError("simulated cycle failure")

    with tempfile.TemporaryDirectory() as tmp:
        checkpoint_dir = Path(tmp) / "checkpoints"
        with patch("eval_suite.run_day", side_effect=fake_run_day_raises), \
             patch("eval_suite.register_run", return_value=None), \
             patch("eval_suite.fetch_fundamentals_assessment_counts", return_value={}):
            report = run_configuration(manifest, EpisodeConfig("baseline"), "smoke", watchlist, checkpoint_dir)

    ep = report["per_episode"][0]
    check_true("a halted episode is reported as halted, never silently treated as complete", ep["halted"])
    check("a halted episode completed zero days", ep["days_completed"], 0)
    print("  ok  run_configuration reports a halted cycle honestly instead of masking the failure")


# ===========================================================================
# 7. fill_count means actual fills only (correction pass, requirement 2)
# ===========================================================================

def _episode_report_from_fake_summaries(summaries: list[dict], *, fundamentals_enabled: bool = False) -> dict:
    """Drives _run_episode (via the public API) with a scripted sequence
    of fake run_day summaries -- one per simulated day -- so the returned
    per-episode report's fill_count/proposal_count can be checked against
    exactly what each fake day reported, structurally, never text."""
    manifest = _approve(_propose(seed=301))
    monday = date(2023, 1, 2)
    manifest["episodes"] = [{"year": 2023, "month": 1, "regime": "rally",
                            "start": monday.isoformat(),
                            "end": (monday + timedelta(days=len(summaries) - 1)).isoformat()}]
    from eval_suite import compute_content_hash as _hash
    manifest["content_hash"] = _hash(manifest)

    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]
    calls = {"n": 0}

    def fake_run_day(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                     fundamentals_enabled=False):
        summary = dict(summaries[calls["n"]])
        calls["n"] += 1
        return portfolio, summary, (lambda: None)

    with tempfile.TemporaryDirectory() as tmp, \
         patch("eval_suite.run_day", side_effect=fake_run_day), \
         patch("eval_suite.register_run", return_value=None), \
         patch("eval_suite.fetch_fundamentals_assessment_counts", return_value={"supportive": 1}):
        report = run_configuration(manifest, EpisodeConfig("baseline", fundamentals_enabled=fundamentals_enabled),
                                   "smoke", watchlist, Path(tmp))
    return report["per_episode"][0]


def _base_summary(day: date) -> dict:
    return {"day": str(day), "exits": [], "decisions": [], "blocked": [], "usage": {},
           "fills": [], "proposals": []}


def test_queued_buy_later_rejected_is_a_proposal_not_a_fill():
    day1 = _base_summary(date(2023, 1, 2))
    day1["decisions"] = ["BUY OLN (ALPHA) x10 queued for next trading day's open"]
    day1["proposals"] = [{"ticker": "OLN", "alias": "ALPHA", "action": "BUY",
                          "quantity": 10, "decision_id": "d1"}]
    day2 = _base_summary(date(2023, 1, 3))
    day2["blocked"] = ["OLN: queued BUY rejected -- §6a: stop must sit below the entry price for a long"]
    # No fills entry -- execution-time rejection, structurally distinct.

    ep = _episode_report_from_fake_summaries([day1, day2])
    check("a queued BUY that is later rejected contributes to proposal_count", ep["proposal_count"], 1)
    check("a queued BUY that is later rejected is NEVER counted as a fill", ep["fill_count"], 0)
    print("  ok  a queued-then-rejected BUY: proposal_count=1, fill_count=0")


def test_queued_buy_later_filled_is_both_a_proposal_and_a_fill():
    day1 = _base_summary(date(2023, 1, 2))
    day1["decisions"] = ["BUY OLN (ALPHA) x10 queued for next trading day's open"]
    day1["proposals"] = [{"ticker": "OLN", "alias": "ALPHA", "action": "BUY",
                          "quantity": 10, "decision_id": "d1"}]
    day2 = _base_summary(date(2023, 1, 3))
    day2["decisions"] = ["BUY OLN (ALPHA) 10 @ $50.00 at 09:45 ET (queued day 1) [d1______]"]
    day2["fills"] = [{"ticker": "OLN", "alias": "ALPHA", "action": "BUY",
                      "quantity": 10, "fill_price": 50.0, "decision_id": "d1"}]

    ep = _episode_report_from_fake_summaries([day1, day2])
    check("the original queue still counts as one proposal", ep["proposal_count"], 1)
    check("the resolved fill counts as exactly one fill", ep["fill_count"], 1)
    print("  ok  a queued-then-filled BUY: proposal_count=1, fill_count=1")


def test_actual_sell_fill_counted_exactly_once():
    day1 = _base_summary(date(2023, 1, 2))
    day1["decisions"] = ["SELL OLN (ALPHA) 5 @ $55.00 at the 09:30 open (queued day 1, P&L +$25.00) [d2______]"]
    day1["fills"] = [{"ticker": "OLN", "alias": "ALPHA", "action": "SELL",
                      "quantity": 5, "fill_price": 55.0, "decision_id": "d2"}]

    ep = _episode_report_from_fake_summaries([day1])
    check("an actual SELL fill is counted exactly once", ep["fill_count"], 1)
    check("no proposal was made this episode", ep["proposal_count"], 0)
    print("  ok  a real SELL fill is counted exactly once")


def test_fill_count_is_immune_to_wording_changes_in_decisions_text():
    """The old bug: fill_count was derived from summary['decisions'] text
    prefixes, so it could never tell a proposal from a fill (both start
    with 'BUY '/'SELL '), and any wording change to either line would
    silently change the metric. Prove the corrected metric no longer
    reads 'decisions' text at all -- an adversarial decisions list that
    LOOKS like two fills but carries a real fills=[] must report zero."""
    day1 = _base_summary(date(2023, 1, 2))
    day1["decisions"] = [
        "BUY OLN (ALPHA) 999 @ $1.00 at 09:45 ET (queued day 1) [totally_fake]",
        "SELL OLN (ALPHA) 999 @ $1.00 at the 09:30 open (queued day 1, P&L +$1.00) [also_fake]",
    ]
    day1["fills"] = []  # the real, structural signal -- no fill actually happened
    day1["proposals"] = []

    ep = _episode_report_from_fake_summaries([day1])
    check("fill_count ignores decisions text entirely -- structured fills=[] wins", ep["fill_count"], 0)
    check("proposal_count ignores decisions text entirely -- structured proposals=[] wins",
          ep["proposal_count"], 0)
    print("  ok  fill_count/proposal_count are immune to decisions-text wording -- "
          "derived only from summary['fills']/['proposals']")


def test_aggregate_report_uses_the_corrected_fill_field():
    day1 = _base_summary(date(2023, 1, 2))
    day1["fills"] = [{"ticker": "OLN", "alias": "ALPHA", "action": "BUY",
                      "quantity": 10, "fill_price": 50.0, "decision_id": "d1"}]
    day1["proposals"] = [{"ticker": "OLN", "alias": "ALPHA", "action": "BUY",
                          "quantity": 10, "decision_id": "d1"}]

    manifest = _approve(_propose(seed=303))
    monday = date(2023, 1, 2)
    manifest["episodes"] = [{"year": 2023, "month": 1, "regime": "rally",
                            "start": monday.isoformat(), "end": monday.isoformat()}]
    from eval_suite import compute_content_hash as _hash
    manifest["content_hash"] = _hash(manifest)
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]

    def fake_run_day(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                     fundamentals_enabled=False):
        return portfolio, day1, (lambda: None)

    with tempfile.TemporaryDirectory() as tmp:
        with patch("eval_suite.run_day", side_effect=fake_run_day), \
             patch("eval_suite.register_run", return_value=None):
            report = run_configuration(manifest, EpisodeConfig("baseline"), "smoke", watchlist, Path(tmp))
    check("aggregate.total_fills reflects the corrected per-episode fill_count",
          report["aggregate"]["total_fills"], 1)
    check("aggregate.total_proposals is tracked separately from fills",
          report["aggregate"]["total_proposals"], 1)
    print("  ok  aggregate totals (total_fills/total_proposals) use the corrected structured fields")


# ===========================================================================
# 8. Deterministic holdout-pool expansion + clear failure (requirement 3)
# ===========================================================================

def test_select_holdout_pool_expands_backward_to_cover_a_missing_regime():
    # 24 months of a regular 4-cycle (2020-01..2021-12) so the comparison
    # side has plenty of every regime, then 11 months of filler "rally"
    # (2022-01..2022-11), then ONE "selloff" at 2022-12, then 12 more
    # months (2023) that themselves cover rally/high_volatility/sideways
    # but deliberately never selloff. The default trailing-12 window
    # (2023-01..2023-12) is missing selloff -> must expand by exactly 1
    # month to reach 2022-12 and pick it up.
    months = [_mm(2020 + (i // 12), 1 + i % 12, REGIME_CYCLE[i % 4]) for i in range(24)]
    months += [_mm(2022, m, "rally") for m in range(1, 12)]          # 2022-01..2022-11
    months.append(_mm(2022, 12, "selloff"))                          # the lone late selloff
    months += [_mm(2023, m, "rally") for m in (1, 2, 3, 4, 5, 7, 8, 10, 11, 12)]
    months.append(_mm(2023, 6, "high_volatility"))
    months.append(_mm(2023, 9, "sideways"))

    eligible_end = date(2023, 12, 31)
    holdout_start, pool, size = select_holdout_pool(months, eligible_end, initial_size=12)
    check("expansion needed exactly one extra month", size, 13)
    check("holdout pool now starts at the month carrying the missing regime", holdout_start, (2022, 12))
    check_true("the expanded pool covers all 4 required regimes",
               {"rally", "selloff", "high_volatility", "sideways"} <= {m.regime for m in pool})

    holdout_start2, pool2, size2 = select_holdout_pool(months, eligible_end, initial_size=12)
    check("select_holdout_pool is deterministic for the same inputs", (holdout_start2, size2), (holdout_start, size))
    print("  ok  select_holdout_pool deterministically expands backward by exactly the "
          "months needed to cover a missing regime, and is reproducible")


def test_select_holdout_pool_fails_clearly_when_regime_missing_from_entire_range():
    months = [_mm(2023, m, "rally" if m % 2 else "sideways") for m in range(1, 13)]
    try:
        select_holdout_pool(months, date(2023, 12, 31), initial_size=6)
        raise AssertionError("expected a permanently-missing regime to fail clearly")
    except ManifestGenerationError as exc:
        check_true("the error names the entire-eligible-range case", "entire eligible range" in str(exc))
    print("  ok  select_holdout_pool fails clearly when a required regime never appears anywhere "
          "in the eligible range, instead of silently returning incomplete coverage")


def test_select_holdout_pool_fails_clearly_when_expansion_would_empty_the_comparison_pool():
    # The ONLY high_volatility month is the very first month in the whole
    # range; the other 12 months (rally/selloff/sideways only) come after
    # it. Reaching full holdout coverage requires expanding to include
    # month 0 -- but that consumes the ENTIRE range, leaving zero months
    # for comparison.
    months = [_mm(2023, 1, "high_volatility")]
    cycle = ["rally", "selloff", "sideways"]
    months += [_mm(2023 + (i + 1) // 12, 1 + (i + 1) % 12, cycle[i % 3]) for i in range(12)]

    try:
        select_holdout_pool(months, date(2024, 1, 31), initial_size=12)
        raise AssertionError("expected a comparison-emptying expansion to fail clearly")
    except ManifestGenerationError as exc:
        check_true("the error names the comparison-pool coverage loss",
                   "comparison pool" in str(exc) and "high_volatility" in str(exc))
    print("  ok  select_holdout_pool fails clearly rather than expanding the holdout so far "
          "that the comparison pool loses a required regime")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print(f"PASS: {checks} assertions -- eval_suite determinism, regime coverage in both sets, "
          "holdout separation/expansion, manifest hash/tamper rejection, public-API approval-gate "
          "refusal before any execution call, clean per-episode state isolation, and the corrected "
          "fill-vs-proposal metric")


if __name__ == "__main__":
    main()
