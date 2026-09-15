"""
eval_suite.py -- regime-diverse evaluation-suite infrastructure (offline).

`docs/PROJECT_BACKLOG.md` §4's "Regime-diverse sampled evaluation suite"
item -- a *prerequisite for* Bayesian optimization, not an approved
experiment. Implements two DELIBERATELY SEPARATE capabilities:

1. MANIFEST GENERATION -- propose_manifest(): classifies SPY's real
   monthly return/volatility over a fixed eligible historical range,
   stratified-samples a comparison suite plus a separate, later, untouched
   holdout set, and returns a versioned, hashed, content-addressed
   manifest. Its "status" is ALWAYS "proposed" here -- a human reviews it
   and flips status to "approved" (a one-line edit on the saved JSON file;
   the content hash deliberately excludes "status" so approval is never
   indistinguishable from tampering -- see compute_content_hash). This
   module never approves its own output and never runs anything to
   produce it: the ONLY I/O in the whole generation path is one ranged
   historical-price fetch through the existing maverick-mcp/yfinance path
   (fetch_daily_closes, mirroring run_backtest.py's own fetch_bars).

   MARKET-REGIME DATA ONLY: month selection reads SPY closes and nothing
   else. This file does not import trades/agent_activity/
   backtest_cycle_evidence access, or any candidate-configuration result
   -- there is nothing here *capable* of looking at agent decisions or
   P&L when choosing months, by construction, not by convention.

   FIXED-WATCHLIST VIABILITY AUDIT (2026-09-13 correction pass, part 2):
   SPY only proves the BROAD market is a genuine example of a regime --
   it says nothing about whether the actual fixed 10-name watchlist
   (`watchlist/watchlist.json`) has enough real historical data to be
   evaluated normally in a given month. Every selected episode is
   audited read-only, through the SAME existing production paths the
   harness already uses (`run_backtest.fetch_technicals` for technical-
   analysis data availability, `fetch_daily_closes` for a purely
   descriptive movement/volatility summary) -- never the trading agent,
   never Neon, never a new run, never the watchlist itself, never a
   trade outcome. See `audit_episode_watchlist_viability` /
   `WATCHLIST_AUDIT_METHOD`. A month failing the audit is never included
   in the manifest -- `propose_manifest` fails clearly and names the
   ticker/month instead. Low movement is explicitly NOT a failure --
   only missing/insufficient point-in-time data is.

2. FUTURE RUNNER -- run_configuration(): given an APPROVED, untampered
   manifest and an explicit configuration (e.g. fundamentals on/off, or a
   future approved rule variant), runs run_backtest.py's REAL, UNCHANGED
   per-day loop once per one-month episode, each with a freshly isolated
   Portfolio/masking/checkpoint (never touching backtest_state.json/
   cycle_checkpoint.json, never Round 2), reusing the EXISTING training/
   smoke/archived classification unchanged (an episode's/suite's own
   identity is local report metadata only -- never a new run_kind), and
   produces a compact per-episode + aggregate comparison report.
   run_configuration() is the ONLY PUBLIC entry point that can execute an
   episode -- the actual per-episode executor (_run_episode, leading
   underscore) is internal precisely so approval cannot be bypassed by
   calling a lower-level function directly; run_configuration verifies
   the manifest (require_approved) AND its per-episode watchlist
   viability audit (_require_valid_watchlist_viability) BEFORE any
   register_run, journal access, market-data call, checkpoint creation,
   or state mutation. A hash-valid, genuinely-approved manifest whose
   audit is missing/malformed/failing is refused just as hard as an
   unapproved one -- approval alone was never proof the fixed watchlist
   is actually testable in every episode.
   NEITHER this module's own top-level code NOR its tests ever call
   run_configuration/_run_episode against real Docker/Neon/Hermes -- see
   test_eval_suite.py's fakes. It exists to be called later, by a human/
   orchestrating script, once an approved manifest and an explicit
   configuration comparison are actually authorized.

HARD BOUNDARY (same discipline as geometry_scan.py/benchmark_sensitivity.py):
evaluation infrastructure only. Never wired into AGENTS.md, a prompt, or
the live path. Generating or reading a manifest is not itself an approval
to run anything -- see require_approved.

Run (manifest generation only -- see the module docstring above for why
the runner is never invoked from the CLI):
    python eval_suite.py propose --seed 20260913
"""
import argparse
import hashlib
import json
import random
import re
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_backtest as rb                                              # noqa: E402
from run_backtest import (                                             # noqa: E402
    DEFAULT_MASKING_SEED, REPO, Portfolio, _limits_for_profile,
    _name_variants, _run_cycle_or_halt, json_from, log_cycle_failure,
    register_run, resolve_trade_rejection, run_day, run_in,
)
from anonymize import RunMasking                                       # noqa: E402

# ---------------------------------------------------------------------
# fixed, pre-committed parameters -- chosen and documented BEFORE any
# month is ever classified, exactly what "pre-committed" requires. Changing
# any of these changes the manifest's content hash, so a change here is
# never silently invisible in a generated manifest.
# ---------------------------------------------------------------------

# 2022-06-01..2026-01-31, ending the last full month before Round 2's own
# real window starts (2026-02-17) -- every episode date is wholly separate
# from Round 2's own dates by construction. START moved forward from the
# original 2021-01-01 (2026-09-15 fundamentals-measurement block, explicit
# user decision on the range/coverage tradeoff first surfaced 2026-09-13):
# under 2021-01-01, the fixed-watchlist viability audit correctly failed
# RYAN (real IPO 2021-07-22) and ALGM (real IPO 2020-10-29), which lack
# enough trailing technical-service history until real dates well into
# 2021-2022 (RYAN not until ~2022-06-01). 2022-06-01 is that verified
# floor, chosen by the user rather than searched for after seeing results.
ELIGIBLE_RANGE_START = date(2022, 6, 1)
ELIGIBLE_RANGE_END = date(2026, 1, 31)

# The project's EXISTING broad-market proxy (see benchmark_sensitivity.py)
# -- not a new choice made here.
PROXY_TICKER = "SPY"

