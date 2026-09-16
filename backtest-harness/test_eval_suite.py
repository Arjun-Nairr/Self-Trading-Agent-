"""
test_eval_suite.py -- pure/fixture-only tests for eval_suite.py (no real
Docker/Neon/Hermes calls anywhere in this file -- see each test's fake).

Covers exactly what the regime-diverse evaluation-suite block requires,
including the 2026-09-13 correction passes (approval/fill-metric/holdout-
coverage, then regime-model/watchlist-viability):
  1. determinism from seed (same seed + data -> same episode selection)
  2. regime coverage (rally/selloff/high_volatility/sideways each have
     EXACTLY one comparison and one holdout episode)
  3. holdout separation (2026-09-16: per-regime paired design -- each
     regime's holdout episode is disjoint from, and chronologically later
     than, its OWN matching comparison episode; never a claim that every
     holdout month is later than every comparison month globally; no
     episode date appears twice across all 8 selected episodes)
  4. manifest hash/tamper rejection
  5. refusal of proposed (and of tampered-but-claimed-approved) manifests
     -- through the PUBLIC API (run_configuration), proving zero calls
     into register_run/run_day/journal access before validation
  6. clean per-episode state isolation (fresh run_id/Portfolio per
     episode, CHECKPOINT_PATH restored after, no bleed between calls)
  7. fill_count means actual fills only, never queued proposals
  8. select_paired_episodes: deterministic per-regime pairing (forced
     chronological order when a regime has exactly 2 eligible months, a
     seeded draw among more than 2), and clear failure when a regime has
     fewer than 2 eligible months to pair
  9. "sideways" requires a genuinely quiet month (not a residual bucket);
     a moderately drifting calm month is "unclassified" and excluded
     from both sampled sets, which still cover all 4 required regimes
  10. fixed-watchlist viability audit: data-availability failures fail
      manifest generation clearly (naming the ticker/month); low
      movement never fails it; tampering with the audit is caught by
      the existing hash check; an approved, hash-consistent manifest
      missing/failing its audit is still refused by the public API
      before any execution-side call

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
    EpisodeConfig, EVAL_RUN_KIND, MANIFEST_VERSION, ManifestNotApprovedError,
    ManifestTamperedError, ManifestGenerationError, ManifestVersionError,
    ManifestWatchlistViabilityError, MonthMetrics,
    classify_regime, compute_content_hash, month_bounds, month_range,
    propose_manifest, require_approved, run_configuration,
    select_paired_episodes, verify_manifest,
)

ELIGIBLE_START = date(2021, 1, 1)
ELIGIBLE_END = date(2023, 12, 31)  # 3 years -- plenty of months, fast tests

REGIME_CYCLE = ["rally", "selloff", "high_volatility", "sideways"]

# A small fixture watchlist -- shape-identical to the real
# watchlist/watchlist.json (correction pass, part 2), never the real
# 10-name file (these tests do zero real I/O).
FIXTURE_WATCHLIST = [
    {"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"},
    {"symbol": "WMG", "name": "Warner Music Group Corp.", "sector": "Communication Services"},
]


def _fake_fetch_tech(tickers: list[str], as_of_day: date) -> dict[str, dict]:
    """Every ticker always has technical-analysis data available."""
    return {t: {"price": 100.0} for t in tickers}


def _fake_fetch_watchlist_closes(tickers: list[str], start: date, end: date) -> dict[str, dict[str, float]]:
    """Reuses the same deterministic SPY-shaped generator for every
    watchlist ticker too -- movement description only, never gates."""
    closes = build_synthetic_closes(start, end)
    return {t: dict(closes) for t in tickers}


def _valid_watchlist_viability(watchlist: list[dict] = FIXTURE_WATCHLIST) -> dict:
    """Minimal always-passing stub for tests that hand-build a one-off
    episode dict and are exercising something OTHER than the viability
    audit itself (that has its own dedicated tests below) -- mirrors the
    exact shape audit_episode_watchlist_viability produces."""
    tickers = sorted(t["symbol"] for t in watchlist)
    return {
        "checked_dates": {"start": "2023-01-02", "end": "2023-01-02"},
        "usable_count": len(tickers), "unavailable_count": 0, "unavailable_tickers": [],
        "per_ticker": {t: {"usable": True, "start_error": None, "end_error": None} for t in tickers},
        "movement": {"per_ticker": {}, "return_pct_range": None, "vol_pct_range": None},
        "valid": True,
    }


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


def _propose(seed: int, start: date = ELIGIBLE_START, end: date = ELIGIBLE_END,
            watchlist: list[dict] | None = None, fetch_tech=None,
            fetch_watchlist_closes=None) -> dict:
    return propose_manifest(
        seed=seed, eligible_start=start, eligible_end=end, fetch_closes=_fake_fetch_closes,
        watchlist=watchlist if watchlist is not None else FIXTURE_WATCHLIST,
        fetch_tech=fetch_tech or _fake_fetch_tech,
        fetch_watchlist_closes=fetch_watchlist_closes or _fake_fetch_watchlist_closes,
    )


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


def test_select_paired_episodes_pure_determinism():
    months = [_mm(2021, i, REGIME_CYCLE[i % 4]) for i in range(1, 13)]
    a = select_paired_episodes(months, seed=42)
    b = select_paired_episodes(months, seed=42)
    check("select_paired_episodes is a pure function of its inputs", a, b)


# ===========================================================================
# 2. Regime coverage
# ===========================================================================

def test_regime_coverage_comparison_and_holdout():
    m = _propose(seed=123)
    comparison_regimes = [e["regime"] for e in m["episodes"]]
    holdout_regimes = [e["regime"] for e in m["holdout_episodes"]]
    for regime in ("rally", "selloff", "high_volatility", "sideways"):
        check(f"comparison suite has EXACTLY one {regime!r} episode",
              comparison_regimes.count(regime), 1)
        check(f"holdout set has EXACTLY one {regime!r} episode",
              holdout_regimes.count(regime), 1)
    check("exactly 4 comparison episodes total", len(m["episodes"]), 4)
    check("exactly 4 holdout episodes total", len(m["holdout_episodes"]), 4)


# ===========================================================================
# 3. Holdout separation
# ===========================================================================

def test_holdout_months_disjoint_and_each_later_than_its_own_pair():
    m = _propose(seed=5)
    comparison_keys = {(e["year"], e["month"]) for e in m["episodes"]}
    holdout_keys = {(e["year"], e["month"]) for e in m["holdout_episodes"]}
    check("comparison and holdout episode months never overlap",
          comparison_keys & holdout_keys, set())
    by_pair = {e["pair_id"]: e for e in m["episodes"]}
    for h in m["holdout_episodes"]:
        c = by_pair[h["pair_id"]]
        check_true(f"{h['pair_id']} holdout is strictly later than its own matching comparison",
                   (h["year"], h["month"]) > (c["year"], c["month"]))
    # The design deliberately does NOT require every holdout month to be
    # later than every comparison month globally -- only proving that
    # would silently reintroduce the old, infeasible invariant.


def test_no_episode_date_appears_twice():
    m = _propose(seed=9)
    all_keys = [(e["year"], e["month"]) for e in m["episodes"] + m["holdout_episodes"]]
    check("all 8 selected episode dates are distinct", len(all_keys), len(set(all_keys)))


def test_holdout_design_recorded_with_the_per_regime_caveat():
    m = _propose(seed=9)
    hd = m["holdout_design"]
    check("holdout_design.method records the per-regime paired approach",
          hd["method"], "per_regime_paired")
    check_true("holdout_design states the caveat that this is not one global trailing holdout",
               "NOT one single globally-trailing holdout" in hd["caveat"])
    for e in m["episodes"] + m["holdout_episodes"]:
        check_true(f"episode {e['year']}-{e['month']:02d} records its role",
                   e["role"] in ("comparison", "holdout"))
        check_true(f"episode {e['year']}-{e['month']:02d} records its matching pair_id",
                   e["pair_id"] == f"{e['regime']}_pair")


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
            "start": day.isoformat(), "end": day.isoformat(),
            "watchlist_viability": _valid_watchlist_viability()}


def _approve(manifest: dict) -> dict:
    approved = dict(manifest)
    approved["status"] = "approved"
    return approved


def test_public_api_refuses_non_evaluation_run_kind_before_any_execution_call():
    """Correction pass item E (prior audit M2): an approved, valid manifest
    must still be refused if the caller passes anything other than
    EVAL_RUN_KIND ('evaluation') -- 'training' would be indistinguishable
    from a real run in the dashboard's training-run selector, 'smoke'
    mislabels genuine evaluation data as an infrastructure test. Checked
    BEFORE require_approved even runs, so zero register_run/run_day/
    journal-access calls happen for either wrong value."""
    m = _approve(_propose(seed=205))
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]
    for wrong_kind in ("training", "smoke", "archived"):
        register_calls, run_day_calls = [], []
        with tempfile.TemporaryDirectory() as tmp, \
             patch("eval_suite.register_run", side_effect=lambda *a: register_calls.append(a)), \
             patch("eval_suite.run_day", side_effect=lambda *a, **k: run_day_calls.append(a)):
            try:
                run_configuration(m, EpisodeConfig("baseline"), wrong_kind, watchlist, Path(tmp))
                raise AssertionError(f"expected run_kind={wrong_kind!r} to be refused")
            except ValueError as exc:
                assert "evaluation" in str(exc) and wrong_kind in str(exc)
        check(f"zero register_run calls for run_kind={wrong_kind!r}", register_calls, [])
        check(f"zero run_day calls for run_kind={wrong_kind!r}", run_day_calls, [])
    print("  ok  run_configuration refuses 'training'/'smoke'/'archived' as run_kind, "
          "before any register_run/run_day call")


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
            run_configuration(m, EpisodeConfig("baseline"), EVAL_RUN_KIND, watchlist, Path(tmp))
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
            run_configuration(tampered, EpisodeConfig("baseline"), EVAL_RUN_KIND, watchlist, Path(tmp))
            raise AssertionError("expected a tampered-but-'approved' manifest to be refused")
        except ManifestTamperedError:
            pass
    check("zero register_run calls for a tampered manifest", register_calls, [])
    check("zero run_day calls for a tampered manifest", run_day_calls, [])
    check("zero journal-access calls for a tampered manifest", journal_calls, [])
    print("  ok  run_configuration (the public API) refuses a tampered-but-claimed-approved "
          "manifest before any register_run/run_day/journal-access call")


def test_public_api_refuses_a_version_mismatched_manifest_before_any_execution_call():
    """Correction pass item E.3 (prior audit L4): manifest_version is
    written and content-hashed but was never actually COMPARED against
    the running code's MANIFEST_VERSION anywhere -- an old-version
    manifest (generated, reviewed, and approved under DIFFERENT
    classification semantics) must be refused, not silently executed
    under today's code."""
    stale = _approve(_propose(seed=207))
    stale["manifest_version"] = MANIFEST_VERSION - 1
    stale["content_hash"] = compute_content_hash(stale)  # re-sign so this is a version
    # mismatch specifically, not also a tamper/hash failure -- isolates the check.
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]
    register_calls, run_day_calls = [], []
    with tempfile.TemporaryDirectory() as tmp, \
         patch("eval_suite.register_run", side_effect=lambda *a: register_calls.append(a)), \
         patch("eval_suite.run_day", side_effect=lambda *a, **k: run_day_calls.append(a)):
        try:
            run_configuration(stale, EpisodeConfig("baseline"), EVAL_RUN_KIND, watchlist, Path(tmp))
            raise AssertionError("expected a version-mismatched manifest to be refused")
        except ManifestVersionError as exc:
            assert str(MANIFEST_VERSION - 1) in str(exc) and str(MANIFEST_VERSION) in str(exc)
    check("zero register_run calls for a version-mismatched manifest", register_calls, [])
    check("zero run_day calls for a version-mismatched manifest", run_day_calls, [])
    print("  ok  run_configuration refuses a manifest_version mismatch, even when "
          "genuinely approved and hash-consistent, before any execution-side call")


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
        report = run_configuration(m, EpisodeConfig("baseline"), EVAL_RUN_KIND, watchlist, Path(tmp))
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
            report = run_configuration(manifest, EpisodeConfig("baseline"), EVAL_RUN_KIND, watchlist, checkpoint_dir)

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
               all(c[1] == EVAL_RUN_KIND for c in register_calls))
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

    fake_fail_logger_calls = []

    def fake_fail_logger(day_label, error_text, run_id, usage=None):
        fake_fail_logger_calls.append({"day_label": day_label, "run_id": run_id})

    with tempfile.TemporaryDirectory() as tmp:
        checkpoint_dir = Path(tmp) / "checkpoints"
        with patch("eval_suite.run_day", side_effect=fake_run_day_raises), \
             patch("eval_suite.register_run", return_value=None), \
             patch("eval_suite.fetch_fundamentals_assessment_counts", return_value={}), \
             patch("eval_suite.log_cycle_failure", side_effect=fake_fail_logger):
            report = run_configuration(manifest, EpisodeConfig("baseline"), EVAL_RUN_KIND, watchlist, checkpoint_dir)

    ep = report["per_episode"][0]
    check_true("a halted episode is reported as halted, never silently treated as complete", ep["halted"])
    check("a halted episode completed zero days", ep["days_completed"], 0)
    check("the FAKE fail logger (not the real log_cycle_failure) received the failure event "
          "-- no real Docker/Neon call made", len(fake_fail_logger_calls), 1)
    print("  ok  run_configuration reports a halted cycle honestly instead of masking the failure, "
          "without ever reaching real Docker/Neon")


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
                            "end": (monday + timedelta(days=len(summaries) - 1)).isoformat(),
                            "watchlist_viability": _valid_watchlist_viability()}]
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
                                   EVAL_RUN_KIND, watchlist, Path(tmp))
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
                            "start": monday.isoformat(), "end": monday.isoformat(),
                            "watchlist_viability": _valid_watchlist_viability()}]
    from eval_suite import compute_content_hash as _hash
    manifest["content_hash"] = _hash(manifest)
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]

    def fake_run_day(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                     fundamentals_enabled=False):
        return portfolio, day1, (lambda: None)

    with tempfile.TemporaryDirectory() as tmp:
        with patch("eval_suite.run_day", side_effect=fake_run_day), \
             patch("eval_suite.register_run", return_value=None):
            report = run_configuration(manifest, EpisodeConfig("baseline"), EVAL_RUN_KIND, watchlist, Path(tmp))
    check("aggregate.total_fills reflects the corrected per-episode fill_count",
          report["aggregate"]["total_fills"], 1)
    check("aggregate.total_proposals is tracked separately from fills",
          report["aggregate"]["total_proposals"], 1)
    print("  ok  aggregate totals (total_fills/total_proposals) use the corrected structured fields")


