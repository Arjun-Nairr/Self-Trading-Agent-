"""
laya_preflight.py -- read-only coverage preflight for the experimental
fixed-30 Laya selector universe. Reports PASS/FAIL per symbol for:

  * price history: a daily bar on every expected trading day of
    [--start, --end] (an expected day = a day most of the 30 names traded,
    so a holiday is never held against one symbol), at least laya_cards.MIN_BARS
    usable bars up to the first simulated day (card warm-up), and no
    malformed bars (non-positive price, negative volume);
  * news coverage: the project's existing criterion
    (watchlist/check_news_coverage.py) -- at least 6 DISTINCT articles across
    three non-overlapping 14-day probes, all ending before the first
    simulated day (rule NEWS_RULE_VERSION), with complete pagination.

It NEVER alters a run: no state file, checkpoint, Neon write, Hermes call,
order, or snapshot is touched. It reads market data through the same two
containers a run uses (maverick-mcp prices, alpaca-mcp news); maverick's own
price cache may warm as a side effect of those normal reads. The evaluators
are pure and deterministic -- tested without Docker.

    python backtest-harness/laya_preflight.py --start 2026-02-17 --end 2026-08-14

Exit code: 0 all symbols pass, 1 at least one fails, 2 the containers could
not be reached (Docker not running, etc.) -- nothing was verified.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence

from laya_cards import MIN_BARS, _clean_bars
from laya_selector import FixedUniverse, load_universe

NEWS_LOOKBACK_DAYS = 14
MIN_NEWS_ARTICLES = 6
EXPECTED_DAY_FRACTION = 0.5
PRICE_HISTORY_DAYS_BEFORE_START = 120
# v2 (2026-10-03). The criterion is unchanged -- the established RULES.md §8 /
# watchlist/check_news_coverage.py rule: at least 6 articles across three
# 14-day lookback probes spread over time. v1 placed the probes at
# (start+14d capped at end, midpoint, end): on a window shorter than 14 days
# two probes were the SAME window and the third overlapped them, so one
# article was counted two or three times, and every probe used news from
# inside the episode being qualified. v2 ends all three probes BEFORE the
# first simulated day, spaces them NEWS_PROBE_SPACING_DAYS apart (never
# overlapping), and counts DISTINCT articles (created_at + headline; the
# provider passes no article id through).
NEWS_RULE_VERSION = "pre-start-3x14d-distinct-v2"
NEWS_PROBE_SPACING_DAYS = 45


def news_probe_windows(start: date) -> tuple[tuple[datetime, datetime], ...]:
    """Three (start_at, end_at] UTC windows of NEWS_LOOKBACK_DAYS each, the
    newest ending at 23:59:59 UTC on the day BEFORE `start` and the others
    NEWS_PROBE_SPACING_DAYS earlier each -- oldest first, non-overlapping,
    and entirely before the first simulated decision."""
    newest_end = datetime.combine(start - timedelta(days=1), time(23, 59, 59), tzinfo=timezone.utc)
    ends = [newest_end - timedelta(days=NEWS_PROBE_SPACING_DAYS * k) for k in (2, 1, 0)]
    return tuple((e - timedelta(days=NEWS_LOOKBACK_DAYS), e) for e in ends)


def _article_key(a: Any) -> tuple[str, str]:
    return (str(a.get("created_at", "")), str(a.get("headline", ""))) if isinstance(a, Mapping) else ("", repr(a))


def evaluate_price_coverage(
    history: Mapping[str, Any], symbols: Sequence[str], start: date, end: date,
) -> dict[str, dict[str, Any]]:
    """-> {symbol: {"pass", "bars_in_window", "missing_days", "warmup_bars",
    "bad_bars", "reasons"}}. Pure."""
    usable: dict[str, set[date]] = {}
    for sym in symbols:
        raw = history.get(sym)
        if isinstance(raw, (list, tuple)):
            usable[sym] = {b["date"] for b in _clean_bars(raw, end)}
    votes: dict[date, int] = {}
    for dates in usable.values():
        for d in dates:
            if start <= d <= end:
                votes[d] = votes.get(d, 0) + 1
    need = max(1, int(len(symbols) * EXPECTED_DAY_FRACTION + 0.999))
    expected = sorted(d for d, n in votes.items() if n >= need)

    out: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        raw = history.get(sym)
        reasons: list[str] = []
        if not isinstance(raw, (list, tuple)):
            reason = "history_fetch_error" if isinstance(raw, Mapping) else "history_missing"
            out[sym] = {"pass": False, "bars_in_window": 0, "missing_days": None,
                        "warmup_bars": 0, "bad_bars": None, "reasons": [reason]}
            continue
        have = usable[sym]
        bad = len(raw) - len(_clean_bars(raw, end))
        missing = [d for d in expected if d not in have]
        first_day = expected[0] if expected else start
        warmup = sum(1 for d in have if d <= first_day)
        if not expected:
            reasons.append("no_expected_trading_days_in_window")
        if missing:
            reasons.append(f"missing_{len(missing)}_trading_days")
        if warmup < MIN_BARS:
            reasons.append(f"warmup_{warmup}_of_{MIN_BARS}_bars")
        if bad:
            reasons.append(f"{bad}_malformed_bars")
        out[sym] = {
            "pass": not reasons, "bars_in_window": sum(1 for d in have if start <= d <= end),
            "missing_days": len(missing), "warmup_bars": warmup, "bad_bars": bad,
            "reasons": reasons,
        }
    return out


def evaluate_news_coverage(
    probes: Sequence[Mapping[str, Any]], symbols: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """`probes`: one {symbol: {"articles": [...]} | {"error": ...}} mapping per
    probe window (the fetch_news_window result shape). -> {symbol: {"pass",
    "counts", "total", "reasons"}}. `counts` are per-probe DISTINCT
    articles; `total` is distinct across all probes (created_at + headline),
    so an article seen by two probes counts once. A fetch error / incomplete
    pagination in any probe fails the symbol (coverage is unproven, not
    zero). Pure."""
    out: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        counts: list[int | None] = []
        reasons: list[str] = []
        seen: set[tuple[str, str]] = set()
        for i, probe in enumerate(probes):
            entry = probe.get(sym)
            if not isinstance(entry, Mapping) or "articles" not in entry:
                counts.append(None)
                reasons.append(f"probe_{i + 1}_unavailable")
            else:
                keys = {_article_key(a) for a in entry["articles"]}
                counts.append(len(keys))
                seen |= keys
        total = len(seen)
        if total < MIN_NEWS_ARTICLES:
            reasons.append(f"only_{total}_articles_need_{MIN_NEWS_ARTICLES}")
        out[sym] = {"pass": not reasons, "counts": counts, "total": total, "reasons": reasons}
    return out


def build_report(
    universe: FixedUniverse, price: Mapping[str, Mapping[str, Any]],
    news: Mapping[str, Mapping[str, Any]], start: date, end: date,
) -> tuple[str, bool]:
    lines = [
        f"Laya fixed-30 coverage preflight -- universe {universe.version} "
        f"({universe.hash[:12]}), window {start} .. {end}",
        f"news rule {NEWS_RULE_VERSION}: >= {MIN_NEWS_ARTICLES} distinct articles across "
        + ", ".join(f"({a:%Y-%m-%d %H:%M}, {b:%Y-%m-%d %H:%M}]" for a, b in news_probe_windows(start))
        + " UTC -- all before the first simulated day",
        f"{'SYMBOL':<7}{'PRICE':<6}{'bars':>6}{'miss':>6}{'warm':>6}   "
        f"{'NEWS':<6}{'total':>6}   notes",
        "-" * 78,
    ]
    all_pass = True
    for m in universe.members:
        p, n = price[m.symbol], news[m.symbol]
        ok = p["pass"] and n["pass"]
        all_pass &= ok
        lines.append(
            f"{m.symbol:<7}{'pass' if p['pass'] else 'FAIL':<6}{p['bars_in_window']:>6}"
            f"{'-' if p['missing_days'] is None else p['missing_days']:>6}{p['warmup_bars']:>6}   "
            f"{'pass' if n['pass'] else 'FAIL':<6}{n['total']:>6}   "
            + ", ".join(p["reasons"] + n["reasons"])
        )
    failing = [m.symbol for m in universe.members
               if not (price[m.symbol]["pass"] and news[m.symbol]["pass"])]
    lines.append("")
    lines.append("PASS: all 30 symbols" if all_pass else f"FAIL: {', '.join(failing)}")
    return "\n".join(lines), all_pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--start", default="2026-02-17")
    parser.add_argument("--end", default="2026-08-14")
    args = parser.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    universe = load_universe()
    symbols = list(universe.symbols)

    import run_backtest as rb  # real I/O helpers: run_in / json_from / fetch_news_window
    from laya_cards import history_script

    try:
        history = rb.json_from(rb.run_in(
            "maverick-mcp",
            history_script(symbols, end, start - timedelta(days=PRICE_HISTORY_DAYS_BEFORE_START)),
        ))["history"]
        probes = [rb.fetch_news_window(symbols, start_at, end_at)
                  for start_at, end_at in news_probe_windows(start)]
    except Exception as exc:
        print(f"PREFLIGHT COULD NOT RUN -- nothing was verified: {type(exc).__name__}: "
              f"{str(exc)[:300]}", file=sys.stderr)
        return 2

    report, ok = build_report(
        universe, evaluate_price_coverage(history, symbols, start, end),
        evaluate_news_coverage(probes, symbols), start, end)
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
