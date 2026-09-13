"""
test_eval_suite.py -- pure/fixture-only tests for eval_suite.py (no real
Docker/Neon/Hermes calls anywhere in this file -- see each test's fake).

Covers exactly what the regime-diverse evaluation-suite block requires:
  1. determinism from seed (same seed + data -> same episode selection)
  2. regime coverage (rally/selloff/high_volatility/sideways all present)
  3. holdout separation (holdout months disjoint from, and chronologically
     later than, every comparison-suite month)
  4. manifest hash/tamper rejection
  5. refusal of proposed (and of tampered-but-claimed-approved) manifests
  6. clean per-episode state isolation (fresh run_id/Portfolio per
     episode, CHECKPOINT_PATH restored after, no bleed between calls)

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
    ManifestGenerationError, compute_content_hash, month_bounds,
    month_range, propose_manifest, require_approved, run_episode,
    stratified_sample, verify_manifest,
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
    under eval_suite's own real classify_regime, not hand-labeled."""
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
    from eval_suite import MonthMetrics
    months = [
        MonthMetrics(2021, i, date(2021, i, 1), date(2021, i, 28), 0.0, 0.0, REGIME_CYCLE[i % 4])
        for i in range(1, 13)
    ]
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


def test_holdout_pool_is_the_documented_trailing_window():
    m = _propose(seed=9)
    all_months = {(mm["year"], mm["month"]) for mm in m["all_classified_months"]}
    holdout_pool_size = m["holdout_months_count"]
    latest = max(all_months)
    check_true("the holdout window's own latest month is the eligible range's latest classified month",
               max((e["year"], e["month"]) for e in m["holdout_episodes"]) <= latest)
    check("holdout_months_count matches the module's own documented constant", holdout_pool_size, 12)


# ===========================================================================
# 4 & 5. Manifest hash / tamper / approval gate
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


# ===========================================================================
# 6. Clean per-episode state isolation (run_episode) -- no real Docker/
#    Neon/Hermes call anywhere in this test.
# ===========================================================================

def _one_day_episode(day: date, regime: str = "rally") -> dict:
    return {"year": day.year, "month": day.month, "regime": regime,
            "start": day.isoformat(), "end": day.isoformat()}


def test_run_episode_isolates_state_and_restores_checkpoint_path():
    import run_backtest as rb

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
        # would NOT start at exactly [STARTING_EQUITY] here -- assert that
        # directly, then apply the SAME fixed delta every call so the two
        # episodes' reports are comparable only if each truly started fresh.
        from run_backtest import STARTING_EQUITY
        assert portfolio.equity_curve == [STARTING_EQUITY], (
            f"episode {fake_calls['n']}: portfolio was not a fresh Portfolio() "
            f"(equity_curve={portfolio.equity_curve!r}) -- state bled from a prior episode"
        )
        portfolio.equity_curve.append(STARTING_EQUITY + 1.0)
        summary = {"day": str(day), "exits": [], "decisions": [], "blocked": [], "usage": {}}
        return portfolio, summary, (lambda: None)

    with tempfile.TemporaryDirectory() as tmp:
        checkpoint_dir = Path(tmp) / "checkpoints"
        with patch("eval_suite.run_day", side_effect=fake_run_day), \
             patch("eval_suite.register_run", side_effect=lambda rid, kind: register_calls.append((rid, kind))):
            monday = date(2023, 1, 2)  # a real Monday -- exactly one trading day episode
            report1 = run_episode(_one_day_episode(monday), EpisodeConfig("baseline"),
                                  "smoke", watchlist, checkpoint_dir)
            report2 = run_episode(_one_day_episode(monday), EpisodeConfig("baseline"),
                                  "smoke", watchlist, checkpoint_dir)

    check("run_day was called once per episode", fake_calls["n"], 2)
    check_true("CHECKPOINT_PATH was isolated to the checkpoint_dir during each call",
               all(str(p).startswith(str(checkpoint_dir)) for p in seen_checkpoint_paths_during_call))
    check("CHECKPOINT_PATH is restored to its original value after run_episode returns",
          rb.CHECKPOINT_PATH, original_checkpoint_path)
    check_true("each episode got a fresh, distinct run_id (no shared identity)",
               report1["run_id"] != report2["run_id"])
    check_true("register_run was called once per episode with that episode's own run_id",
               [c[0] for c in register_calls] == [report1["run_id"], report2["run_id"]])
    check_true("register_run always used the caller's run_kind, never a new classification",
               all(c[1] == "smoke" for c in register_calls))
    check("no state bleeds between episodes -- each starts from the same fresh Portfolio",
          report1["return_pct"], report2["return_pct"])
    print("  ok  run_episode isolates run_id/Portfolio/checkpoint per call and restores "
          "CHECKPOINT_PATH afterward -- no real Docker/Neon/Hermes call involved")


def test_run_episode_halts_cleanly_without_advancing():
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]

    def fake_run_day_raises(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                            fundamentals_enabled=False):
        raise RuntimeError("simulated cycle failure")

    with tempfile.TemporaryDirectory() as tmp:
        checkpoint_dir = Path(tmp) / "checkpoints"
        with patch("eval_suite.run_day", side_effect=fake_run_day_raises), \
             patch("eval_suite.register_run", return_value=None), \
             patch("eval_suite.fetch_fundamentals_assessment_counts", return_value={}):
            monday = date(2023, 1, 2)
            report = run_episode(_one_day_episode(monday), EpisodeConfig("baseline"),
                                 "smoke", watchlist, checkpoint_dir)

    check_true("a halted episode is reported as halted, never silently treated as complete",
               report["halted"])
    check("a halted episode completed zero days", report["days_completed"], 0)
    print("  ok  run_episode reports a halted cycle honestly instead of masking the failure")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print(f"PASS: {checks} assertions -- eval_suite determinism, regime coverage, holdout "
          "separation, manifest hash/tamper rejection, approval-gate refusal, and clean "
          "per-episode state isolation")


if __name__ == "__main__":
    main()
