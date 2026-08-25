"""
run_backtest.py — the loop that finally connects everything.

WHY HERMES IS FED DATA RATHER THAN CALLING ITS OWN TOOLS (the one decision
that shapes this whole file): during a backtest Hermes must see MASKED,
POINT-IN-TIME data. If it called maverick-mcp or the news tool itself it
would pass the real ticker and get today's real data — no alias, no as_of —
and both safety gates would be bypassed in a way nothing would notice. So
during backtesting the harness does all the I/O and Hermes is a pure
reasoner. AGENTS.md's backtest section says the same thing to Hermes.

The portfolio is simulated here too, for the same reason: the real Alpaca
account lives in the present. A 2026-02 cycle cannot ask it what equity was
on that date, so the harness tracks cash/positions/equity itself and marks
them to that day's real historical close.

Stops, targets and time-stops are executed mechanically by the harness, not
asked of Hermes — a real stop rests at the broker and fires without anyone
deciding anything. Hermes handles entries and discretionary (thesis-broken)
exits, which is where judgment actually lives.

Every order still passes the SAME risk_checker the live path uses, with
fuller context: the harness holds the trade journal, so unlike alpaca-mcp it
can also enforce §4a (losing streaks) and §9.4 (same-day re-entry).

PHASE 0 REPAIR (2026-08-21) -- NO SAME-DAY LOOKAHEAD: a discretionary BUY or
SELL Hermes decides on day T's completed data is QUEUED (see QueuedOrder),
never filled that same day. It resolves against day T+1's REAL open on the
very next cycle, re-validated by the SAME risk checker against the actual
opening price -- never against what Hermes originally guessed. See
resolve_queued_order and the "STEP B" block in run_day. Also from that
repair: run_day operates on a deep-copied working portfolio and only the
caller adopts it on success (never publish a partially mutated portfolio);
a failed cycle halts the run rather than silently skipping the day (see
_run_cycle_or_halt); and every journal write now carries a deterministic
decision_id (see make_decision_id) so retrying a failed day cannot
duplicate an already-journaled trade.

Usage:
    python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days 3
"""
import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "risk-checker"))

from anonymize import RunMasking                                    # noqa: E402
from quota_pacer import QuotaPacer                                  # noqa: E402
from risk_checker import (                                          # noqa: E402
    AccountState, Context, MarketClock, Position, ProposedTrade, check_trade,
)

REPO = Path(__file__).resolve().parent.parent
STATE_PATH = Path(__file__).resolve().parent / "backtest_state.json"
STARTING_EQUITY = 100_000.0
# RULES.md §6c, revised from 10 -> 15 trading days. The original 10-day
# figure, paired with the 5% stop floor (§6a) and the 2:1 target minimum
# (§6b), asks for a 10%+ move inside a window whose typical 10-day
# excursion (for ~30% annualized-vol mid-caps, sigma*sqrt(10)) is roughly
# 6% -- the target was rarely reachable before the clock ran out regardless
# of setup quality. 15 days gives the same target real room to play out
# without touching the stop-placement math §2b's gap-protection derivation
# depends on. [JUDGMENT, revised] -- flagged in RULES.md §10 for a real
# second opinion once tuning-window results exist, same as every other
# number there.
TIME_STOP_TRADING_DAYS = 15

# -- name-variant derivation for the masking gate --------------------------
# The masker only strips terms it's told about (see anonymize.py's own
# docstring: "config, not code"). A single split(",")[0] pass leaves a
# trailing suffix on any name with two ("Warner Music Group Corp." ->
# stripping "Corp." once leaves "Warner Music Group", not the bare brand).
# Found via a real leak test: 7 of 10 watchlist names sailed through actual
# headline text unmasked ("Bruker Shares Fall...", "Olin Announces...")
# because the old one-shot strip never produced the bare brand at all.
_CORP_SUFFIXES = re.compile(
    r",?\s*(?:Corporation|Corp\.?|Holdings?|Group|Company|Co\.?|plc|Ltd\.?|Inc\.?|LLC)\s*$",
    re.IGNORECASE,
)

# Names where even the fully-stripped legal name doesn't reach the form
# headlines actually use ("Sprouts Farmers Market" -> "Sprouts", not further
# reducible by suffix-stripping since "Farmers Market" isn't a corporate
# suffix). Hand-verified against real headline phrasing, same as
# anonymize.py's own brand/counterparty test -- this is exactly the "config,
# not code" extension point that module's docstring describes.
_BRAND_OVERRIDES = {
    "SFM": ["Sprouts"],
    "BRX": ["Brixmor"],
    "ALGM": ["Allegro"],
}


def _name_variants(name: str, symbol: str) -> list[str]:
    """Full legal name -> every maskable form, longest first isn't required
    here (RunMasking itself sorts by length) -- just needs to include every
    form real headlines use."""
    variants = {name}
    current = name
    while True:
        stripped = _CORP_SUFFIXES.sub("", current).strip()
        if stripped == current or not stripped:
            break
        variants.add(stripped)
        current = stripped
    variants.update(_BRAND_OVERRIDES.get(symbol, []))
    return sorted(variants)

# maverick runs under uv; the other containers run plain python.
RUNNERS = {
    "maverick-mcp": ["uv", "run", "python", "-"],
    "alpaca-mcp": ["python", "-"],
    "execution-sim": ["python", "-"],
    "trade-journal": ["python", "-"],
}


def run_in(service: str, script: str) -> str:
    """Pipe a script into a container and return its stdout.

    The harness runs on the host because no single container can reach every
    tool — the hub-and-spoke network isolation means only the gateway can,
    and it is not a place to run orchestration code.
    """
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", service, *RUNNERS[service]],
        input=script, capture_output=True, text=True, cwd=REPO, timeout=300,
        # Explicit UTF-8, or Windows encodes the piped script as cp1252 and
        # any non-ASCII character in Hermes' reasoning (an em-dash is
        # enough) reaches the container as invalid UTF-8, killing the
        # journal write with a SyntaxError.
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"{service} failed:\n{result.stderr[-2000:]}")
    return result.stdout


def preflight() -> None:
    """Fail fast and clearly if a container the loop needs is not running.

    Found the hard way: `execution-sim` ships with `restart: "no"` (it was a
    dev-only tool before being repurposed for backtest fills), so it is
    stopped by default. The loop then died mid-cycle with a bare
    'service is not running' — after already spending a Hermes call and a
    quota slot. Cheaper to check all five up front.
    """
    required = ["gateway", "maverick-mcp", "alpaca-mcp", "trade-journal",
                "execution-sim"]
    running = subprocess.run(
        ["docker", "compose", "ps", "--services", "--filter", "status=running"],
        capture_output=True, text=True, cwd=REPO,
    ).stdout.split()
    missing = [s for s in required if s not in running]
    if missing:
        raise SystemExit(
            f"Not running: {', '.join(missing)}\n"
            f"Start them with:  docker compose up -d {' '.join(missing)}"
        )


