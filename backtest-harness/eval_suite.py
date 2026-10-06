"""
eval_suite.py -- regime-diverse evaluation-suite infrastructure (offline).

`docs/PROJECT_BACKLOG.md` §4's "Regime-diverse sampled evaluation suite"
item -- a *prerequisite for* Bayesian optimization, not an approved
experiment. Implements two DELIBERATELY SEPARATE capabilities:

1. MANIFEST GENERATION -- propose_manifest(): classifies SPY's real
   monthly return/volatility over a fixed eligible historical range, then
   selects one comparison + one later, untouched holdout episode PER
   REQUIRED REGIME independently (select_paired_episodes -- 2026-09-16
   correction pass; NOT one single globally-trailing holdout block, see
   that function's own docstring for why), and returns a versioned,
   hashed, content-addressed manifest. Its "status" is ALWAYS "proposed" -- a human reviews it
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
   Each episode also persists the same frozen run-configuration record a
   normal backtest does (watchlist snapshot+hash, alias mapping+hash,
   masking seed, window, fundamentals flag) before any fetch/Hermes/
   journal write. The masking seed is derived from the approved manifest's
   content hash + episode identity (derive_episode_seed), identical for
   paired configurations of one episode.
   The module's own top-level code never runs an episode, and its tests
   use fakes only (no real Docker/Neon/Hermes). The ONLY CLI path that
   executes anything is the explicit `run-one` subcommand below.

HARD BOUNDARY (same discipline as geometry_scan.py/benchmark_sensitivity.py):
evaluation infrastructure only. Never wired into AGENTS.md, a prompt, or
the live path. Generating or reading a manifest is not itself an approval
to run anything -- see require_approved.

Manifest generation (offline, no side effects):
    python eval_suite.py propose --seed 20260913

Run ONE approved comparison episode (REAL action: needs Docker, writes to
Neon, calls Hermes; comparison set only, never holdouts; refuses a
proposed/tampered/non-viable manifest before any side effect):
    python eval_suite.py run-one --manifest <approved.json> --year 2022         --month 7 --regime rally --checkpoint-dir <dir> [--fundamentals]
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
    REPO, Portfolio, _limits_for_profile,
    _name_variants, _run_cycle_or_halt, json_from, log_cycle_failure,
    preflight, register_run, register_run_configuration, resolve_trade_rejection, run_day, run_in,
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

# Correction pass (2026-09-13): a valid comparison/holdout selection must
# cover every one of these regimes.
REQUIRED_REGIMES = frozenset({"rally", "selloff", "high_volatility", "sideways"})
# v3 (was 2): classify_regime now requires "sideways" to be genuinely
# quiet (see SIDEWAYS_RETURN_BAND_PCT/SIDEWAYS_VOL_THRESHOLD_PCT above);
# a month satisfying none of the 4 required regimes is "unclassified"
# and excluded from sampling instead of being forced into "sideways".
# The manifest also gained watchlist_tickers/watchlist_audit_method/
# watchlist_audit_version, and each episode gained a watchlist_viability
# section -- see audit_episode_watchlist_viability / propose_manifest.
# v4 (was 3, 2026-09-16 correction pass): select_holdout_pool's single
# globally-trailing-block holdout (all 4 regimes inside ONE contiguous
# trailing window) is REPLACED by select_paired_episodes -- one comparison
# + one later holdout PER REGIME, independently. The prior design was
# proven infeasible for this project's real eligible range (both real
# 'selloff' months sit too early for any 'sideways' month to ever share a
# trailing window with them -- docs/evaluations/
# fundamentals_v1_measurement_block_2026-09-15.md). Bumped so any manifest
# generated under the old algorithm is refused, never silently
# reinterpreted under the new per-regime pairing (require_approved's
# manifest_version gate, correction pass item E.3). Each episode gained
# `role` ('comparison'/'holdout') and `pair_id`; the manifest's
# `holdout_pool` section is replaced by `holdout_design`, which states the
# caveat explicitly: holdout episodes are later than their OWN regime's
# comparison episode only, never guaranteed later than every comparison
# episode globally.
MANIFEST_VERSION = 4
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


def select_paired_episodes(
    eligible_months: list[MonthMetrics], seed: int,
) -> dict[str, tuple[MonthMetrics, MonthMetrics]]:
    """Deterministic PER-REGIME paired selection (2026-09-16 correction
    pass) -- REPLACES the old select_holdout_pool, which required ALL 4
    regimes to appear inside ONE contiguous trailing window. That design
    was proven infeasible for this project's real watchlist-viable
    eligible range (2022-06-01..2026-01-31): both real 'selloff' months
    (2022-09, 2022-12) sit too early for any 'sideways' month (earliest
    2023-04) to ever share a trailing window with them -- see
    docs/evaluations/fundamentals_v1_measurement_block_2026-09-15.md.

    For each of REQUIRED_REGIMES independently: needs at least 2 eligible
    months of that regime (one comparison, one strictly later holdout of
    the SAME regime) -- fails clearly, naming the regime and how many
    months it actually has, if not. When more than 2 exist, which 2 are
    used is chosen by a seeded RNG (sample without replacement) --
    deterministic for the same (months, seed), and can only differ
    between seeds when a regime genuinely has more than 2 eligible
    months to pick from. Regardless of which 2 are drawn, the
    chronologically EARLIER of the pair is always the comparison episode
    and the LATER is always its holdout -- so when a regime has exactly 2
    eligible months (the real 'selloff' case today), the pair is forced
    into chronological order by construction, never a coin flip and never
    hand-picked.

    Returns {regime: (comparison, holdout)} for exactly the 4 required
    regimes. 'Later' here means later than its OWN regime's comparison
    month only -- never a claim that every holdout episode is later than
    every comparison episode across all 4 regimes. The manifest's own
    `holdout_design` field states this caveat explicitly."""
    by_regime: dict[str, list[MonthMetrics]] = defaultdict(list)
    for m in eligible_months:
        by_regime[m.regime].append(m)
    rng = random.Random(seed)
    pairs: dict[str, tuple[MonthMetrics, MonthMetrics]] = {}
    for regime in sorted(REQUIRED_REGIMES):
        pool = sorted(by_regime.get(regime, []), key=lambda m: (m.year, m.month))
        if len(pool) < 2:
            raise ManifestGenerationError(
                f"regime {regime!r} has only {len(pool)} eligible month(s) in this "
                "eligible range -- a per-regime comparison/holdout pair needs at "
                "least 2 (one comparison, one strictly later holdout of the same "
                "regime). Widen the eligible range, or accept this range cannot "
                "support the paired design; not fabricating a second month."
            )
        comparison, holdout = sorted(rng.sample(pool, 2), key=lambda m: (m.year, m.month))
        pairs[regime] = (comparison, holdout)
    return pairs


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


def _episodes_with_viability(episodes: list[MonthMetrics], role: str, watchlist: list[dict],
                             fetch_tech, fetch_closes) -> list[dict]:
    """MonthMetrics -> episode dict, each carrying its own
    watchlist_viability plus `role` ('comparison'/'holdout') and
    `pair_id` (2026-09-16: the per-regime paired design's matching-pair
    identity -- exactly one comparison and one holdout episode ever share
    a pair_id, since select_paired_episodes produces exactly one pair per
    required regime and the regime itself is a stable, unique key for
    that pair). Fails clearly and NAMES the ticker/month -- never silently
    drops or fabricates coverage for an episode whose watchlist data
    isn't actually there (correction pass, part 2)."""
    out = []
    for m in episodes:
        ep = m.to_dict()
        ep["role"] = role
        ep["pair_id"] = f"{m.regime}_pair"
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
    # selection here, at the single point select_paired_episodes reads
    # from -- it never sees an "unclassified" regime bucket at all. Still
    # recorded in all_classified_months below, for a complete, honest
    # audit trail.
    eligible_months = [m for m in all_months if m.regime != "unclassified"]
    if not eligible_months:
        raise ManifestGenerationError(
            f"every classified month in [{eligible_start}, {eligible_end}] is "
            "'unclassified' under the current thresholds -- nothing eligible to "
            "sample from"
        )

    # 2026-09-16 correction pass: one comparison + one later holdout
    # episode PER REGIME independently (select_paired_episodes), not one
    # single globally-trailing holdout block -- see that function's own
    # docstring for why the prior design was infeasible for this range.
    pairs = select_paired_episodes(eligible_months, seed)
    episodes = [pairs[regime][0] for regime in sorted(REQUIRED_REGIMES)]
    holdout_episodes = [pairs[regime][1] for regime in sorted(REQUIRED_REGIMES)]

    # Post-condition, not just a hope: structurally guaranteed (a month
    # has exactly one regime, so a comparison/holdout pair from different
    # regimes can never share a date) -- but a silent regression here
    # must be loud, never a quietly overlapping manifest.
    all_dates = [(e.year, e.month) for e in episodes + holdout_episodes]
    if len(all_dates) != len(set(all_dates)):
        raise ManifestGenerationError(
            "internal error: the same (year, month) appears more than once across "
            "comparison/holdout episodes -- refusing to write an overlapping manifest"
        )

    watchlist = watchlist if watchlist is not None else _load_default_watchlist()
    episode_dicts = _episodes_with_viability(
        episodes, "comparison", watchlist, fetch_tech, fetch_watchlist_closes)
    holdout_episode_dicts = _episodes_with_viability(
        holdout_episodes, "holdout", watchlist, fetch_tech, fetch_watchlist_closes)

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
        "holdout_design": {
            "method": "per_regime_paired",
            "description": (
                "For each required regime independently: one comparison episode "
                "and one untouched holdout episode of the SAME regime, strictly "
                "later than its own matching comparison episode -- see "
                "select_paired_episodes. Deterministic sampling with the top-level "
                "seed; when a regime has exactly 2 eligible months, they are "
                "forced into the pair in chronological order, never hand-picked."
            ),
            "caveat": (
                "This is NOT one single globally-trailing holdout period covering "
                "all required regimes at once (the prior design, proven infeasible "
                "for this eligible range -- see docs/evaluations/"
                "fundamentals_v1_measurement_block_2026-09-15.md). Each holdout "
                "episode is later only relative to its OWN regime's comparison "
                "episode; it is never guaranteed later than a DIFFERENT regime's "
                "comparison episode."
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


def _require_valid_watchlist_viability(manifest: dict, episode_set: str,
                                       episodes: list[dict] | None = None) -> None:
    """Correction pass, part 2's integration requirement: refuse to run
    ANY episode in `episode_set` unless every one of them carries a
    well-formed, passing watchlist_viability section. Called from
    run_configuration AFTER require_approved but still BEFORE any
    register_run/run_day/journal/market-data/checkpoint call."""
    for ep in (episodes if episodes is not None else manifest.get(episode_set) or []):
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
    metadata only, never a run_kind and never sent to Neon. There is
    deliberately no seed: the masking seed is derived from the approved
    manifest + episode identity (derive_episode_seed), identical across
    paired configurations of one episode."""
    config_label: str
    fundamentals_enabled: bool = False


def derive_episode_seed(manifest_content_hash: str, episode: dict) -> int:
    """Deterministic masking seed from the approved manifest's content hash
    plus the exact episode identity (year, month, regime, start, end).
    Config-independent on purpose (baseline and fundamentals arms of one
    episode share one alias mapping); no runtime randomness, so a retry
    or re-run reproduces it. Fits Neon's INTEGER masking_seed column."""
    ident = "|".join(str(x) for x in (
        manifest_content_hash, episode["year"], episode["month"],
        episode["regime"], episode["start"], episode["end"]))
    return int.from_bytes(hashlib.sha256(ident.encode()).digest()[:8], "big") % (2**31 - 1)


class EpisodeSelectionError(ValueError):
    """The single-episode selector does not identify exactly one approved
    comparison episode. Raised before any side effect."""


@dataclass(frozen=True)
class EpisodeSelector:
    """Identifies ONE comparison episode by (year, month, regime) together
    -- never a free-form date, ticker, or label. Validated on construction;
    membership in the approved manifest is checked by run_configuration."""
    year: int
    month: int
    regime: str

    def __post_init__(self):
        for name in ("year", "month"):
            v = getattr(self, name)
            if type(v) is not int:
                raise EpisodeSelectionError(f"{name} must be an int, got {v!r}")
        if not 1 <= self.month <= 12:
            raise EpisodeSelectionError(f"month must be 1..12, got {self.month!r}")
        if self.regime not in REQUIRED_REGIMES:
            raise EpisodeSelectionError(
                f"regime must be one of {sorted(REQUIRED_REGIMES)}, got {self.regime!r}")


def _select_episode(manifest: dict, selector: EpisodeSelector) -> dict:
    matches = [e for e in manifest["episodes"]
               if (e.get("year"), e.get("month"), e.get("regime"))
               == (selector.year, selector.month, selector.regime)]
    if len(matches) != 1:
        raise EpisodeSelectionError(
            f"{selector} matches {len(matches)} comparison episodes in the approved "
            "manifest -- exactly one is required")
    return matches[0]


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


def resolve_episode_end_pending(portfolio: Portfolio) -> list:
    """Episode-boundary semantics shared by every one-month evaluation
    episode (see _run_episode): an episode never resumes, so each order still
    pending at its end is resolved as rejected ("episode window ended before
    execution") through the harness's own resolve_trade_rejection path --
    best-effort per order -- and cleared. Open holdings are NOT liquidated.
    Returns the orders it resolved."""
    dangling = list(portfolio.pending_orders)
    for order in dangling:
        try:
            resolve_trade_rejection(order.decision_id, "episode window ended before execution")
        except Exception:
            pass
    portfolio.pending_orders = []
    return dangling


def _run_episode(episode: dict, config: EpisodeConfig, run_kind: str,
                 watchlist: list[dict], checkpoint_dir: Path,
                 manifest: dict,
                 fetch_fundamentals_fn=fetch_fundamentals_assessment_counts,
                 isolation_report: dict | None = None) -> dict:
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
    seed = derive_episode_seed(manifest["content_hash"], episode)
    masking = RunMasking(names, run_start=start, seed=seed)
    # Same durable frozen-config record main() writes for a fresh run
    # (backtest_run_configuration), BEFORE any market-data fetch, Hermes
    # call, journal write, or cycle-evidence write. A failure here aborts
    # the episode.
    alias_mapping = masking.mapping()
    register_run_configuration(
        run_id, watchlist, rb._watchlist_hash(watchlist), alias_mapping,
        hashlib.sha256(json.dumps(alias_mapping, sort_keys=True).encode()).hexdigest(),
        seed, episode["start"], episode["end"], config.fundamentals_enabled,
        provenance=rb.build_run_provenance(isolation_report) if isolation_report else None,
    )
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
                                limits_ctx, run_id, fundamentals_enabled=config.fundamentals_enabled,
                                runtime=(isolation_report or {}).get("runtime")),
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
    dangling_pending_orders = resolve_episode_end_pending(portfolio)

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
        "provenance": {
            "manifest_content_hash": manifest["content_hash"],
            "manifest_version": manifest["manifest_version"],
            "episode_identity": f"{episode['year']}-{episode['month']:02d}/{episode['regime']}",
            "config_label": config.config_label,
            "run_id": run_id,
            "masking_seed": seed,
            "alias_mapping_hash": hashlib.sha256(
                json.dumps(alias_mapping, sort_keys=True).encode()).hexdigest(),
            "watchlist_snapshot_hash": rb._watchlist_hash(watchlist),
        },
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
    """Performance metrics come from COMPLETED (non-halted) episodes only.
    Halted episodes are listed by identity/run_id and never averaged in;
    with zero completed episodes there are no performance metrics at all
    (never fabricated zeros). Per-episode reports stay intact."""
    completed = [e for e in per_episode if not e["halted"]]
    excluded = [
        {"episode": f"{e['episode']['year']}-{e['episode']['month']:02d}/{e['episode']['regime']}",
         "run_id": e["run_id"]}
        for e in per_episode if e["halted"]
    ]
    agg = {
        "episode_count": len(per_episode),
        "completed_count": len(completed),
        "halted_count": len(excluded),
        "excluded_episodes": excluded,
    }
    if not completed:
        agg["performance"] = None
        return agg
    n = len(completed)
    agg["performance"] = {
        "mean_return_pct": round(sum(e["return_pct"] for e in completed) / n, 4),
        "worst_max_drawdown_pct": round(min(e["max_drawdown_pct"] for e in completed), 4),
        "total_fills": sum(e["fill_count"] for e in completed),
        "total_proposals": sum(e["proposal_count"] for e in completed),
        "total_closed_trades": sum(e["closed_trade_count"] for e in completed),
        "total_blocked": sum(e["blocked_count"] for e in completed),
    }
    return agg


def run_configuration(manifest: dict, config: EpisodeConfig, run_kind: str,
                      watchlist: list[dict], checkpoint_dir: Path,
                      episode_set: str = "episodes",
                      episode_selector: EpisodeSelector | None = None) -> dict:
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
    layer of the same guarantee.

    episode_selector (optional): run EXACTLY ONE approved comparison
    episode, identified by (year, month, regime). Comparison set only --
    never the holdout. Selection and the selected episode's watchlist
    viability are validated after require_approved and before any side
    effect. Omitted -> the full-suite behavior above, unchanged. A halted
    episode is reported as halted; nothing here retries or resumes it."""
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
    if episode_selector is None:
        selected = manifest[episode_set]
        _require_valid_watchlist_viability(manifest, episode_set)
    else:
        if not isinstance(episode_selector, EpisodeSelector):
            raise EpisodeSelectionError("episode_selector must be an EpisodeSelector")
        if episode_set != "episodes":
            raise EpisodeSelectionError("single-episode selection is comparison-only; holdouts are not runnable this way")
        selected = [_select_episode(manifest, episode_selector)]
        _require_valid_watchlist_viability(manifest, episode_set, selected)
    # Same required-container check run_backtest.main() makes (single source
    # of truth for the service list). After every validation above, before
    # any registration/checkpoint/fetch/Hermes/journal side effect: a stopped
    # execution-sim otherwise halts an episode on the first fill, after a
    # real Hermes call has already been spent.
    isolation_report = preflight()
    # Correction pass, requirement 1: preflight() now provisions a fresh,
    # disposable per-run HermesRuntime -- clean it up on EVERY exit path
    # (a completed suite, a halted episode, or an exception propagating
    # out of _run_episode), same discipline as run_backtest.main().
    try:
        per_episode = [
            _run_episode(ep, config, run_kind, watchlist, checkpoint_dir, manifest,
                        isolation_report=isolation_report)
            for ep in selected
        ]
    finally:
        rb._cleanup_ephemeral_hermes_runtime((isolation_report or {}).get("runtime"))
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
# CLI -- `propose` (manifest generation) and `run-one` (one approved
# comparison episode via run_configuration's episode_selector; the full
# suite is still only reachable by calling run_configuration directly).
# ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    propose = sub.add_parser("propose", help="Generate a new PROPOSED manifest (human review required).")
    propose.add_argument("--seed", type=int, required=True)
    one = sub.add_parser("run-one", help="Run ONE approved comparison episode (real run; needs Docker/Neon).")
    one.add_argument("--manifest", type=Path, required=True)
    one.add_argument("--year", type=int, required=True)
    one.add_argument("--month", type=int, required=True)
    one.add_argument("--regime", required=True)
    one.add_argument("--fundamentals", action="store_true", help="enable fundamentals (default: frozen baseline, off)")
    one.add_argument("--checkpoint-dir", type=Path, required=True)
    args = parser.parse_args()

    if args.cmd == "run-one":
        selector = EpisodeSelector(args.year, args.month, args.regime)
        manifest = json.loads(args.manifest.read_text())
        label = "fundamentals" if args.fundamentals else "baseline"
        report = run_configuration(
            manifest, EpisodeConfig(label, fundamentals_enabled=args.fundamentals),
            EVAL_RUN_KIND, _load_default_watchlist(), args.checkpoint_dir,
            episode_selector=selector)
        print(json.dumps(report, indent=2, default=str))
        return

    if args.cmd == "propose":
        manifest = propose_manifest(seed=args.seed)
        path = save_manifest(manifest)
        print(f"wrote {path} (status=proposed -- human review required before use)")
        print(f"eligible range: {manifest['eligible_range']['start']} .. {manifest['eligible_range']['end']}")
        print(f"proxy: {manifest['proxy_ticker']}  seed: {manifest['selection_seed']}")
        print(f"holdout design: {manifest['holdout_design']['method']} "
              "(one comparison + one later holdout PER REGIME -- see caveat in the manifest)")
        n_episodes = len(manifest["episodes"]) + len(manifest["holdout_episodes"])
        print(f"watchlist viability: {len(manifest['watchlist_tickers'])} tickers, "
              f"all usable across all {n_episodes} selected episodes "
              "(propose_manifest would have failed clearly otherwise)")
        print("comparison episodes:")
        for e in manifest["episodes"]:
            print(f"  {e['year']}-{e['month']:02d}  {e['regime']:<15} pair={e['pair_id']:<20} "
                  f"return={e['monthly_return_pct']:+.2f}%  vol={e['realized_vol_pct']:.2f}%")
        print("holdout episodes (untouched until final out-of-sample eval):")
        for e in manifest["holdout_episodes"]:
            print(f"  {e['year']}-{e['month']:02d}  {e['regime']:<15} pair={e['pair_id']:<20} "
                  f"return={e['monthly_return_pct']:+.2f}%  vol={e['realized_vol_pct']:.2f}%")


if __name__ == "__main__":
    main()