# ===========================================================================
# 7b. Correction pass item E.2 (prior audit M1): dangling pending orders
#     at episode end
# ===========================================================================

def test_episode_resolves_a_dangling_pending_order_at_its_own_end():
    """An episode never resumes -- a decision queued on its own last
    simulated day would otherwise sit 'pending' in the journal forever,
    since no later cycle ever comes along to resolve it (unlike the main
    harness's own next invocation). Must be resolved honestly as rejected,
    reusing resolve_trade_rejection, and reported in the episode's own
    result -- never silently left dangling."""
    from run_backtest import QueuedOrder

    manifest = _approve(_propose(seed=307))
    monday = date(2023, 1, 2)
    manifest["episodes"] = [{"year": 2023, "month": 1, "regime": "rally",
                            "start": monday.isoformat(), "end": monday.isoformat(),
                            "watchlist_viability": _valid_watchlist_viability()}]
    from eval_suite import compute_content_hash as _hash
    manifest["content_hash"] = _hash(manifest)
    watchlist = [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}]

    def fake_run_day(day, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                     fundamentals_enabled=False):
        # Mirrors what a real queued BUY on the episode's own final day
        # leaves behind: an unresolved order in portfolio.pending_orders.
        portfolio.pending_orders.append(QueuedOrder(
            ticker="OLN", alias=masking.alias("OLN"), action="BUY", quantity=10,
            stop_price=90.0, target_price=110.0, entry_price=100.0,
            queued_day_index=day_index, decision={}, decision_id="dangling-decision-id",
        ))
        return portfolio, _base_summary(day), (lambda: None)

    resolved_calls = []
    with tempfile.TemporaryDirectory() as tmp, \
         patch("eval_suite.run_day", side_effect=fake_run_day), \
         patch("eval_suite.register_run", return_value=None), \
         patch("eval_suite.resolve_trade_rejection",
              side_effect=lambda decision_id, reason: resolved_calls.append((decision_id, reason))):
        report = run_configuration(manifest, EpisodeConfig("baseline"), EVAL_RUN_KIND, watchlist, Path(tmp))

    ep = report["per_episode"][0]
    check("exactly the one dangling order is resolved", len(resolved_calls), 1)
    check("resolved with the order's own decision_id", resolved_calls[0][0], "dangling-decision-id")
    check_true("resolved with an honest, episode-specific reason",
              "episode" in resolved_calls[0][1] and "ended" in resolved_calls[0][1])
    check("the episode's own report names how many it drained",
          ep["dangling_pending_orders_resolved"], 1)
    print("  ok  a pending order still open at episode end is resolved as rejected "
          "(never left dangling) and reported in the episode result")


