"""
laya_preflight.py -- read-only coverage preflight for an experimental Laya
selector universe (default watchlist/experimental_fixed30.json; any universe
config via --universe), and the freezer of coverage-qualified pools.
Reports PASS/FAIL per symbol for:

  * price, POINT-IN-TIME part: at least laya_cards.MIN_BARS usable bars up
    to the first simulated day (card warm-up);
  * price, RETROSPECTIVE availability part (operational feasibility only,
    NOT a point-in-time selection signal): a daily bar on every expected
    trading day of [--start, --end] (an expected day = a day at least half
    the names traded, so a holiday is never held against one symbol), and no
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
    python backtest-harness/laya_preflight.py --universe watchlist/experimental_candidates60.json \
        --start 2026-09-28 --end 2026-10-02 --freeze

--freeze writes a write-once frozen pool (watchlist/experimental_pools/) of
exactly the names that passed -- a failing name is excluded, it does not block
the others -- with every candidate's evidence, the coverage policy, window,
parent-config hash, and evidence/content hashes. It refuses (writes nothing)
when fewer than MIN_POOL names pass or when fetch failures hit more than
PROVIDER_FAILURE_FRACTION of candidates (a provider-wide failure or
incomplete verification is never a successful pool). load_frozen_pool()
re-verifies a pool before any run uses it; a run never re-screens.

Exit code: without --freeze 0 all symbols pass, 1 at least one fails; with
--freeze 0 a pool was frozen, 1 it was refused; 2 the containers could not be
reached (Docker not running, etc.) -- nothing was verified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from laya_cards import MIN_BARS, _clean_bars
from laya_selector import REPO, UNIVERSE_PATH, FixedUniverse, load_universe

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


# Frozen coverage-qualified pools (2026-10-03).
POOL_SCHEMA_VERSION = 1
POOL_DIR = REPO / "watchlist" / "experimental_pools"
MIN_POOL = 20
PROVIDER_FAILURE_FRACTION = 0.10
_FETCH_FAILURE_REASONS = ("history_fetch_error", "history_missing", "_unavailable",
                          "no_expected_trading_days_in_window")
COVERAGE_POLICY = {
    "news_rule": NEWS_RULE_VERSION, "min_news_articles": MIN_NEWS_ARTICLES,
    "news_lookback_days": NEWS_LOOKBACK_DAYS, "news_probe_spacing_days": NEWS_PROBE_SPACING_DAYS,
    "news_count": "distinct (created_at, headline)", "complete_pagination_required": True,
    "price_point_in_time": f">= {MIN_BARS} usable bars on or before the first expected session",
    "price_retrospective": ("a bar on every expected session of the window and no malformed bars -- "
                            "operational feasibility only, not a point-in-time selection signal"),
    "expected_day_fraction": EXPECTED_DAY_FRACTION, "min_pool": MIN_POOL,
    "max_fetch_failure_fraction": PROVIDER_FAILURE_FRACTION,
}


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
        f"Laya coverage preflight -- universe {universe.version} "
        f"({len(universe.members)} names, {universe.hash[:12]}), window {start} .. {end}",
        f"news rule {NEWS_RULE_VERSION}: >= {MIN_NEWS_ARTICLES} distinct articles across "
        + ", ".join(f"({a:%Y-%m-%d %H:%M}, {b:%Y-%m-%d %H:%M}]" for a, b in news_probe_windows(start))
        + " UTC -- all before the first simulated day",
        "price: warm = point-in-time warm-up bars before day 1; bars/miss = RETROSPECTIVE in-window availability",
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
    lines.append(f"PASS: all {len(universe.members)} symbols" if all_pass
                 else f"FAIL ({len(failing)}): {', '.join(failing)}")
    return "\n".join(lines), all_pass


# -- frozen coverage-qualified pool -----------------------------------------

class PoolRefused(RuntimeError):
    """Coverage results that must not become a pool (too few names, or a
    provider-wide fetch failure / incomplete verification)."""


class PoolInvalid(ValueError):
    """A frozen pool that fails re-verification (tampered, conflicting parent
    configuration or policy, or internally inconsistent)."""


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def _rel(path: Path) -> str:
    path = Path(path).resolve()
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def _member_entry(m: Any) -> dict[str, Any]:
    e = {"symbol": m.symbol, "sector": m.sector, "name": m.name, "group": m.group}
    if m.brands:
        e["brands"] = list(m.brands)
    return e


def _evidence_body(q: Mapping[str, Any]) -> dict[str, Any]:
    return {k: q[k] for k in ("parent", "policy", "window", "news_probe_windows", "candidates")}


def qualify(universe: FixedUniverse, price: Mapping[str, Mapping[str, Any]],
            news: Mapping[str, Mapping[str, Any]], start: date, end: date) -> dict[str, dict[str, Any]]:
    """-> {symbol: {"status": qualified|excluded, "reasons", "price", "news"}}
    for EVERY candidate, in universe order. Pure."""
    out = {}
    for m in universe.members:
        p, n = price[m.symbol], news[m.symbol]
        reasons = list(p["reasons"]) + list(n["reasons"])
        out[m.symbol] = {
            "status": "qualified" if not reasons else "excluded", "reasons": reasons,
            "price": {"warmup_bars_point_in_time": p["warmup_bars"],
                      "retrospective_bars_in_window": p["bars_in_window"],
                      "retrospective_missing_days": p["missing_days"], "malformed_bars": p["bad_bars"]},
            "news": {"distinct_per_probe": n["counts"], "distinct_total": n["total"]},
        }
    return out


def build_pool(universe: FixedUniverse, parent_path: Path, candidates: Mapping[str, Mapping[str, Any]],
               start: date, end: date, *, checked_at: datetime) -> dict[str, Any]:
    """The frozen pool artifact (a load_universe-compatible config plus a
    `qualification` block). Raises PoolRefused instead of returning a pool
    that is too small or rests on incomplete verification. Pure."""
    n = len(candidates)
    failed_fetch = sorted(s for s, c in candidates.items()
                          if any(f in r for r in c["reasons"] for f in _FETCH_FAILURE_REASONS))
    if len(failed_fetch) > PROVIDER_FAILURE_FRACTION * n:
        raise PoolRefused(f"{len(failed_fetch)}/{n} candidates have fetch failures or unverifiable "
                          f"coverage ({', '.join(failed_fetch)}) -- above {PROVIDER_FAILURE_FRACTION:.0%}; "
                          "treating this as a provider-wide failure, not a pool")
    members = [m for m in universe.members if candidates[m.symbol]["status"] == "qualified"]
    if len(members) < MIN_POOL:
        raise PoolRefused(f"only {len(members)} of {n} candidates qualified; a pool needs >= {MIN_POOL}")
    q = {
        "pool_schema_version": POOL_SCHEMA_VERSION,
        "parent": {"path": _rel(parent_path), "version": universe.version, "universe_hash": universe.hash,
                   "count": n},
        "policy": dict(COVERAGE_POLICY),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "news_probe_windows": [[a.isoformat(), b.isoformat()] for a, b in news_probe_windows(start)],
        "candidates": {s: dict(c) for s, c in candidates.items()},
        "checked_at_utc": checked_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "sources": {"price": "maverick-mcp MarketDataService (laya_cards.history_script)",
                    "news": "alpaca-mcp get_historical_news (run_backtest.fetch_news_window)"},
    }
    q["evidence_sha256"] = _sha(_evidence_body(q))
    member_set = {m.symbol for m in members}
    pool = {
        "_comment": ("FROZEN coverage-qualified pool -- generated by laya_preflight.py --freeze, never "
                     "edited by hand. Exactly the parent candidates that passed coverage for this window; "
                     "the only experimental universe a run may use (run_backtest.py --selector-universe)."),
        "version": f"{universe.version}-pool-{start.isoformat()}-{q['evidence_sha256'][:12]}",
        "experimental": True,
        "expected_count": len(members),
        "word_like_tickers": [t for t in universe.word_like_tickers if t in member_set],
        "tickers": [_member_entry(m) for m in members],
        "qualification": q,
    }
    pool["content_sha256"] = _sha({k: v for k, v in pool.items() if k != "content_sha256"})
    return pool


def write_pool(pool: Mapping[str, Any], out_dir: Path = POOL_DIR) -> Path:
    """Write-once (temp file + os.link): never overwrites an existing pool."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{pool['version']}.json"
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".pool_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(pool, indent=1, sort_keys=True, ensure_ascii=True) + "\n")
        os.link(tmp, path)
    finally:
        os.unlink(tmp)
    return path