def json_from(text: str) -> dict:
    """Extract a JSON object from output that may carry prose around it."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON found in:\n{text[:500]}")
    return json.loads(text[start : end + 1])


# --------------------------------------------------------------------------
# simulated portfolio
# --------------------------------------------------------------------------

@dataclass
class Holding:
    ticker: str
    quantity: int
    entry_price: float
    stop_price: float
    target_price: float
    entry_day: int           # calendar day_index -- display only ("days_held")
    entry_trading_day: int   # Portfolio.trading_days_elapsed at entry -- what
                              # RULES.md §6c's time stop actually counts against


@dataclass
class QueuedOrder:
    """A discretionary (BUY or SELL) decision Hermes made on `queued_day_index`,
    awaiting the NEXT real trading day's open before it can fill.

    PHASE 0 REPAIR (2026-08-21): this is what removes same-day lookahead.
    Hermes previously reasoned on day T's own completed data and got filled
    within that SAME day's range -- meaning the decision and the fill both
    drew on day T's fully-known outcome, which is lookahead by construction
    even though no individual line looked wrong. Now every discretionary
    decision is queued here instead of executed immediately; `run_day`
    resolves each one against the FOLLOWING real trading day's actual open,
    never against `entry_price` (see `resolve_queued_order`).

    `entry_price` is kept for audit/logging ONLY (what Hermes originally
    proposed, for the activity trail) -- it must never be fed into
    `ProposedTrade.entry_price` at fill time, which is the actual guarantee
    against lookahead. `decision` carries the raw Hermes response dict
    (confidence, primary_signal, reasoning, ...) needed to journal the
    eventual fill honestly.
    """
    ticker: str
    alias: str
    action: str                       # "BUY" or "SELL"
    quantity: int
    stop_price: float | None          # required for BUY, unused for SELL
    target_price: float | None        # required for BUY, unused for SELL
    entry_price: float                # Hermes' original guess -- audit only, never used to fill
    queued_day_index: int
    decision: dict


@dataclass
class Portfolio:
    cash: float = STARTING_EQUITY
    holdings: dict[str, Holding] = field(default_factory=dict)
    # Seeded flat, not empty. The risk checker fails closed on an
    # undeterminable weekly P&L — correct for live, where a missing figure
    # means something broke, but wrong at the start of a backtest, where
    # "no history yet" is expected. Left empty it would block the first
    # five cycles of every run, burning quota-limited cycles on a
    # non-problem. Seeding is not a fudge: an untouched account genuinely
    # was flat at its starting equity before the window opened.
    equity_curve: list[float] = field(
        default_factory=lambda: [STARTING_EQUITY] * 6
    )
    closed_pnl: list[float] = field(default_factory=list)
    stopped_out_today: list[str] = field(default_factory=list)
    # Persisted, not a local loop variable -- the harness is invoked as a
    # separate process per run (--days N each time, resumed from state), so
    # a plain in-memory counter would reset every invocation and "every 5
    # cycles" would never mean anything real across runs.
    cycles_since_reflection: int = 0
    # Count of EXECUTED cycles, not calendar days -- day_index counts
    # weekends and no-data days too, so "10 trading days" was silently
    # running as ~7. This is what RULES.md §6c's time stop actually
    # increments against; entry_day (calendar) stays only for the
    # dashboard-facing "days_held" display.
    trading_days_elapsed: int = 0
    # Discretionary decisions awaiting the next real trading day's open --
    # see QueuedOrder. Persisted (not a local run_day variable) so a
    # process restart between the decision day and the fill day cannot
    # lose the order -- Phase 0 repair.
    pending_orders: list[QueuedOrder] = field(default_factory=list)

    def equity(self, prices: dict[str, dict]) -> float:
        held = sum(
            h.quantity * prices.get(t, {}).get("close", h.entry_price)
            for t, h in self.holdings.items()
        )
        return self.cash + held

    def invested(self, prices: dict[str, dict]) -> float:
        return sum(
            h.quantity * prices.get(t, {}).get("close", h.entry_price)
            for t, h in self.holdings.items()
        )

    def pct_since(self, index_back: int) -> float | None:
        """% change vs `index_back` entries ago — None when there is not
        enough history yet, matching get_account's honest-null contract so
        the risk checker fails closed rather than reading a fabricated 0.0."""
        if len(self.equity_curve) <= index_back:
            return None
        past = self.equity_curve[-1 - index_back]
        if not past:
            return None
        return round((self.equity_curve[-1] - past) / past * 100, 2)

    def drawdown_pct(self) -> float:
        peak = max(self.equity_curve)
        return round((self.equity_curve[-1] - peak) / peak * 100, 2) if peak else 0.0

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "holdings": {t: vars(h) for t, h in self.holdings.items()},
            "equity_curve": self.equity_curve,
            "closed_pnl": self.closed_pnl,
            "cycles_since_reflection": self.cycles_since_reflection,
            "trading_days_elapsed": self.trading_days_elapsed,
            "pending_orders": [vars(o) for o in self.pending_orders],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Portfolio":
        return cls(
            cash=d["cash"],
            holdings={
                # .get() on entry_trading_day: old state files predate this
                # field. Falls back to the calendar entry_day, an
                # approximation, only for a holding that was already open
                # before this fix shipped.
                t: Holding(**{**h, "entry_trading_day": h.get("entry_trading_day", h["entry_day"])})
                for t, h in d["holdings"].items()
            },
            equity_curve=d["equity_curve"],
            closed_pnl=d["closed_pnl"],
            # .get(): old state files predate these fields.
            cycles_since_reflection=d.get("cycles_since_reflection", 0),
            trading_days_elapsed=d.get("trading_days_elapsed", 0),
            pending_orders=[QueuedOrder(**o) for o in d.get("pending_orders", [])],
        )


# --------------------------------------------------------------------------
# queued-order resolution -- pure, no I/O (Phase 0 repair)
#
# Mirrors risk_checker.py's own design philosophy on purpose: a decision
# this safety-critical needs to be exhaustively unit-testable against
# hand-built scenarios, which is only possible if it does zero I/O and
# takes every fact as an explicit argument.
# --------------------------------------------------------------------------

@dataclass
class QueuedOrderOutcome:
    accepted: bool
    reason: str = ""                        # rejection reason, if not accepted
    same_day_exit_price: float | None = None
    same_day_exit_reason: str | None = None  # "stop hit" | "target hit"


def resolve_queued_order(order: QueuedOrder, bar: dict, ctx: Context) -> QueuedOrderOutcome:
    """Decide whether a queued order fills at `bar["open"]` (the scheduled
    day's real opening price) -- NEVER at `order.entry_price`, which is
    Hermes' stale, pre-gap guess and is not even read here.

    Re-runs the SAME risk checker used at decision time, but now against
    the REAL open. Rejects outright on any breach (stop distance, reward:
    risk, exposure, cash, sizing) -- never silently resizes a gapped
    order to make it fit, which would substitute the checker's judgment
    for Hermes' and hide how large the gap actually was.

    For a BUY that's accepted: the position was just opened at the very
    start of this trading day, so the REST of that same day's real range
    can still resolve its stop or target before the day is over. OHLC
    alone cannot say whether price touched the stop or the target FIRST
    when both fall inside [low, high] -- resolved conservatively (assume
    the stop, never the target), matching the mechanical-exit priority
    ordering used elsewhere in this file (stop checked before target).
    """
    open_price = bar["open"]
    trade = ProposedTrade(
        ticker=order.ticker, action=order.action, quantity=order.quantity,
        entry_price=open_price, stop_price=order.stop_price, target_price=order.target_price,
    )
    verdict = check_trade(trade, ctx)
    if not verdict.allowed:
        return QueuedOrderOutcome(accepted=False, reason="; ".join(verdict.violations))

    if order.action == "BUY":
        if bar["low"] <= order.stop_price:
            return QueuedOrderOutcome(
                accepted=True,
                same_day_exit_price=min(bar["open"], order.stop_price),
                same_day_exit_reason="stop hit",
            )
        if bar["high"] >= order.target_price:
            return QueuedOrderOutcome(
                accepted=True, same_day_exit_price=order.target_price, same_day_exit_reason="target hit",
            )
    return QueuedOrderOutcome(accepted=True)


# Fixed, arbitrary constant -- only needs to be stable across runs of THIS
# project, never collide with a real UUID, and never change once chosen.
_DECISION_ID_NAMESPACE = uuid.UUID("6f9c9a1e-6b3a-4b8e-9c8a-2f7a2e6f9a10")


def make_decision_id(run_key: str, day_index: int, ticker: str, action: str, kind: str) -> str:
    """Deterministic decision_id -- the SAME (run, day, ticker, action, kind)
    always produces the SAME uuid5. Paired with trade-journal's
    `ON CONFLICT (decision_id) DO NOTHING`, this is what makes retrying a
    failed day safe: the retry regenerates the identical id for the
    identical real-world event instead of a fresh random one, so a
    second attempt at an already-journaled trade is a no-op, not a
    duplicate row. `kind` disambiguates same-ticker-same-action events on
    one day that come from different code paths (a mechanical exit vs. a
    queued discretionary fill vs. a same-day post-entry exit) -- RULES.md
    §9.1 already guarantees at most one open lot per ticker, so this is
    defensive, not load-bearing on its own.
    """
    key = f"{run_key}|{day_index}|{ticker}|{action}|{kind}"
    return str(uuid.uuid5(_DECISION_ID_NAMESPACE, key))


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write-to-temp-then-atomic-rename -- the same pattern quota_pacer.py's
    `_save` already uses. A process killed mid-write (exactly the failure
    mode this whole repair is about) must never leave backtest_state.json
    half-written; `os.replace` is atomic on both POSIX and Windows for a
    same-directory rename, so a reader sees either the fully-old or
    fully-new file, never a partial one."""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".backtest_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# data fetchers (point-in-time gated)
# --------------------------------------------------------------------------

def fetch_bars(tickers: list[str], day: date) -> dict[str, dict]:
    """That day's real OHLC — used to mark the portfolio and to decide
    whether a resting stop/limit would have been hit."""
    script = f'''
import json, yfinance as yf
out = {{}}
for t in {tickers!r}:
    h = yf.Ticker(t).history(start="{day}", end="{day + timedelta(days=1)}")
    if not h.empty:
        r = h.iloc[0]
        out[t] = {{"open": float(r["Open"]), "high": float(r["High"]),
                   "low": float(r["Low"]), "close": float(r["Close"])}}
print(json.dumps({{"bars": out}}))
'''
    return json_from(run_in("maverick-mcp", script))["bars"]


def fetch_technicals(tickers: list[str], day: date) -> dict[str, dict]:
    """Point-in-time technicals — `as_of` is the no-future-data gate."""
    script = f'''
import asyncio, json
from datetime import date
from maverick.platform.config import get_platform_settings
from maverick.platform.db import create_engine_from_settings
from maverick.platform.cache import Cache
from maverick.market_data.fetchers import YFinanceFetcher, build_mover_fetcher
from maverick.market_data.config import get_market_data_settings
from maverick.market_data.service import MarketDataService
from maverick.technical.service import TechnicalService

async def main():
    s = get_platform_settings()
    yf = YFinanceFetcher(http_settings=s.http)
    md = MarketDataService(create_engine_from_settings(s.database),
                           Cache(s.cache, redis_settings=s.redis), yf,
                           build_mover_fetcher(get_market_data_settings(), yf))
    tech = TechnicalService(md)
    out = {{}}
    for t in {tickers!r}:
        try:
            r = await tech.get_full_analysis(t, as_of=date.fromisoformat("{day}"))
            d = r.model_dump()
            out[t] = {{"price": d["current_price"],
                       "rsi": d["rsi"]["current"], "rsi_signal": d["rsi"]["signal"],
                       "macd_signal": d["macd"]["indicator_signal"],
                       "trend": d["trend"]["direction"], "outlook": d["outlook"],
                       "support": d["levels"]["support"][:3],
                       "resistance": d["levels"]["resistance"][:3]}}
        except Exception as e:
            out[t] = {{"error": str(e)[:120]}}
    print(json.dumps({{"tech": out}}, default=str))

asyncio.run(main())
'''
    return json_from(run_in("maverick-mcp", script))["tech"]


def fetch_news(tickers: list[str], day: date) -> dict[str, list]:
    script = f'''
import asyncio, json
from server import get_historical_news

async def main():
    out = {{}}
    for t in {tickers!r}:
        arts = await get_historical_news(t, "{day}", lookback_days=7)
        out[t] = [{{"headline": a["headline"], "created_at": a["created_at"],
                    "summary": a.get("summary", "")[:200], "symbols": a.get("symbols", [])}}
                  for a in arts[:4]]
    print(json.dumps({{"news": out}}))

asyncio.run(main())
'''
    return json_from(run_in("alpaca-mcp", script))["news"]


def simulate_fill(ticker_alias: str, action: str, quantity: int,
                  reference_price: float, bar: dict) -> dict:
    """Real execution-sim tool, in historical mode — the day's true range
    decides whether a limit would have crossed, and now also clamps MARKET
    fills into the same range (a bad/stale reference_price used to be able
    to fill at a price that never traded that day)."""
    script = f'''
import json
from server import place_order
print(json.dumps(place_order(
    ticker="{ticker_alias}", action="{action}", quantity={quantity},
    order_type="MARKET", reference_price={reference_price},
    day_low={bar["low"]}, day_high={bar["high"]})))
'''
    return json_from(run_in("execution-sim", script))


def record_trade(decision: dict, real_ticker: str, fill: dict, decision_id: str) -> str:
    """Journal the REAL ticker, never the alias — the mask exists to keep
    Hermes honest, not to obscure our own records.

    decision_id is now REQUIRED, deterministic (see make_decision_id), and
    supplied by the caller rather than generated server-side -- Phase 0
    repair, so retrying a failed day cannot double-journal this trade."""
    script = f'''
import asyncio, json
from server import _record_decision, _record_execution_result

async def main():
    d = await _record_decision(
        decision_id={decision_id!r},
        ticker={real_ticker!r}, action={decision["action"]!r},
        intended_quantity={int(decision["quantity"])},
        intended_notional={float(decision["quantity"]) * float(decision["entry_price"])},
        order_type="MARKET",
        confidence={int(decision.get("confidence", 3))},
        primary_signal={decision.get("primary_signal", "technical")!r},
        technical_basis={decision.get("technical_basis") or "multiple"!r},
        expected_reward_risk={float(decision.get("expected_reward_risk", 2.0))},
        invalidation_condition={decision.get("invalidation_condition", "n/a")!r},
        strategy_notes={decision.get("reasoning", "")[:900]!r},
    )
    await _record_execution_result(
        d["decision_id"], status={fill["status"]!r},
        broker_order_id={fill.get("broker_order_id")!r},
        fill_price={fill.get("fill_price")!r},
        fill_quantity={fill.get("fill_quantity")!r},
        execution_mode="backtest",
    )
    # str(): asyncpg returns decision_id as a UUID object, which json
    # cannot serialise — this broke every recorded trade until caught.
    print(json.dumps({{"decision_id": str(d["decision_id"])}}))

asyncio.run(main())
'''
    return json_from(run_in("trade-journal", script))["decision_id"]


def record_exit(real_ticker: str, quantity: int, exit_price: float, reason: str, decision_id: str) -> str:
    """Journal a mechanical exit (stop/target/time-stop) as a real SELL row.

    Found missing by actually checking the dashboard against real data, not
    assumed: mechanical exits updated the harness's own portfolio and wrote
    an activity narration line, but never touched the `trades` table at
    all. That's not just a dashboard bug -- it means the journal, the
    project's actual source of truth, had no record of how a position
    resolved. `record_trade` doesn't fit here (it's shaped around a
    Hermes-authored entry decision with real confidence/reasoning); this is
    its mechanical-exit counterpart, tagged 'other' honestly rather than
    inventing a fake technical thesis for a stop firing.

    decision_id is now REQUIRED and deterministic (see make_decision_id) --
    Phase 0 repair, same idempotency reasoning as record_trade.
    """
    script = f'''
import asyncio, json
from server import _record_decision, _record_execution_result

async def main():
    d = await _record_decision(
        decision_id={decision_id!r},
        ticker={real_ticker!r}, action="SELL",
        intended_quantity={int(quantity)},
        intended_notional={float(quantity) * float(exit_price)},
        order_type="MARKET",
        confidence=5, primary_signal="other", technical_basis=None,
        expected_reward_risk=1.0,
        invalidation_condition="n/a -- mechanical exit, not a discretionary decision",
        strategy_notes={f"Mechanical exit: {reason}"!r},
    )
    await _record_execution_result(
        d["decision_id"], status="filled",
        fill_price={float(exit_price)!r}, fill_quantity={int(quantity)},
        execution_mode="backtest",
    )
    print(json.dumps({{"decision_id": str(d["decision_id"])}}))

asyncio.run(main())
'''
    return json_from(run_in("trade-journal", script))["decision_id"]


def log_activity(events: list[tuple[str, str, str | None]]) -> None:
    """Narrate a cycle's real steps on Hermes' behalf.

    Backtest cycles never call log_activity themselves -- Hermes doesn't
    touch its own tools in backtest mode at all (see the module docstring:
    calling the data tools directly would bypass both safety gates). That
    means without this, agent_activity -- the table built specifically for
    "what is Hermes doing right now" -- would sit permanently empty for
    every training cycle, and the dashboard's whole point-of-the-exercise
    fold would just be blank. The harness knows every real step already;
    this narrates them the same way Hermes would live, in one batched call
    per cycle rather than one container exec per event.
    """
    if not events:
        return
    rows = ", ".join(
        f"({event_type!r}, {description[:900]!r}, {decision_id!r})"
        for event_type, description, decision_id in events
    )
    script = f'''
import asyncio
from server import _log_activity

async def main():
    for event_type, description, decision_id in [{rows}]:
        await _log_activity(event_type, description, decision_id, mode="backtest")

asyncio.run(main())
'''
    run_in("trade-journal", script)


def record_snapshot(day_label: str, equity: float, cash: float,
                    exposure_pct: float, drawdown_pct: float,
                    open_positions: int) -> None:
    """Write once per cycle, after marking positions to market. This is the
    only source of equity-over-time data the dashboard has -- the harness
    otherwise only ever tracks its portfolio in the local state file, which
    a browser reading Neon has no way to see."""
    script = f'''
import asyncio
from server import _record_portfolio_snapshot

async def main():
    await _record_portfolio_snapshot(
        mode="backtest", equity={equity!r}, cash={cash!r},
        exposure_pct={exposure_pct!r}, drawdown_pct={drawdown_pct!r},
        day_label={day_label!r}, open_positions={open_positions},
    )

asyncio.run(main())
'''
    run_in("trade-journal", script)


def fetch_recent_insights(limit: int = 2) -> list[dict]:
    """The feed-back half of reflection: past insight text, injected into
    EVERY prompt (not just reflection days) so learning actually changes
    later behaviour. Writing to insight_log without this would just be
    journaling into a void -- the whole point of "self-learning" is that
    cycle N+5 reasons differently because of what cycle N produced.

    Returns dicts, not bare strings, so the caller can show sample_size
    alongside the text -- an insight drawn from 2 trades reads with the
    same authority as one drawn from 40 unless its weight is visible.
    """
    script = f'''
import asyncio, json
from server import _get_insight_history

async def main():
    rows = await _get_insight_history(cycle_type="weekly_reflection", limit={limit})
    print(json.dumps(
        [{{"text": r["insight_text"], "sample_size": r["sample_size"]}} for r in rows],
        default=str,
    ))

asyncio.run(main())
'''
    return json_from_list(run_in("trade-journal", script))


def record_insight(text: str, sample_size: int) -> None:
    script = f'''
import asyncio
from server import _log_insight

async def main():
    await _log_insight("weekly_reflection", {text!r}, sample_size={sample_size})

asyncio.run(main())
'''
    run_in("trade-journal", script)


def fetch_recent_closed_trades(masking: "RunMasking", limit: int = 8) -> list[dict]:
    """Masked review material for a reflection cycle -- real tickers never
    leave this function. Pairs BUY/SELL by ticker (RULES.md §9.1 guarantees
    one open lot per ticker, so simple sequential pairing is exact, not an
    approximation -- same logic as the dashboard's pairTrades, in Python
    here since this runs harness-side, not in a browser).
    """
    script = f'''
import asyncio, json
from server import _get_trade_history

async def main():
    rows = await _get_trade_history(limit=200)
    rows = [r for r in rows if r.get("execution_mode") == "backtest"]
    print(json.dumps(rows, default=str))

asyncio.run(main())
'''
    rows = json_from_list(run_in("trade-journal", script))
    rows.sort(key=lambda r: r["created_at"])  # oldest first, for correct pairing

    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    closed = []
    for ticker, trades in by_ticker.items():
        entry = None
        for t in trades:
            if t["status"] != "filled":
                continue
            if t["action"] == "BUY":
                entry = t
            elif t["action"] == "SELL" and entry:
                pnl = (float(t["fill_price"]) - float(entry["fill_price"])) * float(entry["fill_quantity"])
                closed.append({
                    "alias": masking.alias(ticker),
                    "invalidation_condition": entry.get("invalidation_condition", ""),
                    "confidence": entry.get("confidence"),
                    "exit_reason": (t.get("strategy_notes") or "").replace("Mechanical exit: ", ""),
                    "pnl": round(pnl, 2),
                    "won": pnl >= 0,
                })
                entry = None
    return closed[-limit:]


def json_from_list(text: str) -> list:
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array found in:\n{text[:500]}")
    return json.loads(text[start : end + 1])


# --------------------------------------------------------------------------
# prompt + Hermes
# --------------------------------------------------------------------------

def build_prompt(day_label: str, account: dict, positions: list[dict],
                 candidates: list[dict], recent_insights: list[str],
                 reflection_trades: list[dict] | None = None) -> str:
    lines = [
        f"BACKTEST CYCLE — {day_label}", "",
    ]

    if recent_insights:
        lines.append(
            "YOUR RECENT REFLECTIONS (weigh these, don't just repeat them -- "
            "one drawn from very few trades is a hunch, not a rule)"
        )
        for insight in recent_insights:
            n = insight.get("sample_size")
            weight = f" (from {n} closed trade{'s' if n != 1 else ''})" if n is not None else ""
            lines.append(f"  - {insight['text']}{weight}")
        lines.append("")

    lines += [
        "ACCOUNT",
        f"  equity ${account['equity']:,.2f} | cash ${account['cash']:,.2f} "
        f"| exposure {account['exposure_pct']:.1f}%",
        f"  daily {account['daily_pl_pct']} | weekly {account['weekly_pl_pct']} "
        f"| drawdown {account['drawdown_pct']}%", "",
    ]

    lines.append("OPEN POSITIONS")
    if positions:
        for p in positions:
            lines.append(
                f"  {p['alias']}: {p['quantity']} @ ${p['entry_price']:.2f} "
                f"| now ${p['current_price']:.2f} | stop ${p['stop_price']:.2f} "
                f"| target ${p['target_price']:.2f} | held {p['days_held']}d"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("CANDIDATES")
    for c in candidates:
        t = c["tech"]
        if "error" in t:
            continue
        lines.append(
            f"  {c['alias']} [{c['sector']}] ${t['price']:.2f} | RSI {t['rsi']:.1f}"
            f" ({t['rsi_signal']}) | MACD {t['macd_signal']} | trend {t['trend']}"
            f" | outlook {t['outlook']}"
        )
        lines.append(f"      support {t['support']}  resistance {t['resistance']}")
        for a in c["news"][:3]:
            lines.append(f"      news ({a['age_days']}d ago): {a['headline'][:110]}")
    lines.append("")

    schema = (
        '{"decisions": [{"alias": "...", "action": "BUY"|"SELL", "quantity": N,\n'
        '  "entry_price": N, "stop_price": N, "target_price": N,\n'
        '  "confidence": 1-5, "primary_signal": "technical"|"sentiment"|"both"|"other",\n'
        '  "technical_basis": "rsi"|"macd"|"support_resistance"|"trend"|"multiple"|"other",\n'
        '  "expected_reward_risk": N, "invalidation_condition": "...",\n'
        '  "reasoning": "..."}],\n'
        ' "no_action_reason": "..."'
    )

    if reflection_trades is not None:
        lines.append("REFLECTION DUE — review your last few closed trades")
        if reflection_trades:
            for r in reflection_trades:
                outcome = "WIN" if r["won"] else "LOSS"
                lines.append(
                    f"  {r['alias']}: {outcome} ${r['pnl']:+,.2f} | conf was "
                    f"{r['confidence']}/5 | exit: {r['exit_reason']} | "
                    f"your invalidation condition was: \"{r['invalidation_condition']}\""
                )
        else:
            lines.append("  (no closed trades yet — reflect on process so far instead)")
        lines.append(
            "  Per AGENTS.md: did your invalidation conditions actually predict "
            "how trades failed? Did higher confidence correlate with winning? "
            "Reflect on PROCESS, not just profit. You may refine judgment; you "
            "may NEVER conclude a risk limit should be loosened — see AGENTS.md's "
            "reflection boundary. Write about the PATTERN, never the alias name "
            "(e.g. 'tight stops on high-RSI entries underperform', not 'NOVEMBER "
            "keeps failing') -- alias assignments change between runs, so a "
            "name-specific claim silently becomes false and gets fed into a "
            "future prompt as if it were still true."
        )
        lines.append("")
        schema = schema.replace(
            ' "no_action_reason": "..."',
            ' "no_action_reason": "...",\n'
            ' "reflection": "1-3 sentences, process not profit"'
        )

    lines.append(
        "Decide. Reply with ONLY a JSON object, no prose around it:\n"
        f"{schema}}}\n"
        "Taking no trade is a good outcome when nothing qualifies — return an "
        "empty decisions list and say why."
    )
    return "\n".join(lines)


def _call_hermes(prompt: str, on_invoked: "Callable[[], None] | None" = None) -> str:
    escaped = prompt.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    # --usage-file: real per-call token accounting (input/output/cache/
    # reasoning tokens, api_calls) written to a JSON report inside the
    # container. Found via `hermes --help`, not guessed -- the alternative
    # was estimating token counts, which nobody asked the model to report
    # and nothing upstream can verify.
    usage_path = f"/tmp/hermes_usage_{uuid.uuid4().hex}.json"
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "--workdir", "/opt/data/workspace",
         "gateway", "hermes", "-z", escaped, "-t", "", "--usage-file", usage_path],
        capture_output=True, text=True, cwd=REPO, timeout=600,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"hermes failed:\n{result.stderr[-1500:]}")

    # PHASE 0 REPAIR: fire the quota-counting callback HERE -- the instant
    # we know the model was actually invoked and returned (returncode 0),
    # not after ask_hermes's JSON parsing or anything downstream succeeds.
    # A cycle that spent a real call and then failed to parse, journal, or
    # process further must still count against the weekly budget; the old
    # code only recorded the cycle after run_day returned successfully,
    # silently under-counting every failure that happened after this point.
    if on_invoked is not None:
        on_invoked()

    usage_result = subprocess.run(
        ["docker", "compose", "exec", "-T", "gateway", "cat", usage_path],
        capture_output=True, text=True, cwd=REPO, timeout=30, encoding="utf-8",
    )
    subprocess.run(
        ["docker", "compose", "exec", "-T", "gateway", "rm", "-f", usage_path],
        capture_output=True, cwd=REPO, timeout=30,
    )
    usage = json.loads(usage_result.stdout) if usage_result.returncode == 0 else {}
    return result.stdout, usage