def test_episode_with_nothing_pending_reports_zero_dangling_orders():
    """The ordinary case -- most episodes end with nothing queued -- must
    report 0, not merely omit the field, so its absence is never mistaken
    for "not checked"."""
    day = _base_summary(date(2023, 1, 2))
    ep = _episode_report_from_fake_summaries([day])
    check("an ordinary episode reports zero dangling pending orders",
          ep["dangling_pending_orders_resolved"], 0)
    print("  ok  an episode with nothing pending explicitly reports 0, not an absent field")


# ===========================================================================
# 8. select_paired_episodes: deterministic per-regime pairing + clear
#    failure on under-coverage (2026-09-16 correction pass)
# ===========================================================================

def test_select_paired_episodes_forces_the_pair_when_exactly_two_months_exist():
    # Exactly today's real situation: only 2 real 'selloff' months exist
    # anywhere in the eligible range, chronologically far apart, with
    # 'sideways' months only appearing later still -- the OLD design
    # (select_holdout_pool) made this combination infeasible; the paired
    # design does not care where 'sideways' months fall at all.
    months = [_mm(2021, 3, "selloff"), _mm(2022, 9, "selloff")]
    months += [_mm(2021, i, r) for i, r in
              zip(range(1, 7), ["rally", "rally", "high_volatility", "high_volatility",
                                 "sideways", "sideways"])]
    pairs = select_paired_episodes(months, seed=1)
    comparison, holdout = pairs["selloff"]
    check("the only 2 eligible months of a regime are forced into the pair, earlier as comparison",
          (comparison.year, comparison.month, holdout.year, holdout.month), (2021, 3, 2022, 9))
    pairs_other_seed = select_paired_episodes(months, seed=999)
    check("a forced 2-month pair is identical regardless of seed -- never hand-picked",
          pairs_other_seed["selloff"], pairs["selloff"])
    print("  ok  select_paired_episodes forces the only 2 eligible months of a regime into the "
          "pair, in chronological order, independent of seed")