RALLY_THRESHOLD_PCT = 5.0
SELLOFF_THRESHOLD_PCT = -5.0
# stdev of SPY's own daily % returns within the month, in percentage
# points -- an ordinary SPY month runs well under 1%; 1.5 marks a
# genuinely turbulent one.
HIGH_VOLATILITY_THRESHOLD_PCT = 1.5
# Correction pass (2026-09-13), part 1: "sideways" used to be a residual
# bucket -- any month that simply missed the rally/selloff cutoffs, even
# one that drifted materially. Now it requires BOTH a small net move AND
# low daily variance -- a genuinely quiet month, not merely an
# unremarkable one.
SIDEWAYS_RETURN_BAND_PCT = (-2.0, 2.0)
SIDEWAYS_VOL_THRESHOLD_PCT = 1.0
CLASSIFICATION_METHOD = (
    "month regime, checked in this exact priority order so a month gets exactly one "
    "label: 'high_volatility' if stdev(daily %% returns within the month) >= %.1f pct "
    "pts; elif monthly_return_pct >= %.1f: 'rally'; elif monthly_return_pct <= %.1f: "
    "'selloff'; elif %.1f <= monthly_return_pct <= %.1f AND stdev < %.1f pct pts: "
    "'sideways' (genuinely quiet -- small net move AND low daily variance, never just "
    "a month that missed the rally/selloff cutoffs); else 'unclassified' -- excluded "
    "from both the comparison and holdout pools, never sampled, never silently forced "
    "into 'sideways' or any other label."
) % (HIGH_VOLATILITY_THRESHOLD_PCT, RALLY_THRESHOLD_PCT, SELLOFF_THRESHOLD_PCT,
     SIDEWAYS_RETURN_BAND_PCT[0], SIDEWAYS_RETURN_BAND_PCT[1], SIDEWAYS_VOL_THRESHOLD_PCT)

# The most recent HOLDOUT_MONTHS_COUNT eligible months are the DEFAULT
# holdout pool -- expanded backward (never touched forward) only as much
# as necessary to cover all 4 required regimes; see select_holdout_pool.
# Never touched by the comparison-suite sampler below.
HOLDOUT_MONTHS_COUNT = 12
# 2026-09-15 fundamentals-measurement block: exactly one comparison episode
# per condition (was 2) -- the frozen design's own requirement, chosen
# before any month was classified.
EPISODES_PER_REGIME = 1
HOLDOUT_EPISODES_PER_REGIME = 1
# Correction pass (2026-09-13): a valid holdout (and comparison) set must
# cover every one of these regimes -- see select_holdout_pool.
REQUIRED_REGIMES = frozenset({"rally", "selloff", "high_volatility", "sideways"})
# v3 (was 2): classify_regime now requires "sideways" to be genuinely
# quiet (see SIDEWAYS_RETURN_BAND_PCT/SIDEWAYS_VOL_THRESHOLD_PCT above);
# a month satisfying none of the 4 required regimes is "unclassified"
# and excluded from sampling instead of being forced into "sideways".
# The manifest also gained watchlist_tickers/watchlist_audit_method/
# watchlist_audit_version, and each episode gained a watchlist_viability
# section -- see audit_episode_watchlist_viability / propose_manifest.
MANIFEST_VERSION = 3
MANIFEST_DIR = Path(__file__).resolve().parent / "eval_suites"

# Correction pass item E (prior audit M2): the ONLY run_kind
# run_configuration will register an episode under. Previously the caller
# passed an arbitrary 'training'/'smoke' value -- 'training' made every
# eval episode indistinguishable from a real Round 2-style run in the
# registry and the dashboard's training-run selector; 'smoke' mislabels
# genuine evaluation data as an infrastructure test, the exact thing
# 'smoke' exists to exclude. See trade-journal-mcp/schema.sql's
# backtest_run_registry CHECK constraint, widened to accept this value.
EVAL_RUN_KIND = "evaluation"


# ---------------------------------------------------------------------
# 1a. pure month-window arithmetic
# ---------------------------------------------------------------------

