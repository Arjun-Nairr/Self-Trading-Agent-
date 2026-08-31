"""
alpaca-mcp — our own minimal execution tool over Alpaca's Paper Trading
API. Rebuilt after the official alpacahq/alpaca-mcp-server turned out to
bundle stock, crypto, and options order placement into one tool
(register_order_tools) with no config-level way to expose stocks only —
confirmed via their code and via Alpaca's own dashboard (account-level
options-trading-level setting doesn't touch crypto either). This tool
structurally can't place a crypto or options order because that code was
never written — not "disabled," just never exists.

Six narrow tools, matching what the project's design actually needs (not
the official tool's full 65-tool surface):
  - place_order: submit a stock order (same interface as execution-sim,
    the tool this replaces as primary)
  - get_positions: real broker-side position state — §6c wants the
    broker feed as the AUTHORITATIVE source, not our own trade-journal
    bookkeeping, which could drift from reality if we have a bug
  - get_order_status: look up an order by decision_id (Alpaca's
    client_order_id) — needed for §6b's restart-reconciliation flow
    (check real broker state for any 'pending' trade-journal row before
    doing anything else after a restart)
  - get_account: equity, cash, exposure, and performance state
  - get_historical_news: point-in-time company headlines for backtests
  - get_intraday_bars: point-in-time 15-minute execution bars for backtests

decision_id is threaded through as Alpaca's client_order_id — a second,
broker-level idempotency backstop on top of trade-journal's own
decision_id uniqueness (§6b): our DB protects against retries at OUR
layer (crash before recording result); client_order_id protects against
a retry actually reaching Alpaca twice.
"""
import asyncio
import json
import os
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP

# Mounted read-only from the repo (see docker-compose.yml) rather than
# copied in, so there is exactly one risk_checker and one watchlist across
# the whole project — no per-container duplicate to drift.
from risk_checker import (
    AccountState, Context, MarketClock, Position, ProposedTrade, check_trade,
)

WATCHLIST_PATH = "/app/watchlist.json"

mcp = FastMCP("alpaca")

BASE_URL = "https://paper-api.alpaca.markets"
# Separate host from BASE_URL -- market/news data lives on Alpaca's data API,
# not the paper-trading order API, even though both use the same key/secret.
DATA_URL = "https://data.alpaca.markets"
TERMINAL_STATUSES = {"filled", "partially_filled", "canceled", "rejected", "expired"}
POLL_ATTEMPTS = 6
POLL_DELAY_SECONDS = 1.5

# The backtest harness's intraday no-lookahead execution timing (RULES.md
# §7's 9:45 ET entry floor) needs real ET wall-clock boundaries, not raw
# UTC -- New York's UTC offset changes with DST across the backtest
# window (Feb -> Aug 2026 crosses the March DST transition). zoneinfo
# (stdlib) handles this correctly; no new dependency.
_ET = ZoneInfo("America/New_York")
_REGULAR_SESSION_START = dt_time(9, 30)
_REGULAR_SESSION_END = dt_time(16, 0)


def _headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }


async def _submit_order(
    ticker: str, action: str, quantity: int, order_type: str,
    limit_price: float | None, decision_id: str,
) -> dict[str, Any]:
    body = {
        "symbol": ticker,
        "qty": str(quantity),
        "side": action.lower(),
        "type": order_type.lower(),
        "time_in_force": "day",
        "client_order_id": decision_id,
    }
    if order_type.upper() == "LIMIT":
        if limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        body["limit_price"] = str(limit_price)

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BASE_URL}/v2/orders", json=body, headers=_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()