def test_select_paired_episodes_differs_for_another_seed_when_alternatives_exist():
    # 5 eligible 'rally' months -- more than 2, so WHICH 2 get paired is
    # genuinely seed-sensitive (10 possible pairs).
    months = [_mm(2021, i, "rally") for i in range(1, 6)]
    months += [_mm(2021, 6, "selloff"), _mm(2022, 6, "selloff")]
    months += [_mm(2021, 7, "high_volatility"), _mm(2022, 7, "high_volatility")]
    months += [_mm(2021, 8, "sideways"), _mm(2022, 8, "sideways")]
    seen = {select_paired_episodes(months, seed=s)["rally"] for s in range(20)}
    check_true("different seeds are capable of selecting a different pair when more than 2 "
               "eligible months exist for a regime", len(seen) > 1)
    print("  ok  select_paired_episodes' choice of which months to pair is seed-sensitive "
          "when alternatives genuinely exist")


def test_select_paired_episodes_holdout_always_later_than_its_own_comparison():
    months = [_mm(2021, i, "rally") for i in range(1, 6)]
    months += [_mm(2021, 6, "selloff"), _mm(2022, 6, "selloff")]
    months += [_mm(2021, 7, "high_volatility"), _mm(2022, 7, "high_volatility")]
    months += [_mm(2021, 8, "sideways"), _mm(2022, 8, "sideways")]
    for seed in range(10):
        pairs = select_paired_episodes(months, seed=seed)
        for regime, (comparison, holdout) in pairs.items():
            check_true(f"{regime} holdout is strictly later than its own comparison (seed={seed})",
                       (holdout.year, holdout.month) > (comparison.year, comparison.month))
    print("  ok  every regime's holdout episode is strictly later than its own matching "
          "comparison episode, across every seed tried")