def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last calendar day of (year, month)."""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end - timedelta(days=1)


def month_range(start: date, end: date) -> list[tuple[int, int]]:
    """Every (year, month) whose first day falls within [start, end]."""
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _nth_month_from_end(end: date, n: int) -> tuple[int, int]:
    """The (year, month) that starts the LAST n calendar months up to and
    including end's own month -- e.g. end=2026-01, n=12 -> (2025, 2)
    (Feb 2025 through Jan 2026 inclusive is exactly 12 months)."""
    total = end.year * 12 + (end.month - 1) - (n - 1)
    return total // 12, total % 12 + 1


def _first_weekday_on_or_after(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _last_weekday_on_or_before(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------
# 1b. regime classification -- pure, market-regime data only
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class MonthMetrics:
    year: int
    month: int
    start: date
    end: date
    monthly_return_pct: float
    realized_vol_pct: float
    regime: str

    def to_dict(self) -> dict:
        return {
            "year": self.year, "month": self.month,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "monthly_return_pct": self.monthly_return_pct,
            "realized_vol_pct": self.realized_vol_pct,
            "regime": self.regime,
        }


def classify_regime(monthly_return_pct: float, realized_vol_pct: float) -> str:
    """Fixed priority order -- see CLASSIFICATION_METHOD. Pure, no I/O:
    takes only the two numbers a month's classification depends on.
    'unclassified' (correction pass, part 1) is a real, valid outcome --
    a month that neither rallied/sold off/turned volatile NOR stayed
    genuinely quiet. Callers must exclude it from sampling, never treat
    it as sideways by default."""
    if realized_vol_pct >= HIGH_VOLATILITY_THRESHOLD_PCT:
        return "high_volatility"
    if monthly_return_pct >= RALLY_THRESHOLD_PCT:
        return "rally"
    if monthly_return_pct <= SELLOFF_THRESHOLD_PCT:
        return "selloff"
    if (SIDEWAYS_RETURN_BAND_PCT[0] <= monthly_return_pct <= SIDEWAYS_RETURN_BAND_PCT[1]
            and realized_vol_pct < SIDEWAYS_VOL_THRESHOLD_PCT):
        return "sideways"
    return "unclassified"


def compute_month_metrics(closes: dict[str, float], year: int, month: int) -> MonthMetrics | None:
    """closes: {"YYYY-MM-DD": close}. None when fewer than 2 real trading
    days of data exist for this month -- a return needs at least two
    points; the caller skips it rather than fabricating one."""
    start, end = month_bounds(year, month)
    days = sorted(d for d in closes if start.isoformat() <= d <= end.isoformat())
    if len(days) < 2:
        return None
    values = [closes[d] for d in days]
    monthly_return_pct = (values[-1] - values[0]) / values[0] * 100
    daily_returns_pct = [
        (values[i] - values[i - 1]) / values[i - 1] * 100 for i in range(1, len(values))
    ]
    realized_vol_pct = pstdev(daily_returns_pct) if len(daily_returns_pct) >= 2 else 0.0
    return MonthMetrics(
        year=year, month=month, start=start, end=end,
        monthly_return_pct=round(monthly_return_pct, 4),
        realized_vol_pct=round(realized_vol_pct, 4),
        regime=classify_regime(monthly_return_pct, realized_vol_pct),
    )


def stratified_sample(months: list[MonthMetrics], per_regime: int, seed: int) -> list[MonthMetrics]:
    """Deterministic: the same (months, per_regime, seed) always produces
    the same selection. Groups by regime (fixed alphabetical iteration
    order, independent of dict/hash ordering), samples WITHOUT
    replacement within each regime via a seeded RNG, and takes every
    available month when fewer than per_regime exist for a regime --
    never fabricates coverage that isn't there. Returned chronologically
    sorted, not in regime-group order."""
    by_regime: dict[str, list[MonthMetrics]] = defaultdict(list)
    for m in months:
        by_regime[m.regime].append(m)
    rng = random.Random(seed)
    chosen: list[MonthMetrics] = []
    for regime in sorted(by_regime):
        pool = sorted(by_regime[regime], key=lambda m: (m.year, m.month))
        chosen.extend(rng.sample(pool, min(per_regime, len(pool))))
    return sorted(chosen, key=lambda m: (m.year, m.month))


def select_holdout_pool(
    all_months: list[MonthMetrics], eligible_end: date, initial_size: int,
) -> tuple[tuple[int, int], list[MonthMetrics], int]:
    """Deterministic, auditable holdout-pool selection (correction pass,
    requirement 3). Starts at the trailing `initial_size` calendar months
    (the documented default). If any of REQUIRED_REGIMES is missing from
    that pool, expands ONE calendar month backward at a time -- never
    forward, never a skip -- until all 4 are present. Never touches which
    months are randomly SAMPLED (that stays stratified_sample's seeded
    job); this only decides the pool's own chronological boundary.

    Fails clearly (ManifestGenerationError), never silently returning an
    incomplete holdout or hand-picking a month, in two cases:
      1. Even the ENTIRE eligible range lacks one of the 4 regimes --
         there is no boundary that could ever fix that.
      2. The first boundary where the holdout reaches full coverage
         leaves the comparison pool (every month strictly before it)
         missing a regime. Expanding the holdout FURTHER only shrinks
         the comparison pool more -- it can never recover a regime
         already lost -- so this is reported immediately, not searched
         around.

    `size` counts CALENDAR months back from eligible_end (what
    _nth_month_from_end consumes), never the number of entries in
    `all_months` -- since the correction pass (part 1) excludes
    "unclassified" months, `all_months` is generally a SPARSE subset of
    the calendar range, not one entry per calendar month, so the loop
    must keep expanding until the calendar boundary itself reaches the
    earliest eligible month, not until `size` reaches len(all_months)
    (that bound would stop expanding far too early whenever unclassified
    months are interspersed -- exactly the bug this fixes).

    Returns (holdout_start_month, holdout_pool sorted chronologically,
    final_size actually used) -- all three recorded in the manifest so
    the selection is auditable."""
    months_sorted = sorted(all_months, key=lambda m: (m.year, m.month))
    if not months_sorted:
        raise ManifestGenerationError("no eligible (classified, non-'unclassified') months at all")
    earliest = (months_sorted[0].year, months_sorted[0].month)
    size = initial_size
    while True:
        holdout_start = _nth_month_from_end(eligible_end, size)
        pool = [m for m in months_sorted if (m.year, m.month) >= holdout_start]
        pool_regimes = {m.regime for m in pool}
        if REQUIRED_REGIMES <= pool_regimes:
            comparison_pool = [m for m in months_sorted if (m.year, m.month) < holdout_start]
            comparison_regimes = {m.regime for m in comparison_pool}
            missing = REQUIRED_REGIMES - comparison_regimes
            if missing:
                raise ManifestGenerationError(
                    f"holdout pool reaches all 4 required regimes only by expanding to "
                    f"the trailing {size} months (from {holdout_start[0]}-{holdout_start[1]:02d}), "
                    f"but that leaves the comparison pool (every month before it) missing "
                    f"{sorted(missing)} -- cannot satisfy full 4-regime coverage in both sets "
                    "simultaneously from this eligible range; widen ELIGIBLE_RANGE_START or "
                    "lower HOLDOUT_MONTHS_COUNT and regenerate. Not fabricating coverage."
                )
            return holdout_start, pool, size
        if holdout_start <= earliest:
            raise ManifestGenerationError(
                f"even the entire eligible range ({len(months_sorted)} classified months, "
                f"earliest {earliest[0]}-{earliest[1]:02d}) does not contain all 4 required "
                f"regimes {sorted(REQUIRED_REGIMES)} -- cannot build a valid holdout pool "
                "without fabricating coverage. Widen the eligible range and regenerate."
            )
        size += 1


# ---------------------------------------------------------------------
# 1c. the one real I/O call -- existing maverick-mcp/yfinance path
# ---------------------------------------------------------------------

def fetch_daily_closes(ticker: str, start: date, end: date) -> dict[str, float]:
    """{"YYYY-MM-DD": close} for every real trading day yfinance returns
    in [start, end] -- ONE ranged call (not one per day), the same
    maverick-mcp/yfinance path run_backtest.py's own fetch_bars uses."""
    script = f'''
import json, yfinance as yf
h = yf.Ticker("{ticker}").history(start="{start}", end="{end + timedelta(days=1)}")
out = {{str(idx.date()): float(row["Close"]) for idx, row in h.iterrows()}}
print(json.dumps({{"closes": out}}))
'''
    return json_from(run_in("maverick-mcp", script))["closes"]


class ManifestGenerationError(RuntimeError):
    """Raised when the required historical proxy data cannot be retrieved
    reliably through the existing supported path. Caller must fail
    clearly and document the missing dependency -- never substitute
    hand-picked months (see this module's own docstring)."""