def load_frozen_pool(path: Path) -> tuple[FixedUniverse, dict[str, Any]]:
    """Re-verify a frozen pool and return (its FixedUniverse, the artifact).
    Raises PoolInvalid on tampering (content or evidence hash), a parent
    config whose hash no longer matches, a policy different from this code's,
    membership that is not exactly the parent's qualified candidates (in
    parent order, with identical entries), or fewer than MIN_POOL members.
    Never fetches or re-screens anything."""
    path = Path(path)
    try:
        pool = json.loads(path.read_text(encoding="utf-8"))
        q = pool["qualification"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PoolInvalid(f"{path}: not a frozen pool ({type(exc).__name__}: {exc})") from None
    if q.get("pool_schema_version") != POOL_SCHEMA_VERSION:
        raise PoolInvalid(f"{path}: unsupported pool_schema_version {q.get('pool_schema_version')!r}")
    if _sha({k: v for k, v in pool.items() if k != "content_sha256"}) != pool.get("content_sha256"):
        raise PoolInvalid(f"{path}: content_sha256 mismatch -- the pool was altered")
    if _sha(_evidence_body(q)) != q.get("evidence_sha256"):
        raise PoolInvalid(f"{path}: evidence_sha256 mismatch -- the eligibility evidence was altered")
    if q["policy"] != COVERAGE_POLICY:
        raise PoolInvalid(f"{path}: frozen under a different coverage policy than this code's")
    parent_path = REPO / q["parent"]["path"] if not Path(q["parent"]["path"]).is_absolute() else Path(q["parent"]["path"])
    parent = load_universe(parent_path)
    if parent.hash != q["parent"]["universe_hash"] or parent.version != q["parent"]["version"]:
        raise PoolInvalid(f"{path}: parent {q['parent']['path']} changed since the pool was frozen")
    cands = q["candidates"]
    if sorted(cands) != sorted(parent.symbols):  # key order is not significant (sorted on disk)
        raise PoolInvalid(f"{path}: candidate records do not cover exactly the parent's names")
    want = [_member_entry(m) for m in parent.members if cands[m.symbol]["status"] == "qualified"]
    if pool["tickers"] != want:
        raise PoolInvalid(f"{path}: members are not exactly the parent's qualified candidates")
    for s, c in cands.items():
        if (c["status"] == "qualified") == bool(c["reasons"]) or c["status"] not in ("qualified", "excluded"):
            raise PoolInvalid(f"{path}: candidate {s} status disagrees with its reasons")
    wl = [t for t in parent.word_like_tickers if t in {e["symbol"] for e in want}]
    if pool["word_like_tickers"] != wl:
        raise PoolInvalid(f"{path}: word_like_tickers disagree with the parent")
    if len(want) < q["policy"]["min_pool"]:
        raise PoolInvalid(f"{path}: {len(want)} members < min_pool {q['policy']['min_pool']}")
    return load_universe(path), pool


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--start", default="2026-02-17")
    parser.add_argument("--end", default="2026-08-14")
    parser.add_argument("--universe", default=str(UNIVERSE_PATH),
                        help="universe config to check (default watchlist/experimental_fixed30.json)")
    parser.add_argument("--freeze", action="store_true",
                        help=f"write a frozen pool of the passing names (needs >= {MIN_POOL})")
    args = parser.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    universe_path = Path(args.universe)
    universe = load_universe(universe_path)
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

    price = evaluate_price_coverage(history, symbols, start, end)
    news = evaluate_news_coverage(probes, symbols)
    report, ok = build_report(universe, price, news, start, end)
    print(report)
    if not args.freeze:
        return 0 if ok else 1
    try:
        pool = build_pool(universe, universe_path, qualify(universe, price, news, start, end),
                          start, end, checked_at=datetime.now(timezone.utc))
    except PoolRefused as exc:
        print(f"\nPOOL REFUSED -- nothing written: {exc}")
        return 1
    path = write_pool(pool)
    load_frozen_pool(path)  # the written artifact must verify
    print(f"\nPOOL FROZEN: {pool['expected_count']} of {len(symbols)} names -> {_rel(path)}"
          f"\n  version {pool['version']}\n  evidence_sha256 {pool['qualification']['evidence_sha256']}"
          f"\n  content_sha256 {pool['content_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