def test_select_paired_episodes_fails_clearly_when_a_regime_has_fewer_than_two_months():
    months = [_mm(2021, i, "rally") for i in range(1, 6)]
    months += [_mm(2021, 6, "selloff")]  # only ONE selloff month -- cannot form a pair
    months += [_mm(2021, 7, "high_volatility"), _mm(2022, 7, "high_volatility")]
    months += [_mm(2021, 8, "sideways"), _mm(2022, 8, "sideways")]
    try:
        select_paired_episodes(months, seed=1)
        raise AssertionError("expected a regime with fewer than 2 eligible months to fail clearly")
    except ManifestGenerationError as exc:
        check_true("the error names the under-covered regime", "'selloff'" in str(exc))
        check_true("the error states the real count found", "only 1 eligible month" in str(exc))
    print("  ok  select_paired_episodes fails clearly, naming the regime and its real count, "
          "when fewer than 2 eligible months exist -- never fabricates a second month")


# ===========================================================================
# 9. "sideways" is a genuinely quiet regime, not a residual bucket
#    (correction pass part 1)
# ===========================================================================

def test_classify_regime_moderate_drift_becomes_unclassified():
    check("a +3% return with low volatility is too far from flat to be "
          "sideways, and too small to be a rally -- unclassified",
          classify_regime(monthly_return_pct=3.0, realized_vol_pct=0.5), "unclassified")
    check("a -3% return with low volatility is likewise unclassified, not sideways",
          classify_regime(monthly_return_pct=-3.0, realized_vol_pct=0.5), "unclassified")
    print("  ok  a moderately drifting, calm month is 'unclassified', never 'sideways'")