async def _poll_for_terminal(order_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        for _ in range(POLL_ATTEMPTS):
            resp = await client.get(f"{BASE_URL}/v2/orders/{order_id}", headers=_headers(), timeout=10)
            resp.raise_for_status()
            order = resp.json()
            if order["status"] in TERMINAL_STATUSES:
                return order
            await asyncio.sleep(POLL_DELAY_SECONDS)
        return order  # last-known, non-terminal — caller reports this honestly


def _to_journal_status(alpaca_status: str) -> str:
    if alpaca_status in ("filled", "partially_filled"):
        return "filled"
    if alpaca_status in ("rejected", "canceled", "expired"):
        return "rejected"
    return "pending"  # still resolving — reconciliation flow (§6b) picks this up later


def _parse_fill(order: dict[str, Any]) -> tuple[float | None, int | None]:
    """(fill_price, fill_quantity) from a raw Alpaca order.

    filled_qty is always present as the string "0" even when nothing's
    filled — filled_avg_price is the field that's genuinely null until a
    real fill happens, so gate both on that, not on filled_qty (which is
    truthy as "0" and would otherwise report a fake 0-share fill).
    """
    if not order.get("filled_avg_price"):
        return None, None
    return float(order["filled_avg_price"]), int(float(order["filled_qty"]))


async def _pre_trade_check(
    ticker: str, action: str, quantity: int, entry_price: float,
    stop_price: float | None, target_price: float | None,
) -> None:
    """Enforce RULES.md before an order can reach the broker.

    This is the live-mode chokepoint. It sits inside place_order rather than
    being its own MCP tool on purpose: a check the agent chooses whether to
    call is not enforcement. Every live order passes through here.

    KNOWN GAP — §9.4 (no same-day re-entry after a stop-out) needs trade
    history, which lives in trade-journal, and the hub-and-spoke topology
    means tools never talk to each other. The backtest harness enforces it.
    A future live-cycle orchestrator must do the same; this is not silently
    handled here.
    """
    account, positions, clock = (
        await _fetch_account(), await _fetch_positions(), await _fetch_clock()
    )
    watchlist = [t["symbol"] for t in json.load(open(WATCHLIST_PATH))["tickers"]]

    verdict = check_trade(
        ProposedTrade(
            ticker=ticker, action=action, quantity=quantity,
            entry_price=entry_price, stop_price=stop_price,
            target_price=target_price,
        ),
        Context(
            account=AccountState(
                equity=account["equity"], cash=account["cash"],
                daily_pl_pct=account["daily_pl_pct"],
                weekly_pl_pct=account["weekly_pl_pct"],
                drawdown_from_peak_pct=account["drawdown_from_peak_pct"],
                trading_blocked=account["trading_blocked"],
            ),
            clock=MarketClock(**clock),
            positions=[
                Position(
                    ticker=p["symbol"], quantity=float(p["qty"]),
                    market_value=float(p["market_value"]),
                )
                for p in positions
            ],
            watchlist=watchlist,
        ),
    )
    if not verdict.allowed:
        raise ValueError(
            "BLOCKED by RULES.md — " + "; ".join(verdict.violations)
        )


@mcp.tool()
async def place_order(
    ticker: str,
    action: str,
    quantity: int,
    order_type: str,
    decision_id: str,
    entry_price: float,
    stop_price: float | None = None,
    target_price: float | None = None,
    limit_price: float | None = None,
) -> dict[str, Any]:
    """
    Place a real paper stock order via Alpaca. action: BUY or SELL.
    order_type: MARKET or LIMIT. decision_id (pass trade-journal's
    decision_id) becomes Alpaca's client_order_id for broker-level
    idempotency — always pass it.

    entry_price: the price you sized this position against. stop_price and
    target_price: required on a BUY — RULES.md §6a says the stop is placed
    at the same time as the entry, never added later, so an order without
    one is rejected here rather than merely discouraged.

    EVERY ORDER IS CHECKED AGAINST RULES.md BEFORE IT REACHES THE BROKER.
    A violation raises with the specific rule cited; nothing is silently
    adjusted for you. If you are blocked, read the reason and reconsider the
    trade — do not retry with tweaked numbers until it slips through.

    Returns: status, fill_price, fill_quantity, broker_order_id,
    rejection_reason.
    """
    await _pre_trade_check(
        ticker, action, quantity, entry_price, stop_price, target_price
    )
    submitted = await _submit_order(ticker, action, quantity, order_type, limit_price, decision_id)
    order = await _poll_for_terminal(submitted["id"])
    status = _to_journal_status(order["status"])
    fill_price, fill_quantity = _parse_fill(order)

    return {
        "status": status,
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "broker_order_id": order["id"],
        "rejection_reason": None if status != "rejected" else f"alpaca status: {order['status']}",
        "filled_at": order.get("filled_at") or datetime.now(timezone.utc).isoformat(),
    }


def _pct_change(current: float, baseline: float | None) -> float | None:
    """Percent change vs a baseline, or None when the baseline can't support
    the calculation. Returns None rather than 0.0 for a missing/zero
    baseline -- 0.0 would read as "flat, no loss, breaker clear," which is a
    fabricated all-clear on a safety check. None means "unknown," and
    unknown must be treated as blocking (see get_account)."""
    if not baseline:
        return None
    return round((current - baseline) / baseline * 100, 2)


def _summarize_history(
    timestamps: list[int], equities: list[float], now: datetime
) -> tuple[float | None, float | None]:
    """(equity at the start of the current ISO week, peak equity ever seen).

    Zero-valued points are filtered out: Alpaca returns 0.0 equity for days
    before the account was funded, and letting those through would make
    peak-drawdown math nonsense (a drawdown measured against a peak of 0,
    or a "week start" of 0 producing an infinite loss).

    ISO week (Monday-start) is used consistently everywhere in this
    project, so "this week" means the same thing everywhere.
    """
    points = [(t, e) for t, e in zip(timestamps, equities) if e]
    if not points:
        return None, None

    peak = max(e for _, e in points)
    iso_now = now.isocalendar()
    this_week = [
        e for t, e in points
        if datetime.fromtimestamp(t, timezone.utc).isocalendar()[:2]
        == (iso_now.year, iso_now.week)
    ]
    return (this_week[0] if this_week else None), peak


async def _fetch_account() -> dict[str, Any]:
    """Shared by the get_account tool and the pre-trade risk gate."""
    async with httpx.AsyncClient() as client:
        acct_resp = await client.get(
            f"{BASE_URL}/v2/account", headers=_headers(), timeout=10
        )
        acct_resp.raise_for_status()
        acct = acct_resp.json()

        hist_resp = await client.get(
            f"{BASE_URL}/v2/account/portfolio/history",
            params={"period": "1A", "timeframe": "1D"},
            headers=_headers(), timeout=10,
        )
        history = hist_resp.json() if hist_resp.status_code == 200 else {}

    equity = float(acct["equity"])
    week_start_equity, peak_equity = _summarize_history(
        history.get("timestamp", []), history.get("equity", []),
        datetime.now(timezone.utc),
    )
    peak_equity = max(peak_equity, equity) if peak_equity else equity

    return {
        "equity": equity,
        "cash": float(acct["cash"]),
        "exposure_pct": round(float(acct["long_market_value"]) / equity * 100, 2)
        if equity else None,
        "daily_pl_pct": _pct_change(equity, float(acct["last_equity"])),
        "weekly_pl_pct": _pct_change(equity, week_start_equity),
        "drawdown_from_peak_pct": _pct_change(equity, peak_equity),
        "trading_blocked": acct["trading_blocked"],
        "account_blocked": acct["account_blocked"],
        "status": acct["status"],
    }


async def _fetch_positions() -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()


async def _fetch_clock() -> dict[str, Any]:
    """Market open state plus how far into/from the session we are — §7's
    opening and closing buffers need minutes, not just an is_open flag."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/v2/clock", headers=_headers(), timeout=10)
        resp.raise_for_status()
        clock = resp.json()

    now = datetime.fromisoformat(clock["timestamp"])
    next_open = datetime.fromisoformat(clock["next_open"])
    next_close = datetime.fromisoformat(clock["next_close"])
    if clock["is_open"]:
        # While open, next_open is tomorrow's — derive today's session start
        # from next_close instead (US equities run 6.5h).
        session_open = next_close - timedelta(hours=6, minutes=30)
        return {
            "is_open": True,
            "minutes_since_open": int((now - session_open).total_seconds() // 60),
            "minutes_until_close": int((next_close - now).total_seconds() // 60),
        }
    return {"is_open": False, "minutes_since_open": None, "minutes_until_close": None}


async def _fetch_calendar_day(day: str) -> dict[str, Any] | None:
    """Real Alpaca trading-calendar entry for `day` (YYYY-MM-DD), or None
    if `day` is not a trading day at all (weekend or market holiday).
    `/v2/calendar` only ever lists genuine trading days in the requested
    range -- an empty result IS the "not a trading day" answer, not an
    error to work around.

    2026-08-29 correctness pass #4 (item 3): this is what lets the
    backtest harness tell a real holiday apart from a data OUTAGE on a
    genuine trading day -- previously `if not bars:` treated both
    identically as "skip this day," silently hiding an outage as a
    no-op instead of failing the cycle. Also the source of a real half
    session's actual close time (item 4) -- the returned `close` is the
    genuine early close (e.g. "13:00" the day after Thanksgiving), never
    assumed to be 16:00.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/v2/calendar", params={"start": day, "end": day},
            headers=_headers(), timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
    return rows[0] if rows else None


@mcp.tool()
async def get_account() -> dict[str, Any]:
    """
    Real account state — equity, cash, exposure, and the P&L figures every
    RULES.md limit is measured against. Call this before sizing any position
    or evaluating any circuit breaker.

    Percentages are computed here rather than left as raw numbers for you to
    divide: these gate safety rules, and an arithmetic slip on a drawdown
    check is a silent failure of the exact control meant to stop a bad run.

    Returns:
      equity                  — the ONLY number to base position sizing and
                                every % cap on (RULES.md §2, §3).
      cash                    — settled cash available.
      exposure_pct            — invested % of equity, vs §3's cap.
      daily_pl_pct            — vs previous close, for §4's daily breaker.
      weekly_pl_pct           — vs this week's opening equity (§4 weekly).
      drawdown_from_peak_pct  — vs highest equity ever (§4 total). Never
                                positive; 0.0 means at a new high.
      trading_blocked, account_blocked, status — real broker-side blocks.

    Any of the three P&L figures may be null when there is not enough
    account history to compute it honestly. **Treat null as blocking, not as
    "clear."** An unknown safety state must never be read as permission --
    the whole point of a circuit breaker is that it fails closed.

    NOTE: `buying_power` is deliberately NOT returned. Alpaca reports ~4x
    equity on a margin-enabled paper account, and RULES.md §1 forbids margin
    outright; sizing off that number would silently take 4x leverage while
    appearing to follow the rules. Size off `equity`.
    """
    return await _fetch_account()


@mcp.tool()
async def get_positions() -> list[dict[str, Any]]:
    """
    Real broker-side open positions — the authoritative source for
    portfolio state per §6c (not trade-journal's own bookkeeping, which
    could drift from reality if a bug exists). Returns symbol, qty, side,
    market_value, avg_entry_price, current_price, unrealized_pl per
    position.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        resp.raise_for_status()
        positions = resp.json()

    return [
        {
            "symbol": p["symbol"],
            "qty": float(p["qty"]),
            "side": p["side"],
            "market_value": float(p["market_value"]),
            "avg_entry_price": float(p["avg_entry_price"]),
            "current_price": float(p["current_price"]),
            "unrealized_pl": float(p["unrealized_pl"]),
        }
        for p in positions
    ]


@mcp.tool()
async def get_order_status(decision_id: str) -> dict[str, Any]:
    """
    Look up a real order by decision_id (Alpaca's client_order_id) — for
    §6b's restart-reconciliation flow: after any restart, check real
    broker state for every trade-journal row still marked 'pending'
    before doing anything else.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/v2/orders:by_client_order_id",
            params={"client_order_id": decision_id},
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        order = resp.json()

    fill_price, fill_quantity = _parse_fill(order)
    return {
        "status": _to_journal_status(order["status"]),
        "alpaca_status": order["status"],
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "broker_order_id": order["id"],
    }


@mcp.tool()
async def get_historical_news(
    ticker: str, as_of: str, lookback_days: int = 7
) -> list[dict[str, Any]]:
    """
    Point-in-time historical headlines for `ticker` -- the news half of the
    backtest harness's no-future-data gate (the other half is maverick-mcp's
    as_of param on its technical tools). Alpaca's News API is Benzinga-
    sourced and covers back to 2015 using this same account/credentials --
    no separate news source needed for backtesting.

    as_of (YYYY-MM-DD): the simulated "now" for this backtest cycle. Only
    headlines published on or before this date are ever returned -- nothing
    after it can leak into a decision about this date. lookback_days: how
    far back from as_of to search (default 7, matching a typical decision
    cycle's news window). This tool is backtest-only; live cycles should
    keep using google-news-rss-mcp, not this one, since Alpaca's News API
    has its own separate rate limits we haven't load-tested for live use.
    """
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=lookback_days)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DATA_URL}/v1beta1/news",
            params={
                "symbols": ticker.upper(),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 50,
                "sort": "desc",
            },
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("news", [])

    return [
        {
            "headline": a["headline"],
            "summary": a.get("summary", ""),
            "created_at": a["created_at"],
            "symbols": a.get("symbols", []),
            "source_url": a.get("url", ""),
        }
        for a in articles
    ]