def _load_default_watchlist() -> list[dict]:
    """The SAME file/shape run_backtest.main() itself loads -- the
    current fixed 10-name universe, never chosen or filtered here."""
    return json.loads((REPO / "watchlist" / "watchlist.json").read_text())["tickers"]


def _fetch_watchlist_closes(tickers: list[str], start: date, end: date,
                            fetch_closes=fetch_daily_closes) -> dict[str, dict[str, float]]:
    """{"TICKER": {"YYYY-MM-DD": close}}, one fetch_daily_closes call per
    ticker -- reuses the SAME existing maverick-mcp/yfinance path as the
    SPY fetch above, not a new one. Injectable for tests."""
    return {t: fetch_closes(t, start, end) for t in tickers}


# ---------------------------------------------------------------------
# 1d. fixed-watchlist viability audit -- read-only (correction pass,
# part 2). Proves the ACTUAL fixed watchlist (not just the SPY proxy)
# has enough real historical data to be evaluated normally in a given
# episode, through the SAME existing production paths run_backtest.py
# itself uses. Never calls the trading agent, writes Neon, creates a
# run, alters the watchlist, or inspects a trade outcome.
# ---------------------------------------------------------------------

WATCHLIST_AUDIT_VERSION = 1
WATCHLIST_AUDIT_METHOD = (
    "data-availability only, never a tradability/movement judgement: for each "
    "fixed-watchlist ticker, call the EXISTING production technical-analysis path "
    "(run_backtest.fetch_technicals -> maverick's TechnicalService.get_full_analysis) "
    "at the episode's first and last real (weekday) trading day -- the two extremes "
    "of the technical service's own trailing-lookback window, which only grows with "
    "time, so a pass at both ends implies at least as much history on every day "
    "between (not independently re-checked). A ticker erroring at either end is "
    "'unavailable' for that episode (insufficient/missing listing history, "
    "delisting, or a ticker-identity failure). A separate, purely descriptive "
    "monthly return/volatility range for the full watchlist (the same "
    "compute_month_metrics used for SPY, applied per ticker) is recorded alongside "
    "for visibility only -- it never affects validity; a quiet, low-movement name is "
    "valid evidence, especially in a sideways regime, never a reason to fail an "
    "episode."
)


def audit_episode_watchlist_viability(episode: dict, watchlist: list[dict],
                                      fetch_tech=None, fetch_closes=None) -> dict:
    """Read-only per-episode audit -- see WATCHLIST_AUDIT_METHOD. Both
    fetchers are injectable (tests always inject a fixture, never a real
    network/Docker call); `fetch_tech` defaults to run_backtest's own
    real fetch_technicals (imported as `rb` at module load)."""
    fetch_tech = fetch_tech or rb.fetch_technicals
    fetch_closes = fetch_closes or _fetch_watchlist_closes
    tickers = sorted(t["symbol"] for t in watchlist)

    check_start = _first_weekday_on_or_after(date.fromisoformat(episode["start"]))
    check_end = _last_weekday_on_or_before(date.fromisoformat(episode["end"]))
    start_tech = fetch_tech(tickers, check_start)
    end_tech = fetch_tech(tickers, check_end)

    per_ticker: dict[str, dict] = {}
    unavailable: list[str] = []
    for sym in tickers:
        start_error = (start_tech.get(sym) or {}).get("error")
        end_error = (end_tech.get(sym) or {}).get("error")
        usable = start_error is None and end_error is None
        per_ticker[sym] = {"usable": usable, "start_error": start_error, "end_error": end_error}
        if not usable:
            unavailable.append(sym)

    year, month = episode["year"], episode["month"]
    month_start, month_end = month_bounds(year, month)
    closes = fetch_closes(tickers, month_start, month_end)
    movement_per_ticker: dict[str, dict | None] = {}
    returns, vols = [], []
    for sym in tickers:
        m = compute_month_metrics(closes.get(sym, {}), year, month)
        if m is None:
            movement_per_ticker[sym] = None
            continue
        movement_per_ticker[sym] = {"return_pct": m.monthly_return_pct, "vol_pct": m.realized_vol_pct}
        returns.append(m.monthly_return_pct)
        vols.append(m.realized_vol_pct)

    return {
        "checked_dates": {"start": check_start.isoformat(), "end": check_end.isoformat()},
        "usable_count": len(tickers) - len(unavailable),
        "unavailable_count": len(unavailable),
        "unavailable_tickers": unavailable,
        "per_ticker": per_ticker,
        "movement": {
            "per_ticker": movement_per_ticker,
            "return_pct_range": [min(returns), max(returns)] if returns else None,
            "vol_pct_range": [min(vols), max(vols)] if vols else None,
        },
        "valid": len(unavailable) == 0,
    }


def _episodes_with_viability(episodes: list[MonthMetrics], watchlist: list[dict],
                             fetch_tech, fetch_closes) -> list[dict]:
    """MonthMetrics -> episode dict, each carrying its own
    watchlist_viability. Fails clearly and NAMES the ticker/month --
    never silently drops or fabricates coverage for an episode whose
    watchlist data isn't actually there (correction pass, part 2)."""
    out = []
    for m in episodes:
        ep = m.to_dict()
        wv = audit_episode_watchlist_viability(ep, watchlist, fetch_tech, fetch_closes)
        if not wv["valid"]:
            raise ManifestGenerationError(
                f"watchlist viability audit failed for {ep['year']}-{ep['month']:02d} "
                f"({ep['regime']}): ticker(s) {wv['unavailable_tickers']} lack enough "
                "point-in-time data for the existing technical-analysis path over this "
                "episode -- cannot include this month in the manifest; widen "
                "ELIGIBLE_RANGE_START, fix the underlying data source, or accept a "
                "smaller/adjusted watchlist and regenerate. Not fabricating coverage."
            )
        ep["watchlist_viability"] = wv
        out.append(ep)
    return out