def test_classify_regime_genuinely_flat_calm_month_is_sideways():
    check("a +0.5% return with low volatility is genuinely quiet -- sideways",
          classify_regime(monthly_return_pct=0.5, realized_vol_pct=0.5), "sideways")
    check("boundary: vol exactly at the sideways threshold is excluded (>= high_vol side wins "
          "only above 1.5; between 1.0 and 1.5 it is unclassified, not sideways)",
          classify_regime(monthly_return_pct=0.5, realized_vol_pct=1.0), "unclassified")
    print("  ok  a genuinely flat, calm month is 'sideways'")


REGIME_CYCLE_WITH_UNCLASSIFIED = ["rally", "selloff", "high_volatility", "sideways", "unclassified"]


def build_synthetic_closes_with_unclassified(start: date, end: date) -> dict[str, float]:
    """Same construction as build_synthetic_closes, but a 5-month cycle
    that ALSO deliberately produces a genuinely unclassified month each
    cycle: a moderate, calm drift (~+3%/month, near-zero daily variance)
    -- too big to be sideways, too small to be a rally, under
    classify_regime's own real rule (not hand-labeled)."""
    closes: dict[str, float] = {}
    price = 100.0
    for i, (y, m) in enumerate(month_range(start, end)):
        regime = REGIME_CYCLE_WITH_UNCLASSIFIED[i % 5]
        m_start, m_end = month_bounds(y, m)
        days = _weekdays_in(max(m_start, start), min(m_end, end))
        for j, d in enumerate(days):
            if regime == "rally":
                price *= 1.004
            elif regime == "selloff":
                price *= 0.996
            elif regime == "high_volatility":
                price *= 1.03 if j % 2 == 0 else 0.972
            elif regime == "sideways":
                price *= 1.001 if j % 2 == 0 else 0.999
            else:  # "unclassified" -- moderate, calm drift
                price *= 1.0015
            closes[d.isoformat()] = round(price, 6)
    return closes


