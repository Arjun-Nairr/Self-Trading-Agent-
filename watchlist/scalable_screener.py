"""
scalable_screener.py -- Stage 1 of docs/scopes/SCALABLE_SCREENER_V1.md.

A PARALLEL, live/current-universe screener. It does not replace, import, or
alter screen_watchlist.py, check_news_coverage.py, or watchlist.json (the
fixed 10-name path), and nothing in the backtest harness, the risk checker,
Hermes, or any Laya module imports it.

    current S&P MidCap 400 universe
      -> deterministic basic eligibility checks (RULES.md §8 thresholds)
      -> variable eligible candidate pool (no target size, no sector trim)
      -> immutable local screen snapshot

Every universe name gets exactly one status -- `eligible`, `rejected`, or
`data_unavailable` -- with ordered machine-readable reason codes. Checks run
in cost order (company facts, then history, then news) and a name stops at the
first stage that decides it, so the expensive fetches only happen for names
still in play.

CURRENT UNIVERSE ONLY. The universe source and the company facts are today's
values, not as-of values, so a snapshot says nothing valid about a past date
and must never feed a historical backtest (scope §5).

Explicit live dry-run (needs the maverick-mcp and alpaca-mcp containers; reads
only, writes nothing):

    python watchlist/scalable_screener.py --screen-date 2026-10-03 --dry-run

`--write [--output-dir DIR]` publishes the snapshot instead. There is no
scheduler.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

REPO = Path(__file__).resolve().parent.parent
# Runtime data, git-ignored -- same convention as backtest-harness/.selector_snapshots/.
SNAPSHOT_DIR = REPO / "watchlist" / ".screen_snapshots"

SCREENER_VERSION = "scalable-screener-v1-stage1"
SCHEMA_VERSION = 1

STATUS_ELIGIBLE = "eligible"
STATUS_REJECTED = "rejected"
STATUS_DATA_UNAVAILABLE = "data_unavailable"
SCREEN_OK = "ok"
SCREEN_FAILED = "failed"

UNIVERSE_SOURCE = "S&P MidCap 400 constituents (Wikipedia, current membership only)"
UNIVERSE_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"

# Thresholds mirror RULES.md §8 / screen_watchlist.py, unchanged. Same drift
# caveat as risk_checker.py: RULES.md is the source of truth.
DEFAULT_CONFIG: dict[str, Any] = {
    "min_market_cap": 2_000_000_000,
    "max_market_cap": 50_000_000_000,
    "min_avg_volume": 1_000_000,
    "max_avg_volume": 12_000_000,
    "min_price": 10.0,
    "excluded_industries": ["Biotechnology"],
    "min_history_bars": 750,                  # 3 years x 250 trading days
    "filter_order": ["facts", "history", "news"],
    # News probes trail the screen date: each window is `news_lookback_days`
    # long and ends at 00:00 UTC of (screen date - offset).
    "news_probe_offsets_days": [0, 90, 180],
    "news_lookback_days": 14,
    "min_news_articles": 6,
    "max_data_unavailable_fraction": 0.10,
    "cadence": "weekly_weekend_refresh_effective_following_trading_week",
    "refresh_interval_days": 7,
    "carry_forward_refreshes": 1,
}


class ScreenConflict(RuntimeError):
    """A DIFFERENT snapshot already exists for this screen ID. Snapshots are
    write-once: never overwritten."""


class SnapshotCorrupt(ValueError):
    """A stored snapshot failed its schema or content-hash check."""


class Providers(Protocol):
    """The four data boundaries. Each returns plain JSON-able data; raising
    means the whole stage could not be completed."""

    def universe(self) -> Mapping[str, Any]:
        """-> {"source", "url", "fetched_at", "symbols": [...]}"""

    def facts(self, symbols: Sequence[str]) -> Mapping[str, Any]:
        """-> {"fetched_at", "rows": {symbol: {"name", "sector", "industry",
        "market_cap", "avg_volume", "price"} | {"error": str}}}"""

    def history(self, symbols: Sequence[str]) -> Mapping[str, Any]:
        """-> {"fetched_at", "rows": {symbol: {"bars": int} | {"error": str}}}"""

    def news(self, symbols: Sequence[str], windows: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        """-> {"fetched_at", "rows": {symbol: [{"articles": int} | {"error": str},
        ... one per window, same order]}}"""


# --------------------------------------------------------------------------
# canonical form and hashes
# --------------------------------------------------------------------------

def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def config_hash(config: Mapping[str, Any]) -> str:
    return _hash(config)


def screen_id(screen_date: date, config: Mapping[str, Any]) -> str:
    """Deterministic from the screen date and config identity only."""
    return f"{screen_date.isoformat()}_{config_hash(config)[:12]}"


def snapshot_to_json(snap: Mapping[str, Any]) -> str:
    """Byte-stable: sorted keys, ASCII, no NaN, trailing newline."""
    return json.dumps(snap, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False) + "\n"


def _content_hash(snap: Mapping[str, Any]) -> str:
    return _hash({k: v for k, v in snap.items() if k != "content_hash"})


# --------------------------------------------------------------------------
# pure rules
# --------------------------------------------------------------------------

def news_windows(screen_date: date, config: Mapping[str, Any] = DEFAULT_CONFIG) -> list[dict[str, str]]:
    """Trailing probe windows derived from the screen date -- completed days
    only (each ends at 00:00 UTC of its anchor date)."""
    out = []
    for offset in config["news_probe_offsets_days"]:
        end = datetime.combine(screen_date - timedelta(days=offset), time(0), tzinfo=timezone.utc)
        start = end - timedelta(days=config["news_lookback_days"])
        out.append({"start_at": start.isoformat(), "end_at": end.isoformat()})
    return out


def effective_from(screen_date: date) -> date:
    """The Monday strictly after the screen date.
    ponytail: no market calendar -- a Monday holiday still counts as the start
    of the week; use the exchange calendar if exact first-session dates matter."""
    return screen_date + timedelta(days=7 - screen_date.weekday())


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def evaluate_facts(entry: Any, config: Mapping[str, Any]) -> tuple[dict[str, Any], str | None, list[str]]:
    """-> (sanitized raw inputs, deciding status or None to continue, reasons)."""
    if not isinstance(entry, Mapping) or "error" in entry:
        reason = "facts_fetch_error" if isinstance(entry, Mapping) else "facts_missing"
        return {}, STATUS_DATA_UNAVAILABLE, [reason]
    raw = {
        "name": _text(entry.get("name")), "sector": _text(entry.get("sector")),
        "industry": _text(entry.get("industry")), "market_cap": _num(entry.get("market_cap")),
        "avg_volume": _num(entry.get("avg_volume")), "price": _num(entry.get("price")),
    }
    missing = [f"{k}_missing" for k in ("market_cap", "avg_volume", "price", "industry") if raw[k] is None]
    if missing:
        return raw, STATUS_DATA_UNAVAILABLE, missing
    reasons = []
    if raw["market_cap"] < config["min_market_cap"]:
        reasons.append("market_cap_below_min")
    if raw["market_cap"] > config["max_market_cap"]:
        reasons.append("market_cap_above_max")
    if raw["avg_volume"] < config["min_avg_volume"]:
        reasons.append("avg_volume_below_min")
    if raw["avg_volume"] > config["max_avg_volume"]:
        reasons.append("avg_volume_above_max")
    if raw["price"] < config["min_price"]:
        reasons.append("price_below_min")
    if raw["industry"] in config["excluded_industries"]:
        reasons.append("industry_excluded")
    return raw, (STATUS_REJECTED if reasons else None), reasons


def evaluate_history(entry: Any, config: Mapping[str, Any]) -> tuple[int | None, str | None, list[str]]:
    if not isinstance(entry, Mapping) or "error" in entry:
        reason = "history_fetch_error" if isinstance(entry, Mapping) else "history_missing"
        return None, STATUS_DATA_UNAVAILABLE, [reason]
    bars = entry.get("bars")
    if isinstance(bars, bool) or not isinstance(bars, int) or bars < 0:
        return None, STATUS_DATA_UNAVAILABLE, ["history_missing"]
    if bars < config["min_history_bars"]:
        return bars, STATUS_REJECTED, ["history_below_min_bars"]
    return bars, None, []


def evaluate_news(entry: Any, n_windows: int, config: Mapping[str, Any]) -> tuple[list[int | None] | None, str, list[str]]:
    """A probe that errored is unproven coverage, not zero coverage."""
    if not isinstance(entry, (list, tuple)) or len(entry) != n_windows:
        reason = "news_fetch_error" if isinstance(entry, Mapping) else "news_missing"
        return None, STATUS_DATA_UNAVAILABLE, [reason]
    counts: list[int | None] = []
    reasons = []
    for i, probe in enumerate(entry, start=1):
        n = probe.get("articles") if isinstance(probe, Mapping) else None
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            counts.append(None)
            reasons.append(f"news_probe_{i}_unavailable")
        else:
            counts.append(n)
    if reasons:
        return counts, STATUS_DATA_UNAVAILABLE, reasons
    if sum(counts) < config["min_news_articles"]:
        return counts, STATUS_REJECTED, ["news_coverage_below_min"]
    return counts, STATUS_ELIGIBLE, []


# --------------------------------------------------------------------------
# the screen
# --------------------------------------------------------------------------

def run_screen(
    screen_date: date, providers: Providers, *,
    config: Mapping[str, Any] = DEFAULT_CONFIG, snapshot_dir: Path | None = None,
) -> dict[str, Any]:
    """Build one snapshot (ok or failed). Touches disk only to READ the
    previous successful screen reference from `snapshot_dir`. Identical
    provider outputs and directory state give an identical snapshot."""
    windows = news_windows(screen_date, config)
    failures: list[str] = []
    stamps: dict[str, str | None] = {"facts": None, "history": None, "news": None}
    universe = {"source": UNIVERSE_SOURCE, "url": UNIVERSE_URL, "fetched_at": None,
                "count": 0, "constituents_hash": None}
    symbols: list[str] = []
    try:
        u = providers.universe()
        symbols = sorted({s.strip() for s in u["symbols"] if isinstance(s, str) and s.strip()})
        universe = {"source": str(u["source"]), "url": str(u["url"]), "fetched_at": str(u["fetched_at"]),
                    "count": len(symbols), "constituents_hash": _hash(symbols)}
        if not symbols:
            failures.append("universe_empty")
    except Exception:
        failures.append("universe_fetch_failed")

    rows = {s: {"symbol": s, "name": None, "sector": None, "industry": None, "market_cap": None,
                "avg_volume": None, "price": None, "history_bars": None, "news_counts": None,
                "status": None, "reasons": []} for s in symbols}

    def stage(name: str, pending: list[str], fetch) -> dict[str, Any]:
        """Run one provider stage; a raise fails the screen and leaves every
        pending name data_unavailable (visible, never dropped)."""
        if not pending:
            return {}
        try:
            result = fetch(pending)
            stamps[name] = str(result["fetched_at"])
            return dict(result["rows"])
        except Exception:
            failures.append("news_coverage_incomplete" if name == "news" else f"{name}_provider_failed")
            return {s: {"error": "stage failed"} for s in pending}

    pending = list(symbols)
    got = stage("facts", pending, providers.facts)
    survivors = []
    for s in pending:
        raw, status, reasons = evaluate_facts(got.get(s), config)
        rows[s].update(raw)
        if status is None:
            survivors.append(s)
        else:
            rows[s].update(status=status, reasons=reasons)

    pending, survivors = survivors, []
    got = stage("history", pending, providers.history)
    for s in pending:
        bars, status, reasons = evaluate_history(got.get(s), config)
        rows[s]["history_bars"] = bars
        if status is None:
            survivors.append(s)
        else:
            rows[s].update(status=status, reasons=reasons)

    pending = survivors
    got = stage("news", pending, lambda syms: providers.news(syms, windows))
    for s in pending:
        counts, status, reasons = evaluate_news(got.get(s), len(windows), config)
        rows[s].update(news_counts=counts, status=status, reasons=reasons)

    counts = {k: sum(1 for r in rows.values() if r["status"] == k)
              for k in (STATUS_ELIGIBLE, STATUS_REJECTED, STATUS_DATA_UNAVAILABLE)}
    if symbols and counts[STATUS_DATA_UNAVAILABLE] / len(symbols) > config["max_data_unavailable_fraction"]:
        failures.append("data_unavailable_above_threshold")

    ok = not failures
    pool = sorted(s for s, r in rows.items() if r["status"] == STATUS_ELIGIBLE) if ok else None
    sector_mix: dict[str, int] = {}
    for s in pool or []:
        sector = rows[s]["sector"] or "Unknown"
        sector_mix[sector] = sector_mix.get(sector, 0) + 1

    previous = None
    if snapshot_dir is not None:
        earlier = [p for p in _ok_snapshots(snapshot_dir, config_hash(config))
                   if p["screen_date"] < screen_date.isoformat()]
        if earlier:
            last = max(earlier, key=lambda p: p["screen_date"])
            previous = {"screen_id": last["screen_id"], "content_hash": last["content_hash"]}

    snap = {
        "schema_version": SCHEMA_VERSION, "screener_version": SCREENER_VERSION,
        "screen_id": screen_id(screen_date, config), "screen_date": screen_date.isoformat(),
        "config": dict(config), "config_hash": config_hash(config),
        "universe": universe,
        "data": {"fetched_at": stamps, "news_windows": windows},
        "rows": [rows[s] for s in symbols],
        "counts": counts,
        "pool": pool, "pool_hash": _hash(pool) if ok else None, "sector_mix": sector_mix if ok else None,
        "screen_status": SCREEN_OK if ok else SCREEN_FAILED, "failure_reasons": failures,
        "effective_from": effective_from(screen_date).isoformat() if ok else None,
        "previous_ok_screen": previous,
    }
    snap["content_hash"] = _content_hash(snap)
    return snap


# --------------------------------------------------------------------------
# write-once persistence
# --------------------------------------------------------------------------

def snapshot_path(snapshot_dir: Path, sid: str) -> Path:
    return Path(snapshot_dir) / f"{sid}.json"


def load_snapshot(path: Path) -> dict[str, Any]:
    snap = json.loads(Path(path).read_text(encoding="utf-8"))
    if snap.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotCorrupt(f"{path}: unsupported schema_version {snap.get('schema_version')!r}")
    if snap.get("content_hash") != _content_hash(snap):
        raise SnapshotCorrupt(f"{path}: content hash mismatch -- the stored snapshot was altered")
    return snap


def write_snapshot(snapshot_dir: Path, snap: Mapping[str, Any]) -> Path:
    """Atomic and exclusive (temp file + os.link, which refuses to replace an
    existing file). An identical re-write is a no-op; a different snapshot for
    the same ID raises ScreenConflict and leaves the stored record untouched."""
    path = snapshot_path(snapshot_dir, snap["screen_id"])
    text = snapshot_to_json(snap)

    def existing() -> Path:
        if path.read_text(encoding="utf-8") == text:
            return path
        raise ScreenConflict(f"{path} already holds a different snapshot; screen snapshots are "
                             "write-once (use a later --screen-date to retry a failed screen)")

    if path.exists():
        return existing()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".screen_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return existing()
        return path
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def screen(
    screen_date: date, providers: Providers, snapshot_dir: Path, *,
    config: Mapping[str, Any] = DEFAULT_CONFIG,
) -> tuple[dict[str, Any], bool]:
    """Run and publish, or -- if this screen ID already has a record -- verify
    and return it WITHOUT fetching anything. -> (snapshot, reused)."""
    path = snapshot_path(snapshot_dir, screen_id(screen_date, config))
    if path.exists():
        return load_snapshot(path), True
    snap = run_screen(screen_date, providers, config=config, snapshot_dir=snapshot_dir)
    write_snapshot(snapshot_dir, snap)
    return snap, False


def _ok_snapshots(snapshot_dir: Path, cfg_hash: str) -> list[dict[str, Any]]:
    d = Path(snapshot_dir)
    if not d.is_dir():
        return []
    snaps = (load_snapshot(p) for p in sorted(d.glob("*.json")))
    return [s for s in snaps if s["screen_status"] == SCREEN_OK and s["config_hash"] == cfg_hash]


def usable_pool(
    snapshot_dir: Path, on_date: date, *, config: Mapping[str, Any] = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Which pool, if any, may be used for NEW entries on `on_date`: the latest
    successful screen already in effect. It is `current` for its own refresh
    interval, `carried_forward` for `carry_forward_refreshes` more intervals
    (a failed or missing refresh), and after that `unavailable`."""
    live = [s for s in _ok_snapshots(snapshot_dir, config_hash(config))
            if s["effective_from"] <= on_date.isoformat()]
    if not live:
        return {"state": "unavailable", "reason": "no_successful_screen", "screen_id": None, "pool": None}
    snap = max(live, key=lambda s: (s["effective_from"], s["screen_date"]))
    age = (on_date - date.fromisoformat(snap["effective_from"])).days
    missed = age // snap["config"]["refresh_interval_days"]
    if missed > snap["config"]["carry_forward_refreshes"]:
        return {"state": "unavailable", "reason": "last_successful_screen_expired",
                "screen_id": snap["screen_id"], "pool": None}
    return {"state": "current" if missed == 0 else "carried_forward", "reason": None,
            "screen_id": snap["screen_id"], "pool": list(snap["pool"])}


