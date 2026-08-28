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
never filled that same day. Also from that repair: run_day operates on a
deep-copied working portfolio and only the caller adopts it on success
(never publish a partially mutated portfolio); a failed cycle halts the run
rather than silently skipping the day (see _run_cycle_or_halt); and every
journal write carries a deterministic decision_id (see make_decision_id) so
retrying a failed day cannot duplicate an already-journaled trade.

2026-08-25 FOLLOW-UP -- REAL INTRADAY CHRONOLOGY: the day-T+1 fill above
used to happen at the OPEN and mark existing positions at the prior day's
CLOSE -- violating RULES.md §7's 15-minute entry floor and still leaking a
day's-worth of lookahead into the risk check. Real 15-minute regular-
session bars (Alpaca market data -- see fetch_intraday_bars, full-window
coverage verified for the whole watchlist in HANDOFF.md) replace that with
an actual chronological walk: opening-gap stops/targets, then time-stops,
then a queued SELL (exempt from the entry floor) all resolve at the 09:30
open; a queued BUY resolves no earlier than 09:45, re-validated against
marks known ONLY as of 09:45 (never today's close); the rest of the
session is then walked bar-by-bar for every open position. See
simulate_trading_day for the actual pure algorithm -- run_day only
replays its plan with real I/O.

Usage:
    python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days 3
"""
import argparse
import copy
import json
import math
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

    `decision_id` (2026-08-28 pre-training pass): generated and journaled
    (status='pending') the INSTANT this order is queued -- see
    record_queued_decision -- not only once it fills. The SAME id is
    reused, never regenerated, when this order is resolved the next
    trading day (resolve_trade_fill / resolve_trade_rejection), so the
    journal has one continuous row per intended decision from proposal to
    outcome, exactly as the risk-checker's own risk-check activity should
    link back to it.
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
    decision_id: str


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
            # .get() on decision_id: old state files (pre 2026-08-28)
            # predate queue-time journaling. "" is a safe placeholder here
            # (never a real uuid5 output) -- any such leftover order was
            # never journaled at queue time under the old code either, and
            # will simply get a fresh decision_id path on its next
            # resolution rather than crash on resume.
            pending_orders=[
                QueuedOrder(**{**o, "decision_id": o.get("decision_id", "")})
                for o in d.get("pending_orders", [])
            ],
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
    reason: str = ""  # rejection reason, if not accepted


def resolve_queued_order(order: QueuedOrder, bar: dict, ctx: Context) -> QueuedOrderOutcome:
    """Decide whether a queued order fills at `bar["open"]` -- NEVER at
    `order.entry_price`, which is Hermes' stale, pre-gap guess and is not
    even read here.

    Re-runs the SAME risk checker used at decision time, now against the
    REAL price at the order's actual execution instant (a queued SELL's
    09:30 open, or a queued BUY's 09:45 open -- see simulate_trading_day).
    Rejects outright on any breach (stop distance, reward:risk, exposure,
    cash, sizing, market-hours) -- never silently resizes a gapped order
    to make it fit, which would substitute the checker's judgment for
    Hermes' and hide how large the gap actually was.

    2026-08-25 follow-up: this used to ALSO decide same-day post-entry
    stop/target eligibility from one aggregate daily bar. That's now
    handled correctly (and more precisely) by walking the REAL remaining
    15-minute bars one at a time -- see evaluate_bar_stop_target and
    simulate_trading_day's final walk stage -- so this function's job
    shrank to exactly the risk-check re-validation.
    """
    open_price = bar["open"]
    trade = ProposedTrade(
        ticker=order.ticker, action=order.action, quantity=order.quantity,
        entry_price=open_price, stop_price=order.stop_price, target_price=order.target_price,
    )
    verdict = check_trade(trade, ctx)
    if not verdict.allowed:
        return QueuedOrderOutcome(accepted=False, reason="; ".join(verdict.violations))
    return QueuedOrderOutcome(accepted=True)


# --------------------------------------------------------------------------
# intraday chronological simulation -- pure, no I/O (2026-08-25 follow-up)
#
# Completes the no-lookahead repair: a single daily OHLC bar let a BUY fill
# at the open (violating RULES.md §7's 15-minute floor), marked existing
# positions at the day's CLOSE inside the entry risk check, and processed
# the WHOLE day's stop/target movement before a queued SELL could fill
# (letting an afternoon stop retroactively supersede a morning exit). Real
# 15-minute regular-session bars (Alpaca market data, full-window coverage
# verified for the whole watchlist -- see HANDOFF.md) fix all three by
# actually walking the session in chronological order.
# --------------------------------------------------------------------------

def evaluate_opening_gap(stop_price: float, target_price: float, open_price: float) -> tuple[str, float] | None:
    """Did the session's 09:30 open already blow through a resting stop or
    target, before any further intraday movement is considered? Stop
    checked first -- protecting capital outranks a target, matching the
    existing mechanical-exit priority elsewhere in this file. A stop gap
    fills at the worse of open/stop (RULES.md §2b's whole concentration-
    cap derivation rests on exactly this: "the day opens below it and
    fills near the open, not near the stop"). A target gap-up fills at the
    worse-for-the-strategy of open/target for the same reason, applied
    symmetrically -- never assume the best case in either direction.
    Returns ("stop"|"target", exit_price) or None."""
    if open_price <= stop_price:
        return "stop", min(open_price, stop_price)
    if open_price >= target_price:
        return "target", max(open_price, target_price)
    return None


def evaluate_bar_stop_target(stop_price: float, target_price: float, bar: dict) -> tuple[str, float] | None:
    """Did THIS ONE bar's real intraday range (low/high) touch a resting
    stop or target? Stop checked first -- when a single bar's low/high
    range technically crosses BOTH (possible, if rare, at 15-minute
    granularity), OHLC alone cannot say which came first inside that bar;
    resolved conservatively (assume the stop, never the target). This is a
    real, accepted limitation of bar-level (not tick-level) simulation --
    documented in HANDOFF.md, not silently assumed away. Returns
    ("stop"|"target", exit_price) or None."""
    if bar["low"] <= stop_price:
        return "stop", min(bar["open"], stop_price)
    if bar["high"] >= target_price:
        return "target", target_price
    return None


def require_intraday_coverage(
    needed: list[str], intraday: dict[str, list[dict]],
    required_times: tuple[str, ...] = ("09:30", "09:45"),
) -> None:
    """Fail CLOSED on missing intraday data. Every ticker in `needed`
    (currently held, or with a pending order) must have real bars for
    today, including both the 09:30 and 09:45 boundaries execution timing
    depends on. Raises RuntimeError -- propagates up through run_day and
    is caught by _run_cycle_or_halt, halting the run exactly like any
    other cycle failure (date not advanced, state not written). Never
    fabricates a price and never silently skips -- a real data gap on an
    otherwise-open trading day is a stop-and-report condition, not a
    holiday (holidays are detected separately, before this is even
    called -- see run_day)."""
    for ticker in needed:
        bars = intraday.get(ticker)
        if not bars:
            raise RuntimeError(
                f"{ticker}: no intraday bars returned for this trading day -- "
                "required (held position or pending order) but missing. "
                "Fails closed: no fabricated price, no silent skip."
            )
        times = {b["time"] for b in bars}
        missing = [t for t in required_times if t not in times]
        if missing:
            raise RuntimeError(
                f"{ticker}: missing required intraday bar(s) {missing} -- "
                "fails closed rather than guessing a price."
            )


def _bar_at(intraday: dict[str, list[dict]], ticker: str, hhmm: str) -> dict:
    """The exact bar starting at ET wall-clock `hhmm` for `ticker`. Raises
    if missing -- callers rely on require_intraday_coverage having already
    checked the boundary bars, but a mid-day bar (used by the walk stage)
    gets the same fail-closed treatment if it's ever absent."""
    for b in intraday[ticker]:
        if b["time"] == hhmm:
            return b
    raise RuntimeError(f"{ticker}: no {hhmm} ET bar available -- fails closed")


@dataclass
class DayResult:
    """Everything simulate_trading_day decided, in chronological order --
    plain dicts per event (not a typed hierarchy per event kind) on
    purpose: this is a decision/ordering record for run_day to replay
    with REAL I/O, not a generalized event-sourcing engine. Each event has
    at minimum {"stage", "ticker", "kind"}; "kind" is "exit" (mechanical,
    no slippage -- run_day applies `exit_price` directly), "fill"
    (discretionary -- run_day re-derives the real, slippage-adjusted price
    via the actual execution-sim call against `bar`), "rejected", or
    "superseded" (informational only, no state change).

    Internal bookkeeping (which tickers are still held, for sequencing
    purposes) uses UNSLIPPED bar-open reference prices throughout -- see
    simulate_trading_day's docstring for why that's a small, bounded,
    documented approximation rather than a correctness gap.
    """
    events: list[dict]


def simulate_trading_day(
    holdings: dict[str, "Holding"], pending_orders: list[QueuedOrder],
    intraday: dict[str, list[dict]], day_index: int, trading_days_elapsed: int,
    limits_ctx: dict, watchlist: list[str], closed_pnl: list[float], starting_cash: float,
    daily_pl_pct: float | None, weekly_pl_pct: float | None, drawdown_from_peak_pct: float | None,
) -> DayResult:
    """The full chronological session, pure. Order matters and is exactly
    what fixes the three problems this follow-up repair targets:

      1. 09:30 open -- gap-through stops/targets first (protects capital,
         and a stop/target that already fired here correctly supersedes a
         same-ticker queued SELL considered next), then time-stops
         (decided on information already known before today -- elapsed
         trading days -- so there's nothing to wait for), then queued
         discretionary SELLs (exempt from §7's entry-timing floor --
         exits are always allowed -- filled at the open if the position
         is still open after the two prior checks).
      2. 09:30-09:45 -- the FIRST bar's real range, for positions still
         open after the opening instant.
      3. 09:45 -- queued BUY entries, the ONLY entry execution point,
         re-validated by the risk checker using ONLY marks known by 09:45
         (the 09:30 bar's close for anything still open, never today's
         close or a later bar) and the REAL clock (minutes_since_open=15,
         not a placeholder).
      4. 09:45-through-close -- every remaining bar, in order, for every
         currently open position (carried-over AND anything that just
         entered at 09:45 -- its own entry bar counts, since entry
         happened at that bar's start; earlier bars that day do not,
         since the position didn't exist yet).

    Approximation, stated plainly: this function's OWN internal cash
    tracking (used only to decide the 09:45 risk-check equity figure) uses
    UNSLIPPED bar-open reference prices for every leg, including a same-
    day SELL that precedes a BUY. Real execution-sim slippage (5bps) is
    applied by run_day only to the ACTUAL recorded fill/cash movement, not
    fed back into this function's own sequencing math. Bounded to a few
    dollars on a same-day SELL-then-BUY sequence -- documented in
    HANDOFF.md, not silently assumed away.
    """
    # Running cash balance, starting from the REAL portfolio cash --
    # updated with UNSLIPPED reference prices for every leg during this
    # function's own internal sequencing (see the approximation note
    # above); the actual, slippage-adjusted cash movement is applied by
    # run_day when it replays each event with real execution-sim fills.
    running_cash = starting_cash
    remaining: dict[str, "Holding"] = dict(holdings)
    stopped_out_today: list[str] = []
    events: list[dict] = []

    def equity_mark(prices: dict[str, float], cash_now: float) -> float:
        return cash_now + sum(
            h.quantity * prices.get(t, h.entry_price) for t, h in remaining.items()
        )

    # -- 09:30: opening gaps, then time-stops -----------------------------
    for ticker, holding in list(remaining.items()):
        open_bar = _bar_at(intraday, ticker, "09:30")
        gap = evaluate_opening_gap(holding.stop_price, holding.target_price, open_bar["open"])
        if gap:
            kind, exit_price = gap
            reason = f"{kind} hit (opening gap)"
            if kind == "stop":
                stopped_out_today.append(ticker)
            running_cash += holding.quantity * exit_price
            del remaining[ticker]
            events.append({"stage": "opening_gap", "ticker": ticker, "kind": "exit",
                           "reason": reason, "exit_price": exit_price, "quantity": holding.quantity,
                           "entry_price": holding.entry_price, "bar_time": "09:30"})
            continue
        if trading_days_elapsed - holding.entry_trading_day >= TIME_STOP_TRADING_DAYS:
            exit_price = open_bar["open"]
            running_cash += holding.quantity * exit_price
            del remaining[ticker]
            events.append({"stage": "time_stop", "ticker": ticker, "kind": "exit",
                           "reason": f"time stop ({TIME_STOP_TRADING_DAYS} trading days)",
                           "exit_price": exit_price, "quantity": holding.quantity,
                           "entry_price": holding.entry_price, "bar_time": "09:30"})

    # -- 09:30: queued discretionary SELLs (exempt from the entry floor) --
    for order in [o for o in pending_orders if o.action == "SELL"]:
        if order.ticker not in remaining:
            events.append({"stage": "queued_sell", "ticker": order.ticker, "kind": "superseded",
                           "reason": "position already closed at the open", "order": order})
            continue
        holding = remaining[order.ticker]
        open_bar = _bar_at(intraday, order.ticker, "09:30")
        ctx = Context(
            account=AccountState(
                equity=equity_mark({order.ticker: open_bar["open"]}, running_cash), cash=running_cash,
                daily_pl_pct=daily_pl_pct, weekly_pl_pct=weekly_pl_pct,
                drawdown_from_peak_pct=drawdown_from_peak_pct,
            ),
            clock=MarketClock(is_open=True, minutes_since_open=0, minutes_until_close=390),
            positions=[Position(ticker=order.ticker, quantity=holding.quantity,
                                market_value=holding.quantity * open_bar["open"])],
            watchlist=watchlist, recent_losses=[p < 0 for p in reversed(closed_pnl)],
            stopped_out_today=stopped_out_today, **limits_ctx,
        )
        outcome = resolve_queued_order(order, open_bar, ctx)
        if not outcome.accepted:
            events.append({"stage": "queued_sell", "ticker": order.ticker, "kind": "rejected",
                           "reason": outcome.reason, "order": order})
            continue
        # One-lot model (RULES.md §9.1, 2026-08-28 pre-training pass, item
        # 7): a discretionary SELL must close the WHOLE position. The risk
        # checker (resolve_queued_order, just above) rejects a partial
        # SELL outright -- both here at execution time AND at decision
        # time before it was ever queued (run_day's decision-time risk
        # check) -- so by the time a queued SELL reaches this point it is
        # structurally guaranteed to be a full exit. Asserted, not
        # silently handled with a partial-credit branch: a real regression
        # in either check should surface immediately as a loud failure,
        # not quietly resume crediting a partial fill. An involuntary
        # BROKER partial fill (fewer shares filled than requested by
        # Alpaca) is a separate, unmodelled LIVE-mode limitation -- see
        # HANDOFF.md -- this assertion says nothing about that case.
        assert order.quantity == holding.quantity, (
            f"{order.ticker}: queued SELL quantity {order.quantity} != held "
            f"{holding.quantity} -- the risk checker should have rejected "
            "this partial SELL before it ever reached execution"
        )
        running_cash += order.quantity * open_bar["open"]  # unslipped reference only
        del remaining[order.ticker]
        events.append({"stage": "queued_sell", "ticker": order.ticker, "kind": "fill",
                       "order": order, "bar": open_bar, "bar_time": "09:30"})

    # -- 09:30-09:45: the first bar's real range for still-open positions -
    for ticker, holding in list(remaining.items()):
        first_bar = _bar_at(intraday, ticker, "09:30")
        touch = evaluate_bar_stop_target(holding.stop_price, holding.target_price, first_bar)
        if touch:
            kind, exit_price = touch
            if kind == "stop":
                stopped_out_today.append(ticker)
            running_cash += holding.quantity * exit_price
            del remaining[ticker]
            events.append({"stage": "intraday_930_945", "ticker": ticker, "kind": "exit",
                           "reason": f"{kind} hit", "exit_price": exit_price, "quantity": holding.quantity,
                           "entry_price": holding.entry_price, "bar_time": "09:30"})

    # -- 09:45: queued BUY entries -- the only entry execution point ------
    for order in [o for o in pending_orders if o.action == "BUY"]:
        if order.ticker in remaining:
            events.append({"stage": "buy_945", "ticker": order.ticker, "kind": "superseded",
                           "reason": "already holding", "order": order})
            continue
        bar_945 = _bar_at(intraday, order.ticker, "09:45")
        # Marks known by 09:45: the 09:30 bar's close (fully elapsed by
        # now) for anything still open -- never today's daily close, never
        # a bar later than this one.
        marks = {t: _bar_at(intraday, t, "09:30")["close"] for t in remaining}
        equity_at_945 = equity_mark(marks, running_cash)
        ctx = Context(
            account=AccountState(
                equity=equity_at_945, cash=running_cash,
                daily_pl_pct=daily_pl_pct, weekly_pl_pct=weekly_pl_pct,
                drawdown_from_peak_pct=drawdown_from_peak_pct,
            ),
            # REAL values for the fixed 09:45 execution point -- 15 minutes
            # elapsed (RULES.md §7's "< 15" floor is satisfied exactly at
            # this boundary, not assumed), 375 minutes to the 16:00 close.
            clock=MarketClock(is_open=True, minutes_since_open=15, minutes_until_close=375),
            positions=[
                Position(ticker=t, quantity=h.quantity, market_value=h.quantity * marks.get(t, h.entry_price))
                for t, h in remaining.items()
            ],
            watchlist=watchlist, recent_losses=[p < 0 for p in reversed(closed_pnl)],
            stopped_out_today=stopped_out_today, **limits_ctx,
        )
        outcome = resolve_queued_order(order, bar_945, ctx)
        if not outcome.accepted:
            events.append({"stage": "buy_945", "ticker": order.ticker, "kind": "rejected",
                           "reason": outcome.reason, "order": order})
            continue
        running_cash -= order.quantity * bar_945["open"]  # unslipped reference only
        remaining[order.ticker] = Holding(
            ticker=order.ticker, quantity=order.quantity, entry_price=bar_945["open"],
            stop_price=order.stop_price, target_price=order.target_price,
            entry_day=day_index, entry_trading_day=trading_days_elapsed,
        )
        events.append({"stage": "buy_945", "ticker": order.ticker, "kind": "fill",
                       "order": order, "bar": bar_945, "bar_time": "09:45"})

    # -- 09:45 through the close: every remaining bar, chronologically, ---
    # for every currently open position. Bars strictly before "09:45" are
    # never considered here -- either already handled above (carried-over
    # positions), or the ticker didn't hold a position yet (a fresh 09:45
    # entry) -- "entry-day stops/targets only consider bars after entry."
    for ticker in list(remaining):
        for bar in intraday[ticker]:
            if bar["time"] < "09:45" or ticker not in remaining:
                continue
            holding = remaining[ticker]
            touch = evaluate_bar_stop_target(holding.stop_price, holding.target_price, bar)
            if touch:
                kind, exit_price = touch
                if kind == "stop":
                    stopped_out_today.append(ticker)
                running_cash += holding.quantity * exit_price
                del remaining[ticker]
                events.append({"stage": "intraday_walk", "ticker": ticker, "kind": "exit",
                               "reason": f"{kind} hit", "exit_price": exit_price, "quantity": holding.quantity,
                               "entry_price": holding.entry_price, "bar_time": bar["time"]})

    return DayResult(events=events)


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
    """That day's real daily OHLC — used ONLY for the cheap holiday/
    no-trading-day check and informational display (positions_view's
    current_price, shown to Hermes) since the 2026-08-25 intraday
    chronology follow-up. Mechanical exits, queued-order execution, and
    every risk-check mark now come from fetch_intraday_bars instead --
    see simulate_trading_day. Kept for exactly those two remaining uses
    (and test_execution_path.py's own direct coverage of this maverick/
    yfinance path) rather than removed, to keep this change's diff scoped
    to what actually needed fixing."""
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


def fetch_intraday_bars(tickers: list[str], day: date) -> dict[str, list[dict]]:
    """Real 15-minute regular-session bars for each ticker on `day`, from
    Alpaca's market-data API (full-window coverage verified for the whole
    watchlist -- see HANDOFF.md). This is what makes RULES.md §7's 9:45 ET
    entry floor and chronological intraday stop/target resolution real
    instead of approximated from one daily bar -- see
    simulate_trading_day. Only called for tickers actually needed
    (currently held, or with a pending order) -- not the whole watchlist
    every cycle."""
    script = f'''
import asyncio, json
from server import get_intraday_bars

async def main():
    out = {{}}
    for t in {tickers!r}:
        out[t] = await get_intraday_bars(t, "{day}")
    print(json.dumps({{"bars": out}}))

asyncio.run(main())
'''
    return json_from(run_in("alpaca-mcp", script))["bars"]


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


def record_trade(decision: dict, real_ticker: str, fill: dict, decision_id: str,
                 run_id: str | None = None) -> str:
    """Journal the REAL ticker, never the alias — the mask exists to keep
    Hermes honest, not to obscure our own records.

    decision_id is now REQUIRED, deterministic (see make_decision_id), and
    supplied by the caller rather than generated server-side -- Phase 0
    repair, so retrying a failed day cannot double-journal this trade.

    Journals a decision AND its immediately-known outcome in one call --
    correct for anything that decides and resolves in the same instant
    (kept for test_execution_path.py's own direct coverage of that shape).
    The QUEUED discretionary flow (decide today, resolve tomorrow) uses
    the split record_queued_decision / resolve_trade_fill /
    resolve_trade_rejection instead -- see run_day -- since a full day can
    separate the two halves and each must be independently retryable.
    """
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
        stop_price={decision.get("stop_price")!r},
        target_price={decision.get("target_price")!r},
        run_id={run_id!r},
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


def record_queued_decision(decision: dict, real_ticker: str, decision_id: str,
                           run_id: str | None = None) -> None:
    """Journal a QUEUED discretionary decision the INSTANT it is queued --
    status stays 'pending' (the trades table's own default) until the next
    trading day resolves it (resolve_trade_fill / resolve_trade_rejection).

    2026-08-28 pre-training pass: previously nothing was journaled for a
    queued order until (and unless) it filled the next day -- a decision
    that got REJECTED at resolution left no trades row at all, only a free-
    text activity line, and the fill path re-derived the whole decision
    from scratch with a DIFFERENT decision_id (kind="queued_fill") than
    anything a decision-time risk-check activity line could have linked
    to. Journaling here, with the id the caller already generated at
    decision time, is what makes that link real."""
    script = f'''
import asyncio
from server import _record_decision

async def main():
    await _record_decision(
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
        stop_price={decision.get("stop_price")!r},
        target_price={decision.get("target_price")!r},
        run_id={run_id!r},
    )

asyncio.run(main())
'''
    run_in("trade-journal", script)


def resolve_trade_fill(decision_id: str, fill: dict) -> None:
    """Resolve an already-journaled queued decision to its real fill --
    the SAME decision_id record_queued_decision used, never a new one.
    Idempotent the same way record_execution_result always has been: only
    resolves a row still 'pending'."""
    script = f'''
import asyncio
from server import _record_execution_result

async def main():
    await _record_execution_result(
        {decision_id!r}, status={fill["status"]!r},
        broker_order_id={fill.get("broker_order_id")!r},
        fill_price={fill.get("fill_price")!r},
        fill_quantity={fill.get("fill_quantity")!r},
        execution_mode="backtest",
    )

asyncio.run(main())
'''
    run_in("trade-journal", script)


def resolve_trade_rejection(decision_id: str, reason: str) -> None:
    """Resolve an already-journaled queued decision to a REJECTION --
    never fabricates fill_price/fill_quantity (both stay NULL, the
    correct honest state for something that never actually filled)."""
    script = f'''
import asyncio
from server import _record_execution_result

async def main():
    await _record_execution_result(
        {decision_id!r}, status="rejected",
        rejection_reason={reason[:900]!r},
        execution_mode="backtest",
    )

asyncio.run(main())
'''
    run_in("trade-journal", script)


def record_exit(real_ticker: str, quantity: int, exit_price: float, reason: str, decision_id: str,
                run_id: str | None = None) -> str:
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
        run_id={run_id!r},
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


def log_activity(events: list[tuple[str, str, str | None]],
                 run_id: str | None = None, cycle_id: str | None = None) -> None:
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

    run_id/cycle_id tag every row in the batch -- see record_snapshot_once's
    docstring for why THIS function is only ever called once the cycle's
    snapshot write is confirmed genuinely new (never on a retry of an
    already-fully-recorded day).
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
        await _log_activity(event_type, description, decision_id, mode="backtest",
                            run_id={run_id!r}, cycle_id={cycle_id!r})

asyncio.run(main())
'''
    run_in("trade-journal", script)


def log_cycle_failure(day_label: str, error_text: str, run_id: str | None,
                      usage: dict | None, pacer: "QuotaPacer | None") -> None:
    """Best-effort dashboard visibility for a FAILED cycle attempt --
    called from _run_cycle_or_halt, separately from log_activity's batched
    success-path narration (never gated behind the snapshot-conflict check
    that protects successful-cycle rows from duplication, since a failed
    attempt is exactly what should remain visible on every attempt, not be
    deduplicated away).

    usage/pacer, when available (a real _call_hermes may have already run
    before whatever failed afterward -- JSON parse, semantic validation,
    a journal write), are real numbers, not estimates: this cycle's actual
    token spend and the pacer's actual current count at the moment of
    failure, exactly the state the dashboard is required to show for a
    failed attempt."""
    usage = usage or {}
    quota_used = pacer.used_this_week() if pacer is not None else None
    script = f'''
import asyncio
from server import _log_activity

async def main():
    await _log_activity(
        "cycle_failed", {f"{day_label}: {error_text}"[:900]!r}, None, mode="backtest",
        run_id={run_id!r}, total_tokens={usage.get("total_tokens")!r},
        hermes_calls_used={quota_used!r},
    )

asyncio.run(main())
'''
    run_in("trade-journal", script)


def record_snapshot_once(day_label: str, equity: float, cash: float,
                         exposure_pct: float, drawdown_pct: float,
                         open_positions: int, run_id: str, cycle_id: str,
                         usage: dict | None = None,
                         pacer: "QuotaPacer | None" = None,
                         positions_detail: list[dict] | None = None) -> bool:
    """Write this cycle's snapshot -- AT MOST ONCE, ever, per (run_id,
    day_index) -- and report whether this call was the one that actually
    wrote it. This IS the cycle-success/idempotency mechanism (2026-08-28
    pre-training pass): `cycle_id` is UNIQUE at the database level, the
    INSERT is `ON CONFLICT (cycle_id) DO NOTHING RETURNING *`, and the
    caller (run_day) only proceeds to write this cycle's activity
    narration and reflection insight when this returns True -- a retry of
    a day that ALREADY has a snapshot (the earlier attempt fully
    succeeded, but something after it, e.g. the local state-file write,
    failed) returns False and the caller correctly skips re-narrating.

    usage/pacer: same real-usage/real-quota purpose as the prior
    record_snapshot. positions_detail: per-open-position dicts as the
    harness itself sees them this cycle."""
    usage = usage or {}
    quota_used = pacer.used_this_week() if pacer is not None else None
    quota_budget = pacer.budget if pacer is not None else None
    script = f'''
import asyncio, json
from server import _record_portfolio_snapshot

async def main():
    row = await _record_portfolio_snapshot(
        mode="backtest", equity={equity!r}, cash={cash!r},
        exposure_pct={exposure_pct!r}, drawdown_pct={drawdown_pct!r},
        day_label={day_label!r}, open_positions={open_positions},
        input_tokens={usage.get("input_tokens")!r},
        output_tokens={usage.get("output_tokens")!r},
        cache_read_tokens={usage.get("cache_read_tokens")!r},
        reasoning_tokens={usage.get("reasoning_tokens")!r},
        total_tokens={usage.get("total_tokens")!r},
        api_calls={usage.get("api_calls")!r},
        hermes_calls_used={quota_used!r},
        weekly_budget={quota_budget!r},
        open_positions_detail={positions_detail!r},
        run_id={run_id!r}, cycle_id={cycle_id!r},
    )
    print(json.dumps({{"inserted": row is not None}}))

asyncio.run(main())
'''
    return json_from(run_in("trade-journal", script))["inserted"]


def fetch_recent_insights(limit: int = 2, run_id: str | None = None) -> list[dict]:
    """The feed-back half of reflection: past insight text, injected into
    EVERY prompt (not just reflection days) so learning actually changes
    later behaviour. Writing to insight_log without this would just be
    journaling into a void -- the whole point of "self-learning" is that
    cycle N+5 reasons differently because of what cycle N produced.

    Filtered to mode='backtest' AND this run_id (2026-08-28 pre-training
    pass) -- previously unfiltered, meaning a DISCARDED prior run's
    reflections (or, once live cron exists, a live insight) could
    silently feed into this run's prompts.

    Returns dicts, not bare strings, so the caller can show sample_size
    alongside the text -- an insight drawn from 2 trades reads with the
    same authority as one drawn from 40 unless its weight is visible.
    """
    script = f'''
import asyncio, json
from server import _get_insight_history

async def main():
    rows = await _get_insight_history(
        cycle_type="weekly_reflection", limit={limit},
        mode="backtest", run_id={run_id!r},
    )
    print(json.dumps(
        [{{"text": r["insight_text"], "sample_size": r["sample_size"]}} for r in rows],
        default=str,
    ))

asyncio.run(main())
'''
    return json_from_list(run_in("trade-journal", script))


def record_insight(text: str, sample_size: int, run_id: str, cycle_id: str) -> bool:
    """Returns whether this call actually wrote the insight (see
    record_snapshot_once -- insight_log.cycle_id is UNIQUE the same way)."""
    script = f'''
import asyncio, json
from server import _log_insight

async def main():
    row = await _log_insight(
        "weekly_reflection", {text!r}, mode="backtest", sample_size={sample_size},
        run_id={run_id!r}, cycle_id={cycle_id!r},
    )
    print(json.dumps({{"inserted": row is not None}}))

asyncio.run(main())
'''
    return json_from(run_in("trade-journal", script))["inserted"]


def fetch_recent_closed_trades(masking: "RunMasking", run_id: str, limit: int = 8) -> list[dict]:
    """Masked review material for a reflection cycle -- real tickers never
    leave this function. Pairs BUY/SELL by ticker (RULES.md §9.1 guarantees
    one open lot per ticker, so simple sequential pairing is exact, not an
    approximation -- same logic as the dashboard's pairTrades, in Python
    here since this runs harness-side, not in a browser).

    2026-08-28 pre-training pass, two real fixes:
      1. Filtered to THIS run_id (previously any past backtest run's
         trades, including a discarded one, could feed into reflection).
      2. Sorted by the exit's OWN timestamp (updated_at -- when the SELL
         row was actually resolved to 'filled'), globally across every
         ticker, before taking the most recent `limit` -- previously
         grouped by ticker first, so the final slice was "the last N in
         ticker-iteration order," not truly the N most recently closed
         positions overall.
    """
    script = f'''
import asyncio, json
from server import _get_trade_history

async def main():
    rows = await _get_trade_history(limit=200, execution_mode="backtest", run_id={run_id!r})
    print(json.dumps(rows, default=str))

asyncio.run(main())
'''
    rows = json_from_list(run_in("trade-journal", script))
    rows.sort(key=lambda r: r["created_at"])  # oldest first, for correct BUY->SELL pairing

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
                    # The SELL row's own resolution time -- what "most
                    # recently closed" actually means, not insertion order.
                    "_exit_time": t["updated_at"],
                })
                entry = None
    closed.sort(key=lambda c: c["_exit_time"])  # oldest close first, globally
    return [{k: v for k, v in c.items() if k != "_exit_time"} for c in closed[-limit:]]


def json_from_list(text: str) -> list:
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array found in:\n{text[:500]}")
    return json.loads(text[start : end + 1])


# --------------------------------------------------------------------------
# semantic response validation -- pure, no I/O (2026-08-28 pre-training pass)
#
# json_from() proves the reply PARSES as JSON; it says nothing about
# whether the object actually has the shape a cycle can safely act on. A
# syntactically valid but semantically empty/wrong reply (no "decisions"
# key, a BUY with primary_signal="sentiment", a self-reported reward:risk
# that doesn't match the trade's own stop/target math, a reflection day
# with no reflection) used to sail straight through and either silently do
# the wrong thing downstream or blow up much later (e.g. record_trade's
# own field validation, a full day after the decision was queued and a
# real quota slot already spent). This is the missing gate: validated
# BEFORE a decision is ever queued, with the SAME one-retry-then-fail-
# closed contract json_from already had for a parse failure -- see
# ask_hermes.
# --------------------------------------------------------------------------

class ResponseValidationError(ValueError):
    """A syntactically valid JSON reply that fails semantic validation."""


def _finite_positive(x) -> bool:
    return (
        isinstance(x, (int, float)) and not isinstance(x, bool)
        and math.isfinite(x) and x > 0
    )


_VALID_PRIMARY_SIGNALS = {"technical", "sentiment", "both", "other"}
_VALID_TECHNICAL_BASES = {"rsi", "macd", "support_resistance", "trend", "multiple", "other"}


def _validate_decision_shape(d: dict, i: int) -> None:
    """One decision object from the 'decisions' list. Mirrors trade-
    journal-mcp's own _validate_decision_fields (same rules -- BUY can
    never be primary_signal='sentiment', 'other' needs an explanation) plus
    the checks that module can't make: this is the ONLY point in the
    pipeline that ever sees Hermes' own proposed stop/target/entry
    together with its self-reported expected_reward_risk, so this is the
    only place that can catch a self-report that doesn't match its own
    trade's math."""
    prefix = f"decision[{i}]"
    if not isinstance(d, dict):
        raise ResponseValidationError(f"{prefix}: not a JSON object")

    alias = d.get("alias")
    if not isinstance(alias, str) or not alias.strip():
        raise ResponseValidationError(f"{prefix}: missing/blank alias")
    tag = f"{prefix} ({alias})"

    action = d.get("action")
    if action not in ("BUY", "SELL"):
        raise ResponseValidationError(f"{tag}: action must be BUY or SELL, got {action!r}")

    quantity = d.get("quantity")
    if (
        not isinstance(quantity, (int, float)) or isinstance(quantity, bool)
        or not math.isfinite(quantity) or quantity != int(quantity) or int(quantity) <= 0
    ):
        raise ResponseValidationError(f"{tag}: quantity must be a positive integer, got {quantity!r}")

    if not _finite_positive(d.get("entry_price")):
        raise ResponseValidationError(f"{tag}: entry_price must be a finite positive number, got {d.get('entry_price')!r}")

    confidence = d.get("confidence")
    if (
        not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
        or not (1 <= confidence <= 5)
    ):
        raise ResponseValidationError(f"{tag}: confidence must be 1-5, got {confidence!r}")

    primary_signal = d.get("primary_signal")
    if primary_signal not in _VALID_PRIMARY_SIGNALS:
        raise ResponseValidationError(f"{tag}: invalid primary_signal {primary_signal!r}")
    # RULES.md §5a: sentiment may only VETO an entry, never trigger one.
    if action == "BUY" and primary_signal == "sentiment":
        raise ResponseValidationError(
            f"{tag}: BUY cannot have primary_signal='sentiment' -- RULES.md §5a, "
            "sentiment may only veto an entry, never trigger one"
        )

    technical_basis = d.get("technical_basis")
    needs_basis = primary_signal in ("technical", "both")
    if needs_basis and technical_basis not in _VALID_TECHNICAL_BASES:
        raise ResponseValidationError(
            f"{tag}: technical_basis required when primary_signal={primary_signal!r}, got {technical_basis!r}"
        )

    invalidation = d.get("invalidation_condition")
    if not isinstance(invalidation, str) or not invalidation.strip():
        raise ResponseValidationError(f"{tag}: invalidation_condition is required and cannot be blank")

    reasoning = d.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ResponseValidationError(f"{tag}: reasoning is required and cannot be blank")

    chose_other = primary_signal == "other" or technical_basis == "other"
    if chose_other and not reasoning.strip():
        raise ResponseValidationError(f"{tag}: 'other' requires an explanation in reasoning")

    if action != "BUY":
        return  # stop/target/R:R are entry-only concerns (RULES.md §6a/§6b)

    entry = float(d["entry_price"])
    if not _finite_positive(d.get("stop_price")):
        raise ResponseValidationError(f"{tag}: stop_price must be a finite positive number, got {d.get('stop_price')!r}")
    if not _finite_positive(d.get("target_price")):
        raise ResponseValidationError(f"{tag}: target_price must be a finite positive number, got {d.get('target_price')!r}")
    stop, target = float(d["stop_price"]), float(d["target_price"])
    if stop >= entry:
        raise ResponseValidationError(f"{tag}: stop_price must be below entry_price for a long")
    if target <= entry:
        raise ResponseValidationError(f"{tag}: target_price must be above entry_price for a long")

    if not _finite_positive(d.get("expected_reward_risk")):
        raise ResponseValidationError(
            f"{tag}: expected_reward_risk must be a finite positive number, got {d.get('expected_reward_risk')!r}"
        )
    actual_rr = (target - entry) / (entry - stop)
    reported_rr = float(d["expected_reward_risk"])
    # Generous tolerance -- this only needs to catch a self-report that's
    # badly wrong (bad arithmetic, a stale number left over from editing
    # the trade), not cent-level rounding; the real §6b gate is
    # risk_checker.py's own tolerance, enforced separately and always.
    if abs(reported_rr - actual_rr) > max(0.25, actual_rr * 0.15):
        raise ResponseValidationError(
            f"{tag}: reported expected_reward_risk {reported_rr} does not match "
            f"the actual entry/stop/target math ({actual_rr:.2f})"
        )


def validate_hermes_response(response, is_reflection_day: bool) -> None:
    """Raises ResponseValidationError naming exactly what's wrong; callers
    (ask_hermes) get one corrective retry, then fail closed -- same
    contract as a JSON parse failure."""
    if not isinstance(response, dict):
        raise ResponseValidationError("response is not a JSON object")

    decisions = response.get("decisions")
    if not isinstance(decisions, list):
        raise ResponseValidationError("response missing a 'decisions' list")

    if not decisions:
        reason = response.get("no_action_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ResponseValidationError(
                "an empty decisions list requires a non-blank, honest no_action_reason"
            )

    for i, d in enumerate(decisions):
        _validate_decision_shape(d, i)

    if is_reflection_day:
        reflection = response.get("reflection")
        if not isinstance(reflection, str) or not reflection.strip():
            raise ResponseValidationError(
                "a reflection-day reply requires a non-blank 'reflection' field"
            )


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
            # Fresh point-in-time technicals/news for THIS position -- what
            # actually makes AGENTS.md §6d's thesis-invalidation check
            # ("has the signal that justified the entry reversed, or has
            # genuinely adverse news landed") answerable with real evidence
            # instead of only the entry/current-price numbers above.
            t = p.get("tech")
            if t:
                lines.append(
                    f"      RSI {t['rsi']:.1f} ({t['rsi_signal']}) | MACD {t['macd_signal']} "
                    f"| trend {t['trend']} | outlook {t['outlook']}"
                )
            for a in (p.get("news") or [])[:3]:
                lines.append(f"      news ({a['age_days']}d ago): {a['headline'][:110]}")
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


def ask_hermes(prompt: str, usage_totals: dict, is_reflection_day: bool = False,
               on_invoked: "Callable[[], None] | None" = None) -> dict:
    """One retry on a malformed OR semantically invalid reply — a real
    failure mode, seen on the very first live cycle (an unescaped quote
    inside the reasoning text broke the whole object and cost the cycle).
    2026-08-28: extended to also cover semantic validation (see
    validate_hermes_response) with the SAME one-retry-then-fail-closed
    contract -- a reply that parses as JSON but is missing required fields,
    proposes a BUY with primary_signal='sentiment', or omits a required
    reflection is exactly as unusable as one that doesn't parse at all.

    Retrying with the specific error quoted back is cheaper than losing the
    cycle: a wasted cycle still costs a quota slot AND yields nothing, while
    the retry costs the same slot and usually succeeds. Capped at one — if
    it cannot produce a valid reply twice, the cycle deserves to fail loudly
    rather than silently drop or fabricate a decision.

    usage_totals accumulates across both the original call and a retry (a
    retry is a second real API call and a second real cost -- both must
    count, not just the one that happened to validate).

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
        response = json_from(raw)
        validate_hermes_response(response, is_reflection_day)
        return response
    except (ValueError, json.JSONDecodeError) as exc:
        retry = (
            f"{prompt}\n\n---\nYour previous reply was invalid ({exc}). "
            "Return ONLY a corrected JSON object satisfying every "
            "requirement above, including every required field on each "
            "decision. Keep every free-text field to one short line with "
            "no quote characters, apostrophes, or line breaks inside it."
        )
        raw2, usage2 = _call_hermes(retry, on_invoked)
        _accumulate(usage2)
        # A second failure (parse OR validation) propagates uncaught --
        # fail closed, exactly like the pre-existing JSON-only contract.
        response2 = json_from(raw2)
        validate_hermes_response(response2, is_reflection_day)
        return response2


def _execute_exit(working: "Portfolio", ticker: str, quantity: int, entry_price: float,
                  exit_price: float, reason: str, kind: str, run_key: str, day_index: int,
                  summary: dict, activity: list, run_id: str | None = None) -> None:
    """Real bookkeeping + journal write for a FULL-quantity mechanical exit
    (opening gap, time-stop, or an intraday-walk stop/target touch) --
    replays one "exit" event from simulate_trading_day's plan against
    `working` with a real decision_id and a real record_exit call. No
    execution-sim/slippage here -- mechanical exits never went through
    that path, before or after this repair; only discretionary orders do.
    """
    proceeds = quantity * exit_price
    working.cash += proceeds
    working.closed_pnl.append(proceeds - quantity * entry_price)
    del working.holdings[ticker]
    line = f"{ticker} {reason} @ ${exit_price:.2f} (P&L ${working.closed_pnl[-1]:+,.2f})"
    summary["exits"].append(line)
    exit_id = record_exit(
        ticker, quantity, exit_price, reason,
        decision_id=make_decision_id(run_key, day_index, ticker, "SELL", kind),
        run_id=run_id,
    )
    activity.append(("exit", line, exit_id))


def build_decision_time_context(working: "Portfolio", bars: dict[str, dict],
                                watchlist: list[str], limits_ctx: dict) -> Context:
    """Context for validating a PROPOSED decision BEFORE it is queued --
    pure, no I/O (2026-08-28 pre-training pass, item 3). Uses TODAY's known
    state (equity/cash/positions marked at today's daily close) as a good-
    faith proxy: the real fill won't happen until tomorrow's real 09:45
    open, so this is deliberately advisory, not authoritative -- the real
    gate remains resolve_queued_order's execution-time call against the
    REAL price, unchanged.

    is_open/minute values are placeholders that are NEVER READ: the caller
    passes validate_timing=False to check_trade, which skips the §7 block
    entirely rather than pretending to know a fill time that doesn't exist
    yet -- see check_trade's own docstring for why faking a clock here
    would be wrong.
    """
    equity = working.equity(bars)
    return Context(
        account=AccountState(
            equity=equity, cash=working.cash,
            daily_pl_pct=working.pct_since(1), weekly_pl_pct=working.pct_since(5),
            drawdown_from_peak_pct=working.drawdown_pct(),
        ),
        clock=MarketClock(is_open=True, minutes_since_open=None, minutes_until_close=None),
        positions=[
            Position(ticker=t, quantity=h.quantity,
                    market_value=h.quantity * bars.get(t, {}).get("close", h.entry_price))
            for t, h in working.holdings.items()
        ],
        watchlist=watchlist,
        recent_losses=[p < 0 for p in reversed(working.closed_pnl)],
        stopped_out_today=working.stopped_out_today,
        **limits_ctx,
    )


# --------------------------------------------------------------------------
# the cycle
# --------------------------------------------------------------------------

def run_day(day: date, day_index: int, portfolio: Portfolio,
            masking: RunMasking, watchlist: list[dict], limits_ctx: dict,
            pacer: QuotaPacer, run_key: str) -> tuple[Portfolio, dict]:
    """Run one simulated trading day. Returns (new_portfolio, summary).

    PHASE 0 REPAIR + 2026-08-25 INTRADAY-CHRONOLOGY FOLLOW-UP:

    1. NO SAME-DAY LOOKAHEAD. Hermes still reasons on TODAY's completed
       data, but any BUY/SELL it decides is QUEUED, never executed today.
       See QueuedOrder.

    2. REAL INTRADAY EXECUTION TIMING. A queued BUY fills no earlier than
       09:45 ET (RULES.md §7's entry floor), a queued SELL is exempt and
       can fill at the 09:30 open, and the whole session is walked in real
       chronological order using Alpaca's 15-minute regular-session bars
       (fetch_intraday_bars) -- not approximated from one daily bar. See
       simulate_trading_day for the actual staged algorithm; this function
       only replays its plan with real I/O (journal writes, slippage-
       aware fills).

    3. WORKING COPY. Every mutation in this function happens on `working`,
       a deep copy of the portfolio passed in -- the caller's `portfolio`
       object is never touched. Only on full success does the caller
       adopt `working` as the new portfolio -- "never publish partially
       mutated portfolio state."
    """
    working = copy.deepcopy(portfolio)
    tickers = [t["symbol"] for t in watchlist]
    summary = {"day": str(day), "exits": [], "decisions": [], "blocked": [], "usage": {}}
    day_label = masking.day_index(day)
    # Deterministic per (run, day) -- see record_snapshot_once's docstring.
    # run_key doubles as this run's run_id (main() now generates it as a
    # real uuid4 and persists it in backtest_state.json -- 2026-08-28, item
    # 5) -- no separate parameter needed.
    cycle_id = str(uuid.uuid5(_DECISION_ID_NAMESPACE, f"{run_key}|cycle|{day_index}"))
    activity: list[tuple[str, str, str | None]] = [
        ("cycle_start", f"{day_label} ({day}) — reconciling and fetching data", None)
    ]

    # Daily bars: ONLY the cheap holiday/no-trading-day check now (and
    # positions_view's informational current_price further below) -- see
    # fetch_bars's docstring.
    bars = fetch_bars(tickers, day)
    if not bars:
        summary["skipped"] = "no market data (holiday/weekend)"
        return portfolio, summary  # nothing happened -- caller's object, unchanged
    # This IS a trading day (real bars exist) -- count it before anything
    # below reads it, so a position opened today is at trading-day-offset 0,
    # not -1.
    working.trading_days_elapsed += 1

    # -- chronological intraday execution ----------------------------------
    # Only fetch (and require) real intraday data for tickers that actually
    # need execution-timing this cycle -- currently held, or with a
    # decision queued yesterday. A quiet day with neither needs none of
    # this at all.
    needed = sorted(set(working.holdings) | {o.ticker for o in working.pending_orders})
    intraday: dict[str, list[dict]] = fetch_intraday_bars(needed, day) if needed else {}
    require_intraday_coverage(needed, intraday)  # fails closed -- see its docstring

    if needed:
        # Computed once, before any of today's processing -- equity_curve
        # isn't touched again until the very end of run_day, so these are
        # the same "as of end of yesterday" figures the whole session
        # should see, never drifting mid-day.
        plan = simulate_trading_day(
            holdings=working.holdings, pending_orders=working.pending_orders,
            intraday=intraday, day_index=day_index,
            trading_days_elapsed=working.trading_days_elapsed,
            limits_ctx=limits_ctx, watchlist=tickers, closed_pnl=working.closed_pnl,
            starting_cash=working.cash,
            daily_pl_pct=working.pct_since(1), weekly_pl_pct=working.pct_since(5),
            drawdown_from_peak_pct=working.drawdown_pct(),
        )
        working.stopped_out_today = []
        for event in plan.events:
            ticker, kind = event["ticker"], event["kind"]
            if kind == "exit":
                if event["reason"].startswith("stop"):
                    working.stopped_out_today.append(ticker)
                _execute_exit(
                    working, ticker, event["quantity"], event["entry_price"], event["exit_price"],
                    event["reason"], event["stage"], run_key, day_index, summary, activity,
                    run_id=run_key,
                )
            elif kind == "superseded":
                order = event["order"]
                line = f"{ticker}: queued {order.action} superseded -- {event['reason']}"
                activity.append(("order_queued", line, order.decision_id or None))
                # The decision was already journaled 'pending' at queue time
                # (record_queued_decision) -- a superseded order never fills,
                # so it must still resolve to something other than a
                # dangling 'pending' row. Treated as a rejection, honestly
                # reasoned ("superseded"), never a fabricated fill.
                if order.decision_id:
                    resolve_trade_rejection(order.decision_id, f"superseded: {event['reason']}")
            elif kind == "rejected":
                order = event["order"]
                line = f"{ticker}: queued {order.action} rejected -- {event['reason']}"
                summary["blocked"].append(line)
                activity.append(("order_rejected", line, order.decision_id or None))
                if order.decision_id:
                    resolve_trade_rejection(order.decision_id, event["reason"])
            elif kind == "fill":
                order, bar = event["order"], event["bar"]
                fill = simulate_fill(order.alias, order.action, order.quantity, bar["open"], bar)
                if fill["status"] != "filled":
                    line = f"{ticker}: sim {fill['rejection_reason']}"
                    summary["blocked"].append(line)
                    activity.append(("order_rejected", line, order.decision_id or None))
                    if order.decision_id:
                        resolve_trade_rejection(order.decision_id, fill["rejection_reason"])
                    continue
                # Resolve the SAME decision_id record_queued_decision
                # already journaled 'pending' at queue time -- never a
                # fresh id (item 2: one continuous row, proposal to
                # outcome). Falls back to a fresh journal write only for a
                # pre-migration pending order resumed from an old state
                # file (see Portfolio.from_dict's decision_id="" fallback).
                decision_id = order.decision_id
                if decision_id:
                    resolve_trade_fill(decision_id, fill)
                else:
                    fallback_id = make_decision_id(run_key, day_index, ticker, order.action, "queued_fill")
                    decision_id = record_trade(order.decision, ticker, fill, decision_id=fallback_id, run_id=run_key)
                if order.action == "BUY":
                    cost = fill["fill_price"] * fill["fill_quantity"]
                    working.cash -= cost
                    working.holdings[ticker] = Holding(
                        ticker=ticker, quantity=fill["fill_quantity"], entry_price=fill["fill_price"],
                        stop_price=order.stop_price, target_price=order.target_price,
                        entry_day=day_index, entry_trading_day=working.trading_days_elapsed,
                    )
                    line = (
                        f"BUY {ticker} ({order.alias}) {fill['fill_quantity']} @ ${fill['fill_price']:.2f} "
                        f"at 09:45 ET (queued day {order.queued_day_index + 1}) [{decision_id[:8]}]"
                    )
                    summary["decisions"].append(line)
                    activity.append(("order_placed", line, decision_id))
                else:  # SELL
                    holding = working.holdings[ticker]
                    proceeds = fill["fill_price"] * fill["fill_quantity"]
                    working.cash += proceeds
                    working.closed_pnl.append(proceeds - fill["fill_quantity"] * holding.entry_price)
                    if fill["fill_quantity"] >= holding.quantity:
                        del working.holdings[ticker]
                    else:
                        holding.quantity -= fill["fill_quantity"]
                    line = (
                        f"SELL {ticker} ({order.alias}) {fill['fill_quantity']} @ ${fill['fill_price']:.2f} "
                        f"at the 09:30 open (queued day {order.queued_day_index + 1}, P&L "
                        f"${working.closed_pnl[-1]:+,.2f}) [{decision_id[:8]}]"
                    )
                    summary["decisions"].append(line)
                    activity.append(("exit", line, decision_id))

    # Every order queued yesterday has now been resolved one way or
    # another (filled, rejected, or superseded) -- never carried forward
    # indefinitely.
    working.pending_orders = []

    # End of day: mark to the REAL market close (each held ticker's last
    # regular-session bar). Legitimate now that the day's chronological
    # processing above is actually complete -- "prior completed daily
    # close," not lookahead, from the perspective of the ONLY thing that
    # will ever read it (tomorrow's cycle).
    closing_prices = {
        t: {"close": intraday[t][-1]["close"]} for t in working.holdings if t in intraday
    }
    equity = working.equity(closing_prices)
    working.equity_curve.append(equity)

    # Per-open-position detail for the dashboard, computed from the exact
    # marks/state used for equity above -- not a second, possibly-drifting
    # source. Snapshot write itself is deferred to below (after ask_hermes)
    # purely so this cycle's real token usage can go in the SAME row instead
    # of a second write; the values captured here don't change between here
    # and there.
    snapshot_positions_detail = [
        {
            "ticker": t,
            "quantity": h.quantity,
            "entry_price": h.entry_price,
            "current_price": closing_prices.get(t, {}).get("close", h.entry_price),
            "stop_price": h.stop_price,
            "target_price": h.target_price,
            "days_held": day_index - h.entry_day,
            "unrealized_pl": (
                closing_prices.get(t, {}).get("close", h.entry_price) - h.entry_price
            ) * h.quantity,
            "unrealized_pl_pct": (
                (closing_prices.get(t, {}).get("close", h.entry_price) / h.entry_price - 1) * 100
                if h.entry_price else 0.0
            ),
        }
        for t, h in working.holdings.items()
    ]
    snapshot_kwargs = dict(
        day_label=masking.day_index(day), equity=equity, cash=working.cash,
        exposure_pct=working.invested(closing_prices) / equity * 100 if equity else 0.0,
        drawdown_pct=working.drawdown_pct(),
        open_positions=len(working.holdings),
        positions_detail=snapshot_positions_detail,
    )

    # -- STEP C: gather + mask today's data for Hermes to reason on -------
    # 2026-08-28 pre-training pass (item 4): fresh point-in-time technicals
    # AND news are now fetched for HELD tickers too, not only unheld
    # candidates -- previously §6d's thesis-invalidation check ("has the
    # signal that justified the entry reversed, or has genuinely adverse
    # news landed") had nothing fresh to reason from every cycle after
    # entry; a held position's technicals/news were never fetched again.
    # One combined fetch (not two) -- fewer real docker-exec round trips.
    held_tickers = list(working.holdings)
    unheld_tickers = [t for t in tickers if t not in working.holdings]
    all_technicals = fetch_technicals(unheld_tickers + held_tickers, day)
    news = fetch_news(unheld_tickers + held_tickers, day)
    technicals = {t: all_technicals[t] for t in unheld_tickers if t in all_technicals}
    held_technicals = {t: all_technicals[t] for t in held_tickers if t in all_technicals}

    candidates = []
    for ticker, tech in technicals.items():
        if "error" in tech:
            continue
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
        # None when a fetch error occurred for this ticker -- the position
        # itself is still shown (entry/current/stop/target above), just
        # without fresh evidence this cycle, same "skip, don't fabricate"
        # discipline candidates already follow.
        "tech": (
            masking.mask_technicals({**held_technicals[t], "ticker": t}, day)
            if t in held_technicals and "error" not in held_technicals[t] else None
        ),
        "news": masking.mask_news(news.get(t, []), day),
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
    # is a persisted counter, not a local one. Both filtered to THIS run
    # (run_key doubles as run_id) -- see fetch_recent_insights/
    # fetch_recent_closed_trades docstrings (item 8).
    recent_insights = fetch_recent_insights(run_id=run_key)
    is_reflection_day = working.cycles_since_reflection >= 4
    reflection_trades = (
        fetch_recent_closed_trades(masking, run_id=run_key) if is_reflection_day else None
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

    try:
        response = ask_hermes(prompt, summary["usage"], is_reflection_day=is_reflection_day,
                              on_invoked=mark_invoked)
    except Exception as exc:
        # Tag with whatever real usage ask_hermes accumulated before
        # failing (a validation-retry exhaustion still burns 1-2 real
        # calls) -- _run_cycle_or_halt reads this for log_cycle_failure's
        # "real token usage when available" (item 6). This is the single
        # most usage-relevant failure point (a real _call_hermes just ran);
        # a failure LATER in this function (a journal write, etc.) still
        # gets the real, current quota-pacer count via log_cycle_failure's
        # own pacer argument, just not this specific per-cycle token total.
        exc.usage = summary["usage"]
        raise

    # -- cycle-success gate (2026-08-28 pre-training pass, items 5+6) -----
    # This snapshot write is the ONE thing in this function protected by a
    # real database-level uniqueness constraint (cycle_id). `is_new` tells
    # us whether THIS call is the first time this (run, day) has ever
    # recorded a snapshot: if it is, this cycle's activity narration and
    # reflection insight are genuinely new and get written below; if it is
    # NOT (a retry of a day that fully succeeded before -- e.g. the local
    # state-file write failed AFTER Neon writes already landed), they are
    # correctly skipped, so a retry cannot duplicate a completed day's
    # narration or reflection. This does not change decisions/trades
    # journaling above -- those are already independently idempotent via
    # each trade's own deterministic decision_id.
    is_new = record_snapshot_once(run_id=run_key, cycle_id=cycle_id,
                                  usage=summary["usage"], pacer=pacer, **snapshot_kwargs)
    summary["no_action_reason"] = response.get("no_action_reason", "")
    if is_new and summary["no_action_reason"]:
        activity.append(("reasoning", summary["no_action_reason"][:900], None))

    if is_reflection_day:
        # validate_hermes_response already guarantees a non-blank
        # reflection on a reflection day (fails closed otherwise) -- this
        # if/else is defense in depth, not the primary gate: a missing
        # reflection must never reset the cadence (item 1's last
        # requirement), it should keep trying every subsequent cycle.
        reflection_text = response.get("reflection", "").strip()
        if reflection_text:
            if is_new:
                # reflection_trades IS the closed-trade set this reflection
                # was actually drawn from -- its length is the real sample
                # size, not a guess.
                record_insight(reflection_text, sample_size=len(reflection_trades or []),
                               run_id=run_key, cycle_id=cycle_id)
                activity.append(("weekly_reflection", reflection_text[:900], None))
            working.cycles_since_reflection = 0
        else:
            working.cycles_since_reflection += 1
    else:
        working.cycles_since_reflection += 1

    # -- STEP D: risk-validate, then QUEUE today's decisions -- never -----
    #    execute them today. This is the other half of the no-lookahead
    #    guarantee: whatever Hermes decided from today's completed data
    #    waits for tomorrow's real open (step B, next invocation).
    #
    # 2026-08-28 pre-training pass (items 1-3):
    #   - Structural checks below are now DEFENSE IN DEPTH -- validate_
    #     hermes_response already guarantees every field's shape before
    #     this loop ever runs (see ask_hermes); they should rarely fire.
    #   - A NEW decision-time risk check (check_trade, validate_timing=
    #     False) runs every proposal through the SAME rule set the real
    #     execution-time check uses -- §1/§2/§3/§4/§6/§9 in full, §7 timing
    #     deliberately skipped (the real fill instant isn't known yet; see
    #     build_decision_time_context). This is advisory/pre-flight, NOT a
    #     duplicate of the authoritative check: resolve_queued_order still
    #     re-validates for real against tomorrow's real 09:45 price and
    #     remains the sole authority on whether the trade actually happens.
    #   - Every decision that PASSES both gates is now journaled the
    #     instant it's queued (status 'pending'), not only once it fills --
    #     see record_queued_decision / QueuedOrder.decision_id.
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
        try:
            entry_price = float(decision.get("entry_price", 0.0))
        except (TypeError, ValueError):
            summary["blocked"].append(f"{real_ticker}: invalid entry_price -- cannot queue")
            continue

        proposal = ProposedTrade(
            ticker=real_ticker, action=action, quantity=quantity,
            entry_price=entry_price,
            stop_price=float(stop_price) if stop_price is not None else None,
            target_price=float(target_price) if target_price is not None else None,
        )
        decision_ctx = build_decision_time_context(working, bars, tickers, limits_ctx)
        decision_verdict = check_trade(proposal, decision_ctx, validate_timing=False)
        if not decision_verdict.allowed:
            reason = "; ".join(decision_verdict.violations)
            line = f"{real_ticker}: decision-time risk check rejected -- {reason}"
            summary["blocked"].append(line)
            activity.append(("risk_check", line, None))
            continue

        decision_id = make_decision_id(run_key, day_index, real_ticker, action, "queued_proposal")
        record_queued_decision(decision, real_ticker, decision_id, run_id=run_key)
        order = QueuedOrder(
            ticker=real_ticker, alias=decision["alias"], action=action, quantity=quantity,
            stop_price=proposal.stop_price, target_price=proposal.target_price,
            entry_price=entry_price,  # audit only, never used to fill
            queued_day_index=day_index, decision=decision, decision_id=decision_id,
        )
        working.pending_orders.append(order)
        line = f"{action} {real_ticker} ({decision['alias']}) x{quantity} queued for next trading day's open"
        summary["decisions"].append(line)
        activity.append(("order_queued", line, decision_id))

    if is_new:
        log_activity(activity, run_id=run_key, cycle_id=cycle_id)
    return working, summary


def _run_cycle_or_halt(run_day_fn: "Callable[[], tuple[Portfolio, dict]]", day_label: str = "",
                       run_id: str | None = None, pacer: "QuotaPacer | None" = None):
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
        # Best-effort dashboard visibility (item 6): real token usage when
        # available (ask_hermes tags a raised exception with `.usage` --
        # see run_day) and the real, current quota-pacer count (always
        # available). Never let a logging failure mask the real exception
        # above (e.g. the failure itself was a lost Docker/Neon connection,
        # in which case this write will also fail, harmlessly).
        try:
            log_cycle_failure(
                day_label, f"{type(exc).__name__}: {exc}", run_id,
                usage=getattr(exc, "usage", None), pacer=pacer,
            )
        except Exception:
            pass
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

    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        portfolio = Portfolio.from_dict(state["portfolio"])
        cursor = date.fromisoformat(state["cursor"])
        # 2026-08-28 pre-training pass (item 5): a RESUMED run keeps its
        # existing run_id -- .get() falls back to a fresh one only for a
        # state file written before this field existed (never true after
        # today's baseline reset, but harmless either way: an old file has
        # no pending_orders with a real decision_id to collide with).
        run_id = state.get("run_id") or str(uuid.uuid4())
        print(f"resuming from {cursor} (run_id={run_id})")
    else:
        portfolio, cursor = Portfolio(), start
        run_id = None  # generated below

    # --reset (or a genuinely fresh state file) ALWAYS gets a brand new
    # run_id -- this is what makes "do not let --reset reuse old trade IDs
    # from an earlier run" real: make_decision_id derives every id from
    # run_key, and run_key IS this run_id below, so a fresh uuid4 here
    # guarantees a fresh id space even if --start/--seed are identical to
    # a prior, discarded run.
    if run_id is None:
        run_id = str(uuid.uuid4())
        print(f"starting fresh run (run_id={run_id})")
    run_key = run_id

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
            lambda: run_day(cursor, day_index, portfolio, masking, watchlist, {}, pacer, run_key),
            day_label=masking.day_index(cursor),
            run_id=run_id, pacer=pacer,
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
            {"portfolio": portfolio.to_dict(), "cursor": str(cursor + timedelta(days=1)),
             "run_id": run_id},
        )
        cursor += timedelta(days=1)

    final = portfolio.equity_curve[-1]
    print(f"\n{'=' * 70}")
    print(f"run_id: {run_id}")
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
