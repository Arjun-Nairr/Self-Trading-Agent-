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

2. FUTURE RUNNER -- run_configuration()/run_episode(): given an APPROVED,
   untampered manifest and an explicit configuration (e.g. fundamentals
   on/off, or a future approved rule variant), runs run_backtest.py's
   REAL, UNCHANGED per-day loop once per one-month episode, each with a
   freshly isolated Portfolio/masking/checkpoint (never touching
   backtest_state.json/cycle_checkpoint.json, never Round 2), reusing the
   EXISTING training/smoke/archived classification unchanged (an
   episode's/suite's own identity is local report metadata only -- never
   a new run_kind), and produces a compact per-episode + aggregate
   comparison report. NEITHER this module's own top-level code NOR its
   tests ever call run_configuration/run_episode against real Docker/
   Neon/Hermes -- see test_eval_suite.py's fakes. It exists to be called
   later, by a human/orchestrating script, once an approved manifest and
   an explicit configuration comparison are actually authorized.

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
    _name_variants, _run_cycle_or_halt, json_from, register_run, run_day,
    run_in,
)
from anonymize import RunMasking                                       # noqa: E402

# ---------------------------------------------------------------------
# fixed, pre-committed parameters -- chosen and documented BEFORE any
# month is ever classified, exactly what "pre-committed" requires. Changing
# any of these changes the manifest's content hash, so a change here is
# never silently invisible in a generated manifest.
# ---------------------------------------------------------------------

# 2021-01-01..2026-01-31: five full calendar years, ending the last full
# month before Round 2's own real window starts (2026-02-17) -- every
# episode date is wholly separate from Round 2's own dates by construction.
ELIGIBLE_RANGE_START = date(2021, 1, 1)
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
CLASSIFICATION_METHOD = (
    "month regime = 'high_volatility' if stdev(daily %% returns within the "
    "month) >= %.1f pct pts; elif monthly_return_pct >= %.1f: 'rally'; elif "
    "monthly_return_pct <= %.1f: 'selloff'; else 'sideways' -- checked in "
    "that fixed priority order, so a month gets exactly one label."
) % (HIGH_VOLATILITY_THRESHOLD_PCT, RALLY_THRESHOLD_PCT, SELLOFF_THRESHOLD_PCT)

# The most recent HOLDOUT_MONTHS_COUNT eligible months are reserved as the
# holdout pool -- never touched by the comparison-suite sampler below.
HOLDOUT_MONTHS_COUNT = 12
EPISODES_PER_REGIME = 2
HOLDOUT_EPISODES_PER_REGIME = 1
MANIFEST_VERSION = 1
MANIFEST_DIR = Path(__file__).resolve().parent / "eval_suites"


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
    takes only the two numbers a month's classification depends on."""
    if realized_vol_pct >= HIGH_VOLATILITY_THRESHOLD_PCT:
        return "high_volatility"
    if monthly_return_pct >= RALLY_THRESHOLD_PCT:
        return "rally"
    if monthly_return_pct <= SELLOFF_THRESHOLD_PCT:
        return "selloff"
    return "sideways"


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