def ask_hermes(prompt: str, usage_totals: dict, on_invoked: "Callable[[], None] | None" = None) -> dict:
    """One retry on malformed JSON — a real failure mode, seen on the very
    first live cycle (an unescaped quote inside the reasoning text broke the
    whole object and cost the cycle).

    Retrying with the parse error quoted back is cheaper than losing the
    cycle: a wasted cycle still costs a quota slot AND yields nothing, while
    the retry costs the same slot and usually succeeds. Capped at one — if
    it cannot produce valid JSON twice, the cycle deserves to fail loudly
    rather than silently drop a decision.

    usage_totals accumulates across both the original call and a retry (a
    retry is a second real API call and a second real cost -- both must
    count, not just the one that happened to parse).

    on_invoked: passed straight through to _call_hermes on both the
    original call and the retry -- the caller's callback must itself be
    idempotent (fire at most once per cycle) since this can invoke it
    twice; run_day's own closure guards that.
    """
    def _accumulate(usage: dict) -> None:
        for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                    "reasoning_tokens", "total_tokens", "api_calls"):
            usage_totals[key] = usage_totals.get(key, 0) + usage.get(key, 0)
        usage_totals["estimated_cost_usd"] = (
            usage_totals.get("estimated_cost_usd", 0.0) + usage.get("estimated_cost_usd", 0.0)
        )

    raw, usage = _call_hermes(prompt, on_invoked)
    _accumulate(usage)
    try:
        return json_from(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        retry = (
            f"{prompt}\n\n---\nYour previous reply was not valid JSON "
            f"({exc}). Return ONLY the JSON object. Keep every free-text "
            "field to one short line with no quote characters, apostrophes, "
            "or line breaks inside it."
        )
        raw2, usage2 = _call_hermes(retry, on_invoked)
        _accumulate(usage2)
        return json_from(raw2)


# --------------------------------------------------------------------------
# the cycle
# --------------------------------------------------------------------------

def run_day(day: date, day_index: int, portfolio: Portfolio,
            masking: RunMasking, watchlist: list[dict], limits_ctx: dict,
            pacer: QuotaPacer, run_key: str) -> tuple[Portfolio, dict]:
    """Run one simulated trading day. Returns (new_portfolio, summary).

    PHASE 0 REPAIR -- two structural changes from before:

    1. NO SAME-DAY LOOKAHEAD. Hermes still reasons on TODAY's completed
       data, but any BUY/SELL it decides is QUEUED, never executed today.
       Step B below resolves YESTERDAY's queued orders against TODAY's
       real open before anything else happens. See QueuedOrder /
       resolve_queued_order.

    2. WORKING COPY. Every mutation in this function happens on `working`,
       a deep copy of the portfolio passed in -- the caller's `portfolio`
       object is never touched. Only on full success (every journal write
       below completes) does the caller adopt `working` as the new
       portfolio. If anything raises partway through, `working` is simply
       discarded and the caller's object is exactly as it was -- "never
       publish partially mutated portfolio state."
    """
    working = copy.deepcopy(portfolio)
    tickers = [t["symbol"] for t in watchlist]
    summary = {"day": str(day), "exits": [], "decisions": [], "blocked": [], "usage": {}}
    day_label = masking.day_index(day)
    activity: list[tuple[str, str, str | None]] = [
        ("cycle_start", f"{day_label} ({day}) — reconciling and fetching data", None)
    ]

    bars = fetch_bars(tickers, day)
    if not bars:
        summary["skipped"] = "no market data (holiday/weekend)"
        return portfolio, summary  # nothing happened -- caller's object, unchanged
    # This IS a trading day (real bars exist) -- count it before anything
    # below reads it, so a position opened today is at trading-day-offset 0,
    # not -1.
    working.trading_days_elapsed += 1

    # -- STEP A: mechanical exits first -- a resting stop fires without ---
    # judgment, on TODAY's bar, for positions opened on a PRIOR day. This is
    # not lookahead: a real stop resting at a broker triggers off whatever
    # the market does after it was placed, which is exactly what this
    # simulates. Must run BEFORE step B so a queued discretionary SELL on a
    # ticker that gapped through its own stop this morning is correctly
    # seen as already-closed, not double-handled.
    working.stopped_out_today = []
    for ticker, holding in list(working.holdings.items()):
        bar = bars.get(ticker)
        if not bar:
            continue
        exit_price = reason = None
        if bar["low"] <= holding.stop_price:
            # RULES.md §2b's whole concentration-cap derivation rests on
            # "a stop can be jumped -- the day opens below it and fills
            # near the open, not near the stop." Always filling at the
            # exact stop price made that scenario structurally impossible
            # in every backtest cycle, silently flattering results. A real
            # resting stop fills at the worse of the two.
            exit_price = min(bar["open"], holding.stop_price)
            reason = "stop hit"
            working.stopped_out_today.append(ticker)
        elif bar["high"] >= holding.target_price:
            exit_price, reason = holding.target_price, "target hit"
        elif working.trading_days_elapsed - holding.entry_trading_day >= TIME_STOP_TRADING_DAYS:
            exit_price, reason = bar["close"], f"time stop ({TIME_STOP_TRADING_DAYS} trading days)"
        if exit_price:
            proceeds = holding.quantity * exit_price
            working.cash += proceeds
            working.closed_pnl.append(
                proceeds - holding.quantity * holding.entry_price
            )
            del working.holdings[ticker]
            line = (
                f"{ticker} {reason} @ ${exit_price:.2f} "
                f"(P&L ${working.closed_pnl[-1]:+,.2f})"
            )
            summary["exits"].append(line)
            exit_id = record_exit(
                ticker, holding.quantity, exit_price, reason,
                decision_id=make_decision_id(run_key, day_index, ticker, "SELL", "mechanical_exit"),
            )
            activity.append(("exit", line, exit_id))

    # -- STEP B: resolve YESTERDAY's queued discretionary decisions against --
    # TODAY's real open. This is the actual no-lookahead guarantee: the
    # order was decided on completed data from the PRIOR day, and only now,
    # against a price that didn't exist when Hermes reasoned, does it fill.
    for order in working.pending_orders:
        bar = bars.get(order.ticker)
        if bar is None:
            line = f"{order.ticker}: no bar data at scheduled fill -- queued order dropped"
            summary["blocked"].append(line)
            activity.append(("order_rejected", line, None))
            continue

        if order.action == "SELL" and order.ticker not in working.holdings:
            # Superseded, not a violation -- the position this SELL targeted
            # was already closed (mechanical exit above, or an earlier fill
            # this same loop). Journaling it as a "blocked short" would be
            # actively misleading.
            line = f"{order.ticker}: queued SELL superseded -- position already closed"
            activity.append(("order_queued", line, None))
            continue
        if order.action == "BUY" and order.ticker in working.holdings:
            line = f"{order.ticker}: queued BUY superseded -- already holding"
            activity.append(("order_queued", line, None))
            continue

        ctx = Context(
            account=AccountState(
                equity=working.equity(bars), cash=working.cash,
                daily_pl_pct=working.pct_since(1), weekly_pl_pct=working.pct_since(5),
                drawdown_from_peak_pct=working.drawdown_pct(),
            ),
            clock=MarketClock(is_open=True, minutes_since_open=120, minutes_until_close=120),
            positions=[
                Position(ticker=t, quantity=h.quantity,
                         market_value=h.quantity * bars.get(t, {}).get("close", h.entry_price))
                for t, h in working.holdings.items()
            ],
            watchlist=tickers,
            recent_losses=[p < 0 for p in reversed(working.closed_pnl)],
            stopped_out_today=working.stopped_out_today,
            **limits_ctx,
        )
        outcome = resolve_queued_order(order, bar, ctx)
        if not outcome.accepted:
            line = f"{order.ticker}: gap-invalidated at open -- {outcome.reason}"
            summary["blocked"].append(line)
            activity.append(("order_rejected", line, None))
            continue

        fill = simulate_fill(order.alias, order.action, order.quantity, bar["open"], bar)
        if fill["status"] != "filled":
            line = f"{order.ticker}: sim {fill['rejection_reason']}"
            summary["blocked"].append(line)
            activity.append(("order_rejected", line, None))
            continue

        if order.action == "BUY":
            cost = fill["fill_price"] * fill["fill_quantity"]
            working.cash -= cost
            working.holdings[order.ticker] = Holding(
                ticker=order.ticker, quantity=fill["fill_quantity"], entry_price=fill["fill_price"],
                stop_price=order.stop_price, target_price=order.target_price,
                entry_day=day_index, entry_trading_day=working.trading_days_elapsed,
            )
            fill_id = make_decision_id(run_key, day_index, order.ticker, "BUY", "queued_fill")
            decision_id = record_trade(order.decision, order.ticker, fill, decision_id=fill_id)
            line = (
                f"BUY {order.ticker} ({order.alias}) {fill['fill_quantity']} @ ${fill['fill_price']:.2f} "
                f"(queued day {order.queued_day_index + 1}, filled at open) [{decision_id[:8]}]"
            )
            summary["decisions"].append(line)
            activity.append(("order_placed", line, decision_id))

            if outcome.same_day_exit_reason:
                # Entry happened at the very start of today -- the REST of
                # today's real range can still resolve the fresh stop/target
                # before the day ends. See resolve_queued_order's docstring
                # for the conservative (stop-over-target) OHLC-ambiguity
                # resolution already baked into `outcome`.
                exit_price = outcome.same_day_exit_price
                proceeds = fill["fill_quantity"] * exit_price
                working.cash += proceeds
                working.closed_pnl.append(proceeds - fill["fill_quantity"] * fill["fill_price"])
                del working.holdings[order.ticker]
                if outcome.same_day_exit_reason == "stop hit":
                    working.stopped_out_today.append(order.ticker)
                exit_id = make_decision_id(run_key, day_index, order.ticker, "SELL", "same_day_exit")
                exit_decision_id = record_exit(
                    order.ticker, fill["fill_quantity"], exit_price,
                    f"same-day {outcome.same_day_exit_reason} after open fill", decision_id=exit_id,
                )
                line2 = (
                    f"{order.ticker} {outcome.same_day_exit_reason} @ ${exit_price:.2f} same day as "
                    f"entry (P&L ${working.closed_pnl[-1]:+,.2f})"
                )
                summary["exits"].append(line2)
                activity.append(("exit", line2, exit_decision_id))
        else:  # SELL
            holding = working.holdings[order.ticker]
            proceeds = fill["fill_price"] * fill["fill_quantity"]
            working.cash += proceeds
            working.closed_pnl.append(proceeds - fill["fill_quantity"] * holding.entry_price)
            if fill["fill_quantity"] >= holding.quantity:
                del working.holdings[order.ticker]
            else:
                holding.quantity -= fill["fill_quantity"]
            fill_id = make_decision_id(run_key, day_index, order.ticker, "SELL", "queued_fill")
            decision_id = record_trade(order.decision, order.ticker, fill, decision_id=fill_id)
            line = (
                f"SELL {order.ticker} ({order.alias}) {fill['fill_quantity']} @ ${fill['fill_price']:.2f} "
                f"(queued day {order.queued_day_index + 1}, filled at open, P&L "
                f"${working.closed_pnl[-1]:+,.2f}) [{decision_id[:8]}]"
            )
            summary["decisions"].append(line)
            activity.append(("exit", line, decision_id))

    # Every order queued yesterday has now been resolved one way or
    # another (filled, rejected, dropped, or superseded) -- never carried
    # forward indefinitely.
    working.pending_orders = []

    equity = working.equity(bars)
    working.equity_curve.append(equity)
    record_snapshot(
        day_label=masking.day_index(day), equity=equity, cash=working.cash,
        exposure_pct=working.invested(bars) / equity * 100 if equity else 0.0,
        drawdown_pct=working.drawdown_pct(),
        open_positions=len(working.holdings),
    )

    # -- STEP C: gather + mask today's data for Hermes to reason on -------
    technicals = fetch_technicals(
        [t for t in tickers if t not in working.holdings], day
    )
    news = fetch_news(list(technicals), day)

    candidates = []
    for ticker, tech in technicals.items():
        entry = next(w for w in watchlist if w["symbol"] == ticker)
        candidates.append({
            "alias": masking.alias(ticker),
            "sector": entry["sector"],
            "tech": masking.mask_technicals({**tech, "ticker": ticker}, day),
            "news": masking.mask_news(news.get(ticker, []), day),
        })

    positions_view = [{
        "alias": masking.alias(t),
        "quantity": h.quantity, "entry_price": h.entry_price,
        "stop_price": h.stop_price, "target_price": h.target_price,
        "current_price": bars.get(t, {}).get("close", h.entry_price),
        "days_held": day_index - h.entry_day,
    } for t, h in working.holdings.items()]

    account_view = {
        "equity": equity, "cash": working.cash,
        "exposure_pct": working.invested(bars) / equity * 100 if equity else 0.0,
        "daily_pl_pct": working.pct_since(1),
        "weekly_pl_pct": working.pct_since(5),
        "drawdown_pct": working.drawdown_pct(),
    }

    # Nothing leaves for Hermes without passing the leak check.
    masking.assert_clean(candidates)
    masking.assert_clean(positions_view)

    activity.append((
        "reasoning",
        f"Evaluating {len(candidates)} candidates: "
        f"{', '.join(c['alias'] for c in candidates) or 'none available'}",
        None,
    ))

    # -- reflection: feed-back half is unconditional, trigger half is not --
    # Recent insights go into EVERY prompt (this is what makes past
    # reflection actually change future behaviour, not just sit in a log).
    # Reflection material (masked closed-trade review) is only fetched
    # every 5th cycle -- see cycles_since_reflection's docstring on why this
    # is a persisted counter, not a local one.
    recent_insights = fetch_recent_insights()
    is_reflection_day = working.cycles_since_reflection >= 4
    reflection_trades = (
        fetch_recent_closed_trades(masking) if is_reflection_day else None
    )

    prompt = build_prompt(masking.day_index(day), account_view,
                          positions_view, candidates, recent_insights,
                          reflection_trades)

    # PHASE 0 REPAIR: count the quota slot the instant Hermes is actually
    # invoked (inside _call_hermes), not after this whole cycle finishes.
    # `invoked` guards against double-counting a JSON-parse retry (2 real
    # _call_hermes calls, still 1 cycle's quota) -- see ask_hermes.
    invoked = {"done": False}

    def mark_invoked() -> None:
        if not invoked["done"]:
            pacer.record_cycle()
            invoked["done"] = True

    response = ask_hermes(prompt, summary["usage"], on_invoked=mark_invoked)
    summary["no_action_reason"] = response.get("no_action_reason", "")
    if summary["no_action_reason"]:
        activity.append(("reasoning", summary["no_action_reason"][:900], None))

    if is_reflection_day:
        reflection_text = response.get("reflection", "").strip()
        if reflection_text:
            # reflection_trades IS the closed-trade set this reflection was
            # actually drawn from -- its length is the real sample size,
            # not a guess.
            record_insight(reflection_text, sample_size=len(reflection_trades or []))
            activity.append(("weekly_reflection", reflection_text[:900], None))
        working.cycles_since_reflection = 0
    else:
        working.cycles_since_reflection += 1

    # -- STEP D: QUEUE today's decisions -- never execute them today ------
    # This is the other half of the no-lookahead guarantee: whatever
    # Hermes decided from today's completed data waits for tomorrow's real
    # open (step B, next invocation). Only light structural validation
    # happens here (alias resolves, action/quantity/stop/target are
    # present and sane) -- the real risk-checker validation is deliberately
    # deferred to fill time against the REAL price, not duplicated here.
    for decision in response.get("decisions", []):
        try:
            real_ticker = masking.real(decision["alias"])
        except KeyError:
            summary["blocked"].append(f"unknown alias {decision.get('alias')!r}")
            continue

        action = decision.get("action", "").upper()
        if action not in ("BUY", "SELL"):
            summary["blocked"].append(f"{real_ticker}: unsupported action {action!r}")
            continue
        try:
            quantity = int(decision["quantity"])
        except (KeyError, TypeError, ValueError):
            summary["blocked"].append(f"{real_ticker}: missing/invalid quantity")
            continue
        if quantity <= 0:
            summary["blocked"].append(f"{real_ticker}: quantity must be positive")
            continue
        stop_price = decision.get("stop_price")
        target_price = decision.get("target_price")
        if action == "BUY" and (stop_price is None or target_price is None):
            summary["blocked"].append(f"{real_ticker}: BUY missing stop/target -- cannot queue")
            continue

        order = QueuedOrder(
            ticker=real_ticker, alias=decision["alias"], action=action, quantity=quantity,
            stop_price=float(stop_price) if stop_price is not None else None,
            target_price=float(target_price) if target_price is not None else None,
            entry_price=float(decision.get("entry_price", 0.0)),  # audit only, never used to fill
            queued_day_index=day_index, decision=decision,
        )
        working.pending_orders.append(order)
        line = f"{action} {real_ticker} ({decision['alias']}) x{quantity} queued for next trading day's open"
        summary["decisions"].append(line)
        activity.append(("order_queued", line, None))

    log_activity(activity)
    return working, summary


def _run_cycle_or_halt(run_day_fn: "Callable[[], tuple[Portfolio, dict]]"):
    """Isolated control-flow wrapper -- the one piece of "a failed cycle
    must not advance the simulated date" that's actually unit-testable
    without Docker/Neon. Runs one cycle; on any exception, prints why and
    returns (None, True) -- halted. The caller must not advance its cursor
    or write state when halted is True. Deliberately does NOT retry or
    advance anything itself -- "remove catch-and-continue behavior that
    silently skips failed trading days" means the caller decides to stop,
    not paper over the failure and move to the next day.
    """
    try:
        return run_day_fn(), False
    except Exception as exc:
        print(f"  CYCLE FAILED — simulated date NOT advanced, state NOT written: "
              f"{type(exc).__name__}: {exc}")
        print("  Re-run the harness to retry this same day; any trade already "
              "journaled before the failure is safe to retry (idempotent).")
        return None, True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-02-17")
    parser.add_argument("--end", default="2026-08-14")
    parser.add_argument("--days", type=int, default=3,
                        help="max cycles this run (smoke tests use a few)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    preflight()

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()

    watchlist = json.loads(
        (REPO / "watchlist" / "watchlist.json").read_text()
    )["tickers"]
    start = date.fromisoformat(args.start)

    names = {t["symbol"]: _name_variants(t["name"], t["symbol"]) for t in watchlist}
    masking = RunMasking(names, run_start=start, seed=args.seed)
    pacer = QuotaPacer()
    # Stable per (start, seed) -- feeds make_decision_id so a retry of the
    # SAME logical run regenerates the SAME ids for the SAME real events.
    # A genuinely different run (different window or seed) gets a
    # different key, so it can never collide with an unrelated run's ids.
    run_key = f"{args.start}|{args.seed}"

    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        portfolio = Portfolio.from_dict(state["portfolio"])
        cursor = date.fromisoformat(state["cursor"])
        print(f"resuming from {cursor}")
    else:
        portfolio, cursor = Portfolio(), start

    end = date.fromisoformat(args.end)
    completed = 0
    run_usage = {"total_tokens": 0, "api_calls": 0, "estimated_cost_usd": 0.0}
    while completed < args.days and cursor <= end:
        if not pacer.can_run_cycle():
            print(
                f"\nQUOTA PACER: weekly budget spent "
                f"({pacer.used_this_week()}/{pacer.budget}). "
                f"Resume on {pacer.next_week_starts()}."
            )
            break
        if cursor.weekday() >= 5:
            cursor += timedelta(days=1)
            continue

        day_index = (cursor - start).days
        print(f"\n{'=' * 70}\nDay {day_index + 1} — {cursor}\n{'=' * 70}")

        # PHASE 0 REPAIR: a failed cycle halts this run instead of silently
        # skipping to the next day. The simulated date is NOT advanced and
        # state is NOT written -- re-running the harness later retries this
        # exact day, safely (deterministic decision_ids make an already-
        # journaled trade a no-op on retry; quota was already correctly
        # counted at invocation time regardless of the failure -- see
        # run_day's mark_invoked).
        result, halted = _run_cycle_or_halt(
            lambda: run_day(cursor, day_index, portfolio, masking, watchlist, {}, pacer, run_key)
        )
        if halted:
            break
        portfolio, summary = result

        if "skipped" in summary:
            print(f"  skipped: {summary['skipped']}")
            cursor += timedelta(days=1)
            continue

        completed += 1
        for line in summary["exits"]:
            print(f"  EXIT    {line}")
        for line in summary["decisions"]:
            print(f"  TRADE   {line}")
        for line in summary["blocked"]:
            print(f"  BLOCKED {line}")
        if not summary["decisions"] and not summary["blocked"]:
            print(f"  no trade — {summary['no_action_reason'][:160]}")

        u = summary["usage"]
        if u:
            for k in ("total_tokens", "api_calls", "estimated_cost_usd"):
                run_usage[k] += u.get(k, 0)
            print(f"  usage   {u.get('total_tokens', 0):,} tokens "
                  f"({u.get('api_calls', 0)} call{'s' if u.get('api_calls') != 1 else ''}, "
                  f"{u.get('reasoning_tokens', 0)} reasoning)")

        _atomic_write_json(
            STATE_PATH,
            {"portfolio": portfolio.to_dict(), "cursor": str(cursor + timedelta(days=1))},
        )
        cursor += timedelta(days=1)

    final = portfolio.equity_curve[-1]
    print(f"\n{'=' * 70}")
    print(f"cycles this run: {completed} | quota used this week: "
          f"{pacer.used_this_week()}/{pacer.budget}")
    print(f"equity ${final:,.2f} ({(final / STARTING_EQUITY - 1) * 100:+.2f}%) "
          f"| open {len(portfolio.holdings)} | closed {len(portfolio.closed_pnl)}")
    if completed:
        avg = run_usage["total_tokens"] / completed
        cost = (
            "included in subscription" if run_usage["estimated_cost_usd"] == 0
            else f"${run_usage['estimated_cost_usd']:.4f}"
        )
        print(f"tokens this run: {run_usage['total_tokens']:,} total "
              f"({avg:,.0f} avg/cycle) | api calls: {run_usage['api_calls']} "
              f"| cost: {cost}")


if __name__ == "__main__":
    main()