def propose_manifest(seed: int, eligible_start: date = ELIGIBLE_RANGE_START,
                     eligible_end: date = ELIGIBLE_RANGE_END,
                     fetch_closes=fetch_daily_closes,
                     watchlist: list[dict] | None = None,
                     fetch_tech=None,
                     fetch_watchlist_closes=None) -> dict:
    """The one function that does real I/O. `fetch_closes` is injectable
    (tests always inject a fixture, never a real network/Docker call).

    Uses market-regime data ONLY: `closes` comes straight from
    `fetch_closes` for PROXY_TICKER and nothing else is ever read here --
    no trades, no agent_activity, no backtest_cycle_evidence, no
    candidate-configuration result. Always writes status "proposed" --
    this function can never produce an "approved" manifest."""
    try:
        closes = fetch_closes(PROXY_TICKER, eligible_start, eligible_end)
    except Exception as exc:
        raise ManifestGenerationError(
            f"could not retrieve {PROXY_TICKER} historical data via the existing "
            f"maverick-mcp/yfinance path for [{eligible_start}, {eligible_end}]: "
            f"{type(exc).__name__}: {exc} -- missing dependency, not substituting "
            "hand-picked months"
        ) from exc
    if not closes:
        raise ManifestGenerationError(
            f"{PROXY_TICKER} historical fetch returned no data for "
            f"[{eligible_start}, {eligible_end}] -- cannot classify any month"
        )

    all_months: list[MonthMetrics] = []
    for year, month in month_range(eligible_start, eligible_end):
        m = compute_month_metrics(closes, year, month)
        if m is not None:
            all_months.append(m)
    if not all_months:
        raise ManifestGenerationError(
            f"{PROXY_TICKER} data was retrieved but no month in "
            f"[{eligible_start}, {eligible_end}] had >=2 trading days to classify"
        )

    # Correction pass, part 1: "unclassified" months are excluded from
    # BOTH pools here, at the single point every downstream selection
    # (select_holdout_pool, stratified_sample) reads from -- they never
    # see an "unclassified" regime bucket at all. Still recorded in
    # all_classified_months below, for a complete, honest audit trail.
    eligible_months = [m for m in all_months if m.regime != "unclassified"]
    if not eligible_months:
        raise ManifestGenerationError(
            f"every classified month in [{eligible_start}, {eligible_end}] is "
            "'unclassified' under the current thresholds -- nothing eligible to "
            "sample from"
        )

    holdout_start, holdout_pool, holdout_pool_size = select_holdout_pool(
        eligible_months, eligible_end, HOLDOUT_MONTHS_COUNT)
    comparison_pool = [m for m in eligible_months if (m.year, m.month) < holdout_start]

    episodes = stratified_sample(comparison_pool, EPISODES_PER_REGIME, seed)
    # seed + 1: independent of the comparison draw but still fully
    # reproducible from the single top-level seed -- never a second
    # user-facing seed to track.
    holdout_episodes = stratified_sample(holdout_pool, HOLDOUT_EPISODES_PER_REGIME, seed + 1)

    # Post-condition, not just a hope: select_holdout_pool already
    # guarantees both pools cover all 4 regimes, so a per_regime>=1
    # stratified draw is guaranteed to include each -- but a silent
    # regression here must be loud, never a quietly incomplete manifest.
    episode_regimes = {e.regime for e in episodes}
    holdout_regimes = {e.regime for e in holdout_episodes}
    if not (REQUIRED_REGIMES <= episode_regimes):
        raise ManifestGenerationError(
            f"internal error: comparison pool covered all 4 regimes but the sampled "
            f"episodes only cover {sorted(episode_regimes)} -- refusing to write an "
            "incomplete manifest"
        )
    if not (REQUIRED_REGIMES <= holdout_regimes):
        raise ManifestGenerationError(
            f"internal error: holdout pool covered all 4 regimes but the sampled "
            f"holdout episodes only cover {sorted(holdout_regimes)} -- refusing to write "
            "an incomplete manifest"
        )

    watchlist = watchlist if watchlist is not None else _load_default_watchlist()
    episode_dicts = _episodes_with_viability(episodes, watchlist, fetch_tech, fetch_watchlist_closes)
    holdout_episode_dicts = _episodes_with_viability(holdout_episodes, watchlist, fetch_tech, fetch_watchlist_closes)

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "proposed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "eligible_range": {"start": eligible_start.isoformat(), "end": eligible_end.isoformat()},
        "proxy_ticker": PROXY_TICKER,
        "classification_method": CLASSIFICATION_METHOD,
        "thresholds": {
            "rally_pct": RALLY_THRESHOLD_PCT,
            "selloff_pct": SELLOFF_THRESHOLD_PCT,
            "high_volatility_pct": HIGH_VOLATILITY_THRESHOLD_PCT,
            "sideways_return_band_pct": list(SIDEWAYS_RETURN_BAND_PCT),
            "sideways_max_vol_pct": SIDEWAYS_VOL_THRESHOLD_PCT,
        },
        "required_regimes": sorted(REQUIRED_REGIMES),
        "selection_seed": seed,
        "holdout_pool": {
            "start": f"{holdout_start[0]}-{holdout_start[1]:02d}",
            "size_months": holdout_pool_size,
            "initial_size_months": HOLDOUT_MONTHS_COUNT,
            "expanded": holdout_pool_size > HOLDOUT_MONTHS_COUNT,
            "expansion_rule": (
                "start from the trailing initial_size_months months; if any of the 4 "
                "required regimes is missing, expand one calendar month backward at a "
                "time until all 4 are present; fail clearly if the full eligible range "
                "still cannot cover all 4, or if reaching holdout coverage would leave "
                "the comparison pool missing one"
            ),
        },
        "watchlist_tickers": sorted(t["symbol"] for t in watchlist),
        "watchlist_audit_method": WATCHLIST_AUDIT_METHOD,
        "watchlist_audit_version": WATCHLIST_AUDIT_VERSION,
        "episodes": episode_dicts,
        "holdout_episodes": holdout_episode_dicts,
        "all_classified_months": [m.to_dict() for m in sorted(all_months, key=lambda m: (m.year, m.month))],
    }
    manifest["content_hash"] = compute_content_hash(manifest)
    return manifest