def propose_manifest(seed: int, eligible_start: date = ELIGIBLE_RANGE_START,
                     eligible_end: date = ELIGIBLE_RANGE_END,
                     fetch_closes=fetch_daily_closes) -> dict:
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

    holdout_start = _nth_month_from_end(eligible_end, HOLDOUT_MONTHS_COUNT)
    comparison_pool = [m for m in all_months if (m.year, m.month) < holdout_start]
    holdout_pool = [m for m in all_months if (m.year, m.month) >= holdout_start]

    episodes = stratified_sample(comparison_pool, EPISODES_PER_REGIME, seed)
    # seed + 1: independent of the comparison draw but still fully
    # reproducible from the single top-level seed -- never a second
    # user-facing seed to track.
    holdout_episodes = stratified_sample(holdout_pool, HOLDOUT_EPISODES_PER_REGIME, seed + 1)

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
        },
        "selection_seed": seed,
        "holdout_months_count": HOLDOUT_MONTHS_COUNT,
        "episodes": [m.to_dict() for m in episodes],
        "holdout_episodes": [m.to_dict() for m in holdout_episodes],
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
    """The runner's own gate: refuse a proposed OR tampered manifest.
    Verifies the hash FIRST -- a tampered manifest is rejected regardless
    of its claimed status -- then requires status == 'approved' exactly;
    'proposed' (the only status propose_manifest ever writes) is refused
    exactly like any other non-'approved' value."""
    verify_manifest(manifest)
    if manifest.get("status") != "approved":
        raise ManifestNotApprovedError(
            f"manifest status is {manifest.get('status')!r}, not 'approved' -- "
            "a proposed manifest requires explicit human review (edit its "
            "'status' field on the saved JSON file) before any episode may run"
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


def run_episode(episode: dict, config: EpisodeConfig, run_kind: str,
                watchlist: list[dict], checkpoint_dir: Path,
                fetch_fundamentals_fn=fetch_fundamentals_assessment_counts) -> dict:
    """Runs ONE one-month episode through run_backtest's real, unchanged
    per-day loop (Portfolio/RunMasking/run_day/_run_cycle_or_halt) --
    fresh in-memory Portfolio, fresh RunMasking, and CHECKPOINT_PATH
    isolated to `checkpoint_dir` for the duration of this call only
    (restored after, even on failure -- see the try/finally) so a
    concurrent real run's own checkpoint can never be touched. Never
    writes backtest_state.json -- nothing here is resumable across a
    process restart by design (a one-month episode is short enough not to
    need it); a halted episode is simply reported as such, never retried
    automatically.

    run_kind: 'training' or 'smoke' -- the EXISTING durable classification
    (backtest_run_registry), completely unchanged. The episode's/suite's
    own identity (`config.config_label`, `episode['regime']`, etc.) is
    local report metadata only, never sent as a run_kind."""
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

    fundamentals_assessment_counts = None
    if config.fundamentals_enabled and not halted:
        try:
            fundamentals_assessment_counts = fetch_fundamentals_fn(run_id)
        except Exception:
            fundamentals_assessment_counts = None  # best-effort enrichment only

    fill_count = sum(
        1 for s in day_summaries for line in s["decisions"]
        if line.startswith("BUY ") or line.startswith("SELL ")
    )
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
        "fill_count": fill_count,
        "closed_trade_count": len(portfolio.closed_pnl),
        "blocked_count": len(blocked_reasons),
        "blocked_reasons": blocked_reasons[:20],  # bounded -- report stays compact
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
        "total_closed_trades": sum(e["closed_trade_count"] for e in per_episode),
        "total_blocked": sum(e["blocked_count"] for e in per_episode),
    }


def run_configuration(manifest: dict, config: EpisodeConfig, run_kind: str,
                      watchlist: list[dict], checkpoint_dir: Path,
                      episode_set: str = "episodes") -> dict:
    """The comparison entry point -- requires an APPROVED, untampered
    manifest (require_approved raises otherwise). Runs `config` against
    every episode in manifest[episode_set] ('episodes', the comparison
    suite, by default; 'holdout_episodes' for the separate, later,
    out-of-sample set), each freshly isolated (see run_episode), and
    returns a compact per-episode + aggregate report. Every configuration
    compared this way must be run against the SAME manifest object/file
    for the comparison to be fair -- this function itself does not enforce
    that across separate calls, the caller must reuse one loaded manifest.
    NEVER called by this module's own code."""
    require_approved(manifest)
    if episode_set not in ("episodes", "holdout_episodes"):
        raise ValueError(f"episode_set must be 'episodes' or 'holdout_episodes', got {episode_set!r}")
    per_episode = [
        run_episode(ep, config, run_kind, watchlist, checkpoint_dir)
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