def test_regime_sets_exclude_unclassified_months():
    fetch = lambda t, s, e: build_synthetic_closes_with_unclassified(s, e)  # noqa: E731
    m = propose_manifest(seed=501, eligible_start=ELIGIBLE_START, eligible_end=ELIGIBLE_END,
                         fetch_closes=fetch, watchlist=FIXTURE_WATCHLIST,
                         fetch_tech=_fake_fetch_tech, fetch_watchlist_closes=_fake_fetch_watchlist_closes)
    check_true("this fixture genuinely produces some unclassified months",
               any(mm["regime"] == "unclassified" for mm in m["all_classified_months"]))
    comparison_regimes = {e["regime"] for e in m["episodes"]}
    holdout_regimes = {e["regime"] for e in m["holdout_episodes"]}
    check_true("comparison episodes never include an unclassified month",
               "unclassified" not in comparison_regimes)
    check_true("holdout episodes never include an unclassified month",
               "unclassified" not in holdout_regimes)
    for regime in ("rally", "selloff", "high_volatility", "sideways"):
        check_true(f"comparison suite still covers {regime!r} despite unclassified months present",
                   regime in comparison_regimes)
        check_true(f"holdout set still covers {regime!r} despite unclassified months present",
                   regime in holdout_regimes)
    print("  ok  unclassified months are recorded in all_classified_months but excluded from both "
          "sampled sets, which still cover all 4 required regimes")


# ===========================================================================
# 10. Fixed-watchlist viability audit (correction pass part 2)
# ===========================================================================

def _fetch_tech_one_ticker_always_fails(tickers: list[str], as_of_day: date) -> dict[str, dict]:
    out = {t: {"price": 100.0} for t in tickers}
    out[FIXTURE_WATCHLIST[0]["symbol"]] = {"error": "simulated: not enough trailing history"}
    return out


def test_watchlist_audit_fails_manifest_generation_for_missing_ticker_data():
    try:
        _propose(seed=601, fetch_tech=_fetch_tech_one_ticker_always_fails)
        raise AssertionError("expected a permanently-failing watchlist ticker to fail manifest generation")
    except ManifestGenerationError as exc:
        check_true("the error names the failing ticker",
                   FIXTURE_WATCHLIST[0]["symbol"] in str(exc))
        check_true("the error explains this is a data-availability failure, not a movement judgement",
                   "point-in-time data" in str(exc))
    print("  ok  propose_manifest fails clearly and names the failing ticker when the fixed "
          "watchlist lacks enough point-in-time data for the existing technical-analysis path")


def _fetch_watchlist_closes_quiet(tickers: list[str], start: date, end: date) -> dict[str, dict[str, float]]:
    """Every ticker completely flat -- zero net return, zero variance.
    Must NOT be rejected: viability is a data-availability check only."""
    days = _weekdays_in(start, end)
    closes = {d.isoformat(): 100.0 for d in days}
    return {t: dict(closes) for t in tickers}