def save_manifest(manifest: dict, directory: Path = MANIFEST_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    ts = manifest["generated_at"].replace(":", "").replace("-", "")[:15]
    path = directory / f"manifest_{ts}_{manifest['content_hash'][:12]}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return path


# ---------------------------------------------------------------------
# hashing / tamper detection / approval gate
# ---------------------------------------------------------------------

# "status" is deliberately excluded: flipping proposed -> approved is the
# explicit human approval action (a one-line edit) and must never look
# indistinguishable from tampering with the substance (episodes/
# thresholds/seed/range/proxy/classification method).
_HASH_EXCLUDED_FIELDS = {"status", "content_hash"}


def compute_content_hash(manifest: dict) -> str:
    """SHA-256 over a canonical (sorted-keys, no-whitespace) JSON encoding
    of every field except "status" and "content_hash" itself."""
    payload = {k: v for k, v in manifest.items() if k not in _HASH_EXCLUDED_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ManifestTamperedError(ValueError):
    """The stored content_hash doesn't match a fresh recomputation."""


class ManifestNotApprovedError(ValueError):
    """The manifest's status is not exactly 'approved'."""


class ManifestVersionError(ValueError):
    """Correction pass item E.3 (prior audit L4): the manifest's own
    manifest_version does not match this code's MANIFEST_VERSION.
    manifest_version is written and content-hashed (so tampering with it
    is caught the same way as any other field) but was previously never
    actually COMPARED against the running code anywhere -- a future
    MANIFEST_VERSION bump with changed classification semantics (see that
    constant's own changelog comments) would otherwise execute an older,
    approved manifest silently, under semantics it was never generated
    or reviewed under."""


class ManifestWatchlistViabilityError(ValueError):
    """An episode's watchlist_viability audit is missing, malformed, or
    reports a failing ticker (correction pass, part 2). Distinct from
    ManifestTamperedError: this can fire on a manifest that is perfectly
    hash-consistent and genuinely approved -- approval and an intact
    hash only prove nothing was edited since generation, never that a
    valid audit was ever attached or that it passed."""


def verify_manifest(manifest: dict) -> None:
    """Raises ManifestTamperedError on any mismatch -- the one check that
    proves every substantive field is exactly what propose_manifest
    generated, never edited since. Never inspects "status" itself."""
    stored = manifest.get("content_hash")
    if not stored:
        raise ManifestTamperedError("manifest has no content_hash at all")
    recomputed = compute_content_hash(manifest)
    if stored != recomputed:
        raise ManifestTamperedError(
            f"manifest content_hash mismatch -- stored {stored[:12]}... vs "
            f"recomputed {recomputed[:12]}... (episodes/thresholds/seed/range "
            "edited since generation, or file corruption)"
        )


def require_approved(manifest: dict) -> None:
    """The runner's own gate: refuse a proposed, tampered, OR
    version-incompatible manifest. Verifies the hash FIRST -- a tampered
    manifest is rejected regardless of its claimed status -- then the
    manifest_version compatibility gate (correction pass item E.3, prior
    audit L4), THEN requires status == 'approved' exactly; 'proposed'
    (the only status propose_manifest ever writes) is refused exactly
    like any other non-'approved' value."""
    verify_manifest(manifest)
    stored_version = manifest.get("manifest_version")
    if stored_version != MANIFEST_VERSION:
        raise ManifestVersionError(
            f"manifest_version {stored_version!r} does not match this code's "
            f"MANIFEST_VERSION {MANIFEST_VERSION} -- classification semantics may "
            "have changed since this manifest was generated. Regenerate and "
            "re-approve a fresh manifest with the current code before running "
            "any episode; never run an old-version manifest under new-version code."
        )
    if manifest.get("status") != "approved":
        raise ManifestNotApprovedError(
            f"manifest status is {manifest.get('status')!r}, not 'approved' -- "
            "a proposed manifest requires explicit human review (edit its "
            "'status' field on the saved JSON file) before any episode may run"
        )


def _require_valid_watchlist_viability(manifest: dict, episode_set: str) -> None:
    """Correction pass, part 2's integration requirement: refuse to run
    ANY episode in `episode_set` unless every one of them carries a
    well-formed, passing watchlist_viability section. Called from
    run_configuration AFTER require_approved but still BEFORE any
    register_run/run_day/journal/market-data/checkpoint call."""
    for ep in manifest.get(episode_set) or []:
        label = f"{ep.get('year')}-{ep.get('month', 0):02d}"
        wv = ep.get("watchlist_viability")
        if not isinstance(wv, dict) or "valid" not in wv or "unavailable_tickers" not in wv:
            raise ManifestWatchlistViabilityError(
                f"episode {label} has no well-formed watchlist_viability audit -- "
                "refusing to execute without proof the fixed watchlist has enough "
                "point-in-time data for this episode"
            )
        if not wv["valid"] or wv["unavailable_tickers"]:
            raise ManifestWatchlistViabilityError(
                f"episode {label} failed its watchlist viability audit -- unavailable "
                f"ticker(s) {wv['unavailable_tickers']} -- refusing to execute"
            )


# =======================================================================
# 2. FUTURE RUNNER -- interface only. NEVER invoked by this module's own
# top-level code, and never called against real Docker/Neon/Hermes by
# test_eval_suite.py either (see its fakes). Reuses run_backtest.py's
# real, unchanged per-day loop; the only new things here are (a) the
# approved-manifest gate above and (b) fresh per-episode isolation,
# mirroring run_backtest.main()'s own loop shape exactly.
# =======================================================================

@dataclass(frozen=True)
class EpisodeConfig:
    """One configuration to compare -- e.g. fundamentals disabled vs.
    enabled, or a future approved rule variant. `config_label` is report
    metadata only, never a run_kind and never sent to Neon."""
    config_label: str
    fundamentals_enabled: bool = False
    seed: int = DEFAULT_MASKING_SEED


_FUNDAMENTALS_TAG_RE = re.compile(r"^\[fundamentals:([a-z_]+)\]")


def fetch_fundamentals_assessment_counts(run_id: str) -> dict[str, int]:
    """Best-effort report enrichment only (never gates episode execution
    -- see run_episode): counts of each '[fundamentals:...]' strategy_notes
    tag (run_backtest.py's _tag_strategy_notes) journaled for this run_id.
    A real Neon read through the existing trade-journal container path --
    not a new service."""
    script = f'''
import asyncio, json
from server import get_pool

async def main():
    pool = await get_pool()
    rows = await pool.fetch("SELECT strategy_notes FROM trades WHERE run_id = $1", "{run_id}")
    print(json.dumps({{"notes": [r["strategy_notes"] for r in rows]}}))

asyncio.run(main())
'''
    notes = json_from(run_in("trade-journal", script))["notes"]
    counts: dict[str, int] = defaultdict(int)
    for note in notes:
        m = _FUNDAMENTALS_TAG_RE.match(note or "")
        if m:
            counts[m.group(1)] += 1
    return dict(counts)


def _max_drawdown_pct(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak:
            worst = min(worst, (e - peak) / peak * 100)
    return round(worst, 4)


def _run_episode(episode: dict, config: EpisodeConfig, run_kind: str,
                 watchlist: list[dict], checkpoint_dir: Path,
                 fetch_fundamentals_fn=fetch_fundamentals_assessment_counts) -> dict:
    """INTERNAL. Runs ONE one-month episode through run_backtest's real,
    unchanged per-day loop (Portfolio/RunMasking/run_day/
    _run_cycle_or_halt). Do not call directly -- this performs zero
    manifest/approval validation of its own; run_configuration() is the
    only public entry point, and it verifies the manifest (require_
    approved) before calling this for even the first episode (correction
    pass, requirement 1: approval must not be bypassable through the
    public API).

    Fresh in-memory Portfolio, fresh RunMasking, and CHECKPOINT_PATH
    isolated to `checkpoint_dir` for the duration of this call only
    (restored after, even on failure -- see the try/finally) so a
    concurrent real run's own checkpoint can never be touched. Never
    writes backtest_state.json -- nothing here is resumable across a
    process restart by design (a one-month episode is short enough not to
    need it); a halted episode is simply reported as such, never retried
    automatically.

    run_kind: the durable classification (backtest_run_registry) every
    episode registers under -- run_configuration (the only public caller)
    already enforces this is exactly EVAL_RUN_KIND ('evaluation') before
    ever reaching here. The episode's/suite's own identity
    (`config.config_label`, `episode['regime']`, etc.) is local report
    metadata only, never sent as a run_kind."""
    start = date.fromisoformat(episode["start"])
    end = date.fromisoformat(episode["end"])
    run_id = str(uuid.uuid4())
    register_run(run_id, run_kind)

    names = {t["symbol"]: _name_variants(t["name"], t["symbol"]) for t in watchlist}
    masking = RunMasking(names, run_start=start, seed=config.seed)
    portfolio = Portfolio()
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    original_checkpoint_path = rb.CHECKPOINT_PATH
    rb.CHECKPOINT_PATH = checkpoint_dir / f"{run_id}_checkpoint.json"
    day_summaries: list[dict] = []
    halted = False
    try:
        cursor = start
        while cursor <= end:
            if cursor.weekday() >= 5:
                cursor += timedelta(days=1)
                continue
            day_index = (cursor - start).days
            limits_ctx = _limits_for_profile(portfolio.profile_level)
            result, halted = _run_cycle_or_halt(
                lambda: run_day(cursor, day_index, portfolio, masking, watchlist,
                                limits_ctx, run_id, fundamentals_enabled=config.fundamentals_enabled),
                day_label=masking.day_index(cursor), run_id=run_id,
                # Explicit, not _run_cycle_or_halt's own default: a default
                # parameter binds to run_backtest.log_cycle_failure at THAT
                # module's def-time and is never re-resolved from this
                # module's own name at call time, so patching
                # "eval_suite.log_cycle_failure" would silently do nothing
                # against the implicit default -- found live 2026-09-15 when
                # test_run_configuration_halts_cleanly_without_advancing's
                # deliberately-raising fake run_day reached the REAL logger
                # and wrote a genuine 'cycle_failed' agent_activity row to
                # Neon despite the test's own claim of zero real I/O. Naming
                # it here makes it patchable at this module's own boundary,
                # same as register_run/fetch_fundamentals_assessment_counts
                # just above.
                fail_logger=log_cycle_failure,
            )
            if halted:
                break
            portfolio, summary, do_observability = result
            if "skipped" not in summary:
                day_summaries.append(summary)
                try:
                    do_observability()
                except Exception:
                    pass
            rb._clear_checkpoint()
            cursor += timedelta(days=1)
    finally:
        rb.CHECKPOINT_PATH = original_checkpoint_path

    # Correction pass item E.2 (prior audit M1): an episode never resumes
    # (see this function's own docstring) -- whether it reached `end`
    # cleanly or halted partway, any order still 'pending' in the journal
    # at this point can NEVER be resolved by a later cycle the way the
    # main harness's own next-invocation would. Resolve it honestly as
    # rejected instead of leaving a permanently dangling pending row
    # indistinguishable from one genuinely still awaiting execution.
    # Best-effort (never masks the episode's own halted/report outcome,
    # same discipline as do_observability above) -- but real: reuses the
    # exact resolve_trade_rejection path the main harness itself uses.
    dangling_pending_orders = list(portfolio.pending_orders)
    for order in dangling_pending_orders:
        try:
            resolve_trade_rejection(order.decision_id, "episode window ended before execution")
        except Exception:
            pass
    portfolio.pending_orders = []

    fundamentals_assessment_counts = None
    if config.fundamentals_enabled and not halted:
        try:
            fundamentals_assessment_counts = fetch_fundamentals_fn(run_id)
        except Exception:
            fundamentals_assessment_counts = None  # best-effort enrichment only

    # Correction pass, requirement 2: derived from run_day's own
    # structured summary["fills"]/summary["proposals"] lists (added
    # alongside this correction -- see run_backtest.py), never from
    # matching text prefixes in summary["decisions"] (which mixes a
    # newly-queued proposal and a today-resolved fill under the same
    # "BUY "/"SELL " prefix -- the exact bug this replaces). A queued
    # proposal that is later rejected/superseded contributes to
    # proposal_count only, never fill_count; only an order that actually
    # filled (execution-sim status == "filled", the BUY/SELL branches
    # inside run_day's chronological event loop) is ever counted as a
    # fill, mechanical stop/target/time-stop exits included in neither
    # (see summary["exits"], untouched here).
    fills = [f for s in day_summaries for f in s.get("fills", [])]
    proposals = [p for s in day_summaries for p in s.get("proposals", [])]
    blocked_reasons = [line for s in day_summaries for line in s["blocked"]]
    return {
        "episode": {"year": episode["year"], "month": episode["month"], "regime": episode["regime"],
                    "start": episode["start"], "end": episode["end"]},
        "config_label": config.config_label,
        "fundamentals_enabled": config.fundamentals_enabled,
        "run_id": run_id,
        "run_kind": run_kind,
        "halted": halted,
        "days_completed": len(day_summaries),
        "return_pct": (
            round((portfolio.equity_curve[-1] - portfolio.equity_curve[0])
                  / portfolio.equity_curve[0] * 100, 4)
            if portfolio.equity_curve else 0.0
        ),
        "max_drawdown_pct": _max_drawdown_pct(portfolio.equity_curve),
        "fill_count": len(fills),
        "proposal_count": len(proposals),
        "closed_trade_count": len(portfolio.closed_pnl),
        "blocked_count": len(blocked_reasons),
        "blocked_reasons": blocked_reasons[:20],  # bounded -- report stays compact
        # Correction pass item E.2 (prior audit M1): how many orders this
        # episode itself resolved as rejected at its own end -- 0 in the
        # ordinary case, non-zero exactly when the episode's own final
        # cycle queued a decision with nothing left to resolve it.
        "dangling_pending_orders_resolved": len(dangling_pending_orders),
        "fundamentals_assessment_counts": fundamentals_assessment_counts,
    }


def _aggregate_episode_reports(per_episode: list[dict]) -> dict:
    n = len(per_episode)
    if n == 0:
        return {"episode_count": 0}
    returns = [e["return_pct"] for e in per_episode]
    drawdowns = [e["max_drawdown_pct"] for e in per_episode]
    return {
        "episode_count": n,
        "halted_count": sum(1 for e in per_episode if e["halted"]),
        "mean_return_pct": round(sum(returns) / n, 4),
        "worst_max_drawdown_pct": round(min(drawdowns), 4),
        "total_fills": sum(e["fill_count"] for e in per_episode),
        "total_proposals": sum(e["proposal_count"] for e in per_episode),
        "total_closed_trades": sum(e["closed_trade_count"] for e in per_episode),
        "total_blocked": sum(e["blocked_count"] for e in per_episode),
    }


def run_configuration(manifest: dict, config: EpisodeConfig, run_kind: str,
                      watchlist: list[dict], checkpoint_dir: Path,
                      episode_set: str = "episodes") -> dict:
    """THE public comparison entry point -- the only function in this
    module allowed to execute an episode (correction pass, requirement 1:
    _run_episode is internal and must never be called directly). Requires
    an APPROVED, untampered manifest AND a passing watchlist_viability
    audit on every episode about to run: require_approved, then
    _require_valid_watchlist_viability, both run BEFORE any register_run,
    journal access, market-data call, checkpoint creation, or state
    mutation for even the first episode -- a 'proposed'/tampered manifest,
    or one missing/failing its watchlist audit, causes zero calls into
    any of that (see test_eval_suite.py's dedicated public-API tests).

    Runs `config` against every episode in manifest[episode_set]
    ('episodes', the comparison suite, by default; 'holdout_episodes' for
    the separate, later, out-of-sample set), each freshly isolated (see
    _run_episode), and returns a compact per-episode + aggregate report.
    Every configuration compared this way must be run against the SAME
    manifest object/file for the comparison to be fair -- this function
    itself does not enforce that across separate calls, the caller must
    reuse one loaded manifest. NEVER called by this module's own code.

    run_kind: correction pass item E (prior audit M2) -- must be exactly
    EVAL_RUN_KIND ('evaluation'). Checked here, BEFORE require_approved
    even runs, so an eval episode can never register itself as 'training'
    (indistinguishable from a real run in the dashboard's training-run
    selector) or 'smoke' (mislabels genuine evaluation data as an infra
    test). The parameter stays explicit at the call site (self-documenting
    -- "this registers as evaluation") rather than a silent default;
    trade-journal-mcp's own registry validation is the second, independent
    layer of the same guarantee."""
    if run_kind != EVAL_RUN_KIND:
        raise ValueError(
            f"run_configuration only ever registers episodes as {EVAL_RUN_KIND!r} "
            f"-- got {run_kind!r}. An eval episode must never be classified "
            "'training' (indistinguishable from real training data) or 'smoke' "
            "(mislabels genuine evaluation data as an infrastructure test)."
        )
    require_approved(manifest)
    if episode_set not in ("episodes", "holdout_episodes"):
        raise ValueError(f"episode_set must be 'episodes' or 'holdout_episodes', got {episode_set!r}")
    _require_valid_watchlist_viability(manifest, episode_set)
    per_episode = [
        _run_episode(ep, config, run_kind, watchlist, checkpoint_dir)
        for ep in manifest[episode_set]
    ]
    return {
        "manifest_content_hash": manifest["content_hash"],
        "episode_set": episode_set,
        "config_label": config.config_label,
        "fundamentals_enabled": config.fundamentals_enabled,
        "run_kind": run_kind,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "per_episode": per_episode,
        "aggregate": _aggregate_episode_reports(per_episode),
    }


# ---------------------------------------------------------------------
# CLI -- manifest generation only. The runner above is never wired to a
# subcommand here; it is called later, explicitly, by a human/
# orchestrating script once an approved manifest exists.
# ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    propose = sub.add_parser("propose", help="Generate a new PROPOSED manifest (human review required).")
    propose.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    if args.cmd == "propose":
        manifest = propose_manifest(seed=args.seed)
        path = save_manifest(manifest)
        print(f"wrote {path} (status=proposed -- human review required before use)")
        print(f"eligible range: {manifest['eligible_range']['start']} .. {manifest['eligible_range']['end']}")
        print(f"proxy: {manifest['proxy_ticker']}  seed: {manifest['selection_seed']}")
        hp = manifest["holdout_pool"]
        print(f"holdout pool: starts {hp['start']}, {hp['size_months']} months "
              f"(initial {hp['initial_size_months']}, expanded={hp['expanded']})")
        n_episodes = len(manifest["episodes"]) + len(manifest["holdout_episodes"])
        print(f"watchlist viability: {len(manifest['watchlist_tickers'])} tickers, "
              f"all usable across all {n_episodes} selected episodes "
              "(propose_manifest would have failed clearly otherwise)")
        print("comparison episodes:")
        for e in manifest["episodes"]:
            print(f"  {e['year']}-{e['month']:02d}  {e['regime']:<15} "
                  f"return={e['monthly_return_pct']:+.2f}%  vol={e['realized_vol_pct']:.2f}%")
        print("holdout episodes (untouched until final out-of-sample eval):")
        for e in manifest["holdout_episodes"]:
            print(f"  {e['year']}-{e['month']:02d}  {e['regime']:<15} "
                  f"return={e['monthly_return_pct']:+.2f}%  vol={e['realized_vol_pct']:.2f}%")


if __name__ == "__main__":
    main()