# --------------------------------------------------------------------------
# live providers (containers; never used by tests)
# --------------------------------------------------------------------------

_RUNNERS = {"maverick-mcp": ["uv", "run", "python", "-"], "alpaca-mcp": ["python", "-"]}
_MARK = "SCREENER_JSON:"


def _run_in(service: str, script: str, timeout: int = 3600) -> Any:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", service, *_RUNNERS[service]],
        input=script, capture_output=True, text=True, cwd=REPO, timeout=timeout, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"{service} failed:\n{result.stderr[-2000:]}")
    line = next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith(_MARK))
    return json.loads(line[len(_MARK):])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LiveProviders:
    """Current data via the existing containers: yfinance/Wikipedia inside
    maverick-mcp, Alpaca news inside alpaca-mcp. Read-only."""

    def universe(self) -> dict[str, Any]:
        symbols = _run_in("maverick-mcp", f'''
import json
from io import StringIO
import httpx, pandas as pd
r = httpx.get({UNIVERSE_URL!r}, headers={{"User-Agent": "Mozilla/5.0 (watchlist-screener)"}},
              timeout=30, follow_redirects=True)
r.raise_for_status()
for t in pd.read_html(StringIO(r.text)):
    col = next((c for c in t.columns if "Symbol" in str(c) or "Ticker" in str(c)), None)
    if col is not None:
        print({_MARK!r} + json.dumps([str(s).replace(".", "-").strip() for s in t[col]]))
        break
else:
    raise RuntimeError("no constituent table found")
''')
        return {"source": UNIVERSE_SOURCE, "url": UNIVERSE_URL, "fetched_at": _now(), "symbols": symbols}

    def facts(self, symbols: Sequence[str]) -> dict[str, Any]:
        rows = _run_in("maverick-mcp", f'''
import json
from concurrent.futures import ThreadPoolExecutor
import yfinance as yf
def one(s):
    try:
        i = yf.Ticker(s).info
        return s, {{"name": i.get("shortName"), "sector": i.get("sector"), "industry": i.get("industry"),
                   "market_cap": i.get("marketCap"), "avg_volume": i.get("averageVolume"),
                   "price": i.get("currentPrice") or i.get("regularMarketPrice")}}
    except Exception as e:
        return s, {{"error": str(e)[:120]}}
with ThreadPoolExecutor(max_workers=12) as pool:
    print({_MARK!r} + json.dumps(dict(pool.map(one, {list(symbols)!r}))))
''')
        return {"fetched_at": _now(), "rows": rows}

    def history(self, symbols: Sequence[str]) -> dict[str, Any]:
        rows = _run_in("maverick-mcp", f'''
import json
from concurrent.futures import ThreadPoolExecutor
import yfinance as yf
def one(s):
    try:
        return s, {{"bars": int(len(yf.Ticker(s).history(period="5y")))}}
    except Exception as e:
        return s, {{"error": str(e)[:120]}}
with ThreadPoolExecutor(max_workers=12) as pool:
    print({_MARK!r} + json.dumps(dict(pool.map(one, {list(symbols)!r}))))
''')
        return {"fetched_at": _now(), "rows": rows}

    def news(self, symbols: Sequence[str], windows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
        rows = _run_in("alpaca-mcp", f'''
import asyncio, json
from server import get_historical_news
async def main():
    out = {{}}
    for s in {list(symbols)!r}:
        out[s] = []
        for w in {[dict(w) for w in windows]!r}:
            try:
                r = await get_historical_news(s, w["start_at"], w["end_at"])
                out[s].append({{"articles": len(r["articles"])}} if r["complete"]
                              else {{"error": "incomplete pagination"}})
            except Exception as e:
                out[s].append({{"error": str(e)[:120]}})
    print({_MARK!r} + json.dumps(out))
asyncio.run(main())
''')
        return {"fetched_at": _now(), "rows": rows}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def summary(snap: Mapping[str, Any]) -> str:
    reasons: dict[str, int] = {}
    for r in snap["rows"]:
        for code in r["reasons"]:
            reasons[code] = reasons.get(code, 0) + 1
    lines = [
        f"screen {snap['screen_id']}: {snap['screen_status'].upper()}"
        + (f" ({', '.join(snap['failure_reasons'])})" if snap["failure_reasons"] else ""),
        f"universe: {snap['universe']['count']} names; " + ", ".join(f"{k} {v}" for k, v in snap["counts"].items()),
        "reasons: " + (", ".join(f"{k} {v}" for k, v in sorted(reasons.items())) or "none"),
    ]
    if snap["pool"] is not None:
        lines += [f"pool ({len(snap['pool'])}), effective from {snap['effective_from']}: {' '.join(snap['pool'])}",
                  "sector mix: " + ", ".join(f"{k} {v}" for k, v in sorted(snap["sector_mix"].items()))]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, providers: Providers | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scalable screener, Stage 1 (current universe only).")
    parser.add_argument("--screen-date", required=True, help="YYYY-MM-DD; the screen's identity date")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="run the screen and print it; write nothing")
    mode.add_argument("--write", action="store_true", help="publish the write-once snapshot")
    mode.add_argument("--usable", action="store_true",
                      help="no fetch: report which stored pool is usable on --screen-date")
    parser.add_argument("--output-dir", default=str(SNAPSHOT_DIR), help=f"snapshot directory (default {SNAPSHOT_DIR})")
    args = parser.parse_args(argv)
    day, out_dir = date.fromisoformat(args.screen_date), Path(args.output_dir)

    if args.usable:
        print(json.dumps(usable_pool(out_dir, day), indent=1))
        return 0
    providers = providers or LiveProviders()
    if args.dry_run:
        snap = run_screen(day, providers, snapshot_dir=out_dir)
        print(summary(snap) + "\nDRY RUN -- nothing written")
    else:
        snap, reused = screen(day, providers, out_dir)
        print(summary(snap) + f"\n{'reused existing' if reused else 'wrote'} {snapshot_path(out_dir, snap['screen_id'])}")
    return 0 if snap["screen_status"] == SCREEN_OK else 1


if __name__ == "__main__":
    sys.exit(main())
