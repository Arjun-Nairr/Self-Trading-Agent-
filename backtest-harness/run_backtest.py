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

Usage:
    python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days 3
"""
import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

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
        )


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


def record_trade(decision: dict, real_ticker: str, fill: dict) -> str:
    """Journal the REAL ticker, never the alias — the mask exists to keep
    Hermes honest, not to obscure our own records."""
    script = f'''
import asyncio, json
from server import _record_decision, _record_execution_result

async def main():
    d = await _record_decision(
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


def record_exit(real_ticker: str, quantity: int, exit_price: float, reason: str) -> str:
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
    """
    script = f'''
import asyncio, json
from server import _record_decision, _record_execution_result

async def main():
    d = await _record_decision(
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


def _call_hermes(prompt: str) -> str:
    escaped = prompt.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "--workdir", "/opt/data/workspace",
         "gateway", "hermes", "-z", escaped, "-t", ""],
        capture_output=True, text=True, cwd=REPO, timeout=600,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"hermes failed:\n{result.stderr[-1500:]}")
    return result.stdout


def ask_hermes(prompt: str) -> dict:
    """One retry on malformed JSON — a real failure mode, seen on the very
    first live cycle (an unescaped quote inside the reasoning text broke the
    whole object and cost the cycle).

    Retrying with the parse error quoted back is cheaper than losing the
    cycle: a wasted cycle still costs a quota slot AND yields nothing, while
    the retry costs the same slot and usually succeeds. Capped at one — if
    it cannot produce valid JSON twice, the cycle deserves to fail loudly
    rather than silently drop a decision.
    """
    raw = _call_hermes(prompt)
    try:
        return json_from(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        retry = (
            f"{prompt}\n\n---\nYour previous reply was not valid JSON "
            f"({exc}). Return ONLY the JSON object. Keep every free-text "
            "field to one short line with no quote characters, apostrophes, "
            "or line breaks inside it."
        )
        return json_from(_call_hermes(retry))


# --------------------------------------------------------------------------
# the cycle
# --------------------------------------------------------------------------

def run_day(day: date, day_index: int, portfolio: Portfolio,
            masking: RunMasking, watchlist: list[dict], limits_ctx: dict) -> dict:
    tickers = [t["symbol"] for t in watchlist]
    summary = {"day": str(day), "exits": [], "decisions": [], "blocked": []}
    day_label = masking.day_index(day)
    activity: list[tuple[str, str, str | None]] = [
        ("cycle_start", f"{day_label} ({day}) — reconciling and fetching data", None)
    ]

    bars = fetch_bars(tickers, day)
    if not bars:
        summary["skipped"] = "no market data (holiday/weekend)"
        return summary
    # This IS a trading day (real bars exist) -- count it before anything
    # below reads it, so a position opened today is at trading-day-offset 0,
    # not -1.
    portfolio.trading_days_elapsed += 1

    # -- mechanical exits first: a resting stop fires without judgment ----
    portfolio.stopped_out_today = []
    for ticker, holding in list(portfolio.holdings.items()):
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
            portfolio.stopped_out_today.append(ticker)
        elif bar["high"] >= holding.target_price:
            exit_price, reason = holding.target_price, "target hit"
        elif portfolio.trading_days_elapsed - holding.entry_trading_day >= TIME_STOP_TRADING_DAYS:
            exit_price, reason = bar["close"], f"time stop ({TIME_STOP_TRADING_DAYS} trading days)"
        if exit_price:
            proceeds = holding.quantity * exit_price
            portfolio.cash += proceeds
            portfolio.closed_pnl.append(
                proceeds - holding.quantity * holding.entry_price
            )
            del portfolio.holdings[ticker]
            line = (
                f"{ticker} {reason} @ ${exit_price:.2f} "
                f"(P&L ${portfolio.closed_pnl[-1]:+,.2f})"
            )
            summary["exits"].append(line)
            exit_decision_id = record_exit(ticker, holding.quantity, exit_price, reason)
            activity.append(("exit", line, exit_decision_id))

    equity = portfolio.equity(bars)
    portfolio.equity_curve.append(equity)
    record_snapshot(
        day_label=masking.day_index(day), equity=equity, cash=portfolio.cash,
        exposure_pct=portfolio.invested(bars) / equity * 100 if equity else 0.0,
        drawdown_pct=portfolio.drawdown_pct(),
        open_positions=len(portfolio.holdings),
    )

    # -- gather + mask ----------------------------------------------------
    technicals = fetch_technicals(
        [t for t in tickers if t not in portfolio.holdings], day
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
    } for t, h in portfolio.holdings.items()]

    account_view = {
        "equity": equity, "cash": portfolio.cash,
        "exposure_pct": portfolio.invested(bars) / equity * 100 if equity else 0.0,
        "daily_pl_pct": portfolio.pct_since(1),
        "weekly_pl_pct": portfolio.pct_since(5),
        "drawdown_pct": portfolio.drawdown_pct(),
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
    is_reflection_day = portfolio.cycles_since_reflection >= 4
    reflection_trades = (
        fetch_recent_closed_trades(masking) if is_reflection_day else None
    )

    prompt = build_prompt(masking.day_index(day), account_view,
                          positions_view, candidates, recent_insights,
                          reflection_trades)
    response = ask_hermes(prompt)
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
        portfolio.cycles_since_reflection = 0
    else:
        portfolio.cycles_since_reflection += 1

    # -- act on decisions -------------------------------------------------
    for decision in response.get("decisions", []):
        try:
            real_ticker = masking.real(decision["alias"])
        except KeyError:
            summary["blocked"].append(f"unknown alias {decision.get('alias')!r}")
            continue

        trade = ProposedTrade(
            ticker=real_ticker, action=decision["action"].upper(),
            quantity=int(decision["quantity"]),
            entry_price=float(decision["entry_price"]),
            stop_price=decision.get("stop_price"),
            target_price=decision.get("target_price"),
        )
        ctx = Context(
            account=AccountState(
                equity=equity, cash=portfolio.cash,
                daily_pl_pct=account_view["daily_pl_pct"],
                weekly_pl_pct=account_view["weekly_pl_pct"],
                drawdown_from_peak_pct=portfolio.drawdown_pct(),
            ),
            clock=MarketClock(is_open=True, minutes_since_open=120,
                              minutes_until_close=120),
            positions=[
                Position(ticker=t, quantity=h.quantity,
                         market_value=h.quantity * bars.get(t, {}).get("close", h.entry_price))
                for t, h in portfolio.holdings.items()
            ],
            watchlist=tickers,
            recent_losses=[p < 0 for p in reversed(portfolio.closed_pnl)],
            stopped_out_today=portfolio.stopped_out_today,
            **limits_ctx,
        )

        verdict = check_trade(trade, ctx)
        if not verdict.allowed:
            line = f"{real_ticker}: {'; '.join(verdict.violations)}"
            summary["blocked"].append(line)
            activity.append(("risk_check", f"BLOCKED — {line}", None))
            continue
        activity.append(("risk_check", f"{real_ticker}: passed RULES.md checks", None))

        bar = bars.get(real_ticker)
        if not bar:
            summary["blocked"].append(f"{real_ticker}: no bar data")
            continue

        fill = simulate_fill(decision["alias"], trade.action, trade.quantity,
                             trade.entry_price, bar)
        if fill["status"] != "filled":
            line = f"{real_ticker}: sim {fill['rejection_reason']}"
            summary["blocked"].append(line)
            activity.append(("order_rejected", line, None))
            continue

        if trade.action == "SELL":
            # Discretionary (thesis-invalidation) exit -- the one exit type
            # that's Hermes' judgment call, not a mechanical stop/target/
            # time-stop (those go through record_exit in the loop above).
            # This branch never fired until now, since no backtest cycle had
            # produced a SELL decision -- found by tracing the code, not by
            # a failure: the block below it unconditionally treated every
            # fill as a BUY (subtracted cash, overwrote the holding with a
            # new lot, mislabeled the log line), which would have corrupted
            # the portfolio the first time Hermes actually called an exit.
            holding = portfolio.holdings[real_ticker]
            proceeds = fill["fill_price"] * fill["fill_quantity"]
            portfolio.cash += proceeds
            portfolio.closed_pnl.append(
                proceeds - fill["fill_quantity"] * holding.entry_price
            )
            if fill["fill_quantity"] >= holding.quantity:
                del portfolio.holdings[real_ticker]
            else:
                holding.quantity -= fill["fill_quantity"]
            decision_id = record_trade(decision, real_ticker, fill)
            line = (
                f"SELL {real_ticker} ({decision['alias']}) {fill['fill_quantity']} "
                f"@ ${fill['fill_price']:.2f} (P&L ${portfolio.closed_pnl[-1]:+,.2f}) "
                f"[{decision_id[:8]}]"
            )
            summary["decisions"].append(line)
            activity.append(("exit", line, decision_id))
        else:
            cost = fill["fill_price"] * fill["fill_quantity"]
            portfolio.cash -= cost
            portfolio.holdings[real_ticker] = Holding(
                ticker=real_ticker, quantity=fill["fill_quantity"],
                entry_price=fill["fill_price"], stop_price=float(trade.stop_price),
                target_price=float(trade.target_price), entry_day=day_index,
                entry_trading_day=portfolio.trading_days_elapsed,
            )
            decision_id = record_trade(decision, real_ticker, fill)
            line = (
                f"BUY {real_ticker} ({decision['alias']}) {fill['fill_quantity']} "
                f"@ ${fill['fill_price']:.2f} conf={decision.get('confidence')} "
                f"[{decision_id[:8]}]"
            )
            summary["decisions"].append(line)
            activity.append(("order_placed", line, decision_id))

    log_activity(activity)
    return summary


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
        print(f"resuming from {cursor}")
    else:
        portfolio, cursor = Portfolio(), start

    end = date.fromisoformat(args.end)
    completed = 0
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
        try:
            summary = run_day(cursor, day_index, portfolio, masking,
                              watchlist, {})
        except Exception as exc:
            print(f"  CYCLE FAILED: {type(exc).__name__}: {exc}")
            cursor += timedelta(days=1)
            continue

        if "skipped" in summary:
            print(f"  skipped: {summary['skipped']}")
            cursor += timedelta(days=1)
            continue

        pacer.record_cycle()
        completed += 1
        for line in summary["exits"]:
            print(f"  EXIT    {line}")
        for line in summary["decisions"]:
            print(f"  TRADE   {line}")
        for line in summary["blocked"]:
            print(f"  BLOCKED {line}")
        if not summary["decisions"] and not summary["blocked"]:
            print(f"  no trade — {summary['no_action_reason'][:160]}")

        STATE_PATH.write_text(json.dumps(
            {"portfolio": portfolio.to_dict(), "cursor": str(cursor + timedelta(days=1))},
            indent=1,
        ))
        cursor += timedelta(days=1)

    final = portfolio.equity_curve[-1]
    print(f"\n{'=' * 70}")
    print(f"cycles this run: {completed} | quota used this week: "
          f"{pacer.used_this_week()}/{pacer.budget}")
    print(f"equity ${final:,.2f} ({(final / STARTING_EQUITY - 1) * 100:+.2f}%) "
          f"| open {len(portfolio.holdings)} | closed {len(portfolio.closed_pnl)}")


if __name__ == "__main__":
    main()