def test_watchlist_audit_passes_for_quiet_but_complete_data():
    m = _propose(seed=603, fetch_tech=_fake_fetch_tech,
                fetch_watchlist_closes=_fetch_watchlist_closes_quiet)
    for ep in m["episodes"] + m["holdout_episodes"]:
        wv = ep["watchlist_viability"]
        label = f"{ep['year']}-{ep['month']:02d}"
        check_true(f"{label} watchlist_viability is valid despite zero movement", wv["valid"])
        check(f"{label} has no unavailable tickers", wv["unavailable_tickers"], [])
        for sym, mv in wv["movement"]["per_ticker"].items():
            if mv is not None:
                check_true(f"{label} {sym} movement is genuinely near zero in this fixture",
                           abs(mv["return_pct"]) < 0.01 and mv["vol_pct"] < 0.01)
    print("  ok  quiet-but-complete watchlist data passes viability -- low movement never "
          "invalidates an episode")


def test_watchlist_viability_tampering_invalidates_manifest():
    m = _propose(seed=605)
    tampered = copy.deepcopy(m)
    tampered["episodes"][0]["watchlist_viability"]["unavailable_tickers"].append("FAKE")
    try:
        verify_manifest(tampered)
        raise AssertionError("expected a mutated watchlist_viability field to fail verification")
    except ManifestTamperedError:
        pass
    print("  ok  verify_manifest rejects a manifest whose watchlist_viability was tampered with "
          "(content hash covers the audit results, same as every other substantive field)")


def test_public_api_refuses_manifest_missing_valid_watchlist_viability_before_any_execution_call():
    """Correction pass part 2's integration requirement -- a manifest can
    be perfectly hash-consistent AND genuinely 'approved' and STILL be
    refused if its watchlist_viability audit is missing or failing.
    Proven through the PUBLIC API, same discipline as the approval-gate
    tests in section 5: zero register_run/run_day/journal calls."""
    m = _approve(_propose(seed=607))

    missing = copy.deepcopy(m)
    del missing["episodes"][0]["watchlist_viability"]
    missing["content_hash"] = compute_content_hash(missing)  # hash-consistent -- NOT tampering

    failing = copy.deepcopy(m)
    failing["episodes"][0]["watchlist_viability"]["valid"] = False
    failing["episodes"][0]["watchlist_viability"]["unavailable_tickers"] = ["OLN"]
    failing["content_hash"] = compute_content_hash(failing)

    for label, bad_manifest in (("missing", missing), ("failing", failing)):
        register_calls, run_day_calls, journal_calls = [], [], []
        with tempfile.TemporaryDirectory() as tmp, \
             patch("eval_suite.register_run", side_effect=lambda *a: register_calls.append(a)), \
             patch("eval_suite.run_day", side_effect=lambda *a, **k: run_day_calls.append(a)), \
             patch("eval_suite.fetch_fundamentals_assessment_counts",
                   side_effect=lambda rid: journal_calls.append(rid) or {}):
            try:
                run_configuration(bad_manifest, EpisodeConfig("baseline"), EVAL_RUN_KIND,
                                  FIXTURE_WATCHLIST, Path(tmp))
                raise AssertionError(f"expected a {label} watchlist_viability audit to be refused")
            except ManifestWatchlistViabilityError:
                pass
        check(f"zero register_run calls ({label} audit)", register_calls, [])
        check(f"zero run_day calls ({label} audit)", run_day_calls, [])
        check(f"zero journal-access calls ({label} audit)", journal_calls, [])
    print("  ok  run_configuration refuses an approved, hash-consistent manifest whose "
          "watchlist_viability audit is missing OR failing, before any execution-side call")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print(f"PASS: {checks} assertions -- eval_suite determinism, regime coverage in both sets "
          "(genuinely-quiet sideways vs. excluded unclassified months), holdout separation/expansion, "
          "manifest hash/tamper rejection, public-API approval-gate and watchlist-viability-gate "
          "refusal before any execution call, clean per-episode state isolation, the corrected "
          "fill-vs-proposal metric, and the fixed-watchlist data-availability audit")


if __name__ == "__main__":
    main()