@mcp.tool()
async def get_intraday_bars(ticker: str, day: str) -> list[dict[str, Any]]:
    """
    Real historical 15-minute REGULAR-SESSION bars for `ticker` on `day`
    (YYYY-MM-DD), from Alpaca's market-data API -- what the backtest
    harness's intraday no-lookahead execution timing (RULES.md §7's 9:45
    ET entry floor, and chronological stop/target resolution) needs. A
    single daily OHLC bar cannot represent real sub-day price movement or
    identify a specific ET timestamp; this can. Full-window coverage for
    the whole watchlist was verified before this was built for backtest
    use (see HANDOFF.md) -- this is not an unverified new data path.

    Uses this account's EXISTING market-data credentials (same
    ALPACA_API_KEY/SECRET as place_order/get_account) -- no new provider,
    no new dependency (zoneinfo is stdlib).

    Returns bars sorted chronologically, REGULAR session only (09:30-16:00
    ET -- pre/post-market bars dropped), each as
    {"time": "HH:MM" (ET wall-clock, e.g. "09:45"), "open", "high",
    "low", "close"}. `time` is a plain ET label, not a UTC timestamp --
    callers should never need to re-derive timezone/DST themselves.
    """
    async with httpx.AsyncClient() as client:
        all_bars: list[dict[str, Any]] = []
        params = {
            "symbols": ticker.upper(), "timeframe": "15Min",
            "start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z",
            "limit": 10000, "adjustment": "raw",
        }
        while True:
            resp = await client.get(f"{DATA_URL}/v2/stocks/bars", params=params,
                                    headers=_headers(), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            all_bars.extend(data.get("bars", {}).get(ticker.upper(), []))
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token

    out = []
    for b in all_bars:
        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(_ET)
        if ts.date().isoformat() != day:
            continue  # a UTC-day query can spill one bar into an adjacent ET date
        if not (_REGULAR_SESSION_START <= ts.time() < _REGULAR_SESSION_END):
            continue  # extended-hours bars dropped -- regular session only
        out.append({
            "time": ts.strftime("%H:%M"), "open": b["o"], "high": b["h"],
            "low": b["l"], "close": b["c"],
        })
    out.sort(key=lambda b: b["time"])
    return out


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
