"""
laya_cards.py -- point-in-time numeric card inputs for the experimental
fixed-30 Laya selector (docs/scopes/LAYA_SELECTOR_V1.md). Pure: no I/O, no
Docker, no model. The only I/O is the one container script this module builds
as a string (history_script), which run_backtest.py executes through its
existing `run_in("maverick-mcp", ...)` path -- the same price source
fetch_technicals already reads (maverick's MarketDataService, raw
unadjusted bars).

Input: daily OHLCV bars per symbol. Output: the seven numeric card inputs
(build_card derives the eighth field, data_complete). Point-in-time gate:
ONLY bars dated on or before `as_of` are ever used -- later bars are dropped
here no matter what the fetch returned -- and the latest used bar's date is
reported so the selector can mark a card whose as-of bar is missing as stale.

Formulas (the card's "normalized MACD direction" and "relative volume" are
defined here; they are experiment parameters, recorded in the scope doc):
  five_day_return     close[t] / close[t-5] - 1
  sma20_distance      close[t] / mean(close[t-19..t]) - 1
  rsi14               Wilder RSI(14) over the whole used history, 0..100
  macd_direction      tanh(hist / close[t] / 0.005), hist = MACD(12,26) -
                      EMA9(MACD) at t; in (-1, 1), sign = momentum direction
  relative_volume     volume[t] / mean(volume[t-20..t-1])  (20 PRIOR days)
  realized_volatility sample stdev of the last 20 daily log returns * sqrt(252)
  range_position_20d  (close[t] - min(low, 20d)) / (max(high, 20d) - min(low, 20d))
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from laya_selector import NUMERIC_FIELDS, CandidateCard, InvalidCardValue, build_card

MIN_BARS = 40            # MACD(12,26,9) + RSI warm-up + the 21-bar volume baseline
HISTORY_CALENDAR_DAYS = 120
MACD_SCALE = 0.005       # histogram as a fraction of price that maps to tanh(1)
_REQUIRED_BAR_KEYS = ("date", "open", "high", "low", "close", "volume")


def _empty() -> dict[str, float | None]:
    return {name: None for name in NUMERIC_FIELDS}


def _clean_bars(bars: Sequence[Mapping[str, Any]], as_of: date) -> list[dict[str, Any]]:
    """Point-in-time gate + sanitation: drop bars after `as_of`, bars with a
    missing/non-finite/non-positive price or negative volume, and duplicate
    dates (first wins); return oldest-first."""
    by_date: dict[date, dict[str, Any]] = {}
    for b in bars:
        try:
            d = b["date"] if isinstance(b["date"], date) else date.fromisoformat(str(b["date"])[:10])
            row = {k: float(b[k]) for k in ("open", "high", "low", "close", "volume")}
        except (KeyError, TypeError, ValueError):
            continue
        if d > as_of or d in by_date:
            continue
        if not all(math.isfinite(v) for v in row.values()):
            continue
        if min(row["open"], row["high"], row["low"], row["close"]) <= 0 or row["volume"] < 0:
            continue
        by_date[d] = {"date": d, **row}
    return [by_date[d] for d in sorted(by_date)]


def _ema(values: Sequence[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def _rsi14(closes: Sequence[float]) -> float | None:
    if len(closes) < 15:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14
    for g, l in zip(gains[14:], losses[14:]):
        avg_gain = (avg_gain * 13 + g) / 14
        avg_loss = (avg_loss * 13 + l) / 14
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def compute_card_inputs(
    bars: Sequence[Mapping[str, Any]], as_of: date
) -> tuple[dict[str, float | None], date | None]:
    """-> (seven numeric inputs, date of the latest bar used). Inputs are all
    None (an incomplete card) when fewer than MIN_BARS usable bars exist on or
    before `as_of`, or any window is degenerate. The latest-bar date is
    reported even for an incomplete result so freshness stays auditable."""
    clean = _clean_bars(bars, as_of)
    latest = clean[-1]["date"] if clean else None
    if len(clean) < MIN_BARS:
        return _empty(), latest
    closes = [b["close"] for b in clean]
    highs = [b["high"] for b in clean]
    lows = [b["low"] for b in clean]
    vols = [b["volume"] for b in clean]
    c = closes[-1]

    sma20 = sum(closes[-20:]) / 20
    prior_vol = sum(vols[-21:-1]) / 20
    hi, lo = max(highs[-20:]), min(lows[-20:])
    rsi = _rsi14(closes)
    if prior_vol <= 0 or hi <= lo or rsi is None:
        return _empty(), latest

    macd = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    hist = macd[-1] - _ema(macd, 9)[-1]
    rets = [math.log(b / a) for a, b in zip(closes[-21:], closes[-20:])]
    mean_r = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)

    return {
        "five_day_return": c / closes[-6] - 1.0,
        "sma20_distance": c / sma20 - 1.0,
        "rsi14": rsi,
        "macd_direction": math.tanh(hist / c / MACD_SCALE),
        "relative_volume": vols[-1] / prior_vol,
        "realized_volatility": vol,
        "range_position_20d": min(1.0, max(0.0, (c - lo) / (hi - lo))),
    }, latest


def build_day_cards(
    history: Mapping[str, Any], symbols: Sequence[str], as_of: date,
    row_ids: Mapping[str, str],
) -> tuple[list[CandidateCard], dict[str, date], dict[str, str]]:
    """One card per symbol (in `symbols` order) from `history` ({symbol: list
    of bars | {"error": ...} | missing}). Returns (cards, latest_bar_dates
    keyed by row_id for symbols that have any bar, unavailable_reasons keyed
    by row_id for cards that came out incomplete). A symbol whose history is
    missing/errored simply yields an incomplete card -- data unavailable,
    never a technical rejection."""
    cards: list[CandidateCard] = []
    latest_dates: dict[str, date] = {}
    reasons: dict[str, str] = {}
    for sym in symbols:
        rid = row_ids[sym]
        raw = history.get(sym)
        if not isinstance(raw, (list, tuple)):
            cards.append(build_card(rid, {}))
            reasons[rid] = "history_fetch_error" if isinstance(raw, Mapping) else "history_missing"
            continue
        inputs, latest = compute_card_inputs(raw, as_of)
        try:
            card = build_card(rid, inputs)
        except InvalidCardValue:
            card = build_card(rid, {})
            reasons[rid] = "invalid_value"
        else:
            if not card.data_complete:
                reasons[rid] = "insufficient_or_degenerate_history"
        if latest is not None:
            latest_dates[rid] = latest
        cards.append(card)
    return cards, latest_dates, reasons


def history_script(tickers: Sequence[str], as_of: date, start: date | None = None) -> str:
    """Source of the one container script (run in maverick-mcp) returning
    daily OHLCV for every ticker from `start` (default: HISTORY_CALENDAR_DAYS
    before) through `as_of` inclusive -- the maverick smart-cache price source
    fetch_technicals already uses (raw, unadjusted). Per-ticker resilient: one
    failing ticker becomes {"error": ...} without losing the others."""
    start = start or as_of - timedelta(days=HISTORY_CALENDAR_DAYS)
    return f'''
import asyncio, json
from datetime import date
from maverick.platform.config import get_platform_settings
from maverick.platform.db import create_engine_from_settings
from maverick.platform.cache import Cache
from maverick.market_data.fetchers import YFinanceFetcher, build_mover_fetcher
from maverick.market_data.config import get_market_data_settings
from maverick.market_data.service import MarketDataService

async def main():
    s = get_platform_settings()
    yf = YFinanceFetcher(http_settings=s.http)
    md = MarketDataService(create_engine_from_settings(s.database),
                           Cache(s.cache, redis_settings=s.redis), yf,
                           build_mover_fetcher(get_market_data_settings(), yf))
    start, end = date.fromisoformat("{start}"), date.fromisoformat("{as_of}")
    out = {{}}
    for t in {list(tickers)!r}:
        try:
            df = await md.get_price_history(t, start, end)
            out[t] = [
                {{"date": str(idx.date()), "open": float(r["Open"]), "high": float(r["High"]),
                  "low": float(r["Low"]), "close": float(r["Close"]), "volume": float(r["Volume"])}}
                for idx, r in df.iterrows()
            ]
        except Exception as e:
            out[t] = {{"error": str(e)[:120]}}
    print(json.dumps({{"history": out}}))

asyncio.run(main())
'''
