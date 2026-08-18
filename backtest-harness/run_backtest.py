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
    entry_day: int


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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Portfolio":
        return cls(
            cash=d["cash"],
            holdings={t: Holding(**h) for t, h in d["holdings"].items()},
            equity_curve=d["equity_curve"],
            closed_pnl=d["closed_pnl"],
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
    decides whether a limit would have crossed."""
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


# --------------------------------------------------------------------------
# prompt + Hermes
# --------------------------------------------------------------------------

def build_prompt(day_label: str, account: dict, positions: list[dict],
                 candidates: list[dict]) -> str:
    lines = [
        f"BACKTEST CYCLE — {day_label}", "",
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

    lines.append(
        "Decide. Reply with ONLY a JSON object, no prose around it:\n"
        '{"decisions": [{"alias": "...", "action": "BUY"|"SELL", "quantity": N,\n'
        '  "entry_price": N, "stop_price": N, "target_price": N,\n'
        '  "confidence": 1-5, "primary_signal": "technical"|"sentiment"|"both"|"other",\n'
        '  "technical_basis": "rsi"|"macd"|"support_resistance"|"trend"|"multiple"|"other",\n'
        '  "expected_reward_risk": N, "invalidation_condition": "...",\n'
        '  "reasoning": "..."}],\n'
        ' "no_action_reason": "..."}\n'
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

    bars = fetch_bars(tickers, day)
    if not bars:
        summary["skipped"] = "no market data (holiday/weekend)"
        return summary

    # -- mechanical exits first: a resting stop fires without judgment ----
    portfolio.stopped_out_today = []
    for ticker, holding in list(portfolio.holdings.items()):
        bar = bars.get(ticker)
        if not bar:
            continue
        exit_price = reason = None
        if bar["low"] <= holding.stop_price:
            exit_price, reason = holding.stop_price, "stop hit"
            portfolio.stopped_out_today.append(ticker)
        elif bar["high"] >= holding.target_price:
            exit_price, reason = holding.target_price, "target hit"
        elif day_index - holding.entry_day >= 10:
            exit_price, reason = bar["close"], "time stop (10 days)"
        if exit_price:
            proceeds = holding.quantity * exit_price
            portfolio.cash += proceeds
            portfolio.closed_pnl.append(
                proceeds - holding.quantity * holding.entry_price
            )
            del portfolio.holdings[ticker]
            summary["exits"].append(
                f"{ticker} {reason} @ ${exit_price:.2f} "
                f"(P&L ${portfolio.closed_pnl[-1]:+,.2f})"
            )

    equity = portfolio.equity(bars)
    portfolio.equity_curve.append(equity)

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

    prompt = build_prompt(masking.day_index(day), account_view,
                          positions_view, candidates)
    response = ask_hermes(prompt)
    summary["no_action_reason"] = response.get("no_action_reason", "")

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
            summary["blocked"].append(f"{real_ticker}: {'; '.join(verdict.violations)}")
            continue

        bar = bars.get(real_ticker)
        if not bar:
            summary["blocked"].append(f"{real_ticker}: no bar data")
            continue

        fill = simulate_fill(decision["alias"], trade.action, trade.quantity,
                             trade.entry_price, bar)
        if fill["status"] != "filled":
            summary["blocked"].append(f"{real_ticker}: sim {fill['rejection_reason']}")
            continue

        cost = fill["fill_price"] * fill["fill_quantity"]
        portfolio.cash -= cost
        portfolio.holdings[real_ticker] = Holding(
            ticker=real_ticker, quantity=fill["fill_quantity"],
            entry_price=fill["fill_price"], stop_price=float(trade.stop_price),
            target_price=float(trade.target_price), entry_day=day_index,
        )
        decision_id = record_trade(decision, real_ticker, fill)
        summary["decisions"].append(
            f"BUY {real_ticker} ({decision['alias']}) {fill['fill_quantity']} "
            f"@ ${fill['fill_price']:.2f} conf={decision.get('confidence')} "
            f"[{decision_id[:8]}]"
        )

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

    names = {
        t["symbol"]: [t["name"], t["name"].split(",")[0].split(" Inc")[0].strip()]
        for t in watchlist
    }
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
